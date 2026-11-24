# Contributing to TTRC-Bench

## Set up

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run pytest -q
```

That is the whole CI job (`.github/workflows/ci.yaml`), and it needs no Docker. For work on stacks, also install
Docker with Compose v2 and Node.js, then run `uv run otb bootstrap --profile laptop` and
`uv run otb doctor --profile laptop`. [`README.md`](README.md) lists the requirements.

## Test rules

[`AGENTS.md`](AGENTS.md) holds the rules, and they apply to people too:

- Never write unit tests after you write code.
- Prefer E2E tests, and make each one leave a verifiable, repeatable artifact.
- To test a system in isolation, first write down every way it could fail, then write the code. The existing
  unit tests (`tests/unit/`) list their failure modes at the top.

Which tests run where:

| Directory | Runs | Needs |
|---|---|---|
| `tests/e2e/`, `tests/unit/` | `uv run pytest -q`, in CI | nothing beyond `uv sync` |
| `tests/e2e/test_injector_flagd.py` | `OTB_DOCKER_TESTS=1 uv run pytest -q` | Docker and the demo checkout (`OTB_DEMO_DIR`) |
| `tests/e2e/test_dryrun_*.py`, the dry-run cases in `test_task_suite.py` | `uv run pytest -q` when the data exists | the v2 recording (`OTB_V2_DRYRUN`, `OTB_V2_ROOT`) and a built `corpora/dryrun` |
| `tests/live/` | `uv run python tests/live/<script>.py` | running stacks |
| `tests/scale/` | `uv run python tests/scale/<script>.py` | the full dry-run recording or archive |
| `tests/independent/` | called by the E2E tests | nothing; these scripts never import `otb` |

A test that cannot find its data skips. A test that finds its data and fails is a failure.

## Refresh evidence artifacts

E2E tests write their evidence to a temporary directory. `tests/clean_tree.py` fails any pytest session that
changes `git status`. When a change moves committed evidence on purpose:

```sh
OTB_WRITE_ARTIFACTS=1 uv run pytest -q
git diff tests/e2e/artifacts/
```

That writes `tests/e2e/artifacts/*.json` and copies run directories to `.otb/e2e/`. Values that vary between runs
(timings, crash points) go to `<name>.timing.json`, which no test reads back. Commit the refreshed artifacts in
the same commit as the change that moved them, and say in the message why they moved. Then run
`uv run pytest -q` once more without the variable. It must pass and leave the tree clean.

Scripts under `tests/live/` write their evidence under `docs/v1/units/`, which is not part of this repository
(README, Provenance); pass another directory or create it. Scripts under `tests/scale/` write theirs
under `<corpus>/derived/scale/`, where `otb scale-report` reads it, or to `--out`. Each unit record cites the files it
produced.

## Add a stack or a template

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#extension-points) has the steps. In short:

- A stack is a directory under `stacks/`. Pin every image by digest, keep its ingest collector synchronous, give
  it the same budget as the others in both profiles, implement every gate check in `probes.py`, and disclose
  every setting that departs from the vendor's defaults in `stack.yaml`. Run `otb doctor`, a replay and the gate,
  and name every gate failure.
- A template is a YAML file under `tasks/templates/`, with new measures under `otb/ref/measures/`. Follow the
  rules in [`benchmark/TASKS.md`](benchmark/TASKS.md), pass `otb tasks lint`, and add a handler to
  `tests/independent/task_truth.py` so its truth is recomputed without `otb`.

Changes to the corpus format, the reference schema, the stack contract or the field types are one-way doors:
published corpora and results depend on them. Open an issue with the design before the code.

## Commit conventions

- One logical change per commit, and each commit passes lint and the offline tests.
- The subject is an imperative sentence of at most 72 characters, optionally prefixed with the area:
  `gate: fetch long-arrival traces by id`, `tests: ...`, `docs: ...`, `ci: ...`.
- The body says what changed and why, and names the evidence: the test, the artifact, or the unit record.
- Record a decision that changes the method as a row of `docs/k8s/trail.tsv`, with the commands that check it.
- A change to `otb/grade.py`, `otb/report.py`, `otb/run/proxy.py`, `otb/run/dispatch.py` or anything they import
  changes the pre-registered code closure. Runs registered before it will record the change in
  `grade/provenance.json`.
