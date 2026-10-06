"""#1415：開通前的叢集儲存（Longhorn）排程額度預檢與唯讀額度回報。

fixture ``tests/fixtures/longhorn_capacity_20261004.json`` 是正式站 2026-10-04 15:36Z 唯讀
``kubectl get`` 的 nodes.longhorn.io／settings.longhorn.io／StorageClass（裁剪成排程需要的
欄位，見 ``_source``）。10-04 事件當下（01:17Z）與清孤兒之後的 storageScheduled 取自
#1415 issue 內文的表，套在同一份真實節點資料上重算。
"""
import copy
import json
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import ReleaseInfo, ReleaseStatus
from src.services.helm import HelmException, HelmService, KubectlException, KubernetesService
from src.services.storage_capacity import (
    CAPACITY_ERROR_CODE,
    GIB,
    CapacityDataError,
    LonghornCluster,
    LonghornSettings,
    StorageClassInfo,
    VolumeRequest,
    build_capacity_error_detail,
    capacity_report,
    cdi_size_with_overhead,
    collect_volume_requests,
    format_gib,
    is_schedulable,
    largest_placeable_volume,
    longhorn_volume_size,
    parse_storage_classes,
    simulate,
    summarize_orphans,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "longhorn_capacity_20261004.json").read_text())
NS = "paas-ws-test"

#: #1415 issue 表：事件當下／清孤兒後各節點已預訂（GiB）。
EVENT_SCHEDULED = {"31zix": 1240, "59mqj": 1267, "pggrg": 1255, "z0v3w": 1317}
AFTER_CLEANUP_SCHEDULED = {"31zix": 963, "59mqj": 1127, "pggrg": 930, "z0v3w": 851}


def _snapshot(scheduled_gib=None, mutate=None):
    data = copy.deepcopy(FIXTURE)
    for node in data["nodes"]:
        short = node["metadata"]["name"].rsplit("-", 1)[-1]
        for disk in node["status"]["diskStatus"].values():
            if scheduled_gib is not None:
                disk["storageScheduled"] = int(scheduled_gib[short] * GIB)
    if mutate:
        mutate(data)
    return {"nodes": data["nodes"], "settings": data["settings"],
            "storage_classes": data["storage_classes"]}


def _cluster(snapshot):
    return LonghornCluster.from_snapshot(snapshot["nodes"], snapshot["settings"])


def _classes(snapshot=None):
    return parse_storage_classes((snapshot or FIXTURE)["storage_classes"])


def _pvc_doc(name, size, sc="longhorn-delete"):
    spec = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}}
    if sc is not None:
        spec["storageClassName"] = sc
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name}, "spec": spec}


def _requests(docs, snapshot=None, existing=(), overheads=None):
    snapshot = snapshot or _snapshot()
    classes, default = _classes(snapshot)
    return collect_volume_requests(docs, classes, default, _cluster(snapshot).settings,
                                   existing, overheads)


NEXTCLOUD_DATA = _pvc_doc("svc-x-nextcloud-data", "80Gi")


# ── 真實 fixture 與 10-04 事件重算 ──────────────────────────────────────


