"""Plans one trip: locations -> route options -> stations on each route -> fuel plan -> best route."""

from __future__ import annotations

import copy
import hashlib
import time
from collections import Counter
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from .data import Station, StationIndex, get_stations
from .errors import InvalidRequest, NoFeasibleFuelPlan, PlannerError
from .geo import RouteLine, haversine_miles, simplify
from .geocoding import ResolvedLocation, resolve_location
from .optimizer import Candidate, FuelPlan, plan_fuel_stops
from .routing import Route, get_routes, provider_name

ASSUMPTIONS = [
    "Vehicle: 500-mile maximum range and 10 miles per gallon (50-gallon tank).",
    "The trip starts with the requested tank level (default: full). That starting fuel is valued at the typical "
    "listed price near the start (median of listed stations within 50 miles).",
    "At each stop the planner either fills the tank (where fuel is cheap) or buys just enough to reach the next "
    "stop or the destination (where it is expensive), never letting the tank fall below the requested reserve.",
    "total_fuel_cost_usd is the cost of the fuel burned on the trip: starting fuel value + purchases - value of the "
    "fuel left at the destination (valued at the price it was last bought at).",
    "Where possible, every stop except the last buys at least 10 gallons: a tiny top-up is rounded up by moving "
    "fuel from the next stop (costs a few cents more, far more practical).",
    "Fuel prices come from the supplied price list; when a station is listed several times its lowest price is "
    "used. Non-US (Canadian) rows are excluded.",
    "The price list has no coordinates. A station whose address names a highway the route uses (e.g. 'I-44, EXIT "
    "283') is placed on that highway at the point nearest its city; other stations are placed at their city centre "
    "and the detour there and back is added to the distance and to the range check.",
    "When the routing service offers several routes, the one with the lowest total fuel cost is chosen unless "
    "route_preference=fastest.",
]


@dataclass
class TripOptions:
    start_fuel_percent: float = 100.0
    reserve_miles: float = 0.0
    route_preference: str = "cheapest"  # or "fastest"
    alternatives: bool = True


@dataclass
class RouteResult:
    index: int
    route: Route
    line: RouteLine
    candidates: list[Candidate]
    plan: FuelPlan | None = None
    error: PlannerError | None = None
    costs: dict | None = None

    @property
    def feasible(self) -> bool:
        return self.plan is not None


