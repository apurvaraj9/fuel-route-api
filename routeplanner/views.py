from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .serializers import ErrorSerializer, RoutePlanSerializer, RouteRequestSerializer
from .services.data import get_stations
from .services.planner import TripOptions, plan_trip

ERROR_RESPONSES = {
    400: OpenApiResponse(ErrorSerializer, description="Invalid input (missing field, bad JSON, out-of-range option)."),
    422: OpenApiResponse(
        ErrorSerializer,
        description="Location not found / outside the USA, no drivable route, or no feasible refuelling plan.",
    ),
    429: OpenApiResponse(ErrorSerializer, description="Rate limit for this client exceeded."),
    502: OpenApiResponse(ErrorSerializer, description="The routing/geocoding service failed."),
    503: OpenApiResponse(ErrorSerializer, description="Routing service rate limit, or fuel data not loaded."),
    504: OpenApiResponse(ErrorSerializer, description="The routing/geocoding service timed out."),
}


class RouteView(APIView):
    """Plan a route between two US locations with the cheapest fuel stops."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "route"

    @extend_schema(
        summary="Plan a route with fuel stops (query parameters)",
        parameters=[RouteRequestSerializer],
        responses={200: RoutePlanSerializer, **ERROR_RESPONSES},
    )
    def get(self, request):
        return self._plan(request, request.query_params)

    @extend_schema(
        summary="Plan a route with fuel stops (JSON body)",
        request=RouteRequestSerializer,
        responses={200: RoutePlanSerializer, **ERROR_RESPONSES},
        examples=[
            OpenApiExample(
                "Chicago to Dallas", value={"start": "Chicago, IL", "finish": "Dallas, TX"}, request_only=True
            ),
            OpenApiExample(
                "Half tank, 50-mile reserve",
                request_only=True,
                value={
                    "start": "New York, NY",
                    "finish": "Los Angeles, CA",
                    "start_fuel_percent": 50,
                    "reserve_miles": 50,
                },
            ),
        ],
    )
    def post(self, request):
        return self._plan(request, request.data)

    def _plan(self, request, data):
        serializer = RouteRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        options = TripOptions(
            start_fuel_percent=values["start_fuel_percent"],
            reserve_miles=values["reserve_miles"],
            route_preference=values["route_preference"],
            alternatives=values["alternatives"],
        )
        result = plan_trip(values["start"], values["finish"], options)
        result["map"]["url"] = request.build_absolute_uri(reverse("planner") + "?" + urlencode(_page_params(values)))
        return Response(result)


def _page_params(values) -> dict:
    params = {"start": values["start"], "finish": values["finish"]}
    if values.get("start_fuel_percent", 100) != 100:
        params["start_fuel_percent"] = f"{values['start_fuel_percent']:g}"
    if values.get("reserve_miles", 0):
        params["reserve_miles"] = f"{values['reserve_miles']:g}"
    if values.get("route_preference", "cheapest") != "cheapest":
        params["route_preference"] = values["route_preference"]
    return params


@extend_schema(
    summary="Service status and fuel data summary",
    responses={
        200: inline_serializer("Health", {"status": serializers.CharField(), "fuel_data": serializers.DictField()}),
        503: OpenApiResponse(ErrorSerializer, description="Fuel data not loaded."),
    },
)
@api_view(["GET"])
@throttle_classes([])
def health_view(request):
    stats = get_stations().stats
    return Response({"status": "ok", "fuel_data": stats})


@extend_schema(exclude=True)
@api_view(["GET"])
def api_index_view(request):
    example = urlencode({"start": "New York, NY", "finish": "Los Angeles, CA"})
    url = request.build_absolute_uri
    return Response(
        {
            "service": "Fuel Route Planner API",
            "endpoints": {
                "plan_route": {
                    "method": "GET or POST",
                    "url": url(reverse("route")),
                    "example": url(reverse("route")) + "?" + example,
                },
                "docs": url(reverse("swagger-ui")),
                "openapi_schema": url(reverse("schema")),
                "web_planner": url(reverse("planner")),
                "health": url(reverse("health")),
                "admin": url(reverse("admin:index")),
            },
        }
    )


def planner_view(request):
    """The interactive planner page (form + map). It calls the JSON API from the browser."""
    return render(request, "routeplanner/planner.html")


def legacy_map_view(request):
    """v1 map URL: forwards to the planner page with the same query string."""
    query = request.META.get("QUERY_STRING", "")
    return HttpResponseRedirect(reverse("planner") + (f"?{query}" if query else ""))
