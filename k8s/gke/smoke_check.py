import collections
from pathlib import Path

import orjson
import yaml

from otb.archive import Archive

root = Path("/work/corpora/smoke-deploy")
sched = {e["slot_id"]: e for e in yaml.safe_load((root / "schedule.yaml").read_text())["entries"]}
win = {k: (v["window"]["start"], v["window"]["end"]) for k, v in sched.items()}
a = Archive.open(root)
attr = lambda kvs: {x["key"]: next(iter(x["value"].values()), None) for x in kvs or []}
ver = collections.Counter()
slow = collections.Counter()
cur = collections.Counter()
pcat = collections.Counter()
for seg in a.segments("traces"):
    for entry, body in a.bodies(seg):
        d = orjson.loads(body)
        for rs in d.get("resourceSpans", []):
            ra = attr(rs.get("resource", {}).get("attributes"))
            if ra.get("cloud.region") not in (None, "eu1"):
                continue
            svc, v = ra.get("service.name"), ra.get("service.version")
            for ss in rs.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    t = int(sp["startTimeUnixNano"])
                    dur = (int(sp["endTimeUnixNano"]) - t) / 1e6
                    err = (sp.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR")
                    for slot, (s, e) in win.items():
                        inside = s <= t < e
                        phase = "in" if inside else ("before" if t < s else "after")
                        if svc == "shipping" and slot == "sdship":
                            ver[(phase, v)] += 1
                            if dur >= 4000:
                                slow[(phase, v)] += 1
                        if svc == "currency" and slot == "sbd":
                            cur[(phase, v)] += 1
                        if svc == "product-catalog" and slot == "spcat":
                            pcat[(phase, "error" if err else "ok")] += 1
print("windows", {k: round((e - s) / 1e9) for k, (s, e) in win.items()})
print("shipping spans by (phase, version):", dict(ver))
print("shipping spans >= 4 s by (phase, version):", dict(slow))
print("currency spans by (phase, version):", dict(cur))
print("product-catalog spans by (phase, status):", dict(pcat))
