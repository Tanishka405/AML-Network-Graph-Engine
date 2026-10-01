"""
tests/test_star_schema.py — Tests for scripts/build_analytics_dataset.py::export_star_schema()

Verifies grain (row counts match source cardinality), primary-key
uniqueness, and foreign-key integrity between the fact and dimension
exports -- against a small, hand-built fixture with a known answer,
not just "it ran without an exception."
"""
import csv
import importlib.util
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.database import init_db, insert_transactions, insert_alert


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
def isolated(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    monkeypatch.setattr("backend.database.get_db_path", lambda: db_path)
    monkeypatch.setattr(BUILD, "EXPORTS_DIR", str(tmp_path / "exports"))
    return db_path, tmp_path


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


def _load(tmp_path, name):
    with open(tmp_path / "exports" / name) as f:
        return list(csv.DictReader(f))


class TestGrain:
    """Each table's row count must equal its documented grain."""

    def test_fact_transactions_grain_is_one_row_per_transaction(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([_tx(f"T{i:03d}") for i in range(7)], db_path)
        BUILD.export_star_schema()
        rows = _load(tmp_path, "fact_transactions.csv")
        assert len(rows) == 7

    def test_fact_alerts_grain_is_one_row_per_alert(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([_tx(f"T{i:03d}") for i in range(5)], db_path)
        insert_alert(_alert("T000"), db_path)
        insert_alert(_alert("T002"), db_path)
        BUILD.export_star_schema()
        rows = _load(tmp_path, "fact_alerts.csv")
        assert len(rows) == 2

    def test_dim_entity_grain_is_one_row_per_distinct_entity(self, isolated):
        db_path, tmp_path = isolated
        # 3 transactions, but only 4 distinct entities involved
        insert_transactions([
            _tx("T001", source_entity="A", dest_entity="B"),
            _tx("T002", source_entity="A", dest_entity="C"),
            _tx("T003", source_entity="B", dest_entity="D"),
        ], db_path)
        BUILD.export_star_schema()
        rows = _load(tmp_path, "dim_entity.csv")
        assert {r["entity_id"] for r in rows} == {"A", "B", "C", "D"}
        assert len(rows) == 4

    def test_dim_date_grain_is_one_row_per_distinct_date(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([
            _tx("T001", timestamp="2024-06-01 09:00:00"),
            _tx("T002", timestamp="2024-06-01 15:00:00"),  # same date
            _tx("T003", timestamp="2024-06-02 09:00:00"),
        ], db_path)
        BUILD.export_star_schema()
        rows = _load(tmp_path, "dim_date.csv")
        assert {r["date_key"] for r in rows} == {"2024-06-01", "2024-06-02"}
        assert len(rows) == 2

    def test_dim_currency_only_includes_currencies_actually_present(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([
            _tx("T001", currency="USD"), _tx("T002", currency="EUR"),
        ], db_path)
        BUILD.export_star_schema()
        rows = _load(tmp_path, "dim_currency.csv")
        assert {r["currency_code"] for r in rows} == {"USD", "EUR"}


class TestPrimaryKeyUniqueness:
    def test_fact_transactions_tx_id_is_unique(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([_tx(f"T{i:03d}") for i in range(10)], db_path)
        BUILD.export_star_schema()
        rows = _load(tmp_path, "fact_transactions.csv")
        ids = [r["tx_id"] for r in rows]
        assert len(ids) == len(set(ids))

    def test_dim_entity_entity_id_is_unique(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([_tx(f"T{i:03d}", source_entity=f"E{i%3}") for i in range(10)], db_path)
        BUILD.export_star_schema()
        rows = _load(tmp_path, "dim_entity.csv")
        ids = [r["entity_id"] for r in rows]
        assert len(ids) == len(set(ids))


class TestForeignKeyIntegrity:
    def test_every_fact_transactions_key_resolves_to_a_dimension_row(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([
            _tx("T001", source_entity="A", dest_entity="B",
                currency="EUR", src_jurisdiction="US", dst_jurisdiction="UK",
                pattern_type="structuring", timestamp="2024-06-01 10:00:00"),
        ], db_path)
        BUILD.export_star_schema()
        fact = _load(tmp_path, "fact_transactions.csv")[0]
        entities = {r["entity_id"] for r in _load(tmp_path, "dim_entity.csv")}
        dates = {r["date_key"] for r in _load(tmp_path, "dim_date.csv")}
        currencies = {r["currency_code"] for r in _load(tmp_path, "dim_currency.csv")}
        jurisdictions = {r["jurisdiction_code"] for r in _load(tmp_path, "dim_jurisdiction.csv")}
        patterns = {r["pattern_type"] for r in _load(tmp_path, "dim_transaction_pattern.csv")}

        assert fact["source_entity_key"] in entities
        assert fact["dest_entity_key"] in entities
        assert fact["date_key"] in dates
        assert fact["currency_key"] in currencies
        assert fact["src_jurisdiction_key"] in jurisdictions
        assert fact["dst_jurisdiction_key"] in jurisdictions
        assert fact["pattern_type_key"] in patterns

    def test_every_fact_alerts_tx_id_exists_in_fact_transactions(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([_tx("T001"), _tx("T002")], db_path)
        insert_alert(_alert("T001"), db_path)
        BUILD.export_star_schema()
        fact_al = _load(tmp_path, "fact_alerts.csv")
        fact_tx_ids = {r["tx_id"] for r in _load(tmp_path, "fact_transactions.csv")}
        assert all(r["tx_id"] in fact_tx_ids for r in fact_al)

    def test_every_fact_alerts_risk_category_exists_in_dim_risk_category(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([_tx("T001")], db_path)
        insert_alert(_alert("T001", risk_label="CRITICAL"), db_path)
        BUILD.export_star_schema()
        fact_al = _load(tmp_path, "fact_alerts.csv")
        categories = {r["risk_category"] for r in _load(tmp_path, "dim_risk_category.csv")}
        assert all(r["risk_category_key"] in categories for r in fact_al)

    def test_is_flagged_matches_alert_existence(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([_tx("T001"), _tx("T002")], db_path)
        insert_alert(_alert("T001"), db_path)  # only T001 flagged
        BUILD.export_star_schema()
        by_id = {r["tx_id"]: r for r in _load(tmp_path, "fact_transactions.csv")}
        assert by_id["T001"]["is_flagged"] == "1"
        assert by_id["T002"]["is_flagged"] == "0"


class TestNoInventedColumns:
    """dim_risk_category thresholds and dim_jurisdiction.is_high_risk must
    come from the real config the risk engine actually uses, not a
    hardcoded/guessed value in the export code."""

    def test_risk_category_thresholds_match_real_config(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([_tx("T001")], db_path)
        BUILD.export_star_schema()
        from backend.config import RISK_THRESHOLDS
        rows = {r["risk_category"]: r["min_score_threshold"] for r in _load(tmp_path, "dim_risk_category.csv")}
        for label, threshold in RISK_THRESHOLDS.items():
            assert float(rows[label]) == threshold

    def test_high_risk_jurisdiction_flag_matches_real_config(self, isolated):
        db_path, tmp_path = isolated
        insert_transactions([
            _tx("T001", src_jurisdiction="KY", dst_jurisdiction="DE"),  # KY is high-risk
        ], db_path)
        BUILD.export_star_schema()
        from backend.config import AML_CFG
        rows = {r["jurisdiction_code"]: r["is_high_risk"] for r in _load(tmp_path, "dim_jurisdiction.csv")}
        assert rows["KY"] == "1"
        assert ("KY" in AML_CFG.high_risk_jurisdictions)
        assert rows["DE"] == "0"
        assert "DE" not in AML_CFG.high_risk_jurisdictions
