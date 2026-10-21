"""Shared test utilities: recorded real routes and fake HTTP responses."""
import json
from functools import lru_cache
from pathlib import Path

import requests

from routeplanner.services.geo import encode_polyline

FIXTURES = Path(__file__).parent / "fixtures"

LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "tests"}}


@lru_cache(maxsize=None)
def recorded_routes() -> dict:
    return json.loads((FIXTURES / "recorded_routes.json").read_text())


def osrm_payload(name: str) -> dict:
    """An OSRM /route response built from a recorded real route."""
    route = recorded_routes()[name]
    return {
        "code": "Ok",
        "routes": [{
            "geometry": encode_polyline([tuple(p) for p in route["points"]]),
            "distance": route["distance_m"],
            "duration": route["duration_s"] or route["distance_m"] / 25.0,
        }],
    }


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._payload


class FakeHTTP:
    """Replaces requests.Session.request; records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"Unexpected HTTP call: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def timeout_error():
    return requests.Timeout("timed out")


def connection_error():
    return requests.ConnectionError("connection refused")
