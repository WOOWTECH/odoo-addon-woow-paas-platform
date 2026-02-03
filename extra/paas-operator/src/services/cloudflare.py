"""Cloudflare Tunnel API client for managing ingress routes."""
import logging
from typing import List, Optional

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


class CloudflareService:
    """Service for managing Cloudflare Tunnel routes via API."""

    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self):
        """Initialize the Cloudflare service."""
        self.account_id = settings.cloudflare_account_id
        self.tunnel_id = settings.cloudflare_tunnel_id
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
    ) -> bool:
        """Create a new tunnel route for a service.

        Args:
            subdomain: Subdomain for the route (e.g., 'myapp' for myapp.domain.com).
            service_url: Internal service URL (e.g., 'http://svc.ns.svc.cluster.local:8080').
            path: Optional path prefix to match.

        Returns:
            True if route was created successfully.

        Raises:
            CloudflareException: If route creation fails.
        """
        if not self.enabled:
            logger.info(f"Cloudflare disabled, skipping route creation for {subdomain}")
            return True

        hostname = f"{subdomain}.{self.domain}"
        logger.info(f"Creating route: {hostname} -> {service_url}")

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

        return await self.update_tunnel_config(ingress_rules)

    async def delete_route(self, subdomain: str) -> bool:
        """Delete a tunnel route.

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

        # Get current config
        current_config = await self.get_tunnel_config()
        ingress_rules = current_config.get("config", {}).get("ingress", [])

        # Filter out the route to delete
        new_rules = [rule for rule in ingress_rules if rule.get("hostname") != hostname]

        if len(new_rules) == len(ingress_rules):
            logger.warning(f"Route for {hostname} not found, nothing to delete")
            return True

        return await self.update_tunnel_config(new_rules)

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
