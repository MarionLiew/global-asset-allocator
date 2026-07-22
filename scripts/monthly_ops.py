#!/usr/bin/env python3
"""
月度运营工具 — 动态不交易区再平衡 (被动配置 + 主动策略两层)。

输入: 当前真实持仓 (config/holdings.yaml, 含被动腿和主动策略) + 本月新钱
输出: 本月每个账户该买卖什么/给哪个策略拨入或撤回多少钱 (本币金额)

目标权重 = 被动腿目标 × 被动占比 + 主动策略目标 (satellite.yaml 的 Carver 链条算出),
与 satellite_allocation.py 同一套拆分, 两个脚本输出永远一致。

规则 (Smart Portfolios 风格, 与回测执行层同一套带宽逻辑):
  1. 动态模式为默认: 超配卖回上沿, 所得资金和新钱用于补足欠配腿
  2. 欠配先买到下沿, 剩余资金继续向精确目标靠拢
  3. 带内且无需吸收现金的腿不交易；目标为零时不保留缓冲仓位
  4. 可选 buy-only 积累模式: 不卖出, 只用新钱稀释超配

用法:
    cp config/holdings.example.yaml config/holdings.yaml   # 首次: 建持仓文件并填写
    python scripts/monthly_ops.py --new-money 10000        # 本月注入1万, 输出操作清单
    python scripts/monthly_ops.py --new-money 10000 --mode buy-only
    python scripts/monthly_ops.py --new-money 10000 --refresh   # 先刷新市场数据
"""

