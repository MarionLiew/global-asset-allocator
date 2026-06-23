"""
风险贡献报告 — 风险作为一等公民。

ALLOCATOR_PLAN §四:
  - 输出实际波动率 vs 目标
  - 各组风险贡献 (非现金权重)
  - 纯现货无杠杆的波动率缺口报告
"""

from __future__ import annotations

import logging

import pandas as pd

from ..schema import BacktestResult
from ..data._constants import GROUP_TREE

logger = logging.getLogger(__name__)


def compute_risk_report(result: BacktestResult) -> pd.DataFrame:
    """风险贡献报告: 各资产的风险权重 vs 现金权重对比。

    返回 DataFrame:
    - asset: 资产名
    - group: 所属子组 (equity/rates/real_credit)
    - risk_weight_pct: 平均风险权重 (%)
    - cash_weight_pct: 平均现金权重 (%)
    - vol_ann_pct: 年化波动率 (%)
    """
    if not result.weight_history:
        return pd.DataFrame()

    # 收集所有快照的风险权重和现金权重
    risk_records = []
    for snap in result.weight_history:
        if snap.risk_weights:
            risk_records.append(snap.risk_weights)

    if not risk_records:
        return pd.DataFrame()

    # 平均风险权重
    all_assets = set()
    for r in risk_records:
        all_assets.update(r.keys())

    rows = []
    for asset in sorted(all_assets):
        # 确定所属子组
        group = _find_group(asset)

        # 平均风险权重
        avg_risk = sum(r.get(asset, 0) for r in risk_records) / len(risk_records)

        # 平均现金权重 (如果有)
        cash_records = [s.cash_weights for s in result.weight_history if s.cash_weights]
        avg_cash = (
            sum(c.get(asset, 0) for c in cash_records) / len(cash_records)
            if cash_records else 0.0
        )

        rows.append({
            "asset": asset,
            "group": group,
            "risk_weight_pct": f"{avg_risk:.2%}",
            "cash_weight_pct": f"{avg_cash:.2%}",
        })

    return pd.DataFrame(rows)


def compute_vol_gap(result: BacktestResult, target_vol: float = 0.10) -> dict:
    """波动率缺口报告: 实际波动率 vs 目标。

    Returns
    -------
    dict with keys:
        actual_vol: 实际年化波动率
        target_vol: 目标波动率
        gap: 缺口 (target - actual)
        gap_pct: 缺口百分比
    """
    if result.strategy_nav.empty:
        return {"actual_vol": 0, "target_vol": target_vol, "gap": target_vol, "gap_pct": "100%"}

    rets = result.strategy_nav.pct_change().dropna()
    actual_vol = rets.std() * (12 ** 0.5)  # 年化

    gap = target_vol - actual_vol
    gap_pct = gap / target_vol if target_vol > 0 else 0

    return {
        "actual_vol": f"{actual_vol:.2%}",
        "target_vol": f"{target_vol:.2%}",
        "gap": f"{gap:.2%}",
        "gap_pct": f"{gap_pct:.1%}",
    }


def compute_regime_risk(result: BacktestResult, regimes: dict[str, dict] | None = None) -> pd.DataFrame:
    """各政体下的风险表现: 2022 利率冲击专项。

    重点: 锚的夏普中有多少来自债券久期红利。
    """
    if result.strategy_nav.empty:
        return pd.DataFrame()

    regimes = regimes or {
        "2000_dot_com": {"start": "2000-01-31", "end": "2002-10-31"},
        "2008_gfc": {"start": "2007-10-31", "end": "2009-03-31"},
        "2022_rate_shock": {"start": "2022-01-31", "end": "2022-10-31"},
    }

    nav = result.strategy_nav
    rows = []

    for regime_name, window in regimes.items():
        start = pd.Timestamp(window["start"])
        end = pd.Timestamp(window["end"])
        mask = (nav.index >= start) & (nav.index <= end)
        regime_nav = nav[mask]

        if len(regime_nav) < 2:
            continue

        total_ret = regime_nav.iloc[-1] / regime_nav.iloc[0] - 1
        rets = regime_nav.pct_change().dropna()
        vol = rets.std() * (12 ** 0.5)
        sharpe = total_ret / vol if vol > 0 else 0

        # 最大回撤
        cummax = regime_nav.cummax()
        dd = (regime_nav - cummax) / cummax
        max_dd = dd.min()

        rows.append({
            "regime": regime_name,
            "total_return": f"{total_ret:.2%}",
            "annual_vol": f"{vol:.2%}",
            "sharpe": f"{sharpe:.2f}",
            "max_drawdown": f"{max_dd:.2%}",
        })

    return pd.DataFrame(rows)


def _find_group(asset: str) -> str:
    """查找资产所属的子组。"""
    for sleeve_name, groups in GROUP_TREE.items():
        for group_name, members in groups.items():
            if asset in members:
                return f"{sleeve_name}/{group_name}"
    return "unknown"
