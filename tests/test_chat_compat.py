"""Packet-independent local token limits and Chat Completions finish reasons."""
import ast
import json
import logging
from pathlib import Path
import random
import re
import string
import time
from types import SimpleNamespace
import unittest

from fastapi import HTTPException
import tiktoken


ROOT = Path(__file__).resolve().parents[1]
ENCODING = tiktoken.get_encoding("cl100k_base")


async def values(rows):
    for row in rows:
        yield row


def event(text, *, completed=False):
    return "data: " + json.dumps({"conversation_id": "synthetic-conversation", "message": {
        "id": "synthetic-message", "author": {"role": "assistant"},
        "status": "finished_successfully" if completed else "in_progress", "end_turn": completed,
        "content": {"content_type": "text", "parts": [text]}, "metadata": {}}})


def completion(text=None, reason=None, **extra):
    return "data: " + json.dumps({"id": "synthetic-completion", "choices": [
        {"index": 0, "delta": {"content": text} if text is not None else {}, "finish_reason": reason}], **extra}) + "\n\n"


class ChatCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Do not import the running app, credential stores, or account settings.
        tree = ast.parse((ROOT / "chatgpt/chatFormat.py").read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                 and node.name in ("stream_response", "format_not_stream_response")]
        self.ns = {"json": json, "random": random, "re": re, "string": string, "time": time,
                   "HTTPException": HTTPException, "model_system_fingerprint": {},
                   "logger": logging.getLogger("offline-chat-compat"), "moderation_message": "moderated"}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "chatgpt/chatFormat.py", "exec"), self.ns)
        self.service = SimpleNamespace(history_disabled=True, usage_trace=None)

    async def stream(self, text, *, cuts=(), maximum=100):
        rows = [event(text[:cut]) for cut in cuts] + [event(text, completed=True), "data: [DONE]"]
        chunks = [chunk async for chunk in self.ns["stream_response"](self.service, values(rows), "offline-model", maximum)]
        parsed = [json.loads(chunk[6:]) for chunk in chunks if chunk.startswith("data: {")]
        content = "".join(row["choices"][0]["delta"].get("content", "") for row in parsed)
        reasons = [row["choices"][0]["finish_reason"] for row in parsed if row["choices"][0]["finish_reason"]]
        return chunks, parsed, content, reasons

    async def test_same_content_different_packet_boundaries_has_identical_limit_and_reason(self):
        text = "Hello world. This sentence must be limited by tokens, never by packets."
        expected = ENCODING.decode_bytes(ENCODING.encode(text)[:5]).decode("utf-8", errors="ignore")
        for cuts in ((), (5, 12, 25), tuple(range(1, len(text)))):
            with self.subTest(cuts=len(cuts)):
                chunks, _, output, reasons = await self.stream(text, cuts=cuts, maximum=5)
                self.assertEqual(output, expected)
                self.assertEqual(reasons, ["length"])
                self.assertEqual(chunks[-1].strip(), "data: [DONE]")

    async def test_one_token_word_split_into_many_packets_is_not_prematurely_cut(self):
        for cuts in ((), (1, 2, 3, 4)):
            _, _, content, reasons = await self.stream("Hello", cuts=cuts, maximum=1)
            self.assertEqual(content, "Hello")
            self.assertEqual(reasons, ["stop"])

    async def test_repeated_unchanged_events_do_not_consume_tokens(self):
        _, _, content, reasons = await self.stream("Hello world", cuts=(1,) * 50 + (5,) * 50, maximum=2)
        self.assertEqual(content, "Hello world")
        self.assertEqual(reasons, ["stop"])

    async def test_finished_only_response_keeps_first_content_and_local_usage(self):
        chunks, _, content, reasons = await self.stream("First and only answer.")
        output = await self.ns["format_not_stream_response"](values(chunks), 7, 100, "offline-model")
        self.assertEqual(output["choices"][0]["message"]["content"], content)
        self.assertEqual(reasons, ["stop"])
        self.assertEqual(output["usage"]["completion_tokens"], len(ENCODING.encode(content)))
        self.assertEqual(output["usage"]["total_tokens"], 7 + len(ENCODING.encode(content)))

    async def test_unicode_truncation_never_emits_replacement_character(self):
        text = "你好世界。🦊🐻🌎 Hello world"
        for maximum in range(1, 10):
            coarse = await self.stream(text, maximum=maximum)
            fragmented = await self.stream(text, cuts=tuple(range(1, len(text))), maximum=maximum)
            self.assertEqual(coarse[2:], fragmented[2:])
            self.assertNotIn("\ufffd", coarse[2])
            self.assertLessEqual(len(ENCODING.encode(coarse[2])), maximum)

    async def test_literal_tokenizer_special_strings_are_plain_user_visible_text(self):
        text = "Example: <|endoftext|> and <|fim_prefix|> are literal text."
        chunks, _, content, reasons = await self.stream(text, cuts=tuple(range(1, len(text))))
        self.assertEqual(content, text)
        self.assertEqual(reasons, ["stop"])
        response = await self.ns["format_not_stream_response"](values(chunks), 0, 100, "offline-model")
        self.assertEqual(response["choices"][0]["message"]["content"], text)

    async def test_nonstream_preserves_length_even_when_returned_text_is_short(self):
        rows = [completion("tiny"), completion(reason="length"), "data: [DONE]"]
        result = await self.ns["format_not_stream_response"](values(rows), 1, 100, "offline-model")
        self.assertEqual(result["choices"][0]["finish_reason"], "length")
        self.assertEqual(result["choices"][0]["message"]["content"], "tiny")

    async def test_nonstream_preserves_stop_for_exact_natural_token_limit(self):
        rows = [completion("Hello", "stop"), "data: [DONE]"]
        result = await self.ns["format_not_stream_response"](values(rows), 0, 1, "offline-model")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(result["usage"]["completion_tokens"], 1)

    async def test_nonstream_defensive_truncation_still_returns_length(self):
        rows = [completion("Hello world", "stop"), "data: [DONE]"]
        result = await self.ns["format_not_stream_response"](values(rows), 0, 1, "offline-model")
        self.assertEqual(result["choices"][0]["finish_reason"], "length")
        self.assertEqual(result["choices"][0]["message"]["content"], "Hello")

    async def test_nonstream_tolerates_optional_usage_only_chunk_without_double_counting(self):
        rows = [completion("Hello", "stop"), 'data: {"choices":[],"usage":{"total_tokens":999}}', "data: [DONE]"]
        result = await self.ns["format_not_stream_response"](values(rows), 0, 10, "offline-model")
        self.assertEqual(result["usage"]["total_tokens"], 1)

    async def test_late_upstream_failure_after_token_limit_is_not_hidden(self):
        rows = [event("Hello world. Still continuing"), 'data: {"error":"private upstream text"}', "data: [DONE]"]
        stream = self.ns["stream_response"](self.service, values(rows), "offline-model", 1)
        with self.assertRaises(HTTPException) as raised:
            [chunk async for chunk in stream]
        self.assertNotIn("private", str(raised.exception.detail))

    async def test_done_flushes_pending_tail_without_adding_usage_chunk(self):
        # Image/tool-only output can end with DONE rather than assistant end_turn.
        rows = [event("A pending suffix"), "data: [DONE]"]
        chunks = [chunk async for chunk in self.ns["stream_response"](self.service, values(rows), "offline-model", 100)]
        parsed = [json.loads(chunk[6:]) for chunk in chunks if chunk.startswith("data: {")]
        self.assertEqual("".join(row["choices"][0]["delta"].get("content", "") for row in parsed), "A pending suffix")
        self.assertTrue(all("usage" not in row for row in parsed))
        # The converter itself must not invent a successful upstream terminal.
        self.assertTrue(all(row["choices"][0]["finish_reason"] is None for row in parsed))

    async def test_malformed_empty_choice_is_safe_error_instead_of_index_error(self):
        with self.assertRaises(HTTPException) as raised:
            await self.ns["format_not_stream_response"](values(['data: {"choices":[]}']), 0, 100, "offline-model")
        self.assertEqual(raised.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
