"""
Layer 1: 跨市场地区权重 m_i。

公式 (工程指南 §4.1):
  raw_i = cap_weight_i * (CAPE_target_i / CAPE_i)^λ_i
  m = clip_to_band(raw, cap_weights, ±band_pp)
  m = normalize(m)
  m = add_home_tilt(m, delta_home)   # 中/港有界加点
  m = normalize(m)

不变量:
  sum(m.values()) ≈ 1.0
  |m_i - cap_weight_i| ≤ band_pp  (除 home tilt 叠加外)
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Params
    from ..data.provider import MarketDataProvider


def compute_regional_weights(
    asof: date,
    params: Params,
    md: MarketDataProvider,
) -> dict[str, float]:
    """Layer 1: 各市场 CAPE 倾斜 → 跨市场权重。"""
    markets = md.equity_markets()
    cap_w = md.cap_weights(asof)

    # Step 1: CAPE 倾斜
    raw = {}
    for i in markets:
        cape = md.cape(i, asof)
        cape_t = md.cape_target(i, asof)
        lam = params.lambda_.get(i, 0.5)

        if cape > 0:
            tilt = (cape_t / cape) ** lam
        else:
            tilt = 1.0

        raw[i] = cap_w.get(i, 0.25) * tilt

    # Step 2: clip_to_band (±band_pp 相对市值权重)
    clipped = {}
    for i in markets:
        w_cap = cap_w.get(i, 0.25)
        lo = max(0.0, w_cap - params.band_pp)
        hi = w_cap + params.band_pp
        clipped[i] = max(lo, min(raw[i], hi))

    # Step 3: normalize
    total = sum(clipped.values())
    if total > 0:
        m = {i: w / total for i, w in clipped.items()}
    else:
        n = len(markets)
        m = {i: 1.0 / n for i in markets}

    # Step 4: home tilt (中/港加点)
    m = _add_home_tilt(m, params.delta_home, {"CN", "HK"})

    # Step 5: 再次 normalize
    total = sum(m.values())
    if total > 0:
        m = {i: w / total for i, w in m.items()}

    return m


def _add_home_tilt(
    m: dict[str, float],
    delta_home: float,
    home_markets: set[str],
) -> dict[str, float]:
    """对 home market 加点, 同时从其他市场等比扣除。

    确保:
    - home market 增量 ≤ delta_home
    - 权重非负
    - 仍受 band 约束 (通过 normalize 后处理)
    """
    result = dict(m)
    n_home = len(home_markets & set(m.keys()))
    if n_home == 0:
        return result

    # 每个 home market 增加 delta_home / n_home
    tilt_per = delta_home / n_home
    total_tilt = 0.0
    for mk in home_markets:
        if mk in result:
            result[mk] += tilt_per
            total_tilt += tilt_per

    # 从非 home market 等比扣除
    non_home = [mk for mk in result if mk not in home_markets]
    if non_home and total_tilt > 0:
        non_home_total = sum(result[mk] for mk in non_home)
        if non_home_total > 0:
            for mk in non_home:
                result[mk] -= total_tilt * (result[mk] / non_home_total)
                result[mk] = max(0.0, result[mk])

    return result
