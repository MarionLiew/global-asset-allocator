# allocator · 量化工程指导手册

> 面向 Claude Code 的实现规格。
> 本手册定义一个**组合构建 + 月度定投 + 再平衡**引擎,消费 `value-screener` 的输出,产出**目标权重**与**月度交易清单**。

---

## 0. 给 Claude Code 的硬约束(先读这一节)

实现与运行本系统时,**必须**遵守以下不可协商的规则。任何与之冲突的需求都应停下并报错,而不是绕过。

1. **只生成订单,不执行交易。** 系统的最终产物是一份订单清单(`orders/*.parquet`)。**严禁**调用任何券商/交易所下单 API、签名交易、转账或换汇。执行由人来完成。
2. **目标权重只按年重算。** `recompute-targets` 默认每年运行一次。**严禁**在月度流程里重算 CAPE/ERP 倾斜或调整目标权重。月度只做"把新钱买到最缺的腿"。
3. **参数事前冻结。** 策略参数存于 `config/params.yaml`,带 `params_hash`。任何流程启动时先校验 hash;不匹配则**拒绝运行**,除非显式传 `--allow-param-change "<理由>"` 并写入日志。**严禁**为了拟合历史收益去调参。
4. **月度模式只买不卖。** `run-monthly` 永不产生 SELL。卖出只在人主动运行 `plan-corrective` 时提议,且仍需人执行。
5. **PIT 纪律。** 计算目标权重(及读取 screener 的 IC/排名)时,只能用 `asof` 之前可见的数据(锚 `announcement_date`)。月度执行用实时价格,不涉及前视。
6. **现金守恒 + 整手。** 买入按可成交单位(A 股 100 股/手、港股按手、美股可配置碎股)向下取整;未花完的零钱进入该账户 `cash_buffer`,滚动到下月。任何流程不得"凭空"花掉或丢失现金。
7. **账户/币种边界真实存在。** 不能用 A 股账户的人民币去买美股。跨币种敞口只能通过"本月把新钱注入哪个账户"来调,见 §4.3。

---

## 1. 目的、范围与非目标

**目的:** 把已冻结的配置框架(第 0–2 层产出目标权重,第 3 层 + 执行产出订单)落成可复现的工程流程。

**范围:**
- 第 0 层:总股票预算 `E`(ERP 信号,有界微调)。
- 第 1 层:跨市场地区权重 `m_i`(各市场自身估值倾斜)。
- 第 2 层:防御腿构成 `d_j`(逆波动 ± 象限,单资产封顶)。
- 第 3 层:每个股票市场内 ETF vs 个股的路由(读 screener gate)。
- 执行:月度定投(现金流再平衡)、漂移检查、纠偏卖出提议、再平衡日志。

**非目标(本项目不做):**
- 不下单、不连券商、不换汇(见 §0.1)。
- 不做选股 —— 个股候选与 IC 来自 `value-screener`,本项目只消费其产物。
- 不做 13F/持仓追踪 —— 那是 `holdings-tracker` 的事。
- 不做回测 —— 推迟到 PIT-clean 数据到位后,另起项目。
- MVP 不做"市场内股/债 shading"(第 2 层的次级覆盖项),列为 Phase 2,见 §9。

**与既有仓库的耦合(松耦合,只经 parquet):**
```
value-screener  ──► ranks/{market}/{asof}.parquet  ─┐
                ──► gate/{market}.json              ─┼─► allocator ──► orders/{asof}.parquet
holdings-tracker ─► guru_signal.parquet ─► (screener) │              ──► state/post_trade_weights.parquet
                                                       │              ──► logs/rebalance_{asof}.json
config/params.yaml (frozen) ───────────────────────────┘
```
共享约定仅两条:**ticker 归一化格式**与 **PIT/announcement_date 口径**,各自的 `CLAUDE.md` 中已记录。

---

## 2. 三个时钟(回答"多久调整一次")

