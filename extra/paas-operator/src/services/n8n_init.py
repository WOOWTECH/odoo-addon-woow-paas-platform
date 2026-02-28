"""N8n post-deploy initialization service.

Handles automated owner setup and API key generation for newly deployed
n8n instances so MCP sidecar can connect immediately.
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

    def initialize(
        self,
        namespace: str,
        release_name: str,
        owner_email: str,
        owner_password: str,
    ) -> Dict:
        """Complete n8n initialization sequence.

        Args:
            namespace: K8s namespace
            release_name: Helm release name
            owner_email: Email for the owner user
            owner_password: Password for the owner user

        Returns:
            dict with api_key, owner_email, success

        Raises:
            N8nInitError: If any step fails
        """
        logger.info(
            "Starting n8n initialization for %s/%s",
            namespace, release_name,
        )

        # Find the n8n pod
        pod_name = self._get_n8n_pod(namespace, release_name)
        container = "n8n"

        # Step 1: Wait for n8n to be ready
        self._wait_for_ready(namespace, pod_name, container)

        # Step 2: Check if owner already exists
        if self._is_owner_setup_done(namespace, pod_name, container):
            logger.info("n8n owner already set up, skipping setup step")
        else:
            # Create owner user
            self._setup_owner(namespace, pod_name, container, owner_email, owner_password)

        # Step 3: Login to get auth cookie
        cookie = self._login(namespace, pod_name, container, owner_email, owner_password)

        # Step 4: Create API key
        api_key = self._create_api_key(namespace, pod_name, container, cookie)

        # Step 5: Update K8s Secret
        secret_name = f"{release_name}-n8n-secret"
        try:
            self.k8s.patch_secret(
                namespace=namespace,
                secret_name=secret_name,
                data={"N8N_API_KEY": api_key},
            )
            logger.info("Updated K8s Secret %s with real API key", secret_name)
        except KubectlException as e:
            logger.warning("Failed to patch K8s Secret %s: %s (non-fatal)", secret_name, e)

        # Step 6: Patch sidecar env with real API key
        deployment_name = self._find_deployment(namespace, release_name)
        if deployment_name:
            self._patch_sidecar_env(namespace, deployment_name, "N8N_API_KEY", api_key)

        logger.info("n8n initialization complete for %s/%s", namespace, release_name)

        return {
            "success": True,
            "api_key": api_key,
            "owner_email": owner_email,
        }

    def _get_n8n_pod(self, namespace: str, release_name: str) -> str:
        """Find the n8n pod name."""
        # Try common label selectors
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
                result = self.k8s.exec_in_pod(
                    namespace=namespace,
                    pod_name=pod_name,
                    container=container,
                    command=[
                        "curl", "-sf",
                        "http://localhost:5678/rest/settings",
                    ],
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    logger.info("n8n API is ready (attempt %d)", attempt + 1)
                    return
            except KubectlException:
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
            result = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=container,
                command=[
                    "curl", "-sf",
                    "http://localhost:5678/rest/settings",
                ],
                timeout=10,
            )
            settings = json.loads(result.stdout)
            return not settings.get("showSetupOnFirstLoad", True)
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

        payload = json.dumps({
            "email": email,
            "password": password,
            "firstName": "Admin",
            "lastName": "User",
        })

        try:
            result = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=container,
                command=[
                    "curl", "-sf",
                    "-X", "POST",
                    "-H", "Content-Type: application/json",
                    "-d", payload,
                    "http://localhost:5678/rest/owner/setup",
                ],
                timeout=15,
            )

            response = json.loads(result.stdout)
            if "id" not in response:
                raise N8nInitError(
                    f"Owner setup response missing user id: {result.stdout[:200]}",
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

        payload = json.dumps({
            "email": email,
            "password": password,
        })

        try:
            result = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=container,
                command=[
                    "curl", "-sf",
                    "-X", "POST",
                    "-H", "Content-Type: application/json",
                    "-d", payload,
                    "-D", "-",
                    "http://localhost:5678/rest/login",
                ],
                timeout=10,
            )

            # Extract Set-Cookie header from response headers
            output = result.stdout
            cookie = ""
            for line in output.split("\n"):
                if line.lower().startswith("set-cookie:"):
                    # Extract just the cookie value (before first ;)
                    cookie_part = line.split(":", 1)[1].strip()
                    cookie = cookie_part.split(";")[0].strip()
                    break

            if not cookie:
                raise N8nInitError(
                    "Login succeeded but no cookie returned",
                    step="login",
                )

            logger.info("Login successful, got auth cookie")
            return cookie

        except KubectlException as e:
            raise N8nInitError(
                f"Failed to login: {e.stderr}",
                step="login",
            )

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
            result = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=container,
                command=[
                    "curl", "-sf",
                    "-X", "POST",
                    "-H", "Content-Type: application/json",
                    "-H", f"Cookie: {cookie}",
                    "-d", "{}",
                    "http://localhost:5678/rest/api-keys",
                ],
                timeout=10,
            )

            response = json.loads(result.stdout)

            # Response could be the key object directly or wrapped in data
            api_key = response.get("apiKey") or response.get("data", {}).get("apiKey")

            if not api_key:
                raise N8nInitError(
                    f"API key creation response missing apiKey: {result.stdout[:200]}",
                    step="create_api_key",
                )

            logger.info("API key created successfully")
            return api_key

        except KubectlException as e:
            raise N8nInitError(
                f"Failed to create API key: {e.stderr}",
                step="create_api_key",
            )

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

        # Find the mcp-sidecar container index
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
