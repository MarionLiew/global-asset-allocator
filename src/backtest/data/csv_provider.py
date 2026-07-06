"""
CSVProvider — 从 processed/ parquet 加载数据, 严格 PIT 纪律。

所有查询只返回 asof 之前可见的数据。
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Params
from ..engine.ewma import mixed_ewma_vol
from ._constants import (
    EQUITY_MARKETS, DEFENSIVE_ASSETS, MOMENTUM_LOOKBACK, MOMENTUM_SKIP,
)

logger = logging.getLogger(__name__)


class CSVProvider:
    """从 processed/ parquet 文件驱动的 MarketDataProvider。"""

    def __init__(self, data_dir: str | Path, params: Params):
        self.data_dir = Path(data_dir)
        self.params = params
        self._load_data()

    def _load_data(self):
        """加载所有 parquet 到内存。"""
        d = self.data_dir / "data" / "processed"
        if not d.exists():
            d = self.data_dir / "processed"  # fallback

        # ETF 回报
        p = d / "etf_returns.parquet"
        if p.exists():
            self._etf_returns = pd.read_parquet(p)
            self._etf_returns["date"] = pd.to_datetime(self._etf_returns["date"])
        else:
            logger.warning(f"ETF 回报文件不存在: {p}")
            self._etf_returns = pd.DataFrame()

        # CAPE
        p = d / "cape_series.parquet"
        if p.exists():
            self._cape = pd.read_parquet(p)
            self._cape["date"] = pd.to_datetime(self._cape["date"])
        else:
            logger.warning(f"CAPE 文件不存在: {p}")
            self._cape = pd.DataFrame()

        # FX
        p = d / "fx_rates.parquet"
        if p.exists():
            self._fx = pd.read_parquet(p)
            self._fx["date"] = pd.to_datetime(self._fx["date"])
        else:
            logger.warning(f"FX 文件不存在: {p}")
            self._fx = pd.DataFrame()

        # 宏观指标
        p = d / "macro_data.parquet"
        if p.exists():
            self._macro = pd.read_parquet(p)
            self._macro.index = pd.to_datetime(self._macro.index)
        else:
            logger.warning(f"宏观指标文件不存在: {p}")
            self._macro = pd.DataFrame()

        # 构建回报索引 (asset_id, date) → 快速查询
        if not self._etf_returns.empty:
            self._return_idx = self._etf_returns.set_index(["asset_id", "date"]).sort_index()
        else:
            self._return_idx = pd.DataFrame()

        # 预计算 EWMA 波动率
        self._vol_cache: dict[str, pd.Series] = {}
        if not self._etf_returns.empty:
            for asset_id, grp in self._etf_returns.groupby("asset_id"):
                grp = grp.set_index("date").sort_index()
                rets = grp["return_m"]
                vols = self._compute_ewma_series(rets)
                self._vol_cache[asset_id] = vols

        # 预计算动量分数 (横截面 z-score)
        self._momentum_df: pd.DataFrame | None = None
        self._load_momentum()

    def _compute_ewma_series(self, rets: pd.Series) -> pd.Series:
        """预计算混合 EWMA 波动率序列 (年化)。"""
        p = self.params
        fast_alpha = 1 - np.exp(-np.log(2) / p.ewma_fast_halflife)
        slow_alpha = 1 - np.exp(-np.log(2) / p.ewma_slow_halflife)
        w = p.ewma_mix_weight

        fast_var = rets.ewm(alpha=fast_alpha, min_periods=12).var()
        slow_var = rets.ewm(alpha=slow_alpha, min_periods=12).var()
        mixed_var = w * fast_var + (1 - w) * slow_var
        return np.sqrt(mixed_var) * np.sqrt(12)  # 年化

    def _asof_mask(self, df: pd.DataFrame, asof: date, date_col: str = "date") -> pd.DataFrame:
        """PIT: 只返回 asof 之前 (含) 的数据。"""
        asof_ts = pd.Timestamp(asof)
        return df[df[date_col] <= asof_ts]

    def _load_momentum(self):
        """预计算动量分数: 经典 12-1 动量，跨市场截面 z-score。"""
        if self._etf_returns.empty:
            return

        # pivot 成 (date, market) 回报矩阵
        equity = self._etf_returns[
            self._etf_returns["asset_id"].isin(EQUITY_MARKETS)
        ].copy()
        if equity.empty:
            return

        ret_matrix = equity.pivot_table(
            index="date", columns="asset_id", values="return_m"
        ).sort_index()

        # 12-1 动量: 累积回报 [t-12, t-2]（跳过最近 1 个月）
        lookback = MOMENTUM_LOOKBACK
        skip = MOMENTUM_SKIP
        mom_raw = pd.DataFrame(index=ret_matrix.index, columns=ret_matrix.columns, dtype=float)
        for mkt in ret_matrix.columns:
            col = ret_matrix[mkt]
            # 用 (1+r).rolling().apply(product) 或 shift+rolling
            log_ret = np.log(1 + col)
            cum = log_ret.rolling(window=lookback, min_periods=lookback).sum()
            cum_skip = log_ret.rolling(window=lookback - skip, min_periods=lookback - skip).sum().shift(skip)
            mom_raw[mkt] = cum_skip  # 已跳过最近 skip 个月

        # 横截面 z-score（每月跨市场标准化）
        mom_mean = mom_raw.mean(axis=1)
        mom_std = mom_raw.std(axis=1).replace(0, np.nan)
        mom_z = mom_raw.sub(mom_mean, axis=0).div(mom_std, axis=0)
        mom_z = mom_z.clip(-1, 1)  # clip 到 [-1, +1]

        # 转成长格式
        mom_long = mom_z.stack().reset_index()
        mom_long.columns = ["date", "market", "momentum"]
        mom_long = mom_long.dropna(subset=["momentum"])
        self._momentum_df = mom_long

    def _latest(self, df: pd.DataFrame, asof: date, date_col: str = "date"):
        """取 asof 之前最新一行。"""
        subset = self._asof_mask(df, asof, date_col)
        if subset.empty:
            return None
        return subset.iloc[-1]

    # ─── MarketDataProvider 接口 ───

    def equity_markets(self) -> list[str]:
        return EQUITY_MARKETS

    def defensive_assets(self) -> list[str]:
        return DEFENSIVE_ASSETS

    def cape(self, market: str, asof: date) -> float:
        """某市场 CAPE。US 用 Shiller; 其他市场用合成/代理。"""
        if market == "US":
            row = self._latest(self._cape, asof)
            if row is not None:
                return float(row["cape"])
        # 其他市场: 用 ETF 回报序列推算的 CAPE 代理 (后续可替换)
        return self._cape_proxy(market, asof)

    def _cape_proxy(self, market: str, asof: date) -> float:
        """无真实 CAPE 数据的市场返回 0 → 估值信号中性化。

        之前返回写死的常数 (DM=18 vs 目标15 等), 相当于给非美市场注入
        永久性的负估值信号 — 比没有信号更糟。没有数据就诚实地不给信号。
        """
        return 0.0

    def cape_target(self, market: str, asof: date) -> float:
        """CAPE 滚动中位 (目标值)。"""
        if market == "US" and not self._cape.empty:
            subset = self._asof_mask(self._cape, asof)
            if len(subset) >= 60:
                return float(subset["cape"].median())
        DEFAULT_CAPE_TARGET = {"US": 16.5, "DM": 15.0, "CN": 13.0, "HK": 12.0}
        return DEFAULT_CAPE_TARGET.get(market, 16.0)

    def cap_weight(self, market: str, asof: date) -> float:
        """某市场市值权重。使用配置中的先验权重 (因历史 MSCI 权重难以获取)。"""
        from ..config import BacktestConfig
        bt_cfg = BacktestConfig.load()
        mkt_cfg = bt_cfg.equity_markets.get(market, {})
        return mkt_cfg.get("cap_weight", 0.25)

    def cap_weights(self, asof: date) -> dict[str, float]:
        return {m: self.cap_weight(m, asof) for m in self.equity_markets()}

    def earnings_yield_world(self, asof: date) -> float:
        """全球盈利收益率 = 1/CAPE_world (加权平均)。"""
        total_weight = 0.0
        weighted_cape = 0.0
        for m in self.equity_markets():
            w = self.cap_weight(m, asof)
            c = self.cape(m, asof)
            if c > 0:
                weighted_cape += w * c
                total_weight += w
        if total_weight > 0:
            return 1.0 / (weighted_cape / total_weight)
        return 1.0 / 20.0  # fallback

    def real_yield(self, asof: date) -> float:
        """实际利率。"""
        if not self._macro.empty and "real_yield" in self._macro.columns:
            # PIT: 只用 asof 之前的数据
            subset = self._macro[self._macro.index <= pd.Timestamp(asof)]
            if not subset.empty and subset["real_yield"].notna().any():
                val = subset["real_yield"].dropna().iloc[-1]
                return float(val) / 100.0  # 转为小数
        # fallback: 名义 10Y - 通胀
        return 0.02  # 2% 近似

    def erp_rolling_median(self, asof: date) -> float:
        """ERP 滚动中位。"""
        # 计算历史 ERP 序列
        if not self._cape.empty and not self._macro.empty:
            # 简化: 用一个固定参考值
            pass
        return 0.03  # 3% 近似历史中位

    def vol(self, asset: str, asof: date) -> float:
        """混合 EWMA 波动率 (年化)。"""
        if asset in self._vol_cache:
            series = self._vol_cache[asset]
            subset = series[series.index <= pd.Timestamp(asof)]
            if not subset.empty:
                return float(subset.iloc[-1])
        return 0.15  # 15% 默认

    def monthly_return(self, asset: str, asof: date) -> float:
        """某资产 asof 月的总回报 (本币)。"""
        if self._return_idx.empty:
            return 0.0
        key = (asset, pd.Timestamp(asof))
        if key in self._return_idx.index:
            return float(self._return_idx.loc[key, "return_m"])
        return 0.0

    def monthly_return_cny(self, asset: str, asof: date) -> float:
        """某资产 asof 月的总回报 (折 CNY)。

        ret_cny = (1 + ret_local) × (fx_t / fx_{t-1}) - 1
        无汇率数据的月份 fx_rate 返回常数默认值, 比值为 1, 退化为本币回报。
        """
        from ._constants import MARKET_CURRENCY

        ret_local = self.monthly_return(asset, asof)
        currency = MARKET_CURRENCY.get(asset, "CNY")
        if currency == "CNY":
            return ret_local

        asof_ts = pd.Timestamp(asof)
        prev_ts = (asof_ts - pd.offsets.MonthEnd(1)).date()
        fx_now = self.fx_rate(currency, asof)
        fx_prev = self.fx_rate(currency, prev_ts)
        if fx_prev <= 0:
            return ret_local
        return (1.0 + ret_local) * (fx_now / fx_prev) - 1.0

    def growth_inflation_quadrant(self, asof: date) -> str:
        """象限分类: GG/GI/IG/II。"""
        if self._macro.empty:
            return "GG"  # 默认

        asof_ts = pd.Timestamp(asof)
        subset = self._macro[self._macro.index <= asof_ts]

        # 增长: INDPRO YoY, 含 3 月滞后
        growth = None
        if "growth_yoy_lagged" in subset.columns:
            g = subset["growth_yoy_lagged"].dropna()
            if not g.empty:
                growth = float(g.iloc[-1])

        # 通胀: CPI YoY, 含 3 月滞后
        inflation = None
        if "cpi_yoy_lagged" in subset.columns:
            cpi = subset["cpi_yoy_lagged"].dropna()
            if not cpi.empty:
                inflation = float(cpi.iloc[-1])

        # 象限
        if growth is None or inflation is None:
            return "GG"

        growth_high = growth > 2.5
        inflation_high = inflation > 2.5

        if growth_high and not inflation_high:
            return "GG"  # 高增长低通胀
        elif growth_high and inflation_high:
            return "GI"  # 高增长高通胀
        elif not growth_high and not inflation_high:
            return "IG"  # 低增长低通胀
        else:
            return "II"  # 低增长高通胀 (滞胀)

    def fx_rate(self, currency: str, asof: date) -> float:
        """对 CNY 汇率。CNY = 1.0。"""
        if currency == "CNY":
            return 1.0
        if self._fx.empty:
            # 默认汇率
            defaults = {"USD": 7.2, "HKD": 0.92}
            return defaults.get(currency, 1.0)
        subset = self._fx[
            (self._fx["currency"] == currency) &
            (self._fx["date"] <= pd.Timestamp(asof))
        ]
        if subset.empty:
            defaults = {"USD": 7.2, "HKD": 0.92}
            return defaults.get(currency, 1.0)
        return float(subset.iloc[-1]["rate_to_cny"])

    def momentum(self, market: str, asof: date) -> float:
        """标准化动量分数 (横截面 z-score, clip [-1, +1])。"""
        if self._momentum_df is None or self._momentum_df.empty:
            return 0.0
        subset = self._momentum_df[
            (self._momentum_df["market"] == market) &
            (self._momentum_df["date"] <= pd.Timestamp(asof))
        ]
        if subset.empty:
            return 0.0
        return float(subset.iloc[-1]["momentum"])

    def get_available_dates(self) -> list[pd.Timestamp]:
        """获取所有可用的月末日期。"""
        if self._etf_returns.empty:
            return []
        dates = sorted(self._etf_returns["date"].unique())
        return dates
