#!/usr/bin/env python3
"""
被动配置 + 主动策略 两层风险预算计算 (Carver 式决策链条)。

策略/账户配置全部放在 config/satellite.yaml, 命令行只用来管总金额/是否刷新数据。
生产路径读取每个策略扣费后的 CNY 日收益，自动估计波动率与相关性；
没有收益文件时仍可使用手填波动率和保守相关性作为兼容后备。

决策链条:
    1. 校验策略收益的唯一性、完整性、时效、币种与成本口径
    2. 日收益 EWMA 估计波动率；重叠三日收益估计相关性并收缩、PSD 修复
    3. 账户内及账户间按 handcrafting 等风险层级组合，不按回测 Sharpe 配权
    4. 固定政策风险预算通过双资产风险贡献方程转为主动资金权重
    5. 被动和主动目标统一输出，交给月度 NTZ 执行

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
    get_passive_result,
    latest_fx_rates,
    refresh_data,
)
from backtest.active_sleeve import (  # noqa: E402
    StrategyDataError,
    build_active_risk_model,
    estimate_core_active_correlation,
)


def build_active_sleeve(cfg: dict, base_dir: Path = PROJECT_ROOT, asof=None):
    """
    构建主动层，并转换为旧展示接口需要的兼容结构。
    """
    model = build_active_risk_model(cfg, base_dir=base_dir, asof=asof)
    detail = {}
    account_level = {}
    for account, weights in model.account_strategy_weights.items():
        detail[account] = {}
        for name in weights:
            item = model.strategies[name]
            detail[account][name] = {
                "vol": item.annual_vol,
                "source": item.source,
                "observations": item.observations,
                "first_date": item.first_date,
                "last_date": item.last_date,
                "warnings": item.warnings,
            }
        members = list(weights)
        vector = [weights.get(n, 0.0) for n in model.covariance.index]
        import numpy as np
        vol = float(np.sqrt(np.asarray(vector) @ model.covariance.to_numpy() @ np.asarray(vector)))
        account_level[account] = {"vol": vol, "weights": weights}

    active_combined = {
        "vol": model.annual_vol,
        "weights": model.account_weights,
        "strategy_weights": model.strategy_weights,
        "daily_returns": model.daily_returns,
        "monthly_returns": model.monthly_returns,
        "correlation": model.correlation,
        "diagnostics": model.diagnostics,
    }
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

    if risk_budget_target <= 0:
        return 0.0
    if risk_budget_target >= 1:
        return 1.0

    def f(w_active):
        return risk_contribution_of_active(w_active, vol_passive, vol_active, corr) - risk_budget_target

    lo, hi = 1e-6, 1 - 1e-6
    if f(lo) > 0:
        return lo
    if f(hi) < 0:
        return hi
    return brentq(f, lo, hi)


def compute_active_split(
    cfg: dict,
    passive_vol: float,
    passive_returns=None,
    base_dir: Path = PROJECT_ROOT,
    asof=None,
):
    """整条决策链一步到位: satellite.yaml 配置 + 被动波动率 → 被动/主动资金拆分。

    返回 (w_active, active_combined, account_level, detail, risk_budget_target)。
    """
    active_combined, account_level, detail = build_active_sleeve(cfg, base_dir, asof)
    risk_budget_target = float(cfg.get("active_risk_budget", cfg.get("risk_floor", 0.0)))
    if not 0.0 <= risk_budget_target < 1.0:
        raise StrategyDataError("active_risk_budget must be in [0, 1)")
    top_cfg = cfg.get("top_level_risk", {})
    active_corr, corr_diag = estimate_core_active_correlation(
        passive_returns,
        active_combined.get("monthly_returns"),
        fallback=float(top_cfg.get("fallback_correlation", cfg.get("active_corr", 0.5))),
        floor=float(top_cfg.get("correlation_floor", 0.0)),
        span_months=int(top_cfg.get("correlation_ewma_span_months", 36)),
        min_months=int(top_cfg.get("min_common_months", 24)),
        shrinkage=float(top_cfg.get("correlation_shrinkage", 0.50)),
    )
    w_active = solve_active_weight(
        risk_budget_target, passive_vol, active_combined["vol"], active_corr
    )
    w_active = min(w_active, float(cfg.get("active_capital_cap", 1.0)))
    active_combined["core_active_correlation"] = active_corr
    active_combined["core_active_correlation_diagnostics"] = corr_diag
    active_combined["diagnostics"]["core_active_correlation"] = corr_diag
    return w_active, active_combined, account_level, detail, risk_budget_target


def active_strategy_rows(w_active, active_combined, account_level, include_zero=False):
    """展开主动层到 (策略key, 实体账户, 币种, 全局资金权重) 列表。

    策略名跨账户重名时加账户前缀保证 key 唯一。
    """
    if w_active <= 0 and not include_zero:
        return []
    rows = []
    for account_name in active_combined["weights"]:
        physical = PHYSICAL_ACCOUNT.get(account_name, account_name)
        currency = ACCOUNT_CURRENCY.get(physical, "CNY")
        for strat in account_level[account_name]["weights"]:
            rows.append((
                strat,
                physical,
                currency,
                w_active * active_combined["strategy_weights"][strat],
            ))
    return rows


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
            date_range = "manual"
            if info["first_date"] is not None:
                date_range = f"{info['first_date'].date()}..{info['last_date'].date()} ({info['observations']}日)"
            print(f"    {strat_name}: vol={info['vol']:.1%}, source={info['source']}, {date_range}")
            for warning in info["warnings"]:
                print(f"      ⚠️ {warning}")
        acc = account_level[account_name]
        print(f"    → 账户组合: vol={acc['vol']:.1%}, "
              f"内部权重={ {k: f'{v:.0%}' for k, v in acc['weights'].items()} }")

    print(f"\n  主动策略整体: vol={active_combined['vol']:.1%}, "
          f"账户间权重={ {k: f'{v:.0%}' for k, v in active_combined['weights'].items()} }")
    corr_meta = active_combined["diagnostics"]["correlation"]
    print(f"  主动相关性模式={corr_meta.get('mode')}, "
          f"重叠调整有效样本={corr_meta.get('overlap_adjusted_effective_observations', 'n/a')}")
    print(f"  → 固定政策风险预算 = {risk_budget_target:.1%}")
    print(f"  被动波动率={passive_vol:.1%}, 主动波动率={active_combined['vol']:.1%}, "
          f"使用相关性={active_combined['core_active_correlation']:.2f}")
    print(f"  → 解得 被动:主动 资金权重 = {w_passive:.1%} : {w_active:.1%}")


def main():
    parser = argparse.ArgumentParser(description="被动配置 + 主动策略 风险预算计算 (Carver 式)")
    parser.add_argument("--refresh", action="store_true", help="先抓取最新市场数据")
    parser.add_argument("--params", default="config/params.yaml")
    parser.add_argument("--config", default="config/backtest.yaml")
    parser.add_argument("--satellite-config", default="config/satellite.yaml", help="主动策略配置文件")
    parser.add_argument("--total", type=float, default=None, help="总资金 (人民币)。不指定则只输出比例")
    parser.add_argument("--passive-vol", type=float, default=None,
                         help="覆盖被动组合年化波动率；默认从被动净值估计")
    parser.add_argument("--verbose", action="store_true", help="显示 Carver 决策链条推导过程")
    parser.add_argument("--output", default=None, help="可选: 保存机器可读的配置结果 JSON")
    args = parser.parse_args()

    with open(args.satellite_config) as f:
        cfg = yaml.safe_load(f)

    if args.refresh:
        refresh_data()

    passive_result = get_passive_result(args.params, args.config)
    snap = passive_result.weight_history[-1]
    passive_returns = passive_result.strategy_nav.pct_change().dropna()

    passive_vol = args.passive_vol
    if passive_vol is None:
        span = int(cfg.get("top_level_risk", {}).get("passive_vol_ewma_span_months", 12))
        passive_vol = float(passive_returns.ewm(span=span, min_periods=12).std().iloc[-1] * (12 ** 0.5))

    w_active, active_combined, account_level, detail, risk_budget_target = compute_active_split(
        cfg,
        passive_vol,
        passive_returns=passive_returns,
        base_dir=PROJECT_ROOT,
        asof=snap.asof,
    )
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
    for key, physical, _currency, w in active_strategy_rows(w_active, active_combined, account_level):
        rows.append((physical, "量化", key, w))

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

    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = PROJECT_ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "asof": str(snap.asof),
            "passive_vol": passive_vol,
            "active_vol": active_combined["vol"],
            "active_risk_budget": risk_budget_target,
            "passive_capital_weight": w_passive,
            "active_capital_weight": w_active,
            "core_active_correlation": active_combined["core_active_correlation"],
            "strategy_weights_within_active": active_combined["strategy_weights"],
            "diagnostics": active_combined["diagnostics"],
        }
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"机器可读结果: {output}")


if __name__ == "__main__":
    main()
