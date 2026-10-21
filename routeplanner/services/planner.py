"""Orchestrates one trip plan: locations -> route -> stations on the route -> fuel stops."""
from __future__ import annotations

import time

from django.conf import settings

from .data import StationIndex, get_stations
from .errors import InvalidRequest
from .geo import RouteLine, haversine_miles, simplify
from .geocoding import resolve_location
from .optimizer import Candidate, FuelPlan, plan_fuel_stops
from .routing import get_route

ASSUMPTIONS = [
    "Vehicle: 500-mile maximum range and 10 miles per gallon (50-gallon tank).",
    "The vehicle starts the trip with a full tank; total cost is the fuel bought along the way.",
    "At each fuel stop the tank is filled; the last stop buys only what is needed to reach the destination.",
    "Fuel prices come from the supplied CSV; when a station is listed several times its lowest price is used. "
    "Non-US (Canadian) rows are excluded.",
    "The CSV has no coordinates, so each station is placed at the centre of its city. A station counts as on "
    "the route if that point is within the corridor distance of the route; the detour there and back is added "
    "to the distance driven and to the 500-mile range check.",
    "Fuel cost = gallons bought x that station's retail price.",
]


def find_candidates(line: RouteLine, stations: StationIndex, corridor_miles: float) -> list[Candidate]:
    """Stations whose location lies within `corridor_miles` of the route line."""
    cell = stations.CELL_DEG
    wanted = set()
    pad = 1  # 0.5-degree cells: one ring of neighbours covers a corridor of up to ~25 miles
    for lat, lon in line.points:
        cy, cx = int(lat // cell), int(lon // cell)
        for dy in range(-pad, pad + 1):
            for dx in range(-pad, pad + 1):
                wanted.add((cy + dy, cx + dx))
    matched: dict[tuple[float, float], object] = {}
    result = []
    for key in wanted:
        for station in stations.grid.get(key, ()):
            loc = (station.lat, station.lon)
            if loc not in matched:
                matched[loc] = line.match(station.lat, station.lon, corridor_miles)
            m = matched[loc]
            if m is not None:
                result.append(Candidate(station, m.route_mile, m.off_route_miles))
    return result


def plan_trip(start_text, finish_text) -> dict:
    started = time.perf_counter()
    stations = get_stations()  # fail fast (503) before spending any external API calls

    start, calls_start = resolve_location(start_text, "start")
    finish, calls_finish = resolve_location(finish_text, "finish")
    if haversine_miles(start.lat, start.lon, finish.lat, finish.lon) < 0.1:
        raise InvalidRequest("'start' and 'finish' must be different locations.")

    route, calls_route = get_route(start.point, finish.point)
    line = RouteLine(route.coords, total_miles=route.distance_miles)
    candidates = find_candidates(line, stations, settings.ROUTE_CORRIDOR_MILES)
    plan = plan_fuel_stops(
        candidates, route.distance_miles,
        max_range_miles=settings.VEHICLE_MAX_RANGE_MILES,
        mpg=settings.VEHICLE_MPG,
        stop_penalty=settings.FUEL_STOP_PENALTY_USD,
    )

    return {
        "start": start.as_dict(),
        "finish": finish.as_dict(),
        "route": {
            "provider": route.provider,
            "distance_miles": round(route.distance_miles, 1),
            "duration_hours": round(route.duration_seconds / 3600, 2),
            "stations_within_corridor": len(candidates),
            "corridor_miles": settings.ROUTE_CORRIDOR_MILES,
        },
        "fuel_plan": _plan_dict(plan),
        "map": {"geojson": _geojson(line.points, start, finish, plan)},
        "meta": {
            "external_api_calls": calls_start + calls_finish + calls_route,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "assumptions": ASSUMPTIONS,
        },
    }


def _plan_dict(plan: FuelPlan) -> dict:
    stops = []
    for number, stop in enumerate(plan.stops, start=1):
        s = stop.candidate.station
        stops.append({
            "stop_number": number,
            "opis_truckstop_id": s.opis_id,
            "name": s.name,
            "address": s.address,
            "city": s.city,
            "state": s.state,
            "rack_id": s.rack_id,
            "price_per_gallon": round(s.price, 4),
            "route_mile": round(stop.candidate.route_mile, 1),
            "off_route_miles": round(stop.candidate.off_route_miles, 1),
            "miles_since_previous_fuel": round(stop.miles_from_previous, 1),
            "fuel_on_arrival_gallons": round(stop.fuel_on_arrival_gallons, 2),
            "gallons_purchased": round(stop.gallons, 3),
            "fill": stop.fill_type,
            "cost_usd": round(stop.cost, 2),
            "location": {"lat": s.lat, "lon": s.lon, "precision": "city"},
        })
    total_cost = round(sum(stop["cost_usd"] for stop in stops), 2)
    return {
        "vehicle": {
            "max_range_miles": settings.VEHICLE_MAX_RANGE_MILES,
            "mpg": settings.VEHICLE_MPG,
            "tank_gallons": settings.VEHICLE_MAX_RANGE_MILES / settings.VEHICLE_MPG,
            "starting_fuel_gallons": round(plan.starting_fuel_gallons, 3),
        },
        "number_of_stops": len(stops),
        "stops": stops,
        "total_gallons_purchased": round(plan.gallons_purchased, 3),
        "total_fuel_cost_usd": total_cost,
        "route_miles": round(plan.route_miles, 1),
        "detour_miles": round(plan.detour_miles, 1),
        "total_miles_driven": round(plan.total_miles, 1),
        "fuel_used_gallons": round(plan.fuel_used_gallons, 3),
        "fuel_left_at_destination_gallons": round(plan.fuel_left_at_destination_gallons, 3),
        "note": (
            "No fuel purchase needed: the route is within the 500-mile range of the starting full tank."
            if not stops else
            f"{len(stops)} fuel stop(s) keep every leg within the 500-mile range at the lowest fuel cost found."
        ),
    }


def _geojson(coords, start, finish, plan: FuelPlan) -> dict:
    # ~100 m tolerance keeps the map accurate while keeping the response small.
    line = simplify(coords, 0.001)
    features = [
        {
            "type": "Feature",
            "properties": {"kind": "route"},
            "geometry": {"type": "LineString", "coordinates": [[round(lon, 5), round(lat, 5)] for lat, lon in line]},
        },
        _point(start.lon, start.lat, {"kind": "start", "label": start.label}),
        _point(finish.lon, finish.lat, {"kind": "finish", "label": finish.label}),
    ]
    for number, stop in enumerate(plan.stops, start=1):
        s = stop.candidate.station
        features.append(_point(s.lon, s.lat, {
            "kind": "fuel_stop", "stop_number": number, "name": s.name, "city": s.city, "state": s.state,
            "price_per_gallon": round(s.price, 4), "gallons_purchased": round(stop.gallons, 3),
            "cost_usd": round(stop.cost, 2), "route_mile": round(stop.candidate.route_mile, 1),
        }))
    return {"type": "FeatureCollection", "features": features}


def _point(lon, lat, properties) -> dict:
    return {"type": "Feature", "properties": properties,
            "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]}}
