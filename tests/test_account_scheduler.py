import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from chatgpt.account_scheduler import AccountScheduler, NoAvailableAccount


A = "acct_" + "a" * 24
B = "acct_" + "b" * 24
C = "acct_" + "c" * 24


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=1):
        self.now += seconds


def account(key, reason=10, image=5, *, checked=900, started=899, status="fresh", reset=2000, credential="valid"):
    def feature(value):
        return {"remaining": value, "blocked": False, "reset_at": iso(reset) if reset is not None else None,
                "status": "known" if value is not None else "unknown"}
    return {"id": key, "credential_status": credential, "status": status, "checked_at": iso(checked),
            "features": {"reason": feature(reason), "image_gen": feature(image)},
            "refresh": {"status": "refreshed", "last_attempt_at": iso(started), "error": None}}


class Quotas:
    def __init__(self, *accounts):
        self.accounts = {row["id"]: deepcopy(row) for row in accounts}

    def get_accounts(self):
        return {"accounts": list(deepcopy(self.accounts).values())}

    def _groups(self):
        return {key: {"token": "private-test-credential-" + key} for key in self.accounts}

    def read(self, key, clock, reason=10, image=5, *, started=None, reset=2000):
        self.accounts[key] = account(key, reason, image, checked=clock(), started=clock() if started is None else started, reset=reset)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "scheduler.json"

    def tearDown(self):
        self.directory.cleanup()

    def scheduler(self, *rows, persist=False):
        self.quota = Quotas(*rows)
        return AccountScheduler(self.quota, state_path=self.path if persist else None, clock=self.clock)

    async def test_exhausted_feature_switches_without_disabling_text(self):
        scheduler = self.scheduler(account(A, 10, 0), account(B, 5, 4))
        image = await scheduler.acquire("image_gen")
        self.assertEqual(image.account_id, B)
        text = await scheduler.acquire("reason")
        self.assertEqual(text.account_id, A)
        states = scheduler.snapshot()["accounts"]
        self.assertFalse(states[A]["image_gen"]["available"])
        self.assertTrue(states[A]["reason"]["available"])

    async def test_image_requires_reason_and_image_capacity(self):
        scheduler = self.scheduler(account(A, 0, 100), account(B, 5, 0), account(C, 2, 2))
        lease = await scheduler.acquire("image_gen")
        self.assertEqual(lease.account_id, C)
        view = scheduler.snapshot()["accounts"]
        self.assertFalse(view[A]["image_gen"]["available"])
        self.assertFalse(view[B]["image_gen"]["available"])
        self.assertEqual(view[C]["reason"]["reserved"], 1)
        self.assertEqual(view[C]["image_gen"]["reserved"], 1)

    async def test_fresh_known_capacity_wins_over_unknown_or_stale(self):
        scheduler = self.scheduler(account(A, None, None, status="unknown"),
                                   account(B, 100, 100, status="stale"), account(C, 1, 1))
        self.assertEqual((await scheduler.acquire()).account_id, C)

    async def test_concurrent_reservations_do_not_oversubscribe_known_slots(self):
        scheduler = self.scheduler(account(A, 1), account(B, 1))
        outcomes = await asyncio.gather(*(scheduler.acquire() for _ in range(10)), return_exceptions=True)
        leases = [item for item in outcomes if not isinstance(item, Exception)]
        failures = [item for item in outcomes if isinstance(item, NoAvailableAccount)]
        self.assertEqual({lease.account_id for lease in leases}, {A, B})
        self.assertEqual(len(failures), 8)
        self.assertTrue(any(event["type"] == "switched" for event in scheduler.snapshot()["events"]))

    async def test_unknown_is_single_flight_and_not_zero(self):
        scheduler = self.scheduler(account(A, None, None, status="unknown"))
        before = scheduler.snapshot()["accounts"][A]["reason"]
        self.assertEqual(before["state"], "unknown")
        self.assertIsNone(before["effective_remaining"])
        lease = await scheduler.acquire()
        with self.assertRaises(NoAvailableAccount):
            await scheduler.acquire()
        await scheduler.finish(lease, status="failed", dispatched=False)
        self.assertEqual((await scheduler.acquire()).account_id, A)

    async def test_excluded_and_preferred_never_silently_choose_other_identity(self):
        scheduler = self.scheduler(account(A), account(B))
        self.assertEqual((await scheduler.acquire(excluded={B})).account_id, A)
        with self.assertRaises(NoAvailableAccount):
            await scheduler.acquire(excluded={B}, preferred_account_id=B)

    async def test_credential_error_and_expired_are_not_candidates(self):
        scheduler = self.scheduler(account(A, credential="error"), account(B, credential="expired"))
        with self.assertRaises(NoAvailableAccount):
            await scheduler.acquire()
        state = scheduler.snapshot()["accounts"]
        self.assertEqual(state[A]["reason"]["state"], "auth_error")
        self.assertEqual(state[B]["reason"]["state"], "expired")

    async def test_past_reset_does_not_replenish_until_new_positive_query(self):
        scheduler = self.scheduler(account(A, 0, reset=1010), persist=True)
        self.assertEqual(scheduler.snapshot()["accounts"][A]["reason"]["state"], "exhausted")
        self.clock.advance(11)
        self.quota.accounts[A]["status"] = "stale"
        self.quota.accounts[A]["features"]["reason"]["status"] = "expired"
        with self.assertRaises(NoAvailableAccount):
            await scheduler.acquire()
        self.assertEqual(scheduler.snapshot()["accounts"][A]["reason"]["state"], "reset_pending")
        # A read begun before reset cannot establish the new window.
        self.quota.read(A, self.clock, reason=10, started=1009)
        with self.assertRaises(NoAvailableAccount):
            await scheduler.acquire()
        self.clock.advance()
        self.quota.read(A, self.clock, reason=10)
        self.assertEqual((await scheduler.acquire()).account_id, A)
        self.assertTrue(any(row["type"] == "rejoined" for row in scheduler.snapshot()["events"]))

    async def test_known_exhaustion_remains_blocked_when_snapshot_becomes_unknown(self):
        scheduler = self.scheduler(account(A, 0, reset=None))
        scheduler.snapshot()
        self.clock.advance()
        self.quota.read(A, self.clock, reason=None)
        with self.assertRaises(NoAvailableAccount):
            await scheduler.acquire()

    async def test_debit_requires_read_started_strictly_after_finish(self):
        scheduler = self.scheduler(account(A, 1))
        lease = await scheduler.acquire()
        self.clock.advance(1)
        await scheduler.finish(lease, status="succeeded", dispatched=True)
        with self.assertRaises(NoAvailableAccount):
            await scheduler.acquire()
        self.quota.read(A, self.clock, reason=1, started=1000)
        self.assertEqual(scheduler.snapshot()["accounts"][A]["reason"]["provisional"], 1)
        self.quota.read(A, self.clock, reason=1)  # Equal timestamp is not after.
        self.assertEqual(scheduler.snapshot()["accounts"][A]["reason"]["provisional"], 1)
        self.clock.advance()
        self.quota.read(A, self.clock, reason=1)
        self.assertEqual(scheduler.snapshot()["accounts"][A]["reason"]["provisional"], 0)
        self.assertEqual((await scheduler.acquire()).account_id, A)

    async def test_failed_query_cannot_clear_debit_with_old_checked_timestamp(self):
        scheduler = self.scheduler(account(A, 1))
        lease = await scheduler.acquire()
        await scheduler.finish(lease, status="failed", dispatched=True)
        self.clock.advance()
        row = self.quota.accounts[A]
        row["refresh"] = {"last_attempt_at": iso(self.clock()), "status": "failed", "error": {"code": "network_error"}}
        self.assertEqual(scheduler.snapshot()["accounts"][A]["reason"]["provisional"], 1)

    async def test_cancelled_dispatch_is_debited_and_finish_is_idempotent(self):
        scheduler = self.scheduler(account(A, 5, 5))
        lease = await scheduler.acquire("image_gen")
        await scheduler.finish(lease, status="cancelled", dispatched=True)
        await scheduler.finish(lease, status="succeeded", dispatched=True)
        view = scheduler.snapshot()["accounts"][A]
        self.assertEqual(view["active_requests"], 0)
        self.assertEqual(view["reason"]["provisional"], 1)
        self.assertEqual(view["image_gen"]["provisional"], 1)

    async def test_discovered_image_request_debits_both_features_once(self):
        scheduler = self.scheduler(account(A, 5, 5))
        lease = await scheduler.acquire("reason")
        await scheduler.finish(lease, status="succeeded", dispatched=True, image_requested=True)
        view = scheduler.snapshot()["accounts"][A]
        self.assertEqual(view["reason"]["effective_remaining"], 4)
        self.assertEqual(view["image_gen"]["effective_remaining"], 4)
        self.assertEqual(self.quota.accounts[A]["features"]["image_gen"]["remaining"], 5)

    async def test_text_activity_does_not_count_as_active_image_generation(self):
        scheduler = self.scheduler(account(A, 5, 5))
        await scheduler.acquire("reason")
        view = scheduler.snapshot()["accounts"][A]
        self.assertEqual(view["active_requests"], 1)
        self.assertEqual(view["reason"]["active"], 1)
        self.assertEqual(view["image_gen"]["active"], 0)

    async def test_malformed_error_kind_is_discarded_and_lease_still_released(self):
        scheduler = self.scheduler(account(A, 5, 5))
        lease = await scheduler.acquire()
        await scheduler.finish(lease, status="failed", error={"kind": ["quota"], "feature": {"untrusted": True}})
        view = scheduler.snapshot()["accounts"][A]
        self.assertEqual(view["active_requests"], 0)
        self.assertTrue(view["reason"]["available"])

    async def test_typed_image_quota_error_only_blocks_images(self):
        scheduler = self.scheduler(account(A, 5, 5))
        lease = await scheduler.acquire("image_gen")
        await scheduler.finish(lease, status="failed", dispatched=True, error={"kind": "quota", "feature": "image_gen", "reset_at": iso(1100)})
        view = scheduler.snapshot()["accounts"][A]
        self.assertTrue(view["reason"]["available"])
        self.assertFalse(view["image_gen"]["available"])
        self.clock.advance(101)
        self.quota.read(A, self.clock, reason=5, image=5)
        self.assertTrue(scheduler.snapshot()["accounts"][A]["image_gen"]["available"])

    async def test_generic_rate_limit_is_temporary_and_does_not_set_quota_zero(self):
        scheduler = self.scheduler(account(A, 5, 5))
        lease = await scheduler.acquire()
        await scheduler.finish(lease, status="failed", dispatched=True, error={"kind": "rate_limit", "retry_after_seconds": 20})
        state = scheduler.snapshot()["accounts"][A]["reason"]
        self.assertEqual(state["state"], "cooldown")
        self.assertEqual(state["effective_remaining"], 4)
        with self.assertRaises(NoAvailableAccount) as context:
            await scheduler.acquire()
        self.assertEqual(context.exception.retry_after_seconds, 20)
        self.clock.advance(21)
        self.assertTrue(scheduler.snapshot()["accounts"][A]["reason"]["available"])

    async def test_transient_cooldown_does_not_erase_existing_quota_blocks(self):
        scheduler = self.scheduler(account(A, 5, 0))
        lease = await scheduler.acquire()
        await scheduler.finish(lease, status="failed", error={"kind": "transient"})
        self.clock.advance(11)
        view = scheduler.snapshot()["accounts"][A]
        self.assertTrue(view["reason"]["available"])
        self.assertFalse(view["image_gen"]["available"])

    async def test_auth_requires_successful_newer_read_not_merely_waiting(self):
        scheduler = self.scheduler(account(A))
        lease = await scheduler.acquire()
        await scheduler.finish(lease, status="failed", dispatched=True, error={"kind": "auth"})
        self.clock.advance(600)
        with self.assertRaises(NoAvailableAccount):
            await scheduler.acquire()
        self.quota.read(A, self.clock, started=999)
        self.assertEqual(scheduler.snapshot()["accounts"][A]["reason"]["state"], "auth_error")
        self.quota.read(A, self.clock)
        self.assertTrue(scheduler.snapshot()["accounts"][A]["reason"]["available"])

    async def test_restart_preserves_blocks_and_provisional_consumption(self):
        scheduler = self.scheduler(account(A, 3, 0), persist=True)
        lease = await scheduler.acquire()
        await scheduler.finish(lease, status="succeeded", dispatched=True)
        restarted = AccountScheduler(self.quota, state_path=self.path, clock=self.clock)
        view = restarted.snapshot()["accounts"][A]
        self.assertEqual(view["reason"]["provisional"], 1)
        self.assertEqual(view["image_gen"]["state"], "exhausted")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_restart_converts_unfinished_lease_to_conservative_debit(self):
        scheduler = self.scheduler(account(A, 1, 1), persist=True)
        await scheduler.acquire("image_gen")
        restarted = AccountScheduler(self.quota, state_path=self.path, clock=self.clock)
        view = restarted.snapshot()["accounts"][A]
        self.assertEqual(view["active_requests"], 0)
        self.assertEqual(view["reason"]["provisional"], 1)
        self.assertEqual(view["image_gen"]["provisional"], 1)
        with self.assertRaises(NoAvailableAccount):
            await restarted.acquire()
        self.assertEqual(restarted.snapshot()["events"][0]["type"], "interrupted")

    async def test_state_events_and_lease_repr_do_not_leak_tokens_or_error_text(self):
        scheduler = self.scheduler(account(A), persist=True)
        lease = await scheduler.acquire()
        raw_error = "prompt-and-private-upstream-token-should-never-be-persisted"
        await scheduler.finish(lease, status="failed", error={"kind": "transient", "message": raw_error, "token": lease.token})
        serialized = self.path.read_text() + json.dumps(scheduler.snapshot()) + repr(lease)
        self.assertNotIn(lease.token, serialized)
        self.assertNotIn(raw_error, serialized)
        self.assertIn(A, serialized)

    async def test_cache_read_allowlists_fields_and_events_are_bounded(self):
        private = "not-a-public-field"
        self.path.write_text(json.dumps({"version": 1, "accounts": {A: {"token": private}},
                                         "events": [{"at": iso(900), "type": "selected", "account_id": A, "reason": private,
                                                     "token": private} for _ in range(150)]}))
        self.quota = Quotas(account(A))
        scheduler = AccountScheduler(self.quota, state_path=self.path, clock=self.clock)
        self.assertLessEqual(len(scheduler.snapshot()["events"]), 100)
        self.assertNotIn(private, json.dumps(scheduler.snapshot()))

    async def test_persistence_failure_keeps_in_memory_reservations(self):
        self.quota = Quotas(account(A, 1))
        scheduler = AccountScheduler(self.quota, state_path=self.directory.name, clock=self.clock)
        await scheduler.acquire()
        self.assertTrue(scheduler.snapshot()["persistence_error"])
        with self.assertRaises(NoAvailableAccount):
            await scheduler.acquire()


if __name__ == "__main__":
    unittest.main()
