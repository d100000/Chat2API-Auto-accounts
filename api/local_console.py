"""Authenticated, loopback-only playground for the existing chat API."""

import asyncio
from collections import defaultdict, deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Literal

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from app import app, templates
from utils import configs, globals
from chatgpt import account_quota, account_usage

ROOT = Path(__file__).resolve().parents[1]
COOKIE = "chat2api_console"
SESSION_SECONDS = 8 * 60 * 60
USERNAME = os.environ.get("LOCAL_CONSOLE_USERNAME", "admin")
PASSWORD_HASH = os.environ.get("LOCAL_CONSOLE_PASSWORD_HASH", "")
ALLOWED_HOSTS = {"127.0.0.1:5005", "localhost:5005"}
SESSIONS = {}
FAILURES = defaultdict(deque)
BUSY = set()
QUOTA_TASK = None


def quota_interval():
    try:
        value = int(os.environ.get("LOCAL_QUOTA_AUTO_REFRESH_SECONDS", "300"))
    except ValueError:
        return 300
    return 0 if value == 0 else max(60, value)


@app.on_event("startup")
async def start_quota_monitor():
    global QUOTA_TASK
    try:
        await asyncio.to_thread(account_usage.get_store)
    except Exception:
        account_usage.mark_unavailable()
        from utils.Logger import logger
        logger.error("Local usage storage is unavailable; proxy startup will continue.")
    if not quota_interval():
        return

    async def monitor():
        while True:
            try:
                await account_quota.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                from utils.Logger import logger
                logger.warning("Quota background refresh failed; retained snapshots will be shown as stale.")
            await asyncio.sleep(quota_interval())

    QUOTA_TASK = asyncio.create_task(monitor())


@app.on_event("shutdown")
async def stop_quota_monitor():
    if QUOTA_TASK:
        QUOTA_TASK.cancel()
        await asyncio.gather(QUOTA_TASK, return_exceptions=True)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class RefreshAccountsBody(BaseModel):
    account_id: str | None = Field(default=None, pattern=r"^acct_[0-9a-f]{24}$")


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32000)


class ChatBody(BaseModel):
    model: str = Field(default="auto", min_length=1, max_length=100, pattern=r"\S")
    messages: list[Message] = Field(min_length=1, max_length=40)
    stream: bool = True
    max_tokens: int = Field(default=256, ge=1, le=4096)

    @model_validator(mode="after")
    def limit_context(self):
        if sum(len(message.content) for message in self.messages) > 64000:
            raise ValueError("对话内容过长，请清空会话后重试。")
        return self


def current_session(request):
    now = time.monotonic()
    for token, expires in list(SESSIONS.items()):
        if expires <= now:
            SESSIONS.pop(token, None)
    token = request.cookies.get(COOKIE)
    return token if token in SESSIONS else None


def require_session(request):
    token = current_session(request)
    if not token:
        raise HTTPException(401, "登录已过期，请重新登录。")
    return token


def valid_password(password):
    try:
        algorithm, rounds, salt, expected = PASSWORD_HASH.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def safe_error(value):
    text = str(value)
    for token in [*configs.authorization_list, *globals.token_list]:
        if token:
            text = text.replace(token, "[已隐藏凭据]")
    return re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[已隐藏凭据]", text)[:1000]


