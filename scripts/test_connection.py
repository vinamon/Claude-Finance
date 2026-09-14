"""
Step 1 of the build order: prove the keys work and we are talking to the DEMO
account, nothing else. Reads only - places no orders.

    python scripts/test_connection.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ccxt

import config
from exchange import buildExchange

# Bybit's own codes, mapped to the thing that actually went wrong. Without
# this a failure is just a wall of JSON and a guess.
error_hints = {
    "10003": "the API key is not valid on this host. The single most common "
             "cause is generating the key on the REAL account instead of "
             "switching to Demo Trading first - they are separate accounts "
             "with separate keys.",
    "10004": "signature mismatch. Usually a mangled secret: GitHub stores a "
             "secret exactly as pasted, trailing spaces and newlines included. "
             "Re-paste BYBIT_API_SECRET, watching the end of the string.",
    "10005": "the key lacks permission for this call. Check the key is "
             "Read-Write and has Unified Trading enabled.",
    "10010": "the request IP is not on the key's allowlist. Remove the IP "
             "restriction: GitHub runners get a fresh IP every run.",
    "33004": "the API key has expired. Keys without an IP binding expire "
             "after 90 days. Generate a new one and update the secret.",
}


def explain(error):
    text = str(error)
    print("\nFAILED: %s" % type(error).__name__)
    print("  %s" % text[:400])
    for code, hint in error_hints.items():
        if code in text:
            print("\n  Bybit %s: %s" % (code, hint))
            return
    if isinstance(error, ccxt.AuthenticationError):
        print("\n  Authentication failed. Check, in this order: the key was "
              "made in Demo Trading mode, both secrets were pasted without "
              "trailing whitespace, and the key is Read-Write.")


def main():
    print("python:         %s" % sys.executable)
    print("config:         %s" % config.envDiagnosis())
    if not config.bybit_api_key or not config.bybit_api_secret:
        print("\nBYBIT_API_KEY / BYBIT_API_SECRET are not set")
        return 1


    client = buildExchange()
    # resolve ccxt's {hostname} template - printing it raw is just confusing
    print("REST host:      %s" % client.implode_hostname(client.urls["api"]["private"]))
    print("demo trading:   %s" % client.options.get("enableDemoTrading"))
    print("key length:     %d chars" % len(config.bybit_api_key))
    print("secret length:  %d chars" % len(config.bybit_api_secret))
    if config.bybit_api_key != config.bybit_api_key.strip():
        print("  WARNING: the key has leading/trailing whitespace, re-paste it")
    if config.bybit_api_secret != config.bybit_api_secret.strip():
        print("  WARNING: the secret has leading/trailing whitespace, re-paste it")

    try:
        balance = client.fetch_balance(params={"accountType": "UNIFIED"})
    except Exception as error:
        explain(error)
        return 1

    totals = balance.get("total") or {}
    nonzero = {coin: amount for coin, amount in totals.items() if amount}

    print("\ndemo balances:")
    if not nonzero:
        print("  (empty - request virtual funds in the Bybit Demo Trading UI)")
    for coin, amount in sorted(nonzero.items()):
        free = (balance.get("free") or {}).get(coin)
        print("  %-8s total=%-18s free=%s" % (coin, amount, free))

    markets = client.load_markets()
    print("\nloaded %d markets" % len(markets))

    for symbol in config.symbols:
        if symbol not in markets:
            print("  WARNING %s is not a Bybit market - check the symbol format" % symbol)
            continue
        market = markets[symbol]
        lot = (market.get("info") or {}).get("lotSizeFilter") or {}
        tick = ((market.get("info") or {}).get("priceFilter") or {}).get("tickSize")
        print(
            "  %-16s minQty=%-10s qtyStep=%-10s tickSize=%s"
            % (symbol, lot.get("minOrderQty"), lot.get("qtyStep"), tick)
        )

    if not config.symbols:
        print("  (no symbols configured yet)")

    print("\nconnection OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
