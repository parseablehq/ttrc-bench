# How to deploy the TTRC-Bench runner pod

These files deploy the runner StatefulSet described in `docs/k8s/DESIGN.md` ("Agent and MCP isolation"). The
pod has three containers on public images pinned by digest (`pins.yaml`, `deploy/k8s/charts.lock.yaml`); nothing
is built or pushed to a registry. `runner` runs `otb` and holds the Kubernetes API token, the admin secrets and
the PVC. `tools` runs the recording proxy and a fresh vendor MCP server per cell. `agent` runs `claude -p` per
cell. An `install` init container puts the otb checkout, its venv, uv, kubectl, helm and Claude Code on the PVC
from a git bundle and pinned downloads (`install.sh`), each sha256-checked, and converges on every restart.

The commands below assume a kind cluster. Replace `<cluster>` with its name. On GKE, use the cluster's context.

## Generate and apply the manifests

`namespaces-stacks.yaml`, `rbac.yaml`, `networkpolicies.yaml`, `install-configmap.yaml` and
`fixture-configmap.yaml` are written by `generate.py` from the stacks, the scenarios with `runtime: k8s`
(one `otb-demo-<instance>` namespace per instance), `install.sh`, `pins.yaml` and the fixture server.
`tests/e2e/test_k8s_runner_manifests.py` fails when they are stale.

```sh
uv run python deploy/k8s/runner/generate.py
```

1. Create the namespaces and the RBAC. `rbac.yaml` binds into each stack namespace, so they must exist first.

   ```sh
   kubectl apply -f deploy/k8s/runner/namespace.yaml
   kubectl apply -f deploy/k8s/runner/namespaces-stacks.yaml
   kubectl apply -f deploy/k8s/runner/rbac.yaml
   ```

2. Copy `secrets.example.yaml` outside the repo, replace every `FAKE` value, and apply the copy.

   ```sh
   kubectl apply -f ~/otb-secrets.yaml
   ```

3. Apply the gateway Service and the install ConfigMap.

   ```sh
   kubectl apply -f deploy/k8s/runner/service.yaml
   kubectl apply -f deploy/k8s/runner/install-configmap.yaml
   ```

