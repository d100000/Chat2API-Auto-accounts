"""Capacity-only admission for the inherited web conversation proxy."""

import asyncio
import json
import logging

from anyio import CancelScope
from fastapi import HTTPException

LOG = logging.getLogger(__name__)


class GatewayCapacity:
    def __init__(self, scheduler, lease):
        self.scheduler, self.lease = scheduler, lease
        self._cleanup = None
        self._close_callbacks = ()
        self._source = None

    def bind_cleanup(self, *callbacks):
        self._close_callbacks = (*self._close_callbacks, *callbacks)

    async def _finalize(self, status):
        try:
            close = getattr(self._source, "aclose", None)
            if close is not None:
                try:
                    await close()
                except (Exception, asyncio.CancelledError):
                    LOG.warning("Web gateway stream cleanup failed.")
            for callback in self._close_callbacks:
                try:
                    await callback()
                except (Exception, asyncio.CancelledError):
                    LOG.warning("Web gateway client cleanup failed.")
        finally:
            if self.lease is not None:
                await self.scheduler.finish(self.lease, status=status)

    async def finish(self, status="interrupted"):
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._finalize(status))
        interrupted = False
        with CancelScope(shield=True):
            while True:
                try:
                    await asyncio.shield(self._cleanup)
                    break
                except asyncio.CancelledError:
                    if self._cleanup.cancelled():
                        raise
                    interrupted = True
        if interrupted:
            raise asyncio.CancelledError

    def stream(self, source):
        # Register before the wrapper is first consumed: BackgroundTask must
        # also own cleanup when the response iterator never starts.
        self._source = source
        async def wrapped():
            status = "interrupted"
            try:
                async for chunk in source:
                    yield chunk
                status = "succeeded"
            except (asyncio.CancelledError, GeneratorExit):
                status = "cancelled"
                raise
            except Exception:
                status = "failed"
                raise
            finally:
                await self.finish(status)
        return wrapped()


async def admit_generation(request, path, token, body, *, access=None, account_override=None):
    if request.method != "POST" or path.rstrip("/") not in {
        "backend-api/conversation", "backend-alt/conversation", "/backend-api/conversation", "/backend-alt/conversation"
    }:
        return None
    if not token:
        raise HTTPException(401, "网页会话需要有效账号凭据。")
    from chatgpt.account_quota import _identity, account_identity
    from chatgpt.account_scheduler import get_scheduler, AccountConcurrencyExhausted
    from chatgpt.account_concurrency import ConcurrencyPersistenceError
    from chatgpt.scheduling import request_feature
    try:
        raw = json.loads(body)
        if not isinstance(raw, dict):
            raise ValueError
    except (ValueError, UnicodeError):
        raise HTTPException(400, "网页会话请求必须是 JSON 对象。") from None
    messages = []
    for row in raw.get("messages", []) if isinstance(raw.get("messages", []), list) else []:
        if not isinstance(row, dict):
            continue
        author, content = row.get("author", {}), row.get("content", {})
        role = author.get("role") if isinstance(author, dict) else None
        parts = content.get("parts", []) if isinstance(content, dict) else []
        text = " ".join(part for part in parts if isinstance(part, str)) if isinstance(parts, list) else ""
        messages.append({"role": role, "content": text})
    feature = request_feature({"model": raw.get("model", "auto"), "messages": messages})
    identity = _identity(access, override=account_override) if access else account_identity(token, account_override)
    scheduler = get_scheduler()
    try:
        lease = await scheduler.acquire_for_account(token, feature, account_id=identity["id"],
            account_override=account_override, quota_mode="none")
        return GatewayCapacity(scheduler, lease)
    except AccountConcurrencyExhausted as error:
        raise HTTPException(429, {"code": error.code, "message": "该账号并发已满，请等待当前请求完成。"},
                            headers={"Retry-After": str(error.retry_after_seconds)}) from None
    except ConcurrencyPersistenceError:
        raise HTTPException(503, "账号并发配置暂不可用，请稍后重试。") from None
