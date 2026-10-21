# Fuel Route Planner API (Django)

An API that takes a start and finish location in the USA and returns:

- the driving route (as a GeoJSON map and an interactive map page),
- cost-effective fuel stops along the route for a vehicle with a **500-mile range**,
- the **total money spent on fuel** at **10 miles per gallon**,

using the supplied fuel-price CSV and a free routing API.

**Live API:** <https://fuel-route-api-h6na.onrender.com/> (free Render instance: the first request after
15 idle minutes takes about a minute while it wakes up)

- Example: <https://fuel-route-api-h6na.onrender.com/api/route/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>
- Map: <https://fuel-route-api-h6na.onrender.com/api/route/map/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>

Built with **Django 6.1.2** (latest stable release at the time of writing), Python 3.12+.

---

## Quick start

### Windows (PowerShell, e.g. the VS Code terminal)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python manage.py test
python manage.py runserver
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py test
python manage.py runserver
```

Then open <http://127.0.0.1:8000/api/route/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>.

No API key is needed: by default routes come from the public OSRM server.
A Postman collection with demo requests is in `docs/postman_collection.json` (its `baseUrl`
variable points to the live deployment; set it to `http://127.0.0.1:8000` for a local server).

---

## API

### `GET /api/route/?start=...&finish=...`  or  `POST /api/route/`

```json
{ "start": "Chicago, IL", "finish": "Dallas, TX" }
```

Accepted location formats (all must be in the USA):

| Format | Example | External calls |
|---|---|---|
| City, state code or name | `Dallas, TX`, `Denver, Colorado` | 0 (offline gazetteer) |
| Latitude, longitude | `32.7767,-96.7970` | 0 |
| Anything else (address, landmark, ZIP) | `1600 Pennsylvania Ave NW, Washington, DC` | 1 geocoding call (cached) |

Response (abridged; values from a test run on a recorded New York → Los Angeles route):

```json
{
  "start":  {"query": "New York, NY", "label": "New York, NY", "lat": 40.75, "lon": -73.99, "source": "local_gazetteer"},
  "finish": {"query": "Los Angeles, CA", "label": "Los Angeles, CA", "...": "..."},
  "route":  {"provider": "osrm", "distance_miles": 2794.2, "duration_hours": 49.8,
             "stations_within_corridor": 460, "corridor_miles": 10.0},
  "fuel_plan": {
    "vehicle": {"max_range_miles": 500.0, "mpg": 10.0, "tank_gallons": 50.0, "starting_fuel_gallons": 50.0},
    "number_of_stops": 6,
    "stops": [
      {"stop_number": 1, "opis_truckstop_id": 72782, "name": "SHEETZ #791", "address": "I-76, Exit 57",
       "city": "North Jackson", "state": "OH", "rack_id": 573,
       "price_per_gallon": 3.0657, "route_mile": 391.2, "off_route_miles": 2.2,
       "miles_since_previous_fuel": 393.4, "fuel_on_arrival_gallons": 10.66,
       "gallons_purchased": 39.341, "fill": "full", "cost_usd": 120.61,
       "location": {"lat": 41.088, "lon": -80.8623, "precision": "city"}},
      "... 5 more stops ..."
    ],
    "total_gallons_purchased": 232.362,
    "total_fuel_cost_usd": 711.53,
    "route_miles": 2794.2, "detour_miles": 29.4, "total_miles_driven": 2823.6,
    "fuel_used_gallons": 282.362, "fuel_left_at_destination_gallons": 0.0,
    "note": "..."
  },
  "map":  {"url": "http://127.0.0.1:8000/api/route/map/?start=...&finish=...",
           "geojson": {"type": "FeatureCollection", "features": ["route line, start, finish, fuel stops"]}},
  "meta": {"external_api_calls": 1, "elapsed_ms": 300, "assumptions": ["..."]}
}
```

