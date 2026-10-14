"""Bounded read-only Kubernetes/Prometheus collector.

CPU is a five-minute mean in cores; memory is working-set bytes. PVC filesystem
usage is never inferred from Longhorn physical allocation or requested storage.
Missing, old and partially measured data remain distinguishable from real zero.
"""
import copy
import json
import math
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

from src.config import settings
from src.services.helm import KubernetesService, validate_namespace
from src.services.storage_quota import parse_quantity

CACHE_SECONDS = 30
MAX_CACHE_KEYS = 64
MAX_INFLIGHT = 4
MAX_SAMPLE_AGE = 120
CPU_WINDOW = 300
KINDS = "pods,deployments,statefulsets,daemonsets,replicasets,persistentvolumeclaims"
CONTROLLERS = {"Deployment", "StatefulSet", "DaemonSet", "VirtualMachine"}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch is not None else None


def number(value):
    parsed = parse_quantity(value)
    if parsed is None or not parsed.is_finite() or parsed < 0:
        return None
    return float(parsed)


def validate_scope(namespace):
    if len(namespace) > 63 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace):
        raise ValueError("Invalid namespace")
    validate_namespace(namespace)
    # Production's prefix is also a textual prefix of staging; keep both scopes
    # separate even when the service account can list either namespace.
    if settings.namespace_prefix == "paas-ws-" and namespace.startswith("paas-ws-stg-"):
        raise ValueError("Staging namespace belongs to the staging operator")


def release_label(obj):
    meta = obj.get("metadata", {})
    labels = meta.get("labels", {})
    return (labels.get("app.kubernetes.io/instance")
            or labels.get("paas.woowtech.io/release-name")
            or meta.get("annotations", {}).get("meta.helm.sh/release-name"))


def pod_spec(obj):
    if obj.get("kind") == "Pod":
        return obj.get("spec", {})
    return obj.get("spec", {}).get("template", {}).get("spec", {})


def containers(spec):
    # Restartable init containers are sidecars and consume resources throughout
    # the workload lifetime. Ordinary init containers are not steady-state usage.
    return (spec.get("containers", []) + [c for c in spec.get("initContainers", [])
                                         if c.get("restartPolicy") == "Always"])


def metric_shell(unit, expected=0):
    return {"usage": None, "request": None, "limit": None,
            "limit_status": "unknown", "unit": unit, "status": "unavailable",
            "sampled_at": None, "coverage": {"observed": 0, "expected": expected}}


def resources(entries, resource):
    """Sum actual admitted containers; missing limits mean unbounded, not zero."""
    if not entries:
        return None, None, "unknown"
    requests, limits, unbounded, invalid = [], [], False, False
    for container, multiplier in entries:
        res = container.get("resources", {})
        req = number(res.get("requests", {}).get(resource, "0"))
        raw_limit = res.get("limits", {}).get(resource)
        lim = number(raw_limit)
        unbounded |= raw_limit is None
        invalid |= raw_limit is not None and lim is None
        requests.append(None if req is None else req * multiplier)
        limits.append(None if lim is None else lim * multiplier)
    requested = sum(requests) if all(v is not None for v in requests) else None
    if invalid:
        return requested, None, "unknown"
    if unbounded:
        return requested, None, "unbounded"
    return requested, sum(limits), "bounded"


def configured_replicas(obj):
    if obj.get("kind") == "DaemonSet":
        # An unobserved DaemonSet size is unknown, never silently one replica.
        return obj.get("status", {}).get("desiredNumberScheduled")
    return max(1, obj.get("spec", {}).get("replicas", 1))


