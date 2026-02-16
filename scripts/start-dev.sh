#!/usr/bin/env bash
# Start Development Environment
# 啟動 Odoo 開發環境
#
# 此腳本會：
# 1. 執行環境設定（如果 .env 不存在）
# 2. 根據 USE_SHARED_DB 決定啟動模式
# 3. 啟動 docker-compose
# 4. 等待 Odoo 服務就緒
# 5. 顯示訪問 URL

set -euo pipefail

# 顏色輸出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 取得專案根目錄
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo -e "${BLUE}🚀 啟動 Odoo 開發環境...${NC}"
echo ""

# 1. 檢查並執行環境設定
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠️  .env 檔案不存在，執行環境設定...${NC}"
    ./scripts/setup-worktree-env.sh
else
    echo -e "${GREEN}✓${NC} 使用現有的 .env 配置"
fi

# 2. 載入環境變數
set -a
source .env
set +a

# 3. 顯示配置資訊
echo ""
echo -e "${BLUE}配置資訊：${NC}"
echo -e "  ${BLUE}Branch:${NC}       ${BRANCH_NAME:-unknown}"
echo -e "  ${BLUE}Project:${NC}      ${COMPOSE_PROJECT_NAME:-unknown}"
echo -e "  ${BLUE}Port:${NC}         ${ODOO_PORT:-8069}"
echo -e "  ${BLUE}Database:${NC}     ${ODOO_DB_NAME:-woow_main}"

# 4. 判斷資料庫模式
USE_SHARED_DB="${USE_SHARED_DB:-false}"
if [ "$USE_SHARED_DB" = "true" ]; then
    echo -e "  ${BLUE}DB Mode:${NC}      ${GREEN}共享模式${NC} (${POSTGRES_HOST:-db})"
    COMPOSE_PROFILES=""

    # 確保共享網路存在
    SHARED_NETWORK="${SHARED_DB_NETWORK:-odoo_network}"
    if ! docker network inspect "$SHARED_NETWORK" > /dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  共享網路 $SHARED_NETWORK 不存在，正在建立...${NC}"
        docker network create "$SHARED_NETWORK" > /dev/null 2>&1 || true
    fi
else
    echo -e "  ${BLUE}DB Mode:${NC}      ${YELLOW}獨立模式${NC}"
    COMPOSE_PROFILES="standalone"

    # 確保共享網路存在（即使是獨立模式也需要，因為 docker-compose.yml 定義了它）
    SHARED_NETWORK="${SHARED_DB_NETWORK:-odoo_network}"
    if ! docker network inspect "$SHARED_NETWORK" > /dev/null 2>&1; then
        docker network create "$SHARED_NETWORK" > /dev/null 2>&1 || true
    fi
fi
echo ""

# 5. 從模板生成 Odoo 配置（使用 envsubst 替換環境變數）
TEMPLATE_FILE="$PROJECT_ROOT/config/odoo/odoo.conf.template"
CONFIG_FILE="$PROJECT_ROOT/config/odoo/odoo.conf"
if [ -f "$TEMPLATE_FILE" ]; then
    envsubst < "$TEMPLATE_FILE" > "$CONFIG_FILE"
    echo -e "${GREEN}✓${NC} 已從模板生成 odoo.conf"
else
    echo -e "${YELLOW}⚠️  找不到 odoo.conf.template，跳過配置生成${NC}"
fi

# 6. 啟動 docker-compose
echo -e "${BLUE}📦 啟動 Docker 容器...${NC}"
if [ -n "$COMPOSE_PROFILES" ]; then
    COMPOSE_PROFILES="$COMPOSE_PROFILES" docker compose up -d
else
    docker compose up -d web
fi

# 7. 等待 Odoo 服務就緒
echo ""
echo -e "${BLUE}⏳ 等待 Odoo 服務就緒...${NC}"

MAX_WAIT=120
WAIT_COUNT=0
ODOO_URL="http://localhost:${ODOO_PORT:-8069}"

while [ $WAIT_COUNT -lt $MAX_WAIT ]; do
    if curl -s -f "$ODOO_URL/web/database/selector" > /dev/null 2>&1 || \
       curl -s -f "$ODOO_URL" > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Odoo 服務已就緒！${NC}"
        break
    fi

    # 顯示進度點
    echo -n "."
    sleep 2
    WAIT_COUNT=$((WAIT_COUNT + 2))
done

echo ""

if [ $WAIT_COUNT -ge $MAX_WAIT ]; then
    echo -e "${YELLOW}⚠️  警告：Odoo 服務可能尚未完全啟動${NC}"
    echo -e "${YELLOW}   請等待幾分鐘後再訪問，或執行以下命令查看日誌：${NC}"
    echo -e "${YELLOW}   docker compose logs -f web${NC}"
fi

# 8. 檢查並自動建立資料庫
DB_NAME="${ODOO_DB_NAME:-woow_main}"
echo ""
echo -e "${BLUE}🔍 檢查資料庫 ${DB_NAME}...${NC}"

