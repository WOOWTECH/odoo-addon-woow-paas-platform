# Woow PaaS Platform - Dev Container

此資料夾包含 Odoo 18 開發環境的 VS Code Dev Container 配置。

## 前置需求

- Docker Desktop（或 Docker Engine + Docker Compose）
- VS Code 搭配「Dev Containers」擴充套件（`ms-vscode-remote.remote-containers`）

## 快速開始

1. 在 VS Code 中開啟 `woow_paas_platform` 資料夾
2. 按下 `F1` 並選擇「Dev Containers: Reopen in Container」
3. 等待容器建置並啟動
4. 透過 `http://localhost:8069` 訪問 Odoo

## 服務清單

| 服務    | 埠號 | 說明 |
|---------|------|------|
| Odoo    | 8069 | 主要 Odoo 應用程式 |
| Nginx   | 8000 | 反向代理（選用，需使用 `--profile full`） |
| PgAdmin | 5050 | 資料庫管理介面（選用，需使用 `--profile full`） |

## 執行選用服務

若要包含 Nginx 和 PgAdmin：

```bash
docker compose --profile full up -d
```

## 資料庫憑證

- **主機：** db
- **使用者：** odoo
- **密碼：** odoo

## PgAdmin 憑證（啟用時）

- **Email：** admin@woow.com
- **密碼：** admin

## 開發模式

Odoo 配置包含開發模式，具備：
- 熱重載（`dev_mode = reload,qweb,xml`）
- 除錯日誌

## 常用指令

```bash
# 啟動服務
docker compose up -d

# 檢視日誌
docker compose logs -f web

# 重啟 Odoo
docker compose restart web

# 更新模組
docker compose exec web odoo -u woow_paas_platform -d <資料庫名稱>

# 安裝模組
docker compose exec web odoo -i woow_paas_platform -d <資料庫名稱>

# 停止服務
docker compose down

# 停止服務並移除 volumes
docker compose down -v
```
