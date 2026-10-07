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


def bar(o, h, l, c, open_ms=0):
    return [open_ms, float(o), float(h), float(l), float(c), 1.0]


def lowsOnly(values, start_ms=0, step_ms=900000):
    """Bars that are only their lows, highs 10 above, for the swing tests."""
    return [bar(low + 5, low + 10, low, low + 5, start_ms + i * step_ms)
            for i, low in enumerate(values)]


class Swings(unittest.TestCase):
    def testASwingLowIsStrictlyBelowItsNeighboursOnEachSide(self):
        rows = lowsOnly([10, 9, 5, 8, 9, 7, 7, 8, 9])

        # 5 at index 2; the equal pair of 7s is no swing at all.
        self.assertEqual(signals.swingLows(rows, 2), [2])

    def testASwingIsOnlyKnownOnceItsConfirmingBarsHaveClosed(self):
        # The low at index 4 has one bar after it: with two needed, it does
        # not exist yet, and appears when the second closes.
        rows = lowsOnly([10, 9, 8, 9, 3, 6])
        self.assertEqual(signals.swingLows(rows, 2), [])
        self.assertEqual(signals.swingLows(rows + lowsOnly([7]), 2), [4])

    def testASwingHighMirrorsIt(self):
        rows = [bar(5, high, 0, 5) for high in [1, 2, 6, 3, 2]]
        self.assertEqual(signals.swingHighs(rows, 2), [2])
        self.assertEqual(signals.swingHighs(rows, 1), [2])


# 2026-09-14 00:00 UTC and one 15-minute step.
day0 = 1789344000000
quarter = 900000


class PreviousUtcDay(unittest.TestCase):
    def rows(self, first_ms, count):
        # The low falls one a bar, so the day's low is its last bar's.
        return [bar(100, 110 + i % 7, 90 - i, 100, first_ms + i * quarter) for i in range(count)]

    def testTheLevelsAreThePreviousDaysHighAndLowAndCountFromMidnight(self):
        rows = self.rows(day0, 96 + 10)

        day = signals.previousDay(rows, 105)

        self.assertEqual(day.high, 116)
        self.assertEqual(day.low, 90 - 95)
        self.assertEqual(day.start, day0 + 86400000)

    def testADayOnlyPartlyInTheWindowGivesNoLevels(self):
        rows = self.rows(day0 + quarter, 95 + 10)

        self.assertIsNone(signals.previousDay(rows, len(rows) - 1))

    def testABarOnTheFirstDayHasNoPreviousDay(self):
        rows = self.rows(day0, 106)

        self.assertIsNone(signals.previousDay(rows, 50))


class SweepsAndDraws(unittest.TestCase):
    def testASweepTakesAnUntouchedSwingLowAndClosesBackAboveIt(self):
        # Swing low 5 at index 2, confirmed at 4; bar 6 trades to 4 and
        # closes at 6.
        rows = lowsOnly([10, 9, 5, 8, 9, 7]) + [bar(7, 9, 4, 6, 6 * quarter)]

        self.assertEqual(signals.sweptLevels(rows, signals.swingLows(rows, 2), 0, 96, 0),
                         {6: (5, "swing low")})

    def testATakeWithoutAReclaimInTimeIsNoSweep(self):
        rows = lowsOnly([10, 9, 5, 8, 9, 7]) + [bar(7, 9, 4, 4.5, 6 * quarter),
                                                bar(4.5, 5.5, 4, 5.5, 7 * quarter)]

        self.assertEqual(signals.sweptLevels(rows, signals.swingLows(rows, 2), 0, 96, 0), {})
        self.assertEqual(signals.sweptLevels(rows, signals.swingLows(rows, 2), 0, 96, 1),
                         {6: (5, "swing low")})

    def testADrawIsASwingHighNothingHasTradedAboveSince(self):
        rows = [bar(5, high, 0, 5) for high in [1, 2, 9, 3, 2, 6, 4, 3, 8]]
        swings = signals.swingHighs(rows, 2)

        # 9 is untouched; 6 at index 5 was traded through by the 8.
        self.assertEqual(swings, [2, 5])
        self.assertEqual(signals.untouchedHighs(rows, swings, 0, 8), [9.0])
        self.assertEqual(signals.untouchedHighs(rows, swings, 3, 8), [])

    def testThePreviousDaysHighIsADrawOnlyWhileUntouchedSinceMidnight(self):
        day = signals.Day(120.0, 80.0, day0)
        below = [bar(100, 119, 95, 100, day0 + i * quarter) for i in range(3)]

        self.assertEqual(signals.dayHighDraw(below, day), 120.0)
        self.assertIsNone(signals.dayHighDraw(below + [bar(100, 121, 99, 100, day0 + 3 * quarter)],
                                              day))


