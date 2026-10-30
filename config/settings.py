"""Django settings for the fuel route planner.

All deploy-specific values come from environment variables (or a local `.env`
file, see `.env.example`). No secrets are stored in source control.
"""

import os
import sys
import tempfile
import warnings
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

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "routeplanner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

# SQLite holds the fuel stations (loaded from the CSV by `manage.py load_fuel_prices`).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DATABASE_PATH", str(BASE_DIR / "db.sqlite3")),
    }
}

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
STATIC_ROOT = BASE_DIR / "staticfiles"
if DEBUG or "test" in sys.argv[1:2]:
    # Static files are served from the app folders in development; there is no collectstatic output yet.
    warnings.filterwarnings("ignore", message=r"No directory at: .*staticfiles")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG or "test" in sys.argv[1:2]
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# The map page loads OpenStreetMap tiles, whose usage policy requires a Referer
# header. Django's default ("same-origin") sends none to other sites, so send
# just the origin (no path or query string).
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if env_bool("DJANGO_SECURE_PROXY_SSL_HEADER", bool(RENDER_EXTERNAL_HOSTNAME)):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# ---------------------------------------------------------------------------
# Django REST Framework + OpenAPI docs
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    # Public, read-only style API: no logins, so no session/CSRF handling on the API.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "routeplanner.api.exception_handler",
    # Route planning calls a free external routing service, so limit it per client.
    "DEFAULT_THROTTLE_RATES": {"route": os.environ.get("ROUTE_RATE_LIMIT", "30/min")},
    "NUM_PROXIES": int(os.environ["NUM_PROXIES"]) if os.environ.get("NUM_PROXIES") else None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Fuel Route Planner API",
    "DESCRIPTION": (
        "Plans a driving route between two US locations and the cheapest fuel stops along it "
        "(500-mile range, 10 MPG) using the supplied truck-stop price list."
    ),
    "VERSION": "2.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

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

# A station whose address names a highway that the route uses is placed on that
# highway when the route passes within this many miles of the station's city.
HIGHWAY_SNAP_MILES = env_float("HIGHWAY_SNAP_MILES", 20.0)

# How many route options to ask the routing service for (in the same single call).
ROUTE_ALTERNATIVES = int(os.environ.get("ROUTE_ALTERNATIVES", 3))

# Small cost (USD) charged per extra stop while choosing the plan only, so the
# optimizer does not add a stop to save a few cents. It is never included in
# the reported fuel cost. Set to 0 for the pure minimum-fuel-cost plan.
FUEL_STOP_PENALTY_USD = env_float("FUEL_STOP_PENALTY_USD", 15.0)

# Smallest purchase at a stop (except the last one), see optimizer._round_up_small_purchases.
MIN_PURCHASE_GALLONS = env_float("MIN_PURCHASE_GALLONS", 10.0)

# Routing: OpenRouteService when ORS_API_KEY is set, otherwise the public OSRM server.
ORS_API_KEY = os.environ.get("ORS_API_KEY", "").strip()
ORS_BASE_URL = os.environ.get("ORS_BASE_URL", "https://api.openrouteservice.org").rstrip("/")
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
NOMINATIM_BASE_URL = os.environ.get("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org").rstrip("/")

HTTP_CONNECT_TIMEOUT_SECONDS = env_float("HTTP_CONNECT_TIMEOUT_SECONDS", 5.0)
HTTP_READ_TIMEOUT_SECONDS = env_float("HTTP_READ_TIMEOUT_SECONDS", 20.0)
HTTP_USER_AGENT = os.environ.get(
    "HTTP_USER_AGENT", "fuel-route-api/2.0 (+https://github.com/apurvaraj9/fuel-route-api)"
)

TESTING = "test" in sys.argv[1:2]
if TESTING:
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["route"] = "1000/min"

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
