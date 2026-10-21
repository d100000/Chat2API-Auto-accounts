"""Small, cached public model catalogue without account or credential metadata.

The ChatGPT web model list is a routing hint for one account, not proof of an
underlying model or entitlement across the entire pool. Importing this module
does not read account files, refresh credentials, or make network requests.
"""

import asyncio
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
_MAX_CACHE_BYTES = 64 * 1024
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


class ModelCatalog:
    """Inject dependencies to test the full cache lifecycle without networking.

    ``token_source`` returns an iterable of usable credential strings. The
    default removes marked-error/expired tokens and uncached refresh tokens.
    ``fetcher(token)`` is async and returns the web ``{"models": [...]}`` body.
    Only one token and one request are used per refresh.
    """

    def __init__(self, token_source=None, *, fetcher=None, clock=time.time,
                 cache_path=Path("data/model_catalog.json")):
        self._tokens = token_source or _default_token_source
        self._fetch = fetcher or _fetch_models
        self._clock = clock
        self._cache_path = Path(cache_path) if cache_path is not None else None
        self._loaded = False
        self._models = ()
        self._refreshed_at = None
        self._retry_at = 0
        self._lock = asyncio.Lock()

    def _load(self, now):
        if self._loaded:
            return
        self._loaded = True
        if self._cache_path is None:
            return
        try:
            if self._cache_path.stat().st_size > _MAX_CACHE_BYTES:
                raise ValueError("oversize")
            with self._cache_path.open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            stamp = saved.get("refreshed_at") if isinstance(saved, dict) else None
            if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not math.isfinite(stamp)
                    or stamp < 0 or stamp > now or saved.get("version") != 1):
                raise ValueError("invalid cache")
            models = _slugs(saved.get("models"))
            self._models, self._refreshed_at = models, stamp
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, CatalogReadError):
            _logger.warning("Model catalogue cache could not be read; using defaults.")

    def _persist(self):
        if self._cache_path is None:
            return
        temporary = None
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".model_catalog-", dir=self._cache_path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"version": 1, "refreshed_at": self._refreshed_at, "models": self._models}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._cache_path)
        except OSError:
            _logger.warning("Model catalogue cache could not be persisted; retaining memory snapshot.")
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    _logger.warning("Model catalogue temporary cache could not be removed.")

    def _result(self, now):
        models = self._models if self._refreshed_at is not None and 0 <= now - self._refreshed_at <= MAX_STALE else ()
        return {"object": "list", "data": [_model(identifier) for identifier in
                ("auto", *models, "chatgpt-image-latest", "gpt-image-2")]}

    def _fresh(self, now):
        return self._refreshed_at is not None and 0 <= now - self._refreshed_at < CACHE_TTL

    async def get_catalog(self):
        now = self._clock()
        self._load(now)
        if self._fresh(now) or (now < self._retry_at and not self._lock.locked()):
            return self._result(now)
        async with self._lock:
            now = self._clock()
            if self._fresh(now) or now < self._retry_at:
                return self._result(now)
            self._retry_at = now + RETRY_AFTER
            try:
                token = next((value for value in self._tokens() if isinstance(value, str) and value), None)
                if token is None:
                    return self._result(now)
                payload = await asyncio.wait_for(self._fetch(token), timeout=FETCH_TIMEOUT)
                models = _parse_response(payload)
                self._models, self._refreshed_at = models, self._clock()
                self._persist()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log exception text: HTTP client errors may contain URLs,
                # authorization headers or upstream response bodies.
                _logger.warning("Model catalogue refresh unavailable; retaining cached/default entries.")
            return self._result(self._clock())

    async def find_model(self, identifier):
        if not isinstance(identifier, str):
            return None
        if identifier not in {"auto", "gpt-image-2", "chatgpt-image-latest"} and not is_chat_model_id(identifier):
            return None
        return next((model for model in (await self.get_catalog())["data"] if model["id"] == identifier), None)


_catalog = ModelCatalog()


async def get_catalog():
    return await _catalog.get_catalog()


async def find_model(identifier):
    return await _catalog.find_model(identifier)
