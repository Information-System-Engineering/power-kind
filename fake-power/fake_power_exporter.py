#!/usr/bin/env python3
"""power-kind-fake-power — synthetic power exporter for a local kind testbed.

NOT A MEASUREMENT. Every number this emits is fabricated. It exists so that
power PromQL, joins and report writers have series of the right *shape* to run
against on a cluster that has no RAPL. Every series carries synthetic="true",
so consumers can refuse to treat it as real.

It emits the Kepler metric family plus a Scaphandre-shaped host series. kind
nodes are containers with no RAPL, no IPMI and no Redfish, and Kepler itself
is amd64-only; that is why this file exists.

The model is linear in CPU, so the numbers at least move the way power moves:

    u          = busy_cores / cores                     (node CPU utilisation)
    P_node     = P_idle + (P_max - P_idle) * u
    P_pod      = (P_max - P_idle) * pod_cores / cores    (dynamic share only)
    P_residual = P_node - P_idle - sum(P_pod)            (>= 0: platform + unattributed)

CPU comes from the cluster's own cAdvisor and node-exporter series, so load
actually shows up in the fake power. The residual is non-zero by construction:
platform processes burn CPU that no pod cgroup accounts for.
"""

import hashlib
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://kube-prometheus-stack-prometheus.monitoring:9090")
P_IDLE_W = float(os.environ.get("FAKE_POWER_IDLE_W", "50"))
P_MAX_W = float(os.environ.get("FAKE_POWER_MAX_W", "150"))
RATE_WINDOW = os.environ.get("FAKE_POWER_RATE_WINDOW", "1m")
LISTEN_PORT = int(os.environ.get("FAKE_POWER_PORT", "9260"))

# Kepler splits CPU power across RAPL zones. package dominates; dram is the rest.
# ponytail: a fixed split, not a model. It only has to sum to 1 and stay stable.
ZONES = {"package": 0.85, "dram": 0.15}

# Scaphandre's series must NOT come from the same model as Kepler's, or the two
# agree perfectly, which no real pair of RAPL-derived estimates does. A different
# idle offset, a different gain, and per-sample noise — read from the environment
# at call time so a test can drive the gap out of band on demand.
SCAPH_IDLE_W = float(os.environ.get("FAKE_POWER_SCAPH_IDLE_W", "54"))
SCAPH_NOISE_W = float(os.environ.get("FAKE_POWER_SCAPH_NOISE_W", "0.8"))

_joules = {}       # series key -> accumulated joules
_last_seen = None  # timestamp of the previous refresh


def query(expr):
    """One Prometheus instant query. Raises on any failure — see the handler below."""
    url = PROMETHEUS_URL.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = json.load(resp)
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus rejected query: {expr}")
    return [(r["metric"], float(r["value"][1])) for r in body["data"]["result"]]


def pod_uid(namespace, pod):
    """A stable stand-in for the pod UID Kepler reports."""
    return hashlib.md5(f"{namespace}/{pod}".encode()).hexdigest()


def sample():
    """Read CPU from the cluster and turn it into the power model's inputs."""
    cores = {m["node"]: v for m, v in query('count by (node) (node_cpu_seconds_total{mode="idle"})')}
    busy = {m["node"]: v for m, v in
            query(f'sum by (node) (rate(node_cpu_seconds_total{{mode!="idle"}}[{RATE_WINDOW}]))')}
    pods = [(m["node"], m["namespace"], m["pod"], v) for m, v in
            query(f'sum by (node,namespace,pod) (rate(container_cpu_usage_seconds_total{{pod!=""}}[{RATE_WINDOW}]))')
            if m.get("node") and m.get("pod")]
    return cores, busy, pods


