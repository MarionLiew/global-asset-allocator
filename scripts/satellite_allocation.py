#!/usr/bin/env python3
"""
被动配置 + 主动策略 两层风险预算计算 (Carver 式决策链条)。

策略/账户配置全部放在 config/satellite.yaml, 命令行只用来管总金额/是否刷新数据。
每个策略优先从 stats_path 指向的 json 读取 {annual_vol, sharpe, track_months}
(接你别的项目跑出来的回测/实盘结果), 缺失时回退用 yaml 里手填的 vol/sr/months。

决策链条:
    1. 每个策略的夏普按实盘月数向"零 edge 先验"收缩 (数据越少越接近假设没有 edge)
    2. 同账户内多个策略、以及账户之间, 都按波动率倒数做等风险权重组合
       (Carver: 缺乏压倒性证据前默认等风险, 不按夏普差异分高低)
    3. 主动策略整体的风险预算 = risk-floor 起步, 随组合后的收缩夏普线性爬升到 risk-cap-ceiling
    4. 双资产风险贡献方程, 结合被动/主动波动率和相关性, 反推被动:主动的资金权重
    5. 被动部分展开现有配置树, 主动部分展开到 账户 -> 策略 两层, 一起打印成树形结果

用法:
    python scripts/satellite_allocation.py                 # 用 config/satellite.yaml 里的参数
    python scripts/satellite_allocation.py --total 500000   # 换算成具体金额
    python scripts/satellite_allocation.py --satellite-config config/satellite.yaml
"""

import argparse
import json
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


def shrink_sharpe(observed_sr: float, track_months: int, confidence_months: int):
    """把观测到的夏普比率, 按数据量向 0 (零 edge 先验) 收缩。返回 (收缩后夏普, 信心度)。"""
    confidence = min(1.0, track_months / confidence_months) if confidence_months > 0 else 1.0
    return confidence * observed_sr, confidence


def resolve_strategy_stats(strategy_cfg: dict) -> dict:
    """优先读 stats_path 指向的外部 json ({annual_vol, sharpe, track_months}), 缺失则用 yaml 手填值。"""
    stats_path = strategy_cfg.get("stats_path")
    vol, sr, months = strategy_cfg.get("vol"), strategy_cfg.get("sr", 0.0), strategy_cfg.get("months", 0)

    if stats_path:
        p = Path(stats_path).expanduser()
        if p.exists():
            with open(p) as f:
                external = json.load(f)
            vol = external.get("annual_vol", vol)
            sr = external.get("sharpe", sr)
            months = external.get("track_months", months)

    if vol is None:
        raise ValueError(f"策略 {strategy_cfg.get('name', '?')} 缺少波动率 (vol 或 stats_path.annual_vol)")

    return {"vol": vol, "sr": sr, "months": months}


def combine_pool(items: dict, intra_corr: float) -> dict:
    """
    items: {name: {"vol": .., "sr": .. (已经是要用于组合的有效夏普)}}
    组内按波动率倒数做等风险权重, 假设两两相关性都是 intra_corr。
    返回 {"vol": 组合波动率, "sr": 组合有效夏普, "weights": {name: 组内权重}}
    """
    names = list(items.keys())
    if len(names) == 1:
        n = names[0]
        return {"vol": items[n]["vol"], "sr": items[n]["sr"], "weights": {n: 1.0}}

    inv_vol = {n: 1.0 / items[n]["vol"] for n in names}
    total_inv_vol = sum(inv_vol.values())
    weights = {n: inv_vol[n] / total_inv_vol for n in names}

    var = 0.0
    for a in names:
        for b in names:
            rho = 1.0 if a == b else intra_corr
            var += weights[a] * weights[b] * items[a]["vol"] * items[b]["vol"] * rho
    combined_vol = var ** 0.5

    expected_return = sum(weights[n] * items[n]["sr"] * items[n]["vol"] for n in names)
    combined_sr = expected_return / combined_vol if combined_vol > 0 else 0.0

    return {"vol": combined_vol, "sr": combined_sr, "weights": weights}


