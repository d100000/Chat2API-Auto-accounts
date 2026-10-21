"""Feature inventory checks run without credentials or network access."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException


def load_catalog_module():
    fake_app = ModuleType('app')
    fake_app.app = SimpleNamespace(get=lambda *args, **kwargs: lambda function: function)
    fake_app.templates = SimpleNamespace(TemplateResponse=Mock(return_value='rendered'))
    fake_console = ModuleType('api.local_console')
    fake_console.current_session = Mock(return_value='test-session')
    fake_console.require_session = Mock(return_value='test-session')
    fake_config = ModuleType('utils.configs')
    path = Path(__file__).resolve().parents[1] / 'api/features_console.py'
    spec = importlib.util.spec_from_file_location('_tested_features_console', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'app': fake_app, 'api.local_console': fake_console, 'utils.configs': fake_config}):
        spec.loader.exec_module(module)
    return module


class FeaturesConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_catalog_module()

    def test_explicit_configuration_allowlist_never_contains_secrets(self):
        data = self.module.feature_catalog(SimpleNamespace(authorization_list=['SECRET-KEY'], auth_key='SECRET-AUTH', proxy_url='https://SECRET-PROXY', refresh_token='SECRET-REFRESH', enable_gateway=False))
        self.assertNotIn('SECRET-', json.dumps(data))
        self.assertEqual(len(data['configuration']), 10)
        self.assertTrue(all(type(item['enabled']) is bool for item in data['configuration']))

    def test_gateway_links_follow_current_switch(self):
        closed = self.module.feature_catalog(SimpleNamespace(enable_gateway=False))
        opened = self.module.feature_catalog(SimpleNamespace(enable_gateway=True))
        self.assertFalse(closed['gateway_enabled'])
        self.assertIsNone(closed['inherited'][0]['href'])
        self.assertEqual(opened['inherited'][0]['href'], '/gateway')
        self.assertEqual(opened['inherited'][1]['href'], '/gpts')
        self.assertEqual(opened['inherited'][-1]['state'], 'not_implemented')

    def test_examples_match_actual_protocol_and_context_constraints(self):
        data = self.module.feature_catalog(SimpleNamespace(api_prefix='proxy/test'))
        examples = data['examples']
        self.assertEqual(examples[0]['path'], '/proxy/test/v1/chat/completions')
        self.assertTrue(examples[0]['body']['stream'])
        self.assertEqual(examples[1]['body']['quota_feature'], 'image_gen')
        self.assertIs(examples[1]['body']['history_disabled'], False)
        self.assertEqual(examples[2]['body']['messages'][0]['content'][1]['type'], 'image_url')
        self.assertEqual(examples[3]['auth'], 'ACCOUNT_ACCESS_TOKEN')
        self.assertEqual(examples[4]['path'], '/seedtoken')
        self.assertEqual(examples[5]['encoding'], 'form')
        self.assertNotIn('temperature', examples[0]['body'])
        self.assertTrue(any('未实现' in value for value in data['boundaries']))

    def test_unsafe_prefix_cannot_create_external_management_links(self):
        data = self.module.feature_catalog(SimpleNamespace(api_prefix='https://example.invalid/'))
        self.assertEqual(data['management'][1]['href'], '/tokens')
        self.assertEqual(data['examples'][0]['path'], '/v1/chat/completions')

    def test_api_requires_admin_session_before_reading_catalog(self):
        with patch.object(self.module, 'require_session', side_effect=HTTPException(401)), patch.object(self.module, 'feature_catalog') as catalog:
            with self.assertRaises(HTTPException):
                asyncio.run(self.module.features_info(Mock()))
            catalog.assert_not_called()

    def test_page_redirects_anonymous_users(self):
        with patch.object(self.module, 'current_session', return_value=None):
            response = asyncio.run(self.module.features_page(Mock()))
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers['location'], '/login')


if __name__ == '__main__':
    unittest.main()
