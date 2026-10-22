"""Local account policy editing; generation and secrets never leave this boundary."""

import asyncio
from typing import Annotated

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app import app
from api.local_console import RefreshAccountsBody, require_session, with_local_usage
from chatgpt import account_quota, model_catalog
from chatgpt.account_scheduler import get_scheduler
from chatgpt.account_concurrency import (
    ConcurrencyConflict, ConcurrencyPersistenceError, ConcurrencyValidationError,
)

Limit = Annotated[StrictInt, Field(ge=1, le=10)]
AccountID = Annotated[str, Field(pattern=r"^acct_[0-9a-f]{24}$")]


class Overrides(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Limit | None = None
    image_gen: Limit | None = None

    @model_validator(mode="after")
    def not_empty(self):
        if not self.model_fields_set:
            raise ValueError("请至少选择一种需要修改的能力。")
        return self


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Limit | None = None
    image_gen: Limit | None = None

    @model_validator(mode="after")
    def required_values(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("默认并发需填写 1–10 的整数。")
        return self


class RevisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Annotated[StrictInt, Field(ge=0)]


class DefaultsBody(RevisionBody):
    limits: Defaults


class OverrideBody(RevisionBody):
    overrides: Overrides


class BatchBody(OverrideBody):
    account_ids: list[AccountID] = Field(min_length=1, max_length=1000)


def policy_error(error):
    if isinstance(error, ConcurrencyConflict):
        return HTTPException(409, "配置已被其他操作更新，请重新载入后再保存。")
    if isinstance(error, ConcurrencyValidationError):
        return HTTPException(422, str(error))
    return HTTPException(503, "并发配置暂时无法保存或读取，请稍后重试。")


def current_ids():
    return {row["id"] for row in account_quota.get_accounts()["accounts"]}


@app.get("/local-console/accounts/concurrency", include_in_schema=False)
async def concurrency_info(request: Request):
    require_session(request)
    scheduler = get_scheduler()
    try:
        async with scheduler.admission_guard():
            # Reconcile credential aliases before exposing the editing revision.
            scheduler.synchronize_identities_locked()
            scheduler.snapshot(account_quota.get_accounts())
            return scheduler.concurrency.snapshot(current_ids())
    except (ConcurrencyValidationError, ConcurrencyConflict, ConcurrencyPersistenceError) as error:
        raise policy_error(error) from None


async def change_policy(request, body, *, account_ids=None, defaults=None):
    require_session(request)
    scheduler = get_scheduler()
    try:
        async with scheduler.admission_guard():
            scheduler.synchronize_identities_locked()
            scheduler.snapshot(account_quota.get_accounts())
            known = current_ids()
            selected = list(dict.fromkeys(account_ids or []))
            if selected and not set(selected) <= known:
                raise HTTPException(409, "选中的账号已被移除，请刷新列表后重试。")
            result = scheduler.concurrency.update(
                expected_revision=body.revision,
                defaults=defaults,
                account_ids=selected if account_ids is not None else None,
                overrides=body.overrides.model_dump(exclude_unset=True) if isinstance(body, OverrideBody) else None,
            )
            return result
    except (ConcurrencyValidationError, ConcurrencyConflict, ConcurrencyPersistenceError) as error:
        raise policy_error(error) from None


@app.patch("/local-console/accounts/concurrency/defaults", include_in_schema=False)
async def change_defaults(request: Request, body: DefaultsBody):
    return await change_policy(request, body, defaults=body.limits.model_dump(exclude_unset=True))


@app.patch("/local-console/accounts/concurrency/batch", include_in_schema=False)
async def change_batch(request: Request, body: BatchBody):
    return await change_policy(request, body, account_ids=body.account_ids)


@app.patch("/local-console/accounts/{account_id}/concurrency", include_in_schema=False)
async def change_account(request: Request, account_id: AccountID, body: OverrideBody):
    return await change_policy(request, body, account_ids=[account_id])


@app.post("/local-console/accounts/models/refresh", include_in_schema=False)
async def refresh_models(request: Request, body: RefreshAccountsBody):
    require_session(request)
    if body.account_id is not None and body.account_id not in current_ids():
        raise HTTPException(404, "账号已被移除，请刷新列表。")
    try:
        await model_catalog.refresh_catalog(account_id=body.account_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise HTTPException(503, "模型目录暂时无法刷新，已保留上次查询结果。") from None
    return await with_local_usage(account_quota.get_accounts())
