"""
Push notifications via ntfy.sh.

No account, no API key. The topic name IS the only thing standing between
your notifications and the public, because anyone who knows a topic can read
and post to it. So the topic must be a long random string, it lives in GitHub
Secrets, and it is never printed to the logs.
"""

import requests

import config


def enabled():
    return bool(config.ntfy_topic)


def scrub(text):
    """Strip the topic out of any text before it reaches a log.

    Necessary because requests puts the full request URL into its exception
    messages, and that URL contains the topic. GitHub masks secrets in logs,
    but relying on that alone is thin: masking only covers values that arrived
    through the secrets context, and CI logs on a public repo are the last
    place to find out it did not.
    """
    text = str(text)
    if config.ntfy_topic:
        text = text.replace(config.ntfy_topic, "<topic>")
    return text


def push(title, message, priority="default", tags=None):
    """Send one notification. Never raises - a dead notifier must not kill a
    trading run, it just gets logged."""
    if not enabled():
        print("[notify] NTFY_TOPIC is not set, skipping push: %s" % title)
        return False

    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)

    try:
        response = requests.post(
            "%s/%s" % (config.ntfy_server.rstrip("/"), config.ntfy_topic),
            data=message.encode("utf-8"),
            headers=headers,
            timeout=config.request_timeout_seconds,
        )
        if response.status_code >= 400:
            print("[notify] push rejected with HTTP %s" % response.status_code)
            return False
        return True
    except Exception as error:
        print("[notify] push failed: %s: %s" % (type(error).__name__, scrub(error)))
        return False


def positionOpened(result):
    lines = [
        "qty %s @ %.6f" % (result["qty"], result["price"]),
        "notional %.2f USDT" % result["notional"],
    ]
    if result.get("strategy"):
        votes = result.get("votes") or []
        # Naming the strategy on the phone matters more once several run at
        # once: "which rule opened this" is the first thing you want to know,
        # and it is also what decides when the position will be let go.
        lines.append(
            "strategy %s%s"
            % (result["strategy"],
               (" (agreed: %s)" % ", ".join(votes)) if len(votes) > 1 else "")
        )
    if result.get("risk_model"):
        lines.append("risk %s" % result["risk_model"])
    if result.get("stop_loss") is not None:
        lines.append("SL %.6f" % result["stop_loss"])
    if result.get("take_profit") is not None:
        lines.append("TP %.6f" % result["take_profit"])
    if result.get("trailing_distance") is not None:
        state = "armed" if result.get("trailing_set") else "FAILED TO SET"
        lines.append("trail %.6f (%s)" % (result["trailing_distance"], state))
    lines.append("why: %s" % result["reason"])
    return push(
        "Opened %s" % result["symbol"],
        "\n".join(lines),
        priority="high",
        tags=["chart_with_upwards_trend"],
    )


def positionClosed(record):
    pnl = record.get("closed_pnl")
    direction = "up" if (pnl or 0) >= 0 else "down"
    lines = [
        "qty %s" % record.get("qty"),
        "entry %s -> exit %s" % (record.get("avg_entry"), record.get("avg_exit")),
        "pnl %s USDT" % pnl,
        # A loss means something different after a stop loss than after a
        # liquidation or a manual close, and the phone is where it is read.
        "why: %s" % record.get("cause"),
    ]
    return push(
        "Closed %s" % record.get("symbol"),
        "\n".join(lines),
        priority="high",
        tags=["chart_with_%swards_trend" % direction],
    )


def strategyExit(result):
    owner = result.get("strategy")
    return push(
        "Exit signal %s%s" % (result["symbol"], (" [%s]" % owner) if owner else ""),
        "closed %s contracts\nwhy: %s" % (result["qty"], result["reason"]),
        priority="high",
        tags=["outbox_tray"],
    )


def criticalError(message):
    return push("Bot error", message, priority="urgent", tags=["rotating_light"])
