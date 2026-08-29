"""
tests/test_generator.py — Transaction generator tests
"""
import os
import sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.config import GeneratorConfig
from backend.generator import generate_transactions, _build_entity_pool
import numpy as np


def small_cfg(**kwargs):
    defaults = dict(n_transactions=500, seed=42, n_entities=20)
    defaults.update(kwargs)
    return GeneratorConfig(**defaults)


class TestGeneratorBasics:
    def test_returns_correct_count(self):
        txs = generate_transactions(small_cfg(), verbose=False)
        assert len(txs) == 500

    def test_all_fields_present(self):
        txs = generate_transactions(small_cfg(), verbose=False)
        required = {"tx_id","timestamp","source_entity","dest_entity",
                    "amount","currency","src_jurisdiction","dst_jurisdiction",
                    "pattern_type","inter_arrival_s"}
        for tx in txs[:10]:
            assert required.issubset(tx.keys()), f"Missing fields: {required - tx.keys()}"

    def test_all_strings_are_python_str(self):
        """numpy.str_ must be coerced to str before insertion."""
        txs = generate_transactions(small_cfg(), verbose=False)
        for tx in txs[:20]:
            for k in ("source_entity","dest_entity","currency",
                      "src_jurisdiction","dst_jurisdiction","pattern_type"):
                assert type(tx[k]) is str, f"{k} is {type(tx[k])}"

    def test_reproducible_with_same_seed(self):
        cfg = small_cfg(seed=99)
        a = generate_transactions(cfg, verbose=False)
        b = generate_transactions(cfg, verbose=False)
        assert [t["tx_id"] for t in a] == [t["tx_id"] for t in b]

    def test_different_seeds_differ(self):
        a = generate_transactions(small_cfg(seed=1), verbose=False)
        b = generate_transactions(small_cfg(seed=2), verbose=False)
        assert a[0]["tx_id"] != b[0]["tx_id"]

    def test_amounts_are_positive(self):
        txs = generate_transactions(small_cfg(), verbose=False)
        for tx in txs:
            assert tx["amount"] > 0

    def test_pattern_types_present(self):
        txs = generate_transactions(small_cfg(), verbose=False)
        patterns = {t["pattern_type"] for t in txs}
        assert "normal" in patterns

    def test_structuring_amounts_below_threshold(self):
        txs = generate_transactions(small_cfg(), verbose=False)
        smurf = [t for t in txs if t["pattern_type"] == "structuring"]
        if smurf:
            for t in smurf:
                assert t["amount"] < 10_000, f"Structuring tx has amount {t['amount']}"

    def test_layering_large_amounts(self):
        txs = generate_transactions(small_cfg(), verbose=False)
        layers = [t for t in txs if t["pattern_type"] == "layering"]
        if layers:
            amounts = [t["amount"] for t in layers]
            assert max(amounts) > 10_000

    def test_timestamps_sorted(self):
        """Generated (then sorted) timestamps should be non-decreasing."""
        txs = generate_transactions(small_cfg(), verbose=False)
        ts_list = [t["timestamp"] for t in txs]
        assert ts_list == sorted(ts_list)
