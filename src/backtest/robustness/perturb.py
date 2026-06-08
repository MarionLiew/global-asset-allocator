"""
参数扰动生成器 — 为稳健性测试生成参数变体。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..config import Params


@dataclass
class PerturbationSpec:
    """参数扰动规格。"""
    param_name: str
    base_value: float
    perturbations: list[float]  # 绝对值列表


# 默认扰动规格
DEFAULT_SPECS = [
    PerturbationSpec("E_base", 0.60, [0.50, 0.55, 0.60, 0.65, 0.70]),
    PerturbationSpec("k0", 0.20, [0.10, 0.15, 0.20, 0.25, 0.30]),
    PerturbationSpec("band_pp", 0.15, [0.10, 0.12, 0.15, 0.18, 0.20]),
    PerturbationSpec("delta_quadrant", 0.08, [0.04, 0.06, 0.08, 0.10, 0.12]),
    PerturbationSpec("defensive_single_asset_cap", 0.45, [0.35, 0.40, 0.45, 0.50, 0.55]),
    PerturbationSpec("delta_home", 0.03, [0.00, 0.02, 0.03, 0.04, 0.05]),
]


def generate_perturbations(
    base_params: Params,
    specs: list[PerturbationSpec] | None = None,
) -> list[tuple[str, float, Params]]:
    """生成参数扰动列表。

    返回: [(param_name, perturbed_value, perturbed_params), ...]
    每次只扰动一个参数, 其他保持 base 值。
    """
    specs = specs or DEFAULT_SPECS
    variants = []

    for spec in specs:
        for val in spec.perturbations:
            if val == spec.base_value:
                # 基准值也加入 (作为对照)
                variants.append((spec.param_name, val, base_params))
                continue

            # 创建扰动后的 Params
            kwargs = {}
            # lambda_ 是 dict, 需要特殊处理
            if spec.param_name == "lambda_US":
                new_lambda = dict(base_params.lambda_)
                new_lambda["US"] = val
                kwargs["lambda_"] = new_lambda
            else:
                kwargs[spec.param_name] = val

            perturbed = replace(base_params, **kwargs)
            variants.append((spec.param_name, val, perturbed))

    return variants
