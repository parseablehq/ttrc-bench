"""The stack adapter contract: signals, the closed catalogue of gate checks in OTel terms, probe results, and
the protocol every stacks/<id>/probes.py implements."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from otb.timebase import WallWindow

if TYPE_CHECKING:
    from otb.stacks.context import StackContext
    from otb.stacks.definition import StackDef

Signal = Literal["traces", "logs", "metrics"]
SIGNALS: tuple[Signal, ...] = ("traces", "logs", "metrics")
Phase = Literal["discovery", "execution", "sql"]  # sql: decided per statement by the proxy
Family = Literal["federated", "search", "unified-columnar", "unified-lake"]


# ---------- gate checks: a closed catalogue in OTel terms; probes translate them ----------
#
# Every check is evaluated over a half-open wall-time window [start, end) on the record's event time:
# span start time, log time (observed time when the record has none), metric point time.


@dataclass(frozen=True, slots=True)
class CountLogs:
    service: str | None  # None: all services
    error_only: bool  # severity_number >= 17 OR lower(severity_text) in ('error', 'fatal')
    contains: str | None = None  # substring of a string body


@dataclass(frozen=True, slots=True)
class CountSpans:
    service: str | None
    errors_only: bool  # status code ERROR
    kind: Literal["SERVER", "CLIENT"] | None = None


@dataclass(frozen=True, slots=True)
class TraceSpanCount:
    trace_id: str  # lowercase hex; spans of this trace starting in the window


@dataclass(frozen=True, slots=True)
class DistinctTraces:
    service: str | None  # traces with at least one span (of this service) starting in the window


MetricType = Literal["gauge", "sum", "histogram", "exponential_histogram", "summary"]


@dataclass(frozen=True, slots=True)
class SeriesPoints:
    """A metric is identified by name, unit, type and monotonicity: stacks that rename metrics (Prometheus adds
    unit and _total suffixes) translate from all four."""

    metric: str  # OTel name; a histogram counts its points (Prometheus: the `_count` series)
    attrs: tuple[tuple[str, str], ...]  # resource or point attributes that must all match, incl. service.name
    unit: str = ""
    type: MetricType = "gauge"
    monotonic: bool = False


@dataclass(frozen=True, slots=True)
class SeriesLast:
    metric: str  # sum or gauge
    attrs: tuple[tuple[str, str], ...]  # the value of the latest point in the window; ties broken by max value
    unit: str = ""
    type: MetricType = "gauge"
    monotonic: bool = False


@dataclass(frozen=True, slots=True)
class MetricNames:
    """Sorted distinct names, as the stack stores them, of metrics with a point in the window. The expected value
    is each OTel metric translated by stack_metric_name()."""


@dataclass(frozen=True, slots=True)
class MaxAttrLen:
    """Longest value, in characters, of one span or log-record attribute in the window. The gate picks keys with
    the longest reference values, so a stack that truncates (Tempo, Loki, ES ignore_above) fails it."""

    signal: Literal["traces", "logs"]
    key: str


@dataclass(frozen=True, slots=True)
class IngestCounters:
    """Records the stack's ingest path refused or failed, per signal, since the collector started (DESIGN
    amendment 1): collector send-failed and receiver-refused counters, plus the stack's own dead-letter store.
    Not windowed. Expected value is zero for every signal."""


Check = (
    CountLogs
    | CountSpans
    | TraceSpanCount
    | DistinctTraces
    | SeriesPoints
    | SeriesLast
    | MetricNames
    | MaxAttrLen
    | IngestCounters
)


def check_signal(check: Check) -> Signal | None:
    """The signal a check vouches for; None for IngestCounters, whose value is per signal."""
    match check:
        case CountLogs():
            return "logs"
        case CountSpans() | TraceSpanCount() | DistinctTraces():
            return "traces"
        case SeriesPoints() | SeriesLast() | MetricNames():
            return "metrics"
        case MaxAttrLen(signal=s):
            return s
        case IngestCounters():
            return None


def check_id(check: Check) -> str:
    """Stable text identity: type name plus canonical JSON of its fields."""
    fields = {k: getattr(check, k) for k in check.__slots__}  # type: ignore[attr-defined]
    return f"{type(check).__name__}:{json.dumps(fields, sort_keys=True, separators=(',', ':'))}"


def metric_key(name: str) -> str:
    """Metric name normalised for stacks that keep the OTel name: lower case, runs of non-alphanumerics as one
    underscore."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def stack_metric_name(stack: StackDef, name: str, unit: str, type: str, monotonic: bool) -> str:
    """The name a stack reports for an OTel metric in MetricNames: its probes' metric_name() when the stack
    renames metrics (B1 Prometheus), else metric_key(name)."""
    fn = getattr(stack.probes, "metric_name", None)
    return fn(name, unit, type, monotonic) if fn is not None else metric_key(name)


# ---------- probe results ----------


@dataclass(frozen=True, slots=True)
class Exact:
    value: int | float | dict[str, int] | list[str] | None
    note: dict[str, object] | None = None  # how an exact value was reached when the plain query fell short


@dataclass(frozen=True, slots=True)
class Unverifiable:
    reason: str  # the stack's API cannot answer exactly
    approximate: object | None = None


@dataclass(frozen=True, slots=True)
class Unsupported:
    reason: str  # the adapter has no implementation: judged like Unverifiable, counted as an adapter gap


@dataclass(frozen=True, slots=True)
class ProbeError:
    error: str  # transport or query error: judged as fail


Probe = Exact | Unverifiable | Unsupported | ProbeError


def probe_json(p: Probe) -> dict[str, object]:
    match p:
        case Exact(value=v, note=None):
            return {"result": "exact", "value": v}
        case Exact(value=v, note=n):
            return {"result": "exact", "value": v, "note": n}
        case Unverifiable(reason=r, approximate=a):
            return {"result": "unverifiable", "reason": r, "approximate": a}
        case Unsupported(reason=r):
            return {"result": "unsupported", "reason": r}
        case ProbeError(error=e):
            return {"result": "error", "error": e}


class StackProbes(Protocol):
    def probe(self, check: Check, window: WallWindow, ctx: StackContext) -> Probe: ...
    def provisioning(self) -> Mapping[str, Callable[[StackContext], bool]]: ...  # step -> changed?
    def is_empty(self, ctx: StackContext) -> bool: ...
