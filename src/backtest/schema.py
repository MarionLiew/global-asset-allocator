"""
数据契约 — 回测结果的 dataclass 定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd


@dataclass
class WeightSnapshot:
    """某月的目标权重快照。"""
    asof: date
    E: float                                    # 旧 Layer 0: 总股票预算 (兼容)
    m_i: dict[str, float]                       # 旧 Layer 1: 跨市场权重 (兼容)
    d_j: dict[str, float]                       # 旧 Layer 2: 防御构成 (兼容)
    targets: dict[str, float]                   # 合成: equity legs = E*m_i, defensive = (1-E)*d_j
    params_hash: str = ""
    # 新: 锚层输出
    risk_weights: dict[str, float] = field(default_factory=dict)   # handcrafting 风险权重
    cash_weights: dict[str, float] = field(default_factory=dict)   # 逆波动率现金权重
    tilt_weights: dict[str, float] = field(default_factory=dict)   # 倾斜后最终权重


@dataclass
class ExecutionRecord:
    """月度执行记录。"""
    asof: date
    contribution_cny: float                     # 本月注入
    allocations: dict[str, float]               # leg → 本币金额 (扣成本后)
    costs: dict[str, float]                     # leg → 成本
    residuals: dict[str, float]                 # leg → 零钱
    weights_before: dict[str, float]            # 执行前权重
    weights_after: dict[str, float]             # 执行后权重
    nav_before: float = 0.0
    nav_after: float = 0.0


@dataclass
class AttributionRecord:
    """逐层归因记录。"""
    asof: date
    E_timing: float             # 旧 Layer 0 择时贡献 (兼容)
    regional_tilt: float        # 旧 Layer 1 地区倾斜贡献 (兼容)
    defensive_comp: float       # 旧 Layer 2 防御构成贡献 (兼容)
    style: float = 0.0          # Layer 3 (stub)
    residual: float = 0.0
    total: float = 0.0
    # 新: 锚 vs 倾斜归因
    anchor_return: float = 0.0           # 锚层回报
    tilt_incremental: float = 0.0        # 倾斜相对锚的增量 (核心验证目标)


@dataclass
class BacktestResult:
    """完整回测结果。"""
    # 净值序列 (TWR - 时间加权回报, 用于公平比较)
    strategy_nav: pd.Series = field(default_factory=pd.Series)
    benchmark_navs: dict[str, pd.Series] = field(default_factory=dict)
    # 总 NAV (含贡献, 用于实际价值追踪)
    total_nav: pd.Series = field(default_factory=pd.Series)

    # 权重历史
    weight_history: list[WeightSnapshot] = field(default_factory=list)

    # 执行记录
    executions: list[ExecutionRecord] = field(default_factory=list)

    # 归因
    attribution: list[AttributionRecord] = field(default_factory=list)

    # 成本汇总
    total_costs: float = 0.0
    cost_breakdown: dict[str, float] = field(default_factory=dict)

    # 元数据
    params_hash: str = ""
    start_date: str = ""
    end_date: str = ""

    def summary_df(self) -> pd.DataFrame:
        """核心指标汇总表。"""
        from .reporting.tables import compute_summary
        return compute_summary(self)

    def regime_df(self) -> pd.DataFrame:
        """政体分析表。"""
        from .reporting.regime import compute_regime_analysis
        return compute_regime_analysis(self)
