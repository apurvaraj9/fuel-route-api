import json
import logging
from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .services.errors import InvalidRequest, PlannerError
from .services.planner import plan_trip

logger = logging.getLogger(__name__)


def _error_response(error: PlannerError) -> JsonResponse:
    response = JsonResponse({"error": error.as_dict()}, status=error.status)
    retry_after = error.details.get("retry_after")
    if retry_after:
        response["Retry-After"] = retry_after
    return response


def _read_params(request) -> tuple[str, str]:
    if request.method == "POST":
        try:
            body = json.loads(request.body or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise InvalidRequest("Request body must be valid JSON, e.g. {\"start\": \"Dallas, TX\", \"finish\": \"Denver, CO\"}.")
        if not isinstance(body, dict):
            raise InvalidRequest("Request body must be a JSON object with 'start' and 'finish'.")
        return body.get("start"), body.get("finish")
    return request.GET.get("start"), request.GET.get("finish")


def _plan(request):
    start, finish = _read_params(request)
    result = plan_trip(start, finish)
    query = urlencode({"start": start, "finish": finish})
    result["map"]["url"] = request.build_absolute_uri(f"{reverse('route-map')}?{query}")
    return result


@csrf_exempt
@require_http_methods(["GET", "POST"])
def route_view(request):
    """Plan a route with fuel stops. GET ?start=..&finish=..  or  POST {"start": .., "finish": ..}."""
    try:
        return JsonResponse(_plan(request), json_dumps_params={"indent": 2})
    except PlannerError as error:
        return _error_response(error)
    except Exception:  # pragma: no cover - last-resort safety net
        logger.exception("Unexpected error while planning a route")
        return JsonResponse({"error": {"code": "internal_error", "message": "Unexpected server error."}}, status=500)


@require_GET
def route_map_view(request):
    """The same plan rendered as an interactive map (Leaflet + OpenStreetMap tiles)."""
    try:
        result = _plan(request)
    except PlannerError as error:
        return render(request, "routeplanner/map.html", {"error": error.as_dict()}, status=error.status)
    return render(request, "routeplanner/map.html", {"plan": result})


@require_GET
def index_view(request):
    base = request.build_absolute_uri("/").rstrip("/")
    example = urlencode({"start": "New York, NY", "finish": "Los Angeles, CA"})
    return JsonResponse({
        "service": "Fuel route planner API",
        "endpoints": {
            "plan_route": {
                "method": "GET or POST",
                "url": f"{base}{reverse('route')}",
                "params": {"start": "US location: \"City, ST\", \"lat,lon\" or an address",
                           "finish": "US location"},
                "example": f"{base}{reverse('route')}?{example}",
            },
            "route_map": {"method": "GET", "url": f"{base}{reverse('route-map')}?{example}"},
            "health": f"{base}{reverse('health')}",
        },
    }, json_dumps_params={"indent": 2})


@require_GET
def health_view(request):
    from .services.data import get_stations

    try:
        stats = get_stations().stats
    except PlannerError as error:
        return _error_response(error)
    return JsonResponse({"status": "ok", "fuel_data": stats})
