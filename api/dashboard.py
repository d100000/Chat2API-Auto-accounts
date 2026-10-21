"""Authenticated local analytics and estimation settings; no generation calls."""

import asyncio
import logging
from typing import Literal

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import app, templates
from api.local_console import current_session, require_session
from chatgpt import analytics, pricing
from chatgpt.request_metrics import recording_status

LOG = logging.getLogger(__name__)


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page(request: Request):
    if not current_session(request):
        return RedirectResponse("/login?next=/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="local_dashboard.html", context={})


@app.get("/local-console/dashboard", include_in_schema=False)
async def dashboard_info(
    request: Request,
    days: int = Query(30, ge=7, le=90),
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    api_key_id: str | None = Query(None, min_length=1, max_length=160),
    model: str | None = Query(None, min_length=1, max_length=100),
    kind: Literal["chat", "image"] | None = None,
    status: Literal["succeeded", "failed", "cancelled", "interrupted", "in_progress"] | None = None,
    timezone: str = Query("Asia/Shanghai", min_length=1, max_length=64),
):
    require_session(request)
    try:
        if days not in (7, 30, 90):
            raise analytics.ValidationError("日期范围仅支持 7、30、90 天，或自定义日期。")
        def read_snapshot():
            return analytics.get_store().snapshot(days=days,
            date_from=date_from, date_to=date_to, api_key_id=api_key_id, model=model,
            kind=kind, status=status, timezone=timezone)
        snapshot = await asyncio.to_thread(read_snapshot)
        return {**snapshot, "recording_status": recording_status()}
    except analytics.ValidationError as error:
        raise HTTPException(422, str(error)) from None
    except Exception:
        LOG.error("Dashboard request ledger could not be read.")
        raise HTTPException(503, "用量记录暂时无法读取，请稍后重试。") from None


@app.get("/local-console/dashboard/pricing", include_in_schema=False)
async def dashboard_prices(request: Request):
    require_session(request)
    try:
        return await asyncio.to_thread(pricing.get_pricing_config)
    except pricing.PricingError as error:
        raise HTTPException(503, str(error)) from None


@app.put("/local-console/dashboard/pricing", include_in_schema=False)
async def dashboard_update_prices(request: Request, payload: dict = Body(...)):
    require_session(request)
    try:
        return await asyncio.to_thread(pricing.update_pricing_config, payload)
    except pricing.PricingError as error:
        raise HTTPException(422, str(error)) from None
