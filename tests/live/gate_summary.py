"""Print a gate.json per stack: counts, signal verdicts, failing families and every failing check.

uv run python tests/live/gate_summary.py state/sessions/<id>/gate/gate.json [--all]
"""

import json
import sys
from collections import Counter
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text())
for sid, v in d["stacks"].items():
    fails = {k: c for k, c in v["checks"].items() if c["status"] == "fail"}
    unver = Counter(c["family"] for c in v["checks"].values() if c["status"] == "unverifiable")
    print(sid, v["counts"], v["signals"])
    print("   failing families", dict(Counter(c["family"] for c in fails.values())), "unverifiable", dict(unver))
    for k, c in list(fails.items())[: None if "--all" in sys.argv else 12]:
        exp, got = c["expected"], c["got"]
        if isinstance(exp, list) and isinstance(got, list):
            exp, got = f"{len(exp)} names, missing {sorted(set(exp) - set(got))[:4]}", f"{len(got)} names"
        print(f"     {k[:150]}  expected={str(exp)[:90]} got={str(got)[:60]} {c['reason'] or ''}"[:330])
