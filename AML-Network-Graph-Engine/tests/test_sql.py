"""
tests/test_sql.py — Database and SQL-based structuring detection tests
"""
import os, sys, tempfile, pytest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.database import (
    init_db, insert_transactions, sql_entity_structuring_score,
    sql_velocity_score, sql_structuring_scores, count_transactions,
)


@pytest.fixture
def tmp_db():
    """Return path to a fresh temporary SQLite DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _make_tx(tx_id, entity, ts, amount, dest="DEST_001",
              src_jx="US", dst_jx="US", pattern="normal"):
    return dict(
        tx_id=tx_id, timestamp=ts,
        source_entity=entity, dest_entity=dest,
        amount=amount, currency="USD",
        src_jurisdiction=src_jx, dst_jurisdiction=dst_jx,
        pattern_type=pattern, inter_arrival_s=60.0,
    )


class TestInsertAndCount:
    def test_insert_single(self, tmp_db):
        tx = _make_tx("T001", "ENT_A", "2024-01-01 10:00:00", 5000.0)
        insert_transactions([tx], tmp_db)
        assert count_transactions(tmp_db) == 1

    def test_insert_batch(self, tmp_db):
        txs = [_make_tx(f"T{i:03d}", "ENT_A", "2024-01-01 10:00:00", 5000.0)
               for i in range(10)]
        insert_transactions(txs, tmp_db)
        assert count_transactions(tmp_db) == 10

    def test_duplicate_tx_id_ignored(self, tmp_db):
        tx = _make_tx("DUP001", "ENT_A", "2024-01-01 10:00:00", 5000.0)
        insert_transactions([tx, tx], tmp_db)
        assert count_transactions(tmp_db) == 1


class TestStructuringDetection:
    def _seed_structuring_sequence(self, tmp_db, entity="SMURF_A"):
        """Insert 6 sub-threshold transactions from same entity in 30-min window."""
        txs = []
        base_time = "2024-06-15 14:{:02d}:00"
        for i in range(6):
            minute = i * 5  # one every 5 minutes
            txs.append(_make_tx(
                f"STR{i:03d}", entity,
                base_time.format(minute),
                9_500.0 + i * 10,
                pattern="structuring",
            ))
        insert_transactions(txs, tmp_db)
        return txs

    def test_structuring_detected_for_coordinated_batch(self, tmp_db):
        self._seed_structuring_sequence(tmp_db)
        result = sql_entity_structuring_score(
            "SMURF_A", "2024-06-15 14:30:00",
            window_minutes=60, threshold=10_000, path=tmp_db
        )
        assert result["window_count"] >= 3, (
            f"Expected >=3 sub-threshold txs, got {result['window_count']}"
        )
        assert result["window_sum"] > 0
        assert result["structuring_score"] > 0.0

    def test_structuring_score_higher_than_single_normal(self, tmp_db):
        """A coordinated batch should score higher than a single normal transaction."""
        self._seed_structuring_sequence(tmp_db, entity="SMURF_B")
        # Single normal transaction at same time
        insert_transactions([_make_tx(
            "NORM001","NORM_ENT","2024-06-15 14:30:00", 5000.0
        )], tmp_db)

        smurf_result = sql_entity_structuring_score(
            "SMURF_B","2024-06-15 14:30:00", path=tmp_db
        )
        normal_result = sql_entity_structuring_score(
            "NORM_ENT","2024-06-15 14:30:00", path=tmp_db
        )
        assert smurf_result["structuring_score"] > normal_result["structuring_score"], (
            f"Smurfing score {smurf_result['structuring_score']:.4f} should exceed "
            f"normal score {normal_result['structuring_score']:.4f}"
        )

    def test_not_flagged_on_single_sub_threshold_tx(self, tmp_db):
        """A single sub-threshold tx should NOT trigger structuring."""
        insert_transactions([_make_tx("S001","ENT_X","2024-01-01 10:00:00",9000.0)], tmp_db)
        result = sql_entity_structuring_score(
            "ENT_X","2024-01-01 10:00:00",
            window_minutes=60, threshold=10_000,
            path=tmp_db
        )
        # Window count < 3 → score should be 0
        assert result["structuring_score"] == 0.0 or result["window_count"] < 3

    def test_structuring_respects_time_window(self, tmp_db):
        """Transactions outside the window should NOT be counted."""
        entity = "TIME_ENT"
        # 4 txs within window
        for i in range(4):
            insert_transactions([_make_tx(
                f"TW{i:02d}", entity,
                f"2024-06-15 14:{i*5:02d}:00",
                9_000.0,
            )], tmp_db)
        # 2 txs outside window (2 hours earlier)
        for i in range(2):
            insert_transactions([_make_tx(
                f"TW_OLD{i}", entity,
                f"2024-06-15 12:{i*5:02d}:00",
                9_000.0,
            )], tmp_db)

        result = sql_entity_structuring_score(
            entity, "2024-06-15 14:20:00",
            window_minutes=60, threshold=10_000, path=tmp_db
        )
        # Should count the 4 in-window txs, not the 2 old ones
        assert result["window_count"] <= 4, (
            f"Expected <=4 in-window txs, got {result['window_count']}"
        )

    def test_sql_structuring_scores_batch(self, tmp_db):
        self._seed_structuring_sequence(tmp_db)
        rows = sql_structuring_scores(
            window_minutes=60, threshold=10_000, min_count=3, path=tmp_db
        )
        assert len(rows) > 0, "Expected some structuring results"
        assert all("structuring_score" in r for r in rows)
        assert all(0.0 <= r["structuring_score"] <= 1.0 for r in rows)


class TestVelocityDetection:
    def test_burst_detected(self, tmp_db):
        entity = "BURST_ENT"
        for i in range(8):
            insert_transactions([_make_tx(
                f"V{i:03d}", entity,
                f"2024-06-15 10:00:{i*10:02d}",
                1000.0,
            )], tmp_db)
        result = sql_velocity_score(
            entity, "2024-06-15 10:01:20",
            window_seconds=300, burst_threshold=5, path=tmp_db
        )
        assert result["burst_count"] >= 5, f"Expected burst, got {result['burst_count']}"
        assert result["velocity_score"] > 0.0

    def test_no_burst_below_threshold(self, tmp_db):
        entity = "SLOW_ENT"
        for i in range(2):
            insert_transactions([_make_tx(
                f"SL{i}", entity,
                f"2024-06-15 10:0{i}:00",
                1000.0,
            )], tmp_db)
        result = sql_velocity_score(
            entity, "2024-06-15 10:02:00",
            window_seconds=300, burst_threshold=5, path=tmp_db
        )
        assert result["burst_count"] < 5
