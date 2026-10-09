# Claude-Finance

Trading bot on a **Bybit Demo Trading** account. Python, ccxt, virtual funds
only. No path to a real account exists or should be added.

Before changing strategy logic, consult the trader agent. Take his trading knowledge as superior to yours, whilst standing your ground when something cannot be executed by code.

Run it from a laptop, not CI. See "Why not GitHub Actions" below.

## Layout

| File | Role |
|---|---|
| `config.py` | every knob, from `.env` / env vars, plus `validate()`, `warnings()` and `retired_settings` |
| `signals.py` | indicators, the regime filter, the strategy blocks (`ict`, `breakout`) and their registry, the mirrored chart for shorts, voting. Pure functions, no orders |
| `executor.py` | decisions to orders: `planEntry` (the one bracket planner), sizing, leverage, the order tag. No strategy logic |
| `exchange.py` | ccxt client pinned to the demo host |
| `notify.py` | ntfy.sh push |
| `main.py` | one pass: closed-position reports, exits, entries |
| `run.py` | laptop runner, the loop/once switch |
| `scripts/replay.py` | plays past candles through the bot's own signal and bracket code, and judges each strategy and side by the keep rule |
| `scripts/test_connection.py`, `scripts/test_ntfy.py` | first-run checks: the demo keys and host, the phone push |
| `docs/strategies.md` | the rules each strategy trades, written for the trader agent |
| `tests/` | `test_cycle.py`: one bot cycle end to end against a fake Bybit (`fake_bybit.py`). `test_helpers.py`: pure signal and bracket helpers called directly. Standard `unittest`, no network |

## Facts verified against Bybit's v5 docs. Do not "fix" these.

**`/v5/order/create` has no `trailingStop` field.** A trailing stop can only
be set through `/v5/position/trading-stop`, against a position that already
exists. An entry is a market order with `stopLoss` and `takeProfit` attached;
a trail, when one is configured, follows as a second call to trading-stop
(`executor.applyTrailingStop`). A trailing failure is deliberately non-fatal
because the exchange-side stop loss is already live. No trail is configured
by default (see "Parameter choices").

**`trailingStop` is a price distance, not a percentage.** The docs say
"Trailing stop by price distance". Config takes a fraction and multiplies it by
the entry price. `activePrice` is the activation trigger; until price reaches
it the trail is dormant.

**Demo trading is not ccxt's sandbox.** `enable_demo_trading(True)` swaps
`urls['api']` for `urls['demotrading']` (`api-demo.bybit.com`). Testnet is a
different host with a different account, and ccxt refuses to combine them.
`exchange.assertDemoHost()` fails loudly if any URL is not the demo host.

**Leverage error 110043** ("not modified") is a no-op that fires on nearly
every run. It is swallowed; every other leverage error propagates.

**One-way mode**, `positionIdx = 0`. Not hedge mode.

## Design decisions with reasons

**The exchange is the only source of truth for positions.** No local state.
Every run asks Bybit what it holds. Anything cached would be a guess that opens
duplicate positions.

**Entries are events, exits are states.** An entry looks for an event - a
breakout's crossing, ict's retest of its gap - within the last
`SIGNAL_LOOKBACK_BARS` closed candles, because entering whenever a condition
still holds would re-enter forever. An exit reads the current indicator state,
because this bot polls every few minutes and misses most bars; an exit defined
as a single crossing bar would eventually be missed and strand a position.

**Closed candles only.** `signals.closedCandles()` drops the last row from
every ccxt OHLCV response. The final candle is still forming and its close is
just the current price, so a signal computed on it fires and un-fires inside
one bar.

**`orderLinkId` is deterministic per (symbol, strategy, time bucket).** A
retry inside one bucket reuses the id and Bybit rejects it, which is the
point: a retry after an ambiguous timeout must not open a second position. The
next cycle lands in a new bucket.

The bucket length is `ORDER_BUCKET_SECONDS`, which defaults to the loop
interval and is re-derived when `--interval` overrides it. These two must not
drift apart: a bucket longer than the interval blocks a cycle that
legitimately wants to retry until the bucket rolls over. It used to be a
hardcoded 300 seconds, which stopped matching the moment the interval moved
off 5 minutes.

**Quantity rounds down** to `qtyStep` and is refused below `minOrderQty`,
rather than quietly trading a size nobody asked for.

**`POSITION_NOTIONAL_USDT` is notional, not margin.** It is `qty * price`.
Margin used is roughly notional / leverage.

## Every closed position says why it closed

The `CLOSED` log line ends in `why=<cause>` and the phone push in a
`why: <cause>` line, so a loss can be read without a trip through Bybit's
order history. `main.closeCause()` decides it for each closed-pnl row, first
match wins:

| Evidence | Cause |
|---|---|
| `execType=BustTrade` on the closed-pnl row itself | `liquidation`, with no lookup |
| the closing order's `stopOrderType` is `StopLoss` / `TakeProfit` / `TrailingStop` | `stop loss` / `take profit` / `trailing stop` |
| its `orderLinkId` starts with `ORDER_LINK_PREFIX` and a dash | `bot exit` |
| anything else | `closed outside the bot (<createType>)`, no brackets when Bybit sends none |
| the lookup raised, or found no order | `unknown` |

The closing order is read from `/v5/order/history` by the `orderId` on the
closed-pnl row. These values were checked against real orders on the demo
account, not only the docs: the ARB stops of 2026-09-15 show
`stopOrderType=StopLoss` and `createType=CreateByStopLoss`; the bot's own
close has `orderLinkId` `cf-ARBUSDT-c-…`; the XRP liquidation of 2026-09-19
shows `execType=BustTrade` and `createType=CreateByTakeOver_PassThrough`; a
manual close in Bybit's interface shows `createType=CreateByClosing`.

The order of the checks matters. Liquidation is read off the row so the worst
outcome is named even when the lookup fails. The stop type is checked before
the prefix, so an exchange-side stop is never reported as a bot exit whatever
`orderLinkId` the triggered order carries. The prefix, not `createType`, is
what marks an order as the bot's: the bot tags every order it sends, and
`executor.isBotOrderLinkId()` sits beside the function that builds the tag so
the two cannot drift apart.

