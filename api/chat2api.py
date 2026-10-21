import asyncio
import hmac
import re
import types

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Request, HTTPException, Form, Security
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exception_handlers import http_exception_handler

import utils.globals as globals
from app import app, templates, security_scheme
from chatgpt.ChatService import ChatService
from chatgpt.authorization import refresh_all_tokens
from chatgpt.account_usage import start_trace
from chatgpt.api_keys import MANAGED_PREFIX, ManagedKeyError, resolve_managed_key
from chatgpt.scheduling import run_request, failure
from chatgpt.image_api import validate_image_request, web_image_request, format_web_images, IMAGE_MODELS
from chatgpt.request_metrics import start_request, measured_stream
from utils.Logger import logger
from utils.disconnect import ClientDisconnected, run_connected
from utils.configs import api_prefix, scheduled_refresh, authorization_list

scheduler = AsyncIOScheduler()


@app.exception_handler(StarletteHTTPException)
async def api_error(request, exception):
    prefix = f"/{api_prefix}" if api_prefix else ""
    if not request.url.path.startswith(prefix + "/v1/"):
        return await http_exception_handler(request, exception)
    detail = exception.detail
    if exception.status_code == 403 and detail in ("Not authenticated", "Invalid authentication credentials"):
        return JSONResponse({"error": {"message": "A valid API key is required.", "type": "authentication_error",
            "param": None, "code": "invalid_api_key"}}, 401, headers={"WWW-Authenticate": "Bearer"})
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        payload = detail
    else:
        message = detail.get("message") or detail.get("error") if isinstance(detail, dict) else detail
        payload = {"error": {"message": message if isinstance(message, str) else "Request failed.",
            "type": "authentication_error" if exception.status_code in (401, 403) else "invalid_request_error" if exception.status_code < 500 else "server_error",
            "param": None, "code": detail.get("code", "request_failed") if isinstance(detail, dict) else "request_failed"}}
    headers = dict(exception.headers or {})
    if getattr(request.state, "analytics_request_id", None):
        headers["X-Request-ID"] = request.state.analytics_request_id
    return JSONResponse(payload, exception.status_code, headers=headers)


@app.on_event("startup")
async def app_start():
    if scheduled_refresh:
        scheduler.add_job(id='refresh', func=refresh_all_tokens, trigger='cron', hour=3, minute=0, day='*/2',
                          kwargs={'force_refresh': True})
        scheduler.start()
        asyncio.get_event_loop().call_later(0, lambda: asyncio.create_task(refresh_all_tokens(force_refresh=False)))


async def to_send_conversation(request_data, req_token, lease=None):
    chat_service = ChatService(req_token, selected_token=lease.token) if lease else ChatService(req_token)
    metrics = request_data.get("_request_metrics")
    if metrics is not None:
        metrics.attach(chat_service)
    chat_service.schedule_lease = lease
    # req_token may be the shared local key; only the service knows which
    # imported account this attempt will actually use.
    try:
        chat_service.usage_trace = await start_trace(
            chat_service.req_token, request_data.get("model", "gpt-3.5-turbo-0125"),
            request_data.get("Chatgpt-Account-Id"))
        if chat_service.usage_trace:
            chat_service.usage_trace.scheduled_feature = lease.feature if lease else "reason"
        await chat_service.set_dynamic_data(request_data)
        if chat_service.usage_trace:
            chat_service.usage_trace.set_model(chat_service.req_model)
        await chat_service.get_chat_requirements()
        return chat_service
    except asyncio.CancelledError:
        await finish_attempt(chat_service, "cancelled", "client_cancelled")
        raise
    except HTTPException as e:
        failure(chat_service, e)
        safe_detail = e.detail.get("error", {}) if isinstance(e.detail, dict) else {}
        code = safe_detail.get("code") if isinstance(safe_detail, dict) else None
        await finish_attempt(chat_service, "failed", code or f"http_{e.status_code}")
        raise
    except Exception:
        error = failure(chat_service, HTTPException(500, "Server error"))
        await finish_attempt(chat_service, "failed", "setup_error")
        logger.error("Chat setup failed.")
        raise error


