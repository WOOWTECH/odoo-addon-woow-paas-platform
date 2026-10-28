"""FastAPI application entry point."""
import logging
import os
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import (
    access,
    agent_runtimes,
    analytics,
    cluster,
    custom_domains,
    init,
    namespaces,
    odoo_modules,
    reconcile,
    resource_observations,
    releases,
    retrieval,
    routes,
    tunnels,
)
from src.config import assert_env_guard, settings
from src.models.schemas import ErrorResponse, HealthResponse
from src.services.helm import HelmService
from src.services.helm_limiter import HelmOperationBusy

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Process-lifetime identity. A replacement Pod/process must produce a distinct
# value so consumers can durably invalidate publication before reconfirming it.
OPERATOR_INSTANCE_EPOCH = str(uuid.uuid4())

# Replica identity: stable across container restarts inside one pod, distinct
# between replicas.  Consumers pair it with the epoch so that two replicas
# answering alternately behind one Service are not mistaken for restarts.
OPERATOR_INSTANCE_ID = (os.environ.get("HOSTNAME") or socket.gethostname() or "")[:128] or None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    logger.info("Starting PaaS Operator Service...")
    logger.info(f"Namespace prefix: {settings.namespace_prefix}")
    # F6 (#279): FAIL FAST on a PAAS_ENV misconfig that would stamp cross-env-colliding
    # tenant-isolation labels on the shared prod/stg cluster. Reverses the earlier
    # fail-OPEN "just log an unset env" stance — an env-less or env/prefix-swapped label
    # is a cross-env tenant leak once O4 makes the label load-bearing.
    assert_env_guard(
        settings.paas_env, settings.namespace_prefix, settings.allow_unset_paas_env
    )
    logger.info(
        "F6 env guard OK — PAAS_ENV=%s, namespace_prefix=%s",
        settings.paas_env or "(unset, dev opt-out)",
        settings.namespace_prefix,
    )

    # Verify Helm is available - FAIL FAST if not
    try:
        helm_service = HelmService()
        version = helm_service.get_version()
        logger.info(f"Helm version: {version}")
    except Exception as e:
        logger.critical(f"Helm not available: {e} - Service cannot start!")
        raise RuntimeError(f"Helm is required but not available: {e}")

    yield

    logger.info("Shutting down PaaS Operator Service...")


# Create FastAPI application
app = FastAPI(
    title="PaaS Operator Service",
    description="Kubernetes Helm operations API for PaaS platform",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS middleware - configure via CORS_ORIGINS env var
# For production, set CORS_ORIGINS=https://your-domain.com,https://another-domain.com
cors_origins = [
    origin.strip()
    for origin in settings.cors_origins.split(",")
    if origin.strip()
] if settings.cors_origins else []

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Authentication middleware
@app.middleware("http")
async def verify_api_key(request: Request, call_next):
    """Verify API key for all requests except health check."""
    # Skip auth for health check and docs
    if request.url.path in ["/health", "/docs", "/redoc", "/openapi.json"]:
        return await call_next(request)

    # Check API key header
    api_key = request.headers.get("X-API-Key")

    # An UNSET key is a misconfiguration, not a "disable auth" switch: fail
    # CLOSED (503) for every protected route rather than serving spawn / reap /
    # delete / scale to anyone (F-PC-4 / C-2 / D-1). Health & docs stay exempt
    # (checked above) so the k8s liveness probe is unaffected.
    if not settings.api_key:
        logger.error("API key not configured - failing closed (503)")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Operator API key not configured"},
        )

    if api_key != settings.api_key:
        logger.warning(f"Invalid API key attempt from {request.client.host}")
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or missing API key"},
        )

    return await call_next(request)


# Exception handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions."""
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            detail=exc.detail,
            error_type="HTTPException",
        ).model_dump(),
    )


@app.exception_handler(HelmOperationBusy)
async def helm_operation_busy_handler(request: Request, exc: HelmOperationBusy):
    """#1552：helm 名額排隊逾時 ⇒ 503（什麼都沒套用，可以直接重送）。

    結構化 ``detail.error_code=operator_busy`` 讓平台顯示自己的中文說明，而不是把它當成
    一般的升級失敗。``Retry-After`` 給 HTTP 層的重試一個合理的間隔。
    """
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={"Retry-After": "15"},
        content=ErrorResponse(
            detail=exc.detail(),
            error_type="HelmOperationBusy",
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions."""
    logger.exception(f"Unexpected error: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            detail="Internal server error",
            error_type=type(exc).__name__,
        ).model_dump(),
    )


# Health check endpoint
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["health"],
    summary="Health check",
)
async def health_check():
    """Check service health and Helm availability.

    Returns:
        Health status information
    """
    helm_version = None
    try:
        helm_service = HelmService()
        helm_version = helm_service.get_version()
    except Exception as e:
        logger.error(f"Helm health check failed: {e}")

    return HealthResponse(
        status="healthy" if helm_version else "degraded",
        timestamp=datetime.now(timezone.utc),
        helm_version=helm_version,
        instance_epoch=OPERATOR_INSTANCE_EPOCH,
        instance_id=OPERATOR_INSTANCE_ID,
    )


# Include routers
app.include_router(releases.router)
app.include_router(namespaces.router)
app.include_router(routes.router)
app.include_router(custom_domains.router)
app.include_router(tunnels.router)
app.include_router(init.router)
app.include_router(odoo_modules.router)
app.include_router(agent_runtimes.router)
app.include_router(access.router)
app.include_router(reconcile.router)
app.include_router(resource_observations.router)
app.include_router(analytics.router)
# #1415：叢集層唯讀查詢（Longhorn 排程額度）。
app.include_router(cluster.router)
# woow_paas_platform #1540：停權／刪除後的限時唯讀資料取回入口。
app.include_router(retrieval.router)


# Root endpoint
@app.get("/", tags=["root"])
async def root():
    """Root endpoint with service information."""
    return {
        "service": "PaaS Operator Service",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=True,
    )