class TestProductionFixture:
    def test_settings_and_default_class_from_the_real_cluster(self):
        snap = _snapshot()
        settings = _cluster(snap).settings
        assert settings.over_provisioning_percentage == 200
        assert settings.minimal_available_percentage == 25
        assert settings.replica_soft_anti_affinity is False
        assert settings.allow_degraded_creation is True
        assert settings.default_replica_count == 3  # {"v1":"3","v2":"3"}
        classes, default = _classes(snap)
        # local-path 與 longhorn 都標 default，k8s 取最新建立的 longhorn。
        assert default == "longhorn"
        assert classes["longhorn-delete"].replicas == 2
        assert classes["longhorn-delete"].is_longhorn
        assert not classes["local-path"].is_longhorn

    def test_per_node_limit_is_659gib_times_200_percent(self):
        nodes = _cluster(_snapshot()).node_summaries()
        assert [n["name"] for n in nodes] == [
            "ubuntuserver-31zix", "ubuntuserver-59mqj", "ubuntuserver-pggrg", "ubuntuserver-z0v3w"]
        assert {n["limit"] for n in nodes} == {"1317.2Gi"}
        assert all(n["schedulable"] for n in nodes)

    def test_incident_80gi_does_not_fit_and_fits_after_orphan_cleanup(self):
        """10-04 事件：80Gi×2 一個 replica 都排不上 ⇒ 擋；清孤兒後兩台各放一個 ⇒ 放行。"""
        event = _snapshot(EVENT_SCHEDULED)
        placement = simulate(_cluster(event), _requests([NEXTCLOUD_DATA], event))[0]
        assert placement.placed == 0
        assert not is_schedulable(placement, _cluster(event).settings)

        after = _snapshot(AFTER_CLEANUP_SCHEDULED)
        placement = simulate(_cluster(after), _requests([NEXTCLOUD_DATA], after))[0]
        assert placement.placed == 2
        assert len(set(placement.nodes)) == 2, "replica-soft-anti-affinity=false：兩台不同節點"
        assert is_schedulable(placement, _cluster(after).settings)

    def test_incident_largest_placeable_volume(self):
        event = _cluster(_snapshot(EVENT_SCHEDULED))
        sc = _classes()[0]["longhorn-delete"]
        # 有備援（2 台）：第二大的餘裕 pggrg 1317.2−1255＝62.2Gi；degraded：31zix 77.2Gi。
        assert format_gib(largest_placeable_volume(event, sc)) == "62.2Gi"
        assert format_gib(largest_placeable_volume(event, sc, full_replicas=False)) == "77.2Gi"

    def test_incident_error_detail(self):
        event = _snapshot(EVENT_SCHEDULED)
        cluster = _cluster(event)
        placements = simulate(cluster, _requests([NEXTCLOUD_DATA], event))
        detail = build_capacity_error_detail(NS, cluster, placements, orphans={"count": 55})
        assert detail["error_code"] == CAPACITY_ERROR_CODE
        assert detail["unschedulable"] == [{
            "name": "svc-x-nextcloud-data", "kind": "PersistentVolumeClaim", "size": "80Gi",
            "size_bytes": 80 * GIB, "storage_class": "longhorn-delete", "replicas": 2,
            "replicas_placeable": 0,
        }]
        assert detail["largest_placeable_volume"] == "77.2Gi"
        assert [n["headroom"] for n in detail["nodes"]] == ["77.2Gi", "50.2Gi", "62.2Gi", "0.2Gi"]
        assert detail["orphaned_pvcs"] == {"count": 55}
        assert "80Gi x2" in detail["message"]

    def test_current_state_report(self):
        snap = _snapshot()
        classes, default = _classes(snap)
        report = capacity_report(_cluster(snap), classes, default, "longhorn-delete")
        assert report["available"] is True
        assert report["replicas"] == 2
        assert report["largest_placeable_volume"] == "403.7Gi"
        assert report["largest_placeable_volume_degraded"] == "500.5Gi"
        assert report["max_scheduled_ratio"] == pytest.approx(0.8558, abs=1e-4)


# ── Longhorn 排程公式（IsSchedulableToDisk 等） ─────────────────────────


def _one_disk_cluster(maximum=100 * GIB, available=80 * GIB, scheduled=0, reserved=0,
                      nodes=("a", "b"), settings=None, zone="", **disk_overrides):
    node_items = []
    for name in nodes:
        disk_spec = {"allowScheduling": True, "evictionRequested": False,
                     "storageReserved": reserved, "tags": [], "diskType": "filesystem"}
        disk_status = {"storageMaximum": maximum, "storageAvailable": available,
                       "storageScheduled": scheduled, "diskUUID": f"uuid-{name}",
                       "conditions": [{"type": "Schedulable", "status": "True"}]}
        disk_spec.update({k: v for k, v in disk_overrides.items() if k in disk_spec})
        node_items.append({
            "metadata": {"name": name},
            "spec": {"allowScheduling": True, "tags": [], "disks": {"d": disk_spec}},
            "status": {"zone": zone,
                       "conditions": [{"type": "Ready", "status": "True"},
                                      {"type": "Schedulable", "status": "True"}],
                       "diskStatus": {"d": disk_status}},
        })
    setting_items = [{"metadata": {"name": k}, "value": v} for k, v in (settings or {}).items()]
    return LonghornCluster.from_snapshot(node_items, setting_items)


