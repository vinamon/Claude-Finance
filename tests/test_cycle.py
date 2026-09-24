"""
One bot cycle, end to end, against a fake Bybit.

Every test here drives main.main() - the same door run.py uses - and asserts
only on what the cycle sends to the exchange, what it logs and what it would
push to the phone. Nothing reaches into a helper or a state file, so the
internals can be renamed, merged or split without touching these tests.

Run from the repository root, with the project virtualenv active:

    python -m unittest discover -s tests
"""

import unittest

from fake_bybit import FakeBybit, candles, runCycle

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

    def testForcedEntryNamesTheStrategyThatOwnsThePosition(self):
        client = FakeBybit(bars=candles(**flat_2000))

        cycle = runCycle(client, force_entry=True, **risk)

        # In the order id Bybit shows in its own UI, and on the phone.
        self.assertIn("-t-", client.created_orders[0]["params"]["orderLinkId"])
        opened = [push for push in cycle.pushes if push["title"] == "Opened ETH/USDT:USDT"]
        self.assertEqual(len(opened), 1, cycle.pushes)
        self.assertIn("strategy trend", opened[0]["message"])


# ARB on 2026-09-15: the last closed candle said 0.14991, and by the time the
# order went out the market was at 0.14703. A tick fine enough for the
# five-decimal prices ARB traded at.
arb = dict(symbols=("ARB/USDT:USDT",), tick="0.00001", qty_step="0.1")

# Every bar closes at 0.14991 and spans 0.14941-0.15041, so ATR is 0.001.
arb_candles = dict(close=0.14991, half_range=0.0005)


class LivePriceEntry(unittest.TestCase):
    def testTheStopIsMeasuredFromTheLivePriceNotTheLastClosedCandle(self):
        client = FakeBybit(bars=candles(**arb_candles), last_price=0.14703, **arb)

        cycle = runCycle(client, force_entry=True, **risk)

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


class ClosedPositionReport(unittest.TestCase):
    def testACloseIsLoggedAndPushedFromOneReadOfTheCloseHistory(self):
        client = FakeBybit(closed=[closedRecord()], orders={"close-1": stopLossOrder()})

        cycle = runCycle(client)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.closed_requests), 1)
        self.assertIn("closed-position check: 1 record(s) in the last 15 minute(s)", cycle.output)
        self.assertIn("position closed: ETHUSDT qty=0.22 entry=2000 exit=1940 pnl=-13.2",
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
        self.assertIn("position closed: ETHUSDT qty=0.22 entry=2000 exit=1940 pnl=-13.2 "
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
        self.assertIn("position closed: XRPUSDT qty=170 entry=2.80 exit=2.61 pnl=-33.0 "
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
        self.assertNotIn("position closed:", second.output)

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


class ConfigurationWarnings(unittest.TestCase):
    def testMixedClocksWithoutPerStrategyCapsAreWarnedAbout(self):
        # A 15-minute rule fires far more often than an hourly one and takes
        # every slot first unless the strategies are budgeted separately.
        cycle = runCycle(FakeBybit(), strategy_timeframes={"trend": "1h"},
                         max_open_per_strategy={})

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertIn("CONFIG WARNING: STRATEGY_TIMEFRAMES", cycle.output)
        self.assertIn("MAX_OPEN_PER_STRATEGY", cycle.output)

    def testOneClockIsNotWarnedAbout(self):
        cycle = runCycle(FakeBybit())

        self.assertNotIn("CONFIG WARNING", cycle.output)


if __name__ == "__main__":
    unittest.main()
