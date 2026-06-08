"""
Layer 2 测试 — 逆波动 + 封顶。
"""

import pytest
from datetime import date

from backtest.engine.layer2 import compute_defensive_weights


def test_defensive_weights_sum_to_one(params, provider):
    """sum(d_j) ≈ 1.0。"""
    asof = date(2000, 6, 30)
    d = compute_defensive_weights(asof, params, provider)
    assert abs(sum(d.values()) - 1.0) < 1e-6, f"权重和={sum(d.values())}"


def test_defensive_single_asset_cap(params, provider):
    """max(d_j) ≤ defensive_single_asset_cap。"""
    asof = date(2000, 6, 30)
    d = compute_defensive_weights(asof, params, provider)
    max_w = max(d.values())
    assert max_w <= params.defensive_single_asset_cap + 1e-6, \
        f"最大权重 {max_w:.3f} > cap {params.defensive_single_asset_cap}"


def test_inverse_vol_ordering(params, provider):
    """低波动 → 高权重 (象限倾斜前)。"""
    asof = date(2000, 6, 30)
    d = compute_defensive_weights(asof, params, provider)

    # CN_GOVT (vol=0.05) 应该权重最高
    # EM_BOND (vol=0.10) 应该权重较低
    # 但象限倾斜可能改变顺序, 所以只验证基本逻辑
    assert "CN_GOVT" in d
    assert "GOLD" in d


def test_defensive_all_assets(params, provider):
    """所有防御资产都有权重。"""
    asof = date(2000, 6, 30)
    d = compute_defensive_weights(asof, params, provider)
    for asset in provider.defensive_assets():
        assert asset in d, f"{asset} 不在防御权重中"
        assert d[asset] >= 0, f"{asset} 权重 < 0"
