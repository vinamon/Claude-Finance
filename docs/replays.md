# Replay evidence

The numbers behind "Parameter choices" in CLAUDE.md, newest first.

## 2026-10-07: the 90-day replay of #21

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
