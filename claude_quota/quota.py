"""Core quota logic: exact OAuth source with a local-reconstruction fallback.

Two sources, tried in order:

1. EXACT  - Anthropic's OAuth usage endpoint (the same data behind Claude Code's
   /usage command): per-window utilization percent, severity, and reset times for
   the 5-hour session window, the weekly window, and per-model weekly windows.
   The endpoint is undocumented and rate-limits aggressively per token, so
   responses are cached on disk (default 180s TTL) and stale cache is served on
   any failure (including 429) before giving up.

2. ESTIMATE - reconstruction from local Claude Code transcripts
   (~/.claude/projects/**/*.jsonl) using the same "blocks" method as ccusage:
   a window starts at the first message after >=5h of inactivity, floored to the
   hour, and lasts 5 hours. Percent is estimated against the largest token total
   observed in one completed historical window on this machine.

Stdlib only. Reads the local Claude Code OAuth token (credentials file, or the
macOS keychain); never sends it anywhere except api.anthropic.com.
"""

import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone

WINDOW = timedelta(hours=5)
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
HISTORY_DAYS = 30  # lookback for estimating the per-window token ceiling

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-quota-mcp"
CACHE_TTL_SECONDS = 180  # the endpoint 429s below roughly this polling interval


def _cache_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(tempfile.gettempdir())
    return os.path.join(base, "claude-quota-mcp", "oauth-cache.json")


def _read_cache():
    try:
        with open(_cache_path(), encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _write_cache(payload):
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"fetched_at": time.time(), "payload": payload}, handle)
    except Exception:
        pass  # cache is an optimization, never a failure


def _load_token():
    """Find the local Claude Code OAuth access token.

    Order: CLAUDE_CODE_OAUTH_TOKEN env var, ~/.claude/.credentials.json
    (Windows/Linux), then the macOS keychain entry Claude Code creates.
    """
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if env_token:
        return env_token
    creds_path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    try:
        with open(creds_path, encoding="utf-8") as handle:
            token = (json.load(handle).get("claudeAiOauth") or {}).get("accessToken", "")
        if token:
            return token
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            raw = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if raw:
                return (json.loads(raw).get("claudeAiOauth") or {}).get("accessToken", "")
        except Exception:
            pass
    return ""


