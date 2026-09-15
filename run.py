"""
Laptop runner. The switch between "it runs itself" and "I run it".

    python run.py                 one cycle, then exit
    python run.py --loop          keep cycling until you press Ctrl+C
    python run.py --force-entry   one cycle that opens a test position
    python run.py --loop --interval 1   cycle every minute

Or set AUTOSTART=true in .env and plain `python run.py` loops by default.

WHY THERE IS NO CRON HERE
-------------------------
Scheduling lives inside this script rather than in cron, Task Scheduler or
launchd, because that is one mechanism that behaves identically on Windows,
macOS and Linux, and because you can see it working: the countdown to the next
cycle is right there in the terminal.

WHAT HAPPENS WHEN THE LAPTOP IS SHUT
------------------------------------
Nothing, which is fine. Stop losses, take profits and trailing stops live on
Bybit's servers and are enforced whether or not this script is running. What
you lose while it is off is the strategy's own exit rule and any new entries.
An open position is protected, not managed.
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta

import config
import main
import signals


def parseArgs():
    parser = argparse.ArgumentParser(
        description="Run the trading bot once, or on a loop.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="keep cycling until interrupted (overrides AUTOSTART)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single cycle and exit (overrides AUTOSTART)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        metavar="MINUTES",
        help="minutes between cycles in loop mode (default: %d)"
        % config.loop_interval_minutes,
    )
    parser.add_argument(
        "--force-entry",
        action="store_true",
        help="in dummy_mode, open a test position on this cycle regardless of "
        "the market. The local equivalent of the Run workflow button. Applies "
        "to the first cycle only.",
    )
    return parser.parse_args()


def resolveMode(args):
    """--loop and --once beat AUTOSTART, which beats the default of one run."""
    if args.loop and args.once:
        print("--loop and --once contradict each other, pick one")
        raise SystemExit(2)
    if args.loop:
        return True
    if args.once:
        return False
    return config.autostart


def sleepUntil(seconds):
    """Sleep in short slices so Ctrl+C is felt immediately, not in five minutes."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def runCycle(cycle):
    print("\n" + "=" * 70)
    print("cycle %d  %s" % (cycle, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("=" * 70)
    try:
        return main.main()
    except KeyboardInterrupt:
        raise
    except Exception as error:
        # A loop that dies on the first hiccup is worse than useless: it stops
        # managing positions without telling anyone.
        print("cycle %d crashed: %s" % (cycle, error))
        return 1


def run():
    args = parseArgs()
    looping = resolveMode(args)
    interval = args.interval if args.interval is not None else config.loop_interval_minutes

    if args.force_entry:
        config.force_entry = True

    if interval < 1:
        print("--interval must be at least 1 minute")
        return 2

    # Keep everything downstream agreeing with the interval actually in use.
    # The order-id bucket in particular is derived from it, and a bucket
    # longer than the interval blocks a cycle that legitimately wants to
    # retry. An explicit ORDER_BUCKET_SECONDS is left alone.
    config.loop_interval_minutes = interval
    if not os.environ.get("ORDER_BUCKET_SECONDS"):
        config.order_bucket_seconds = max(60, interval * 60)

    print("Claude-Finance runner")
    print("  mode:        %s" % ("loop every %d min" % interval if looping else "single run"))
    print("  strategy:    %s (%s)"
          % (config.strategy, ", ".join(signals.activeStrategies()) or "none"))
    print("  entry vote:  %d of %d strateg(ies) must agree"
          % (config.min_entry_votes, len(signals.activeStrategies())))
    print("  regime:      %s"
          % (("long only above SMA%d" % config.regime_period)
             if config.regime_filter else "filter OFF"))
    print("  risk model:  %s" % config.risk_model)
    print("  timeframe:   entry %s, exit %s" % (config.entry_timeframe, config.exit_timeframe))
    print("  dummy_mode:  %s" % config.dummy_mode)
    print("  max open:    %s" % (config.max_open_positions or "unlimited"))
    print("  symbols:     %s" % (", ".join(config.symbols) or "(none configured)"))
    if args.force_entry:
        print("  force-entry: yes, first cycle only")
    if not looping:
        return runCycle(1)

    print("\nCtrl+C to stop.")
    cycle = 0
    last_code = 0
    try:
        while True:
            cycle += 1
            last_code = runCycle(cycle)

            # One-shot: without this a forced entry would re-fire on every
            # symbol on every cycle, which is a fast way to open a lot of
            # positions you did not mean to.
            if config.force_entry:
                config.force_entry = False
                print("(force-entry consumed, later cycles follow the strategy)")

            next_run = datetime.now() + timedelta(minutes=interval)
            print("\nnext cycle at %s" % next_run.strftime("%H:%M:%S"))
            sleepUntil(interval * 60)
    except KeyboardInterrupt:
        print("\n\nstopped after %d cycle(s)." % cycle)
        print("Open positions stay protected by the stop loss, take profit and")
        print("trailing stop held on Bybit. They just will not be managed by the")
        print("strategy until you start this again.")
        return last_code


if __name__ == "__main__":
    sys.exit(run())
