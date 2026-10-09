"""woow_paas_platform #1540：停權／刪除後的限時唯讀資料取回入口。

本檔釘住：

* **唯讀**：PVC 在 volume 與 volumeMount 兩層都是 readOnly；nginx 只收 GET（HEAD）；容器根目錄唯讀。
* **身分**：非 root（Nextcloud＝uid/gid 33）、PSA restricted 的欄位都在、**沒有 fsGroup**、不掛 SA token。
* **不混進 release**：取回物件不帶 ``app.kubernetes.io/instance``（scale／status／unpark 的自動偵測不會撿到）。
* **密碼只出現一次**：Secret 只有 ``{SSHA}`` 雜湊；回應有明文。
* **前置條件**：服務還在跑、PVC 還掛著、已有入口、保留期快到、profile 不認得 —— 一律 4xx＋error_code，
  **沒有任何一條路回 404**（平台把 404 當成舊 operator）。
* **限時**：TTL 上限、已刪除服務截到 PVC 到期、``activeDeadlineSeconds``。
* **route**：只在 tunnel 上沒有規則的 hostname 建；失敗回滾 k8s 物件；關閉先刪 k8s 再刪 route；
  只刪指向本入口 Service 的規則；舊的取回 route 在新開時清掉。
* **sweep** 只收本 operator 前綴＋env 的過期入口。

Cloudflare 一律假在 httpx 的 GET／PUT（走真的 ``get_tunnel_config``／``update_tunnel_config``）；
kubectl 一律 mock。
"""
import base64
import copy
import hashlib
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api import retrieval
from src.main import app
from src.services import data_retrieval as dr
from src.services.helm import KubectlException

NS = "paas-ws-abc12345"
REL = "svc-1540abcd"
PVC = f"{REL}-nextcloud-data"
SVC_URL = f"http://paas-retrieval-{REL}.{NS}.svc.cluster.local:8080"
CATCH_ALL = {"service": "http_status:404"}
NOW = 1_800_000_000


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _pvc(name, component="app", labels=None):
    return {"metadata": {"name": name, "labels": {
        "app.kubernetes.io/instance": REL, "app.kubernetes.io/component": component,
        **(labels or {})}}}


def _nextcloud_pvcs(extra_labels=None):
    return [
        _pvc(f"{REL}-nextcloud-html", labels=extra_labels),
        _pvc(PVC, labels=extra_labels),
        _pvc(f"{REL}-nextcloud-db", component="postgresql", labels=extra_labels),
        _pvc(f"{REL}-nextcloud-redis", component="redis", labels=extra_labels),
    ]


def _workload(kind, name, replicas):
    return {"kind": kind, "metadata": {"name": name}, "spec": {"replicas": replicas}}


def _pod(name, claim=None, phase="Running", ready=False, labels=None):
    volumes = [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}] if claim else []
    return {
        "kind": "Pod",
        "metadata": {"name": name, "labels": labels or {}},
        "spec": {"volumes": volumes},
        "status": {"phase": phase, "conditions": [
            {"type": "Ready", "status": "True" if ready else "False"}]},
    }


def _anchor(namespace=NS, release=REL, expires_at=NOW + 3600, subdomain="paas-dl-0123456789ab"):
    return {"kind": "Secret", "metadata": {
        "name": dr.resource_name(release), "namespace": namespace,
        "labels": {**dr.selector_labels(release), dr.LABEL_MANAGED_BY: dr.MANAGED_BY},
        "annotations": {
            dr.ANN_EXPIRES_AT: str(expires_at), dr.ANN_OPENED_AT: str(NOW - 60),
            dr.ANN_SUBDOMAIN: subdomain or "", dr.ANN_PVC: PVC, dr.ANN_PROFILE: "nextcloud",
        }}}


