# OpenTelemetry Demo on Kubernetes

One demo instance per namespace `otb-demo-<instance>`, from the `open-telemetry/opentelemetry-demo` chart 0.42.3
(sha256 pinned in `install.sh`). The demo collector labels every record `cloud.region = service.instance.id =
<instance>` and exports traces, logs and metrics to the TTRC-Bench gateway, as the Compose overlay in
`capture/demo-overlay/` does.

```sh
deploy/k8s/demo/install.sh eu1 gke                         # in the cluster that runs otb-gateway
OTB_GATEWAY=http://host.docker.internal:4318 OTB_K8S_CONTEXT=kind-otb-w3 deploy/k8s/demo/install.sh k1 kind
deploy/k8s/demo/install.sh --template eu1 gke | python tests/k8s/demo_check.py /dev/stdin --instance eu1
```

## Which demo

The images are the Compose capture's, digest for digest (`capture/demo-source-local.yaml`). That capture ran the
3.1.0 checkout (`dedc017`), whose `.env` pulls `ghcr.io/open-telemetry/demo:latest-<service>`, not
`3.1.0-<service>`. The digests it recorded are `latest-*` builds of 2026-09-24, after the 3.1.0 release images of
2026-09-16. So the label is 3.1.0 (chart appVersion, flag file, `scenarios/faults.yaml`), and the bytes are the
Compose capture's. The values write each image as `latest-<service>@sha256:<digest>`; the tag is informational.
The same `.env` sets `service.version=3.0.0`, and the values keep it, so a question checked on a Compose corpus
reads the same attributes here.

Each service's environment and ports are the Compose capture's (`capture/demo-compose-gke.json`, a snapshot of
`docker compose config` of `compose.yaml`, `compose.otb.yaml` and `compose.gke.yaml`). The chart's defaults differ:
every service on port 8080, and Kafka, accounting, fraud-detection, the agent, the chatbot, MCP and OpAMP running.
`tests/k8s/demo_check.py` fails on any difference. Regenerate the snapshot with
`python tests/k8s/demo_check.py --snapshot <compose config json>`.

## What differs from the Compose capture

| What | Compose | Kubernetes | Why |
|---|---|---|---|
| flagd-ui | its own container, flag file on a bind mount | sidecar of the flagd pod, flag file on an `emptyDir` | the chart's layout. A new flagd pod reverts every flag write, so flagd is never a decoy target. |
| `FLAGD_UI_HOST` (frontend-proxy) | `flagd-ui` | `flagd` | the sidecar is reached through the flagd Service |
| container metrics | `docker_stats` | `kubeletstats`, own pipeline, filtered to the namespace | no Docker socket. See below. |
| `host_metrics` | host root filesystem, every scraper | `cpu`, `load`, `memory`, `paging` only | no host root filesystem without a hostPath mount. These read node-wide `/proc` files. |
| `resource_detection` | `env, docker, system` | `env, system` | no Docker socket |
| startup order | `depends_on` with health checks | init containers wait for valkey-cart, astronomy-db, flagd | the chart's mechanism, with busybox pinned |
| CPU | no limit | requests = limits on every container (Guaranteed) | `profile-<name>.yaml` |
| memory (gke) | Compose limits | the same limits | memory faults hit the same OOM limit |
| memory (kind) | Compose limits | load-generator, recommendation, flagd-ui, collector cut | fits a laptop. Capture memory faults on gke only. |
| collector queue | named volume | PVC `otb-queue`, Deployment strategy `Recreate` | a ReadWriteOnce volume holds one pod at a time |
| collector Deployment label | none | `app.kubernetes.io/version: 0.160.0` (collector chart) | cosmetic. The image is contrib 0.159.0 by digest. |

## Container metrics (kubeletstats)

The `metrics/kubeletstats` pipeline reads this node's kubelet (`https://$K8S_NODE_IP:10250`, service-account token,
ClusterRole `otb-demo-<instance>-kubeletstats` with `nodes/stats get` only) every 10 s, for the `container` and
`pod` metric groups. `filter/otb_namespace` drops every pod outside `otb-demo-<instance>`, then the same instance
labels and batching apply.

The names below are the receiver's defaults for these two groups (contrib `receiver/kubeletstatsreceiver`
documentation), plus the two this config enables. They are not yet confirmed from a corpus: the kind smoke
capture that records them (`tests/k8s/test_demo_capture.py`, evidence `tests/k8s/artifacts/demo_capture.json`)
has not run to completion. Rerun it and replace this paragraph with the observed list.

