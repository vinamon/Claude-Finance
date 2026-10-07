"""
One bot cycle, end to end, against a fake Bybit.

Every test here drives main.main() - the same door run.py uses - and asserts
only on what the cycle sends to the exchange, what it logs and what it would
push to the phone. Nothing reaches into a helper or a state file, so the
internals can be renamed, merged or split without touching these tests.

Run from the repository root, with the project virtualenv active:

    python -m unittest discover -s tests
"""

import time
import unittest

from fake_bybit import (FakeBybit, breakingDown, breakingUp, candles, heldPosition, ictLong,
                        ictLongGapBeforeShift, ictShort, pullbackLong,
                        pullbackLongGapBeforeBreak, reflected, risingRun, runCycle, series,
                        trendDown, trendUp)

# 3 ATR stop, 6 ATR target, and leverage low enough that the liquidation cap
# (half of 1/5 of the price, 200 at 2000) stays out of the way of a 60 stop.
risk = dict(risk_model="atr", atr_stop_mult=3.0, atr_target_mult=6.0, leverage=5)

# Every bar closes at 2000 and spans 1990-2010, so ATR is exactly 20.
flat_2000 = dict(close=2000.0, half_range=10.0)


class ForcedEntry(unittest.TestCase):
    def testForcedEntryOnAFlatSymbolSendsAProtectedMarketBuy(self):
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, force_entry=True, **risk)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        self.assertEqual(order["symbol"], "ETH/USDT:USDT")
        self.assertEqual(order["type"], "market")
        self.assertEqual(order["side"], "buy")
        # stop 2000 - 3 x 20, target 2000 + 6 x 20
        self.assertEqual(order["params"]["stopLoss"]["triggerPrice"], 1940.0)
        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 2120.0)

    def testWithTheTrailOffNoTrailingStopIsSent(self):
        # A trail armed at +3 ATR closed most winners short of the 6 ATR
        # target, so it is off and the entry is one call, not two.
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, force_entry=True, atr_trail_mult=0.0, **risk)

        self.assertEqual(len(client.created_orders), 1, cycle.output)
        self.assertEqual(client.trading_stops, [])
        self.assertNotIn("trailing stop", cycle.output)

    def testForcedEntryNamesTheStrategyThatOwnsThePosition(self):
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, force_entry=True, **risk)

        # In the order id Bybit shows in its own UI, and on the phone.
        self.assertIn("-t-", client.created_orders[0]["params"]["orderLinkId"])
        opened = [push for push in cycle.pushes if push["title"] == "Opened ETH/USDT:USDT"]
        self.assertEqual(len(opened), 1, cycle.pushes)
        self.assertIn("strategy trend", opened[0]["message"])

    def testTheSideIsOnThePhone(self):
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, force_entry=True, **risk)

        opened = [push for push in cycle.pushes if push["title"] == "Opened ETH/USDT:USDT"]
        self.assertTrue(opened[0]["message"].startswith("long qty "), opened[0]["message"])


# ARB on 2026-09-15: the last closed candle said 0.14991, and by the time the
# order went out the market was at 0.14703. A tick fine enough for the
# five-decimal prices ARB traded at.
arb = dict(symbols=("ARB/USDT:USDT",), tick="0.00001", qty_step="0.1")

# Every bar closes at 0.14991 and spans 0.14941-0.15041, so ATR is 0.001.
arb_candles = dict(close=0.14991, half_range=0.0005)


class LivePriceEntry(unittest.TestCase):
    def testTheStopIsMeasuredFromTheLivePriceNotTheLastClosedCandle(self):
        client = FakeBybit(bars=candles(**arb_candles), last_price=0.14703, **arb)

        cycle = runCycle(client, force_entry=True, atr_trail_mult=1.5, **risk)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        # stop 0.14703 - 3 x 0.001, target 0.14703 + 6 x 0.001. Measured from
        # the candle, the stop would have sat 0.00013 under the fill.
        self.assertEqual(order["params"]["stopLoss"]["triggerPrice"], 0.14403)
        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 0.15303)
        # 450 USDT at 0.14703, rounded down to the 0.1 lot step
        self.assertEqual(order["amount"], 3060.5)
        # the trail arms 3 ATR above the live price as well
        self.assertIn("activation=0.15003", cycle.output)

    def testTheEntryLogShowsTheLastCloseAndTheLivePrice(self):
        client = FakeBybit(bars=candles(**arb_candles), last_price=0.14703, **arb)

        cycle = runCycle(client, force_entry=True, **risk)

        opening = [line for line in cycle.output.splitlines() if "opening long" in line]
        self.assertEqual(len(opening), 1, cycle.output)
        # how far the market moved between the signal and the order
        self.assertIn("@~0.147030 live, last close 0.149910 (-1.92%)", opening[0])

    def testTheLiquidationCapIsMeasuredFromTheLivePrice(self):
        # ATR 0.002 at 15x, as ARB was traded: the 3 ATR stop (0.006) is wider
        # than half the distance to liquidation, so the cap decides. Measured
        # from the candle close the cap gives 0.14491, the very stop ARB was
        # sent with four times on 2026-09-15.
        client = FakeBybit(bars=candles(close=0.14991, half_range=0.001),
                           last_price=0.14703, **arb)

        cycle = runCycle(client, force_entry=True, **dict(risk, leverage=15))

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        # 0.14703 - 0.14703 / 15 / 2, rounded down to the tick
        self.assertEqual(client.created_orders[0]["params"]["stopLoss"]["triggerPrice"], 0.14212)
        self.assertIn("ARB/USDT:USDT: stop capped", cycle.output)

    def testWithoutALivePriceNothingIsBought(self):
        # Bybit answered, but with no last trade. The candle close is right
        # there and is exactly the guess that put ARB's stop under its fill.
        client = FakeBybit(bars=candles(**flat_2000),
                           ticker={"symbol": "ETH/USDT:USDT", "last": None})

        cycle = runCycle(client, force_entry=True, **risk)

        # a skipped entry, not a failed run
        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertNotIn("Bot error", [push["title"] for push in cycle.pushes])
        self.assertEqual(client.created_orders, [])
        self.assertIn("ETH/USDT:USDT: no entry - no usable live price", cycle.output)