import argparse
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from current_allocation import (  # noqa: E402
    ASSET_ACCOUNT,
    get_passive_result,
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
        value = float(amount or 0)
        if value < 0:
            raise ValueError(f"持仓金额不能为负数: {leg}={value}")
        result[leg] = value * fx.get(LEG_CURRENCY[leg], 1.0)
    for name, amount in (raw.get("active_holdings") or {}).items():
        if name not in active_currency:
            print(f"⚠️ 持仓文件里的主动策略 {name} 不在 satellite.yaml 中, 忽略")
            continue
        value = float(amount or 0)
        if value < 0:
            raise ValueError(f"主动策略持仓金额不能为负数: {name}={value}")
        result[name] = value * fx.get(active_currency[name], 1.0)
    return result


def build_unified_targets(snap, sat_cfg, params, passive_returns=None, passive_vol=None):
    """被动腿 + 主动策略 合并成统一的腿表。

    返回 {key: (类型标签, 实体账户, 币种, 全局目标权重)}。
    被动腿权重乘上被动占比, 主动策略按 Carver 链条展开 — 两层加总为 1。
    """
    w_active, active_combined, account_level, _, _ = compute_active_split(
        sat_cfg,
        passive_vol if passive_vol is not None else params.target_vol,
        passive_returns=passive_returns,
        base_dir=PROJECT_ROOT,
        asof=snap.asof,
    )
    w_passive = 1.0 - w_active

    legs = {}
    for leg, w in snap.targets.items():
        src_account, currency = ASSET_ACCOUNT[leg]
        physical = PHYSICAL_ACCOUNT.get(src_account, src_account)
        legs[leg] = ("被动", physical, currency, w * w_passive)
    # 即使主动目标为零也保留策略腿，确保已有主动持仓仍计入总资产并被报告为超配。
    for key, physical, currency, w in active_strategy_rows(
        w_active, active_combined, account_level, include_zero=True
    ):
        if key in legs:
            print(f"⚠️ 主动策略名 {key} 与被动腿重名, 忽略该策略")
            continue
        legs[key] = ("量化", physical, currency, w)
    return legs, w_passive, w_active


def plan_monthly_trades(legs, holdings_cny, new_money, bands, allow_sells=True):
    """按目标权重和不交易区生成买卖计划，金额口径均为 CNY。

    动态模式先把越过上沿的持仓卖回上沿，再用卖出款与新钱补足下沿，
    最后向精确目标靠拢。目标为零时带宽强制为零，避免停用策略残留仓位。
    """
    current_total = sum(holdings_cny.values())
    total = current_total + new_money
    if total <= 0:
        return {"buys": {}, "sells": {}, "overweights": {}, "in_band": list(legs), "total": total}

    working = {key: float(holdings_cny.get(key, 0.0)) for key in legs}
    buys = {}
    sells = {}
    overweights = {}
    in_band = []

    # 先处理上沿。卖出释放的资金留在组合内，随后用于补足其他腿。
    for key, (_, _, _, target) in legs.items():
        band = 0.0 if target <= 0 else float(bands[key])
        current_weight = working[key] / total
        upper = min(1.0, target + band)
        if current_weight > upper + 1e-12:
            excess = (current_weight - upper) * total
            if allow_sells:
                sells[key] = excess
                working[key] -= excess
            else:
                overweights[key] = (current_weight - target) * 100

    available = new_money + sum(sells.values())

    # 第一次只补到下沿，体现 buffer 的低换手原则。
    lower_gaps = {}
    for key, (_, _, _, target) in legs.items():
        band = 0.0 if target <= 0 else float(bands[key])
        lower = max(0.0, target - band)
        lower_gaps[key] = max(0.0, lower * total - working[key])
    lower_total = sum(lower_gaps.values())
    if lower_total > 0 and available > 0:
        budget = min(available, lower_total)
        for key, gap in lower_gaps.items():
            if gap > 0:
                amount = budget * gap / lower_total
                buys[key] = buys.get(key, 0.0) + amount
                working[key] += amount
        available -= budget

    # 尚有现金时，只向精确目标的缺口分配，不给已高于目标的腿加仓。
    exact_gaps = {
        key: max(0.0, info[3] * total - working[key])
        for key, info in legs.items()
    }
    exact_total = sum(exact_gaps.values())
    if exact_total > 0 and available > 0:
        budget = min(available, exact_total)
        for key, gap in exact_gaps.items():
            if gap > 0:
                amount = budget * gap / exact_total
                buys[key] = buys.get(key, 0.0) + amount
                working[key] += amount
        available -= budget

    for key, (_, _, _, target) in legs.items():
        band = 0.0 if target <= 0 else float(bands[key])
        current_weight = holdings_cny.get(key, 0.0) / total
        if target - band - 1e-12 <= current_weight <= target + band + 1e-12:
            in_band.append(key)

    return {
        "buys": buys,
        "sells": sells,
        "overweights": overweights,
        "in_band": in_band,
        "unallocated_cash": max(0.0, available),
        "total": total,
    }


def main():
    parser = argparse.ArgumentParser(description="月度运营: 持仓 + 新钱 → 动态再平衡操作清单")
    parser.add_argument("--holdings", default="config/holdings.yaml", help="当前持仓文件")
    parser.add_argument("--new-money", type=float, required=True, help="本月注入的新钱 (人民币)")
    parser.add_argument("--refresh", action="store_true", help="先抓取最新市场数据")
    parser.add_argument("--params", default="config/params.yaml")
    parser.add_argument("--config", default="config/backtest.yaml")
    parser.add_argument("--satellite-config", default="config/satellite.yaml", help="主动策略配置文件")
    parser.add_argument(
        "--mode", choices=("dynamic", "buy-only"), default="dynamic",
        help="dynamic=越界买卖回缓冲区边缘（默认）；buy-only=积累期只用新钱",
    )
    args = parser.parse_args()
    if args.new_money < 0:
        parser.error("--new-money 不能为负数")

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

    passive_result = get_passive_result(args.params, args.config)
    snap = passive_result.weight_history[-1]
    passive_returns = passive_result.strategy_nav.pct_change().dropna()
    span = int(sat_cfg.get("top_level_risk", {}).get("passive_vol_ewma_span_months", 12))
    passive_vol = float(passive_returns.ewm(span=span, min_periods=12).std().iloc[-1] * (12 ** 0.5))
    legs, w_passive, w_active = build_unified_targets(
        snap, sat_cfg, params, passive_returns=passive_returns, passive_vol=passive_vol
    )

    fx, fx_date = latest_fx_rates()
    active_currency = {k: cur for k, (kind, _, cur, _) in legs.items() if kind == "量化"}
    holdings_cny = load_holdings_cny(holdings_path, fx, active_currency)

    current_total = sum(holdings_cny.values())
    bands = {
        key: (active_band if info[0] == "量化" else _compute_band(key, bt_cfg, params))
        for key, info in legs.items()
    }
    plan = plan_monthly_trades(
        legs, holdings_cny, args.new_money, bands, allow_sells=args.mode == "dynamic"
    )
    T = plan["total"]

    print(f"\n=== 月度操作 (基于 {snap.asof} 数据) ===")
    print(f"  当前持仓 ¥{current_total:,.0f} + 新钱 ¥{args.new_money:,.0f} = ¥{T:,.0f}")
    print(f"  被动:主动 = {w_passive*100:.0f} : {w_active*100:.0f}\n")
    print(f"  执行模式 = {'动态缓冲再平衡' if args.mode == 'dynamic' else '积累期只买不卖'}\n")

    buys = plan["buys"]
    sells = plan["sells"]
    overweights = plan["overweights"]
    in_band = plan["in_band"]

    # ── 输出: 按实体账户分组的买入清单 ──
    by_account = {}
    for key, amount_cny in buys.items():
        if amount_cny < 1.0:
            continue
        kind, physical, currency, _ = legs[key]
        by_account.setdefault(physical, []).append((kind, key, amount_cny, currency))

    sell_by_account = {}
    for key, amount_cny in sells.items():
        if amount_cny < 1.0:
            continue
        kind, physical, currency, _ = legs[key]
        sell_by_account.setdefault(physical, []).append((kind, key, amount_cny, currency))

    if sell_by_account:
        print("── 本月卖出/撤回清单（卖回不交易区上沿）──\n")
        for physical, items in sorted(sell_by_account.items(), key=lambda x: -sum(a for _, _, a, _ in x[1])):
            print(f"{physical}  共调出 ¥{sum(a for _, _, a, _ in items):,.0f}")
            for kind, key, amount_cny, currency in sorted(items, key=lambda x: -x[2]):
                amount_native = amount_cny / fx.get(currency, 1.0)
                verb = "撤回" if kind == "量化" else "卖出"
                suffix = f"{amount_native:,.0f} {currency}" if currency != "CNY" else f"¥{amount_cny:,.0f}"
                print(f"  └─ {verb} [{kind}] {key}: {suffix}  (≈¥{amount_cny:,.0f})")
            print()

    print("── 本月买入/拨入清单 ──\n")
    if not by_account:
        print("  无需买入: 所有腿都在不交易区内, 新钱为 0 或不足 1 元\n")
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
        print("── 超配提示（buy-only 模式不卖，由后续新钱稀释）──")
        for key, pp in sorted(overweights.items(), key=lambda x: -x[1]):
            print(f"  [{legs[key][0]}] {key}: 超出目标 {pp:.1f}pp")
        print()
    if in_band:
        print(f"期初位于带内: {', '.join(in_band)}")
    if plan["unallocated_cash"] >= 1.0:
        print(f"未分配现金: ¥{plan['unallocated_cash']:,.0f}（请检查目标权重或持仓映射）")
    print(f"\n汇率基准: {fx_date.date()}  "
          f"({', '.join(f'{c}={r:.4f}' for c, r in fx.items() if c != 'CNY')})")


if __name__ == "__main__":
    main()