class FakeK8s:
    """``retrieval.k8s_service`` 的替身：get_items 依 (kinds, selector) 回資料，寫入動作都記下來。"""

    def __init__(self, *, anchor=None, leftovers=(), pvcs=None, workloads=(), pods=(),
                 release_exists=True, own_pods=()):
        self.anchor = anchor
        self.leftovers = list(leftovers)
        self.pvcs = _nextcloud_pvcs() if pvcs is None else pvcs
        self.workloads = list(workloads)
        self.pods = list(pods)
        self.own_pods = list(own_pods)
        self.release_exists = release_exists
        self.created = []
        self.deleted = []
        self.calls = []
        self.create_error = None   # (index, KubectlException)
        self.sweep_anchors = []

    def get_items(self, kinds, namespace, label_selector=None):
        self.calls.append(("get", kinds, namespace, label_selector))
        own = dr.label_selector(REL)
        if kinds == "secrets":
            return [self.anchor] if (self.anchor and label_selector == own) else []
        if kinds == "pods,services,configmaps,networkpolicies":
            return list(self.leftovers)
        if kinds == "persistentvolumeclaims":
            return list(self.pvcs)
        if kinds == "deployments,statefulsets":
            return list(self.workloads)
        if kinds == "pods":
            return list(self.own_pods) if label_selector == own else list(self.pods)
        raise AssertionError(f"unexpected get_items({kinds!r})")

    def helm_release_exists(self, namespace, release):
        return self.release_exists

    def create_manifests(self, manifests):
        for manifest in manifests:
            index = len(self.created)
            if self.create_error and self.create_error[0] == index:
                raise self.create_error[1]
            self.created.append(copy.deepcopy(manifest))

    def delete_by_selector(self, kinds, namespace, label_selector):
        self.deleted.append((kinds, namespace, label_selector))
        self.calls.append(("delete", kinds, namespace, label_selector))

    def list_items_all_namespaces(self, kind, label_selector):
        self.calls.append(("list_all", kind, label_selector))
        return [a for a in self.sweep_anchors]


class FakeTunnel:
    def __init__(self, ingress):
        self.ingress = copy.deepcopy(ingress)
        self.puts = []
        self.fail_put = False
        self.events = None

    def _response(self, status_code, body):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = str(body)
        resp.json.return_value = body
        return resp

    async def get(self, url, headers=None, timeout=None, params=None):
        assert url.endswith("/configurations"), url
        return self._response(200, {
            "success": True, "result": {"config": {"ingress": copy.deepcopy(self.ingress)}}})

    async def put(self, url, headers=None, json=None, timeout=None):
        assert url.endswith("/configurations"), url
        if self.fail_put:
            return self._response(500, {"success": False})
        self.puts.append(copy.deepcopy(json))
        if self.events is not None:
            self.events.append("route")
        self.ingress = copy.deepcopy(json["config"]["ingress"])
        return self._response(200, {"success": True})


def _base_ingress(*extra):
    return [
        {"hostname": "other.woowtech.io", "service": "http://o.paas-ws-other.svc.cluster.local:80"},
        *extra,
        dict(CATCH_ALL),
    ]


@pytest.fixture
def cf():
    svc = retrieval.cloudflare_service
    fake = FakeTunnel(_base_ingress())
    with patch.object(svc, "enabled", True), \
            patch.object(svc, "dns_enabled", False), \
            patch.object(svc, "domain", "woowtech.io"), \
            patch.object(svc, "account_id", "acc"), \
            patch.object(svc, "tunnel_id", "tun-prod"), \
            patch.object(svc, "api_token", "test-token"), \
            patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake.get)), \
            patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake.put)):
        yield fake


@pytest.fixture
def env_prod():
    with patch.object(retrieval.settings, "paas_env", "prod"), \
            patch.object(retrieval.settings, "cloudflared_namespace", ""), \
            patch.object(retrieval.settings, "data_retrieval_max_ttl_hours", 72), \
            patch.object(retrieval.settings, "data_retrieval_min_remaining_minutes", 60), \
            patch.object(retrieval.settings, "data_retrieval_excluded_nodes", ""):
        yield


@pytest.fixture
def now():
    with patch("src.api.retrieval.time.time", return_value=float(NOW)):
        yield NOW


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def _use(fake):
    return patch.object(retrieval, "k8s_service", fake)


def _open(client, profile="nextcloud", ttl_hours=None, ns=NS, rel=REL):
    body = {"profile": profile}
    if ttl_hours is not None:
        body["ttl_hours"] = ttl_hours
    return client.post(f"/api/releases/{ns}/{rel}/retrieval", json=body)


