"""woow_paas_platform #1540：停權／刪除後的限時唯讀資料取回入口（API）。

| 方法 | 路徑 | 用途 |
|---|---|---|
| POST   | ``/api/releases/{ns}/{release}/retrieval`` | 開（201，回一次性密碼） |
| GET    | ``/api/releases/{ns}/{release}/retrieval`` | 狀態 |
| DELETE | ``/api/releases/{ns}/{release}/retrieval`` | 關（冪等） |
| POST   | ``/api/retrievals/sweep``                   | 清掉所有已到期的入口（平台 cron 每輪呼叫） |

錯誤一律 4xx／5xx＋``detail.error_code``，**不回 404**（平台把 404 當成沒有這組端點的舊
operator）。規則與 manifest 在 ``services/data_retrieval.py``（純函式）。

開的順序（每一步失敗都回滾前面建的東西）：
1. 讀現況（PVC、workload、pod、helm release、既有入口）→ 依 profile 驗證（``_plan_open``）。
2. 挑一個 tunnel 上**完全沒有規則**的 hostname（``create_route`` 遇到同名會覆寫，所以先擋）。
3. ``kubectl create`` Secret（錨點：同名已存在＝這個 release 已有入口 ⇒ 409，什麼都不動）。
4. 刪掉指向本 release 取回 Service 的舊 route（上一次關閉時沒刪成的）。
5. 建 ConfigMap、NetworkPolicy、Service、Pod。
6. ``create_route``。

關的順序：先刪 k8s 物件（PVC 立刻釋放，復權的服務 pod 才掛得上）、再刪 route。
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src.config import settings
from src.models.schemas import (
    RetrievalCloseResponse,
    RetrievalOpenRequest,
    RetrievalOpenResponse,
    RetrievalRouteStatus,
    RetrievalState,
    RetrievalStatusResponse,
    RetrievalSweepResponse,
)
from src.services import data_retrieval as dr
from src.services.cloudflare import CloudflareException, CloudflareService
from src.services.helm import (
    KubectlException,
    KubernetesService,
    _cloudflared_namespace,
    validate_label_value,
    validate_namespace,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["retrieval"])

k8s_service = KubernetesService()
cloudflare_service = CloudflareService()

#: 關閉時一次刪掉的物件種類（全部帶 ``data_retrieval.label_selector(release)``）。
RETRIEVAL_KINDS = "pods,services,secrets,configmaps,networkpolicies"
#: 開的時候若上一個入口還有殘骸（沒有錨點 Secret），先清掉；Pod 還在收尾就請呼叫端稍後再試。
CLOSING_ERROR_CODE = "retrieval_closing"
_RELEASE_NAME_RE = re.compile(r"\A[a-z0-9]([a-z0-9-]{0,51}[a-z0-9])?\Z")
_SUBDOMAIN_ATTEMPTS = 3


def _http_error(exc: dr.RetrievalError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error_code": exc.error_code, "message": exc.message},
    )


def _validate_target(namespace: str, release: str) -> None:
    """namespace 前綴（租戶邊界）＋release 名稱（會拼進 label selector）。不合格 403（同 releases）。"""
    try:
        validate_namespace(namespace)
        if not _RELEASE_NAME_RE.match(release or ""):
            raise ValueError(f"Invalid release name {release!r}")
        validate_label_value(dr.LABEL_RETRIEVAL_FOR, release)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


def _ts(epoch: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch else None


def _hostname(subdomain: Optional[str]) -> Optional[str]:
    return f"{subdomain}.{cloudflare_service.domain}" if subdomain else None


def _kubectl_error(action: str, e: KubectlException) -> HTTPException:
    logger.error("Data retrieval %s failed: %s %s", action, e.message, (e.stderr or "").strip()[:500])
    return _http_error(dr.RetrievalError(
        503, dr.KUBECTL_ERROR,
        f"Kubernetes call failed while trying to {action} the data retrieval entrance; retry later.",
    ))


# ---------------------------------------------------------------------------
# 現況與判斷
# ---------------------------------------------------------------------------


def _snapshot(namespace: str, release: str) -> Dict[str, Any]:
    """開之前要看的現況（全部唯讀）。"""
    instance = f"app.kubernetes.io/instance={release}"
    own = dr.label_selector(release)
    return {
        "anchor": next(iter(k8s_service.get_items("secrets", namespace, own)), None),
        "leftovers": k8s_service.get_items(
            "pods,services,configmaps,networkpolicies", namespace, own),
        "pvcs": k8s_service.get_items("persistentvolumeclaims", namespace, instance),
        "workloads": k8s_service.get_items("deployments,statefulsets", namespace, instance),
        "pods": k8s_service.get_items("pods", namespace),
        "release_exists": k8s_service.helm_release_exists(namespace, release),
    }


def _plan_open(snapshot: Dict[str, Any], profile: dr.RetrievalProfile, ttl_hours: int,
               now: int) -> Dict[str, Any]:
    """依現況決定能不能開、掛哪顆 PVC、開到什麼時候。不行就 raise ``RetrievalError``。"""
    if snapshot.get("anchor"):
        raise dr.RetrievalError(
            409, dr.ALREADY_OPEN,
            "A data retrieval entrance is already open for this service; close it first.",
        )
    pvc = dr.select_data_pvc(snapshot.get("pvcs") or [], profile)
    pvc_name = pvc["metadata"]["name"]
    data_expires_at = dr.orphan_expiry_epoch(pvc)
    if snapshot.get("release_exists"):
        running = dr.running_workloads(snapshot.get("workloads") or [])
        if running:
            raise dr.RetrievalError(
                409, dr.RELEASE_RUNNING,
                "The service is still running (" + ", ".join(running) + "); data retrieval is "
                "only available while it is suspended (scaled to 0) or after it was deleted.",
            )
    elif data_expires_at is None:
        # release 已不在、PVC 卻沒有保留標記：不是平台刪除流程留下的磁碟，不知道它的保留期。
        raise dr.RetrievalError(
            409, dr.PVC_NOT_FOUND,
            f"The service no longer exists and its data volume {pvc_name} carries no retention "
            f"label; it cannot be opened for retrieval.",
        )
    in_use = dr.pods_using_claim(snapshot.get("pods") or [], pvc_name)
    if in_use:
        raise dr.RetrievalError(
            409, dr.PVC_IN_USE,
            f"The data volume is still mounted by {', '.join(in_use)}; wait until the service "
            f"has fully stopped.",
        )
    ttl_hours = max(1, min(int(ttl_hours), settings.data_retrieval_max_ttl_hours))
    expires_at = now + ttl_hours * 3600
    if data_expires_at is not None:
        if data_expires_at - now < settings.data_retrieval_min_remaining_minutes * 60:
            raise dr.RetrievalError(
                409, dr.RETENTION_EXPIRED,
                "The retention period of this deleted service's data has (almost) ended; "
                "it can no longer be opened.",
            )
        expires_at = min(expires_at, data_expires_at)
    return {"pvc": pvc_name, "expires_at": expires_at, "data_expires_at": data_expires_at}


# ---------------------------------------------------------------------------
# Cloudflare
# ---------------------------------------------------------------------------


async def _fresh_subdomain() -> str:
    """挑一個 tunnel 上沒有任何規則的隨機 hostname（48 bit 隨機，撞到就換）。"""
    for _ in range(_SUBDOMAIN_ATTEMPTS):
        candidate = dr.new_subdomain(settings.paas_env)
        if not await cloudflare_service.hostname_rules(candidate):
            return candidate
    raise dr.RetrievalError(
        503, dr.CLOUDFLARE_ERROR, "Could not pick an unused hostname; retry later.")


async def _remove_stale_routes(namespace: str, release: str, keep: Optional[str]) -> List[str]:
    """刪掉指向本 release 取回 Service 的舊 route（只認本功能的 hostname 形狀）。

    避免上一次關閉時沒刪成的 hostname 指到這一次新開的入口。
    """
    url = dr.service_url(namespace, release)
    suffix = f".{cloudflare_service.domain}"
    removed = []
    for route in await cloudflare_service.list_routes():
        hostname = route.hostname or ""
        if route.service != url or not hostname.endswith(suffix):
            continue
        sub = hostname[: -len(suffix)]
        if sub == keep or not dr.is_retrieval_subdomain(sub):
            continue
        await cloudflare_service.delete_route(sub)
        removed.append(sub)
    if removed:
        logger.warning("Removed stale data retrieval route(s) for %s/%s: %s",
                       namespace, release, removed)
    return removed


async def _close_route(namespace: str, release: str,
                       subdomain: Optional[str]) -> RetrievalRouteStatus:
    """刪掉入口的 route。只刪「每一條規則都指向本 release 取回 Service」的 hostname。

    Raises:
        RetrievalError: Cloudflare 讀寫失敗（``route_cleanup_failed``，502）。
    """
    if not cloudflare_service.enabled:
        return RetrievalRouteStatus.DISABLED
    if not subdomain:
        return RetrievalRouteStatus.NONE
    url = dr.service_url(namespace, release)
    try:
        rules = await cloudflare_service.hostname_rules(subdomain)
        if any(rule.get("service") != url for rule in rules):
            logger.error(
                "Data retrieval close %s/%s: %s has rules pointing elsewhere (%s); not touched",
                namespace, release, subdomain, [r.get("service") for r in rules])
            return RetrievalRouteStatus.NOT_OWNED
        # 沒有 ingress 規則也呼叫：上一次可能只刪了一半（DNS 還在）。delete_dns_record 自己會
        # 確認記錄指向本 tunnel 才刪。
        await cloudflare_service.delete_route(subdomain)
    except CloudflareException as e:
        raise dr.RetrievalError(
            502, dr.ROUTE_CLEANUP_FAILED,
            f"The entrance was shut down but its public hostname {subdomain} could not be "
            f"removed ({e.message}); retry the close.",
        )
    return RetrievalRouteStatus.DELETED if rules else RetrievalRouteStatus.NOT_FOUND


async def _close(namespace: str, release: str,
                 subdomain: Optional[str]) -> RetrievalCloseResponse:
    """關一個入口：先刪 k8s 物件、再刪 route。``subdomain`` 只在錨點已不在時使用。"""
    anchor = next(iter(await asyncio.to_thread(
        k8s_service.get_items, "secrets", namespace, dr.label_selector(release))), None)
    session = dr.session_from_anchor(anchor) if anchor else {}
    sub = session.get("subdomain") or subdomain
    await asyncio.to_thread(
        k8s_service.delete_by_selector, RETRIEVAL_KINDS, namespace, dr.label_selector(release))
    route = await _close_route(namespace, release, sub)
    logger.info("Closed data retrieval %s/%s (anchor=%s, route=%s %s)",
                namespace, release, bool(anchor), sub, route.value)
    return RetrievalCloseResponse(
        status="closed" if anchor else "not_found",
        hostname=_hostname(sub),
        route=route,
    )


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/api/releases/{namespace}/{name}/retrieval",
    response_model=RetrievalOpenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="#1540: open a temporary read-only data retrieval entrance",
)
async def open_retrieval(namespace: str, name: str, request: RetrievalOpenRequest):
    _validate_target(namespace, name)
    profile = dr.PROFILES.get(request.profile)
    if profile is None:
        raise _http_error(dr.RetrievalError(
            422, dr.PROFILE_UNKNOWN, f"Unknown data retrieval profile {request.profile!r}."))
    now = int(time.time())
    try:
        snapshot = await asyncio.to_thread(_snapshot, namespace, name)
    except KubectlException as e:
        raise _kubectl_error("inspect", e)
    try:
        plan = _plan_open(snapshot, profile, request.ttl_hours, now)
    except dr.RetrievalError as e:
        logger.info("Data retrieval open %s/%s refused: %s", namespace, name, e.error_code)
        raise _http_error(e)

    own = dr.label_selector(name)
    leftovers = snapshot.get("leftovers") or []
    if leftovers:
        # 上一個入口的殘骸（錨點已不在）：清掉；Pod 還在收尾就請呼叫端稍後再試（同名 Pod 建不起來）。
        try:
            await asyncio.to_thread(k8s_service.delete_by_selector, RETRIEVAL_KINDS, namespace, own)
        except KubectlException as e:
            raise _kubectl_error("clean up", e)
        if any(item.get("kind") == "Pod" for item in leftovers):
            raise _http_error(dr.RetrievalError(
                409, CLOSING_ERROR_CODE,
                "The previous data retrieval entrance is still shutting down; retry in a minute."))

    subdomain = None
    if cloudflare_service.enabled:
        try:
            subdomain = await _fresh_subdomain()
        except CloudflareException as e:
            logger.error("Data retrieval open %s/%s: Cloudflare read failed: %s",
                         namespace, name, e.message)
            raise _http_error(dr.RetrievalError(
                502, dr.CLOUDFLARE_ERROR, "Cloudflare API error; nothing was created."))
        except dr.RetrievalError as e:
            raise _http_error(e)

    password = dr.new_password()
    manifests = dr.build_manifests(
        namespace=namespace,
        release=name,
        profile=profile,
        pvc_name=plan["pvc"],
        htpasswd=dr.ssha_htpasswd_line(dr.USERNAME, password),
        subdomain=subdomain,
        opened_at=now,
        expires_at=plan["expires_at"],
        image=settings.data_retrieval_image,
        env=settings.paas_env,
        cloudflared_namespace=_cloudflared_namespace(),
        excluded_nodes=[n.strip() for n in settings.data_retrieval_excluded_nodes.split(",")],
    )

    # 3. 錨點 Secret：同名已存在＝別人剛開了（或還沒關乾淨）⇒ 409，什麼都不動。
    try:
        await asyncio.to_thread(k8s_service.create_manifests, manifests[:1])
    except KubectlException as e:
        if KubernetesService.is_already_exists_error(e):
            raise _http_error(dr.RetrievalError(
                409, dr.ALREADY_OPEN,
                "A data retrieval entrance is already open for this service; close it first."))
        raise _kubectl_error("create", e)

    route_created = False

    async def _rollback(reason: str) -> None:
        logger.warning("Rolling back data retrieval %s/%s: %s", namespace, name, reason)
        try:
            await asyncio.to_thread(k8s_service.delete_by_selector, RETRIEVAL_KINDS, namespace, own)
        except KubectlException as exc:
            logger.error("Rollback of data retrieval %s/%s left objects behind: %s",
                         namespace, name, exc.stderr or exc.message)
        if route_created and subdomain:
            try:
                await cloudflare_service.delete_route(subdomain)
            except CloudflareException as exc:
                logger.error("Rollback of data retrieval %s/%s left route %s: %s",
                             namespace, name, subdomain, exc.message)

    try:
        if subdomain:
            # 4. 本 release 舊的取回 route（指向同一個 Service URL）——這時錨點在我們手上。
            await _remove_stale_routes(namespace, name, keep=subdomain)
        # 5. 其餘 k8s 物件（Pod 最後）。
        await asyncio.to_thread(k8s_service.create_manifests, manifests[1:])
        # 6. route。
        dns_failed = False
        route_status = RetrievalRouteStatus.DISABLED
        if subdomain:
            outcome = await cloudflare_service.create_route(
                subdomain=subdomain, service_url=dr.service_url(namespace, name))
            route_created = True
            dns_failed = outcome.dns_failed
            route_status = RetrievalRouteStatus.CREATED
    except KubectlException as e:
        await _rollback(f"kubectl: {e.message}")
        if "exceeded quota" in (e.stderr or "").lower():
            raise _http_error(dr.RetrievalError(
                409, dr.QUOTA_EXCEEDED,
                "The workspace has no CPU/memory quota left for the small retrieval container; "
                "stop another service in this workspace and retry."))
        raise _kubectl_error("create", e)
    except CloudflareException as e:
        await _rollback(f"cloudflare: {e.message}")
        raise _http_error(dr.RetrievalError(
            502, dr.CLOUDFLARE_ERROR,
            "Cloudflare API error while publishing the entrance; nothing was left open."))
    except Exception:
        await _rollback("unexpected error")
        raise

    hostname = _hostname(subdomain)
    logger.info(
        "Opened data retrieval %s/%s profile=%s pvc=%s host=%s expires=%s",
        namespace, name, profile.key, plan["pvc"], hostname, plan["expires_at"])
    return RetrievalOpenResponse(
        hostname=hostname,
        url=f"https://{hostname}/" if hostname else None,
        username=dr.USERNAME,
        password=password,
        opened_at=_ts(now),
        expires_at=_ts(plan["expires_at"]),
        data_expires_at=_ts(plan["data_expires_at"]),
        pvc=plan["pvc"],
        profile=profile.key,
        route=route_status,
        dns_failed=dns_failed,
    )


@router.get(
    "/api/releases/{namespace}/{name}/retrieval",
    response_model=RetrievalStatusResponse,
    summary="#1540: status of the data retrieval entrance",
)
async def get_retrieval(namespace: str, name: str):
    _validate_target(namespace, name)
    own = dr.label_selector(name)
    try:
        anchor = next(iter(await asyncio.to_thread(
            k8s_service.get_items, "secrets", namespace, own)), None)
        pod = None
        if anchor:
            pod = next(iter(await asyncio.to_thread(
                k8s_service.get_items, "pods", namespace, own)), None)
    except KubectlException as e:
        raise _kubectl_error("read", e)
    if not anchor:
        return RetrievalStatusResponse(status=RetrievalState.NOT_FOUND)
    session = dr.session_from_anchor(anchor)
    phase, ready = dr.pod_state(pod)
    if ready:
        state = RetrievalState.ACTIVE
    elif phase in (None, "Succeeded", "Failed"):
        state = RetrievalState.STOPPED
    else:
        state = RetrievalState.STARTING
    hostname = _hostname(session["subdomain"])
    return RetrievalStatusResponse(
        status=state,
        hostname=hostname,
        url=f"https://{hostname}/" if hostname else None,
        opened_at=_ts(session["opened_at"]),
        expires_at=_ts(session["expires_at"]),
        pod_phase=phase,
        ready=ready,
        pvc=session["pvc"] or None,
        profile=session["profile"] or None,
    )


@router.delete(
    "/api/releases/{namespace}/{name}/retrieval",
    response_model=RetrievalCloseResponse,
    summary="#1540: close the data retrieval entrance (idempotent)",
)
async def close_retrieval(
    namespace: str,
    name: str,
    subdomain: Optional[str] = Query(
        None,
        description="Hostname label the platform recorded at open time; used only when the "
                    "entrance objects are already gone (e.g. a previous close removed them but "
                    "not the route).",
    ),
):
    _validate_target(namespace, name)
    if subdomain and not dr.is_retrieval_subdomain(subdomain):
        raise _http_error(dr.RetrievalError(
            422, dr.INVALID_SUBDOMAIN, f"{subdomain!r} is not a data retrieval hostname."))
    try:
        return await _close(namespace, name, subdomain)
    except KubectlException as e:
        raise _kubectl_error("close", e)
    except dr.RetrievalError as e:
        raise _http_error(e)


@router.post(
    "/api/retrievals/sweep",
    response_model=RetrievalSweepResponse,
    summary="#1540: close every expired data retrieval entrance of this operator",
)
async def sweep_retrievals():
    """平台 cron 漏掉的（平台記錄被刪、cron 沒跑、關閉失敗）由這裡依 k8s 上的到期標註收掉。

    範圍：本 operator 的 namespace 前綴，且（有 PAAS_ENV 時）``paas.woowtech.io/env`` 相同——
    prod／stg 共用叢集，prod 的前綴 ``paas-ws-`` 也涵蓋 stg 的 ``paas-ws-stg-``。
    """
    try:
        anchors = await asyncio.to_thread(
            k8s_service.list_items_all_namespaces, "secrets",
            dr.anchors_selector(settings.paas_env))
    except KubectlException as e:
        raise _kubectl_error("list", e)
    now = int(time.time())
    closed: List[str] = []
    failed: List[str] = []
    for anchor in anchors:
        session = dr.session_from_anchor(anchor)
        namespace, release = session["namespace"], session["release"]
        try:
            validate_namespace(namespace)
        except ValueError:
            continue
        if not release or session["expires_at"] > now:
            continue
        key = f"{namespace}/{release}"
        try:
            await _close(namespace, release, None)
            closed.append(key)
        except (KubectlException, dr.RetrievalError) as e:
            logger.error("Sweep could not close data retrieval %s: %s", key,
                         getattr(e, "message", e))
            failed.append(key)
    if closed or failed:
        logger.info("Data retrieval sweep: closed=%s failed=%s", closed, failed)
    return RetrievalSweepResponse(checked=len(anchors), closed=closed, failed=failed)
