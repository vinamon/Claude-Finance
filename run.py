"""
Laptop runner. The switch between "it runs itself" and "I run it".

    python run.py                 one cycle, then exit
    python run.py --loop          keep cycling until you press Ctrl+C
    python run.py --force-entry   one cycle that opens a test position
    python run.py --loop --interval 1   cycle every minute
    python run.py --kill-all      stop every other running copy, then exit

Or set AUTOSTART=true in .env and plain `python run.py` loops by default.

STOPPING IT
-----------
Ctrl+C in the window it runs in, or `--kill-all` from anywhere - useful when
the loop was started in a terminal that is now closed, or when a second copy
got started by accident. Neither touches open positions: the stop loss, take
profit and trailing stop live on Bybit and keep working regardless.

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
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import config
import main
import signals


# ---------------------------------------------------------------------------
# finding and stopping other copies of the bot
#
# WHY THIS EXISTS. Two loops running at once do not open duplicate positions:
# every cycle asks the exchange what it holds, and the orderLinkId bucket
# rejects a second order inside the same window. What they do wreck is
# state/owners.json. Both processes write it and the later write wins, so the
# record of WHICH strategy opened a position can be lost - and the exit then
# falls back to UNKNOWN_OWNER_EXIT instead of the rule that belongs to the
# trade. You also get every push notification twice.
#
# There is deliberately no psutil dependency for this. One convenience command
# is not worth another package to install and pin, so the process list comes
# from whatever the platform already ships.
# ---------------------------------------------------------------------------

project_dir = Path(__file__).resolve().parent
project_name = project_dir.name


def processTable():
    """Every running process as (pid, parent_pid, executable, command line).

    Best effort, never raises. Windows has no ps, so the PowerShell process
    table stands in. The tab separator matters: command lines are full of
    spaces, and splitting on those would cut paths in half.

    The executable is carried separately from the command line because the two
    answer different questions - "what program is this" versus "what was it
    asked to do" - and only the first can be trusted to identify a process.
    The parent is carried because a virtualenv launcher shows up as a second
    process running the same command as its own child.
    """
    if os.name == "nt":
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t"
            "$($_.Name)`t$($_.CommandLine)\" }"
        )
        command = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        separator = "\t"
    else:
        command = ["ps", "-eo", "pid=,ppid=,comm=,args="]
        separator = None

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except Exception as error:
        print("could not read the process list (%s)" % error)
        return []

    rows = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(separator, 3) if separator else line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_text, parent_text, executable, command_line = parts
        try:
            rows.append((int(pid_text), int(parent_text),
                         executable.strip(), command_line.strip()))
        except ValueError:
            continue
    return rows


def botProcesses():
    """Other processes running the run.py of THIS project, as (pid, command).

    Three conditions, and the first one is not optional.

    THE PROCESS MUST BE A PYTHON INTERPRETER. Matching on the command line
    alone was a real bug, caught the first time this ran: the shell executing
    the kill-all command carries both "run.py" and the project path in its OWN
    command line, so the command killed the terminal it was typed into.
    Editors, terminals and task runners mention file paths constantly; only an
    interpreter actually runs one.

    Then the command line has to name run.py, and it has to name this project
    directory, so an unrelated project run.py is never a target. Our own pid
    and our parent pid are excluded, which keeps the command from killing the
    shell that launched it.
    """
    mine = {os.getpid(), os.getppid()}
    found = []
    for pid, parent, executable, command in processTable():
        if pid in mine:
            continue
        if not os.path.basename(executable).lower().startswith("python"):
            continue
        if "run.py" not in command:
            continue
        if project_name.lower() not in command.lower():
            print("  (ignoring PID %d, a run.py that does not look like %s)"
                  % (pid, project_name))
            continue
        found.append((pid, parent, command))
    return [(pid, command) for pid, parent, command in found]


def botRoots():
    """One entry per RUNNING BOT, rather than per process.

    A virtualenv on Windows launches python.exe as a small stub that starts
    the real interpreter as its child, so a single `run.py --loop` shows up
    twice in the process table with an identical command line. Counting raw
    processes therefore reports two bots where there is one - which is not a
    cosmetic problem, because "you are running two copies" is a warning that
    sends someone hunting for a duplicate that does not exist.

    A process whose parent is also a bot process is a child of one, not a bot
    of its own.
    """
    rows = []
    for pid, parent, executable, command in processTable():
        if pid in {os.getpid(), os.getppid()}:
            continue
        if not os.path.basename(executable).lower().startswith("python"):
            continue
        if "run.py" not in command or project_name.lower() not in command.lower():
            continue
        rows.append((pid, parent, command))
    pids = {pid for pid, _, _ in rows}
    return [(pid, command) for pid, parent, command in rows if parent not in pids]


def stopProcess(pid, force):
    """Ask a process to stop, or insist. True if the request was accepted."""
    if os.name == "nt":
        # /T takes the process tree. A virtualenv launcher and the real
        # interpreter it started are one bot, and killing only the parent can
        # leave the child cycling.
        command = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
    else:
        command = ["kill", "-KILL" if force else "-TERM", str(pid)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except Exception as error:
        print("  could not signal PID %d: %s" % (pid, error))
        return False


def killAll():
    """Stop every other copy of this bot. Returns a process exit code.

    Polite first, forceful only if needed. A cycle in flight is usually
    mid-HTTP-call to Bybit, and a forced kill during an order round trip is
    the one moment the local view and the exchange can disagree. Nothing is
    actually lost when it happens - the next run asks Bybit what it holds -
    but waiting KILL_GRACE_SECONDS is cheaper than not waiting.
    """
    targets = botRoots()
    if not targets:
        print("No other copy of the bot is running.")
        return 0

    extra = len(botProcesses()) - len(targets)
    print("Found %d running cop%s of the bot%s:"
          % (len(targets), "y" if len(targets) == 1 else "ies",
             " (plus %d launcher/child process(es))" % extra if extra > 0 else ""))
    for pid, command in targets:
        print("  PID %-7d %s" % (pid, command))

    print("\nAsking them to stop (up to %ds)..." % config.kill_grace_seconds)
    for pid, _ in targets:
        stopProcess(pid, force=False)

    deadline = time.monotonic() + config.kill_grace_seconds
    while time.monotonic() < deadline:
        if not botProcesses():
            break
        time.sleep(0.3)

    survivors = botProcesses()
    if survivors:
        print("Still up, forcing:")
        for pid, _ in survivors:
            print("  PID %d" % pid)
            stopProcess(pid, force=True)
        time.sleep(1.0)

    left = botProcesses()
    if left:
        print("\nCould not stop: %s" % ", ".join(str(pid) for pid, _ in left))
        print("Another user may own them, or they need administrator rights.")
        return 1

    print("\nStopped %d process(es). Nothing of this bot is running now."
          % len(targets))
    print("Open positions stay protected by the stop loss, take profit and")
    print("trailing stop held on Bybit. They just will not be managed by the")
    print("strategy until you start the bot again.")
    return 0


def warnAboutDuplicates():
    """Say something before a second loop is started by accident."""
    others = botRoots()
    if not others:
        return
    print("\n  WARNING: the bot already appears to be running:")
    for pid, command in others:
        print("    PID %-7d %s" % (pid, command))
    print("  Two loops fight over state/owners.json and double every push.")
    print("  Stop the other one with:  python run.py --kill-all\n")


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
        "--kill-all",
        action="store_true",
        help="stop every other running copy of this bot, then exit. Does not "
        "touch open positions - the exchange-side stop loss, take profit and "
        "trailing stop keep protecting them.",
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
    if config.log_detail:
        print("\n" + "=" * 70)
    print("cycle %d  %s" % (cycle, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    if config.log_detail:
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

    # Answered before config validation and before anything that needs keys:
    # stopping a runaway loop must not itself depend on the configuration
    # being correct.
    if args.kill_all:
        return killAll()

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

    if config.log_detail:
        printSettings(interval, looping, args.force_entry)
    else:
        print("Claude-Finance: %s, %d symbols, %.0f USDT at %dx, dummy_mode=%s%s"
              % ("loop every %d min" % interval if looping else "single run",
                 len(config.symbols), config.position_notional_usdt, config.leverage,
                 config.dummy_mode, ", force-entry" if args.force_entry else ""))
    if not looping:
        return runCycle(1)

    # A second loop is the mistake worth catching, and only loops collide -
    # a single run finishes before it can race anything.
    warnAboutDuplicates()

    print("Ctrl+C to stop.")
    return loop(interval)


def printSettings(interval, looping, force_entry):
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
    # Notional and margin are shown together on purpose. They are the two
    # numbers people most often confuse, and leverage silently decides which
    # one you are actually risking.
    print("  size:        %.0f USDT notional, %.2f USDT margin at %dx"
          % (config.position_notional_usdt,
             config.position_notional_usdt / max(1, config.leverage),
             config.leverage))
    print("  timeframe:   entry %s, exit %s" % (config.entry_timeframe, config.exit_timeframe))
    print("  dummy_mode:  %s" % config.dummy_mode)
    print("  max open:    %s" % (config.max_open_positions or "unlimited"))
    print("  symbols:     %s" % (", ".join(config.symbols) or "(none configured)"))
    if force_entry:
        print("  force-entry: yes, first cycle only")


def loop(interval):
    cycle = 0
    last_code = 0
    try:
        while True:
            cycle += 1
            started = time.monotonic()
            last_code = runCycle(cycle)
            elapsed = time.monotonic() - started

            # One-shot: without this a forced entry would re-fire on every
            # symbol on every cycle, which is a fast way to open a lot of
            # positions you did not mean to.
            if config.force_entry:
                config.force_entry = False
                print("(force-entry consumed, later cycles follow the strategy)")

            # The interval is a PERIOD, not a gap. Sleeping the full
            # interval AFTER the work would stretch every gap by however long
            # the cycle itself took, which is not what "every N minutes"
            # asked for. Sleep only the remainder.
            remaining = interval * 60 - elapsed
            if remaining <= 0:
                print("  cycle took %.0fs, longer than the %d min interval - "
                      "starting the next one immediately" % (elapsed, interval))
                continue
            next_run = datetime.now() + timedelta(seconds=remaining)
            print("  took %.0fs, next at %s"
                  % (elapsed, next_run.strftime("%H:%M:%S")))
            sleepUntil(remaining)
    except KeyboardInterrupt:
        print("\n\nstopped after %d cycle(s)." % cycle)
        print("Open positions stay protected by the stop loss, take profit and")
        print("trailing stop held on Bybit. They just will not be managed by the")
        print("strategy until you start this again.")
        return last_code


if __name__ == "__main__":
    sys.exit(run())
