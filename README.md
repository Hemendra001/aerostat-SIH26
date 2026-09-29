# AeroStat local edition

## Run in VS Code

1. Extract the ZIP and open the `aerostat-local` folder in VS Code.
2. Install Node.js if it is not already installed.
3. Open Terminal > New Terminal and run `npm start`.
4. Open http://127.0.0.1:4173 in your browser.

No npm install, API key or build step is required. Stop the server with Ctrl+C.

Alternative with Python: `python3 -m http.server 4173 --bind 127.0.0.1 --directory dist`.
You can also open `dist/index.html` with the VS Code Live Server extension.

## Edit the website

- `dist/index.html`: page structure and methodology
- `dist/style.css`: colors, spacing and typography
- `dist/app.js`: demo observations, filters, charts, export and navigation
- `dist/features.js`: feature cards, descriptions and flowcharts
- `dist/automation.js`: collector connection controls
- `dist/aerostat-logo.png`: supplied logo, unchanged
- `dist/aviation-runway.jpg`: airport photograph

Refresh your browser after saving changes. Images and scripts are local. The original design loads DM Sans and IBM Plex Mono from Google Fonts when online, with system font fallbacks. The demo works without external accounts.

## Data and backend

Fares and route weights are illustrative until a collection API is connected. The optional Python collection service and its own setup instructions are in `backend/`. It currently uses SQLite and a demo adapter; live airline scraping and the proposed PostgreSQL/FastAPI architecture are not implemented by this design update. API connections still require HTTPS and appropriate CORS settings. Running the local frontend does not start the backend or a collection schedule.

This package contains no hosting configuration or deployment command. The hosted website was not updated.

## Asset reference

Airport photograph: Yucel M on Unsplash, https://unsplash.com/photos/white-passenger-plane-on-runway-Ga2msh7voXU (Unsplash License). The photo credit is retained here only, not displayed in the website. The AeroStat logo was supplied by the project owner.
