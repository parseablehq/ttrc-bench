"""Injector against a real flagd and flagd-ui (OTel Demo 3.1.0 images, pinned by digest).

Starts only those two services from the demo's compose.yaml under project otbtest-flagd, with the pinned flag
file copied into a temporary directory, runs three one-fault plans (one through flagd-ui's HTTP API, one by
editing the mounted file, and productCatalogFailure, whose targeting rule decides what flagd serves, through
flagd-ui while the product its rule targets is evaluated), and tears everything down. Needs Docker and the demo
checkout: set OTB_DOCKER_TESTS=1.
Evidence: tests/e2e/artifacts/injector_flagd.json.
"""

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import orjson
import pytest
from conftest import write_artifact

from otb.inject import FlagdFile, FlagdUi, Injector, ofrep_variant, read_events
from otb.scenario import Catalogue, load_scenario, plan

REPO = Path(__file__).resolve().parents[2]
DEMO = Path(os.environ.get("OTB_DEMO_DIR", str(REPO / "corpus/dryrun/demo")))
OVERRIDE = REPO / "tests/e2e/flagd/compose.override.yaml"
FLAGD_JSON = REPO / "tests/fixtures/flagd/demo.flagd.json"
PROJECT = "otbtest-flagd"

pytestmark = pytest.mark.skipif(
    os.environ.get("OTB_DOCKER_TESTS") != "1" or not (DEMO / "compose.yaml").exists(),
    reason="needs Docker and the OTel Demo checkout; set OTB_DOCKER_TESTS=1",
)

SCENARIO = """
id: injector-e2e-{name}
seed: 1
duration_s: 12
instances: [local]
min_gap_s: 0
slots:
  - id: f1
    kind: fault
    flag_in: [{flag}]
    variant_in: ["{variant}"]
    start_s: {{ lo: 1, hi: 1 }}
    duration_s: {{ lo: 4, hi: 4 }}
    instance: local
"""


def compose(env: dict, *args: str) -> subprocess.CompletedProcess:
    argv = ["docker", "compose", "-p", PROJECT, "--project-directory", str(DEMO), "-f", str(DEMO / "compose.yaml")]
    return subprocess.run([*argv, "-f", str(OVERRIDE), *args], env=env, capture_output=True, text=True)


def published(env: dict, service: str, port: int) -> str:
    r = compose(env, "port", service, str(port))
    assert r.returncode == 0, r.stderr
    return "http://" + r.stdout.strip().replace("0.0.0.0", "127.0.0.1")


def run_plan(tmp: Path, name: str, flag: str, variant: str, driver) -> tuple[list[dict], dict]:
    path = tmp / f"{name}.yaml"
    path.write_text(SCENARIO.format(name=name, flag=flag, variant=variant))
    cat = Catalogue.load()
    p = plan(load_scenario(path), cat, seed=1)
    events = tmp / f"{name}.events.jsonl"
    inj = Injector(p, cat, {"local": driver}, events, time.time_ns())
    assert inj.preflight() == []
    schedule = inj.run()
    (entry,) = schedule.entries
    return read_events(events), {
        "flag": entry.flag,
        "variant": entry.variant,
        "status": entry.status,
        "window_s": (entry.window.end - entry.window.start) / 1e9,
    }