| 时钟 | 频率 | 命令 | 是否动用卖出 | 作用 |
|---|---|---|---|---|
| 目标重算 | **每年 1 次**(固定日历日) | `recompute-targets` | 否 | 用 PIT 数据重算 `E`、`m_i`、`d_j`,产 `targets/{year}.parquet`,然后冻结 |
| 月度定投 | **每月 1 次**(固定日) | `run-monthly` | **永不** | 现金流再平衡:把当月新钱买到最缺的腿 |
| 漂移检查 | 每月(并入月度)+ 年中手动 | `check-drift` | 否 | 报告各腿对目标的偏离与 band 状态 |
| 纠偏卖出 | **仅 band 突破时,人工触发** | `plan-corrective` | 提议卖出(人执行) | 当新钱无法收窄的超配腿突破 band 时提议卖出 |

**一句话:** 你每月都"碰"组合(注入新钱),但计划只每年重算一次,只有当某腿涨到突破 band、且月度买入收不回时,才动手卖。

**推荐日历:**
- `recompute-targets`:每年首个交易日,`asof` 取年初(用当时 PIT 可见的数据;不必等年报全部披露)。
- `run-monthly`:每月固定日(如发薪日次日)。
- 年中(6 月)手动跑一次 `check-drift`。
- band 突破时随时 `plan-corrective`。

---

## 3. 数据契约(Schemas)

所有 parquet 列名、类型固定;新增列向后兼容,删改列须升 schema 版本。

### 3.1 输入

**`targets/{year}.parquet`**(由 `recompute-targets` 产出,之后只读)
| 列 | 类型 | 说明 |
|---|---|---|
| leg_id | str | 腿标识,与 config 一致(如 `us_equity`) |
| sleeve | str | `equity` / `defensive` |
| target_weight | float | 占组合总值的目标权重 |
| asof | date | 计算基准日 |
| params_hash | str | 计算时的参数指纹 |

不变量:`sum(target_weight) == 1.0 ± 1e-6`。

**`value-screener` 产物(只读)**
- `ranks/{market}/{asof}.parquet`:`ticker, composite_score, rank, ic_used(可空), asof`
- `gate/{market}.json`:`{ market, icir_oos, n_obs, breadth, tau, passed: bool, asof }`

**`state/positions.parquet`**(当前持仓,人或同步脚本维护)
| 列 | 类型 | 说明 |
|---|---|---|
| account | str | 账户(与 config 一致) |
| ticker | str | 归一化 ticker;现金行用 `CASH` |
| leg_id | str | 所属腿 |
| shares | float | 持股数;现金行为名义额 |
| last_price_local | float | 最新本币价 |
| currency | str | `CNY`/`HKD`/`USD` |
| asof | date | |

**`state/funding.yaml`**(当月可注入的现金,按账户与币种)
```yaml
asof: 2026-07-02
funding:
  eastmoney: { currency: CNY, amount: 8000 }
  za_bank:   { currency: HKD, amount: 3000 }
  us_broker: { currency: USD, amount: 0 }     # 本月无 USD 可注入
```

**`fx/rates.parquet`** 或运行时拉取:`currency, to_cny, asof`(币种→人民币即期)。

### 3.2 输出

**`orders/{asof}.parquet`**
| 列 | 类型 | 说明 |
|---|---|---|
| account | str | |
| ticker | str | |
| leg_id | str | |
| side | str | 月度恒为 `BUY`;纠偏可为 `SELL` |
| amount_local | float | 计划本币金额 |
| est_shares | float | 整手取整后股数 |
| est_cost_local | float | 估算成交额 |
| residual_local | float | 进 cash_buffer 的零钱 |
| reason | str | 如 `cashflow_rebalance` / `band_breach_buy` / `corrective_sell` |

**`state/post_trade_weights.parquet`**:`leg_id, weight_before, weight_after, target, drift, band_status`(`OK`/`WARN`/`BREACH`)。

