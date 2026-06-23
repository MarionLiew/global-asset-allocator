"""
市场/资产常量定义。
"""

# 股票市场
EQUITY_MARKETS = ["US", "DM", "CN", "HK"]

# 防御资产
DEFENSIVE_ASSETS = ["CN_GOVT", "TIPS", "GOLD", "CORP_BOND", "EM_BOND"]

# 所有腿 (equity + defensive)
ALL_LEGS = [f"{m}_equity" for m in EQUITY_MARKETS] + DEFENSIVE_ASSETS

# ── 分组树（按风险来源分组，见 ALLOCATOR_PLAN.md §二） ──────────────────────
GROUP_TREE = {
    "attack": {
        "equity":  ["US", "DM", "CN", "HK"],
        "crypto":  [],   # 本轮留空，2015 子样本单独评估
    },
    "defense": {
        "rates":       ["CN_GOVT", "TIPS"],
        "real_credit": ["GOLD", "CORP_BOND", "EM_BOND"],
    },
}

# ── 动量参数 ─────────────────────────────────────────────────────────────────
MOMENTUM_LOOKBACK = 12   # 回看窗口（月）
MOMENTUM_SKIP = 1        # 跳过最近 N 个月（避免短期反转）

# 市场 → 货币
MARKET_CURRENCY = {
    "US": "USD",
    "DM": "USD",   # EFA 以 USD 计价
    "CN": "CNY",
    "HK": "HKD",
    "CN_GOVT": "CNY",
    "TIPS": "USD",
    "GOLD": "USD",
    "CORP_BOND": "USD",
    "EM_BOND": "USD",
}

# 腿 → sleeve
LEG_SLEEVE = {
    "US_equity": "equity",
    "DM_equity": "equity",
    "CN_equity": "equity",
    "HK_equity": "equity",
    "CN_GOVT": "defensive",
    "TIPS": "defensive",
    "GOLD": "defensive",
    "CORP_BOND": "defensive",
    "EM_BOND": "defensive",
}

# leg → 对应的市场/资产 key (用于查数据)
LEG_DATA_KEY = {
    "US_equity": "US",
    "DM_equity": "DM",
    "CN_equity": "CN",
    "HK_equity": "HK",
    "CN_GOVT": "CN_GOVT",
    "TIPS": "TIPS",
    "GOLD": "GOLD",
    "CORP_BOND": "CORP_BOND",
    "EM_BOND": "EM_BOND",
}

# ETF ticker 映射 (yfinance 格式)
ETF_TICKERS = {
    "US": "SPY",
    "DM": "EFA",
    "CN": "510300.SS",    # 沪深300 ETF
    "HK": "2800.HK",
    "CN_GOVT": "511260.SS",
    "TIPS": "TIP",
    "GOLD": "GLD",
    "CORP_BOND": "LQD",
    "EM_BOND": "EMB",
}
