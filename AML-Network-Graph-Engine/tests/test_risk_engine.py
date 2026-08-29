"""
tests/test_risk_engine.py — Risk scoring, AML rules, and Isolation Forest tests
"""
import os, sys, pytest
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.aml_rules import (
    rule_structuring, rule_velocity, rule_circular_flow,
    rule_layering, rule_high_risk_jurisdiction, evaluate_all_rules,
)
from backend.risk_engine import compute_composite_risk, enrich_transaction, is_alert
from backend.anomaly_detector import AMLAnomalyDetector
from backend.config import RiskWeights, AMLRuleConfig, IFConfig
from backend.feature_engineering import extract_features, N_FEATURES, FEATURE_NAMES


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def normal_tx():
    return dict(
        tx_id="N001", timestamp="2024-01-01 10:00:00",
        source_entity="ENT_A", dest_entity="ENT_B",
        amount=5000.0, currency="USD",
        src_jurisdiction="US", dst_jurisdiction="US",
        pattern_type="normal", inter_arrival_s=60.0,
    )

def high_risk_tx():
    return dict(
        tx_id="HR001", timestamp="2024-01-01 10:00:00",
        source_entity="ENT_X", dest_entity="ENT_Y",
        amount=250_000.0, currency="AED",
        src_jurisdiction="KY", dst_jurisdiction="VG",
        pattern_type="high_risk_jx", inter_arrival_s=1.0,
    )


# ─── AML Rule Tests ───────────────────────────────────────────────────────────

class TestStructuringRule:
    def test_triggered_on_high_score(self):
        sql_data = {"structuring_score": 0.9, "window_count": 7, "window_sum": 67420.0, "window_avg": 9631.0}
        tx = normal_tx()
        tx["amount"] = 9500.0
        r = rule_structuring(tx, sql_data)
        assert r.triggered
        assert r.score >= 0.8
        assert "STRUCTURING" in r.reason
        assert "7" in r.reason

    def test_not_triggered_on_zero_score(self):
        sql_data = {"structuring_score": 0.0, "window_count": 0}
        r = rule_structuring(normal_tx(), sql_data)
        assert not r.triggered
        assert r.score == 0.0

    def test_not_triggered_for_large_amount(self):
        """Large single transaction should not trigger structuring even with high sql score."""
        tx = normal_tx()
        tx["amount"] = 500_000.0  # above threshold
        sql_data = {"structuring_score": 0.5, "window_count": 5}
        r = rule_structuring(tx, sql_data)
        assert not r.triggered   # amount >= threshold

    def test_reason_contains_window_stats(self):
        sql_data = {"structuring_score": 0.85, "window_count": 5, "window_sum": 47000.0, "window_avg": 9400.0}
        tx = normal_tx()
        tx["amount"] = 9400.0
        r = rule_structuring(tx, sql_data)
        assert r.triggered
        assert "$47,000" in r.reason or "47000" in r.reason or "47,000" in r.reason


class TestVelocityRule:
    def test_triggered_on_burst(self):
        sql_vel = {"velocity_score": 0.8, "burst_count": 12}
        r = rule_velocity(normal_tx(), sql_vel)
        assert r.triggered
        assert "12" in r.reason

    def test_not_triggered_below_threshold(self):
        r = rule_velocity(normal_tx(), {"velocity_score": 0.05, "burst_count": 2})
        assert not r.triggered


class TestCircularFlowRule:
    def test_triggered_when_in_cycle(self):
        gf = {"in_cycle": 1.0}
        r = rule_circular_flow(normal_tx(), gf)
        assert r.triggered
        assert r.score == 1.0

    def test_not_triggered_when_not_in_cycle(self):
        r = rule_circular_flow(normal_tx(), {"in_cycle": 0.0})
        assert not r.triggered


class TestLayeringRule:
    def test_triggered_on_multi_hop(self):
        gf = {"betweenness_centrality": 0.5}
        r = rule_layering(normal_tx(), multi_hop_score=0.8, graph_feats=gf)
        assert r.triggered
        assert r.score > 0.3

    def test_not_triggered_on_zero(self):
        r = rule_layering(normal_tx(), multi_hop_score=0.0, graph_feats={"betweenness_centrality":0.0})
        assert not r.triggered


class TestHighRiskJxRule:
    def test_both_flagged(self):
        tx = high_risk_tx()
        r = rule_high_risk_jurisdiction(tx)
        assert r.triggered
        assert r.score == 1.0
        assert "Both" in r.reason

    def test_one_flagged(self):
        tx = normal_tx()
        tx["src_jurisdiction"] = "KY"
        r = rule_high_risk_jurisdiction(tx)
        assert r.triggered
        assert r.score == 0.6

    def test_none_flagged(self):
        r = rule_high_risk_jurisdiction(normal_tx())
        assert not r.triggered
        assert r.score == 0.0

    def test_reason_includes_amount(self):
        r = rule_high_risk_jurisdiction(high_risk_tx())
        assert "250,000" in r.reason or "250000" in r.reason


# ─── Composite Risk Score ─────────────────────────────────────────────────────

