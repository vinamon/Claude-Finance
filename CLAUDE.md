# Claude-Finance

Trading bot on a **Bybit Demo Trading** account. Python, ccxt, virtual funds
only. No path to a real account exists or should be added.

Run it from a laptop, not CI. See "Why not GitHub Actions" below.

## Layout

| File | Role |
|---|---|
| `config.py` | every knob, from `.env` / env vars, plus `validate()` and `warnings()` |
| `signals.py` | indicators, the regime filter, four strategies, voting. Pure functions, no orders |
| `executor.py` | decisions to orders. No strategy logic |
| `exchange.py` | ccxt client pinned to the demo host |
| `notify.py` | ntfy.sh push |
| `main.py` | one pass: closed-position reports, exits, entries |
| `run.py` | laptop runner, the loop/once switch |
| `tests/` | one bot cycle end to end against a fake Bybit (`fake_bybit.py`). Standard `unittest`, no network |

## Facts verified against Bybit's v5 docs. Do not "fix" these.

**`/v5/order/create` has no `trailingStop` field.** A trailing stop can only
be set through `/v5/position/trading-stop`, against a position that already
exists. An entry is therefore two calls: market buy with `stopLoss` and
`takeProfit` attached, then trading-stop for the trail. A trailing failure is
deliberately non-fatal because the exchange-side stop loss is already live.

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

**Entries are events, exits are states.** An entry looks for a crossing within
the last `SIGNAL_LOOKBACK_BARS` closed candles, because entering whenever a
condition still holds would re-enter forever. An exit reads the current
indicator state, because this bot polls every few minutes and misses most bars;
an exit defined as a single crossing bar would eventually be missed and strand
a position.

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

The "position closed" log line ends in `why=<cause>` and the phone push in a
`why: <cause>` line, so a loss can be read without a trip through Bybit's
order history. `main.closeCause()` decides it for each closed-pnl row, first
match wins:

| Evidence | Cause |
|---|---|
| `execType=BustTrade` on the closed-pnl row itself | `liquidation`, with no lookup |
| the closing order's `stopOrderType` is `StopLoss` / `TakeProfit` / `TrailingStop` | `stop loss` / `take profit` / `trailing stop` |
| its `orderLinkId` starts with `ORDER_LINK_PREFIX` and a dash | `bot exit` |
| anything else | `closed outside the bot (<createType>)` |
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
what marks an order as the bot's: the bot tags every order it sends.

The lookup runs only for a close announced for the first time, after the
`state/notified.json` check, so a close that stays inside the lookback window
costs one request, not one per cycle. A failed lookup is logged and reported
as `unknown`; it never costs the push. Other exchange-side closes, such as
auto-deleveraging, are not special-cased and land in the last two rows.

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
and the only symptom is "keys not set".

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

`STRATEGY=multi` evaluates every strategy in `ACTIVE_STRATEGIES` each cycle
and enters when at least `MIN_ENTRY_VOTES` agree. Three things make that
coherent rather than self-defeating:

