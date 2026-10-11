"""#1647：升級前檢查工作區的 compute 配額——確定放不下就在 helm 套用任何東西前擋。

起因（2026-10-10 正式站 omnigent.woowtech.io）：namespace 的 ``limits.cpu`` 上限 12，server 與
同 namespace 平台外的 PG 被調成各 8。Deployment 是 ``Recreate``：舊 pod 先被砍、新 pod 被 quota
admission 拒絕（ReplicaSet ``FailedCreate: exceeded quota``），剩 0 個 pod，而平台一直顯示 running。

做法：#1407／#1513 的升級預檢已經 dry-run 出 manifest、讀了 live workload。請求帶
``check_compute_quota: true`` 時再讀一次 ResourceQuota／LimitRange／Pod，用同一份 manifest 算
這個 release 升級後的 pod 放不放得下。**只擋確定放不下**；任何「不知道」一律放行，不帶旗標
的請求行為與加入本檢查前完全相同（operator 可以先於平台發版）。
"""
import json
import logging
from decimal import Decimal
from unittest.mock import patch

import pytest
import yaml

from src.models.schemas import ReleaseInfo, ReleaseRevision, ReleaseStatus
from src.services import compute_quota
from src.services.compute_quota import (
    COMPUTE_QUOTA_ERROR_CODE,
    TRACKED_RESOURCES,
    Unknown,
    build_error_detail,
    container_resources,
    evaluate,
    format_quantity,
    limitrange_defaults,
    own_pods,
    pod_resources,
    quota_limits,
)
from src.services.helm import KubectlException

NS = "paas-ws-test"
RELEASE = "svc"
GI = 1024 ** 3
MI = 1024 ** 2


# --- 造資料 ------------------------------------------------------------------------

def _container(name="app", cpu=None, memory=None, requests=None, limits=None, **extra):
    """``cpu``／``memory`` 是 limits 的簡寫；``limits``／``requests`` 可給完整 dict。"""
    resources = {}
    lim = dict(limits or {})
    if cpu is not None:
        lim["cpu"] = cpu
    if memory is not None:
        lim["memory"] = memory
    if lim:
        resources["limits"] = lim
    if requests:
        resources["requests"] = dict(requests)
    return {"name": name, "image": "example/app:1", "resources": resources, **extra}


def _workload(kind, name, containers, replicas=1, strategy=None, labels=None, release=None,
              status_replicas=None, init=None, selector=None):
    labels = labels or {"app": name}
    spec = {"selector": selector or {"matchLabels": dict(labels)},
            "template": {"metadata": {"labels": dict(labels)},
                         "spec": {"containers": containers}}}
    if replicas is not None:
        spec["replicas"] = replicas
    if init:
        spec["template"]["spec"]["initContainers"] = init
    if strategy is not None:
        spec["strategy" if kind == "Deployment" else "updateStrategy"] = strategy
    doc = {"apiVersion": "apps/v1", "kind": kind, "metadata": {"name": name}, "spec": spec}
    if release:
        doc["metadata"]["annotations"] = {"meta.helm.sh/release-name": release}
    if status_replicas is not None:
        doc["status"] = {"replicas": status_replicas}
    return doc


def _deploy(name, containers, **kwargs):
    return _workload("Deployment", name, containers, **kwargs)


def _sts(name, containers, **kwargs):
    return _workload("StatefulSet", name, containers, **kwargs)


def _pod(name, owner, containers, labels=None, phase="Running", deleting=False, init=None):
    """``owner``＝``("ReplicaSet", "svc-app-abc12")``／``("StatefulSet", "pg")``／``None``。

    live pod 已被 API server 補過預設：容器要自己帶 requests（``_live`` 幫忙補）。
    """
    meta = {"name": name, "labels": dict(labels or {})}
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    if deleting:
        meta["deletionTimestamp"] = "2026-10-10T18:07:00Z"
    spec = {"containers": containers}
    if init:
        spec["initContainers"] = init
    return {"kind": "Pod", "metadata": meta, "spec": spec, "status": {"phase": phase}}


def _live(name="app", cpu="1", memory="1Gi"):
    """live pod 的容器：limits 與 requests 都在（API server 的預設已經套上）。"""
    both = {"cpu": cpu, "memory": memory}
    return _container(name, limits=both, requests=both)


def _quota(hard, used, name=f"{NS}-quota", **spec_extra):
    return {"kind": "ResourceQuota", "metadata": {"name": name},
            "spec": {"hard": dict(hard), **spec_extra},
            "status": {"hard": dict(hard), "used": dict(used)}}


def _limitrange(name=f"{NS}-limits", default=None, default_request=None):
    entry = {"type": "Container"}
    if default is not None:
        entry["default"] = default
    if default_request is not None:
        entry["defaultRequest"] = default_request
    return {"kind": "LimitRange", "metadata": {"name": name}, "spec": {"limits": [entry]}}


def _cpu_quota(hard, used):
    return _quota({"limits.cpu": hard}, {"limits.cpu": used})


def _incident(new_cpu="8", old_cpu="4", strategy=None, used="12", extra_pods=()):
    """事故的形狀：hard limits.cpu=12、平台外 StatefulSet ``pg`` 佔 8、本 release 的舊 pod。"""
    strategy = strategy or {"type": "Recreate"}
    rendered = [_deploy("svc-app", [_container(cpu=new_cpu, memory="1Gi")], strategy=strategy)]
    live = [
        _deploy("svc-app", [_container(cpu=old_cpu, memory="1Gi")], strategy=strategy,
                release=RELEASE, status_replicas=1),
        _sts("pg", [_container("pg", cpu="8", memory="1Gi")], status_replicas=1),
    ]
    pods = [
        _pod("svc-app-7d9c6-x1", ("ReplicaSet", "svc-app-7d9c6"), [_live(cpu=old_cpu)],
             labels={"app": "svc-app"}),
        _pod("pg-0", ("StatefulSet", "pg"), [_live("pg", cpu="8")], labels={"app": "pg"}),
        *extra_pods,
    ]
    return dict(release=RELEASE, rendered_docs=rendered, live_items=live, pods=pods,
                quota_items=[_cpu_quota("12", used)], limitrange_items=[])


# --- LimitRange 預設與單一容器 -----------------------------------------------------

def test_limitrange_fills_missing_limits_and_requests():
    defaults = limitrange_defaults([_limitrange(
        default={"cpu": "500m", "memory": "512Mi"},
        default_request={"cpu": "250m", "memory": "256Mi"})])
    got = container_resources(_container(), defaults)
    assert got["limits.cpu"] == Decimal("0.5")
    assert got["requests.cpu"] == Decimal("0.25")
    assert got["limits.memory"] == 512 * MI
    assert got["requests.memory"] == 256 * MI


