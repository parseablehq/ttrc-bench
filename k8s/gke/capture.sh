#!/usr/bin/env bash
# Usage (inside the runner, via podrun.sh): bash k8s/gke/capture.sh <corpus name> [scenario, default smoke-30]
# The live-30b sequence on GKE: fresh stacks, then the gateway and follow-mode ingest, then the OTel Demo with the
# TTRC-Bench overlay; then seal checks, the reference store, the task suite and the gate.
set -euo pipefail
NAME="$1"
SCENARIO="${2:-scenarios/smoke-30.yaml}"
R=/work/parseable
D=$R/.otb/demo-3.1.0
cd "$R"
trap 'rc=$?; kill $(jobs -p) 2>/dev/null; exit $rc' EXIT

uv run otb down --profile bighost --volumes
uv run otb up --profile bighost

# The demo collector's file queue lives in a volume that outlives its container: drop what an earlier capture left
# unsent, before the gateway listens, so none of it reaches this corpus (the gateway also quarantines such frames).
(cd "$D" && docker compose -p demo-local down)
docker volume rm -f demo-local_otb-queue

# The capture and its follow-mode ingest run under the supervisor, which restarts either one when it exits until
# the corpus is sealed; both resume. It appends their output to /work/logs/$NAME-capture.log and -ingest.log.
: > "/work/logs/$NAME-capture.log"
: > "/work/logs/$NAME-ingest.log"
k8s/gke/supervise.sh "corpora/$NAME" /work/logs -- \
  uv run otb capture "$SCENARIO" --out "corpora/$NAME" \
  --flagd-ui http://127.0.0.1:14000 --ofrep http://127.0.0.1:18016 --locust http://127.0.0.1:8080/loadgen \
  --compose-project demo-local --source capture/demo-source-local.yaml --wait-demo 600 \
  -- uv run otb ingest "corpora/$NAME" --mode follow --wait-corpus 900 \
  > "/work/logs/$NAME-supervise.log" 2>&1 &
SUPERVISOR=$!
until grep -q "gateway on" "/work/logs/$NAME-capture.log"; do sleep 1; done

# CPU and memory per container once a minute, to size demo pods and check the stacks keep up at each load step.
while sleep 60; do docker stats --no-stream --format '{{json .}}' | sed "s/^/{\"t\":\"$(date -u +%FT%TZ)\",\"s\":/; s/$/}/"; done \
  > "/work/logs/$NAME-stats.jsonl" 2>&1 &

# The daemon's socket is not at the demo's default path in this pod; the demo collector's docker_stats needs it.
(cd "$D" && OTB_INSTANCE=local OTB_GATEWAY=http://host.docker.internal:4318 DOCKER_SOCK=/run/otb-docker/docker.sock \
  OTEL_COLLECTOR_CONFIG_EXTRAS=$R/capture/demo-overlay/otelcol-otb.yml LOCUST_USERS=8 \
  docker compose -p demo-local -f compose.yaml -f $R/capture/demo-overlay/compose.otb.yaml \
  -f $R/capture/demo-overlay/compose.gke.yaml up -d --no-build)
(cd "$D" && docker compose -p demo-local images --format json) > "/work/logs/$NAME-demo-images.json"

# Stop the demo once the capture has sealed, so it queues nothing more, then let the ingest drain.
until grep -qx 'sealed: true' "corpora/$NAME/manifest.yaml" || ! kill -0 "$SUPERVISOR" 2>/dev/null; do sleep 10; done
(cd "$D" && docker compose -p demo-local stop)
wait "$SUPERVISOR"

uv run otb corpus verify "corpora/$NAME" --level full
uv run otb ref build "corpora/$NAME"
uv run otb tasks instantiate --corpus "corpora/$NAME" --seed 7 --out "suites/$NAME"
SESSION=$(ls -t state/sessions | grep "^$NAME-follow-" | head -1)
echo "session $SESSION"
uv run otb gate --session "$SESSION" --suite "suites/$NAME"
