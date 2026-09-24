"""
Signal generation. Pure functions over candle data - no order placement, no
network calls, no exchange objects. Feed it candles, get a decision.

EVERY NUMBER IN THIS FILE COMES FROM config
-------------------------------------------
There is not a single hardcoded period, threshold or multiplier below. Each
one reads config.<name>, and each of those reads an environment variable of
the same upper-case name, which means every knob is settable from .env without
touching code. If you find a bare number in a strategy here, it is a bug.

FOUR STRATEGIES, AND THEY CAN ALL RUN AT ONCE
---------------------------------------------
  trend     EMA fast/slow crossover, confirmed by ADX trend strength
  meanrev   Connors-style short-RSI pullback bought inside an uptrend
  breakout  Donchian (Turtle) channel breakout, asymmetric exit
  scalp     Bollinger band stretch below the lower band, back to the middle

config.strategy picks one, or "multi" runs every strategy in
config.active_strategies and enters when at least config.min_entry_votes of
them agree. By default all four read the same 15-minute candles;
STRATEGY_TIMEFRAMES can move any of them to its own clock. They are textbook
systems with published track records, not edges we discovered. Assume each
loses money after fees until a backtest says otherwise.

THE REGIME FILTER IS WHAT MAKES COMBINING THEM COHERENT
-------------------------------------------------------
Trend following and mean reversion are philosophical opposites: one buys
strength, the other buys weakness. Run naively side by side they fight, and
one strategy's entry is the other's exit.

config.regime_filter resolves that. Every strategy may only go long while
price is above the slow regime average, so mean reversion becomes "buy the dip
IN an uptrend" - which is the well-documented version of it - rather than
"catch the falling knife". They all then pull in the same direction and
differ only in what triggers the entry.

WHY ENTRIES ARE EVENTS AND EXITS ARE STATES
-------------------------------------------
Entry looks for a crossing *event* within the last few closed candles, because
entering merely because a condition still holds would re-enter forever.

Exit looks at the current *state* instead. This bot polls on a timer and
therefore misses most individual bars, so an exit defined as a single crossing
bar would eventually be missed and leave a position stranded. "Are we on the
wrong side of the indicator right now" cannot be missed.

The one deliberate exception is the mean-reversion entry, which reads the
newest closed bar as a state. A 2-period RSI resolves within a bar or two, so
an event window would let a stale reading re-open a position that just took
profit. The comment at meanrevEntry spells this out.
"""

import config

# Decision actions
buy = "buy"
hold = "hold"
close = "close"


class Decision:
    """What signals decided, and the sentence explaining it for the log.

    `strategy` names which strategy produced it. In multi-strategy mode that
    tag is what gets written into the order id and remembered, so the exit is
    judged by the same rule that opened the position.
    """

    def __init__(self, action, reason, strategy=None, votes=None):
        self.action = action
        self.reason = reason
        self.strategy = strategy
        self.votes = votes or []

    def __repr__(self):
        return "Decision(%s, %s, %r)" % (self.action, self.strategy, self.reason)


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
#
# All return a list aligned to the input, None where there is not enough
# history yet. Aligning them means index i always refers to candle i, so
# comparing two indicators never needs offset arithmetic.
# ---------------------------------------------------------------------------


def sma(values, period):
    """Simple moving average."""
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def ema(values, period):
    """Exponential moving average, seeded with the SMA of the first window.

    Seeding with an SMA rather than the first value is what charting packages
    do; starting from a single price makes the first dozen readings meaningless
    and would fire crossovers that no chart shows.
    """
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    multiplier = 2.0 / (period + 1)
    running = sum(values[:period]) / period
    out[period - 1] = running
    for i in range(period, len(values)):
        running = (values[i] - running) * multiplier + running
        out[i] = running
    return out


def wilderSmooth(values, period):
    """Wilder's smoothing, the averaging used by RSI, ATR and ADX.

    Seeded with the simple average of the first `period` readings, then
    recursive. `values` may start with None entries (true range has no reading
    for the first bar); those are skipped when looking for the seed window.
    """
    out = [None] * len(values)
    if period <= 0:
        return out

    start = 0
    while start < len(values) and values[start] is None:
        start += 1
    if start + period > len(values):
        return out

    running = sum(values[start:start + period]) / period
    out[start + period - 1] = running
    for i in range(start + period, len(values)):
        value = values[i] if values[i] is not None else 0.0
        running = (running * (period - 1) + value) / period
        out[i] = running
    return out


