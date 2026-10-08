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


def envMap(name, default, cast=str):
    """Parse "key:value,key:value" into a dict.

    Used where a setting is per-strategy rather than global - a timeframe or a
    position cap that differs between a scalper and a swing rule. Anything
    unparseable is skipped rather than crashing the bot at import time, and
    validate() reports it instead.
    """
    value = os.environ.get(name)
    if value in (None, ""):
        return dict(default)
    parsed = {}
    for item in value.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        key, _, raw = item.partition(":")
        key, raw = key.strip(), raw.strip()
        if not key or not raw:
            continue
        try:
            parsed[key] = cast(raw)
        except (TypeError, ValueError):
            continue
    return parsed


def parseKillZones(text):
    """"HH:MM-HH:MM,HH:MM-HH:MM" as [(start, end)] in minutes of the day, or
    None for "off". A window whose end is earlier than its start runs past
    midnight. Raises ValueError on anything else; validate() reports it."""
    if (text or "").strip().lower() == "off":
        return None
    windows = []
    for item in text.split(","):
        start, _, end = item.strip().partition("-")
        minutes = []
        for clock in (start, end):
            hours, _, mins = clock.strip().partition(":")
            if not (hours.isdigit() and mins.isdigit()) or int(hours) > 23 or int(mins) > 59:
                raise ValueError("%r is not HH:MM-HH:MM" % item.strip())
            minutes.append(int(hours) * 60 + int(mins))
        if minutes[0] == minutes[1]:
            raise ValueError("%r is an empty window" % item.strip())
        windows.append(tuple(minutes))
    return windows


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

# How much a cycle prints. false: one line per cycle plus whatever opened,
# closed or went wrong. true: every symbol's decision and why, for debugging a
# strategy. Errors and warnings print either way.
log_detail = envBool("LOG_DETAIL", False)

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
#
# The ten crypto perpetuals with the highest 24h turnover on Bybit, measured
# 2026-09-24. Tokenized stocks and commodities in that ranking (SOXL, CL, XAU)
# were skipped: they are not crypto and follow a different clock. On 15-minute
# candles ten symbols gave about 25 entries a day in a replay, and the most
# liquid markets are the ones where a market order fills near the price the
# stop was measured from.
symbols = envList(
    "SYMBOLS",
    [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "XRP/USDT:USDT",
        "SOL/USDT:USDT",
        "ZEC/USDT:USDT",
        "NEAR/USDT:USDT",
        "HYPE/USDT:USDT",
        "DOGE/USDT:USDT",
        "1000PEPE/USDT:USDT",
        "BCH/USDT:USDT",
    ],
)

# Every strategy the bot has, in the order the documentation lists them. A
# name outside this list is refused by validate().
strategy_names = ("ict", "breakout")

# One of strategy_names, or "multi" to run several at once.
strategy = envStr("STRATEGY", "multi")

# Which strategies are live when STRATEGY=multi, in PRIORITY order. When more
# than one fires on the same bar the first listed owns the position, and its
# exit rule is what will close it. Ignored unless STRATEGY=multi.
#
# breakout alone. In the 25.8-day replay it was the one strategy that paid:
# +0.16% a trade over 313 trades, while meanrev and scalp lost money and
# closed two trades in three on their own exit rule, a small loss plus fees.
active_strategies = envList("ACTIVE_STRATEGIES", ["breakout"])

# Which of the live strategies may also trade short, as a comma list. Empty
# means long only. A short is the exact mirror of the strategy's long rule,
# read on the chart turned upside down, with no separate settings: it enters
# only below the regime line, where the long side may not. A side that fails
# the replay is left out of this list without touching its long side.
short_strategies = envList("SHORT_STRATEGIES", [])

# Per-strategy timeframe override, as "name:timeframe,name:timeframe".
# Anything not listed runs on ENTRY_TIMEFRAME.
#
# Empty by default: every strategy trades the same 15-minute clock. The
# override is what lets one bot hold a swing book and a scalping book at the
# same time - "breakout:1h" would put breakout on hourly candles while the
# rest stay fast.
strategy_timeframes = envMap("STRATEGY_TIMEFRAMES", {})

