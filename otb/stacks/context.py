"""The runtime context handed to probes and provisioning: resolved URLs, credentials, and a recording HTTP
client."""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from otb.stacks.contract import SIGNALS, Check, Probe, ProbeError, Signal
from otb.stacks.definition import REPO, StackDef
from otb.stacks.secrets import read_env, secrets_dir, write_env
from otb.timebase import WallWindow


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes

    def json(self) -> object:
        return json.loads(self.body) if self.body.strip() else None


class HttpError(RuntimeError):
    def __init__(self, method: str, url: str, resp: Response) -> None:
        super().__init__(f"{method} {url} -> HTTP {resp.status}: {resp.body[:400].decode(errors='replace')}")
        self.response = resp


def basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@dataclass
class StackContext:
    """Resolved URLs, credentials, and an HTTP client. Every exchange is kept in `exchanges` so the gate can
    record raw responses next to the verdicts derived from them."""

    stack: StackDef
    repo: Path = REPO
    exchanges: list[dict[str, object]] = field(default_factory=list)
    by_id: dict[str, list[dict]] = field(default_factory=dict)  # trace id -> spans the gate fetched by id
    arrival_horizon_s: int | None = None  # the session's (otb.ingest.arrival_horizon_s), for arrival-time stores

    def _secrets(self) -> dict[str, str]:
        return read_env(secrets_dir(self.repo) / f"{self.stack.id}.env")

    def secret(self, name: str) -> str:
        values = self._secrets()
        if name not in values:
            raise KeyError(f"{self.stack.id}: secret {name} not generated or provisioned yet")
        return values[name]

    def has_secret(self, name: str) -> bool:
        return name in self._secrets()

    def set_secret(self, name: str, value: str) -> None:
        values = self._secrets() | {name: value}
        write_env(secrets_dir(self.repo) / f"{self.stack.id}.env", values)

    def url(self, name: str) -> str:
        return self.stack.urls[name]

    def http(
        self,
        method: str,
        url: str,
        *,
        body: object | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 60,
    ) -> Response:
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        hdrs = dict(headers or {})
        if data is not None and not isinstance(body, bytes):
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                resp = Response(r.status, r.read())
        except urllib.error.HTTPError as e:
            resp = Response(e.code, e.read())
        except OSError as e:
            self.exchanges.append(
                {
                    "method": method,
                    "url": url,
                    "request": data.decode(errors="replace") if data else None,
                    "error": {"type": type(e).__name__, "message": str(e)},
                }
            )
            raise
        self.exchanges.append(
            {
                "method": method,
                "url": url,
                "request": data.decode(errors="replace") if data else None,
                "status": resp.status,
                "response": resp.body.decode(errors="replace"),
            }
        )
        return resp

    def http_json(
        self,
        method: str,
        url: str,
        *,
        body: object | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 60,
    ) -> object:
        resp = self.http(method, url, body=body, headers=headers, timeout_s=timeout_s)
        if not 200 <= resp.status < 300:
            raise HttpError(method, url, resp)
        return resp.json()


_COUNTER = re.compile(
    r"^otelcol_(exporter_send_failed|receiver_refused|exporter_enqueue_failed|processor_dropped)_"
    r"(spans|log_records|metric_points)(?:_total)?(?:\{[^}]*\})? ([0-9.e+]+)$",
    re.M,
)
_COUNTER_SIGNAL: dict[str, Signal] = {"spans": "traces", "log_records": "logs", "metric_points": "metrics"}


def collector_counters(ctx: StackContext) -> dict[Signal, int]:
    """Refused and failed record counts from the stack's ingest collector, by signal."""
    resp = ctx.http("GET", ctx.stack.ingest.metrics_url)
    if resp.status != 200:
        raise HttpError("GET", ctx.stack.ingest.metrics_url, resp)
    out: dict[Signal, int] = dict.fromkeys(SIGNALS, 0)
    for m in _COUNTER.finditer(resp.body.decode()):
        out[_COUNTER_SIGNAL[m.group(2)]] += int(float(m.group(3)))
    return out


def run_probe(stack: StackDef, check: Check, window: WallWindow, ctx: StackContext) -> Probe:
    """The one place a probe exception becomes a ProbeError, so adapters can raise freely."""
    try:
        return stack.probes.probe(check, window, ctx)
    except Exception as e:
        return ProbeError(f"{type(e).__name__}: {e}")
