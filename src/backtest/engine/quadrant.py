"""
象限分类器 — 增长/通胀四象限。

用当时可见 + 含发布滞后 (lag_months) 的已实现数据。
象限:
  GG = 高增长低通胀 (Goldilocks)
  GI = 高增长高通胀
  IG = 低增长低通胀 (Secular Stagnation)
  II = 低增长高通胀 (Stagflation)

象限决定防御腿的倾斜方向:
  GG → 偏信用债 (corporate bond)
  GI → 偏商品/黄金
  IG → 偏国债
  II → 偏黄金/TIPS
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..data.provider import MarketDataProvider


# 象限 → 防御资产倾斜加减
# delta_j: + 表示增加该资产权重, - 表示减少
QUADRANT_TILTS: dict[str, dict[str, float]] = {
    "GG": {  # 高增长低通胀 → 偏信用债
        "CORP_BOND": +0.08,
        "CN_GOVT": -0.04,
        "GOLD": -0.04,
    },
    "GI": {  # 高增长高通胀 → 偏黄金
        "GOLD": +0.08,
        "CORP_BOND": -0.04,
        "TIPS": +0.04,
        "EM_BOND": -0.04,
        "CN_GOVT": -0.04,
    },
    "IG": {  # 低增长低通胀 → 偏国债
        "CN_GOVT": +0.06,
        "TIPS": +0.02,
        "GOLD": -0.04,
        "CORP_BOND": -0.04,
    },
    "II": {  # 滞胀 → 偏黄金/TIPS
        "GOLD": +0.06,
        "TIPS": +0.04,
        "CN_GOVT": -0.04,
        "CORP_BOND": -0.04,
        "EM_BOND": -0.02,
    },
}


def apply_quadrant_tilt(
    d: dict[str, float],
    quadrant: str,
    delta: float = 0.08,
) -> dict[str, float]:
    """对防御资产权重施加象限倾斜。

    参数:
        d: 原始防御资产权重 (归一化后)
        quadrant: "GG" / "GI" / "IG" / "II"
        delta: 最大倾斜幅度 (默认 8pp)

    返回: 倾斜后权重 (未重新归一化)
    """
    tilts = QUADRANT_TILTS.get(quadrant, {})
    result = dict(d)

    for asset, tilt_raw in tilts.items():
        if asset in result:
            # 按 delta 缩放倾斜
            tilt = tilt_raw * (delta / 0.08)  # 标准化到 delta 参数
            result[asset] = max(0.0, result[asset] + tilt)

    return result
