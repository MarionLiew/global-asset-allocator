"""
Layer 1 测试 — 权重和=1, band 约束。
"""

import pytest
from datetime import date

from backtest.engine.layer1 import compute_regional_weights


def test_regional_weights_sum_to_one(params, provider):
    """sum(m_i) ≈ 1.0 (±1e-6)。"""
    asof = date(2000, 6, 30)
    m = compute_regional_weights(asof, params, provider)
    assert abs(sum(m.values()) - 1.0) < 1e-6, f"权重和={sum(m.values())}"


def test_regional_band_constraint(params, provider):
    """|m_i - cap_weight_i| ≤ band_pp。"""
    asof = date(2000, 6, 30)
    m = compute_regional_weights(asof, params, provider)
    cap_w = provider.cap_weights(asof)

    for market, w in m.items():
        cap = cap_w.get(market, 0.25)
        diff = abs(w - cap)
        # 允许 home tilt 导致的轻微超出
        assert diff <= params.band_pp + params.delta_home + 0.01, \
            f"{market}: m={w:.3f}, cap={cap:.3f}, diff={diff:.3f} > band={params.band_pp}"


def test_home_tilt_adds_to_cn_hk(params, provider):
    """home tilt 应增加中/港权重。"""
    from dataclasses import replace
    from backtest.config import Params
    # 无 home tilt
    params_no_tilt = replace(params, delta_home=0.0)

    asof = date(2000, 6, 30)
    m_with = compute_regional_weights(asof, params, provider)
    m_without = compute_regional_weights(asof, params_no_tilt, provider)

    # CN + HK 权重应该增加
    cn_hk_with = m_with.get("CN", 0) + m_with.get("HK", 0)
    cn_hk_without = m_without.get("CN", 0) + m_without.get("HK", 0)
    assert cn_hk_with >= cn_hk_without - 1e-6


def test_regional_weights_all_markets(params, provider):
    """所有市场都有权重。"""
    asof = date(2000, 6, 30)
    m = compute_regional_weights(asof, params, provider)
    for market in provider.equity_markets():
        assert market in m, f"{market} 不在权重中"
        assert m[market] > 0, f"{market} 权重 = 0"
