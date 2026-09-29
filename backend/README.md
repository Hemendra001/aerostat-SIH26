# AeroStat collection service

This service automates the six-route, five-horizon basket shown in the website. It stores validated fares in SQLite, keeps run history and runs a daily schedule in Asia/Kolkata. It runs independently of the dashboard, provided the service remains running. The published dashboard itself does not host this Python service.

## Start locally

Python 3.11 or newer is required. The service has no third-party dependencies for demo mode.

```bash
export AEROSTAT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 server.py
```

Keep the token private. Use your terminal environment to copy it into the dashboard's Service access token field; it is kept only in browser memory. A fresh random token on each restart requires reconnecting the dashboard. For a permanent server, configure a fixed generated token in your hosting provider's secret settings.

Demo mode generates explicitly simulated fares. It proves the scheduling, persistence, validation and dashboard flow, not airline website access. A daily schedule is initially paused. Saving the plan in the dashboard enables it; POST /schedule with enabled:false pauses it. The default plan is 06:00 IST. If the service starts later in the day, it performs that day's overdue run once. It does not backfill missed historical days. Run only one service process against the database.

## Connect the published dashboard

Deploy this service behind an HTTPS reverse proxy on an always-on Python host. Set AEROSTAT_HOST=0.0.0.0 when your host requires it, PORT to its assigned port, AEROSTAT_TOKEN as a secret, and AEROSTAT_ORIGIN to your dashboard's exact origin. Mount a persistent disk and set AEROSTAT_DB to a SQLite file on that disk. The default origin is the current AeroStat website.

In Automation, enter the HTTPS service URL and token, then Connect service. Select Run collection to collect the first basket, and Save collection plan to enable the daily schedule. The browser refresh cadence only polls the service; it does not replace the server scheduler. The service can collect while the browser is closed. A connected browser must remain open to refresh its display.

## Connect a real scraper

No working airline-specific adapter is supplied: airline selectors and search flows need to be verified against the current site. Add your existing verified Playwright scraper as a Python module, for example `indigo_adapter.py`, alongside server.py. Export `collect(route, departure_date)` and return an iterable of fare dictionaries matching the contract below. Install Playwright and its browser if your adapter needs them:

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
export AEROSTAT_MODE=live
export AEROSTAT_ADAPTER=indigo_adapter
python3 server.py
```

The adapter must perform the search for the supplied route (e.g. DEL-BOM) and departure date (YYYY-MM-DD), impose bounded network/browser timeouts, close browser resources, and return observed fares. Do not return sample prices or cache old prices under new timestamps. On a blocked search, unavailable route or challenge, raise an exception or return no fares. That search will be recorded as failed, while other searches continue. Do not bypass access restrictions. For multiple sources, a composite adapter can call each verified collector and combine their results; retain source labels.

Each record must include:

```json
{
  "airline": "IndiGo",
  "flight": "actual observed flight number",
  "source": "IndiGo",
  "fare": 4500,
  "departureDate": "YYYY-MM-DD",
  "observedAt": "timezone-aware ISO timestamp of collection",
  "currency": "INR",
  "cabin": "economy",
  "adults": 1,
  "stops": 0,
  "includesTaxes": true
}
```

The example fare is illustrative. The service adds IDs, route and horizon, validates the basket, removes identical source/flight/date/price duplicates within a search, and retains only positive comparable INR fares. It never substitutes demo fares on live failures. Dashboard queries return the latest completed snapshot for the selected day and mode, with incomplete route coverage left incomplete. The dashboard's fixed base fares and route weights remain illustrative; its index is not official CPI.

## API

All endpoints require Authorization: Bearer <token>. POST bodies are JSON. Browser requests also require the configured origin.

- GET /status: mode, saved schedule, running state, last 20 runs.
- GET /observations?observation_date=YYYY-MM-DD: latest completed snapshot.
- POST /run: start a non-overlapping run.
- POST /schedule: save daily schedule and enable or pause it.

Plan body: time as HH:MM, timezone as Asia/Kolkata, enabled boolean, routes as [DEL-BOM, DEL-BLR, BLR-BOM, DEL-CCU, BOM-HYD, DEL-MAA], horizons as [1,7,15,30,45]. Use JSON string quotes for route IDs.

The six routes are a proposed research basket, not a verified ranking of India's busiest routes. Replace the illustrative weights and base fares in the frontend after selecting a documented traffic dataset and base period.

## Operational limits

This is a single-process SIH prototype. Use an HTTPS proxy with request timeouts and rate limits before exposing it. Adapter calls must time out themselves; a hung adapter can stall the worker. Daily runs do not automatically retry failures. A manually triggered retry creates a separate snapshot. SQLite history is retained until you manage it; use persistent disk backups. No paid provider accounts or hosting resources have been created.

References: https://playwright.dev/python/docs/locators and https://docs.python.org/3/library/sqlite3.html
