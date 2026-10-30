"""Request validation and response documentation for the route-planning API."""

from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        max_length=200,
        trim_whitespace=True,
        help_text='Start in the USA: "City, ST", "latitude,longitude" or an address.',
    )
    finish = serializers.CharField(max_length=200, trim_whitespace=True, help_text="Destination in the USA.")
    start_fuel_percent = serializers.FloatField(
        default=100,
        min_value=10,
        max_value=100,
        help_text="How full the tank is at the start (10-100). Default 100.",
    )
    reserve_miles = serializers.FloatField(
        default=0,
        min_value=0,
        max_value=200,
        help_text="Never plan to have less than this many miles of fuel left (0-200). Default 0.",
    )
    route_preference = serializers.ChoiceField(
        choices=["cheapest", "fastest"],
        default="cheapest",
        help_text="cheapest: route option with the lowest total fuel cost; fastest: the quickest option.",
    )
    alternatives = serializers.BooleanField(
        default=True,
        help_text="Ask the routing service for alternative routes (still one API call).",
    )

    def validate(self, attrs):
        if attrs["reserve_miles"] >= 500 * attrs["start_fuel_percent"] / 100:
            raise serializers.ValidationError({"reserve_miles": "The reserve must be less than the starting fuel."})
        return attrs


# ----------------------------------------------------------------- response shape (for the API docs)


class LocationSerializer(serializers.Serializer):
    query = serializers.CharField()
    label = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    source = serializers.CharField(help_text="local_gazetteer, coordinates, nominatim or openrouteservice_geocoder")


class StopLocationSerializer(serializers.Serializer):
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    placement = serializers.ChoiceField(
        choices=["highway", "city"], help_text="highway: on the route's highway; city: city centre"
    )


class FuelStopSerializer(serializers.Serializer):
    stop_number = serializers.IntegerField()
    opis_truckstop_id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    rack_id = serializers.IntegerField()
    price_per_gallon = serializers.FloatField()
    route_mile = serializers.FloatField()
    off_route_miles = serializers.FloatField()
    miles_since_previous_fuel = serializers.FloatField()
    fuel_on_arrival_gallons = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    fill = serializers.ChoiceField(choices=["full", "partial"])
    cost_usd = serializers.FloatField()
    location = StopLocationSerializer()


class FuelPlanSerializer(serializers.Serializer):
    vehicle = serializers.DictField()
    starting_fuel = serializers.DictField()
    number_of_stops = serializers.IntegerField()
    stops = FuelStopSerializer(many=True)
    total_gallons_purchased = serializers.FloatField()
    purchases_usd = serializers.FloatField(help_text="Money paid at the stops along the way.")
    fuel_used_gallons = serializers.FloatField()
    fuel_left_at_destination = serializers.DictField()
    total_fuel_cost_usd = serializers.FloatField(help_text="Cost of the fuel burned on the trip.")
    route_miles = serializers.FloatField()
    detour_miles = serializers.FloatField()
    total_miles_driven = serializers.FloatField()
    note = serializers.CharField()


class RouteSummarySerializer(serializers.Serializer):
    provider = serializers.CharField()
    index = serializers.IntegerField()
    distance_miles = serializers.FloatField()
    duration_hours = serializers.FloatField()
    main_highways = serializers.ListField(child=serializers.CharField())
    stations_considered = serializers.IntegerField()
    stations_on_highway = serializers.IntegerField()
    corridor_miles = serializers.FloatField()


class RoutePlanSerializer(serializers.Serializer):
    start = LocationSerializer()
    finish = LocationSerializer()
    options = serializers.DictField()
    route = RouteSummarySerializer()
    alternatives = serializers.ListField(child=serializers.DictField())
    fuel_plan = FuelPlanSerializer()
    map = serializers.DictField(help_text="url: interactive map page; geojson: route, alternatives and stops.")
    meta = serializers.DictField()


class ErrorSerializer(serializers.Serializer):
    error = serializers.DictField(help_text="{code, message, details}")
