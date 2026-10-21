import asyncio
import ast
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from chatgpt.api_keys import APIKeyStore, usage_key_id
from chatgpt.request_metrics import RequestMetrics, measured_stream, start_request


class Store:
    def __init__(self): self.rows = []
    def finish(self, identifier, **values): self.rows.append((identifier, values))


def service(complete=True, *, images=()):
    return SimpleNamespace(prompt_tokens=12, resp_model="gpt-6",
        usage_trace=SimpleNamespace(complete=complete,
            evidence=SimpleNamespace(generated_assets=frozenset(images), image_requested=bool(images), upstream_model="gpt-6")))


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        self.metrics = RequestMetrics(self.store, "req-test", model="gpt-6", pricing={})
        self.price_patch = patch("chatgpt.pricing.estimate", return_value={
            "estimated_cost_usd": .0001, "estimated_text_cost_usd": .0001,
            "estimated_image_cost_usd": None, "price_source": "custom", "pricing_status": "known", "rate_version": "v1"})
        self.price_patch.start(); self.addCleanup(self.price_patch.stop)

    async def test_account_retries_are_one_client_request_and_no_content_is_persisted(self):
        self.metrics.attach(service(False))
        self.metrics.attach(service())
        self.metrics.observe_result({"usage": {"prompt_tokens": 15, "completion_tokens": 9}, "choices": [{"text": "private-output"}]})
        await self.metrics.finish("succeeded", 200)
        await self.metrics.finish("cancelled", 499)
        self.assertEqual(len(self.store.rows), 1)
        row = self.store.rows[0][1]
        self.assertEqual(row["prompt_tokens"], 15)
        self.assertEqual(row["completion_tokens"], 9)
        self.assertEqual(row["estimated_cost_usd"], .0001)
        self.assertEqual(row["rate_version"], "v1")
        self.assertNotIn("private-output", json.dumps(self.store.rows))

    async def test_stream_options_usage_has_same_completion_id_and_precedes_done(self):
        selected = service()
        self.metrics.attach(selected)
        async def chunks():
            yield 'data: {"id":"chatcmpl-one","created":123,"model":"gpt-6","choices":[{"delta":{"content":"Hello"}}]}\n\n'
            yield 'data: [DONE]\n\n'
        output = [value async for value in measured_stream(self.metrics, selected, chunks(), include_usage=True)]
        usage = json.loads(output[-2][6:])
        self.assertEqual(usage["choices"], [])
        self.assertEqual(usage["id"], "chatcmpl-one")
        self.assertEqual(usage["created"], 123)
        self.assertEqual(usage["usage"]["prompt_tokens"], 12)
        self.assertGreater(usage["usage"]["completion_tokens"], 0)
        self.assertEqual(output[-1], 'data: [DONE]\n\n')
        self.assertEqual(self.store.rows[0][1]["status"], "succeeded")

    async def test_client_closing_at_done_does_not_turn_completed_generation_into_cancelled(self):
        selected = service(); self.metrics.attach(selected)
        async def chunks():
            yield 'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
        stream = measured_stream(self.metrics, selected, chunks())
        await anext(stream)
        self.assertIn("[DONE]", await anext(stream))
        await stream.aclose()
        self.assertEqual(self.store.rows[0][1]["status"], "succeeded")

    async def test_images_are_separate_from_text_request_counts(self):
        self.metrics.attach(service(images=("asset-1", "asset-2")))
        self.metrics.observe_result({"usage": {"prompt_tokens": 12, "completion_tokens": 2}})
        await self.metrics.finish("succeeded", 200)
        self.assertEqual(self.store.rows[0][1]["image_requests"], 1)
        self.assertEqual(self.store.rows[0][1]["generated_images"], 2)

    async def test_stream_failure_is_counted_once_and_has_no_success_usage_chunk(self):
        selected = service(False); self.metrics.attach(selected)
        async def chunks():
            yield 'data: {"error":{"message":"failed"}}\n\n'
            yield 'data: [DONE]\n\n'
        output = [value async for value in measured_stream(self.metrics, selected, chunks(), include_usage=True)]
        self.assertFalse(any('"usage"' in value for value in output))
        self.assertEqual(self.store.rows[0][1]["status"], "failed")

    async def test_wrapper_close_always_awaits_account_source_cleanup(self):
        cleaned = asyncio.Event()
        selected = service(False); self.metrics.attach(selected)
        async def source():
            try:
                yield 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
        stream = measured_stream(self.metrics, selected, source())
        await anext(stream)
        await stream.aclose()
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.store.rows[0][1]["status"], "cancelled")

    async def test_native_usage_keeps_upstream_provenance_and_image_count(self):
        self.metrics.kind = "image"
        self.metrics.attach(service(images=("native-image-0",)))
        self.metrics.observe_result({"usage": {"input_tokens": 100, "output_tokens": 200}})
        await self.metrics.finish("succeeded", 200)
        values = self.store.rows[0][1]
        self.assertEqual(values["token_source"], "upstream_reported")
        self.assertEqual(values["completion_tokens"], 200)
        self.assertEqual(values["generated_images"], 1)

    async def test_explicit_auto_tariff_is_not_replaced_by_upstream_slug(self):
        self.metrics.model = "auto"
        self.metrics.pricing = {"rates": [{"model": "auto", "kind": "text"}]}
        self.metrics.attach(service())
        with patch("chatgpt.pricing.estimate", return_value={"pricing_status": "unknown"}) as estimate:
            await self.metrics.finish("succeeded", 200)
        self.assertEqual(estimate.call_args.args[0], "auto")

    async def test_cancel_during_pricing_setup_finalizes_already_created_ledger_row(self):
        import sys
        entered, release = threading.Event(), threading.Event()
        store = Store()
        store.start = lambda **kwargs: "req-test"
        def prices():
            entered.set()
            release.wait(2)
            return {"rates": []}
        with patch.dict(sys.modules, {"utils.configs": SimpleNamespace(authorization_list=("system",))}), \
             patch("chatgpt.analytics.get_store", return_value=store), \
             patch("chatgpt.api_keys.usage_key_id", return_value="direct"), \
             patch("chatgpt.pricing.get_pricing_config", side_effect=prices):
            task = asyncio.create_task(start_request("system", {"model": "auto"}, "chat"))
            while not entered.is_set(): await asyncio.sleep(0)
            task.cancel(); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(len(store.rows), 1)
        self.assertEqual(store.rows[0][1]["status"], "cancelled")


class KeyAttributionTests(unittest.TestCase):
    def test_stable_non_secret_identity_survives_revocation_without_authenticating(self):
        with tempfile.TemporaryDirectory() as folder:
            store = APIKeyStore(Path(folder) / "keys.sqlite3")
            created = store.create("test app")
            self.assertEqual(usage_key_id(created["api_key"], (), store=store), created["key"]["id"])
            store.set_status(created["key"]["id"], "revoked")
            self.assertIsNone(store.authenticate(created["api_key"]))
            self.assertEqual(usage_key_id(created["api_key"], (), store=store), created["key"]["id"])
        system = "test-system-key"
        identifier = usage_key_id(system, (system,))
        self.assertTrue(identifier.startswith("system_"))
        self.assertNotIn(system, identifier)
        self.assertEqual(usage_key_id("private-account-credential", (system,)), "direct")


class ChatOptionsTests(unittest.TestCase):
    def test_usage_and_length_options_validate_before_account_selection(self):
        source = Path(__file__).resolve().parents[1] / "api/chat2api.py"
        tree = ast.parse(source.read_text())
        method = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "validate_chat_options")
        namespace = {"HTTPException": HTTPException}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), "exec"), namespace)
        validate = namespace["validate_chat_options"]
        value = validate({"stream": True, "stream_options": {"include_usage": True}, "max_completion_tokens": 20})
        self.assertEqual(value["max_tokens"], 20)
        for options in ({"stream": "yes"}, {"stream_options": {"include_usage": True}},
                        {"stream": True, "stream_options": {"include_usage": 1}}, {"n": 2},
                        {"max_completion_tokens": -1}, {"max_tokens": 5, "max_completion_tokens": 5}):
            with self.subTest(options=options), self.assertRaises(HTTPException): validate(options)
