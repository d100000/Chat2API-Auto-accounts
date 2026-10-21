"""Offline evidence and lifecycle regressions; no deployment imports or credentials."""

import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import logging
from pathlib import Path
import random
import re
import string
import tempfile
import time
import types
import unittest

from fastapi import HTTPException
from chatgpt.account_usage import Evidence, UsageStore, UsageTrace


ROOT = Path(__file__).resolve().parents[1]


def event(text="hello", *, role="assistant", status="finished_successfully", end_turn=True, parts=None, message_id="output"):
    return {"message": {"id": message_id, "author": {"role": role}, "status": status,
                        "end_turn": end_turn, "content": {"content_type": "multimodal_text" if parts else "text", "parts": parts or [text]},
                        "metadata": {"model_slug": "observed-model"}}}


def asset(identifier):
    return {"content_type": "image_asset_pointer", "asset_pointer": "file-service://" + identifier}


async def lines(events):
    for value in events:
        yield ("data: " + (json.dumps(value) if isinstance(value, dict) else value)).encode()


def source_functions(path, names, namespace):
    # Isolate actual source functions from legacy modules whose top-level
    # imports load .env and account files. Dependencies are offline test doubles.
    tree = ast.parse((ROOT / path).read_text())
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert len(selected) == len(names)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(ROOT / path), "exec"), namespace)
    return namespace


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.path = Path(self.sandbox.name) / "usage.sqlite3"
        self.store = UsageStore(self.path)

    def test_windows_accounts_outcomes_and_private_persistence(self):
        now = time.time()
        old = self.store.start("a", "alias", now - 90000)
        self.store.finish(old, "succeeded", Evidence(sent_model="mapped", upstream_model="observed"))
        recent = self.store.start("a", "alias", now - 20)
        self.store.finish(recent, "failed", Evidence(image_requested=True, generated_assets=frozenset(("image",))), "http_502")
        other = self.store.start("b", "auto")
        self.store.finish(other, "cancelled", Evidence())
        values = self.store.summaries(["a"], now)["accounts"]
        self.assertEqual(set(values), {"a"})
        self.assertEqual(values["a"]["totals"]["attempts"], 2)
        self.assertEqual(values["a"]["windows"]["last_5h"]["attempts"], 1)
        self.assertEqual(values["a"]["windows"]["last_7d"]["attempts"], 2)
        self.assertEqual(values["a"]["totals"]["generated_images"], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(UsageStore(self.path).summaries(["a"])["accounts"]["a"]["totals"]["attempts"], 2)

    def test_duplicate_finish_and_restart_do_not_invent_success(self):
        finished = self.store.start("a", "auto")
        self.store.finish(finished, "failed", Evidence(), "http_429")
        self.store.finish(finished, "succeeded", Evidence(terminal=True))
        self.store.start("a", "auto")
        restored = UsageStore(self.path)
        totals = restored.summaries(["a"])["accounts"]["a"]["totals"]
        self.assertEqual((totals["failed"], totals["interrupted"], totals["succeeded"]), (1, 1, 0))

    def test_concurrent_attempts_are_not_lost(self):
        def run(index):
            identifier = self.store.start("a", "auto")
            self.store.finish(identifier, "succeeded", Evidence(terminal=True))
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(run, range(30)))
        totals = self.store.summaries(["a"])["accounts"]["a"]["totals"]
        self.assertEqual((totals["attempts"], totals["succeeded"], totals["in_progress"]), (30, 30, 0))


class UsageBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.store = UsageStore(Path(self.sandbox.name) / "usage.sqlite3")


