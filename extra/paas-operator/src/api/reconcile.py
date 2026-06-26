"""L4 reconciler — 唯讀帳號級反向 inventory 端點（Phase A / TS-3a，方向 A）。

operator 在此**只做唯讀發現**：列帳號級 CF 資源（tunnels / access apps / reusable policies）
回 Odoo 當右集 ground truth。差集 / 分類（env_hint / reconcilable / reason）/ 任何回收動作
一律不在 operator —— 全在 Odoo `src/services/reconciler.py`，且首版零自動回收（dry-run-only、
刪除權留人手）。

本檔在 reconcile delete-gate 監看清單內（tests/test_reconcile_delete_gate.py）：
此路徑禁止任何破壞動詞（delete / uninstall / unlink / scale / subprocess ...），
CI fail-closed 守住「唯讀」不變式，非人工自律。

reconcile 路徑對 PAAS_ENV **fail-CLOSED**（與 operator 既有 ns-label 的 fail-OPEN 相反）：
env 未鎖定（不在 prod|stg）→ 503，不沿用 fail-open 預設，避免「漏設」偽裝成「成功讀 prod」
而跨環境誤判孤兒（R3 env-pin）。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from src.config import settings
from src.models.schemas import CfResource, InventoryResponse, OrphanKind
from src.services.cloudflare import CloudflareService

logger = logging.getLogger(__name__)

cloudflare_service = CloudflareService()

router = APIRouter(prefix="/api/reconcile", tags=["reconcile"])

_PINNED_ENVS = {"prod", "stg"}


def require_pinned_env() -> str:
    """R3 env-pin fail-CLOSED：要求 PAAS_ENV 為顯式 prod|stg，否則 503（不回 inventory）。

    與 operator 既有 ns-label 的 fail-OPEN（unset → 照常啟動）方向相反：reconcile 是
    跨環境誤判風險路徑，env 判不出時寧可整個端點不可用，也不沿用任何 fallback 預設。
    """
    env = settings.paas_env
    if env not in _PINNED_ENVS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "reconcile fail-closed: PAAS_ENV 未鎖定為 prod|stg "
                f"(目前 {env!r})；拒絕在無法判定環境時提供反向 inventory（避免跨環境誤判）。"
            ),
        )
    return env


# (kind, 取得右集的 method 名) — 帳號級唯讀 list-all，逐類獨立 try（completeness gate）。
_INVENTORY_SOURCES = (
    (OrphanKind.TUNNEL, "list_all_tunnels"),
    (OrphanKind.ACCESS_APP, "list_all_access_apps"),
    (OrphanKind.ACCESS_POLICY, "list_all_reusable_policies"),
)


@router.get(
    "/inventory",
    response_model=InventoryResponse,
    summary="帳號級 CF 資源反向 inventory（唯讀、右集 ground truth）",
)
async def get_inventory(env: str = Depends(require_pinned_env)) -> InventoryResponse:
    """列帳號級 CF 資源回 Odoo。逐類獨立撈取：任一類失敗 → partial=True + 記 failed_kinds，
    該類資源缺漏不混入（completeness gate，B.2）。絕不靜默回空清單當「無孤兒」。"""
    resources: list[CfResource] = []
    failed_kinds: list[str] = []

    for kind, method_name in _INVENTORY_SOURCES:
        fetch = getattr(cloudflare_service, method_name)
        try:
            items = await fetch()
            # item 建構放進**同一個 try**：缺 id 的畸形 payload（KeyError / ValidationError）
            # 與 list-all 失敗同等對待 —— 該類無法可靠物化即整類標 partial，半套不外洩。
            # 先建 local list，整類成功才 extend（避免邊建邊 append 全域 → 壞 item 前的半套殘留）。
            kind_resources = [
                CfResource(id=item["id"], name=item.get("name", ""), kind=kind)
                for item in items
            ]
        except Exception as exc:  # noqa: BLE001 — 蓄意 catch-all，理由見下
            # completeness gate（R4/B.2）：對「某類無法完整且可靠取得」一律降級成 partial，
            # 含 CloudflareException（CF API 錯）、httpx 傳輸層錯（timeout / connection reset）、
            # 畸形 payload。這是 fail-SAFE 且**可觀測**（warning 記下 exc + failed_kinds），
            # 永不讓單類失敗坍成整個 500（會丟掉已撈到的其他類、且無 partial 訊號 → 下游無法
            # 區分『沒掃到』與『無孤兒』）。下游 Odoo reconciler 視 partial=True 為「不得動作」。
            # **不可**收窄成特定例外型別 —— 收窄會重新打開 timeout / 畸形 payload 繞過 gate 的洞。
            logger.warning(
                "reconcile inventory: %s list-all/物化 失敗（%s: %s）→ 標 partial、該類缺漏",
                kind.value,
                type(exc).__name__,
                exc,
            )
            failed_kinds.append(kind.value)
            continue
        resources.extend(kind_resources)

    return InventoryResponse(
        paas_env=env,
        partial=bool(failed_kinds),
        failed_kinds=failed_kinds,
        resources=resources,
    )
