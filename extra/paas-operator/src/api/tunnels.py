"""API endpoints for dedicated Cloudflare Tunnel lifecycle management."""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from src.config import settings
from src.models.schemas import (
    TunnelConfigUpdateRequest,
    TunnelCreateRequest,
    TunnelCreateResponse,
    TunnelRoutesSyncRequest,
    TunnelRoutesSyncResponse,
    TunnelRouteSyncResult,
    TunnelStatusResponse,
    TunnelTokenResponse,
)
from src.services.cloudflare import CloudflareException, CloudflareService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tunnels", tags=["tunnels"])

cloudflare_service = CloudflareService()


# --------------------------------------------------------------------------- #
#  #531 tunnel 歸屬驗證（三個寫入端點共用）                                     #
# --------------------------------------------------------------------------- #
#
# 為什麼需要：這個 router 的認證只有**一把平台級 X-API-Key**（src/main.py），而那
# 把 key 是 Odoo 代所有租戶呼叫時共用的 ⇒ 「這條 tunnel 屬於誰」在此之前完全由
# 呼叫端自律，operator 端零把關。呼叫端只要帶對 key，就能對任意 tunnel_id 動手。
#
# ⚠ **這道檢查的誠實強度（不要把它寫成完整授權）**：
# 權威來源是 Cloudflare 上那筆 tunnel 的 name，而 tunnel name **不是機密** ——
# CF dashboard 看得到，也 stamp 在 DNS record 的 comment 上。它擋的是
# 「呼叫端拿著**自己那一列**的 name、卻塞了**別人的** tunnel_id」這個形狀
# （＝ #522 的攻擊形狀）；它**擋不住**同時掌握兩個值的攻擊者。這是縱深防禦，
# 不是授權模型 —— 真正的授權要 per-tenant 認證，本單明令不新增認證模型。
# 平台自己的 L4 reconciler 也已經在 src/services/reconciler.py 明寫「name 相符
# 不可作為 ownership 證據（手動建資源可照抄命名）」，兩處說法一致。
#
# 為什麼不是別的權威來源（三條都被否掉）：
#   * **ns prefix**：/routes 這個端點結構上沒有 namespace 輸入，CF 側也沒有
#     tunnel ↔ namespace 的連結；而且 docs/decisions/patterns/
#     external-resource-env-token-naming.md 守則 2 明文禁止在 operator 端從
#     namespace_prefix 隱式推導 env。
#   * **operator 自持對照表**：要引入狀態，而 operator 是無狀態服務。
#   * **per-tenant 認證**：工單明令不新增認證模型。
#
# 為什麼比對「等值」而不是「認前綴 / 解析格式」：CF tunnel 的命名在本 repo 有過
# 三代（`smarthome-` → `paas-sm-…` → `paas-{env}-sm-…`，SA 的 canonical_name 也
# 在後期才注 env token），舊列身上是舊格式。**只做等值比對**時格式漂移完全透明
# ——兩側比的都是「CF 自己那一份」；一旦改成認前綴，舊列會被判成「不是平台的」。

_OWNERSHIP_DENIED = status.HTTP_403_FORBIDDEN


def _ownership_error(code: str, tunnel_id: str, operation: str, **extra) -> dict:
    """結構化錯誤 body。

    ⚠ **刻意不回吐 CF 上的真實 name**：攻擊者手上本來就有 tunnel_id，缺的正是
    name。把實際 name 放進錯誤訊息等於把缺的那一半直接送給他，這道檢查會退化成
    一支「猜 id → 換得 name」的 oracle。
    """
    return {"error": code, "tunnel_id": tunnel_id, "operation": operation, **extra}


