"""
A fake Bybit, and a way to run one bot cycle against it.

FakeBybit stands in for the ccxt client at the one place the bot builds it,
so a test can drive a whole cycle without a network, an API key or the demo
account. It answers what a cycle asks and remembers every order it is told
to place.

runCycle() isolates the cycle from the laptop it runs on. The owner's .env
has already been loaded into config by the time any test imports it, so every
setting that shapes a cycle is pinned here explicitly rather than inherited,
and the state files go to a scratch directory instead of state/. Pushes are
captured at notify.push and never leave the process.
"""

import contextlib
import io
import os
import tempfile
import time
import types
from unittest import mock

import config
import main
import notify

scratch = tempfile.TemporaryDirectory(prefix="claude-finance-tests-")

# Every setting that decides what a cycle does, pinned so a test never
# depends on whatever happens to be in .env. A test overrides any of these by
# name. SYMBOLS is not here: it follows the markets the fake client lists.
baseline = {
    "bybit_api_key": "harness",
    "bybit_api_secret": "harness",
    "category": "linear",
    "position_idx": 0,
    "order_link_prefix": "cf",
    "order_bucket_seconds": 120,
    "strategy": "multi",
    "active_strategies": ["trend", "breakout"],
    "min_entry_votes": 1,
    "max_open_positions": 10,
    "max_open_per_strategy": {},
    # Off here, on by name in the tests about it, so a close record written
    # for a test about something else cannot quietly hold back its entry.
    "reentry_cooldown_bars": 0,
    "dummy_mode": True,
    # Most tests read the per-symbol reasons, so they run with the full log;
    # the tests about the short log turn it off by name.
    "log_detail": True,
    "force_entry": False,
    "github_event_name": "local",
    "entry_timeframe": "15m",
    "exit_timeframe": "15m",
    "strategy_timeframes": {},
    "signal_lookback_bars": 3,
    "regime_filter": True,
    "regime_period": 200,
    "exit_on_regime_break": False,
    "unknown_owner_exit": "regime",
    "ema_fast_period": 20,
    "ema_slow_period": 50,
    "adx_period": 14,
    "adx_min": 20.0,
    "breakout_lookback": 20,
    "breakout_exit_lookback": 10,
    "ict_swing_bars": 2,
    "ict_liquidity_lookback_bars": 96,
    "ict_sweep_reclaim_bars": 1,
    "ict_mss_max_bars": 8,
    "ict_displacement_min_atr": 1.0,
    "ict_fvg_min_atr": 0.1,
    "ict_fvg_max_age_bars": 12,
    "ict_entry_close_min": 0.5,
    "ict_max_chase_atr": 0.5,
    "ict_kill_zones": "off",
    "ict_kill_zone_tz": "America/New_York",
    "ict_stop_ref": "candle1",
    "ict_stop_buffer_atr": 0.1,
    "ict_stop_floor_atr": 1.5,
    "ict_allow_capped_stop": False,
    "ict_min_rr": 1.5,
    "ict_fallback_target_r": 2.0,
    "pullback_displacement_min_atr": 1.5,
    "risk_model": "atr",
    "atr_period": 14,
    "atr_stop_mult": 3.0,
    "atr_target_mult": 6.0,
    "atr_trail_mult": 0.0,
    "atr_trail_activation_mult": 3.0,
    "max_stop_fraction_of_liquidation": 0.5,
    "min_stop_atr_mult": 1.0,
    "stop_loss_pct": 0.05,
    "take_profit_pct": 0.10,
    "trailing_stop_pct": 0.0,
    "trailing_activation_pct": 0.05,
    "leverage": 5,
    "position_notional_usdt": 450.0,
    "warmup_multiplier": 10,
    "candle_floor": 60,
    "candle_ceiling": 1000,
    "closed_lookback_minutes": 15,
}


