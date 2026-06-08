"""
MarketDataProvider 协议 — 引擎消费的数据接口。

所有数据只返回 asof 之前可见的 (PIT 纪律)。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable


@runtime_checkable
class MarketDataProvider(Protocol):
    """引擎消费的数据接口。所有数据只返回 asof 之前可见的。"""

    def equity_markets(self) -> list[str]:
        """返回股票市场列表, 如 ["US", "DM", "CN", "HK"]。"""
        ...

    def defensive_assets(self) -> list[str]:
        """返回防御资产列表, 如 ["CN_GOVT", "TIPS", "GOLD", "CORP_BOND", "EM_BOND"]。"""
        ...

    def cape(self, market: str, asof: date) -> float:
        """某市场在 asof 时的 CAPE。"""
        ...

    def cape_target(self, market: str, asof: date) -> float:
        """某市场在 asof 时的历史中位 CAPE (滚动窗口)。"""
        ...

    def cap_weight(self, market: str, asof: date) -> float:
        """某市场在 asof 时的市值权重。"""
        ...

    def cap_weights(self, asof: date) -> dict[str, float]:
        """所有市场在 asof 时的市值权重。"""
        ...

    def earnings_yield_world(self, asof: date) -> float:
        """全球盈利收益率 = 1/CAPE_world。"""
        ...

    def real_yield(self, asof: date) -> float:
        """实际利率。"""
        ...

    def erp_rolling_median(self, asof: date) -> float:
        """ERP 滚动中位。"""
        ...

    def vol(self, asset: str, asof: date) -> float:
        """某资产在 asof 时的混合 EWMA 波动率 (年化)。"""
        ...

    def monthly_return(self, asset: str, asof: date) -> float:
        """某资产在 asof 月的总回报 (本币)。"""
        ...

    def monthly_return_cny(self, asset: str, asof: date) -> float:
        """某资产在 asof 月的总回报 (折 CNY)。"""
        ...

    def growth_inflation_quadrant(self, asof: date) -> str:
        """象限分类: "GG" / "GI" / "IG" / "II"。"""
        ...

    def fx_rate(self, currency: str, asof: date) -> float:
        """币种对 CNY 的汇率。CNY 自身为 1.0。"""
        ...
