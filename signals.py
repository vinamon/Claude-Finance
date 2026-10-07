"""
Signal generation. Pure functions over candle data - no order placement, no
network calls, no exchange objects. Feed it candles, get a decision.

EVERY NUMBER IN THIS FILE COMES FROM config
-------------------------------------------
There is not a single hardcoded period, threshold or multiplier below. Each
one reads config.<name>, and each of those reads an environment variable of
the same upper-case name, which means every knob is settable from .env without
touching code. If you find a bare number in a strategy here, it is a bug.

STRATEGIES, AND THEY CAN ALL RUN AT ONCE
----------------------------------------
  ict       sweep of a known low, structure shift, retest of the gap it left
  pullback  break of a swing high after a higher low, retest of the gap it left
  trend     EMA fast/slow crossover, confirmed by ADX trend strength
  breakout  Donchian (Turtle) channel breakout, asymmetric exit

Each one is a block: its entry and exit functions, its candle budget, and
one line in the `strategies` registry. Removing a strategy is deleting its
block and its line, nothing else.

config.strategy picks one, or "multi" runs every strategy in
config.active_strategies and enters when at least config.min_entry_votes of
them agree. By default all read the same 15-minute candles;
STRATEGY_TIMEFRAMES can move any of them to its own clock.

THE REGIME FILTER IS WHAT MAKES COMBINING THEM COHERENT
-------------------------------------------------------
Every strategy may only go long while price is above the slow regime
average, and short while it is below, so they all pull in the same direction
and differ only in what triggers the entry.

A SHORT IS THE LONG RULE ON A MIRRORED CHART
--------------------------------------------
Each rule is written once, for longs. The short side reads the same rule on
mirror() of the candles and the sentence is told back with words(), so the two
sides cannot drift apart. A held short is judged by the mirrored exit rule.
SHORT_STRATEGIES says which strategies may open a short; sidesFor() reads it.

WHY ENTRIES ARE EVENTS AND EXITS ARE STATES
-------------------------------------------
Entry looks for a crossing *event* within the last few closed candles, because
entering merely because a condition still holds would re-enter forever.

Exit looks at the current *state* instead. This bot polls on a timer and
therefore misses most individual bars, so an exit defined as a single crossing
bar would eventually be missed and leave a position stranded. "Are we on the
wrong side of the indicator right now" cannot be missed.
"""

import datetime
import re
from collections import namedtuple
from zoneinfo import ZoneInfo

import ccxt

import config

# Decision actions
enter = "enter"
hold = "hold"
close = "close"

# Sides. A position's side is never stored: it is read from the exchange.
long = "long"
short = "short"


class Decision:
    """What signals decided, and the sentence explaining it for the log.

    `strategy` names which strategy produced it. In multi-strategy mode that
    tag is what gets written into the order id and remembered, so the exit is
    judged by the same rule that opened the position.

    `side` is the side to open. `setup` is None when the stop and target are
    the ATR bracket; a strategy that reads them off the chart hands over its
    levels there instead, as a LevelSetup on the chart the rule read.
    """

    def __init__(self, action, reason, strategy=None, votes=None, side=long, setup=None):
        self.action = action
        self.reason = reason
        self.strategy = strategy
        self.votes = votes or []
        self.side = side
        self.setup = setup

    def __repr__(self):
        return "Decision(%s, %s %s, %r)" % (self.action, self.side, self.strategy, self.reason)


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


def mirror(candles):
    """The same candles upside down: every price negated, high and low swapped.

    A short is the long rule read on this chart. A close above the 20-bar high
    here is a close below the 20-bar low on the real one, a gap up here is a
    gap down there, the regime line flips with the price, and ranges - ATR,
    ADX - are unchanged. So every rule is written once, for longs, and is
    exactly mirrored for shorts by construction rather than by a second copy
    that could drift.
    """
    return [[row[0], -row[1], -row[3], -row[2], -row[4]] + list(row[5:]) for row in candles]


def oriented(candles, side):
    """The chart a rule for `side` reads: as it is for a long, mirrored for a short."""
    return mirror(candles) if side == short else candles


# Words that mean the opposite on a mirrored chart.
opposites = {"above": "below", "below": "above", "high": "low", "low": "high",
             "highs": "lows", "lows": "highs", "higher": "lower", "lower": "higher",
             "highest": "lowest", "lowest": "highest", "up": "down", "down": "up",
             "fell": "rose", "rose": "fell", "falls": "rises", "rises": "falls",
             "bullish": "bearish", "bearish": "bullish", "buy-side": "sell-side",
             "sell-side": "buy-side", "+DI": "-DI", "-DI": "+DI"}
