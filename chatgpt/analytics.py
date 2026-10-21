"""One row per validated client generation request, independent of attempts.

Only opaque key IDs and bounded usage metadata enter this ledger. It never
imports account credentials, prompts, outputs, URLs, or the old attempt ledger.
Prices are supplied at completion and remain frozen; reads do not recalculate.
The application uses one worker. A new runtime marks its predecessor's pending
requests interrupted, while reopening the store in the same runtime is safe.
"""

from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timedelta, timezone as utc_timezone
from decimal import Decimal
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


STATUSES = ("succeeded", "failed", "cancelled", "interrupted", "in_progress")
KINDS = ("chat", "image")
ENDPOINTS = {"chat": "/v1/chat/completions", "image": "/v1/images/generations"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_KEY_ID = re.compile(r"(?:key_[0-9a-f]{24}|system_[0-9a-f]{16,64}|direct)\Z")
_REQUEST_ID = re.compile(r"req_[0-9a-f]{32}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_RUNTIME_ID = uuid.uuid4().hex
_PUBLIC_COLUMNS = (
    "id,key_id,kind,requested_model,upstream_model,started_at,finished_at,status,"
    "http_status,duration_ms,prompt_tokens,completion_tokens,generated_images,"
    "image_requests,token_source,estimated_cost_usd,estimated_text_cost_usd,"
    "estimated_image_cost_usd,price_source,rate_version,pricing_status,error_code"
)
_MONEY_FIELDS = ("estimated_cost_usd", "estimated_text_cost_usd", "estimated_image_cost_usd")


class ValidationError(ValueError):
    """Locally defined validation messages; never echo rejected input."""


def _identifier(value, field, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValidationError(field + " must be a safe identifier of at most 100 characters.")
    return value


def _key_id(value):
    if not isinstance(value, str) or not _KEY_ID.fullmatch(value):
        raise ValidationError("key_id must be an opaque managed/system key ID or direct.")
    return value


def _choice(value, choices, field):
    if not isinstance(value, str) or value not in choices:
        raise ValidationError("Unsupported " + field + ".")
    return value


def _count(value, field, *, optional=False, maximum=10**12):
    if value is None and optional:
        return None
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValidationError(field + " must be a non-negative integer within the supported limit.")
    return value


def _money(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValidationError(field + " must be a finite non-negative amount or null.")
    try:
        amount = float(value)
    except (ValueError, OverflowError):
        raise ValidationError(field + " must be a finite non-negative amount or null.") from None
    if not math.isfinite(amount) or not 0 <= amount <= 10**9:
        raise ValidationError(field + " must be a finite non-negative amount or null.")
    return amount


def _stamp(value):
    return datetime.fromtimestamp(value, utc_timezone.utc).isoformat(timespec="milliseconds") if value is not None else None


def _range(days, date_from, date_to, timezone, now):
    if type(days) is not int or days not in (7, 30, 90):
        raise ValidationError("days must be 7, 30 or 90; use explicit dates for a custom range.")
    if not isinstance(timezone, str) or not 1 <= len(timezone) <= 100:
        raise ValidationError("timezone must be a supported IANA timezone.")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError("timezone must be a supported IANA timezone.") from None
    if (date_from is None) != (date_to is None):
        raise ValidationError("date_from and date_to must be supplied together.")
    try:
        if date_from is None:
            end = datetime.fromtimestamp(now, zone).date()
            start = end - timedelta(days=days - 1)
        else:
            if not all(isinstance(value, str) and _DATE.fullmatch(value) for value in (date_from, date_to)):
                raise ValidationError("Dates must use YYYY-MM-DD.")
            start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        count = (end - start).days + 1
        if not 1 <= count <= 366:
            raise ValidationError("The date range must contain 1 to 366 calendar days.")
        first = datetime.combine(start, day_time.min, zone).timestamp()
        # Calendar arithmetic rather than +86400 preserves 23/25-hour DST days.
        last = datetime.combine(end + timedelta(days=1), day_time.min, zone).timestamp()
    except (ValueError, OverflowError, OSError) as error:
        if isinstance(error, ValidationError):
            raise
        raise ValidationError("Invalid date range.") from None
    return zone, start, end, first, last, count


def _bucket():
    return {
        "requests": 0, **{state: 0 for state in STATUSES},
        "prompt_tokens": 0, "completion_tokens": 0, "generated_images": 0, "image_requests": 0,
        "known_cost_requests": 0, "unknown_cost_requests": 0,
        "known_text_cost_requests": 0, "known_image_cost_requests": 0,
        "token_unknown_requests": 0, "_duration_sum": 0, "_duration_count": 0,
        **{field: Decimal(0) for field in _MONEY_FIELDS},
    }


def _add(bucket, row):
    bucket["requests"] += 1
    bucket[row["status"]] += 1
    for field in ("prompt_tokens", "completion_tokens", "generated_images", "image_requests"):
        bucket[field] += row[field] or 0
    if row["prompt_tokens"] is None or row["completion_tokens"] is None:
        bucket["token_unknown_requests"] += 1
    known = row["pricing_status"] == "known" and row["estimated_cost_usd"] is not None
    bucket["known_cost_requests" if known else "unknown_cost_requests"] += 1
    for field in _MONEY_FIELDS:
        if row[field] is not None:
            bucket[field] += Decimal(str(row[field]))
    bucket["known_text_cost_requests"] += row["estimated_text_cost_usd"] is not None
    bucket["known_image_cost_requests"] += row["estimated_image_cost_usd"] is not None
    if row["duration_ms"] is not None:
        bucket["_duration_sum"] += row["duration_ms"]
        bucket["_duration_count"] += 1


def _summary(bucket):
    result = {key: value for key, value in bucket.items() if not key.startswith("_")}
    result.update({field: float(bucket[field]) for field in _MONEY_FIELDS})
    result["avg_duration_ms"] = round(bucket["_duration_sum"] / bucket["_duration_count"], 2) if bucket["_duration_count"] else None
    result["success_rate"] = bucket["succeeded"] / bucket["requests"] if bucket["requests"] else 0.0
    result["total_tokens"] = bucket["prompt_tokens"] + bucket["completion_tokens"]
    return result


class AnalyticsStore:
    def __init__(self, path=Path("data/request_usage.sqlite3"), *, clock=time.time):
        self.path = Path(path)
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        now = self._now()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, key_id TEXT NOT NULL, kind TEXT NOT NULL,
                    requested_model TEXT NOT NULL, upstream_model TEXT,
                    started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                    http_status INTEGER, duration_ms INTEGER,
                    prompt_tokens INTEGER, completion_tokens INTEGER,
                    generated_images INTEGER NOT NULL DEFAULT 0,
                    image_requests INTEGER NOT NULL DEFAULT 0, token_source TEXT,
                    estimated_cost_usd REAL, estimated_text_cost_usd REAL, estimated_image_cost_usd REAL,
                    price_source TEXT, rate_version TEXT, pricing_status TEXT NOT NULL DEFAULT 'unknown',
                    error_code TEXT, runtime_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requests_time ON requests(started_at);
                CREATE INDEX IF NOT EXISTS requests_key_time ON requests(key_id, started_at);
                CREATE INDEX IF NOT EXISTS requests_model_time ON requests(requested_model, started_at);
            """)
            db.execute("INSERT OR IGNORE INTO metadata (key,value) VALUES ('since',?)", (now,))
            db.execute("""UPDATE requests SET status='interrupted', finished_at=?,
                duration_ms=CAST(MAX(0, (? - started_at) * 1000) AS INTEGER), error_code='process_restarted'
                WHERE status='in_progress' AND runtime_id != ?""", (now, now, _RUNTIME_ID))

    def _now(self):
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 253402214400:
            raise ValidationError("clock must return a supported UTC timestamp.")
        return float(value)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start(self, *, key_id, kind, requested_model, image_requests=None):
        key_id = _key_id(key_id)
        kind = _choice(kind, KINDS, "kind")
        requested_model = _identifier(requested_model, "requested_model")
        image_requests = int(kind == "image") if image_requests is None else _count(image_requests, "image_requests", maximum=1)
        if kind == "image" and image_requests != 1:
            raise ValidationError("An image endpoint request must count as one image request.")
        identifier = "req_" + uuid.uuid4().hex
        with self.connect() as db:
            db.execute("""INSERT INTO requests
                (id,key_id,kind,requested_model,started_at,status,image_requests,runtime_id)
                VALUES (?,?,?,?,?,'in_progress',?,?)""",
                (identifier, key_id, kind, requested_model, self._now(), image_requests, _RUNTIME_ID))
        return identifier

    def finish(self, request_id, *, status, http_status=None, upstream_model=None,
               prompt_tokens=None, completion_tokens=None, generated_images=0,
               image_requests=None, token_source=None, estimated_cost_usd=None,
               estimated_text_cost_usd=None, estimated_image_cost_usd=None,
               price_source=None, rate_version=None, pricing_status="unknown", error_code=None):
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ValidationError("Invalid request ID.")
        status = _choice(status, STATUSES[:-1], "status")
        if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
            raise ValidationError("http_status must be a valid HTTP status or null.")
        upstream_model = _identifier(upstream_model, "upstream_model", optional=True)
        prompt_tokens = _count(prompt_tokens, "prompt_tokens", optional=True)
        completion_tokens = _count(completion_tokens, "completion_tokens", optional=True)
        generated_images = _count(generated_images, "generated_images", maximum=10000)
        image_requests = _count(image_requests, "image_requests", optional=True, maximum=1)
        token_source = _identifier(token_source, "token_source", optional=True)
        price_source = _choice(price_source, ("official", "custom", "unknown"), "price_source") if price_source is not None else None
        rate_version = _identifier(rate_version, "rate_version", optional=True)
        error_code = _identifier(error_code, "error_code", optional=True)
        pricing_status = _choice(pricing_status, ("known", "unknown"), "pricing_status")
        estimated_cost_usd = _money(estimated_cost_usd, "estimated_cost_usd")
        estimated_text_cost_usd = _money(estimated_text_cost_usd, "estimated_text_cost_usd")
        estimated_image_cost_usd = _money(estimated_image_cost_usd, "estimated_image_cost_usd")
        if (pricing_status == "known") != (estimated_cost_usd is not None):
            raise ValidationError("Only known pricing may contain an estimated total cost.")
        now = self._now()
        with self.connect() as db:
            cursor = db.execute("""UPDATE requests SET status=?, finished_at=?,
                duration_ms=CAST(MAX(0, (? - started_at) * 1000) AS INTEGER), http_status=?,
                upstream_model=?, prompt_tokens=?, completion_tokens=?, generated_images=?,
                image_requests=MAX(image_requests, COALESCE(?,0), CASE WHEN ? > 0 THEN 1 ELSE 0 END),
                token_source=?, estimated_cost_usd=?, estimated_text_cost_usd=?, estimated_image_cost_usd=?,
                price_source=?, rate_version=?, pricing_status=?, error_code=?
                WHERE id=? AND status='in_progress'""",
                (status, now, now, http_status, upstream_model, prompt_tokens, completion_tokens, generated_images,
                 image_requests, generated_images, token_source, estimated_cost_usd, estimated_text_cost_usd,
                 estimated_image_cost_usd, price_source, rate_version, pricing_status, error_code, request_id))
            return cursor.rowcount == 1

    def snapshot(self, *, days=7, date_from=None, date_to=None, api_key_id=None, model=None,
                 kind=None, status=None, timezone="Asia/Shanghai"):
        now = self._now()
        zone, first_day, last_day, first, last, count = _range(days, date_from, date_to, timezone, now)
        values = {"api_key_id": _key_id(api_key_id) if api_key_id is not None else None,
                  "model": _identifier(model, "model", optional=True),
                  "kind": _choice(kind, KINDS, "kind") if kind is not None else None,
                  "status": _choice(status, STATUSES, "status") if status is not None else None}
        clauses, parameters = ["started_at >= ?", "started_at < ?"], [first, last]
        for name, column in (("api_key_id", "key_id"), ("model", "requested_model"), ("kind", "kind"), ("status", "status")):
            if values[name] is not None:
                clauses.append(column + " = ?")
                parameters.append(values[name])
        where = " AND ".join(clauses)
        total = _bucket()
        daily = {(first_day + timedelta(days=offset)).isoformat(): _bucket() for offset in range(count)}
        hourly = {hour: _bucket() for hour in range(24)}
        groups = {"models": {}, "keys": {}, "endpoints": {}, "statuses": {value: _bucket() for value in STATUSES}}
        with self.connect() as db:
            db.execute("BEGIN")
            since = db.execute("SELECT value FROM metadata WHERE key='since'").fetchone()[0]
            for row in db.execute("SELECT " + _PUBLIC_COLUMNS + " FROM requests WHERE " + where, parameters):
                local = datetime.fromtimestamp(row["started_at"], zone)
                for bucket in (total, daily[local.date().isoformat()], hourly[local.hour]):
                    _add(bucket, row)
                for group, value in (("models", row["requested_model"]), ("keys", row["key_id"]),
                                     ("endpoints", row["kind"]), ("statuses", row["status"])):
                    bucket = groups[group].setdefault(value, _bucket())
                    _add(bucket, row)
            recent = [dict(row) for row in db.execute("SELECT " + _PUBLIC_COLUMNS + " FROM requests WHERE " + where +
                                                     " ORDER BY started_at DESC, rowid DESC LIMIT 50", parameters)]
            options = {
                "models": [row[0] for row in db.execute("SELECT requested_model FROM requests GROUP BY requested_model ORDER BY MAX(started_at) DESC, requested_model LIMIT 200")],
                "keys": [row[0] for row in db.execute("SELECT key_id FROM requests GROUP BY key_id ORDER BY MAX(started_at) DESC, key_id LIMIT 1000")],
            }
        for row in recent:
            row["started_at"], row["finished_at"] = _stamp(row["started_at"]), _stamp(row["finished_at"])
        group_result = {}
        for name, field in (("models", "model"), ("keys", "key_id"), ("endpoints", "kind"), ("statuses", "status")):
            ordered = sorted(groups[name].items(), key=lambda item: (-item[1]["requests"], item[0]))
            group_result[name] = [{field: identifier, **_summary(bucket),
                                   **({"endpoint": ENDPOINTS[identifier]} if name == "endpoints" else {})}
                                  for identifier, bucket in ordered]
        requests = total["requests"]
        return {
            "range": {"date_from": first_day.isoformat(), "date_to": last_day.isoformat(), "days": count, "timezone": timezone},
            "filters": values, "total": _summary(total),
            "daily": [{"date": key, **_summary(value)} for key, value in daily.items()],
            "hourly": [{"hour": key, **_summary(value)} for key, value in hourly.items()],
            **group_result, "recent": recent, "options": options,
            "coverage": {"requests": requests, "known_cost_requests": total["known_cost_requests"],
                "unknown_cost_requests": total["unknown_cost_requests"],
                "known_cost_ratio": total["known_cost_requests"] / requests if requests else 0.0,
                "token_known_requests": requests - total["token_unknown_requests"],
                "token_unknown_requests": total["token_unknown_requests"],
                "token_known_ratio": (requests - total["token_unknown_requests"]) / requests if requests else 0.0},
            "since": _stamp(since),
            "legacy": {"included": False, "message": "仅统计启用请求账本后的客户端请求；历史账号执行记录未归属到 API Key，也未回填。"},
        }


_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = AnalyticsStore()
    return _store
