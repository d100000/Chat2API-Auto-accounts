"""Pool routing policy and bounded error signals, separate from upstream balances."""

import asyncio
from datetime import datetime, timezone
import os
import re
import time

from fastapi import HTTPException

FEATURES = frozenset(("reason", "image_gen"))
QUOTA_CODES = frozenset(("quota_exceeded", "usage_limit_reached", "message_limit_reached",
    "model_cap_exceeded", "model_limit_reached", "rate_limit_exceeded", "image_generation_limit_reached",
    "image_generation_limit_exceeded", "image_gen_limit_reached", "image_gen_limit_exceeded"))
IMAGE_CODES = frozenset(code for code in QUOTA_CODES if code.startswith("image_"))


def enabled():
    return os.environ.get("ACCOUNT_QUOTA_SCHEDULING", "true").lower() in ("true", "1", "yes")


def pool_request(token):
    from utils import configs
    return enabled() and configs.auto_seed and token in configs.authorization_list


def request_feature(data):
    explicit = data.get("quota_feature", "auto")
    if explicit not in ("auto", "reason", "image_gen"):
        raise HTTPException(400, "quota_feature must be auto, reason or image_gen")
    if explicit != "auto":
        return explicit
    model = str(data.get("model", "")).lower()
    choice = data.get("tool_choice")
    tool = choice.get("type") if isinstance(choice, dict) else choice
    if "image" in model or "dall-e" in model or tool in ("image_generation", "image_gen"):
        return "image_gen"
    # Only inspect the latest user instruction. Input images and mentions of
    # previous images alone do not imply a new image generation request.
    messages = data.get("messages", [])
    latest = next((row.get("content") for row in reversed(messages)
                   if isinstance(row, dict) and row.get("role") == "user"), "")
    if isinstance(latest, list):
        latest = " ".join(row.get("text", "") for row in latest
                          if isinstance(row, dict) and row.get("type") == "text" and isinstance(row.get("text"), str))
    text = latest[:32000] if isinstance(latest, str) else ""
    image_request = re.search(r"(?:生成|绘制|画出|制作|创建).{0,12}(?:图片|图像|插画|海报|照片)|(?:请|帮我)?画(?:一|个|张|幅)|生图|\b(?:draw|paint)\s+(?:me\s+)?(?:a|an|one|the)\b|\b(?:generate|create|make)\s+(?:me\s+)?(?:a|an|one|the)\s+(?:image|picture|photo|illustration|poster)\b", text, re.I)
    return "image_gen" if image_request else "reason"


def _reset(value, now):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and now < value < now + 366 * 86400:
        return float(value)
    if isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return _reset(parsed.timestamp(), now) if parsed.tzinfo else None
        except (ValueError, OverflowError, OSError):
            return None
    return None