def _hosts_to(ingress, url):
    return [r["hostname"] for r in ingress if r.get("service") == url]


# ---------------------------------------------------------------------------
# 純函式
# ---------------------------------------------------------------------------


class TestNames:

    def test_resource_name_short_and_long(self):
        assert dr.resource_name("svc-1a2b3c4d") == "paas-retrieval-svc-1a2b3c4d"
        long_release = "a" * 53
        name = dr.resource_name(long_release)
        assert len(name) <= 63 and name.startswith("paas-retrieval-")
        assert name == dr.resource_name(long_release)  # 確定函式（關閉時算得回來）

    @pytest.mark.parametrize("env, prefix", [("prod", "paas-dl-"), ("", "paas-dl-"),
                                             ("stg", "paas-stg-dl-")])
    def test_subdomain_env_token(self, env, prefix):
        sub = dr.new_subdomain(env)
        assert sub.startswith(prefix)
        assert dr.is_retrieval_subdomain(sub)
        assert len(sub) <= 63

    @pytest.mark.parametrize("sub", ["paas-cs-abc12345-1", "paas-dl-xyz", "evil",
                                     "paas-dl-0123456789ab.evil", "", None])
    def test_only_retrieval_hostnames_are_recognised(self, sub):
        assert not dr.is_retrieval_subdomain(sub)


class TestHtpasswd:

    def test_ssha_line_verifies_and_does_not_contain_password(self):
        line = dr.ssha_htpasswd_line("download", "s3cret-pass")
        user, _, value = line.strip().partition(":")
        assert user == "download" and value.startswith("{SSHA}")
        raw = base64.b64decode(value[len("{SSHA}"):])
        digest, salt = raw[:20], raw[20:]
        assert hashlib.sha1(b"s3cret-pass" + salt).digest() == digest
        assert "s3cret-pass" not in line

    def test_salt_is_random(self):
        assert dr.ssha_htpasswd_line("u", "p") != dr.ssha_htpasswd_line("u", "p")

    def test_password_strength(self):
        pw = dr.new_password()
        assert len(pw) >= 24 and pw != dr.new_password()


class TestRules:

    def test_select_nextcloud_data_pvc(self):
        assert dr.select_data_pvc(_nextcloud_pvcs(), dr.PROFILES["nextcloud"])["metadata"][
            "name"] == PVC

    @pytest.mark.parametrize("pvcs", [
        [],
        [_pvc(f"{REL}-nextcloud-html")],
        [_pvc(PVC), _pvc(f"{REL}-other-data")],
        [_pvc(PVC, component="postgresql")],
    ])
    def test_select_refuses_zero_or_many(self, pvcs):
        with pytest.raises(dr.RetrievalError) as exc:
            dr.select_data_pvc(pvcs, dr.PROFILES["nextcloud"])
        assert exc.value.error_code == dr.PVC_NOT_FOUND and exc.value.status_code == 409

    def test_orphan_expiry(self):
        assert dr.orphan_expiry_epoch(_pvc(PVC)) is None
        labelled = _pvc(PVC, labels={dr.ORPHANED_AT_LABEL: "1000",
                                     dr.GRACE_PERIOD_DAYS_LABEL: "90"})
        assert dr.orphan_expiry_epoch(labelled) == 1000 + 90 * 86400
        # 沒有 grace label：照 pvc-cleanup cron 的預設 7 天（不是 operator 的設定值）。
        no_grace = _pvc(PVC, labels={dr.ORPHANED_AT_LABEL: "1000"})
        assert dr.orphan_expiry_epoch(no_grace) == 1000 + 7 * 86400
        assert dr.orphan_expiry_epoch(_pvc(PVC, labels={dr.ORPHANED_AT_LABEL: "x"})) is None

    def test_pods_using_claim(self):
        pods = [
            _pod("svc-app", claim=PVC),
            _pod("old", claim=PVC, phase="Failed"),
            _pod("done", claim=PVC, phase="Succeeded"),
            _pod("pending", claim=PVC, phase="Pending"),
            _pod("other", claim="something-else"),
        ]
        assert dr.pods_using_claim(pods, PVC) == ["pending", "svc-app"]

    def test_running_workloads(self):
        items = [_workload("Deployment", "a", 0), _workload("StatefulSet", "b", 1),
                 {"kind": "Deployment", "metadata": {"name": "c"}, "spec": {}}]
        assert dr.running_workloads(items) == ["Deployment/c", "StatefulSet/b"]

    def test_pod_state(self):
        assert dr.pod_state(None) == (None, False)
        assert dr.pod_state(_pod("p", ready=True)) == ("Running", True)
        assert dr.pod_state(_pod("p", phase="Pending")) == ("Pending", False)


