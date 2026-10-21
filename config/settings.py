"""Django settings for the fuel route planner API.

All deploy-specific values come from environment variables (or a local `.env`
file, see `.env.example`). No secrets are stored in source control.
"""
import os
import sys
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "insecure-dev-only-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY (or DJANGO_DEBUG=True for local development).")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
# Render sets this to the service's public host name (e.g. my-app.onrender.com).
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# The API needs no database, sessions or admin: only the app itself.
INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "routeplanner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

DATABASES = {}

# Caches external geocoding/routing responses so repeated requests (and the map
# page for a route that was just planned) do not call the free APIs again.
# The file cache is shared by all worker processes on one machine.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": os.environ.get("CACHE_DIR", str(Path(tempfile.gettempdir()) / "fuel_route_cache")),
        "TIMEOUT": int(os.environ.get("CACHE_TIMEOUT_SECONDS", 60 * 60 * 24)),
        "OPTIONS": {"MAX_ENTRIES": 2000},
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if env_bool("DJANGO_SECURE_PROXY_SSL_HEADER", bool(RENDER_EXTERNAL_HOSTNAME)):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# ---------------------------------------------------------------------------
# Fuel route planner settings
# ---------------------------------------------------------------------------
FUEL_PRICES_CSV = Path(os.environ.get("FUEL_PRICES_CSV", BASE_DIR / "data" / "fuel-prices-for-be-assessment.csv"))
US_PLACES_CSV = Path(os.environ.get("US_PLACES_CSV", BASE_DIR / "data" / "us_places.csv"))

VEHICLE_MAX_RANGE_MILES = 500.0  # fixed by the assessment
VEHICLE_MPG = 10.0  # fixed by the assessment

# A station counts as "along the route" when its location is within this many
# straight-line miles of the route line. The detour (there and back) is added
# to the fuel used and to the range check.
ROUTE_CORRIDOR_MILES = env_float("ROUTE_CORRIDOR_MILES", 10.0)

# Small cost (USD) charged per extra stop while choosing the plan only, so the
# optimizer does not add a stop to save a few cents. It is never included in
# the reported fuel cost. Set to 0 for the pure minimum-fuel-cost plan.
FUEL_STOP_PENALTY_USD = env_float("FUEL_STOP_PENALTY_USD", 5.0)

# Routing: OpenRouteService when ORS_API_KEY is set, otherwise the public OSRM server.
ORS_API_KEY = os.environ.get("ORS_API_KEY", "").strip()
ORS_BASE_URL = os.environ.get("ORS_BASE_URL", "https://api.openrouteservice.org").rstrip("/")
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
NOMINATIM_BASE_URL = os.environ.get("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org").rstrip("/")

HTTP_CONNECT_TIMEOUT_SECONDS = env_float("HTTP_CONNECT_TIMEOUT_SECONDS", 5.0)
HTTP_READ_TIMEOUT_SECONDS = env_float("HTTP_READ_TIMEOUT_SECONDS", 20.0)
HTTP_USER_AGENT = os.environ.get(
    "HTTP_USER_AGENT", "fuel-route-api/1.0 (Django coding assessment; https://github.com/)"
)

TESTING = "test" in sys.argv[1:2]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
if TESTING:
    # Tests deliberately trigger 4xx/5xx responses and upstream failures; keep the output readable.
    LOGGING["root"]["level"] = "CRITICAL"
    LOGGING["loggers"] = {"django.request": {"level": "CRITICAL"}}
