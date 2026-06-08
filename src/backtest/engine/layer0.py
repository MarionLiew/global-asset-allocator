"""
Layer 0: 总股票预算 E。

公式 (工程指南 §4.1):
  ERP = 1/CAPE_world - r_real
  E_raw = E_base + k0 * (ERP / ERP_ref - 1.0)
  E = clip(E_raw, E_base - 0.10, E_base + 0.10)  # ±10pp 微调带
  E = clip(E, E_min, E_max)

不变量:
  E ∈ [E_min, E_max]
  E ∈ [E_base - 0.10, E_base + 0.10]
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Params
    from ..data.provider import MarketDataProvider


def compute_equity_budget(asof: date, params: Params, md: MarketDataProvider) -> float:
    """Layer 0: ERP 信号 → 总股票预算 E (有界微调)。

    ERP = 1/CAPE_world - r_real
    E_raw = E_base + k0 * (ERP / ERP_ref - 1.0)
    E = clip(E_raw, E_base - 0.10, E_base + 0.10)
    E = clip(E, E_min, E_max)
    """
    earnings_yield = md.earnings_yield_world(asof)
    real_yield = md.real_yield(asof)
    erp = earnings_yield - real_yield  # 风险溢价

    erp_ref = md.erp_rolling_median(asof)  # ERP 滚动中位

    if erp_ref == 0:
        erp_ratio = 1.0
    else:
        erp_ratio = erp / erp_ref

    # E_raw = E_base + k0 * (ERP/ERP_ref - 1)
    E_raw = params.E_base + params.k0 * (erp_ratio - 1.0)

    # ±10pp 微调带
    E = max(params.E_base - 0.10, min(E_raw, params.E_base + 0.10))

    # [E_min, E_max]
    E = max(params.E_min, min(E, params.E_max))

    return E
