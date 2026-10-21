"""Offline API ingress checks: no deployment settings or network connections."""

import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote
from unittest.mock import patch

from anyio import CancelScope
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
        self.close_calls = 0

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def request(self, method, url, **kwargs):
        self.factory.methods.append(method)
        self.factory.calls.append((url, kwargs))
        self.factory.started.set()
        if self.factory.wait is not None:
            await self.factory.wait.wait()
        if self.factory.failure:
            raise RuntimeError("secret must not be echoed: " + KEY)
        return self.factory.response

    async def close(self):
        self.close_calls += 1
        self.closed = True


class Factory:
    def __init__(self):
        self.clients, self.calls, self.methods = [], [], []
        self.wait, self.failure, self.response = None, False, Response()
        self.started = asyncio.Event()

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
        self.assertEqual(len(self.factory.calls), 3)

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
                     "/v1/chat/completions?path=/tokens", "/v1/images/edits", "/v1/responses"):
            for method in ("GET", "POST"):
                with self.subTest(path=path, method=method):
                    status, _, _ = await exchange(self.app, path, method)
                    self.assertEqual(status, 404)
        for method in ("GET", "PUT", "OPTIONS", "DELETE", "HEAD"):
            self.assertEqual((await exchange(self.app, method=method))[0], 404)
        self.assertFalse(self.factory.calls)

    async def test_models_requires_auth_and_forwards_real_catalogue(self):
        class Catalogue(Response):
            async def aiter_content(self):
                yield b'{"object":"list","data":[{"id":"auto"},{"id":"gpt-image-2"},{"id":"gpt-4o"}]}'

        self.factory.response = Catalogue()
        self.assertEqual((await exchange(self.app, "/v1/models", "GET", key=None))[0], 401)
        status, headers, body = await exchange(self.app, "/v1/models", "GET")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in json.loads(body)["data"]], ["auto", "gpt-image-2", "gpt-4o"])
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertEqual(self.factory.methods, ["GET"])
        self.assertEqual(self.factory.calls[0][0], "http://127.0.0.1:5005/v1/models")
        self.assertNotIn("json", self.factory.calls[0][1])
        self.assertTrue(self.factory.clients[0].closed)
        self.assertEqual(self.app.state.active_requests, 0)

    async def test_single_model_uses_fixed_prefixed_target_and_preserves_not_found(self):
        class Model(Response):
            async def aiter_content(self):
                yield b'{"object":"model","id":"gpt-image-2","owned_by":"chat2api"}'

        self.factory.response = Model()
        app = create_app(GatewaySettings(keys=(KEY,), api_prefix="private/api"), self.factory, self.keys)
        status, _, body = await exchange(app, "/v1/models/gpt-image-2", "GET", headers=((b"cookie", b"admin=secret"), (b"x-forwarded-host", b"evil.invalid")))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], "gpt-image-2")
        url, options = self.factory.calls[0]
        self.assertEqual(url, "http://127.0.0.1:5005/private/api/v1/models/gpt-image-2")
        self.assertEqual(options["headers"], {"Authorization": "Bearer " + KEY})
        self.assertFalse(options["allow_redirects"])
        self.factory.response.status_code = 404
        status, _, body = await exchange(app, "/v1/models/unknown", "GET")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "upstream_request_failed")
        self.assertTrue(all(client.closed for client in self.factory.clients))

    async def test_model_identifiers_reject_encoding_query_and_invalid_characters(self):
        invalid = ("", "/", "_auto", ".auto", "-auto", "../tokens", "a/b", "a%2fb", "%61uto", "auto%3fkey", "auto%00", "auto%252f", "模型", "a" * 101, "auto?x=1")
        for identifier in invalid:
            with self.subTest(identifier=identifier):
                self.assertEqual((await exchange(self.app, "/v1/models/" + identifier, "GET"))[0], 404)
        self.assertFalse(self.factory.calls)
        for identifier in ("auto", "gpt-image-2", "A._-0", "a" * 100):
            self.assertEqual((await exchange(self.app, "/v1/models/" + identifier, "GET"))[0], 200)
        self.assertEqual(len(self.factory.calls), 4)

    async def test_generation_json_reaches_private_api_with_managed_key_once(self):
        class Images(Response):
            async def aiter_content(self):
                yield b'{"created":123,"data":[{"b64_json":"aW1hZ2U="}]}'

        self.factory.response = Images()
        managed = self.keys.create("images")
        app = create_app(GatewaySettings(keys=(KEY,), api_prefix="private/api"), self.factory, self.keys)
        payload = {"prompt": "Generate a blue circle.", "response_format": "b64_json", "quality": "auto", "n": 1}
        status, _, body = await exchange(app, "/v1/images/generations", data=payload, key=managed["api_key"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["b64_json"], "aW1hZ2U=")
        url, options = self.factory.calls[0]
        self.assertEqual(url, "http://127.0.0.1:5005/private/api/v1/images/generations")
        self.assertEqual(options["json"], {**payload, "stream": False})
        self.assertEqual(options["headers"]["Authorization"], "Bearer " + managed["api_key"])
        self.assertEqual(self.factory.clients[0].options["timeout"], (5, 600))
        self.assertFalse(self.factory.clients[0].options["trust_env"])
        self.assertTrue(self.factory.clients[0].closed)
        self.assertEqual(self.keys.list_keys()[0]["request_count"], 0)
        self.assertEqual(app.state.active_requests, 0)

    async def test_new_routes_require_auth_and_reject_alternate_methods_and_encoded_paths(self):
        for path, method in (("/v1/models/auto", "GET"), ("/v1/images/generations", "POST")):
            for key in (None, "incorrect", "eyJhbGciOi-direct-token"):
                self.assertEqual((await exchange(self.app, path, method, key=key, data={"prompt": "test"}))[0], 401)
        for path, method in (("/v1/models/auto", "POST"), ("/v1/images/generations", "GET"), ("/v1/images/generations", "OPTIONS"),
                ("/v1/images/generations/", "POST"), ("/v1/images/%67enerations", "POST"), ("/v1/images/generations?download=1", "POST")):
            self.assertEqual((await exchange(self.app, path, method, data={"prompt": "test"}))[0], 404)
        self.assertFalse(self.factory.calls)

    async def test_generation_prompt_model_stream_and_body_are_validated_before_forwarding(self):
        invalid = ({}, {"prompt": ""}, {"prompt": " \n "}, {"prompt": 1}, {"prompt": ["text"]}, {"prompt": "x" * 32001},
            {"prompt": "test", "model": None}, {"prompt": "test", "model": True}, {"prompt": "test", "model": " "},
            {"prompt": "test", "model": "a" * 101}, {"prompt": "test", "stream": "false"}, {"prompt": "test", "stream": True})
        for payload in invalid:
            self.assertEqual((await exchange(self.app, "/v1/images/generations", data=payload))[0], 400)
        self.assertEqual((await exchange(self.app, "/v1/images/generations", raw=b'[]'))[0], 400)
        self.assertEqual((await exchange(self.app, "/v1/images/generations", raw=b" " * (MAX_BODY_BYTES + 1)))[0], 413)
        self.assertFalse(self.factory.calls)
        self.assertFalse(self.factory.clients)
        self.assertEqual(self.app.state.active_requests, 0)
        self.assertEqual((await exchange(self.app, "/v1/images/generations", data={"prompt": "x" * 32000}))[0], 200)

    async def test_generations_cannot_download_input_attachments_and_private_validates_other_parameters(self):
        for name in ("image", "images", "image_url", "input_image", "mask", "file", "files", "file_url", "url"):
            self.assertEqual((await exchange(self.app, "/v1/images/generations", data={"prompt": "test", name: "http://169.254.169.254/private"}))[0], 400)
        self.assertEqual((await exchange(self.app, "/v1/images/generations", data={"prompt": "test", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://localhost"}}]}]}))[0], 400)
        self.assertFalse(self.factory.calls)
        response = Response(); response.status_code = 422
        self.factory.response = response
        payload = {"prompt": "Reference URL is text: https://example.invalid/", "quality": "invalid", "unknown_option": {"test": True}}
        status, _, _ = await exchange(self.app, "/v1/images/generations", data=payload)
        self.assertEqual(status, 422)
        self.assertEqual(self.factory.calls[0][1]["json"], {**payload, "stream": False})
        self.assertTrue(self.factory.clients[0].closed)

    async def test_image_response_has_independent_size_limit_and_timeout(self):
        with patch("api.public_gateway.MAX_RESPONSE_BYTES", 1):
            self.assertEqual((await exchange(self.app, "/v1/images/generations", data={"prompt": "test"}))[0], 200)
        with patch("api.public_gateway.MAX_IMAGE_RESPONSE_BYTES", 4):
            status, _, body = await exchange(self.app, "/v1/images/generations", data={"prompt": "test"})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["code"], "upstream_response_too_large")

        class Slow(Response):
            async def aiter_content(self):
                await asyncio.sleep(1)
                yield b'{}'

        self.factory.response = Slow()
        with patch("api.public_gateway.IMAGE_UPSTREAM_TIMEOUT_SECONDS", 0.001):
            status, _, body = await exchange(self.app, "/v1/images/generations", data={"prompt": "test"})
        self.assertEqual(status, 504)
        self.assertEqual(json.loads(body)["error"]["code"], "upstream_timeout")
        self.assertTrue(all(client.closed for client in self.factory.clients))
        self.assertEqual(self.app.state.active_requests, 0)

    async def test_images_and_chat_share_ten_request_capacity(self):
        self.factory.wait = asyncio.Event()
        tasks = [asyncio.create_task(exchange(self.app, "/v1/images/generations", data={"prompt": "test"})) for _ in range(5)]
        tasks += [asyncio.create_task(exchange(self.app)) for _ in range(5)]
        try:
            for _ in range(100):
                if len(self.factory.calls) == 10:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(len(self.factory.calls), 10)
            self.assertEqual((await exchange(self.app, "/v1/images/generations", data={"prompt": "test"}))[0], 429)
            self.assertEqual((await exchange(self.app, "/v1/models", "GET"))[0], 429)
            self.assertEqual((await exchange(self.app))[0], 429)
        finally:
            self.factory.wait.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(client.closed for client in self.factory.clients))
        self.assertEqual(self.app.state.active_requests, 0)

    async def test_new_endpoints_release_capacity_when_cancelled(self):
        for path, method, payload in (("/v1/models", "GET", b''), ("/v1/images/generations", "POST", b'{"prompt":"test"}')):
            factory = Factory(); factory.wait = asyncio.Event()
            app = create_app(GatewaySettings(keys=(KEY,)), factory, self.keys)
            endpoint = next(route.endpoint for route in app.routes if route.path == path)

            async def receive():
                return {"type": "http.request", "body": payload, "more_body": False}

            request = Request(scope(path, method), receive); request.state.api_key = KEY
            task = asyncio.create_task(endpoint(request))
            for _ in range(100):
                if factory.calls:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(app.state.active_requests, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(factory.clients[0].closed)
            self.assertEqual(app.state.active_requests, 0)


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

    async def test_real_asgi_image_disconnect_cancels_header_and_body_waits(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                factory = Factory()
                entered = factory.started
                if phase == "headers":
                    factory.wait = asyncio.Event()
                else:
                    entered = asyncio.Event()

                    class PendingBody(Response):
                        async def aiter_content(self):
                            entered.set()
                            await asyncio.Future()
                            yield b"unreachable"

                    factory.response = PendingBody()
                app = create_app(GatewaySettings(keys=(KEY,)), factory, self.keys)
                incoming = asyncio.Queue()
                body = json.dumps({"prompt": "Offline image request", "stream": False}).encode()
                await incoming.put({"type": "http.request", "body": body, "more_body": False})
                output = []

                async def send(message):
                    output.append(message)

                task = asyncio.create_task(app(scope("/v1/images/generations", headers=[
                    (b"authorization", ("Bearer " + KEY).encode())]), incoming.get, send))
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(app.state.active_requests, 1)
                # Simulate the ASGI server reporting a closed socket, rather
                # than directly cancelling the Python request task.
                await incoming.put({"type": "http.disconnect"})
                await asyncio.wait_for(task, 1)
                self.assertEqual(app.state.active_requests, 0)
                self.assertEqual(len(factory.calls), 1)
                self.assertEqual(factory.clients[0].close_calls, 1)
                self.assertEqual(next(row["status"] for row in output if row["type"] == "http.response.start"), 499)
                factory.wait = None
                factory.response = Response()
                self.assertEqual((await exchange(app, "/v1/images/generations", data={"prompt": "Next request"}))[0], 200)

    async def test_image_body_finishes_before_disconnect_watcher_starts(self):
        self.factory.wait = asyncio.Event()
        incoming = asyncio.Queue()
        body = b'{"prompt":"A complete offline request","stream":false}'
        await incoming.put({"type": "http.request", "body": body[:14], "more_body": True})
        output = []

        async def send(message):
            output.append(message)

        task = asyncio.create_task(self.app(scope("/v1/images/generations", headers=[
            (b"authorization", ("Bearer " + KEY).encode())]), incoming.get, send))
        await asyncio.sleep(0)
        self.assertFalse(self.factory.calls)
        await incoming.put({"type": "http.request", "body": body[14:], "more_body": False})
        await asyncio.wait_for(self.factory.started.wait(), 1)
        self.assertEqual(self.factory.calls[0][1]["json"]["prompt"], "A complete offline request")
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.app.state.active_requests, 0)
        self.assertEqual(self.factory.clients[0].close_calls, 1)

    async def test_cancellation_during_stream_handoff_does_not_leak_capacity(self):
        self.factory.response.headers = {"content-type": "text/event-stream"}
        app = create_app(GatewaySettings(keys=(KEY,), streaming=True), self.factory, self.keys)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/v1/chat/completions")
        cleaning, release = asyncio.Event(), asyncio.Event()
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b'{"stream":true}', "more_body": False}
            try:
                await asyncio.Future()
            finally:
                with CancelScope(shield=True):
                    cleaning.set()
                    await release.wait()

        request = Request(scope("/v1/chat/completions"), receive)
        request.state.api_key = KEY
        task = asyncio.create_task(endpoint(request))
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(app.state.active_requests, 0)
        self.assertEqual(self.factory.clients[0].close_calls, 1)

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
