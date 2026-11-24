#!/usr/bin/env bash
# Serve a local otel-benchmark clone to the kind cluster over git://, so the background generators build from
# it without the private repository's token (deploy/k8s/background/build.yaml). The pods reach the host as
# host.docker.internal; pass the URL to the capture:
#
#   deploy/k8s/kind/bg-source.sh start /path/to/otel-benchmark-clone      (prints the URL)
#   deploy/k8s/kind/bg-source.sh stop
#   otb capture ... --decoy-driver k8s --background-source git://host.docker.internal:9418/otel-benchmark
#
# The build fetches the pinned commit by hash, which git daemon allows only with uploadpack.allowAnySHA1InWant
# set on the served repository (a local config entry; build.sh falls back to a full fetch without it). A hostPath
# mount of the clone was ruled out: otb-bg is PodSecurity restricted, which forbids hostPath volumes.
set -euo pipefail
PID="${TMPDIR:-/tmp}/otb-bg-source.pid"
case "${1:-}" in
  start)
    clone="$(cd "${2:?path to the otel-benchmark clone}" && pwd)"
    git -C "$clone" config uploadpack.allowAnySHA1InWant true
    if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then kill "$(cat "$PID")"; fi
    git daemon --reuseaddr --export-all --base-path="$(dirname "$clone")" --port=9418 --detach --pid-file="$PID" \
      --listen=0.0.0.0 "$clone"
    echo "git://host.docker.internal:9418/$(basename "$clone")  (commit $(git -C "$clone" rev-parse HEAD))"
    ;;
  stop)
    if [ -f "$PID" ]; then kill "$(cat "$PID")" 2>/dev/null || true; rm -f "$PID"; fi
    ;;
  *) sed -n '2,8p' "$0" >&2; exit 2 ;;
esac
