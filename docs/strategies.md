# The strategies the bot trades

This is what the bot does today, stated as rules. It is for the `trader`
agent and for anyone who needs to reason about the strategies without
reading `signals.py`. When the code and this file disagree, the code is
right and this file is stale. Update this file in the same commit as any
change to a strategy.

Values are the ones in `.env.example`. Every one of them is a setting (see
"Every setting is a knob" in `CLAUDE.md`). The reasons behind the values
are in `CLAUDE.md` under "Parameter choices". They are not repeated here.

## The frame they all share

- **Market:** ten Bybit USDT perpetuals: BTC, ETH, XRP, SOL, ZEC, NEAR, HYPE,
  DOGE, 1000PEPE and BCH. This is a demo account.
- **Direction:** long only. The bot never opens a short. A short already held
  (opened by hand) is judged by the mirror of the exit rule: breakout closes
  it on a close above the previous `BREAKOUT_EXIT_LOOKBACK` bars' high, trend
  when EMA20 is above EMA50.
- **Clock:** 15-minute candles (`ENTRY_TIMEFRAME=15m`) for every strategy.
  Exits read the same timeframe.
- **Closed candles only.** The candle still forming is dropped before any
  rule sees it.
- **Entries are events.** An entry condition must have happened inside the
  last `SIGNAL_LOOKBACK_BARS=3` closed candles.
- **Exits are states.** An exit rule reads the current bar every cycle.
- **Voting:** `STRATEGY=multi` with `MIN_ENTRY_VOTES=1`. Any one strategy
  saying "buy" opens the position.
- **Live set:** `ACTIVE_STRATEGIES=breakout`. trend is described below but is
  not trading: it had too few trades in the replay to judge. meanrev and scalp
  lost money in the replay and were removed.
- **Unknown owner:** `UNKNOWN_OWNER_EXIT=regime`. A position whose strategy
  is unknown or no longer active is left to its exchange-side stop and
  target.
- **One position per symbol.** The strategy that opened a position owns its
  exit. Only that strategy's exit rule can close it; the exchange-side stops
  can too.

## The regime filter (gates every entry)

A long entry is allowed only when the last close is above the
`REGIME_PERIOD=200` simple moving average on that strategy's timeframe.
That is about two days of 15-minute bars.

The filter gates entries only. `EXIT_ON_REGIME_BREAK=false`: a position is
not closed when price falls back below the line.

## trend: EMA crossover confirmed by ADX

- **Entry:** EMA(`EMA_FAST_PERIOD=20`) crossed above EMA(`EMA_SLOW_PERIOD=50`)
  within the lookback window, and the fast EMA is still above the slow one
  now. ADX(`ADX_PERIOD=14`) must be at least `ADX_MIN=20`.
- **Exit:** EMA20 is below EMA50.
- **Why ADX:** a bare crossover fires on every wiggle in a sideways market.
  ADX says whether anything is actually trending.
- **Caveat:** it wins rarely (27% of trades over one measured month) and earns
  its money in rare large moves.

## breakout: Donchian channel, Turtle style

- **Entry:** a close above the highest high of the previous
  `BREAKOUT_LOOKBACK=20` bars, within the lookback window. The breaking bar
  is not part of its own level.
- **Exit:** a close below the lowest low of the previous
  `BREAKOUT_EXIT_LOOKBACK=10` bars. `BREAKOUT_EXIT_LOOKBACK=0` means no rule
  exit at all: the position is left to its exchange-side stop and target. The
  replay tests that variant next to 10 and 20 (wave 1).
- **Why the asymmetry:** it is the original Turtle rule. A symmetric exit
  gives back most of the move before admitting the trend is over.

## Risk, applied to every entry

The stops ride on the order, on the exchange. They are live even when the bot
is not running.

- **Sizing:** `POSITION_NOTIONAL_USDT=450` of notional at `LEVERAGE=15`, about
  30 USDT of margin. At most `MAX_OPEN_POSITIONS=10` positions.
- **Measured from the live price.** The stop, target and trail are measured
  from the ticker's last trade just before the order, not from the candle
  close.
- **`RISK_MODEL=atr`**, with ATR(`ATR_PERIOD=14`) on the entering strategy's
  timeframe:
  - stop: entry − `ATR_STOP_MULT=3.0` × ATR
  - target: entry + `ATR_TARGET_MULT=6.0` × ATR
  - no trailing stop: `ATR_TRAIL_MULT=0`. A trail of 1.5 ATR armed at +3 ATR
    closed most winners well short of the 6 ATR target.
- **Liquidation cap.** The stop may sit no further than
  `MAX_STOP_FRACTION_OF_LIQUIDATION=0.5` of the distance to liquidation, about
  3.33% at 15x.
- **Minimum stop.** If the cap squeezes the stop under `MIN_STOP_ATR_MULT=1.0`
  × ATR, the trade is refused.
- **Re-entry cooldown.** After any close on a symbol, `REENTRY_COOLDOWN_BARS=3`
  candles pass before any strategy may enter it again.

## How they relate

`trend` and `breakout` both buy strength, and the regime filter lets them buy
only in an uptrend.

They are textbook systems, not a demonstrated edge. In the replay they lost
in the falling half and won in the rising one (see `CLAUDE.md`).
