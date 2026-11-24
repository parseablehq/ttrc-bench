"""A tiny MCP server with canned, deterministic tools, standing in for a stack's vendor server in tests.

  python mcp_fixture_server.py                 stdio
  python mcp_fixture_server.py --http PORT     streamable HTTP at http://127.0.0.1:PORT/mcp
                                               (FIXTURE_HTTP_HOST=0.0.0.0 to serve other pods)

Built on the MCP Python SDK, so the proxy is exercised against an independent protocol implementation.
The tools cover every path the proxy distinguishes: a plain result with trailing whitespace, a result over the
context cap, a vendor error, a Kibana Agent Builder error payload, a slow tool, a tool a stack allowlist hides,
and a tool that asks the client to sample the model.
"""

from __future__ import annotations

import json
import os
import sys
import time

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import SamplingMessage, TextContent

ERROR_SPANS = {"payment": 42, "checkout": 7}
FIRST_ERROR = {"payment": "2026-09-21T14:13:20Z", "checkout": "2026-09-21T14:15:03Z"}

server = MCPServer("otb-fixture", version="1.0.0", log_level="WARNING")


@server.tool(structured_output=False)
def list_services() -> str:
    """List the services that sent telemetry."""
    return "checkout\nfrontend\npayment\n\n"


@server.tool(structured_output=False)
def count_error_spans(service: str, start: str, end: str) -> str:
    """Count server spans with error status for a service between two UTC timestamps."""
    return json.dumps({"service": service, "start": start, "end": end, "error_spans": ERROR_SPANS.get(service, 0)})


@server.tool(structured_output=False)
def first_error_time(service: str) -> str:
    """The start time of the first error span of a service."""
    return json.dumps({"service": service, "first_error_at": FIRST_ERROR.get(service)})


@server.tool(structured_output=False)
def big_dump() -> str:
    """Return a very large result."""
    row = "trace 0123456789abcdef span ok é\n"
    return "".join(f"{i:06d} {row}" for i in range(2000))


@server.tool(structured_output=False)
def broken_tool() -> str:
    """Always fails."""
    raise ValueError("fixture failure: backend unavailable")


@server.tool(structured_output=False)
def execute_esql(query: str) -> str:
    """Run an ES|QL query (Kibana Agent Builder style results)."""
    return json.dumps(
        {
            "results": [
                {
                    "tool_result_id": "fx0001",
                    "type": "error",
                    "data": {"message": "verification_exception: Unknown index [nope]"},
                }
            ]
        }
    )


@server.tool(structured_output=False)
def slow_tool(seconds: float) -> str:
    """Sleep, then answer."""
    time.sleep(seconds)
    return "slept"


@server.tool(structured_output=False)
def whoami() -> str:
    """Which environment variables this server process received (names only) and whether its token is set."""
    return json.dumps({"env_keys": sorted(os.environ), "token_set": bool(os.environ.get("FIXTURE_TOKEN"))})


@server.tool(structured_output=False)
def admin_drop_all() -> str:
    """A write tool that a read-only allowlist must hide."""
    return "dropped every table"


@server.tool(structured_output=False)
async def ask_model(ctx: Context) -> str:
    """Ask the client to run the model on the server's behalf (MCP sampling)."""
    message = SamplingMessage(role="user", content=TextContent(type="text", text="Say yes."))
    result = await ctx.session.create_message([message], max_tokens=5)
    return f"the client answered: {getattr(result.content, 'text', result.content)}"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--http":
        server.run("streamable-http", host=os.environ.get("FIXTURE_HTTP_HOST", "127.0.0.1"), port=int(sys.argv[2]))
    else:
        server.run("stdio")
