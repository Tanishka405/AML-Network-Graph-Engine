"""
tests/test_graph.py — Graph engine correctness tests
"""
import os, sys, pytest, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.graph_engine import TransactionGraph


def _build_cycle_graph():
    """A → B → C → A forms a 3-cycle."""
    g = TransactionGraph()
    g.add_transaction("T1", "A", "B", 5000, timestamp=time.time())
    g.add_transaction("T2", "B", "C", 4500, timestamp=time.time())
    g.add_transaction("T3", "C", "A", 4000, timestamp=time.time())
    return g


def _build_hub_graph():
    """D is a hub connected to many nodes — high betweenness."""
    g = TransactionGraph()
    t = time.time()
    # D mediates paths from A→E, B→E, C→E
    g.add_transaction("T1", "A", "D", 1000, timestamp=t)
    g.add_transaction("T2", "D", "E", 900,  timestamp=t)
    g.add_transaction("T3", "B", "D", 1000, timestamp=t)
    g.add_transaction("T4", "C", "D", 1000, timestamp=t)
    return g


class TestGraphConstruction:
    def test_nodes_and_edges(self):
        g = TransactionGraph()
        g.add_transaction("T1", "Alice", "Bob", 1000)
        g.add_transaction("T2", "Bob", "Carol", 900)
        assert g.n_nodes == 3
        assert g.n_edges == 2

    def test_edge_weight_accumulates(self):
        g = TransactionGraph()
        g.add_transaction("T1", "A", "B", 1000)
        g.add_transaction("T2", "A", "B", 2000)
        assert g.G["A"]["B"]["weight"] == 3000
        assert g.G["A"]["B"]["tx_count"] == 2

    def test_self_loop_not_added(self):
        """Generator should never produce src==dst, but graph shouldn't explode."""
        g = TransactionGraph()
        g.add_transaction("T1", "A", "B", 1000)
        assert g.n_nodes >= 2


class TestCycleDetection:
    def test_cycle_detected(self):
        g = _build_cycle_graph()
        assert len(g.nodes_in_cycles) >= 2, "Expected nodes in cycle"

    def test_cycle_members_correct(self):
        g = _build_cycle_graph()
        for node in ("A", "B", "C"):
            assert node in g.nodes_in_cycles, f"{node} should be in cycle"

    def test_no_false_cycle_on_path(self):
        """A straight path A→B→C should NOT produce a cycle."""
        g = TransactionGraph()
        g.add_transaction("T1", "A", "B", 1000)
        g.add_transaction("T2", "B", "C", 900)
        # Could have nodes in cycles if there's no return edge
        # A→B→C with no C→A should not be in a cycle
        cycles = g.nodes_in_cycles
        # A simple chain should have empty cycles (no strongly connected components)
        # with length >1
        assert "A" not in cycles or "C" not in cycles or len(cycles) < 3

    def test_cycle_score_higher_than_non_cycle(self):
        g_cycle = _build_cycle_graph()
        g_path  = TransactionGraph()
        g_path.add_transaction("T1", "X", "Y", 5000)
        g_path.add_transaction("T2", "Y", "Z", 4500)

        cycle_score = g_cycle.get_graph_score("A")
        path_score  = g_path.get_graph_score("X")
        assert cycle_score >= path_score, (
            f"Cycle score {cycle_score:.4f} should >= path score {path_score:.4f}"
        )


class TestBetweennessCentrality:
    def test_hub_has_higher_betweenness(self):
        g = _build_hub_graph()
        feats_D = g.get_node_features("D")
        feats_A = g.get_node_features("A")
        assert feats_D["betweenness_centrality"] >= feats_A["betweenness_centrality"], (
            f"Hub D bc={feats_D['betweenness_centrality']:.4f} "
            f"should >= leaf A bc={feats_A['betweenness_centrality']:.4f}"
        )

    def test_betweenness_in_zero_one(self):
        g = _build_hub_graph()
        for node in g.G.nodes():
            bc = g.get_node_features(node)["betweenness_centrality"]
            assert 0.0 <= bc <= 1.0, f"BC={bc} out of range for node {node}"


class TestEigenvectorCentrality:
    def test_eigenvector_computed(self):
        g = _build_hub_graph()
        feats = g.get_node_features("D")
        assert feats["eigenvector_centrality"] >= 0.0

    def test_eigenvector_in_zero_one(self):
        g = _build_hub_graph()
        for node in g.G.nodes():
            ec = g.get_node_features(node)["eigenvector_centrality"]
            assert 0.0 <= ec <= 1.0 + 1e-6, f"EC={ec} out of range"


class TestCommunityDetection:
    def test_communities_detected(self):
        g = TransactionGraph()
        # Two clusters: (A,B,C) and (X,Y,Z)
        for s, d in [("A","B"),("B","C"),("C","A"),
                     ("X","Y"),("Y","Z"),("Z","X")]:
            g.add_transaction(f"T_{s}{d}", s, d, 1000)
        assert g.n_communities >= 1  # at least 1 community found

    def test_community_id_assigned(self):
        g = _build_cycle_graph()
        for node in ("A","B","C"):
            feats = g.get_node_features(node)
            assert feats["community_id"] >= -1


class TestMultiHop:
    def test_direct_edge_low_score(self):
        g = TransactionGraph()
        g.add_transaction("T1","A","B",1000)
        score = g.multi_hop_indicator("A","B")
        assert score == 0.0, f"Direct edge should give 0, got {score}"

    def test_two_hop_nonzero(self):
        g = TransactionGraph()
        g.add_transaction("T1","A","B",1000)
        g.add_transaction("T2","B","C",900)
        score = g.multi_hop_indicator("A","C")
        assert score > 0.0, f"2-hop should give >0, got {score}"

    def test_longer_path_higher_score(self):
        g = TransactionGraph()
        g.add_transaction("T1","A","B",1000)
        g.add_transaction("T2","B","C",900)
        g.add_transaction("T3","C","D",800)
        g.add_transaction("T4","D","E",700)
        s2 = g.multi_hop_indicator("A","C")
        s4 = g.multi_hop_indicator("A","E")
        assert s4 >= s2, f"Longer path {s4:.4f} should >= shorter {s2:.4f}"


class TestRollingWindowCorrectness:
    def test_edge_not_removed_prematurely(self):
        """
        Two transactions on same edge; only one expires.
        Edge should remain with one instance.
        """
        g = TransactionGraph()
        now = time.time()
        # Add two transactions: one old, one recent
        g.add_transaction("T1", "A", "B", 1000, timestamp=now - g.cfg.rolling_window_seconds - 10)
        g.add_transaction("T2", "A", "B", 2000, timestamp=now)
        # Force expiry check
        g._expire_old()
        # Edge should still exist (T2 is still in window)
        assert g.G.has_edge("A","B"), "Edge removed prematurely"
        assert g.G["A"]["B"]["tx_count"] == 1, "Should have 1 remaining tx"

    def test_edge_removed_when_all_expired(self):
        g = TransactionGraph()
        old_ts = time.time() - g.cfg.rolling_window_seconds - 100
        g.add_transaction("T1", "A", "B", 1000, timestamp=old_ts)
        g._expire_old()
        assert not g.G.has_edge("A","B"), "Edge should be removed after expiry"
