"""
数据下载脚本 — 从公开源获取真实历史数据。

数据源:
- ETF 回报: yfinance (SPY, EFA, GLD, TIP, LQD, EMB 等)
- US CAPE: Shiller (Yale) 公开数据
- FX: yfinance (USDCNY=X, HKDCNY=X)
- 实际利率: 10Y Treasury - CPI YoY (近似)
- 增长/通胀: FRED 公开数据 (用于象限分类器)

输出: data/raw/ 下的 CSV 文件。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

RAW_DIR = Path(__file__).parent.parent.parent.parent / "data" / "raw"


def download_etf_returns(tickers: dict[str, str], start: str = "1990-01-01",
                         end: str = "2025-01-01", save_dir: Path | None = None) -> pd.DataFrame:
    """下载 ETF 月度总回报 (含分红再投资)。"""
    import yfinance as yf

    save_dir = save_dir or RAW_DIR / "etf"
    save_dir.mkdir(parents=True, exist_ok=True)

    all_data = []
    for asset_id, ticker in tickers.items():
        logger.info(f"下载 {asset_id} ({ticker})...")
        try:
            time.sleep(3)  # 避免 rate limit
            df = yf.download(ticker, start=start, end=end, interval="1mo",
                             auto_adjust=True, progress=False)
            if df.empty:
                logger.warning(f"  {ticker}: 无数据, 跳过")
                continue

            # 取月末收盘价
            if isinstance(df.columns, pd.MultiIndex):
                close = df["Close"].iloc[:, 0]
            else:
                close = df["Close"]

            close = close.resample("ME").last().dropna()
            ret = close.pct_change().dropna()

            out = pd.DataFrame({
                "date": ret.index,
                "asset_id": asset_id,
                "ticker": ticker,
                "close": close.iloc[1:].values if len(close) > 1 else [],
                "return_m": ret.values,
            })
            all_data.append(out)
            out.to_csv(save_dir / f"{asset_id}.csv", index=False)
            logger.info(f"  {asset_id}: {len(out)} 月 ({out['date'].min().date()} ~ {out['date'].max().date()})")

        except Exception as e:
            logger.warning(f"  {ticker}: 下载失败 - {e}")

    if not all_data:
        return pd.DataFrame()

    result = pd.concat(all_data, ignore_index=True)
    result.to_csv(save_dir / "all_etf_returns.csv", index=False)
    return result


def download_fx_rates(start: str = "1990-01-01", end: str = "2025-01-01",
                      save_dir: Path | None = None) -> pd.DataFrame:
    """下载 FX 月度汇率 (对 USD)。"""
    import yfinance as yf

    save_dir = save_dir or RAW_DIR / "fx"
    save_dir.mkdir(parents=True, exist_ok=True)

    pairs = {
        "USDCNY": "USDCNY=X",
        "HKDUSD": "HKDUSD=X",
    }

    all_data = []
    for name, ticker in pairs.items():
        logger.info(f"下载汇率 {name} ({ticker})...")
        try:
            time.sleep(3)
            df = yf.download(ticker, start=start, end=end, interval="1mo",
                             progress=False)
            if df.empty:
                logger.warning(f"  {name}: 无数据")
                continue

            if isinstance(df.columns, pd.MultiIndex):
                close = df["Close"].iloc[:, 0]
            else:
                close = df["Close"]

            close = close.resample("ME").last().dropna()
            out = pd.DataFrame({"date": close.index, "pair": name, "rate": close.values})
            all_data.append(out)
            logger.info(f"  {name}: {len(out)} 月")
        except Exception as e:
            logger.warning(f"  {name}: 下载失败 - {e}")

    if not all_data:
        return pd.DataFrame()

    result = pd.concat(all_data, ignore_index=True)
    result.to_csv(save_dir / "fx_rates.csv", index=False)
    return result


def download_shiller_cape(save_dir: Path | None = None) -> pd.DataFrame:
    """下载 Shiller CAPE 数据 (1871+)。"""
    save_dir = save_dir or RAW_DIR / "cape"
    save_dir.mkdir(parents=True, exist_ok=True)

    logger.info("下载 Shiller CAPE 数据...")

    try:
        import io
        import requests

        url = "http://www.econ.yale.edu/~shiller/data/ie_data.xls"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()

        df = pd.read_excel(io.BytesIO(resp.content), sheet_name="Data", skiprows=[0, 1, 2])

        # 找 CAPE 列
        cape_col = None
        for col in df.columns:
            if isinstance(col, str) and "cape" in col.lower():
                cape_col = col
                break
        if cape_col is None:
            cape_col = df.columns[10] if len(df.columns) > 10 else df.columns[-1]

        date_col = df.columns[0]
        df = df[[date_col, cape_col]].dropna()
        df.columns = ["date_raw", "cape"]

        def parse_shiller_date(d):
            try:
                d = float(d)
                year = int(d)
                month = int(round((d - year) * 100))
                if month < 1 or month > 12:
                    return None
                return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)
            except (ValueError, TypeError):
                return None

        df["date"] = df["date_raw"].apply(parse_shiller_date)
        df = df.dropna(subset=["date"])
        df["cape"] = pd.to_numeric(df["cape"], errors="coerce")
        df = df.dropna(subset=["cape"])
        df = df[df["cape"] > 0]

        result = df[["date", "cape"]].copy()
        result["market"] = "US"
        result.to_csv(save_dir / "shiller_cape.csv", index=False)
        logger.info(f"  Shiller CAPE: {len(result)} 月 ({result['date'].min().date()} ~ {result['date'].max().date()})")
        return result

    except Exception as e:
        logger.warning(f"  Shiller 下载失败: {e}")
        return pd.DataFrame()


def download_macro_indicators(save_dir: Path | None = None) -> pd.DataFrame:
    """下载增长/通胀指标 (用于象限分类器)。"""
    save_dir = save_dir or RAW_DIR / "macro"
    save_dir.mkdir(parents=True, exist_ok=True)

    fred_series = {
        "DGS10": "10Y Treasury 收益率",
        "DFII10": "TIPS 10Y 收益率",
        "CPIAUCSL": "CPI (All Urban)",
        "INDPRO": "工业生产指数",
    }

    all_data = []
    for series_id, desc in fred_series.items():
        logger.info(f"下载 FRED {series_id} ({desc})...")
        try:
            time.sleep(2)
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
            df = pd.read_csv(url)
            # FRED CSV 格式: DATE, VALUE
            if len(df.columns) >= 2:
                df.columns = ["date", "value"] if len(df.columns) == 2 else df.columns[:2]
                df = df.rename(columns={df.columns[0]: "date", df.columns[1]: "value"})
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            df = df.dropna(subset=["date", "value"])
            df["series"] = series_id
            all_data.append(df)
            df.to_csv(save_dir / f"{series_id}.csv", index=False)
            logger.info(f"  {series_id}: {len(df)} 观测值")
        except Exception as e:
            logger.warning(f"  {series_id}: 下载失败 - {e}")

    if not all_data:
        return pd.DataFrame()

    result = pd.concat(all_data, ignore_index=True)
    result.to_csv(save_dir / "macro_indicators.csv", index=False)
    return result


def download_all(data_dir: Path | None = None):
    """下载所有数据。"""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    base = data_dir or RAW_DIR

    # 1. Shiller CAPE (最可靠)
    download_shiller_cape()

    # 2. ETF 回报 (yfinance, 需要间隔)
    from ._constants import ETF_TICKERS
    download_etf_returns(ETF_TICKERS)

    # 3. FX 汇率
    download_fx_rates()

    # 4. 宏观指标
    download_macro_indicators()

    logger.info("\n✅ 所有数据下载完成! 文件在 data/raw/")


if __name__ == "__main__":
    download_all()
