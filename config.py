"""
Central configuration.

Secrets (API keys, ntfy topic) come from environment variables only, which in
GitHub Actions are fed from repository Secrets. Nothing sensitive is ever
hardcoded here, because this repository is public.

EVERYTHING HERE IS A KNOB
-------------------------
Every setting below reads an environment variable of the same upper-case name
and falls back to the value written here. Nothing downstream hardcodes any of
them: signals.py, executor.py and run.py read config.<name> and never a bare
number. So changing behaviour - the loop interval, a boolean, an indicator
period, which strategies are live - means editing .env and restarting, never
editing code.

Types are inferred from the helper used: envBool understands
1/true/yes/on, envInt and envFloat parse numbers, envList splits on commas.
"""

import os
from pathlib import Path

# Load a local .env file if one exists, so running on a laptop does not mean
# exporting variables by hand every time you open a terminal. Values already
# present in the real environment win, which keeps GitHub Actions unaffected.
#
# The path is pinned to this file's own directory rather than discovered from
# the current working directory: otherwise "python scripts/test_connection.py"
# and "python run.py" could disagree about which .env is in play, depending on
# where the terminal happens to be sitting.
env_path = Path(__file__).resolve().parent / ".env"
dotenv_loaded = False
dotenv_missing = False

try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
        dotenv_loaded = True
except ImportError:
    # Staying quiet here was a mistake worth not repeating. If a .env sits
    # right there and python-dotenv cannot be imported, the keys never load
    # and the only symptom is "BYBIT_API_KEY is not set" - which sends you
    # hunting through a file that is perfectly correct. The usual cause is a
    # virtualenv that is not active, so say so.
    dotenv_missing = env_path.exists()

# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------


