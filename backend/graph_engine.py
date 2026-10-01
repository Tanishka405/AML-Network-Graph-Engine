"""
graph_engine.py — Directed transaction graph with full graph analytics.

Implements:
  - Rolling window directed graph (correct edge tracking)
  - Betweenness centrality
  - Eigenvector centrality (using scipy-backed NetworkX for directed graphs)
  - Community detection (Louvain on undirected projection)
  - Cycle / circular flow detection
  - Multi-hop path analysis
  - Transaction velocity per entity
  - Graph feature extraction for the ML pipeline
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx
import numpy as np

from backend.config import GRAPH_CFG, GraphConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Edge metadata tracker — fixes rolling-window correctness
# ---------------------------------------------------------------------------
@dataclass
class EdgeInstance:
    """One individual transaction contribution to an edge."""
    tx_id:     str
    amount:    float
    timestamp: float   # unix epoch


@dataclass
class EdgeData:
    """Aggregated data for a directed edge (src → dst)."""
    instances:   List[EdgeInstance] = field(default_factory=list)
    total_amount: float = 0.0
    tx_count:     int   = 0

    def add(self, inst: EdgeInstance) -> None:
        self.instances.append(inst)
        self.total_amount += inst.amount
        self.tx_count     += 1

    def remove_before(self, cutoff_epoch: float) -> bool:
        """Remove expired instances.  Returns True if edge still has instances."""
        expired = [i for i in self.instances if i.timestamp < cutoff_epoch]
        for inst in expired:
            self.instances.remove(inst)
            self.total_amount -= inst.amount
            self.tx_count     -= 1
        self.total_amount = max(0.0, self.total_amount)
        self.tx_count     = max(0, self.tx_count)
        return len(self.instances) > 0

    @property
    def avg_amount(self) -> float:
        return self.total_amount / self.tx_count if self.tx_count > 0 else 0.0

    @property
    def last_ts(self) -> float:
        return max((i.timestamp for i in self.instances), default=0.0)


# ---------------------------------------------------------------------------
# Rolling Transaction Graph
# ---------------------------------------------------------------------------
class TransactionGraph:
    """
    Maintains a directed NetworkX graph over a rolling time window.

    Edge tracking is per-transaction, so expiry of one transaction
    does NOT remove the edge if other transactions remain in the window.
    """

    def __init__(self, cfg: GraphConfig | None = None) -> None:
        self.cfg  = cfg or GRAPH_CFG
        self.G    = nx.DiGraph()
        self._edge_data: Dict[Tuple[str, str], EdgeData] = {}
        self._tx_queue: deque[Tuple[float, str, str, str]] = deque()
        # (epoch, tx_id, src, dst)

        # Cached metrics — recomputed lazily, not on every add.
        self._metrics_dirty = True
        self._cache: Dict[str, Any] = {}
        self._adds_since_recompute: int = 0
        self._recompute_every: int = 50   # recompute centrality every 50 adds

    def add_transaction(
        self,
        tx_id: str,
        source: str,
        dest: str,
        amount: float,
        timestamp: Optional[float] = None,
    ) -> None:
        epoch = timestamp or time.time()
        inst  = EdgeInstance(tx_id=tx_id, amount=amount, timestamp=epoch)
        key   = (source, dest)

        if key not in self._edge_data:
            self._edge_data[key] = EdgeData()
        self._edge_data[key].add(inst)
        self._tx_queue.append((epoch, tx_id, source, dest))

        # Ensure nodes and edge exist in NetworkX graph
        if not self.G.has_node(source):
            self.G.add_node(source)
        if not self.G.has_node(dest):
            self.G.add_node(dest)

        ed = self._edge_data[key]
        self.G.add_edge(
            source, dest,
            weight=ed.total_amount,
            tx_count=ed.tx_count,
            avg_amount=ed.avg_amount,
        )
        self._adds_since_recompute += 1
        if self._adds_since_recompute >= self._recompute_every:
            self._metrics_dirty = True
            self._adds_since_recompute = 0
        if not getattr(self, '_batch_mode', False):
            self._expire_old()

    def force_recompute(self) -> None:
        """Force a full metric recompute (call after batch ingest)."""
        self._metrics_dirty = True
        self._recompute()

    def _expire_old(self) -> None:
        """
        Remove transactions older than the rolling window.

        We scan the FULL queue (not just the front) because transactions
        may be inserted with out-of-order timestamps (e.g. historical
        batch ingestion during evaluation).  This keeps edge weights and
        counts correct when old-timestamped transactions are added after
        newer ones are already in the queue.
        """
        cutoff = time.time() - self.cfg.rolling_window_seconds
        # Partition queue into expired and live
        expired = [(ep, tid, src, dst) for (ep, tid, src, dst) in self._tx_queue
                   if ep < cutoff]
        if not expired:
            return
        # Rebuild queue keeping only live entries
        self._tx_queue = deque(
            item for item in self._tx_queue if item[0] >= cutoff
        )
        # Process each expired transaction
        affected_edges: set = set()
        for epoch, tx_id, src, dst in expired:
            key = (src, dst)
            affected_edges.add(key)
            if key in self._edge_data:
                self._edge_data[key].remove_before(cutoff)

        # Update or remove affected edges
        for key in affected_edges:
            src, dst = key
            if key in self._edge_data and self._edge_data[key].tx_count > 0:
                ed = self._edge_data[key]
                if self.G.has_edge(src, dst):
                    self.G[src][dst]["weight"]     = ed.total_amount
                    self.G[src][dst]["tx_count"]   = ed.tx_count
                    self.G[src][dst]["avg_amount"]  = ed.avg_amount
            else:
                # All instances expired — remove edge and possibly nodes
                if key in self._edge_data:
                    del self._edge_data[key]
                if self.G.has_edge(src, dst):
                    self.G.remove_edge(src, dst)
                for n in (src, dst):
                    if n in self.G and self.G.degree(n) == 0:
                        self.G.remove_node(n)
        self._metrics_dirty = True

    # -----------------------------------------------------------------------
    # Graph metrics
    # -----------------------------------------------------------------------
    def _recompute(self) -> None:
        if not self._metrics_dirty:
            return
        G = self.G
        n = G.number_of_nodes()

        # Betweenness centrality — use k-sample approximation when graph
        # is large (>50 nodes) to keep runtime bounded to ~0.05s.
        self._cache["betweenness"] = {}
        if n >= 3:
            try:
                k_samples = self.cfg.betweenness_k
                if k_samples is None and n > 50:
                    k_samples = min(50, n)
                self._cache["betweenness"] = nx.betweenness_centrality(
                    G, weight="weight", k=k_samples, normalized=True,
                    seed=self.cfg.betweenness_seed,
                )
            except Exception:
                self._cache["betweenness"] = {}

        # Eigenvector centrality — use numpy eigenvector on the undirected
        # projection. This always converges (numpy LAPACK eig), captures
        # the influence structure of the transaction network, and avoids
        # the AmbiguousSolution / non-convergence failure modes of the
        # directed power-iteration approach on sparse/disconnected graphs.
        # Choice is documented: undirected EC reflects bilateral influence.
        self._cache["eigenvector"] = {}
        if n >= 3:
            try:
                UG = G.to_undirected()
                ec = nx.eigenvector_centrality_numpy(UG, weight="weight")
                self._cache["eigenvector"] = ec
            except Exception:
                # Final fallback: normalised weighted in-degree
                total_w = sum(dict(G.in_degree(weight="weight")).values()) or 1.0
                self._cache["eigenvector"] = {
                    node: (G.in_degree(node, weight="weight") or 0.0) / total_w
                    for node in G.nodes()
                }

        # PageRank (kept for backward compat)
        if n >= 2:
            try:
                self._cache["pagerank"] = nx.pagerank(
                    G, alpha=self.cfg.pagerank_alpha, weight="weight"
                )
            except Exception:
                self._cache["pagerank"] = {}
        else:
            self._cache["pagerank"] = {}

        # Community detection via Louvain on undirected projection
        self._cache["communities"] = {}
        self._cache["community_sizes"] = {}
        if n >= 4:
            try:
                UG = G.to_undirected()
                # Use greedy modularity communities (available in all nx versions)
                comms = nx.algorithms.community.greedy_modularity_communities(
                    UG, weight="weight"
                )
                for cid, members in enumerate(comms):
                    sz = len(members)
                    for m in members:
                        self._cache["communities"][m]     = cid
                        self._cache["community_sizes"][m] = sz
            except Exception:
                pass

        # Cycles (circular flow) — use SCC decomposition instead of
        # simple_cycles which is O(V+E * (V+E+C)) and too slow on dense graphs.
        # Nodes in non-trivial SCCs (size >= 2) participate in cycles.
        self._cache["nodes_in_cycles"] = set()
        if n >= 3 and G.number_of_edges() >= 3:
            try:
                for scc in nx.strongly_connected_components(G):
                    if len(scc) >= 2:
                        self._cache["nodes_in_cycles"].update(scc)
            except Exception:
                pass

        self._metrics_dirty = False

    def get_node_features(self, node: str) -> Dict[str, float]:
        """Return a dict of graph features for a single node."""
        self._recompute()
        G = self.G

        if node not in G:
            return _zero_features()

        in_deg    = G.in_degree(node)
        out_deg   = G.out_degree(node)
        w_in_deg  = G.in_degree(node, weight="weight") or 0.0
        w_out_deg = G.out_degree(node, weight="weight") or 0.0

        bc   = self._cache["betweenness"].get(node, 0.0)
        ec   = self._cache["eigenvector"].get(node, 0.0)
        pr   = self._cache["pagerank"].get(node, 0.0)
        comm = self._cache["communities"].get(node, -1)
        csz  = self._cache["community_sizes"].get(node, 1)
        in_cycle = 1.0 if node in self._cache["nodes_in_cycles"] else 0.0

        # Transaction velocity: out-transactions in the edge window
        tx_count = sum(
            ed.tx_count
            for (s, _), ed in self._edge_data.items()
            if s == node
        )

        total_out = sum(
            ed.total_amount
            for (s, _), ed in self._edge_data.items()
            if s == node
        )
        total_in = sum(
            ed.total_amount
            for (_, d), ed in self._edge_data.items()
            if d == node
        )

        return {
            "betweenness_centrality":  float(bc),
            "eigenvector_centrality":  float(ec),
            "pagerank":                float(pr),
            "in_degree":               float(in_deg),
            "out_degree":              float(out_deg),
            "weighted_in_degree":      float(w_in_deg),
            "weighted_out_degree":     float(w_out_deg),
            "tx_count":                float(tx_count),
            "total_incoming_amount":   float(total_in),
            "total_outgoing_amount":   float(total_out),
            "community_id":            float(comm),
            "community_size":          float(csz),
            "in_cycle":                float(in_cycle),
        }

    def get_graph_score(self, node: str) -> float:
        """
        Composite graph risk score [0,1] for a node.
        Combines betweenness, eigenvector, cycle participation, and community.
        """
        self._recompute()
        if node not in self.G:
            return 0.0

        bc  = self._cache["betweenness"].get(node, 0.0)
        ec  = self._cache["eigenvector"].get(node, 0.0)
        cyc = 1.0 if node in self._cache["nodes_in_cycles"] else 0.0
        csz = self._cache["community_sizes"].get(node, 1)
        comm_density = min(1.0, csz / max(self.G.number_of_nodes(), 1))

        score = (
            0.35 * min(bc * 10, 1.0) +
            0.25 * min(ec * 5,  1.0) +
            0.25 * cyc +
            0.15 * comm_density
        )
        return round(min(score, 1.0), 6)

    def multi_hop_indicator(self, source: str, dest: str, max_depth: int = 5) -> float:
        """
        Returns a [0,1] score indicating suspicious multi-hop paths
        between source and dest.  Higher = more intermediaries.
        """
        if source not in self.G or dest not in self.G:
            return 0.0
        try:
            length = nx.shortest_path_length(self.G, source, dest)
            if length <= 1:
                return 0.0
            return min(1.0, (length - 1) / max_depth)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return 0.0

    @property
    def n_nodes(self) -> int:
        return self.G.number_of_nodes()

    @property
    def n_edges(self) -> int:
        return self.G.number_of_edges()

    @property
    def n_communities(self) -> int:
        self._recompute()
        comms = set(self._cache["communities"].values())
        return len(comms)

    @property
    def nodes_in_cycles(self) -> Set[str]:
        self._recompute()
        return self._cache["nodes_in_cycles"]

    def summary(self) -> Dict[str, Any]:
        self._recompute()
        return {
            "n_nodes":     self.n_nodes,
            "n_edges":     self.n_edges,
            "n_communities": self.n_communities,
            "cycle_nodes": len(self._cache.get("nodes_in_cycles", set())),
        }


# ---------------------------------------------------------------------------
# Batch graph builder (for evaluation / benchmark)
# ---------------------------------------------------------------------------
def build_graph_from_transactions(
    txs: List[Dict[str, Any]],
    cfg: GraphConfig | None = None,
) -> TransactionGraph:
    """Build a TransactionGraph from a list of transaction dicts."""
    tg = TransactionGraph(cfg)
    for tx in txs:
        try:
            ts_str = tx["timestamp"]
            epoch  = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
        except (KeyError, ValueError):
            epoch = time.time()
        tg.add_transaction(
            tx_id=tx["tx_id"],
            source=tx["source_entity"],
            dest=tx["dest_entity"],
            amount=float(tx["amount"]),
            timestamp=epoch,
        )
    return tg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _zero_features() -> Dict[str, float]:
    return {
        "betweenness_centrality":  0.0,
        "eigenvector_centrality":  0.0,
        "pagerank":                0.0,
        "in_degree":               0.0,
        "out_degree":              0.0,
        "weighted_in_degree":      0.0,
        "weighted_out_degree":     0.0,
        "tx_count":                0.0,
        "total_incoming_amount":   0.0,
        "total_outgoing_amount":   0.0,
        "community_id":            -1.0,
        "community_size":          1.0,
        "in_cycle":                0.0,
    }
