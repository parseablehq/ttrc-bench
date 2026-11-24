"""ReferenceStore: DuckDB views over the reference Parquet, and named measures, the only way truth is computed.

A measure is otb/ref/measures/<name>.sql with typed `$param` placeholders plus <name>.yaml (description, params,
result columns, the gate check families that can verify it on a stack). A `window` parameter named w binds as
$w_start and $w_end, half-open, in recording time.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

import duckdb
import orjson
import yaml

from otb.archive import Archive
from otb.ref.build import builder_sha256, read_built, ref_dir
from otb.ref.schema import SCHEMA_VERSION, TABLES
from otb.timebase import EventNs, Window, parse_utc

MEASURES_DIR = Path(__file__).parent / "measures"
ParamType = Literal["str", "int", "float", "bool", "window", "str_list"]
PARAM_TYPES = ("str", "int", "float", "bool", "window", "str_list")
FORBIDDEN = re.compile(r"\b(now|random|current_timestamp|current_date|uuid|gen_random_uuid)\s*\(", re.I)
Param = str | int | float | bool | Window | Sequence[str] | None


class MeasureError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ParamSpec:
    type: ParamType
    required: bool
    default: object = None


@dataclass(frozen=True, slots=True)
class MeasureDef:
    name: str
    description: str
    params: Mapping[str, ParamSpec]
    columns: tuple[str, ...]
    exact_on: tuple[str, ...]
    sql: str


@dataclass(frozen=True, slots=True)
class Rows:
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    sha256: str

    def one(self) -> dict[str, object]:
        if len(self.rows) != 1:
            raise MeasureError(f"expected one row, got {len(self.rows)}")
        return dict(zip(self.columns, self.rows[0], strict=True))

    def as_dicts(self) -> list[dict[str, object]]:
        return [dict(zip(self.columns, r, strict=True)) for r in self.rows]


def _parse_param_spec(name: str, spec: object) -> ParamSpec:
    if isinstance(spec, str):
        optional = spec.endswith("?")
        typ = spec.removesuffix("?")
        spec = {"type": typ, "default": None} if optional else {"type": typ}
    if not isinstance(spec, dict) or spec.get("type") not in PARAM_TYPES:
        raise MeasureError(f"param {name}: type must be one of {PARAM_TYPES}, got {spec!r}")
    return ParamSpec(spec["type"], "default" not in spec, spec.get("default"))


def _placeholders(spec_name: str, spec: ParamSpec) -> set[str]:
    return {f"{spec_name}_start", f"{spec_name}_end"} if spec.type == "window" else {spec_name}


def load_measure(path: Path) -> MeasureDef:
    """Parse and lint one measure: deterministic ORDER BY, no clock or randomness, params and placeholders agree."""
    meta = yaml.safe_load(path.with_suffix(".yaml").read_text())
    sql = path.read_text()
    name = path.stem
    params = {k: _parse_param_spec(k, v) for k, v in (meta.get("params") or {}).items()}
    if "order by" not in sql.lower():
        raise MeasureError(f"{name}: no ORDER BY, so row order is not deterministic")
    if FORBIDDEN.search(sql):
        raise MeasureError(f"{name}: uses a clock or randomness")
    used = set(re.findall(r"\$([a-z_][a-z0-9_]*)", sql))
    declared = set().union(*(_placeholders(k, s) for k, s in params.items())) if params else set()
    if used != declared:
        raise MeasureError(f"{name}: placeholders {sorted(used)} != declared params {sorted(declared)}")
    return MeasureDef(
        name=name,
        description=meta["description"].strip(),
        params=params,
        columns=tuple(meta["columns"]),
        exact_on=tuple(meta.get("exact_on") or ()),
        sql=sql,
    )


@cache
def measures() -> dict[str, MeasureDef]:
    return {p.stem: load_measure(p) for p in sorted(MEASURES_DIR.glob("*.sql"))}


def parse_window(text: str) -> Window:
    """'<start>/<end>' as ns integers or ISO-8601 UTC with a date."""
    a, _, b = text.partition("/")
    if not b:
        raise MeasureError(f"window must be '<start>/<end>', got {text!r}")
    conv = (lambda s: EventNs(int(s))) if a.strip().isdigit() else (lambda s: EventNs(parse_utc(s)))
    return Window(conv(a), conv(b))


def parse_param(spec: ParamSpec, text: str) -> Param:
    """CLI boundary: a `--param k=v` string to the declared type."""
    match spec.type:
        case "str":
            return text
        case "int":
            return int(text)
        case "float":
            return float(text)
        case "bool":
            if text.lower() not in ("true", "false"):
                raise MeasureError(f"bool param must be true or false, got {text!r}")
            return text.lower() == "true"
        case "window":
            return parse_window(text)
        case "str_list":
            return [x for x in text.split(",") if x]


def _bind(m: MeasureDef, params: Mapping[str, Param]) -> dict[str, object]:
    unknown = set(params) - set(m.params)
    if unknown:
        raise MeasureError(f"{m.name}: unknown params {sorted(unknown)}")
    out: dict[str, object] = {}
    for name, spec in m.params.items():
        if name in params:
            v = params[name]
        elif spec.required:
            raise MeasureError(f"{m.name}: missing param {name}")
        else:
            v = spec.default
        if spec.type == "window":
            if not isinstance(v, Window):
                raise MeasureError(f"{m.name}: {name} must be a Window")
            out[f"{name}_start"], out[f"{name}_end"] = v.start, v.end
        elif v is not None and spec.type == "str_list":
            out[name] = list(v)
        else:
            out[name] = v
    return out


class ReferenceStore:
    def __init__(self, con: duckdb.DuckDBPyConnection, instances: tuple[str, ...] = ()) -> None:
        self.con = con
        self.instances = instances
        self._scoped: dict[str, ReferenceStore] = {}

    @staticmethod
    def open(
        corpus: Path,
        *,
        memory_limit: str = "4GB",
        threads: int = 4,
        frames: Iterable[tuple[str, int]] | None = None,
    ) -> ReferenceStore:
        """Refuses a partial or stale build: truth from part of the corpus would be wrong without saying so.
        `frames` ((seg, frame) pairs, seg as `<signal>-<stream>-<seq:06d>`) restricts every record table to those
        frames: the reference for a session that replayed only part of the corpus."""
        out = ref_dir(corpus)
        built = read_built(out)
        sha = builder_sha256()
        for seg in Archive(corpus).segments():
            b = built.get(seg.sha256)
            if b is None or b.builder_sha256 != sha or b.schema_version != SCHEMA_VERSION:
                raise MeasureError(f"{corpus}: {seg.path} is not built by the current builder; run `otb ref build`")
        con = duckdb.connect()
        tmp = corpus / "derived" / "duckdb_tmp"
        con.execute(f"SET memory_limit = '{memory_limit}'")
        con.execute(f"SET threads = {int(threads)}")
        con.execute(f"SET temp_directory = '{_sql_str(str(tmp))}'")
        if frames is not None:
            con.execute("CREATE TABLE selected_frames (seg VARCHAR, frame INTEGER)")
            con.executemany("INSERT INTO selected_frames VALUES (?, ?)", list(frames))
        for table in TABLES:
            glob = _sql_str(str(out / table / "*.parquet"))
            only = (
                " WHERE (seg, frame) IN (SELECT seg, frame FROM selected_frames)"
                if frames is not None and table != "resources"
                else ""
            )
            con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet('{glob}'){only}")
        return ReferenceStore(con, Archive(corpus).manifest.instances)

    def scoped(self, instance: str) -> ReferenceStore:
        """The store as one demo instance's questions see it: every table holds only records whose resource has
        cloud.region = instance, so background (cloud.region = bg) and other instances never enter a measure. A
        record without cloud.region belongs to the instance only when the corpus has one instance (a recording
        made before the capture attributed instances)."""
        if instance in self._scoped:
            return self._scoped[instance]
        if instance not in self.instances:
            raise MeasureError(f"instance {instance!r} is not one of the corpus's instances {list(self.instances)}")
        schema = "scope_" + hashlib.sha256(instance.encode()).hexdigest()[:16]
        rule = f"region = '{_sql_str(instance)}'" + (" OR region IS NULL" if len(self.instances) == 1 else "")
        self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for table in TABLES:
            body = f"SELECT * FROM main.{table} WHERE {rule}"
            if table == "span_events":  # its rows carry no resource columns; they belong to the scoped spans
                own = f"SELECT trace_id, span_id FROM {schema}.spans"
                body = f"SELECT * FROM main.{table} WHERE (trace_id, span_id) IN ({own})"
            self.con.execute(f"CREATE OR REPLACE VIEW {schema}.{table} AS {body}")
        cur = self.con.cursor()
        cur.execute(f"SET search_path = '{schema}'")
        self._scoped[instance] = ReferenceStore(cur, (instance,))
        return self._scoped[instance]

    def measures(self) -> Mapping[str, MeasureDef]:
        return measures()

    def run_measure(self, name: str, params: Mapping[str, Param]) -> Rows:
        m = measures().get(name)
        if m is None:
            raise MeasureError(f"unknown measure {name!r}; known: {sorted(measures())}")
        cur = self.con.execute(m.sql, _bind(m, params))
        columns = tuple(d[0] for d in cur.description)
        if columns != m.columns:
            raise MeasureError(f"{name}: SQL returns columns {columns}, yaml declares {m.columns}")
        rows = tuple(tuple(r) for r in cur.fetchall())
        digest = hashlib.sha256(orjson.dumps({"columns": columns, "rows": rows}, option=orjson.OPT_SORT_KEYS))
        return Rows(columns, rows, digest.hexdigest())


def _sql_str(s: str) -> str:
    return s.replace("'", "''")