def rsi(values, period):
    """Wilder's RSI, seeded with a simple average of the first `period`
    changes. This is the version charting packages draw."""
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


def trueRanges(candles):
    """Wilder's true range per bar. None for the first bar, which has no
    previous close to gap from."""
    out = [None] * len(candles)
    for i in range(1, len(candles)):
        high = candles[i][2]
        low = candles[i][3]
        prev_close = candles[i - 1][4]
        out[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))
    return out


def atr(candles, period):
    """Average true range: how far this instrument typically travels in a bar.

    The point of ATR is that it is denominated in the instrument's own price
    units, so "two ATR" means the same amount of risk on BTC as on a small alt,
    which a flat percentage never does.
    """
    return wilderSmooth(trueRanges(candles), period)


def adx(candles, period):
    """Wilder's ADX: trend STRENGTH, with no opinion on direction.

    A moving-average crossover system's main weakness is that it fires
    constantly in a sideways market and loses on every one of those trades.
    ADX is the standard filter for exactly that: low ADX means the market is
    ranging and a crossover means nothing.

    Returns (adx, plus_di, minus_di), each aligned to candles.
    """
    length = len(candles)
    empty = [None] * length
    if period <= 0 or length < 2:
        return empty, empty, empty

    ranges = trueRanges(candles)
    plus_dm = [None] * length
    minus_dm = [None] * length

    for i in range(1, length):
        up_move = candles[i][2] - candles[i - 1][2]
        down_move = candles[i - 1][3] - candles[i][3]
        # Only the larger of the two directional moves counts, and only when
        # it is positive. A bar that is merely inside the previous one
        # contributes nothing in either direction.
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    smoothed_tr = wilderSmooth(ranges, period)
    smoothed_plus = wilderSmooth(plus_dm, period)
    smoothed_minus = wilderSmooth(minus_dm, period)

    plus_di = [None] * length
    minus_di = [None] * length
    dx = [None] * length

    for i in range(length):
        if smoothed_tr[i] in (None, 0) or smoothed_plus[i] is None or smoothed_minus[i] is None:
            continue
        plus_di[i] = 100.0 * smoothed_plus[i] / smoothed_tr[i]
        minus_di[i] = 100.0 * smoothed_minus[i] / smoothed_tr[i]
        total = plus_di[i] + minus_di[i]
        dx[i] = 0.0 if total == 0 else 100.0 * abs(plus_di[i] - minus_di[i]) / total

    return wilderSmooth(dx, period), plus_di, minus_di


def stdev(values, period):
    """Population standard deviation over a rolling window, aligned to values.

    Population rather than sample, because that is what Bollinger bands use
    and what every charting package draws. The sample form would widen every
    band slightly and quietly shift every signal.
    """
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((value - mean) ** 2 for value in window) / period
        out[i] = variance ** 0.5
    return out


def bollinger(values, period, deviations):
    """(middle, upper, lower) bands, each aligned to values.

    The middle band is a simple moving average; the outer two sit a number of
    standard deviations either side, so they widen when the market gets
    volatile and tighten when it calms down. That self-scaling is the point:
    "unusually far from normal" means the same thing on BTC and on a memecoin.
    """
    middle = sma(values, period)
    spread = stdev(values, period)
    upper = [None] * len(values)
    lower = [None] * len(values)
    for i in range(len(values)):
        if middle[i] is None or spread[i] is None:
            continue
        upper[i] = middle[i] + deviations * spread[i]
        lower[i] = middle[i] - deviations * spread[i]
    return middle, upper, lower


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


def formatValue(value):
    return "n/a" if value is None else ("%.4f" % value)


# ---------------------------------------------------------------------------
# regime filter
#
# The gate every strategy passes through. Turn it off with
# REGIME_FILTER=false and the strategies revert to arguing with each other.
# ---------------------------------------------------------------------------


