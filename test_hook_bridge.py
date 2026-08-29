"""Tests for the quota-guard reset-bridging tiers (80% seconds, 95% checkpoint).

Drives the emitter with synthetic limit dicts so no network or OAuth token is
involved. Run: python test_hook_bridge.py
"""

import io
import os
import re
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

# The hook snapshots QUOTA_HOOK_* env into module constants at import time, so
# clear them first: these tests assert the shipped defaults, and a developer's
# own settings.json env (QUOTA_HOOK_WARN_PERCENT=101, TIME_ALWAYS=1, ...) would
# otherwise silently rewrite the thresholds under test.
for _key in [k for k in os.environ if k.startswith("QUOTA_HOOK_")]:
    del os.environ[_key]

from claude_quota import hook  # noqa: E402  (must follow the env scrub)

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"PASS {label}")
    else:
        print(f"FAIL {label} {detail}")
        FAILURES.append(label)


def session_limit(percent, minutes_out, severity="normal"):
    resets_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes_out)).isoformat()
    return {"kind": "session", "percent_used": percent, "severity": severity,
            "resets_at": resets_at, "minutes_until_reset": minutes_out}


def emit(limits, event="UserPromptSubmit", session_id=None):
    """Capture one emission, with a unique session id to dodge the throttle."""
    session_id = session_id or f"test-{datetime.now(timezone.utc).timestamp()}"
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        hook._emit_quota(event, session_id, limits, "Usage limits")
    return buffer.getvalue()


def main():
    # Below the bridge tier: human span only, no seconds, no directive.
    out = emit([session_limit(72, 150)])
    check("70-80%: warns without seconds", "72%" in out and "(9" not in out and "BRIDGE" not in out, out)

    # At the bridge tier: seconds appear alongside the human span.
    out = emit([session_limit(81, 150, "warning")])
    check("80%+: reset carries exact seconds", "2h 30m (" in out and "s)" in out, out)
    check("80%+: no jump instruction yet", "BRIDGE" not in out, out)

    # At the jump tier: a checkpoint warning with the seconds remaining, and
    # explicitly NO sleep instruction -- native autoContinueAtUsageLimit owns
    # the waiting, and agent activity cancels it.
    out = emit([session_limit(96, 150, "critical")])
    check("95%+: emits BRIDGE directive", "BRIDGE:" in out, out)
    check("95%+: tells the agent to checkpoint", "checkpoint it" in out, out)
    # Allow a couple of seconds of drift: the reset stamp is sub-second precise,
    # so 150 minutes out reads as 8999s rather than a clean 9000s.
    secs_match = re.search(r"reset is (\d+)s away", out)
    expected = 150 * 60
    check("95%+: names the seconds until reset",
          bool(secs_match) and abs(int(secs_match.group(1)) - expected) <= 3,
          f"expected ~{expected}s, got {secs_match.group(1) if secs_match else 'none'}")
    check("95%+: forbids rather than instructs the reset jump",
          "Do NOT run a background sleep" in out
          and "JUMP THE RESET" not in out
          and not re.search(r"sleep of \d+s", out), out)
    check("95%+: points at the native setting",
          "autoContinueAtUsageLimit" in out, out)

    # A weekly window alone must never produce a bridge instruction.
    weekly = {"kind": "weekly_all", "percent_used": 99, "severity": "critical",
              "resets_at": (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat(),
              "minutes_until_reset": 1200}
    out = emit([weekly])
    check("weekly-only pressure never suggests bridging", "BRIDGE" not in out, out)

    # Throttle must not swallow the jump tier within one 10-percent band.
    same_session = "throttle-band-test"
    first = emit([session_limit(91, 100, "critical")], session_id=same_session)
    second = emit([session_limit(96, 95, "critical")], session_id=same_session)
    check("90->96 in one band still delivers BRIDGE",
          "BRIDGE" not in first and "BRIDGE:" in second, f"first={first!r} second={second!r}")

    # SessionStart at high usage carries the directive immediately.
    out = emit([session_limit(97, 60, "critical")], event="SessionStart")
    check("SessionStart at 97% includes BRIDGE", "BRIDGE:" in out, out)

    # Past-reset / unparseable stamps must not produce a nonsense sleep.
    broken = {"kind": "session", "percent_used": 99, "severity": "critical",
              "resets_at": "not-a-timestamp", "minutes_until_reset": 0}
    out = emit([broken])
    check("unusable reset time produces no bridge instruction", "BRIDGE" not in out, out)

    if FAILURES:
        print(f"\n{len(FAILURES)} test(s) failed: {', '.join(FAILURES)}")
        return 1
    print("\nAll quota bridge-tier tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
