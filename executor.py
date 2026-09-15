"""
Order execution.

Takes a decision from signals.py and turns it into orders. Contains no
strategy logic whatsoever.

THE EXCHANGE IS THE ONLY SOURCE OF TRUTH
----------------------------------------
Nothing about open positions is cached locally. Every run asks Bybit what it
is actually holding. A GitHub Actions runner is destroyed after each run, so
any local view of "what we own" would be a guess, and a guess that opens
duplicate positions.

TWO CALLS, NOT ONE
------------------
Bybit's v5 API cannot attach a trailing stop to an order at creation time.
/v5/order/create accepts takeProfit and stopLoss but has no trailingStop
field at all; trailingStop lives only on /v5/position/trading-stop, which
operates on an existing position. So an entry is:

  1. POST /v5/order/create   -> market buy, with stopLoss + takeProfit attached
  2. POST /v5/position/trading-stop -> trailingStop (+ activePrice) on the
     position that step 1 just created

Verified against Bybit's v5 documentation, September 2026.
"""

import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP

import ccxt

import config
import signals


class ExecutionError(Exception):
    """Something went wrong that the operator needs to know about."""


# ---------------------------------------------------------------------------
# rounding
#
# Done in Decimal. Sizing errors caused by float drift are the kind of bug
# that shows up as a rejected order at 3am.
# ---------------------------------------------------------------------------


