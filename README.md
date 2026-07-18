# claude-quota-mcp

**Let your agent check its own Claude usage limits — so it can plan long-running
work around resets instead of dying mid-task.**

Every Claude usage monitor out there renders for human eyeballs: statuslines,
tray apps, terminal dashboards. This is the other direction — a single MCP tool
the *agent* calls to answer questions like:

- "The session window resets in 40 minutes and I'm only at 30% — front-load the
  heavy work now."
- "The weekly Opus window is at 85% — downshift to a cheaper model before the
  wall, not after."
- "Quota resets before this migration would finish — schedule the second half
  for the next window."

## What it returns

Exact, server-side numbers — the same data behind Claude Code's `/usage` — for
every limit window on the account:

| Window | Used | Resets in | Severity |
|---|---|---|---|
| 5-hour session | 17% | 4h 36m | normal |
| Weekly (all models) | 5% | 2d 3h | normal |
| Weekly (Opus) | 8% | 2d 3h | normal |

Why exact matters: local-transcript estimators (the ccusage "blocks" method)
can only guess percent-used against your own historical maximum. On the machine
this was built on, that estimate said **0.7%** when the real figure was **13%**.
Reset *times* reconstruct fine locally; percentages don't.

## Install

Requires a logged-in [Claude Code](https://code.claude.com) (Pro/Max) on the
same machine — the tool reuses its OAuth token, read-only, and talks only to
`api.anthropic.com`.

```bash
claude mcp add quota -- uvx claude-quota-mcp
```

Or in any MCP host's config:

```json
{
  "mcpServers": {
    "quota": { "command": "uvx", "args": ["claude-quota-mcp"] }
  }
}
```

Non-MCP agents and scripts can shell out instead:

```bash
python -m claude_quota          # exact, JSON
python -m claude_quota --local  # force the local estimate
```

## The hook: quota awareness the agent can't forget

A tool the agent must remember to call gets forgotten exactly when it matters —
deep in a long task. The included **quota-guard hook** inverts that: quota state
is pushed into context instead of pulled.

- **SessionStart** — always injects a one-line baseline
  (`5h session 23% (resets 4h 11m); weekly 5% ...`).
- **UserPromptSubmit** — silent in the common case; speaks only when a window
  crosses a threshold (default 70%), severity escalates, or there's an
  *opportunity*: the session window resets soon with usage to spare — unused
  quota expires at reset, so it nudges the agent to front-load heavy work.
  Repeats are throttled per session.

Claude Code `settings.json`:

```json
"hooks": {
  "SessionStart":     [{ "hooks": [{ "type": "command", "command": "python -m claude_quota.hook" }] }],
  "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python -m claude_quota.hook" }] }]
}
```

Tune with env vars: `QUOTA_HOOK_WARN_PERCENT` (70), `QUOTA_HOOK_OPPORTUNITY_MINUTES`
(60), `QUOTA_HOOK_OPPORTUNITY_MAX_PERCENT` (50), `QUOTA_HOOK_REMIND_MINUTES` (30).
The 180s disk cache makes per-prompt frequency free — no extra API calls.

Every emitted line carries the current local time (`Now Fri 2026-07-18 15:04`) —
agents have no clock, and "resets in 40m" is meaningless without one. Set
`QUOTA_HOOK_TIME_ALWAYS=1` to also emit a minimal `<now>` tag on every prompt
even when quota has nothing to say (~10 tokens of temporal grounding; default off).

## Codex too

The same tool reports OpenAI Codex limits (`provider: "codex"`, default `"all"`
covers both). Codex CLI echoes the server's own rate-limit state into its local
rollout logs (`~/.codex/sessions/**/rollout-*.jsonl`), so the exact numbers are
read straight from disk - no token, no endpoint, no rate-limit risk. Freshness
equals Codex's last activity and is always reported (`snapshot_age_seconds`).

To let Codex check its own quota, register the server in `~/.codex/config.toml`:

```toml
[mcp_servers.quota]
command = "python"
args = ["-m", "claude_quota.server"]
```

## Design notes

- **One tool, tiny schema.** MCP servers tax every message with their tool
  schemas; this one adds a single tool with one optional parameter.
- **429-safe by construction.** The usage endpoint rate-limits aggressively per
  token. Responses are disk-cached (180s TTL, shared across processes), and any
  network failure serves stale cache before falling back — so wiring this into
  a statusline or a polling loop won't get your token throttled.
- **Graceful degradation.** No token, endpoint change, offline: falls back to a
  local transcript reconstruction (times exact, percent estimated) and *says
  so* in the output. It never fails silently into wrong numbers.
- **Token discovery** covers `CLAUDE_CODE_OAUTH_TOKEN`,
  `~/.claude/.credentials.json` (Windows/Linux), and the macOS keychain.

## Caveats, honestly

The usage endpoint is **undocumented** (`api.anthropic.com/api/oauth/usage`,
`anthropic-beta: oauth-2025-04-20`). Anthropic could change or remove it any
time; the local fallback keeps the tool alive if that happens. Community tools
have used this endpoint for a while (statuslines, monitors), but treat it as
best-effort, not a contract.

Your OAuth token is read from local Claude Code storage and sent only to
Anthropic's own API — the same thing Claude Code itself does on every request.
Audit `claude_quota/quota.py`; it's ~250 lines of stdlib.

## Why this exists

Built as a side quest of a multi-agent orchestration experiment: agents
coordinating over a shared bus needed to decide *when* to push hard and when to
wind down a lane — which turns out to require knowing your own quota. Write-up
coming.

MIT licensed.