4. Apply the NetworkPolicies. `networkpolicies.yaml` needs two addresses: `KUBE_API_CIDR`, the API server
   endpoint behind the `kubernetes` Service (policy is evaluated after kube-proxy's DNAT), and `NODE_CIDR`, the
   nodes whose kubelets the demo collectors read (`kubeletstats`, port 10250). On kind both are the control-plane
   node.

   ```sh
   NODE=$(docker inspect <cluster>-control-plane --format '{{ .NetworkSettings.Networks.kind.IPAddress }}')
   sed "s|KUBE_API_CIDR|$NODE/32|; s|NODE_CIDR|$NODE/32|" deploy/k8s/runner/networkpolicies.yaml | kubectl apply -f -
   ```

   On GKE, `KUBE_API_CIDR` is the master endpoint from `gcloud container clusters describe` (port 443) and
   `NODE_CIDR` the node subnet's primary range.

5. Apply the StatefulSet, then hand the checkout to the installer. The init container waits for
   `/work/otb.bundle`; copy it in as soon as the container runs. The bundle carries `HEAD` and its branch.
   `kubectl cp` creates the file before it has written it, so copy to `otb.bundle.part` and rename it.

   ```sh
   kubectl apply -f deploy/k8s/runner/statefulset.yaml
   git bundle create /tmp/otb.bundle HEAD "$(git rev-parse --abbrev-ref HEAD)"
   kubectl -n otb cp /tmp/otb.bundle otb-runner-0:/work/otb.bundle.part -c install
   kubectl -n otb exec otb-runner-0 -c install -- mv /work/otb.bundle.part /work/otb.bundle
   kubectl -n otb logs -f otb-runner-0 -c install
   kubectl -n otb rollout status statefulset/otb-runner
   kubectl -n otb exec otb-runner-0 -c runner -- cat /work/tools/versions.json
   ```

   The installer refuses any download whose sha256 differs from `pins.yaml`, fails when `claude --version` or
   the uv-resolved Python differ from the pins, and fails when the checkout is dirty after `uv sync`. A new
   commit is a new bundle: copy it in and delete the pod; the installer re-clones when the bundle's `HEAD` is
   not the checkout's, carrying `.otb` (charts, secrets, server environments), `state`, `suites` and the venv
   over to the new checkout.

6. For the kind test, apply the fixture MCP server. It stands in for a backend in `otb-fx` on the same public
   python image, with the MCP SDK installed from `fixture-requirements.txt` (`--require-hashes`).

   ```sh
   kubectl apply -f deploy/k8s/runner/fixture-configmap.yaml
   kubectl apply -f deploy/k8s/runner/fixture-stack.yaml
   kubectl -n otb-fx rollout status deployment/fixture-mcp
   ```

If you delete a stack namespace, its RoleBinding and NetworkPolicies go with it. Apply `namespaces-stacks.yaml`,
`rbac.yaml` and the policies again before the runner installs into that namespace.

## Run the stacks and a run from inside the pod

The operator runs every `otb` command in the `runner` container, from `/work/app` (its `PATH` has the venv,
`uv`, `kubectl` and `helm`). `otb` authenticates to the API server with the pod's service account.

```sh
kubectl -n otb exec otb-runner-0 -c runner -- otb bootstrap --runtime k8s
kubectl -n otb exec otb-runner-0 -c runner -- otb doctor --runtime k8s
kubectl -n otb exec otb-runner-0 -c runner -- otb up --runtime k8s --profile gke --stacks b4
```

Run directories live on the PVC under `/work/runs`. Use `examples/run-dryrun-oracle.yaml` as the starting point
for the run config; `isolation: split` is what the pod sets through `OTB_ISOLATION`.

```sh
kubectl -n otb cp run.yaml otb-runner-0:/work/run.yaml -c runner
kubectl -n otb exec otb-runner-0 -c runner -- otb run new /work/runs/scratch-k8s --config /work/run.yaml
kubectl -n otb exec otb-runner-0 -c runner -- otb run register /work/runs/scratch-k8s
kubectl -n otb exec otb-runner-0 -c runner -- otb run dispatch /work/runs/scratch-k8s
kubectl -n otb exec otb-runner-0 -c runner -- otb run grade /work/runs/scratch-k8s
kubectl -n otb exec otb-runner-0 -c runner -- otb run report /work/runs/scratch-k8s
kubectl -n otb exec otb-runner-0 -c runner -- otb run selfcheck /work/runs/scratch-k8s
```

## Verify the container isolation

`tests/e2e/test_k8s_runner.py` (`OTB_K8S_TESTS=1`) checks all of this on kind and writes
`tests/e2e/artifacts/k8s_runner.json`. By hand, check each container with `kubectl exec`. Every container runs
as uid 10001 on a read-only root filesystem.

```sh
for c in runner tools agent; do
  kubectl -n otb exec otb-runner-0 -c $c -- sh -c 'id -u; touch /probe || echo rootfs read-only'
done
```

The API token is mounted in `runner` only; in `tools` and `agent` the first command below fails with
`No such file or directory`, and `kubectl auth can-i` errors there while it answers `yes` in `runner`.

```sh
kubectl -n otb exec otb-runner-0 -c runner -- ls /var/run/secrets/kubernetes.io/serviceaccount
kubectl -n otb exec otb-runner-0 -c tools -- ls /var/run/secrets/kubernetes.io/serviceaccount
kubectl -n otb exec otb-runner-0 -c agent -- ls /var/run/secrets/kubernetes.io/serviceaccount
```

These checks also hold:

- `tools` and `agent` mount read-only subPaths of the PVC only: the otb package, its venv and the interpreters,
  and (`tools`) `servers/`, `tests/fixtures/` and the vendor environments under `.otb/`. Neither sees `/work`,
  `.otb/secrets`, `suites/`, `corpora/` or `runs/`.
- `tools` sees `/secrets/<stack>.env`; `agent` sees `/credentials` and nothing of `/secrets`.
- `env` in `tools` and `agent` shows no `FIXTURE_ADMIN_PASSWORD`. Only `runner` gets `otb-stack-admin`.

## Know the NetworkPolicy limits

- A NetworkPolicy selects pods, not containers. The per-container egress rows in `DESIGN.md` collapse into one
  policy for the whole runner pod, so `agent` can reach the backends and `tools` can reach the model API. A
  per-container restriction needs separate pods or an in-pod egress proxy.
- kind's kindnet enforces NetworkPolicy (kind v0.33.0: a pod-to-host connection the runner's policy did not
  list was refused while the node's own was not), so the kind integration run exercises these policies. GKE
  without Dataplane V2 does not enforce them; `benchmark/SPEC.md` discloses it.
- The model API rule allows any public address on port 443. A rule for `api.anthropic.com` alone needs a
  `CiliumNetworkPolicy` with `toFQDNs`, which needs Cilium, GKE Dataplane V2 or an egress proxy.
