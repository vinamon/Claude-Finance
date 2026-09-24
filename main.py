"""
Entry point. One pass over every configured symbol, then exit.

Shape of a run:
  1. validate config, build a demo-pinned Bybit client
  2. read Bybit's closed-position records once, and report the new ones
     (SL/TP/trailing/liquidation fills that happened while nothing was
     running)
  3. read what is actually held, in ONE request, before deciding anything
  4. for each symbol holding a position: check the owning strategy exit rule
  5. for each symbol holding nothing: collect every active strategy vote
  6. log every decision and why, then exit 0, or exit 1 on a real failure

Exiting non-zero matters: a red run is the cheapest monitoring you will get.

WHY POSITIONS ARE READ IN A SEPARATE PASS FIRST
-----------------------------------------------
MAX_OPEN_POSITIONS and MAX_OPEN_PER_STRATEGY cap how much of the account can
be at work at once. To honour either, the run has to know what is already held
BEFORE it decides whether symbol number three may open something - otherwise
it counts only the symbols it happens to have visited and walks through the
cap. The counts are then kept current as positions open and close within the
run.

Bybit answers "what do I hold" for every linear position in a single request:
one call for the whole symbol list instead of one per symbol. The per-symbol
path is kept as a fallback because a silent failure here would make the bot
think it is flat and open duplicates.

ONE OR SEVERAL TIMEFRAMES IN ONE CYCLE
--------------------------------------
signals.requiredTimeframes() says which timeframes the active set needs and
how much history each wants, and this module fetches exactly those - one
request per distinct timeframe per symbol, not one per strategy. By default
every strategy reads 15-minute candles, so that is one request per symbol;
STRATEGY_TIMEFRAMES can put a strategy on its own clock in the same pass.

SCHEDULING REALITY
------------------
Nothing here may assume even spacing between runs, or that the previous run
happened at all - which is why position state lives on the exchange and
signals scan a window of bars rather than only the newest one.
"""

import json
import os
import sys
import time
import traceback

import config
import executor
import notify
import signals
from exchange import buildExchange, clockReport

started_at = time.time()


def log(message):
    elapsed = time.time() - started_at
    print("[%7.2fs] %s" % (elapsed, message), flush=True)


# ---------------------------------------------------------------------------
# small json state files
#
# Both are best effort and neither is authoritative about whether a position
# exists - the exchange remains the only source of truth for that. They answer
# softer questions: "have I already told the phone about this close" and
# "which rule opened this position". Losing either degrades the logs, never
# the trading.
# ---------------------------------------------------------------------------


def loadState(path, default):
    if not path:
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except Exception as error:
        log("could not read %s (%s), treating as empty" % (path, error))
        return default


def saveState(path, data):
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
    except Exception as error:
        log("could not write %s (%s), continuing" % (path, error))


def loadNotified():
    data = loadState(config.notified_state_file, {})
    return set(data.get("ids", []))


def saveNotified(ids):
    # keep the file from growing forever
    saveState(config.notified_state_file, {"ids": sorted(ids)[-500:]})


def loadOwners():
    """symbol -> strategy name that opened the position currently held."""
    data = loadState(config.owners_state_file, {})
    return data if isinstance(data, dict) else {}


def saveOwners(owners):
    saveState(config.owners_state_file, owners)


# ---------------------------------------------------------------------------
# closed-position reporting
# ---------------------------------------------------------------------------


def fetchClosedPositions(client):
    """Bybit's recent closed-position records, or None if the read failed.

    Kept apart from the report so the rows are read once per cycle. A failed
    read is logged here and is never fatal - reporting a close is worth less
    than running the cycle.
    """
    start_time = int(time.time() * 1000) - config.closed_lookback_minutes * 60 * 1000
    try:
        response = client.privateGetV5PositionClosedPnl(
            {
                "category": config.category,
                "startTime": start_time,
                "limit": 100,
            }
        )
    except Exception as error:
        log("could not fetch closed positions: %s" % error)
        return None
    return ((response or {}).get("result") or {}).get("list") or []