def error_signal(value, status=None, feature="reason", now=None):
    """Extract only explicit, bounded machine-readable evidence; never error text."""
    now = time.time() if now is None else now
    rows = [value] if isinstance(value, dict) else []
    # Bounded nesting covers HTTP detail and SSE error envelopes.
    for _ in range(3):
        rows += [child for row in tuple(rows) for key in ("error", "detail")
                 if isinstance((child := row.get(key)), dict) and child not in rows]
    codes = {row.get(key) for row in rows for key in ("code", "type") if isinstance(row.get(key), str)}
    if codes & {"account_concurrency_exhausted", "concurrency_configuration_unavailable", "model_not_available", "account_pool_empty"}:
        # Local admission failures are not upstream quota/rate-limit evidence.
        return {"kind": "request"}
    explicit_feature = next((row.get(key) for row in rows for key in ("feature_name", "feature")
                             if isinstance(row.get(key), str) and row.get(key) in FEATURES), None)
    reset_at = next((stamp for row in rows for key in ("reset_at", "resets_at", "reset_after")
                     if (stamp := _reset(row.get(key), now)) is not None), None)
    clears = next((row.get("clears_in") for row in rows if isinstance(row.get("clears_in"), (int, float))
                   and not isinstance(row.get("clears_in"), bool) and 0 < row["clears_in"] <= 366 * 86400), None)
    if reset_at is None and clears is not None:
        reset_at = now + clears
    quota = bool(codes & (QUOTA_CODES - {"rate_limit_exceeded"})) or (status == 429 and explicit_feature is not None)
    if quota:
        return {"kind": "quota", "feature": "image_gen" if codes & IMAGE_CODES else explicit_feature or feature,
                "reset_at": reset_at}
    if status == 401:
        return {"kind": "auth"}
    if status == 429 or "rate_limit_exceeded" in codes:
        return {"kind": "rate_limit", "retry_after_seconds": min(300, clears) if clears is not None else 60}
    if status == 403:
        # Challenge/policy failures are not evidence of invalid credentials or
        # exhausted quota. Keep the account on a short circuit-breaker only.
        return {"kind": "transient", "retry_after_seconds": 60}
    if status is not None and status >= 500:
        return {"kind": "transient", "retry_after_seconds": 20}
    return {"kind": "request"}


def failure(service, error):
    trace = getattr(service, "usage_trace", None)
    evidence = getattr(trace, "evidence", None)
    feature = getattr(getattr(service, "schedule_lease", None), "feature", "reason")
    signal = getattr(trace, "scheduling_error", None) or error_signal(getattr(error, "detail", None), getattr(error, "status_code", None), feature)
    service.schedule_error = signal
    dispatched = getattr(service, "conversation_dispatched", False)
    accepted = getattr(service, "conversation_accepted", False)
    output = bool(evidence and (evidence.output_started or evidence.image_requested or evidence.generated_assets))
    status = getattr(error, "status_code", 500)
    # Accepted/ambiguous generation must never be replayed. A structured quota
    # error before any output is the only SSE exception to this rule.
    safe = not output and ((not dispatched and status in (401, 429, 500, 502, 503, 504)) or
                           (not accepted and getattr(service, "conversation_rejected", False) and status in (401, 429)) or
                           (signal["kind"] == "quota"))
    error.account_retry_safe = safe
    return error


