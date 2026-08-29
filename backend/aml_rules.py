"""
aml_rules.py — Explicit AML rule engine.

Each rule produces:
  - a normalised float score in [0.0, 1.0]
  - a human-readable reason string

Rules
-----
1. Structuring / smurfing
2. Rapid transaction velocity
3. Circular fund flow
4. Multi-hop layering
5. High-risk jurisdiction
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from backend.config import AML_CFG, AMLRuleConfig


@dataclass
class RuleResult:
    rule_name: str
    score:     float      # [0, 1]
    reason:    str
    triggered: bool


def rule_structuring(
    tx: Dict[str, Any],
    sql_structuring: Dict[str, Any],
    cfg: AMLRuleConfig | None = None,
) -> RuleResult:
    """
    Structuring rule: score based on SQL-aggregated window analysis.
    A score from the SQL module already encodes the coordinated pattern.
    """
    cfg   = cfg or AML_CFG
    score = float(sql_structuring.get("structuring_score", 0.0))
    count = int(sql_structuring.get("window_count", 0))
    wsum  = float(sql_structuring.get("window_sum", 0.0))
    wavg  = float(sql_structuring.get("window_avg", 0.0))

    triggered = (
        count >= cfg.structuring_min_count and
        float(tx.get("amount", 0)) < cfg.structuring_threshold and
        score > 0.1
    )
    if triggered:
        reason = (
            f"STRUCTURING: {count} sub-threshold transactions "
            f"totalling ${wsum:,.0f} (avg ${wavg:,.0f}) "
            f"within {cfg.structuring_window_minutes} min window"
        )
    else:
        reason = ""
    return RuleResult("structuring", score, reason, triggered)


def rule_velocity(
    tx: Dict[str, Any],
    sql_velocity: Dict[str, Any],
    cfg: AMLRuleConfig | None = None,
) -> RuleResult:
    """Rapid transaction velocity rule."""
    cfg    = cfg or AML_CFG
    score  = float(sql_velocity.get("velocity_score", 0.0))
    count  = int(sql_velocity.get("burst_count", 0))
    window = cfg.velocity_window_seconds

    triggered = count >= cfg.velocity_burst_threshold and score > 0.15
    if triggered:
        reason = (
            f"VELOCITY: {count} transactions from "
            f"{tx.get('source_entity', '?')} "
            f"in {window}s window"
        )
    else:
        reason = ""
    return RuleResult("velocity", score, reason, triggered)


def rule_circular_flow(
    tx: Dict[str, Any],
    graph_feats: Dict[str, Any],
) -> RuleResult:
    """Circular flow detection via graph cycle membership."""
    in_cycle = float(graph_feats.get("in_cycle", 0.0))
    score    = in_cycle  # binary from graph engine
    triggered = in_cycle > 0.5
    reason = (
        f"CIRCULAR FLOW: {tx.get('source_entity', '?')} "
        f"participates in a transaction cycle"
    ) if triggered else ""
    return RuleResult("circular_flow", score, reason, triggered)


def rule_layering(
    tx: Dict[str, Any],
    multi_hop_score: float,
    graph_feats: Dict[str, Any],
) -> RuleResult:
    """Multi-hop layering indicator."""
    bc    = float(graph_feats.get("betweenness_centrality", 0.0))
    score = min(1.0, 0.6 * multi_hop_score + 0.4 * min(bc * 10, 1.0))
    triggered = score > 0.35
    reason = ""
    if triggered:
        reason = (
            f"LAYERING: Multi-hop indicator {multi_hop_score:.2f}, "
            f"betweenness {bc:.4f} for "
            f"{tx.get('source_entity', '?')}"
        )
    return RuleResult("layering", score, reason, triggered)


def rule_high_risk_jurisdiction(
    tx: Dict[str, Any],
    cfg: AMLRuleConfig | None = None,
) -> RuleResult:
    """High-risk jurisdiction flag."""
    cfg  = cfg or AML_CFG
    src  = tx.get("src_jurisdiction", "")
    dst  = tx.get("dst_jurisdiction", "")
    hrj  = cfg.high_risk_jurisdictions

    src_flag = src in hrj
    dst_flag = dst in hrj
    both     = src_flag and dst_flag

    if both:
        score  = 1.0
        reason = (
            f"HIGH-RISK JX: Both jurisdictions flagged "
            f"({src} → {dst}) for ${tx.get('amount', 0):,.0f}"
        )
    elif src_flag or dst_flag:
        score  = 0.6
        flagged = src if src_flag else dst
        reason = (
            f"HIGH-RISK JX: Jurisdiction {flagged} flagged "
            f"(${tx.get('amount', 0):,.0f})"
        )
    else:
        score  = 0.0
        reason = ""

    triggered = score > 0.0
    return RuleResult("high_risk_jx", score, reason, triggered)


# ---------------------------------------------------------------------------
# Full rule evaluation
# ---------------------------------------------------------------------------
def evaluate_all_rules(
    tx: Dict[str, Any],
    sql_structuring: Dict[str, Any],
    sql_velocity: Dict[str, Any],
    graph_feats: Dict[str, Any],
    multi_hop_score: float,
    cfg: AMLRuleConfig | None = None,
) -> Dict[str, Any]:
    """
    Run all AML rules and return a consolidated result dict.
    """
    cfg = cfg or AML_CFG

    r_struct = rule_structuring(tx, sql_structuring, cfg)
    r_vel    = rule_velocity(tx, sql_velocity, cfg)
    r_circ   = rule_circular_flow(tx, graph_feats)
    r_layer  = rule_layering(tx, multi_hop_score, graph_feats)
    r_hrj    = rule_high_risk_jurisdiction(tx, cfg)

    all_rules: List[RuleResult] = [r_struct, r_vel, r_circ, r_layer, r_hrj]
    reasons   = [r.reason for r in all_rules if r.triggered and r.reason]

    return {
        "structuring_score": r_struct.score,
        "velocity_score":    r_vel.score,
        "circular_score":    r_circ.score,
        "layering_score":    r_layer.score,
        "high_risk_jx_score": r_hrj.score,
        "reasons":           reasons,
        "any_triggered":     any(r.triggered for r in all_rules),
        "rules":             {r.rule_name: r.score for r in all_rules},
    }
