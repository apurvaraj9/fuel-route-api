from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from routeplanner.services import http
from routeplanner.services.errors import NoRouteFound, UpstreamError, UpstreamRateLimited, UpstreamTimeout
from routeplanner.services.routing import get_routes

from .helpers import LOCMEM_CACHE, FakeHTTP, FakeResponse, connection_error, osrm_payload, timeout_error

CHI, DAL = (41.85, -87.6779), (32.7673, -96.7776)


class PatchedHTTP:
    def _patch(self, *responses):
        fake = FakeHTTP(*responses)
        patcher = mock.patch.object(http._session, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


@override_settings(CACHES=LOCMEM_CACHE, ORS_API_KEY="", ROUTE_ALTERNATIVES=3)
class OsrmTests(PatchedHTTP, SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_one_call_returns_alternatives_with_highways_and_is_cached(self):
        fake = self._patch(FakeResponse(200, osrm_payload("chi_dal")))
        routes, calls = get_routes(CHI, DAL)
        self.assertEqual((calls, len(routes)), (1, 2))
        self.assertAlmostEqual(routes[0].distance_miles, 967.6, delta=0.5)
        self.assertAlmostEqual(routes[1].distance_miles, 921.7, delta=0.5)
        self.assertIn(frozenset({"I-30", "US-70"}), [refs for _, refs in routes[0].segments])
        params = fake.calls[0]["params"]
        self.assertEqual((params["overview"], params["steps"], params["alternatives"]), ("full", "true", "3"))
        self.assertIn("/route/v1/driving/-87.677900,41.850000;-96.777600,32.767300", fake.calls[0]["url"])
        self.assertIsNotNone(fake.calls[0]["timeout"])
        again, calls_again = get_routes(CHI, DAL)
        self.assertEqual((calls_again, len(again)), (0, 2))
        self.assertEqual(len(fake.calls), 1)

    def test_without_alternatives(self):
        fake = self._patch(FakeResponse(200, osrm_payload("chi_dal", routes=1)))
        routes, _ = get_routes(CHI, DAL, alternatives=False)
        self.assertEqual(len(routes), 1)
        self.assertEqual(fake.calls[0]["params"]["alternatives"], "false")

    def test_no_route(self):
        self._patch(FakeResponse(400, {"code": "NoRoute", "message": "Impossible route"}))
        with self.assertRaises(NoRouteFound):
            get_routes(CHI, (21.3069, -157.8583))

    def test_failures(self):
        cases = [
            (FakeResponse(429, {}), UpstreamRateLimited),
            (timeout_error(), UpstreamTimeout),
            (connection_error(), UpstreamError),
            (FakeResponse(503, {}), UpstreamError),
            (FakeResponse(200, text="<html>"), UpstreamError),
            (FakeResponse(200, {"code": "Ok", "routes": []}), UpstreamError),
            (
                FakeResponse(200, {"code": "Ok", "routes": [{"geometry": "_p~iF", "distance": 1, "duration": 1}]}),
                UpstreamError,
            ),
        ]
        for response, error in cases:
            with self.subTest(error=error.__name__):
                cache.clear()
                self._patch(response)
                with self.assertRaises(error):
                    get_routes(CHI, DAL)


@override_settings(CACHES=LOCMEM_CACHE, ORS_API_KEY="secret-test-key", ROUTE_ALTERNATIVES=3)
class OpenRouteServiceTests(PatchedHTTP, SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_success_with_step_names(self):
        osrm = osrm_payload("chi_dal", routes=1)["routes"][0]
        fake = self._patch(
            FakeResponse(
                200,
                {
                    "routes": [
                        {
                            "geometry": osrm["geometry"],
                            "summary": {"distance": osrm["distance"], "duration": 54000},
                            "segments": [
                                {
                                    "steps": [
                                        {"distance": 1000, "name": "Lake Shore Drive"},
                                        {"distance": 600000, "name": "Interstate 57, I 57"},
                                    ]
                                }
                            ],
                        }
                    ]
                },
            )
        )
        routes, calls = get_routes(CHI, DAL)
        self.assertEqual((calls, routes[0].provider), (1, "openrouteservice"))
        self.assertEqual(routes[0].segments[1][1], {"I-57"})
        call = fake.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["Authorization"], "secret-test-key")
        self.assertEqual(call["json"]["coordinates"], [[-87.6779, 41.85], [-96.7776, 32.7673]])
        self.assertNotIn("alternative_routes", call["json"])  # ORS only offers alternatives under 100 km

    def test_short_trip_requests_alternatives(self):
        osrm = osrm_payload("nyc_phl", routes=1)["routes"][0]
        fake = self._patch(
            FakeResponse(
                200, {"routes": [{"geometry": osrm["geometry"], "summary": {"distance": 50000, "duration": 3000}}]}
            )
        )
        get_routes((40.0, -75.0), (40.3, -75.2))
        self.assertEqual(fake.calls[0]["json"]["alternative_routes"]["target_count"], 3)

    def test_route_not_found_and_bad_key(self):
        self._patch(FakeResponse(404, {"error": {"code": 2009, "message": "Route could not be found"}}))
        with self.assertRaises(NoRouteFound):
            get_routes(CHI, DAL)
        self._patch(FakeResponse(403, {"error": "Access to this API has been disallowed"}))
        with self.assertRaisesMessage(UpstreamError, "API key"):
            get_routes(CHI, DAL)
