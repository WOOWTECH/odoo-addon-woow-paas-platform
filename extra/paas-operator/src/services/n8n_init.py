"""N8n post-deploy initialization service.

Handles automated owner setup and API key generation for newly deployed
n8n instances so MCP sidecar can connect immediately.

Uses Node.js (available in n8n container) for HTTP requests since wget
has encoding issues with n8n's body parser.

#1534：owner 已存在時**不再用平台存的 owner 密碼登入**——那組密碼在租戶於 n8n 介面改密碼
或開 MFA 之後就失效（401／998），以前 Redeploy 的 init 因此連續失敗、服務轉 error。改由
``n8n_scripts/ensure_api_key.js`` 在容器內直接沿用或簽發 init 用的 API key（見該檔說明）。
"""
import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional

from src.models.schemas import InitErrorKind
from src.services.helm import KubectlException, KubernetesService
from src.services.odoo_init import _last_json_object

logger = logging.getLogger(__name__)

# Retry configuration
MAX_READY_RETRIES = 15
READY_RETRY_DELAY = 4  # seconds


class N8nInitError(Exception):
    """Raised when n8n initialization fails.

    ``kind``（#1263）：失敗分類，值域見 ``schemas.InitErrorKind``。預設 ``unknown``
    ——exec 失敗（含 timeout）、輸出不可解析、200 但 body 不合預期，都不能證明
    n8n 端沒有產生副作用。
    """

    def __init__(self, message: str, step: str = "", kind: InitErrorKind = "unknown"):
        self.step = step
        self.kind = kind
        super().__init__(message)


#: init 建的 API key 名稱。n8n 2.x 不允許同名，重建前要先刪舊的（#1471）。
_API_KEY_LABEL = "mcp-sidecar"
#: 一次 init 最多刪幾把同名舊 key、每個請求的逾時（秒）。有上限才算得出 init 的最壞耗時
#: （scripts/audit/init_n8n_budget.py 會把 1 次列出＋本上限次刪除計入 envelope）。
_MAX_KEY_DELETES = 3
_KEY_PRUNE_REQUEST_TIMEOUT = 10
#: 與上面 POST /rest/api-keys 的 expiresAt 相同（2100-01-01），exec 路徑簽出的 key 一樣不實際過期。
_API_KEY_EXPIRES_AT = 4102444800

#: DATA-N8N-006-1：mcp-sidecar 的 API key 只授與 MCP 工具實際用得到的 scope（allowlist）。
#: 以前一律帶 ``GET /rest/api-keys/scopes`` 的全部 88 個，含 user:create／delete／changeRole、
#: role:manage、ldap／saml／oidc:manage、securitySettings:manage、communityPackage:install、
#: sourceControl:pull、eventBusDestination:*——sidecar 一旦被攻破（或 MCP 的工具被誘導）就等於
#: n8n 實例管理權。allowlist 而不是 denylist：n8n 將來新增的管理類 scope 預設不會被授與，
#: 需要時再明確加進來。
#:
#: 對應 MCP 工具（n8n-mcp 的 28 支）：workflow（含 activate／export／import／move）、execution、
#: credential、tag／workflowTags／executionTags、folder、variable、dataTable*、testRun（evaluations）、
#: securityAudit:generate（n8n_audit_instance）、insights:read、project:list（挑專案）。
#: ⚠ 與 ``n8n_scripts/ensure_api_key.js`` 的 ``SIDECAR_SCOPE_PREFIXES``／``SIDECAR_SCOPE_EXACT``
#: 必須相同（tests/test_n8n_sidecar_scopes_dn006.py 對帳）。
_SIDECAR_SCOPE_PREFIXES = (
    "workflow:", "workflowTags:", "execution:", "executionTags:", "credential:", "tag:",
    "folder:", "variable:", "dataTable", "testRun:", "insights:", "securityAudit:",
)
_SIDECAR_SCOPE_EXACT = frozenset({"project:list"})


