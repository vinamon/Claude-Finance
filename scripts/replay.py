"""
Replay the strategies over past candles, with the bot's own code.

    python scripts/replay.py                      every strategy alone, 90 days
    python scripts/replay.py --days 26 --end 2026-09-24
    python scripts/replay.py --multi              the ACTIVE_STRATEGIES set, as live
    python scripts/replay.py --set ICT_MIN_RR=2   one change against the baseline
    python scripts/replay.py --wave 1 --jobs 4    the first wave of variants

Nothing here decides a trade. Each closed bar is handed to
signals.entrySignal / signals.exitSignal through the same window the live bot
passes them, and every stop and target comes from the executor's own maths,
so the replay cannot drift away from what the bot does. What it adds is the
one thing the bot cannot see: what happened next.

HOW A BAR IS PLAYED
-------------------
  1. what was decided at the close of the previous bar fills at this bar's
     open: an entry, or the owning strategy's exit
  2. the exchange-side stop and target are checked against this bar's range.
     When one bar touches both, the stop wins - the bar's path inside is
     unknown and assuming the better case flatters every strategy. A bar that
     opens beyond a level fills at the open.
  3. the bot decides at this bar's close, seeing only bars up to this one

Fees and slippage are charged on both fills, slippage always against the
trade. The re-entry cooldown is played as the live bot plays it, in candles
of the entering strategy. Symbols are independent: with one position per
symbol and ten symbols, MAX_OPEN_POSITIONS=10 never binds.

Not modelled: the trailing stop (the replay refuses to run with one), funding,
and fills worse than the slippage allowance in a violent bar.

THE KEEP RULE
-------------
The sample is split in two halves by entry time. A row passes when it is
positive in one half and not negative in the other; a variant helps only when
it improves the baseline in both halves. One good half is a market, not an
edge.

Candles are cached under state/candles/, which is gitignored. --offline uses
the cache alone.
"""

import argparse
import collections
import concurrent.futures
import datetime
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ccxt

import config
import executor
import main
import signals
from exchange import buildExchange

cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "state", "candles")

# The environment as .env left it, before any variant touches it.
base_environ = dict(os.environ)

# What every replay run pins, whatever .env says: a replay of dummy mode would
# replay nothing, and each strategy is judged on its own trades.
forced = {"DUMMY_MODE": "false", "STRATEGY": "multi", "MIN_ENTRY_VOTES": "1",
          "LOG_DETAIL": "false"}

# Variants to try against the baseline, one change at a time:
# (label, settings, strategies it concerns or None for all).
waves = {
    "1": [
        ("ICT_KILL_ZONES=02:00-05:00,07:00-11:00",
         {"ICT_KILL_ZONES": "02:00-05:00,07:00-11:00"}, ["ict"]),
        ("ICT_MIN_RR=1.0", {"ICT_MIN_RR": "1.0"}, ["ict"]),
        ("ICT_MIN_RR=2.0", {"ICT_MIN_RR": "2.0"}, ["ict"]),
        ("ICT_MIN_RR=3.0", {"ICT_MIN_RR": "3.0"}, ["ict"]),
        ("ICT_STOP_REF=leglow", {"ICT_STOP_REF": "leglow"}, ["ict"]),
        ("ICT_STOP_FLOOR_ATR=1.0", {"ICT_STOP_FLOOR_ATR": "1.0"}, ["ict"]),
        ("ICT_STOP_FLOOR_ATR=2.0", {"ICT_STOP_FLOOR_ATR": "2.0"}, ["ict"]),
        ("REGIME_PERIOD=400", {"REGIME_PERIOD": "400"}, None),
        ("REGIME_PERIOD=800", {"REGIME_PERIOD": "800"}, None),
        ("BREAKOUT_EXIT_LOOKBACK=0", {"BREAKOUT_EXIT_LOOKBACK": "0"}, ["breakout"]),
        ("BREAKOUT_EXIT_LOOKBACK=20", {"BREAKOUT_EXIT_LOOKBACK": "20"}, ["breakout"]),
    ],
}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def applySettings(settings, base=None):
    """Rebuild config from .env plus `settings`, exactly as the bot parses it.

    A worker process passes the parent's `base`: it inherits whatever the
    parent's environment held when it was started, variant included.
    """
    os.environ.clear()
    os.environ.update(base_environ if base is None else base)
    os.environ.update(forced)
    os.environ.update(settings)
    importlib.reload(config)