async def finish_attempt(service, status, error_code=None):
    try:
        lease = getattr(service, "schedule_lease", None)
        if lease and not getattr(service, "schedule_finished", False):
            from chatgpt.account_scheduler import get_scheduler
            evidence = getattr(service.usage_trace, "evidence", None)
            await asyncio.shield(get_scheduler().finish(lease, status=status,
                dispatched=getattr(service, "conversation_dispatched", False),
                error=getattr(service, "schedule_error", None),
                image_requested=bool(evidence and evidence.image_requested)))
            service.schedule_finished = True
        if service.usage_trace:
            await asyncio.shield(service.usage_trace.finish(status, error_code))
    finally:
        await asyncio.shield(service.close_client())


async def tracked_stream(service, response):
    outcome, error_code = "interrupted", "incomplete_stream"
    final_outcome = False
    try:
        async for chunk in response:
            if service.usage_trace:
                service.usage_trace.observe_output(chunk)
            # Delay DONE until the upstream completion evidence has been checked.
            if isinstance(chunk, str) and chunk.strip() == "data: [DONE]":
                continue
            yield chunk
        if service.usage_trace and not service.usage_trace.complete:
            final_outcome = True
            await service.usage_trace.finish(outcome, error_code)
            yield 'data: {"error":{"message":"上游响应未完整结束，请检查账号状态后重试。","code":"incomplete_stream"}}\n\n'
        else:
            outcome, error_code = "succeeded", None
            final_outcome = True
            if service.usage_trace:
                await service.usage_trace.finish(outcome)
        yield "data: [DONE]\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        if not final_outcome:
            outcome, error_code = "cancelled", "client_cancelled"
        raise
    except Exception as error:
        failure(service, error)
        outcome, error_code = "failed", "stream_error"
        yield 'data: {"error":{"message":"上游响应失败或连接中断，请检查账号状态后重试。","code":"upstream_stream_error"}}\n\ndata: [DONE]\n\n'
    finally:
        await asyncio.shield(finish_attempt(service, outcome, error_code))


async def process(request_data, req_token, lease=None, *, transformer=None):
    chat_service = await to_send_conversation(request_data, req_token, lease)
    try:
        await chat_service.prepare_send_conversation()
        if chat_service.usage_trace:
            chat_service.usage_trace.exclude_inputs(chat_service.chat_request.get("messages", []))
        res = await chat_service.send_conversation()
        if isinstance(res, types.AsyncGeneratorType):
            return chat_service, tracked_stream(chat_service, res)
        if chat_service.usage_trace and not chat_service.usage_trace.complete:
            await finish_attempt(chat_service, "interrupted", "incomplete_stream")
            raise HTTPException(502, "Upstream response did not complete.")
        if transformer is not None:
            res = await transformer(chat_service, res, request_data)
        await finish_attempt(chat_service, "succeeded")
        return chat_service, res
    except asyncio.CancelledError:
        await asyncio.shield(finish_attempt(chat_service, "cancelled", "client_cancelled"))
        raise
    except HTTPException as e:
        failure(chat_service, e)
        safe_detail = e.detail.get("error", {}) if isinstance(e.detail, dict) else {}
        code = safe_detail.get("code") if isinstance(safe_detail, dict) else None
        await finish_attempt(chat_service, "failed", code or f"http_{e.status_code}")
        raise
    except Exception:
        error = failure(chat_service, HTTPException(500, "Server error"))
        await finish_attempt(chat_service, "failed", "request_error")
        raise error


async def pool_key(token, *, record_use=False):
    try:
        return await asyncio.to_thread(resolve_managed_key, token, authorization_list, record_use=record_use)
    except ManagedKeyError as exception:
        raise HTTPException(exception.status_code, {"error": str(exception), "code": exception.code},
                            headers={"WWW-Authenticate": "Bearer"} if exception.status_code == 401 else None) from None


