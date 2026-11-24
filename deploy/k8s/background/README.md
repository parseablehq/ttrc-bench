# Background volume (namespace otb-bg)

otel-benchmark's `otel-loggen` and `otel-loadgen` with `-field-profile=generic`, one collector, and an export to
the TTRC-Bench gateway. Background records carry `cloud.region = service.instance.id = bg`. Questions are scoped to a
demo instance, so these records add bytes to the corpus and to every backend, and never enter an answer.

| File | What |
|---|---|
| `namespace.yaml` | `otb-bg`, Pod Security `restricted` |
| `build.yaml` | the pinned otel-benchmark commit, and the init container's `build.sh` |
| `rate.yaml` | the rate knobs, one ConfigMap per generator |
| `loggen.yaml`, `loadgen.yaml` | one Deployment each: build init container, then the binary in distroless |
| `collector.yaml` | contrib 0.161.0: OTLP in, upsert `bg`, OTLP/HTTP JSON (gzip) out to `otb-gateway.otb` |
| `networkpolicy.yaml` | default-deny egress, then DNS, generators to collector and 443, collector to `otb` |

Every container, init containers included, has requests equal to limits, so every pod is Guaranteed QoS.

## Why the generators are built in the pod

No image registry is involved, as in otel-benchmark's `k8s/heavy/heavy-ingest.yaml.tmpl`. The init container runs
the golang 1.22.12 image pinned by digest. It fetches one pinned commit, and git verifies that commit by its hash.
`go.sum` pins every module. The build uses `-trimpath`, `GOTOOLCHAIN=local` and `CGO_ENABLED=0`, and the binary's
sha256 goes to the init container's log. The repository is private today. Create the Secret `otb-bg-github`
(key `token`) until it is public; on kind, `deploy/k8s/kind/bg-source.sh` serves a local clone instead. Each
generator keeps its module cache, build cache and the binary for the pinned commit on a 4 GiB claim
(`otb-bg-<generator>-cache`), so only the first start of a new commit pays for the fetch and the build; the
claims stay when `otb capture` removes the generators. The cost is egress on 443 from the generator pods.

## Rate

`otb scenario bytes` counts the scenario's `background.mb_per_s` for the whole duration, in OTLP/JSON bytes as the
gateway receives them. `rate.yaml` states the two equations that turn that into generator settings. Its
`otb.bg/json-bytes-per-record` annotations are measured gateway bytes per record. Change the scenario, then
change the knobs. `tests/e2e/test_background_manifests.py` fails until the two agree within 2%.

Metrics have a fixed series set: 26 series per pod, so `PODS=240` gives 6240 series. At 12,048 points/s each
series gets about 1.9 samples/s. Logs come from 16 services with 3 pods each (48 workloads). Severity is INFO 80%,
WARN 12%, ERROR 6%, DEBUG 2%.

Start the generators when the capture starts, and delete them when it ends. Before the gateway's archive exists,
batches older than its `--stale-s` are quarantined. The collector's queue is in memory, so an outage of more than
about 5 minutes drops background batches. Dropped batches show in `otelcol_exporter_send_failed_*` on :8888.

## Sizing

Measured by `OTB_DOCKER_TESTS=1 OTB_BG_SRC=<otel-benchmark clone> uv run pytest tests/e2e/test_background_local.py`
on a 12-core Apple M-series laptop (OrbStack): a 120 s window at the tb-hybrid rate, every container at these
manifests' limits. Evidence is in `tests/e2e/artifacts/background_local.json` and `.timing.json`.

| Container | Limit (cores) | Used (cores) | Cores per MB/s |
|---|---|---|---|
| loggen | 0.5 | 0.057 at 6.78 MB/s of logs | 0.008 |
| loadgen | 0.5 | 0.034 at 4.55 MB/s of metrics | 0.007 |
| collector (OTLP to JSON, gzip) | 1 | 0.162 at 11.33 MB/s | 0.014 |
| gateway (runner pod, for reference) | none | 0.105 at 11.33 MB/s | 0.009 |

CPU is the Docker cgroup's `cpu_usage.total_usage` over the window, and the gateway's own rusage. `docker stats`
at the window's midpoint agrees (collector 12.7%, loggen 7.0%, loadgen 3.0%). Gateway bytes per record were
1055.2 for logs and 377.4 for metric points, the values in `rate.yaml`. Two builds of the pinned commit gave the
same binary sha256. An Apple performance core does more work than a cloud vCPU. Budget about 3x these figures
on GKE: about 0.03 cores per MB/s for each generator and 0.04 for the collector. At 11.33 MB/s that is about
0.8 cores in all, well inside the limits above (the limits were halved after the kind integration run, where
the earlier 2-core build and collector requests left no CPU for the second generator beside a demo and a
stack). The build init container has 1 core and 1 GiB. Memory is not the constraint at this rate.