# Per-strategy ceiling on open positions, as "name:count". A strategy not
# listed is limited only by MAX_OPEN_POSITIONS.
#
# Empty by default, because it only matters when strategies run on different
# clocks. A 15-minute rule fires many times more often than an hourly one, so
# with mixed timeframes it reaches every free slot first and the slow rules
# never get to trade; budgeting them separately fixes that. With every rule on
# the same clock there is nothing to protect, and a cap only turns signals
# away.
max_open_per_strategy = envMap("MAX_OPEN_PER_STRATEGY", {}, int)

# How many active strategies must agree before a position opens. 1 is "any
# signal trades" and produces the most trades; raising it demands confluence
# and produces far fewer, better-supported ones.
min_entry_votes = envInt("MIN_ENTRY_VOTES", 1)

# Hard ceiling on simultaneous open positions across all symbols, so a market
# where everything breaks out at once cannot put the whole account to work in
# one direction. Symbols are considered in SYMBOLS order. 0 disables the cap.
max_open_positions = envInt("MAX_OPEN_POSITIONS", 10)

# After a position on a symbol closes - by the strategy, a stop, a target,
# anything - no new entry on that symbol for this many candles of the
# strategy's timeframe. 0 disables it.
#
# Entries look for a signal within the last SIGNAL_LOOKBACK_BARS candles, so
# one signal stays valid for several cycles. Without a cooldown a position
# stopped out on the first cycle is simply bought again on the next, on the
# same signal. Measured live on ARB: four entries on one trend signal inside
# nine minutes, the first stopped out after three minutes and the other three
# within ten seconds of the fill. Matching the lookback window means each
# signal is traded once.
#
# The close times come from Bybit's closed-position records, not a local
# file, so a restart or a second copy of the bot cannot lose them.
reentry_cooldown_bars = envInt("REENTRY_COOLDOWN_BARS", 3)


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

# Timeframe the entry signal is evaluated on. 15-minute candles: positions last
# about three hours on average in a replay, and ten symbols give roughly 25
# entries a day. The regime line (REGIME_PERIOD=200) is about two days on this
# clock, and it is a condition checked every cycle, not a crossing to wait for.
entry_timeframe = envStr("ENTRY_TIMEFRAME", "15m")

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

# Long entries only while price is above this average, short entries (see
# SHORT_STRATEGIES) only while it is below, so every strategy trades with the
# slow trend rather than against it, and a long and a short never compete for
# one symbol.
regime_filter = envBool("REGIME_FILTER", True)
regime_period = envInt("REGIME_PERIOD", 200)

# Close an open position, whichever strategy opened it, when price falls back
# under the regime average - except one opened by a strategy that closes at its
# exchange-side stop and target only (ict; see signals.strategies).
#
# Off by default. Replayed on 15-minute candles over 26 days on ten symbols,
# turning it off improved results in both halves of the sample: a dip inside
# an uptrend is exactly what sags toward that line, so this exit mostly
# closed trades that were about to work. Every position already carries an
# exchange-side stop loss, which is the real protection.
exit_on_regime_break = envBool("EXIT_ON_REGIME_BREAK", False)

# What to do about a position whose owning strategy is unknown - opened by
# hand, the owner file was lost, or its strategy is no longer active. "any"
# closes as soon as any active strategy wants out, "all" waits for unanimity,
# "regime" leaves it to the regime break and the exchange-side stops.
#
# "regime": at any moment one of several exit rules usually says close, so
# "any" closed an orphaned position within a cycle and paid the fees for
# nothing. Its stop and target are already on the exchange.
unknown_owner_exit = envStr("UNKNOWN_OWNER_EXIT", "regime")

# ---------------------------------------------------------------------------
# breakout - Donchian channel, Turtle style
# ---------------------------------------------------------------------------

# Enter on a close above the N-bar high, leave on a close below the M-bar low,
# M shorter than N. The asymmetry is the original Turtle rule: a symmetric
# channel gives back most of a move before admitting the trend is over.
# An exit lookback of 0 means no rule exit: the exchange-side stop and target
# alone close the trade.
breakout_lookback = envInt("BREAKOUT_LOOKBACK", 20)
breakout_exit_lookback = envInt("BREAKOUT_EXIT_LOOKBACK", 10)

