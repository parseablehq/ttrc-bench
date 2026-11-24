#!/usr/bin/env bash
# A kind cluster for TTRC-Bench behind pull-through registry caches: one distribution registry per upstream
# (ghcr.io, docker.io, quay.io, gcr.io) on the kind network, each with a named volume so cached blobs outlive
# clusters. They serve the upstream manifests unchanged, so digest pins resolve, and the node never pulls from the
# internet twice. Warm them with warm.py before creating a cluster.
#
#   deploy/k8s/kind/up.sh mirrors                 start (or keep) the four caches
#   deploy/k8s/kind/up.sh cluster <name>          write .otb/k8s/kind-<name>.yaml and create the cluster
#
# kind's extraMounts need an absolute hostPath, so the config is written at run time from this checkout's
# certs.d; nothing in the repo carries a machine path.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
REGISTRY_IMAGE=registry@sha256:6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e
UPSTREAMS=(ghcr.io docker.io quay.io gcr.io)

mirrors() {
  docker network inspect kind >/dev/null 2>&1 || docker network create kind >/dev/null
  local port=5101
  for up in "${UPSTREAMS[@]}"; do
    local name="otb-mirror-${up%%.*}" remote="https://${up/docker.io/registry-1.docker.io}"
    if ! docker inspect "$name" >/dev/null 2>&1; then
      docker run -d --restart unless-stopped --name "$name" --network kind -p "127.0.0.1:${port}:5000" \
        -v "${name}:/var/lib/registry" -e REGISTRY_PROXY_REMOTEURL="$remote" -e REGISTRY_PROXY_TTL=0 \
        --memory 256m "$REGISTRY_IMAGE" >/dev/null
    elif [ "$(docker inspect -f '{{.State.Running}}' "$name")" != true ]; then
      docker start "$name" >/dev/null
    fi
    echo "$up -> $name (host 127.0.0.1:${port}, nodes http://${name}:5000)"
    port=$((port + 1))
  done
}

cluster() {
  local name="${1:?cluster name}" config="$REPO/.otb/k8s/kind-${1}.yaml"
  mkdir -p "$(dirname "$config")"
  cat > "$config" <<YAML
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
  - |-
    [plugins."io.containerd.grpc.v1.cri".registry]
      config_path = "/etc/containerd/certs.d"
nodes:
  - role: control-plane
    extraMounts:
      - hostPath: $HERE/certs.d
        containerPath: /etc/containerd/certs.d
        readOnly: true
YAML
  kind create cluster --name "$name" --config "$config" --wait 120s
}

case "${1:-}" in
  mirrors) mirrors ;;
  cluster) mirrors >/dev/null; cluster "${2:-}" ;;
  *) sed -n '2,10p' "$0" >&2; exit 2 ;;
esac