def regimeState(candles):
    """(ok, sentence) - is the market in a regime we are allowed to buy in.

    Long only above the slow average. This is the oldest filter in trend
    trading and the reason a mean-reversion rule can sit next to a
    trend-following one without the two cancelling out.
    """
    if not config.regime_filter:
        return True, "regime filter off"

    price = closes(candles)
    period = config.regime_period
    if len(price) < period + 1:
        return False, "regime: need %d closed candles, have %d" % (period + 1, len(price))

    trend_line = sma(price, period)
    if trend_line[-1] is None:
        return False, "regime: SMA%d not seeded yet" % period

    if price[-1] > trend_line[-1]:
        return True, "regime: price %.6f above SMA%d %.6f" % (price[-1], period, trend_line[-1])
    return False, "regime: price %.6f is below SMA%d %.6f, long entries blocked" % (
        price[-1],
        period,
        trend_line[-1],
    )


def regimeBroken(candles):
    """(broken, sentence) - has the regime failed under an open position.

    Used as an exit override that applies to every strategy, so a position
    opened by any of them is closed when the reason to be long at all is gone.
    Controlled by EXIT_ON_REGIME_BREAK.
    """
    if not config.exit_on_regime_break or not config.regime_filter:
        return False, "regime-break exit off"

    price = closes(candles)
    period = config.regime_period
    if len(price) < period + 1:
        return False, "regime exit: not enough history to judge"

    trend_line = sma(price, period)
    if trend_line[-1] is None:
        return False, "regime exit: SMA%d not seeded yet" % period

    if price[-1] < trend_line[-1]:
        return True, "regime break: price %.6f fell below SMA%d %.6f" % (
            price[-1],
            period,
            trend_line[-1],
        )
    return False, "regime intact: price %.6f above SMA%d %.6f" % (
        price[-1],
        period,
        trend_line[-1],
    )


# ---------------------------------------------------------------------------
# strategy 1: trend - EMA crossover confirmed by ADX
#
# The most-traded system there is, plus the standard fix for its worst flaw.
# A bare moving-average crossover bleeds in sideways markets because it fires
# on every wiggle; requiring ADX above a floor means "only take the crossover
# when something is actually trending".
#
# EMA rather than SMA, and shorter periods than the classic 50/200, because
# 50/200 is a daily-chart signal. On an intraday timeframe it fires once in
# months. EMA_FAST_PERIOD / EMA_SLOW_PERIOD set the speed.
# ---------------------------------------------------------------------------


def trendEntry(candles):
    price = closes(candles)
    need = config.ema_slow_period + config.signal_lookback_bars + 2
    if len(price) < need:
        return Decision(hold, "trend: need %d closed candles, have %d" % (need, len(price)),
                        "trend")

    fast = ema(price, config.ema_fast_period)
    slow = ema(price, config.ema_slow_period)
    strength, plus_di, minus_di = adx(candles, config.adx_period)

    for i in lookbackRange(len(price), config.signal_lookback_bars):
        if not crossedAbove(fast, slow, i):
            continue
        if fast[-1] is None or slow[-1] is None or fast[-1] <= slow[-1]:
            continue

        bars_ago = len(price) - 1 - i
        if config.adx_min > 0:
            if strength[-1] is None:
                return Decision(
                    hold,
                    "trend: EMA%d crossed above EMA%d %d bar(s) ago but ADX%d is not "
                    "seeded yet" % (config.ema_fast_period, config.ema_slow_period,
                                    bars_ago, config.adx_period),
                    "trend",
                )
            if strength[-1] < config.adx_min:
                return Decision(
                    hold,
                    "trend: EMA%d crossed above EMA%d %d bar(s) ago but ADX%d is %.1f, "
                    "under the %.1f floor - the market is ranging, not trending"
                    % (config.ema_fast_period, config.ema_slow_period, bars_ago,
                       config.adx_period, strength[-1], config.adx_min),
                    "trend",
                )

        return Decision(
            buy,
            "trend: EMA%d crossed above EMA%d %d bar(s) ago and is still above "
            "(fast=%.6f slow=%.6f, ADX%d=%s +DI=%s -DI=%s)"
            % (config.ema_fast_period, config.ema_slow_period, bars_ago, fast[-1], slow[-1],
               config.adx_period, formatValue(strength[-1]), formatValue(plus_di[-1]),
               formatValue(minus_di[-1])),
            "trend",
        )

    return Decision(
        hold,
        "trend: no EMA%d/EMA%d cross up in the last %d closed bar(s) (fast=%s slow=%s ADX%d=%s)"
        % (config.ema_fast_period, config.ema_slow_period, config.signal_lookback_bars,
           formatValue(fast[-1]), formatValue(slow[-1]), config.adx_period,
           formatValue(strength[-1])),
        "trend",
    )


