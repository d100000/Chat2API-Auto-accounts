"""Private profile metadata tests using synthetic JWTs and isolated quota state."""

import base64
from copy import deepcopy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from chatgpt.account_quota import (AccountQuotaService, QuotaReadError, STALE_AFTER,
                                   _identity, _iso, account_identity, get_account_profiles)
from chatgpt.account_scheduler import AccountScheduler
from chatgpt.model_catalog import ModelCatalog
from chatgpt.account_usage import UsageStore, start_trace

NOW = 1800000000
EMAIL = "person+local@example.invalid"
OTHER_EMAIL = "other@example.invalid"


def token(*, email=EMAIL, plan="plus", account="synthetic-account", user="synthetic-user",
          issued=NOW - 600, expiry=NOW + 3600, nonce="first", **extra):
    claims = {"https://api.openai.com/profile": {"email": email},
              "https://api.openai.com/auth": {"chatgpt_account_id": account,
                    "chatgpt_user_id": user, "chatgpt_plan_type": plan},
              "iat": issued, "exp": expiry, "nonce": nonce, **extra}
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "none"}) + "." + encode(claims) + ".synthetic"


def quota():
    return {"limits_progress": [{"feature_name": "reason", "remaining": 50},
                                {"feature_name": "image_gen", "remaining": 25}],
            "model_limits": [], "blocked_features": []}


class AccountProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="private-profile-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "quota.json"
        self.now = NOW
        self.tokens = [token()]
        self.refresh_map = {}
        self.errors = []
        self.fetch = AsyncMock(side_effect=lambda *_: quota())
        self.service = self.make_service()

    def make_service(self):
        return AccountQuotaService(lambda: tuple(self.tokens),
            refresh_map_source=lambda: deepcopy(self.refresh_map),
            error_token_source=lambda: tuple(self.errors),
            cache_path=self.path, clock=lambda: self.now, fetcher=self.fetch)

    def profile(self):
        return next(iter(self.service.get_account_profiles().values()))

    async def test_namespace_fields_are_private_unverified_claims_without_network(self):
        profile = self.profile()
        self.assertEqual(profile["email"], EMAIL)
        self.assertEqual(profile["plan"], "plus")
        self.assertEqual(profile["email_source"], "jwt_profile")
        self.assertEqual(profile["plan_source"], "jwt_auth")
        self.assertEqual(profile["email_trust"], "unverified_claim")
        self.assertEqual(profile["plan_trust"], "unverified_claim")
        self.assertFalse(profile["claims_verified"])
        self.assertEqual(profile["authorization_status"], "unverified")
        self.assertIsNone(profile["last_verified_at"])
        self.assertEqual(profile["token_type"], "access_token")
        self.assertEqual(profile["token_type_source"], "local_format")
        self.assertEqual(profile["auth_method"], "bearer_token")
        self.fetch.assert_not_called()
        self.assertFalse(self.path.exists())

    async def test_top_level_email_fallback_and_namespace_priority(self):
        self.tokens = [token(**{"email": EMAIL})]
        # Top-level claims are supplied separately from the helper's profile field.
        original = self.tokens[0]
        payload = json.loads(base64.urlsafe_b64decode(original.split(".")[1] + "=" * (-len(original.split(".")[1]) % 4)))
        payload["email"] = OTHER_EMAIL
        self.tokens = [self.encode(payload)]
        self.assertEqual(self.profile()["email"], EMAIL)
        payload["https://api.openai.com/profile"] = {"email": "not-an-email"}
        self.tokens = [self.encode(payload)]
        self.assertEqual(self.profile()["email"], OTHER_EMAIL)
        self.assertEqual(self.profile()["email_source"], "jwt_top_level")
        payload["https://api.openai.com/profile"] = ["bad"]
        self.tokens = [self.encode(payload)]
        self.assertEqual(self.profile()["email"], OTHER_EMAIL)

    @staticmethod
    def encode(payload):
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        return encode({"alg": "none"}) + "." + encode(payload) + ".synthetic"

    async def test_plan_normalization_is_explicit_and_unknown_is_not_free(self):
        for expected in ("free", "plus", "pro", "team", "business", "enterprise", "edu", "go"):
            with self.subTest(plan=expected):
                self.tokens = [token(plan=" " + expected.upper() + " ")]
                self.assertEqual(self.profile()["plan"], expected)
        for value in (None, True, 10, {}, [], "premium", "chatgpt_plus", "pro_trial", "x" * 1000):
            with self.subTest(plan=value):
                self.tokens = [token(plan=value)]
                self.assertEqual(self.profile()["plan"], "unknown")
        self.tokens = [token(plan=None)]
        await self.service.refresh()
        self.assertEqual(self.profile()["plan"], "unknown")
        self.assertIsNone(self.profile()["plan_source"])

    async def test_malformed_email_and_arbitrary_claims_are_not_returned(self):
        for value in (None, {}, [], True, "", "not-email", "<script>@example.invalid",
                      "a\n@example.invalid", "a\x00@example.invalid", "a\u202e@example.invalid",
                      "a..b@example.invalid", "a@bad..invalid", "a@-bad.invalid",
                      "a" * 65 + "@example.invalid", "a@" + "b" * 64 + ".invalid", self.tokens[0]):
            with self.subTest(email=str(value)[:30]):
                self.tokens = [token(email=value, access_token="PRIVATE-TOKEN", session_key="PRIVATE-SESSION")]
                profile = self.profile()
                self.assertIsNone(profile["email"])
                self.assertEqual(profile["email_trust"], "unknown")
                output = json.dumps(profile)
                self.assertNotIn("PRIVATE", output)
                self.assertNotIn(self.tokens[0], output)
                self.assertNotIn("synthetic-account", output)
                self.assertNotIn("synthetic-user", output)

    async def test_issue_time_expiry_and_auth_time_are_not_conflated(self):
        self.tokens = [token(issued=NOW - 120, auth_time=NOW - 5000)]
        profile = self.profile()
        self.assertEqual(profile["issued_at"], _iso(NOW - 120))
        self.assertEqual(profile["issued_at_source"], "jwt_iat")
        self.assertEqual(profile["expires_at"], _iso(NOW + 3600))
        self.assertEqual(profile["expires_at_source"], "jwt_exp")
        self.assertEqual(profile["expiry_subject"], "access_token")
        for value in (None, True, -1, "1800000000", float("nan"), float("inf")):
            self.tokens = [token(issued=value)]
            self.assertIsNone(self.profile()["issued_at"])
            self.assertIsNone(self.profile()["issued_at_source"])

    async def test_workspace_override_cannot_borrow_default_workspace_plan(self):
        self.tokens = [token() + ",different-workspace"]
        profile = self.profile()
        self.assertEqual(profile["email"], EMAIL)
        self.assertEqual(profile["plan"], "unknown")
        self.assertEqual(profile["plan_scope"], "workspace_mismatch")
        self.assertIsNone(profile["plan_source"])
        self.tokens = [token() + ",synthetic-account"]
        self.assertEqual(self.profile()["plan"], "plus")
        self.tokens = [token(account=None) + ",workspace"]
        self.assertEqual(self.profile()["plan"], "unknown")

    async def test_multiple_credentials_profile_uses_the_same_selected_token(self):
        self.tokens = [token(email=EMAIL, plan="free", issued=NOW - 600, expiry=NOW + 60),
                       token(email=OTHER_EMAIL, plan="pro", issued=NOW - 300, expiry=NOW + 9000, nonce="second")]
        groups = self.service.get_accounts()["accounts"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["token_count"], 2)
        profile = self.profile()
        self.assertEqual(profile["email"], OTHER_EMAIL)
        self.assertEqual(profile["plan"], "pro")
        self.assertEqual(profile["issued_at"], _iso(NOW - 300))
        self.assertEqual(profile["expires_at"], groups[0]["credential_expires_at"])
        await self.service.refresh()
        self.assertEqual(self.fetch.call_args.args[0], self.tokens[1])
        self.assertEqual(self.profile()["last_verified_at"], _iso(NOW))
        self.errors = [self.tokens[1]]
        self.assertEqual(self.profile()["email"], EMAIL)
        self.assertIsNone(self.profile()["last_verified_at"])

    async def test_refresh_credential_reports_access_expiry_without_claiming_refresh_lifetime(self):
        refresh = "R" * 45
        access = token()
        self.tokens = [refresh]
        self.refresh_map = {refresh: {"token": access, "timestamp": NOW - 100}}
        profile = self.profile()
        self.assertEqual(profile["token_type"], "refresh_token")
        self.assertEqual(profile["auth_method"], "refresh_token")
        self.assertEqual(profile["email"], EMAIL)
        self.assertEqual(profile["expires_at"], _iso(NOW + 3600))
        self.assertEqual(profile["expiry_subject"], "access_token")
        self.assertIsNone(profile["last_verified_at"])
        await self.service.refresh()
        self.assertEqual(self.profile()["last_verified_at"], _iso(NOW))
        self.refresh_map = {refresh: {"token": token(nonce="rotated"), "timestamp": NOW}}
        self.assertIsNone(self.profile()["last_verified_at"])

    async def test_opaque_and_uncached_refresh_tokens_remain_unknown(self):
        for credential, expected in (("opaque-synthetic-session", "opaque"), ("R" * 45, "refresh_token")):
            self.tokens = [credential]
            profile = self.profile()
            self.assertEqual(profile["token_type"], expected)
            self.assertIsNone(profile["email"])
            self.assertEqual(profile["plan"], "unknown")
            self.assertIsNone(profile["issued_at"])
            self.assertIsNone(profile["expires_at"])
            self.assertIsNone(profile["last_verified_at"])
        self.fetch.assert_not_called()

    async def test_quota_success_verifies_current_credential_but_not_profile_claims(self):
        await self.service.refresh()
        profile = self.profile()
        self.assertEqual(profile["last_verified_at"], _iso(NOW))
        self.assertEqual(profile["verification_source"], "quota_endpoint")
        self.assertEqual(profile["authorization_status"], "verified_recently")
        self.assertFalse(profile["claims_verified"])
        self.assertEqual(profile["email_trust"], "unverified_claim")
        self.assertEqual(profile["plan_trust"], "unverified_claim")
        self.now += STALE_AFTER
        self.assertEqual(self.profile()["authorization_status"], "verification_stale")

    async def test_failure_retains_historical_verification_but_does_not_show_recently_valid(self):
        await self.service.refresh()
        self.now += 61
        self.fetch.side_effect = QuotaReadError("upstream_http", 401)
        await self.service.refresh()
        profile = self.profile()
        self.assertEqual(profile["last_verified_at"], _iso(NOW))
        self.assertEqual(profile["authorization_status"], "error")
        self.tokens = [token(nonce="new-current-credential")]
        self.assertEqual(self.profile()["authorization_status"], "unverified")
        self.assertIsNone(self.profile()["last_verified_at"])

    async def test_expiry_or_error_marker_overrides_success_status(self):
        self.tokens = [token(expiry=NOW + 60)]
        await self.service.refresh()
        self.now += 61
        self.assertEqual(self.profile()["authorization_status"], "expired")
        self.assertEqual(self.profile()["last_verified_at"], _iso(NOW))
        self.now = NOW
        self.errors = [self.tokens[0]]
        self.assertEqual(self.profile()["authorization_status"], "error")

    async def test_old_cache_without_credential_binding_never_claims_verification(self):
        await self.service.refresh()
        document = json.loads(self.path.read_text())
        for row in document["accounts"].values():
            row.pop("verified_credential_fingerprint", None)
            row.pop("attempted_credential_fingerprint", None)
        self.path.write_text(json.dumps(document))
        self.service = self.make_service()
        self.assertIsNotNone(self.service.get_accounts()["accounts"][0]["checked_at"])
        self.assertIsNone(self.profile()["last_verified_at"])
        self.assertEqual(self.profile()["authorization_status"], "unverified")

    async def test_cache_persists_only_hash_binding_and_is_private_and_restart_safe(self):
        await self.service.refresh()
        saved = self.path.read_text()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn(EMAIL, saved)
        self.assertNotIn(self.tokens[0], saved)
        self.assertNotIn('"profile"', saved)
        self.assertNotIn('"plan"', saved)
        self.assertNotIn("https://api.openai.com/auth", saved)
        row = next(iter(json.loads(saved)["accounts"].values()))
        self.assertRegex(row["verified_credential_fingerprint"], r"^[0-9a-f]{64}$")
        self.service = self.make_service()
        self.assertEqual(self.profile()["last_verified_at"], _iso(NOW))
        self.assertEqual(self.profile()["email"], EMAIL)

    async def test_bad_or_future_cache_verification_is_ignored(self):
        await self.service.refresh()
        document = json.loads(self.path.read_text())
        for row in document["accounts"].values():
            row["verified_credential_fingerprint"] = self.tokens[0]
        self.path.write_text(json.dumps(document))
        self.service = self.make_service()
        self.assertIsNone(self.profile()["last_verified_at"])
        for row in document["accounts"].values():
            row["checked_at"] = NOW + 1000
        self.path.write_text(json.dumps(document))
        self.service = self.make_service()
        self.assertIsNone(self.profile()["last_verified_at"])

    async def test_identity_quota_scheduler_and_public_catalog_never_contain_profile(self):
        identity = account_identity(self.tokens[0])
        self.assertEqual(set(identity), {"id", "label", "identity_source", "credential_status", "credential_expires_at"})
        self.assertEqual(identity["id"], _identity(self.tokens[0])["id"])
        await self.service.refresh()
        scheduler = AccountScheduler(self.service, state_path=None)
        snapshot = scheduler.snapshot(self.service.get_accounts())
        catalog = ModelCatalog(lambda: tuple(self.tokens), cache_path=None, clock=lambda: self.now,
                               fetcher=AsyncMock(return_value={"models": [{"slug": "gpt-6"}]}), refresh_spacing=0)
        await catalog.refresh_catalog()
        public = await catalog.get_catalog()
        # This includes actual public discovery and scheduler outputs, not only
        # the quota service's internal identity helper.
        serialized = json.dumps([identity, self.service.get_accounts(), snapshot, public])
        for value in (EMAIL, self.tokens[0], "email_source", "email_trust", "jwt_profile"):
            self.assertNotIn(value, serialized)
        self.assertIn(EMAIL, json.dumps(self.service.get_account_profiles()))
        self.assertNotIn("verified_credential_fingerprint", json.dumps(self.service.get_accounts()))

    async def test_untrusted_or_compatibility_upstream_profile_does_not_replace_claims(self):
        raw = {**quota(), "profile": {"email": OTHER_EMAIL, "plan": "free"},
               "account": {"plan_type": "pro"}, "email": OTHER_EMAIL,
               "access_token": "PRIVATE", "sessionToken": "PRIVATE-SESSION"}
        self.fetch.side_effect = lambda *_: raw
        await self.service.refresh()
        profile = self.profile()
        self.assertEqual(profile["email"], EMAIL)
        self.assertEqual(profile["plan"], "plus")
        self.assertEqual(profile["email_source"], "jwt_profile")
        self.assertNotIn(OTHER_EMAIL, self.path.read_text())
        self.assertNotIn("PRIVATE", self.path.read_text())

    async def test_usage_ledger_only_receives_anonymous_identity(self):
        path = Path(self.directory.name) / "usage.sqlite3"
        store = UsageStore(path)
        with patch("chatgpt.account_usage.get_store", return_value=store):
            trace = await start_trace(self.tokens[0], "auto")
            await trace.finish("cancelled", "local_test")
        with store.connect() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM attempts")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account_id"], account_identity(self.tokens[0])["id"])
        serialized = json.dumps(rows)
        for private in (EMAIL, self.tokens[0], "jwt_profile", "chatgpt_plan_type"):
            self.assertNotIn(private, serialized)
            self.assertNotIn(private.encode(), path.read_bytes())
        self.fetch.assert_not_called()

    async def test_module_function_delegates_only_to_private_profile_method(self):
        with patch("chatgpt.account_quota._default_service", return_value=self.service):
            self.assertEqual(get_account_profiles(), self.service.get_account_profiles())
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
