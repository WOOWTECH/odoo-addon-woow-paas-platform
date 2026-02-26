---
name: deploy-local
description: Copy the addon source to local Odoo addons directory and restart the Odoo container.
usage: |
  Use when:
  (1) User wants to deploy/publish the addon to the local Odoo test environment
  (2) User wants to test the addon in the local Odoo instance
  (3) User says "deploy", "publish", "發佈", "部署" related to local testing

  Keywords: deploy, publish, local, 發佈, 部署, 測試
---

# Deploy Addon to Local Odoo

將 `src/` 資料夾複製到本地 Odoo 測試環境的 addons 目錄，並重啟 Odoo 容器。

## Configuration

- **Addon source**: `src/` (relative to project root)
- **Addon name**: Derived from project root directory name (e.g. `woow_paas_platform`)
- **Target addons dir**: `/Users/eugene/Documents/woow/AREA-odoo/odoo-server/data/18/addons`
- **Docker compose dir**: `/Users/eugene/Documents/woow/AREA-odoo/odoo-server`
- **Docker compose file**: `docker-compose-18.yml`

## Steps

### Step 1: Determine project root and addon name

Find the project root directory (where `src/` is located). The addon name is the project root directory name — this follows the Odoo convention where the repo/directory name equals the addon's technical name.

```bash
PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
ADDON_NAME=$(basename "$PROJECT_ROOT")
```

### Step 2: Copy addon to target

```bash
TARGET_DIR="/Users/eugene/Documents/woow/AREA-odoo/odoo-server/data/18/addons"

# Remove old version if exists
rm -rf "${TARGET_DIR}/${ADDON_NAME}"

# Copy src/ as the addon directory
cp -r "${PROJECT_ROOT}/src" "${TARGET_DIR}/${ADDON_NAME}"

echo "Copied src/ -> ${TARGET_DIR}/${ADDON_NAME}"
```

### Step 3: Restart Odoo container

```bash
ODOO_DIR="/Users/eugene/Documents/woow/AREA-odoo/odoo-server"

docker compose -f "${ODOO_DIR}/docker-compose-18.yml" restart web
```

### Step 4: Confirm

```bash
say -r 180 "主人，addon 已經部署到本地 Odoo 環境囉"
```

Report the result to the user with the test URL: http://localhost
