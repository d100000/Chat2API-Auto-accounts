"""Recoverable credential import plus account policy publication.

Only the existing token file and a short-lived private staging file contain
credentials. The recovery journal contains hashes, opaque IDs and policy only.
The admission lock prevents accounts becoming eligible before their policy.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from chatgpt.account_quota import _identity


class ImportValidationError(ValueError):
    pass


class ImportPersistenceError(OSError):
    pass


def parse_limit(value):
    if value is None or value == "inherit":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(?:[1-9]|10)", value):
        raise ImportValidationError("并发需选择继承默认，或填写 1–10 的整数。")
    return int(value)


def parse_credentials(text):
    if not isinstance(text, str) or len(text.encode()) > 2 * 1024 * 1024:
        raise ImportValidationError("导入内容不能超过 2 MiB。")
    rows = [row.strip() for row in text.splitlines() if row.strip() and not row.strip().startswith("#")]
    if not rows or len(rows) > 1000:
        raise ImportValidationError("每次请输入 1–1000 条凭据。")
    for row in rows:
        credential, separator, account = row.partition(",")
        if (not credential or len(row) > 32768 or any(c.isspace() for c in credential)
                or any(ord(c) < 32 for c in row)
                or (separator and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", account))):
            raise ImportValidationError("凭据格式无效；每行仅填写一条凭据及可选的工作区标识。")
    return rows


def _write_private(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".account-import-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _paths(state):
    tokens = Path(state.TOKENS_FILE)
    tokens.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return tokens, tokens.parent / ".account-import.json"


def _recover_locked(state, scheduler):
    tokens, journal = _paths(state)
    if not journal.exists():
        return
    try:
        if journal.stat().st_size > 256 * 1024:
            raise ValueError
        operation = json.loads(journal.read_text())
        name = operation.get("staging", "")
        if (operation.get("version") != 1 or not re.fullmatch(r"\.account-import-credentials-[A-Za-z0-9_-]+", name)
                or not re.fullmatch(r"[0-9a-f]{64}", operation.get("digest", ""))):
            raise ValueError
        staging = tokens.parent / name
        content = staging.read_bytes() if staging.exists() else tokens.read_bytes()
        if hashlib.sha256(content).hexdigest() != operation["digest"]:
            raise ValueError
        selected = sorted({scheduler.concurrency.resolve(key) for key in operation["account_ids"]})
        overrides = operation["overrides"]
        snapshot = scheduler.concurrency.snapshot(selected)
        revision = operation["revision"]
        if selected:
            if snapshot["revision"] == revision:
                scheduler.concurrency.update(expected_revision=revision, account_ids=selected, overrides=overrides)
            elif snapshot["revision"] < revision or any(
                snapshot["accounts"].get(account_id, {}).get("overrides", {}).get(key) != value
                for account_id in selected for key, value in overrides.items()
            ):
                raise ValueError
        if staging.exists():
            os.replace(staging, tokens)
            _sync_dir(tokens.parent)
        os.chmod(tokens, 0o600)
        state.token_list = [row.strip() for row in content.decode().splitlines()
                            if row.strip() and not row.strip().startswith("#")]
        journal.unlink()
        _sync_dir(tokens.parent)
    except Exception:
        raise ImportPersistenceError("上次导入尚未完成，暂未发布新账号；请检查本机存储后重试。") from None


async def recover_import(*, state=None, scheduler=None):
    if state is None:
        from utils import globals as state
    if scheduler is None:
        from chatgpt.account_scheduler import get_scheduler
        scheduler = get_scheduler()
    async with scheduler.admission_guard():
        _recover_locked(state, scheduler)


async def import_accounts(text, *, reason_limit="inherit", image_gen_limit="inherit",
                          overwrite_existing=False, state=None, scheduler=None):
    rows = parse_credentials(text)
    overrides = {"reason": parse_limit(reason_limit), "image_gen": parse_limit(image_gen_limit)}
    if type(overwrite_existing) is not bool:
        raise ImportValidationError("覆盖已有配置选项无效。")
    if state is None:
        from utils import globals as state
    if scheduler is None:
        from chatgpt.account_scheduler import get_scheduler
        scheduler = get_scheduler()
    async with scheduler.admission_guard():
        _recover_locked(state, scheduler)
        try:
            scheduler.synchronize_identities_locked()
            before = list(dict.fromkeys(state.token_list))
            identity = lambda token: _identity(token, refresh_map=getattr(state, "refresh_map", {}))
            existing_ids = {scheduler.concurrency.resolve(identity(token)["id"]) for token in before}
            unique = list(dict.fromkeys(rows))
            after = list(dict.fromkeys([*before, *unique]))
            identities = {}
            for token in unique:
                row = identity(token)
                canonical = scheduler.bind_identity(token, row["id"])
                identities = {**identities, canonical: {**row, "id": canonical}}
            existing_ids = {scheduler.concurrency.resolve(key) for key in existing_ids}
            ids = set(identities)
            selected = sorted(ids if overwrite_existing else ids - existing_ids)
            policy = scheduler.concurrency.snapshot(selected)
            tokens, journal = _paths(state)
            content = ("\n".join(after) + "\n").encode()
            descriptor, temporary = tempfile.mkstemp(prefix=".account-import-credentials-", dir=tokens.parent)
        except Exception:
            raise ImportPersistenceError("账号身份或并发配置暂时无法保存，尚未发布新凭据。") from None
        prepared = False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            operation = {"version": 1, "staging": Path(temporary).name,
                "digest": hashlib.sha256(content).hexdigest(), "revision": policy["revision"],
                "account_ids": selected, "overrides": overrides}
            _write_private(journal, json.dumps(operation).encode())
            prepared = True
            _recover_locked(state, scheduler)
        except Exception:
            # After the journal is durable the operation is recoverable; never
            # publish an unconfigured account or erase its staged credentials.
            raise ImportPersistenceError("导入尚未完成，输入已保留；请刷新状态确认后重试。") from None
        finally:
            # Journal replacement can succeed even when its directory fsync
            # fails. In that case recovery still needs the staged credentials.
            if not prepared and not journal.exists() and os.path.exists(temporary):
                os.unlink(temporary)
        return {"status": "success", "tokens_count": len(set(after) - set(state.error_token_list)),
            "added_credentials": len(after) - len(before), "duplicate_credentials": len(rows) - (len(after) - len(before)),
            "new_accounts": len(ids - existing_ids), "existing_accounts": len(ids & existing_ids),
            "settings_applied": len(selected), "pending_identity": sum(row["identity_source"] == "token_fingerprint" for row in identities.values()),
            "account_ids": sorted(ids)}


async def clear_accounts(*, state=None, scheduler=None):
    if state is None:
        from utils import globals as state
    if scheduler is None:
        from chatgpt.account_scheduler import get_scheduler
        scheduler = get_scheduler()
    async with scheduler.admission_guard():
        _recover_locked(state, scheduler)
        tokens, _ = _paths(state)
        try:
            _write_private(tokens, b"")
        except OSError:
            raise ImportPersistenceError("账号池暂时无法保存，原有凭据保持不变。") from None
        state.token_list = []
        state.error_token_list = []
    return {"status": "success", "tokens_count": 0}
