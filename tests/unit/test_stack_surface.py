"""The agent surface a run freezes, in isolation: stack.yaml -> AgentSurface -> inputs/stacks.json and cell.json ->
the launch the proxy starts.

Ways this could fail, listed before the code:
1. A frozen surface does not parse back (an optional key frozen as null, such as a docker launch's `lock`).
2. It parses back different: argv, env, inherit, lock or allowlist lost on the way (the stand-in parser dropped
   `inherit` and `lock`).
3. A credential the server does not use (an admin password) is counted as one it needs.
4. A launch names a placeholder that is neither a declared credential nor an OTB_* variable, and nothing fails
   until a cell starts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from otb.stacks import registry
from otb.stacks.definition import StackDefError, load_surface, parse_launch, parse_surface, surface_json


def test_every_stack_surface_round_trips_through_json() -> None:
    for stack in registry().values():
        frozen = json.loads(json.dumps(surface_json(stack.surface)))
        assert parse_surface("inputs/stacks.json", frozen) == stack.surface
        assert parse_launch("cell.json upstream", frozen["mcp"]) == stack.surface.mcp


def test_only_launch_credentials_reach_the_agent_side() -> None:
    assert {s.id: s.surface.credentials for s in registry().values()} == {
        "b1-lgtm": ("B1_GRAFANA_MCP_TOKEN",),
        "b3-clickhouse": ("B3_CH_AGENT_PASSWORD", "B3_CH_AGENT_USER"),
        "b4-parseable": ("B4_PARSEABLE_MCP_KEY",),
    }


def test_undeclared_placeholder_fails_at_load(tmp_path: Path) -> None:
    d = tmp_path / "bad"
    d.mkdir()
    (d / "stack.yaml").write_text(
        "id: bad\nlabel: Bad\ncredentials: [{name: TOKEN, kind: password}]\n"
        "mcp: {kind: http, url: 'http://x/${OTHER}', headers: {}, pin: x}\n"
    )
    with pytest.raises(StackDefError, match=r"\['OTHER'\] are neither declared credentials"):
        load_surface(d)
