# AeroStat FastAPI Collection & Econometric Intelligence Service

Production-grade, asynchronous FastAPI backend designed for **Smart India Hackathon 2026 (SIH26056)** — augmenting India's Consumer Price Index (CPI) through high-frequency airfare intelligence.

---

## ⚡ Quick Start

### 1. Install Dependencies
```bash
cd backend
pip install -r requirements.txt
```

### 2. Run the Server
You can launch the FastAPI server using either **Python** or **Uvicorn**:

```bash
# Option A: Direct Python runner
python server.py

# Option B: Uvicorn with auto-reload for development
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Once running, access the **interactive Swagger documentation**:
👉 **[http://localhost:8000/docs](http://localhost:8000/docs)**  
👉 **[http://localhost:8000/redoc](http://localhost:8000/redoc)**

---

## 🛠 Features & Architecture

* **FastAPI + Pydantic v2:** Automatic request/response validation, OpenAPI 3.1 specification, and type safety.
* **Axiomatic Jevons Index Engine (`/indices`):** Computes geometric price relatives across all 6 routes and 5 horizons ($T+1, T+7, T+15, T+30, T+45$) aggregated with **DGCA Table 5.01 passenger traffic weights**.
* **Automated Background Scheduler:** Runs unattended daily collection runs at `06:00 IST` with collision locking.
* **Dual Execution Modes:**
  * `demo` (Default): Realistic synthetic yield curves simulating advance purchase decay.
  * `live`: Modular Playwright browser automation adapter interface.
* **CORS & Authentication:** Permissive CORS for cross-origin frontend dashboard connection with optional Bearer token security.

---

## 📡 API Reference

| Method | Endpoint | Description |
| :---: | :--- | :--- |
| **GET** | `/` | Service health check, IST timestamp, and API metadata |
| **GET** | `/docs` | Interactive Swagger UI (OpenAPI 3.1) |
| **GET** | `/status` | Active collection state, scheduler configuration, and 20 recent run logs |
| **GET** | `/observations` | Query verified, deduplicated fare records by `observation_date` |
| **POST** | `/schedule` | Update daily collection schedule (time, enabled flag, route basket) |
| **POST** | `/run` | Trigger an immediate asynchronous collection run across all 30 route-horizon pairs |
| **GET** | `/indices` | Compute real-time Jevons price relatives and national weighted indices |

---

## 🔒 Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `AEROSTAT_HOST` | `0.0.0.0` | Server host binding |
| `PORT` | `8000` | Port for the HTTP/ASGI server |
| `AEROSTAT_MODE` | `demo` | `demo` (synthetic yields) or `live` (custom scraping adapter) |
| `AEROSTAT_TOKEN` | *(empty)* | Optional Bearer token for production authorization |
| `AEROSTAT_DB` | `aerostat.sqlite3` | SQLite database file path |
| `AEROSTAT_ADAPTER`| *(empty)* | Python module exporting `collect(route, departure_date)` for live mode |

---

## 🧪 Testing the API

```bash
# Health check
curl http://localhost:8000/

# Check status
curl http://localhost:8000/status

# Trigger collection
curl -X POST http://localhost:8000/run

# View computed Jevons indices
curl http://localhost:8000/indices
```
