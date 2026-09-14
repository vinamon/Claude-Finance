"""
Central configuration.

Secrets (API keys, ntfy topic) come from environment variables only, which in
GitHub Actions are fed from repository Secrets. Nothing sensitive is ever
hardcoded here, because this repository is public.

Every tuning knob below can also be overridden by an environment variable of
the same (upper-case) name, so you can change symbols or risk settings from
GitHub repository Variables without touching the code.
"""

import os

# Load a local .env file if one exists, so running on a laptop does not mean
# exporting variables by hand every time you open a terminal. Values already
# present in the real environment win, which keeps GitHub Actions unaffected.
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:
    # python-dotenv is optional: without it, plain environment variables still
    # work exactly as before.
    pass

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

# dummy_mode ignores the market entirely and only enters when the workflow was
# started by hand (workflow_dispatch). Defaults to True on purpose: live
# signal trading has to be switched on deliberately, never by forgetting.
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

# Loop by default, or run once and exit. This is the autostart switch.
autostart = envBool("AUTOSTART", False)

# Minutes between cycles in loop mode.
loop_interval_minutes = envInt("LOOP_INTERVAL_MINUTES", 5)

# ---------------------------------------------------------------------------
# TODO(you): fill these in. Deliberately left empty / placeholder - these are
# the numbers that decide whether you make or lose (virtual) money, and
# guessing them for you would be worse than useless.
# ---------------------------------------------------------------------------

# TODO(you): which markets to trade. Use ccxt unified symbols for Bybit USDT
# perpetuals, e.g. "BTC/USDT:USDT", "ETH/USDT:USDT".
# Can also be set as a comma-separated SYMBOLS repository variable.
# Example: symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
symbols = envList("SYMBOLS", [])

# TODO(you): which strategy is live. One of: "trend", "meanrev", "breakout".
# See signals.py - all three are textbook starting points, not edges.
strategy = envStr("STRATEGY", "trend")

# TODO(you): notional value of each position, in USDT. This is qty * price,
# NOT the margin you put up. Margin used is roughly notional / leverage, so at
# notional 500 and leverage 5 you are risking 100 USDT of margin.
position_notional_usdt = envFloat("POSITION_NOTIONAL_USDT", 100.0)

# TODO(you): leverage. 1 means no leverage. Higher leverage does not change
# your notional above, it changes how little margin backs it, i.e. how close
# liquidation sits.
leverage = envInt("LEVERAGE", 1)

# TODO(you): exit distances, as a fraction of the entry price.
# 0.02 == 2%. Set any of them to 0 to disable that leg.
stop_loss_pct = envFloat("STOP_LOSS_PCT", 0.02)
take_profit_pct = envFloat("TAKE_PROFIT_PCT", 0.04)

# Trailing stop. Bybit's API takes a PRICE DISTANCE here, not a percentage
# (verified against the v5 docs: "Trailing stop by price distance"), so this
# fraction gets multiplied by the entry price before being sent.
trailing_stop_pct = envFloat("TRAILING_STOP_PCT", 0.015)

# Trailing stop activation. The trailing stop stays dormant until price
# reaches entry * (1 + this). Set to 0 to arm the trailing stop immediately.
trailing_activation_pct = envFloat("TRAILING_ACTIVATION_PCT", 0.01)

# ---------------------------------------------------------------------------
# strategy parameters - see signals.py for what each one does
# ---------------------------------------------------------------------------

# Timeframe the entry signal is evaluated on.
entry_timeframe = envStr("ENTRY_TIMEFRAME", "1h")

# Timeframe the exit signal is evaluated on. The plan asks for 1m here; be
# aware the bot only wakes up every ~5 minutes (often less often, see README),
# so a 1m exit is checked in bursts, not bar by bar. The exchange-side
# stopLoss / takeProfit / trailingStop are what actually protect you between
# runs.
exit_timeframe = envStr("EXIT_TIMEFRAME", "1m")

# How many recently closed candles to scan for a signal event. A cron-driven
# bot misses bars, so looking only at the newest bar would throw away most
# crossings. 3 means "did this fire on any of the last 3 closed candles".
signal_lookback_bars = envInt("SIGNAL_LOOKBACK_BARS", 3)

# trend: simple moving average crossover ("golden cross")
sma_fast_period = envInt("SMA_FAST_PERIOD", 50)
sma_slow_period = envInt("SMA_SLOW_PERIOD", 200)

# meanrev: RSI leaving oversold
rsi_period = envInt("RSI_PERIOD", 14)
rsi_oversold = envFloat("RSI_OVERSOLD", 30.0)
rsi_overbought = envFloat("RSI_OVERBOUGHT", 70.0)

# breakout: Donchian channel breakout
breakout_lookback = envInt("BREAKOUT_LOOKBACK", 20)

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
# the lookback window. In GitHub Actions this is restored/saved by
# actions/cache. If the file is unavailable the bot still works, it just gets
# chatty. Set to "" to disable.
notified_state_file = envStr("NOTIFIED_STATE_FILE", "state/notified.json")

# Fail loudly rather than silently doing nothing.
request_timeout_seconds = envInt("REQUEST_TIMEOUT_SECONDS", 30)


def validate():
    """Return a list of human-readable configuration problems."""
    problems = []
    if not bybit_api_key or not bybit_api_secret:
        problems.append("BYBIT_API_KEY / BYBIT_API_SECRET are not set")
    if strategy not in ("trend", "meanrev", "breakout"):
        problems.append(
            "STRATEGY must be one of 'trend', 'meanrev', 'breakout', got %r" % strategy
        )
    if position_notional_usdt <= 0:
        problems.append("POSITION_NOTIONAL_USDT must be > 0")
    if leverage < 1:
        problems.append("LEVERAGE must be >= 1")
    if sma_fast_period >= sma_slow_period:
        problems.append("SMA_FAST_PERIOD must be smaller than SMA_SLOW_PERIOD")
    return problems
