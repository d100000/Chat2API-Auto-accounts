"""Offline API ingress checks: no deployment settings or network connections."""

import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote
from unittest.mock import patch

from fastapi import Request

from api.public_gateway import GatewaySettings, MAX_BODY_BYTES, create_app, load_settings
from chatgpt.api_keys import APIKeyStore


KEY = "test-only-public-ingress-key"


class Response:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def aiter_content(self):
        yield b'{"id":"test","choices":[{"message":{"content":"OK"}}]}'


class Client:
    def __init__(self, factory, options):
        self.factory, self.options, self.closed = factory, options, False

    async def post(self, url, **kwargs):
        self.factory.calls.append((url, kwargs))
        if self.factory.wait is not None:
            await self.factory.wait.wait()
        if self.factory.failure:
            raise RuntimeError("secret must not be echoed: " + KEY)
        return self.factory.response

    async def close(self):
        self.closed = True


class Factory:
    def __init__(self):
        self.clients, self.calls = [], []
        self.wait, self.failure, self.response = None, False, Response()

    def __call__(self, **options):
        client = Client(self, options)
        self.clients.append(client)
        return client


def scope(path, method="POST", headers=None):
    return {"type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": unquote(path.split("?", 1)[0]),
        "raw_path": path.split("?", 1)[0].encode(),
        "query_string": path.split("?", 1)[1].encode() if "?" in path else b"",
        "root_path": "", "headers": headers or [], "server": ("127.0.0.1", 5006),
        "client": ("198.51.100.20", 45678), "asgi": {"version": "3.0", "spec_version": "2.4"}}