The lookup runs only for a close announced for the first time, after the
`state/notified.json` check, so a close that stays inside the lookback window
costs one request, not one per cycle. A failed lookup is logged and reported
as `unknown`; it never costs the push. Other exchange-side closes, such as
auto-deleveraging, are not special-cased and land in the last two rows.

`state/notified.json` keeps exactly the closes the latest read returned,
nothing older: a close that has left the window cannot be read again, so
forgetting it is free. It used to keep the last 500 ids in sorted order, but
Bybit's order ids are random, so past 500 it dropped fresh closes instead of
old ones, and each dropped close was pushed and looked up again every cycle
for as long as it stayed in the window.

## Why not GitHub Actions

Bybit fronts its API with CloudFront and geo-blocks the country GitHub's
runners sit in. Measured, not guessed: the runner reported `Azure Region:
eastus`, IP in Virginia, and all three Bybit hosts (`api-demo`, `api-testnet`,
`api.bybit.com`) returned 403 with "The Amazon CloudFront distribution is
configured to block access from your country" on a public, unauthenticated
endpoint. The region of a standard hosted runner cannot be chosen on a free
plan.

The `.github/workflows/` files still work for a machine in a country Bybit
serves. `geo-check.yml` re-measures this in 30 seconds. Do not attempt to route
around the block with a proxy or VPN: it violates Bybit's terms and risks the
account.

**The workflows are archived, not deleted.** `trade.yml` carried
`cron: "*/5 * * * *"` and `keepalive.yml` a weekly cron, both sitting on the
default branch, so GitHub kept starting the bot and kept getting 403. Every
scheduled `trade` run in the history is a failure. Both now have only
`workflow_dispatch:`. The files are kept because they work unchanged from a
country Bybit serves, and each carries a header explaining how to bring it
back.

Two things a future session must not get wrong here:

**A cron only fires from the default branch.** Removing `schedule:` on a
working branch changes nothing until it is merged to `main`. Do not report the
schedule as stopped before that.

**`trade.yml`'s `env:` block is deliberately stale.** It still passes the
retired `SMA_FAST_PERIOD` / `SMA_SLOW_PERIOD` and knows none of the settings
added in `4055141`. This was the owner's call: a loud warning in the file
rather than a list that would silently rot again. Reactivating the workflow
means updating that block against `.env.example` first - do not treat it as a
bug to quietly fix, and do not treat it as safe to run as-is.

`keepalive.yml` existed only to stop GitHub disabling `trade.yml`'s cron after
60 days of repository inactivity. With no cron to protect it protects nothing,
and it is the only workflow with `contents: write`. Reactivate it only after
`trade.yml` has a schedule again.

`smoke-test.yml` never had a schedule and is untouched, but its connection
step returns 403 from a GitHub runner like everything else; the ntfy step
still works. `geo-check.yml` is untouched and is the one workflow here that is
still straightforwardly useful.

## Running it

```
python run.py                 one cycle, then exit
python run.py --loop          cycle until Ctrl+C
python run.py --force-entry   one cycle that opens a test position
```