def _vol(size, replicas=2, **sc_kwargs):
    sc = StorageClassInfo(name="lh", provisioner="driver.longhorn.io", replicas=replicas,
                          **sc_kwargs)
    return VolumeRequest(name=f"v{size}", kind="PersistentVolumeClaim", requested_bytes=size,
                         size_bytes=size, storage_class=sc, replicas=replicas)


class TestSchedulingFormula:
    def test_exactly_at_the_over_provisioning_limit_fits(self):
        # limit＝(100−0)×100%＝100Gi；已預訂 60Gi ⇒ 40Gi 剛好、40Gi＋2MiB 不行。
        cluster = _one_disk_cluster(scheduled=60 * GIB)
        assert simulate(cluster, [_vol(40 * GIB)])[0].placed == 2
        assert simulate(cluster, [_vol(40 * GIB + 2 * 1024 ** 2)])[0].placed == 0

    def test_reserved_and_over_provisioning_percentage(self):
        cluster = _one_disk_cluster(reserved=10 * GIB,
                                    settings={"storage-over-provisioning-percentage": "200"})
        # (100−10)×200%＝180Gi
        assert cluster.node_summaries()[0]["limit_bytes"] == 180 * GIB
        assert simulate(cluster, [_vol(180 * GIB)])[0].placed == 2

    def test_minimal_available_condition(self):
        # available 必須 > 25%×max（新 volume ActualSize＝0）；等於就不行。
        assert simulate(_one_disk_cluster(available=25 * GIB), [_vol(GIB)])[0].placed == 0
        assert simulate(_one_disk_cluster(available=25 * GIB + 1), [_vol(GIB)])[0].placed == 2

    @pytest.mark.parametrize("mutate", [
        lambda n: n["spec"].update(allowScheduling=False),
        lambda n: n["status"].update(conditions=[{"type": "Ready", "status": "False"},
                                                 {"type": "Schedulable", "status": "True"}]),
        lambda n: n["spec"]["disks"]["d"].update(allowScheduling=False),
        lambda n: n["spec"]["disks"]["d"].update(evictionRequested=True),
        lambda n: n["status"]["diskStatus"]["d"].update(
            conditions=[{"type": "Schedulable", "status": "False"}]),
        lambda n: n["metadata"].update(deletionTimestamp="2026-10-04T00:00:00Z"),
    ])
    def test_unschedulable_node_or_disk_is_skipped(self, mutate):
        # 兩台一樣的節點，第一台套用 mutate。
        items = []
        for name in ("a", "b"):
            items.append({
                "metadata": {"name": name},
                "spec": {"allowScheduling": True, "tags": [], "disks": {"d": {
                    "allowScheduling": True, "evictionRequested": False,
                    "storageReserved": 0, "tags": [], "diskType": "filesystem"}}},
                "status": {"zone": "", "conditions": [
                    {"type": "Ready", "status": "True"}, {"type": "Schedulable", "status": "True"}],
                    "diskStatus": {"d": {"storageMaximum": 100 * GIB, "storageAvailable": 80 * GIB,
                                         "storageScheduled": 0, "diskUUID": f"u-{name}",
                                         "conditions": [{"type": "Schedulable", "status": "True"}]}}},
            })
        mutate(items[0])
        cluster = LonghornCluster.from_snapshot(items, [])
        placement = simulate(cluster, [_vol(GIB)])[0]
        assert placement.nodes == ["b"]
        # 預設 allow-volume-creation-with-degraded-availability＝true：一個 replica 就算 Scheduled。
        assert is_schedulable(placement, cluster.settings)

    def test_degraded_creation_disabled_requires_every_replica(self):
        cluster = _one_disk_cluster(
            nodes=("a",), settings={"allow-volume-creation-with-degraded-availability": "false"})
        placement = simulate(cluster, [_vol(GIB)])[0]
        assert placement.placed == 1
        assert not is_schedulable(placement, cluster.settings)

    def test_node_hard_anti_affinity_needs_distinct_nodes(self):
        cluster = _one_disk_cluster(nodes=("a",))
        assert simulate(cluster, [_vol(GIB)])[0].placed == 1

    def test_node_soft_anti_affinity_allows_same_node(self):
        cluster = _one_disk_cluster(nodes=("a",), settings={"replica-soft-anti-affinity": "true"})
        assert simulate(cluster, [_vol(GIB)])[0].nodes == ["a", "a"]
        # StorageClass 參數可覆寫全域設定（Longhorn CSI replicaSoftAntiAffinity）。
        hard = _one_disk_cluster(nodes=("a",))
        assert simulate(hard, [_vol(GIB, soft_anti_affinity=True)])[0].placed == 2

    def test_disk_hard_anti_affinity_blocks_second_replica_on_same_disk(self):
        cluster = _one_disk_cluster(nodes=("a",), settings={
            "replica-soft-anti-affinity": "true", "replica-disk-soft-anti-affinity": "false"})
        assert simulate(cluster, [_vol(GIB)])[0].placed == 1

    def test_zone_hard_anti_affinity_treats_empty_zone_as_one_zone(self):
        cluster = _one_disk_cluster(settings={"replica-zone-soft-anti-affinity": "false"})
        assert simulate(cluster, [_vol(GIB)])[0].placed == 1

    def test_selectors_must_match_tags(self):
        cluster = _one_disk_cluster()
        cluster.nodes[0].tags = ("ssd",)
        placement = simulate(cluster, [_vol(GIB, node_selector=("ssd",))])[0]
        assert placement.nodes == ["a"]
        # allow-empty-node-selector-volume=false：沒有 selector 的 volume 不能放到有 tag 的節點。
        strict = _one_disk_cluster(settings={"allow-empty-node-selector-volume": "false"})
        strict.nodes[0].tags = ("ssd",)
        assert simulate(strict, [_vol(GIB)])[0].nodes == ["b"]

    def test_multiple_volumes_consume_each_others_headroom(self):
        # 每台 limit 100Gi：60Gi×2 放得下，再一顆 60Gi×2 就沒地方了。
        cluster = _one_disk_cluster()
        placements = simulate(cluster, [_vol(60 * GIB), _vol(60 * GIB + 2 * 1024 ** 2)])
        assert [p.placed for p in placements] == [0, 2], "大的先放"
        assert cluster.node_summaries()[0]["scheduled_bytes"] == 0, "模擬不改原模型"

    def test_bad_numbers_are_data_errors(self):
        node = {"metadata": {"name": "a"}, "spec": {"disks": {"d": {"storageReserved": "x"}}},
                "status": {"diskStatus": {"d": {"storageMaximum": 1, "storageAvailable": 1,
                                                "storageScheduled": 0}}}}
        with pytest.raises(CapacityDataError):
            LonghornCluster.from_snapshot([node], [])
        with pytest.raises(CapacityDataError):
            LonghornSettings.from_items([{"metadata": {"name": "storage-over-provisioning-percentage"},
                                          "value": "lots"}])