def build_active_sleeve(cfg: dict):
    """
    解析 config/satellite.yaml 的 accounts 结构:
      account -> 策略们(先做叶子收缩+组合) -> 账户级 vol/sr
      账户们 -> 组合 -> 整体主动策略 vol/sr
    返回 (active_combined, account_level, per_strategy_detail)
    """
    confidence_months = cfg["confidence_months"]
    intra_corr = cfg["intra_active_corr"]

    account_level = {}
    detail = {}

    for account_name, account_cfg in cfg["accounts"].items():
        leaf_items = {}
        detail[account_name] = {}
        for strat_cfg in account_cfg["strategies"]:
            raw = resolve_strategy_stats(strat_cfg)
            sr_shrunk, confidence = shrink_sharpe(raw["sr"], raw["months"], confidence_months)
            leaf_items[strat_cfg["name"]] = {"vol": raw["vol"], "sr": sr_shrunk}
            detail[account_name][strat_cfg["name"]] = {
                "raw": raw, "sr_shrunk": sr_shrunk, "confidence": confidence,
            }

        combined = combine_pool(leaf_items, intra_corr)
        account_level[account_name] = combined

    active_pool = {name: {"vol": info["vol"], "sr": info["sr"]} for name, info in account_level.items()}
    active_combined = combine_pool(active_pool, intra_corr)

    return active_combined, account_level, detail


def risk_contribution_of_active(w_active: float, vol_passive: float, vol_active: float, corr: float) -> float:
    """双资产 (被动, 主动) 组合中, 主动那部分占总风险的百分比 (Euler 风险贡献)。"""
    w_passive = 1.0 - w_active
    cov_pp, cov_aa = vol_passive ** 2, vol_active ** 2
    cov_pa = corr * vol_passive * vol_active

    sum_w_p = w_passive * cov_pp + w_active * cov_pa
    sum_w_a = w_passive * cov_pa + w_active * cov_aa
    port_var = w_passive * sum_w_p + w_active * sum_w_a
    if port_var <= 0:
        return 0.0
    return (w_active * sum_w_a) / port_var


def solve_active_weight(risk_budget_target: float, vol_passive: float, vol_active: float, corr: float) -> float:
    from scipy.optimize import brentq

    def f(w_active):
        return risk_contribution_of_active(w_active, vol_passive, vol_active, corr) - risk_budget_target

    lo, hi = 1e-6, 1 - 1e-6
    if f(lo) > 0:
        return lo
    if f(hi) < 0:
        return hi
    return brentq(f, lo, hi)


# ── 展示层映射 ────────────────────────────────────────────────────────────────

# 被动/主动的账户名 → 实体入金账户 (打款就打给这几个)
PHYSICAL_ACCOUNT = {
    "Schwab": "Schwab",
    "OKX (XAUT现货)": "OKX",
    "同花顺": "同花顺",
    "ZA": "ZA",
    "Schwab量化": "Schwab",
    "OKX量化": "OKX",
    "同花顺手动": "同花顺",
    "ZA手动": "ZA",
}

# 实体账户的打款货币
ACCOUNT_CURRENCY = {"Schwab": "USD", "OKX": "USD", "同花顺": "CNY", "ZA": "HKD"}

# 资产大类 (总览用)
ASSET_CLASS = {
    "US_equity": "股票", "DM_equity": "股票", "CN_equity": "股票", "HK_equity": "股票",
    "CN_GOVT": "债券", "TIPS": "债券", "CORP_BOND": "债券", "EM_BOND": "债券",
    "GOLD": "黄金",
}


