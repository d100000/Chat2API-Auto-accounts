"""Credential/policy transaction tests; synthetic tokens and temporary files only."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chatgpt.account_concurrency import ConcurrencyPersistenceError, ConcurrencyStore, credential_alias
from chatgpt.account_import import (ImportPersistenceError, ImportValidationError, clear_accounts,
                                    import_accounts, parse_credentials, parse_limit, recover_import)
from chatgpt.account_quota import _identity


def token(account, variant="original"):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "none"}) + "." + encode({"exp": 2000000000,
        "https://api.openai.com/auth": {"chatgpt_account_id": account}, "synthetic_variant": variant}) + ".synthetic"


class Scheduler:
    def __init__(self, path, state):
        self.lock = asyncio.Lock()
        self.concurrency = ConcurrencyStore(path)
        self.state = state

    def admission_guard(self):
        return self.lock

    def synchronize_identities_locked(self):
        assert self.lock.locked()
        for value in self.state.token_list:
            self.bind_identity(value, _identity(value, refresh_map=self.state.refresh_map)["id"])

    def bind_identity(self, value, account_id):
        assert self.lock.locked()
        self.concurrency.bind_alias(account_id, credential_alias(value))
        return self.concurrency.resolve(account_id)


class ImportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="account-import-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.tokens_path = self.root / "tokens.txt"
        self.old, self.new = token("synthetic-a"), token("synthetic-b")
        self.a, self.b = _identity(self.old)["id"], _identity(self.new)["id"]
        self.tokens_path.write_text(self.old + "\n")
        self.state = SimpleNamespace(TOKENS_FILE=str(self.tokens_path), token_list=[self.old],
                                     error_token_list=[], refresh_map={})
        self.policy_path = self.root / "policy.json"
        self.scheduler = Scheduler(self.policy_path, self.state)
        self.journal = self.root / ".account-import.json"

    async def import_text(self, text, **kwargs):
        return await import_accounts(text, state=self.state, scheduler=self.scheduler, **kwargs)

    async def recover(self):
        return await recover_import(state=self.state, scheduler=self.scheduler)

    def policy(self, account_id):
        return self.scheduler.concurrency.limits(account_id)

    async def test_dedup_new_account_limits_and_private_persistence(self):
        result = await self.import_text(f"# comment\n{self.old}\n{self.new}\n{self.new}\n",
                                        reason_limit="7", image_gen_limit="2")
        self.assertEqual(self.state.token_list, [self.old, self.new])
        self.assertEqual(self.tokens_path.read_text(), self.old + "\n" + self.new + "\n")
        self.assertEqual(result["added_credentials"], 1)
        self.assertEqual(result["duplicate_credentials"], 2)
        self.assertEqual(result["new_accounts"], 1)
        self.assertEqual(result["existing_accounts"], 1)
        self.assertEqual(result["settings_applied"], 1)
        self.assertEqual(self.policy(self.b)["effective"], {"reason": 7, "image_gen": 2})
        self.assertEqual(self.policy(self.a)["overrides"], {"reason": None, "image_gen": None})
        self.assertFalse(self.journal.exists())
        self.assertEqual(list(self.root.glob(".account-import-*")), [])
        self.assertEqual(stat.S_IMODE(self.tokens_path.stat().st_mode), 0o600)
        for secret in (self.old, self.new):
            self.assertNotIn(secret, json.dumps(result) + self.policy_path.read_text())

    async def test_duplicate_account_different_token_preserves_policy_by_default(self):
        self.scheduler.concurrency.update(expected_revision=0, account_ids=[self.a],
                                           overrides={"reason": 2, "image_gen": 1})
        second = token("synthetic-a", "rotated")
        result = await self.import_text(second, reason_limit="10", image_gen_limit="10")
        self.assertEqual(result["added_credentials"], 1)
        self.assertEqual(result["new_accounts"], 0)
        self.assertEqual(result["settings_applied"], 0)
        self.assertEqual(self.policy(self.a)["effective"], {"reason": 2, "image_gen": 1})
        revision = self.scheduler.concurrency.snapshot()["revision"]
        await self.import_text(second, reason_limit="4", image_gen_limit="inherit", overwrite_existing=True)
        self.assertEqual(self.policy(self.a)["overrides"], {"reason": 4, "image_gen": None})
        self.assertEqual(self.scheduler.concurrency.snapshot()["revision"], revision + 1)

    async def test_inherited_import_tracks_later_system_defaults(self):
        await self.import_text(self.new)
        self.assertEqual(self.policy(self.b)["overrides"], {"reason": None, "image_gen": None})
        revision = self.scheduler.concurrency.snapshot()["revision"]
        self.scheduler.concurrency.update(expected_revision=revision, defaults={"reason": 8, "image_gen": 3})
        self.assertEqual(self.policy(self.b)["effective"], {"reason": 8, "image_gen": 3})

    async def test_invalid_input_does_not_touch_files_state_or_policy(self):
        before = self.tokens_path.read_bytes()
        invalid = [dict(text=""), dict(text="# only comments"), dict(text="has space"),
                   dict(text="synthetic,../workspace"), dict(text="synthetic\x00"),
                   dict(text="x" * (32768 + 1)), dict(text="x\n" * 1001),
                   dict(text=self.new, reason_limit="0"), dict(text=self.new, reason_limit="11"),
                   dict(text=self.new, reason_limit=True), dict(text=self.new, image_gen_limit=2),
                   dict(text=self.new, image_gen_limit="1.0"), dict(text=self.new, overwrite_existing="true")]
        for args in invalid:
            with self.subTest(args={k: str(v)[:40] for k, v in args.items()}), self.assertRaises(ImportValidationError):
                await self.import_text(**args)
        self.assertEqual(self.tokens_path.read_bytes(), before)
        self.assertEqual(self.state.token_list, [self.old])
        self.assertFalse(self.policy_path.exists())
        self.assertFalse(self.journal.exists())
        self.assertEqual(list(self.root.glob(".account-import-*")), [])

    async def test_policy_failure_retains_journal_but_does_not_publish_tokens(self):
        before = self.tokens_path.read_bytes()
        with patch.object(self.scheduler.concurrency, "update", side_effect=ConcurrencyPersistenceError("PRIVATE-PATH")):
            with self.assertRaises(ImportPersistenceError) as caught:
                await self.import_text(self.new, reason_limit="6", image_gen_limit="2")
        self.assertNotIn("PRIVATE-PATH", str(caught.exception))
        self.assertEqual(self.state.token_list, [self.old])
        self.assertEqual(self.tokens_path.read_bytes(), before)
        self.assertEqual(self.scheduler.concurrency.snapshot()["revision"], 0)
        operation = json.loads(self.journal.read_text())
        serialized = self.journal.read_text()
        self.assertNotIn(self.new, serialized)
        self.assertNotIn(self.old, serialized)
        self.assertEqual(set(operation), {"version", "staging", "digest", "revision", "account_ids", "overrides"})
        self.assertEqual(stat.S_IMODE(self.journal.stat().st_mode), 0o600)
        staging = self.root / operation["staging"]
        self.assertTrue(staging.exists())
        self.assertEqual(stat.S_IMODE(staging.stat().st_mode), 0o600)
        self.assertEqual(hashlib.sha256(staging.read_bytes()).hexdigest(), operation["digest"])
        await self.recover()
        self.assertEqual(self.state.token_list, [self.old, self.new])
        self.assertEqual(self.policy(self.b)["effective"], {"reason": 6, "image_gen": 2})
        self.assertFalse(self.journal.exists())
        self.assertFalse(staging.exists())

    async def test_token_replace_failure_after_policy_commit_recovers_idempotently(self):
        import os
        original = os.replace
        def replace(source, destination):
            if Path(destination) == self.tokens_path:
                raise OSError("synthetic token replace failure")
            return original(source, destination)
        with patch("chatgpt.account_import.os.replace", side_effect=replace):
            with self.assertRaises(ImportPersistenceError):
                await self.import_text(self.new, reason_limit="5")
        self.assertEqual(self.state.token_list, [self.old])
        self.assertEqual(self.policy(self.b)["effective"]["reason"], 5)
        committed_revision = self.scheduler.concurrency.snapshot()["revision"]
        await self.recover()
        self.assertEqual(self.state.token_list, [self.old, self.new])
        self.assertEqual(self.scheduler.concurrency.snapshot()["revision"], committed_revision)
        await self.recover()
        self.assertEqual(self.scheduler.concurrency.snapshot()["revision"], committed_revision)

    async def test_journal_directory_fsync_failure_does_not_erase_staging(self):
        with patch("chatgpt.account_import._sync_dir", side_effect=OSError("synthetic fsync failure")):
            with self.assertRaises(ImportPersistenceError):
                await self.import_text(self.new, reason_limit="4")
        self.assertTrue(self.journal.exists())
        operation = json.loads(self.journal.read_text())
        self.assertTrue((self.root / operation["staging"]).exists())
        self.assertEqual(self.state.token_list, [self.old])
        await self.recover()
        self.assertEqual(self.state.token_list, [self.old, self.new])
        self.assertEqual(self.policy(self.b)["effective"]["reason"], 4)

    async def test_identity_persistence_failure_is_safe_before_staging_or_publication(self):
        with patch.object(self.scheduler, "bind_identity", side_effect=ConcurrencyPersistenceError("PRIVATE PATH")):
            with self.assertRaises(ImportPersistenceError) as caught:
                await self.import_text(self.new)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(self.state.token_list, [self.old])
        self.assertFalse(self.journal.exists())
        self.assertEqual(list(self.root.glob(".account-import-*")), [])

    async def test_journal_write_failure_before_commit_cleans_staging(self):
        with patch("chatgpt.account_import._write_private", side_effect=OSError("synthetic storage error")):
            with self.assertRaises(ImportPersistenceError):
                await self.import_text(self.new)
        self.assertFalse(self.journal.exists())
        self.assertEqual(list(self.root.glob(".account-import-*")), [])
        self.assertEqual(self.state.token_list, [self.old])

    async def test_recovery_uses_committed_token_file_after_rename(self):
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == self.journal:
                raise OSError("synthetic journal cleanup failure")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            with self.assertRaises(ImportPersistenceError):
                await self.import_text(self.new, reason_limit="6")
        operation = json.loads(self.journal.read_text())
        self.assertFalse((self.root / operation["staging"]).exists())
        self.assertEqual(self.tokens_path.read_text().splitlines(), [self.old, self.new])
        revision = self.scheduler.concurrency.snapshot()["revision"]
        # Simulate process restart: globals may have loaded either old or new list.
        self.state.token_list = [self.old]
        await self.recover()
        self.assertEqual(self.state.token_list, [self.old, self.new])
        self.assertEqual(self.scheduler.concurrency.snapshot()["revision"], revision)

    async def test_corrupt_recovery_journal_fails_closed_without_publishing(self):
        self.journal.write_text(json.dumps({"version": 1, "staging": "../../private", "digest": "a" * 64}))
        with self.assertRaises(ImportPersistenceError):
            await self.recover()
        self.assertEqual(self.state.token_list, [self.old])
        self.assertFalse(self.policy_path.exists())

    async def test_refresh_identity_migration_preserves_existing_override_on_reimport(self):
        refresh = "R" * 45
        original_id = _identity(refresh)["id"]
        self.state.token_list = [refresh]
        self.tokens_path.write_text(refresh + "\n")
        self.scheduler.concurrency.bind_alias(original_id, credential_alias(refresh))
        self.scheduler.concurrency.update(expected_revision=0, account_ids=[original_id],
                                           overrides={"reason": 2, "image_gen": 1})
        self.state.refresh_map = {refresh: {"token": self.old}}
        result = await self.import_text(self.old, reason_limit="10", image_gen_limit="10")
        self.assertEqual(result["new_accounts"], 0)
        self.assertEqual(result["settings_applied"], 0)
        self.assertEqual(self.scheduler.concurrency.resolve(original_id), self.a)
        self.assertEqual(self.policy(self.a)["effective"], {"reason": 2, "image_gen": 1})
        self.assertEqual(self.scheduler.concurrency._document["aliases"][credential_alias(self.old)], self.a)

    async def test_import_holds_admission_guard_until_policy_and_token_publish(self):
        observed = []
        original = self.scheduler.concurrency.update
        def update(**kwargs):
            observed.append((self.scheduler.lock.locked(), list(self.state.token_list)))
            return original(**kwargs)
        with patch.object(self.scheduler.concurrency, "update", side_effect=update):
            await self.import_text(self.new)
        self.assertEqual(observed, [(True, [self.old])])
        self.assertFalse(self.scheduler.lock.locked())

    async def test_clear_persists_before_publishing_and_preserves_state_on_write_error(self):
        with patch("chatgpt.account_import._write_private", side_effect=OSError("synthetic")):
            with self.assertRaises(ImportPersistenceError):
                await clear_accounts(state=self.state, scheduler=self.scheduler)
        self.assertEqual(self.state.token_list, [self.old])
        await clear_accounts(state=self.state, scheduler=self.scheduler)
        self.assertEqual(self.tokens_path.read_bytes(), b"")
        self.assertEqual(self.state.token_list, [])


class ImportValidationTests(unittest.TestCase):
    def test_parse_limit_is_strict_and_inherit_is_explicit(self):
        for value in (None, "inherit"):
            self.assertIsNone(parse_limit(value))
        self.assertEqual(parse_limit("10"), 10)
        for value in (True, False, 1, 1.0, "01", " 2", "2 ", "0", "11", "1e0", "1.0", ""):
            with self.subTest(value=value), self.assertRaises(ImportValidationError):
                parse_limit(value)

    def test_credential_size_and_workspace_validation(self):
        self.assertEqual(parse_credentials(" # comment\n synthetic,workspace-1 \n"), ["synthetic,workspace-1"])
        for value in (None, b"secret", "a" * (2 * 1024 * 1024 + 1), "synthetic,", "synthetic,has space"):
            with self.assertRaises(ImportValidationError):
                parse_credentials(value)


if __name__ == "__main__":
    unittest.main()