async def run_request(process, data, token):
    feature = request_feature(data)
    from chatgpt.account_scheduler import (get_scheduler, NoAvailableAccount,
        AccountConcurrencyExhausted, ModelNotAvailable)
    from chatgpt.account_concurrency import ConcurrencyPersistenceError
    scheduler = get_scheduler()
    model = data.get("model")

    def admission_error(error):
        if isinstance(error, ModelNotAvailable):
            return HTTPException(503, {"code": error.code, "message": "当前没有已确认支持此模型的账号，请刷新模型目录或选择 auto。"})
        return HTTPException(429, {"code": error.code, "message": "当前账号并发已满，请等待正在运行的请求完成。"},
                             headers={"Retry-After": str(error.retry_after_seconds)})

    if not pool_request(token):
        # Resolve a Seed/legacy pool selector exactly once. Passing the selected
        # credential through a lease prevents ChatService from selecting again.
        from chatgpt.authorization import get_req_token
        selected = get_req_token(token)
        if not isinstance(selected, str) or not selected.strip():
            raise HTTPException(503, {"code": "account_pool_empty", "message": "没有可用账号凭据，请先导入有效账号。"})
        try:
            lease = await scheduler.acquire_for_account(selected, feature, model=model,
                account_override=data.get("Chatgpt-Account-Id"))
        except (AccountConcurrencyExhausted, ModelNotAvailable) as error:
            raise admission_error(error) from None
        except ConcurrencyPersistenceError:
            raise HTTPException(503, {"code": "concurrency_configuration_unavailable", "message": "账号并发配置暂不可用。"}) from None
        try:
            return await process(data, token, lease=lease)
        except BaseException as error:
            await asyncio.shield(scheduler.finish(lease, status="cancelled" if isinstance(error, asyncio.CancelledError) else "failed"))
            raise
    if any(data.get(key) is not None for key in ("conversation_id", "parent_message_id", "Chatgpt-Account-Id")):
        raise HTTPException(400, "账号池请求请传完整 messages；已有 conversation_id、parent_message_id 或工作区覆盖请使用该账号的直接凭据。")
    excluded = frozenset()
    try:
        maximum = max(1, min(10, int(os.environ.get("ACCOUNT_MAX_ATTEMPTS", "3"))))
    except ValueError:
        maximum = 3
    refreshed = False
    for attempt in range(maximum):
        try:
            lease = await scheduler.acquire(feature, excluded=excluded, model=model)
        except (AccountConcurrencyExhausted, ModelNotAvailable) as unavailable:
            raise admission_error(unavailable) from None
        except ConcurrencyPersistenceError:
            raise HTTPException(503, {"code": "concurrency_configuration_unavailable", "message": "账号并发配置暂不可用。"}) from None
        except NoAvailableAccount as unavailable:
            if not refreshed:
                await refresh_due(force=True)
                refreshed = True
                try:
                    lease = await scheduler.acquire(feature, excluded=excluded, model=model)
                except (AccountConcurrencyExhausted, ModelNotAvailable) as unavailable_after_refresh:
                    raise admission_error(unavailable_after_refresh) from None
                except NoAvailableAccount as unavailable_after_refresh:
                    unavailable = unavailable_after_refresh
                    lease = None
                if lease is not None:
                    pass
                else:
                    raise HTTPException(429, {"code": unavailable.code, "message": "当前没有满足额度条件的账号，请等待冷却或额度刷新后重试。"},
                                        headers={"Retry-After": str(max(1, unavailable.retry_after_seconds or 60))}) from None
            else:
                raise HTTPException(429, {"code": unavailable.code, "message": "当前没有满足额度条件的账号，请稍后重试。"},
                                    headers={"Retry-After": str(max(1, unavailable.retry_after_seconds or 60))}) from None
        excluded = excluded | {lease.account_id}
        try:
            return await process(data, token, lease=lease)
        except HTTPException as error:
            if not getattr(error, "account_retry_safe", False) or attempt + 1 >= maximum:
                if getattr(error, "safe_api_error", False):
                    raise
                raise HTTPException(error.status_code if error.status_code >= 400 else 502,
                                    {"code": "upstream_request_failed", "message": "上游请求未成功，请查看账号调度状态后重试。"}) from None
    raise HTTPException(503, "No available account")


async def refresh_due(force=False, interval=300):
    from chatgpt import account_quota
    from chatgpt.account_scheduler import get_scheduler
    snapshot = account_quota.get_accounts()
    await get_scheduler().synchronize_identities()
    routing = get_scheduler().snapshot(snapshot)
    now = time.time()
    selected = []
    for account in snapshot["accounts"]:
        refresh = account.get("refresh", {})
        if refresh.get("retry_after_seconds", 0) > 0 or refresh.get("status") == "in_progress":
            continue
        checked = account.get("checked_at")
        try:
            age = now - datetime.fromisoformat(checked).timestamp() if checked else float("inf")
        except (ValueError, TypeError):
            age = float("inf")
        resets = any(row.get("status") == "expired" for row in account.get("features", {}).values())
        needs_confirmation = any(routing["accounts"].get(account["id"], {}).get(feature, {}).get("state") in
                                 ("reset_pending", "auth_error") for feature in FEATURES)
        if force or age >= interval or resets or needs_confirmation:
            selected.append(account["id"])
    await asyncio.gather(*(account_quota.refresh(identifier) for identifier in selected))
    await get_scheduler().synchronize_identities()
    get_scheduler().snapshot(account_quota.get_accounts())
