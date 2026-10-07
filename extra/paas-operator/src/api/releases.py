"""API endpoints for Helm release management."""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from fastapi import APIRouter, HTTPException, Query, status

from src.config import settings
from src.models.schemas import (
    MqttAddressResponse,
    PublicAddressResponse,
    ReleaseCreateRequest,
    ReleaseInfo,
    ReleaseResetConfigRequest,
    ReleaseRevisionsResponse,
    ReleaseRollbackRequest,
    ReleaseStatus,
    ReleaseStatusResponse,
    ReleaseUpgradeRequest,
    RouteInfo,
    RouteStatus,
    SidecarPatchRequest,
    SidecarPatchResponse,
)
from src.services.cloudflare import CloudflareException, CloudflareService
from src.services.helm import (
    HELM_CLI_OVERHEAD_SECONDS,
    HelmException,
    HelmService,
    KubectlException,
    KubernetesService,
)
from src.services.helm import _live_objects, _sanitize_release_values
from src.services.upgrade_preflight import (
    build_expansion_error_detail,
    classify_expansion_rejection,
    describe_conflicts,
    describe_helm_failure,
    find_immutable_conflicts,
    find_pvc_expansions,
    longhorn_volume_expansion_blocker,
)
from src.services.storage_quota import (
    build_quota_error_detail,
    exhausted_resources,
    is_storage_quota_rejection,
    summarize,
)
from src.services.storage_capacity import (
    CapacityDataError,
    LonghornCluster,
    build_capacity_error_detail,
    collect_volume_requests,
    format_gib,
    has_storage_datavolume,
    is_schedulable,
    parse_storage_classes,
    simulate,
    summarize_orphans,
)


# ── #461：reset-config 的時間預算（階梯的最內層，＝單一權威）───────────────
#
# 為什麼需要一組專屬常數：reset 的三層預算（前端 90s ／ 平台 client 75s ／
# 這裡）原本彼此不知道對方，operator 最壞路徑 ≈560s 卻由 120s 的平台 client
# 等待，冷 image cache 時必逾時（#461／F-449-3，#459 stg 實測兩台第一次都輸）。
# 修法不是把數字改大，而是宣告一條可機械檢查的階梯：
#
#     前端 90_000ms > 平台 RESET_CONFIG_TIMEOUT(75s) > RESET_HARD_CAP_SECONDS
#
# 由 scripts/audit/repo-lint.py 的 check_reset_budget_ladder 跨三層驗算。
# 改任何一個數字都必須讓那道 gate 仍然綠——散文註解攔不住這件事，它已經被
# 攔失敗過一次了。
RESET_HELM_TIMEOUT_SECONDS = 15
# ⚠️ 為什麼是 2n 而不是 n：helm 的 `--timeout` 是 **per-operation**，而
# `--atomic` 失敗時 helm 會自己再跑一次「帶 wait 的 rollback」＝第二個
# operation，再吃一次完整的 timeout。subprocess 上限只給 n+slack 會在
# rollback **進行中** SIGKILL helm ⇒ release 卡 pending-*、Secret 狀態不明
# ⇒ 打破「2xx 之前的失敗保證叢集仍是舊密碼」這個 C11′ 前提。
#
# 這不是新風險：改動前 upgrade() 根本不傳 `--timeout`（helm 用預設 5m）而
# subprocess 上限是 settings.helm_timeout=300s ⇒ 2×300 > 300，**今天的 atomic
# 失敗路徑本來就在 rollback 開始前就被 kill**。本常數順手修掉該既有缺陷。
RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS = (
    2 * RESET_HELM_TIMEOUT_SECONDS + HELM_CLI_OVERHEAD_SECONDS
)
# config.json 的 read-modify-write：內嵌 python 腳本，實際執行 <1s。
RESET_EXEC_TIMEOUT_SECONDS = 12
# 備份清理：best-effort（失敗只 warning），且 pod 正在被 Recreate 取代。
RESET_CLEANUP_EXEC_TIMEOUT_SECONDS = 5
# Deployment 探索 ＋ rollout restart 下達（各 ~1s 的 kubectl 呼叫）。
RESET_KUBECTL_SLACK_SECONDS = 3
# operator 對 reset-config 的**硬上限**：外層預算必須大於它。
RESET_HARD_CAP_SECONDS = (
    RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS
    + RESET_EXEC_TIMEOUT_SECONDS
    + RESET_CLEANUP_EXEC_TIMEOUT_SECONDS
    + RESET_KUBECTL_SLACK_SECONDS
)


class _NoCdiInstalled(Exception):
    """Sentinel: this cluster has no CDI, so there is nothing to orphan-label.

    Not an error condition — keeps the teardown flow linear without an extra
    nesting level (MINOR-4).
    """

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/releases", tags=["releases"])

helm_service = HelmService()
k8s_service = KubernetesService()
cloudflare_service = CloudflareService()


def _chart_ref_is_remote(chart: Optional[str]) -> bool:
    """chart ref 會不會被 helm 重新「解析」成某個版本（＝有跳版風險）。

    #552：`--version` 只對**需要解析**的 ref 有意義——OCI ref（``oci://...``）與
    classic repo ref（``bitnami/postgresql``）沒有 `--version` 就取 registry 當下
    最新的那一版。反過來，**本機路徑或 .tgz 的版本寫在 chart 自己的 Chart.yaml
    裡**，helm 根本不會去查任何 registry，`--version` 對它是 no-op。

    所以判準是「helm 需不需要去外面查」，用「這是不是一個存在的本機路徑」表達
    ——這正是 helm 自己的第一步。刻意不寫成 ``oci://`` 前綴白名單：那會漏掉
    classic ``repo/chart`` 形式（一樣會跳版），而漏掉的方向是 fail-open。

    ⚠ 這個區分不是為了測試方便：integration 測試用本機 chart 目錄，而生產一律是
    ``oci://``。把本機 chart 也擋掉等於宣稱一件不成立的事（本機 chart 不會跳版）。
    """
    if not chart:
        # chart 缺席時 `helm_service.upgrade` 自己會 raise ValueError（helm 不保存
        # 原始 chart URL）。那是另一個錯誤，不歸本閘門管——這裡回 False 讓它照原路
        # 拋出自己的訊息，不要偽裝成「沒釘版本」。
        return False
    return not Path(chart).exists()


