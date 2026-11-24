"""A `helm` stand-in for the Kubernetes lifecycle tests. Every call is appended to $FAKE_LOG as a JSON line.

pull:     writes <destination>/<chart>-<version>.tgz holding `chart <chart> <version>\\n` (plus $FAKE_CHART_SUFFIX,
          which a test sets to make the archive's sha256 differ from the lock).
template: prints <dir>/<release>.yaml from the first directory of $FAKE_RENDER (colon-separated) that has it, with
          `resources: __PROFILE__` replaced by the `resources` block of the last `-f` file (the profile overlay).
history:  prints the release's revisions from $FAKE_STATE (JSON; key `history`, release name -> list of
          {revision, status}); exit 1 when the release has none, as helm does.
plugin:   `install <dir>` records the directory's name in $FAKE_STATE.plugins; `list` prints the recorded names.
Anything else only logs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml


def main(argv: list[str]) -> int:
    with Path(os.environ["FAKE_LOG"]).open("a") as log:
        log.write(json.dumps(["helm", *argv]) + "\n")
    if not argv:
        return 0
    if argv[0] == "pull":
        chart, version = argv[1], argv[argv.index("--version") + 1]
        dest = Path(argv[argv.index("--destination") + 1])
        name = chart.rsplit("/", 1)[-1]
        (dest / f"{name}-{version}.tgz").write_bytes(
            f"chart {name} {version}\n{os.environ.get('FAKE_CHART_SUFFIX', '')}".encode()
        )
    elif argv[0] == "template":
        release = argv[1]
        text = next(
            (d / f"{release}.yaml").read_text()
            for d in (Path(p) for p in os.environ["FAKE_RENDER"].split(":"))
            if (d / f"{release}.yaml").exists()
        )
        values = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
        overlay = yaml.safe_load(Path(values[-1]).read_text()) if values else {}
        if "resources" in (overlay or {}):
            text = text.replace("resources: __PROFILE__", "resources: " + json.dumps(overlay["resources"]))
        sys.stdout.write(text)
    elif argv[0] == "plugin":
        path = Path(os.environ["FAKE_STATE"] + ".plugins")
        names = path.read_text().split() if path.exists() else []
        if argv[1] == "install":
            path.write_text("\n".join([*names, Path(argv[2]).name]) + "\n")
        elif argv[1] == "list":
            print("NAME\tVERSION\tTYPE")
            for n in names:
                print(f"{n}\t0.1.0\tpostrenderer/v1")
    elif argv[0] == "history":
        path = os.environ.get("FAKE_STATE")
        history = (json.loads(Path(path).read_text()) if path and Path(path).exists() else {}).get("history", {})
        if argv[1] not in history:
            print("Error: release: not found", file=sys.stderr)
            return 1
        print(json.dumps(history[argv[1]]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