class TestManifests:

    def _build(self, **overrides):
        kwargs = dict(
            namespace=NS, release=REL, profile=dr.PROFILES["nextcloud"], pvc_name=PVC,
            htpasswd=dr.ssha_htpasswd_line("download", "pw"), subdomain="paas-dl-0123456789ab",
            opened_at=NOW, expires_at=NOW + 72 * 3600, image="img@sha256:x", env="prod",
            cloudflared_namespace="paas-cloudflare-system",
        )
        kwargs.update(overrides)
        return {m["kind"]: m for m in dr.build_manifests(**kwargs)}

    def test_creation_order_anchor_first_pod_last(self):
        kinds = [m["kind"] for m in dr.build_manifests(
            namespace=NS, release=REL, profile=dr.PROFILES["nextcloud"], pvc_name=PVC,
            htpasswd="x", subdomain=None, opened_at=NOW, expires_at=NOW + 60, image="i")]
        assert kinds == ["Secret", "ConfigMap", "NetworkPolicy", "Service", "Pod"]

    def test_pvc_is_read_only_at_both_layers(self):
        pod = self._build()["Pod"]["spec"]
        data_vol = next(v for v in pod["volumes"] if v["name"] == "data")
        assert data_vol["persistentVolumeClaim"] == {"claimName": PVC, "readOnly": True}
        for container in pod["containers"] + pod["initContainers"]:
            mounts = [m for m in container["volumeMounts"] if m["name"] == "data"]
            assert mounts and all(m["readOnly"] is True for m in mounts), container["name"]

    def test_restricted_security_and_no_fsgroup(self):
        pod = self._build()["Pod"]["spec"]
        assert "fsGroup" not in pod["securityContext"]
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["runAsUser"] == 33
        assert pod["automountServiceAccountToken"] is False
        assert pod["enableServiceLinks"] is False
        for container in pod["containers"] + pod["initContainers"]:
            sc = container["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False
            assert sc["readOnlyRootFilesystem"] is True
            assert sc["capabilities"] == {"drop": ["ALL"]}
            assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
            assert container["resources"]["limits"]["memory"] == "128Mi"
            assert "hostPath" not in str(pod["volumes"])

    def test_deadline_matches_expiry(self):
        pod = self._build(expires_at=NOW + 3600)["Pod"]["spec"]
        assert pod["activeDeadlineSeconds"] == 3600
        assert pod["restartPolicy"] == "Always"

    def test_not_part_of_the_release(self):
        for manifest in self._build().values():
            labels = manifest["metadata"]["labels"]
            assert "app.kubernetes.io/instance" not in labels
            assert labels[dr.LABEL_RETRIEVAL_FOR] == REL
            assert labels[dr.LABEL_ENV] == "prod"
            assert manifest["metadata"]["name"] == f"paas-retrieval-{REL}"

    def test_secret_holds_only_the_hash(self):
        secret = self._build(htpasswd=dr.ssha_htpasswd_line("download", "plain-pw"))["Secret"]
        assert "plain-pw" not in str(secret)
        assert secret["stringData"]["htpasswd"].startswith("download:{SSHA}")

    def test_network_policy_only_tunnel_in_nothing_out(self):
        np = self._build()["NetworkPolicy"]["spec"]
        assert np["podSelector"] == {"matchLabels": dr.selector_labels(REL)}
        assert np["egress"] == []
        assert np["ingress"] == [{
            "from": [{"namespaceSelector": {"matchLabels": {
                "kubernetes.io/metadata.name": "paas-cloudflare-system"}}}],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }]
        assert self._build(cloudflared_namespace="")["NetworkPolicy"]["spec"]["ingress"] == []

    def test_excluded_nodes(self):
        assert "affinity" not in self._build()["Pod"]["spec"]
        pod = self._build(excluded_nodes=["ubuntuserver-59mqj", ""])["Pod"]["spec"]
        expr = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
            "nodeSelectorTerms"][0]["matchExpressions"][0]
        assert expr == {"key": "kubernetes.io/hostname", "operator": "NotIn",
                        "values": ["ubuntuserver-59mqj"]}

    def test_nginx_is_get_only_with_basic_auth(self):
        conf = self._build()["ConfigMap"]["data"]["default.conf"]
        assert "limit_except GET" in conf and "deny all;" in conf
        assert "auth_basic_user_file /etc/nginx/auth/htpasswd;" in conf
        assert "root /srv/export;" in conf
        assert "disable_symlinks if_not_owner" in conf
        assert 'Content-Security-Policy "sandbox"' in conf
        assert "autoindex on;" in conf

    def test_export_script_nextcloud(self):
        script = self._build()["ConfigMap"]["data"]["export.sh"]
        assert "for d in */files; do" in script
        assert ".*|appdata_*|__*) continue" in script
        assert 'ln -s "/data/$d" "/srv/export/$name"' in script

    def test_whole_volume_profile_has_no_init(self):
        profile = dr.RetrievalProfile(key="x", data_pvc_suffix="-data", run_as_user=1000,
                                      run_as_group=1000)
        built = self._build(profile=profile)
        assert built["Pod"]["spec"]["initContainers"] == []
        assert "root /data;" in built["ConfigMap"]["data"]["default.conf"]
        assert "export.sh" not in built["ConfigMap"]["data"]