class KillZones(unittest.TestCase):
    # 2026-09-15 13:45 UTC is 09:45 in New York (summer time, UTC-4).
    retest_ms = 1789479900000

    def testABarIsReadOnTheNewYorkClock(self):
        self.assertTrue(signals.inKillZone(self.retest_ms, "07:00-11:00", "America/New_York"))
        self.assertFalse(signals.inKillZone(self.retest_ms, "02:00-05:00", "America/New_York"))

    def testAWindowRunsUpToButNotIncludingItsEnd(self):
        self.assertFalse(signals.inKillZone(self.retest_ms, "07:00-09:45", "America/New_York"))
        self.assertTrue(signals.inKillZone(self.retest_ms, "09:45-10:00", "America/New_York"))

    def testAWindowCanRunPastMidnight(self):
        self.assertTrue(signals.inKillZone(self.retest_ms, "23:00-09:50", "America/New_York"))

    def testOffLetsEveryBarThrough(self):
        self.assertTrue(signals.inKillZone(self.retest_ms, "off", "America/New_York"))


def gapRows():
    """ATR 10 throughout (passed in, not computed). Candle 1 high 100, candle
    2 a 15-point up body, candle 3 low 104: the gap is 100-104."""
    return [bar(95, 98, 92, 96),
            bar(96, 100, 94, 99),       # 1: candle 1
            bar(99, 116, 98, 114),      # 2: candle 2
            bar(114, 118, 104, 117),    # 3: candle 3
            bar(117, 120, 110, 119)]


class GapFinder(unittest.TestCase):
    atr_values = [10.0] * 5

    def testAGapIsCandleOnesHighToCandleThreesLow(self):
        gaps = signals.bullishGaps(gapRows(), 0, 4, self.atr_values, 0.1, 1.0)

        self.assertEqual(gaps, [signals.Gap(1, 2, 3, 100.0, 104.0)])

    def testTheMiddleCandleMustBeADisplacementCandle(self):
        # A 15-point body is 1.5 ATR, not 2.
        self.assertEqual(signals.bullishGaps(gapRows(), 0, 4, self.atr_values, 0.1, 2.0), [])

    def testAGapSmallerThanTheMinimumDoesNotCount(self):
        # 4 points is 0.4 ATR.
        self.assertEqual(signals.bullishGaps(gapRows(), 0, 4, self.atr_values, 0.5, 1.0), [])

    def testCandleTwoMustLieInsideTheLeg(self):
        self.assertEqual(signals.bullishGaps(gapRows(), 3, 4, self.atr_values, 0.1, 1.0), [])

    def testAGapWhoseThirdCandleHasNotClosedIsNotFound(self):
        self.assertEqual(signals.bullishGaps(gapRows()[:3], 0, 4, self.atr_values, 0.1, 1.0), [])