def reportClosedPositions(client, rows, notified):
    """Announce positions Bybit closed on its own since we last looked."""
    log("closed-position check: %d record(s) in the last %d minute(s)"
        % (len(rows), config.closed_lookback_minutes))

    for row in rows:
        key = row.get("orderId") or "%s-%s" % (row.get("symbol"), row.get("updatedTime"))
        if key in notified:
            continue
        record = {
            "symbol": row.get("symbol"),
            "qty": row.get("qty"),
            "avg_entry": row.get("avgEntryPrice"),
            "avg_exit": row.get("avgExitPrice"),
            "closed_pnl": toFloat(row.get("closedPnl")),
            # Looked up only here, after the notified check, so a close costs
            # one order-history request the first time it is seen and none on
            # every later cycle that still finds it inside the window.
            "cause": closeCause(client, row),
        }
        log(
            "position closed: %s qty=%s entry=%s exit=%s pnl=%s why=%s"
            % (
                record["symbol"],
                record["qty"],
                record["avg_entry"],
                record["avg_exit"],
                record["closed_pnl"],
                record["cause"],
            )
        )
        notify.positionClosed(record)
        notified.add(key)

    return notified


# Bybit's stopOrderType on the order that closed a position, for the three
# kinds of exit the bot attaches to every entry.
stop_causes = {
    "StopLoss": "stop loss",
    "TakeProfit": "take profit",
    "TrailingStop": "trailing stop",
}


def closeCause(client, row):
    """Why a position closed, in words, from one closed-pnl row.

    The closed-pnl row itself only says "a position closed at this price".
    The order that closed it, looked up by its id in the order history, says
    who sent it. Never raises: a close that cannot be explained is still a
    close worth reporting, so a failed lookup is "unknown", not a lost push.
    """
    # The liquidation engine's fill is marked on the close record itself, so
    # the worst outcome is named even when the lookup below would fail.
    if row.get("execType") == "BustTrade":
        return "liquidation"

    symbol = row.get("symbol")
    order_id = row.get("orderId")
    try:
        response = client.privateGetV5OrderHistory(
            {"category": config.category, "orderId": order_id})
    except Exception as error:
        log("could not look up why %s closed (order %s): %s" % (symbol, order_id, error))
        return "unknown"
    orders = ((response or {}).get("result") or {}).get("list") or []
    if not orders:
        log("could not look up why %s closed: order %s is not in Bybit's order history"
            % (symbol, order_id))
        return "unknown"
    order = orders[0]

    stop_type = order.get("stopOrderType")
    if stop_type in stop_causes:
        return stop_causes[stop_type]
    # Every order the bot sends carries its prefix in orderLinkId, and the
    # only closing orders it sends are its own exits.
    if (order.get("orderLinkId") or "").startswith(config.order_link_prefix + "-"):
        return "bot exit"
    # A hand-made order, a close from Bybit's interface, anything else.
    # createType is how Bybit itself says where the order came from.
    return "closed outside the bot (%s)" % order.get("createType")