# ---------------------------------------------------------------------------
# API：開
# ---------------------------------------------------------------------------


class TestOpen:

    def test_opens_suspended_service(self, client, cf, env_prod, now):
        fake = FakeK8s(workloads=[_workload("Deployment", f"{REL}-nextcloud", 0)],
                       pods=[_pod("unrelated", claim="other")])
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "opened" and body["route"] == "created"
        assert body["username"] == "download" and len(body["password"]) >= 24
        assert body["hostname"].startswith("paas-dl-") and body["hostname"].endswith(".woowtech.io")
        assert body["url"] == f"https://{body['hostname']}/"
        assert body["pvc"] == PVC and body["data_expires_at"] is None
        assert datetime.fromisoformat(body["expires_at"]).timestamp() == NOW + 72 * 3600
        # k8s：錨點先、Pod 最後；明文密碼不在任何 manifest 裡。
        assert [m["kind"] for m in fake.created] == [
            "Secret", "ConfigMap", "NetworkPolicy", "Service", "Pod"]
        assert body["password"] not in str(fake.created)
        pod = fake.created[-1]
        assert pod["spec"]["activeDeadlineSeconds"] == 72 * 3600
        sub = body["hostname"].split(".")[0]
        assert pod["metadata"]["annotations"][dr.ANN_SUBDOMAIN] == sub
        # route：新 hostname → 取回 Service；其他規則與 catch-all 不動。
        assert _hosts_to(cf.ingress, SVC_URL) == [body["hostname"]]
        assert cf.ingress[0]["hostname"] == "other.woowtech.io"
        assert cf.ingress[-1] == CATCH_ALL
        assert fake.deleted == []

    def test_ttl_is_capped(self, client, cf, env_prod, now):
        fake = FakeK8s()
        with _use(fake), patch.object(retrieval.settings, "data_retrieval_max_ttl_hours", 24):
            resp = _open(client, ttl_hours=500)
        assert resp.status_code == 201, resp.text
        assert fake.created[-1]["spec"]["activeDeadlineSeconds"] == 24 * 3600

    def test_deleted_service_capped_by_retention(self, client, cf, env_prod, now):
        orphan = {dr.ORPHANED_AT_LABEL: str(NOW - 89 * 86400), dr.GRACE_PERIOD_DAYS_LABEL: "90"}
        fake = FakeK8s(pvcs=_nextcloud_pvcs(orphan), release_exists=False)
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 201, resp.text
        assert resp.json()["data_expires_at"] is not None
        # 保留期只剩 1 天 ⇒ 入口開 24 小時，不是 72。
        assert fake.created[-1]["spec"]["activeDeadlineSeconds"] == 86400

    def test_deleted_service_retention_almost_over(self, client, cf, env_prod, now):
        orphan = {dr.ORPHANED_AT_LABEL: str(NOW - 90 * 86400 + 600),
                  dr.GRACE_PERIOD_DAYS_LABEL: "90"}
        fake = FakeK8s(pvcs=_nextcloud_pvcs(orphan), release_exists=False)
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "data_retention_expired"
        assert fake.created == [] and cf.puts == []

    def test_deleted_release_without_retention_label_is_refused(self, client, cf, env_prod, now):
        fake = FakeK8s(release_exists=False)
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "retrieval_pvc_not_found"

    @pytest.mark.parametrize("fake_kwargs, code", [
        ({"anchor": "ANCHOR"}, "retrieval_already_open"),
        ({"workloads": [_workload("Deployment", "app", 1)]}, "retrieval_release_running"),
        ({"pods": [_pod(f"{REL}-nextcloud-x", claim=PVC)]}, "retrieval_pvc_in_use"),
        ({"pvcs": []}, "retrieval_pvc_not_found"),
    ])
    def test_refusals_change_nothing(self, client, cf, env_prod, now, fake_kwargs, code):
        if fake_kwargs.get("anchor") == "ANCHOR":
            fake_kwargs = {"anchor": _anchor()}
        fake = FakeK8s(**fake_kwargs)
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error_code"] == code
        assert fake.created == [] and fake.deleted == [] and cf.puts == []

    def test_unknown_profile_is_422(self, client, cf, env_prod, now):
        fake = FakeK8s()
        with _use(fake):
            resp = _open(client, profile="odoo")
        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "retrieval_profile_unknown"
        assert fake.calls == []

    def test_namespace_outside_prefix_is_403(self, client, cf, env_prod, now):
        fake = FakeK8s()
        with _use(fake):
            resp = _open(client, ns="kube-system")
        assert resp.status_code == 403
        assert fake.calls == []

    @pytest.mark.parametrize("rel", ["Svc-Upper", "svc_x", "a" * 60])
    def test_bad_release_name_is_403(self, client, cf, env_prod, now, rel):
        fake = FakeK8s()
        with _use(fake):
            resp = _open(client, rel=rel)
        assert resp.status_code == 403
        assert fake.calls == []

    def test_anchor_race_is_409_and_touches_nothing(self, client, cf, env_prod, now):
        fake = FakeK8s()
        fake.create_error = (0, KubectlException(
            "failed", "kubectl create", 'Error from server (AlreadyExists): secrets '
            '"paas-retrieval-x" already exists'))
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "retrieval_already_open"
        assert fake.deleted == [] and cf.puts == []

    def test_quota_rejection_rolls_back(self, client, cf, env_prod, now):
        fake = FakeK8s()
        fake.create_error = (4, KubectlException(
            "failed", "kubectl create", 'Error from server (Forbidden): pods "x" is forbidden: '
            'exceeded quota: paas-quota, requested: limits.cpu=500m'))
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "retrieval_quota_exceeded"
        assert fake.deleted == [(retrieval.RETRIEVAL_KINDS, NS, dr.label_selector(REL))]
        assert cf.puts == []  # route 還沒建

    def test_cloudflare_failure_rolls_back_k8s(self, client, cf, env_prod, now):
        cf.fail_put = True
        before = copy.deepcopy(cf.ingress)
        fake = FakeK8s()
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 502
        assert resp.json()["detail"]["error_code"] == "cloudflare_error"
        assert fake.deleted == [(retrieval.RETRIEVAL_KINDS, NS, dr.label_selector(REL))]
        assert cf.ingress == before

    def test_stale_retrieval_route_of_this_release_is_removed(self, client, cf, env_prod, now):
        stale = {"hostname": "paas-dl-aaaaaaaaaaaa.woowtech.io", "service": SVC_URL}
        # 指向同一個 Service URL、但不是本功能的 hostname 形狀：不碰。
        foreign = {"hostname": "my-app.woowtech.io", "service": SVC_URL}
        cf.ingress = _base_ingress(stale, foreign)
        fake = FakeK8s()
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 201, resp.text
        hosts = _hosts_to(cf.ingress, SVC_URL)
        assert "paas-dl-aaaaaaaaaaaa.woowtech.io" not in hosts
        assert "my-app.woowtech.io" in hosts and resp.json()["hostname"] in hosts

    def test_leftovers_with_pod_ask_to_retry(self, client, cf, env_prod, now):
        fake = FakeK8s(leftovers=[_pod(f"paas-retrieval-{REL}")])
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "retrieval_closing"
        assert fake.deleted == [(retrieval.RETRIEVAL_KINDS, NS, dr.label_selector(REL))]
        assert fake.created == []

    def test_leftovers_without_pod_are_cleaned_then_opened(self, client, cf, env_prod, now):
        fake = FakeK8s(leftovers=[{"kind": "ConfigMap", "metadata": {"name": "x"}}])
        with _use(fake):
            resp = _open(client)
        assert resp.status_code == 201, resp.text
        assert len(fake.deleted) == 1 and len(fake.created) == 5

    def test_cloudflare_disabled_has_no_hostname(self, client, env_prod, now):
        fake = FakeK8s()
        with _use(fake), patch.object(retrieval.cloudflare_service, "enabled", False):
            resp = _open(client)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["route"] == "disabled" and body["hostname"] is None and body["url"] is None
        assert fake.created[0]["metadata"]["annotations"][dr.ANN_SUBDOMAIN] == ""

    def test_stg_hostname_and_cloudflared_namespace(self, client, cf, now):
        fake = FakeK8s()
        with _use(fake), patch.object(retrieval.settings, "paas_env", "stg"), \
                patch.object(retrieval.settings, "cloudflared_namespace", ""):
            resp = _open(client)
        assert resp.status_code == 201, resp.text
        assert resp.json()["hostname"].startswith("paas-stg-dl-")
        np = next(m for m in fake.created if m["kind"] == "NetworkPolicy")
        assert np["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"] == {
            "kubernetes.io/metadata.name": "paas-stg-cloudflare-system"}
        assert np["metadata"]["labels"][dr.LABEL_ENV] == "stg"


