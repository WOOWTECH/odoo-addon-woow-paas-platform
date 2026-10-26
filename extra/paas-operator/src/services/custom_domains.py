"""Minimal Cloudflare for SaaS Custom Hostnames client.

Creation is intentionally a bare, non-idempotent provider call. In particular,
this module never lists or adopts a resource by hostname: the durable caller
must record intent before POST and persist the returned provider ID.
"""
from __future__ import annotations

from typing import Any

import httpx

from src.services.cloudflare import CloudflareException


class CustomDomainsClient:
    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self, *, zone_id: str, api_token: str):
        self.zone_id = zone_id
        self.api_token = api_token

    @property
    def _collection_url(self) -> str:
        return f"{self.BASE_URL}/zones/{self.zone_id}/custom_hostnames"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            # Do not retry mutations here. A POST transport failure is ambiguous:
            # Cloudflare may already have created the resource.
            raise CloudflareException("Cloudflare request outcome is unknown") from exc

    @staticmethod
    def _result(response: httpx.Response, accepted: set[int]) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise CloudflareException(
                "Cloudflare returned an invalid response", response.status_code
            ) from exc
        if response.status_code not in accepted or body.get("success") is not True:
            raise CloudflareException("Cloudflare custom hostname request failed", response.status_code)
        result = body.get("result")
        if not isinstance(result, dict):
            raise CloudflareException("Cloudflare response did not contain a resource", response.status_code)
        return result

    async def create(self, hostname: str, gateway_hostname: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            self._collection_url,
            json={
                "hostname": hostname,
                "custom_origin_server": gateway_hostname,
                # TXT DCV permits the customer to validate before changing the
                # live CNAME at their DNS provider.
                "ssl": {"method": "txt", "type": "dv"},
            },
        )
        return self._result(response, {200, 201})

    async def can_manage(self) -> bool:
        """Read-only permission probe before exposing billable onboarding."""
        response = await self._request(
            "GET", self._collection_url, params={"per_page": 1})
        if response.status_code != 200:
            return False
        try:
            return response.json().get("success") is True
        except ValueError:
            return False

    async def get_fallback_origin(self) -> dict[str, Any] | None:
        """Inspect the one-time Cloudflare for SaaS origin bootstrap."""
        response = await self._request(
            "GET", f"{self._collection_url}/fallback_origin")
        if response.status_code == 404:
            return None
        return self._result(response, {200})

    async def get(self, provider_id: str) -> dict[str, Any] | None:
        response = await self._request("GET", f"{self._collection_url}/{provider_id}")
        if response.status_code == 404:
            return None
        return self._result(response, {200})

    async def delete(self, provider_id: str) -> None:
        response = await self._request("DELETE", f"{self._collection_url}/{provider_id}")
        if response.status_code == 404:
            return
        self._result(response, {200, 202})
