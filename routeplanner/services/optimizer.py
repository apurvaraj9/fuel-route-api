"""Choose where to stop and how much fuel to buy along a route.

Model (distances in miles; fuel is measured as miles of range):

* The vehicle starts with `start_fuel_miles` in the tank and holds at most
  `max_range_miles`. It may never drop below `reserve_miles` (including at the
  destination).
* Driving from fuel point A to fuel point B costs: detour out of A + route miles
  between them + detour to B ("detour" = distance between the station and the
  route, counted in each direction).
* At each stop the driver either **fills the tank** or buys **just enough to
  reach the next stop (or the destination)**. For stations on a line this is
  known to contain an optimal plan ("To fill or not to fill", Khuller, Malekian
  and Mestre, 2007): fill up where fuel is cheap, buy only what you need where
  it is expensive.
* A small per-stop penalty (settings.FUEL_STOP_PENALTY_USD) is added while
  choosing, so a stop is only added when it saves real money. It is never part
  of any reported cost.

Because a full tank maximises reach at every stop, the planner finds a plan
whenever any sequence of the listed stations can cover the route, and reports
the gap otherwise.

Dynamic programming: the arrival fuel at a stop is either the reserve (the
previous stop bought "just enough") or what is left after a full tank / the
starting fuel. That gives O(k) states per stop (k = stations within one tank),
and each transition is O(log k) thanks to prefix minima, so a coast-to-coast
route with a few hundred candidate stations takes milliseconds.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field

from .data import Station
from .errors import NoFeasibleFuelPlan

EPS = 1e-9


@dataclass(frozen=True)
class Candidate:
    station: Station
    route_mile: float
    off_route_miles: float
    placement: str = "city"  # "highway" when placed on the route's highway, else the city centre
    lat: float | None = None  # where the station is assumed to be (defaults to its city centre)
    lon: float | None = None

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
    max_range_miles: float = 0.0
    mpg: float = 0.0
    reserve_miles: float = 0.0
    starting_fuel_gallons: float = 0.0
    fuel_left_at_destination_gallons: float = 0.0
    purchases_cost: float = 0.0

    @property
    def total_miles(self) -> float:
        return self.route_miles + self.detour_miles

    @property
    def fuel_used_gallons(self) -> float:
        return self.total_miles / self.mpg

    @property
    def gallons_purchased(self) -> float:
        return sum(s.gallons for s in self.stops)


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Stations at exactly the same point only differ by price: keep the cheapest."""
    best: dict[tuple[float, float], Candidate] = {}
    for cand in candidates:
        key = (round(cand.route_mile, 3), round(cand.off_route_miles, 3))
        current = best.get(key)
        if current is None or (cand.price, cand.station.opis_id) < (current.price, current.station.opis_id):
            best[key] = cand
    return sorted(best.values(), key=lambda c: (c.route_mile, c.off_route_miles, c.price))


@dataclass
class _State:
    arrival: float  # usable fuel on arrival (miles above the reserve)
    objective: float  # money + stop penalties
    cost: float  # money only
    prev_node: int  # 0 = start
    prev_state: int  # index into states[prev_node] (-1 for the start)
    bought: float  # miles of fuel bought at prev_node to get here
    fill_type: str  # what was done at prev_node


