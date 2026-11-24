"""A `kubectl` stand-in for the Kubernetes lifecycle tests: appends each call to $FAKE_LOG as a JSON line, with the
manifest read from stdin when the call applies `-f -`. `get <kind> <name> -o json` answers from $FAKE_STATE, a
JSON file: `deployments` and `jobs` map "<namespace>/<name>" to the object's spec/status; a name not there is not
found (exit 1). A Job's status is a condition type ("Complete" or "Failed"). Everything else only logs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def state() -> dict:
    path = os.environ.get("FAKE_STATE")
    return json.loads(Path(path).read_text()) if path and Path(path).exists() else {}


def main(argv: list[str]) -> int:
    record: list[object] = ["kubectl", *argv]
    if "-f" in argv and argv[argv.index("-f") + 1] == "-":
        record.append({"stdin": sys.stdin.read()})
    with Path(os.environ["FAKE_LOG"]).open("a") as log:
        log.write(json.dumps(record) + "\n")
    args = [a for a in argv if not a.startswith("--context")]
    if args[:1] == ["get"] and "-o" in args:
        kind, name = args[1], args[2]
        ns = args[args.index("--namespace") + 1]
        table = state().get({"deployment": "deployments", "job": "jobs"}.get(kind, kind), {})
        if f"{ns}/{name}" not in table:
            print(f'Error from server (NotFound): {kind} "{name}" not found', file=sys.stderr)
            return 1
        entry = table[f"{ns}/{name}"]
        if kind == "job":
            doc = {"status": {"conditions": [{"type": entry, "status": "True"}]}}
        else:
            doc = {"metadata": {"name": name}, "spec": entry}
        print(json.dumps(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
