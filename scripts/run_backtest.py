#!/usr/bin/env python3
"""
主回测入口。

用法:
    python scripts/run_backtest.py
    python scripts/run_backtest.py --config config/backtest.yaml
"""

import argparse
import logging
import sys
from pathlib import Path

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from backtest.config import Params, BacktestConfig
from backtest.data.csv_provider import CSVProvider
from backtest.engine.backtest_loop import run_backtest_v2
from backtest.reporting.tables import compute_summary, compute_incremental
from backtest.reporting.regime import compute_regime_analysis

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="宏观叠加层回测")
    parser.add_argument("--params", default="config/params.yaml", help="参数文件")
    parser.add_argument("--config", default="config/backtest.yaml", help="回测配置")
    parser.add_argument("--data-dir", default=str(Path(__file__).parent.parent), help="数据目录 (项目根目录)")
    parser.add_argument("--allow-param-change", type=str, default=None,
                        help="允许参数变更的理由")
    parser.add_argument("--output", default="output", help="输出目录")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # 加载配置
    logger.info("加载配置...")
    params = Params.load(args.params)
    bt_cfg = BacktestConfig.load(args.config)

    # 校验参数 hash
    if args.allow_param_change:
        logger.warning(f"⚠️  参数变更已允许: {args.allow_param_change}")
    else:
        try:
            params.verify_hash(args.params)
            logger.info(f"✅ params_hash 校验通过: {params.params_hash}")
        except ValueError as e:
            logger.error(f"❌ {e}")
            sys.exit(1)

    # 加载数据
    logger.info("加载数据...")
    md = CSVProvider(args.data_dir, params)

    # 运行回测
    logger.info("开始回测...")
    result = run_backtest_v2(params, bt_cfg, md)

    # 输出结果
    output_dir = Path(args.output)
    output_dir.mkdir(exist_ok=True)

    # 汇总表
    summary = compute_summary(result)
    logger.info("\n=== 核心指标 ===")
    logger.info(f"\n{summary.to_string()}")

    # 增量
    incremental = compute_incremental(result)
    if not incremental.empty:
        logger.info("\n=== 相对基准增量 ===")
        logger.info(f"\n{incremental.to_string()}")

    # Regime 分析
    regime = compute_regime_analysis(result)
    logger.info("\n=== Regime 分析 ===")
    logger.info(f"\n{regime.to_string()}")

    # 保存 NAV 序列
    result.strategy_nav.to_csv(output_dir / "strategy_nav.csv")
    for name, nav in result.benchmark_navs.items():
        nav.to_csv(output_dir / f"benchmark_{name}_nav.csv")
    result.total_nav.to_csv(output_dir / "total_nav.csv")

    # 保存结果表
    summary.to_csv(output_dir / "summary.csv")
    regime.to_csv(output_dir / "regime_analysis.csv")
    if not incremental.empty:
        incremental.to_csv(output_dir / "incremental.csv")

    logger.info(f"\n结果已保存到 {output_dir}/")
    logger.info(f"总交易成本: ¥{result.total_costs:,.0f}")


if __name__ == "__main__":
    main()