def _ensure_image_pull_secrets(
    namespace: str, values: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """補齊 imagePullSecrets 的**兩層**，回傳 values 淺複本（install/upgrade/reset 共用）。

    第一層 —— **values**（chart 自己 render 出來的 podSpec）：私有 image chart
    （如 mcp-server 拉 jcr-prod 的 hermes-mcp-admin）需要 imagePullSecrets 讓
    kubelet 通過認證；secret 本體由 ensure_registry_secret 先佈建進 namespace。
    與 agent_runtimes.py（F-PILOT-4）同一 family——install 路徑在 a0e7c25a 補上，
    但 upgrade 路徑漏了：#411 之後 upgrade 走全量 reset values，私有 image 的
    cloud service 任一次 redeploy / reconfigure / reset-password 都會掉 pull
    secret → ImagePullBackOff 401（#448）。抽成共用 helper 讓兩邊永不再漂移。
    用 setdefault：template 的 helm_default_values 可自行覆寫；public-image
    chart（bitnami n8n/odoo）不消費 top-level imagePullSecrets、多帶無害。

    第二層 —— **ns 的 default ServiceAccount**（#423）：VM 型 cloud service 的
    golden image 由 CDI importer pod 匯入，那顆 pod 由 CDI controller 生成、不屬於
    任何 chart 的 podSpec，第一層完全碰不到它；它跑在 ns 的 default SA 上，只能靠
    ServiceAccount admission 把 SA 的 imagePullSecrets 併進 pod。沒有這一層，HAOS
    開通會永久卡在 ImportScheduled。細節與 stg 實證見
    ``KubernetesService.ensure_default_sa_image_pull_secret``。

    為什麼兩層綁在同一個 helper：#448 的教訓正是「install 補了、upgrade 漏了」。
    綁在同一個呼叫上，任何一條路徑都不可能只補一半。``namespace`` 刻意設成必填的
    位置參數——未來新增呼叫點時漏傳會直接 TypeError，而不是靜默少收斂一層。
    """
    # SA 收斂必須發生在 helm install / upgrade **之前**：DataVolume 一旦被 apply，
    # CDI 立刻生出 importer pod，屆時再 patch SA 已經來不及（admission 只在 pod
    # 建立當下併入）。
    k8s_service.ensure_default_sa_image_pull_secret(namespace)
    return _with_default_pull_secrets(values)


def _with_default_pull_secrets(values: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """values 淺複本＋預設 imagePullSecrets（純函式；不碰叢集）。

    #1415：容量預檢的 dry-run 要用與真正 install **同一份** values 渲染，但必須在任何
    叢集寫入（SA patch）之前，所以把「改 values」從 ``_ensure_image_pull_secrets`` 拆出來。
    """
    merged = dict(values or {})
    merged.setdefault(
        "imagePullSecrets", [{"name": settings.registry_secret_name}]
    )
    return merged


def _storage_quota_summary(namespace: str) -> Optional[Dict[str, Any]]:
    """#1348：讀 namespace 的儲存配額摘要；讀不到回 None（fail-open，見呼叫端）。"""
    try:
        snapshot = k8s_service.get_storage_quota_snapshot(namespace)
    except (KubectlException, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Storage quota pre-check skipped for %s: %s", namespace,
            getattr(exc, "stderr", None) or exc,
        )
        return None
    return summarize(snapshot["quotas"], snapshot["pvcs"])


def _storage_quota_conflict(
    namespace: str, summary: Optional[Dict[str, Any]], resources: List[str]
) -> HTTPException:
    """#1348：儲存配額不足的 409，``detail`` 帶固定 ``error_code`` 供平台轉成租戶訊息。"""
    detail = build_quota_error_detail(namespace, summary, resources)
    logger.warning(
        "Storage quota exhausted in %s: %s (orphaned PVCs: %s)",
        namespace, detail["usage"], detail.get("orphaned_pvc_count"),
    )
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _precheck_storage_quota(namespace: str) -> None:
    """#1348：helm install 前確認 PVC 數量／requests.storage 配額沒有用滿。

    只擋「已經滿了」（used ≥ hard）：此時任何會建 PVC 的 chart 都必定在 admission
    被拒，留下半套 release 與泛用的「Helm installation failed」。剩餘額度不夠本次
    chart 用（例 odoo 一次 3 顆）的情況事前看不到 chart 要幾顆，交給 install 失敗後
    的 :func:`is_storage_quota_rejection` 轉成同一個錯誤碼。

    讀不到配額（kubectl 失敗、權限、ns 尚未建立）一律放行——這是給租戶看得懂的
    說明，不是新的閘門；放行後的行為與本檢查加入前完全相同。
    """
    summary = _storage_quota_summary(namespace)
    if summary is None:
        return
    resources = exhausted_resources(summary)
    if resources:
        raise _storage_quota_conflict(namespace, summary, resources)


def _cluster_capacity_conflict_detail(request: ReleaseCreateRequest) -> Optional[Dict[str, Any]]:
    """#1415：這次 install 要新建的 Longhorn volume 是否「確定」排不進叢集。

    排不進回 409 的 ``detail``；放得下、或任何一步拿不到資料（不知道）回 None＝放行。
    步驟（全部唯讀，渲染不套用任何東西）：

    1. 讀 Longhorn 節點／設定與 StorageClass（叢集沒有 Longhorn、權限不足、逾時 ⇒ 放行）；
    2. 用與真正 install **同一組** chart／version／values／post-renderer 跑
       ``helm install --dry-run``，拿到這次會建的 PVC、StatefulSet volumeClaimTemplates、
       CDI DataVolume（manifest 可能含渲染後的 Secret，**不得進 log**）；
    3. 扣掉 namespace 裡已經存在的同名 PVC（重試安裝），只算 provisioner 是 Longhorn 的；
    4. 照 Longhorn 排程規則模擬放置（``storage_capacity.simulate``）。

    為什麼用 dry-run 而不是在 post-renderer 裡判斷：post-renderer 是 helm 的子程序，拿不到
    operator 的設定與 kubectl，失敗只能透過 stderr 字串傳回來；dry-run 多一次 chart 渲染
    （實測數秒），換來資料流單純、任何失敗都能明確放行，形制同 #1407 的 upgrade 預檢。
    """
    namespace, name = request.namespace, request.name
    try:
        snapshot = k8s_service.get_longhorn_capacity_snapshot()
    except (KubectlException, ValueError) as exc:
        logger.warning(
            "Cluster storage capacity pre-check skipped for %s/%s (Longhorn unreadable): %s",
            namespace, name, getattr(exc, "stderr", None) or exc,
        )
        return None
    try:
        cluster = LonghornCluster.from_snapshot(snapshot["nodes"], snapshot["settings"])
        classes, default_class = parse_storage_classes(snapshot["storage_classes"])
    except CapacityDataError as exc:
        logger.warning(
            "Cluster storage capacity pre-check skipped for %s/%s (unexpected Longhorn data): %s",
            namespace, name, exc,
        )
        return None
    if not cluster.nodes or not any(sc.is_longhorn for sc in classes.values()):
        logger.info("Cluster storage capacity pre-check: no Longhorn in this cluster, skipped")
        return None

    try:
        manifest = helm_service.install(
            namespace=namespace,
            name=name,
            chart=request.chart,
            values=_with_default_pull_secrets(request.values),
            sensitive_values=request.sensitive_values,
            version=request.version,
            dry_run=True,
            subprocess_timeout_seconds=settings.storage_capacity_render_timeout_seconds,
        )
    except (HelmException, ValueError) as exc:
        # 真正的 install 會在同一步失敗並走原本的錯誤處理（含 #1348 的 quota 辨識）。
        logger.warning(
            "Cluster storage capacity pre-check skipped for %s/%s (render failed): %s",
            namespace, name, getattr(exc, "message", None) or exc,
        )
        return None
    if not isinstance(manifest, str):
        return None
    try:
        docs = [d for d in yaml.safe_load_all(manifest) if isinstance(d, dict)]
    except yaml.YAMLError as exc:
        logger.warning(
            "Cluster storage capacity pre-check skipped for %s/%s (manifest unparsable: %s)",
            namespace, name, type(exc).__name__,
        )
        return None
    try:
        existing = k8s_service.list_pvcs(namespace)
    except KubectlException as exc:
        logger.warning(
            "Cluster storage capacity pre-check skipped for %s/%s (cannot list PVCs): %s",
            namespace, name, exc.stderr or exc,
        )
        return None
    overheads = k8s_service.get_cdi_filesystem_overhead() if has_storage_datavolume(docs) else None
    volumes = collect_volume_requests(
        docs, classes, default_class, cluster.settings, existing, overheads,
    )
    if not volumes:
        return None
    placements = simulate(cluster, volumes)
    degraded = [
        p for p in placements
        if is_schedulable(p, cluster.settings) and p.placed < p.volume.replicas
    ]
    if degraded:
        # Longhorn 會建、可以掛載，但少了備援——不擋（不是「確定放不下」），留給監控告警。
        logger.warning(
            "Cluster storage capacity: %s/%s will be created DEGRADED: %s", namespace, name,
            ", ".join(f"{p.volume.name} {p.placed}/{p.volume.replicas} replicas"
                      for p in degraded),
        )
    if all(is_schedulable(p, cluster.settings) for p in placements):
        logger.info(
            "Cluster storage capacity pre-check passed for %s/%s: %s", namespace, name,
            ", ".join(f"{v.name} {format_gib(v.size_bytes)}x{v.replicas}" for v in volumes),
        )
        return None
    try:
        orphans = summarize_orphans(
            k8s_service.list_orphaned_pvcs_all_namespaces(), classes, cluster.settings,
        )
    except (KubectlException, ValueError):
        orphans = None
    return build_capacity_error_detail(namespace, cluster, placements, orphans)


def _precheck_cluster_storage_capacity(request: ReleaseCreateRequest) -> None:
    """#1415：helm install 前確認叢集（Longhorn）排得下這次要建的 volume；排不下 raise 409。

    只有「確定放不下」才擋；預檢本身的任何意外都放行（log），行為與本檢查加入前相同。
    與 #1348 的工作區配額檢查分工：那邊看 namespace ResourceQuota，這裡看叢集層排程額度。
    """
    if not settings.storage_capacity_precheck_enabled:
        return
    try:
        detail = _cluster_capacity_conflict_detail(request)
    except Exception:  # noqa: BLE001 — 預檢的意外不能變成擋開通
        logger.exception(
            "Cluster storage capacity pre-check failed unexpectedly for %s/%s; allowing install",
            request.namespace, request.name,
        )
        return
    if detail is not None:
        logger.warning(
            "Cluster storage capacity exhausted for %s/%s: %s",
            request.namespace, request.name, detail["message"],
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


#: #1157：同名 release 的孤兒 PVC／DataVolume 還在 ⇒ 拒絕 install。平台照一般錯誤顯示
#: （release name 隨機，正常流程撞不到；會撞到的是手動操作與 integration test）。
ORPHANED_RELEASE_RESOURCES_ERROR_CODE = "orphaned_release_resources_exist"
#: #1157：查不到孤兒資源（kubectl 失敗）⇒ 不放行，回 503。
ORPHANED_RELEASE_CHECK_UNAVAILABLE_ERROR_CODE = "orphaned_release_check_unavailable"


def _precheck_orphaned_release_resources(namespace: str, name: str) -> None:
    """#1157 裁決 A：install 前拒絕「同 namespace＋同 release name」的孤兒資源接回。

    uninstall 留下的 PVC／DataVolume 帶 ``orphaned-at`` 等三個 label（``delete_release``）。
    同名重裝時 helm 會靜默接回它們（舊站原地復活、新填的 admin 帳密不生效），而 label
    沒人清 ⇒ 回收 CronJob 在保留期到期後刪掉活站資料。這裡在任何叢集寫入之前擋下，
    回 409 要呼叫端明確表態：確認資料不要就先刪掉那些資源，或改用新的 release name。

    P1 刻意**不**提供「接回」旗標（裁決不做 B：會讓上一個租戶的資料／備份歷史被新站
    接回）；日後要加，接回成功後必須同時拔掉三個 label。

    與 #1348／#1415 兩個預檢相反，這裡查詢失敗走 **fail-closed**（503）：放行就等於回到
    #1157 的資料遺失路徑；而 kubectl 連 ``get pvc`` 都失敗時，接下來的 helm install 本來
    也很難成功。
    """
    try:
        found = k8s_service.list_orphaned_release_resources(namespace, name)
    except KubectlException as exc:
        logger.error(
            "Orphaned-resource pre-check failed for %s/%s; refusing install: %s",
            namespace, name, exc.stderr or exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": ORPHANED_RELEASE_CHECK_UNAVAILABLE_ERROR_CODE,
                "message": (
                    f"Cannot verify whether release {name} in {namespace} has orphaned "
                    f"PVCs/DataVolumes from a previous uninstall; install refused. Retry later."
                ),
            },
        )
    pvcs = sorted(found.get("pvcs") or [])
    datavolumes = sorted(found.get("datavolumes") or [])
    if not pvcs and not datavolumes:
        return
    logger.warning(
        "Refusing install of %s/%s: orphaned resources from a previous uninstall: "
        "pvcs=%s datavolumes=%s", namespace, name, pvcs, datavolumes,
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error_code": ORPHANED_RELEASE_RESOURCES_ERROR_CODE,
            "message": (
                f"Release {name} in {namespace} still has orphaned resources from a previous "
                f"uninstall (PVCs: {', '.join(pvcs) or '-'}; DataVolumes: "
                f"{', '.join(datavolumes) or '-'}). Installing under the same name would "
                f"silently re-attach the old data while the orphan labels stay, so the "
                f"pvc-cleanup CronJob would later delete the live data. Either delete these "
                f"resources first (if the data is not needed) or use a new release name."
            ),
            "pvcs": pvcs,
            "datavolumes": datavolumes,
        },
    )


@router.post(
    "",
    response_model=ReleaseInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Install a Helm release",
)
async def create_release(request: ReleaseCreateRequest):
    """Install a new Helm chart release.

    Args:
        request: Release creation parameters

    Returns:
        Information about the created release

    Raises:
        HTTPException: If installation fails（同名 release 的孤兒 PVC／DataVolume 還在回
            409、查不到回 503，#1157；儲存配額不足回 409，#1348；叢集儲存排程額度放不下
            回 409，#1415）
    """
    try:
        # #1157：同名 release 的孤兒資源還在就拒絕（helm 會靜默接回、回收 CronJob 之後
        # 會刪掉活站資料）——放最前面：它是確定性的拒絕，不必先跑容量預檢的 dry-run。
        _precheck_orphaned_release_resources(request.namespace, request.name)
        # #1348：在任何叢集寫入（SA patch／helm install）之前先檢查儲存配額。
        _precheck_storage_quota(request.namespace)
        # #1415：再檢查叢集（Longhorn）排程額度——同樣在任何叢集寫入之前。
        _precheck_cluster_storage_capacity(request)
        # 注入預設 imagePullSecrets（values + ns default SA 兩層，理由見
        # _ensure_image_pull_secrets docstring）。
        values = _ensure_image_pull_secrets(request.namespace, request.values)
        release = helm_service.install(
            namespace=request.namespace,
            name=request.name,
            chart=request.chart,
            values=values,
            sensitive_values=request.sensitive_values,
            version=request.version,
            create_namespace=request.create_namespace,
        )
        if release.values is not None:
            release.values = _sanitize_release_values(release.values)
        logger.info(f"Installed release {request.name} in {request.namespace}")

        # Handle Cloudflare Tunnel route creation if expose is enabled
        route_info = None
        if request.expose and request.expose.enabled:
            route_info = await _create_cloudflare_route(
                namespace=request.namespace,
                release_name=request.name,
                expose_config=request.expose,
            )
            if route_info:
                release.route = route_info

        return release

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Helm install failed: {e.message}\nStderr: {e.stderr}")
        # #1348：PVC admission 被儲存配額拒絕（剩餘額度不夠本次 chart 用）——
        # 轉成與事前檢查同一個錯誤碼，不再只回泛用訊息。
        rejected = is_storage_quota_rejection(e.stderr)
        if rejected:
            raise _storage_quota_conflict(
                request.namespace, _storage_quota_summary(request.namespace), rejected,
            )
        # Sanitize error message - don't expose internal details
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Helm installation failed. Check operator logs for details.",
        )


@router.get(
    "/{namespace}/{name}",
    response_model=ReleaseInfo,
    summary="Get release information",
)
def get_release(namespace: str, name: str):
    """Get detailed information about a Helm release.

    Args:
        namespace: Release namespace
        name: Release name

    Returns:
        Release information

    Raises:
        HTTPException: If release not found or access denied
    """
    try:
        release = helm_service.get(namespace, name)
        return release

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        if "not found" in e.stderr.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Release {name} not found in namespace {namespace}",
            )
        logger.error(f"Failed to get release: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get release. Check operator logs for details.",
        )


@router.patch(
    "/{namespace}/{name}",
    response_model=ReleaseInfo,
    summary="Upgrade a Helm release",
)
def upgrade_release(
    namespace: str,
    name: str,
    request: ReleaseUpgradeRequest,
):
    """Upgrade an existing Helm release.

    Args:
        namespace: Release namespace
        name: Release name
        request: Upgrade parameters

    Returns:
        Updated release information

    Raises:
        HTTPException: If upgrade fails
    """
    try:
        # #448：upgrade 也必須注入預設 imagePullSecrets。#411 之後 upgrade 走
        # 全量 reset values，沒有這行時私有 image 的 CS 任一次 redeploy /
        # reconfigure 都會掉 pull secret → ImagePullBackOff 401。
        # #423：同一個呼叫連帶收斂 ns default SA（VM 型 CS 重跑 import 時要用）。
        upgrade_kwargs = dict(
            namespace=namespace,
            name=name,
            chart=request.chart,
            values=_ensure_image_pull_secrets(namespace, request.values),
            sensitive_values=request.sensitive_values,
            version=request.version,
            reset_values=request.reset_values,
            reuse_values=request.reuse_values,
        )
        # #1407：helm upgrade 不是交易——中途被 API server 拒絕時，前面的資源已經
        # 換成新版、release 卻標 failed。先用同一組參數 dry-run 出 manifest，跟 live
        # 物件比對不可變欄位；有衝突就在這裡擋下（409，什麼都還沒套用）。
        _upgrade_preflight(upgrade_kwargs)
        release = helm_service.upgrade(**upgrade_kwargs)
        if release.values is not None:
            release.values = _sanitize_release_values(release.values)
        logger.info(f"Upgraded release {name} in {namespace}")
        return release

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Helm upgrade failed: {e.message}\nStderr: {e.stderr}")
        # #1407：管理員只看得到 detail——原本是「Check operator logs」，看不出原因。
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=describe_helm_failure(e.stderr),
        )


