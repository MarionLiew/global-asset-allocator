#!/usr/bin/env python3
"""
月度运营工具 — 只买不卖。

输入: 当前真实持仓 (config/holdings.yaml) + 本月新钱
输出: 本月每个账户该买什么、买多少 (本币金额)

规则 (Smart Portfolios 风格, 与回测执行层同一套带宽逻辑):
  1. 欠配且漂出不交易区下沿的腿, 新钱优先填补到带边缘
  2. 填完还有剩钱, 按目标权重分配给全部腿
  3. 超配的腿只报告不卖出 — 用后续新钱稀释, 不产生卖出交易/税费
  4. 带内的腿不动

用法:
    cp config/holdings.example.yaml config/holdings.yaml   # 首次: 建持仓文件并填写
    python scripts/monthly_ops.py --new-money 10000        # 本月注入1万, 输出操作清单
    python scripts/monthly_ops.py --new-money 10000 --refresh   # 先刷新市场数据
"""

import argparse
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from current_allocation import (  # noqa: E402
    ASSET_ACCOUNT,
    get_passive_snapshot,
    latest_fx_rates,
    refresh_data,
)

# 各腿计价货币 (与 holdings.yaml 填写口径一致)
LEG_CURRENCY = {leg: cur for leg, (_, cur) in ASSET_ACCOUNT.items()}


def load_holdings_cny(path: Path, fx: dict) -> dict:
    """读持仓文件, 本币金额折成 CNY。"""
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    holdings = raw.get("holdings") or {}

    result = {}
    for leg, amount in holdings.items():
        if leg not in ASSET_ACCOUNT:
            print(f"⚠️ 持仓文件里的 {leg} 不在配置资产中, 忽略")
            continue
        currency = LEG_CURRENCY[leg]
        rate = fx.get(currency, 1.0)
        result[leg] = float(amount or 0) * rate
    return result


def main():
    parser = argparse.ArgumentParser(description="月度运营: 持仓 + 新钱 → 只买不卖操作清单")
    parser.add_argument("--holdings", default="config/holdings.yaml", help="当前持仓文件")
    parser.add_argument("--new-money", type=float, required=True, help="本月注入的新钱 (人民币)")
    parser.add_argument("--refresh", action="store_true", help="先抓取最新市场数据")
    parser.add_argument("--params", default="config/params.yaml")
    parser.add_argument("--config", default="config/backtest.yaml")
    args = parser.parse_args()

    holdings_path = Path(args.holdings)
    if not holdings_path.is_absolute():
        holdings_path = PROJECT_ROOT / holdings_path
    if not holdings_path.exists():
        print(f"持仓文件不存在: {holdings_path}")
        print("首次使用: cp config/holdings.example.yaml config/holdings.yaml 然后填写实际持仓")
        sys.exit(1)

    if args.refresh:
        refresh_data()

    from backtest.config import Params, BacktestConfig
    from backtest.engine.execution import _compute_band

    params = Params.load(args.params)
    bt_cfg = BacktestConfig.load(args.config)

    fx, fx_date = latest_fx_rates()
    holdings_cny = load_holdings_cny(holdings_path, fx)

    snap = get_passive_snapshot(args.params, args.config)
    targets = snap.targets  # {leg: 目标现金权重}

    current_total = sum(holdings_cny.values())
    T = current_total + args.new_money

    print(f"\n=== 月度操作 (基于 {snap.asof} 数据) ===")
    print(f"  当前持仓 ¥{current_total:,.0f} + 新钱 ¥{args.new_money:,.0f} = ¥{T:,.0f}\n")

    # ── 第一步: 找出漂出带的腿 ──
    shortfalls = {}   # 欠配: 填到带下沿的缺口
    overweights = {}  # 超配: 只报告
    in_band = []
    for leg, tgt_w in targets.items():
        cur_w = holdings_cny.get(leg, 0.0) / T if T > 0 else 0.0
        band = _compute_band(leg, bt_cfg, params)
        if cur_w < tgt_w - band:
            shortfalls[leg] = (tgt_w - band - cur_w) * T
        elif cur_w > tgt_w + band:
            overweights[leg] = (cur_w - tgt_w) * 100  # 超配 pp
        else:
            in_band.append(leg)

    # ── 第二步: 新钱填补欠配 (到带边缘) ──
    buys = {}
    remaining = args.new_money
    total_shortfall = sum(shortfalls.values())
    if total_shortfall > 0 and remaining > 0:
        fill_budget = min(remaining, total_shortfall)
        for leg, gap in shortfalls.items():
            alloc = fill_budget * (gap / total_shortfall)
            buys[leg] = buys.get(leg, 0.0) + alloc
        remaining -= fill_budget

    # ── 第三步: 剩余新钱按目标权重分配 (跳过超配腿) ──
    if remaining > 1.0:
        investable = {l: w for l, w in targets.items() if l not in overweights and w > 0}
        total_w = sum(investable.values())
        if total_w > 0:
            for leg, w in investable.items():
                buys[leg] = buys.get(leg, 0.0) + remaining * (w / total_w)

    # ── 输出: 按账户分组的买入清单 ──
    by_account = {}
    for leg, amount_cny in buys.items():
        if amount_cny < 1.0:
            continue
        account, currency = ASSET_ACCOUNT[leg]
        by_account.setdefault(account, []).append((leg, amount_cny, currency))

    print("── 本月买入清单 (只买不卖) ──\n")
    if not by_account:
        print("  无需操作: 所有腿都在不交易区内, 新钱为 0 或不足 1 元\n")
    for account, legs in sorted(by_account.items(), key=lambda x: -sum(a for _, a, _ in x[1])):
        account_cny = sum(a for _, a, _ in legs)
        currency = legs[0][2]
        rate = fx.get(currency, 1.0)
        if currency == "CNY":
            print(f"{account}  共 ¥{account_cny:,.0f}")
        else:
            print(f"{account}  共 ¥{account_cny:,.0f}  ≈ {account_cny/rate:,.0f} {currency}")
        for i, (leg, amount_cny, cur) in enumerate(sorted(legs, key=lambda x: -x[1])):
            branch = "└─" if i == len(legs) - 1 else "├─"
            r = fx.get(cur, 1.0)
            if cur == "CNY":
                print(f"  {branch} 买入 {leg:12s} ¥{amount_cny:,.0f}")
            else:
                print(f"  {branch} 买入 {leg:12s} {amount_cny/r:,.0f} {cur}  (≈¥{amount_cny:,.0f})")
        print()

    # ── 超配/带内报告 ──
    if overweights:
        print("── 超配提示 (按只买不卖原则不动, 由后续新钱稀释) ──")
        for leg, pp in sorted(overweights.items(), key=lambda x: -x[1]):
            print(f"  {leg}: 超出目标 {pp:.1f}pp")
        print()
    if in_band:
        print(f"带内 (无需再平衡, 新钱按目标比例正常分配): {', '.join(in_band)}")
    print(f"\n汇率基准: {fx_date.date()}  "
          f"({', '.join(f'{c}={r:.4f}' for c, r in fx.items() if c != 'CNY')})")


if __name__ == "__main__":
    main()
