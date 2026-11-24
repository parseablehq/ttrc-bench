"""How tests/scale/stack_disk.py turns `docker system df -v` into bytes per stack, tested in isolation because no
Docker ran in U10 and a wrong number here becomes a wrong disk projection in every scale report.

Ways it can be wrong, written before the code:
 1. Docker's sizes are decimal (kB = 1000 B), and are read as binary.
 2. A fractional size ("1.032kB") is truncated, or "0B" fails to parse.
 3. An unknown unit is silently read as bytes.
 4. A volume of another compose project (the demo's) is counted for a stack.
 5. Stack b1's prefix also takes a volume of a stack b12.
 6. A stack with no volume reports 0 instead of saying it has none.
"""

from __future__ import annotations

import pytest
from scale.stack_disk import parse_size, volumes_by_stack

DF = {
    "Volumes": [
        {"Name": "otb_b1-prometheus", "Size": "1.5GB"},
        {"Name": "otb_b1-loki", "Size": "1.032kB"},
        {"Name": "otb_b12-data", "Size": "7MB"},
        {"Name": "otb_b4-data", "Size": "0B"},
        {"Name": "demo-eu1_b1-prometheus", "Size": "9GB"},
    ]
}


def test_sizes_are_decimal_and_fractional() -> None:
    assert [parse_size(s) for s in ("0B", "512B", "1.032kB", "1.5GB", "2TB", "7MB")] == [
        0,
        512,
        1032,
        1_500_000_000,
        2_000_000_000_000,
        7_000_000,
    ]


def test_an_unknown_unit_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown size '3KiB'"):
        parse_size("3KiB")


def test_volumes_belong_to_their_own_stack_and_project() -> None:
    got = volumes_by_stack(DF, "otb", ["b1-lgtm", "b4-parseable", "b3-clickhouse"])
    assert got == {
        "b1-lgtm": {"otb_b1-prometheus": 1_500_000_000, "otb_b1-loki": 1032},
        "b4-parseable": {"otb_b4-data": 0},
        "b3-clickhouse": {},
    }
