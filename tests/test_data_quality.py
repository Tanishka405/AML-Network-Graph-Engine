"""
tests/test_data_quality.py — Tests for backend/data_quality.py

Follows the same tmp_db fixture convention as tests/test_sql.py: every
test gets an isolated, empty SQLite database so checks can be tested
against deliberately-injected bad rows without touching the real
data/aml.db used by scripts/build_analytics_dataset.py.
"""
import os
import sys
import tempfile
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.database import init_db, insert_transactions, db_conn
from backend import data_quality as dq


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


class TestCleanDataPasses:
    def test_all_checks_pass_on_clean_data(self, tmp_db):
        txs = [_tx(f"T{i:03d}", source_entity=f"ENT_{i}") for i in range(20)]
        insert_transactions(txs, tmp_db)
        results = dq.run_all_checks(tmp_db)
        failed = [r for r in results if r.status == "FAIL"]
        assert failed == [], f"Unexpected failures on clean data: {failed}"

    def test_returns_one_result_per_registered_check(self, tmp_db):
        insert_transactions([_tx("T001")], tmp_db)
        results = dq.run_all_checks(tmp_db)
        assert len(results) == len(dq.CHECK_FUNCS)


class TestInvalidAmountDetection:
    def test_negative_amount_is_flagged(self, tmp_db):
        txs = [_tx("T001", amount=-500.0), _tx("T002", amount=1000.0)]
        insert_transactions(txs, tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "invalid_amounts")
        assert r.violations == 1
        assert r.status in ("WARN", "FAIL")

    def test_zero_amount_is_flagged(self, tmp_db):
        insert_transactions([_tx("T001", amount=0.0)], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "invalid_amounts")
        assert r.violations == 1

    def test_positive_amount_not_flagged(self, tmp_db):
        insert_transactions([_tx("T001", amount=100.0)], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "invalid_amounts")
        assert r.violations == 0
        assert r.status == "PASS"


class TestCurrencyAndJurisdiction:
    def test_invalid_currency_is_flagged(self, tmp_db):
        insert_transactions([_tx("T001", currency="ZZZ")], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "invalid_currency")
        assert r.violations == 1

    def test_valid_currency_not_flagged(self, tmp_db):
        insert_transactions([_tx("T001", currency="EUR")], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "invalid_currency")
        assert r.violations == 0

    def test_invalid_jurisdiction_is_flagged(self, tmp_db):
        insert_transactions([_tx("T001", src_jurisdiction="ZZ")], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "invalid_jurisdiction")
        assert r.violations == 1


class TestSelfTransactionDetection:
    def test_self_transaction_is_flagged(self, tmp_db):
        insert_transactions(
            [_tx("T001", source_entity="ENT_A", dest_entity="ENT_A")], tmp_db
        )
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "self_transactions")
        assert r.violations == 1

    def test_normal_transaction_not_flagged_as_self(self, tmp_db):
        insert_transactions(
            [_tx("T001", source_entity="ENT_A", dest_entity="ENT_B")], tmp_db
        )
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "self_transactions")
        assert r.violations == 0


class TestTimestampChecks:
    def test_future_timestamp_is_flagged(self, tmp_db):
        insert_transactions([_tx("T001", timestamp="2099-01-01 00:00:00")], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "future_timestamps")
        assert r.violations == 1

    def test_unparseable_timestamp_is_flagged(self, tmp_db):
        insert_transactions([_tx("T001", timestamp="not-a-date")], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "invalid_timestamps")
        assert r.violations == 1

    def test_past_timestamp_not_flagged_as_future(self, tmp_db):
        insert_transactions([_tx("T001", timestamp="2020-01-01 00:00:00")], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "future_timestamps")
        assert r.violations == 0