**`logs/rebalance_{asof}.json`**:完整决策记录 —— mode、contribution、funding 快照、`params_hash`、`targets_year`、各市场 gate 快照、所有 flag。供审计与"参数为何这样"追溯。

---

## 4. 核心算法(伪代码)

Python 风格;签名与 screener 的 `DataProvider` 协议风格一致。

### 4.1 目标权重(年度)

```python
def recompute_targets(asof: date, params: Params, md: MarketDataProvider) -> TargetsTable:
    # 第 0 层:总股票预算 E
    erp = md.earnings_yield_world(asof) - md.real_yield(asof)     # 1/CAPE_world - r_real
    erp_ref = md.erp_rolling_median(asof)                         # 滚动/扩张中位,见说明书 3.2
    E = clip(params.E_base + params.k0 * (erp / erp_ref - 1.0),
             params.E_base - 0.10, params.E_base + 0.10)          # ±10pp 微调带
    E = clip(E, params.E_min, params.E_max)

    # 第 1 层:跨市场地区权重 m_i(按各市场自身历史的便宜程度)
    raw = {}
    for i in md.equity_markets():
        cape, cape_t = md.cape(i, asof), md.cape_target(i, asof)
        lam = params.lambda_[i]                                    # 发达 0.5-0.8;中/港 0.2-0.4
        raw[i] = md.cap_weight(i, asof) * (cape_t / cape) ** lam
    m = clip_to_band(raw, md.cap_weights(asof), band=params.band_pp)  # ±15pp(相对市值)
    m = normalize(m)
    m = add_home_tilt(m, params.delta_home)                        # 中/港有界加点,叠加后仍受 band 约束
    m = normalize(m)

    # 第 2 层:防御腿构成 d_j(逆波动 ± 象限,单资产封顶)
    d = {j: 1.0 / md.vol(j, asof) for j in md.defensive_assets()}
    d = apply_quadrant_tilt(d, md.growth_inflation_quadrant(asof), delta=params.delta_quadrant)  # ±8pp
    d = cap_and_renormalize(d, cap=params.defensive_single_asset_cap)  # 单资产 ≤ 40-50%

    legs = {f"{i}_equity": E * w for i, w in m.items()}
    legs |= {f"{j}": (1 - E) * w for j, w in d.items()}
    assert abs(sum(legs.values()) - 1.0) < 1e-6
    return TargetsTable(legs, asof, params.hash)
```

> 注意 ER_i 公式(`DY_i + g_real_i + (1/H)·ln(CAPE_target/CAPE_i)`)仅用于**排序与 sanity check**,不直接定权重;权重由上式的倾斜决定。**不要**把 `1/CAPE` 与 `g` 同时计入 ER。

### 4.2 当前权重

```python
def current_weights(positions, fx, asof) -> dict[str, float]:
    mv_cny = {}
    for row in positions:                 # 含 CASH 行
        rate = fx.to_cny(row.currency, asof)
        mv_cny[row.leg_id] = mv_cny.get(row.leg_id, 0) + row.shares * row.last_price_local * rate
    total = sum(mv_cny.values())
    return {leg: v / total for leg, v in mv_cny.items()}, total
```

### 4.3 月度定投(现金流再平衡)—— 回答"每月定投怎么落实"

核心:**按账户分桶**,每桶内把当月新钱按"缺口"比例买到最缺的腿。跨账户(跨币种)缺口**不**由月度新钱修正,只记录、留给年度或未来该币种的注资。

