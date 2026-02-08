# Multi-stage build for PaaS Operator Service
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Detect architecture and install Helm
ARG HELM_VERSION=3.13.3
ARG TARGETARCH
RUN ARCH=${TARGETARCH:-amd64} && \
    curl -fsSL https://get.helm.sh/helm-v${HELM_VERSION}-linux-${ARCH}.tar.gz | tar xz && \
    mv linux-${ARCH}/helm /usr/local/bin/helm && \
    rm -rf linux-${ARCH} && \
    helm version

# Install kubectl (matching architecture)
ARG KUBECTL_VERSION=1.28.4
RUN ARCH=${TARGETARCH:-amd64} && \
    curl -LO "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" && \
    chmod +x kubectl && \
    mv kubectl /usr/local/bin/kubectl && \
    kubectl version --client

# Create non-root user BEFORE copying packages
RUN useradd -m -u 1000 -g operator operator 2>/dev/null || \
    useradd -m -u 1000 operator 2>/dev/null || true

# Copy Python packages from builder to operator's home
COPY --from=builder /root/.local /home/operator/.local

# Copy application code
COPY src/ ./src/

# Set ownership
RUN chown -R operator:operator /app /home/operator/.local

USER operator

# Add local Python packages to PATH (use operator's home)
ENV PATH=/home/operator/.local/bin:$PATH
ENV PYTHONPATH=/home/operator/.local/lib/python3.11/site-packages
ENV PYTHONUNBUFFERED=1

# Expose application port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run application
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
