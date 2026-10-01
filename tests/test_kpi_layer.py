"""
tests/test_kpi_layer.py — Tests for the KPI computation functions in
scripts/build_analytics_dataset.py.

Imports the script as a module (it has no other package name) and checks
every KPI against a hand-computable fixture, so a change to a KPI's SQL
that silently changes its meaning fails a test instead of just changing
a number no one is watching.
"""
import importlib.util
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.database import init_db, insert_transactions, insert_alert, db_conn


def _load_build_script():
    spec = importlib.util.spec_from_file_location(
        "build_analytics_dataset",
        os.path.join(ROOT, "scripts", "build_analytics_dataset.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BUILD = _load_build_script()


@pytest.fixture
def tmp_db(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    init_db(path)
    # compute_kpis() uses db_conn() with no path -> patch get_db_path so
    # it resolves to our temp file without touching the real database.
    monkeypatch.setattr("backend.database.get_db_path", lambda: path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _tx(tx_id, **overrides):
    base = dict(
        tx_id=tx_id, timestamp="2024-06-01 10:00:00",
        source_entity="ENT_A", dest_entity="ENT_B",
        amount=1000.0, currency="USD",
        src_jurisdiction="US", dst_jurisdiction="UK",
        pattern_type="normal", inter_arrival_s=60.0,
    )
    base.update(overrides)
    return base


def _alert(tx_id, **overrides):
    base = dict(
        tx_id=tx_id, timestamp="2024-06-01 10:00:00",
        risk_score=0.7, risk_label="HIGH",
        anomaly_score=0.5, structuring_score=0.0, graph_score=0.0,
        velocity_score=0.0, circular_score=0.0, high_risk_jx_score=0.0,
        reason="test",
    )
    base.update(overrides)
    return base


class TestBasicVolumeKPIs:
    def test_total_transactions_and_value(self, tmp_db):
        insert_transactions([
            _tx("T001", amount=1000.0),
            _tx("T002", amount=2000.0),
            _tx("T003", amount=3000.0),
        ], tmp_db)
        kpis = BUILD.compute_kpis()
        assert kpis["total_transactions"] == 3
        assert kpis["total_transaction_value"] == pytest.approx(6000.0)
        assert kpis["average_transaction_value"] == pytest.approx(2000.0)

    def test_median_odd_count(self, tmp_db):
        insert_transactions([
            _tx("T001", amount=100.0),
            _tx("T002", amount=200.0),
            _tx("T003", amount=300.0),
        ], tmp_db)
        kpis = BUILD.compute_kpis()
        assert kpis["median_transaction_value"] == pytest.approx(200.0)

    def test_median_even_count_averages_middle_two(self, tmp_db):
        insert_transactions([
            _tx("T001", amount=100.0),
            _tx("T002", amount=200.0),
            _tx("T003", amount=300.0),
            _tx("T004", amount=400.0),
        ], tmp_db)
        kpis = BUILD.compute_kpis()
        # middle two are 200, 300 -> median 250
        assert kpis["median_transaction_value"] == pytest.approx(250.0)


class TestAlertKPIs:
    def test_alert_counts_and_rate(self, tmp_db):
        insert_transactions([_tx(f"T{i:03d}") for i in range(10)], tmp_db)
        insert_alert(_alert("T000", risk_label="CRITICAL"), tmp_db)
        insert_alert(_alert("T001", risk_label="HIGH"), tmp_db)
        kpis = BUILD.compute_kpis()
        assert kpis["total_alerts"] == 2
        assert kpis["critical_alerts"] == 1
        assert kpis["high_alerts"] == 1
        assert kpis["suspicious_transaction_rate_pct"] == pytest.approx(20.0)

    def test_flagged_value_percentage(self, tmp_db):
        insert_transactions([
            _tx("T001", amount=1000.0),
            _tx("T002", amount=9000.0),
        ], tmp_db)
        insert_alert(_alert("T001"), tmp_db)
        kpis = BUILD.compute_kpis()
        assert kpis["flagged_transaction_value"] == pytest.approx(1000.0)
        assert kpis["flagged_transaction_value_pct"] == pytest.approx(10.0)

    def test_no_alerts_gives_zero_not_error(self, tmp_db):
        insert_transactions([_tx("T001")], tmp_db)
        kpis = BUILD.compute_kpis()
        assert kpis["total_alerts"] == 0
        assert kpis["suspicious_transaction_rate_pct"] == 0.0
        assert kpis["average_risk_score"] == 0.0


class TestStructuringAndCircularKPIs:
    def test_structuring_alert_count(self, tmp_db):
        # structuring_alert_count counts alerts where the structuring rule
        # actually TRIGGERED (reason contains "STRUCTURING:"), not merely
        # alerts with a nonzero structuring_score -- see
        # docs/analytics.md "Rule trigger frequency vs. raw signal
        # magnitude" for the investigation that fixed this. T002 has a
        # nonzero score but no matching reason, i.e. the rule computed a
        # score but did not cross its trigger threshold -- a real,
        # possible case, and it must NOT be counted.
        insert_transactions([_tx("T001"), _tx("T002")], tmp_db)
        insert_alert(_alert(
            "T001", structuring_score=0.5,
            reason="STRUCTURING: 4 sub-threshold transactions totalling "
                   "$20,000 (avg $5,000) within 60 min window",
        ), tmp_db)
        insert_alert(_alert("T002", structuring_score=0.05, reason="VELOCITY: x"), tmp_db)
        kpis = BUILD.compute_kpis()
        assert kpis["structuring_alert_count"] == 1

    def test_circular_flow_entity_count(self, tmp_db):
        # Likewise gated on the rule having actually triggered, read back
        # from `reason`, not `circular_score > 0` in isolation.
        insert_transactions([
            _tx("T001", source_entity="ENT_X", dest_entity="ENT_Y"),
        ], tmp_db)
        insert_alert(_alert(
            "T001", circular_score=0.8,
            reason="CIRCULAR FLOW: ENT_X participates in a transaction cycle",
        ), tmp_db)
        kpis = BUILD.compute_kpis()
        # both source and dest of the flagged tx count as involved
        assert kpis["circular_flow_entity_count"] == 2


class TestKPIExport:
    def test_export_kpis_writes_csv_and_json(self, tmp_db, tmp_path, monkeypatch):
        insert_transactions([_tx("T001")], tmp_db)
        monkeypatch.setattr(BUILD, "EXPORTS_DIR", str(tmp_path))
        kpis = BUILD.compute_kpis()
        BUILD.export_kpis(kpis)
        assert os.path.exists(tmp_path / "kpi_summary.csv")
        assert os.path.exists(tmp_path / "kpi_summary.json")
        import json
        with open(tmp_path / "kpi_summary.json") as f:
            loaded = json.load(f)
        assert loaded["total_transactions"] == kpis["total_transactions"]
