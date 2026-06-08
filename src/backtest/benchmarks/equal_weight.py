"""
等权基准 — 9 个腿各 1/9。

每月按已实现回报更新, 不做再平衡 (允许漂移)。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from ..config import BacktestConfig


class EqualWeight:
    """等权基准。"""

    def __init__(self, bt_cfg: BacktestConfig, all_legs: list[str]):
        self.bt_cfg = bt_cfg
        self.all_legs = all_legs

        bench_cfg = bt_cfg.benchmarks.get("equal_weight", {})
        weights = bench_cfg.get("weights", {})

        if weights:
            self.weights = dict(weights)
        else:
            # 默认: 等权
            n = len(all_legs)
            self.weights = {leg: 1.0 / n for leg in all_legs}

        self._nav = 1.0
        self._nav_history: dict = {}

    def mark_to_market(self, returns: dict[str, float], asof: date):
        """按固定权重更新 NAV。"""
        weighted_ret = 0.0
        for leg, w in self.weights.items():
            data_key = leg.replace("_equity", "")
            ret = returns.get(data_key, 0.0)
            weighted_ret += w * ret
        self._nav *= (1.0 + weighted_ret)
        self._nav_history[asof] = self._nav

    def nav_series(self) -> pd.Series:
        return pd.Series(self._nav_history)
