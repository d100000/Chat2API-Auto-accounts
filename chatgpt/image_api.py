"""Images API validation and the explicitly named ChatGPT web image bridge."""

import asyncio
import base64
import re
import time
from io import BytesIO
from urllib.parse import urlsplit

from fastapi import HTTPException
from PIL import Image


IMAGE_MODELS = frozenset(("gpt-image-2", "chatgpt-image-latest"))
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def invalid(message, param=None, code="invalid_parameter", status=400):
    exception = HTTPException(status, {"error": {"message": message,
        "type": "server_error" if status >= 500 else "invalid_request_error", "param": param, "code": code}})
    exception.safe_api_error = True
    raise exception


def validate_image_request(value):
    if not isinstance(value, dict):
        invalid("The request body must be a JSON object.")
    allowed = {"model", "prompt", "n", "size", "quality", "response_format", "output_format",
               "background", "stream", "user", "safety_identifier"}
    if set(value) - allowed:
        invalid("Unsupported image parameter: " + sorted(set(value) - allowed)[0])
    model = value.get("model", "chatgpt-image-latest")
    if not isinstance(model, str) or model not in IMAGE_MODELS:
        invalid("Use gpt-image-2 or chatgpt-image-latest at the Images endpoint.", "model", "model_not_found", 404)
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        invalid("prompt must be a non-empty string of at most 32000 characters.", "prompt")
    n = value.get("n", 1)
    if type(n) is not int or n != 1:
        invalid("This account-backed API supports n=1. Send independent requests for concurrent images.", "n")
    if type(value.get("stream", False)) is not bool or value.get("stream", False):
        invalid("Image streaming is not supported. Use stream=false.", "stream")
    enums = {"quality": ("auto", "low", "medium", "high"), "output_format": ("png", "jpeg", "webp"),
             "background": ("auto", "opaque", "transparent"), "response_format": ("b64_json",)}
    defaults = {"quality": "auto", "output_format": "png", "background": "auto", "response_format": "b64_json"}
    for name, choices in enums.items():
        if not isinstance(value.get(name, defaults[name]), str) or value.get(name, defaults[name]) not in choices:
            invalid("Unsupported " + name + ".", name)
    size = value.get("size", "auto")
    match = re.fullmatch(r"([1-9][0-9]{2,3})x([1-9][0-9]{2,3})", size) if isinstance(size, str) else None
    if size != "auto":
        if not match:
            invalid("size must be auto or WIDTHxHEIGHT.", "size")
        width, height = map(int, match.groups())
        if width % 16 or height % 16 or max(width, height) > 3840 or max(width, height) > 3 * min(width, height) or not 655360 <= width * height <= 8294400:
            invalid("Image dimensions must be multiples of 16, within the Image2 pixel and aspect-ratio limits.", "size")
    for name in ("user", "safety_identifier"):
        if name in value and (not isinstance(value[name], str) or len(value[name]) > 128):
            invalid(name + " must be a string of at most 128 characters.", name)
    if value.get("background") == "transparent" and value.get("output_format") == "jpeg":
        invalid("Transparent backgrounds require png or webp.", "output_format")
    if model == "chatgpt-image-latest":
        if size != "auto" or value.get("quality", "auto") != "auto" or value.get("background", "auto") != "auto" or value.get("output_format", "png") != "png":
            invalid("The web image bridge supports auto size/quality/background and png only; it cannot enforce native Image2 settings.")
    return {**value, "model": model, "n": 1, "stream": False, **{k: value.get(k, v) for k, v in defaults.items()}, "size": size}


def web_image_request(data):
    return {**data, "messages": [{"role": "user", "content": "Generate exactly one image using the image generation tool. " + data["prompt"]}],
            "quota_feature": "image_gen", "history_disabled": False, "max_tokens": 8192}


def trusted_image_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        return parsed.scheme == "https" and not parsed.username and not parsed.password and parsed.port in (None, 443) and (
            (host == "chatgpt.com" and parsed.path == "/backend-api/estuary/content") or
            host == "files.oaiusercontent.com" or host.endswith(".oaiusercontent.com") or
            host.endswith(".blob.core.windows.net"))
    except (ValueError, TypeError):
        return False


def verify_image_bytes(raw):
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        invalid("The upstream image exceeded the image size limit.", code="invalid_upstream_image", status=502)
    try:
        with Image.open(BytesIO(raw)) as img:
            if img.format not in ("PNG", "JPEG", "WEBP") or img.width * img.height > 40_000_000:
                raise ValueError("Invalid image")
            img.verify()
    except Exception:
        invalid("The upstream did not return a valid image.", code="invalid_upstream_image", status=502)


async def format_web_images(service, result, data, *, client_factory=None):
    evidence = getattr(getattr(service, "usage_trace", None), "evidence", None)
    # Only URLs resolved from this attempt's completed image assets are eligible.
    # Assistant prose and Markdown may contain arbitrary user-influenced links.
    resolved = getattr(service, "generated_image_urls", {})
    urls = [url for asset, url in resolved.items() if evidence and asset in evidence.generated_assets]
    if not evidence or not evidence.generated_assets or not urls:
        invalid("The account did not return a completed image. No text response is substituted for an image.", code="image_not_generated", status=502)
    url = next((url for url in urls if trusted_image_url(url)), None)
    if url is None:
        invalid("The upstream image URL is not an approved OpenAI image host.", code="invalid_upstream_image", status=502)
    if data["response_format"] == "url":
        invalid("Account-generated image URLs require upstream authentication. Use b64_json.", "response_format")
    else:
        from curl_cffi.requests import AsyncSession
        client = (client_factory or AsyncSession)(trust_env=False, timeout=60,
            impersonate=getattr(service, "impersonate", "chrome123"))
        response = None
        try:
            # ChatGPT's asset resolver can return its authenticated estuary
            # endpoint. Credentials never accompany external signed blob URLs.
            headers = {}
            if urlsplit(url).hostname == "chatgpt.com":
                headers = {key: value for key, value in getattr(service, "base_headers", {}).items()
                           if key.lower() in {"authorization", "user-agent", "oai-device-id", "chatgpt-account-id", "origin", "referer"}}
            response = await client.get(url, headers=headers, allow_redirects=False, stream=True)
            if response.status_code != 200:
                invalid("Could not retrieve the generated image.", code="image_download_failed", status=502)
            chunks, size = [], 0
            async with asyncio.timeout(60):
                async for chunk in response.aiter_content():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        invalid("The generated image is too large.", code="invalid_upstream_image", status=502)
                    chunks.append(chunk)
            raw = b"".join(chunks)
            await asyncio.to_thread(verify_image_bytes, raw)
            row = {"b64_json": base64.b64encode(raw).decode("ascii")}
        finally:
            # curl_cffi's Response.aclose only waits for its download task.
            # Stop an oversized/cancelled transfer before closing the session.
            if response is not None:
                stop = getattr(response, "quit_now", None)
                if stop is not None:
                    stop.set()
                transfer = getattr(response, "astream_task", None)
                if transfer is not None and not transfer.done():
                    transfer.cancel()
                    await asyncio.gather(transfer, return_exceptions=True)
            await asyncio.shield(client.close())
    return {"created": int(time.time()), "data": [row], "chat2api": {
        "requested_model": "chatgpt-image-latest", "route": "chatgpt_web", "model_identity": "not_exposed_by_upstream"}}
