"""
Pure signal and bracket helpers, tested directly.

The cycle tests in test_cycle.py go through main.main() and assert on what
reaches the exchange. These do not: each one calls a pure function and checks
its contract. They exist for the helpers whose edge cases a whole cycle would
need a contrived market to reach - rounding on each side, every refusal code,
the mirrored chart. They test function contracts, not wiring, so wiring stays
in test_cycle.py.

Run from the repository root, with the project virtualenv active:

    python -m unittest discover -s tests
"""

import unittest
from unittest import mock

import config
import executor
import signals

# The settings planEntry reads, pinned so the owner's .env cannot reach them.
bracket = dict(risk_model="atr", atr_stop_mult=3.0, atr_target_mult=6.0, atr_trail_mult=0.0,
               atr_trail_activation_mult=3.0, max_stop_fraction_of_liquidation=0.5,
               min_stop_atr_mult=1.0, leverage=5, stop_loss_pct=0.05, take_profit_pct=0.10,
               trailing_stop_pct=0.0, trailing_activation_pct=0.05, atr_period=14)

# A half-unit tick makes the rounding visible.
spec = {"tick_size": 0.5}


def plan(side, price=100.3, atr=1.0, **settings):
    with mock.patch.multiple(config, **dict(bracket, **settings)):
        return executor.planEntry(spec, side, None, price, atr)


class MirroredChart(unittest.TestCase):
    rows = [[1000, 10.0, 12.0, 9.0, 11.0, 5.0],
            [2000, 11.0, 15.0, 10.0, 14.0, 7.0]]

    def testMirroringTwiceGivesTheChartBack(self):
        self.assertEqual(signals.mirror(signals.mirror(self.rows)), self.rows)

    def testAMirroredBarSwapsHighAndLowAndKeepsTimeAndVolume(self):
        self.assertEqual(signals.mirror(self.rows)[0], [1000, -10.0, -9.0, -12.0, -11.0, 5.0])

    def testALongReadsTheChartAsItIsAndAShortReadsTheMirror(self):
        self.assertIs(signals.oriented(self.rows, signals.long), self.rows)
        self.assertEqual(signals.oriented(self.rows, signals.short), signals.mirror(self.rows))

    def testRangesAreTheSameOnBothCharts(self):
        # ATR is why one stop distance serves both sides.
        rows = [[i, 100.0 + i % 5, 103.0 + i % 5, 98.0 + i % 3, 101.0 + i % 4, 1.0]
                for i in range(40)]
        self.assertEqual(signals.atr(rows, 14), signals.atr(signals.mirror(rows), 14))


class ReasonsToldOnTheRealChart(unittest.TestCase):
    def testALongReasonIsLeftAlone(self):
        text = "close broke above the 20-bar high (close=2010.000000 level=2000.000000)"
        self.assertEqual(signals.words(text, signals.long), text)

    def testAShortReasonSwapsAboveBelowHighAndLowAndDropsTheMirroredMinus(self):
        text = "close broke above the 20-bar high (close=-1990.000000 level=-2000.000000)"
        self.assertEqual(signals.words(text, signals.short),
                         "close broke below the 20-bar low (close=1990.000000 level=2000.000000)")

    def testAShortReasonSwapsMovesAndDirectionalIndicators(self):
        text = "exit: close fell below the low, bullish, +DI=30.0 -DI=10.0, higher highs"
        self.assertEqual(signals.words(text, signals.short),
                         "exit: close rose above the high, bearish, -DI=30.0 +DI=10.0, "
                         "lower lows")

    def testWordsInsideOtherWordsAreNotTouched(self):
        # "below" must not turn "lowest-ish" or "shadow" into nonsense.
        self.assertEqual(signals.words("shadow of EMA20-EMA50, 3 bar(s)", signals.short),
                         "shadow of EMA20-EMA50, 3 bar(s)")

    def testTheMirroredRegimeSentenceReadsAsAShort(self):
        # Price -1999 against a line at -2000 is price 1999 against 2000.
        text = "regime: price -1999.000000 above SMA200 -2000.000000"
        self.assertEqual(signals.words(text, signals.short),
                         "regime: price 1999.000000 below SMA200 2000.000000")