def print_decision_chain(detail, account_level, active_combined, cfg,
                          risk_budget_target, passive_vol, w_passive, w_active):
    print("\n=== Carver 式风险预算决策链条 ===\n")
    for account_name, strategies in detail.items():
        print(f"  [{account_name}]")
        for strat_name, info in strategies.items():
            raw = info["raw"]
            print(f"    {strat_name}: vol={raw['vol']:.1%}, 回测夏普={raw['sr']:.2f}, "
                  f"实盘{raw['months']}个月 → 信心={info['confidence']:.0%} → 收缩后夏普={info['sr_shrunk']:.2f}")
        acc = account_level[account_name]
        print(f"    → 账户组合: vol={acc['vol']:.1%}, 有效夏普={acc['sr']:.2f}, "
              f"内部权重={ {k: f'{v:.0%}' for k, v in acc['weights'].items()} }")

    print(f"\n  主动策略整体: vol={active_combined['vol']:.1%}, 有效夏普={active_combined['sr']:.2f}, "
          f"账户间权重={ {k: f'{v:.0%}' for k, v in active_combined['weights'].items()} }")
    print(f"  → 风险预算 = {cfg['risk_floor']:.0%} + min(1, {active_combined['sr']:.2f}/{cfg['full_confidence_sr']:.2f}) × "
          f"({cfg['risk_cap_ceiling']:.0%} - {cfg['risk_floor']:.0%}) = {risk_budget_target:.1%}")
    print(f"  被动波动率={passive_vol:.1%}, 主动波动率={active_combined['vol']:.1%}, "
          f"假设相关性={cfg['active_corr']:.2f}")
    print(f"  → 解得 被动:主动 资金权重 = {w_passive:.1%} : {w_active:.1%}")