# ---------------------------------------------------------------------------
# ict - sweep, structure shift, fair-value gap retest
#
# The trader agent's rules (issue #15, "Strategy rules"). Every count is in
# candles of the strategy's timeframe, every size in ATR on that timeframe.
# ---------------------------------------------------------------------------

# A swing high is a high strictly above this many bars on each side, a swing
# low the mirror. It is known only once the bars after it have closed.
ict_swing_bars = envInt("ICT_SWING_BARS", 2)

# How far back a swing low counts as liquidity worth sweeping: 96 bars is one
# day of 15-minute candles. The previous UTC day's low counts as well.
ict_liquidity_lookback_bars = envInt("ICT_LIQUIDITY_LOOKBACK_BARS", 96)

# Bars after the sweeping candle within which a close must come back above the
# swept level. 0 means the sweeping candle itself must close back above it.
ict_sweep_reclaim_bars = envInt("ICT_SWEEP_RECLAIM_BARS", 1)

# Bars after the sweep within which a close must break the last swing high,
# the structure shift. 8 bars is two hours on 15-minute candles.
ict_mss_max_bars = envInt("ICT_MSS_MAX_BARS", 8)

# The gap's middle candle must close up with a body of at least this many ATR:
# the move that made the gap was displacement, not drift.
ict_displacement_min_atr = envFloat("ICT_DISPLACEMENT_MIN_ATR", 1.0)

# The smallest gap worth trading, in ATR. A one-tick gap is smaller than the fee.
ict_fvg_min_atr = envFloat("ICT_FVG_MIN_ATR", 0.1)

# The retest must come within this many bars of the gap's third candle.
ict_fvg_max_age_bars = envInt("ICT_FVG_MAX_AGE_BARS", 12)

# The retest candle must close at or above bottom + this fraction of the gap:
# 0.5 is the gap's midpoint.
ict_entry_close_min = envFloat("ICT_ENTRY_CLOSE_MIN", 0.5)

# The live price may sit at most this many ATR above the gap's top, and must
# sit above its bottom. Further away the market order would not fill at the gap.
ict_max_chase_atr = envFloat("ICT_MAX_CHASE_ATR", 0.5)

# Hours the retest candle must open in, as "HH:MM-HH:MM,..." on the clock of
# ICT_KILL_ZONE_TZ, or "off". Off by default: a session filter has to earn its
# place in the replay before it removes trades. Needs the tzdata package on
# Windows, which has no time zone database of its own.
ict_kill_zones = envStr("ICT_KILL_ZONES", "off")
ict_kill_zone_tz = envStr("ICT_KILL_ZONE_TZ", "America/New_York")

# What the stop hides under: "candle1" is the gap's first candle's low,
# "leglow" the lowest low of the move from the sweep to the structure shift.
# Either way the stop also stays under every low from the gap's third candle
# to the retest.
ict_stop_ref = envStr("ICT_STOP_REF", "candle1")

# The stop sits this many ATR under that reference...
ict_stop_buffer_atr = envFloat("ICT_STOP_BUFFER_ATR", 0.1)

# ...and at least this many ATR under the live price. A structure stop tighter
# than that sits inside ordinary 15-minute noise, so it is widened, not refused.
ict_stop_floor_atr = envFloat("ICT_STOP_FLOOR_ATR", 1.5)

# When the liquidation cap pulls the stop above the structural stop, the stop
# no longer sits where the setup is wrong, and the trade is refused. true takes
# it with the capped stop instead.
ict_allow_capped_stop = envBool("ICT_ALLOW_CAPPED_STOP", False)

# The target is the nearest untouched buy-side level that existed before the
# sweep. Closer than this many times the risk, the trade is refused: fees would
# take most of the win. With no such level, the target is the fallback
# multiple of the risk.
ict_min_rr = envFloat("ICT_MIN_RR", 1.5)
ict_fallback_target_r = envFloat("ICT_FALLBACK_TARGET_R", 2.0)

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

