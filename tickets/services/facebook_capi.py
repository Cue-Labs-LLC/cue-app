import hashlib
import logging
import time

import requests

logger = logging.getLogger(__name__)
GRAPH_API_VERSION = 'v21.0'


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def send_capi_event(
    pixel_id, access_token, event_name, *,
    value=None, currency='USD',
    content_ids=None, content_type='product',
    email=None, first_name=None, last_name=None,
    client_ip=None, client_user_agent=None,
    fbp=None, fbc=None,
    event_id=None, event_source_url=None,
):
    """Send a single event to Meta Conversions API. Fails silently (logged)."""
    if not pixel_id or not access_token:
        return

    user_data = {}
    if email:
        user_data['em'] = _hash(email)
    if first_name:
        user_data['fn'] = _hash(first_name)
    if last_name:
        user_data['ln'] = _hash(last_name)
    if client_ip:
        user_data['client_ip_address'] = client_ip
    if client_user_agent:
        user_data['client_user_agent'] = client_user_agent
    if fbp:
        user_data['fbp'] = fbp
    if fbc:
        user_data['fbc'] = fbc

    custom_data = {}
    if value is not None:
        custom_data['value'] = float(value)
        custom_data['currency'] = currency
    if content_ids:
        custom_data['content_ids'] = content_ids
        custom_data['content_type'] = content_type

    payload = {
        'event_name': event_name,
        'event_time': int(time.time()),
        'action_source': 'website',
        'user_data': user_data,
        'custom_data': custom_data,
    }
    if event_id:
        payload['event_id'] = event_id
    if event_source_url:
        payload['event_source_url'] = event_source_url

    url = f'https://graph.facebook.com/{GRAPH_API_VERSION}/{pixel_id}/events'
    try:
        resp = requests.post(
            url,
            json={'data': [payload], 'access_token': access_token},
            timeout=5,
        )
        if not resp.ok:
            logger.warning('Facebook CAPI [%s] error %s: %s', event_name, resp.status_code, resp.text[:300])
    except Exception as exc:
        logger.error('Facebook CAPI [%s] request failed: %s', event_name, exc)