def closedRecord(order_id="close-1", symbol="ETHUSDT"):
    """One row of Bybit's /v5/position/closed-pnl, shaped like the real ones
    read off the demo account."""
    return {
        "orderId": order_id,
        "symbol": symbol,
        "qty": "0.22",
        "avgEntryPrice": "2000",
        "avgExitPrice": "1940",
        "closedPnl": "-13.2",
        "execType": "Trade",
        "updatedTime": "1789496187000",
    }


def closingOrder(order_id="close-1", stop_order_type="", create_type="", order_link_id=""):
    """The row Bybit's /v5/order/history returns for the order that closed a
    position. Three of its fields say who closed it; each test sets only the
    ones it needs, to values read off real orders on the demo account."""
    return {
        "orderId": order_id,
        "symbol": "ETHUSDT",
        "side": "Sell",
        "orderType": "Market",
        "orderStatus": "Filled",
        "reduceOnly": True,
        "stopOrderType": stop_order_type,
        "createType": create_type,
        "orderLinkId": order_link_id,
    }


def stopLossOrder():
    """As on the ARB stops of 2026-09-15."""
    return closingOrder(stop_order_type="StopLoss", create_type="CreateByStopLoss")


def closePushes(cycle, symbol="ETHUSDT"):
    return [push for push in cycle.pushes if push["title"] == "Closed %s" % symbol]


def breakingLow(close=2000.0, half_range=10.0):
    """Flat candles whose newest closed bar closes 20 under the 10-bar low:
    the breakout exit rule says close."""
    bars = candles(close=close, half_range=half_range)
    bars[-2] = bars[-2][:1] + [close, close + half_range, close - 25.0, close - 20.0, 1.0]
    return bars


class UnknownOwner(unittest.TestCase):
    def testUnderRegimeAPositionWithAnUnknownOwnerIsLeftToTheExchange(self):
        # Its stop and target are already on Bybit. One of the exit rules
        # nearly always says close, so "any" sold such positions within a
        # cycle for nothing but the fees.
        client = FakeBybit(bars=breakingLow(), positions=[heldPosition()])

        cycle = runCycle(client, active_strategies=["breakout"], unknown_owner_exit="regime")

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("falling back to UNKNOWN_OWNER_EXIT=regime", cycle.output)

    def testTheRegimeBreakStillClosesItWhenSwitchedOn(self):
        # Nothing is known about its bracket; it may have no stop at all, so
        # the regime break is the one rule that protects it.
        client = FakeBybit(bars=breakingLow(), positions=[heldPosition()])

        cycle = runCycle(client, active_strategies=["breakout"], unknown_owner_exit="regime",
                         exit_on_regime_break=True)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        self.assertTrue(client.created_orders[0]["params"]["reduceOnly"])
        self.assertIn("regime break: price", cycle.output)

    def testUnderAnyTheSameBarsCloseIt(self):
        client = FakeBybit(bars=breakingLow(), positions=[heldPosition()])

        cycle = runCycle(client, active_strategies=["breakout"], unknown_owner_exit="any")

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        self.assertEqual(client.created_orders[0]["side"], "sell")
        self.assertTrue(client.created_orders[0]["params"]["reduceOnly"])


def breakingHigh(close=2000.0, half_range=10.0):
    """The mirror of breakingLow: the newest closed bar closes 20 over the
    10-bar high, which is the breakout exit rule for a short."""
    bars = candles(close=close, half_range=half_range)
    bars[-2] = bars[-2][:1] + [close, close + 25.0, close - half_range, close + 20.0, 1.0]
    return bars


class HeldShort(unittest.TestCase):
    """A short opened by hand has no owner: it is judged by the active
    strategies' exits, and a short's exit is the mirror of a long's."""

    def testAShortIsClosedByAReduceOnlyBuyWhenTheCloseBreaksTheTenBarHigh(self):
        client = FakeBybit(bars=breakingHigh(), positions=[heldPosition(side="short")])

        cycle = runCycle(client, active_strategies=["breakout"], unknown_owner_exit="any")

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        self.assertEqual(order["side"], "buy")
        self.assertTrue(order["params"]["reduceOnly"])
        # told the way the real chart reads, not the mirrored one
        self.assertIn("breakout exit: close rose above the 10-bar high", cycle.output)

    def testTheSameBarsDoNotCloseALong(self):
        client = FakeBybit(bars=breakingHigh(), positions=[heldPosition(side="long")])

        cycle = runCycle(client, active_strategies=["breakout"], unknown_owner_exit="any")

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)

    def testAShortIsNotClosedByTheRuleThatClosesALong(self):
        # A close under the 10-bar low is the long's exit and the short's
        # best day.
        client = FakeBybit(bars=breakingLow(), positions=[heldPosition(side="short")])

        cycle = runCycle(client, active_strategies=["breakout"], unknown_owner_exit="any")

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)

    def testASideReportedOnlyInBybitsRawRowIsStillRead(self):
        position = heldPosition(side="short")
        position["side"] = None
        client = FakeBybit(bars=breakingHigh(), positions=[position])

        cycle = runCycle(client, active_strategies=["breakout"], unknown_owner_exit="any")

        self.assertEqual([order["side"] for order in client.created_orders], ["buy"], cycle.output)