def test_container_that_only_writes_limits_gets_equal_requests():
    """API server 對 Pod 的預設（早於 LimitRange）：requests 沒寫、limits 有寫 ⇒ requests＝limits。"""
    defaults = limitrange_defaults([_limitrange(
        default={"cpu": "500m"}, default_request={"cpu": "100m"})])
    got = container_resources(_container(cpu="2"), defaults)
    assert got["limits.cpu"] == 2
    assert got["requests.cpu"] == 2, "LimitRange 的 defaultRequest 不能蓋過 API server 的預設"


def test_limitrange_with_only_default_uses_it_for_requests_too():
    defaults = limitrange_defaults([_limitrange(default={"cpu": "500m"})])
    assert defaults["requests"]["cpu"] == Decimal("0.5")
    assert container_resources(_container(), defaults)["requests.cpu"] == Decimal("0.5")


def test_first_limitrange_by_name_wins():
    defaults = limitrange_defaults([
        _limitrange("z-limits", default={"cpu": "4"}),
        _limitrange("a-limits", default={"cpu": "1"}),
    ])
    assert defaults["limits"]["cpu"] == 1


def test_only_container_type_entries_count():
    item = {"kind": "LimitRange", "metadata": {"name": "x"},
            "spec": {"limits": [{"type": "Pod", "default": {"cpu": "9"}}]}}
    assert limitrange_defaults([item]) == {"limits": {}, "requests": {}}


def test_live_pod_containers_are_not_defaulted_again():
    """``defaults=None``：物件已被 API server 補過，沒寫的就是沒有，不再補。"""
    got = container_resources(_container(cpu="2"), None)
    assert got == {"limits.cpu": Decimal(2)}


# --- pod 有效資源 ------------------------------------------------------------------

def test_regular_containers_add_up():
    spec = {"containers": [_live("a", cpu="1", memory="1Gi"), _live("b", cpu="500m", memory="512Mi")]}
    got = pod_resources(spec, None)
    assert got["limits.cpu"] == Decimal("1.5")
    assert got["limits.memory"] == GI + 512 * MI


def test_init_container_counts_as_a_peak_not_a_sum():
    spec = {"containers": [_live("app", cpu="1")],
            "initContainers": [_live("migrate", cpu="3"), _live("seed", cpu="2")]}
    assert pod_resources(spec, None)["limits.cpu"] == 3


def test_small_init_container_does_not_lower_the_total():
    spec = {"containers": [_live("a", cpu="2"), _live("b", cpu="2")],
            "initContainers": [_live("init", cpu="1")]}
    assert pod_resources(spec, None)["limits.cpu"] == 4


def test_sidecar_init_container_runs_alongside_everything():
    """``restartPolicy: Always`` 的 init container 是 sidecar：計入總和，也墊高之後的 init 峰值。"""
    sidecar = dict(_live("proxy", cpu="1"), restartPolicy="Always")
    spec = {"containers": [_live("app", cpu="2")],
            "initContainers": [sidecar, _live("migrate", cpu="4")]}
    # 常駐：app 2＋sidecar 1＝3；init 峰值：migrate 4＋已啟動的 sidecar 1＝5
    assert pod_resources(spec, None)["limits.cpu"] == 5


def test_pod_overhead_is_added():
    spec = {"containers": [_live("app", cpu="1", memory="1Gi")],
            "overhead": {"cpu": "250m", "memory": "64Mi"}}
    got = pod_resources(spec, None)
    assert got["limits.cpu"] == Decimal("1.25")
    assert got["requests.memory"] == GI + 64 * MI


def test_pod_level_resources_are_unknown():
    with pytest.raises(Unknown) as err:
        pod_resources({"resources": {"limits": {"cpu": "2"}}, "containers": [_live()]}, None)
    assert err.value.reason == "pod_level_resources"


def test_resource_missing_from_any_container_is_left_out():
    """一個容器算不出來，整顆 pod 的那個資源就是不知道——回傳值不含該鍵。"""
    spec = {"containers": [_container("a", cpu="1", memory="1Gi"), _container("b", memory="1Gi")]}
    got = pod_resources(spec, {"limits": {}, "requests": {}})
    assert "limits.cpu" not in got and "requests.cpu" not in got
    assert got["limits.memory"] == 2 * GI


def test_unparseable_quantity_is_left_out():
    got = pod_resources({"containers": [_container(cpu="lots", memory="1Gi")]},
                        {"limits": {}, "requests": {}})
    assert "limits.cpu" not in got
    assert got["limits.memory"] == GI


# --- quota -------------------------------------------------------------------------

def test_tightest_quota_wins_per_resource():
    got = quota_limits([
        _quota({"limits.cpu": "12", "limits.memory": "8Gi"},
               {"limits.cpu": "2", "limits.memory": "7Gi"}, name="a"),
        _quota({"limits.cpu": "6"}, {"limits.cpu": "5"}, name="b"),
    ])
    assert got["limits.cpu"] == {"hard": Decimal(6), "used": Decimal(5), "quota": "b"}
    assert got["limits.memory"]["quota"] == "a"


def test_scoped_quotas_are_skipped():
    """帶 scope 的 quota 只管一部分 pod（BestEffort、PriorityClass…），不拿來判。"""
    got = quota_limits([
        _quota({"limits.cpu": "1"}, {"limits.cpu": "1"}, name="scoped", scopes=["BestEffort"]),
        _quota({"limits.cpu": "1"}, {"limits.cpu": "1"}, name="selector",
               scopeSelector={"matchExpressions": [{"scopeName": "PriorityClass",
                                                    "operator": "Exists"}]}),
    ])
    assert got == {}


def test_pending_hard_replaces_the_platform_quota():
    live = _quota({"limits.cpu": "12"}, {"limits.cpu": "12"})
    got = quota_limits([live], pending_hard={"limits.cpu": "24"},
                       platform_quota_name=f"{NS}-quota")
    assert got["limits.cpu"] == {"hard": Decimal(24), "used": Decimal(12), "quota": f"{NS}-quota"}


def test_pending_hard_does_not_touch_other_quotas():
    other = _quota({"limits.cpu": "10"}, {"limits.cpu": "9"}, name="ops-extra")
    got = quota_limits([other], pending_hard={"limits.cpu": "24"},
                       platform_quota_name=f"{NS}-quota")
    assert got["limits.cpu"]["quota"] == "ops-extra", "另一個 quota 仍是最緊的那個"


def test_pending_hard_is_synthesised_when_the_platform_quota_is_absent():
    got = quota_limits([], pending_hard={"limits.cpu": "24"}, platform_quota_name=f"{NS}-quota")
    assert got["limits.cpu"] == {"hard": Decimal(24), "used": Decimal(0), "quota": f"{NS}-quota"}


