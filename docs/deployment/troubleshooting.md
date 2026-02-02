# Troubleshooting Guide

This guide covers common issues encountered when deploying and operating the WoowTech PaaS Platform.

## Table of Contents

- [PaaS Operator Issues](#paas-operator-issues)
- [Service Deployment Issues](#service-deployment-issues)
- [Networking Issues](#networking-issues)
- [Odoo Integration Issues](#odoo-integration-issues)
- [Helm Issues](#helm-issues)
- [Resource Issues](#resource-issues)
- [Debugging Tools](#debugging-tools)

---

## PaaS Operator Issues

### Issue: PaaS Operator Pod Not Starting

**Symptoms**: Pod is in `CrashLoopBackOff` or `Error` state

**Diagnosis**:
```bash
# Check pod status
kubectl get pods -n paas-system -l app=paas-operator

# View pod logs
kubectl logs -n paas-system -l app=paas-operator --tail=100

# Describe pod for events
kubectl describe pod -n paas-system -l app=paas-operator
```

**Common Causes & Solutions**:

| Cause | Solution |
|-------|----------|
| Missing API key in secret | Verify secret exists: `kubectl get secret paas-operator-secrets -n paas-system` |
| RBAC permissions missing | Apply RBAC: `kubectl apply -f k8s/rbac.yaml` |
| Helm binary not found | Check Dockerfile includes Helm installation |
| Python dependencies issue | Verify requirements.txt is complete, rebuild image |

**Fix Example**:
```bash
# Recreate secret with valid API key
kubectl delete secret paas-operator-secrets -n paas-system
kubectl create secret generic paas-operator-secrets \
  --from-literal=api-key=$(openssl rand -hex 32) \
  -n paas-system

# Restart deployment
kubectl rollout restart deployment/paas-operator -n paas-system
```

### Issue: PaaS Operator Returns 401 Unauthorized

**Symptoms**: Odoo shows "Authentication failed" when connecting to PaaS Operator

**Diagnosis**:
```bash
# Check the API key in secret
kubectl get secret paas-operator-secrets -n paas-system -o jsonpath='{.data.api-key}' | base64 -d

# Test with correct API key
kubectl run curl-test --rm -it --restart=Never --image=curlimages/curl -- \
  curl -H "X-API-Key: YOUR_KEY" http://paas-operator.paas-system.svc:8000/health
```

**Common Causes**:
- API key mismatch between K8s secret and Odoo settings
- Missing `X-API-Key` header in requests
- Incorrect secret name in deployment

**Solution**:
```bash
# Get current API key
CURRENT_KEY=$(kubectl get secret paas-operator-secrets -n paas-system -o jsonpath='{.data.api-key}' | base64 -d)
echo "Current API Key: $CURRENT_KEY"

# Update Odoo settings with this exact key
# Go to Settings → General Settings → Woow PaaS section
```

---

## Service Deployment Issues

### Issue: Service Stuck in "Deploying" State

**Symptoms**: Service shows "Deploying" for more than 5 minutes, status never changes to "Running"

**Diagnosis**:
```bash
# Check PaaS Operator logs
kubectl logs -n paas-system -l app=paas-operator --tail=50

# Check Helm release status
helm list -n paas-ws-{workspace_id}

# Check pods in workspace namespace
kubectl get pods -n paas-ws-{workspace_id}

# Check pod events
kubectl describe pod -n paas-ws-{workspace_id}
```

**Common Causes**:

| Cause | Diagnosis Command | Solution |
|-------|------------------|----------|
| Image pull failure | `kubectl get pods -n paas-ws-X -o jsonpath='{.items[0].status.containerStatuses[0].state.waiting.reason}'` | Add imagePullSecrets or use public image |
| Resource quota exceeded | `kubectl describe resourcequota -n paas-ws-X` | Increase quota or reduce resource requests |
| PVC provisioning issue | `kubectl get pvc -n paas-ws-X` | Check storage class exists, increase timeout |
| Init container failing | `kubectl logs -n paas-ws-X <pod-name> -c init-container` | Fix init container configuration |

**Fix Example**:
```bash
# If image pull issue, create imagePullSecret
kubectl create secret docker-registry regcred \
  --docker-server=registry.example.com \
  --docker-username=user \
  --docker-password=pass \
  -n paas-ws-{workspace_id}

# Patch deployment to use secret
kubectl patch deployment <deployment-name> -n paas-ws-{workspace_id} \
  -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}}}'
```

### Issue: Service Shows "Running" but Not Accessible

**Symptoms**: Service status is "Running", pods are healthy, but application is unreachable

**Diagnosis**:
```bash
# Check service endpoint
kubectl get svc -n paas-ws-{workspace_id}

# Check ingress
kubectl get ingress -n paas-ws-{workspace_id}

# Test service from within cluster
kubectl run curl-test --rm -it --restart=Never --image=curlimages/curl -- \
  curl http://<service-name>.paas-ws-{workspace_id}.svc:<port>

# Check pod logs
kubectl logs -n paas-ws-{workspace_id} -l app=<app-name>
```

**Common Causes**:
- Service port mismatch
- Ingress not created or misconfigured
- Application not listening on expected port
- Network policy blocking traffic

**Solution**:
```bash
# Verify service ports
kubectl get svc <service-name> -n paas-ws-{workspace_id} -o yaml

# Check ingress configuration
kubectl describe ingress -n paas-ws-{workspace_id}

# Test pod directly (port-forward)
kubectl port-forward -n paas-ws-{workspace_id} <pod-name> 8080:8080
# Then access http://localhost:8080
```

### Issue: Service Deployment Fails with Helm Error

**Symptoms**: Service shows "Error" state, Odoo logs show Helm command failure

**Diagnosis**:
```bash
# Check PaaS Operator logs for detailed error
kubectl logs -n paas-system -l app=paas-operator | grep "ERROR"

# Check Helm release
helm list -n paas-ws-{workspace_id}
helm status <release-name> -n paas-ws-{workspace_id}

# Check Helm history
helm history <release-name> -n paas-ws-{workspace_id}
```

**Common Causes**:
- Invalid Helm chart values
- Chart repository unreachable
- Namespace quota exceeded
- RBAC permissions insufficient

**Solution**:
```bash
# Verify RBAC permissions
kubectl auth can-i create deployments \
  --as system:serviceaccount:paas-system:paas-operator \
  -n paas-ws-{workspace_id}

# Manually uninstall failed release
helm uninstall <release-name> -n paas-ws-{workspace_id}

# Try deployment again from Odoo UI
```

---

## Networking Issues

### Issue: Ingress Returns 404 Not Found

**Symptoms**: Accessing service URL returns 404 error

**Diagnosis**:
```bash
# Check if ingress exists
kubectl get ingress -n paas-ws-{workspace_id}

# Check ingress configuration
kubectl describe ingress -n paas-ws-{workspace_id}

# Check Traefik/ingress controller logs
kubectl logs -l app.kubernetes.io/name=traefik -n kube-system --tail=50

# Verify DNS resolution
nslookup {subdomain}.woowtech.com
```

**Common Causes**:
- Ingress not created (missing in Helm chart)
- Wrong ingress class
- DNS not pointing to cluster
- Ingress controller not running

**Solution**:
```bash
# Check ingress controller is running
kubectl get pods -n kube-system -l app.kubernetes.io/name=traefik

# Verify ingress class
kubectl get ingressclass

# Test ingress with curl
curl -v https://{subdomain}.woowtech.com
```

### Issue: Connection Refused to PaaS Operator

**Symptoms**: Odoo shows "Failed to connect to PaaS Operator" or connection timeout

**Diagnosis**:
```bash
# Verify operator pod is running
kubectl get pods -n paas-system -l app=paas-operator

# Verify service exists
kubectl get svc paas-operator -n paas-system

# Test connectivity from Odoo pod
ODOO_POD=$(kubectl get pods -n <odoo-namespace> -l app=odoo -o jsonpath='{.items[0].metadata.name}')
kubectl exec -it $ODOO_POD -n <odoo-namespace> -- \
  curl http://paas-operator.paas-system.svc:8000/health

# Check network policies
kubectl get networkpolicies -n paas-system
kubectl get networkpolicies -n <odoo-namespace>
```

**Common Causes**:
- PaaS Operator pod crashed
- Wrong URL in Odoo settings
- Network policy blocking traffic
- Service port mismatch

**Solution**:
```bash
# Verify correct URL in Odoo settings
# Should be: http://paas-operator.paas-system.svc:8000

# Check service port
kubectl get svc paas-operator -n paas-system -o yaml

# Restart operator if needed
kubectl rollout restart deployment/paas-operator -n paas-system
```

---

## Odoo Integration Issues

### Issue: Application Templates Not Showing in Marketplace

**Symptoms**: Clicking "Browse Marketplace" shows no templates or loading error

**Diagnosis**:
```bash
# Check browser console for errors
# Check Odoo logs
docker compose logs web | grep -i "cloud"

# Test PaaS Operator API directly
kubectl run curl-test --rm -it --restart=Never --image=curlimages/curl -- \
  curl -H "X-API-Key: YOUR_KEY" \
  http://paas-operator.paas-system.svc:8000/api/templates
```

**Common Causes**:
- PaaS Operator not configured in Odoo settings
- Incorrect API key
- JavaScript error in frontend
- PaaS Operator service down

**Solution**:
1. Verify Odoo Settings:
   - Go to Settings → General Settings → Woow PaaS
   - Ensure URL is: `http://paas-operator.paas-system.svc:8000`
   - Ensure API key matches K8s secret
2. Check browser console for JavaScript errors
3. Restart Odoo if needed

### Issue: Service Status Not Updating in Odoo

**Symptoms**: Service deployed successfully but Odoo still shows "Deploying"

**Diagnosis**:
```bash
# Check service status in K8s
kubectl get pods -n paas-ws-{workspace_id}
helm status <release-name> -n paas-ws-{workspace_id}

# Check Odoo logs for status update errors
docker compose logs web | grep "status"
```

**Common Causes**:
- Status polling not working
- PaaS Operator returning incorrect status
- Database connection issue

**Solution**:
```bash
# Manually trigger status refresh in Odoo
# Click "Refresh" button in service detail page

# Check PaaS Operator status endpoint
kubectl run curl-test --rm -it --restart=Never --image=curlimages/curl -- \
  curl -H "X-API-Key: YOUR_KEY" \
  http://paas-operator.paas-system.svc:8000/api/releases/paas-ws-X/<service-name>/status
```

---

## Helm Issues

### Issue: Helm Command Timeout

**Symptoms**: PaaS Operator logs show "Helm command timeout" error

**Diagnosis**:
```bash
# Check HELM_TIMEOUT setting
kubectl get configmap -n paas-system

# Check resource availability
kubectl top nodes
kubectl describe node
```

**Common Causes**:
- Image pull taking too long
- Pod startup probe timing out
- Insufficient cluster resources

**Solution**:
```bash
# Increase Helm timeout in operator config
# Edit deployment and add env var:
kubectl set env deployment/paas-operator \
  HELM_TIMEOUT=600 \
  -n paas-system

# Or update k8s/deployment.yaml and reapply
```

### Issue: Helm Chart Not Found

**Symptoms**: Service deployment fails with "chart not found" error

**Diagnosis**:
```bash
# Test Helm repo access from operator pod
kubectl exec -it <paas-operator-pod> -n paas-system -- \
  helm search repo bitnami/postgresql

# Check if chart repo is added
kubectl exec -it <paas-operator-pod> -n paas-system -- \
  helm repo list
```

**Common Causes**:
- Chart repository not added
- Network issue accessing chart repo
- Chart version doesn't exist

**Solution**:
```bash
# Add missing chart repositories in operator startup script
# Edit src/main.py to add repo on startup:
# helm.run_command(["repo", "add", "bitnami", "https://charts.bitnami.com/bitnami"])
# helm.run_command(["repo", "update"])
```

---

## Resource Issues

### Issue: Namespace Quota Exceeded

**Symptoms**: Service deployment fails with "exceeded quota" error

**Diagnosis**:
```bash
# Check namespace resource quota
kubectl describe resourcequota -n paas-ws-{workspace_id}

# Check current resource usage
kubectl top pods -n paas-ws-{workspace_id}
```

**Common Causes**:
- Too many services in one workspace
- Service requesting more resources than available
- Default quota too restrictive

**Solution**:
```bash
# Increase namespace quota
kubectl patch resourcequota workspace-quota -n paas-ws-{workspace_id} \
  --type='json' -p='[{"op": "replace", "path": "/spec/hard/requests.cpu", "value":"8"}]'

# Or delete services to free up resources
helm uninstall <old-service> -n paas-ws-{workspace_id}
```

### Issue: PVC Not Binding

**Symptoms**: Pod stuck in "Pending" state, events show "FailedScheduling: persistentvolumeclaim not found"

**Diagnosis**:
```bash
# Check PVC status
kubectl get pvc -n paas-ws-{workspace_id}

# Check storage classes
kubectl get storageclass

# Describe PVC for events
kubectl describe pvc -n paas-ws-{workspace_id}
```

**Common Causes**:
- No storage class available
- Storage provisioner not running
- Storage quota exceeded

**Solution**:
```bash
# Check if storage provisioner is running (K3s local-path)
kubectl get pods -n kube-system -l app=local-path-provisioner

# Create default storage class if missing
kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-path
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: rancher.io/local-path
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
EOF
```

---

## Debugging Tools

### Essential kubectl Commands

```bash
# View all resources in namespace
kubectl get all -n paas-ws-{workspace_id}

# Get pod logs
kubectl logs <pod-name> -n paas-ws-{workspace_id}

# Follow pod logs in real-time
kubectl logs -f <pod-name> -n paas-ws-{workspace_id}

# Exec into pod for debugging
kubectl exec -it <pod-name> -n paas-ws-{workspace_id} -- /bin/sh

# Port forward to access service locally
kubectl port-forward svc/<service-name> 8080:8080 -n paas-ws-{workspace_id}

# Check pod events
kubectl get events -n paas-ws-{workspace_id} --sort-by='.lastTimestamp'
```

### Helm Debugging

```bash
# List all releases in namespace
helm list -n paas-ws-{workspace_id}

# Get release status
helm status <release-name> -n paas-ws-{workspace_id}

# Get release history
helm history <release-name> -n paas-ws-{workspace_id}

# Get release values
helm get values <release-name> -n paas-ws-{workspace_id}

# Dry run to test chart
helm install <name> <chart> --dry-run --debug -n paas-ws-{workspace_id}
```

### PaaS Operator Debugging

```bash
# View real-time logs
kubectl logs -f -n paas-system -l app=paas-operator

# Search for errors
kubectl logs -n paas-system -l app=paas-operator | grep -i error

# Check API health
kubectl exec -it <paas-operator-pod> -n paas-system -- \
  curl http://localhost:8000/health

# Check Helm version
kubectl exec -it <paas-operator-pod> -n paas-system -- \
  helm version
```

---

## Getting Help

If you're still experiencing issues after trying these solutions:

1. **Collect diagnostic information**:
   ```bash
   # Save all relevant logs
   kubectl logs -n paas-system -l app=paas-operator > paas-operator.log
   kubectl get all -n paas-ws-{workspace_id} -o yaml > workspace-resources.yaml
   helm list -n paas-ws-{workspace_id} > helm-releases.txt
   ```

2. **Check GitHub Issues**: Search for similar problems at https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues

3. **Create a new issue** with:
   - Description of the problem
   - Steps to reproduce
   - Logs and diagnostic output
   - Environment details (K8s version, Helm version, etc.)

4. **Review documentation**:
   - [K8s Setup Guide](k8s-setup.md)
   - [Developer Guide](../development/getting-started.md)
   - [Cloud Services Spec](../spec/cloud-services.md)
