"""
混合 EWMA 波动率估算。

sigma = w * sigma_fast + (1-w) * sigma_slow
halflife → alpha: alpha = 1 - exp(-ln2 / halflife)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def mixed_ewma_vol(
    returns: pd.Series,
    fast_halflife: int = 6,
    slow_halflife: int = 36,
    mix_weight: float = 0.7,
    min_periods: int = 12,
) -> float:
    """计算混合 EWMA 波动率 (年化)。

    参数:
        returns: 月度回报序列
        fast_halflife: 快速 EWMA 半衰期 (月)
        slow_halflife: 慢速 EWMA 半衰期 (月)
        mix_weight: 快速权重 (0.7*fast + 0.3*slow)
        min_periods: 最少观察期数

    返回: 年化波动率 (float)
    """
    if len(returns) < min_periods:
        # 数据不足, 用简单标准差
        return float(returns.std() * np.sqrt(12)) if len(returns) > 1 else 0.15

    fast_alpha = 1 - np.exp(-np.log(2) / fast_halflife)
    slow_alpha = 1 - np.exp(-np.log(2) / slow_halflife)

    fast_var = returns.ewm(alpha=fast_alpha, min_periods=min_periods).var().iloc[-1]
    slow_var = returns.ewm(alpha=slow_alpha, min_periods=min_periods).var().iloc[-1]

    mixed_var = mix_weight * fast_var + (1 - mix_weight) * slow_var
    return float(np.sqrt(mixed_var) * np.sqrt(12))  # 月方差 → 年化标准差


def ewma_vol_series(
    returns: pd.Series,
    fast_halflife: int = 6,
    slow_halflife: int = 36,
    mix_weight: float = 0.7,
    min_periods: int = 12,
) -> pd.Series:
    """返回整条混合 EWMA 波动率序列 (年化), 与 returns 索引对齐。"""
    fast_alpha = 1 - np.exp(-np.log(2) / fast_halflife)
    slow_alpha = 1 - np.exp(-np.log(2) / slow_halflife)

    fast_var = returns.ewm(alpha=fast_alpha, min_periods=min_periods).var()
    slow_var = returns.ewm(alpha=slow_alpha, min_periods=min_periods).var()

    mixed_var = mix_weight * fast_var + (1 - mix_weight) * slow_var
    return np.sqrt(mixed_var) * np.sqrt(12)
