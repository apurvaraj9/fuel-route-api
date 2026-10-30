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


def check_plan(test, plan, route_miles, start=R, reserve=0.0, cap=R, mpg=MPG):
    """Invariants every plan must satisfy: never below the reserve, never above the tank size."""
    fuel = start
    prev_mile, prev_off = 0.0, 0.0
    for stop in plan.stops:
        leg = prev_off + (stop.candidate.route_mile - prev_mile) + stop.candidate.off_route_miles
        test.assertAlmostEqual(stop.miles_from_previous, leg, places=6)
        fuel -= leg
        test.assertGreaterEqual(fuel, reserve - 1e-6)
        test.assertAlmostEqual(stop.fuel_on_arrival_gallons, fuel / mpg, places=6)
        test.assertGreater(stop.gallons, 0)
        fuel += stop.gallons * mpg
        test.assertLessEqual(fuel, cap + 1e-6)
        test.assertAlmostEqual(stop.cost, stop.gallons * stop.candidate.price, places=9)
        prev_mile, prev_off = stop.candidate.route_mile, stop.candidate.off_route_miles
    fuel -= prev_off + (route_miles - prev_mile)
    test.assertGreaterEqual(fuel, reserve - 1e-6)
    test.assertAlmostEqual(plan.fuel_left_at_destination_gallons, fuel / mpg, places=6)
    test.assertAlmostEqual(plan.purchases_cost, sum(s.cost for s in plan.stops), places=9)
    # Fuel conservation: start + bought - left == used.
    test.assertAlmostEqual(
        start / mpg + plan.gallons_purchased - plan.fuel_left_at_destination_gallons, plan.fuel_used_gallons, places=6
    )


def exact_cost(stations, length, cap, start, reserve):
    """Ground truth for integer instances: try every whole-unit purchase at every station."""
    points = [(0, None)] + sorted(stations)
    best_at = [dict() for _ in points]
    best_at[0][start] = 0.0
    answer = float("inf")
    for a, (pos, price) in enumerate(points):
        for fuel, cost in best_at[a].items():
            for buy in range(0, (cap - fuel if price is not None else 0) + 1):
                level, paid = fuel + buy, cost + buy * (price or 0)
                if length - pos <= level - reserve:
                    answer = min(answer, paid)
                for b in range(a + 1, len(points)):
                    left = level - (points[b][0] - pos)
                    if left < reserve:
                        break
                    if best_at[b].get(left, float("inf")) > paid:
                        best_at[b][left] = paid
    return answer


