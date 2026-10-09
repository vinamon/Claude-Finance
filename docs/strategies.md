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
- **Direction:** long, and short for the strategies named in
  `SHORT_STRATEGIES=ict` (ict trades both sides). A short is the exact mirror
  of the long rule; see "Shorts" below. A held short is judged on the
  mirrored chart: by its owner's exit rule, mirrored, when the owner is a
  live strategy, otherwise by `UNKNOWN_OWNER_EXIT`.
- **Clock:** 15-minute candles (`ENTRY_TIMEFRAME=15m`) for every strategy.
  Exits read the same timeframe.
- **Closed candles only.** The candle still forming is dropped before any
  rule sees it.
- **Entries are events.** An entry condition must have happened inside the
  last `SIGNAL_LOOKBACK_BARS=3` closed candles.
- **Exits are states.** An exit rule reads the current bar every cycle.
- **Voting:** `STRATEGY=multi` with `MIN_ENTRY_VOTES=1`. Any one strategy
  saying `enter` opens the position, on the side its Decision carries. Votes
  count on one side only: the side of the highest-priority strategy that
  fired.
- **Live set:** `ACTIVE_STRATEGIES=ict`, as an experiment through its first
  review (about 100 trades per side). breakout is described below but is not
  trading: run beside ict it took the symbols ict needed (#21). meanrev and
  scalp lost money in the replay of the old setup (988f72f), pullback and
  trend in the 90-day replay of #21, and all four were removed.
- **Unknown owner:** `UNKNOWN_OWNER_EXIT=regime`. A position whose strategy
  is unknown or no longer active is left to its exchange-side stop and
  target. A position opened by hand without them is never closed by the bot
  while `EXIT_ON_REGIME_BREAK=false`.
  A breakout position still open when breakout is switched off is such a
  position: it never takes its channel exit (only the regime break would
  close it, and that is off).
- **One position per symbol.** The strategy that opened a position owns its
  exit. Only that strategy's exit rule can close it; the exchange-side stops
  can too. A setup for the other side on a symbol already held is ignored
  (logged as "holding long, short setup ignored" with `LOG_DETAIL=true`): the
  bot never flips or fights its own position.

## The regime filter (gates every entry)

A long entry is allowed only when the last close is above the
`REGIME_PERIOD=200` simple moving average on that strategy's timeframe, a
short entry only when it is below. That is about two days of 15-minute bars.
With the filter on, at most one side of a strategy can pass on any bar.

The filter gates entries only. `EXIT_ON_REGIME_BREAK=false`: a position is
not closed when price crosses back over the line. Set to true, the break
closes every position whose exit is a rule (breakout, also with
`BREAKOUT_EXIT_LOOKBACK=0`) and one whose owner is unknown. ict is exempt,
whether or not it is still live: it closes at its exchange-side stop and
target only.

## Shorts

`SHORT_STRATEGIES` names the strategies that may also open a short; empty
means long only. A short is the long rule read on the mirrored chart: every
price negated, high and low swapped. Ranges (ATR) are the same on both
charts, so every setting means the same on both sides and there are no
asymmetric defaults. The rules below are written for longs; for a short read
"above" as "below", "high" as "low", "buy-side" as "sell-side", and so on.

- **Gate:** the last close is below the `REGIME_PERIOD` SMA.
- **ict short:** a sweep of a confirmed swing high or the previous UTC day's
  high and a close back under it, a close below the last swing low, the first
  retest of the bearish gap left by that move. Stop above the structure,
  target at the nearest untouched sell-side level (swing lows, the previous
  day's low) that existed before the sweep.
- **breakout short:** a close below the lowest low of the previous
  `BREAKOUT_LOOKBACK=20` bars; exit on a close above the highest high of the
  previous `BREAKOUT_EXIT_LOOKBACK` bars, off at the default of 0. Not in
  `SHORT_STRATEGIES`: it lost in both halves of the replay.
- **Brackets:** the stop sits above the live price and the target below it,
  at the same distances as the long's (3 and 6 ATR, or the setup's levels).
  The stop rounds up to the tick and the target down, so the stop is never
  nearer than asked. The liquidation cap holds the stop within half the
  distance to liquidation above the price, 3.33% at 15x.

## ict: sweep, structure shift, retest of the gap

Long side. Every count is in closed 15-minute candles and every size in
ATR(`ATR_PERIOD=14`) on the same candles. The gap's size and body use the ATR
at candle 3; the chase limit, stop buffer, stop floor and minimum-stop check
use the ATR of the newest closed candle. Tag `i` in the order id.

- **Swings.** A swing high is a high strictly above the `ICT_SWING_BARS=2`
  highs on each side; a swing low the mirror. It exists only once those bars
  have closed.
- **Sweep, at bar `s`.** A level `L` is taken: a swing low inside the last
  `ICT_LIQUIDITY_LOOKBACK_BARS=96` bars before `s`, confirmed by bar `s−1`, or
  the previous UTC day's low (counted only when the candles read cover that
  whole day; the 96-bar lookback does not apply to it). `L` is untouched up
  to `s−1` (no low under it; for the day's low, counted from 00:00 UTC of the
  sweep's day), `low[s] < L`, and a bar from `s` to
  `s + ICT_SWEEP_RECLAIM_BARS=1` closes above `L`.
- **Structure shift, at bar `m`.** The first bar from `s` to
  `s + ICT_MSS_MAX_BARS=8` that closes above `H`, the latest swing high before
  `s` that is confirmed by bar `m`. Only the most recent shift counts; two
  sweeps that shift on the same bar are read from the first.
- **Fair-value gap.** Candles 1, 2, 3 with candle 2 between `s` and `m`, and
  `low[3] > high[1]`: the gap runs from `high[1]` (bottom) to `low[3]` (top).
  It must be at least `ICT_FVG_MIN_ATR=0.1` ATR, and candle 2 must close up
  with a body of at least `ICT_DISPLACEMENT_MIN_ATR=1.0` ATR, both with the
  ATR at candle 3. Of several, the one with the highest top.
- **Retest.** The first bar after both candle 3 and `m` whose low reaches the
  top: the retest follows the move. A bar at or before `m` is no touch; it
  neither triggers an entry nor uses up the one touch. The retest triggers
  only if it is among the last `SIGNAL_LOOKBACK_BARS=3` bars, no more than
  `ICT_FVG_MAX_AGE_BARS=12` after candle 3, and closes at or above
  `bottom + ICT_ENTRY_CLOSE_MIN=0.5` × the gap. No bar from candle 3 to the
  newest may close under the bottom. A failed first touch after `m`, or one
  outside the window or the age limit, is not retried: the gap is dead.
- **Kill zones.** `ICT_KILL_ZONES=off`. When set ("HH:MM-HH:MM,..." on the
  `ICT_KILL_ZONE_TZ=America/New_York` clock), the retest bar must open inside
  one. Needs the `tzdata` package on Windows.
- **Live price.** `bottom < live ≤ top + ICT_MAX_CHASE_ATR=0.5` × ATR, or no
  trade (refusal "chase"). No ATR to measure with: no trade (refusal "atr").
- **Stop.** The reference is candle 1's low (`ICT_STOP_REF=candle1`; `leglow`
  is the lowest low from `s` to `m`), or any lower low from candle 3 to the
  retest. `structural = reference − ICT_STOP_BUFFER_ATR=0.1` × ATR, and the
  stop is the lower of that and `live − ICT_STOP_FLOOR_ATR=1.5` × ATR. Then the
  liquidation cap; if it pulls the stop above `structural`, no trade
  (refusal "capped-stop", `ICT_ALLOW_CAPPED_STOP=false`). Then the shared
  minimum-stop check.