def plan_fuel_stops(
    candidates: list[Candidate],
    route_miles: float,
    *,
    max_range_miles: float,
    mpg: float,
    start_fuel_miles: float | None = None,
    reserve_miles: float = 0.0,
    stop_penalty: float = 0.0,
    min_purchase_gallons: float = 0.0,
) -> FuelPlan:
    R = max_range_miles
    S = R if start_fuel_miles is None else start_fuel_miles
    cap = R - reserve_miles  # usable capacity above the reserve
    usable_start = S - reserve_miles
    if cap <= EPS:
        raise NoFeasibleFuelPlan("The reserve must be smaller than the vehicle's range.")
    if usable_start < -EPS:
        raise NoFeasibleFuelPlan("The starting fuel is below the requested reserve.")
    usable_start = max(usable_start, 0.0)

    plan = FuelPlan(
        route_miles=route_miles, max_range_miles=R, mpg=mpg, reserve_miles=reserve_miles, starting_fuel_gallons=S / mpg
    )
    if route_miles <= usable_start + EPS:
        plan.fuel_left_at_destination_gallons = (S - route_miles) / mpg
        return plan

    cands = dedupe_candidates([c for c in candidates if 0.0 <= c.route_mile <= route_miles])
    n = len(cands)
    pos = [0.0] + [c.route_mile for c in cands]
    off = [0.0] + [c.off_route_miles for c in cands]
    price = [0.0] + [c.price for c in cands]

    def leg(i: int, j: int) -> float:
        return off[i] + (pos[j] - pos[i]) + off[j]

    def to_end(i: int) -> float:
        return off[i] + (route_miles - pos[i])

    states: list[list[_State]] = [[] for _ in range(n + 1)]
    # Per node, after its states are final: states sorted by arrival, and prefix minima of
    # (objective - arrival * price) so "buy up to level L" can be priced in O(log k).
    sorted_arrivals: list[list[float]] = [[] for _ in range(n + 1)]
    prefix_best: list[list[tuple[float, float, int]]] = [[] for _ in range(n + 1)]
    fill_best: list[tuple[float, float, int] | None] = [None] * (n + 1)

    def finalize(i: int) -> None:
        order = sorted(range(len(states[i])), key=lambda k: states[i][k].arrival)
        p = price[i]
        arrivals, prefix = [], []
        best: tuple[float, float, int] | None = None
        for k in order:
            st = states[i][k]
            item = (st.objective - st.arrival * p, st.cost - st.arrival * p, k)
            if best is None or item[0] < best[0] - EPS:
                best = item
            arrivals.append(st.arrival)
            prefix.append(best)
        sorted_arrivals[i], prefix_best[i] = arrivals, prefix
        fill_best[i] = best

    seen: list[dict[float, int]] = [{} for _ in range(n + 1)]  # arrival level -> state index

    def add_state(j: int, new: _State) -> None:
        key = round(new.arrival, 6)
        k = seen[j].get(key)
        if k is None:
            seen[j][key] = len(states[j])
            states[j].append(new)
        elif new.objective < states[j][k].objective - EPS:
            states[j][k] = new

    # Arrivals straight from the start (no purchase at the start).
    for j in range(1, n + 1):
        if pos[j] > usable_start + EPS:
            break
        d = leg(0, j)
        if d > usable_start + EPS:
            continue
        add_state(j, _State(usable_start - d, 0.0, 0.0, 0, -1, 0.0, "start"))

    first = 0
    for j in range(1, n + 1):
        while pos[j] - pos[first] > cap + EPS:
            first += 1
        for i in range(max(first, 1), j):
            if not states[i]:
                continue
            d = leg(i, j)
            if d > cap + EPS:
                continue
            p = price[i]
            # Only one move from i to j can be optimal: if j is cheaper, buy just enough at i to
            # reach it (more fuel at i would cost more than buying it at j); otherwise fill up at i
            # (fuel bought later at j would cost at least as much). This halves the work.
            if price[j] < p:
                # Buy just enough at i to reach j (only from states that need fuel).
                idx = bisect_left(sorted_arrivals[i], d - EPS) - 1
                if idx >= 0:
                    base_obj, base_cost, k = prefix_best[i][idx]
                    add_state(
                        j,
                        _State(
                            0.0,
                            base_obj + d * p + stop_penalty,
                            base_cost + d * p,
                            i,
                            k,
                            d - states[i][k].arrival,
                            "partial",
                        ),
                    )
                continue
            # Fill the tank at i, arrive at j with cap - d.
            base_obj, base_cost, k = fill_best[i]
            if cap - states[i][k].arrival > EPS:
                add_state(
                    j,
                    _State(
                        cap - d,
                        base_obj + cap * p + stop_penalty,
                        base_cost + cap * p,
                        i,
                        k,
                        cap - states[i][k].arrival,
                        "full",
                    ),
                )
        if states[j]:
            finalize(j)

    # Finish: from the start directly, or from a stop buying just enough to reach the end.
    best_finish: tuple[float, float, int, int, float] | None = None  # obj, cost, node, state, bought
    for i in range(1, n + 1):
        if not states[i]:
            continue
        need = to_end(i)
        if need > cap + EPS:
            continue
        idx = bisect_left(sorted_arrivals[i], need - EPS) - 1
        if idx < 0:
            continue  # every way of arriving here could finish without stopping here
        base_obj, base_cost, k = prefix_best[i][idx]
        obj = base_obj + need * price[i] + stop_penalty
        if best_finish is None or obj < best_finish[0] - EPS:
            best_finish = (obj, base_cost + need * price[i], i, k, need - states[i][k].arrival)

    if best_finish is None:
        raise _infeasible(pos, route_miles, cap, usable_start)

    # Rebuild the chain of stops from the destination backwards.
    chain: list[tuple[int, float, str]] = [(best_finish[2], best_finish[4], "partial")]
    node, k = best_finish[2], best_finish[3]
    while node > 0:
        st = states[node][k]
        if st.prev_node > 0:
            chain.append((st.prev_node, st.bought, st.fill_type))
        node, k = st.prev_node, st.prev_state
    chain.reverse()

    prev = 0
    fuel = usable_start  # usable miles in the tank
    detour = 0.0
    for j, bought, fill_type in chain:
        d = leg(prev, j)
        fuel -= d
        gallons = bought / mpg
        plan.stops.append(
            PlannedStop(cands[j - 1], d, (fuel + reserve_miles) / mpg, gallons, gallons * price[j], fill_type)
        )
        fuel += bought
        detour += 2 * off[j]
        prev = j
    fuel -= to_end(prev)
    plan.detour_miles = detour
    if min_purchase_gallons > 0:
        _round_up_small_purchases(plan.stops, min_purchase_gallons, R / mpg)
    plan.purchases_cost = sum(s.cost for s in plan.stops)
    plan.fuel_left_at_destination_gallons = (max(fuel, 0.0) + reserve_miles) / mpg
    return plan


