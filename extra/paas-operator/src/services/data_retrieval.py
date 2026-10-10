"""woow_paas_platform #1540：停權／刪除後的限時唯讀資料取回入口。

服務停權（scale 0）或已刪除（PVC 帶 ``paas.woowtech.io/orphaned-at`` 保留中）時，平台請
operator 在同一個工作區 namespace 開一個**臨時、唯讀**的檔案瀏覽 pod：

* 資料 PVC 以 readOnly 掛上（volume ``readOnly: true``＋volumeMount ``readOnly: true``）；
* nginx（叢集已在用的 ``nginx-unprivileged``）以 ``autoindex`` 列目錄、只收 GET／HEAD，
  basic auth 的密碼由 operator 產生、只在建立的回應出現一次（Secret 只存 ``{SSHA}`` 雜湊）；
* Pod ``activeDeadlineSeconds``＝到期秒數：平台的 cron 沒跑，kubelet 也會準時停掉它。

本模組只放**純函式**（不呼叫 kubectl、不呼叫 Cloudflare），讓 manifest 與判斷規則可以單獨
測；kubectl 在 ``helm.KubernetesService``，流程在 ``api/retrieval.py``。

profile 白名單在這裡：平台只送 profile 名字，**不能**指定要掛哪顆 PVC、用哪個 uid、匯出哪些路徑。
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 名稱、標籤、標註
# ---------------------------------------------------------------------------

COMPONENT = "data-retrieval"
LABEL_COMPONENT = "app.kubernetes.io/component"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
MANAGED_BY = "paas-operator"
LABEL_RETRIEVAL_FOR = "paas.woowtech.io/retrieval-for"
LABEL_ENV = "paas.woowtech.io/env"

ANN_EXPIRES_AT = "paas.woowtech.io/retrieval-expires-at"   # epoch 秒
ANN_OPENED_AT = "paas.woowtech.io/retrieval-opened-at"     # epoch 秒
ANN_SUBDOMAIN = "paas.woowtech.io/retrieval-subdomain"     # 空＝沒有建 route（Cloudflare 關閉）
ANN_PVC = "paas.woowtech.io/retrieval-pvc"
ANN_PROFILE = "paas.woowtech.io/retrieval-profile"

#: 與 ``api/releases.py`` ``build_orphan_pvc_labels``、``storage_quota`` 同一組 label。
ORPHANED_AT_LABEL = "paas.woowtech.io/orphaned-at"
GRACE_PERIOD_DAYS_LABEL = "paas.woowtech.io/grace-period-days"
#: pvc-cleanup CronJob 在 PVC 沒有 grace-period-days label 時用的天數（``GRACE_DAYS=${GRACE_DAYS:-7}``）。
#: 照 cron 的實際行為算（寧可少算），不是 operator 的 ``PVC_GRACE_PERIOD_DAYS``。
CRON_DEFAULT_GRACE_DAYS = 7

HTTP_PORT = 8080
HEALTH_PORT = 8081
USERNAME = "download"
DATA_MOUNT = "/data"
EXPORT_MOUNT = "/srv/export"

_RESOURCE_PREFIX = "paas-retrieval-"
_SUBDOMAIN_RANDOM_HEX = 12
#: operator 產生的臨時 hostname：prod ``paas-dl-<12 hex>``、其他環境 ``paas-<env>-dl-<12 hex>``
#: （external-resource-env-token-naming：共用 zone 裡一眼分得出 prod／stg）。
SUBDOMAIN_PATTERN = re.compile(r"\Apaas-(?:[a-z0-9]+-)?dl-[0-9a-f]{%d}\Z" % _SUBDOMAIN_RANDOM_HEX)


@dataclass(frozen=True)
class RetrievalProfile:
    """一種 chart 的資料取回規則。新增支援的 app＝在 ``PROFILES`` 加一筆（＋平台範本設 profile）。

    * ``data_pvc_suffix``：release 的 PVC 中，名稱以它結尾者是資料 PVC（必須唯一）。
    * ``data_pvc_component``：同時要求 ``app.kubernetes.io/component`` 等於它（None＝不檢查）。
    * ``run_as_user``／``run_as_group``：以資料擁有者的身分讀（非 root）；**不設 fsGroup**——
      kubelet 不會對客戶資料做任何 chown。
    * ``export_glob``：只匯出符合的子目錄，第一段當資料夾名稱（例：Nextcloud ``*/files`` ⇒
      每個使用者一個資料夾）。None＝匯出整顆 PVC。
    * ``skip_prefixes``：第一段以這些字開頭的不匯出（``.`` 開頭的一律不匯出）。
    """

    key: str
    data_pvc_suffix: str
    run_as_user: int
    run_as_group: int
    data_pvc_component: Optional[str] = None
    export_glob: Optional[str] = None
    skip_prefixes: Tuple[str, ...] = ()


PROFILES: Dict[str, RetrievalProfile] = {
    # woow-paas-charts charts/nextcloud：資料 PVC＝<fullname>-data（component=app），掛在
    # /var/www/html/data；使用者檔案在 <使用者>/files/。擁有者 www-data（33）、目錄 0770。
    # config/config.php（DB 密碼、instance secret）在另一顆 html PVC，不會被掛上。
    # appdata_<instanceid>（縮圖、快取）、__groupfolders 等內部資料夾不匯出。
    "nextcloud": RetrievalProfile(
        key="nextcloud",
        data_pvc_suffix="-data",
        data_pvc_component="app",
        run_as_user=33,
        run_as_group=33,
        export_glob="*/files",
        skip_prefixes=("appdata_", "__"),
    ),
}


class RetrievalError(Exception):
    """流程中可預期的拒絕：API 層原樣轉成 ``HTTPException(status, {error_code, message})``。"""

    def __init__(self, status_code: int, error_code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


# 錯誤碼（平台 ``woow_paas_platform`` 認這些字串，改名要兩邊一起改）。刻意**沒有 404**：
# 平台把 404 當成「operator 是沒有這組端點的舊 image」。
PROFILE_UNKNOWN = "retrieval_profile_unknown"
ALREADY_OPEN = "retrieval_already_open"
PVC_NOT_FOUND = "retrieval_pvc_not_found"
PVC_IN_USE = "retrieval_pvc_in_use"
RELEASE_RUNNING = "retrieval_release_running"
RETENTION_EXPIRED = "data_retention_expired"
QUOTA_EXCEEDED = "retrieval_quota_exceeded"
KUBECTL_ERROR = "kubectl_error"
CLOUDFLARE_ERROR = "cloudflare_error"
ROUTE_CLEANUP_FAILED = "route_cleanup_failed"
INVALID_SUBDOMAIN = "retrieval_invalid_subdomain"


# ---------------------------------------------------------------------------
# 名稱
# ---------------------------------------------------------------------------


def resource_name(release: str) -> str:
    """取回入口所有 k8s 物件共用的名字（一個 release 一個 ⇒ ``kubectl create`` 撞名＝已開著）。

    Service 名必須是 ≤63 的 DNS label；release 太長時改用 hash（仍是 release 的確定函式）。
    """
    name = f"{_RESOURCE_PREFIX}{release}"
    if len(name) <= 63:
        return name
    digest = hashlib.sha256(release.encode()).hexdigest()[:16]
    return f"{_RESOURCE_PREFIX}{digest}"


def service_url(namespace: str, release: str) -> str:
    """tunnel 規則要指的位址（與 ``CloudflareService.generate_service_url`` 同形）。"""
    return f"http://{resource_name(release)}.{namespace}.svc.cluster.local:{HTTP_PORT}"


def selector_labels(release: str) -> Dict[str, str]:
    return {LABEL_COMPONENT: COMPONENT, LABEL_RETRIEVAL_FOR: release}


def label_selector(release: str) -> str:
    return ",".join(f"{k}={v}" for k, v in selector_labels(release).items())


def anchors_selector(env: str) -> str:
    """sweep 用：本 operator 建的所有取回入口（有 env 時只限同 env，prod／stg 共用叢集）。"""
    parts = [f"{LABEL_COMPONENT}={COMPONENT}", f"{LABEL_MANAGED_BY}={MANAGED_BY}"]
    if env:
        parts.append(f"{LABEL_ENV}={env}")
    return ",".join(parts)


def new_subdomain(env: str) -> str:
    token = secrets.token_hex(_SUBDOMAIN_RANDOM_HEX // 2)
    if env and env != "prod":
        return f"paas-{env}-dl-{token}"
    return f"paas-dl-{token}"


def is_retrieval_subdomain(subdomain: Optional[str]) -> bool:
    return bool(subdomain) and bool(SUBDOMAIN_PATTERN.match(subdomain))


# ---------------------------------------------------------------------------
# 帳密
# ---------------------------------------------------------------------------


def new_password() -> str:
    """24 字元（144 bit）URL-safe 隨機密碼。"""
    return secrets.token_urlsafe(18)


def ssha_htpasswd_line(username: str, password: str, salt: Optional[bytes] = None) -> str:
    """nginx ``auth_basic_user_file`` 一行：``user:{SSHA}base64(sha1(pw+salt)+salt)``。

    nginx 內建支援 RFC 2307 的 ``{SSHA}``（不必 crypt(3)／apr1）。密碼是 144 bit 隨機值，
    加鹽 SHA-1 已足以防離線還原；雜湊只存在 Secret，明文只出現在建立的回應一次。
    """
    salt = salt if salt is not None else secrets.token_bytes(8)
    digest = hashlib.sha1(password.encode() + salt).digest()
    return f"{username}:{{SSHA}}{base64.b64encode(digest + salt).decode()}\n"


# ---------------------------------------------------------------------------
# 現況判斷
# ---------------------------------------------------------------------------


def _labels(item: Dict[str, Any]) -> Dict[str, str]:
    return (item.get("metadata") or {}).get("labels") or {}


def _annotations(item: Dict[str, Any]) -> Dict[str, str]:
    return (item.get("metadata") or {}).get("annotations") or {}


def _name(item: Dict[str, Any]) -> str:
    return (item.get("metadata") or {}).get("name") or ""


def select_data_pvc(pvc_items: Iterable[Dict[str, Any]], profile: RetrievalProfile) -> Dict[str, Any]:
    """release 的 PVC 中挑出 profile 的資料 PVC；沒有或不只一顆都 raise（不猜）。"""
    matches = [
        pvc for pvc in pvc_items
        if _name(pvc).endswith(profile.data_pvc_suffix)
        and (profile.data_pvc_component is None
             or _labels(pvc).get(LABEL_COMPONENT) == profile.data_pvc_component)
        and not (pvc.get("metadata") or {}).get("deletionTimestamp")
    ]
    if len(matches) != 1:
        names = sorted(_name(p) for p in matches)
        raise RetrievalError(
            409, PVC_NOT_FOUND,
            f"Expected exactly one data volume (*{profile.data_pvc_suffix}) for this service, "
            f"found {len(matches)}{': ' + ', '.join(names) if names else ''}.",
        )
    return matches[0]


def orphan_expiry_epoch(pvc: Dict[str, Any]) -> Optional[int]:
    """孤兒 PVC 會被 pvc-cleanup cron 刪掉的時間（epoch 秒）；不是孤兒（沒有 orphaned-at）回 None。

    orphaned-at 不是數字時 cron 會跳過（永遠不刪），這裡回 None 由呼叫端當「沒有保留資訊」。
    """
    labels = _labels(pvc)
    raw = labels.get(ORPHANED_AT_LABEL)
    if raw is None:
        return None
    try:
        orphaned_at = int(raw)
    except (TypeError, ValueError):
        return None
    try:
        grace_days = int(labels.get(GRACE_PERIOD_DAYS_LABEL, ""))
    except (TypeError, ValueError):
        grace_days = CRON_DEFAULT_GRACE_DAYS
    return orphaned_at + grace_days * 86400


_TERMINAL_PHASES = ("Succeeded", "Failed")


def pods_using_claim(pod_items: Iterable[Dict[str, Any]], claim: str) -> List[str]:
    """還沒結束（含 Terminating）而且掛著 ``claim`` 的 pod 名稱。

    RWO 的 Longhorn volume 同時只能掛在一個節點：服務的 pod 還在（停權沒縮完、復權中）就不能開，
    反之亦然。結束（Succeeded／Failed）的 pod 不再持有 volume。
    """
    users = []
    for pod in pod_items:
        if ((pod.get("status") or {}).get("phase")) in _TERMINAL_PHASES:
            continue
        for volume in (pod.get("spec") or {}).get("volumes") or []:
            if ((volume.get("persistentVolumeClaim") or {}).get("claimName")) == claim:
                users.append(_name(pod))
                break
    return sorted(users)


def running_workloads(workload_items: Iterable[Dict[str, Any]]) -> List[str]:
    """``spec.replicas`` 大於 0 的 Deployment／StatefulSet（``kind/name``）。沒寫 replicas＝1。"""
    running = []
    for item in workload_items:
        replicas = (item.get("spec") or {}).get("replicas", 1)
        if replicas is None or replicas > 0:
            running.append(f"{item.get('kind', 'workload')}/{_name(item)}")
    return sorted(running)


def session_from_anchor(secret: Dict[str, Any]) -> Dict[str, Any]:
    """Secret（入口的錨點）上的標註 → session 資訊。數字壞掉當 0（＝已到期，sweep 會清）。"""
    ann = _annotations(secret)

    def _epoch(key: str) -> int:
        try:
            return int(ann.get(key, ""))
        except (TypeError, ValueError):
            return 0

    meta = secret.get("metadata") or {}
    return {
        "namespace": meta.get("namespace") or "",
        "release": _labels(secret).get(LABEL_RETRIEVAL_FOR) or "",
        "expires_at": _epoch(ANN_EXPIRES_AT),
        "opened_at": _epoch(ANN_OPENED_AT),
        "subdomain": ann.get(ANN_SUBDOMAIN) or None,
        "pvc": ann.get(ANN_PVC) or "",
        "profile": ann.get(ANN_PROFILE) or "",
    }


def pod_state(pod: Optional[Dict[str, Any]]) -> Tuple[Optional[str], bool]:
    """``(phase, ready)``；沒有 pod 回 ``(None, False)``。"""
    if not pod:
        return None, False
    status = pod.get("status") or {}
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in status.get("conditions") or []
    )
    return status.get("phase"), ready


# ---------------------------------------------------------------------------
# manifests
# ---------------------------------------------------------------------------

NGINX_CONF = """\
worker_processes 1;
error_log /dev/stderr notice;
pid /tmp/nginx.pid;