class BreakoutWithoutARuleExit(unittest.TestCase):
    def testLookbackZeroLeavesAHeldPositionToItsExchangeStopAndTarget(self):
        # The same bars close the position under the default lookback of 10
        # (see UnknownOwner above); 0 means "no rule exit" and used to crash
        # the cycle on an empty slice.
        client = FakeBybit(bars=breakingLow(), positions=[heldPosition()])

        cycle = runCycle(client, active_strategies=["breakout"], unknown_owner_exit="any",
                         breakout_exit_lookback=0)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertNotIn("Traceback", cycle.output)

    def testLookbackZeroIsStillClosedByTheRegimeBreakWhenSwitchedOn(self):
        # No rule exit is not the same as brackets only: breakout's bracket is
        # a generic ATR one, so the regime break still applies. breakingLow()
        # closes at 1980, under the 200-bar average of about 2000.
        client = FakeBybit(bars=breakingUp())
        settings = dict(active_strategies=["breakout"], breakout_exit_lookback=0,
                        dummy_mode=False, **risk)
        first = runCycle(client, **settings)
        self.assertEqual(len(client.created_orders), 1, first.output)

        client.bars = breakingLow()
        second = runCycle(client, state_dir=first.state_dir, exit_on_regime_break=True,
                          **settings)

        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual(len(client.created_orders), 2, second.output)
        self.assertTrue(client.created_orders[1]["params"]["reduceOnly"])
        self.assertIn("regime break: price", second.output)

    def testLookbackZeroLeavesAHeldShortToItsExchangeStopAndTarget(self):
        # The mirrored exit path reads the same "off" switch: these bars
        # close a short under the default lookback (see HeldShort above).
        client = FakeBybit(bars=breakingHigh(), positions=[heldPosition(side="short")])

        cycle = runCycle(client, active_strategies=["breakout"], unknown_owner_exit="any",
                         breakout_exit_lookback=0)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertNotIn("Traceback", cycle.output)

    def testLookbackZeroIsNotWarnedAbout(self):
        cycle = runCycle(FakeBybit(), breakout_exit_lookback=0)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertNotIn("CONFIG WARNING", cycle.output)

    def testANegativeExitLookbackIsRefused(self):
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, force_entry=True, breakout_exit_lookback=-1, **risk)

        self.assertEqual(cycle.exit_code, 1, cycle.output)
        self.assertIn("CONFIG ERROR: BREAKOUT_EXIT_LOOKBACK must be >= 0", cycle.output)
        self.assertEqual(client.created_orders, [])

    def testAnEntryLookbackBelowOneIsRefused(self):
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, force_entry=True, breakout_lookback=0, breakout_exit_lookback=0,
                         **risk)

        self.assertEqual(cycle.exit_code, 1, cycle.output)
        self.assertIn("CONFIG ERROR: BREAKOUT_LOOKBACK must be >= 1", cycle.output)
        self.assertEqual(client.created_orders, [])


# ict trading for real, alone. Leverage 15 as live: the cap (3.33% of 2014,
# about 67) stays clear of the 36-wide structural stop.
ict = dict(active_strategies=["ict"], dummy_mode=False, leverage=15)


class IctLong(unittest.TestCase):
    """The setup of fake_bybit.ictLong(): gap 1990-2010, retest closing at
    2014, structure at candle 1's low 1978, one draw at 2100, ATR about 13.4."""

    def testTheRetestBuysWithTheStructuralStopAndTheDrawAsTarget(self):
        client = FakeBybit(bars=ictLong())

        # No buffer, so the stop is the structure itself: 1978 is wider than
        # the 1.5 ATR floor (about 1993.9), and 2100 is 2.3 times the 36 of
        # risk away.
        cycle = runCycle(client, ict_stop_buffer_atr=0.0, **ict)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["params"]["stopLoss"]["triggerPrice"], 1978.0)
        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 2100.0)
        self.assertIn("-i-", order["params"]["orderLinkId"])
        self.assertEqual(client.trading_stops, [])
        opened = [push for push in cycle.pushes if push["title"] == "Opened ETH/USDT:USDT"]
        self.assertIn("strategy ict", opened[0]["message"])

    def testADrawCloserThanTheMinimumRewardRiskIsNotBought(self):
        client = FakeBybit(bars=ictLong())

        cycle = runCycle(client, ict_min_rr=3.0, **ict)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("under ICT_MIN_RR=3", cycle.output)

    def testALivePriceThatHasLeftTheGapIsNotChased(self):
        # 2030 is more than half an ATR above the gap's top of 2010.
        client = FakeBybit(bars=ictLong(), last_price=2030.0)

        cycle = runCycle(client, **ict)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("has left the gap", cycle.output)

    def testAStopTheLiquidationCapPullsInsideTheStructureIsRefused(self):
        # At 100x the cap allows about 10 of room, and the structure sits 36
        # under the price.
        client = FakeBybit(bars=ictLong())

        cycle = runCycle(client, **dict(ict, leverage=100))

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("inside the setup's structure", cycle.output)

    def testAKillZoneThatExcludesTheRetestBarHoldsTheEntryBack(self):
        # The retest opens 09:45 in New York, outside the London window.
        client = FakeBybit(bars=ictLong())

        cycle = runCycle(client, ict_kill_zones="02:00-05:00", **ict)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("opened at 09:45 America/New_York, outside the kill zones", cycle.output)

    def testAKillZoneThatHoldsTheRetestBarLetsTheEntryThrough(self):
        client = FakeBybit(bars=ictLong())

        cycle = runCycle(client, ict_kill_zones="02:00-05:00,07:00-11:00", **ict)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)

    def testAHeldIctPositionIsNotClosedByARule(self):
        # Opened in the first cycle, then two cycles on bars whose newest
        # close breaks the 10-bar low - the breakout exit would sell.
        client = FakeBybit(bars=ictLong())
        first = runCycle(client, **ict)
        self.assertEqual(len(client.created_orders), 1, first.output)

        client.bars = breakingLow()
        settings = dict(ict, active_strategies=["ict", "breakout"], unknown_owner_exit="any")
        second = runCycle(client, state_dir=first.state_dir, **settings)
        third = runCycle(client, state_dir=first.state_dir, **settings)

        self.assertEqual(len(client.created_orders), 1, third.output)
        for cycle in (second, third):
            self.assertEqual(cycle.exit_code, 0, cycle.output)
            self.assertIn("ict exit: none, left to the exchange-side stop and target",
                          cycle.output)

    def testTheRegimeBreakDoesNotCloseAHeldIctPositionEvenWhenSwitchedOn(self):
        # breakingLow() closes at 1980, under the 200-bar average of about
        # 2000. ict's bracket is where its setup is wrong; the regime break
        # does not override it, whether ict is still live or not.
        client = FakeBybit(bars=ictLong())
        first = runCycle(client, **ict)
        self.assertEqual(len(client.created_orders), 1, first.output)

        client.bars = breakingLow()
        live = runCycle(client, state_dir=first.state_dir, exit_on_regime_break=True, **ict)
        retired = runCycle(client, state_dir=first.state_dir, exit_on_regime_break=True,
                           **dict(ict, active_strategies=["breakout"]))

        self.assertEqual(len(client.created_orders), 1, retired.output)
        for cycle in (live, retired):
            self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("ict exit: none, left to the exchange-side stop and target", live.output)
        self.assertIn("no regime-break exit for ict, left to the exchange-side stop and target",
                      retired.output)

    def testATouchOfTheGapBeforeTheStructureShiftIsNotTheRetest(self):
        # The gap 1990-1996 is left before the shift. A touch before the shift
        # closing under the midpoint, read as the one touch, would kill it;
        # the retest after the shift buys. Its 15 body is about 1 ATR, so the
        # displacement asked is lowered to let the gap stand.
        client = FakeBybit(bars=ictLongGapBeforeShift())

        cycle = runCycle(client, ict_displacement_min_atr=0.5, **ict)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        self.assertEqual(order["side"], "buy")
        self.assertIn("-i-", order["params"]["orderLinkId"])
        self.assertIn("first retest of the gap 1990.000000-1996.000000 closed at 1999.000000 "
                      "0 bar(s) ago", openedPushes(cycle)[0]["message"])

    def testABadKillZoneIsRefused(self):
        cycle = runCycle(FakeBybit(bars=ictLong()), ict_kill_zones="9-11", **ict)

        self.assertEqual(cycle.exit_code, 1, cycle.output)
        self.assertIn("CONFIG ERROR: ICT_KILL_ZONES must be 'off' or HH:MM-HH:MM", cycle.output)