def strategySettings(names, overrides):
    """The settings for one run: the strategies to trade, then the variant."""
    settings = {"ACTIVE_STRATEGIES": ",".join(names)}
    settings.update(overrides)
    return settings


def refusal():
    """Why this configuration cannot be replayed honestly, or None."""
    # Candles are public; the keys are not needed to read them.
    ignored = ("BYBIT_API_KEY / BYBIT_API_SECRET are not set", config.envDiagnosis())
    problems = [problem for problem in config.validate() if problem not in ignored]
    if problems:
        return "; ".join(problems)
    if config.atr_trail_mult > 0 or config.trailing_stop_pct > 0:
        return ("a trailing stop is set (ATR_TRAIL_MULT / TRAILING_STOP_PCT) and the replay "
                "does not model one, so its results would describe a different bot")
    if config.strategy_timeframes:
        return "STRATEGY_TIMEFRAMES is set; the replay plays one clock, ENTRY_TIMEFRAME"
    return None


# ---------------------------------------------------------------------------
# candles
# ---------------------------------------------------------------------------


def cachePath(market_id, timeframe):
    return os.path.join(cache_dir, "%s-%s.json" % (market_id, timeframe))


def fetchRange(client, symbol, timeframe, since_ms, until_ms):
    """Every closed candle from since_ms up to until_ms, oldest first."""
    step_ms = ccxt.Exchange.parse_timeframe(timeframe) * 1000
    rows = []
    cursor = since_ms
    while cursor < until_ms:
        page = client.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000,
                                  params={"category": config.category})
        page = [row for row in page if cursor <= row[0] < until_ms]
        if not page:
            break
        rows.extend(page)
        cursor = page[-1][0] + step_ms
    return rows


def loadCandles(client, symbol, timeframe, start_ms, end_ms, offline):
    """(tick, closed candles from start_ms to end_ms), cached on disk.

    Only the part the cache lacks is fetched, at either end.
    """
    step_ms = ccxt.Exchange.parse_timeframe(timeframe) * 1000
    market_id = symbol.split("/")[0] + "USDT"
    path = cachePath(market_id, timeframe)
    cached = {"tick": None, "rows": []}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)

    rows = cached["rows"]
    tick = cached["tick"]
    if not offline:
        if tick is None:
            tick = executor.instrumentSpec(client, symbol)["tick_size"]
        # A candle is closed once its open time is a whole step in the past.
        closed_until = min(end_ms, client.milliseconds() // step_ms * step_ms)
        head, tail = [], []
        if not rows or rows[0][0] > start_ms:
            head = fetchRange(client, symbol, timeframe, start_ms,
                              rows[0][0] if rows else closed_until)
        if rows and rows[-1][0] + step_ms < closed_until:
            tail = fetchRange(client, symbol, timeframe, rows[-1][0] + step_ms, closed_until)
        if head or tail:
            merged = {row[0]: row for row in head + rows + tail}
            rows = [merged[key] for key in sorted(merged)]
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"tick": tick, "rows": rows}, handle)

    if tick is None:
        raise SystemExit("no cached candles for %s; run once without --offline" % symbol)
    return tick, [row for row in rows if start_ms <= row[0] < end_ms]


# ---------------------------------------------------------------------------
# one symbol, one run
# ---------------------------------------------------------------------------


def bracket(decision, price, atr_value, tick):
    """(plan, refused) for an entry at `price`, from the executor's own maths."""
    targets = executor.planEntry({"tick_size": tick}, decision.side, decision.setup, price,
                                 atr_value)
    if targets["refused"]:
        return None, targets["refused"][0]
    return {"side": decision.side, "stop": targets["stop_loss"],
            "target": targets["take_profit"], "capped": bool(targets["capped"])}, None


