# Claude-Finance

Trading bot on a **Bybit Demo Trading** account. Python, ccxt, virtual funds
only. No path to a real account exists or should be added.

Run it from a laptop, not CI. See "Why not GitHub Actions" below.

## Layout

| File | Role |
|---|---|
| `config.py` | settings from `.env` / env vars, the TODO block, `validate()` |
| `signals.py` | three strategies, entries and exits. Pure functions, no orders |
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

**`orderLinkId` is deterministic per (symbol, 5-minute bucket).** A retry
inside one run reuses the id and Bybit rejects it, which is the point: a retry
after an ambiguous timeout must not open a second position. The next run lands
in a new bucket.

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

## Running it

```
python run.py                 one cycle, then exit
python run.py --loop          cycle until Ctrl+C
python run.py --force-entry   one cycle that opens a test position
```

`AUTOSTART` in `.env` sets the default; flags override it. Requires Python
3.10+ (ccxt's floor) and an active virtualenv, or `.env` is silently ignored
and the only symptom is "keys not set".

`dummy_mode` defaults to true: it ignores the market and enters only on
`--force-entry` or a GitHub `workflow_dispatch` run. `--force-entry` is
one-shot in loop mode on purpose.

## Parameter choices, and why they are what they are

The owner originally reserved these decisions and later handed them over,
asking for simple textbook settings based on well-known signals. The set below
is the result. It is a starting point for tuning, not a strategy with a
demonstrated edge: assume it loses money after fees until a backtest says
otherwise.

**`ENTRY_TIMEFRAME=4h`.** SMA 50/200 is a daily-chart signal in the
textbooks. On 1h it runs about eight times faster than intended and reads as
noise; on 1d it fires once or twice a year. At 4h, SMA50 covers ~8 days and
SMA200 ~33, a normal crypto swing horizon that still produces observable
signals.

**`EXIT_TIMEFRAME` is unset on purpose.** It inherits `ENTRY_TIMEFRAME`. The
exit uses the same indicator and the same periods as the entry, so running it
on a shorter timeframe is not a symmetric exit but a much noisier one.
Measured live: SMA50/200 reads 258 hours of history on 1h and 4 hours on 1m, a
65x difference. A short exit timeframe closes positions the entry trend still
endorses and pays fees for it.

**`STOP_LOSS_PCT=0.05`, `TAKE_PROFIT_PCT=0.10`.** The stop is a disaster
brake, not the primary exit; the death cross is. A 2% stop on a 4h trend
strategy fires on routine BTC noise before the trend can play out. 1:2
risk-to-reward.

**`TRAILING_STOP_PCT=0.03`, `TRAILING_ACTIVATION_PCT=0.05`.** Activation must
be >= distance and `config.validate()` refuses to run otherwise. Bybit puts
the trail's first trigger at (activation - distance), so 5% and 3% land it 2%
above entry and arming the trail locks in profit. Reversed, it lands below
entry and fires as an early loss before the stop loss would. This was observed
live at 1% activation with a 1.5% distance: the trail sat 0.49% under entry.

**`POSITION_NOTIONAL_USDT=1000`.** 2% of the 50,000 USDT demo balance, and
twelve times Bybit's 0.001 BTC minimum lot at current prices. A notional that
sits exactly on the minimum gets the order REFUSED as soon as the price rises
enough that the notional no longer covers one lot.

**`LEVERAGE=1`.** Leverage does not change position size, only how close
liquidation sits.

SMA 50/200, RSI 14 with 30/70, and Donchian 20 are left at their textbook
values.

## Conventions

Variables lower case, functions camelCase. Secrets never printed: `notify.py`
scrubs the ntfy topic out of anything heading for a log, because `requests`
puts the full URL into its exception messages.
