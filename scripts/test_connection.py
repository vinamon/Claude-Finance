"""
Step 1 of the build order: prove the keys work and we are talking to the DEMO
account, nothing else. Reads only - places no orders.

    python scripts/test_connection.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from exchange import buildExchange


def main():
    if not config.bybit_api_key or not config.bybit_api_secret:
        print("BYBIT_API_KEY / BYBIT_API_SECRET are not set")
        return 1

    client = buildExchange()
    print("REST host:      %s" % client.urls["api"]["private"])
    print("demo trading:   %s" % client.options.get("enableDemoTrading"))

    balance = client.fetch_balance(params={"accountType": "UNIFIED"})
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
