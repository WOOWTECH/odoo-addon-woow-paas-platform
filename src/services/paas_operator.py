"""HTTP client for PaaS Operator Service.

This module provides a client for communicating with the PaaS Operator
FastAPI service that manages Helm releases in Kubernetes.
"""
import json
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout

_logger = logging.getLogger(__name__)

# Default timeout for HTTP requests (seconds)
DEFAULT_TIMEOUT = 30
# Resource observations are an interactive, read-only request, not a Helm job.
RESOURCE_OBSERVATION_TIMEOUT = 10
# Longer timeout for helm operations that may take time
HELM_OPERATION_TIMEOUT = 120
# #1152：init 專屬 connect/read inactivity 預算，不連帶放大其他操作或 connect 等待。
# operator 宣告子步 envelope：discovery 90 + readiness (30×15 + 29×5) + update 120
# = 805s；read 留 95s 餘裕。repo-lint init-odoo-budget-ladder 核對實際來源與接線。
# 這不是 server／整體 HTTP 的硬 deadline：排隊、process 收尾與遠端 exec 取消不保證。
# #1257 起 init 只由 cron 經 model 的單一入口執行（list/get 不再同步呼叫），外層
# 預算是由這組常數推導的 lease（src/models/cloud_service_init_attempt.py）；init
# cron 的每 tick 預算見 #1159。
INIT_ODOO_CONNECT_TIMEOUT = 120
INIT_ODOO_READ_TIMEOUT = 900
# #1257 Q31：n8n init 專屬 connect/read 預算，不再共用 HELM_OPERATION_TIMEOUT=120
# （那個 120s 比 operator 端的子步 envelope 短，client 會先斷線、server 照跑）。
# envelope 由 repo-lint init-n8n-budget-ladder 從 operator 原始碼的 AST 算出（目前
# 571s：find_pod 60＋readiness 206＋owner check 15＋max(新建 owner：node request 4×15＋
# 刪舊 key 40，既有 owner：exec 40)＋secret／patch／deployment 3×30＋restart 100；#1471
# ／#1534）；read 600 留約 29s 餘裕。同樣不是 server／整體 HTTP 的硬
# deadline，lease 由這組常數推導（src/models/cloud_service_init_attempt.py）。
INIT_N8N_CONNECT_TIMEOUT = 120
INIT_N8N_READ_TIMEOUT = 600
# #461：reset-admin-password 專屬預算。**刻意不共用** HELM_OPERATION_TIMEOUT——
# 那個 120s 是「helm 操作大概要多久」的猜測，與 operator 端真正的上限沒有關係，
# 而 reset 是唯一會撞到那個落差的路徑（#459 stg 實測：operator 最壞 ≈560s，
# 平台 120s 必先斷線 ⇒ 密碼不落庫、租戶得重按第二次，冷 image cache 時必然發生）。
#
# 修法是宣告一條可機械檢查的階梯，而不是把數字改大：
#
#     前端 RESET_REQUEST_TIMEOUT_MS(90_000)
#       > 本常數(75s)
#         > operator RESET_HARD_CAP_SECONDS(73s，含 #1552 等 helm 名額的 8s；見 extra/paas-operator/src/api/releases.py)
#
# 由 scripts/audit/repo-lint.py 的 check_reset_budget_ladder 跨三層驗算；改任一層
# 的數字而沒同步，那道 gate 會紅。**這個 75 比 120 小是刻意的**：reset 移除
# rollout 等待後，request 內只剩 helm upgrade（實測 4–12s），把外層預算收到
# operator 硬上限之上一點，逾時才能重新變成「異常訊號」而不是常態。
RESET_CONFIG_TIMEOUT = 75
# #1370：Odoo／n8n 管理員密碼重設（operator exec 腳本直接改 app DB）專屬預算。與
# reset-config 共用同一條前端 route（RESET_REQUEST_TIMEOUT_MS），所以階梯是
#
#     前端 RESET_REQUEST_TIMEOUT_MS(90_000)
#       > 本常數(75s)
#         > operator 的 exec 逾時＋找 pod 餘裕（Odoo 45＋15、n8n 30＋15，
#           見 extra/paas-operator/src/services/app_admin_password.py）
#
# 由 scripts/audit/repo-lint.py 的 check_app_admin_reset_budget_ladder 驗算。
APP_ADMIN_RESET_TIMEOUT = 75

#: #1563：停權說明頁 park／unpark 的預算。operator 端最壞是 kubectl（30s）＋Cloudflare
#: 讀＋寫 tunnel config（各 30s），unpark 另多一次讀與 Service 查詢——給 150s，逾時仍是
#: 「不知道有沒有改到」，呼叫端照各自的失敗語意處理（park 只記 log、unpark fail-loud）。
ROUTE_PARK_TIMEOUT = 150

#: #1540：資料取回入口開／關的預算。operator 端是一串 kubectl（各 20s 上限，實測通常 <1s）＋
#: Cloudflare 讀寫 tunnel config（各 30s 上限）；實測開一次約數秒。給 75s——前端 90s
#: （``services/data_retrieval.js``），再長會撞 Cloudflare 約 100s 的 524。逾時＝不知道有沒有
#: 開成，呼叫端不寫 session（operator 端的入口有 activeDeadlineSeconds 與 sweep 兜底）。
DATA_RETRIEVAL_TIMEOUT = 75

#: #1647：工作區頁讀 namespace quota 的 (connect, read) 預算。階梯：
#:
#:     前端 QUOTA_REQUEST_TIMEOUT_MS(15_000，``services/workspace_quota.js``)
#:       > 本常數（connect 3＋read 8＝最多 11s）
#:         > operator NS_QUOTA_READ_TIMEOUT_SECONDS(5s，一次 kubectl get；
#:           extra/paas-operator ``KubernetesService``)
#:
#: **刻意比 DEFAULT_TIMEOUT 短**：這是租戶開工作區頁時同步呼叫的唯讀查詢，operator 掛掉
#: 或連不上時不能讓一個 Odoo worker 被卡 30 秒——拿不到就讓面板顯示「暫時無法取得」。
#:
#: 階梯的守線分兩處，各守自己讀得到的那一段：
#:
#:   * 前端 ＞ 本常數（connect＋read）、本常數 ＜ DEFAULT_TIMEOUT：
#:     ``src/tests/test_workspace_quota_1647.py``（Odoo 測試讀得到前端的 JS，讀不到
#:     extra/paas-operator）。
#:   * 本常數的 read ＞ operator 的 NS_QUOTA_READ_TIMEOUT_SECONDS：
#:     ``extra/paas-operator/tests/test_namespace_quota_1647.py``。它從 repo 根以 regex 讀
#:     下面這一行 ⇒ 這一行要維持 ``NAMESPACE_QUOTA_TIMEOUT = (<connect>, <read>)``、兩個
#:     整數字面值的形狀；改了形狀那支測試會紅（不會靜默失效），屆時連同它的 regex 一起改。
NAMESPACE_QUOTA_TIMEOUT = (3, 8)

#: #1513：operator ``upgrade-preflight``／upgrade 擴容被拒的 409 ``detail.error_code``
#: （extra/paas-operator ``upgrade_preflight.STORAGE_EXPANSION_ERROR_CODE``；改名兩邊一起改）。
STORAGE_EXPANSION_ERROR_CODE = 'storage_expansion_rejected'

#: #1552：operator 同時處理的 helm 作業已達上限、排隊逾時的 503 ``detail.error_code``
#: （extra/paas-operator ``helm_limiter.OPERATOR_BUSY_ERROR_CODE``；改名兩邊一起改）。
#: 回這個碼時什麼都沒套用，重送是安全的。
OPERATOR_BUSY_ERROR_CODE = 'operator_busy'

