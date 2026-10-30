"""Fuel-price data and the offline US place gazetteer.

* `parse_fuel_csv` reads the supplied price list (used by `manage.py load_fuel_prices`).
* Stations live in the database (`FuelStation`, editable in the Django admin).
  `get_stations` keeps an in-memory index of them per process and rebuilds it
  when the table changes (row count or latest `updated_at`).
* The price list has no coordinates, only City and State, so every station is
  first located at its city's centre from `data/us_places.csv` (built from public
  US ZIP-code centroids by `manage.py build_places`). The planner then moves
  stations onto the route when the route uses the highway named in their address.
"""

from __future__ import annotations

import csv
import logging
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from .errors import FuelDataUnavailable
from .geo import haversine_miles
from .states import STATE_BY_NAME, US_STATES

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = (
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
)

_PREFIXES = {"ST": "SAINT", "STE": "SAINTE", "FT": "FORT", "MT": "MOUNT", "PT": "POINT"}


def normalize_city(name: str) -> str:
    """Canonical form used to join station cities, user input and the gazetteer."""
    text = re.sub(r"[.'`]", "", name.upper().strip())
    text = re.sub(r"[-/,]", " ", text)
    tokens = text.split()
    if tokens and tokens[0] in _PREFIXES:
        tokens[0] = _PREFIXES[tokens[0]]
    return " ".join(tokens)


def normalize_state(value: str) -> str | None:
    text = value.strip().upper().replace(".", "")
    if text in US_STATES:
        return text
    return STATE_BY_NAME.get(text)


# --------------------------------------------------------------------------- places


@dataclass(frozen=True)
class Place:
    name: str
    state: str
    lat: float
    lon: float

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


class PlaceIndex:
    CELL_DEG = 1.0

    def __init__(self, places: list[Place]):
        self.places = places
        self._by_key: dict[tuple[str, str], Place] = {}
        self._by_compact_key: dict[tuple[str, str], Place] = {}
        self._grid: dict[tuple[int, int], list[Place]] = defaultdict(list)
        for place in places:
            key = normalize_city(place.name)
            self._by_key.setdefault((key, place.state), place)
            self._by_compact_key.setdefault((key.replace(" ", ""), place.state), place)
            self._grid[self._cell(place.lat, place.lon)].append(place)

    @classmethod
    def _cell(cls, lat: float, lon: float) -> tuple[int, int]:
        return (math.floor(lat / cls.CELL_DEG), math.floor(lon / cls.CELL_DEG))

    def lookup(self, city: str, state: str) -> Place | None:
        key = normalize_city(city)
        return self._by_key.get((key, state)) or self._by_compact_key.get((key.replace(" ", ""), state))

    def nearest(self, lat: float, lon: float) -> tuple[Place, float] | None:
        cy, cx = self._cell(lat, lon)
        best: tuple[Place, float] | None = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for place in self._grid.get((cy + dy, cx + dx), ()):
                    d = haversine_miles(lat, lon, place.lat, place.lon)
                    if best is None or d < best[1]:
                        best = (place, d)
        return best


@lru_cache(maxsize=1)
def get_places() -> PlaceIndex:
    return load_places(Path(settings.US_PLACES_CSV))


def load_places(path: Path) -> PlaceIndex:
    if not path.is_file():
        raise FuelDataUnavailable(f"US place gazetteer not found at {path.name}; station locations cannot be resolved.")
    places = []
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                places.append(Place(row["city"], row["state"], float(row["lat"]), float(row["lon"])))
    except (KeyError, ValueError, csv.Error) as exc:
        raise FuelDataUnavailable(f"US place gazetteer {path.name} is malformed: {exc}") from exc
    if not places:
        raise FuelDataUnavailable(f"US place gazetteer {path.name} is empty.")
    return PlaceIndex(places)


# --------------------------------------------------------------------------- price list