async def exchange(app, path="/v1/chat/completions", method="POST", data=None, key=KEY, headers=(), raw=None):
    body = raw if raw is not None else json.dumps(data if data is not None else {
        "model": "auto", "messages": [{"role": "user", "content": "Offline test."}], "stream": False}).encode()
    all_headers = [(b"content-type", b"application/json"), *headers]
    if key is not None:
        all_headers.append((b"authorization", ("Bearer " + key).encode()))
    sent = False
    complete = asyncio.Event()
    output = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await complete.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        output.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            complete.set()

    await app(scope(path, method, all_headers), receive, send)
    start = next(row for row in output if row["type"] == "http.response.start")
    payload = b"".join(row.get("body", b"") for row in output if row["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), payload


class PublicGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.factory = Factory()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.keys = APIKeyStore(Path(self.directory.name) / "api-keys.sqlite3")
        self.app = create_app(GatewaySettings(keys=(KEY,)), self.factory, self.keys)

    async def test_authentication_rejects_missing_wrong_and_direct_tokens(self):
        for key in (None, "incorrect", "eyJhbGciOi-test-upstream", "fk-upstream", "seed-test", KEY + " "):
            with self.subTest(key=key):
                status, headers, body = await exchange(self.app, key=key)
                self.assertEqual(status, 401)
                self.assertEqual(headers[b"www-authenticate"], b"Bearer")
                self.assertNotIn(KEY.encode(), body)
        self.assertFalse(self.factory.calls)

    async def test_duplicate_authorization_is_rejected(self):
        status, _, _ = await exchange(self.app, headers=((b"authorization", b"Bearer " + KEY.encode()),))
        self.assertEqual(status, 401)
        self.assertFalse(self.factory.calls)

    async def test_managed_key_changes_apply_without_gateway_restart_or_double_counting(self):
        created = self.keys.create("public service")
        token, identifier = created["api_key"], created["key"]["id"]
        self.assertEqual((await exchange(self.app, key=token))[0], 200)
        self.assertEqual(self.factory.calls[-1][1]["headers"]["Authorization"], "Bearer " + token)
        self.assertEqual((await exchange(self.app, "/v1/models", "GET", key=token))[0], 200)
        self.assertEqual(self.keys.list_keys()[0]["request_count"], 0)
        self.keys.set_status(identifier, "disabled")
        self.assertEqual((await exchange(self.app, key=token))[0], 401)
        self.keys.set_status(identifier, "active")
        self.assertEqual((await exchange(self.app, key=token))[0], 200)
        self.keys.set_status(identifier, "revoked")
        self.assertEqual((await exchange(self.app, key=token))[0], 401)
        self.assertEqual((await exchange(self.app, "/v1/models", "GET", key=token))[0], 401)
        self.assertEqual(len(self.factory.calls), 2)

    async def test_managed_keys_fail_closed_without_system_key_or_database(self):
        token = self.keys.create("service")["api_key"]
        app = create_app(GatewaySettings(), self.factory, self.keys)
        self.assertEqual((await exchange(app, key=token))[0], 503)
        self.assertEqual((await exchange(self.app, key="sk-c2a-unknown"))[0], 401)
        self.keys.path.write_bytes(b"corrupt")
        self.assertEqual((await exchange(self.app, key=token))[0], 503)
        self.assertFalse(self.factory.calls)

    async def test_remote_image_sources_are_rejected_for_every_message_role(self):
        for role in ("system", "user", "assistant", "tool", "function"):
            for url in ("http://127.0.0.1:8123/private-document", "http://169.254.169.254/latest/meta-data/",
                        "https://example.invalid/image.png", "http://[::1]/private", "file:///private/file",
                        "https://image.invalid/redirect", "data:text/plain;base64,SGVsbG8="):
                body = {"messages": [{"role": role, "content": [{"type": "image_url", "image_url": {"url": url}}]}]}
                status, _, payload = await exchange(self.app, data=body)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(payload)["error"]["code"], "remote_image_url_unavailable")
        self.assertFalse(self.factory.calls)
        self.assertFalse(self.factory.clients)

    async def test_nested_or_malformed_attachment_shapes_cannot_bypass_validation(self):
        malformed = (None, 1, "not a part", [], [{"type": "image_url", "image_url": {"url": "http://localhost"}}],
            {"type": "image_url", "image_url": "http://localhost"},
            {"type": "image_url", "image_url": {"url": {"url": "http://localhost"}}},
            {"type": "text", "text": {"image_url": "http://localhost"}},
            {"type": "text", "text": "hello", "image_url": {"url": "http://localhost"}},
            {"type": "file", "file_url": "http://localhost"},
            {"type": "input_image", "image_url": "http://localhost"})
        for part in malformed:
            body = {"messages": [{"role": "user", "content": [part]}]}
            self.assertEqual((await exchange(self.app, data=body))[0], 400)
        for messages in ({"content": "http://localhost"}, [None], [1], [[{"content": "http://localhost"}]],
                         [{"role": "user", "content": {"image_url": {"url": "http://localhost"}}}]):
            self.assertEqual((await exchange(self.app, data={"messages": messages}))[0], 400)
        self.assertFalse(self.factory.calls)

    async def test_inline_images_are_decoded_and_bounded_before_forwarding(self):
        encoded = base64.b64encode(b"offline-image-fixture").decode()
        for mime in ("png", "jpeg", "webp", "gif"):
            body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "Describe this."},
                {"type": "image_url", "image_url": {"url": "data:image/" + mime + ";base64," + encoded, "detail": "low"}}]}]}
            self.assertEqual((await exchange(self.app, data=body))[0], 200)
        self.assertEqual(len(self.factory.calls), 4)
        for data_url in ("data:image/png;base64,A", "data:image/png;base64,AAAA=", "data:image/png;base64,***",
                         "data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,"):
            body = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url}}]}]}
            self.assertEqual((await exchange(self.app, data=body))[0], 400)
        body = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + encoded}}]}]}
        with patch("api.public_gateway.MAX_IMAGE_BYTES", 4):
            self.assertEqual((await exchange(self.app, data=body))[0], 413)
        self.assertEqual(len(self.factory.calls), 4)

    async def test_public_image_generation_and_textual_urls_still_work(self):
        body = {"model": "auto", "quota_feature": "image_gen", "history_disabled": False, "stream": False,
                "messages": [{"role": "user", "content": "Generate an image of a blue circle. Reference: https://example.invalid/"}]}
        self.assertEqual((await exchange(self.app, data=body))[0], 200)
        self.assertEqual(self.factory.calls[0][1]["json"], body)

    async def test_automatic_url_upload_configuration_is_fail_closed(self):
        app = create_app(GatewaySettings(keys=(KEY,), upload_by_url=True), self.factory, self.keys)
        for path, method in (("/v1/models", "GET"), ("/v1/chat/completions", "POST")):
            status, _, body = await exchange(app, path, method)
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["error"]["code"], "unsafe_upload_configuration")
        self.assertFalse(self.factory.calls)
        for setting in ("true", "1", "t", "y", "yes"):
            with patch("api.public_gateway.dotenv_values", return_value={"AUTHORIZATION": KEY, "UPLOAD_BY_URL": setting}), \
                    patch("api.public_gateway.os.environ", {}):
                with self.assertRaisesRegex(ValueError, "UPLOAD_BY_URL=false"):
                    load_settings()
        with patch("api.public_gateway.dotenv_values", return_value={"AUTHORIZATION": KEY, "UPLOAD_BY_URL": "false"}), \
                patch("api.public_gateway.os.environ", {}):
            self.assertFalse(load_settings().upload_by_url)

    async def test_no_keys_is_fail_closed(self):
        status, _, _ = await exchange(create_app(GatewaySettings(), self.factory))
        self.assertEqual(status, 503)
        self.assertFalse(self.factory.calls)

    async def test_routes_and_encoded_variants_cannot_reach_private_app(self):
        for path in ("/", "/accounts", "/login", "/tokens", "/tokens/error", "/tokens/clear", "/tokens/add/value",
                     "/local-console/session", "/seed_tokens/clear", "/docs", "/openapi.json",
                     "/v1/chat/completions/", "/v1/%63hat/completions", "//v1/chat/completions",
                     "/v1/chat/completions?path=/tokens", "/v1/images/generations"):
            for method in ("GET", "POST"):
                with self.subTest(path=path, method=method):
                    status, _, _ = await exchange(self.app, path, method)
                    self.assertEqual(status, 404)
        for method in ("GET", "PUT", "OPTIONS", "DELETE", "HEAD"):
            self.assertEqual((await exchange(self.app, method=method))[0], 404)
        self.assertFalse(self.factory.calls)

    async def test_models_requires_auth_and_only_lists_supported_default(self):
        self.assertEqual((await exchange(self.app, "/v1/models", "GET", key=None))[0], 401)
        status, headers, body = await exchange(self.app, "/v1/models", "GET")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in json.loads(body)["data"]], ["auto"])
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertFalse(self.factory.calls)

    async def test_only_fixed_target_and_required_headers_are_forwarded(self):
        app = create_app(GatewaySettings(keys=("other-test-key", KEY), api_prefix="private/api"), self.factory)
        status, _, body = await exchange(app, headers=((b"cookie", b"admin=private"),
            (b"host", b"evil.invalid"), (b"origin", b"https://evil.invalid"),
            (b"x-forwarded-host", b"evil.invalid"), (b"chatgpt-account-id", b"bad")))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], "test")
        url, options = self.factory.calls[0]
        self.assertEqual(url, "http://127.0.0.1:5005/private/api/v1/chat/completions")
        self.assertEqual(options["headers"], {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
        self.assertFalse(options["allow_redirects"])
        self.assertFalse(self.factory.clients[0].options["trust_env"])
        self.assertTrue(self.factory.clients[0].closed)
        self.assertEqual(app.state.active_requests, 0)

    async def test_invalid_prefix_cannot_change_destination(self):
        for prefix in ("../tokens", "https://evil.invalid", "api?x=1", "/api", "api//x", "api%2ftokens"):
            with self.assertRaises(ValueError):
                GatewaySettings(keys=(KEY,), api_prefix=prefix)

    async def test_streaming_disabled_is_explicit_and_omitted_stream_is_false(self):
        status, _, body = await exchange(self.app, data={"stream": True})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "streaming_unavailable")
        self.assertFalse(self.factory.calls)
        self.assertEqual((await exchange(self.app, data={"messages": []}))[0], 200)
        self.assertIs(self.factory.calls[0][1]["json"]["stream"], False)

    async def test_invalid_bodies_and_declared_or_streamed_oversize_are_rejected(self):
        for raw in (b"invalid", b"[]", b'{"stream":"false"}'):
            self.assertEqual((await exchange(self.app, raw=raw))[0], 400)
        self.assertEqual((await exchange(self.app, headers=((b"content-length", str(MAX_BODY_BYTES + 1).encode()),)))[0], 413)
        self.assertEqual((await exchange(self.app, raw=b" " * (MAX_BODY_BYTES + 1)))[0], 413)
        self.assertFalse(self.factory.calls)
        self.assertEqual(self.app.state.active_requests, 0)

    async def test_upstream_errors_and_redirects_do_not_leak_response(self):
        for upstream, expected in ((302, 502), (429, 429), (500, 500)):
            response = Response()
            response.status_code = upstream
            response.headers = {"retry-after": "30", "set-cookie": "private=value", "location": "https://evil.invalid"}
            self.factory.response = response
            status, headers, body = await exchange(self.app)
            self.assertEqual(status, expected)
            self.assertNotIn(b"set-cookie", headers)
            self.assertNotIn(b"location", headers)
            self.assertEqual(headers[b"retry-after"], b"30")
            self.assertNotIn(KEY.encode(), body)
        self.factory.failure = True
        status, _, body = await exchange(self.app)
        self.assertEqual(status, 502)
        self.assertNotIn(KEY.encode(), body)
        self.assertTrue(all(client.closed for client in self.factory.clients))

    async def test_concurrency_bound_and_completion_release_capacity(self):
        self.factory.wait = asyncio.Event()
        tasks = [asyncio.create_task(exchange(self.app)) for _ in range(10)]
        try:
            for _ in range(100):
                if len(self.factory.calls) == 10:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(len(self.factory.calls), 10)
            self.assertEqual((await exchange(self.app))[0], 429)
            self.assertEqual(self.app.state.active_requests, 10)
        finally:
            self.factory.wait.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self.app.state.active_requests, 0)
        self.assertTrue(all(client.closed for client in self.factory.clients))

    async def test_cancellation_closes_upstream_and_releases_capacity(self):
        self.factory.wait = asyncio.Event()
        endpoint = next(route.endpoint for route in self.app.routes if route.path == "/v1/chat/completions")

        async def receive():
            return {"type": "http.request", "body": b'{"stream":false}', "more_body": False}

        request = Request(scope("/v1/chat/completions"), receive)
        request.state.api_key = KEY
        task = asyncio.create_task(endpoint(request))
        for _ in range(100):
            if self.factory.calls:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(self.app.state.active_requests, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.factory.clients[0].closed)
        self.assertEqual(self.app.state.active_requests, 0)

    async def test_enabled_streaming_preserves_chunks_and_closes_client(self):
        class Stream(Response):
            headers = {"content-type": "text/event-stream; charset=utf-8"}

            async def aiter_content(self):
                yield b'data: {"choices":[]}\n\n'
                yield b'data: [DONE]\n\n'

        self.factory.response = Stream()
        app = create_app(GatewaySettings(keys=(KEY,), streaming=True), self.factory)
        status, headers, body = await exchange(app, data={"stream": True})
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"x-accel-buffering"], b"no")
        self.assertEqual(body, b'data: {"choices":[]}\n\ndata: [DONE]\n\n')
        self.assertTrue(self.factory.clients[0].closed)
        self.assertEqual(app.state.active_requests, 0)

    async def test_response_body_is_bounded(self):
        with patch("api.public_gateway.MAX_RESPONSE_BYTES", 4):
            status, _, body = await exchange(self.app)
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["code"], "upstream_response_too_large")
        self.assertTrue(self.factory.clients[0].closed)


if __name__ == "__main__":
    unittest.main()
