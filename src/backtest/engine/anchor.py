"""
锚层 — Carver handcrafting: 分组逐层等风险, 零判断。

ALLOCATOR_PLAN §一A:
  1. 顶层: attack/defense 按固定风险比分配 (config 冻结)
  2. 每个子组内: 等风险贡献 (1/vol_j 归一化)
  3. 返回每个叶子的风险权重 risk_weight_i (总和=1)

不变量:
  - 所有 risk_weights 之和 = 1.0
  - attack 总风险权重 = attack_defense_ratio
  - defense 总风险权重 = 1 - attack_defense_ratio
  - 零判断: 不依赖任何信号
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..data._constants import GROUP_TREE

if TYPE_CHECKING:
    from ..config import Params
    from ..data.provider import MarketDataProvider


def _equal_risk_weights(
    assets: list[str],
    provider: "MarketDataProvider",
    asof: date,
) -> dict[str, float]:
    """子组内等风险贡献: risk_weight_j ∝ 1/vol_j, 归一化到总和=1。"""
    if not assets:
        return {}

    inv_vols: dict[str, float] = {}
    for a in assets:
        v = provider.vol(a, asof)
        inv_vols[a] = 1.0 / v if v > 0 else 0.0

    total = sum(inv_vols.values())
    if total <= 0:
        # fallback: 等权
        n = len(assets)
        return {a: 1.0 / n for a in assets}

    return {a: iv / total for a, iv in inv_vols.items()}


def _subtree_risk_weights(
    subtree: dict[str, list[str]],
    provider: "MarketDataProvider",
    asof: date,
) -> dict[str, float]:
    """递归计算子树的风险权重 (子树内部归一化到总和=1)。"""
    result: dict[str, float] = {}

    # 每个子组的组内等风险权重
    group_internal: dict[str, dict[str, float]] = {}
    for group_name, members in subtree.items():
        group_internal[group_name] = _equal_risk_weights(members, provider, asof)

    # 子组间的权重: 按子组的平均波动率反比分配
    # (等风险贡献在组间层面: 波动率高的组获得更低权重)
    group_avg_vol: dict[str, float] = {}
    for group_name, members in subtree.items():
        if not members:
            group_avg_vol[group_name] = float("inf")
            continue
        vols = [provider.vol(a, asof) for a in members]
        group_avg_vol[group_name] = sum(vols) / len(vols) if vols else float("inf")

    # 组间等风险: 1/avg_vol, 归一化
    active_groups = {g: v for g, v in group_avg_vol.items() if v < float("inf") and v > 0}
    if not active_groups:
        # fallback: 等权
        for group_name, members in subtree.items():
            n = len(members)
            if n > 0:
                for a in members:
                    result[a] = 1.0 / n / len(subtree)
        return result

    inv_avg = {g: 1.0 / v for g, v in active_groups.items()}
    total_inv = sum(inv_avg.values())
    group_weights = {g: iv / total_inv for g, iv in inv_avg.items()}

    # 合成: 叶子 = 组间权重 * 组内权重
    for group_name, members in subtree.items():
        gw = group_weights.get(group_name, 0.0)
        internal = group_internal.get(group_name, {})
        for a in members:
            result[a] = gw * internal.get(a, 0.0)

    return result


def compute_anchor_risk_weights(
    provider: "MarketDataProvider",
    params: "Params",
    asof: date,
) -> dict[str, float]:
    """锚层: Carver handcrafting, 返回每个叶子的风险权重 (总和=1)。

    流程:
    1. 计算 attack 子树内部风险权重 (归一化到 1)
    2. 计算 defense 子树内部风险权重 (归一化到 1)
    3. 顶层: attack = attack_defense_ratio, defense = 1 - ratio
    4. 叶子风险权重 = 顶层比例 × 子树内部权重
    """
    tree = GROUP_TREE

    # 子树内部权重 (各自归一化到 1)
    attack_internal = _subtree_risk_weights(tree["attack"], provider, asof)
    defense_internal = _subtree_risk_weights(tree["defense"], provider, asof)

    # 顶层分配
    attack_share = params.attack_defense_ratio
    defense_share = 1.0 - attack_share

    result: dict[str, float] = {}
    for a, w in attack_internal.items():
        result[a] = attack_share * w
    for a, w in defense_internal.items():
        result[a] = defense_share * w

    # 归一化 (处理空子组等边界情况)
    total = sum(result.values())
    if total > 0 and abs(total - 1.0) > 1e-9:
        result = {a: w / total for a, w in result.items()}

    return result
