"""Offline routing integration: real scheduler + real lifecycle with fake upstream."""

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from chatgpt.account_scheduler import AccountScheduler
from chatgpt.account_usage import UsageTrace
from chatgpt.scheduling import error_signal, failure, request_feature, run_request
from test_account_usage import PipelineTests, event

A = 'acct_' + 'a' * 24
B = 'acct_' + 'b' * 24
C = 'acct_' + 'c' * 24


def account(identifier, reason=5, images=2):
    return {'id': identifier, 'credential_status': 'valid', 'status': 'fresh',
            'checked_at': '2026-01-01T00:00:00+00:00',
            'refresh': {'status': 'refreshed', 'last_attempt_at': '2025-12-31T23:59:59+00:00'},
            'features': {name: {'status': 'known', 'remaining': remaining, 'reset_at': None, 'blocked': False}
                         for name, remaining in [('reason', reason), ('image_gen', images)]}}


class FakeQuota:
    def __init__(self):
        self.rows = [account(A), account(B), account(C)]
    def get_accounts(self):
        return {'accounts': self.rows}
    def _groups(self):
        return {row['id']: {'token': 'synthetic-' + row['id']} for row in self.rows}


class ClassificationTests(unittest.TestCase):
    def test_explicit_image_routing_and_narrow_automatic_intent(self):
        self.assertEqual(request_feature({'quota_feature': 'image_gen'}), 'image_gen')
        self.assertEqual(request_feature({'messages': [{'role': 'user', 'content': '请生成一张图片'}]}), 'image_gen')
        self.assertEqual(request_feature({'messages': [{'role': 'user', 'content': 'Draw a red square.'}]}), 'image_gen')
        self.assertEqual(request_feature({'messages': [{'role': 'user', 'content': '解释这张图片'}]}), 'reason')
        self.assertEqual(request_feature({'messages': [{'role': 'user', 'content': 'draw a cat'}, {'role': 'user', 'content': 'Explain it.'}]}), 'reason')
        self.assertEqual(request_feature({'quota_feature': 'reason', 'model': 'image-model'}), 'reason')
        self.assertEqual(request_feature({'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'ignored'}}]}]}), 'reason')
        with self.assertRaises(HTTPException):
            request_feature({'quota_feature': 'invalid'})

    def test_machine_signals_only_do_not_persist_error_text(self):
        quota = error_signal({'error': {'code': 'image_generation_limit_reached', 'message': 'private', 'clears_in': 120}}, 429, now=1000)
        self.assertEqual(quota, {'kind': 'quota', 'feature': 'image_gen', 'reset_at': 1120})
        self.assertEqual(error_signal({'message': 'quota exhausted, private'}, 429)['kind'], 'rate_limit')
        self.assertEqual(error_signal({'code': 'rate_limit_exceeded'}, 429)['kind'], 'rate_limit')
        self.assertEqual(error_signal({'clears_in': 45}, 429)['kind'], 'rate_limit')
        self.assertEqual(error_signal({'error': {'code': 'quota_exceeded'}}, feature='image_gen')['feature'], 'image_gen')
        self.assertEqual(error_signal('expired token secret', 401), {'kind': 'auth'})
        self.assertEqual(error_signal({'feature': {}, 'code': []}, 403)['kind'], 'transient')

    def test_ambiguous_generation_never_replays(self):
        trace = UsageTrace(None, None)
        service = types.SimpleNamespace(usage_trace=trace, conversation_dispatched=True, conversation_accepted=True, conversation_rejected=False)
        self.assertFalse(failure(service, HTTPException(502, 'connection interrupted')).account_retry_safe)
        trace.observe_event(event('partial', status='in_progress', end_turn=False))
        trace.scheduling_error = {'kind': 'quota', 'feature': 'reason'}
        self.assertFalse(failure(service, HTTPException(502, 'quota')).account_retry_safe)
        fresh = types.SimpleNamespace(usage_trace=UsageTrace(None, None), conversation_dispatched=True, conversation_accepted=False, conversation_rejected=True)
        self.assertTrue(failure(fresh, HTTPException(429, 'rate limit')).account_retry_safe)
        self.assertFalse(failure(fresh, HTTPException(403, 'policy')).account_retry_safe)

    def test_code_tool_output_prevents_nonstream_quota_replay(self):
        trace = UsageTrace(None, None)
        trace.observe_event({'message': {'author': {'role': 'assistant'}, 'status': 'in_progress',
                                        'content': {'content_type': 'code', 'text': 'print(1)'}, 'recipient': 'python'}})
        with self.assertRaises(HTTPException):
            trace.observe_event({'error': {'code': 'quota_exceeded'}})
        service = types.SimpleNamespace(usage_trace=trace, conversation_dispatched=True, conversation_accepted=True)
        self.assertFalse(failure(service, HTTPException(502, 'quota')).account_retry_safe)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.quota = FakeQuota()
        self.scheduler = AccountScheduler(self.quota, state_path=None)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('chatgpt.scheduling.pool_request', return_value=True))
        self.stack.enter_context(patch('chatgpt.account_scheduler.get_scheduler', return_value=self.scheduler))
        self.refresh_calls = 0
        async def no_network_refresh(**kwargs):
            self.refresh_calls += 1
        self.stack.enter_context(patch('chatgpt.scheduling.refresh_due', side_effect=no_network_refresh))

    async def test_failover_uses_distinct_accounts_and_preserves_success(self):
        selected = []
        async def process(data, token, lease=None):
            selected.append(lease.account_id)
            if len(selected) == 1:
                await self.scheduler.finish(lease, status='failed', dispatched=True, error={'kind': 'quota', 'feature': 'image_gen'})
                error = HTTPException(429, 'private error')
                error.account_retry_safe = True
                raise error
            await self.scheduler.finish(lease, status='succeeded', dispatched=True, image_requested=True)
            return 'service', 'result'
        result = await run_request(process, {'quota_feature': 'image_gen'}, 'local-key')
        self.assertEqual(result, ('service', 'result'))
        self.assertEqual(len(set(selected)), 2)
        public = self.scheduler.snapshot()
        self.assertEqual(public['accounts'][selected[0]]['image_gen']['state'], 'exhausted')
        self.assertTrue(public['accounts'][selected[0]]['reason']['available'])
        self.assertTrue(any(row['type'] == 'switched' for row in public['events']))
        self.assertEqual(sum(row['active_requests'] for row in public['accounts'].values()), 0)

    async def test_nonretryable_failure_only_calls_once_and_sanitizes(self):
        calls = []
        async def process(data, token, lease=None):
            calls.append(lease.account_id)
            await self.scheduler.finish(lease, status='failed', dispatched=True)
            raise HTTPException(502, 'private upstream error')
        with self.assertRaises(HTTPException) as raised:
            await run_request(process, {}, 'local-key')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('private', str(raised.exception.detail))

    async def test_no_available_refreshes_once_then_returns_retry_after(self):
        self.quota.rows = [account(A, reason=0)]
        async def must_not_call(*args, **kwargs):
            self.fail('generation must not start')
        with self.assertRaises(HTTPException) as raised:
            await run_request(must_not_call, {}, 'local-key')
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(self.refresh_calls, 1)
        self.assertIn('Retry-After', raised.exception.headers)

    async def test_direct_credentials_bypass_scheduler_and_keep_binding_fields(self):
        calls = []
        async def process(data, token):
            calls.append((data, token))
            return 'direct', 'result'
        with patch('chatgpt.scheduling.pool_request', return_value=False):
            body = {'conversation_id': 'existing', 'Chatgpt-Account-Id': 'workspace'}
            self.assertEqual(await run_request(process, body, 'direct-token'), ('direct', 'result'))
        self.assertEqual(calls, [(body, 'direct-token')])
        self.assertFalse(self.scheduler.snapshot()['events'])

    async def test_pool_binding_fields_reject_before_acquisition(self):
        async def must_not_call(*args, **kwargs):
            self.fail('generation must not start')
        for key in ('conversation_id', 'parent_message_id', 'Chatgpt-Account-Id'):
            with self.assertRaises(HTTPException) as raised:
                await run_request(must_not_call, {key: 'bound'}, 'local-key')
            self.assertEqual(raised.exception.status_code, 400)
        self.assertFalse(self.scheduler.snapshot()['events'])


class LeaseLifecycleTests(PipelineTests):
    # Reuse the already isolated real format/pipeline fixture, but run only
    # the lease-specific regressions here (parent tests are collected once).
    async def asyncSetUp(self):
        await super().asyncSetUp()
        original = self.ns['ChatService']
        class SelectedService(original):
            def __init__(self, token, selected_token=None):
                super().__init__(selected_token or token)
                self.conversation_dispatched = False
                self.conversation_accepted = False
                self.conversation_rejected = False
            async def send_conversation(self):
                self.conversation_dispatched = True
                self.conversation_accepted = True
                return await super().send_conversation()
        self.ns['ChatService'] = SelectedService
        self.scheduler = AccountScheduler(FakeQuota(), state_path=None)
        patcher = patch('chatgpt.account_scheduler.get_scheduler', return_value=self.scheduler)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_scheduler_releases_on_success_cancel_setup_and_late_error(self):
        scenarios = [({'events': [event(), '[DONE]']}, 'success'),
                     ({'events': [event('partial', status='in_progress', end_turn=False), {'error': {'code': 'quota_exceeded'}}, '[DONE]']}, 'error'),
                     ({'events': [event('partial', status='in_progress', end_turn=False)]}, 'cancel'),
                     ({'setup_error': True}, 'setup')]
        for data, action in scenarios:
            with self.subTest(action=action):
                lease = await self.scheduler.acquire()
                if action == 'setup':
                    with self.assertRaises(HTTPException):
                        await self.ns['process'](data, 'shared', lease=lease)
                else:
                    service, result = await self.ns['process'](data, 'shared', lease=lease)
                    if action == 'cancel':
                        await anext(result)
                        await result.aclose()
                    else:
                        async for chunk in result:
                            if action == 'success' and chunk.strip() == 'data: [DONE]':
                                await result.aclose()
                                break
                    # Background cleanup is idempotent after the stream ends.
                    await self.ns['finish_attempt'](service, 'interrupted', 'response_not_consumed')
                self.assertEqual(sum(row['active_requests'] for row in self.scheduler.snapshot()['accounts'].values()), 0)

    async def test_cancelled_finalizer_can_be_retried_without_leaking_lease(self):
        lease = await self.scheduler.acquire()
        service, response = await self.ns['process']({'events': [event(), '[DONE]']}, 'shared', lease=lease)
        await self.scheduler._lock.acquire()
        try:
            cleanup = asyncio.create_task(self.ns['finish_attempt'](service, 'interrupted', 'response_not_consumed'))
            await asyncio.sleep(0)
            cleanup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cleanup
        finally:
            self.scheduler._lock.release()
        await self.ns['finish_attempt'](service, 'interrupted', 'response_not_consumed')
        self.assertTrue(service.closed)
        self.assertEqual(sum(row['active_requests'] for row in self.scheduler.snapshot()['accounts'].values()), 0)
        await response.aclose()

# Inherited regression methods are exercised by test_account_usage itself.
for _name in tuple(vars(PipelineTests)):
    if _name.startswith('test_'):
        setattr(LeaseLifecycleTests, _name, None)
# Prevent unittest from collecting the imported base class a second time.
del PipelineTests
