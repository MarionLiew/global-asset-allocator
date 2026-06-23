"""
共享 fixtures — 合成 MarketDataProvider + 最小 Params。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from datetime import date

from backtest.config import Params, BacktestConfig


class SyntheticProvider:
    """合成 MarketDataProvider — 确定性测试用。"""

    def __init__(self, n_months: int = 120):
        self.n_months = n_months
        np.random.seed(42)

    def equity_markets(self) -> list[str]:
        return ["US", "DM", "CN", "HK"]

    def defensive_assets(self) -> list[str]:
        return ["CN_GOVT", "TIPS", "GOLD", "CORP_BOND", "EM_BOND"]

    def cape(self, market: str, asof: date) -> float:
        return {"US": 20.0, "DM": 18.0, "CN": 15.0, "HK": 14.0}.get(market, 18.0)

    def cape_target(self, market: str, asof: date) -> float:
        return {"US": 16.5, "DM": 15.0, "CN": 13.0, "HK": 12.0}.get(market, 16.0)

    def cap_weight(self, market: str, asof: date) -> float:
        return {"US": 0.50, "DM": 0.20, "CN": 0.20, "HK": 0.10}.get(market, 0.25)

    def cap_weights(self, asof: date) -> dict[str, float]:
        return {m: self.cap_weight(m, asof) for m in self.equity_markets()}

    def earnings_yield_world(self, asof: date) -> float:
        return 1.0 / 20.0  # 5%

    def real_yield(self, asof: date) -> float:
        return 0.02  # 2%

    def erp_rolling_median(self, asof: date) -> float:
        return 0.03  # 3%

    def vol(self, asset: str, asof: date) -> float:
        return {"US": 0.16, "DM": 0.18, "CN": 0.25, "HK": 0.22,
                "CN_GOVT": 0.05, "TIPS": 0.06, "GOLD": 0.15,
                "CORP_BOND": 0.08, "EM_BOND": 0.10}.get(asset, 0.15)

    def monthly_return(self, asset: str, asof: date) -> float:
        # 确定性回报 (基于月份)
        month_idx = asof.month + asof.year * 12
        np.random.seed(hash((asset, month_idx)) % 2**31)
        return np.random.normal(0.005, 0.04)

    def monthly_return_cny(self, asset: str, asof: date) -> float:
        return self.monthly_return(asset, asof)

    def growth_inflation_quadrant(self, asof: date) -> str:
        return "GG"

    def fx_rate(self, currency: str, asof: date) -> float:
        return {"CNY": 1.0, "USD": 7.2, "HKD": 0.92}.get(currency, 1.0)

    def momentum(self, market: str, asof: date) -> float:
        """标准化动量分数 (确定性, 基于月份哈希)。"""
        month_idx = asof.month + asof.year * 12
        np.random.seed(hash(("momentum", market, month_idx)) % 2**31)
        return float(np.clip(np.random.normal(0, 0.3), -1, 1))

    def get_available_dates(self) -> list[pd.Timestamp]:
        return pd.date_range("1995-01-31", periods=self.n_months, freq="ME")


@pytest.fixture
def provider():
    return SyntheticProvider(120)


@pytest.fixture
def params():
    return Params(
        E_base=0.60,
        k0=0.20,
        E_min=0.40,
        E_max=0.80,
        lambda_={"US": 0.6, "DM": 0.6, "CN": 0.3, "HK": 0.3},
        band_pp=0.15,
        delta_home=0.03,
        cape_target_window=120,
        delta_quadrant=0.08,
        defensive_single_asset_cap=0.45,
        ewma_fast_halflife=6,
        ewma_slow_halflife=36,
        ewma_mix_weight=0.7,
        H=10.0,
    )


@pytest.fixture
def bt_cfg():
    return BacktestConfig(
        start_date="1995-01-31",
        end_date="2004-12-31",
        monthly_contribution_cny=10000,
    )
