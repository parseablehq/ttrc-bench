"""Check 4 of U4: every stack's MCP server launches with the stack's credentials, answers tools/list, and serves one
read-only call. Writes docs/v1/units/U4-mcp.json: tool names, manifest bytes (JSON of the tool list as the agent
would receive it) and the probe call's outcome. B2 is HTTP; its allowlist is applied here, client-side, as the proxy
will apply it.

  uv run python tests/live/mcp_list.py [--stacks b1,b2,b3,b4]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from otb.stacks import REPO, StackDef, registry, select
from otb.stacks.mcp import HttpLaunch, launch_vars, resolve_http, resolve_stdio
from otb.stacks.secrets import credential_values

PROBE_CALL = {
    "b1-lgtm": ("list_datasources", {}),
    "b3-clickhouse": ("list_databases", {}),
    "b4-parseable": ("list_datasets", {}),
}


def _manifest(tools: list) -> tuple[list[str], int]:
    dumped = [t.model_dump(exclude_none=True, by_alias=True) for t in tools]
    return sorted(t.name for t in tools), len(json.dumps(dumped, separators=(",", ":")).encode())


async def _session(stack: StackDef, read, write) -> dict[str, object]:  # type: ignore[no-untyped-def]
    async with ClientSession(read, write) as s:
        init = await s.initialize()
        tools = (await s.list_tools()).tools
        names, size = _manifest(tools)
        out: dict[str, object] = {
            "server": init.server_info.model_dump(exclude_none=True),
            "protocol": init.protocol_version,
            "tools_listed": len(tools),
            "manifest_bytes": size,
            "tools": names,
        }
        if stack.surface.mcp.tool_allowlist is not None:
            allowed = [t for t in tools if t.name in stack.surface.mcp.tool_allowlist]
            out["allowlist"] = list(stack.surface.mcp.tool_allowlist)
            out["allowlist_missing"] = sorted(set(stack.surface.mcp.tool_allowlist) - {t.name for t in tools})
            out["tools"], out["manifest_bytes"] = _manifest(allowed)
            out["unfiltered_manifest_bytes"] = size
        name, args = PROBE_CALL[stack.id]
        r = await s.call_tool(name, args)
        text = " ".join(c.text for c in r.content if getattr(c, "text", None))
        out["probe_call"] = {
            "tool": name,
            "args": args,
            "is_error": r.is_error,
            "result_bytes": len(text.encode()),
            "result_head": text[:300],
        }
        return out


async def check(stack: StackDef) -> dict[str, object]:
    t0 = time.monotonic()
    launch = stack.surface.mcp
    values = launch_vars(REPO) | credential_values(stack.id, stack.surface.credentials)
    if isinstance(launch, HttpLaunch):
        url, headers = resolve_http(launch, values)
        async with create_mcp_http_client(headers=headers) as http, streamable_http_client(url, http_client=http) as st:
            out = await _session(stack, st[0], st[1])
        out["env_keys"] = []
    else:
        argv, env = resolve_stdio(launch, values, os.environ)
        params = StdioServerParameters(command=argv[0], args=argv[1:], env=env, cwd=str(REPO / ".otb"))
        async with stdio_client(params) as (read, write):
            out = await _session(stack, read, write)
        out["env_keys"] = sorted(env)
    out["transport"] = "streamable-http" if isinstance(launch, HttpLaunch) else "stdio"
    out["pin"] = launch.pin
    out["secs"] = round(time.monotonic() - t0, 2)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--stacks", default=None)
    p.add_argument("--out", default=str(REPO / "docs" / "v1" / "units" / "U4-mcp.json"))
    a = p.parse_args()
    result: dict[str, object] = {
        "unit": "U4 check 4",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    for stack in select(registry(), a.stacks):
        try:
            result[stack.id] = asyncio.run(check(stack))
        except Exception as e:  # recorded, not raised: the artifact names the failing stack
            result[stack.id] = {"error": f"{type(e).__name__}: {e}"}
        r = result[stack.id]
        print(
            stack.id,
            json.dumps({k: r[k] for k in ("tools_listed", "manifest_bytes", "probe_call", "error") if k in r})[:400],
        )
    Path(a.out).write_text(json.dumps(result, indent=1) + "\n")


if __name__ == "__main__":
    main()
