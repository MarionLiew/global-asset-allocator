#!/usr/bin/env python3
"""Diagnose EWMA response speed and one-step risk forecast errors.

This script never evaluates portfolio return or Sharpe and never writes frozen
strategy parameters. Outputs are data-quality and risk-forecast diagnostics.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backtest.config import BacktestConfig, Params
from backtest.data._constants import DEFENSIVE_ASSETS, EQUITY_MARKETS
from backtest.data.csv_provider import CSVProvider
from backtest.engine.anchor import compute_anchor_risk_weights
from backtest.engine.layer1 import compute_tilt
from backtest.engine.risk_to_cash import risk_weights_to_cash_weights


CANDIDATES = {
    "6/36 current": (6, 36),
    "4/24 faster": (4, 24),
    "3/18 faster": (3, 18),
    "3/12 faster": (3, 12),
}

EVENTS = {
    "GFC 2008": "2008-09-30",
    "COVID 2020": "2020-03-31",
    "Inflation 2022": "2022-06-30",
}


def ewma_variance(rets: pd.Series, fast: int, slow: int, mix: float = 0.7) -> pd.Series:
    af = 1 - np.exp(-np.log(2) / fast)
    ass = 1 - np.exp(-np.log(2) / slow)
    vf = rets.ewm(alpha=af, min_periods=12).var()
    vs = rets.ewm(alpha=ass, min_periods=12).var()
    return mix * vf + (1 - mix) * vs


def theoretical_response() -> tuple[pd.DataFrame, dict]:
    months = np.arange(0, 37)
    rows = []
    summary = {}
    for label, (fast, slow) in CANDIDATES.items():
        absorbed = 0.7 * (1 - 2 ** (-months / fast)) + 0.3 * (1 - 2 ** (-months / slow))
        # Illustrative persistent doubling of volatility: variance rises 4x.
        vol_ratio = np.sqrt(1 + 3 * absorbed)
        # One of nine initially equal-risk/equal-vol assets experiences the shock.
        affected_weight = (1 / vol_ratio) / (8 + 1 / vol_ratio)
        initial_weight = 1 / 9
        final_weight = (1 / 2) / (8 + 1 / 2)
        weight_progress = (initial_weight - affected_weight) / (initial_weight - final_weight)
        for m, a, vr, wp, aw in zip(months, absorbed, vol_ratio, weight_progress, affected_weight):
            rows.append({
                "parameter": label,
                "month": int(m),
                "variance_state_absorbed": float(a),
                "volatility_ratio": float(vr),
                "target_adjustment_absorbed": float(wp),
                "affected_asset_weight": float(aw),
            })

        def first_half(values: np.ndarray) -> int:
            return int(months[np.flatnonzero(values >= 0.5)[0]])

        summary[label] = {
            "variance_half_response_months": first_half(absorbed),
            "volatility_level_half_response_months": first_half((vol_ratio - 1) / (2 - 1)),
            "target_weight_half_response_months": first_half(weight_progress),
        }
    return pd.DataFrame(rows), summary


def forecast_errors(ret_matrix: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for label, (fast, slow) in CANDIDATES.items():
        asset_losses = []
        for asset in ret_matrix.columns:
            r = ret_matrix[asset].dropna()
            forecast = ewma_variance(r, fast, slow).shift(1)
            actual = r.pow(2)
            sample = pd.concat({"forecast": forecast, "actual": actual}, axis=1).dropna()
            sample["forecast"] = sample["forecast"].clip(lower=1e-8)
            qlike = sample["actual"] / sample["forecast"] + np.log(sample["forecast"])
            high_threshold = sample["actual"].quantile(0.80)
            high = sample["actual"] >= high_threshold
            high_qlike = qlike[high].mean()
            high_actual_to_forecast = (
                sample.loc[high, "actual"] / sample.loc[high, "forecast"]
            ).mean()
            variance_rmse = np.sqrt(np.mean((sample["forecast"] - sample["actual"]) ** 2))
            asset_losses.append(
                (asset, qlike.mean(), high_qlike, high_actual_to_forecast, variance_rmse, len(sample))
            )
        for asset, qlike, high_qlike, high_ratio, rmse, n in asset_losses:
            rows.append({
                "parameter": label,
                "asset": asset,
                "qlike": qlike,
                "top20_realized_risk_qlike": high_qlike,
                "top20_actual_to_forecast_variance": high_ratio,
                "variance_rmse": rmse,
                "observations": n,
            })
    out = pd.DataFrame(rows)
    baseline = out[out["parameter"] == "6/36 current"].set_index("asset")["qlike"]
    out["qlike_vs_current"] = out.apply(lambda r: r["qlike"] - baseline[r["asset"]], axis=1)
    return out


def historical_curves(provider: CSVProvider, dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    assets = EQUITY_MARKETS + DEFENSIVE_ASSETS
    ret = provider._etf_returns.pivot(index="date", columns="asset_id", values="return_m").reindex(dates)
    records = []
    for dt in dates:
        asof = dt.date()
        risk = compute_anchor_risk_weights(provider, provider.params, asof)
        tilted = compute_tilt(risk, provider, provider.params, asof)
        cash = risk_weights_to_cash_weights(tilted, provider, asof, provider.params.vol_floor)
        vols = [provider.vol(a, asof) for a in assets]
        records.append({
            "date": dt,
            "shock_rms_return": float(np.sqrt(np.mean(np.square(ret.loc[dt, assets])))),
            "median_annual_vol": float(np.median(vols)),
            "equity_cash_weight": float(sum(cash.get(a, 0.0) for a in EQUITY_MARKETS)),
            **{f"weight_{a}": cash.get(a, 0.0) for a in assets},
            **{f"vol_{a}": provider.vol(a, asof) for a in assets},
        })
    state = pd.DataFrame(records).set_index("date")

    curves = []
    event_summary = []
    weight_cols = [f"weight_{a}" for a in assets]
    for event, event_date in EVENTS.items():
        center = pd.Timestamp(event_date)
        window_dates = pd.date_range(center - pd.offsets.MonthEnd(6), center + pd.offsets.MonthEnd(18), freq="ME")
        w = state.reindex(window_dates).dropna().copy()
        baseline_date = center - pd.offsets.MonthEnd(1)
        baseline = state.loc[baseline_date]
        w["target_l1_change"] = w[weight_cols].sub(baseline[weight_cols]).abs().sum(axis=1)

        # Freeze anchor/tilt risk weights at the pre-event month so this series
        # isolates only the cash-weight response caused by changing EWMA vols.
        base_asof = baseline_date.date()
        base_risk = compute_anchor_risk_weights(provider, provider.params, base_asof)
        base_tilt = compute_tilt(base_risk, provider, provider.params, base_asof)
        base_cash = {a: baseline[f"weight_{a}"] for a in assets}
        risk_only_l1 = []
        for _, row in w.iterrows():
            raw = {
                a: base_tilt[a] / max(row[f"vol_{a}"], provider.params.vol_floor)
                for a in assets
            }
            total = sum(raw.values())
            risk_only = {a: raw[a] / total for a in assets}
            risk_only_l1.append(sum(abs(risk_only[a] - base_cash[a]) for a in assets))
        w["risk_only_target_l1_change"] = risk_only_l1
        w["event"] = event
        w["month_offset"] = [
            (d.year - center.year) * 12 + d.month - center.month for d in w.index
        ]
        curves.append(w.reset_index())

        post = w[w["month_offset"].between(0, 12)]
        shock_peak = post["shock_rms_return"].idxmax()
        after_shock = post.loc[shock_peak:]
        vol_peak = after_shock["median_annual_vol"].idxmax()
        weight_peak = after_shock["risk_only_target_l1_change"].idxmax()
        event_summary.append({
            "event": event,
            "shock_peak": shock_peak.date().isoformat(),
            "vol_peak": vol_peak.date().isoformat(),
            "vol_peak_lag_months": (vol_peak.year - shock_peak.year) * 12 + vol_peak.month - shock_peak.month,
            "weight_peak": weight_peak.date().isoformat(),
            "weight_peak_lag_months": (weight_peak.year - shock_peak.year) * 12 + weight_peak.month - shock_peak.month,
            "peak_risk_only_target_l1_change": float(
                after_shock.loc[weight_peak, "risk_only_target_l1_change"]
            ),
            "peak_full_model_target_l1_change": float(
                after_shock["target_l1_change"].max()
            ),
        })
    return pd.concat(curves, ignore_index=True), pd.DataFrame(event_summary)


def main() -> None:
    out_dir = ROOT / "output" / "diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)

    params = Params.load(ROOT / "config" / "params.yaml")
    config = BacktestConfig.load(ROOT / "config" / "backtest.yaml")
    provider = CSVProvider(ROOT, params)
    assets = EQUITY_MARKETS + DEFENSIVE_ASSETS
    dates = pd.DatetimeIndex(provider.get_complete_return_dates(assets, config.start_date, "2026-06-30"))
    ret_matrix = provider._etf_returns.pivot(index="date", columns="asset_id", values="return_m").reindex(dates)

    response, response_summary = theoretical_response()
    errors = forecast_errors(ret_matrix)
    curves, events = historical_curves(provider, dates)

    response.to_csv(out_dir / "ewma_step_response.csv", index=False)
    errors.to_csv(out_dir / "ewma_forecast_errors.csv", index=False)
    curves.to_csv(out_dir / "historical_shock_response.csv", index=False)
    events.to_csv(out_dir / "historical_shock_summary.csv", index=False)

    aggregate = errors.groupby("parameter").agg(
        mean_qlike=("qlike", "mean"),
        mean_qlike_vs_current=("qlike_vs_current", "mean"),
        mean_variance_rmse=("variance_rmse", "mean"),
        top20_realized_risk_qlike=("top20_realized_risk_qlike", "mean"),
        top20_actual_to_forecast_variance=("top20_actual_to_forecast_variance", "mean"),
    ).reset_index()
    payload = {
        "frozen_parameters_unchanged": True,
        "common_window": [dates.min().date().isoformat(), dates.max().date().isoformat()],
        "monthly_sampling_delay_months": {"best": 0.0, "average": 0.5, "worst": 1.0},
        "response": response_summary,
        "forecast_error_aggregate": aggregate.to_dict(orient="records"),
        "events": events.to_dict(orient="records"),
    }
    (out_dir / "ewma_diagnostic_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
