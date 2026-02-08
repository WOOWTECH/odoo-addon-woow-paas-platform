---
name: Setup End-to-End Worktree Development
description: Complete guide for setting up worktree development environment and testing Odoo addons
usage: |
  Use this skill when:
  - Starting a new feature development in a worktree
  - Setting up parallel testing environments
  - Need step-by-step guide from worktree creation to addon testing

  Invoke with: "Set up end-to-end worktree development for epic/feature-name"
  Keywords: end-to-end testing, e2e, ui testing, 網頁測試
---

# Setup End-to-End Worktree Development

完整的 worktree 開發環境設定與測試流程。

## 前置需求

- Git repository 已初始化
- Docker (or podman) 和 Docker Compose 已安裝
- 專案已包含 worktree 自動化腳本（`scripts/` 目錄）

## 完整流程

### 步驟 1：建立 Worktree

```bash
# 從主專案目錄執行
cd /path/to/woow_paas_platform

# 建立 worktree（替換 <feature-name> 為實際的功能名稱）
git worktree add ../woow_paas_platform.worktrees/<feature-name> -b epic/<feature-name>

# 範例：
# git worktree add ../woow_paas_platform.worktrees/workspace-api -b epic/workspace-api
```

**驗證**：

```bash
# 確認 worktree 已建立
git worktree list
```

### 步驟 2：切換到 Worktree

```bash
cd ../woow_paas_platform.worktrees/<feature-name>
```

### 步驟 3：自動設定環境

```bash
# 執行環境設定腳本（自動生成 .env）
./scripts/setup-worktree-env.sh
```

**腳本會自動：**

- 偵測 git branch 名稱
- 計算唯一的 port（避免與其他 worktree 衝突）
- 生成資料庫名稱（`woow_<branch>`）
- 建立 `.env` 檔案

**預期輸出範例**：

```
✅ Worktree 環境配置完成
  Branch:       epic/workspace-api
  Project:      woow_paas_platform_epic_workspace_api
  Port:         8234
  Database:     woow_epic_workspace_api
```

### 步驟 4：啟動開發環境

```bash
# 啟動 Docker 容器（Odoo + PostgreSQL）
./scripts/start-dev.sh
```

**腳本會自動：**

1. 檢查 `.env` 配置
2. 從模板生成 `odoo.conf`（使用 envsubst）
3. 啟動 Docker Compose
4. 等待 Odoo 服務就緒
5. **自動建立資料庫並安裝 `base` + `woow_paas_platform`**（首次啟動）
6. 顯示訪問 URL

**預期輸出範例**：

```
🚀 啟動 Odoo 開發環境...
✓ 已從模板生成 odoo.conf
📦 啟動 Docker 容器...
✅ Odoo 服務已就緒！

🔍 檢查資料庫 woow_epic_workspace_api...
  等待 PostgreSQL... 就緒
📦 資料庫不存在，正在建立並安裝模組...
   （首次啟動需要 1-3 分鐘，請耐心等候）
✅ 資料庫建立完成！

訪問 Odoo：
  http://localhost:8234

資料庫名稱：
  woow_epic_workspace_api
```

**驗證容器狀態**：

```bash
# 檢查容器是否運行
docker compose ps

# 查看 Odoo 日誌
docker compose logs -f web
```

### 步驟 5：訪問 Odoo

在瀏覽器開啟：`http://localhost:<PORT>`（使用 setup 顯示的 port）

**自動建立的環境**：

- 資料庫已自動建立（名稱依據 branch，如 `woow_epic_workspace_api`）
- `woow_paas_platform` 模組已自動安裝
- 語言已設定為繁體中文（zh_TW）
- 無 demo 資料（乾淨環境）

**登入資訊**：

- **Email**: `admin`
- **Password**: `admin`

> **注意**：如果看到「Create Database」頁面，表示自動建立失敗。請查看日誌：
>
> ```bash
> docker compose logs web | tail -50
> ```

### 步驟 6：開發與即時更新

目前的 `dev_mode` 設定為 `reload,qweb,xml`，大部分修改會**自動重載**。

#### 自動重載（無需操作）

| 修改類型      | 說明                                    |
| ------------- | --------------------------------------- |
| Python 程式碼 | `dev_mode=reload` 自動偵測並重載        |
| XML 視圖      | `dev_mode=xml` 自動重載                 |
| QWeb 模板     | `dev_mode=qweb` 自動重載                |
| JS/CSS/SCSS   | 刷新瀏覽器即可（Ctrl+Shift+R 清除快取） |

#### 需要手動更新模組（`-u`）

以下情況需要執行更新指令：

```bash
# 更新模組
docker compose exec web odoo -d <DB_NAME> -u woow_paas_platform --stop-after-init
```

| 修改類型          | 說明                                |
| ----------------- | ----------------------------------- |
| `__manifest__.py` | 修改模組依賴、版本等                |
| 新增/修改欄位     | models 中新增 `fields.Char()` 等    |
| 新增模型          | 新增 `class NewModel(models.Model)` |
| data/\*.xml       | 初始資料檔案                        |
| security/\*.csv   | 權限設定                            |

#### 查看即時日誌

```bash
docker compose logs -f web
```

#### 完全重啟（如果自動重載失效）

```bash
docker compose restart web
```

### 步驟 7：執行 Addon 測試

```bash
# 執行完整的 addon 測試套件
./scripts/test-addon.sh
```

**手動執行測試（進階）**：

```bash
# 進入容器執行測試
docker compose exec web odoo \
  --test-enable \
  --test-tags woow_paas_platform \
  --stop-after-init \
  --log-level=test \
  -d woow_epic_workspace_api
```

**測試特定模組**：

