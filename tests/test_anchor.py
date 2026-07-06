"""
锚层测试 — Carver handcrafting 等风险贡献。

替代旧 test_layer0.py (CAPE 择时已砍)。
"""

from __future__ import annotations

from datetime import date

import pytest

from backtest.config import Params
from backtest.engine.anchor import (
    compute_anchor_risk_weights,
    _equal_risk_weights,
    _subtree_risk_weights,
)
from backtest.data._constants import EQUITY_MARKETS, DEFENSIVE_ASSETS, GROUP_TREE


@pytest.fixture
def provider():
    from tests.conftest import SyntheticProvider
    return SyntheticProvider()


@pytest.fixture
def params():
    return Params()


@pytest.fixture
def sample_date():
    return date(2020, 6, 30)


# ── 基本性质 ──────────────────────────────────────────────────────────────────


def test_weights_sum_to_one(provider, params, sample_date):
    """所有风险权重之和 = 1.0。"""
    rw = compute_anchor_risk_weights(provider, params, sample_date)
    assert abs(sum(rw.values()) - 1.0) < 1e-9


def test_all_weights_positive(provider, params, sample_date):
    """所有风险权重 > 0。"""
    rw = compute_anchor_risk_weights(provider, params, sample_date)
    for asset, w in rw.items():
        assert w > 0, f"{asset} 权重={w}, 应 > 0"


def test_all_assets_present(provider, params, sample_date):
    """所有配置的资产都有权重。"""
    rw = compute_anchor_risk_weights(provider, params, sample_date)
    expected = set(EQUITY_MARKETS) | set(DEFENSIVE_ASSETS)
    assert set(rw.keys()) == expected


# ── 攻防比例 ──────────────────────────────────────────────────────────────────


def test_attack_defense_split(provider, params, sample_date):
    """攻防比例 = attack_defense_ratio。"""
    rw = compute_anchor_risk_weights(provider, params, sample_date)
    attack = sum(rw.get(m, 0) for m in EQUITY_MARKETS)
    defense = sum(rw.get(j, 0) for j in DEFENSIVE_ASSETS)
    ratio = params.attack_defense_ratio
    assert abs(attack - ratio) < 1e-9
    assert abs(defense - (1 - ratio)) < 1e-9


def test_attack_defense_ratio_configurable(provider, sample_date):
    """不同攻防比例应反映在权重中。"""
    p1 = Params(attack_defense_ratio=0.3)
    p2 = Params(attack_defense_ratio=0.7)
    rw1 = compute_anchor_risk_weights(provider, p1, sample_date)
    rw2 = compute_anchor_risk_weights(provider, p2, sample_date)

    attack1 = sum(rw1.get(m, 0) for m in EQUITY_MARKETS)
    attack2 = sum(rw2.get(m, 0) for m in EQUITY_MARKETS)
    assert attack2 > attack1  # 0.7 > 0.3


# ── 等风险贡献 ────────────────────────────────────────────────────────────────


def test_equal_risk_weights_in_subgroup(provider, sample_date):
    """子组内风险权重纯等分 — vol 不进入风险空间 (只在 risk_to_cash 出现一次)。"""
    assets = ["US", "DM", "CN", "HK"]
    weights = _equal_risk_weights(assets, provider, sample_date)

    for a in assets:
        assert abs(weights[a] - 0.25) < 1e-9, f"{a} 风险权重={weights[a]}, 应等分为 0.25"


def test_defense_group_structure(provider, params, sample_date):
    """防御子树: rates/real_credit 两组等分, 组内叶子等分。"""
    rw = compute_anchor_risk_weights(provider, params, sample_date)
    defense_share = 1.0 - params.attack_defense_ratio
    # rates 组 (2 资产): 每个 = defense * 0.5 / 2
    assert abs(rw["CN_GOVT"] - defense_share * 0.25) < 1e-9
    assert abs(rw["TIPS"] - defense_share * 0.25) < 1e-9
    # real_credit 组 (3 资产): 每个 = defense * 0.5 / 3
    for a in ["GOLD", "CORP_BOND", "EM_BOND"]:
        assert abs(rw[a] - defense_share * 0.5 / 3) < 1e-9


def test_subtree_weights_sum_to_one(provider, sample_date):
    """子树内部权重归一化到 1。"""
    subtree = GROUP_TREE["attack"]
    weights = _subtree_risk_weights(subtree, provider, sample_date)
    assert abs(sum(weights.values()) - 1.0) < 1e-9


# ── 零判断 ────────────────────────────────────────────────────────────────────


def test_no_signal_dependency(provider, params):
    """锚层不依赖任何信号 (CAPE, 动量等), 只用 vol。"""
    # 不同日期, 只要 vol 相同, 锚权重就相同
    rw1 = compute_anchor_risk_weights(provider, params, date(2000, 1, 31))
    rw2 = compute_anchor_risk_weights(provider, params, date(2020, 12, 31))
    # SyntheticProvider 的 vol 是固定的, 所以权重应相同
    for asset in rw1:
        assert abs(rw1[asset] - rw2[asset]) < 1e-9, (
            f"{asset}: 2000={rw1[asset]:.4f}, 2020={rw2[asset]:.4f}, 应相同"
        )


# ── 加密腿留空 ────────────────────────────────────────────────────────────────


def test_empty_crypto_group(provider, params, sample_date):
    """加密子组为空时, 锚层仍正常工作。"""
    rw = compute_anchor_risk_weights(provider, params, sample_date)
    # 加密为空, 但其他资产正常
    assert len(rw) == len(EQUITY_MARKETS) + len(DEFENSIVE_ASSETS)
    assert abs(sum(rw.values()) - 1.0) < 1e-9
