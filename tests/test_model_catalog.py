"""Offline catalogue checks: injected credentials, clock and temporary cache."""

import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from chatgpt.model_catalog import (CACHE_TTL, MAX_STALE, RETRY_AFTER, CatalogReadError,
                                  ModelCatalog, _default_token_source, _fetch_models, is_chat_model_id)


NOW = 1800000000


def token(expiry=NOW + 10000):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "none"}) + "." + encode({"exp": expiry,
        "https://api.openai.com/auth": {"chatgpt_account_id": "synthetic-account"}}) + ".synthetic"


def ids(catalog):
    return [model["id"] for model in catalog["data"]]


class ModelCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="model-catalog-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "catalog.json"
        self.now = NOW
        self.tokens = ["synthetic-private-token", "unused-second-token"]
        self.calls = []
        self.reply = {"models": [{"slug": "auto"}, {"slug": "gpt-6"}, {"slug": "gpt-5-6"}]}
        self.failure = None

        async def fetch(value):
            self.calls.append(value)
            if self.failure:
                raise self.failure
            return deepcopy(self.reply)

        self.fetch = fetch
        self.service = ModelCatalog(lambda: tuple(self.tokens), fetcher=fetch,
                                    clock=lambda: self.now, cache_path=self.path)

    async def test_lazy_startup_and_openai_shape_always_include_safe_defaults(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(self.calls, [])
        self.tokens = []
        result = await self.service.get_catalog()
        self.assertEqual(result["object"], "list")
        self.assertEqual(ids(result), ["auto", "chatgpt-image-latest", "gpt-image-2"])
        self.assertEqual(self.calls, [])
        image = result["data"][-1]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["source"], "codex_images")
        self.assertEqual(image["supported_endpoints"], ["/v1/images/generations"])
        self.assertEqual(image["availability"], "requires_codex_authorization")
        self.assertIn("默认未启用", image["description"])
        self.assertIn("未声明已验证", image["description"])
        self.assertEqual(result["data"][-2]["source"], "chatgpt_web")
        self.assertIn("不保证", result["data"][-2]["description"])
        self.assertFalse(self.path.exists())

    async def test_only_safe_native_chat_slugs_are_returned(self):
        accepted = ["gpt-6", "gpt-5-6", "gpt-5-6-mini", "gpt-4.1", "o1", "o3-mini", "o4-mini"]
        rejected = ["auto", "gpt-image-2", "research", "gpt-deep-research", "codex-auto-review",
                    "gpt-codex-auto-review", "gpt-6-review", "o1anything", "o2", "gpt-",
                    "gpt-<script>", "gpt-test\n", "gpt-https://private", "GPT-6", "gpt-" + "x" * 97,
                    "sk-private", "eyJ.private.token", None, 7]
        self.reply = {"models": [{"slug": slug, "title": "PRIVATE-TITLE", "email": "private@example.invalid"}
                                  for slug in [*accepted, *rejected, *accepted]]}
        result = await self.service.get_catalog()
        self.assertEqual(ids(result), ["auto", *accepted, "chatgpt-image-latest", "gpt-image-2"])
        self.assertEqual(self.calls, [self.tokens[0]])
        self.assertTrue(all(model["type"] == "chat" for model in result["data"][:-2]))
        serialized = json.dumps(result) + self.path.read_text()
        for secret in [*self.tokens, "PRIVATE-TITLE", "private@example.invalid"]:
            self.assertNotIn(secret, serialized)
        self.assertEqual(set(json.loads(self.path.read_text())), {"version", "refreshed_at", "models"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob(".model_catalog-*")), [])

    async def test_ttl_and_readonly_results(self):
        first = await self.service.get_catalog()
        first["data"][1]["id"] = "mutated"
        self.now += CACHE_TTL - 1
        self.assertIn("gpt-6", ids(await self.service.get_catalog()))
        self.assertEqual(len(self.calls), 1)
        self.now += 1
        self.reply = {"models": [{"slug": "gpt-6-mini"}]}
        self.assertEqual(ids(await self.service.get_catalog()), ["auto", "gpt-6-mini", "chatgpt-image-latest", "gpt-image-2"])
        self.assertEqual(len(self.calls), 2)

    async def test_restart_uses_fresh_sanitized_disk_cache_without_fetch(self):
        before = await self.service.get_catalog()
        restarted = ModelCatalog(lambda: self.tokens, fetcher=self.fetch, clock=lambda: self.now, cache_path=self.path)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(await restarted.get_catalog(), before)
        self.assertEqual(len(self.calls), 1)

    async def test_failed_refresh_preserves_cache_until_max_stale(self):
        before = await self.service.get_catalog()
        original_file = self.path.read_bytes()
        self.failure = RuntimeError("PRIVATE-BEARER upstream raw failure")
        self.now += CACHE_TTL
        with self.assertLogs("chatgpt.model_catalog", level="WARNING") as captured:
            self.assertEqual(await self.service.get_catalog(), before)
        self.assertNotIn("PRIVATE-BEARER", "".join(captured.output))
        self.assertEqual(self.path.read_bytes(), original_file)
        await self.service.get_catalog()
        self.assertEqual(len(self.calls), 2)
        self.now = NOW + MAX_STALE
        self.assertEqual(await self.service.get_catalog(), before)
        self.now += RETRY_AFTER + 1
        self.assertEqual(ids(await self.service.get_catalog()), ["auto", "chatgpt-image-latest", "gpt-image-2"])
        self.assertEqual(self.path.read_bytes(), original_file)

    async def test_invalid_upstream_body_does_not_clear_previous_models(self):
        before = await self.service.get_catalog()
        self.now += CACHE_TTL
        self.reply = {"models": "PRIVATE-INVALID-RESPONSE"}
        self.assertEqual(await self.service.get_catalog(), before)

    async def test_no_token_keeps_stale_cache_and_throttles_repeated_attempts(self):
        before = await self.service.get_catalog()
        self.tokens = []
        self.now += CACHE_TTL
        self.assertEqual(await self.service.get_catalog(), before)
        self.assertEqual(len(self.calls), 1)
        self.tokens = ["new-synthetic-token"]
        await self.service.get_catalog()
        self.assertEqual(len(self.calls), 1)
        self.now += RETRY_AFTER
        await self.service.get_catalog()
        self.assertEqual(self.calls[-1], "new-synthetic-token")

    async def test_concurrent_success_and_failure_refresh_are_singleflight(self):
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []

        async def fetch(value):
            calls.append(value)
            started.set()
            await finish.wait()
            if self.failure:
                raise self.failure
            return self.reply

        service = ModelCatalog(lambda: self.tokens, fetcher=fetch, clock=lambda: self.now, cache_path=None)
        pending = [asyncio.create_task(service.get_catalog()) for _ in range(12)]
        await started.wait()
        self.assertEqual(len(calls), 1)
        finish.set()
        results = await asyncio.gather(*pending)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(item == results[0] for item in results))
        self.now += CACHE_TTL
        self.failure = RuntimeError("synthetic error")
        failed = await asyncio.gather(*(service.get_catalog() for _ in range(12)))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(item == results[0] for item in failed))

    async def test_timeout_returns_defaults_and_cancellation_releases_lock(self):
        async def slow(_):
            await asyncio.Event().wait()

        service = ModelCatalog(lambda: self.tokens, fetcher=slow, clock=lambda: self.now, cache_path=None)
        with patch("chatgpt.model_catalog.FETCH_TIMEOUT", 0.01):
            self.assertEqual(ids(await service.get_catalog()), ["auto", "chatgpt-image-latest", "gpt-image-2"])
        self.now += RETRY_AFTER
        pending = asyncio.create_task(service.get_catalog())
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(service._lock.locked())
        self.assertEqual(ids(await service.get_catalog()), ["auto", "chatgpt-image-latest", "gpt-image-2"])

    async def test_disk_failure_retains_memory_and_rejects_unsafe_cache(self):
        self.path.write_text(json.dumps({"version": 1, "refreshed_at": NOW + 1, "models": ["gpt-unsafe-future"]}))
        with patch("chatgpt.model_catalog.os.replace", side_effect=OSError("PRIVATE-PATH")):
            result = await self.service.get_catalog()
        self.assertEqual(ids(result), ["auto", "gpt-6", "gpt-5-6", "chatgpt-image-latest", "gpt-image-2"])
        self.assertEqual(list(self.path.parent.glob(".model_catalog-*")), [])
        self.assertEqual(await self.service.get_catalog(), result)
        self.assertEqual(len(self.calls), 1)

    async def test_cached_models_are_revalidated_and_oversize_file_is_ignored(self):
        self.path.write_text(json.dumps({"version": 1, "refreshed_at": NOW, "models": ["gpt-6", "sk-secret", "gpt-image-2", "gpt-6", "<script>"]}))
        self.assertEqual(ids(await self.service.get_catalog()), ["auto", "gpt-6", "chatgpt-image-latest", "gpt-image-2"])
        self.assertEqual(self.calls, [])
        self.path.write_text(" " * 70000)
        service = ModelCatalog(lambda: [], fetcher=self.fetch, clock=lambda: self.now, cache_path=self.path)
        self.assertEqual(ids(await service.get_catalog()), ["auto", "chatgpt-image-latest", "gpt-image-2"])

    async def test_find_model_does_not_claim_unknown_slug_exists(self):
        self.assertIsNone(await self.service.find_model([]))
        self.assertIsNone(await self.service.find_model("gpt-<script>"))
        self.assertEqual(self.calls, [])
        self.assertEqual((await self.service.find_model("gpt-6"))["type"], "chat")
        self.assertEqual((await self.service.find_model("gpt-image-2"))["type"], "image")
        self.assertEqual((await self.service.find_model("chatgpt-image-latest"))["type"], "image")
        self.assertIsNone(await self.service.find_model("gpt-not-listed"))


class DefaultModelFetcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_source_excludes_error_expired_and_uncached_refresh_tokens(self):
        valid, expired = token(), token(NOW - 1)
        refresh = "R" * 45
        state = ModuleType("utils.globals")
        state.token_list = ["marked-bad", "workspace-bad,account", expired, "U" * 45, refresh, valid, valid]
        state.error_token_list = ["marked-bad", "workspace-bad"]
        state.refresh_map = {refresh: {"token": valid}}
        with patch.dict(sys.modules, {"utils.globals": state}), patch("chatgpt.model_catalog.time.time", return_value=NOW):
            self.assertEqual(_default_token_source(), (refresh, valid))

    async def test_fetch_uses_fixed_path_fp_access_and_account_without_redirects(self):
        access = token()
        state = ModuleType("utils.globals"); state.refresh_map = {}
        config = ModuleType("utils.configs"); config.chatgpt_base_url_list = ["https://chatgpt.example.invalid/"]; config.auth_key = "synthetic-relay-key"
        fp = ModuleType("chatgpt.fp"); fp.get_fp = lambda _: {"user-agent": "Synthetic UA", "oai-device-id": "synthetic-device", "cookie": "MUST-NOT-SEND", "impersonate": "chrome123"}
        calls = []
        response = SimpleNamespace(status_code=200, content=b'{}', json=lambda: {"models": [{"slug": "gpt-6"}]})

        class Session:
            def __init__(self, **kwargs):
                calls.append(("session", kwargs))

            async def get(self, url, **kwargs):
                calls.append((url, kwargs)); return response

            async def close(self):
                calls.append(("closed", {}))

        with patch.dict(sys.modules, {"utils.configs": config, "utils.globals": state, "chatgpt.fp": fp}), patch("curl_cffi.requests.AsyncSession", Session):
            result = await _fetch_models(access + ",override-account")
            self.assertEqual(result["models"][0]["slug"], "gpt-6")
            self.assertEqual(calls[1][0], "https://chatgpt.example.invalid/backend-api/models")
            self.assertFalse(calls[1][1]["allow_redirects"])
            headers = calls[1][1]["headers"]
            self.assertEqual(headers["Authorization"], "Bearer " + access)
            self.assertEqual(headers["ChatGPT-Account-Id"], "override-account")
            self.assertEqual(headers["user-agent"], "Synthetic UA")
            self.assertNotIn("cookie", headers)
            self.assertEqual(calls[-1][0], "closed")
            response.status_code = 302
            with self.assertRaises(CatalogReadError):
                await _fetch_models(access)
            self.assertEqual(calls[-1][0], "closed")
            for bad in ["https://user:secret@host.invalid", "https://host.invalid/?token=private", "https://host.invalid/#fragment", "file:///private"]:
                config.chatgpt_base_url_list = [bad]
                count = len(calls)
                with self.assertRaises(CatalogReadError):
                    await _fetch_models(access)
                self.assertEqual(len(calls), count)


if __name__ == "__main__":
    unittest.main()
