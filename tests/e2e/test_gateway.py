"""Gateway end to end, offline: the corpus-mini frames go in over OTLP/HTTP the way the demo collector sends them,
and the archive that comes out must hold the same records.

Ways it could fail, each asserted below:
- a frame is lost, duplicated or stored under the wrong signal;
- re-serialised input (gzip, or different JSON whitespace) is stored as different bytes than the canonical body;
- a stored frame differs from the fixture's frame, or its record count differs;
- the archive does not seal on SIGTERM, or `otb corpus verify --level full` finds a problem;
- protobuf is accepted, or rejected without naming `encoding: json`;
- malformed JSON, or a logs body posted to /v1/traces, is stored instead of rejected with 400;
- the gateway report miscounts frames, records, rejects or dedupe hits.

Evidence: tests/e2e/artifacts/gateway_e2e.json.
"""

import gzip
import hashlib
import json
from pathlib import Path

from conftest import free_port, ok, otb, write_artifact
from gateway_proc import GatewayProc, health, mini_frames, post

from otb.archive import Archive

REPO = Path(__file__).resolve().parents[2]


def collector_forms(i: int, body: bytes) -> tuple[bytes, dict[str, str]]:
    """Every third frame gzip-compressed (the overlay's setting), every third re-serialised with whitespace."""
    if i % 3 == 0:
        return gzip.compress(body), {"Content-Encoding": "gzip"}
    if i % 3 == 1:
        return json.dumps(json.loads(body), indent=1).encode(), {}
    return body, {}


def test_gateway_e2e(tmp_path: Path, artifacts: Path) -> None:
    frames = mini_frames()
    out = tmp_path / "corpus"
    gw = GatewayProc(out, free_port(), tmp_path / "gateway.log", "--rotate-mb", "0.25")
    gw.start()

    statuses = []
    for i, (signal, body) in enumerate(frames):
        data, headers = collector_forms(i, body)
        statuses.append(post(gw.url, signal, data, headers)[0])
    logs_body = next(b for s, b in frames if s == "logs")
    protobuf = post(gw.url, "traces", b"\x0a\x02\x0a\x00", {"Content-Type": "application/x-protobuf"})
    bad_json = post(gw.url, "logs", b'{"resourceLogs": [')
    wrong_signal = post(gw.url, "traces", logs_body)
    resent = post(gw.url, frames[0][0], frames[0][1])
    live = health(gw.url)
    exit_code = gw.term()

    verify = ok(otb("corpus", "verify", out, "--level", "full"))
    a = Archive.open(out, verify="full")
    m = a.manifest
    src = Archive.open(REPO / "tests/fixtures/corpus-mini", verify="index").manifest
    stored = {s: sorted(e.body_sha256 for r, e in a.merged() if r.signal == s) for s in ("traces", "logs", "metrics")}
    want = {s: sorted(hashlib.sha256(b).hexdigest() for sig, b in frames if sig == s) for s in stored}
    bodies_equal = all(a.read(r, e) == b for (r, e), (_, b) in zip(a.merged(), frames, strict=True))
    report = gw.reports()[-1]
    result = {
        "posted": len(frames),
        "acked_200": statuses.count(200),
        "gzip_frames": len(range(0, len(frames), 3)),
        "protobuf": {"status": protobuf[0], "names_encoding_json": "encoding: json" in protobuf[1]},
        "bad_json_status": bad_json[0],
        "wrong_signal_status": wrong_signal[0],
        "resent_status": resent[0],
        "health_status": live["status"] if live else None,
        "gateway_exit": exit_code,
        "sealed": m.sealed,
        "verify": verify.stdout.strip().splitlines()[-1].split(": ", 1)[1],
        "frames": {s: m.totals[s].frames for s in stored},
        "records": {s: m.totals[s].records for s in stored},
        "fixture_records": {s: src.totals[s].records for s in stored},
        "bodies_sha_equal": stored == want,
        "bodies_equal_in_replay_order": bodies_equal,
        "segments": len(a.segments()),
        "report": {
            "frames": report["totals"]["frames"],
            "records": report["totals"]["records"],
            "dedupe_hits": report["totals"]["dedupe_hits"],
            "rejects": report["rejects"],
        },
    }
    write_artifact(artifacts / "gateway_e2e.json", result)

    assert result == {
        "posted": 232,
        "acked_200": 232,
        "gzip_frames": 78,
        "protobuf": {"status": 415, "names_encoding_json": True},
        "bad_json_status": 400,
        "wrong_signal_status": 400,
        "resent_status": 200,
        "health_status": "ok",
        "gateway_exit": 0,
        "sealed": True,
        "verify": "full verify ok (0 problems)",
        "frames": {"traces": 90, "logs": 89, "metrics": 53},
        "records": {"traces": 24323, "logs": 8394, "metrics": 40712},
        "fixture_records": {"traces": 24323, "logs": 8394, "metrics": 40712},
        "bodies_sha_equal": True,
        "bodies_equal_in_replay_order": True,
        "segments": result["segments"],
        "report": {
            "frames": 232,
            "records": 73429,
            "dedupe_hits": 1,
            "rejects": {"logs 400": 1, "traces 400": 1, "traces 415": 1},
        },
    }
    assert result["segments"] > 3, "rotation at 0.25 MB must have produced several segments"