opposite_words = re.compile(r"(?<![\w+-])(buy-side|sell-side|[+-]DI|%s)(?![\w-])"
                            % "|".join(word for word in opposites if word[0].isalpha()))


def words(text, side):
    """A reason written on the mirrored chart, told the way the real chart reads.

    For a short: above and below, high and low and their kin swap places, and
    the minus sign every mirrored price carries is dropped. Rules print only
    prices, counts and ranges, none of which is negative on the real chart.
    """
    if side != short:
        return text
    text = opposite_words.sub(lambda match: opposites[match.group(1)], text)
    return re.sub(r"(?<![\w.])-(?=\d)", "", text)


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


def crossedAbove(fast, slow, index):
    """True if fast crossed from at-or-below to above slow at `index`."""
    if index < 1:
        return False
    prev_fast, prev_slow = fast[index - 1], slow[index - 1]
    now_fast, now_slow = fast[index], slow[index]
    if None in (prev_fast, prev_slow, now_fast, now_slow):
        return False
    return prev_fast <= prev_slow and now_fast > now_slow


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


def regimeState(candles, side=long):
    """(ok, sentence) - is the market in a regime we may enter in.

    Above the slow average on the chart the rule reads: long only above it,
    and on the mirrored chart a short only below it. The oldest filter in
    trend trading, and what keeps several strategies from taking opposite
    views of one market.
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
    return False, "regime: price %.6f is below SMA%d %.6f, %s entries blocked" % (
        price[-1],
        period,
        trend_line[-1],
        side,
    )


def regimeBroken(candles):
    """(broken, sentence) - has the regime failed under an open position.

    Used as an exit override for every strategy whose exit is a rule, and for
    a position whose owner is unknown, so it is closed when the reason to
    hold it at all is gone. A strategy registered as brackets_only (ict,
    pullback) is exempt; see exitSignal. Read on the chart of the position's
    side, like regimeState. Controlled by EXIT_ON_REGIME_BREAK.
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
        return True, "regime break: price %.6f is below SMA%d %.6f" % (
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
# ict - a sweep of a known low, a structure shift, the first retest of the gap
#
# Sell stops rest under every low the market can see. When a candle trades
# through such a low and the market closes back above it, the sellers have
# been forced out and their stops filled the buyers. A close above the last
# swing high then says the structure has turned, and the fast move that did
# it usually leaves a fair-value gap: three candles where the third's low sits
# above the first's high. The entry is the first time price comes back into
# that gap. The stop goes under the setup's structure, the target at the
# nearest buy-side level that already existed before the sweep, and only the
# exchange-side stop and target close the trade.
#
# The rules are the trader agent's, issue #15 "Strategy rules". The level
# helpers below are shared by every strategy that reads levels off the chart,
# and live in this block because ict is never deleted.
# ---------------------------------------------------------------------------

day_ms = 86400000

# Candle 1, 2 and 3 of a bullish fair-value gap, and the gap they leave.
Gap = namedtuple("Gap", "c1 c2 c3 bottom top")

# What a level strategy hands the executor. `bottom` and `top` bound the gap
# the live price must still be in, `structural` is the price the setup is
# wrong under (the stop goes a buffer below it), `draws` the levels price is
# drawn to, lowest first. All on the chart the rule read.
LevelSetup = namedtuple("LevelSetup", "bottom top structural draws")

# The previous UTC day's range, and when the day after it began: the levels
# count only while untouched from `start` on.
Day = namedtuple("Day", "high low start")


def swingPoints(values, bars, above):
    found = []
    for i in range(bars, len(values) - bars):
        value = values[i]
        neighbours = values[i - bars:i] + values[i + 1:i + bars + 1]
        if all((value > other) if above else (value < other) for other in neighbours):
            found.append(i)
    return found


def swingHighs(candles, bars):
    """Indices of swing highs: a high strictly above the `bars` highs on each
    side.

    A swing at i exists only once bar i + bars has closed, so a peak whose
    confirming bars are not in `candles` yet is not returned. That delay is
    what keeps a replay from trading on a swing it could not have seen.
    """
    return swingPoints(highs(candles), bars, True)


def swingLows(candles, bars):
    """The mirror of swingHighs: a low strictly below `bars` lows on each side."""
    return swingPoints(lows(candles), bars, False)


def firstBreaks(values, above):
    """For each bar i, the first later bar whose value goes beyond values[i] -
    higher when `above`, lower otherwise - or len(values) when none has yet.

    A level is untouched up to that bar and taken on it, so this one pass
    answers both "is it still untouched" and "which bar swept it".
    """
    out = [len(values)] * len(values)
    waiting = []
    for k, value in enumerate(values):
        while waiting and ((value > values[waiting[-1]]) if above
                           else (value < values[waiting[-1]])):
            out[waiting.pop()] = k
        waiting.append(k)
    return out


def previousDay(candles, index):
    """Day(high, low, start) of the UTC day before the day of bar `index`.

    Derived from the bars' own timestamps, so it needs no other timeframe.
    None unless the window holds that whole day: a day seen in part has a high
    and low that are not the day's.
    """
    if not candles:
        return None
    today = candles[index][0] // day_ms * day_ms
    yesterday = today - day_ms
    if candles[0][0] > yesterday:
        return None
    rows = [row for row in candles[:index + 1] if yesterday <= row[0] < today]
    if not rows:
        return None
    return Day(max(row[2] for row in rows), min(row[3] for row in rows), today)


def inKillZone(open_ms, zones, tz_name):
    """True when a bar opening at `open_ms` opens inside one of the kill zones.

    `zones` is the ICT_KILL_ZONES text, read on the wall clock of `tz_name`,
    so daylight saving moves the windows the way it moves the sessions they
    stand for. "off" lets every bar through.
    """
    windows = config.parseKillZones(zones)
    if windows is None:
        return True
    local = datetime.datetime.fromtimestamp(open_ms / 1000.0, ZoneInfo(tz_name))
    minute = local.hour * 60 + local.minute
    for start, end in windows:
        if (start <= minute < end) if start < end else (minute >= start or minute < end):
            return True
    return False


def bullishGaps(candles, first, last, atr_values, min_atr, displacement_atr):
    """Bullish fair-value gaps whose candle 2 lies in [first, last].

    Candles 1, 2, 3 leave a gap when low[3] > high[1]; the gap runs from
    high[1] up to low[3]. It counts when it is at least `min_atr` ATR tall and
    candle 2 closed up with a body of at least `displacement_atr` ATR, both
    measured with the ATR at candle 3. Candle 3 must have closed.
    """
    found = []
    for c2 in range(max(1, first), min(last, len(candles) - 2) + 1):
        c1, c3 = c2 - 1, c2 + 1
        bottom, top = candles[c1][2], candles[c3][3]
        measure = atr_values[c3]
        if top <= bottom or measure is None:
            continue
        body = candles[c2][4] - candles[c2][1]
        if top - bottom < min_atr * measure or body <= 0 or body < displacement_atr * measure:
            continue
        found.append(Gap(c1, c2, c3, bottom, top))
    return found


def gapRetest(candles, gap, move_end, close_min, max_age, window):
    """(bar, None) for the retest that triggers an entry, or (None, why not).

    The retest is the FIRST bar after both candle 3 and `move_end` - the bar
    the move that left the gap ended on - whose low reaches the gap's top: one
    touch, not the best of several. A bar at or before `move_end` is part of
    the move, not a return to the gap, so it neither triggers nor uses up the
    touch. The retest triggers when it is among the last `window` closed bars,
    no later than `max_age` bars after candle 3, and closes at or above
    bottom + `close_min` of the gap. A close below the bottom by any bar from
    candle 3 up to the newest kills the gap.
    """
    newest = len(candles) - 1
    for k in range(gap.c3 + 1, newest + 1):
        if candles[k][4] < gap.bottom:
            return None, "a candle closed below the gap %d bar(s) ago" % (newest - k)
    touch = next((k for k in range(max(gap.c3, move_end) + 1, newest + 1)
                  if candles[k][3] <= gap.top), None)
    if touch is None:
        return None, "no retest of the gap yet"
    if touch > gap.c3 + max_age:
        return None, ("the first retest came %d bar(s) after the gap, over the %d-bar limit"
                      % (touch - gap.c3, max_age))
    if touch <= newest - window:
        return None, ("the first retest was %d bar(s) ago, outside the last %d closed bar(s)"
                      % (newest - touch, window))
    entry_floor = gap.bottom + close_min * (gap.top - gap.bottom)
    if candles[touch][4] < entry_floor:
        return None, ("the retest closed at %.6f, under %.6f, the entry level inside the gap"
                      % (candles[touch][4], entry_floor))
    return touch, None


def stopReference(candles, gap, retest, leg_low, mode):
    """The price the setup is wrong under: candle 1's low, or the leg low when
    `mode` is "leglow", and never above a low printed from candle 3 to the
    retest."""
    anchor = leg_low if mode == "leglow" else candles[gap.c1][3]
    return min([anchor] + lows(candles[gap.c3:retest + 1]))


def untouchedHighs(candles, swings, first, last):
    """The highs of the swings with index in [first, last] that no later bar
    has traded above, through the newest one."""
    broken = firstBreaks(highs(candles), True)
    return [candles[i][2] for i in swings if first <= i <= last and broken[i] == len(candles)]


def dayHighDraw(candles, day):
    """The previous UTC day's high while nothing since that day has traded
    above it, else None."""
    since = [row[2] for row in candles if row[0] >= day.start]
    if since and max(since) > day.high:
        return None
    return day.high


def levelSetup(bottom, top, structural, draws):
    """A LevelSetup, its draws sorted from the lowest."""
    return LevelSetup(bottom, top, structural, tuple(sorted(draws)))


def sweptLevels(candles, swing_lows, first, lookback, reclaim):
    """Bar -> (level, what it was) for every bar from `first` on that swept an
    untouched sell-side level and closed back above it in time.

    The level is a swing low from the last `lookback` bars, confirmed before
    the sweeping bar, or the previous UTC day's low. When one bar takes
    several, the lowest is named.
    """
    newest = len(candles) - 1
    candle_lows = lows(candles)
    taken_at = firstBreaks(candle_lows, False)
    candidates = {}
    for i in swing_lows:
        s = taken_at[i]
        # A swing low cannot be broken by its own confirming bars, so the
        # swing was already known when bar s took it.
        if first <= s <= newest and i >= s - lookback:
            candidates.setdefault(s, []).append((candle_lows[i], "swing low"))
    for s in range(max(1, first), newest + 1):
        day = previousDay(candles, s)
        if day is None:
            continue
        before = [row[3] for row in candles[:s] if row[0] >= day.start]
        if candle_lows[s] < day.low and (not before or min(before) >= day.low):
            candidates.setdefault(s, []).append((day.low, "previous UTC day's low"))

    swept = {}
    for s, levels in candidates.items():
        reclaimed = [(level, name) for level, name in levels
                     if any(candles[k][4] > level for k in range(s, min(s + reclaim, newest) + 1))]
        if reclaimed:
            swept[s] = min(reclaimed)
    return swept


def structureShift(candles, swing_highs, sweep, max_bars, bars):
    """(bar, swing high index) of the first close above the last swing high
    before the sweep, within `max_bars` of it, or None. The swing high must be
    confirmed by the bar that closes above it."""
    price = closes(candles)
    prior = [j for j in swing_highs if j < sweep]
    for m in range(sweep, min(sweep + max_bars, len(candles) - 1) + 1):
        known = [j for j in prior if j + bars <= m]
        if known and price[m] > candles[known[-1]][2]:
            return m, known[-1]
    return None


def retestedGap(name, candles, gap, move_end):
    """(bar, None) for the retest of the gap that triggers an entry, or (None,
    the hold Decision saying why not): the first touch after `move_end`, inside
    the signal window and the age limit, closing in the upper part of the gap,
    on a bar that opened inside the kill zones."""
    retest, why = gapRetest(candles, gap, move_end, config.ict_entry_close_min,
                            config.ict_fvg_max_age_bars, config.signal_lookback_bars)
    if retest is None:
        return None, Decision(hold, "%s: gap %.6f-%.6f: %s" % (name, gap.bottom, gap.top, why),
                              name)

    if not inKillZone(candles[retest][0], config.ict_kill_zones, config.ict_kill_zone_tz):
        opened = datetime.datetime.fromtimestamp(candles[retest][0] / 1000.0,
                                                 ZoneInfo(config.ict_kill_zone_tz))
        return None, Decision(
            hold, "%s: the retest of gap %.6f-%.6f opened at %s %s, outside the kill zones %s"
            % (name, gap.bottom, gap.top, opened.strftime("%H:%M"), config.ict_kill_zone_tz,
               config.ict_kill_zones), name)
    return retest, None


def structureAndDraws(candles, swing_highs, gap, retest, leg_low, anchor):
    """(the price the setup is wrong under, the buy-side levels above it).

    Targets are levels that already existed before `anchor`, the bar the move
    began from: swing highs confirmed by the bar before it and untouched since,
    and the previous UTC day's high of its day.
    """
    structural = stopReference(candles, gap, retest, leg_low, config.ict_stop_ref)
    bars = config.ict_swing_bars
    draws = untouchedHighs(candles, swing_highs, anchor - config.ict_liquidity_lookback_bars,
                           anchor - 1 - bars)
    day = previousDay(candles, anchor)
    if day is not None and dayHighDraw(candles, day) is not None:
        draws.append(day.high)
    return structural, draws


def ictEntry(candles):
    bars = config.ict_swing_bars
    lookback = config.ict_liquidity_lookback_bars
    window = config.signal_lookback_bars
    # How far back a sweep can sit and still have its retest inside the
    # signal window: the retest is at most ICT_FVG_MAX_AGE_BARS after candle
    # 3, which is at most one bar after the structure shift.
    reach = window + config.ict_fvg_max_age_bars + config.ict_mss_max_bars
    need = lookback + reach + 2 * bars + 2
    if len(candles) < need:
        return Decision(hold, "ict: need %d closed candles, have %d" % (need, len(candles)), "ict")

    atr_values = atr(candles, config.atr_period)
    if atr_values[-1] is None:
        return Decision(hold, "ict: ATR%d not seeded yet" % config.atr_period, "ict")

    newest = len(candles) - 1
    swing_highs = swingHighs(candles, bars)
    swept = sweptLevels(candles, swingLows(candles, bars), newest - reach, lookback,
                        config.ict_sweep_reclaim_bars)
    if not swept:
        return Decision(hold, "ict: no sweep of a swing low or the previous UTC day's low in "
                        "the last %d closed bar(s)" % (reach + 1), "ict")

    # Only the most recent structure shift counts. Two sweeps that shift on
    # the same bar are one leg, read from its first sweep.
    shift = None
    for s in sorted(swept):
        found = structureShift(candles, swing_highs, s, config.ict_mss_max_bars, bars)
        if found and (shift is None or found[0] > shift[0]):
            shift = (found[0], found[1], s)
    if shift is None:
        s = max(swept)
        return Decision(hold, "ict: swept the %s %.6f %d bar(s) ago, but no close broke "
                        "above the last swing high within %d bar(s)"
                        % (swept[s][1], swept[s][0], newest - s, config.ict_mss_max_bars), "ict")
    m, high_index, s = shift
    level, level_name = swept[s]

    gaps = bullishGaps(candles, s, m, atr_values, config.ict_fvg_min_atr,
                       config.ict_displacement_min_atr)
    if not gaps:
        return Decision(hold, "ict: structure shifted above %.6f %d bar(s) ago with no gap of "
                        "%.2f ATR behind a %.2f ATR candle"
                        % (candles[high_index][2], newest - m, config.ict_fvg_min_atr,
                           config.ict_displacement_min_atr), "ict")
    gap = max(gaps, key=lambda found: found.top)

    retest, held = retestedGap("ict", candles, gap, m)
    if retest is None:
        return held

    structural, draws = structureAndDraws(candles, swing_highs, gap, retest,
                                          min(lows(candles[s:m + 1])), s)

    return Decision(
        enter,
        "ict: swept the %s %.6f %d bar(s) ago and closed back above it, broke above the "
        "swing high %.6f %d bar(s) ago, and the first retest of the gap %.6f-%.6f closed at "
        "%.6f %d bar(s) ago (structure low %.6f, %d draw(s))"
        % (level_name, level, newest - s, candles[high_index][2], newest - m, gap.bottom,
           gap.top, candles[retest][4], newest - retest, structural, len(draws)),
        "ict",
        setup=levelSetup(gap.bottom, gap.top, structural, draws),
    )


def levelCandles(name):
    """Closed candles a level strategy needs: a whole previous UTC day plus
    today so far, or the liquidity lookback if longer, behind the furthest
    move that can still be traded."""
    per_day = day_ms // 1000 // ccxt.Exchange.parse_timeframe(strategyTimeframe(name))
    return (max(config.ict_liquidity_lookback_bars, 2 * per_day) + config.ict_mss_max_bars
            + config.ict_fvg_max_age_bars + config.signal_lookback_bars
            + 2 * config.ict_swing_bars + 5)


def ictCandles():
    """Closed candles ict needs."""
    return levelCandles("ict")


def ictExit(candles):
    """None: an ict position is closed by its exchange-side stop and target
    only. The exit on a bearish structure shift is deferred (#15)."""
    return Decision(hold, "ict exit: none, left to the exchange-side stop and target", "ict")


# ---------------------------------------------------------------------------
# pullback - the break of a swing high after a higher low, then the gap retest
#
# ict without the sweep. In an uptrend the market makes a swing high, dips to a
# low that sits above the swing low before it (the higher low, so the trend is
# intact), and then closes above that swing high in one big candle that leaves
# a fair-value gap. The entry is the first retest of the gap, exactly as for
# ict, with the same stop, chase limit and targets: the low of the dip plays
# the part of the swept low. Most of the time no untouched level stands above
# the price, and the target is the fallback multiple of the risk.
#
# The rules are the trader agent's, issue #15 "Strategy rules". Everything not
# named PULLBACK_ is an ICT_ setting shared with ict; this block is the leg
# search, its one setting, and nothing else, so deleting pullback removes it
# whole.
# ---------------------------------------------------------------------------

# Bar indices of a pullback leg: the swing high H, the first close above it
# (the break), and the lowest low between them (the leg low).
Leg = namedtuple("Leg", "high break_bar low")


def pullbackLeg(candles, swing_highs, swing_lows, bars, reach, max_bars):
    """(Leg, None) for the latest break of a swing high that follows a higher
    low, or (None, why not).

    H is judged AS OF the break bar: the latest swing high already confirmed
    when that bar closed, not the latest one today. A swing high that forms
    after the break has nothing to do with it, and judged from today it would
    sit above the break and hide it, so no setup could ever trigger.

    The break is the FIRST close above H, among the last `reach` + 1 bars; only
    the most recent break is judged. The leg low is the lowest low from the bar
    after H to the break (the latest of tied lows). It must lie above the low of
    the last swing low before H - a higher low - and no more than `max_bars`
    before the break.
    """
    price, high, low = closes(candles), highs(candles), lows(candles)
    newest = len(candles) - 1
    for b in range(newest, max(newest - reach, 0) - 1, -1):
        known = [j for j in swing_highs if j + bars <= b]
        if not known:
            continue
        j = known[-1]
        if price[b] <= high[j] or any(price[k] > high[j] for k in range(j + 1, b)):
            continue

        before = [i for i in swing_lows if i < j]
        if not before:
            return None, ("no confirmed swing low before the swing high %.6f, so no higher low "
                          "to judge" % high[j])
        leg_low = min(low[j + 1:b + 1])
        i = max(k for k in range(j + 1, b + 1) if low[k] == leg_low)
        if leg_low <= low[before[-1]]:
            return None, ("the leg low %.6f is not above the prior swing low %.6f: a lower low, "
                          "not a pullback" % (leg_low, low[before[-1]]))
        if b - i > max_bars:
            return None, ("the leg low is %d bar(s) before the break, over the %d-bar limit"
                          % (b - i, max_bars))
        return Leg(j, b, i), None
    return None, ("no close above the latest swing high in the last %d closed bar(s)"
                  % (reach + 1))


def pullbackEntry(candles):
    bars = config.ict_swing_bars
    lookback = config.ict_liquidity_lookback_bars
    window = config.signal_lookback_bars
    # How old the break may be: the retest window plus the retest's age limit
    # (issue #15).
    reach = window + config.ict_fvg_max_age_bars
    need = lookback + reach + config.ict_mss_max_bars + 2 * bars + 2
    if len(candles) < need:
        return Decision(hold, "pullback: need %d closed candles, have %d" % (need, len(candles)),
                        "pullback")

    atr_values = atr(candles, config.atr_period)
    if atr_values[-1] is None:
        return Decision(hold, "pullback: ATR%d not seeded yet" % config.atr_period, "pullback")

    newest = len(candles) - 1
    swing_highs = swingHighs(candles, bars)
    leg, why = pullbackLeg(candles, swing_highs, swingLows(candles, bars), bars, reach,
                           config.ict_mss_max_bars)
    if leg is None:
        return Decision(hold, "pullback: %s" % why, "pullback")

    broken = candles[leg.high][2]
    gaps = bullishGaps(candles, leg.low, leg.break_bar, atr_values, config.ict_fvg_min_atr,
                       config.pullback_displacement_min_atr)
    if not gaps:
        return Decision(hold, "pullback: broke above the swing high %.6f %d bar(s) ago with no "
                        "gap of %.2f ATR behind a %.2f ATR candle"
                        % (broken, newest - leg.break_bar, config.ict_fvg_min_atr,
                           config.pullback_displacement_min_atr), "pullback")
    gap = max(gaps, key=lambda found: found.top)

    retest, held = retestedGap("pullback", candles, gap, leg.break_bar)
    if retest is None:
        return held

    # H is broken by definition, so no draw could be it; it is left out
    # explicitly because the rule says so.
    structural, draws = structureAndDraws(
        candles, [i for i in swing_highs if i != leg.high], gap, retest, candles[leg.low][3],
        leg.low)

    return Decision(
        enter,
        "pullback: broke above the swing high %.6f %d bar(s) ago after a higher low of %.6f, "
        "and the first retest of the gap %.6f-%.6f closed at %.6f %d bar(s) ago (structure "
        "low %.6f, %d draw(s))"
        % (broken, newest - leg.break_bar, candles[leg.low][3], gap.bottom, gap.top,
           candles[retest][4], newest - retest, structural, len(draws)),
        "pullback",
        setup=levelSetup(gap.bottom, gap.top, structural, draws),
    )


def pullbackCandles():
    """Closed candles pullback needs: the same as ict, on its own clock."""
    return levelCandles("pullback")


def pullbackExit(candles):
    """None: a pullback position is closed by its exchange-side stop and
    target only, as for ict."""
    return Decision(hold, "pullback exit: none, left to the exchange-side stop and target",
                    "pullback")


# ---------------------------------------------------------------------------
# trend - EMA crossover confirmed by ADX
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
            enter,
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


def trendCandles():
    """Closed candles trend needs: the slow EMA and ADX are recursive and only
    settle after several times their period."""
    return max(config.ema_slow_period * config.warmup_multiplier,
               config.adx_period * config.warmup_multiplier * 2) + config.signal_lookback_bars + 5


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
# breakout - Donchian channel, Turtle style
#
# Buy a close above the highest high of the previous N bars; leave on a close
# below the lowest low of the previous M, where M is SHORTER than N.
#
# The asymmetry is the whole point and it is the original Turtle rule. A
# symmetric channel gives back most of a move before admitting it is over,
# because by the time price makes a new N-bar low the trend has been dead for
# a long time. BREAKOUT_LOOKBACK sets the entry channel,
# BREAKOUT_EXIT_LOOKBACK the (shorter) exit channel. An exit lookback of 0
# switches the rule exit off: the position is left to its exchange-side stop
# and target.
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
                enter,
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


def breakoutCandles():
    return (max(config.breakout_lookback, config.breakout_exit_lookback)
            + config.signal_lookback_bars + 5)


def breakoutExit(candles):
    window = config.breakout_exit_lookback
    if window == 0:
        return Decision(
            hold, "breakout exit: off (BREAKOUT_EXIT_LOOKBACK=0), left to the stop and target",
            "breakout",
        )
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
# dispatch
# ---------------------------------------------------------------------------

# What each strategy is: its entry rule, its exit rule, and how many closed
# candles it needs. Priority between strategies is not here; it is the order
# of ACTIVE_STRATEGIES. `brackets_only` marks a strategy whose position is
# closed by its exchange-side stop and target alone: its stop sits where the
# setup is wrong and its target was judged against it, so not even the
# regime break overrides them.
Strategy = namedtuple("Strategy", "entry exit candles brackets_only", defaults=(False,))

strategies = {
    "ict": Strategy(ictEntry, ictExit, ictCandles, brackets_only=True),
    "pullback": Strategy(pullbackEntry, pullbackExit, pullbackCandles, brackets_only=True),
    "trend": Strategy(trendEntry, trendExit, trendCandles),
    "breakout": Strategy(breakoutEntry, breakoutExit, breakoutCandles),
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


def sidesFor(name):
    """The sides this strategy may open, long first: long always, short too
    when SHORT_STRATEGIES names it."""
    return [long, short] if name in config.short_strategies else [long]


def shortStrategies():
    """The live strategies that may open a short, in priority order."""
    return [name for name in activeStrategies() if short in sidesFor(name)]


def sideCanOpen(side):
    """Whether any live strategy may open a position on `side`: a long
    whenever one is live, a short only when one is named in SHORT_STRATEGIES."""
    return bool(shortStrategies() if side == short else activeStrategies())


def entryFor(name, bars, side):
    """One strategy's entry rule for one side, read on that side's chart.

    A rule is written once, for longs. A short runs the same rule on the
    mirrored chart, behind the same regime gate, and the sentence it gives is
    told back the way the real chart reads. The Decision carries the side to
    open.
    """
    chart = oriented(bars, side)
    allowed, regime_reason = regimeState(chart, side)
    if allowed:
        decision = strategies[name].entry(chart)
    else:
        decision = Decision(
            hold, "%s [%s]: %s" % (name, strategyTimeframe(name), regime_reason), name)
    return Decision(decision.action, words(decision.reason, side), decision.strategy,
                    decision.votes, side, decision.setup)


def exitFor(name, bars, side):
    """One strategy's exit rule for a position on `side`, on that side's chart."""
    decision = strategies[name].exit(oriented(bars, side))
    return Decision(decision.action, words(decision.reason, side), decision.strategy,
                    side=side)


def evaluateEntries(candles_by_timeframe):
    """Every active entry rule, each evaluated on its own timeframe.

    The regime gate is applied per strategy rather than once globally, because
    each strategy reads it on the candles it actually trades: on hourly bars
    the 200-period line is about eight days of trend, on 15-minute bars about
    two. A scalper has no business being blocked by an eight-day view, and a
    swing rule has no business being let in by a two-day one.

    Each strategy is read once per side it may open (sidesFor). With the
    regime filter on, the gate lets at most one side through on any bar -
    long above the line, short below it - so the two sides of one rule never
    fire together.

    Returns one Decision per strategy and side, nothing filtered out - a
    strategy explaining why it did NOT fire is most of the value of the log.
    """
    decisions = []
    for name in activeStrategies():
        bars = barsFor(name, candles_by_timeframe)
        if not bars:
            decisions.append(Decision(
                hold, "%s: no candles on %s" % (name, strategyTimeframe(name)), name))
            continue
        for side in sidesFor(name):
            decisions.append(entryFor(name, bars, side))
    return decisions


# ---------------------------------------------------------------------------
# the public entry / exit questions
# ---------------------------------------------------------------------------


def entrySignal(symbol, candles_by_timeframe):
    """Decide whether to open a position. Returns a Decision of enter or hold.

    The argument maps a timeframe to that timeframe raw ccxt rows;
    requiredTimeframes() says which ones to fetch.

    Every active strategy votes and the position opens when at least
    config.min_entry_votes of them say enter. Votes count on one side only:
    the side of the highest-priority strategy that fired, because a long vote
    and a short vote on one symbol are not agreement. The returned Decision
    carries that strategy, which is what will own the exit, plus the full
    list of who agreed.

    In dummy_mode the market is ignored completely: enter only on an explicit
    --force-entry or a GitHub workflow_dispatch run.
    """
    live = activeStrategies()
    if config.dummy_mode:
        owner = live[0] if live else None
        if config.force_entry:
            return Decision(enter, "dummy_mode: --force-entry given, forcing a test entry", owner)
        if config.github_event_name == "workflow_dispatch":
            return Decision(enter, "dummy_mode: manual run, forcing a test entry", owner)
        return Decision(
            hold,
            "dummy_mode: triggered by %r without --force-entry, so no entry"
            % config.github_event_name,
        )

    decisions = evaluateEntries(candles_by_timeframe)
    voters = [decision for decision in decisions if decision.action == enter]
    if voters:
        voters = [decision for decision in voters if decision.side == voters[0].side]
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
        enter,
        "%s [%s on %s] (%d/%d vote(s): %s)"
        % (owner.reason, owner.strategy, strategyTimeframe(owner.strategy),
           len(voters), config.min_entry_votes, ", ".join(names)),
        owner.strategy,
        names,
        owner.side,
        owner.setup,
    )


def exitSignal(symbol, candles_by_timeframe, owner=None, side=long):
    """Decide whether to close an open position. Returns close or hold.

    `side` is the side of the position, read from the exchange. Every rule
    below is judged on that side's chart, so a held short is closed by the
    mirror of the rule that would close a long, never by the long rule.

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

    # The regime break applies to every position but one whose recorded owner
    # closes by its brackets alone - live or not, its bracket on the exchange
    # is still that strategy's. It is judged on the timeframe of the owner so
    # a 15-minute trade is not held open by an hourly view it never traded on.
    judge = owner if known_owner else (live[0] if live else config.strategy)
    bars = barsFor(judge, candles_by_timeframe, config.exit_timeframe)
    if owner in strategies and strategies[owner].brackets_only:
        regime_reason = ("no regime-break exit for %s, left to the exchange-side stop and "
                         "target" % owner)
    else:
        broken, regime_reason = regimeBroken(oriented(bars, side))
        regime_reason = words(regime_reason, side)
        if broken:
            return Decision(close, regime_reason, owner, side=side)

    if known_owner:
        decision = exitFor(owner, bars, side)
        return Decision(decision.action, "%s [owner, %s]"
                        % (decision.reason, strategyTimeframe(owner)), owner, side=side)

    if config.strategy != multi:
        decision = exitFor(config.strategy, bars, side)
        return Decision(decision.action, decision.reason, config.strategy, side=side)

    # Owner unknown or no longer active.
    decisions = []
    for name in live:
        name_bars = barsFor(name, candles_by_timeframe, config.exit_timeframe)
        if name_bars:
            decisions.append(exitFor(name, name_bars, side))
    wants_out = [decision for decision in decisions if decision.action == close]
    detail = " | ".join(decision.reason for decision in decisions)
    note = "owner unknown (%r), falling back to UNKNOWN_OWNER_EXIT=%s" % (
        owner, config.unknown_owner_exit)

    if config.unknown_owner_exit == "any" and wants_out:
        return Decision(close, "%s: %s" % (note, wants_out[0].reason), owner, side=side)
    if config.unknown_owner_exit == "all" and decisions and len(wants_out) == len(decisions):
        return Decision(close, "%s: every active strategy wants out. %s" % (note, detail), owner,
                        side=side)

    return Decision(hold, "%s. %s. %s" % (note, regime_reason, detail), owner, side=side)


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
        needs.append(strategies[strategy_name].candles())

    return min(config.candle_ceiling, max(needs))