def guest_resources(virtual_machines, key):
    capacities, requests = [], []
    for vm in virtual_machines:
        domain = pod_spec(vm).get("domain", {})
        res = domain.get("resources", {})
        req = number(res.get("requests", {}).get(key))
        if key == "cpu":
            topology = domain.get("cpu", {})
            if topology:
                parts = [number(topology.get(k, 1)) for k in ("cores", "sockets", "threads")]
                capacity = math.prod(parts) if all(v is not None and v > 0 for v in parts) else None
            else:
                configured = number(res.get("limits", {}).get("cpu")) or req or 1
                capacity = max(1, math.ceil(configured))
        else:
            capacity = number(domain.get("memory", {}).get("guest")) or req or number(res.get("limits", {}).get("memory"))
        capacities.append(capacity)
        requests.append(req)
    capacity = sum(capacities) if capacities and all(v is not None for v in capacities) else None
    request = sum(requests) if requests and all(v is not None for v in requests) else None
    return request, capacity, "bounded" if capacity is not None else "unknown"


def samples(rows, identity):
    """Prometheus timestamp() query values are the *source* sample timestamps."""
    result = {}
    for row in rows or []:
        labels = row.get("metric", {})
        key = tuple(labels.get(k, "") for k in identity)
        try:
            value = float(row["value"][1])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if all(key) and math.isfinite(value) and value >= 0:
            result[key] = value
    return result


def measurement(keys, values, timestamps, now):
    present = [(values[k], timestamps[k]) for k in keys if k in values and k in timestamps]
    if not present:
        return None, None, "unavailable", 0
    stale = any(now - stamp > MAX_SAMPLE_AGE or stamp > now + 10 for _, stamp in present)
    state = "stale" if stale else ("ok" if len(present) == len(keys) else "partial")
    return sum(v for v, _ in present), iso(min(t for _, t in present)), state, len(present)


