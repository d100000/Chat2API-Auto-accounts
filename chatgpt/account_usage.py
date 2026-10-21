"""Local request observations, deliberately separate from upstream quota balances.

No prompts, output text, tokens, image URLs, or raw upstream errors are persisted.
An attempt is one selected-account execution; retries are separate attempts.
"""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from fastapi import HTTPException


LOG = logging.getLogger(__name__)
STATES = ("succeeded", "failed", "cancelled", "interrupted", "in_progress")
IMAGE_TOOLS = frozenset(("dalle.text2im", "image_gen", "imagegen", "image_generation", "t2uay3k.sj1i4kz"))


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def model_name(value):
    # Model labels are untrusted input. In particular, never persist arbitrary
    # text supplied where a model identifier should have been used.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", value) else "unknown"


def asset_key(value):
    if not isinstance(value, str) or "://" not in value:
        return None
    scheme, key = value.split("://", 1)
    return key if scheme in ("file-service", "sediment") and key else None


def assets_in(value):
    if isinstance(value, dict):
        own = asset_key(value.get("asset_pointer"))
        return frozenset(([own] if own else [])).union(*(assets_in(v) for v in value.values() if isinstance(v, (dict, list))))
    if isinstance(value, list):
        return frozenset().union(*(assets_in(v) for v in value))
    return frozenset()


class UsageStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    requested_model TEXT NOT NULL, upstream_model TEXT, sent_model TEXT,
                    started_at REAL NOT NULL, finished_at REAL,
                    status TEXT NOT NULL, image_requested INTEGER NOT NULL DEFAULT 0,
                    generated_images INTEGER NOT NULL DEFAULT 0, error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS attempts_account_time ON attempts(account_id, started_at);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('since', ?)", (time.time(),))
            # This application is deployed as one worker. A previous process
            # cannot complete its outstanding requests after it has restarted.
            db.execute("UPDATE attempts SET status='interrupted', finished_at=?, error_code='process_restarted' WHERE status='in_progress'", (time.time(),))

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start(self, account_id, requested_model, started_at=None):
        attempt_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO attempts (id, account_id, requested_model, started_at, status) VALUES (?, ?, ?, ?, 'in_progress')",
                       (attempt_id, account_id, model_name(requested_model), time.time() if started_at is None else started_at))
        return attempt_id

    def finish(self, attempt_id, status, evidence, error_code=None):
        if status not in STATES or status == "in_progress":
            raise ValueError("Invalid attempt outcome")
        with self.connect() as db:
            db.execute("""UPDATE attempts SET status=?, finished_at=?, upstream_model=?, sent_model=?,
                       image_requested=?, generated_images=?, error_code=? WHERE id=? AND status='in_progress'""",
                       (status, time.time(), evidence.upstream_model, evidence.sent_model, int(evidence.image_requested),
                        len(evidence.generated_assets), model_name(error_code) if error_code else None, attempt_id))

    @staticmethod
    def counts(rows):
        return {
            "attempts": len(rows),
            **{state: sum(row["status"] == state for row in rows) for state in STATES},
            "image_requests": sum(row["image_requested"] for row in rows),
            "generated_images": sum(row["generated_images"] for row in rows),
            "last_request_at": timestamp(max((row["started_at"] for row in rows), default=None)),
        }

    def summaries(self, account_ids, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            since = db.execute("SELECT value FROM metadata WHERE key='since'").fetchone()[0]
            # Query only currently imported accounts. Removed accounts retain
            # private history on disk but disappear from the management view.
            result = {}
            for account_id in account_ids:
                rows = [dict(row) for row in db.execute("SELECT requested_model, upstream_model, sent_model, started_at, status, image_requested, generated_images FROM attempts WHERE account_id=?", (account_id,))]
                pairs = sorted({(row["requested_model"], row["sent_model"] or "", row["upstream_model"] or "") for row in rows})
                result[account_id] = {
                    "since": timestamp(since), "totals": self.counts(rows),
                    "windows": {name: self.counts([row for row in rows if row["started_at"] >= now - seconds])
                                for name, seconds in (("last_5h", 18000), ("last_24h", 86400), ("last_7d", 604800))},
                    "models": [{"requested_model": requested, "sent_model": sent or None, "upstream_model": observed or None,
                                **self.counts([row for row in rows if row["requested_model"] == requested and (row["sent_model"] or "") == sent and (row["upstream_model"] or "") == observed])}
                               for requested, sent, observed in pairs],
                }
        return {"since": timestamp(since), "accounts": result}


@dataclass(frozen=True)
class Evidence:
    upstream_model: str | None = None
    sent_model: str | None = None
    image_requested: bool = False
    generated_assets: frozenset = frozenset()
    input_assets: frozenset = frozenset()
    input_message_ids: frozenset = frozenset()
    terminal: bool = False
    done_received: bool = False
    output_limited: bool = False
    error: bool = False
    output_started: bool = False


class UsageTrace:
    def __init__(self, store, attempt_id):
        self.store = store
        self.attempt_id = attempt_id
        self.evidence = Evidence()
        self.finished = False
        self.scheduling_error = None

    def exclude_inputs(self, messages):
        message_ids = frozenset(message["id"] for message in messages if isinstance(message, dict) and isinstance(message.get("id"), str))
        self.evidence = replace(self.evidence, input_assets=assets_in(messages), input_message_ids=message_ids)

    def set_model(self, sent_model):
        self.evidence = replace(self.evidence, sent_model=model_name(sent_model))

    def observe_image_result(self, model, count):
        """Record validated final native outputs without storing bytes or URLs."""
        if type(count) is not int or not 1 <= count <= 10:
            raise ValueError("Invalid image result count")
        self.evidence = replace(self.evidence, upstream_model=model_name(model), image_requested=True,
            generated_assets=frozenset("native-image-" + str(index) for index in range(count)),
            terminal=True, done_received=True, output_started=True)

    def observe_event(self, event):
        if not isinstance(event, dict):
            return
        if event.get("error") or event.get("errors"):
            from chatgpt.scheduling import error_signal
            self.scheduling_error = error_signal(event, feature="image_gen" if self.evidence.image_requested else getattr(self, "scheduled_feature", "reason"))
            self.evidence = replace(self.evidence, error=True)
            raise HTTPException(502, "Upstream stream reported an error.")
        message = event.get("message")
        if not isinstance(message, dict):
            return
        if isinstance(message.get("id"), str) and message["id"] in self.evidence.input_message_ids:
            return
        author = message.get("author") or {}
        if not isinstance(author, dict) or author.get("role") not in ("assistant", "tool"):
            return
        if message.get("status") in ("failed", "error", "finished_with_error", "cancelled"):
            from chatgpt.scheduling import error_signal
            self.scheduling_error = error_signal(message, feature="image_gen" if self.evidence.image_requested else getattr(self, "scheduled_feature", "reason"))
            self.evidence = replace(self.evidence, error=True)
            raise HTTPException(502, "Upstream message did not complete successfully.")
        metadata = message.get("metadata") or {}
        observed = model_name(metadata.get("model_slug")) if isinstance(metadata, dict) and metadata.get("model_slug") else self.evidence.upstream_model
        recipient = message.get("recipient")
        image_requested = self.evidence.image_requested or (isinstance(recipient, str) and recipient in IMAGE_TOOLS) or (isinstance(author.get("name"), str) and author["name"] in IMAGE_TOOLS)
        completed = message.get("status") == "finished_successfully"
        content = message.get("content") or {}
        output_assets = assets_in(content) - self.evidence.input_assets
        # Only image parts in a completed output message count. Never count an
        # input echo, a progress preview, a URL, or a textual promise to draw.
        parts = content.get("parts", []) if isinstance(content, dict) else []
        parts = parts if isinstance(parts, list) else []
        complete_images = frozenset(asset_key(part.get("asset_pointer")) for part in parts
                                    if isinstance(part, dict) and part.get("content_type") == "image_asset_pointer" and asset_key(part.get("asset_pointer"))) if completed else frozenset()
        generated = (complete_images & output_assets) | self.evidence.generated_assets
        output_started = (bool(parts) or (isinstance(content, dict) and bool(content.get("text"))) or
                          author.get("role") == "tool" or
                          (isinstance(recipient, str) and recipient not in ("", "all")) or
                          (completed and message.get("end_turn") is True))
        self.evidence = replace(self.evidence, upstream_model=observed, image_requested=image_requested or bool(generated),
                                output_started=self.evidence.output_started or output_started,
                                generated_assets=generated,
                                terminal=self.evidence.terminal or (completed and author.get("role") == "assistant" and message.get("end_turn") is True))

    async def observe_lines(self, lines):
        async for line in lines:
            text = line.decode("utf-8") if isinstance(line, bytes) else line
            if isinstance(text, str) and text.startswith("data:"):
                payload = text[5:].strip()
                if payload == "[DONE]":
                    self.evidence = replace(self.evidence, done_received=True)
                if payload and payload != "[DONE]":
                    try:
                        event = json.loads(payload)
                    except (ValueError, TypeError):
                        self.evidence = replace(self.evidence, error=True)
                        raise HTTPException(502, "Upstream stream contained invalid JSON.")
                    self.observe_event(event)
            yield line

    def observe_output(self, chunk):
        if not isinstance(chunk, str) or not chunk.startswith("data: {"):
            return
        try:
            value = json.loads(chunk[6:])
            if any(choice.get("finish_reason") == "length" for choice in value.get("choices", [])):
                self.evidence = replace(self.evidence, output_limited=True)
        except (ValueError, TypeError, AttributeError):
            return

    @property
    def complete(self):
        image_complete = bool(self.evidence.generated_assets) and self.evidence.done_received
        return not self.evidence.error and (self.evidence.terminal or self.evidence.output_limited or image_complete)

    async def finish(self, status, error_code=None):
        global _recording_error
        if self.finished:
            return
        self.finished = True
        if self.store is None:
            return
        try:
            await asyncio.to_thread(self.store.finish, self.attempt_id, status, self.evidence, error_code)
        except Exception:
            _recording_error = True
            LOG.error("Account usage outcome could not be saved; local statistics may be incomplete.")


_store = None
_store_lock = threading.Lock()
_recording_error = False


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = UsageStore(Path("data") / "account_usage.sqlite3")
    return _store


async def start_trace(token, requested_model, account_override=None):
    global _recording_error
    from chatgpt.account_quota import account_identity
    if not token:
        return UsageTrace(None, None)
    try:
        identity = account_identity(token, account_override)
        store = await asyncio.to_thread(get_store)
        attempt_id = await asyncio.to_thread(store.start, identity["id"], requested_model)
        return UsageTrace(store, attempt_id)
    except Exception:
        _recording_error = True
        LOG.error("Account usage attempt could not be saved; local statistics may be incomplete.")
        return UsageTrace(None, None)


def recording_status():
    return "incomplete" if _recording_error else "ok"


def mark_unavailable():
    global _recording_error
    _recording_error = True
