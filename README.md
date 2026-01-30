# Woow PaaS Platform

Odoo 18 addon module providing the foundation for a multi-tenant PaaS application.

## Features

- **Standalone OWL Application** - Independent frontend at `/woow` with hash-based routing
- **Configuration Settings** - System settings via `res.config.settings`
- **Menu Structure** - Root menu "Woow PaaS" ready for child items
- **Claude Code PM** - Project management integration

## Quick Start

### Standard Development

```bash
# 1. Setup environment
./scripts/setup-worktree-env.sh

# 2. Start Odoo
./scripts/start-dev.sh

# 3. Access application
open http://localhost:8069
```

### Worktree Development

Support for parallel testing with multiple worktrees:

```bash
# Create worktree
git worktree add ../woow_paas_platform.worktrees/epic-feature -b epic/feature

# Switch to worktree
cd ../woow_paas_platform.worktrees/epic-feature

# Start (auto-configured with unique port and database)
./scripts/start-dev.sh
# → http://localhost:8234 (or other unique port)
```

## Available Scripts

| Script | Description |
|--------|-------------|
| `./scripts/setup-worktree-env.sh` | Auto-configure environment variables |
| `./scripts/start-dev.sh` | Start Odoo development environment |
| `./scripts/test-addon.sh` | Run addon tests |
| `./scripts/cleanup-worktree.sh` | Clean up environment |

## Environment Configuration

Each worktree automatically gets:
- **Unique port** (8069 + directory hash) - Avoid conflicts
- **Isolated database** (`woow_<branch>`) - Data isolation
- **Separate containers** (based on `COMPOSE_PROJECT_NAME`)

All configuration managed via `.env` file (auto-generated).

## Parallel Testing

Run multiple worktrees simultaneously:

```bash
# Terminal 1 - Main project
cd /path/to/woow_paas_platform
./scripts/start-dev.sh
# → http://localhost:8069 (woow_main)

# Terminal 2 - Feature A
cd ../woow_paas_platform.worktrees/epic-feature-a
./scripts/start-dev.sh
# → http://localhost:8234 (woow_epic_feature_a)

# Terminal 3 - Feature B
cd ../woow_paas_platform.worktrees/fix-bug-123
./scripts/start-dev.sh
# → http://localhost:8501 (woow_fix_bug_123)
```

## VS Code Development

- Uses **local VS Code** (not Dev Container)
- Each worktree can open separate VS Code window
- Odoo runs in Docker container
- Optional remote debugging (Python debugger attach to container)

Recommended extensions configured in `.vscode/extensions.json`.

## Docker Services

### Standard Mode (Default)
Each worktree has its own PostgreSQL container.

```bash
docker compose up -d
```

### Shared Database Mode (Optional)
All worktrees share one PostgreSQL container to save resources.

```bash
# 1. Start shared PostgreSQL
docker compose -f docker-compose.shared-db.yml up -d

# 2. Configure each worktree
echo "USE_SHARED_DB=true" >> .env
echo "POSTGRES_HOST=odoo_postgres_shared" >> .env

# 3. Start worktree
./scripts/start-dev.sh
```

## Common Commands

```bash
# View logs
docker compose logs -f web

# Restart service
docker compose restart web

# Update module
docker compose exec web odoo -d woow_main -u woow_paas_platform --dev xml

# Stop services
docker compose stop

# Full cleanup (with volumes)
./scripts/cleanup-worktree.sh
```

## Project Structure

```
woow_paas_platform/
├── scripts/              # Development automation scripts
├── config/               # Odoo configuration
├── src/                  # Addon source code
├── .vscode/              # VS Code settings
├── .env.example          # Environment template
└── docker-compose.yml    # Docker services
```

## Documentation

See [CLAUDE.md](CLAUDE.md) for comprehensive development guide.

## Requirements

- Docker & Docker Compose
- Git
- VS Code (recommended)

## License

Proprietary - Woow PaaS Platform