def _upgrade_preflight(upgrade_kwargs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """#1407：dry-run 比對不可變欄位；有衝突 raise 409（中文、指出物件與欄位）。

    dry-run 本身失敗（chart 拉不到、template 錯）照樣 raise HelmException——真正
    upgrade 也一定會在同一步失敗，提早回報而且什麼都沒套用。live 物件讀不到時
    不擋（log warning），行為退回 #1407 之前。

    #1513：接著檢查要**變大**的 PVC（``_refuse_unplaceable_expansions``）。回傳這次會擴容的
    PVC 清單（``upgrade-preflight`` 端點回給呼叫端）。
    """
    namespace = upgrade_kwargs["namespace"]
    manifest = helm_service.upgrade(**upgrade_kwargs, dry_run=True)
    live_items = _live_objects(namespace, "pvc,statefulsets,deployments")
    if live_items is None:
        logger.warning(
            "Upgrade preflight for %s/%s skipped: live objects unavailable",
            namespace, upgrade_kwargs["name"],
        )
        return []
    docs = [d for d in yaml.safe_load_all(manifest or "") if isinstance(d, dict)]
    conflicts = find_immutable_conflicts(docs, live_items)
    if conflicts:
        logger.warning(
            "Upgrade preflight refused %s/%s: %s", namespace, upgrade_kwargs["name"], conflicts,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=describe_conflicts(conflicts),
        )
    expansions = find_pvc_expansions(docs, live_items)
    _refuse_unplaceable_expansions(namespace, upgrade_kwargs["name"], expansions)
    return expansions


def _refuse_unplaceable_expansions(
    namespace: str, name: str, expansions: List[Dict[str, Any]],
) -> None:
    """#1513：每顆要變大的 PVC 先看 Longhorn volume 的 replica 排滿沒（沒排滿 Longhorn 不給擴，
    ``longhorn_volume_expansion_blocker``），再送 ``kubectl patch --dry-run=server``；被拒就 raise 409。

    helm upgrade 不是交易：#1158 實測 data PVC 擴容被 Longhorn 拒絕時，Deployment／Secret
    與 backup PVC 已經換新、release 標 failed。這裡在 helm 套用任何東西之前問一次。
    只有 API server／Longhorn **明確拒絕**才擋；逾時、連不上等「不知道」一律放行
    （同 #1415 原則）。與 #1415 開通預檢共用 ``storage_capacity_precheck_enabled`` 開關。

    ⚠ 每顆 PVC 各自 dry-run：同一顆磁碟上同時擴兩顆時，Longhorn 真正套用也是逐顆判斷，
    兩顆合計放不下而各自放得下的邊界情況這裡看不到（偏樂觀＝少擋，不會誤擋）。
    """
    if not expansions or not settings.storage_capacity_precheck_enabled:
        return
    denials = []
    for exp in expansions:
        public = {k: exp[k] for k in ("name", "live", "rendered")}
        if exp.get("volume"):
            try:
                blocker = longhorn_volume_expansion_blocker(
                    k8s_service.get_longhorn_volume(exp["volume"]))
            except (KubectlException, ValueError) as exc:
                blocker = None
                logger.warning(
                    "PVC expansion pre-check for %s/%s: Longhorn volume %s unreadable: %s",
                    namespace, exp["name"], exp["volume"],
                    (getattr(exc, "stderr", None) or str(exc)).strip()[:300],
                )
            if blocker:
                denials.append(dict(public, reason="capacity", message=blocker))
                continue
        try:
            k8s_service.dry_run_pvc_resize(namespace, exp["name"], exp["rendered"])
        except KubectlException as exc:
            reason = classify_expansion_rejection(exc.stderr)
            if reason is None:
                logger.warning(
                    "PVC expansion pre-check for %s/%s skipped (%s): %s",
                    namespace, exp["name"], exc.message, (exc.stderr or "").strip()[:300],
                )
                continue
            denials.append(dict(public, reason=reason, message=(exc.stderr or "").strip()))
    if denials:
        detail = build_expansion_error_detail(namespace, denials)
        logger.warning("Upgrade preflight refused %s/%s: %s", namespace, name, detail["message"])
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


@router.post(
    "/{namespace}/{name}/upgrade-preflight",
    summary="Check an upgrade without applying it (#1407 immutable fields, #1513 PVC expansion)",
)
def upgrade_release_preflight(
    namespace: str,
    name: str,
    request: ReleaseUpgradeRequest,
) -> Dict[str, Any]:
    """#1513：用與 ``PATCH /{namespace}/{name}`` **同一組**參數跑升級預檢，不套用任何東西。

    平台換方案時在**扣款前**呼叫：放不下就整筆擋下，不會出現「帳單已換、磁碟沒擴」。
    與真正的 upgrade 唯一的差別：這裡不收斂 namespace default SA 的 pull secret（那是寫入），
    values 只做純函式的預設 pull secret 補位——兩者送給 helm 的 values 相同。

    Returns:
        ``{"ok": True, "expansions": [...]}``（這次會擴容的 PVC）。

    Raises:
        HTTPException: 409（不可變欄位衝突／擴容被拒）、500（dry-run 渲染失敗）。
    """
    upgrade_kwargs = dict(
        namespace=namespace,
        name=name,
        chart=request.chart,
        values=_with_default_pull_secrets(request.values),
        sensitive_values=request.sensitive_values,
        version=request.version,
        reset_values=request.reset_values,
        reuse_values=request.reuse_values,
    )
    try:
        expansions = _upgrade_preflight(upgrade_kwargs)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HelmException as e:
        logger.error(f"Helm upgrade preflight failed: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=describe_helm_failure(e.stderr),
        )
    return {"ok": True, "expansions": expansions}


def build_orphan_pvc_labels(release_name: str, grace_period_days: int) -> Dict[str, str]:
    """Build the orphaned-PVC labels stamped at teardown for the pvc-cleanup cron.

    orphaned-at is epoch SECONDS (colon-free): an RFC3339 timestamp contains ':',
    an illegal k8s label value, which made `kubectl label` reject the whole batch
    atomically so the cron's `-l orphaned-at` selector never matched and data PVCs
    leaked forever (operator #208). Single source of truth so the integration
    test exercises the exact value production the teardown path uses.
    """
    now_epoch = str(int(datetime.now(timezone.utc).timestamp()))
    return {
        "paas.woowtech.io/orphaned-at": now_epoch,
        "paas.woowtech.io/release-name": release_name,
        "paas.woowtech.io/grace-period-days": str(grace_period_days),
    }


def resolve_orphan_grace_period_days(
    requested: Optional[str], namespace: str, name: str
) -> int:
    """#1431：這次刪除的孤兒 PVC／DataVolume 保留天數。

    平台對「從未正常運作過」的服務會帶 ``grace_period_days=1``（沒有租戶資料要保護，
    不必佔用 Longhorn 排程額度 90 天）。只接受 ``1 ≤ n ≤ settings.pvc_grace_period_days``
    ——只能**縮短**、不能延長預設保留期；沒帶、不是整數、超出範圍一律用預設，
    **不回 422**：刪除是不可重來的操作，參數寫壞不該讓服務刪不掉（超出範圍時記 warning）。

    參數宣告成字串再自己解析，正是為了不讓 FastAPI 對非整數回 422。
    """
    default = settings.pvc_grace_period_days
    if requested is None or requested == "":
        return default
    try:
        days = int(requested)
    except (TypeError, ValueError):
        days = None
    if days is None or not 1 <= days <= default:
        logger.warning(
            "Ignoring grace_period_days=%r for %s/%s (allowed 1..%s); using the default %s",
            requested, namespace, name, default, default,
        )
        return default
    return days


@router.delete(
    "/{namespace}/{name}",
    response_model=Dict[str, str],
    summary="Uninstall a Helm release",
)
async def delete_release(
    namespace: str,
    name: str,
    subdomain: Optional[str] = None,
    grace_period_days: Optional[str] = Query(
        default=None,
        description=(
            "#1431: shorten how long the orphaned PVCs / DataVolumes of this release are "
            "kept (1..PVC_GRACE_PERIOD_DAYS). Anything else is ignored and the default is used."
        ),
    ),
):
    """Uninstall a Helm release.

    Args:
        namespace: Release namespace
        name: Release name
        subdomain: Optional subdomain to delete from Cloudflare (if not provided, auto-generated)
        grace_period_days: #1431 optional shorter retention for the orphaned PVCs /
            DataVolumes (see ``resolve_orphan_grace_period_days``)

    Returns:
        Deletion confirmation message

    Raises:
        HTTPException: If uninstallation fails
    """
    # 先解析（純函式、不會失敗），PVC 與 DataVolume 兩段共用同一個值。
    orphan_grace_days = resolve_orphan_grace_period_days(grace_period_days, namespace, name)
    try:
        result = helm_service.uninstall(namespace, name)
        logger.info(f"Uninstalled release {name} from {namespace}")

        # Label orphaned PVCs for deferred cleanup — scoped to THIS release's own
        # PVCs (#281). A per-workspace ns is shared by multiple services; an
        # unscoped list would stamp orphaned-at on other live services' data PVCs
        # → the pvc-cleanup cron then deletes them (data-loss). helm stamps
        # app.kubernetes.io/instance={release} on templated resources, and the
        # Bitnami StatefulSet volumeClaimTemplate data PVCs carry it too (verified
        # live on stg: data-svc-…-postgresql-0 has instance=svc-…).
        try:
            remaining_pvcs = k8s_service.list_pvcs(
                namespace, label_selector=f"app.kubernetes.io/instance={name}"
            )
            if remaining_pvcs:
                labels = build_orphan_pvc_labels(name, orphan_grace_days)
                k8s_service.label_pvcs(namespace, remaining_pvcs, labels)
                logger.info(
                    f"Labeled {len(remaining_pvcs)} orphaned PVCs in {namespace} "
                    f"(grace {orphan_grace_days}d): {remaining_pvcs}"
                )
        except Exception as e:
            logger.warning(f"Failed to label orphaned PVCs in {namespace}: {e}")

        # #423 W4.4: VM-type releases keep their disk in a CDI DataVolume that
        # OWNS the PVC. The chart marks the DataVolume helm.sh/resource-policy:
        # keep, so `helm uninstall` leaves it behind (verified in the W1 PoC) —
        # meaning it needs the same orphan stamp as the PVC, and the cleanup
        # cron must delete THE DATAVOLUME. Deleting the PVC alone makes CDI
        # recreate it and re-run the whole import (also measured in the PoC).
        # Separate try block: a DataVolume failure must not mask the PVC
        # labeling above, and vice versa.
        try:
            # MINOR-4: gate on a cached CRD probe so a container-only cluster
            # does not pay an extra synchronous kubectl on EVERY delete — and,
            # more importantly, so the failure log below stays rare enough to
            # mean something when it does fire.
            if not k8s_service.has_datavolume_crd():
                raise _NoCdiInstalled()
            remaining_dvs = k8s_service.list_datavolumes(
                namespace, label_selector=f"app.kubernetes.io/instance={name}"
            )
            if remaining_dvs:
                labels = build_orphan_pvc_labels(name, orphan_grace_days)
                labelled = k8s_service.label_datavolumes(
                    namespace, remaining_dvs, labels
                )
                # Report what actually succeeded — a DataVolume that missed
                # its stamp is kept forever by the chart's resource-policy and
                # is invisible to the cleanup cron (silent storage leak).
                if len(labelled) != len(remaining_dvs):
                    missed = sorted(set(remaining_dvs) - set(labelled))
                    logger.error(
                        f"Orphan-labelled only {len(labelled)}/{len(remaining_dvs)} "
                        f"DataVolumes in {namespace}; UNRECLAIMABLE: {missed}"
                    )
                else:
                    logger.info(
                        f"Labeled {len(labelled)} orphaned DataVolumes in "
                        f"{namespace}: {labelled}"
                    )
        except _NoCdiInstalled:
            logger.debug(
                f"No CDI CRD in this cluster; skipping DataVolume "
                f"orphan-labeling for {namespace}/{name}"
            )
        except KubectlException as e:
            # A cluster with no CDI is an expected, uninteresting case — but
            # anything else (Forbidden, timeout) on a VM-capable cluster is
            # the ONLY signal that a tenant's disk was left unreclaimable, so
            # it must not be buried at the same level as routine noise.
            if KubernetesService.is_crd_absent_error(e):
                logger.debug(
                    f"No CDI CRD in this cluster; skipping DataVolume "
                    f"orphan-labeling for {namespace}/{name}"
                )
            else:
                logger.error(
                    f"Failed to list DataVolumes for orphan-labeling in "
                    f"{namespace}/{name} — any VM disk here will NOT be "
                    f"reclaimed: {e.stderr}"
                )
        except Exception as e:
            logger.error(
                f"Failed to label orphaned DataVolumes in {namespace}: {e}"
            )

        # Delete Cloudflare Tunnel route if exists
        try:
            # Use provided subdomain or fall back to auto-generated
            route_subdomain = subdomain or cloudflare_service.generate_subdomain(namespace, name)
            await cloudflare_service.delete_route(route_subdomain)
            logger.info(f"Deleted Cloudflare route for {route_subdomain}")
        except CloudflareException as e:
            # Log but don't fail - release is already uninstalled
            logger.warning(f"Failed to delete Cloudflare route: {e.message}")

        # Delete MCP sidecar Cloudflare route and K8s Service if they exist
        try:
            # #301: deploy no longer builds a public mcp CF route (see
            # _create_sidecar_service). This delete leg is KEPT to converge any
            # residual {subdomain}-mcp route left by historical deploys (pre-#301);
            # it must use generate_subdomain (the same source those deploys used),
            # not the caller subdomain, or it targets a different hostname and
            # orphans the route (operator #207). delete_route is idempotent, so it
            # is a harmless no-op once no such route remains.
            mcp_subdomain = cloudflare_service.generate_subdomain(namespace, name) + "-mcp"
            await cloudflare_service.delete_route(mcp_subdomain)
            logger.info(f"Deleted MCP sidecar Cloudflare route for {mcp_subdomain}")
        except CloudflareException as e:
            logger.warning(f"Failed to delete MCP sidecar Cloudflare route: {e.message}")

        try:
            mcp_service_name = f"{name}-mcp"
            k8s_service.delete_service(namespace, mcp_service_name)
            logger.info(f"Deleted MCP sidecar K8s Service {mcp_service_name}")
        except Exception as e:
            logger.warning(f"Failed to delete MCP sidecar K8s Service: {e}")

        return result

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        # If release not found, return 404 so client can clean up DB record
        if "not found" in e.stderr.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Release {name} not found in namespace {namespace}",
            )
        logger.error(f"Helm uninstall failed: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Helm uninstall failed. Check operator logs for details.",
        )


