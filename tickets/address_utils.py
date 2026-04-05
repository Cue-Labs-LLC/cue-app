"""Address normalization and geocoding utilities for Venue records."""

import logging
import re

import requests

logger = logging.getLogger(__name__)


def _title_case_with_ordinals(s):
    """Title-case a string but keep ordinal suffixes (1st, 2nd, 3rd, 4th, 11th, etc.) lowercase.

    Python's str.title() turns "11th" into "11Th"; this fixes that and similar ordinals.
    """
    s = s.strip().title()
    # Fix ordinal suffixes: 11Th -> 11th, 5Th -> 5th, 1St -> 1st, 2Nd -> 2nd, 3Rd -> 3rd
    s = re.sub(r'(\d+)(St|Nd|Rd|Th)\b', lambda m: m.group(1) + m.group(2).lower(), s)
    return s

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
        venue.street_address = _title_case_with_ordinals(venue.street_address)
    if venue.city:
        venue.city = _title_case_with_ordinals(venue.city)
    if venue.state:
        s = venue.state.strip()
        venue.state = s.upper() if len(s) <= 2 else _title_case_with_ordinals(s)
    if venue.postal_code:
        venue.postal_code = venue.postal_code.strip().upper()
    if venue.country:
        c = venue.country.strip()
        venue.country = c.upper() if len(c) <= 3 else _title_case_with_ordinals(c)


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
