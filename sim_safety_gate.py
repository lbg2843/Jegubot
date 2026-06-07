# BILL 5/12 15:45:24 시점 데이터로 safety_gate 시뮬레이션
import os

bill_data = {
    "symbol": "BILL",
    "liquidity_usd": 2149_000,  # $2,149K
    "market_cap_usd": None,  # 모름 — None이면 통과
    "txns_1h": 11287,
    "price_change_1h_pct": 8.49,
    "audit_flags": [],  # 가정
    "contract_address": "0x...",
}

chain = "bsc"

def passes_safety_gate(token_dict, chain):
    symbol = token_dict.get("symbol", "?")
    liq = token_dict.get("liquidity_usd", 0)
    min_liq = {"bsc": 100_000, "solana": 50_000, "base": 150_000}
    
    print(f"Test 1 (min_liq): {liq} >= {min_liq.get(chain)} ? ", end="")
    if liq < min_liq.get(chain, 100_000):
        print(f"FAIL - low_liquidity")
        return False, "low_liquidity"
    print("PASS")
    
    mcap = token_dict.get("market_cap_usd")
    print(f"Test 2 (liq/mcap): mcap={mcap} ? ", end="")
    if mcap and mcap > 0:
        ratio = liq / mcap
        print(f"ratio={ratio:.4f} ", end="")
        if ratio < 0.03:
            print(f"FAIL - low_liq_ratio")
            return False, "low_liq_ratio"
        print("PASS")
    else:
        print("SKIP (no mcap)")
    
    print(f"Test 3 (txns): {token_dict.get('txns_1h', 0)} >= 20 ? ", end="")
    if token_dict.get("txns_1h", 0) < 20:
        print(f"FAIL - low_tx_count")
        return False, "low_tx_count"
    print("PASS")
    
    print(f"Test 4 (1h_change): {token_dict.get('price_change_1h_pct', 0)} <= 50 ? ", end="")
    if token_dict.get("price_change_1h_pct", 0) > 50:
        print(f"FAIL - already_pumped")
        return False, "already_pumped"
    print("PASS")
    
    print(f"Test 5 (audit_flags): {token_dict.get('audit_flags')} ? ", end="")
    if token_dict.get("audit_flags") and len(token_dict["audit_flags"]) > 0:
        print(f"FAIL - audit_flags")
        return False, f"audit_flags: {token_dict['audit_flags']}"
    print("PASS")
    
    return True, "ok"

print(f"=== BILL safety_gate 시뮬레이션 ({chain}) ===")
ok, reason = passes_safety_gate(bill_data, chain)
print(f"\nResult: {ok}, reason: {reason}")
