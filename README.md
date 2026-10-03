# claude-quota-mcp

**Let your agent check its own Claude usage limits, so it can plan long-running
work around resets instead of dying mid-task.**

Every Claude usage monitor renders for human eyeballs: statuslines, tray apps,
terminal dashboards. This is the other direction: a single MCP tool the *agent*
calls to answer questions like:

- "The session window resets in 40 minutes and I'm only at 30%. Front-load the
  heavy work now."
- "The weekly window is at 85%. Downshift to a cheaper model before the wall,
  not after."
- "Quota resets before this migration would finish. Schedule the second half
  for the next window."

## Where the numbers come from

The tool tries three sources, best first, and always says which one answered.

| Source | What it is | Credential | Network | Windows |
|---|---|---|---|---|
| **Statusline snapshot** (default) | The documented `rate_limits` fields Claude Code pipes to its statusline command (`five_hour` and `seven_day`: `used_percentage`, `resets_at`), persisted locally by `claude-quota-statusline` | none | none | 5-hour, weekly (account-wide) |
| OAuth usage endpoint (**opt-in only**) | The undocumented endpoint behind Claude Code's `/usage`, reached with your Claude Code OAuth token | your Claude Code token, read-only | `api.anthropic.com`, cached 180 s | 5-hour, weekly, per-model weekly |
| Local estimate (fallback) | Reconstruction from `~/.claude/projects/**/*.jsonl` using the ccusage "blocks" method | none | none | 5-hour, times exact, percent estimated against this machine's own history |

**Why the endpoint is off by default.** Anthropic's Claude Code legal page says
the consumer OAuth token is intended exclusively to support ordinary use of
Claude Code and other native Anthropic applications, and the endpoint is not
documented. Anthropic publishes no usage or quota API for individual Pro or Max
subscriptions. The statusline fields, by contrast, are documented at
<https://code.claude.com/docs/en/statusline>, carry exact subscriber numbers,
and need no credential at all. So that is the default. If you understand the
terms question and still want the per-model windows, set
`CLAUDE_QUOTA_ALLOW_OAUTH_ENDPOINT=1` for the MCP server or hook, or pass
`--allow-oauth` to the CLI. The request then identifies itself honestly as
`claude-quota-mcp`; nothing here impersonates Claude Code.

Limits of the statusline source, stated plainly: account-wide windows only, no
per-model weekly window; data exists only after Claude Code has made its first
API response and rendered a statusline; freshness equals the last render, and
the tool reports the snapshot age and labels it stale after fifteen minutes.

## What it returns

```
| Window | Used | Resets in | Severity |
|---|---|---|---|
| 5-hour session | 17% | 4h 36m | normal |
| Weekly (all models) | 5% | 2d 3h | normal |
```

Why exact percentages matter: local-transcript estimators can only guess
percent-used against your own historical maximum. On the machine this was
built on, that estimate said **0.7%** when the real figure was **13%**. Reset
*times* reconstruct fine locally; percentages don't.

## Install

Status: **not yet published.** There is no PyPI release and no public
repository yet; the `uvx` one-liner will appear here once both exist. Until
then, install from a local clone:

```bash
python -m pip install .
```

Then register the MCP server with your host. Claude Code:

```bash
claude mcp add quota -- claude-quota-mcp
```

Any MCP host's config:

```json
{
  "mcpServers": {
    "quota": { "command": "claude-quota-mcp" }
  }
}
```

Non-MCP agents and scripts can shell out instead:

```bash
python -m claude_quota                # best available source, JSON
python -m claude_quota --local        # force the transcript estimate
python -m claude_quota --allow-oauth  # permit the undocumented endpoint for this call
python -m claude_quota --codex        # Codex snapshot only
```

## Feed the snapshot: the statusline command

The default source needs Claude Code to hand its statusline payload to this
package once per render. Two ways:

**Standalone.** Use the bundled command as your statusline. It writes the
snapshot and prints a compact `(5h) 53%  (7d) 6%`:

```json
"statusLine": { "type": "command", "command": "claude-quota-statusline" }
```