# pullback trading for real, alone. Same leverage as ict: the cap (3.33% of
# 2026, about 67) stays clear of the 28-wide structural stop.
pullback = dict(active_strategies=["pullback"], dummy_mode=False, leverage=15)


class PullbackLong(unittest.TestCase):
    """The setup of fake_bybit.pullbackLong(): H 2040 broken after a leg low
    of 1996 over the swing low 1980, gap 2012-2024, retest closing at 2026,
    structure at candle 1's low 1998, nothing untouched above, ATR about 14."""

    def testTheRetestBuysWithTheCandleOneStopAndTheFallbackTarget(self):
        client = FakeBybit(bars=pullbackLong())

        # No buffer, so the stop is candle 1's low itself: 1998 is wider than
        # the 1.5 ATR floor (about 2005), and the 28 of risk puts the 2R
        # target at 2026 + 56.
        cycle = runCycle(client, ict_stop_buffer_atr=0.0, **pullback)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["params"]["stopLoss"]["triggerPrice"], 1998.0)
        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 2082.0)
        self.assertIn("-p-", order["params"]["orderLinkId"])
        self.assertEqual(client.trading_stops, [])
        opened = [push for push in cycle.pushes if push["title"] == "Opened ETH/USDT:USDT"]
        self.assertIn("strategy pullback", opened[0]["message"])

    def testAnUntouchedHighFromBeforeTheLegIsTheTarget(self):
        # 2100 is 74 away, 2.6 times the 28 of risk.
        client = FakeBybit(bars=pullbackLong(draw=2100.0))

        cycle = runCycle(client, ict_stop_buffer_atr=0.0, **pullback)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 2100.0)

    def testAHighCloserThanTheMinimumRewardRiskIsNotBought(self):
        client = FakeBybit(bars=pullbackLong(draw=2100.0))

        cycle = runCycle(client, ict_min_rr=3.0, **pullback)

        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("under ICT_MIN_RR=3", cycle.output)

    def testALegLowBelowThePriorSwingLowIsNotBought(self):
        # 1970 is under the swing low at 1980: a lower low, not a pullback.
        client = FakeBybit(bars=pullbackLong(leg_low=1970.0))

        cycle = runCycle(client, **pullback)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("not above the prior swing low", cycle.output)

    def testALivePriceThatHasLeftTheGapIsNotChased(self):
        # 2040 is more than half an ATR above the gap's top of 2024.
        client = FakeBybit(bars=pullbackLong(), last_price=2040.0)

        cycle = runCycle(client, **pullback)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("has left the gap", cycle.output)

    def testACandleTwoThatIsNotADisplacementIsNotBought(self):
        # Its 36 body is 2.6 ATR; asking for 4 turns the gap away.
        client = FakeBybit(bars=pullbackLong())

        cycle = runCycle(client, pullback_displacement_min_atr=4.0, **pullback)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("no gap of", cycle.output)

    def testTheRegimeBreakDoesNotCloseAHeldPullbackPositionEvenWhenSwitchedOn(self):
        client = FakeBybit(bars=pullbackLong())
        first = runCycle(client, **pullback)
        self.assertEqual(len(client.created_orders), 1, first.output)

        client.bars = breakingLow()
        second = runCycle(client, state_dir=first.state_dir, exit_on_regime_break=True,
                          **pullback)

        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual(len(client.created_orders), 1, second.output)
        self.assertIn("pullback exit: none, left to the exchange-side stop and target",
                      second.output)

    def testATouchOfTheGapBeforeTheBreakIsNotTheRetest(self):
        # The gap 2012-2024 is left before the break. A touch before the break
        # closing under the midpoint, read as the one touch, would kill it;
        # the retest after the break buys. Its 22 body is under 1.5 ATR, so
        # the displacement asked is lowered to let the gap stand.
        client = FakeBybit(bars=pullbackLongGapBeforeBreak())

        cycle = runCycle(client, pullback_displacement_min_atr=1.0, **pullback)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        self.assertEqual(order["side"], "buy")
        self.assertIn("-p-", order["params"]["orderLinkId"])
        self.assertIn("first retest of the gap 2012.000000-2024.000000 closed at 2026.000000 "
                      "0 bar(s) ago", openedPushes(cycle)[0]["message"])

    def testABadDisplacementIsRefused(self):
        cycle = runCycle(FakeBybit(bars=pullbackLong()), pullback_displacement_min_atr=-1.0,
                         **pullback)

        self.assertEqual(cycle.exit_code, 1, cycle.output)
        self.assertIn("CONFIG ERROR: PULLBACK_DISPLACEMENT_MIN_ATR must be >= 0", cycle.output)

    def testAHeldPullbackPositionIsNotClosedByARule(self):
        client = FakeBybit(bars=pullbackLong())
        first = runCycle(client, **pullback)
        self.assertEqual(len(client.created_orders), 1, first.output)

        client.bars = breakingLow()
        settings = dict(pullback, unknown_owner_exit="any")
        second = runCycle(client, state_dir=first.state_dir, **settings)

        self.assertEqual(len(client.created_orders), 1, second.output)
        self.assertIn("pullback exit: none, left to the exchange-side stop and target",
                      second.output)


# A strategy trading both sides for real. Leverage 5 as in `risk`, so the
# liquidation cap stays out of the way of a 60 stop.
both_sides = dict(dummy_mode=False, **risk)