class TestPatternTypeValidation:
    def test_invalid_pattern_type_is_flagged(self, tmp_db):
        insert_transactions([_tx("T001", pattern_type="not_a_real_pattern")], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "invalid_transaction_type")
        assert r.violations == 1

    @pytest.mark.parametrize("pattern", sorted(dq.VALID_PATTERN_TYPES))
    def test_every_real_generator_pattern_type_is_accepted(self, tmp_db, pattern):
        """Regression test for the bug caught while building this module:
        an earlier version of VALID_PATTERN_TYPES assumed variable names
        ("smurfing"/"circular"/"velocity") instead of the generator's
        actual string literals, and wrongly failed ~36% of legitimate
        transactions. This test pins the check against every value the
        generator can actually produce."""
        insert_transactions([_tx("T001", pattern_type=pattern)], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "invalid_transaction_type")
        assert r.violations == 0, f"pattern_type={pattern!r} should be valid"


class TestBlankEntityIds:
    def test_blank_source_entity_is_flagged(self, tmp_db):
        insert_transactions([_tx("T001", source_entity="   ")], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "blank_entity_ids")
        assert r.violations == 1

    def test_empty_string_dest_entity_is_flagged(self, tmp_db):
        insert_transactions([_tx("T001", dest_entity="")], tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "blank_entity_ids")
        assert r.violations == 1


class TestDuplicateRecordDetection:
    def test_duplicate_business_key_is_flagged(self, tmp_db):
        # Same source/dest/amount/currency/timestamp under two different
        # tx_ids -- the same economic event inserted twice.
        txs = [
            _tx("T001", source_entity="ENT_A", dest_entity="ENT_B",
                amount=1234.0, timestamp="2024-06-01 09:00:00"),
            _tx("T002", source_entity="ENT_A", dest_entity="ENT_B",
                amount=1234.0, timestamp="2024-06-01 09:00:00"),
        ]
        insert_transactions(txs, tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "duplicate_transaction_records")
        assert r.violations == 1  # 2 rows -> 1 "extra" beyond the first

    def test_distinct_transactions_not_flagged_as_duplicates(self, tmp_db):
        txs = [_tx(f"T{i:03d}", amount=1000.0 + i) for i in range(5)]
        insert_transactions(txs, tmp_db)
        results = dq.run_all_checks(tmp_db)
        r = next(r for r in results if r.check_name == "duplicate_transaction_records")
        assert r.violations == 0


class TestReportGeneration:
    def test_summarize_counts_statuses_correctly(self, tmp_db):
        insert_transactions([_tx("T001", amount=-1.0)], tmp_db)  # forces a violation
        results = dq.run_all_checks(tmp_db)
        summary = dq.summarize(results)
        assert summary["total_checks"] == len(dq.CHECK_FUNCS)
        assert summary["passed"] + summary["warned"] + summary["failed"] == len(results)

    def test_run_and_persist_writes_to_db(self, tmp_db):
        insert_transactions([_tx("T001")], tmp_db)
        report = dq.run_and_persist(path=tmp_db)
        assert "summary" in report and "checks" in report
        with db_conn(tmp_db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM data_quality_results").fetchone()[0]
        assert n == len(dq.CHECK_FUNCS)

    def test_run_and_persist_writes_json_report(self, tmp_db, tmp_path):
        insert_transactions([_tx("T001")], tmp_db)
        json_out = str(tmp_path / "dq_report.json")
        report = dq.run_and_persist(path=tmp_db, json_out=json_out)
        assert os.path.exists(json_out)
        import json
        with open(json_out) as f:
            loaded = json.load(f)
        assert loaded["summary"] == report["summary"]

    def test_get_latest_data_quality_results_returns_most_recent_run(self, tmp_db):
        from backend.database import get_latest_data_quality_results
        insert_transactions([_tx("T001")], tmp_db)
        dq.run_and_persist(path=tmp_db)
        latest = get_latest_data_quality_results(tmp_db)
        assert len(latest) == len(dq.CHECK_FUNCS)
        assert all("check_name" in r for r in latest)