- **Target.** The nearest level above the live price among the swing highs
  with index from `s − ICT_LIQUIDITY_LOOKBACK_BARS` to the last one confirmed
  by `s−1` that no bar has traded above through the newest one, and the high
  of the UTC day before the sweep bar's day, while nothing from 00:00 UTC of
  the sweep's day to the newest bar has traded above it. The target is that
  level. Closer than `ICT_MIN_RR=1.5` × risk: no
  trade (refusal "reward-risk"). No such level: `ICT_FALLBACK_TARGET_R=2.0` ×
  risk.
- **Exit.** The exchange-side stop and target only. No trail, no rule exit.
  The exit on a bearish structure shift is deferred.

## breakout: Donchian channel, Turtle style

Long side, switched off (not in `ACTIVE_STRATEGIES`). Tag `b` in the order id.

- **Entry:** a close above the highest high of the previous
  `BREAKOUT_LOOKBACK=20` bars, within the lookback window. The breaking bar
  is not part of its own level.
- **Exit:** none by rule. `BREAKOUT_EXIT_LOOKBACK=0`: the position is left
  to its exchange-side stop and target. On the long side it was the one
  variant of the #21 replay that passed, ahead of a 20-bar and a 10-bar exit.
- **With a rule exit** (`BREAKOUT_EXIT_LOOKBACK` above 0): a close below the
  lowest low of that many previous bars. Keep it shorter than
  `BREAKOUT_LOOKBACK`, the original Turtle asymmetry: a symmetric exit gives
  back most of the move before admitting the trend is over.
  `config.warnings()` flags an exit lookback longer than the entry's.

