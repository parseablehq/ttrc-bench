"""The injector turns a flag with a targeting rule on the way flagd-ui does, offline, against the pinned flag file.

productCatalogFailure's rule is `if: [product_id == OLJCESPC7Z, "off", "off"]`, so its defaultVariant is never
served; flagd-ui's write sets the rule's then-branch instead (src/flagd-ui/lib/flagd_ui/storage.ex,
update_flag). test_injector_flagd.py checks what real flagd then serves (needs Docker).

Ways this could fail, listed before the code:
1. the injector writes defaultVariant on a flag whose three-branch rule never serves it, so the fault never fires
   (gke-24h: productCatalogFailure);
2. the on is confirmed from a readback of defaultVariant, so a flag that still serves off is recorded realised;
3. the off leaves the then-branch on, so the fault outlives its window;
4. preflight, settle or the final reset read defaultVariant, so a flag left on in its rule passes as off;
5. a flag without targeting, or any other part of the file, changes.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path

import orjson

from otb.inject import FlagdFile, Injector, read_events
from otb.scenario import Catalogue, load_scenario, plan

FLAGD_JSON = Path(__file__).resolve().parents[2] / "tests/fixtures/flagd/demo.flagd.json"
SCENARIO = """
id: injector-targeting
seed: 1
duration_s: 8
instances: [local]
min_gap_s: 0
slots:
  - id: p1
    kind: fault
    flag_in: [productCatalogFailure]
    variant_in: ["on"]
    start_s: { lo: 1, hi: 1 }
    duration_s: { lo: 2, hi: 2 }
    instance: local
  - id: c1
    kind: fault
    flag_in: [paymentFailure]
    variant_in: ["50%"]
    start_s: { lo: 4, hi: 4 }
    duration_s: { lo: 2, hi: 2 }
    instance: local
"""


class Recorded(FlagdFile):
    """The flag file, with every config written to it kept in order."""

    writes: list[dict]

    def write(self, config: dict) -> tuple[bool, dict[str, str]]:
        self.writes.append(copy.deepcopy(config))
        return super().write(config)


def flag_file(tmp_path: Path, edit=None) -> Recorded:
    cfg = orjson.loads(FLAGD_JSON.read_bytes())
    if edit:
        edit(cfg)
    (tmp_path / "demo.flagd.json").write_bytes(orjson.dumps(cfg))
    driver = Recorded(tmp_path / "demo.flagd.json")
    object.__setattr__(driver, "writes", [])
    return driver


def injector(tmp_path: Path, driver: Recorded) -> Injector:
    (tmp_path / "s.yaml").write_text(SCENARIO)
    cat = Catalogue.load()
    p = plan(load_scenario(tmp_path / "s.yaml"), cat, seed=1)
    return Injector(p, cat, {"local": driver}, tmp_path / "events.jsonl", time.time_ns())


def test_a_flag_with_a_targeting_rule_is_switched_in_its_rule(tmp_path: Path) -> None:
    pinned = orjson.loads(FLAGD_JSON.read_bytes())
    rule = pinned["flags"]["productCatalogFailure"]["targeting"]["if"]
    driver = flag_file(tmp_path)
    inj = injector(tmp_path, driver)
    assert inj.preflight() == []
    schedule = inj.run()

    assert {e.slot_id: e.status for e in schedule.entries} == {"p1": "realised", "c1": "realised"}
    on, off = driver.writes[0], driver.writes[1]
    catalog_on = copy.deepcopy(pinned)
    catalog_on["flags"]["productCatalogFailure"]["targeting"]["if"] = [rule[0], "on", "off"]
    assert on == catalog_on, "the on changed more than the rule's then-branch"
    assert off == pinned, "the off did not restore the pinned flag"
    payment_on = copy.deepcopy(pinned)
    payment_on["flags"]["paymentFailure"]["defaultVariant"] = "50%"
    assert driver.writes[2:] == [payment_on, pinned], "a flag without targeting is no longer set by defaultVariant"
    assert orjson.loads(driver.path.read_bytes()) == pinned

    confirms = [
        (e["slot_id"], e["phase"], e["detail"]["readback"])
        for e in read_events(tmp_path / "events.jsonl")
        if e["action"] == "flag.confirm"
    ]
    assert confirms == [("p1", "on", "on"), ("p1", "off", "off"), ("c1", "on", "50%"), ("c1", "off", "off")]


def test_a_flag_left_on_in_its_rule_is_not_taken_for_off(tmp_path: Path) -> None:
    def left_on(cfg: dict) -> None:
        cfg["flags"]["productCatalogFailure"]["targeting"]["if"][1] = "on"

    driver = flag_file(tmp_path, left_on)
    inj = injector(tmp_path, driver)
    assert inj.preflight() == ["local: productCatalogFailure is 'on', not off, before the capture"]
    inj.run()
    after = orjson.loads(driver.path.read_bytes())["flags"]["productCatalogFailure"]
    assert after["targeting"]["if"][1:] == ["off", "off"], "the reset left the rule on"
