"""Background volume end to end on Docker: deploy/k8s/background/ run as containers into a real `otb` gateway.

The manifests are the source of everything run here. The init container's build.sh builds both generators from
the pinned commit, out of a local otel-benchmark clone ($OTB_BG_SRC) because the commit may not be on GitHub yet.
The collector runs the ConfigMap's config in the pinned contrib image with OTB_GATEWAY pointed at a gateway on
this host, and the generators run in the pinned runtime image with the rate ConfigMaps' environment, on a Docker
network where the collector answers to its Service name. Containers get the manifests' CPU and memory limits.

Ways it can fail, each asserted below:
- build.sh does not build a generator from the pinned commit as uid 65532 on a read-only root;
- a generator runs the default profile instead of the generic one;
- the collector config does not load in contrib 0.161.0, or exports something the gateway rejects;
- a sealed frame has a resource without cloud.region=bg and service.instance.id=bg;
- the metric series the archive holds are not exactly the bounded set the loadgen announces (26 per pod, PODS=240);
- a log severity outside INFO/WARN/ERROR/DEBUG;
- the achieved rate is more than 15% off the rate ConfigMaps' target.

Needs Docker, OTB_DOCKER_TESTS=1 and OTB_BG_SRC; OTB_BG_SECONDS sets the measured window (default 120). Writes
tests/e2e/artifacts/background_local.json and, with MB/s and CPU per MB/s, background_local.timing.json.
"""

from __future__ import annotations

import http.client
import os
import re
import socket
import subprocess
import time
from collections import Counter
from pathlib import Path

import orjson
import pytest
from background import manifests, target_mb_per_s
from conftest import free_port, write_artifact
from gateway_proc import GatewayProc, health

from otb.archive import Archive

SRC = os.environ.get("OTB_BG_SRC", "")
SECONDS = float(os.environ.get("OTB_BG_SECONDS", "120"))
WARMUP_S = 20.0
GENERATORS = ("loggen", "loadgen")

pytestmark = pytest.mark.skipif(
    os.environ.get("OTB_DOCKER_TESTS") != "1" or not SRC,
    reason="needs Docker and an otel-benchmark clone; set OTB_DOCKER_TESTS=1 and OTB_BG_SRC",
)


def docker(*args: str, check: bool = True) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=900)
    if check and r.returncode:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {r.stderr[-2000:]}")
    return r.stdout + r.stderr


def limits(container: dict) -> list[str]:
    lim = container["resources"]["limits"]
    mem = lim["memory"].replace("Gi", "g").replace("Mi", "m")
    cpu = str(lim["cpu"])
    cpus = str(int(cpu[:-1]) / 1000) if cpu.endswith("m") else cpu  # docker takes cores, not millicores
    return ["--cpus", cpus, "--memory", mem]


class DockerAPI(http.client.HTTPConnection):
    def __init__(self) -> None:
        super().__init__("localhost")
        self.socket_path = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock").removeprefix("unix://")

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


def cpu_ns(name: str) -> int:
    api = DockerAPI()
    api.request("GET", f"/containers/{name}/stats?stream=false&one-shot=true")
    return orjson.loads(api.getresponse().read())["cpu_stats"]["cpu_usage"]["total_usage"]


def scan(archive: Path) -> dict:
    a = Archive.open(archive, verify="full")
    regions, instance_ids, severities, series = Counter(), Counter(), Counter(), set()
    records = Counter()
    for seg in a.segments():
        for entry, body in a.bodies(seg):
            d = orjson.loads(body)
            records[seg.signal] += entry.records
            for res in d.get("resourceLogs", []) + d.get("resourceMetrics", []):
                kvs = res.get("resource", {}).get("attributes", [])
                attrs = {kv["key"]: next(iter(kv["value"].values())) for kv in kvs}
                regions[attrs.get("cloud.region")] += 1
                instance_ids[attrs.get("service.instance.id")] += 1
                rkey = tuple(sorted((k, str(v)) for k, v in attrs.items()))
                for sl in res.get("scopeLogs", []):
                    severities.update(r.get("severityText") for r in sl.get("logRecords", []))
                for sm in res.get("scopeMetrics", []):
                    for m in sm.get("metrics", []):
                        data = next(v for k, v in m.items() if k in ("gauge", "sum", "histogram"))
                        for dp in data["dataPoints"]:
                            pkey = tuple(sorted((kv["key"], str(kv["value"])) for kv in dp.get("attributes", [])))
                            series.add((m["name"], rkey, pkey))
    return {
        "sealed": a.manifest.sealed,
        "regions": sorted(map(str, regions)),
        "instance_ids": sorted(map(str, instance_ids)),
        "severities": severities,
        "series": len(series),
        "records": records,
    }