# ---------------------------------------------------------------------------
# API：狀態／關／sweep
# ---------------------------------------------------------------------------


class TestStatus:

    @pytest.mark.parametrize("own_pods, expected", [
        ([_pod("p", ready=True)], "active"),
        ([_pod("p", phase="Pending")], "starting"),
        ([_pod("p", phase="Failed")], "stopped"),
        ([], "stopped"),
    ])
    def test_states(self, client, cf, env_prod, own_pods, expected):
        fake = FakeK8s(anchor=_anchor(), own_pods=own_pods)
        with _use(fake):
            resp = client.get(f"/api/releases/{NS}/{REL}/retrieval")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == expected
        assert body["hostname"] == "paas-dl-0123456789ab.woowtech.io"
        assert body["pvc"] == PVC and body["profile"] == "nextcloud"
        assert "password" not in body

    def test_not_found_is_200(self, client, cf, env_prod):
        with _use(FakeK8s()):
            resp = client.get(f"/api/releases/{NS}/{REL}/retrieval")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"


class TestClose:

    def test_close_deletes_k8s_before_route(self, client, cf, env_prod):
        sub = "paas-dl-0123456789ab"
        cf.ingress = _base_ingress({"hostname": f"{sub}.woowtech.io", "service": SVC_URL})
        fake = FakeK8s(anchor=_anchor(subdomain=sub))
        events = []
        cf.events = events
        original = fake.delete_by_selector

        def _delete(*args):
            events.append("k8s")
            return original(*args)

        fake.delete_by_selector = _delete
        with _use(fake):
            resp = client.delete(f"/api/releases/{NS}/{REL}/retrieval")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "closed", "hostname": f"{sub}.woowtech.io",
                               "route": "deleted"}
        assert events == ["k8s", "route"]
        assert _hosts_to(cf.ingress, SVC_URL) == []
        assert cf.ingress[-1] == CATCH_ALL

    def test_close_is_idempotent(self, client, cf, env_prod):
        fake = FakeK8s()
        with _use(fake):
            resp = client.delete(f"/api/releases/{NS}/{REL}/retrieval")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found" and resp.json()["route"] == "none"
        assert cf.puts == []

    def test_close_with_recorded_subdomain_after_objects_are_gone(self, client, cf, env_prod):
        sub = "paas-dl-0123456789ab"
        cf.ingress = _base_ingress({"hostname": f"{sub}.woowtech.io", "service": SVC_URL})
        with _use(FakeK8s()):
            resp = client.delete(f"/api/releases/{NS}/{REL}/retrieval", params={"subdomain": sub})
        assert resp.status_code == 200
        assert resp.json()["route"] == "deleted"
        assert _hosts_to(cf.ingress, SVC_URL) == []

    def test_close_never_touches_a_hostname_pointing_elsewhere(self, client, cf, env_prod):
        sub = "paas-dl-0123456789ab"
        other = "http://victim.paas-ws-other.svc.cluster.local:80"
        cf.ingress = _base_ingress({"hostname": f"{sub}.woowtech.io", "service": other})
        before = copy.deepcopy(cf.ingress)
        with _use(FakeK8s()):
            resp = client.delete(f"/api/releases/{NS}/{REL}/retrieval", params={"subdomain": sub})
        assert resp.status_code == 200
        assert resp.json()["route"] == "not_owned"
        assert cf.ingress == before and cf.puts == []

    @pytest.mark.parametrize("sub", ["paas-cs-abc12345-1", "my-app"])
    def test_close_rejects_non_retrieval_subdomain(self, client, cf, env_prod, sub):
        fake = FakeK8s()
        with _use(fake):
            resp = client.delete(f"/api/releases/{NS}/{REL}/retrieval", params={"subdomain": sub})
        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "retrieval_invalid_subdomain"
        assert fake.deleted == []

    def test_close_route_failure_still_stops_the_pod(self, client, cf, env_prod):
        sub = "paas-dl-0123456789ab"
        cf.ingress = _base_ingress({"hostname": f"{sub}.woowtech.io", "service": SVC_URL})
        cf.fail_put = True
        fake = FakeK8s(anchor=_anchor(subdomain=sub))
        with _use(fake):
            resp = client.delete(f"/api/releases/{NS}/{REL}/retrieval")
        assert resp.status_code == 502
        assert resp.json()["detail"]["error_code"] == "route_cleanup_failed"
        assert fake.deleted == [(retrieval.RETRIEVAL_KINDS, NS, dr.label_selector(REL))]