def openedPushes(cycle, symbol="ETH/USDT:USDT"):
    return [push for push in cycle.pushes if push["title"] == "Opened %s" % symbol]


class Shorts(unittest.TestCase):
    """SHORT_STRATEGIES: each strategy's long rule read on the mirrored chart,
    below the regime line only."""

    def testAnIctShortSellsWithTheMirroredStopAndTargetAndTheIctTag(self):
        # ictLong() turned upside down around 4000: the long's 1978 structure
        # and 2100 draw are the short's 2022 and 1900, from a retest at 1986.
        client = FakeBybit(bars=ictShort())

        cycle = runCycle(client, ict_stop_buffer_atr=0.0, short_strategies=["ict"], **ict)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        self.assertEqual(order["side"], "sell")
        self.assertFalse(order["params"].get("reduceOnly"))
        self.assertEqual(order["params"]["stopLoss"]["triggerPrice"], 2022.0)
        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 1900.0)
        self.assertIn("-i-", order["params"]["orderLinkId"])
        self.assertEqual(client.trading_stops, [])
        # told on the phone the way the real chart reads
        self.assertIn("why: ict: swept the swing high", openedPushes(cycle)[0]["message"])
        self.assertTrue(openedPushes(cycle)[0]["message"].startswith("short qty "),
                        cycle.pushes)

    def testWithShortsOffTheSameBarsSendNoOrder(self):
        client = FakeBybit(bars=ictShort())

        cycle = runCycle(client, ict_stop_buffer_atr=0.0, short_strategies=[], **ict)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)

    def testAPullbackShortSellsWithTheMirroredStopAndTargetAndThePullbackTag(self):
        # pullbackLong() turned upside down around 4000: the long's candle 1
        # stop 1998 and 2R target 2082 from 2026 are the short's 2002 and 1918
        # from 1974.
        client = FakeBybit(bars=reflected(pullbackLong()))

        cycle = runCycle(client, ict_stop_buffer_atr=0.0, short_strategies=["pullback"],
                         **pullback)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        self.assertEqual(order["side"], "sell")
        self.assertFalse(order["params"].get("reduceOnly"))
        self.assertEqual(order["params"]["stopLoss"]["triggerPrice"], 2002.0)
        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 1918.0)
        self.assertIn("-p-", order["params"]["orderLinkId"])
        self.assertIn("why: pullback: broke below the swing low",
                      openedPushes(cycle)[0]["message"])

    def testAnIctShortWhoseCappedStopFallsInsideTheStructureIsNotSold(self):
        # At 100x the cap allows about 10 of room above the price, and the
        # structure sits 36 above it.
        client = FakeBybit(bars=ictShort())

        cycle = runCycle(client, short_strategies=["ict"], **dict(ict, leverage=100))

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("inside the setup's structure", cycle.output)

    def testWithShortsOffAHeldLongIsReadOnItsOwnersCandlesOnly(self):
        # ict needs more candles than breakout. A held breakout long can only
        # meet a short setup to ignore, so with shorts off the entry rules -
        # and ict's longer history - are not fetched for it; with shorts on
        # they are, in the same one request per timeframe.
        def heldRequestLimit(shorts):
            client = FakeBybit(bars=breakingUp())
            settings = dict(active_strategies=["breakout", "ict"], short_strategies=shorts,
                            **both_sides)
            first = runCycle(client, **settings)
            self.assertEqual(len(client.created_orders), 1, first.output)
            flat_limit = client.ohlcv_requests[-1]["limit"]
            client.ohlcv_requests = []
            second = runCycle(client, state_dir=first.state_dir, **settings)
            self.assertEqual(second.exit_code, 0, second.output)
            self.assertEqual(len(client.ohlcv_requests), 1, client.ohlcv_requests)
            return flat_limit, client.ohlcv_requests[0]["limit"]

        flat_limit, held_limit = heldRequestLimit([])
        self.assertLess(held_limit, flat_limit)
        flat_limit, held_limit = heldRequestLimit(["breakout"])
        self.assertEqual(held_limit, flat_limit)

    def testWithShortsOffAHeldShortStillSaysALongSetupWasIgnored(self):
        # A short opened by hand: the other side is a long, which is always on.
        client = FakeBybit(bars=breakingUp(), positions=[heldPosition(side="short")])

        cycle = runCycle(client, active_strategies=["breakout"], short_strategies=[],
                         unknown_owner_exit="regime", **both_sides)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("holding short, long setup ignored - breakout: close broke above the "
                      "20-bar high", cycle.output)

    def testABreakoutShortSellsWithTheMirroredAtrBracket(self):
        # A close at 1985 under the 20-bar low of 1990, ATR exactly 20: the
        # stop 3 ATR above the live 1985, the target 6 ATR below it.
        client = FakeBybit(bars=breakingDown())

        cycle = runCycle(client, active_strategies=["breakout"], short_strategies=["breakout"],
                         **both_sides)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        order = client.created_orders[0]
        self.assertEqual(order["side"], "sell")
        self.assertEqual(order["params"]["stopLoss"]["triggerPrice"], 2045.0)
        self.assertEqual(order["params"]["takeProfit"]["triggerPrice"], 1865.0)
        self.assertIn("-b-", order["params"]["orderLinkId"])
        self.assertIn("why: breakout: close broke below the 20-bar low",
                      openedPushes(cycle)[0]["message"])
        self.assertIn("OPENED ETH/USDT:USDT short qty=", cycle.output)

    def testATrendShortIsTheTrendLongMirrored(self):
        # trendUp() buys at 2106 with ATR 20: stop 2046, target 2226. Turned
        # upside down around 4000 it sells at 1894: stop 1954, target 1774.
        up = FakeBybit(bars=trendUp())
        down = FakeBybit(bars=trendDown())
        settings = dict(active_strategies=["trend"], short_strategies=["trend"], **both_sides)

        long_cycle = runCycle(up, **settings)
        short_cycle = runCycle(down, **settings)

        for client, cycle in ((up, long_cycle), (down, short_cycle)):
            self.assertEqual(cycle.exit_code, 0, cycle.output)
            self.assertEqual(len(client.created_orders), 1, cycle.output)
            self.assertIn("-t-", client.created_orders[0]["params"]["orderLinkId"])
        bought, sold = up.created_orders[0], down.created_orders[0]
        self.assertEqual(bought["side"], "buy")
        self.assertEqual(bought["params"]["stopLoss"]["triggerPrice"], 2046.0)
        self.assertEqual(bought["params"]["takeProfit"]["triggerPrice"], 2226.0)
        self.assertEqual(sold["side"], "sell")
        self.assertEqual(sold["params"]["stopLoss"]["triggerPrice"], 1954.0)
        self.assertEqual(sold["params"]["takeProfit"]["triggerPrice"], 1774.0)
        self.assertIn("why: trend: EMA20 crossed below EMA50",
                      openedPushes(short_cycle)[0]["message"])

    def testAHeldBreakoutShortIsClosedByAReduceOnlyBuyOnACloseAboveTheTenBarHigh(self):
        client = FakeBybit(bars=breakingDown())
        settings = dict(active_strategies=["breakout"], short_strategies=["breakout"],
                        **both_sides)
        first = runCycle(client, **settings)
        self.assertEqual([order["side"] for order in client.created_orders], ["sell"],
                         first.output)

        client.bars = breakingHigh()
        second = runCycle(client, state_dir=first.state_dir, **settings)

        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual(len(client.created_orders), 2, second.output)
        order = client.created_orders[1]
        self.assertEqual(order["side"], "buy")
        self.assertTrue(order["params"]["reduceOnly"])
        self.assertIn("breakout exit: close rose above the 10-bar high", second.output)
        self.assertIn("[owner, 15m]", second.output)

    def testAShortIsNotOpenedAboveTheRegimeLine(self):
        # A close at 1975 under the 20-bar low of about 1984, but a slow climb
        # holds the 200-bar average near 1940, under the price.
        client = FakeBybit(bars=series(risingRun(300, 2000.0, 0.6, 4.0)
                                       + [(2000, 2001, 1970, 1975)]))

        cycle = runCycle(client, active_strategies=["breakout"], short_strategies=["breakout"],
                         **both_sides)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("is above SMA200", cycle.output)
        self.assertIn("short entries blocked", cycle.output)

    def testAHeldLongMeetingAShortSetupSendsNoOrderAndSaysItWasIgnored(self):
        # The breakout short of breakingDown(), on a symbol already held long.
        # Its owner is unknown and UNKNOWN_OWNER_EXIT=regime keeps it.
        client = FakeBybit(bars=breakingDown(), positions=[heldPosition(side="long")])

        cycle = runCycle(client, active_strategies=["breakout"], short_strategies=["breakout"],
                         unknown_owner_exit="regime", **both_sides)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("ETH/USDT:USDT: holding long, short setup ignored - breakout: close broke "
                      "below the 20-bar low", cycle.output)

    def testAnUnknownNameInShortStrategiesIsRefused(self):
        client = FakeBybit(bars=breakingDown())

        cycle = runCycle(client, active_strategies=["breakout"], short_strategies=["meanrev"],
                         **both_sides)

        self.assertEqual(cycle.exit_code, 1, cycle.output)
        self.assertIn("CONFIG ERROR: SHORT_STRATEGIES contains unknown 'meanrev'", cycle.output)
        self.assertEqual(client.created_orders, [])

    def testAShortStrategyThatIsNotLiveIsWarnedAbout(self):
        cycle = runCycle(FakeBybit(), active_strategies=["breakout"], short_strategies=["ict"])

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("CONFIG WARNING: SHORT_STRATEGIES names ict, which is not live",
                      cycle.output)


