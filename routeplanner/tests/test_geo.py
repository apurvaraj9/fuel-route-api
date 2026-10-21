from django.test import SimpleTestCase

from routeplanner.services.geo import RouteLine, decode_polyline, encode_polyline, haversine_miles, simplify


class GeoTests(SimpleTestCase):
    def test_haversine_known_distance(self):
        # New York City Hall to Philadelphia City Hall is about 80 miles in a straight line.
        self.assertAlmostEqual(haversine_miles(40.7128, -74.0060, 39.9526, -75.1652), 80.6, delta=1.0)

    def test_decode_google_reference_polyline(self):
        coords = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(coords, [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])

    def test_encode_decode_roundtrip(self):
        coords = [(41.87811, -87.6298), (32.77672, -96.79699), (34.05223, -118.24368)]
        self.assertEqual(decode_polyline(encode_polyline(coords)), coords)

    def test_malformed_polyline_raises(self):
        with self.assertRaises(ValueError):
            decode_polyline("_p~iF~ps|U_")

    def test_simplify_keeps_endpoints_and_drops_collinear_points(self):
        line = [(0.0, float(i)) for i in range(100)]
        self.assertEqual(simplify(line, 0.001), [(0.0, 0.0), (0.0, 99.0)])

    def test_route_line_matching_and_scaling(self):
        # A straight east-west line along latitude 40 of about 53 miles.
        line = RouteLine([(40.0, -100.0 + i * 0.01) for i in range(101)], total_miles=60.0)
        self.assertAlmostEqual(line.length_miles, 60.0)
        match = line.match(40.05, -99.5, max_miles=10)  # ~3.5 miles north of the line, halfway along
        self.assertIsNotNone(match)
        self.assertAlmostEqual(match.route_mile, 30.0, delta=1.0)
        self.assertAlmostEqual(match.off_route_miles, 3.45, delta=0.2)
        self.assertIsNone(line.match(40.5, -99.5, max_miles=10))  # ~35 miles away