async def _assert_tunnel_ownership(
    tunnel_id: str, claimed_name: Optional[str], *, operation: str,
) -> None:
    """驗證呼叫端提出的 ``claimed_name`` 真的是這條 tunnel 在 CF 上的 name。

    Args:
        tunnel_id: path 上的目標 tunnel。
        claimed_name: 呼叫端提出的歸屬主張；``None`` / 空字串 = 沒帶。
        operation: 進錯誤 body 與 log 的動作名，讓兩者能對得起來。

    Raises:
        HTTPException: 403 拒絕（三種 error code，見下）或 502（查不動 CF）。

    四種結局，其中三種是「不得放行」：

    ``tunnel_ownership_mismatch``
        claim 有帶但對不上 ⇒ **一律拒，三個端點都立刻生效**、不受 rollout 旗標
        影響。這是本修法真正在擋的那一格。
    ``tunnel_ownership_claim_required``
        claim 沒帶。**兩段 rollout**：預設（stage 1）放行並留一行
        ``tunnel_claim_absent`` warning；``TUNNEL_OWNERSHIP_REQUIRE_CLAIM=true``
        （stage 2）才拒。理由見 src/config.py 該欄位的註解。
    ``tunnel_ownership_unverifiable``
        CF 回了空 name。**必須特判**：``get_tunnel_status`` 對缺欄位是
        ``result.get("name", "")``，若不特判，claim 也是空字串時 ``"" == ""``
        會**通過** —— 兩側一起壞成同一個垃圾值，比不相等**更容易**成立。
    ``tunnel_ownership_check_failed``
        CF 查詢本身失敗（非 404）⇒ 502。**「沒驗到」不等於「驗過了」**，不放行。

    唯一的放行例外是 **CF 回 404（tunnel 不存在）**：沒有別人的資源可傷，而且
    ``CloudflareService.delete_tunnel`` 對 404 是**當成功處理**的（teardown 重試
    靠這個冪等性）。若把 404 當拒絕，等於把「重試把殘留收乾淨」這條路封死 ——
    tunnel 已經不在了，呼叫端那一列卻永遠拆不掉。
    """
    claim = (claimed_name or "").strip()

    if not claim:
        if settings.tunnel_ownership_require_claim:
            logger.warning(
                "tunnel_ownership_claim_required: op=%s tunnel=%s — rejected "
                "(TUNNEL_OWNERSHIP_REQUIRE_CLAIM is on)", operation, tunnel_id,
            )
            raise HTTPException(
                status_code=_OWNERSHIP_DENIED,
                detail=_ownership_error(
                    "tunnel_ownership_claim_required", tunnel_id, operation),
            )
        # Stage 1：放行，但**不得無聲**——這行 warning 是判斷「可以翻旗了沒」的
        # 唯一訊號（marker 刻意不與任何 error code 共前綴，grep 分得開）。
        logger.warning(
            "tunnel_claim_absent: op=%s tunnel=%s — allowed under the stage-1 "
            "rollout default; set TUNNEL_OWNERSHIP_REQUIRE_CLAIM=true once every "
            "caller in this environment sends the claim", operation, tunnel_id,
        )
        return

    try:
        tunnel_status = await cloudflare_service.get_tunnel_status(tunnel_id)
    except CloudflareException as e:
        if e.status_code == 404:
            logger.info(
                "tunnel_absent_on_cloudflare: op=%s tunnel=%s — ownership check "
                "skipped (nothing to protect); letting the operation proceed so "
                "idempotent teardown still converges", operation, tunnel_id,
            )
            return
        logger.error(
            "tunnel_ownership_check_failed: op=%s tunnel=%s — %s",
            operation, tunnel_id, e.message,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_ownership_error(
                "tunnel_ownership_check_failed", tunnel_id, operation),
        )

    actual_name = (tunnel_status.get("name") or "").strip()
    if not actual_name:
        logger.error(
            "tunnel_ownership_unverifiable: op=%s tunnel=%s — Cloudflare returned "
            "no tunnel name; refusing rather than comparing against an empty value",
            operation, tunnel_id,
        )
        raise HTTPException(
            status_code=_OWNERSHIP_DENIED,
            detail=_ownership_error(
                "tunnel_ownership_unverifiable", tunnel_id, operation),
        )

    if actual_name != claim:
        # log 記得下 claim（呼叫端自己送的，記錄它不外洩任何東西），但**不記**
        # actual_name 以外的比對細節到回應裡。
        logger.warning(
            "tunnel_ownership_mismatch: op=%s tunnel=%s claimed=%r — the caller "
            "does not own this tunnel; refusing", operation, tunnel_id, claim,
        )
        raise HTTPException(
            status_code=_OWNERSHIP_DENIED,
            detail=_ownership_error(
                "tunnel_ownership_mismatch", tunnel_id, operation,
                claimed_name=claim),
        )


