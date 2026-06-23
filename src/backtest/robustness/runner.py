"""
稳健性回测运行器 — 批量运行参数扰动回测。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from ..config import Params, BacktestConfig
from ..data.provider import MarketDataProvider
from ..engine.backtest_loop import run_backtest
from .perturb import generate_perturbations, PerturbationSpec, DEFAULT_SPECS

logger = logging.getLogger(__name__)


def run_robustness(
    base_params: Params,
    bt_cfg: BacktestConfig,
    md: MarketDataProvider,
    specs: list[PerturbationSpec] | None = None,
    regimes: dict[str, dict] | None = None,
) -> list[dict]:
    """运行参数稳健性测试。

    对每个参数, 逐一扰动并重跑回测, 记录关键指标。
    """
    variants = generate_perturbations(base_params, specs)
    regimes = regimes or {
        "2000": {"start": "2000-01-31", "end": "2002-10-31"},
        "2008": {"start": "2007-10-31", "end": "2009-03-31"},
        "2022": {"start": "2022-01-31", "end": "2022-10-31"},
    }

    results = []
    total = len(variants)

    for i, (param_name, value, params) in enumerate(variants):
        logger.info(f"[{i+1}/{total}] 扰动 {param_name}={value}...")

        try:
            result = run_backtest(params, bt_cfg, md)
            nav = result.strategy_nav

            if nav.empty:
                continue

            # 基础指标
            rets = nav.pct_change().dropna()
            n_months = len(rets)
            total_ret = nav.iloc[-1] / nav.iloc[0] - 1
            years = n_months / 12
            ann_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0
            ann_vol = rets.std() * np.sqrt(12)
            sharpe = ann_ret / ann_vol if ann_vol > 0 else 0

            row = {
                "param_name": param_name,
                "perturbed_value": value,
                "base_value": getattr(base_params, param_name, value),
                "annual_return": f"{ann_ret:.2%}",
                "annual_vol": f"{ann_vol:.2%}",
                "sharpe": f"{sharpe:.2f}",
            }

            # 各 regime 回撤
            for regime_name, window in regimes.items():
                start = pd.Timestamp(window["start"])
                end = pd.Timestamp(window["end"])
                mask = (nav.index >= start) & (nav.index <= end)
                regime_nav = nav[mask]
                if len(regime_nav) >= 2:
                    r_norm = regime_nav / regime_nav.iloc[0]
                    cummax = r_norm.cummax()
                    dd = (r_norm - cummax) / cummax
                    row[f"max_drawdown_{regime_name}"] = f"{dd.min():.1%}"

            results.append(row)

        except Exception as e:
            logger.warning(f"  扰动 {param_name}={value} 失败: {e}")

    return results
