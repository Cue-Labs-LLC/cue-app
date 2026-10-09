"""Tests for the Langfuse production-tracing helper (tickets/services/ai_tracing.py)."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from tickets.services import ai_tracing


class AITracingTests(TestCase):
    def setUp(self):
        # The helper caches the enabled/disabled decision in a module global; reset it so
        # each test starts fresh regardless of order.
        ai_tracing._enabled = None
        self.addCleanup(lambda: setattr(ai_tracing, '_enabled', None))

    @override_settings(LANGFUSE_PUBLIC_KEY='', LANGFUSE_SECRET_KEY='')
    def test_noop_when_unconfigured(self):
        # No keys -> empty config so the call site's .invoke/.stream is unchanged.
        self.assertEqual(ai_tracing.trace_config(name='x', tags=['t'], session_id='s'), {})

    @override_settings(LANGFUSE_PUBLIC_KEY='pk', LANGFUSE_SECRET_KEY='sk',
                       LANGFUSE_HOST='https://lf.internal')
    def test_returns_config_when_configured(self):
        sentinel = object()
        with patch('langfuse.Langfuse') as fake_client, \
                patch('langfuse.langchain.CallbackHandler', return_value=sentinel):
            cfg = ai_tracing.trace_config(
                name='ig-support-answer', tags=['ig-support-agent', 'answer'],
                session_id='conv-1', user_id=7, metadata={'organization_id': 'org-1'})

        fake_client.assert_called_once()
        config = cfg['config']
        self.assertEqual(config['callbacks'], [sentinel])
        self.assertEqual(config['run_name'], 'ig-support-answer')
        md = config['metadata']
        self.assertEqual(md['langfuse_session_id'], 'conv-1')
        self.assertEqual(md['langfuse_user_id'], '7')
        self.assertEqual(md['langfuse_tags'], ['ig-support-agent', 'answer'])
        self.assertEqual(md['organization_id'], 'org-1')

    @override_settings(LANGFUSE_PUBLIC_KEY='pk', LANGFUSE_SECRET_KEY='sk')
    def test_init_failure_disables_gracefully(self):
        # A bad client init must not raise into the call site — tracing just turns off.
        with patch('langfuse.Langfuse', side_effect=RuntimeError('boom')):
            self.assertEqual(ai_tracing.trace_config(name='x'), {})

    @override_settings(LANGFUSE_PUBLIC_KEY='pk', LANGFUSE_SECRET_KEY='sk')
    def test_client_initialized_once(self):
        with patch('langfuse.Langfuse') as fake_client, \
                patch('langfuse.langchain.CallbackHandler', return_value=object()):
            ai_tracing.trace_config(name='a')
            ai_tracing.trace_config(name='b')
        fake_client.assert_called_once()  # cached across calls
