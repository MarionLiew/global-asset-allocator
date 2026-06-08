"""
基准测试 — 基准完整性。
"""

import pytest
import pandas as pd
from datetime import date

from backtest.config import BacktestConfig
from backtest.benchmarks.static_60_40 import StaticSixtyForty
from backtest.benchmarks.equal_weight import EqualWeight


def _make_bt_cfg_with_benchmarks():
    """创建包含基准配置的 BacktestConfig。"""
    return BacktestConfig(
        benchmarks={
            "static_60_40": {
                "equity_weight": 0.60,
                "defensive_weight": 0.40,
                "equity_sleeve": {"US": 0.50, "DM": 0.20, "CN": 0.20, "HK": 0.10},
                "defensive_sleeve": {"CN_GOVT": 0.30, "TIPS": 0.25, "GOLD": 0.20, "CORP_BOND": 0.15, "EM_BOND": 0.10},
            },
            "equal_weight": {
                "weights": {l: 1/9 for l in ["US_equity", "DM_equity", "CN_equity", "HK_equity",
                                               "CN_GOVT", "TIPS", "GOLD", "CORP_BOND", "EM_BOND"]},
            },
        }
    )


def test_6040_weights_sum_to_one():
    """60/40 基准权重和 = 1.0。"""
    bt_cfg = _make_bt_cfg_with_benchmarks()
    legs = ["US_equity", "DM_equity", "CN_equity", "HK_equity",
            "CN_GOVT", "TIPS", "GOLD", "CORP_BOND", "EM_BOND"]
    bench = StaticSixtyForty(bt_cfg, legs)
    assert abs(sum(bench.weights.values()) - 1.0) < 1e-6


def test_6040_weights_constant():
    """60/40 权重永不改变。"""
    bt_cfg = _make_bt_cfg_with_benchmarks()
    legs = ["US_equity", "DM_equity", "CN_equity", "HK_equity",
            "CN_GOVT", "TIPS", "GOLD", "CORP_BOND", "EM_BOND"]
    bench = StaticSixtyForty(bt_cfg, legs)

    w_before = dict(bench.weights)
    returns = {"US": 0.05, "DM": 0.03, "CN": -0.02, "HK": 0.01,
               "CN_GOVT": 0.01, "TIPS": 0.02, "GOLD": -0.01,
               "CORP_BOND": 0.01, "EM_BOND": 0.00}
    bench.mark_to_market(returns, date(2000, 1, 31))
    assert bench.weights == w_before


def test_equal_weight_sum_to_one():
    """等权基准权重和 = 1.0。"""
    bt_cfg = _make_bt_cfg_with_benchmarks()
    legs = ["US_equity", "DM_equity", "CN_equity", "HK_equity",
            "CN_GOVT", "TIPS", "GOLD", "CORP_BOND", "EM_BOND"]
    bench = EqualWeight(bt_cfg, legs)
    assert abs(sum(bench.weights.values()) - 1.0) < 1e-3


def test_benchmark_nav_positive():
    """基准 NAV 应为正。"""
    bt_cfg = _make_bt_cfg_with_benchmarks()
    legs = ["US_equity", "DM_equity", "CN_equity", "HK_equity",
            "CN_GOVT", "TIPS", "GOLD", "CORP_BOND", "EM_BOND"]
    bench = StaticSixtyForty(bt_cfg, legs)

    returns = {"US": 0.05, "DM": 0.03, "CN": -0.02, "HK": 0.01,
               "CN_GOVT": 0.01, "TIPS": 0.02, "GOLD": -0.01,
               "CORP_BOND": 0.01, "EM_BOND": 0.00}
    bench.mark_to_market(returns, date(2000, 1, 31))
    bench.mark_to_market(returns, date(2000, 2, 29))
    nav = bench.nav_series()
    assert all(nav > 0)