def trendExit(candles):
    price = closes(candles)
    need = config.ema_slow_period + 1
    if len(price) < need:
        return Decision(hold, "trend exit: need %d closed candles, have %d" % (need, len(price)),
                        "trend")

    fast = ema(price, config.ema_fast_period)
    slow = ema(price, config.ema_slow_period)
    if fast[-1] is None or slow[-1] is None:
        return Decision(hold, "trend exit: moving averages not seeded yet", "trend")

    if fast[-1] < slow[-1]:
        return Decision(
            close,
            "trend exit: EMA%d is below EMA%d (fast=%.6f slow=%.6f)"
            % (config.ema_fast_period, config.ema_slow_period, fast[-1], slow[-1]),
            "trend",
        )
    return Decision(
        hold,
        "trend exit: EMA%d still above EMA%d (fast=%.6f slow=%.6f)"
        % (config.ema_fast_period, config.ema_slow_period, fast[-1], slow[-1]),
        "trend",
    )


# ---------------------------------------------------------------------------
# strategy 2: meanrev - short-period RSI pullback inside an uptrend
#
# The Connors "RSI-2" pattern: in an established uptrend, buy the bar where a
# very short RSI collapses into deep oversold, and let go as soon as price
# snaps back over a short average. Published, widely replicated, and known for
# a high win rate with small wins.
#
# What makes it defensible rather than knife-catching is the regime filter
# above: it only ever buys weakness inside strength. Without that filter it is
# a completely different, and much worse, strategy.
#
# Honest caveat: RSI-2 was built on equity indices, which mean-revert. Crypto
# trends harder and pays mean reversion less. Expect a lower hit rate here
# than the literature quotes.
# ---------------------------------------------------------------------------


def meanrevEntry(candles):
    price = closes(candles)
    need = max(config.rsi_period, config.meanrev_exit_sma_period) + 2
    if len(price) < need:
        return Decision(hold, "meanrev: need %d closed candles, have %d" % (need, len(price)),
                        "meanrev")

    values = rsi(price, config.rsi_period)
    if values[-1] is None:
        return Decision(hold, "meanrev: RSI%d not seeded yet" % config.rsi_period, "meanrev")

    # Read as a STATE on the newest closed bar, not as an event over a window.
    # A 2-period RSI leaves oversold within a bar or two, so scanning a window
    # would let a reading that has already resolved re-open a position that
    # just closed at target. The regime filter and the one-position-per-symbol
    # rule are what keep this from re-entering endlessly.
    if values[-1] <= config.rsi_oversold:
        return Decision(
            buy,
            "meanrev: RSI%d is %.2f, at or below the %.1f oversold line, on the newest "
            "closed bar (close=%.6f)"
            % (config.rsi_period, values[-1], config.rsi_oversold, price[-1]),
            "meanrev",
        )

    return Decision(
        hold,
        "meanrev: RSI%d is %.2f, above the %.1f oversold line"
        % (config.rsi_period, values[-1], config.rsi_oversold),
        "meanrev",
    )


def meanrevExit(candles):
    price = closes(candles)
    need = max(config.rsi_period, config.meanrev_exit_sma_period) + 2
    if len(price) < need:
        return Decision(hold, "meanrev exit: need %d closed candles, have %d" % (need, len(price)),
                        "meanrev")

    values = rsi(price, config.rsi_period)
    fast_line = sma(price, config.meanrev_exit_sma_period)

    # Connors' own exit is the close crossing back above a short average: the
    # bounce being bought has happened, take it. The overbought line is kept
    # as a second door for the move that runs further than the average.
    if fast_line[-1] is not None and price[-1] > fast_line[-1]:
        return Decision(
            close,
            "meanrev exit: close %.6f snapped back above SMA%d %.6f, the bounce played out"
            % (price[-1], config.meanrev_exit_sma_period, fast_line[-1]),
            "meanrev",
        )

    if values[-1] is not None and values[-1] >= config.rsi_overbought:
        return Decision(
            close,
            "meanrev exit: RSI%d reached overbought (rsi=%.2f >= %.1f)"
            % (config.rsi_period, values[-1], config.rsi_overbought),
            "meanrev",
        )

    return Decision(
        hold,
        "meanrev exit: close %.6f still under SMA%d %s and RSI%d %s below %.1f"
        % (price[-1], config.meanrev_exit_sma_period, formatValue(fast_line[-1]),
           config.rsi_period, formatValue(values[-1]), config.rsi_overbought),
        "meanrev",
    )


