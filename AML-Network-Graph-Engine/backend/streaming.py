"""
streaming.py — Central transaction-processing loop.

Architecture
-----------
Transaction Generator
       ↓
  Processing Engine (this module)
       ↓
  ┌────┼─────────────┐
  ↓    ↓             ↓
Graph  SQL anomaly  Rules
       ↓
     Alert DB
       ↓
  asyncio broadcast queue
       ↓
  All WebSocket clients

All connected WebSocket clients receive the SAME enriched records
from a single shared async queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Set

from backend.config import STREAM_CFG, IF_CFG
from backend.database import (
    insert_transaction,
    insert_alert,
    sql_entity_structuring_score,
    sql_velocity_score,
)
from backend.generator import stream_transactions
from backend.graph_engine import TransactionGraph
from backend.feature_engineering import extract_features
from backend.anomaly_detector import get_detector
from backend.aml_rules import evaluate_all_rules
from backend.risk_engine import enrich_transaction, is_alert, alert_payload

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_broadcast_queue: asyncio.Queue = None     # type: ignore[assignment]
_connected_clients: Set[asyncio.Queue] = set()
_graph: Optional[TransactionGraph] = None
_stats: Dict[str, Any] = {
    "total_processed": 0,
    "total_alerts":    0,
    "start_time":      time.time(),
}


def _get_graph() -> TransactionGraph:
    global _graph
    if _graph is None:
        _graph = TransactionGraph()
    return _graph


async def get_broadcast_queue() -> asyncio.Queue:
    global _broadcast_queue
    if _broadcast_queue is None:
        _broadcast_queue = asyncio.Queue(maxsize=STREAM_CFG.broadcast_queue_maxsize)
    return _broadcast_queue


def subscribe() -> asyncio.Queue:
    """Create a per-client queue and register it for broadcast."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _connected_clients.add(q)
    log.info("Client subscribed — total clients: %d", len(_connected_clients))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _connected_clients.discard(q)
    log.info("Client unsubscribed — total clients: %d", len(_connected_clients))


async def _broadcast(record: Dict[str, Any]) -> None:
    """Push a record to every connected client queue."""
    payload = json.dumps({"type": "transaction", "data": record})
    dead: List[asyncio.Queue] = []
    for client_q in list(_connected_clients):
        try:
            client_q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(client_q)
    for d in dead:
        _connected_clients.discard(d)


# ---------------------------------------------------------------------------
# Core processing function
# ---------------------------------------------------------------------------
def _process_transaction(tx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Synchronous processing of one transaction.
    All heavy computation (graph, SQL, IF) happens here.
    """
    detector = get_detector()
    graph    = _get_graph()

    # 1. Persist raw transaction
    try:
        insert_transaction(tx)
    except Exception as exc:
        log.debug("Insert error (likely duplicate): %s", exc)

    # 2. Update graph
    graph.add_transaction(
        tx_id=tx["tx_id"],
        source=tx["source_entity"],
        dest=tx["dest_entity"],
        amount=float(tx["amount"]),
    )

    # 3. Graph features
    graph_feats = graph.get_node_features(tx["source_entity"])
    graph_score = graph.get_graph_score(tx["source_entity"])
    multi_hop   = graph.multi_hop_indicator(tx["source_entity"], tx["dest_entity"])

    # 4. SQL-based structuring and velocity scores
    structuring = sql_entity_structuring_score(
        entity=tx["source_entity"],
        tx_timestamp=tx["timestamp"],
    )
    velocity = sql_velocity_score(
        entity=tx["source_entity"],
        tx_timestamp=tx["timestamp"],
    )

    # 5. Build feature vector
    feat_vec = extract_features(
        tx=tx,
        structuring=structuring,
        velocity=velocity,
        graph_feats=graph_feats,
        graph_score=graph_score,
        multi_hop=multi_hop,
    )

    # 6. Anomaly score (warm-up aware)
    if_score = detector.add_sample(feat_vec)

    # 7. AML rules
    aml_result = evaluate_all_rules(
        tx=tx,
        sql_structuring=structuring,
        sql_velocity=velocity,
        graph_feats=graph_feats,
        multi_hop_score=multi_hop,
    )

    # 8. Composite risk
    record = enrich_transaction(
        tx=tx,
        if_score=if_score,
        graph_score=graph_score,
        graph_feats=graph_feats,
        aml_rules=aml_result,
        structuring=structuring,
        velocity=velocity,
        multi_hop=multi_hop,
    )

    # 9. Graph stats
    record["graph_nodes"] = graph.n_nodes
    record["graph_edges"] = graph.n_edges
    record["model_generation"] = detector.generation

    # 10. Persist alert if threshold exceeded
    if is_alert(record):
        try:
            insert_alert(alert_payload(record))
        except Exception as exc:
            log.debug("Alert insert error: %s", exc)
        _stats["total_alerts"] += 1

    _stats["total_processed"] += 1
    return record


# ---------------------------------------------------------------------------
# Async streaming loop
# ---------------------------------------------------------------------------
async def run_stream_loop(tx_per_second: float | None = None) -> None:
    """
    Main async task: generates transactions, processes them, and
    broadcasts enriched records to all subscribed client queues.
    """
    rate    = tx_per_second or STREAM_CFG.tx_per_second
    interval = 1.0 / rate
    gen     = stream_transactions()

    log.info("Stream loop started at %.1f tx/s", rate)

    while True:
        t0 = time.monotonic()

        try:
            tx = next(gen)
        except StopIteration:
            break

        # Run blocking processing in thread pool to keep event loop free
        loop = asyncio.get_event_loop()
        record = await loop.run_in_executor(None, _process_transaction, tx)

        await _broadcast(record)

        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0.0, interval - elapsed))


def get_stats() -> Dict[str, Any]:
    elapsed = time.time() - _stats["start_time"]
    rate    = _stats["total_processed"] / max(elapsed, 1.0)
    graph   = _get_graph()
    return {
        **_stats,
        "elapsed_seconds":  round(elapsed, 1),
        "tx_per_second":    round(rate, 2),
        "graph_nodes":      graph.n_nodes,
        "graph_edges":      graph.n_edges,
        "n_communities":    graph.n_communities,
        "model_generation": get_detector().generation,
        "model_warm":       get_detector().is_warm,
    }
