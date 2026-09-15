# Claude-Finance

Trading bot on a **Bybit Demo Trading** account. Python, ccxt, virtual funds
only. No path to a real account exists or should be added.

Run it from a laptop, not CI. See "Why not GitHub Actions" below.

## Layout

| File | Role |
|---|---|
| `config.py` | every knob, from `.env` / env vars, plus `validate()` and `warnings()` |
| `signals.py` | indicators, the regime filter, three strategies, voting. Pure functions, no orders |
| `executor.py` | decisions to orders. No strategy logic |
| `exchange.py` | ccxt client pinned to the demo host |
| `notify.py` | ntfy.sh push |
| `main.py` | one pass: closed-position reports, exits, entries |
| `run.py` | laptop runner, the loop/once switch |

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

## Running three strategies at once

`STRATEGY=multi` evaluates every strategy in `ACTIVE_STRATEGIES` each cycle
and enters when at least `MIN_ENTRY_VOTES` agree. Three things make that
coherent rather than self-defeating:

**The regime filter is load-bearing.** Trend following buys strength, mean
reversion buys weakness; run side by side without a filter one strategy's
entry is the other's exit. `REGIME_FILTER` allows long entries only above the
`REGIME_PERIOD` average, which turns mean reversion into "buy the dip inside
an uptrend" - the documented version - so all three pull the same way and
differ only in the trigger. Turning it off is supported and is a different,
worse system.

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

## Parameter choices, and why they are what they are

The owner originally reserved these decisions and later handed them over,
asking for simple textbook settings based on well-known signals, and later
still for more aggressive ones that still deserve trust. The set below is the
result. It is a starting point for tuning, not a demonstrated edge: assume it
loses money after fees until a backtest says otherwise.

**`ENTRY_TIMEFRAME=1h` with EMA 20/50, not 4h with SMA 50/200.** The old
reasoning still holds for the old indicator: SMA 50/200 IS a daily-chart
signal, which is why 4h was right for it. Changing the indicator changes the
natural timeframe with it. EMA 20/50 on 1h is about one day against two days
of history, with the 200-period regime line at roughly eight days.

**`EXIT_TIMEFRAME` is unset on purpose.** It inherits `ENTRY_TIMEFRAME`. The
exit uses the same indicator and periods as the entry, so a shorter timeframe
is not a symmetric exit but a far noisier one. Measured live: the same MA pair
read 258 hours of history on 1h and 4 hours on 1m, a 65x difference.

**`ADX_MIN=20`.** A bare MA crossover fires on every wiggle and bleeds in
ranges. ADX is the standard filter for exactly that. Measured over 31 days on
nine symbols, `trend` alone won 27% of its trades and finished slightly
negative - the filter is what keeps it from being much worse, and 31 days is
far too short to judge a trend system, which earns its keep in rare large
moves. Do not delete it on the strength of one month.

**`RSI_PERIOD=2` with `RSI_OVERSOLD=10`, not 14 with 30.** This is the
Connors RSI-2 pullback pattern. Measured on the same 31 days it won 68% of
trades with an average hold of 3 bars, which is precisely the published
signature of RSI-2: high hit rate, small wins. It only works paired with the
regime filter. Caveat worth keeping: RSI-2 was built on equity indices, which
mean-revert more than crypto does.

**`BREAKOUT_EXIT_LOOKBACK=10` against `BREAKOUT_LOOKBACK=20`.** The original
Turtle asymmetry. A symmetric channel gives back most of a move before
admitting the trend is over, because a new 20-bar low arrives long after the
trend died.

**`RISK_MODEL=atr`.** Measured live in one instant: ATR14 was 0.48% of price
on BTC and 1.02% on XRP. A flat percentage is therefore too tight on one
market and too wide on another, and the market picks which. Stop 2 ATR, target
4 ATR, trail 1.5 ATR arming at 3 ATR - so the trail's first trigger sits 1.5
ATR ABOVE entry by construction. Falls back to the percentage model when ATR
cannot be computed, so both sets stay meaningful.

**Activation >= distance, in both models.** Bybit puts the trail's first
trigger at (activation - distance). Reversed it lands below entry and fires as
an early loss before the stop loss would - observed live at 1% activation with
a 1.5% distance: the trail sat 0.49% under entry. `validate()` refuses both
the percentage and the ATR form of this mistake.

**Nine symbols, `MAX_OPEN_POSITIONS=5`, `POSITION_NOTIONAL_USDT=1000`.** More
symbols is the most effective way to get more trades without loosening a
single rule, and those trades are far less correlated than the extra ones a
lower threshold on one market would produce. Measured: 3 trades in 41 days on
the old single-symbol setup, 274 in 31 days here. At most 5,000 USDT is ever
at work, 10% of the 50,000 demo balance.

**`LEVERAGE=1`.** Leverage does not change position size, only how close
liquidation sits.

## Conventions

Variables lower case, functions camelCase. Secrets never printed: `notify.py`
scrubs the ntfy topic out of anything heading for a log, because `requests`
puts the full URL into its exception messages.

`.gitignore` masks `.env.*` as well as `.env`, negating `.env.example` back
in. Plain `.env` does not cover `.env.backup` or `.env.local`, and this
repository is public.
