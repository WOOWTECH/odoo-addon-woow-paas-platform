"""Cloudflare Tunnel API client for managing ingress routes and tunnel lifecycle."""
import base64
import logging
import secrets
from typing import Any, Dict, List, Optional

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

    async def get_dns_record(self, hostname: str) -> Optional[dict]:
        """Get a DNS record by hostname.

        Args:
            hostname: Full hostname (e.g., 'myapp.domain.com')

        Returns:
            DNS record dict if found, None otherwise
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
                return None

            data = response.json()
            if data.get("success") and data.get("result"):
                return data["result"][0] if data["result"] else None

            return None

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
            logger.info(f"DNS record for {hostname} already exists")
            # Update if target is different
            if existing.get("content") != self._tunnel_cname_target:
                return await self._update_dns_record(existing["id"], hostname)
            return True

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
        self, subdomain: str, tunnel_id: str
    ) -> Optional[str]:
        """Create a CNAME DNS record pointing to a specific tunnel.

        Args:
            subdomain: Subdomain to create (e.g., 'myapp' for myapp.domain.com)
            tunnel_id: The tunnel ID to point the CNAME at.

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
            logger.info(f"DNS record for {hostname} already exists")
            if existing.get("content") != cname_target:
                await self._update_dns_record(existing["id"], hostname)
            return existing["id"]

        logger.info(f"Creating DNS record: {hostname} -> {cname_target}")

        payload = {
            "type": "CNAME",
            "name": subdomain,
            "content": cname_target,
            "ttl": 1,
            "proxied": True,
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

            record_id = data.get("result", {}).get("id")
            logger.info(f"DNS record created: {hostname} (ID: {record_id})")
            return record_id

    async def _update_dns_record(self, record_id: str, hostname: str) -> bool:
        """Update an existing DNS record.

        Args:
            record_id: DNS record ID
            hostname: Full hostname

        Returns:
            True if update was successful
        """
        logger.info(f"Updating DNS record: {hostname} -> {self._tunnel_cname_target}")

        payload = {
            "type": "CNAME",
            "name": hostname,
            "content": self._tunnel_cname_target,
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
    ) -> bool:
        """Create a new tunnel route for a service.

        This method creates both the tunnel ingress route and the DNS CNAME record.

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

        # Step 1: Create DNS record (so the hostname resolves)
        try:
            await self.create_dns_record(subdomain)
        except CloudflareException as e:
            logger.error(f"Failed to create DNS record for {subdomain}: {e.message}")
            # Continue with route creation - DNS might be managed manually

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

        return await self.update_tunnel_config(ingress_rules)

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

    async def get_tunnel_status(self, tunnel_id: str) -> Dict[str, Any]:
        """Get the status and connection info for a Cloudflare Tunnel.

        Args:
            tunnel_id: The tunnel ID.

        Returns:
            Dict with tunnel status, name, connections, and created_at.

        Raises:
            CloudflareException: If status retrieval fails.
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

        Args:
            cname_target: The CNAME target to search for (e.g., '<tunnel_id>.cfargotunnel.com').
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self._dns_records_url,
                headers=self._headers,
                params={"type": "CNAME", "content": cname_target},
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.warning(
                    f"Failed to list DNS records for cleanup: {response.status_code}"
                )
                return

            data = response.json()
            records = data.get("result", [])

            for record in records:
                record_id = record.get("id")
                record_name = record.get("name", "unknown")
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
