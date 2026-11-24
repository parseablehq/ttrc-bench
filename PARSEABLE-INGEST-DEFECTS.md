# Parseable OTLP ingestion defects, reproduced on v3.2.1 (commit 503fa56), standalone, local-store

All found while feeding the OpenTelemetry Demo 3.1.0 through an OTel Collector (`otlphttp`, `encoding: json`)
into Parseable, ClickHouse and the Grafana stack in parallel. ClickHouse and Loki accept every payload below.
Each reproduces with the single-record payload shown, posted to `POST /v1/logs` or `/v1/metrics` with
`X-P-Stream` and basic auth. Full timestamps omitted for brevity (`$NOW` = current epoch nanoseconds).

## 1. A log record with an empty attribute value rejects the whole batch

Payload: one log record whose attribute has an OTLP value with no kind set, `{"key":"amount.nanos","value":{}}`.
The OpenTelemetry Demo's payment service emits this on `app.payment.*` logs.

Response: `HTTP 400  Could not deserialize into JSON object, Invalid data for Value, no known keys found`

Effect: the collector treats 400 as permanent and drops the entire batch (2,000 records under its default batch
size). In a 15-minute sample, one such record cost 2,000 log lines.

Expected: store the attribute as null (or drop that attribute), accept the record.

## 2. A log record with an empty body rejects the whole batch

Payload: one log record with `"body": {}`. The demo's shipping service emits this for feature-flag evaluation
logs (`feature_flag.key = intlShippingSlowdown`), about one every three minutes.

Response: the same `HTTP 400` as above. Effect: 49 to 70 records dropped per occurrence in a live run.

Expected: store an empty body, accept the record.

## 3. Severity text is discarded when no severity number is sent

Payload: one log record with `"severityText": "error"` and no `severityNumber`. Node.js and Python OTel log
bridges commonly send text only.

Stored: `severity_text = "UNSPECIFIED"`, `severity_number = 0`. ClickHouse stores `SeverityText = "error"`.

Effect: in a sample of the demo, all 31 error logs of the payment service, including the ones that explain the
planted incident, are invisible to any query on severity. An agent answering "which services logged errors"
from Parseable gets a wrong answer through no fault of its own.

Expected: keep the text as sent; derive the number from the text when the number is absent, not the reverse.

## 4. Integer metric data points are stored without a value

Payload: a `sum` metric with `"asInt": "42"`, a `gauge` with `"asInt": "7"`, and a `sum` with `"asDouble": 42.0`.

Stored: the `asDouble` sum has `data_point_value = 42.0`; the `asInt` sum and gauge have NULL in
`data_point_value`, `data_point_count` and `data_point_sum`.

Effect at scale: every integer-valued metric in the demo has no values. Verified over a 38-minute run:
`traces.span.metrics.calls` (5,484 rows, all NULL), every `container.*` counter from `docker_stats` (22,307 rows
each, all NULL), `system.memory.usage` (all NULL). Double-valued metrics (`process.cpu.time`,
`system.cpu.utilization`) are intact. The OTLP spec makes `asInt` and `asDouble` a oneof; both must be read.

Root cause narrowed: `asInt` is read only when it is a JSON number. Sent as `"asInt": 42` the value is stored;
sent as `"asInt": "42"` it is NULL. The proto3 JSON mapping that OTLP/JSON follows encodes int64 as a string,
and the OpenTelemetry Collector always emits the string form (0 numeric `asInt` occurrences in a 38-minute
recording). So every collector-fed integer metric is lost.

Expected: parse `asInt` as int64 from a JSON string, per the OTLP/JSON encoding.

## Status on v3.2.2 with `P_NORMALISE_OTEL_PAYLOAD=true` (checked 2026-09-28)