def _minutes_until(iso_stamp, now=None):
    """Minutes from now until an ISO timestamp; None if unparseable, 0 if past."""
    try:
        when = datetime.fromisoformat(iso_stamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    seconds = (when - (now or datetime.now(timezone.utc))).total_seconds()
    return int(seconds // 60) if seconds > 0 else 0


def _parse_oauth_payload(payload, cache_age_seconds=None):
    """Extract the useful fields from the (undocumented) usage payload."""
    result = {"source": "oauth-exact"}
    if cache_age_seconds is not None:
        result["cache_age_seconds"] = int(cache_age_seconds)
    window = payload.get("five_hour") or {}
    if isinstance(window, dict):
        if window.get("utilization") is not None:
            result["percent_used"] = window["utilization"]
        if window.get("resets_at"):
            result["resets_at"] = window["resets_at"]
            result["minutes_remaining"] = _minutes_until(window["resets_at"])
    limits = payload.get("limits")
    if isinstance(limits, list):
        parsed = []
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            scope_model = None
            scope = entry.get("scope") or {}
            if isinstance(scope, dict):
                scope_model = (scope.get("model") or {}).get("display_name")
            parsed.append({
                "kind": entry.get("kind"),
                "percent_used": entry.get("percent"),
                "severity": entry.get("severity"),
                "resets_at": entry.get("resets_at"),
                "minutes_until_reset": _minutes_until(entry.get("resets_at")),
                "model": scope_model,
                "is_active": entry.get("is_active"),
            })
        if parsed:
            result["limits"] = parsed
    return result


def oauth_quota_status(max_age_seconds=CACHE_TTL_SECONDS):
    """Query the OAuth usage endpoint, honoring the disk cache.

    Fresh cache -> served without a network call. Network failure of any kind
    (including 429) -> stale cache if one exists, else None so callers fall back
    to the local reconstruction.
    """
    cached = _read_cache()
    now = time.time()
    if cached and (now - cached.get("fetched_at", 0)) < max_age_seconds:
        return _parse_oauth_payload(cached["payload"],
                                    cache_age_seconds=now - cached["fetched_at"])
    token = _load_token()
    if not token:
        return None
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": OAUTH_BETA,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except Exception:
        if cached:  # stale beats nothing, and beats hammering a 429ing endpoint
            return _parse_oauth_payload(cached["payload"],
                                        cache_age_seconds=now - cached.get("fetched_at", 0))
        return None
    _write_cache(payload)
    return _parse_oauth_payload(payload, cache_age_seconds=0)


# ---------------------------------------------------------------------------
# Fallback: local transcript reconstruction (ccusage "blocks" method)
# ---------------------------------------------------------------------------

def _iter_usage_events(since):
    """Yield (utc_datetime, total_tokens) for assistant messages newer than `since`."""
    pattern = os.path.join(PROJECTS_DIR, "*", "*.jsonl")
    since_epoch = since.timestamp()
    for path in glob.glob(pattern):
        try:
            if os.path.getmtime(path) < since_epoch:
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    if '"assistant"' not in raw[:400]:
                        continue
                    try:
                        line = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if line.get("type") != "assistant":
                        continue
                    usage = (line.get("message") or {}).get("usage") or {}
                    stamp = line.get("timestamp")
                    if not stamp or not usage:
                        continue
                    try:
                        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if when < since:
                        continue
                    tokens = (
                        (usage.get("input_tokens") or 0)
                        + (usage.get("output_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0)
                        + (usage.get("cache_read_input_tokens") or 0)
                    )
                    yield when, tokens
        except OSError:
            continue


def _split_into_blocks(events):
    """Group chronological (time, tokens) events into 5-hour blocks."""
    blocks = []
    current = None
    last_event_time = None
    for when, tokens in events:
        gap_break = last_event_time is not None and (when - last_event_time) >= WINDOW
        if current is None or when >= current["end"] or gap_break:
            start = when.replace(minute=0, second=0, microsecond=0)
            current = {"start": start, "end": start + WINDOW, "tokens": 0, "messages": 0}
            blocks.append(current)
        current["tokens"] += tokens
        current["messages"] += 1
        last_event_time = when
    return blocks


def reconstruct_quota_status(now=None):
    """Estimate the current 5-hour window from local transcripts."""
    now = now or datetime.now(timezone.utc)
    events = sorted(_iter_usage_events(now - timedelta(days=HISTORY_DAYS)))
    blocks = _split_into_blocks(events)

    active = blocks[-1] if blocks and blocks[-1]["end"] > now else None
    completed = [b for b in blocks if b is not active]
    ceiling = max((b["tokens"] for b in completed), default=0)

    if active is None:
        return {
            "source": "local-estimate",
            "active_window": False,
            "note": "No Claude Code activity within the last 5 hours; a fresh "
                    "window starts with the next message.",
            "history_blocks_scanned": len(blocks),
        }
    remaining = active["end"] - now
    percent = round(100.0 * active["tokens"] / ceiling, 1) if ceiling else None
    return {
        "source": "local-estimate",
        "active_window": True,
        "window_start": active["start"].isoformat(),
        "resets_at": active["end"].isoformat(),
        "minutes_remaining": int(remaining.total_seconds() // 60),
        "tokens_used": active["tokens"],
        "messages_in_window": active["messages"],
        "est_percent_of_historical_max": percent,
        "confidence": "estimate (local transcript reconstruction, ccusage blocks "
                      "method); times are exact, percent is vs this machine's "
                      "largest past window, not Anthropic's real cap",
    }


def quota_status(prefer_exact=True):
    """Full quota picture: exact OAuth data when available, estimate otherwise.

    Returns a dict with 'source' set to 'oauth-exact' or 'local-estimate'. When
    the exact source succeeds, the local reconstruction is attached under
    'reconstruction' as a cross-check.
    """
    exact = oauth_quota_status() if prefer_exact else None
    recon = reconstruct_quota_status()
    if exact:
        exact["reconstruction"] = recon
        return exact
    return recon
