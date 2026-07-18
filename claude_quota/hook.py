"""Quota-guard hook: pushes quota state to the agent so it never forgets to check.

A tool the agent must remember to call gets forgotten exactly when it matters --
deep in a long task. Wire this script into the host's hook system instead and
quota awareness becomes ambient:

- SessionStart      -> always injects a one-line quota baseline.
- UserPromptSubmit  -> SILENT unless something deserves attention:
    * any window's severity is elevated (not "normal"), or
    * any window's percent-used crosses QUOTA_HOOK_WARN_PERCENT (default 70), or
    * opportunity: the 5h window resets within QUOTA_HOOK_OPPORTUNITY_MINUTES
      (default 60) with usage under QUOTA_HOOK_OPPORTUNITY_MAX_PERCENT
      (default 50) -- i.e. quota about to be wasted, push heavy work now.
  Repeats are throttled per session (QUOTA_HOOK_REMIND_MINUTES, default 30)
  unless severity or the 10-percent band changes.

Every line the hook prints carries the current local time ("Now Fri 2026-07-18
15:04") -- agents have no clock, and quota resets are meaningless without one.
Set QUOTA_HOOK_TIME_ALWAYS=1 to additionally emit a minimal <now> tag on every
UserPromptSubmit even when quota has nothing to say (temporal grounding at
~10 tokens per prompt; default off to keep the common case free).

Claude Code settings.json:
    "SessionStart":     [{"hooks": [{"type": "command", "command": "python -m claude_quota.hook"}]}]
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python -m claude_quota.hook"}]}]

Reads hook JSON on stdin (hook_event_name, session_id). The OAuth response is
disk-cached (180s), so per-prompt frequency costs no extra API calls. Exits 0
and prints nothing on ANY internal failure -- it must never break the host.
"""

import json
import os
import sys
import tempfile
import time

from claude_quota.codex import codex_quota_status
from claude_quota.quota import quota_status

WARN_PERCENT = float(os.environ.get("QUOTA_HOOK_WARN_PERCENT", "70"))
OPPORTUNITY_MINUTES = int(os.environ.get("QUOTA_HOOK_OPPORTUNITY_MINUTES", "60"))
OPPORTUNITY_MAX_PERCENT = float(os.environ.get("QUOTA_HOOK_OPPORTUNITY_MAX_PERCENT", "50"))
REMIND_MINUTES = int(os.environ.get("QUOTA_HOOK_REMIND_MINUTES", "30"))
# "1": emit a minimal <now> timestamp on every UserPromptSubmit, even when the
# quota picture doesn't warrant speaking. Default off: time rides along only on
# lines the hook was going to print anyway.
TIME_ALWAYS = os.environ.get("QUOTA_HOOK_TIME_ALWAYS", "") == "1"


def _now_str():
    return time.strftime("%a %Y-%m-%d %H:%M")


def _state_path(session_id):
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")[:64]
    return os.path.join(base, "claude-quota-mcp", f"hook-state-{safe}.json")