def sidecar_scopes(available) -> list:
    """從 n8n 提供的 scope 清單挑出 mcp-sidecar 該有的（保留 n8n 的順序、去重）。"""
    seen, kept = set(), []
    for scope in available or []:
        if not isinstance(scope, str) or scope in seen:
            continue
        if scope in _SIDECAR_SCOPE_EXACT or scope.startswith(_SIDECAR_SCOPE_PREFIXES):
            seen.add(scope)
            kept.append(scope)
    return kept

#: #1534：owner 已存在時沿用／簽發 API key 的腳本。import 時就讀——檔案不見要在 operator 啟動
#: 當下就炸，不要等到 Redeploy 才發現。
ENSURE_API_KEY_SCRIPT = (
    Path(__file__).resolve().parent / "n8n_scripts" / "ensure_api_key.js"
).read_text(encoding="utf-8")
#: 腳本最壞耗時：SQLite busyTimeout 10s＋最多兩次公開 API 自我驗證（各 8s）＋node 冷啟動。
_ENSURE_KEY_EXEC_TIMEOUT = 40
#: 腳本回報「owner 不是平台要的那個」的原因碼 ⇒ error_step=owner_identity（平台據此給說明）。
_OWNER_IDENTITY_CODES = frozenset({"owner_missing", "owner_ambiguous", "owner_mismatch"})


def _api_key_items(payload) -> list:
    """取出 ``GET /rest/api-keys`` 回應裡的 key 清單（#1471）。

    n8n 2.x（正式站 2.35.3 實測）回 ``{"data": {"items": [...], "counts": …}}``；
    舊版回 ``{"data": [...]}``。兩種都接受；其他形狀回空清單（只會變成「不刪」，
    與修正前行為相同，不會誤刪）。
    """
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data = data.get("items")
    return [k for k in data if isinstance(k, dict)] if isinstance(data, list) else []


def _is_json_object(body) -> bool:
    """``body`` 是 JSON 物件嗎（#1616）。

    n8n 2.x 一啟動就先開一個暫時的 HTTP server：DB migration 跑完之前，除了
    ``/healthz/readiness`` 之外**任何路徑**（含 ``GET /rest/settings``、``POST /rest/owner/setup``）
    都回 200＋純文字「n8n is starting up. Please wait」（2.35.3 實測：sqlite 約 2–4 秒、
    postgres 約 1–6 秒），請求不會被處理。只有 JSON 才是真正的 REST API 回的。
    """
    try:
        return isinstance(json.loads(body), dict)
    except (TypeError, ValueError):
        return False


def _http_error_kind(resp: dict) -> str:
    """非 200 的 node 回應 → 分類（#1263）。

    拿到 n8n 真正的 HTTP 狀態碼＝明確的錯誤回應 ⇒ failed。``status`` 為 0 是
    node 端 request error（連線被拒、socket hang up）——沒有回應，不知道請求
    有沒有送達 ⇒ unknown。
    """
    status = resp.get("status")
    return "failed" if isinstance(status, int) and status > 0 else "unknown"


def _build_node_http_script(
    method: str,
    path: str,
    body: Optional[dict] = None,
    headers: Optional[Dict[str, str]] = None,
    capture_headers: bool = False,
) -> str:
    """Build a Node.js one-liner for making HTTP requests to localhost:5678.

    Returns a script that outputs JSON: {"status": <code>, "body": <string>, "headers": {...}}
    """
    extra_headers = ""
    if headers:
        for k, v in headers.items():
            extra_headers += f',"{k}":"{v}"'

    data_str = json.dumps(body) if body else ""
    data_line = f'const d={json.dumps(data_str)};' if body else 'const d="";'

    content_length = ""
    if body:
        content_length = ',"Content-Length":Buffer.byteLength(d)'

    header_capture = ""
    if capture_headers:
        header_capture = ',headers:JSON.stringify(res.headers)'

    return (
        f'const http=require("http");'
        f'{data_line}'
        f'const req=http.request({{hostname:"localhost",port:5678,path:"{path}",'
        f'method:"{method}",headers:{{"Content-Type":"application/json"{content_length}{extra_headers}}}}},'
        f'res=>{{let b="";res.on("data",c=>b+=c);'
        f'res.on("end",()=>console.log(JSON.stringify({{status:res.statusCode,body:b{header_capture}}})));'
        f'}});'
        f'req.on("error",e=>console.log(JSON.stringify({{status:0,body:e.message}})));'
        f'{"req.write(d);" if body else ""}'
        f'req.end();'
    )


