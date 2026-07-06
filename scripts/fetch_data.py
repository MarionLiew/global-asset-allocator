#!/usr/bin/env python3
"""
数据抓取入口 — 下载所有 raw 数据并构建 processed/ parquet。

策略:
  1. yfinance 下载 US/DM/HK ETF + 防御资产 ETF
  2. CN/CN_GOVT 用 data/raw/etf/ 现有 CSV (yfinance 沪深 ETF 历史太短)
  3. FX: yfinance USDCNY=X / HKDUSD=X
  4. 宏观: FRED DGS10 / DFII10 (补 CPI/INDPRO 已有)
  5. 合并 → data/raw/etf/all_etf_returns.csv → build_processed

用法:
    python scripts/fetch_data.py            # 完整下载 + 构建
    python scripts/fetch_data.py --skip-download   # 仅构建 (数据已下载)
    python scripts/fetch_data.py --synthetic       # 用合成数据 (离线/测试)
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROC_DIR = PROJECT_ROOT / "data" / "processed"

PROXY = "http://127.0.0.1:1082"
PROXIES = {"http": PROXY, "https": PROXY}


# ─── 依赖检查 ─────────────────────────────────────────────────────────────────

def _ensure_deps():
    needed = []
    try:
        import yfinance  # noqa: F401
    except ImportError:
        needed.append("yfinance")
    try:
        import requests  # noqa: F401
    except ImportError:
        needed.append("requests")
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        needed.append("pyarrow")
    if needed:
        logger.info(f"安装依赖: {' '.join(needed)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q"] + needed)


# ─── ETF 下载 ─────────────────────────────────────────────────────────────────

# US/DM/HK 及防御资产 — yfinance 历史较完整, 每次刷新到最新
YFINANCE_TICKERS = {
    "US":        "SPY",
    "DM":        "EFA",
    "HK":        "2800.HK",
    "TIPS":      "TIP",
    "GOLD":      "GLD",
    "CORP_BOND": "LQD",
    "EM_BOND":   "EMB",
}

# CN/CN_GOVT — 用本地 CSV (已通过 fetch_proxy_data.py 拼接长历史+近期数据, 不要在此覆盖)
LOCAL_ETFS = ["CN", "CN_GOVT"]


def _set_yf_proxy():
    """给 yfinance 设置代理。"""
    import os
    os.environ.setdefault("HTTP_PROXY",  PROXY)
    os.environ.setdefault("HTTPS_PROXY", PROXY)


def download_etfs(start: str = "1993-01-01", end: str = "2025-01-01") -> None:
    """下载 yfinance ETF，本地 CSV 跳过。"""
    import pandas as pd
    import yfinance as yf
    _set_yf_proxy()

    out_dir = RAW_DIR / "etf"
    out_dir.mkdir(parents=True, exist_ok=True)

    for asset_id, ticker in YFINANCE_TICKERS.items():
        out_file = out_dir / f"{asset_id}.csv"

        logger.info(f"  下载 {asset_id} ({ticker})...")
        try:
            time.sleep(2)
            df = yf.download(ticker, start=start, end=end, interval="1mo",
                             auto_adjust=True, progress=False)
            if df.empty:
                logger.warning(f"    {ticker}: 无数据")
                continue

            close = df["Close"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
            close = close.resample("ME").last().dropna()
            ret = close.pct_change().dropna()

            out = pd.DataFrame({
                "date":     ret.index,
                "close":    close.iloc[1:].values,
                "return_m": ret.values,
                "asset_id": asset_id,
            })
            out.to_csv(out_file, index=False)
            logger.info(f"    {asset_id}: {len(out)} 月 ({out['date'].min().date()} ~ {out['date'].max().date()})")
        except Exception as e:
            logger.warning(f"    {ticker}: 下载失败 — {e}")


def build_all_etf_returns() -> None:
    """合并所有 ETF CSV → all_etf_returns.csv。"""
    import pandas as pd

    out_dir = RAW_DIR / "etf"
    all_assets = list(YFINANCE_TICKERS.keys()) + LOCAL_ETFS
    frames = []

    for asset_id in all_assets:
        p = out_dir / f"{asset_id}.csv"
        if not p.exists():
            logger.warning(f"  缺少 {asset_id}.csv, 跳过")
            continue
        df = pd.read_csv(p, parse_dates=["date"])
        if "asset_id" not in df.columns:
            df["asset_id"] = asset_id
        frames.append(df[["date", "asset_id", "return_m"]])

    if not frames:
        logger.error("没有可用的 ETF 数据!")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"]).dt.to_period("M").dt.to_timestamp("M")
    combined = combined.sort_values(["asset_id", "date"]).reset_index(drop=True)

    out_path = out_dir / "all_etf_returns.csv"
    combined.to_csv(out_path, index=False)

    # 摘要
    summary = combined.groupby("asset_id")["date"].agg(["min", "max", "count"])
    logger.info(f"\n  all_etf_returns.csv: {len(combined)} 行, {combined['asset_id'].nunique()} 资产")
    for asset, row in summary.iterrows():
        logger.info(f"    {asset:12s}: {row['count']:3d} 月  {row['min'].date()} ~ {row['max'].date()}")


# ─── FX 下载 ──────────────────────────────────────────────────────────────────

def download_fx(start: str = "1993-01-01", end: str = "2025-01-01") -> None:
    """下载 USD/CNY 和 HKD/USD 汇率。"""
    import pandas as pd
    import yfinance as yf
    _set_yf_proxy()

    out_dir = RAW_DIR / "fx"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "fx_rates.csv"

    pairs = {"USDCNY": "USDCNY=X", "HKDUSD": "HKDUSD=X"}
    frames = []

    for name, ticker in pairs.items():
        logger.info(f"  下载汇率 {name} ({ticker})...")
        try:
            time.sleep(2)
            df = yf.download(ticker, start=start, end=end, interval="1mo", progress=False)
            if df.empty:
                logger.warning(f"    {name}: 无数据")
                continue
            close = df["Close"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
            close = close.resample("ME").last().dropna()
            frames.append(pd.DataFrame({"date": close.index, "pair": name, "rate": close.values}))
            logger.info(f"    {name}: {len(close)} 月")
        except Exception as e:
            logger.warning(f"    {name}: 下载失败 — {e}")

    if frames:
        pd.concat(frames, ignore_index=True).to_csv(out_path, index=False)
    else:
        logger.warning("  FX 数据全部下载失败，将使用默认汇率")


# ─── 宏观补充 (DGS10 / DFII10) ───────────────────────────────────────────────

def download_fred_yields() -> None:
    """下载 DGS10 / DFII10 利率 (yfinance 代替 FRED)。

    yfinance tickers:
      ^TNX  = 10Y Treasury 收益率 (≈ DGS10, 单位 %)
      ^TYX  = 30Y Treasury (备用)
    TIPS 收益率无直接 yfinance 代理, 暂不填充 (build_processed 里用 CPI 代理)。
    """
    import pandas as pd
    import yfinance as yf
    _set_yf_proxy()

    macro_path = RAW_DIR / "macro" / "macro_indicators.csv"
    if not macro_path.exists():
        logger.warning("  macro_indicators.csv 不存在，跳过利率下载")
        return

    existing = pd.read_csv(macro_path)
    existing_series = set(existing["series"].unique())

    frames = [existing]

    if "DGS10" not in existing_series:
        logger.info("  下载 ^TNX (10Y 国债收益率) via yfinance...")
        try:
            time.sleep(2)
            df = yf.download("^TNX", start="1993-01-01", end="2025-01-01",
                             interval="1mo", auto_adjust=True, progress=False)
            close = df["Close"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
            close = close.resample("ME").last().dropna()
            out = pd.DataFrame({
                "date":   close.index.strftime("%Y-%m-%d"),
                "value":  close.values,
                "series": "DGS10",
            })
            frames.append(out)
            logger.info(f"    DGS10 (^TNX): {len(out)} 月")
        except Exception as e:
            logger.warning(f"    ^TNX: 下载失败 — {e}")
    else:
        logger.info("  DGS10 已存在，跳过")

    combined = pd.concat(frames, ignore_index=True)
    combined.to_csv(macro_path, index=False)
    logger.info(f"  macro_indicators.csv 更新: {combined['series'].nunique()} 个指标")


# ─── 合成模式 ─────────────────────────────────────────────────────────────────

def build_synthetic() -> None:
    """用合成数据构建 processed/（离线/测试用）。"""
    from backtest.data.generate_synthetic import build_all_synthetic
    logger.info("\n=== 合成模式: 构建合成 processed 数据 ===")
    build_all_synthetic()


# ─── build_processed ─────────────────────────────────────────────────────────

def build_processed() -> None:
    """从 raw/ 构建 processed/ parquet。"""
    from backtest.data.build_processed import build_all
    logger.info("\n=== 构建 processed/ parquet ===")
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    build_all()


# ─── 主入口 ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="数据抓取 + 预处理")
    parser.add_argument("--skip-download", action="store_true", help="跳过下载，直接构建 processed/")
    parser.add_argument("--synthetic",     action="store_true", help="使用合成数据（无需网络）")
    import datetime
    parser.add_argument("--start",  default="1993-01-01", help="数据起始日期")
    parser.add_argument("--end",    default=datetime.date.today().isoformat(), help="数据结束日期 (默认今天)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.synthetic:
        _ensure_deps()
        build_synthetic()
        return

    if not args.skip_download:
        _ensure_deps()

        logger.info("\n=== 1/4  ETF 价格 (yfinance) ===")
        download_etfs(args.start, args.end)

        logger.info("\n=== 2/4  合并 all_etf_returns.csv ===")
        build_all_etf_returns()

        logger.info("\n=== 3/4  FX 汇率 (yfinance) ===")
        download_fx(args.start, args.end)

        logger.info("\n=== 4/4  FRED 利率补充 ===")
        download_fred_yields()

    logger.info("\n=== 构建 processed/ parquet ===")
    build_processed()

    logger.info("\n✅ 完成! 可以运行回测:")
    logger.info("   python scripts/run_backtest.py")


if __name__ == "__main__":
    main()