**The regime filter is load-bearing.** Trend following buys strength, mean
reversion buys weakness; run side by side without a filter one strategy's
entry is the other's exit. `REGIME_FILTER` allows long entries only above the
`REGIME_PERIOD` average, which turns mean reversion into "buy the dip inside
an uptrend" - the documented version - so they all pull the same way and
differ only in the trigger. Turning it off is supported and is a different,
worse system. The filter gates ENTRIES; closing on a break of the same line
is a separate knob, `EXIT_ON_REGIME_BREAK`, and it is off (see "Parameter
choices").

**The strategy that opened a position owns its exit.** One-way mode holds one
position per symbol, so the owner is recorded in `state/owners.json` and
tagged into `orderLinkId` (visible in Bybit's own UI). Measured on the same
uptrend: a `trend` owner holds while a `meanrev` owner exits, because for
mean reversion the bounce has already happened. Closing a mean-reversion trade
on a trend-following rule ruins both strategies at once.

`state/owners.json` is best effort and is NOT authoritative about whether a
position exists - the exchange still is. It answers a different question:
which rule should decide when to let go. Lose it and `UNKNOWN_OWNER_EXIT`
decides. Same status as `state/notified.json`.

**Positions are read in one pass before any decision.** `MAX_OPEN_POSITIONS`
cannot be honoured while discovering positions symbol by symbol - the count
would only include symbols already visited. `main.run()` reads them all first,
then acts, keeping the count current as positions open and close.

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
re-measuring ATR across the whole symbol list.

## Stops are measured from the live price

**Measured live, not theorised.** Signals read closed candles, and the stop
used to be measured from the last one's close as well - a price up to a whole
bar old. On 2026-09-15 ARB's stop was measured from a close of 0.14991 and
came out at 0.14491, 0.005 (3.3%) below it. The first fill was 0.14703: the
market had already fallen 1.9% since that close, so the stop sat 0.00212
(1.4%) under the fill and was hit three minutes later. Three more entries
followed within nine minutes with the same stop, filling 0.00016, 0.00064
and 0 above it; the last, at 0.14491, sat on the stop itself and was stopped
out the same second. Then Bybit rejected the orders outright ("StopLoss ...
should lower than base_price").

So `executor.execute()` reads the ticker's last trade right before sizing,
and the size, stop, target, trail, liquidation cap and minimum-stop check are
all measured from it. The fill price itself cannot be used: SL/TP ride on the
order and must be known before it exists, which is what keeps a position from
ever being naked. The last trade is the nearest honest stand-in. The candle
close is still passed in, and only logged.

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

## Four strategies on one clock

The owner asked for a fast bot: many entries a day on 15-minute candles, not a
swing book waiting days for an hourly signal. So all four strategies read the
same 15-minute candles by default - `ENTRY_TIMEFRAME=15m`, with
`STRATEGY_TIMEFRAMES` and `MAX_OPEN_PER_STRATEGY` both empty. One timeframe
also means one candle request per symbol per cycle.

`scalp` is Bollinger Band reversion: buy a close stretched below the lower
band, let go when it reverts to the middle. It earns its place because
standard deviation measures something none of the others do — how far the
current move sits outside normal variation for that market — which is why the
same rule works on BTC and on a memecoin without retuning.

**Splitting the clocks is still supported, and brings a rule with it.**
`STRATEGY_TIMEFRAMES` gives a strategy its own candle size (say `trend:1h`),
so the bot can hold a swing book and a scalping book at once, in the same
cycle, against the same account. The moment the clocks differ,
`MAX_OPEN_PER_STRATEGY` becomes load-bearing: a 15-minute rule fires many
times more often than an hourly one and takes every position slot before the
slow rule reaches one, so "fast AND daily" quietly becomes "fast only". That
happened on the old two-clock setup and is why the cap exists.

The regime filter is applied **per strategy on its own timeframe**, not once
globally. On 15-minute bars the 200-period line is about two days of trend; on
hourly bars about eight. A scalper has no business being blocked by an
eight-day view, and a swing rule has no business being let in by a two-day one.

`signals.requiredTimeframes()` returns one entry per DISTINCT timeframe, so
strategies sharing a chart cost one request, not one each.

## The symbol list was measured, then filtered by hand

The ten crypto perpetuals with the highest 24h turnover, read straight from
the exchange on 2026-09-24: BTC, ETH, XRP, SOL, ZEC, NEAR, HYPE, DOGE,
1000PEPE, BCH. Ten rather than the forty traded before: on 15-minute candles
ten symbols already give about 25 entries a day, and the most liquid markets
are where a market order fills closest to the price its stop was measured
from. The ranking moves day to day; re-measure it rather than trusting the
list forever.

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

## Parameter choices, and why they are what they are

The owner originally reserved these decisions and later handed them over,
asking for simple textbook settings based on well-known signals, later for
more aggressive ones that still deserve trust, and most recently for a fast
bot - many entries a day on 15-minute candles, ten symbols, nothing
complicated. The owner does not know trading and wants these decisions made
and explained, not put to them. The set below is the result. It is a starting
point for tuning, not a demonstrated edge.

**The replay behind the chosen set.** The four strategies, all on 15-minute
candles, the ten symbols, 25.8 days to 2026-09-24, 0.11% taker fees per round
trip. Per-trade returns added up (not compounded), each row adding one change
to the row above:

| Variant | 1st half (falling) | 2nd half (rising) |
|---|---|---|
| regime-break exit on, no re-entry cooldown (the old rules) | -65.6% | +36.3% |
| regime-break exit off | -48.3% | +39.6% |
| + 3-bar re-entry cooldown | -43.3% | +74.4% |
| + stop 3 ATR / target 6 ATR (**chosen**) | -35.1% | +79.1% |

The chosen set made about 25 trades a day, won 46% of them, held a position
about three hours on average, and averaged +0.07% per trade after fees. The
re-entry cooldown is part of it and is its own setting,
`REENTRY_COOLDOWN_BARS`; until that is in the code the bot re-enters sooner
than the replay did. Each change was kept only because it helped in BOTH
halves, not merely overall. Dropping `scalp` helped one half and hurt the
other, so it stays. The bot is long-only: in a falling market it still loses,
and no parameter here changes that. The replay did not model the trailing
stop, slippage, or the refusal of trades whose capped stop is under one ATR.

**`ENTRY_TIMEFRAME=15m` with EMA 20/50.** On 15m, EMA20 is five hours of
history, EMA50 about twelve, and the 200-period regime line about two days.
Earlier settings ran 1h (and before that 4h with SMA 50/200, a daily-chart
signal); the owner found that far too slow.

**`EXIT_ON_REGIME_BREAK=false`.** The regime filter still blocks entries
below the line. Closing on a break of it helped in neither half of the replay:
`meanrev` and `scalp` buy dips, and a dip in an uptrend is exactly what sags
toward that line, so the exit threw out trades just before they worked. The
exchange-side stop loss is the real protection.

**`EXIT_TIMEFRAME` is unset on purpose.** It inherits `ENTRY_TIMEFRAME`. The
exit uses the same indicator and periods as the entry, so a shorter timeframe
is not a symmetric exit but a far noisier one. Measured live: the same MA pair
read 258 hours of history on 1h and 4 hours on 1m, a 65x difference.

**`ADX_MIN=20`.** A bare MA crossover fires on every wiggle and bleeds in
ranges. ADX is the standard filter for exactly that. Measured over 31 days on
nine symbols on hourly candles, `trend` alone won 27% of its trades and
finished slightly negative - the filter is what keeps it from being much
worse, and 31 days is far too short to judge a trend system, which earns its
keep in rare large moves. Do not delete it on the strength of one month.

**`RSI_PERIOD=2` with `RSI_OVERSOLD=10`, not 14 with 30.** This is the
Connors RSI-2 pullback pattern. Measured on the same hourly 31 days it won
68% of trades with an average hold of 3 bars, which is precisely the published
signature of RSI-2: high hit rate, small wins. It only works paired with the
regime filter. Caveat worth keeping: RSI-2 was built on equity indices, which
mean-revert more than crypto does.

**`BREAKOUT_EXIT_LOOKBACK=10` against `BREAKOUT_LOOKBACK=20`.** The original
Turtle asymmetry. A symmetric channel gives back most of a move before
admitting the trend is over, because a new 20-bar low arrives long after the
trend died.

**`RISK_MODEL=atr`.** Measured live in one instant: ATR14 was 0.48% of price
on BTC and 1.02% on XRP. A flat percentage is therefore too tight on one
market and too wide on another, and the market picks which. Stop 3 ATR, target
6 ATR, trail 1.5 ATR arming at 3 ATR - so the trail's first trigger sits 1.5
ATR ABOVE entry by construction. Falls back to the percentage model when ATR
cannot be computed, so both sets stay meaningful.

**3/6 ATR, not the textbook 2/4.** Two ATR is a daily-chart stop. A 15-minute
candle is mostly noise by comparison, and 3/6 beat 2/4 in both halves of the
replay. On these ten symbols the median 3×ATR stop is 0.78% (BTC) to 2.92%
(NEAR), inside the 3.33% liquidation cap at 15x.

**Activation >= distance, in both models.** Bybit puts the trail's first
trigger at (activation - distance). Reversed it lands below entry and fires as
an early loss before the stop loss would - observed live at 1% activation with
a 1.5% distance: the trail sat 0.49% under entry. `validate()` refuses both
the percentage and the ATR form of this mistake.

**Ten symbols, `MAX_OPEN_POSITIONS=10`, `POSITION_NOTIONAL_USDT=450`.** More
symbols is the most effective way to get more trades without loosening a
single rule, and those trades are far less correlated than the extra ones a
lower threshold on one market would produce. Measured: 3 trades in 41 days on
the old single-symbol setup, 274 in 31 days on nine hourly symbols, about 25 a
day on ten 15-minute ones. One position per symbol, so at most 4,500 USDT of
notional is ever at work, 9% of the 50,000 demo balance.

**`LEVERAGE=15`.** Leverage does not change position size, only how little
margin backs it - 30 USDT per 450 USDT position - and how close liquidation
sits: about 6.67% away, which caps every stop at 3.33% (see "A stop beyond
liquidation is not a stop").

These two live in `.env.example` and `.env`. `config.py` deliberately falls
back to the safer 1000 USDT at `LEVERAGE=1` when neither is set, so the
figures above hold only with the control panel in place.

## Tests

```
python -m unittest discover -s tests     from the repo root, no network
```

**Tests go through one door.** Every test runs a whole cycle via
`main.main()` against `tests/fake_bybit.py` and asserts on what reaches the
exchange, the log and the phone - never on a helper or a state file.
`runCycle()` pins every setting that shapes a cycle, because the owner's
`.env` is already loaded into `config` when a test imports it; a test that
relies on a particular value passes it explicitly. When a cycle starts asking
Bybit something new, teach the fake to answer it rather than patching around
it.

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
