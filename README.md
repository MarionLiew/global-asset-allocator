# Global Asset Allocator

全球大类资产配置回测引擎：以风险平价为锚，叠加估值+动量的横截面倾斜，输出跨美股/A股/港股/防御资产的目标配置权重，并可直接换算成分账户入金金额。

## 核心逻辑

```
[1] 锚层     — handcrafting 树每层等分风险，算出各资产的战略风险权重（零判断，不含 vol）
[2] 倾斜层   — 估值 + 动量组合信号，在风险空间对锚做 ±5pp 硬上限的横截面倾斜
[3] 现金权重 — 倾斜后的风险权重通过逆波动率（混合 EWMA，含 vol_floor 下限）转换为现金配置比例
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

## 被动配置 + 主动策略（卫星仓）

如果除了这套被动配置，还想拿一部分钱跑自己的量化策略（比如 Schwab/OKX 上的自动化交易接口），不要把它揉进被动配置的风险平价锚里——两者的收益特性不兼容，会污染锚层对波动率/相关性的估计。正确做法是把主动策略当独立的"卫星仓"，用 [scripts/satellite_allocation.py](scripts/satellite_allocation.py) 算它该占总资产多少。

参数全部放在 [config/satellite.yaml](config/satellite.yaml)，不用在命令行里堆一堆 `--xxx`：

```yaml
accounts:
  Schwab量化:
    strategies:
      - name: 示例策略A
        stats_path: null   # 可指向别的项目产出的 json: {"annual_vol":.., "sharpe":.., "track_months":..}
        vol: 0.15           # stats_path 缺失/文件不存在时用这几个手填值兜底
        sr: 0.0
        months: 0
  OKX量化:
    strategies:
      - name: 示例策略B
        vol: 0.30
        sr: 0.0
        months: 0
```

每个账户下可以放任意多个策略；`stats_path` 是留给你接其他项目回测/实盘结果的接口。

```bash
python scripts/satellite_allocation.py                # 只看比例
python scripts/satellite_allocation.py --total 500000  # 换算成具体金额
```

决策链条按 Robert Carver 的风险预算哲学实现，核心是"没有证据前不给权重，权重跟着实盘记录慢慢挣出来"：

1. **每个策略的夏普按实盘月数向"零 edge 先验"收缩**——刚上线、没有实盘记录的策略，夏普按 0 算，不管回测多好看；月数积累到 `confidence_months`（默认36个月）后才不再收缩。
2. **同账户内多策略、账户之间，都按波动率倒数做等风险权重**——不按夏普高低分配，因为短样本的夏普差异统计上通常不显著。
3. **主动策略整体的风险预算，从 `risk_floor`（默认5%）起步，随收缩后的组合夏普线性爬升到 `risk_cap_ceiling`（默认30%）**——`risk_cap_ceiling` 是满信心时的政策上限，不是起始值；数据不够时实际拿到的预算会远低于这个上限。
4. **用双资产风险贡献方程**（被动配置 vs 主动策略整体，波动率+相关性）反推出资金层面该怎么分，而不是直接用风险预算比例当资金比例（两者只有在波动率相近时才近似相等）。

输出跟 `current_allocation.py` 风格一致，被动部分展开成原来那棵账户树，主动部分展开成 账户 → 策略 两层：

```
被动配置  94.91%
  Schwab  55.67%  ...
  同花顺  32.47%  ...
  ...

主动策略  5.09%
  Schwab量化  3.39%
    └─ 示例策略A          3.39%  (占该账户 100.0%)
  OKX量化  1.70%
    └─ 示例策略B          1.70%  (占该账户 100.0%)

Polymarket  — 暂不参与风险预算, 待对冲策略确定后再纳入
```

Polymarket 暂时没有策略，只是留了个位置——等它被明确用作对冲工具（对冲什么风险、怎么对冲）后再纳入风险预算计算，不要在没想清楚定位前强行分配仓位。

## 月度运营（只买不卖）

首次建仓之后，日常用的是 [scripts/monthly_ops.py](scripts/monthly_ops.py)：

```bash
cp config/holdings.example.yaml config/holdings.yaml   # 首次: 建持仓文件
# 每月: 打开各账户抄一遍当前市值填进 holdings.yaml (本币计), 然后
python scripts/monthly_ops.py --new-money 10000        # 本月注入1万, 输出买入清单
```

规则与回测执行层同一套带宽逻辑：欠配且漂出不交易区的腿新钱优先补到带边缘，剩余按目标比例分配；超配的腿**只提示不卖出**，由后续新钱稀释——不产生卖出交易和税费。

`holdings.yaml` 是真实持仓（敏感数据），已 gitignore 不会被提交。

手填容易抄错数字，可以用 [scripts/verify_holdings.py](scripts/verify_holdings.py) 对 OKX / Schwab 的 API 做交叉校验（只需只读权限的 API Key，配置见 `config/api_credentials.example.yaml`，凭据文件同样 gitignore）：

```bash
cp config/api_credentials.example.yaml config/api_credentials.yaml   # 填入只读 API Key
python scripts/verify_holdings.py    # 手填 vs API, 相差>2% 报不一致
```

同花顺/ZA 没有可用的个人 API，这两个账户的数字靠手填时自查。

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

`fetch_data.py` 在构建后会自动跑 sanity check（CAPE 落在 10-60 区间、月度回报无 |r|>50% 异常、数据新鲜度），不通过直接报错退出——multpl 是网页抓取，页面改版时解析可能错位，这道闸门防止坏数据静默流入回测。

⚠️ **已知数据局限**：
- **CN_GOVT 的 2014 年之前历史是合成数据**（固定 3.5%/年 + 小噪声），没有真实的危机行为——2008 年它不会像真实国债那样避险上涨或遭遇流动性冲击。凡是覆盖 2014 年以前的回测结果，防御篮子的相关性/回撤特征都要打折扣看待。
- **估值信号只有美国有真实 CAPE 数据**。DM/CN/HK 的估值信号是中性的（0），这些市场的倾斜实际只由动量驱动。之前版本给非美市场写死了常数 CAPE，相当于注入永久性伪估值信号，已修复为"没有数据就不给信号"。
- **汇率数据从 2001 年起**。更早月份的境外资产回报按本币计（汇率视为不变），1994 年汇改后到 2001 年 USDCNY 基本盯住 8.28，影响有限。

`data/raw/` 已提交进仓库；`data/processed/` 由 `build_processed()` 从 raw 生成，不提交（见 `.gitignore`）。

## 参数冻结

`config/params.yaml` 里的策略参数改动后必须重新冻结哈希，否则回测会拒绝运行：

```bash
python scripts/freeze_params.py
```

## 目录结构

```
config/           策略参数(params.yaml) + 回测配置(backtest.yaml) + 主动策略卫星仓配置(satellite.yaml)
src/backtest/     引擎代码 — engine/ 锚层+倾斜层+执行层, data/ 数据管道, reporting/ 报表
scripts/          命令行入口
tests/            单测(含核心不变量测试)
data/raw/         原始数据(已提交)
data/processed/   处理后的 parquet(不提交, 由 fetch_data.py 生成)
output/           回测结果(不提交)
```

## 免责声明

本项目仅为个人回测/研究工具，输出的配置权重不构成投资建议。实际操作前请自行核实模型逻辑与数据准确性。