# ── 從 manifest 找出要新建的 volume ─────────────────────────────────────


class TestCollectVolumeRequests:
    def test_explicit_default_empty_and_foreign_classes(self):
        docs = [
            _pvc_doc("explicit", "10Gi", "longhorn-delete"),
            _pvc_doc("default", "5Gi", None),          # 沒寫 ⇒ 叢集預設 longhorn
            _pvc_doc("no-class", "5Gi", ""),           # "" ⇒ 不動態佈建
            _pvc_doc("local", "5Gi", "local-path"),    # 非 Longhorn
            _pvc_doc("unknown", "5Gi", "nope"),
            {"kind": "ConfigMap", "metadata": {"name": "x"}},
        ]
        reqs = _requests(docs)
        assert [(r.name, r.storage_class.name, r.replicas) for r in reqs] == [
            ("explicit", "longhorn-delete", 2), ("default", "longhorn", 2)]

    def test_statefulset_volume_claim_templates_expand_per_ordinal(self):
        sts = {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "svc-pg"},
               "spec": {"replicas": 2, "volumeClaimTemplates": [{
                   "metadata": {"name": "data"},
                   "spec": {"storageClassName": "longhorn-delete",
                            "resources": {"requests": {"storage": "8Gi"}}}}]}}
        assert [r.name for r in _requests([sts])] == ["data-svc-pg-0", "data-svc-pg-1"]

    def test_existing_pvcs_are_not_counted_again(self):
        reqs = _requests([NEXTCLOUD_DATA, _pvc_doc("other", "1Gi")],
                         existing=["svc-x-nextcloud-data"])
        assert [r.name for r in reqs] == ["other"]

    def test_datavolume_storage_api_gets_cdi_filesystem_overhead(self):
        """HAOS：宣告 64Gi，CDI 渲染成 72843526144 B（#423 實測 quota 拒收訊息的數字）。"""
        dv = {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
              "metadata": {"name": "svc-haos-root"},
              "spec": {"storage": {"volumeMode": "Filesystem", "storageClassName": "longhorn-delete",
                                   "resources": {"requests": {"storage": "64Gi"}}}}}
        overheads = {"global": "0.06", "storageClass": {"longhorn-delete": "0.06"}}
        (req,) = _requests([dv], overheads=overheads)
        assert req.kind == "DataVolume"
        assert req.requested_bytes == 72843526144
        assert req.size_bytes == 72844574720  # Longhorn 再對齊 2 MiB
        block = copy.deepcopy(dv)
        block["spec"]["storage"]["volumeMode"] = "Block"
        assert _requests([block], overheads=overheads)[0].size_bytes == 64 * GIB
        legacy = {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
                  "metadata": {"name": "dv"}, "spec": {"pvc": dv["spec"]["storage"]}}
        assert _requests([legacy])[0].size_bytes == 64 * GIB

    def test_sizes_are_aligned_like_longhorn_csi(self):
        assert longhorn_volume_size(1) == 2 * 1024 ** 2
        assert longhorn_volume_size(10 ** 9) == 1000341504  # 1G → 2MiB 對齊
        assert cdi_size_with_overhead(64 * GIB, 0.06) == 72843526144

    def test_missing_number_of_replicas_uses_longhorn_default(self):
        reqs = _requests([_pvc_doc("static", "1Gi", "longhorn-static")])
        assert reqs[0].replicas == 3  # default-replica-count {"v1":"3",...}


