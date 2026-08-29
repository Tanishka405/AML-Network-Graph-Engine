"""
generator.py — Reproducible synthetic AML transaction generator.

Generates realistic normal traffic plus labelled suspicious patterns:
  - structuring (smurfing): coordinated sub-threshold transactions
  - layering: multi-hop fund movement
  - circular_flow: funds returning to originator
  - velocity_burst: rapid-fire transactions in short windows
  - high_risk_jx: transactions through high-risk jurisdictions

Ground-truth `pattern_type` is stored in the DB for evaluation ONLY.
It must NOT be used as a feature in the fraud detector.
"""

from __future__ import annotations

import uuid
import math
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Generator, List, Optional, Tuple

import numpy as np

from backend.config import GEN_CFG, GeneratorConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entity pool
# ---------------------------------------------------------------------------
_ENTITY_PREFIXES = [
    "MERIDIAN", "NOVA", "ATLAS", "SOLARIS", "VORTEX",
    "HELIX", "QUANTUM", "ZENITH", "OBSIDIAN", "ECLIPSE",
    "CIPHER", "NEXUS", "DELTA", "OMEGA", "APEX",
    "VERTEX", "CORONA", "STRATUS", "AXIOM", "PRIME",
]
_ENTITY_TYPES = ["CAPITAL", "TRUST", "BANCORP", "FIN", "CREDIT",
                 "PAYMENTS", "CLEARING", "FUND", "PARTNERS", "HOLDINGS"]


def _build_entity_pool(n: int, rng: np.random.Generator) -> List[str]:
    names = []
    for i in range(n):
        p = _ENTITY_PREFIXES[i % len(_ENTITY_PREFIXES)]
        t = _ENTITY_TYPES[i % len(_ENTITY_TYPES)]
        names.append(f"{p}_{t}_{i:03d}")
    return names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tx_id(rng: np.random.Generator | None = None) -> str:
    """Generate a reproducible transaction ID using the provided rng, or random uuid."""
    if rng is not None:
        hi = rng.integers(0, 2**32)
        lo = rng.integers(0, 2**32)
        return f"{hi:08X}{lo:04X}"
    return uuid.uuid4().hex[:12].upper()


def _fmt_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _random_currency(rng: np.random.Generator, cfg: GeneratorConfig) -> str:
    return str(rng.choice(cfg.currencies))


def _random_jurisdiction(
    rng: np.random.Generator,
    cfg: GeneratorConfig,
    high_risk: bool = False,
) -> str:
    if high_risk:
        return str(rng.choice(list(cfg.high_risk_jurisdictions)))
    return str(rng.choice(cfg.all_jurisdictions))


# ---------------------------------------------------------------------------
# Pattern generators
# ---------------------------------------------------------------------------
def _normal_tx(
    rng: np.random.Generator,
    cfg: GeneratorConfig,
    entities: List[str],
    ts: datetime,
    prev_ts: Optional[datetime],
) -> Dict[str, Any]:
    src, dst = [str(x) for x in rng.choice(entities, size=2, replace=False)]
    amount = float(np.exp(rng.normal(cfg.normal_amount_mu, cfg.normal_amount_sigma)))
    ia = (ts - prev_ts).total_seconds() if prev_ts else 0.0
    return dict(
        tx_id=_tx_id(rng), timestamp=_fmt_ts(ts),
        source_entity=str(src), dest_entity=str(dst),
        amount=round(amount, 2),
        currency=str(_random_currency(rng, cfg)),
        src_jurisdiction=str(_random_jurisdiction(rng, cfg)),
        dst_jurisdiction=str(_random_jurisdiction(rng, cfg)),
        pattern_type="normal",
        inter_arrival_s=round(ia, 2),
    )