@router.post(
    "/{namespace}/{name}/rollback",
    response_model=Dict[str, Any],
    summary="Rollback a Helm release",
)
def rollback_release(
    namespace: str,
    name: str,
    request: ReleaseRollbackRequest,
):
    """Rollback a Helm release to a previous revision.

    Args:
        namespace: Release namespace
        name: Release name
        request: Rollback parameters

    Returns:
        Rollback confirmation message

    Raises:
        HTTPException: If rollback fails
    """
    if request.preserve_value_paths:
        return _rollback_preserving_values(namespace, name, request)

    try:
        # #1526：純 helm rollback 重放目標 revision 的 stored manifest——chart 版本也一起
        # 回到當時。與保留憑證的回復同一道版本下限（見 `_refuse_chart_downgrade`）。
        _validate_namespace_or_raise(namespace)
        history = helm_service.history(namespace, name)
        target_revision = _resolve_rollback_target(history, request.revision, name)
        target_version = _refuse_chart_downgrade(namespace, name, history, target_revision)

        result = helm_service.rollback(namespace, name, target_revision)
        logger.info(f"Rolled back release {name} in {namespace}")
        # #1526：回報回復後的 chart 版本，平台據此更新帳本（同版或升版都可能——例如從
        # 早先被降過版的狀態回到較新的 revision）；不回報的話平台帳本與叢集不一致，
        # 下一次修改配置會以舊帳本釘版。
        return {**result, "mode": "rollback", "target_revision": target_revision,
                "chart_version": target_version}

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Helm rollback failed: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Helm rollback failed. Check operator logs for details.",
        )


#: #1526：回復被 chart 版本下限擋下時的錯誤碼（409 的 ``detail.error_code``）。
#: 平台據此顯示租戶看得懂的訊息；字串與平台 ``controllers/paas.py`` 的同名常數一致。
ROLLBACK_CHART_DOWNGRADE_ERROR_CODE = "rollback_chart_downgrade"

_SEMVER_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def chart_version_key(version: Optional[str]) -> Optional[Tuple]:
    """#1526：chart 版本（SemVer 2）的比較鍵；讀不懂回 ``None``。

    prerelease 低於同號正式版（``1.0.0-rc.1 < 1.0.0``），數字識別字低於英數識別字、
    逐段比較——SemVer §11 的規則。build metadata（``+…``）不參與比較。平台
    ``services/chart_version.py`` 有一份同規則的實作（兩邊是不同的部署單位，不能共用）。
    """
    if not isinstance(version, str):
        return None
    m = _SEMVER_RE.match(version.strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    if pre is None:
        pre_key: Tuple = ()
        release_rank = 1
    else:
        pre_key = tuple(
            (0, int(part), "") if part.isdigit() else (1, 0, part)
            for part in pre.split(".")
        )
        release_rank = 0
    return (int(major), int(minor), int(patch), release_rank, pre_key)


def chart_version_not_lower(target: Optional[str], current: Optional[str]) -> bool:
    """#1526：``target`` 是否**不低於** ``current``。任一方未知或讀不懂 ⇒ False（fail-closed）。

    字串完全相同直接放行（同一版、即使不是 SemVer 也不必比較）。
    """
    if not target or not current:
        return False
    if target == current:
        return True
    target_key, current_key = chart_version_key(target), chart_version_key(current)
    if target_key is None or current_key is None:
        return False
    return target_key >= current_key


def _baseline_revision(history) -> int:
    """#1526：版本下限的基準 revision＝目前**實際部署中**的那一版。

    最新的 revision 不一定是部署中的（升級失敗的 revision 狀態是 failed，前一版仍是
    deployed）；以失敗的新版當下限，會連「退回原本那一版」都被擋掉。沒有 deployed
    （例如首裝就失敗）時退回最新的 revision。
    """
    deployed = [r.revision for r in history if r.status == ReleaseStatus.DEPLOYED]
    if deployed:
        return max(deployed)
    return max(r.revision for r in history)


def _resolve_rollback_target(history, requested: Optional[int], name: str) -> int:
    """回復的目標 revision（``None``／0＝上一版），並驗證它在歷史裡、不是目前那一版。"""
    if not history:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Release {name} has no revision history",
        )
    current_revision = max(rev.revision for rev in history)
    target_revision = requested or (current_revision - 1)
    known = {rev.revision for rev in history}
    if target_revision < 1 or target_revision not in known:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Revision {target_revision} not found in release history",
        )
    if target_revision == current_revision:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target revision is the current revision",
        )
    return target_revision


def _refuse_chart_downgrade(
    namespace: str,
    name: str,
    history,
    target_revision: int,
    target_version: Optional[str] = None,
) -> Optional[str]:
    """#1526：回復不得讓 chart 版本低於目前部署中的版本；擋下時 409。回傳目標版本。

    chart 升版帶著平台的安全修正（例：pi-agent 0.1.5 收斂 egress、加 ephemeral-storage
    上限；hermes-agent 0.3.0 修 console RBAC）。回復原本照目標 revision 的 chart 版本
    重新渲染／重放 manifest，工作區 admin 按兩下就能把這些修正撤掉，之後平台的改帳密、
    修改配置也都釘在那個舊版。chart 升版是管理員 Redeploy 的職責，退回舊 chart 同理。

    同版或更高版的 revision 照常可回復（設定錯了要退回去的用例不受影響）。版本讀不到或
    不是 SemVer ⇒ 拒絕（fail-closed）——寧可擋下一次回復，也不放行一次降版。
    """
    if target_version is None:
        target_version = helm_service.get_revision_chart_version(
            namespace, name, target_revision,
        )
    baseline = _baseline_revision(history)
    current_version = (
        target_version if baseline == target_revision
        else helm_service.get_revision_chart_version(namespace, name, baseline)
    )
    if chart_version_not_lower(target_version, current_version):
        return target_version
    logger.warning(
        "Refusing rollback of %s/%s to revision %s: chart %s is older than the "
        "deployed %s (revision %s)",
        namespace, name, target_revision, target_version, current_version, baseline,
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error_code": ROLLBACK_CHART_DOWNGRADE_ERROR_CODE,
            "message": (
                f"Revision {target_revision} uses chart version {target_version or 'unknown'}, "
                f"older than the deployed {current_version or 'unknown'}; rolling back to "
                "an older chart is not allowed"
            ),
            "target_revision": target_revision,
            "target_chart_version": target_version,
            "current_chart_version": current_version,
        },
    )


_PATH_MISSING = object()


def _get_path(values: Dict[str, Any], path: str) -> Any:
    node: Any = values
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _PATH_MISSING
        node = node[part]
    return node


def _set_path(values: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = values
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _delete_path(values: Dict[str, Any], path: str) -> None:
    parts = path.split(".")
    node: Any = values
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def overlay_preserved_paths(
    target_values: Dict[str, Any],
    current_values: Dict[str, Any],
    paths: List[str],
) -> Dict[str, Any]:
    """#1433：把 ``paths`` 在**目前** values 的狀態（含「不存在」）蓋到目標 values 上。

    純函式、回傳新的 dict（不改呼叫端的物件）。「目前不存在」也要蓋過去：目標
    revision 若帶著一份目前已經沒有的憑證，留著它就是讓被撤銷的值復活——與本票
    要堵的是同一件事。
    """
    import copy

    merged = copy.deepcopy(target_values or {})
    for path in paths:
        current = _get_path(current_values or {}, path)
        if current is _PATH_MISSING:
            _delete_path(merged, path)
        else:
            _set_path(merged, path, copy.deepcopy(current))
    return merged


def _rollback_preserving_values(
    namespace: str,
    name: str,
    request: ReleaseRollbackRequest,
) -> Dict[str, Any]:
    """#1433：「回復版本」但保留目前的憑證——以 helm upgrade 重現目標 revision。

    #1526：目標 revision 的 chart 版本不得低於目前部署中的版本（``_refuse_chart_downgrade``）；
    平台另把 ``helm_value_specs`` 裡 ``type: password`` 的可調設定（API 金鑰等）也列進
    ``preserve_value_paths``——與管理員帳密同一個語意：憑證不跟著回復。

    為什麼不是 ``helm rollback`` 之後再補一次 reset：rollback 一落地 Secret 就是舊
    密碼，open-design 的 authproxy 每個請求重讀 htpasswd ⇒ 補救之前那段時間舊密碼
    可以登入、目前的密碼被拒（R8 實測約 30–60 秒），而補救本身若失敗就停在最糟的
    狀態。改成一次 upgrade：目標 revision 的 values（一般設定）＋目前 revision 的
    ``preserve_value_paths``（憑證）＋目標 revision 的 chart 版本，憑證從頭到尾沒有
    離開目前的值。

    與純 rollback 的差異（刻意的取捨）：
    - helm rollback 重放 stored manifest；這裡是用目標 chart 版本＋目標 values
      **重新渲染**。chart 若有 ``randAlphaNum`` 這類非決定性模板，結果可能與當時不同
      ——但同一個 chart 的平常 reconfigure 也是這樣渲染，不是新的風險面。
    - 會重新拉目標版本的 chart；registry 裡那一版若已被刪，回 500 而**不**退回純
      rollback（退回就是讓舊憑證復活，fail-open 的方向錯了）。
    - 與 reconfigure 一樣先跑 #1407 不可變欄位 preflight。
    """
    _validate_namespace_or_raise(namespace)
    try:
        history = helm_service.history(namespace, name)
        target_revision = _resolve_rollback_target(history, request.revision, name)
        current_revision = max(rev.revision for rev in history)

        target_version = helm_service.get_revision_chart_version(
            namespace, name, target_revision,
        )
        # #552 同一道閘：遠端 chart 沒有版本就會解析成 registry latest——「回復版本」
        # 結果反而升版。讀不到目標版本就拒絕，不猜。
        if _chart_ref_is_remote(request.chart) and not target_version:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Cannot determine the chart version of revision {target_revision}; "
                    "refusing to roll back without a pinned version"
                ),
            )
        # #1526：chart 版本不得低於目前部署中的版本。
        _refuse_chart_downgrade(
            namespace, name, history, target_revision, target_version=target_version,
        )

        # ⛔ 兩份都是**明文** values（含憑證）：只進下面 upgrade 的 --values 暫存檔，
        # 不 log、不回傳。
        target_values = helm_service.get_values(namespace, name, revision=target_revision)
        current_values = helm_service.get_values(namespace, name)
        merged_values = overlay_preserved_paths(
            target_values, current_values, request.preserve_value_paths,
        )

        upgrade_kwargs = dict(
            namespace=namespace,
            name=name,
            chart=request.chart,
            values=_ensure_image_pull_secrets(namespace, merged_values),
            version=target_version,
            reset_values=True,
            reuse_values=False,
        )
        _upgrade_preflight(upgrade_kwargs)
        release = helm_service.upgrade(
            **upgrade_kwargs,
            description=(
                f"Rollback to {target_revision} (credentials preserved)"
            ),
        )
        logger.info(
            "Rolled back release %s in %s to revision %s via upgrade "
            "(preserved %d value path(s))",
            name, namespace, target_revision, len(request.preserve_value_paths),
        )
        return {
            "message": f"Rolled back to revision {target_revision} (credentials preserved)",
            "mode": "upgrade_preserving",
            "target_revision": target_revision,
            # `helm upgrade -o json` 把 revision 放在頂層 `version`，`_parse_release_info`
            # 讀 `info.revision` 恆為 0（既有缺陷，與 helm_revision=0 同源）；這裡以歷史
            # 推算的新 revision 兜底，不把 0 回給平台。
            "revision": release.revision or (current_revision + 1),
            "chart_version": release.chart_version or target_version,
        }

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(
            "Preserving rollback failed for %s/%s: %s\nStderr: %s",
            namespace, name, e.message, e.stderr,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=describe_helm_failure(e.stderr),
        )