class TestCompositeRisk:
    def test_output_in_zero_one(self):
        for _ in range(20):
            score = compute_composite_risk(
                *[np.random.uniform(0,1) for _ in range(6)]
            )
            assert 0.0 <= score <= 1.0, f"Score {score} out of range"

    def test_all_zeros_give_zero(self):
        assert compute_composite_risk(0,0,0,0,0,0) == 0.0

    def test_all_ones_give_one(self):
        assert compute_composite_risk(1,1,1,1,1,1) == 1.0

    def test_weights_respected(self):
        """IF score has highest weight — should dominate."""
        w = RiskWeights()
        high_if  = compute_composite_risk(1.0, 0, 0, 0, 0, 0, weights=w)
        high_hrj = compute_composite_risk(0, 0, 0, 0, 0, 1.0, weights=w)
        assert high_if > high_hrj

    def test_is_alert_threshold(self):
        assert is_alert({"composite_risk": 0.75}, threshold=0.60)
        assert not is_alert({"composite_risk": 0.45}, threshold=0.60)


# ─── Feature Engineering ──────────────────────────────────────────────────────

class TestFeatureEngineering:
    def test_feature_count(self):
        tx = normal_tx()
        gf = {k: 0.0 for k in ["betweenness_centrality","eigenvector_centrality",
                                  "pagerank","in_degree","out_degree",
                                  "weighted_in_degree","weighted_out_degree",
                                  "tx_count","total_incoming_amount",
                                  "total_outgoing_amount","community_id",
                                  "community_size","in_cycle"]}
        fv = extract_features(tx, {}, {}, gf, 0.0, 0.0)
        assert len(fv) == N_FEATURES
        assert len(FEATURE_NAMES) == N_FEATURES

    def test_no_nan_in_features(self):
        tx = normal_tx()
        gf = {k: 0.0 for k in ["betweenness_centrality","eigenvector_centrality",
                                  "pagerank","in_degree","out_degree",
                                  "weighted_in_degree","weighted_out_degree",
                                  "tx_count","total_incoming_amount",
                                  "total_outgoing_amount","community_id",
                                  "community_size","in_cycle"]}
        fv = extract_features(tx, {}, {}, gf, 0.0, 0.0)
        assert not np.any(np.isnan(fv)), "NaN in feature vector"
        assert not np.any(np.isinf(fv)), "Inf in feature vector"

    def test_high_risk_jx_flag(self):
        tx = high_risk_tx()
        gf = {k: 0.0 for k in ["betweenness_centrality","eigenvector_centrality",
                                  "pagerank","in_degree","out_degree",
                                  "weighted_in_degree","weighted_out_degree",
                                  "tx_count","total_incoming_amount",
                                  "total_outgoing_amount","community_id",
                                  "community_size","in_cycle"]}
        fv = extract_features(tx, {}, {}, gf, 0.0, 0.0)
        # high_risk_jx_flag is index 3
        idx = FEATURE_NAMES.index("high_risk_jx_flag")
        assert fv[idx] == 1.0


# ─── Isolation Forest ─────────────────────────────────────────────────────────

class TestIsolationForest:
    def _make_detector(self, n_train=600):
        det = AMLAnomalyDetector(IFConfig(
            n_estimators=100, contamination=0.1,
            random_state=42, min_train_samples=500, retrain_interval=99999
        ))
        rng = np.random.default_rng(42)
        # Normal-ish feature vectors
        for _ in range(n_train):
            vec = rng.normal(0, 1, N_FEATURES).astype(np.float64)
            det.add_sample(vec)
        det._retrain()
        return det

    def test_model_warm_after_training(self):
        det = self._make_detector()
        assert det.is_warm

    def test_score_in_zero_one(self):
        det = self._make_detector()
        rng = np.random.default_rng(1)
        for _ in range(20):
            vec = rng.normal(0, 1, N_FEATURES).astype(np.float64)
            s = det.score(vec)
            assert 0.0 <= s <= 1.0, f"Score {s} out of range"

    def test_extreme_outlier_scores_higher(self):
        """An extreme outlier should score higher than an inlier on average."""
        det = self._make_detector()
        rng = np.random.default_rng(99)

        normal_scores = [det.score(rng.normal(0, 1, N_FEATURES).astype(np.float64))
                         for _ in range(50)]
        outlier_scores = [det.score(rng.normal(10, 0.1, N_FEATURES).astype(np.float64))
                          for _ in range(50)]

        assert np.mean(outlier_scores) > np.mean(normal_scores), (
            f"Mean outlier {np.mean(outlier_scores):.4f} should > normal {np.mean(normal_scores):.4f}"
        )

    def test_score_zero_before_warmup(self):
        det = AMLAnomalyDetector(IFConfig(min_train_samples=1000))
        vec = np.zeros(N_FEATURES, dtype=np.float64)
        assert det.score(vec) == 0.0

    def test_batch_scoring_consistent(self):
        det = self._make_detector()
        rng = np.random.default_rng(7)
        vecs = rng.normal(0, 1, (10, N_FEATURES)).astype(np.float64)
        batch_scores = det.score_batch(vecs)
        individual = [det.score(v) for v in vecs]
        np.testing.assert_allclose(batch_scores, individual, atol=1e-6)
