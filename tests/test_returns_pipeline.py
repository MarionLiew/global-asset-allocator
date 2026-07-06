"""
收益管道回归测试 — 防止两类历史 bug 复发:

1. Portfolio.mark_to_market 键不匹配: returns 用数据键 (US), 持仓用腿名
   (US_equity), 股票持仓从未被更新市值 → 30年回测股票收益恒为 0。
2. v2 主循环用风险权重直接当现金目标执行, risk_to_cash 是死代码。
"""

from __future__ import annotations

from datetime import date

import pytest

from backtest.config import Params, BacktestConfig
from backtest.engine.execution import Portfolio
from backtest.engine.backtest_loop import run_backtest_v2
from backtest.data._constants import EQUITY_MARKETS, DEFENSIVE_ASSETS


@pytest.fixture
def provider():
    from tests.conftest import SyntheticProvider
    return SyntheticProvider()


def test_mark_to_market_updates_equity_legs():
    """股票腿 (US_equity) 必须吃到数据键 (US) 的回报。"""
    p = Portfolio()
    p.add_holdings("US_equity", 100.0)
    p.add_holdings("TIPS", 100.0)

    p.mark_to_market({"US": 0.10, "TIPS": 0.02})

    assert abs(p.holdings["US_equity"] - 110.0) < 1e-9, (
        "US_equity 未吃到 US 的回报 — mark_to_market 键映射回归"
    )
    assert abs(p.holdings["TIPS"] - 102.0) < 1e-9


def test_v2_strategy_nav_moves_with_market(provider):
    """v2 回测的策略 TWR 不应是水平线 (股票腿收益恒为0的症状)。"""
    params = Params()
    bt_cfg = BacktestConfig(
        start_date="1996-01-31", end_date="2004-12-31",
        monthly_contribution_cny=10_000.0,
    )
    result = run_backtest_v2(params, bt_cfg, provider)

    monthly = result.strategy_nav.pct_change().dropna()
    # 合成数据里股票有正常波动, 组合月度收益的标准差不应接近 0
    assert monthly.std() > 0.002, (
        f"策略月度收益 std={monthly.std():.5f}, 疑似股票腿未被 mark-to-market"
    )


def test_v2_targets_are_cash_weights(provider):
    """v2 执行目标必须是现金权重 (风险→现金转换后), 不是风险权重。"""
    params = Params()
    bt_cfg = BacktestConfig(
        start_date="2000-01-31", end_date="2001-12-31",
        monthly_contribution_cny=10_000.0,
    )
    result = run_backtest_v2(params, bt_cfg, provider)
    snap = result.weight_history[-1]

    # targets 应与 cash_weights 一致 (腿名映射后)
    for asset, cw in snap.cash_weights.items():
        leg = f"{asset}_equity" if asset in EQUITY_MARKETS else asset
        assert abs(snap.targets[leg] - cw) < 1e-9, (
            f"{leg}: target={snap.targets[leg]:.4f} != cash_weight={cw:.4f} — "
            "执行目标未走风险→现金转换"
        )

    # 现金权重不应等于风险权重 (合成数据里各资产 vol 不同, 两者必然有差异)
    diffs = [abs(snap.cash_weights[a] - snap.risk_weights[a]) for a in snap.risk_weights]
    assert max(diffs) > 0.01, "cash_weights 与 risk_weights 完全相同, 逆波动转换疑似未生效"


def test_v2_no_asset_zeroed_out(provider):
    """风险平价锚下, 任何配置内资产的现金权重都不应接近零。"""
    params = Params()
    bt_cfg = BacktestConfig(
        start_date="2000-01-31", end_date="2001-12-31",
        monthly_contribution_cny=10_000.0,
    )
    result = run_backtest_v2(params, bt_cfg, provider)
    snap = result.weight_history[-1]

    for asset, w in snap.cash_weights.items():
        assert w > 0.01, f"{asset} 现金权重={w:.4f}, 风险平价不应把资产清零"