class TestOrphanSummary:
    def test_counts_longhorn_scheduled_bytes_times_replicas(self):
        classes, _ = _classes()
        settings = _cluster(_snapshot()).settings

        def pvc(ns, size, sc, orphan=True):
            labels = {"paas.woowtech.io/orphaned-at": "1759500000"} if orphan else {}
            return {"metadata": {"namespace": ns, "labels": labels},
                    "spec": {"storageClassName": sc, "resources": {"requests": {"storage": size}}}}

        summary = summarize_orphans([
            pvc("paas-ws-a", "10Gi", "longhorn-delete"), pvc("paas-ws-a", "8Gi", "longhorn"),
            pvc("paas-ws-b", "5Gi", "local-path"), pvc("paas-ws-b", "99Gi", "longhorn", orphan=False),
        ], classes, settings)
        assert summary["count"] == 3
        assert summary["namespaces"] == 2
        assert summary["requested"] == "23Gi"
        assert summary["longhorn_scheduled"] == "36Gi"  # (10+8)×2，local-path 不算
        assert summary["oldest_orphaned_at_epoch"] == 1759500000


# ── HelmService.install(dry_run=True) ───────────────────────────────────


class TestInstallDryRun:
    @patch("subprocess.run")
    def test_dry_run_renders_without_creating_and_returns_manifest_with_hooks(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stderr="", stdout=json.dumps({
            "manifest": "kind: PersistentVolumeClaim\nmetadata: {name: a}\n",
            "hooks": [{"manifest": "kind: Job\nmetadata: {name: j}\n"}],
        }))
        out = HelmService().install(namespace=NS, name="svc", chart="oci://example.invalid/c",
                                    version="1.0.0", create_namespace=True, dry_run=True,
                                    subprocess_timeout_seconds=45)
        args = mock_run.call_args[0][0]
        assert "--dry-run" in args
        assert "--create-namespace" not in args
        assert mock_run.call_args.kwargs["timeout"] == 45
        assert out == ("kind: PersistentVolumeClaim\nmetadata: {name: a}\n"
                       "\n---\nkind: Job\nmetadata: {name: j}\n")


