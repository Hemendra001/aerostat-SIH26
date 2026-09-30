"""
AeroStat FastAPI Collection Service & Econometric Intelligence API.
Built for Smart India Hackathon (SIH26056) - MoSPI CPI Augmentation.
Python 3.11+ with FastAPI, Pydantic, and Uvicorn.
"""

import concurrent.futures
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import hmac
import importlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional
import uuid
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Header, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")
ROUTES = ["DEL-BOM", "DEL-BLR", "BLR-BOM", "DEL-CCU", "BOM-HYD", "DEL-MAA"]
HORIZONS = [1, 7, 15, 30, 45]

ROUTE_WEIGHTS = {
    "DEL-BOM": 0.24,
    "DEL-BLR": 0.19,
    "BLR-BOM": 0.16,
    "DEL-CCU": 0.15,
    "BOM-HYD": 0.13,
    "DEL-MAA": 0.13,
}

BASE_FARES = {
    "DEL-BOM": 5000,
    "DEL-BLR": 6500,
    "BLR-BOM": 4200,
    "DEL-CCU": 5400,
    "BOM-HYD": 3800,
    "DEL-MAA": 6200,
}

DB = os.getenv("AEROSTAT_DB", "aerostat.sqlite3")
TOKEN = os.getenv("AEROSTAT_TOKEN", "")
ORIGIN = os.getenv("AEROSTAT_ORIGIN", "*")
MODE = os.getenv("AEROSTAT_MODE", "demo")
ADAPTER = os.getenv("AEROSTAT_ADAPTER", "")

LOCK = threading.Lock()
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2)
SCHEDULER_STOP_EVENT = threading.Event()

# ---------------------------------------------------------------------------
# Database Utilities
# ---------------------------------------------------------------------------
def now() -> datetime:
    return datetime.now(IST)

