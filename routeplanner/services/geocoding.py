"""Turn the user's start/finish text into coordinates inside the USA.

To keep calls to free external APIs to a minimum:

1. "lat,lon" coordinates are used as-is (0 API calls).
2. "City, ST" / "City, State" is resolved from the bundled US gazetteer
   (0 API calls).
3. Anything else (a street address, a landmark, a ZIP code) is sent to a free
   geocoder restricted to the USA: OpenRouteService if ORS_API_KEY is set,
   otherwise Nominatim (OpenStreetMap). 1 call, cached afterwards.

So a typical request costs exactly 1 external call (the route).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from django.conf import settings
from django.core.cache import cache

from .data import get_places, normalize_state
from .errors import InvalidRequest, LocationNotFound, LocationOutsideUSA, UpstreamError
from .http import request_json

MAX_QUERY_LENGTH = 200
# A coordinate counts as "in the USA" when it is this close to a US place.
MAX_MILES_FROM_US_PLACE = 50.0

_COORDS = re.compile(r"^\s*([-+]?\d{1,3}(?:\.\d+)?)\s*,\s*([-+]?\d{1,3}(?:\.\d+)?)\s*$")
_COUNTRY_SUFFIX = re.compile(r",?\s*(USA|U\.S\.A\.|US|U\.S\.|UNITED STATES( OF AMERICA)?)\s*$", re.IGNORECASE)
NON_US_REGIONS = {
    "CANADA", "MEXICO", "ONTARIO", "QUEBEC", "BRITISH COLUMBIA", "ALBERTA", "MANITOBA", "SASKATCHEWAN",
    "NOVA SCOTIA", "NEW BRUNSWICK", "NEWFOUNDLAND AND LABRADOR", "PRINCE EDWARD ISLAND", "YUKON",
    "NORTHWEST TERRITORIES", "NUNAVUT",
}
_STATE_ZIP = re.compile(r"^(?P<state>[A-Za-z .]+?)\s+\d{5}(-\d{4})?$")


@dataclass
class ResolvedLocation:
    query: str
    label: str
    lat: float
    lon: float
    source: str

    @property
    def point(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["lat"] = round(self.lat, 6)
        data["lon"] = round(self.lon, 6)
        return data


def resolve_location(raw, field: str) -> tuple[ResolvedLocation, int]:
    """Return (location, external_calls_made)."""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidRequest(f"'{field}' is required, e.g. \"Dallas, TX\" or \"32.7767,-96.7970\".", field=field)
    query = " ".join(raw.split())
    if len(query) > MAX_QUERY_LENGTH:
        raise InvalidRequest(f"'{field}' is too long (max {MAX_QUERY_LENGTH} characters).", field=field)

    match = _COORDS.match(query)
    if match:
        return _from_coordinates(query, float(match.group(1)), float(match.group(2)), field), 0

    local = _from_gazetteer(query, field)
    if local:
        return local, 0

    return _from_external_geocoder(query, field)


def _from_coordinates(query: str, lat: float, lon: float, field: str) -> ResolvedLocation:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise InvalidRequest(f"'{field}' coordinates are out of range; use \"latitude,longitude\".", field=field)
    nearest = get_places().nearest(lat, lon)
    if nearest is None or nearest[1] > MAX_MILES_FROM_US_PLACE:
        raise LocationOutsideUSA(f"'{field}' ({query}) is not within the USA.", field=field)
    place, miles = nearest
    return ResolvedLocation(query, f"{lat:.5f}, {lon:.5f} (near {place.label})", lat, lon, "coordinates")


def _reject_non_us_region(parts: list[str], field: str) -> None:
    """'Toronto, ON' or 'Monterrey, Mexico' must not be fuzzy-matched to a US street by the geocoder."""
    last = parts[-1].upper().replace(".", "").strip() if len(parts) >= 2 else ""
    if last in NON_US_REGIONS or (len(last) == 2 and last.isalpha() and normalize_state(last) is None):
        raise LocationOutsideUSA(f"'{field}' ({', '.join(parts)}) is not within the USA.", field=field)


def _from_gazetteer(query: str, field: str = "location") -> ResolvedLocation | None:
    text = _COUNTRY_SUFFIX.sub("", query).strip(" ,")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if parts:
        _reject_non_us_region(parts, field)
    city = state = None
    if len(parts) == 2:
        city, state_text = parts
        zip_match = _STATE_ZIP.match(state_text)
        state = normalize_state(zip_match.group("state") if zip_match else state_text)
    elif len(parts) == 1:
        tokens = parts[0].split()
        if len(tokens) >= 2 and len(tokens[-1]) == 2:
            city, state = " ".join(tokens[:-1]), normalize_state(tokens[-1])
    if not city or not state:
        return None
    place = get_places().lookup(city, state)
    if place is None:
        return None
    return ResolvedLocation(query, place.label, place.lat, place.lon, "local_gazetteer")


def _from_external_geocoder(query: str, field: str) -> tuple[ResolvedLocation, int]:
    use_ors = bool(settings.ORS_API_KEY)
    source = "openrouteservice_geocoder" if use_ors else "nominatim"
    key = "geocode:v1:{}:{}".format(source, hashlib.sha256(query.lower().encode()).hexdigest())
    cached = cache.get(key)
    calls = 0
    if cached is None:
        cached = _geocode_ors(query) if use_ors else _geocode_nominatim(query)
        calls = 1
        cache.set(key, cached or {"not_found": True})
    if not cached or cached.get("not_found"):
        raise LocationNotFound(f"Could not find '{query}' in the USA. Try \"City, ST\" or \"latitude,longitude\".",
                               field=field)
    return ResolvedLocation(query, cached["label"], cached["lat"], cached["lon"], source), calls


def _geocode_nominatim(query: str) -> dict | None:
    status, payload = request_json(
        "GET", f"{settings.NOMINATIM_BASE_URL}/search", service="Nominatim geocoder",
        params={"q": query, "format": "jsonv2", "countrycodes": "us", "limit": 1},
    )
    if status != 200 or not isinstance(payload, list):
        raise UpstreamError("Nominatim geocoder returned an error.", service="Nominatim geocoder",
                            upstream_status=status)
    if not payload:
        return None
    hit = payload[0]
    return {"label": hit.get("display_name", query), "lat": float(hit["lat"]), "lon": float(hit["lon"])}


def _geocode_ors(query: str) -> dict | None:
    status, payload = request_json(
        "GET", f"{settings.ORS_BASE_URL}/geocode/search", service="OpenRouteService geocoder",
        headers={"Authorization": settings.ORS_API_KEY},
        params={"text": query, "boundary.country": "US", "size": 1},
    )
    if status != 200 or not isinstance(payload, dict):
        raise UpstreamError("OpenRouteService geocoder returned an error.", service="OpenRouteService geocoder",
                            upstream_status=status)
    features = payload.get("features") or []
    if not features:
        return None
    feature = features[0]
    if (feature.get("properties", {}).get("country_a") or "USA").upper() not in ("USA", "US"):
        return None
    lon, lat = feature["geometry"]["coordinates"][:2]
    return {"label": feature.get("properties", {}).get("label", query), "lat": float(lat), "lon": float(lon)}