```python
def run_monthly(asof, targets, positions, funding, fx, gate, ranks, cfg, params) -> Orders:
    assert params.hash == cfg.params_hash, "params_hash 不匹配,拒绝运行"   # §0.3

    w_now, V = current_weights(positions, fx, asof)
    C_total_cny = sum(f.amount * fx.to_cny(f.currency, asof) for f in funding)
    T = V + C_total_cny                                                    # 注资后总值

    orders = []
    for account, f in funding.items():                                     # 逐账户(逐币种)
        legs = cfg.legs_in(account)
        cash_local = f.amount + cfg.cash_buffer(account)                   # 含上月滚存零钱
        # 该账户内各腿的缺口(以人民币计,再换回本币分配)
        shortfall = {}
        for leg in legs:
            target_cny = targets[leg].target_weight * T
            now_cny    = w_now.get(leg, 0.0) * V
            shortfall[leg] = max(0.0, target_cny - now_cny)
        S = sum(shortfall.values())
        if S == 0:                                                         # 该账户都不缺 → 钱进 buffer
            cfg.set_cash_buffer(account, cash_local); continue
        for leg in legs:
            if shortfall[leg] == 0: continue
            alloc_cny   = cash_local * fx.to_cny(f.currency, asof) * shortfall[leg] / S
            alloc_local = alloc_cny / fx.to_cny(f.currency, asof)
            orders += route_within_market(leg, alloc_local, gate, ranks, cfg, params, asof)
        cfg.roll_residual_to_buffer(account, orders)                       # §0.6 现金守恒

    log_decision(asof, mode="monthly", funding=funding, orders=orders, params=params)
    return Orders(orders)
```

**关键点:**
- 当月新钱通常 < 总漂移,所以是"按缺口比例填",不是"填满"。这正是低成本、免卖出的现金流再平衡。
- `us_broker` 本月 `amount: 0` → 美股腿即使缺口最大也不动;记录为跨账户未收窄缺口,留待年度或未来 USD 注资。这是资本管制/分账户的真实约束,**不可**用人民币去补。

### 4.4 市场内路由(第 3 层门控)

```python
def route_within_market(leg, amount_local, gate, ranks, cfg, params, asof):
    cfg_leg = cfg.legs[leg]
    if cfg_leg.sleeve == "defensive":
        return [lot_round_order(leg, cfg_leg.default_instrument, amount_local, cfg_leg, "cashflow_rebalance")]

    market = cfg_leg.market
    g = gate.get(market)
    passed = g and g.passed and g.n_obs >= params.n_min and g.breadth >= params.breadth_min \
                 and g.icir_oos > params.tau                                    # 三重门控
    if not passed:
        return [lot_round_order(leg, cfg_leg.default_instrument, amount_local, cfg_leg, "cashflow_rebalance")]

    # 门控通过 → 买 screener 个股,主动权重 ∝ 收缩后 IC,分数 Kelly 缩放,封顶
    names = ranks[market]                                                       # 已按 composite_score 排序
    ic_used = {n.ticker: n.ic_used * (g.icir_oos / (g.icir_oos + 1)) for n in names}  # IC 收缩
    weights = kelly_tilt(ic_used, f=params.kelly_fraction, cap=params.active_name_cap)
    return [lot_round_order(leg, tkr, amount_local * w, cfg_leg, "cashflow_rebalance")
            for tkr, w in weights.items()]
```

```python
def lot_round_order(leg, ticker, amount_local, cfg_leg, reason):
    price = quote(ticker)                       # 实时价
    if cfg_leg.allow_fractional:
        shares = amount_local / price
    else:
        lot = cfg_leg.lot_size                  # A股100 / 港股按手
        shares = floor(amount_local / (price * lot)) * lot
    cost = shares * price
    return Order(cfg_leg.account, ticker, leg, "BUY",
                 amount_local, shares, cost, residual=amount_local - cost, reason=reason)
```

### 4.5 漂移检查与纠偏(卖出仅人工触发)

```python
def check_drift(asof, targets, positions, fx, params):
    w_now, _ = current_weights(positions, fx, asof)
    rows = []
    for leg, tgt in targets.items():
        drift = w_now.get(leg, 0) - tgt.target_weight
        rel = abs(drift) / tgt.target_weight if tgt.target_weight else 0
        status = "BREACH" if (rel > params.band_rel or abs(drift) > params.band_abs) else \
                 "WARN" if rel > 0.8 * params.band_rel else "OK"
        rows.append((leg, w_now.get(leg,0), tgt.target_weight, drift, status))
    return rows                                  # 写 post_trade_weights.parquet

def plan_corrective(asof, ...):                  # 人工触发;产出 SELL 提议,仍由人执行
    # 仅对 BREACH 的超配腿,在同账户内提议卖出至目标;跨账户不自动处理
    ...
```