# ---------------------------------------------------------------------------
# strategy 3: breakout - Donchian channel, Turtle style
#
# Buy a close above the highest high of the previous N bars; leave on a close
# below the lowest low of the previous M, where M is SHORTER than N.
#
# The asymmetry is the whole point and it is the original Turtle rule. A
# symmetric channel gives back most of a move before admitting it is over,
# because by the time price makes a new N-bar low the trend has been dead for
# a long time. BREAKOUT_LOOKBACK sets the entry channel,
# BREAKOUT_EXIT_LOOKBACK the (shorter) exit channel.
# ---------------------------------------------------------------------------


def breakoutEntry(candles):
    window = config.breakout_lookback
    need = window + config.signal_lookback_bars + 2
    if len(candles) < need:
        return Decision(hold, "breakout: need %d closed candles, have %d" % (need, len(candles)),
                        "breakout")

    price = closes(candles)
    candle_highs = highs(candles)

    for i in lookbackRange(len(candles), config.signal_lookback_bars):
        if i < window:
            continue
        # Highest high of the N candles BEFORE bar i - the bar itself must not
        # be part of the level it is supposed to break.
        level = max(candle_highs[i - window:i])
        if price[i] > level:
            bars_ago = len(candles) - 1 - i
            return Decision(
                buy,
                "breakout: close broke above the %d-bar high %d bar(s) ago "
                "(close=%.6f level=%.6f)" % (window, bars_ago, price[i], level),
                "breakout",
            )

    last_level = max(candle_highs[-window - 1:-1])
    return Decision(
        hold,
        "breakout: no close above the %d-bar high in the last %d closed bar(s) "
        "(close=%.6f level=%.6f)"
        % (window, config.signal_lookback_bars, price[-1], last_level),
        "breakout",
    )


def breakoutExit(candles):
    window = config.breakout_exit_lookback
    need = window + 2
    if len(candles) < need:
        return Decision(
            hold, "breakout exit: need %d closed candles, have %d" % (need, len(candles)),
            "breakout",
        )

    price = closes(candles)
    candle_lows = lows(candles)
    level = min(candle_lows[-window - 1:-1])

    if price[-1] < level:
        return Decision(
            close,
            "breakout exit: close fell below the %d-bar low (close=%.6f level=%.6f)"
            % (window, price[-1], level),
            "breakout",
        )
    return Decision(
        hold,
        "breakout exit: close still above the %d-bar low (close=%.6f level=%.6f)"
        % (window, price[-1], level),
        "breakout",
    )


# ---------------------------------------------------------------------------
# strategy 4: scalp - Bollinger Band reversion on a fast timeframe
#
# Bollinger bands are a moving average with a channel drawn BB_STDEV standard
# deviations either side. Buy a close stretched below the lower band, let go
# once it has snapped back to the middle. That is the published use of the
# indicator, not an invention.
#
# It earns its place next to the other three by measuring something none of
# them measure. EMA crossovers, RSI and Donchian channels all read where price
# IS; standard deviation reads how far the current move sits outside normal
# variation for this market, which is why the same rule works on BTC and on a
# memecoin without retuning.
#
# It shares the 15-minute clock with the other three by default. Should a
# slower rule ever go back to hourly candles through STRATEGY_TIMEFRAMES, give
# this one a MAX_OPEN_PER_STRATEGY budget too, or it fires so much more often
# that it takes every position slot before the slow rule can reach one.
#
# Honest caveat: on a fast clock fees stop being a rounding error. At 6xATR on
# 15-minute candles the target is roughly 1.5-6%, but this rule usually exits
# earlier, at the middle band, for a fraction of that - against about 0.11%
# for a taker round trip. Survivable, but not free, and the reason this is not
# on 5-minute candles.
# ---------------------------------------------------------------------------


