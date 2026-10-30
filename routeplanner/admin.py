from django.contrib import admin

from .models import FuelPriceImport, FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "price", "highways", "updated_at")
    list_filter = ("state",)
    search_fields = ("opis_id", "name", "city", "address", "highways")
    ordering = ("state", "city", "name")
    readonly_fields = ("updated_at",)
    list_per_page = 50


@admin.register(FuelPriceImport)
class FuelPriceImportAdmin(admin.ModelAdmin):
    list_display = ("imported_at", "source_file", "stats")
    readonly_fields = ("source_file", "imported_at", "stats")

    def has_add_permission(self, request):
        return False
