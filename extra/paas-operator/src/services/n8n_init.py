"""N8n post-deploy initialization service.

Handles automated owner setup and API key generation for newly deployed
n8n instances so MCP sidecar can connect immediately.

Uses Node.js (available in n8n container) for HTTP requests since wget
has encoding issues with n8n's body parser.
"""
import json
import logging
import time
from typing import Dict, Optional

from src.services.helm import KubectlException, KubernetesService

logger = logging.getLogger(__name__)

# Retry configuration
MAX_READY_RETRIES = 15
READY_RETRY_DELAY = 4  # seconds


class N8nInitError(Exception):
    """Raised when n8n initialization fails."""

    def __init__(self, message: str, step: str = ""):
        self.step = step
        super().__init__(message)


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
    6. Patch sidecar container env with real API key
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
    ) -> Dict:
        """Complete n8n initialization sequence."""
        logger.info(
            "Starting n8n initialization for %s/%s",
            namespace, release_name,
        )

        pod_name = self._get_n8n_pod(namespace, release_name)
        container = "n8n"

        # Step 1: Wait for n8n to be ready
        self._wait_for_ready(namespace, pod_name, container)

        # Step 2: Check if owner already exists
        if self._is_owner_setup_done(namespace, pod_name, container):
            logger.info("n8n owner already set up, skipping setup step")
        else:
            self._setup_owner(namespace, pod_name, container, owner_email, owner_password)

        # Step 3: Login to get auth cookie
        cookie = self._login(namespace, pod_name, container, owner_email, owner_password)

        # Step 4: Create API key
        api_key = self._create_api_key(namespace, pod_name, container, cookie)

        # Step 5: Update K8s Secret with real API key
        secret_name = self._find_secret(namespace, release_name)
        if secret_name:
            try:
                self.k8s.patch_secret(
                    namespace=namespace,
                    secret_name=secret_name,
                    data={"N8N_API_KEY": api_key},
                )
                logger.info("Updated K8s Secret %s with real API key", secret_name)
            except KubectlException as e:
                logger.warning("Failed to patch K8s Secret %s: %s (non-fatal)", secret_name, e)
        else:
            logger.warning("No secret found for release %s, skipping secret update", release_name)

        # Note: Skipping sidecar env patch to avoid deployment restart which
        # would wipe n8n's ephemeral data (owner + API key). The Secret is
        # updated above; sidecar picks up the key on next manual pod restart.

        logger.info("n8n initialization complete for %s/%s", namespace, release_name)

        return {
            "success": True,
            "api_key": api_key,
            "owner_email": owner_email,
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
        )

    def _wait_for_ready(self, namespace: str, pod_name: str, container: str) -> None:
        """Wait for n8n REST API to be ready."""
        logger.info("Waiting for n8n API to be ready...")

        for attempt in range(MAX_READY_RETRIES):
            try:
                resp = self._node_request(
                    namespace, pod_name, container,
                    "GET", "/rest/settings",
                    timeout=10,
                )
                if resp.get("status") == 200 and resp.get("body"):
                    logger.info("n8n API is ready (attempt %d)", attempt + 1)
                    return
            except (KubectlException, N8nInitError, json.JSONDecodeError):
                pass

            if attempt < MAX_READY_RETRIES - 1:
                logger.debug("n8n not ready yet, retrying in %ds...", READY_RETRY_DELAY)
                time.sleep(READY_RETRY_DELAY)

        raise N8nInitError(
            f"n8n API not ready after {MAX_READY_RETRIES * READY_RETRY_DELAY}s",
            step="wait_ready",
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
            raise N8nInitError(
                f"Failed to setup owner: {e.stderr}",
                step="setup_owner",
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

    def _create_api_key(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        cookie: str,
    ) -> str:
        """Create an API key via n8n REST API."""
        logger.info("Creating n8n API key")

        try:
            # First, discover available scopes
            scopes = self._get_api_key_scopes(namespace, pod_name, container, cookie)
            logger.info("Available API key scopes: %s", scopes)

            resp = self._node_request(
                namespace, pod_name, container,
                "POST", "/rest/api-keys",
                # expiresAt=null means no expiration; use far-future timestamp if null not accepted
                body={"label": "mcp-sidecar", "scopes": scopes, "expiresAt": 4102444800},
                headers={"Cookie": cookie},
            )

            if resp.get("status") != 200:
                raise N8nInitError(
                    f"API key creation returned HTTP {resp.get('status')}: {resp.get('body', '')[:200]}",
                    step="create_api_key",
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
            )

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

    def _patch_sidecar_env(
        self,
        namespace: str,
        deployment_name: str,
        env_name: str,
        value: str,
    ) -> None:
        """Patch the MCP sidecar container's env with the real API key."""
        logger.info("Patching sidecar env %s in deployment %s", env_name, deployment_name)

        try:
            result = self.k8s._run_command([
                "get", "deployment", deployment_name,
                "--namespace", namespace,
                "--output", "jsonpath={.spec.template.spec.containers[*].name}",
            ])

            container_names = result.stdout.strip().split()
            sidecar_index = None
            for i, name in enumerate(container_names):
                if name == "mcp-sidecar":
                    sidecar_index = i
                    break

            if sidecar_index is None:
                logger.warning("mcp-sidecar container not found in %s", deployment_name)
                return

            self.k8s.patch_container_env(
                namespace=namespace,
                deployment_name=deployment_name,
                container_index=sidecar_index,
                env_name=env_name,
                value=value,
            )

            logger.info("Sidecar env %s patched successfully", env_name)

        except KubectlException as e:
            logger.warning("Failed to patch sidecar env: %s (non-fatal)", e)
