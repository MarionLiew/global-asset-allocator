"""
主回测循环 — 逐月从 start_date 到 end_date。

每月步骤:
1. mark-to-market (已实现回报更新)
2. Layer 0: compute_equity_budget
3. Layer 1: compute_regional_weights
4. Layer 2: compute_defensive_weights
5. 合成目标权重: equity legs = E * m_i, defensive legs = (1-E) * d_j
6. 执行: monthly_execute
7. 记录: weights, attribution, costs
8. 推进到下个月
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

from ..config import BacktestConfig, Params
from ..schema import BacktestResult, WeightSnapshot, AttributionRecord, ExecutionRecord
from .layer0 import compute_equity_budget
from .layer1 import compute_regional_weights
from .layer2 import compute_defensive_weights
from .execution import Portfolio, monthly_execute
from ..data._constants import EQUITY_MARKETS, DEFENSIVE_ASSETS, MARKET_CURRENCY

if TYPE_CHECKING:
    from ..data.provider import MarketDataProvider

logger = logging.getLogger(__name__)


def run_backtest(
    params: Params,
    bt_cfg: BacktestConfig,
    md: MarketDataProvider,
) -> BacktestResult:
    """主回测循环: 逐月从 start_date 到 end_date。

    同时计算两个被动基准。
    """
    # 获取所有可用月末日期
    available = md.get_available_dates()
    start = pd.Timestamp(bt_cfg.start_date)
    end = pd.Timestamp(bt_cfg.end_date)
    dates = [d for d in available if start <= d <= end]

    if not dates:
        raise ValueError(f"无可用日期在 {bt_cfg.start_date} ~ {bt_cfg.end_date}")

    logger.info(f"回测: {dates[0].date()} → {dates[-1].date()}, 共 {len(dates)} 月")

    # 初始化
    portfolio = Portfolio()
    contribution = bt_cfg.monthly_contribution_cny

    # 所有腿 ID
    all_legs = [f"{m}_equity" for m in EQUITY_MARKETS] + DEFENSIVE_ASSETS

    # 结果收集
    nav_series = {}
    weight_history = []
    executions = []
    attributions = []
    # 时间加权回报 (TWR) 追踪: 分离贡献 vs 回报
    cumulative_return = 1.0
    twr_series = {}
    prev_nav_after_exec = 0.0  # 上月执行后净值

    # 基准
    from ..benchmarks.static_60_40 import StaticSixtyForty
    from ..benchmarks.equal_weight import EqualWeight
    bench_6040 = StaticSixtyForty(bt_cfg, all_legs)
    bench_ew = EqualWeight(bt_cfg, all_legs)

    # 主循环
    for i, dt in enumerate(dates):
        asof = dt.date() if hasattr(dt, 'date') else dt

        # Step 1: mark-to-market (上月回报)
        if i > 0:
            prev_dt = dates[i - 1]
            prev_asof = prev_dt.date() if hasattr(prev_dt, 'date') else prev_dt
            returns_cny = {}
            for leg in all_legs:
                data_key = leg.replace("_equity", "")
                ret = md.monthly_return(data_key, asof)
                # FX 调整: 非 CNY 资产的回报需要考虑汇率变化
                currency = MARKET_CURRENCY.get(data_key, "CNY")
                if currency != "CNY":
                    # 简化: 直接用本币回报 (FX 影响在后面处理)
                    # 实际应该: ret_cny = (1+ret_local) * (fx_new/fx_old) - 1
                    pass
                returns_cny[data_key] = ret

            portfolio.mark_to_market(returns_cny)

            # 基准也 mark-to-market
            bench_6040.mark_to_market(returns_cny, asof)
            bench_ew.mark_to_market(returns_cny, asof)

        # Step 2: Layer 0
        E = compute_equity_budget(asof, params, md)

        # Step 3: Layer 1
        m_i = compute_regional_weights(asof, params, md)

        # Step 4: Layer 2
        d_j = compute_defensive_weights(asof, params, md)

        # Step 5: 合成目标权重
        targets = {}
        for mkt, w in m_i.items():
            targets[f"{mkt}_equity"] = E * w
        for j, w in d_j.items():
            targets[j] = (1.0 - E) * w

        # 记录权重快照
        snap = WeightSnapshot(
            asof=asof,
            E=E,
            m_i=dict(m_i),
            d_j=dict(d_j),
            targets=dict(targets),
            params_hash=params.params_hash,
        )
        weight_history.append(snap)

        # Step 6: 执行 (第一个月只注入, 不 mark-to-market)
        exec_rec = monthly_execute(
            asof=asof,
            targets=targets,
            portfolio=portfolio,
            md=md,
            params=params,
            bt_cfg=bt_cfg,
            contribution_cny=contribution,
        )
        executions.append(exec_rec)

        # Step 7: 归因 (简化版: 基于权重差 × 回报)
        if i > 0:
            attr = _compute_attribution(
                asof, E, m_i, d_j, returns_cny, targets, params, md
            )
            attributions.append(attr)

        # 记录 NAV (总值, 含贡献)
        nav_series[dt] = portfolio.nav

        # 记录时间加权回报 (TWR): 只反映投资回报, 不含贡献
        # 公式: R_t = (NAV_after_MT - NAV_before_MT) / NAV_before_MT
        # 其中 NAV_before_MT 是上月末净值 (即本月 mark-to-market 前)
        nav_after_mt = exec_rec.nav_before  # mark-to-market 后, 贡献前
        nav_before_mt = prev_nav_after_exec if i > 0 else 0
        if i > 0 and nav_before_mt > 0:
            market_return = (nav_after_mt - nav_before_mt) / nav_before_mt
            cumulative_return *= (1 + market_return)
        twr_series[dt] = cumulative_return
        prev_nav_after_exec = exec_rec.nav_after  # 本月执行后净值 (含贡献)

    # 构建结果 — 使用 TWR (时间加权回报) 用于公平比较
    nav_idx = pd.Series(twr_series)
    # 标准化到 1.0
    if nav_idx.iloc[0] > 0:
        nav_idx = nav_idx / nav_idx.iloc[0]

    bench_6040_nav = bench_6040.nav_series()
    bench_ew_nav = bench_ew.nav_series()

    # 也保存总 NAV (含贡献)
    total_nav = pd.Series(nav_series)

    return BacktestResult(
        strategy_nav=nav_idx,
        benchmark_navs={
            "static_60_40": bench_6040_nav,
            "equal_weight": bench_ew_nav,
        },
        total_nav=total_nav,
        weight_history=weight_history,
        executions=executions,
        attribution=attributions,
        total_costs=portfolio.total_cost_paid,
        params_hash=params.params_hash,
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )


def _compute_attribution(
    asof: date,
    E: float,
    m_i: dict[str, float],
    d_j: dict[str, float],
    returns: dict[str, float],
    targets: dict[str, float],
    params: Params,
    md: MarketDataProvider,
) -> AttributionRecord:
    """逐层归因: 逐层剥离法。

    R_total = R_base + ΔR_E_timing + ΔR_regional + ΔR_defensive
    """
    # 基准: E=E_base, 等权 region, 等权 defensive
    E_base = params.E_base
    n_eq = len(md.equity_markets())
    m_neutral = {m: 1.0 / n_eq for m in md.equity_markets()}
    n_def = len(md.defensive_assets())
    d_neutral = {j: 1.0 / n_def for j in md.defensive_assets()}

    # 计算各层回报
    def portfolio_return(E_val, m_vals, d_vals):
        total_ret = 0.0
        for mkt, w in m_vals.items():
            key = f"{mkt}_equity"
            ret = returns.get(mkt, 0.0)
            total_ret += E_val * w * ret
        for j, w in d_vals.items():
            ret = returns.get(j, 0.0)
            total_ret += (1.0 - E_val) * w * ret
        return total_ret

    # 基准回报 (全部中性)
    r_base = portfolio_return(E_base, m_neutral, d_neutral)

    # E timing: Layer 0 实际 E vs 固定 E_base
    r_e_timing = portfolio_return(E, m_neutral, d_neutral) - r_base

    # Regional: Layer 1 CAPE 倾斜 vs 等权
    r_regional = portfolio_return(E, m_i, d_neutral) - portfolio_return(E, m_neutral, d_neutral)

    # Defensive: Layer 2 逆波动 vs 等权
    r_defensive = portfolio_return(E, m_i, d_j) - portfolio_return(E, m_i, d_neutral)

    # 总回报
    r_total = portfolio_return(E, m_i, d_j)
    residual = r_total - (r_base + r_e_timing + r_regional + r_defensive)

    return AttributionRecord(
        asof=asof,
        E_timing=r_e_timing,
        regional_tilt=r_regional,
        defensive_comp=r_defensive,
        style=0.0,
        residual=residual,
        total=r_total,
    )
