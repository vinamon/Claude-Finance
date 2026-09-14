"""
Signal generation. Pure functions over candle data - no order placement, no
network calls, no exchange objects. Feed it candles, get a decision.

THREE TEXTBOOK STRATEGIES, ONE ACTIVE AT A TIME
-----------------------------------------------
Every strategy here is a single-indicator classic out of a first-year trading
book. They are starting points for tuning and comparison, NOT strategies with
a demonstrated edge. Assume each one loses money after fees until your own
backtest says otherwise.

Pick the live one with config.strategy ("trend" | "meanrev" | "breakout").
Nothing outside this file needs to change when you switch.

WHY ENTRIES ARE EVENTS AND EXITS ARE STATES
-------------------------------------------
Entry looks for a crossing *event* within the last few closed candles, because
entering merely because a condition still holds would re-enter forever.

Exit looks at the current *state* instead. This bot polls every few minutes and
therefore misses most individual bars, so an exit defined as a single crossing
bar would eventually be missed and leave a position stranded. "Are we on the
wrong side of the indicator right now" cannot be missed.
"""

import config

# Decision actions
buy = "buy"
hold = "hold"
close = "close"


class Decision:
    """What signals decided, and the sentence explaining it for the log."""

    def __init__(self, action, reason):
        self.action = action
        self.reason = reason

    def __repr__(self):
        return "Decision(%s, %r)" % (self.action, self.reason)


# ---------------------------------------------------------------------------
# candle helpers
#
# Candles are ccxt OHLCV rows: [timestamp_ms, open, high, low, close, volume]
# ---------------------------------------------------------------------------


def closedCandles(candles):
    """Drop the final candle, which is still forming.

    ccxt returns the in-progress candle as the last row. Its close is just the
    current price and it flickers, so a signal computed on it fires and
    un-fires within the same bar. Everything downstream uses closed bars only.
    """
    if not candles:
        return []
    return candles[:-1]


def closes(candles):
    return [candle[4] for candle in candles]


def highs(candles):
    return [candle[2] for candle in candles]


def lows(candles):
    return [candle[3] for candle in candles]


# ---------------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------------


