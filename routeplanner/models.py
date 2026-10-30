from django.db import models


class FuelStation(models.Model):
    """A truck stop from the supplied price list (one row per OPIS station ID)."""

    opis_id = models.PositiveIntegerField("OPIS truckstop ID", unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(default=0)
    price = models.DecimalField("retail price (USD/gal)", max_digits=8, decimal_places=5)
    price_rows = models.PositiveSmallIntegerField(
        default=1, help_text="How many rows the price list had for this station (the lowest price is used)."
    )
    latitude = models.FloatField(help_text="City centre (the price list has no coordinates).")
    longitude = models.FloatField()
    highways = models.CharField(
        max_length=200,
        blank=True,
        help_text="Highways named in the address, e.g. 'I-44,US-69'. Used to place the station on the route.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state})"


class FuelPriceImport(models.Model):
    """One run of `manage.py load_fuel_prices`."""

    source_file = models.CharField(max_length=300)
    imported_at = models.DateTimeField(auto_now_add=True)
    stats = models.JSONField(default=dict)

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self) -> str:
        return f"{self.source_file} at {self.imported_at:%Y-%m-%d %H:%M}"

    def summary(self) -> dict:
        return {"source_file": self.source_file, "imported_at": self.imported_at.isoformat(), **self.stats}