class PlanEntry(unittest.TestCase):
    def testALongStopRoundsDownAndItsTargetRoundsUp(self):
        result = plan(signals.long)

        # 100.3 - 3 and 100.3 + 6, on a 0.5 tick
        self.assertEqual(result["stop_loss"], 97.0)
        self.assertEqual(result["take_profit"], 106.5)
        self.assertIsNone(result["refused"])

    def testAShortStopRoundsUpAndItsTargetRoundsDown(self):
        result = plan(signals.short)

        # 100.3 + 3 and 100.3 - 6, on a 0.5 tick
        self.assertEqual(result["stop_loss"], 103.5)
        self.assertEqual(result["take_profit"], 94.0)
        self.assertIsNone(result["refused"])

    def testTheTrailArmsOnTheProfitableSideOfEntry(self):
        long_plan = plan(signals.long, atr_trail_mult=1.5)
        short_plan = plan(signals.short, atr_trail_mult=1.5)

        self.assertEqual(long_plan["trailing_distance"], 1.5)
        self.assertEqual(long_plan["trailing_activation"], 103.5)
        self.assertEqual(short_plan["trailing_distance"], 1.5)
        self.assertEqual(short_plan["trailing_activation"], 97.0)

    def testNoTrailIsPlannedWhenItIsOff(self):
        result = plan(signals.short)

        self.assertIsNone(result["trailing_distance"])
        self.assertIsNone(result["trailing_activation"])

    def testAStopBeyondLiquidationIsPulledInOnTheLosingSideOfEachTrade(self):
        # 15x puts liquidation 6.67 away at 100, and half of that is allowed;
        # the 6-wide stop of a 2-point ATR does not fit.
        long_plan = plan(signals.long, price=100.0, atr=2.0, leverage=15)
        short_plan = plan(signals.short, price=100.0, atr=2.0, leverage=15)

        self.assertIn("stop capped", long_plan["capped"])
        self.assertEqual(long_plan["stop_loss"], 96.5)
        self.assertEqual(short_plan["stop_loss"], 103.5)
        self.assertAlmostEqual(short_plan["stop_distance"], 100.0 / 15 / 2)
        self.assertIsNone(long_plan["refused"])
        self.assertIsNone(short_plan["refused"])

    def testACappedStopUnderTheMinimumAtrMultipleIsRefusedOnBothSides(self):
        # ATR 4 against a 3.33 cap is 0.83 ATR, under the floor of 1.0.
        for side in (signals.long, signals.short):
            result = plan(side, price=100.0, atr=4.0, leverage=15)

            code, reason = result["refused"]
            self.assertEqual(code, "min-stop", side)
            self.assertIn("MIN_STOP_ATR_MULT", reason)

    def testAStopThatFitsWithoutCappingIsNeverRefusedForBeingTight(self):
        # A tight stop is only suspect when the cap made it so.
        result = plan(signals.long, atr=0.2, atr_stop_mult=1.0)

        self.assertIsNone(result["refused"])

    def testTheMinimumStopCheckCanBeSwitchedOff(self):
        result = plan(signals.short, price=100.0, atr=4.0, leverage=15, min_stop_atr_mult=0.0)

        self.assertIsNone(result["refused"])

    def testSettingsThatPutTheStopOnTheWrongSideOfThePriceAreRefusedAsAPriceError(self):
        # A stop allowed all the way to liquidation, at 1x, is the whole
        # price: a long's stop lands on zero.
        result = plan(signals.long, risk_model="pct", stop_loss_pct=1.5, leverage=1,
                      max_stop_fraction_of_liquidation=1.0)

        code, reason = result["refused"]
        self.assertEqual(code, "price")
        self.assertIn("not below entry", reason)
        self.assertIsNone(result["stop_loss"])

    def testAShortsTargetBelowZeroIsRefusedAsAPriceError(self):
        result = plan(signals.short, risk_model="pct", take_profit_pct=1.5)

        code, reason = result["refused"]
        self.assertEqual(code, "price")
        self.assertIn("take profit", reason)


class PositionSide(unittest.TestCase):
    def testCcxtsSideIsUsedFirst(self):
        self.assertEqual(executor.positionSide({"side": "short", "info": {"side": "Buy"}}),
                         signals.short)

    def testBybitsRawSideIsTheFallback(self):
        self.assertEqual(executor.positionSide({"info": {"side": "Sell"}}), signals.short)
        self.assertEqual(executor.positionSide({"side": None, "info": {"side": "Buy"}}),
                         signals.long)

    def testARowThatSaysNothingIsALong(self):
        self.assertEqual(executor.positionSide({}), signals.long)


if __name__ == "__main__":
    unittest.main()
