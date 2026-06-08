#!/usr/bin/env python3
"""
冻结 params.yaml: 计算 sha256 hash 写回 params_hash 字段。

运行: python scripts/freeze_params.py
冻结后任何改动必须重新运行并注明理由。
"""

import hashlib
import sys
from pathlib import Path

import yaml


def main():
    path = Path(__file__).parent.parent / "config" / "params.yaml"
    if not path.exists():
        print(f"错误: {path} 不存在", file=sys.stderr)
        sys.exit(1)

    raw = path.read_text()
    # 排除 params_hash 行本身
    lines = [l for l in raw.splitlines() if not l.lstrip().startswith("params_hash")]
    h = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]
    hash_str = f"sha256:{h}"

    data = yaml.safe_load(raw)
    data["params_hash"] = hash_str
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"✅ params_hash 已冻结: {hash_str}")
    print(f"   文件: {path}")


if __name__ == "__main__":
    main()
