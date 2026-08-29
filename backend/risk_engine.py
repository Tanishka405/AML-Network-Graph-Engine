"""
risk_engine.py — Composite AML risk score.

Combines signals from:
  - Isolation Forest anomaly detection
  - Graph-topology score
  - Structuring rule
  - Velocity rule
  - Circular flow indicator
  - High-risk jurisdiction flag

All weights are centralised in config.py (RiskWeights).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from backend.config import RISK_W, RiskWeights, classify_risk

log = logging.getLogger(__name__)


def compute_composite_risk(
    if_score:           float,
    graph_score:        float,
    structuring_score:  float,
    velocity_score:     float,
    circular_score:     float,
    high_risk_jx_score: float,
    weights: RiskWeights | None = None,
) -> float:
    """
    Weighted linear combination of component risk scores.
    All inputs and output are in [0, 1].
    """
    w = weights or RISK_W
    raw = (
        w.isolation_forest * if_score +
        w.graph_score       * graph_score +
        w.structuring       * structuring_score +
        w.velocity          * velocity_score +
        w.circular_flow     * circular_score +
        w.high_risk_jx      * high_risk_jx_score
    )
    return round(min(max(raw, 0.0), 1.0), 6)


def enrich_transaction(
    tx: Dict[str, Any],
    if_score:           float,
    graph_score:        float,
    graph_feats:        Dict[str, float],
    aml_rules:          Dict[str, Any],
    structuring:        Dict[str, Any],
    velocity:           Dict[str, Any],
    multi_hop:          float,
    weights: RiskWeights | None = None,
) -> Dict[str, Any]:
    """
    Attach all risk signals to a transaction dict and return the
    enriched record ready for broadcasting and DB storage.
    """
    str_score  = float(aml_rules.get("structuring_score", 0.0))
    vel_score  = float(aml_rules.get("velocity_score", 0.0))
    circ_score = float(aml_rules.get("circular_score", 0.0))
    hrj_score  = float(aml_rules.get("high_risk_jx_score", 0.0))

    composite = compute_composite_risk(
        if_score=if_score,
        graph_score=graph_score,
        structuring_score=str_score,
        velocity_score=vel_score,
        circular_score=circ_score,
        high_risk_jx_score=hrj_score,
        weights=weights,
    )
    risk_label = classify_risk(composite)
    reasons    = aml_rules.get("reasons", [])

    return {
        # Original transaction fields
        **tx,
        # Scores
        "if_anomaly_score":    round(if_score, 6),
        "graph_score":         round(graph_score, 6),
        "structuring_score":   round(str_score, 6),
        "velocity_score":      round(vel_score, 6),
        "circular_score":      round(circ_score, 6),
        "high_risk_jx_score":  round(hrj_score, 6),
        "composite_risk":      composite,
        "risk_label":          risk_label,
        # Graph topology
        "betweenness_centrality": round(graph_feats.get("betweenness_centrality", 0.0), 6),
        "eigenvector_centrality": round(graph_feats.get("eigenvector_centrality", 0.0), 6),
        "pagerank":               round(graph_feats.get("pagerank", 0.0), 6),
        "in_cycle":               int(graph_feats.get("in_cycle", 0.0)),
        "community_id":           int(graph_feats.get("community_id", -1)),
        "multi_hop_score":        round(multi_hop, 6),
        # SQL-derived
        "window_count":           int(structuring.get("window_count", 0)),
        "window_sum":             float(structuring.get("window_sum", 0.0)),
        "burst_count":            int(velocity.get("burst_count", 0)),
        # Human explanation
        "alert_reasons":          reasons,
        "alert_reason_str":       " | ".join(reasons) if reasons else "No alert",
    }


def is_alert(record: Dict[str, Any], threshold: float = 0.60) -> bool:
    return float(record.get("composite_risk", 0.0)) >= threshold


def alert_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the minimal fields needed for an alert DB record."""
    return {
        "tx_id":              record["tx_id"],
        "timestamp":          record["timestamp"],
        "risk_score":         record["composite_risk"],
        "risk_label":         record["risk_label"],
        "anomaly_score":      record["if_anomaly_score"],
        "structuring_score":  record["structuring_score"],
        "graph_score":        record["graph_score"],
        "velocity_score":     record["velocity_score"],
        "circular_score":     record["circular_score"],
        "high_risk_jx_score": record["high_risk_jx_score"],
        "reason":             record["alert_reason_str"],
    }
