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

from chatgpt.account_concurrency import (ConcurrencyStore, ConcurrencyPersistenceError,
    credential_alias, get_concurrency_store)


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
    quota_features: tuple = ()
    channel: str = "chatgpt_web"

    @property
    def concurrency_kind(self):
        return self.feature


class NoAvailableAccount(Exception):
    """No secret or upstream response is included in this public failure."""
    code = "no_available_account"

    def __init__(self, feature, retry_after_seconds=60):
        super().__init__("No account is currently available for this feature.")
        self.feature = feature
        self.retry_after_seconds = max(1, min(3600, math.ceil(retry_after_seconds)))


class AccountConcurrencyExhausted(NoAvailableAccount):
    code = "account_concurrency_exhausted"

    def __init__(self, feature):
        super().__init__(feature, 3)


class ModelNotAvailable(NoAvailableAccount):
    code = "model_not_available"


class AccountScheduler:
    def __init__(self, quota_service, *, state_path="data/account_scheduler.json", clock=time.time,
                 concurrency_store=None, model_support=None):
        self._quota = quota_service
        self._path = Path(state_path) if state_path is not None else None
        self._clock = clock
        # Tests/custom state paths get an adjacent isolated policy store.
        self.concurrency = concurrency_store if concurrency_store is not None else ConcurrencyStore(
            self._path.with_name("account_concurrency.json") if self._path is not None else None)
        self._model_support = model_support
        self._lock = asyncio.Lock()
        self._active = {}
        self._states = {}
        self._events = []
        self._last_selected = {}
        self._persistence_error = False
        self._load()

    def admission_guard(self):
        """Coordinate import publication and administrative policy changes."""
        return self._lock

    async def update_concurrency(self, **kwargs):
        async with self._lock:
            self._sync_identities(self._quota._groups())
            return self.concurrency.update(**kwargs)

    async def synchronize_identities(self):
        """Call before an administrative snapshot after credentials refresh."""
        async with self._lock:
            self.synchronize_identities_locked()

    def synchronize_identities_locked(self):
        """Synchronous counterpart for callers already holding admission_guard."""
        self._sync_identities(self._quota._groups())

    def _supports(self, account_id, model, channel="chatgpt_web"):
        if model is None or channel != "chatgpt_web":
            return True
        check = self._model_support
        if check is None:
            from chatgpt.model_catalog import account_supports_model
            check = account_supports_model
        return bool(check(account_id, model, channel=channel))

    def _merge_runtime(self, previous, target):
        old = self._states.get(previous)
        if old is not None:
            new = self._states.get(target, _empty())
            blocks = dict(new["blocks"])
            debits = dict(new["provisional"])
            for feature, value in old["blocks"].items():
                existing = blocks.get(feature)
                blocks[feature] = value if existing is None else {
                    "at": max(value["at"], existing["at"]), "reason": "upstream_quota",
                    "reset_at": max(value.get("reset_at") or 0, existing.get("reset_at") or 0) or None}
            for feature, value in old["provisional"].items():
                existing = debits.get(feature, {})
                debits[feature] = {"count": min(10**9, value["count"] + existing.get("count", 0)),
                    "finished_at": max(value["finished_at"], existing.get("finished_at", 0))}
            merged = {**new, "blocks": blocks, "provisional": debits,
                "cooldown_until": max(old["cooldown_until"] or 0, new["cooldown_until"] or 0) or None,
                "cooldown_reason": new["cooldown_reason"] or old["cooldown_reason"],
                "auth_at": max(old["auth_at"] or 0, new["auth_at"] or 0) or None,
                "last_selected_at": max(old["last_selected_at"] or 0, new["last_selected_at"] or 0) or None}
            self._states = {**{key: value for key, value in self._states.items() if key != previous}, target: merged}
        self._last_selected = {feature: target if key == previous else key for feature, key in self._last_selected.items()}

    def bind_identity(self, token, account_id, *, account_override=None):
        """Call under admission_guard when importing/confirming a new identity."""
        migrations = self.concurrency.bind_alias(account_id, credential_alias(token, account_override))
        for previous, target in migrations:
            self._merge_runtime(previous, target)
        if migrations:
            self._save()
        return self.concurrency.resolve(account_id)

    def _sync_identities(self, groups):
        # Include every credential, not only the group's selected token. This
        # preserves overrides when a refresh-token fingerprint becomes a JWT ID.
        token_source = getattr(self._quota, "_token_source", None)
        if token_source is not None:
            from chatgpt.account_quota import _identity
            refresh_source = getattr(self._quota, "_refresh_map_source", lambda: {})
            refresh_map = refresh_source()
            for token in tuple(token_source()):
                identity = _identity(token, refresh_map=refresh_map)
                self.bind_identity(token, identity["id"])
        else:
            for account_id, group in groups.items():
                self.bind_identity(group["token"], account_id)

    def _capacity(self, account_id, feature):
        key = self.concurrency.resolve(account_id)
        policy = self.concurrency.limits(key)
        active = sum(self.concurrency.resolve(lease.account_id) == key and lease.feature == feature for lease in self._active.values())
        limit = policy["effective"][feature]
        return {"active": active, "limit": limit, "available_slots": max(0, limit - active),
                "available": active < limit, "state": "draining" if active > limit else "full" if active == limit else "available",
                "source": "default" if policy["overrides"][feature] is None else "account", "conflict": policy["conflict"]}

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
                features = row.get("quota_features", _required(row["feature"]))
                if not isinstance(features, (list, tuple)) or any(item not in FEATURES for item in features):
                    continue
                self._debit(self.concurrency.resolve(key), features, self._clock())
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
                           "active": [{"account_id": self.concurrency.resolve(lease.account_id), "feature": lease.feature,
                                       "quota_features": list(lease.quota_features),
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
            raw_key = _public_id(account.get("id"))
            if not raw_key:
                continue
            key = self.concurrency.resolve(raw_key)
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
        key = self.concurrency.resolve(account_id)
        leases = [lease for lease in self._active.values() if self.concurrency.resolve(lease.account_id) == key]
        return len(leases), {feature: sum(feature in lease.quota_features for lease in leases) for feature in FEATURES}

    def _feature_state(self, account, feature):
        now = self._clock()
        key = self.concurrency.resolve(account["id"])
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
            capacity = {feature: self._capacity(key, feature) for feature in FEATURES}
            def with_capacity(value, feature):
                slot = capacity[feature]
                result = {**value, "active": slot["active"], "limit": slot["limit"], "available_slots": slot["available_slots"]}
                if value["available"] and not slot["available"]:
                    result = {**result, "available": False, "state": "concurrency_full", "reason": "concurrency_full"}
                return result
            reason, image = with_capacity(reason, "reason"), with_capacity(image, "image_gen")
            state = self._states.get(self.concurrency.resolve(key), _empty())
            accounts[key] = {"reason": reason, "image_gen": image,
                             "concurrency": capacity,
                             "active_requests": self._active_counts(key)[0],
                             "last_selected_at": _iso(state["last_selected_at"]), "last_event": deepcopy(state["last_event"])}
        return {"enabled": True, "policy": "quota_aware", "accounts": accounts,
                "concurrency": self.concurrency.snapshot(accounts),
                "events": deepcopy(self._events[::-1]), "persistence_error": self._persistence_error}

    async def acquire(self, feature="reason", excluded=frozenset(), preferred_account_id=None, model=None):
        if feature not in FEATURES:
            raise ValueError("Unsupported scheduling feature")
        async with self._lock:
            source = self._quota.get_accounts()
            self._sync_identities(self._quota._groups())
            public = self.snapshot(source)
            groups = self._quota._groups()
            excluded = frozenset(excluded)
            ranked = []
            retries = []
            capacity_blocked = False
            model_blocked = False
            model_candidates = 0
            for row in source.get("accounts", []):
                key = row.get("id")
                if key not in public["accounts"] or key in excluded or key not in groups:
                    continue
                if preferred_account_id is not None and key != preferred_account_id:
                    continue
                canonical = self.concurrency.resolve(key)
                if not self._supports(canonical, model):
                    model_blocked = True
                    continue
                model_candidates += 1
                if not self._capacity(canonical, feature)["available"]:
                    capacity_blocked = True
                    continue
                # Quota dependencies do not imply concurrency dependencies:
                # an image reserves both balances but only one image slot.
                per_feature = [self._feature_state(row, name) for name in _required(feature)]
                if not all(item["available"] for item in per_feature):
                    retries.extend(stamp - self._clock() for item in per_feature if (stamp := _stamp(item.get("retry_at"))) is not None and stamp > self._clock())
                    continue
                fresh = all(item["state"] == "ready" for item in per_feature)
                values = [item["effective_remaining"] for item in per_feature]
                capacity = min(values) if all(value is not None for value in values) else -1
                selected_at = self._states.get(key, _empty())["last_selected_at"] or 0
                ranked.append(((fresh, capacity, -selected_at, key), row, groups[key]))
            if not ranked:
                if capacity_blocked:
                    raise AccountConcurrencyExhausted(feature)
                if model_blocked and not model_candidates:
                    raise ModelNotAvailable(feature)
                raise NoAvailableAccount(feature, min(retries) if retries else 60)
            _, row, group = max(ranked, key=lambda item: item[0])
            key, now = self.concurrency.resolve(row["id"]), self._clock()
            lease = Lease(uuid.uuid4().hex, group["token"], key, feature, now, _required(feature))
            self._active = {**self._active, lease.id: lease}
            state = self._states.get(key, _empty())
            self._states = {**self._states, key: {**state, "last_selected_at": now}}
            previous = self._last_selected.get(feature)
            fallback = any(self._feature_state(row, name)["state"] != "ready" for name in _required(feature))
            self._event("switched" if previous and previous != key else "selected", key, feature,
                        "unknown_fallback" if fallback else "quota_priority", previous)
            self._last_selected = {**self._last_selected, feature: key}
            self._save()
            return lease

    async def acquire_for_account(self, token, feature="reason", *, account_id=None,
                                  account_override=None, model=None, quota_mode="none", channel="chatgpt_web"):
        """Capacity for a fixed direct/Seed/native identity, without switching."""
        if feature not in FEATURES or quota_mode != "none" or channel not in {"chatgpt_web", "codex_native"}:
            raise ValueError("Unsupported fixed-account admission mode")
        from chatgpt.account_quota import account_identity
        identifier = account_id or account_identity(token, account_override)["id"]
        async with self._lock:
            self._sync_identities(self._quota._groups())
            key = self.bind_identity(token, identifier, account_override=account_override)
            if not self._supports(key, model, channel):
                raise ModelNotAvailable(feature)
            if not self._capacity(key, feature)["available"]:
                raise AccountConcurrencyExhausted(feature)
            lease = Lease(uuid.uuid4().hex, token, key, feature, self._clock(), (), channel)
            self._active = {**self._active, lease.id: lease}
            self._save()
            return lease

    async def confirm_identity(self, lease, account_id, *, account_override=None):
        """After access-token refresh, merge identities before dispatching.

        If two previously separate credentials now exceed the shared cap, reject
        this not-yet-dispatched request; the caller's finally releases its lease.
        Existing upstream work is never cancelled by a settings change.
        """
        async with self._lock:
            old_key = self.concurrency.resolve(lease.account_id)
            key = self.bind_identity(lease.token, account_id, account_override=account_override)
            # A different alias spelling must still migrate this active lease.
            if old_key != key and self.concurrency.resolve(old_key) != key:
                key = self.bind_identity(lease.token, account_id)
            if lease.account_id != key and self._capacity(key, lease.feature)["active"] > self._capacity(key, lease.feature)["limit"]:
                raise AccountConcurrencyExhausted(lease.feature)
            return key

    async def finish(self, lease, *, status, dispatched=False, error=None, image_requested=False):
        if not isinstance(status, str) or status not in {"succeeded", "failed", "cancelled", "interrupted"}:
            raise ValueError("Unsupported lease outcome")
        async with self._lock:
            live = self._active.get(lease.id)
            if live is None or live != lease:
                return
            self._active = {key: value for key, value in self._active.items() if key != lease.id}
            now, key = self._clock(), self.concurrency.resolve(lease.account_id)
            if dispatched and lease.quota_features:
                required = FEATURES if image_requested or lease.feature == "image_gen" else ("reason",)
                self._debit(key, required, now)
            evidence = error if isinstance(error, dict) and isinstance(error.get("kind"), str) and error["kind"] in _KINDS else {}
            kind = evidence.get("kind")
            state = self._states.get(key, _empty())
            if not lease.quota_features:
                self._save()
                return
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
        _scheduler = AccountScheduler(_default_service(), concurrency_store=get_concurrency_store())
    return _scheduler