```bash
# 測試特定的 Python 檔案
docker compose exec web odoo \
  --test-enable \
  --test-tags /woow_paas_platform/models \
  --stop-after-init \
  -d woow_epic_workspace_api
```

**驗證測試結果**：

- ✅ 所有測試通過：繼續開發
- ❌ 測試失敗：查看日誌修復問題

### 步驟 8：並行測試（可選）

如需同時測試多個功能，可在不同終端機啟動多個 worktree：

```bash
# Terminal 1 - Feature A
cd ../woow_paas_platform.worktrees/feature-a
./scripts/start-dev.sh
# → http://localhost:8234

# Terminal 2 - Feature B
cd ../woow_paas_platform.worktrees/feature-b
./scripts/start-dev.sh
# → http://localhost:8501
```

每個 worktree 使用：

- 不同的 port（自動分配）
- 獨立的資料庫（資料隔離）
- 獨立的 Docker 容器

### 步驟 9：VS Code 開發（可選）

```bash
# 在 worktree 目錄開啟 VS Code
code .
```

**推薦設定**：

- 安裝推薦擴充套件（`.vscode/extensions.json`）
- 使用 Python 虛擬環境（如需本機 linting）
- 配置遠端調試（`.vscode/launch.json`）

### 步驟 10：提交變更

```bash
# 查看變更
git status
git diff

# 暫存變更
git add <files>

# 提交（遵循 commit message 規範）
git commit -m "feat: add workspace API endpoints"

# 推送到遠端
git push -u origin epic/<feature-name>
```

### 步驟 11：清理環境

**完成開發後**：

```bash
# 停止容器（保留資料）
docker compose stop

# 或完全清理（包含資料庫）
./scripts/cleanup-worktree.sh
```

**刪除 worktree**（開發完成並合併後）：

```bash
# 返回主專案
cd /path/to/woow_paas_platform

# 刪除 worktree
git worktree remove ../woow_paas_platform.worktrees/<feature-name>

# 刪除遠端分支（如果已合併）
git push origin --delete epic/<feature-name>
```

## 常見問題排解

### 問題 1：Port 被占用

```bash
# 查看 port 占用
lsof -i :<PORT>

# 修改 .env 中的 ODOO_PORT
# 或停止衝突的服務
```

### 問題 2：容器啟動失敗

```bash
# 查看詳細日誌
docker compose logs web

# 重新啟動
docker compose down
docker compose up -d
```

### 問題 3：資料庫連線錯誤

```bash
# 確認 PostgreSQL 容器運行
docker compose ps db

# 檢查環境變數
cat .env | grep POSTGRES
```

### 問題 4：Addon 未顯示

```bash
# 確認 addon 路徑掛載
docker compose exec web ls -la /mnt/extra-addons/woow_paas_platform

# 重新啟動並更新 apps 列表
docker compose restart web
# 在 Odoo 介面：Apps → Update Apps List
```

### 問題 5：測試失敗

```bash
# 查看完整測試日誌
docker compose logs web | grep -A 20 "ERROR\|FAIL"

# 進入容器檢查
docker compose exec web bash
cd /mnt/extra-addons/woow_paas_platform
```

## 完整範例流程

```bash
# 1. 建立 worktree
cd ~/Documents/woow/AREA-odoo/woow-addons/woow_paas_platform
git worktree add ../woow_paas_platform.worktrees/workspace-api -b epic/workspace-api

# 2. 切換到 worktree 並啟動（自動設定環境 + 建立資料庫）
cd ../woow_paas_platform.worktrees/workspace-api
./scripts/start-dev.sh
# 首次啟動會自動：
# - 生成 .env 和 odoo.conf
# - 建立資料庫並安裝 woow_paas_platform

# 3. 訪問 Odoo（登入：admin / admin）
open http://localhost:8234

# 4. 開發...修改程式碼（大部分會自動重載）

# 5. 如有結構性變更，更新模組
docker compose exec web odoo -d woow_epic_workspace_api -u woow_paas_platform --stop-after-init

# 6. 執行測試
./scripts/test-addon.sh

# 7. 提交變更
git add .
git commit -m "feat: implement workspace CRUD API"
git push -u origin epic/workspace-api

# 8. 清理
docker compose stop
```

## 重要提醒

- **自動化**：`start-dev.sh` 會自動處理環境設定、資料庫建立、模組安裝
- **Port**：每個 worktree 自動分配唯一 port（8069-9068）
- **資料庫**：自動建立，名稱基於 branch（如 `woow_epic_feature`）
- **配置檔**：`odoo.conf` 由 `envsubst` 從模板生成，請修改 `.template` 檔
- **測試 URL**：使用 `http://localhost:<PORT>`
- **資源管理**：建議最多同時運行 3-4 個 worktree
- **自動重載**：`dev_mode=reload,qweb,xml` 讓大部分修改自動生效

## 進階選項

### 使用共享 PostgreSQL

節省資源，多個 worktree 共享一個 PostgreSQL：

```bash
# 1. 啟動共享資料庫（只需執行一次，在主專案目錄）
cd ~/Documents/woow/AREA-odoo/woow-addons/woow_paas_platform
docker compose -f docker-compose.shared-db.yml up -d

# 2. 在每個 worktree 設定環境（使用 --shared-db 參數）
cd ../woow_paas_platform.worktrees/<feature-name>
./scripts/setup-worktree-env.sh --shared-db

# 3. 啟動（不會建立獨立 PostgreSQL）
./scripts/start-dev.sh
```

### 遠端調試

```bash
# 1. 修改 docker-compose.yml 加入 debugpy
# 2. 在 .env 設定 DEBUG_PORT=5678
# 3. VS Code 使用 "Python: Attach to Odoo Container" 配置
```

---

**結束！** 現在你已經有一個完整的 worktree 開發與測試環境。
