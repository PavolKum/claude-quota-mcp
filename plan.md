# Plan: claude-quota-mcp — standalone repo + reputation play

## Goal
Publish a small, polished, standalone MCP server that lets an AI agent check
its own Claude usage limits (exact 5-hour / weekly / per-model percentages and
reset times) so it can plan long-running work around resets. First release of a
two-step reputation arc: this tool now, the multi-agent bus case study later.

## Inputs
- Working OAuth exact source + transcript fallback (extracted from
  Tools/opencode-usage/claude_session_limit.py, validated live 2026-07-17)
- Verified niche gap: human-facing monitors are saturated (ccusage, statuslines,
  tray apps); NO agent-facing quota MCP exists as of 2026-07-17
- Known constraint: endpoint 429s aggressively (~180s min polling per token)

## Outputs
- This folder as a standalone GitHub repo (github.com/PavolKum/claude-quota-mcp)
- PyPI package `claude-quota-mcp` (uvx-runnable)
- Launch posts: r/ClaudeCode -> HN Show HN -> LinkedIn (reframed)
- Registry listings: awesome-mcp-servers, Smithery, PulseMCP

## Steps
Scaffold (done 2026-07-17):
- [x] Package layout: claude_quota/ (quota.py core, server.py MCP, __main__.py CLI)
- [x] 429-safe disk cache (180s TTL, stale-on-failure), shared across processes
- [x] Token discovery: env var, credentials file, macOS keychain
- [x] pyproject.toml (hatchling, `claude-quota-mcp` entry point), MIT LICENSE,
      README with the 0.7%-vs-13% hook
- [x] Smoke-tested: CLI exact + --local, MCP tool render, cache hit path
- [x] quota-guard hook (claude_quota/hook.py): SessionStart baseline +
      silent-unless-triggered UserPromptSubmit (warn threshold / severity /
      opportunity signal), per-session throttle; all 5 paths smoke-tested
- [x] Codex provider (claude_quota/codex.py, added 2026-07-17): parses the
      newest server-issued rate_limits snapshot from local rollout JSONL
      (exact numbers, no network; snapshot age always reported). quota_status
      tool gained provider=claude|codex|all (default all); CLI --codex/--all.
      Registered in ~/.codex/config.toml [mcp_servers.quota] so Codex checks
      its own quota. Verified live: plan prolite, weekly 3%, snapshot age 23h.

Weekend polish (Pavol + agent):
- [ ] Test cold paths: no credentials file, expired token, offline, 429 (mock),
      fresh machine with no transcripts
- [ ] Test macOS keychain path if a Mac is available (else mark "untested" in README)
- [ ] Add ~5 pytest cases for parsing + cache logic (payload fixtures from live capture)
- [ ] Run several real sessions with the MCP + quota-guard hook wired in;
      confirm numbers track /usage and the hook stays silent when it should
- [ ] Dogfood the opportunity signal once for real (low usage + near reset)
- [ ] Test quota tool FROM Codex (restart Codex CLI, ask it to check its own
      quota; also confirm which windows appear on plans with a 5h primary)
- [ ] Extend quota-guard hook triggers to the codex provider (Codex hooks.json)
- [ ] Decide final name (claude-quota-mcp vs alternatives) BEFORE first push
- [ ] git init, first commit, create GitHub repo (public)
- [ ] Publish to PyPI (needs account/token) so `uvx claude-quota-mcp` works
- [ ] Verify install path end-to-end on a clean environment: `claude mcp add quota -- uvx claude-quota-mcp`

Launch (next week, in order):
- [ ] r/ClaudeCode post (353k members) - story framing: "I gave Claude the
      ability to check its own rate limits so it plans long tasks around resets";
      lead with the 0.7% vs 13% estimate-vs-exact gap
- [ ] HN Show HN same week
- [ ] r/ClaudeAI cross-post if r/ClaudeCode lands
- [ ] LinkedIn 2-3 days later, reframed for strategy audience: "agents that
      manage their own resource budgets" + link dev traction
- [ ] PR into awesome-mcp-servers / awesome-claude-code; submit to Smithery, PulseMCP

## Success Criteria
- `uvx claude-quota-mcp` works from a clean machine with logged-in Claude Code
- Tool output matches /usage percentages within rounding
- Never 429s a user's token (cache verified under repeated polling)
- Falls back loudly-labeled (never silent wrong numbers) when OAuth unavailable
- Launch: any traction is a win (niche was empty); repo cross-links the coming
  bus write-up so the audience compounds

## Sequencing decision (recorded)
Do NOT bundle with the multi-agent bus. Bus stays under feature freeze
(F1-F5 hardening pending); its arc is case-study first, OSS second. This tool
ships first as the teaser; README's "why this exists" seeds the bus story.