@router.post(
    "",
    response_model=TunnelCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a dedicated Cloudflare Tunnel",
)
async def create_tunnel(request: TunnelCreateRequest):
    """Create a new dedicated Cloudflare Tunnel with ingress config and DNS.

    This performs the full tunnel provisioning flow:
    1. Create the tunnel via Cloudflare API
    2. Retrieve the tunnel token
    3. Configure ingress rules (hostname -> service_url + catch-all 404)
    4. Create a DNS CNAME record pointing to the tunnel

    Args:
        request: Tunnel creation parameters.

    Returns:
        Tunnel ID, name, token, hostname, and DNS record ID.

    Raises:
        HTTPException: If tunnel creation fails.
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for tunnel management",
        )

    try:
        # Step 1: Create the tunnel
        tunnel_info = await cloudflare_service.create_tunnel(name=request.name)
        tunnel_id = tunnel_info["tunnel_id"]
        tunnel_name = tunnel_info["tunnel_name"]

        # Step 2: Get the tunnel token
        tunnel_token = await cloudflare_service.get_tunnel_token(tunnel_id)

        # Step 3 + 4: with a hostname -> single ingress rule + DNS record.
        # Without a hostname -> BARE tunnel: catch-all 404 only, no DNS. The
        # subdomain-extraction below dereferences request.hostname, so it MUST
        # stay inside the hostname branch (None.endswith would raise).
        dns_record_id = None
        dns_failed = False
        if request.hostname:
            await cloudflare_service.configure_tunnel(
                tunnel_id=tunnel_id,
                hostname=request.hostname,
                service_url=request.service_url,
            )

            if cloudflare_service.dns_enabled:
                # Extract subdomain from hostname
                domain = settings.cloudflare_domain
                if request.hostname.endswith(f".{domain}"):
                    subdomain = request.hostname[: -(len(domain) + 1)]
                else:
                    subdomain = request.hostname.split(".")[0]

                try:
                    dns_record_id = await cloudflare_service.create_dns_record_for_tunnel(
                        subdomain=subdomain,
                        tunnel_id=tunnel_id,
                    )
                except CloudflareException as e:
                    # DNS was ENABLED but creation failed: surface dns_failed=True
                    # so the caller (Odoo) can tell this apart from dns_record_id
                    # being None because DNS is intentionally disabled. Without the
                    # flag the caller would mark the SA active with no resolvable
                    # hostname and rely entirely on a later re-sync to self-heal.
                    dns_failed = True
                    logger.warning(
                        f"DNS record creation failed for {request.hostname}: {e.message}. "
                        "Tunnel is created but DNS must be configured manually."
                    )
        else:
            # Bare tunnel: empty rule list -> configure_tunnel_ingress appends
            # the catch-all 404 automatically. No DNS until the caller adds a
            # route via the declarative /routes sync.
            await cloudflare_service.configure_tunnel_ingress(tunnel_id, rules=[])

        logger.info(
            f"Tunnel provisioned: {tunnel_name} (ID: {tunnel_id}, "
            f"hostname: {request.hostname or '<bare>'})"
        )

        return TunnelCreateResponse(
            tunnel_id=tunnel_id,
            tunnel_name=tunnel_name,
            tunnel_token=tunnel_token,
            hostname=request.hostname,
            dns_record_id=dns_record_id,
            dns_failed=dns_failed,
        )

    except CloudflareException as e:
        logger.error(f"Failed to create tunnel: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create tunnel: {e.message}",
        )


@router.get(
    "/{tunnel_id}",
    response_model=TunnelStatusResponse,
    summary="Get tunnel status",
)
async def get_tunnel_status(tunnel_id: str):
    """Get the status and connection information for a Cloudflare Tunnel.

    Args:
        tunnel_id: The Cloudflare Tunnel ID.

    Returns:
        Tunnel status, name, connections, and creation timestamp.

    Raises:
        HTTPException: If status retrieval fails.
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for tunnel management",
        )

    try:
        tunnel_status = await cloudflare_service.get_tunnel_status(tunnel_id)

        return TunnelStatusResponse(
            tunnel_id=tunnel_status["tunnel_id"],
            name=tunnel_status["name"],
            status=tunnel_status["status"],
            connections=tunnel_status["connections"],
            created_at=tunnel_status.get("created_at"),
        )

    except CloudflareException as e:
        if e.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tunnel {tunnel_id} not found",
            )
        logger.error(f"Failed to get tunnel status: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get tunnel status: {e.message}",
        )


