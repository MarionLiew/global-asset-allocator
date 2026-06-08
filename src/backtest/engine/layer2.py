"""
Layer 2: 防御腿构成 d_j。

公式 (工程指南 §4.1):
  d_j = 1 / vol_j  (混合 EWMA 波动率)
  d = apply_quadrant_tilt(d, quadrant, delta=±8pp)
  d = cap_and_renormalize(d, cap=defensive_single_asset_cap)

不变量:
  sum(d.values()) ≈ 1.0
  max(d.values()) ≤ defensive_single_asset_cap
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from .quadrant import apply_quadrant_tilt

if TYPE_CHECKING:
    from ..config import Params
    from ..data.provider import MarketDataProvider


def compute_defensive_weights(
    asof: date,
    params: Params,
    md: MarketDataProvider,
) -> dict[str, float]:
    """Layer 2: 逆波动 + 象限倾斜 → 防御腿构成。"""
    assets = md.defensive_assets()

    # Step 1: 逆波动
    raw = {}
    for j in assets:
        vol_j = md.vol(j, asof)
        if vol_j > 0:
            raw[j] = 1.0 / vol_j
        else:
            raw[j] = 1.0  # fallback

    # normalize
    total = sum(raw.values())
    if total > 0:
        d = {j: w / total for j, w in raw.items()}
    else:
        n = len(assets)
        d = {j: 1.0 / n for j in assets}

    # Step 2: 象限倾斜
    quadrant = md.growth_inflation_quadrant(asof)
    d = apply_quadrant_tilt(d, quadrant, delta=params.delta_quadrant)

    # 确保非负
    d = {j: max(0.0, w) for j, w in d.items()}

    # Step 3: 封顶 + 重新归一化
    d = _cap_and_renormalize(d, params.defensive_single_asset_cap)

    return d


def _cap_and_renormalize(d: dict[str, float], cap: float) -> dict[str, float]:
    """单资产封顶, 超出部分按比例分配给其他资产。"""
    result = dict(d)

    # 迭代封顶 (可能需要多轮)
    for _ in range(10):
        capped = {j: w for j, w in result.items() if w > cap}
        if not capped:
            break

        excess = sum(w - cap for j, w in capped.items())
        for j in capped:
            result[j] = cap

        # 分配 excess 给未封顶资产
        uncapped = {j: w for j, w in result.items() if w < cap}
        uncapped_total = sum(uncapped.values())
        if uncapped_total > 0:
            for j in uncapped:
                result[j] += excess * (result[j] / uncapped_total)
        else:
            # 全部都封顶了 → 等分
            n = len(result)
            result = {j: 1.0 / n for j in result}
            break

    # 最终 normalize
    total = sum(result.values())
    if total > 0:
        result = {j: w / total for j, w in result.items()}

    return result
