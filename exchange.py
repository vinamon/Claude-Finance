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
                # Sign requests against Bybit's clock, not the laptop's.
                # See syncClock() for why this is not optional.
                "adjustForTimeDifference": True,
            },
        }
    )
    client.enable_demo_trading(True)
    assertDemoHost(client)
    syncClock(client)
    return client


def syncClock(client):
    """Measure the offset between this machine's clock and Bybit's, and sign
    requests against theirs.

    Bybit's rule is strict and asymmetric:

        server_time - recv_window <= timestamp < server_time + 1000

    Being LATE is forgiven up to recv_window (5s by default). Being EARLY is
    forgiven by only 1000 ms, whatever recv_window says. So a laptop clock
    running one second fast gets every signed request rejected with error
    10002, while every public endpoint keeps working perfectly - which makes
    it look like a credentials problem when it is a clock problem.

    ccxt subtracts options['timeDifference'] inside nonce(), so measuring it
    once per run makes the signed timestamp track Bybit rather than the local
    clock. Telling the user to fix their clock is not a fix: Windows clocks
    drift, and this would fail again at random.

    Returns the offset in milliseconds (positive = this machine is ahead), or
    None if it could not be measured.
    """
    try:
        offset = client.load_time_difference()
        return offset
    except Exception as error:
        # Not fatal: without the offset ccxt falls back to the raw clock, and
        # if that is too far out the next call fails with a clear 10002.
        print("[exchange] could not read Bybit server time (%s)" % error)
        return None


def clockReport(client):
    """One line describing the clock offset, for the smoke test."""
    offset = client.options.get("timeDifference")
    if offset is None:
        return "not measured"
    direction = "ahead of" if offset > 0 else "behind"
    note = ""
    if abs(offset) > 1000:
        note = "  <- beyond Bybit's 1000ms allowance, compensated in software"
    return "local clock is %d ms %s Bybit%s" % (abs(offset), direction, note)


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
