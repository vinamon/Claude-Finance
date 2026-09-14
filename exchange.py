"""
Bybit client construction.

Kept in its own module because main.py, executor.py and both smoke-test
scripts all need an identical, correctly configured client, and getting the
demo-trading switch wrong is the one mistake that would point this bot at a
real account.
"""

import ccxt

import config


def buildExchange():
    """Return a ccxt Bybit client pinned to the demo-trading host.

    enable_demo_trading(True) swaps exchange.urls['api'] for
    exchange.urls['demotrading'] (https://api-demo.bybit.com). Verified
    against ccxt 4.5.78 and Bybit's v5 demo-trading docs.

    Note demo trading is NOT ccxt's sandbox/testnet mode. They are different
    hosts with different accounts, and ccxt refuses to combine them.
    """
    client = ccxt.bybit(
        {
            "apiKey": config.bybit_api_key,
            "secret": config.bybit_api_secret,
            "enableRateLimit": True,
            "timeout": config.request_timeout_seconds * 1000,
            "options": {
                "defaultType": "swap",
                "defaultSubType": config.category,
            },
        }
    )
    client.enable_demo_trading(True)
    assertDemoHost(client)
    return client


def assertDemoHost(client):
    """Refuse to continue unless every REST URL points at the demo host.

    This is a belt-and-braces check. If a future ccxt release renames or
    reshapes the demo switch, we want a loud crash, not live orders.
    """
    urls = client.urls.get("api")
    if isinstance(urls, str):
        candidates = [urls]
    elif isinstance(urls, dict):
        candidates = [value for value in urls.values() if isinstance(value, str)]
    else:
        raise RuntimeError("unexpected ccxt urls['api'] shape: %r" % type(urls))

    bad = [url for url in candidates if "api-demo." not in url]
    if bad or not candidates:
        raise RuntimeError(
            "refusing to trade: demo trading is not active, api urls resolved to %r"
            % (candidates,)
        )
    if not client.options.get("enableDemoTrading"):
        raise RuntimeError("refusing to trade: enableDemoTrading option is not set")
    return True
