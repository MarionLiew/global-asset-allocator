import pytest

from scripts.monthly_ops import plan_monthly_trades


def _legs(target_a=0.5, target_b=0.5):
    return {
        "A": ("被动", "acct", "CNY", target_a),
        "B": ("被动", "acct", "CNY", target_b),
    }


def test_dynamic_mode_sells_and_buys_to_buffer_edges():
    plan = plan_monthly_trades(
        _legs(), {"A": 80.0, "B": 20.0}, 0.0,
        {"A": 0.10, "B": 0.10}, allow_sells=True,
    )

    assert plan["sells"]["A"] == pytest.approx(20.0)
    assert plan["buys"]["B"] == pytest.approx(20.0)
    assert plan["unallocated_cash"] == 0.0


def test_buy_only_mode_never_sells():
    plan = plan_monthly_trades(
        _legs(), {"A": 80.0, "B": 20.0}, 0.0,
        {"A": 0.10, "B": 0.10}, allow_sells=False,
    )

    assert plan["sells"] == {}
    assert plan["buys"] == {}
    assert plan["overweights"]["A"] == pytest.approx(30.0)


def test_zero_target_has_no_buffer_residual():
    plan = plan_monthly_trades(
        _legs(target_a=1.0, target_b=0.0), {"A": 90.0, "B": 10.0}, 0.0,
        {"A": 0.05, "B": 0.05}, allow_sells=True,
    )

    assert plan["sells"]["B"] == 10.0
    assert plan["buys"]["A"] == 10.0