#: #1647：operator 升級預檢判定「工作區 compute 配額放不下這次變更」的 409 ``detail.error_code``
#: （extra/paas-operator ``compute_quota.COMPUTE_QUOTA_ERROR_CODE``；改名兩邊一起改）。
#: 回這個碼時什麼都沒套用。平台只認這個明說的 409——回應缺 ``compute_quota``、或它的
#: ``status`` 是 ``unknown``／``skipped``，都不是失敗訊號。
COMPUTE_QUOTA_ERROR_CODE = 'workspace_compute_quota_exceeded'

#: #1647：儲存設定（reconfigure）在請求內**同步**等 ``upgrade-preflight`` 的上限。前端這條 RPC
#: 的預算是 30 秒（``jsonRpc`` 預設），預檢之後還要寫 DB、回應；operator 端通常幾秒內回來
#: （一次 helm dry-run＋兩次 kubectl get）。逾時＝不知道＝照舊進背景 upgrade，行為與加入預檢前相同。
COMPUTE_QUOTA_SYNC_PREFLIGHT_TIMEOUT = 15


class PaaSOperatorError(Exception):
    """Base exception for PaaS Operator client errors."""

    def __init__(self, message: str, status_code: Optional[int] = None, detail: Optional[str] = None):
        self.message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(self.message)


class PaaSOperatorConnectionError(PaaSOperatorError):
    """Raised when connection to operator fails."""
    pass


class PaaSOperatorTimeoutError(PaaSOperatorError):
    """Raised when request times out."""
    pass


class PaaSOperatorAPIError(PaaSOperatorError):
    """Raised when API returns an error response."""
    pass


