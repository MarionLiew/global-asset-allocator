"""
成本/税费/FX 点差模型。

成本从 NAV 中扣除 (不降低投资额)。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import CostsConfig


@dataclass
class CostBreakdown:
    """单笔交易成本明细。"""
    commission: float = 0.0    # 佣金
    fx_spread: float = 0.0     # 换汇点差
    dividend_tax: float = 0.0  # 股息预扣税

    @property
    def total(self) -> float:
        return self.commission + self.fx_spread + self.dividend_tax


def compute_trade_cost(
    notional: float,
    asset_type: str,         # "equity" / "defensive"
    asset_currency: str,     # 资产计价货币
    base_currency: str,      # 组合基准货币
    costs_cfg: CostsConfig,
) -> CostBreakdown:
    """计算单笔交易成本。

    参数:
        notional: 交易金额 (本币)
        asset_type: "equity" / "defensive"
        asset_currency: 资产计价货币
        base_currency: 组合基准货币 (CNY)
        costs_cfg: 成本配置
    """
    breakdown = CostBreakdown()

    # 佣金
    bps = costs_cfg.equity_bps if asset_type == "equity" else costs_cfg.defensive_bps
    breakdown.commission = notional * (bps / 10_000)

    # 换汇点差 (只对非基准货币收取, 半边)
    if asset_currency != base_currency:
        breakdown.fx_spread = notional * (costs_cfg.fx_spread_bps / 10_000)

    return breakdown


def apply_dividend_tax(
    dividend_yield: float,
    notional: float,
    market: str,
    costs_cfg: CostsConfig,
) -> float:
    """计算股息预扣税。

    参数:
        dividend_yield: 股息率 (年化)
        notional: 持仓市值
        market: 市场代码 (US/DM/CN/HK)
        costs_cfg: 成本配置

    返回: 股息预扣税金额 (月度)
    """
    rate = costs_cfg.tax_dividend_withholding.get(market, 0.0)
    if rate <= 0:
        return 0.0
    # 年化股息 → 月度
    monthly_div = notional * dividend_yield / 12
    return monthly_div * rate
