"""Production-oriented risk model for combining external strategy return streams.

The module deliberately does not use backtest return or Sharpe to choose capital
weights.  Risk budgets are policy inputs; observed returns are used only for
volatility, correlation, and data-quality diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


class StrategyDataError(ValueError):
    """Raised when a strategy return stream is unsafe for allocation."""


@dataclass
class StrategyRiskInput:
    name: str
    account: str
    currency: str
    annual_vol: float
    risk_budget: float = 1.0
    max_active_weight: float = 1.0
    returns: pd.Series | None = None
    source: str = "manual"
    observations: int = 0
    first_date: pd.Timestamp | None = None
    last_date: pd.Timestamp | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class ActiveRiskModel:
    strategies: dict[str, StrategyRiskInput]
    correlation: pd.DataFrame
    covariance: pd.DataFrame
    strategy_weights: dict[str, float]
    account_weights: dict[str, float]
    account_strategy_weights: dict[str, dict[str, float]]
    annual_vol: float
    daily_returns: pd.Series | None
    monthly_returns: pd.Series | None
    diagnostics: dict[str, Any]


def _resolve_path(path: str, base_dir: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _read_return_file(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise StrategyDataError(f"Strategy returns file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    raise StrategyDataError(f"Unsupported strategy returns format: {path.suffix}")


def load_strategy_returns(
    strategy_cfg: dict,
    base_dir: Path,
    asof: pd.Timestamp | None = None,
) -> tuple[pd.Series | None, list[str]]:
    """Load and validate a net-of-cost, CNY-denominated daily return stream."""
    returns_path = strategy_cfg.get("returns_path")
    if not returns_path:
        return None, []

    if strategy_cfg.get("returns_net_of_costs") is not True:
        raise StrategyDataError(
            f"{strategy_cfg.get('name', '?')}: returns_net_of_costs must be true"
        )
    currency = str(strategy_cfg.get("returns_currency", "CNY")).upper()
    if currency != "CNY":
        raise StrategyDataError(
            f"{strategy_cfg.get('name', '?')}: returns must be converted to CNY; got {currency}"
        )

    path = _resolve_path(str(returns_path), base_dir)
    frame = _read_return_file(path)
    date_col = strategy_cfg.get("date_column", "date")
    return_col = strategy_cfg.get("return_column", "return")
    missing_cols = [c for c in (date_col, return_col) if c not in frame.columns]
    if missing_cols:
        raise StrategyDataError(
            f"{strategy_cfg.get('name', '?')}: missing columns {missing_cols} in {path}"
        )

    dates = pd.to_datetime(frame[date_col], errors="coerce", utc=True).dt.tz_localize(None)
    values = pd.to_numeric(frame[return_col], errors="coerce")
    if dates.isna().any() or values.isna().any():
        raise StrategyDataError(
            f"{strategy_cfg.get('name', '?')}: invalid dates or null/non-numeric returns"
        )

    series = pd.Series(values.to_numpy(dtype=float), index=pd.DatetimeIndex(dates), name=strategy_cfg.get("name"))
    series.index = series.index.normalize()
    if series.index.duplicated().any():
        duplicates = series.index[series.index.duplicated()].unique()[:5]
        raise StrategyDataError(
            f"{strategy_cfg.get('name', '?')}: duplicate return dates: "
            + ", ".join(str(d.date()) for d in duplicates)
        )
    series = series.sort_index()
    cutoff = pd.Timestamp(asof or pd.Timestamp.today()).tz_localize(None).normalize()
    if (series.index > cutoff).any():
        future = series.index[series.index > cutoff][0]
        raise StrategyDataError(
            f"{strategy_cfg.get('name', '?')}: future return date {future.date()} > {cutoff.date()}"
        )
    if (series <= -1.0).any():
        bad = series[series <= -1.0].index[0]
        raise StrategyDataError(
            f"{strategy_cfg.get('name', '?')}: return <= -100% at {bad.date()}"
        )
    if not np.isfinite(series.to_numpy()).all():
        raise StrategyDataError(f"{strategy_cfg.get('name', '?')}: non-finite return values")

    warnings: list[str] = []
    if len(series) > 1:
        median_gap = float(pd.Series(series.index).diff().dt.days.dropna().median())
        if median_gap > 4:
            warnings.append(f"median observation gap is {median_gap:.0f} days; daily risk model may be unreliable")
    latest_lag = (cutoff - series.index.max()).days
    max_lag = int(strategy_cfg.get("max_freshness_days", 7))
    if latest_lag > max_lag:
        raise StrategyDataError(
            f"{strategy_cfg.get('name', '?')}: latest return is {latest_lag} days stale "
            f"({series.index.max().date()})"
        )
    if (series.abs() > 0.50).any():
        warnings.append("contains absolute daily return above 50%; verify leverage, units, and corporate actions")
    return series, warnings


def ewma_annual_vol(returns: pd.Series, span: int = 35, min_periods: int = 20) -> float:
    if len(returns) < min_periods:
        raise StrategyDataError(
            f"Need at least {min_periods} daily returns for EWMA volatility; got {len(returns)}"
        )
    vol = returns.ewm(span=span, adjust=True, min_periods=min_periods).std().iloc[-1]
    if not np.isfinite(vol) or vol <= 0:
        raise StrategyDataError("EWMA volatility is missing or non-positive")
    return float(vol * np.sqrt(252))


def _asof_log_nav_panel(
    return_series: dict[str, pd.Series],
    max_staleness_days: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    start = max(s.index.min() for s in return_series.values())
    end = min(s.index.max() for s in return_series.values())
    if start >= end:
        raise StrategyDataError("Strategy return streams have no overlapping date range")
    calendar = pd.date_range(start, end, freq="B")
    nav_panel = pd.DataFrame(index=calendar)
    stale_panel = pd.DataFrame(index=calendar)

    for name, returns in return_series.items():
        log_nav = np.log1p(returns).cumsum()
        nav_panel[name] = log_nav.reindex(calendar).ffill()
        observed = pd.Series(returns.index, index=returns.index)
        last_seen = observed.reindex(calendar).ffill()
        stale = pd.Series((calendar - pd.DatetimeIndex(last_seen)).days, index=calendar)
        stale_panel[name] = stale
        nav_panel.loc[stale > max_staleness_days, name] = np.nan
    return nav_panel, stale_panel


def _weighted_covariance(values: pd.DataFrame, span: int) -> tuple[np.ndarray, float]:
    n = len(values)
    alpha = 2.0 / (span + 1.0)
    decay = 1.0 - alpha
    weights = decay ** np.arange(n - 1, -1, -1, dtype=float)
    weights /= weights.sum()
    array = values.to_numpy(dtype=float)
    mean = np.sum(array * weights[:, None], axis=0)
    centered = array - mean
    denominator = 1.0 - float(np.sum(weights**2))
    cov = (centered * weights[:, None]).T @ centered / denominator
    effective_n = 1.0 / float(np.sum(weights**2))
    return cov, effective_n


def estimate_overlapping_correlation(
    return_series: dict[str, pd.Series],
    overlap_days: int = 3,
    ewma_span: int = 126,
    min_common_observations: int = 63,
    max_staleness_days: int = 5,
    shrinkage: float = 0.50,
    eigenvalue_floor: float = 1e-6,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Estimate a shrunk PSD correlation matrix from overlapping k-day returns."""
    names = list(return_series)
    if len(names) == 1:
        return pd.DataFrame([[1.0]], index=names, columns=names), {
            "common_observations": len(next(iter(return_series.values()))),
            "ewma_effective_observations": None,
            "overlap_adjusted_effective_observations": None,
            "psd_repair": 0.0,
        }
    nav_panel, stale = _asof_log_nav_panel(return_series, max_staleness_days)
    overlapping = nav_panel.diff(overlap_days)
    valid = (stale <= max_staleness_days) & (stale.shift(overlap_days) <= max_staleness_days)
    overlapping = overlapping.where(valid).dropna(how="any")
    if len(overlapping) < min_common_observations:
        raise StrategyDataError(
            f"Only {len(overlapping)} common overlapping-return observations; "
            f"need {min_common_observations}"
        )

    cov_k, effective_n = _weighted_covariance(overlapping, ewma_span)
    cov_daily = cov_k / float(overlap_days)
    std = np.sqrt(np.clip(np.diag(cov_daily), 1e-16, None))
    corr = cov_daily / np.outer(std, std)
    corr = np.clip((corr + corr.T) / 2.0, -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)

    shrinkage = float(np.clip(shrinkage, 0.0, 1.0))
    corr = (1.0 - shrinkage) * corr + shrinkage * np.eye(len(names))
    before = corr.copy()
    vals, vecs = np.linalg.eigh((corr + corr.T) / 2.0)
    vals = np.maximum(vals, eigenvalue_floor)
    corr = vecs @ np.diag(vals) @ vecs.T
    scale = np.sqrt(np.diag(corr))
    corr = corr / np.outer(scale, scale)
    corr = np.clip((corr + corr.T) / 2.0, -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    repair = float(np.linalg.norm(corr - before, ord="fro"))

    return pd.DataFrame(corr, index=names, columns=names), {
        "common_observations": int(len(overlapping)),
        "first_common_date": overlapping.index.min().date().isoformat(),
        "last_common_date": overlapping.index.max().date().isoformat(),
        "ewma_effective_observations": float(effective_n),
        "overlap_adjusted_effective_observations": float(effective_n / overlap_days),
        "psd_repair": repair,
    }


def _cap_and_normalize(weights: dict[str, float], caps: dict[str, float]) -> dict[str, float]:
    if not weights:
        return {}
    if any(not np.isfinite(caps.get(k, 1.0)) or not 0 < caps.get(k, 1.0) <= 1.0 for k in weights):
        raise StrategyDataError("Strategy weight caps must be in (0, 1]")
    if sum(caps.get(k, 1.0) for k in weights) < 1.0 - 1e-12:
        raise StrategyDataError("Strategy max_active_weight caps are infeasible")
    result = {k: max(0.0, float(v)) for k, v in weights.items()}
    total = sum(result.values())
    if total <= 0:
        raise StrategyDataError("All strategy risk budgets are zero")
    result = {k: v / total for k, v in result.items()}
    for _ in range(len(result) + 2):
        over = {k: result[k] - caps.get(k, 1.0) for k in result if result[k] > caps.get(k, 1.0)}
        if not over:
            break
        excess = sum(over.values())
        for k in over:
            result[k] = caps.get(k, 1.0)
        room = {k: max(0.0, caps.get(k, 1.0) - result[k]) for k in result if k not in over}
        room_total = sum(room.values())
        if room_total + 1e-12 < excess:
            raise StrategyDataError("Strategy max_active_weight caps are infeasible")
        for k, available in room.items():
            result[k] += excess * available / room_total
    return result


def build_active_risk_model(
    cfg: dict,
    base_dir: Path,
    asof: pd.Timestamp | None = None,
) -> ActiveRiskModel:
    """Build a hierarchical active sleeve from configured strategy return streams."""
    risk_cfg = cfg.get("risk_estimation", {})
    vol_span = int(risk_cfg.get("vol_ewma_span", 35))
    vol_min = int(risk_cfg.get("vol_min_periods", 20))
    fallback_corr = float(risk_cfg.get("fallback_correlation", cfg.get("intra_active_corr", 0.5)))

    strategies: dict[str, StrategyRiskInput] = {}
    account_names: dict[str, list[str]] = {}
    for account, account_cfg in (cfg.get("accounts") or {}).items():
        for item in account_cfg.get("strategies", []):
            if item.get("enabled", True) is False:
                continue
            name = str(item["name"])
            if name in strategies:
                raise StrategyDataError(f"Duplicate strategy name: {name}")
            returns, warnings = load_strategy_returns(item, base_dir, asof)
            if returns is not None:
                vol = ewma_annual_vol(returns, span=int(item.get("vol_ewma_span", vol_span)), min_periods=vol_min)
                source = str(_resolve_path(str(item["returns_path"]), base_dir))
            else:
                vol = item.get("vol")
                if vol is None:
                    raise StrategyDataError(f"{name}: provide returns_path or manual vol")
                vol = float(vol)
                source = "manual"
                warnings.append("using manual volatility and fallback correlations")
            if not np.isfinite(vol) or vol <= 0:
                raise StrategyDataError(f"{name}: annual volatility must be positive")
            risk_budget = float(item.get("risk_budget", 1.0))
            confidence = float(item.get("allocation_confidence", 1.0))
            max_active_weight = float(item.get("max_active_weight", 1.0))
            if not np.isfinite(risk_budget) or risk_budget < 0:
                raise StrategyDataError(f"{name}: risk_budget must be non-negative")
            if not 0 <= confidence <= 1:
                raise StrategyDataError(f"{name}: allocation_confidence must be between 0 and 1")
            strategies[name] = StrategyRiskInput(
                name=name,
                account=account,
                currency=str(item.get("account_currency", "CNY")).upper(),
                annual_vol=vol,
                risk_budget=risk_budget * confidence,
                max_active_weight=max_active_weight,
                returns=returns,
                source=source,
                observations=len(returns) if returns is not None else 0,
                first_date=returns.index.min() if returns is not None else None,
                last_date=returns.index.max() if returns is not None else None,
                warnings=warnings,
            )
            account_names.setdefault(account, []).append(name)
    if not strategies:
        raise StrategyDataError("No enabled active strategies configured")

    names = list(strategies)
    corr = pd.DataFrame(fallback_corr, index=names, columns=names, dtype=float)
    np.fill_diagonal(corr.values, 1.0)
    empirical_names = [n for n in names if strategies[n].returns is not None]
    corr_diag: dict[str, Any] = {"mode": "fallback", "fallback_correlation": fallback_corr}
    if len(empirical_names) >= 2:
        empirical, corr_diag = estimate_overlapping_correlation(
            {n: strategies[n].returns for n in empirical_names},
            overlap_days=int(risk_cfg.get("overlap_days", 3)),
            ewma_span=int(risk_cfg.get("correlation_ewma_span", 126)),
            min_common_observations=int(risk_cfg.get("min_common_observations", 63)),
            max_staleness_days=int(risk_cfg.get("max_staleness_days", 5)),
            shrinkage=float(risk_cfg.get("correlation_shrinkage", 0.50)),
            eigenvalue_floor=float(risk_cfg.get("eigenvalue_floor", 1e-6)),
        )
        corr.loc[empirical_names, empirical_names] = empirical
        corr_diag["mode"] = "empirical_with_fallback_pairs" if len(empirical_names) < len(names) else "empirical"
        corr_diag["fallback_correlation"] = fallback_corr

    vols = pd.Series({n: strategies[n].annual_vol for n in names})
    covariance = corr.mul(vols, axis=0).mul(vols, axis=1)

    account_internal: dict[str, dict[str, float]] = {}
    account_vols: dict[str, float] = {}
    for account, members in account_names.items():
        raw = {n: strategies[n].risk_budget / strategies[n].annual_vol for n in members}
        weights = _cap_and_normalize(raw, {n: 1.0 for n in members})
        account_internal[account] = weights
        vector = pd.Series(weights).reindex(names, fill_value=0.0).to_numpy()
        account_vols[account] = float(np.sqrt(vector @ covariance.to_numpy() @ vector))

    account_budgets = {
        account: float((cfg["accounts"][account] or {}).get("risk_budget", 1.0))
        for account in account_names
    }
    if any(not np.isfinite(value) or value < 0 for value in account_budgets.values()):
        raise StrategyDataError("Account risk_budget must be non-negative")
    account_raw = {
        account: account_budgets[account] / account_vols[account]
        for account in account_names
    }
    account_weights = _cap_and_normalize(account_raw, {a: 1.0 for a in account_raw})
    combined = {
        name: account_weights[account] * account_internal[account][name]
        for account, members in account_names.items()
        for name in members
    }
    combined = _cap_and_normalize(combined, {n: strategies[n].max_active_weight for n in names})

    # Strategy caps can move capital across accounts.  Rebuild both hierarchy
    # levels from the final strategy weights so reporting and funding agree with
    # the covariance calculation below.
    account_weights = {
        account: sum(combined[name] for name in members)
        for account, members in account_names.items()
    }
    account_internal = {
        account: {
            name: combined[name] / account_weights[account]
            for name in members
        }
        for account, members in account_names.items()
        if account_weights[account] > 0
    }
    vector = pd.Series(combined).reindex(names).to_numpy()
    active_vol = float(np.sqrt(vector @ covariance.to_numpy() @ vector))

    daily_returns = None
    monthly_returns = None
    if len(empirical_names) == len(names):
        aligned = pd.concat({n: strategies[n].returns for n in names}, axis=1).dropna(how="any")
        if not aligned.empty:
            daily_returns = aligned.mul(pd.Series(combined), axis=1).sum(axis=1)
        monthly_by_strategy = pd.concat(
            {
                n: (1.0 + strategies[n].returns).resample("ME").prod() - 1.0
                for n in names
            },
            axis=1,
        ).dropna(how="any")
        if not monthly_by_strategy.empty:
            monthly_returns = monthly_by_strategy.mul(pd.Series(combined), axis=1).sum(axis=1)

    return ActiveRiskModel(
        strategies=strategies,
        correlation=corr,
        covariance=covariance,
        strategy_weights=combined,
        account_weights=account_weights,
        account_strategy_weights=account_internal,
        annual_vol=active_vol,
        daily_returns=daily_returns,
        monthly_returns=monthly_returns,
        diagnostics={
            "correlation": corr_diag,
            "manual_strategy_count": len(names) - len(empirical_names),
            "empirical_strategy_count": len(empirical_names),
            "strategy_data": {
                name: {
                    "source": item.source,
                    "observations": item.observations,
                    "first_date": item.first_date,
                    "last_date": item.last_date,
                    "annual_vol": item.annual_vol,
                    "currency": item.currency,
                }
                for name, item in strategies.items()
            },
            "warnings": {n: strategies[n].warnings for n in names if strategies[n].warnings},
        },
    )


def estimate_core_active_correlation(
    passive_monthly_returns: pd.Series | None,
    active_monthly_returns: pd.Series | None,
    fallback: float,
    floor: float = 0.0,
    span_months: int = 36,
    min_months: int = 24,
    shrinkage: float = 0.50,
) -> tuple[float, dict[str, Any]]:
    if passive_monthly_returns is None or active_monthly_returns is None:
        return fallback, {"mode": "fallback", "used": fallback}
    joined = pd.concat(
        {"passive": passive_monthly_returns, "active": active_monthly_returns}, axis=1
    ).dropna()
    if len(joined) < min_months:
        return fallback, {
            "mode": "fallback_insufficient_history",
            "months": len(joined),
            "required": min_months,
            "used": fallback,
        }
    empirical = float(joined["passive"].ewm(span=span_months, min_periods=min_months).corr(joined["active"]).iloc[-1])
    if not np.isfinite(empirical):
        return fallback, {"mode": "fallback_invalid_estimate", "used": fallback}
    shrunk = (1.0 - shrinkage) * empirical + shrinkage * fallback
    used = float(np.clip(max(floor, shrunk), -0.99, 0.99))
    return used, {
        "mode": "empirical_shrunk",
        "months": len(joined),
        "empirical": empirical,
        "shrunk": shrunk,
        "floor": floor,
        "used": used,
    }
