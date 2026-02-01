# PaaS Operator Service

FastAPI-based Kubernetes Helm operations API for the PaaS platform.

## Overview

The PaaS Operator Service is a secure, high-performance API that wraps Helm CLI operations for managing Kubernetes workloads. It provides RESTful endpoints for installing, upgrading, and managing Helm releases in a multi-tenant PaaS environment.

### Key Features

- **Helm Operations**: Install, upgrade, rollback, and uninstall Helm releases
- **Namespace Management**: Create namespaces with resource quotas
- **Pod Monitoring**: Real-time pod status and health checks
- **Security**: API key authentication and namespace prefix enforcement
- **High Performance**: Async-first FastAPI with connection pooling
- **Production-Ready**: Health checks, structured logging, and error handling

## Architecture

```
┌──────────────┐
│     Odoo     │
│  (woow_paas) │
└──────┬───────┘
       │ HTTP + API Key
       ▼
┌──────────────────────┐
│  PaaS Operator API   │
│     (FastAPI)        │
└──────┬───────────────┘
       │ subprocess
       ▼
┌──────────────────────┐      ┌─────────────────┐
│    Helm CLI          │─────▶│  Kubernetes API │
└──────────────────────┘      └─────────────────┘
```

## API Endpoints

### Health

- `GET /health` - Health check with Helm version

### Releases

- `POST /api/releases` - Install a Helm chart
- `GET /api/releases/{namespace}/{name}` - Get release info
- `PATCH /api/releases/{namespace}/{name}` - Upgrade release
- `DELETE /api/releases/{namespace}/{name}` - Uninstall release
- `POST /api/releases/{namespace}/{name}/rollback` - Rollback release
- `GET /api/releases/{namespace}/{name}/revisions` - Get revision history
- `GET /api/releases/{namespace}/{name}/status` - Get release and pod status

### Namespaces

- `POST /api/namespaces` - Create namespace with resource quota

## Quick Start

### Prerequisites

- Python 3.11+
- Helm 3.13+
- kubectl 1.28+
- Kubernetes cluster access

### Local Development

1. **Install dependencies:**

```bash
pip install -r requirements.txt
```

2. **Configure environment:**

```bash
cat > .env <<EOF
API_KEY=your-secret-key
LOG_LEVEL=info
NAMESPACE_PREFIX=paas-ws-
HELM_BINARY=/usr/local/bin/helm
HELM_TIMEOUT=300
EOF
```

3. **Run the service:**

```bash
cd src
python -m uvicorn main:app --reload --port 8000
```

4. **Access the API:**

- API Documentation: http://localhost:8000/docs
- Health Check: http://localhost:8000/health

### Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src --cov-report=html

# Run specific test file
pytest tests/test_helm.py -v
```

## Deployment

### Build Docker Image

```bash
docker build -t paas-operator:latest .
```

### Deploy to Kubernetes

1. **Generate API Key:**

```bash
openssl rand -base64 32
```

2. **Update secret:**

Edit `k8s/secret.yaml` and replace `CHANGEME_GENERATE_SECURE_KEY` with the generated key.

3. **Deploy:**

```bash
# Create RBAC (ServiceAccount, ClusterRole, ClusterRoleBinding)
kubectl apply -f k8s/rbac.yaml

# Create Secret
kubectl apply -f k8s/secret.yaml

# Deploy application
kubectl apply -f k8s/deployment.yaml

# Create Service
kubectl apply -f k8s/service.yaml
```

4. **Verify deployment:**

```bash
# Check pods
kubectl get pods -l app=paas-operator

# Check service
kubectl get svc paas-operator

# Test health endpoint
kubectl run test-curl --rm -it --restart=Never --image=curlimages/curl -- \
  curl http://paas-operator/health
```

### Access the Service

From within the cluster:

```bash
curl -H "X-API-Key: YOUR_API_KEY" \
  http://paas-operator/api/releases/paas-ws-test/myapp