@router.get(
    "/{namespace}/{name}/revisions",
    response_model=ReleaseRevisionsResponse,
    summary="Get release revision history",
)
def get_release_revisions(namespace: str, name: str):
    """Get the revision history of a Helm release.

    Args:
        namespace: Release namespace
        name: Release name

    Returns:
        List of release revisions

    Raises:
        HTTPException: If history retrieval fails
    """
    try:
        revisions = helm_service.history(namespace, name)
        return ReleaseRevisionsResponse(revisions=revisions)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Failed to get history: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get release history. Check operator logs for details.",
        )


def _read_advertised_address(namespace: str, name: str, schemes) -> Optional[str]:
    """兩個位址端點共用：403 namespace 越界、500 kubectl 失敗（不外洩 stderr）。"""
    try:
        return k8s_service.get_advertised_public_address(namespace, name, schemes=schemes)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except KubectlException as e:
        logger.error(
            "Failed to read the advertised public address for %s/%s: %s",
            namespace,
            name,
            e.message,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read the advertised public address. Check operator logs.",
        )


@router.get(
    "/{namespace}/{name}/public-address",
    response_model=PublicAddressResponse,
    summary="Get the public address advertised by the release's ngrok sidecar",
)
def get_public_address(namespace: str, name: str):
    """對外位址（chart 的 ngrok sidecar；EMQX 的 MQTT、Headscale 的控制網址）。

    平台的 Cloudflare 通道只載 HTTP/WS，而且會剝掉 Tailscale 控制協議的
    ``Upgrade`` header，所以原生 MQTT（TCP）與 Headscale 控制面（HTTPS）都走租戶
    自己的 ngrok agent（chart 的 sidecar）。agent 實際拿到的位址只有它自己知道
    （隨機指派、或 dev domain 由 agent API 才查得到），它每 60 秒把當前位址印到
    自己的 stdout，平台從那裡讀回來。log 契約見
    ``KubernetesService.get_advertised_public_address``。

    回 200 ＋ ``public_url: null`` 是正常狀態（租戶沒開、agent 還沒連上、sidecar
    撤回、pod 還沒起來），**不是**錯誤；只有 kubectl 本身失敗才會是 5xx。

    Args:
        namespace: Release namespace
        name: Release name

    Returns:
        The advertised ``tcp://host:port`` / ``https://host[:port]``, or
        ``public_url: null`` when there is none.

    Raises:
        HTTPException: 403 namespace 越界、500 kubectl 失敗
    """
    return PublicAddressResponse(
        public_url=_read_advertised_address(
            namespace, name, KubernetesService.PUBLIC_ADDRESS_SCHEMES
        )
    )


@router.get(
    "/{namespace}/{name}/mqtt-address",
    response_model=MqttAddressResponse,
    summary="[Legacy] Get the public MQTT address advertised by the release's ngrok sidecar",
)
def get_mqtt_address(namespace: str, name: str):
    """舊端點：對外 MQTT 位址（charts/emqx）。

    平台 ≤ 18.0.1.0.205 呼叫這支；operator 必須比改呼叫 ``/public-address`` 的平台版本
    先上線，所以這支要留到沒有任何已部署的平台還在呼叫為止。行為與舊版相同：只回
    ``tcp://`` 位址（marker 新舊兩種都認，見 ``get_public_address``）。

    Raises:
        HTTPException: 403 namespace 越界、500 kubectl 失敗
    """
    return MqttAddressResponse(
        public_url=_read_advertised_address(namespace, name, ("tcp",))
    )


@router.get(
    "/{namespace}/{name}/status",
    response_model=ReleaseStatusResponse,
    summary="Get release and pod status",
)
def get_release_status(
    namespace: str,
    name: str,
    include_vm: bool = Query(
        default=False,
        description=(
            "#423 W4.2: also return KubeVirt VMIs and CDI DataVolumes for this "
            "release. Defaults to false so existing callers are unaffected and "
            "container-only clusters never pay for the extra CR lookups."
        ),
    ),
):
    """Get detailed status including pod information.

    Args:
        namespace: Release namespace
        name: Release name
        include_vm: Include VMI/DataVolume status (VM-type workloads)

    Returns:
        Release and pod status information

    Raises:
        HTTPException: If status retrieval fails
    """
    try:
        # Get release info
        release = helm_service.get(namespace, name)

        selector = f"app.kubernetes.io/instance={name}"

        # Get pods in namespace (filter by app.kubernetes.io/instance label)
        pods = []
        pod_retrieval_error = None
        try:
            pods = k8s_service.get_pods(
                namespace=namespace,
                label_selector=selector,
                # #1417：未就緒的 Pod 附最近一筆 Warning 事件，讓平台的部署逾時訊息
                # 說得出「卡在哪」。事件查詢失敗由 get_pods 自己吞掉，不會落到下面。
                include_warning_events=True,
            )
        except Exception as e:
            logger.error(f"Failed to get pods for {namespace}/{name}: {e}")
            pod_retrieval_error = str(e)

        # #423 W4.2: VM-type workloads. A virt-launcher pod being Running does
        # NOT mean the guest booted, so the platform's readiness gate reads the
        # VMI instead. Errors are surfaced (never swallowed) because the Odoo
        # side is fail-CLOSED on an empty vmis list: it keeps the service in
        # `deploying`, and without these fields a broken lookup would be
        # indistinguishable from "still importing".
        #
        # None (not []) while include_vm is false, so "not queried" and
        # "queried, none found" stay distinguishable — a caller that forgets
        # the flag gets a diagnosable None instead of a silently-stuck service.
        vmis = None
        datavolumes = None
        vmi_retrieval_error = None
        datavolume_retrieval_error = None
        if include_vm:
            # Separate try blocks per resource kind: a DataVolume failure must
            # not discard an already-known VMI readiness answer (and vice
            # versa). Readiness depends only on the VMI half.
            try:
                vmis = k8s_service.get_vmis(
                    namespace=namespace, label_selector=selector,
                )
            except Exception as e:
                logger.error(f"Failed to get VMIs for {namespace}/{name}: {e}")
                vmi_retrieval_error = str(e)
            try:
                datavolumes = k8s_service.get_datavolumes(
                    namespace=namespace, label_selector=selector,
                )
            except Exception as e:
                logger.error(
                    f"Failed to get DataVolumes for {namespace}/{name}: {e}"
                )
                datavolume_retrieval_error = str(e)

        return ReleaseStatusResponse(
            release=release,
            pods=pods,
            pod_retrieval_error=pod_retrieval_error,
            # #1263：get_pods 一律填身分欄位；與這次抓 Pod 成敗無關（見 schema 註解）。
            pod_identity_included=True,
            vm_included=include_vm,
            vmis=vmis,
            datavolumes=datavolumes,
            vmi_retrieval_error=vmi_retrieval_error,
            datavolume_retrieval_error=datavolume_retrieval_error,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Failed to get status: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get release status. Check operator logs for details.",
        )


