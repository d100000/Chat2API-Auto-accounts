import asyncio
import hmac
import types

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Request, HTTPException, Form, Security
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from starlette.background import BackgroundTask

import utils.globals as globals
from app import app, templates, security_scheme
from chatgpt.ChatService import ChatService
from chatgpt.authorization import refresh_all_tokens
from chatgpt.account_usage import start_trace
from chatgpt.api_keys import MANAGED_PREFIX, ManagedKeyError, resolve_managed_key
from chatgpt.scheduling import run_request, failure
from utils.Logger import logger
from utils.configs import api_prefix, scheduled_refresh, authorization_list

scheduler = AsyncIOScheduler()


@app.on_event("startup")
async def app_start():
    if scheduled_refresh:
        scheduler.add_job(id='refresh', func=refresh_all_tokens, trigger='cron', hour=3, minute=0, day='*/2',
                          kwargs={'force_refresh': True})
        scheduler.start()
        asyncio.get_event_loop().call_later(0, lambda: asyncio.create_task(refresh_all_tokens(force_refresh=False)))


async def to_send_conversation(request_data, req_token, lease=None):
    chat_service = ChatService(req_token, selected_token=lease.token) if lease else ChatService(req_token)
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
        await finish_attempt(chat_service, "failed", f"http_{e.status_code}")
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


async def process(request_data, req_token, lease=None):
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
        await finish_attempt(chat_service, "succeeded")
        return chat_service, res
    except asyncio.CancelledError:
        await asyncio.shield(finish_attempt(chat_service, "cancelled", "client_cancelled"))
        raise
    except HTTPException as e:
        failure(chat_service, e)
        await finish_attempt(chat_service, "failed", f"http_{e.status_code}")
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
    if not authorization_list:
        raise HTTPException(503, "尚未配置系统共享密钥。")
    token = credentials.credentials
    if token.startswith(MANAGED_PREFIX):
        await pool_key(token)
    elif not token.isascii() or not any(hmac.compare_digest(token, key) for key in authorization_list):
        raise HTTPException(401, "API 密钥无效。", headers={"WWW-Authenticate": "Bearer"})
    return {"object": "list", "data": [{"id": "auto", "object": "model", "created": 0,
        "owned_by": "chat2api", "description": "Supported default: account-selected upstream model"}]}


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
    if any(not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant", "tool", "function")
           or not isinstance(message.get("content"), (str, list)) for message in request_data["messages"]):
        raise HTTPException(400, "Each message requires a valid role and string or array content")
    if supplied_token.startswith(MANAGED_PREFIX):
        # Count once at the generating service, never at the public gateway.
        # Re-check active status atomically so concurrent revocation takes effect.
        req_token = await pool_key(supplied_token, record_use=True)
    chat_service, res = await run_request(process, request_data, req_token)
    try:
        if isinstance(res, types.AsyncGeneratorType):
            background = BackgroundTask(finish_attempt, chat_service, "interrupted", "response_not_consumed")
            return StreamingResponse(res, media_type="text/event-stream", background=background)
        else:
            background = BackgroundTask(chat_service.close_client)
            return JSONResponse(res, media_type="application/json", background=background)
    except HTTPException as e:
        await chat_service.close_client()
        if e.status_code == 500:
            logger.error(f"Server error, {str(e)}")
            raise HTTPException(status_code=500, detail="Server error")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


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