def candles(count=1000, close=2000.0, half_range=10.0, timeframe_seconds=900):
    """Flat candles: every bar closes at `close` and spans close +- half_range,
    so the true range, and therefore ATR, is exactly 2 * half_range. The last
    row is the one still forming, as in a real ccxt response."""
    now_ms = int(time.time() // timeframe_seconds * timeframe_seconds * 1000)
    step_ms = timeframe_seconds * 1000
    return [
        [now_ms - (count - 1 - i) * step_ms, close, close + half_range, close - half_range,
         close, 1.0]
        for i in range(count)
    ]


# 2026-09-15 13:45 UTC, 09:45 in New York: the open time of the newest closed
# bar of a series(). Rules that read the clock - the previous UTC day, kill
# zones - need a fixed one, not the time the test happens to run.
anchor_ms = 1789479900000


def series(bars, newest_open_ms=anchor_ms, timeframe_seconds=900):
    """ccxt rows for (open, high, low, close) bars, the last one opening at
    `newest_open_ms`, plus the candle still forming after it, which opens and
    sits at the last close."""
    step_ms = timeframe_seconds * 1000
    first_ms = newest_open_ms - (len(bars) - 1) * step_ms
    rows = [[first_ms + i * step_ms, float(o), float(h), float(l), float(c), 1.0]
            for i, (o, h, l, c) in enumerate(bars)]
    last = rows[-1][4]
    return rows + [[newest_open_ms + step_ms, last, last, last, last, 1.0]]


def risingRun(count, last_close, step, half_range):
    """`count` bars climbing `step` a bar to `last_close`, each opening where
    the previous closed. Highs and lows only rise, so there is no swing in it."""
    return [(close - step, close + half_range, close - step - half_range, close)
            for close in (last_close - step * (count - 1 - i) for i in range(count))]


def ramp(start, step, count, wick):
    """`count` bars from `start`, each closing `step` from its open (down when
    negative), with `wick` beyond its body either side. A straight run has no
    swing inside it."""
    bars = []
    for i in range(count):
        o = start + step * i
        c = o + step
        bars.append((o, max(o, c) + wick, min(o, c) - wick, c))
    return bars


def ictLong():
    """Candles forming one ict long, newest closed bar the retest, at 09:45 in
    New York.

    A slow climb (the regime line stays under the price), a run up to a swing
    high at 2100 and back down, a swing low at 1980, a swing high at 2005, then:
    a sweep to 1975 that closes back above 1980, a candle 2 closing at 2022
    above 2005 (the structure shift) and leaving the gap 1990-2010 between
    candle 1's high and candle 3's low, one bar away, and the first retest:
    low 2006, close 2014. Candle 1's low, 1978, is the structure; 2100 is the
    only untouched buy-side level above, the draw. ATR is about 14.
    """
    bars = risingRun(300, 2000.0, 0.6, 4.0)
    bars += ramp(2000.0, 3.0, 30, 2.0)                  # up to 2090
    bars += [(2090, 2100, 2088, 2093)]                  # the swing high, the draw
    bars += ramp(2093.0, -3.0, 30, 2.0)                 # down to 2003
    bars += [
        (2003, 2006, 1995, 1998),
        (1998, 2001, 1985, 1990),
        (1990, 1994, 1980, 1988),                       # swing low 1980
        (1988, 1998, 1984, 1995),
        (1995, 2005, 1990, 2000),                       # swing high 2005
        (2000, 2003, 1990, 1992),
        (1992, 1996, 1983, 1986),
        (1986, 1990, 1975, 1984),                       # the sweep
        (1984, 1990, 1978, 1988),                       # candle 1
        (1988, 2025, 1987, 2022),                       # candle 2, the structure shift
        (2022, 2030, 2010, 2026),                       # candle 3
        (2026, 2032, 2018, 2024),
        (2024, 2026, 2006, 2014),                       # the retest
    ]
    return series(bars)


def pullbackLong(leg_low=1996.0, draw=None):
    """Candles forming one pullback long, newest closed bar the retest, at
    09:45 in New York.

    The same slow climb, then a swing low at 1980, a swing high H at 2040, a
    leg low at `leg_low` (1996: above the 1980) and one big candle, 2010 to
    2046, that closes above H and leaves the gap 2012-2024 between candle 1's
    high and candle 3's low. The first retest: low 2022, close 2026. Candle 1's
    low, 1998, is the structure. Nothing untouched stands above the price, so
    the target is the fallback multiple of the risk. ATR is about 14. `draw`
    puts one more swing high, untouched since, into the climb at that price.
    """
    bars = risingRun(300, 2000.0, 0.6, 4.0)
    if draw is not None:
        o, h, l, c = bars[240]
        bars[240] = (o, draw, l, c)
    bars += [
        (2000, 2006, 1994, 2002),
        (2002, 2004, 1990, 1992),
        (1992, 1996, 1980, 1990),                       # swing low 1980
        (1990, 2000, 1988, 1998),
        (1998, 2008, 1992, 2006),
        (2006, 2020, 2002, 2018),
        (2018, 2040, 2014, 2036),                       # swing high H 2040
        (2036, 2038, 2024, 2026),
        (2026, 2030, 2018, 2020),
        (2020, 2024, 2008, 2012),
        (2012, 2016, leg_low, leg_low + 6),             # the leg low
        (leg_low + 6, 2012, 1998, 2010),                # candle 1
        (2010, 2050, 2008, 2046),                       # candle 2, closes above H
        (2046, 2062, 2024, 2058),                       # candle 3
        (2058, 2060, 2022, 2026),                       # the retest
    ]
    return series(bars)


def heldPosition(symbol="ETH/USDT:USDT", contracts=0.22, side="long"):
    """One row of ccxt's fetch_positions for a position Bybit holds."""
    return {"symbol": symbol, "contracts": contracts, "side": side,
            "info": {"size": str(contracts), "side": "Buy" if side == "long" else "Sell"}}


def market(symbol, tick="0.01", qty_step="0.01"):
    """A ccxt market carrying Bybit's own instruments-info filters, which is
    what executor.instrumentSpec reads."""
    base = symbol.split("/")[0]
    return {
        "id": "%sUSDT" % base,
        "symbol": symbol,
        "info": {
            "lotSizeFilter": {"qtyStep": qty_step, "minOrderQty": qty_step,
                              "maxOrderQty": "1000000"},
            "priceFilter": {"tickSize": tick},
        },
        "precision": {"amount": float(qty_step), "price": float(tick)},
        "limits": {"amount": {"min": float(qty_step), "max": 1000000.0}},
    }


class FakeBybit:
    """The part of ccxt's Bybit client a cycle touches.

    `last_price` is what the ticker reports; None means the close of the
    newest candle. `ticker`, when given, is returned whole instead, for a
    ticker that carries no usable price at all. `orders` maps an order id to
    the row Bybit's order history returns for it, and `order_history_error`,
    when set, is raised by every order-history request instead. `tick` and
    `qty_step` are every market's instrument filters; the defaults suit an
    ETH-sized price, a coin priced in cents needs a finer tick.
    """

    def __init__(self, symbols=("ETH/USDT:USDT",), bars=None, positions=None,
                 closed=None, closed_error=None, last_price=None, orders=None,
                 order_history_error=None, tick="0.01", qty_step="0.01", ticker=None):
        self.urls = {"api": {"private": "https://api-demo.bybit.com"}}
        self.options = {}
        self.markets = {symbol: market(symbol, tick, qty_step) for symbol in symbols}
        self.bars = bars if bars is not None else candles()
        self.positions = list(positions or [])
        self.closed = list(closed or [])
        self.closed_error = closed_error
        self.last_price = last_price
        self.ticker = ticker
        self.orders = dict(orders or {})
        self.order_history_error = order_history_error
        self.created_orders = []
        self.trading_stops = []
        self.closed_requests = []
        self.order_history_requests = []

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def implode_hostname(self, url):
        return url

    def fetch_positions(self, symbols=None, params=None):
        return [position for position in self.positions
                if not symbols or position.get("symbol") in symbols]

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params=None):
        return self.bars[-limit:] if limit else list(self.bars)

    def fetch_ticker(self, symbol, params=None):
        if self.ticker is not None:
            return dict(self.ticker)
        last = self.last_price if self.last_price is not None else self.bars[-1][4]
        return {"symbol": symbol, "last": last}

    def set_leverage(self, leverage, symbol, params=None):
        return {}

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.created_orders.append({"symbol": symbol, "type": type, "side": side,
                                    "amount": amount, "price": price,
                                    "params": dict(params or {})})
        # Filled at once: an entry leaves a position for the next cycle to
        # find, a reduce-only order takes it away.
        self.positions = [position for position in self.positions
                          if position.get("symbol") != symbol]
        if not (params or {}).get("reduceOnly"):
            self.positions.append(heldPosition(symbol, amount,
                                               "long" if side == "buy" else "short"))
        return {"id": "fake-order-%d" % len(self.created_orders)}

    def privatePostV5PositionTradingStop(self, request):
        self.trading_stops.append(dict(request))
        return {"retCode": 0}

    def privateGetV5PositionClosedPnl(self, request):
        self.closed_requests.append(dict(request))
        if self.closed_error is not None:
            raise self.closed_error
        return {"result": {"list": list(self.closed)}}

    def privateGetV5OrderHistory(self, request):
        self.order_history_requests.append(dict(request))
        if self.order_history_error is not None:
            raise self.order_history_error
        order = self.orders.get(request.get("orderId"))
        return {"result": {"list": [order] if order else []}}


