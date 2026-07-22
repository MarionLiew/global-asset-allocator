from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backtest.data.monthly import (
    IncompleteReturnPanelError,
    complete_return_dates,
    monthly_returns_from_daily_close,
)


def test_daily_prices_exclude_incomplete_month_and_do_not_forward_fill():
    close = pd.Series(
        [100.0, 110.0, 121.0],
        index=pd.to_datetime(["2024-01-31", "2024-03-29", "2024-04-15"]),
    )
    monthly = monthly_returns_from_daily_close(close, asof=date(2024, 4, 20))

    # February is absent. March's cross-gap return must not be accepted, and
    # April is still incomplete as of April 20.
    assert monthly.empty


def test_complete_dates_move_start_to_common_inception():
    rows = []
    for asset, start in [("A", "2020-01-31"), ("B", "2020-03-31")]:
        for dt in pd.date_range(start, "2020-05-31", freq="ME"):
            rows.append({"asset_id": asset, "date": dt, "return_m": 0.01})
    dates = complete_return_dates(
        pd.DataFrame(rows), ["A", "B"], "2020-01-31", "2020-05-31"
    )
    assert list(dates) == list(pd.date_range("2020-03-31", "2020-05-31", freq="ME"))


def test_complete_dates_reject_internal_missing_month():
    rows = []
    for asset in ["A", "B"]:
        for dt in pd.date_range("2020-01-31", "2020-04-30", freq="ME"):
            if not (asset == "B" and dt == pd.Timestamp("2020-03-31")):
                rows.append({"asset_id": asset, "date": dt, "return_m": 0.01})

    with pytest.raises(IncompleteReturnPanelError, match="B@2020-03-31"):
        complete_return_dates(
            pd.DataFrame(rows), ["A", "B"], "2020-01-31", "2020-04-30"
        )
