"""API endpoints for Helm release management."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src.config import settings
from src.models.schemas import (
    MqttAddressResponse,
    ReleaseCreateRequest,
    ReleaseInfo,
    ReleaseResetConfigRequest,
    ReleaseRevisionsResponse,
    ReleaseRollbackRequest,
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
from src.services.helm import _sanitize_release_values


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

    merged = dict(values or {})
    merged.setdefault(
        "imagePullSecrets", [{"name": settings.registry_secret_name}]
    )
    return merged


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
        HTTPException: If installation fails
    """
    try:
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
        release = helm_service.upgrade(
            namespace=namespace,
            name=name,
            chart=request.chart,
            values=_ensure_image_pull_secrets(namespace, request.values),
            sensitive_values=request.sensitive_values,
            version=request.version,
            reset_values=request.reset_values,
            reuse_values=request.reuse_values,
        )
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Helm upgrade failed. Check operator logs for details.",
        )


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


@router.delete(
    "/{namespace}/{name}",
    response_model=Dict[str, str],
    summary="Uninstall a Helm release",
)
async def delete_release(namespace: str, name: str, subdomain: Optional[str] = None):
    """Uninstall a Helm release.

    Args:
        namespace: Release namespace
        name: Release name
        subdomain: Optional subdomain to delete from Cloudflare (if not provided, auto-generated)

    Returns:
        Deletion confirmation message

    Raises:
        HTTPException: If uninstallation fails
    """
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
                labels = build_orphan_pvc_labels(
                    name, settings.pvc_grace_period_days
                )
                k8s_service.label_pvcs(namespace, remaining_pvcs, labels)
                logger.info(
                    f"Labeled {len(remaining_pvcs)} orphaned PVCs in {namespace}: "
                    f"{remaining_pvcs}"
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
                labels = build_orphan_pvc_labels(
                    name, settings.pvc_grace_period_days
                )
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
    response_model=Dict[str, str],
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
    try:
        result = helm_service.rollback(namespace, name, request.revision)
        logger.info(f"Rolled back release {name} in {namespace}")
        return result

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


@router.get(
    "/{namespace}/{name}/mqtt-address",
    response_model=MqttAddressResponse,
    summary="Get the public MQTT address advertised by the release's ngrok sidecar",
)
def get_mqtt_address(namespace: str, name: str):
    """對外 MQTT 位址（charts/emqx）。

    平台的 Cloudflare 通道只載 HTTP/WS，所以原生 MQTT 走租戶自己的 ngrok agent
    （chart 的 sidecar）。固定位址（ngrok 保留位址）平台本來就知道——它是租戶填進
    helm values 的；本端點要解的是**隨機指派**那一種：agent 每次連線拿到的位址只有
    agent 自己知道，它每 60 秒把當前位址印到自己的 stdout，平台從那裡讀回來。

    回 200 ＋ ``public_url: null`` 是正常狀態（租戶沒開、agent 還沒連上、pod 還沒起來），
    **不是**錯誤；只有 kubectl 本身失敗才會是 5xx。

    Args:
        namespace: Release namespace
        name: Release name

    Returns:
        The advertised address, or ``public_url: null`` when there is none.

    Raises:
        HTTPException: 403 namespace 越界、500 kubectl 失敗
    """
    try:
        return MqttAddressResponse(
            public_url=k8s_service.get_advertised_mqtt_address(namespace, name)
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except KubectlException as e:
        logger.error(
            "Failed to read the advertised MQTT address for %s/%s: %s",
            namespace,
            name,
            e.message,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read the advertised MQTT address. Check operator logs.",
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

    try:
        deployment_names = k8s_service.get_deployment_names(
            namespace=namespace,
            label_selector=label_selector,
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
        "note": "password lives in the chart Secret — no config.json patch",
    }


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
        deployment_names = k8s_service.get_deployment_names(
            namespace=namespace,
            label_selector=label_selector,
        )
        for deployment_name in deployment_names:
            k8s_service.rollout_restart_deployment(namespace, deployment_name)
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
