"""
Step 2 of the build order: prove a push actually reaches your phone.
Touches no exchange API.

    python scripts/test_ntfy.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import notify


def main():
    if not notify.enabled():
        print("NTFY_TOPIC is not set")
        return 1

    # never print the topic itself - anyone who sees it can read your alerts
    print("server:      %s" % config.ntfy_server)
    print("topic:       (%d chars, hidden)" % len(config.ntfy_topic))

    ok = notify.push(
        "Claude-Finance test",
        "If this arrived on your phone, notifications are wired up correctly.",
        priority="default",
        tags=["white_check_mark"],
    )
    print("push sent:   %s" % ok)
    if not ok:
        return 1
    print("\nsubscribe to the topic in the ntfy app if you have not already")
    return 0


if __name__ == "__main__":
    sys.exit(main())
