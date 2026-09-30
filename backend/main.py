"""
AeroStat FastAPI Application Entrypoint.
Imports and exposes the FastAPI app from server.py.
"""
from server import app

if __name__ == "__main__":
    import os
    import uvicorn
    host = os.getenv("AEROSTAT_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=False)
