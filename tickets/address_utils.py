"""Address normalization and geocoding utilities for Venue records."""

import logging

import requests

logger = logging.getLogger(__name__)

_ADDRESS_COMPONENT_TYPES = {
    'street_number': None,
    'route': None,
    'locality': 'city',
    'administrative_area_level_1': 'state',
    'postal_code': 'postal_code',
    'country': 'country',
}


def normalize_venue_address_fields(venue):
    """Apply Python-only normalization to address fields in-place. No I/O."""
    if venue.street_address:
        venue.street_address = venue.street_address.strip().title()
    if venue.city:
        venue.city = venue.city.strip().title()
    if venue.state:
        s = venue.state.strip()
        venue.state = s.upper() if len(s) <= 2 else s.title()
    if venue.postal_code:
        venue.postal_code = venue.postal_code.strip().upper()
    if venue.country:
        venue.country = venue.country.strip().title()


def geocode_venue_address(venue, api_key, timeout=5):
    """Call Google Geocoding API and overwrite address fields with canonical values.

    Only called when venue.street_address is non-empty. Fails silently on errors.
    Updates fields in-place; does not save.
    """
    parts = [
        p for p in [
            venue.street_address,
            venue.city,
            venue.state,
            venue.postal_code,
            venue.country,
        ] if p
    ]
    if not parts:
        return

    address_query = ', '.join(parts)
    url = 'https://maps.googleapis.com/maps/api/geocode/json'
    params = {'address': address_query, 'key': api_key}

    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning('Venue geocoding request failed for "%s": %s', address_query, exc)
        return

    status = data.get('status')
    if status != 'OK':
        logger.warning('Venue geocoding returned status %s for "%s"', status, address_query)
        return

    results = data.get('results')
    if not results:
        return

    components = results[0].get('address_components', [])

    street_number = ''
    route = ''
    parsed = {}

    for component in components:
        types = component.get('types', [])
        if 'street_number' in types:
            street_number = component.get('long_name', '')
        elif 'route' in types:
            route = component.get('long_name', '')
        elif 'locality' in types:
            parsed['city'] = component.get('long_name', '')
        elif 'administrative_area_level_1' in types:
            parsed['state'] = component.get('short_name', '')
        elif 'postal_code' in types:
            parsed['postal_code'] = component.get('long_name', '')
        elif 'country' in types:
            parsed['country'] = component.get('long_name', '')

    if street_number and route:
        parsed['street_address'] = f'{street_number} {route}'
    elif route:
        parsed['street_address'] = route

    for field, value in parsed.items():
        if value:
            setattr(venue, field, value)
