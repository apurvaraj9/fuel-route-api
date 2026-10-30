from django.test import SimpleTestCase

from routeplanner.services.highways import parse_address_highways, parse_route_refs


class AddressHighwayTests(SimpleTestCase):
    def test_real_address_formats(self):
        cases = {
            "I-44, EXIT 283 & US-69": ("I-44", "US-69"),
            "I-94, EXIT 143 & US-12 & SR-21": ("I-94", "US-12", "SR-21"),
            "I-81, EXIT 273 & SR-703/SR-292": ("I-81", "SR-703", "SR-292"),
            "I-39, EXIT 111/US-51 & SR-72": ("I-39", "US-51", "SR-72"),
            "I-29 & I-80, EXIT 3": ("I-29", "I-80"),
            "HIGHWAY 11/17": ("SR-11", "SR-17"),
            "HWY 84 & CR-466": ("SR-84", "CR-466"),
            "US-169 & US-169 BUS/US-20 BUS": ("US-169", "US-20"),
            "I-35,  EXIT 271": ("I-35",),
            "1234 MAIN STREET": (),
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                self.assertEqual(parse_address_highways(address), expected)

    def test_exit_and_mile_marker_numbers_are_not_roads(self):
        self.assertEqual(parse_address_highways("I-70, EXIT 28 MM 30"), ("I-70",))


class RouteRefTests(SimpleTestCase):
    def test_osrm_and_ors_formats(self):
        self.assertEqual(parse_route_refs("I 80; US 6"), {"I-80", "US-6"})
        self.assertEqual(parse_route_refs("I 55; I 64; US 40; IL 3"), {"I-55", "I-64", "US-40", "SR-3"})
        self.assertEqual(parse_route_refs("Interstate 80, I 80"), {"I-80"})
        self.assertEqual(parse_route_refs("I 80 Express"), {"I-80"})
        self.assertEqual(parse_route_refs("Exit 11"), frozenset())
        self.assertEqual(parse_route_refs("NJTP"), frozenset())
        self.assertEqual(parse_route_refs(None), frozenset())