def _structuring_batch(
    rng: np.random.Generator,
    cfg: GeneratorConfig,
    entities: List[str],
    ts: datetime,
) -> List[Dict[str, Any]]:
    """
    Generate a coordinated batch of sub-threshold transactions from the
    same source entity to disguise a large aggregate transfer.
    """
    src = str(rng.choice(entities))
    dst_pool = [str(e) for e in entities if e != src]
    count = int(rng.integers(cfg.smurf_count_min, cfg.smurf_count_max + 1))
    results = []
    prev_ts: Optional[datetime] = None
    for i in range(count):
        offset = timedelta(seconds=int(rng.integers(30, 300)))
        t = ts + offset * i
        dst = str(rng.choice(dst_pool))
        amount = round(float(rng.uniform(cfg.smurf_min_amount, cfg.smurf_threshold - 0.01)), 2)
        ia = (t - prev_ts).total_seconds() if prev_ts else 0.0
        results.append(dict(
            tx_id=_tx_id(rng), timestamp=_fmt_ts(t),
            source_entity=src, dest_entity=dst,
            amount=amount, currency=_random_currency(rng, cfg),
            src_jurisdiction=_random_jurisdiction(rng, cfg),
            dst_jurisdiction=_random_jurisdiction(rng, cfg),
            pattern_type="structuring",
            inter_arrival_s=round(ia, 2),
        ))
        prev_ts = t
    return results


def _layering_chain(
    rng: np.random.Generator,
    cfg: GeneratorConfig,
    entities: List[str],
    ts: datetime,
) -> List[Dict[str, Any]]:
    """
    A → B → C → D … multi-hop movement of a single sum.
    Each hop uses a slightly smaller amount (fee/attrition simulation).
    """
    hops = int(rng.integers(cfg.layer_min_hops, cfg.layer_max_hops + 1))
    pool = [str(e) for e in rng.choice(entities, size=hops + 1, replace=False).tolist()]
    amount = round(float(rng.uniform(cfg.layer_amount_min, cfg.layer_amount_max)), 2)
    results = []
    prev_ts: Optional[datetime] = None
    for i in range(hops):
        offset = timedelta(seconds=int(rng.integers(60, 900)))
        t = ts + offset * i
        ia = (t - prev_ts).total_seconds() if prev_ts else 0.0
        hop_amount = round(amount * (0.95 ** i), 2)
        results.append(dict(
            tx_id=_tx_id(rng), timestamp=_fmt_ts(t),
            source_entity=pool[i], dest_entity=pool[i + 1],
            amount=hop_amount, currency=_random_currency(rng, cfg),
            src_jurisdiction=_random_jurisdiction(rng, cfg, high_risk=rng.random() > 0.5),
            dst_jurisdiction=_random_jurisdiction(rng, cfg, high_risk=rng.random() > 0.5),
            pattern_type="layering",
            inter_arrival_s=round(ia, 2),
        ))
        prev_ts = t
    return results


def _circular_flow(
    rng: np.random.Generator,
    cfg: GeneratorConfig,
    entities: List[str],
    ts: datetime,
) -> List[Dict[str, Any]]:
    """
    A → B → C → A  — funds return to originator through a cycle.
    """
    length = int(rng.integers(cfg.cycle_min_len, cfg.cycle_max_len + 1))
    pool = [str(e) for e in rng.choice(entities, size=length, replace=False).tolist()]
    amount = round(float(np.exp(rng.normal(10.5, 1.0))), 2)
    results = []
    prev_ts: Optional[datetime] = None
    for i in range(length):
        offset = timedelta(seconds=int(rng.integers(120, 1_800)))
        t = ts + offset * i
        ia = (t - prev_ts).total_seconds() if prev_ts else 0.0
        src = pool[i]
        dst = pool[(i + 1) % length]
        results.append(dict(
            tx_id=_tx_id(rng), timestamp=_fmt_ts(t),
            source_entity=src, dest_entity=dst,
            amount=round(amount * rng.uniform(0.9, 1.1), 2),
            currency=_random_currency(rng, cfg),
            src_jurisdiction=_random_jurisdiction(rng, cfg),
            dst_jurisdiction=_random_jurisdiction(rng, cfg),
            pattern_type="circular_flow",
            inter_arrival_s=round(ia, 2),
        ))
        prev_ts = t
    return results


