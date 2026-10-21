"""Driving route from a free routing API: exactly one HTTP call per new route.

* OpenRouteService (free API key) is used when ORS_API_KEY is set.
* Otherwise the public OSRM server (no key) is used.

Results are cached, so repeating a request costs no extra API calls.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from .errors import NoRouteFound, UpstreamError
from .geo import METERS_PER_MILE, decode_polyline
from .http import request_json

logger = logging.getLogger(__name__)


@dataclass
class Route:
    coords: list[tuple[float, float]]  # (lat, lon)
    distance_miles: float
    duration_seconds: float
    provider: str
    encoded: str


def provider_name() -> str:
    return "openrouteservice" if settings.ORS_API_KEY else "osrm"


def get_route(start: tuple[float, float], finish: tuple[float, float]) -> tuple[Route, int]:
    """Return (route, external_calls_made)."""
    provider = provider_name()
    key = "route:v1:{}:{:.5f},{:.5f}:{:.5f},{:.5f}".format(provider, *start, *finish)
    cached = cache.get(key)
    if cached:
        return _build(cached, provider), 0

    fetch = _fetch_ors if provider == "openrouteservice" else _fetch_osrm
    data = fetch(start, finish)
    try:
        route = _build(data, provider)
    except ValueError as exc:
        raise UpstreamError(f"{provider} returned an unusable route geometry.", service=provider) from exc
    cache.set(key, data)
    return route, 1


def _build(data: dict, provider: str) -> Route:
    coords = decode_polyline(data["geometry"])
    if len(coords) < 2:
        raise ValueError("route geometry has fewer than two points")
    return Route(
        coords=coords,
        distance_miles=data["distance_m"] / METERS_PER_MILE,
        duration_seconds=data["duration_s"],
        provider=provider,
        encoded=data["geometry"],
    )


def _fetch_osrm(start, finish) -> dict:
    url = "{}/route/v1/driving/{:.6f},{:.6f};{:.6f},{:.6f}".format(
        settings.OSRM_BASE_URL, start[1], start[0], finish[1], finish[0]
    )
    status, payload = request_json(
        "GET", url, service="OSRM routing",
        params={"overview": "full", "geometries": "polyline", "steps": "false", "alternatives": "false"},
    )
    code = payload.get("code") if isinstance(payload, dict) else None
    if code in {"NoRoute", "NoSegment"}:
        raise NoRouteFound("No drivable route was found between these locations.", upstream_code=code)
    if status != 200 or code != "Ok" or not payload.get("routes"):
        raise UpstreamError("OSRM routing returned an error.", service="OSRM routing",
                            upstream_status=status, upstream_code=code, upstream_message=payload.get("message"))
    route = payload["routes"][0]
    return {"geometry": route["geometry"], "distance_m": route["distance"], "duration_s": route["duration"]}


def _fetch_ors(start, finish) -> dict:
    url = f"{settings.ORS_BASE_URL}/v2/directions/driving-car"
    status, payload = request_json(
        "POST", url, service="OpenRouteService",
        headers={"Authorization": settings.ORS_API_KEY, "Content-Type": "application/json"},
        json={"coordinates": [[start[1], start[0]], [finish[1], finish[0]]], "instructions": False},
    )
    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else str(error)
        # 2009: route not found, 2010: point not routable, 2004: limits exceeded
        if code in (2009, 2010, 2004):
            raise NoRouteFound(f"No drivable route was found: {message}", upstream_code=code)
        raise UpstreamError("OpenRouteService returned an error.", service="OpenRouteService",
                            upstream_status=status, upstream_code=code, upstream_message=message)
    if status != 200 or not payload.get("routes"):
        raise UpstreamError("OpenRouteService returned an unexpected response.", service="OpenRouteService",
                            upstream_status=status)
    route = payload["routes"][0]
    summary = route.get("summary", {})
    return {"geometry": route["geometry"], "distance_m": summary.get("distance", 0.0),
            "duration_s": summary.get("duration", 0.0)}
