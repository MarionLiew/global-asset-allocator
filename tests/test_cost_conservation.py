"""
成本守恒测试。
"""

import pytest
from datetime import date

from backtest.engine.cost import compute_trade_cost
from backtest.config import CostsConfig


def test_cost_non_negative():
    """成本不能为负。"""
    costs = CostsConfig(equity_bps=10, defensive_bps=5, fx_spread_bps=20)
    brk = compute_trade_cost(10000, "equity", "USD", "CNY", costs)
    assert brk.total >= 0


def test_fx_cost_only_for_non_base():
    """只有非基准货币才收 FX 点差。"""
    costs = CostsConfig(fx_spread_bps=20)
    brk_cny = compute_trade_cost(10000, "equity", "CNY", "CNY", costs)
    brk_usd = compute_trade_cost(10000, "equity", "USD", "CNY", costs)
    assert brk_cny.fx_spread == 0
    assert brk_usd.fx_spread > 0


def test_cost_proportional():
    """成本与金额成正比。"""
    costs = CostsConfig(equity_bps=10)
    brk1 = compute_trade_cost(10000, "equity", "USD", "CNY", costs)
    brk2 = compute_trade_cost(20000, "equity", "USD", "CNY", costs)
    assert abs(brk2.commission - 2 * brk1.commission) < 1e-6


def test_defensive_cost_lower():
    """防御资产交易成本更低。"""
    costs = CostsConfig(equity_bps=10, defensive_bps=5)
    eq = compute_trade_cost(10000, "equity", "USD", "CNY", costs)
    def_ = compute_trade_cost(10000, "defensive", "USD", "CNY", costs)
    assert def_.commission < eq.commission
