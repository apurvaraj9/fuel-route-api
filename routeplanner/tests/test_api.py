"""End-to-end API tests: real price list in the test database, recorded real OSRM routes."""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.throttling import ScopedRateThrottle

from routeplanner.services import http

from .helpers import LOCMEM_CACHE, FakeHTTP, FakeResponse, load_stations, osrm_payload, timeout_error


@override_settings(CACHES=LOCMEM_CACHE, ORS_API_KEY="", ROUTE_ALTERNATIVES=3, FUEL_STOP_PENALTY_USD=15.0)
class RouteApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        load_stations()

    def setUp(self):
        cache.clear()

    def _patch(self, *responses):
        fake = FakeHTTP(*responses)
        patcher = mock.patch.object(http._session, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _get(self, start, finish, **extra):
        return self.client.get(
            "/api/route/", {"start": start, "finish": finish, **extra}, HTTP_ACCEPT="application/json"
        )

    def assert_plan_consistent(self, body):
        plan = body["fuel_plan"]
        previous = 0.0
        for stop in plan["stops"]:
            self.assertLessEqual(stop["miles_since_previous_fuel"], 500.0)
            self.assertGreaterEqual(stop["route_mile"], previous)
            previous = stop["route_mile"]
            self.assertAlmostEqual(stop["cost_usd"], stop["gallons_purchased"] * stop["price_per_gallon"], delta=0.01)
            self.assertLessEqual(stop["fuel_on_arrival_gallons"] + stop["gallons_purchased"], 50.0 + 1e-3)
        self.assertAlmostEqual(plan["purchases_usd"], sum(s["cost_usd"] for s in plan["stops"]), delta=0.02)
        start = plan["starting_fuel"]
        self.assertAlmostEqual(
            start["gallons"] + plan["total_gallons_purchased"] - plan["fuel_left_at_destination"]["gallons"],
            plan["fuel_used_gallons"],
            delta=0.01,
        )
        self.assertAlmostEqual(plan["fuel_used_gallons"], plan["total_miles_driven"] / 10, delta=0.01)
        self.assertAlmostEqual(
            plan["total_fuel_cost_usd"],
            start["value_usd"] + plan["purchases_usd"] - plan["fuel_left_at_destination"]["value_usd"],
            delta=0.03,
        )

    def test_long_route_cross_country(self):
        fake = self._patch(FakeResponse(200, osrm_payload("nyc_la")))
        response = self._get("New York, NY", "Los Angeles, CA")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        plan = body["fuel_plan"]
        self.assertEqual(body["meta"]["external_api_calls"], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertAlmostEqual(body["route"]["distance_miles"], 2794.2, delta=0.5)
        self.assertIn("I-80", body["route"]["main_highways"])
        self.assertGreater(body["route"]["stations_on_highway"], 100)
        self.assertGreaterEqual(plan["number_of_stops"], 5)
        self.assert_plan_consistent(body)
        placements = {s["location"]["placement"] for s in plan["stops"]}
        self.assertIn("highway", placements)
        kinds = [f["properties"]["kind"] for f in body["map"]["geojson"]["features"]]
        self.assertEqual(kinds.count("route"), 1)
        self.assertEqual(kinds.count("fuel_stop"), plan["number_of_stops"])
        self.assertIn("/?start=New+York", body["map"]["url"])

    def test_picks_the_cheaper_alternative_route(self):
        self._patch(FakeResponse(200, osrm_payload("chi_dal")))
        body = self._get("Chicago, IL", "Dallas, TX").json()
        alternatives = body["alternatives"]
        self.assertEqual(len(alternatives), 2)
        chosen = [a for a in alternatives if a["chosen"]]
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["total_fuel_cost_usd"], min(a["total_fuel_cost_usd"] for a in alternatives))
        self.assertEqual(body["route"]["index"], chosen[0]["index"])
        self.assertEqual(body["fuel_plan"]["total_fuel_cost_usd"], chosen[0]["total_fuel_cost_usd"])
        self.assert_plan_consistent(body)
        alt_lines = [f for f in body["map"]["geojson"]["features"] if f["properties"]["kind"] == "alternative_route"]
        self.assertEqual(len(alt_lines), 1)

    def test_fastest_preference(self):
        self._patch(FakeResponse(200, osrm_payload("chi_dal")))
        body = self._get("Chicago, IL", "Dallas, TX", route_preference="fastest").json()
        self.assertEqual(body["route"]["index"], 0)  # 17.1 h vs 17.2 h
        self.assertEqual(body["options"]["route_preference"], "fastest")

    def test_options_start_fuel_and_reserve_via_post(self):
        self._patch(FakeResponse(200, osrm_payload("chi_dal")))
        response = self.client.post(
            "/api/route/",
            json.dumps(
                {
                    "start": "Chicago, IL",
                    "finish": "Dallas, TX",
                    "start_fuel_percent": 25,
                    "reserve_miles": 50,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        plan = body["fuel_plan"]
        self.assertEqual(plan["starting_fuel"]["gallons"], 12.5)
        for stop in plan["stops"]:
            self.assertGreaterEqual(stop["fuel_on_arrival_gallons"], 5.0 - 1e-6)
        self.assertGreaterEqual(plan["fuel_left_at_destination"]["gallons"], 5.0 - 1e-6)
        self.assert_plan_consistent(body)
        self.assertIn("start_fuel_percent=25", body["map"]["url"])

    def test_short_route_costs_only_the_fuel_burned(self):
        self._patch(FakeResponse(200, osrm_payload("nyc_phl")))
        body = self._get("New York, NY", "Philadelphia, PA").json()
        plan = body["fuel_plan"]
        self.assertEqual(plan["stops"], [])
        self.assertEqual(plan["purchases_usd"], 0)
        # ~97 miles = ~9.7 gallons, valued at the local price near New York.
        expected = plan["fuel_used_gallons"] * plan["starting_fuel"]["price_per_gallon"]
        self.assertAlmostEqual(plan["total_fuel_cost_usd"], expected, delta=0.02)
        self.assertGreater(plan["total_fuel_cost_usd"], 20)
        self.assertIn("No fuel stop needed", plan["note"])

    def test_repeat_request_uses_cache(self):
        fake = self._patch(FakeResponse(200, osrm_payload("chi_dal")))
        self._get("Chicago, IL", "Dallas, TX")
        body = self._get("Chicago, IL", "Dallas, TX").json()
        self.assertEqual(body["meta"]["external_api_calls"], 0)
        self.assertEqual(len(fake.calls), 1)

    def test_validation_errors(self):
        fake = self._patch()
        cases = [
            ({}, 400, "invalid_request"),
            ({"start": "Dallas, TX"}, 400, "invalid_request"),
            ({"start": "Dallas, TX", "finish": "Dallas, TX"}, 400, "invalid_request"),
            ({"start": "Dallas, TX", "finish": "Denver, CO", "start_fuel_percent": 5}, 400, "invalid_request"),
            ({"start": "Dallas, TX", "finish": "Denver, CO", "reserve_miles": 300}, 400, "invalid_request"),
            (
                {"start": "Dallas, TX", "finish": "Denver, CO", "start_fuel_percent": 10, "reserve_miles": 60},
                400,
                "invalid_request",
            ),
            ({"start": "Dallas, TX", "finish": "Denver, CO", "route_preference": "scenic"}, 400, "invalid_request"),
            ({"start": "48.8566,2.3522", "finish": "Dallas, TX"}, 422, "location_outside_usa"),
            ({"start": "Toronto, ON", "finish": "Dallas, TX"}, 422, "location_outside_usa"),
        ]
        for params, status, code in cases:
            with self.subTest(params=params):
                response = self.client.get("/api/route/", params, HTTP_ACCEPT="application/json")
                self.assertEqual(response.status_code, status, response.content)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertTrue(response.json()["error"]["message"])
        self.assertEqual(fake.calls, [])  # bad input never reaches the external API

    def test_malformed_json_body_and_method(self):
        response = self.client.post("/api/route/", "{not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        response = self.client.post("/api/route/", "[1, 2]", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.delete("/api/route/").status_code, 405)

    def test_unknown_location(self):
        self._patch(FakeResponse(200, []))  # geocoder finds nothing
        response = self._get("Qwertyuiop Zxcv", "Dallas, TX")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "location_not_found")

    def test_upstream_failures(self):
        cases = [
            (FakeResponse(429, {}, headers={"Retry-After": "5"}), 503, "upstream_rate_limited"),
            (timeout_error(), 504, "upstream_timeout"),
            (FakeResponse(502, {}), 502, "upstream_error"),
            (FakeResponse(400, {"code": "NoRoute"}), 422, "no_route_found"),
        ]
        for response_or_error, status, code in cases:
            with self.subTest(code=code):
                cache.clear()
                self._patch(response_or_error)
                response = self._get("Chicago, IL", "Dallas, TX")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertNotIn("fuel_plan", response.json())
        cache.clear()
        self._patch(FakeResponse(429, {}, headers={"Retry-After": "5"}))
        self.assertEqual(self._get("Chicago, IL", "Dallas, TX")["Retry-After"], "5")

    def test_infeasible_plan_when_no_station_in_range(self):
        # A real route with no listed station near it must fail, not invent a plan.
        self._patch(FakeResponse(200, osrm_payload("chi_dal")))
        with mock.patch("routeplanner.services.planner.find_candidates", return_value=[]):
            response = self._get("Chicago, IL", "Dallas, TX")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "no_feasible_fuel_plan")

    def test_rate_limit(self):
        self._patch(*[FakeResponse(200, osrm_payload("chi_dal"))])
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"route": "2/min"}):
            codes = [self._get("Chicago, IL", "Dallas, TX").status_code for _ in range(3)]
            limited = self._get("Chicago, IL", "Dallas, TX")
        self.assertEqual(codes, [200, 200, 429])
        self.assertEqual(limited.json()["error"]["code"], "rate_limited")
        self.assertIn("Retry-After", limited)


@override_settings(CACHES=LOCMEM_CACHE)
class PagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        load_stations()

    def test_planner_page(self):
        response = self.client.get("/?start=Chicago,%20IL&finish=Dallas,%20TX")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fuel Route Planner")
        self.assertContains(response, "leaflet")
        # OpenStreetMap tiles are refused without a Referer header.
        self.assertEqual(response["Referrer-Policy"], "strict-origin-when-cross-origin")

    def test_legacy_map_url_redirects_to_planner(self):
        response = self.client.get("/api/route/map/?start=A&finish=B")
        self.assertRedirects(response, "/?start=A&finish=B", fetch_redirect_response=False)

    def test_api_docs_and_schema(self):
        self.assertContains(self.client.get("/api/docs/"), "swagger")
        schema = self.client.get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json").json()
        self.assertIn("/api/route/", schema["paths"])
        self.assertIn("post", schema["paths"]["/api/route/"])

    def test_index_and_health(self):
        index = self.client.get("/api/", HTTP_ACCEPT="application/json").json()
        self.assertIn("plan_route", index["endpoints"])
        health = self.client.get("/api/health/", HTTP_ACCEPT="application/json").json()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["fuel_data"]["stations"], 6626)
        self.assertEqual(health["fuel_data"]["last_import"]["rows_total"], 8151)

    def test_admin_lists_stations(self):
        user = get_user_model().objects.create_superuser("admin", "admin@example.com", "test-password-123")
        self.client.force_login(user)
        response = self.client.get("/admin/routeplanner/fuelstation/?q=Big+Cabin")
        self.assertContains(response, "WOODSHED OF BIG CABIN")


@override_settings(CACHES=LOCMEM_CACHE, ORS_API_KEY="")
class MissingFuelDataTests(TestCase):
    def test_returns_503_without_calling_external_apis(self):
        fake = FakeHTTP()
        with mock.patch.object(http._session, "request", fake):
            response = self.client.get(
                "/api/route/", {"start": "Chicago, IL", "finish": "Dallas, TX"}, HTTP_ACCEPT="application/json"
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "fuel_data_unavailable")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.client.get("/api/health/", HTTP_ACCEPT="application/json").status_code, 503)
