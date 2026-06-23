"""
锚基准 — handcrafting 锚 + 零倾斜 + 月度定投。

ALLOCATOR_PLAN §三:
  主基准 = 朴素分散版 (handcrafting 锚 + 零倾斜 + 月度定投), 零 judgment。

这是验证倾斜增量的对照基线。
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

from ..config import BacktestConfig
from ..data._constants import EQUITY_MARKETS, DEFENSIVE_ASSETS, MARKET_CURRENCY

if TYPE_CHECKING:
    from ..data.provider import MarketDataProvider
    from ..config import Params


class AnchorOnlyBenchmark:
    """朴素分散基准: handcrafting 锚 + 零倾斜 + 月度定投。"""

    def __init__(self, bt_cfg: BacktestConfig, all_legs: list[str]):
        self.bt_cfg = bt_cfg
        self.all_legs = all_legs
        self.holdings: dict[str, float] = {}
        self.nav_history: dict = {}
        self._nav_at_last_dt: dict[str, float] = {}

    def execute_month(
        self,
        asof: date,
        targets: dict[str, float],
        contribution_cny: float,
    ):
        """月度执行: 注入新钱按目标权重分配 (无成本简化)。"""
        nav = sum(self.holdings.values())
        available = contribution_cny + nav

        for leg, tgt_w in targets.items():
            target_cny = tgt_w * available
            current_cny = self.holdings.get(leg, 0.0)
            gap = target_cny - current_cny
            if gap > 0:
                self.holdings[leg] = current_cny + gap

        self.nav_history[asof] = sum(self.holdings.values())

    def mark_to_market(self, returns_cny: dict[str, float], asof: date):
        """用已实现回报更新各腿市值。"""
        for leg in list(self.holdings.keys()):
            data_key = leg.replace("_equity", "")
            ret = returns_cny.get(data_key, 0.0)
            self.holdings[leg] *= (1.0 + ret)
        self.nav_history[asof] = sum(self.holdings.values())

    def nav_series(self) -> pd.Series:
        """返回 TWR 净值序列 (标准化到 1.0)。"""
        if not self.nav_history:
            return pd.Series(dtype=float)
        s = pd.Series(self.nav_history).sort_index()
        # 计算 TWR
        twr = [1.0]
        navs = s.values
        for i in range(1, len(navs)):
            # 需要分离贡献 vs 回报
            # 简化: 直接用 NAV 变化 (因每月贡献相同且在月初注入)
            if navs[i - 1] > 0:
                # 估算月回报: (NAV_t - contribution) / NAV_{t-1} - 1
                contribution = self.bt_cfg.monthly_contribution_cny
                market_nav = navs[i] - contribution
                if navs[i - 1] > 0:
                    r = market_nav / navs[i - 1] - 1
                    twr.append(twr[-1] * (1 + r))
                else:
                    twr.append(twr[-1])
            else:
                twr.append(1.0)
        result = pd.Series(twr, index=s.index)
        return result / result.iloc[0]
