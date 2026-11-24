"""Check (c) of U5: a stack collector answering 4xx for one frame gives a `rejected` ledger outcome with the body,
and the feeder sends the frames after it.

  uv run python tests/live/ingest_reject.py build OUT   # a sealed 3-frame archive: valid, malformed, valid
  uv run otb ingest OUT --mode replay --allow-nonempty   # then: uv run otb ingest status

The malformed frame is valid JSON whose span carries a traceId that is not hex, which every OTLP/HTTP JSON
receiver refuses while decoding. The archive bypasses the normaliser for that frame only, because the normaliser
(rightly) parses bodies it is given.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import orjson

from otb.archive import ArchiveWriter, Header, NormaliserStamp, SourcePins
from otb.normalise import Normalised, normalise

NS = 1_000_000_000


def log_body(t_ns: int, text: str) -> bytes:
    rec = {"timeUnixNano": str(t_ns), "severityNumber": 9, "severityText": "INFO", "body": {"stringValue": text}}
    res = {"attributes": [{"key": "service.name", "value": {"stringValue": "otb-reject-probe"}}]}
    return orjson.dumps({"resourceLogs": [{"resource": res, "scopeLogs": [{"logRecords": [rec]}]}]})


def bad_span(t_ns: int) -> bytes:
    span = {
        "traceId": "zz-not-hex",
        "spanId": "0102030405060708",
        "name": "malformed",
        "kind": 2,
        "startTimeUnixNano": str(t_ns),
        "endTimeUnixNano": str(t_ns + 1000),
    }
    res = {"attributes": [{"key": "service.name", "value": {"stringValue": "otb-reject-probe"}}]}
    return orjson.dumps({"resourceSpans": [{"resource": res, "scopeSpans": [{"spans": [span]}]}]})


def build(out: Path) -> None:
    t0 = (time.time_ns() // (60 * NS) - 10) * 60 * NS
    pins = SourcePins("none", "none", "none", {}, "none")
    header = Header("reject-probe", ("default",), pins, NormaliserStamp.current(), rotate_bytes=1 << 20)
    w = ArchiveWriter(out, header, rotate_s=None, dedupe=False)
    w.append("logs", normalise("logs", log_body(t0, "before the malformed frame")), received_ns=0)
    w.append("traces", Normalised(bad_span(t0 + 5 * NS), 1, t0 + 5 * NS, t0 + 5 * NS + 1000), received_ns=0)
    w.append("logs", normalise("logs", log_body(t0 + 10 * NS, "after the malformed frame")), received_ns=0)
    m = w.seal(scenario_sha256=None, schedule_sha256=None)
    w.close()
    print(m.corpus_id)


if __name__ == "__main__":
    build(Path(sys.argv[2]))