def simulate(task):
    """Play one symbol under one set of settings. Returns trades and refusals."""
    base, settings, symbol, tick, rows, eval_start_ms, fee, slippage = task
    applySettings(settings, base)

    timeframe = config.entry_timeframe
    step_ms = ccxt.Exchange.parse_timeframe(timeframe) * 1000
    wait_ms = main.cooldownSeconds(timeframe) * 1000
    window_size = max(signals.requiredCandles(name) for name in signals.activeStrategies())

    trades = []
    refused = collections.Counter()
    position = None
    pending = None
    last_close_ms = None

    def finish(time_ms, fill, cause):
        sign = 1.0 if position["side"] == "long" else -1.0
        exit_fill = fill * (1.0 - sign * slippage)
        net = sign * (exit_fill / position["fill"] - 1.0) - 2.0 * fee
        risk = abs(position["fill"] - position["stop"]) / position["fill"]
        trades.append({
            "symbol": symbol, "strategy": position["strategy"], "side": position["side"],
            "entry_ms": position["entry_ms"], "exit_ms": time_ms, "net": net,
            "r": net / risk if risk else 0.0, "cause": cause,
        })

    for k in range(len(rows)):
        open_ms, open_price, high, low, close_price = rows[k][:5]

        # 1. what the last close decided fills at this open
        if pending is not None:
            kind, payload = pending
            pending = None
            if kind == "enter" and position is None:
                decision, atr_value = payload
                plan, why = bracket(decision, open_price, atr_value, tick)
                if why:
                    refused[(decision.strategy, why)] += 1
                else:
                    sign = 1.0 if plan["side"] == "long" else -1.0
                    position = dict(plan, strategy=decision.strategy, entry_ms=open_ms,
                                    fill=open_price * (1.0 + sign * slippage))
            elif kind == "exit" and position is not None:
                finish(open_ms, open_price, "rule")
                position = None
                last_close_ms = open_ms

        # 2. the exchange-side stop and target, stop first
        if position is not None:
            long_side = position["side"] == "long"
            stop, target = position["stop"], position["target"]
            hit = None
            if stop is not None and (low <= stop if long_side else high >= stop):
                beyond = open_price <= stop if long_side else open_price >= stop
                hit = ("stop", open_price if beyond else stop)
            elif target is not None and (high >= target if long_side else low <= target):
                beyond = open_price >= target if long_side else open_price <= target
                hit = ("target", open_price if beyond else target)
            if hit:
                finish(open_ms + step_ms, hit[1], hit[0])
                position = None
                last_close_ms = open_ms + step_ms

        # 3. decide at this close, seeing nothing later
        if open_ms < eval_start_ms or k + 1 < window_size:
            continue
        window = rows[k + 2 - window_size:k + 1]
        # The live bot's last row is the candle still forming, which
        # closedCandles() drops unread; a copy of this bar stands in for it.
        candles = {timeframe: window + [rows[k]]}
        if position is not None:
            decision = signals.exitSignal(symbol, candles, position["strategy"],
                                          position["side"])
            if decision.action == signals.close:
                pending = ("exit", None)
            continue
        if last_close_ms is not None and open_ms + step_ms - last_close_ms < wait_ms:
            continue
        decision = signals.entrySignal(symbol, candles)
        if decision.action == signals.enter:
            pending = ("enter", (decision, signals.atrValue(candles[timeframe])))

    if position is not None:
        finish(rows[-1][0] + step_ms, rows[-1][4], "end")
    return {"trades": trades, "refused": dict(refused)}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def summary(trades, days):
    count = len(trades)
    if not count:
        return {"trades": 0, "per_day": 0.0, "win": 0.0, "avg_r": 0.0, "avg": 0.0,
                "sum": 0.0, "drawdown": 0.0, "exits": collections.Counter()}
    running = peak = drawdown = 0.0
    for trade in sorted(trades, key=lambda trade: trade["exit_ms"]):
        running += trade["net"]
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    return {
        "trades": count,
        "per_day": count / days if days else 0.0,
        "win": sum(1 for trade in trades if trade["net"] > 0) / float(count),
        "avg_r": sum(trade["r"] for trade in trades) / count,
        "avg": sum(trade["net"] for trade in trades) / count,
        "sum": sum(trade["net"] for trade in trades),
        "drawdown": drawdown,
        "exits": collections.Counter(trade["cause"] for trade in trades),
    }


