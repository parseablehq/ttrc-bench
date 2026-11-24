"""Single-request reproduction of the Parseable v3.2.3 exemplar defect found in U4 check 3: metric data points that
carry exemplars are not stored, while the OTLP request answers HTTP 200.

Each case is one OTLP/JSON request POSTed straight to Parseable (no collector) into a fresh dataset; the stored
row count per metric name is read back. Writes docs/v1/units/U4-parseable-exemplars.json.

  uv run python tests/live/parseable_exemplars.py
"""

from __future__ import annotations

import json
import time

from otb.stacks import REPO, StackContext, basic, registry

EXEMPLAR = {"asInt": "1", "traceId": "87fe0d009eca24dedb3e337ad40e4adb", "spanId": "f8ef1847050f5058"}


def point(now: int, i: int, kind: str, exemplar: bool) -> dict:
    p: dict = {
        "attributes": [{"key": "i", "value": {"stringValue": str(i)}}],
        "startTimeUnixNano": str(now - 60_000_000_000),
        "timeUnixNano": str(now),
    }
    if kind == "histogram":
        p |= {"count": "2", "sum": 3.0, "bucketCounts": ["1", "1"], "explicitBounds": [1.0]}
    else:
        p["asDouble"] = 5.0
    if exemplar:
        p["exemplars"] = [EXEMPLAR | {"timeUnixNano": str(now - 1_000_000_000)}]
    return p


def metric(name: str, kind: str, points: list[dict]) -> dict:
    body: dict = {"dataPoints": points}
    if kind != "gauge":
        body["aggregationTemporality"] = 2
    if kind == "sum":
        body["isMonotonic"] = True
    return {"name": name, "unit": "1", kind: body}


def main() -> None:
    stack = registry()["b4-parseable"]
    ctx = StackContext(stack)
    admin = basic(ctx.secret("B4_PARSEABLE_USERNAME"), ctx.secret("B4_PARSEABLE_PASSWORD"))
    now = time.time_ns()
    cases: dict[str, tuple[str, list[bool]]] = {
        "sum_plain": ("sum", [False]),
        "sum_exemplar": ("sum", [True]),
        "gauge_plain": ("gauge", [False]),
        "gauge_exemplar": ("gauge", [True]),
        "histogram_plain": ("histogram", [False]),
        "histogram_exemplar": ("histogram", [True]),
        "sum_two_points_second_has_exemplar": ("sum", [False, True]),
    }
    dataset = f"otb_exemplar_repro_{now // 1_000_000_000}"
    out: dict[str, object] = {"unit": "U4 Parseable exemplar reproduction", "dataset": dataset, "cases": {}}
    for name, (kind, flags) in cases.items():
        req = {
            "resourceMetrics": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "repro"}}]},
                    "scopeMetrics": [
                        {
                            "scope": {"name": "repro"},
                            "metrics": [
                                metric(f"repro.{name}", kind, [point(now, i, kind, f) for i, f in enumerate(flags)])
                            ],
                        }
                    ],
                }
            ]
        }
        r = ctx.http("POST", f"{ctx.url('parseable')}/v1/metrics", body=req, headers=admin | {"X-P-Stream": dataset})
        out["cases"][name] = {
            "kind": kind,
            "points_sent": len(flags),
            "with_exemplar": sum(flags),  # type: ignore[index]
            "http_status": r.status,
        }
    time.sleep(3)
    rows = ctx.http_json(
        "POST",
        f"{ctx.url('parseable')}/api/v1/query",
        headers=admin,
        body={
            "query": f"SELECT metric_name AS m, count(*) AS c FROM {dataset} GROUP BY 1",
            "startTime": "2000-01-01T00:00:00Z",
            "endTime": "2100-01-01T00:00:00Z",
        },
    )
    stored = {r["m"]: r["c"] for r in rows}  # type: ignore[union-attr]
    for name, case in out["cases"].items():  # type: ignore[union-attr]
        case["rows_stored"] = stored.get(f"repro.{name}", 0)
        print(
            f"{name:40s} sent={case['points_sent']} exemplars={case['with_exemplar']} "
            f"http={case['http_status']} stored={case['rows_stored']}"
        )
    out["other_rows"] = {k: v for k, v in stored.items() if not k.startswith("repro.")}
    (REPO / "docs" / "v1" / "units" / "U4-parseable-exemplars.json").write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
