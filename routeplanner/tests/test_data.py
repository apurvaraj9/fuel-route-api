import io
import tempfile
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from routeplanner.models import FuelPriceImport, FuelStation
from routeplanner.services.data import get_places, get_stations, normalize_city, normalize_state, parse_fuel_csv
from routeplanner.services.errors import FuelDataUnavailable

HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"


def write_csv(test, content: str) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    tmp.write(content)
    tmp.close()
    test.addCleanup(Path(tmp.name).unlink)
    return Path(tmp.name)


class SuppliedCsvTests(SimpleTestCase):
    """Checks against the real price list supplied with the assessment."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.records, cls.stats = parse_fuel_csv(Path(settings.FUEL_PRICES_CSV), get_places())

    def test_all_rows_accounted_for(self):
        self.assertEqual(self.stats["rows_total"], 8151)
        self.assertEqual(self.stats["rows_outside_us"], 620)  # Canadian provinces
        self.assertEqual(self.stats["rows_invalid_price"], 0)
        self.assertEqual(self.stats["stations_without_location"], 0)
        self.assertEqual(self.stats["stations_usable"], 6626)  # unique US OPIS IDs
        self.assertGreater(self.stats["stations_with_highway"], 6000)

    def test_duplicate_station_ids_use_lowest_listed_price(self):
        station = next(s for s in self.records if s.opis_id == 105)  # listed 6 times in the CSV
        self.assertEqual(station.price_rows, 6)
        self.assertAlmostEqual(station.price, 3.269)

    def test_station_location_and_highways(self):
        station = next(s for s in self.records if s.opis_id == 7)  # Big Cabin, OK: "I-44, EXIT 283 & US-69"
        self.assertAlmostEqual(station.lat, 36.54, delta=0.15)
        self.assertAlmostEqual(station.lon, -95.22, delta=0.15)
        self.assertEqual(station.highways, ("I-44", "US-69"))


class CsvValidationTests(SimpleTestCase):
    def test_missing_file(self):
        with self.assertRaises(FuelDataUnavailable):
            parse_fuel_csv(Path("does-not-exist.csv"), get_places())

    def test_missing_columns(self):
        with self.assertRaisesMessage(FuelDataUnavailable, "missing columns"):
            parse_fuel_csv(write_csv(self, "id,name\n1,x\n"), get_places())

    def test_invalid_prices_are_skipped_not_invented(self):
        path = write_csv(self, HEADER + "1,A,I-1,Dallas,TX,1,abc\n2,B,I-1,Dallas,TX,1,-3\n3,C,I-1,Dallas,TX,1,3.10\n")
        records, stats = parse_fuel_csv(path, get_places())
        self.assertEqual(stats["rows_invalid_price"], 2)
        self.assertEqual([r.opis_id for r in records], [3])

    def test_file_without_usable_rows(self):
        with self.assertRaisesMessage(FuelDataUnavailable, "no usable US stations"):
            parse_fuel_csv(write_csv(self, HEADER + "1,A,HWY 1,Toronto,ON,1,3.10\n"), get_places())


class LoadCommandTests(TestCase):
    def test_load_update_and_remove(self):
        first = write_csv(self, HEADER + '1,A,"I-35, EXIT 1",Dallas,TX,10,3.10\n2,B,US-69,Tulsa,OK,11,3.20\n')
        call_command("load_fuel_prices", str(first), stdout=io.StringIO())
        self.assertEqual(FuelStation.objects.count(), 2)
        self.assertEqual(FuelStation.objects.get(opis_id=1).highways, "I-35")

        second = write_csv(self, HEADER + '1,A,"I-35, EXIT 1",Dallas,TX,10,2.95\n3,C,I-40,Amarillo,TX,12,3.00\n')
        out = io.StringIO()
        call_command("load_fuel_prices", str(second), stdout=out)
        self.assertIn("1 new, 1 updated, 1 removed", out.getvalue())
        self.assertEqual(FuelStation.objects.get(opis_id=1).price, Decimal("2.95000"))
        self.assertFalse(FuelStation.objects.filter(opis_id=2).exists())
        self.assertEqual(FuelPriceImport.objects.count(), 2)

    def test_bad_file_is_a_command_error(self):
        with self.assertRaises(CommandError):
            call_command("load_fuel_prices", "does-not-exist.csv", stdout=io.StringIO())


class StationIndexTests(TestCase):
    def test_index_follows_database_changes(self):
        call_command(
            "load_fuel_prices", str(write_csv(self, HEADER + "1,A,I-35,Dallas,TX,1,3.10\n")), stdout=io.StringIO()
        )
        self.assertAlmostEqual(get_stations().stations[0].price, 3.10)
        station = FuelStation.objects.get(opis_id=1)
        station.price = Decimal("2.50")
        station.save()  # e.g. an edit in the Django admin
        self.assertAlmostEqual(get_stations().stations[0].price, 2.50)

    def test_empty_table(self):
        with self.assertRaisesMessage(FuelDataUnavailable, "load_fuel_prices"):
            get_stations()


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