def test_bare_cpu_and_memory_are_request_aliases():
    got = quota_limits([_quota({"cpu": "4", "memory": "8Gi"}, {"cpu": "1", "memory": "2Gi"})])
    assert got["requests.cpu"]["hard"] == 4
    assert got["requests.memory"]["used"] == 2 * GI
    assert "cpu" not in got and "memory" not in got


# --- pod 歸屬 ----------------------------------------------------------------------

def test_own_pods_need_selector_and_controller_owner():
    deploy = _deploy("svc-app", [_container(cpu="1")])
    mine = _pod("p1", ("ReplicaSet", "svc-app-7d9c6"), [_live()], labels={"app": "svc-app"})
    same_labels_other_owner = _pod("p2", ("StatefulSet", "svc-app"), [_live()],
                                   labels={"app": "svc-app"})
    no_owner = _pod("p3", None, [_live()], labels={"app": "svc-app"})
    other_labels = _pod("p4", ("ReplicaSet", "svc-app-7d9c6"), [_live()], labels={"app": "x"})
    got = own_pods(deploy, [mine, same_labels_other_owner, no_owner, other_labels])
    assert [p["metadata"]["name"] for p in got] == ["p1"]


def test_statefulset_owns_pods_of_the_statefulset_with_the_same_name():
    sts = _sts("pg", [_container(cpu="1")])
    mine = _pod("pg-0", ("StatefulSet", "pg"), [_live()], labels={"app": "pg"})
    other = _pod("pg-replica-0", ("StatefulSet", "pg-replica"), [_live()], labels={"app": "pg"})
    assert [p["metadata"]["name"] for p in own_pods(sts, [mine, other])] == ["pg-0"]


def test_foo_and_foo_bar_deployments_are_not_confused():
    """``foo-bar-7d9c6`` 以 ``foo-`` 開頭，而且 ``foo`` 的 selector 是 ``foo-bar`` 的子集——
    只看 selector 或只看名稱前綴都會把 ``foo-bar`` 的 pod 算進 ``foo``。"""
    foo = _deploy("foo", [_container(cpu="1")], labels={"release": "r"})
    foo_bar = _deploy("foo-bar", [_container(cpu="1")], labels={"release": "r", "tier": "bar"})
    foo_pod = _pod("foo-5c8d7-a", ("ReplicaSet", "foo-5c8d7"), [_live()], labels={"release": "r"})
    bar_pod = _pod("foo-bar-7d9c6-b", ("ReplicaSet", "foo-bar-7d9c6"), [_live()],
                   labels={"release": "r", "tier": "bar"})
    assert [p["metadata"]["name"] for p in own_pods(foo, [foo_pod, bar_pod])] == ["foo-5c8d7-a"]
    assert [p["metadata"]["name"] for p in own_pods(foo_bar, [foo_pod, bar_pod])] == [
        "foo-bar-7d9c6-b"]


def test_match_expressions_selector_is_unknown():
    deploy = _deploy("svc-app", [_container(cpu="1")], selector={
        "matchLabels": {"app": "svc-app"},
        "matchExpressions": [{"key": "tier", "operator": "In", "values": ["web"]}]})
    with pytest.raises(Unknown) as err:
        own_pods(deploy, [])
    assert err.value.reason == "selector_unreadable"


def test_selector_without_match_labels_is_unknown():
    """空的 matchLabels 會選中 namespace 裡每一顆 pod——不是我們要的歸屬。"""
    for selector in ({"matchLabels": {}}, {"matchLabels": None}, {"other": 1}):
        with pytest.raises(Unknown):
            own_pods(_deploy("svc-app", [_container(cpu="1")], selector=selector), [])


def test_workload_claiming_pods_we_cannot_find_is_unknown():
    """live workload 說自己有 1 顆 pod、我們卻一顆都對不到：歸屬規則在這裡不成立，放行。"""
    case = _incident()
    case["pods"] = [p for p in case["pods"] if p["metadata"]["name"] == "pg-0"]
    with pytest.raises(Unknown) as err:
        evaluate(**case)
    assert err.value.reason == "ownership_mismatch"
    assert "svc-app" in err.value.subject


def test_on_delete_statefulset_is_unknown():
    """``OnDelete`` 不會自己換 pod——升級後 pod 的樣子由別人什麼時候刪決定，不猜。"""
    case = _incident()
    case["rendered_docs"] = [_sts("svc-db", [_container(cpu="8", memory="1Gi")],
                                  strategy={"type": "OnDelete"})]
    with pytest.raises(Unknown) as err:
        evaluate(**case)
    assert err.value.reason == "statefulset_on_delete"


# --- 事故重現 ----------------------------------------------------------------------

def test_incident_recreate_upgrade_that_cannot_fit_is_refused():
    """hard limits.cpu=12、平台外 StatefulSet 佔 8、本 release 舊 pod 4、Recreate 新 pod 8。"""
    result = evaluate(**_incident())
    assert result["status"] == "exceeded"
    assert result["quota"] == f"{NS}-quota"
    assert result["quota_source"] == "live"
    [short] = result["shortfalls"]
    assert short == {
        "resource": "limits.cpu", "dimension": "cpu", "kind": "limits",
        "phase": "steady", "workload": None,
        "hard": "12", "used": "12", "release_current": "4", "other": "8",
        "required": "8", "available": "4", "short_by": "4",
    }
    assert result["workloads"] == [{
        "kind": "Deployment", "name": "svc-app", "strategy": "Recreate", "replicas": 1,
        "per_pod": {"limits.cpu": "8", "limits.memory": "1Gi",
                    "requests.cpu": "8", "requests.memory": "1Gi"}}]
    assert result["consumers"] == [{"owner": "StatefulSet/pg", "pods": 1, "value": "8"}]


def test_statefulset_upgrade_that_cannot_fit_is_refused_too():
    case = _incident()
    case["rendered_docs"] = [_sts("svc-app", [_container(cpu="8", memory="1Gi")])]
    case["live_items"][0] = _sts("svc-app", [_container(cpu="4", memory="1Gi")],
                                 release=RELEASE, status_replicas=1)
    case["pods"][0] = _pod("svc-app-0", ("StatefulSet", "svc-app"), [_live(cpu="4")],
                           labels={"app": "svc-app"})
    result = evaluate(**case)
    assert result["status"] == "exceeded"
    assert result["shortfalls"][0]["short_by"] == "4"


# --- 不誤擋的對照 ------------------------------------------------------------------

def test_recreate_with_unchanged_size_passes_at_a_full_quota():
    """used 已經等於 hard（12/12），但自己的舊 pod 會先被砍：4 → 4 放得下。"""
    result = evaluate(**_incident(new_cpu="4"))
    assert result["status"] == "ok"
    assert result["shortfalls"] == [] and result["advisories"] == []
    assert result["resources"]["limits.cpu"] == {
        "hard": "12", "used": "12", "release_current": "4", "other": "8",
        "required": "4", "available": "4"}


