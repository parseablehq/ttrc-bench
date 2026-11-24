#!/usr/bin/env bash
# Usage: k8s/gke/podwait.sh <name> [lines]
# Waits for a podrun.sh step to finish, then prints its exit code and the tail of its log.
# OTB_K8S_CONTEXT names the kube context of the cluster.
set -uo pipefail
: "${OTB_K8S_CONTEXT:?set OTB_K8S_CONTEXT to the kube context of the cluster}"
K=(kubectl --context "$OTB_K8S_CONTEXT" -n otb exec otb-host-0 -c runner --)
until "${K[@]}" test -f "/work/logs/$1.rc"; do sleep 15; done
echo "rc=$("${K[@]}" cat "/work/logs/$1.rc")"
"${K[@]}" tail -n "${2:-30}" "/work/logs/$1.log"
