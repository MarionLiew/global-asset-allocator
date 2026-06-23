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
        # TWR: 记录 mark-to-market 后、贡献注入前的 NAV
        self._nav_after_mt: list[tuple[date, float]] = []

    def mark_to_market(self, returns_cny: dict[str, float], asof: date):
        """用已实现回报更新各腿市值 (在贡献注入前调用)。"""
        for leg in list(self.holdings.keys()):
            data_key = leg.replace("_equity", "")
            ret = returns_cny.get(data_key, 0.0)
            self.holdings[leg] *= (1.0 + ret)
        # 记录 mark-to-market 后的 NAV (贡献注入前, 用于 TWR)
        nav = sum(self.holdings.values())
        self._nav_after_mt.append((asof, nav))

    def execute_month(
        self,
        asof: date,
        targets: dict[str, float],
        contribution_cny: float,
    ):
        """月度执行: 注入新钱 + 完全再平衡到目标权重 (无成本简化)。

        在 mark_to_market() 之后调用。
        完全再平衡 (非只买不卖), 避免超配累积偏差。
        """
        # 注入贡献
        nav = sum(self.holdings.values())
        available = contribution_cny + nav

        # 完全再平衡: 直接设置为目标权重 × 可用资金
        for leg, tgt_w in targets.items():
            self.holdings[leg] = tgt_w * available

    def nav_series(self) -> pd.Series:
        """返回 TWR 净值序列 (标准化到 1.0)。

        TWR = ∏ (1 + r_t), 其中 r_t = NAV_after_MT_t / NAV_after_exec_{t-1} - 1
        NAV_after_MT = mark-to-market 后、贡献注入前
        NAV_after_exec = 上月执行后 (含贡献)
        """
        if not self._nav_after_mt:
            return pd.Series(dtype=float)

        dates = [d for d, _ in self._nav_after_mt]
        navs_after_mt = [n for _, n in self._nav_after_mt]

        # 第一个月: 只有贡献, 无回报
        twr = [1.0]

        for i in range(len(navs_after_mt)):
            nav_mt = navs_after_mt[i]  # 本月 mark-to-market 后 (贡献前)

            if i == 0:
                # 第一个月: 贡献注入后 mark-to-market
                # NAV_after_exec_0 = nav_mt + contribution
                # 无上月参考, r=0
                prev_exec_nav = nav_mt + self.bt_cfg.monthly_contribution_cny
                continue

            # r_t = (nav_after_MT_t - 上月执行后 NAV) / 上月执行后 NAV
            #     = (nav_after_MT_t / 上月执行后 NAV) - 1
            if prev_exec_nav > 0:
                r = nav_mt / prev_exec_nav - 1
                twr.append(twr[-1] * (1 + r))
            else:
                twr.append(twr[-1])

            # 本月执行后 NAV = nav_mt + contribution
            prev_exec_nav = nav_mt + self.bt_cfg.monthly_contribution_cny

        # 对齐日期: 第一个月 TWR=1.0, 之后每个月有回报
        result = pd.Series(twr, index=dates)
        return result / result.iloc[0]
