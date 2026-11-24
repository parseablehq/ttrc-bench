#!/usr/bin/env bash
# Serve a local otel-benchmark clone to the GKE background build from the otb-bg-source pod (bg-source.yaml):
#
#   deploy/k8s/gke/bg-source.sh /path/to/otel-benchmark-clone      (prints the URL)
#   otb capture ... --decoy-driver k8s --background-source git://otb-bg-source.otb:9418/otel-benchmark
#
# Needs OTB_K8S_CONTEXT, namespaces otb and otb-bg. The clone must hold the commit build.yaml pins.
set -euo pipefail
clone="$(cd "${1:?path to the otel-benchmark clone}" && pwd)"
here="$(cd "$(dirname "$0")" && pwd)"
k() { kubectl --context "${OTB_K8S_CONTEXT:?}" -n otb "$@"; }
commit="$(sed -n 's/^ *OTB_BG_COMMIT: *//p' "$here/../background/build.yaml")"
git -C "$clone" cat-file -e "$commit^{commit}" || { echo "bg-source: $clone lacks $commit" >&2; exit 1; }
bundle="$(mktemp -d)/otel-benchmark.bundle"
git -C "$clone" bundle create "$bundle" "$commit" HEAD 2>/dev/null
# the policy lives in otb-bg, which the capture would create later: create it now, labels and all
kubectl --context "$OTB_K8S_CONTEXT" apply -f "$here/../background/namespace.yaml" -f "$here/bg-source.yaml" >/dev/null
k wait --for=condition=Ready pod/otb-bg-source --timeout=300s >/dev/null
# kubectl cp writes in place: copy under another name, then rename, so a partial copy is never cloned
k cp "$bundle" otb-bg-source:/srv/bundle.part -c git
k exec otb-bg-source -c git -- sh -c 'export HOME=/srv; cd /srv && mv bundle.part bundle && rm -rf otel-benchmark &&
  git clone -q --bare bundle otel-benchmark && git -C otel-benchmark config uploadpack.allowAnySHA1InWant true &&
  rm bundle && touch ready'
k exec otb-bg-source -c git -- git -C /srv/otel-benchmark cat-file -e "$commit^{commit}"
echo "git://otb-bg-source.otb:9418/otel-benchmark  (commit $commit)"