def _velocity_burst(
    rng: np.random.Generator,
    cfg: GeneratorConfig,
    entities: List[str],
    ts: datetime,
) -> List[Dict[str, Any]]:
    """Rapid succession of transactions from a single entity."""
    src = str(rng.choice(entities))
    dst_pool = [str(e) for e in entities if e != src]
    count = int(rng.integers(cfg.velocity_burst_min, cfg.velocity_burst_max + 1))
    results = []
    prev_ts: Optional[datetime] = None
    for i in range(count):
        offset = timedelta(seconds=int(rng.integers(1, 30)))
        t = ts + offset * i
        ia = (t - prev_ts).total_seconds() if prev_ts else 0.0
        results.append(dict(
            tx_id=_tx_id(rng), timestamp=_fmt_ts(t),
            source_entity=src, dest_entity=str(rng.choice(dst_pool)),
            amount=round(float(rng.uniform(500, 15_000)), 2),
            currency=_random_currency(rng, cfg),
            src_jurisdiction=_random_jurisdiction(rng, cfg),
            dst_jurisdiction=_random_jurisdiction(rng, cfg),
            pattern_type="velocity_burst",
            inter_arrival_s=round(ia, 2),
        ))
        prev_ts = t
    return results


def _high_risk_jx_tx(
    rng: np.random.Generator,
    cfg: GeneratorConfig,
    entities: List[str],
    ts: datetime,
    prev_ts: Optional[datetime],
) -> Dict[str, Any]:
    src, dst = [str(x) for x in rng.choice(entities, size=2, replace=False)]
    amount = round(float(rng.uniform(20_000, 500_000)), 2)
    ia = (ts - prev_ts).total_seconds() if prev_ts else 0.0
    return dict(
        tx_id=_tx_id(rng), timestamp=_fmt_ts(ts),
        source_entity=src, dest_entity=dst,
        amount=amount,
        currency=str(rng.choice(["CHF", "AED", "HKD"])),
        src_jurisdiction=_random_jurisdiction(rng, cfg, high_risk=True),
        dst_jurisdiction=_random_jurisdiction(rng, cfg, high_risk=rng.random() > 0.5),
        pattern_type="high_risk_jx",
        inter_arrival_s=round(ia, 2),
    )


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------
def generate_transactions(
    cfg: GeneratorConfig | None = None,
    verbose: bool = True,
) -> List[Dict[str, Any]]:
    """
    Generate `cfg.n_transactions` synthetic transactions.
    Returns a list of dicts suitable for DB insertion.
    """
    cfg = cfg or GEN_CFG
    rng = np.random.default_rng(cfg.seed)
    entities = _build_entity_pool(cfg.n_entities, rng)

    # Build a timeline
    start = datetime.strptime(cfg.start_date, "%Y-%m-%d")
    end   = datetime.strptime(cfg.end_date,   "%Y-%m-%d")
    total_seconds = (end - start).total_seconds()

    # Decide how many of each pattern type to generate
    n = cfg.n_transactions
    n_smurf    = int(n * cfg.smurf_fraction)
    n_layer    = int(n * cfg.layering_fraction)
    n_circ     = int(n * cfg.circular_fraction)
    n_vel      = int(n * cfg.velocity_fraction)
    n_hirisk   = int(n * cfg.high_risk_jx_frac)
    n_normal   = n  # overproduced then trimmed

    all_txs: List[Dict[str, Any]] = []

    def rand_ts() -> datetime:
        return start + timedelta(seconds=float(rng.uniform(0, total_seconds)))

    # --- Normal transactions
    log.info("Generating normal transactions…")
    ts_list = sorted([rand_ts() for _ in range(n_normal)])
    prev_ts: Optional[datetime] = None
    for ts in ts_list:
        all_txs.append(_normal_tx(rng, cfg, entities, ts, prev_ts))
        prev_ts = ts

    # --- Structuring batches
    log.info("Generating structuring sequences…")
    for _ in range(n_smurf):
        all_txs.extend(_structuring_batch(rng, cfg, entities, rand_ts()))

    # --- Layering chains
    log.info("Generating layering chains…")
    for _ in range(n_layer):
        all_txs.extend(_layering_chain(rng, cfg, entities, rand_ts()))

    # --- Circular flows
    log.info("Generating circular flows…")
    for _ in range(n_circ):
        all_txs.extend(_circular_flow(rng, cfg, entities, rand_ts()))

    # --- Velocity bursts
    log.info("Generating velocity bursts…")
    for _ in range(n_vel):
        all_txs.extend(_velocity_burst(rng, cfg, entities, rand_ts()))

    # --- High-risk jurisdiction transactions
    log.info("Generating high-risk jurisdiction transactions…")
    prev_ts = None
    for _ in range(n_hirisk):
        ts = rand_ts()
        all_txs.append(_high_risk_jx_tx(rng, cfg, entities, ts, prev_ts))
        prev_ts = ts

    # Sort all by timestamp
    all_txs.sort(key=lambda x: x["timestamp"])

    # Trim to exactly n if over-generated
    if len(all_txs) > n:
        all_txs = all_txs[:n]

    # Recompute inter_arrival_s after sorting so values are globally correct.
    # Each tx's ia = seconds since the previous transaction (any entity/pattern).
    prev_sorted_ts = None
    for tx in all_txs:
        curr_ts = datetime.strptime(tx["timestamp"], "%Y-%m-%d %H:%M:%S")
        if prev_sorted_ts is None:
            tx["inter_arrival_s"] = 0.0
        else:
            tx["inter_arrival_s"] = round((curr_ts - prev_sorted_ts).total_seconds(), 2)
        prev_sorted_ts = curr_ts

    if verbose:
        from collections import Counter
        counts = Counter(t["pattern_type"] for t in all_txs)
        log.info("Generated %d transactions: %s", len(all_txs), dict(counts))

    return all_txs


