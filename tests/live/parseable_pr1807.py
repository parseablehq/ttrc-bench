"""Re-test of parseablehq/parseable#1807 (head bcee847): severity normalisation (ingest defect 3) and
`GET /logstream/{name}/schema?format=compact` (related item 2, schema size).

Severity: each case is one OTLP/JSON log record POSTed straight to Parseable into a fresh dataset; the stored
severity_text and severity_number are read back, with the benchmark's error rule applied to them.
Schema: for every dataset, the bytes of the default, `format=json` and `format=compact` responses, and whether the
compact rows name the same columns as the JSON fields.

  uv run python tests/live/parseable_pr1807.py LABEL [BASE_URL]

BASE_URL defaults to the b4-parseable stack. Writes docs/v1/units/U7-parseable-pr1807-LABEL.json.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

from otb.stacks import REPO, StackContext, basic, registry

# (severityNumber or None, severityText or None)
CASES: dict[str, tuple[int | None, str | None]] = {
    "text_error_only": (None, "error"),
    "text_ERROR_only": (None, "ERROR"),
    "text_warning_only": (None, "warning"),
    "text_information_only": (None, "Information"),
    "number_17_only": (17, None),
    "number_9_only": (9, None),
    "both_9_information": (9, "Information"),
    "both_17_error": (17, "error"),
    "both_17_text_unspecified": (17, "UNSPECIFIED"),
    "neither": (None, None),
}


def is_error(number: int | None, text: str | None) -> bool:
    return (number or 0) >= 17 or (text or "").lower() in ("error", "fatal")


def severity(ctx: StackContext, base: str, admin: dict[str, str]) -> dict[str, object]:
    now = time.time_ns()
    dataset = f"otb_pr1807_sev_{now // 1_000_000_000}"
    records = []
    for i, (name, (num, text)) in enumerate(CASES.items()):
        rec: dict = {"timeUnixNano": str(now + i), "body": {"stringValue": name}}
        if num is not None:
            rec["severityNumber"] = num
        if text is not None:
            rec["severityText"] = text
        records.append(rec)
    res = {"attributes": [{"key": "service.name", "value": {"stringValue": "pr1807-repro"}}]}
    req = {"resourceLogs": [{"resource": res, "scopeLogs": [{"logRecords": records}]}]}
    r = ctx.http("POST", f"{base}/v1/logs", body=req, headers=admin | {"X-P-Stream": dataset})
    stored: dict[str, dict] = {}
    for _ in range(40):  # wait out the post-ingest settle (defect 6) until every case is visible
        time.sleep(3)
        rows = ctx.http_json(
            "POST",
            f"{base}/api/v1/query",
            headers=admin,
            body={
                "query": f"SELECT body, severity_text, severity_number FROM {dataset}",
                "startTime": "2000-01-01T00:00:00Z",
                "endTime": "2100-01-01T00:00:00Z",
            },
        )
        stored = {row["body"]: row for row in rows}  # type: ignore[union-attr]
        if len(stored) == len(CASES):
            break
    cases = {}
    for name, (num, text) in CASES.items():
        row = stored.get(name)
        got = (row["severity_number"], row["severity_text"]) if row else (None, None)
        cases[name] = {
            "sent": {"severity_number": num, "severity_text": text},
            "stored": {"severity_number": got[0], "severity_text": got[1]} if row else None,
            "error_rule_on_sent": is_error(num, text),
            "error_rule_on_stored": is_error(*got) if row else None,
        }
        print(
            f"{name:28s} sent=({num}, {text!r:15s}) stored={got} error sent/stored="
            f"{cases[name]['error_rule_on_sent']}/{cases[name]['error_rule_on_stored']}"
        )
    return {"dataset": dataset, "http_status": r.status, "cases": cases}


def raw_get(url: str, admin: dict[str, str]) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, headers=admin)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def schemas(ctx: StackContext, base: str, admin: dict[str, str]) -> dict[str, object]:
    names = sorted(d["name"] for d in ctx.http_json("GET", f"{base}/api/v1/logstream", headers=admin))  # type: ignore[union-attr]
    out = {}
    for name in names:
        url = f"{base}/api/v1/logstream/{name}/schema"
        s0, _, default = raw_get(url, admin)
        s1, _, as_json = raw_get(url + "?format=json", admin)
        s2, ctype, compact = raw_get(url + "?format=compact", admin)
        row: dict[str, object] = {
            "status": {"default": s0, "json": s1, "compact": s2},
            "bytes": {"default": len(default), "json": len(as_json), "compact": len(compact)},
            "json_equals_default": as_json == default,
            "compact_content_type": ctype,
        }
        if s0 == 200 and s2 == 200:
            json_cols = [f["name"] for f in json.loads(default)["fields"]]
            compact_lines = compact.decode().splitlines()
            compact_cols = [line.split("\t")[0] for line in compact_lines[1:]]
            row |= {
                "columns_json": len(json_cols),
                "columns_compact": len(compact_cols),
                "same_columns": sorted(json_cols) == sorted(compact_cols),
                "compact_header": compact_lines[0] if compact_lines else None,
                "compact_sample": compact_lines[1:4],
            }
        out[name] = row
        print(
            f"{name:32s} default={len(default):>7d}B compact={len(compact):>6d}B status={s0}/{s1}/{s2} "
            f"cols={row.get('columns_json')}/{row.get('columns_compact')}"
        )
    return out


def main() -> None:
    label = sys.argv[1]
    ctx = StackContext(registry()["b4-parseable"])
    base = sys.argv[2] if len(sys.argv) > 2 else ctx.url("parseable")
    admin = basic(ctx.secret("B4_PARSEABLE_USERNAME"), ctx.secret("B4_PARSEABLE_PASSWORD"))
    about = ctx.http_json("GET", f"{base}/api/v1/about", headers=admin)
    out = {
        "unit": "U7 re-test of parseablehq/parseable#1807",
        "label": label,
        "base_url": base,
        "about": {k: about.get(k) for k in ("version", "commit", "mode", "store")},  # type: ignore[union-attr]
        "schema": schemas(ctx, base, admin),  # before the severity dataset exists, so it is not in the list
        "severity": severity(ctx, base, admin),
    }
    path = REPO / "docs" / "v1" / "units" / f"U7-parseable-pr1807-{label}.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(path)


if __name__ == "__main__":
    main()
