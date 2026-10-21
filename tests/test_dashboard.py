"""Dashboard route checks using only temporary ledgers and an isolated ASGI app."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from chatgpt import analytics, pricing


async def exchange(app, path, method="GET", payload=None, authenticated=True):
    route, _, query = path.partition("?")
    body = json.dumps(payload).encode() if payload is not None else b""
    scope = {"type": "http", "http_version": "1.1", "method": method, "scheme": "http",
             "path": route, "raw_path": route.encode(), "query_string": query.encode(),
             "root_path": "", "headers": [(b"content-type", b"application/json")] +
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
    return start["status"], dict(start["headers"]), raw


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="dashboard-test-")
        self.addCleanup(self.directory.cleanup)
        self.store = analytics.AnalyticsStore(Path(self.directory.name) / "requests.sqlite3")
        self.pricing_path = Path(self.directory.name) / "prices.json"
        application = ModuleType("app")
        application.app = FastAPI()
        application.templates = SimpleNamespace(TemplateResponse=lambda **_: HTMLResponse("Dashboard"))
        console = ModuleType("api.local_console")
        console.current_session = lambda request: request.headers.get("x-test-admin") == "yes"
        def require(request):
            if not console.current_session(request):
                raise HTTPException(401, "Login required")
        console.require_session = require
        spec = importlib.util.spec_from_file_location("_tested_dashboard", Path(__file__).resolve().parents[1] / "api/dashboard.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"app": application, "api.local_console": console}):
            spec.loader.exec_module(self.module)
        self.app = application.app
        self.addCleanup(patch.stopall)
        patch.object(self.module.analytics, "get_store", return_value=self.store).start()
        # Freeze references before patching the service names.
        get_prices, update_prices = pricing.get_pricing_config, pricing.update_pricing_config
        patch.object(self.module.pricing, "get_pricing_config", side_effect=lambda: get_prices(path=self.pricing_path)).start()
        patch.object(self.module.pricing, "update_pricing_config", side_effect=lambda payload: update_prices(payload, path=self.pricing_path)).start()

    async def test_anonymous_page_redirect_and_all_data_routes_require_session(self):
        result = await exchange(self.app, "/dashboard", authenticated=False)
        self.assertEqual(result[0], 303)
        self.assertIn(b"/login", result[1][b"location"])
        for path, method, payload in (("/local-console/dashboard", "GET", None),
                                     ("/local-console/dashboard/pricing", "GET", None),
                                     ("/local-console/dashboard/pricing", "PUT", {"custom_rates": []})):
            self.assertEqual((await exchange(self.app, path, method, payload, authenticated=False))[0], 401)
        self.module.analytics.get_store.assert_not_called()
        self.module.pricing.get_pricing_config.assert_not_called()
        self.module.pricing.update_pricing_config.assert_not_called()

    async def test_query_schema_and_dates_validate_before_storage_read(self):
        for query in ("days=8", "days=abc", "kind=secret", "model=" + "x" * 101,
                      "date_from=2026-15-99&date_to=2026-10-01", "timezone=Not/AZone",
                      "date_from=2026-10-21"):
            result = await exchange(self.app, "/local-console/dashboard?" + query)
            self.assertEqual(result[0], 422, query)

    async def test_real_snapshot_empty_and_key_filtered_unknown_money(self):
        request = self.store.start(key_id="key_" + "a" * 24, kind="image", requested_model="chatgpt-image-latest")
        self.store.finish(request, status="succeeded", http_status=200, generated_images=1)
        status, _, body = await exchange(self.app, "/local-console/dashboard?days=30&timezone=Asia%2FShanghai")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["total"]["requests"], 1)
        self.assertEqual(data["total"]["image_requests"], 1)
        self.assertEqual(data["total"]["generated_images"], 1)
        self.assertEqual(data["coverage"]["known_cost_requests"], 0)
        self.assertEqual(data["coverage"]["unknown_cost_requests"], 1)
        self.assertEqual(len(data["hourly"]), 24)
        self.assertEqual(len(data["daily"]), 30)
        self.assertNotIn("prompt", data["recent"][0])
        result = await exchange(self.app, "/local-console/dashboard?api_key_id=key_" + "b" * 24)
        self.assertEqual(json.loads(result[2])["total"]["requests"], 0)

    async def test_price_roundtrip_unknown_fields_and_clear_custom(self):
        response = await exchange(self.app, "/local-console/dashboard/pricing")
        self.assertEqual(response[0], 200)
        initial = json.loads(response[2])
        self.assertEqual(initial["currency"], "USD")
        rate = {"model": "synthetic-model", "kind": "image", "image_per_unit_usd": "0.123456789"}
        saved = await exchange(self.app, "/local-console/dashboard/pricing", "PUT", {"custom_rates": [rate]})
        self.assertEqual(saved[0], 200)
        self.assertEqual(json.loads(saved[2])["custom_rates"][0]["image_per_unit_usd"], "0.123456789")
        invalid = await exchange(self.app, "/local-console/dashboard/pricing", "PUT", {"custom_rates": [{**rate, "source": "official"}]})
        self.assertEqual(invalid[0], 422)
        after = json.loads((await exchange(self.app, "/local-console/dashboard/pricing"))[2])
        self.assertEqual(after["custom_rates"], json.loads(saved[2])["custom_rates"])
        cleared = await exchange(self.app, "/local-console/dashboard/pricing", "PUT", {"custom_rates": []})
        self.assertEqual(json.loads(cleared[2])["custom_rates"], [])

    async def test_unavailable_ledger_does_not_echo_error_details(self):
        with patch.object(self.store, "snapshot", side_effect=RuntimeError("PRIVATE TOKEN PATH")):
            status, _, body = await exchange(self.app, "/local-console/dashboard")
        self.assertEqual(status, 503)
        self.assertNotIn(b"PRIVATE", body)


if __name__ == "__main__":
    unittest.main()