# Stop 3 ATR, target 6 ATR. Two ATR is the textbook stop on daily candles; a
# 15-minute candle is mostly noise by comparison and needs more room. Replayed
# over 26 days on ten symbols, 3/6 beat 2/4 in both halves of the sample, with
# fewer trades stopped out by ordinary wiggle.
#
# No trail (0). A 1.5 ATR pullback is ordinary on 15-minute candles, so a
# trail armed at +3 ATR closed most winners between +1.5 and +3 ATR, short of
# the +6 ATR target that breakout's replay result came from. That replay never
# modelled the trail at all.
atr_period = envInt("ATR_PERIOD", 14)
atr_stop_mult = envFloat("ATR_STOP_MULT", 3.0)
atr_target_mult = envFloat("ATR_TARGET_MULT", 6.0)
atr_trail_mult = envFloat("ATR_TRAIL_MULT", 0.0)
atr_trail_activation_mult = envFloat("ATR_TRAIL_ACTIVATION_MULT", 3.0)

# HOW CLOSE THE STOP MAY GET TO THE LIQUIDATION PRICE.
#
# Leverage puts liquidation roughly 100/leverage percent from entry - 6.67% at
# 15x. A stop further away than that never fires: the exchange closes the
# position first, for the full margin, with a liquidation fee on top, and the
# risk management the strategy was built around simply stops existing.
#
# Measured live across the forty symbols traded at the time, 2xATR ranged from
# 1.24% on BTC to 27% on the wildest alt - a twentyfold spread. Even the ten
# liquid symbols traded now put a 15-minute 3xATR stop anywhere from 0.78%
# (BTC, median) to 11% (NEAR, worst spell). No single leverage covers that,
# so the stop is capped per trade instead: it may use at most this fraction
# of the distance to liquidation.
max_stop_fraction_of_liquidation = envFloat("MAX_STOP_FRACTION_OF_LIQUIDATION", 0.5)

# WHEN A CAPPED STOP IS TOO TIGHT TO BE WORTH TAKING.
#
# Capping protects the position from liquidation, but a stop squeezed inside
# normal bar-to-bar movement is not protection, it is a guaranteed exit. If
# the cap pulls the stop below this many ATR, the trade is skipped rather than
# taken with a stop the next candle will hit by accident. Set to 0 to take
# every trade regardless.
min_stop_atr_mult = envFloat("MIN_STOP_ATR_MULT", 1.0)

# Percentage model, and the fallback for the ATR model. Fractions of the entry
# price: 0.05 == 5%. Set any of them to 0 to disable that leg.
stop_loss_pct = envFloat("STOP_LOSS_PCT", 0.05)
take_profit_pct = envFloat("TAKE_PROFIT_PCT", 0.10)

# Trailing stop. Bybit's API takes a PRICE DISTANCE here, not a percentage
# (verified against the v5 docs: "Trailing stop by price distance"), so this
# fraction gets multiplied by the entry price before being sent. Off (0), for
# the same reason as ATR_TRAIL_MULT.
trailing_stop_pct = envFloat("TRAILING_STOP_PCT", 0.0)

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

