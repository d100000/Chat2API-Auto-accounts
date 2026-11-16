"""Administrator model checks with isolated ASGI, synthetic accounts and no network.

The scheduler, model-evidence normalization and request ledger are real. Only
authentication, credential discovery and the generation boundary are replaced.
"""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException

from chatgpt.account_concurrency import ConcurrencyPersistenceError
from chatgpt.account_scheduler import AccountScheduler
from chatgpt.analytics import AnalyticsStore
from chatgpt.model_catalog import ModelCatalog
from chatgpt.request_metrics import RequestMetrics


ROOT = Path(__file__).resolve().parents[1]
PATH = "/local-console/accounts/models/check"
A, B, MISSING = ("acct_" + letter * 24 for letter in "abc")
MODEL = "gpt-6-luna"
PRIVATE_OUTPUT = "synthetic-private-generated-output"
PRIVATE_ERROR = "synthetic-private-upstream-error"


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class SyntheticQuota:
    def __init__(self):
        self.groups = {key: {"token": "synthetic-private-credential-" + key,
                             "supported": True, "identity": {"credential_status": "valid"}}
                       for key in (A, B)}
        self.rows = {key: {"id": key, "credential_status": "valid", "status": "fresh",
                          "checked_at": iso(999),
                          "features": {name: {"remaining": 10, "blocked": False,
                                               "reset_at": iso(2000), "status": "known"}
                                       for name in ("reason", "image_gen")},
                          "refresh": {"status": "refreshed", "last_attempt_at": iso(998), "error": None}}
                     for key in (A, B)}

    def _groups(self):
        return deepcopy(self.groups)

    def get_accounts(self):
        return {"accounts": list(deepcopy(self.rows).values())}


async def exchange(app, payload, *, authenticated=True, disconnected=None):
    """Deliver one HTTP body; the real disconnect watcher then awaits an event."""
    body = json.dumps(payload).encode()
    scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": PATH, "raw_path": PATH.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"content-type", b"application/json")] +
                        ([(b"x-test-admin", b"yes")] if authenticated else []),
             "server": ("127.0.0.1", 5005), "client": ("127.0.0.1", 12345),
             "asgi": {"version": "3.0", "spec_version": "2.4"}}
    messages, delivered = [], False
    disconnect = disconnected or asyncio.Event()

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), raw


class ModelCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="model-check-test-")
        self.addCleanup(self.directory.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = 1000.0
        self.clock = lambda: self.now
        self.quota = SyntheticQuota()
        tokens = {group["token"]: key for key, group in self.quota.groups.items()}
        self.fetch = AsyncMock(side_effect=AssertionError("Tests must never contact the model upstream"))
        self.catalog = ModelCatalog(lambda: tuple(tokens), fetcher=self.fetch,
                                    identity_resolver=tokens.__getitem__, cache_path=None, clock=self.clock)
        self.scheduler = AccountScheduler(self.quota, state_path=None, clock=self.clock,
                                          model_support=self.catalog.account_supports_model)
        self.acquire = self.stack.enter_context(patch.object(self.scheduler, "acquire", wraps=self.scheduler.acquire))
        self.finish = self.stack.enter_context(patch.object(self.scheduler, "finish", wraps=self.scheduler.finish))
        self.store = AnalyticsStore(Path(self.directory.name) / "requests.sqlite3", clock=self.clock)
        self.metrics = []

        application = ModuleType("app")
        application.app = FastAPI()
        console = ModuleType("api.local_console")

        def require_session(request):
            if request.headers.get("x-test-admin") != "yes":
                raise HTTPException(401, "Login required")

        console.require_session = Mock(side_effect=require_session)
        self.require_session = console.require_session
        spec = importlib.util.spec_from_file_location("_tested_model_checks", ROOT / "api/model_checks.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"app": application, "api.local_console": console}):
            spec.loader.exec_module(self.module)
        self.app = application.app
        # Replace route references only; do not alter the process-global clock or
        # use the default catalog, scheduler, credentials, pricing or ledger.
        self.module.time = SimpleNamespace(monotonic=self.clock)
        self.module.model_catalog = self.catalog
        self.module.account_quota = SimpleNamespace(_default_service=Mock(return_value=self.quota))
        self.module.get_scheduler = Mock(return_value=self.scheduler)
        self.module.start_request = AsyncMock(side_effect=self.start_metrics)
        self.process = AsyncMock(side_effect=self.success)
        generation = ModuleType("api.chat2api")
        generation.process = self.process
        self.stack.enter_context(patch.dict(sys.modules, {"api.chat2api": generation}))

    async def start_metrics(self, supplied_key, data, kind):
        self.assertEqual(supplied_key, "direct")
        identifier = self.store.start(key_id="direct", kind=kind, requested_model=data["model"])
        metric = RequestMetrics(self.store, identifier, kind=kind, model=data["model"],
                                pricing={"rates": [], "rate_version": "isolated-test"})
        self.metrics.append(metric)
        return metric

    async def success(self, data, token, *, lease):
        service = SimpleNamespace(req_model=data["model"], usage_trace=SimpleNamespace(
            evidence=SimpleNamespace(sent_model=data["model"], upstream_model=MODEL,
                                     generated_assets=frozenset(), image_requested=False)))
        data["_request_metrics"].attach(service)
        return service, {"model": MODEL, "choices": [{"message": {"content": PRIVATE_OUTPUT}}],
                         "usage": {"prompt_tokens": 7, "completion_tokens": 1}}

    async def request(self, key=A, **kwargs):
        return await exchange(self.app, {"account_id": key, "model": MODEL}, **kwargs)

    def recent(self):
        return self.store.snapshot()["recent"]

    def assert_released(self, status):
        self.assertFalse(self.module.BUSY)
        self.assertEqual(self.scheduler.snapshot()["accounts"][A]["active_requests"], 0)
        self.finish.assert_awaited_once()
        self.assertEqual(self.finish.await_args.kwargs["status"], status)

    async def test_requires_administrator_before_credential_or_generation_access(self):
        status, _, _ = await self.request(authenticated=False)
        self.assertEqual(status, 401)
        self.module.account_quota._default_service.assert_not_called()
        self.module.start_request.assert_not_awaited()
        self.acquire.assert_not_awaited()
        self.process.assert_not_awaited()

    async def test_only_exact_luna_and_bounded_server_prompt_are_accepted(self):
        for change in ({"model": "auto"}, {"model": "gpt-6"}, {"model": "gpt-6-luna-latest"},
                       {"model": None}, {"messages": [{"role": "user", "content": "user supplied"}]},
                       {"max_tokens": 1000}, {"stream": True}, {"account_id": "not-an-account"},
                       {"account_id": "acct_" + "d" * 25}):
            with self.subTest(change=change):
                status, _, _ = await exchange(self.app, {"account_id": A, "model": MODEL, **change})
                self.assertEqual(status, 422)
        self.module.start_request.assert_not_awaited()
        self.process.assert_not_awaited()
        status, _, raw = await exchange(self.app, {"account_id": A})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["model"], MODEL)

    async def test_unknown_account_is_rejected_without_metrics_or_generation(self):
        status, _, raw = await self.request(MISSING)
        self.assertEqual(status, 404)
        self.assertNotIn(b"synthetic-private", raw)
        self.module.start_request.assert_not_awaited()
        self.process.assert_not_awaited()
        self.assertFalse(self.module.BUSY)

    async def test_unusable_credentials_record_error_without_generation(self):
        for credential_status, supported in (("expired", True), ("error", True), ("valid", False)):
            with self.subTest(credential_status=credential_status, supported=supported):
                self.quota.groups[A] = {**self.quota.groups[A], "supported": supported,
                                        "identity": {"credential_status": credential_status}}
                status, _, raw = await self.request()
                self.assertEqual(status, 200)
                check = json.loads(raw)["check"]
                self.assertEqual(check["status"], "error")
                self.assertEqual(check["error_code"], "credentials_unavailable")
        self.module.start_request.assert_not_awaited()
        self.acquire.assert_not_awaited()
        self.process.assert_not_awaited()

    async def test_empty_directory_probe_is_exact_single_request_with_real_quota_and_capacity_lease(self):
        self.assertEqual(self.catalog.get_account_catalogs()["accounts"][0]["models"], [])

        async def inspect(data, token, *, lease):
            self.assertEqual({key: value for key, value in data.items() if key != "_request_metrics"},
                {"model": MODEL, "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                 "stream": False, "max_tokens": 8, "history_disabled": True, "quota_feature": "reason"})
            self.assertEqual(token, self.quota.groups[A]["token"])
            self.assertEqual(lease.account_id, A)
            self.assertEqual(lease.feature, "reason")
            self.assertEqual(lease.quota_features, ("reason",))
            row = self.scheduler.snapshot()["accounts"][A]
            self.assertEqual(row["concurrency"]["reason"]["active"], 1)
            self.assertEqual(row["reason"]["reserved"], 1)
            self.assertEqual(row["image_gen"]["reserved"], 0)
            return await self.success(data, token, lease=lease)

        self.process.side_effect = inspect
        status, _, raw = await self.request()
        self.assertEqual(status, 200)
        check = json.loads(raw)["check"]
        self.assertEqual(check["status"], "verified")
        self.assertEqual(check["sent_model"], MODEL)
        self.assertEqual(check["reported_model"], MODEL)
        self.process.assert_awaited_once()
        self.acquire.assert_awaited_once_with("reason", preferred_account_id=A)
        self.fetch.assert_not_awaited()
        self.assert_released("succeeded")
        row = self.recent()[0]
        self.assertEqual((row["status"], row["prompt_tokens"], row["completion_tokens"]), ("succeeded", 7, 1))
        self.assertEqual(row["key_id"], "direct")
        serialized = json.dumps(self.store.snapshot()).encode() + raw
        with self.store.connect() as db:
            serialized += "\n".join(db.iterdump()).encode()
        for private in (self.quota.groups[A]["token"], PRIVATE_OUTPUT, "Reply with exactly OK."):
            self.assertNotIn(private.encode(), serialized)
        self.assertFalse(self.metrics[0].services)

    async def test_success_with_different_or_missing_model_evidence_is_not_verified(self):
        for sent, reported in ((MODEL, "gpt-6"), ("auto", MODEL), (MODEL, None)):
            with self.subTest(sent=sent, reported=reported):
                self.now += 61

                async def mismatch(data, token, *, lease):
                    service, result = await self.success(data, token, lease=lease)
                    service.usage_trace.evidence.sent_model = sent
                    service.usage_trace.evidence.upstream_model = reported
                    # The echoed client envelope still says Luna; it must not
                    # be substituted for the upstream trace's model evidence.
                    return service, result

                self.process.side_effect = mismatch
                status, _, raw = await self.request()
                self.assertEqual(status, 200)
                check = json.loads(raw)["check"]
                self.assertEqual(check["status"], "model_mismatch")
                self.assertEqual(check["reported_model"], reported)
                self.assertEqual(check["error_code"], "model_mismatch")

    async def test_per_account_sixty_second_limit_and_other_account_is_independent(self):
        self.assertEqual((await self.request())[0], 200)
        self.assertEqual((await self.request(B))[0], 200)
        self.now += 59.9
        status, headers, _ = await self.request()
        self.assertEqual(status, 429)
        self.assertEqual(headers[b"retry-after"], b"1")
        self.assertEqual(self.process.await_count, 2)
        self.now = 1060.0
        self.assertEqual((await self.request())[0], 200)
        self.assertEqual(self.process.await_count, 3)

    async def test_busy_account_remains_single_flight_after_sixty_seconds(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def waiting(data, token, *, lease):
            entered.set()
            await release.wait()
            return await self.success(data, token, lease=lease)

        self.process.side_effect = waiting
        first = asyncio.create_task(self.request())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.now += 61
            self.assertEqual((await self.request())[0], 429)
            self.process.assert_awaited_once()
        finally:
            release.set()
            await first
        self.assert_released("succeeded")

    async def test_empty_quota_cannot_probe_or_fall_back_to_other_account(self):
        self.quota.rows[A]["features"]["reason"]["remaining"] = 0
        status, _, raw = await self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["check"]["status"], "error")
        self.assertEqual(self.recent()[0]["error_code"], "no_available_account")
        self.process.assert_not_awaited()
        self.finish.assert_not_awaited()
        self.assertFalse(self.module.BUSY)
        self.assertEqual(self.scheduler.snapshot()["accounts"][B]["active_requests"], 0)

    async def test_full_capacity_cannot_probe_or_fall_back_to_other_account(self):
        leases = [await self.scheduler.acquire("reason", preferred_account_id=A) for _ in range(3)]
        self.acquire.reset_mock()
        try:
            status, _, raw = await self.request()
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["check"]["status"], "error")
            self.assertEqual(self.recent()[0]["error_code"], "account_concurrency_exhausted")
            self.process.assert_not_awaited()
            self.acquire.assert_awaited_once_with("reason", preferred_account_id=A)
            self.assertEqual(self.scheduler.snapshot()["accounts"][A]["active_requests"], 3)
            self.assertEqual(self.scheduler.snapshot()["accounts"][B]["active_requests"], 0)
            self.assertFalse(self.module.BUSY)
        finally:
            for lease in leases:
                await self.scheduler.finish(lease, status="cancelled")

    async def test_http_not_found_is_denied_auth_and_quota_failures_are_errors_without_retry(self):
        for upstream_status, expected in ((404, "denied"), (403, "denied"), (401, "error"), (429, "error")):
            with self.subTest(upstream_status=upstream_status):
                self.now += 61
                self.process.reset_mock()
                self.finish.reset_mock()
                self.process.side_effect = HTTPException(upstream_status, PRIVATE_ERROR)
                status, _, raw = await self.request()
                self.assertEqual(status, 200)
                check = json.loads(raw)["check"]
                self.assertEqual(check["status"], expected)
                self.assertEqual(check["http_status"], upstream_status)
                self.assertEqual(check["sent_model"], MODEL)
                self.assertNotIn(PRIVATE_ERROR.encode(), raw)
                self.process.assert_awaited_once()
                self.assert_released("failed")
                row = self.recent()[0]
                self.assertEqual((row["status"], row["http_status"]), ("failed", upstream_status))
                self.assertEqual(row["error_code"], "http_" + str(upstream_status))
                self.assertNotIn(PRIVATE_ERROR, json.dumps(self.store.snapshot()))

    async def test_unexpected_generation_failure_releases_lease_and_redacts_error(self):
        self.process.side_effect = RuntimeError(PRIVATE_ERROR)
        status, _, raw = await self.request()
        self.assertEqual(status, 502)
        self.assertNotIn(PRIVATE_ERROR.encode(), raw)
        self.process.assert_awaited_once()
        self.assert_released("failed")
        self.assertEqual(self.catalog.get_account_catalogs()["accounts"][0]["checks"][MODEL]["status"], "error")
        self.assertEqual(self.recent()[0]["error_code"], "check_failed")

    async def test_unavailable_concurrency_policy_fails_closed_without_generation(self):
        self.acquire.side_effect = ConcurrencyPersistenceError(PRIVATE_ERROR)
        status, _, raw = await self.request()
        self.assertEqual(status, 503)
        self.assertNotIn(PRIVATE_ERROR.encode(), raw)
        self.process.assert_not_awaited()
        self.finish.assert_not_awaited()
        self.assertFalse(self.module.BUSY)
        self.assertEqual(self.recent()[0]["error_code"], "concurrency_configuration_unavailable")

    async def test_generation_timeout_is_bounded_and_cleans_up_without_retry(self):
        closed = asyncio.Event()

        async def waiting(*args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                closed.set()

        async def accelerated_timeout(operation, *, timeout):
            self.assertEqual(timeout, 90)
            return await asyncio.wait_for(operation, timeout=0.01)

        # Keep the production timeout contract while accelerating only this
        # route's timer; never patch the shared asyncio module or loop clock.
        self.module.asyncio = SimpleNamespace(wait_for=accelerated_timeout,
            CancelledError=asyncio.CancelledError, TimeoutError=asyncio.TimeoutError, shield=asyncio.shield)
        self.process.side_effect = waiting
        status, _, raw = await self.request()
        self.assertEqual(status, 504)
        self.assertTrue(closed.is_set())
        self.process.assert_awaited_once()
        self.assert_released("failed")
        self.assertEqual(self.recent()[0]["error_code"], "check_timeout")
        self.assertNotIn(PRIVATE_ERROR.encode(), raw)

    async def test_http_disconnect_cancels_generation_and_releases_lease(self):
        entered, disconnected, generation_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def waiting(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                generation_closed.set()

        self.process.side_effect = waiting
        pending = asyncio.create_task(self.request(disconnected=disconnected))
        await asyncio.wait_for(entered.wait(), 2)
        disconnected.set()
        status, _, raw = await asyncio.wait_for(pending, 2)
        self.assertEqual((status, raw), (499, b""))
        self.assertTrue(generation_closed.is_set())
        self.assert_released("cancelled")
        self.assertEqual(self.recent()[0]["error_code"], "client_disconnected")

    async def test_task_cancellation_during_generation_releases_lease_and_finishes_metrics(self):
        entered, closed = asyncio.Event(), asyncio.Event()

        async def waiting(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()

        self.process.side_effect = waiting
        pending = asyncio.create_task(self.request())
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(closed.is_set())
        self.assert_released("cancelled")
        self.assertEqual(self.recent()[0]["status"], "cancelled")
        self.assertEqual(self.recent()[0]["error_code"], "client_cancelled")

    async def test_cancellation_during_metrics_setup_does_not_leave_account_permanently_busy(self):
        entered = asyncio.Event()

        async def initializing(*args):
            entered.set()
            await asyncio.Future()

        self.module.start_request.side_effect = initializing
        pending = asyncio.create_task(self.request())
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(self.module.BUSY, "Cancellation before lease admission must still clear the check lock")
        self.acquire.assert_not_awaited()
        self.process.assert_not_awaited()


async def call(app, method, path, payload=None, *, authenticated=True):
    body = json.dumps(payload).encode() if payload is not None else b""
    scope = {"type": "http", "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"content-type", b"application/json")] +
                        ([(b"x-test-admin", b"yes")] if authenticated else []),
             "server": ("127.0.0.1", 5005), "client": ("127.0.0.1", 12345),
             "asgi": {"version": "3.0", "spec_version": "2.4"}}
    messages, delivered = [], False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return start["status"], json.loads(raw) if raw else None


PROBE = "/local-console/accounts/models/probe"


class ModelProbeTests(unittest.IsolatedAsyncioTestCase):
    """Background multi-model probing reuses the exact-check boundary."""

    start_metrics = ModelCheckTests.start_metrics
    success = ModelCheckTests.success
    request = ModelCheckTests.request
    recent = ModelCheckTests.recent

    def setUp(self):
        ModelCheckTests.setUp(self)
        self.module.PROBE_SPACING = 0
        self.reported = {}
        self.process.side_effect = self.echo

    async def echo(self, data, token, *, lease):
        service, result = await self.success(data, token, lease=lease)
        service.usage_trace.evidence.upstream_model = self.reported.get(data["model"], data["model"])
        return service, result

    async def probe(self, payload, **kwargs):
        status, body = await call(self.app, "POST", PROBE, payload, **kwargs)
        tasks = list(self.module.PROBES.values())
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        return status, body

    def account(self, key=A):
        return next(row for row in self.catalog.get_account_catalogs()["accounts"] if row["account_id"] == key)

    async def test_probe_requires_administrator_and_chat_model_ids(self):
        status, _ = await self.probe({"account_ids": [A]}, authenticated=False)
        self.assertEqual(status, 401)
        for models in (["gpt-image-2"], ["deep-research"], [], ["x" * 101]):
            with self.subTest(models=models):
                status, _ = await self.probe({"account_ids": [A], "models": models})
                self.assertEqual(status, 422)
        self.process.assert_not_awaited()

    async def test_probe_records_each_model_and_exposes_available_models(self):
        self.reported = {"gpt-5": "gpt-4o"}  # upstream silently substituted another model
        status, body = await self.probe({"account_ids": [A], "models": [MODEL, "gpt-5", "gpt-5-mini"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["started"], [A])
        self.assertEqual([call.args[0]["model"] for call in self.process.await_args_list],
                         [MODEL, "gpt-5", "gpt-5-mini"])
        row = self.account()
        self.assertEqual(row["available_models"], [MODEL, "gpt-5-mini"])
        self.assertEqual(row["unavailable_models"], ["gpt-5"])
        self.assertEqual(row["probe"]["status"], "completed")
        self.assertEqual(row["probe"]["done"], 3)
        self.assertEqual(row["probe"]["results"], {MODEL: "verified", "gpt-5": "model_mismatch",
                                                  "gpt-5-mini": "verified"})
        self.assertFalse(self.module.BUSY)
        self.assertFalse(self.module.PROBES)
        self.assertEqual(self.scheduler.snapshot()["accounts"][A]["active_requests"], 0)
        self.assertTrue(self.catalog.account_supports_model(A, "gpt-5-mini"))
        self.assertFalse(self.catalog.account_supports_model(A, "gpt-5"))
        self.assertFalse(self.catalog.account_supports_model(B, "gpt-5-mini"))

    async def test_probe_skips_conclusive_results_unless_forced(self):
        await self.probe({"account_ids": [A], "models": [MODEL]})
        self.process.reset_mock()
        await self.probe({"account_ids": [A], "models": [MODEL, "gpt-5"]})
        self.assertEqual([c.args[0]["model"] for c in self.process.await_args_list], ["gpt-5"])
        self.assertEqual(self.account()["probe"]["skipped"], [MODEL])
        self.process.reset_mock()
        await self.probe({"account_ids": [A], "models": [MODEL, "gpt-5"], "force": True})
        self.assertEqual(self.process.await_count, 2)

    async def test_account_level_failures_stop_the_probe(self):
        self.process.side_effect = HTTPException(429, PRIVATE_ERROR)
        await self.probe({"account_ids": [A], "models": [MODEL, "gpt-5"]})
        self.process.assert_awaited_once()
        probe = self.account()["probe"]
        self.assertEqual((probe["status"], probe["stop_reason"]), ("stopped", "rate_limit"))
        self.assertNotIn(PRIVATE_ERROR, json.dumps(probe))

    async def test_empty_quota_stops_without_generation_or_fallback(self):
        self.quota.rows[A]["features"]["reason"]["remaining"] = 0
        await self.probe({"account_ids": [A], "models": [MODEL, "gpt-5"]})
        self.process.assert_not_awaited()
        self.assertEqual(self.account()["probe"]["stop_reason"], "no_available_account")
        self.assertEqual(self.scheduler.snapshot()["accounts"][B]["active_requests"], 0)

    async def test_model_specific_failures_continue_with_next_model(self):
        async def failing(data, token, *, lease):
            if data["model"] == "gpt-5":
                raise HTTPException(404, PRIVATE_ERROR)
            return await self.echo(data, token, lease=lease)

        self.process.side_effect = failing
        await self.probe({"account_ids": [A], "models": ["gpt-5", MODEL]})
        row = self.account()
        self.assertEqual(row["probe"]["status"], "completed")
        self.assertEqual(row["available_models"], [MODEL])
        self.assertEqual(row["unavailable_models"], ["gpt-5"])

    async def test_busy_account_is_skipped_and_single_check_is_blocked_during_probe(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def waiting(data, token, *, lease):
            entered.set()
            await release.wait()
            return await self.echo(data, token, lease=lease)

        self.process.side_effect = waiting
        status, body = await call(self.app, "POST", PROBE, {"account_ids": [A], "models": [MODEL]})
        task = self.module.PROBES[A]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            _, again = await call(self.app, "POST", PROBE, {"account_ids": [A, B], "models": [MODEL]})
            self.assertEqual(again["skipped"], {A: "busy"})
            self.assertEqual((await self.request())[0], 429)
        finally:
            release.set()
            await asyncio.gather(task, *self.module.PROBES.values())
        self.assertFalse(self.module.BUSY)

    async def test_cancel_releases_lease_and_marks_cancelled(self):
        entered = asyncio.Event()

        async def waiting(*args, **kwargs):
            entered.set()
            await asyncio.Future()

        self.process.side_effect = waiting
        await call(self.app, "POST", PROBE, {"account_ids": [A], "models": [MODEL, "gpt-5"]})
        await asyncio.wait_for(entered.wait(), 2)
        status, body = await call(self.app, "POST", PROBE + "/cancel", {"account_ids": [A]})
        self.assertEqual((status, body["cancelled"]), (200, 1))
        self.assertEqual(self.account()["probe"]["status"], "cancelled")
        self.assertFalse(self.module.BUSY)
        self.assertEqual(self.scheduler.snapshot()["accounts"][A]["active_requests"], 0)
        self.assertEqual(self.recent()[0]["error_code"], "client_cancelled")

    async def test_default_candidates_include_other_accounts_directory(self):
        directory = {A: ["gpt-5"], B: ["gpt-5", "o3", "gpt-5-pro"]}
        tokens = {group["token"]: key for key, group in self.quota.groups.items()}
        async def fetch(token):
            return {"models": [{"slug": slug} for slug in directory[tokens[token]]]}

        self.fetch.side_effect = fetch
        await self.catalog.refresh_catalog(account_id=B)
        await self.probe({"account_ids": [A]})
        self.assertEqual([c.args[0]["model"] for c in self.process.await_args_list],
                         ["gpt-5", MODEL, "o3", "gpt-5-pro"])
        status, body = await call(self.app, "GET", PROBE)
        self.assertEqual(status, 200)
        self.assertEqual(body["accounts"][A]["available_models"], ["gpt-5", MODEL, "o3", "gpt-5-pro"])
        self.assertEqual(body["running"], [])

    async def test_forbidden_model_is_unusable_with_reason_and_probe_continues(self):
        async def forbidden(data, token, *, lease):
            if data["model"] == "gpt-5":
                raise HTTPException(403, PRIVATE_ERROR)
            if data["model"] == "o3":
                raise RuntimeError(PRIVATE_ERROR)
            return await self.echo(data, token, lease=lease)

        self.process.side_effect = forbidden
        await self.probe({"account_ids": [A], "models": ["gpt-5", "o3", MODEL]})
        row = self.account()
        self.assertEqual(row["probe"]["status"], "completed")
        self.assertEqual(row["unavailable_models"], ["gpt-5"])
        self.assertEqual(row["available_models"], [MODEL])
        self.assertEqual((row["checks"]["gpt-5"]["status"], row["checks"]["gpt-5"]["error_code"]), ("denied", "http_403"))
        self.assertEqual((row["checks"]["o3"]["status"], row["checks"]["o3"]["error_code"]), ("error", "check_failed"))
        self.assertNotIn(PRIVATE_ERROR, json.dumps(row))

    async def test_probe_state_survives_restart_and_active_job_becomes_interrupted(self):
        path = Path(self.directory.name) / "model_catalog.json"
        tokens = {group["token"]: key for key, group in self.quota.groups.items()}
        make = lambda: ModelCatalog(lambda: tuple(tokens), fetcher=self.fetch, identity_resolver=tokens.__getitem__,
                                    cache_path=path, clock=self.clock)
        first = make()
        first.record_model_check(A, "gpt-5", "denied", sent_model="gpt-5", http_status=403, error_code="http_403")
        first.set_probe_state(A, status="running", total=3, done=1, current="o3", models=["gpt-5", "o3", MODEL],
                              results={"gpt-5": "denied"}, private="must not persist")
        self.assertNotIn("must not persist", path.read_text(encoding="utf-8"))
        restarted = make().get_account_catalogs()["accounts"]
        row = next(item for item in restarted if item["account_id"] == A)
        self.assertEqual((row["probe"]["status"], row["probe"]["stop_reason"], row["probe"]["current"]),
                         ("stopped", "interrupted", None))
        self.assertEqual(row["probe"]["done"], 1)
        self.assertEqual(row["checks"]["gpt-5"]["error_code"], "http_403")
        self.assertEqual(row["unavailable_models"], ["gpt-5"])


if __name__ == "__main__":
    unittest.main()