def scalpEntry(candles):
    price = closes(candles)
    need = config.bb_period + config.bb_lookback_bars + 2
    if len(price) < need:
        return Decision(hold, "scalp: need %d closed candles, have %d" % (need, len(price)),
                        "scalp")

    middle, upper, lower = bollinger(price, config.bb_period, config.bb_stdev)
    if lower[-1] is None or middle[-1] is None:
        return Decision(hold, "scalp: bands not seeded yet", "scalp")

    # A short window, not a state. A band touch on a 15-minute chart resolves
    # within a bar or two, so reading it as a state - or scanning a wide
    # window - would re-enter on a move that has already finished.
    for i in lookbackRange(len(price), config.bb_lookback_bars):
        if lower[i] is None or middle[i] is None or price[i] >= lower[i]:
            continue
        # Only while price has not already recovered to the middle band. That
        # is this strategy's own exit, and entering on top of it would open a
        # trade that the very next cycle closes.
        if price[-1] >= middle[-1]:
            continue
        bars_ago = len(price) - 1 - i
        stretch = 100.0 * (middle[i] - price[i]) / middle[i] if middle[i] else 0.0
        return Decision(
            buy,
            "scalp: close broke below the lower Bollinger band %d bar(s) ago, %.2f%% under "
            "the %d-bar mean (close=%.6f lower=%.6f middle=%.6f)"
            % (bars_ago, stretch, config.bb_period, price[i], lower[i], middle[i]),
            "scalp",
        )

    return Decision(
        hold,
        "scalp: no close below the lower Bollinger band in the last %d bar(s) "
        "(close=%.6f lower=%s middle=%s)"
        % (config.bb_lookback_bars, price[-1], formatValue(lower[-1]), formatValue(middle[-1])),
        "scalp",
    )


def scalpExit(candles):
    price = closes(candles)
    need = config.bb_period + 2
    if len(price) < need:
        return Decision(hold, "scalp exit: need %d closed candles, have %d" % (need, len(price)),
                        "scalp")

    middle, upper, lower = bollinger(price, config.bb_period, config.bb_stdev)
    if middle[-1] is None:
        return Decision(hold, "scalp exit: bands not seeded yet", "scalp")

    # Reversion to the mean IS the trade. Once price is back at the middle
    # band the reason for being in it has been paid out, and holding on turns
    # a mean-reversion trade into a directional bet it was never sized for.
    if price[-1] >= middle[-1]:
        return Decision(
            close,
            "scalp exit: close %.6f reverted to the %d-bar mean %.6f, the stretch is paid out"
            % (price[-1], config.bb_period, middle[-1]),
            "scalp",
        )

    return Decision(
        hold,
        "scalp exit: close %.6f still below the %d-bar mean %.6f"
        % (price[-1], config.bb_period, middle[-1]),
        "scalp",
    )


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

strategies = {
    "trend": (trendEntry, trendExit),
    "meanrev": (meanrevEntry, meanrevExit),
    "breakout": (breakoutEntry, breakoutExit),
    "scalp": (scalpEntry, scalpExit),
}

multi = "multi"


def activeStrategies():
    """Which strategies are live this run, in priority order.

    Priority matters: when several fire on the same bar, the first one owns
    the position and its exit rule is the one that will close it. Order is
    exactly the order written in ACTIVE_STRATEGIES.
    """
    if config.strategy != multi:
        return [config.strategy]
    return [name for name in config.active_strategies if name in strategies]


def strategyTimeframe(name):
    """The candle size this strategy is evaluated on.

    STRATEGY_TIMEFRAMES overrides ENTRY_TIMEFRAME per strategy, which is what
    lets one bot hold a swing book and a scalping book at once - say a trend
    rule on hourly candles beside the rest on 15-minute ones, in the same
    cycle, against the same account. Empty by default: one clock for all.
    """
    return config.strategy_timeframes.get(name, config.entry_timeframe)


def requiredTimeframes():
    """Map of timeframe to how many candles to fetch, for everything active.

    One entry per DISTINCT timeframe, not per strategy, so four strategies
    sharing the 15-minute chart cost one request rather than four. The count
    is the largest any strategy on that timeframe needs.
    """
    wanted = {}
    for name in activeStrategies():
        timeframe = strategyTimeframe(name)
        wanted[timeframe] = max(wanted.get(timeframe, 0), requiredCandles(name))
    if not wanted:
        wanted[config.entry_timeframe] = requiredCandles()
    return wanted