def test_recreate_that_shrinks_passes():
    result = evaluate(**_incident(new_cpu="4", old_cpu="8", used="16"))
    assert result["status"] == "ok"


def test_rolling_update_with_unchanged_template_is_only_an_advisory():
    """pod template 沒變的 upgrade 不會換 pod：就算多一顆放不下，也證明不了會出事。"""
    result = evaluate(**_incident(new_cpu="4", strategy={"type": "RollingUpdate"}))
    assert result["status"] == "ok"
    assert result["shortfalls"] == []
    [advisory] = result["advisories"]
    assert advisory["type"] == "rolling_update_headroom"
    assert advisory["workload"] == "Deployment/svc-app"
    assert advisory["resource"] == "limits.cpu"


def test_rolling_update_with_changed_resources_and_no_room_is_refused():
    """預設 RollingUpdate（25%／25%）在 1 個 replica 時 maxUnavailable＝0：新 pod 要先建出來。"""
    case = _incident(new_cpu="2", old_cpu="4", strategy={"type": "RollingUpdate"})
    # 升級後放得下（8＋2 ≤ 12），但換的過程新舊並存：12＋2 > 12。
    result = evaluate(**case)
    assert result["status"] == "exceeded"
    [short] = result["shortfalls"]
    assert short["phase"] == "rolling"
    assert short["workload"] == "Deployment/svc-app"
    assert (short["required"], short["used"], short["hard"], short["short_by"]) == (
        "2", "12", "12", "2")


def test_rolling_update_that_may_take_a_pod_down_first_is_not_checked_for_overlap():
    """maxUnavailable ≥ 1：controller 可以先砍舊 pod，不需要新舊並存的空間。"""
    strategy = {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1}}
    result = evaluate(**_incident(new_cpu="2", old_cpu="4", strategy=strategy))
    assert result["status"] == "ok"
    assert result["advisories"] == []


