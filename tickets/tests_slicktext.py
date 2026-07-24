from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from .services.slicktext import SlickTextClient, normalize_campaign_report


def _response(status_code=200, payload=None):
    response = MagicMock(status_code=status_code)
    response.json.return_value = payload if payload is not None else {}
    return response


class SlickTextClientTests(SimpleTestCase):
    @patch('tickets.services.slicktext.requests.request')
    def test_get_campaign_links_filters_links_by_campaign_source(self, mock_request):
        mock_request.return_value = _response(payload={'data': []})

        client = SlickTextClient('api-key', 'brand-1')
        client.get_campaign_links('226911')

        mock_request.assert_called_once()
        _, url = mock_request.call_args.args
        self.assertEqual(url, 'https://dev.slicktext.com/v1/brands/brand-1/links')
        self.assertEqual(mock_request.call_args.kwargs['params'], {
            'limit': 250,
            'offset': 0,
            'source': 'Campaign',
            '_source_id': '226911',
        })


class SlickTextNormalizationTests(SimpleTestCase):
    def test_normalize_campaign_report_reads_campaign_analytics_totals(self):
        normalized = normalize_campaign_report({
            'campaign': {
                'campaign_id': 226911,
                'name': 'Day of show',
                'body': 'Doors are open.',
                'audience_size': 7000,
            },
            'analytics': {
                'totals': {
                    'contacts': 7133,
                    'clicks': 44,
                    'unique_clicks': 38,
                    'click_rate': '0.0053',
                    'unsubscribes': 6,
                    'unsubscribe_rate': '0.0008',
                    'orders': 3,
                    'revenue': '125.50',
                },
            },
        })

        self.assertEqual(normalized['external_id'], '226911')
        self.assertEqual(normalized['audience_size'], 7133)
        self.assertEqual(normalized['clicks'], 44)
        self.assertEqual(normalized['unique_clicks'], 38)
        self.assertEqual(normalized['click_rate'], Decimal('0.0053'))
        self.assertEqual(normalized['unsubscribes'], 6)
        self.assertEqual(normalized['unsubscribe_rate'], Decimal('0.0008'))
        self.assertEqual(normalized['orders'], 3)
        self.assertEqual(normalized['revenue'], Decimal('125.50'))

    def test_normalize_campaign_report_uses_links_when_analytics_clicks_empty(self):
        links = [
            {'link_id': 1, 'source': 'Campaign', '_source_id': 226911, 'clicks': 8, 'unique_clicks': 5, 'bot_clicks': 1},
            {'link_id': 2, 'source': 'Campaign', '_source_id': 226911, 'clicks': 4, 'unique_clicks': 3, 'bot_clicks': 0},
        ]

        normalized = normalize_campaign_report({
            'campaign': {'campaign_id': 226911, 'name': 'Linked broadcast'},
            'analytics': {
                'totals': {
                    'contacts': 100,
                    'clicks': 0,
                    'unique_clicks': 0,
                    'unsubscribes': 7,
                },
            },
            'links': links,
        })

        self.assertEqual(normalized['clicks'], 12)
        self.assertEqual(normalized['unique_clicks'], 8)
        self.assertEqual(normalized['unsubscribes'], 7)
        self.assertEqual(normalized['external_metadata']['link_metrics'], {
            'clicks': 12,
            'unique_clicks': 8,
            'bot_clicks': 1,
        })
        self.assertEqual(normalized['external_metadata']['raw_links'], links)
        self.assertEqual(normalized['external_metadata']['raw_analytics']['totals']['unsubscribes'], 7)

    def test_normalize_campaign_report_uses_analytics_campaign_fallback_details(self):
        normalized = normalize_campaign_report({
            'campaign': {},
            'analytics': {
                'totals': {'contacts': 200},
                'campaign': {
                    'campaign_id': 123,
                    'name': 'Fallback campaign',
                    'body': 'Fallback body',
                    'media_url': 'https://example.com/image.jpg',
                    'audience_size': 150,
                },
            },
        })

        self.assertEqual(normalized['external_id'], '123')
        self.assertEqual(normalized['name'], 'Fallback campaign')
        self.assertEqual(normalized['message'], 'Fallback body')
        self.assertEqual(normalized['media_url'], 'https://example.com/image.jpg')
        self.assertEqual(normalized['audience_size'], 200)
