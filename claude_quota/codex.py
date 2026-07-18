"""Codex provider: read the newest server-issued rate-limit snapshot from local
Codex CLI rollout files.

Codex echoes the server's own rate-limit state into every session's rollout
JSONL (~/.codex/sessions/**/rollout-*.jsonl, token_count events with a
"rate_limits" object: primary/secondary windows with used_percent,
window_minutes, resets_at epoch, plus plan_type and credits). That makes the
exact source LOCAL - no token, no endpoint, no rate-limit risk. The only cost
is freshness: the snapshot is only as current as Codex's last activity, so the
result always carries snapshot_age_seconds and callers should surface it.

Note: rollout files are also known to replay/inflate token SUMS on fork/resume;
that failure mode does not apply here because we read the latest snapshot,
never an aggregate.
"""

import json
import os
import time
from datetime import datetime, timezone

CODEX_HOME = os.path.join(os.path.expanduser("~"), ".codex")
SESSION_DIRS = ("sessions", "archived_sessions")
MAX_FILES_TO_SCAN = 20  # newest-first; the first file with a snapshot wins

WINDOW_KINDS = {300: "session", 10080: "weekly_all"}


def _rollout_files_newest_first():
    files = []
    for base in SESSION_DIRS:
        root = os.path.join(CODEX_HOME, base)
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(".jsonl"):
                    path = os.path.join(dirpath, name)
                    try:
                        files.append((os.path.getmtime(path), path))
                    except OSError:
                        continue
    files.sort(reverse=True)
    return [path for _mtime, path in files[:MAX_FILES_TO_SCAN]]


def _find_rate_limits(obj):
    """Depth-first search for a 'rate_limits' dict anywhere in a parsed line."""
    if isinstance(obj, dict):
        found = obj.get("rate_limits")
        if isinstance(found, dict):
            return found
        for value in obj.values():
            hit = _find_rate_limits(value)
            if hit:
                return hit
    elif isinstance(obj, list):
        for value in obj:
            hit = _find_rate_limits(value)
            if hit:
                return hit
    return None


def _latest_snapshot():
    """Return (rate_limits, line_timestamp_iso) from the newest rollout that has one."""
    for path in _rollout_files_newest_first():
        last = None
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    if '"rate_limits"' not in raw:
                        continue
                    try:
                        line = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    limits = _find_rate_limits(line)
                    if limits:
                        last = (limits, line.get("timestamp"))
        except OSError:
            continue
        if last:
            return last
    return None, None


def _window_entry(kind_hint, window):
    if not isinstance(window, dict) or window.get("used_percent") is None:
        return None
    minutes = window.get("window_minutes")
    kind = WINDOW_KINDS.get(minutes, kind_hint)
    resets_epoch = window.get("resets_at")
    entry = {
        "kind": kind,
        "percent_used": window.get("used_percent"),
        "window_minutes": minutes,
        "severity": "normal",
        "model": None,
        "is_active": True,
    }
    if isinstance(resets_epoch, (int, float)):
        entry["resets_at"] = datetime.fromtimestamp(
            resets_epoch, tz=timezone.utc).isoformat()
        entry["minutes_until_reset"] = max(0, int((resets_epoch - time.time()) // 60))
    return entry


def codex_quota_status():
    """Latest Codex rate-limit state, normalized to the claude-quota shape.

    Returns None when no snapshot exists (Codex never ran here / logs purged).
    """
    limits_raw, stamp = _latest_snapshot()
    if not limits_raw:
        return None
    result = {
        "source": "codex-rollout-snapshot",
        "provider": "codex",
        "plan_type": limits_raw.get("plan_type"),
        "raw": limits_raw,
    }
    if stamp:
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            result["snapshot_at"] = when.isoformat()
            result["snapshot_age_seconds"] = max(
                0, int((datetime.now(timezone.utc) - when).total_seconds()))
        except (ValueError, AttributeError):
            pass
    entries = []
    primary = _window_entry("primary", limits_raw.get("primary"))
    if primary:
        entries.append(primary)
    secondary = _window_entry("secondary", limits_raw.get("secondary"))
    if secondary:
        entries.append(secondary)
    if entries:
        result["limits"] = entries
    credits = limits_raw.get("credits")
    if isinstance(credits, dict):
        result["credits"] = credits
    if limits_raw.get("rate_limit_reached_type"):
        result["rate_limit_reached"] = limits_raw["rate_limit_reached_type"]
        for entry in entries:
            entry["severity"] = "exceeded"
    return result
