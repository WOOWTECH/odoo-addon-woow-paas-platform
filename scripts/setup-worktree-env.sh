#!/usr/bin/env bash
# Setup Worktree Environment
# 自動為當前 git worktree 設定環境變數
#
# 此腳本會：
# 1. 偵測當前的 git branch 名稱
# 2. 計算唯一的 port（避免多個 worktree 衝突）
# 3. 生成資料庫名稱（基於 branch 名稱）
# 4. 寫入 .env 檔案

set -euo pipefail

# 顏色輸出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 解析參數
USE_SHARED_DB=false
POSTGRES_HOST=db

while [[ $# -gt 0 ]]; do
    case $1 in
        --shared-db)
            USE_SHARED_DB=true
            POSTGRES_HOST=odoo_postgres_shared
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --shared-db    使用共享 PostgreSQL（連接到 odoo_postgres_shared）"
            echo "  -h, --help     顯示此說明"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# 取得專案根目錄（腳本所在目錄的上一層）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo -e "${BLUE}🔧 設定 Worktree 環境...${NC}"
echo ""

# 1. 偵測 Git Branch
if [ -d .git ]; then
    BRANCH_NAME=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
else
    # 可能是 worktree，嘗試讀取 .git 檔案
    if [ -f .git ]; then
        BRANCH_NAME=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
    else
        echo -e "${YELLOW}⚠️  警告：無法偵測 git branch，使用預設值 'main'${NC}"
        BRANCH_NAME="main"
    fi
fi

# 2. 計算 COMPOSE_PROJECT_NAME（基於目錄名稱）
DIR_NAME=$(basename "$PROJECT_ROOT")
if [ "$BRANCH_NAME" = "main" ]; then
    COMPOSE_PROJECT_NAME="$DIR_NAME"
else
    # 將 branch 名稱轉為合法的專案名稱（移除 / 和特殊字元）
    SAFE_BRANCH=$(echo "$BRANCH_NAME" | sed 's/[\/\-]/_/g')
    COMPOSE_PROJECT_NAME="${DIR_NAME}_${SAFE_BRANCH}"
fi

# 3. 計算唯一的 PORT（使用目錄路徑 hash）
# 基礎 port = 8069，加上目錄 hash 的後 3 位數（0-999）
if command -v md5sum >/dev/null 2>&1; then
    DIR_HASH=$(echo -n "$PROJECT_ROOT" | md5sum | cut -c1-3)
elif command -v md5 >/dev/null 2>&1; then
    # macOS
    DIR_HASH=$(echo -n "$PROJECT_ROOT" | md5 | cut -c1-3)
else
    echo -e "${YELLOW}⚠️  警告：無法計算 hash，使用預設 port 8069${NC}"
    DIR_HASH="000"
fi

# 將 hex 轉為 decimal，然後取模 1000
PORT_OFFSET=$((16#$DIR_HASH % 1000))
ODOO_PORT=$((8069 + PORT_OFFSET))

# 確保 port 在合理範圍內（8069-9068）
if [ "$ODOO_PORT" -lt 8069 ] || [ "$ODOO_PORT" -gt 9068 ]; then
    echo -e "${YELLOW}⚠️  警告：計算的 port $ODOO_PORT 超出範圍，使用 8069${NC}"
    ODOO_PORT=8069
fi

# 4. 生成資料庫名稱（將 branch 名稱轉為合法的 PostgreSQL 識別符）
# PostgreSQL 識別符規則：
# - 只能包含字母、數字、底線
# - 長度限制 63 字元
DB_NAME="woow_${BRANCH_NAME//[\/\-]/_}"
# 確保不超過 63 字元
if [ ${#DB_NAME} -gt 63 ]; then
    DB_NAME="${DB_NAME:0:63}"
    echo -e "${YELLOW}⚠️  警告：資料庫名稱過長，已截斷為 $DB_NAME${NC}"
fi

# 5. 讀取 .env.example 作為範本
if [ ! -f .env.example ]; then
    echo -e "${RED}❌ 錯誤：找不到 .env.example 檔案${NC}"
    exit 1
fi

# 5b. 探索第三方 addon 路徑（paas-odoo-ci/addons）→ 寫絕對路徑。
# 硬編相對 ../../../ 會因 clone/worktree 深度不同（例如 .maestri/floors/ 多兩層）
# 解析到錯誤目錄。改為從 PROJECT_ROOT 向上找「含 ai_base_gt 的
# woow-paas-repo/paas-odoo-ci/addons」——ai_base_gt 存在代表該目錄真的有第三方
# 依賴，藉此跳過同名但空殼的目錄。找不到才退回相對預設並警告。
_DEFAULT_CI_ADDONS="../../../woow-paas-repo/paas-odoo-ci/addons"
CI_ADDONS_PATH=""
_probe="$PROJECT_ROOT"
while [ "$_probe" != "/" ]; do
    if [ -d "$_probe/woow-paas-repo/paas-odoo-ci/addons/ai_base_gt" ]; then
        CI_ADDONS_PATH="$_probe/woow-paas-repo/paas-odoo-ci/addons"
        break
    fi
    _probe="$(dirname "$_probe")"
done
if [ -n "$CI_ADDONS_PATH" ]; then
    echo -e "${BLUE}ℹ️  PAAS_CI_ADDONS_PATH（自動探索）：$CI_ADDONS_PATH${NC}"
else
    CI_ADDONS_PATH="$_DEFAULT_CI_ADDONS"
    echo -e "${YELLOW}⚠️  警告：向上找不到含第三方依賴（ai_base_gt）的 woow-paas-repo/paas-odoo-ci/addons；PAAS_CI_ADDONS_PATH 退回相對預設 '$_DEFAULT_CI_ADDONS'。若建 sandbox 時模組裝不起來（缺 ai_base_gt 等依賴），請手動把 .env 的 PAAS_CI_ADDONS_PATH 改成絕對路徑。${NC}"
fi

# 6. 寫入 .env 檔案
cat > .env <<EOF
# Worktree 配置（自動生成於 $(date -u +"%Y-%m-%dT%H:%M:%SZ")）
# 請勿手動修改此檔案，執行 scripts/setup-worktree-env.sh 重新生成

# Docker Compose 專案名稱（基於目錄名稱）
COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME

# Git 分支名稱
BRANCH_NAME=$BRANCH_NAME

# Odoo Web 服務 Port（自動計算以避免衝突）
ODOO_PORT=$ODOO_PORT

# Odoo 資料庫名稱（基於分支名稱）
ODOO_DB_NAME=$DB_NAME

# PostgreSQL 配置
POSTGRES_HOST=$POSTGRES_HOST
POSTGRES_PORT=5432
POSTGRES_USER=odoo
POSTGRES_PASSWORD=odoo

# Odoo 配置
ODOO_ADMIN_PASSWD=admin

# 共享資料庫模式
USE_SHARED_DB=$USE_SHARED_DB
SHARED_DB_NETWORK=odoo_network

# PgAdmin 配置（可選）
PGADMIN_DEFAULT_EMAIL=admin@woow.com
PGADMIN_DEFAULT_PASSWORD=admin

# Debug Port（用於 VS Code 遠端調試，可選）
DEBUG_PORT=5678

# 第三方 addon 路徑（mount 進 /mnt/extra-addons-ext）
# 由上方 5b 自動向上探索 paas-odoo-ci/addons 並寫成絕對路徑（避免相對深度誤判）；
# 探索失敗則為相對預設，屆時請手動改為絕對路徑。
PAAS_CI_ADDONS_PATH=$CI_ADDONS_PATH
EOF

# 7. 顯示配置摘要
echo -e "${GREEN}✅ Worktree 環境配置完成${NC}"
echo ""
echo -e "  ${BLUE}Branch:${NC}       $BRANCH_NAME"
echo -e "  ${BLUE}Project:${NC}      $COMPOSE_PROJECT_NAME"
echo -e "  ${BLUE}Port:${NC}         $ODOO_PORT"
echo -e "  ${BLUE}Database:${NC}     $DB_NAME"
if [ "$USE_SHARED_DB" = "true" ]; then
    echo -e "  ${BLUE}DB Mode:${NC}      ${GREEN}共享模式${NC} ($POSTGRES_HOST)"
else
    echo -e "  ${BLUE}DB Mode:${NC}      ${YELLOW}獨立模式${NC}"
fi
echo ""
echo -e "${GREEN}下一步：${NC}"
echo -e "  執行: ${YELLOW}./scripts/start-dev.sh${NC}"
echo ""