def toFloat(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# reading what is held
# ---------------------------------------------------------------------------


def readPositions(client, symbols):
    """symbol -> position dict, or None. One request for the whole account.

    Bybit returns every linear position for a settle coin in one response, so
    the whole symbol list costs one call, not one per symbol. The per-symbol
    loop is kept as a fallback and is genuinely needed: if this silently
    returned nothing on error, the bot would believe it is flat everywhere
    and open a second position on top of every one it already holds.
    """
    held = dict.fromkeys(symbols)
    try:
        rows = client.fetch_positions(
            None, params={"category": config.category, "settleCoin": "USDT"})
    except Exception as error:
        log("batch position read failed (%s), falling back to one call per symbol" % error)
        for symbol in symbols:
            held[symbol] = executor.openPosition(client, symbol)
        return held

    wanted = set(symbols)
    for row in rows:
        symbol = row.get("symbol")
        if symbol not in wanted:
            continue
        if executor.positionSize(row) > 0:
            held[symbol] = row
    return held


def openCounts(held, owners):
    """How many positions each strategy currently holds."""
    counts = {}
    for symbol, position in held.items():
        if position is None:
            continue
        owner = owners.get(symbol)
        if owner:
            counts[owner] = counts.get(owner, 0) + 1
    return counts


def mayOpen(strategy, open_total, counts):
    """(allowed, reason) for one strategy wanting one more position."""
    if config.max_open_positions and open_total >= config.max_open_positions:
        return False, ("MAX_OPEN_POSITIONS=%d reached (%d open)"
                       % (config.max_open_positions, open_total))
    cap = config.max_open_per_strategy.get(strategy)
    if cap is not None and counts.get(strategy, 0) >= cap:
        return False, ("MAX_OPEN_PER_STRATEGY for %s is %d and %d are already open"
                       % (strategy, cap, counts.get(strategy, 0)))
    return True, ""


# ---------------------------------------------------------------------------
# per-symbol work
# ---------------------------------------------------------------------------


def fetchCandles(client, symbol, wanted):
    """Fetch each requested timeframe once. wanted maps timeframe -> limit."""
    candles = {}
    for timeframe, limit in wanted.items():
        candles[timeframe] = client.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
            params={"category": config.category},
        )
    return candles


def handleHeld(client, symbol, position, owners):
    """A symbol we are holding: ask the owning strategy whether to let go."""
    size = executor.positionSize(position)
    owner = owners.get(symbol)
    log("%s: holding %s contracts, opened by %s, checking its exit rule"
        % (symbol, size, owner or "an unknown rule"))

    candles = fetchCandles(client, symbol, signals.exitTimeframes(owner))
    decision = signals.exitSignal(symbol, candles, owner)

    if decision.action == signals.close:
        result = executor.closePosition(client, symbol, position, decision.reason, log)
        if result.get("closed"):
            result["strategy"] = owner
            notify.strategyExit(result)
            owners.pop(symbol, None)
        return result

    log("%s: staying in - %s" % (symbol, decision.reason))
    return {"held": True, "reason": decision.reason}