```

## Configuration

Environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `API_KEY` | API authentication key | "" |
| `HOST` | Service host | 0.0.0.0 |
| `PORT` | Service port | 8000 |
| `LOG_LEVEL` | Logging level | info |
| `NAMESPACE_PREFIX` | Allowed namespace prefix | paas-ws- |
| `HELM_BINARY` | Path to Helm binary | /usr/local/bin/helm |
| `HELM_TIMEOUT` | Helm command timeout (seconds) | 300 |

## Security

### Namespace Enforcement

All operations are restricted to namespaces starting with `paas-ws-` prefix. This is enforced at both the application and RBAC levels.

### API Key Authentication

All endpoints (except `/health` and `/docs`) require the `X-API-Key` header:

```bash
curl -H "X-API-Key: your-secret-key" \
  http://paas-operator/api/releases/paas-ws-test/myapp
```

### RBAC Permissions

The service uses a dedicated ServiceAccount with ClusterRole permissions limited to:

- Namespace management
- Core resources (Pods, Services, ConfigMaps, Secrets, PVCs)
- Apps resources (Deployments, StatefulSets)
- Networking (Ingresses)
- Resource quotas

## Usage Examples

### Install a Helm Chart

```bash
curl -X POST http://paas-operator/api/releases \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "namespace": "paas-ws-demo",
    "name": "my-nginx",
    "chart": "nginx",
    "version": "15.0.0",
    "create_namespace": true,
    "values": {
      "replicaCount": 2,
      "service": {
        "type": "ClusterIP"
      }
    }
  }'
```

### Get Release Status

```bash
curl http://paas-operator/api/releases/paas-ws-demo/my-nginx/status \
  -H "X-API-Key: your-key"
```

### Upgrade Release

```bash
curl -X PATCH http://paas-operator/api/releases/paas-ws-demo/my-nginx \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "version": "15.1.0",
    "values": {
      "replicaCount": 3
    }
  }'
```

### Rollback Release

```bash
curl -X POST http://paas-operator/api/releases/paas-ws-demo/my-nginx/rollback \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "revision": 1
  }'
```

### Create Namespace with Quota

```bash
curl -X POST http://paas-operator/api/namespaces \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "paas-ws-new-tenant",
    "cpu_limit": "4",
    "memory_limit": "8Gi",
    "storage_limit": "50Gi"
  }'
```

## Monitoring

### Health Checks

The service includes Kubernetes liveness and readiness probes:

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 10
  periodSeconds: 30

readinessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 5
  periodSeconds: 10
```

### Logging

Structured logging with configurable levels:

```python
logger.info("Installed release my-nginx in paas-ws-demo")
logger.error(f"Helm install failed: {error_message}")
```

## Troubleshooting

### Helm Command Fails

Check logs:

```bash
kubectl logs -l app=paas-operator --tail=100
```

Common issues:
- Helm binary not found → Check `HELM_BINARY` path
- Permission denied → Verify RBAC configuration
- Timeout → Increase `HELM_TIMEOUT` setting

### Authentication Errors

- Verify API key in Secret matches client requests
- Check `X-API-Key` header is present
- Health endpoint doesn't require authentication

### Namespace Access Denied

- Ensure namespace starts with `paas-ws-` prefix
- Verify RBAC permissions include the namespace
- Check ClusterRoleBinding is correctly applied

## Development

### Code Structure

```
src/
├── main.py              # FastAPI app + middleware
├── config.py            # Settings management
├── api/
│   ├── releases.py      # Release endpoints
│   └── namespaces.py    # Namespace endpoints
├── services/
│   └── helm.py          # Helm/kubectl wrappers
└── models/
    └── schemas.py       # Pydantic models
```

### Adding New Endpoints

1. Define Pydantic schema in `models/schemas.py`
2. Implement service logic in `services/helm.py`
3. Create API endpoint in `api/`
4. Add tests in `tests/`
5. Update this README

### Running Tests

```bash
# Install test dependencies
pip install pytest pytest-asyncio pytest-cov

# Run tests
pytest -v

# Generate coverage report
pytest --cov=src --cov-report=term-missing
```

## License

Proprietary - Woow PaaS Platform

## Support

For issues or questions, contact the PaaS Platform team.
