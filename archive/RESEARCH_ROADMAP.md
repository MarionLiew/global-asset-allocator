# Research Roadmap

当前框架（Layer 0-2）已完成并通过回测验证。以下为下一步研究规划（尚未实现）。

---

## 下一步：债务周期 + Regime 品种偏好

### 第一步：Regime 品种偏好（Ilmanen / Meb Faber）

将经济环境分为增长和通胀两个维度，按"季节"调整防御层品种权重：

| 象限 | 特征 | 超配品种 |
|---|---|---|
| 高增长 + 低通胀（Goldilocks） | 经济扩张，通胀温和 | 股票、REITs |
| 高增长 + 高通胀（Inflationary Boom） | 经济过热 | 大宗商品、黄金、TIPS |
| 低增长 + 高通胀（Stagflation） | 滞胀 | 黄金、TIPS、大宗商品 |
| 低增长 + 低通胀（Disinflationary Bust） | 通缩衰退 | 名义长债（政府债） |

当前 Layer 2 象限分类器（GG/GI/IG/II）已有雏形，但品种偏好权重尚未接入。

---

### 第二步：债务周期阶段识别（Ray Dalio 七阶段）

在 Regime 象限之上叠加债务周期位置信号：

| 阶段 | 信号特征 | 配置偏好 |
|---|---|---|
| 早期 / Goldilocks | 信贷健康，资产负债表扩张 | 重仓股票（牛市第一二阶段） |
| 泡沫期 | 估值远超价值，杠杆高 | 减仓股票 → 现金 + 短债 |
| 见顶期 | 央行加息，收益率曲线倒挂 | 增加现金 |
| 萧条 / Beautiful Deleveraging | 资产暴跌，央行印钞 | 反向买入跌幅最大股票 + 黄金 |

候选信号数据源（待确认可得性）：
- 收益率曲线形态（10Y-2Y 利差）
- 私人部门信贷增速（Bank of International Settlements）
- 股票估值偏离度（CAPE vs 历史中位）
- M2 增速

---

### 第三步：风格轮动（Style Tilts）

确定某市场总仓位后，在 ETF 内部做风格偏移：

| 周期位置 | 偏好风格 | 逻辑 |
|---|---|---|
| 底部 / 复苏初期 | 小盘股（Small-Cap） | 复苏弹性最大 |
| 周期中后期 | 价值股（Value） | 安全边际 |
| 崩盘期 | 动量（Momentum） | 趋势跟随作为分散化工具 |

这是 Layer 3 接口位（当前预留，未实现）的延伸。

---

## 工程约束（继承自 BACKTEST BRIEF.md）

- 所有新信号参数在回测前冻结，严禁为改善结果调参
- 债务周期分类器只能用当时可见的已实现数据，禁止前视
- 新增 Regime 层需要逐层归因，独立于现有层验证

---

## 参考文献

- Ilmanen, Antti — *Expected Returns* (2011)
- Faber, Meb — *Global Asset Allocation* (2015)
- Dalio, Ray — *Principles for Navigating Big Debt Crises* (2018)
