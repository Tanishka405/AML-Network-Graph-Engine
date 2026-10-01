"""
config.py — Central configuration for the AML / Fraud Detection Engine.

Every tuneable constant lives here.  Importing modules should never
hard-code magic numbers; they should reference this module.
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Dict, List, Set


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH  = os.path.join(DATA_DIR, "aml.db")

# ---------------------------------------------------------------------------
# Synthetic generator
# ---------------------------------------------------------------------------
@dataclass
class GeneratorConfig:
    seed: int = 42
    n_transactions: int = 500_000
    n_entities: int = 200           # distinct financial entities / accounts
    start_date: str = "2024-01-01"
    end_date:   str = "2024-12-31"

    # Pattern mix (fractions; must sum ≤ 1.0; remainder = normal)
    smurf_fraction:     float = 0.04   # structuring / smurfing
    layering_fraction:  float = 0.03   # multi-hop layering
    circular_fraction:  float = 0.02   # circular fund flows
    velocity_fraction:  float = 0.03   # rapid-fire transaction bursts
    high_risk_jx_frac:  float = 0.03   # high-risk jurisdiction activity

    # Normal amount distribution (log-normal parameters)
    normal_amount_mu:    float = 9.5   # ln(~$13 000)
    normal_amount_sigma: float = 2.0

    # Smurfing parameters
    smurf_threshold:     float = 10_000.0
    smurf_min_amount:    float = 8_000.0
    smurf_count_min:     int   = 3
    smurf_count_max:     int   = 8

    # Layering parameters
    layer_min_hops: int   = 3
    layer_max_hops: int   = 6
    layer_amount_min: float = 50_000.0
    layer_amount_max: float = 500_000.0

    # Circular flow parameters
    cycle_min_len: int = 3
    cycle_max_len: int = 5

    # Velocity burst parameters
    velocity_burst_min: int = 5
    velocity_burst_max: int = 20
    velocity_window_s:  int = 300        # 5-minute burst window

    currencies: List[str] = field(default_factory=lambda: [
        "USD", "EUR", "GBP", "CHF", "SGD", "AED", "HKD",
    ])

    all_jurisdictions: List[str] = field(default_factory=lambda: [
        "US", "UK", "DE", "FR", "SG", "JP", "AU",
        "AE", "KY", "VG", "PA", "MT", "CY", "LI",
    ])

    high_risk_jurisdictions: Set[str] = field(default_factory=lambda: {
        "AE", "KY", "VG", "PA", "MT", "CY", "LI",
    })


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@dataclass
class DatabaseConfig:
    path: str = DB_PATH
    # Structuring detection window
    structuring_window_minutes: int = 60
    structuring_count_threshold: int = 3
    structuring_aggregate_ratio: float = 0.85  # aggregate / (n * threshold)


# ---------------------------------------------------------------------------
# Graph engine
# ---------------------------------------------------------------------------
@dataclass
class GraphConfig:
    rolling_window_seconds: int = 3_600     # 1-hour rolling graph
    max_nodes: int = 500
    pagerank_alpha: float = 0.85
    betweenness_k: int | None = None        # None = exact; int = approximation
    # Seed for networkx's k-sampled betweenness approximation. Without it the
    # sampled source nodes come from Python's unseeded global RNG, making
    # graph scores (and therefore alert counts) differ run to run.
    betweenness_seed: int = 42
    eigenvector_max_iter: int = 300
    eigenvector_tol: float = 1e-6


# ---------------------------------------------------------------------------
# Isolation Forest
# ---------------------------------------------------------------------------
@dataclass
class IFConfig:
    n_estimators: int = 200
    contamination: float = 0.08
    random_state: int = 42
    # Warm-up: min samples before model is considered trained
    min_train_samples: int = 500
    # Retrain every N new samples
    retrain_interval: int = 200


# ---------------------------------------------------------------------------
# AML Rule Engine
# ---------------------------------------------------------------------------
@dataclass
class AMLRuleConfig:
    structuring_threshold: float = 10_000.0
    structuring_window_minutes: int = 60
    structuring_min_count: int = 3

    velocity_window_seconds: int = 300
    velocity_burst_threshold: int = 5

    high_risk_jurisdictions: Set[str] = field(default_factory=lambda: {
        "AE", "KY", "VG", "PA", "MT", "CY", "LI",
    })


# ---------------------------------------------------------------------------
# Composite risk score weights (must sum to 1.0)
# ---------------------------------------------------------------------------
@dataclass
class RiskWeights:
    isolation_forest: float = 0.30
    graph_score:      float = 0.25
    structuring:      float = 0.20
    velocity:         float = 0.10
    circular_flow:    float = 0.10
    high_risk_jx:     float = 0.05

    def validate(self) -> None:
        total = (
            self.isolation_forest + self.graph_score +
            self.structuring + self.velocity +
            self.circular_flow + self.high_risk_jx
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Risk weights must sum to 1.0, got {total:.4f}")


# ---------------------------------------------------------------------------
# Risk classification thresholds
# ---------------------------------------------------------------------------
RISK_THRESHOLDS: Dict[str, float] = {
    "CRITICAL": 0.80,
    "HIGH":     0.60,
    "MEDIUM":   0.40,
    "LOW":      0.00,
}


def classify_risk(score: float) -> str:
    for label, threshold in RISK_THRESHOLDS.items():
        if score >= threshold:
            return label
    return "LOW"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
@dataclass
class StreamConfig:
    tx_per_second: float = 2.0          # generation rate
    broadcast_queue_maxsize: int = 1_000


# ---------------------------------------------------------------------------
# Singleton instances (import these in other modules)
# ---------------------------------------------------------------------------
GEN_CFG    = GeneratorConfig()
DB_CFG     = DatabaseConfig()
GRAPH_CFG  = GraphConfig()
IF_CFG     = IFConfig()
AML_CFG    = AMLRuleConfig()
RISK_W     = RiskWeights()
STREAM_CFG = StreamConfig()

RISK_W.validate()