# 根據模式選擇正確的容器來檢查 PostgreSQL
if [ "$USE_SHARED_DB" = "true" ]; then
    PG_CONTAINER="${POSTGRES_HOST:-db}"
    # 嘗試從 web 容器內部檢查 PostgreSQL
    echo -n "  等待 PostgreSQL ($PG_CONTAINER)..."
    PG_READY=0
    for i in {1..30}; do
        if docker compose exec -T web bash -c "pg_isready -h $PG_CONTAINER -U ${POSTGRES_USER:-odoo}" > /dev/null 2>&1; then
            PG_READY=1
            echo -e " ${GREEN}就緒${NC}"
            break
        fi
        echo -n "."
        sleep 1
    done
else
    # 獨立模式：使用本地 db 容器
    echo -n "  等待 PostgreSQL..."
    PG_READY=0
    for i in {1..30}; do
        if docker compose exec -T db pg_isready -U "${POSTGRES_USER:-odoo}" > /dev/null 2>&1; then
            PG_READY=1
            echo -e " ${GREEN}就緒${NC}"
            break
        fi
        echo -n "."
        sleep 1
    done
fi

if [ "$PG_READY" = "0" ]; then
    echo -e " ${YELLOW}超時${NC}"
    echo -e "${YELLOW}⚠️  PostgreSQL 可能尚未完全啟動，跳過自動建立資料庫${NC}"
else
    # 檢查資料庫是否已存在
    if [ "$USE_SHARED_DB" = "true" ]; then
        DB_LIST=$(docker compose exec -T web bash -c "psql -h ${POSTGRES_HOST:-db} -U ${POSTGRES_USER:-odoo} -lqt 2>/dev/null | cut -d \| -f 1 | tr -d ' '" 2>/dev/null || echo "")
    else
        DB_LIST=$(docker compose exec -T db psql -U "${POSTGRES_USER:-odoo}" -lqt 2>/dev/null | cut -d \| -f 1 | tr -d ' ')
    fi

    if echo "$DB_LIST" | grep -qx "$DB_NAME"; then
        DB_EXISTS="1"
    else
        DB_EXISTS="0"
    fi

    if [ "$DB_EXISTS" = "0" ]; then
        echo -e "${YELLOW}📦 資料庫不存在，正在建立並安裝模組...${NC}"
        echo -e "${YELLOW}   （首次啟動需要 1-3 分鐘，請耐心等候）${NC}"

        # 自動偵測 extra/extra-addons 中的模組
        INSTALL_MODULES="base,woow_paas_platform"
        EXTRA_ADDONS_DIR="$PROJECT_ROOT/extra/extra-addons"
        if [ -d "$EXTRA_ADDONS_DIR" ]; then
            for dir in "$EXTRA_ADDONS_DIR"/*/; do
                module_name=$(basename "$dir")
                if [[ "$module_name" != .* ]] && [ -f "$dir/__manifest__.py" ]; then
                    INSTALL_MODULES="${INSTALL_MODULES},${module_name}"
                fi
            done
        fi
        echo -e "  ${BLUE}安裝模組：${NC} ${INSTALL_MODULES}"

        # 使用 Odoo CLI 初始化資料庫並安裝所有偵測到的模組
        if docker compose exec -T web odoo \
            -d "$DB_NAME" \
            -i "$INSTALL_MODULES" \
            --stop-after-init \
            --without-demo=all \
            --load-language=zh_TW 2>&1 | tee /tmp/odoo_init.log | grep -E "(Loading|Installing|init db|error|Error)" | head -20; then
            echo -e "${GREEN}✅ 資料庫建立完成！${NC}"
            # 重啟 web 容器（因為 --stop-after-init 會停止 Odoo 進程）
            echo -e "${BLUE}🔄 重啟 Odoo 服務...${NC}"
            docker compose restart web > /dev/null 2>&1
            sleep 3
        else
            echo -e "${YELLOW}⚠️  資料庫初始化可能有問題，查看完整日誌：cat /tmp/odoo_init.log${NC}"
        fi
    else
        echo -e "${GREEN}✓${NC} 資料庫 ${DB_NAME} 已存在"
    fi
fi

# 9. 顯示訪問資訊
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}🎉 開發環境已啟動！${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${BLUE}訪問 Odoo：${NC}"
echo -e "  ${YELLOW}$ODOO_URL${NC}"
echo ""
echo -e "${BLUE}資料庫名稱：${NC}"
echo -e "  ${YELLOW}${ODOO_DB_NAME:-woow_main}${NC}"
echo ""
echo -e "${BLUE}資料庫模式：${NC}"
if [ "$USE_SHARED_DB" = "true" ]; then
    echo -e "  ${GREEN}共享模式${NC} - 連接到 ${POSTGRES_HOST:-db}"
else
    echo -e "  ${YELLOW}獨立模式${NC} - 使用獨立的 PostgreSQL 容器"
fi
echo ""
echo -e "${BLUE}常用命令：${NC}"
echo -e "  查看日誌：    ${YELLOW}docker compose logs -f web${NC}"
echo -e "  停止服務：    ${YELLOW}docker compose stop${NC}"
echo -e "  重啟服務：    ${YELLOW}docker compose restart web${NC}"
echo -e "  執行測試：    ${YELLOW}./scripts/test-addon.sh${NC}"
echo -e "  更新模組：    ${YELLOW}docker compose exec web odoo -d ${ODOO_DB_NAME:-woow_main} -u woow_paas_platform --dev xml${NC}"
echo ""
