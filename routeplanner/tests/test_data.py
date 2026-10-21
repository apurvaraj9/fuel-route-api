import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from routeplanner.services.data import get_places, load_stations, normalize_city, normalize_state
from routeplanner.services.errors import FuelDataUnavailable

HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"


class SuppliedCsvTests(SimpleTestCase):
    """Checks against the real CSV supplied with the assessment."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.index = load_stations(Path(settings.FUEL_PRICES_CSV), get_places())

    def test_all_rows_accounted_for(self):
        stats = self.index.stats
        self.assertEqual(stats["rows_total"], 8151)
        self.assertEqual(stats["rows_outside_us"], 620)  # Canadian provinces
        self.assertEqual(stats["rows_invalid_price"], 0)
        self.assertEqual(stats["stations_without_location"], 0)
        self.assertEqual(stats["stations_usable"], 6626)  # unique US OPIS IDs

    def test_duplicate_station_ids_use_lowest_listed_price(self):
        station = next(s for s in self.index.stations if s.opis_id == 105)  # listed 6 times in the CSV
        self.assertEqual(station.price_rows, 6)
        self.assertAlmostEqual(station.price, 3.269)

    def test_station_is_located_at_its_city(self):
        station = next(s for s in self.index.stations if s.opis_id == 7)  # Big Cabin, OK
        self.assertAlmostEqual(station.lat, 36.54, delta=0.15)
        self.assertAlmostEqual(station.lon, -95.22, delta=0.15)

    def test_no_canadian_stations(self):
        self.assertFalse(any(s.state in {"ON", "AB", "BC", "QC"} for s in self.index.stations))


class CsvValidationTests(SimpleTestCase):
    def _write(self, content: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        tmp.write(content)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_missing_file(self):
        with self.assertRaises(FuelDataUnavailable):
            load_stations(Path("does-not-exist.csv"), get_places())

    def test_missing_columns(self):
        path = self._write("id,name\n1,x\n")
        with self.assertRaisesMessage(FuelDataUnavailable, "missing columns"):
            load_stations(path, get_places())

    def test_invalid_prices_are_skipped_not_invented(self):
        path = self._write(HEADER + "1,A,I-1,Dallas,TX,1,abc\n2,B,I-1,Dallas,TX,1,-3\n3,C,I-1,Dallas,TX,1,3.10\n")
        index = load_stations(path, get_places())
        self.assertEqual(index.stats["rows_invalid_price"], 2)
        self.assertEqual([s.opis_id for s in index.stations], [3])

    def test_file_without_usable_rows(self):
        path = self._write(HEADER + "1,A,HWY 1,Toronto,ON,1,3.10\n")
        with self.assertRaisesMessage(FuelDataUnavailable, "no usable US stations"):
            load_stations(path, get_places())


class NormalizationTests(SimpleTestCase):
    def test_city_names(self):
        self.assertEqual(normalize_city("  St. Louis "), "SAINT LOUIS")
        self.assertEqual(normalize_city("Ft Worth"), "FORT WORTH")
        self.assertEqual(normalize_city("Winston-Salem"), "WINSTON SALEM")

    def test_states(self):
        self.assertEqual(normalize_state("tx"), "TX")
        self.assertEqual(normalize_state("Texas"), "TX")
        self.assertIsNone(normalize_state("ON"))

    def test_city_spelling_variants_resolve(self):
        places = get_places()
        self.assertIsNotNone(places.lookup("De Forest", "WI"))  # gazetteer spells it "Deforest"
        self.assertIsNotNone(places.lookup("St Louis", "MO"))
