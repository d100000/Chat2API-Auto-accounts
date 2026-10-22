"""Bounded, administrator-only exact-model checks against a selected account."""

import asyncio
import time
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app import app
from api.local_console import require_session
from chatgpt import account_quota, model_catalog
from chatgpt.account_scheduler import get_scheduler, NoAvailableAccount
from chatgpt.account_concurrency import ConcurrencyPersistenceError
from chatgpt.request_metrics import start_request
from utils.disconnect import ClientDisconnected, run_connected

CHECK_INTERVAL = 60
ATTEMPTS = {}
BUSY = set()


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str = Field(pattern=r"^acct_[0-9a-f]{24}$")
    model: Literal["gpt-6-luna"] = "gpt-6-luna"


def check_result(account_id, model):
    row = next((row for row in model_catalog.get_account_catalogs()["accounts"]
                if row["account_id"] == account_id), {})
    return {"account_id": account_id, "model": model,
            "check": row.get("checks", {}).get(model, {"status": "unverified"})}


@app.post("/local-console/accounts/models/check", include_in_schema=False)
async def check_model(request: Request, body: CheckBody):
    require_session(request)
    group = account_quota._default_service()._groups().get(body.account_id)
    if group is None:
        raise HTTPException(404, "账号不存在或已被移除。")
    now = time.monotonic()
    remaining = CHECK_INTERVAL - (now - ATTEMPTS.get(body.account_id, -CHECK_INTERVAL))
    if body.account_id in BUSY or remaining > 0:
        raise HTTPException(429, "此账号刚刚执行过模型核查，请稍后重试。",
                            headers={"Retry-After": str(max(1, int(remaining) + 1))})
    if not group.get("supported") or group["identity"].get("credential_status") in {"expired", "error"}:
        model_catalog.record_model_check(body.account_id, body.model, "error", error_code="credentials_unavailable")
        return check_result(body.account_id, body.model)
    ATTEMPTS[body.account_id] = now
    BUSY.add(body.account_id)
    data = {"model": body.model, "messages": [{"role": "user", "content": "Reply with exactly OK."}],
            "stream": False, "max_tokens": 8, "history_disabled": True, "quota_feature": "reason"}
    try:
        metrics = await start_request("direct", data, "chat")
    except BaseException:
        BUSY.discard(body.account_id)
        raise
    request.state.analytics_request_id = metrics.id
    data = {**data, "_request_metrics": metrics}
    scheduler = get_scheduler()
    lease = None
    outcome = "failed"
    try:
        try:
            # Deliberately bypass only directory membership, to determine
            # whether this exact slug is accepted. Capacity and quota remain
            # enforced, with no retries or automatic model/account fallback.
            lease = await scheduler.acquire("reason", preferred_account_id=body.account_id)
        except NoAvailableAccount as error:
            model_catalog.record_model_check(body.account_id, body.model, "error", error_code=error.code)
            await metrics.finish("failed", 429, error.code)
            return check_result(body.account_id, body.model)
        from api.chat2api import process
        service, result = await run_connected(request,
            asyncio.wait_for(process(data, group["token"], lease=lease), timeout=90))
        evidence = getattr(getattr(service, "usage_trace", None), "evidence", None)
        sent = getattr(evidence, "sent_model", None) or getattr(service, "req_model", None)
        observed = getattr(evidence, "upstream_model", None)
        model_catalog.record_model_check(body.account_id, body.model, "verified",
            sent_model=sent, reported_model=observed, http_status=200)
        metrics.observe_result(result)
        await metrics.finish("succeeded", 200)
        outcome = "succeeded"
        return check_result(body.account_id, body.model)
    except ClientDisconnected:
        outcome = "cancelled"
        await metrics.finish("cancelled", 499, "client_disconnected")
        return Response(status_code=499)
    except asyncio.CancelledError:
        outcome = "cancelled"
        await metrics.finish("cancelled", 499, "client_cancelled")
        raise
    except asyncio.TimeoutError:
        model_catalog.record_model_check(body.account_id, body.model, "error", error_code="check_timeout")
        await metrics.finish("failed", 504, "check_timeout")
        raise HTTPException(504, "模型核查超时，未重试或切换模型；上游结果未知。") from None
    except HTTPException as error:
        code = f"http_{error.status_code}"
        model_catalog.record_model_check(body.account_id, body.model,
            "denied" if error.status_code == 404 else "error",
            sent_model=body.model, http_status=error.status_code, error_code=code)
        await metrics.finish("failed", error.status_code, code)
        return check_result(body.account_id, body.model)
    except ConcurrencyPersistenceError:
        await metrics.finish("failed", 503, "concurrency_configuration_unavailable")
        raise HTTPException(503, "账号并发配置暂不可用。") from None
    except Exception:
        model_catalog.record_model_check(body.account_id, body.model, "error", error_code="check_failed")
        await metrics.finish("failed", 502, "check_failed")
        raise HTTPException(502, "模型核查未完成，未重试或切换模型。") from None
    finally:
        BUSY.discard(body.account_id)
        if lease is not None:
            await asyncio.shield(scheduler.finish(lease, status=outcome))