# How far back to ask Bybit for closed positions when reporting fills. The
# read reaches further back on its own when the re-entry cooldown needs it
# (REENTRY_COOLDOWN_BARS times the slowest active timeframe).
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
    "SMA_FAST_PERIOD": "the trend strategy was removed",
    "SMA_SLOW_PERIOD": "the trend strategy was removed (REGIME_PERIOD is the slow line)",
    "RSI_PERIOD": "the meanrev strategy was removed",
    "RSI_OVERSOLD": "the meanrev strategy was removed",
    "RSI_OVERBOUGHT": "the meanrev strategy was removed",
    "MEANREV_EXIT_SMA_PERIOD": "the meanrev strategy was removed",
    "BB_PERIOD": "the scalp strategy was removed",
    "BB_STDEV": "the scalp strategy was removed",
    "BB_LOOKBACK_BARS": "the scalp strategy was removed",
    "PULLBACK_DISPLACEMENT_MIN_ATR": "the pullback strategy was removed",
    "EMA_FAST_PERIOD": "the trend strategy was removed",
    "EMA_SLOW_PERIOD": "the trend strategy was removed",
    "ADX_PERIOD": "the trend strategy was removed",
    "ADX_MIN": "the trend strategy was removed",
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
    if breakout_exit_lookback > 0 and breakout_exit_lookback > breakout_lookback:
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
    live = active_strategies if strategy == "multi" else [strategy]
    idle = [name for name in short_strategies if name in strategy_names and name not in live]
    if idle:
        notes.append(
            "SHORT_STRATEGIES names %s, which %s not live (see ACTIVE_STRATEGIES and "
            "STRATEGY), so %s neither side."
            % (", ".join(idle), "is" if len(idle) == 1 else "are",
               "it trades" if len(idle) == 1 else "they trade")
        )
    clocks = sorted({strategy_timeframes.get(name, entry_timeframe) for name in live})
    if len(clocks) > 1 and not max_open_per_strategy:
        notes.append(
            "STRATEGY_TIMEFRAMES puts the strategies on %s but MAX_OPEN_PER_STRATEGY is "
            "empty. The faster clock fires many times more often and will take every "
            "position slot before the slower rule reaches one; give each strategy a "
            "budget." % " and ".join(clocks)
        )
    if 0 < reentry_cooldown_bars < signal_lookback_bars:
        notes.append(
            "REENTRY_COOLDOWN_BARS (%d) is shorter than SIGNAL_LOOKBACK_BARS (%d), so a "
            "symbol is let back in while the signal behind its last trade is still live, "
            "and that signal is bought again. Match them to trade each signal once."
            % (reentry_cooldown_bars, signal_lookback_bars)
        )
    if ict_fallback_target_r < ict_min_rr:
        notes.append(
            "ICT_FALLBACK_TARGET_R (%g) is under ICT_MIN_RR (%g), so a level setup with no "
            "draw in its direction is taken at %gR while one whose nearest draw is closer than %gR is "
            "refused." % (ict_fallback_target_r, ict_min_rr, ict_fallback_target_r, ict_min_rr)
        )
    if ict_stop_floor_atr < min_stop_atr_mult:
        notes.append(
            "ICT_STOP_FLOOR_ATR (%g) is under MIN_STOP_ATR_MULT (%g), so a level setup's "
            "stop can sit %g ATR from the price, closer than the %g ATR a capped stop is "
            "refused under as noise."
            % (ict_stop_floor_atr, min_stop_atr_mult, ict_stop_floor_atr, min_stop_atr_mult)
        )
    if not regime_filter and strategy == "multi" and len(active_strategies) > 1:
        notes.append(
            "REGIME_FILTER is off while several strategies run together, so nothing keeps "
            "them trading the same direction as the slow trend."
        )
    return notes


