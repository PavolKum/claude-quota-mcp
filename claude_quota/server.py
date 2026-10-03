"""claude-quota-mcp: a single-tool MCP server that lets an agent check its own
Claude usage limits, so it can plan long-running work around resets.

Transport: stdio. One tool, tiny schema footprint by design.
"""

from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from claude_quota.codex import codex_quota_status
from claude_quota.quota import quota_status

mcp = FastMCP("claude_quota")

WINDOW_LABELS = {
    "session": "5-hour session",
    "weekly_all": "Weekly (all models)",
    "weekly_scoped": "Weekly",
    "primary": "Primary window",
    "secondary": "Secondary window",
}


class QuotaInput(BaseModel):
    """Input for quota status."""
    provider: Literal["claude", "codex", "all"] = Field(
        default="all",
        description="Which agent's limits to check: 'claude' (statusline snapshot, "
                    "or the opt-in OAuth endpoint, or a transcript estimate), "
                    "'codex' (latest server-issued snapshot from local rollout "
                    "files), or 'all'.",
    )
    response_format: Literal["markdown", "json"] = Field(
        default="markdown",
        description="Output format: 'markdown' or 'json'.",
    )


def _fmt_age(seconds):
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def render_codex_markdown(status):
    if status is None:
        return ("**Codex limits: no local snapshot found** (Codex has not run "
                "on this machine, or its session logs were cleaned).")
    lines = [f"**Codex usage limits (server-issued snapshot, plan: "
             f"{status.get('plan_type', '?')}):**", ""]
    limits = status.get("limits") or []
    if limits:
        lines += ["| Window | Used | Resets in | Severity |", "|---|---|---|---|"]
        for lim in limits:
            mins_window = lim.get("window_minutes")
            name = WINDOW_LABELS.get(lim.get("kind"), lim.get("kind") or "?")
            if lim.get("kind") not in WINDOW_LABELS and isinstance(mins_window, int):
                name = f"{mins_window // 60}h window"
            mins = lim.get("minutes_until_reset")
            resets = f"{mins // 60}h {mins % 60}m" if isinstance(mins, int) else "?"
            lines.append(f"| {name} | {lim.get('percent_used', '?')}% "
                         f"| {resets} | {lim.get('severity', '?')} |")
    age = status.get("snapshot_age_seconds")
    if isinstance(age, int):
        lines += ["", f"*Snapshot from Codex's last activity {_fmt_age(age)} ago "
                      f"- refreshes whenever Codex runs.*"]
    return chr(10).join(lines)


def render_markdown(status):
    if status["source"] == "statusline-snapshot":
        lines = ["**Claude usage limits (Claude Code statusline, documented fields):**", "",
                 "| Window | Used | Resets in | Severity |", "|---|---|---|---|"]
        for lim in status.get("limits") or []:
            name = WINDOW_LABELS.get(lim.get("kind"), lim.get("kind") or "?")
            mins = lim.get("minutes_until_reset")
            resets = f"{mins // 60}h {mins % 60}m" if isinstance(mins, int) else "?"
            lines.append(f"| {name} | {lim.get('percent_used', '?')}% "
                         f"| {resets} | {lim.get('severity', '?')} |")
        age = status.get("snapshot_age_seconds")
        if isinstance(age, int):
            note = f"*Snapshot from the last statusline render {_fmt_age(age)} ago"
            note += "; STALE, Claude Code has not rendered recently.*" if status.get("stale") else ".*"
            lines += ["", note]
        lines += ["", "*Account-wide windows only; per-model weekly windows are not in the "
                      "documented statusline data.*"]
        return chr(10).join(lines)

    if status["source"] == "oauth-exact":
        lines = ["**Claude usage limits (exact, via OAuth usage endpoint):**", ""]
        limits = status.get("limits") or []
        if limits:
            lines += ["| Window | Used | Resets in | Severity |", "|---|---|---|---|"]
            for lim in limits:
                name = WINDOW_LABELS.get(lim.get("kind"), lim.get("kind") or "?")
                if lim.get("model"):
                    name += f" ({lim['model']})"
                mins = lim.get("minutes_until_reset")
                resets = f"{mins // 60}h {mins % 60}m" if isinstance(mins, int) else "?"
                lines.append(f"| {name} | {lim.get('percent_used', '?')}% "
                             f"| {resets} | {lim.get('severity', '?')} |")
        else:
            lines.append(f"5-hour window: **{status.get('percent_used', '?')}%** used, "
                         f"resets at {status.get('resets_at', '?')}.")
        age = status.get("cache_age_seconds")
        if isinstance(age, int) and age > 0:
            lines += ["", f"*Cached {age}s ago (endpoint is rate-limited; "
                          f"cache TTL 180s).*"]
        return chr(10).join(lines)

    # local-estimate fallback
    if not status.get("active_window"):
        return f"**No active session window.** {status['note']}"
    return chr(10).join([
        "**Claude 5-hour window (local estimate - exact source unavailable):**",
        "",
        f"- Resets at {status['resets_at']} ({status['minutes_remaining']} min)",
        f"- Tokens observed locally: {status['tokens_used']:,}",
        f"- Est. percent of this machine's historical max: "
        f"{status['est_percent_of_historical_max'] or 'n/a'}",
        "",
        f"*{status['confidence']}*",
    ])


@mcp.tool(
    name="quota_status",
    description=(
        "Check the agent's own usage limits so long-running work can be planned "
        "around resets - e.g. if a window resets soon with usage to spare, "
        "front-load heavy work now. Providers: 'claude' (5-hour and weekly "
        "percentages from the documented Claude Code statusline snapshot, no "
        "credential; the undocumented OAuth usage endpoint only when the host "
        "opted in via CLAUDE_QUOTA_ALLOW_OAUTH_ENDPOINT=1; transcript-estimate "
        "fallback) and 'codex' "
        "(latest server-issued rate-limit snapshot from local Codex rollout "
        "logs - exact numbers, freshness = Codex's last activity). Default "
        "'all' reports both."
    ),
)
async def quota_status_tool(params: QuotaInput) -> str:
    claude = quota_status() if params.provider in ("claude", "all") else None
    codex = codex_quota_status() if params.provider in ("codex", "all") else None
    if params.response_format == "json":
        import json
        if params.provider == "claude":
            return json.dumps(claude, indent=2)
        if params.provider == "codex":
            return json.dumps(codex, indent=2)
        return json.dumps({"claude": claude, "codex": codex}, indent=2)
    parts = []
    if claude is not None:
        parts.append(render_markdown(claude))
    if params.provider in ("codex", "all"):
        parts.append(render_codex_markdown(codex))
    return (chr(10) + chr(10)).join(parts)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