def runCycle(client, state_dir=None, environ=None, **settings):
    """Run one bot cycle against `client`.

    Returns exit_code, output (everything the cycle printed), pushes (what it
    would have sent to the phone) and state_dir. Pass the state_dir of an
    earlier cycle to run a second one that remembers the first. `environ`
    sets environment variables for the cycle; the retired settings are
    blanked unless it names them, so a stale key in the owner's .env cannot
    put a warning into every test.
    """
    if state_dir is None:
        state_dir = tempfile.mkdtemp(dir=scratch.name)
    pushes = []

    def recordPush(title, message, priority="default", tags=None):
        pushes.append({"title": title, "message": message, "priority": priority,
                       "tags": tags})
        return True

    merged = dict(baseline)
    merged["symbols"] = list(client.markets)
    merged["notified_state_file"] = os.path.join(state_dir, "notified.json")
    merged["owners_state_file"] = os.path.join(state_dir, "owners.json")
    merged.update(settings)

    variables = {name: "" for name in config.retired_settings}
    variables.update(environ or {})

    output = io.StringIO()
    # patch.multiple refuses a name config does not have, so a misspelt
    # setting fails loudly instead of silently testing nothing.
    with mock.patch.multiple(config, **merged), \
            mock.patch.dict(os.environ, variables), \
            mock.patch.object(main, "buildExchange", lambda: client), \
            mock.patch.object(notify, "push", recordPush), \
            contextlib.redirect_stdout(output):
        exit_code = main.main()

    return types.SimpleNamespace(exit_code=exit_code, output=output.getvalue(),
                                 pushes=pushes, state_dir=state_dir)
