"""Offline admission/policy tests: no live credentials or upstream requests."""
import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from chatgpt.account_concurrency import (ConcurrencyStore, ConcurrencyConflict,
    ConcurrencyValidationError, ConcurrencyPersistenceError, credential_alias)
from chatgpt.account_scheduler import AccountScheduler, AccountConcurrencyExhausted, ModelNotAvailable
from test_account_scheduler import A, B, C, account, Quotas


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "policy.json"
        self.store = ConcurrencyStore(self.path)

    def test_defaults_inheritance_partial_updates_and_revision(self):
        initial = self.store.snapshot((A, B))
        self.assertEqual(initial["defaults"], {"reason": 3, "image_gen": 1})
        changed = self.store.update(expected_revision=0, account_ids=[A], overrides={"reason": 2})
        self.store.update(expected_revision=changed["revision"], defaults={"reason": 5, "image_gen": 2})
        self.assertEqual(self.store.limits(A)["effective"], {"reason": 2, "image_gen": 2})
        self.assertEqual(self.store.limits(B)["effective"], {"reason": 5, "image_gen": 2})
        revision = self.store.snapshot()["revision"]
        self.store.update(expected_revision=revision, account_ids=[A], overrides={"reason": None})
        self.assertEqual(self.store.limits(A)["effective"]["reason"], 5)
        with self.assertRaises(ConcurrencyConflict):
            self.store.update(expected_revision=revision, defaults={"reason": 6})

    def test_validation_is_strict_and_bulk_is_atomic(self):
        before = self.store.snapshot((A,))
        for value in (0, 11, -1, True, 1.0, "2", None):
            with self.subTest(value=value), self.assertRaises(ConcurrencyValidationError):
                self.store.update(expected_revision=0, defaults={"reason": value})
        for value in (0, 11, False, 1.1, "1"):
            with self.subTest(value=value), self.assertRaises(ConcurrencyValidationError):
                self.store.update(expected_revision=0, account_ids=[A], overrides={"image_gen": value})
        with self.assertRaises(ConcurrencyValidationError):
            self.store.update(expected_revision=0, account_ids=[A, "not-an-id"], overrides={"reason": 1})
        self.assertEqual(self.store.snapshot((A,)), before)
        self.assertFalse(self.path.exists())

    def test_private_atomic_persistence_and_no_secret_in_store(self):
        secret = "synthetic-private-credential"
        self.store.bind_alias(A, credential_alias(secret))
        self.store.update(expected_revision=0, account_ids=[A], overrides={"image_gen": 2})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn(secret, self.path.read_text())
        self.assertNotIn("cred_", json.dumps(self.store.snapshot((A,))))
        self.assertEqual(ConcurrencyStore(self.path).snapshot((A,)), self.store.snapshot((A,)))
        before = self.store.snapshot((A,))
        with patch("chatgpt.account_concurrency.os.replace", side_effect=OSError), self.assertRaises(ConcurrencyPersistenceError):
            self.store.update(expected_revision=before["revision"], defaults={"reason": 10})
        self.assertEqual(self.store.snapshot((A,)), before)
        self.assertEqual(ConcurrencyStore(self.path).snapshot((A,)), before)

    def test_corrupt_configuration_fails_closed(self):
        self.path.write_text('{"version":1,"defaults":{"reason":1000}}')
        broken = ConcurrencyStore(self.path)
        with self.assertRaises(ConcurrencyPersistenceError):
            broken.snapshot((A,))

    def test_identity_merge_uses_stricter_limits_and_reports_conflict(self):
        alias = credential_alias("synthetic-refresh")
        self.store.bind_alias(A, alias)
        self.store.update(expected_revision=0, account_ids=[A], overrides={"reason": 2, "image_gen": 3})
        revision = self.store.snapshot()["revision"]
        self.store.update(expected_revision=revision, account_ids=[B], overrides={"reason": 5, "image_gen": 1})
        self.assertEqual(self.store.bind_alias(B, alias), ((A, B),))
        self.assertEqual(self.store.resolve(A), B)
        policy = self.store.limits(A)
        self.assertEqual(policy["effective"], {"reason": 2, "image_gen": 1})
        self.assertTrue(policy["conflict"])
        self.assertNotIn(A, self.store.snapshot()["accounts"])
        restarted = ConcurrencyStore(self.path)
        self.assertEqual(restarted.resolve(A), B)
        self.assertEqual(restarted.limits(B), policy)


