from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.active_sleeve import (
    StrategyDataError,
    build_active_risk_model,
    estimate_core_active_correlation,
    estimate_overlapping_correlation,
    load_strategy_returns,
)


def _write_returns(path: Path, values: np.ndarray, start: str = "2024-01-02") -> Path:
    dates = pd.bdate_range(start, periods=len(values))
    pd.DataFrame({"date": dates, "return": values}).to_csv(path, index=False)
    return path


def _strategy(name: str, path: Path, **overrides) -> dict:
    result = {
        "name": name,
        "returns_path": str(path),
        "returns_currency": "CNY",
        "returns_net_of_costs": True,
        "account_currency": "USD",
        "risk_budget": 1.0,
        "max_active_weight": 0.75,
        "max_freshness_days": 9999,
    }
    result.update(overrides)
    return result


def test_return_contract_rejects_duplicate_dates(tmp_path):
    path = tmp_path / "dup.csv"
    pd.DataFrame(
        {"date": ["2025-01-02", "2025-01-02"], "return": [0.01, 0.02]}
    ).to_csv(path, index=False)
    with pytest.raises(StrategyDataError, match="duplicate return dates"):
        load_strategy_returns(_strategy("dup", path), tmp_path, pd.Timestamp("2025-01-03"))


def test_return_contract_requires_cost_and_cny_flags(tmp_path):
    path = _write_returns(tmp_path / "returns.csv", np.zeros(30))
    with pytest.raises(StrategyDataError, match="returns_net_of_costs"):
        load_strategy_returns(
            _strategy("gross", path, returns_net_of_costs=False), tmp_path, pd.Timestamp("2026-01-01")
        )
    with pytest.raises(StrategyDataError, match="converted to CNY"):
        load_strategy_returns(
            _strategy("usd", path, returns_currency="USD"), tmp_path, pd.Timestamp("2026-01-01")
        )


def test_three_day_overlap_correlation_is_psd_and_reports_effective_sample():
    rng = np.random.default_rng(7)
    n = 400
    common = rng.normal(0, 0.01, n)
    a = pd.Series(common + rng.normal(0, 0.003, n), index=pd.bdate_range("2024-01-02", periods=n))
    # Same shock reaches B one business day later.
    b_values = np.r_[0.0, common[:-1]] + rng.normal(0, 0.003, n)
    b = pd.Series(b_values, index=a.index)

    corr, diagnostics = estimate_overlapping_correlation(
        {"A": a, "B": b}, overlap_days=3, ewma_span=126,
        min_common_observations=63, shrinkage=0.25,
    )

    assert corr.loc["A", "B"] > 0.45
    assert np.linalg.eigvalsh(corr.to_numpy()).min() >= -1e-10
    assert 35 < diagnostics["overlap_adjusted_effective_observations"] < 45


def test_build_active_model_uses_returns_and_respects_caps(tmp_path):
    rng = np.random.default_rng(11)
    n = 300
    a = _write_returns(tmp_path / "a.csv", rng.normal(0, 0.008, n))
    b = _write_returns(tmp_path / "b.csv", rng.normal(0, 0.015, n))
    cfg = {
        "risk_estimation": {
            "vol_ewma_span": 35,
            "vol_min_periods": 20,
            "overlap_days": 3,
            "correlation_ewma_span": 126,
            "min_common_observations": 63,
            "correlation_shrinkage": 0.5,
            "fallback_correlation": 0.5,
        },
        "accounts": {
            "acct_a": {"strategies": [_strategy("A", a, max_active_weight=0.60)]},
            "acct_b": {"strategies": [_strategy("B", b, max_active_weight=0.60)]},
        },
    }
    model = build_active_risk_model(cfg, tmp_path, asof=pd.Timestamp("2026-12-31"))

    assert abs(sum(model.strategy_weights.values()) - 1.0) < 1e-9
    assert max(model.strategy_weights.values()) <= 0.60 + 1e-9
    assert model.annual_vol > 0
    assert model.diagnostics["correlation"]["mode"] == "empirical"
    assert model.daily_returns is not None
    assert model.monthly_returns is not None
    assert abs(sum(model.account_weights.values()) - 1.0) < 1e-9
    for account, internal in model.account_strategy_weights.items():
        assert abs(sum(internal.values()) - 1.0) < 1e-9
        reconstructed = sum(model.strategy_weights[name] for name in internal)
        assert model.account_weights[account] == pytest.approx(reconstructed)


def test_active_model_rejects_infeasible_caps(tmp_path):
    rng = np.random.default_rng(19)
    a = _write_returns(tmp_path / "a.csv", rng.normal(0, 0.01, 100))
    b = _write_returns(tmp_path / "b.csv", rng.normal(0, 0.01, 100))
    cfg = {
        "accounts": {
            "acct": {
                "strategies": [
                    _strategy("A", a, max_active_weight=0.40),
                    _strategy("B", b, max_active_weight=0.40),
                ]
            }
        }
    }
    with pytest.raises(StrategyDataError, match="caps are infeasible"):
        build_active_risk_model(cfg, tmp_path, asof=pd.Timestamp("2026-12-31"))


def test_core_active_correlation_falls_back_then_uses_shrunk_history():
    dates = pd.date_range("2020-01-31", periods=50, freq="ME")
    passive = pd.Series(np.sin(np.arange(50)) * 0.02, index=dates)
    active = passive * 0.8

    used, meta = estimate_core_active_correlation(
        passive, active, fallback=0.5, floor=0.2, min_months=24, shrinkage=0.5
    )
    assert meta["mode"] == "empirical_shrunk"
    assert 0.2 <= used <= 0.99

    fallback, meta = estimate_core_active_correlation(
        passive.iloc[:10], active.iloc[:10], fallback=0.5, min_months=24
    )
    assert fallback == 0.5
    assert meta["mode"].startswith("fallback")
