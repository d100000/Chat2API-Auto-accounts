"""Offline managed client-key lifecycle, storage and authorization tests."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from chatgpt.api_keys import (APIKeyStore, KeyNotFound, KeyRevoked, KeyStoreError, ManagedKeyError,
                             key_digest, resolve_managed_key, system_key_info)


SYSTEM_KEY = "test-only-existing-system-key"


class APIKeyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data" / "api-keys.sqlite3"
        self.store = APIKeyStore(self.path)

    def test_empty_reads_do_not_create_database(self):
        self.assertEqual(self.store.list_keys(), [])
        self.assertIsNone(self.store.authenticate("sk-c2a-unknown"))
        self.assertFalse(self.path.exists())

    def test_plaintext_is_returned_once_and_never_written_to_disk(self):
        created = self.store.create(" 云端服务 ")
        plaintext = created["api_key"]
        row = created["key"]
        self.assertTrue(plaintext.startswith("sk-c2a-"))
        self.assertGreaterEqual(len(plaintext), 40)
        self.assertEqual(row["name"], "云端服务")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["prefix"], plaintext[:12])
        self.assertEqual(row["last4"], plaintext[-4:])
        self.assertIsNone(row["last_used_at"])
        self.assertEqual(row["request_count"], 0)
        self.assertEqual(self.store.list_keys(), [row])
        self.assertNotIn(plaintext, json.dumps(self.store.list_keys()))
        self.assertNotIn("hash", json.dumps(self.store.list_keys()))
        with sqlite3.connect(self.path) as connection:
            stored = connection.execute("SELECT key_hash FROM api_keys").fetchone()[0]
        self.assertEqual(stored, key_digest(plaintext))
        for path in self.path.parent.iterdir():
            self.assertNotIn(plaintext.encode(), path.read_bytes())
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_hash_prefix_and_metadata_are_not_replayable_keys(self):
        created = self.store.create("service")
        for value in (key_digest(created["api_key"]), "sk-c2a-" + key_digest(created["api_key"]),
                      created["key"]["prefix"], created["key"]["last4"], created["key"]["id"]):
            self.assertIsNone(self.store.authenticate(value))

    def test_enable_disable_revoke_are_immediate_across_store_instances(self):
        created = self.store.create("service")
        token, identifier = created["api_key"], created["key"]["id"]
        other_process_view = APIKeyStore(self.path)
        self.assertIsNotNone(other_process_view.authenticate(token))
        self.assertEqual(self.store.set_status(identifier, "disabled")["status"], "disabled")
        self.assertIsNone(other_process_view.authenticate(token))
        self.store.set_status(identifier, "active")
        self.assertIsNotNone(other_process_view.authenticate(token))
        self.store.set_status(identifier, "revoked")
        self.assertIsNone(other_process_view.authenticate(token))
        self.assertEqual(self.store.set_status(identifier, "revoked")["status"], "revoked")
        for status in ("active", "disabled"):
            with self.assertRaises(KeyRevoked):
                self.store.set_status(identifier, status)

    def test_authentication_reads_do_not_count_and_acceptance_is_counted_once(self):
        created = self.store.create("service")
        token = created["api_key"]
        for _ in range(4):
            self.store.authenticate(token)
        self.assertEqual(self.store.list_keys()[0]["request_count"], 0)
        accepted = self.store.authenticate(token, record_use=True)
        self.assertEqual(accepted["request_count"], 1)
        self.assertTrue(accepted["last_used_at"].endswith("+00:00"))
        self.store.set_status(created["key"]["id"], "disabled")
        self.assertIsNone(self.store.authenticate(token, record_use=True))
        self.assertEqual(self.store.list_keys()[0]["request_count"], 1)

    def test_concurrent_request_updates_do_not_lose_counts(self):
        token = self.store.create("parallel")["api_key"]
        def accept(_):
            return APIKeyStore(self.path).authenticate(token, record_use=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(accept, range(80)))
        self.assertTrue(all(accepted))
        self.assertEqual(self.store.list_keys()[0]["request_count"], 80)

    def test_concurrent_creates_have_unique_ids_and_keys(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            created = list(pool.map(lambda index: APIKeyStore(self.path).create("service " + str(index)), range(16)))
        self.assertEqual(len({item["api_key"] for item in created}), 16)
        self.assertEqual(len({item["key"]["id"] for item in created}), 16)
        self.assertEqual(len(self.store.list_keys()), 16)

    def test_names_are_bounded_and_queries_are_parameterized(self):
        for name in ("", " ", "x" * 81, "line\nnext", None):
            with self.assertRaises(ValueError):
                self.store.create(name)
        name = "'; DROP TABLE api_keys; --"
        self.assertEqual(self.store.create(name)["key"]["name"], name)
        with self.assertRaises(KeyNotFound):
            self.store.set_status("' OR 1=1 --", "disabled")
        self.assertEqual(len(self.store.list_keys()), 1)

    def test_managed_keys_map_to_system_pool_key_and_legacy_tokens_are_preserved(self):
        token = self.store.create("pool")["api_key"]
        self.assertEqual(resolve_managed_key(token, [SYSTEM_KEY], store=self.store), SYSTEM_KEY)
        self.assertEqual(self.store.list_keys()[0]["request_count"], 0)
        self.assertEqual(resolve_managed_key(token, [SYSTEM_KEY], store=self.store, record_use=True), SYSTEM_KEY)
        self.assertEqual(self.store.list_keys()[0]["request_count"], 1)
        for token in ("eyJhbGciOi-upstream", SYSTEM_KEY, "legacy-seed"):
            self.assertEqual(resolve_managed_key(token, [SYSTEM_KEY], store=self.store), token)

    def test_reserved_prefix_cannot_fall_back_to_upstream_tokens(self):
        created = self.store.create("pool")
        self.store.set_status(created["key"]["id"], "revoked")
        for token in ("sk-c2a-not-found", created["api_key"], "sk-c2a-" + "x" * 300):
            with self.assertRaises(ManagedKeyError) as caught:
                resolve_managed_key(token, [SYSTEM_KEY], store=self.store)
            self.assertEqual(caught.exception.status_code, 401)
        with self.assertRaises(ManagedKeyError) as caught:
            resolve_managed_key("sk-c2a-any", [], store=self.store)
        self.assertEqual(caught.exception.status_code, 503)

    def test_explicit_environment_key_with_managed_prefix_remains_compatible(self):
        token = "sk-c2a-test-only-env-key"
        self.assertEqual(resolve_managed_key(token, [token], store=self.store, record_use=True), token)
        self.assertFalse(self.path.exists())

    def test_corrupt_database_fails_closed_without_details_or_secret(self):
        token = self.store.create("pool")["api_key"]
        self.path.write_bytes(b"not a database")
        with self.assertRaises(ManagedKeyError) as caught:
            resolve_managed_key(token, [SYSTEM_KEY], store=self.store)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn(str(self.path), str(caught.exception))

    def test_symlink_database_is_rejected(self):
        target = Path(self.directory.name) / "target"
        target.write_text("do not overwrite")
        self.path.parent.mkdir()
        self.path.symlink_to(target)
        with self.assertRaises(KeyStoreError):
            self.store.create("pool")
        self.assertEqual(target.read_text(), "do not overwrite")

    def test_system_key_metadata_never_exposes_the_full_value(self):
        info = system_key_info([SYSTEM_KEY, "other-test-key"])
        self.assertEqual(info["count"], 2)
        self.assertTrue(info["read_only"])
        self.assertNotIn(SYSTEM_KEY, json.dumps(info))
        self.assertEqual(system_key_info([]), {"configured": False, "hint": None, "count": 0, "read_only": True})
        self.assertNotIn("short", json.dumps(system_key_info(["short"])))


if __name__ == "__main__":
    unittest.main()