def validate():
    """Return a list of human-readable configuration problems. Non-empty means
    the run is refused."""
    problems = []
    known = strategy_names

    if not bybit_api_key or not bybit_api_secret:
        problems.append("BYBIT_API_KEY / BYBIT_API_SECRET are not set")
        problems.append(envDiagnosis())

    if strategy not in known + ("multi",):
        problems.append(
            "STRATEGY must be one of %s or 'multi', got %r"
            % (", ".join(repr(name) for name in known), strategy)
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

    unknown = [name for name in short_strategies if name not in known]
    if unknown:
        problems.append(
            "SHORT_STRATEGIES contains unknown %s - valid names are %s"
            % (", ".join(repr(name) for name in unknown), ", ".join(known))
        )

    if min_entry_votes < 1:
        problems.append("MIN_ENTRY_VOTES must be >= 1")
    if unknown_owner_exit not in ("any", "all", "regime"):
        problems.append(
            "UNKNOWN_OWNER_EXIT must be 'any', 'all' or 'regime', got %r" % unknown_owner_exit
        )
    if risk_model not in ("atr", "pct"):
        problems.append("RISK_MODEL must be 'atr' or 'pct', got %r" % risk_model)

    if not 0 < max_stop_fraction_of_liquidation <= 1:
        problems.append(
            "MAX_STOP_FRACTION_OF_LIQUIDATION must be above 0 and at most 1, got %.4g"
            % max_stop_fraction_of_liquidation)
    if min_stop_atr_mult < 0:
        problems.append("MIN_STOP_ATR_MULT must be >= 0")
    if position_notional_usdt <= 0:
        problems.append("POSITION_NOTIONAL_USDT must be > 0")
    if leverage < 1:
        problems.append("LEVERAGE must be >= 1")
    if max_open_positions < 0:
        problems.append("MAX_OPEN_POSITIONS must be >= 0 (0 means no cap)")
    if reentry_cooldown_bars < 0:
        problems.append("REENTRY_COOLDOWN_BARS must be >= 0 (0 disables the cooldown)")
    if loop_interval_minutes < 1:
        problems.append("LOOP_INTERVAL_MINUTES must be >= 1")
    if order_bucket_seconds < 1:
        problems.append("ORDER_BUCKET_SECONDS must be >= 1")

    if regime_filter and regime_period < 2:
        problems.append("REGIME_PERIOD must be >= 2 while REGIME_FILTER is on")
    if signal_lookback_bars < 1:
        problems.append("SIGNAL_LOOKBACK_BARS must be >= 1")
    if breakout_lookback < 1:
        problems.append("BREAKOUT_LOOKBACK must be >= 1")
    if breakout_exit_lookback < 0:
        problems.append("BREAKOUT_EXIT_LOOKBACK must be >= 0 (0 means no rule exit)")
    problems.extend(ictProblems())
    for name in strategy_timeframes:
        if name not in known:
            problems.append(
                "STRATEGY_TIMEFRAMES names unknown strategy %r - valid names are %s"
                % (name, ", ".join(known)))
    for name, cap in max_open_per_strategy.items():
        if name not in known:
            problems.append(
                "MAX_OPEN_PER_STRATEGY names unknown strategy %r - valid names are %s"
                % (name, ", ".join(known)))
        elif cap < 0:
            problems.append("MAX_OPEN_PER_STRATEGY for %r must be >= 0" % name)

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


def ictProblems():
    """validate()'s checks for the ICT settings."""
    problems = []
    at_least = [("ICT_SWING_BARS", ict_swing_bars, 1),
                ("ICT_LIQUIDITY_LOOKBACK_BARS", ict_liquidity_lookback_bars, 1),
                ("ICT_SWEEP_RECLAIM_BARS", ict_sweep_reclaim_bars, 0),
                ("ICT_MSS_MAX_BARS", ict_mss_max_bars, 0),
                ("ICT_FVG_MAX_AGE_BARS", ict_fvg_max_age_bars, 1),
                ("ICT_DISPLACEMENT_MIN_ATR", ict_displacement_min_atr, 0),
                ("ICT_FVG_MIN_ATR", ict_fvg_min_atr, 0),
                ("ICT_MAX_CHASE_ATR", ict_max_chase_atr, 0),
                ("ICT_STOP_BUFFER_ATR", ict_stop_buffer_atr, 0),
                ("ICT_STOP_FLOOR_ATR", ict_stop_floor_atr, 0)]
    for name, value, floor in at_least:
        if value < floor:
            problems.append("%s must be >= %s, got %s" % (name, floor, value))
    if not 0 <= ict_entry_close_min <= 1:
        problems.append("ICT_ENTRY_CLOSE_MIN must be between 0 and 1, got %s"
                        % ict_entry_close_min)
    if ict_min_rr <= 0:
        problems.append("ICT_MIN_RR must be > 0, got %s" % ict_min_rr)
    if ict_fallback_target_r <= 0:
        problems.append("ICT_FALLBACK_TARGET_R must be > 0, got %s" % ict_fallback_target_r)
    if ict_stop_ref not in ("candle1", "leglow"):
        problems.append("ICT_STOP_REF must be 'candle1' or 'leglow', got %r" % ict_stop_ref)

    try:
        zones = parseKillZones(ict_kill_zones)
    except ValueError as error:
        problems.append("ICT_KILL_ZONES must be 'off' or HH:MM-HH:MM,... - %s" % error)
        zones = None
    if zones:
        # Only with kill zones on: the time zone database is not needed
        # otherwise, and Windows has none without the tzdata package.
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(ict_kill_zone_tz)
        except Exception as error:
            problems.append(
                "ICT_KILL_ZONE_TZ %r cannot be loaded (%s). On Windows the time zone "
                "database comes from the tzdata package: pip install -r requirements.txt"
                % (ict_kill_zone_tz, error))
    return problems