---

## 5. 配置(冻结)

**`config/params.yaml`**(策略参数,带 hash;改动须留痕)
```yaml
version: 1
params_hash: "sha256:..."         # 由 CI 校验
E_base: 0.60
k0: 0.2
E_min: 0.40
E_max: 0.80
lambda_: { US: 0.6, DM: 0.6, CN: 0.3, HK: 0.3 }
band_pp: 0.15                     # 地区权重相对市值偏离上限
delta_home: 0.03
H: 10
delta_quadrant: 0.08
defensive_single_asset_cap: 0.45
tau: 0.5                          # ICIR 门控阈值(占位,需你定且显著)
n_min: 24                         # 最小独立观测期数(占位)
breadth_min: 20                   # 占位
kelly_fraction: 0.25
active_name_cap: 0.10
band_rel: 0.25
band_abs: 0.05
```

**`config/allocator.yaml`**(账户与腿映射;非策略参数)
```yaml
base_currency: CNY
accounts:
  eastmoney: { currency: CNY, can_fund_monthly: true }
  za_bank:   { currency: HKD, can_fund_monthly: true }
  us_broker: { currency: USD, can_fund_monthly: false }   # 券商待定,见 §8
legs:
  us_equity: { sleeve: equity, market: US, account: us_broker, default_instrument: VTI,     lot_size: 1,   allow_fractional: true }
  dm_equity: { sleeve: equity, market: DM, account: us_broker, default_instrument: VEA,     lot_size: 1,   allow_fractional: true }
  cn_equity: { sleeve: equity, market: CN, account: eastmoney, default_instrument: "512890", lot_size: 100, allow_fractional: false }
  hk_equity: { sleeve: equity, market: HK, account: za_bank,   default_instrument: "2800.HK", lot_size: 500, allow_fractional: false }
  cn_govt:   { sleeve: defensive, asset: CN_GOVT, account: eastmoney, default_instrument: "511260", lot_size: 100 }
  gold:      { sleeve: defensive, asset: GOLD,    account: eastmoney, default_instrument: "518880", lot_size: 100 }
# TIPS / EM 债等按可得工具补充
cash_buffer: { eastmoney: 0, za_bank: 0, us_broker: 0 }
```

---

## 6. 包结构与 CLI

```
allocator/
  __init__.py
  config.py            # 加载 + 校验 params_hash
  providers/
    market_data.py     # MarketDataProvider 协议 + stub(CAPE/ERP/vol/象限)
    fx.py
  targets.py           # recompute_targets (§4.1)
  weights.py           # current_weights (§4.2)
  monthly.py           # run_monthly (§4.3)
  routing.py           # route_within_market, lot_round_order (§4.4)
  drift.py             # check_drift, plan_corrective (§4.5)
  io.py                # parquet/json 读写,schema 校验
  cli.py               # 入口
tests/
config/
state/  targets/  orders/  logs/  fx/
CLAUDE.md
```

```bash
allocator recompute-targets --asof 2026-01-02 [--allow-param-change "理由"]
allocator run-monthly       --asof 2026-07-02 --funding state/funding.yaml
allocator check-drift       --asof 2026-07-02
allocator plan-corrective   --asof 2026-07-02        # 人工触发,产出 SELL 提议
```

---

## 7. Claude Code Skills

放 `.claude/skills/` 下,与现有 `run-screen` / `add-provider` 同构。

**`recompute-targets/SKILL.md`** — "用 PIT 数据重算年度目标权重并冻结。先校验 params_hash;只用 asof 前可见数据;产 `targets/{year}.parquet` 并断言权重和为 1。绝不在此外的时间重算目标。"