def get_db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            started TEXT,
            mode TEXT,
            status TEXT,
            count INTEGER,
            detail TEXT,
            scheduled_day TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS fares (
            id TEXT PRIMARY KEY,
            day TEXT,
            mode TEXT,
            run_id TEXT,
            payload TEXT
        );
        CREATE INDEX IF NOT EXISTS fares_day ON fares(day, mode);
        """)
        c.execute(
            "INSERT OR IGNORE INTO settings VALUES (1, ?)",
            (
                json.dumps({
                    "time": "06:00",
                    "enabled": False,
                    "timezone": "Asia/Kolkata",
                    "routes": ROUTES,
                    "horizons": HORIZONS,
                }),
            ),
        )
        c.execute(
            "UPDATE runs SET status='interrupted', detail='Service restarted before run completed' WHERE status='running'"
        )

def get_config() -> Dict[str, Any]:
    with get_db() as c:
        row = c.execute("SELECT value FROM settings WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}

# ---------------------------------------------------------------------------
# Pydantic Schemas (Request / Response validation & OpenAPI Documentation)
# ---------------------------------------------------------------------------
class SchedulePlan(BaseModel):
    time: str = Field(..., pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", description="Daily collection time in HH:MM (24h IST)")
    enabled: bool = Field(False, description="Whether automated daily collection is enabled")
    timezone: str = Field("Asia/Kolkata", description="Timezone for scheduled operations")
    routes: List[str] = Field(default_factory=lambda: list(ROUTES), description="List of monitored route pairs")
    horizons: List[int] = Field(default_factory=lambda: list(HORIZONS), description="Advance booking horizons in days")

class FareObservation(BaseModel):
    id: str
    route: str
    horizon: int
    airline: str
    flight: str
    source: str
    fare: float
    departureDate: str
    observedAt: str

class RunHistoryItem(BaseModel):
    started: str
    mode: str
    status: str
    count: int
    detail: str

class SystemStatusResponse(BaseModel):
    mode: str
    running: bool
    schedule: Dict[str, Any]
    runs: List[RunHistoryItem]

class ObservationsResponse(BaseModel):
    mode: str
    observation_date: str
    count: int
    observations: List[Dict[str, Any]]

class JevonsRouteHorizonIndex(BaseModel):
    route: str
    horizon: int
    matched_count: int
    base_fare: float
    current_geometric_mean: float
    price_relative: float
    index_value: float

class NationalHorizonIndex(BaseModel):
    horizon: int
    index_value: float
    route_count: int

class EconometricIndicesResponse(BaseModel):
    observation_date: str
    mode: str
    methodology: str
    national_indices: Dict[str, float]
    route_horizon_indices: List[JevonsRouteHorizonIndex]

# ---------------------------------------------------------------------------
# Econometric Fare Validation & Collection Logic
# ---------------------------------------------------------------------------
def validate_fare_record(raw: Dict[str, Any], route: str, horizon: int, day: str, run_id: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Adapter result must be a JSON object.")
    
    fare = raw.get("fare")
    if type(fare) not in (int, float) or not math.isfinite(fare) or fare <= 0:
        raise ValueError("Fare must be a positive finite number.")
    
    for key in ("airline", "flight", "source"):
        val = raw.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"Missing or empty required field: {key}")
            
    expected_dep = (datetime.fromisoformat(day) + timedelta(days=horizon)).date().isoformat()
    if raw.get("departureDate") != expected_dep:
        raise ValueError(f"Departure date mismatch. Expected {expected_dep}, received {raw.get('departureDate')}")
        
    if (
        raw.get("currency") != "INR"
        or raw.get("cabin") != "economy"
        or raw.get("adults") != 1
        or raw.get("stops") != 0
        or raw.get("includesTaxes") is not True
    ):
        raise ValueError("Fare record does not conform to the Standardized Consumer Fare basket.")
        
    stamp_str = raw["observedAt"].replace("Z", "+00:00")
    stamp = datetime.fromisoformat(stamp_str)
    if stamp.tzinfo is None or stamp.astimezone(IST).date().isoformat() != day or stamp > now() + timedelta(minutes=5):
        raise ValueError("Invalid observation timestamp.")
        
    return {
        "id": str(uuid.uuid4()),
        "route": route,
        "horizon": horizon,
        "airline": raw["airline"].strip(),
        "flight": raw["flight"].strip(),
        "source": raw["source"].strip(),
        "fare": round(float(fare), 2),
        "departureDate": expected_dep,
        "observedAt": raw["observedAt"],
    }

def demo_collect_adapter(route: str, departure_date: str) -> List[Dict[str, Any]]:
    """Simulates realistic dynamic fare quotes with booking horizon decay curve."""
    base = BASE_FARES.get(route, 5000)
    horizon = (datetime.fromisoformat(departure_date).date() - now().date()).days
    multipliers = {1: 1.45, 7: 1.12, 15: 0.98, 30: 0.85, 45: 0.88}
    multiplier = multipliers.get(horizon, 1.0)
    
    results = []
    demo_airlines = [
        ("IndiGo", "6E-204", 0.0),
        ("Air India", "AI-805", 0.04),
        ("Akasa Air", "QP-112", -0.03),
    ]
    for airline, flight, variance in demo_airlines:
        calculated_fare = round(base * (multiplier + variance))
        results.append({
            "airline": airline,
            "flight": flight,
            "source": airline,
            "fare": calculated_fare,
            "departureDate": departure_date,
            "observedAt": now().isoformat(),
            "currency": "INR",
            "cabin": "economy",
            "adults": 1,
            "stops": 0,
            "includesTaxes": True,
        })
    return results

def execute_collection_run(run_id: str, day: str, plan_dict: Dict[str, Any]):
    count = 0
    errors = []
    successful_searches = 0
    total_searches = len(plan_dict.get("routes", ROUTES)) * len(plan_dict.get("horizons", HORIZONS))
    
    try:
        if MODE == "demo":
            adapter_fn = demo_collect_adapter
        else:
            if not ADAPTER:
                raise RuntimeError("Live mode configured but AEROSTAT_ADAPTER module is missing.")
            adapter_fn = importlib.import_module(ADAPTER).collect

        for route in plan_dict.get("routes", ROUTES):
            for h in plan_dict.get("horizons", HORIZONS):
                if now().date().isoformat() != day:
                    errors.append("Run crossed observation-day boundary; remaining searches skipped.")
                    raise RuntimeError("Day boundary crossed")
                
                dep = (datetime.fromisoformat(day) + timedelta(days=h)).date().isoformat()
                try:
                    raw_rows = list(adapter_fn(route, dep))
                    if not raw_rows:
                        raise ValueError("No available fare offers returned.")
                    
                    seen_offers = set()
                    cleaned_observations = []
                    for raw in raw_rows:
                        valid_item = validate_fare_record(raw, route, h, day, run_id)
                        dedup_key = (valid_item["source"], valid_item["flight"], valid_item["departureDate"], valid_item["fare"])
                        if dedup_key not in seen_offers:
                            cleaned_observations.append(valid_item)
                            seen_offers.add(dedup_key)
                    
                    with get_db() as c:
                        for item in cleaned_observations:
                            c.execute(
                                "INSERT INTO fares VALUES (?, ?, ?, ?, ?)",
                                (item["id"], day, MODE, run_id, json.dumps(item)),
                            )
                    count += len(cleaned_observations)
                    successful_searches += 1
                except Exception as ex:
                    errors.append(f"{route} T+{h}: {str(ex)[:60]}")
                
                if MODE == "live":
                    time.sleep(2)
                    
    except Exception as general_err:
        errors.append(f"Collection interrupted: {str(general_err)[:60]}")
    finally:
        status_str = "succeeded" if successful_searches == total_searches else ("partial" if count > 0 else "failed")
        detail_msg = f"{successful_searches}/{total_searches} searches completed. " + (
            "; ".join(errors[:3]) if errors else "All fare records successfully validated."
        )
        with get_db() as c:
            c.execute(
                "UPDATE runs SET status=?, count=?, detail=? WHERE id=?",
                (status_str, count, detail_msg, run_id),
            )
        if LOCK.locked():
            LOCK.release()

def start_collection(plan_dict: Dict[str, Any], scheduled_day: Optional[str] = None) -> str:
    if not LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A collection run is already in progress. Please wait for it to finish.",
        )
    run_id = str(uuid.uuid4())
    day = now().date().isoformat()
    try:
        with get_db() as c:
            c.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, now().isoformat(), MODE, "running", 0, "Collection in progress", scheduled_day),
            )
        EXECUTOR.submit(execute_collection_run, run_id, day, plan_dict)
        return run_id
    except Exception as e:
        if LOCK.locked():
            LOCK.release()
        raise HTTPException(status_code=500, detail=f"Failed to initiate run: {str(e)}")

# ---------------------------------------------------------------------------
# Background Scheduler
# ---------------------------------------------------------------------------
def scheduler_loop():
    while not SCHEDULER_STOP_EVENT.is_set():
        try:
            cfg = get_config()
            current_time = now()
            today_str = current_time.date().isoformat()
            if cfg.get("enabled") and current_time.strftime("%H:%M") >= cfg.get("time", "06:00"):
                with get_db() as c:
                    existing = c.execute(
                        "SELECT 1 FROM runs WHERE scheduled_day=?", (today_str,)
                    ).fetchone()
                if not existing and not LOCK.locked():
                    start_collection(cfg, scheduled_day=today_str)
        except Exception:
            pass
        SCHEDULER_STOP_EVENT.wait(20)

# ---------------------------------------------------------------------------
# FastAPI Lifespan Context Manager
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize Database & Background Scheduler
    init_db()
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True, name="AeroStatScheduler")
    scheduler_thread.start()
    print(f"🚀 AeroStat FastAPI Collection Service initialized in '{MODE}' mode.")
    print("📅 Background cron scheduler started. Swagger UI available at /docs")
    yield
    # Shutdown
    SCHEDULER_STOP_EVENT.set()
    EXECUTOR.shutdown(wait=False)

# ---------------------------------------------------------------------------
# FastAPI Application Declaration
# ---------------------------------------------------------------------------
app = FastAPI(
    title="AeroStat CPI Intelligence API",
    description="""