@router.get(
    "/{tunnel_id}/token",
    response_model=TunnelTokenResponse,
    summary="Get tunnel token",
)
async def get_tunnel_token(tunnel_id: str):
    """Get the token for a Cloudflare Tunnel.

    The token is used by cloudflared to authenticate and connect to the tunnel.

    Args:
        tunnel_id: The Cloudflare Tunnel ID.

    Returns:
        Tunnel ID and token.

    Raises:
        HTTPException: If token retrieval fails.
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for tunnel management",
        )

    try:
        token = await cloudflare_service.get_tunnel_token(tunnel_id)

        return TunnelTokenResponse(
            tunnel_id=tunnel_id,
            token=token,
        )

    except CloudflareException as e:
        logger.error(f"Failed to get tunnel token: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get tunnel token: {e.message}",
        )


@router.put(
    "/{tunnel_id}/config",
    summary="Update tunnel ingress configuration",
)
async def update_tunnel_config(
    tunnel_id: str, request: TunnelConfigUpdateRequest
):
    """Update a tunnel's ingress rules.

    Used for subscription suspend (service_url="http_status:503")
    and resume (service_url="http://localhost:PORT").

    #769: ``hostname`` / ``service_url`` arrive in the **request body**. They
    used to be bare ``str`` parameters, which FastAPI binds as required query
    params — and the only caller (Odoo ``PaaSOperatorClient.configure_tunnel``)
    sends a JSON body, so every call was a 422 that the caller swallowed into
    ``state='error'``. Deliberately **not** dual-accepting query params: stg
    access logs over 14 days show no third party using that form, and a second
    accepted shape doubles the contract surface for nothing.

    Args:
        tunnel_id: The Cloudflare Tunnel ID.
        request: Desired ingress config (hostname + target service URL).

    Returns:
        Configuration confirmation message.

    Raises:
        HTTPException: If configuration update fails.
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required",
        )

    await _assert_tunnel_ownership(
        tunnel_id, request.tunnel_name, operation="update_config")

    try:
        await cloudflare_service.configure_tunnel(
            tunnel_id=tunnel_id,
            hostname=request.hostname,
            service_url=request.service_url,
        )
        logger.info(
            f"Updated tunnel {tunnel_id} config: "
            f"{request.hostname} -> {request.service_url}"
        )
        return {
            "message": f"Tunnel {tunnel_id} configuration updated",
            "hostname": request.hostname,
            "service_url": request.service_url,
        }

    except CloudflareException as e:
        logger.error(f"Failed to update tunnel config: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update tunnel configuration: {e.message}",
        )


