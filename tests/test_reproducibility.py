"""
tests/test_reproducibility.py

Regression tests for the two sources of run-to-run nondeterminism found
while auditing `--transactions 5000 --seed 42` builds:

1. backend/graph_engine.py: nx.betweenness_centrality(k=...) samples source
   nodes with an RNG. It was called without `seed`, so once the graph had
   more than 50 nodes the sampled centralities (and therefore graph scores,
   IsolationForest features and alert counts) differed on every run.

2. backend/generator.py: `rng.choice(list(cfg.high_risk_jurisdictions))`
   turned a set of strings into a list. String-set iteration order depends
   on PYTHONHASHSEED (randomised per process), so the same RNG draw picked
   a different jurisdiction in each process.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

import networkx as nx
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.graph_engine import TransactionGraph
from backend.config import GRAPH_CFG


def _run_py(code: str, hashseed: str) -> str:
    env = dict(os.environ, PYTHONHASHSEED=hashseed, PYTHONPATH=ROOT)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env, cwd=ROOT, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def _build_graph(n_nodes=90, n_tx=400, seed=3) -> TransactionGraph:
    rng = np.random.default_rng(seed)
    g = TransactionGraph()
    now = time.time()
    for i in range(n_tx):
        s, d = rng.choice(n_nodes, size=2, replace=False)
        g.add_transaction(f"T{i}", f"E{s}", f"E{d}",
                          float(rng.uniform(100, 5000)), timestamp=now)
    g.force_recompute()
    return g


class TestBetweennessSeeding:
    def test_config_exposes_a_betweenness_seed(self):
        assert isinstance(GRAPH_CFG.betweenness_seed, int)

    def test_networkx_sampling_is_actually_random_without_seed(self):
        """Documents WHY the seed is needed (guards against the seed being
        removed as 'unnecessary'): unseeded k-sampling is not repeatable."""
        G = nx.gnp_random_graph(80, 0.06, seed=1, directed=True)
        runs = [nx.betweenness_centrality(G, k=50) for _ in range(4)]
        assert any(r != runs[0] for r in runs[1:])

    def test_graph_over_50_nodes_uses_sampling_path(self):
        g = _build_graph()
        assert g.n_nodes > 50  # otherwise this test wouldn't exercise k-sampling

    def test_betweenness_identical_across_independent_graphs(self):
        a, b = _build_graph(), _build_graph()
        assert a._cache["betweenness"] == b._cache["betweenness"]

    def test_betweenness_stable_on_repeated_recompute(self):
        g = _build_graph()
        first = dict(g._cache["betweenness"])
        for _ in range(3):
            g._metrics_dirty = True
            g._recompute()
            assert g._cache["betweenness"] == first

    def test_graph_scores_identical_across_independent_graphs(self):
        a, b = _build_graph(), _build_graph()
        for node in ("E1", "E7", "E42"):
            assert a.get_graph_score(node) == b.get_graph_score(node)


class TestGeneratorHashSeedIndependence:
    CODE = (
        "import hashlib;"
        "from backend.config import GeneratorConfig;"
        "from backend.generator import generate_transactions;"
        "t=generate_transactions(GeneratorConfig(n_transactions=1500,seed=42),verbose=False);"
        "print(hashlib.md5(repr([(x['tx_id'],x['source_entity'],x['dest_entity'],x['amount'],"
        "x['currency'],x['src_jurisdiction'],x['dst_jurisdiction'],x['timestamp'],x['pattern_type'])"
        " for x in t]).encode()).hexdigest())"
    )

    def test_same_output_under_different_python_hash_seeds(self):
        digests = {_run_py(self.CODE, hs) for hs in ("0", "1", "12345")}
        assert len(digests) == 1, f"generator depends on PYTHONHASHSEED: {digests}"

    def test_high_risk_jurisdiction_choice_uses_stable_order(self):
        import inspect
        from backend import generator
        src = inspect.getsource(generator._random_jurisdiction)
        assert "sorted(" in src and "list(cfg.high_risk_jurisdictions)" not in src


class TestExportLevelReproducibility:
    """The two remaining sources of byte-difference across builds
    (kpi_summary.csv's generated_at_utc, data_quality_run_history.csv's
    run_timestamp) are intentional wall-clock provenance, not a bug.
    This test proves that by regenerating exports twice and asserting
    those specific fields are the ONLY thing that differs -- if anything
    else in these files started differing, this test would fail instead
    of silently passing because the whole file was excluded.

    Runs in an isolated temp copy of the repo (not ROOT) so it can't
    clobber the real data/aml.db or exports/ fixture committed to the
    repository -- build_analytics_dataset.py has no way to redirect its
    DB path via env var, only via backend.config.DB_CFG.path, which is
    resolved relative to the repo root it's run from."""

    @staticmethod
    def _isolated_copy(tmp_path):
        import shutil
        dest = tmp_path / "repo_copy"
        shutil.copytree(
            ROOT, dest,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".pytest_cache", "data", "exports",
                "data_quality_report.json", ".git",
            ),
        )
        return dest

    def _build(self, repo_copy, out_root, tag):
        import shutil
        env = dict(os.environ, PYTHONHASHSEED=tag)
        subprocess.run(
            [sys.executable, "scripts/build_analytics_dataset.py",
             "--transactions", "600", "--seed", "42"],
            cwd=str(repo_copy), check=True, capture_output=True, text=True, env=env,
        )
        dest = out_root / f"exports_{tag}"
        shutil.copytree(repo_copy / "exports", dest)
        return dest

    def test_only_timestamp_fields_differ_across_builds(self, tmp_path):
        repo_copy = self._isolated_copy(tmp_path)
        d1 = self._build(repo_copy, tmp_path, "0")
        d2 = self._build(repo_copy, tmp_path, "9999")

        for name in sorted(os.listdir(d1)):
            a = open(d1 / name, "rb").read()
            b = open(d2 / name, "rb").read()
            if name == "kpi_summary.csv":
                la, lb = a.decode().splitlines(), b.decode().splitlines()
                non_ts_a = [l for l in la if not l.startswith("generated_at_utc")]
                non_ts_b = [l for l in lb if not l.startswith("generated_at_utc")]
                assert non_ts_a == non_ts_b, f"{name}: non-timestamp content differs"
                assert a != b or True  # timestamp itself may or may not coincide
            elif name == "data_quality_run_history.csv":
                la, lb = a.decode().splitlines(), b.decode().splitlines()
                # strip the first CSV column (run_timestamp) from data rows
                strip = lambda lines: [
                    (",".join(l.split(",")[1:]) if i else l) for i, l in enumerate(lines)
                ]
                assert strip(la) == strip(lb), f"{name}: non-timestamp content differs"
            elif name == "kpi_summary.json":
                import json
                da, db = json.loads(a), json.loads(b)
                da.pop("generated_at_utc", None)
                db.pop("generated_at_utc", None)
                assert da == db, f"{name}: non-timestamp content differs"
            else:
                assert a == b, f"{name}: expected byte-identical, differs"

    CODE = (
        "import os,json,tempfile;"
        "import backend.database as d;"
        "p=tempfile.mktemp(suffix='.db');d.init_db(p);d.get_db_path=lambda:p;"
        "from backend.config import GeneratorConfig;"
        "from backend.generator import generate_transactions;"
        "import backend.streaming as st;"
        "txs=generate_transactions(GeneratorConfig(n_transactions=800,seed=42),verbose=False);"
        "[st._process_transaction(t) for t in txs]\n"
        "with d.db_conn(p) as c:\n"
        "    r=c.execute(\"SELECT COUNT(*),SUM(risk_label='HIGH'),SUM(risk_label='CRITICAL'),"
        "ROUND(SUM(risk_score),8) FROM alerts\").fetchone()\n"
        "print(json.dumps(list(r)))"
    )

    def test_identical_alerts_across_processes_and_hash_seeds(self):
        results = {_run_py(self.CODE, hs) for hs in ("0", "7")}
        assert len(results) == 1, f"pipeline not reproducible: {results}"
        assert json.loads(results.pop())[0] > 0  # sanity: alerts were produced
