#!/usr/bin/env python3
"""
输出基于最新数据的目标配置权重。

用法:
    python scripts/current_allocation.py                  # 只看比例
    python scripts/current_allocation.py --refresh         # 先抓取最新数据再计算
    python scripts/current_allocation.py --total 500000    # 按人民币总金额算出每个账户该入多少钱
"""

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# leg -> (入金账户, 计价货币)
ASSET_ACCOUNT = {
    "US_equity":  ("Schwab", "USD"),
    "DM_equity":  ("Schwab", "USD"),
    "TIPS":       ("Schwab", "USD"),
    "CORP_BOND":  ("Schwab", "USD"),
    "EM_BOND":    ("Schwab", "USD"),
    "GOLD":       ("OKX (XAUT现货)", "USD"),
    "CN_equity":  ("同花顺", "CNY"),
    "CN_GOVT":    ("同花顺", "CNY"),
    "HK_equity":  ("ZA", "HKD"),
}


def main():
    parser = argparse.ArgumentParser(description="计算最新目标配置权重")
    parser.add_argument("--refresh", action="store_true", help="先抓取最新市场数据")
    parser.add_argument("--params", default="config/params.yaml")
    parser.add_argument("--config", default="config/backtest.yaml")
    parser.add_argument("--total", type=float, default=None, help="本次入金总金额 (人民币)。不指定则只输出比例")
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

    # 按账户汇总
    account_weight = {}
    for k, v in snap.targets.items():
        account, currency = ASSET_ACCOUNT.get(k, ("未映射", "CNY"))
        key = (account, currency)
        account_weight[key] = account_weight.get(key, 0.0) + v

    print("\n=== 按入金账户汇总 ===")
    if args.total is None:
        for (account, currency), w in sorted(account_weight.items(), key=lambda x: -x[1]):
            print(f"  {account:16s} {w * 100:6.2f}%  ({currency})")
        print("\n(未指定 --total, 只显示比例; 加 --total <人民币金额> 可算出具体入金金额)")
    else:
        fx = pd.read_parquet(PROJECT_ROOT / "data" / "processed" / "fx_rates.parquet")
        latest_fx_date = fx["date"].max()
        rate_to_cny = fx[fx["date"] == latest_fx_date].set_index("currency")["rate_to_cny"].to_dict()

        total_cny = args.total
        for (account, currency), w in sorted(account_weight.items(), key=lambda x: -x[1]):
            amount_cny = total_cny * w
            rate = rate_to_cny.get(currency, 1.0)
            amount_native = amount_cny / rate
            print(f"  {account:16s} ¥{amount_cny:>12,.0f}  ≈ {amount_native:>12,.2f} {currency}  ({w*100:5.2f}%)")
        print(f"\n汇率基准: {latest_fx_date.date()}  ({', '.join(f'{c}={r:.4f}' for c, r in rate_to_cny.items() if c != 'CNY')})")


if __name__ == "__main__":
    main()
