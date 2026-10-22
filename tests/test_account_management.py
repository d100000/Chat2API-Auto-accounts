"""Isolated ASGI tests for account admission policy management; no live state."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from chatgpt.account_concurrency import ConcurrencyPersistenceError, ConcurrencyStore

A, B, UNKNOWN = ("acct_" + character * 24 for character in "abc")


async def exchange(app, path, method="GET", payload=None, authenticated=True):
    body = json.dumps(payload).encode() if payload is not None else b""
    scope = {"type": "http", "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"content-type", b"application/json")] +
                        ([(b"x-test-admin", b"yes")] if authenticated else []),
             "server": ("127.0.0.1", 5005), "client": ("127.0.0.1", 12345),
             "asgi": {"version": "3.0", "spec_version": "2.4"}}
    messages = []
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], json.loads(raw)


class Scheduler:
    def __init__(self, path):
        self.concurrency = ConcurrencyStore(path)
        self.lock = asyncio.Lock()
        self.calls = []

    def admission_guard(self):
        return self.lock

    def synchronize_identities_locked(self):
        assert self.lock.locked()
        self.calls.append("sync")

    def snapshot(self, value):
        assert self.lock.locked()
        self.calls.append("snapshot")
        return value


class AccountManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="account-policy-api-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "policy.json"
        self.scheduler = Scheduler(self.path)
        application = ModuleType("app")
        application.app = FastAPI()
        console = ModuleType("api.local_console")
        class RefreshAccountsBody(BaseModel):
            account_id: str | None = Field(default=None, pattern=r"^acct_[0-9a-f]{24}$")
        console.RefreshAccountsBody = RefreshAccountsBody
        def require(request):
            if request.headers.get("x-test-admin") != "yes":
                raise HTTPException(401, "Login required")
        console.require_session = require
        console.with_local_usage = AsyncMock(side_effect=lambda value: value)
        spec = importlib.util.spec_from_file_location("_tested_account_management",
            Path(__file__).resolve().parents[1] / "api/account_management.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"app": application, "api.local_console": console}):
            spec.loader.exec_module(self.module)
        self.app = application.app
        self.quota = {"accounts": [{"id": A}, {"id": B}]}
        patches = [patch.object(self.module, "get_scheduler", return_value=self.scheduler),
                   patch.object(self.module.account_quota, "get_accounts", side_effect=lambda: self.quota),
                   patch.object(self.module.model_catalog, "refresh_catalog", new=AsyncMock())]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def get(self):
        return await exchange(self.app, "/local-console/accounts/concurrency")

    async def change(self, path, payload):
        return await exchange(self.app, path, "PATCH", payload)

    async def test_all_management_routes_require_admin_before_state_access(self):
        paths = [("/local-console/accounts/concurrency", "GET", None),
                 ("/local-console/accounts/concurrency/defaults", "PATCH", {"revision": 0, "limits": {"reason": 4}}),
                 ("/local-console/accounts/concurrency/batch", "PATCH", {"revision": 0, "account_ids": [A], "overrides": {"reason": 2}}),
                 (f"/local-console/accounts/{A}/concurrency", "PATCH", {"revision": 0, "overrides": {"reason": None}}),
                 ("/local-console/accounts/models/refresh", "POST", {})]
        for path, method, payload in paths:
            with self.subTest(path=path):
                self.assertEqual((await exchange(self.app, path, method, payload, authenticated=False))[0], 401)
        self.module.get_scheduler.assert_not_called()
        self.module.account_quota.get_accounts.assert_not_called()
        self.module.model_catalog.refresh_catalog.assert_not_called()
        self.assertFalse(self.path.exists())

    async def test_default_values_and_identity_sync_are_read_under_admission_guard(self):
        status, data = await self.get()
        self.assertEqual(status, 200)
        self.assertEqual(data["defaults"], {"reason": 3, "image_gen": 1})
        self.assertEqual(data["accounts"][A]["effective"], data["defaults"])
        self.assertEqual(self.scheduler.calls, ["sync", "snapshot"])
        self.assertFalse(self.scheduler.lock.locked())

    async def test_defaults_partial_patch_and_null_rejected(self):
        status, data = await self.change("/local-console/accounts/concurrency/defaults",
                                         {"revision": 0, "limits": {"reason": 7}})
        self.assertEqual(status, 200)
        self.assertEqual(data["defaults"], {"reason": 7, "image_gen": 1})
        self.assertEqual(data["revision"], 1)
        status, _ = await self.change("/local-console/accounts/concurrency/defaults",
                                      {"revision": 1, "limits": {"image_gen": None}})
        self.assertEqual(status, 422)
        self.assertEqual(self.scheduler.concurrency.snapshot()["revision"], 1)
        self.assertEqual(ConcurrencyStore(self.path).snapshot()["defaults"], data["defaults"])

    async def test_single_partial_override_and_explicit_null_restore_inheritance(self):
        path = f"/local-console/accounts/{A}/concurrency"
        status, data = await self.change(path, {"revision": 0, "overrides": {"reason": 2, "image_gen": 3}})
        self.assertEqual(status, 200)
        status, data = await self.change(path, {"revision": data["revision"], "overrides": {"reason": 5}})
        self.assertEqual(data["accounts"][A]["overrides"], {"reason": 5, "image_gen": 3})
        status, data = await self.change(path, {"revision": data["revision"], "overrides": {"reason": None}})
        self.assertEqual(status, 200)
        self.assertEqual(data["accounts"][A]["overrides"], {"reason": None, "image_gen": 3})
        self.assertEqual(data["accounts"][A]["effective"], {"reason": 3, "image_gen": 3})

    async def test_batch_deduplicates_and_updates_all_selected_accounts(self):
        status, data = await self.change("/local-console/accounts/concurrency/batch",
            {"revision": 0, "account_ids": [A, B, A], "overrides": {"image_gen": 4}})
        self.assertEqual(status, 200)
        self.assertEqual(data["revision"], 1)
        self.assertEqual(data["accounts"][A]["effective"], {"reason": 3, "image_gen": 4})
        self.assertEqual(data["accounts"][B]["effective"], {"reason": 3, "image_gen": 4})

    async def test_unknown_account_rejects_whole_batch_without_any_policy_change(self):
        before = self.scheduler.concurrency.snapshot((A, B))
        status, _ = await self.change("/local-console/accounts/concurrency/batch",
            {"revision": 0, "account_ids": [A, UNKNOWN], "overrides": {"reason": 9}})
        self.assertEqual(status, 409)
        self.assertEqual(self.scheduler.concurrency.snapshot((A, B)), before)
        self.assertFalse(self.path.exists())
        self.assertEqual((await self.change(f"/local-console/accounts/{UNKNOWN}/concurrency",
            {"revision": 0, "overrides": {"image_gen": 5}}))[0], 409)

    async def test_stale_revision_returns_409_without_overwriting_current_policy(self):
        await self.change(f"/local-console/accounts/{A}/concurrency", {"revision": 0, "overrides": {"reason": 2}})
        before = self.path.read_bytes()
        status, _ = await self.change("/local-console/accounts/concurrency/defaults",
                                     {"revision": 0, "limits": {"reason": 10}})
        self.assertEqual(status, 409)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.scheduler.concurrency.limits(A)["effective"]["reason"], 2)

    async def test_schema_rejects_wrong_types_bounds_unknown_fields_empty_and_missing_revision(self):
        path = f"/local-console/accounts/{A}/concurrency"
        payloads = [{"revision": 0, "overrides": {"reason": value}} for value in (True, "2", 1.0, 0, 11)]
        payloads += [{"revision": 0, "overrides": {}}, {"revision": 0, "overrides": {"unknown": 2}},
                     {"revision": 0, "overrides": {"reason": 2}, "token": "synthetic"},
                     {"overrides": {"reason": 2}}, {"revision": True, "overrides": {"reason": 2}},
                     {"revision": -1, "overrides": {"reason": 2}}]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual((await self.change(path, payload))[0], 422)
        for account_ids in ([], ["invalid"], [A] * 1001):
            self.assertEqual((await self.change("/local-console/accounts/concurrency/batch",
                {"revision": 0, "account_ids": account_ids, "overrides": {"reason": 2}}))[0], 422)
        for limits in ({}, {"reason": None}, {"image_gen": 0}, {"reason": "3"}):
            self.assertEqual((await self.change("/local-console/accounts/concurrency/defaults",
                {"revision": 0, "limits": limits}))[0], 422)
        self.assertFalse(self.path.exists())
        self.module.get_scheduler.assert_not_called()

    async def test_persistence_failure_returns_safe_503_and_keeps_previous_state(self):
        with patch.object(self.scheduler.concurrency, "_commit", side_effect=ConcurrencyPersistenceError("PRIVATE token path")):
            status, data = await self.change(f"/local-console/accounts/{A}/concurrency",
                {"revision": 0, "overrides": {"reason": 5}})
        self.assertEqual(status, 503)
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.assertEqual(self.scheduler.concurrency.snapshot()["revision"], 0)
        self.assertFalse(self.scheduler.lock.locked())

    async def test_explicit_model_refresh_is_admin_only_targeted_and_safe_on_failure(self):
        status, data = await exchange(self.app, "/local-console/accounts/models/refresh", "POST", {"account_id": A})
        self.assertEqual(status, 200)
        self.module.model_catalog.refresh_catalog.assert_awaited_once_with(account_id=A)
        self.assertEqual(data, self.quota)
        self.assertEqual((await exchange(self.app, "/local-console/accounts/models/refresh", "POST", {"account_id": UNKNOWN}))[0], 404)
        self.module.model_catalog.refresh_catalog.side_effect = RuntimeError("PRIVATE upstream body")
        status, data = await exchange(self.app, "/local-console/accounts/models/refresh", "POST", {})
        self.assertEqual(status, 503)
        self.assertNotIn("PRIVATE", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