class ClosedPositionReport(unittest.TestCase):
    def testACloseIsLoggedAndPushedFromOneReadOfTheCloseHistory(self):
        client = FakeBybit(closed=[closedRecord()], orders={"close-1": stopLossOrder()})

        cycle = runCycle(client)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.closed_requests), 1)
        self.assertIn("closed-position check: 1 record(s) in the last 15 minute(s)", cycle.output)
        self.assertIn("CLOSED ETHUSDT qty=0.22 entry=2000 exit=1940 pnl=-13.2",
                      cycle.output)
        closes = [push for push in cycle.pushes if push["title"] == "Closed ETHUSDT"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["message"],
                         "qty 0.22\nentry 2000 -> exit 1940\npnl -13.2 USDT\nwhy: stop loss")

    def testACloseAlreadyAnnouncedIsNotPushedAgain(self):
        client = FakeBybit(closed=[closedRecord()])
        first = runCycle(client)

        second = runCycle(client, state_dir=first.state_dir)

        self.assertNotIn("Closed ETHUSDT", [push["title"] for push in second.pushes])

    def testEveryCloseStillInTheWindowStaysAnnouncedHoweverManyThereAre(self):
        # Bybit's order ids are random, so no ordering of them says which close
        # is old. More closes than the file ever held before, none pushed twice.
        ids = ["%08x-close" % (i * 2654435761 % 2 ** 32) for i in range(600)]
        client = FakeBybit(closed=[closedRecord(order_id=order_id) for order_id in ids])
        first = runCycle(client)

        second = runCycle(client, state_dir=first.state_dir)

        self.assertEqual(len(first.pushes), 600)
        self.assertEqual(len(second.pushes), 0)

    def testAFailedReadOfTheCloseHistoryDoesNotStopTheCycle(self):
        client = FakeBybit(bars=candles(**flat_2000), closed_error=RuntimeError("closed-pnl is down"))

        cycle = runCycle(client, force_entry=True, **risk)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("could not fetch closed positions: closed-pnl is down", cycle.output)
        self.assertEqual(len(client.created_orders), 1)


