"""The agent boundary. An adapter turns a cell into a process launch and its transcript into what the grader needs;
no transcript wire format leaks past `parse`.

A cell is handed to the agent side as a CellJob: the prompts, the budget and, for a scripted agent, the plan the
runner computed from the truth. Nothing else the runner holds (the run dir, the suite, secrets) crosses that
boundary, so the same CellJob serves the in-process launch and the split-role worker.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from otb.tasks.suite import Instance
from otb.timebase import Shift

AdapterId = Literal["claude-code", "oracle", "null"]
SERVER = "telemetry"
# The only variables an agent process inherits; everything else in the operator's environment stays out.
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "TERM")


@dataclass(frozen=True, slots=True)
class AgentSpec:
    adapter: AdapterId
    model: str | None = None
    effort: str | None = None
    cli_version: str | None = None
    variant: str | None = None


@dataclass(frozen=True, slots=True)
class CellJob:
    cell_id: str
    spec: AgentSpec
    budget: Mapping[str, float]
    ctx_cap_bytes: int
    user_prompt: str
    system_prompt: str
    plan: object | None  # a scripted agent's calls and answer, computed by the runner; None for a model agent

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict) -> CellJob:
        return cls(**{**raw, "spec": AgentSpec(**raw["spec"])})


@dataclass(frozen=True, slots=True)
class Launch:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


Stop = Literal["end_turn", "budget_usd", "max_turns", "error", "unknown"]


@dataclass(frozen=True, slots=True)
class AgentTranscript:
    init_ok: bool
    model: str | None
    client_version: str | None
    session_id: str | None
    rounds: int | None
    stop: Stop
    usage: Usage
    first_turn_input_tokens: int | None
    api_s: float | None
    usd_reported: float | None
    tool_result_ctx_bytes: tuple[int, ...]
    extra: Mapping[str, object] = field(default_factory=dict)


class AgentAdapter(Protocol):
    id: AdapterId

    def preflight(self, spec: AgentSpec) -> str | None:
        """Check the pinned client version before any cell runs; return what was observed, or raise."""
        ...

    def prepare(self, run_dir: Path, inst: Instance, shift: Shift, spec: AgentSpec) -> object | None:
        """The runner's side of a cell: whatever the agent side needs that only the run dir holds (a scripted
        agent's answer from the truth). The result goes into the CellJob and is the only thing that crosses."""
        ...

    def launch(self, job: CellJob, cwd: Path, mcp_config: Path, env: Mapping[str, str]) -> Launch:
        """The agent process for one cell: `env` is the complete base environment the caller allows."""
        ...

    def parse(self, transcript: Path) -> AgentTranscript: ...

    def isolation_violations(self, transcript: Path, spec: AgentSpec, cwd: Path) -> list[str]: ...

    def scrub_paths(self, transcript: Path, cwd: Path, home: Path) -> None:
        """After the isolation check: remove the cell's cwd and HOME, local paths, from what the client wrote."""
        ...


def sanitised_env() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