@dataclass(frozen=True)
class StationRecord:
    """One station as read from the price list (before it is saved)."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int
    price: float
    price_rows: int
    lat: float
    lon: float
    highways: tuple[str, ...]


def parse_fuel_csv(path: Path, places: PlaceIndex) -> tuple[list[StationRecord], dict]:
    """Read the supplied fuel-price CSV.

    * Rows outside the 50 US states + DC (the file also lists Canadian provinces)
      are excluded: trips are within the USA and those prices may use another
      currency/unit.
    * Several station IDs appear on multiple rows with different prices; the
      lowest listed retail price is used for that station.
    * Invalid prices are skipped and counted, never guessed.
    """
    from .highways import parse_address_highways

    if not path.is_file():
        raise FuelDataUnavailable(f"Fuel price file not found: {path.name}")
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise FuelDataUnavailable(f"Fuel price file is missing columns: {', '.join(missing)}")
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise FuelDataUnavailable(f"Fuel price file could not be read: {exc}") from exc

    stats = {"rows_total": len(rows), "rows_outside_us": 0, "rows_invalid_price": 0, "stations_without_location": 0}
    best: dict[int, dict] = {}
    counts: dict[int, int] = defaultdict(int)
    for row in rows:
        state = row["State"].strip().upper()
        if state not in US_STATES:
            stats["rows_outside_us"] += 1
            continue
        try:
            opis_id = int(row["OPIS Truckstop ID"])
            price = float(row["Retail Price"])
            rack_id = int(row["Rack ID"]) if row["Rack ID"].strip() else 0
        except ValueError:
            stats["rows_invalid_price"] += 1
            continue
        if not math.isfinite(price) or price <= 0:
            stats["rows_invalid_price"] += 1
            continue
        counts[opis_id] += 1
        if opis_id not in best or price < best[opis_id]["price"]:
            best[opis_id] = {"row": row, "price": price, "rack_id": rack_id, "state": state}

    records = []
    for opis_id, item in sorted(best.items()):
        row = item["row"]
        city = " ".join(row["City"].split())
        place = places.lookup(city, item["state"])
        if place is None:
            stats["stations_without_location"] += 1
            logger.warning("No location for station %s (%s, %s)", opis_id, city, item["state"])
            continue
        address = " ".join(row["Address"].split())
        records.append(
            StationRecord(
                opis_id=opis_id,
                name=" ".join(row["Truckstop Name"].split()),
                address=address,
                city=city,
                state=item["state"],
                rack_id=item["rack_id"],
                price=item["price"],
                price_rows=counts[opis_id],
                lat=place.lat,
                lon=place.lon,
                highways=parse_address_highways(address),
            )
        )
    if not records:
        raise FuelDataUnavailable("Fuel price file contains no usable US stations.")
    stats["stations_usable"] = len(records)
    stats["stations_with_highway"] = sum(1 for r in records if r.highways)
    return records, stats


# --------------------------------------------------------------------------- in-memory index


@dataclass(frozen=True)
class Station:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int
    price: float  # USD per gallon
    price_rows: int
    lat: float  # city centre
    lon: float
    highways: tuple[str, ...] = ()


@dataclass
class StationIndex:
    stations: list[Station]
    stats: dict = field(default_factory=dict)
    version: tuple = ()
    CELL_DEG = 0.5

    def __post_init__(self):
        self.grid: dict[tuple[int, int], list[Station]] = defaultdict(list)
        for station in self.stations:
            self.grid[self._cell(station.lat, station.lon)].append(station)

    @classmethod
    def _cell(cls, lat: float, lon: float) -> tuple[int, int]:
        return (math.floor(lat / cls.CELL_DEG), math.floor(lon / cls.CELL_DEG))

    def near(self, lat: float, lon: float, radius_miles: float) -> list[tuple[Station, float]]:
        pad = math.ceil(radius_miles / (self.CELL_DEG * 45.0))
        cy, cx = self._cell(lat, lon)
        result = []
        for dy in range(-pad, pad + 1):
            for dx in range(-pad, pad + 1):
                for s in self.grid.get((cy + dy, cx + dx), ()):
                    d = haversine_miles(lat, lon, s.lat, s.lon)
                    if d <= radius_miles:
                        result.append((s, d))
        return sorted(result, key=lambda item: item[1])

    def local_price(self, lat: float, lon: float, radius_miles: float = 50.0) -> dict | None:
        """Typical price near a point: median of listed stations within the radius (else the nearest)."""
        nearby = self.near(lat, lon, radius_miles)
        if nearby:
            return {
                "price_per_gallon": statistics.median(s.price for s, _ in nearby),
                "source": f"median of {len(nearby)} listed station(s) within {radius_miles:.0f} miles of the start",
            }
        if not self.stations:
            return None
        station, d = min(((s, haversine_miles(lat, lon, s.lat, s.lon)) for s in self.stations), key=lambda x: x[1])
        return {
            "price_per_gallon": station.price,
            "source": f"nearest listed station ({station.city}, {station.state}, {d:.0f} miles away)",
        }


_index_cache: dict[str, StationIndex] = {}


def get_stations() -> StationIndex:
    """In-memory index of the stations table, rebuilt whenever the table changes."""
    from django.db import DatabaseError
    from django.db.models import Count, Max

    from ..models import FuelPriceImport, FuelStation

    try:
        agg = FuelStation.objects.aggregate(n=Count("id"), latest=Max("updated_at"))
    except DatabaseError as exc:
        raise FuelDataUnavailable(
            "The fuel station table is not available. Run `python manage.py migrate` and "
            "`python manage.py load_fuel_prices`."
        ) from exc
    if not agg["n"]:
        raise FuelDataUnavailable("No fuel stations are loaded. Run `python manage.py load_fuel_prices`.")
    version = (agg["n"], agg["latest"])
    cached = _index_cache.get("index")
    if cached is not None and cached.version == version:
        return cached

    stations = [
        Station(
            opis_id=row["opis_id"],
            name=row["name"],
            address=row["address"],
            city=row["city"],
            state=row["state"],
            rack_id=row["rack_id"],
            price=float(row["price"]),
            price_rows=row["price_rows"],
            lat=row["latitude"],
            lon=row["longitude"],
            highways=tuple(h for h in row["highways"].split(",") if h),
        )
        for row in FuelStation.objects.values(
            "opis_id",
            "name",
            "address",
            "city",
            "state",
            "rack_id",
            "price",
            "price_rows",
            "latitude",
            "longitude",
            "highways",
        )
    ]
    last_import = FuelPriceImport.objects.order_by("-imported_at").first()
    stats = {"stations": len(stations), "last_import": last_import.summary() if last_import else None}
    index = StationIndex(stations=stations, stats=stats, version=version)
    _index_cache["index"] = index
    return index


def clear_caches() -> None:
    get_places.cache_clear()
    _index_cache.clear()