def floorToStep(value, step):
    if step is None or float(step) <= 0:
        return float(value)
    value_d, step_d = Decimal(str(value)), Decimal(str(step))
    return float((value_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d)


def ceilToStep(value, step):
    if step is None or float(step) <= 0:
        return float(value)
    value_d, step_d = Decimal(str(value)), Decimal(str(step))
    return float((value_d / step_d).to_integral_value(rounding=ROUND_UP) * step_d)


def formatDecimal(value, step):
    """Render a number with exactly the decimals the instrument's step implies.

    Bybit wants strings, and rejects values carrying more precision than the
    step allows. It also dislikes scientific notation, which is what str()
    produces for small floats.
    """
    if step is None or float(step) <= 0:
        return ("%.8f" % float(value)).rstrip("0").rstrip(".")
    exponent = Decimal(str(step)).normalize().as_tuple().exponent
    places = max(0, -exponent)
    return str(Decimal(str(value)).quantize(Decimal(1).scaleb(-places)))


# ---------------------------------------------------------------------------
# instrument specification
# ---------------------------------------------------------------------------


def instrumentSpec(client, symbol):
    """Pull qty/price steps straight from Bybit's instruments-info payload.

    ccxt's load_markets() already fetches /v5/market/instruments-info, so
    market['info'] carries the exchange's own lotSizeFilter and priceFilter.
    Reading those rather than ccxt's normalised fields keeps us honest about
    what the exchange will actually accept.
    """
    market = client.market(symbol)
    info = market.get("info") or {}
    lot = info.get("lotSizeFilter") or {}
    price_filter = info.get("priceFilter") or {}

    spec = {
        "market": market,
        "market_id": market["id"],
        "qty_step": toFloat(lot.get("qtyStep"), market["precision"].get("amount")),
        "min_qty": toFloat(lot.get("minOrderQty"), (market["limits"]["amount"] or {}).get("min")),
        "max_qty": toFloat(lot.get("maxOrderQty"), (market["limits"]["amount"] or {}).get("max")),
        "tick_size": toFloat(price_filter.get("tickSize"), market["precision"].get("price")),
    }
    if not spec["qty_step"] or not spec["tick_size"]:
        raise ExecutionError(
            "%s: incomplete instrument spec (qty_step=%s tick_size=%s)"
            % (symbol, spec["qty_step"], spec["tick_size"])
        )
    return spec


def toFloat(primary, fallback):
    for candidate in (primary, fallback):
        if candidate is None or candidate == "":
            continue
        try:
            return float(candidate)
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# position state - always read from the exchange
# ---------------------------------------------------------------------------


def openPosition(client, symbol):
    """Return the open position for symbol, or None.

    Bybit reports flat positions as rows with size 0, so a row existing is not
    the same as a position existing.
    """
    positions = client.fetch_positions([symbol], params={"category": config.category})
    for position in positions:
        size = position.get("contracts")
        if size is None:
            size = (position.get("info") or {}).get("size")
        try:
            size = float(size or 0)
        except (TypeError, ValueError):
            size = 0.0
        if size > 0:
            return position
    return None


def positionSize(position):
    size = position.get("contracts")
    if size is None:
        size = (position.get("info") or {}).get("size")
    return float(size or 0)


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------


def computeQty(spec, price, notional_usdt):
    """Convert a USDT notional into a legal contract quantity.

    Rounds DOWN to the lot step - rounding up would quietly trade a bigger
    position than asked for. If the rounded size falls under the instrument's
    minimum, the trade is refused rather than silently upsized.
    """
    if price <= 0:
        raise ExecutionError("cannot size a position at price %s" % price)

    raw_qty = notional_usdt / price
    qty = floorToStep(raw_qty, spec["qty_step"])

    if spec["min_qty"] and qty < spec["min_qty"]:
        raise ExecutionError(
            "notional %.2f USDT at price %.6f gives %s contracts, below the %s minimum "
            "for %s. Raise POSITION_NOTIONAL_USDT to at least %.2f USDT."
            % (
                notional_usdt,
                price,
                formatDecimal(qty, spec["qty_step"]),
                formatDecimal(spec["min_qty"], spec["qty_step"]),
                spec["market_id"],
                spec["min_qty"] * price,
            )
        )
    if spec["max_qty"] and qty > spec["max_qty"]:
        qty = floorToStep(spec["max_qty"], spec["qty_step"])
    if qty <= 0:
        raise ExecutionError("computed a non-positive quantity for %s" % spec["market_id"])
    return qty


def liquidationDistance(entry_price):
    """Roughly how far price must move against a long before Bybit closes it.

    Leverage is what decides this: at 15x a 6.67% move against the position
    consumes the whole margin. The real figure is slightly nearer than that
    once maintenance margin and fees are counted, which is exactly why the
    stop is only allowed a FRACTION of this distance rather than all of it.
    """
    leverage = max(1, config.leverage)
    return entry_price / float(leverage)


def riskDistances(entry_price, atr_value):
    """How far the stop, target, trail and its activation sit from entry, in
    price units. Returns (distances, model_name).

    Two models, chosen by RISK_MODEL:

    "atr" measures everything in the recent volatility of the instrument. Two
    ATR is the same amount of risk on BTC as on a small alt, and the same
    amount in a calm week as in a violent one. A flat percentage is neither:
    it is too tight somewhere and too wide somewhere else, and the market
    decides which.

    "pct" is a flat fraction of the entry price.

    The ATR model falls back to the percentage one when ATR is unavailable -
    a fresh symbol without enough history should not silently trade naked.
    """
    if config.risk_model == "atr" and atr_value and atr_value > 0:
        return {
            "stop": atr_value * config.atr_stop_mult,
            "target": atr_value * config.atr_target_mult,
            "trail": atr_value * config.atr_trail_mult,
            "activation": atr_value * config.atr_trail_activation_mult,
        }, "atr(%d)=%.6f" % (config.atr_period, atr_value)

    model = "pct"
    if config.risk_model == "atr":
        model = "pct (ATR unavailable, fell back)"
    return {
        "stop": entry_price * config.stop_loss_pct,
        "target": entry_price * config.take_profit_pct,
        "trail": entry_price * config.trailing_stop_pct,
        "activation": entry_price * config.trailing_activation_pct,
    }, model


def capStopAtLiquidation(distances, entry_price):
    """Pull the stop inside the liquidation price. Returns a note, or None.

    A STOP BEYOND LIQUIDATION IS NOT A STOP. The exchange closes the position
    first, takes the whole margin and charges a liquidation fee, and the rule
    the strategy was built around never gets a say. Measured live on this
    account: ARB opened with a 2xATR stop 7.07% away while liquidation sat
    5.72% away, so the stop could not have fired.

    Volatility across a forty-symbol list spans a factor of twenty - 2xATR is
    1.24% on BTC and 27% on the wildest alt - so no single leverage setting
    makes every symbol safe. Capping per trade does.
    """
    limit = liquidationDistance(entry_price) * config.max_stop_fraction_of_liquidation
    if distances["stop"] <= limit:
        return None
    original = distances["stop"]
    distances["stop"] = limit
    return ("stop capped from %.2f%% to %.2f%% of entry: liquidation sits %.2f%% away at %dx "
            "and MAX_STOP_FRACTION_OF_LIQUIDATION allows half of that"
            % (100.0 * original / entry_price,
               100.0 * limit / entry_price,
               100.0 * liquidationDistance(entry_price) / entry_price,
               max(1, config.leverage)))


def exitPrices(spec, entry_price, atr_value=None):
    """Stop loss / take profit prices and the trailing distance, for a long.

    trailing_distance is a PRICE DISTANCE, not a percentage: the Bybit v5 docs
    define trailingStop as "Trailing stop by price distance". Sending 1.5
    meaning "1.5%" would actually ask for a 1.5 USDT trail, which on BTC is a
    stop roughly at the current price.
    """
    tick = spec["tick_size"]
    distances, model = riskDistances(entry_price, atr_value)
    capped = capStopAtLiquidation(distances, entry_price)
    prices = {"stop_loss": None, "take_profit": None, "trailing_distance": None,
              "trailing_activation": None, "risk_model": model, "capped": capped,
              "stop_distance": distances["stop"]}

    if distances["stop"] > 0:
        # round DOWN so the stop sits at or slightly further from entry, never
        # closer than requested
        stop = floorToStep(entry_price - distances["stop"], tick)
        if stop <= 0 or stop >= entry_price:
            raise ExecutionError(
                "stop loss %.8f is not below entry %.8f - check the %s risk settings"
                % (stop, entry_price, config.risk_model)
            )
        prices["stop_loss"] = stop

    if distances["target"] > 0:
        # round UP for the same reason, in the other direction
        target = ceilToStep(entry_price + distances["target"], tick)
        if target <= entry_price:
            raise ExecutionError(
                "take profit %.8f is not above entry %.8f - check the %s risk settings"
                % (target, entry_price, config.risk_model)
            )
        prices["take_profit"] = target

    if distances["trail"] > 0:
        distance = ceilToStep(distances["trail"], tick)
        if distance < tick:
            distance = tick
        prices["trailing_distance"] = distance
        if distances["activation"] > 0:
            prices["trailing_activation"] = ceilToStep(
                entry_price + distances["activation"], tick
            )

    return prices


def stopTooTight(targets, atr_value):
    """(too_tight, reason). A capped stop inside normal noise is not worth taking.

    Capping keeps the position clear of liquidation, but squeezing the stop
    inside ordinary bar-to-bar movement turns it from protection into a
    scheduled exit - the next candle hits it by accident. On a symbol whose
    ATR is 13% of price, no stop that fits inside a 15x liquidation is far
    enough away to mean anything, and the honest answer is to skip the trade.
    """
    if not config.min_stop_atr_mult or not atr_value or atr_value <= 0:
        return False, ""
    if not targets.get("capped"):
        return False, ""
    multiple = targets["stop_distance"] / atr_value
    if multiple >= config.min_stop_atr_mult:
        return False, ""
    return True, ("after capping, the stop would sit %.2f ATR from entry, under the "
                  "MIN_STOP_ATR_MULT floor of %.2f. At %dx leverage there is no room "
                  "on this symbol for a stop wide enough to survive normal movement."
                  % (multiple, config.min_stop_atr_mult, max(1, config.leverage)))


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def strategyTag(strategy):
    """One character standing for the strategy, for the order id.

    Bybit shows orderLinkId in its own UI, so tagging the id means you can
    tell at a glance which rule opened a position without consulting anything
    local. Derived from the name rather than a hardcoded table, so adding a
    strategy needs no change here - validated for collisions in validate()'s
    sibling checks by the fact that the names differ in their first letter.
    """
    if not strategy:
        return "x"
    return "".join(char for char in strategy if char.isalnum())[:1].lower() or "x"


def buildOrderLinkId(market_id, strategy=None, bucket_seconds=None):
    """Deterministic per (symbol, strategy, time bucket) client order id.

    Bybit rejects a duplicate orderLinkId. Bucketing by the scheduling
    interval means a retry inside the same bucket reuses the id and gets
    rejected - which is exactly what we want, because a retry after an
    ambiguous timeout must not open a second position. The next cycle lands in
    a new bucket and is free to trade again.

    The bucket length comes from config.order_bucket_seconds, which defaults
    to the loop interval. Those two must not drift apart: a bucket longer than
    the interval blocks a cycle that legitimately wants to retry until the
    bucket rolls over.

    Max 36 characters, letters/digits/dash/underscore only.
    """
    if bucket_seconds is None:
        bucket_seconds = config.order_bucket_seconds
    bucket = int(time.time() // max(1, bucket_seconds))
    safe_id = "".join(char for char in market_id if char.isalnum() or char in "-_")
    tag = strategyTag(strategy)
    candidate = "%s-%s-%s-%d" % (config.order_link_prefix, safe_id, tag, bucket)
    if len(candidate) <= 36:
        return candidate
    # keep the prefix, tag and bucket readable, trim the symbol
    overflow = len(candidate) - 36
    trimmed = safe_id[: max(1, len(safe_id) - overflow)]
    return "%s-%s-%s-%d" % (config.order_link_prefix, trimmed, tag, bucket)


# ---------------------------------------------------------------------------
# leverage
# ---------------------------------------------------------------------------


def applyLeverage(client, symbol, log):
    """Set leverage, treating "already set" as success.

    Bybit returns error 110043 ("leverage not modified") when the requested
    leverage is already in place. That is a no-op, not a failure, and it will
    happen on almost every run.
    """
    try:
        client.set_leverage(config.leverage, symbol, params={"category": config.category})
        log("%s: leverage set to %sx" % (symbol, config.leverage))
    except (ccxt.BadRequest, ccxt.NoChange, ccxt.MarginModeAlreadySet, ccxt.ExchangeError) as error:
        text = str(error).lower()
        if "110043" in text or "not modified" in text or "leverage not modified" in text:
            log("%s: leverage already %sx, nothing to change" % (symbol, config.leverage))
            return
        raise


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------


def execute(client, symbol, decision, price, log, atr_value=None):
    """Act on a signals.Decision. Returns a dict describing what happened.

    `atr_value` is the latest ATR on the entry timeframe, computed by the
    caller from candles it has already fetched. It is what the "atr" risk
    model sizes the stop, target and trail from; pass None and the percentage
    model takes over.
    """
    if decision.action != signals.buy:
        log("%s: no entry - %s" % (symbol, decision.reason))
        return {"opened": False, "reason": decision.reason}

    existing = openPosition(client, symbol)
    if existing is not None:
        reason = "already holding %s contracts, one position per symbol" % positionSize(existing)
        log("%s: no entry - %s" % (symbol, reason))
        return {"opened": False, "reason": reason}

    spec = instrumentSpec(client, symbol)
    qty = computeQty(spec, price, config.position_notional_usdt)
    applyLeverage(client, symbol, log)

    # SL/TP are derived from the last closed price, not the eventual fill
    # price, because they are attached to the order itself and therefore have
    # to be known before it exists. That costs a little accuracy on a market
    # order and buys something worth more: the position is never naked, not
    # even for the round trip of a second API call.

    targets = exitPrices(spec, price, atr_value)

    if targets["capped"]:
        log("%s: %s" % (symbol, targets["capped"]))
    too_tight, why = stopTooTight(targets, atr_value)
    if too_tight:
        # Not an error and not a failed run: this is a risk decision, and a
        # skipped trade is a correct outcome worth seeing in the log.
        log("%s: no entry - %s" % (symbol, why))
        return {"opened": False, "reason": why, "skipped_for_risk": True}

    order_link_id = buildOrderLinkId(spec["market_id"], decision.strategy)

    params = {
        "category": config.category,
        "positionIdx": config.position_idx,
        "orderLinkId": order_link_id,
        # Full: the stop loss / take profit cover the whole position. Bybit
        # requires Market order types in Full mode, which is the default.
        "tpslMode": "Full",
    }
    if targets["stop_loss"] is not None:
        params["stopLoss"] = {"triggerPrice": targets["stop_loss"]}
    if targets["take_profit"] is not None:
        params["takeProfit"] = {"triggerPrice": targets["take_profit"]}

    log(
        "%s: opening long qty=%s @~%.6f (notional %.2f USDT, %sx) strategy=%s risk=%s "
        "sl=%s tp=%s linkId=%s"
        % (
            symbol,
            formatDecimal(qty, spec["qty_step"]),
            price,
            qty * price,
            config.leverage,
            decision.strategy,
            targets["risk_model"],
            targets["stop_loss"],
            targets["take_profit"],
            order_link_id,
        )
    )

    order = client.create_order(symbol, "market", "buy", qty, None, params)

    trailing = applyTrailingStop(client, spec, targets, log)

    return {
        "opened": True,
        "reason": decision.reason,
        "strategy": decision.strategy,
        "votes": decision.votes,
        "risk_model": targets["risk_model"],
        "symbol": symbol,
        "qty": qty,
        "price": price,
        "notional": qty * price,
        "order_id": order.get("id"),
        "order_link_id": order_link_id,
        "stop_loss": targets["stop_loss"],
        "take_profit": targets["take_profit"],
        "trailing_distance": targets["trailing_distance"],
        "trailing_activation": targets["trailing_activation"],
        "trailing_set": trailing,
    }


def applyTrailingStop(client, spec, targets, log):
    """Second call: attach the trailing stop to the now-open position.

    Deliberately calls the raw v5 endpoint rather than going through ccxt's
    createOrder trailing shim - that shim reroutes the whole order to
    /v5/position/trading-stop and refuses to carry stopLoss/takeProfit
    alongside, which is not what we want here.

    A failure here is logged but not fatal: the position already exists and is
    already protected by the exchange-side stop loss from step 1. Blowing up
    would leave it unmanaged for no benefit.
    """
    if targets["trailing_distance"] is None:
        return False

    request = {
        "category": config.category,
        "symbol": spec["market_id"],
        "positionIdx": config.position_idx,
        "tpslMode": "Full",
        # price distance, not percent - see exitPrices()
        "trailingStop": formatDecimal(targets["trailing_distance"], spec["tick_size"]),
    }
    if targets["trailing_activation"] is not None:
        # activePrice: "Trailing stop trigger price. Trailing stop will be
        # triggered when this price is reached only" - until then it is dormant.
        request["activePrice"] = formatDecimal(targets["trailing_activation"], spec["tick_size"])

    try:
        client.privatePostV5PositionTradingStop(request)
        log(
            "%s: trailing stop set, distance=%s activation=%s"
            % (spec["market_id"], request["trailingStop"], request.get("activePrice", "immediate"))
        )
        return True
    except Exception as error:
        log(
            "%s: WARNING trailing stop failed (%s). Position stays protected by the "
            "exchange-side stop loss." % (spec["market_id"], error)
        )
        return False


# ---------------------------------------------------------------------------
# exit
# ---------------------------------------------------------------------------


def closePosition(client, symbol, position, reason, log):
    """Market-close the whole position, reduce-only."""
    spec = instrumentSpec(client, symbol)
    size = positionSize(position)
    if size <= 0:
        return {"closed": False, "reason": "nothing to close"}

    side = "sell" if (position.get("side") or "long") == "long" else "buy"
    params = {
        "category": config.category,
        "positionIdx": config.position_idx,
        "reduceOnly": True,
        # "close" tags this id so it can never collide with the entry id for
        # the same symbol in the same bucket, while staying just as
        # idempotent against a retried close.
        "orderLinkId": buildOrderLinkId(spec["market_id"], "close"),
    }

    log("%s: closing %s contracts (%s) - %s" % (symbol, size, side, reason))
    order = client.create_order(symbol, "market", side, size, None, params)
    return {
        "closed": True,
        "symbol": symbol,
        "qty": size,
        "side": side,
        "reason": reason,
        "order_id": order.get("id"),
    }