class TestSweep:

    def test_closes_only_expired_in_scope(self, client, cf, env_prod, now):
        fake = FakeK8s()
        fake.sweep_anchors = [
            _anchor(expires_at=NOW - 1, subdomain=None),
            _anchor(release="svc-notyet", expires_at=NOW + 600),
            _anchor(namespace="kube-system", release="svc-x", expires_at=NOW - 1),
        ]
        fake.anchor = fake.sweep_anchors[0]
        with _use(fake):
            resp = client.post("/api/retrievals/sweep")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"checked": 3, "closed": [f"{NS}/{REL}"], "failed": []}
        assert fake.deleted == [(retrieval.RETRIEVAL_KINDS, NS, dr.label_selector(REL))]
        # 範圍：本 operator 建的、同 env 的。
        list_call = next(c for c in fake.calls if c[0] == "list_all")
        assert list_call[2] == (
            "app.kubernetes.io/component=data-retrieval,"
            "app.kubernetes.io/managed-by=paas-operator,paas.woowtech.io/env=prod")


class TestNever404:

    def test_every_error_path_is_not_404(self, client, cf, env_prod, now):
        paths = []
        for kwargs in ({"anchor": _anchor()}, {"pvcs": []},
                       {"workloads": [_workload("Deployment", "a", 1)]}):
            with _use(FakeK8s(**kwargs)):
                paths.append(_open(client).status_code)
        with _use(FakeK8s()):
            paths.append(_open(client, profile="nope").status_code)
        broken = MagicMock()
        broken.get_items.side_effect = KubectlException("x", "kubectl get", "boom")
        with _use(broken):
            paths.append(_open(client).status_code)
            paths.append(client.get(f"/api/releases/{NS}/{REL}/retrieval").status_code)
            paths.append(client.delete(f"/api/releases/{NS}/{REL}/retrieval").status_code)
        assert 404 not in paths and all(code >= 400 for code in paths), paths
