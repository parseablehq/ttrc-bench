"""Cut the corpus-mini raw slice from the v2 dry-run recording.

  python tests/fixtures/make_corpus_mini.py <v2 dryrun dir> <out dir>
  otb corpus import-v2 <out dir>/corpus.yaml --out tests/fixtures/corpus-mini --rotate-mb 1

A line (one export request) is in the slice when its latest event time falls in [08:13:00, 08:16:00) UTC, the
replay rule v2 used for due times. The slice covers the paymentFailure onset at 08:14:12. The slice's corpus.yaml
carries its own hashes and the v2 events that overlap it.
"""

import hashlib
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

START = datetime(2026, 9, 24, 8, 13, tzinfo=UTC)
MINUTES = 3
NANOS = re.compile(rb'UnixNano":"(\d+)"')


def main() -> int:
    v2, out = Path(sys.argv[1]), Path(sys.argv[2])
    lo = int(START.timestamp()) * 10**9
    hi = lo + MINUTES * 60 * 10**9
    (out / "recording").mkdir(parents=True, exist_ok=True)
    sha = {}
    for s in ("traces", "logs", "metrics"):
        dst = out / "recording" / f"{s}.jsonl"
        with (v2 / "recording" / f"{s}.jsonl").open("rb") as src, dst.open("wb") as f:
            for line in src:
                due = max((int(x) for x in NANOS.findall(line)), default=0)
                if lo <= due < hi:
                    f.write(line)
        sha[s] = hashlib.sha256(dst.read_bytes()).hexdigest()
    cfg = yaml.safe_load((v2 / "corpus.yaml").read_text())
    cfg["sha256"] = sha
    cfg["window_utc"] = [
        START.strftime("%Y-%m-%dT%H:%M:%S"),
        datetime.fromtimestamp(hi // 10**9, UTC).strftime("%Y-%m-%dT%H:%M:%S"),
    ]
    cfg["events"] = [e for e in cfg["events"] if e.get("kind") == "fault"]
    cfg["slice_of"] = {"corpus": "corpus/dryrun/corpus.yaml", "rule": "line max event time in window_utc"}
    (out / "corpus.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False, width=120))
    return 0


if __name__ == "__main__":
    sys.exit(main())
