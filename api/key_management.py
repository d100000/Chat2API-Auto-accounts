"""Local, authenticated lifecycle management for client API keys."""

import asyncio
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import app
from api.local_console import require_session
from chatgpt.api_keys import APIKeyStore, KeyNotFound, KeyRevoked, KeyStoreError, system_key_info, valid_name
from utils import configs


STORE = APIKeyStore()


class CreateKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def check_name(cls, value):
        return valid_name(value)


class KeyStatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "disabled"]


async def store_call(function, *args):
    try:
        return await asyncio.to_thread(function, *args)
    except KeyNotFound as exception:
        raise HTTPException(404, str(exception)) from None
    except KeyRevoked as exception:
        raise HTTPException(409, str(exception)) from None
    except KeyStoreError as exception:
        raise HTTPException(503, str(exception)) from None


@app.get("/local-console/api-keys", include_in_schema=False)
async def list_api_keys(request: Request):
    require_session(request)
    return {"keys": await store_call(STORE.list_keys), "system_key": system_key_info(configs.authorization_list)}


@app.post("/local-console/api-keys", include_in_schema=False)
async def create_api_key(body: CreateKeyBody, request: Request):
    require_session(request)
    if not configs.authorization_list:
        raise HTTPException(503, "请先配置 .env 中的 AUTHORIZATION 系统共享密钥。")
    return await store_call(STORE.create, body.name)


@app.patch("/local-console/api-keys/{identifier}", include_in_schema=False)
async def update_api_key(identifier: str, body: KeyStatusBody, request: Request):
    require_session(request)
    return {"key": await store_call(STORE.set_status, identifier, body.status)}


@app.delete("/local-console/api-keys/{identifier}", include_in_schema=False)
async def revoke_api_key(identifier: str, request: Request):
    require_session(request)
    return {"key": await store_call(STORE.set_status, identifier, "revoked")}
