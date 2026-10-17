# newapi-proxy image (DESIGN §1.4, ticket LLM-01a).
# Built by .gitea/workflows/build-newapi-proxy.yml and pushed to JCR under the
# commit sha only; deployments pin the digest (Woow_k3s_newapi proxy.image).
#
# Base image pinned by tag *and* index digest (python 3.12.14, bookworm slim).
ARG PYTHON_IMAGE=docker.io/library/python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
COPY requirements.txt /tmp/requirements.txt
# Fully locked with hashes (uv pip compile --generate-hashes); --no-deps so
# nothing outside the lock file can be pulled in.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.txt \
    && /opt/venv/bin/pip check

FROM ${PYTHON_IMAGE}
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY newapi_proxy/ ./newapi_proxy/
# Same uid as the rest of the newapi chart; root FS is mounted read-only.
USER 65532:65532
# 8080: the signed proxy port. 8081: admin listener (/healthz, /readyz,
# /metrics) — the chart's probes use it; no HEALTHCHECK here.
EXPOSE 8080 8081
CMD ["python", "-m", "newapi_proxy"]
