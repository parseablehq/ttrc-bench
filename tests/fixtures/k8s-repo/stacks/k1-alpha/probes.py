"""Fixture stack: no probes. The lifecycle tests never gate it."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from otb.stacks import Check, Probe, StackContext, Unsupported
from otb.timebase import WallWindow


def probe(check: Check, window: WallWindow, ctx: StackContext) -> Probe:
    return Unsupported("fixture stack")


def provisioning() -> Mapping[str, Callable[[StackContext], bool]]:
    return {}


def is_empty(ctx: StackContext) -> bool:
    return True
