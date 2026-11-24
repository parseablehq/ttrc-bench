"""Check 5 of U4: each stack's ingest collector is synchronous. With the backend stopped, a POST to the ingest
endpoint must answer non-2xx (the exporter's failure reaches the client, nothing is queued); with the backend
started again, the same POST must answer 2xx. Writes docs/v1/units/U4-sync.json.

  uv run python tests/live/sync_collector.py [--stacks b1,b2,b3,b4]

Run after check 3: the failed sends raise the collector's send-failed counters, and the artifact records the
IngestCounters probe afterwards, so it also shows that check catching them.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import sample as S

from otb.stacks import (
    REPO,
    IngestCounters,
    StackContext,
    probe_json,
    registry,
    run_probe,
    select,
)
from otb.stacks.lifecycle import compose_argv, wait_ready
from otb.timebase import WallWindow

# The backend container each signal's exporter writes to.
BACKEND = {
    "b1-lgtm": {"metrics": "prometheus", "logs": "loki", "traces": "tempo"},
    "b3-clickhouse": {"logs": "clickhouse"},
    "b4-parseable": {"logs": "parseable"},
}


def timed_post(url: str, signal: str, body: bytes) -> dict[str, object]:
    t0 = time.monotonic()
    status, text = S.post(url, signal, body, timeout_s=180)
    return {"status": status, "secs": round(time.monotonic() - t0, 2), "body": text[:200]}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--stacks", default=None)
    p.add_argument("--profile", default="laptop")
    p.add_argument("--out", default=str(REPO / "docs" / "v1" / "units" / "U4-sync.json"))
    a = p.parse_args()
    sample = S.build(1)  # one request per signal, newest point a minute ago
    out: dict[str, object] = {"unit": "U4 check 5", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    ok = True
    for stack in select(registry(), a.stacks):
        compose = compose_argv([stack], a.profile)
        rows = []
        for signal, service in BACKEND[stack.id].items():
            body = sample.bodies[signal][0]
            subprocess.run([*compose, "stop", service], check=True, capture_output=True)
            down = timed_post(stack.ingest.url, signal, body)
            subprocess.run([*compose, "start", service], check=True, capture_output=True)
            ready_s = wait_ready(stack, StackContext(stack))
            up = timed_post(stack.ingest.url, signal, body)
            passed = not 200 <= int(down["status"]) < 300 and 200 <= int(up["status"]) < 300  # type: ignore[call-overload]
            ok &= passed
            rows.append(
                {
                    "signal": signal,
                    "stopped": service,
                    "backend_down": down,
                    "ready_after_restart_s": round(ready_s, 1),
                    "backend_up": up,
                    "pass": passed,
                }
            )
            print(
                stack.id,
                signal,
                service,
                "down:",
                down["status"],
                f"{down['secs']}s",
                "up:",
                up["status"],
                "PASS" if passed else "FAIL",
            )
        counters = run_probe(stack, IngestCounters(), WallWindow(0, 1), StackContext(stack))  # type: ignore[arg-type]
        out[stack.id] = {"cases": rows, "ingest_counters_after": probe_json(counters)}
        print(stack.id, "IngestCounters after the failed sends:", probe_json(counters))
    out["pass"] = ok
    Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