class N8nInitService:
    """Handles n8n post-deploy initialization.

    Performs the full init sequence:
    1. Wait for n8n API to be ready
    2. Create owner user via /rest/owner/setup
    3. Login to get auth cookie
    4. Create API key via /rest/api-keys
    5. Update K8s Secret with real API key
    6. Rollout restart deployment so sidecar picks up the new key via secretKeyRef

    #1534：owner 已存在時 2–4 改成 ``_ensure_api_key``（exec 進容器沿用或簽發 key），
    不用 owner 密碼登入；5–6 不變。
    """

    def __init__(self, k8s_service: Optional[KubernetesService] = None):
        self.k8s = k8s_service or KubernetesService()

    def _node_request(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        method: str,
        path: str,
        body: Optional[dict] = None,
        headers: Optional[Dict[str, str]] = None,
        capture_headers: bool = False,
        timeout: int = 15,
    ) -> dict:
        """Execute an HTTP request inside the n8n container using Node.js.

        Returns:
            dict with 'status' (int), 'body' (str), optionally 'headers' (dict)
        """
        script = _build_node_http_script(method, path, body, headers, capture_headers)

        result = self.k8s.exec_in_pod(
            namespace=namespace,
            pod_name=pod_name,
            container=container,
            command=["node", "-e", script],
            timeout=timeout,
        )

        output = result.stdout.strip()
        if not output:
            raise N8nInitError(f"Empty response from node script", step="node_request")

        resp = json.loads(output)
        return resp

    def initialize(
        self,
        namespace: str,
        release_name: str,
        owner_email: str,
        owner_password: str,
        owner_confirmed: bool = False,
    ) -> Dict:
        """Complete n8n initialization sequence.

        ``owner_confirmed``（#1534）：平台曾對這台服務 init 成功過（owner 是平台建立、之後歸
        租戶管理）。為 False 時，既有 owner 的 Email 必須等於 ``owner_email``，否則不發 key——
        可能是別人搶先完成了 setup。
        """
        logger.info(
            "Starting n8n initialization for %s/%s",
            namespace, release_name,
        )

        pod_name = self._get_n8n_pod(namespace, release_name)
        container = "n8n"

        # Step 1: Wait for n8n to be ready
        self._wait_for_ready(namespace, pod_name, container)

        # Step 2: owner 已存在 ⇒ 不登入（#1534）；否則建 owner、登入、建 key。
        if self._is_owner_setup_done(namespace, pod_name, container):
            logger.info(
                "n8n owner already set up; ensuring the API key without the owner password "
                "(confirmed by platform: %s)", owner_confirmed,
            )
            api_key = self._ensure_api_key(
                namespace, pod_name, container,
                expected_email=None if owner_confirmed else owner_email,
            )
        else:
            self._setup_owner(namespace, pod_name, container, owner_email, owner_password)
            # Step 3: Login to get auth cookie（owner 剛由這一次 init 建立，密碼必定是這組）
            cookie = self._login(namespace, pod_name, container, owner_email, owner_password)
            # Step 4: Create API key
            api_key = self._create_api_key(namespace, pod_name, container, cookie)

        # Step 5: Update K8s Secret with real API key
        # This is critical — without it, the sidecar keeps the placeholder key.
        secret_patched = False
        secret_name = self._find_secret(namespace, release_name)
        if secret_name:
            try:
                self.k8s.patch_secret(
                    namespace=namespace,
                    secret_name=secret_name,
                    data={"N8N_API_KEY": api_key},
                )
                secret_patched = True
                logger.info("Updated K8s Secret %s with real API key", secret_name)
            except KubectlException as e:
                logger.error("Failed to patch K8s Secret %s: %s", secret_name, e)
                return {
                    "success": False,
                    "api_key": api_key,
                    "owner_email": owner_email,
                    "secret_patched": False,
                    "pod_restarted": False,
                    # key 已在 n8n 端建立並隨回應帶回，只差 Secret ⇒ partial。
                    "error_step": "patch_secret",
                    "error_kind": "partial",
                }
        else:
            logger.error("No secret found for release %s, cannot update API key", release_name)
            return {
                "success": False,
                "api_key": api_key,
                "owner_email": owner_email,
                "secret_patched": False,
                "pod_restarted": False,
                "error_step": "find_secret",
                "error_kind": "partial",
            }

        # Step 6: Rollout restart to pick up real API key from Secret
        # The sidecar reads N8N_API_KEY via secretKeyRef, so a restart
        # makes it load the updated Secret value. PVC persists n8n data.
        pod_restarted = False
        deployment_name = self._find_deployment(namespace, release_name)
        if deployment_name:
            for attempt in range(3):
                try:
                    self.k8s.rollout_restart_deployment(
                        namespace=namespace,
                        deployment_name=deployment_name,
                    )
                    pod_restarted = True
                    logger.info("Triggered rollout restart for %s", deployment_name)
                    break
                except Exception as e:
                    logger.warning(
                        "Restart attempt %d/3 failed for %s: %s",
                        attempt + 1, deployment_name, e,
                    )
                    if attempt < 2:
                        time.sleep(5)
        else:
            logger.warning("No deployment found for release %s, skipping restart", release_name)

        logger.info("n8n initialization complete for %s/%s", namespace, release_name)

        return {
            "success": True,
            "api_key": api_key,
            "owner_email": owner_email,
            "secret_patched": secret_patched,
            "pod_restarted": pod_restarted,
        }

    def _get_n8n_pod(self, namespace: str, release_name: str) -> str:
        """Find the n8n pod name."""
        selectors = [
            f"app.kubernetes.io/instance={release_name},app.kubernetes.io/name=n8n",
            f"app.kubernetes.io/instance={release_name}",
        ]

        for selector in selectors:
            try:
                return self.k8s.get_pod_name(namespace, selector)
            except KubectlException:
                continue

        raise N8nInitError(
            f"Cannot find n8n pod for release {release_name} in {namespace}",
            step="find_pod",
            kind="not_executed",
        )

    def _wait_for_ready(self, namespace: str, pod_name: str, container: str) -> None:
        """Wait for n8n REST API to be ready.

        #1616：200 不夠——n8n 2.x 啟動中的暫時 server 對 ``/rest/settings`` 也回 200（純文字，
        見 ``_is_json_object``）；chart 的 readinessProbe 打 ``/healthz``，那段時間 pod 已經
        Ready。以前在這個空窗送 init，``POST /rest/owner/setup`` 拿到 200＋非 JSON，init 以
        500（JSONDecodeError、error_step 空白）收場。要等到回 JSON 才算 ready。
        """
        logger.info("Waiting for n8n API to be ready...")

        for attempt in range(MAX_READY_RETRIES):
            try:
                resp = self._node_request(
                    namespace, pod_name, container,
                    "GET", "/rest/settings",
                    timeout=10,
                )
                if resp.get("status") == 200 and _is_json_object(resp.get("body")):
                    logger.info("n8n API is ready (attempt %d)", attempt + 1)
                    return
            except (KubectlException, N8nInitError, json.JSONDecodeError):
                pass

            if attempt < MAX_READY_RETRIES - 1:
                logger.debug("n8n not ready yet, retrying in %ds...", READY_RETRY_DELAY)
                time.sleep(READY_RETRY_DELAY)

        # readiness 只打 GET /rest/settings，唯讀 ⇒ 還沒動到 n8n 狀態。
        raise N8nInitError(
            f"n8n API not ready after {MAX_READY_RETRIES * READY_RETRY_DELAY}s",
            step="wait_ready",
            kind="not_executed",
        )

    def _is_owner_setup_done(self, namespace: str, pod_name: str, container: str) -> bool:
        """Check if n8n owner is already set up."""
        try:
            resp = self._node_request(
                namespace, pod_name, container,
                "GET", "/rest/settings",
            )
            if resp.get("status") != 200:
                return False
            settings = json.loads(resp["body"])
            data = settings.get("data", settings)
            user_mgmt = data.get("userManagement", {})
            return not user_mgmt.get("showSetupOnFirstLoad", True)
        except Exception:
            return False

    def _setup_owner(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        email: str,
        password: str,
    ) -> None:
        """Create the owner user via n8n REST API."""
        logger.info("Setting up n8n owner: %s", email)

        try:
            resp = self._node_request(
                namespace, pod_name, container,
                "POST", "/rest/owner/setup",
                body={
                    "email": email,
                    "password": password,
                    "firstName": "Admin",
                    "lastName": "User",
                },
            )

            if resp.get("status") != 200:
                raise N8nInitError(
                    f"Owner setup returned HTTP {resp.get('status')}: {resp.get('body', '')[:200]}",
                    step="setup_owner",
                    kind=_http_error_kind(resp),
                )

            response = json.loads(resp["body"])
            user_data = response.get("data", response)
            if "id" not in user_data:
                raise N8nInitError(
                    f"Owner setup response missing user id: {resp['body'][:200]}",
                    step="setup_owner",
                )

            logger.info("Owner user created successfully")

        except KubectlException as e:
            # timeout 或其他 exec 失敗都分不出請求有沒有送達 n8n ⇒ unknown。
            raise N8nInitError(
                f"Failed to setup owner: {e.stderr}",
                step="setup_owner",
                kind="unknown",
            )

    def _login(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        email: str,
        password: str,
    ) -> str:
        """Login to n8n and return the auth cookie."""
        logger.info("Logging in to n8n as %s", email)

        try:
            resp = self._node_request(
                namespace, pod_name, container,
                "POST", "/rest/login",
                body={"emailOrLdapLoginId": email, "password": password},
                capture_headers=True,
            )

            if resp.get("status") != 200:
                raise N8nInitError(
                    f"Login returned HTTP {resp.get('status')}: {resp.get('body', '')[:200]}",
                    step="login",
                    kind=_http_error_kind(resp),
                )

            # Parse headers to find set-cookie
            headers_str = resp.get("headers", "{}")
            headers = json.loads(headers_str) if isinstance(headers_str, str) else headers_str

            set_cookie = headers.get("set-cookie", "")
            if not set_cookie:
                raise N8nInitError(
                    f"Login succeeded but no set-cookie header found",
                    step="login",
                )

            # set-cookie can be a string or array; take the first cookie
            if isinstance(set_cookie, list):
                set_cookie = set_cookie[0]

            # Extract "name=value" part before first ";"
            cookie = set_cookie.split(";")[0].strip()

            if not cookie:
                raise N8nInitError(
                    "Login succeeded but cookie value is empty",
                    step="login",
                )

            logger.info("Login successful, got auth cookie")
            return cookie

        except KubectlException as e:
            raise N8nInitError(
                f"Failed to login: {e.stderr}",
                step="login",
                kind="unknown",
            )

    def _get_api_key_scopes(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        cookie: str,
    ) -> list:
        """Get available API key scopes from n8n."""
        try:
            resp = self._node_request(
                namespace, pod_name, container,
                "GET", "/rest/api-keys/scopes",
                headers={"Cookie": cookie},
            )
            if resp.get("status") == 200:
                response = json.loads(resp["body"])
                scopes = response.get("data", response)
                if isinstance(scopes, list):
                    return scopes
        except Exception:
            pass
        # Fallback: common n8n scopes (must match /rest/api-keys/scopes output)
        return [
            "workflow:list", "workflow:read", "workflow:create",
            "workflow:update", "workflow:delete",
            "workflow:activate", "workflow:deactivate",
            "execution:read", "execution:list",
        ]

    def _delete_api_keys_labelled(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        cookie: str,
        label: str,
    ) -> int:
        """刪掉指定 label 的既有 API key，回傳刪了幾把。

        失敗一律只記 warning、不中斷 init：真的刪不掉時，接下來的建 key 會拿到 n8n
        明確的錯誤回應，分類（failed）與現在相同，不會比不刪更糟。
        """
        try:
            resp = self._node_request(
                namespace, pod_name, container,
                "GET", "/rest/api-keys",
                headers={"Cookie": cookie},
                timeout=_KEY_PRUNE_REQUEST_TIMEOUT,
            )
            if resp.get("status") != 200:
                logger.warning("Listing n8n API keys returned HTTP %s; not deleting", resp.get("status"))
                return 0
            keys = _api_key_items(json.loads(resp["body"]))
        except Exception as e:  # noqa: BLE001 — best-effort，見 docstring
            logger.warning("Listing n8n API keys failed (%s); not deleting", e)
            return 0
        stale = [k for k in keys if k.get("label") == label and k.get("id")]
        if not keys:
            logger.info("No existing n8n API keys found before creating %r", label)
        deleted = 0
        for key in stale[:_MAX_KEY_DELETES]:
            try:
                resp = self._node_request(
                    namespace, pod_name, container,
                    "DELETE", f"/rest/api-keys/{key['id']}",
                    headers={"Cookie": cookie},
                    timeout=_KEY_PRUNE_REQUEST_TIMEOUT,
                )
                if resp.get("status") == 200:
                    deleted += 1
                else:
                    logger.warning("Deleting n8n API key %s returned HTTP %s", key["id"], resp.get("status"))
            except Exception as e:  # noqa: BLE001
                logger.warning("Deleting n8n API key %s failed: %s", key["id"], e)
        if deleted:
            logger.info("Deleted %d existing n8n API key(s) labelled %r", deleted, label)
        return deleted

    def _create_api_key(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        cookie: str,
    ) -> str:
        """Create an API key via n8n REST API."""
        logger.info("Creating n8n API key")

        # 先刪掉同名的舊 key，再建新的（#1471）。n8n 2.x 不允許兩把 key 同名
        # （"There is already an entry with this name"）；init 只要重跑（redeploy 開新一代、
        # partial 之後重試），第二次建 key 就必定失敗，服務永遠卡在 initializing。
        self._delete_api_keys_labelled(namespace, pod_name, container, cookie, _API_KEY_LABEL)

        try:
            # First, discover available scopes
            available = self._get_api_key_scopes(namespace, pod_name, container, cookie)
            # DATA-N8N-006-1：只授與 sidecar 用得到的 scope（見 _SIDECAR_SCOPE_PREFIXES）。
            scopes = sidecar_scopes(available)
            logger.info("API key scopes: granting %d of %d available", len(scopes), len(available))
            if not scopes:
                raise N8nInitError(
                    "none of the scopes n8n offers are usable by the MCP sidecar",
                    step="create_api_key",
                    kind="failed",
                )

            resp = self._node_request(
                namespace, pod_name, container,
                "POST", "/rest/api-keys",
                # expiresAt=null means no expiration; use far-future timestamp if null not accepted
                body={"label": _API_KEY_LABEL, "scopes": scopes, "expiresAt": 4102444800},
                headers={"Cookie": cookie},
            )

            if resp.get("status") != 200:
                raise N8nInitError(
                    f"API key creation returned HTTP {resp.get('status')}: {resp.get('body', '')[:200]}",
                    step="create_api_key",
                    kind=_http_error_kind(resp),
                )

            response = json.loads(resp["body"])
            data = response.get("data", response)
            # n8n returns full key in 'rawApiKey', 'apiKey' is masked
            api_key = data.get("rawApiKey") or data.get("apiKey") if isinstance(data, dict) else None

            if not api_key:
                raise N8nInitError(
                    f"API key creation response missing rawApiKey: {resp['body'][:200]}",
                    step="create_api_key",
                )

            logger.info("API key created successfully")
            return api_key

        except KubectlException as e:
            raise N8nInitError(
                f"Failed to create API key: {e.stderr}",
                step="create_api_key",
                kind="unknown",
            )

    def _ensure_api_key(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        expected_email: Optional[str],
    ) -> str:
        """#1534：owner 已存在時，不用 owner 密碼取得 init 用的 API key（沿用或簽發）。

        參數走 stdin（不進 argv／operator log）。分類：腳本明說的失敗＝``failed``（沒有留下
        新 key：寫入前失敗，或自我驗證失敗時已刪掉剛寫的那列）；逾時、輸出不可解析、腳本內
        非預期例外＝``unknown``（不知道 key 寫進去了沒有）。
        """
        payload = json.dumps({
            "label": _API_KEY_LABEL,
            "expected_email": expected_email,
            "expires_at": _API_KEY_EXPIRES_AT,
        })
        try:
            result = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=container,
                command=["node", "-e", ENSURE_API_KEY_SCRIPT],
                timeout=_ENSURE_KEY_EXEC_TIMEOUT,
                input_data=payload,
            )
            reported = _last_json_object(result.stdout)
        except KubectlException as e:
            # 腳本失敗時先印 JSON 再 exit 1 ⇒ kubectl 非 0，最後一行仍是腳本的回報。
            reported = None if e.timed_out else _last_json_object(e.stdout)
            if reported is None:
                raise N8nInitError(
                    "timed out waiting for the API key script" if e.timed_out
                    else "failed to execute the API key script",
                    step="ensure_api_key",
                    kind="unknown",
                ) from e
        if reported is None:
            raise N8nInitError(
                "API key script returned no parsable result", step="ensure_api_key", kind="unknown")
        if reported.get("success") is True and reported.get("api_key"):
            logger.info("n8n API key %s without the owner password",
                        "reused" if reported.get("reused") else "issued")
            return reported["api_key"]
        code = reported.get("code")
        error = str(reported.get("error") or "API key script reported a failure")
        if not isinstance(code, str):
            # 腳本內非預期例外：可能在寫入之後才發生。
            raise N8nInitError(error, step="ensure_api_key", kind="unknown")
        step = "owner_identity" if code in _OWNER_IDENTITY_CODES else "ensure_api_key"
        raise N8nInitError(f"{code}: {error}", step=step, kind="failed")

    def _find_secret(self, namespace: str, release_name: str) -> Optional[str]:
        """Find the secret name for the release by label selector."""
        try:
            result = self.k8s._run_command([
                "get", "secrets",
                "--namespace", namespace,
                "-l", f"app.kubernetes.io/instance={release_name}",
                "--output", "jsonpath={.items[*].metadata.name}",
            ])
            secret_names = result.stdout.strip().split()
            # Prefer secrets with 'app-secret' or 'secret' in name, skip helm release secrets
            for name in secret_names:
                if "app-secret" in name or (name.endswith("-secret") and "helm" not in name):
                    return name
            # Fallback: first non-helm secret
            for name in secret_names:
                if "sh.helm.release" not in name:
                    return name
        except KubectlException:
            pass
        return None

    def _find_deployment(self, namespace: str, release_name: str) -> Optional[str]:
        """Find the deployment name for the release."""
        try:
            deployments = self.k8s.get_deployments(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={release_name}",
            )
            if deployments:
                return deployments[0]["name"]
        except KubectlException:
            pass
        return None