**`run-monthly/SKILL.md`** — "执行月度现金流再平衡。读冻结的 targets + funding,按账户分桶把新钱买到最缺的腿;只买不卖;整手取整、零钱滚存;经第 3 层门控路由 ETF/个股;产 orders 与日志。不下单。"

**`check-drift/SKILL.md`** — "对比当前权重与目标,输出各腿 drift 与 band 状态;只读,不产订单。"

---

## 8. 待你确认的假设(占位,可覆盖)

实现时按下列默认推进,但这些是你应核对或改写的点(已在 config 中以占位值体现):

1. **跨币种注资机制。** 假设你每月分别向 `eastmoney`(CNY)、`za_bank`(HKD)注资,`us_broker`(USD)视当月有无 USD 决定。引擎据此把跨市场再平衡限制在"本月注入哪个账户"。若你能自由换汇/跨境划转,可放开 §4.3 的账户约束。
2. **美股券商未定。** `us_broker` 暂为占位(影响碎股、可注资性)。定下后更新 `allocator.yaml`。
3. **腿→工具映射。** 默认 ETF 用了占位代码(VTI/VEA/512890/2800.HK/511260/518880),按你实际可买标的替换。
4. **HK 经 ZA Bank 以 HKD 成交、整手**;A 股 100 股/手。如有差异改 `lot_size`。
5. **第 2 层"市场内股/债 shading"** MVP 不实现(见 §9)。

---

## 9. 分阶段实现

- **Phase 1(MVP):** §4.1–4.5 全链路 + §3 契约 + §5 配置校验 + §10 测试。先用 stub MarketDataProvider(CAPE/ERP/vol 走手填 CSV 或 AKShare),screener gate 全部 `passed=false` → 所有股票腿走 ETF。端到端跑通"年度目标 → 月度订单"。
- **Phase 2:** 接 screener gate/ranks,开个股路由;接真实 MarketDataProvider;加 `plan-corrective` 的同账户卖出优化。
- **Phase 3:** 第 2 层市场内股/债 shading 覆盖项;成本/税费进入 band 校准;接 `holdings-tracker` 的 guru 信号(经 screener)。

---

## 10. 测试与不变量(必须有的 test)

最小测试集 + 必须始终成立的断言:

1. `recompute_targets`:输出权重和 = 1.0(±1e-6);`E` 落在 `[E_base±10pp]` ∩ `[E_min,E_max]`;防御单资产 ≤ cap。
2. `run_monthly`:**无任何 SELL**;每账户支出 ≤ 当月可用现金 + buffer;`Σ(est_cost) + Σ(residual) == Σ(分配额)`(现金守恒);整手腿的 `est_shares % lot == 0`。
3. 跨账户:`us_broker` funding=0 时,不产生任何 `us_broker` 订单,且日志含"跨账户未收窄缺口"flag。
4. FX 恒等:任一币种 `to_cny>0`;组合总值 `T = V + ΣC_cny` 与逐腿换算一致。
5. 门控:gate `passed=false` 或 `n_obs<n_min` → 该市场只出 ETF 订单,无个股。
6. params_hash 不匹配且未传 `--allow-param-change` → 进程以非零码退出,不产订单。
7. 现金流再平衡方向性:构造一个"某腿明显低配"的 fixture,断言当月新钱优先流向该腿。

---

## 11. 与说明书风险点的对应

本引擎是说明书第 8 章"执行与再平衡"的落地,并直接对冲其点名的风险:

- **参数冻结纪律被突破**(说明书最大单点失效)→ §0.3 + §10.6 用 `params_hash` 校验把它变成 CI 级硬约束。
- **成本未进 band** → Phase 3 把成本/税费纳入 `band` 校准;在此之前订单日志保留 `est_cost` 供事后核。
- **跨市场缺口无法用新钱收窄** → 引擎不隐藏,显式记 flag,交由年度/未来注资处理,绝不用错币种资金硬补。
