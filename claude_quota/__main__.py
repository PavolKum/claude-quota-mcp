"""CLI mode: `python -m claude_quota` prints quota status as JSON.

For agents that are not MCP hosts (shell out from scripts, statuslines).
Flags: --codex (Codex snapshot only), --all (both providers),
--local (force the Claude transcript-only estimate),
--allow-oauth (permit the undocumented OAuth usage endpoint for this call;
see quota.py for why it is off by default). Default: Claude only.
"""

import json
import sys

from claude_quota.codex import codex_quota_status
from claude_quota.quota import quota_status

if __name__ == "__main__":
    prefer_exact = "--local" not in sys.argv
    allow_oauth = True if "--allow-oauth" in sys.argv else None
    if "--codex" in sys.argv:
        print(json.dumps(codex_quota_status(), indent=2))
    elif "--all" in sys.argv:
        print(json.dumps({"claude": quota_status(prefer_exact=prefer_exact, allow_oauth=allow_oauth),
                          "codex": codex_quota_status()}, indent=2))
    else:
        print(json.dumps(quota_status(prefer_exact=prefer_exact, allow_oauth=allow_oauth), indent=2))
