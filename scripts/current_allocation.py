#!/usr/bin/env python3
"""
输出基于最新数据的目标配置权重。

用法:
    python scripts/current_allocation.py            # 用现有 processed 数据
    python scripts/current_allocation.py --refresh   # 先抓取最新数据再计算
"""

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main():
    parser = argparse.ArgumentParser(description="计算最新目标配置权重")
    parser.add_argument("--refresh", action="store_true", help="先抓取最新市场数据")
    parser.add_argument("--params", default="config/params.yaml")
    parser.add_argument("--config", default="config/backtest.yaml")
    args = parser.parse_args()

    if args.refresh:
        subprocess.check_call([sys.executable, str(PROJECT_ROOT / "scripts" / "fetch_data.py")])

    import pandas as pd

    from backtest.config import Params, BacktestConfig
    from backtest.data.csv_provider import CSVProvider
    from backtest.engine.backtest_loop import run_backtest

    params = Params.load(args.params)
    bt_cfg = BacktestConfig.load(args.config)

    # 每个资产都有数据的最新月份 = 全部 legs 最大日期的交集
    import dataclasses

    etf = pd.read_parquet(PROJECT_ROOT / "data" / "processed" / "etf_returns.parquet")
    latest_common = etf.groupby("asset_id")["date"].max().min()
    bt_cfg = dataclasses.replace(bt_cfg, end_date=latest_common.strftime("%Y-%m-%d"))

    md = CSVProvider(str(PROJECT_ROOT), params)
    result = run_backtest(params, bt_cfg, md)

    snap = result.weight_history[-1]
    print(f"\n=== 目标配置 (基于 {snap.asof} 数据) ===\n")

    print("股票篮子:")
    for m, w in sorted(snap.m_i.items(), key=lambda x: -x[1]):
        print(f"  {m:12s} {w * snap.E * 100:6.2f}%  (股票内部占比 {w*100:5.1f}%)")

    print(f"\n防御篮子 (占比 {1 - snap.E:.1%}):")
    for a, w in sorted(snap.d_j.items(), key=lambda x: -x[1]):
        print(f"  {a:12s} {w * (1 - snap.E) * 100:6.2f}%  (防御内部占比 {w*100:5.1f}%)")

    print(f"\n股票总预算 E = {snap.E:.1%}  |  防御总预算 = {1 - snap.E:.1%}")
    print("\n=== 完整明细 (targets) ===")
    for k, v in sorted(snap.targets.items(), key=lambda x: -x[1]):
        print(f"  {k:15s} {v * 100:6.2f}%")


if __name__ == "__main__":
    main()
