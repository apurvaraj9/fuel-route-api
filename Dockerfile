FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_DEBUG=False \
    DATABASE_PATH=/app/data/db.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Build-time secret only for collectstatic/migrate; the real one comes from the environment at runtime.
RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --no-input \
    && DJANGO_SECRET_KEY=build-only python manage.py migrate --no-input \
    && DJANGO_SECRET_KEY=build-only python manage.py load_fuel_prices \
    && useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
CMD ["sh", "-c", "gunicorn config.wsgi --workers 2 --preload --timeout 60 --bind 0.0.0.0:${PORT:-8000}"]
