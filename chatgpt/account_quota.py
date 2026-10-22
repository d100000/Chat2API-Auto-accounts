"""Private, cached ChatGPT web quota snapshots; never sends a chat message.

JWT claims are unverified labels, not authentication evidence. This module does
not read Codex usage or infer account entitlements from a model name.
"""

import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import tempfile
import time
from urllib.parse import urlsplit


SOURCE = "chatgpt_web_conversation_init"
REFRESH_INTERVAL = 60
STALE_AFTER = 300
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.|:-]{0,127}\Z")
_PUBLIC_ID = re.compile(r"acct_[0-9a-f]{24}\Z")
_NUMBERS = {"remaining", "total", "limit", "used", "used_percent", "window_seconds",
            "limit_window_seconds", "reset_after_seconds", "max_requests"}
_NAMES = {"name", "feature_name", "model", "model_slug", "type", "limit_type", "block_reason"}
_DATES = {"reset_after", "reset_at", "reset", "resets_at"}
_BOOLS = {"blocked", "is_blocked", "allowed", "limit_reached"}
_NESTED = {"window", "windows", "primary_window", "secondary_window", "rate_limit", "limits"}
_PLANS = frozenset({"free", "plus", "pro", "team", "business", "enterprise", "edu", "go"})
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 10**15 else None


def _iso(value):
    if _number(value) is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _timestamp(value):
    if _number(value) is not None:
        return value if _iso(value) else None
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _name(value):
    # Permit identifiers, never arbitrary upstream text, URLs, or JWTs.
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) and not value.startswith(("eyJ", "sk-", "fk-")) else None


def _claims(token):
    try:
        parts = token.split(".")
        if len(parts) != 3 or len(parts[1]) > 32768:
            return {}
        value = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, UnicodeError):
        return {}


def _split_token(token, override=None):
    credential, _, embedded = str(token).strip().partition(",")
    return credential, _name(override if override is not None else embedded)


def _cached_access(token, refresh_map):
    cached = refresh_map.get(token, {}) if isinstance(refresh_map, dict) else {}
    return cached.get("token", token) if isinstance(cached, dict) and isinstance(cached.get("token", token), str) else token


def _identity(token, override=None, refresh_map=None, now=None):
    credential, account_override = _split_token(token, override)
    access = _cached_access(credential, refresh_map or {})
    claims = _claims(access)
    auth = claims.get("https://api.openai.com/auth", {})
    auth = auth if isinstance(auth, dict) else {}
    account = account_override or _name(auth.get("chatgpt_account_id")) or _name(claims.get("account_id"))
    user = _name(auth.get("chatgpt_user_id")) or _name(claims.get("sub"))
    if account and user:
        source, raw = "account_user_claim", account + "\0" + user
    else:
        source, raw = ("account_claim", account) if account else (("user_claim", user) if user else ("token_fingerprint", credential))
    opaque = hashlib.sha256((source + ":" + raw).encode()).hexdigest()[:24]
    expiry = _number(claims.get("exp"))
    clock = time.time() if now is None else now
    status = "unknown" if expiry is None else ("expired" if expiry <= clock else "valid")
    return {"id": "acct_" + opaque, "label": "账号 " + opaque[:8], "identity_source": source,
            "credential_status": status, "credential_expires_at": _iso(expiry)}


def account_identity(token: str, account_override=None):
    """Return a non-secret stable label, also used by the local usage ledger.

    Expiry comes only from unsigned JWT claims. Opaque/uncached refresh tokens
    have a token-derived identity until a cached access token is available.
    """
    credential, _ = _split_token(token, account_override)
    refresh_map = {}
    if len(credential) == 45:
        from utils import globals as state
        refresh_map = state.refresh_map
    return _identity(token, account_override, refresh_map)