class Retest(unittest.TestCase):
    gap = signals.Gap(1, 2, 3, 100.0, 104.0)

    def retest(self, *later, window=3, max_age=12, move_end=3):
        # `move_end` is the bar the move that left the gap ended on: the structure
        # shift for ict, the break for pullback. By default candle 3 itself.
        return signals.gapRetest(gapRows()[:4] + [bar(*row) for row in later], self.gap,
                                 move_end, 0.5, max_age, window)

    def testTheFirstTouchOfTheTopThatClosesAboveTheMidpointTriggers(self):
        self.assertEqual(self.retest((117, 120, 110, 119), (119, 119, 103, 102)), (5, None))

    def testOnlyTheFirstTouchCounts(self):
        # Bar 4 touched and closed under the midpoint of 102; bar 5 is a
        # second touch.
        index, why = self.retest((117, 118, 101, 101.5), (101.5, 106, 101, 105))
        self.assertIsNone(index)
        self.assertIn("under 102.000000", why)

    def testACloseBelowTheBottomUpToTheNewestBarKillsTheGap(self):
        index, why = self.retest((117, 118, 103, 104), (104, 105, 98, 99))
        self.assertIsNone(index)
        self.assertIn("closed below the gap", why)

    def testARetestOutsideTheSignalWindowDoesNotTrigger(self):
        index, why = self.retest((117, 118, 103, 104), (104, 108, 104.5, 107),
                                 (107, 109, 105, 108), window=2)
        self.assertIsNone(index)
        self.assertIn("outside the last 2", why)

    def testARetestLaterThanTheMaximumAgeDoesNotTrigger(self):
        index, why = self.retest((117, 120, 110, 119), (119, 119, 103, 102), max_age=1)
        self.assertIsNone(index)
        self.assertIn("over the 1-bar limit", why)

    def testNoTouchYetIsNoRetest(self):
        self.assertEqual(self.retest((117, 120, 110, 119)), (None, "no retest of the gap yet"))

    def testATouchAtOrBeforeTheEndOfTheMoveIsNotARetest(self):
        # The move ends on bar 5. Bars 4 and 5 both touch and close above the
        # midpoint, but the retest must follow the move: bar 6 is the first.
        early = ((117, 118, 103, 104), (104, 108, 103.5, 107))
        self.assertEqual(self.retest(*early, move_end=5), (None, "no retest of the gap yet"))
        self.assertEqual(self.retest(*early, (107, 109, 103, 106), move_end=5), (6, None))

    def testATouchBeforeTheEndOfTheMoveDoesNotUseUpTheOneTouch(self):
        # Bar 4 touches and closes under the midpoint of 102; read as the
        # first touch it would kill the gap. Before the move it is no touch.
        self.assertEqual(self.retest((117, 118, 101, 101.5), (101.5, 112, 105, 110),
                                     (110, 111, 103, 105), move_end=5), (6, None))

    def testTheFirstTouchAfterTheMoveIsStillTheOnlyOne(self):
        # The move ends on bar 4; bar 5 touches and closes under the midpoint,
        # so bar 6's good close is a second touch.
        index, why = self.retest((117, 120, 110, 119), (119, 120, 101, 101.5),
                                 (101.5, 106, 101, 105), move_end=4)
        self.assertIsNone(index)
        self.assertIn("under 102.000000", why)


def hlc(*rows):
    """Bars from (high, low, close) rows, each opening at its own close."""
    return [bar(c, h, l, c, i * quarter) for i, (h, l, c) in enumerate(rows)]


# A swing low at 1 (low 6), the swing high H at 3 (15), a pullback whose lowest
# low is 7.5 at 6, and a close at 8 of 16, above H.
leg_rows = [(10, 8, 9), (9, 6, 7), (12, 7, 11), (15, 10, 14), (13, 9, 11), (12, 8, 9),
            (11, 7.5, 10), (13, 8.5, 12), (17, 12, 16), (18, 14, 17)]


def findLeg(rows, reach=5, max_bars=8):
    candles = hlc(*rows)
    return signals.pullbackLeg(candles, signals.swingHighs(candles, 1),
                               signals.swingLows(candles, 1), 1, reach, max_bars)


