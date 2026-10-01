"""
tests/test_sql_analytics.py — Tests for sql/analytics/*.sql

These tests do NOT re-derive business meaning; they pin down structural
correctness against a small, hand-built fixture dataset with known
answers, so a future schema change or query edit that breaks a query's
logic fails loudly here instead of silently producing wrong numbers in
a Power BI report. Every query file is also executed for pure syntax
validity against a larger, realistic dataset.
"""
import glob
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SQL_DIR = os.path.join(ROOT, "sql", "analytics")

from backend.database import init_db, insert_transactions, insert_alert, db_conn


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _tx(tx_id, **overrides):
    base = dict(
        tx_id=tx_id, timestamp="2024-06-01 10:00:00",
        source_entity="ENT_A", dest_entity="ENT_B",
        amount=5000.0, currency="USD",
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
        reason="test alert",
    )
    base.update(overrides)
    return base


def _strip_comments(sql: str) -> str:
    return "\n".join(
        line[: line.find("--")] if "--" in line else line
        for line in sql.split("\n")
    )


def _statements(filename: str):
    raw = open(os.path.join(SQL_DIR, filename)).read()
    return [s.strip() for s in _strip_comments(raw).split(";") if s.strip()]


ALL_SQL_FILES = sorted(os.path.basename(f) for f in glob.glob(os.path.join(SQL_DIR, "*.sql")))


class TestAllQueriesAreValidSQL:
    """Every statement in every file must at least execute without error
    against a populated database -- this is the regression test for the
    kind of bug caught during development (the invalid_currency check's
    parameter-binding bug, for example)."""

    @pytest.mark.parametrize("filename", ALL_SQL_FILES)
    def test_all_statements_execute(self, tmp_db, filename):
        # Populate a modestly-sized, varied dataset so every query
        # (including ones with HAVING/window-frame edge cases) has
        # something real to run against.
        txs = []
        for i in range(30):
            txs.append(_tx(
                f"T{i:03d}",
                source_entity=f"ENT_{i % 5}",
                dest_entity=f"ENT_{(i + 1) % 5}",
                amount=1000.0 * (i + 1),
                timestamp=f"2024-06-{(i % 28) + 1:02d} 10:00:00",
                currency=["USD", "EUR"][i % 2],
                pattern_type=["normal", "structuring", "circular_flow"][i % 3],
            ))
        insert_transactions(txs, tmp_db)
        for i in range(0, 30, 3):
            insert_alert(_alert(
                f"T{i:03d}",
                risk_score=0.5 + (i % 5) / 10,
                risk_label=["LOW", "MEDIUM", "HIGH", "CRITICAL"][i % 4],
                structuring_score=0.6 if i % 6 == 0 else 0.0,
                graph_score=0.4 if i % 9 == 0 else 0.0,
                circular_score=0.3 if i % 12 == 0 else 0.0,
            ), tmp_db)

        with db_conn(tmp_db) as conn:
            for stmt in _statements(filename):
                conn.execute(stmt).fetchall()  # raises on syntax/schema error

    def test_data_quality_report_query_matches_python_checks(self, tmp_db):
        """12_data_quality.sql should surface exactly what
        backend/data_quality.py wrote -- same row count."""
        from backend import data_quality as dq
        insert_transactions([_tx("T001")], tmp_db)
        report = dq.run_and_persist(path=tmp_db)
        with db_conn(tmp_db) as conn:
            stmt = _statements("12_data_quality.sql")[0]
            rows = conn.execute(stmt).fetchall()
        assert len(rows) == len(dq.CHECK_FUNCS)
        assert len(rows) == len(report["checks"])


