"""Bounded, administrator-only exact-model checks against a selected account.

``/check`` verifies one model on demand. ``/probe`` runs the same exact check
sequentially over candidate models in the background, so each account learns
which models it can really use. Results land in the model catalogue, which the
scheduler already consults for model-aware routing.
"""

import asyncio
import time
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app import app
from api.local_console import require_session
from chatgpt import account_quota, model_catalog
from chatgpt.account_scheduler import get_scheduler, NoAvailableAccount
from chatgpt.account_concurrency import ConcurrencyPersistenceError
from chatgpt.model_catalog import is_chat_model_id
from chatgpt.request_metrics import start_request
from utils.disconnect import ClientDisconnected, run_connected

CHECK_INTERVAL = 60
ATTEMPTS = {}
BUSY = set()

MAX_PROBE_MODELS = 24
PROBE_SPACING = 3.0
PROBE_ACCOUNT_CONCURRENCY = 2
# Upstream answers that concern the account, not the model: stop the job
# instead of spending more quota on a request that cannot succeed.
PROBE_STOP_HTTP = {401: "authentication_error", 429: "rate_limit"}
PROBES = {}
_probe_slots = None

AccountID = Annotated[str, Field(pattern=r"^acct_[0-9a-f]{24}$")]


def _chat_model(value):
    if not is_chat_model_id(value):
        raise ValueError("仅支持 ChatGPT 聊天模型标识。")
    return value


ModelID = Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_chat_model)]


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: AccountID
    model: Literal["gpt-6-luna"] = "gpt-6-luna"


class ProbeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_ids: list[AccountID] = Field(min_length=1, max_length=200)
    models: list[ModelID] | None = Field(default=None, min_length=1, max_length=MAX_PROBE_MODELS)
    force: bool = False


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_ids: list[AccountID] = Field(min_length=1, max_length=200)


def check_result(account_id, model):
    row = next((row for row in model_catalog.get_account_catalogs()["accounts"]
                if row["account_id"] == account_id), {})
    return {"account_id": account_id, "model": model,
            "check": row.get("checks", {}).get(model, {"status": "unverified"})}


def _usable_credentials(group):
    return bool(group.get("supported")) and group["identity"].get("credential_status") not in {"expired", "error"}


async def _execute_check(account_id, group, model, request=None):
    """Run one exact-model request and record its outcome.

    Returns ``None`` or the scheduler's refusal code (quota/capacity, nothing
    was sent). Returns a 499 ``Response`` when the HTTP client disconnected.
    Raises ``HTTPException`` 502/503/504 for unrecorded or unknown outcomes.
    """
    data = {"model": model, "messages": [{"role": "user", "content": "Reply with exactly OK."}],
            "stream": False, "max_tokens": 8, "history_disabled": True, "quota_feature": "reason"}
    metrics = await start_request("direct", data, "chat")
    if request is not None:
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
            lease = await scheduler.acquire("reason", preferred_account_id=account_id)
        except NoAvailableAccount as error:
            model_catalog.record_model_check(account_id, model, "error", error_code=error.code)
            await metrics.finish("failed", 429, error.code)
            return error.code
        from api.chat2api import process
        operation = asyncio.wait_for(process(data, group["token"], lease=lease), timeout=90)
        service, result = await (run_connected(request, operation) if request is not None else operation)
        evidence = getattr(getattr(service, "usage_trace", None), "evidence", None)
        sent = getattr(evidence, "sent_model", None) or getattr(service, "req_model", None)
        observed = getattr(evidence, "upstream_model", None)
        model_catalog.record_model_check(account_id, model, "verified",
            sent_model=sent, reported_model=observed, http_status=200)
        metrics.observe_result(result)
        await metrics.finish("succeeded", 200)
        outcome = "succeeded"
        return None
    except ClientDisconnected:
        outcome = "cancelled"
        await metrics.finish("cancelled", 499, "client_disconnected")
        return Response(status_code=499)
    except asyncio.CancelledError:
        outcome = "cancelled"
        await metrics.finish("cancelled", 499, "client_cancelled")
        raise
    except asyncio.TimeoutError:
        model_catalog.record_model_check(account_id, model, "error", error_code="check_timeout")
        await metrics.finish("failed", 504, "check_timeout")
        raise HTTPException(504, "模型核查超时，未重试或切换模型；上游结果未知。") from None
    except HTTPException as error:
        code = f"http_{error.status_code}"
        # 403/404 on an otherwise working account: this model is not granted.
        model_catalog.record_model_check(account_id, model,
            "denied" if error.status_code in {403, 404} else "error",
            sent_model=model, http_status=error.status_code, error_code=code)
        await metrics.finish("failed", error.status_code, code)
        return None
    except ConcurrencyPersistenceError:
        await metrics.finish("failed", 503, "concurrency_configuration_unavailable")
        raise HTTPException(503, "账号并发配置暂不可用。") from None
    except Exception:
        model_catalog.record_model_check(account_id, model, "error", error_code="check_failed")
        await metrics.finish("failed", 502, "check_failed")
        raise HTTPException(502, "模型核查未完成，未重试或切换模型。") from None
    finally:
        if lease is not None:
            await asyncio.shield(scheduler.finish(lease, status=outcome))


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
    if not _usable_credentials(group):
        model_catalog.record_model_check(body.account_id, body.model, "error", error_code="credentials_unavailable")
        return check_result(body.account_id, body.model)
    ATTEMPTS[body.account_id] = now
    BUSY.add(body.account_id)
    try:
        outcome = await _execute_check(body.account_id, group, body.model, request)
        if isinstance(outcome, Response):
            return outcome
        return check_result(body.account_id, body.model)
    finally:
        BUSY.discard(body.account_id)


