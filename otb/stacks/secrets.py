"""Credential values in .otb/secrets/<stack>.env (KEY=VALUE, mode 0600); all.env merges them for compose."""

from __future__ import annotations

import os
import secrets as secrets_lib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from otb.stacks.definition import REPO

if TYPE_CHECKING:
    from otb.stacks.definition import StackDef


def secrets_dir(repo: Path = REPO) -> Path:
    return repo / ".otb" / "secrets"


def credential_values(stack_id: str, names: Iterable[str], repo: Path = REPO) -> dict[str, str]:
    """Values of `names`: the environment first, then .otb/secrets/<stack>.env. A missing name raises KeyError."""
    stored = read_env(secrets_dir(repo) / f"{stack_id}.env")
    missing = sorted(n for n in names if n not in os.environ and n not in stored)
    if missing:
        raise KeyError(f"{stack_id}: credentials not set: {missing}")
    return {n: os.environ.get(n, stored.get(n, "")) for n in names}


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


def write_env(path: Path, values: Mapping[str, str]) -> None:
    _write_private(path, "".join(f"{k}={v}\n" for k, v in sorted(values.items())))


def ensure_secrets(stacks: list[StackDef], repo: Path = REPO) -> dict[str, dict[str, str]]:
    """Generate missing passwords; keep existing values. all.env feeds compose interpolation and is rebuilt from
    every stack's file, including stacks not selected now."""
    out = {}
    for s in stacks:
        path = secrets_dir(repo) / f"{s.id}.env"
        values = read_env(path)
        for c in s.credentials:
            if c.kind == "password" and c.name not in values:
                values[c.name] = secrets_lib.token_hex(24)
            elif c.kind == "static":
                values[c.name] = cast(str, c.value)
        write_env(path, values)
        out[s.id] = values
    merged: dict[str, str] = {}
    for f in sorted(secrets_dir(repo).glob("b*.env")):
        merged |= read_env(f)
    write_env(secrets_dir(repo) / "all.env", merged)
    return out