| Group | Metrics | Resource attributes |
|---|---|---|
| container | `container.cpu.time`, `container.cpu.usage` (cores), `container.memory.available`, `container.memory.major_page_faults`, `container.memory.page_faults`, `container.memory.rss`, `container.memory.usage`, `container.memory.working_set` (bytes), `container.filesystem.available`, `container.filesystem.capacity`, `container.filesystem.usage`, `container.uptime` | `k8s.pod.uid`, `k8s.pod.name`, `k8s.namespace.name`, `k8s.container.name` |
| pod | `k8s.pod.cpu.time`, `k8s.pod.cpu.usage`, `k8s.pod.memory.*` (the same six), `k8s.pod.filesystem.*` (the same three), `k8s.pod.network.io`, `k8s.pod.network.errors` (point attributes `interface`, `direction`), `k8s.pod.uptime` | `k8s.pod.uid`, `k8s.pod.name`, `k8s.namespace.name` |

Every point also carries `cloud.region` and `service.instance.id` (the instance), and the `env`/`system`
resource-detection attributes (`host.name`, `os.type`, ...). None carries `service.name`.

`k8s.container.name` equals the demo service name, as Compose's `container.name` does: the chart names each
Deployment's container after its component (`cart`, `frontend-proxy`), and the flagd pod holds `flagd` and the
sidecar `flagd-ui`. The one exception is the collector, whose container is `opentelemetry-collector` (Compose:
`otel-collector`). The receiver's default metrics are all kept (only `container.uptime` and `k8s.pod.uptime` are
added), and these points carry `cloud.region` and `service.instance.id` from `resource/otb_instance` like every
other record. The collector is contrib 0.159.0, the Compose capture's digest. `container.cpu.usage` is in cores, where Compose's
`container.cpu.utilization` is a percentage. `k8s.pod.name` changes on a restart: a rollout restart shows as a
second pod for the same `k8s.container.name`, overlapping the first for a few seconds.

## Capturing

In the cluster, `otb capture <scenario> --decoy-driver k8s` derives each instance's URLs from its namespace:
flagd-ui `http://flagd.<ns>.svc:4000`, OFREP `http://flagd.<ns>.svc:8016`, Locust
`http://load-generator.<ns>.svc:8089`. `benign_restart` runs `kubectl rollout restart deployment/<target> -n <ns>`
and its end runs `kubectl rollout status`, so a restart that never completes fails its schedule entry. Preflight
checks that each target Deployment exists and refuses flagd.

From a laptop against kind (OrbStack or Docker Desktop resolve `host.docker.internal` inside kind's node, and
CoreDNS forwards to it, so pods reach the host's gateway):

```sh
uv run otb capture scenarios/k8s-smoke.yaml --out corpora/k8s-smoke --listen 0.0.0.0:4318 \
  --decoy-driver k8s --kube-context kind-otb-w3 \
  --flagd-ui http://127.0.0.1:14000 --ofrep http://127.0.0.1:18016 --locust http://127.0.0.1:18089 &
OTB_GATEWAY=http://host.docker.internal:4318 OTB_K8S_CONTEXT=kind-otb-w3 deploy/k8s/demo/install.sh k1 kind
kubectl --context kind-otb-w3 -n otb-demo-k1 port-forward svc/flagd 14000:4000 18016:8016 &
kubectl --context kind-otb-w3 -n otb-demo-k1 port-forward svc/load-generator 18089:8089 &
```

The node pulls the pinned digests itself; at about 1 MB/s the first install outran the Deployments' 600 s
progress deadline, so `install.sh` reported failure while the pods were still pulling. On a laptop, expect
`kind load docker-image` not to help: it imports a re-serialised image whose manifest digest differs from the
pin, so the kubelet still pulls (unverified; check with `crictl images --digests` in the node).

Start the capture first: the gateway listens before the collector exports, and preflight waits (`--wait-demo`)
for flagd. `tests/k8s/test_demo_capture.py` runs exactly this.

## Provenance

`uv run python deploy/k8s/demo/source.py <instance>... > capture/demo-source-k8s.yaml` writes the `--source` file
from the running pods' resolved image digests. It refuses when a container runs a digest other than the one its
Deployment pins, or when instances differ.