def sma(values, period):
    """Simple moving average. Returns a list aligned to values, None where the
    window is not yet full."""
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def rsi(values, period):
    """Wilder's RSI. Returns a list aligned to values, None until seeded.

    Seeded with a simple average of the first `period` changes, then smoothed
    the way Wilder defined it. This is the version charting packages draw.
    """
    out = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = rsiFromAverages(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = rsiFromAverages(avg_gain, avg_loss)

    return out


def rsiFromAverages(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def crossedAbove(fast, slow, index):
    """True if fast crossed from at-or-below to above slow at `index`."""
    if index < 1:
        return False
    prev_fast, prev_slow = fast[index - 1], slow[index - 1]
    now_fast, now_slow = fast[index], slow[index]
    if None in (prev_fast, prev_slow, now_fast, now_slow):
        return False
    return prev_fast <= prev_slow and now_fast > now_slow


def crossedAboveLevel(values, level, index):
    """True if values crossed from at-or-below `level` to above it at `index`."""
    if index < 1:
        return False
    prev_value, now_value = values[index - 1], values[index]
    if prev_value is None or now_value is None:
        return False
    return prev_value <= level and now_value > level


def lookbackRange(length, bars):
    """Indices of the last `bars` closed candles, newest last."""
    start = max(1, length - max(1, bars))
    return range(start, length)


# ---------------------------------------------------------------------------
# strategy 1: trend following - moving average crossover ("golden cross")
#
# TEXTBOOK STARTING POINT, NOT AN EDGE.
# Buy when the fast SMA crosses above the slow SMA, stay in while it is above.
# Famous, simple, and famously whipsawed in sideways markets.
# ---------------------------------------------------------------------------


def trendEntry(candles):
    price = closes(candles)
    need = config.sma_slow_period + 2
    if len(price) < need:
        return Decision(hold, "trend: need %d closed candles, have %d" % (need, len(price)))

    fast = sma(price, config.sma_fast_period)
    slow = sma(price, config.sma_slow_period)

    for i in lookbackRange(len(price), config.signal_lookback_bars):
        if crossedAbove(fast, slow, i):
            bars_ago = len(price) - 1 - i
            if fast[-1] is not None and slow[-1] is not None and fast[-1] > slow[-1]:
                return Decision(
                    buy,
                    "trend: SMA%d crossed above SMA%d %d bar(s) ago and is still above "
                    "(fast=%.6f slow=%.6f)"
                    % (
                        config.sma_fast_period,
                        config.sma_slow_period,
                        bars_ago,
                        fast[-1],
                        slow[-1],
                    ),
                )

    return Decision(
        hold,
        "trend: no SMA%d/SMA%d golden cross in the last %d closed bar(s) (fast=%s slow=%s)"
        % (
            config.sma_fast_period,
            config.sma_slow_period,
            config.signal_lookback_bars,
            formatValue(fast[-1]),
            formatValue(slow[-1]),
        ),
    )


def trendExit(candles):
    price = closes(candles)
    need = config.sma_slow_period + 1
    if len(price) < need:
        return Decision(hold, "trend exit: need %d closed candles, have %d" % (need, len(price)))

    fast = sma(price, config.sma_fast_period)
    slow = sma(price, config.sma_slow_period)
    if fast[-1] is None or slow[-1] is None:
        return Decision(hold, "trend exit: moving averages not seeded yet")

    if fast[-1] < slow[-1]:
        return Decision(
            close,
            "trend exit: SMA%d is below SMA%d (fast=%.6f slow=%.6f)"
            % (config.sma_fast_period, config.sma_slow_period, fast[-1], slow[-1]),
        )
    return Decision(
        hold,
        "trend exit: SMA%d still above SMA%d (fast=%.6f slow=%.6f)"
        % (config.sma_fast_period, config.sma_slow_period, fast[-1], slow[-1]),
    )


# ---------------------------------------------------------------------------
# strategy 2: mean reversion - RSI leaving oversold
#
# TEXTBOOK STARTING POINT, NOT AN EDGE.
# Buy when RSI crosses back up through the oversold line, exit when it reaches
# overbought. Catches bounces, and catches falling knives with equal enthusiasm.
# ---------------------------------------------------------------------------


def meanrevEntry(candles):
    price = closes(candles)
    need = config.rsi_period + 3
    if len(price) < need:
        return Decision(hold, "meanrev: need %d closed candles, have %d" % (need, len(price)))

    values = rsi(price, config.rsi_period)

    for i in lookbackRange(len(price), config.signal_lookback_bars):
        if crossedAboveLevel(values, config.rsi_oversold, i):
            bars_ago = len(price) - 1 - i
            if values[-1] is not None and values[-1] < config.rsi_overbought:
                return Decision(
                    buy,
                    "meanrev: RSI%d crossed above %.1f %d bar(s) ago and has not reached "
                    "%.1f yet (rsi=%.2f)"
                    % (
                        config.rsi_period,
                        config.rsi_oversold,
                        bars_ago,
                        config.rsi_overbought,
                        values[-1],
                    ),
                )

    return Decision(
        hold,
        "meanrev: RSI%d did not cross up through %.1f in the last %d closed bar(s) (rsi=%s)"
        % (
            config.rsi_period,
            config.rsi_oversold,
            config.signal_lookback_bars,
            formatValue(values[-1]),
        ),
    )


def meanrevExit(candles):
    price = closes(candles)
    need = config.rsi_period + 2
    if len(price) < need:
        return Decision(hold, "meanrev exit: need %d closed candles, have %d" % (need, len(price)))

    values = rsi(price, config.rsi_period)
    if values[-1] is None:
        return Decision(hold, "meanrev exit: RSI not seeded yet")

    if values[-1] >= config.rsi_overbought:
        return Decision(
            close,
            "meanrev exit: RSI%d reached overbought (rsi=%.2f >= %.1f)"
            % (config.rsi_period, values[-1], config.rsi_overbought),
        )
    return Decision(
        hold,
        "meanrev exit: RSI%d below overbought (rsi=%.2f < %.1f)"
        % (config.rsi_period, values[-1], config.rsi_overbought),
    )


# ---------------------------------------------------------------------------
# strategy 3: breakout - Donchian channel
#
# TEXTBOOK STARTING POINT, NOT AN EDGE.
# Buy when close exceeds the highest high of the previous N candles, exit when
# close drops under the lowest low of the previous N. Pays for a lot of false
# breakouts while waiting for the one real move.
# ---------------------------------------------------------------------------


def breakoutEntry(candles):
    window = config.breakout_lookback
    need = window + 2
    if len(candles) < need:
        return Decision(hold, "breakout: need %d closed candles, have %d" % (need, len(candles)))

    price = closes(candles)
    candle_highs = highs(candles)

    for i in lookbackRange(len(candles), config.signal_lookback_bars):
        if i < window:
            continue
        # highest high of the N candles BEFORE bar i - the bar itself must not
        # be part of the level it is supposed to break.
        level = max(candle_highs[i - window:i])
        if price[i] > level:
            bars_ago = len(candles) - 1 - i
            return Decision(
                buy,
                "breakout: close broke above the %d-bar high %d bar(s) ago "
                "(close=%.6f level=%.6f)" % (window, bars_ago, price[i], level),
            )

    last_level = max(candle_highs[-window - 1:-1])
    return Decision(
        hold,
        "breakout: no close above the %d-bar high in the last %d closed bar(s) "
        "(close=%.6f level=%.6f)"
        % (window, config.signal_lookback_bars, price[-1], last_level),
    )


def breakoutExit(candles):
    window = config.breakout_lookback
    need = window + 2
    if len(candles) < need:
        return Decision(
            hold, "breakout exit: need %d closed candles, have %d" % (need, len(candles))
        )

    price = closes(candles)
    candle_lows = lows(candles)
    level = min(candle_lows[-window - 1:-1])

    if price[-1] < level:
        return Decision(
            close,
            "breakout exit: close fell below the %d-bar low (close=%.6f level=%.6f)"
            % (window, price[-1], level),
        )
    return Decision(
        hold,
        "breakout exit: close still above the %d-bar low (close=%.6f level=%.6f)"
        % (window, price[-1], level),
    )


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

strategies = {
    "trend": (trendEntry, trendExit),
    "meanrev": (meanrevEntry, meanrevExit),
    "breakout": (breakoutEntry, breakoutExit),
}


def formatValue(value):
    return "n/a" if value is None else ("%.4f" % value)


def entrySignal(symbol, candles):
    """Decide whether to open a position. Returns a Decision of buy or hold.

    In dummy_mode the market is ignored completely: buy only when a human
    pressed the workflow_dispatch button, hold on every scheduled run. That
    lets you prove the whole pipeline works without a signal ever firing.
    """
    if config.dummy_mode:
        if config.github_event_name == "workflow_dispatch":
            return Decision(buy, "dummy_mode: manual run, forcing a test entry")
        return Decision(
            hold,
            "dummy_mode: run was triggered by %r, not workflow_dispatch, so no entry"
            % config.github_event_name,
        )

    entry, _ = resolveStrategy()
    bars = closedCandles(candles)
    return entry(bars)


def exitSignal(symbol, candles):
    """Decide whether to close an open position. Returns close or hold.

    Not short-circuited by dummy_mode: a dummy position is a real demo
    position, and the strategy's own exit rule is exactly what you want to
    watch while testing.
    """
    _, exit_rule = resolveStrategy()
    bars = closedCandles(candles)
    return exit_rule(bars)


def resolveStrategy():
    if config.strategy not in strategies:
        raise ValueError(
            "unknown strategy %r, expected one of %s"
            % (config.strategy, ", ".join(sorted(strategies)))
        )
    return strategies[config.strategy]


def requiredCandles():
    """How many candles to request so the active strategy can be computed.

    Asks for generous headroom - Bybit serves up to 1000 per request and the
    extra bars cost nothing.
    """
    if config.strategy == "trend":
        need = config.sma_slow_period + config.signal_lookback_bars + 5
    elif config.strategy == "meanrev":
        need = config.rsi_period * 10 + config.signal_lookback_bars + 5
    else:
        need = config.breakout_lookback + config.signal_lookback_bars + 5
    return min(1000, max(need, 60))
