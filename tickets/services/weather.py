"""Weather forecast service for event detail pages.

Provider waterfall:
  1. NWS (https://api.weather.gov) — US-only, no key, no rate limit, 7-day forecast.
  2. Open-Meteo (https://open-meteo.com) — global, no key, ~14-day horizon.
     Fallback for non-US venues; can hit per-IP rate limits in production.
  3. wttr.in (https://wttr.in) — global, no key, ~3-day horizon, last resort.

Geocodes the venue once and caches; fetches a daily forecast for the event's
start date and caches that too. Fails silently (logs a warning and returns
None) so a weather outage never breaks the event page.
"""

import logging
import re
from datetime import date, timedelta

import requests
from django.core.cache import cache as django_cache


logger = logging.getLogger(__name__)


GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WTTR_URL_TEMPLATE = "https://wttr.in/{lat},{lng}"
NWS_POINTS_URL_TEMPLATE = "https://api.weather.gov/points/{lat},{lng}"

GEOCODE_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days — venue coords don't move
FORECAST_TTL_SECONDS = 60 * 60 * 3       # 3 hours — daily forecast is stable
FORECAST_FAILURE_TTL_SECONDS = 60 * 30   # 30 min — back off when all providers fail
FORECAST_HORIZON_DAYS = 14               # Open-Meteo's reliable daily window
WTTR_HORIZON_DAYS = 2                    # wttr.in returns today + next 2 days
NWS_GRIDPOINT_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days — gridpoints don't move

HTTP_TIMEOUT_SECONDS = 5

# Identifying ourselves keeps Open-Meteo from lumping us in with anonymous
# bot traffic that gets rate-limited first. NWS *requires* a non-empty UA.
USER_AGENT = "cue-events (+https://cueup.co) weather forecast widget"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# Bumped when the on-disk cache shape or provider routing changes so old
# `{}` failure entries from previous deploys don't keep suppressing the chip.
_FORECAST_CACHE_VERSION = 3


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
            headers=DEFAULT_HEADERS,
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
    """Fetch the daily forecast for target_date at (lat, lng), with caching.

    Waterfall: NWS (US only) → Open-Meteo → wttr.in. The first provider that
    returns data wins; the result is cached for FORECAST_TTL. A combined
    failure caches an empty sentinel for FORECAST_FAILURE_TTL so repeat page
    loads don't keep hammering rate-limited APIs.
    """
    date_str = target_date.isoformat()
    cache_key = f"weather:forecast:v{_FORECAST_CACHE_VERSION}:{lat:.3f}:{lng:.3f}:{date_str}"
    cached = django_cache.get(cache_key)
    if cached is not None:
        return cached or None  # falsy sentinel means previous failure

    forecast = _fetch_nws_day(lat, lng, target_date)
    if forecast is None:
        forecast = _fetch_open_meteo_day(lat, lng, target_date)
    if forecast is None and (target_date - date.today()).days <= WTTR_HORIZON_DAYS:
        forecast = _fetch_wttr_in_day(lat, lng, target_date)

    if forecast is None:
        django_cache.set(cache_key, {}, FORECAST_FAILURE_TTL_SECONDS)
        return None

    django_cache.set(cache_key, forecast, FORECAST_TTL_SECONDS)
    return forecast


def _fetch_open_meteo_day(lat, lng, target_date):
    """Call Open-Meteo for one day's forecast. Returns the unified dict or None."""
    date_str = target_date.isoformat()
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
        resp = requests.get(
            FORECAST_URL,
            params=params,
            headers=DEFAULT_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Open-Meteo forecast failed for (%s, %s) on %s: %s — will try wttr.in fallback',
                       lat, lng, date_str, exc)
        return None

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        return None

    try:
        weather_code = int(daily.get("weather_code", [None])[0] or 0)
    except (TypeError, ValueError):
        weather_code = 0

    def _first(name):
        arr = daily.get(name) or [None]
        return arr[0] if arr else None

    return _build_forecast(
        target_date=target_date,
        weather_code=weather_code,
        temp_high=_first("temperature_2m_max"),
        temp_low=_first("temperature_2m_min"),
        precip_prob=_first("precipitation_probability_max") or 0,
        precip_amount=_first("precipitation_sum") or 0,
        wind_max=_first("wind_speed_10m_max") or 0,
        source="open-meteo",
    )


