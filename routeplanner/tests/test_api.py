"""End-to-end API tests. The routing API is replaced by recorded real OSRM routes."""
import json
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from routeplanner.services import data, http

from .helpers import LOCMEM_CACHE, FakeHTTP, FakeResponse, osrm_payload, timeout_error


@override_settings(CACHES=LOCMEM_CACHE, ORS_API_KEY="")
class RouteApiTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def _patch(self, *responses):
        fake = FakeHTTP(*responses)
        patcher = mock.patch.object(http._session, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _get(self, start, finish, url="/api/route/"):
        return self.client.get(url, {"start": start, "finish": finish})

    def test_long_route_cross_country(self):
        fake = self._patch(FakeResponse(200, osrm_payload("nyc_la")))
        response = self._get("New York, NY", "Los Angeles, CA")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        plan = body["fuel_plan"]

        self.assertEqual(body["meta"]["external_api_calls"], 1)  # one routing call, no geocoding calls
        self.assertEqual(len(fake.calls), 1)
        self.assertAlmostEqual(body["route"]["distance_miles"], 2794.2, delta=0.5)
        self.assertGreaterEqual(plan["number_of_stops"], 5)  # 2794 miles needs at least 5 refuels

        # Every leg fits in the 500-mile range; money and fuel add up.
        previous = 0.0
        for stop in plan["stops"]:
            self.assertLessEqual(stop["miles_since_previous_fuel"], 500.0)
            self.assertGreater(stop["route_mile"], previous)
            previous = stop["route_mile"]
            self.assertAlmostEqual(stop["cost_usd"], stop["gallons_purchased"] * stop["price_per_gallon"], delta=0.01)
            self.assertLessEqual(stop["gallons_purchased"], 50.0)
        self.assertAlmostEqual(plan["total_fuel_cost_usd"], sum(s["cost_usd"] for s in plan["stops"]), places=2)
        self.assertAlmostEqual(50 + plan["total_gallons_purchased"] - plan["fuel_left_at_destination_gallons"],
                               plan["fuel_used_gallons"], delta=0.01)
        self.assertAlmostEqual(plan["fuel_used_gallons"], plan["total_miles_driven"] / 10, delta=0.01)

        # Map: GeoJSON with the route, start, finish and every stop, plus an HTML map link.
        kinds = [f["properties"]["kind"] for f in body["map"]["geojson"]["features"]]
        self.assertEqual(kinds.count("route"), 1)
        self.assertEqual(kinds.count("fuel_stop"), plan["number_of_stops"])
        self.assertIn("/api/route/map/?start=New+York", body["map"]["url"])

    def test_medium_route_one_stop_and_post_json(self):
        self._patch(FakeResponse(200, osrm_payload("chi_dal")))
        response = self.client.post("/api/route/", json.dumps({"start": "Chicago, IL", "finish": "Dallas, TX"}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        plan = response.json()["fuel_plan"]
        self.assertEqual(plan["number_of_stops"], 1)  # 967 miles
        self.assertGreater(plan["total_fuel_cost_usd"], 0)
        self.assertEqual(plan["stops"][0]["fill"], "partial")

    def test_short_route_no_stops(self):
        self._patch(FakeResponse(200, osrm_payload("nyc_phl")))
        body = self._get("New York, NY", "Philadelphia, PA").json()
        self.assertEqual(body["fuel_plan"]["stops"], [])
        self.assertEqual(body["fuel_plan"]["total_fuel_cost_usd"], 0)
        self.assertIn("No fuel purchase needed", body["fuel_plan"]["note"])

    def test_repeat_request_uses_cache(self):
        fake = self._patch(FakeResponse(200, osrm_payload("chi_dal")))
        self._get("Chicago, IL", "Dallas, TX")
        body = self._get("Chicago, IL", "Dallas, TX").json()
        self.assertEqual(body["meta"]["external_api_calls"], 0)
        self.assertEqual(len(fake.calls), 1)

    def test_map_page(self):
        self._patch(FakeResponse(200, osrm_payload("chi_dal")))
        response = self._get("Chicago, IL", "Dallas, TX", url="/api/route/map/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "leaflet")
        self.assertContains(response, 'id="route-geojson"')
        self.assertContains(response, "Total fuel cost")
        # OpenStreetMap tiles are refused without a Referer header.
        self.assertEqual(response["Referrer-Policy"], "strict-origin-when-cross-origin")

    def test_validation_errors(self):
        fake = self._patch()
        cases = [
            ({}, 400, "invalid_request"),
            ({"start": "Dallas, TX"}, 400, "invalid_request"),
            ({"start": "Dallas, TX", "finish": "Dallas, TX"}, 400, "invalid_request"),
            ({"start": "48.8566,2.3522", "finish": "Dallas, TX"}, 422, "location_outside_usa"),
        ]
        for params, status, code in cases:
            with self.subTest(params=params):
                response = self.client.get("/api/route/", params)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["error"]["code"], code)
        self.assertEqual(fake.calls, [])  # bad input never reaches the external API

    def test_malformed_json_body_and_method(self):
        response = self.client.post("/api/route/", "{not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)
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

    def test_infeasible_plan_when_no_stations_reachable(self):
        # A real 967-mile route with an empty station list: must fail, not invent a plan.
        self._patch(FakeResponse(200, osrm_payload("chi_dal")))
        empty = data.StationIndex(stations=[], stats={})
        with mock.patch("routeplanner.services.planner.get_stations", return_value=empty):
            response = self._get("Chicago, IL", "Dallas, TX")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "no_feasible_fuel_plan")

    def test_index_and_health(self):
        self.assertIn("plan_route", self.client.get("/").json()["endpoints"])
        health = self.client.get("/api/health/").json()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["fuel_data"]["stations_usable"], 6626)


@override_settings(CACHES=LOCMEM_CACHE, ORS_API_KEY="", FUEL_PRICES_CSV=Path("missing.csv"))
class MissingFuelDataTests(SimpleTestCase):
    def setUp(self):
        data.clear_caches()
        self.addCleanup(data.clear_caches)

    def test_returns_503_without_calling_external_apis(self):
        fake = FakeHTTP()
        with mock.patch.object(http._session, "request", fake):
            response = self.client.get("/api/route/", {"start": "Chicago, IL", "finish": "Dallas, TX"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "fuel_data_unavailable")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.client.get("/api/health/").status_code, 503)
