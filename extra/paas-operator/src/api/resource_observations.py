"""Read-only, namespace-scoped resource observations for authorized PaaS callers."""
import re
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.services.resource_observations import ResourceCollector, validate_scope

router = APIRouter(prefix="/api/namespaces", tags=["resources"])
collector = ResourceCollector()


class ResourceObservationRequest(BaseModel):
    release_names: List[str] = Field(min_length=1, max_length=100)

    @field_validator("release_names")
    @classmethod
    def valid_releases(cls, names):
        if any(len(n) > 53 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", n) for n in names):
            raise ValueError("Invalid Helm release name")
        return sorted(set(names))


@router.post("/{namespace}/resource-observations")
def resource_observations(namespace: str, request: ResourceObservationRequest):
    """Does not invoke Helm, exec into tenants, or change workload state."""
    try:
        validate_scope(namespace)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        return collector.collect(namespace, request.release_names)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Resource observations temporarily busy") from exc