@router.post(
    "/{namespace}/{name}/scale",
    response_model=Dict[str, str],
    summary="Scale a release's deployments",
)
def scale_release(namespace: str, name: str, replicas: int = Query(default=0, ge=0, le=10)):
    """Scale all workloads (Deployments + StatefulSets + VirtualMachines).

    Used for subscription suspend (replicas=0) and resume (replicas=1).
    Bitnami charts deploy their primary workload as a StatefulSet so we
    cover both kinds; releases that have none of the three are a 404.

    #423 W4.3 — VM-type releases (HAOS) have NO replicas to scale. Without
    the VirtualMachine branch below, suspending such a service hit the 404
    here, the Odoo caller caught the exception and wrote the service to
    `error` — i.e. every unpaid HAOS tenant would land in a bogus error
    state instead of being suspended. VMs map to `runStrategy` instead:
    Halted (stop the guest, keep the definition + disk) / Always.

    #423 W6.3 — 三種 workload **一律都查、都處理**：一個 release 可以同時擁有
    容器與 VM（HAOS chart 0.1.2 起有 xff-proxy Deployment + VirtualMachine）。
    早期版本只在沒有容器 workload 時才查 VM，混合拓撲下等於 suspend 只停了
    proxy、VM 照跑照計費——詳見下方 VM lookup 前的註解。

    Args:
        namespace: Release namespace
        name: Release name
        replicas: Target replica count (0 to suspend, 1+ to resume)

    Returns:
        Confirmation message with scaled workload counts.

    Raises:
        HTTPException: If scaling fails
    """
    selector = f"app.kubernetes.io/instance={name}"
    try:
        deployments = k8s_service.get_deployments(
            namespace=namespace,
            label_selector=selector,
        )
        statefulsets = k8s_service.get_statefulsets(
            namespace=namespace,
            label_selector=selector,
        )

        # 三種 workload 一律都查、都處理——**不再**拿「有沒有容器 workload」當閘門。
        #
        # W4 原本只在 deployments 與 statefulsets 都空時才查 VM，為的是替沒有
        # KubeVirt 的叢集省一次 CRD lookup。那個前提假設「一個 release 不是純容器
        # 就是純 VM」，而它在 chart 0.1.2 替 HAOS 加上 xff-proxy Deployment 的當下
        # 就破了：release 同時擁有 Deployment 與 VirtualMachine，閘門於是永遠不成立，
        # suspend 只把 proxy scale 到 0，VM 從頭到尾沒有被 patch runStrategy。
        #
        # stg 實測（#423 W6.3 紅燈，現場 paas-ws-stg-0f5bf95d / svc-5851cdbf）：
        # Odoo state=suspended、proxy replicas=0，但 VM 仍 Running，ns quota 只釋放
        # 50m —— VM 的 2000m/4.4Gi 照燒。這比 W4 修掉的 404 更危險：欠費停權在帳面上
        # 「成功」了，成本卻全額繼續產生，沒有任何錯誤訊號可循（operator log 整段只有
        # get deployments/statefulsets + scale deployment，零 VM 操作）。
        #
        # 省下一次 lookup 的代價，是把「這個 release 究竟有哪些 workload」變成基於
        # chart 拓撲的猜測。混合拓撲只會越來越多（proxy / sidecar / 只有 init 的
        # chart），所以改成無條件查詢：正確性不該依賴 chart 長什麼樣子。代價是沒有
        # KubeVirt 的叢集每次 scale 多付一次必定失敗的 CRD lookup，由下面既有的
        # CRD-absent 容忍吸收。
        virtualmachines = []
        try:
            virtualmachines = k8s_service.get_virtualmachines(
                namespace=namespace,
                label_selector=selector,
            )
        except KubectlException as e:
            # ⚠ Narrow to "the CRD does not exist". Everything else —
            # RBAC Forbidden, a 30s timeout, a missing kubectl — must
            # NOT be folded into "no VM workload".
            #
            # This is not hypothetical: KubeVirt CRDs are already
            # installed on woow-k3s while the operator ClusterRole has
            # not yet been republished with the kubevirt.io apiGroup
            # (chart 0.1.19 still in the CI queue). With a blanket
            # catch, the very first VM suspend would go
            #   Forbidden → 404 → _do_suspend writes state=error
            # leaving an unpaid VM running — precisely the failure this
            # branch exists to fix, re-created one layer up.
            #
            # 拆掉閘門後這條規則更要緊：混合拓撲下容器那半會成功，一個被吞掉的
            # Forbidden 會讓端點回 200「已 suspend」，而 VM 依然在跑——正是上面
            # 那個「成功地沒停機」的故障。所以只有 CRD-absent 能被容忍。
            #
            # Note the deliberate asymmetry with the container path
            # above: get_deployments/get_statefulsets do not catch at
            # all, so their failures surface as 500. VM lookups get one
            # narrow exemption and nothing more.
            if not KubernetesService.is_crd_absent_error(e):
                logger.error(
                    f"VirtualMachine lookup failed for {namespace}/{name} "
                    f"(not a missing-CRD error — refusing to report "
                    f"'no workloads'): {e.stderr}"
                )
                raise
            logger.info(
                f"No KubeVirt CRD in this cluster; skipping VirtualMachine "
                f"lookup for {namespace}/{name}"
            )

        if not deployments and not statefulsets and not virtualmachines:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No deployments, statefulsets or virtualmachines found for "
                    f"release {name} in namespace {namespace}"
                ),
            )

        scaled_deps = []
        for dep in deployments:
            dep_name = dep["name"]
            k8s_service.scale_deployment(namespace, dep_name, replicas)
            scaled_deps.append(dep_name)
            logger.info(f"Scaled deployment {dep_name} in {namespace} to {replicas} replicas")

        scaled_sts = []
        for sts in statefulsets:
            sts_name = sts["name"]
            k8s_service.scale_statefulset(namespace, sts_name, replicas)
            scaled_sts.append(sts_name)
            logger.info(f"Scaled statefulset {sts_name} in {namespace} to {replicas} replicas")

        # replicas==0 is the suspend signal; anything >=1 is resume. A VM has
        # no notion of "2 replicas", so every non-zero value maps to Always.
        run_strategy = "Halted" if replicas == 0 else "Always"
        scaled_vms = []
        for vm in virtualmachines:
            vm_name = vm["name"]
            k8s_service.patch_vm_run_strategy(namespace, vm_name, run_strategy)
            scaled_vms.append(vm_name)
            logger.info(
                f"Set runStrategy={run_strategy} on VM {vm_name} in {namespace} "
                f"(replicas={replicas})"
            )

        total = len(scaled_deps) + len(scaled_sts) + len(scaled_vms)
        return {
            "message": f"Scaled {total} workload(s) to {replicas} replicas",
            "deployments": ", ".join(scaled_deps),
            "statefulsets": ", ".join(scaled_sts),
            "virtualmachines": ", ".join(scaled_vms),
        }

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except KubectlException as e:
        logger.error(f"kubectl scale failed: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to scale workload. Check operator logs for details.",
        )


@router.post(
    "/{namespace}/{name}/restart",
    response_model=Dict[str, Any],
    summary="#1432: rollout-restart every Deployment and StatefulSet of a release",
)
def restart_release(namespace: str, name: str):
    """#1432：租戶的「重新啟動」——對 release 下**所有** Deployment 與 StatefulSet
    下 ``kubectl rollout restart``（改 pod template annotation，由 controller 依各自
    的更新策略重建 Pod；PVC、Secret、values 都不動）。

    - 只「下達」不等待，與 reset-config（#461）同一個理由：收斂交給平台狀態機
      （``upgrading`` → poll → ``running``），等 Pod 會讓回應時間綁在 image pull 與
      RWO PVC re-attach 上。
    - 一律查兩種 workload（scale 端點 #423 W6.3 的教訓：不要猜 chart 拓撲）。
      open-design 的 daemon 是 StatefulSet，只重啟 Deployment 等於沒重啟主程式。
    - VirtualMachine **不**處理（KubeVirt 沒有 rollout 語意；硬關機重開屬另一種
      操作），回應列出略過的 VM，平台據此對 VM 型服務不提供本功能。
    - 一個都沒有 → 404（靜默成功會讓租戶以為重啟了）。
    - 任一個下達失敗 → 500，回應不宣稱成功；已下達的那幾個照常收斂（rollout
      restart 本身是冪等、無害的，沒有需要補償的狀態）。
    """
    _validate_namespace_or_raise(namespace)
    selector = f"app.kubernetes.io/instance={name}"
    try:
        deployments = k8s_service.get_deployments(namespace=namespace, label_selector=selector)
        statefulsets = k8s_service.get_statefulsets(namespace=namespace, label_selector=selector)
        if not deployments and not statefulsets:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No deployments or statefulsets found for release {name} "
                    f"in namespace {namespace}"
                ),
            )
        restarted_deps = []
        for dep in deployments:
            k8s_service.rollout_restart_deployment(namespace, dep["name"])
            restarted_deps.append(dep["name"])
        restarted_sts = []
        for sts in statefulsets:
            k8s_service.rollout_restart_statefulset(namespace, sts["name"])
            restarted_sts.append(sts["name"])
        logger.info(
            "Restart issued for %s/%s: deployments=%s statefulsets=%s",
            namespace, name, restarted_deps, restarted_sts,
        )
        return {
            "action": "restart",
            "namespace": namespace,
            "release": name,
            "deployments": restarted_deps,
            "statefulsets": restarted_sts,
            "rollout_awaited": False,
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except KubectlException as e:
        logger.error(f"Restart failed for {namespace}/{name}: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to restart workloads. Check operator logs for details.",
        )


@router.post(
    "/{namespace}/{name}/reset-config",
    response_model=Dict[str, Any],
    summary="#357/#449：reset admin_password（依 admin_password_carrier 分流）",
)
def reset_release_config(
    namespace: str,
    name: str,
    request: ReleaseResetConfigRequest,
):
    """Reset the admin password, by whichever mechanism the chart actually uses.

    #449: this handler used to assume every chart keeps its password in
    ``/data/config.json``. That is only true of the mcp-server family; charts
    that carry the password in their own Secret (hermes-agent, open-design)
    have no such file, so the exec patch failed, its backup was never created,
    the compensating ``os.replace`` failed too, and every rotation ended in a
    500. ``request.admin_password_carrier`` now selects the path:

    ``helm_secret`` — Helm's Secret *is* the credential store. Upgrade it and
    restart the workloads that read it; see ``_reset_via_helm_secret``.

    ``config_json`` (default, and what an older platform sends) — Helm's Secret
    and the PVC-backed config file cannot be updated in one transaction, so a
    compensating saga is used:

    1. Atomically upgrade the Helm Secret and wait for success.
    2. Read-modify-write only ``admin_password`` in config.json, retaining a
       same-volume backup of the complete original file.
    3. Trigger the workload restart, then remove that backup.

    A failure after step 1 rolls Helm back to the prior revision.  A failure
    after step 2 additionally restores the original config file and rolls the
    workload back.  The password enters the container via exec stdin only, not
    process argv, logs, or a temporary file.

    #461: this handler **no longer waits for the rollout to finish**. The commit
    point is the Helm upgrade (and, here, the config.json write): once those
    succeed the new password is the authoritative one and nothing rolls it back,
    so waiting for pods only made the caller's budget depend on image pull and
    RWO PVC re-attach times — a probabilistic timeout the tenant hit on every
    cold image cache (#459 measured 120.6s / 121.0s failures against a 120s
    client budget, both succeeding on the second press). Rollout convergence is
    the platform state machine's job (``upgrading`` → cron/UI poll → ``running``),
    exactly like the deploy / reconfigure paths. Everything still synchronous is
    still compensated; nothing asynchronous needs compensation.
    """
    _validate_namespace_or_raise(namespace)

    if not request.sensitive_values:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reset-config requires sensitive_values containing new admin_password",
        )

    new_password = (
        (request.sensitive_values or {})
        .get("config", {})
        .get("sensitive", {})
        .get("admin_password")
    )
    if not new_password or not isinstance(new_password, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sensitive_values.config.sensitive.admin_password is required",
        )

    # #552 層 4 —— fail-closed，**刻意放在載體分流之前**。
    #
    # reset 是一次真正的 `helm upgrade`（兩條分支都是），而 `helm_service.upgrade`
    # 對 chart 是必填的（helm 不保存原始 chart URL）。⇒ chart 一定會被重新解析，
    # 少了 `--version` 就是「拿 registry 當下最新的那一版」——租戶按一下換密碼，
    # 底層 chart 靜默跳版，`--atomic` 只在失敗時回滾、成功跳版永久生效。
    #
    # ⚠ 位置是這道閘門的重點：下一行就依 `admin_password_carrier` 分流成
    # `_reset_via_helm_secret` 與底下的 config_json 路，兩條**各有自己的**
    # `helm_service.upgrade(` 呼叫點。守在任一個呼叫點必漏另一條（open-design /
    # hermes-agent 走 helm_secret 那條）。守在分流**之前**一次涵蓋兩條。
    if _chart_ref_is_remote(request.chart) and not request.version:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "reset-config requires an explicit chart version for remote chart "
                "refs — without it Helm resolves the registry's latest and silently "
                "bumps the tenant's chart"
            ),
        )

    if request.admin_password_carrier == "helm_secret":
        return _reset_via_helm_secret(namespace, name, request)

    try:
        previous_revision = helm_service.get(namespace, name).revision
        release = helm_service.upgrade(
            namespace=namespace,
            name=name,
            chart=request.chart,
            # #448/#449：reset 也走全量 reset values，沒有這行時私有 image 的 CS
            # 每次 reset password 都會掉 pull secret → ImagePullBackOff 401。
            # 之前只因呼叫端沿用 reuse_values 預設才沒踩到（#449 comment）。
            values=_ensure_image_pull_secrets(namespace, request.values),
            sensitive_values=request.sensitive_values,
            version=request.version,
            reset_values=request.reset_values,
            reuse_values=request.reuse_values,
            atomic=True,
            # #461: bound BOTH — the per-operation wait and the subprocess. The
            # subprocess ceiling covers `2n + overhead` so an atomic failure gets
            # to finish its rollback instead of being SIGKILLed halfway.
            timeout_seconds=RESET_HELM_TIMEOUT_SECONDS,
            subprocess_timeout_seconds=RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (ValueError, HelmException) as e:
        logger.error("reset-config Helm upgrade failed for %s/%s: %s", namespace, name, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="reset-config: failed to update the chart Secret",
        )

    backup_name = f"/data/.config.json.reset-backup-{uuid.uuid4().hex}"
    # The chart's main image carries python3 for seed-config.py.  Deliberately
    # fail if config.json is absent: replacing it with a generated skeleton
    # would destroy GUI-owned connection/tools state.
    #
    # #461: the leading glob sweep bounds backup accumulation. The cleanup exec at
    # the end of this handler is best-effort, and now that we no longer wait for the
    # rollout it fires while the old pod is already Terminating — so it will often
    # fail and leave one `.config.json.reset-backup-*` behind (a file containing the
    # PREVIOUS plaintext password). Clearing leftovers here caps that at one per
    # release instead of one per reset, while keeping the backup available for the
    # patch-failure compensation below (which is its only consumer).
    script = (
        "import glob, json, os, sys, tempfile;"
        "p='/data/config.json';b=sys.argv[1];"
        "[os.unlink(f) for f in glob.glob('/data/.config.json.reset-backup-*')];"
        "original=open(p,'rb').read();"
        "payload=json.load(sys.stdin);"
        "d=json.loads(original);"
        "d['admin_password']=payload['admin_password'];"
        "open(b,'xb').write(original);"
        "fd,tmp=tempfile.mkstemp(dir='/data',prefix='.config.json.reset-');"
        "os.write(fd,json.dumps(d,indent=2,ensure_ascii=False).encode());"
        "os.fsync(fd);os.close(fd);"
        "os.replace(tmp, p)"
    )

    try:
        pod_name = k8s_service.get_pod_name(
            namespace=namespace,
            label_selector=f"app.kubernetes.io/instance={name}",
        )
        k8s_service.exec_in_pod(
            namespace=namespace,
            pod_name=pod_name,
            container="",
            command=["python3", "-c", script, backup_name],
            timeout=RESET_EXEC_TIMEOUT_SECONDS,
            input_data=json.dumps({"admin_password": new_password}),
        )
    except KubectlException as e:
        recovery_failed = _rollback_reset(
            # The exec connection can fail after the atomic rename. Treat the
            # write as applied unless proven otherwise, so recovery never leaves
            # a potentially patched config.json behind.
            namespace, name, previous_revision, backup_name, config_patched=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "reset-config: failed to patch config.json; previous password restored"
                if not recovery_failed else
                "reset-config: failed to patch config.json and recovery failed; operator intervention required"
            ),
        ) from e

    try:
        # The release name is not the Deployment name (chart fullname renders
        # '{release}-mcp-server'), so discover it the same way the pod lookup
        # above does.
        deployment_name = k8s_service.get_deployment_name(
            namespace=namespace,
            label_selector=f"app.kubernetes.io/instance={name}",
        )
        # #461: issue the restart, do NOT wait for it. Failing to *issue* it is
        # still compensated (below) because it is still synchronous; the rollout
        # itself is the platform state machine's business from here on.
        k8s_service.rollout_restart_deployment(
            namespace=namespace,
            deployment_name=deployment_name,
        )
    except KubectlException as e:
        recovery_error = _rollback_reset(
            namespace, name, previous_revision, backup_name, config_patched=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "reset-config: rollout failed; previous password restored"
                if not recovery_error else
                "reset-config: rollout failed and recovery failed; operator intervention required"
            ),
        ) from e

    try:
        k8s_service.exec_in_pod(
            namespace=namespace,
            pod_name=k8s_service.get_pod_name(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={name}",
            ),
            container="",
            command=["python3", "-c", "import os,sys;os.unlink(sys.argv[1])", backup_name],
            timeout=RESET_CLEANUP_EXEC_TIMEOUT_SECONDS,
        )
    except KubectlException as e:
        # #461: expected to fail more often now — the restart above is already
        # tearing this pod down (Recreate). The patch script's leading glob sweep
        # is the backstop, so a leftover is bounded to one per release.
        logger.warning("reset-config backup cleanup failed for %s/%s: %s", namespace, name, e)

    return {
        "action": "reset-config",
        "namespace": namespace,
        "release": name,
        "helm_revision": release.revision,
        # #552: reset carries `request.version` into a real helm upgrade, so it can
        # move the chart version — report what Helm actually resolved so the
        # platform ledger stops guessing. Mirrored in `_reset_via_helm_secret`:
        # this endpoint has TWO implementations and covering only one leaves
        # open-design / hermes-agent (the helm_secret carrier) blind.
        "chart_version": release.chart_version,
        "config_json_patched": True,
        "helm_upgraded": True,
        "rollout_restart_issued": True,
        # #461: explicit so a caller can tell "restart issued" from "pods ready".
        "rollout_awaited": False,
        "admin_password_carrier": "config_json",
        "note": "preserve connection/tools — read-modify-write only admin_password",
    }


