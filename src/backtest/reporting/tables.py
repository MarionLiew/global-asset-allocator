"""
结果表 — 年化收益、波动率、最大回撤、Sharpe 等核心指标。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..schema import BacktestResult


def compute_nav_metrics(nav: pd.Series, label: str = "strategy") -> dict[str, float]:
    """计算 NAV 序列的核心指标。"""
    if nav.empty or len(nav) < 2:
        return {"label": label}

    # 月度回报
    rets = nav.pct_change().dropna()
    n_months = len(rets)

    # 年化收益
    total_return = nav.iloc[-1] / nav.iloc[0] - 1
    years = n_months / 12
    annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    # 年化波动率
    annual_vol = rets.std() * np.sqrt(12)

    # 最大回撤
    cummax = nav.cummax()
    drawdown = (nav - cummax) / cummax
    max_drawdown = drawdown.min()

    # Sharpe (假设无风险利率 0)
    sharpe = annual_return / annual_vol if annual_vol > 0 else 0

    # Calmar
    calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0

    return {
        "label": label,
        "总回报": f"{total_return:.1%}",
        "年化收益": f"{annual_return:.2%}",
        "年化波动率": f"{annual_vol:.2%}",
        "最大回撤": f"{max_drawdown:.1%}",
        "Sharpe": f"{sharpe:.2f}",
        "Calmar": f"{calmar:.2f}",
        "月数": n_months,
    }


def compute_summary(result: BacktestResult) -> pd.DataFrame:
    """生成对比汇总表。"""
    rows = []

    # 策略
    if not result.strategy_nav.empty:
        rows.append(compute_nav_metrics(result.strategy_nav, "策略"))

    # 基准
    for name, nav in result.benchmark_navs.items():
        if not nav.empty:
            rows.append(compute_nav_metrics(nav, name))

    df = pd.DataFrame(rows)
    if "label" in df.columns:
        df = df.set_index("label")
    return df


def compute_incremental(result: BacktestResult) -> pd.DataFrame:
    """计算策略相对基准的增量收益。"""
    if result.strategy_nav.empty:
        return pd.DataFrame()

    strategy_rets = result.strategy_nav.pct_change().dropna()
    rows = []

    for name, nav in result.benchmark_navs.items():
        if nav.empty:
            continue
        bench_rets = nav.pct_change().dropna()
        # 对齐索引
        common = strategy_rets.index.intersection(bench_rets.index)
        if len(common) == 0:
            continue
        incremental = strategy_rets.loc[common] - bench_rets.loc[common]

        inc_nav = (1 + incremental).cumprod()
        total_inc = inc_nav.iloc[-1] - 1
        ann_inc = incremental.mean() * 12  # 年化增量
        tracking_err = incremental.std() * np.sqrt(12)
        info_ratio = ann_inc / tracking_err if tracking_err > 0 else 0

        rows.append({
            "基准": name,
            "累计增量": f"{total_inc:.2%}",
            "年化增量": f"{ann_inc:.2%}",
            "跟踪误差": f"{tracking_err:.2%}",
            "信息比率": f"{info_ratio:.2f}",
        })

    return pd.DataFrame(rows).set_index("基准") if rows else pd.DataFrame()