def _email(value):
    """Only a bounded ordinary email address, never arbitrary claim content."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 3 <= len(value) <= 254 or value.count("@") != 1:
        return None
    local, domain = value.split("@")
    if (not 1 <= len(local) <= 64 or local.startswith(".") or local.endswith(".") or ".." in local
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)):
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                              for label in labels):
        return None
    return value


def _safe_fingerprint(value):
    return value if isinstance(value, str) and _FINGERPRINT.fullmatch(value) else None


def _credential_fingerprint(group):
    # Bind a successful check to the actual access credential and workspace.
    # Refresh-token cache timestamps and JWT claims are not proof of a check.
    return hashlib.sha256((group["access"] + "\0" + (group["upstream_account"] or "")).encode()).hexdigest()


def _profile(group, record, now):
    """Private admin presentation. Never merge this into identity/quota rows.

    JWT signatures are not verified here. Even a successful quota request does
    not turn the JWT's email, plan, iat or exp labels into verified assertions.
    """
    credential, override = _split_token(group["token"])
    claims = _claims(group["access"])
    namespace = claims.get("https://api.openai.com/profile")
    namespace = namespace if isinstance(namespace, dict) else {}
    auth = claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    email = _email(namespace.get("email"))
    email_source = "jwt_profile" if email is not None else None
    if email is None:
        email = _email(claims.get("email"))
        email_source = "jwt_top_level" if email is not None else None
    plan_value = auth.get("chatgpt_plan_type")
    plan_value = plan_value.strip().lower() if isinstance(plan_value, str) and len(plan_value) <= 64 else None
    claim_account = _name(auth.get("chatgpt_account_id")) or _name(claims.get("account_id"))
    workspace_mismatch = bool(override and override != claim_account)
    plan = plan_value if plan_value in _PLANS and not workspace_mismatch else "unknown"
    plan_source = "jwt_auth" if plan_value and not workspace_mismatch else None
    token_type = "refresh_token" if len(credential) == 45 else "access_token" if claims else "opaque"
    fingerprint = _credential_fingerprint(group)
    verified = _number(record.get("checked_at"))
    if record.get("verified_credential_fingerprint") != fingerprint or verified is None or verified > now:
        verified = None
    current_error = bool(record.get("error") and record.get("attempted_credential_fingerprint") == fingerprint)
    credential_status = group["identity"]["credential_status"]
    authorization = ("expired" if credential_status == "expired" else
                     "error" if credential_status == "error" or current_error else
                     "unverified" if verified is None else
                     "verification_stale" if now - verified >= STALE_AFTER else "verified_recently")
    return {"email": email, "plan": plan, "email_source": email_source, "plan_source": plan_source,
            "email_trust": "unverified_claim" if email_source else "unknown",
            "plan_trust": "unverified_claim" if plan_source else "unknown", "claims_verified": False,
            "plan_scope": "workspace_mismatch" if workspace_mismatch else "credential_account" if plan_source else "unknown",
            "token_type": token_type, "token_type_source": "local_format",
            "auth_method": "refresh_token" if token_type == "refresh_token" else "bearer_token" if token_type == "access_token" else "unknown",
            "issued_at": _iso(_number(claims.get("iat"))), "issued_at_source": "jwt_iat" if _iso(_number(claims.get("iat"))) else None,
            "expires_at": group["identity"]["credential_expires_at"],
            "expires_at_source": "jwt_exp" if group["identity"]["credential_expires_at"] else None,
            "expiry_subject": "access_token" if claims else None,
            "last_verified_at": _iso(verified), "verification_source": "quota_endpoint" if verified is not None else None,
            "authorization_status": authorization}


def _safe_fields(value, depth=0):
    if not isinstance(value, dict) or depth > 3:
        return {}
    safe = {}
    for key, item in value.items():
        if key in _NUMBERS:
            safe[key] = _number(item)
        elif key in _NAMES:
            safe[key] = _name(item)
        elif key in _DATES:
            stamp = _timestamp(item)
            safe[key] = _iso(stamp) if isinstance(item, str) else (stamp if stamp is not None else None)
        elif key in _BOOLS and isinstance(item, bool):
            safe[key] = item
        elif key in _NESTED:
            if isinstance(item, dict):
                safe[key] = _safe_fields(item, depth + 1)
            elif isinstance(item, list):
                safe[key] = [_safe_fields(row, depth + 1) for row in item[:100] if isinstance(row, dict)]
    return safe


def _payload(value):
    if not isinstance(value, dict) or not isinstance(value.get("limits_progress"), list):
        raise QuotaReadError("invalid_response")
    output = {}
    for key in ("limits_progress", "model_limits", "blocked_features"):
        rows = value.get(key, [])
        if not isinstance(rows, list):
            raise QuotaReadError("invalid_response")
        output[key] = [_safe_fields(row) for row in rows[:200] if isinstance(row, dict)]
    output["windows"] = {key: _safe_fields({key: value[key]})[key] for key in _NESTED
                         if key in value and isinstance(value[key], (dict, list))}
    return output


def _features(payload, now):
    blocks = {row.get("name") or row.get("feature_name"): row for row in payload.get("blocked_features", [])}
    rows = {row["feature_name"]: row for row in payload.get("limits_progress", []) if row.get("feature_name")}
    names = dict.fromkeys(["reason", "image_gen", *rows, *[name for name in blocks if name]])
    output = {}
    for name in names:
        row, block = rows.get(name, {}), blocks.get(name, {})
        stamp = next((_timestamp(row[key]) for key in ("reset_after", "reset_at", "reset", "resets_at")
                      if _timestamp(row.get(key)) is not None), None)
        remaining = _number(row.get("remaining"))
        total = next((value for value in (_number(row.get("total")), _number(row.get("limit")),
                                         _number(block.get("limit"))) if value is not None), None)
        output[name] = {"remaining": remaining, "total": total, "used": _number(row.get("used")),
                        "reset_at": _iso(stamp), "blocked": name in blocks,
                        "status": "expired" if stamp is not None and stamp <= now else
                                  ("known" if remaining is not None else "unknown")}
    return output


class QuotaReadError(Exception):
    """Only bounded, locally defined codes may cross the quota API boundary."""
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


_MESSAGES = {"invalid_response": "上游未返回可识别的额度数据。", "network_error": "额度请求连接失败。",
             "timeout": "额度请求超时。", "upstream_http": "上游拒绝额度请求。",
             "expired_token": "凭据声明已过期，请更新后再刷新。",
             "unsupported_token": "需要可用的 Access Token，未自动交换 Refresh Token。",
             "invalid_config": "额度请求的上游地址配置无效。", "cancelled": "额度刷新已取消，保留先前快照。"}


async def _fetch_quota(access_token, account_id, token_key):
    from curl_cffi.requests import AsyncSession
    from chatgpt.fp import get_fp
    from utils import configs

    host = random.choice(configs.chatgpt_base_url_list) if configs.chatgpt_base_url_list else "https://chatgpt.com"
    parsed = urlsplit(host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise QuotaReadError("invalid_config")
    fp = dict(get_fp(token_key))
    proxy = fp.get("proxy_url")
    if proxy:
        proxy = proxy.replace("{}", hashlib.md5(token_key.encode()).hexdigest())
    headers = {key: value for key, value in fp.items() if key in
               {"user-agent", "oai-device-id", "sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile"}}
    headers = {**headers, "Authorization": "Bearer " + access_token, "Content-Type": "application/json",
               "Accept": "application/json", "Origin": host.rstrip("/"), "Referer": host.rstrip("/") + "/"}
    if account_id:
        headers = {**headers, "ChatGPT-Account-Id": account_id}
    if configs.auth_key:
        headers = {**headers, "authkey": configs.auth_key}
    client = AsyncSession(proxies={"http": proxy, "https": proxy}, timeout=20,
                          impersonate=fp.get("impersonate", "chrome123"))
    try:
        response = await client.post(host.rstrip("/") + "/backend-api/conversation/init", headers=headers,
                                     json={"requested_default_model": None, "conversation_id": None,
                                           "timezone_offset_min": -int(datetime.now().astimezone().utcoffset().total_seconds() / 60),
                                           "conversation_origin": None}, allow_redirects=False)
        if response.status_code != 200:
            raise QuotaReadError("upstream_http", response.status_code)
        if len(response.content) > 1024 * 1024:
            raise QuotaReadError("invalid_response")
        try:
            return response.json()
        except (ValueError, TypeError):
            raise QuotaReadError("invalid_response") from None
    finally:
        await client.close()


class AccountQuotaService:
    """Inject sources/fetcher/clock for offline tests; default service is lazy."""
    def __init__(self, token_source, *, error_token_source=lambda: (), refresh_map_source=lambda: {},
                 cache_path="data/account_quota.json", fetcher=None, clock=time.time, concurrency=2):
        self._token_source = token_source
        self._error_token_source = error_token_source
        self._refresh_map_source = refresh_map_source
        self._cache_path = Path(cache_path) if cache_path is not None else None
        self._fetcher = fetcher or _fetch_quota
        self._clock = clock
        self._semaphore = asyncio.Semaphore(max(1, min(concurrency, 4)))
        self._inflight = frozenset()
        self._records = self._load()
        self._cache_error = False

    def _load(self):
        if self._cache_path is None:
            return {}
        try:
            if self._cache_path.stat().st_size > 8 * 1024 * 1024:
                return {}
            document = json.loads(self._cache_path.read_text())
            rows = document.get("accounts", {}) if document.get("version") == 1 else {}
            if not isinstance(rows, dict):
                return {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}
        safe = {}
        for key, record in rows.items():
            try:
                if not _PUBLIC_ID.fullmatch(key) or not isinstance(record, dict):
                    continue
                saved = record.get("payload")
                if saved is not None and not isinstance(saved, dict):
                    continue
                payload = _payload({**{name: value for name, value in saved.items() if name != "windows"},
                                    **saved.get("windows", {})}) if isinstance(saved, dict) else None
                safe[key] = {"payload": payload, "checked_at": _number(record.get("checked_at")),
                             "last_attempt_at": _number(record.get("last_attempt_at")),
                             "verified_credential_fingerprint": _safe_fingerprint(record.get("verified_credential_fingerprint")),
                             "attempted_credential_fingerprint": _safe_fingerprint(record.get("attempted_credential_fingerprint")),
                             "last_result": "refreshed" if payload is not None and not record.get("error") else "failed",
                             "error": self._safe_error(record.get("error"))}
            except (ValueError, TypeError, AttributeError, QuotaReadError):
                continue
        return safe

    @staticmethod
    def _safe_error(error):
        if not isinstance(error, dict) or error.get("code") not in _MESSAGES:
            return None
        status = error.get("http_status")
        return {"code": error["code"], "message": _MESSAGES[error["code"]],
                "http_status": status if type(status) is int and 100 <= status <= 599 else None}

    def _save(self):
        if self._cache_path is None:
            return
        temporary = None
        try:
            self._cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".account-quota-", dir=self._cache_path.parent)
            with os.fdopen(descriptor, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"version": 1, "accounts": self._records}, handle, ensure_ascii=False)
            os.replace(temporary, self._cache_path)
            self._cache_error = False
        except OSError:
            self._cache_error = True
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    self._cache_error = True

    def _groups(self):
        refresh_map = dict(self._refresh_map_source())
        errors = frozenset(self._error_token_source())
        groups = {}
        for token in dict.fromkeys(str(value).strip() for value in self._token_source() if str(value).strip()):
            credential, override = _split_token(token)
            access = _cached_access(credential, refresh_map)
            identity = _identity(token, refresh_map=refresh_map, now=self._clock())
            if token in errors or credential in errors:
                identity = {**identity, "credential_status": "error"}
            claims = _claims(access)
            auth = claims.get("https://api.openai.com/auth", {})
            auth = auth if isinstance(auth, dict) else {}
            account = override or _name(auth.get("chatgpt_account_id")) or _name(claims.get("account_id"))
            candidate = {"identity": identity, "token": token, "access": access, "upstream_account": account,
                         "supported": not (len(credential) == 45 and access == credential)}
            old = groups.get(identity["id"])
            score = {"valid": 3, "unknown": 2, "expired": 1, "error": 0}
            rank = lambda row: (score[row["identity"]["credential_status"]], row["identity"].get("credential_expires_at") or "")
            selected = candidate if old is None or rank(candidate) > rank(old) else old
            groups = {**groups, identity["id"]: {**selected, "token_count": (old or {}).get("token_count", 0) + 1}}
        return groups

    def get_accounts(self):
        now = self._clock()
        accounts = []
        for key, group in self._groups().items():
            record = self._records.get(key, {})
            payload = deepcopy(record.get("payload") or {"limits_progress": [], "model_limits": [], "blocked_features": [], "windows": {}})
            features = _features(payload, now)
            checked, attempted = record.get("checked_at"), record.get("last_attempt_at")
            retry = max(0, math.ceil(attempted + REFRESH_INTERVAL - now)) if attempted is not None else 0
            stale = checked is not None and (now - checked >= STALE_AFTER or record.get("error") is not None or
                                             any(row["status"] == "expired" for row in features.values()))
            status = "stale" if stale else ("fresh" if checked is not None else ("error" if record.get("error") else "unknown"))
            if group["identity"]["credential_status"] == "expired":
                status = "expired"
            accounts.append({**group["identity"], "token_count": group["token_count"], "status": status,
                             "checked_at": _iso(checked), "features": features, **payload,
                             "refresh": {"status": "in_progress" if key in self._inflight else record.get("last_result", "never"),
                                         "last_attempt_at": _iso(attempted),
                                         "next_refresh_at": _iso(attempted + REFRESH_INTERVAL) if attempted is not None else None,
                                         "retry_after_seconds": retry, "error": deepcopy(record.get("error"))}})
        return {"source": SOURCE, "generated_at": _iso(now), "refresh_interval_seconds": REFRESH_INTERVAL,
                "stale_after_seconds": STALE_AFTER, "cache_persistence_error": self._cache_error, "accounts": accounts}

    def get_account_profiles(self):
        """PII-bearing metadata for authenticated local administration only.

        Keep this separate from get_accounts(), scheduler snapshots, model
        discovery and usage identity. This method performs no network request.
        """
        now = self._clock()
        return {key: _profile(group, self._records.get(key, {}), now) for key, group in self._groups().items()}

    async def refresh(self, account_id=None):
        groups = self._groups()
        if account_id is not None and account_id not in groups:
            raise KeyError("Unknown account")
        selected = [groups[account_id]] if account_id is not None else list(groups.values())
        await asyncio.gather(*(self._refresh_one(group) for group in selected))
        return self.get_accounts()

    async def _refresh_one(self, group):
        key = group["identity"]["id"]
        old = self._records.get(key, {})
        now = self._clock()
        if key in self._inflight:
            return
        if old.get("last_attempt_at") is not None and now < old["last_attempt_at"] + REFRESH_INTERVAL:
            self._records = {**self._records, key: {**old, "last_result": "cooldown"}}
            return
        self._inflight = self._inflight | {key}
        try:
            async with self._semaphore:
                now = self._clock()
                fingerprint = _credential_fingerprint(group)
                self._records = {**self._records, key: {**old, "last_attempt_at": now, "last_result": "in_progress",
                                                      "attempted_credential_fingerprint": fingerprint}}
                self._save()
                try:
                    if group["identity"]["credential_status"] == "expired":
                        raise QuotaReadError("expired_token")
                    if not group["supported"]:
                        raise QuotaReadError("unsupported_token")
                    raw = await asyncio.wait_for(self._fetcher(group["access"], group["upstream_account"], group["token"]), timeout=25)
                    payload = _payload(raw)
                    record = {"payload": payload, "checked_at": self._clock(), "last_attempt_at": now,
                              "verified_credential_fingerprint": fingerprint, "attempted_credential_fingerprint": fingerprint,
                              "last_result": "refreshed", "error": None}
                except asyncio.CancelledError:
                    self._records = {**self._records, key: {**old, "last_attempt_at": now, "last_result": "failed",
                                     "attempted_credential_fingerprint": fingerprint,
                                     "error": self._safe_error({"code": "cancelled"})}}
                    self._save()
                    raise
                except Exception as error:
                    if isinstance(error, QuotaReadError) and error.code in _MESSAGES:
                        code, status = error.code, error.http_status
                    else:
                        timed_out = isinstance(error, (TimeoutError, asyncio.TimeoutError)) or getattr(error, "code", None) == 28
                        code, status = ("timeout" if timed_out else "network_error"), None
                    record = {**old, "last_attempt_at": now,
                              "attempted_credential_fingerprint": fingerprint,
                              "last_result": {"expired_token": "expired", "unsupported_token": "unsupported"}.get(code, "failed"),
                              "error": self._safe_error({"code": code, "http_status": status})}
                self._records = {**self._records, key: record}
                self._save()
        finally:
            self._inflight = self._inflight - {key}


_service = None


def _default_service():
    global _service
    if _service is None:
        from utils import globals as state
        _service = AccountQuotaService(lambda: tuple(state.token_list),
                                       error_token_source=lambda: tuple(state.error_token_list),
                                       refresh_map_source=lambda: dict(state.refresh_map))
    return _service


def get_accounts():
    return _default_service().get_accounts()


def get_account_profiles():
    return _default_service().get_account_profiles()


async def refresh(account_id=None):
    return await _default_service().refresh(account_id)