def envStr(name, default):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def envBool(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def envInt(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return int(value)


def envFloat(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return float(value)


def envList(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# secrets - never hardcode, never log
# ---------------------------------------------------------------------------

bybit_api_key = os.environ.get("BYBIT_API_KEY", "")
bybit_api_secret = os.environ.get("BYBIT_API_SECRET", "")

# ntfy topic doubles as the only access control ntfy.sh has, so treat it as a
# secret: a long random string, stored in GitHub Secrets, never printed.
ntfy_topic = os.environ.get("NTFY_TOPIC", "")
ntfy_server = envStr("NTFY_SERVER", "https://ntfy.sh")

# ---------------------------------------------------------------------------
# safety switch
# ---------------------------------------------------------------------------

# dummy_mode ignores the market entirely and only enters when the run was
# started by hand. Defaults to True on purpose: live signal trading has to be
# switched on deliberately, never by forgetting.
dummy_mode = envBool("DUMMY_MODE", True)

# GitHub Actions sets this. "workflow_dispatch" means a human pressed the
# button; "schedule" means cron fired.
github_event_name = envStr("GITHUB_EVENT_NAME", "local")

# The local equivalent of pressing that button. Running off a laptop there is
# no GITHUB_EVENT_NAME, so without this dummy_mode could never open its test
# position. Set by run.py --force-entry, and deliberately one-shot: in loop
# mode it applies to the first cycle only, otherwise a forced entry would fire
# on every symbol every few minutes forever.
force_entry = envBool("FORCE_ENTRY", False)

# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------

# Loop by default, or run once and exit. Command-line --loop / --once win.
autostart = envBool("AUTOSTART", False)

# Minutes between cycles in loop mode. Safe to change freely: the signal logic
# works off closed candles, so polling faster only means noticing a closed bar
# sooner, never a different decision.
loop_interval_minutes = envInt("LOOP_INTERVAL_MINUTES", 5)

# Seconds per idempotency bucket for orderLinkId. Two entry attempts landing
# in the same bucket produce the same client order id, and Bybit rejects the
# duplicate - which is the point, because a retry after an ambiguous timeout
# must not open a second position.
#
# Derived from the loop interval so the two cannot drift apart: with a bucket
# longer than the interval, a cycle that legitimately wants to retry gets
# blocked until the bucket rolls over. Set ORDER_BUCKET_SECONDS to pin it.
order_bucket_seconds = envInt("ORDER_BUCKET_SECONDS", max(60, loop_interval_minutes * 60))

# ---------------------------------------------------------------------------
# what to trade
# ---------------------------------------------------------------------------

# ccxt unified symbols for Bybit USDT perpetuals. More symbols is the single
# most effective way to get more trades without loosening any rule: the same
# strategy on ten markets fires roughly ten times as often as on one, and the
# trades are far less correlated than the ones you would get by lowering a
# threshold on a single market.
symbols = envList(
    "SYMBOLS",
    [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "BNB/USDT:USDT",
        "XRP/USDT:USDT",
        "DOGE/USDT:USDT",
        "ADA/USDT:USDT",
        "AVAX/USDT:USDT",
        "LINK/USDT:USDT",
    ],
)

# "trend", "meanrev", "breakout", or "multi" to run several at once.
strategy = envStr("STRATEGY", "multi")

# Which strategies are live when STRATEGY=multi, in PRIORITY order. When more
# than one fires on the same bar the first listed owns the position, and its
# exit rule is what will close it. Ignored unless STRATEGY=multi.
active_strategies = envList("ACTIVE_STRATEGIES", ["trend", "breakout", "meanrev"])

# How many active strategies must agree before a position opens. 1 is "any
# signal trades" and produces the most trades; raising it demands confluence
# and produces far fewer, better-supported ones.
min_entry_votes = envInt("MIN_ENTRY_VOTES", 1)

# Hard ceiling on simultaneous open positions across all symbols, so a market
# where everything breaks out at once cannot put the whole account to work in
# one direction. Symbols are considered in SYMBOLS order. 0 disables the cap.
max_open_positions = envInt("MAX_OPEN_POSITIONS", 5)

# ---------------------------------------------------------------------------
# position size and leverage
# ---------------------------------------------------------------------------

# Notional value of each position in USDT. This is qty * price, NOT the margin
# you put up. Margin used is roughly notional / leverage.
position_notional_usdt = envFloat("POSITION_NOTIONAL_USDT", 1000.0)

# 1 means no leverage. Higher leverage does not change the notional above, it
# changes how little margin backs it, i.e. how close liquidation sits.
leverage = envInt("LEVERAGE", 1)

# ---------------------------------------------------------------------------
# timeframes
# ---------------------------------------------------------------------------

# Timeframe the entry signal is evaluated on.
entry_timeframe = envStr("ENTRY_TIMEFRAME", "1h")

# Timeframe the exit signal is evaluated on. Defaults to the entry timeframe,
# and that default is the point: the exit rule uses the SAME indicator and the
# SAME periods as the entry, so evaluating it on a much shorter timeframe is
# not a symmetric exit, it is a far noisier one.
#
# Measured on live data: the same moving-average pair read 258 hours of
# history on 1h and 4 hours on 1m, a 65x difference. A short exit timeframe
# closes positions the entry trend still endorses and pays fees for it.
exit_timeframe = envStr("EXIT_TIMEFRAME", entry_timeframe)

# How many recently closed candles to scan for a signal event. A polled bot
# can miss bars, so looking only at the newest bar would throw away crossings.
signal_lookback_bars = envInt("SIGNAL_LOOKBACK_BARS", 3)

# ---------------------------------------------------------------------------
# regime filter - the gate every strategy passes through
# ---------------------------------------------------------------------------

# Long entries only while price is above this average. This is what lets a
# mean-reversion rule and a trend-following rule coexist: mean reversion
# becomes "buy the dip inside an uptrend" instead of "catch the falling
# knife". Turning it off makes the strategies fight each other.
regime_filter = envBool("REGIME_FILTER", True)
regime_period = envInt("REGIME_PERIOD", 200)

# Close any open position, whichever strategy opened it, when price falls back
# under the regime average. The reason to be long at all has gone.
exit_on_regime_break = envBool("EXIT_ON_REGIME_BREAK", True)

# What to do about a position whose owning strategy is unknown - opened by
# hand, or the owner file was lost. "any" closes as soon as any active
# strategy wants out, "all" waits for unanimity, "regime" leaves it to the
# regime break and the exchange-side stops.
unknown_owner_exit = envStr("UNKNOWN_OWNER_EXIT", "any")

# ---------------------------------------------------------------------------
# strategy 1: trend - EMA crossover confirmed by ADX
# ---------------------------------------------------------------------------

# Shorter and exponential rather than the classic SMA 50/200, which is a
# DAILY-chart signal: on an intraday timeframe 50/200 fires once in months.
# 20/50 on 1h is roughly a day against two days of history - a normal short
# swing horizon.
ema_fast_period = envInt("EMA_FAST_PERIOD", 20)
ema_slow_period = envInt("EMA_SLOW_PERIOD", 50)

# ADX measures trend STRENGTH with no opinion on direction. A bare moving
# average crossover bleeds in sideways markets because it fires on every
# wiggle; requiring ADX above a floor is the standard fix. Below ~20 is
# conventionally "no trend". Set ADX_MIN to 0 to disable the filter.
adx_period = envInt("ADX_PERIOD", 14)
adx_min = envFloat("ADX_MIN", 20.0)

# ---------------------------------------------------------------------------
# strategy 2: meanrev - short-RSI pullback inside an uptrend
# ---------------------------------------------------------------------------

# A 2-period RSI, not the usual 14. The short period is what makes this the
# Connors "RSI-2" pullback pattern rather than a slow oscillator: it collapses
# into single digits on an ordinary pullback and recovers within a bar or two,
# which is precisely the move being bought.
rsi_period = envInt("RSI_PERIOD", 2)
rsi_oversold = envFloat("RSI_OVERSOLD", 10.0)
rsi_overbought = envFloat("RSI_OVERBOUGHT", 70.0)

# Connors' own exit: leave when the close snaps back above a short average.
meanrev_exit_sma_period = envInt("MEANREV_EXIT_SMA_PERIOD", 5)

# ---------------------------------------------------------------------------
# strategy 3: breakout - Donchian channel, Turtle style
# ---------------------------------------------------------------------------

# Enter on a close above the N-bar high, leave on a close below the M-bar low,
# M shorter than N. The asymmetry is the original Turtle rule: a symmetric
# channel gives back most of a move before admitting the trend is over.
breakout_lookback = envInt("BREAKOUT_LOOKBACK", 20)
breakout_exit_lookback = envInt("BREAKOUT_EXIT_LOOKBACK", 10)

# ---------------------------------------------------------------------------
# risk model - where the stop, target and trail actually go
# ---------------------------------------------------------------------------

# "atr" sizes every exit in the instrument's own recent volatility; "pct"
# uses flat percentages of the entry price.
#
# ATR is the upgrade worth having. A flat 5% stop is a different amount of
# risk on BTC than on a small alt, and a different amount on the same coin in
# a calm week versus a violent one - so it is either too tight and gets hit by
# noise, or too wide and pays for it. ATR adapts to both.
#
# If ATR cannot be computed (not enough candles) the code falls back to the
# percentage values automatically, so both sets below stay meaningful.
risk_model = envStr("RISK_MODEL", "atr")

atr_period = envInt("ATR_PERIOD", 14)
atr_stop_mult = envFloat("ATR_STOP_MULT", 2.0)
atr_target_mult = envFloat("ATR_TARGET_MULT", 4.0)
atr_trail_mult = envFloat("ATR_TRAIL_MULT", 1.5)
atr_trail_activation_mult = envFloat("ATR_TRAIL_ACTIVATION_MULT", 3.0)

# Percentage model, and the fallback for the ATR model. Fractions of the entry
# price: 0.05 == 5%. Set any of them to 0 to disable that leg.
#
# The stop is a disaster brake, not the primary exit - the strategy's own rule
# is what normally closes a position - so it is set wide enough to stay out of
# the way of ordinary noise.
stop_loss_pct = envFloat("STOP_LOSS_PCT", 0.05)
take_profit_pct = envFloat("TAKE_PROFIT_PCT", 0.10)

# Trailing stop. Bybit's API takes a PRICE DISTANCE here, not a percentage
# (verified against the v5 docs: "Trailing stop by price distance"), so this
# fraction gets multiplied by the entry price before being sent.
trailing_stop_pct = envFloat("TRAILING_STOP_PCT", 0.03)

# Trailing stop activation. The trail stays dormant until price reaches
# entry * (1 + this).
#
# MUST be >= trailing_stop_pct, and validate() refuses to run otherwise. Bybit
# places the trail's first trigger at (activation - distance). If the
# activation is the smaller of the two, that trigger lands BELOW your entry,
# so arming the trail caps a loss instead of protecting a profit - and it
# fires long before the stop loss would. Observed live at 1% activation with a
# 1.5% distance: the trail sat 0.49% under the entry price. The same rule
# applies to ATR_TRAIL_ACTIVATION_MULT vs ATR_TRAIL_MULT.
trailing_activation_pct = envFloat("TRAILING_ACTIVATION_PCT", 0.05)

# ---------------------------------------------------------------------------
# candle budget
# ---------------------------------------------------------------------------

# Wilder-smoothed indicators are recursive and only settle after several times
# their period, so signals.requiredCandles() asks for period * this.
warmup_multiplier = envInt("WARMUP_MULTIPLIER", 10)

# Never request fewer than this, never more than this. Bybit serves 1000 per
# request and the extra bars cost nothing.
candle_floor = envInt("CANDLE_FLOOR", 60)
candle_ceiling = envInt("CANDLE_CEILING", 1000)

# ---------------------------------------------------------------------------
# execution / plumbing
# ---------------------------------------------------------------------------

# Bybit product category. This bot assumes USDT perpetuals.
category = envStr("CATEGORY", "linear")

# One-way mode (not hedge mode). positionIdx 0 is the one-way position.
position_idx = envInt("POSITION_IDX", 0)

# Prefix for orderLinkId, used for idempotency. Bybit allows 36 chars of
# letters, digits, dashes and underscores.
order_link_prefix = envStr("ORDER_LINK_PREFIX", "cf")

# How far back to ask Bybit for closed positions when reporting fills.
closed_lookback_minutes = envInt("CLOSED_LOOKBACK_MINUTES", 15)

# Optional file used to remember which closes were already announced, so a
# close is not pushed to your phone once per run for as long as it sits inside
# the lookback window. Best effort - if it is unavailable the bot still works,
# it just gets chatty. Set to "" to disable.
notified_state_file = envStr("NOTIFIED_STATE_FILE", "state/notified.json")

# Which strategy opened which position. Also best effort, and deliberately
# NOT authoritative about whether a position exists - the exchange remains the
# only source of truth for that. This file answers a different question: not
# "do we hold BTC" but "which rule should decide when to let it go". Lose it
# and UNKNOWN_OWNER_EXIT decides instead. Set to "" to disable.
owners_state_file = envStr("OWNERS_STATE_FILE", "state/owners.json")

# Fail loudly rather than silently doing nothing.
request_timeout_seconds = envInt("REQUEST_TIMEOUT_SECONDS", 30)

# Seconds `run.py --kill-all` gives another copy of the bot to shut down
# politely before it is killed outright. A cycle mid-flight is finishing an
# HTTP call to Bybit, so a few seconds is worth waiting: a forced kill during
# an order round-trip is the one moment the local view and the exchange can
# disagree. Nothing is lost if it does happen - the next run asks Bybit what
# it holds - but the polite path is cheaper.
kill_grace_seconds = envInt("KILL_GRACE_SECONDS", 5)

# Environment variables that used to mean something and no longer do. Warned
# about in validate(), because a stale key in .env that is silently ignored is
# the kind of thing that costs an afternoon.
retired_settings = {
    "SMA_FAST_PERIOD": "replaced by EMA_FAST_PERIOD",
    "SMA_SLOW_PERIOD": "replaced by EMA_SLOW_PERIOD (REGIME_PERIOD is the slow line now)",
}


def envDiagnosis():
    """Explain where settings came from, or why they did not arrive."""
    if dotenv_missing:
        return (
            "found %s but python-dotenv is not installed in THIS interpreter, so "
            "the file was ignored. Almost always a virtualenv that is not active. "
            "Activate it (.venv\\Scripts\\Activate.ps1 on Windows, "
            "source .venv/bin/activate elsewhere) and run again." % env_path.name
        )
    if dotenv_loaded:
        return "loaded settings from %s" % env_path
    if env_path.exists():
        return "%s exists but was not loaded" % env_path
    return (
        "no .env file at %s - copy .env.example to .env and fill it in" % env_path
    )


def warnings():
    """Non-fatal complaints: settings that are legal but probably not meant."""
    notes = []
    for name, replacement in sorted(retired_settings.items()):
        if os.environ.get(name):
            notes.append("%s is set but no longer used - %s" % (name, replacement))
    if breakout_exit_lookback > breakout_lookback:
        notes.append(
            "BREAKOUT_EXIT_LOOKBACK (%d) is longer than BREAKOUT_LOOKBACK (%d), so the "
            "exit channel is wider than the entry channel and the trade gives back most "
            "of the move before closing. The Turtle rule is the other way round."
            % (breakout_exit_lookback, breakout_lookback)
        )
    if strategy == "multi" and min_entry_votes > 1 and regime_filter:
        notes.append(
            "MIN_ENTRY_VOTES=%d demands that %d strategies fire on the same bar, which is "
            "rare. Expect very few trades." % (min_entry_votes, min_entry_votes)
        )
    if not regime_filter and strategy == "multi" and len(active_strategies) > 1:
        notes.append(
            "REGIME_FILTER is off while several strategies run together. Mean reversion "
            "buys weakness and trend following buys strength, so without the filter they "
            "will take opposite views of the same market."
        )
    return notes


def validate():
    """Return a list of human-readable configuration problems. Non-empty means
    the run is refused."""
    problems = []
    known = ("trend", "meanrev", "breakout")

    if not bybit_api_key or not bybit_api_secret:
        problems.append("BYBIT_API_KEY / BYBIT_API_SECRET are not set")
        problems.append(envDiagnosis())

    if strategy not in known + ("multi",):
        problems.append(
            "STRATEGY must be one of 'trend', 'meanrev', 'breakout', 'multi', got %r" % strategy
        )

    if strategy == "multi":
        unknown = [name for name in active_strategies if name not in known]
        if unknown:
            problems.append(
                "ACTIVE_STRATEGIES contains unknown %s - valid names are %s"
                % (", ".join(repr(name) for name in unknown), ", ".join(known))
            )
        live = [name for name in active_strategies if name in known]
        if not live:
            problems.append("STRATEGY=multi but ACTIVE_STRATEGIES lists no valid strategy")
        elif min_entry_votes > len(live):
            problems.append(
                "MIN_ENTRY_VOTES (%d) is higher than the %d active strateg(ies), so no "
                "entry can ever fire" % (min_entry_votes, len(live))
            )

    if min_entry_votes < 1:
        problems.append("MIN_ENTRY_VOTES must be >= 1")
    if unknown_owner_exit not in ("any", "all", "regime"):
        problems.append(
            "UNKNOWN_OWNER_EXIT must be 'any', 'all' or 'regime', got %r" % unknown_owner_exit
        )
    if risk_model not in ("atr", "pct"):
        problems.append("RISK_MODEL must be 'atr' or 'pct', got %r" % risk_model)

    if position_notional_usdt <= 0:
        problems.append("POSITION_NOTIONAL_USDT must be > 0")
    if leverage < 1:
        problems.append("LEVERAGE must be >= 1")
    if max_open_positions < 0:
        problems.append("MAX_OPEN_POSITIONS must be >= 0 (0 means no cap)")
    if loop_interval_minutes < 1:
        problems.append("LOOP_INTERVAL_MINUTES must be >= 1")
    if order_bucket_seconds < 1:
        problems.append("ORDER_BUCKET_SECONDS must be >= 1")

    if ema_fast_period >= ema_slow_period:
        problems.append("EMA_FAST_PERIOD must be smaller than EMA_SLOW_PERIOD")
    if regime_filter and regime_period < 2:
        problems.append("REGIME_PERIOD must be >= 2 while REGIME_FILTER is on")
    if rsi_oversold >= rsi_overbought:
        problems.append("RSI_OVERSOLD must be below RSI_OVERBOUGHT")
    if rsi_period < 2:
        problems.append("RSI_PERIOD must be >= 2")
    if adx_min < 0:
        problems.append("ADX_MIN must be >= 0 (0 disables the filter)")
    if signal_lookback_bars < 1:
        problems.append("SIGNAL_LOOKBACK_BARS must be >= 1")

    # A trailing stop whose activation sits below its own distance arms BELOW
    # the entry price, turning profit protection into an early loss. Zero
    # activation is a deliberate "arm immediately" and is left alone. The same
    # arithmetic applies in both risk models.
    if trailing_stop_pct > 0 and 0 < trailing_activation_pct < trailing_stop_pct:
        problems.append(
            "TRAILING_ACTIVATION_PCT (%.4g) is smaller than TRAILING_STOP_PCT "
            "(%.4g), so the trailing stop would arm %.2f%% BELOW your entry and "
            "close at a loss before the stop loss ever triggers. Raise the "
            "activation to at least the distance."
            % (
                trailing_activation_pct,
                trailing_stop_pct,
                (trailing_stop_pct - trailing_activation_pct) * 100,
            )
        )
    if atr_trail_mult > 0 and 0 < atr_trail_activation_mult < atr_trail_mult:
        problems.append(
            "ATR_TRAIL_ACTIVATION_MULT (%.4g) is smaller than ATR_TRAIL_MULT (%.4g), so "
            "the trailing stop would arm %.4g ATR BELOW your entry and close at a loss "
            "before the stop loss ever triggers. Raise the activation to at least the "
            "distance."
            % (atr_trail_activation_mult, atr_trail_mult,
               atr_trail_mult - atr_trail_activation_mult)
        )
    if risk_model == "atr" and atr_stop_mult > 0 and atr_target_mult > 0 \
            and atr_target_mult <= atr_stop_mult:
        problems.append(
            "ATR_TARGET_MULT (%.4g) is not above ATR_STOP_MULT (%.4g), so every trade "
            "risks more than it can win." % (atr_target_mult, atr_stop_mult)
        )

    return problems
