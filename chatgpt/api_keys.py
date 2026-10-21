"""Managed client keys, separate from ChatGPT credentials and console sessions.

Only SHA-256 digests of 256-bit random secrets are persisted. Each operation
opens SQLite independently, so revocation is visible across gateway processes.
"""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import stat


MANAGED_PREFIX = "sk-c2a-"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "api-keys.sqlite3"
PUBLIC_COLUMNS = "id, name, prefix, last4, status, created_at, last_used_at, request_count"


class KeyStoreError(Exception):
    """A safe, non-secret error; never expose the underlying database exception."""


class KeyNotFound(KeyStoreError):
    pass


class KeyRevoked(KeyStoreError):
    pass


class ManagedKeyError(Exception):
    def __init__(self, status_code, code, message):
        super().__init__(message)
        self.status_code, self.code = status_code, code


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key_digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid_name(value):
    if not isinstance(value, str):
        raise ValueError("名称须为 1 至 80 个字符。")
    value = value.strip()
    if not 1 <= len(value) <= 80 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("名称须为 1 至 80 个字符，且不能包含控制字符。")
    return value


def system_key_info(keys):
    configured = tuple(key for key in keys if key)
    first = configured[0] if configured else None
    hint = (first[:4] + "…" + first[-4:]) if first and len(first) > 12 else ("••••" if first else None)
    return {"configured": bool(configured), "hint": hint, "count": len(configured), "read_only": True}


class APIKeyStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)

    def _exists(self):
        try:
            if self.path.is_symlink():
                raise KeyStoreError("API 密钥数据库不能是符号链接。")
            return self.path.exists()
        except OSError:
            raise KeyStoreError("API 密钥数据库不可用，请检查本地数据目录。") from None

    def _prepare(self):
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                file_stat = os.fstat(descriptor)
                if not stat.S_ISREG(file_stat.st_mode) or (hasattr(os, "getuid") and file_stat.st_uid != os.getuid()):
                    raise KeyStoreError("API 密钥数据库所有权无效。")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            with closing(self._connect(write=True)) as connection, connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
                    prefix TEXT NOT NULL, last4 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','disabled','revoked')),
                    created_at TEXT NOT NULL, last_used_at TEXT,
                    request_count INTEGER NOT NULL DEFAULT 0 CHECK(request_count >= 0))""")
        except (OSError, sqlite3.Error):
            raise KeyStoreError("API 密钥数据库不可用，请检查本地数据目录。") from None

    def _connect(self, write=False):
        if not self._exists():
            raise KeyStoreError("API 密钥数据库尚未创建。")
        try:
            # mode=rw/ro prevents a deleted database silently becoming a new one.
            connection = sqlite3.connect(self.path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                         uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except (OSError, sqlite3.Error):
            raise KeyStoreError("API 密钥数据库不可用，请检查本地数据目录。") from None

    def list_keys(self):
        if not self._exists():
            return []
        try:
            with closing(self._connect()) as connection:
                return [dict(row) for row in connection.execute(
                    f"SELECT {PUBLIC_COLUMNS} FROM api_keys ORDER BY created_at DESC, rowid DESC")]
        except sqlite3.Error:
            raise KeyStoreError("读取 API 密钥失败，请稍后重试。") from None

    def create(self, name):
        name = valid_name(name)
        self._prepare()
        plaintext = MANAGED_PREFIX + secrets.token_urlsafe(32)
        identifier = "key_" + secrets.token_hex(12)
        row = {"id": identifier, "name": name, "prefix": plaintext[:12], "last4": plaintext[-4:],
               "status": "active", "created_at": now_iso(), "last_used_at": None, "request_count": 0}
        try:
            with closing(self._connect(write=True)) as connection, connection:
                connection.execute("""INSERT INTO api_keys
                    (id,name,key_hash,prefix,last4,status,created_at,last_used_at,request_count)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (identifier, name, key_digest(plaintext), row["prefix"], row["last4"],
                                                   row["status"], row["created_at"], None, 0))
            return {"api_key": plaintext, "key": row}
        except sqlite3.Error:
            raise KeyStoreError("创建 API 密钥失败，请稍后重试。") from None

    def set_status(self, identifier, status):
        if status not in ("active", "disabled", "revoked"):
            raise ValueError("无效的 API 密钥状态。")
        if not self._exists():
            raise KeyNotFound("API 密钥不存在。")
        try:
            with closing(self._connect(write=True)) as connection, connection:
                # Serialize the read/change pair so a concurrent revoke cannot be
                # overwritten by a delayed enable/disable operation.
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(f"SELECT {PUBLIC_COLUMNS} FROM api_keys WHERE id=?", (identifier,)).fetchone()
                if row is None:
                    raise KeyNotFound("API 密钥不存在。")
                if row["status"] == "revoked" and status != "revoked":
                    raise KeyRevoked("已撤销的 API 密钥不能恢复，请创建新密钥。")
                connection.execute("UPDATE api_keys SET status=? WHERE id=?", (status, identifier))
                return {**dict(row), "status": status}
        except sqlite3.Error:
            raise KeyStoreError("更新 API 密钥失败，请稍后重试。") from None

    def authenticate(self, plaintext, *, record_use=False):
        if not isinstance(plaintext, str) or not plaintext.startswith(MANAGED_PREFIX) or len(plaintext) > 200:
            return None
        if not self._exists():
            return None
        digest = key_digest(plaintext)
        try:
            with closing(self._connect(write=record_use)) as connection, connection:
                if record_use:
                    # A single conditional write verifies active status and counts
                    # acceptance atomically with any concurrent revocation.
                    result = connection.execute("""UPDATE api_keys SET request_count=request_count+1, last_used_at=?
                        WHERE key_hash=? AND status='active'""", (now_iso(), digest))
                    if result.rowcount != 1:
                        return None
                row = connection.execute(f"SELECT {PUBLIC_COLUMNS} FROM api_keys WHERE key_hash=? AND status='active'",
                                         (digest,)).fetchone()
                return dict(row) if row is not None else None
        except sqlite3.Error:
            raise KeyStoreError("API 密钥校验暂不可用，请稍后重试。") from None


def resolve_managed_key(token, system_keys, *, store=None, record_use=False):
    """Return the pool key for managed tokens; ordinary legacy tokens pass through.

    Reserved-prefix invalid, disabled and revoked tokens always fail closed.
    Managed secrets never reach upstream authentication or account fingerprints.
    """
    if not isinstance(token, str) or not token.startswith(MANAGED_PREFIX):
        return token
    keys = tuple(key for key in system_keys if key)
    if not keys:
        raise ManagedKeyError(503, "api_key_unconfigured", "尚未配置系统共享密钥，托管密钥暂不可用。")
    # Preserve an explicitly configured legacy system key even if it happens to
    # use the managed prefix; these are displayed as read-only environment keys.
    if token.isascii() and any(hmac.compare_digest(token, key) for key in keys):
        return token
    try:
        row = (store or APIKeyStore()).authenticate(token, record_use=record_use)
    except KeyStoreError:
        raise ManagedKeyError(503, "api_key_store_unavailable", "API 密钥校验暂不可用，请稍后重试。") from None
    if row is None:
        raise ManagedKeyError(401, "invalid_api_key", "API 密钥无效、已停用或已撤销。")
    return keys[0]