def exitTimeframes(owner):
    """Timeframes needed to judge an exit on a position held by this owner."""
    if owner in strategies:
        names = [owner]
    else:
        names = activeStrategies() or [config.strategy]
    wanted = {}
    for name in names:
        if name not in strategies:
            continue
        timeframe = config.strategy_timeframes.get(name, config.exit_timeframe)
        wanted[timeframe] = max(wanted.get(timeframe, 0), requiredCandles(name))
    return wanted or {config.exit_timeframe: requiredCandles()}


def barsFor(name, candles_by_timeframe, fallback=None):
    """Closed candles on the timeframe this strategy runs on."""
    timeframe = config.strategy_timeframes.get(name, fallback or config.entry_timeframe)
    return closedCandles(candles_by_timeframe.get(timeframe) or [])


def evaluateEntries(candles_by_timeframe):
    """Every active entry rule, each evaluated on its own timeframe.

    The regime gate is applied per strategy rather than once globally, because
    each strategy reads it on the candles it actually trades: on hourly bars
    the 200-period line is about eight days of trend, on 15-minute bars about
    two. A scalper has no business being blocked by an eight-day view, and a
    swing rule has no business being let in by a two-day one.

    Returns one Decision per strategy, nothing filtered out - a strategy
    explaining why it did NOT fire is most of the value of the log.
    """
    decisions = []
    for name in activeStrategies():
        bars = barsFor(name, candles_by_timeframe)
        if not bars:
            decisions.append(Decision(
                hold, "%s: no candles on %s" % (name, strategyTimeframe(name)), name))
            continue
        allowed, regime_reason = regimeState(bars)
        if not allowed:
            decisions.append(Decision(
                hold, "%s [%s]: %s" % (name, strategyTimeframe(name), regime_reason), name))
            continue
        decisions.append(strategies[name][0](bars))
    return decisions


# ---------------------------------------------------------------------------
# the public entry / exit questions
# ---------------------------------------------------------------------------


def entrySignal(symbol, candles_by_timeframe):
    """Decide whether to open a position. Returns a Decision of buy or hold.

    The argument maps a timeframe to that timeframe raw ccxt rows;
    requiredTimeframes() says which ones to fetch.

    Every active strategy votes and the position opens when at least
    config.min_entry_votes of them say buy. The returned Decision carries the
    highest-priority voter as its strategy, which is what will own the exit,
    plus the full list of who agreed.

    In dummy_mode the market is ignored completely: buy only on an explicit
    --force-entry or a GitHub workflow_dispatch run.
    """
    live = activeStrategies()
    if config.dummy_mode:
        owner = live[0] if live else None
        if config.force_entry:
            return Decision(buy, "dummy_mode: --force-entry given, forcing a test entry", owner)
        if config.github_event_name == "workflow_dispatch":
            return Decision(buy, "dummy_mode: manual run, forcing a test entry", owner)
        return Decision(
            hold,
            "dummy_mode: triggered by %r without --force-entry, so no entry"
            % config.github_event_name,
        )

    decisions = evaluateEntries(candles_by_timeframe)
    voters = [decision for decision in decisions if decision.action == buy]
    names = [decision.strategy for decision in voters]

    if len(voters) < config.min_entry_votes:
        if voters:
            summary = "only %d of the required %d strategies fired (%s)" % (
                len(voters), config.min_entry_votes, ", ".join(names))
        else:
            summary = "no strategy fired"
        detail = " | ".join(decision.reason for decision in decisions)
        return Decision(hold, "%s. %s" % (summary, detail))

    owner = voters[0]
    return Decision(
        buy,
        "%s [%s on %s] (%d/%d vote(s): %s)"
        % (owner.reason, owner.strategy, strategyTimeframe(owner.strategy),
           len(voters), config.min_entry_votes, ", ".join(names)),
        owner.strategy,
        names,
    )