class TestKubectlReads:
    def test_capacity_snapshot_is_read_only_and_drops_unrelated_settings(self):
        svc = KubernetesService()
        calls = []
        outputs = {
            "nodes.longhorn.io": {"items": [{"metadata": {"name": "n"}}]},
            "settings.longhorn.io": {"items": [
                {"metadata": {"name": "storage-over-provisioning-percentage"}, "value": "200"},
                {"metadata": {"name": "registry-secret"}, "value": "x"}]},
            "storageclasses": {"items": [{"metadata": {"name": "sc"}}]},
        }

        def fake_run(args, **kwargs):
            calls.append(args)
            out = MagicMock()
            out.stdout = json.dumps(outputs[args[1]])
            return out

        with patch.object(svc, "_run_command", side_effect=fake_run):
            snap = svc.get_longhorn_capacity_snapshot()
        assert [c[0] for c in calls] == ["get", "get", "get"]
        assert calls[0][:4] == ["get", "nodes.longhorn.io", "--namespace", "longhorn-system"]
        assert [s["metadata"]["name"] for s in snap["settings"]] == [
            "storage-over-provisioning-percentage"]

    def test_orphan_list_is_label_scoped_and_read_only(self):
        svc = KubernetesService()
        with patch.object(svc, "_run_command",
                          return_value=MagicMock(stdout='{"items": []}')) as run:
            assert svc.list_orphaned_pvcs_all_namespaces() == []
        args = run.call_args[0][0]
        assert args[:3] == ["get", "pvc", "--all-namespaces"]
        assert "paas.woowtech.io/orphaned-at" in args


# ── POST /api/releases 的 409 與放行 ────────────────────────────────────


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def _release():
    return ReleaseInfo(name="svc-x", namespace=NS, revision=1, status=ReleaseStatus.DEPLOYED,
                       chart="nextcloud", app_version="1", updated="2026-10-04T00:00:00Z")


def _post(client):
    return client.post("/api/releases", json={
        "namespace": NS, "name": "svc-x", "chart": "oci://example.invalid/nextcloud",
        "version": "0.1.4", "values": {"persistence": {"storageClass": "longhorn-delete"}},
        "sensitive_values": {"admin": {"password": "s3cret"}}})


def _install_side_effect(manifest):
    def install(**kwargs):
        if kwargs.get("dry_run"):
            return manifest
        return _release()
    return install


NEXTCLOUD_MANIFEST = json.dumps(NEXTCLOUD_DATA)  # JSON 是合法 YAML


