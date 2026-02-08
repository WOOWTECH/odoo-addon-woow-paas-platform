# Kubernetes Deployment Guide

## Prerequisites

- K3s or K8s cluster running (1.28+)
- kubectl configured and authenticated
- Helm 3.13+ installed
- Cloudflare DNS setup (\*.woowtech.io) or your own domain
- PostgreSQL database for Odoo (can be external or in-cluster)

## Architecture Overview

The WoowTech PaaS Platform consists of:

1. **Odoo Application**: Main application running the woow_paas_platform addon
2. **PaaS Operator Service**: FastAPI service that wraps Helm CLI operations
3. **User Workspaces**: Isolated Kubernetes namespaces for each workspace's services

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           K8s Cluster (K3s)                             │
│                                                                         │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐          │
│  │   Odoo Pod   │      │ PaaS Operator│      │ User Services│          │
│  │  (Frontend)  │─────▶│   (FastAPI)  │─────▶│   (Helm)     │          │
│  │              │ HTTP │              │ Helm │              │          │
│  └──────────────┘      └──────────────┘      └──────────────┘          │
│         │                     │                     │                   │
│         ▼                     ▼                     ▼                   │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐          │
│  │  PostgreSQL  │      │ ServiceAccount│      │  Namespaces  │          │
│  │  (Metadata)  │      │ + RBAC       │      │  (paas-ws-*) │          │
│  └──────────────┘      └──────────────┘      └──────────────┘          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Step 1: Create System Namespace

Create a dedicated namespace for PaaS system components:

```bash
kubectl create namespace paas-system
```

## Step 2: Generate API Key

Generate a secure API key for communication between Odoo and PaaS Operator:

```bash
# Generate API key
API_KEY=$(openssl rand -hex 32)
echo "Save this API key for Odoo configuration: $API_KEY"
echo ""
echo "Store this key securely - you'll need it in Step 3 (Helm install) and Step 4 (Odoo config)!"
```

> **Note**: The Kubernetes secret will be created automatically by Helm in Step 3.

## Step 3: Deploy PaaS Operator Service

The PaaS Operator is deployed using Helm chart. This automatically creates:

- ServiceAccount with RBAC permissions (scoped to `paas-ws-*` namespaces)
- Kubernetes Secret for API key
- Deployment and Service

### 3.1 Install with Helm

```bash
cd extra/paas-operator

# Install PaaS Operator using Helm
helm install paas-operator ./helm \
  --namespace paas-system \
  --set auth.apiKey=$API_KEY
```

Alternatively, create a `values-override.yaml` file:

```yaml
# values-override.yaml
auth:
  apiKey: "your-generated-api-key-here"

resources:
  limits:
    cpu: 500m
    memory: 512Mi
  requests:
    cpu: 100m
    memory: 128Mi
```

Then install with:

```bash
helm install paas-operator ./helm \
  --namespace paas-system \
  -f values-override.yaml
```

### 3.2 Verify Deployment

```bash
# Check if pod is running
kubectl get pods -n paas-system -l app.kubernetes.io/name=paas-operator

# Expected output:
# NAME                              READY   STATUS    RESTARTS   AGE
# paas-operator-xxxxxxxxxx-xxxxx    1/1     Running   0          30s

# Check service
kubectl get svc -n paas-system paas-operator

# Test health endpoint from within cluster
kubectl run curl-test --rm -it --restart=Never --image=curlimages/curl -- \
  curl http://paas-operator.paas-system.svc/health

# Expected output:
# {"status":"healthy","helm_version":"v3.13.0","timestamp":"2024-02-02T10:30:45Z"}
```

If the pod is not running, check logs:

```bash
kubectl logs -n paas-system -l app.kubernetes.io/name=paas-operator --tail=50
```

## Step 4: Configure Odoo Integration

### 4.1 Access Odoo Admin Settings

1. Login to Odoo as administrator
2. Navigate to: **Settings → General Settings**
3. Scroll down to **Woow PaaS** section

### 4.2 Configure PaaS Operator Connection

Set the following configuration:

| Field                 | Value                                   | Notes                              |
| --------------------- | --------------------------------------- | ---------------------------------- |
| **PaaS Operator URL** | `http://paas-operator.paas-system.svc`  | Internal K8s service DNS (port 80) |
| **API Key**           | `<API_KEY from Step 2>`                 | The key you generated with openssl |

Example:

```
PaaS Operator URL: http://paas-operator.paas-system.svc
API Key: a3f2b8c9d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1
```

### 4.3 Save Configuration

Click **Save** to apply the settings.

## Step 5: Verify Integration

### 5.1 Test Connection from Odoo

1. Navigate to **Woow PaaS → Dashboard**
2. Access any existing workspace or create a new one
3. Click **"Browse Marketplace"** button
4. You should see application templates (AnythingLLM, n8n, PostgreSQL, etc.)

