"""
Regime 分析 — 2000/2008/2022 分段表现。

关注:
- 各 regime 的回撤深度/持续期
- Layer 0: E 是否向 E_min 移动
- Layer 1: 地区权重变化
- Layer 2: 防御构成变化
- vs 基准比较
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..schema import BacktestResult


# 默认 regime 定义
DEFAULT_REGIMES = {
    "2000 科技泡沫": {"start": "2000-01-31", "end": "2002-10-31"},
    "2008 金融危机": {"start": "2007-10-31", "end": "2009-03-31"},
    "2022 利率冲击": {"start": "2022-01-31", "end": "2022-10-31"},
}


def compute_regime_analysis(result: BacktestResult) -> pd.DataFrame:
    """计算各 regime 的表现。"""
    rows = []

    for regime_name, window in DEFAULT_REGIMES.items():
        start = pd.Timestamp(window["start"])
        end = pd.Timestamp(window["end"])

        row = _analyze_regime(result, start, end, regime_name)
        rows.append(row)

    return pd.DataFrame(rows).set_index("regime")


def _analyze_regime(
    result: BacktestResult,
    start: pd.Timestamp,
    end: pd.Timestamp,
    label: str,
) -> dict:
    """分析单个 regime 的表现。"""
    nav = result.strategy_nav
    if nav.empty:
        return {"regime": label}

    # 截取 regime 窗口
    mask = (nav.index >= start) & (nav.index <= end)
    regime_nav = nav[mask]

    if regime_nav.empty or len(regime_nav) < 2:
        return {"regime": label, "备注": "数据不足"}

    # 回撤
    regime_nav_norm = regime_nav / regime_nav.iloc[0]
    cummax = regime_nav_norm.cummax()
    drawdown = (regime_nav_norm - cummax) / cummax
    max_dd = drawdown.min()

    # 总回报
    total_ret = regime_nav_norm.iloc[-1] - 1

    # E 值变化
    regime_weights = [w for w in result.weight_history
                      if start <= pd.Timestamp(w.asof) <= end]
    E_start = regime_weights[0].E if regime_weights else None
    E_end = regime_weights[-1].E if regime_weights else None

    # 基准回撤
    bench_max_dds = {}
    for bench_name, bench_nav in result.benchmark_navs.items():
        if bench_nav.empty:
            continue
        b_nav_idx = bench_nav.index
        if hasattr(b_nav_idx[0], 'date'):
            pass  # already Timestamp
        else:
            bench_nav.index = pd.to_datetime(b_nav_idx)
        b_mask = (bench_nav.index >= start) & (bench_nav.index <= end)
        b_nav = bench_nav[b_mask]
        if len(b_nav) >= 2:
            b_norm = b_nav / b_nav.iloc[0]
            b_cummax = b_norm.cummax()
            b_dd = (b_norm - b_cummax) / b_cummax
            bench_max_dds[bench_name] = f"{b_dd.min():.1%}"

    # 归因
    regime_attr = [a for a in result.attribution
                   if start <= pd.Timestamp(a.asof) <= end]
    attr_sums = {}
    if regime_attr:
        attr_sums = {
            "E择时": f"{sum(a.E_timing for a in regime_attr):.3%}",
            "地区倾斜": f"{sum(a.regional_tilt for a in regime_attr):.3%}",
            "防御构成": f"{sum(a.defensive_comp for a in regime_attr):.3%}",
        }

    return {
        "regime": label,
        "总回报": f"{total_ret:.1%}",
        "最大回撤": f"{max_dd:.1%}",
        "E 起始": f"{E_start:.2%}" if E_start else "N/A",
        "E 结束": f"{E_end:.2%}" if E_end else "N/A",
        **bench_max_dds,
        **attr_sums,
    }
