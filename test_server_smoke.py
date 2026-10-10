"""Smoke test for the MCP server surface.

Runs in CI before publish. It exists because two defects reached the public
repo without any test touching the server module: an `mcp` 2.x import break
(FastMCP renamed) and a nested `params` argument schema that made a plain
`quota_status(provider="all")` call fail validation. Both are checked here.

Usage: python test_server_smoke.py
"""
import asyncio
import json
import os
import sys
import tempfile


def main() -> int:
    # Point every local source at an empty directory: the test must pass on a
    # clean machine with no statusline snapshot, no transcripts, no Codex logs.
    scratch = tempfile.mkdtemp(prefix="quota-smoke-")
    os.environ["LOCALAPPDATA"] = scratch
    os.environ["HOME"] = scratch
    os.environ["USERPROFILE"] = scratch
    os.environ.pop("CLAUDE_QUOTA_ALLOW_OAUTH_ENDPOINT", None)

    from claude_quota.server import mcp  # import break shows up here

    tools = asyncio.run(mcp.list_tools())
    names = [t.name for t in tools]
    assert names == ["quota_status"], names
    schema = tools[0].inputSchema
    props = schema.get("properties", {})
    assert "params" not in props, "arguments must be top-level, not nested under params"
    assert "provider" in props and "response_format" in props, props
    assert not schema.get("required"), "every argument must have a default"

    result = asyncio.run(mcp.call_tool("quota_status", {"provider": "all"}))
    text = _text_of(result)
    assert text, "empty tool result"

    result = asyncio.run(
        mcp.call_tool("quota_status", {"provider": "claude", "response_format": "json"})
    )
    payload = json.loads(_text_of(result))
    assert isinstance(payload, dict), type(payload)

    print("server smoke: OK (tools=%s, clean-machine call returned %d chars)" % (names, len(text)))
    return 0


def _text_of(result) -> str:
    # FastMCP 1.x returns a list of content blocks, or a (blocks, structured) tuple.
    if isinstance(result, tuple):
        result = result[0]
    return "\n".join(getattr(block, "text", "") for block in result)


if __name__ == "__main__":
    sys.exit(main())
