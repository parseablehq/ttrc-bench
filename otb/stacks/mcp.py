"""How a stack's MCP server is launched, and the one place ${VAR} placeholders are resolved.

A launch keeps its placeholders in every file a run writes (inputs/stacks.json, cells/C/cell.json). Only the process
that starts the server resolves them: from OTB_* launch variables and the credentials the launch names, nothing else.
"""

from __future__ import annotations

import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

NETWORK = "otb"
LAUNCH_VARS = ("OTB_REPO", "OTB_HOME", "OTB_NODE", "OTB_NETWORK", "OTB_PYTHON")
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass(frozen=True, slots=True)
class StdioLaunch:
    kind: Literal["docker", "venv", "node", "binary"]
    argv: tuple[str, ...]
    env: Mapping[str, str]  # only these (plus `inherit`) reach the server
    inherit: tuple[str, ...]  # host env vars the launcher itself needs (docker: HOME, PATH)
    pin: str  # image@sha256:... or <lockfile>@sha256:<sha of lockfile>
    lock: str | None  # venv/node/binary: repo-relative lockfile that bootstrap installs from
    tool_allowlist: tuple[str, ...] | None = None  # None: every tool the server lists
    tool_notes: Mapping[str, str] | None = None  # appended to that tool's description in tools/list


@dataclass(frozen=True, slots=True)
class HttpLaunch:
    kind: Literal["http"]
    url: str
    headers: Mapping[str, str]
    pin: str  # the image that serves it
    tool_allowlist: tuple[str, ...] | None = None  # the proxy filters tools/list and refuses other calls
    tool_notes: Mapping[str, str] | None = None  # appended to that tool's description in tools/list


McpLaunch = StdioLaunch | HttpLaunch


def placeholders(launch: McpLaunch) -> set[str]:
    texts = (
        [launch.url, *launch.headers.values()]
        if isinstance(launch, HttpLaunch)
        else [*launch.argv, *launch.env.values()]
    )
    return {m for t in texts for m in PLACEHOLDER.findall(t)}


def credential_names(launch: McpLaunch) -> tuple[str, ...]:
    """The credentials this server needs: its placeholders that are not launch variables. Nothing else a stack
    declares (admin passwords, encryption keys) ever reaches the agent side."""
    return tuple(sorted(placeholders(launch) - set(LAUNCH_VARS)))


def launch_vars(repo: Path) -> dict[str, str]:
    return {
        "OTB_REPO": str(repo),
        "OTB_HOME": str(repo / ".otb"),
        "OTB_NODE": shutil.which("node") or "node",
        "OTB_NETWORK": NETWORK,
        "OTB_PYTHON": sys.executable,
    }


def substitute(text: str, values: Mapping[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        if m.group(1) not in values:
            raise KeyError(f"${{{m.group(1)}}} is not a credential or launch variable")
        return values[m.group(1)]

    return PLACEHOLDER.sub(repl, text)


def resolve_stdio(
    launch: StdioLaunch, values: Mapping[str, str], host_env: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    """argv and the complete environment for a stdio MCP server: declared env plus `inherit`, nothing else."""
    env = {k: substitute(v, values) for k, v in launch.env.items()}
    env |= {k: host_env[k] for k in launch.inherit if k in host_env}
    return [substitute(a, values) for a in launch.argv], env


def resolve_http(launch: HttpLaunch, values: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    return substitute(launch.url, values), {k: substitute(v, values) for k, v in launch.headers.items()}
