"""Reference tables, ref-schema/1: the one-way door that truth and gate reference values are computed over.

Every attribute value is a string under one rule (`anyvalue_str`), so presence and equality are testable the same
way for every type. Span kind and status, and metric temporality, are stored as OTel names (SERVER, ERROR,
CUMULATIVE), not enum integers. Columns promoted out of attrs: http_status, http_route, url_path on spans;
service_version, instance (service.instance.id), region (cloud.region) on every record table. `resource_id`
joins a record to its full resource attributes in `resources`.
"""

from __future__ import annotations

import pyarrow as pa

SCHEMA_VERSION = "ref-schema/1"

ATTRS = pa.map_(pa.string(), pa.string())

_RESOURCE_COLS = [
    ("service", pa.string()),
    ("service_version", pa.string()),
    ("instance", pa.string()),
    ("region", pa.string()),
]
_TAIL = [("scope", pa.string()), ("resource_id", pa.string()), ("seg", pa.string()), ("frame", pa.int32())]

SPANS = pa.schema(
    [
        ("trace_id", pa.string()),
        ("span_id", pa.string()),
        ("parent_span_id", pa.string()),
        *_RESOURCE_COLS,
        ("name", pa.string()),
        ("kind", pa.string()),
        ("start_ns", pa.int64()),
        ("end_ns", pa.int64()),
        ("duration_ns", pa.int64()),
        ("status_code", pa.string()),
        ("status_message", pa.string()),
        ("http_status", pa.int32()),
        ("http_route", pa.string()),
        ("url_path", pa.string()),
        ("events", pa.int32()),
        ("attrs", ATTRS),
        *_TAIL,
    ]
)

SPAN_EVENTS = pa.schema(
    [
        ("trace_id", pa.string()),
        ("span_id", pa.string()),
        ("service", pa.string()),
        ("ts_ns", pa.int64()),
        ("name", pa.string()),
        ("attrs", ATTRS),
        ("seg", pa.string()),
        ("frame", pa.int32()),
    ]
)

LOGS = pa.schema(
    [
        ("ts_ns", pa.int64()),
        ("observed_ns", pa.int64()),
        ("minute", pa.int64()),
        *_RESOURCE_COLS,
        ("severity_number", pa.int32()),
        ("severity_text", pa.string()),
        ("body", pa.string()),
        ("trace_id", pa.string()),
        ("span_id", pa.string()),
        ("attrs", ATTRS),
        *_TAIL,
    ]
)

POINTS = pa.schema(
    [
        ("metric", pa.string()),
        ("type", pa.string()),
        ("unit", pa.string()),
        ("monotonic", pa.bool_()),
        ("temporality", pa.string()),
        ("ts_ns", pa.int64()),
        ("start_ns", pa.int64()),
        ("minute", pa.int64()),
        *_RESOURCE_COLS,
        ("attrs", ATTRS),
        ("value", pa.float64()),
        ("value_int", pa.int64()),
        ("count", pa.int64()),
        ("sum", pa.float64()),
        ("min", pa.float64()),
        ("max", pa.float64()),
        ("bounds", pa.list_(pa.float64())),
        ("bucket_counts", pa.list_(pa.int64())),
        *_TAIL,
    ]
)

RESOURCES = pa.schema(
    [
        ("resource_id", pa.string()),
        *_RESOURCE_COLS,
        ("attrs", ATTRS),
        ("records", pa.int64()),
        ("first_ns", pa.int64()),
        ("last_ns", pa.int64()),
        ("seg", pa.string()),
    ]
)

TABLES: dict[str, pa.Schema] = {
    "spans": SPANS,
    "span_events": SPAN_EVENTS,
    "logs": LOGS,
    "points": POINTS,
    "resources": RESOURCES,
}
TABLES_BY_SIGNAL: dict[str, tuple[str, ...]] = {
    "traces": ("spans", "span_events", "resources"),
    "logs": ("logs", "resources"),
    "metrics": ("points", "resources"),
}

SPAN_KINDS = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS_CODES = {0: "UNSET", 1: "OK", 2: "ERROR"}
TEMPORALITIES = {0: "UNSPECIFIED", 1: "DELTA", 2: "CUMULATIVE"}
