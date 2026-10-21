"""Shared HTTP helper for the external map/routing services.

Every outbound call goes through `request_json`, which applies timeouts and a
User-Agent, and turns network failures into domain errors.
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings

from .errors import UpstreamError, UpstreamRateLimited, UpstreamTimeout

logger = logging.getLogger(__name__)

_session = requests.Session()


def request_json(method: str, url: str, *, service: str, **kwargs):
    """Perform one HTTP call and return (status_code, parsed_json).

    Timeouts, connection errors, 429, 401/403, 5xx and non-JSON bodies raise;
    other statuses (e.g. 400/404 "no route") are returned for the caller.
    """
    headers = {"User-Agent": settings.HTTP_USER_AGENT, "Accept": "application/json"}
    headers.update(kwargs.pop("headers", {}) or {})
    timeout = (settings.HTTP_CONNECT_TIMEOUT_SECONDS, settings.HTTP_READ_TIMEOUT_SECONDS)
    try:
        response = _session.request(method, url, headers=headers, timeout=timeout, **kwargs)
    except requests.Timeout as exc:
        raise UpstreamTimeout(f"{service} did not respond in time. Please try again.", service=service) from exc
    except requests.RequestException as exc:
        logger.warning("%s request failed: %s", service, exc)
        raise UpstreamError(f"Could not reach {service}. Please try again later.", service=service) from exc

    if response.status_code == 429:
        raise UpstreamRateLimited(
            f"{service} rate limit reached. Please retry shortly.",
            service=service,
            retry_after=response.headers.get("Retry-After"),
        )
    if response.status_code in (401, 403):
        raise UpstreamError(f"{service} rejected the request (check the API key).", service=service,
                            upstream_status=response.status_code)
    if response.status_code >= 500:
        raise UpstreamError(f"{service} is currently unavailable (HTTP {response.status_code}).",
                            service=service, upstream_status=response.status_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamError(f"{service} returned an invalid response.", service=service,
                            upstream_status=response.status_code) from exc
    return response.status_code, payload
