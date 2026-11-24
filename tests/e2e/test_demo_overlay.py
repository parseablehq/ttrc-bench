"""The demo-side collector overlay (DESIGN amendment 5), checked against the demo's own collector config without
running a collector.

Ways the overlay can be wrong, each asserted below:
- a pipeline names a receiver, processor, exporter, connector or extension defined in neither file;
- a pipeline drops or reorders one of the demo's receivers or processors (the collector replaces arrays);
- the gateway exporter is not OTLP/HTTP JSON, has an in-memory queue, or gives up retrying;
- a signal's pipeline does not export to the gateway, or does not stamp the instance labels;
- the span_metrics connector loses its traces side or its metrics side;
- the compose override does not mount the queue volume or publish flagd-ui and OFREP.

Needs the demo checkout ($OTB_DEMO_DIR); skips without it. Writes
tests/e2e/artifacts/demo_overlay.json.
"""

import os
from pathlib import Path

import pytest
import yaml
from conftest import write_artifact

REPO = Path(__file__).resolve().parents[2]
OVERLAY = REPO / "capture/demo-overlay"
DEMO = Path(os.environ.get("OTB_DEMO_DIR", str(REPO / "corpus/dryrun/demo")))
BASE = DEMO / "src/otel-collector/otelcol-config.yml"
KINDS = ("receivers", "processors", "exporters", "connectors", "extensions")


def merge(a: object, b: object) -> object:
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: merge(a[k], b[k]) if k in a and k in b else b.get(k, a.get(k)) for k in a | b}
    return b


@pytest.mark.skipif(not BASE.exists(), reason="demo checkout not present")
def test_demo_overlay(artifacts: Path) -> None:
    base = yaml.safe_load(BASE.read_text())
    over = yaml.safe_load((OVERLAY / "otelcol-otb.yml").read_text())
    cfg = merge(base, over)
    defined = {k: set(cfg.get(k) or {}) for k in KINDS}
    pipes = cfg["service"]["pipelines"]

    undefined = []
    for name, p in pipes.items():
        for role, kinds in (
            ("receivers", ("receivers", "connectors")),
            ("processors", ("processors",)),
            ("exporters", ("exporters", "connectors")),
        ):
            for c in p.get(role, []):
                if not any(c in defined[k] for k in kinds):
                    undefined.append(f"{name}.{role}: {c}")
    undefined += [f"extension: {e}" for e in cfg["service"].get("extensions", []) if e not in defined["extensions"]]

    base_kept = {}
    for sig in ("traces", "metrics", "logs"):
        b, m = base["service"]["pipelines"][sig], pipes[sig]
        base_kept[sig] = {
            "receivers_equal": b["receivers"] == m["receivers"],
            "base_processors_are_prefix": m["processors"][: len(b["processors"])] == b["processors"],
            "added_processors": m["processors"][len(b["processors"]) :],
            "exports_to_gateway": "otlp_http/otb" in m["exporters"],
        }

    exp = cfg["exporters"]["otlp_http/otb"]
    labels = {a["key"]: a for a in cfg["processors"]["resource/otb_instance"]["attributes"]}
    compose = yaml.safe_load((OVERLAY / "compose.otb.yaml").read_text())
    svc = compose["services"]
    result = {
        "undefined_components": undefined,
        "pipelines": base_kept,
        "profiles_pipeline_unchanged": pipes["profiles"] == base["service"]["pipelines"]["profiles"],
        "span_metrics": {
            "traces_exports_to_connector": "span_metrics" in pipes["traces"]["exporters"],
            "metrics_receives_from_connector": "span_metrics" in pipes["metrics"]["receivers"],
        },
        "exporter": {
            "encoding": exp["encoding"],
            "compression": exp["compression"],
            "queue_storage": exp["sending_queue"].get("storage"),
            "retry_max_elapsed_time": exp["retry_on_failure"]["max_elapsed_time"],
            "endpoint": exp["endpoint"],
        },
        "instance_labels": sorted(k for k, a in labels.items() if a["value"] == "${env:OTB_INSTANCE}"),
        "compose": {
            "queue_volume": "otb-queue:/var/lib/otelcol/otb-queue" in svc["otel-collector"]["volumes"],
            "flagd_ui_port": svc["flagd-ui"]["ports"],
            "ofrep_port": svc["flagd"]["ports"],
        },
    }
    write_artifact(artifacts / "demo_overlay.json", result)

    added = ["resource/otb_instance", "batch/otb"]
    assert result == {
        "undefined_components": [],
        "pipelines": {
            s: {
                "receivers_equal": True,
                "base_processors_are_prefix": True,
                "added_processors": added,
                "exports_to_gateway": True,
            }
            for s in ("traces", "metrics", "logs")
        },
        "profiles_pipeline_unchanged": True,
        "span_metrics": {"traces_exports_to_connector": True, "metrics_receives_from_connector": True},
        "exporter": {
            "encoding": "json",
            "compression": "gzip",
            "queue_storage": "file_storage/otb",
            "retry_max_elapsed_time": "0s",
            "endpoint": "${env:OTB_GATEWAY}",
        },
        "instance_labels": ["cloud.region", "service.instance.id"],
        "compose": {
            "queue_volume": True,
            "flagd_ui_port": ["${OTB_FLAGD_UI_PORT:-14000}:${FLAGD_UI_PORT}"],
            "ofrep_port": ["${OTB_OFREP_PORT:-18016}:${FLAGD_OFREP_PORT}"],
        },
    }