def find_candidates(
    line: RouteLine, stations: StationIndex, corridor_miles: float, snap_miles: float | None = None
) -> list[Candidate]:
    """Stations near the route, with their position along it.

    * Address names a highway the route uses, and the route passes within
      `snap_miles` of the city (half that for non-interstates): the station is
      placed on that highway, at the point nearest its city (no detour).
    * Otherwise, the city centre within `corridor_miles` of the route: the
      station keeps its city location and the detour is counted.
    """
    snap_miles = settings.HIGHWAY_SNAP_MILES if snap_miles is None else snap_miles
    reach = max(corridor_miles, snap_miles)
    cell = stations.CELL_DEG
    pad = max(1, int(reach // (cell * 45.0)) + 1)
    wanted = set()
    for lat, lon in line.points:
        cy, cx = int(lat // cell), int(lon // cell)
        for dy in range(-pad, pad + 1):
            for dx in range(-pad, pad + 1):
                wanted.add((cy + dy, cx + dx))

    memo: dict[tuple, tuple | None] = {}
    result = []
    for key in wanted:
        for station in stations.grid.get(key, ()):
            shared = frozenset(station.highways) & line.refs
            memo_key = (station.lat, station.lon, shared)
            if memo_key not in memo:
                memo[memo_key] = _place(line, station, shared, corridor_miles, snap_miles)
            placed = memo[memo_key]
            if placed is not None:
                mile, off, placement, lat, lon = placed
                result.append(Candidate(station, mile, off, placement, lat, lon))
    return result


def _place(line: RouteLine, station: Station, shared: frozenset[str], corridor_miles: float, snap_miles: float):
    if shared:
        radius = snap_miles if any(ref.startswith("I-") for ref in shared) else snap_miles / 2
        m = line.match(station.lat, station.lon, radius, on_highways=shared)
        if m is not None:
            return (m.route_mile, 0.0, "highway", m.lat, m.lon)
    m = line.match(station.lat, station.lon, corridor_miles)
    if m is not None:
        return (m.route_mile, m.off_route_miles, "city", station.lat, station.lon)
    return None


def plan_trip(start_text, finish_text, options: TripOptions | None = None) -> dict:
    options = options or TripOptions()
    started = time.perf_counter()
    stations = get_stations()  # fail fast (503) before spending any external API calls

    start, calls_start = resolve_location(start_text, "start")
    finish, calls_finish = resolve_location(finish_text, "finish")
    if haversine_miles(start.lat, start.lon, finish.lat, finish.lon) < 0.1:
        raise InvalidRequest("'start' and 'finish' must be different locations.")

    # A finished plan only depends on these inputs, so repeats are served from the cache.
    plan_key = _plan_cache_key(start, finish, options, stations)
    cached = cache.get(plan_key)
    if cached is not None:
        result = copy.deepcopy(cached)
        result["start"]["query"], result["finish"]["query"] = start.query, finish.query
        result["meta"]["external_api_calls"] = calls_start + calls_finish
        result["meta"]["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return result

    routes, calls_route = get_routes(start.point, finish.point, alternatives=options.alternatives)
    R, mpg = settings.VEHICLE_MAX_RANGE_MILES, settings.VEHICLE_MPG
    local = stations.local_price(start.lat, start.lon)

    results: list[RouteResult] = []
    for index, route in enumerate(routes):
        line = RouteLine(route.coords, total_miles=route.distance_miles, segments=route.segments)
        candidates = find_candidates(line, stations, settings.ROUTE_CORRIDOR_MILES)
        result = RouteResult(index, route, line, candidates)
        try:
            result.plan = plan_fuel_stops(
                candidates,
                route.distance_miles,
                max_range_miles=R,
                mpg=mpg,
                start_fuel_miles=R * options.start_fuel_percent / 100.0,
                reserve_miles=options.reserve_miles,
                stop_penalty=settings.FUEL_STOP_PENALTY_USD,
                min_purchase_gallons=settings.MIN_PURCHASE_GALLONS,
            )
            result.costs = _costs(result.plan, local)
        except NoFeasibleFuelPlan as exc:
            result.error = exc
        results.append(result)

    feasible = [r for r in results if r.feasible]
    if not feasible:
        raise results[0].error
    if options.route_preference == "fastest":
        chosen = min(feasible, key=lambda r: (r.route.duration_seconds, r.costs["total_fuel_cost_usd"]))
    else:
        chosen = min(feasible, key=lambda r: (round(r.costs["total_fuel_cost_usd"], 2), r.route.duration_seconds))

    result = {
        "start": start.as_dict(),
        "finish": finish.as_dict(),
        "options": {
            "start_fuel_percent": options.start_fuel_percent,
            "reserve_miles": options.reserve_miles,
            "route_preference": options.route_preference,
        },
        "route": _route_dict(chosen),
        "alternatives": [_alternative_dict(r, chosen) for r in results],
        "fuel_plan": _plan_dict(chosen.plan, chosen.costs, local, options),
        "map": {"geojson": _geojson(chosen, results, start, finish)},
        "meta": {
            "external_api_calls": calls_start + calls_finish + calls_route,
            "routes_compared": len(results),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "assumptions": ASSUMPTIONS,
        },
    }
    cache.set(plan_key, result)
    return result


def _plan_cache_key(start: ResolvedLocation, finish: ResolvedLocation, options: TripOptions, stations) -> str:
    parts = (
        "plan:v2",
        provider_name(),
        f"{start.lat:.5f},{start.lon:.5f}",
        f"{finish.lat:.5f},{finish.lon:.5f}",
        options.start_fuel_percent,
        options.reserve_miles,
        options.route_preference,
        options.alternatives,
        stations.version,
        settings.ROUTE_ALTERNATIVES,
        settings.ROUTE_CORRIDOR_MILES,
        settings.HIGHWAY_SNAP_MILES,
        settings.FUEL_STOP_PENALTY_USD,
        settings.MIN_PURCHASE_GALLONS,
    )
    return "plan:" + hashlib.sha256(repr(parts).encode()).hexdigest()


def _costs(plan: FuelPlan, local: dict | None) -> dict:
    start_price = local["price_per_gallon"] if local else 0.0
    start_value = plan.starting_fuel_gallons * start_price
    last_price = plan.stops[-1].candidate.price if plan.stops else start_price
    left_value = plan.fuel_left_at_destination_gallons * last_price
    return {
        "starting_fuel_value_usd": start_value,
        "purchases_usd": plan.purchases_cost,
        "fuel_left_value_usd": left_value,
        "total_fuel_cost_usd": start_value + plan.purchases_cost - left_value,
    }


def _main_highways(route: Route, limit: int = 6) -> list[str]:
    miles: Counter[str] = Counter()
    for length, refs in route.segments:
        for ref in refs:
            miles[ref] += length
    return [ref for ref, m in miles.most_common() if m >= 10][:limit]


def _route_dict(result: RouteResult) -> dict:
    snapped = sum(1 for c in result.candidates if c.placement == "highway")
    return {
        "provider": result.route.provider,
        "index": result.index,
        "distance_miles": round(result.route.distance_miles, 1),
        "duration_hours": round(result.route.duration_seconds / 3600, 2),
        "main_highways": _main_highways(result.route),
        "stations_considered": len(result.candidates),
        "stations_on_highway": snapped,
        "corridor_miles": settings.ROUTE_CORRIDOR_MILES,
    }


def _alternative_dict(result: RouteResult, chosen: RouteResult) -> dict:
    item = {
        "index": result.index,
        "chosen": result is chosen,
        "distance_miles": round(result.route.distance_miles, 1),
        "duration_hours": round(result.route.duration_seconds / 3600, 2),
        "main_highways": _main_highways(result.route),
        "feasible": result.feasible,
    }
    if result.feasible:
        item["number_of_stops"] = len(result.plan.stops)
        item["total_fuel_cost_usd"] = round(result.costs["total_fuel_cost_usd"], 2)
        item["purchases_usd"] = round(result.costs["purchases_usd"], 2)
    else:
        item["error"] = result.error.as_dict()
    return item


def _plan_dict(plan: FuelPlan, costs: dict, local: dict | None, options: TripOptions) -> dict:
    stops = []
    for number, stop in enumerate(plan.stops, start=1):
        c = stop.candidate
        s = c.station
        stops.append(
            {
                "stop_number": number,
                "opis_truckstop_id": s.opis_id,
                "name": s.name,
                "address": s.address,
                "city": s.city,
                "state": s.state,
                "rack_id": s.rack_id,
                "price_per_gallon": round(s.price, 4),
                "route_mile": round(c.route_mile, 1),
                "off_route_miles": round(c.off_route_miles, 1),
                "miles_since_previous_fuel": round(stop.miles_from_previous, 1),
                "fuel_on_arrival_gallons": round(stop.fuel_on_arrival_gallons, 2),
                "gallons_purchased": round(stop.gallons, 3),
                "fill": stop.fill_type,
                "cost_usd": round(stop.cost, 2),
                "location": {
                    "lat": round(c.lat if c.lat is not None else s.lat, 5),
                    "lon": round(c.lon if c.lon is not None else s.lon, 5),
                    "placement": c.placement,
                },
            }
        )
    mpg = plan.mpg
    total = round(costs["total_fuel_cost_usd"], 2)
    if not stops:
        note = (
            f"No fuel stop needed: the starting fuel covers the {plan.route_miles:.0f}-mile trip "
            f"(with the {options.reserve_miles:.0f}-mile reserve)."
        )
    else:
        note = (
            f"{len(stops)} fuel stop(s) keep every leg within the {plan.max_range_miles:.0f}-mile range "
            "at the lowest fuel cost found."
        )
    return {
        "vehicle": {
            "max_range_miles": plan.max_range_miles,
            "mpg": mpg,
            "tank_gallons": plan.max_range_miles / mpg,
        },
        "starting_fuel": {
            "gallons": round(plan.starting_fuel_gallons, 3),
            "price_per_gallon": round(local["price_per_gallon"], 4) if local else None,
            "price_source": local["source"] if local else "no listed stations",
            "value_usd": round(costs["starting_fuel_value_usd"], 2),
        },
        "number_of_stops": len(stops),
        "stops": stops,
        "total_gallons_purchased": round(plan.gallons_purchased, 3),
        "purchases_usd": round(costs["purchases_usd"], 2),
        "fuel_used_gallons": round(plan.fuel_used_gallons, 3),
        "fuel_left_at_destination": {
            "gallons": round(plan.fuel_left_at_destination_gallons, 3),
            "value_usd": round(costs["fuel_left_value_usd"], 2),
        },
        "total_fuel_cost_usd": total,
        "route_miles": round(plan.route_miles, 1),
        "detour_miles": round(plan.detour_miles, 1),
        "total_miles_driven": round(plan.total_miles, 1),
        "note": note,
    }


def _line_coords(coords, tolerance):
    return [[round(lon, 5), round(lat, 5)] for lat, lon in simplify(coords, tolerance)]


def _geojson(
    chosen: RouteResult, results: list[RouteResult], start: ResolvedLocation, finish: ResolvedLocation
) -> dict:
    features = []
    for r in results:
        if r is chosen:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "kind": "alternative_route",
                    "index": r.index,
                    "feasible": r.feasible,
                    "total_fuel_cost_usd": round(r.costs["total_fuel_cost_usd"], 2) if r.feasible else None,
                    "distance_miles": round(r.route.distance_miles, 1),
                },
                # Coarser line for alternatives: they are only drawn for context.
                "geometry": {"type": "LineString", "coordinates": _line_coords(r.line.points, 0.01)},
            }
        )
    features.append(
        {
            "type": "Feature",
            "properties": {"kind": "route", "index": chosen.index},
            # ~100 m tolerance keeps the map accurate while keeping the response small.
            "geometry": {"type": "LineString", "coordinates": _line_coords(chosen.line.points, 0.001)},
        }
    )
    features.append(_point(start.lon, start.lat, {"kind": "start", "label": start.label}))
    features.append(_point(finish.lon, finish.lat, {"kind": "finish", "label": finish.label}))
    for number, stop in enumerate(chosen.plan.stops, start=1):
        c = stop.candidate
        s = c.station
        features.append(
            _point(
                c.lon if c.lon is not None else s.lon,
                c.lat if c.lat is not None else s.lat,
                {
                    "kind": "fuel_stop",
                    "stop_number": number,
                    "name": s.name,
                    "address": s.address,
                    "city": s.city,
                    "state": s.state,
                    "price_per_gallon": round(s.price, 4),
                    "gallons_purchased": round(stop.gallons, 3),
                    "cost_usd": round(stop.cost, 2),
                    "route_mile": round(c.route_mile, 1),
                    "fill": stop.fill_type,
                    "placement": c.placement,
                },
            )
        )
    return {"type": "FeatureCollection", "features": features}


def _point(lon, lat, properties) -> dict:
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
    }