@router.delete(
    "/{tunnel_id}",
    summary="Delete a tunnel",
)
async def delete_tunnel(
    tunnel_id: str,
    tunnel_name: Optional[str] = Query(
        default=None,
        description=(
            "Ownership claim (#531): the caller's own record of this tunnel's "
            "Cloudflare name. Verified against the live tunnel before deleting."
        ),
    ),
):
    """Delete a Cloudflare Tunnel and clean up associated resources.

    This will:
    1. Clean up DNS records pointing to the tunnel
    2. Delete the tunnel with cascade (removes connections)

    #531 的 ownership claim 在這條端點走 **query param** 而不是 request body，
    這條與姊妹端點（``/routes`` / ``/config`` 走 body）刻意不一致，理由是相容性：

    * DELETE **今天沒有 body**，而帶 body 的 DELETE 雖然合法，卻是最容易被中間層
      （proxy / ingress / WAF）默默丟掉的形狀。被丟掉的後果不是報錯，是
      **claim 靜默退化成「沒帶」** —— 一旦翻到 stage 2 就變成「teardown 全數被拒、
      tunnel 拆不掉還在計費」，而現場看到的訊息會指向錯的地方。query param 不會
      被丟。
    * 唯一的代價是 claim 會進 access log，而 **tunnel name 本來就不是機密**
      （CF dashboard 可見、也 stamp 在 DNS record comment 上），所以這個代價可接受。
    * 舊版平台不帶這個 param 也無痛：FastAPI 對**未宣告**的 query param 是忽略，
      而這裡宣告成 ``Optional`` ⇒ 新舊 client 都不會 422。

    Args:
        tunnel_id: The Cloudflare Tunnel ID.
        tunnel_name: Ownership claim; see ``_assert_tunnel_ownership``.

    Returns:
        Deletion confirmation message.

    Raises:
        HTTPException: If tunnel deletion fails.
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for tunnel management",
        )

    await _assert_tunnel_ownership(
        tunnel_id, tunnel_name, operation="delete_tunnel")

    try:
        await cloudflare_service.delete_tunnel(tunnel_id)

        logger.info(f"Tunnel {tunnel_id} deleted successfully")
        return {"message": f"Tunnel {tunnel_id} deleted successfully"}

    except CloudflareException as e:
        logger.error(f"Failed to delete tunnel: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete tunnel: {e.message}",
        )


@router.put(
    "/{tunnel_id}/routes",
    response_model=TunnelRoutesSyncResponse,
    summary="Declaratively sync a dedicated tunnel's routes (ingress + DNS)",
)
async def sync_tunnel_routes(tunnel_id: str, request: TunnelRoutesSyncRequest):
    """Replace the tunnel's ingress rules and reconcile DNS records.

    Declarative full sync: the request body is the complete desired route
    list. Ingress is replaced wholesale (plus catch-all 404); DNS records
    pointing at this tunnel are created/kept/deleted to match. Idempotent —
    used by Security Access for route CRUD, suspend (service_url=
    http_status:503) and resume.

    NOT atomic across the two stages. ``configure_tunnel_ingress`` runs first,
    then ``reconcile_tunnel_dns`` (create-loop then delete-loop, each raising
    on the first failure). There is no compensating action: if ingress
    succeeds and reconcile then fails partway, the ingress and some DNS
    records are already mutated while this call returns 500. Convergence is by
    design left to the caller's next idempotent re-sync (the desired state is
    re-asserted in full), since the Cloudflare API offers no transaction to
    roll the partial mutation back. Callers (Odoo SecurityAccess) keep the
    driving DB row so a re-sync can always reach the records — a 500 here means
    "CF may be partially changed; re-sync to converge", not "nothing changed".
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for tunnel management",
        )

    # #531：擋在 configure_tunnel_ingress 之前。這條端點是**整份 ingress 覆寫 ＋
    # DNS 對帳**，一次呼叫就能把某條 tunnel 的整份路由表換掉，所以歸屬檢查必須在
    # 任何 CF 寫入之前完成 —— 這兩個動作之間本來就沒有交易可以回捲（見下方
    # docstring 的 NOT atomic 段）。
    await _assert_tunnel_ownership(
        tunnel_id, request.tunnel_name, operation="sync_routes")

    domain = cloudflare_service.domain
    rules = [
        {
            "hostname": f"{r.subdomain}.{domain}",
            "service": r.service_url,
            "http_host_header": r.http_host_header,
        }
        for r in request.routes
    ]

    try:
        await cloudflare_service.configure_tunnel_ingress(
            tunnel_id=tunnel_id, rules=rules,
        )
        dns_map = await cloudflare_service.reconcile_tunnel_dns(
            tunnel_id=tunnel_id,
            subdomains=[r.subdomain for r in request.routes],
            comment=request.comment,
        )
        logger.info(
            f"Tunnel {tunnel_id} routes synced: {len(request.routes)} route(s)"
        )
        return TunnelRoutesSyncResponse(
            tunnel_id=tunnel_id,
            routes=[
                TunnelRouteSyncResult(
                    subdomain=r.subdomain,
                    hostname=f"{r.subdomain}.{domain}",
                    dns_record_id=dns_map.get(r.subdomain),
                )
                for r in request.routes
            ],
        )
    except CloudflareException as e:
        logger.error(f"Failed to sync tunnel routes: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync tunnel routes: {e.message}",
        )
