#!/usr/bin/env python3
"""
持仓校验 — 用 OKX / Schwab API 核对手填的 holdings.yaml, 防手抄错数字。

同花顺/ZA 没有可用的个人 API, 只能靠手填时自己核对。

用法:
    cp config/api_credentials.example.yaml config/api_credentials.yaml  # 首次: 填入只读 API Key
    python scripts/verify_holdings.py
"""

import base64
import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

PROJECT_ROOT = Path(__file__).parent.parent

# Schwab 持仓 symbol → holdings.yaml 的腿
SCHWAB_SYMBOL_LEG = {
    "SPY": "US_equity",
    "EFA": "DM_equity",
    "TIP": "TIPS",
    "LQD": "CORP_BOND",
    "EMB": "EM_BOND",
}

TOLERANCE = 0.02  # 手填与 API 相差超过 2% 视为不一致


# ── OKX ───────────────────────────────────────────────────────────────────────

def _okx_request(creds: dict, path: str) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    msg = ts + "GET" + path
    sign = base64.b64encode(
        hmac.new(creds["secret_key"].encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "OK-ACCESS-KEY": creds["api_key"],
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": creds["passphrase"],
    }
    resp = requests.get(f"https://www.okx.com{path}", headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX API 错误: {data.get('msg')} (code={data.get('code')})")
    return data


def fetch_okx_gold_usd(creds: dict) -> float:
    """OKX 里 XAUT 总量 (交易+资金账户) × XAUT-USDT 价格 → USD 市值。"""
    xaut = 0.0

    # 交易账户
    data = _okx_request(creds, "/api/v5/account/balance?ccy=XAUT")
    for account in data.get("data", []):
        for detail in account.get("details", []):
            if detail.get("ccy") == "XAUT":
                xaut += float(detail.get("eq") or detail.get("cashBal") or 0)

    # 资金账户
    data = _okx_request(creds, "/api/v5/asset/balances?ccy=XAUT")
    for row in data.get("data", []):
        if row.get("ccy") == "XAUT":
            xaut += float(row.get("bal") or 0)

    if xaut == 0:
        return 0.0

    # 公开行情 (无需鉴权)
    resp = requests.get(
        "https://www.okx.com/api/v5/market/ticker?instId=XAUT-USDT", timeout=15
    )
    resp.raise_for_status()
    price = float(resp.json()["data"][0]["last"])
    return xaut * price


# ── Schwab ────────────────────────────────────────────────────────────────────

def fetch_schwab_positions_usd(creds: dict) -> dict:
    """Schwab 各持仓的 USD 市值, 按 symbol → leg 映射汇总。"""
    # refresh_token → access_token
    auth = base64.b64encode(f"{creds['app_key']}:{creds['app_secret']}".encode()).decode()
    resp = requests.post(
        "https://api.schwabapi.com/v1/oauth/token",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]},
        timeout=15,
    )
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    resp = requests.get(
        "https://api.schwabapi.com/trader/v1/accounts?fields=positions",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()

    legs: dict[str, float] = {}
    for account in resp.json():
        positions = account.get("securitiesAccount", {}).get("positions", [])
        for pos in positions:
            symbol = pos.get("instrument", {}).get("symbol", "")
            leg = SCHWAB_SYMBOL_LEG.get(symbol)
            if leg:
                legs[leg] = legs.get(leg, 0.0) + float(pos.get("marketValue", 0))
    return legs


# ── 校验 ──────────────────────────────────────────────────────────────────────

def compare(leg: str, manual: float, api: float) -> bool:
    """返回是否一致 (2% 容差)。"""
    if manual == 0 and api == 0:
        return True
    base = max(abs(manual), abs(api))
    return abs(manual - api) / base <= TOLERANCE


def main():
    creds_path = PROJECT_ROOT / "config" / "api_credentials.yaml"
    holdings_path = PROJECT_ROOT / "config" / "holdings.yaml"

    if not holdings_path.exists():
        print("config/holdings.yaml 不存在, 先按模板填写持仓")
        sys.exit(1)
    if not creds_path.exists():
        print("config/api_credentials.yaml 不存在 —")
        print("cp config/api_credentials.example.yaml config/api_credentials.yaml 并填入只读 API Key")
        sys.exit(1)

    with open(holdings_path) as f:
        manual = (yaml.safe_load(f) or {}).get("holdings") or {}
    with open(creds_path) as f:
        creds = yaml.safe_load(f) or {}

    mismatches = []
    checked = 0

    # OKX: GOLD (XAUT)
    okx_creds = creds.get("okx") or {}
    if okx_creds.get("api_key"):
        try:
            api_gold = fetch_okx_gold_usd(okx_creds)
            manual_gold = float(manual.get("GOLD") or 0)
            checked += 1
            ok = compare("GOLD", manual_gold, api_gold)
            mark = "✅" if ok else "❌"
            print(f"{mark} OKX GOLD: 手填 {manual_gold:,.0f} USD vs API {api_gold:,.0f} USD")
            if not ok:
                mismatches.append("GOLD")
        except Exception as e:
            print(f"⚠️ OKX 校验失败: {e}")
    else:
        print("— OKX 未配置 API Key, 跳过 (GOLD 靠手填自查)")

    # Schwab: 五条美元腿
    schwab_creds = creds.get("schwab") or {}
    if schwab_creds.get("refresh_token"):
        try:
            api_legs = fetch_schwab_positions_usd(schwab_creds)
            for leg in SCHWAB_SYMBOL_LEG.values():
                manual_v = float(manual.get(leg) or 0)
                api_v = api_legs.get(leg, 0.0)
                checked += 1
                ok = compare(leg, manual_v, api_v)
                mark = "✅" if ok else "❌"
                print(f"{mark} Schwab {leg}: 手填 {manual_v:,.0f} USD vs API {api_v:,.0f} USD")
                if not ok:
                    mismatches.append(leg)
        except Exception as e:
            print(f"⚠️ Schwab 校验失败: {e}")
            print("   (refresh_token 有效期 7 天, 过期需在 developer.schwab.com 重新授权)")
    else:
        print("— Schwab 未配置 refresh_token, 跳过 (美元腿靠手填自查)")

    print("— 同花顺/ZA 无个人 API, CN_equity/CN_GOVT/HK_equity 靠手填自查")

    if mismatches:
        print(f"\n❌ {len(mismatches)} 项不一致: {', '.join(mismatches)} — 修正 holdings.yaml 后再跑 monthly_ops")
        sys.exit(1)
    elif checked:
        print(f"\n✅ 已校验 {checked} 项全部一致, 可以放心跑 monthly_ops.py")
    else:
        print("\n(未配置任何 API, 全部靠手填自查)")


if __name__ == "__main__":
    main()
