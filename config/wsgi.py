"""WSGI entry point (used by gunicorn in production)."""
import logging
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()

# Load the fuel-price CSV and the place gazetteer once at startup so the first
# API request is fast. A data problem is logged here and reported by the API
# as a 503 instead of preventing the server from starting.
try:
    from routeplanner.services.data import get_places, get_stations

    get_places()
    get_stations()
except Exception:  # pragma: no cover - startup diagnostics only
    logging.getLogger(__name__).exception("Could not preload route planner data")
