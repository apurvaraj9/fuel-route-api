"""Load (or reload) the fuel price list into the database.

    python manage.py load_fuel_prices                 # the bundled CSV
    python manage.py load_fuel_prices path/to/new.csv # a newer price list

Stations are matched by OPIS ID: existing ones are updated, new ones added.
Stations missing from the new file are removed unless --keep-missing is given.
"""

from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routeplanner.models import FuelPriceImport, FuelStation
from routeplanner.services.data import clear_caches, get_places, parse_fuel_csv
from routeplanner.services.errors import FuelDataUnavailable


class Command(BaseCommand):
    help = "Load the fuel price CSV into the FuelStation table."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", nargs="?", default=str(settings.FUEL_PRICES_CSV))
        parser.add_argument("--keep-missing", action="store_true", help="Keep stations that are not in the new file.")

    def handle(self, *args, **options):
        path = Path(options["csv_path"])
        try:
            records, stats = parse_fuel_csv(path, get_places())
        except FuelDataUnavailable as exc:
            raise CommandError(exc.message) from exc

        fields = [
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
        ]
        with transaction.atomic():
            existing = {s.opis_id: s for s in FuelStation.objects.all()}
            to_create, to_update = [], []
            for r in records:
                values = {
                    "name": r.name[:200],
                    "address": r.address[:300],
                    "city": r.city[:100],
                    "state": r.state,
                    "rack_id": r.rack_id,
                    "price": Decimal(str(r.price)).quantize(Decimal("0.00001")),
                    "price_rows": r.price_rows,
                    "latitude": r.lat,
                    "longitude": r.lon,
                    "highways": ",".join(r.highways)[:200],
                }
                station = existing.pop(r.opis_id, None)
                if station is None:
                    to_create.append(FuelStation(opis_id=r.opis_id, **values))
                elif any(getattr(station, k) != v for k, v in values.items()):
                    for k, v in values.items():
                        setattr(station, k, v)
                    to_update.append(station)
            FuelStation.objects.bulk_create(to_create, batch_size=500)
            if to_update:
                # bulk_update skips auto_now, so set the change time explicitly.
                from django.utils import timezone

                now = timezone.now()
                for station in to_update:
                    station.updated_at = now
                FuelStation.objects.bulk_update(to_update, fields + ["updated_at"], batch_size=500)
            removed = 0
            if existing and not options["keep_missing"]:
                removed, _ = FuelStation.objects.filter(opis_id__in=list(existing)).delete()
            stats.update(created=len(to_create), updated=len(to_update), removed=removed)
            FuelPriceImport.objects.create(source_file=path.name, stats=stats)
        clear_caches()
        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {stats['stations_usable']} US stations from {path.name} "
                f"({stats['created']} new, {stats['updated']} updated, {removed} removed; "
                f"{stats['rows_outside_us']} non-US rows skipped, "
                f"{stats['rows_invalid_price']} invalid prices skipped)."
            )
        )
