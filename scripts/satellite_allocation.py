#!/usr/bin/env python3
"""
被动配置 + 主动策略 两层风险预算计算 (Carver 式决策链条)。

决策链条:
    1. 每个主动策略的夏普比率按实盘/回测月数向"零 edge 先验"收缩
       (数据越少, 越接近假设没有 edge; 收缩满 --confidence-months 后不再收缩)
    2. 主动策略整体的风险预算 = risk-floor 起步, 随"收缩后夏普 / 满信心夏普"
       线性爬升到 risk-cap-ceiling (数据不够或夏普不够都拿不到高预算)
    3. 主动策略内部 (Schwab量化 / OKX量化) 按各自波动率倒数做等风险权重分配
       (Carver: 缺乏压倒性证据前默认等风险, 不按夏普差异分高低)
    4. 用双资产风险贡献方程, 结合被动/主动波动率和相关性, 反推被动:主动的资金权重
    5. 被动部分按原比例展开现有配置树 (current_allocation.py), 主动部分展开为
       Schwab量化/OKX量化 两条腿, 一起打印成树形结果

用法:
    python scripts/satellite_allocation.py \
        --schwab-quant-vol 0.15 --schwab-quant-sr 0.8 --schwab-quant-months 6 \
        --okx-quant-vol 0.30 --okx-quant-sr 0.6 --okx-quant-months 3 \
        --active-corr 0.5 --risk-cap-ceiling 0.3 --risk-floor 0.05 \
        --confidence-months 36 --full-confidence-sr 1.0 \
        --total 500000
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from current_allocation import (  # noqa: E402
    build_account_tree,
    get_passive_snapshot,
    latest_fx_rates,
    print_account_tree,
    refresh_data,
)


def shrink_sharpe(observed_sr: float, track_months: int, confidence_months: int) -> float:
    """把观测到的夏普比率, 按数据量向 0 (零 edge 先验) 收缩。"""
    confidence = min(1.0, track_months / confidence_months) if confidence_months > 0 else 1.0
    return confidence * observed_sr, confidence


def combine_active_sleeve(legs: dict) -> dict:
    """
    legs: {name: {"vol": float, "sr": float, "months": int}}
    组内按波动率倒数做等风险权重 (Carver: 缺乏证据前默认等风险, 不按夏普加权)。
    返回组合后的 vol / 收缩后夏普 / 内部权重。
    """
    names = list(legs.keys())
    inv_vol = {n: 1.0 / legs[n]["vol"] for n in names}
    total_inv_vol = sum(inv_vol.values())
    internal_w = {n: inv_vol[n] / total_inv_vol for n in names}

    # 组合波动率 (假设组内相关性 intra_corr, 简化为两两相同)
    intra_corr = legs[names[0]].get("intra_corr", 0.5) if len(names) > 1 else 0.0
    var = 0.0
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            rho = 1.0 if a == b else intra_corr
            var += internal_w[a] * internal_w[b] * legs[a]["vol"] * legs[b]["vol"] * rho
    combined_vol = var ** 0.5

    shrunk = {}
    expected_return = 0.0
    for n in names:
        sr_shrunk, confidence = shrink_sharpe(legs[n]["sr"], legs[n]["months"], legs[n]["confidence_months"])
        shrunk[n] = {"sr_shrunk": sr_shrunk, "confidence": confidence}
        expected_return += internal_w[n] * sr_shrunk * legs[n]["vol"]

    combined_sr_shrunk = expected_return / combined_vol if combined_vol > 0 else 0.0

    return {
        "vol": combined_vol,
        "sr_shrunk": combined_sr_shrunk,
        "internal_weights": internal_w,
        "per_leg": shrunk,
    }


def risk_contribution_of_active(w_active: float, vol_passive: float, vol_active: float, corr: float) -> float:
    """双资产 (被动, 主动) 组合中, 主动那部分占总风险的百分比 (Euler 风险贡献)。"""
    w_passive = 1.0 - w_active
    cov_pp = vol_passive ** 2
    cov_aa = vol_active ** 2
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
        # 即便主动权重趋近于0, 风险贡献已经超过目标 (说明主动波动率远高于被动) -> 给最小权重
        return lo
    if f(hi) < 0:
        return hi
    return brentq(f, lo, hi)


def main():
    parser = argparse.ArgumentParser(description="被动配置 + 主动策略 风险预算计算 (Carver 式)")
    parser.add_argument("--refresh", action="store_true", help="先抓取最新市场数据")
    parser.add_argument("--params", default="config/params.yaml")
    parser.add_argument("--config", default="config/backtest.yaml")
    parser.add_argument("--total", type=float, default=None, help="总资金 (人民币)。不指定则只输出比例")

    parser.add_argument("--schwab-quant-vol", type=float, default=None, help="Schwab量化策略年化波动率假设")
    parser.add_argument("--schwab-quant-sr", type=float, default=0.0, help="Schwab量化策略回测/实盘夏普")
    parser.add_argument("--schwab-quant-months", type=int, default=0, help="Schwab量化策略实盘月数")

    parser.add_argument("--okx-quant-vol", type=float, default=None, help="OKX量化策略年化波动率假设")
    parser.add_argument("--okx-quant-sr", type=float, default=0.0, help="OKX量化策略回测/实盘夏普")
    parser.add_argument("--okx-quant-months", type=int, default=0, help="OKX量化策略实盘月数")

    parser.add_argument("--intra-active-corr", type=float, default=0.5,
                         help="两个主动策略之间的相关性假设 (同一设计者的方法论重合风险, 默认偏保守)")
    parser.add_argument("--active-corr", type=float, default=0.5,
                         help="主动策略整体 与 被动配置 的相关性假设 (用危机期数字, 不用平静期数字)")
    parser.add_argument("--confidence-months", type=int, default=36,
                         help="需要多少个月实盘记录才算'满信心' (Carver: 通常要几年)")
    parser.add_argument("--full-confidence-sr", type=float, default=1.0,
                         help="满信心时, 收缩后夏普达到多少才给满风险预算上限")
    parser.add_argument("--risk-floor", type=float, default=0.05, help="主动策略风险预算下限 (数据不足时的起步值)")
    parser.add_argument("--risk-cap-ceiling", type=float, default=0.3, help="主动策略风险预算上限 (满信心时的政策上限)")
    parser.add_argument("--passive-vol", type=float, default=None,
                         help="被动配置目标波动率, 默认读 params.yaml 的 target_vol")

    args = parser.parse_args()

    active_legs = {}
    if args.schwab_quant_vol is not None:
        active_legs["Schwab量化"] = {
            "vol": args.schwab_quant_vol, "sr": args.schwab_quant_sr,
            "months": args.schwab_quant_months, "confidence_months": args.confidence_months,
            "intra_corr": args.intra_active_corr,
        }
    if args.okx_quant_vol is not None:
        active_legs["OKX量化"] = {
            "vol": args.okx_quant_vol, "sr": args.okx_quant_sr,
            "months": args.okx_quant_months, "confidence_months": args.confidence_months,
            "intra_corr": args.intra_active_corr,
        }

    if not active_legs:
        print("未提供任何主动策略的波动率假设 (--schwab-quant-vol / --okx-quant-vol), 无法计算。")
        sys.exit(1)

    if args.refresh:
        refresh_data()

    snap = get_passive_snapshot(args.params, args.config)

    passive_vol = args.passive_vol
    if passive_vol is None:
        from backtest.config import Params
        passive_vol = Params.load(args.params).target_vol

    active = combine_active_sleeve(active_legs)

    risk_budget_target = args.risk_floor + max(0.0, min(1.0, active["sr_shrunk"] / args.full_confidence_sr)) * (
        args.risk_cap_ceiling - args.risk_floor
    )

    w_active = solve_active_weight(risk_budget_target, passive_vol, active["vol"], args.active_corr)
    w_passive = 1.0 - w_active

    print("\n=== Carver 式风险预算决策链条 ===\n")
    for name, info in active["per_leg"].items():
        leg = active_legs[name]
        print(f"  {name}: 回测夏普={leg['sr']:.2f}, 实盘{leg['months']}个月 "
              f"→ 信心={info['confidence']:.0%} → 收缩后夏普={info['sr_shrunk']:.2f}")
    print(f"\n  主动策略组合波动率(等风险内部权重 {', '.join(f'{k}={v:.0%}' for k, v in active['internal_weights'].items())}) "
          f"= {active['vol']:.1%}")
    print(f"  主动策略组合收缩后夏普 = {active['sr_shrunk']:.2f}")
    print(f"  → 风险预算 = {args.risk_floor:.0%} + "
          f"min(1, {active['sr_shrunk']:.2f}/{args.full_confidence_sr:.2f}) × "
          f"({args.risk_cap_ceiling:.0%} - {args.risk_floor:.0%}) = {risk_budget_target:.1%}")
    print(f"  被动波动率={passive_vol:.1%}, 主动波动率={active['vol']:.1%}, 假设相关性={args.active_corr:.2f}")
    print(f"  → 解得 被动:主动 资金权重 = {w_passive:.1%} : {w_active:.1%}")

    fx_rate_to_cny, latest_fx_date = (None, None)
    if args.total is not None:
        fx_rate_to_cny, latest_fx_date = latest_fx_rates()

    print(f"\n=== 配置树 (被动配置 {w_passive:.1%} / 主动策略 {w_active:.1%}) ===\n")

    print(f"被动配置  {w_passive * 100:.2f}%")
    accounts = build_account_tree(snap.targets)
    print_account_tree(accounts, scale=w_passive, total_cny=args.total, fx_rate_to_cny=fx_rate_to_cny, indent="  ")

    # 每个主动策略走各自账户 (Schwab量化/OKX量化), 不合并
    active_by_account = {name: {"currency": "USD", "legs": {name: w * w_active}}
                          for name, w in active["internal_weights"].items()}
    print(f"主动策略  {w_active * 100:.2f}%")
    print_account_tree(active_by_account, scale=1.0, total_cny=args.total, fx_rate_to_cny=fx_rate_to_cny, indent="  ")

    print("Polymarket  — 暂不参与风险预算, 待对冲策略确定后再纳入\n")

    if args.total is None:
        print("(未指定 --total, 只显示比例; 加 --total <人民币金额> 可算出具体入金金额)")
    else:
        print(f"汇率基准: {latest_fx_date.date()}  "
              f"({', '.join(f'{c}={r:.4f}' for c, r in fx_rate_to_cny.items() if c != 'CNY')})")


if __name__ == "__main__":
    main()
