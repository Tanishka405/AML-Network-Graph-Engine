"""
main.py — FastAPI application entry point.

Endpoints
---------
GET  /health                  — liveness check
GET  /stats                   — live engine statistics
GET  /alerts?limit=N          — recent alerts from DB
GET  /graph/summary           — graph topology summary
WS   /ws/transactions         — real-time enriched transaction stream

Architecture: a single central stream loop processes every transaction
and broadcasts to ALL connected WebSocket clients via per-client queues.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.config import DB_CFG
from backend.database import init_db, get_recent_alerts, count_transactions, count_alerts
from backend.streaming import run_stream_loop, subscribe, unsubscribe, get_stats, _get_graph

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("aml.api")

# ---------------------------------------------------------------------------
# Lifespan — initialise DB and start streaming loop
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Initialising database…")
    os.makedirs(os.path.dirname(DB_CFG.path), exist_ok=True)
    init_db()
    log.info("Starting stream loop…")
    task = asyncio.create_task(run_stream_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log.info("Stream loop stopped.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="AML Fraud Detection Engine",
    description="Real-Time Financial Fraud Detection & AML Network Graph Engine",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "version": "3.0.0"}


@app.get("/stats")
async def stats() -> Dict[str, Any]:
    return get_stats()


@app.get("/alerts")
async def alerts(limit: int = 100) -> Dict[str, Any]:
    rows = get_recent_alerts(limit=limit)
    return {"alerts": rows, "total": len(rows)}


@app.get("/graph/summary")
async def graph_summary() -> Dict[str, Any]:
    g = _get_graph()
    return g.summary()


@app.get("/db/counts")
async def db_counts() -> Dict[str, Any]:
    return {
        "transactions": count_transactions(),
        "alerts":       count_alerts(),
    }


# ---------------------------------------------------------------------------
# WebSocket — real-time stream
# ---------------------------------------------------------------------------
@app.websocket("/ws/transactions")
async def websocket_stream(ws: WebSocket) -> None:
    await ws.accept()
    client = f"{ws.client.host}:{ws.client.port}" if ws.client else "unknown"
    log.info("WS connected: %s", client)

    # Send handshake
    await ws.send_text(json.dumps({
        "type": "handshake",
        "msg":  "AML Engine v3.0 — real-time stream initialised",
    }))

    client_q = subscribe()
    try:
        while True:
            # Wait for next record from the central broadcast queue
            try:
                payload = await asyncio.wait_for(client_q.get(), timeout=30.0)
                await ws.send_text(payload)
            except asyncio.TimeoutError:
                # Send keepalive ping
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        log.info("WS disconnected: %s", client)
    except Exception as exc:
        log.warning("WS error [%s]: %s", client, exc)
    finally:
        unsubscribe(client_q)


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
