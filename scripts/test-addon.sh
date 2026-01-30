#!/usr/bin/env bash
# Test Addon
# 執行 Odoo addon 測試
#
# 此腳本會：
# 1. 確保環境已啟動
# 2. 執行 woow_paas_platform 模組的測試
# 3. 顯示測試結果

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

echo -e "${BLUE}🧪 執行 Odoo Addon 測試...${NC}"
echo ""

# 1. 檢查 .env 是否存在
if [ ! -f .env ]; then
    echo -e "${RED}❌ 錯誤：.env 檔案不存在${NC}"
    echo -e "${YELLOW}請先執行: ./scripts/setup-worktree-env.sh${NC}"
    exit 1
fi

# 2. 載入環境變數
set -a
source .env
set +a

# 3. 檢查容器是否運行
if ! docker compose ps web | grep -q "Up"; then
    echo -e "${YELLOW}⚠️  Web 容器未運行，正在啟動...${NC}"
    docker compose up -d web
    echo -e "${BLUE}⏳ 等待容器就緒...${NC}"
    sleep 10
fi

# 4. 顯示測試資訊
echo -e "${BLUE}測試配置：${NC}"
echo -e "  ${BLUE}Database:${NC}     ${ODOO_DB_NAME:-woow_main}"
echo -e "  ${BLUE}Module:${NC}       woow_paas_platform"
echo ""

# 5. 執行測試
echo -e "${BLUE}🔍 開始執行測試...${NC}"
echo ""

# 執行測試命令
docker compose exec web odoo \
  --test-enable \
  --test-tags woow_paas_platform \
  --stop-after-init \
  --log-level=test \
  -d "${ODOO_DB_NAME:-woow_main}"

TEST_EXIT_CODE=$?

echo ""

# 6. 顯示結果
if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}✅ 所有測試通過！${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
else
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}❌ 測試失敗！${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "${YELLOW}查看詳細日誌：${NC}"
    echo -e "  ${YELLOW}docker compose logs web${NC}"
    exit 1
fi

echo ""
