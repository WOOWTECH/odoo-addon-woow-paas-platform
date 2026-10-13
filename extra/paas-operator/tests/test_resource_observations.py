"""Telemetry tests distinguish real zero, missing/stale data and workload scope."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services import resource_observations as resource

NOW = 1000


def obj(kind, name, release=None, spec=None, status=None, owners=None):
    meta = {"name": name, "uid": kind + "-" + name, "namespace": "paas-ws-test"}
    if release:
        meta["labels"] = {"app.kubernetes.io/instance": release}
    if owners:
        meta["ownerReferences"] = [{"uid": owner} for owner in owners]
    return {"kind": kind, "metadata": meta, "spec": spec or {}, "status": status or {}}


def container(name="web", cpu="2", memory="4Gi"):
    return {"name": name, "resources": {"requests": {"cpu": "500m", "memory": "1Gi"},
                                        "limits": {"cpu": cpu, "memory": memory, "ephemeral-storage": "10Gi"}}}


def pod(name="one", release="svc-one", claim="existing-data", **kwargs):
    return obj("Pod", name, release,
               {"containers": [container()], "volumes": [{"persistentVolumeClaim": {"claimName": claim}}]},
               {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}, **kwargs)


def pvc(name="existing-data", **kwargs):
    return obj("PersistentVolumeClaim", name, spec={"resources": {"requests": {"storage": "256Gi"}}},
               status={"capacity": {"storage": "256Gi"}}, **kwargs)


def metrics(pods=("one",), claims=("existing-data",), stamp=NOW - 20):
    result = {k: {} for k in ("cpu", "cpu_time", "memory", "memory_time", "storage", "storage_time", "storage_capacity", "storage_capacity_time")}
    for name in pods:
        for key, val in (("cpu", 0), ("memory", 2 ** 30)):
            result[key][(name, "web")] = val
            result[key + "_time"][(name, "web")] = stamp
    for name in claims:
        result["storage"][(name,)] = 100 * 2 ** 30
        result["storage_time"][(name,)] = stamp
        result["storage_capacity"][(name,)] = 250 * 2 ** 30
        result["storage_capacity_time"][(name,)] = stamp
    return result


def observe(group=None, metric=None, claims=None, pvcs=None):
    return resource.ResourceCollector._observation(
        group if group is not None else [pod()], claims if claims is not None else {"existing-data"},
        pvcs if pvcs is not None else {"existing-data": pvc()}, metric if metric is not None else metrics(),
        NOW, [], True)


def test_real_zero_is_not_missing_and_filesystem_capacity_is_not_requested():
    got = observe()
    assert got["cpu"]["usage"] == 0
    assert got["cpu"]["status"] == "ok"
    assert got["cpu"]["limit"] == 2
    assert got["cpu"]["request"] == 0.5
    assert got["memory"]["usage"] == 2 ** 30
    assert got["storage"]["capacity"] == 250 * 2 ** 30
    assert got["storage"]["requested"] == 256 * 2 ** 30
    assert got["ephemeral"]["usage"] is None
    assert got["ephemeral"]["limit"] == 10 * 2 ** 30


def test_missing_container_data_is_partial_and_sidecars_count_in_limits():
    p = pod()
    p["spec"]["containers"].append(container("proxy", "100m", "64Mi"))
    got = observe([p])
    assert got["cpu"]["status"] == "partial"
    assert got["cpu"]["coverage"] == {"expected": 2, "observed": 1}
    assert got["cpu"]["limit"] == 2.1


def test_unbounded_container_never_turns_into_a_false_limit():
    c = container("sidecar")
    del c["resources"]["limits"]["cpu"]
    request, limit, state = resource.resources([(container(), 1), (c, 1)], "cpu")
    assert (request, limit, state) == (1, None, "unbounded")


def test_old_samples_are_stale_even_when_query_was_just_made():
    got = observe(metric=metrics(stamp=NOW - 121))
    assert got["cpu"]["status"] == "stale"
    assert got["storage"]["status"] == "stale"
    assert got["cpu"]["sampled_at"] == resource.iso(NOW - 121)


def test_predecessor_statefulset_samples_are_not_current_usage():
    p = pod()
    p["metadata"]["creationTimestamp"] = resource.iso(NOW - 5)
    got = observe([p])
    assert got["cpu"]["usage"] is None
    assert got["cpu"]["status"] == "unavailable"


def test_missing_storage_is_null_not_zero_and_shared_claim_is_deduplicated():
    group = [pod(), pod("two")]
    claims = resource.ResourceCollector._claims(group, [pvc()])
    assert claims == {"existing-data"}
    got = observe(group, metrics(pods=("one", "two"), claims=()))
    assert got["storage"]["usage"] is None
    assert got["storage"]["capacity"] == 256 * 2 ** 30
    assert got["storage"]["coverage"] == {"observed": 0, "expected": 1}


def test_partial_disk_coverage_keeps_total_capacity_and_marks_partial():
    got = observe(claims={"existing-data", "other"}, pvcs={"existing-data": pvc(), "other": pvc("other")})
    assert got["storage"]["usage"] == 100 * 2 ** 30
    assert got["storage"]["capacity"] == 506 * 2 ** 30
    assert got["storage"]["status"] == "partial"


def test_stopped_controller_preserves_configured_capacity_without_fake_zero():
    deployment = obj("Deployment", "one", "svc-one", {"replicas": 0, "template": {"spec": {"containers": [container()]}}})
    got = observe([deployment], metric={}, claims=set(), pvcs={})
    assert got["status"] == "stopped"
    assert got["cpu"]["usage"] is None
    assert got["cpu"]["limit"] == 2
    deployment["spec"]["replicas"] = 1
    assert observe([deployment], metric={}, claims=set(), pvcs={})["status"] == "unavailable"


def test_vm_pod_usage_is_never_presented_as_guest_usage():
    p = pod()
    p["metadata"]["labels"]["kubevirt.io"] = "virt-launcher"
    got = observe([p])
    assert got["workload_type"] == "vm"
    assert got["cpu"]["usage"] is None
    assert got["storage"]["usage"] is None
    assert got["status"] == "unsupported"


def test_vm_preserves_guest_capacity_without_using_qemu_overhead():
    vm = obj("VirtualMachine", "guest", "svc-one", {"running": True, "template": {"spec": {"domain": {
        "cpu": {"cores": 4, "sockets": 2, "threads": 1}, "memory": {"guest": "16Gi"},
        "resources": {"requests": {"cpu": "2", "memory": "16Gi"}}}}}})
    got = observe([vm], metric={})
    assert got["resource_scope"] == "vm_guest"
    assert got["cpu"]["limit"] == 8
    assert got["cpu"]["request"] == 2
    assert got["memory"]["limit"] == 16 * 2 ** 30
    assert got["cpu"]["usage"] is None
    assert got["status"] == "unsupported"


def test_daemonset_configuration_uses_scheduled_node_count():
    ds = obj("DaemonSet", "agents", "svc-one", {"template": {"spec": {"containers": [container()]}}},
             {"desiredNumberScheduled": 4})
    got = observe([ds], metric={}, claims=set(), pvcs={})
    assert got["cpu"]["limit"] == 8
    ds["status"] = {}
    assert observe([ds], metric={}, claims=set(), pvcs={})["cpu"]["limit"] is None


def test_stateless_service_is_ok_when_its_applicable_metrics_are_complete():
    p = pod()
    p["spec"]["volumes"] = []
    assert observe([p], claims=set(), pvcs={})["status"] == "ok"


def test_owner_chain_and_existing_claim_and_statefulset_claim_templates():
    deploy = obj("Deployment", "app", "svc-one")
    rs = obj("ReplicaSet", "app-rs", owners=[deploy["metadata"]["uid"]])
    p = pod(release=None, owners=[rs["metadata"]["uid"]])
    owned = resource.ResourceCollector._ownership([deploy, rs, p])
    assert owned[p["metadata"]["uid"]] == "svc-one"
    assert resource.ResourceCollector._claims([p], [pvc()]) == {"existing-data"}
    sts = obj("StatefulSet", "db", "svc-one", {"volumeClaimTemplates": [{"metadata": {"name": "data"}}]})
    assert resource.ResourceCollector._claims([sts], [pvc("data-db-0"), pvc("data-db-other")]) == {"data-db-0"}


def test_scoped_queries_and_response_never_include_other_releases(monkeypatch):
    collector = resource.ResourceCollector(clock=lambda: NOW)
    items = [pod(), pod("stranger", "svc-two", "stranger-data"), pvc(), pvc("stranger-data")]
    monkeypatch.setattr(collector, "_inventory", lambda ns, kinds=resource.KINDS: items if kinds == resource.KINDS else [])
    queries = []
    monkeypatch.setattr(collector, "_query", lambda q: queries.append(q) or [])
    got = collector.collect("paas-ws-test", ["svc-one"])
    assert list(got["services"]) == ["svc-one"]
    assert len(queries) == 8
    assert all("stranger" not in q and 'namespace="paas-ws-test"' in q for q in queries)
    assert "stranger" not in json.dumps(got)
    assert any("timestamp(" in q for q in queries)


def test_collection_failure_does_not_claim_stopped_or_zero(monkeypatch):
    collector = resource.ResourceCollector(clock=lambda: NOW)
    def unavailable(*args):
        raise RuntimeError("secret connection details must not escape")
    monkeypatch.setattr(collector, "_inventory", unavailable)
    got = collector.collect("paas-ws-test", ["svc-one"])["services"]["svc-one"]
    assert got["status"] == "unavailable"
    assert got["cpu"]["usage"] is None
    assert got["warnings"] == ["inventory_unavailable"]
    assert "secret" not in json.dumps(got)


def test_cache_singleflight_and_copy_isolation(monkeypatch):
    collector = resource.ResourceCollector(clock=lambda: NOW)
    started, release = threading.Event(), threading.Event()
    calls = []
    def slow(*args):
        calls.append(args)
        started.set()
        assert release.wait(2)
        return {"services": {"svc-one": {"status": "ok"}}}
    monkeypatch.setattr(collector, "_collect", slow)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(collector.collect, "paas-ws-test", ["svc-one"])
        assert started.wait(1)
        second = pool.submit(collector.collect, "paas-ws-test", ["svc-one"])
        release.set()
        got, other = first.result(), second.result()
    assert len(calls) == 1
    got["services"]["svc-one"]["status"] = "changed"
    assert other["services"]["svc-one"]["status"] == "ok"
    assert collector.collect("paas-ws-test", ["svc-one"])["services"]["svc-one"]["status"] == "ok"


def test_cache_has_hard_size_limit(monkeypatch):
    collector = resource.ResourceCollector(clock=lambda: NOW)
    monkeypatch.setattr(resource, "MAX_CACHE_KEYS", 2)
    monkeypatch.setattr(collector, "_collect", lambda ns, names: {"services": {}})
    for name in ["one", "two", "three"]:
        collector.collect("paas-ws-test", [name])
    assert len(collector._cache) == 2


def test_cached_data_ages_without_resetting_source_timestamp(monkeypatch):
    now = [NOW]
    collector = resource.ResourceCollector(clock=lambda: now[0])
    result = {"services": {"svc-one": observe(metric=metrics(stamp=NOW - 110))}}
    monkeypatch.setattr(collector, "_collect", lambda ns, names: result)
    first = collector.collect("paas-ws-test", ["svc-one"])
    assert first["services"]["svc-one"]["cpu"]["status"] == "ok"
    now[0] += 20
    cached = collector.collect("paas-ws-test", ["svc-one"])
    assert cached["services"]["svc-one"]["cpu"]["status"] == "stale"


def test_raw_sample_parser_rejects_nonfinite_values():
    assert resource.samples([{"metric": {"pod": "p", "container": "c"}, "value": [NOW, "NaN"]}], ("pod", "container")) == {}


def test_values_and_timestamps_select_the_same_latest_series():
    queries = resource.ResourceCollector._expressions("paas-ws-test", ["pod-one"], ["claim-one"])
    # Both previous container IDs and previous mount nodes may remain in the
    # lookback window. Reduce only the newest source, not max(old_value,new_value).
    for key in ("cpu", "memory", "storage", "storage_capacity"):
        assert " and topk by(" in queries[key]
        assert "(1,timestamp(" in queries[key]
    assert "timestamp(" in queries["cpu_time"]


def test_block_volume_capacity_is_pvc_and_never_filesystem_usage():
    claim = pvc()
    claim["spec"]["volumeMode"] = "Block"
    got = observe(pvcs={"existing-data": claim})["storage"]["volumes"][0]
    assert got["capacity_source"] == "pvc"
    assert got["usage"] is None
    assert got["capacity"] == 256 * 2 ** 30


def test_restartable_init_sidecar_samples_must_postdate_its_start():
    p = pod()
    sidecar = container("proxy")
    sidecar["restartPolicy"] = "Always"
    p["spec"]["initContainers"] = [sidecar]
    p["status"]["initContainerStatuses"] = [{"name": "proxy", "state": {"running": {"startedAt": resource.iso(NOW - 1)}}}]
    m = metrics()
    m["cpu"][("one", "proxy")] = 5
    m["cpu_time"][("one", "proxy")] = NOW - 10
    got = observe([p], m)
    assert got["cpu"]["usage"] == 0
    assert got["cpu"]["coverage"] == {"observed": 1, "expected": 2}


@pytest.fixture
def client():
    with patch("src.main.settings") as settings:
        settings.api_key = "test-key"
        yield TestClient(app, headers={"X-API-Key": "test-key"})


def test_api_requires_auth_and_enforces_scope_and_bounds(client):
    path = "/api/namespaces/paas-ws-test/resource-observations"
    with patch("src.api.resource_observations.collector.collect") as collect:
        collect.return_value = {"services": {}}
        assert client.post(path, json={"release_names": ["svc-one"]}, headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.post(path.replace("paas-ws-test", "kube-system"), json={"release_names": ["svc-one"]}).status_code == 403
        assert client.post(path.replace("paas-ws-test", "paas-ws-stg-test"), json={"release_names": ["svc-one"]}).status_code == 403
        assert client.post(path, json={"release_names": ["svc-one", 'svc-.*"}']}).status_code == 422
        assert client.post(path, json={"release_names": ["svc-one"] * 101}).status_code == 422
        collect.assert_not_called()
        assert client.post(path, json={"release_names": ["svc-one", "svc-one"]}).status_code == 200
        collect.assert_called_once_with("paas-ws-test", ["svc-one"])


def test_inventory_is_only_namespaced_get_with_bounded_timeout():
    k8s = SimpleNamespace(_run_command=lambda *a, **kw: SimpleNamespace(stdout='{"items":[]}'))
    collector = resource.ResourceCollector(k8s=k8s)
    with patch.object(k8s, "_run_command", return_value=SimpleNamespace(stdout='{"items":[]}')) as run:
        collector._inventory("paas-ws-test")
    args = run.call_args.args[0]
    assert args[0] == "get" and "paas-ws-test" in args
    assert "--all-namespaces" not in args
    assert not any(arg.startswith("--request-timeout") for arg in args)
    assert run.call_args.kwargs["timeout"] == 5
