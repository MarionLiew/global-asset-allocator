"""
合成数据生成器 — 基于真实统计特征生成模拟数据。

用于当 yfinance 被 rate limit 时。CAPE 数据是真实的 (Shiller)，
ETF 回报基于各资产类别的历史统计特征生成。

数据质量说明:
- US CAPE: 真实 (Shiller 1871+)
- 宏观指标 (CPI, INDPRO): 真实 (FRED)
- ETF 回报: 合成 (基于历史均值/波动率/相关性)
- FX: 合成 (基于历史均值/波动率)
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from ._constants import EQUITY_MARKETS, MOMENTUM_LOOKBACK, MOMENTUM_SKIP

logger = logging.getLogger(__name__)

PROC_DIR = Path(__file__).parent.parent.parent.parent / "data" / "processed"
RAW_DIR = Path(__file__).parent.parent.parent.parent / "data" / "raw"

# 各资产的年化统计特征 (基于 1995-2024 历史)
ASSET_STATS = {
    # 资产ID: (年化均值, 年化波动率, 偏度)
    "US":        (0.10, 0.16, -0.5),   # 美国大盘
    "DM":        (0.07, 0.18, -0.5),   # 发达市场 (除美国)
    "CN":        (0.08, 0.25, 0.0),    # 中国大盘
    "HK":        (0.07, 0.22, -0.3),   # 香港大盘
    "CN_GOVT":   (0.035, 0.05, 0.3),  # 中国国债
    "TIPS":      (0.04, 0.06, 0.0),   # 美国 TIPS
    "GOLD":      (0.06, 0.15, 0.5),   # 黄金
    "CORP_BOND": (0.05, 0.08, -0.2),  # 公司债
    "EM_BOND":   (0.06, 0.10, -0.3),  # 新兴市场债
}

# 相关性矩阵 (近似)
CORRELATIONS = {
    ("US", "DM"): 0.85,
    ("US", "CN"): 0.30,
    ("US", "HK"): 0.50,
    ("US", "GOLD"): -0.10,
    ("US", "CN_GOVT"): -0.15,
    ("US", "TIPS"): 0.20,
    ("US", "CORP_BOND"): 0.40,
    ("US", "EM_BOND"): 0.50,
    ("DM", "CN"): 0.25,
    ("DM", "HK"): 0.55,
    ("CN", "HK"): 0.60,
    ("GOLD", "CN_GOVT"): 0.20,
    ("GOLD", "TIPS"): 0.30,
    ("CN_GOVT", "TIPS"): 0.10,
}


def generate_monthly_returns(
    dates: list[pd.Timestamp],
    seed: int = 42,
) -> pd.DataFrame:
    """生成所有资产的月度回报序列。

    使用 Cholesky 分解生成相关回报。
    """
    np.random.seed(seed)
    n = len(dates)
    assets = list(ASSET_STATS.keys())

    # 构建相关性矩阵
    n_assets = len(assets)
    corr_matrix = np.eye(n_assets)
    for i, a1 in enumerate(assets):
        for j, a2 in enumerate(assets):
            if i != j:
                key = (a1, a2) if (a1, a2) in CORRELATIONS else (a2, a1)
                corr_matrix[i, j] = CORRELATIONS.get(key, 0.2)

    # 确保正定
    eigvals = np.linalg.eigvalsh(corr_matrix)
    if eigvals.min() < 0.01:
        corr_matrix += np.eye(n_assets) * (0.01 - eigvals.min())

    # Cholesky
    try:
        L = np.linalg.cholesky(corr_matrix)
    except np.linalg.LinAlgError:
        L = np.eye(n_assets)

    # 生成独立标准正态
    Z = np.random.randn(n, n_assets)

    # 相关化
    correlated = Z @ L.T

    # 转换为各资产的回报
    rows = []
    for i_date, dt in enumerate(dates):
        for i_asset, asset in enumerate(assets):
            ann_mean, ann_vol, skew = ASSET_STATS[asset]
            # 月度参数
            mu_m = ann_mean / 12
            sigma_m = ann_vol / np.sqrt(12)

            # 基本正态回报
            z = correlated[i_date, i_asset]
            ret = mu_m + sigma_m * z

            # 偏度调整 (简化)
            if skew < 0 and z < -1.5:
                ret *= 1.3  # 左尾加厚
            elif skew > 0 and z > 1.5:
                ret *= 1.2  # 右尾加厚

            rows.append({
                "date": dt,
                "asset_id": asset,
                "return_m": ret,
            })

    return pd.DataFrame(rows)


def build_synthetic_etf_returns(save: bool = True) -> pd.DataFrame:
    """生成 ETF 回报。"""
    # 用 CAPE 数据的日期范围
    cape_path = RAW_DIR / "cape" / "shiller_cape.csv"
    if cape_path.exists():
        cape = pd.read_csv(cape_path, parse_dates=["date"])
        cape["date"] = cape["date"].dt.to_period("M").dt.to_timestamp("M")
        dates = sorted(cape["date"].unique())
        # 只取 1995+
        dates = [d for d in dates if d >= pd.Timestamp("1995-01-31")]
    else:
        dates = pd.date_range("1995-01-31", "2024-12-31", freq="ME").tolist()

    logger.info(f"生成合成 ETF 回报: {len(dates)} 月, {len(ASSET_STATS)} 资产")

    df = generate_monthly_returns(dates)

    if save:
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(PROC_DIR / "etf_returns.parquet", index=False)
        logger.info(f"✅ 合成 ETF 回报已保存: {len(df)} 行")

    return df


def build_synthetic_fx(dates: list[pd.Timestamp] | None = None,
                       save: bool = True) -> pd.DataFrame:
    """生成合成 FX 汇率。"""
    if dates is None:
        dates = pd.date_range("1995-01-31", "2024-12-31", freq="ME").tolist()

    np.random.seed(123)
    n = len(dates)

    rows = []
    # USD/CNY: 从 ~8.3 逐步到 ~7.2 (含 2005 汇改)
    usdcny = 8.3
    for i, dt in enumerate(dates):
        # 2005 年前固定, 之后有波动
        if dt < pd.Timestamp("2005-07-31"):
            usdcny = 8.28 + np.random.normal(0, 0.01)
        else:
            usdcny *= (1 + np.random.normal(-0.0005, 0.005))
            usdcny = max(6.0, min(usdcny, 8.5))

        hkdusd = 7.78 / 100 + np.random.normal(0, 0.0001)  # HKD/USD ≈ 0.0778
        hkdcny = usdcny * hkdusd

        rows.append({"date": dt, "currency": "USD", "rate_to_cny": usdcny})
        rows.append({"date": dt, "currency": "HKD", "rate_to_cny": hkdcny})
        rows.append({"date": dt, "currency": "CNY", "rate_to_cny": 1.0})

    df = pd.DataFrame(rows)

    if save:
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(PROC_DIR / "fx_rates.parquet", index=False)
        logger.info(f"✅ 合成 FX 汇率已保存: {len(df)} 行")

    return df


def build_cape_from_shiller(save: bool = True) -> pd.DataFrame:
    """从 Shiller 数据构建 CAPE 序列。"""
    cape_path = RAW_DIR / "cape" / "shiller_cape.csv"
    if not cape_path.exists():
        logger.warning("Shiller CAPE 数据不存在")
        return pd.DataFrame()

    df = pd.read_csv(cape_path, parse_dates=["date"])
    df["date"] = df["date"].dt.to_period("M").dt.to_timestamp("M")
    df = df.sort_values("date").reset_index(drop=True)

    # CAPE 目标值 (滚动 10 年中位)
    df["cape_target"] = df["cape"].rolling(120, min_periods=60).median()

    if save:
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(PROC_DIR / "cape_series.parquet", index=False)
        logger.info(f"✅ CAPE 序列已保存: {len(df)} 行")

    return df


def build_macro_from_fred(save: bool = True) -> pd.DataFrame:
    """从 FRED 数据构建宏观指标。"""
    macro_path = RAW_DIR / "macro" / "macro_indicators.csv"
    if not macro_path.exists():
        logger.warning("宏观指标文件不存在")
        return pd.DataFrame()

    df = pd.read_csv(macro_path, parse_dates=["date"])
    df["date"] = df["date"].dt.to_period("M").dt.to_timestamp("M")

    macro = df.pivot_table(index="date", columns="series", values="value", aggfunc="last")
    macro = macro.sort_index()

    # 计算实际利率
    if "CPIAUCSL" in macro.columns:
        macro["cpi_yoy"] = macro["CPIAUCSL"].pct_change(12) * 100
        macro["cpi_yoy_lagged"] = macro["cpi_yoy"].shift(3)

    if "INDPRO" in macro.columns:
        macro["growth_yoy"] = macro["INDPRO"].pct_change(12) * 100
        macro["growth_yoy_lagged"] = macro["growth_yoy"].shift(3)

    # 实际利率: 用 CPI 代理 (因 DGS10 下载失败)
    if "cpi_yoy_lagged" in macro.columns:
        # 粗略: 假设名义利率 = CPI + 2% 实际
        macro["real_yield"] = 0.02  # 2% 近似

    if save:
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        macro.to_parquet(PROC_DIR / "macro_data.parquet")
        logger.info(f"✅ 宏观指标已保存: {len(macro)} 行")

    return macro


def build_synthetic_momentum(save: bool = True) -> pd.DataFrame:
    """从合成 ETF 回报计算 12-1 动量, 输出 momentum.parquet。

    逻辑与 build_processed.build_momentum 相同, 但直接从已生成的 parquet 读取。
    """
    proc = PROC_DIR
    etf_path = proc / "etf_returns.parquet"
    if not etf_path.exists():
        logger.warning("etf_returns.parquet 不存在, 跳过动量")
        return pd.DataFrame()

    df = pd.read_parquet(etf_path)
    equity = df[df["asset_id"].isin(EQUITY_MARKETS)].copy()
    if equity.empty:
        return pd.DataFrame()

    ret_matrix = equity.pivot_table(
        index="date", columns="asset_id", values="return_m"
    ).sort_index()

    lookback = MOMENTUM_LOOKBACK
    skip = MOMENTUM_SKIP

    log_ret = np.log(1 + ret_matrix)
    mom_raw = log_ret.rolling(window=lookback, min_periods=lookback).sum().shift(skip)

    # 横截面 z-score
    mom_mean = mom_raw.mean(axis=1)
    mom_std = mom_raw.std(axis=1).replace(0, np.nan)
    mom_z = mom_raw.sub(mom_mean, axis=0).div(mom_std, axis=0)
    mom_z = mom_z.clip(-1, 1)

    mom_long = mom_z.stack().reset_index()
    mom_long.columns = ["date", "market", "momentum"]
    mom_long = mom_long.dropna(subset=["momentum"])
    mom_long = mom_long.sort_values(["market", "date"]).reset_index(drop=True)

    if save:
        proc.mkdir(parents=True, exist_ok=True)
        mom_long.to_parquet(proc / "momentum.parquet", index=False)
        logger.info(f"✅ 合成动量: {len(mom_long)} 行")

    return mom_long


def build_all_synthetic():
    """构建所有合成数据。"""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    logger.info("=== 构建合成数据 ===")

    # 1. CAPE (真实)
    build_cape_from_shiller()

    # 2. ETF 回报 (合成)
    build_synthetic_etf_returns()

    # 3. FX (合成)
    build_synthetic_fx()

    # 4. 宏观指标 (真实 CPI/INDPRO)
    build_macro_from_fred()

    # 5. 动量 (从合成回报计算)
    build_synthetic_momentum()

    logger.info("\n✅ 所有数据构建完成!")


if __name__ == "__main__":
    build_all_synthetic()
