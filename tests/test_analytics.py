"""Offline request-ledger, calendar-boundary, and aggregate correctness tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from chatgpt.analytics import AnalyticsStore, ValidationError


A = "key_" + "a" * 24
B = "key_" + "b" * 24
SYSTEM = "system_" + "c" * 64


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class Clock:
    def __init__(self):
        self.now = stamp("2026-10-21T04:00:00Z")

    def __call__(self):
        return self.now


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "request_usage.sqlite3"
        self.clock = Clock()
        self.store = AnalyticsStore(self.path, clock=self.clock)

    def record(self, *, key_id=A, kind="chat", model="auto", status="succeeded", **usage):
        identifier = self.store.start(key_id=key_id, kind=kind, requested_model=model)
        self.clock.now += 2
        self.store.finish(identifier, status=status, http_status=200 if status == "succeeded" else 502, **usage)
        return identifier

    def test_empty_calendar_is_zero_filled_and_historical_attempts_are_not_imported(self):
        old = self.path.with_name("account_usage.sqlite3")
        old.write_text("Unrelated account attempt history")
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["range"], {"date_from": "2026-10-15", "date_to": "2026-10-21", "days": 7, "timezone": "Asia/Shanghai"})
        self.assertEqual(len(snapshot["daily"]), 7)
        self.assertEqual([row["hour"] for row in snapshot["hourly"]], list(range(24)))
        self.assertEqual(len(snapshot["statuses"]), 5)
        self.assertEqual(snapshot["total"]["requests"], 0)
        self.assertIsNone(snapshot["total"]["avg_duration_ms"])
        self.assertEqual(snapshot["coverage"]["known_cost_ratio"], 0)
        self.assertEqual(snapshot["since"], "2026-10-21T04:00:00.000+00:00")
        self.assertFalse(snapshot["legacy"]["included"])
        self.assertEqual(snapshot["recent"], [])
        self.assertEqual(snapshot["options"], {"models": [], "keys": []})
        self.assertEqual(old.read_text(), "Unrelated account attempt history")

    def test_sqlite_is_private_wal_and_schema_has_no_content_or_credentials(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            fields = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            for name in ("prompt", "output", "token", "authorization", "url", "raw_error", "account_id"):
                self.assertNotIn(name, fields)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.path) + suffix)
                if sidecar.exists():
                    self.assertEqual(os.stat(sidecar).st_mode & 0o777, 0o600)

    def test_finish_is_idempotent_and_retries_do_not_create_more_client_requests(self):
        identifier = self.store.start(key_id=A, kind="chat", requested_model="gpt-4.1")
        self.clock.now += 2.5
        self.assertTrue(self.store.finish(identifier, status="succeeded", upstream_model="gpt-4.1", http_status=200,
            prompt_tokens=100, completion_tokens=20, pricing_status="known", estimated_cost_usd=.03,
            estimated_text_cost_usd=.03, estimated_image_cost_usd=0, price_source="custom", rate_version="frozen-v1"))
        self.assertFalse(self.store.finish(identifier, status="failed", pricing_status="known", estimated_cost_usd=999,
            error_code="late_failure", rate_version="frozen-v2"))
        self.assertFalse(self.store.finish("req_" + "0" * 32, status="failed"))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["total"]["requests"], 1)
        self.assertEqual(snapshot["total"]["succeeded"], 1)
        self.assertEqual(snapshot["total"]["estimated_cost_usd"], .03)
        self.assertEqual(snapshot["total"]["avg_duration_ms"], 2500)
        self.assertEqual(snapshot["recent"][0]["rate_version"], "frozen-v1")
        self.assertEqual(snapshot["recent"][0]["upstream_model"], "gpt-4.1")

    def test_known_unknown_and_known_zero_costs_remain_distinguishable(self):
        self.record(model="gpt-4.1", prompt_tokens=10, completion_tokens=2, token_source="upstream",
                    pricing_status="known", estimated_cost_usd=0, price_source="custom")
        self.record(key_id=B, model="gpt-6", prompt_tokens=20, completion_tokens=None)
        self.store.start(key_id=SYSTEM, kind="image", requested_model="gpt-image-2")
        result = self.store.snapshot()
        self.assertEqual(result["total"]["estimated_cost_usd"], 0)
        self.assertEqual(result["total"]["known_cost_requests"], 1)
        self.assertEqual(result["total"]["unknown_cost_requests"], 2)
        self.assertEqual(result["total"]["prompt_tokens"], 30)
        self.assertEqual(result["total"]["completion_tokens"], 2)
        self.assertEqual(result["coverage"]["token_unknown_requests"], 2)
        self.assertAlmostEqual(result["coverage"]["known_cost_ratio"], 1 / 3)
        self.assertEqual(result["total"]["in_progress"], 1)
        self.assertIsNone(result["recent"][0]["estimated_cost_usd"])
        self.assertIsNone(result["recent"][0]["duration_ms"])

    def test_image_requests_generated_images_and_key_amounts_are_separate(self):
        self.record(kind="image", model="chatgpt-image-latest", status="failed")
        self.record(kind="chat", model="auto", generated_images=2, image_requests=1,
                    pricing_status="known", estimated_cost_usd=.32, estimated_text_cost_usd=.02,
                    estimated_image_cost_usd=.30, price_source="custom")
        self.record(key_id=B, kind="image", model="gpt-image-2", generated_images=1,
                    pricing_status="known", estimated_cost_usd=.10, estimated_text_cost_usd=0,
                    estimated_image_cost_usd=.10, price_source="official")
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["total"]["image_requests"], 3)
        self.assertEqual(snapshot["total"]["generated_images"], 3)
        self.assertEqual(snapshot["total"]["estimated_cost_usd"], .42)
        self.assertEqual(snapshot["total"]["estimated_text_cost_usd"], .02)
        self.assertEqual(snapshot["total"]["estimated_image_cost_usd"], .4)
        key_a = next(row for row in snapshot["keys"] if row["key_id"] == A)
        self.assertEqual((key_a["requests"], key_a["image_requests"], key_a["generated_images"]), (2, 2, 2))
        self.assertEqual(key_a["estimated_cost_usd"], .32)
        self.assertEqual({row["endpoint"] for row in snapshot["endpoints"]}, {"/v1/chat/completions", "/v1/images/generations"})

    def test_partial_estimate_is_preserved_without_claiming_a_known_total(self):
        self.record(generated_images=1, pricing_status="unknown", estimated_cost_usd=None,
                    estimated_text_cost_usd=.05, estimated_image_cost_usd=None, price_source="custom")
        total = self.store.snapshot()["total"]
        self.assertEqual(total["estimated_cost_usd"], 0)
        self.assertEqual(total["known_cost_requests"], 0)
        self.assertEqual(total["unknown_cost_requests"], 1)
        self.assertEqual(total["estimated_text_cost_usd"], .05)
        self.assertEqual(total["known_text_cost_requests"], 1)
        self.assertEqual(total["known_image_cost_requests"], 0)
        self.assertEqual(total["image_requests"], 1)

    def test_filters_are_conjunctive_and_candidates_do_not_shrink_with_filters(self):
        matching = self.record(key_id=A, kind="chat", model="gpt-4.1", status="failed", error_code="quota_exceeded")
        self.record(key_id=A, kind="chat", model="gpt-4.1", status="succeeded")
        self.record(key_id=B, kind="chat", model="gpt-4.1", status="failed")
        self.record(key_id=A, kind="image", model="gpt-image-2", status="failed")
        snapshot = self.store.snapshot(api_key_id=A, model="gpt-4.1", kind="chat", status="failed")
        self.assertEqual(snapshot["total"]["requests"], 1)
        self.assertEqual(snapshot["recent"][0]["id"], matching)
        self.assertEqual(snapshot["models"][0]["model"], "gpt-4.1")
        self.assertEqual(snapshot["keys"][0]["key_id"], A)
        self.assertEqual(set(snapshot["options"]["models"]), {"gpt-4.1", "gpt-image-2"})
        self.assertEqual(set(snapshot["options"]["keys"]), {A, B})

    def test_local_day_uses_inclusive_dates_and_exclusive_next_midnight(self):
        for instant in ("2026-10-20T15:59:59Z", "2026-10-20T16:00:00Z", "2026-10-21T15:59:59Z", "2026-10-21T16:00:00Z"):
            self.clock.now = stamp(instant)
            self.record()
        snapshot = self.store.snapshot(date_from="2026-10-21", date_to="2026-10-21")
        self.assertEqual(snapshot["total"]["requests"], 2)
        self.assertEqual(snapshot["daily"][0]["date"], "2026-10-21")
        self.assertEqual(snapshot["hourly"][0]["requests"], 1)
        self.assertEqual(snapshot["hourly"][23]["requests"], 1)

    def test_dst_spring_day_is_23_hours_and_fall_repeated_hour_is_combined(self):
        for instant in ("2026-03-08T04:59:59Z", "2026-03-08T05:00:00Z", "2026-03-08T07:00:00Z", "2026-03-09T04:00:00Z"):
            self.clock.now = stamp(instant)
            self.record()
        spring = self.store.snapshot(date_from="2026-03-08", date_to="2026-03-08", timezone="America/New_York")
        self.assertEqual(spring["total"]["requests"], 2)
        self.assertEqual(spring["hourly"][2]["requests"], 0)
        self.assertEqual(spring["hourly"][3]["requests"], 1)
        for instant in ("2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z", "2026-11-02T04:30:00Z"):
            self.clock.now = stamp(instant)
            self.record()
        fall = self.store.snapshot(date_from="2026-11-01", date_to="2026-11-01", timezone="America/New_York")
        self.assertEqual(fall["total"]["requests"], 3)
        self.assertEqual(fall["hourly"][1]["requests"], 2)
        self.assertEqual(fall["hourly"][23]["requests"], 1)
        self.assertEqual(len(fall["hourly"]), 24)

    def test_recent_is_limited_to_fifty_latest_rows_and_keeps_only_public_metadata(self):
        ids = [self.record(model="auto") for _ in range(54)]
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["total"]["requests"], 54)
        self.assertEqual([row["id"] for row in snapshot["recent"]], list(reversed(ids))[:50])
        self.assertNotIn("runtime_id", snapshot["recent"][0])
        self.assertTrue(snapshot["recent"][0]["started_at"].endswith("+00:00"))
        # All output is directly JSON serializable, including empty/missing values.
        json.dumps(snapshot, allow_nan=False)

    def test_same_runtime_reopen_keeps_activity_and_new_runtime_recovers_interrupted(self):
        started = self.store.start(key_id=A, kind="image", requested_model="gpt-image-2")
        finished = self.record(model="gpt-4.1", pricing_status="known", estimated_cost_usd=.1, rate_version="original")
        since = self.store.snapshot()["since"]
        self.clock.now += 28
        reopened = AnalyticsStore(self.path, clock=self.clock)
        self.assertEqual(reopened.snapshot()["total"]["in_progress"], 1)
        with patch("chatgpt.analytics._RUNTIME_ID", "synthetic-new-runtime"):
            recovered = AnalyticsStore(self.path, clock=self.clock).snapshot()
        row = next(row for row in recovered["recent"] if row["id"] == started)
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["error_code"], "process_restarted")
        self.assertEqual(row["duration_ms"], 30000)
        self.assertEqual(row["image_requests"], 1)
        self.assertIsNone(row["estimated_cost_usd"])
        self.assertEqual(recovered["since"], since)
        done = next(row for row in recovered["recent"] if row["id"] == finished)
        self.assertEqual(done["rate_version"], "original")
        self.assertFalse(self.store.finish(started, status="succeeded"))

    def test_concurrent_finishes_commit_only_one_outcome(self):
        identifier = self.store.start(key_id=SYSTEM, kind="chat", requested_model="auto")
        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(lambda _: self.store.finish(identifier, status="succeeded", prompt_tokens=1, completion_tokens=1), range(24)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(self.store.snapshot()["total"]["requests"], 1)
        with ThreadPoolExecutor(max_workers=8) as executor:
            identifiers = list(executor.map(lambda _: self.store.start(key_id=A, kind="chat", requested_model="auto"), range(24)))
        self.assertEqual(len(set(identifiers)), 24)
        total = self.store.snapshot()["total"]
        self.assertEqual((total["requests"], total["succeeded"], total["in_progress"]), (25, 1, 24))

    def test_known_decimal_estimates_are_summed_without_binary_accumulation_drift(self):
        self.record(pricing_status="known", estimated_cost_usd=.1)
        self.record(pricing_status="known", estimated_cost_usd=.2)
        self.assertEqual(self.store.snapshot()["total"]["estimated_cost_usd"], .3)

    def test_calendar_and_filter_validation_rejects_unsupported_values(self):
        invalid = ({"days": True}, {"days": 1}, {"days": 366}, {"days": "7"}, {"timezone": "../UTC"},
            {"timezone": "Missing/Zone"}, {"timezone": None}, {"date_from": "2026-01-01"},
            {"date_from": "2026-02-30", "date_to": "2026-03-01"},
            {"date_from": "20260101", "date_to": "2026-01-01"},
            {"date_from": "2026-01-02", "date_to": "2026-01-01"},
            {"date_from": "2025-01-01", "date_to": "2026-01-02"},
            {"date_from": "9999-12-31", "date_to": "9999-12-31"},
            {"api_key_id": "sk-c2a-private-key"}, {"model": "a secret prompt\n"},
            {"kind": "images"}, {"status": "all"})
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValidationError):
                self.store.snapshot(**args)
        leap = self.store.snapshot(date_from="2024-01-01", date_to="2024-12-31", timezone="UTC")
        self.assertEqual(len(leap["daily"]), 366)
        for days in (7, 30, 90):
            self.assertEqual(len(self.store.snapshot(days=days)["daily"]), days)

    def test_start_rejects_plaintext_secrets_and_invalid_metadata(self):
        for extra in ({"key_id": "sk-live-sensitive-key"}, {"key_id": "eyJhbGci.private.jwt"}, {"key_id": ""},
                      {"kind": "images"}, {"requested_model": "https://private.example/image?signature=secret"},
                      {"requested_model": "A prompt with spaces"}, {"image_requests": True}, {"image_requests": 2}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self.store.start(**{"key_id": A, "kind": "chat", "requested_model": "auto", **extra})
        with self.assertRaises(ValidationError):
            self.store.start(key_id=A, kind="image", requested_model="gpt-image-2", image_requests=0)
        for key_id in (A, SYSTEM, "direct"):
            self.store.start(key_id=key_id, kind="chat", requested_model="auto")
        self.assertEqual(self.store.snapshot()["total"]["requests"], 3)

    def test_finish_validation_does_not_overwrite_pending_request(self):
        identifier = self.store.start(key_id=A, kind="chat", requested_model="auto")
        invalid = ({"status": "in_progress"}, {"http_status": 999}, {"http_status": True},
            {"prompt_tokens": -1}, {"completion_tokens": True}, {"generated_images": 10001},
            {"image_requests": 2}, {"token_source": "Bearer private"},
            {"price_source": "https://openai.com/pricing"}, {"rate_version": "path/private"},
            {"pricing_status": "priced"}, {"pricing_status": "known"},
            {"estimated_cost_usd": 1}, {"estimated_text_cost_usd": float("nan")},
            {"estimated_image_cost_usd": float("inf")}, {"estimated_text_cost_usd": -1},
            {"estimated_cost_usd": "0.1"}, {"error_code": "raw error with private value"})
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValidationError):
                self.store.finish(identifier, **{"status": "failed", **args})
        self.assertEqual(self.store.snapshot()["total"]["in_progress"], 1)

    def test_global_candidates_include_records_outside_selected_dates(self):
        self.record(key_id=B, model="gpt-4.1")
        result = self.store.snapshot(date_from="2026-01-01", date_to="2026-01-01", api_key_id=A)
        self.assertEqual(result["total"]["requests"], 0)
        self.assertEqual(result["options"], {"models": ["gpt-4.1"], "keys": [B]})


if __name__ == "__main__":
    unittest.main()