class UsageEvidenceTests(UsageBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.trace = UsageTrace(self.store, self.store.start("a", "auto"))
    async def test_images_exclude_inputs_history_progress_and_duplicates(self):
        self.trace.exclude_inputs([{"id": "history", "content": {"parts": [asset("input")]}}])
        for value in (
            event(role="user", parts=[asset("user")]),
            event(message_id="history", parts=[asset("history-echo")]),
            event(parts=[asset("preview")], status="in_progress", end_turn=False),
            event(parts=[asset("input"), asset("generated")]),
            event(role="tool", parts=[asset("generated"), {**asset("other"), "asset_pointer": "sediment://other"}]),
            event(parts=[asset("other")]),
        ):
            self.trace.observe_event(value)
        self.assertEqual(self.trace.evidence.generated_assets, frozenset(("generated", "other")))
        await self.trace.finish("succeeded")
        totals = self.store.summaries(["a"])["accounts"]["a"]["totals"]
        self.assertEqual((totals["image_requests"], totals["generated_images"]), (1, 2))

    async def test_tool_call_and_done_alone_are_not_images_or_completion(self):
        value = event(status="in_progress", end_turn=False)
        value["message"]["recipient"] = "image_gen"
        async for _ in self.trace.observe_lines(lines([value, "[DONE]"])):
            pass
        self.assertTrue(self.trace.evidence.image_requested)
        self.assertEqual(self.trace.evidence.generated_assets, frozenset())
        self.assertFalse(self.trace.complete)

    async def test_error_after_text_and_completed_image_is_failed_evidence(self):
        sequence = [event(parts=[asset("generated")]), {"error": {"message": "sensitive upstream text"}}]
        with self.assertRaises(HTTPException) as context:
            async for _ in self.trace.observe_lines(lines(sequence)):
                pass
        self.assertNotIn("sensitive", str(context.exception.detail))
        self.assertFalse(self.trace.complete)
        await self.trace.finish("failed", "stream_error")
        self.assertEqual(self.store.summaries(["a"])["accounts"]["a"]["totals"]["generated_images"], 1)

    async def test_failed_status_and_malformed_json_are_not_success(self):
        with self.assertRaises(HTTPException):
            self.trace.observe_event(event(status="failed"))
        with self.assertRaises(HTTPException):
            async for _ in self.trace.observe_lines(lines(["{broken"])):
                pass

    async def test_image_tool_completion_requires_done_when_no_assistant_end_turn(self):
        image = event(role="tool", end_turn=False, parts=[asset("image-result")])
        self.trace.observe_event(image)
        self.assertFalse(self.trace.complete)
        async for _ in self.trace.observe_lines(lines(["[DONE]"])):
            pass
        self.assertTrue(self.trace.complete)


class PipelineTests(UsageBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async def split_content(text, maximum, model):
            return text, len(text), "stop"
        namespace = {"asyncio": asyncio, "json": json, "random": random, "re": re,
                     "string": string, "time": time, "types": types, "HTTPException": HTTPException,
                     "logger": logging.getLogger("offline-test"), "model_system_fingerprint": {},
                     "split_tokens_from_content": split_content, "moderation_message": "moderated"}
        self.ns = source_functions("chatgpt/chatFormat.py", {"head_process_response", "stream_response", "format_not_stream_response"}, namespace)
        owner = self
        class FakeService:
            def __init__(self, token):
                self.req_token, self.usage_trace, self.closed = token, None, False
                self.req_model, self.resp_model, self.prompt_tokens, self.history_disabled = "mapped-model", "alias", 0, True
            async def set_dynamic_data(self, data):
                self.data = data
                if data.get("setup_error"):
                    raise HTTPException(401, "invalid credential")
            async def get_chat_requirements(self):
                pass
            async def prepare_send_conversation(self):
                self.chat_request = {"messages": []}
                if self.data.get("prepare_error"):
                    raise HTTPException(400, "invalid message")
            async def send_conversation(self):
                observed = self.usage_trace.observe_lines(lines(self.data["events"]))
                remaining, valid = await owner.ns["head_process_response"](observed)
                if not valid:
                    raise HTTPException(403, "invalid stream")
                converted = owner.ns["stream_response"](self, remaining, "alias", 100)
                if self.data.get("stream", True):
                    return converted
                return await owner.ns["format_not_stream_response"](converted, 0, 100, "alias")
            async def close_client(self):
                self.closed = True
            async def get_download_url(self, file_id):
                return "https://example.invalid/image.png"
        async def start_trace(*args):
            owner.trace = UsageTrace(owner.store, owner.store.start("a", "alias"))
            return owner.trace
        namespace.update({"ChatService": FakeService, "start_trace": start_trace})
        source_functions("api/chat2api.py", {"to_send_conversation", "finish_attempt", "tracked_stream", "process"}, namespace)

    async def test_finished_only_response_keeps_first_content_in_both_modes(self):
        for stream in (True, False):
            service, result = await self.ns["process"]({"events": [event("first and only"), "[DONE]"], "stream": stream}, "fake")
            text = "".join([chunk async for chunk in result]) if stream else json.dumps(result)
            self.assertIn("first and only", text)
            self.assertTrue(service.closed)
        self.assertEqual(self.store.summaries(["a"])["accounts"]["a"]["totals"]["succeeded"], 2)

    async def test_partial_then_error_never_counts_as_success(self):
        sequence = [event("partial", status="in_progress", end_turn=False), {"error": "private raw error"}, "[DONE]"]
        service, result = await self.ns["process"]({"events": sequence}, "fake")
        text = "".join([chunk async for chunk in result])
        self.assertIn("upstream_stream_error", text)
        self.assertNotIn("private raw error", text)
        self.assertTrue(service.closed)
        with self.assertRaises(HTTPException):
            await self.ns["process"]({"events": sequence, "stream": False}, "fake")
        totals = self.store.summaries(["a"])["accounts"]["a"]["totals"]
        self.assertEqual((totals["failed"], totals["succeeded"]), (2, 0))

    async def test_late_error_after_final_message_is_not_ignored(self):
        sequence = [event(), {"type": "metadata"}, {"error": "late error"}, "[DONE]"]
        _, result = await self.ns["process"]({"events": sequence}, "fake")
        text = "".join([chunk async for chunk in result])
        self.assertIn("upstream_stream_error", text)
        self.assertEqual(self.store.summaries(["a"])["accounts"]["a"]["totals"]["failed"], 1)

    async def test_incomplete_eof_and_client_close_have_distinct_outcomes(self):
        sequence = [event(status="in_progress", end_turn=False)]
        _, result = await self.ns["process"]({"events": sequence}, "fake")
        self.assertIn("incomplete_stream", "".join([chunk async for chunk in result]))
        service, result = await self.ns["process"]({"events": sequence}, "fake")
        await anext(result)
        await result.aclose()
        self.assertTrue(service.closed)
        totals = self.store.summaries(["a"])["accounts"]["a"]["totals"]
        self.assertEqual((totals["interrupted"], totals["cancelled"], totals["succeeded"]), (1, 1, 0))

    async def test_preflight_and_preparation_errors_are_recorded(self):
        for key in ("setup_error", "prepare_error"):
            with self.assertRaises(HTTPException):
                await self.ns["process"]({key: True}, "fake")
        self.assertEqual(self.store.summaries(["a"])["accounts"]["a"]["totals"]["failed"], 2)

    async def test_close_after_done_does_not_overwrite_success(self):
        _, result = await self.ns["process"]({"events": [event(), "[DONE]"]}, "fake")
        while True:
            chunk = await anext(result)
            if chunk.strip() == "data: [DONE]":
                break
        await result.aclose()
        totals = self.store.summaries(["a"])["accounts"]["a"]["totals"]
        self.assertEqual((totals["succeeded"], totals["cancelled"]), (1, 0))

    async def test_memory_only_trace_still_rejects_incomplete_stream(self):
        trace = UsageTrace(None, None)
        async def start_without_disk(*args):
            return trace
        self.ns["start_trace"] = start_without_disk
        _, result = await self.ns["process"]({"events": [event(status="in_progress", end_turn=False), "[DONE]"]}, "fake")
        self.assertIn("incomplete_stream", "".join([chunk async for chunk in result]))

    async def test_completed_image_only_response_in_both_modes(self):
        sequence = [event(role="tool", end_turn=False, parts=[asset("generated")]), "[DONE]"]
        for stream in (True, False):
            service, result = await self.ns["process"]({"events": sequence, "stream": stream}, "fake")
            text = "".join([chunk async for chunk in result]) if stream else json.dumps(result)
            self.assertIn("image.png", text)
            self.assertNotIn("incomplete_stream", text)
            self.assertTrue(service.closed)
        totals = self.store.summaries(["a"])["accounts"]["a"]["totals"]
        self.assertEqual((totals["succeeded"], totals["generated_images"]), (2, 2))


if __name__ == "__main__":
    unittest.main()
