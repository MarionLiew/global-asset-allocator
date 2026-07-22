"""Monthly market-data alignment and completeness helpers."""

from __future__ import annotations

from datetime import date

import pandas as pd


class MissingReturnError(ValueError):
    """Raised when a requested asset/month has no observed return."""


class IncompleteReturnPanelError(ValueError):
    """Raised when the common backtest window contains missing asset-months."""


def last_complete_month_end(asof: date | pd.Timestamp | None = None) -> pd.Timestamp:
    """Return the month-end immediately before the month containing ``asof``."""
    ts = pd.Timestamp(asof or date.today()).tz_localize(None).normalize()
    return (ts.to_period("M") - 1).to_timestamp("M")


def monthly_returns_from_daily_close(
    close: pd.Series,
    asof: date | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build simple monthly returns from daily closes for completed months only.

    Prices are never forward-filled. If a whole calendar month is absent, both
    that month and the following cross-gap return remain missing so validation
    can reject the discontinuity instead of silently fabricating a return.
    """
    if close.empty:
        return pd.DataFrame(columns=["close", "return_m"])

    values = close.copy()
    values.index = pd.DatetimeIndex(values.index).tz_localize(None)
    values = values.sort_index()
    values = values[~values.index.duplicated(keep="last")].dropna()

    cutoff = last_complete_month_end(asof)
    month_end_close = values.resample("ME").last().loc[:cutoff]
    returns = month_end_close.pct_change(fill_method=None)
    return pd.DataFrame({"close": month_end_close, "return_m": returns}).dropna()


def complete_return_dates(
    returns: pd.DataFrame,
    assets: list[str],
    requested_start: date | pd.Timestamp,
    requested_end: date | pd.Timestamp,
) -> pd.DatetimeIndex:
    """Return a continuous common monthly window or raise on internal gaps.

    The effective start is moved to the latest first observation across assets,
    preventing pre-inception months from becoming zero returns. Once every asset
    has begun, every asset/month must be present through the common end.
    """
    required = set(assets)
    available = set(returns.get("asset_id", pd.Series(dtype=str)).unique())
    absent = sorted(required - available)
    if absent:
        raise IncompleteReturnPanelError(
            f"Configured assets have no return history: {', '.join(absent)}"
        )

    panel = returns[returns["asset_id"].isin(assets)].copy()
    panel["date"] = pd.to_datetime(panel["date"]).dt.to_period("M").dt.to_timestamp("M")
    duplicates = panel.duplicated(["asset_id", "date"], keep=False)
    if duplicates.any():
        sample = panel.loc[duplicates, ["asset_id", "date"]].head(5)
        raise IncompleteReturnPanelError(
            "Duplicate asset-month returns: "
            + ", ".join(f"{r.asset_id}@{r.date.date()}" for r in sample.itertuples())
        )

    bounds = panel.groupby("asset_id")["date"].agg(["min", "max"])
    start = max(pd.Timestamp(requested_start), bounds["min"].max())
    end = min(pd.Timestamp(requested_end), bounds["max"].min())
    start = start.to_period("M").to_timestamp("M")
    end = end.to_period("M").to_timestamp("M")
    if start > end:
        raise IncompleteReturnPanelError(
            f"No common return window inside {requested_start} .. {requested_end}"
        )

    expected_dates = pd.date_range(start, end, freq="ME")
    expected = pd.MultiIndex.from_product(
        [assets, expected_dates], names=["asset_id", "date"]
    )
    observed = panel.set_index(["asset_id", "date"])["return_m"].reindex(expected)
    missing = observed[observed.isna()]
    if not missing.empty:
        sample = list(missing.index[:8])
        detail = ", ".join(f"{asset}@{dt.date()}" for asset, dt in sample)
        more = " ..." if len(missing) > len(sample) else ""
        raise IncompleteReturnPanelError(
            f"Missing returns inside common window ({len(missing)}): {detail}{more}"
        )
    return expected_dates