def verdict(first, second):
    """Positive in one half and not negative in the other."""
    if first["trades"] + second["trades"] == 0:
        return "no trades"
    halves = (first["sum"], second["sum"])
    return "pass" if max(halves) > 0 and min(halves) >= 0 else "fail"


def splitHalves(trades, eval_start_ms, end_ms):
    middle = (eval_start_ms + end_ms) // 2
    return ([trade for trade in trades if trade["entry_ms"] < middle],
            [trade for trade in trades if trade["entry_ms"] >= middle])


def printReport(title, trades, refused, eval_start_ms, end_ms):
    days = (end_ms - eval_start_ms) / 86400000.0
    print("\n== %s" % title)
    header = "%-10s %-5s %-4s %6s %6s %6s %7s %8s %8s %7s  %s" % (
        "strategy", "side", "half", "trades", "/day", "win%", "avg R", "avg %", "sum %",
        "maxDD%", "exits stop/target/rule/end")
    print(header)
    keys = sorted({(trade["strategy"], trade["side"]) for trade in trades})
    for strategy, side in keys:
        mine = [trade for trade in trades if (trade["strategy"], trade["side"]) == (strategy, side)]
        halves = splitHalves(mine, eval_start_ms, end_ms)
        stats = [summary(half, days / 2.0) for half in halves]
        for label, stat in zip(("1st", "2nd"), stats):
            exits = stat["exits"]
            print("%-10s %-5s %-4s %6d %6.1f %6.0f %7.2f %8.3f %8.2f %7.2f  %d/%d/%d/%d" % (
                strategy, side, label, stat["trades"], stat["per_day"], 100 * stat["win"],
                stat["avg_r"], 100 * stat["avg"], 100 * stat["sum"], 100 * stat["drawdown"],
                exits["stop"], exits["target"], exits["rule"], exits["end"]))
        print("%-10s %-5s -> %s" % (strategy, side, verdict(*stats)))

    if refused:
        print("refused entries: %s" % ", ".join(
            "%s %s=%d" % (strategy, why, count)
            for (strategy, why), count in sorted(refused.items())))

    symbols = sorted({trade["symbol"] for trade in trades})
    if symbols and keys:
        print("per symbol, sum % (trades):")
        for symbol in symbols:
            cells = []
            for strategy, side in keys:
                mine = [trade for trade in trades if trade["symbol"] == symbol
                        and (trade["strategy"], trade["side"]) == (strategy, side)]
                cells.append("%s-%s %+.2f (%d)" % (strategy, side,
                                                   100 * sum(t["net"] for t in mine), len(mine)))
            print("  %-20s %s" % (symbol, "  ".join(cells)))


def halfSums(trades, eval_start_ms, end_ms):
    """(strategy, side) -> (sum of the first half, sum of the second)."""
    sums = {}
    for key in sorted({(trade["strategy"], trade["side"]) for trade in trades}):
        mine = [trade for trade in trades if (trade["strategy"], trade["side"]) == key]
        first, second = splitHalves(mine, eval_start_ms, end_ms)
        sums[key] = (sum(t["net"] for t in first), sum(t["net"] for t in second))
    return sums


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def runSet(label, settings, data, eval_start_ms, end_ms, args, pool):
    """Play every symbol under `settings`; return (trades, refusals)."""
    tasks = [(base_environ, settings, symbol, tick, rows, eval_start_ms, args.fee, args.slippage)
             for symbol, (tick, rows) in data.items()]
    results = pool.map(simulate, tasks) if pool else map(simulate, tasks)
    trades, refused = [], collections.Counter()
    for result in results:
        trades.extend(result["trades"])
        refused.update({tuple(key): count for key, count in result["refused"].items()})
    return trades, refused