def test_background_local(artifacts: Path, tmp_path: Path) -> None:
    m = manifests()
    commit = m["ConfigMap", "otb-bg-source"]["data"]["OTB_BG_COMMIT"]
    (tmp_path / "build").mkdir()
    (tmp_path / "build/build.sh").write_text(m["ConfigMap", "otb-bg-build"]["data"]["build.sh"])
    (tmp_path / "otelcol").mkdir()
    (tmp_path / "otelcol/config.yaml").write_text(m["ConfigMap", "otb-bg-collector"]["data"]["config.yaml"])
    pods = {n: m["Deployment", f"otb-bg-{n}"]["spec"]["template"]["spec"] for n in (*GENERATORS, "collector")}
    tag = f"otb-bg-e2e-{os.getpid()}"
    net, coll = tag, f"{tag}-collector"
    started: list[str] = []
    port = free_port()
    listen = ("--listen", f"0.0.0.0:{port}", "--instances", "bg")
    gw = GatewayProc(tmp_path / "corpus", port, tmp_path / "gateway.log", *listen)
    try:
        built = {}
        for g in GENERATORS:
            init = pods[g]["initContainers"][0]
            for d in (f"work-{g}", f"shared-{g}"):
                (tmp_path / d).mkdir()
            out = docker(
                "run", "--rm", "--user", "65532:65532", "--read-only", "--tmpfs", "/tmp", *limits(init),
                "-v", f"{tmp_path}/build:/build:ro", "-v", f"{tmp_path}/work-{g}:/work",
                "-v", f"{tmp_path}/shared-{g}:/shared", "-v", f"{SRC}:/src:ro",
                "-e", "OTB_BG_REPO=/src", "-e", f"OTB_BG_COMMIT={commit}", "-e", f"OTB_BG_GENERATOR=otel-{g}",
                init["image"], *init["command"],
            )  # fmt: skip
            built[g] = re.search(r"built (\S+) at (\w+): sha256 (\w+)", out).groups()

        docker("network", "create", net)
        gw.start()
        cc = pods["collector"]["containers"][0]
        env = [f"{e['name']}={e['value']}" for e in cc["env"] if e["name"] != "OTB_GATEWAY"]
        docker(
            "run", "-d", "--name", coll, "--network", net, "--network-alias", "otb-bg-collector", "--read-only",
            "--add-host", "host.docker.internal:host-gateway", *limits(cc),
            "-e", f"OTB_GATEWAY=http://host.docker.internal:{port}", *(x for e in env for x in ("-e", e)),
            "-v", f"{tmp_path}/otelcol:/etc/otelcol:ro", cc["image"], *cc["args"],
        )  # fmt: skip
        started.append(coll)
        deadline = time.time() + 60
        while "Everything is ready" not in docker("logs", coll):
            assert time.time() < deadline, docker("logs", coll)[-3000:]
            time.sleep(0.5)
        for g in GENERATORS:
            gc = pods[g]["containers"][0]
            data = m["ConfigMap", f"otb-bg-{g}"]["data"]
            docker(
                "run", "-d", "--name", f"{tag}-{g}", "--network", net, "--read-only", "--user", "65532:65532",
                *limits(gc), "-v", f"{tmp_path}/shared-{g}:/shared:ro",
                *(x for k, v in data.items() for x in ("-e", f"{k}={v}")), gc["image"], *gc["command"],
            )  # fmt: skip
            started.append(f"{tag}-{g}")

        time.sleep(WARMUP_S)
        names = [coll, *(f"{tag}-{g}" for g in GENERATORS)]
        t0, cpu0, h0 = time.monotonic(), {n: cpu_ns(n) for n in names}, health(gw.url)
        time.sleep(SECONDS / 2)
        fmt = "{{.Name}} {{.CPUPerc}}"
        midpoint = dict(line.split() for line in docker("stats", "--no-stream", "--format", fmt, *names).splitlines())
        time.sleep(SECONDS / 2)
        t1, cpu1, h1 = time.monotonic(), {n: cpu_ns(n) for n in names}, health(gw.url)
        logs = {g: docker("logs", f"{tag}-{g}") for g in GENERATORS}
        for g in GENERATORS:
            docker("stop", "-t", "10", f"{tag}-{g}")
        time.sleep(5)
        docker("stop", "-t", "30", coll)
        assert gw.term() == 0
        found = scan(gw.out)
    finally:
        if gw.proc and gw.proc.poll() is None:
            gw.kill()
        for n in started:
            docker("rm", "-f", n, check=False)
        docker("network", "rm", net, check=False)

    dt = t1 - t0
    wall = {s: (h1["signals"][s]["raw_bytes"] - h0["signals"][s]["raw_bytes"]) / dt / 1e6 for s in ("logs", "metrics")}
    total = sum(wall.values())
    cores = {n.removeprefix(f"{tag}-"): (cpu1[n] - cpu0[n]) / 1e9 / dt for n in names}
    gw_cores = (h1["cpu_user_s"] + h1["cpu_sys_s"] - h0["cpu_user_s"] - h0["cpu_sys_s"]) / dt
    target = target_mb_per_s(m)
    announced = int(re.search(r"series: (\d+)", logs["loadgen"]).group(1))
    rejects = h1["rejects"]
    bytes_per_record = {
        s: round(h1["signals"][s]["raw_bytes"] / h1["signals"][s]["records"], 1) for s in ("logs", "metrics")
    }
    sev_total = sum(found["severities"].values())
    timing = {
        "window_s": round(dt, 1),
        "mb_per_s": {s: round(v, 3) for s, v in wall.items()} | {"total": round(total, 3)},
        "target_mb_per_s": {s: round(v, 3) for s, v in target.items()},
        "cores": {k: round(v, 3) for k, v in cores.items()} | {"gateway": round(gw_cores, 3)},
        "cores_per_mb_per_s": {
            "loggen (per logs MB/s)": round(cores["loggen"] / wall["logs"], 3),
            "loadgen (per metrics MB/s)": round(cores["loadgen"] / wall["metrics"], 3),
            "collector": round(cores["collector"] / total, 3),
            "gateway": round(gw_cores / total, 3),
        },
        "docker_stats_cpu_at_midpoint": {k.removeprefix(f"{tag}-"): v for k, v in midpoint.items()},
        "json_bytes_per_record": bytes_per_record,
        "severity_share": {k: round(v / sev_total, 4) for k, v in sorted(found["severities"].items())},
        "records": dict(found["records"]),
        "binaries": {g: {"commit": c, "sha256": h} for g, (_, c, h) in built.items()},
    }
    result = {
        "built_from": {g: [name, c] for g, (name, c, _) in built.items()},
        "profiles": [line for g in GENERATORS for line in logs[g].splitlines() if "profile" in line.lower()][:2],
        "rejects": rejects,
        "sealed": found["sealed"],
        "regions": found["regions"],
        "instance_ids": found["instance_ids"],
        "severities": sorted(found["severities"]),
        "series_announced": announced,
        "series_in_archive": found["series"],
        "rate_within_15pct": {s: abs(wall[s] - target[s]) <= 0.15 * target[s] for s in target},
    }
    write_artifact(artifacts / "background_local.json", result, timing=timing)

    assert result == {
        "built_from": {"loggen": ["otel-loggen", commit], "loadgen": ["otel-loadgen", commit]},
        "profiles": result["profiles"],
        "rejects": {},
        "sealed": True,
        "regions": ["bg"],
        "instance_ids": ["bg"],
        "severities": ["DEBUG", "ERROR", "INFO", "WARN"],
        "series_announced": 6240,
        "series_in_archive": 6240,
        "rate_within_15pct": {"logs": True, "metrics": True},
    }
    assert all("generic" in line for line in result["profiles"]) and len(result["profiles"]) == 2
