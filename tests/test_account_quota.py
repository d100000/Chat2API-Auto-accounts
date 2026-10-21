"""Offline quota tests: synthetic JWTs, temporary caches, no application imports."""

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

from chatgpt.account_quota import AccountQuotaService, QuotaReadError, _fetch_quota, account_identity


NOW = 1800000000


def token(account="example-account", expiry=NOW + 3600, nonce="one", user=None):
    auth = {"chatgpt_account_id": account, **({"chatgpt_user_id": user} if user else {})}
    claims = {"https://api.openai.com/auth": auth, "exp": expiry, "nonce": nonce}
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "none"}) + "." + encode(claims) + ".synthetic"


def fixture():
    return {"limits_progress": [
        {"feature_name": "reason", "remaining": 12, "reset_after": "2027-01-15T12:00:00+00:00"},
        {"feature_name": "image_gen", "remaining": 0, "reset_after": "2027-01-16T00:00:00+00:00"},
        {"feature_name": "file_upload", "remaining": 15, "reset_after_seconds": 700},
    ], "model_limits": [{"model_slug": "example-model-1.2", "limit": 4, "window_seconds": 3600}],
        "blocked_features": [{"name": "image_gen", "limit": 25, "block_reason": None}],
        "primary_window": {"remaining": 7, "window_seconds": 7200}}


class AccountQuotaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="account-quota-test-")
        self.addCleanup(self.sandbox.cleanup)
        self.now = NOW
        self.tokens = [token()]
        self.calls = 0
        self.reply = fixture()
        self.failure = None

        async def fetch(*args):
            self.calls += 1
            if self.failure:
                raise self.failure
            return deepcopy(self.reply)

        self.service = AccountQuotaService(lambda: tuple(self.tokens), cache_path=Path(self.sandbox.name) / "quota.json",
                                           clock=lambda: self.now, fetcher=fetch)

    async def test_unknown_is_not_zero_and_listing_never_calls_network(self):
        row = self.service.get_accounts()["accounts"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertIsNone(row["features"]["reason"]["remaining"])
        self.assertIsNone(row["features"]["image_gen"]["total"])
        self.assertEqual(self.calls, 0)
        self.tokens = []
        self.assertEqual((await self.service.refresh())["accounts"], [])
        with self.assertRaises(KeyError):
            await self.service.refresh("not-present")

    async def test_identity_deduplicates_tokens_and_override_matches_ledger(self):
        self.tokens = [token(expiry=NOW - 1), token(), token(nonce="new")]
        row = (await self.service.refresh())["accounts"][0]
        self.assertEqual(row["token_count"], 3)
        self.assertEqual(row["credential_status"], "valid")
        self.assertEqual(self.calls, 1)
        self.assertEqual(row["id"], account_identity(self.tokens[1])["id"])
        explicit = self.tokens[1] + ",other-account"
        self.assertEqual(account_identity(explicit)["id"], account_identity(self.tokens[1], "other-account")["id"])
        self.tokens = [explicit]
        self.assertEqual(self.service.get_accounts()["accounts"][0]["id"], account_identity(explicit)["id"])

    async def test_only_explicit_quota_values_and_safe_fields_survive(self):
        secret = self.tokens[0]
        self.reply["access_token"] = secret
        self.reply["limits_progress"][0]["email"] = "secret@example.invalid"
        self.reply["model_limits"][0]["message"] = secret
        row = (await self.service.refresh())["accounts"][0]
        reason, image = row["features"]["reason"], row["features"]["image_gen"]
        self.assertEqual(reason["remaining"], 12)
        self.assertIsNone(reason["total"])
        self.assertIsNone(reason["used"])
        self.assertEqual(image["total"], 25)
        self.assertEqual(image["remaining"], 0)
        self.assertIsNone(image["used"])
        self.assertTrue(image["blocked"])
        self.assertEqual(row["windows"]["primary_window"]["window_seconds"], 7200)
        self.assertEqual(row["model_limits"][0]["model_slug"], "example-model-1.2")
        serialized = json.dumps(row) + (Path(self.sandbox.name) / "quota.json").read_text()
        for value in (secret, "secret@example.invalid", "example-account"):
            self.assertNotIn(value, serialized)

    async def test_workspace_members_are_separate_but_rotated_member_tokens_deduplicate(self):
        self.tokens = [token(user="user-a"), token(user="user-b"), token(user="user-a", nonce="rotated")]
        rows = (await self.service.refresh())["accounts"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["token_count"] for row in rows), [1, 2])
        self.assertEqual(self.calls, 2)
        self.assertNotEqual(account_identity(self.tokens[0])["id"], account_identity(self.tokens[1])["id"])
        self.assertEqual(rows[0]["id"], account_identity(self.tokens[0])["id"])
        self.assertEqual(rows[0]["identity_source"], "account_user_claim")

    async def test_cooldown_failure_keeps_prior_values_and_safe_error(self):
        first = (await self.service.refresh())["accounts"][0]
        again = (await self.service.refresh(first["id"]))["accounts"][0]
        self.assertEqual(again["refresh"]["status"], "cooldown")
        self.assertEqual(again["refresh"]["retry_after_seconds"], 60)
        self.assertEqual(self.calls, 1)
        self.now += 61
        self.failure = RuntimeError("Bearer " + self.tokens[0])
        failed = (await self.service.refresh())["accounts"][0]
        self.assertEqual(failed["status"], "stale")
        self.assertEqual(failed["checked_at"], first["checked_at"])
        self.assertEqual(failed["features"]["reason"]["remaining"], 12)
        self.assertEqual(failed["refresh"]["error"]["code"], "network_error")
        self.assertNotIn(self.tokens[0], json.dumps(failed))
        self.now += 61
        self.failure = QuotaReadError("upstream_http", 401)
        self.assertEqual((await self.service.refresh())["accounts"][0]["refresh"]["error"]["http_status"], 401)

    async def test_cache_is_private_restart_preserves_data_and_cooldown(self):
        before = await self.service.refresh()
        path = Path(self.sandbox.name) / "quota.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        restarted = AccountQuotaService(lambda: tuple(self.tokens), cache_path=path, clock=lambda: self.now)
        after = await restarted.refresh()
        self.assertEqual(after["accounts"][0]["features"], before["accounts"][0]["features"])
        self.assertEqual(after["accounts"][0]["windows"], before["accounts"][0]["windows"])
        self.assertEqual(after["accounts"][0]["refresh"]["status"], "cooldown")

    async def test_reset_and_cache_expiry_do_not_refill_remaining(self):
        await self.service.refresh()
        self.now += 301
        self.assertEqual(self.service.get_accounts()["accounts"][0]["status"], "stale")
        self.now = 1800144001
        row = self.service.get_accounts()["accounts"][0]
        self.assertEqual(row["features"]["image_gen"]["status"], "expired")
        self.assertEqual(row["features"]["image_gen"]["remaining"], 0)
        self.assertEqual(row["credential_status"], "expired")

    async def test_one_corrupt_cache_record_preserves_other_accounts_and_cooldowns(self):
        before = (await self.service.refresh())["accounts"][0]
        path = Path(self.sandbox.name) / "quota.json"
        document = json.loads(path.read_text())
        document["accounts"]["acct_" + "0" * 24] = {"payload": {"limits_progress": "corrupt"}}
        path.write_text(json.dumps(document))
        restarted = AccountQuotaService(lambda: tuple(self.tokens), cache_path=path, clock=lambda: self.now)
        row = (await restarted.refresh())["accounts"][0]
        self.assertEqual(row["checked_at"], before["checked_at"])
        self.assertEqual(row["features"], before["features"])
        self.assertEqual(row["refresh"]["status"], "cooldown")

    async def test_expired_and_uncached_refresh_tokens_do_not_make_requests(self):
        self.tokens = [token(expiry=NOW - 1)]
        row = (await self.service.refresh())["accounts"][0]
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["refresh"]["status"], "expired")
        self.tokens = ["r" * 45]
        row = (await self.service.refresh())["accounts"][0]
        self.assertEqual(row["credential_status"], "unknown")
        self.assertEqual(row["refresh"]["status"], "unsupported")
        self.assertEqual(self.calls, 0)

    async def test_malformed_success_is_a_failure_and_bad_numbers_stay_unknown(self):
        self.reply = {"error": "credential material must never be returned"}
        row = (await self.service.refresh())["accounts"][0]
        self.assertEqual(row["refresh"]["error"]["code"], "invalid_response")
        self.assertIsNone(row["checked_at"])
        self.now += 61
        self.reply = {"limits_progress": [{"feature_name": "reason", "remaining": False, "total": -1, "used": float("inf")}],
                      "model_limits": [], "blocked_features": []}
        row = (await self.service.refresh())["accounts"][0]
        self.assertIsNone(row["features"]["reason"]["remaining"])
        self.assertIsNone(row["features"]["reason"]["total"])
        self.assertIsNone(row["features"]["reason"]["used"])

    async def test_global_concurrency_and_same_account_single_flight(self):
        self.tokens = [token(account=f"synthetic-{i}") for i in range(5)]
        started, release = asyncio.Event(), asyncio.Event()
        active, peak, calls = 0, 0, 0

        async def fetch(*args):
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            started.set()
            await release.wait()
            active -= 1
            return fixture()

        service = AccountQuotaService(lambda: tuple(self.tokens), cache_path=None, fetcher=fetch, clock=lambda: self.now)
        pending = asyncio.create_task(service.refresh())
        await started.wait()
        during = await service.refresh()
        self.assertTrue(all(row["refresh"]["status"] == "in_progress" for row in during["accounts"]))
        release.set()
        await pending
        self.assertEqual(peak, 2)
        self.assertEqual(calls, 5)

    async def test_transport_uses_only_metadata_path_and_never_follows_redirects(self):
        captured = {}
        class Client:
            def __init__(self, **kwargs):
                captured["client"] = kwargs
            async def post(self, url, **kwargs):
                captured.update(url=url, request=kwargs)
                return SimpleNamespace(status_code=302, content=b"secret upstream body")
            async def close(self):
                captured["closed"] = True

        fake_utils = ModuleType("utils")
        fake_utils.configs = SimpleNamespace(chatgpt_base_url_list=["https://example.invalid"], auth_key=None)
        modules = {"utils": fake_utils, "curl_cffi.requests": SimpleNamespace(AsyncSession=Client),
                   "chatgpt.fp": SimpleNamespace(get_fp=lambda key: {"user-agent": "SyntheticBrowser", "impersonate": "chrome123", "proxy_url": "http://localhost:8080"})}
        with patch.dict(sys.modules, modules):
            with self.assertRaises(QuotaReadError) as error:
                await _fetch_quota("synthetic-access", "synthetic-account", "synthetic-access")
        self.assertEqual(error.exception.http_status, 302)
        self.assertEqual(captured["url"], "https://example.invalid/backend-api/conversation/init")
        self.assertFalse(captured["request"]["allow_redirects"])
        self.assertNotIn("messages", captured["request"]["json"])
        self.assertEqual(captured["client"]["impersonate"], "chrome123")
        self.assertEqual(captured["client"]["proxies"]["https"], "http://localhost:8080")
        self.assertTrue(captured["closed"])

    async def test_cancelled_refresh_preserves_old_snapshot_and_is_not_stuck_busy(self):
        before = (await self.service.refresh())["accounts"][0]
        self.now += 61
        started = asyncio.Event()
        async def fetch(*args):
            started.set()
            await asyncio.Event().wait()
        self.service._fetcher = fetch
        pending = asyncio.create_task(self.service.refresh())
        await started.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        row = self.service.get_accounts()["accounts"][0]
        self.assertEqual(row["checked_at"], before["checked_at"])
        self.assertEqual(row["status"], "stale")
        self.assertEqual(row["refresh"]["status"], "failed")
        self.assertEqual(row["refresh"]["error"]["code"], "cancelled")


if __name__ == "__main__":
    unittest.main()
