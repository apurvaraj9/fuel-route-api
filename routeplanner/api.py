"""Consistent JSON errors for every API failure: {"error": {"code", "message", "details"}}."""

import logging

from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .services.errors import PlannerError

logger = logging.getLogger(__name__)


def _first_message(detail) -> str:
    if isinstance(detail, dict):
        field, value = next(iter(detail.items()))
        prefix = "" if field == "non_field_errors" else f"'{field}': "
        return prefix + _first_message(value)
    if isinstance(detail, list) and detail:
        return _first_message(detail[0])
    return str(detail)


def exception_handler(exc, context):
    if isinstance(exc, PlannerError):
        response = Response({"error": exc.as_dict()}, status=exc.status)
        retry_after = exc.details.get("retry_after")
        if retry_after:
            response["Retry-After"] = retry_after
        return response

    response = drf_exception_handler(exc, context)
    if response is None:
        logger.exception("Unexpected error", exc_info=exc)
        return Response(
            {"error": {"code": "internal_error", "message": "Unexpected server error."}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, exceptions.ValidationError):
        body = {"code": "invalid_request", "message": _first_message(exc.detail), "details": exc.detail}
    elif isinstance(exc, exceptions.Throttled):
        body = {
            "code": "rate_limited",
            "message": f"Too many requests. Try again in {exc.wait or 1:.0f} seconds.",
            "details": {"retry_after_seconds": exc.wait},
        }
    elif isinstance(exc, exceptions.ParseError):
        body = {"code": "invalid_request", "message": "Request body must be valid JSON."}
    else:
        body = {"code": getattr(exc, "default_code", "error"), "message": _first_message(exc.detail)}
    response.data = {"error": body}
    return response
