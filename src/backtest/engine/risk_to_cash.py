"""
风险权重 → 现金权重转换 (逆波动率法)。

ALLOCATOR_PLAN §一A 第3步:
  cash_i ∝ risk_weight_i / vol_i, 归一化使总和=1。

使用混合 EWMA 波动率 (σ 用 ewma_fast_halflife/ewma_slow_halflife 混合)。
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Params
    from ..data.provider import MarketDataProvider


def risk_weights_to_cash_weights(
    risk_weights: dict[str, float],
    provider: "MarketDataProvider",
    asof: date,
) -> dict[str, float]:
    """将风险权重转换为现金权重: cash_i ∝ risk_weight_i / vol_i。

    Parameters
    ----------
    risk_weights : dict[str, float]
        各资产的风险权重 (总和=1)。
    provider : MarketDataProvider
        数据源，提供 vol() 方法。
    asof : date
        当前日期 (PIT)。

    Returns
    -------
    dict[str, float]
        各资产的现金权重 (总和=1)。
    """
    if not risk_weights:
        return {}

    cash_raw: dict[str, float] = {}
    for asset, rw in risk_weights.items():
        vol = provider.vol(asset, asof)
        if vol > 0:
            cash_raw[asset] = rw / vol
        else:
            cash_raw[asset] = 0.0

    total = sum(cash_raw.values())
    if total <= 0:
        # fallback: 等权
        n = len(risk_weights)
        return {a: 1.0 / n for a in risk_weights}

    return {a: v / total for a, v in cash_raw.items()}