def _fetch_wttr_in_day(lat, lng, target_date):
    """Call wttr.in for one day's forecast. Returns the unified dict or None.

    wttr.in returns today + the next 2 days. We pick the matching date from
    the `weather` array and derive daily max/min from its hourly samples.
    """
    date_str = target_date.isoformat()
    url = WTTR_URL_TEMPLATE.format(lat=f"{lat:.4f}", lng=f"{lng:.4f}")
    try:
        resp = requests.get(
            url,
            params={"format": "j1"},
            headers=DEFAULT_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('wttr.in forecast failed for (%s, %s) on %s: %s',
                       lat, lng, date_str, exc)
        return None

    days = data.get("weather") or []
    day = next((d for d in days if d.get("date") == date_str), None)
    if day is None:
        return None

    try:
        temp_high = float(day.get("maxtempF")) if day.get("maxtempF") is not None else None
        temp_low = float(day.get("minTempF") or day.get("mintempF")) if (day.get("minTempF") or day.get("mintempF")) is not None else None
    except (TypeError, ValueError):
        temp_high = temp_low = None

    hourly = day.get("hourly") or []
    precip_prob = 0
    wind_max = 0
    desc_counts = {}
    for h in hourly:
        try:
            precip_prob = max(precip_prob, int(h.get("chanceofrain") or 0), int(h.get("chanceofsnow") or 0))
            wind_max = max(wind_max, float(h.get("windspeedMiles") or 0))
        except (TypeError, ValueError):
            pass
        desc_list = h.get("weatherDesc") or []
        if desc_list and isinstance(desc_list, list):
            d = (desc_list[0] or {}).get("value", "")
            if d:
                desc_counts[d] = desc_counts.get(d, 0) + 1

    # Pick the most-frequent hourly description as the day's representative.
    representative_desc = max(desc_counts, key=desc_counts.get) if desc_counts else ""
    weather_code = _wmo_code_from_wttr_desc(representative_desc)

    return _build_forecast(
        target_date=target_date,
        weather_code=weather_code,
        temp_high=temp_high,
        temp_low=temp_low,
        precip_prob=precip_prob,
        precip_amount=0,  # wttr.in's hourly precipMM is per-hour; daily sum not surfaced
        wind_max=wind_max,
        source="wttr.in",
    )


def _wmo_code_from_wttr_desc(desc):
    """Map a wttr.in weather description to a WMO code we already understand.

    Lets us reuse `_describe_weather_code` and `_classify_severity` without a
    separate WWO mapping table. Order matters — more specific terms first.
    """
    d = (desc or "").lower()
    if "thunder" in d:
        return 95
    if "torrential" in d or "heavy rain" in d:
        return 65
    if "heavy snow" in d or "blizzard" in d:
        return 75
    if "freezing rain" in d or "ice pellet" in d or "sleet" in d:
        return 67
    if "snow" in d:
        return 71
    if "rain" in d or "drizzle" in d or "shower" in d:
        return 61
    if "fog" in d or "mist" in d or "haze" in d:
        return 45
    if "overcast" in d:
        return 3
    if "cloud" in d:
        return 2
    if "clear" in d or "sunny" in d:
        return 0
    return 0


def _resolve_nws_gridpoint(lat, lng):
    """Return the NWS gridpoint forecast URL for (lat, lng), or None.

    NWS's points endpoint returns 404 for coordinates outside US territory —
    that's how we silently fall through to Open-Meteo for international
    venues. We cache the URL (or a "miss" sentinel) for 30 days because
    gridpoints don't move and the response is large.
    """
    cache_key = f"weather:nws:points:v1:{lat:.3f}:{lng:.3f}"
    cached = django_cache.get(cache_key)
    if cached is not None:
        return None if cached == "miss" else cached

    url = NWS_POINTS_URL_TEMPLATE.format(lat=f"{lat:.4f}", lng=f"{lng:.4f}")
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT_SECONDS)
        if resp.status_code == 404:
            django_cache.set(cache_key, "miss", NWS_GRIDPOINT_TTL_SECONDS)
            return None
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('NWS points lookup failed for (%s, %s): %s', lat, lng, exc)
        return None

    forecast_url = (data.get("properties") or {}).get("forecast")
    if not forecast_url:
        django_cache.set(cache_key, "miss", NWS_GRIDPOINT_TTL_SECONDS)
        return None

    django_cache.set(cache_key, forecast_url, NWS_GRIDPOINT_TTL_SECONDS)
    return forecast_url