def parseArgs():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--days", type=int, default=90, help="days to evaluate (default 90)")
    parser.add_argument("--end", help="last day to include, YYYY-MM-DD UTC (default: now)")
    parser.add_argument("--strategies", help="comma list to replay, each alone "
                        "(default: every strategy the bot has)")
    parser.add_argument("--multi", action="store_true",
                        help="replay ACTIVE_STRATEGIES from .env together, in priority order")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="a variant to compare against the baseline; repeatable")
    parser.add_argument("--wave", choices=sorted(waves), help="run a wave of variants")
    parser.add_argument("--fee", type=float, default=0.00055,
                        help="taker fee per fill (default 0.00055: 0.11%% a round trip)")
    parser.add_argument("--slippage", type=float, default=0.0002,
                        help="slippage per fill, against the trade (default 0.0002)")
    parser.add_argument("--offline", action="store_true", help="use cached candles only")
    parser.add_argument("--jobs", type=int, default=1, help="worker processes")
    return parser.parse_args()


def main_():
    args = parseArgs()
    applySettings({})
    timeframe = config.entry_timeframe
    step_ms = ccxt.Exchange.parse_timeframe(timeframe) * 1000

    if args.end:
        end_day = datetime.datetime.strptime(args.end, "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc)
        end_ms = int((end_day + datetime.timedelta(days=1)).timestamp() * 1000)
    else:
        end_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    end_ms = end_ms // step_ms * step_ms
    eval_start_ms = end_ms - args.days * 86400000
    # Room for the longest window any variant asks for.
    start_ms = eval_start_ms - config.candle_ceiling * step_ms

    if args.multi:
        sets = [("multi", list(config.active_strategies))]
    else:
        names = args.strategies.split(",") if args.strategies else list(signals.strategies)
        sets = [(name, [name]) for name in names]

    client = None
    if not args.offline:
        client = buildExchange()
        client.load_markets()
    data = {}
    for symbol in config.symbols:
        tick, rows = loadCandles(client, symbol, timeframe, start_ms, end_ms, args.offline)
        if rows:
            data[symbol] = (tick, rows)
        print("%-20s %d candles" % (symbol, len(rows)), flush=True)

    variants = []
    for item in args.set:
        name, _, value = item.partition("=")
        variants.append((item, {name.strip(): value.strip()}, None))
    if args.wave:
        variants.extend(waves[args.wave])

    print("\nreplay %s to %s UTC, %d days, %d symbols, %s candles, fee %.3f%% + slippage "
          "%.3f%% per fill" % (
              datetime.datetime.fromtimestamp(eval_start_ms / 1000, datetime.timezone.utc)
              .strftime("%Y-%m-%d %H:%M"),
              datetime.datetime.fromtimestamp(end_ms / 1000, datetime.timezone.utc)
              .strftime("%Y-%m-%d %H:%M"),
              args.days, len(data), timeframe, 100 * args.fee, 100 * args.slippage))

    pool = concurrent.futures.ProcessPoolExecutor(args.jobs) if args.jobs > 1 else None
    try:
        baseline = {}
        for label, names in sets:
            settings = strategySettings(names, {})
            applySettings(settings)
            why = refusal()
            if why:
                print("\n== %s: not replayed - %s" % (label, why))
                continue
            trades, refused = runSet(label, settings, data, eval_start_ms, end_ms, args, pool)
            printReport("%s, baseline" % label, trades, refused, eval_start_ms, end_ms)
            baseline[label] = halfSums(trades, eval_start_ms, end_ms)

        for variant_label, overrides, concerns in variants:
            for label, names in sets:
                if concerns and not set(concerns) & set(names):
                    continue
                settings = strategySettings(names, overrides)
                applySettings(settings)
                why = refusal()
                if why:
                    print("\n== %s, %s: not replayed - %s" % (label, variant_label, why))
                    continue
                trades, refused = runSet(label, settings, data, eval_start_ms, end_ms,
                                         args, pool)
                printReport("%s, %s" % (label, variant_label), trades, refused,
                            eval_start_ms, end_ms)
                for key, (first, second) in halfSums(trades, eval_start_ms, end_ms).items():
                    base_first, base_second = baseline.get(label, {}).get(key, (0.0, 0.0))
                    helps = first > base_first and second > base_second
                    print("%s-%s against the baseline: 1st %+.2f%%, 2nd %+.2f%% -> helps both "
                          "halves: %s" % (key[0], key[1], 100 * (first - base_first),
                                          100 * (second - base_second),
                                          "yes" if helps else "no"))
    finally:
        if pool:
            pool.shutdown()


if __name__ == "__main__":
    main_()
