"""stack.yaml parsed into a StackDef, and the registry of stack directories.

A stack is a directory stacks/<id>/ holding stack.yaml, compose.yaml (its services and its own `ingest-<n>`
collector), collector.yaml (synchronous: the receiver's HTTP status is the exporter's result), probes.py (gate
probes and provisioning in the stack's native query language) and config/. Registration is the directory's
existence; nothing outside the directory names a stack. Every check of stack.yaml happens here, at load.

The runtime (OTB_RUNTIME=compose|k8s) is applied here and nowhere else: under k8s the stack's `k8s.endpoints`
replace the URL-bearing fields (ingest.*, urls.*, readiness.<name>, mcp.env.<VAR>) before anything reads them, and
`StackDef.same_address` maps a recorded URL back to its endpoint key under either runtime so gate recordings
rejudge across runtimes.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal, cast, get_args
from urllib.parse import urlsplit

import yaml

from otb.stacks.contract import SIGNALS, Family, Phase, Signal, StackProbes
from otb.stacks.mcp import HttpLaunch, McpLaunch, StdioLaunch, credential_names

REPO = Path(__file__).resolve().parents[2]

Runtime = Literal["compose", "k8s"]
RUNTIMES: tuple[Runtime, ...] = ("compose", "k8s")
K8S_PROFILES = ("kind", "gke", "gke-pilot")


def runtime_from_env() -> Runtime:
    value = os.environ.get("OTB_RUNTIME", "compose")
    if value not in RUNTIMES:
        raise StackDefError(f"OTB_RUNTIME={value!r}: must be one of {', '.join(RUNTIMES)}")
    return cast(Runtime, value)


@dataclass(frozen=True, slots=True)
class IngestEndpoint:
    service: str  # compose service name of the stack's collector
    url: str  # host-side OTLP/HTTP base URL
    health_url: str  # collector health_check extension
    metrics_url: str  # collector self-telemetry, Prometheus text
    paths: Mapping[Signal, str]
    max_inflight: int


@dataclass(frozen=True, slots=True)
class CollectorDef:
    config: Path
    processors: Mapping[Signal, tuple[str, ...]]  # declared and disclosed; must equal collector.yaml's pipelines


@dataclass(frozen=True, slots=True)
class Credential:
    name: str  # env var name, unique across stacks
    kind: Literal["password", "static", "provisioned"]
    value: str | None = None  # static only (usernames)
    step: str | None = None  # provisioned only: the post_up step that writes it


@dataclass(frozen=True, slots=True)
class Readiness:
    name: str
    url: str
    expect_status: int
    basic_auth: tuple[str, str] | None  # credential names (user, password)
    timeout_s: int


@dataclass(frozen=True, slots=True)
class AgentSurface:
    """The part of stack.yaml a run freezes into inputs/stacks.json: how the agent reaches the stack and how the
    report names it."""

    id: str
    label: str
    mcp: McpLaunch
    phase: Mapping[str, Phase]  # tool -> phase; unlisted tools are execution; diagnostic only
    disclosures: tuple[str, ...]

    @property
    def credentials(self) -> tuple[str, ...]:
        return credential_names(self.mcp)


@dataclass(frozen=True, slots=True)
class ChartRelease:
    name: str
    chart: str  # as deploy/k8s/charts.lock.yaml keys it: `<repo alias>/<chart>` or `oci://...`
    version: str
    values: Path  # repo-relative values file, resolved


@dataclass(frozen=True, slots=True)
class ManifestRelease:
    name: str
    manifests: tuple[str, ...]  # repo-relative paths; `{profile}` stands for the budget profile

    def paths(self, profile: str, repo: Path) -> tuple[Path, ...]:
        return tuple(repo / m.format(profile=profile) for m in self.manifests)


Release = ChartRelease | ManifestRelease


@dataclass(frozen=True, slots=True)
class K8sDef:
    namespace: str
    releases: tuple[Release, ...]  # applied in this order, removed in reverse
    endpoints: Mapping[str, str]  # dotted field path -> in-cluster value


@dataclass(frozen=True, slots=True)
class StackDef:
    id: str  # directory name, e.g. "b4-parseable"
    surface: AgentSurface
    family: Family
    dir: Path
    runtime: Runtime
    compose_file: Path
    k8s: K8sDef | None
    ingest: IngestEndpoint
    collector: CollectorDef
    urls: Mapping[str, str]  # named host-side URLs for probes and provisioning
    arrival_time_sensitive: bool
    max_late_s: Mapping[Signal, float | None]  # None: no bound for that signal
    settle_s: float  # the gate probes no earlier than this after the stack's last ingested frame
    credentials: tuple[Credential, ...]
    post_up: tuple[str, ...]  # provisioning steps, in order, implemented in probes.py
    readiness: tuple[Readiness, ...]
    probes: StackProbes
    origins: Mapping[str, frozenset[str]]  # scheme://host:port -> the endpoint keys it serves, both runtimes

    def same_address(self, recorded: str, asked: str) -> bool:
        """Two URLs name the same logical address when their origins serve a common endpoint key (the compose
        127.0.0.1:port and the cluster Service of one backend) and the rest of the URL is identical."""
        if recorded == asked:
            return True
        (ro, rrest), (ao, arest) = _split_origin(recorded), _split_origin(asked)
        return rrest == arest and bool(self.origins.get(ro, frozenset()) & self.origins.get(ao, frozenset()))


def _split_origin(url: str) -> tuple[str, str]:
    u = urlsplit(url)
    origin = f"{u.scheme}://{u.netloc}"
    return origin, url[len(origin) :]


class StackDefError(ValueError):
    pass


class _Doc:
    """Typed reads from one parsed YAML mapping; every error names the file and the dotted key."""

    def __init__(self, path: Path, data: object, prefix: str = "") -> None:
        if not isinstance(data, dict):
            raise StackDefError(f"{path}: {prefix or 'document'} must be a mapping")
        self.path, self.data, self.prefix = path, cast(dict[str, object], data), prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}.{key}" if self.prefix else key

    def fail(self, key: str, msg: str) -> StackDefError:
        return StackDefError(f"{self.path}: {self._key(key)}: {msg}")

    def get(self, key: str, typ: type | tuple[type, ...], default: object = ...) -> object:
        if key not in self.data or (self.data[key] is None and default is not ...):
            if default is ...:
                raise self.fail(key, "required")
            return default
        value = self.data[key]
        if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
            raise self.fail(key, f"expected {typ}, got {type(value).__name__}")
        return value

    def str(self, key: str, default: object = ...) -> str:
        return cast(str, self.get(key, str, default))

    def sub(self, key: str, default: object = ...) -> _Doc:
        value = self.get(key, dict, default)
        return _Doc(self.path, value if value is not None else {}, self._key(key))

    def items(self, key: str, default: object = ...) -> list[_Doc]:
        value = cast(list[object], self.get(key, list, default))
        return [_Doc(self.path, v, f"{self._key(key)}[{i}]") for i, v in enumerate(value)]

    def strs(self, key: str, default: object = ...) -> tuple[str, ...]:
        value = cast(list[object], self.get(key, list, default))
        if not all(isinstance(v, str) for v in value):
            raise self.fail(key, "expected a list of strings")
        return tuple(cast(list[str], value))

    def str_map(self, key: str, default: object = ...) -> dict[str, str]:
        value = cast(dict[object, object], self.get(key, dict, default))
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise self.fail(key, "expected a mapping of strings to strings")
        return cast(dict[str, str], value)

    def literal(self, key: str, allowed: tuple[str, ...], default: object = ...) -> str:
        value = self.str(key, default)
        if value not in allowed:
            raise self.fail(key, f"must be one of {', '.join(allowed)}, got {value!r}")
        return value


def _signal_map(doc: _Doc, key: str) -> dict[Signal, str]:
    m = doc.str_map(key)
    if set(m) != set(SIGNALS):
        raise doc.fail(key, f"must name exactly {', '.join(SIGNALS)}")
    return cast(dict[Signal, str], m)


def _parse_collector(doc: _Doc, stack_dir: Path) -> CollectorDef:
    path = stack_dir / doc.str("config")
    declared_doc = doc.sub("processors", {})
    declared = {s: declared_doc.strs(s, []) for s in SIGNALS}
    unknown = set(declared_doc.data) - set(SIGNALS)
    if unknown:
        raise declared_doc.fail(sorted(unknown)[0], "not a signal")
    _check_synchronous(path, declared)
    return CollectorDef(path, declared)


def _check_synchronous(path: Path, declared: Mapping[Signal, tuple[str, ...]]) -> None:
    """The ingest ack means "the backend accepted it" only if nothing between receiver and backend buffers:
    no batch processor, sending_queue disabled on every exporter, retry bounded. Enforced here, at load."""
    if not path.exists():
        raise StackDefError(f"{path}: missing (collector.config)")
    cfg = _Doc(path, yaml.safe_load(path.read_text()))
    for name, exp in cfg.sub("exporters").data.items():
        e = _Doc(path, exp or {}, f"exporters.{name}")
        if e.sub("sending_queue", {}).get("enabled", bool, None) is not False:
            raise e.fail("sending_queue.enabled", "must be false (synchronous ingest)")
        if "batcher" in e.data:
            raise e.fail("batcher", "not allowed (synchronous ingest)")
        if "retry" in e.data:  # exporters with their own retry block (elasticsearch)
            if not isinstance(e.sub("retry").get("max_retries", int, None), int):
                raise e.fail("retry.max_retries", "must be set: retries are bounded")
            continue
        retry = e.sub("retry_on_failure", {})
        if retry.get("enabled", bool, True) and not retry.get("max_elapsed_time", str, None):
            raise e.fail("retry_on_failure.max_elapsed_time", "must be set: retries are bounded")
    for name in cfg.sub("processors", {}).data:
        if name.split("/")[0] in ("batch", "memory_limiter"):
            raise cfg.fail(f"processors.{name}", "not allowed (synchronous ingest)")
    pipelines = cfg.sub("service").sub("pipelines")
    for signal in SIGNALS:
        p = pipelines.sub(signal)
        actual = p.strs("processors", [])
        if actual != declared[signal]:
            raise p.fail("processors", f"{list(actual)} differs from stack.yaml collector.processors.{signal}")


def _parse_mcp(doc: _Doc) -> McpLaunch:
    kind = doc.literal("kind", ("docker", "venv", "node", "binary", "http"))
    allow = doc.strs("tool_allowlist") if doc.data.get("tool_allowlist") is not None else None
    notes = doc.str_map("tool_notes") if doc.data.get("tool_notes") is not None else None
    if kind == "http":
        return HttpLaunch("http", doc.str("url"), doc.str_map("headers", {}), doc.str("pin"), allow, notes)
    return StdioLaunch(
        cast(Literal["docker", "venv", "node", "binary"], kind),
        doc.strs("argv"),
        doc.str_map("env", {}),
        doc.strs("inherit", []),
        doc.str("pin"),
        doc.str("lock", None),
        allow,
        notes,
    )


def _parse_surface(doc: _Doc) -> AgentSurface:
    phase = doc.str_map("phase", {})
    for tool, ph in phase.items():
        if ph not in get_args(Phase):
            raise doc.fail(f"phase.{tool}", f"must be one of {', '.join(get_args(Phase))}, got {ph!r}")
    return AgentSurface(
        doc.str("id"),
        doc.str("label"),
        _parse_mcp(doc.sub("mcp")),
        cast(dict[str, Phase], phase),
        doc.strs("disclosures", []),
    )


def parse_surface(where: str, raw: object) -> AgentSurface:
    """An AgentSurface frozen as JSON (inputs/stacks.json), read with the same parser as stack.yaml."""
    return _parse_surface(_Doc(Path(where), raw))


def parse_launch(where: str, raw: object) -> McpLaunch:
    """A launch frozen as JSON (cells/C/cell.json `upstream`)."""
    return _parse_mcp(_Doc(Path(where), raw))


def surface_json(surface: AgentSurface) -> dict[str, object]:
    return asdict(surface)


def _parse_credentials(doc: _Doc) -> tuple[Credential, ...]:
    creds = []
    for c in doc.items("credentials", []):
        kind = c.literal("kind", ("password", "static", "provisioned"))
        cred = Credential(c.str("name"), cast(Literal["password", "static", "provisioned"], kind))
        if kind == "static":
            cred = Credential(cred.name, "static", value=c.str("value"))
        elif kind == "provisioned":
            cred = Credential(cred.name, "provisioned", step=c.str("step"))
        creds.append(cred)
    return tuple(creds)


def _address_fields(doc: _Doc) -> dict[str, tuple[dict[str, object], str]]:
    """Every field an endpoint override may replace, by dotted path: the mapping that holds it and its key."""
    data = doc.data
    out: dict[str, tuple[dict[str, object], str]] = {}
    ingest = cast(dict[str, object], data.get("ingest") or {})
    for k in ("url", "health_url", "metrics_url"):
        if k in ingest:
            out[f"ingest.{k}"] = (ingest, k)
    urls = cast(dict[str, object], data.get("urls") or {})
    out |= {f"urls.{k}": (urls, k) for k in urls}
    for item in cast(list[object], data.get("readiness") or []):
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out[f"readiness.{item['name']}"] = (cast(dict[str, object], item), "url")
    mcp = cast(dict[str, object], data.get("mcp") or {})
    if "url" in mcp:
        out["mcp.url"] = (mcp, "url")
    env = cast(dict[str, object], mcp.get("env") or {})
    out |= {f"mcp.env.{k}": (env, k) for k in env}
    return out


def _origins(values: Mapping[str, object]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path, value in values.items():
        if isinstance(value, str) and "://" in value:
            out.setdefault(_split_origin(value)[0], set()).add(path)
    return out


def _parse_release(r: _Doc, repo: Path) -> Release:
    if "manifests" in r.data:
        for key in ("chart", "version", "values"):
            if key in r.data:
                raise r.fail(key, "a manifests release has no chart")
        manifests = r.strs("manifests")
        for m in manifests:
            for profile in K8S_PROFILES:
                if not (repo / m.format(profile=profile)).exists():
                    raise r.fail("manifests", f"{m.format(profile=profile)} does not exist")
        return ManifestRelease(r.str("name"), manifests)
    values = repo / r.str("values")
    if not values.exists():
        raise r.fail("values", f"{values} does not exist")
    return ChartRelease(r.str("name"), r.str("chart"), r.str("version"), values)


def _parse_k8s(doc: _Doc, repo: Path) -> K8sDef | None:
    if doc.data.get("k8s") is None:
        return None
    k = doc.sub("k8s")
    releases = tuple(_parse_release(r, repo) for r in k.items("releases"))
    names = [r.name for r in releases]
    if len(set(names)) != len(names):
        raise k.fail("releases", f"release names repeat: {names}")
    endpoints = k.str_map("endpoints", {})
    fields = _address_fields(doc)
    for path in endpoints:
        if path not in fields:
            raise k.fail(f"endpoints.{path}", f"not a URL-bearing field; known: {', '.join(sorted(fields))}")
    return K8sDef(k.str("namespace"), releases, endpoints)


def _open(
    stack_dir: Path, runtime: Runtime | None, repo: Path
) -> tuple[_Doc, AgentSurface, tuple[Credential, ...], Runtime, K8sDef | None, dict[str, frozenset[str]]]:
    """Parse stack.yaml under `runtime` (None: OTB_RUNTIME). Under k8s the endpoint overrides are applied to the
    raw document before any field is read, so every reader sees one set of addresses."""
    runtime = runtime or runtime_from_env()
    path = stack_dir / "stack.yaml"
    doc = _Doc(path, yaml.safe_load(path.read_text()))
    k8s = _parse_k8s(doc, repo)
    fields = _address_fields(doc)
    origins = _origins({p: holder[key] for p, (holder, key) in fields.items()})
    if k8s is not None:
        for origin, keys in _origins(k8s.endpoints).items():
            origins.setdefault(origin, set()).update(keys)
        if runtime == "k8s":
            for p, value in k8s.endpoints.items():
                holder, key = fields[p]
                holder[key] = value
    surface = _parse_surface(doc)
    if surface.id != stack_dir.name:
        raise doc.fail("id", f"must equal the directory name {stack_dir.name!r}")
    mcp = surface.mcp
    if isinstance(mcp, StdioLaunch) and mcp.kind in ("venv", "node", "binary") and mcp.lock is None:
        raise doc.fail("mcp.lock", f"required for kind {mcp.kind}")
    creds = _parse_credentials(doc)
    unknown = sorted(set(surface.credentials) - {c.name for c in creds})
    if unknown:
        raise doc.fail("mcp", f"{unknown} are neither declared credentials nor OTB_* launch variables")
    return doc, surface, creds, runtime, k8s, {o: frozenset(k) for o, k in origins.items()}


def load_surface(stack_dir: Path, runtime: Runtime | None = None, repo: Path = REPO) -> AgentSurface:
    """What the run path reads from a stack directory. Test stacks (tests/fixtures/stacks) declare only this part."""
    return _open(stack_dir, runtime, repo)[1]


def load_stack(stack_dir: Path, runtime: Runtime | None = None, repo: Path = REPO) -> StackDef:
    doc, surface, creds, runtime, k8s, origins = _open(stack_dir, runtime, repo)
    sid = surface.id
    compose_file = stack_dir / doc.str("compose_file")
    if not compose_file.exists():
        raise doc.fail("compose_file", f"{compose_file} does not exist")

    ing = doc.sub("ingest")
    ingest = IngestEndpoint(
        ing.str("service"),
        ing.str("url"),
        ing.str("health_url"),
        ing.str("metrics_url"),
        _signal_map(ing, "paths"),
        cast(int, ing.get("max_inflight", int, 4)),
    )

    late = doc.sub("max_late_s")
    max_late: dict[Signal, float | None] = {}
    for s in SIGNALS:
        v = late.get(s, (int, float, type(None)))
        max_late[s] = None if v is None else float(cast(float, v))

    post_up = doc.strs("post_up", [])
    for c in creds:
        if c.kind == "provisioned" and c.step not in post_up:
            raise doc.fail("credentials", f"{c.name}: step {c.step!r} is not listed in post_up")

    readiness = []
    for r in doc.items("readiness"):
        auth = r.strs("basic_auth", [])
        if auth and len(auth) != 2:
            raise r.fail("basic_auth", "must be [user credential, password credential]")
        readiness.append(
            Readiness(
                r.str("name"),
                r.str("url"),
                cast(int, r.get("expect_status", int, 200)),
                (auth[0], auth[1]) if auth else None,
                cast(int, r.get("timeout_s", int, 300)),
            )
        )

    probes_path = stack_dir / "probes.py"
    if not probes_path.exists():
        raise StackDefError(f"{probes_path}: missing")
    probes = cast(StackProbes, _load_module(f"otb_stack_{sid.replace('-', '_')}", probes_path))
    for fn in ("probe", "provisioning", "is_empty"):
        if not callable(getattr(probes, fn, None)):
            raise StackDefError(f"{probes_path}: must define {fn}()")
    missing = [s for s in post_up if s not in probes.provisioning()]
    if missing:
        raise doc.fail("post_up", f"{missing} not implemented in {probes_path}")

    return StackDef(
        id=sid,
        surface=surface,
        family=cast(Family, doc.literal("family", get_args(Family))),
        dir=stack_dir,
        runtime=runtime,
        compose_file=compose_file,
        k8s=k8s,
        ingest=ingest,
        collector=_parse_collector(doc.sub("collector"), stack_dir),
        urls=doc.str_map("urls", {}),
        arrival_time_sensitive=cast(bool, doc.get("arrival_time_sensitive", bool)),
        max_late_s=max_late,
        settle_s=float(cast(float, doc.get("settle_s", (int, float)))),
        credentials=creds,
        post_up=post_up,
        readiness=tuple(readiness),
        probes=probes,
        origins=origins,
    )


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def registry(repo: Path = REPO, runtime: Runtime | None = None) -> dict[str, StackDef]:
    runtime = runtime or runtime_from_env()
    stacks = {
        d.name: load_stack(d, runtime, repo) for d in sorted((repo / "stacks").iterdir()) if (d / "stack.yaml").exists()
    }
    seen: dict[str, str] = {}
    for s in stacks.values():
        for c in s.credentials:
            if c.name in seen:
                raise StackDefError(f"{s.dir / 'stack.yaml'}: credentials: {c.name} also declared by {seen[c.name]}")
            seen[c.name] = s.id
    return stacks


def select(stacks: Mapping[str, StackDef], names: str | None) -> list[StackDef]:
    """'b1,b4' or 'b1-lgtm,b4-parseable'; None selects all."""
    if not names:
        return list(stacks.values())
    out = []
    for n in names.split(","):
        hits = [s for sid, s in stacks.items() if sid == n or sid.split("-")[0] == n]
        if len(hits) != 1:
            raise SystemExit(f"unknown stack {n!r}; known: {', '.join(stacks)}")
        out.append(hits[0])
    return out
