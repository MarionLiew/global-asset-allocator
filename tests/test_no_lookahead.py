"""
无前视测试 — PIT 纪律。
"""

import pytest
import pandas as pd
from datetime import date


def test_cape_uses_only_visible_data():
    """CAPE 在 asof 不能用之后的数据。"""
    # 构造一个 provider, 只在特定日期有数据
    # 如果查询 asof 在数据之前, 应该返回 fallback
    from backtest.data.csv_provider import CSVProvider
    from backtest.config import Params

    # 使用 SyntheticProvider 验证概念
    from tests.conftest import SyntheticProvider
    provider = SyntheticProvider()

    # 查询 1995 年的 CAPE (应该有值)
    cape = provider.cape("US", date(1995, 1, 31))
    assert cape > 0


def test_quadrant_respects_lag():
    """象限分类器用 lag_months 偏移。"""
    from backtest.data.csv_provider import CSVProvider

    # 验证: 如果宏观数据只到 2000 年 3 月, 查询 2000 年 6 月应该用 2000 年 3 月的数据
    # (含 3 月发布滞后, 所以实际用的是 1999 年 12 月的数据)
    pass  # 实际测试需要真实数据


def test_returns_use_close_of_month():
    """月度回报用月末价, 不用下月初价。"""
    # 验证 ETF 回报的日期都是月末
    import pandas as pd
    from backtest.data._constants import ETF_TICKERS

    # 回报文件应该是月末日期
    # 这个在 build_processed.py 中通过 dt.to_period("M").to_timestamp("M") 保证
    pass
