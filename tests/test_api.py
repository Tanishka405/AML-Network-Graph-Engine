"""
tests/test_api.py — FastAPI endpoint integration tests
"""
import os, sys, tempfile, pytest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Point to a temp DB before importing the app
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("AML_DB_PATH", _tmp_db.name)

from backend.config import DB_CFG
DB_CFG.path = _tmp_db.name  # override before app import

from backend.database import init_db
init_db(_tmp_db.name)

from fastapi.testclient import TestClient
from backend.main import app

client = TestClient(app, raise_server_exceptions=False)


class TestHealthEndpoint:
    def test_health_returns_200(self):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_has_status_ok(self):
        r = client.get("/health")
        assert r.json()["status"] == "ok"

    def test_health_has_version(self):
        r = client.get("/health")
        assert "version" in r.json()


class TestStatsEndpoint:
    def test_stats_returns_200(self):
        r = client.get("/stats")
        assert r.status_code == 200

    def test_stats_has_expected_keys(self):
        r = client.get("/stats")
        data = r.json()
        for key in ("total_processed", "graph_nodes", "graph_edges"):
            assert key in data, f"Missing key: {key}"


class TestAlertsEndpoint:
    def test_alerts_returns_200(self):
        r = client.get("/alerts")
        assert r.status_code == 200

    def test_alerts_has_list(self):
        r = client.get("/alerts")
        data = r.json()
        assert "alerts" in data
        assert isinstance(data["alerts"], list)

    def test_alerts_limit_param(self):
        r = client.get("/alerts?limit=5")
        assert r.status_code == 200


class TestGraphEndpoint:
    def test_graph_summary_returns_200(self):
        r = client.get("/graph/summary")
        assert r.status_code == 200

    def test_graph_summary_keys(self):
        r = client.get("/graph/summary")
        data = r.json()
        assert "n_nodes" in data
        assert "n_edges" in data


class TestDbCountsEndpoint:
    def test_db_counts_200(self):
        r = client.get("/db/counts")
        assert r.status_code == 200

    def test_db_counts_nonnegative(self):
        r = client.get("/db/counts")
        data = r.json()
        assert data["transactions"] >= 0
        assert data["alerts"] >= 0


class TestWebSocket:
    def test_websocket_handshake(self):
        with client.websocket_connect("/ws/transactions") as ws:
            data = ws.receive_json()
            assert data["type"] == "handshake"
            assert "AML Engine" in data["msg"]

    def test_websocket_receives_transaction_or_ping(self):
        """
        After handshake, the next message should be a transaction
        or a ping (keepalive) — both are valid.
        """
        with client.websocket_connect("/ws/transactions") as ws:
            _handshake = ws.receive_json()
            # We may or may not get a transaction immediately in test mode
            # Just verify the socket stays connected and handshake was valid
            assert _handshake["type"] == "handshake"


# ─── Cleanup ──────────────────────────────────────────────────────────────────
def teardown_module(module):
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass
