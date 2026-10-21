import itertools
import random

from django.test import SimpleTestCase

from routeplanner.services.data import Station
from routeplanner.services.errors import NoFeasibleFuelPlan
from routeplanner.services.optimizer import Candidate, plan_fuel_stops

R, MPG = 500.0, 10.0


def cand(mile, price, off=0.0, sid=None):
    sid = sid if sid is not None else int(mile * 10)
    station = Station(sid, f"S{sid}", "addr", "City", "TX", 1, price, 1, 30.0 + mile / 1000, -100.0)
    return Candidate(station, mile, off)


def check_plan(test, plan, route_miles):
    """Invariants every plan must satisfy (the vehicle never runs dry, tank never overfilled)."""
    fuel = R  # miles of range, start full
    prev_mile, prev_off = 0.0, 0.0
    for stop in plan.stops:
        leg = prev_off + (stop.candidate.route_mile - prev_mile) + stop.candidate.off_route_miles
        test.assertLessEqual(leg, R + 1e-6)
        fuel -= leg
        test.assertGreaterEqual(fuel, -1e-6)
        test.assertAlmostEqual(stop.fuel_on_arrival_gallons, fuel / MPG, places=6)
        fuel += stop.gallons * MPG
        test.assertLessEqual(fuel, R + 1e-6)
        test.assertAlmostEqual(stop.cost, stop.gallons * stop.candidate.price, places=9)
        prev_mile, prev_off = stop.candidate.route_mile, stop.candidate.off_route_miles
    fuel -= prev_off + (route_miles - prev_mile)
    test.assertGreaterEqual(fuel, -1e-6)
    test.assertAlmostEqual(plan.fuel_left_at_destination_gallons, max(fuel, 0) / MPG, places=6)
    test.assertAlmostEqual(plan.total_cost, sum(s.cost for s in plan.stops), places=9)
    # Fuel conservation: start + bought - left == used.
    test.assertAlmostEqual(R / MPG + plan.gallons_purchased - plan.fuel_left_at_destination_gallons,
                           plan.fuel_used_gallons, places=6)


def brute_force_cost(cands, route_miles):
    """Cheapest plan under the same policy, by trying every subset of stations."""
    best = float("inf")
    for k in range(len(cands) + 1):
        for subset in itertools.combinations(sorted(cands, key=lambda c: c.route_mile), k):
            fuel, cost, ok = R, 0.0, True
            prev_mile, prev_off = 0.0, 0.0
            for idx, c in enumerate(subset):
                leg = prev_off + c.route_mile - prev_mile + c.off_route_miles
                if leg > R:
                    ok = False
                    break
                fuel -= leg
                if idx == len(subset) - 1:  # last stop: buy just enough to finish
                    need = c.off_route_miles + route_miles - c.route_mile
                    if need > R:
                        ok = False
                        break
                    buy = max(0.0, need - fuel)
                else:
                    buy = R - fuel
                cost += buy / MPG * c.price
                fuel += buy
                prev_mile, prev_off = c.route_mile, c.off_route_miles
            if ok and fuel - (prev_off + route_miles - prev_mile) >= -1e-9:
                best = min(best, cost)
    return best