- `map.geojson` is the route and all stops as standard GeoJSON (paste it into <https://geojson.io> to view).
- `map.url` opens the same plan as an interactive Leaflet/OpenStreetMap page. It reuses the cached
  route, so opening it does not call the routing API again.

### `GET /api/route/map/?start=...&finish=...`
HTML map of the route with numbered fuel stops and a cost summary.

### `GET /api/health/` and `GET /`
Health check (fuel data statistics) and a short endpoint index.

### Errors

Errors return `{"error": {"code": "...", "message": "...", "details": {...}}}`:

| Status | Code | When |
|---|---|---|
| 400 | `invalid_request` | missing/blank/too long input, bad JSON, bad coordinates, same start and finish |
| 422 | `location_not_found` / `location_outside_usa` | location cannot be found in the USA, or is outside it (coordinates, or "City, ON"-style non-US regions) |
| 422 | `no_route_found` | no drivable route (e.g. to Hawaii) |
| 422 | `no_feasible_fuel_plan` | no listed station within range somewhere on the route |
| 502 | `upstream_error` | routing/geocoding service failed or returned bad data |
| 503 | `upstream_rate_limited` | free API rate limit hit (`Retry-After` is passed through) |
| 504 | `upstream_timeout` | routing/geocoding service timed out |
| 503 | `fuel_data_unavailable` | fuel price CSV missing or unusable (checked before any external call) |

A fuel cost is only returned when a real route and a feasible plan exist.

---

## How it works

1. **Locations** are resolved offline when possible (`data/us_places.csv`, ~39k US city centroids
   derived from public ZIP-code centroids). Only free-form addresses use a geocoder.
2. **Route**: one call to the routing API with the full route geometry.
   - Default: public [OSRM](https://project-osrm.org/) server (free, no key).
   - If `ORS_API_KEY` is set: [OpenRouteService](https://openrouteservice.org/) (free key).
   - Responses are cached (24 h), so repeated requests and the map page make no extra calls.
   - **Typical request: exactly 1 external API call** (`meta.external_api_calls`).
3. **Stations on the route**: the supplied CSV has no coordinates, so each station is placed at the
   centre of its city. Stations within `ROUTE_CORRIDOR_MILES` (10) of the route are candidates; each one
   gets its position along the route and its distance off the route. A grid index keeps this to a few
   milliseconds.
4. **Fuel plan** (`routeplanner/services/optimizer.py`): dynamic programming over the candidate stations.
   - Starts with a full tank (500 miles); no leg may exceed 500 miles, **including the detour** to and from
     an off-route station.
   - At each stop the tank is filled; the last stop buys only what is needed to reach the destination.
   - Minimises total fuel cost; a small per-stop penalty (`FUEL_STOP_PENALTY_USD`, default $5, never added
     to the reported cost) stops it from adding a stop just to save a few cents.
   - If no plan can keep every leg within range, it returns `no_feasible_fuel_plan` with the gap location.
   - Tests check it against brute force on random routes.
5. **Cost** = Σ gallons bought × that station's retail price (10 MPG).

Processing time after the route is received is about 0.3 s for a coast-to-coast route.

### Data handling (supplied CSV, kept unmodified in `data/`)

- 8,151 rows; 620 rows are Canadian provinces and are excluded (the trip is within the USA, and those
  prices may use a different currency/unit).
- 6,626 unique US station IDs; some IDs are listed several times with different prices, and the lowest
  listed price is used for that station.
- All 6,626 US stations are matched to a city location.

### Assumptions and limitations

- Vehicle starts with a full tank; the reported total is the fuel bought along the way. A trip under
  500 miles therefore needs no stop and costs $0 in purchases (the response says so).
- Station positions are city-level, not exact pump locations; markers show the city centre.
- Off-route distance is straight-line, not road distance.
- Routing uses a car profile from a free public service, so availability depends on that service.

---

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | — (required when `DJANGO_DEBUG` is false) | Django secret key |
| `DJANGO_DEBUG` | `False` | Debug mode for local development |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated host names |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | empty | Only needed for browser form posts |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | `False` | Set `True` behind an HTTPS proxy |
| `ORS_API_KEY` | empty | Use OpenRouteService instead of OSRM |
| `ROUTE_CORRIDOR_MILES` | `10` | Max distance of a station from the route (keep ≤ 20) |
| `FUEL_STOP_PENALTY_USD` | `5` | Per-stop penalty used only to choose the plan |
| `HTTP_CONNECT_TIMEOUT_SECONDS` / `HTTP_READ_TIMEOUT_SECONDS` | `5` / `20` | External API timeouts |
| `HTTP_USER_AGENT` | project name | Identifies the app to OSRM/Nominatim (their usage policies require it) |
| `CACHE_DIR` | system temp dir | File cache for API responses |

Secrets live only in `.env` (git-ignored) or the host's environment settings.

## Deploying (Render free web service)

1. Push this repository to GitHub.
2. In Render: **New → Web Service**, connect the repository, choose the **Free** instance type, and set:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn config.wsgi --workers 2 --preload --timeout 60 --bind 0.0.0.0:$PORT`
   - Health check path: `/api/health/`
   - Environment variables: `DJANGO_SECRET_KEY` (a long random value), `DJANGO_DEBUG=False`,
     `PYTHON_VERSION=3.12.3`, optionally `ORS_API_KEY`.
   (`render.yaml` contains the same settings as a Blueprint.)
3. The public host name is picked up automatically from Render's `RENDER_EXTERNAL_HOSTNAME`, and HTTPS
   proxy headers are trusted when it is set.

The API has no database, sessions or cookies, so CSRF middleware is not used; Render terminates HTTPS.
Free Render services sleep after 15 minutes without traffic; the first request afterwards takes about a
minute while the service starts.

## Tests

```bash
python manage.py test
```

57 tests cover geometry, CSV loading and validation, location parsing, routing clients (OSRM and
OpenRouteService), timeouts, rate limits, invalid responses, the optimizer (range limit, detours,
reachability, brute-force optimality), and the API end to end. Tests never call external services:
routing responses are replaced by **real routes recorded from OSRM** (`routeplanner/tests/fixtures`).

## Project layout

```
config/                     Django settings, URLs, WSGI
routeplanner/
  views.py                  API endpoints and map page
  services/
    data.py                 CSV + gazetteer loading
    geocoding.py            start/finish -> coordinates
    routing.py              OSRM / OpenRouteService client (1 call, cached)
    geo.py                  distances, polylines, route matching
    optimizer.py            fuel-stop dynamic programming
    planner.py              puts it all together, builds the response
  templates/routeplanner/map.html
  management/commands/build_places.py   rebuilds data/us_places.csv
  tests/
data/
  fuel-prices-for-be-assessment.csv     supplied file (unchanged)
  us_places.csv                         US city centroids
docs/postman_collection.json
```

## Attribution

Routes © OSRM / OpenRouteService, map data © OpenStreetMap contributors (ODbL).
City centroids derived from public US ZIP-code data (`zipcodes` package).
