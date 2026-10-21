from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.core.cache import cache

from routeplanner.services import http
from routeplanner.services.errors import (
    InvalidRequest, LocationNotFound, LocationOutsideUSA, UpstreamRateLimited, UpstreamTimeout,
)
from routeplanner.services.geocoding import resolve_location

from .helpers import LOCMEM_CACHE, FakeHTTP, FakeResponse, timeout_error


@override_settings(CACHES=LOCMEM_CACHE, ORS_API_KEY="")
class LocalResolutionTests(SimpleTestCase):
    """Common inputs are resolved offline: zero external API calls."""

    def setUp(self):
        cache.clear()
        self.http = FakeHTTP()  # any external call would fail the test
        patcher = mock.patch.object(http._session, "request", self.http)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_city_state_code(self):
        loc, calls = resolve_location("Dallas, TX", "start")
        self.assertEqual((loc.label, loc.source, calls), ("Dallas, TX", "local_gazetteer", 0))
        self.assertAlmostEqual(loc.lat, 32.78, delta=0.2)
        self.assertAlmostEqual(loc.lon, -96.80, delta=0.2)

    def test_city_full_state_name_and_country_suffix(self):
        loc, calls = resolve_location("new york, New York, USA", "start")
        self.assertEqual(loc.label, "New York, NY")
        self.assertEqual(calls, 0)

    def test_city_state_without_comma_and_zip(self):
        self.assertEqual(resolve_location("Denver CO", "start")[0].label, "Denver, CO")
        self.assertEqual(resolve_location("Austin, TX 78701", "start")[0].label, "Austin, TX")

    def test_coordinates(self):
        loc, calls = resolve_location("34.0522, -118.2437", "finish")
        self.assertEqual((loc.source, calls), ("coordinates", 0))
        self.assertIn("Los Angeles", loc.label)

    def test_coordinates_outside_usa(self):
        for text in ("48.8566,2.3522", "19.4326,-99.1332", "0,0"):  # Paris, Mexico City, ocean
            with self.subTest(text=text), self.assertRaises(LocationOutsideUSA):
                resolve_location(text, "start")

    def test_non_us_region_rejected_without_geocoding(self):
        # Found in live testing: "Toronto, ON" used to be fuzzy-matched to "Toronto Court, Indianapolis".
        for text in ("Toronto, ON", "Vancouver, BC", "Montreal, Quebec", "Monterrey, Mexico", "Calgary, Alberta, Canada"):
            with self.subTest(text=text), self.assertRaises(LocationOutsideUSA):
                resolve_location(text, "start")
        self.assertEqual(self.http.calls, [])

    def test_coordinates_out_of_range(self):
        with self.assertRaises(InvalidRequest):
            resolve_location("95,200", "start")

    def test_missing_or_bad_types(self):
        for value in (None, "", "   ", 42, ["Dallas"], "x" * 201):
            with self.subTest(value=value), self.assertRaises(InvalidRequest):
                resolve_location(value, "start")
        self.assertEqual(self.http.calls, [])


@override_settings(CACHES=LOCMEM_CACHE, ORS_API_KEY="")
class ExternalGeocoderTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def _patch(self, *responses):
        fake = FakeHTTP(*responses)
        patcher = mock.patch.object(http._session, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_address_uses_nominatim_once_then_cache(self):
        fake = self._patch(FakeResponse(200, [{"lat": "38.8977", "lon": "-77.0365", "display_name": "White House"}]))
        loc, calls = resolve_location("1600 Pennsylvania Ave NW, Washington, DC 20500", "start")
        self.assertEqual((loc.source, calls, loc.label), ("nominatim", 1, "White House"))
        self.assertEqual(fake.calls[0]["params"]["countrycodes"], "us")
        loc2, calls2 = resolve_location("1600 Pennsylvania Ave NW, Washington, DC 20500", "start")
        self.assertEqual((calls2, loc2.lat), (0, loc.lat))
        self.assertEqual(len(fake.calls), 1)

    def test_not_found(self):
        self._patch(FakeResponse(200, []))
        with self.assertRaises(LocationNotFound):
            resolve_location("Nowhereville Xyz", "finish")

    def test_rate_limit_and_timeout(self):
        self._patch(FakeResponse(429, {}, headers={"Retry-After": "2"}), timeout_error())
        with self.assertRaises(UpstreamRateLimited):
            resolve_location("Some Address 1", "start")
        with self.assertRaises(UpstreamTimeout):
            resolve_location("Some Address 2", "start")

    @override_settings(ORS_API_KEY="test-key")
    def test_openrouteservice_geocoder_when_key_set(self):
        fake = self._patch(FakeResponse(200, {"features": [{
            "geometry": {"coordinates": [-77.0365, 38.8977]},
            "properties": {"label": "White House, Washington, DC, USA", "country_a": "USA"},
        }]}))
        loc, calls = resolve_location("White House", "start")
        self.assertEqual((loc.source, calls), ("openrouteservice_geocoder", 1))
        self.assertEqual(fake.calls[0]["headers"]["Authorization"], "test-key")
        self.assertNotIn("api_key", fake.calls[0]["params"])  # key never placed in the URL
