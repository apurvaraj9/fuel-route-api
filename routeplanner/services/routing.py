"""Driving routes from a free routing API: exactly one HTTP call per new trip.

* OpenRouteService (free API key) is used when ORS_API_KEY is set.
* Otherwise the public OSRM server (no key) is used.

The single call returns up to ROUTE_ALTERNATIVES route options, each with its
full geometry and the highway references along it (from the step list), so
the planner can compare routes and place stations on the highways they name.
Results are cached, so repeating a request costs no extra API calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache

from .errors import NoRouteFound, UpstreamError
from .geo import METERS_PER_MILE, decode_polyline, haversine_miles
from .highways import parse_route_refs
from .http import request_json

logger = logging.getLogger(__name__)

# OpenRouteService only computes alternative routes for trips shorter than 100 km.
ORS_ALTERNATIVES_MAX_KM = 100


@dataclass
class Route:
    coords: list[tuple[float, float]]  # (lat, lon)
    distance_miles: float
    duration_seconds: float
    provider: str
    segments: list[tuple[float, frozenset[str]]] = field(default_factory=list)  # (miles, highway refs)


def provider_name() -> str:
    return "openrouteservice" if settings.ORS_API_KEY else "osrm"


def get_routes(
    start: tuple[float, float], finish: tuple[float, float], alternatives: bool = True
) -> tuple[list[Route], int]:
    """Return (routes, external_calls_made). routes[0] is the provider's recommended route."""
    provider = provider_name()
    count = max(1, settings.ROUTE_ALTERNATIVES) if alternatives else 1
    key = "route:v2:{}:{}:{:.5f},{:.5f}:{:.5f},{:.5f}".format(provider, count, *start, *finish)
    cached = cache.get(key)
    if cached:
        return [_build(item, provider) for item in cached], 0

    fetch = _fetch_ors if provider == "openrouteservice" else _fetch_osrm
    data = fetch(start, finish, count)
    try:
        routes = [_build(item, provider) for item in data]
    except (ValueError, KeyError, TypeError) as exc:
        raise UpstreamError(f"{provider} returned an unusable route geometry.", service=provider) from exc
    cache.set(key, data)
    return routes, 1


def _build(data: dict, provider: str) -> Route:
    coords = decode_polyline(data["geometry"])
    if len(coords) < 2:
        raise ValueError("route geometry has fewer than two points")
    return Route(
        coords=coords,
        distance_miles=data["distance_m"] / METERS_PER_MILE,
        duration_seconds=data["duration_s"],
        provider=provider,
        segments=[(meters / METERS_PER_MILE, parse_route_refs(ref)) for meters, ref in data.get("segments", [])],
    )


def _merge_segments(pieces: list[tuple[float, str]]) -> list[list]:
    """Join consecutive steps on the same road to keep the cached data small."""
    merged: list[list] = []
    for meters, ref in pieces:
        ref = ref or ""
        if merged and merged[-1][1] == ref:
            merged[-1][0] += meters
        else:
            merged.append([meters, ref])
    return merged


def _fetch_osrm(start, finish, count: int) -> list[dict]:
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{start[1]:.6f},{start[0]:.6f};{finish[1]:.6f},{finish[0]:.6f}"
    status, payload = request_json(
        "GET",
        url,
        service="OSRM routing",
        params={
            "overview": "full",
            "geometries": "polyline",
            "steps": "true",
            "alternatives": str(count) if count > 1 else "false",
        },
    )
    code = payload.get("code") if isinstance(payload, dict) else None
    if code in {"NoRoute", "NoSegment"}:
        raise NoRouteFound("No drivable route was found between these locations.", upstream_code=code)
    if status != 200 or code != "Ok" or not payload.get("routes"):
        raise UpstreamError(
            "OSRM routing returned an error.",
            service="OSRM routing",
            upstream_status=status,
            upstream_code=code,
            upstream_message=payload.get("message"),
        )
    result = []
    for route in payload["routes"][:count]:
        pieces = [
            (step.get("distance", 0.0), step.get("ref") or "")
            for leg in route.get("legs", [])
            for step in leg.get("steps", [])
        ]
        result.append(
            {
                "geometry": route["geometry"],
                "distance_m": route["distance"],
                "duration_s": route["duration"],
                "segments": _merge_segments(pieces),
            }
        )
    return result


def _fetch_ors(start, finish, count: int) -> list[dict]:
    url = f"{settings.ORS_BASE_URL}/v2/directions/driving-car"
    body = {"coordinates": [[start[1], start[0]], [finish[1], finish[0]]], "instructions": True}
    if count > 1 and haversine_miles(*start, *finish) * 1.609 < ORS_ALTERNATIVES_MAX_KM * 0.7:
        body["alternative_routes"] = {"target_count": count, "share_factor": 0.6, "weight_factor": 1.4}
    status, payload = request_json(
        "POST",
        url,
        service="OpenRouteService",
        headers={"Authorization": settings.ORS_API_KEY, "Content-Type": "application/json"},
        json=body,
    )
    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else str(error)
        # 2009: route not found, 2010: point not routable, 2004: limits exceeded
        if code in (2009, 2010, 2004):
            raise NoRouteFound(f"No drivable route was found: {message}", upstream_code=code)
        raise UpstreamError(
            "OpenRouteService returned an error.",
            service="OpenRouteService",
            upstream_status=status,
            upstream_code=code,
            upstream_message=message,
        )
    if status != 200 or not payload.get("routes"):
        raise UpstreamError(
            "OpenRouteService returned an unexpected response.", service="OpenRouteService", upstream_status=status
        )
    result = []
    for route in payload["routes"][:count]:
        summary = route.get("summary", {})
        pieces = [
            (step.get("distance", 0.0), step.get("name") or "")
            for segment in route.get("segments", [])
            for step in segment.get("steps", [])
        ]
        result.append(
            {
                "geometry": route["geometry"],
                "distance_m": summary.get("distance", 0.0),
                "duration_s": summary.get("duration", 0.0),
                "segments": _merge_segments(pieces),
            }
        )
    return result