class TestCreateReleaseCapacityGate:
    @patch("src.api.releases.k8s_service.list_orphaned_pvcs_all_namespaces", return_value=[])
    @patch("src.api.releases.k8s_service.list_pvcs", return_value=[])
    @patch("src.api.releases.helm_service")
    def test_incident_returns_409_before_any_cluster_write(
        self, mock_helm, _pvcs, _orphans, client, longhorn_capacity_snapshot,
        sa_pull_secret_convergence,
    ):
        longhorn_capacity_snapshot.return_value = _snapshot(EVENT_SCHEDULED)
        mock_helm.install.side_effect = _install_side_effect(NEXTCLOUD_MANIFEST)
        resp = _post(client)

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == CAPACITY_ERROR_CODE
        assert detail["unschedulable"][0]["name"] == "svc-x-nextcloud-data"
        assert detail["orphaned_pvcs"]["count"] == 0
        # 只渲染過一次（dry-run），沒有真正 install、沒有 patch SA。
        assert [c.kwargs.get("dry_run") for c in mock_helm.install.call_args_list] == [True]
        sa_pull_secret_convergence.assert_not_called()

    @patch("src.api.releases.k8s_service.list_pvcs", return_value=[])
    @patch("src.api.releases.helm_service")
    def test_dry_run_uses_the_same_chart_version_and_values(
        self, mock_helm, _pvcs, client, longhorn_capacity_snapshot,
    ):
        longhorn_capacity_snapshot.return_value = _snapshot(AFTER_CLEANUP_SCHEDULED)
        mock_helm.install.side_effect = _install_side_effect(NEXTCLOUD_MANIFEST)
        assert _post(client).status_code == 201
        dry, real = mock_helm.install.call_args_list
        assert dry.kwargs["dry_run"] is True
        for key in ("namespace", "name", "chart", "version", "values", "sensitive_values"):
            assert dry.kwargs[key] == real.kwargs[key], key
        assert dry.kwargs["values"]["imagePullSecrets"]

    @patch("src.api.releases.helm_service")
    def test_unreadable_longhorn_fails_open_without_rendering(
        self, mock_helm, client, longhorn_capacity_snapshot,
    ):
        longhorn_capacity_snapshot.side_effect = KubectlException(
            message="boom", command="kubectl get nodes.longhorn.io",
            stderr='Error from server (Forbidden): nodes.longhorn.io is forbidden')
        mock_helm.install.return_value = _release()
        assert _post(client).status_code == 201
        mock_helm.install.assert_called_once()
        assert not mock_helm.install.call_args.kwargs.get("dry_run")

    @patch("src.api.releases.helm_service")
    def test_render_failure_fails_open(self, mock_helm, client, longhorn_capacity_snapshot):
        longhorn_capacity_snapshot.return_value = _snapshot(EVENT_SCHEDULED)

        def install(**kwargs):
            if kwargs.get("dry_run"):
                raise HelmException("timed out", "helm install", "timed out")
            return _release()
        mock_helm.install.side_effect = install
        assert _post(client).status_code == 201

    @patch("src.api.releases.k8s_service.list_pvcs", return_value=["svc-x-nextcloud-data"])
    @patch("src.api.releases.helm_service")
    def test_retry_with_existing_pvc_is_not_counted_again(
        self, mock_helm, _pvcs, client, longhorn_capacity_snapshot,
    ):
        longhorn_capacity_snapshot.return_value = _snapshot(EVENT_SCHEDULED)
        mock_helm.install.side_effect = _install_side_effect(NEXTCLOUD_MANIFEST)
        assert _post(client).status_code == 201

    @patch("src.api.releases.k8s_service.list_pvcs", return_value=[])
    @patch("src.api.releases.helm_service")
    def test_non_longhorn_volumes_are_not_gated(
        self, mock_helm, _pvcs, client, longhorn_capacity_snapshot,
    ):
        longhorn_capacity_snapshot.return_value = _snapshot(EVENT_SCHEDULED)
        mock_helm.install.side_effect = _install_side_effect(
            json.dumps(_pvc_doc("svc-x-cache", "500Gi", "local-path")))
        assert _post(client).status_code == 201

    @patch("src.api.releases.helm_service")
    def test_unexpected_precheck_errors_fail_open(
        self, mock_helm, client, longhorn_capacity_snapshot,
    ):
        longhorn_capacity_snapshot.side_effect = RuntimeError("surprise")
        mock_helm.install.return_value = _release()
        assert _post(client).status_code == 201

    @patch("src.api.releases.helm_service")
    def test_kill_switch(self, mock_helm, client, longhorn_capacity_snapshot):
        mock_helm.install.return_value = _release()
        with patch("src.api.releases.settings.storage_capacity_precheck_enabled", False):
            assert _post(client).status_code == 201
        longhorn_capacity_snapshot.assert_not_called()

    @patch("src.api.releases.k8s_service.list_pvcs", return_value=[])
    @patch("src.api.releases.helm_service")
    def test_workspace_quota_check_runs_first(
        self, mock_helm, _pvcs, client, storage_quota_snapshot, longhorn_capacity_snapshot,
    ):
        """#1348 的工作區配額已滿時直接回那個錯誤碼，不必再讀 Longhorn／渲染。"""
        storage_quota_snapshot.return_value = {"quotas": [{
            "metadata": {"name": "q"},
            "status": {"hard": {"persistentvolumeclaims": "1"},
                       "used": {"persistentvolumeclaims": "1"}}}], "pvcs": []}
        resp = _post(client)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "workspace_storage_quota_exhausted"
        longhorn_capacity_snapshot.assert_not_called()
        mock_helm.install.assert_not_called()