class CloseCause(unittest.TestCase):
    def testAStopLossIsNamedInTheLogAndOnThePhone(self):
        client = FakeBybit(closed=[closedRecord()], orders={"close-1": stopLossOrder()})

        cycle = runCycle(client)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.order_history_requests,
                         [{"category": "linear", "orderId": "close-1"}])
        self.assertIn("CLOSED ETHUSDT qty=0.22 entry=2000 exit=1940 pnl=-13.2 "
                      "why=stop loss", cycle.output)
        closes = closePushes(cycle)
        self.assertEqual(len(closes), 1, cycle.pushes)
        self.assertIn("why: stop loss", closes[0]["message"].splitlines())

    def testALiquidationIsNamedFromTheCloseRecordAlone(self):
        # Shaped like the manual XRP trade liquidated for -33 USDT on
        # 2026-09-19; the prices are illustrative. Bybit marks the fill itself
        # as a bust trade, so no order lookup is needed.
        record = dict(closedRecord(symbol="XRPUSDT"), qty="170", avgEntryPrice="2.80",
                      avgExitPrice="2.61", closedPnl="-33", execType="BustTrade")
        client = FakeBybit(closed=[record])

        cycle = runCycle(client)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.order_history_requests, [])
        self.assertIn("CLOSED XRPUSDT qty=170 entry=2.80 exit=2.61 pnl=-33.0 "
                      "why=liquidation", cycle.output)
        closes = closePushes(cycle, "XRPUSDT")
        self.assertEqual(len(closes), 1, cycle.pushes)
        self.assertIn("why: liquidation", closes[0]["message"].splitlines())

    def testTheBotsOwnExitIsRecognisedByItsOrderLinkId(self):
        # The close tag the bot puts on every exit it sends, as on ARB's
        # cf-ARBUSDT-c-... on the demo account.
        exit_order = closingOrder(order_link_id="cf-ETHUSDT-c-14912468")
        client = FakeBybit(closed=[closedRecord()], orders={"close-1": exit_order})

        cycle = runCycle(client)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("pnl=-13.2 why=bot exit", cycle.output)
        closes = closePushes(cycle)
        self.assertEqual(len(closes), 1, cycle.pushes)
        self.assertIn("why: bot exit", closes[0]["message"].splitlines())

    def testAManualCloseIsReportedAsClosedOutsideTheBot(self):
        # The Close button in Bybit's own interface.
        manual = closingOrder(create_type="CreateByClosing")
        client = FakeBybit(closed=[closedRecord()], orders={"close-1": manual})

        cycle = runCycle(client)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("pnl=-13.2 why=closed outside the bot (CreateByClosing)", cycle.output)
        closes = closePushes(cycle)
        self.assertEqual(len(closes), 1, cycle.pushes)
        self.assertIn("why: closed outside the bot (CreateByClosing)",
                      closes[0]["message"].splitlines())

    def testAnOutsideCloseWithoutACreateTypeSaysSoWithoutEmptyBrackets(self):
        client = FakeBybit(closed=[closedRecord()], orders={"close-1": closingOrder()})

        cycle = runCycle(client)

        self.assertIn("why: closed outside the bot", closePushes(cycle)[0]["message"].splitlines())

    def testACloseAlreadyAnnouncedIsNotLookedUpAgain(self):
        # The close stays inside the lookback window for several cycles; only
        # the first one that sees it should pay for the order-history request.
        client = FakeBybit(closed=[closedRecord()], orders={"close-1": stopLossOrder()})
        first = runCycle(client)
        self.assertEqual(len(client.order_history_requests), 1, first.output)

        second = runCycle(client, state_dir=first.state_dir)

        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual(len(client.order_history_requests), 1, second.output)
        self.assertEqual(closePushes(second), [])
        self.assertNotIn("CLOSED ", second.output)

    def testAClosingOrderMissingFromTheHistoryIsStillReportedAsUnknown(self):
        client = FakeBybit(closed=[closedRecord()], orders={})

        cycle = runCycle(client)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("could not look up why ETHUSDT closed", cycle.output)
        self.assertIn("pnl=-13.2 why=unknown", cycle.output)
        closes = closePushes(cycle)
        self.assertEqual(len(closes), 1, cycle.pushes)
        self.assertIn("why: unknown", closes[0]["message"].splitlines())

    def testAFailedLookupIsReportedAsUnknownAndStillLoggedAndPushed(self):
        client = FakeBybit(bars=candles(**flat_2000), closed=[closedRecord()],
                           order_history_error=RuntimeError("order history is down"))

        cycle = runCycle(client, force_entry=True, **risk)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("could not look up why ETHUSDT closed (order close-1): "
                      "order history is down", cycle.output)
        self.assertIn("pnl=-13.2 why=unknown", cycle.output)
        closes = closePushes(cycle)
        self.assertEqual(len(closes), 1, cycle.pushes)
        self.assertIn("why: unknown", closes[0]["message"].splitlines())
        # and the rest of the cycle carries on
        self.assertEqual(len(client.created_orders), 1, cycle.output)


def closedMinutesAgo(minutes, symbol="ETHUSDT"):
    """A closed-position record whose close happened `minutes` ago."""
    closed_at = int((time.time() - minutes * 60) * 1000)
    return dict(closedRecord(order_id="close-%s-%d" % (symbol, minutes), symbol=symbol),
                updatedTime=str(closed_at))


def lineContaining(output, text):
    return next((line for line in output.splitlines() if text in line), "")


# Three 15-minute candles: 45 minutes of waiting after any close.
cooldown = dict(reentry_cooldown_bars=3, entry_timeframe="15m")