All four defects above still reproduce. The flag (#1788) only adds a missing `values` array to `arrayValue` and
`kvlistValue` before retrying deserialisation; it does not handle an AnyValue with no kind set (cases 1 and 2),
and cases 3 and 4 parse successfully and are stored wrong. A 38-minute replay (`corpus/DRYRUN-RUN4.md`) stored
all 5,484 `traces.span.metrics.calls` points with NULL values.

## Found on v3.2.3 (checked 2026-10-02)

### 5. A metric whose data points carry exemplars is not stored, and the request answers 200

Any sum, gauge or histogram whose points include an `exemplars` array is dropped in full: zero rows are stored
for that metric, while the OTLP request returns HTTP 200 with an empty `partialSuccess`. One exemplar on one point
is enough. A metric with two points, only the second carrying an exemplar, stores zero rows, not one.
In the OTel Demo sample this removed 24 metric names, including the span-metrics histograms.

Reproduction: `uv run python tests/live/parseable_exemplars.py` against the `b4-parseable` stack (one request per
case straight to Parseable, no collector). Result in `docs/v1/units/U4-parseable-exemplars.json`:

| case | points sent | with exemplar | HTTP | rows stored |
|---|---|---|---|---|
| sum, plain | 1 | 0 | 200 | 1 |
| sum, exemplar | 1 | 1 | 200 | 0 |
| gauge, plain | 1 | 0 | 200 | 1 |
| gauge, exemplar | 1 | 1 | 200 | 0 |
| histogram, plain | 1 | 0 | 200 | 1 |
| histogram, exemplar | 1 | 1 | 200 | 0 |
| sum, two points, second has exemplar | 2 | 1 | 200 | 0 |

Expected: store the points (and the exemplars, or drop only the exemplars), or reject the request with an error.

### 6. Query results are wrong for about two minutes after ingest

Right after a batch is acknowledged, queries over it return counts that are wrong in both directions: 9,263 of
9,479 spans, and 3,018 logs for a one-minute window that holds 1,676. The same queries were exact 122 seconds
later. Evidence: `docs/v1/units/U4-probes-b4-unsettled.json` against `U4-probes.json`. The benchmark waits a
per-stack settle time before checking Parseable.

### 7. A log record without `timeUnixNano` gets its arrival time, not `observedTimeUnixNano`

OTLP makes `timeUnixNano` optional; a record without it still carries `observedTimeUnixNano`, the time the SDK
or collector saw it. Parseable stores such a record with `time_unix_nano` set to the moment the request
reached Parseable, not to the observed time, and not to null.

Evidence, `uv run python tests/live/parseable_timeless_logs.py state/sessions/dryrun-replay-20261002T004236Z`
(41-minute dry run, read-only, `docs/v1/units/U6b-parseable-timeless-logs.json`):

| service | records sent | without timeUnixNano | stored | time within 5 ms before p_timestamp | time = observed | time minus observed |
|---|---|---|---|---|---|---|
| currency | 2,410 | 2,410 | 2,410 | 2,410 | 0 | 6 ms to 3,976 ms |
| shipping | 785 | 785 | 785 | 785 | 0 | 8 ms to 4,014 ms |
| payment | 321 | 0 | 321 | 0 | 321 | 0 |
| all 11 other services | 73,253 | 0 | 73,253 | 6 | 24,943 | at most 0 |

One stored currency record: `time_unix_nano 00:44:29.928`, `p_timestamp 00:44:29.928`,
`observed_time_unix_nano 00:44:28.656`. Every record is stored, so a count over a window that covers whole
batches is exact. A record moves up to four seconds later, though, so per-minute counts are off: currency 61
for 60 sent in 07:51, 65 for 59 in 08:11. In the benchmark's gate this is 13 failed log counts, plus 6 more over
all services. ClickHouse, Elasticsearch and Loki pass the same checks, so they date these records by
`observedTimeUnixNano`.

Expected: use `observedTimeUnixNano` when `timeUnixNano` is absent or zero, as the OTel log data model
recommends, or store null; never the arrival time.

### 8. A query drops the final partial minute of its time range

Rows whose `p_timestamp` falls in the last, incomplete minute of `[startTime, endTime)` are not returned. Found on
the 45-minute live capture (session `live-45-follow-20261002T201321Z`), read-only:

| query range | predicate | rows returned |
|---|---|---|
| 20:40:00 to 20:40:30 | `p_timestamp` in [20:40:00, 20:40:30) | 0 |
| 20:40:00 to 20:40:59 | same | 0 |
| 20:40:00 to 20:41:00 | same | 760 |
| 20:39:00 to 20:39:30 | `p_timestamp` in [20:39:00, 20:39:30) | 0 |
| 20:39:00 to 20:40:00 | `p_timestamp` in [20:39:00, 20:40:00) | 1,438 |

The same holds for traces: a span stamped 20:44:01.120 is missing for every `endTime` from 20:44:02 to 20:44:59 and
present from 20:45:00. Any query whose range does not end on a minute boundary silently loses up to 59 seconds of
data, which an agent asking about "08:14:12 to 08:19:19" will not notice. The benchmark's gate probes round their
`endTime` up to the next minute (with an event-time predicate keeping the count exact); agents get no such help.

Expected: return every row whose `p_timestamp` is in `[startTime, endTime)`.

## Vendor response (Parseable, 2026-10-02)

| item | response | status in the benchmark |
|---|---|---|
| 1, 2 | Caused by the OTel JSON deserialisation library; OSS accepts only JSON, Enterprise also accepts protobuf, which avoids it. A flag handles null fields at some ingestion cost. | Flag name and first version pending. `P_NORMALISE_OTEL_PAYLOAD=true` did not cover these cases on v3.2.2 or v3.2.3. The normaliser workarounds stay until the flag is verified on the B4 stack. |
| 3 | Fixed in the upstream OTel crates; Parseable will pick it up. | Re-test on the release that includes it. |
| 4 | Changing the behaviour would conflict with existing data and need a renamed `_int` column; under discussion. | B4 stays excluded from metric-value checks. Suggested option: parse the `asInt` string into the existing float column (exact up to 2^53). |
| 5, 6, 7 | Not yet reviewed. | Named gate failures with committed reproductions. |
| Related 1 (span events as rows) | Needs an MCP update. | |
| Related 2 (schema size) | May need a new API or a flag. | |
| Related 3, 4 (PromQL, protobuf) | Enterprise only. | The OSS MCP server still lists `query_promql`, which returns the web UI's HTML; reported as an MCP issue. |

## Workaround in the benchmark (2026-10-02)

Parseable chose a collector-side workaround over upstream fixes for now. The B4 ingest collector
(`stacks/b4-parseable/collector.yaml`) applies three OTTL transforms, declared in `stack.yaml` and printed in every
report:

| Defect | Transform |
|---|---|
| 4, integer points stored as NULL | `set(value_double, Double(value_int)) where value_double == 0.0` (exact up to 2^53) |
| 5, metrics with exemplars not stored | `set(exemplars, nil)` |
| 7, timeless logs dated by arrival | `set(time_unix_nano, observed_time_unix_nano) where time_unix_nano == 0` |

`tests/live/parseable_b4_transforms.py` checks each end to end. With them, a fresh replay of the dry run passes the
gate on all 1,009 checks (session `dryrun-replay-20261002T130813Z`), against 947 without. Defects 1 and 2 are still
handled by the archive normaliser for every stack. Defect 6 (results wrong for about two minutes after ingest) is
handled by the gate's settle time. Arrival-time dating in live capture (rows land 7-11 s after their event time) has
no OSS workaround and stays a disclosed finding.

## Related, not ingestion bugs

- Span events are stored as extra rows in `otel_traces`: 44,159 rows for 25,833 spans in one sample. `count(*)`
  over spans overcounts unless the user knows to `count(DISTINCT span_span_id)`. Nothing in the schema or the
  MCP tool descriptions says so.
- `get_dataset_schema` via MCP returns about 50 KB for the traces dataset (one column per attribute, each with
  `dict_id`, `dict_is_ordered`, `nullable`, empty `metadata`), and there is no `information_schema` to filter
  columns. In agent runs this one tool was 80% of all bytes Parseable returned.
- `query_promql` via MCP on the OSS build returns the web UI's HTML page rather than an error saying PromQL is
  not available in OSS.
- OTLP over protobuf is rejected on OSS; collectors must set `encoding: json`. Documented nowhere obvious.

## Pipeline used, for reproduction

OpenTelemetry Demo 3.1.0 → demo's `opentelemetry-collector-contrib` 0.159 (OTLP receiver, batch, redaction,
`rr-web` log filter) → `otlphttp` exporter with `encoding: json` → a second contrib collector 0.161 (batch) →
`otlphttp` exporter with `encoding: json`, `X-P-Stream` header, basic auth → Parseable `/v1/{logs,metrics,traces}`.
The second collector fans the same batches to ClickHouse (`clickhouse` exporter) and the Grafana stack (`otlphttp`),
which store all four cases correctly. Every defect above also reproduces with a hand-written single-record
payload sent directly with curl, with no collector involved.
