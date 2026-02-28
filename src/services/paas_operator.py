"""HTTP client for PaaS Operator Service.

This module provides a client for communicating with the PaaS Operator
FastAPI service that manages Helm releases in Kubernetes.
"""
import json
import logging
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout

_logger = logging.getLogger(__name__)

# Default timeout for HTTP requests (seconds)
DEFAULT_TIMEOUT = 30
# Longer timeout for helm operations that may take time
HELM_OPERATION_TIMEOUT = 120


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
        timeout: int = DEFAULT_TIMEOUT,
    ) -> Dict[str, Any]:
        """Make an HTTP request to the PaaS Operator API.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE)
            endpoint: API endpoint (e.g., '/api/releases')
            data: Request body data (for POST, PATCH)
            timeout: Request timeout in seconds

        Returns:
            Parsed JSON response

        Raises:
            PaaSOperatorConnectionError: If connection fails
            PaaSOperatorTimeoutError: If request times out
            PaaSOperatorAPIError: If API returns an error
        """
        url = f"{self.base_url}{endpoint}"
        _logger.debug("PaaS Operator request: %s %s", method, url)

        try:
            response = self._session.request(
                method=method,
                url=url,
                json=data if data else None,
                timeout=timeout,
            )

            # Log response status
            _logger.debug("PaaS Operator response: %s %s", response.status_code, endpoint)

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
    ) -> Dict[str, str]:
        """Create a Kubernetes namespace with resource quota.

        Args:
            namespace: Namespace name (must start with 'paas-ws-')
            cpu_limit: CPU limit (e.g., '2' or '2000m')
            memory_limit: Memory limit (e.g., '4Gi')
            storage_limit: Storage limit (e.g., '20Gi')

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
        return self._request('POST', '/api/namespaces', data=data)

    # ==================== Helm Release Operations ====================

    def install_release(
        self,
        namespace: str,
        release_name: str,
        chart: str,
        repo_url: Optional[str] = None,
        version: Optional[str] = None,
        values: Optional[Dict[str, Any]] = None,
        create_namespace: bool = False,
        expose: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Install a new Helm release.

        Args:
            namespace: Target namespace
            release_name: Name for the release
            chart: Chart reference (name or path)
            repo_url: Helm repository URL (optional, for adding repo)
            version: Chart version (optional)
            values: Helm values override
            create_namespace: Whether to create namespace if not exists
            expose: Cloudflare Tunnel expose configuration
                    e.g., {'enabled': True, 'subdomain': 'myapp', 'service_port': 8080}

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
        if expose:
            data['expose'] = expose
            _logger.info("install_release: Adding expose config to request: %s", expose)
        else:
            _logger.info("install_release: No expose config provided")

        _logger.debug("install_release: Full request data: %s", data)

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
        version: Optional[str] = None,
        chart: Optional[str] = None,
        reset_values: bool = False,
        reuse_values: bool = True,
    ) -> Dict[str, Any]:
        """Upgrade an existing Helm release.

        Args:
            namespace: Release namespace
            release_name: Release name
            values: New Helm values override
            version: New chart version
            chart: New chart reference (if changing)
            reset_values: Reset values to chart defaults
            reuse_values: Reuse last release values

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

        return self._request(
            'PATCH',
            f'/api/releases/{namespace}/{release_name}',
            data=data,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def uninstall_release(
        self,
        namespace: str,
        release_name: str,
        subdomain: Optional[str] = None,
    ) -> Dict[str, str]:
        """Uninstall a Helm release.

        Args:
            namespace: Release namespace
            release_name: Release name
            subdomain: Optional subdomain to delete from Cloudflare

        Returns:
            Deletion confirmation

        Raises:
            PaaSOperatorError: If uninstallation fails
        """
        url = f'/api/releases/{namespace}/{release_name}'
        if subdomain:
            url += f'?subdomain={subdomain}'
        return self._request(
            'DELETE',
            url,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def rollback_release(
        self,
        namespace: str,
        release_name: str,
        revision: int = 0,
    ) -> Dict[str, str]:
        """Rollback a Helm release to a previous revision.

        Args:
            namespace: Release namespace
            release_name: Release name
            revision: Target revision (0 = previous)

        Returns:
            Rollback confirmation

        Raises:
            PaaSOperatorError: If rollback fails
        """
        data = {'revision': revision}
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
    ) -> Dict[str, Any]:
        """Initialize n8n instance after deployment.

        Triggers owner setup and API key generation on the PaaS Operator.

        Args:
            namespace: K8s namespace
            release_name: Helm release name
            owner_email: Email for n8n owner user
            owner_password: Password for n8n owner user

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
            },
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def get_status(self, namespace: str, release_name: str) -> Dict[str, Any]:
        """Get detailed status of a release including pod information.

        Args:
            namespace: Release namespace
            release_name: Release name

        Returns:
            Release and pod status

        Raises:
            PaaSOperatorError: If status retrieval fails
        """
        return self._request(
            'GET',
            f'/api/releases/{namespace}/{release_name}/status',
        )


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


def get_mcp_endpoint_url(
    env,
    sidecar_response: Optional[Dict[str, Any]] = None,
    subdomain: Optional[str] = None,
    namespace: Optional[str] = None,
    release_name: Optional[str] = None,
    endpoint_path: str = '/mcp',
) -> Optional[str]:
    """Construct the MCP endpoint URL for a cloud service's sidecar.

    Priority order:
    1. Use mcp_endpoint_url from sidecar patch response (operator-provided, includes Cloudflare route)
    2. Build from subdomain + PaaS domain setting (Cloudflare convention: {subdomain}-mcp.{domain})
    3. Use mcp_internal_url from sidecar patch response (K8s internal, only works in-cluster)

    For local development, users can override via port-forward:
        kubectl port-forward -n {namespace} svc/{release}-mcp {port}:{port}

    Args:
        env: Odoo environment
        sidecar_response: Response dict from PaaSOperatorClient.patch_sidecar()
        subdomain: Service subdomain (from CloudService record)
        namespace: K8s namespace (for fallback internal URL)
        release_name: Helm release name (for fallback internal URL)
        endpoint_path: MCP endpoint path (default '/mcp')

    Returns:
        MCP endpoint URL or None if cannot be determined
    """
    # 1. Try operator-provided public URL
    if sidecar_response and sidecar_response.get('mcp_endpoint_url'):
        return sidecar_response['mcp_endpoint_url']

    # 2. Build from subdomain + PaaS domain
    IrConfigParameter = env['ir.config_parameter'].sudo()
    paas_domain = IrConfigParameter.get_param('woow_paas_platform.paas_domain', '')

    if subdomain and paas_domain:
        mcp_hostname = f"{subdomain}-mcp.{paas_domain}"
        return f"https://{mcp_hostname}{endpoint_path}"

    # 3. Try internal URL from sidecar response
    if sidecar_response and sidecar_response.get('mcp_internal_url'):
        internal_url = sidecar_response['mcp_internal_url'].rstrip('/')
        return f"{internal_url}{endpoint_path}"

    _logger.warning(
        "Cannot determine MCP endpoint URL for release %s in %s. "
        "Use port-forward for local access.",
        release_name, namespace,
    )
    return None
