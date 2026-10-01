# power-kind
A local [kind](https://kind.sigs.k8s.io/) cluster - Prometheus, node-exporter, cAdvisor + synthetic power. Bootstrapped in one command.

This is ment to run local integration tests, test experiment orchestrators, installations, bot to measure anything. It is configured to produce plausible observations, but they are not accuracte.

## Contents

- * One control-plane node labeled `role=system`, one worker labeled `role=sut`.
- `kube-prometheus-stack` in namespace `monitoring`, admin API enabled,
  Prometheus published on a host port.
- node-exporter and cAdvisor, with pod metrics relabelled so `node` carries the
  Kubernetes node name.
- `fake-power/` — a stdlib-only Python exporter deriving **synthetic** power from
  the cluster's own CPU metrics, mirroring [Kepler](https://github.com/sustainable-computing-io/kepler) and [Scaphandre](https://github.com/hubblo-org/scaphandre)
- `process-exporter/` — per-process CPU and memory, grouped by `{{.Comm}}`.

## Bootstrap

```bash
./bootstrap.sh     # idempotent: reuses an existing cluster
./cleanup.sh       # deletes it
```

Needs `docker`, `kind`, `kubectl`, `helm`. Prometheus should deploy on <http://localhost:30090>. To reach it the in "cluster" use `http://kube-prometheus-stack-prometheus.monitoring:9090`.
## Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLUSTER_NAME` | `power-kind` | cluster name; kubectl context is `kind-$CLUSTER_NAME` |
| `PROM_HOSTPORT` | `30090` | host port Prometheus is published on |
| `KUBE_PROM_VERSION` | `82.0.0` | kube-prometheus-stack chart version |
| `ENABLE_FAKE_POWER` | `true` | install the synthetic power exporter |
| `ENABLE_PROCESS_EXPORTER` | `true` | install process-exporter |
| `FAKE_POWER_IDLE_W` | `50` | watts attributed to a fully idle node |
| `FAKE_POWER_MAX_W` | `150` | watts attributed to a fully busy node |
| `FAKE_POWER_SCAPH_NODE_LABEL` | `node` | label carrying the node name on `scaph_host_power_microwatts` |