**Alongside your own statusline.** Run it quietly first, then your script, by
teeing stdin to both (any shell that has `tee` and process substitution):

```bash
tee >(claude-quota-statusline --quiet) | python my_statusline.py
```

Or call it from Python inside your statusline:

```python
from claude_quota.statusline import write_snapshot
write_snapshot(stdin_data)  # the parsed JSON Claude Code piped in
```

Only the documented `rate_limits` fields are persisted. Session ids, working
directories, model names and context-window data in the payload are not
written anywhere. Snapshot location: `%LOCALAPPDATA%\claude-quota-mcp\statusline-snapshot.json`
(override with `CLAUDE_QUOTA_SNAPSHOT`).

## The hook: quota awareness the agent can't forget

A tool the agent must remember to call gets forgotten exactly when it matters,
deep in a long task. The included **quota-guard hook** inverts that: quota state
is pushed into context instead of pulled.

- **SessionStart**: always injects a one-line baseline
  (`5h session 23% (resets 4h 11m); weekly 5% ...`).
- **UserPromptSubmit**: silent in the common case; speaks only when a window
  crosses a threshold (default 70%), severity escalates, or there's an
  *opportunity*: the session window resets soon with usage to spare. Unused
  quota expires at reset, so it nudges the agent to front-load heavy work.
  Repeats are throttled per session.

Claude Code `settings.json`:

```json
"hooks": {
  "SessionStart":     [{ "hooks": [{ "type": "command", "command": "claude-quota-hook" }] }],
  "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "claude-quota-hook" }] }]
}
```

Tune with env vars: `QUOTA_HOOK_WARN_PERCENT` (70), `QUOTA_HOOK_OPPORTUNITY_MINUTES`
(60), `QUOTA_HOOK_OPPORTUNITY_MAX_PERCENT` (50), `QUOTA_HOOK_REMIND_MINUTES` (30).
The hook reads the statusline snapshot, so per-prompt frequency costs nothing.

Every emitted line carries the current local time (`Now Fri 2026-07-18 15:04`);
agents have no clock, and "resets in 40m" is meaningless without one. Set
`QUOTA_HOOK_TIME_ALWAYS=1` to also emit a minimal `<now>` tag on every prompt
even when quota has nothing to say (about 10 tokens of temporal grounding;
default off).

## Codex too

The same tool reports OpenAI Codex limits (`provider: "codex"`; the default
`"all"` covers both). Codex CLI echoes the server's own rate-limit state into
its local rollout logs (`~/.codex/sessions/**/rollout-*.jsonl`), so the exact
numbers are read straight from disk: no token, no endpoint, no rate-limit risk.
Freshness equals Codex's last activity and is always reported
(`snapshot_age_seconds`). This is the same local-only pattern the Claude
statusline source follows.

To let Codex check its own quota, register the server in `~/.codex/config.toml`:

```toml
[mcp_servers.quota]
command = "claude-quota-mcp"
```

## Design notes

- **One tool, tiny schema.** MCP servers tax every message with their tool
  schemas; this one adds a single tool with two optional parameters.
- **Documented sources first.** Both default sources (Claude statusline
  snapshot, Codex rollout logs) are local files written by the vendors' own
  tools. No credential leaves the machine unless you opt in.
- **429-safe when opted in.** The OAuth endpoint rate-limits aggressively per
  token. Responses are disk-cached (180 s TTL, shared across processes), and
  any network failure serves stale cache before falling back.
- **Graceful degradation.** No snapshot, no opt-in, offline: falls back to the
  local transcript reconstruction (times exact, percent estimated) and *says
  so* in the output. It never fails silently into wrong numbers.
- **Stdlib core.** `quota.py` and `statusline.py` are plain stdlib; `mcp` and
  `pydantic` are needed only by the MCP server itself.

## Tests

```bash
python test_hook_bridge.py
python test_statusline.py
```

## Why this exists

Built as a side quest of a multi-agent orchestration experiment: agents
coordinating over a shared bus needed to decide *when* to push hard and when to
wind down a lane, which turns out to require knowing your own quota.

MIT licensed.