class PaaSOperatorClient:
    """HTTP client for PaaS Operator Service.

    This client wraps HTTP calls to the PaaS Operator FastAPI service,
    providing methods for namespace and Helm release management.

    Example:
        client = PaaSOperatorClient(
            base_url='http://paas-operator:8000',
            api_key='your-secret-key'
        )
        # Install a release
        release = client.install_release(
            namespace='paas-ws-demo',
            release_name='my-nginx',
            chart='nginx',
            repo_url='https://charts.bitnami.com/bitnami',
            version='15.0.0',
            values={'replicaCount': 2}
        )
    """

    def __init__(self, base_url: str, api_key: str):
        """Initialize the PaaS Operator client.

        Args:
            base_url: Base URL of the PaaS Operator service (e.g., 'http://paas-operator:8000')
            api_key: API key for authentication
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self._session = requests.Session()
        self._session.headers.update({
            'X-API-Key': api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Union[int, Tuple[int, int]] = DEFAULT_TIMEOUT,
        create_once: bool = False,
    ) -> Dict[str, Any]:
        """Make an HTTP request to the PaaS Operator API.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE)
            endpoint: API endpoint (e.g., '/api/releases')
            data: Request body data (for POST, PATCH)
            params: URL query string parameters
            timeout: 秒數；scalar 同時套用 connect/read，tuple 為 (connect, read)。
                requests 的 read timeout 是 inactivity 預算，不是整體 wall-clock 上限。
            create_once: Bare-create transport guard; no redirect replay, exact 201 only.

        Returns:
            Parsed JSON response

        Raises:
            PaaSOperatorConnectionError: If connection fails
            PaaSOperatorTimeoutError: If request times out
            PaaSOperatorAPIError: If API returns an error
        """
        # Bare create may already have happened before a redirect/error arrives.
        # Keep legacy transport behavior for all other callers.
        request_options = {"allow_redirects": False} if create_once else {}
        url = f"{self.base_url}{endpoint}"
        _logger.debug("PaaS Operator request: %s %s", method, url)

        try:
            response = self._session.request(
                method=method,
                url=url,
                json=data if data else None,
                params=params if params else None,
                timeout=timeout,
                **request_options,
            )

            # Log response status
            _logger.debug("PaaS Operator response: %s %s", response.status_code, endpoint)

            if create_once and response.status_code != 201:
                # Never infer absence from status, replay a redirect, or retain
                # a potentially sensitive Location/provider response body.
                raise PaaSOperatorAPIError(
                    message="Bare tunnel create outcome is unknown",
                    status_code=response.status_code,
                    detail="bare_create_outcome_unknown",
                )

            # Handle error responses
            if response.status_code >= 400:
                error_detail = self._parse_error(response)
                _logger.error(
                    "PaaS Operator API error: %s %s -> %s: %s",
                    method, endpoint, response.status_code, error_detail
                )
                raise PaaSOperatorAPIError(
                    message=f"API error: {error_detail}",
                    status_code=response.status_code,
                    detail=error_detail,
                )

            # Parse and return JSON response
            if response.content:
                return response.json()
            return {}

        except ConnectionError as e:
            _logger.error("PaaS Operator connection error: %s", str(e))
            raise PaaSOperatorConnectionError(
                message="Failed to connect to PaaS Operator service",
                detail=str(e),
            )
        except Timeout as e:
            _logger.error("PaaS Operator timeout: %s", str(e))
            raise PaaSOperatorTimeoutError(
                message="Request to PaaS Operator timed out",
                detail=str(e),
            )
        except RequestException as e:
            _logger.error("PaaS Operator request error: %s", str(e))
            raise PaaSOperatorError(
                message=f"Request error: {str(e)}",
                detail=str(e),
            )

    def _parse_error(self, response: requests.Response) -> str:
        """Parse error detail from response.

        Args:
            response: HTTP response object

        Returns:
            Error detail string
        """
        try:
            error_data = response.json()
            return error_data.get('detail', str(error_data))
        except (json.JSONDecodeError, ValueError):
            return response.text or f"HTTP {response.status_code}"

    # ==================== Health Check ====================

    def health_check(self) -> Dict[str, Any]:
        """Check if the PaaS Operator service is healthy.

        Returns:
            Health status with Helm version info

        Raises:
            PaaSOperatorError: If health check fails
        """
        return self._request('GET', '/health', timeout=10)

    # ==================== Namespace Operations ====================

    def create_namespace(
        self,
        namespace: str,
        cpu_limit: str = '2',
        memory_limit: str = '4Gi',
        storage_limit: str = '20Gi',
        workspace_id: Optional[int] = None,
        ephemeral_storage_request: Optional[str] = None,
        ephemeral_storage_limit: Optional[str] = None,
    ) -> Dict[str, str]:
        """Create a Kubernetes namespace with resource quota.

        Args:
            namespace: Namespace name (must start with 'paas-ws-')
            cpu_limit: CPU limit (e.g., '2' or '2000m')
            memory_limit: Memory limit (e.g., '4Gi')
            storage_limit: Storage limit (e.g., '20Gi')
            workspace_id: Owning CloudService's Odoo workspace id; the operator
                stamps it as the woow.tw/workspace-id NetworkPolicy label (#279).
                Omitted from the payload when None (backward compatible).

        Returns:
            Confirmation message

        Raises:
            PaaSOperatorError: If namespace creation fails
        """
        data = {
            'name': namespace,
            'cpu_limit': cpu_limit,
            'memory_limit': memory_limit,
            'storage_limit': storage_limit,
        }
        if workspace_id is not None:
            data['workspace_id'] = workspace_id
        # #1434：只在有值時送——舊 operator 的 pydantic model 會忽略未知欄位，但不送
        # 讓 payload 對「沒有 ephemeral 額度」的呼叫端維持原樣。
        if ephemeral_storage_request:
            data['ephemeral_storage_request'] = ephemeral_storage_request
        if ephemeral_storage_limit:
            data['ephemeral_storage_limit'] = ephemeral_storage_limit
        return self._request('POST', '/api/namespaces', data=data)

    def update_namespace_quota(
        self,
        namespace: str,
        cpu_limit: str,
        memory_limit: str,
        storage_limit: str,
        ephemeral_storage_request: Optional[str] = None,
        ephemeral_storage_limit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """#278 A1：PUT 整組 ns quota（值 = 存活 service 集重算）。

        Args:
            namespace: 目標命名空間（須以 'paas-ws-' 開頭）
            cpu_limit: requests.cpu / limits.cpu（字串 quantity，如 '30'）
            memory_limit: requests.memory / limits.memory（如 '30Gi'）
            storage_limit: requests.storage（如 '150Gi'）

        Returns:
            確認訊息
        """
        return self._request(
            'PUT',
            f'/api/namespaces/{namespace}/quota',
            data={
                'cpu_limit': cpu_limit,
                'memory_limit': memory_limit,
                'storage_limit': storage_limit,
                # #1434：見 create_namespace 同名欄位。
                **({'ephemeral_storage_request': ephemeral_storage_request}
                   if ephemeral_storage_request else {}),
                **({'ephemeral_storage_limit': ephemeral_storage_limit}
                   if ephemeral_storage_limit else {}),
            },
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def get_namespace_quota(self, namespace: str, *, include_managed: bool = False) -> Dict[str, Any]:
        """#1647：讀 namespace ResourceQuota 的上限與已佔用量（純唯讀）。

        Args:
            namespace: 目標命名空間（須以平台設定的 namespace 前綴開頭）

        Returns:
            operator 一律回 200：

            - 拿得到：``{'namespace', 'available': True, 'reason': None, 'quotas': [name],
              'resources': {<k8s 資源鍵>: {'hard', 'used', 'hard_value', 'used_value',
              'unit': 'cores'|'bytes'|'count', 'ratio', 'quota'}}}``
            - 拿不到：``{'namespace', 'available': False,
              'reason': 'quota_not_found'|'quota_unreadable', 'quotas': [], 'resources': {}}``

            轉成前台顯示用的形狀在 ``services/workspace_quota.build_view``。

        Raises:
            PaaSOperatorAPIError: 404／405＝operator 比這個端點舊（舊版只替同一路徑註冊
                PUT，GET 回 405）；400＝namespace 不在 operator 允許的前綴內。
            PaaSOperatorConnectionError / PaaSOperatorTimeoutError: 連不上或逾時
                （預算 ``NAMESPACE_QUOTA_TIMEOUT``）。

            本方法不吞例外——「沒有」「太舊」「失敗」怎麼對租戶呈現由呼叫端決定。

        Note:
            平台先於 operator 上線（舊 operator 對這個 GET 回 405／404）、或 operator
            連不上／逾時時，``_request`` 會在拋出例外之前各記一行 ERROR（例如
            ``PaaS Operator API error: GET /api/namespaces/<ns>/quota -> 405``），屬預期：
            工作區頁每讀一次配額就多一行，畫面則由呼叫端安靜降級（面板不出現，或顯示
            「暫時無法取得」）。其中 405／404 那一種不代表 operator 故障，只是它還沒換到
            有這個端點的版本。
        """
        return self._request('GET', f'/api/namespaces/{namespace}/quota',
                             **({'params': {'include_managed': True}} if include_managed else {}),
                             timeout=NAMESPACE_QUOTA_TIMEOUT)

    def backfill_baseline_netpol(
        self, namespace: str, workspace_id: int
    ) -> Dict[str, Any]:
        """#279 P5 backfill: apply the baseline NetworkPolicy bundle + re-stamp
        managed labels (paas-managed + env + workspace-id) on a PRE-EXISTING
        CloudService namespace created before O4/O1. Idempotent. (Agent-ns env
        re-stamp is hash-based on AgentRuntimeClient.restamp_labels — the operator
        derives the env-specific ns there.)"""
        return self._request(
            'POST', f'/api/namespaces/{namespace}/baseline-netpol',
            data={'workspace_id': workspace_id},
        )

    # ==================== Helm Release Operations ====================

    def install_release(
        self,
        namespace: str,
        release_name: str,
        chart: str,
        repo_url: Optional[str] = None,
        version: Optional[str] = None,
        values: Optional[Dict[str, Any]] = None,
        sensitive_values: Optional[Dict[str, Any]] = None,
        create_namespace: bool = False,
        expose: Optional[Dict[str, Any]] = None,
        tenant_secret: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Install a new Helm release.

        Args:
            namespace: Target namespace
            release_name: Name for the release
            chart: Chart reference (name or path)
            repo_url: Helm repository URL (optional, for adding repo)
            version: Chart version (optional)
            values: Helm values override
            sensitive_values: Sensitive values merged in memory only
            create_namespace: Whether to create namespace if not exists
            expose: Cloudflare Tunnel expose configuration
                    e.g., {'enabled': True, 'subdomain': 'myapp', 'service_port': 8080}
            tenant_secret: INFRA-FIX-019-1——``{name, data}``，operator 在 helm 之前寫進
                ``<release>-tenant-secret``（helm release 之外）。不 log。

        Returns:
            Release information

        Raises:
            PaaSOperatorError: If installation fails
        """
        # Build chart reference with repo URL if provided
        chart_ref = chart
        if repo_url:
            # The operator expects just the chart name;
            # repo configuration should be handled separately
            # For now, we pass the chart name as-is
            chart_ref = chart

        data = {
            'namespace': namespace,
            'name': release_name,
            'chart': chart_ref,
            'create_namespace': create_namespace,
        }
        if version:
            data['version'] = version
        if values:
            data['values'] = values
        if sensitive_values:
            data['sensitive_values'] = sensitive_values
        if tenant_secret:
            data['tenant_secret'] = tenant_secret
        if expose:
            data['expose'] = expose
            _logger.info("install_release: Adding expose config to request: %s", expose)
        else:
            _logger.info("install_release: No expose config provided")

        _logger.debug(
            "install_release: request namespace=%s release=%s chart=%s values_keys=%s sensitive_keys=%s",
            namespace,
            release_name,
            chart,
            sorted((values or {}).keys()),
            sorted((sensitive_values or {}).keys()),
        )

        return self._request(
            'POST',
            '/api/releases',
            data=data,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def get_release(self, namespace: str, release_name: str) -> Dict[str, Any]:
        """Get information about a Helm release.

        Args:
            namespace: Release namespace
            release_name: Release name

        Returns:
            Release information

        Raises:
            PaaSOperatorError: If release not found or access denied
        """
        return self._request(
            'GET',
            f'/api/releases/{namespace}/{release_name}',
        )

    def upgrade_release(
        self,
        namespace: str,
        release_name: str,
        values: Optional[Dict[str, Any]] = None,
        sensitive_values: Optional[Dict[str, Any]] = None,
        version: Optional[str] = None,
        chart: Optional[str] = None,
        reset_values: bool = False,
        reuse_values: bool = False,
        check_compute_quota: bool = False,
        tenant_secret: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Upgrade an existing Helm release.

        #411: ``reuse_values`` 預設由 True 翻成 False。--reuse-values 會拿舊
        release 的 coalesced values 當 render base，chart 升版新增的預設值永不
        生效（既存 release 升級靜默漏新預設，Gap 2 bug class）。平台呼叫端一律
        傳全量 values ＋ ``reset_values=True``，讓 chart values.yaml 為 base、
        全量 values 疊上，新預設自然補位。見 docs/decisions/patterns/
        helm-reset-values-unified-pipeline.md。

        Args:
            namespace: Release namespace
            release_name: Release name
            values: New Helm values override
            version: New chart version
            chart: New chart reference (if changing)
            reset_values: Reset values to chart defaults（平台升級路徑用 True）
            reuse_values: Reuse last release values（危險預設已翻 False）
            check_compute_quota: #1647——請 operator 在套用前檢查工作區的 compute 配額放不放
                得下；確定放不下回 409（``detail.error_code``＝``COMPUTE_QUOTA_ERROR_CODE``）、
                什麼都沒套用。只在 True 時送這一鍵（舊 operator 會忽略未知欄位）。
            tenant_secret: INFRA-FIX-019-1——``{name, data}``，operator 預檢通過後、helm 之前
                寫進 ``<release>-tenant-secret``。有值才送。⚠ operator 必須先發版：舊 operator
                忽略本欄、Secret 不會建立，chart 讀不到 token（sidecar 閒置）。

        Returns:
            Updated release information

        Raises:
            PaaSOperatorError: If upgrade fails
        """
        data = {
            'reset_values': reset_values,
            'reuse_values': reuse_values,
        }
        if chart:
            data['chart'] = chart
        if version:
            data['version'] = version
        if values:
            data['values'] = values
        if sensitive_values:
            data['sensitive_values'] = sensitive_values
        if check_compute_quota:
            data['check_compute_quota'] = True
        if tenant_secret:
            data['tenant_secret'] = tenant_secret

        return self._request(
            'PATCH',
            f'/api/releases/{namespace}/{release_name}',
            data=data,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def upgrade_preflight(
        self,
        namespace: str,
        release_name: str,
        values: Optional[Dict[str, Any]] = None,
        sensitive_values: Optional[Dict[str, Any]] = None,
        version: Optional[str] = None,
        chart: Optional[str] = None,
        reset_values: bool = False,
        reuse_values: bool = False,
        check_compute_quota: bool = False,
        pending_quota: Optional[Dict[str, Any]] = None,
        timeout: Optional[Union[int, Tuple[int, int]]] = None,
        tenant_secret: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """#1513：用與 ``upgrade_release`` **同一組**參數跑 operator 的升級預檢，不套用任何東西。

        operator 會 dry-run 渲染、比對不可變欄位（#1407），並對每顆要變大的 PVC 送
        ``kubectl patch --dry-run=server``，由 Longhorn validator 判斷擴得了嗎。

        #1647：``check_compute_quota`` 為 True 時另外檢查工作區的 compute 配額（同
        ``upgrade_release``）。``pending_quota``（鍵同 ``update_namespace_quota`` 的參數）是
        平台在這次檢查之後才會推送的 quota——換方案扣款前帶上，operator 以它取代現行上限
        來判斷。``timeout`` 不給＝``HELM_OPERATION_TIMEOUT``。

        INFRA-FIX-019-1：``tenant_secret`` 只是為了與 ``upgrade_release`` 收同一組參數（#1513）；
        預檢不寫入，所以**不送**——憑證不必多走一趟。

        Returns:
            ``{"ok": True, "expansions": [...]}``；帶 ``check_compute_quota`` 且 operator 夠新時
            多一鍵 ``compute_quota``（``status``＝``ok``／``skipped``／``unknown``）。⚠ 缺這一鍵、
            或 ``status`` 不是 ``ok``，都**不是**失敗訊號——放不下只會以 409 表達。

        Raises:
            PaaSOperatorAPIError: 409（擴容被拒的 ``detail`` 是 dict，``error_code``＝
                ``storage_expansion_rejected``；compute 配額放不下也是 dict，``error_code``＝
                ``COMPUTE_QUOTA_ERROR_CODE``；不可變欄位衝突是字串）；舊版 operator 沒有這個
                端點時 404。
        """
        data = {
            'reset_values': reset_values,
            'reuse_values': reuse_values,
        }
        if chart:
            data['chart'] = chart
        if version:
            data['version'] = version
        if values:
            data['values'] = values
        if sensitive_values:
            data['sensitive_values'] = sensitive_values
        if check_compute_quota:
            data['check_compute_quota'] = True
        if pending_quota:
            data['pending_quota'] = pending_quota
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/upgrade-preflight',
            data=data,
            timeout=HELM_OPERATION_TIMEOUT if timeout is None else timeout,
        )

    def reset_release_config(
        self,
        namespace: str,
        release_name: str,
        values: Optional[Dict[str, Any]] = None,
        sensitive_values: Optional[Dict[str, Any]] = None,
        chart: Optional[str] = None,
        version: Optional[str] = None,
        reset_values: bool = False,
        reuse_values: bool = False,
        admin_password_carrier: str = 'config_json',
        tenant_secret: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """#357/#449 reset admin_password；載體決定 operator 走哪條路。

        ``admin_password_carrier``（來自 ``template.admin_password_carrier``）：

        - ``config_json``（mcp-server 家族）：operator read-modify-write
          ``/data/config.json`` 只替換 ``admin_password`` leaf（保留
          connection/tools/mcp_server.env/token_history），helm upgrade 對齊
          Secret，rollout restart 讓 ConfigStore cache 從新檔讀。比「砍整份 +
          re-seed」少一次完整 PVC 重置。
        - ``helm_secret``（hermes-agent / open-design）：chart Secret 即權威來源、
          PVC 內沒有 config.json；operator 走純 helm upgrade ＋ **全部** 同 instance
          label 的 Deployment rollout restart（那些 chart 的 Deployment 沒有
          checksum/secret annotation，不重啟＝新密碼靜默不生效）。#449 之前這條路
          被當成 config_json 處理，對不存在的檔 exec patch → 必 500。

        #411: ``reuse_values`` 預設由 True 翻成 False（--reuse-values 會讓 chart 預設
        漏掉，與 ``upgrade_release`` 同一 bug class）。平台呼叫端傳全量 values ＋
        ``reset_values=True``。chart 版本＝**服務現在跑的版本**（F-461-2，2026-09-29）：
        reset 不再夾帶 template 的新 chart——升版會改 pod template，operator 的
        15 秒 ``--atomic`` 等不到新 pod 必定回退。升版由 deploy／redeploy 負責。

        ``reset_values`` 已於 #449 在 operator 端接通（``ReleaseResetConfigRequest``
        有此 field，handler 轉給 ``helm_service.upgrade``），送過去會渲染成顯式
        ``--reset-values``。接通前它被 pydantic 忽略、實際靠 ``reuse_values=False``
        讓 helm 走「無-flag 預設」（chart values.yaml 為 base ≈ reset）；兩者對
        helm 3 的語意相同，故對 operator 舊版部署仍向後相容。見 decision doc
        ``docs/decisions/patterns/helm-reset-values-unified-pipeline.md``。

        Args:
            namespace: helm release namespace
            release_name: helm release name
            values: 新 values（非敏感；平台傳全量組裝結果）
            sensitive_values: 新敏感值（須含 config.sensitive.admin_password）
            chart: chart ref（如需升版；通常省略）
            version: chart version（如需升版）
            reset_values: reset 到 chart 預設（平台 reset 路徑用 True）
            reuse_values: 複用既有 release values（危險預設已翻 False）
            admin_password_carrier: ``config_json`` | ``helm_secret``。舊 operator
                不認得本欄會靜默忽略（＝退回 config_json 既有行為），故
                **operator 必須先發版、平台後發版**。

        #461：operator 兩條路都只「下達」rollout restart、**不等它完成**（回應含
        ``rollout_awaited: False``）。所以 2xx 的語意由「密碼已生效」收斂成
        「密碼已是叢集權威值、且不再自動回退」——這正是 C11′ 的提交點，見
        ``models/cloud_service.py::_reset_admin_password``。服務何時恢復由狀態機
        表達（成功後 stamp ``upgrading`` → 背景 poll cron → ``running``；前端輪詢只讀 DB）。

        Returns:
            Operator 回應 dict（含 helm_revision、config_json_patched、helm_upgraded、
            rollout_awaited、admin_password_carrier；helm_secret 路另含
            restarted_deployments）

        Raises:
            PaaSOperatorError: 任一子步失敗
        """
        data: Dict[str, Any] = {
            'reset_values': reset_values,
            'reuse_values': reuse_values,
            'admin_password_carrier': admin_password_carrier,
        }
        if values:
            data['values'] = values
        if sensitive_values:
            data['sensitive_values'] = sensitive_values
        if chart:
            data['chart'] = chart
        if version:
            data['version'] = version
        # INFRA-FIX-019-1：reset 的 values 帶 tenantSecret 參照；Secret 同步寫一次（內容不變＝無變化）。
        if tenant_secret:
            data['tenant_secret'] = tenant_secret

        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/reset-config',
            data=data,
            # #461：專屬預算，見模組頂端 RESET_CONFIG_TIMEOUT 的階梯說明。
            timeout=RESET_CONFIG_TIMEOUT,
        )

    def uninstall_release(
        self,
        namespace: str,
        release_name: str,
        subdomain: Optional[str] = None,
        grace_period_days: Optional[int] = None,
    ) -> Dict[str, str]:
        """Uninstall a Helm release.

        Args:
            namespace: Release namespace
            release_name: Release name
            subdomain: Optional subdomain to delete from Cloudflare
            grace_period_days: #1431 — keep the release's orphaned PVCs / DataVolumes
                for only this many days (the platform sends 1 for a service that never
                reached running). The operator honours ``1..PVC_GRACE_PERIOD_DAYS`` and
                falls back to its default otherwise; operators older than #1431 ignore it.

        Returns:
            Deletion confirmation

        Raises:
            PaaSOperatorError: If uninstallation fails
        """
        params = {}
        if subdomain:
            params['subdomain'] = subdomain
        if grace_period_days:
            params['grace_period_days'] = int(grace_period_days)
        return self._request(
            'DELETE',
            f'/api/releases/{namespace}/{release_name}',
            params=params or None,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def scale_release(
        self,
        release_name: str,
        namespace: str,
        replicas: int = 0,
    ) -> Dict[str, str]:
        """Scale a Helm release's deployments.

        Used for subscription suspend (replicas=0) and resume (replicas=1).

        Args:
            release_name: Release name
            namespace: Release namespace
            replicas: Target replica count

        Returns:
            Scale confirmation

        Raises:
            PaaSOperatorError: If scaling fails
        """
        # Operator reads `replicas` as a Query parameter, not request body.
        # Send via URL query string to match the server-side contract.
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/scale',
            params={'replicas': replicas},
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def park_release_route(
        self,
        namespace: str,
        release_name: str,
        subdomain: str,
    ) -> Dict[str, Any]:
        """#1563：把服務的對外 hostname（所有規則）指向共用的停權說明頁。

        說明頁的位址由 operator 自己的設定決定（不由平台傳入）。200 的 ``status``：
        ``parked``／``already_parked``／``not_found``（tunnel 上沒有這個 hostname）／
        ``disabled``（operator 沒開 Cloudflare 整合）。拒絕一律 4xx／5xx＋
        ``detail.error_code``，operator 刻意不回 404 ⇒ **404＝沒有這個端點的舊 operator**。
        """
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/route/park',
            data={'subdomain': subdomain},
            timeout=ROUTE_PARK_TIMEOUT,
        )

    def unpark_release_route(
        self,
        namespace: str,
        release_name: str,
        expose: Dict[str, Any],
    ) -> Dict[str, Any]:
        """#1563：把指向停權說明頁的規則改回服務。

        ``expose`` 傳安裝時那一份（``CloudService._build_expose_config``）：operator 用
        安裝時同一段邏輯重算 service_url，平台不必存 URL。200 的 ``status``：
        ``unparked``／``not_parked``（沒有指向說明頁的規則）／``not_found``／
        ``disabled``。409 ``route_target_unresolved``＝推不出要指回的 service（仍是說明頁）。
        同 park，**404＝沒有這個端點的舊 operator**（它不可能 park 過任何東西）。
        """
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/route/unpark',
            data={'expose': expose},
            timeout=ROUTE_PARK_TIMEOUT,
        )

    # ==================== #1540 限時唯讀資料取回入口 ====================

    def open_data_retrieval(
        self,
        namespace: str,
        release_name: str,
        profile: str,
        ttl_hours: int,
    ) -> Dict[str, Any]:
        """#1540：開一個限時唯讀取回入口（201）。

        回應帶 ``hostname``／``url``／``username``／**``password``（只有這一次，operator 不存）**／
        ``expires_at``／``data_expires_at``。呼叫端不得把回應寫進 log 或 DB。拒絕一律
        4xx／5xx＋``detail.error_code``；operator 刻意不回 404 ⇒ **404＝沒有這組端點的舊 operator**。
        """
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/retrieval',
            data={'profile': profile, 'ttl_hours': int(ttl_hours)},
            timeout=DATA_RETRIEVAL_TIMEOUT,
        )

    def get_data_retrieval(self, namespace: str, release_name: str) -> Dict[str, Any]:
        """#1540：入口狀態（``active``／``starting``／``stopped``／``not_found``）。不含密碼。"""
        return self._request(
            'GET',
            f'/api/releases/{namespace}/{release_name}/retrieval',
            timeout=DEFAULT_TIMEOUT,
        )

    def close_data_retrieval(
        self,
        namespace: str,
        release_name: str,
        subdomain: Optional[str] = None,
    ) -> Dict[str, Any]:
        """#1540：關入口（冪等）。``subdomain`` 是開的時候記下的 hostname 第一段，只在 k8s 物件
        已不在、route 還沒刪成時讓 operator 補刪。502 ``route_cleanup_failed``＝pod 已停、route 待重試。"""
        params = {'subdomain': subdomain} if subdomain else None
        return self._request(
            'DELETE',
            f'/api/releases/{namespace}/{release_name}/retrieval',
            params=params,
            timeout=DATA_RETRIEVAL_TIMEOUT,
        )

    def sweep_data_retrievals(self) -> Dict[str, Any]:
        """#1540：請 operator 關掉所有已到期的入口（平台記錄漏掉的也會收）。"""
        return self._request('POST', '/api/retrievals/sweep', timeout=HELM_OPERATION_TIMEOUT)

    def restart_release(
        self,
        namespace: str,
        release_name: str,
    ) -> Dict[str, Any]:
        """#1432：rollout restart release 下所有 Deployment 與 StatefulSet。

        operator 只「下達」不等待（回應 ``rollout_awaited: False``）；收斂交給平台
        狀態機（呼叫端把服務轉 ``upgrading``，poll 轉回 ``running``）。PVC、Secret、
        values 都不動。沒有任何 workload 時 operator 回 404。
        """
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/restart',
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def get_config_projection(self, namespace, release_name, revision, paths, excluded_paths=None):
        """固定 revision 的公開設定標量；不取得或回傳 raw Helm values。"""
        return self._request('POST',
            f'/api/releases/{namespace}/{release_name}/config-projection',
            data={'revision': revision, 'paths': list(paths),
                  'excluded_paths': list(excluded_paths or [])}, timeout=DEFAULT_TIMEOUT)

    def rollback_release(
        self,
        namespace: str,
        release_name: str,
        revision: int = 0,
        chart: Optional[str] = None,
        preserve_value_paths: Optional[List[str]] = None,
        check_compute_quota: bool = False,
    ) -> Dict[str, Any]:
        """Rollback a Helm release to a previous revision.

        #1433：帶 ``preserve_value_paths``（必須同時帶 ``chart``）時 operator 改走
        「目標 revision 的 values ＋ 目前 revision 的這些路徑 ＋ 目標 chart 版本」的
        helm upgrade，憑證（``config.sensitive`` 等）維持目前的值，不會跟著回到舊版。
        回應多了 ``mode``／``revision``／``chart_version``。不帶＝純 helm rollback。

        Args:
            namespace: Release namespace
            release_name: Release name
            revision: Target revision (0 = previous)
            chart: Chart reference (required with preserve_value_paths)
            preserve_value_paths: Dot-paths whose current values must survive
            check_compute_quota: #1647——同 ``upgrade_release``。兩種回復 operator 都檢查：
                保留憑證的回復算那次 helm upgrade 的 dry-run，純 helm rollback 算目標 revision
                的 stored manifest（#1685；更早的 operator 在純 rollback 收到旗標也不檢查）。

        Returns:
            Rollback confirmation

        Raises:
            PaaSOperatorError: If rollback fails
        """
        data: Dict[str, Any] = {'revision': revision}
        if preserve_value_paths:
            data['chart'] = chart
            data['preserve_value_paths'] = list(preserve_value_paths)
        if check_compute_quota:
            data['check_compute_quota'] = True
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/rollback',
            data=data,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def get_revisions(self, namespace: str, release_name: str) -> Dict[str, Any]:
        """Get revision history of a Helm release.

        Args:
            namespace: Release namespace
            release_name: Release name

        Returns:
            List of revisions

        Raises:
            PaaSOperatorError: If history retrieval fails
        """
        return self._request(
            'GET',
            f'/api/releases/{namespace}/{release_name}/revisions',
        )

    def patch_sidecar(
        self,
        namespace: str,
        release_name: str,
        sidecar_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Patch a release's deployment with a sidecar container.

        After patching, the operator also creates a ClusterIP K8s Service
        and a Cloudflare Tunnel route for the sidecar, making its MCP
        endpoint externally reachable.

        Args:
            namespace: Release namespace
            release_name: Release name
            sidecar_config: Sidecar container specification dict containing
                'container' key with name, image, ports, env, resources, probes

        Returns:
            Patch confirmation with deployment and container info.
            Includes 'mcp_endpoint_url' (public URL), 'mcp_internal_url'
            (K8s internal URL), and 'mcp_service_name' if service was created.

        Raises:
            PaaSOperatorError: If patching fails
        """
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/sidecar',
            data=sidecar_config,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def init_n8n(
        self,
        namespace: str,
        release_name: str,
        owner_email: str,
        owner_password: str,
        owner_confirmed: bool = False,
    ) -> Dict[str, Any]:
        """Initialize n8n instance after deployment.

        Triggers owner setup and API key generation on the PaaS Operator.

        Args:
            namespace: K8s namespace
            release_name: Helm release name
            owner_email: Email for n8n owner user
            owner_password: Password for n8n owner user
            owner_confirmed: #1534：平台曾對這台服務 init 成功過。operator 在 owner 已存在時
                不再登入；這個值為 False 時它會要求既有 owner 的 Email 等於 ``owner_email``。
                舊 operator 忽略未知欄位。

        Returns:
            Dict with success, api_key, owner_email

        Raises:
            PaaSOperatorError: If initialization fails
        """
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/init/n8n',
            data={
                'owner_email': owner_email,
                'owner_password': owner_password,
                'owner_confirmed': bool(owner_confirmed),
            },
            timeout=(INIT_N8N_CONNECT_TIMEOUT, INIT_N8N_READ_TIMEOUT),
        )

    def init_odoo(
        self,
        namespace: str,
        release_name: str,
    ) -> Dict[str, Any]:
        """Initialize Odoo module list after deployment.

        Triggers module list update so newly deployed addons are discoverable
        in the Odoo Apps interface without manual intervention.

        Args:
            namespace: K8s namespace
            release_name: Helm release name

        Returns:
            Dict with success, modules_added, modules_updated

        Raises:
            PaaSOperatorError: If initialization fails
        """
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/init/odoo',
            timeout=(INIT_ODOO_CONNECT_TIMEOUT, INIT_ODOO_READ_TIMEOUT),
        )

    def reset_odoo_admin_password(
        self,
        namespace: str,
        release_name: str,
        new_password: str,
    ) -> Dict[str, Any]:
        """#1370：重設租戶 Odoo 的 base.user_admin 密碼（login 不變）並清掉 2FA。

        operator 把新密碼經 stdin 交給容器內的 ORM 腳本，直接改租戶 DB；不 helm
        upgrade、不重啟。業務失敗回 200＋``success: false``（``error_kind``／
        ``error_code`` 見 operator 的 ``AppAdminPasswordResetResponse``）。

        ⛔ ``new_password`` 只在 request body；本 client 的 log 只記 method／endpoint。
        """
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/odoo/admin-password',
            data={'new_password': new_password},
            timeout=APP_ADMIN_RESET_TIMEOUT,
        )

    def reset_n8n_owner_password(
        self,
        namespace: str,
        release_name: str,
        new_password: str,
    ) -> Dict[str, Any]:
        """#1370：重設租戶 n8n 的 owner 密碼（email 不變）並關掉 MFA。

        語意同 ``reset_odoo_admin_password``；n8n 的 API key 與 MCP sidecar 不受影響。
        """
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/n8n/owner-password',
            data={'new_password': new_password},
            timeout=APP_ADMIN_RESET_TIMEOUT,
        )

    def reset_omnigent_admin_password(
        self,
        namespace: str,
        release_name: str,
        new_password: str,
    ) -> Dict[str, Any]:
        """#1370 後續：重設租戶 Omnigent 的 admin 密碼（帳號不變）。

        operator exec 進 omnigent server 容器，用 omnigent 自己的 AccountStore 寫入（與 app
        內 Members → Reset 同一條）；不重啟。與上游一致：已登入的 session、已配對的 host
        都不受影響。語意與錯誤分類同 ``reset_odoo_admin_password``。
        """
        return self._request(
            'POST',
            f'/api/releases/{namespace}/{release_name}/omnigent/admin-password',
            data={'new_password': new_password},
            timeout=APP_ADMIN_RESET_TIMEOUT,
        )

    def get_odoo_module_states(
        self,
        namespace: str,
        release_name: str,
        module_names: list[str],
    ) -> Dict[str, Any]:
        """Query Odoo module installation states.

        Args:
            namespace: K8s namespace
            release_name: Helm release name
            module_names: Module technical names to query

        Returns:
            Dict with 'success' and 'modules' mapping name -> {state, dependents}
        """
        return self._request(
            'POST',
            f'/api/odoo/{namespace}/{release_name}/module-states',
            data={'module_names': module_names},
            timeout=150,
        )

    def get_resource_observations(
        self, namespace: str, release_names: List[str],
    ) -> Dict[str, Any]:
        """Observe a server-resolved namespace batch without changing releases.

        This deliberately does not use the Helm operation budget. The caller
        also bounds the complete batch wait when a workspace spans namespaces.
        """
        return self._request(
            'POST', f'/api/namespaces/{namespace}/resource-observations',
            data={'release_names': release_names},
            timeout=(2, RESOURCE_OBSERVATION_TIMEOUT),
        )

    def get_status(
        self,
        namespace: str,
        release_name: str,
        include_vm: bool = False,
        include_workloads: bool = False,
    ) -> Dict[str, Any]:
        """Get detailed status of a release including pod information.

        Args:
            namespace: Release namespace
            release_name: Release name
            include_vm: 也取回 KubeVirt VMI 與 CDI DataVolume 狀態（#423 W5a）。

                ⚠ VM 型服務**必須**帶 True。operator 在 include_vm=False 時
                回 ``vmis: None``（＝未查詢，與「查了但沒有」的 ``[]`` 不同），
                而平台對 VM 的就緒判定是 fail-closed —— 漏帶這個旗標會讓
                VM 服務永遠停在 ``deploying``。回應的 ``vm_included`` 欄位
                是 operator 對本旗標的 echo，可用來斷言確實有帶到。
            include_workloads: 也取回 Deployment／StatefulSet 的副本數與「建不出 Pod」
                的證據（#1647）。回應多 ``workloads_included``（operator 對本旗標的
                echo）、``workloads``、``workload_retrieval_error``。

                ⚠ 舊 operator 會**忽略**這個參數（FastAPI 不認得的 query 直接略過），
                回應裡**沒有**這三個 key。呼叫端必須把缺席當成「不支援」——不是
                「沒有 workload」，更不是「健康」（判定見
                ``services/workload_health.py`` 的 ``workload_verdict``）。

        Returns:
            Release and pod status（``include_vm=True`` 時另含 ``vm_included`` /
            ``vmis`` / ``datavolumes`` / ``vmi_retrieval_error`` /
            ``datavolume_retrieval_error``；``include_workloads=True`` 時另含
            ``workloads_included`` / ``workloads`` / ``workload_retrieval_error``）

        Raises:
            PaaSOperatorError: If status retrieval fails
        """
        # 兩個旗標各自為真才放；都沒有時 params=None，與加旗標之前的 wire 完全相同。
        params = {}
        if include_vm:
            params['include_vm'] = 'true'
        if include_workloads:
            params['include_workloads'] = 'true'
        return self._request(
            'GET',
            f'/api/releases/{namespace}/{release_name}/status',
            params=params or None,
        )


    def get_public_address(self, namespace: str, release_name: str) -> Dict[str, Any]:
        """本服務的對外位址（由 chart 的 ngrok sidecar 廣告：EMQX 的 tcp://、Headscale 的 https://）。

        只有 `cloud_app_template.has_public_tunnel_address` 的商品需要問；呼叫端的
        閘門是 `CloudService._chart_has_public_tunnel_address`，不要在這裡自建判準。

        舊 operator（還沒有 ``/public-address``，回 404）時退回舊端點
        ``/mqtt-address``：那支只回 ``tcp://``，EMQX 照常顯示，Headscale 的 https
        位址要等 operator 升級後才看得到。部署順序本來就是 operator 先上
        （見 PR 說明），這個退路只是讓「平台先上了」不至於讓 EMQX 的位址整格消失。

        Args:
            namespace: Release namespace
            release_name: Release name

        Returns:
            ``{'public_url': 'tcp://host:port' | 'https://host'}``，或
            ``{'public_url': None}`` —— 後者是**正常狀態**（租戶沒開、agent 還沒連上、
            sidecar 撤回、pod 還沒起來），不是失敗，呼叫端不得把它當錯誤處理。

        Raises:
            PaaSOperatorError: operator 本身失敗（kubectl 掛了、namespace 越界）
        """
        try:
            return self._request(
                'GET',
                f'/api/releases/{namespace}/{release_name}/public-address',
            )
        except PaaSOperatorAPIError as e:
            if e.status_code != 404:
                raise
            return self._request(
                'GET',
                f'/api/releases/{namespace}/{release_name}/mqtt-address',
            )


    # ==================== Tunnel Operations ====================

    def get_tunnel_provider_binding(self) -> Dict[str, Any]:
        """Read non-secret provider account binding for the SHC prototype."""
        return self._request('GET', '/api/tunnels/provider-binding')

    def create_bare_tunnel(
        self, name: str, expected_account_id: str,
    ) -> Dict[str, Any]:
        """Create only. Caller must durably anchor before later mutations."""
        return self._request(
            'POST', '/api/tunnels/bare',
            data={'name': name, 'expected_account_id': expected_account_id},
            timeout=HELM_OPERATION_TIMEOUT,
            create_once=True,
        )

    def create_tunnel(
        self,
        name: str,
        hostname: Optional[str] = None,
        service_url: str = 'http://localhost:8123',
    ) -> Dict[str, Any]:
        """Create a dedicated Cloudflare Tunnel.

        Args:
            name: Tunnel name
            hostname: Public hostname (e.g., 'sh-myworkspace.woowtech.io').
                Omit to create a BARE tunnel (catch-all 404 ingress, no DNS) —
                used by Security Access where routes are added on demand.
            service_url: Local service URL to tunnel to (default: HA port)

        Returns:
            Tunnel info including tunnel_id, tunnel_name, tunnel_token, hostname

        Raises:
            PaaSOperatorError: If tunnel creation fails
        """
        data: Dict[str, Any] = {
            'name': name,
            'service_url': service_url,
        }
        # Only send hostname when provided; absence signals a bare tunnel.
        # Smart Home always passes one, so its behaviour is unchanged.
        if hostname:
            data['hostname'] = hostname
        return self._request(
            'POST',
            '/api/tunnels',
            data=data,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def get_tunnel_status(self, tunnel_id: str) -> Dict[str, Any]:
        """Get tunnel connection status.

        Args:
            tunnel_id: Cloudflare Tunnel ID

        Returns:
            Tunnel status including connections info

        Raises:
            PaaSOperatorError: If status retrieval fails
        """
        return self._request('GET', f'/api/tunnels/{tunnel_id}')

    def get_hostname_traffic(self, hostnames, window_hours: int = 24) -> Dict[str, Any]:
        """#1234：各 hostname 的請求數與最後請求時間（Cloudflare zone analytics）。

        operator 一律回 200 + ``available``／``reason``（缺權限是設定狀態、不是
        錯誤）；較舊的 operator 沒有這個端點會回 404，由呼叫端處理。
        """
        return self._request(
            'POST', '/api/analytics/traffic',
            data={'hostnames': list(hostnames), 'window_hours': window_hours},
            timeout=25,
        )

    def get_tunnel_token(self, tunnel_id: str) -> Dict[str, Any]:
        """Get tunnel token for cloudflared.

        Args:
            tunnel_id: Cloudflare Tunnel ID

        Returns:
            Dict with tunnel_id and token

        Raises:
            PaaSOperatorError: If token retrieval fails
        """
        return self._request('GET', f'/api/tunnels/{tunnel_id}/token')

    def delete_tunnel(
        self,
        tunnel_id: str,
        tunnel_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Delete a dedicated Cloudflare Tunnel.

        Args:
            tunnel_id: Cloudflare Tunnel ID
            tunnel_name: #531 歸屬主張 —— 呼叫端自己那一列存的 CF tunnel 名。
                operator 會拿它跟該 tunnel 在 Cloudflare 上的實際 name 比對，
                對不上就拒。**走 query param 不走 body**：這條端點今天沒有 body，
                而帶 body 的 DELETE 最容易被中間層默默丟掉，丟掉後會靜默退化成
                「沒帶 claim」；query param 不會。tunnel name 非機密，進 access
                log 可接受。

        Returns:
            Deletion confirmation

        Raises:
            PaaSOperatorError: If deletion fails
        """
        params = {'tunnel_name': tunnel_name} if tunnel_name else None
        return self._request(
            'DELETE',
            f'/api/tunnels/{tunnel_id}',
            params=params,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def configure_tunnel(
        self,
        tunnel_id: str,
        hostname: str,
        service_url: str,
        tunnel_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a tunnel's ingress configuration.

        Used for subscription suspend (service_url="http_status:503")
        and resume (service_url="http://localhost:PORT").

        Args:
            tunnel_id: Cloudflare Tunnel ID
            hostname: Public hostname (e.g., ha-xxx.woowtech.io)
            service_url: Target service URL or http_status:503
            tunnel_name: #531 歸屬主張（見 ``delete_tunnel``）。這條端點走 body。

        Returns:
            Configuration confirmation

        Raises:
            PaaSOperatorError: If configuration fails
        """
        data = {
            'hostname': hostname,
            'service_url': service_url,
        }
        if tunnel_name:
            data['tunnel_name'] = tunnel_name
        return self._request(
            'PUT',
            f'/api/tunnels/{tunnel_id}/config',
            data=data,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def sync_tunnel_routes(
        self,
        tunnel_id: str,
        routes: List[Dict[str, Any]],
        comment: Optional[str] = None,
        tunnel_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Declaratively sync a dedicated tunnel's full route list.

        Replaces the tunnel ingress (plus catch-all 404) and reconciles DNS
        records. Idempotent — Security Access uses this for route CRUD,
        suspend (service_url='http_status:503') and resume.

        Args:
            tunnel_id: Cloudflare Tunnel ID
            routes: list of dicts {subdomain, service_url, http_host_header}
            comment: canonical name stamped on DNS records (traceability)
            tunnel_name: #531 歸屬主張（見 ``delete_tunnel``）。這條端點走 body。
                ⚠ **與 ``comment`` 是兩件事，不要合併**：``comment`` 是 DNS record
                上的自由文字 stamp（operator 端只寫不讀），``tunnel_name`` 是授權
                判準。兩者在 SA 上剛好都衍生自同一個 canonical_name，但把 stamp
                當判準會讓「改 stamp 格式」等於「改安全姿態」。

        Returns:
            Dict with tunnel_id and routes (each with dns_record_id)

        Raises:
            PaaSOperatorError: If the route sync fails
        """
        data: Dict[str, Any] = {'routes': routes}
        if comment:
            data['comment'] = comment
        if tunnel_name:
            data['tunnel_name'] = tunnel_name
        return self._request(
            'PUT',
            f'/api/tunnels/{tunnel_id}/routes',
            data=data,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def sync_access_app(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Declaratively sync one SA's Cloudflare Access config.

        payload keys: enabled, canonical_name, app_name, primary_hostname,
        protected_hostnames, session_duration, login_idp_types, policies,
        existing_app_id, existing_policy_ids. Returns {app_id, policy_ids,
        allowed_idps, unresolved_idp_types}. unresolved_idp_types lists
        requested login methods with no matching CF account IdP — a fail-open
        login-widening signal the caller persists (SA.access_unresolved_idp_types).
        Idempotent.
        """
        return self._request(
            'PUT', '/api/access/sync', data=payload,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def list_identity_providers(self) -> Dict[str, Any]:
        """List account Access IdPs ({identity_providers: [{id,name,type}]})."""
        return self._request('GET', '/api/access/identity-providers')

    def check_route_availability(self, subdomain: str) -> Dict[str, Any]:
        """Check Cloudflare-side whether a subdomain is free in the zone.

        Args:
            subdomain: subdomain prefix (without domain)

        Returns:
            Dict with:
              - ``subdomain``: the queried prefix
              - ``hostname``: the full FQDN that was checked
              - ``status``: one of ``free`` / ``taken`` / ``unknown``.
                ``unknown`` means the Cloudflare API errored (401/403/429/5xx)
                and availability could NOT be determined — it is NOT "free".
              - ``available``: ``bool | None``. ``True``/``False`` for
                ``free``/``taken``; ``None`` when ``status == 'unknown'``
                (the operator deliberately does NOT fail open).
              - ``reason``: present only on ``unknown``, a human message.

            Callers MUST branch on ``status`` and treat ``unknown`` /
            ``available is None`` as fail-closed (see
            controllers/security_access.py:_check_route_prefix). Do not treat
            ``available`` as a plain bool.

        Raises:
            PaaSOperatorError: If the request itself fails (transport / non-2xx
                outside the operator's handled ``unknown`` path).
        """
        return self._request(
            'GET',
            '/api/routes/availability',
            params={'subdomain': subdomain},
        )

    def get_reconcile_inventory(self) -> Dict[str, Any]:
        """GET /api/reconcile/inventory — operator 唯讀 CF 帳號級右集 inventory（L4，方向 A）。

        Returns:
            Dict：``{paas_env, partial, failed_kinds:[str], resources:[{id,name,kind}]}``。
            operator 對 CF 帳號級資源只做唯讀發現；差集 / 分類 / 刪除全在 Odoo。

        Raises:
            PaaSOperatorError（及子類）：404（operator 太舊、無此端點）/ 503（env-pin 未鎖
            為 prod|stg）/ 連線失敗 / timeout → ``_request`` fail-loud raise。**絕不**降級回空
            inventory —— 空 inventory 會被下游差集當『無孤兒』，與『沒掃到 / 沒權限』無法區分
            （B.0 / §機械守線）。
        """
        return self._request('GET', '/api/reconcile/inventory')

    def get_cluster_storage_capacity(self, storage_class: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/cluster/storage-capacity — 叢集儲存（Longhorn）排程額度（#1415，唯讀）。

        Args:
            storage_class: 以哪個 StorageClass 的 replica 數估「最大可放單顆 volume」；
                None＝叢集預設 SC。

        Returns:
            Dict：``{available, reason?, nodes:[{name, limit_bytes, scheduled_bytes,
            headroom_bytes, scheduled_ratio, ...}], largest_placeable_volume_bytes, replicas,
            orphaned_pvcs:{count, requested, longhorn_scheduled, ...}|None, ...}``。
            讀不到 Longhorn 時 operator 仍回 200、``available: false``。

        Raises:
            PaaSOperatorError（及子類）：404＝operator 太舊、沒有這個端點（呼叫端安靜略過）。
        """
        params = {'storage_class': storage_class} if storage_class else None
        return self._request('GET', '/api/cluster/storage-capacity', params=params)


def get_paas_operator_client(env) -> Optional[PaaSOperatorClient]:
    """Get a configured PaaS Operator client from Odoo settings.

    Args:
        env: Odoo environment (request.env or self.env)

    Returns:
        Configured PaaSOperatorClient or None if not configured
    """
    IrConfigParameter = env['ir.config_parameter'].sudo()

    base_url = IrConfigParameter.get_param('woow_paas_platform.operator_url', '')
    api_key = IrConfigParameter.get_param('woow_paas_platform.operator_api_key', '')

    if not base_url or not api_key:
        _logger.warning("PaaS Operator not configured. Set operator_url and operator_api_key in settings.")
        return None

    return PaaSOperatorClient(base_url=base_url, api_key=api_key)
