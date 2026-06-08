"""
静态 60/40 基准 — 权重永不改变。

equity_sleeve: {US: 0.50, DM: 0.20, CN: 0.20, HK: 0.10} (占 60%)
defensive_sleeve: {CN_GOVT: 0.30, TIPS: 0.25, GOLD: 0.20, CORP_BOND: 0.15, EM_BOND: 0.10} (占 40%)

同样应用成本模型 (公平比较)。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from ..config import BacktestConfig


class StaticSixtyForty:
    """静态 60/40 基准。"""

    def __init__(self, bt_cfg: BacktestConfig, all_legs: list[str]):
        self.bt_cfg = bt_cfg
        self.all_legs = all_legs

        bench_cfg = bt_cfg.benchmarks.get("static_60_40", {})
        eq_w = bench_cfg.get("equity_sleeve", {})
        def_w = bench_cfg.get("defensive_sleeve", {})
        eq_total = bench_cfg.get("equity_weight", 0.60)
        def_total = bench_cfg.get("defensive_weight", 0.40)

        # 合成权重
        self.weights = {}
        for mkt, w in eq_w.items():
            self.weights[f"{mkt}_equity"] = eq_total * w
        for j, w in def_w.items():
            self.weights[j] = def_total * w

        # NAV 序列
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
