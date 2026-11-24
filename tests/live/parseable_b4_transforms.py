"""End-to-end check of the B4 collector transforms (stacks/b4-parseable/collector.yaml) against a running B4 stack:
integer points stored as doubles, metrics with exemplars stored, a log without timeUnixNano dated by its observed
time, and double points unchanged. Run with the b4-parseable stack up:

  uv run python tests/live/parseable_b4_transforms.py
"""

import time

from otb.stacks import StackContext, basic, registry

ctx = StackContext(registry()["b4-parseable"])
admin = basic(ctx.secret("B4_PARSEABLE_USERNAME"), ctx.secret("B4_PARSEABLE_PASSWORD"))
now = time.time_ns()
obs = now - 2_000_000_000
svc = f"probe{now // 1_000_000_000}"
res = {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]}
ex = [
    {
        "timeUnixNano": str(now),
        "asInt": "1",
        "traceId": "87fe0d009eca24dedb3e337ad40e4adb",
        "spanId": "f8ef1847050f5058",
    }
]
metrics = {
    "resourceMetrics": [
        {
            "resource": res,
            "scopeMetrics": [
                {
                    "metrics": [
                        {
                            "name": "c_int",
                            "sum": {
                                "aggregationTemporality": 2,
                                "isMonotonic": True,
                                "dataPoints": [{"timeUnixNano": str(now), "asInt": "7", "exemplars": ex}],
                            },
                        },
                        {"name": "g_dbl", "gauge": {"dataPoints": [{"timeUnixNano": str(now), "asDouble": 2.5}]}},
                        {"name": "g_int0", "gauge": {"dataPoints": [{"timeUnixNano": str(now), "asInt": "0"}]}},
                        {
                            "name": "h_ex",
                            "histogram": {
                                "aggregationTemporality": 2,
                                "dataPoints": [
                                    {
                                        "timeUnixNano": str(now),
                                        "count": "2",
                                        "sum": 3.0,
                                        "bucketCounts": ["1", "1"],
                                        "explicitBounds": [1.0],
                                        "exemplars": [{"timeUnixNano": str(now), "asDouble": 1.0}],
                                    }
                                ],
                            },
                        },
                    ]
                }
            ],
        }
    ]
}
logs = {
    "resourceLogs": [
        {
            "resource": res,
            "scopeLogs": [
                {
                    "logRecords": [
                        {"observedTimeUnixNano": str(obs), "body": {"stringValue": "no time"}},
                        {
                            "timeUnixNano": str(now),
                            "observedTimeUnixNano": str(obs),
                            "body": {"stringValue": "has time"},
                        },
                    ]
                }
            ],
        }
    ]
}
base = ctx.stack.ingest.url
for sig, body in (("metrics", metrics), ("logs", logs)):
    r = ctx.http("POST", f"{base}/v1/{sig}", body=body)
    print(sig, "via ingest-b4:", r.status)
time.sleep(130)  # Parseable's settle time (defect 6)


def q(sql):
    return ctx.http_json(
        "POST",
        f"{ctx.url('parseable')}/api/v1/query",
        headers=admin,
        body={"query": sql, "startTime": "2000-01-01T00:00:00Z", "endTime": "2100-01-01T00:00:00Z"},
    )


rows = q(f"SELECT * FROM otel_metrics WHERE \"service.name\" = '{svc}'")
for r in rows:
    print(
        {
            k: v
            for k, v in r.items()
            if k in ("metric_name", "data_point_value", "data_point_sum", "data_point_count", "metric_type")
            or "value" in k
        }
    )
for r in q(f"SELECT body, time_unix_nano, observed_time_unix_nano FROM otel_logs WHERE \"service.name\" = '{svc}'"):
    print(r)
print("sent now=", now, "obs=", obs)
