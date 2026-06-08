"""
参数稳健性报告 — 扰动 vs 关键指标。

判断是否"刀尖上": 每个参数 ±20% 扰动, 看 2000/2008/2022 的表现变化是否平滑。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_robustness_table(results: list[dict]) -> pd.DataFrame:
    """从参数扰动结果生成稳健性报告。

    参数:
        results: 列表, 每项包含:
            - param_name: 参数名
            - perturbed_value: 扰动后值
            - base_value: 基准值
            - max_drawdown_2000/2008/2022: 各 regime 最大回撤
            - annual_return: 年化收益
            - sharpe: Sharpe

    返回: DataFrame, 显示各参数扰动的敏感度。
    """
    if not results:
        return pd.DataFrame()

    df = pd.DataFrame(results)

    # 计算每个参数的敏感度 (回撤变化 / 参数变化)
    sensitivity_rows = []
    for param in df["param_name"].unique():
        sub = df[df["param_name"] == param]
        base = sub[sub["perturbed_value"] == sub["base_value"]]
        if base.empty:
            continue

        base_row = base.iloc[0]
        for _, row in sub.iterrows():
            if row["perturbed_value"] == row["base_value"]:
                continue
            pct_change = (row["perturbed_value"] - row["base_value"]) / row["base_value"]

            # 各 regime 回撤变化
            dd_changes = {}
            for regime in ["2000", "2008", "2022"]:
                dd_key = f"max_drawdown_{regime}"
                if dd_key in row and dd_key in base_row:
                    base_dd = float(str(base_row[dd_key]).strip('%')) / 100
                    this_dd = float(str(row[dd_key]).strip('%')) / 100
                    dd_changes[f"DD_{regime}_变化"] = this_dd - base_dd

            sensitivity_rows.append({
                "参数": param,
                "扰动": f"{pct_change:+.0%}",
                **dd_changes,
            })

    return pd.DataFrame(sensitivity_rows)


def is_knife_edge(sensitivity_df: pd.DataFrame, threshold: float = 0.05) -> bool:
    """判断参数是否在刀尖上。

    标准: 任一参数 ±20% 扰动导致回撤变化 > threshold (5pp)。
    """
    if sensitivity_df.empty:
        return False

    dd_cols = [c for c in sensitivity_df.columns if c.startswith("DD_")]
    if not dd_cols:
        return False

    max_change = sensitivity_df[dd_cols].abs().max().max()
    return max_change > threshold
