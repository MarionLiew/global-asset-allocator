# Global Asset Allocator

全球大类资产配置回测引擎：以风险平价为锚，叠加估值+动量的横截面倾斜，输出跨美股/A股/港股/防御资产的目标配置权重，并可直接换算成分账户入金金额。

## 核心逻辑

```
[1] 锚层     — handcrafting 逐层等风险，算出各资产的战略风险权重（零判断）
[2] 倾斜层   — 估值 + 动量组合信号，在锚权重基础上做 ±5pp 硬上限的横截面倾斜
[3] 现金权重 — 风险权重通过逆波动率（混合 EWMA）转换为实际现金配置比例
[4] 执行层   — 不交易区 + 成本驱动带宽，月度新钱优先填补欠配资产
```

详细设计见 [ALLOCATOR_PLAN.md](ALLOCATOR_PLAN.md)。

覆盖的资产：

| 类别 | 市场/资产 | ETF |
|---|---|---|
| 股票 | 美国 / 发达市场 / 中国A股 / 港股 | SPY / EFA / 510300 / 2800.HK |
| 防御 | 中国国债 / 美国TIPS / 黄金 / 投资级公司债 / 新兴市场债 | 511260 / TIP / GLD / LQD / EMB |

## 安装

```bash
pip install -e .
```

需要 Python ≥ 3.9。依赖见 [pyproject.toml](pyproject.toml)。

## 快速上手：拿到当下该配多少

```bash
# 只看比例（用已有本地数据，几秒钟出结果）
python scripts/current_allocation.py

# 先抓取最新市场数据再计算（ETF价格、CAPE估值、汇率全部刷新到最新月份）
python scripts/current_allocation.py --refresh

# 传入本次入金总额（人民币），直接算出每个账户该转多少钱
python scripts/current_allocation.py --total 500000
```

输出是按入金账户分组的树形结构，例如：

```
Schwab  58.66%
  ├─ US_equity     22.01%  (占该账户 37.5%)
  ├─ TIPS          12.50%  (占该账户 21.3%)
  ├─ DM_equity     11.28%  (占该账户 19.2%)
  ├─ EM_BOND        6.78%  (占该账户 11.6%)
  └─ CORP_BOND      6.08%  (占该账户 10.4%)

同花顺  34.22%
  ├─ CN_GOVT       20.77%  (占该账户 60.7%)
  └─ CN_equity     13.44%  (占该账户 39.3%)

ZA  7.10%
  └─ HK_equity      7.10%  (占该账户 100.0%)

OKX (XAUT现货)  0.02%
  └─ GOLD           0.02%  (占该账户 100.0%)
```

资产 → 入金账户的映射写死在 [scripts/current_allocation.py](scripts/current_allocation.py) 的 `ASSET_ACCOUNT` 字典里，按自己实际用的账户改一下即可：

- **Schwab**：美元现货 ETF（US/DM 股票 + TIPS/公司债/新兴市场债），零佣金长期持有
- **OKX（XAUT 现货）**：黄金敞口，用锚定实物黄金的现货代币而非永续合约，避免资金费率成本；但要承担代币发行方的对手方风险
- **同花顺**：A 股股票 + 中国国债 ETF，境内直接买，无换汇成本
- **ZA**：港股

⚠️ 大额资金走 Schwab 需注意个人年度 5 万美元购汇额度；超额部分可考虑天天基金 / 微众银行的对应 QDII 指数基金作为不占额度的替代通道。

## 完整回测（历史表现验证）

```bash
python scripts/run_backtest.py
```

用 [config/backtest.yaml](config/backtest.yaml) 里固定的 `start_date`/`end_date` 跑一段历史区间，输出核心指标（年化收益/波动/Sharpe/回撤）、相对基准的增量、以及三次危机（2000科技泡沫/2008金融危机/2022利率冲击）的 regime 分析，结果存到 `output/`。这是用来验证策略逻辑的，不是用来拿当下配置的（拿当下配置用 `current_allocation.py`）。

## 数据管道

```bash
python scripts/fetch_data.py            # 完整下载 + 构建 processed/
python scripts/fetch_data.py --skip-download   # 数据已下载，只重新构建 processed/
python scripts/fetch_data.py --synthetic       # 合成数据，离线/测试用
```

数据来源：
- ETF 价格：yfinance
- CN / CN_GOVT 长历史：`scripts/fetch_proxy_data.py` 用退市/场外代理拼接（见文件头注释的拼接优先级表）
- CAPE 估值：Shiller 数据。Yale 官方页面已于 2023-09 停止更新，[src/backtest/data/download.py](src/backtest/data/download.py) 里补了 multpl.com 作为最新月份的补充源
- 宏观指标（象限分类用）：FRED

`data/raw/` 已提交进仓库；`data/processed/` 由 `build_processed()` 从 raw 生成，不提交（见 `.gitignore`）。

## 参数冻结

`config/params.yaml` 里的策略参数改动后必须重新冻结哈希，否则回测会拒绝运行：

```bash
python scripts/freeze_params.py
```

## 目录结构

```
config/           策略参数(params.yaml) + 回测配置(backtest.yaml)
src/backtest/     引擎代码 — engine/ 锚层+倾斜层+执行层, data/ 数据管道, reporting/ 报表
scripts/          命令行入口
tests/            单测(含核心不变量测试)
data/raw/         原始数据(已提交)
data/processed/   处理后的 parquet(不提交, 由 fetch_data.py 生成)
output/           回测结果(不提交)
```

## 免责声明

本项目仅为个人回测/研究工具，输出的配置权重不构成投资建议。实际操作前请自行核实模型逻辑与数据准确性。
