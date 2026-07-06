"""
锚层 — Carver handcrafting: 分组逐层等风险, 零判断。

ALLOCATOR_PLAN §一A:
  1. 顶层: attack/defense 按固定风险比分配 (config 冻结)
  2. 树的每一层: 非空子组等分风险, 组内叶子等分风险
  3. 返回每个叶子的风险权重 risk_weight_i (总和=1)

波动率不进入风险权重 — 只在 risk_to_cash 的风险→现金转换中出现一次。

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
    """子组内等风险: 每个叶子等分风险权重 (1/n)。

    此前这里用 1/vol 分配, 与 risk_to_cash 的再次除 vol 叠加成 1/vol²,
    极端超配低波动资产。风险权重每层纯等分才是 handcrafting 的本意。
    """
    if not assets:
        return {}
    n = len(assets)
    return {a: 1.0 / n for a in assets}


def _subtree_risk_weights(
    subtree: dict[str, list[str]],
    provider: "MarketDataProvider",
    asof: date,
) -> dict[str, float]:
    """子树风险权重: 非空子组等分, 组内叶子等分 (子树内部总和=1)。"""
    result: dict[str, float] = {}

    active_groups = [g for g, members in subtree.items() if members]
    if not active_groups:
        return result

    group_w = 1.0 / len(active_groups)
    for group_name in active_groups:
        internal = _equal_risk_weights(subtree[group_name], provider, asof)
        for a, w in internal.items():
            result[a] = group_w * w

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
