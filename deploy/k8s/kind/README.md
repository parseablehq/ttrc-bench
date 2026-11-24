# kind for TTRC-Bench

A local kind cluster that pulls every pinned image through pull-through caches, and serves the background
generators' source without a GitHub token. Nothing here is used on GKE.

| File | What |
|---|---|
| `up.sh mirrors` | four `registry` caches on the kind network, one per upstream (ghcr.io, docker.io, quay.io, gcr.io), with named volumes |
| `up.sh cluster <name>` | writes `.otb/k8s/kind-<name>.yaml` (the containerd mirror config, an absolute path to `certs.d/`) and creates the cluster |
| `certs.d/<upstream>/hosts.toml` | tells the node's containerd to pull each upstream from its cache |
| `warm.py` | fetches every blob of every image in `deploy/k8s/charts.lock.yaml` (or the refs given) through the caches |
| `bg-source.sh start <clone>` | `git daemon` serving a local otel-benchmark clone as `git://host.docker.internal:9418/<clone>` |
| `bg-source-policy.yaml` | lets the generators' build containers reach that port; kind's kindnet enforces NetworkPolicy |

```sh
deploy/k8s/kind/up.sh mirrors
uv run python deploy/k8s/kind/warm.py                 # once per lock change; about 3 GB the first time
deploy/k8s/kind/up.sh cluster otb-i1
deploy/k8s/kind/bg-source.sh start ~/parseable-otel-benchmark-otb
kubectl --context kind-otb-i1 apply -f deploy/k8s/kind/bg-source-policy.yaml
```

The caches serve the upstream manifests unchanged, so a pod that names a digest finds it; `kind load
docker-image` does not (it re-serialises the image, see `deploy/k8s/stacks/NOTES.md`). Never pull with `crictl`
inside a node: the cache stays cold and the next cluster pulls again. An image missing from the lock is a test
failure (`tests/e2e/test_k8s_images_locked.py`), so the lock is the warm list.

The background build (`deploy/k8s/background/build.yaml`) clones a pinned commit of a private repository. On
kind, `bg-source.sh` serves the local clone over `git://` instead, and `otb capture --background-source` points
the build at it. A hostPath mount of the clone was ruled out: `otb-bg` is PodSecurity `restricted`, which forbids
hostPath volumes.

Delete the cluster when done; the caches and their volumes stay for the next one.

```sh
kind delete cluster --name otb-i1
deploy/k8s/kind/bg-source.sh stop
```
