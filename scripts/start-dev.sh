#!/usr/bin/env bash
# Start Development Environment
# 啟動 Odoo 開發環境
#
# 此腳本會：
# 1. 執行環境設定（如果 .env 不存在）
# 2. 啟動 docker-compose
# 3. 等待 Odoo 服務就緒
# 4. 顯示訪問 URL

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
echo ""

# 4. 啟動 docker-compose
echo -e "${BLUE}📦 啟動 Docker 容器...${NC}"
docker compose up -d

# 5. 等待 Odoo 服務就緒
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

# 6. 顯示訪問資訊
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
echo -e "${BLUE}常用命令：${NC}"
echo -e "  查看日誌：    ${YELLOW}docker compose logs -f web${NC}"
echo -e "  停止服務：    ${YELLOW}docker compose stop${NC}"
echo -e "  重啟服務：    ${YELLOW}docker compose restart web${NC}"
echo -e "  執行測試：    ${YELLOW}./scripts/test-addon.sh${NC}"
echo -e "  更新模組：    ${YELLOW}docker compose exec web odoo -d ${ODOO_DB_NAME:-woow_main} -u woow_paas_platform --dev xml${NC}"
echo ""
