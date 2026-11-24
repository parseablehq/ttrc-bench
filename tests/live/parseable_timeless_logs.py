"""Evidence for the Parseable defect "a log record without timeUnixNano is stamped with its arrival time".

  uv run python tests/live/parseable_timeless_logs.py state/sessions/<id> [--out FILE]

Read-only. Per service, from the archive: log records sent, and those without timeUnixNano (all of which carry
observedTimeUnixNano). From the loaded b4-parseable stack, over the whole session (milliseconds, as Parseable
stores them): records stored, those whose time_unix_nano is within 5 ms before p_timestamp (the arrival time),
those whose time_unix_nano equals observed_time_unix_nano, and the range of time_unix_nano minus observed. Plus
one stored record of the first affected service, as returned.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import orjson

from otb.archive import Archive
from otb.ingest import Session, corpus_path, selected_frames
from otb.stacks import StackContext, registry

MS = "CAST({} AS BIGINT)"  # Parseable's timestamps cast to epoch milliseconds
ARRIVAL_LAG = f"{MS.format('p_timestamp')} - {MS.format('time_unix_nano')}"
AFTER_OBSERVED = f"{MS.format('time_unix_nano')} - {MS.format('observed_time_unix_nano')}"
SQL = f"""
SELECT "service.name" AS service, count(*) AS stored,
       sum(CASE WHEN time_unix_nano IS NULL THEN 1 ELSE 0 END) AS time_null,
       sum(CASE WHEN {ARRIVAL_LAG} BETWEEN 0 AND 5 THEN 1 ELSE 0 END) AS time_within_5ms_before_arrival,
       sum(CASE WHEN time_unix_nano = observed_time_unix_nano THEN 1 ELSE 0 END) AS time_equals_observed,
       min({AFTER_OBSERVED}) AS min_ms_after_observed, max({AFTER_OBSERVED}) AS max_ms_after_observed
FROM otel_logs WHERE $WHERE GROUP BY "service.name" ORDER BY "service.name"
"""
STORED = (
    "stored",
    "time_null",
    "time_within_5ms_before_arrival",
    "time_equals_observed",
    "min_ms_after_observed",
    "max_ms_after_observed",
)


def archive_counts(session: Session) -> dict[str, dict[str, int]]:
    a = Archive(corpus_path(session))
    sent: Counter[str] = Counter()
    timeless: Counter[str] = Counter()
    observed: Counter[str] = Counter()
    for ref, e in selected_frames(a, session.window):
        if ref.signal != "logs":
            continue
        for rl in orjson.loads(a.read(ref, e)).get("resourceLogs") or ():
            attrs = (rl.get("resource") or {}).get("attributes") or ()
            svc = next((x["value"].get("stringValue") for x in attrs if x["key"] == "service.name"), "?")
            for sl in rl.get("scopeLogs") or ():
                for r in sl.get("logRecords") or ():
                    sent[svc] += 1
                    if not int(r.get("timeUnixNano") or 0):
                        timeless[svc] += 1
                        observed[svc] += bool(int(r.get("observedTimeUnixNano") or 0))
    return {s: {"sent": sent[s], "no_time": timeless[s], "no_time_with_observed": observed[s]} for s in sorted(sent)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("session", type=Path)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    session = Session.load(a.session / "session.json")
    sent = archive_counts(session)
    stack = registry()["b4-parseable"]
    ctx = StackContext(stack)
    archive = Archive(corpus_path(session))
    scope = session.window or archive.manifest.window
    assert scope is not None
    w = session.shift.wall_window(scope)
    stored = {r["service"]: r for r in stack.probes._rows(ctx, SQL, "logs", w)}  # type: ignore[attr-defined]
    first = next(s for s, c in sent.items() if c["no_time"])
    sample = stack.probes._rows(  # type: ignore[attr-defined]
        ctx,
        'SELECT time_unix_nano, observed_time_unix_nano, p_timestamp, "service.name" FROM otel_logs '
        f"WHERE $WHERE AND \"service.name\" = '{first}' ORDER BY observed_time_unix_nano LIMIT 1",
        "logs",
        w,
    )
    out = {
        "session": session.id,
        "per_service": {s: c | {k: stored.get(s, {}).get(k) for k in STORED} for s, c in sent.items()},
        "sample": sample,
    }
    text = json.dumps(out, indent=1, default=str) + "\n"
    if a.out:
        a.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