@app.middleware("http")
async def console_boundary(request: Request, call_next):
    path = request.url.path
    prefix = f"/{configs.api_prefix}" if configs.api_prefix else ""
    management_path = path[len(prefix):] if prefix and path.startswith(prefix + "/") else path
    management = management_path == "/tokens" or management_path.startswith(("/tokens/", "/seed_tokens/"))
    console = path in {"/", "/login", "/playground", "/accounts"} or path.startswith("/local-console/")
    if console or management:
        if request.headers.get("host") not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "请使用本机地址访问。"}, 400)
        if management and not current_session(request):
            if management_path == "/tokens" and request.method == "GET":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"detail": "请先登录本地测试台。"}, 401)
        mutating = request.method not in {"GET", "HEAD", "OPTIONS"} or management_path.startswith("/tokens/add/")
        if mutating:
            origin = request.headers.get("origin", "")
            if origin != f"http://{request.headers.get('host')}":
                return JSONResponse({"detail": "请求来源校验失败，请从本地测试页面操作。"}, 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response
    return await call_next(request)


app.mount("/static/local-console", StaticFiles(directory=ROOT / "static/local-console"), name="local-console-assets")


@app.get("/", include_in_schema=False)
async def console_root():
    return RedirectResponse("/playground", status_code=303)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    if current_session(request):
        return RedirectResponse("/playground", status_code=303)
    return templates.TemplateResponse(request=request, name="local_console_login.html", context={})


@app.get("/playground", response_class=HTMLResponse, include_in_schema=False)
async def playground(request: Request):
    if not current_session(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request=request, name="local_console.html", context={})


@app.get("/accounts", response_class=HTMLResponse, include_in_schema=False)
async def accounts_page(request: Request):
    if not current_session(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request=request, name="local_accounts.html", context={})


async def with_local_usage(snapshot):
    try:
        store = await asyncio.to_thread(account_usage.get_store)
        summaries = await asyncio.to_thread(store.summaries, [account["id"] for account in snapshot["accounts"]])
    except Exception:
        account_usage.mark_unavailable()
        # Keep upstream quota readable even if local history storage is broken.
        return {**snapshot, "usage_started_at": None, "usage_recording_status": "incomplete",
                "auto_refresh_seconds": quota_interval(),
                "accounts": [{**account, "local_usage": None} for account in snapshot["accounts"]]}
    return {**snapshot, "usage_started_at": summaries["since"],
            "usage_recording_status": account_usage.recording_status(),
            "auto_refresh_seconds": quota_interval(),
            "accounts": [{**account, "local_usage": summaries["accounts"][account["id"]]} for account in snapshot["accounts"]]}


@app.get("/local-console/accounts", include_in_schema=False)
async def accounts_info(request: Request):
    require_session(request)
    return await with_local_usage(account_quota.get_accounts())


@app.post("/local-console/accounts/refresh", include_in_schema=False)
async def refresh_accounts(body: RefreshAccountsBody, request: Request):
    require_session(request)
    try:
        snapshot = await account_quota.refresh(body.account_id)
    except KeyError:
        raise HTTPException(404, "账号不存在或已从 Token 池移除。")
    return await with_local_usage(snapshot)


@app.post("/local-console/login", include_in_schema=False)
async def login(body: LoginBody, request: Request):
    if not PASSWORD_HASH:
        raise HTTPException(503, "本地登录尚未配置。")
    now = time.monotonic()
    failures = FAILURES[request.client.host]
    while failures and failures[0] < now - 60:
        failures.popleft()
    if len(failures) >= 5:
        raise HTTPException(429, "尝试次数过多，请一分钟后重试。", headers={"Retry-After": "60"})
    password_ok = await asyncio.to_thread(valid_password, body.password)
    if not (hmac.compare_digest(body.username.encode(), USERNAME.encode()) and password_ok):
        failures.append(now)
        raise HTTPException(401, "用户名或密码不正确。")
    failures.clear()
    SESSIONS.pop(request.cookies.get(COOKIE), None)
    current_session(request)
    if len(SESSIONS) >= 20:
        SESSIONS.pop(next(iter(SESSIONS)))
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = now + SESSION_SECONDS
    response = JSONResponse({"ok": True, "redirect": "/playground"})
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/")
    return response


@app.post("/local-console/logout", include_in_schema=False)
async def logout(request: Request):
    SESSIONS.pop(request.cookies.get(COOKIE), None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@app.get("/local-console/session", include_in_schema=False)
async def session_info(request: Request):
    require_session(request)
    return {"username": USERNAME, "token_count": len(set(globals.token_list)),
            "error_token_count": len(set(globals.error_token_list)),
            "default_model": "auto", "base_url": str(request.base_url).rstrip("/") + "/v1"}


@app.post("/local-console/chat", include_in_schema=False)
async def test_chat(body: ChatBody, request: Request):
    token = require_session(request)
    if token in BUSY:
        raise HTTPException(409, "上一条请求仍在处理，请稍后重试。")
    if not configs.authorization_list or not globals.token_list:
        raise HTTPException(503, "尚未配置本地密钥或网页账号。")
    BUSY.add(token)
    client = AsyncSession(curl_options={CurlOpt.PROXY: ""}, timeout=(5, 180))
    prefix = f"/{configs.api_prefix}" if configs.api_prefix else ""
    url = f"http://127.0.0.1:5005{prefix}/v1/chat/completions"
    handed_to_stream = False
    try:
        response = await client.post(url, headers={"Authorization": f"Bearer {configs.authorization_list[0]}"},
                                     json=body.model_dump(), stream=True, allow_redirects=False)
        content_type = response.headers.get("content-type", "")
        if response.status_code != 200:
            detail = safe_error(await response.atext())
            # A provider's 401 must not be mistaken for an expired console login.
            status = 502 if response.status_code == 401 else response.status_code
            if response.status_code == 401:
                detail = "上游账号认证失败（HTTP 401），请检查账号凭据。 " + detail
            return JSONResponse({"detail": detail or "反代请求失败，请稍后重试。",
                                 "upstream_status": response.status_code}, status)
        if body.stream and "text/event-stream" in content_type:
            async def events():
                try:
                    async for chunk in response.aiter_content():
                        yield chunk
                except asyncio.CancelledError:
                    raise
                except Exception:
                    yield 'data: {"error":{"message":"响应连接中断，请重试。"}}\n\ndata: [DONE]\n\n'
                finally:
                    BUSY.discard(token)
                    await asyncio.shield(client.close())
            handed_to_stream = True
            return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
        try:
            payload = json.loads(await response.atext())
        except (ValueError, UnicodeError):
            return JSONResponse({"detail": "上游返回了无法识别的响应格式。"}, 502)
        return JSONResponse(payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        return JSONResponse({"detail": "本地反代连接失败或超时，请稍后重试。"}, 502)
    finally:
        if not handed_to_stream:
            BUSY.discard(token)
            await asyncio.shield(client.close())
