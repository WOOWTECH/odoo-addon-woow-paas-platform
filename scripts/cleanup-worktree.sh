#!/usr/bin/env bash
# Cleanup Worktree Environment
# 清理 Odoo 開發環境
#
# 此腳本會：
# 1. 停止 docker-compose 服務
# 2. 移除容器和 volumes
# 3. 清理 .env 檔案（可選）

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

echo -e "${BLUE}🧹 清理 Odoo 開發環境...${NC}"
echo ""

# 檢查是否有 .env 檔案
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠️  .env 檔案不存在，跳過環境變數載入${NC}"
else
    # 載入環境變數以取得專案名稱
    set -a
    source .env
    set +a

    echo -e "${BLUE}環境資訊：${NC}"
    echo -e "  ${BLUE}Project:${NC}      ${COMPOSE_PROJECT_NAME:-unknown}"
    echo -e "  ${BLUE}Branch:${NC}       ${BRANCH_NAME:-unknown}"
    echo ""
fi

# 詢問是否要移除 volumes
echo -e "${YELLOW}❓ 是否要移除 Docker volumes（會刪除資料庫資料）？ (y/N)${NC}"
read -r REMOVE_VOLUMES

# 1. 停止並移除容器
echo -e "${BLUE}🛑 停止容器...${NC}"
docker compose down

# 2. 移除 volumes（如果使用者確認）
if [[ "$REMOVE_VOLUMES" =~ ^[Yy]$ ]]; then
    echo -e "${BLUE}🗑️  移除 volumes...${NC}"
    docker compose down -v
    echo -e "${GREEN}✓${NC} Volumes 已移除"
else
    echo -e "${GREEN}✓${NC} 保留 volumes（資料庫資料保留）"
fi

# 3. 詢問是否刪除 .env 檔案
if [ -f .env ]; then
    echo ""
    echo -e "${YELLOW}❓ 是否要刪除 .env 檔案？ (y/N)${NC}"
    read -r REMOVE_ENV

    if [[ "$REMOVE_ENV" =~ ^[Yy]$ ]]; then
        rm -f .env
        echo -e "${GREEN}✓${NC} .env 檔案已刪除"
    else
        echo -e "${GREEN}✓${NC} 保留 .env 檔案"
    fi
fi

# 4. 顯示清理結果
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ 環境清理完成！${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${BLUE}重新啟動環境：${NC}"
echo -e "  ${YELLOW}./scripts/start-dev.sh${NC}"
echo ""
