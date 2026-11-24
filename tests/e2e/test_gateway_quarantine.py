"""A demo collector's file-backed queue outlives its container, so a new capture can receive batches left over
from an earlier one (on 2026-10-12, 28 frames whose events were ~177,600 s old). The gateway quarantines a frame
whose newest event precedes the archive's creation by more than --stale-s: it acks it, so the collector drops it,
records it in quarantine.jsonl and the manifest's `quarantined` counts, and never archives it.

Ways it could fail, each asserted below:
- a leftover frame is archived, or is answered with an error so the collector resends it forever;
- a frame of this capture is quarantined: one a few seconds old, one between the archive's creation and a
  gateway restart, or one with no event time;
- the bound is measured from the gateway's own start, so a restart quarantines frames of the same capture;
- the quarantined frames are missing from the sealed manifest, or the sealed archive does not verify.

Evidence: tests/e2e/artifacts/gateway_quarantine.json.
"""

import time
from pathlib import Path

import orjson
from conftest import free_port, ok, otb, write_artifact
from gateway_proc import GatewayProc
from gateway_proc import post as http_post
from synthetic import kv, log, logs, span, traces

from otb.archive import Archive

NS = 1_000_000_000


def label(signal: str, raw: bytes) -> str:
    d = orjson.loads(raw)
    if signal == "traces":
        return d["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"]
    return d["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"]["stringValue"]


def test_a_leftover_queue_is_quarantined_not_archived(tmp_path: Path, artifacts: Path) -> None:
    out = tmp_path / "corpus"
    gw = GatewayProc(out, free_port(), tmp_path / "gateway.log", "--stale-s", "2")
    gw.start()
    created = time.time_ns()
    old = created - 177_600 * NS

    def post(name: str, doc: tuple[str, dict]) -> tuple[str, int]:
        return name, http_post(gw.url, doc[0], orjson.dumps(doc[1]))[0]

    codes = dict(
        [
            post("leftover traces", traces("cart", span("a" * 32, "s1", None, "GET", 2, old, 10))),
            post("leftover logs", logs("cart", log(old + NS, "left in the queue"))),
            post("this capture", traces("cart", span("b" * 32, "s2", None, "GET", 2, created - NS, 10))),
            post("no event time", logs("cart", {"body": {"stringValue": "untimed"}, "attributes": [kv("k", 1)]})),
        ]
    )
    time.sleep(3.5)
    gw.kill()
    gw.start()
    codes |= dict(
        [
            post("before the restart", traces("cart", span("c" * 32, "s3", None, "GET", 2, created + NS // 2, 10))),
            post("leftover after restart", traces("cart", span("d" * 32, "s4", None, "GET", 2, created - 5 * NS, 10))),
        ]
    )
    exit_code = gw.term()

    verify = ok(otb("corpus", "verify", out, "--level", "full"))
    a = Archive.open(out, verify="full")
    m = a.manifest
    stored = sorted(label(ref.signal, a.read(ref, e)) for ref, e in a.merged())
    quarantine = [orjson.loads(line) for line in (out / "quarantine.jsonl").read_bytes().splitlines()]
    result = {
        "codes": codes,
        "exit_code": exit_code,
        "sealed": m.sealed,
        "stored": stored,
        "quarantined": m.quarantined,
        "quarantine_lines": [[q["signal"], q["records"]] for q in quarantine],
        "reopened_manifest_keeps_counts": Archive(out).manifest.quarantined == m.quarantined,
        "verify": verify.stdout.strip().splitlines()[-1].split(": ", 1)[1],
    }
    write_artifact(artifacts / "gateway_quarantine.json", result)
    assert result == {
        "codes": {
            "leftover traces": 200,
            "leftover logs": 200,
            "this capture": 200,
            "no event time": 200,
            "before the restart": 200,
            "leftover after restart": 200,
        },
        "exit_code": 0,
        "sealed": True,
        "stored": ["s2", "s3", "untimed"],
        "quarantined": {"logs": {"frames": 1, "records": 1}, "traces": {"frames": 2, "records": 2}},
        "quarantine_lines": [["traces", 1], ["logs", 1], ["traces", 1]],
        "reopened_manifest_keeps_counts": True,
        "verify": "full verify ok (0 problems)",
    }
