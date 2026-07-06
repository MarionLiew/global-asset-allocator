#!/usr/bin/env python3
"""
执行方式对比回测 — 回答"只买不卖 vs 月度NTZ含卖出 vs 年度再平衡"哪个好。

同一套锚+倾斜权重, 只换执行层, 对比:
  ntz      — 月度不交易区: 新钱补欠配, 超配卖回带边缘 (回测原默认)
  buy_only — 只买不卖: 超配靠后续新钱稀释 (实盘 monthly_ops 的规则)
  annual   — 平时只买不卖, 每年12月强制全量再平衡到精确目标

用法:
    python scripts/compare_execution.py
"""

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backtest.config import Params, BacktestConfig  # noqa: E402
from backtest.data.csv_provider import CSVProvider  # noqa: E402
from backtest.engine.backtest_loop import run_backtest_v2  # noqa: E402
from backtest.reporting.tables import compute_summary  # noqa: E402

MODES = {
    "ntz": "月度NTZ(含卖出)",
    "buy_only": "只买不卖",
    "annual": "年度再平衡",
}


def main():
    params = Params.load("config/params.yaml")
    bt_cfg = BacktestConfig.load("config/backtest.yaml")
    md = CSVProvider(str(PROJECT_ROOT), params)

    rows = []
    for mode, label in MODES.items():
        result = run_backtest_v2(params, bt_cfg, md, execution_mode=mode)
        summary = compute_summary(result)
        strat = summary.loc["策略"].copy()
        strat.name = label
        strat["总交易成本"] = f"¥{result.total_costs:,.0f}"
        rows.append(strat)

    df = pd.DataFrame(rows)
    print(f"\n=== 执行方式对比 ({bt_cfg.start_date} ~ {bt_cfg.end_date}, "
          f"月投 ¥{bt_cfg.monthly_contribution_cny:,.0f}) ===\n")
    print(df.to_string())
    print("\n(同一套锚+倾斜权重, 只换执行层; 差异来自交易成本与漂移容忍度)")


if __name__ == "__main__":
    main()
