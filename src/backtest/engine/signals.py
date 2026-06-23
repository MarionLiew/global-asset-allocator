"""
信号层 — 估值 + 动量组合信号。

ALLOCATOR_PLAN §一B:
  - 默认「估值 + 动量」组合, 纯估值降为对照
  - 连续评分, 不用阈值开关
  - 估值与动量负相关互补 (Asness et al.)

信号输出标准化到 [-1, +1]:
  - 估值: (CAPE_target/CAPE - 1) → z-score → clip
  - 动量: 已在 csv_provider 中预计算为 [-1, +1]
  - 组合: w_val * val + w_mom * mom → clip [-1, +1]
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Params
    from ..data.provider import MarketDataProvider


def valuation_score(market: str, provider: "MarketDataProvider", asof: date) -> float:
    """CAPE 估值分数: CAPE_target/CAPE - 1, 标准化到 [-1, +1]。

    高分 = 便宜 (CAPE < target), 低分 = 贵 (CAPE > target)。
    """
    cape = provider.cape(market, asof)
    cape_target = provider.cape_target(market, asof)

    if cape <= 0 or cape_target <= 0:
        return 0.0

    raw = cape_target / cape - 1.0
    # 将 raw 映射到 [-1, +1]
    # 典型范围: raw ∈ [-0.5, +0.5] (CAPE 偏离目标 ±50%)
    # 用 tanh 或简单 clip
    return max(-1.0, min(raw, 1.0))


def momentum_score(market: str, provider: "MarketDataProvider", asof: date) -> float:
    """动量分数: 直接用 provider.momentum(), 已标准化到 [-1, +1]。"""
    return provider.momentum(market, asof)


def combined_score(
    market: str,
    provider: "MarketDataProvider",
    asof: date,
    w_val: float = 0.5,
    w_mom: float = 0.5,
) -> float:
    """估值 + 动量组合信号: w_val * val + w_mom * mom, clip [-1, +1]。"""
    val = valuation_score(market, provider, asof)
    mom = momentum_score(market, provider, asof)
    combined = w_val * val + w_mom * mom
    return max(-1.0, min(combined, 1.0))


def valuation_only_score(
    market: str,
    provider: "MarketDataProvider",
    asof: date,
) -> float:
    """纯估值信号 (对照用, 用于归因对比)。"""
    return valuation_score(market, provider, asof)
