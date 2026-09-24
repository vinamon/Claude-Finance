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


class ClosedPositionReport(unittest.TestCase):
    def testACloseIsLoggedAndPushedFromOneReadOfTheCloseHistory(self):
        client = FakeBybit(closed=[closedRecord()])

        cycle = runCycle(client)

        self.assertEqual(cycle.exit_code, 0, cycle.output)
        self.assertEqual(len(client.closed_requests), 1)
        self.assertIn("closed-position check: 1 record(s) in the last 15 minute(s)", cycle.output)
        self.assertIn("position closed: ETHUSDT qty=0.22 entry=2000 exit=1940 pnl=-13.2",
                      cycle.output)
        closes = [push for push in cycle.pushes if push["title"] == "Closed ETHUSDT"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["message"], "qty 0.22\nentry 2000 -> exit 1940\npnl -13.2 USDT")

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


if __name__ == "__main__":
    unittest.main()