If templates don't appear:

- Check PaaS Operator logs: `kubectl logs -n paas-system -l app.kubernetes.io/name=paas-operator`
- Verify API key matches in both Odoo settings and K8s secret
- Test connectivity from Odoo pod (see Step 5.3)

### 5.2 Test Service Deployment

Create a test service to verify end-to-end functionality:

1. In the Marketplace modal, select **PostgreSQL**
2. Fill in the configuration:
   - Name: `test-postgres`
   - Version: `16.1.0`
   - Storage: `1Gi`
3. Click **Deploy**
4. Wait for deployment to complete (status should change to "Running")

### 5.3 Troubleshooting Connection Issues

If Odoo cannot connect to PaaS Operator:

```bash
# Get Odoo pod name
ODOO_POD=$(kubectl get pods -n odoo-namespace -l app=odoo -o jsonpath='{.items[0].metadata.name}')

# Test connectivity from Odoo pod
kubectl exec -it $ODOO_POD -n odoo-namespace -- \
  curl -H "X-API-Key: YOUR_API_KEY" \
  http://paas-operator.paas-system.svc/health

# Check DNS resolution
kubectl exec -it $ODOO_POD -n odoo-namespace -- \
  nslookup paas-operator.paas-system.svc
```

## Step 6: Configure Ingress (Optional)

If you want services to be accessible via custom domains:

### 6.1 Ensure Ingress Controller is Running

K3s comes with Traefik by default:

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=traefik
```

### 6.2 Configure DNS Wildcard

Point your wildcard domain to the cluster's external IP:

```
*.woowtech.io → <CLUSTER_EXTERNAL_IP>
```

For Cloudflare DNS:

1. Go to DNS settings
2. Add A record: `*` → `<CLUSTER_EXTERNAL_IP>`
3. Enable proxy (orange cloud) for DDoS protection

### 6.3 Test Ingress

After deploying a service with custom subdomain:

```bash
# Check ingress was created
kubectl get ingress -n paas-ws-1

# Test access
curl https://test-app.woowtech.io
```

## Step 7: Production Considerations

### 7.1 Resource Quotas

Each workspace namespace should have resource quotas to prevent resource exhaustion:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: workspace-quota
  namespace: paas-ws-1
spec:
  hard:
    requests.cpu: "4"
    requests.memory: "8Gi"
    requests.storage: "50Gi"
    persistentvolumeclaims: "10"
```

The PaaS Operator automatically creates these quotas when namespaces are created.

### 7.2 Network Policies

Implement network policies to isolate workspaces:

```bash
kubectl apply -f k8s/network-policies.yaml
```

### 7.3 Backup Strategy

- **Odoo Database**: Regular PostgreSQL backups
- **User Service Data**: Velero for PVC backups
- **Configuration**: Store K8s manifests in Git

### 7.4 Monitoring

Deploy monitoring stack:

```bash
# Prometheus + Grafana
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install monitoring prometheus-community/kube-prometheus-stack -n monitoring --create-namespace
```

Monitor:

- PaaS Operator pod health
- Workspace resource usage
- Helm release status

### 7.5 Log Aggregation

Use Loki for centralized logging:

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm install loki grafana/loki-stack -n monitoring
```

## Upgrade Procedure

### Upgrading PaaS Operator

```bash
cd extra/paas-operator

# Pull latest changes
git pull origin main

# Upgrade with Helm
helm upgrade paas-operator ./helm \
  --namespace paas-system \
  --reuse-values

# Or upgrade with new values
helm upgrade paas-operator ./helm \
  --namespace paas-system \
  --set image.tag=v1.2.0

# Verify rollout
kubectl rollout status deployment/paas-operator -n paas-system
```

### Upgrading Odoo Module

```bash
# Update module in Odoo pod
kubectl exec -it $ODOO_POD -n odoo-namespace -- \
  odoo -u woow_paas_platform --stop-after-init

# Restart Odoo
kubectl rollout restart deployment/odoo -n odoo-namespace
```

## Uninstallation

To remove the PaaS platform:

```bash
# 1. Delete all user workspaces
kubectl delete namespace -l woow-paas=workspace

# 2. Uninstall PaaS Operator with Helm (removes RBAC, secrets, deployment, service)
helm uninstall paas-operator -n paas-system

# 3. Delete system namespace
kubectl delete namespace paas-system
```

## Next Steps

- Review [Troubleshooting Guide](troubleshooting.md) for common issues
- Read [Developer Getting Started](../development/getting-started.md) to customize the platform
- Check [Application Marketplace Spec](../spec/cloud-services.md) to add custom templates

## Support

For issues or questions:

- GitHub Issues: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues
- Documentation: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/tree/main/docs