def exitSignal(symbol, candles_by_timeframe, owner=None):
    """Decide whether to close an open position. Returns close or hold.

    The owner is the strategy that opened this position, remembered from the
    entry. Its exit rule is the one that applies, read on its own timeframe:
    closing a 15-minute mean-reversion trade with an hourly trend rule, or the
    reverse, is how a multi-strategy bot destroys both strategies at once.

    If the owner is unknown - a position opened by hand, or state lost - the
    fallback is config.unknown_owner_exit: "any" closes as soon as any active
    strategy wants out, "all" waits for unanimity, "regime" leaves it to the
    regime break and the exchange-side stops alone.

    Not short-circuited by dummy_mode: a dummy position is a real demo
    position, and watching the exit rule work is the point of testing it.
    """
    live = activeStrategies()
    known_owner = owner in strategies and (config.strategy != multi or owner in live)

    # The regime break applies to every position regardless of owner, and is
    # judged on the timeframe of the owner so a scalp is not held open by an
    # eight-day view it never traded on.
    judge = owner if known_owner else (live[0] if live else config.strategy)
    bars = barsFor(judge, candles_by_timeframe, config.exit_timeframe)
    broken, regime_reason = regimeBroken(bars)
    if broken:
        return Decision(close, regime_reason, owner)

    if known_owner:
        decision = strategies[owner][1](bars)
        return Decision(decision.action, "%s [owner, %s]"
                        % (decision.reason, strategyTimeframe(owner)), owner)

    if config.strategy != multi:
        decision = strategies[config.strategy][1](bars)
        return Decision(decision.action, decision.reason, config.strategy)

    # Owner unknown or no longer active.
    decisions = []
    for name in live:
        name_bars = barsFor(name, candles_by_timeframe, config.exit_timeframe)
        if name_bars:
            decisions.append(strategies[name][1](name_bars))
    wants_out = [decision for decision in decisions if decision.action == close]
    detail = " | ".join(decision.reason for decision in decisions)
    note = "owner unknown (%r), falling back to UNKNOWN_OWNER_EXIT=%s" % (
        owner, config.unknown_owner_exit)

    if config.unknown_owner_exit == "any" and wants_out:
        return Decision(close, "%s: %s" % (note, wants_out[0].reason), owner)
    if config.unknown_owner_exit == "all" and decisions and len(wants_out) == len(decisions):
        return Decision(close, "%s: every active strategy wants out. %s" % (note, detail), owner)

    return Decision(hold, "%s. %s. %s" % (note, regime_reason, detail), owner)


def atrValue(candles):
    """Latest ATR reading, or None. Used by the risk layer to size stops in
    the volatility of the instrument rather than a flat percentage.

    Feed it the candles of the strategy that is opening the trade: a stop
    sized from hourly volatility would be several times too wide for a
    15-minute scalp, and would sit far outside the move it is protecting.
    """
    bars = closedCandles(candles)
    if len(bars) < config.atr_period + 2:
        return None
    return atr(bars, config.atr_period)[-1]


def requiredCandles(name=None):
    """How many candles one strategy needs, or the most any active one needs.

    Asks for generous headroom - Bybit serves up to 1000 per request and the
    extra bars cost nothing. Wilder-smoothed indicators in particular are
    recursive and only settle after several times their period, so the warmup
    multiplier is deliberately fat.
    """
    names = [name] if name in strategies else activeStrategies()
    needs = [config.candle_floor, config.atr_period * config.warmup_multiplier + 5]

    if config.regime_filter:
        needs.append(config.regime_period + config.signal_lookback_bars + 5)

    for strategy_name in names:
        if strategy_name == "trend":
            needs.append(config.ema_slow_period * config.warmup_multiplier
                         + config.signal_lookback_bars + 5)
            needs.append(config.adx_period * config.warmup_multiplier * 2
                         + config.signal_lookback_bars + 5)
        elif strategy_name == "meanrev":
            needs.append(config.rsi_period * config.warmup_multiplier
                         + config.meanrev_exit_sma_period + config.signal_lookback_bars + 5)
        elif strategy_name == "breakout":
            needs.append(max(config.breakout_lookback, config.breakout_exit_lookback)
                         + config.signal_lookback_bars + 5)
        elif strategy_name == "scalp":
            needs.append(config.bb_period * 3 + config.bb_lookback_bars + 5)

    return min(config.candle_ceiling, max(needs))