def watch_served(ofrep: str, seen: list[str | None], done: threading.Event) -> None:
    """What flagd serves for productCatalogFailure to a request for the product its rule targets, each change once."""
    body = json.dumps({"context": {"product_id": "OLJCESPC7Z"}}).encode()
    while not done.is_set():
        req = urllib.request.Request(
            f"{ofrep}/ofrep/v1/evaluate/flags/productCatalogFailure",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            variant = json.loads(r.read()).get("variant")
        if not seen or seen[-1] != variant:
            seen.append(variant)
        time.sleep(0.2)


def test_injector_flips_a_flag_on_and_off(tmp_path: Path, artifacts: Path) -> None:
    flagd_dir = tmp_path / "flagd"
    flagd_dir.mkdir()
    shutil.copy(FLAGD_JSON, flagd_dir / "demo.flagd.json")
    env = os.environ | {"OTB_FLAGD_DIR": str(flagd_dir)}
    try:
        up = compose(env, "up", "-d", "--no-deps", "--no-build", "--wait", "flagd", "flagd-ui")
        assert up.returncode == 0, up.stderr
        ui, ofrep = published(env, "flagd-ui", 4000), published(env, "flagd", 8016)
        deadline = time.monotonic() + 60
        while ofrep_variant(ofrep, "paymentFailure") != "off":
            assert time.monotonic() < deadline, "flagd did not serve the flag file"
            time.sleep(0.5)

        ui_events, ui_entry = run_plan(tmp_path, "ui", "paymentFailure", "50%", FlagdUi(ui, ofrep))
        file_events, file_entry = run_plan(
            tmp_path, "file", "cartFailure", "25%", FlagdFile(flagd_dir / "demo.flagd.json", ofrep)
        )
        served_to_product: list[str | None] = []
        watching = threading.Event()
        watch = threading.Thread(target=watch_served, args=(ofrep, served_to_product, watching))
        watch.start()
        try:
            targeted_events, targeted_entry = run_plan(
                tmp_path, "targeted", "productCatalogFailure", "on", FlagdUi(ui, ofrep)
            )
            time.sleep(1)
        finally:
            watching.set()
            watch.join()
        after = orjson.loads((flagd_dir / "demo.flagd.json").read_bytes())["flags"]
        served_after = {f: ofrep_variant(ofrep, f) for f in ("paymentFailure", "cartFailure")}
    finally:
        down = compose(env, "down", "-v", "--remove-orphans")
    leftover = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}", "-q"],
        capture_output=True,
        text=True,
    ).stdout.strip()

    def actions(evs: list[dict]) -> list[list]:
        return [[e["action"], e["phase"], e["ok"], e["detail"].get("readback"), e["detail"].get("served")] for e in evs]

    timing = {
        "ui": {"window_s": ui_entry.pop("window_s"), "events": ui_events},
        "file": {"window_s": file_entry.pop("window_s"), "events": file_events},
        "targeted": {"window_s": targeted_entry.pop("window_s"), "events": targeted_events},
    }
    result = {
        "ui": {"entry": ui_entry, "actions": actions(ui_events)},
        "file": {"entry": file_entry, "actions": actions(file_events)},
        "targeted": {
            "entry": targeted_entry,
            "actions": actions(targeted_events),
            "served_to_the_targeted_product": served_to_product,
            "rule_after": after["productCatalogFailure"]["targeting"]["if"][1:],
        },
        "default_variants_after": {f: after[f]["defaultVariant"] for f in ("paymentFailure", "cartFailure")},
        "served_after": served_after,
        "torn_down": down.returncode == 0 and leftover == "",
    }
    write_artifact(artifacts / "injector_flagd.json", result, timing=timing)

    assert result["ui"]["actions"] == [
        ["flag.write", "on", True, None, None],
        ["flag.confirm", "on", True, "50%", "50%"],
        ["flag.write", "off", True, None, None],
        ["flag.confirm", "off", True, "off", "off"],
    ]
    assert result["file"]["actions"] == [
        ["flag.write", "on", True, None, None],
        ["flag.confirm", "on", True, "25%", "25%"],
        ["flag.write", "off", True, None, None],
        ["flag.confirm", "off", True, "off", "off"],
    ]
    assert result["targeted"]["actions"] == [
        ["flag.write", "on", True, None, None],
        ["flag.confirm", "on", True, "on", "None"],
        ["flag.write", "off", True, None, None],
        ["flag.confirm", "off", True, "off", "None"],
    ]
    assert result["targeted"]["served_to_the_targeted_product"] == ["off", "on", "off"]
    assert result["targeted"]["rule_after"] == ["off", "off"]
    for side in ("ui", "file", "targeted"):
        assert result[side]["entry"]["status"] == "realised"
        assert 3.0 < timing[side]["window_s"] < 8.0, timing[side]
    assert result["default_variants_after"] == {"paymentFailure": "off", "cartFailure": "off"}
    assert result["served_after"] == {"paymentFailure": "off", "cartFailure": "off"}
    assert result["torn_down"]
