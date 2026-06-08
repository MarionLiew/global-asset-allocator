"""
配置 hash 校验测试。
"""

import pytest
import tempfile
import yaml
from pathlib import Path

from backtest.config import Params, compute_params_hash


def test_params_load():
    """能正确加载 params.yaml。"""
    # 需要一个临时的 params.yaml
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump({
            'version': 1,
            'E_base': 0.60,
            'k0': 0.20,
            'E_min': 0.40,
            'E_max': 0.80,
            'params_hash': '',
        }, f)
        path = f.name

    params = Params.load(path)
    assert params.E_base == 0.60
    assert params.k0 == 0.20


def test_hash_mismatch_raises():
    """修改 params.yaml 后不重新冻结, 应抛异常。"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump({
            'version': 1,
            'E_base': 0.60,
            'k0': 0.20,
            'params_hash': 'sha256:wrong_hash',
        }, f)
        path = f.name

    params = Params.load(path)
    with pytest.raises(ValueError, match="params_hash 不匹配"):
        params.verify_hash(path)


def test_hash_consistency():
    """相同文件生成相同 hash。"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump({
            'version': 1,
            'E_base': 0.60,
            'params_hash': '',
        }, f)
        path = f.name

    h1 = compute_params_hash(path)
    h2 = compute_params_hash(path)
    assert h1 == h2
