# Suggested Commands

## Odoo Module Development

### Run Odoo with module
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform
```

### Update module after changes
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init
```

### Run Odoo module tests
```bash
./odoo-bin -c odoo.conf --test-enable --test-tags woow_paas_platform --stop-after-init
```

## K8s Dev Sandbox (Recommended)

### Onboard (one-time) + start sandbox
```bash
/woowprd:k8s-dev-sandbox onboard --repo "$(git rev-parse --show-toplevel)" --context docker-desktop
/woowprd:k8s-dev-sandbox up --context docker-desktop
```

### Run module tests in sandbox
```bash
/woowprd:k8s-dev-sandbox test <sandbox-name> --module woow_paas_platform
```

### Destroy sandbox
```bash
/woowprd:k8s-dev-sandbox destroy <sandbox-name> --context docker-desktop
```

## PaaS Operator Development

### Run PaaS Operator tests
```bash
cd extra/paas-operator
pytest tests/ -v --cov=src
```

### Port-forward PaaS Operator
```bash
kubectl port-forward -n paas-system svc/paas-operator 8000:80
```

### Get API key
```bash
kubectl get secret -n paas-system paas-operator-secret -o jsonpath='{.data.api-key}' | base64 -d
```

## Git / Worktree

### Create worktree for epic
```bash
git worktree add ../woow_paas_platform.worktrees/epic-feature -b epic/feature
```

### List worktrees
```bash
git worktree list
```

## System (Darwin)
- `git`, `ls`, `cd`, `grep`, `find` - standard unix commands
- `date -u +"%Y-%m-%dT%H:%M:%SZ"` - ISO 8601 datetime
- `sed`, `awk` - text processing

## Test URL
- `kubectl port-forward -n dev-sandbox-<slug> svc/<slug>-odoo-dev-sandbox-nginx 8080:80` → http://localhost:8080
