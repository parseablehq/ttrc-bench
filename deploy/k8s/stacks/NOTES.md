# Where the Kubernetes stacks differ from compose

The releases run compose's images by digest, with compose's configs byte for byte (Tempo's passes through the
chart's template, which reindents it), compose's environment and compose's per-container budgets. These are
the differences that remain. Each is a disclosure candidate; none changes what a stack stores or answers, as far
as the gates on kind show.

## Process limits and users

- **ClickHouse `nofile`.** Compose sets `ulimits.nofile` 262144. A pod cannot set ulimits, so ClickHouse gets the
  node's limit: 2147483584 on kind (measured in the pod). GKE's is not measured yet.
- **Users** match each image's default except where a chart forces one, and there the chart's user is the
  image's user too: Prometheus 65534, Loki 10001, Grafana 472, the collector 10001. Tempo runs as 0:0 as in compose
  (the chart's default 10001 is overridden). Parseable runs as root as in compose (the chart's default 1000 is
  overridden). ClickHouse's entrypoint starts as root and drops to uid 101, as in compose.
- **Hardening the charts add and compose lacks.** Loki and Grafana run with all capabilities dropped, seccomp
  RuntimeDefault and a read-only root filesystem (with emptyDir `/tmp`); Parseable with no privilege escalation.

## Flags, paths and environment

- **Config paths.** Prometheus reads `/etc/config/prometheus.yml` (compose: `/etc/prometheus/prometheus.yml`),
  Loki `/etc/loki/config/config.yaml`, Tempo `/conf/tempo.yaml`, the collectors `/conf/relay.yaml`. Contents are
  the repo files.
- **Prometheus** gets the chart's `--web.console.libraries` and `--web.console.templates` flags. Prometheus 3 ships
  no console files, so they serve nothing. Retention time is unset, as in compose (size only).
- **Loki** gets `-config.expand-env=true` (loki.yaml has no `${}`) and `-memberlist.advertise-addr` (loki.yaml
  configures no memberlist ring). The chart injects `GOMEMLIMIT` (0.85 of the limit) and `GOGC=80` unless they
  are set, and treats `goMemLimitFactor: 0` as unset, so the values restate Go's defaults: `GOMEMLIMIT=off`,
  `GOGC=100`. Grafana's and the collector's `GOMEMLIMIT` injection is switched off.
- **Grafana** gets the chart's `grafana.ini` paths, which equal the image's, `log.mode console` (the image logs to
  console and file), and `unified_storage.index_path` on an emptyDir instead of the data volume: the search index
  (dashboards and folders, which no MCP tool in the run uses) is rebuilt after a restart.
- **Parseable** gets `P_MODE=all` and `HOSTNAME` from the chart; standalone local-store means the same. Its
  chart's `RUST_LOG=warn` is removed so the log level is compose's.
- **Collector** gets the chart's `MY_POD_IP` and `OTEL_K8S_*` variables, which collector.yaml does not read.

## Names and ports

- In-cluster ports equal compose's container ports. Service names equal compose service names (`prometheus`,
  `loki`, `tempo`, `grafana`, `clickhouse`, `parseable`, `ingest-b<n>`), so collector.yaml and datasources.yaml
  are unchanged. Parseable's chart Service is `parseable-standalone-service` on port 80; b4-parseable/objects.yaml adds
  a `parseable` Service on 8000.
- The in-cluster addresses are reachable from the runner pod, where `otb up --runtime k8s` runs its readiness
  checks. From a laptop, `kubectl port-forward` is the tunnel to a Service, which compose does not need.

## Restarts, probes and volumes

- **Probes.** Compose restarts a container only when it exits. The charts add liveness probes that also restart a
  process slow to answer, and on kind Prometheus stopped answering its probe under the gate's queries, so every
  chart release renders through the `no-liveness` helm plugin (`deploy/k8s/stacks/no-liveness/`, installed by
  `otb bootstrap --runtime k8s`), which removes the livenessProbe from every backend and ingest container;
  `tests/k8s/check_rendered.py` refuses one that remains. Readiness probes stay: they keep a pod out of its
  Service until it first answers; compose gates no traffic on readiness, so each probe tolerates 30 failures
  (5 min) before a slow backend leaves its Service. Loki's chart
  publishes not-ready addresses anyway. On kind, Tempo once answered neither the gate's all-spans search nor its
  own /ready for 300 s (dry-run gate, first pass); the same check passed on the second pass over the same data.
- **Volumes** are PersistentVolumeClaims of fixed size. kind's local-path provisioner does not enforce the size.
  The GKE sizes in profiles/gke.yaml are provisional for the 1 TB run; compose's named volumes are unbounded.
- **Grafana token.** Besides otb's host-side `viewer_service_account` step, the grafana-token Job keeps a token
  for the same service account (`otb-agent`) in the in-cluster Secret `grafana-mcp-token`, for MCP servers that
  run in the cluster. Both tokens are Viewer.

## Setup Jobs and credentials

- Every credential reaches the cluster as the `otb-credentials` Secret, one key per credential name, which the
  runtime applies before the releases. Compose interpolates the same values from `.otb/secrets/all.env`.
- Parseable's chart reads P_ADDR, P_USERNAME and P_PASSWORD from fixed keys of one Secret, so the parseable-env
  Job copies them out of otb-credentials. Until it has run, the Parseable pod waits in CreateContainerConfigError
  and the kubelet retries.
- Both setup Jobs are plain manifests applied as they are. `up` deletes them before applying, so every `up`
  runs both, and waits for each to complete; a failed Job fails `up`. Each is also deleted 120 s after it
  finishes. Both scripts change nothing when their Secret is already right. A
  credential change reaches parseable-env at the next `up`, but Parseable reads it only when its pod restarts,
  where compose would recreate the container.

## Images on kind

`kind load docker-image` imports `docker save` output, whose manifests and layers docker rewrites, so a pod that
names compose's index digest does not find it and the kubelet pulls inside the node. The kind runs here loaded
each locked image from the registry on the host instead, with the original index, arm64 manifest, config and
layers checked against their digests, then tagged the containerd record with its digest reference. No pod pulled
inside the node (no `Pulling` events). GKE pulls from the registries by digest.
