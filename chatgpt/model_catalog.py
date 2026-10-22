"""Small, cached public model catalogue without account or credential metadata.

Directory discovery and successful exact-model requests are separate evidence.
Public results aggregate account snapshots without exposing account identities. Importing this module
does not read account files, refresh credentials, or make network requests.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import importlib
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit


CACHE_TTL = 300
MAX_STALE = 24 * 60 * 60
RETRY_AFTER = 60
FETCH_TIMEOUT = 8
MAX_MODELS = 200
MAX_ACCOUNTS = 2000
REFRESH_CONCURRENCY = 2
REFRESH_SPACING = 0.25
CHANNEL = "chatgpt_web"
DEFAULT_CHAT_MODEL = "gpt-6-luna"
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_MODELS = (DEFAULT_CHAT_MODEL, "auto", DEFAULT_IMAGE_MODEL)
_MAX_CACHE_BYTES = 8 * 1024 * 1024
_ACCOUNT_ID = re.compile(r"acct_[0-9a-f]{24}\Z")
_CHECK_STATES = {"unverified", "verified", "denied", "error", "model_mismatch"}
_ERROR_CODES = {"upstream_http", "invalid_response", "invalid_models", "invalid_config", "timeout",
                "unavailable", "cancelled", "model_not_found", "model_not_supported", "permission_denied",
                "authentication_error", "rate_limit", "quota_exceeded", "model_mismatch", "incomplete_response",
                "credentials_unavailable"}
_CHAT_ID = re.compile(r"(?:gpt-[a-z0-9][a-z0-9._-]*|o[134](?:[-.][a-z0-9][a-z0-9._-]*)?)\Z")
_NON_CHAT = re.compile(r"(?:image|research|codex|review)", re.IGNORECASE)
_logger = logging.getLogger(__name__)


class CatalogReadError(Exception):
    """Only locally defined error codes; never include an upstream response."""


def is_chat_model_id(value):
    """Validate a native chat slug, without claiming it exists in the catalogue."""
    return (isinstance(value, str) and 0 < len(value) <= 100
            and _CHAT_ID.fullmatch(value) is not None and _NON_CHAT.search(value) is None)


def _slugs(values):
    if not isinstance(values, list):
        raise CatalogReadError("invalid_models")
    # A bounded set of identifiers is the only upstream content we retain.
    return tuple(dict.fromkeys(value for value in values[:1000] if is_chat_model_id(value)))[:MAX_MODELS]


def _parse_response(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise CatalogReadError("invalid_response")
    return _slugs([model.get("slug") for model in payload["models"][:1000] if isinstance(model, dict)])


def _model(identifier):
    native_image = identifier == "gpt-image-2"
    image = native_image or identifier == "chatgpt-image-latest"
    source = "codex_images" if native_image else "chatgpt_web"
    description = ("原生 Image2 接口默认未启用；需独立 Codex 授权，未声明已验证。" if native_image else
                   "ChatGPT 网页生图桥接；不保证底层原生 Image2 模型身份。" if image else
                   "由 ChatGPT 网页自动选择当前账号可用的聊天模型。" if identifier == "auto" else
                   "ChatGPT 网页返回的聊天模型标识；具体可用性取决于账号。")
    return {"id": identifier, "object": "model", "created": 0, "owned_by": source,
            "type": "image" if image else "chat", "source": source,
            "supported_endpoints": ["/v1/images/generations" if image else "/v1/chat/completions"],
            "availability": "requires_codex_authorization" if native_image else "account_dependent", "description": description}


def _access_details(token_key, refresh_map):
    from chatgpt.account_quota import _cached_access, _claims, _name, _split_token

    credential, override = _split_token(token_key)
    access = _cached_access(credential, refresh_map)
    if not isinstance(access, str) or not access or len(access) > 32768 or any(c.isspace() for c in access):
        raise CatalogReadError("unsupported_token")
    # A directory lookup must not trigger a refresh-token exchange.
    if len(credential) == 45 and access == credential:
        raise CatalogReadError("uncached_refresh_token")
    claims = _claims(access)
    auth = claims.get("https://api.openai.com/auth", {})
    auth = auth if isinstance(auth, dict) else {}
    account_id = override or _name(auth.get("chatgpt_account_id")) or _name(claims.get("account_id"))
    return access, account_id, claims.get("exp")


def _default_token_source():
    state = importlib.import_module("utils.globals")
    errors = {value for value in state.error_token_list if isinstance(value, str)}
    errored_credentials = {value.partition(",")[0].strip() for value in errors}
    now = time.time()
    available = []
    for token in tuple(state.token_list):
        if not isinstance(token, str) or not token or token in errors or token.partition(",")[0].strip() in errored_credentials:
            continue
        try:
            _, _, expiry = _access_details(token, state.refresh_map)
        except CatalogReadError:
            continue
        if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and expiry <= now:
            continue
        available.append(token)
    return tuple(dict.fromkeys(available))


def _default_all_token_source():
    state = importlib.import_module("utils.globals")
    return tuple(value for value in state.token_list if isinstance(value, str) and value)


async def _fetch_models(token_key):
    """Read one configured host and fixed path; redirects are never followed."""
    from curl_cffi.requests import AsyncSession
    from chatgpt.fp import get_fp

    configs = importlib.import_module("utils.configs")
    state = importlib.import_module("utils.globals")
    access, account_id, _ = _access_details(token_key, state.refresh_map)
    host = (configs.chatgpt_base_url_list or ["https://chatgpt.com"])[0]
    parsed = urlsplit(host)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise CatalogReadError("invalid_config")
    host = host.rstrip("/")
    fp = dict(get_fp(token_key))
    proxy = fp.get("proxy_url")
    if proxy:
        proxy = proxy.replace("{}", hashlib.md5(token_key.encode()).hexdigest())
    headers = {key: value for key, value in fp.items() if key in
               {"user-agent", "oai-device-id", "sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile"}}
    headers = {**headers, "Authorization": "Bearer " + access, "Accept": "application/json",
               "Origin": host, "Referer": host + "/"}
    if account_id:
        headers = {**headers, "ChatGPT-Account-Id": account_id}
    if configs.auth_key:
        headers = {**headers, "authkey": configs.auth_key}
    client = AsyncSession(proxies={"http": proxy, "https": proxy}, timeout=FETCH_TIMEOUT,
                          impersonate=fp.get("impersonate", "chrome123"))
    try:
        response = await client.get(host + "/backend-api/models", headers=headers, allow_redirects=False)
        if response.status_code != 200:
            raise CatalogReadError("upstream_http")
        if len(response.content) > 1024 * 1024:
            raise CatalogReadError("invalid_response")
        try:
            return response.json()
        except (TypeError, ValueError):
            raise CatalogReadError("invalid_response") from None
    finally:
        await client.close()


def _timestamp(value, now):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= now else None)


def _iso(value):
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _account_id(token):
    from chatgpt.account_quota import account_identity
    return account_identity(token)["id"]


def _compatible(model):
    if model is not None and not isinstance(model, str):
        return False
    if model in {None, "auto", "chatgpt-image-latest"}:
        return True
    if not isinstance(model, str):
        return False
    if model.startswith(("g-", "gizmo")):
        return True
    from api.models import model_proxy
    legacy = {"gpt-4", "gpt-4-mobile", "gpt-4o-canmore", "o3", "o1", "o1-pro",
              "o3-mini-high", "o3-mini-medium", "o3-mini-low"}
    legacy.update(key for key in model_proxy if key.startswith(("gpt-", "o1", "o3")))
    legacy.update(value for key, value in model_proxy.items() if key.startswith(("gpt-", "o1", "o3")))
    return model in legacy


def _empty_record(fingerprint):
    return {"fingerprint": fingerprint, "models": (), "checked_at": None, "last_success_at": None,
            "error_code": None, "checks": {}}


def _safe_check(model, value, now):
    if not is_chat_model_id(model) or not isinstance(value, dict) or value.get("status") not in _CHECK_STATES:
        return None
    stamp = _timestamp(value.get("checked_at"), now)
    if stamp is None:
        return None
    sent = value.get("sent_model") if is_chat_model_id(value.get("sent_model")) else None
    reported = value.get("reported_model") if is_chat_model_id(value.get("reported_model")) else None
    status = value["status"]
    if status == "verified" and (sent != model or reported != model):
        status = "model_mismatch"
    http = value.get("http_status")
    return {"status": status, "checked_at": stamp, "requested_model": model, "sent_model": sent,
            "reported_model": reported, "http_status": http if type(http) is int and 100 <= http <= 599 else None,
            "error_code": value.get("error_code") if value.get("error_code") in _ERROR_CODES else None}


class ModelCatalog:
    """Per-account web discovery with a bounded, single-flight refresh worker.

    Reads return cached data immediately. ``refresh_catalog`` explicitly waits
    for discovery (never generation); background refresh uses the same worker.
    Inject identity_resolver for tests. Neither importing nor constructing this
    class reads credentials or contacts an upstream server.
    """

    def __init__(self, token_source=None, *, fetcher=None, clock=time.time,
                 cache_path=Path("data/model_catalog.json"), identity_resolver=None,
                 refresh_spacing=REFRESH_SPACING, account_token_source=None):
        self._tokens = token_source or _default_token_source
        self._account_tokens = account_token_source or token_source or _default_all_token_source
        self._fetch = fetcher or _fetch_models
        self._identity = identity_resolver or _account_id
        self._clock = clock
        self._cache_path = Path(cache_path) if cache_path is not None else None
        self._loaded = False
        self._disk_signature = None
        self._records = {}
        self._legacy_models = ()
        self._legacy_at = None
        self._last_refresh_at = None
        self._retry_at = 0
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._next_start = 0
        self._spacing = max(0, float(refresh_spacing))
        self._task = None
        self._persistence_error = False

    def _groups(self):
        groups = {}
        available = set(self._tokens())
        for token in tuple(self._account_tokens())[:10000]:
            if not isinstance(token, str) or not token:
                continue
            key = self._identity(token)
            if not isinstance(key, str) or not _ACCOUNT_ID.fullmatch(key):
                continue
            old = groups.get(key, {"tokens": ()})
            if key not in groups and len(groups) >= MAX_ACCOUNTS:
                continue
            groups[key] = {"tokens": tuple(dict.fromkeys((*old["tokens"], token)))}
        return {key: {**row, "available_tokens": tuple(token for token in row["tokens"] if token in available),
                      "fingerprint": hashlib.sha256("\0".join(sorted(
            hashlib.sha256(token.encode()).hexdigest() for token in row["tokens"])).encode()).hexdigest()}
                for key, row in groups.items()}

    def _load(self, now):
        if self._cache_path is None:
            self._loaded = True
            return
        try:
            stat = self._cache_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if self._loaded and signature == self._disk_signature:
                return
            self._loaded, self._disk_signature = True, signature
            if stat.st_size > _MAX_CACHE_BYTES:
                raise ValueError("oversize")
            with self._cache_path.open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            if not isinstance(saved, dict):
                raise ValueError("invalid cache")
            if saved.get("version") == 1:
                # Old global entries have no account provenance. Retain only as
                # discovery hints; they never grant an account eligibility.
                stamp = _timestamp(saved.get("refreshed_at"), now)
                if stamp is None:
                    raise ValueError("invalid cache")
                self._legacy_models, self._legacy_at = _slugs(saved.get("models")), stamp
                return
            if saved.get("version") != 2 or not isinstance(saved.get("accounts"), dict):
                raise ValueError("invalid cache")
            records = {}
            for key, channels in list(saved["accounts"].items())[:MAX_ACCOUNTS]:
                if not _ACCOUNT_ID.fullmatch(key) or not isinstance(channels, dict):
                    continue
                row = channels.get(CHANNEL)
                if not isinstance(row, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("fingerprint", ""))):
                    continue
                checks = row.get("checks", {})
                records[key] = {"fingerprint": row["fingerprint"], "models": _slugs(row.get("models", [])),
                                "checked_at": _timestamp(row.get("checked_at"), now),
                                "last_success_at": _timestamp(row.get("last_success_at"), now),
                                "error_code": row.get("error_code") if row.get("error_code") in _ERROR_CODES else None,
                                "checks": {model: safe for model, check in list(checks.items())[:MAX_MODELS]
                                           if (safe := _safe_check(model, check, now))} if isinstance(checks, dict) else {}}
            self._records = records
            self._last_refresh_at = _timestamp(saved.get("last_refresh_at"), now)
            legacy = saved.get("legacy", {})
            if isinstance(legacy, dict):
                self._legacy_models, self._legacy_at = _slugs(legacy.get("models", [])), _timestamp(legacy.get("checked_at"), now)
        except FileNotFoundError:
            self._loaded = True
        except (OSError, ValueError, TypeError, CatalogReadError):
            _logger.warning("Model catalogue cache could not be read; retaining safe memory snapshot.")

    def _persist(self):
        if self._cache_path is None:
            return
        temporary = None
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".model_catalog-", dir=self._cache_path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"version": 2, "last_refresh_at": self._last_refresh_at,
                           "accounts": {key: {CHANNEL: row} for key, row in self._records.items()},
                           "legacy": {"models": self._legacy_models, "checked_at": self._legacy_at}}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._cache_path)
            stat = self._cache_path.stat()
            self._disk_signature = (stat.st_mtime_ns, stat.st_size)
            self._persistence_error = False
        except OSError:
            self._persistence_error = True
            _logger.warning("Model catalogue cache could not be persisted; retaining memory snapshot.")
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    _logger.warning("Model catalogue temporary cache could not be removed.")

    def _record(self, key, groups):
        group = groups.get(key)
        row = self._records.get(key)
        if group is None or row is None or row["fingerprint"] != group["fingerprint"]:
            return _empty_record(group["fingerprint"] if group else "")
        return row

    def _usable(self, stamp, now):
        return stamp is not None and 0 <= now - stamp <= MAX_STALE

    def _support(self, key, model, channel, now, groups):
        row = self._record(key, groups)
        listed = model in row["models"] and self._usable(row["last_success_at"], now)
        check = row["checks"].get(model, {})
        current_check = self._usable(check.get("checked_at"), now)
        if channel != CHANNEL:
            status, eligible = "unverified", False
        elif key not in groups or not groups[key]["available_tokens"]:
            status, eligible = "unverified", False
        elif _compatible(model):
            status, eligible = "compatible", True
        elif current_check and check.get("status") in {"denied", "model_mismatch"}:
            status, eligible = check["status"], False
        elif current_check and check.get("status") == "verified":
            status, eligible = "verified", True
        elif listed:
            status, eligible = "directory_visible", True
        else:
            status, eligible = "unverified", False
        return {"account_id": key, "channel": channel, "model": model, "status": status,
                "eligible": eligible, "directory_listed": listed,
                "checked_at": _iso(row["last_success_at"]), "verification_checked_at": _iso(check.get("checked_at"))}

    def get_model_support(self, account_id, model, channel=CHANNEL):
        now = self._clock()
        self._load(now)
        return self._support(account_id, model, channel, now, self._groups())

    def account_supports_model(self, account_id, model, channel=CHANNEL):
        return self.get_model_support(account_id, model, channel)["eligible"]

    def get_account_catalogs(self):
        now = self._clock()
        self._load(now)
        accounts = []
        for key, group in self._groups().items():
            row = self._records.get(key, _empty_record(group["fingerprint"]))
            if row["fingerprint"] != group["fingerprint"]:
                row = _empty_record(group["fingerprint"])
            success = row["last_success_at"]
            status = ("error" if row["error_code"] else "unqueried") if success is None else (
                "expired" if not self._usable(success, now) else "stale" if now - success >= CACHE_TTL or row["error_code"] else "fresh")
            if not group["available_tokens"]:
                status = "error"
            checks = {model: {**check, "checked_at": _iso(check["checked_at"]),
                              "stale": not self._usable(check["checked_at"], now)}
                      for model, check in row["checks"].items()}
            checks.setdefault("gpt-6-luna", {"status": "unverified", "checked_at": None,
                "requested_model": "gpt-6-luna", "sent_model": None, "reported_model": None,
                "http_status": None, "error_code": None, "stale": False})
            accounts.append({"account_id": key, "channel": CHANNEL, "status": status,
                "checked_at": _iso(row["checked_at"]), "last_success_at": _iso(success),
                "models": list(row["models"]), "checks": checks,
                "discovery_available": bool(group["available_tokens"]),
                "error_code": row["error_code"] if group["available_tokens"] else "credentials_unavailable"})
        return {"accounts": accounts, "refreshing": self._lock.locked() or bool(self._task and not self._task.done()),
                "last_refresh_at": _iso(self._last_refresh_at), "cache_persistence_error": self._persistence_error}

    def _result(self, now, groups):
        discovered = {}
        for key in groups:
            row = self._record(key, groups)
            candidates = [*row["models"], *row["checks"]]
            for model in dict.fromkeys(candidates):
                support = self._support(key, model, CHANNEL, now, groups)
                if support["eligible"]:
                    old = discovered.get(model, {"directory_accounts": 0, "verified_accounts": 0})
                    discovered[model] = {"directory_accounts": old["directory_accounts"] + int(support["directory_listed"]),
                                         "verified_accounts": old["verified_accounts"] + int(support["status"] == "verified")}
        # Migration keeps prior discovery visible but never implies account access.
        if self._usable(self._legacy_at, now):
            for model in self._legacy_models:
                discovered.setdefault(model, {"directory_accounts": 0, "verified_accounts": 0})
        data = [_model("auto")]
        for model, counts in discovered.items():
            if model == "auto":
                continue
            evidence = "verified" if counts["verified_accounts"] else "directory_visible" if counts["directory_accounts"] else "unverified"
            data.append({**_model(model), "verification": evidence, "meta": counts})
        data.extend((_model("chatgpt-image-latest"), _model("gpt-image-2")))
        return {"object": "list", "data": data, "meta": {
            "source": CHANNEL, "last_refresh_at": _iso(self._last_refresh_at),
            "refreshing": self._lock.locked() or bool(self._task and not self._task.done())}}

    def _due(self, row, now, force=False):
        attempted = row["checked_at"]
        if attempted is not None and now - attempted < RETRY_AFTER:
            return False
        return force or row["error_code"] is not None or row["last_success_at"] is None or now - row["last_success_at"] >= CACHE_TTL

    def _schedule(self, now, groups):
        if self._task and not self._task.done():
            return
        if now < self._retry_at or not any(group["available_tokens"] and self._due(self._record(key, groups), now)
                                            for key, group in groups.items()):
            return
        self._retry_at = now + RETRY_AFTER
        self._task = asyncio.create_task(self.refresh_catalog())
        self._task.add_done_callback(self._completed)

    @staticmethod
    def _completed(task):
        if not task.cancelled() and task.exception() is not None:
            _logger.warning("Background model discovery failed; retaining cached entries.")

    async def get_catalog(self):
        now = self._clock()
        self._load(now)
        groups = self._groups()
        self._schedule(now, groups)
        return self._result(now, groups)

    async def get_default_catalog(self):
        """Curated client defaults, separate from discovered routing evidence."""
        catalog = await self.get_catalog()
        discovered = {row["id"]: row for row in catalog["data"]}
        data = []
        for identifier in DEFAULT_MODELS:
            row = discovered.get(identifier, _model(identifier))
            if identifier == DEFAULT_CHAT_MODEL and not any(
                    row.get("meta", {}).get(key, 0) for key in ("directory_accounts", "verified_accounts")):
                row = {**row, "availability": "unavailable", "verification": "unverified",
                       "description": "默认聊天模型；当前账号池尚无有效可用证据，请刷新模型目录或核查账号权限。"}
            data.append(row)
        return {**catalog, "data": data}

    async def _refresh_one(self, key, group, semaphore):
        async with semaphore:
            async with self._start_lock:
                loop = asyncio.get_running_loop()
                wait = self._next_start - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._next_start = loop.time() + self._spacing
            row = self._records.get(key, _empty_record(group["fingerprint"]))
            if row["fingerprint"] != group["fingerprint"]:
                row = _empty_record(group["fingerprint"])
            self._records = {**self._records, key: {**row, "checked_at": self._clock()}}
            try:
                payload = await asyncio.wait_for(self._fetch(group["available_tokens"][0]), timeout=FETCH_TIMEOUT)
                models = _parse_response(payload)
                # Preserve a concurrent explicit verification result.
                row = self._records[key]
                self._records = {**self._records, key: {**row, "models": models,
                    "last_success_at": self._clock(), "error_code": None}}
            except asyncio.CancelledError:
                self._records = {**self._records, key: {**self._records[key], "error_code": "cancelled"}}
                raise
            except Exception as error:
                code = (str(error) if isinstance(error, CatalogReadError) and str(error) in _ERROR_CODES else
                        "timeout" if isinstance(error, (asyncio.TimeoutError, TimeoutError)) else "unavailable")
                self._records = {**self._records, key: {**self._records[key], "error_code": code}}

    async def refresh_catalog(self, account_id=None, force=False):
        if account_id is not None and (not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id)):
            raise ValueError("Invalid account identifier")
        async with self._lock:
            now = self._clock()
            self._load(now)
            groups = self._groups()
            if account_id is not None and account_id not in groups:
                raise ValueError("Account is unavailable for model discovery")
            selected = [(key, group) for key, group in groups.items()
                        if (account_id is None or account_id == key) and group["available_tokens"]
                        and self._due(self._record(key, groups), now, force)]
            if selected:
                semaphore = asyncio.Semaphore(REFRESH_CONCURRENCY)
                tasks = [asyncio.create_task(self._refresh_one(key, group, semaphore)) for key, group in selected]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    self._last_refresh_at = self._clock()
                    self._records = {key: row for key, row in self._records.items() if key in groups}
                    self._persist()
        return self.get_account_catalogs()

    def record_model_check(self, account_id, model, status, *, sent_model=None, reported_model=None,
                           http_status=None, error_code=None):
        """Record an explicit caller-performed terminal request; never generate.

        The caller must inspect the real upstream model claims, not an echoed
        OpenAI envelope. A successful status alone cannot verify a model.
        """
        if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id) or not is_chat_model_id(model):
            raise ValueError("Invalid account or model identifier")
        if status not in _CHECK_STATES:
            raise ValueError("Invalid model verification status")
        now = self._clock()
        self._load(now)
        groups = self._groups()
        if account_id not in groups:
            raise ValueError("Account is unavailable for model verification")
        if status == "verified" and (sent_model != model or reported_model != model):
            status = "model_mismatch"
            error_code = "model_mismatch"
        value = _safe_check(model, {"status": status, "checked_at": now, "sent_model": sent_model,
            "reported_model": reported_model, "http_status": http_status, "error_code": error_code}, now)
        row = self._record(account_id, groups)
        checks = {**row["checks"], model: value}
        if len(checks) > MAX_MODELS:
            checks = dict(list(checks.items())[-MAX_MODELS:])
        self._records = {**self._records, account_id: {**row, "checks": checks}}
        self._persist()
        return {**value, "checked_at": _iso(value["checked_at"])}

    async def find_model(self, identifier):
        if not isinstance(identifier, str):
            return None
        if identifier not in {"auto", "gpt-image-2", "chatgpt-image-latest"} and not is_chat_model_id(identifier):
            return None
        return next((model for model in (await self.get_catalog())["data"] if model["id"] == identifier), None)


_catalog = ModelCatalog()


async def get_catalog():
    return await _catalog.get_catalog()


async def get_default_catalog():
    return await _catalog.get_default_catalog()


async def find_model(identifier):
    return await _catalog.find_model(identifier)


async def refresh_catalog(account_id=None, force=False):
    return await _catalog.refresh_catalog(account_id, force)


def get_account_catalogs():
    return _catalog.get_account_catalogs()


def get_model_support(account_id, model, channel=CHANNEL):
    return _catalog.get_model_support(account_id, model, channel)


def account_supports_model(account_id, model, channel=CHANNEL):
    return _catalog.account_supports_model(account_id, model, channel)


def record_model_check(account_id, model, status, **kwargs):
    return _catalog.record_model_check(account_id, model, status, **kwargs)