`AUTOSTART` in `.env` sets the default; flags override it. It is NOT an
operating-system autostart despite the name: nothing here registers with
Windows or macOS, so the bot does not come back by itself after a reboot.
Requires Python
3.10+ (ccxt's floor) and an active virtualenv, or `.env` is silently ignored
and the only symptom is "keys not set". `pip install -r requirements.txt`
also brings `tzdata`: Windows has no time zone database, and `ICT_KILL_ZONES`
reads New York time.

`dummy_mode` defaults to true: it ignores the market and enters only on
`--force-entry` or a GitHub `workflow_dispatch` run. `--force-entry` is
one-shot in loop mode on purpose.

`python run.py --kill-all` stops every other copy of the bot: Ctrl+C only
works if you still have the window. Two loops at once do not open duplicate
positions - the exchange is asked every cycle and the orderLinkId bucket
rejects the second order - but both write `state/owners.json`, so the record
of which strategy opened a position can be lost and the exit falls back to
`UNKNOWN_OWNER_EXIT`. `--loop` warns when another copy is already running.

**A process is identified by its executable, never by its command line
alone.** This was a real bug the first time `--kill-all` ran, not a
theoretical one: the shell executing `python run.py --kill-all` carries both
"run.py" and the project path in its own command line, so the command killed
the terminal it was typed into. `botProcesses()` now requires the executable
to be a Python interpreter, and excludes its own pid and its parent's. Do not
loosen that check - editors, terminals and task runners mention file paths
constantly, and only an interpreter actually runs one.

## The log is short unless asked

The owner found the per-symbol log unreadable in a loop, so by default
(`LOG_DETAIL=false`) a cycle prints its number and time, one `OPENED` line per
entry (instrument, side, size, notional, leverage, stop, target), one `CLOSED`
line per close with its cause, errors and warnings, and a summary ending in
the open count - "no entries, 3 open" when nothing happened. `main.say()` is
the always-printed channel, `main.log()` the detail one; every per-symbol
decision, the start banner and the connection check go through `log()`.

`LOG_DETAIL=true` brings back every symbol's decision and why - the way to
debug a strategy. A new line that someone must see to trust the bot (an
error, a failed stop, money moving) goes through `say()`; anything that only
explains a decision goes through `log()`. The test harness runs with the
detail on, and `ShortLog` pins the short form.

## Every setting is a knob. Do not hardcode one.

No period, threshold, multiplier or boolean is written into `signals.py`,
`executor.py` or `run.py`. Each reads `config.<name>`, and each of those reads
an environment variable of the same upper-case name. `.env.example` is the
complete control panel and documents every entry. A bare number inside a
strategy function is a bug; add the knob to `config.py` instead.

`config.validate()` refuses to run on an incoherent combination and
`config.warnings()` flags legal-but-probably-unintended ones. Retired setting
names live in `config.retired_settings` so a stale key in `.env` is reported
instead of silently ignored.

## Running several strategies at once

`STRATEGY=multi` evaluates every strategy in `ACTIVE_STRATEGIES` each cycle,
once for each side it may open, and enters when at least `MIN_ENTRY_VOTES`
agree on one side. Since the owner's decision of 2026-10-08 only `ict` is
live; `breakout` is in the code but switched off (see "Parameter
choices"). What follows is what makes several at once coherent, and it stays
load-bearing for the day a second strategy comes back:

**The regime filter is load-bearing.** The strategies trigger on different
things - `breakout` buys strength, `ict` buys a dip under a known low once the
structure has turned back up - and side by side without a filter they take
opposite views of one market. `REGIME_FILTER` allows long entries only above
the `REGIME_PERIOD` average and shorts only below it, so every strategy trades
with the slow trend, they differ only in the trigger, and a long and a short
never compete for one symbol. Turning it off is supported and is a different,
worse system. The filter gates ENTRIES; closing on a break of the same line is
a separate knob, `EXIT_ON_REGIME_BREAK`, and it is off (see "Parameter
choices").

**The strategy that opened a position owns its exit.** One-way mode holds one
position per symbol, so the owner is recorded in `state/owners.json` and
tagged into `orderLinkId` (visible in Bybit's own UI). Measured on the first
multi-strategy set (trend and meanrev, both since removed): on the same
uptrend the trend owner held while the meanrev owner exited, because for mean
reversion the bounce had already happened. Closing one strategy's trade on
another's rule ruins both at once. Since 3e8c699 an `ict` position is closed
by its exchange-side stop and target alone, and a `breakout` position, while
breakout is live, by its channel exit when `BREAKOUT_EXIT_LOOKBACK` is above
0.

`state/owners.json` is best effort and is NOT authoritative about whether a
position exists - the exchange still is. It answers a different question:
which rule should decide when to let go. Lose it and `UNKNOWN_OWNER_EXIT`
decides. Same status as `state/notified.json`.

**Strategies compete for symbols.** One position per symbol holds across
strategies, so a strategy holding a symbol blocks every other strategy on it,
on one clock as much as on several, and when several fire on the same bar the
first in `ACTIVE_STRATEGIES` owns the position. Measured in the replay of
`ict` and `breakout` together (#21, `--multi`): ict long fell from 111
trades to 15 in 90 days, because breakout long - by the trader's reading -
held most symbols most of the time.
`MAX_OPEN_PER_STRATEGY` (`name:count`) limits that. The trader's suggestion if
both go live again is `breakout:4`; it is not measured, because the replay
plays each symbol on its own, and ict long would still get only about 15-20
trades a month.

**Positions are read in one pass before any decision.** `MAX_OPEN_POSITIONS`
cannot be honoured while discovering positions symbol by symbol - the count
would only include symbols already visited. `main.run()` reads them all first,
then acts, keeping the count current as positions open and close.

**A position whose strategy is switched off has an unknown owner.** Under
`STRATEGY=multi` an owner counts only while it is in `ACTIVE_STRATEGIES`. A
breakout position still open when breakout is switched off (the owner's
decision of 2026-10-08) is therefore judged by `UNKNOWN_OWNER_EXIT=regime`:
no active strategy's rule closes it, and it is left to the stop and target it
carries on the exchange. It never takes its Donchian exit, whatever
`BREAKOUT_EXIT_LOOKBACK` says. Only the regime break could still close it,
and only if `EXIT_ON_REGIME_BREAK` were switched on, because breakout is not
`brackets_only`. That is by design: the switch-over must not close anything
for nothing but the fees, and the bracket is how breakout with no rule exit,
the variant that passed, manages a trade anyway. `GoLive` in
`tests/test_cycle.py` pins it. Its opening order carries the tag `b`, so it
stays out of ict's review; while it is held, ict cannot trade that symbol on
either side.

## Shorts are the long rule on a flipped chart

Every rule is written once, for longs. `signals.mirror()` turns the candles
upside down - every price negated, high and low swapped - and a short is the
long rule read on that chart: a close above the 20-bar high there is a close
below the 20-bar low on the real one, a gap up on it is a gap down on the real
one, the regime line flips with the price, and ATR is unchanged.
`signals.words()` tells the reason back the way the real chart reads. So
the two sides cannot drift apart, every setting means the same on both, and
there are no asymmetric defaults.

- **`SHORT_STRATEGIES`** names the strategies that may also open a short;
  empty means long only. It is `ict` (2026-10-08). breakout short lost in
  both halves of the replay and stays out, whether or not breakout is live.
  An unknown name is refused at startup; a name that is not live is warned
  about.
- **The regime gate splits the sides.** A short enters only below the line,
  where the long may not, so with the regime filter on at most one side of a
  strategy can fire on any bar. Votes count on one side only: the side of the
  highest-priority strategy that fired.
- **A held short is closed by the mirrored rule.** The side is never stored:
  `executor.positionSide()` reads it from the exchange (ccxt's `side`, falling
  back to Bybit's `Buy`/`Sell`), and `exitSignal` reads the owner's exit rule
  on that side's chart; with no live owner, `UNKNOWN_OWNER_EXIT` decides, on
  that chart too. Before 6e882ec a short opened by hand was judged by
  the long rules, and a breakout short would have been "closed" by a close
  under the 10-bar low, which is its best day.
- **One position per symbol: no flips.** A setup for the other side on a
  symbol already held is never acted on. It is logged as "holding long, short
  setup ignored" (with `LOG_DETAIL=true`), and only read when that side can
  open at all (`signals.sideCanOpen`), never in dummy mode.
- **The bracket is the mirror too.** The stop sits above the price and rounds
  up to the tick, the target below and rounds down, so neither is ever nearer
  the price than asked. The liquidation cap holds a short's stop within the
  same 3.33% at 15x, above the price.

The `OPENED` line, the open push and the start banner show the side.

## Stops and targets come from the setup

`executor.planEntry(spec, side, setup, price, atr)` is the one bracket
planner. It is pure - no client, no clock - so the live bot and the replay
plan an entry with the same code. `execute()` calls it before sizing and
before touching leverage. A refusal comes back as `refused=(code, reason)`;
every code but `price` is a risk decision, logged as a skipped entry, while
`price` makes `execute()` raise.

**Why.** The old strategies' exits read an indicator and ignored the entry
price. In the replay behind 37a7028 and #15 two closes in three were a
strategy's own exit rule, which usually closed a trade just under water, and
no entry was tied to a price level. A setup now names its own stop, where
it is wrong, and its own target, where price is drawn to.

- **`setup` is `None`: the ATR bracket.** breakout, and a forced test entry.
  The distances come from `RISK_MODEL` (`ATR_STOP_MULT` / `ATR_TARGET_MULT`,
  or the percentages when ATR is missing), then the liquidation cap and the
  minimum-stop check.
- **`setup` is a `signals.LevelSetup(bottom, top, structural, draws)`**: ict
  hands over the gap, the price the setup is wrong under, and the levels price
  is drawn to, all on the chart it read. `planLevels` judges it in order:
  1. `atr`: no ATR to measure with, no trade;
  2. `chase`: the live price must be above the gap's bottom and at most
     `ICT_MAX_CHASE_ATR` above its top;
  3. the stop: `ICT_STOP_BUFFER_ATR` under the structure, widened to at least
     `ICT_STOP_FLOOR_ATR` under the live price;
  4. the liquidation cap, as for every entry, and `capped-stop` when it pulls
     the stop inside the structure (`ICT_ALLOW_CAPPED_STOP=false`);
  5. `min-stop`, the shared minimum-stop check;
  6. the target: the nearest draw beyond the price is the target itself, and
     `reward-risk` refuses it when it is nearer than `ICT_MIN_RR` times the
     risk. With no draw at all the target is `ICT_FALLBACK_TARGET_R` times the
     risk.

  There is no trail on a level setup.
- **`price`** refuses a plan whose settings put a stop or target on the wrong
  side of the price: a misconfiguration, not a market view, so `execute()`
  raises and the symbol fails.

A level strategy is registered `brackets_only`: its stop sits where the setup
is wrong and its target was judged against it, so only the exchange closes the
trade - not a rule exit, and not the regime break either, live or not.

The liquidation cap and the minimum-stop refusal apply to every plan; see the
next section. `tests/test_helpers.py` covers every refusal code and the
rounding on each side.

## A stop beyond liquidation is not a stop

**The hardest-won fact in this repository.** Leverage puts liquidation roughly
`100/leverage` percent from entry — 6.67% at 15x. A stop further out than that
never fires: the exchange closes the position first, takes the whole margin,
and charges a liquidation fee on top. The risk management the strategy was
built around simply stops existing.

Caught live, not in theory. ARB opened with a 2×ATR stop 7.07% from entry
while `liqPrice` sat 5.72% away. The stop could not have worked.

Measured across the forty hourly-traded symbols of the time, 2×ATR ran from
**1.24% on BTC to 27% on the wildest alt** — a twentyfold spread. The ten
liquid symbols traded now, on 15-minute candles with a 3×ATR stop, still run
from a 0.78% median on BTC to 2.92% on NEAR, and NEAR's reached 11% in a
violent spell. No single `LEVERAGE` value covers that, which is why the fix is
per-trade rather than a smaller number:

- `MAX_STOP_FRACTION_OF_LIQUIDATION` (0.5) caps the stop at half the distance
  to liquidation. `executor.capStopAtLiquidation()` applies it and the cap is
  logged whenever it bites.
- `MIN_STOP_ATR_MULT` (1.0) then **refuses the trade** when the cap has
  squeezed the stop below one ATR. A stop inside normal bar-to-bar movement is
  not protection, it is a scheduled exit the next candle triggers by accident.
  On a symbol whose ATR is 13% of price there is no stop that both fits inside
  a 15x liquidation and means anything.

Do not "simplify" either of these away, and do not raise `LEVERAGE` without
re-measuring ATR across the whole symbol list. The 30x replay in "Parameter
choices" shows what a higher leverage does to the refusals.

## Stops are measured from the live price

**Measured live, not theorised.** Signals read closed candles, and the stop
used to be measured from the last one's close as well - a price up to a whole
bar old. On 2026-09-15 ARB's stop was measured from a close of 0.14991 and
came out at 0.14491, 0.005 (3.3%) below it. The first fill was 0.14703: the
market had already fallen 1.9% since that close, so the stop sat 0.00212
(1.4%) under the fill and was hit three minutes later. The re-entries that
followed filled ever closer to the same stop, the last one on it exactly (the
sequence is in "One signal, one trade"), and then Bybit rejected the orders
outright ("StopLoss ... should lower than base_price").

So `executor.execute()` reads the ticker's last trade right before sizing,
and the size, stop, target, liquidation cap and minimum-stop check - and for a
level setup the chase limit - are all measured from it. The fill price itself
cannot be used: SL/TP ride on the order and must be known before it exists,
which is what keeps a position from ever being naked. The last trade is the
nearest honest stand-in. The candle close is still passed in, and only logged.

Signals and ATR still come from closed candles only. This moves where the
stop is measured from, not how signals are computed.

**No price, no trade.** A ticker without a usable last price (missing, zero,
not a number) skips the entry with a logged reason. There is deliberately no
fallback to the candle close: that close is exactly the stale price this
replaced. The skip is not a failed run; the next cycle asks again. A ticker
request that raises is a different thing and fails the symbol like any other
exchange call that raises.

The entry log line prints both prices and the move between them,
`@~0.147030 live, last close 0.149910 (-1.92%)`, so a stale signal shows.
`tests/test_cycle.py` replays the ARB numbers: measured from 0.14991 at 15x
the capped stop is exactly the 0.14491 ARB was sent with; measured from
0.14703 it is 0.14212.

## One signal, one trade

**A signal stays live for `SIGNAL_LOOKBACK_BARS` candles, so without a
cooldown a failed trade is bought again on the next cycle.** Entries are
events inside a window (see "Entries are events, exits are states"), and one
position per symbol stops two positions at once, not four in a row. Measured
on ARB on 2026-09-15, one signal of the since-removed trend strategy (`-t-`),
the same stop 0.14491 each time:

| Entry | Fill | Stopped out |
|---|---|---|
| 20:13:29 | 0.14703 | 20:16:27 |
| 20:18:24 | 0.14507 | 6 s later |
| 20:20:23 | 0.14555 | 10 s later |
| 20:22:25 | 0.14491 | same second |

After the fourth, Bybit rejected the orders outright.

`REENTRY_COOLDOWN_BARS=3` keeps a symbol out for that many candles after any
close on it, whatever closed it: stop, target, the bot's own exit, a manual
close. Three matches `SIGNAL_LOOKBACK_BARS`, so by the time the symbol may
enter again the signal behind the last trade has left the window. In the
26-day replay of 2026-09-24 it was worth about 5 points in the falling half
and 35 in the rising one (see "Parameter choices"), and `scripts/replay.py`
plays it as the live bot does.

- **Candles of the strategy that wants in.** The wait is counted on the
  entering strategy's own timeframe: 45 minutes for a 15m rule, three hours
  for `breakout:1h`.
- **Bybit's close history, never a local file.** Each cycle reads
  `/v5/position/closed-pnl` once, takes the newest `updatedTime` per market id
  (`ETHUSDT`, mapped from the ccxt symbol), and the same rows feed the close
  report. A restart or a second copy of the bot cannot lose a close.
- **The read window follows the cooldown.** It is the larger of
  `CLOSED_LOOKBACK_MINUTES` and the cooldown on the slowest active timeframe,
  and the `closed-position check` log line states that window, not the
  setting. Given only `startTime`, one request covers at most seven days
  (Bybit v5 docs), so a cooldown longer than a week would read the oldest week
  and miss the newest closes. Nothing guards against that yet.
- **Checked after the signal and the position caps.** A held-back entry logs
  `re-entry cooldown, N minute(s) left` followed by the signal itself, so the
  strategy can still be judged.
- **An unreadable history trades without it for one cycle**, logged as
  `re-entry cooldown unavailable this cycle`. One failed request is not worth
  stopping the bot for; the cost is one cycle in which a symbol that just
  closed can be bought again.

## One clock: 15-minute candles

The owner asked for a fast bot: many entries a day on 15-minute candles, not a
swing book waiting days for an hourly signal. So every strategy reads the
same 15-minute candles by default - `ENTRY_TIMEFRAME=15m`, with
`STRATEGY_TIMEFRAMES` and `MAX_OPEN_PER_STRATEGY` both empty. One timeframe
also means one candle request per symbol per cycle. Longer context comes from
the same candles: ict's previous-UTC-day high and low are built from 15-minute
bars, which is why it asks for a whole previous day plus today.

**Splitting the clocks is still supported, and brings a rule with it.**
`STRATEGY_TIMEFRAMES` gives a strategy its own candle size (say
`breakout:1h`), so the bot can hold a swing book and a fast book at once, in
the same cycle, against the same account. The moment the clocks differ,
`MAX_OPEN_PER_STRATEGY` becomes load-bearing: a 15-minute rule fires many
times more often than an hourly one and takes every free symbol and slot
before the slow rule reaches one, so "fast AND daily" quietly becomes "fast
only". That happened on the old two-clock setup and is why the cap exists.
The replay plays one clock and refuses to run with `STRATEGY_TIMEFRAMES` set.

The regime filter is applied **per strategy on its own timeframe**, not once
globally. On 15-minute bars the 200-period line is about two days of trend; on
hourly bars about eight. A fast rule has no business being blocked by an
eight-day view, and a swing rule has no business being let in by a two-day one.

`signals.requiredTimeframes()` returns one entry per DISTINCT timeframe, so
strategies sharing a chart cost one request, not one each.

## The symbol list was measured, then filtered by hand

The ten crypto perpetuals with the highest 24h turnover, read straight from
the exchange on 2026-09-24: BTC, ETH, XRP, SOL, ZEC, NEAR, HYPE, DOGE,
1000PEPE, BCH. Ten rather than the forty traded before: on 15-minute candles
ten symbols gave the four strategies of the time about 25 entries a day, and
the most liquid markets are where a market order fills closest to the price
its stop was measured from. The ranking moves day to day; re-measure it rather
than trusting the list forever.

Bybit lists 762 USDT perpetuals, and fetching all of them is not an option:
one cycle would take about 14 minutes, nearly a whole 15-minute candle, so
every cycle would act on a bar that is already gone.

The raw ranking by volume **is not all crypto**. It includes tokenized
equities and commodities — SOXL, crude (CL) and gold (XAU) sat in the top
twelve on 2026-09-24; AAPL, TSLA, MSTR, SKHYNIX and XAG have appeared before —
which follow a different clock and a different logic than anything these
strategies were built for. Nothing in the API metadata distinguishes them:
`contractType` is `LinearPerpetual` for all of them, `fetch_currencies()`
returns nothing on the demo host, and they trade 24/7 so a dead-candle test
does not separate them either. They were excluded by name, and anything whose
identity was uncertain was left out rather than guessed at.

## One bot can look like two processes

On Windows a virtualenv `python.exe` is a stub that launches the real
interpreter as its child, so a single `run.py --loop` appears **twice** in the
process table with an identical command line. Counting raw processes reports
two bots where there is one, which sent this session hunting for a duplicate
that did not exist.

`run.botRoots()` drops any process whose parent is also a bot process and is
what `--kill-all` and the duplicate warning count. `taskkill` is called with
`/T` so the tree goes together.

A genuine second instance is still a real problem, and it happened here: a
`--loop` running in one terminal alongside a manual `--once` in another both
wrote `state/owners.json`, the later write won, and the record of which
strategy opened a position was lost. The exits then fell back to
`UNKNOWN_OWNER_EXIT`. Trading stayed correct — the exchange is still the only
source of truth for what is held — but the strategies were judged by the wrong
rules.

**A running loop does not pick up edits.** `config.py` and every module are
imported once when the process starts, so changing `.env` or the code means
restarting the loop. `--kill-all` then a fresh `run.py --loop`.

## A strategy is a removable block

Each strategy is one self-contained block, so a strategy that fails can be
deleted cleanly rather than left switched off. A block is:

- its entry rule, exit rule and candle budget in `signals.py`, under its own
  banner, and one line in the `signals.strategies` registry:
  `Strategy(entry, exit, candles, brackets_only)`;
- its name in `config.strategy_names`, its settings in `config.py` with their
  validation and warnings, and its entry in `.env.example`;
- its section in `docs/strategies.md`, and its tests;
- its letter in the order tag (see the next section).

Priority between strategies is not part of a block: it is the order of
`ACTIVE_STRATEGIES`. Helpers another block still uses belong to that block:
every level helper - swings, sweeps, gaps, the retest, the stop reference,
the draws - is ict's.

**Removing one** deletes the block and its registry line, its indicators if
nothing else reads them, its control-panel entry, docs section and tests. Its
settings move to `config.retired_settings`, so a stale line in `.env` warns
instead of silently doing nothing; its name leaves `config.strategy_names`, so
the name in `ACTIVE_STRATEGIES` is refused at startup; its tag letter goes
into `executor.reserved_tags`. Done three times so far, each in its own commit:

- **988f72f** removed meanrev and scalp, with RSI and the Bollinger bands. It
  also introduced the registry.
- **9cb1a22** removed pullback. The level helpers it shared stayed, as ict's.
- **0e444ee** removed trend, with the EMA and ADX. The regime SMA and ATR
  stayed: ict, breakout and the regime filter need them.

The keep rule (#15): a strategy that fails the replay is deleted; a side that
fails is left out of `SHORT_STRATEGIES`. ict is exempt through its first
review. breakout is the one strategy kept but switched off, because its long
side passed one variant of the replay (see "Parameter choices").

## The order tag names the strategy

`executor.buildOrderLinkId()` builds
`<ORDER_LINK_PREFIX>-<market id>-<tag>-<bucket>`, for example
`cf-ETHUSDT-i-…`. The tag is one letter, `strategyTag()`: the strategy name's
first letter, lower case. A short carries the same tag as its long. Bybit
shows `orderLinkId` in its own interface, and the tag on the opening order is
what lets a close be attributed to its strategy from Bybit's data alone.

| Letter | Meaning |
|---|---|
| `i` | ict |
| `b` | breakout |
| `c` | the bot's own close (`closePosition`) |
| `x` | an order with no strategy |
| `p` | pullback, removed; never reused |
| `t` | trend, removed; never reused |

`c`, `x`, `p` and `t` are `executor.reserved_tags`. A removed strategy's
letter is never reused, so an old order id in Bybit's history is never read as
a newer strategy's. `OrderTags` in `tests/test_helpers.py` fails when two
strategies share a letter or one takes a reserved letter, so a new strategy
needs a name whose first letter is free. meanrev (`m`) and scalp (`s`) placed
orders before `reserved_tags` existed and are not in it yet, so nothing stops
a new name from taking those letters; avoid them.

## The replay and its keep rule

`scripts/replay.py` plays past 15-minute candles through the bot's own code:
each closed bar goes to `signals.entrySignal` / `signals.exitSignal` through
the window the live bot uses, and every bracket comes from
`executor.planEntry`. It cannot drift from the bot; what it adds is what
happened next.

```
python scripts/replay.py                       every strategy and side alone, 90 days
python scripts/replay.py --sides both          each strategy on both sides at once
python scripts/replay.py --multi               ACTIVE_STRATEGIES and SHORT_STRATEGIES together, as live
python scripts/replay.py --set ICT_MIN_RR=2    one change against the baseline
python scripts/replay.py --wave 1 --jobs 4     the predefined variants
python scripts/replay.py --days 26 --end 2026-09-24
```

- **A bar:** what the previous close decided fills at this bar's open; then
  the stop and target are checked against this bar's range, the stop first
  when one bar touches both, and a bar that opens beyond a level fills at the
  open; then the bot decides at this close, seeing nothing later.
- **Costs:** fee 0.055% and slippage 0.02% per fill, slippage always against
  the trade. The re-entry cooldown is played as live.
- **Candles** are cached under `state/candles/` (gitignored); `--offline`
  uses the cache alone.
- **It refuses** to run with a trail set or with `STRATEGY_TIMEFRAMES`, and on
  anything `config.validate()` refuses.
- **Not modelled:** symbols are played one by one, so `MAX_OPEN_POSITIONS`
  and `MAX_OPEN_PER_STRATEGY` never bind (`--multi` does show strategies
  blocking each other within a symbol); funding; fills worse than the
  slippage in a violent bar. Refusals are counted per bar, so one setup can
  be refused up to three times.

**The keep rule.** Trades are split at the window's calendar midpoint, by
entry time. A row passes when its sum % is positive in one half and not
negative in the other. A variant is kept only when it improves the baseline
in both halves. One good half is a market, not an edge.

**The trader's rules for reading it:**

- **Judge per side.** An `ICT_` setting is kept only when it helps ict long
  and ict short, each in both halves, never summed across sides: a short is
  the mirror of the long, so a real improvement shows on both, and in a
  one-directional window anything that cuts the losing side looks good. A
  setting both strategies read (`REGIME_PERIOD`, `REGIME_FILTER`,
  `SIGNAL_LOOKBACK_BARS`, `ATR_PERIOD`, `REENTRY_COOLDOWN_BARS`,
  `MIN_STOP_ATR_MULT`, `MAX_STOP_FRACTION_OF_LIQUIDATION`, `LEVERAGE`,
  `ENTRY_TIMEFRAME`) must in addition make breakout long worse in neither
  half. A breakout-only setting (`BREAKOUT_*`, `ATR_STOP_MULT`,
  `ATR_TARGET_MULT`) is judged on breakout long alone.
- **Read average R beside sum %.** A pass in sum % with average R at or under
  0 is a pass of the symbol mix, not of the signal, and counts as thin.
- **Compare with random removal.** Most "helps both halves" rows of #21 only
  cut trades from a losing row. Removing trades at random would change a half
  by about -(trades removed / baseline trades) × baseline sum %; a variant
  counts only when its change beats that in both halves. The replay does not
  compute that benchmark yet.
- **A pass in two rising halves means only "worked in two rising markets".**
  Both halves of the #21 window rose (the ten symbols averaged +31% and +33%),
  so no verdict so far has faced a falling market.
- **Do not tune ict on a 90-day window.**

**Always set `LEVERAGE` and `POSITION_NOTIONAL_USDT` explicitly** for a
replay, from `.env` or the environment. `config.py` falls back to 1x and
1000 USDT when neither is set, and one run silently used 1x: its whole table
was wrong. The header line prints the notional and leverage in
use; read it before reading anything else.

The replay has no tests of its own. It is checked by reproducing earlier
runs: when it moved to `planEntry` (6e882ec) it reproduced the previous
breakout and trend numbers exactly on 25 cached days.

## Parameter choices, and why they are what they are

The owner originally reserved these decisions and later handed them over,
asking first for simple textbook settings, then for more aggressive ones that
still deserve trust, then for a fast bot - many entries a day on 15-minute
candles, ten symbols. He does not know trading and wants these decisions made
and explained, not put to them; the trading rules come from the trader agent
and the replay decides what trades. Nothing here is a demonstrated edge.

**The current evidence: the 90-day replay of #21.** 2026-07-09 to 2026-10-07
UTC, the ten symbols, 15-minute candles, fee 0.055% and slippage 0.02% per
fill, 450 USDT notional at 15x, code at 651417a, each strategy and side
alone. "Sum %" adds up each trade's return on its notional (USDT = sum % ×
4.5). Both halves rose. Raw output and the trader's reading are on #15 and in
`Claude_Code\Claude-Finance\replays\2026-10-07\`.

| Strategy | Side | 1st half sum % | 2nd half sum % | Trades | Verdict |
|---|---|---|---|---|---|
| ict | long | -0.15 | -17.74 | 62 / 49 | fail |
| ict | short | -12.27 | -39.59 | 52 / 51 | fail |
| pullback | long | +14.37 | -18.69 | 60 / 64 | fail |
| pullback | short | -7.56 | +0.17 | 40 / 41 | fail |
| trend | long | -71.40 | +7.68 | 147 / 131 | fail |
| trend | short | -53.15 | -47.30 | 143 / 163 | fail |
| breakout (10-bar exit, the default then) | long | +15.23 | -60.89 | 572 / 596 | fail |
| breakout (10-bar exit, the default then) | short | -95.57 | -221.94 | 494 / 508 | fail |
| breakout, `BREAKOUT_EXIT_LOOKBACK=0` (the default since 3e8c699) | long | +69.86 | +13.85 | 403 / 439 | **pass** |

Every baseline row failed; trading both sides of a strategy at once changed
no verdict. Of the eleven wave-1 variants one row passed, breakout long with
no rule exit. Run together as the trader first proposed (`--multi`, ict and
breakout with exit 0), breakout long passed again (+64.44 / +15.83) but ict
long fell to 15 trades.

**The owner's verdict, 2026-10-08, on the trader's revised recommendation:**

- **ict goes live on both sides, every `ICT_` setting at its default**, as an
  experiment, not for profit, through its first review: about 100 trades per
  side. Alone it makes about 37 longs and 34 shorts a month, so about three
  months, at an expected cost of about 100 USDT of demo money a month if the
  replay holds, most of it on the short side.
- **pullback and trend are deleted** (-50 and -542 USDT over the 90 days, both
  sides together).
- **breakout stays in the code, switched off, with `BREAKOUT_EXIT_LOOKBACK=0`;
  its short side stays out.** The pass is thin. Per unit of risk it was
  slightly negative in both halves (average R -0.00 / -0.05); the profit in
  sum % comes from the few volatile symbols with the widest stops. ZEC, PEPE
  and NEAR made +89.9 points, more than the whole +83.7; the other seven
  together lost 6.2, DOGE alone -28.9. In each half its worst drawdown was
  bigger than that half's profit: about 419 USDT against +314, then about 491
  against +62. Beside ict it took the symbols ict needed. It comes back after
  the ict review, or sooner if a 365-day replay with a falling half confirms
  it.
- **No `ICT_` variant and no `REGIME_PERIOD` variant was adopted:** none
  helped both ict sides in both halves.

**`BREAKOUT_EXIT_LOOKBACK=0`.** No rule exit; the position is left to its
exchange-side stop and target. It cut trades by about 28% (572 / 596 to
403 / 439), as positions are held longer, but the cut explains little:
removing that many trades at random would have changed the halves by about
-4.5 / +16.0 points, against +54.6 / +74.7 measured. The gain is in the
payoff of the trades kept. It ran in order - a 10-bar exit worst, 20 better,
none best. A 10-bar exit on 15-minute candles is 2.5 hours of noise, not the
Turtle's days. With a rule exit (above 0), keep the exit lookback shorter than
`BREAKOUT_LOOKBACK=20`, the original Turtle asymmetry; `config.warnings()`
flags one longer than the entry's.

**`REGIME_PERIOD=200` on `ENTRY_TIMEFRAME=15m`.** About two days of trend.
Wave 1 tried 400 and 800 and neither helped both ict sides in both halves.
Earlier settings ran 1h and before that 4h; the owner found both far too
slow.

**`ICT_*` at the trader's defaults.** The values and their tuning ranges are
in `.env.example` and the rules in `docs/strategies.md`. Kill zones are off
until a replay shows the session filter helps.

**`EXIT_ON_REGIME_BREAK=false`.** The regime filter still blocks entries on
the wrong side of the line. History: in the 26-day replay of 2026-09-24
closing on a break helped in neither half, because the dip-buying strategies
of the time bought exactly what sags toward that line, and the exit threw out
trades just before they worked. An ict position is exempt from the break in
any case (see "Stops and targets come from the setup").

**`UNKNOWN_OWNER_EXIT=regime`.** With `any`, one of the exit rules nearly
always said close, so a position whose strategy was no longer active was sold
within a cycle for nothing but the fees (37a7028).

**`EXIT_TIMEFRAME` is unset on purpose.** It inherits `ENTRY_TIMEFRAME`. An
exit rule uses the same periods as its entry, so a shorter timeframe is not a
symmetric exit but a far noisier one. Measured live on an old MA pair: it
read 258 hours of history on 1h and 4 hours on 1m, a 65x difference.

**`RISK_MODEL=atr`, stop 3 ATR, target 6 ATR.** The bracket for an entry with
no setup: breakout, and a forced test entry. Measured live in one instant:
ATR14 was 0.48% of price on BTC and 1.02% on XRP, so a flat percentage is too
tight on one market and too wide on another, and the market picks which.
Falls back to the percentage model when ATR cannot be computed. Two ATR is a
daily-chart stop; in the 26-day replay of 2026-09-24, 3/6 beat 2/4 in both
halves. On these ten symbols the median 3×ATR stop is 0.78% (BTC) to 2.92%
(NEAR), inside the 3.33% liquidation cap at 15x.

**No trailing stop: `ATR_TRAIL_MULT=0`, `TRAILING_STOP_PCT=0`** (37a7028). A
trail armed at +3 ATR with a 1.5 ATR distance locks in only +1.5 ATR, and a
1.5 ATR pullback is ordinary on 15-minute candles, so it closed winners short
of the 6 ATR target (most of them, by 37a7028's reading); the replay that
chose the 3/6 bracket never modelled the trail. ict never trails, and the
replay refuses to run with a trail. The code to set one is kept, and so is
`validate()`'s refusal of an activation under the distance: Bybit puts the
trail's first trigger at (activation - distance), and reversed it lands below
entry - observed live at 1% activation with a 1.5% distance, the trail sat
0.49% under entry.

**Size: `LEVERAGE=15`, `POSITION_NOTIONAL_USDT=450`, `MAX_OPEN_POSITIONS=10`**
(owner, 2026-10-08). Leverage does not change position size, only how little
margin backs it - 30 USDT per 450 USDT position - and how close liquidation
sits: about 6.67% away, which caps every stop at 3.33%. One position per
symbol and ten symbols, so at most 4,500 USDT of notional is ever at work, 9%
of the 50,000 demo balance.

**30x was tried and rejected.** The owner first proposed 30x and positions of
900 or 1000 USDT, then withdrew both after the trader's reading. A replay of
the same 90 days at 30x (900 USDT, the same 30 USDT of margin) passed nothing
new, and the liquidation cap, halved to 1.67%, squeezed the stops: ict's
capped-stop refusals went from 6 to 34 on the long side and from 2 to 48 on
the short side, and breakout long's min-stop refusals, with its 10-bar exit,
from 0 to 84 (86 with exit 0). ict short lost less at 30x (-8.10 / -3.64
against -12.27 / -39.59) because the cap refused its widest-stop setups, but
ict long lost more in the first half (-3.95 against -0.15), so by the per-side
rule it is no gain; a stop-width filter, if wanted, belongs in its own
setting, not in the leverage. breakout long with exit 0 passed again at 30x
(+67.79 / +6.24). The trader's 300 USDT account simulation (100 USDT
positions, five at most, isolated margin, an account loss limit) was not
adopted either.

These two live in `.env.example` and `.env`. `config.py` deliberately falls
back to the safer 1000 USDT at `LEVERAGE=1` when neither is set, so the
figures above hold only with the control panel in place - and a replay
without them measures a different bot (see "The replay and its keep rule").

**Ten symbols.** More symbols is the most effective way to get more trades
without loosening a single rule, and those trades are far less correlated
than the extra ones a lower threshold on one market would produce. Measured
on the strategies of the time: 3 trades in 41 days on the old single-symbol
setup, 274 in 31 days on nine hourly symbols, about 25 a day on ten
15-minute ones.

**History: the 26-day replay of 2026-09-24.** Before the rework the four
strategies of the time were tuned on 25.8 days to 2026-09-24, long only, with
fees but no slippage; its first half fell. It chose the 3-bar re-entry
cooldown, the regime-break exit off and the 3/6 ATR bracket, each because it
helped both halves, and they stand, though #21 re-tested none of them. Of
the three only the cooldown touches ict, which has no ATR bracket and is
exempt from the regime break; `EXIT_ON_REGIME_BREAK=false` was chosen for
dip-buyers and has not been tested on breakout. It is not the evidence for
what trades now: three of its four strategies are deleted, and it never
modelled the trail. On that window the new replay later found breakout alone,
with its 10-bar exit, failing too: -29% in the first half and +68% in the
second (d897138, reported on #15).

## Tests

```
python -m unittest discover -s tests     from the repo root, no network
```

**Behaviour goes through one door.** The cycle tests (`tests/test_cycle.py`)
run a whole cycle via `main.main()` against `tests/fake_bybit.py` and assert
on what reaches the exchange, the log and the phone - never on a helper or a
state file. `runCycle()` pins every setting that shapes a cycle, because the
owner's `.env` is already loaded into `config` when a test imports it; a test
that relies on a particular value passes it explicitly. It pins the ntfy
topic too, and blanks the retired settings unless its `environ=` argument
names them, so the owner's `.env` cannot put a warning into every test. When a
cycle starts asking Bybit something new, teach the fake to answer it rather
than patching around it.

**A fresh checkout is tested as it ships.** `tests/fake_bybit.py` also
holds two settings helpers. `settingsFrom(environ)` reads
every pinned setting the way `config.py` parses it from that environment
alone, with no `.env`: `settingsFrom({})` is the code's own defaults, and
`settingsFrom(controlPanel())` is `.env.example` as it would read once copied
to `.env`. The `GoLive` tests run whole cycles on both, so the defaults and
the control panel cannot drift from the live set.

**Pure helpers are also tested directly.** Pure signal and bracket helpers -
`mirror` / `oriented` / `words`, `planEntry`, `positionSide`, the order tags,
and ict's level helpers - have unit tests in `tests/test_helpers.py`. Those
check a function's contract: its rounding on each side, every refusal code,
the edge a whole cycle would need a contrived market to reach. Keep them
apart from the cycle tests, and keep wiring (what a cycle does with the
answer) in the cycle tests. A helper that touches a client, the clock or a
file is not pure and does not belong in that file.

## Conventions

Variables lower case, functions camelCase. Secrets never printed: `notify.py`
scrubs the ntfy topic out of anything heading for a log, because `requests`
puts the full URL into its exception messages.

`.gitignore` masks `.env.*` as well as `.env`, negating `.env.example` back
in. Plain `.env` does not cover `.env.backup` or `.env.local`, and this
repository is public.

## Agent skills

### Issue tracker

Issues and specs are GitHub issues in vinamon/Claude-Finance. A bare number, as in `implement 12`, means issue #12. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root, created only when a term or decision is settled. See `docs/agents/domain.md`.
