"""
月度执行引擎 — 现金流再平衡。

核心: 每月注入新钱, 按缺口比例分配到各腿 (只买不卖)。
Layer 3 门控: 全部 stub (passed=false), 所有股票腿走默认 ETF。

不变量:
- 无任何 SELL
- Σ(cost) + Σ(residual) = 可用现金
- 现金守恒
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
        """用已实现回报更新各腿市值 (CNY 计)。"""
        for leg, ret in returns_cny.items():
            if leg in self.holdings:
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
