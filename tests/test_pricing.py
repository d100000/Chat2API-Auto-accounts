"""Offline pricing arithmetic, provenance and persistence; no real config IO."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatgpt import pricing


def text_rate(model="gpt-6", input_price="2", output_price="10", cached=None):
    return {"model": model, "kind": "text", "input_per_million_usd": input_price,
            "output_per_million_usd": output_price, "cached_input_per_million_usd": cached}


def image_rate(model="chatgpt-image-latest", value="0.03"):
    return {"model": model, "kind": "image", "image_per_unit_usd": value}


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "pricing.json"

    def save(self, rows):
        return pricing.update_pricing_config({"custom_rates": rows}, path=self.path)

    def test_read_defaults_does_not_write_and_only_lists_verified_exact_models(self):
        config = pricing.get_pricing_config(path=self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(config["currency"], "USD")
        self.assertIn("不是 OpenAI 账单", config["notice"])
        by_model = {row["model"]: row for row in config["rates"]}
        self.assertEqual(by_model["gpt-6-astra"]["input_per_million_usd"], "10")
        self.assertEqual(by_model["gpt-5.6-terra"]["output_per_million_usd"], "12")
        self.assertTrue(all(row["source"] == "official" and row["source_url"].startswith("https://developers.openai.com/") for row in config["rates"]))
        for model in ("gpt-6", "gpt-5-6", "gpt-5-6-terra", "auto", "gpt-image-2", "chatgpt-image-latest"):
            self.assertNotIn(model, by_model)

    def test_standard_rate_with_cached_input_has_explicit_equivalent_basis(self):
        value = pricing.estimate("gpt-6-astra", 1000, 100, 0, "chat", cached_prompt_tokens=500)
        self.assertEqual(value["estimated_cost_usd"], .0105)
        self.assertEqual(value["estimated_text_cost_usd"], .0105)
        self.assertEqual(value["estimated_image_cost_usd"], 0)
        self.assertEqual(value["price_source"], "official")
        self.assertEqual(value["pricing_status"], "known")
        self.assertEqual(value["cost_basis"], "api_equivalent_estimate")
        self.assertEqual(value["token_source"], "local_estimate")
        self.assertIn("source_url", value["rate_snapshot"][0])

    def test_long_context_threshold_applies_to_whole_request(self):
        short = pricing.estimate("gpt-6-sol", 272000, 100, 0, "chat")
        long = pricing.estimate("gpt-6-sol", 272001, 100, 0, "chat")
        self.assertEqual(short["estimated_cost_usd"], .545)
        self.assertEqual(long["estimated_cost_usd"], 1.089504)
        self.assertIn("long_context_multiplier", long["assumptions"])

    def test_unknown_identifiers_and_images_are_unknown_not_free(self):
        for model, kind in (("auto", "chat"), ("gpt-6", "chat"), ("gpt-5-6-terra", "chat"),
                            ("chatgpt-image-latest", "image"), ("gpt-image-2", "image")):
            with self.subTest(model=model):
                value = pricing.estimate(model, 0, 0, 1 if kind == "image" else 0, kind)
                self.assertIsNone(value["estimated_cost_usd"])
                self.assertEqual(value["pricing_status"], "unknown")
                self.assertEqual(value["price_source"], "unknown")

    def test_custom_web_image_price_counts_images_without_guessing_tokens(self):
        snapshot = self.save([image_rate()])
        value = pricing.estimate("chatgpt-image-latest", None, None, 3, "image", pricing=snapshot)
        self.assertEqual(value["estimated_cost_usd"], .09)
        self.assertEqual(value["estimated_image_cost_usd"], .09)
        self.assertEqual(value["estimated_text_cost_usd"], 0)
        self.assertEqual(value["price_source"], "custom")
        self.assertEqual(value["pricing_status"], "known")

    def test_request_snapshot_freezes_amount_after_later_price_edits(self):
        old = self.save([text_rate()])
        original = pricing.estimate("gpt-6", 1000, 1000, 0, "chat", pricing=old)
        new = self.save([text_rate(input_price="20", output_price="100")])
        changed = pricing.estimate("gpt-6", 1000, 1000, 0, "chat", pricing=new)
        self.assertEqual(original["estimated_cost_usd"], .012)
        self.assertEqual(changed["estimated_cost_usd"], .12)
        self.assertNotEqual(original["rate_version"], changed["rate_version"])
        self.assertEqual(pricing.estimate("gpt-6", 1000, 1000, 0, "chat", pricing=old), original)

    def test_estimate_is_pure_and_never_reads_or_mutates_snapshot(self):
        snapshot = self.save([text_rate()])
        before = json.dumps(snapshot, sort_keys=True)
        with patch.object(Path, "read_text", side_effect=AssertionError("No disk in estimate")):
            pricing.estimate("gpt-6", 1, 1, 0, "chat", pricing=snapshot)
            pricing.estimate("gpt-6-sol", 1, 1, 0, "chat")
        self.assertEqual(json.dumps(snapshot, sort_keys=True), before)

    def test_custom_overrides_official_without_forged_provenance_and_delete_restores(self):
        config = self.save([text_rate("gpt-6-astra")])
        value = pricing.estimate("gpt-6-astra", 1000, 1000, 0, "text", pricing=config)
        self.assertEqual(value["price_source"], "custom")
        self.assertIsNone(value["rate_snapshot"][0]["source_url"])
        reset = self.save([])
        self.assertEqual(pricing.estimate("gpt-6-astra", 1000, 1000, 0, "text", pricing=reset)["price_source"], "official")

    def test_explicit_zero_price_is_known_unlike_missing_price(self):
        snapshot = self.save([image_rate(value="0")])
        for images in (0, 1):
            result = pricing.estimate("chatgpt-image-latest", None, None, images, "image", pricing=snapshot)
            self.assertEqual(result["estimated_cost_usd"], 0)
            self.assertEqual(result["price_source"], "custom")
            self.assertEqual(result["pricing_status"], "known")

    def test_missing_cached_rate_does_not_charge_uncached_rate_silently(self):
        snapshot = self.save([text_rate()])
        value = pricing.estimate("gpt-6", 1000, 1000, 0, "chat", pricing=snapshot, cached_prompt_tokens=100)
        self.assertIsNone(value["estimated_cost_usd"])

    def test_partial_known_text_cost_preserved_while_image_cost_unknown(self):
        value = pricing.estimate("gpt-6-astra", 1000, 100, 1, "chat")
        self.assertEqual(value["estimated_text_cost_usd"], .015)
        self.assertIsNone(value["estimated_image_cost_usd"])
        self.assertIsNone(value["estimated_cost_usd"])
        self.assertEqual(value["pricing_status"], "unknown")

    def test_custom_text_and_image_prices_can_combine_without_duplicate_counting(self):
        config = self.save([text_rate(), image_rate("gpt-6")])
        value = pricing.estimate("gpt-6", 1000, 1000, 2, "chat", pricing=config)
        self.assertEqual(value["estimated_cost_usd"], .072)
        self.assertEqual(len(value["rate_snapshot"]), 2)

    def test_invalid_usage_is_not_estimated(self):
        for prompt, completion, images, cached in ((None, 1, 0, 0), (-1, 1, 0, 0), (True, 1, 0, 0),
            (1, float("nan"), 0, 0), (1, 1, float("inf"), 0), (1, 1, 0, 2), (1, 1, None, 0)):
            with self.subTest(values=(prompt, completion, images, cached)):
                result = pricing.estimate("gpt-6-sol", prompt, completion, images, "chat", cached_prompt_tokens=cached)
                self.assertEqual(result["pricing_status"], "unknown")
                self.assertIsNone(result["estimated_cost_usd"])

    def test_reject_nan_negative_bool_scientific_and_oversized_price_atomically(self):
        self.save([image_rate()])
        original = self.path.read_bytes()
        for value in (float("nan"), "NaN", "Infinity", "-1", "1e-3", True, "1000001", ".1", "0.0000000001", {}):
            with self.subTest(value=value), self.assertRaises(pricing.PricingError):
                self.save([image_rate(value=value)])
            self.assertEqual(self.path.read_bytes(), original)

    def test_schema_rejects_provenance_injection_duplicate_models_and_wrong_units(self):
        invalid = [[{**text_rate(), "source": "official"}], [text_rate(), text_rate()],
                   [text_rate(model="../../tokens")], [{**image_rate(), "input_per_million_usd": "1"}],
                   [{"model": "gpt-6", "kind": "text"}], [image_rate()] * 101]
        for rows in invalid:
            with self.subTest(rows=rows[:1]), self.assertRaises(pricing.PricingError):
                self.save(rows)

    def test_file_is_private_and_atomic_and_has_no_hidden_default_image_rate(self):
        result = self.save([image_rate(value=Decimal("0.10"))])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.parent.glob(".pricing-*")), [])
        self.assertEqual(pricing.get_pricing_config(path=self.path), result)
        self.assertEqual(result["custom_rates"][0]["image_per_unit_usd"], "0.1")

    def test_corrupt_config_does_not_silently_fall_back_to_default_rates(self):
        self.save([])
        for content in ("{broken", '{"schema_version":99}', '{"schema_version":1,"updated_at":"bad","custom_rates":[]}'):
            self.path.write_text(content)
            with self.assertRaises(pricing.PricingError):
                pricing.get_pricing_config(path=self.path)

    def test_failed_replace_keeps_previous_config_and_removes_temporary_file(self):
        self.save([image_rate()])
        original = self.path.read_bytes()
        with patch.object(pricing.os, "replace", side_effect=OSError("offline injected failure")):
            with self.assertRaises(pricing.PricingError):
                self.save([image_rate(value="1")])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".pricing-*")), [])

    def test_concurrent_writes_are_complete_valid_snapshots(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda i: self.save([image_rate(value=str(i))]), range(12)))
        current = pricing.get_pricing_config(path=self.path)
        self.assertIn(current, results)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
