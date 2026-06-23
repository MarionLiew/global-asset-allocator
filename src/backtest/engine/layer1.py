"""
Layer 1: 跨市场地区权重 m_i。

旧逻辑 (保留向后兼容):
  raw_i = cap_weight_i * (CAPE_target_i / CAPE_i)^λ_i
  m = clip_to_band(raw, cap_weights, ±band_pp)
  m = normalize(m)

新逻辑 (ALLOCATOR_PLAN §一B):
  信号 = 估值 + 动量组合, 连续评分
  偏离锚硬上限 ±5pp (风险权重口径)
  只影响股票子组内部权重, 不影响攻防比例
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from .signals import combined_score

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


# ── 新: 倾斜层 (ALLOCATOR_PLAN §一B) ─────────────────────────────────────────


def compute_tilt(
    anchor_risk_weights: dict[str, float],
    provider: "MarketDataProvider",
    params: "Params",
    asof: date,
) -> dict[str, float]:
    """倾斜层: 估值+动量组合信号, 偏离锚硬上限 ±5pp。

    只影响股票子组内部的相对权重, 不改变总攻防比例。

    Parameters
    ----------
    anchor_risk_weights : dict[str, float]
        锚层输出的风险权重 (总和=1)。
    provider : MarketDataProvider
        数据源。
    params : Params
        策略参数 (w_val, w_mom, tilt_max, tilt_band_pp)。
    asof : date
        当前日期。

    Returns
    -------
    dict[str, float]
        倾斜后的最终权重 (总和=1)。
    """
    markets = provider.equity_markets()
    result = dict(anchor_risk_weights)

    # 计算各股票市场的组合信号
    signals: dict[str, float] = {}
    for mkt in markets:
        signals[mkt] = combined_score(
            mkt, provider, asof,
            w_val=params.w_val, w_mom=params.w_mom,
        )

    # 计算股票子组的锚总权重
    equity_anchor_total = sum(
        anchor_risk_weights.get(m, 0.0) for m in markets
    )
    if equity_anchor_total <= 0:
        return result

    # 对每个股票市场: 倾斜幅度 = signal * tilt_max * 子组总权重
    for mkt in markets:
        anchor_w = anchor_risk_weights.get(mkt, 0.0)
        sig = signals.get(mkt, 0.0)

        # 倾斜量 (相对于子组总权重的比例)
        tilt_delta = sig * params.tilt_max * equity_anchor_total

        # 硬限: 偏离锚 ≤ ±tilt_band_pp (绝对值)
        new_w = anchor_w + tilt_delta
        deviation = abs(new_w - anchor_w)
        if deviation > params.tilt_band_pp:
            # 截断到带边缘
            if tilt_delta > 0:
                new_w = anchor_w + params.tilt_band_pp
            else:
                new_w = anchor_w - params.tilt_band_pp

        new_w = max(0.0, new_w)
        result[mkt] = new_w

    # 归一化 (保持总和=1, 防御部分不参与倾斜)
    total = sum(result.values())
    if total > 0 and abs(total - 1.0) > 1e-9:
        result = {a: w / total for a, w in result.items()}

    return result