def _reset_via_helm_secret(
    namespace: str,
    name: str,
    request: ReleaseResetConfigRequest,
) -> Dict[str, Any]:
    """#449：密碼載體＝chart Secret 的 reset 路（hermes-agent / open-design）。

    這條路**刻意不碰 config.json**：這些 chart 的 PVC 裡沒有那個檔（hermes-agent
    掛 ``/opt/data``、open-design 掛 ``/app/.od``），對它 exec patch 必炸。

    為什麼 helm upgrade 之後還一定要 rollout restart：兩個 chart 的 Deployment
    都**沒有** ``checksum/secret`` annotation（實查 charts/hermes-agent 與
    charts/open-design 的 deployment.yaml：只有 checksum/config 與
    checksum/authproxy-config）。改 Secret 不改 pod template ⇒ helm 不會滾動 pod，
    而密碼是**啟動期**被消費的（nginx basic-auth 的 htpasswd 由 initContainer 從
    Secret 算出、webui/dashboard 走 env）。少了 restart，upgrade 會「成功」但新密碼
    永遠不生效 —— 比 500 更糟的靜默失敗。

    為什麼是**全部** Deployment 而非一個：兩個 chart 都渲染 daemon + console 兩顆
    Deployment 共用同一個 ``app.kubernetes.io/instance`` label，單數版
    ``get_deployment_name`` 撞多筆會 raise「Ambiguous selector」→ 500（#449 brief
    的原始預測）。這裡用複數版一次收全，未來 chart 再長出第三顆也自動涵蓋。

    失敗補償與 config_json 路同形：先 helm rollback 回前一個 revision，再把
    Deployment 重新滾一次，讓**舊密碼**確實回到 pod。

    #461：**restart 只下達、不等它跑完**。提交點是 helm upgrade——Secret 一換完，
    新密碼就是權威值且不再自動回退；繼續等 pod 只是把呼叫端的預算綁在 image pull
    與 RWO PVC re-attach 上（#459 實測 hermes 冷 cache 光 pull 954MB 就 70s、
    PVC detach→attach 19s），那是機率性逾時而非確定性失敗。rollout 的收斂交給
    平台狀態機（``upgrading`` → cron／前端輪詢 → ``running``），與 deploy／
    reconfigure 同形。
    """
    label_selector = f"app.kubernetes.io/instance={name}"

    try:
        previous_revision = helm_service.get(namespace, name).revision
        release = helm_service.upgrade(
            namespace=namespace,
            name=name,
            chart=request.chart,
            # #448：全量 reset values 會抹掉 install 時注入的 pull secret，兩條
            # reset 路都必須自己補回來。
            values=_ensure_image_pull_secrets(namespace, request.values),
            sensitive_values=request.sensitive_values,
            version=request.version,
            reset_values=request.reset_values,
            reuse_values=request.reuse_values,
            atomic=True,
            # #461：見 config_json 路的同一段註解——subprocess 上限必須蓋住
            # `2n + overhead`，否則 atomic 失敗時的自動 rollback 會被腰斬。
            timeout_seconds=RESET_HELM_TIMEOUT_SECONDS,
            subprocess_timeout_seconds=RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (ValueError, HelmException) as e:
        logger.error(
            "reset-config (helm_secret) Helm upgrade failed for %s/%s: %s",
            namespace, name, e,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="reset-config: failed to update the chart Secret",
        )

    statefulset_names: List[str] = []
    try:
        deployment_names, statefulset_names = _credential_reset_workloads(
            namespace, label_selector,
        )
        # #461：全部下 restart 後**直接回**，不再逐一等 rollout。原本的「先全部下、
        # 再逐一等」是為了讓兩顆並行推進（總耗時 ≈max 而非 sum）以塞進 120s 預算；
        # 現在等待整段移出 request，那個最佳化不再需要——但**下達順序仍保留全部下完
        # 才 return**，因為任何一顆漏下就等於那個入口還吃舊密碼（#449 的靜默失敗）。
        for deployment_name in deployment_names:
            k8s_service.rollout_restart_deployment(
                namespace=namespace,
                deployment_name=deployment_name,
            )
        # hermes-agent >= 0.3.0、headscale／tailscale >= 0.1.1：吃密碼的是 StatefulSet
        # （opt-in label），一樣要滾。
        for statefulset_name in statefulset_names:
            k8s_service.rollout_restart_statefulset(
                namespace=namespace,
                statefulset_name=statefulset_name,
            )
    except KubectlException as e:
        recovery_failed = _rollback_helm_secret_reset(
            namespace, name, previous_revision, label_selector,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "reset-config: rollout failed; previous password restored"
                if not recovery_failed else
                "reset-config: rollout failed and recovery failed; operator intervention required"
            ),
        ) from e

    return {
        "action": "reset-config",
        "namespace": namespace,
        "release": name,
        "helm_revision": release.revision,
        # #552: same contract as the config_json branch above — both reset paths
        # run `helm upgrade` and both must report the resolved chart version.
        "chart_version": release.chart_version,
        "config_json_patched": False,
        "helm_upgraded": True,
        "rollout_restart_issued": True,
        # #461: explicit so a caller can tell "restart issued" from "pods ready".
        "rollout_awaited": False,
        "admin_password_carrier": "helm_secret",
        "restarted_deployments": deployment_names,
        "restarted_statefulsets": statefulset_names,
        "note": "password lives in the chart Secret — no config.json patch",
    }


def _credential_reset_workloads(
    namespace: str, label_selector: str,
) -> Tuple[List[str], List[str]]:
    """Every workload a helm_secret reset must roll: (deployments, statefulsets).

    Deployments: all of the release (#449). StatefulSets: only those carrying
    ``CREDENTIAL_RESET_RESTART_LABEL`` (hermes-agent >= 0.3.0 runs its agent as a
    StatefulSet — platform #1360 follow-up — and reads admin_password at start).
    A release may now have zero Deployments (hermes-agent with console.enabled=false;
    headscale/tailscale >= 0.1.1, which are StatefulSet-only charts — before this,
    their reset always ended in "No deployments found" → rollback → 500), so
    "no Deployment" is only an error when no opted-in StatefulSet exists either:
    restarting nothing would report a rotation that never reached a pod.
    """
    try:
        deployment_names = k8s_service.get_deployment_names(
            namespace=namespace,
            label_selector=label_selector,
        )
    except KubectlException as e:
        if "No deployments found" not in (e.message or ""):
            raise
        deployment_names = []
    statefulset_names = k8s_service.get_credential_reset_statefulset_names(
        namespace=namespace,
        label_selector=label_selector,
    )
    if not deployment_names and not statefulset_names:
        raise KubectlException(
            message=f"No deployments or opted-in statefulsets found matching selector '{label_selector}'",
            command=f"get deployments,statefulsets --namespace {namespace} --selector {label_selector}",
            stderr="",
        )
    return deployment_names, statefulset_names


def _rollback_helm_secret_reset(
    namespace: str,
    name: str,
    previous_revision: int,
    label_selector: str,
) -> bool:
    """Compensate a helm_secret reset failure. Returns True when recovery failed.

    Rolling Helm back restores the *old* password in the Secret, but a pod that
    already restarted is running the new one — so the workloads must be rolled
    again for the rollback to actually reach them.

    #461: the re-roll is **issued, not awaited**. Waiting here would make a failing
    reset take up to 2×130s to answer — the compensation path would blow the very
    budget ceiling this change exists to establish, and the wait proves nothing the
    caller can act on (helm has already restored the Secret; the pods converge on
    it whenever they come up). ``recovery_failed`` therefore reflects "did the
    rollback and the re-roll get issued", not "are the pods ready".
    """
    recovery_failed = False
    try:
        helm_service.rollback(namespace, name, previous_revision)
    except HelmException as e:
        logger.error(
            "reset-config (helm_secret) Helm rollback failed for %s/%s: %s",
            namespace, name, e,
        )
        recovery_failed = True

    if recovery_failed:
        # Secret 還停在新密碼；再滾 pod 只會把「沒人持有的密碼」推上線，更糟。
        return recovery_failed

    try:
        deployment_names, statefulset_names = _credential_reset_workloads(
            namespace, label_selector,
        )
        for deployment_name in deployment_names:
            k8s_service.rollout_restart_deployment(namespace, deployment_name)
        for statefulset_name in statefulset_names:
            k8s_service.rollout_restart_statefulset(namespace, statefulset_name)
    except KubectlException as e:
        logger.error(
            "reset-config (helm_secret) rollback rollout failed for %s/%s: %s",
            namespace, name, e,
        )
        recovery_failed = True
    return recovery_failed