def handleFlat(client, symbol, owners, open_total, counts):
    """A symbol we are flat on: collect every active strategy vote."""
    candles = fetchCandles(client, symbol, signals.requiredTimeframes())
    if not any(candles.values()):
        log("%s: no candles returned, skipping" % symbol)
        return {"opened": False, "reason": "no candle data"}

    decision = signals.entrySignal(symbol, candles)

    if decision.action != signals.buy:
        log("%s: no entry - %s" % (symbol, decision.reason))
        return {"opened": False, "reason": decision.reason}

    # The caps are checked AFTER the signal so the log still records what the
    # strategies wanted. A blocked entry you cannot see in the log is a
    # strategy you cannot evaluate later.
    allowed, why = mayOpen(decision.strategy, open_total, counts)
    if not allowed:
        reason = "%s, so this signal is skipped: %s" % (why, decision.reason)
        log("%s: no entry - %s" % (symbol, reason))
        return {"opened": False, "reason": reason, "capped": True}

    # Price and volatility both come from the timeframe the OWNING strategy
    # trades. A stop sized from hourly candles would be several times too wide
    # for a 15-minute scalp and would sit outside the move it is protecting.
    timeframe = signals.strategyTimeframe(decision.strategy)
    bars = signals.closedCandles(candles.get(timeframe) or [])
    if not bars:
        log("%s: no closed candles on %s, skipping" % (symbol, timeframe))
        return {"opened": False, "reason": "no closed candles on %s" % timeframe}

    last_close = bars[-1][4]
    log("%s: flat, %d closed candle(s) on %s, last close %.6f"
        % (symbol, len(bars), timeframe, last_close))

    result = executor.execute(
        client, symbol, decision, last_close, log, signals.atrValue(candles.get(timeframe) or [])
    )
    if result.get("opened"):
        owners[symbol] = result.get("strategy")
        notify.positionOpened(result)
    return result


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run():
    problems = config.validate()
    if problems:
        for problem in problems:
            log("CONFIG ERROR: %s" % problem)
        raise SystemExit(1)

    for note in config.warnings():
        log("CONFIG WARNING: %s" % note)

    live = signals.activeStrategies()
    books = ", ".join("%s@%s" % (name, signals.strategyTimeframe(name)) for name in live)
    log(
        "start: strategy=%s (%s) votes=%d/%d regime=%s risk=%s dummy_mode=%s "
        "trigger=%s symbols=%d max_open=%s"
        % (
            config.strategy,
            books or "none",
            config.min_entry_votes,
            len(live),
            ("SMA%d" % config.regime_period) if config.regime_filter else "off",
            config.risk_model,
            config.dummy_mode,
            config.github_event_name,
            len(config.symbols),
            config.max_open_positions or "unlimited",
        )
    )
    if config.dummy_mode:
        log(
            "DUMMY MODE: market signals are ignored. Entries fire only with "
            "--force-entry, or on a GitHub workflow_dispatch run."
        )
    if not notify.enabled():
        log("WARNING: NTFY_TOPIC is not set, no push notifications will be sent")

    client = buildExchange()
    client.load_markets()
    # implode_hostname resolves the ccxt hostname template; printing it raw
    # makes the one line that confirms the demo host look broken.
    log(
        "connected to Bybit demo trading (%s), %s"
        % (
            client.implode_hostname(client.urls["api"]["private"]),
            clockReport(client),
        )
    )

    notified = loadNotified()
    closed_rows = fetchClosedPositions(client)
    if closed_rows is not None:
        notified = reportClosedPositions(client, closed_rows, notified)
    saveNotified(notified)

    if not config.symbols:
        log("no symbols configured. Set SYMBOLS in .env. Nothing to do.")
        return 0

    owners = loadOwners()
    failures = []

    # Pass one: what do we actually hold. See the module docstring for why
    # this cannot be folded into the loop below.
    held = readPositions(client, config.symbols)

    # Owners recorded for symbols we no longer hold are stale - the position
    # was closed by an exchange-side stop while nothing was running.
    for symbol in list(owners):
        if held.get(symbol) is None:
            owners.pop(symbol, None)

    open_total = sum(1 for position in held.values() if position is not None)
    counts = openCounts(held, owners)
    log("holding %d position(s) across %d symbol(s)%s"
        % (open_total, len(config.symbols),
           (" - " + ", ".join("%s=%d" % item for item in sorted(counts.items())))
           if counts else ""))

    # Pass two: act.
    for symbol in config.symbols:
        position = held.get(symbol)
        try:
            if position is not None:
                result = handleHeld(client, symbol, position, owners)
                if result.get("closed"):
                    open_total -= 1
                    counts = openCounts(held, owners)
            else:
                result = handleFlat(client, symbol, owners, open_total, counts)
                if result.get("opened"):
                    open_total += 1
                    opener = result.get("strategy")
                    if opener:
                        counts[opener] = counts.get(opener, 0) + 1
        except executor.ExecutionError as error:
            log("%s: EXECUTION ERROR %s" % (symbol, error))
            failures.append("%s: %s" % (symbol, error))
        except Exception as error:
            log("%s: UNEXPECTED ERROR %s" % (symbol, error))
            log(traceback.format_exc())
            failures.append("%s: %s" % (symbol, error))

    saveNotified(notified)
    saveOwners(owners)

    if failures:
        summary = "\n".join(failures)
        notify.criticalError("run finished with %d failure(s):\n%s" % (len(failures), summary))
        log("run finished with %d failure(s)" % len(failures))
        return 1

    log("run finished cleanly, %d position(s) open" % open_total)
    return 0


def main():
    try:
        return run()
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    except Exception as error:
        log("FATAL: %s" % error)
        log(traceback.format_exc())
        # keep the push short - the logs hold the traceback, and the topic is
        # readable by anyone who has it
        notify.criticalError("run aborted: %s" % error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