class TestRuleTriggerVsRawScoreSemantics:
    """Regression tests for the CASE B bug found while investigating why
    rule_frequency.csv showed structuring at ~100% of alerts: several
    queries used `<component>_score > 0` as a proxy for "the rule fired",
    but structuring_score and velocity_score are raw continuous scores
    that can be > 0 without RuleResult.triggered ever being True. The
    authoritative signal is `alerts.reason`, which
    backend/aml_rules.py::evaluate_all_rules() populates ONLY from rules
    where triggered=True. These tests build a fixture where a score is
    nonzero but the rule did NOT trigger, and a second case where it did,
    and assert the corrected queries get both right -- so a future
    regression back to `score > 0` fails these tests immediately."""

    def test_nonzero_score_without_trigger_is_not_counted(self, tmp_db):
        # structuring_score is nonzero (0.05) but reason has no
        # STRUCTURING: marker -- i.e. the raw SQL score was computed but
        # rule_structuring()'s threshold (count>=3, amount<10000,
        # score>0.1) was not met, exactly like a real untriggered case.
        insert_transactions([_tx("T001")], tmp_db)
        insert_alert(_alert(
            "T001", structuring_score=0.05,
            reason="VELOCITY: 6 transactions from ENT_A in 300s window",
        ), tmp_db)
        with db_conn(tmp_db) as conn:
            rows = {r["rule_name"]: dict(r) for r in
                    conn.execute(_statements("10_alert_analysis.sql")[1]).fetchall()}
        assert rows["structuring"]["alert_contributions"] == 0

    def test_triggered_rule_with_score_above_threshold_is_counted(self, tmp_db):
        insert_transactions([_tx("T001")], tmp_db)
        insert_alert(_alert(
            "T001", structuring_score=0.83,
            reason="STRUCTURING: 5 sub-threshold transactions totalling $30,000 "
                   "(avg $6,000) within 60 min window",
        ), tmp_db)
        with db_conn(tmp_db) as conn:
            rows = {r["rule_name"]: dict(r) for r in
                    conn.execute(_statements("10_alert_analysis.sql")[1]).fetchall()}
        assert rows["structuring"]["alert_contributions"] == 1
        assert rows["structuring"]["avg_score_when_triggered"] == pytest.approx(0.83)

    def test_velocity_same_semantics_as_structuring(self, tmp_db):
        insert_transactions([_tx("T001"), _tx("T002")], tmp_db)
        # T001: nonzero score, not triggered
        insert_alert(_alert("T001", velocity_score=0.1, reason="HIGH-RISK JX: x"), tmp_db)
        # T002: triggered
        insert_alert(_alert("T002", velocity_score=0.9,
                             reason="VELOCITY: 8 transactions from ENT_B in 300s window"), tmp_db)
        with db_conn(tmp_db) as conn:
            rows = {r["rule_name"]: dict(r) for r in
                    conn.execute(_statements("10_alert_analysis.sql")[1]).fetchall()}
        assert rows["velocity"]["alert_contributions"] == 1
        assert rows["velocity"]["avg_score_when_triggered"] == pytest.approx(0.9)

    def test_structuring_analysis_query_excludes_untriggered_alerts(self, tmp_db):
        """06_structuring_analysis.sql must show the same corrected
        semantics as rule_frequency -- an entity with only an untriggered
        nonzero structuring_score should not appear as a structuring
        entity at all."""
        insert_transactions([
            _tx("T001", source_entity="ENT_QUIET"),
            _tx("T002", source_entity="ENT_LOUD"),
        ], tmp_db)
        insert_alert(_alert("T001", structuring_score=0.05, reason="VELOCITY: x"), tmp_db)
        insert_alert(_alert("T002", structuring_score=0.9,
                             reason="STRUCTURING: 6 sub-threshold transactions "
                                     "totalling $40,000 (avg $6,666) within 60 min window"), tmp_db)
        with db_conn(tmp_db) as conn:
            rows = {r["source_entity"]: dict(r) for r in
                    conn.execute(_statements("06_structuring_analysis.sql")[0]).fetchall()}
        assert "ENT_QUIET" not in rows
        assert rows["ENT_LOUD"]["structuring_alert_count"] == 1

    def test_continuous_signals_reported_separately_not_as_rule_frequency(self, tmp_db):
        """graph_score and anomaly_score are not aml_rules.py rules (no
        RuleResult/triggered concept exists for them) -- they must appear
        in the continuous-signal query (statement 2), not be mixed into
        the rule-trigger-frequency query (statement 1)."""
        insert_transactions([_tx("T001")], tmp_db)
        insert_alert(_alert("T001", graph_score=0.4, anomaly_score=0.7,
                             reason="STRUCTURING: x"), tmp_db)
        with db_conn(tmp_db) as conn:
            rule_freq_names = {r["rule_name"] for r in
                                conn.execute(_statements("10_alert_analysis.sql")[1]).fetchall()}
            signal_names = {r["signal_name"] for r in
                             conn.execute(_statements("10_alert_analysis.sql")[2]).fetchall()}
        assert "graph_centrality" not in rule_freq_names
        assert "isolation_forest_anomaly" not in rule_freq_names
        assert {"graph_centrality", "isolation_forest_anomaly"} == signal_names

    def test_kpi_structuring_alert_count_uses_trigger_not_raw_score(self, tmp_db, monkeypatch):
        """scripts/build_analytics_dataset.py::compute_kpis()'s
        structuring_alert_count had the identical CASE B bug."""
        monkeypatch.setattr("backend.database.get_db_path", lambda: tmp_db)
        insert_transactions([_tx("T001"), _tx("T002")], tmp_db)
        insert_alert(_alert("T001", structuring_score=0.05, reason="VELOCITY: x"), tmp_db)
        insert_alert(_alert("T002", structuring_score=0.9,
                             reason="STRUCTURING: 5 sub-threshold transactions "
                                     "totalling $30,000 (avg $6,000) within 60 min window"), tmp_db)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_analytics_dataset",
            os.path.join(ROOT, "scripts", "build_analytics_dataset.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        kpis = mod.compute_kpis()
        assert kpis["structuring_alert_count"] == 1  # not 2


class TestEntityActivityKnownAnswer:
    """03_entity_activity.sql against a fixture with a hand-computable
    answer: ENT_A sends 3 transactions of $100, $200, $300 -- total value
    and count must match exactly, and ranking must put it first since
    it's the only entity."""

    def test_single_entity_totals_are_exact(self, tmp_db):
        txs = [
            _tx("T001", source_entity="ENT_A", dest_entity="ENT_X", amount=100.0),
            _tx("T002", source_entity="ENT_A", dest_entity="ENT_X", amount=200.0),
            _tx("T003", source_entity="ENT_A", dest_entity="ENT_X", amount=300.0),
        ]
        insert_transactions(txs, tmp_db)
        with db_conn(tmp_db) as conn:
            stmt = _statements("03_entity_activity.sql")[0]
            rows = {r["entity"]: dict(r) for r in conn.execute(stmt).fetchall()}
        assert rows["ENT_A"]["outgoing_count"] == 3
        assert rows["ENT_A"]["total_value"] == pytest.approx(600.0 + 0.0)
        # ENT_X received all 3 as incoming, sent none
        assert rows["ENT_X"]["incoming_count"] == 3
        assert rows["ENT_X"]["outgoing_count"] == 0


class TestHighRiskEntitiesKnownAnswer:
    """04_high_risk_entities.sql: an entity with two alerts averaging a
    known risk score must report that exact average."""

    def test_average_risk_score_is_exact(self, tmp_db):
        insert_transactions([
            _tx("T001", source_entity="ENT_RISKY", dest_entity="ENT_X"),
            _tx("T002", source_entity="ENT_RISKY", dest_entity="ENT_X"),
        ], tmp_db)
        insert_alert(_alert("T001", risk_score=0.6, risk_label="HIGH"), tmp_db)
        insert_alert(_alert("T002", risk_score=0.8, risk_label="CRITICAL"), tmp_db)
        with db_conn(tmp_db) as conn:
            stmt = _statements("04_high_risk_entities.sql")[0]
            rows = {r["entity"]: dict(r) for r in conn.execute(stmt).fetchall()}
        assert rows["ENT_RISKY"]["alert_count"] == 2
        assert rows["ENT_RISKY"]["avg_risk_score"] == pytest.approx(0.7)
        assert rows["ENT_RISKY"]["critical_alerts"] == 1
        assert rows["ENT_RISKY"]["high_alerts"] == 1


class TestRiskRankingWindowFunctions:
    """08_risk_rankings.sql: RANK() must leave gaps after ties while
    DENSE_RANK() must not -- this is the textbook distinction between the
    two functions, verified against a fixture with a deliberate tie."""

    def test_rank_vs_dense_rank_on_tied_scores(self, tmp_db):
        insert_transactions([
            _tx("T001", source_entity="ENT_A"),
            _tx("T002", source_entity="ENT_B"),
            _tx("T003", source_entity="ENT_C"),
        ], tmp_db)
        # ENT_A and ENT_B tie at 0.9 (peak_risk_score = MAX per entity);
        # ENT_C is lower at 0.5.
        insert_alert(_alert("T001", risk_score=0.9), tmp_db)
        insert_alert(_alert("T002", risk_score=0.9), tmp_db)
        insert_alert(_alert("T003", risk_score=0.5), tmp_db)
        with db_conn(tmp_db) as conn:
            stmt = _statements("08_risk_rankings.sql")[0]
            rows = {r["entity"]: dict(r) for r in conn.execute(stmt).fetchall()}
        # Tied entities both get rank 1 under RANK() and DENSE_RANK()
        assert rows["ENT_A"]["rank_with_ties"] == 1
        assert rows["ENT_B"]["rank_with_ties"] == 1
        assert rows["ENT_A"]["dense_rank_with_ties"] == 1
        # RANK() skips to 3 after a 2-way tie for 1st; DENSE_RANK() does not
        assert rows["ENT_C"]["rank_with_ties"] == 3
        assert rows["ENT_C"]["dense_rank_with_ties"] == 2


class TestAlertAnalysisPercentages:
    """10_alert_analysis.sql part 1: flagged-value percentage must equal
    flagged_value / total_value exactly, hand-computed."""

    def test_flagged_value_percentage_is_exact(self, tmp_db):
        insert_transactions([
            _tx("T001", amount=1000.0),   # flagged
            _tx("T002", amount=3000.0),   # not flagged
        ], tmp_db)
        insert_alert(_alert("T001", risk_label="HIGH"), tmp_db)
        with db_conn(tmp_db) as conn:
            stmt = _statements("10_alert_analysis.sql")[0]
            rows = {r["risk_label"]: dict(r) for r in conn.execute(stmt).fetchall()}
        assert rows["HIGH"]["flagged_value"] == pytest.approx(1000.0)
        # 1000 / (1000 + 3000) = 25%
        assert rows["HIGH"]["pct_of_total_value"] == pytest.approx(25.0)


class TestRollingWindowFunctions:
    """09_temporal_analysis.sql part 1: a 7-day rolling sum over exactly
    3 days of data with known daily counts must equal the plain sum of
    those 3 days (window is wider than the data, so rolling == cumulative)."""

    def test_rolling_7d_equals_cumulative_when_fewer_than_7_days(self, tmp_db):
        insert_transactions([
            _tx("T001", timestamp="2024-06-01 10:00:00", amount=100.0),
            _tx("T002", timestamp="2024-06-02 10:00:00", amount=200.0),
            _tx("T003", timestamp="2024-06-03 10:00:00", amount=300.0),
        ], tmp_db)
        with db_conn(tmp_db) as conn:
            stmt = _statements("09_temporal_analysis.sql")[0]
            rows = conn.execute(stmt).fetchall()
        by_date = {r["tx_date"]: dict(r) for r in rows}
        assert by_date["2024-06-01"]["rolling_7d_tx_count"] == 1
        assert by_date["2024-06-02"]["rolling_7d_tx_count"] == 2
        assert by_date["2024-06-03"]["rolling_7d_tx_count"] == 3
        assert by_date["2024-06-03"]["rolling_7d_value"] == pytest.approx(600.0)