@app.get(f"/{api_prefix}/v1/models" if api_prefix else "/v1/models")
async def list_models(credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    await api_key_only(credentials.credentials)
    from chatgpt.model_catalog import get_catalog
    return await get_catalog()


async def api_key_only(token, *, record_use=False):
    if not authorization_list:
        raise HTTPException(503, "尚未配置系统共享密钥。")
    if token.startswith(MANAGED_PREFIX):
        return await pool_key(token, record_use=record_use)
    elif not token.isascii() or not any(hmac.compare_digest(token, key) for key in authorization_list):
        raise HTTPException(401, "API 密钥无效。", headers={"WWW-Authenticate": "Bearer"})
    return token


@app.get(f"/{api_prefix}/v1/models/{{model_id}}" if api_prefix else "/v1/models/{model_id}")
async def retrieve_model(model_id: str, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    await api_key_only(credentials.credentials)
    from chatgpt.model_catalog import get_catalog
    catalog = await get_catalog()
    model = next((row for row in catalog["data"] if row["id"] == model_id), None)
    if model is None:
        raise HTTPException(404, {"error": {"message": "Model is not in this account pool's catalog.",
            "type": "invalid_request_error", "param": "model", "code": "model_not_found"}})
    return model


async def validate_chat_model(data):
    model = data.get("model", "auto")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", model):
        raise HTTPException(400, "model must be a model identifier of at most 100 characters")
    if model in IMAGE_MODELS or "image" in model or model.startswith("dall-e"):
        raise HTTPException(400, {"error": {"message": "Use /v1/images/generations for image models.",
            "type": "invalid_request_error", "param": "model", "code": "model_endpoint_mismatch"}})
    from api.models import model_proxy
    from chatgpt.model_catalog import get_catalog
    # Retain inherited aliases and custom GPTs; current GPT slugs must come from
    # model discovery, and unknown labels must never become GPT-4o silently.
    legacy = {"gpt-4", "gpt-4-mobile", "gpt-4o-canmore", "o3", "o1", "o1-pro", "o3-mini-high", "o3-mini-medium", "o3-mini-low"} | {
        value for key, value in model_proxy.items() if key.startswith(("gpt-", "o1", "o3"))} | {
        key for key in model_proxy if key.startswith(("gpt-", "o1", "o3"))}
    if model not in legacy and not model.startswith(("g-", "gizmo")):
        catalog = await get_catalog()
        if not any(row["id"] == model and row.get("type") == "chat" for row in catalog["data"]):
            raise HTTPException(404, {"error": {"message": "Unknown model. Query /v1/models for available identifiers.",
                "type": "invalid_request_error", "param": "model", "code": "model_not_found"}})
    return {**data, "model": model}


async def process_web_images(data, token, lease=None):
    return await process(web_image_request(data), token, lease, transformer=format_web_images)


@app.post(f"/{api_prefix}/v1/images/generations" if api_prefix else "/v1/images/generations")
async def generate_images(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    supplied = credentials.credentials
    token = await api_key_only(supplied)
    try:
        raw = await request.body()
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(413, "Request body exceeds 2 MiB.")
        data = validate_image_request(await request.json())
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Invalid JSON body.") from None
    from chatgpt.native_images import process_native_images, native_images_enabled, image_error
    if data["model"] == "gpt-image-2" and not native_images_enabled():
        raise image_error(403, "unsupported_credentials", "原生 Image2 尚未启用，需要 Codex 授权；当前请使用 chatgpt-image-latest 网页生图通道。")
    token = await api_key_only(supplied, record_use=True)
    metrics = await start_request(supplied, data, "image")
    request.state.analytics_request_id = metrics.id
    data = {**data, "_request_metrics": metrics}
    handler = process_native_images if data["model"] == "gpt-image-2" else process_web_images
    try:
        if data["model"] == "gpt-image-2":
            # Codex quotas are separate from ChatGPT web balances. Never reserve
            # or debit web quota for an explicitly enabled native Codex request.
            service, result = await run_connected(request, handler(data, token))
        else:
            service, result = await run_connected(request, run_request(handler, {**data, "quota_feature": "image_gen"}, token))
    except ClientDisconnected:
        await metrics.finish("cancelled", 499, "client_disconnected")
        return Response(status_code=499)
    except asyncio.CancelledError:
        await metrics.finish("cancelled", 499, "client_cancelled")
        raise
    except HTTPException as exception:
        await metrics.finish("failed", exception.status_code, f"http_{exception.status_code}")
        raise
    except Exception:
        await metrics.finish("failed", 500, "request_failed")
        raise
    metrics.observe_result(result)
    await metrics.finish("succeeded", 200)
    return JSONResponse(result, headers={"X-Request-ID": metrics.id} if metrics.id else {})


@app.post(f"/{api_prefix}/v1/chat/completions" if api_prefix else "/v1/chat/completions")
async def send_conversation(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    supplied_token = credentials.credentials
    # Authenticate reserved-prefix tokens before parsing or forwarding anything.
    # Legacy direct upstream credentials retain their existing behavior.
    req_token = await pool_key(supplied_token) if supplied_token.startswith(MANAGED_PREFIX) else supplied_token
    try:
        request_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "Invalid JSON body"})
    if not isinstance(request_data, dict) or not isinstance(request_data.get("messages"), list) or not request_data["messages"]:
        raise HTTPException(400, "messages must be a non-empty array")
    if "model" in request_data and not isinstance(request_data["model"], str):
        raise HTTPException(400, "model must be a string")
    if any(not isinstance(message, dict) or message.get("role") not in ("system", "developer", "user", "assistant", "tool", "function")
           or not isinstance(message.get("content"), (str, list)) for message in request_data["messages"]):
        raise HTTPException(400, "Each message requires a valid role and string or array content")
    request_data = validate_chat_options(request_data)
    request_data = {**request_data, "messages": [
        {**message, "role": "system"} if message["role"] == "developer" else message for message in request_data["messages"]]}
    request_data = await validate_chat_model(request_data)
    if supplied_token.startswith(MANAGED_PREFIX):
        # Count once at the generating service, never at the public gateway.
        # Re-check active status atomically so concurrent revocation takes effect.
        req_token = await pool_key(supplied_token, record_use=True)
    metrics = await start_request(supplied_token, request_data, "chat")
    request.state.analytics_request_id = metrics.id
    request_data = {**request_data, "_request_metrics": metrics}
    try:
        chat_service, res = await run_request(process, request_data, req_token)
    except asyncio.CancelledError:
        await metrics.finish("cancelled", 499, "client_cancelled")
        raise
    except HTTPException as exception:
        await metrics.finish("failed", exception.status_code, f"http_{exception.status_code}")
        raise
    except Exception:
        await metrics.finish("failed", 500, "request_failed")
        raise
    try:
        if isinstance(res, types.AsyncGeneratorType):
            async def unconsumed():
                await finish_attempt(chat_service, "interrupted", "response_not_consumed")
                await metrics.finish("interrupted", 502, "response_not_consumed")
            background = BackgroundTask(unconsumed)
            stream = measured_stream(metrics, chat_service, res,
                include_usage=request_data.get("stream_options", {}).get("include_usage", False))
            return StreamingResponse(stream, media_type="text/event-stream", background=background,
                headers={"X-Request-ID": metrics.id} if metrics.id else {})
        else:
            metrics.observe_result(res)
            await metrics.finish("succeeded", 200)
            background = BackgroundTask(chat_service.close_client)
            return JSONResponse(res, media_type="application/json", background=background,
                headers={"X-Request-ID": metrics.id} if metrics.id else {})
    except HTTPException as e:
        await metrics.finish("failed", e.status_code, "response_failed")
        await chat_service.close_client()
        if e.status_code == 500:
            logger.error(f"Server error, {str(e)}")
            raise HTTPException(status_code=500, detail="Server error")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await metrics.finish("failed", 500, "response_failed")
        await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


def validate_chat_options(data):
    """OpenAI-compatible usage/length options that this bridge can honor."""
    if type(data.get("stream", False)) is not bool:
        raise HTTPException(400, "stream must be a boolean")
    options = data.get("stream_options", {})
    if not isinstance(options, dict) or set(options) - {"include_usage"} or type(options.get("include_usage", False)) is not bool:
        raise HTTPException(400, "stream_options must contain boolean include_usage only")
    if options and not data.get("stream", False):
        raise HTTPException(400, "stream_options requires stream=true")
    if "max_tokens" in data and "max_completion_tokens" in data:
        raise HTTPException(400, "Specify either max_tokens or max_completion_tokens")
    maximum = data.get("max_completion_tokens", data.get("max_tokens"))
    if maximum is not None and (type(maximum) is not int or not 1 <= maximum <= 1000000):
        raise HTTPException(400, "Token limit must be an integer from 1 to 1000000")
    # Do not silently claim multi-choice generation when only one is produced.
    if "n" in data and (type(data["n"]) is not int or data["n"] != 1):
        raise HTTPException(400, "This account-backed API supports n=1")
    for field in ("tools", "tool_choice", "functions", "function_call", "parallel_tool_calls"):
        if data.get(field):
            raise HTTPException(400, {"error": {"message": f"{field} is not supported by the ChatGPT web bridge.",
                "type": "invalid_request_error", "param": field, "code": "unsupported_parameter"}})
    if data.get("response_format") not in (None, {"type": "text"}):
        raise HTTPException(400, {"error": {"message": "Structured response_format is not supported by this bridge.",
            "type": "invalid_request_error", "param": "response_format", "code": "unsupported_parameter"}})
    return {**data, **({"max_tokens": maximum} if maximum is not None else {})}


@app.get(f"/{api_prefix}/tokens" if api_prefix else "/tokens", response_class=HTMLResponse)
async def upload_html(request: Request):
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return templates.TemplateResponse("tokens.html",
                                      {"request": request, "api_prefix": api_prefix, "tokens_count": tokens_count})


@app.post(f"/{api_prefix}/tokens/upload" if api_prefix else "/tokens/upload")
async def upload_post(text: str = Form(...)):
    lines = text.split("\n")
    for line in lines:
        if line.strip() and not line.startswith("#"):
            globals.token_list.append(line.strip())
            with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
                f.write(line.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/clear" if api_prefix else "/tokens/clear")
async def clear_tokens():
    globals.token_list.clear()
    globals.error_token_list.clear()
    with open(globals.TOKENS_FILE, "w", encoding="utf-8") as f:
        pass
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/error" if api_prefix else "/tokens/error")
async def error_tokens():
    error_tokens_list = list(set(globals.error_token_list))
    return {"status": "success", "error_tokens": error_tokens_list}


@app.get(f"/{api_prefix}/tokens/add/{{token}}" if api_prefix else "/tokens/add/{token}")
async def add_token(token: str):
    if token.strip() and not token.startswith("#"):
        globals.token_list.append(token.strip())
        with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
            f.write(token.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/seed_tokens/clear" if api_prefix else "/seed_tokens/clear")
async def clear_seed_tokens():
    globals.seed_map.clear()
    globals.conversation_map.clear()
    with open(globals.SEED_MAP_FILE, "w", encoding="utf-8") as f:
        f.write("{}")
    with open(globals.CONVERSATION_MAP_FILE, "w", encoding="utf-8") as f:
        f.write("{}")
    logger.info(f"Seed token count: {len(globals.seed_map)}")
    return {"status": "success", "seed_tokens_count": len(globals.seed_map)}