def stream_transactions(
    cfg: GeneratorConfig | None = None,
) -> Generator[Dict[str, Any], None, None]:
    """
    Infinite generator that yields one transaction at a time in real-time.
    Used by the FastAPI streaming engine.
    """
    cfg = cfg or GEN_CFG
    rng = np.random.default_rng(cfg.seed)
    entities = _build_entity_pool(cfg.n_entities, rng)

    start = datetime.now(tz=timezone.utc)
    prev_ts: Optional[datetime] = None
    _counter = 0

    while True:
        ts = datetime.now(tz=timezone.utc)

        # Weighted pattern selection
        roll = rng.random()
        if roll < cfg.smurf_fraction:
            batch = _structuring_batch(rng, cfg, entities, ts.replace(tzinfo=None))
            for tx in batch:
                yield tx
                _counter += 1
        elif roll < cfg.smurf_fraction + cfg.layering_fraction:
            batch = _layering_chain(rng, cfg, entities, ts.replace(tzinfo=None))
            for tx in batch:
                yield tx
                _counter += 1
        elif roll < cfg.smurf_fraction + cfg.layering_fraction + cfg.circular_fraction:
            batch = _circular_flow(rng, cfg, entities, ts.replace(tzinfo=None))
            for tx in batch:
                yield tx
                _counter += 1
        elif roll < cfg.smurf_fraction + cfg.layering_fraction + cfg.circular_fraction + cfg.velocity_fraction:
            batch = _velocity_burst(rng, cfg, entities, ts.replace(tzinfo=None))
            for tx in batch:
                yield tx
                _counter += 1
        else:
            prev_ts_dt = prev_ts.replace(tzinfo=None) if prev_ts else None
            yield _normal_tx(rng, cfg, entities, ts.replace(tzinfo=None), prev_ts_dt)
            _counter += 1

        prev_ts = ts