class OptimizerTests(SimpleTestCase):
    def test_short_route_needs_no_stop(self):
        plan = plan_fuel_stops([cand(100, 3.0)], 480, max_range_miles=R, mpg=MPG)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_cost, 0)
        self.assertAlmostEqual(plan.fuel_used_gallons, 48.0)
        self.assertAlmostEqual(plan.fuel_left_at_destination_gallons, 2.0)

    def test_exactly_500_miles_needs_no_stop(self):
        self.assertEqual(plan_fuel_stops([], 500, max_range_miles=R, mpg=MPG).stops, [])

    def test_buys_only_what_is_needed_to_finish(self):
        plan = plan_fuel_stops([cand(300, 4.0)], 700, max_range_miles=R, mpg=MPG)
        self.assertEqual(len(plan.stops), 1)
        stop = plan.stops[0]
        self.assertEqual(stop.fill_type, "partial")
        self.assertAlmostEqual(stop.fuel_on_arrival_gallons, 20.0)  # 200 miles left in the tank
        self.assertAlmostEqual(stop.gallons, 20.0)  # 400 miles to go - 200 in tank = 200 miles
        self.assertAlmostEqual(plan.total_cost, 80.0)
        self.assertAlmostEqual(plan.fuel_left_at_destination_gallons, 0.0)
        check_plan(self, plan, 700)

    def test_prefers_cheaper_reachable_station(self):
        plan = plan_fuel_stops([cand(250, 4.00), cand(450, 3.00)], 900, max_range_miles=R, mpg=MPG)
        self.assertEqual([s.candidate.route_mile for s in plan.stops], [450])
        self.assertAlmostEqual(plan.total_cost, 40 * 3.00)

    def test_cheapest_station_is_not_used_when_unreachable(self):
        # The $1 station at mile 520 is beyond the 500-mile range from the start.
        plan = plan_fuel_stops([cand(480, 5.0), cand(520, 1.0)], 950, max_range_miles=R, mpg=MPG)
        self.assertEqual([s.candidate.route_mile for s in plan.stops], [480])
        check_plan(self, plan, 950)

    def test_detour_counts_against_range_and_fuel(self):
        # The station at mile 490 is 8 miles off the route: reaching it takes 498 miles,
        # and the 16-mile round trip is added to the distance driven.
        plan = plan_fuel_stops([cand(490, 2.0, off=8)], 800, max_range_miles=R, mpg=MPG)
        self.assertEqual(len(plan.stops), 1)
        self.assertAlmostEqual(plan.stops[0].miles_from_previous, 498)
        self.assertAlmostEqual(plan.detour_miles, 16)
        self.assertAlmostEqual(plan.total_miles, 816)
        check_plan(self, plan, 800)
        with self.assertRaises(NoFeasibleFuelPlan):  # 495 + 8 > 500: unreachable
            plan_fuel_stops([cand(495, 2.0, off=8)], 800, max_range_miles=R, mpg=MPG)

    def test_long_route_needs_multiple_stops(self):
        cands = [cand(m, 3.0 + (m % 7) / 10) for m in range(40, 2800, 60)]
        plan = plan_fuel_stops(cands, 2800, max_range_miles=R, mpg=MPG, stop_penalty=5)
        self.assertGreaterEqual(len(plan.stops), 5)  # 2800 miles from a 500-mile tank
        check_plan(self, plan, 2800)

    def test_gap_larger_than_range_is_reported(self):
        with self.assertRaises(NoFeasibleFuelPlan) as ctx:
            plan_fuel_stops([cand(300, 3.0), cand(900, 3.0)], 1200, max_range_miles=R, mpg=MPG)
        self.assertEqual(ctx.exception.details["gap_start_mile"], 300)
        self.assertEqual(ctx.exception.details["gap_end_mile"], 900)

    def test_no_stations_on_long_route(self):
        with self.assertRaises(NoFeasibleFuelPlan):
            plan_fuel_stops([], 501, max_range_miles=R, mpg=MPG)

    def test_matches_brute_force_on_random_routes(self):
        rng = random.Random(42)
        for _ in range(150):
            length = rng.uniform(520, 1400)
            cands = [cand(rng.uniform(0, length), round(rng.uniform(2.8, 4.5), 3), off=rng.choice([0, 0, 2, 6]), sid=i)
                     for i in range(rng.randint(3, 9))]
            expected = brute_force_cost(cands, length)
            if expected == float("inf"):
                with self.assertRaises(NoFeasibleFuelPlan):
                    plan_fuel_stops(cands, length, max_range_miles=R, mpg=MPG)
                continue
            plan = plan_fuel_stops(cands, length, max_range_miles=R, mpg=MPG, stop_penalty=0)
            self.assertAlmostEqual(plan.total_cost, expected, places=6)
            check_plan(self, plan, length)

    def test_stop_penalty_trades_cents_for_fewer_stops(self):
        # Two stops save $0.50 versus one stop; a $5 penalty prefers one stop.
        cands = [cand(400, 3.00), cand(450, 3.01), cand(700, 2.99)]
        cheapest = plan_fuel_stops(cands, 1000, max_range_miles=R, mpg=MPG, stop_penalty=0)
        fewer = plan_fuel_stops(cands, 1000, max_range_miles=R, mpg=MPG, stop_penalty=5)
        self.assertLessEqual(cheapest.total_cost, fewer.total_cost)
        self.assertLessEqual(len(fewer.stops), len(cheapest.stops))
        check_plan(self, fewer, 1000)