## Risk, applied to every entry

The stops ride on the order, on the exchange. They are live even when the bot
is not running.

- **Sizing:** `POSITION_NOTIONAL_USDT=450` of notional at `LEVERAGE=15`, about
  30 USDT of margin. At most `MAX_OPEN_POSITIONS=10` positions.
- **Measured from the live price.** The stop and target, and ict's chase
  limit, are measured from the ticker's last trade just before the order, not
  from the candle close.
- **`RISK_MODEL=atr`** for breakout (ict reads its stop and target off the
  chart, see above), with ATR(`ATR_PERIOD=14`) on the entering strategy's
  timeframe:
  - stop: entry − `ATR_STOP_MULT=3.0` × ATR (a short: entry +)
  - target: entry + `ATR_TARGET_MULT=6.0` × ATR (a short: entry −)
  - no trailing stop: `ATR_TRAIL_MULT=0` and `TRAILING_STOP_PCT=0`. A trail
    armed at +3 ATR with a 1.5 ATR distance locks in only +1.5 ATR, and an
    ordinary 1.5 ATR pullback triggers it. ict never trails.
- **Liquidation cap.** The stop may sit no further than
  `MAX_STOP_FRACTION_OF_LIQUIDATION=0.5` of the distance to liquidation, about
  3.33% at 15x.
- **Minimum stop.** If the cap squeezes the stop under `MIN_STOP_ATR_MULT=1.0`
  × ATR, the trade is refused.
- **Re-entry cooldown.** After any close on a symbol, `REENTRY_COOLDOWN_BARS=3`
  candles pass before any strategy may enter it again.

## How they relate

`breakout` buys strength, and the regime filter lets it buy only in an
uptrend. `ict` buys a dip under a known low inside that uptrend, once the
structure has turned back up. ict's short side does the same in a
downtrend, below the regime line. breakout's short would sell a close under
the 20-bar low below the line, but it is not in `SHORT_STRATEGIES` (it lost
in both halves at every exit setting tried), and breakout itself is off.

Neither is a demonstrated edge. In the 90-day replay of #21 every strategy
and side failed alone at the defaults of the time (breakout with a 10-bar
exit); breakout long passed only without a rule exit, the default since
3e8c699, and thinly. Both halves of that window rose, so no verdict has yet
faced a falling market. ict trades as an experiment, to be judged on its live
trades after about 100 per side (see `CLAUDE.md`, "Parameter choices").
