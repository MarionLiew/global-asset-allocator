"""
配置加载与 hash 校验。

Params 是不可变容器; 回测启动时先校验 params_hash。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CostsConfig:
    equity_bps: float = 10.0
    defensive_bps: float = 5.0
    fx_spread_bps: float = 20.0
    tax_dividend_withholding: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestConfig:
    """回测运行时配置 (非策略参数)。"""
    start_date: str = "1995-01-31"
    end_date: str = "2024-12-31"
    base_currency: str = "CNY"
    monthly_contribution_cny: float = 10_000.0
    costs: CostsConfig = field(default_factory=CostsConfig)
    equity_markets: dict = field(default_factory=dict)
    defensive_assets: dict = field(default_factory=dict)
    benchmarks: dict = field(default_factory=dict)
    quadrant: dict = field(default_factory=dict)
    regimes: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = "config/backtest.yaml") -> BacktestConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)
        costs = CostsConfig(**raw.pop("costs"))
        return cls(costs=costs, **raw)


@dataclass(frozen=True)
class Params:
    """冻结策略参数。从 params.yaml 加载, 验证 hash。"""
    version: int = 2
    params_hash: str = ""

    # ── 旧 Layer 0 (保留向后兼容，旧测试仍用) ──
    E_base: float = 0.60
    k0: float = 0.20
    E_min: float = 0.40
    E_max: float = 0.80

    # ── 旧 Layer 1 (保留向后兼容) ──
    lambda_: dict[str, float] = field(default_factory=lambda: {"US": 0.6, "DM": 0.6, "CN": 0.3, "HK": 0.3})
    band_pp: float = 0.15
    delta_home: float = 0.03
    cape_target_window: int = 120

    # ── 旧 Layer 2 (保留向后兼容) ──
    delta_quadrant: float = 0.08
    defensive_single_asset_cap: float = 0.45
    ewma_fast_halflife: int = 6
    ewma_slow_halflife: int = 36
    ewma_mix_weight: float = 0.7

    # ── 旧执行/预留 (保留向后兼容) ──
    H: float = 10.0
    tau: float = 0.5
    n_min: int = 24
    breadth_min: int = 20
    kelly_fraction: float = 0.25
    active_name_cap: float = 0.10
    band_rel: float = 0.25
    band_abs: float = 0.05

    # ── 新: 锚层 (ALLOCATOR_PLAN §一A) ──
    attack_defense_ratio: float = 0.50   # 进攻占总风险的固定比例

    # ── 新: 倾斜层 (ALLOCATOR_PLAN §一B) ──
    tilt_band_pp: float = 0.05           # 偏离锚硬上限 (风险口径)
    w_val: float = 0.50                  # 估值信号权重
    w_mom: float = 0.50                  # 动量信号权重
    tilt_max: float = 0.10               # 最大倾斜幅度 (占子组权重)

    # ── 新: 执行层 (ALLOCATOR_PLAN §一C) ──
    no_trade_cost_multiplier: float = 2.0  # 带宽 = 成本 * 倍数
    no_trade_min_band: float = 0.02        # 最小绝对带宽 2%
    no_trade_max_band: float = 0.10        # 最大绝对带宽 10%

    # ── 新: 风险报告 ──
    target_vol: float = 0.10             # 组合目标波动率 (年化, 纯报告用)

    @classmethod
    def load(cls, path: str | Path = "config/params.yaml") -> Params:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})

    def verify_hash(self, path: str | Path = "config/params.yaml") -> None:
        """重新计算 hash 并断言匹配。不匹配则抛 ValueError。"""
        expected = compute_params_hash(path)
        if self.params_hash != expected:
            raise ValueError(
                f"params_hash 不匹配! 文件中={self.params_hash}, 实际={expected}\n"
                f"请运行 scripts/freeze_params.py 重新冻结, 或传 --allow-param-change"
            )


def compute_params_hash(path: str | Path) -> str:
    """计算 params.yaml 的 sha256 hash (排除 params_hash 行本身)。"""
    with open(path) as f:
        lines = [l for l in f.read().splitlines() if not l.lstrip().startswith("params_hash")]
    h = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]
    return f"sha256:{h}"
