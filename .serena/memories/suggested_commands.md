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

## Docker Development (Recommended)

### Start dev environment
```bash
./scripts/start-dev.sh
```

### Run tests in Docker
```bash
./scripts/test-addon.sh
```

### Clean up environment
```bash
./scripts/cleanup-worktree.sh
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
- http://localhost (NOT :8069, to enable websocket)