class ResourceCollector:
    def __init__(self, k8s=None, clock=time.time):
        self.k8s = k8s or KubernetesService()
        self.clock = clock
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        self._inflight = {}
        self._http = None
        self._http_lock = threading.Lock()

    def _cached_result(self, result):
        result = copy.deepcopy(result)
        now = self.clock()
        for observation in result.get("services", {}).values():
            for key in ("cpu", "memory", "storage"):
                item = observation.get(key, {})
                stamp = item.get("sampled_at")
                if stamp and item.get("status") in {"ok", "partial"}:
                    if now - datetime.fromisoformat(stamp).timestamp() > MAX_SAMPLE_AGE:
                        item["status"] = "stale"
                        if observation.get("status") == "ok":
                            observation["status"] = "partial"
        return result

    def collect(self, namespace, release_names):
        validate_scope(namespace)
        key = (namespace, tuple(sorted(set(release_names))))
        with self._lock:
            cached = self._cache.get(key)
            if cached and self.clock() - cached[0] < CACHE_SECONDS:
                self._cache.move_to_end(key)
                return self._cached_result(cached[1])
            event = self._inflight.get(key)
            leader = event is None
            if leader:
                if len(self._inflight) >= MAX_INFLIGHT:
                    raise TimeoutError("Collector busy")
                event = self._inflight[key] = threading.Event()
        if not leader:
            if not event.wait(9):
                raise TimeoutError("Collector busy")
            with self._lock:
                cached = self._cache.get(key)
                if cached and self.clock() - cached[0] < CACHE_SECONDS:
                    return self._cached_result(cached[1])
            raise TimeoutError("Collector failed")
        try:
            result = self._collect(namespace, key[1])
            with self._lock:
                self._cache[key] = (self.clock(), result)
                self._cache.move_to_end(key)
                while len(self._cache) > MAX_CACHE_KEYS:
                    self._cache.popitem(last=False)
            return copy.deepcopy(result)
        finally:
            with self._lock:
                self._inflight.pop(key, None)
                event.set()

    def _inventory(self, namespace, kinds=KINDS):
        # In the deployed in-cluster kubectl, --request-timeout disables the
        # service-account fallback and makes discovery try localhost:8080.
        # The subprocess timeout still bounds each namespaced read to 5s.
        result = self.k8s._run_command(
            ["get", kinds, "--namespace", namespace, "--output", "json"],
            timeout=5)
        return json.loads(result.stdout).get("items", [])

    def _query(self, expression):
        if not settings.resource_prometheus_url:
            raise ValueError("Metrics disabled")
        # Share the connection pool across concurrent queries and refreshes.
        # The convenience httpx.get creates eight clients/SSL contexts per
        # snapshot and can exhaust the UI's budget before an HTTP timeout starts.
        with self._http_lock:
            if self._http is None:
                self._http = httpx.Client(
                    timeout=httpx.Timeout(3, connect=1, pool=1),
                    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                    follow_redirects=False, trust_env=False)
        response = self._http.get(
            settings.resource_prometheus_url.rstrip("/") + "/api/v1/query",
            params={"query": expression, "timeout": "2s"})
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "success" or data.get("data", {}).get("resultType") != "vector":
            raise ValueError("Invalid metric response")
        return data["data"]["result"]

    def _collect(self, namespace, names):
        warnings, inventory_ok = [], True
        with ThreadPoolExecutor(max_workers=2) as pool:
            inv = pool.submit(self._inventory, namespace)
            vm = pool.submit(self._inventory, namespace, "virtualmachines.kubevirt.io")
            try:
                items = inv.result()
            except Exception:
                items, inventory_ok = [], False
                warnings.append("inventory_unavailable")
            try:
                items += vm.result()
            except Exception:
                # KubeVirt is optional. Pod labels still prevent QEMU metrics
                # from being presented as guest CPU/RAM when CR access fails.
                pass
        owned = self._ownership(items)
        grouped = {name: [o for o in items if owned.get(o.get("metadata", {}).get("uid")) == name
                          or release_label(o) == name] for name in names}
        pod_names = sorted({o["metadata"]["name"] for group in grouped.values() for o in group
                            if o.get("kind") == "Pod" and o.get("status", {}).get("phase") not in {"Succeeded", "Failed"}})
        claims = {name: self._claims(group, items) for name, group in grouped.items()}
        claim_names = sorted(set().union(*claims.values()))
        queries = self._expressions(namespace, pod_names, claim_names)
        raw = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {key: pool.submit(self._query, expr) for key, expr in queries.items()}
            for key, future in futures.items():
                try:
                    raw[key] = future.result()
                except Exception:
                    raw[key] = []
                    warnings.append("metrics_" + key + "_unavailable")
        now = self.clock()
        metrics = {key: samples(rows, ("persistentvolumeclaim",) if key.startswith("storage") else ("pod", "container"))
                   for key, rows in raw.items()}
        pvcs = {o["metadata"]["name"]: o for o in items if o.get("kind") == "PersistentVolumeClaim"}
        return {"observed_at": iso(now), "refresh_seconds": CACHE_SECONDS,
                "services": {name: self._observation(grouped[name], claims[name], pvcs, metrics,
                                                     now, warnings, inventory_ok) for name in names}}

    @staticmethod
    def _ownership(items):
        owners = {o.get("metadata", {}).get("uid"): release_label(o) for o in items if release_label(o)}
        for _ in range(4):
            for obj in items:
                meta = obj.get("metadata", {})
                if meta.get("uid") not in owners:
                    found = {owners[r["uid"]] for r in meta.get("ownerReferences", []) if r.get("uid") in owners}
                    if len(found) == 1:
                        owners[meta.get("uid")] = found.pop()
        return owners

    @staticmethod
    def _claims(group, items):
        claims = {o["metadata"]["name"] for o in group if o.get("kind") == "PersistentVolumeClaim"}
        for obj in group:
            if obj.get("kind") == "Pod" and obj.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
                continue
            spec = pod_spec(obj)
            for vol in spec.get("volumes", []):
                name = vol.get("persistentVolumeClaim", {}).get("claimName") or vol.get("dataVolume", {}).get("name")
                if name:
                    claims.add(name)
            if obj.get("kind") == "StatefulSet":
                # Retained StatefulSet PVCs often have no release label and no
                # ownerReference; match only the declared claim template/ordinal.
                for template in obj.get("spec", {}).get("volumeClaimTemplates", []):
                    prefix = template["metadata"]["name"] + "-" + obj["metadata"]["name"] + "-"
                    claims.update(o["metadata"]["name"] for o in items if o.get("kind") == "PersistentVolumeClaim"
                                  and o["metadata"]["name"].startswith(prefix)
                                  and o["metadata"]["name"][len(prefix):].isdigit())
        return claims

    @staticmethod
    def _expressions(namespace, pod_names, claim_names):
        expressions = {}
        # Exact inventory-derived names, regexp-escaped then JSON-quoted: neither
        # client-supplied PromQL nor a broad all-tenant query is accepted.
        if pod_names:
            selector = 'namespace=' + json.dumps(namespace) + ',pod=~' + json.dumps("|".join(re.escape(n) for n in pod_names)) + ',container!="",container!="POD"'
            cpu = "container_cpu_usage_seconds_total{" + selector + "}"
            memory = "container_memory_working_set_bytes{" + selector + "}"
            expressions.update({
                # Select the newest counter series before reducing labels. A
                # restarted container's historical rate must not win max().
                "cpu": "max by(pod,container)(rate(" + cpu + f"[{CPU_WINDOW}s]) and topk by(pod,container)(1,timestamp(" + cpu + ")))",
                "cpu_time": "max by(pod,container)(timestamp(" + cpu + "))",
                "memory": "max by(pod,container)(" + memory + " and topk by(pod,container)(1,timestamp(" + memory + ")))",
                "memory_time": "max by(pod,container)(timestamp(" + memory + "))",
            })
        if claim_names:
            selector = 'namespace=' + json.dumps(namespace) + ',persistentvolumeclaim=~' + json.dumps("|".join(re.escape(n) for n in claim_names))
            for key, metric in (("storage", "used"), ("storage_capacity", "capacity")):
                expr = "kubelet_volume_stats_" + metric + "_bytes{" + selector + "}"
                expressions[key] = "max by(persistentvolumeclaim)(" + expr + " and topk by(persistentvolumeclaim)(1,timestamp(" + expr + ")))"
                expressions[key + "_time"] = "max by(persistentvolumeclaim)(timestamp(" + expr + "))"
        return expressions

    @staticmethod
    def _observation(group, claim_names, pvcs, metrics, now, warnings, inventory_ok):
        pods = [o for o in group if o.get("kind") == "Pod" and o.get("status", {}).get("phase") not in {"Succeeded", "Failed"}]
        controllers = [o for o in group if o.get("kind") in CONTROLLERS]
        vm = any(o.get("kind") == "VirtualMachine" or "kubevirt.io" in o.get("metadata", {}).get("labels", {}) for o in controllers + pods)
        stopped = not pods and bool(controllers) and all(
            o.get("spec", {}).get("replicas") == 0 if o.get("kind") != "VirtualMachine"
            else o.get("spec", {}).get("running") is False or o.get("spec", {}).get("runStrategy") == "Halted"
            for o in controllers)
        source = pods or controllers
        unknown_replicas = not pods and any(configured_replicas(o) is None for o in controllers)
        entries = [] if unknown_replicas else [(c, 1 if pods else configured_replicas(o))
                                               for o in source for c in containers(pod_spec(o))]
        expected = [(p["metadata"]["name"], c["name"]) for p in pods for c in containers(pod_spec(p))]
        # StatefulSets reuse pod names. Samples from before this pod/container
        # started belong to its predecessor and are missing, never current zero.
        started = {}
        for pod in pods:
            statuses = {c["name"]: c for c in (
                pod.get("status", {}).get("containerStatuses", [])
                + pod.get("status", {}).get("initContainerStatuses", []))}
            for c in containers(pod_spec(pod)):
                stamp = statuses.get(c["name"], {}).get("state", {}).get("running", {}).get("startedAt") or pod.get("metadata", {}).get("creationTimestamp")
                if stamp:
                    try:
                        started[(pod["metadata"]["name"], c["name"])] = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                    except (TypeError, ValueError):
                        pass
        output = {"status": "unavailable", "workload_type": "vm" if vm else "container" if pods or controllers else "unknown",
                  "resource_scope": "vm_guest" if vm else "live_pods" if pods else "workload_templates",
                  "pods": {"total": len(pods), "running": sum(p.get("status", {}).get("phase") == "Running" for p in pods),
                           "ready": sum(any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", [])) for p in pods)},
                  "warnings": list(warnings)}
        for key, resource, unit in (("cpu", "cpu", "cores"), ("memory", "memory", "bytes"), ("ephemeral", "ephemeral-storage", "bytes")):
            item = metric_shell(unit, len(expected))
            item["request"], item["limit"], item["limit_status"] = resources(entries, resource)
            if key == "cpu":
                item["window_seconds"] = CPU_WINDOW
            if vm:
                item.update(status="unsupported", request=None, limit=None, limit_status="unknown")
                if key in {"cpu", "memory"}:
                    item["request"], item["limit"], item["limit_status"] = guest_resources(
                        [o for o in controllers if o.get("kind") == "VirtualMachine"], key)
                if stopped:
                    item["status"] = "stopped"
            elif stopped:
                item["status"] = "stopped"
            elif key != "ephemeral":
                timestamps = {k: t for k, t in metrics.get(key + "_time", {}).items() if t >= started.get(k, 0)}
                item["usage"], item["sampled_at"], item["status"], observed = measurement(expected, metrics.get(key, {}), timestamps, now)
                item["coverage"]["observed"] = observed
            output[key] = item
        volumes = []
        for name in sorted(claim_names):
            pvc = pvcs.get(name, {})
            requested = number(pvc.get("spec", {}).get("resources", {}).get("requests", {}).get("storage"))
            provisioned = number(pvc.get("status", {}).get("capacity", {}).get("storage"))
            usage, sampled_at, state, _ = measurement([(name,)], metrics.get("storage", {}), metrics.get("storage_time", {}), now)
            capacity, capacity_at, capacity_state, _ = measurement([(name,)], metrics.get("storage_capacity", {}), metrics.get("storage_capacity_time", {}), now)
            if vm or pvc.get("spec", {}).get("volumeMode") == "Block":
                usage, sampled_at, state = None, None, "unsupported"
                capacity = provisioned
            elif capacity is None:
                capacity = provisioned
                if state == "ok":
                    state = "partial"
            elif capacity_state == "stale":
                state = "stale"
            volumes.append({"name": name, "usage": usage, "capacity": capacity, "requested": requested,
                            "status": state, "sampled_at": sampled_at,
                            "capacity_source": "pvc" if capacity_at is None or vm or pvc.get("spec", {}).get("volumeMode") == "Block" else "filesystem"})
        measured = [v for v in volumes if v["usage"] is not None]
        storage_state = "unavailable"
        if vm:
            storage_state = "unsupported"
        elif any(v["status"] == "stale" for v in measured):
            storage_state = "stale"
        elif measured:
            storage_state = "ok" if len(measured) == len(volumes) and all(v["status"] == "ok" for v in volumes) else "partial"
        output["storage"] = {"usage": sum(v["usage"] for v in measured) if measured else None,
                             "capacity": sum(v["capacity"] for v in volumes) if volumes and all(v["capacity"] is not None for v in volumes) else None,
                             "requested": sum(v["requested"] for v in volumes) if volumes and all(v["requested"] is not None for v in volumes) else None,
                             "unit": "bytes", "status": storage_state,
                             "sampled_at": min((v["sampled_at"] for v in measured if v["sampled_at"]), default=None),
                             "coverage": {"observed": len(measured), "expected": len(volumes)}, "volumes": volumes}
        states = [output[k]["status"] for k in (("cpu", "memory", "storage") if claim_names else ("cpu", "memory"))]
        if stopped:
            output["status"] = "stopped"
        elif vm:
            output["status"] = "unsupported"
            output["warnings"].append("vm_guest_metrics_unavailable")
        elif inventory_ok and all(s == "ok" for s in states):
            output["status"] = "ok"
        elif any(output[k]["usage"] is not None for k in ("cpu", "memory", "storage")):
            output["status"] = "partial"
        return output
