"""Documented quota source: the Claude Code statusline payload.

Claude Code pipes a JSON document to the configured statusline command on
every render. For claude.ai Pro and Max subscribers that document carries a
`rate_limits` object with two windows, `five_hour` and `seven_day`, each giving
`used_percentage` (0-100) and `resets_at` (Unix epoch seconds). The fields are
documented at https://code.claude.com/docs/en/statusline and need no
credential and no network call.

This module does two things:

1. `write_snapshot(stdin_data)` persists those fields to a small local file,
   atomically, so other processes (the MCP server, the hook, a dashboard) can
   read the latest numbers without being a statusline themselves.
2. `read_snapshot()` returns them in the claude-quota limit shape, with the
   snapshot age so callers can label stale data instead of trusting it.

Entry point `claude-quota-statusline` (also `python -m claude_quota.statusline`)
reads stdin, writes the snapshot and prints a compact one-line status, so it
can be the statusline command on its own. Pass `--quiet` to print nothing and
chain it in front of an existing statusline script.

Limits of the source, stated plainly: only the two account-wide windows (no
per-model weekly window), only while Claude Code has rendered a statusline
since its first API response, and only as fresh as the last render.

Stdlib only.
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

SOURCE = "statusline-snapshot"
DEFAULT_MAX_AGE_SECONDS = 6 * 3600  # older than this is not a current picture
STALE_AFTER_SECONDS = 15 * 60       # label, do not drop

WINDOWS = (
    ("five_hour", "session"),
    ("seven_day", "weekly_all"),
)


def snapshot_path():
    override = os.environ.get("CLAUDE_QUOTA_SNAPSHOT")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "claude-quota-mcp", "statusline-snapshot.json")


def _severity(percent):
    if not isinstance(percent, (int, float)):
        return "unknown"
    if percent >= 95:
        return "critical"
    if percent >= 80:
        return "warning"
    return "normal"


def extract_rate_limits(stdin_data):
    """Return {'five_hour': {...}, 'seven_day': {...}} or None if absent.

    Only keeps the documented fields. Anything else in the payload (session id,
    cwd, model, context window) is deliberately not persisted.
    """
    rate_limits = (stdin_data or {}).get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None
    kept = {}
    for key, _kind in WINDOWS:
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        entry = {}
        if isinstance(window.get("used_percentage"), (int, float)):
            entry["used_percentage"] = window["used_percentage"]
        if isinstance(window.get("resets_at"), (int, float)):
            entry["resets_at"] = int(window["resets_at"])
        if entry:
            kept[key] = entry
    return kept or None


def write_snapshot(stdin_data, path=None, now=None):
    """Persist the documented rate-limit fields atomically. Returns the kept dict or None."""
    kept = extract_rate_limits(stdin_data)
    if not kept:
        return None
    path = path or snapshot_path()
    payload = {"written_at": now if now is not None else time.time(),
               "source": "claude-code-statusline", "rate_limits": kept}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="snapshot-", suffix=".json", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except OSError:
        return None  # a statusline must never fail; the reader simply sees no snapshot
    return kept


def read_snapshot(path=None, max_age_seconds=DEFAULT_MAX_AGE_SECONDS, now=None):
    """Latest statusline numbers in the claude-quota shape, or None.

    Returns None when there is no snapshot, it is unreadable, or it is older
    than `max_age_seconds`. Between STALE_AFTER_SECONDS and max age the result
    carries `stale: True` so renderers can say so.
    """
    path = path or snapshot_path()
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    written_at = payload.get("written_at")
    rate_limits = payload.get("rate_limits")
    if not isinstance(written_at, (int, float)) or not isinstance(rate_limits, dict):
        return None
    now = now if now is not None else time.time()
    age = max(0, int(now - written_at))
    if age > max_age_seconds:
        return None
    limits = []
    for key, kind in WINDOWS:
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        entry = {"kind": kind, "percent_used": window.get("used_percentage"),
                 "severity": _severity(window.get("used_percentage")),
                 "model": None, "is_active": True}
        resets = window.get("resets_at")
        if isinstance(resets, (int, float)):
            entry["resets_at"] = datetime.fromtimestamp(resets, tz=timezone.utc).isoformat()
            entry["minutes_until_reset"] = max(0, int((resets - now) // 60))
        limits.append(entry)
    if not limits:
        return None
    result = {
        "source": SOURCE,
        "snapshot_at": datetime.fromtimestamp(written_at, tz=timezone.utc).isoformat(),
        "snapshot_age_seconds": age,
        "limits": limits,
    }
    session = next((l for l in limits if l["kind"] == "session"), None)
    if session:
        if session.get("percent_used") is not None:
            result["percent_used"] = session["percent_used"]
        if session.get("resets_at"):
            result["resets_at"] = session["resets_at"]
            result["minutes_remaining"] = session.get("minutes_until_reset")
    if age > STALE_AFTER_SECONDS:
        result["stale"] = True
    return result


def compact_line(kept):
    """'(5h) 53%  (7d) 6%' for use as a statusline on its own."""
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = (kept or {}).get(key) or {}
        pct = window.get("used_percentage")
        if isinstance(pct, (int, float)):
            parts.append(f"({label}) {pct:.0f}%")
    return "  ".join(parts)


def main():
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print("claude-quota-statusline: reads the Claude Code statusline JSON on stdin, writes the rate-limit snapshot, prints a one-line status. Flags: --quiet (print nothing), --help.")
        return
    quiet = "--quiet" in sys.argv[1:]
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    kept = write_snapshot(data)
    if not quiet:
        line = compact_line(kept)
        print(line if line else "(quota) waiting for first response")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never break the host's statusline
    sys.exit(0)
