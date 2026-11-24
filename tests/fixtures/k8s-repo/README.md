# k8s-repo: a two-stack repository for the Kubernetes runtime tests

`tests/e2e/test_k8s_lifecycle.py` copies this tree to a temporary directory, puts a fake `helm` and `kubectl` on
PATH and drives `otb.stacks.lifecycle` through `up`, `down`, `bootstrap` and `doctor` under both runtimes.

- `stacks/k1-alpha`: two chart releases (`k1-store`, `k1-ingest`) and a `kind: binary` MCP server.
- `stacks/k2-beta`: a manifests release whose paths carry `{profile}` (`k2-store`) and one chart release.
- `deploy/k8s/charts.lock.yaml`: the fake `helm pull` writes `chart <name> <version>\n`; the sha256 values are of
  those bytes.
- `deploy/k8s/profiles/{kind,gke}.yaml`: one values overlay per release name.
- `__PORT__` in `stacks/*/stack.yaml` and `servers/fx/fx-mcp.lock.yaml` is replaced by the test's local HTTP
  server port, and `mcp.pin` is recomputed from the rewritten lockfile.

`tests/fixtures/k8s-render/<case>/<release>.yaml` is what the fake `helm template` prints for a chart release:
`good` passes doctor; `burstable`, `nodigest` and `unequal` each trip one check.