def _round_up_small_purchases(stops: list[PlannedStop], minimum: float, tank: float) -> None:
    """Make tiny top-ups realistic: buy at least `minimum` gallons at a stop by moving fuel from the next stop.

    The cheapest plan sometimes buys a fraction of a gallon just to reach a cheaper station a few
    miles later. Moving a few gallons from the next stop to this one keeps every leg and the final
    fuel level unchanged and costs a few cents more. The last stop (buying just enough to finish)
    is left alone, and the next stop is never made smaller than `minimum`.
    """
    for here, nxt in zip(stops, stops[1:], strict=False):
        if here.gallons >= minimum - 1e-9:
            continue
        room = tank - (here.fuel_on_arrival_gallons + here.gallons)
        shift = min(minimum - here.gallons, room, nxt.gallons - minimum)
        if shift <= 1e-9:
            continue
        here.gallons += shift
        nxt.gallons -= shift
        nxt.fuel_on_arrival_gallons += shift
        here.cost = here.gallons * here.candidate.price
        nxt.cost = nxt.gallons * nxt.candidate.price
        if here.fuel_on_arrival_gallons + here.gallons >= tank - 1e-6:
            here.fill_type = "full"


def _infeasible(positions: list[float], route_miles: float, cap: float, usable_start: float) -> NoFeasibleFuelPlan:
    stations = positions[1:]
    first = stations[0] if stations else route_miles
    if first > usable_start + EPS:
        return NoFeasibleFuelPlan(
            f"The starting fuel ({usable_start:.0f} usable miles) does not reach the first listed station "
            f"on the route (route mile {first:.0f}).",
            gap_start_mile=0.0,
            gap_end_mile=round(first, 1),
        )
    points = stations + [route_miles]
    for a, b in zip(points, points[1:], strict=False):
        if b - a > cap + EPS:
            return NoFeasibleFuelPlan(
                f"No fuel station from the price list is reachable between route mile {a:.0f} and "
                f"route mile {b:.0f} ({b - a:.0f} miles, more than the usable range of {cap:.0f} miles).",
                gap_start_mile=round(a, 1),
                gap_end_mile=round(b, 1),
            )
    return NoFeasibleFuelPlan(
        "No refuelling plan keeps every leg within range using the listed stations "
        "(detours to reach the stations make some legs too long)."
    )