def test_no_surge_means_no_overlap_either():
    """maxSurge＝0 時 k8s 把 maxUnavailable 當 1（兩者不能同時為 0）——不會多建 pod。"""
    strategy = {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": "25%", "maxSurge": 0}}
    result = evaluate(**_incident(new_cpu="2", old_cpu="4", strategy=strategy))
    assert result["status"] == "ok"
    assert result["advisories"] == []


def test_terminating_pods_do_not_count_as_other_usage():
    """正在終止的別人的 pod 馬上就會釋出配額：不算佔用。"""
    leaving = _pod("old-job-x", ("Job", "old-job"), [_live(cpu="4")], deleting=True)
    # used 16＝自己 4＋pg 8＋正在終止的 4；新 pod 4 放得下（8＋4 ≤ 12）。
    result = evaluate(**_incident(new_cpu="4", used="16", extra_pods=[leaving]))
    assert result["status"] == "ok"
    assert result["resources"]["limits.cpu"]["other"] == "8"


def test_finished_pods_do_not_count():
    """Succeeded／Failed 的 pod 不佔 quota。``status.used`` 偏高（20）時靠的是 pod 加總那一邊，
    所以結束的 pod 必須從加總裡排除——不排除的話會算成 8＋6＋6＝20 而誤擋。"""
    done = [_pod("job-ok", ("Job", "j"), [_live(cpu="6")], phase="Succeeded"),
            _pod("job-bad", ("Job", "j"), [_live(cpu="6")], phase="Failed")]
    result = evaluate(**_incident(new_cpu="4", used="20", extra_pods=done))
    assert result["status"] == "ok"
    assert result["resources"]["limits.cpu"]["other"] == "8"


def test_evicted_own_pod_is_not_counted_as_current_usage():
    """本 release 被驅逐（Failed）的舊 pod 還掛在那裡：不佔 quota，也不算「目前佔用」。"""
    evicted = _pod("svc-app-7d9c6-old", ("ReplicaSet", "svc-app-7d9c6"), [_live(cpu="4")],
                   labels={"app": "svc-app"}, phase="Failed")
    result = evaluate(**_incident(new_cpu="4", extra_pods=[evicted]))
    assert result["status"] == "ok"
    assert result["resources"]["limits.cpu"]["release_current"] == "4"


def test_stale_quota_usage_is_capped_by_the_pods_that_exist():
    """``status.used`` 由 quota controller 非同步更新；比實際 pod 加總大時取小的那個。"""
    # used 說 20，但其他 pod 實際只有 pg 的 8 ⇒ 8＋4 ≤ 12，不擋。
    result = evaluate(**_incident(new_cpu="4", used="20"))
    assert result["status"] == "ok"
    assert result["resources"]["limits.cpu"]["other"] == "8"


def test_pods_newer_than_the_quota_status_do_not_inflate_usage_either():
    """反方向：pod 清單比 ``status.used`` 新（多一顆還沒被記帳的）時，取 used 那一邊。"""
    newcomer = _pod("fresh-0", ("StatefulSet", "fresh"), [_live(cpu="6")])
    # used 12＝自己 4＋pg 8；pod 加總說其他有 14 ⇒ 取 8。
    result = evaluate(**_incident(new_cpu="4", extra_pods=[newcomer]))
    assert result["status"] == "ok"
    assert result["resources"]["limits.cpu"]["other"] == "8"


def test_cronjobs_jobs_and_daemonsets_are_not_part_of_the_demand():
    case = _incident(new_cpu="4")
    template = {"spec": {"containers": [_container(cpu="6", memory="1Gi")]}}
    case["rendered_docs"] += [
        {"kind": "CronJob", "metadata": {"name": "svc-backup"},
         "spec": {"jobTemplate": {"spec": {"template": template}}}},
        {"kind": "Job", "metadata": {"name": "svc-migrate"}, "spec": {"template": template}},
        {"kind": "DaemonSet", "metadata": {"name": "svc-agent"}, "spec": {"template": template}},
    ]
    result = evaluate(**case)
    assert result["status"] == "ok"
    assert result["resources"]["limits.cpu"]["required"] == "4"
    assert [w["name"] for w in result["workloads"]] == ["svc-app"]
    # CronJob 跑的時候放不下（剩 0、要 6）：只提醒。
    [advisory] = result["advisories"]
    assert advisory["type"] == "cronjob_headroom"
    assert advisory["workload"] == "CronJob/svc-backup"
    assert (advisory["required"], advisory["available"]) == ("6", "0")


def test_demand_of_all_workloads_in_the_release_adds_up():
    case = _incident(new_cpu="2")
    case["rendered_docs"].append(
        _sts("svc-db", [_container("db", cpu="3", memory="1Gi")], replicas=1))
    result = evaluate(**case)
    # 其他 8＋本 release 2＋3＝13 > 12
    assert result["status"] == "exceeded"
    assert result["shortfalls"][0]["required"] == "5"
    assert result["shortfalls"][0]["short_by"] == "1"


def test_replicas_multiply_the_demand():
    case = _incident(new_cpu="2")
    case["rendered_docs"][0]["spec"]["replicas"] = 3
    result = evaluate(**case)
    assert result["status"] == "exceeded"
    assert result["shortfalls"][0]["required"] == "6"


def test_replicas_missing_from_the_manifest_fall_back_to_the_live_value():
    """HPA 管的 workload chart 不寫 replicas：用 live 物件現在的值。"""
    case = _incident(new_cpu="2")
    del case["rendered_docs"][0]["spec"]["replicas"]
    case["live_items"][0]["spec"]["replicas"] = 3
    result = evaluate(**case)
    assert result["workloads"][0]["replicas"] == 3
    assert result["shortfalls"][0]["required"] == "6"


def test_new_workload_without_replicas_counts_as_one():
    case = _incident(new_cpu="2")
    del case["rendered_docs"][0]["spec"]["replicas"]
    case["live_items"] = [w for w in case["live_items"] if w["metadata"]["name"] == "pg"]
    case["pods"] = [p for p in case["pods"] if p["metadata"]["name"] == "pg-0"]
    case["quota_items"] = [_cpu_quota("12", "8")]
    result = evaluate(**case)
    assert result["status"] == "ok"
    assert result["workloads"][0]["replicas"] == 1


def test_workload_removed_from_the_chart_frees_its_pods():
    """live 上屬於本 release、新版 chart 已經沒有的 workload：它的 pod 會消失，不算別人的佔用。"""
    case = _incident(new_cpu="4")
    case["live_items"].append(_deploy("svc-legacy", [_container(cpu="6", memory="1Gi")],
                                      release=RELEASE, status_replicas=1))
    case["pods"].append(_pod("svc-legacy-5b6c7-z", ("ReplicaSet", "svc-legacy-5b6c7"),
                             [_live(cpu="6")], labels={"app": "svc-legacy"}))
    case["quota_items"] = [_cpu_quota("18", "18")]
    result = evaluate(**case)
    assert result["status"] == "ok"
    assert result["resources"]["limits.cpu"]["release_current"] == "10"
    assert result["resources"]["limits.cpu"]["other"] == "8"


def test_zero_demand_is_never_a_shortfall():
    """需求為 0 的資源 admission 不檢查——別人已經超過上限也不關這次升級的事。"""
    case = _incident(new_cpu="4")
    case["rendered_docs"][0]["spec"]["replicas"] = 0
    case["quota_items"] = [_cpu_quota("6", "12")]  # 上限被調低，pg 的 8 已經超過
    assert evaluate(**case)["status"] == "ok"


def test_limitrange_defaults_feed_the_demand():
    """rendered 的容器沒寫 resources：需求是 LimitRange 會補的值（讀叢集上的實物）。"""
    case = _incident()
    case["rendered_docs"] = [_deploy("svc-app", [_container()], strategy={"type": "Recreate"})]
    case["limitrange_items"] = [_limitrange(default={"cpu": "5", "memory": "512Mi"})]
    result = evaluate(**case)
    assert result["workloads"][0]["per_pod"]["limits.cpu"] == "5"
    assert result["status"] == "exceeded"  # 8＋5 > 12
    assert result["shortfalls"][0]["short_by"] == "1"


# --- 不知道一律放行 ----------------------------------------------------------------

def test_container_without_limits_and_no_limitrange_is_not_judged():
    case = _incident()
    case["rendered_docs"] = [_deploy("svc-app", [_container()], strategy={"type": "Recreate"})]
    result = evaluate(**case)
    assert result["status"] == "ok"
    assert result["resources"] == {}, "算不出需求的資源整個不判"


def test_unparseable_quantity_is_not_judged():
    result = evaluate(**_incident(new_cpu="a-lot"))
    assert result["status"] == "ok"
    assert "limits.cpu" not in result["resources"]


def test_unparseable_quota_is_not_judged():
    case = _incident()
    case["quota_items"] = [_cpu_quota("plenty", "12")]
    assert evaluate(**case)["status"] == "ok"


def test_no_quota_means_nothing_to_check():
    case = _incident()
    case["quota_items"] = []
    result = evaluate(**case)
    assert result["status"] == "ok"
    assert result["workloads"] == []


def test_quota_that_tracks_nothing_we_check_passes():
    case = _incident()
    case["quota_items"] = [_quota({"persistentvolumeclaims": "10", "requests.storage": "100Gi"},
                                  {"persistentvolumeclaims": "10", "requests.storage": "100Gi"})]
    assert evaluate(**case)["status"] == "ok"


def test_release_without_workloads_passes():
    case = _incident()
    case["rendered_docs"] = [{"kind": "ConfigMap", "metadata": {"name": "svc-config"}}]
    assert evaluate(**case)["status"] == "ok"


def test_own_pod_with_unknown_usage_is_not_judged():
    case = _incident()
    case["pods"][0]["spec"]["containers"] = [_container(memory="1Gi")]  # live pod 沒有 cpu
    result = evaluate(**case)
    assert result["status"] == "ok"
    assert "limits.cpu" not in result["resources"]


def test_other_pod_with_unknown_usage_falls_back_to_the_quota_status():
    """別人的 pod 算不出來：只剩 ``status.used`` 這一種估法（12−4＝8）。"""
    case = _incident()
    case["pods"][1]["spec"]["containers"] = [_container("pg", memory="1Gi")]
    result = evaluate(**case)
    assert result["status"] == "exceeded"
    assert result["shortfalls"][0]["other"] == "8"


# --- pending quota（換方案） -------------------------------------------------------

def test_pending_quota_lets_a_larger_plan_through():
    """換到較大方案：quota 在扣款後才推送，預檢要以「將推送的上限」判斷。"""
    case = _incident()
    assert evaluate(**case)["status"] == "exceeded"
    result = evaluate(**case, pending_hard={"limits.cpu": "24"},
                      platform_quota_name=f"{NS}-quota")
    assert result["status"] == "ok"
    assert result["quota_source"] == "pending"
    assert result["resources"]["limits.cpu"]["hard"] == "24"


def test_pending_quota_can_still_be_too_small():
    result = evaluate(**_incident(), pending_hard={"limits.cpu": "14"},
                      platform_quota_name=f"{NS}-quota")
    assert result["status"] == "exceeded"
    assert result["shortfalls"][0]["short_by"] == "2"


# --- 輸出形狀 ----------------------------------------------------------------------

def test_format_quantity_units():
    assert format_quantity("limits.cpu", Decimal("8")) == "8"
    assert format_quantity("requests.cpu", Decimal("0.95")) == "0.95"
    assert format_quantity("limits.cpu", Decimal("0.1234")) == "0.123"
    assert format_quantity("limits.cpu", Decimal("0")) == "0"
    assert format_quantity("limits.memory", Decimal(8 * GI)) == "8Gi"
    assert format_quantity("limits.memory", Decimal(GI + 512 * MI)) == "1.5Gi"
    assert format_quantity("requests.memory", Decimal(512 * MI)) == "512Mi"
    assert format_quantity("limits.ephemeral-storage", Decimal(80 * GI)) == "80Gi"
    assert format_quantity("requests.ephemeral-storage", Decimal(128 * MI)) == "128Mi"


def test_several_resources_are_reported_in_a_fixed_order():
    case = _incident()
    case["rendered_docs"] = [_deploy("svc-app", [_container(cpu="8", memory="8Gi")],
                                     strategy={"type": "Recreate"})]
    case["quota_items"] = [_quota(
        {"limits.memory": "8Gi", "limits.cpu": "12", "requests.cpu": "12"},
        {"limits.memory": "2Gi", "limits.cpu": "12", "requests.cpu": "12"})]
    result = evaluate(**case)
    assert [s["resource"] for s in result["shortfalls"]] == [
        "limits.cpu", "limits.memory", "requests.cpu"]
    assert all(s["resource"] in TRACKED_RESOURCES for s in result["shortfalls"])
    memory = result["shortfalls"][1]
    assert (memory["dimension"], memory["kind"]) == ("memory", "limits")
    assert (memory["required"], memory["hard"], memory["other"], memory["short_by"]) == (
        "8Gi", "8Gi", "1Gi", "1Gi")


def test_consumers_list_the_largest_other_owners():
    extra = [
        _pod("web-5c8d7-a", ("ReplicaSet", "web-5c8d7"), [_live(cpu="1")]),
        _pod("web-5c8d7-b", ("ReplicaSet", "web-5c8d7"), [_live(cpu="1")]),
        _pod("debug", None, [_live(cpu="500m")]),
    ]
    result = evaluate(**_incident(used="14.5", extra_pods=extra))
    assert result["consumers"] == [
        {"owner": "StatefulSet/pg", "pods": 1, "value": "8"},
        {"owner": "Deployment/web", "pods": 2, "value": "2"},
        {"owner": "Pod/debug", "pods": 1, "value": "0.5"},
    ]


def test_error_detail_shape_and_message():
    detail = build_error_detail(NS, RELEASE, evaluate(**_incident()))
    assert set(detail) == {"error_code", "namespace", "release", "quota", "quota_source",
                           "shortfalls", "workloads", "consumers", "message"}
    assert detail["error_code"] == COMPUTE_QUOTA_ERROR_CODE == "workspace_compute_quota_exceeded"
    assert (detail["namespace"], detail["release"]) == (NS, RELEASE)
    assert detail["quota"] == f"{NS}-quota"
    assert detail["message"] == (
        "升級前檢查發現工作區的資源配額放不下這次變更，已停止升級（沒有套用任何變更）：\n"
        "・CPU 上限（limits.cpu）：這個服務升級後需要 8，配額上限 12，其他 workload 已佔用 8，還差 4")
    json.dumps(detail)  # 數量都已經是字串：整包可以直接序列化


def test_error_detail_describes_a_rolling_shortfall():
    result = evaluate(**_incident(new_cpu="2", old_cpu="4", strategy={"type": "RollingUpdate"}))
    line = build_error_detail(NS, RELEASE, result)["message"].splitlines()[1]
    assert "Deployment/svc-app" in line and "新舊 pod 會同時存在" in line
    assert "還差 2" in line


_SECRET_MARKER = "s3cr3t-do-not-leak-1647"


def _with_secrets(case):
    """在 rendered manifest 裡塞一個 Secret 與一個帶密碼的 env，供「不外流」的斷言用。"""
    case["rendered_docs"][0]["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "ADMIN_PASSWORD", "value": _SECRET_MARKER}]
    case["rendered_docs"].append({
        "kind": "Secret", "metadata": {"name": "svc-secret"},
        "stringData": {"admin_password": _SECRET_MARKER}})
    return case


def test_result_and_detail_carry_no_manifest_content():
    result = evaluate(**_with_secrets(_incident()))
    assert _SECRET_MARKER not in json.dumps(result)
    assert _SECRET_MARKER not in json.dumps(build_error_detail(NS, RELEASE, result))


def test_unknown_carries_no_manifest_content():
    case = _with_secrets(_incident())
    case["rendered_docs"][0]["spec"]["replicas"] = _SECRET_MARKER
    with pytest.raises(Unknown) as err:
        evaluate(**case)
    assert err.value.reason == "replicas_unreadable"
    assert _SECRET_MARKER not in str(err.value)


# --- kubectl seam ------------------------------------------------------------------

@patch("src.services.helm.subprocess.run")
def test_snapshot_is_one_read_only_get_grouped_by_kind(mock_run):
    from src.services.helm import KubernetesService

    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = json.dumps({"items": [
        {"kind": "Pod", "metadata": {"name": "p"}},
        {"kind": "ResourceQuota", "metadata": {"name": "q"}},
        {"kind": "LimitRange", "metadata": {"name": "l"}},
        {"kind": "ConfigMap", "metadata": {"name": "ignored"}},
        "not-a-dict",
    ]})
    snapshot = KubernetesService().get_compute_quota_snapshot(NS)
    assert {k: [i["metadata"]["name"] for i in v] for k, v in snapshot.items()} == {
        "quotas": ["q"], "limitranges": ["l"], "pods": ["p"]}
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd[1:] == ["get", "resourcequotas,limitranges,pods", "--namespace", NS, "-o", "json"]
    assert mock_run.call_args.kwargs["timeout"] == 10


@patch("src.services.helm.subprocess.run")
def test_snapshot_of_an_empty_namespace(mock_run):
    from src.services.helm import KubernetesService

    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = ""
    assert KubernetesService().get_compute_quota_snapshot(NS) == {
        "quotas": [], "limitranges": [], "pods": []}


def test_snapshot_rejects_a_foreign_namespace():
    from src.services.helm import KubernetesService

    with pytest.raises(ValueError):
        KubernetesService().get_compute_quota_snapshot("kube-system")


# --- endpoint ----------------------------------------------------------------------

_INFO = ReleaseInfo(name=RELEASE, namespace=NS, revision=3, status=ReleaseStatus.DEPLOYED,
                    chart="omnigent", app_version="0.17", updated="2026-10-10T00:00:00Z")
_BODY = {"chart": "oci://registry/charts/omnigent", "version": "0.1.7", "reset_values": True,
         "values": {"resources": {"limits": {"cpu": "8"}}}}
_CHECKED = {**_BODY, "check_compute_quota": True}
_URL = f"/api/releases/{NS}/{RELEASE}"


def _dry_run_aware(manifest_docs):
    def upgrade(**kwargs):
        if kwargs.get("dry_run"):
            return yaml.safe_dump_all(manifest_docs)
        return _INFO
    return upgrade


def _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, case=None):
    """把一個 ``_incident`` 形狀接到三個 seam：helm dry-run、live workload、quota snapshot。"""
    case = case or _incident()
    mock_helm.upgrade.side_effect = _dry_run_aware(case["rendered_docs"])
    live_objects_lookup["objects"].return_value = case["live_items"]
    compute_quota_snapshot.return_value = {
        "quotas": case["quota_items"], "limitranges": case["limitrange_items"],
        "pods": case["pods"]}
    return case


def _dry_runs(mock_helm):
    return [c.kwargs.get("dry_run") for c in mock_helm.upgrade.call_args_list]


@patch("src.api.releases.helm_service")
def test_checked_upgrade_is_refused_before_anything_is_applied(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    r = client.patch(_URL, json=_CHECKED)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error_code"] == COMPUTE_QUOTA_ERROR_CODE
    assert detail["namespace"] == NS and detail["release"] == RELEASE
    assert detail["shortfalls"][0]["short_by"] == "4"
    compute_quota_snapshot.assert_called_once_with(NS)
    # 只跑了 dry-run——什麼都沒套用（舊 pod 還在）
    assert _dry_runs(mock_helm) == [True]


@patch("src.api.releases.helm_service")
def test_checked_upgrade_that_fits_is_applied(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident(new_cpu="4"))
    r = client.patch(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    assert _dry_runs(mock_helm) == [True, None]


@patch("src.api.releases.helm_service")
def test_without_the_flag_nothing_is_read_and_nothing_is_refused(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    """舊平台（不帶旗標）：同一個放不下的升級照舊送出，連 snapshot 都不讀。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    r = client.patch(_URL, json=_BODY)
    assert r.status_code == 200, r.text
    compute_quota_snapshot.assert_not_called()
    assert _dry_runs(mock_helm) == [True, None]


@patch("src.api.releases.helm_service")
def test_kill_switch_skips_the_check(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "compute_quota_precheck_enabled", False)
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    r = client.patch(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    compute_quota_snapshot.assert_not_called()


@patch("src.api.releases.helm_service")
def test_unreadable_snapshot_does_not_block(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, caplog):
    """ServiceAccount 讀不到 limitranges／kubectl 逾時＝不知道，放行（log 留下可 grep 的一行）。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    compute_quota_snapshot.side_effect = KubectlException(
        message="kubectl command failed with code 1", command="kubectl get",
        stderr='Error from server (Forbidden): limitranges is forbidden: User '
               '"system:serviceaccount:paas-system:paas-operator" cannot list resource')
    with caplog.at_level(logging.WARNING, logger="src.api.releases"):
        r = client.patch(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    assert "Compute quota pre-check skipped" in caplog.text
    assert _dry_runs(mock_helm) == [True, None]


@pytest.mark.parametrize("failure", [
    Unknown("selector_unreadable", "Deployment/svc-app"),
    RuntimeError("boom"),
    KeyError("spec"),
])
@patch("src.api.releases.helm_service")
def test_a_failing_check_never_blocks_the_upgrade(
        mock_helm, failure, client, live_objects_lookup, compute_quota_snapshot):
    """預檢自己的意外（資料對不起來、程式錯）絕不能變成擋升級。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    with patch.object(compute_quota, "evaluate", side_effect=failure) as evaluated:
        r = client.patch(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    evaluated.assert_called_once()
    assert _dry_runs(mock_helm) == [True, None]


@patch("src.api.releases.helm_service")
def test_malformed_snapshot_does_not_block(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    compute_quota_snapshot.return_value = {"quotas": "nonsense"}
    r = client.patch(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text


@patch("src.api.releases.helm_service")
def test_preflight_endpoint_reports_the_check_only_when_asked(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident(new_cpu="4"))
    plain = client.post(f"{_URL}/upgrade-preflight", json=_BODY)
    assert plain.status_code == 200
    assert plain.json() == {"ok": True, "expansions": []}, "不帶旗標的回應必須與原本逐字相同"
    compute_quota_snapshot.assert_not_called()

    checked = client.post(f"{_URL}/upgrade-preflight", json=_CHECKED)
    assert checked.status_code == 200
    body = checked.json()
    assert set(body) == {"ok", "expansions", "compute_quota"}
    assert body["compute_quota"] == {
        "status": "ok", "quota_source": "live", "advisories": [],
        "resources": {"limits.cpu": {
            "hard": "12", "used": "12", "release_current": "4", "other": "8",
            "required": "4", "available": "4"}}}
    assert _dry_runs(mock_helm) == [True, True], "預檢端點不套用任何東西"


@patch("src.api.releases.helm_service")
def test_preflight_endpoint_reports_the_denial(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    r = client.post(f"{_URL}/upgrade-preflight", json=_CHECKED)
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == COMPUTE_QUOTA_ERROR_CODE
    assert _dry_runs(mock_helm) == [True]


@patch("src.api.releases.helm_service")
def test_preflight_endpoint_reports_unknown_and_skipped_as_passes(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, monkeypatch):
    """``status`` 不是 ok（不知道／關掉）都不是失敗訊號：照樣 200。"""
    from src.config import settings

    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    compute_quota_snapshot.side_effect = KubectlException(
        message="kubectl command timed out after 10s", command="kubectl get", stderr="",
        timed_out=True)
    r = client.post(f"{_URL}/upgrade-preflight", json=_CHECKED)
    assert r.status_code == 200
    assert r.json()["compute_quota"] == {"status": "unknown", "reason": "snapshot_unreadable"}

    compute_quota_snapshot.side_effect = None
    live = live_objects_lookup["objects"].return_value
    live[0]["spec"]["selector"]["matchExpressions"] = [
        {"key": "tier", "operator": "Exists"}]
    r = client.post(f"{_URL}/upgrade-preflight", json=_CHECKED)
    assert r.status_code == 200
    assert r.json()["compute_quota"] == {"status": "unknown", "reason": "selector_unreadable"}

    monkeypatch.setattr(settings, "compute_quota_precheck_enabled", False)
    r = client.post(f"{_URL}/upgrade-preflight", json=_CHECKED)
    assert r.status_code == 200
    assert r.json()["compute_quota"] == {"status": "skipped", "reason": "disabled"}


@patch("src.api.releases.helm_service")
def test_preflight_endpoint_judges_by_the_pending_quota(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    """換方案扣款前：live 上限放不下、平台將推送的上限放得下 ⇒ 放行。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    pending = {"cpu_limit": "24", "memory_limit": "48Gi", "storage_limit": "100Gi",
               "ephemeral_storage_request": "4Gi", "ephemeral_storage_limit": "80Gi"}
    r = client.post(f"{_URL}/upgrade-preflight", json={**_CHECKED, "pending_quota": pending})
    assert r.status_code == 200, r.text
    compute = r.json()["compute_quota"]
    assert compute["quota_source"] == "pending"
    assert compute["resources"]["limits.cpu"]["hard"] == "24"

    still_small = {**pending, "cpu_limit": "14"}
    r = client.post(f"{_URL}/upgrade-preflight", json={**_CHECKED, "pending_quota": still_small})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["quota_source"] == "pending"
    assert detail["shortfalls"][0]["hard"] == "14"


@patch("src.api.releases.helm_service")
def test_patch_ignores_the_pending_quota(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    """``pending_quota`` 只有預檢端點認：真正的 upgrade 以叢集上現在的 quota 為準。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    pending = {"cpu_limit": "24", "memory_limit": "48Gi", "storage_limit": "100Gi"}
    r = client.patch(_URL, json={**_CHECKED, "pending_quota": pending})
    assert r.status_code == 409
    assert r.json()["detail"]["quota_source"] == "live"


@patch("src.api.releases.helm_service")
def test_helm_never_receives_the_check_arguments(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident(new_cpu="4"))
    pending = {"cpu_limit": "24", "memory_limit": "48Gi", "storage_limit": "100Gi"}
    client.post(f"{_URL}/upgrade-preflight", json={**_CHECKED, "pending_quota": pending})
    client.patch(_URL, json=_CHECKED)
    assert len(mock_helm.upgrade.call_args_list) == 3
    for call in mock_helm.upgrade.call_args_list:
        assert not {"check_compute_quota", "pending_quota"} & set(call.kwargs), call.kwargs


@patch("src.api.releases.helm_service")
def test_flag_does_not_change_what_helm_is_asked_to_do(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    """帶不帶旗標，送給 helm 的參數完全相同（預檢與真正 upgrade 仍是同一組）。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident(new_cpu="4"))
    client.patch(_URL, json=_BODY)
    client.patch(_URL, json=_CHECKED)
    plain_dry, plain_real, checked_dry, checked_real = mock_helm.upgrade.call_args_list
    assert plain_dry.kwargs == checked_dry.kwargs
    assert plain_real.kwargs == checked_real.kwargs


@patch("src.api.releases.helm_service")
def test_immutable_field_conflict_is_reported_first(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    """不可變欄位衝突是確定性的拒絕（字串 detail），先回報；這時不必再讀 quota。"""
    case = _incident()
    case["rendered_docs"][0]["spec"]["selector"] = {"matchLabels": {"app": "renamed"}}
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, case)
    r = client.patch(_URL, json=_CHECKED)
    assert r.status_code == 409
    assert isinstance(r.json()["detail"], str)
    assert "spec.selector.matchLabels" in r.json()["detail"]
    compute_quota_snapshot.assert_not_called()


@patch("src.api.releases.helm_service")
def test_unreadable_live_objects_skip_the_whole_preflight(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    live_objects_lookup["objects"].return_value = None
    r = client.post(f"{_URL}/upgrade-preflight", json=_CHECKED)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "expansions": [], "compute_quota": None}
    compute_quota_snapshot.assert_not_called()


@patch("src.api.releases.helm_service")
def test_refusal_leaks_no_manifest_content_to_the_response_or_the_log(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, caplog):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _with_secrets(_incident()))
    with caplog.at_level(logging.DEBUG, logger="src"):
        r = client.patch(_URL, json=_CHECKED)
    assert r.status_code == 409
    assert _SECRET_MARKER not in r.text
    assert _SECRET_MARKER not in caplog.text
    assert "Upgrade preflight refused" in caplog.text


@patch("src.api.releases.helm_service")
def test_pass_and_unexpected_failure_leak_no_manifest_content_to_the_log(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, caplog):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot,
             _with_secrets(_incident(new_cpu="4")))
    with caplog.at_level(logging.DEBUG, logger="src"):
        assert client.patch(_URL, json=_CHECKED).status_code == 200
        with patch.object(compute_quota, "quota_limits", side_effect=RuntimeError("boom")):
            assert client.patch(_URL, json=_CHECKED).status_code == 200
    assert "Compute quota pre-check passed" in caplog.text
    assert "failed unexpectedly" in caplog.text
    assert _SECRET_MARKER not in caplog.text


# --- rollback（保留憑證的回復＝一次 helm upgrade） ---------------------------------

def _history(*revisions):
    return [
        ReleaseRevision(
            revision=r, updated="2026-10-04T00:00:00Z",
            status=ReleaseStatus.DEPLOYED if r == max(revisions) else ReleaseStatus.SUPERSEDED,
            chart=f"omnigent-0.1.{r}", app_version="1", description="Upgrade complete",
        )
        for r in revisions
    ]


def _arrange_rollback(mock_helm, live_objects_lookup, compute_quota_snapshot, case):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, case)
    mock_helm.history.return_value = _history(10, 11, 12)
    mock_helm.get_revision_chart_version.return_value = "0.1.10"
    mock_helm.get_values.return_value = {"config": {"sensitive": {"admin_password": "x"}}}


_ROLLBACK = {"revision": 10, "chart": "oci://registry/charts/omnigent",
             "preserve_value_paths": ["config.sensitive"]}


@patch("src.api.releases.helm_service")
def test_preserving_rollback_is_checked_when_asked(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange_rollback(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident())
    r = client.post(f"{_URL}/rollback", json={**_ROLLBACK, "check_compute_quota": True})
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == COMPUTE_QUOTA_ERROR_CODE
    assert _dry_runs(mock_helm) == [True]
    mock_helm.rollback.assert_not_called()


@patch("src.api.releases.helm_service")
def test_preserving_rollback_without_the_flag_is_unchanged(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange_rollback(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident())
    r = client.post(f"{_URL}/rollback", json=_ROLLBACK)
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "upgrade_preserving"
    compute_quota_snapshot.assert_not_called()


@patch("src.api.releases.helm_service")
def test_plain_rollback_is_not_checked(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    """純 helm rollback 重放 stored manifest，沒有 dry-run 可算——旗標在這條路上不生效。"""
    _arrange_rollback(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident())
    mock_helm.rollback.return_value = {"message": "rolled back"}
    r = client.post(f"{_URL}/rollback", json={"revision": 10, "check_compute_quota": True})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "rollback"
    compute_quota_snapshot.assert_not_called()
    mock_helm.upgrade.assert_not_called()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from src.main import app

    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})
