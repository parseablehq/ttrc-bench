#!/usr/bin/env bash
# Usage: deploy/k8s/demo/install.sh [--template] <instance> [kind|gke]
# Installs (or upgrades) one OpenTelemetry Demo instance into namespace otb-demo-<instance>; its collector labels
# every record cloud.region = service.instance.id = <instance> and exports to $OTB_GATEWAY (default: the in-cluster
# otb-gateway Service). --template prints the rendered manifests instead and touches no cluster.
# Environment: OTB_GATEWAY, OTB_K8S_CONTEXT (kube context; unset in-cluster), OTB_CHART_DIR (default .otb/charts).
set -euo pipefail

TEMPLATE=0
if [[ "${1:-}" == "--template" ]]; then TEMPLATE=1; shift; fi
INSTANCE="${1:?usage: install.sh [--template] <instance> [kind|gke]}"
PROFILE="${2:-gke}"
if [[ ! "$INSTANCE" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]]; then
  echo "install.sh: instance must be a DNS label of at most 32 characters, got '$INSTANCE'" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
[[ -f "$HERE/profile-$PROFILE.yaml" ]] || { echo "install.sh: no profile $HERE/profile-$PROFILE.yaml" >&2; exit 2; }

CHART_VERSION=0.42.3
CHART_SHA256=3c16f3549ea86a85145d7b8db507fe171417d95ebbaf52a9d7f1b668d694ad62
CHART_DIR="${OTB_CHART_DIR:-$REPO/.otb/charts}"
CHART="$CHART_DIR/opentelemetry-demo-$CHART_VERSION.tgz"
if [[ ! -f "$CHART" ]]; then
  mkdir -p "$CHART_DIR"
  helm pull opentelemetry-demo --repo https://open-telemetry.github.io/opentelemetry-helm-charts \
    --version "$CHART_VERSION" -d "$CHART_DIR"
fi
HAVE="$(shasum -a 256 "$CHART" | cut -d' ' -f1)"
if [[ "$HAVE" != "$CHART_SHA256" ]]; then
  echo "install.sh: $CHART has sha256 $HAVE, expected $CHART_SHA256" >&2
  exit 1
fi

ARGS=(otb-demo-"$INSTANCE" "$CHART" --namespace otb-demo-"$INSTANCE"
  -f "$HERE/values.yaml" -f "$HERE/profile-$PROFILE.yaml"
  --set-string global.otb.instance="$INSTANCE")
if [[ -n "${OTB_GATEWAY:-}" ]]; then ARGS+=(--set-string global.otb.gateway="$OTB_GATEWAY"); fi
CTX=()
if [[ -n "${OTB_K8S_CONTEXT:-}" ]]; then CTX=(--kube-context "$OTB_K8S_CONTEXT"); fi

if (( TEMPLATE )); then
  exec helm template "${ARGS[@]}"
fi
exec helm upgrade --install "${ARGS[@]}" "${CTX[@]}" --create-namespace --wait --timeout 15m