def model(cores, busy, pods):
    """The linear model. Pure — this is what --selftest exercises.

    Returns (node_watts, pod_watts) where pod_watts is keyed by (node, ns, pod).
    Nodes with no core count are skipped entirely: without node-exporter there is
    no denominator, and inventing one would be a number pretending to be a model.
    """
    dynamic = P_MAX_W - P_IDLE_W
    node_watts = {}
    for node, n_cores in cores.items():
        u = min(1.0, busy.get(node, 0.0) / n_cores) if n_cores else 0.0
        node_watts[node] = P_IDLE_W + dynamic * u

    pod_watts = {}
    for node, namespace, pod, pod_cores in pods:
        if node not in cores or not cores[node]:
            continue
        pod_watts[(node, namespace, pod)] = dynamic * min(1.0, pod_cores / cores[node])
    return node_watts, pod_watts


def scaph_watts(node_w):
    """A second, deliberately divergent estimate of the same node's power.

    Two RAPL-derived models disagree by an amount that varies with load and
    hardware state; this reproduces that shape. The magnitude is meaningless —
    only the fact that it moves, and can be forced large, is load-bearing.
    """
    gain = float(os.environ.get("FAKE_POWER_SCAPH_GAIN", "1.06"))
    dynamic = max(0.0, node_w - P_IDLE_W)
    return max(0.0, SCAPH_IDLE_W + dynamic * gain + random.gauss(0.0, SCAPH_NOISE_W))


def integrate(key, watts, elapsed_s):
    """Accumulate joules so the counters actually climb and increase() has something to bite."""
    _joules[key] = _joules.get(key, 0.0) + watts * elapsed_s
    return _joules[key]