def _rollback_reset(
    namespace: str,
    name: str,
    previous_revision: int,
    backup_name: str,
    config_patched: bool,
) -> bool:
    """Compensate a reset failure without reading an old password value.

    #461: like ``_rollback_helm_secret_reset``, the re-roll is issued but not
    awaited — a failing reset must still answer within the budget ceiling.
    """
    recovery_failed = False
    if config_patched:
        try:
            pod_name = k8s_service.get_pod_name(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={name}",
            )
            k8s_service.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container="",
                command=[
                    "python3", "-c",
                    "import os,sys;p,b=sys.argv[1:];os.replace(b,p)",
                    "/data/config.json", backup_name,
                ],
                timeout=RESET_EXEC_TIMEOUT_SECONDS,
            )
        except KubectlException as e:
            logger.error("reset-config config rollback failed for %s/%s: %s", namespace, name, e)
            recovery_failed = True
    try:
        helm_service.rollback(namespace, name, previous_revision)
    except HelmException as e:
        logger.error("reset-config Helm rollback failed for %s/%s: %s", namespace, name, e)
        recovery_failed = True
    if config_patched and not recovery_failed:
        try:
            deployment_name = k8s_service.get_deployment_name(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={name}",
            )
            k8s_service.rollout_restart_deployment(namespace, deployment_name)
        except KubectlException as e:
            logger.error("reset-config rollback rollout failed for %s/%s: %s", namespace, name, e)
            recovery_failed = True
    return recovery_failed


def _validate_namespace_or_raise(namespace: str) -> None:
    """Raise 403 if namespace fails prefix validation. Mirrors helm.py validate_namespace."""
    from src.services.helm import validate_namespace  # late import: avoid cycle in tests
    try:
        validate_namespace(namespace)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.post(
    "/{namespace}/{name}/sidecar",
    response_model=SidecarPatchResponse,
    summary="Patch deployment with sidecar container",
)
async def patch_sidecar(
    namespace: str,
    name: str,
    request: SidecarPatchRequest,
):
    """Patch a release's deployment to add a sidecar container.

    This endpoint uses kubectl JSON patch to inject an additional container
    into the deployment's pod spec. This is useful when the Helm chart does
    not natively support sidecar/extra containers.

    After patching the deployment, this endpoint also:
    1. Creates a ClusterIP K8s Service for the sidecar port (e.g., {release}-mcp)
    2. Creates a Cloudflare Tunnel route so the sidecar is externally reachable

    Args:
        namespace: Release namespace
        name: Release name (used to find the deployment if deployment_name not provided)
        request: Sidecar container specification

    Returns:
        Confirmation of the sidecar patch including MCP endpoint URL

    Raises:
        HTTPException: If patching fails
    """
    try:
        # Determine which deployment to patch
        deployment_name = request.deployment_name
        if not deployment_name:
            # Auto-detect deployment from release label
            deployments = k8s_service.get_deployments(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={name}",
            )
            if not deployments:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No deployments found for release {name} in namespace {namespace}",
                )
            deployment_name = deployments[0]["name"]

        # Build the sidecar container spec for JSON patch
        container_spec = {
            "name": request.container.name,
            "image": request.container.image,
        }
        if request.container.ports:
            container_spec["ports"] = request.container.ports
        if request.container.env:
            container_spec["env"] = request.container.env
        if request.container.resources:
            container_spec["resources"] = request.container.resources
        if request.container.liveness_probe:
            container_spec["livenessProbe"] = request.container.liveness_probe
        if request.container.readiness_probe:
            container_spec["readinessProbe"] = request.container.readiness_probe

        # Build JSON patch to add container to pod spec
        patch = json.dumps([{
            "op": "add",
            "path": "/spec/template/spec/containers/-",
            "value": container_spec,
        }])

        k8s_service.patch_deployment(
            namespace=namespace,
            deployment_name=deployment_name,
            patch=patch,
            patch_type="json",
        )

        logger.info(
            f"Patched deployment {deployment_name} in {namespace} "
            f"with sidecar container {request.container.name}"
        )

        # Create K8s Service and Cloudflare route for the sidecar
        mcp_service_name = None
        mcp_endpoint_url = None
        mcp_internal_url = None

        sidecar_port = _get_sidecar_port(request)
        if sidecar_port:
            mcp_service_name, mcp_internal_url, mcp_endpoint_url = (
                await _create_sidecar_service(
                    namespace=namespace,
                    release_name=name,
                    sidecar_port=sidecar_port,
                )
            )

        return SidecarPatchResponse(
            message=f"Sidecar container '{request.container.name}' added to deployment '{deployment_name}'",
            deployment=deployment_name,
            namespace=namespace,
            container_name=request.container.name,
            mcp_service_name=mcp_service_name,
            mcp_endpoint_url=mcp_endpoint_url,
            mcp_internal_url=mcp_internal_url,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except KubectlException as e:
        logger.error(f"kubectl patch failed: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to patch deployment with sidecar. Check operator logs for details.",
        )


# Helper functions for sidecar service and route creation


def _get_sidecar_port(request: SidecarPatchRequest) -> Optional[int]:
    """Extract the sidecar container port from the patch request.

    Args:
        request: Sidecar patch request

    Returns:
        First container port number, or None if no ports defined
    """
    if not request.container.ports:
        return None
    for port_spec in request.container.ports:
        port = port_spec.get("containerPort")
        if port:
            return port
    return None


async def _create_sidecar_service(
    namespace: str,
    release_name: str,
    sidecar_port: int,
) -> tuple:
    """Create the in-cluster ClusterIP Service for the MCP sidecar.

    Creates a ClusterIP K8s Service named ``{release_name}-mcp`` targeting the
    sidecar port, so the agent data-plane reaches the MCP sidecar via in-cluster
    DNS (``{release}-mcp.{ns}.svc.cluster.local``).

    #301: this deliberately does NOT create a public Cloudflare ``{subdomain}-mcp``
    route for the sidecar. That route was dead weight (Odoo drops the public URL,
    the agent data-plane uses in-cluster DNS) and an unprotected public attack
    surface (no CF Access; the #280 baseline NP allows cloudflared→sidecar; only
    the sidecar's bearer token guards it). The release-teardown path still deletes
    any residual ``{subdomain}-mcp`` route so pre-existing orphans converge. See
    decision doc ``agent-dataplane-in-cluster-no-cf-access-ip-bypass``.

    Service creation failure does NOT propagate - the sidecar patch is already done.

    Args:
        namespace: Kubernetes namespace
        release_name: Helm release name
        sidecar_port: Port the sidecar listens on

    Returns:
        Tuple of (mcp_service_name, mcp_internal_url, mcp_endpoint_url). By design
        (#301) mcp_endpoint_url is always None; mcp_internal_url is None if Service
        creation failed.
    """
    mcp_service_name = f"{release_name}-mcp"
    mcp_internal_url = None

    # Create the in-cluster ClusterIP Service for the sidecar
    service_manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": mcp_service_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/instance": release_name,
                "app.kubernetes.io/component": "mcp-sidecar",
            },
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {
                "app.kubernetes.io/instance": release_name,
            },
            "ports": [
                {
                    "port": sidecar_port,
                    "targetPort": sidecar_port,
                    "protocol": "TCP",
                    "name": "mcp",
                },
            ],
        },
    }

    try:
        k8s_service.apply_manifest(service_manifest)
        mcp_internal_url = (
            f"http://{mcp_service_name}.{namespace}.svc.cluster.local:{sidecar_port}"
        )
        logger.info(
            f"Created K8s Service {mcp_service_name} in {namespace} "
            f"targeting port {sidecar_port}"
        )
    except Exception as e:
        logger.error(
            f"Failed to create K8s Service for sidecar: {e}. "
            "Sidecar patch was successful but service is not exposed."
        )
        return mcp_service_name, None, None

    # #301: no public Cloudflare route for the sidecar (see docstring). The MCP
    # sidecar is reachable only in-cluster (agent data-plane) or via port-forward:
    #   kubectl port-forward -n {ns} svc/{release}-mcp {port}:{port}
    return mcp_service_name, mcp_internal_url, None


# Helper functions for Cloudflare integration


async def _create_cloudflare_route(
    namespace: str,
    release_name: str,
    expose_config,
) -> Optional[RouteInfo]:
    """Create a Cloudflare Tunnel route for a release.

    Args:
        namespace: Kubernetes namespace
        release_name: Helm release name
        expose_config: ExposeConfig from request

    Returns:
        ``None`` **only** when there is nothing to report: the Cloudflare integration
        is switched off for this deployment. Every other outcome returns a
        :class:`RouteInfo` whose ``status`` says whether it worked.

        🔴 Before #648 (a) this function returned a bare ``None`` for *five* different
        situations — integration off, no service found, no usable port, Cloudflare
        error, unexpected error — so the caller could not tell "this deployment has no
        public URL by design" apart from "expose was requested and failed". Since
        ``cloudflare_enabled`` defaults to **false**, any caller that read a missing
        route as a failure would turn **every** deployment on a Cloudflare-less
        operator into an error. See
        ``docs/tracking/pitfalls/operator-route-none-conflates-disabled-and-failed.md``
        in the platform repo.
    """
    if not cloudflare_service.enabled:
        # Not a failure: this deployment simply has no public URL. Keep returning
        # None so "no route to report" stays one single, unambiguous shape.
        logger.info("Cloudflare integration disabled, skipping route creation")
        return None

    subdomain = None
    service_url = None

    def _failed(reason: str) -> RouteInfo:
        """Expose was requested and did not work — say so explicitly."""
        logger.warning(
            "Cloudflare route not created for %s/%s: %s",
            namespace, release_name, reason,
        )
        return RouteInfo(
            # 🔴 不知道就回 None，**不要**填空字串。兩條失敗路徑（找不到 service／
            # 推不出 port）發生在 service_url 被算出來之前，硬填 "" 會讓呼叫端分不出
            # 「沒算出來」與「算出來是空的」——那正是本修法要消滅的那個病
            # （bare None 承載兩種語意）換個位置重演一次。
            hostname=(
                f"{subdomain}.{settings.cloudflare_domain}" if subdomain else None
            ),
            service_url=service_url,
            enabled=False,
            status=RouteStatus.FAILED,
            # 這裡失敗的是整條 route；DNS 那一步有沒有問題我們不知道，
            # 不知道就不要宣稱（`dns_failed` 只在確實嘗試過而失敗時才為真）。
            dns_failed=False,
        )

    try:
        # Determine subdomain
        subdomain = expose_config.subdomain
        if not subdomain:
            subdomain = cloudflare_service.generate_subdomain(namespace, release_name)

        # Determine service name and port
        service_name = expose_config.service_name
        service_port = expose_config.service_port

        if not service_name or not service_port:
            # Auto-detect from K8s services
            # Wait a bit for services to be created
            await asyncio.sleep(3)

            services = k8s_service.get_services(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={release_name}",
            )

            if not services:
                return _failed("no services found for this release")

            # Find first non-headless service with HTTP-like port
            for svc in services:
                # Skip headless services
                if svc.get("clusterIP") == "None":
                    continue

                # Use provided values or detect from service
                if not service_name:
                    service_name = svc["name"]

                if not service_port:
                    # Prefer common HTTP ports
                    http_ports = [80, 8080, 3000, 8000, 443, 8443]
                    for port_info in svc.get("ports", []):
                        port = port_info.get("port")
                        if port in http_ports:
                            service_port = port
                            break
                    # Fallback to first port
                    if not service_port and svc.get("ports"):
                        service_port = svc["ports"][0].get("port")

                if service_name and service_port:
                    break

        if not service_name or not service_port:
            return _failed("could not determine which service/port to expose")

        # Generate internal service URL
        service_url = cloudflare_service.generate_service_url(
            namespace=namespace,
            service_name=service_name,
            port=service_port,
        )

        # Create the route
        outcome = await cloudflare_service.create_route(
            subdomain=subdomain,
            service_url=service_url,
        )

        hostname = f"{subdomain}.{settings.cloudflare_domain}"
        logger.info(f"Created Cloudflare route: {hostname} -> {service_url}")
        if outcome.dns_failed:
            # Tunnel ingress exists but the hostname will not resolve. HTTP 201 is
            # still correct (the helm release installed fine) — the caller is told
            # via this flag, exactly like TunnelCreateResponse.dns_failed (#484).
            logger.error(
                "Route %s created without its DNS record — the hostname will not "
                "resolve until someone creates it", hostname,
            )

        return RouteInfo(
            hostname=hostname,
            service_url=service_url,
            enabled=True,
            status=RouteStatus.OK,
            dns_failed=outcome.dns_failed,
        )

    except CloudflareException as e:
        # Still don't fail the release creation (the helm install succeeded) — but
        # the caller now learns that expose was asked for and did not happen.
        return _failed(f"Cloudflare API error: {e.message}")
    except Exception as e:  # noqa: BLE001 — route failure must not sink the release
        return _failed(f"unexpected error: {e}")
