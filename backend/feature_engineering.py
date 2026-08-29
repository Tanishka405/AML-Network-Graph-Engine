"""
feature_engineering.py — Build feature vectors for the Isolation Forest.

Features combine:
  - Transaction-level numeric signals
  - SQL-derived structuring / velocity scores
  - Graph centrality and topology metrics

Ground-truth `pattern_type` is NEVER included as a feature.
"""

from __future__ import annotations

import math
import logging
from typing import Any, Dict, List

import numpy as np

from backend.config import AML_CFG, GEN_CFG

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature names (in order) — used for column alignment in the IF model
# ---------------------------------------------------------------------------
FEATURE_NAMES: List[str] = [
    # Transaction-level
    "log_amount",
    "round_number_flag",
    "cross_border_flag",
    "high_risk_jx_flag",
    "sub_threshold_flag",
    "rapid_succession",
    "currency_entropy",
    # SQL-derived
    "structuring_score",
    "window_count",
    "window_sum_log",
    "velocity_score",
    "burst_count",
    # Graph-derived
    "betweenness_centrality",
    "eigenvector_centrality",
    "pagerank",
    "in_degree_norm",
    "out_degree_norm",
    "weighted_out_degree_log",
    "tx_count_norm",
    "in_cycle",
    "multi_hop_score",
    "community_size_log",
]

N_FEATURES = len(FEATURE_NAMES)


def extract_features(
    tx: Dict[str, Any],
    structuring: Dict[str, Any],
    velocity:    Dict[str, Any],
    graph_feats: Dict[str, Any],
    graph_score: float,
    multi_hop:   float,
    max_degree:  int = 50,
) -> np.ndarray:
    """
    Build a 1-D feature vector from all available signals.

    Parameters
    ----------
    tx           : raw transaction dict
    structuring  : output of database.sql_entity_structuring_score()
    velocity     : output of database.sql_velocity_score()
    graph_feats  : output of TransactionGraph.get_node_features()
    graph_score  : float composite graph risk [0,1]
    multi_hop    : float multi-hop indicator [0,1]
    max_degree   : normalisation constant for degree features

    Returns
    -------
    np.ndarray of shape (N_FEATURES,)
    """
    amount = max(float(tx.get("amount", 1.0)), 1.0)
    log_amount = math.log(amount)

    # --- Transaction-level flags
    round_flag   = 1.0 if (amount % 1_000 < 1.0) else 0.0
    cross_border = 1.0 if (tx.get("src_jurisdiction") != tx.get("dst_jurisdiction")) else 0.0
    high_risk_jx = 1.0 if (
        tx.get("src_jurisdiction") in AML_CFG.high_risk_jurisdictions or
        tx.get("dst_jurisdiction") in AML_CFG.high_risk_jurisdictions
    ) else 0.0
    sub_thresh   = 1.0 if amount < AML_CFG.structuring_threshold else 0.0

    ia = float(tx.get("inter_arrival_s", 1.0))
    rapid = min(1.0, 1.0 / max(ia, 0.01))

    # Currency entropy proxy: non-USD exotic currencies
    exotic = {"CHF", "AED", "HKD", "SGD"}
    curr_entropy = 1.0 if tx.get("currency") in exotic else 0.0

    # --- SQL-derived
    str_score   = float(structuring.get("structuring_score", 0.0))
    win_count   = float(structuring.get("window_count", 0))
    win_sum     = float(structuring.get("window_sum", 0.0))
    win_sum_log = math.log(win_sum + 1.0)

    vel_score  = float(velocity.get("velocity_score", 0.0))
    burst_cnt  = float(velocity.get("burst_count", 0))

    # --- Graph-derived
    bc  = float(graph_feats.get("betweenness_centrality", 0.0))
    ec  = float(graph_feats.get("eigenvector_centrality", 0.0))
    pr  = float(graph_feats.get("pagerank", 0.0))
    ind = float(graph_feats.get("in_degree", 0.0))  / max(max_degree, 1)
    oud = float(graph_feats.get("out_degree", 0.0)) / max(max_degree, 1)
    wod = float(graph_feats.get("weighted_out_degree", 0.0))
    wod_log = math.log(wod + 1.0)
    txc = float(graph_feats.get("tx_count", 0.0)) / max(max_degree * 5, 1)
    cyc = float(graph_feats.get("in_cycle", 0.0))
    csz = float(graph_feats.get("community_size", 1.0))
    csz_log = math.log(max(csz, 1.0))

    vec = np.array([
        log_amount, round_flag, cross_border, high_risk_jx,
        sub_thresh, rapid, curr_entropy,
        str_score, win_count, win_sum_log,
        vel_score, burst_cnt,
        bc, ec, pr, ind, oud, wod_log, txc, cyc,
        multi_hop, csz_log,
    ], dtype=np.float64)

    assert len(vec) == N_FEATURES, f"Feature count mismatch: {len(vec)} vs {N_FEATURES}"
    return vec


def build_feature_matrix(
    records: List[Dict[str, Any]],
) -> np.ndarray:
    """
    Build a (N, N_FEATURES) matrix from pre-enriched records.
    Each record should have keys matching the feature names or the
    sub-dicts used in extract_features.
    Used in batch training / evaluation.
    """
    rows = []
    for r in records:
        tx = r.get("tx", r)
        structuring = r.get("structuring", {})
        velocity    = r.get("velocity", {})
        graph_feats = r.get("graph_feats", {})
        graph_score = float(r.get("graph_score", 0.0))
        multi_hop   = float(r.get("multi_hop", 0.0))
        vec = extract_features(tx, structuring, velocity, graph_feats, graph_score, multi_hop)
        rows.append(vec)
    return np.vstack(rows)
