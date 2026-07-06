#!/usr/bin/env python3
"""
月度运营工具 — 只买不卖 (被动配置 + 主动策略两层)。

输入: 当前真实持仓 (config/holdings.yaml, 含被动腿和主动策略) + 本月新钱
输出: 本月每个账户该买什么/给哪个策略拨多少钱 (本币金额)

目标权重 = 被动腿目标 × 被动占比 + 主动策略目标 (satellite.yaml 的 Carver 链条算出),
与 satellite_allocation.py 同一套拆分, 两个脚本输出永远一致。

规则 (Smart Portfolios 风格, 与回测执行层同一套带宽逻辑):
  1. 欠配且漂出不交易区下沿的腿, 新钱优先填补到带边缘
  2. 填完还有剩钱, 按目标权重分配给全部腿
  3. 超配的腿只报告不卖出 — 用后续新钱稀释, 不产生卖出交易/税费
  4. 带内的腿不动
  主动策略同样只买不卖: 预算上调时拨新钱进去, 预算下调只报告不撤资。

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
from satellite_allocation import (  # noqa: E402
    ACCOUNT_CURRENCY,
    PHYSICAL_ACCOUNT,
    active_strategy_rows,
    compute_active_split,
)

# 各被动腿计价货币 (与 holdings.yaml 填写口径一致)
LEG_CURRENCY = {leg: cur for leg, (_, cur) in ASSET_ACCOUNT.items()}


def load_holdings_cny(path: Path, fx: dict, active_currency: dict) -> dict:
    """读持仓文件 (被动腿 + 主动策略), 本币金额折成 CNY, 合并成一个 dict。"""
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    result = {}
    for leg, amount in (raw.get("holdings") or {}).items():
        if leg not in ASSET_ACCOUNT:
            print(f"⚠️ 持仓文件里的 {leg} 不在配置资产中, 忽略")
            continue
        result[leg] = float(amount or 0) * fx.get(LEG_CURRENCY[leg], 1.0)
    for name, amount in (raw.get("active_holdings") or {}).items():
        if name not in active_currency:
            print(f"⚠️ 持仓文件里的主动策略 {name} 不在 satellite.yaml 中, 忽略")
            continue
        result[name] = float(amount or 0) * fx.get(active_currency[name], 1.0)
    return result


def build_unified_targets(snap, sat_cfg, params):
    """被动腿 + 主动策略 合并成统一的腿表。

    返回 {key: (类型标签, 实体账户, 币种, 全局目标权重)}。
    被动腿权重乘上被动占比, 主动策略按 Carver 链条展开 — 两层加总为 1。
    """
    w_active, active_combined, account_level, _, _ = compute_active_split(
        sat_cfg, params.target_vol
    )
    w_passive = 1.0 - w_active

    legs = {}
    for leg, w in snap.targets.items():
        src_account, currency = ASSET_ACCOUNT[leg]
        physical = PHYSICAL_ACCOUNT.get(src_account, src_account)
        legs[leg] = ("被动", physical, currency, w * w_passive)
    for key, physical, currency, w in active_strategy_rows(w_active, active_combined, account_level):
        if key in legs:
            print(f"⚠️ 主动策略名 {key} 与被动腿重名, 忽略该策略")
            continue
        legs[key] = ("量化", physical, currency, w)
    return legs, w_passive, w_active


def main():
    parser = argparse.ArgumentParser(description="月度运营: 持仓 + 新钱 → 只买不卖操作清单")
    parser.add_argument("--holdings", default="config/holdings.yaml", help="当前持仓文件")
    parser.add_argument("--new-money", type=float, required=True, help="本月注入的新钱 (人民币)")
    parser.add_argument("--refresh", action="store_true", help="先抓取最新市场数据")
    parser.add_argument("--params", default="config/params.yaml")
    parser.add_argument("--config", default="config/backtest.yaml")
    parser.add_argument("--satellite-config", default="config/satellite.yaml", help="主动策略配置文件")
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

    with open(args.satellite_config) as f:
        sat_cfg = yaml.safe_load(f)
    # 主动策略的带宽: 只管资金拨付节奏, 不必像被动腿那样成本驱动, 默认给得宽
    active_band = float(sat_cfg.get("active_band", 0.05))

    snap = get_passive_snapshot(args.params, args.config)
    legs, w_passive, w_active = build_unified_targets(snap, sat_cfg, params)

    fx, fx_date = latest_fx_rates()
    active_currency = {k: cur for k, (kind, _, cur, _) in legs.items() if kind == "量化"}
    holdings_cny = load_holdings_cny(holdings_path, fx, active_currency)

    current_total = sum(holdings_cny.values())
    T = current_total + args.new_money

    print(f"\n=== 月度操作 (基于 {snap.asof} 数据) ===")
    print(f"  当前持仓 ¥{current_total:,.0f} + 新钱 ¥{args.new_money:,.0f} = ¥{T:,.0f}")
    print(f"  被动:主动 = {w_passive*100:.0f} : {w_active*100:.0f}\n")

    # ── 第一步: 找出漂出带的腿 ──
    shortfalls = {}   # 欠配: 填到带下沿的缺口
    overweights = {}  # 超配: 只报告
    in_band = []
    for key, (kind, _, _, tgt_w) in legs.items():
        cur_w = holdings_cny.get(key, 0.0) / T if T > 0 else 0.0
        band = active_band if kind == "量化" else _compute_band(key, bt_cfg, params)
        if cur_w < tgt_w - band:
            shortfalls[key] = (tgt_w - band - cur_w) * T
        elif cur_w > tgt_w + band:
            overweights[key] = (cur_w - tgt_w) * 100  # 超配 pp
        else:
            in_band.append(key)

    # ── 第二步: 新钱填补欠配 (到带边缘) ──
    buys = {}
    remaining = args.new_money
    total_shortfall = sum(shortfalls.values())
    if total_shortfall > 0 and remaining > 0:
        fill_budget = min(remaining, total_shortfall)
        for key, gap in shortfalls.items():
            alloc = fill_budget * (gap / total_shortfall)
            buys[key] = buys.get(key, 0.0) + alloc
        remaining -= fill_budget

    # ── 第三步: 剩余新钱按目标权重分配 (跳过超配腿) ──
    if remaining > 1.0:
        investable = {k: info[3] for k, info in legs.items()
                      if k not in overweights and info[3] > 0}
        total_w = sum(investable.values())
        if total_w > 0:
            for key, w in investable.items():
                buys[key] = buys.get(key, 0.0) + remaining * (w / total_w)

    # ── 输出: 按实体账户分组的买入清单 ──
    by_account = {}
    for key, amount_cny in buys.items():
        if amount_cny < 1.0:
            continue
        kind, physical, currency, _ = legs[key]
        by_account.setdefault(physical, []).append((kind, key, amount_cny, currency))

    print("── 本月买入清单 (只买不卖) ──\n")
    if not by_account:
        print("  无需操作: 所有腿都在不交易区内, 新钱为 0 或不足 1 元\n")
    for physical, items in sorted(by_account.items(), key=lambda x: -sum(a for _, _, a, _ in x[1])):
        account_cny = sum(a for _, _, a, _ in items)
        account_currency = ACCOUNT_CURRENCY.get(physical, "CNY")
        rate = fx.get(account_currency, 1.0)
        if account_currency == "CNY":
            print(f"{physical}  共 ¥{account_cny:,.0f}")
        else:
            print(f"{physical}  共 ¥{account_cny:,.0f}  ≈ {account_cny/rate:,.0f} {account_currency}")
        for i, (kind, key, amount_cny, cur) in enumerate(sorted(items, key=lambda x: -x[2])):
            branch = "└─" if i == len(items) - 1 else "├─"
            verb = "拨给" if kind == "量化" else "买入"
            label = f"[{kind}] {key}"
            r = fx.get(cur, 1.0)
            if cur == "CNY":
                print(f"  {branch} {verb} {label:20s} ¥{amount_cny:,.0f}")
            else:
                print(f"  {branch} {verb} {label:20s} {amount_cny/r:,.0f} {cur}  (≈¥{amount_cny:,.0f})")
        print()

    # ── 超配/带内报告 ──
    if overweights:
        print("── 超配提示 (按只买不卖原则不动, 由后续新钱稀释) ──")
        for key, pp in sorted(overweights.items(), key=lambda x: -x[1]):
            print(f"  [{legs[key][0]}] {key}: 超出目标 {pp:.1f}pp")
        print()
    if in_band:
        print(f"带内 (无需再平衡, 新钱按目标比例正常分配): {', '.join(in_band)}")
    print(f"\n汇率基准: {fx_date.date()}  "
          f"({', '.join(f'{c}={r:.4f}' for c, r in fx.items() if c != 'CNY')})")


if __name__ == "__main__":
    main()
