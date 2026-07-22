#!/usr/bin/env python3
"""
代理数据拼接脚本 — 为历史缺口补充代理 ETF/基金，扩展回测起始日期。

拼接策略 (优先级从高到低，越后越老):
  DM        : EFA (2001-09+) ← FDIVX/Fidelity Intl (1991-12+)
  HK        : 2800.HK (2008-02+) ← EWH/iShares MSCI HK (1996-03+)
  CN        : 510300 (2012-06+) ← 000001.SS/上证综指 (1997-07+)
  CN_GOVT   : 511260 (2017-09+) ← CBON/VanEck ChinaBond (2014-11+) ← 合成3.5%/yr
  TIPS      : TIP (2004-01+) ← VIPSX/Vanguard TIPS (2000-06+) ← 合成(国债-通胀)
  GOLD      : GLD (2004-12+) ← GC=F/COMEX期货 (2000-08+) ← 合成金价
  CORP_BOND : LQD (2002-08+) ← VWESX/Vanguard LT Corp (1990-01+)
  EM_BOND   : EMB (2008-01+) ← PCY/Invesco EM Sovereign (2007-10+)
                             ← VWEHX/Vanguard HY Corp (1990-01+)

用法:
  python scripts/fetch_proxy_data.py
  python scripts/fetch_proxy_data.py --start 1993-01-01 --end 2025-01-01
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
RAW_DIR = PROJECT_ROOT / "data" / "raw"

os.environ.setdefault("HTTP_PROXY",  "http://127.0.0.1:1082")
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:1082")


# ─── 代理配置 ─────────────────────────────────────────────────────────────────

# 每个资产: [(ticker, label), ...] 按优先级从高(最新最准)到低(最老代理)排列
PROXY_LAYERS: dict[str, list[tuple[str, str]]] = {
    "DM":        [("EFA",        "EFA(主)"),
                  ("FDIVX",      "FDIVX(Fidelity Intl)")],
    "HK":        [("2800.HK",    "2800.HK(主)"),
                  ("EWH",        "EWH(iShares MSCI HK)")],
    "CN":        [("510300.SS",  "510300(主)"),
                  ("000001.SS",  "000001(上证综指)")],
    "CN_GOVT":   [("511260.SS",  "511260(主)"),
                  ("CBON",       "CBON(VanEck ChinaBond)"),
                  ("__SYNTHETIC_CNGOV__", "合成(3.5%/yr)")],
    "TIPS":      [("TIP",        "TIP(主)"),
                  ("VIPSX",      "VIPSX(Vanguard TIPS)")],
    "GOLD":      [("GLD",        "GLD(主)"),
                  ("GC=F",       "GC=F(COMEX黄金期货)")],
    "CORP_BOND": [("LQD",        "LQD(主)"),
                  ("VWESX",      "VWESX(Vanguard LT Corp)")],
    "EM_BOND":   [("EMB",        "EMB(主)"),
                  ("PCY",        "PCY(Invesco EM Sovereign)"),
                  ("VWEHX",      "VWEHX(Vanguard HY Corp)")],
    "US":        [("SPY",        "SPY(主)")],   # SPY 已有完整历史，无需代理
}


# ─── 下载单个 ticker ──────────────────────────────────────────────────────────

def _download_ticker(ticker: str, start: str, end: str) -> pd.Series:
    """返回月度回报 Series，index=月末日期。"""
    import yfinance as yf
    from backtest.data.monthly import monthly_returns_from_daily_close
    time.sleep(1.5)
    df = yf.download(ticker, start=start, end=end, interval="1d",
                     auto_adjust=True, progress=False)
    if df.empty:
        return pd.Series(dtype=float)
    close = df["Close"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
    return monthly_returns_from_daily_close(close)["return_m"]


# ─── 合成序列 ─────────────────────────────────────────────────────────────────

def _synthetic_cn_govt(dates: pd.DatetimeIndex, seed: int = 77) -> pd.Series:
    """中国国债合成回报：3.5%/yr + ~3% vol，轻微负偏（利率上行时跌）。"""
    np.random.seed(seed)
    n = len(dates)
    mu = 0.035 / 12
    sigma = 0.03 / np.sqrt(12)
    rets = np.random.normal(mu, sigma, n)
    return pd.Series(rets, index=dates, name="return_m")


# ─── 拼接逻辑 ─────────────────────────────────────────────────────────────────

def splice_asset(asset_id: str, start: str, end: str) -> pd.Series:
    """
    下载所有代理层，按优先级拼接：
    - 最高优先级数据有的月份用最高优先级
    - 只有在最高优先级缺失时才向下取代理
    """
    layers: list[tuple[str, pd.Series]] = []

    for ticker, label in PROXY_LAYERS.get(asset_id, []):
        if ticker == "__SYNTHETIC_CNGOV__":
            # 合成数据：填满整个日期范围
            dates = pd.date_range(start, end, freq="ME")
            s = _synthetic_cn_govt(dates)
            layers.append((label, s))
            logger.info(f"    合成 CN_GOVT: {len(s)} 月")
        else:
            # 先检查本地已有 CSV
            local = RAW_DIR / "etf" / f"{asset_id}.csv"
            if ticker == PROXY_LAYERS[asset_id][0][0] and local.exists():
                df = pd.read_csv(local, parse_dates=["date"])
                df["date"] = pd.to_datetime(df["date"]).dt.to_period("M").dt.to_timestamp("M")
                s = df.set_index("date")["return_m"].dropna()
                layers.append((label, s))
                logger.info(f"    {label}: {len(s)} 月 (本地CSV)")
                continue

            logger.info(f"    下载 {label} ({ticker})...")
            s = _download_ticker(ticker, start, end)
            if s.empty:
                logger.warning(f"    {label}: 无数据，跳过")
                continue
            layers.append((label, s))
            logger.info(f"    {label}: {s.index.min().strftime('%Y-%m')} ~ "
                        f"{s.index.max().strftime('%Y-%m')}  ({len(s)} 月)")

    if not layers:
        logger.error(f"  {asset_id}: 所有层均无数据!")
        return pd.Series(dtype=float)

    # 拼接：从最低优先级(最老)开始堆叠，高优先级覆盖
    combined: pd.Series = layers[-1][1].copy()
    for label, s in reversed(layers[:-1]):
        combined = combined.combine_first(s)   # s 中有数据的月份覆盖 combined
        combined.update(s)                      # 高优先级覆盖低优先级

    combined = combined.sort_index()
    logger.info(f"  {asset_id}: 拼接后 {combined.index.min().strftime('%Y-%m')} ~ "
                f"{combined.index.max().strftime('%Y-%m')}  ({len(combined)} 月)")
    return combined


# ─── 主函数 ───────────────────────────────────────────────────────────────────

def main(start: str = "1993-01-01", end: str = "2025-01-01"):
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    all_assets = list(PROXY_LAYERS.keys())
    frames = []
    coverage = {}

    logger.info(f"=== 代理数据拼接  {start} ~ {end} ===\n")

    for asset_id in all_assets:
        logger.info(f"{'─'*50}")
        logger.info(f"▶ {asset_id}")
        s = splice_asset(asset_id, start, end)
        if s.empty:
            continue
        df = pd.DataFrame({
            "date":     s.index,
            "asset_id": asset_id,
            "return_m": s.values,
        })
        frames.append(df)
        coverage[asset_id] = (s.index.min(), s.index.max(), len(s))

    if not frames:
        logger.error("没有任何数据，退出")
        return

    combined = pd.concat(frames, ignore_index=True)
    out_path = RAW_DIR / "etf" / "all_etf_returns.csv"
    combined.to_csv(out_path, index=False)

    # 打印覆盖摘要
    logger.info(f"\n{'='*60}")
    logger.info("覆盖摘要")
    logger.info(f"{'='*60}")
    crises = {
        "2000 科技泡沫": ("2000-01", "2002-10"),
        "2008 金融危机": ("2007-10", "2009-03"),
        "2022 利率冲击": ("2022-01", "2022-10"),
    }
    for asset_id, (mn, mx, cnt) in sorted(coverage.items()):
        row = f"  {asset_id:12s}: {mn.strftime('%Y-%m')} ~ {mx.strftime('%Y-%m')}  ({cnt:3d} 月)"
        covered = []
        for crisis, (cs, ce) in crises.items():
            if mn.strftime("%Y-%m") <= cs and mx.strftime("%Y-%m") >= ce:
                covered.append("✅")
            elif mn.strftime("%Y-%m") <= ce and mx.strftime("%Y-%m") >= cs:
                covered.append("⚠️ 部分")
            else:
                covered.append("❌")
        logger.info(row + "   " + " | ".join(covered))

    logger.info(f"\n[图例] ✅2000泡沫 | ✅2008危机 | ✅2022冲击")
    logger.info(f"\nall_etf_returns.csv: {len(combined)} 行")

    # 重建 processed/
    logger.info("\n=== 重建 processed/ parquet ===")
    from backtest.data.build_processed import build_all
    (PROJECT_ROOT / "data" / "processed").mkdir(parents=True, exist_ok=True)
    build_all()

    logger.info("\n✅ 完成! 运行回测:")
    logger.info("   python scripts/run_backtest.py")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="1993-01-01")
    p.add_argument("--end",   default="2025-01-01")
    args = p.parse_args()
    main(args.start, args.end)