class CapacityTests(unittest.IsolatedAsyncioTestCase):
    def make(self, *rows, **kwargs):
        self.quota = Quotas(*(rows or (account(A, 100, 100),)))
        self.scheduler = AccountScheduler(self.quota, state_path=None, **kwargs)
        return self.scheduler

    async def test_chat_and_image_slots_are_independent_from_quota_reservations(self):
        scheduler = self.make()
        text = [await scheduler.acquire("reason") for _ in range(3)]
        image = await scheduler.acquire("image_gen")
        with self.assertRaises(AccountConcurrencyExhausted): await scheduler.acquire("reason")
        with self.assertRaises(AccountConcurrencyExhausted): await scheduler.acquire("image_gen")
        row = scheduler.snapshot()["accounts"][A]
        self.assertEqual(row["reason"]["active"], 3)
        self.assertEqual(row["image_gen"]["active"], 1)
        self.assertEqual(row["reason"]["reserved"], 4)
        self.assertEqual(row["image_gen"]["reserved"], 1)
        await scheduler.finish(image, status="cancelled", dispatched=True)
        await scheduler.finish(image, status="succeeded", dispatched=True)
        image2 = await scheduler.acquire("image_gen")
        for lease in (*text, image2): await scheduler.finish(lease, status="succeeded")
        self.assertEqual(scheduler.snapshot()["accounts"][A]["active_requests"], 0)

    async def test_many_simultaneous_acquires_do_not_oversubscribe_and_switch(self):
        scheduler = self.make(account(A, 100, 100), account(B, 100, 100))
        outcomes = await asyncio.gather(*(scheduler.acquire("reason") for _ in range(20)), return_exceptions=True)
        leases = [item for item in outcomes if not isinstance(item, Exception)]
        self.assertEqual(len(leases), 6)
        self.assertEqual({key: sum(lease.account_id == key for lease in leases) for key in (A, B)}, {A: 3, B: 3})
        self.assertTrue(all(isinstance(item, AccountConcurrencyExhausted) for item in outcomes if isinstance(item, Exception)))

    async def test_lowering_drains_without_cancelling_and_raising_is_immediate(self):
        scheduler = self.make()
        leases = [await scheduler.acquire() for _ in range(3)]
        changed = await scheduler.update_concurrency(expected_revision=0, account_ids=[A], overrides={"reason": 1})
        self.assertEqual(scheduler.snapshot()["accounts"][A]["concurrency"]["reason"]["state"], "draining")
        await scheduler.confirm_identity(leases[0], A)
        for lease in leases[:2]: await scheduler.finish(lease, status="succeeded")
        with self.assertRaises(AccountConcurrencyExhausted): await scheduler.acquire()
        await scheduler.update_concurrency(expected_revision=changed["revision"], account_ids=[A], overrides={"reason": 2})
        additional = await scheduler.acquire()
        self.assertEqual(scheduler.snapshot()["accounts"][A]["concurrency"]["reason"]["active"], 2)
        await scheduler.finish(additional, status="cancelled")
        await scheduler.finish(leases[2], status="succeeded")

    async def test_direct_and_native_share_capacity_without_web_quota_debits(self):
        scheduler = self.make()
        selected = self.quota._groups()[A]["token"]
        direct = await scheduler.acquire_for_account(selected, "image_gen", account_id=A, channel="codex_native", model="gpt-image-2")
        with self.assertRaises(AccountConcurrencyExhausted): await scheduler.acquire("image_gen")
        row = scheduler.snapshot()["accounts"][A]
        self.assertEqual(row["image_gen"]["reserved"], 0)
        self.assertEqual(row["image_gen"]["active"], 1)
        await scheduler.finish(direct, status="succeeded", dispatched=True, image_requested=True)
        row = scheduler.snapshot()["accounts"][A]
        self.assertEqual(row["reason"]["provisional"], 0)
        self.assertEqual(row["image_gen"]["provisional"], 0)

    async def test_model_filter_does_not_route_to_unverified_account(self):
        scheduler = self.make(account(A, 100, 100), account(B, 10, 10),
                              model_support=lambda key, model, **kwargs: key == B and model == "precise-model")
        lease = await scheduler.acquire(model="precise-model")
        self.assertEqual(lease.account_id, B)
        with self.assertRaises(ModelNotAvailable): await scheduler.acquire(model="unknown-model")

    async def test_supported_but_quota_empty_is_not_misreported_as_unknown_model(self):
        from chatgpt.account_scheduler import NoAvailableAccount
        scheduler = self.make(account(A, 0, 0), account(B, 100, 100),
            model_support=lambda key, model, **kwargs: key == A)
        with self.assertRaises(NoAvailableAccount) as error:
            await scheduler.acquire(model="supported-model")
        self.assertNotIsInstance(error.exception, ModelNotAvailable)
        self.assertEqual(error.exception.code, "no_available_account")

    async def test_refresh_identity_migrates_active_leases_and_limit_without_releasing_twice(self):
        scheduler = self.make()
        selected = self.quota._groups()[A]["token"]
        lease = await scheduler.acquire()
        await scheduler.update_concurrency(expected_revision=0, account_ids=[A], overrides={"reason": 1})
        self.quota.accounts = {B: account(B, 100, 100)}
        self.quota._groups = lambda: {B: {"token": selected}}
        await scheduler.synchronize_identities()
        row = scheduler.snapshot()["accounts"][B]
        self.assertEqual(row["concurrency"]["reason"]["active"], 1)
        self.assertEqual(row["concurrency"]["reason"]["limit"], 1)
        with self.assertRaises(AccountConcurrencyExhausted): await scheduler.acquire()
        await scheduler.finish(lease, status="succeeded", dispatched=True)
        await scheduler.finish(lease, status="succeeded", dispatched=True)
        row = scheduler.snapshot()["accounts"][B]
        self.assertEqual(row["reason"]["provisional"], 1)
        self.assertEqual(row["concurrency"]["reason"]["active"], 0)

    async def test_identity_confirmation_rejects_newly_merged_excess_before_dispatch(self):
        scheduler = self.make(account(A, 100, 100), account(B, 100, 100))
        await scheduler.update_concurrency(expected_revision=0, account_ids=[A, B], overrides={"reason": 1})
        old = await scheduler.acquire(preferred_account_id=A)
        existing = await scheduler.acquire(preferred_account_id=B)
        with self.assertRaises(AccountConcurrencyExhausted):
            await scheduler.confirm_identity(old, B)
        await scheduler.finish(old, status="failed")
        self.assertEqual(scheduler._capacity(B, "reason")["active"], 1)
        await scheduler.finish(existing, status="succeeded")

    async def test_real_refresh_claim_identity_and_duplicate_credentials_share_policy(self):
        from chatgpt.account_quota import AccountQuotaService, account_identity
        claims = {"exp": 4102444800, "https://api.openai.com/auth": {
            "chatgpt_account_id": "synthetic-workspace", "chatgpt_user_id": "synthetic-user"}}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        access = "header." + payload + ".synthetic-signature"
        duplicate = "header." + payload + ".other-synthetic-signature"
        refresh = "r" * 45
        tokens, cache = [refresh], {}
        quota = AccountQuotaService(lambda: tokens, refresh_map_source=lambda: cache, cache_path=None)
        scheduler = AccountScheduler(quota, state_path=None)
        pending = await scheduler.acquire()
        old = pending.account_id
        await scheduler.update_concurrency(expected_revision=0, account_ids=[old], overrides={"reason": 2})
        canonical = account_identity(access)["id"]
        one = await scheduler.acquire_for_account(access, account_id=canonical)
        two = await scheduler.acquire_for_account(duplicate, account_id=canonical)
        cache[refresh] = {"token": access}
        tokens.append(duplicate)
        await scheduler.synchronize_identities()
        self.assertEqual(len(quota.get_accounts()["accounts"]), 1)
        self.assertEqual(scheduler.concurrency.resolve(old), canonical)
        self.assertEqual(scheduler._capacity(canonical, "reason")["active"], 3)
        self.assertEqual(scheduler._capacity(canonical, "reason")["limit"], 2)
        with self.assertRaises(AccountConcurrencyExhausted):
            await scheduler.confirm_identity(pending, canonical)
        for lease in (pending, one, two): await scheduler.finish(lease, status="cancelled")
        self.assertEqual(scheduler._capacity(canonical, "reason")["active"], 0)

    async def test_restart_restores_policy_but_not_running_slots_or_native_web_debits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheduler.json"
            quota = Quotas(account(A, 100, 100))
            scheduler = AccountScheduler(quota, state_path=path)
            await scheduler.update_concurrency(expected_revision=0, account_ids=[A], overrides={"image_gen": 2})
            await scheduler.acquire_for_account(quota._groups()[A]["token"], "image_gen", account_id=A, channel="codex_native")
            restarted = AccountScheduler(quota, state_path=path)
            row = restarted.snapshot()["accounts"][A]
            self.assertEqual(row["concurrency"]["image_gen"]["limit"], 2)
            self.assertEqual(row["concurrency"]["image_gen"]["active"], 0)
            self.assertEqual(row["image_gen"]["provisional"], 0)
