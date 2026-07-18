"""CLI mode: `python -m claude_quota` prints quota status as JSON.

For agents that are not MCP hosts (shell out from scripts, statuslines).
Flags: --codex (Codex snapshot only), --all (both providers),
--local (force the Claude transcript-only estimate). Default: Claude only.
"""

import json
import sys

from claude_quota.codex import codex_quota_status
from claude_quota.quota import quota_status

if __name__ == "__main__":
    prefer_exact = "--local" not in sys.argv
    if "--codex" in sys.argv:
        print(json.dumps(codex_quota_status(), indent=2))
    elif "--all" in sys.argv:
        print(json.dumps({"claude": quota_status(prefer_exact=prefer_exact),
                          "codex": codex_quota_status()}, indent=2))
    else:
        print(json.dumps(quota_status(prefer_exact=prefer_exact), indent=2))
