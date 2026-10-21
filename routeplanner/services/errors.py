"""Domain errors. Each one maps to an HTTP status and a stable error code."""


class PlannerError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        body = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class InvalidRequest(PlannerError):
    status = 400
    code = "invalid_request"


class LocationNotFound(PlannerError):
    status = 422
    code = "location_not_found"


class LocationOutsideUSA(PlannerError):
    status = 422
    code = "location_outside_usa"


class NoRouteFound(PlannerError):
    status = 422
    code = "no_route_found"


class NoFeasibleFuelPlan(PlannerError):
    status = 422
    code = "no_feasible_fuel_plan"


class FuelDataUnavailable(PlannerError):
    status = 503
    code = "fuel_data_unavailable"


class UpstreamError(PlannerError):
    """The external map/routing service failed or returned something unusable."""

    status = 502
    code = "upstream_error"


class UpstreamRateLimited(UpstreamError):
    status = 503
    code = "upstream_rate_limited"


class UpstreamTimeout(UpstreamError):
    status = 504
    code = "upstream_timeout"
