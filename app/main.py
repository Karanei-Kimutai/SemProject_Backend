"""FastAPI application for the mobile app's public API."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from sqlalchemy import text

from app import __version__
from app.db import get_engine
from app.errors import install_error_handlers
from app.routers import auth, batches, forecasts, imports, models_api, products, research, team, training, workspaces

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="Retail Demand Forecasting API", version=__version__,
              description="Public API for the mobile app. See docs/api.md for the guide.")
install_error_handlers(app)


@app.middleware("http")
async def request_id(request: Request, call_next):
    incoming = request.headers.get("X-Request-Id", "")
    request.state.request_id = incoming[:80] if incoming else f"req_{uuid.uuid4().hex[:16]}"
    response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.request_id
    return response


@app.get("/healthz", tags=["health"])
def health() -> dict:
    with get_engine().connect() as connection:
        connection.execute(text("select 1"))
    return {"status": "ok", "version": __version__}


for router in (auth.router, workspaces.router, products.router, models_api.router, forecasts.router,
               imports.router, training.router, team.router, research.router, batches.router):
    app.include_router(router)
