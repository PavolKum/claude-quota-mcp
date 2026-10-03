"""Tests for the statusline snapshot source and the quota source ordering.

No network, no credential: everything runs against a temp snapshot path.
Run: python test_statusline.py
"""

import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="claude-quota-test-")
SNAP = os.path.join(TMP, "snapshot.json")
os.environ["CLAUDE_QUOTA_SNAPSHOT"] = SNAP
os.environ.pop("CLAUDE_QUOTA_ALLOW_OAUTH_ENDPOINT", None)

from claude_quota import quota, statusline  # noqa: E402  (env must be set first)

FAILURES = []


def check(name, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + name + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


NOW = time.time()
PAYLOAD = {
    "session_id": "should-not-be-persisted",
    "cwd": "C:/private/path",
    "model": {"id": "x", "display_name": "X"},
    "rate_limits": {
        "five_hour": {"used_percentage": 53.4, "resets_at": int(NOW + 2 * 3600 + 30 * 60)},
        "seven_day": {"used_percentage": 6, "resets_at": int(NOW + 3 * 86400)},
    },
}

# 1. extract keeps only documented fields
kept = statusline.extract_rate_limits(PAYLOAD)
check("extract keeps both windows", set(kept) == {"five_hour", "seven_day"})
check("extract keeps only documented keys",
      set(kept["five_hour"]) == {"used_percentage", "resets_at"})
check("extract returns None without rate_limits", statusline.extract_rate_limits({"cwd": "x"}) is None)

# 2. write is atomic and persists nothing but rate_limits
check("write returns kept dict", statusline.write_snapshot(PAYLOAD, now=NOW) == kept)
raw = json.load(open(SNAP, encoding="utf-8"))
check("snapshot has no session/cwd/model", not any(k in raw for k in ("session_id", "cwd", "model")))
check("snapshot carries written_at and source", raw["source"] == "claude-code-statusline" and raw["written_at"] == NOW)
check("no temp files left behind", [f for f in os.listdir(TMP) if f.startswith("snapshot-")] == [])

# 3. read normalises to the claude-quota shape
status = statusline.read_snapshot(now=NOW + 60)
check("read source label", status["source"] == "statusline-snapshot")
kinds = [l["kind"] for l in status["limits"]]
check("read kinds", kinds == ["session", "weekly_all"])
session = status["limits"][0]
check("read percent", session["percent_used"] == 53.4)
check("read minutes until reset", 148 <= session["minutes_until_reset"] <= 150, str(session["minutes_until_reset"]))
check("read severity normal", session["severity"] == "normal")
check("read top-level session fields", status["percent_used"] == 53.4 and status["minutes_remaining"] == session["minutes_until_reset"])
check("read not stale at 60s", not status.get("stale"))
check("read stale after 15 min", statusline.read_snapshot(now=NOW + 16 * 60).get("stale") is True)
check("read None after max age", statusline.read_snapshot(now=NOW + 7 * 3600) is None)
check("severity bands", statusline._severity(80) == "warning" and statusline._severity(95) == "critical"
      and statusline._severity(None) == "unknown")

# 4. ordering: snapshot wins, OAuth never consulted unless opted in
calls = {"oauth": 0, "recon": 0}
quota.oauth_quota_status = lambda *a, **k: calls.__setitem__("oauth", calls["oauth"] + 1) or {"source": "oauth-exact"}
quota.reconstruct_quota_status = lambda *a, **k: calls.__setitem__("recon", calls["recon"] + 1) or {"source": "local-estimate", "active_window": False, "note": "n/a"}
result = quota.quota_status()
check("quota_status prefers snapshot", result["source"] == "statusline-snapshot")
check("quota_status attaches reconstruction", result.get("reconstruction", {}).get("source") == "local-estimate")
check("quota_status did not call oauth", calls["oauth"] == 0)
check("prefer_exact=False returns estimate", quota.quota_status(prefer_exact=False)["source"] == "local-estimate")

# 5. OAuth gate: without opt-in the token is never read
os.remove(SNAP)
reads = {"token": 0}
import importlib  # noqa: E402
importlib.reload(quota)
quota._load_token = lambda: reads.__setitem__("token", reads["token"] + 1) or "tok"
quota.reconstruct_quota_status = lambda *a, **k: {"source": "local-estimate", "active_window": False, "note": "n/a"}
check("oauth disabled by default", quota.oauth_quota_status() is None and reads["token"] == 0)
check("oauth_endpoint_allowed false by default", quota.oauth_endpoint_allowed() is False)
check("no snapshot + no opt-in -> estimate", quota.quota_status()["source"] == "local-estimate")
os.environ["CLAUDE_QUOTA_ALLOW_OAUTH_ENDPOINT"] = "1"
check("oauth_endpoint_allowed true when opted in", quota.oauth_endpoint_allowed() is True)
quota._read_cache = lambda: {"fetched_at": time.time(), "payload": {"five_hour": {"utilization": 12, "resets_at": "2030-01-01T00:00:00Z"}}}
served = quota.oauth_quota_status()
check("oauth serves fresh cache when opted in", served and served["source"] == "oauth-exact" and served["percent_used"] == 12)
check("allow=False overrides env", quota.oauth_quota_status(allow=False) is None)

# 6. compact line
check("compact line", statusline.compact_line(kept) == "(5h) 53%  (7d) 6%")

print()
if FAILURES:
    print(f"{len(FAILURES)} failure(s): {FAILURES}")
    sys.exit(1)
print("all statusline tests passed")
