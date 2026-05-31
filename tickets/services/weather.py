"""Weather forecast service for event detail pages.

Uses Open-Meteo (https://open-meteo.com) — free, no API key required.
Geocodes the venue address once and caches; fetches a daily forecast for the
event's start date and caches that too. Fails silently (logs a warning and
returns None) so a weather outage never breaks the event page.
"""

import logging
from datetime import date, timedelta

import requests
from django.core.cache import cache as django_cache


logger = logging.getLogger(__name__)


GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

GEOCODE_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days — venue coords don't move
FORECAST_TTL_SECONDS = 60 * 60 * 3       # 3 hours — daily forecast is stable
FORECAST_HORIZON_DAYS = 14               # Open-Meteo's reliable daily window

HTTP_TIMEOUT_SECONDS = 5


class WeatherAPIError(Exception):
    """Raised when Open-Meteo calls fail unexpectedly."""


# WMO weather interpretation codes (https://open-meteo.com/en/docs)
# Maps each code to (human label, Bootstrap Icon class).
_WMO_CODE_MAP = {
    0:  ("Clear",                "bi-sun"),
    1:  ("Mostly clear",         "bi-sun"),
    2:  ("Partly cloudy",        "bi-cloud-sun"),
    3:  ("Overcast",             "bi-cloud"),
    45: ("Fog",                  "bi-cloud-fog2"),
    48: ("Freezing fog",         "bi-cloud-fog2"),
    51: ("Light drizzle",        "bi-cloud-drizzle"),
    53: ("Drizzle",              "bi-cloud-drizzle"),
    55: ("Heavy drizzle",        "bi-cloud-drizzle"),
    56: ("Freezing drizzle",     "bi-cloud-sleet"),
    57: ("Freezing drizzle",     "bi-cloud-sleet"),
    61: ("Light rain",           "bi-cloud-rain"),
    63: ("Rain",                 "bi-cloud-rain"),
    65: ("Heavy rain",           "bi-cloud-rain-heavy"),
    66: ("Freezing rain",        "bi-cloud-sleet"),
    67: ("Freezing rain",        "bi-cloud-sleet"),
    71: ("Light snow",           "bi-cloud-snow"),
    73: ("Snow",                 "bi-cloud-snow"),
    75: ("Heavy snow",           "bi-snow"),
    77: ("Snow grains",          "bi-cloud-snow"),
    80: ("Rain showers",         "bi-cloud-rain"),
    81: ("Rain showers",         "bi-cloud-rain-heavy"),
    82: ("Heavy showers",        "bi-cloud-rain-heavy"),
    85: ("Snow showers",         "bi-cloud-snow"),
    86: ("Heavy snow showers",   "bi-snow"),
    95: ("Thunderstorm",         "bi-cloud-lightning-rain"),
    96: ("Thunderstorm w/ hail", "bi-cloud-lightning-rain"),
    99: ("Severe thunderstorm",  "bi-cloud-lightning-rain"),
}


def _describe_weather_code(code):
    """Return (label, icon) for a WMO weather code, with a sensible fallback."""
    return _WMO_CODE_MAP.get(code, ("Mixed conditions", "bi-cloud"))


# Conditions that should escalate the chip to attract organizer attention.
_SEVERE_WMO_CODES = frozenset({65, 67, 75, 82, 86, 95, 96, 99})
_CAUTION_WMO_CODES = frozenset({45, 48, 51, 53, 55, 56, 57, 61, 63, 71, 73, 77, 80, 81, 85})


def _classify_severity(weather_code, precip_prob, temp_high, temp_low, wind_max):
    """Return 'normal' | 'caution' | 'severe' based on the forecast.

    Severe wins over caution. None inputs are treated as benign so partial data
    never trips an alert.
    """
    if weather_code in _SEVERE_WMO_CODES:
        return 'severe'
    if temp_high is not None and temp_high >= 100:
        return 'severe'
    if temp_low is not None and temp_low <= 20:
        return 'severe'

    if weather_code in _CAUTION_WMO_CODES:
        return 'caution'
    if precip_prob is not None and precip_prob >= 50:
        return 'caution'
    if temp_high is not None and temp_high >= 90:
        return 'caution'
    if temp_low is not None and temp_low <= 32:
        return 'caution'
    if wind_max is not None and wind_max >= 25:
        return 'caution'

    return 'normal'


def get_event_weather_forecast(event):
    """Return a forecast dict for the event's start date, or None.

    Returns None (silently) when:
      - the event has no venue or no city,
      - the start date is missing,
      - the event is in the past,
      - the event is more than FORECAST_HORIZON_DAYS out,
      - geocoding fails to find a match,
      - the Open-Meteo API errors.
    """
    venue = getattr(event, 'venue', None)
    if venue is None or not venue.city:
        return None

    event_date = getattr(event, 'start_date', None)
    if event_date is None:
        return None

    today = date.today()
    if event_date < today:
        return None
    if (event_date - today).days > FORECAST_HORIZON_DAYS:
        return None

    coords = _resolve_venue_coords(venue)
    if coords is None:
        return None
    lat, lng = coords

    return _fetch_daily_forecast(lat, lng, event_date)