events {
    worker_connections 256;
}

http {
    client_body_temp_path /tmp/client_temp;
    proxy_temp_path /tmp/proxy_temp;
    fastcgi_temp_path /tmp/fastcgi_temp;
    uwsgi_temp_path /tmp/uwsgi_temp;
    scgi_temp_path /tmp/scgi_temp;
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    # 稽核用：誰（Cloudflare 回報的來源 IP）、哪個帳號、拿了什麼。不記 query string。
    log_format retrieval '$http_cf_connecting_ip $remote_user [$time_local] '
                         '"$request_method $uri" $status $body_bytes_sent';
    access_log /dev/stdout retrieval;
    sendfile on;
    tcp_nopush on;
    keepalive_timeout 65;
    server_tokens off;
    include /etc/nginx/conf.d/*.conf;
}
"""

SERVER_CONF = """\
# woow_paas_platform #1540：限時唯讀資料取回入口（由 paas-operator 建立，到期自動刪除）。
server {
    listen %(http_port)d default_server;
    server_name _;
    charset utf-8;
    root %(export)s;
    # 只跟 initContainer 建的那層 symlink；資料裡的 symlink 指到別人的檔案一律不跟。
    disable_symlinks if_not_owner from=%(export)s;
    client_max_body_size 1k;

    auth_basic "Data retrieval";
    auth_basic_user_file /etc/nginx/auth/htpasswd;

    add_header Cache-Control "no-store" always;
    add_header X-Robots-Tag "noindex, nofollow" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;
    # 使用者上傳的 HTML 在這個網域打開也不能跑 script、不能碰任何 cookie。
    add_header Content-Security-Policy "sandbox" always;

    location / {
        # 唯讀：GET（含 HEAD）以外一律 403。
        limit_except GET {
            deny all;
        }
        autoindex on;
        autoindex_exact_size off;
        autoindex_localtime on;
    }
}

# kubelet 探針（不需要帳密、只回固定字串）。
server {
    listen %(health_port)d;
    server_name _;
    access_log off;
    location = /healthz {
        default_type text/plain;
        return 200 "ok\\n";
    }
    location / {
        return 404;
    }
}
"""


def server_conf(profile: RetrievalProfile) -> str:
    root = EXPORT_MOUNT if profile.export_glob else DATA_MOUNT
    return SERVER_CONF % {"http_port": HTTP_PORT, "health_port": HEALTH_PORT, "export": root}


def export_script(profile: RetrievalProfile) -> str:
    """initContainer：在 emptyDir 建一層 symlink（``/srv/export/<名稱> → /data/<名稱>/<子路徑>``）。

    只讀 ``/data``（唯讀掛載）、只寫 emptyDir。名稱是 glob 的第一段；``.`` 開頭與
    ``skip_prefixes`` 不匯出。
    """
    glob = profile.export_glob or "*"
    first, _, rest = glob.partition("/")
    if first != "*" or not rest or "'" in rest:
        raise ValueError(f"unsupported export_glob {glob!r}: expected '*/<path>'")
    skips = "|".join(f"{p}*" for p in profile.skip_prefixes) or "''"
    return f"""\
set -eu
cd {DATA_MOUNT}
n=0
for d in */{rest}; do
  [ -d "$d" ] || continue
  name=${{d%/{rest}}}
  case "$name" in
    .*|{skips}) continue ;;
  esac
  ln -s "{DATA_MOUNT}/$d" "{EXPORT_MOUNT}/$name"
  n=$((n + 1))
done
echo "data-retrieval: exported $n folder(s)"
"""


def _container_security_context(profile: RetrievalProfile) -> Dict[str, Any]:
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "runAsUser": profile.run_as_user,
        "runAsGroup": profile.run_as_group,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }


_RESOURCES = {
    "requests": {"cpu": "10m", "memory": "16Mi", "ephemeral-storage": "16Mi"},
    "limits": {"cpu": "500m", "memory": "128Mi", "ephemeral-storage": "128Mi"},
}


def build_manifests(
    *,
    namespace: str,
    release: str,
    profile: RetrievalProfile,
    pvc_name: str,
    htpasswd: str,
    subdomain: Optional[str],
    opened_at: int,
    expires_at: int,
    image: str,
    env: str = "",
    cloudflared_namespace: str = "",
    excluded_nodes: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """取回入口的全部 k8s 物件，**建立順序**：Secret（錨點，撞名＝已開）→ ConfigMap → NetworkPolicy
    → Service → Pod（最後建：它一出現就開始掛 PVC）。"""
    name = resource_name(release)
    labels = {
        **selector_labels(release),
        LABEL_MANAGED_BY: MANAGED_BY,
    }
    if env:
        labels[LABEL_ENV] = env
    annotations = {
        ANN_EXPIRES_AT: str(int(expires_at)),
        ANN_OPENED_AT: str(int(opened_at)),
        ANN_SUBDOMAIN: subdomain or "",
        ANN_PVC: pvc_name,
        ANN_PROFILE: profile.key,
    }

    def _meta() -> Dict[str, Any]:
        return {
            "name": name,
            "namespace": namespace,
            "labels": dict(labels),
            "annotations": dict(annotations),
        }

    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _meta(),
        "type": "Opaque",
        "stringData": {"htpasswd": htpasswd},
    }
    config_data = {
        "nginx.conf": NGINX_CONF,
        "default.conf": server_conf(profile),
    }
    if profile.export_glob:
        config_data["export.sh"] = export_script(profile)
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta(),
        "data": config_data,
    }
    # 只選這個 pod。NetworkPolicy 是聯集：工作區 baseline（same-ns、egress）仍然有效；在沒有
    # baseline 的 namespace，這條就是唯一規則（只有 tunnel 進得來、出不去）。
    ingress: List[Dict[str, Any]] = []
    if cloudflared_namespace:
        ingress.append({
            "from": [{"namespaceSelector": {"matchLabels": {
                "kubernetes.io/metadata.name": cloudflared_namespace,
            }}}],
            "ports": [{"protocol": "TCP", "port": HTTP_PORT}],
        })
    netpol = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _meta(),
        "spec": {
            "podSelector": {"matchLabels": selector_labels(release)},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": ingress,
            "egress": [],
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta(),
        "spec": {
            "type": "ClusterIP",
            "selector": selector_labels(release),
            "ports": [{"name": "http", "port": HTTP_PORT, "targetPort": HTTP_PORT,
                       "protocol": "TCP"}],
        },
    }

    sc = _container_security_context(profile)
    data_mount = {"name": "data", "mountPath": DATA_MOUNT, "readOnly": True}
    volumes: List[Dict[str, Any]] = [
        {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name, "readOnly": True}},
        {"name": "config", "configMap": {"name": name, "defaultMode": 0o444}},
        {"name": "auth", "secret": {"secretName": name, "defaultMode": 0o444,
                                    "items": [{"key": "htpasswd", "path": "htpasswd"}]}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "32Mi"}},
    ]
    nginx_mounts = [
        {"name": "config", "mountPath": "/etc/nginx/nginx.conf", "subPath": "nginx.conf",
         "readOnly": True},
        {"name": "config", "mountPath": "/etc/nginx/conf.d/default.conf",
         "subPath": "default.conf", "readOnly": True},
        {"name": "auth", "mountPath": "/etc/nginx/auth", "readOnly": True},
        dict(data_mount),
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    init_containers: List[Dict[str, Any]] = []
    if profile.export_glob:
        volumes.append({"name": "export", "emptyDir": {"sizeLimit": "1Mi"}})
        nginx_mounts.append({"name": "export", "mountPath": EXPORT_MOUNT, "readOnly": True})
        init_containers.append({
            "name": "export",
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "command": ["/bin/sh", "/etc/retrieval/export.sh"],
            "securityContext": dict(sc),
            "resources": _RESOURCES,
            "volumeMounts": [
                dict(data_mount),
                {"name": "export", "mountPath": EXPORT_MOUNT},
                {"name": "config", "mountPath": "/etc/retrieval", "readOnly": True},
            ],
        })
    probe = {"httpGet": {"path": "/healthz", "port": "health"}, "timeoutSeconds": 2}
    pod_spec: Dict[str, Any] = {
        # 到期由 kubelet 強制（平台 cron／sweep 沒跑也一樣）；裸 Pod 不會被重建。
        "activeDeadlineSeconds": max(1, int(expires_at) - int(opened_at)),
        "restartPolicy": "Always",
        "terminationGracePeriodSeconds": 5,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        # 刻意不設 fsGroup：唯讀掛載也不讓 kubelet 對客戶資料做任何 chown。
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": profile.run_as_user,
            "runAsGroup": profile.run_as_group,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "initContainers": init_containers,
        "containers": [{
            "name": "nginx",
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            # 不走 image 的 entrypoint（它會嘗試改寫唯讀的 conf.d）。
            "command": ["nginx", "-g", "daemon off;"],
            "ports": [
                {"name": "http", "containerPort": HTTP_PORT, "protocol": "TCP"},
                {"name": "health", "containerPort": HEALTH_PORT, "protocol": "TCP"},
            ],
            "readinessProbe": dict(probe, periodSeconds=5),
            "livenessProbe": dict(probe, periodSeconds=30, failureThreshold=3),
            "resources": _RESOURCES,
            "securityContext": dict(sc),
            "volumeMounts": nginx_mounts,
        }],
        "volumes": volumes,
    }
    nodes = [n for n in excluded_nodes if n]
    if nodes:
        pod_spec["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{
                "key": "kubernetes.io/hostname", "operator": "NotIn", "values": nodes,
            }]}],
        }}}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": _meta(), "spec": pod_spec}
    return [secret, configmap, netpol, service, pod]