class OptimizerTests(SimpleTestCase):
    def test_short_route_needs_no_stop(self):
        plan = plan_fuel_stops([cand(100, 3.0)], 480, max_range_miles=R, mpg=MPG)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.purchases_cost, 0)
        self.assertAlmostEqual(plan.fuel_used_gallons, 48.0)
        self.assertAlmostEqual(plan.fuel_left_at_destination_gallons, 2.0)

    def test_exactly_500_miles_needs_no_stop(self):
        self.assertEqual(plan_fuel_stops([], 500, max_range_miles=R, mpg=MPG).stops, [])

    def test_buys_only_what_is_needed_to_finish(self):
        plan = plan_fuel_stops([cand(300, 4.0)], 700, max_range_miles=R, mpg=MPG)
        stop = plan.stops[0]
        self.assertEqual(stop.fill_type, "partial")
        self.assertAlmostEqual(stop.fuel_on_arrival_gallons, 20.0)
        self.assertAlmostEqual(stop.gallons, 20.0)
        self.assertAlmostEqual(plan.purchases_cost, 80.0)
        check_plan(self, plan, 700)

    def test_fills_up_where_cheap_and_tops_up_where_expensive(self):
        # Cheap station at mile 100, expensive at mile 450: fill at 100, buy only what is needed at 450.
        plan = plan_fuel_stops([cand(100, 2.00), cand(450, 5.00)], 900, max_range_miles=R, mpg=MPG)
        self.assertEqual(
            [(s.candidate.route_mile, s.fill_type, round(s.gallons, 6)) for s in plan.stops],
            [(100, "full", 10.0), (450, "partial", 30.0)],
        )
        check_plan(self, plan, 900)

    def test_partial_fill_beats_full_fill_policy(self):
        # Filling up at the $5 station (mile 400) would cost $240 in total; buying just enough
        # to reach the $2 station (mile 600) and filling there costs $150.
        plan = plan_fuel_stops([cand(400, 5.0), cand(600, 2.0)], 1100, max_range_miles=R, mpg=MPG)
        first = plan.stops[0]
        self.assertEqual((first.candidate.route_mile, first.fill_type), (400, "partial"))
        self.assertAlmostEqual(first.gallons, 10.0)
        self.assertAlmostEqual(plan.purchases_cost, 150.0)
        check_plan(self, plan, 1100)

    def test_cheapest_station_is_not_used_when_unreachable(self):
        # The $1 station at mile 520 is out of range from the start, so the $5 station at mile 480
        # must come first; buying only enough there to reach the $1 station is cheapest.
        plan = plan_fuel_stops([cand(480, 5.0), cand(520, 1.0)], 950, max_range_miles=R, mpg=MPG)
        self.assertEqual([(s.candidate.route_mile, round(s.gallons, 6)) for s in plan.stops], [(480, 2.0), (520, 43.0)])
        check_plan(self, plan, 950)

    def test_detour_counts_against_range_and_fuel(self):
        plan = plan_fuel_stops([cand(490, 2.0, off=8)], 800, max_range_miles=R, mpg=MPG)
        self.assertAlmostEqual(plan.stops[0].miles_from_previous, 498)
        self.assertAlmostEqual(plan.detour_miles, 16)
        check_plan(self, plan, 800)
        with self.assertRaises(NoFeasibleFuelPlan):  # 495 + 8 > 500: unreachable
            plan_fuel_stops([cand(495, 2.0, off=8)], 800, max_range_miles=R, mpg=MPG)

    def test_reserve_is_never_used(self):
        cands = [cand(m, 3.0 + (m % 7) / 10) for m in range(40, 1500, 70)]
        plan = plan_fuel_stops(cands, 1500, max_range_miles=R, mpg=MPG, reserve_miles=60)
        for stop in plan.stops:
            self.assertGreaterEqual(stop.fuel_on_arrival_gallons, 6.0 - 1e-9)
        self.assertGreaterEqual(plan.fuel_left_at_destination_gallons, 6.0 - 1e-9)
        check_plan(self, plan, 1500, reserve=60)

    def test_starting_fuel_below_full(self):
        cands = [cand(50, 3.0), cand(400, 3.5)]
        plan = plan_fuel_stops(cands, 700, max_range_miles=R, mpg=MPG, start_fuel_miles=100)
        self.assertEqual(plan.stops[0].candidate.route_mile, 50)
        check_plan(self, plan, 700, start=100)
        with self.assertRaisesMessage(NoFeasibleFuelPlan, "starting fuel"):
            plan_fuel_stops([cand(150, 3.0)], 700, max_range_miles=R, mpg=MPG, start_fuel_miles=100)

    def test_no_pointless_top_up_near_the_start(self):
        # Regression: with a full tank, a pricier station at mile 6 must not be used for a tiny top-up.
        plan = plan_fuel_stops([cand(6, 3.40), cand(499, 2.90)], 900, max_range_miles=R, mpg=MPG, stop_penalty=15)
        self.assertEqual([s.candidate.route_mile for s in plan.stops], [499])

    def test_long_route_needs_multiple_stops(self):
        cands = [cand(m, 3.0 + (m % 7) / 10) for m in range(40, 2800, 60)]
        plan = plan_fuel_stops(cands, 2800, max_range_miles=R, mpg=MPG, stop_penalty=15)
        self.assertGreaterEqual(len(plan.stops), 5)
        check_plan(self, plan, 2800)

    def test_gap_larger_than_range_is_reported(self):
        with self.assertRaises(NoFeasibleFuelPlan) as ctx:
            plan_fuel_stops([cand(300, 3.0), cand(900, 3.0)], 1200, max_range_miles=R, mpg=MPG)
        self.assertEqual(ctx.exception.details["gap_start_mile"], 300)
        self.assertEqual(ctx.exception.details["gap_end_mile"], 900)

    def test_invalid_reserve(self):
        with self.assertRaises(NoFeasibleFuelPlan):
            plan_fuel_stops([], 900, max_range_miles=R, mpg=MPG, reserve_miles=500)
        with self.assertRaises(NoFeasibleFuelPlan):
            plan_fuel_stops([], 900, max_range_miles=R, mpg=MPG, start_fuel_miles=40, reserve_miles=50)

    def test_matches_exact_solution_on_random_instances(self):
        """Without detours the fill-or-just-enough DP is provably optimal: compare with exhaustive search."""
        rng = random.Random(7)
        for _ in range(250):
            cap = rng.randint(8, 20)
            length = rng.randint(cap + 1, 4 * cap)
            reserve = rng.choice([0, 0, 1, 2])
            start = rng.randint(reserve, cap)
            stations = list(
                {rng.randint(1, length - 1): round(rng.uniform(1, 5), 2) for _ in range(rng.randint(2, 10))}.items()
            )
            expected = exact_cost(stations, length, cap, start, reserve)
            cands = [cand(float(p), price, sid=i) for i, (p, price) in enumerate(stations)]
            kwargs = dict(max_range_miles=cap, mpg=1.0, start_fuel_miles=start, reserve_miles=reserve)
            if expected == float("inf"):
                with self.assertRaises(NoFeasibleFuelPlan):
                    plan_fuel_stops(cands, float(length), **kwargs)
                continue
            plan = plan_fuel_stops(cands, float(length), **kwargs)
            self.assertAlmostEqual(plan.purchases_cost, expected, places=6)
            check_plan(self, plan, length, start=start, reserve=reserve, cap=cap, mpg=1.0)

    def test_random_plans_with_detours_are_valid(self):
        rng = random.Random(11)
        for _ in range(400):
            length = rng.uniform(520, 1600)
            cands = [
                cand(rng.uniform(0, length), round(rng.uniform(2.8, 4.5), 3), off=rng.choice([0, 0, 2, 6]), sid=i)
                for i in range(rng.randint(3, 14))
            ]
            try:
                plan = plan_fuel_stops(
                    cands,
                    length,
                    max_range_miles=R,
                    mpg=MPG,
                    stop_penalty=rng.choice([0, 15]),
                    reserve_miles=rng.choice([0, 30]),
                    min_purchase_gallons=rng.choice([0, 10]),
                )
            except NoFeasibleFuelPlan:
                continue
            check_plan(self, plan, length, reserve=plan.reserve_miles)

    def test_tiny_top_up_is_rounded_up_from_the_next_stop(self):
        # Reaching the $2.90 station at mile 504 needs only 0.4 gal at mile 403 ($2.97).
        cands = [cand(403, 2.97), cand(504, 2.90)]
        exact = plan_fuel_stops(cands, 920, max_range_miles=R, mpg=MPG)
        self.assertAlmostEqual(exact.stops[0].gallons, 0.4)
        practical = plan_fuel_stops(cands, 920, max_range_miles=R, mpg=MPG, min_purchase_gallons=10)
        self.assertAlmostEqual(practical.stops[0].gallons, 10.0)
        self.assertAlmostEqual(practical.gallons_purchased, exact.gallons_purchased)
        self.assertAlmostEqual(practical.purchases_cost - exact.purchases_cost, 9.6 * (2.97 - 2.90))
        check_plan(self, practical, 920)

    def test_stop_penalty_trades_cents_for_fewer_stops(self):
        cands = [cand(400, 3.00), cand(450, 3.01), cand(700, 2.99)]
        cheapest = plan_fuel_stops(cands, 1000, max_range_miles=R, mpg=MPG, stop_penalty=0)
        fewer = plan_fuel_stops(cands, 1000, max_range_miles=R, mpg=MPG, stop_penalty=15)
        self.assertLessEqual(cheapest.purchases_cost, fewer.purchases_cost)
        self.assertLessEqual(len(fewer.stops), len(cheapest.stops))
        check_plan(self, fewer, 1000)