# US state abbreviation → full name, used to match Open-Meteo's `admin1` field
# when the venue stores a 2-letter code.
_US_STATE_ABBR_TO_NAME = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'DC': 'District of Columbia', 'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii',
    'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island',
    'SC': 'South Carolina', 'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas',
    'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington',
    'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
}


def _state_matches(venue_state, admin1):
    """True if venue.state matches Open-Meteo's admin1 (full state name)."""
    if not venue_state or not admin1:
        return False
    vs = venue_state.strip().upper()
    full = _US_STATE_ABBR_TO_NAME.get(vs, venue_state).strip().lower()
    return full == admin1.strip().lower()


def _country_matches(venue_country, country_code, country):
    """True if venue.country matches Open-Meteo's country / country_code."""
    if not venue_country:
        return False
    vc = venue_country.strip().upper()
    if country_code and vc == country_code.strip().upper():
        return True
    # Common 3-letter or full-name → ISO-2 shorthands we care about.
    aliases = {'USA': 'US', 'U.S.A.': 'US', 'U.S.': 'US',
               'UNITED STATES': 'US', 'UNITED STATES OF AMERICA': 'US',
               'UK': 'GB', 'UNITED KINGDOM': 'GB',
               'CAN': 'CA', 'CANADA': 'CA',
               'MEX': 'MX', 'MEXICO': 'MX'}
    if country_code and aliases.get(vc) == country_code.strip().upper():
        return True
    if country and vc == country.strip().upper():
        return True
    return False


def _resolve_venue_coords(venue):
    """Look up (lat, lng) for a venue, with long-lived caching by venue id.

    Open-Meteo's geocoding API matches a single place name (city), so we query
    with venue.city and disambiguate the result list using venue.state and
    venue.country when available.
    """
    cache_key = f"weather:geocode:{venue.pk}"
    cached = django_cache.get(cache_key)
    if cached is not None:
        if cached == "miss":
            return None
        return cached

    if not venue.city:
        return None

    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"name": venue.city, "count": 10, "language": "en", "format": "json"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Weather geocoding failed for venue %s ("%s"): %s', venue.pk, venue.city, exc)
        return None

    results = data.get("results") or []
    if not results:
        django_cache.set(cache_key, "miss", GEOCODE_TTL_SECONDS)
        return None

    # Narrow by state and country when we have them; fall back to the full list.
    candidates = results
    if venue.state:
        state_matches = [r for r in candidates if _state_matches(venue.state, r.get("admin1"))]
        if state_matches:
            candidates = state_matches
    if venue.country:
        country_matches = [
            r for r in candidates
            if _country_matches(venue.country, r.get("country_code"), r.get("country"))
        ]
        if country_matches:
            candidates = country_matches

    # Prefer the highest-population match (Open-Meteo's natural tiebreaker).
    top = max(candidates, key=lambda r: r.get("population") or 0)

    lat = top.get("latitude")
    lng = top.get("longitude")
    if lat is None or lng is None:
        django_cache.set(cache_key, "miss", GEOCODE_TTL_SECONDS)
        return None

    coords = (float(lat), float(lng))
    django_cache.set(cache_key, coords, GEOCODE_TTL_SECONDS)
    return coords


def _fetch_daily_forecast(lat, lng, target_date):
    """Fetch the daily forecast for target_date at (lat, lng), with caching."""
    date_str = target_date.isoformat()
    cache_key = f"weather:forecast:{lat:.3f}:{lng:.3f}:{date_str}"
    cached = django_cache.get(cache_key)
    if cached is not None:
        return cached or None  # falsy sentinel means previous failure

    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lng:.4f}",
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
            "precipitation_sum",
            "weather_code",
            "wind_speed_10m_max",
        ]),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "start_date": date_str,
        "end_date": date_str,
    }

    try:
        resp = requests.get(FORECAST_URL, params=params, timeout=HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Weather forecast failed for (%s, %s) on %s: %s', lat, lng, date_str, exc)
        # Short-cache the failure so we don't retry on every page reload.
        django_cache.set(cache_key, {}, 60 * 5)
        return None

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        django_cache.set(cache_key, {}, FORECAST_TTL_SECONDS)
        return None

    # Open-Meteo returns parallel arrays keyed by date; index 0 is the only entry.
    try:
        weather_code = int(daily.get("weather_code", [None])[0] or 0)
    except (TypeError, ValueError):
        weather_code = 0
    label, icon = _describe_weather_code(weather_code)

    def _first(name):
        arr = daily.get(name) or [None]
        return arr[0] if arr else None

    temp_high = _first("temperature_2m_max")
    temp_low = _first("temperature_2m_min")
    precip_prob = _first("precipitation_probability_max") or 0
    precip_amount = _first("precipitation_sum") or 0
    wind_max = _first("wind_speed_10m_max") or 0

    forecast = {
        "date": target_date,
        "temp_high": temp_high,
        "temp_low": temp_low,
        "precip_prob": precip_prob,
        "precip_amount": precip_amount,
        "wind_max": wind_max,
        "weather_code": weather_code,
        "condition_label": label,
        "condition_icon": icon,
        "severity": _classify_severity(weather_code, precip_prob, temp_high, temp_low, wind_max),
    }
    django_cache.set(cache_key, forecast, FORECAST_TTL_SECONDS)
    return forecast