def _fetch_nws_day(lat, lng, target_date):
    """Call NWS for one day's forecast. Returns the unified dict or None.

    Returns None for non-US venues (404 from points endpoint), network errors,
    or dates past NWS's ~7-day forecast window.
    """
    forecast_url = _resolve_nws_gridpoint(lat, lng)
    if forecast_url is None:
        return None

    date_str = target_date.isoformat()
    try:
        resp = requests.get(forecast_url, headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('NWS forecast failed for (%s, %s) on %s: %s — will try Open-Meteo fallback',
                       lat, lng, date_str, exc)
        return None

    periods = ((data.get("properties") or {}).get("periods")) or []
    day_period = next(
        (p for p in periods if p.get("isDaytime") and (p.get("startTime") or "")[:10] == date_str),
        None,
    )
    night_period = next(
        (p for p in periods if not p.get("isDaytime") and (p.get("startTime") or "")[:10] == date_str),
        None,
    )
    if day_period is None and night_period is None:
        return None  # target_date is past NWS's horizon

    # Prefer the day period for the "headline" condition; an evening-only forecast
    # (event starts past noon today) falls back to the night period.
    headline = day_period or night_period
    temp_high = (day_period or night_period).get("temperature")
    temp_low = (night_period or day_period).get("temperature")

    precip_values = []
    for p in (day_period, night_period):
        if not p:
            continue
        v = (p.get("probabilityOfPrecipitation") or {}).get("value")
        if v is not None:
            precip_values.append(v)
    precip_prob = max(precip_values) if precip_values else 0

    wind_max = _parse_nws_wind(headline.get("windSpeed") or "")
    weather_code = _wmo_code_from_nws_short_forecast(headline.get("shortForecast") or "")

    return _build_forecast(
        target_date=target_date,
        weather_code=weather_code,
        temp_high=temp_high,
        temp_low=temp_low,
        precip_prob=precip_prob,
        precip_amount=0,  # NWS daily quantitative precip lives on a different endpoint
        wind_max=wind_max,
        source="nws",
    )


_WIND_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _parse_nws_wind(text):
    """Extract the max numeric mph from NWS windSpeed text.

    NWS returns strings like '5 to 10 mph', 'Around 7 mph', '15 mph', or ''.
    Returns 0.0 when no number is present.
    """
    nums = _WIND_NUMBER_RE.findall(text or "")
    return max((float(n) for n in nums), default=0.0)


def _wmo_code_from_nws_short_forecast(desc):
    """Map an NWS shortForecast to a WMO code.

    NWS often chains conditions like 'Patchy Fog then Mostly Sunny' or
    'Sunny then Thunderstorms'. The trailing clause reflects the end-of-day
    condition that matters most for evening Cue events, so we split on
    ' then ' and classify the LAST clause through the existing matcher.
    """
    if not desc:
        return 0
    last_clause = desc.rsplit(" then ", 1)[-1]
    return _wmo_code_from_wttr_desc(last_clause)


def _build_forecast(target_date, weather_code, temp_high, temp_low,
                    precip_prob, precip_amount, wind_max, source):
    """Assemble the unified forecast dict used by the template."""
    label, icon = _describe_weather_code(weather_code)
    return {
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
        "source": source,
    }