def main():
    parser = argparse.ArgumentParser(description="被动配置 + 主动策略 风险预算计算 (Carver 式)")
    parser.add_argument("--refresh", action="store_true", help="先抓取最新市场数据")
    parser.add_argument("--params", default="config/params.yaml")
    parser.add_argument("--config", default="config/backtest.yaml")
    parser.add_argument("--satellite-config", default="config/satellite.yaml", help="主动策略配置文件")
    parser.add_argument("--total", type=float, default=None, help="总资金 (人民币)。不指定则只输出比例")
    parser.add_argument("--passive-vol", type=float, default=None,
                         help="被动配置目标波动率, 默认读 params.yaml 的 target_vol")
    parser.add_argument("--verbose", action="store_true", help="显示 Carver 决策链条推导过程")
    args = parser.parse_args()

    with open(args.satellite_config) as f:
        cfg = yaml.safe_load(f)

    if args.refresh:
        refresh_data()

    snap = get_passive_snapshot(args.params, args.config)

    passive_vol = args.passive_vol
    if passive_vol is None:
        from backtest.config import Params
        passive_vol = Params.load(args.params).target_vol

    active_combined, account_level, detail = build_active_sleeve(cfg)

    full_confidence_sr = cfg["full_confidence_sr"]
    risk_floor, risk_cap_ceiling = cfg["risk_floor"], cfg["risk_cap_ceiling"]
    risk_budget_target = risk_floor + max(0.0, min(1.0, active_combined["sr"] / full_confidence_sr)) * (
        risk_cap_ceiling - risk_floor
    )

    w_active = solve_active_weight(risk_budget_target, passive_vol, active_combined["vol"], cfg["active_corr"])
    w_passive = 1.0 - w_active

    if args.verbose:
        print_decision_chain(detail, account_level, active_combined, cfg,
                              risk_budget_target, passive_vol, w_passive, w_active)

    # ── 汇总所有腿到实体账户: (实体账户, 类型标签, 名称, 全局权重) ──
    rows = []
    for leg, w in snap.targets.items():
        src_account, _ = ASSET_ACCOUNT.get(leg, ("未映射", "CNY"))
        physical = PHYSICAL_ACCOUNT.get(src_account, src_account)
        rows.append((physical, "被动", leg, w * w_passive))
    for account_name, w_in_active in active_combined["weights"].items():
        physical = PHYSICAL_ACCOUNT.get(account_name, account_name)
        for strat_name, w_in_account in account_level[account_name]["weights"].items():
            rows.append((physical, "量化", strat_name, w_in_active * w_in_account * w_active))

    # ── 总览: 资产大类占比 + 股债比 ──
    class_totals: dict[str, float] = {}
    for _, sleeve, name, w in rows:
        cls = "主动策略" if sleeve == "量化" else ASSET_CLASS.get(name, "其他")
        class_totals[cls] = class_totals.get(cls, 0.0) + w

    stock = class_totals.get("股票", 0.0)
    bond = class_totals.get("债券", 0.0)
    sb_total = stock + bond
    print(f"\n=== 总览 (基于 {snap.asof} 数据) ===\n")
    parts = [f"{cls} {w*100:.1f}%" for cls, w in
             sorted(class_totals.items(), key=lambda x: -x[1])]
    print("  " + "  |  ".join(parts))
    if sb_total > 0:
        print(f"  股债比 = {stock/sb_total*100:.0f} : {bond/sb_total*100:.0f}"
              f"  (被动:主动 = {w_passive*100:.0f} : {w_active*100:.0f})")

    # ── 按实体账户: 打款总额 + 内部明细 ──
    fx_rate_to_cny, latest_fx_date = (None, None)
    if args.total is not None:
        fx_rate_to_cny, latest_fx_date = latest_fx_rates()

    by_account: dict[str, list] = {}
    for physical, sleeve, name, w in rows:
        by_account.setdefault(physical, []).append((sleeve, name, w))

    if args.total is not None:
        print(f"\n=== 按账户入金 (总资金 ¥{args.total:,.0f}) ===\n")
    else:
        print("\n=== 按账户占比 ===\n")

    manual_accounts = set(cfg.get("manual_execution") or [])

    for physical, legs in sorted(by_account.items(), key=lambda x: -sum(w for _, _, w in x[1])):
        account_w = sum(w for _, _, w in legs)
        currency = ACCOUNT_CURRENCY.get(physical, "CNY")
        manual_tag = "  〔标的自选: 各角色额度只约束资产类别〕" if physical in manual_accounts else ""
        if args.total is None:
            print(f"{physical}  {account_w*100:.2f}%{manual_tag}")
        else:
            amount_cny = args.total * account_w
            rate = fx_rate_to_cny.get(currency, 1.0)
            if currency == "CNY":
                print(f"{physical}  {account_w*100:.2f}%  →  打款 ¥{amount_cny:,.0f}{manual_tag}")
            else:
                print(f"{physical}  {account_w*100:.2f}%  →  打款 ¥{amount_cny:,.0f}"
                      f"  ≈ {amount_cny/rate:,.0f} {currency}{manual_tag}")

        legs_sorted = sorted(legs, key=lambda x: -x[2])
        for i, (sleeve, name, w) in enumerate(legs_sorted):
            branch = "└─" if i == len(legs_sorted) - 1 else "├─"
            label = f"[{sleeve}] {name}"
            if args.total is None:
                print(f"  {branch} {label:24s} {w*100:6.2f}%")
            else:
                leg_cny = args.total * w
                rate = fx_rate_to_cny.get(currency, 1.0)
                if currency == "CNY":
                    print(f"  {branch} {label:24s} {w*100:6.2f}%  ≈ ¥{leg_cny:,.0f}")
                else:
                    print(f"  {branch} {label:24s} {w*100:6.2f}%  ≈ {leg_cny/rate:,.0f} {currency}")
        print()

    if args.total is None:
        print("(加 --total <人民币金额> 可算出每个账户具体打款金额; --verbose 看推导过程)")
    else:
        print(f"汇率基准: {latest_fx_date.date()}  "
              f"({', '.join(f'{c}={r:.4f}' for c, r in fx_rate_to_cny.items() if c != 'CNY')})")


if __name__ == "__main__":
    main()
