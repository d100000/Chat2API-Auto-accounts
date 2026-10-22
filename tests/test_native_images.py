"""Offline native image protocol and lifecycle checks; no real account imports."""
import ast
import asyncio
import base64
from contextlib import ExitStack
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from PIL import Image

from chatgpt import native_images as native
from chatgpt.account_usage import UsageTrace

ROOT = Path(__file__).resolve().parents[1]


def png():
    target = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(target, format='PNG')
    return base64.b64encode(target.getvalue()).decode()


PNG = png()
CLAIMS = base64.urlsafe_b64encode(json.dumps({'https://api.openai.com/auth': {'chatgpt_account_id': 'synthetic-workspace'}}).encode()).decode().rstrip('=')
HEADER = base64.urlsafe_b64encode(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode()).decode().rstrip('=')
TOKEN = '.'.join((HEADER, CLAIMS, 'synthetic'))
DATA = {'model': 'gpt-image-2', 'prompt': 'A blue circle.', 'n': 1, 'output_format': 'png',
        'size': '1024x1024', 'quality': 'low', 'response_format': 'b64_json', 'quota_feature': 'image_gen'}


def direct(**extra):
    return {'created': 123, 'model': 'gpt-image-2', 'data': [{'b64_json': PNG}], **extra}


def sse(events):
    return ''.join('data: ' + json.dumps(event) + '\r\n\r\n' for event in events).encode()


class Response:
    def __init__(self, payload=None, status=200, body=None, headers=None):
        self.status_code = status
        self.headers = headers or {'content-type': 'application/json'}
        self.body = body if body is not None else json.dumps(payload or direct()).encode()
        self.closed = 0
        self.started = asyncio.Event()
        self.wait = None

    async def aiter_content(self):
        self.started.set()
        if self.wait is not None:
            await self.wait.wait()
        split = len(self.body) // 2
        yield self.body[:split]
        yield self.body[split:]

    async def aclose(self):
        self.closed += 1


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.closed = 0
        self.started = asyncio.Event()
        self.wait = None
        self.error = None

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.started.set()
        if self.wait:
            await self.wait.wait()
        if self.error:
            raise self.error
        return self.responses.pop(0)

    async def close(self):
        self.closed += 1


class NativeImagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.response = Response()
        self.session = Session([self.response])
        self.trace = UsageTrace(None, 'synthetic-attempt')
        self.trace.finish = AsyncMock(wraps=self.trace.finish)
        self.scheduler = types.SimpleNamespace(finish=AsyncMock())
        self.lease = types.SimpleNamespace(token=TOKEN, feature='image_gen')
        self.native_lease = types.SimpleNamespace(token=TOKEN, feature='image_gen', quota_features=(), channel='codex_native')
        self.scheduler.acquire_for_account = AsyncMock(return_value=self.native_lease)
        self.finish_calls = []
        # Execute the real shared cleanup function without importing app.py or .env.
        tree = ast.parse((ROOT / 'api/chat2api.py').read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'finish_attempt')
        namespace = {'asyncio': asyncio}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'api/chat2api.py', 'exec'), namespace)
        async def finish(service, status, code):
            self.finish_calls.append((service, status, code))
            await namespace['finish_attempt'](service, status, code)
        self.start = self.stack.enter_context(patch.object(native, '_start_trace', AsyncMock(return_value=self.trace)))
        self.stack.enter_context(patch.object(native, '_finish_attempt', finish))
        self.stack.enter_context(patch.object(native, '_verify_token', AsyncMock(return_value=TOKEN)))
        self.stack.enter_context(patch.object(native, '_select_token', side_effect=lambda value: value))
        self.stack.enter_context(patch.object(native, '_fingerprint', return_value={'impersonate': 'test'}))
        self.stack.enter_context(patch.object(native, '_runtime', return_value=types.SimpleNamespace(chatgpt_base_url_list=['https://upstream.example'], auth_key=None)))
        self.stack.enter_context(patch.object(native, '_new_session', return_value=self.session))
        self.stack.enter_context(patch('chatgpt.account_scheduler.get_scheduler', return_value=self.scheduler))
        self.stack.enter_context(patch.dict('os.environ', {'CODEX_IMAGE_REASONING_MODEL': 'gpt-5.6-luna', 'CODEX_CLIENT_VERSION': '0.146.0',
                                                        'CODEX_NATIVE_IMAGES_ENABLED': 'true'}))

    async def call(self, data=None):
        return await native.process_native_images(DATA if data is None else data, 'local-shared-key', self.lease)

    def finished(self, status='succeeded', traced=True):
        self.assertEqual(len(self.finish_calls), 1)
        self.assertEqual(self.finish_calls[0][1], status)
        self.assertEqual(self.scheduler.finish.await_count, 1)
        self.assertEqual(self.trace.finish.await_count, int(traced))
        self.assertEqual(self.session.closed, int(traced))

    async def test_native_success_validates_png_tracks_final_evidence_and_only_forwards_allowlist(self):
        service, result = await self.call({**DATA, 'user': 'synthetic-user', 'safety_identifier': 'client-safety', 'attacker_url': 'https://evil.example/'})
        self.assertEqual(result['data'][0]['b64_json'], PNG)
        self.assertEqual(result['size'], '2x2')
        self.assertEqual(result['model'], 'gpt-image-2')
        self.assertTrue(self.trace.complete)
        self.assertEqual(len(self.trace.evidence.generated_assets), 1)
        self.assertEqual(self.trace.evidence.sent_model, 'gpt-image-2')
        self.assertEqual(self.trace.evidence.upstream_model, 'gpt-image-2')
        url, options = self.session.calls[0]
        self.assertEqual(url, 'https://upstream.example/backend-api/codex/images/generations')
        self.assertEqual(options['headers']['ChatGPT-Account-ID'], 'synthetic-workspace')
        self.assertFalse(options['allow_redirects'])
        self.assertTrue(options['stream'])
        self.assertNotIn('quota_feature', options['json'])
        self.assertNotIn('attacker_url', options['json'])
        self.assertNotIn('user', options['json'])
        self.assertEqual(self.response.closed, 1)
        self.finished()
        await service.finish_once('failed', 'duplicate')
        self.finished()

    async def test_native_images_default_disabled_without_any_upstream_or_trace(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(native.native_images_enabled())
            with self.assertRaises(HTTPException) as raised:
                await native.process_native_images(DATA, 'local-shared-key')
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail['error']['code'], 'unsupported_credentials')
        self.assertTrue(raised.exception.safe_api_error)
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.start.await_count, 0)
        self.assertEqual(self.scheduler.finish.await_count, 0)
        self.assertEqual(len(self.finish_calls), 1)

    async def test_enabled_native_without_web_lease_never_debits_web_scheduler(self):
        service, result = await native.process_native_images(DATA, TOKEN)
        self.assertIs(service.schedule_lease, self.native_lease)
        options = self.scheduler.acquire_for_account.call_args.kwargs
        self.assertEqual(options['quota_mode'], 'none')
        self.assertEqual(options['channel'], 'codex_native')
        self.assertEqual(service.schedule_lease.quota_features, ())
        self.assertEqual(result['data'][0]['b64_json'], PNG)
        self.assertEqual(self.scheduler.finish.await_count, 1)
        self.assertEqual(self.trace.finish.await_count, 1)
        self.assertEqual(self.session.closed, 1)
        self.assertEqual(len(self.finish_calls), 1)

    async def test_native_capacity_full_stops_before_upstream_generation(self):
        from chatgpt.account_scheduler import AccountConcurrencyExhausted
        self.scheduler.acquire_for_account.side_effect = AccountConcurrencyExhausted('image_gen')
        with self.assertRaises(HTTPException) as raised:
            await native.process_native_images(DATA, TOKEN)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.detail['error']['code'], 'account_concurrency_exhausted')
        self.assertIn('Retry-After', raised.exception.headers)
        self.assertFalse(self.session.calls)

    def test_unknown_native_enable_setting_fails_closed(self):
        for value in ('', 'false', '0', 'not-configured'):
            with self.subTest(value=value), patch.dict('os.environ', {'CODEX_NATIVE_IMAGES_ENABLED': value}):
                self.assertFalse(native.native_images_enabled())

    async def test_403_does_not_fallback_retry_leak_error_or_cool_web_account(self):
        self.response.status_code = 403
        self.response.body = b'{"error":"secret token and internal server details"}'
        with self.assertRaises(HTTPException) as raised:
            await self.call()
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail['error']['code'], 'unsupported_credentials')
        self.assertTrue(raised.exception.safe_api_error)
        self.assertFalse(raised.exception.account_retry_safe)
        self.assertNotIn('secret', str(raised.exception.detail))
        self.assertEqual(len(self.session.calls), 1)
        finish_args = self.scheduler.finish.await_args.kwargs
        self.assertFalse(finish_args['dispatched'])
        self.assertEqual(finish_args['error'], {'kind': 'request'})
        self.finished('failed')

    async def test_only_missing_endpoint_falls_back_same_account_with_explicit_image_tool(self):
        self.response.status_code = 404
        final = {'type': 'image_generation_call', 'id': 'synthetic-image', 'status': 'completed', 'result': PNG, 'model': 'gpt-image-2'}
        fallback = Response(body=sse([{'type': 'response.output_item.done', 'item': final},
            {'type': 'response.completed', 'response': {'status': 'completed', 'created_at': 456, 'output': [],
             'tool_usage': {'image_gen': {'input_tokens': 3, 'output_tokens': 4, 'secret': TOKEN}}}}]))
        self.session.responses.append(fallback)
        service, result = await self.call()
        self.assertEqual(len(self.session.calls), 2)
        url, options = self.session.calls[1]
        self.assertTrue(url.endswith('/backend-api/codex/responses'))
        self.assertEqual(options['headers']['Authorization'], self.session.calls[0][1]['headers']['Authorization'])
        body = options['json']
        self.assertEqual(body['model'], 'gpt-5.6-luna')
        self.assertEqual(body['tools'][0]['model'], 'gpt-image-2')
        self.assertEqual(body['tools'][0]['type'], 'image_generation')
        self.assertEqual(body['input'][0]['content'][0]['text'], DATA['prompt'])
        self.assertTrue(body['stream'])
        self.assertFalse(body['store'])
        self.assertNotIn('quota_feature', body)
        self.assertEqual(result['usage'], {'input_tokens': 3, 'output_tokens': 4})
        self.assertEqual((self.response.closed, fallback.closed), (1, 1))
        self.finished()

    async def test_partial_preview_without_completed_is_not_success(self):
        self.response.status_code = 405
        self.session.responses.append(Response(body=sse([{'type': 'response.image_generation_call.partial_image', 'partial_image_b64': PNG}])))
        with self.assertRaises(HTTPException) as raised:
            await self.call()
        self.assertEqual(raised.exception.detail['error']['code'], 'incomplete_image_response')
        self.assertFalse(self.trace.complete)
        self.assertFalse(self.trace.evidence.generated_assets)
        self.finished('failed')

    async def test_invalid_signature_wrong_model_or_remote_url_never_succeed(self):
        # Each isolated child service has its own trace/lease lifecycle.
        for payload in [direct(data=[{'b64_json': base64.b64encode(b'<html>not a PNG</html>').decode()}]),
                        direct(model='gpt-image-1'), direct(data=[{'url': 'http://127.0.0.1/private'}]),
                        direct(data=[{'b64_json': PNG}, {'b64_json': PNG}])]:
            with self.subTest(payload=list(payload)):
                with self.assertRaises(HTTPException):
                    native._normalise_result(payload)
        self.assertEqual(self.session.calls, [])

    async def test_response_size_is_bounded_and_never_replayed(self):
        with patch.object(native, 'MAX_RESPONSE_BYTES', 50):
            with self.assertRaises(HTTPException) as raised:
                await self.call()
        self.assertEqual(raised.exception.detail['error']['code'], 'image_response_too_large')
        self.assertEqual(len(self.session.calls), 1)
        self.assertTrue(self.scheduler.finish.await_args.kwargs['dispatched'])
        self.finished('failed')

    async def test_size_rejection_stops_curl_download_before_awaiting_aclose(self):
        # Match curl_cffi's actual aclose semantics: it only waits for the
        # streaming task, which otherwise keeps queuing bytes until timeout.
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def download():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.response.quit_now = asyncio.Event()
        self.response.astream_task = asyncio.create_task(download())
        await entered.wait()
        self.response.headers['content-length'] = str(native.MAX_RESPONSE_BYTES + 1)
        async def close_like_curl():
            self.response.closed += 1
            await self.response.astream_task
        self.response.aclose = close_like_curl
        with self.assertRaises(HTTPException) as raised:
            await asyncio.wait_for(self.call(), timeout=1)
        self.assertEqual(raised.exception.detail['error']['code'], 'image_response_too_large')
        self.assertTrue(self.response.quit_now.is_set())
        self.assertTrue(self.response.astream_task.cancelled())
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.response.closed, 1)
        self.finished('failed')

    async def test_cancel_after_acceptance_releases_trace_lease_and_client_once(self):
        self.response.wait = asyncio.Event()
        task = asyncio.create_task(self.call())
        await self.response.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.response.closed, 1)
        self.assertTrue(self.scheduler.finish.await_args.kwargs['dispatched'])
        self.finished('cancelled')

    async def test_timeout_remains_ambiguous_and_no_auto_retry(self):
        self.response.wait = asyncio.Event()
        with patch.object(native, 'REQUEST_TIMEOUT_SECONDS', .01):
            with self.assertRaises(HTTPException) as raised:
                await self.call()
        self.assertEqual(raised.exception.status_code, 504)
        self.assertFalse(raised.exception.account_retry_safe)
        self.assertEqual(len(self.session.calls), 1)
        self.finished('failed')

    async def test_connection_failure_is_sanitized_and_not_retried(self):
        self.session.error = RuntimeError('private bearer ' + TOKEN)
        with self.assertRaises(HTTPException) as raised:
            await self.call()
        self.assertNotIn(TOKEN, str(raised.exception.detail))
        self.assertFalse(raised.exception.account_retry_safe)
        self.finished('failed')

    async def test_invalid_input_releases_acquired_lease_before_opening_client(self):
        with self.assertRaises(HTTPException):
            await self.call({**DATA, 'response_format': 'url'})
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.start.await_count, 0)
        self.finished('failed', traced=False)

    async def test_repeated_cancellation_waits_for_unique_cleanup_task(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = native._finish_attempt
        async def delayed_finish(service, status, code):
            entered.set()
            await release.wait()
            await original(service, status, code)
        with patch.object(native, '_finish_attempt', delayed_finish):
            task = asyncio.create_task(self.call())
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.finished()

    async def test_upstream_failure_event_after_preview_never_counts_as_generated(self):
        self.response.status_code = 404
        self.session.responses.append(Response(body=sse([
            {'type': 'response.image_generation_call.partial_image', 'partial_image_b64': PNG},
            {'type': 'response.failed', 'response': {'error': {'message': TOKEN}}}])))
        with self.assertRaises(HTTPException) as raised:
            await self.call()
        self.assertNotIn(TOKEN, str(raised.exception.detail))
        self.assertFalse(self.trace.evidence.generated_assets)
        self.finished('failed')

    async def test_explicit_account_selection_and_fingerprint_override_jwt_claim(self):
        self.lease.token = TOKEN + ',embedded-workspace'
        with patch.object(native, '_fingerprint', return_value={'chatgpt-account-id': 'fp-workspace'}):
            await self.call({**DATA, 'Chatgpt-Account-Id': 'selected-workspace'})
        self.assertEqual(self.session.calls[0][1]['headers']['ChatGPT-Account-ID'], 'selected-workspace')
        self.finished()

    async def test_429_is_not_replayed_or_treated_as_web_auth_failure(self):
        self.response.status_code = 429
        with self.assertRaises(HTTPException) as raised:
            await self.call()
        self.assertEqual(raised.exception.status_code, 429)
        self.assertFalse(raised.exception.account_retry_safe)
        self.assertEqual(self.scheduler.finish.await_args.kwargs['error'], {'kind': 'request'})
        self.assertEqual(len(self.session.calls), 1)
        self.finished('failed')


if __name__ == '__main__':
    unittest.main()
