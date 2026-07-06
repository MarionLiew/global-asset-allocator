#!/usr/bin/env python3
"""
输出基于最新数据的目标配置权重。

用法:
    python scripts/current_allocation.py                  # 只看比例
    python scripts/current_allocation.py --refresh         # 先抓取最新数据再计算
    python scripts/current_allocation.py --total 500000    # 按人民币总金额算出每个账户该入多少钱
"""

import argparse
import dataclasses
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


def refresh_data():
    subprocess.check_call([sys.executable, str(PROJECT_ROOT / "scripts" / "fetch_data.py")])


def get_passive_snapshot(params_path="config/params.yaml", config_path="config/backtest.yaml"):
    """跑一遍被动配置模型, 返回最新一期的目标权重快照 (WeightSnapshot)。"""
    from backtest.config import Params, BacktestConfig
    from backtest.data.csv_provider import CSVProvider
    from backtest.engine.backtest_loop import run_backtest
    import pandas as pd

    params = Params.load(params_path)
    bt_cfg = BacktestConfig.load(config_path)

    # 每个资产都有数据的最新月份 = 全部 legs 最大日期的交集
    etf = pd.read_parquet(PROJECT_ROOT / "data" / "processed" / "etf_returns.parquet")
    latest_common = etf.groupby("asset_id")["date"].max().min()
    bt_cfg = dataclasses.replace(bt_cfg, end_date=latest_common.strftime("%Y-%m-%d"))

    md = CSVProvider(str(PROJECT_ROOT), params)
    result = run_backtest(params, bt_cfg, md)
    return result.weight_history[-1]


def build_account_tree(targets: dict, asset_account: dict = ASSET_ACCOUNT) -> dict:
    """把 {leg: weight} 按入金账户分组 -> {account: {currency, legs: {leg: weight}}}。"""
    accounts = {}
    for k, v in targets.items():
        account, currency = asset_account.get(k, ("未映射", "CNY"))
        bucket = accounts.setdefault(account, {"currency": currency, "legs": {}})
        bucket["legs"][k] = bucket["legs"].get(k, 0.0) + v
    return accounts


def latest_fx_rates():
    """返回 {currency: rate_to_cny} 及对应日期。"""
    import pandas as pd
    fx = pd.read_parquet(PROJECT_ROOT / "data" / "processed" / "fx_rates.parquet")
    latest_fx_date = fx["date"].max()
    rate_to_cny = fx[fx["date"] == latest_fx_date].set_index("currency")["rate_to_cny"].to_dict()
    return rate_to_cny, latest_fx_date


def print_account_tree(accounts: dict, scale: float = 1.0, total_cny: float = None,
                        fx_rate_to_cny: dict = None, indent: str = ""):
    """打印账户树。scale: 这组账户在总资产里的占比 (被动配置整体权重, 主动策略下则为1)。"""
    account_order = sorted(accounts.items(), key=lambda x: -sum(x[1]["legs"].values()))
    for account, info in account_order:
        currency = info["currency"]
        account_w = sum(info["legs"].values()) * scale
        if total_cny is None:
            print(f"{indent}{account}  {account_w * 100:.2f}%  ({currency})")
        else:
            amount_cny = total_cny * account_w
            rate = (fx_rate_to_cny or {}).get(currency, 1.0)
            amount_native = amount_cny / rate
            print(f"{indent}{account}  {account_w * 100:.2f}%  →  ¥{amount_cny:,.0f}  ≈ {amount_native:,.2f} {currency}")

        legs_sorted = sorted(info["legs"].items(), key=lambda x: -x[1])
        for i, (leg, w) in enumerate(legs_sorted):
            branch = "└─" if i == len(legs_sorted) - 1 else "├─"
            leg_w_scaled = w * scale
            pct_of_account = w / sum(info["legs"].values()) * 100 if info["legs"] else 0
            if total_cny is None:
                print(f"{indent}  {branch} {leg:12s} {leg_w_scaled * 100:6.2f}%  (占该账户 {pct_of_account:5.1f}%)")
            else:
                leg_amount_cny = total_cny * leg_w_scaled
                leg_amount_native = leg_amount_cny / (fx_rate_to_cny or {}).get(currency, 1.0)
                print(f"{indent}  {branch} {leg:12s} {leg_w_scaled * 100:6.2f}%  (占该账户 {pct_of_account:5.1f}%)  "
                      f"≈ {leg_amount_native:,.2f} {currency}")
        print()


def main():
    parser = argparse.ArgumentParser(description="计算最新目标配置权重")
    parser.add_argument("--refresh", action="store_true", help="先抓取最新市场数据")
    parser.add_argument("--params", default="config/params.yaml")
    parser.add_argument("--config", default="config/backtest.yaml")
    parser.add_argument("--total", type=float, default=None, help="本次入金总金额 (人民币)。不指定则只输出比例")
    args = parser.parse_args()

    if args.refresh:
        refresh_data()

    snap = get_passive_snapshot(args.params, args.config)
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

    accounts = build_account_tree(snap.targets)

    fx_rate_to_cny, latest_fx_date = (None, None)
    if args.total is not None:
        fx_rate_to_cny, latest_fx_date = latest_fx_rates()

    print("\n=== 按入金账户汇总 (树形) ===\n")
    print_account_tree(accounts, total_cny=args.total, fx_rate_to_cny=fx_rate_to_cny)

    if args.total is None:
        print("(未指定 --total, 只显示比例; 加 --total <人民币金额> 可算出具体入金金额)")
    else:
        print(f"汇率基准: {latest_fx_date.date()}  "
              f"({', '.join(f'{c}={r:.4f}' for c, r in fx_rate_to_cny.items() if c != 'CNY')})")


if __name__ == "__main__":
    main()