class ReentryCooldown(unittest.TestCase):
    def testAPositionClosedTenMinutesAgoHoldsBackTheNextEntry(self):
        client = FakeBybit(bars=candles(**flat_2000), closed=[closedMinutesAgo(10)])

        cycle = runCycle(client, force_entry=True, **cooldown, **risk)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        skipped = lineContaining(cycle.output, "re-entry cooldown")
        self.assertIn("ETH/USDT:USDT: no entry", skipped, cycle.output)
        # 45 minutes of cooldown, 10 of them already gone
        self.assertIn("35 minute(s) left", skipped)
        # The signal it held back is still on record.
        self.assertIn("forcing a test entry", skipped)

    def testACloseOlderThanTheCooldownDoesNotHoldBackTheEntry(self):
        client = FakeBybit(bars=candles(**flat_2000), closed=[closedMinutesAgo(50)])

        cycle = runCycle(client, force_entry=True, **cooldown, **risk)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        self.assertNotIn("re-entry cooldown", cycle.output)

    def testTheWaitIsCountedInCandlesOfTheStrategyThatWantsToEnter(self):
        # The same close 50 minutes ago that a 15-minute strategy is past is
        # still inside three hourly candles.
        client = FakeBybit(bars=candles(**flat_2000), closed=[closedMinutesAgo(50)])

        cycle = runCycle(client, force_entry=True, active_strategies=["trend"],
                         strategy_timeframes={"trend": "1h"}, **cooldown, **risk)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(client.created_orders, [], cycle.output)
        self.assertIn("130 minute(s) left", lineContaining(cycle.output, "re-entry cooldown"))

    def testTheCloseHistoryIsReadAsFarBackAsTheLongestCooldown(self):
        # 3 hourly candles reach further back than CLOSED_LOOKBACK_MINUTES.
        cycle = runCycle(FakeBybit(), active_strategies=["trend"],
                         strategy_timeframes={"trend": "1h"}, closed_lookback_minutes=15,
                         **cooldown)

        self.assertIn("closed-position check: 0 record(s) in the last 180 minute(s)",
                      cycle.output)

    def testACloseOnOneSymbolDoesNotHoldBackAnother(self):
        client = FakeBybit(symbols=("ETH/USDT:USDT", "BTC/USDT:USDT"),
                           bars=candles(**flat_2000), closed=[closedMinutesAgo(10, "ETHUSDT")])

        cycle = runCycle(client, force_entry=True, **cooldown, **risk)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual([order["symbol"] for order in client.created_orders],
                         ["BTC/USDT:USDT"], cycle.output)

    def testZeroBarsSwitchesTheCooldownOff(self):
        client = FakeBybit(bars=candles(**flat_2000), closed=[closedMinutesAgo(1)])

        cycle = runCycle(client, force_entry=True, reentry_cooldown_bars=0, **risk)

        self.assertEqual(len(client.created_orders), 1, cycle.output)

    def testAnUnreadableCloseHistoryTradesWithoutTheCooldownForOneCycle(self):
        client = FakeBybit(bars=candles(**flat_2000),
                           closed_error=RuntimeError("closed-pnl is down"))

        cycle = runCycle(client, force_entry=True, **cooldown, **risk)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.created_orders), 1, cycle.output)
        self.assertIn("re-entry cooldown unavailable this cycle", cycle.output)

    def testANegativeCooldownIsRefused(self):
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, force_entry=True, reentry_cooldown_bars=-1, **risk)

        self.assertEqual(cycle.exit_code, 1, cycle.output)
        self.assertIn("CONFIG ERROR: REENTRY_COOLDOWN_BARS must be >= 0", cycle.output)
        self.assertEqual(client.created_orders, [])


class ShortLog(unittest.TestCase):
    """LOG_DETAIL off: only what changed, errors, and one summary line."""

    def testAQuietCycleSaysNoEntriesAndNothingPerSymbol(self):
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, log_detail=False, **risk)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(cycle.output.strip(), "no entries, 0 open")

    def testAnEntryPrintsTheInstrumentSizeAndLeverage(self):
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, log_detail=False, force_entry=True, **risk)

        lines = cycle.output.strip().splitlines()
        self.assertEqual(len(lines), 2, cycle.output)
        self.assertIn("OPENED ETH/USDT:USDT long qty=", lines[0])
        self.assertIn("USDT at 5x", lines[0])
        self.assertEqual(lines[1].strip(), "1 opened, 1 open")

    def testACloseIsPrintedWithItsCause(self):
        client = FakeBybit(closed=[closedRecord()], orders={"close-1": stopLossOrder()})

        cycle = runCycle(client, log_detail=False)

        self.assertIn("CLOSED ETHUSDT qty=0.22 entry=2000 exit=1940 pnl=-13.2 why=stop loss",
                      cycle.output)
        self.assertIn("1 closed, 0 open", cycle.output)
        self.assertNotIn("closed-position check", cycle.output)


class ConfigurationWarnings(unittest.TestCase):
    def testAMissingNtfyTopicIsWarnedAbout(self):
        # runCycle pins a topic, so the warning shows only when a test asks
        # for it with an empty one.
        cycle = runCycle(FakeBybit(), ntfy_topic="")

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("WARNING: NTFY_TOPIC is not set", cycle.output)

    def testMixedClocksWithoutPerStrategyCapsAreWarnedAbout(self):
        # A 15-minute rule fires far more often than an hourly one and takes
        # every slot first unless the strategies are budgeted separately.
        cycle = runCycle(FakeBybit(), strategy_timeframes={"trend": "1h"},
                         max_open_per_strategy={})

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("CONFIG WARNING: STRATEGY_TIMEFRAMES", cycle.output)
        self.assertIn("MAX_OPEN_PER_STRATEGY", cycle.output)

    def testACooldownShorterThanTheSignalWindowIsWarnedAbout(self):
        # The signal behind a stopped-out trade would still be in the window
        # when the symbol is let back in, and would be bought again.
        cycle = runCycle(FakeBybit(), reentry_cooldown_bars=1, signal_lookback_bars=3)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("CONFIG WARNING: REENTRY_COOLDOWN_BARS (1) is shorter than "
                      "SIGNAL_LOOKBACK_BARS (3)", cycle.output)

    def testAFallbackTargetUnderTheMinimumRewardRiskIsWarnedAbout(self):
        # A setup with no level above would be taken at 2R while one whose
        # nearest level sits at 2.5R is refused.
        cycle = runCycle(FakeBybit(), ict_min_rr=3.0, ict_fallback_target_r=2.0)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("CONFIG WARNING: ICT_FALLBACK_TARGET_R (2) is under ICT_MIN_RR (3)",
                      cycle.output)

    def testALevelStopFloorUnderTheMinimumStopIsWarnedAbout(self):
        cycle = runCycle(FakeBybit(), ict_stop_floor_atr=0.5, min_stop_atr_mult=1.0)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("CONFIG WARNING: ICT_STOP_FLOOR_ATR (0.5) is under MIN_STOP_ATR_MULT (1)",
                      cycle.output)

    def testOneClockIsNotWarnedAbout(self):
        cycle = runCycle(FakeBybit())

        self.assertNotIn("CONFIG WARNING", cycle.output)

    def testASettingOfARemovedStrategyIsWarnedAbout(self):
        # A stale key in .env that is silently ignored costs an afternoon.
        cycle = runCycle(FakeBybit(), environ={"RSI_PERIOD": "2"})

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("CONFIG WARNING: RSI_PERIOD is set but no longer used - the meanrev "
                      "strategy was removed", cycle.output)

    def testARemovedStrategyIsRefused(self):
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, force_entry=True, active_strategies=["meanrev"], **risk)

        self.assertEqual(cycle.exit_code, 1, cycle.output)
        self.assertIn("CONFIG ERROR: ACTIVE_STRATEGIES contains unknown 'meanrev'", cycle.output)
        self.assertEqual(client.created_orders, [])


if __name__ == "__main__":
    unittest.main()