class PullbackLeg(unittest.TestCase):
    def testTheFirstCloseAboveTheSwingHighAfterAHigherLowIsTheBreak(self):
        leg, why = findLeg(leg_rows)

        # H at 3, the break at 8 (the newest bar, 9, is not the first close
        # above), and the leg low at 6, over the swing low at 1.
        self.assertEqual(leg, signals.Leg(3, 8, 6), why)

    def testTheSwingHighIsJudgedAsOfTheBreakBar(self):
        # Bars 10 and 11 make a swing high at 9 (18) that nothing has closed
        # above. Judged from today it would be the latest swing high, and no
        # break would exist; as of bar 8 it is not yet confirmed.
        rows = leg_rows + [(16, 13, 15), (14, 11, 12)]
        self.assertEqual(signals.swingHighs(hlc(*rows), 1)[-1], 9)

        leg, why = findLeg(rows, reach=5)

        self.assertEqual(leg, signals.Leg(3, 8, 6), why)

    def testALaterBarThatClosesAboveIsNotTheFirstCloseAbove(self):
        # Bar 8 already closed above H; bars 9, 10 and 11 are in reach but
        # none of them is the break.
        rows = leg_rows + [(16, 13, 15), (14, 11, 12)]

        leg, why = findLeg(rows, reach=2)

        self.assertIsNone(leg)
        self.assertIn("no close above the latest swing high", why)

    def testASwingHighConfirmedBeforeTheBreakReplacesTheOlderOne(self):
        # H moves to the lower swing high at 6 (12.5), broken by the close of
        # 13.5 at 8. The leg low is then the lowest low from 7 to 8.
        rows = [(10, 8, 9), (9, 6, 7), (12, 7, 11), (15, 10, 14), (13, 9, 11), (11, 7.5, 10),
                (12.5, 9, 12), (12, 10, 11), (14, 11, 13.5)]

        leg, why = findLeg(rows)

        self.assertEqual(leg, signals.Leg(6, 8, 7), why)

    def testALegLowAtOrBelowThePriorSwingLowIsNotAPullback(self):
        rows = list(leg_rows)
        rows[6] = (11, 5.5, 10)

        leg, why = findLeg(rows)

        self.assertIsNone(leg)
        self.assertIn("not above the prior swing low", why)

    def testALegLowEqualToThePriorSwingLowIsNotAPullback(self):
        rows = list(leg_rows)
        rows[6] = (11, 6, 10)

        self.assertIn("not above the prior swing low", findLeg(rows)[1])

    def testALegLowTooFarBeforeTheBreakIsRefused(self):
        leg, why = findLeg(leg_rows, max_bars=1)

        self.assertIsNone(leg)
        self.assertIn("2 bar(s) before the break, over the 1-bar limit", why)

    def testNoPriorSwingLowMeansNoHigherLowToJudge(self):
        rows = [(10, 9, 9.5), (12, 8, 11), (15, 7, 14), (13, 9, 11), (12, 8, 9),
                (11, 7.5, 10), (13, 8.5, 12), (17, 12, 16)]

        leg, why = findLeg(rows)

        self.assertIsNone(leg)
        self.assertIn("no confirmed swing low before the swing high", why)

    def testABreakOlderThanTheReachIsNotLookedAt(self):
        leg, why = findLeg(leg_rows, reach=0)

        self.assertIsNone(leg)
        self.assertIn("no close above the latest swing high", why)

    def testTheLegLowIsTheLatestOfTiedLows(self):
        rows = list(leg_rows)
        rows[7] = (13, 7.5, 12)

        self.assertEqual(findLeg(rows)[0], signals.Leg(3, 8, 7))


# The ICT settings planEntry's level path reads, as in .env.example.
levels = dict(bracket, ict_max_chase_atr=0.5, ict_stop_buffer_atr=0.1, ict_stop_floor_atr=1.5,
              ict_allow_capped_stop=False, ict_min_rr=1.5, ict_fallback_target_r=2.0)


