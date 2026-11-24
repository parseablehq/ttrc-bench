"""Normaliser n2's flag-fingerprint scrub, on hand-made OTLP/JSON batches. Failure modes first: the leaks the
scrub exists to stop, then what it must keep."""

import orjson

from otb.normalise import normalise


def _res(service: str, *extra: dict) -> dict:
    return {"attributes": [{"key": "service.name", "value": {"stringValue": service}}, *extra]}


def _kv(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def test_flagd_spans_and_openfeature_evaluations_leave_no_trace() -> None:
    raw = {
        "resourceSpans": [
            {
                "resource": _res("flagd"),
                "scopeSpans": [{"spans": [{"name": "resolveBoolean", "startTimeUnixNano": "50"}]}],
            },
            {
                "resource": _res("payment", _kv("demo.feature_flag.cache", "on")),
                "scopeSpans": [
                    {
                        "scope": {"name": "s", "attributes": [_kv("feature_flag.provider.name", "flagd")]},
                        "spans": [
                            {
                                "name": "charge",
                                "startTimeUnixNano": "100",
                                "endTimeUnixNano": "200",
                                "attributes": [
                                    _kv("feature_flag.key", "paymentFailure"),
                                    _kv("feature_flag.result.variant", "50%"),
                                    _kv("app.payment.amount", "12"),
                                ],
                                "events": [
                                    {
                                        "name": "feature_flag.evaluation",
                                        "timeUnixNano": "300",
                                        "attributes": [_kv("feature_flag.key", "paymentFailure")],
                                    },
                                    {
                                        "name": "exception",
                                        "timeUnixNano": "150",
                                        "attributes": [_kv("feature_flag_variant", "x"), _kv("exception.type", "E")],
                                    },
                                ],
                                "links": [{"traceId": "t", "attributes": [_kv("feature_flag.key", "k")]}],
                            }
                        ],
                    }
                ],
            },
        ]
    }
    got = normalise("traces", orjson.dumps(raw))
    assert orjson.loads(got.body) == {
        "resourceSpans": [
            {
                "resource": _res("payment"),
                "scopeSpans": [
                    {
                        "scope": {"name": "s", "attributes": []},
                        "spans": [
                            {
                                "name": "charge",
                                "startTimeUnixNano": "100",
                                "endTimeUnixNano": "200",
                                "attributes": [_kv("app.payment.amount", "12")],
                                "events": [
                                    {
                                        "name": "exception",
                                        "timeUnixNano": "150",
                                        "attributes": [_kv("exception.type", "E")],
                                    }
                                ],
                                "links": [{"traceId": "t", "attributes": []}],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    assert (got.records, got.min_event_ns, got.max_event_ns) == (1, 100, 200)
    assert got.dropped == {"flag_service_records": 1, "flag_events": 1, "flag_attrs": 6}


def test_flag_metrics_and_point_attributes_are_dropped() -> None:
    def gauge(name: str, *attrs: dict) -> dict:
        return {
            "name": name,
            "gauge": {"dataPoints": [{"timeUnixNano": "10", "asInt": "1", "attributes": list(attrs)}]},
        }

    raw = {
        "resourceMetrics": [
            {"resource": _res("flagd-ui"), "scopeMetrics": [{"metrics": [gauge("up"), gauge("up")]}]},
            {
                "resource": _res("frontend"),
                "scopeMetrics": [
                    {
                        "metrics": [
                            gauge("feature_flag.evaluation_success_total", _kv("feature_flag.key", "adFailure")),
                            gauge("http.server.duration", _kv("feature_flag.key", "adFailure"), _kv("route", "/")),
                        ]
                    }
                ],
            },
        ]
    }
    got = normalise("metrics", orjson.dumps(raw))
    assert orjson.loads(got.body)["resourceMetrics"] == [
        {
            "resource": _res("frontend"),
            "scopeMetrics": [{"metrics": [gauge("http.server.duration", _kv("route", "/"))]}],
        }
    ]
    assert got.records == 1
    assert got.dropped == {"flag_service_records": 2, "flag_metric_points": 1, "flag_attrs": 1}


def test_log_flag_attributes_go_and_flagservice_access_lines_stay() -> None:
    line = '[2026-09-24T08:14:00.000Z] "POST /flagservice/flagd.evaluation.v1.Service/EventStream HTTP/1.1" 200'
    raw = {
        "resourceLogs": [
            {
                "resource": _res("frontend-proxy"),
                "scopeLogs": [
                    {
                        "logRecords": [
                            {"timeUnixNano": "5", "body": {"stringValue": line}},
                            {
                                "timeUnixNano": "6",
                                "body": {"stringValue": "flag evaluated"},
                                "attributes": [_kv("feature_flag_key", "cartFailure"), _kv("level", "info")],
                            },
                        ]
                    }
                ],
            }
        ]
    }
    got = normalise("logs", orjson.dumps(raw))
    records = orjson.loads(got.body)["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert records == [
        {"timeUnixNano": "5", "body": {"stringValue": line}},
        {"timeUnixNano": "6", "body": {"stringValue": "flag evaluated"}, "attributes": [_kv("level", "info")]},
    ]
    assert got.dropped == {"flag_attrs": 1}


def test_a_batch_without_fingerprints_is_unchanged_and_n2_is_idempotent() -> None:
    raw = orjson.dumps(
        {"resourceSpans": [{"resource": _res("cart"), "scopeSpans": [{"spans": [{"name": "GetCart"}]}]}]}
    )
    once = normalise("traces", raw)
    assert once.body == raw
    assert once.dropped == {}
    assert normalise("traces", once.body).body == once.body
