# PaaS Operator Service

FastAPI-based Kubernetes Helm operations API for the PaaS platform.

## Overview

The PaaS Operator Service is a secure, high-performance API that wraps Helm CLI operations for managing Kubernetes workloads. It provides RESTful endpoints for installing, upgrading, and managing Helm releases in a multi-tenant PaaS environment.

### Key Features

- **Helm Operations**: Install, upgrade, rollback, and uninstall Helm releases
- **Namespace Management**: Create namespaces with resource quotas
- **Pod Monitoring**: Real-time pod status and health checks
- **Cloudflare Integration**: Automatic DNS record and Tunnel route management for public service access
- **Security**: API key authentication and application-layer namespace prefix validation (see [Namespace Enforcement](#namespace-enforcement) — the prefix boundary is enforced in the app layer, _not_ by RBAC)
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
┌──────────────────────┐       ┌─────────────────┐
│  PaaS Operator API   │──────▶│  Cloudflare API │
│     (FastAPI)        │ HTTPS │  (DNS + Tunnel) │
└──────┬───────────────┘       └─────────────────┘
       │ subprocess
       ▼
┌──────────────────────┐       ┌─────────────────┐
│    Helm CLI          │──────▶│  Kubernetes API │
└──────────────────────┘       └─────────────────┘
```

## API Endpoints

### Health

- `GET /health` - Health check with Helm version

### Releases

- `POST /api/releases` - Install a Helm chart. Before any cluster write it runs two read-only pre-checks and answers **409** with a structured `detail.error_code` when the install could not succeed: `workspace_storage_quota_exhausted` (namespace ResourceQuota already full, #1348) and `cluster_storage_capacity_exhausted` (the PVCs / StatefulSet `volumeClaimTemplates` / CDI DataVolumes this install would create — rendered with `helm install --dry-run` from the same chart, version and values — cannot be scheduled by Longhorn under its own rules, #1415; `detail` lists the volumes, per-node headroom and orphaned PVCs). Both checks let the install through when they cannot read their data. Kill switch: env `STORAGE_CAPACITY_PRECHECK_ENABLED=false` (chart ≥ 0.1.22: `config.storageCapacityPrecheck: false`).
- `GET /api/releases/{namespace}/{name}` - Get release info
- `PATCH /api/releases/{namespace}/{name}` - Upgrade release
- `DELETE /api/releases/{namespace}/{name}` - Uninstall release. The release's leftover PVCs / CDI DataVolumes (kept by #317) are labelled `paas.woowtech.io/orphaned-at` + `grace-period-days` for the chart's pvc-cleanup CronJob. Query: `subdomain` (Cloudflare route to delete); `grace_period_days` (#1431, optional) shortens the retention of those orphans to N days — the platform sends `1` for a service that never reached `running`. Only `1 ≤ N ≤ PVC_GRACE_PERIOD_DAYS` is honoured; anything else (missing, non-integer, out of range) falls back to `PVC_GRACE_PERIOD_DAYS` and never fails the delete. Operators older than #1431 ignore the parameter (FastAPI drops undeclared query parameters).
- `POST /api/releases/{namespace}/{name}/rollback` - Rollback release
- `GET /api/releases/{namespace}/{name}/revisions` - Get revision history
- `POST /api/releases/{namespace}/{name}/restart` - (#1432) `kubectl rollout restart` every Deployment **and** StatefulSet labelled `app.kubernetes.io/instance={name}`; issued, not awaited (`rollout_awaited: false`). VirtualMachines are not touched. 404 when the release has no Deployment/StatefulSet.
- `GET /api/releases/{namespace}/{name}/status` - Get release and pod status
- `GET /api/releases/{namespace}/{name}/public-address` - Get the public address the release's ngrok sidecar advertises on its container log: `tcp://host:port` (charts/emqx, native MQTT) or `https://host[:port]` (charts/headscale, control URL). Contract: the sidecar container is named `ngrok` and prints `WOOW_PUBLIC_URL=<url>` (legacy `WOOW_MQTT_PUBLIC_URL=` is still honoured) at the **start of a line**; the last valid line wins and an empty value retracts the address. `public_url: null` is a normal state (not enabled / agent not connected yet / retracted), not an error.
- `GET /api/releases/{namespace}/{name}/mqtt-address` - **Legacy** (called by platform ≤ 18.0.1.0.205): same contract, `tcp://` addresses only. Keep until no deployed platform calls it.

### Namespaces

- `POST /api/namespaces` - Create namespace with resource quota

### Cluster (read-only)

- `GET /api/cluster/storage-capacity?storage_class=<sc>` - Longhorn scheduling capacity (#1415): per node limit / scheduled / headroom / ratio, the largest new volume that still fits with all replicas (and degraded), and orphaned PVCs (`paas.woowtech.io/orphaned-at`, all namespaces). Always 200; `available: false` + `reason` when Longhorn cannot be read. Polled by the platform monitor cron.

### Routes (Cloudflare Tunnel)

- `GET /api/routes` - List all tunnel routes
- `POST /api/routes` - Create a tunnel route
- `DELETE /api/routes/{subdomain}` - Delete a tunnel route

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

### Deploy to Kubernetes with Helm

1. **Generate API Key:**

```bash
openssl rand -base64 32
```

2. **Install with Helm:**

```bash
# Install with custom API key
helm install paas-operator ./helm \
  --namespace paas-system \
  --create-namespace \
  --set auth.apiKey="YOUR_GENERATED_KEY"

# Or use an existing secret
helm install paas-operator ./helm \
  --namespace paas-system \
  --create-namespace \
  --set auth.existingSecret="my-secret" \
  --set auth.existingSecretKey="api-key"
```

3. **Customize configuration:**

```bash
# Override values
helm install paas-operator ./helm \
  --namespace paas-system \
  --create-namespace \
  --set auth.apiKey="YOUR_KEY" \
  --set replicaCount=3 \
  --set resources.limits.memory=1Gi
```

Or create a `values-prod.yaml`:

```yaml
replicaCount: 3
auth:
  apiKey: "your-secure-key"
resources:
  limits:
    cpu: 1000m
    memory: 1Gi
```

```bash
helm install paas-operator ./helm -f values-prod.yaml --namespace paas-system
```

4. **Verify deployment:**

```bash
# Check pods
kubectl get pods -n paas-system -l app.kubernetes.io/name=paas-operator

# Check service
kubectl get svc -n paas-system paas-operator

# Test health endpoint
kubectl run test-curl --rm -it --restart=Never --image=curlimages/curl -- \
  curl http://paas-operator.paas-system/health
```

5. **Upgrade:**

```bash
helm upgrade paas-operator ./helm --namespace paas-system --set image.tag=v1.1.0
```

6. **Uninstall:**

```bash
helm uninstall paas-operator --namespace paas-system
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
| `CORS_ORIGINS` | Comma-separated allowed CORS origins | "" (none) |

### Cloudflare Integration (Optional)

| Variable | Description | Default |
|----------|-------------|---------|
| `CLOUDFLARE_ENABLED` | Enable Cloudflare integration | false |
| `CLOUDFLARE_API_TOKEN` | API token with Tunnel:Edit and DNS:Edit permissions | "" |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID | "" |
| `CLOUDFLARE_TUNNEL_ID` | Tunnel ID from Zero Trust dashboard | "" |
| `CLOUDFLARE_ZONE_ID` | Zone ID for DNS management (optional) | "" |
| `CLOUDFLARE_DOMAIN` | Base domain (e.g., woowtech.io) | "" |

> **Note**: If `CLOUDFLARE_ZONE_ID` is not set, DNS records must be managed manually. Routes will still be created in the tunnel configuration.

## Security

### Namespace Enforcement

All operations are restricted to namespaces starting with the `paas-ws-` prefix. **This prefix boundary is enforced at the application layer only** — `validate_namespace()` in [`src/services/helm.py:79-84`](src/services/helm.py) rejects any namespace that does not start with `settings.namespace_prefix` (`paas-ws-`) before a Helm/kubectl call is issued, and it is invoked at the entry of every mutating `HelmService` / `KubernetesService` method (the single choke-point through which all shell-outs pass).

> **⚠️ RBAC does _not_ enforce the prefix.** The operator's `ClusterRole` (`paas-operator`, and the `paas-operator-stg` variant) grants verbs on resource *types* (`namespaces`, `pods`, `secrets`, `persistentvolumeclaims`, `deployments`, `statefulsets`, `ingresses`, `networkpolicies`, `resourcequotas`, `roles`/`rolebindings`, …) with **no `resourceNames` restriction**, bound cluster-wide via a `ClusterRoleBinding`. At the RBAC layer the ServiceAccount is therefore authorized across **all** namespaces, not just `paas-ws-*`. Kubernetes RBAC structurally *cannot* express a `paas-ws-*` prefix boundary: `resourceNames` matches exact object **names** (a different axis from namespace), supports no wildcard/prefix, and is ignored for `list`/`watch`/`create`; namespace scoping is only achievable via a namespaced `Role` bound per-namespace — impractical here because the operator must **create** namespaces on demand with dynamic `paas-ws-{hash}` names that cannot be pre-enumerated. Treat this `ClusterRole` as **operational authorization** (which verbs on which resource *types*), **not** a tenant-isolation boundary. Rationale, ground-truth manifest, and the defense-in-depth decision (admission-layer webhook as a post-launch hardening candidate) are recorded in [`docs/decisions/namespace-prefix-enforcement-app-layer-not-rbac.md`](../../docs/decisions/namespace-prefix-enforcement-app-layer-not-rbac.md).

### API Key Authentication

All endpoints (except `/health` and `/docs`) require the `X-API-Key` header:

```bash
curl -H "X-API-Key: your-secret-key" \
  http://paas-operator/api/releases/paas-ws-test/myapp
```

### RBAC Permissions

The service uses a dedicated ServiceAccount (`paas-system/paas-operator`) bound cluster-wide via `ClusterRoleBinding` to the `paas-operator` `ClusterRole`. The role grants full RWCU verbs on the following resource **types** (with **no `resourceNames` restriction** — i.e. cluster-wide across every namespace; see the ⚠️ note under [Namespace Enforcement](#namespace-enforcement)):

- `namespaces`
- Core resources: `pods` (+ `pods/log`, `pods/exec`), `services`, `serviceaccounts`, `configmaps`, `secrets`, `persistentvolumeclaims`, `events`
- Apps: `deployments`, `statefulsets`, `replicasets`, `daemonsets` (+ `*/scale`)
- Networking: `ingresses`, `networkpolicies`
- `resourcequotas`, `poddisruptionbudgets`, `horizontalpodautoscalers`, `leases`
- `jobs`, `cronjobs`
- Cluster-scoped: `persistentvolumes` (get/list/patch/delete), `storageclasses` (get/list)
- Longhorn (read-only, chart ≥ 0.1.22, #1415): `nodes.longhorn.io`, `settings.longhorn.io` (get/list) — without it the capacity pre-check fails open
- RBAC: `roles`, `rolebindings` (RWCU — needed for per-tenant Role provisioning)

> The `ClusterRole` is defined and published from the cross-repo **`woow-paas-charts`** `paas-operator` chart and applied via Fleet (`paas-app-bundle-cd`); it is _not_ managed from this repo. Edit it there, not with a local `helm upgrade`.

### YAML Injection Prevention

All Kubernetes manifests are created using `yaml.safe_dump()` to prevent YAML injection attacks. User inputs are never directly interpolated into YAML strings.

### CORS Configuration

For cross-origin requests, configure allowed origins:

```bash
# Allow specific origins
CORS_ORIGINS=https://odoo.example.com,https://admin.example.com

# Leave empty to block all cross-origin requests (recommended for internal services)
CORS_ORIGINS=
```

## Error Handling

The service provides structured error responses for all failure cases:

### Error Response Format

```json
{
  "detail": "Human-readable error message"
}
```

### HTTP Status Codes

| Status Code | Meaning |
|-------------|---------|
| 400 | Bad Request - Invalid input or namespace |
| 401 | Unauthorized - Missing or invalid API key |
| 404 | Not Found - Release or namespace doesn't exist |
| 409 | Conflict - Resource already exists |
| 500 | Internal Server Error - Helm command failed |

### Sanitized Error Messages

All error messages returned to clients are sanitized to prevent information disclosure:

- Internal paths and filenames are removed
- Helm stderr output is logged but not exposed to clients
- Stack traces are never included in responses

### Fail-Fast Startup

The service validates Helm availability on startup. If Helm is not accessible, the service will fail to start with a clear error message:

```
CRITICAL: Helm not available: {error} - Service cannot start!
```

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

### Create Cloudflare Tunnel Route

```bash
curl -X POST http://paas-operator/api/routes \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "subdomain": "myapp-demo",
    "service_url": "http://my-nginx.paas-ws-demo.svc.cluster.local:80"
  }'
```

### List All Tunnel Routes

```bash
curl http://paas-operator/api/routes \
  -H "X-API-Key: your-key"
```

### Delete a Tunnel Route

```bash
curl -X DELETE http://paas-operator/api/routes/myapp-demo \
  -H "X-API-Key: your-key"
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
kubectl logs -n paas-system -l app.kubernetes.io/name=paas-operator --tail=100
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

### Cloudflare Integration Issues

**Route creation fails:**
- Verify `CLOUDFLARE_API_TOKEN` has correct permissions (Tunnel:Edit, DNS:Edit)
- Check `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_TUNNEL_ID` are correct
- Ensure the tunnel is running and connected

**DNS records not created:**
- Confirm `CLOUDFLARE_ZONE_ID` is set (required for DNS management)
- Verify the domain matches the zone

**Check Cloudflare status:**

```bash
# View operator logs for Cloudflare errors
kubectl logs -n paas-system -l app.kubernetes.io/name=paas-operator | grep -i cloudflare
```

## Development

### Code Structure

```
src/
├── main.py              # FastAPI app + middleware
├── config.py            # Settings management
├── api/
│   ├── releases.py      # Release endpoints
│   ├── namespaces.py    # Namespace endpoints
│   └── routes.py        # Cloudflare route endpoints
├── services/
│   ├── helm.py          # Helm/kubectl wrappers
│   └── cloudflare.py    # Cloudflare Tunnel API client
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