# ── GET /api/cluster/storage-capacity ───────────────────────────────────


class TestStorageCapacityEndpoint:
    @patch("src.api.cluster.k8s_service.list_orphaned_pvcs_all_namespaces", return_value=[])
    def test_report(self, _orphans, client, longhorn_capacity_snapshot):
        longhorn_capacity_snapshot.return_value = _snapshot(EVENT_SCHEDULED)
        body = client.get("/api/cluster/storage-capacity",
                          params={"storage_class": "longhorn-delete"}).json()
        assert body["available"] is True
        assert body["storage_class"] == "longhorn-delete"
        assert body["largest_placeable_volume"] == "62.2Gi"
        assert body["largest_placeable_volume_degraded"] == "77.2Gi"
        assert body["max_scheduled_ratio"] == pytest.approx(1317 / 1317.16, abs=1e-3)
        assert body["orphaned_pvcs"]["count"] == 0
        assert len(body["nodes"]) == 4

    def test_default_storage_class_when_not_given(self, client, longhorn_capacity_snapshot):
        longhorn_capacity_snapshot.return_value = _snapshot()
        with patch("src.api.cluster.k8s_service.list_orphaned_pvcs_all_namespaces",
                   side_effect=KubectlException("x", "kubectl", "forbidden")):
            body = client.get("/api/cluster/storage-capacity").json()
        assert body["storage_class"] == "longhorn"
        assert body["orphaned_pvcs"] is None, "孤兒讀不到不影響其他回報"

    def test_unknown_storage_class(self, client, longhorn_capacity_snapshot):
        longhorn_capacity_snapshot.return_value = _snapshot()
        with patch("src.api.cluster.k8s_service.list_orphaned_pvcs_all_namespaces",
                   return_value=[]):
            body = client.get("/api/cluster/storage-capacity",
                              params={"storage_class": "local-path"}).json()
        assert body["storage_class_error"] == "not_longhorn"
        assert body["largest_placeable_volume_bytes"] is None

    def test_no_longhorn_is_a_200_with_reason(self, client, longhorn_capacity_snapshot):
        longhorn_capacity_snapshot.side_effect = KubectlException(
            "x", "kubectl", 'error: the server doesn\'t have a resource type "nodes"')
        resp = client.get("/api/cluster/storage-capacity")
        assert resp.status_code == 200
        assert resp.json() == {"available": False, "reason": "longhorn_unreadable"}

    def test_empty_cluster(self, client):
        body = client.get("/api/cluster/storage-capacity").json()
        assert body == {"available": False, "reason": "no_longhorn_nodes"}

    def test_requires_api_key(self):
        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "test-api-key"
            resp = TestClient(app).get("/api/cluster/storage-capacity")
        assert resp.status_code == 401
