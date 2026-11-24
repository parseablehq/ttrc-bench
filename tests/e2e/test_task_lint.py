"""`otb tasks lint` over the shipped templates, and over copies broken in the ways a template author gets wrong:
an unknown measure, an L4 statement that names the root service, a positive without its control twin, a twin
that rewrites the statement, a bind path with a typo, a constant truth on a scored field with no twin to differ
from (l5-red-herring scored, l4-unanswerable without its twin, l4-infra-vs-app's old constant layer), and an
unscored field without a reason, a statement that counts spans "ended with error" in a window (they lie in it by
start time), a `min` on a string truth, and a measure bound before the schedule entry whose instance scopes it. Each
must fail with its own named error.

Evidence: tests/e2e/artifacts/task_lint.json (the CLI output for the clean run and for each broken copy).
"""

import json
import shutil
from pathlib import Path

from conftest import REPO, otb

TEMPLATES = REPO / "tasks/templates"
FAULT_BIND = "  fault: { schedule: { kind: fault } }"
WINDOW_BIND = "  window: { around: fault.window, pad_before_s: 600, pad_after_s: 600 }"

BREAKS = {
    "unknown_measure": (
        "l1-span-errors.yaml",
        lambda s: s.replace("measure: span_count", "measure: span_errors"),
        [
            "l1-span-errors: truth.error_span_count: unknown measure 'span_errors'",
            "l1-span-errors: truth.first_error_at: unknown measure 'span_errors'",
        ],
    ),
    "l4_names_root_service": (
        "l4-symptom-rca.yaml",
        lambda s: s.replace("Find the root cause.", "Look at `{{fault.root_service}}` first."),
        [
            "l4-symptom-rca: L4 statement names fault identity: {{fault.root_service}}",
            "l4-symptom-rca: L4 statement quotes a literal name in backticks",
        ],
    ),
    "missing_twin": (
        "l3-incident-window.yaml",
        lambda s: s[: s.index("twin:")],
        [
            "l3-incident-window: has incident_occurred but no twin: positives need a negative-control twin with the "
            "same framing",
            "l3-incident-window: truth.incident_occurred: constant True on a scored field; only a twin whose truth "
            "differs makes it informative (else unscore it with a reason)",
        ],
    ),
    "twin_with_own_statement": (
        "l3-error-to-log.yaml",
        lambda s: s.replace("twin:\n  control: decoy\n", "twin:\n  control: decoy\n  statement: All is well.\n"),
        ["l3-error-to-log: twin: unknown keys ['statement']; allowed ['control', 'rebind', 'truth', 'quiet']"],
    ),
    "constant_scored_truth": (
        "l5-red-herring.yaml",
        lambda s: s[: s.index("    scored: false")] + s[s.index("  failure_window:") :],
        [
            "l5-red-herring: truth.release_related: constant False on a scored field; only a twin whose truth "
            "differs makes it informative (else unscore it with a reason)"
        ],
    ),
    "constant_scored_truth_without_twin": (
        "l4-unanswerable.yaml",
        lambda s: s[: s.index("twin:")],
        [
            "l4-unanswerable: truth.evidence: constant 'measured' on a scored field; only a twin whose truth differs "
            "makes it informative (else unscore it with a reason)"
        ],
    ),
    "constant_layer": (
        "l4-infra-vs-app.yaml",
        lambda s: s.replace("  layer: { bind: layer }", "  layer: { const: resource }"),
        [
            "l4-infra-vs-app: truth.layer: constant 'resource' on a scored field; only a twin whose truth differs "
            "makes it informative (else unscore it with a reason)"
        ],
    ),
    "unscored_without_reason": (
        "l5-red-herring.yaml",
        lambda s: s[: s.index("    reason: >")] + s[s.index("  failure_window:") :],
        ["l5-red-herring: fields.release_related: an unscored field states why (reason)"],
    ),
    "span_counted_by_end": (
        "l2-one-per-signal.yaml",
        lambda s: s.replace("that started in that window have error status", "ended with error status"),
        [
            "l2-one-per-signal: statement says 'ended with error': a span lies in a window by its start time, so ask "
            "for the spans that started in it and have error status"
        ],
    ),
    "window_before_the_instance": (
        "l2-latency-shift.yaml",
        lambda s: s.replace(f"{FAULT_BIND}\n{WINDOW_BIND}", f"{WINDOW_BIND}\n{FAULT_BIND}"),
        [
            "l2-latency-shift: the first bind must be a schedule bind: its entry's demo instance scopes every measure",
            "l2-latency-shift: binds.window: 'fault' is not bound",
        ],
    ),
    "min_on_a_string_field": (
        "l3-latency-localise.yaml",
        lambda s: s.replace(
            "  service: { bind: fault.root_service }", "  service: { bind: fault.root_service, min: 1 }"
        ),
        ["l3-latency-localise: truth.service: min applies to integer and number fields, not string"],
    ),
    "bind_path_typo": (
        "l1-span-errors.yaml",
        lambda s: s.replace("service: fault.root_service, kind", "service: fault.root_servce, kind", 1),
        [
            "l1-span-errors: truth.error_span_count.service: 'fault.root_servce': 'root_servce' is not one of "
            "['slot_id', 'kind', 'instance', 'status', 'observed', 'root_service', 'class', 'flag', 'variant', "
            "'decoy_type', 'group', 'relation', 'window', 'onset', 'offset', 'symptom_window', 'signature']"
        ],
    ),
}


def test_shipped_templates_lint_clean() -> None:
    p = otb("tasks", "lint")
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "26 templates lint clean"
    assert len(list(TEMPLATES.glob("*.yaml"))) == 26


def test_broken_copies_fail_with_named_errors(tmp_path: Path, artifacts: Path) -> None:
    out = {"clean": otb("tasks", "lint").stdout.strip()}
    for name, (file, breaker, expected) in BREAKS.items():
        d = tmp_path / name
        shutil.copytree(TEMPLATES, d)
        (d / file).write_text(breaker((d / file).read_text()))
        p = otb("tasks", "lint", "--templates", d)
        errors = p.stderr.strip().splitlines()
        out[name] = {"exit": p.returncode, "stderr": errors}
        assert p.returncode == 1, f"{name}: lint passed a broken template"
        assert errors[:-1] == expected, f"{name}: {errors}"
        assert errors[-1] == f"{len(expected)} lint errors in {d}"
    (artifacts / "task_lint.json").write_text(
        json.dumps(out, indent=1, sort_keys=True).replace(str(tmp_path), "<tmp>") + "\n"
    )
