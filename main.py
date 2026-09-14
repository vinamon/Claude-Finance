"""
Entry point. One pass over every configured symbol, then exit.

Shape of a run:
  1. validate config, build a demo-pinned Bybit client
  2. report positions Bybit closed recently (SL/TP/trailing/liquidation fills
     that happened while nothing was running)
  3. for each symbol holding a position: check the strategy exit rule
  4. for each symbol holding nothing: check the strategy entry rule
  5. log every decision and why, then exit 0, or exit 1 on a real failure

Exiting non-zero matters: a red run in the GitHub mobile app is the cheapest
monitoring you will ever get.

SCHEDULING REALITY
------------------
GitHub's cron is best-effort. A */5 schedule is regularly late, and under load
runs get skipped outright. Nothing here may assume even spacing or that the
previous run happened at all - which is why state lives on the exchange and
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
from exchange import buildExchange

started_at = time.time()


def log(message):
    elapsed = time.time() - started_at
    print("[%7.2fs] %s" % (elapsed, message), flush=True)


# ---------------------------------------------------------------------------
# closed-position reporting
# ---------------------------------------------------------------------------


def loadNotified():
    """Ids of closes already pushed, so a close inside the lookback window is
    not announced on every run.

    Best effort by design. In GitHub Actions this file is carried between runs
    by actions/cache; if the cache misses, we simply re-announce. Trading
    correctness never depends on it - only your phone's dignity does.
    """
    path = config.notified_state_file
    if not path:
        return set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return set(data.get("ids", []))
    except FileNotFoundError:
        return set()
    except Exception as error:
        log("could not read notify state (%s), treating as empty" % error)
        return set()


def saveNotified(ids):
    path = config.notified_state_file
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # keep the file from growing forever
        trimmed = sorted(ids)[-500:]
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"ids": trimmed}, handle)
    except Exception as error:
        log("could not write notify state (%s), continuing" % error)


def reportClosedPositions(client, notified):
    """Announce positions Bybit closed on its own since we last looked."""
    window_ms = config.closed_lookback_minutes * 60 * 1000
    start_time = int(time.time() * 1000) - window_ms

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
        return notified

    rows = ((response or {}).get("result") or {}).get("list") or []
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
        }
        log(
            "position closed: %s qty=%s entry=%s exit=%s pnl=%s"
            % (
                record["symbol"],
                record["qty"],
                record["avg_entry"],
                record["avg_exit"],
                record["closed_pnl"],
            )
        )
        notify.positionClosed(record)
        notified.add(key)

    return notified


def toFloat(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# per-symbol work
# ---------------------------------------------------------------------------


def handleSymbol(client, symbol):
    """One symbol: exit check if holding, entry check if flat."""
    position = executor.openPosition(client, symbol)

    if position is not None:
        size = executor.positionSize(position)
        log("%s: holding %s contracts, checking the exit rule" % (symbol, size))

        candles = client.fetch_ohlcv(
            symbol,
            timeframe=config.exit_timeframe,
            limit=signals.requiredCandles(),
            params={"category": config.category},
        )
        decision = signals.exitSignal(symbol, candles)

        if decision.action == signals.close:
            result = executor.closePosition(client, symbol, position, decision.reason, log)
            if result.get("closed"):
                notify.strategyExit(result)
            return result

        log("%s: staying in - %s" % (symbol, decision.reason))
        return {"held": True, "reason": decision.reason}

    candles = client.fetch_ohlcv(
        symbol,
        timeframe=config.entry_timeframe,
        limit=signals.requiredCandles(),
        params={"category": config.category},
    )
    closed = signals.closedCandles(candles)
    if not closed:
        log("%s: no closed candles returned, skipping" % symbol)
        return {"opened": False, "reason": "no candle data"}

    last_close = closed[-1][4]
    log(
        "%s: flat. %d candle(s) on %s, last closed candle %.6f"
        % (symbol, len(closed), config.entry_timeframe, last_close)
    )

    decision = signals.entrySignal(symbol, candles)
    result = executor.execute(client, symbol, decision, last_close, log)
    if result.get("opened"):
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

    log(
        "start: strategy=%s dummy_mode=%s trigger=%s entry_tf=%s exit_tf=%s symbols=%d"
        % (
            config.strategy,
            config.dummy_mode,
            config.github_event_name,
            config.entry_timeframe,
            config.exit_timeframe,
            len(config.symbols),
        )
    )
    if config.dummy_mode:
        log(
            "DUMMY MODE: market signals are ignored. Entries fire only on a manual "
            "workflow_dispatch run."
        )
    if not notify.enabled():
        log("WARNING: NTFY_TOPIC is not set, no push notifications will be sent")

    client = buildExchange()
    client.load_markets()
    log("connected to Bybit demo trading (%s)" % client.urls["api"]["private"])

    notified = loadNotified()
    notified = reportClosedPositions(client, notified)
    saveNotified(notified)

    if not config.symbols:
        log(
            "no symbols configured. Set the SYMBOLS repository variable or edit "
            "config.py - see the TODO block there. Nothing to do."
        )
        return 0

    failures = []
    for symbol in config.symbols:
        try:
            handleSymbol(client, symbol)
        except executor.ExecutionError as error:
            log("%s: EXECUTION ERROR %s" % (symbol, error))
            failures.append("%s: %s" % (symbol, error))
        except Exception as error:
            log("%s: UNEXPECTED ERROR %s" % (symbol, error))
            log(traceback.format_exc())
            failures.append("%s: %s" % (symbol, error))

    saveNotified(notified)

    if failures:
        summary = "\n".join(failures)
        notify.criticalError("run finished with %d failure(s):\n%s" % (len(failures), summary))
        log("run finished with %d failure(s)" % len(failures))
        return 1

    log("run finished cleanly")
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
