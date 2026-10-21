"""API-only ingress. Run one worker on loopback; expose it through HTTPS.

This process deliberately does not import the console app or account stores.
Only explicit shared or active managed keys may reach the private API, never
direct account tokens. Managed key validity is read from SQLite on each request.
"""

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import re

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from dotenv import dotenv_values
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from chatgpt.api_keys import APIKeyStore, MANAGED_PREFIX, ManagedKeyError, resolve_managed_key
from utils.disconnect import ClientDisconnected, run_connected


ROOT = Path(__file__).resolve().parents[1]
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_ACTIVE_REQUESTS = 10
BODY_TIMEOUT_SECONDS = 30
UPSTREAM_TIMEOUT_SECONDS = 180
IMAGE_UPSTREAM_TIMEOUT_SECONDS = 600
ALLOWED_ROUTES = {("POST", b"/v1/chat/completions"), ("POST", b"/v1/images/generations"), ("GET", b"/v1/models")}
MODEL_ROUTE = re.compile(rb"/v1/models/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
IMAGE_DATA_URL = re.compile(r"data:image/(?:png|jpeg|webp|gif);base64,([A-Za-z0-9+/]+={0,2})")


@dataclass(frozen=True)
class GatewaySettings:
    keys: tuple[str, ...] = ()
    api_prefix: str = ""
    streaming: bool = False
    upload_by_url: bool = False

    def __post_init__(self):
        if self.api_prefix and not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", self.api_prefix):
            raise ValueError("API_PREFIX must contain only plain path segments")
        if any(not key or not key.isascii() or any(char.isspace() for char in key) for key in self.keys):
            raise ValueError("AUTHORIZATION keys must be non-empty ASCII values without whitespace")

    @property
    def upstream_url(self):
        return self.upstream_for("/v1/chat/completions")

    def upstream_for(self, path):
        # Callers pass an explicit endpoint or a model path already matched by
        # MODEL_ROUTE; no URL, query or attachment source controls this origin.
        if path not in ("/v1/chat/completions", "/v1/images/generations", "/v1/models") and MODEL_ROUTE.fullmatch(path.encode("ascii")) is None:
            raise ValueError("Unsupported upstream API path")
        prefix = "/" + self.api_prefix if self.api_prefix else ""
        return "http://127.0.0.1:5005" + prefix + path


def load_settings():
    # Match the existing application's environment-over-file precedence without
    # importing its configuration module (which initializes unrelated stores).
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    keys = tuple(key.strip() for key in (values.get("AUTHORIZATION") or "").split(",") if key.strip())
    upload_by_url = str(values.get("UPLOAD_BY_URL", "false")).lower() in ("true", "1", "t", "y", "yes")
    if upload_by_url:
        raise ValueError("Public access requires UPLOAD_BY_URL=false. Disable automatic URL uploads and restart the private API before starting this gateway.")
    return GatewaySettings(keys=keys, api_prefix=values.get("API_PREFIX", "") or "",
        streaming=str(values.get("PUBLIC_API_STREAMING", "false")).lower() in ("true", "1", "yes"),
        upload_by_url=upload_by_url)


def error(status, code, message, headers=None):
    return JSONResponse({"error": {"message": message, "type": "invalid_request_error" if status < 500 else "server_error",
                                   "code": code}}, status_code=status, headers=headers)


async def authenticated_key(request, settings, key_store):
    values = [value for name, value in request.scope["headers"] if name.lower() == b"authorization"]
    if len(values) != 1:
        return None
    try:
        scheme, supplied = values[0].decode("ascii").split(" ", 1)
    except (UnicodeError, ValueError):
        return None
    if scheme.lower() != "bearer" or not supplied or any(char.isspace() for char in supplied):
        return None
    # Compare all configured keys, including when an earlier key matched.
    matches = [hmac.compare_digest(supplied, key) for key in settings.keys]
    if any(matches):
        return supplied
    if supplied.startswith(MANAGED_PREFIX):
        await asyncio.to_thread(resolve_managed_key, supplied, settings.keys, store=key_store)
        # The generating service receives the original managed key so it can
        # revalidate and count the accepted request exactly once.
        return supplied
    return None


def validate_attachments(data):
    """Block server-side URL downloads before contacting the private API.

    The inherited converter downloads only messages[*].content image_url parts
    when UPLOAD_BY_URL is disabled. Validate all roles and the complete content
    union; unknown nested arrays/objects must not bypass this boundary. Textual
    URLs remain text, and generated image links in responses are unaffected.
    """
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        return error(400, "invalid_messages", "messages must be an array.")
    for message in messages:
        if not isinstance(message, dict):
            return error(400, "invalid_messages", "Each message must be an object with string or array content.")
        content = message.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            return error(400, "invalid_messages", "Each message must have string or array content.")
        for part in content:
            if not isinstance(part, dict):
                return error(400, "invalid_content_part", "Content parts must be text or image_url objects.")
            if part.get("type") == "text":
                if not isinstance(part.get("text"), str) or set(part) - {"type", "text"}:
                    return error(400, "invalid_content_part", "A text part must contain only type and string text.")
                continue
            if part.get("type") != "image_url" or set(part) - {"type", "image_url"}:
                return error(400, "unsupported_attachment", "The public API supports text and inline image_url parts only.")
            image = part.get("image_url")
            if not isinstance(image, dict) or set(image) - {"url", "detail"}:
                return error(400, "invalid_image_url", "image_url must contain a data URL and optional detail.")
            url = image.get("url")
            if not isinstance(url, str):
                return error(400, "invalid_image_url", "image_url.url must be a data:image URL string.")
            match = IMAGE_DATA_URL.fullmatch(url)
            if match is None:
                return error(400, "remote_image_url_unavailable",
                    "The public API accepts inline data:image/png, jpeg, webp or gif;base64 images only. HTTP(S) image URLs are available through the local API only.")
            detail = image.get("detail", "auto")
            if not isinstance(detail, str) or detail not in ("auto", "high", "low"):
                return error(400, "invalid_image_detail", "Image detail must be auto, high or low.")
            try:
                decoded = base64.b64decode(match[1], validate=True)
            except (ValueError, binascii.Error):
                return error(400, "invalid_image_data", "Image data must contain valid base64.")
            if base64.b64encode(decoded).decode("ascii") != match[1]:
                return error(400, "invalid_image_data", "Image data must contain canonical padded base64.")
            if not decoded or len(decoded) > MAX_IMAGE_BYTES:
                return error(413, "image_too_large", "Each decoded inline image must be non-empty and at most 1 MiB.")
    return None


async def read_body(request, *, image_generation=False):
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        if not raw_length.isascii() or not raw_length.isdigit():
            return None, error(400, "invalid_content_length", "Invalid Content-Length.")
        if len(raw_length) > 12 or int(raw_length) > MAX_BODY_BYTES:
            return None, error(413, "request_too_large", "Request body exceeds the 2 MiB limit.")
    chunks, size = [], 0
    try:
        async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return None, error(413, "request_too_large", "Request body exceeds the 2 MiB limit.")
                chunks.append(chunk)
    except TimeoutError:
        return None, error(408, "request_timeout", "Request body timed out.")
    try:
        data = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError):
        return None, error(400, "invalid_json", "Request body must be valid JSON.")
    if not isinstance(data, dict):
        return None, error(400, "invalid_json", "Request body must be a JSON object.")
    if "stream" in data and not isinstance(data["stream"], bool):
        return None, error(400, "invalid_stream", "stream must be a boolean.")
    if image_generation:
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            return None, error(400, "invalid_prompt", "prompt must be a non-empty string of at most 32000 characters.")
        if "model" in data and (not isinstance(data["model"], str) or not data["model"].strip() or len(data["model"]) > 100):
            return None, error(400, "invalid_model", "model must be a non-empty string of at most 100 characters.")
        # The generation endpoint accepts a textual prompt, not input files or
        # remote download sources. Other parameters remain for private validation.
        if any(name in data for name in ("image", "images", "image_url", "input_image", "mask", "file", "files", "file_url", "url")):
            return None, error(400, "unsupported_attachment", "Image generations accepts a prompt, not input attachments or download URLs.")
    attachment_error = validate_attachments(data)
    if attachment_error is not None:
        return None, attachment_error
    # OpenAI clients can omit stream; establish the non-streaming default
    # explicitly so it remains stable at the public boundary.
    return {**data, "stream": data.get("stream", False)}, None


