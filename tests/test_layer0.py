"""
Layer 0 测试 — E 的范围断言。
"""

import pytest
from datetime import date

from backtest.engine.layer0 import compute_equity_budget


def test_equity_budget_bounds(params, provider):
    """E ∈ [E_min, E_max] ∩ [E_base-0.10, E_base+0.10]。"""
    asof = date(2000, 6, 30)
    E = compute_equity_budget(asof, params, provider)

    assert params.E_min <= E <= params.E_max, f"E={E} 不在 [{params.E_min}, {params.E_max}]"
    assert params.E_base - 0.10 <= E <= params.E_base + 0.10, \
        f"E={E} 不在 [{params.E_base-0.10}, {params.E_base+0.10}]"


def test_equity_budget_monotonic_in_erp(params, provider):
    """更高的 ERP → 更高的 E (单调性)。"""
    # 低 ERP: 高 CAPE (低收益率) + 高实际利率
    class LowERP:
        def earnings_yield_world(self, asof): return 0.03  # 1/CAPE=3%
        def real_yield(self, asof): return 0.04
        def erp_rolling_median(self, asof): return 0.03

    # 高 ERP: 低 CAPE (高收益率) + 低实际利率
    class HighERP:
        def earnings_yield_world(self, asof): return 0.08  # 1/CAPE=8%
        def real_yield(self, asof): return 0.01
        def erp_rolling_median(self, asof): return 0.03

    E_low = compute_equity_budget(asof=date(2000, 1, 31), params=params, md=LowERP())
    E_high = compute_equity_budget(asof=date(2000, 1, 31), params=params, md=HighERP())

    assert E_high >= E_low, f"高 ERP 的 E={E_high} 应 ≥ 低 ERP 的 E={E_low}"


def test_equity_budget_default():
    """默认参数下 E = E_base = 0.60。"""
    from tests.conftest import SyntheticProvider
    from backtest.config import Params
    provider = SyntheticProvider()
    params_default = Params(E_base=0.60, k0=0.20, E_min=0.40, E_max=0.80)
    E = compute_equity_budget(date(2000, 1, 31), params_default, provider)
    # 默认情况下, ERP ≈ ERP_ref, 所以 E ≈ E_base
    assert abs(E - params_default.E_base) < 0.05
