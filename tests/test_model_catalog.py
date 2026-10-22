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
                                  DEFAULT_MODELS, ModelCatalog, _default_token_source, _fetch_models, is_chat_model_id)


NOW = 1800000000


def token(expiry=NOW + 10000):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "none"}) + "." + encode({"exp": expiry,
        "https://api.openai.com/auth": {"chatgpt_account_id": "synthetic-account"}}) + ".synthetic"


def ids(catalog):
    return [model["id"] for model in catalog["data"]]


A = "acct_" + "a" * 24
B = "acct_" + "b" * 24
C = "acct_" + "c" * 24


class ModelCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="model-catalog-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "catalog.json"
        self.now = NOW
        self.tokens = ["private-a", "private-b"]
        self.identities = {"private-a": A, "private-b": B, "private-a-rotated": A, "private-c": C}
        self.calls = []
        self.replies = {"private-a": {"models": [{"slug": "gpt-6"}]},
                        "private-b": {"models": [{"slug": "gpt-6-luna"}]}}

        async def fetch(value):
            self.calls.append(value)
            result = self.replies[value]
            if isinstance(result, Exception):
                raise result
            return deepcopy(result)

        self.fetch = fetch
        self.service = self.create()

    def create(self, **kwargs):
        return ModelCatalog(lambda: tuple(self.tokens), fetcher=kwargs.get("fetcher", self.fetch),
                            clock=lambda: self.now, cache_path=kwargs.get("cache_path", self.path),
                            identity_resolver=lambda value: self.identities[value], refresh_spacing=0)

    async def settle(self, service=None):
        service = service or self.service
        if service._task:
            await service._task

    async def test_lazy_get_returns_immediately_and_schedules_one_background_worker(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(value):
            self.calls.append(value)
            started.set()
            await release.wait()
            return self.replies[value]

        service = self.create(fetcher=slow)
        self.assertFalse(self.path.exists())
        first = await asyncio.wait_for(service.get_catalog(), 0.2)
        self.assertEqual(ids(first), ["auto", "chatgpt-image-latest", "gpt-image-2"])
        self.assertTrue(first["meta"]["refreshing"])
        worker = service._task
        await started.wait()
        for _ in range(10):
            await service.get_catalog()
            self.assertIs(worker, service._task)
        release.set()
        await self.settle(service)
        self.assertEqual(set(self.calls), set(self.tokens))

    async def test_per_account_union_and_public_privacy(self):
        await self.service.refresh_catalog()
        result = await self.service.get_catalog()
        self.assertEqual(ids(result), ["auto", "gpt-6", "gpt-6-luna", "chatgpt-image-latest", "gpt-image-2"])
        serialized = json.dumps(result)
        for secret in [*self.tokens, A, B, "fingerprint"]:
            self.assertNotIn(secret, serialized)
        saved = self.path.read_text()
        for secret in self.tokens:
            self.assertNotIn(secret, saved)
        self.assertEqual(json.loads(saved)["version"], 2)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob(".model_catalog-*")), [])
        self.assertTrue(self.service.account_supports_model(A, "gpt-6"))
        self.assertFalse(self.service.account_supports_model(A, "gpt-6-luna"))
        self.assertTrue(self.service.account_supports_model(B, "gpt-6-luna"))
        self.assertFalse(self.service.account_supports_model(B, "gpt-6-6luna"))
        admin = self.service.get_account_catalogs()
        rows = {row["account_id"]: row for row in admin["accounts"]}
        self.assertEqual(rows[A]["models"], ["gpt-6"])
        self.assertEqual(rows[B]["checks"]["gpt-6-luna"]["status"], "unverified")
        self.assertEqual(rows[B]["status"], "fresh")
        self.assertEqual(result["data"][-1]["availability"], "requires_codex_authorization")

    async def test_default_catalog_has_exact_order_without_granting_account_access(self):
        self.tokens = []
        result = await self.service.get_default_catalog()
        self.assertEqual(ids(result), list(DEFAULT_MODELS))
        self.assertEqual(ids(result), ["gpt-6-luna", "auto", "gpt-image-2"])
        self.assertEqual(result["data"][0]["availability"], "unavailable")
        self.assertEqual(result["data"][0]["verification"], "unverified")
        self.assertEqual(result["data"][2]["availability"], "requires_codex_authorization")
        self.assertFalse(self.service.account_supports_model(A, "gpt-6-luna"))
        self.assertEqual(self.calls, [])

    async def test_default_projection_preserves_full_discovery_and_evidence(self):
        await self.service.refresh_catalog()
        public = await self.service.get_default_catalog()
        self.assertEqual(ids(public), list(DEFAULT_MODELS))
        self.assertEqual(public["data"][0]["verification"], "directory_visible")
        self.assertEqual(public["data"][0]["meta"]["directory_accounts"], 1)
        self.assertEqual(public["data"][0]["source"], "chatgpt_web")
        self.assertEqual(public["data"][2]["source"], "codex_images")
        full = await self.service.get_catalog()
        self.assertIn("gpt-6", ids(full))
        self.assertIn("chatgpt-image-latest", ids(full))
        self.assertTrue(self.service.account_supports_model(A, "gpt-6"))
        self.assertTrue(self.service.account_supports_model(B, "gpt-6-luna"))
        for secret in [*self.tokens, A, B, "fingerprint"]:
            self.assertNotIn(secret, json.dumps(public))

    async def test_denied_or_stale_luna_stays_listed_but_not_schedulable(self):
        await self.service.refresh_catalog()
        self.service.record_model_check(B, "gpt-6-luna", "denied", sent_model="gpt-6-luna", http_status=403)
        public = await self.service.get_default_catalog()
        self.assertEqual(public["data"][0]["availability"], "unavailable")
        self.assertFalse(self.service.account_supports_model(B, "gpt-6-luna"))
        self.now += MAX_STALE + 1
        public = await self.service.get_default_catalog()
        self.assertEqual(public["data"][0]["availability"], "unavailable")
        self.assertFalse(self.service.account_supports_model(B, "gpt-6-luna"))
        await self.settle()

    async def test_safe_slugs_only_and_no_alias_invention(self):
        accepted = ["gpt-6", "gpt-6-luna", "gpt-6-6luna", "gpt-5-6", "gpt-4.1", "o1", "o3-mini", "o4-mini"]
        rejected = ["auto", "luna", "gpt-image-2", "gpt-deep-research", "codex-auto-review", "gpt-6-review",
                    "o1anything", "o2", "gpt-", "gpt-<script>", "gpt-test\n", "gpt-https://private",
                    "GPT-6", "gpt-" + "x" * 97, "sk-private", "eyJ.private.token", None, 7]
        self.replies["private-a"] = {"models": [{"slug": slug, "title": "PRIVATE", "email": "secret@example.invalid"}
                                              for slug in [*accepted, *rejected, *accepted]]}
        self.tokens = ["private-a"]
        await self.service.refresh_catalog()
        self.assertEqual(ids(await self.service.get_catalog()), ["auto", *accepted, "chatgpt-image-latest", "gpt-image-2"])
        self.assertNotIn("PRIVATE", self.path.read_text())
        self.assertNotIn("secret@example.invalid", self.path.read_text())

    async def test_ttl_force_rate_limit_and_readonly_snapshots(self):
        await self.service.refresh_catalog()
        first = await self.service.get_catalog()
        first["data"][1]["id"] = "mutated"
        first_admin = self.service.get_account_catalogs()
        first_admin["accounts"][0]["models"].clear()
        await self.service.refresh_catalog(force=True)
        self.assertEqual(len(self.calls), 2)
        self.now += RETRY_AFTER
        await self.service.refresh_catalog(account_id=A, force=True)
        self.assertEqual(self.calls.count("private-a"), 2)
        self.assertIn("gpt-6", ids(await self.service.get_catalog()))
        self.now += CACHE_TTL
        await self.service.get_catalog()
        await self.settle()
        self.assertEqual(len(self.calls), 5)

    async def test_failed_one_account_does_not_stop_other_accounts_or_leak_error(self):
        self.replies["private-a"] = RuntimeError("PRIVATE-BEARER raw upstream response")
        await self.service.refresh_catalog()
        self.assertTrue(self.service.account_supports_model(B, "gpt-6-luna"))
        rows = self.service.get_account_catalogs()["accounts"]
        self.assertEqual(rows[0]["error_code"], "unavailable")
        self.assertEqual(rows[0]["status"], "error")
        self.assertNotIn("PRIVATE-BEARER", json.dumps(rows) + self.path.read_text())

    async def test_failed_refresh_keeps_last_success_until_max_stale(self):
        await self.service.refresh_catalog()
        self.replies["private-a"] = {"models": "bad"}
        self.now += CACHE_TTL
        await self.service.refresh_catalog()
        row = self.service.get_account_catalogs()["accounts"][0]
        self.assertEqual(row["status"], "stale")
        self.assertEqual(row["error_code"], "invalid_response")
        self.assertTrue(self.service.account_supports_model(A, "gpt-6"))
        self.now = NOW + MAX_STALE + 1
        self.assertFalse(self.service.account_supports_model(A, "gpt-6"))
        self.assertEqual(self.service.get_account_catalogs()["accounts"][0]["status"], "expired")

    async def test_restart_uses_per_account_cache_without_network(self):
        await self.service.refresh_catalog()
        restarted = self.create()
        self.assertEqual(ids(await restarted.get_catalog()), ids(await self.service.get_catalog()))
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(restarted._task)
        self.assertTrue(restarted.account_supports_model(B, "gpt-6-luna"))

    async def test_legacy_v1_cache_is_unattributed_not_all_account_permission(self):
        self.path.write_text(json.dumps({"version": 1, "refreshed_at": NOW,
                                        "models": ["gpt-old", "sk-secret", "gpt-old"]}))
        result = await self.service.get_catalog()
        row = next(row for row in result["data"] if row["id"] == "gpt-old")
        self.assertEqual(row["verification"], "unverified")
        self.assertFalse(self.service.account_supports_model(A, "gpt-old"))
        await self.settle()
        self.assertEqual(json.loads(self.path.read_text())["version"], 2)

    async def test_rotating_credentials_invalidates_snapshot_and_checks(self):
        await self.service.refresh_catalog()
        self.service.record_model_check(A, "gpt-6-luna", "verified", sent_model="gpt-6-luna",
                                        reported_model="gpt-6-luna", http_status=200)
        self.tokens = ["private-a-rotated", "private-b"]
        self.assertFalse(self.service.account_supports_model(A, "gpt-6"))
        self.assertFalse(self.service.account_supports_model(A, "gpt-6-luna"))
        self.assertEqual(self.service.get_account_catalogs()["accounts"][0]["status"], "unqueried")
        self.replies["private-a-rotated"] = {"models": [{"slug": "gpt-6-mini"}]}
        await self.service.refresh_catalog()
        self.assertTrue(self.service.account_supports_model(A, "gpt-6-mini"))

    async def test_removed_account_never_supplies_public_models(self):
        await self.service.refresh_catalog()
        self.tokens = ["private-a"]
        self.assertNotIn("gpt-6-luna", ids(await self.service.get_catalog()))
        self.assertFalse(self.service.account_supports_model(B, "gpt-6-luna"))

    async def test_unavailable_credentials_keep_admin_row_without_upstream_requests(self):
        service = ModelCatalog(lambda: ["private-a"], account_token_source=lambda: self.tokens,
            fetcher=self.fetch, identity_resolver=lambda value: self.identities[value],
            clock=lambda: self.now, cache_path=None, refresh_spacing=0)
        await service.refresh_catalog()
        rows = {row["account_id"]: row for row in service.get_account_catalogs()["accounts"]}
        self.assertEqual(set(rows), {A, B})
        self.assertEqual(rows[B]["status"], "error")
        self.assertEqual(rows[B]["error_code"], "credentials_unavailable")
        self.assertFalse(rows[B]["discovery_available"])
        self.assertFalse(service.account_supports_model(B, "auto"))
        self.assertEqual(self.calls, ["private-a"])

    async def test_duplicate_credentials_for_same_account_fetch_once(self):
        self.identities["private-a-copy"] = A
        self.tokens = ["private-a", "private-a", "private-a-copy"]
        await self.service.refresh_catalog()
        self.assertEqual(self.calls, ["private-a"])
        self.assertEqual(len(self.service.get_account_catalogs()["accounts"]), 1)

    async def test_exact_verified_model_is_added_but_mismatch_or_denial_excluded(self):
        await self.service.refresh_catalog()
        self.service.record_model_check(A, "gpt-6-luna", "verified", sent_model="gpt-6-luna",
                                        reported_model="gpt-6-luna", http_status=200)
        self.assertTrue(self.service.account_supports_model(A, "gpt-6-luna"))
        self.service.record_model_check(B, "gpt-6-luna", "verified", sent_model="gpt-6-luna",
                                        reported_model="gpt-6-mini", http_status=200)
        self.assertFalse(self.service.account_supports_model(B, "gpt-6-luna"))
        self.assertEqual(self.service.get_model_support(B, "gpt-6-luna")["status"], "model_mismatch")
        self.service.record_model_check(A, "gpt-6-luna", "denied", sent_model="gpt-6-luna",
                                        http_status=403, error_code="permission_denied")
        self.assertFalse(self.service.account_supports_model(A, "gpt-6-luna"))
        self.assertNotIn("gpt-6-luna", ids(await self.service.get_catalog()))

    async def test_http_200_without_model_evidence_does_not_verify(self):
        row = self.service.record_model_check(A, "gpt-6-luna", "verified", http_status=200)
        self.assertEqual(row["status"], "model_mismatch")
        self.assertFalse(self.service.account_supports_model(A, "gpt-6-luna"))
        self.assertEqual(self.calls, [])

    async def test_verification_status_expiry_and_safe_metadata(self):
        row = self.service.record_model_check(A, "gpt-6-luna", "error", sent_model="sk-PRIVATE",
                                              reported_model="PRIVATE URL", http_status="200", error_code="Bearer SECRET")
        self.assertIsNone(row["sent_model"])
        self.assertIsNone(row["reported_model"])
        self.assertIsNone(row["error_code"])
        self.assertNotIn("PRIVATE", self.path.read_text())
        self.assertNotIn("SECRET", self.path.read_text())
        self.service.record_model_check(A, "gpt-6-luna", "verified", sent_model="gpt-6-luna", reported_model="gpt-6-luna")
        self.now += MAX_STALE + 1
        self.assertFalse(self.service.account_supports_model(A, "gpt-6-luna"))
        self.assertTrue(self.service.get_account_catalogs()["accounts"][0]["checks"]["gpt-6-luna"]["stale"])
        for key, model, status in [("bad", "gpt-6-luna", "verified"), (A, "sk-private", "verified"), (A, "gpt-6", "bad")]:
            with self.assertRaises(ValueError):
                self.service.record_model_check(key, model, status)

    async def test_public_evidence_counts_are_not_double_counted(self):
        await self.service.refresh_catalog()
        self.service.record_model_check(B, "gpt-6-luna", "verified", sent_model="gpt-6-luna", reported_model="gpt-6-luna")
        row = next(row for row in (await self.service.get_catalog())["data"] if row["id"] == "gpt-6-luna")
        self.assertEqual(row["meta"], {"directory_accounts": 1, "verified_accounts": 1})

    async def test_external_verification_cache_update_is_observed(self):
        await self.service.refresh_catalog()
        other = self.create()
        self.assertFalse(self.service.account_supports_model(A, "gpt-6-luna"))
        other.record_model_check(A, "gpt-6-luna", "verified", sent_model="gpt-6-luna", reported_model="gpt-6-luna")
        self.assertTrue(self.service.account_supports_model(A, "gpt-6-luna"))

    async def test_legacy_auto_and_web_image_remain_compatible_but_channels_do_not_mix(self):
        for model in [None, "auto", "gpt-4o", "gpt-4o-2024-08-06", "o3-mini-high", "g-test", "gizmo-test", "chatgpt-image-latest"]:
            self.assertTrue(self.service.account_supports_model(A, model))
        self.assertFalse(self.service.account_supports_model(A, "gpt-image-2"))
        self.assertFalse(self.service.account_supports_model(A, "auto", channel="codex_native"))
        self.assertEqual(self.calls, [])

    async def test_bounded_concurrency_cancellation_and_timeout(self):
        self.tokens.append("private-c")
        started, finish = asyncio.Event(), asyncio.Event()
        active, maximum = 0, 0

        async def slow(value):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            started.set()
            try:
                await finish.wait()
            finally:
                active -= 1
            return {"models": [{"slug": "gpt-6"}]}

        service = self.create(fetcher=slow, cache_path=None)
        pending = asyncio.create_task(service.refresh_catalog())
        await started.wait()
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(active, 0)
        self.assertLessEqual(maximum, 2)
        self.assertFalse(service._lock.locked())
        self.now += RETRY_AFTER
        with patch("chatgpt.model_catalog.FETCH_TIMEOUT", 0.01):
            await service.refresh_catalog()
        self.assertEqual(active, 0)
        self.assertTrue(all(row["error_code"] == "timeout" for row in service.get_account_catalogs()["accounts"]))

    async def test_explicit_refresh_singleflight_and_target_validation(self):
        await asyncio.gather(*(self.service.refresh_catalog() for _ in range(12)))
        self.assertEqual(len(self.calls), 2)
        with self.assertRaises(ValueError):
            await self.service.refresh_catalog("bad")
        with self.assertRaises(ValueError):
            await self.service.refresh_catalog(C)

    async def test_disk_failure_and_invalid_cache_are_safe(self):
        self.path.write_text(json.dumps({"version": 1, "refreshed_at": NOW + 1, "models": ["gpt-future"]}))
        with patch("chatgpt.model_catalog.os.replace", side_effect=OSError("PRIVATE-PATH")):
            await self.service.refresh_catalog()
        self.assertTrue(self.service.account_supports_model(A, "gpt-6"))
        self.assertTrue(self.service.get_account_catalogs()["cache_persistence_error"])
        self.assertEqual(list(self.path.parent.glob(".model_catalog-*")), [])

    async def test_find_model_does_not_invent_unknown_ids(self):
        self.assertIsNone(await self.service.find_model([]))
        self.assertIsNone(await self.service.find_model("gpt-<script>"))
        self.assertEqual(self.calls, [])
        await self.service.refresh_catalog()
        self.assertEqual((await self.service.find_model("gpt-6"))["type"], "chat")
        self.assertEqual((await self.service.find_model("gpt-image-2"))["type"], "image")
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
