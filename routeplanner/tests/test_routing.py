from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from routeplanner.services import http
from routeplanner.services.errors import NoRouteFound, UpstreamError, UpstreamRateLimited, UpstreamTimeout
from routeplanner.services.routing import get_route

from .helpers import LOCMEM_CACHE, FakeHTTP, FakeResponse, connection_error, osrm_payload, timeout_error

NYC, LA = (40.7128, -74.006), (34.0522, -118.2437)


@override_settings(CACHES=LOCMEM_CACHE, ORS_API_KEY="")
class OsrmTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def _patch(self, *responses):
        fake = FakeHTTP(*responses)
        patcher = mock.patch.object(http._session, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_single_call_and_cache(self):
        fake = self._patch(FakeResponse(200, osrm_payload("nyc_la")))
        route, calls = get_route(NYC, LA)
        self.assertEqual((calls, route.provider), (1, "osrm"))
        self.assertAlmostEqual(route.distance_miles, 2794.2, delta=0.5)
        self.assertIn("/route/v1/driving/-74.006000,40.712800;-118.243700,34.052200", fake.calls[0]["url"])
        self.assertEqual(fake.calls[0]["params"]["overview"], "full")
        self.assertIsNotNone(fake.calls[0]["timeout"])
        route2, calls2 = get_route(NYC, LA)
        self.assertEqual((calls2, len(route2.coords)), (0, len(route.coords)))
        self.assertEqual(len(fake.calls), 1)

    def test_no_route(self):
        self._patch(FakeResponse(400, {"code": "NoRoute", "message": "Impossible route"}))
        with self.assertRaises(NoRouteFound):
            get_route(NYC, (21.3069, -157.8583))  # Honolulu

    def test_failures(self):
        cases = [
            (FakeResponse(429, {}), UpstreamRateLimited),
            (timeout_error(), UpstreamTimeout),
            (connection_error(), UpstreamError),
            (FakeResponse(503, {}), UpstreamError),
            (FakeResponse(200, text="<html>"), UpstreamError),
            (FakeResponse(200, {"code": "Ok", "routes": []}), UpstreamError),
            (FakeResponse(200, {"code": "Ok", "routes": [{"geometry": "_p~iF", "distance": 1, "duration": 1}]}),
             UpstreamError),
        ]
        for response, error in cases:
            with self.subTest(error=error.__name__):
                cache.clear()
                self._patch(response)
                with self.assertRaises(error):
                    get_route(NYC, LA)


@override_settings(CACHES=LOCMEM_CACHE, ORS_API_KEY="secret-test-key")
class OpenRouteServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def _patch(self, *responses):
        fake = FakeHTTP(*responses)
        patcher = mock.patch.object(http._session, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_success(self):
        osrm = osrm_payload("chi_dal")["routes"][0]
        fake = self._patch(FakeResponse(200, {"routes": [{
            "geometry": osrm["geometry"], "summary": {"distance": osrm["distance"], "duration": 54000},
        }]}))
        route, calls = get_route((41.8781, -87.6298), (32.7767, -96.797))
        self.assertEqual((calls, route.provider), (1, "openrouteservice"))
        self.assertAlmostEqual(route.distance_miles, 966.9, delta=0.5)
        call = fake.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["Authorization"], "secret-test-key")
        self.assertEqual(call["json"]["coordinates"], [[-87.6298, 41.8781], [-96.797, 32.7767]])

    def test_route_not_found_and_bad_key(self):
        self._patch(FakeResponse(404, {"error": {"code": 2009, "message": "Route could not be found"}}))
        with self.assertRaises(NoRouteFound):
            get_route(NYC, LA)
        self._patch(FakeResponse(403, {"error": "Access to this API has been disallowed"}))
        with self.assertRaisesMessage(UpstreamError, "API key"):
            get_route(NYC, LA)
