"""Choose where to buy fuel along a route.

Model (all distances in miles, fuel measured as miles of range):

* The vehicle starts with a full tank (500 miles of range) and must never run
  dry. Distance between two consecutive fuel points =
  detour out of the first station + route miles between them + detour to the
  second station ("detour" = straight-line distance from the route, counted in
  each direction).
* At every fuel stop the tank is filled completely, except at the final stop,
  where only the fuel needed to reach the destination is bought.
* Among all feasible plans, dynamic programming picks the one with the lowest
  total fuel cost (+ an optional small per-stop penalty, see settings, so a
  stop is only added when it saves real money). The penalty is never part of
  the reported cost.

Because a full tank maximises the reachable distance at each stop, a plan
exists under this policy whenever any sequence of stations can cover the
route, so the 500-mile constraint is enforced exactly and infeasible routes are
reported instead of being silently "solved".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .data import Station
from .errors import NoFeasibleFuelPlan

EPS = 1e-9


@dataclass(frozen=True)
class Candidate:
    station: Station
    route_mile: float
    off_route_miles: float

    @property
    def price(self) -> float:
        return self.station.price


@dataclass
class PlannedStop:
    candidate: Candidate
    miles_from_previous: float  # driven since the previous fuel point (incl. detours)
    fuel_on_arrival_gallons: float
    gallons: float
    cost: float
    fill_type: str  # "full" or "partial"


@dataclass
class FuelPlan:
    stops: list[PlannedStop] = field(default_factory=list)
    route_miles: float = 0.0
    detour_miles: float = 0.0
    fuel_used_gallons: float = 0.0
    gallons_purchased: float = 0.0
    total_cost: float = 0.0
    fuel_left_at_destination_gallons: float = 0.0
    starting_fuel_gallons: float = 0.0

    @property
    def total_miles(self) -> float:
        return self.route_miles + self.detour_miles


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Stations at the same location only differ by price: keep the cheapest one."""
    best: dict[tuple[float, float], Candidate] = {}
    for cand in candidates:
        key = (cand.station.lat, cand.station.lon)
        current = best.get(key)
        if current is None or (cand.price, cand.station.opis_id) < (current.price, current.station.opis_id):
            best[key] = cand
    return sorted(best.values(), key=lambda c: (c.route_mile, c.price))


def plan_fuel_stops(candidates: list[Candidate], route_miles: float, *, max_range_miles: float, mpg: float,
                    stop_penalty: float = 0.0) -> FuelPlan:
    R = max_range_miles
    plan = FuelPlan(route_miles=route_miles, starting_fuel_gallons=R / mpg)
    if route_miles <= R + EPS:
        plan.fuel_used_gallons = route_miles / mpg
        plan.fuel_left_at_destination_gallons = (R - route_miles) / mpg
        return plan

    cands = dedupe_candidates([c for c in candidates if 0.0 <= c.route_mile <= route_miles])
    n = len(cands)
    pos = [0.0] + [c.route_mile for c in cands]  # node 0 is the start
    off = [0.0] + [c.off_route_miles for c in cands]
    price = [0.0] + [c.price for c in cands]

    def leg(i: int, j: int) -> float:
        return off[i] + (pos[j] - pos[i]) + off[j]

    def to_end(i: int) -> float:
        return off[i] + (route_miles - pos[i])

    inf = float("inf")
    # best[i] = (objective, cost, previous node) for "tank full at node i"
    best: list[tuple[float, float, int]] = [(inf, inf, -1)] * (n + 1)
    best[0] = (0.0, 0.0, -1)
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            if pos[j] - pos[i] > R + EPS:
                break
            if best[i][0] == inf:
                continue
            d = leg(i, j)
            if d > R + EPS:
                continue
            cost = best[i][1] + price[j] * d / mpg
            objective = best[i][0] + price[j] * d / mpg + stop_penalty
            if objective < best[j][0] - EPS:
                best[j] = (objective, cost, i)

    # Finish either straight from a full tank at i, or via one last partial stop j.
    finish: tuple[float, float, int, int] = (inf, inf, -1, -1)  # objective, cost, last_full, partial
    for i in range(n + 1):
        if best[i][0] == inf:
            continue
        if to_end(i) <= R + EPS:
            if best[i][0] < finish[0] - EPS:
                finish = (best[i][0], best[i][1], i, -1)
            continue
        for j in range(i + 1, n + 1):
            if pos[j] - pos[i] > R + EPS:
                break
            d_in, d_out = leg(i, j), to_end(j)
            if d_in > R + EPS or d_out > R + EPS:
                continue
            buy = d_in + d_out - R
            objective = best[i][0] + price[j] * buy / mpg + stop_penalty
            if objective < finish[0] - EPS:
                finish = (objective, best[i][1] + price[j] * buy / mpg, i, j)

    if finish[2] < 0:
        raise _infeasible(pos, route_miles, R)

    # Rebuild the chain of full-tank stops.
    chain = []
    node = finish[2]
    while node > 0:
        chain.append(node)
        node = best[node][2]
    chain.reverse()

    prev = 0
    detour = 0.0
    for j in chain:
        d = leg(prev, j)
        gallons = d / mpg
        plan.stops.append(PlannedStop(cands[j - 1], d, (R - d) / mpg, gallons, gallons * price[j], "full"))
        detour += 2 * off[j]
        prev = j
    final_leg_from = prev
    if finish[3] > 0:
        j = finish[3]
        d_in, d_out = leg(prev, j), to_end(j)
        gallons = (d_in + d_out - R) / mpg
        plan.stops.append(PlannedStop(cands[j - 1], d_in, (R - d_in) / mpg, gallons, gallons * price[j], "partial"))
        detour += 2 * off[j]
        final_leg_from = j
        fuel_left = 0.0
    else:
        fuel_left = R - to_end(final_leg_from)

    plan.detour_miles = detour
    plan.fuel_used_gallons = plan.total_miles / mpg
    plan.gallons_purchased = sum(s.gallons for s in plan.stops)
    plan.total_cost = sum(s.cost for s in plan.stops)
    plan.fuel_left_at_destination_gallons = max(0.0, fuel_left) / mpg
    return plan


def _infeasible(positions: list[float], route_miles: float, R: float) -> NoFeasibleFuelPlan:
    points = sorted(positions) + [route_miles]
    for a, b in zip(points, points[1:]):
        if b - a > R:
            return NoFeasibleFuelPlan(
                f"No fuel station from the price list is reachable between route mile {a:.0f} and "
                f"route mile {b:.0f} ({b - a:.0f} miles, more than the {R:.0f}-mile range).",
                gap_start_mile=round(a, 1), gap_end_mile=round(b, 1),
            )
    return NoFeasibleFuelPlan(
        f"No refuelling plan keeps every leg within the {R:.0f}-mile range using the listed stations "
        "(detours to reach the stations make some legs too long)."
    )
