#!/usr/bin/env bash
# Usage: k8s/gke/podrun.sh <name> <command...>
# Runs a command in the runner container of otb-host-0, detached, logging to /work/logs/<name>.log with its exit
# code in /work/logs/<name>.rc, so a long step survives a dropped kubectl session. Prints the log path.
# OTB_K8S_CONTEXT names the kube context of the cluster.
set -euo pipefail
: "${OTB_K8S_CONTEXT:?set OTB_K8S_CONTEXT to the kube context of the cluster}"
NAME="$1"; shift
CMD="$(printf '%q ' "$@")"
kubectl --context "$OTB_K8S_CONTEXT" -n otb exec -i otb-host-0 -c runner -- bash -s <<EOF
mkdir -p /work/logs
cat > /work/logs/$NAME.sh <<'INNER'
cd /work/parseable
$CMD
INNER
nohup bash -c 'bash /work/logs/$NAME.sh > /work/logs/$NAME.log 2>&1; echo \$? > /work/logs/$NAME.rc' >/dev/null 2>&1 &
echo /work/logs/$NAME.log
EOF
