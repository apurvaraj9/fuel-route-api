"""Regenerate data/us_places.csv (city/state -> lat/lon) from US ZIP-code centroids.

Only needed if you want to rebuild the bundled gazetteer:

    pip install -r requirements-dev.txt
    python manage.py build_places

A city's location is the median of the centroids of all ZIP codes whose
primary or "acceptable" city name is that city. This is done offline once, so
the API never geocodes fuel stations at request time.
"""
import csv
import statistics
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routeplanner.services.data import US_STATES, normalize_city


class Command(BaseCommand):
    help = "Build data/us_places.csv from the `zipcodes` package (dev dependency)."

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(settings.US_PLACES_CSV))

    def handle(self, *args, **options):
        try:
            import zipcodes
        except ImportError as exc:  # pragma: no cover
            raise CommandError("Install dev requirements first: pip install -r requirements-dev.txt") from exc

        primary: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
        alias: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
        display: dict[tuple[str, str], str] = {}
        for z in zipcodes.filter_by():
            if z["state"] not in US_STATES or z["zip_code_type"] == "MILITARY":
                continue
            lat, lon = float(z["lat"]), float(z["long"])
            if lat == 0 and lon == 0:
                continue
            key = (normalize_city(z["city"]), z["state"])
            primary[key].append((lat, lon))
            display.setdefault(key, z["city"])
            for name in z.get("acceptable_cities") or []:
                akey = (normalize_city(name), z["state"])
                alias[akey].append((lat, lon))
                display.setdefault(akey, name.title())

        rows = []
        for key in sorted(set(primary) | set(alias)):
            points = primary.get(key) or alias[key]
            rows.append({
                "city": display[key],
                "state": key[1],
                "lat": round(statistics.median(p[0] for p in points), 5),
                "lon": round(statistics.median(p[1] for p in points), 5),
            })

        output = Path(options["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["city", "state", "lat", "lon"])
            writer.writeheader()
            writer.writerows(rows)
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(rows)} places to {output}"))