### High-Frequency Automated Airfare Observatory for CPI Augmentation
**Smart India Hackathon 2026 — Problem Statement ID: SIH26056**

AeroStat provides automated, auditable, and resilient airfare price collection across 6 Indian domestic trunk routes and 5 advance-booking horizons ($T+1, T+7, T+15, T+30, T+45$).
    """,
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# CORS Middleware
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permits Render frontend, localhost, and Vercel domains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Authentication Dependency
# ---------------------------------------------------------------------------
def verify_access_token(authorization: Optional[str] = Header(None)) -> bool:
    if not TOKEN:
        # If no token configured in environment, allow local development access
        return True
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    incoming_token = authorization.split("Bearer ", 1)[1].strip()
    if not hmac.compare_digest(incoming_token, TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True

# ---------------------------------------------------------------------------
# REST API Endpoints
# ---------------------------------------------------------------------------
@app.get("/", summary="API Root & Health Check", tags=["System"])
def root():
    return {
        "name": "AeroStat CPI Augmentation API",
        "version": "2.0.0",
        "framework": "FastAPI",
        "mode": MODE,
        "status": "online",
        "currentTimeIST": now().isoformat(),
        "documentation": "/docs",
        "endpoints": ["/status", "/observations", "/schedule", "/run", "/indices"],
    }

@app.get("/status", response_model=SystemStatusResponse, summary="Get Collector Status & Run History", tags=["Automation"])
def get_system_status(authenticated: bool = Depends(verify_access_token)):
    with get_db() as c:
        rows = c.execute(
            "SELECT started, mode, status, count, detail FROM runs ORDER BY started DESC LIMIT 20"
        ).fetchall()
        runs_list = [dict(r) for r in rows]
    return {
        "mode": MODE,
        "running": LOCK.locked(),
        "schedule": get_config(),
        "runs": runs_list,
    }

@app.get("/observations", response_model=ObservationsResponse, summary="Query Verified Fare Observations", tags=["Fares"])
def get_fare_observations(
    observation_date: Optional[str] = Query(None, description="Observation day in YYYY-MM-DD format (defaults to current date in IST)"),
    authenticated: bool = Depends(verify_access_token),
):
    target_date = observation_date or now().date().isoformat()
    with get_db() as c:
        # Pull single latest completed snapshot for consistency
        latest_run = c.execute(
            """SELECT id FROM runs 
               WHERE substr(started, 1, 10)=? AND mode=? AND status IN ('succeeded', 'partial') 
               ORDER BY started DESC LIMIT 1""",
            (target_date, MODE),
        ).fetchone()

        if not latest_run:
            # Fallback to any recent run if specific date has no completed run
            latest_run = c.execute(
                """SELECT id, substr(started, 1, 10) as dt FROM runs 
                   WHERE mode=? AND status IN ('succeeded', 'partial') 
                   ORDER BY started DESC LIMIT 1""",
                (MODE,),
            ).fetchone()
            if latest_run:
                target_date = latest_run["dt"]

        fares = []
        if latest_run:
            rows = c.execute(
                "SELECT payload FROM fares WHERE run_id=?", (latest_run["id"],)
            ).fetchall()
            fares = [json.loads(r[0]) for r in rows]

    return {
        "mode": MODE,
        "observation_date": target_date,
        "count": len(fares),
        "observations": fares,
    }

@app.post("/schedule", summary="Save or Update Daily Collection Plan", tags=["Automation"])
def update_schedule_plan(plan: SchedulePlan, authenticated: bool = Depends(verify_access_token)):
    plan_dict = plan.model_dump()
    with get_db() as c:
        c.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(plan_dict),))
    return get_system_status()

@app.post("/run", summary="Trigger On-Demand Collection Run", tags=["Automation"])
def trigger_manual_collection(
    plan: Optional[SchedulePlan] = None,
    authenticated: bool = Depends(verify_access_token),
):
    plan_dict = plan.model_dump() if plan else get_config()
    start_collection(plan_dict)
    return get_system_status()

@app.get("/indices", response_model=EconometricIndicesResponse, summary="Compute Elementary & National Price Indices (Jevons / Dutot / Carli)", tags=["Econometrics"])
def compute_indices(
    observation_date: Optional[str] = Query(None, description="Observation day in YYYY-MM-DD format"),
    formula: str = Query("jevons", description="Elementary index formula: 'jevons' (recommended), 'dutot', or 'carli'"),
    authenticated: bool = Depends(verify_access_token),
):
    """
    Computes elementary price index per route and horizon using the specified formula:
    - **jevons** (default): Axiomatic geometric mean of price relatives. Unbiased and scale-invariant.
    - **dutot**: Ratio of arithmetic mean prices (can be integrated if MoSPI requires backward compatibility).
    - **carli**: Arithmetic mean of price relatives.
    Aggregated to the National Airfare Index using DGCA Table 5.01 Passenger Traffic weights.
    """
    target_date = observation_date or now().date().isoformat()
    norm_formula = formula.lower().strip()
    if norm_formula not in ("jevons", "dutot", "carli"):
        raise HTTPException(status_code=400, detail="Invalid formula. Must be 'jevons', 'dutot', or 'carli'.")

    with get_db() as c:
        latest_run = c.execute(
            """SELECT id, substr(started, 1, 10) as dt FROM runs 
               WHERE mode=? AND status IN ('succeeded', 'partial') 
               ORDER BY started DESC LIMIT 1""",
            (MODE,),
        ).fetchone()

        if not latest_run:
            raise HTTPException(status_code=404, detail="No completed observation runs found to compute indices.")
        
        run_date = latest_run["dt"]
        rows = c.execute("SELECT payload FROM fares WHERE run_id=?", (latest_run["id"],)).fetchall()
        all_fares = [json.loads(r[0]) for r in rows]

    grouped: Dict[tuple, List[float]] = {}
    for f in all_fares:
        key = (f["route"], f["horizon"])
        grouped.setdefault(key, []).append(float(f["fare"]))

    route_indices: List[JevonsRouteHorizonIndex] = []
    horizon_aggregates: Dict[int, List[tuple]] = {h: [] for h in HORIZONS}

    for route in ROUTES:
        base_price = BASE_FARES.get(route, 5000)
        for h in HORIZONS:
            fares_list = grouped.get((route, h), [])
            if fares_list:
                curr_mean = round(sum(fares_list) / len(fares_list), 2)
                if norm_formula == "dutot":
                    # Dutot: Ratio of arithmetic means (sum(P_t) / sum(P_0))
                    rel = curr_mean / base_price
                elif norm_formula == "carli":
                    # Carli: Arithmetic mean of price relatives
                    rel = sum(p / base_price for p in fares_list) / len(fares_list)
                else:
                    # Jevons (Default & Recommended): Geometric mean of price relatives
                    log_sum = sum(math.log(p / base_price) for p in fares_list)
                    rel = math.exp(log_sum / len(fares_list))

                index_val = round(100.0 * rel, 2)
            else:
                rel = 1.0
                index_val = 100.0
                curr_mean = float(base_price)

            item = JevonsRouteHorizonIndex(
                route=route,
                horizon=h,
                matched_count=len(fares_list),
                base_fare=float(base_price),
                current_geometric_mean=curr_mean,
                price_relative=round(rel, 4),
                index_value=index_val,
            )
            route_indices.append(item)
            w = ROUTE_WEIGHTS.get(route, 1.0 / len(ROUTES))
            horizon_aggregates[h].append((w, index_val))

    national_dict = {}
    for h, route_vals in horizon_aggregates.items():
        total_w = sum(w for w, _ in route_vals) or 1.0
        weighted_sum = sum(w * idx for w, idx in route_vals)
        national_dict[f"T+{h}"] = round(weighted_sum / total_w, 2)

    methodology_desc = {
        "jevons": "Jevons Elementary Aggregator (Axiomatic Geometric Mean) with DGCA Table 5.01 Traffic Weights",
        "dutot": "Dutot Elementary Aggregator (Ratio of Arithmetic Means - MoSPI Legacy Option) with DGCA Table 5.01 Traffic Weights",
        "carli": "Carli Elementary Aggregator (Arithmetic Mean of Price Relatives) with DGCA Table 5.01 Traffic Weights",
    }.get(norm_formula, "Custom Formula")

    return {
        "observation_date": run_date,
        "mode": MODE,
        "methodology": methodology_desc,
        "national_indices": national_dict,
        "route_horizon_indices": route_indices,
    }

# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    host = os.getenv("AEROSTAT_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    print(f"Starting AeroStat FastAPI service on http://{host}:{port}")
    uvicorn.run("server:app", host=host, port=port, reload=False)
