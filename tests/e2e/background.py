"""deploy/k8s/background/ as the background tests read it: every object by (kind, name), and the OTLP/JSON MB/s the
rate ConfigMaps imply (deploy/k8s/background/rate.yaml states the two equations)."""

from __future__ import annotations

from pathlib import Path

import yaml

BG = Path(__file__).resolve().parents[2] / "deploy/k8s/background"


def manifests() -> dict[tuple[str, str], dict]:
    return {
        (d["kind"], d["metadata"]["name"]): d
        for p in sorted(BG.glob("*.yaml"))
        for d in yaml.safe_load_all(p.read_text())
        if d
    }


def target_mb_per_s(m: dict[tuple[str, str], dict]) -> dict[str, float]:
    def per_record(cm: dict) -> float:
        return float(cm["metadata"]["annotations"]["otb.bg/json-bytes-per-record"])

    lg, ld = m["ConfigMap", "otb-bg-loggen"], m["ConfigMap", "otb-bg-loadgen"]
    logs_per_s = int(lg["data"]["LOGS_PER_MIN"]) / 60
    d = ld["data"]
    points_per_s = int(d["WORKERS"]) * int(d["DATAPOINTS_PER_REQ"]) * 1000 / int(d["INTERVAL_MS"])
    return {"logs": logs_per_s * per_record(lg) / 1e6, "metrics": points_per_s * per_record(ld) / 1e6}
