"""Private account admission policy. No credentials, prompts or active leases.

All request admission and administrative writes are coordinated by the
AccountScheduler lock. The store also serializes standalone reads/writes.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

FEATURES = ("reason", "image_gen")
DEFAULTS = {"reason": 3, "image_gen": 1}
_ID = re.compile(r"acct_[0-9a-f]{24}\Z")
_ALIAS = re.compile(r"cred_[0-9a-f]{64}\Z")
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data/account_concurrency.json"


class ConcurrencyValidationError(ValueError):
    pass


class ConcurrencyConflict(ConcurrencyValidationError):
    pass


class ConcurrencyPersistenceError(RuntimeError):
    pass


def credential_alias(token, account_override=None):
    """Stable across access-token cache changes; never expose this to clients."""
    if not isinstance(token, str) or not token.strip():
        raise ConcurrencyValidationError("无法确定账号凭据身份。")
    raw = token.strip() + ("\0" + str(account_override) if account_override else "")
    return "cred_" + hashlib.sha256(raw.encode()).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ConcurrencyValidationError("账号标识无效。")
    return value


def _values(value, nullable=False):
    if not isinstance(value, dict) or set(value) - set(FEATURES):
        raise ConcurrencyValidationError("并发配置仅支持 reason 和 image_gen。")
    for limit in value.values():
        if nullable and limit is None:
            continue
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ConcurrencyValidationError("并发上限必须是 1 至 10 的整数。")
    return dict(value)


class ConcurrencyStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._document = {"version": 1, "revision": 0, "defaults": dict(DEFAULTS),
                          "accounts": {}, "aliases": {}, "redirects": {}}
        self.persistence_error = False
        if self.path is not None:
            try:
                if self.path.is_symlink() or self.path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError
                value = json.loads(self.path.read_text())
                if not isinstance(value, dict) or set(value) != set(self._document) or value["version"] != 1:
                    raise ValueError
                if type(value["revision"]) is not int or value["revision"] < 0:
                    raise ValueError
                if set(_values(value["defaults"])) != set(FEATURES):
                    raise ValueError
                for name in ("accounts", "aliases", "redirects"):
                    if not isinstance(value[name], dict) or len(value[name]) > 10000:
                        raise ValueError
                for key, row in value["accounts"].items():
                    _identifier(key)
                    if not isinstance(row, dict) or set(row) != {"overrides", "conflict"} or type(row["conflict"]) is not bool:
                        raise ValueError
                    if set(_values(row["overrides"], True)) != set(FEATURES):
                        raise ValueError
                for key, target in value["aliases"].items():
                    if not _ALIAS.fullmatch(key):
                        raise ValueError
                    _identifier(target)
                for key, target in value["redirects"].items():
                    _identifier(key); _identifier(target)
                self._document = value
                for key in value["redirects"]:
                    self.resolve(key)
                os.chmod(self.path, 0o600)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, KeyError):
                self.persistence_error = True

    def _healthy(self):
        if self.persistence_error:
            raise ConcurrencyPersistenceError("并发配置无法读取，请修复配置后重启；未放宽账号上限。")

    def resolve(self, account_id):
        key = _identifier(account_id)
        seen = set()
        while key in self._document["redirects"]:
            if key in seen:
                raise ConcurrencyValidationError("账号身份映射无效。")
            seen.add(key)
            key = self._document["redirects"][key]
        return key

    def _commit(self, document, *, bump=True):
        document = {**document, "revision": self._document["revision"] + int(bump)}
        temporary = None
        try:
            if self.path is not None:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self.path.is_symlink():
                    raise OSError
                fd, temporary = tempfile.mkstemp(prefix=".account-concurrency-", dir=self.path.parent)
                with os.fdopen(fd, "w") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    json.dump(document, handle, separators=(",", ":"))
                    handle.flush(); os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                temporary = None
            self._document = document
        except OSError:
            raise ConcurrencyPersistenceError("并发配置保存失败，原配置保持有效。") from None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def snapshot(self, account_ids=()):
        with self._lock:
            self._healthy()
            ids = {self.resolve(key) for key in account_ids} | set(self._document["accounts"])
            defaults = self._document["defaults"]
            accounts = {}
            for key in sorted(ids):
                row = self._document["accounts"].get(key, {"overrides": dict.fromkeys(FEATURES), "conflict": False})
                accounts[key] = {**deepcopy(row), "effective": {
                    feature: row["overrides"][feature] if row["overrides"][feature] is not None else defaults[feature] for feature in FEATURES}}
            return {"revision": self._document["revision"], "defaults": dict(defaults),
                    "bounds": {"min": 1, "max": 10}, "accounts": accounts, "persistence_error": False}

    def limits(self, account_id):
        with self._lock:
            self._healthy()
            key = self.resolve(account_id)
            row = self._document["accounts"].get(key, {"overrides": dict.fromkeys(FEATURES), "conflict": False})
            return {**deepcopy(row), "effective": {
                feature: row["overrides"][feature] if row["overrides"][feature] is not None else self._document["defaults"][feature]
                for feature in FEATURES}}

    def update(self, *, expected_revision, defaults=None, account_ids=None, overrides=None):
        with self._lock:
            self._healthy()
            if type(expected_revision) is not int or expected_revision != self._document["revision"]:
                raise ConcurrencyConflict("并发配置已更新，请刷新后重试。")
            if account_ids is not None and (not isinstance(account_ids, (list, tuple, set, frozenset)) or not 1 <= len(account_ids) <= 2000):
                raise ConcurrencyValidationError("请选择 1 至 2000 个账号。")
            if (account_ids is None) != (overrides is None):
                raise ConcurrencyValidationError("账号批量设置需要账号列表和覆盖配置。")
            document = deepcopy(self._document)
            if defaults is not None:
                document["defaults"] = {**document["defaults"], **_values(defaults)}
            ids = {self.resolve(key) for key in account_ids or ()}
            if overrides is not None:
                values = _values(overrides, True)
                if not values:
                    raise ConcurrencyValidationError("至少修改一种并发上限。")
                for key in ids:
                    row = document["accounts"].get(key, {"overrides": dict.fromkeys(FEATURES), "conflict": False})
                    document["accounts"][key] = {"overrides": {**row["overrides"], **values}, "conflict": False}
            if len(document["accounts"]) > 10000:
                raise ConcurrencyValidationError("账号配置数量超过限制。")
            if document != self._document:
                self._commit(document)
            return self.snapshot(ids)

    def bind_alias(self, account_id, alias):
        """Merge prior identity into the discovered canonical identity, if any."""
        with self._lock:
            self._healthy()
            target = self.resolve(account_id)
            if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
                raise ConcurrencyValidationError("凭据身份标识无效。")
            previous = self._document["aliases"].get(alias)
            previous = self.resolve(previous) if previous else None
            if previous == target:
                return ()
            document = deepcopy(self._document)
            document["aliases"][alias] = target
            migrated = ()
            policy_changed = False
            if previous is not None:
                old = document["accounts"].pop(previous, None)
                new = document["accounts"].get(target)
                if old is not None:
                    if new is None:
                        document["accounts"][target] = old
                    else:
                        defaults = document["defaults"]
                        old_limits = {f: old["overrides"][f] if old["overrides"][f] is not None else defaults[f] for f in FEATURES}
                        new_limits = {f: new["overrides"][f] if new["overrides"][f] is not None else defaults[f] for f in FEATURES}
                        document["accounts"][target] = {"overrides": {
                            f: new["overrides"][f] if old_limits[f] == new_limits[f] else min(old_limits[f], new_limits[f]) for f in FEATURES},
                            "conflict": old["conflict"] or new["conflict"] or old_limits != new_limits}
                    policy_changed = True
                document["redirects"][previous] = target
                migrated = ((previous, target),)
            if len(document["aliases"]) > 10000 or len(document["redirects"]) > 10000:
                raise ConcurrencyValidationError("账号身份映射数量超过限制。")
            self._commit(document, bump=policy_changed)
            return migrated


_store = None
_store_lock = threading.Lock()


def get_concurrency_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = ConcurrencyStore()
        return _store
