"""Inherited gateway capacity tests with fake clients and ASGI disconnects."""
import ast
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import random
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from chatgpt.account_scheduler import AccountScheduler
from chatgpt.gateway_capacity import GatewayCapacity, admit_generation
from utils.disconnect import ClientDisconnected


class Quota:
    def get_accounts(self): return {"accounts": []}
    def _groups(self): return {}


def access(user):
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "test-workspace", "chatgpt_user_id": user}}
    return "header." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".signature"


class Source:
    def __init__(self): self.closed = 0
    def __aiter__(self): return self
    async def __anext__(self): raise StopAsyncIteration
    async def aclose(self): self.closed += 1


class GatewayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = AccountScheduler(Quota(), state_path=None)

    async def test_unconsumed_stream_background_closes_source_client_then_releases(self):
        lease = await self.scheduler.acquire_for_account("synthetic-credential")
        capacity = GatewayCapacity(self.scheduler, lease)
        events = []
        source = Source()
        async def close():
            self.assertEqual(len(self.scheduler._active), 1)
            self.assertEqual(source.closed, 1)
            events.append("closed")
        capacity.bind_cleanup(close)
        stream = capacity.stream(source)
        await BackgroundTask(capacity.finish)()
        await capacity.finish("succeeded")
        await stream.aclose()
        self.assertEqual(events, ["closed"])
        self.assertEqual(source.closed, 1)
        self.assertEqual(len(self.scheduler._active), 0)

    async def test_repeated_cancellation_waits_for_source_and_client_cleanup(self):
        lease = await self.scheduler.acquire_for_account("synthetic-credential")
        capacity = GatewayCapacity(self.scheduler, lease)
        entered, release = asyncio.Event(), asyncio.Event()
        closed = []
        async def source():
            try:
                yield b"data: test\n\n"
            finally:
                entered.set()
                await release.wait()
                closed.append("source")
        async def close(): closed.append("client")
        capacity.bind_cleanup(close)
        stream = capacity.stream(source())
        await anext(stream)
        task = asyncio.create_task(stream.aclose())
        await entered.wait()
        task.cancel(); await asyncio.sleep(0)
        task.cancel(); await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(len(self.scheduler._active), 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(closed, ["source", "client"])
        self.assertEqual(len(self.scheduler._active), 0)

    async def test_non_generation_routes_never_admit_and_invalid_generation_cannot_bypass(self):
        with patch("chatgpt.account_scheduler.get_scheduler") as get:
            self.assertIsNone(await admit_generation(SimpleNamespace(method="GET"), "backend-api/conversation", "synthetic", b"{}"))
            self.assertIsNone(await admit_generation(SimpleNamespace(method="POST"), "backend-api/sentinel/chat-requirements", "synthetic", b"{}"))
            get.assert_not_called()
        for token, body, status in (("", b"{}", 401), ("synthetic", b"[]", 400), ("synthetic", b"invalid", 400)):
            with self.assertRaises(HTTPException) as error:
                await admit_generation(SimpleNamespace(method="POST"), "backend-api/conversation", token, body)
            self.assertEqual(error.exception.status_code, status)

    async def test_image_classification_uses_verified_identity_and_capacity_only(self):
        from chatgpt.account_quota import _identity
        body = json.dumps({"messages": [{"author": {"role": "user"}, "content": {"parts": ["请生成一张图片"]}}]}).encode()
        with patch("chatgpt.account_scheduler.get_scheduler", return_value=self.scheduler):
            capacity = await admit_generation(SimpleNamespace(method="POST"), "backend-api/conversation", "synthetic-refresh",
                body, access=access("user-one"), account_override="other-workspace")
        self.assertEqual(capacity.lease.account_id, _identity(access("user-one"), override="other-workspace")["id"])
        self.assertEqual(capacity.lease.feature, "image_gen")
        self.assertEqual(capacity.lease.quota_features, ())
        await capacity.finish("succeeded")


class ReverseProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = AccountScheduler(Quota(), state_path=None)
        self.patch = patch("chatgpt.account_scheduler.get_scheduler", return_value=self.scheduler)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.clients, self.selected, self.fingerprints = [], [], []
        self.started, self.block = asyncio.Event(), None
        self.fail = False
        test = self
        class Client:
            def __init__(self, **kwargs): self.closed = 0; test.clients.append(self)
            async def request(self, method, url, **kwargs):
                test.headers = kwargs["headers"]
                test.started.set()
                if test.block: await test.block.wait()
                if test.fail: raise RuntimeError("private-upstream-url-and-credential")
                async def text(): return "resource"
                async def content(): return b"resource"
                return SimpleNamespace(headers={"content-type": "text/event-stream" if "conversation" in url else "text/plain"},
                    status_code=200, cookies={}, atext=text, acontent=content)
            async def close(self): self.closed += 1
        async def resolve(token): test.selected.append(token); return "credential-" + token
        async def verify(token): return access(token)
        def fingerprint(token): test.fingerprints.append(token); return {"user-agent": "synthetic-agent"}
        def source(*args): test.source = Source(); return test.source
        namespace = {"Request": Request, "Response": Response, "StreamingResponse": StreamingResponse,
            "BackgroundTask": BackgroundTask, "HTTPException": HTTPException, "json": json, "random": random,
            "hashlib": hashlib, "time": time, "get_real_req_token": resolve, "verify_token": verify,
            "get_fp": fingerprint, "Client": Client, "content_generator": source,
            "logger": SimpleNamespace(info=lambda *a: None, error=lambda *a: None),
            "headers_accept_list": ("authorization", "chatgpt-account-id"), "chatgpt_base_url_list": [],
            "sentinel_proxy_url_list": [], "force_no_history": False, "voice_host": "", "file_host": ""}
        source_path = Path(__file__).resolve().parents[1] / "gateway/reverseProxy.py"
        tree = ast.parse(source_path.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "chatgpt_reverse_proxy")
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)
        self.route = namespace["chatgpt_reverse_proxy"]

    def request(self, *, path="backend-api/conversation", header="header-selector", cookie="cookie-selector", method="POST"):
        queue = asyncio.Queue()
        queue.put_nowait({"type": "http.request", "body": b'{"messages":[]}', "more_body": False})
        headers = [(b"host", b"localhost")]
        if header: headers.append((b"authorization", ("Bearer " + header).encode()))
        if cookie: headers.append((b"cookie", ("token=" + cookie).encode()))
        scope = {"type": "http", "method": method, "path": "/" + path, "raw_path": ("/" + path).encode(),
                 "query_string": b"", "headers": headers, "scheme": "http", "server": ("localhost", 80), "client": ("127.0.0.1", 1)}
        return Request(scope, queue.get), queue

    async def test_authorization_account_wins_over_cookie_for_admission_and_fingerprint(self):
        request, _ = self.request()
        response = await self.route(request, "backend-api/conversation")
        self.assertEqual(self.selected, ["header-selector"])
        self.assertEqual(self.fingerprints, ["credential-header-selector"])
        self.assertEqual(self.headers["authorization"], "Bearer " + access("credential-header-selector"))
        self.assertEqual(len(self.scheduler._active), 1)
        await response.background()
        self.assertEqual(self.clients[0].closed, 1)
        self.assertEqual(self.source.closed, 1)
        self.assertEqual(len(self.scheduler._active), 0)

    async def test_cookie_only_generation_is_admitted(self):
        request, _ = self.request(header=None)
        response = await self.route(request, "backend-api/conversation")
        self.assertEqual(self.selected, ["cookie-selector"])
        self.assertEqual(len(self.scheduler._active), 1)
        await response.background()
        self.assertEqual(len(self.scheduler._active), 0)

    async def test_asgi_disconnect_while_upstream_pending_closes_and_releases(self):
        self.block = asyncio.Event()
        request, queue = self.request()
        task = asyncio.create_task(self.route(request, "backend-api/conversation"))
        await self.started.wait()
        self.assertEqual(len(self.scheduler._active), 1)
        queue.put_nowait({"type": "http.disconnect"})
        with self.assertRaises(ClientDisconnected): await task
        self.assertEqual(self.clients[0].closed, 1)
        self.assertEqual(len(self.scheduler._active), 0)

    async def test_resources_do_not_take_slots_and_errors_do_not_echo_private_values(self):
        request, _ = self.request(path="assets/app.js", header=None, cookie=None, method="GET")
        response = await self.route(request, "assets/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.scheduler._active), 0)
        self.assertEqual(self.clients[-1].closed, 1)
        self.fail = True
        request, _ = self.request()
        with self.assertRaises(HTTPException) as error: await self.route(request, "backend-api/conversation")
        self.assertEqual(error.exception.status_code, 502)
        self.assertNotIn("private-upstream", str(error.exception.detail))
        self.assertEqual(self.clients[-1].closed, 1)
        self.assertEqual(len(self.scheduler._active), 0)
