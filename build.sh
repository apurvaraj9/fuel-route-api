#!/usr/bin/env bash
# Build step for Render (and any Linux host): install, collect static files,
# create the database and load the fuel price list.
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate --no-input
python manage.py load_fuel_prices

# Optional admin account, only when all three variables are set.
if [[ -n "${DJANGO_SUPERUSER_USERNAME:-}" && -n "${DJANGO_SUPERUSER_PASSWORD:-}" && -n "${DJANGO_SUPERUSER_EMAIL:-}" ]]; then
  python manage.py createsuperuser --no-input || echo "Admin user already exists"
fi