# --- Background per-account probing -------------------------------------------------

def _iso_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _slots():
    global _probe_slots
    if _probe_slots is None:
        _probe_slots = asyncio.Semaphore(PROBE_ACCOUNT_CONCURRENCY)
    return _probe_slots


def _terminal(check, force):
    """A current, conclusive result that a non-forced probe may skip."""
    return (not force and isinstance(check, dict) and not check.get("stale")
            and check.get("status") in {"verified", "denied", "model_mismatch"})


def _account_checks(account_id):
    row = next((row for row in model_catalog.get_account_catalogs()["accounts"]
                if row["account_id"] == account_id), {})
    return row.get("checks", {})


async def _probe_account(account_id, requested, force):
    state = {"status": "stopped", "stop_reason": None}
    try:
        async with _slots():
            model_catalog.set_probe_state(account_id, status="discovering", started_at=_iso_now())
            if requested is None:
                try:
                    # Directory lookup is metadata only; it respects its own retry spacing.
                    await model_catalog.refresh_catalog(account_id=account_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # Probe the cross-account candidates anyway.
                models = model_catalog.probe_candidates(account_id, MAX_PROBE_MODELS)
            else:
                models = list(dict.fromkeys(requested))
            checks = _account_checks(account_id)
            pending = [model for model in models if not _terminal(checks.get(model), force)]
            model_catalog.set_probe_state(account_id, status="running", models=pending,
                                          skipped=[model for model in models if model not in pending],
                                          total=len(pending), done=0, current=None, results={})
            results = {}
            for index, model in enumerate(pending):
                if index and PROBE_SPACING > 0:
                    await asyncio.sleep(PROBE_SPACING)
                group = account_quota._default_service()._groups().get(account_id)
                if group is None:
                    state["stop_reason"] = "account_removed"
                    return
                if not _usable_credentials(group):
                    model_catalog.record_model_check(account_id, model, "error", error_code="credentials_unavailable")
                    state["stop_reason"] = "credentials_unavailable"
                    return
                model_catalog.set_probe_state(account_id, current=model)
                try:
                    refusal = await _execute_check(account_id, group, model)
                except HTTPException as error:
                    if error.status_code == 503:
                        state["stop_reason"] = "concurrency_configuration_unavailable"
                        return
                    refusal = None  # Timeout/unknown failure is recorded; try the next model.
                check = _account_checks(account_id).get(model, {})
                results = {**results, model: check.get("status", "error")}
                model_catalog.set_probe_state(account_id, done=index + 1, current=None, results=results)
                if isinstance(refusal, str):
                    state["stop_reason"] = refusal
                    return
                stop = PROBE_STOP_HTTP.get(check.get("http_status"))
                if stop:
                    state["stop_reason"] = stop
                    return
            state["status"] = "completed"
    except asyncio.CancelledError:
        state = {"status": "cancelled", "stop_reason": "cancelled"}
        raise
    except Exception:
        state = {"status": "stopped", "stop_reason": "probe_failed"}
    finally:
        BUSY.discard(account_id)
        ATTEMPTS[account_id] = time.monotonic()
        PROBES.pop(account_id, None)
        model_catalog.set_probe_state(account_id, **state, current=None, finished_at=_iso_now())


def _probe_summary(account_ids):
    return {key: model_catalog.get_probe_state(key) for key in account_ids}


@app.post("/local-console/accounts/models/probe", include_in_schema=False)
async def start_probe(request: Request, body: ProbeBody):
    """Start background probes; each model costs one minimal chat request."""
    require_session(request)
    groups = account_quota._default_service()._groups()
    started, skipped = [], {}
    for account_id in dict.fromkeys(body.account_ids):
        group = groups.get(account_id)
        if group is None:
            skipped[account_id] = "account_removed"
        elif account_id in BUSY or account_id in PROBES:
            skipped[account_id] = "busy"
        elif not _usable_credentials(group):
            skipped[account_id] = "credentials_unavailable"
        else:
            BUSY.add(account_id)
            model_catalog.set_probe_state(account_id, status="queued", queued_at=_iso_now(), started_at=None,
                                          finished_at=None, stop_reason=None, models=[], skipped=[],
                                          total=0, done=0, current=None, results={})
            PROBES[account_id] = asyncio.create_task(_probe_account(account_id, body.models, body.force))
            started.append(account_id)
    if not started and len(skipped) == 1 and next(iter(skipped.values())) == "account_removed":
        raise HTTPException(404, "账号不存在或已被移除。")
    return {"started": started, "skipped": skipped, "probes": _probe_summary(body.account_ids)}


@app.get("/local-console/accounts/models/probe", include_in_schema=False)
async def probe_status(request: Request):
    require_session(request)
    rows = model_catalog.get_account_catalogs()["accounts"]
    return {"running": sorted(PROBES), "accounts": {row["account_id"]: {
        "probe": row.get("probe"), "available_models": row.get("available_models", []),
        "unavailable_models": row.get("unavailable_models", [])} for row in rows}}


@app.post("/local-console/accounts/models/probe/cancel", include_in_schema=False)
async def cancel_probe(request: Request, body: CancelBody):
    require_session(request)
    tasks = [PROBES[key] for key in dict.fromkeys(body.account_ids) if key in PROBES]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return {"cancelled": len(tasks), "probes": _probe_summary(body.account_ids)}
