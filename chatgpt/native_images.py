"""Native Images adapter; never relabel a webpage chat image as GPT Image 2.

Only explicit 404/405 responses permit a same-account Responses fallback. All
other errors, including ambiguous disconnects, are terminal for this attempt.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import random
import re
import time
from urllib.parse import urlsplit

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

LOG = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 24 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 600
IMAGE_MODEL = "gpt-image-2"
DEFAULT_REASONING_MODEL = "gpt-5.6-luna"
_IMAGE_MODEL = re.compile(r"gpt-image-2(?:-\d{4}-\d{2}-\d{2})?\Z")
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?\Z")
_IMAGE_FIELDS = frozenset(("model", "prompt", "size", "quality", "background", "output_format", "output_compression", "moderation"))


def native_images_enabled():
    """Explicit opt-in only; webpage credentials do not imply Codex authorization."""
    return os.environ.get("CODEX_NATIVE_IMAGES_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on")


def image_error(status, code, message):
    error = HTTPException(status, {"error": {"type": "upstream_error" if status >= 500 else "invalid_request_error",
                                            "code": code, "message": message}})
    error.account_retry_safe = False
    error.safe_api_error = True
    return error


def _runtime():
    from utils import configs
    return configs


def _select_token(token):
    from chatgpt.authorization import get_req_token
    return get_req_token(token)


async def _verify_token(token):
    from chatgpt.authorization import verify_token
    return await verify_token(token)


def _fingerprint(token):
    from chatgpt.fp import get_fp
    return get_fp(token).copy()


async def _start_trace(token, model, account_id=None):
    from chatgpt.account_usage import start_trace
    return await start_trace(token, model, account_id)


async def _finish_attempt(service, status, code):
    # The entrypoint imports this adapter, so defer the dependency until runtime.
    from api.chat2api import finish_attempt
    await finish_attempt(service, status, code)


async def _acquire_capacity(token, access, account_override):
    from chatgpt.account_quota import _identity
    from chatgpt.account_scheduler import get_scheduler, AccountConcurrencyExhausted
    from chatgpt.account_concurrency import ConcurrencyPersistenceError
    try:
        return await get_scheduler().acquire_for_account(token, "image_gen",
            account_id=_identity(access, override=account_override)["id"], account_override=account_override,
            quota_mode="none", channel="codex_native")
    except AccountConcurrencyExhausted as error:
        busy = image_error(429, error.code, "该账号的生图并发已满，请等待当前请求完成。")
        busy.headers = {"Retry-After": str(error.retry_after_seconds)}
        raise busy from None
    except ConcurrencyPersistenceError:
        raise image_error(503, "concurrency_configuration_unavailable", "账号并发配置暂不可用。") from None


def _new_session(proxy, impersonate):
    from curl_cffi import CurlOpt
    from curl_cffi.requests import AsyncSession
    return AsyncSession(proxy=proxy, trust_env=False, impersonate=impersonate,
                        timeout=REQUEST_TIMEOUT_SECONDS,
                        curl_options={} if proxy else {CurlOpt.PROXY: ""})


def _account_claim(token):
    try:
        parts = token.split(".")
        if len(parts) != 3 or len(parts[1]) > 32768:
            return None
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        auth = claims.get("https://api.openai.com/auth", {})
        value = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
        return value if isinstance(value, str) and _ACCOUNT_ID.fullmatch(value) else None
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return None


def _safe_usage(value, depth=0):
    if not isinstance(value, dict) or depth > 2:
        return None
    result = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "text_tokens", "image_tokens", "cached_tokens"):
        amount = value.get(key)
        if isinstance(amount, int) and not isinstance(amount, bool) and 0 <= amount <= 10**12:
            result[key] = amount
    for key in ("input_tokens_details", "output_tokens_details", "cached_tokens_details"):
        details = _safe_usage(value.get(key), depth + 1)
        if details:
            result[key] = details
    return result or None


def _validate_image(encoded):
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_RESPONSE_BYTES:
        raise image_error(502, "invalid_image_response", "上游没有返回有效的图片数据。")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise image_error(502, "invalid_image_response", "上游返回的图片编码无效。") from None
    if len(content) > MAX_IMAGE_BYTES:
        raise image_error(502, "image_response_too_large", "上游图片超过本地接收大小限制。")
    signature = (content.startswith(b"\x89PNG\r\n\x1a\n") or content.startswith(b"\xff\xd8\xff")
                 or content.startswith((b"GIF87a", b"GIF89a"))
                 or (content.startswith(b"RIFF") and content[8:12] == b"WEBP"))
    if not signature:
        raise image_error(502, "invalid_image_response", "上游返回的数据不是受支持的图片文件。")
    try:
        with Image.open(io.BytesIO(content)) as picture:
            output_format, size = picture.format.lower(), picture.size
            picture.verify()
        if output_format not in ("png", "jpeg", "webp", "gif") or min(size) < 1:
            raise ValueError("invalid image")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise image_error(502, "invalid_image_response", "上游返回的图片文件不完整。") from None
    return output_format, f"{size[0]}x{size[1]}"


def _normalise_result(payload, *, requested_format=None):
    if not isinstance(payload, dict) or payload.get("error"):
        raise image_error(502, "native_image_failed", "原生生图接口未完成请求。")
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise image_error(502, "invalid_image_response", "上游没有返回请求的一张最终图片。")
    row = rows[0]
    output_format, size = _validate_image(row.get("b64_json"))
    if requested_format and requested_format != output_format:
        raise image_error(502, "image_format_mismatch", "上游图片格式与请求格式不一致。")
    observed = row.get("model", payload.get("model"))
    if observed is not None and (not isinstance(observed, str) or not _IMAGE_MODEL.fullmatch(observed)):
        raise image_error(502, "image_model_mismatch", "上游返回的图片模型与请求的 GPT Image 2 不一致。")
    data = {"b64_json": row["b64_json"]}
    if isinstance(row.get("revised_prompt"), str):
        data["revised_prompt"] = row["revised_prompt"][:16000]
    created = payload.get("created")
    result = {"created": created if isinstance(created, int) and not isinstance(created, bool) and 0 < created < 10**11 else int(time.time()),
              "data": [data], "output_format": output_format, "size": size}
    if observed:
        result["model"] = observed
    usage = _safe_usage(payload.get("usage"))
    if usage:
        result["usage"] = usage
    return result, observed


def build_responses_payload(data):
    outer_model = os.environ.get("CODEX_IMAGE_REASONING_MODEL", DEFAULT_REASONING_MODEL).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", outer_model) or outer_model.startswith("gpt-image-"):
        raise image_error(503, "invalid_native_configuration", "原生生图的推理模型配置无效。")
    tool = {key: value for key, value in data.items() if key in _IMAGE_FIELDS and key not in ("prompt",)}
    tool = {**tool, "type": "image_generation", "action": "generate"}
    return {"model": outer_model, "instructions": "When invoking the image_generation tool, use the user's image prompt verbatim.",
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": data["prompt"]}]}],
            "tools": [tool], "tool_choice": {"type": "image_generation"}, "store": False, "stream": True}


def _responses_result(body):
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        raise image_error(502, "invalid_image_response", "上游返回了无法解析的图片响应。") from None
    completed, completed_rows, fallback_rows, created, usage = False, [], {}, None, None
    data_lines = []
    # SSE records can contain multiple data lines and CRLF separators.
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        elif line == "" and data_lines:
            value = "\n".join(data_lines)
            data_lines = []
            if value == "[DONE]":
                continue
            try:
                event = json.loads(value)
            except (ValueError, TypeError):
                raise image_error(502, "invalid_image_response", "上游图片响应包含无效的事件数据。") from None
            if not isinstance(event, dict):
                raise image_error(502, "invalid_image_response", "上游图片事件格式无效。")
            kind = event.get("type")
            if event.get("error") or kind in ("error", "response.failed", "response.incomplete", "response.cancelled"):
                raise image_error(502, "native_image_failed", "原生图片生成未完整完成，请查看账号原生接口权限。")
            if kind == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "image_generation_call" and item.get("result"):
                    marker = item.get("id") or hashlib.sha256(str(item.get("result")).encode()).hexdigest()
                    if not isinstance(marker, str):
                        raise image_error(502, "invalid_image_response", "上游图片事件标识无效。")
                    fallback_rows[marker] = item
            elif kind == "response.completed":
                response = event.get("response")
                if not isinstance(response, dict) or response.get("status", "completed") != "completed":
                    raise image_error(502, "incomplete_image_response", "上游图片生成尚未完整结束。")
                completed = True
                output = response.get("output", [])
                if not isinstance(output, list):
                    raise image_error(502, "invalid_image_response", "上游图片输出格式无效。")
                completed_rows = [item for item in output if isinstance(item, dict) and item.get("type") == "image_generation_call" and item.get("result")]
                created = response.get("created_at")
                tool_usage = response.get("tool_usage", {})
                usage = tool_usage.get("image_gen") if isinstance(tool_usage, dict) else None
    if not completed:
        raise image_error(502, "incomplete_image_response", "上游连接结束，但未收到图片生成完成事件。")
    rows = completed_rows or list(fallback_rows.values())
    if len(rows) != 1 or rows[0].get("status", "completed") != "completed":
        raise image_error(502, "invalid_image_response", "上游没有返回请求的一张最终图片。")
    row = rows[0]
    data = {"b64_json": row.get("result")}
    for key in ("model", "revised_prompt"):
        if key in row:
            data[key] = row[key]
    return {"created": created, "data": [data], "usage": usage}


class NativeImageService:
    def __init__(self, req_token, lease=None):
        self.req_token = lease.token if lease else req_token
        self.schedule_lease = lease
        self.schedule_finished = False
        self.schedule_error = {"kind": "request"}
        self.conversation_dispatched = False
        self.conversation_accepted = False
        self.conversation_rejected = False
        self.usage_trace = None
        self.session = None
        self.response = None
        self._finish_task = None
        self._closed = False

    async def prepare(self, data):
        if not self.schedule_lease:
            self.req_token = _select_token(self.req_token)
        self.usage_trace = await _start_trace(self.req_token, IMAGE_MODEL, data.get("Chatgpt-Account-Id"))
        if self.usage_trace:
            self.usage_trace.scheduled_feature = "image_gen"
            self.usage_trace.set_model(IMAGE_MODEL)
        if not isinstance(self.req_token, str) or not self.req_token:
            raise image_error(403, "unsupported_credentials", "原生 GPT Image 2 需要具有 Codex 图片接口权限的账号凭据。")
        token, _, embedded = self.req_token.partition(",")
        try:
            access = await _verify_token(token)
        except HTTPException:
            raise image_error(403, "unsupported_credentials", "当前账号凭据不能用于原生图片接口，请更新或授权 Codex 凭据。") from None
        if not isinstance(access, str) or not access or "\r" in access or "\n" in access:
            raise image_error(403, "unsupported_credentials", "当前账号没有可用于原生图片接口的访问令牌。")
        fingerprint = _fingerprint(self.req_token)
        account_id = (data.get("Chatgpt-Account-Id") or embedded or fingerprint.get("chatgpt-account-id") or _account_claim(access))
        if account_id and (not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id)):
            raise image_error(400, "invalid_account_id", "账号工作区标识无效。")
        if not self.schedule_lease:
            self.schedule_lease = await _acquire_capacity(self.req_token, access, account_id)
        config = _runtime()
        base = random.choice(config.chatgpt_base_url_list) if config.chatgpt_base_url_list else "https://chatgpt.com"
        origin = urlsplit(base)
        if origin.scheme not in ("https", "http") or not origin.netloc or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ("", "/"):
            raise image_error(503, "invalid_native_configuration", "ChatGPT 上游地址必须是有效的服务根地址。")
        self.base_url = f"{origin.scheme}://{origin.netloc}/backend-api/codex"
        version = os.environ.get("CODEX_CLIENT_VERSION", "0.146.0").strip()
        if not _VERSION.fullmatch(version):
            raise image_error(503, "invalid_native_configuration", "Codex 客户端版本配置无效。")
        self.headers = {"Authorization": "Bearer " + access, "Content-Type": "application/json", "Accept": "application/json",
                        "User-Agent": f"codex_cli_rs/{version}", "Originator": "codex_cli_rs", "Version": version}
        if account_id:
            self.headers["ChatGPT-Account-ID"] = account_id
        if getattr(config, "auth_key", None):
            self.headers["authkey"] = config.auth_key
        proxy = fingerprint.get("proxy_url")
        if proxy:
            proxy = proxy.replace("{}", hashlib.sha256(self.req_token.encode()).hexdigest()[:32])
        self.session = _new_session(proxy, fingerprint.get("impersonate", "safari15_3"))

    async def _close_response(self, response):
        if self.response is not response:
            return
        self.response = None
        # curl_cffi Response.aclose only waits for its download task. Stopping
        # consumption alone otherwise leaves that task filling an unbounded queue.
        quit_now = getattr(response, "quit_now", None)
        if quit_now is not None:
            quit_now.set()
        stream_task = getattr(response, "astream_task", None)
        if stream_task is not None and not stream_task.done():
            stream_task.cancel()
        try:
            await response.aclose()
        except asyncio.CancelledError:
            if stream_task is None or not stream_task.cancelled():
                raise
        except Exception:
            LOG.warning("Native image response cleanup failed.")

    async def close_client(self):
        if self._closed:
            return
        self._closed = True
        if self.response is not None:
            await self._close_response(self.response)
        if self.session is not None:
            session, self.session = self.session, None
            try:
                await session.close()
            except Exception:
                LOG.warning("Native image client cleanup failed.")

    async def finish_once(self, status, code=None):
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(_finish_attempt(self, status, code))
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._finish_task)
                break
            except asyncio.CancelledError:
                # Repeated disconnect cancellation must not abandon cleanup.
                if self._finish_task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _post(self, suffix, payload, *, responses=False):
        headers = {**self.headers, "Accept": "text/event-stream" if responses else "application/json"}
        if responses:
            headers["OpenAI-Beta"] = "responses=experimental"
        self.conversation_dispatched = True
        response = await self.session.post(self.base_url + suffix, headers=headers, json=payload,
                                           stream=True, allow_redirects=False, timeout=REQUEST_TIMEOUT_SECONDS)
        self.response = response
        try:
            status = response.status_code
            if not 200 <= status < 300:
                self.conversation_rejected = True
                self.conversation_dispatched = False
                if status in (404, 405) and not responses:
                    return None
                if status in (401, 403):
                    raise image_error(403, "unsupported_credentials", "当前账号凭据或上游未授权原生 GPT Image 2。网页对话可用不代表原生图片接口可用。")
                if status == 429:
                    raise image_error(429, "native_image_rate_limited", "原生图片接口当前限流或额度不足，请稍后重试。")
                raise image_error(502, "native_image_unavailable", "原生图片上游拒绝了请求；未自动重试或切换模型。")
            self.conversation_accepted = True
            length = response.headers.get("content-length", "")
            if str(length).isdigit() and int(length) > MAX_RESPONSE_BYTES:
                raise image_error(502, "image_response_too_large", "上游图片响应超过 32 MiB 接收限制。")
            chunks, size = [], 0
            async for chunk in response.aiter_content():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise image_error(502, "image_response_too_large", "上游图片响应超过 32 MiB 接收限制。")
                chunks.append(chunk)
            body = b"".join(chunks)
            if responses:
                return _responses_result(body)
            try:
                return json.loads(body)
            except (ValueError, UnicodeError):
                raise image_error(502, "invalid_image_response", "原生图片接口没有返回有效的 JSON 数据。") from None
        finally:
            await asyncio.shield(self._close_response(response))

    async def generate(self, data):
        await self.prepare(data)
        payload = {key: value for key, value in data.items() if key in _IMAGE_FIELDS}
        payload["model"] = IMAGE_MODEL
        result = await self._post("/images/generations", payload)
        if result is None:
            result = await self._post("/responses", build_responses_payload(payload), responses=True)
        result, observed_model = _normalise_result(result, requested_format=data.get("output_format"))
        if self.usage_trace:
            self.usage_trace.observe_image_result(observed_model, 1)
        return result


async def process_native_images(request_data, req_token, lease=None):
    """Return (service, Images JSON), after exactly one trace/lease/client finish."""
    service = NativeImageService(req_token, lease)
    if request_data.get("_request_metrics") is not None:
        request_data["_request_metrics"].attach(service)
    status, code = "failed", "native_image_failed"
    try:
        if not native_images_enabled():
            raise image_error(403, "unsupported_credentials", "原生 GPT Image 2 接口尚未启用，需要独立的 Codex 授权。当前网页图片通道请使用 chatgpt-image-latest。")
        if request_data.get("model") != IMAGE_MODEL or type(request_data.get("n", 1)) is not int or request_data.get("n", 1) != 1 or request_data.get("stream", False):
            raise image_error(400, "invalid_image_request", "原生接口当前仅支持 gpt-image-2、n=1 和非流式响应。")
        if request_data.get("response_format", "b64_json") != "b64_json":
            raise image_error(400, "unsupported_response_format", "原生图片接口当前仅支持 b64_json，不下载上游返回的 URL。")
        if not isinstance(request_data.get("prompt"), str) or not request_data["prompt"].strip():
            raise image_error(400, "invalid_image_request", "prompt 不能为空。")
        result = await asyncio.wait_for(service.generate(request_data), timeout=REQUEST_TIMEOUT_SECONDS)
        status, code = "succeeded", None
        return service, result
    except asyncio.CancelledError:
        status, code = "cancelled", "client_cancelled"
        raise
    except asyncio.TimeoutError:
        code = "native_image_timeout"
        raise image_error(504, code, "原生生图等待超过 10 分钟，结果未知；没有自动重试，请勿立即重复提交。") from None
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        safe = detail.get("error", {})
        code = safe.get("code", "native_image_failed") if isinstance(safe, dict) else "native_image_failed"
        raise
    except Exception:
        code = "native_image_connection_error"
        raise image_error(502, code, "原生图片连接中断，结果未知；没有自动重试或切换账号。") from None
    finally:
        await service.finish_once(status, code)