def planSetup(side=signals.long, price=105.0, atr=2.0, draws=(130.0,), structural=96.0,
               **settings):
    """The gap 100-104 with its structure at 96, entered at 105, on a 0.5 tick."""
    setup = signals.levelSetup(100.0, 104.0, structural, draws)
    with mock.patch.multiple(config, **dict(levels, **settings)):
        return executor.planEntry(spec, side, setup, price, atr)


class PlanEntryLevels(unittest.TestCase):
    def testTheStopGoesABufferUnderTheStructureAndTheTargetOnTheDraw(self):
        result = planSetup()

        # 96 - 0.1 x 2 = 95.8, rounded down to the tick
        self.assertEqual(result["stop_loss"], 95.5)
        self.assertEqual(result["take_profit"], 130.0)
        self.assertIsNone(result["refused"])
        self.assertIsNone(result["trailing_distance"])

    def testAStructureInsideTheFloorIsWidenedToTheFloor(self):
        # Structure at 104 would be 1 under the price; the floor is 1.5 ATR.
        result = planSetup(structural=104.0)

        self.assertEqual(result["stop_loss"], 102.0)
        self.assertIsNone(result["refused"])

    def testTheNearestDrawAboveThePriceIsTheTarget(self):
        result = planSetup(draws=(103.0, 140.0, 130.0))

        self.assertEqual(result["take_profit"], 130.0)

    def testADrawCloserThanTheMinimumRewardRiskRefusesTheTrade(self):
        # Risk 9.5, draw 13 away: 1.37 times.
        code, reason = planSetup(draws=(118.0,))["refused"]

        self.assertEqual(code, "reward-risk")
        self.assertIn("ICT_MIN_RR", reason)

    def testWithNoDrawTheTargetIsTheFallbackMultipleOfTheRisk(self):
        # 105 + 2 x 9.5
        result = planSetup(draws=())

        self.assertEqual(result["take_profit"], 124.0)
        self.assertIsNone(result["refused"])

    def testAPriceAboveTheChaseLimitOrUnderTheGapIsRefused(self):
        # The limit is 104 + 0.5 x 2 = 105.
        self.assertIsNone(planSetup(price=105.0)["refused"])
        self.assertEqual(planSetup(price=105.5)["refused"][0], "chase")
        self.assertEqual(planSetup(price=100.0)["refused"][0], "chase")

    def testACapThatPullsTheStopIntoTheStructureIsRefused(self):
        # At 50x the cap allows 1.05, so the stop would sit at 103.95.
        code, reason = planSetup(leverage=50)["refused"]

        self.assertEqual(code, "capped-stop")
        self.assertIn("ICT_ALLOW_CAPPED_STOP", reason)

    def testACappedStopIsTakenWhenAllowed(self):
        # 30x allows 1.75: 103.25, rounded down to 103, inside the structure
        # but allowed, and exactly the one-ATR minimum from the price.
        result = planSetup(leverage=30, ict_allow_capped_stop=True, draws=())

        self.assertEqual(result["stop_loss"], 103.0)
        self.assertIn("stop capped", result["capped"])
        self.assertIsNone(result["refused"])

    def testACappedStopUnderTheMinimumAtrMultipleIsStillRefused(self):
        result = planSetup(leverage=50, ict_allow_capped_stop=True)

        self.assertEqual(result["refused"][0], "min-stop")

    def testAShortReadsTheSetupOnTheMirroredChartAndRoundsOnTheRealOne(self):
        # On the real chart: gap 196-200 above the price of 195, structure at
        # 204, draw at 170. The rule read it negated.
        setup = signals.levelSetup(-200.0, -196.0, -204.0, (-170.0,))
        with mock.patch.multiple(config, **levels):
            result = executor.planEntry(spec, signals.short, setup, 195.0, 2.0)

        # 204 + 0.1 x 2 = 204.2, rounded up; the draw itself as the target
        self.assertEqual(result["stop_loss"], 204.5)
        self.assertEqual(result["take_profit"], 170.0)
        self.assertIsNone(result["refused"])


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