def render():
    global _last_seen
    now = time.time()
    elapsed = 0.0 if _last_seen is None else max(0.0, now - _last_seen)
    _last_seen = now

    node_watts, pod_watts = model(*sample())

    out = [
        "# HELP kepler_node_cpu_watts SYNTHETIC node CPU power (power-kind-fake-power, not a measurement)",
        "# TYPE kepler_node_cpu_watts gauge",
        "# HELP kepler_node_cpu_joules_total SYNTHETIC node CPU energy",
        "# TYPE kepler_node_cpu_joules_total counter",
        "# HELP kepler_pod_cpu_watts SYNTHETIC per-pod CPU power",
        "# TYPE kepler_pod_cpu_watts gauge",
        "# HELP kepler_pod_cpu_joules_total SYNTHETIC per-pod CPU energy",
        "# TYPE kepler_pod_cpu_joules_total counter",
        "# HELP scaph_host_power_microwatts SYNTHETIC host power",
        "# TYPE scaph_host_power_microwatts gauge",
    ]

    for node, watts in sorted(node_watts.items()):
        scaph = scaph_watts(watts)
        # Consumers differ on this label: some query `node`, some `node_name`.
        # That difference was the only real fork between the two copies this
        # file replaces, so it is a knob. Kepler's own series always use
        # node_name, whatever this is set to.
        label = os.environ.get("FAKE_POWER_SCAPH_NODE_LABEL", "node")
        out.append(f'scaph_host_power_microwatts{{{label}="{node}",synthetic="true"}} {scaph * 1e6:.0f}')
        for zone, frac in ZONES.items():
            labels = f'node_name="{node}",zone="{zone}",synthetic="true"'
            joules = integrate(("node", node, zone), watts * frac, elapsed)
            out.append(f"kepler_node_cpu_watts{{{labels}}} {watts * frac:.4f}")
            out.append(f"kepler_node_cpu_joules_total{{{labels}}} {joules:.4f}")

    for (node, namespace, pod), watts in sorted(pod_watts.items()):
        uid = pod_uid(namespace, pod)
        for zone, frac in ZONES.items():
            labels = (f'pod_id="{uid}",pod_name="{pod}",pod_namespace="{namespace}",'
                      f'node_name="{node}",zone="{zone}",synthetic="true"')
            joules = integrate(("pod", node, namespace, pod, zone), watts * frac, elapsed)
            out.append(f"kepler_pod_cpu_watts{{{labels}}} {watts * frac:.4f}")
            out.append(f"kepler_pod_cpu_joules_total{{{labels}}} {joules:.4f}")

    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        try:
            body = render().encode()
        except Exception as exc:
            # Fail loudly. Emitting zeros because Prometheus is unreachable would be
            # the exact failure this whole fixture exists to make impossible.
            self.send_error(503, f"cannot build synthetic series: {exc}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def selftest():
    cores = {"n1": 10.0, "n2": 4.0}
    busy = {"n1": 5.0, "n2": 0.0}
    pods = [("n1", "ns-a", "pod-a", 2.0), ("n1", "ns-a", "pod-b", 1.0),
            ("n1", "kube-system", "kube-proxy", 0.5), ("nX", "other", "orphan", 9.0)]
    node_watts, pod_watts = model(cores, busy, pods)
    dynamic = P_MAX_W - P_IDLE_W

    assert node_watts["n1"] == P_IDLE_W + dynamic * 0.5, node_watts
    assert node_watts["n2"] == P_IDLE_W, "an idle node draws idle power, not zero"
    assert ("nX", "other", "orphan") not in pod_watts, "a node with no core count is skipped"
    assert pod_watts[("n1", "ns-a", "pod-a")] == dynamic * 0.2

    # Pods never exceed node dynamic power, and the residual (platform +
    # unattributed) stays positive because kubelet and containerd burn CPU that
    # no pod cgroup owns.
    attributed = sum(w for (n, _, _), w in pod_watts.items() if n == "n1")
    residual = node_watts["n1"] - P_IDLE_W - attributed
    assert residual > 0, f"expected a positive residual, got {residual}"

    # Counters must climb, or increase() has nothing to measure.
    assert integrate(("t",), 10.0, 2.0) == 20.0
    assert integrate(("t",), 10.0, 2.0) == 40.0

    # A saturated pod cannot draw more than the whole dynamic envelope.
    _, clipped = model({"n": 2.0}, {"n": 2.0}, [("n", "ns", "hog", 99.0)])
    assert clipped[("n", "ns", "hog")] == dynamic

    # The two estimates must not be the same series, or a consumer comparing
    # them sees perfect agreement, which no real pair of estimates shows.
    node_w = 100.0
    gaps = {scaph_watts(node_w) - node_w for _ in range(20)}
    assert all(g != 0 for g in gaps), "scaphandre model must not equal the kepler model"
    assert len(gaps) > 1, "the gap must vary over time, not be a constant offset"

    # and it must be drivable outside any plausible band on demand
    import os as _os
    _os.environ["FAKE_POWER_SCAPH_GAIN"] = "2.0"
    assert scaph_watts(node_w) > node_w * 1.5, "the gap must be forceable large"
    del _os.environ["FAKE_POWER_SCAPH_GAIN"]

    # but never negative, even under a pathological knob value.
    _os.environ["FAKE_POWER_SCAPH_GAIN"] = "-10"
    assert scaph_watts(150.0) >= 0.0, "scaph_watts must not go negative"
    del _os.environ["FAKE_POWER_SCAPH_GAIN"]


    # The one real fork between the two copies this file consolidates: one
    # consumer queried scaph on `node`, the other on `node_name`. A knob, not a fork.
    import re
    global _last_seen
    saved_sample = globals()["sample"]
    globals()["sample"] = lambda: (cores, busy, pods)
    try:
        for want in ("node_name", "node"):
            os.environ["FAKE_POWER_SCAPH_NODE_LABEL"] = want
            _last_seen = None
            body = render()
            scaph = next(l for l in body.splitlines()
                         if l.startswith("scaph_host_power_microwatts{"))
            assert re.search(rf'[{{,]{want}="n1"', scaph), (want, scaph)
            # Kepler's own series always use node_name, whatever scaph uses.
            kepler = next(l for l in body.splitlines()
                          if l.startswith("kepler_node_cpu_watts{"))
            assert 'node_name="n1"' in kepler, kepler
    finally:
        globals()["sample"] = saved_sample
        os.environ.pop("FAKE_POWER_SCAPH_NODE_LABEL", None)

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print(f"power-kind-fake-power on :{LISTEN_PORT} — SYNTHETIC, not a measurement", flush=True)
        HTTPServer(("", LISTEN_PORT), Handler).serve_forever()