def _load_state(session_id):
    try:
        with open(_state_path(session_id), encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _save_state(session_id, state):
    path = _state_path(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    except Exception:
        pass


def _fmt_reset(minutes):
    if not isinstance(minutes, int):
        return "?"
    if minutes >= 2880:
        return f"{minutes // 1440}d {(minutes % 1440) // 60}h"
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"


def _window_name(lim):
    labels = {"session": "5h session", "weekly_all": "weekly",
              "weekly_scoped": "weekly"}
    name = labels.get(lim.get("kind"), lim.get("kind") or "?")
    if lim.get("model"):
        name += f" ({lim['model']})"
    return name


def _summary_line(limits):
    parts = [f"{_window_name(l)} {l.get('percent_used', '?')}% "
             f"(resets {_fmt_reset(l.get('minutes_until_reset'))})" for l in limits]
    return "; ".join(parts)


def _analyze(limits):
    """Return (alerts, opportunity, session_limit) for the trigger decision."""
    alerts = []
    opportunity = None
    session = None
    for lim in limits:
        pct = lim.get("percent_used")
        sev = (lim.get("severity") or "normal").lower()
        if lim.get("kind") == "session":
            session = lim
        if sev != "normal":
            alerts.append(f"{_window_name(lim)} severity is '{sev}' at {pct}%, "
                          f"resets in {_fmt_reset(lim.get('minutes_until_reset'))}")
        elif isinstance(pct, (int, float)) and pct >= WARN_PERCENT:
            alerts.append(f"{_window_name(lim)} at {pct}% used, resets in "
                          f"{_fmt_reset(lim.get('minutes_until_reset'))}")
    if session:
        pct = session.get("percent_used")
        mins = session.get("minutes_until_reset")
        if (isinstance(pct, (int, float)) and isinstance(mins, int)
                and mins <= OPPORTUNITY_MINUTES and pct <= OPPORTUNITY_MAX_PERCENT):
            opportunity = (f"5h window resets in {_fmt_reset(mins)} with only {pct}% "
                           f"used -- unused quota expires at reset; a good moment to "
                           f"front-load heavy work.")
    return alerts, opportunity, session


def _emit_quota(event, session_id, limits, label, suffix=""):
    """Shared SessionStart/UserPromptSubmit emission for any provider."""
    alerts, opportunity, session = _analyze(limits)

    if event == "SessionStart":
        print(f"<quota-guard>Now {_now_str()}. {label}: {_summary_line(limits)}"
              f"{suffix}. Plan long-running work around these resets.</quota-guard>")
        return

    if not alerts and not opportunity:
        # The common case: no quota message. Time tag only if opted in.
        if event == "UserPromptSubmit" and TIME_ALWAYS:
            print(f"<now>{_now_str()}</now>")
        return

    # Throttle repeats: re-emit only if the picture changed or enough time passed.
    state = _load_state(session_id)
    now = time.time()
    band = int((session or {}).get("percent_used") or 0) // 10
    severities = ",".join(sorted((l.get("severity") or "normal") for l in limits))
    fingerprint = f"{band}|{severities}|{bool(opportunity)}"
    if (state.get("fingerprint") == fingerprint
            and now - state.get("emitted_at", 0) < REMIND_MINUTES * 60):
        if event == "UserPromptSubmit" and TIME_ALWAYS:
            print(f"<now>{_now_str()}</now>")
        return
    _save_state(session_id, {"fingerprint": fingerprint, "emitted_at": now})

    lines = [f"<quota-guard>Now {_now_str()}."]
    for alert in alerts:
        lines.append(f"LIMIT: {alert}{suffix}. Pace or checkpoint work accordingly.")
    if opportunity:
        lines.append(f"OPPORTUNITY: {opportunity}")
    lines.append("</quota-guard>")
    print(chr(10).join(lines))


def _run_codex(event, session_id):
    """Codex provider: exact numbers from local rollout logs, no network."""
    status = codex_quota_status()
    if not status or not status.get("limits"):
        if event == "UserPromptSubmit" and TIME_ALWAYS:
            print(f"<now>{_now_str()}</now>")
        return
    age = status.get("snapshot_age_seconds")
    suffix = ""
    if isinstance(age, int) and age > 600:
        suffix = f" (snapshot {_fmt_reset(age // 60)} old)"
    _emit_quota(event, f"codex-{session_id}", status["limits"],
                "Codex usage limits", suffix)


def main():
    event = ""
    session_id = ""
    provider = (os.environ.get("QUOTA_HOOK_PROVIDER") or "claude").lower()
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if raw:
        try:
            data = json.loads(raw)
            event = data.get("hook_event_name") or ""
            session_id = data.get("session_id") or ""
        except json.JSONDecodeError:
            pass
    # CLI overrides for testing/wiring:
    #   python -m claude_quota.hook SessionStart --provider=codex --time-always
    global TIME_ALWAYS
    for arg in sys.argv[1:]:
        if arg.startswith("--provider="):
            provider = arg.split("=", 1)[1].lower()
        elif arg == "--time-always":
            TIME_ALWAYS = True
        else:
            event = arg

    if provider == "codex":
        _run_codex(event, session_id)
        return

    status = quota_status(prefer_exact=True)
    if status.get("source") != "oauth-exact":
        # Estimates are too weak to nag about; give a baseline only at start.
        if event == "SessionStart" and status.get("active_window"):
            print(f"<quota-guard>Now {_now_str()}. 5h window (local estimate) resets at "
                  f"{status.get('resets_at')} ({status.get('minutes_remaining')} min). "
                  f"Exact source unavailable.</quota-guard>")
        elif event == "UserPromptSubmit" and TIME_ALWAYS:
            print(f"<now>{_now_str()}</now>")
        return
    limits = status.get("limits") or []
    if not limits:
        if event == "UserPromptSubmit" and TIME_ALWAYS:
            print(f"<now>{_now_str()}</now>")
        return

    _emit_quota(event, session_id, limits, "Usage limits")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never break the host
    sys.exit(0)
