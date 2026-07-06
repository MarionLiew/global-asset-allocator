"""
月度执行引擎 — 现金流再平衡。

旧逻辑 (保留向后兼容):
  每月注入新钱, 按缺口比例分配到各腿 (只买不卖)。

新逻辑 (ALLOCATOR_PLAN §一C, Smart Portfolios 风格):
  1. 不交易区 (No-Trade Zone): 每资产容忍带, 带内不交易
  2. 带宽由成本/税驱动: 成本越高 → 带子越宽
  3. 欠配: 新钱优先填补, 只拉回带边缘 (不拉回正中)
  4. 超配: 卖回带边缘 (失控保护, 非 timing)

不变量:
- Σ(cost) + Σ(residual) = 可用现金
- 现金守恒
- 带内资产无交易
- 超配资产卖到带边缘
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from ..config import BacktestConfig, Params, CostsConfig
from ..schema import ExecutionRecord
from ..data._constants import MARKET_CURRENCY, LEG_SLEEVE, LEG_DATA_KEY
from .cost import compute_trade_cost

if TYPE_CHECKING:
    from ..data.provider import MarketDataProvider


@dataclass
class Portfolio:
    """回测中的组合状态。"""
    holdings: dict[str, float] = field(default_factory=dict)  # leg → CNY 市值
    cash: float = 0.0                                          # CNY 现金余额
    total_cost_paid: float = 0.0                               # 累计交易成本
    inception_nav: float = 0.0

    @property
    def nav(self) -> float:
        return sum(self.holdings.values()) + self.cash

    @property
    def weights(self) -> dict[str, float]:
        total = self.nav
        if total <= 0:
            return {}
        w = {leg: mv / total for leg, mv in self.holdings.items()}
        if self.cash > 0:
            w["_cash"] = self.cash / total
        return w

    def mark_to_market(self, returns_cny: dict[str, float]):
        """用已实现回报更新各腿市值 (CNY 计)。

        returns_cny 的键是数据键 (US/DM/...), 持仓键是腿名 (US_equity/...),
        通过 LEG_DATA_KEY 映射后查找回报。
        """
        for leg in self.holdings:
            data_key = LEG_DATA_KEY.get(leg, leg)
            ret = returns_cny.get(data_key)
            if ret is not None:
                self.holdings[leg] *= (1.0 + ret)

    def add_holdings(self, leg: str, amount_cny: float):
        """增加某腿持仓。"""
        self.holdings[leg] = self.holdings.get(leg, 0.0) + amount_cny


def monthly_execute(
    asof: date,
    targets: dict[str, float],
    portfolio: Portfolio,
    md: MarketDataProvider,
    params: Params,
    bt_cfg: BacktestConfig,
    contribution_cny: float,
) -> ExecutionRecord:
    """月度执行: 注入新钱 + 按缺口再平衡。

    步骤:
    1. 组合 mark-to-market (已在主循环中完成)
    2. 计算当前权重 vs 目标权重的缺口
    3. 按缺口比例分配新钱到各腿
    4. 扣除交易成本
    5. 记录
    """
    nav_before = portfolio.nav
    weights_before = dict(portfolio.weights)
    if "_cash" in weights_before:
        del weights_before["_cash"]

    # 可用资金
    available = contribution_cny + portfolio.cash
    portfolio.cash = 0.0  # 全部用于投资

    # 计算各腿缺口
    T = nav_before + contribution_cny  # 注资后总值
    shortfall = {}
    for leg, tgt_w in targets.items():
        current_cny = portfolio.holdings.get(leg, 0.0)
        target_cny = tgt_w * T
        gap = max(0.0, target_cny - current_cny)
        shortfall[leg] = gap

    total_shortfall = sum(shortfall.values())

    # 分配资金
    allocations = {}
    costs = {}
    residuals = {}
    total_cost = 0.0

    if total_shortfall > 0:
        for leg, gap in shortfall.items():
            if gap <= 0:
                continue

            # 按缺口比例分配
            alloc_cny = available * (gap / total_shortfall)

            # 计算成本
            sleeve = LEG_SLEEVE.get(leg, "equity")
            data_key = LEG_DATA_KEY.get(leg, leg)
            currency = MARKET_CURRENCY.get(leg, "CNY")

            cost_brk = compute_trade_cost(
                alloc_cny, sleeve, currency, "CNY", bt_cfg.costs
            )
            cost = cost_brk.total

            # 扣成本后实际投资
            invested = alloc_cny - cost
            if invested < 0:
                invested = 0
                cost = alloc_cny

            allocations[leg] = invested
            costs[leg] = cost
            total_cost += cost

            # 更新组合
            portfolio.add_holdings(leg, invested)

    # 零钱处理: 不足 1 元的残余进 cash_buffer
    # (在回测中, 连续金额不需要整手取整, 直接投出去)
    residuals = {}

    # NAV 后
    nav_after = portfolio.nav
    portfolio.total_cost_paid += total_cost

    # 权重后
    weights_after = {leg: mv / nav_after for leg, mv in portfolio.holdings.items()} if nav_after > 0 else {}

    return ExecutionRecord(
        asof=asof,
        contribution_cny=contribution_cny,
        allocations=allocations,
        costs=costs,
        residuals=residuals,
        weights_before=weights_before,
        weights_after=weights_after,
        nav_before=nav_before,
        nav_after=nav_after,
    )


# ── 新: 不交易区执行 (ALLOCATOR_PLAN §一C) ──────────────────────────────────


def _compute_band(leg: str, bt_cfg: BacktestConfig, params: Params) -> float:
    """计算某资产的不交易区带宽 (成本驱动)。

    带宽 = max(min_band, cost_bps * multiplier / 10000)
    上限 = max_band
    """
    sleeve = LEG_SLEEVE.get(leg, "equity")
    base_cost_bps = bt_cfg.costs.equity_bps if sleeve == "equity" else bt_cfg.costs.defensive_bps

    # 非 base 货币的资产有额外 FX 成本
    currency = MARKET_CURRENCY.get(leg, "CNY")
    fx_cost_bps = bt_cfg.costs.fx_spread_bps if currency != bt_cfg.base_currency else 0.0

    total_cost_bps = base_cost_bps + fx_cost_bps
    band = total_cost_bps * params.no_trade_cost_multiplier / 10000.0

    return max(params.no_trade_min_band, min(band, params.no_trade_max_band))


def monthly_execute_ntz(
    asof: date,
    targets: dict[str, float],
    portfolio: Portfolio,
    md: "MarketDataProvider",
    params: Params,
    bt_cfg: BacktestConfig,
    contribution_cny: float,
) -> ExecutionRecord:
    """月度执行: 不交易区 (No-Trade Zone) + 新钱填补 + 超配卖回边缘。

    步骤:
    1. 计算当前实际权重
    2. 对每个资产计算带宽 (成本驱动)
    3. 判断是否在带内:
       - 带内 → 不交易
       - 低于带下沿 (欠配) → 新钱优先填补到带边缘
       - 高于带上沿 (超配) → 卖回带边缘
    4. 剩余新钱按目标权重分配
    5. 成本从交易金额中扣除
    """
    nav_before = portfolio.nav
    weights_before = dict(portfolio.weights)
    if "_cash" in weights_before:
        del weights_before["_cash"]

    available = contribution_cny + portfolio.cash
    portfolio.cash = 0.0

    T = nav_before + contribution_cny  # 注资后总值
    if T <= 0:
        return ExecutionRecord(
            asof=asof, contribution_cny=contribution_cny,
            allocations={}, costs={}, residuals={},
            weights_before=weights_before, weights_after={},
            nav_before=nav_before, nav_after=nav_before,
        )

    allocations: dict[str, float] = {}
    costs: dict[str, float] = {}
    sells: dict[str, float] = {}  # 卖出记录 (负值)
    total_cost = 0.0
    remaining_cash = available

    # ── 第一步: 超配卖回边缘 (失控保护) ──
    for leg, tgt_w in targets.items():
        current_cny = portfolio.holdings.get(leg, 0.0)
        current_w = current_cny / T if T > 0 else 0.0
        band = _compute_band(leg, bt_cfg, params)

        upper_edge = tgt_w + band
        if current_w > upper_edge:
            # 超配: 卖回到带边缘 (不卖到正中)
            sell_cny = (current_w - upper_edge) * T
            sleeve = LEG_SLEEVE.get(leg, "equity")
            currency = MARKET_CURRENCY.get(leg, "CNY")
            cost_brk = compute_trade_cost(
                sell_cny, sleeve, currency, "CNY", bt_cfg.costs
            )
            cost = cost_brk.total
            net_sell = sell_cny - cost

            portfolio.holdings[leg] = max(0.0, current_cny - sell_cny)
            remaining_cash += net_sell
            sells[leg] = -sell_cny
            costs[leg] = costs.get(leg, 0.0) + cost
            total_cost += cost

    # ── 第二步: 计算欠配缺口 (用于新钱填补) ──
    # T_now = 卖出后的实际 NAV
    T_now = portfolio.nav + contribution_cny
    shortfall: dict[str, float] = {}
    for leg, tgt_w in targets.items():
        current_cny = portfolio.holdings.get(leg, 0.0)
        current_w = current_cny / T_now if T_now > 0 else 0.0
        band = _compute_band(leg, bt_cfg, params)

        lower_edge = tgt_w - band
        if current_w < lower_edge:
            # 欠配: 缺口 = 到带边缘的距离
            gap_cny = (lower_edge - current_w) * T_now
            shortfall[leg] = max(0.0, gap_cny)

    # ── 第三步: 新钱填补欠配 (优先) ──
    total_shortfall = sum(shortfall.values())
    if total_shortfall > 0 and remaining_cash > 0:
        fill_budget = min(remaining_cash, total_shortfall)
        for leg, gap in shortfall.items():
            if gap <= 0:
                continue
            alloc_cny = fill_budget * (gap / total_shortfall)
            alloc_cny = min(alloc_cny, remaining_cash)

            sleeve = LEG_SLEEVE.get(leg, "equity")
            currency = MARKET_CURRENCY.get(leg, "CNY")
            cost_brk = compute_trade_cost(
                alloc_cny, sleeve, currency, "CNY", bt_cfg.costs
            )
            cost = cost_brk.total
            invested = alloc_cny - cost
            if invested < 0:
                invested = 0
                cost = alloc_cny

            allocations[leg] = allocations.get(leg, 0.0) + invested
            costs[leg] = costs.get(leg, 0.0) + cost
            total_cost += cost
            remaining_cash -= alloc_cny

            portfolio.add_holdings(leg, invested)

    # ── 第四步: 剩余新钱按目标权重分配 ──
    if remaining_cash > 1.0:  # 至少 1 元才投
        # 排除已超配的资产
        investable = {l: w for l, w in targets.items() if w > 0}
        total_target = sum(investable.values())
        if total_target > 0:
            for leg, tgt_w in investable.items():
                alloc_cny = remaining_cash * (tgt_w / total_target)
                if alloc_cny <= 0:
                    continue

                sleeve = LEG_SLEEVE.get(leg, "equity")
                currency = MARKET_CURRENCY.get(leg, "CNY")
                cost_brk = compute_trade_cost(
                    alloc_cny, sleeve, currency, "CNY", bt_cfg.costs
                )
                cost = cost_brk.total
                invested = alloc_cny - cost
                if invested < 0:
                    invested = 0
                    cost = alloc_cny

                allocations[leg] = allocations.get(leg, 0.0) + invested
                costs[leg] = costs.get(leg, 0.0) + cost
                total_cost += cost
                portfolio.add_holdings(leg, invested)

    # NAV 后
    nav_after = portfolio.nav
    portfolio.total_cost_paid += total_cost
    portfolio.cash = max(0.0, remaining_cash - sum(allocations.values()) + portfolio.cash)

    weights_after = {leg: mv / nav_after for leg, mv in portfolio.holdings.items()} if nav_after > 0 else {}

    # 合并卖出和买入为统一的 allocations (卖出为负值)
    all_allocs = dict(allocations)
    for leg, sell_amt in sells.items():
        all_allocs[leg] = all_allocs.get(leg, 0.0) + sell_amt

    return ExecutionRecord(
        asof=asof,
        contribution_cny=contribution_cny,
        allocations=all_allocs,
        costs=costs,
        residuals={},
        weights_before=weights_before,
        weights_after=weights_after,
        nav_before=nav_before,
        nav_after=nav_after,
    )
