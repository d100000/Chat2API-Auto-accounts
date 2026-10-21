"""Quota-aware account leases, with private restart-safe scheduling state.

The upstream snapshot remains authoritative. Reservations and provisional debits
are deliberately separate: one dispatched request conservatively reserves one
slot until a quota query *started after* it finished. This is a scheduling
estimate, not a claim about how ChatGPT charges a request.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid


FEATURES = ("reason", "image_gen")
MAX_EVENTS = 100
MAX_ACCOUNTS = 2000
_ID = re.compile(r"acct_[0-9a-f]{24}\Z")
_KINDS = {"quota", "auth", "rate_limit", "transient", "request"}
_EVENTS = {"selected", "switched", "exhausted", "rejoined", "cooldown", "auth_error", "recovered", "interrupted"}
_REASONS = {"quota_empty", "upstream_quota", "quota_refreshed", "rate_limit", "transient", "auth",
            "quota_priority", "unknown_fallback", "restart", "credentials", "cooldown_elapsed"}


def _stamp(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 253402300799:
        return float(value)
    if isinstance(value, str) and len(value) <= 64:
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return _stamp(result.timestamp()) if result.tzinfo else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _iso(value):
    stamp = _stamp(value)
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None


def _amount(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 10**15 else None


def _count(value):
    return min(value, 10**9) if type(value) is int and value >= 0 else 0


def _public_id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _required(feature):
    return FEATURES if feature == "image_gen" else ("reason",)


def _empty():
    return {"blocks": {}, "provisional": {}, "cooldown_until": None, "cooldown_reason": None,
            "auth_at": None, "last_selected_at": None, "last_event": None}


@dataclass(frozen=True)
class Lease:
    id: str
    token: str = field(repr=False)
    account_id: str
    feature: str
    acquired_at: float


class NoAvailableAccount(Exception):
    """No secret or upstream response is included in this public failure."""
    code = "no_available_account"

    def __init__(self, feature, retry_after_seconds=60):
        super().__init__("No account is currently available for this feature.")
        self.feature = feature
        self.retry_after_seconds = max(1, min(3600, math.ceil(retry_after_seconds)))


class AccountScheduler:
    def __init__(self, quota_service, *, state_path="data/account_scheduler.json", clock=time.time):
        self._quota = quota_service
        self._path = Path(state_path) if state_path is not None else None
        self._clock = clock
        self._lock = asyncio.Lock()
        self._active = {}
        self._states = {}
        self._events = []
        self._last_selected = {}
        self._persistence_error = False
        self._load()

    def _event(self, kind, account_id, feature=None, reason=None, from_id=None):
        event = {"at": _iso(self._clock()), "type": kind, "account_id": account_id,
                 "from_account_id": from_id, "to_account_id": account_id if kind in {"selected", "switched"} else None,
                 "feature": feature, "reason": reason}
        self._events = [*self._events, event][-MAX_EVENTS:]
        state = self._states.get(account_id, _empty())
        self._states = {**self._states, account_id: {**state, "last_event": event}}

    @staticmethod
    def _safe_event(value):
        if not isinstance(value, dict) or value.get("type") not in _EVENTS or not _public_id(value.get("account_id")):
            return None
        return {"at": _iso(value.get("at")), "type": value["type"], "account_id": value["account_id"],
                "from_account_id": _public_id(value.get("from_account_id")),
                "to_account_id": _public_id(value.get("to_account_id")),
                "feature": value.get("feature") if value.get("feature") in FEATURES else None,
                "reason": value.get("reason") if value.get("reason") in _REASONS else None}

    @staticmethod
    def _safe_state(value):
        if not isinstance(value, dict):
            return _empty()
        blocks, provisional = {}, {}
        for feature in FEATURES:
            block = value.get("blocks", {}).get(feature) if isinstance(value.get("blocks"), dict) else None
            if isinstance(block, dict) and _stamp(block.get("at")) is not None:
                blocks[feature] = {"at": _stamp(block["at"]), "reset_at": _stamp(block.get("reset_at")),
                                   "reason": "upstream_quota" if block.get("reason") == "upstream_quota" else "quota_empty"}
            debit = value.get("provisional", {}).get(feature) if isinstance(value.get("provisional"), dict) else None
            if isinstance(debit, dict) and _count(debit.get("count")) and _stamp(debit.get("finished_at")) is not None:
                provisional[feature] = {"count": _count(debit["count"]), "finished_at": _stamp(debit["finished_at"])}
        return {"blocks": blocks, "provisional": provisional,
                "cooldown_until": _stamp(value.get("cooldown_until")),
                "cooldown_reason": value.get("cooldown_reason") if value.get("cooldown_reason") in {"rate_limit", "transient"} else None,
                "auth_at": _stamp(value.get("auth_at")), "last_selected_at": _stamp(value.get("last_selected_at")),
                "last_event": AccountScheduler._safe_event(value.get("last_event"))}

    def _load(self):
        if self._path is None:
            return
        try:
            if self._path.stat().st_size > 8 * 1024 * 1024:
                self._persistence_error = True
                return
            document = json.loads(self._path.read_text())
            if not isinstance(document, dict) or document.get("version") != 1:
                self._persistence_error = True
                return
            states = document.get("accounts", {})
            if not isinstance(states, dict):
                self._persistence_error = True
                return
            self._states = {key: self._safe_state(value) for key, value in list(states.items())[-MAX_ACCOUNTS:] if _public_id(key)}
            events = document.get("events", [])
            if isinstance(events, list):
                self._events = [safe for row in events[-MAX_EVENTS:] if (safe := self._safe_event(row))]
            last = document.get("last_selected", {})
            if isinstance(last, dict):
                self._last_selected = {key: value for key, value in last.items() if key in FEATURES and _public_id(value)}
            # A process can die after dispatch but before recording the outcome.
            # Convert every old lease to a conservative debit; no token is stored.
            inflight = document.get("active", [])
            recovered = False
            for row in inflight[:10000] if isinstance(inflight, list) else []:
                if not isinstance(row, dict) or not _public_id(row.get("account_id")) or row.get("feature") not in FEATURES:
                    continue
                key = row["account_id"]
                self._debit(key, _required(row["feature"]), self._clock())
                self._event("interrupted", key, row["feature"], "restart")
                recovered = True
            if recovered:
                self._save()
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, AttributeError):
            self._persistence_error = True

    def _save(self):
        if self._path is None:
            return
        temporary = None
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".account-scheduler-", dir=self._path.parent)
            with os.fdopen(descriptor, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"version": 1, "accounts": self._states, "events": self._events,
                           "last_selected": self._last_selected,
                           "active": [{"account_id": lease.account_id, "feature": lease.feature,
                                       "acquired_at": lease.acquired_at} for lease in self._active.values()]}, handle)
            os.replace(temporary, self._path)
            self._persistence_error = False
        except OSError:
            self._persistence_error = True
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    self._persistence_error = True

    def _debit(self, account_id, features, at):
        state = self._states.get(account_id, _empty())
        provisional = dict(state["provisional"])
        for feature in features:
            old = provisional.get(feature, {})
            provisional = {**provisional, feature: {"count": min(10**9, old.get("count", 0) + 1),
                                                   "finished_at": max(old.get("finished_at", 0), at)}}
        self._states = {**self._states, account_id: {**state, "provisional": provisional}}

    @staticmethod
    def _successful_read(account):
        refresh = account.get("refresh", {})
        checked = _stamp(account.get("checked_at"))
        started = _stamp(refresh.get("last_attempt_at"))
        # in_progress/failed refreshes may still expose the preceding snapshot.
        if refresh.get("error") or refresh.get("status") in {"failed", "in_progress", "expired", "unsupported"}:
            return None
        return started if checked is not None and started is not None and started <= checked else None

    def _reconcile(self, snapshot):
        now = self._clock()
        before = (deepcopy(self._states), len(self._events))
        for account in snapshot.get("accounts", []):
            key = _public_id(account.get("id"))
            if not key:
                continue
            state = self._states.get(key, _empty())
            read_started = self._successful_read(account)
            checked = _stamp(account.get("checked_at"))
            blocks, provisional = dict(state["blocks"]), dict(state["provisional"])
            for feature in FEATURES:
                observed = account.get("features", {}).get(feature, {})
                remaining = _amount(observed.get("remaining"))
                reset = _stamp(observed.get("reset_at"))
                old = blocks.get(feature)
                exhausted = observed.get("blocked") is True or remaining == 0
                if exhausted:
                    if old is None:
                        blocks = {**blocks, feature: {"at": checked if checked is not None else now,
                                                     "reset_at": reset, "reason": "quota_empty"}}
                        self._event("exhausted", key, feature, "quota_empty")
                    elif reset is not None and (old.get("reset_at") is None or reset > old["reset_at"]):
                        blocks = {**blocks, feature: {**old, "reset_at": reset}}
                elif old is not None and remaining is not None and remaining > 0 and read_started is not None:
                    after_reset = old.get("reset_at") is None or read_started >= old["reset_at"]
                    if read_started > old["at"] and after_reset and account.get("status") == "fresh":
                        blocks = {name: block for name, block in blocks.items() if name != feature}
                        self._event("rejoined", key, feature, "quota_refreshed")
                debit = provisional.get(feature)
                if debit and read_started is not None and read_started > debit["finished_at"] and remaining is not None:
                    provisional = {name: value for name, value in provisional.items() if name != feature}
            auth_at = state["auth_at"]
            if auth_at is not None and read_started is not None and read_started > auth_at and account.get("credential_status") not in {"expired", "error"}:
                auth_at = None
                self._event("recovered", key, reason="quota_refreshed")
            cooldown = state["cooldown_until"]
            cooldown_reason = state["cooldown_reason"]
            if cooldown is not None and cooldown <= now:
                cooldown, cooldown_reason = None, None
                self._event("recovered", key, reason="cooldown_elapsed")
            # Preserve events just emitted while applying reconciled state.
            event = self._states.get(key, state).get("last_event")
            self._states = {**self._states, key: {**state, "blocks": blocks, "provisional": provisional,
                                                "auth_at": auth_at, "cooldown_until": cooldown,
                                                "cooldown_reason": cooldown_reason, "last_event": event}}
        if len(self._states) > MAX_ACCOUNTS:
            current = {row.get("id") for row in snapshot.get("accounts", [])}
            keep = sorted(self._states, key=lambda key: (key in current, self._states[key].get("last_selected_at") or 0), reverse=True)[:MAX_ACCOUNTS]
            self._states = {key: self._states[key] for key in keep}
        if before != (self._states, len(self._events)):
            self._save()

    def _active_counts(self, account_id):
        leases = [lease for lease in self._active.values() if lease.account_id == account_id]
        return len(leases), {feature: sum(feature in _required(lease.feature) for lease in leases) for feature in FEATURES}

    def _feature_state(self, account, feature):
        now = self._clock()
        key = account["id"]
        state = self._states.get(key, _empty())
        count, reserved = self._active_counts(key)
        observed = account.get("features", {}).get(feature, {})
        remaining = _amount(observed.get("remaining"))
        debit = state["provisional"].get(feature, {}).get("count", 0)
        effective = max(0, remaining - reserved[feature] - debit) if remaining is not None else None
        result = {"state": "ready", "reason": "quota_priority", "available": True,
                  "active": reserved[feature], "reserved": reserved[feature], "provisional": debit,
                  "reset_at": observed.get("reset_at"), "retry_at": None,
                  "effective_remaining": effective}
        credential = account.get("credential_status")
        block = state["blocks"].get(feature)
        if credential in {"expired", "error"}:
            return {**result, "state": "expired" if credential == "expired" else "auth_error", "reason": "credentials", "available": False}
        if state["auth_at"] is not None:
            return {**result, "state": "auth_error", "reason": "auth", "available": False}
        if state["cooldown_until"] is not None and state["cooldown_until"] > now:
            return {**result, "state": "cooldown", "reason": state["cooldown_reason"], "retry_at": _iso(state["cooldown_until"]), "available": False}
        if block:
            reset = block.get("reset_at")
            return {**result, "state": "reset_pending" if reset is not None and reset <= now else "exhausted",
                    "reason": block["reason"], "reset_at": _iso(reset), "retry_at": _iso(reset), "available": False}
        if effective is not None and effective <= 0:
            return {**result, "state": "active" if reserved[feature] else "reset_pending",
                    "reason": "reserved" if reserved[feature] else "awaiting_quota_refresh", "available": False}
        known_fresh = (account.get("status") == "fresh" and observed.get("status") == "known" and remaining is not None)
        if not known_fresh:
            return {**result, "state": "active" if count else ("stale" if account.get("status") == "stale" else "unknown"),
                    "reason": "unknown_fallback", "available": count == 0}
        return result

    def snapshot(self, quota_snapshot=None):
        source = quota_snapshot if quota_snapshot is not None else self._quota.get_accounts()
        self._reconcile(source)
        accounts = {}
        for row in source.get("accounts", []):
            if not _public_id(row.get("id")):
                continue
            key = row["id"]
            reason = self._feature_state(row, "reason")
            image = self._feature_state(row, "image_gen")
            if not reason["available"] and image["available"]:
                image = {**image, "state": reason["state"], "available": False,
                         "reason": "reason_" + str(reason["reason"]), "retry_at": reason["retry_at"]}
            state = self._states.get(key, _empty())
            accounts[key] = {"reason": reason, "image_gen": image,
                             "active_requests": self._active_counts(key)[0],
                             "last_selected_at": _iso(state["last_selected_at"]), "last_event": deepcopy(state["last_event"])}
        return {"enabled": True, "policy": "quota_aware", "accounts": accounts,
                "events": deepcopy(self._events[::-1]), "persistence_error": self._persistence_error}

    async def acquire(self, feature="reason", excluded=frozenset(), preferred_account_id=None):
        if feature not in FEATURES:
            raise ValueError("Unsupported scheduling feature")
        async with self._lock:
            source = self._quota.get_accounts()
            public = self.snapshot(source)
            groups = self._quota._groups()
            excluded = frozenset(excluded)
            ranked = []
            retries = []
            for row in source.get("accounts", []):
                key = row.get("id")
                if key not in public["accounts"] or key in excluded or key not in groups:
                    continue
                if preferred_account_id is not None and key != preferred_account_id:
                    continue
                per_feature = [public["accounts"][key][name] for name in _required(feature)]
                if not all(item["available"] for item in per_feature):
                    retries.extend(stamp - self._clock() for item in per_feature if (stamp := _stamp(item.get("retry_at"))) is not None and stamp > self._clock())
                    continue
                fresh = all(item["state"] == "ready" for item in per_feature)
                values = [item["effective_remaining"] for item in per_feature]
                capacity = min(values) if all(value is not None for value in values) else -1
                selected_at = self._states.get(key, _empty())["last_selected_at"] or 0
                ranked.append(((fresh, capacity, -selected_at, key), row, groups[key]))
            if not ranked:
                raise NoAvailableAccount(feature, min(retries) if retries else 60)
            _, row, group = max(ranked, key=lambda item: item[0])
            key, now = row["id"], self._clock()
            lease = Lease(uuid.uuid4().hex, group["token"], key, feature, now)
            self._active = {**self._active, lease.id: lease}
            state = self._states.get(key, _empty())
            self._states = {**self._states, key: {**state, "last_selected_at": now}}
            previous = self._last_selected.get(feature)
            fallback = any(public["accounts"][key][name]["state"] != "ready" for name in _required(feature))
            self._event("switched" if previous and previous != key else "selected", key, feature,
                        "unknown_fallback" if fallback else "quota_priority", previous)
            self._last_selected = {**self._last_selected, feature: key}
            self._save()
            return lease

    async def finish(self, lease, *, status, dispatched=False, error=None, image_requested=False):
        if not isinstance(status, str) or status not in {"succeeded", "failed", "cancelled", "interrupted"}:
            raise ValueError("Unsupported lease outcome")
        async with self._lock:
            live = self._active.get(lease.id)
            if live is None or live != lease:
                return
            self._active = {key: value for key, value in self._active.items() if key != lease.id}
            now, key = self._clock(), lease.account_id
            if dispatched:
                required = FEATURES if image_requested or lease.feature == "image_gen" else ("reason",)
                self._debit(key, required, now)
            evidence = error if isinstance(error, dict) and isinstance(error.get("kind"), str) and error["kind"] in _KINDS else {}
            kind = evidence.get("kind")
            state = self._states.get(key, _empty())
            if kind == "quota":
                affected = evidence.get("feature") if evidence.get("feature") in FEATURES else lease.feature
                reset = _stamp(evidence.get("reset_at"))
                block = {"at": now, "reset_at": reset, "reason": "upstream_quota"}
                self._states = {**self._states, key: {**state, "blocks": {**state["blocks"], affected: block}}}
                self._event("exhausted", key, affected, "upstream_quota")
            elif kind == "auth":
                self._states = {**self._states, key: {**state, "auth_at": now}}
                self._event("auth_error", key, lease.feature, "auth")
            elif kind in {"rate_limit", "transient"}:
                requested = _amount(evidence.get("retry_after_seconds"))
                seconds = max(1, min(300, requested if requested is not None else (30 if kind == "rate_limit" else 10)))
                until = max(state["cooldown_until"] or 0, now + seconds)
                self._states = {**self._states, key: {**state, "cooldown_until": until, "cooldown_reason": kind}}
                self._event("cooldown", key, lease.feature, kind)
            self._save()


_scheduler = None


def get_scheduler():
    global _scheduler
    if _scheduler is None:
        from chatgpt.account_quota import _default_service
        _scheduler = AccountScheduler(_default_service())
    return _scheduler
