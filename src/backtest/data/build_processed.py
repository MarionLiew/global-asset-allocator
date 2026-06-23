"""
数据清洗脚本 — 从 raw/ 清洗成 processed/ parquet。

统一列名、日期格式、计算衍生指标 (CAPE 目标值、实际利率、EWMA 波动率等)。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from ._constants import EQUITY_MARKETS, MOMENTUM_LOOKBACK, MOMENTUM_SKIP

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent.parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"
PROC_DIR = BASE_DIR / "data" / "processed"


def build_etf_returns(save: bool = True) -> pd.DataFrame:
    """清洗 ETF 回报 → 月频回报矩阵。"""
    proc = PROC_DIR
    raw = RAW_DIR / "etf" / "all_etf_returns.csv"
    if not raw.exists():
        logger.warning(f"ETF 回报文件不存在: {raw}")
        return pd.DataFrame()

    df = pd.read_csv(raw, parse_dates=["date"])
    df = df.sort_values(["asset_id", "date"]).reset_index(drop=True)

    # 月末对齐
    df["date"] = df["date"].dt.to_period("M").dt.to_timestamp("M")

    if save:
        proc.mkdir(parents=True, exist_ok=True)
        df.to_parquet(proc / "etf_returns.parquet", index=False)
        logger.info(f"✅ ETF 回报: {len(df)} 行, 保存到 processed/etf_returns.parquet")

    return df


def build_cape_series(save: bool = True) -> pd.DataFrame:
    """清洗 CAPE 数据 → 各市场 CAPE 序列。"""
    raw_path = RAW_DIR / "cape" / "shiller_cape.csv"
    if not raw_path.exists():
        logger.warning(f"CAPE 文件不存在: {raw_path}")
        return pd.DataFrame()

    df = pd.read_csv(raw_path, parse_dates=["date"])
    df["date"] = df["date"].dt.to_period("M").dt.to_timestamp("M")
    df = df.sort_values("date").reset_index(drop=True)

    # 添加 CAPE 目标值 (滚动中位, 默认 10 年窗口)
    window = 120  # 月
    df["cape_target"] = df.groupby("market")["cape"].transform(
        lambda x: x.rolling(window, min_periods=60).median()
    )

    if save:
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(PROC_DIR / "cape_series.parquet", index=False)
        logger.info(f"✅ CAPE 序列: {len(df)} 行")

    return df


def build_fx_rates(save: bool = True) -> pd.DataFrame:
    """清洗 FX 数据 → 对 CNY 汇率。"""
    raw_path = RAW_DIR / "fx" / "fx_rates.csv"
    if not raw_path.exists():
        logger.warning(f"FX 文件不存在: {raw_path}")
        return pd.DataFrame()

    df = pd.read_csv(raw_path, parse_dates=["date"])
    df["date"] = df["date"].dt.to_period("M").dt.to_timestamp("M")

    # pivot
    rates = df.pivot_table(index="date", columns="pair", values="rate", aggfunc="last")
    rates = rates.sort_index().ffill()

    # 构建各货币对 CNY 的汇率
    out_rows = []
    for dt, row in rates.iterrows():
        usdcny = row.get("USDCNY", None)
        hkdusd = row.get("HKDUSD", None)

        if pd.notna(usdcny):
            out_rows.append({"date": dt, "currency": "USD", "rate_to_cny": usdcny})
        if pd.notna(usdcny) and pd.notna(hkdusd):
            # HKD/CNY = USD/CNY * HKD/USD
            hkdcny = usdcny * hkdusd
            out_rows.append({"date": dt, "currency": "HKD", "rate_to_cny": hkdcny})
        out_rows.append({"date": dt, "currency": "CNY", "rate_to_cny": 1.0})

    out = pd.DataFrame(out_rows)
    if save:
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        out.to_parquet(PROC_DIR / "fx_rates.parquet", index=False)
        logger.info(f"✅ FX 汇率: {len(out)} 行")

    return out


def build_macro_data(save: bool = True) -> pd.DataFrame:
    """清洗宏观指标 → 实际利率 + 象限分类器输入。"""
    raw_path = RAW_DIR / "macro" / "macro_indicators.csv"
    if not raw_path.exists():
        logger.warning(f"宏观指标文件不存在: {raw_path}")
        return pd.DataFrame()

    df = pd.read_csv(raw_path, parse_dates=["date"])
    df["date"] = df["date"].dt.to_period("M").dt.to_timestamp("M")

    # pivot
    macro = df.pivot_table(index="date", columns="series", values="value", aggfunc="last")
    macro = macro.sort_index()

    # 计算实际利率: DGS10 - CPI YoY%
    if "DGS10" in macro.columns and "CPIAUCSL" in macro.columns:
        macro["cpi_yoy"] = macro["CPIAUCSL"].pct_change(12) * 100  # 百分比
        # 用 CPI YoY 的 3 个月发布滞后
        macro["cpi_yoy_lagged"] = macro["cpi_yoy"].shift(3)
        macro["real_yield"] = macro["DGS10"] - macro["cpi_yoy_lagged"].fillna(0)
        # 2003+ 用 TIPS 收益率替代
        if "DFII10" in macro.columns:
            macro.loc[macro["DFII10"].notna(), "real_yield"] = macro.loc[macro["DFII10"].notna(), "DFII10"]

    # 增长代理: INDPRO YoY%
    if "INDPRO" in macro.columns:
        macro["growth_yoy"] = macro["INDPRO"].pct_change(12) * 100
        macro["growth_yoy_lagged"] = macro["growth_yoy"].shift(3)

    if save:
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        macro.to_parquet(PROC_DIR / "macro_data.parquet")
        logger.info(f"✅ 宏观指标: {len(macro)} 行")

    return macro


def build_momentum(save: bool = True) -> pd.DataFrame:
    """从 ETF 回报计算 12-1 动量, 横截面 z-score, 输出 processed/momentum.parquet。

    动量 = cum_log_return[t-12 : t-2], 跳过最近 1 个月。
    每月跨所有股票市场做 z-score 标准化, clip 到 [-1, +1]。
    """
    raw = RAW_DIR / "etf" / "all_etf_returns.csv"
    if not raw.exists():
        logger.warning(f"ETF 回报文件不存在: {raw}, 跳过动量计算")
        return pd.DataFrame()

    df = pd.read_csv(raw, parse_dates=["date"])
    df["date"] = df["date"].dt.to_period("M").dt.to_timestamp("M")

    # 只取股票市场
    equity = df[df["asset_id"].isin(EQUITY_MARKETS)].copy()
    if equity.empty:
        logger.warning("无股票市场回报数据, 跳过动量")
        return pd.DataFrame()

    # pivot → (date, market) 回报矩阵
    ret_matrix = equity.pivot_table(
        index="date", columns="asset_id", values="return_m"
    ).sort_index()

    lookback = MOMENTUM_LOOKBACK
    skip = MOMENTUM_SKIP

    # 12-1 动量: 累积 log return [t-12, t-2]
    log_ret = np.log(1 + ret_matrix)
    # 先 rolling sum 全窗口, 再 shift 跳过最近 skip 个月
    mom_raw = log_ret.rolling(window=lookback, min_periods=lookback).sum().shift(skip)

    # 横截面 z-score
    mom_mean = mom_raw.mean(axis=1)
    mom_std = mom_raw.std(axis=1).replace(0, np.nan)
    mom_z = mom_raw.sub(mom_mean, axis=1).div(mom_std, axis=1)
    mom_z = mom_z.clip(-1, 1)

    # 转长格式
    mom_long = mom_z.stack().reset_index()
    mom_long.columns = ["date", "market", "momentum"]
    mom_long = mom_long.dropna(subset=["momentum"])
    mom_long = mom_long.sort_values(["market", "date"]).reset_index(drop=True)

    if save:
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        mom_long.to_parquet(PROC_DIR / "momentum.parquet", index=False)
        n_markets = mom_long["market"].nunique()
        logger.info(f"✅ 动量: {len(mom_long)} 行, {n_markets} 市场")

    return mom_long


def build_all():
    """构建所有 processed 数据。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("=== 构建 processed 数据 ===")
    build_etf_returns()
    build_cape_series()
    build_fx_rates()
    build_macro_data()
    build_momentum()
    logger.info("\n✅ 所有 processed 数据构建完成!")


if __name__ == "__main__":
    build_all()
