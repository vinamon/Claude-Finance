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
    "active_strategies": ["trend", "breakout", "meanrev", "scalp"],
    "min_entry_votes": 1,
    "max_open_positions": 10,
    "max_open_per_strategy": {},
    # Off here, on by name in the tests about it, so a close record written
    # for a test about something else cannot quietly hold back its entry.
    "reentry_cooldown_bars": 0,
    "dummy_mode": True,
    "force_entry": False,
    "github_event_name": "local",
    "entry_timeframe": "15m",
    "exit_timeframe": "15m",
    "strategy_timeframes": {},
    "signal_lookback_bars": 3,
    "regime_filter": True,
    "regime_period": 200,
    "exit_on_regime_break": False,
    "unknown_owner_exit": "any",
    "ema_fast_period": 20,
    "ema_slow_period": 50,
    "adx_period": 14,
    "adx_min": 20.0,
    "rsi_period": 2,
    "rsi_oversold": 10.0,
    "rsi_overbought": 70.0,
    "meanrev_exit_sma_period": 5,
    "breakout_lookback": 20,
    "breakout_exit_lookback": 10,
    "bb_period": 20,
    "bb_stdev": 2.0,
    "bb_lookback_bars": 2,
    "risk_model": "atr",
    "atr_period": 14,
    "atr_stop_mult": 3.0,
    "atr_target_mult": 6.0,
    "atr_trail_mult": 1.5,
    "atr_trail_activation_mult": 3.0,
    "max_stop_fraction_of_liquidation": 0.5,
    "min_stop_atr_mult": 1.0,
    "stop_loss_pct": 0.05,
    "take_profit_pct": 0.10,
    "trailing_stop_pct": 0.03,
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
        return list(self.positions)

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


def runCycle(client, state_dir=None, **settings):
    """Run one bot cycle against `client`.

    Returns exit_code, output (everything the cycle printed), pushes (what it
    would have sent to the phone) and state_dir. Pass the state_dir of an
    earlier cycle to run a second one that remembers the first.
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

    output = io.StringIO()
    # patch.multiple refuses a name config does not have, so a misspelt
    # setting fails loudly instead of silently testing nothing.
    with mock.patch.multiple(config, **merged), \
            mock.patch.object(main, "buildExchange", lambda: client), \
            mock.patch.object(notify, "push", recordPush), \
            contextlib.redirect_stdout(output):
        exit_code = main.main()

    return types.SimpleNamespace(exit_code=exit_code, output=output.getvalue(),
                                 pushes=pushes, state_dir=state_dir)
