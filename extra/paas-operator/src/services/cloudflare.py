"""Cloudflare Tunnel API client for managing ingress routes and tunnel lifecycle."""
import base64
import logging
import secrets
from typing import Any, Dict, List, NamedTuple, Optional

import httpx
from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)


class TunnelRoute(BaseModel):
    """Represents a Cloudflare Tunnel ingress route."""

    hostname: Optional[str] = None
    service: str
    path: Optional[str] = None


class CloudflareException(Exception):
    """Exception raised for Cloudflare API errors."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class DnsRecordOwnershipError(CloudflareException):
    """我方（operator）拒絕動一筆不屬於本次操作的 DNS 記錄（#824）。

    與父類的差別是**誰說不行**：``CloudflareException`` 代表 Cloudflare 回錯，本類
    代表 operator 在送出任何寫入之前**自己**擋下來，呼叫端因此分得出兩者。
    ``status_code`` 固定 409 Conflict —— 目標記錄存在、但它歸屬於別人；不是 400
    （請求格式錯）也不是 403（權杖權限不足）。

    刻意繼承 ``CloudflareException``：既有呼叫端的 ``except CloudflareException`` 會
    直接接住它（``api/tunnels.py`` provision → ``dns_failed=True``、
    ``sync_tunnel_routes`` → HTTPException 帶 ``e.status_code``、``create_route`` →
    ``RouteOutcome.dns_failed``），所以本類不會變成未處理例外。
    """

    def __init__(self, message: str):
        super().__init__(message, status_code=409)


#: 專用 tunnel 的 CNAME 目標後綴。抽成常數是為了讓「精確後綴比對」只有一份定義。
CFARGOTUNNEL_SUFFIX = ".cfargotunnel.com"


def _normalize_dns_content(content: Optional[str]) -> str:
    """把 CNAME content 正規化成可比較的形式。

    依 DNS 語意做**等價**正規化（大小寫不敏感、尾點不影響 FQDN 身分），不是放寬比對：
    ``ABC.cfargotunnel.com``、``abc.cfargotunnel.com.`` 與 ``abc.cfargotunnel.com``
    指的是同一個名字，也就是同一條 tunnel。少了這一步，CF 哪天回一個大小寫不同的
    content 就會讓合法操作被判成搶佔。
    """
    return (content or "").strip().rstrip(".").lower()


def dns_record_points_at(record: Optional[dict], cname_target: str) -> bool:
    """本地歸屬述詞：``record`` 的 content 是不是就是 ``cname_target``（#824）。

    DNS 記錄的 ``content`` 字面寫著 ``{tunnel_id}.cfargotunnel.com`` —— 歸屬證據就在
    記錄本身，所以這個述詞是**純本地、零額外 API 呼叫、不依賴任何外部契約**的。

    刻意**不**用 comment stamp 當判準，兩個獨立理由：
    1. ``models/schemas.py`` 已把 comment 定義成「在本 repo 是只寫不讀的自由文字」，
       把它變成授權判準等於讓改 stamp 格式順帶改掉安全姿態（#531 已裁過同一題，
       才另開 ``tunnel_name`` 欄位）。
    2. 就算不管 1，它在缺口發生的那條路徑上 fail-open：``api/tunnels.py`` 的 provision
       呼叫 ``create_dns_record_for_tunnel`` 時**沒有傳 comment**，只有 reconcile 那條
       會傳，而 create 時的搶佔正好發生在沒有 stamp 的那一條。

    空 record / 空 content / 空 target 一律回 ``False``（fail-closed：無法證明歸屬，
    就當作不歸屬）。
    """
    if not record:
        return False
    target = _normalize_dns_content(cname_target)
    if not target:
        return False
    return _normalize_dns_content(record.get("content")) == target


def tunnel_id_from_cname_target(content: Optional[str]) -> Optional[str]:
    """從 CNAME content 抽出 tunnel id；不是 ``*.cfargotunnel.com`` 就回 ``None``。

    **必須是精確後綴比對，不可以用 substring / ``in``**：``abc.cfargotunnel.com.evil.com``
    含有那串字面卻不是 tunnel 目標；用 ``in`` 判會把它當成一條 tunnel，接著去查「這條
    tunnel 還在不在」、查不到就當孤兒接收 —— 正好是本單要擋的搶佔。

    多一層 label（``a.b.cfargotunnel.com``）同樣回 ``None``：那不是任何一條 tunnel 的
    目標，**沒有 tunnel 可查** ⇒ 由呼叫端 fail-closed 處理（拒絕），不得當成「查不到＝
    不存在＝可以接收」。
    """
    normalized = _normalize_dns_content(content)
    if not normalized.endswith(CFARGOTUNNEL_SUFFIX):
        return None
    tunnel_id = normalized[: -len(CFARGOTUNNEL_SUFFIX)]
    if not tunnel_id or "." in tunnel_id:
        return None
    return tunnel_id


class RouteOutcome(NamedTuple):
    """``create_route`` 的結果——比原本的 ``bool`` 多一格（#648 (a)）。

    ``configured`` 是「tunnel ingress rule 有沒有設好」，``dns_failed`` 是「DNS
    record 有沒有嘗試過而且失敗」。兩者**必須分開**：DNS 那半失敗時 ingress 仍然
    會建起來（見 ``create_route`` Step 1 的註解），舊的單一 bool 沒有地方講這件事，
    於是「hostname 永遠不會解析」這個事實在呼叫端一次都沒被看見。

    ``dns_failed=False`` 同時涵蓋「DNS 建成了」與「這個 deployment 根本沒開 DNS
    管理」兩種情形——兩者對呼叫端的處置相同（不必 fail closed），要區分「有沒有
    對外網址」請看 ``configured``。
    """

    configured: bool
    dns_failed: bool

class CloudflareService:
    """Service for managing Cloudflare Tunnel routes and DNS records via API."""

    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self):
        """Initialize the Cloudflare service."""
        self.account_id = settings.cloudflare_account_id
        self.tunnel_id = settings.cloudflare_tunnel_id
        self.zone_id = settings.cloudflare_zone_id
        self.api_token = settings.cloudflare_api_token
        self.domain = settings.cloudflare_domain
        self.enabled = settings.cloudflare_enabled

        if self.enabled and not all(
            [self.account_id, self.tunnel_id, self.api_token, self.domain]
        ):
            logger.warning(
                "Cloudflare integration enabled but missing configuration. "
                "Required: CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_TUNNEL_ID, "
                "CLOUDFLARE_API_TOKEN, CLOUDFLARE_DOMAIN"
            )
            self.enabled = False

        # DNS management requires zone_id
        self.dns_enabled = self.enabled and bool(self.zone_id)
        if self.enabled and not self.zone_id:
            logger.warning(
                "Cloudflare DNS management disabled: CLOUDFLARE_ZONE_ID not set. "
                "Routes will be created but DNS records must be managed manually."
            )

    @property
    def _headers(self) -> dict:
        """Get headers for Cloudflare API requests."""
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    @property
    def _tunnel_config_url(self) -> str:
        """Get the tunnel configuration API URL."""
        return f"{self.BASE_URL}/accounts/{self.account_id}/cfd_tunnel/{self.tunnel_id}/configurations"

    @property
    def _dns_records_url(self) -> str:
        """Get the DNS records API URL."""
        return f"{self.BASE_URL}/zones/{self.zone_id}/dns_records"

    @property
    def _tunnel_cname_target(self) -> str:
        """Get the CNAME target for the tunnel."""
        return f"{self.tunnel_id}.cfargotunnel.com"

    def _tunnels_url(self, tunnel_id: Optional[str] = None) -> str:
        """Get the tunnels API URL for a specific or all tunnels.

        Args:
            tunnel_id: Optional tunnel ID for a specific tunnel.

        Returns:
            Cloudflare API URL for tunnel operations.
        """
        base = f"{self.BASE_URL}/accounts/{self.account_id}/cfd_tunnel"
        if tunnel_id:
            return f"{base}/{tunnel_id}"
        return base

    def _tunnel_cname_target_for(self, tunnel_id: str) -> str:
        """Get the CNAME target for a specific tunnel.

        Args:
            tunnel_id: The tunnel ID.

        Returns:
            CNAME target string for the tunnel.
        """
        return f"{tunnel_id}.cfargotunnel.com"

    # ==================== DNS Record Management ====================

    async def get_dns_record(
        self, hostname: str, raise_on_error: bool = False
    ) -> Optional[dict]:
        """Get a DNS record by hostname.

        Args:
            hostname: Full hostname (e.g., 'myapp.domain.com')
            raise_on_error: when True, a non-200 Cloudflare response (401/403/
                429/5xx) raises CloudflareException instead of collapsing to
                None. Callers that interpret None as "no record exists" (e.g.
                the availability check) MUST set this — otherwise an API error
                is silently reported as "subdomain free" (fail-open). Internal
                create/delete callers keep the legacy None behaviour.

        Returns:
            DNS record dict if found, None if confirmed absent (or, when
            raise_on_error is False, on API error).

        Raises:
            CloudflareException: only when raise_on_error and the API returns
                a non-200 / unsuccessful response.
        """
        if not self.dns_enabled:
            return None

        async with httpx.AsyncClient() as client:
            response = await client.get(
                self._dns_records_url,
                headers=self._headers,
                params={"name": hostname, "type": "CNAME"},
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error(f"Failed to get DNS record: {response.status_code}")
                logger.debug(f"Response details: {response.text}")
                if raise_on_error:
                    raise CloudflareException(
                        f"Cloudflare DNS lookup failed for {hostname}: "
                        f"{response.text}",
                        status_code=response.status_code,
                    )
                return None

            data = response.json()
            if not data.get("success"):
                if raise_on_error:
                    raise CloudflareException(
                        f"Cloudflare API error looking up {hostname}: "
                        f"{data.get('errors', 'Unknown error')}"
                    )
                return None
            if data.get("result"):
                return data["result"][0] if data["result"] else None

            return None

    async def _assert_dns_repoint_allowed(
        self, hostname: str, existing: dict, cname_target: str
    ) -> None:
        """既有記錄 content 與本次 target 不符時，決定「拒絕」還是「允許自癒接收」（#824）。

        只有一種情況准接收：那筆記錄指向的 tunnel **證實已不存在**（孤兒殘留）。
        ``delete_tunnel`` 的 DNS 清理是把失敗吞成 warning 繼續走的，所以殘留是真的會
        發生的良性情境；沒有這條自癒，那個 hostname 會永遠卡住無法重建。

        ⚠ **存活判定只能走 ``list_all_tunnels``，不可用 ``get_tunnel_status``。**
        理由在 #833 修完之後**變了，但結論沒變**，兩段都要讀：

        * 修法前的理由（已失效）：``get_tunnel_status`` 整支函式內 ``deleted_at``
          出現 0 次 ⇒ 會把軟刪的 tunnel 判成活的。**這一點 #833 已經修掉**，那支
          現在會如實轉發 ``deleted_at``。
        * 現在仍然成立的理由（這才是禁令的依據）：
          1. **fail-closed 的方向不同。** ``list_all_tunnels`` 走 ``_get_paginated``，
             錯誤一律 raise、**絕不回空清單**——回空清單會讓這裡把**每一筆**都判成
             孤兒，等於把守衛整個關掉。單筆 GET 的失敗模式則是「404 ⇒ 不存在」，
             而「查不到」與「不存在」在這個守衛裡不可以劃等號。
          2. **這裡要的是集合成員判定，不是單筆查詢。** 迴圈裡逐筆打單筆 GET 是
             N+1，且每一筆都各自帶上面那個失敗模式。

        ⇒ 別因為「``get_tunnel_status`` 現在看得到軟刪了」就把這裡改掉。守線＝
        ``tests/test_dns_record_ownership_824.py`` 的
        ``test_soft_deleted_tunnel_counts_as_gone``（它鎖的是**呼叫形狀**）。

        ⚠ 涵蓋範圍的誠實邊界：``list_all_tunnels`` 只看得到**本 account** 的 tunnel，
        所以指向他人 account 的 ``*.cfargotunnel.com`` 會被判成孤兒。本平台的 zone 與
        operator token 綁在同一個 account，這種目標不會出現 —— 但這點未實測。

        ⛔ 刻意**不做**「呼叫端顯式宣告我要搶」的 opt-in 參數：一個沒有任何呼叫端的
        allow-takeover 參數會留在簽名裡等著某次「先能動再說」把它打開，而被寫成例外
        的那一格就是會出事的那一格。本檢查不接受呼叫端的宣告，只接受可查證的事實。

        Raises:
            DnsRecordOwnershipError: 記錄屬於一條還活著的 tunnel，或無從證明它是孤兒。
            CloudflareException: tunnel 清單查詢失敗（fail-closed，不得當成孤兒接收）。
        """
        content = existing.get("content")
        record_id = existing.get("id")
        other_tunnel_id = tunnel_id_from_cname_target(content)

        if other_tunnel_id is None:
            # content 根本不是 *.cfargotunnel.com（有人把這個 hostname 指到別的地方）
            # ⇒ 沒有 tunnel 可查 ⇒ 一律拒。⛔ 這一格不准當成「查不到＝不存在＝可接收」。
            raise DnsRecordOwnershipError(
                f"Refusing to repoint DNS record {hostname} (id={record_id}): its "
                f"content {content!r} is not a Cloudflare Tunnel target, so there is "
                f"no tunnel to check and no way to prove the record is an orphan. "
                f"Not taking it over (#824)."
            )

        try:
            tunnels = await self.list_all_tunnels()
        except CloudflareException:
            logger.error(
                f"Refusing to repoint DNS record {hostname} (id={record_id}, "
                f"content={content!r}): could not list tunnels to check whether "
                f"{other_tunnel_id} still exists. Failing closed (#824)."
            )
            raise

        live_tunnel_ids = {
            _normalize_dns_content(t.get("id")) for t in tunnels if t.get("id")
        }
        if other_tunnel_id in live_tunnel_ids:
            raise DnsRecordOwnershipError(
                f"Refusing to repoint DNS record {hostname} (id={record_id}) from "
                f"{content!r} to {cname_target!r}: it points at tunnel "
                f"{other_tunnel_id}, which still exists. Repointing would silently "
                f"redirect that hostname's traffic away from its owner (#824)."
            )

        logger.warning(
            f"Adopting DNS record {hostname} (id={record_id}): it points at tunnel "
            f"{other_tunnel_id}, which no longer exists in this account — treating "
            f"it as residue left behind by a deleted tunnel and repointing it at "
            f"{cname_target}."
        )

    async def create_dns_record(self, subdomain: str) -> bool:
        """Create a CNAME DNS record pointing to the tunnel.

        Args:
            subdomain: Subdomain to create (e.g., 'myapp' for myapp.domain.com)

        Returns:
            True if record was created or already exists
        """
        if not self.dns_enabled:
            logger.info(f"DNS management disabled, skipping DNS record for {subdomain}")
            return True

        hostname = f"{subdomain}.{self.domain}"

        # Check if record already exists
        existing = await self.get_dns_record(hostname)
        if existing:
            if dns_record_points_at(existing, self._tunnel_cname_target):
                logger.info(f"DNS record for {hostname} already exists")
                return True
            # #824 site 3：content 指向別的地方。這是 create_dns_record_for_tunnel 那條
            # repoint 分支在 legacy 共用 tunnel 路徑上的雙胞胎 —— 在此之前同樣是「不同
            # 就直接改」。只有證實是孤兒才准接收，其餘 raise。
            await self._assert_dns_repoint_allowed(
                hostname, existing, self._tunnel_cname_target
            )
            return await self._update_dns_record(existing["id"], hostname)

        logger.info(f"Creating DNS record: {hostname} -> {self._tunnel_cname_target}")

        payload = {
            "type": "CNAME",
            "name": subdomain,
            "content": self._tunnel_cname_target,
            "ttl": 1,  # Auto TTL
            "proxied": True,  # Enable Cloudflare proxy (orange cloud)
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._dns_records_url,
                headers=self._headers,
                json=payload,
                timeout=30.0,
            )

            if response.status_code not in (200, 201):
                logger.error(f"Failed to create DNS record: {response.status_code}")
                logger.debug(f"Response details: {response.text}")
                raise CloudflareException(
                    f"Failed to create DNS record: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            logger.info(f"DNS record created: {hostname}")
            return True

    async def create_dns_record_for_tunnel(
        self, subdomain: str, tunnel_id: str, comment: Optional[str] = None
    ) -> Optional[str]:
        """Create a CNAME DNS record pointing to a specific tunnel.

        Args:
            subdomain: Subdomain to create (e.g., 'myapp' for myapp.domain.com)
            tunnel_id: The tunnel ID to point the CNAME at.
            comment: Optional canonical name stamped on the DNS record for
                traceability (e.g., 'paas-sa-{workspace}-{name}').

        Returns:
            DNS record ID if created, None if DNS is disabled.

        Raises:
            CloudflareException: If DNS record creation fails.
        """
        if not self.dns_enabled:
            logger.info(
                f"DNS management disabled, skipping DNS record for {subdomain}"
            )
            return None

        hostname = f"{subdomain}.{self.domain}"
        cname_target = self._tunnel_cname_target_for(tunnel_id)

        # Check if record already exists
        existing = await self.get_dns_record(hostname)
        if existing:
            if dns_record_points_at(existing, cname_target):
                logger.info(f"DNS record for {hostname} already exists")
                return existing["id"]

            # #824 site 1：content 指向**別條** tunnel。在此之前這裡直接
            # _update_dns_record 把記錄搶過來，再把 record id 當成「自己的」回傳給
            # 呼叫端 —— 全程 HTTP 200，受害的那一側完全沒有訊號（它的 DB 裡仍存著
            # 那個 record id，而該記錄已經不指向它了）。
            #
            # 只有「那條 tunnel 證實已不存在」才准接收（見 _assert_dns_repoint_allowed）。
            await self._assert_dns_repoint_allowed(hostname, existing, cname_target)

            # 自癒接收。**必須明確帶 cname_target**：讓 _update_dns_record 掉回共用
            # 預設 target 會把記錄「修」成指向共用 tunnel，那是沒有任何錯誤會浮現的
            # routing 黑洞（record_id 照樣回傳、照樣被當成正確的存起來）。
            logger.info(
                f"Repointing orphaned DNS record {hostname} from "
                f"{existing.get('content')} -> {cname_target}"
            )
            await self._update_dns_record(
                existing["id"], hostname, content=cname_target
            )
            return existing["id"]

        logger.info(f"Creating DNS record: {hostname} -> {cname_target}")

        payload = {
            "type": "CNAME",
            "name": subdomain,
            "content": cname_target,
            "ttl": 1,
            "proxied": True,
        }
        if comment:
            payload["comment"] = comment

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._dns_records_url,
                headers=self._headers,
                json=payload,
                timeout=30.0,
            )

            if response.status_code not in (200, 201):
                logger.error(f"Failed to create DNS record: {response.status_code}")
                logger.debug(f"Response details: {response.text}")
                raise CloudflareException(
                    f"Failed to create DNS record: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            record_id = data.get("result", {}).get("id")
            logger.info(f"DNS record created: {hostname} (ID: {record_id})")
            return record_id

    async def _update_dns_record(
        self, record_id: str, hostname: str, content: Optional[str] = None
    ) -> bool:
        """Update an existing DNS record.

        Args:
            record_id: DNS record ID
            hostname: Full hostname
            content: CNAME target to point at. Defaults to the shared tunnel
                target (``_tunnel_cname_target``) for the legacy single-tunnel
                callers (``create_dns_record``). Per-tunnel callers
                (``create_dns_record_for_tunnel``) MUST pass the dedicated
                tunnel's target so a repoint lands on the correct tunnel rather
                than silently on the shared one.

        Returns:
            True if update was successful
        """
        target = content or self._tunnel_cname_target
        logger.info(f"Updating DNS record: {hostname} -> {target}")

        payload = {
            "type": "CNAME",
            "name": hostname,
            "content": target,
            "ttl": 1,
            "proxied": True,
        }

        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{self._dns_records_url}/{record_id}",
                headers=self._headers,
                json=payload,
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error(f"Failed to update DNS record: {response.status_code}")
                logger.debug(f"Response details: {response.text}")
                return False

            logger.info(f"DNS record updated: {hostname}")
            return True

    async def delete_dns_record(self, subdomain: str) -> bool:
        """Delete a DNS record.

        Args:
            subdomain: Subdomain to delete (e.g., 'myapp' for myapp.domain.com)

        Returns:
            True if record was deleted or didn't exist
        """
        if not self.dns_enabled:
            logger.info(f"DNS management disabled, skipping DNS record deletion for {subdomain}")
            return True

        hostname = f"{subdomain}.{self.domain}"

        # Find the record
        existing = await self.get_dns_record(hostname)
        if not existing:
            logger.info(f"DNS record for {hostname} not found, nothing to delete")
            return True

        record_id = existing["id"]

        # #824 site 4：這一處只用 hostname 找、找到就刪 —— content 一次都沒看，連
        # reconcile 那邊「CF 的 content= 收窄」這個外包擔保都沒有。回 False 而不是
        # raise：呼叫端 delete_route 的前一步（移除 ingress）本來就該完成，把整條刪除
        # 流程炸掉沒有好處；留下的孤兒記錄可以從這行 ERROR 找回來，而刪掉別人的記錄
        # 是不可逆的。
        if not dns_record_points_at(existing, self._tunnel_cname_target):
            logger.error(
                f"Refusing to delete DNS record {hostname} (id={record_id}): its "
                f"content {existing.get('content')!r} does not point at this "
                f"deployment's tunnel target {self._tunnel_cname_target!r} — the "
                f"record belongs to something else. Skipping (#824)."
            )
            return False

        logger.info(f"Deleting DNS record: {hostname} (ID: {record_id})")

        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{self._dns_records_url}/{record_id}",
                headers=self._headers,
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error(f"Failed to delete DNS record: {response.status_code}")
                logger.debug(f"Response details: {response.text}")
                return False

            logger.info(f"DNS record deleted: {hostname}")
            return True

    async def _get_paginated(
        self,
        url: str,
        params: Optional[dict] = None,
        *,
        per_page: int = 1000,
        resource: str = "resource",
        max_pages: int = 1000,
    ) -> List[dict]:
        """GET a Cloudflare v4 LIST endpoint across **all** pages 並回傳完整 result。

        DP-PAGINATION 已用權威三來源（CF API docs / OpenAPI / cloudflare-python SDK）確認：
        ``dns_records`` / ``cfd_tunnel`` / ``access/apps`` / ``access/apps/{id}/policies`` /
        ``access/policies`` 全部是 **page/per_page offset 分頁**（無 cursor、無不分頁），且都有
        ``result_info.total_count``。故：``total_count`` 主停 + 空頁 fallback（不假設 server 一定
        回 total_count，也對「server 把 per_page 上限偷壓低」robust）+ ``max_pages`` 防病態無限迴圈。

        **fail-closed**：任何一頁非 200 / ``success=False`` 一律 raise，**絕不回部分/空清單**——
        部分清單會讓下游差集把「沒取到的資源」誤判成孤兒回收。``per_page`` 預設 1000（≤ cfd_tunnel
        硬上限 1000；DNS/access 皆可）。
        """
        base_params = dict(params or {})
        base_params["per_page"] = per_page
        results: List[dict] = []
        page = 1
        async with httpx.AsyncClient() as client:
            while True:
                response = await client.get(
                    url,
                    headers=self._headers,
                    params=dict(base_params, page=page),
                    timeout=30.0,
                )
                if response.status_code != 200:
                    raise CloudflareException(
                        f"Failed to list {resource} (page {page}): {response.text}",
                        status_code=response.status_code,
                    )
                data = response.json()
                if not data.get("success"):
                    raise CloudflareException(
                        f"Cloudflare API error listing {resource}: "
                        f"{data.get('errors', 'Unknown error')}"
                    )
                batch = data.get("result") or []
                results.extend(batch)
                if not batch:
                    break
                total_count = (data.get("result_info") or {}).get("total_count")
                if total_count is not None and len(results) >= total_count:
                    break
                if page >= max_pages:
                    raise CloudflareException(
                        f"Pagination for {resource} exceeded max_pages={max_pages} "
                        f"(collected {len(results)}); aborting fail-loud rather than "
                        "silently truncating."
                    )
                page += 1
        return results

    async def _list_dns_records_by_content(self, content: str) -> List[dict]:
        """List CNAME records in the zone whose content equals ``content`` (all pages).

        Used to find every record pointing at a dedicated tunnel
        ({tunnel_id}.cfargotunnel.com) for declarative reconciliation. 走完整分頁 —
        修 ``*-mcp 漏 15 筆`` 事故（原寫死 ``per_page=100`` 且無分頁迴圈 → 超過 100 筆同 content
        的孤兒被當「不存在」）。
        """
        return await self._get_paginated(
            self._dns_records_url,
            params={"type": "CNAME", "content": content},
            resource="DNS records",
        )

    # ============ Account-level list-all（L4 reconciler reverse-discovery）============
    # 帳號級反向發現能力：對 stored-id 取差集前，先撈 CF account 上的全量資源（右集）。
    # 皆走 _get_paginated（fail-closed、分頁到底），不可回部分清單。
    #
    # per_page 取值（DP-PAGINATION 研究結論）：tunnel 硬上限 1000（用 1000）、DNS 上限極大
    # （用助手預設 1000）；**access 三端點（apps / app-policies / reusable）的 per_page 硬上限
    # 在 CF OpenAPI 未明訂**（只有 default 1000/200/100）→ 保守用 50（必落在任何合理上限內），
    # 靠 total_count 迴圈補齊，避免「server 對過大 per_page 硬回 400」的首跑意外。多打幾頁對
    # 低頻 dry-run 對帳無感。
    _ACCESS_PER_PAGE = 50

    async def list_all_tunnels(self) -> List[dict]:
        """List **all** cfd_tunnel in the account（分頁到底），過濾軟刪（``deleted_at != null``）。

        軟刪 tunnel 不是孤兒、不該回收。fail-closed（錯誤 raise，絕不回 []）。
        """
        tunnels = await self._get_paginated(
            self._tunnels_url(), resource="tunnels", per_page=1000
        )
        return [t for t in tunnels if not t.get("deleted_at")]

    async def list_all_access_apps(self) -> List[dict]:
        """List **all** Access applications in the account（分頁到底）。"""
        return await self._get_paginated(
            f"{self._access_base}/apps", resource="access apps",
            per_page=self._ACCESS_PER_PAGE,
        )

    async def list_policies_of_app(self, app_id: str) -> List[dict]:
        """List policies attached to an Access app（分頁到底）。

        判 Access policy 孤兒以「CF app 實際引用」為 ground truth（不信 DB 白名單，因 resync
        重建 policy 會造成 DB↔CF 漂移）—— 即用本 method 取某 app 的真實 policy 清單。
        """
        return await self._get_paginated(
            f"{self._access_base}/apps/{app_id}/policies",
            resource=f"policies of app {app_id}",
            per_page=self._ACCESS_PER_PAGE,
        )

    async def list_all_reusable_policies(self) -> List[dict]:
        """List **all** account-level reusable Access policies（分頁到底）。"""
        return await self._get_paginated(
            f"{self._access_base}/policies", resource="reusable policies",
            per_page=self._ACCESS_PER_PAGE,
        )

    async def _delete_dns_record_by_id(self, record_id: str) -> bool:
        """Delete a DNS record by its Cloudflare record ID."""
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{self._dns_records_url}/{record_id}",
                headers=self._headers,
                timeout=30.0,
            )
            if response.status_code != 200:
                raise CloudflareException(
                    f"Failed to delete DNS record {record_id}: {response.text}",
                    status_code=response.status_code,
                )
            return True

    async def reconcile_tunnel_dns(
        self,
        tunnel_id: str,
        subdomains: List[str],
        comment: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        """Make the zone's DNS records for a dedicated tunnel match ``subdomains``.

        Declarative: creates missing CNAMEs, keeps matching ones, deletes
        records that point at this tunnel but are no longer desired.
        Idempotent — safe to call repeatedly (suspend/resume don't change
        hostnames, so reconcile is a no-op there).

        Returns:
            Mapping of subdomain -> DNS record ID.
        """
        if not self.dns_enabled:
            logger.info("DNS management disabled, skipping reconcile")
            return {s: None for s in subdomains}

        cname_target = self._tunnel_cname_target_for(tunnel_id)
        existing = await self._list_dns_records_by_content(cname_target)
        existing_by_sub = {}
        for rec in existing:
            # #824 site 2：本地複驗歸屬。撈取靠 CF 的 params={"content": ...} 收窄，
            # 但「不會撈到別人的」這件事在此之前**完全外包給那個外部契約**（content=
            # 是等值還是前綴／包含比對，repo 內所有程式碼都假設等值但未對真 CF 實測），
            # 而且撈回來到刪掉之間還有 TOCTOU 窗口。
            #
            # 守線刻意放在**進入 existing_by_sub 之前**，一道涵蓋兩個後果：範圍外的
            # 記錄既不會被當成自己的回報給呼叫端（那個 id 會被 Odoo 存進 dns_record_id、
            # 成為對別人記錄的錯誤歸屬），也不會走到下面的刪除迴圈。
            #
            # delete 側刻意**不 raise**：我們跟 CF 要的是「指向本 tunnel 的記錄」，CF 回
            # 了別的，刪掉才是 bug；跳過是安全動作，大聲 log 是訊號。raise 會讓整個 sync
            # 中止，而下一次 re-sync 會撞到同一筆 ⇒ 收斂不了的迴圈。
            if not dns_record_points_at(rec, cname_target):
                logger.error(
                    f"Reconcile: refusing to touch DNS record {rec.get('name')} "
                    f"(id={rec.get('id')}): its content {rec.get('content')!r} does "
                    f"not point at tunnel {tunnel_id}. Cloudflare returned a record "
                    f"outside the requested content= scope — skipping it (#824)."
                )
                continue
            # rec["name"] is the FQDN; strip the zone domain suffix
            name = rec.get("name", "")
            if name.endswith(f".{self.domain}"):
                existing_by_sub[name[: -(len(self.domain) + 1)]] = rec

        desired = set(subdomains)
        result: Dict[str, Optional[str]] = {}

        for sub in subdomains:
            if sub in existing_by_sub:
                result[sub] = existing_by_sub[sub]["id"]
            else:
                result[sub] = await self.create_dns_record_for_tunnel(
                    subdomain=sub, tunnel_id=tunnel_id, comment=comment,
                )

        for sub, rec in existing_by_sub.items():
            if sub not in desired:
                logger.info(
                    f"Reconcile: deleting stale DNS record {rec['name']} "
                    f"(tunnel {tunnel_id})"
                )
                await self._delete_dns_record_by_id(rec["id"])

        return result

    # ==================== Tunnel Configuration ====================

    async def get_tunnel_config(self) -> dict:
        """Get current tunnel configuration.

        Returns:
            Current tunnel configuration including ingress rules.

        Raises:
            CloudflareException: If API request fails.
        """
        if not self.enabled:
            return {"config": {"ingress": [{"service": "http_status:404"}]}}

        async with httpx.AsyncClient() as client:
            response = await client.get(
                self._tunnel_config_url,
                headers=self._headers,
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error(
                    f"Failed to get tunnel config: {response.status_code} - {response.text}"
                )
                raise CloudflareException(
                    f"Failed to get tunnel configuration: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            return data.get("result", {})

    async def update_tunnel_config(self, ingress_rules: List[dict]) -> bool:
        """Update tunnel configuration with new ingress rules.

        Args:
            ingress_rules: List of ingress rule dictionaries.

        Returns:
            True if update was successful.

        Raises:
            CloudflareException: If API request fails.
        """
        if not self.enabled:
            logger.info("Cloudflare integration disabled, skipping config update")
            return True

        # Ensure catch-all rule is at the end
        if not ingress_rules or ingress_rules[-1].get("hostname"):
            ingress_rules.append({"service": "http_status:404"})

        payload = {"config": {"ingress": ingress_rules}}

        async with httpx.AsyncClient() as client:
            response = await client.put(
                self._tunnel_config_url,
                headers=self._headers,
                json=payload,
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error(
                    f"Failed to update tunnel config: {response.status_code} - {response.text}"
                )
                raise CloudflareException(
                    f"Failed to update tunnel configuration: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            logger.info("Tunnel configuration updated successfully")
            return True

    async def create_route(
        self,
        subdomain: str,
        service_url: str,
        path: Optional[str] = None,
    ) -> RouteOutcome:
        """Create a new tunnel route for a service.

        This method creates both the tunnel ingress route and the DNS CNAME record.

        Args:
            subdomain: Subdomain for the route (e.g., 'myapp' for myapp.domain.com).
            service_url: Internal service URL (e.g., 'http://svc.ns.svc.cluster.local:8080').
            path: Optional path prefix to match.

        Returns:
            A :class:`RouteOutcome`. **The return type changed from ``bool`` in #648
            (a)**: the DNS half of this method can fail on its own while the tunnel
            ingress half succeeds, and the old ``bool`` had nowhere to say so — the
            failure was logged and then vanished (see the Step 1 comment below).
            Both existing callers ignored the return value entirely, so widening it
            breaks nothing.

        Raises:
            CloudflareException: If route creation fails.
        """
        if not self.enabled:
            logger.info(f"Cloudflare disabled, skipping route creation for {subdomain}")
            return RouteOutcome(configured=False, dns_failed=False)

        hostname = f"{subdomain}.{self.domain}"
        logger.info(f"Creating route: {hostname} -> {service_url}")

        # Step 1: Create DNS record (so the hostname resolves)
        #
        # Swallowing the exception is deliberate (DNS may be managed by hand), but as
        # of #648 (a) it is no longer *silent*: the failure is reported to the caller
        # via ``dns_failed`` so it can decide whether to fail closed, exactly like
        # ``TunnelCreateResponse.dns_failed`` already does on the tunnel-create path
        # (#484). Before this change the platform marked the service as running with a
        # hostname that would never resolve.
        #
        # ⚠ Invariant: ``dns_failed`` can only become True when DNS management is
        # actually enabled. ``create_dns_record`` returns early (True) when
        # ``self.dns_enabled`` is false, so it cannot raise in that case — "DNS is
        # intentionally off" therefore never masquerades as "DNS failed".
        dns_failed = False
        try:
            await self.create_dns_record(subdomain)
        except CloudflareException as e:
            logger.error(f"Failed to create DNS record for {subdomain}: {e.message}")
            dns_failed = True

        # Step 2: Create tunnel ingress route
        # Get current config
        current_config = await self.get_tunnel_config()
        ingress_rules = current_config.get("config", {}).get("ingress", [])

        # Remove catch-all rule temporarily (always last)
        catch_all = None
        if ingress_rules and not ingress_rules[-1].get("hostname"):
            catch_all = ingress_rules.pop()

        # Check if route already exists
        for rule in ingress_rules:
            if rule.get("hostname") == hostname:
                logger.warning(f"Route for {hostname} already exists, updating...")
                rule["service"] = service_url
                if path:
                    rule["path"] = path
                elif "path" in rule:
                    del rule["path"]
                break
        else:
            # Add new rule
            new_rule = {"hostname": hostname, "service": service_url}
            if path:
                new_rule["path"] = path
            ingress_rules.append(new_rule)

        # Re-add catch-all rule
        if catch_all:
            ingress_rules.append(catch_all)

        configured = await self.update_tunnel_config(ingress_rules)
        return RouteOutcome(configured=configured, dns_failed=dns_failed)

    async def delete_route(self, subdomain: str) -> bool:
        """Delete a tunnel route and its DNS record.

        Args:
            subdomain: Subdomain of the route to delete.

        Returns:
            True if route was deleted successfully.

        Raises:
            CloudflareException: If route deletion fails.
        """
        if not self.enabled:
            logger.info(f"Cloudflare disabled, skipping route deletion for {subdomain}")
            return True

        hostname = f"{subdomain}.{self.domain}"
        logger.info(f"Deleting route: {hostname}")

        # Step 1: Delete tunnel ingress route
        # Get current config
        current_config = await self.get_tunnel_config()
        ingress_rules = current_config.get("config", {}).get("ingress", [])

        # Filter out the route to delete
        new_rules = [rule for rule in ingress_rules if rule.get("hostname") != hostname]

        if len(new_rules) == len(ingress_rules):
            logger.warning(f"Route for {hostname} not found, nothing to delete")
        else:
            await self.update_tunnel_config(new_rules)

        # Step 2: Delete DNS record
        try:
            await self.delete_dns_record(subdomain)
        except Exception as e:
            logger.warning(f"Failed to delete DNS record for {subdomain}: {e}")
            # Don't fail - route is already deleted

        return True

    async def list_routes(self) -> List[TunnelRoute]:
        """List all tunnel routes.

        Returns:
            List of TunnelRoute objects.
        """
        if not self.enabled:
            return []

        current_config = await self.get_tunnel_config()
        ingress_rules = current_config.get("config", {}).get("ingress", [])

        routes = []
        for rule in ingress_rules:
            routes.append(
                TunnelRoute(
                    hostname=rule.get("hostname"),
                    service=rule.get("service", ""),
                    path=rule.get("path"),
                )
            )

        return routes

    def generate_subdomain(self, namespace: str, release_name: str) -> str:
        """Generate a subdomain for a release.

        Args:
            namespace: Kubernetes namespace.
            release_name: Helm release name.

        Returns:
            Generated subdomain string.
        """
        # Remove paas-ws- prefix from namespace for cleaner URLs
        ns_suffix = namespace.replace("paas-ws-", "")
        return f"{release_name}-{ns_suffix}"

    def generate_service_url(
        self,
        namespace: str,
        service_name: str,
        port: int = 80,
    ) -> str:
        """Generate internal Kubernetes service URL.

        Args:
            namespace: Kubernetes namespace.
            service_name: Kubernetes service name.
            port: Service port.

        Returns:
            Internal service URL.
        """
        return f"http://{service_name}.{namespace}.svc.cluster.local:{port}"

    # ==================== Dedicated Tunnel Lifecycle ====================

    async def create_tunnel(self, name: str) -> Dict[str, Any]:
        """Create a new Cloudflare Tunnel.

        Args:
            name: Name for the new tunnel.

        Returns:
            Dict with tunnel_id and tunnel_name.

        Raises:
            CloudflareException: If tunnel creation fails.
        """
        tunnel_secret = base64.b64encode(secrets.token_bytes(32)).decode("utf-8")

        payload = {
            "name": name,
            "tunnel_secret": tunnel_secret,
        }

        logger.info(f"Creating Cloudflare Tunnel: {name}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._tunnels_url(),
                headers=self._headers,
                json=payload,
                timeout=30.0,
            )

            if response.status_code not in (200, 201):
                logger.error(
                    f"Failed to create tunnel: {response.status_code} - {response.text}"
                )
                raise CloudflareException(
                    f"Failed to create tunnel: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            result = data.get("result", {})
            tunnel_id = result.get("id", "")
            tunnel_name = result.get("name", name)

            logger.info(f"Tunnel created: {tunnel_name} (ID: {tunnel_id})")

            return {
                "tunnel_id": tunnel_id,
                "tunnel_name": tunnel_name,
            }

    async def get_tunnel_token(self, tunnel_id: str) -> str:
        """Get the token for an existing Cloudflare Tunnel.

        The token is a JWT generated by Cloudflare that contains the tunnel_id
        and secret. It is used by cloudflared to authenticate with the tunnel.

        Args:
            tunnel_id: The tunnel ID.

        Returns:
            Tunnel token string.

        Raises:
            CloudflareException: If token retrieval fails.
        """
        url = f"{self._tunnels_url(tunnel_id)}/token"

        logger.info(f"Getting token for tunnel: {tunnel_id}")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=self._headers,
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error(
                    f"Failed to get tunnel token: {response.status_code} - {response.text}"
                )
                raise CloudflareException(
                    f"Failed to get tunnel token: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            token = data.get("result", "")
            logger.info(f"Token retrieved for tunnel: {tunnel_id}")
            return token

    async def configure_tunnel(
        self, tunnel_id: str, hostname: str, service_url: str
    ) -> None:
        """Set the ingress configuration for a dedicated tunnel.

        Configures the tunnel with a single hostname rule pointing to the
        specified service URL, plus a catch-all 404 rule.

        Args:
            tunnel_id: The tunnel ID.
            hostname: Public hostname (e.g., 'myapp.domain.com').
            service_url: Backend service URL (e.g., 'http://localhost:8123').

        Raises:
            CloudflareException: If configuration update fails.
        """
        url = f"{self._tunnels_url(tunnel_id)}/configurations"

        ingress_rules = [
            {"hostname": hostname, "service": service_url},
            {"service": "http_status:404"},
        ]

        payload = {"config": {"ingress": ingress_rules}}

        logger.info(
            f"Configuring tunnel {tunnel_id}: {hostname} -> {service_url}"
        )

        async with httpx.AsyncClient() as client:
            response = await client.put(
                url,
                headers=self._headers,
                json=payload,
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error(
                    f"Failed to configure tunnel: {response.status_code} - {response.text}"
                )
                raise CloudflareException(
                    f"Failed to configure tunnel: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            logger.info(f"Tunnel {tunnel_id} configured successfully")

    async def configure_tunnel_ingress(
        self, tunnel_id: str, rules: List[dict]
    ) -> None:
        """Replace a dedicated tunnel's full ingress rule list.

        Multi-route counterpart of ``configure_tunnel`` (which only expresses
        a single hostname). Each rule dict: {hostname, service,
        http_host_header}. A catch-all 404 rule is appended automatically.

        Args:
            tunnel_id: The tunnel ID.
            rules: Desired ingress rules, order preserved.

        Raises:
            CloudflareException: If configuration update fails.
        """
        url = f"{self._tunnels_url(tunnel_id)}/configurations"

        ingress_rules = []
        for rule in rules:
            entry = {"hostname": rule["hostname"], "service": rule["service"]}
            if rule.get("http_host_header"):
                entry["originRequest"] = {"httpHostHeader": rule["hostname"]}
            ingress_rules.append(entry)
        ingress_rules.append({"service": "http_status:404"})

        payload = {"config": {"ingress": ingress_rules}}

        logger.info(
            f"Configuring tunnel {tunnel_id} ingress with {len(rules)} route(s)"
        )

        async with httpx.AsyncClient() as client:
            response = await client.put(
                url,
                headers=self._headers,
                json=payload,
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error(
                    f"Failed to configure tunnel ingress: "
                    f"{response.status_code} - {response.text}"
                )
                raise CloudflareException(
                    f"Failed to configure tunnel ingress: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            logger.info(f"Tunnel {tunnel_id} ingress configured successfully")

    async def get_tunnel_status(self, tunnel_id: str) -> Dict[str, Any]:
        """Get the status and connection info for a Cloudflare Tunnel.

        Args:
            tunnel_id: The tunnel ID.

        Returns:
            Dict with tunnel status, name, connections, created_at and
            ``deleted_at``.

            ⚠ **``deleted_at`` 轉發的是 CF 的事實，不是我們算出來的判斷**（#833）：
            非 None ＝ CF 上已**軟刪除**；None ＝ 未刪除。CF 對未刪除的 tunnel 回的
            是 ``deleted_at: null``（欄位存在），但 openapi 的 ``tunnel_cfd_tunnel``
            沒有 ``required`` 陣列 ⇒ 兩種形狀都可能出現，**判準一律是 truthy，不是
            ``"deleted_at" in payload``**。

        Raises:
            CloudflareException: If status retrieval fails.

        ⚠ **這支不是「這條 tunnel 還在不在」的權威來源**（#824／#833）：CF 刪
        tunnel 是軟刪除，DELETE 之後這裡仍回 **200**。本 method 現在會**如實轉發**
        ``deleted_at`` 讓呼叫端自己判，但它仍然只看得到**一條**指定的 tunnel；
        需要「這條 tunnel 在不在帳號的存活集合裡」的判定請走 ``list_all_tunnels``
        （有濾軟刪，且 fail-closed —— 錯誤 raise、絕不回空清單）。
        """
        url = self._tunnels_url(tunnel_id)

        logger.info(f"Getting status for tunnel: {tunnel_id}")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=self._headers,
                timeout=30.0,
            )

            if response.status_code == 404:
                raise CloudflareException(
                    f"Tunnel {tunnel_id} not found",
                    status_code=404,
                )

            if response.status_code != 200:
                logger.error(
                    f"Failed to get tunnel status: {response.status_code} - {response.text}"
                )
                raise CloudflareException(
                    f"Failed to get tunnel status: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            result = data.get("result", {})

            # Extract connection details
            connections = []
            for conn in result.get("connections", []):
                connections.append(
                    {
                        "connector_id": conn.get("id", ""),
                        "type": conn.get("type", ""),
                        "origin_ip": conn.get("origin_ip", ""),
                        "opened_at": conn.get("opened_at", ""),
                    }
                )

            return {
                "tunnel_id": result.get("id", tunnel_id),
                "name": result.get("name", ""),
                "status": result.get("status", "unknown"),
                "connections": connections,
                "created_at": result.get("created_at"),
                # #833：CF 的軟刪除訊號，原值轉發。⛔ 不要在這裡把它塌成布林或
                # 併進 ``status``——CF 的 ``status`` 列舉只有 inactive / degraded /
                # healthy / down，**沒有一個代表已刪除**，塞進去會毀掉既有語意。
                "deleted_at": result.get("deleted_at"),
            }

    async def delete_tunnel(self, tunnel_id: str) -> None:
        """Delete a Cloudflare Tunnel and clean up associated DNS records.

        Attempts to clean up DNS records associated with the tunnel before
        deleting it. Uses cascade=true to also remove tunnel connections.

        Args:
            tunnel_id: The tunnel ID to delete.

        Raises:
            CloudflareException: If tunnel deletion fails.
        """
        logger.info(f"Deleting tunnel: {tunnel_id}")

        # Try to clean up DNS records pointing to this tunnel
        if self.dns_enabled:
            cname_target = self._tunnel_cname_target_for(tunnel_id)
            try:
                await self._cleanup_dns_for_tunnel(cname_target)
            except Exception as e:
                logger.warning(
                    f"Failed to clean up DNS records for tunnel {tunnel_id}: {e}"
                )
                # Continue with tunnel deletion even if DNS cleanup fails

        # Delete the tunnel with cascade to clean up connections
        url = self._tunnels_url(tunnel_id)

        async with httpx.AsyncClient() as client:
            response = await client.delete(
                url,
                headers=self._headers,
                params={"cascade": "true"},
                timeout=30.0,
            )

            if response.status_code == 404:
                logger.warning(f"Tunnel {tunnel_id} not found, nothing to delete")
                return

            if response.status_code != 200:
                logger.error(
                    f"Failed to delete tunnel: {response.status_code} - {response.text}"
                )
                raise CloudflareException(
                    f"Failed to delete tunnel: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            logger.info(f"Tunnel {tunnel_id} deleted successfully")

    async def _cleanup_dns_for_tunnel(self, cname_target: str) -> None:
        """Remove DNS records that point to a specific tunnel CNAME target.

        撈取改走 ``_list_dns_records_by_content``（分頁到底、fail-closed），不再自己送
        一次寫死單頁的 GET —— 原本那個查詢帶著 ``*-mcp 漏 15 筆`` 那個**已經在
        ``_list_dns_records_by_content`` 修掉**的分頁 bug：超過一頁的記錄會被當成不存在
        而永遠殘留。撈取失敗現在會 raise（而不是靜默 return），由唯一呼叫端
        ``delete_tunnel`` 的 ``except Exception`` 收成 warning 後照樣繼續刪 tunnel ——
        對外行為不變，但「沒清到」不再是靜默的。

        每一筆刪除前本地複驗歸屬（#824 site 5）：不符就跳過 ＋ ERROR log ＋繼續，不
        raise（理由同 ``reconcile_tunnel_dns``：raise 會讓清理中止且無法收斂）。

        Args:
            cname_target: The CNAME target to search for (e.g., '<tunnel_id>.cfargotunnel.com').
        """
        records = await self._list_dns_records_by_content(cname_target)

        async with httpx.AsyncClient() as client:
            for record in records:
                record_id = record.get("id")
                record_name = record.get("name", "unknown")

                if not dns_record_points_at(record, cname_target):
                    logger.error(
                        f"Refusing to delete DNS record {record_name} "
                        f"(id={record_id}) during tunnel cleanup: its content "
                        f"{record.get('content')!r} does not point at "
                        f"{cname_target!r}. Cloudflare returned a record outside "
                        f"the requested content= scope — skipping it (#824)."
                    )
                    continue

                logger.info(f"Cleaning up DNS record: {record_name} (ID: {record_id})")

                delete_response = await client.delete(
                    f"{self._dns_records_url}/{record_id}",
                    headers=self._headers,
                    timeout=30.0,
                )

                if delete_response.status_code == 200:
                    logger.info(f"DNS record {record_name} deleted")
                else:
                    logger.warning(
                        f"Failed to delete DNS record {record_name}: "
                        f"{delete_response.status_code}"
                    )

    # ==================== Cloudflare Access (Zero Trust) ====================

    @property
    def _access_base(self) -> str:
        return f"{self.BASE_URL}/accounts/{self.account_id}/access"

    async def list_identity_providers(self) -> List[dict]:
        """List the account's Access identity providers.

        Returns list of {id, name, type}. Needs token scope
        'Access: Organizations, Identity Providers and Groups (Read)' —
        a 403 here means that scope is missing.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._access_base}/identity_providers",
                headers=self._headers,
                timeout=30.0,
            )
            if response.status_code != 200:
                raise CloudflareException(
                    f"Failed to list identity providers: {response.text}",
                    status_code=response.status_code,
                )
            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown')}"
                )
            return data.get("result", [])

    async def create_reusable_policy(
        self,
        name: str,
        decision: str,
        include: List[dict],
        exclude: Optional[List[dict]] = None,
        require: Optional[List[dict]] = None,
    ) -> str:
        """Create a reusable Access policy; return its id."""
        payload = {
            "name": name,
            "decision": decision,
            "include": include,
            "exclude": exclude or [],
            "require": require or [],
            "reusable": True,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._access_base}/policies",
                headers=self._headers,
                json=payload,
                timeout=30.0,
            )
            if response.status_code not in (200, 201):
                raise CloudflareException(
                    f"Failed to create access policy: {response.text}",
                    status_code=response.status_code,
                )
            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown')}"
                )
            return data["result"]["id"]

    async def delete_reusable_policy(self, policy_id: str) -> bool:
        """Delete a reusable Access policy by id.

        Cloudflare's DELETE on Access policies/apps replies **202 Accepted**
        (body ``{"success": true, "result": {"id": ...}}``), not 200 — and 404
        when already gone. Treat all three as success; anything else is a real
        failure. (Mocked tests returned 200, masking this until a live CF call.)
        """
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{self._access_base}/policies/{policy_id}",
                headers=self._headers,
                timeout=30.0,
            )
            if response.status_code not in (200, 202, 404):
                raise CloudflareException(
                    f"Failed to delete access policy {policy_id}: {response.text}",
                    status_code=response.status_code,
                )
            return True

    async def _resolve_idp_ids(
        self, idp_types: List[str]
    ) -> tuple[List[str], List[str]]:
        """Map IdP type strings (google/onetimepin) to account IdP ids.

        Empty input → empty list (= app shows all available IdPs). A requested
        type with no matching IdP on the account is skipped (logged) — but it
        is also collected and returned so the caller can surface the silent
        downgrade. Returns ``(resolved_ids, unresolved_types)``.

        Security note: a requested-but-missing type that is silently dropped to
        ``[]`` flips the app to "all available IdPs" (login restriction widened,
        opposite of intent). Reporting ``unresolved_types`` lets Odoo/the BFF
        warn the user instead of letting the widening go unnoticed.
        """
        if not idp_types:
            return [], []
        idps = await self.list_identity_providers()
        by_type = {}
        for idp in idps:
            by_type.setdefault(idp.get("type"), idp.get("id"))
        resolved = []
        unresolved = []
        for t in idp_types:
            idp_id = by_type.get(t)
            if idp_id:
                resolved.append(idp_id)
            else:
                unresolved.append(t)
                logger.warning(
                    "Access: login method '%s' has no IdP on the account; "
                    "skipping (configure it in Zero Trust → Settings → "
                    "Authentication).", t,
                )
        return resolved, unresolved

    async def _delete_access_app(self, app_id: str) -> None:
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{self._access_base}/apps/{app_id}",
                headers=self._headers, timeout=30.0,
            )
            # CF replies 202 Accepted on a successful Access-app delete (not
            # 200); 404 = already gone. Anything else is a real failure. Reading
            # 202 as failure made teardown raise *after* CF actually deleted the
            # app, aborting the subsequent policy cleanup → orphan policies.
            if response.status_code not in (200, 202, 404):
                raise CloudflareException(
                    f"Failed to delete access app {app_id}: {response.text}",
                    status_code=response.status_code,
                )

    async def sync_access_app(
        self,
        canonical_name: str,
        app_name: str,
        primary_hostname: str,
        protected_hostnames: List[str],
        session_duration: str,
        login_idp_types: List[str],
        policies: List[dict],
        enabled: bool = True,
        existing_app_id: Optional[str] = None,
        existing_policy_ids: Optional[List[str]] = None,
    ) -> dict:
        """Declaratively reconcile one SA's Cloudflare Access config.

        Strategy (simple + idempotent): always delete the SA's previous
        reusable policies (existing_policy_ids) then recreate from the desired
        list; create-or-update the single per-SA Access App referencing them.
        enabled=False → tear down app + policies, return empty.

        Returns {app_id, policy_ids, allowed_idps, unresolved_idp_types}.
        """
        existing_policy_ids = existing_policy_ids or []

        # Teardown path: disabled or no protected hostnames → remove everything.
        if not enabled or not protected_hostnames:
            if existing_app_id:
                await self._delete_access_app(existing_app_id)
            orphaned = []
            for pid in existing_policy_ids:
                # Best-effort: a leftover policy must not fail teardown. Catch
                # broadly — a transient httpx transport error is not a
                # CloudflareException; collect failures so the caller can observe
                # the orphan (the SA clears its ids on teardown regardless).
                try:
                    await self.delete_reusable_policy(pid)
                except Exception as e:
                    orphaned.append(pid)
                    logger.warning("Access teardown: policy %s delete failed "
                                   "(left as orphan): %s", pid, e)
            return {"app_id": None, "policy_ids": [], "allowed_idps": [],
                    "unresolved_idp_types": [], "orphaned_policy_ids": orphaned}

        # Resync strategy (detach-before-delete): create the NEW reusable
        # policies, point the app at them (which detaches the OLD ones), and
        # only THEN delete the old policies (deferred to after a successful app
        # update, below). Deleting old policies first — while the app still
        # references them — makes Cloudflare reject the delete as "in use",
        # silently orphaning them.
        new_policy_ids = []
        # Compensating-cleanup scope: this is a two-stage remote provision
        # (create N reusable policies, then PUT/POST the app referencing them).
        # If the app update fails, the policies just created are NOT returned to
        # the caller (the function raises), so Odoo's savepoint rolls back the
        # DB row and the next idempotent re-sync only knows the OLD cf_policy_ids
        # — these would be permanent Cloudflare orphans, never reconciled. Wrap
        # the whole create+update window and best-effort delete this batch on
        # failure before re-raising, so a partial failure leaves no orphans.
        try:
            for idx, p in enumerate(policies):
                include = self._build_rule_group(p.get("include", []))
                require = self._build_rule_group(p.get("require", []))
                exclude = self._build_rule_group(p.get("exclude", []))
                pid = await self.create_reusable_policy(
                    name=f"{canonical_name}: {p['name']}",
                    decision=p["decision"],
                    include=include,
                    require=require,
                    exclude=exclude,
                )
                new_policy_ids.append((pid, p.get("precedence", idx + 1)))

            allowed_idps, unresolved_idps = await self._resolve_idp_ids(
                login_idp_types)

            app_payload = {
                "name": app_name,
                "type": "self_hosted",
                "domain": primary_hostname,
                "destinations": [{"type": "public", "uri": h}
                                 for h in protected_hostnames],
                "session_duration": session_duration,
                "allowed_idps": allowed_idps,
                "auto_redirect_to_identity": False,
                "app_launcher_visible": False,
                # Sort by precedence (lower first) so the declared evaluation
                # order reaches CF deterministically. CF itself still tiers
                # Bypass/Service-Auth ahead of Allow/Block — we do not re-tier.
                "policies": [{"id": pid, "precedence": prec}
                             for pid, prec in sorted(
                                 new_policy_ids, key=lambda t: t[1])],
            }

            async with httpx.AsyncClient() as client:
                if existing_app_id:
                    response = await client.put(
                        f"{self._access_base}/apps/{existing_app_id}",
                        headers=self._headers, json=app_payload, timeout=30.0,
                    )
                else:
                    response = await client.post(
                        f"{self._access_base}/apps",
                        headers=self._headers, json=app_payload, timeout=30.0,
                    )
                if response.status_code not in (200, 201):
                    raise CloudflareException(
                        f"Failed to sync access app: {response.text}",
                        status_code=response.status_code,
                    )
                data = response.json()
                if not data.get("success"):
                    raise CloudflareException(
                        f"Cloudflare API error: {data.get('errors', 'Unknown')}"
                    )
                app_id = data["result"]["id"]
        except Exception:
            # App update failed after policies were created → reclaim them so
            # they don't linger as Cloudflare orphans (the caller's DB rollback
            # would otherwise lose all reference to this batch).
            for pid, _ in new_policy_ids:
                # Best-effort reclaim: one delete error must not abort cleanup of
                # the rest. Catch broadly (transient httpx errors are not
                # CloudflareException); the original failure is re-raised below.
                try:
                    await self.delete_reusable_policy(pid)
                except Exception as ce:
                    logger.warning(
                        "Access rollback: orphan policy %s delete failed: %s",
                        pid, ce)
            raise

        # App now references only the new policies → the OLD ones are detached
        # and safe to delete. Best-effort: a leftover policy is undesirable but
        # must NOT fail an otherwise-successful sync — the app PUT already
        # committed CF's desired state, and raising here would roll back the
        # caller's DB write-back into CF↔DB divergence. Catch broadly (a
        # transient httpx transport error is not a CloudflareException) and
        # collect failures so the caller can observe the orphan (mirrors the
        # unresolved_idp_types signal channel). Skip ids we just created
        # (defensive; create always returns fresh ids).
        new_ids = {pid for pid, _ in new_policy_ids}
        orphaned = []
        for pid in existing_policy_ids:
            if pid in new_ids:
                continue
            try:
                await self.delete_reusable_policy(pid)
            except Exception as e:
                orphaned.append(pid)
                logger.warning("Access resync: old policy %s delete failed "
                               "(left as orphan): %s", pid, e)

        return {
            "app_id": app_id,
            "policy_ids": [pid for pid, _ in new_policy_ids],
            "allowed_idps": allowed_idps,
            # Requested login methods with no matching account IdP. Non-empty
            # here means the resolved set was narrowed (or, if it became empty,
            # the app silently fell back to "all available IdPs") — the caller
            # must surface this so the login-restriction widening is visible.
            "unresolved_idp_types": unresolved_idps,
            # Old reusable policies that could not be deleted after the app was
            # repointed — permanent CF orphans the caller should log/alert on.
            "orphaned_policy_ids": orphaned,
        }

    @staticmethod
    def _build_access_rule(rule_type: str, rule_value: str) -> List[dict]:
        """Map (rule_type, rule_value) to a list of Cloudflare Access rule dicts.

        A single selector may expand to multiple rules (email_list → one per
        email, country → one per code). The same dict shape is valid in any of a
        policy's include / require / exclude groups.
        """
        value = rule_value or ""
        if rule_type == "everyone":
            return [{"everyone": {}}]
        if rule_type == "email_domain":
            return [{"email_domain": {"domain": value.lstrip("@").strip()}}]
        if rule_type == "email_list":
            emails = [e.strip() for e in value.split(",") if e.strip()]
            return [{"email": {"email": e}} for e in emails]
        if rule_type == "ip_range":
            return [{"ip": {"ip": value.strip()}}]
        if rule_type == "country":
            codes = [c.strip().upper() for c in value.split(",") if c.strip()]
            return [{"geo": {"country_code": c}} for c in codes]
        raise CloudflareException(f"Unknown access rule_type: {rule_type}")

    @staticmethod
    def _build_rule_group(rules: List[dict]) -> List[dict]:
        """Flatten a group's selector specs ({rule_type, rule_value}) into the
        list of Cloudflare rule dicts for that include/require/exclude group."""
        out: List[dict] = []
        for r in rules or []:
            out.extend(CloudflareService._build_access_rule(
                r["rule_type"], r.get("rule_value") or ""))
        return out