def create_app(settings=None, client_factory=AsyncSession, key_store=None):
    @asynccontextmanager
    async def lifespan(application):
        if application.state.settings is None:
            application.state.settings = load_settings()
        yield

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None,
                          redirect_slashes=False, lifespan=lifespan)
    application.state.settings = settings
    application.state.active_requests = 0
    application.state.key_store = key_store or APIKeyStore()

    @application.middleware("http")
    async def boundary(request, call_next):
        raw_path = request.scope.get("raw_path", request.url.path.encode()).split(b"?", 1)[0]
        allowed = (request.method, raw_path) in ALLOWED_ROUTES or (request.method == "GET" and MODEL_ROUTE.fullmatch(raw_path) is not None)
        if not allowed or request.scope.get("query_string"):
            response = error(404, "not_found", "This gateway only exposes the supported API endpoints.")
        elif application.state.settings is None or not application.state.settings.keys:
            response = error(503, "gateway_unconfigured", "The public API key has not been configured.")
        elif application.state.settings.upload_by_url:
            response = error(503, "unsafe_upload_configuration",
                "Public access requires UPLOAD_BY_URL=false. Disable automatic URL uploads and restart the private API and gateway.")
        else:
            try:
                key = await authenticated_key(request, application.state.settings, application.state.key_store)
                if key is None:
                    response = error(401, "invalid_api_key", "A valid Chat2API API key is required.",
                                     {"WWW-Authenticate": "Bearer"})
                else:
                    request.state.api_key = key
                    response = await call_next(request)
            except ManagedKeyError as exception:
                response = error(exception.status_code, exception.code, str(exception),
                    {"WWW-Authenticate": "Bearer"} if exception.status_code == 401 else None)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def forward(request, path, *, method="POST", image_generation=False):
        if application.state.active_requests >= MAX_ACTIVE_REQUESTS:
            return error(429, "gateway_busy", "The gateway is handling 10 requests. Please retry later.",
                         {"Retry-After": "5"})
        # No await between checking and reserving; this is a single-worker app.
        application.state.active_requests += 1
        client, handed_off, closed = None, False, False

        async def cleanup():
            nonlocal closed
            if closed:
                return
            closed = True
            try:
                if client is not None:
                    await client.close()
            finally:
                application.state.active_requests -= 1

        async def forward_upstream(data):
            nonlocal client
            stream_ready = False
            try:
                streaming = data is not None and data["stream"]
                if streaming and not application.state.settings.streaming:
                    return error(400, "streaming_unavailable",
                        "This temporary public endpoint does not support SSE. Set stream to false, or use the local API for streaming.")
                timeout_seconds = IMAGE_UPSTREAM_TIMEOUT_SECONDS if image_generation else UPSTREAM_TIMEOUT_SECONDS
                response_limit = MAX_IMAGE_RESPONSE_BYTES if image_generation else MAX_RESPONSE_BYTES
                client = client_factory(trust_env=False, curl_options={CurlOpt.PROXY: ""},
                                        timeout=(5, timeout_seconds))
                target = application.state.settings.upstream_for(path)
                headers = {"Authorization": "Bearer " + request.state.api_key}
                if method == "POST":
                    response = await client.post(target, headers={**headers, "Content-Type": "application/json"},
                        json=data, stream=True, allow_redirects=False)
                else:
                    response = await client.get(target, headers=headers, stream=True, allow_redirects=False)
                if not 200 <= response.status_code < 300:
                    status = response.status_code if response.status_code in (400, 401, 403, 404, 409, 413, 422, 429, 500, 502, 503, 504) else 502
                    headers = {}
                    retry = response.headers.get("retry-after", "")
                    if retry.isascii() and retry.isdigit() and len(retry) < 5 and 1 <= int(retry) <= 3600:
                        headers["Retry-After"] = retry
                    return error(status, "upstream_request_failed", "The private API could not complete this request. Check the local account dashboard.", headers)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if streaming:
                    if content_type != "text/event-stream":
                        return error(502, "invalid_upstream_response", "The private API did not return an event stream.")

                    async def events():
                        try:
                            async for chunk in response.aiter_content():
                                yield chunk
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            yield b'data: {"error":{"code":"upstream_stream_error","message":"Response interrupted."}}\n\ndata: [DONE]\n\n'
                        finally:
                            await asyncio.shield(cleanup())

                    stream_ready = True
                    return StreamingResponse(events(), media_type="text/event-stream",
                        headers={"X-Accel-Buffering": "no"}, background=BackgroundTask(cleanup))
                if content_type != "application/json":
                    return error(502, "invalid_upstream_response", "The private API did not return JSON.")
                chunks, size = [], 0
                async with asyncio.timeout(timeout_seconds):
                    async for chunk in response.aiter_content():
                        size += len(chunk)
                        if size > response_limit:
                            return error(502, "upstream_response_too_large", "The private API response exceeded the gateway limit.")
                        chunks.append(chunk)
                try:
                    payload = json.loads(b"".join(chunks))
                except (ValueError, UnicodeError):
                    return error(502, "invalid_upstream_response", "The private API returned invalid JSON.")
                if not isinstance(payload, dict):
                    return error(502, "invalid_upstream_response", "The private API returned invalid JSON.")
                return JSONResponse(payload)
            finally:
                if not stream_ready:
                    await cleanup()

        try:
            data = None
            if method == "POST":
                data, rejection = await read_body(request, image_generation=image_generation)
                if rejection is not None:
                    return rejection
            # The body reader has finished before the disconnect watcher owns
            # receive. Once an SSE response is returned, its own listener takes
            # over; non-streaming forwarding remains monitored through EOF.
            result = await run_connected(request, forward_upstream(data))
            # Transfer cleanup ownership only after the watcher has stopped.
            # Cancellation during that handoff must still close the client.
            handed_off = isinstance(result, StreamingResponse)
            return result
        except ClientDisconnected:
            # BaseHTTPMiddleware expects a response even when the socket has
            # already closed; avoid reporting normal disconnects as HTTP 500.
            return Response(status_code=499)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return error(504, "upstream_timeout", "The private API response timed out.")
        except Exception:
            # Do not echo exception messages: they may contain URLs or credentials.
            return error(502, "upstream_unavailable", "The private API is unavailable or the connection was interrupted.")
        finally:
            if not handed_off:
                await asyncio.shield(cleanup())

    @application.get("/v1/models")
    async def models(request: Request):
        return await forward(request, "/v1/models", method="GET")

    @application.get("/v1/models/{model_id}")
    async def model(request: Request, model_id: str):
        # Validate again for direct endpoint invocation as well as middleware.
        path = "/v1/models/" + model_id
        try:
            valid = MODEL_ROUTE.fullmatch(path.encode("ascii")) is not None
        except UnicodeError:
            valid = False
        if not valid:
            return error(404, "not_found", "Unsupported model identifier.")
        return await forward(request, path, method="GET")

    @application.post("/v1/chat/completions")
    async def chat(request: Request):
        return await forward(request, "/v1/chat/completions")

    @application.post("/v1/images/generations")
    async def images(request: Request):
        return await forward(request, "/v1/images/generations", image_generation=True)

    return application


app = create_app()
