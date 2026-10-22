"""Images compatibility checks without account credentials or network calls."""

import asyncio
import ast
import base64
from io import BytesIO
from types import SimpleNamespace
from pathlib import Path
import re
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from PIL import Image

from chatgpt.image_api import (validate_image_request, web_image_request, format_web_images,
                              trusted_image_url, verify_image_bytes)


class ImageValidationTests(unittest.TestCase):
    def test_native_options_and_web_route_are_distinct(self):
        native = validate_image_request({"model": "gpt-image-2", "prompt": "A red circle", "size": "1536x1024", "quality": "high"})
        self.assertEqual(native["model"], "gpt-image-2")
        self.assertEqual(native["response_format"], "b64_json")
        web = validate_image_request({"model": "chatgpt-image-latest", "prompt": "A blue circle"})
        converted = web_image_request(web)
        self.assertEqual(converted["model"], "chatgpt-image-latest")
        self.assertEqual(converted["quota_feature"], "image_gen")
        self.assertFalse(converted["history_disabled"])
        self.assertEqual(validate_image_request({"prompt": "A circle"})["model"], "gpt-image-2")

    def test_rejects_unsupported_work_before_generation(self):
        for change in ({"n": 2}, {"n": True}, {"stream": True}, {"prompt": " "}, {"model": "gpt-6"},
                       {"size": "1025x1024"}, {"size": "3840x3840"}, {"size": "512x512"},
                       {"quality": "max"}, {"image_url": "http://127.0.0.1"},
                       {"response_format": "url"}, {"output_format": "jpeg", "background": "transparent"}):
            with self.subTest(change=change), self.assertRaises(HTTPException):
                validate_image_request({"model": "gpt-image-2", "prompt": "A circle", **change})
        for change in ({"size": "1024x1024"}, {"quality": "high"}, {"background": "opaque"}, {"output_format": "webp"}):
            with self.subTest(change=change), self.assertRaises(HTTPException):
                validate_image_request({"model": "chatgpt-image-latest", "prompt": "A circle", **change})

    def test_only_approved_signed_download_hosts(self):
        self.assertTrue(trusted_image_url("https://files.oaiusercontent.com/file?sig=example"))
        self.assertTrue(trusted_image_url("https://chatgpt.com/backend-api/estuary/content?id=file-test&sig=example"))
        for url in ("http://files.oaiusercontent.com/a", "https://files.oaiusercontent.com.evil.test/a",
                    "https://127.0.0.1/a", "https://chatgpt.com/backend-api/me", "https://files.oaiusercontent.com:8080/a", "https://u:p@files.oaiusercontent.com/a"):
            self.assertFalse(trusted_image_url(url))
        with self.assertRaises(HTTPException):
            verify_image_bytes(b"<html>not an image</html>")


class WebImageBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_image_is_returned_and_download_client_is_closed(self):
        out = BytesIO()
        Image.new("RGB", (16, 16), "red").save(out, format="PNG")
        raw = out.getvalue()
        class Response:
            status_code = 200
            async def aiter_content(self):
                yield raw[:10]
                yield raw[10:]
        class Client:
            closed = False
            async def get(self, url, **kwargs):
                self.url, self.options = url, kwargs
                return Response()
            async def close(self):
                self.closed = True
        client = Client()
        service = SimpleNamespace(usage_trace=SimpleNamespace(evidence=SimpleNamespace(generated_assets={"asset-1"})),
            generated_image_urls={"asset-1": "https://files.oaiusercontent.com/file?sig=x"})
        result = {"choices": [{"message": {"content": "![image](https://files.oaiusercontent.com/file?sig=x)"}}]}
        data = validate_image_request({"model": "chatgpt-image-latest", "prompt": "A red circle"})
        response = await format_web_images(service, result, data, client_factory=lambda **kwargs: client)
        self.assertEqual(base64.b64decode(response["data"][0]["b64_json"]), raw)
        self.assertTrue(client.closed)
        self.assertFalse(client.options["allow_redirects"])
        self.assertEqual(client.options["headers"], {})
        self.assertEqual(response["chat2api"]["model_identity"], "not_exposed_by_upstream")


class ModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_validation_uses_full_discovery_beyond_public_defaults(self):
        source = Path(__file__).resolve().parents[1] / "api/chat2api.py"
        tree = ast.parse(source.read_text())
        method = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "validate_chat_model")
        namespace = {"re": re, "HTTPException": HTTPException, "IMAGE_MODELS": {"gpt-image-2", "chatgpt-image-latest"}}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        full = AsyncMock(return_value={"data": [{"id": "auto", "type": "chat"}, {"id": "gpt-6", "type": "chat"}]})
        with patch("chatgpt.model_catalog.get_catalog", full):
            self.assertEqual((await namespace["validate_chat_model"]({}))["model"], "gpt-6-luna")
            full.assert_not_called()
            for model in ("gpt-6-luna", "auto", "gpt-6", "gpt-4", "g-custom"):
                self.assertEqual((await namespace["validate_chat_model"]({"model": model}))["model"], model)
            with self.assertRaises(HTTPException) as context:
                await namespace["validate_chat_model"]({"model": "gpt-nonexistent"})
            self.assertEqual(context.exception.status_code, 404)
            with self.assertRaises(HTTPException) as context:
                await namespace["validate_chat_model"]({"model": "gpt-image-2"})
            self.assertEqual(context.exception.status_code, 400)

    async def test_real_slugs_and_image_bridge_never_fall_back_to_gpt4o(self):
        source = Path(__file__).resolve().parents[1] / "chatgpt/ChatService.py"
        tree = ast.parse(source.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ChatService")
        method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "set_model")
        namespace = {"re": re, "HTTPException": HTTPException, "model_proxy": {}}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), "exec"), namespace)
        for model, expected in ((None, "gpt-6-luna"), ("auto", "auto"), ("gpt-6-luna", "gpt-6-luna"),
                                ("gpt-6", "gpt-6"), ("gpt-5-6", "gpt-5-6"), ("o4-mini", "o4-mini"),
                                ("gpt-5-thinking-mini", "gpt-5-thinking-mini"), ("gpt-4.1-mini", "gpt-4.1-mini"), ("o3-pro", "o3-pro"),
                                ("chatgpt-image-latest", "auto"), ("g-my-custom-gpt", "auto")):
            service = SimpleNamespace(data={"model": model} if model is not None else {})
            await namespace["set_model"](service)
            self.assertEqual(service.req_model, expected)
        for model in ("gpt-image-2", "unknown-model", "claude-3-opus"):
            with self.subTest(model=model), self.assertRaises(HTTPException):
                await namespace["set_model"](SimpleNamespace(data={"model": model}))
    async def test_text_refusal_or_untrusted_link_never_counts_as_image_success(self):
        for assets, text in ((set(), "I will draw an image"), ({"asset-1"}, "![image](https://127.0.0.1/private)")):
            with self.subTest(text=text), self.assertRaises(HTTPException):
                await format_web_images(SimpleNamespace(usage_trace=SimpleNamespace(evidence=SimpleNamespace(generated_assets=assets))),
                    {"choices": [{"message": {"content": text}}]}, {"response_format": "b64_json"})

    async def test_cancellation_closes_download_client_without_retry(self):
        entered = asyncio.Event()
        class Response:
            status_code = 200
            async def aiter_content(self):
                entered.set()
                await asyncio.Future()
                yield b""
        class Client:
            closed = False
            async def get(self, *args, **kwargs): return Response()
            async def close(self): self.closed = True
        client = Client()
        task = asyncio.create_task(format_web_images(SimpleNamespace(usage_trace=SimpleNamespace(evidence=SimpleNamespace(generated_assets={"one"})),
            generated_image_urls={"one": "https://files.oaiusercontent.com/file"}),
            {"choices": [{"message": {"content": "![image](https://files.oaiusercontent.com/file)"}}]},
            {"response_format": "b64_json"}, client_factory=lambda **kwargs: client))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(client.closed)

    async def test_markdown_links_cannot_replace_completed_asset_url(self):
        real = "https://chatgpt.com/backend-api/estuary/content?id=real-generated"
        service = SimpleNamespace(usage_trace=SimpleNamespace(evidence=SimpleNamespace(generated_assets={"real"})),
            generated_image_urls={"unrelated": "https://attacker.blob.core.windows.net/wrong", "real": real},
            base_headers={"authorization": "Bearer synthetic-test-only", "unrelated-secret": "must-not-be-forwarded"})
        out = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(out, format="PNG")
        class Response:
            status_code = 200
            async def aiter_content(self): yield out.getvalue()
        class Client:
            async def get(self, url, **kwargs):
                self.url, self.headers = url, kwargs["headers"]
                return Response()
            async def close(self): pass
        client = Client()
        response = await format_web_images(service, {"choices": [{"message": {"content":
            "![image](https://attacker.blob.core.windows.net/wrong)"}}]}, {"response_format": "b64_json"}, client_factory=lambda **kwargs: client)
        self.assertEqual(client.url, real)
        self.assertEqual(client.headers, {"authorization": "Bearer synthetic-test-only"})
        self.assertEqual(base64.b64decode(response["data"][0]["b64_json"]), out.getvalue())


if __name__ == "__main__":
    unittest.main()
