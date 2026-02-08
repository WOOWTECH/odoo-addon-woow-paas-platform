"""FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import namespaces, releases, routes
from src.config import settings
from src.models.schemas import ErrorResponse, HealthResponse
from src.services.helm import HelmService

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    logger.info("Starting PaaS Operator Service...")
    logger.info(f"Namespace prefix: {settings.namespace_prefix}")

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

    if not settings.api_key:
        logger.warning("API key not configured - authentication disabled")
        return await call_next(request)

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
        timestamp=datetime.utcnow(),
        helm_version=helm_version,
    )


# Include routers
app.include_router(releases.router)
app.include_router(namespaces.router)
app.include_router(routes.router)


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
