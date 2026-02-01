---
name: cloud-services-mvp
status: backlog
created: 2026-02-01T17:21:51Z
updated: 2026-02-01T17:39:37Z
progress: 0%
prd: .claude/prds/cloud-services-mvp.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/4
---

# Epic: cloud-services-mvp

## Overview

實現 Cloud Services MVP 功能，讓用戶能夠一鍵部署容器化應用程式。採用 **Odoo + PaaS Operator (FastAPI) + Kubernetes + Helm** 架構，Odoo 負責前端 UI 和元數據管理，PaaS Operator 負責執行 Helm 操作。

**核心流程**：Marketplace → Configure → Launch → Manage

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Helm 操作執行 | 獨立 PaaS Operator (FastAPI) | Odoo Pod 無法執行 Helm CLI |
| Operator 通訊 | HTTP REST + API Key | 簡單、安全、可擴展 |
| 前端框架 | 複用現有 OWL 架構 | 已有 router、layout、components |
| Namespace 策略 | `paas-ws-{workspace_id}` | 與 Workspace 1:1 對應 |
| TLS 終止 | Cloudflare Proxy | 免費、自動續期 |

## Technical Approach

### Backend - PaaS Operator (`extra/paas-operator/`)
- FastAPI + Pydantic + subprocess (Helm CLI)
- API Key middleware 認證
- Helm wrapper: install, upgrade, uninstall, rollback, history, status
- K8s manifests: Deployment, Service (ClusterIP), RBAC

### Backend - Odoo (`src/`)
- **Models**: `CloudAppTemplate`, `CloudService`
- **Controller**: 擴展 `paas.py` 加入 Cloud Service API
- **Service**: PaaS Operator HTTP client (類似 `workspace_service.js` 的後端版)

### Frontend - OWL (`src/static/src/paas/`)
- **Pages**: AppMarketplacePage, AppConfigurationPage, ServiceDetailPage
- **Components**: AppCard, HelmValueForm, StatusBadge
- **Services**: cloud_service.js (API client)
- **Router**: 新增 `/workspaces/:id/services/*` 路由

## Implementation Strategy

採用 **垂直切片** 方式，每個 Task 交付完整的端到端功能：

1. **先建立 PaaS Operator** - 獨立服務，可單獨測試
2. **再建立 Odoo 基礎** - Models + API + Operator Client
3. **最後建立 UI** - 複用現有 OWL 架構，快速迭代
4. **E2E 測試** - 確保所有流程正常運作

## Task Breakdown Preview

限制為 **10 個以內** 的高內聚任務：

- [ ] **Task 1: PaaS Operator Service** - FastAPI + Helm wrapper + Dockerfile + K8s manifests
- [ ] **Task 2: Odoo Models & Seed Data** - CloudAppTemplate + CloudService + 3 個初始應用
- [ ] **Task 3: Odoo API & Operator Client** - Cloud Service JSON APIs + HTTP client
- [ ] **Task 4: Marketplace UI** - AppMarketplacePage + AppCard + CategoryFilter + Search
- [ ] **Task 5: Configuration & Launch UI** - AppConfigurationPage + HelmValueForm + Launch flow
- [ ] **Task 6: Service Detail UI** - ServiceDetailPage + Overview/Config tabs + StatusBadge
- [ ] **Task 7: Service Operations** - Rollback + Delete + Edit Domain modals
- [ ] **Task 8: Dashboard Integration** - ServiceCard on WorkspaceDetailPage + 引導畫面
- [ ] **Task 9: E2E Testing** - All user flows + Integration tests + Error cases
- [ ] **Task 10: Documentation & Deployment** - README + K8s deployment guide

## Dependencies

### External (Already Available)
- K3s cluster with Traefik
- Cloudflare DNS (`*.woowtech.com`)
- Public Helm repos (Bitnami, etc.)

### Internal (Already Completed)
- Workspace model (`src/models/workspace.py`)
- WorkspaceAccess model (`src/models/workspace_access.py`)
- OWL App Shell (router, layout, base components)
- SCSS theme system

### To Be Created
- PaaS Operator Service → Created in Task 1
- CloudAppTemplate model → Created in Task 2
- CloudService model → Created in Task 2

## Success Criteria (Technical)

| Criterion | Target |
|-----------|--------|
| PaaS Operator 啟動時間 | < 5s |
| API 回應時間 | < 200ms (excluding Helm ops) |
| Marketplace 載入 | < 500ms |
| Helm install 完成 | < 5 min |
| 測試覆蓋率 | > 80% |
| E2E 測試通過 | 100% |

## Estimated Effort

| Task | Effort |
|------|--------|
| Task 1: PaaS Operator | L |
| Task 2: Odoo Models | M |
| Task 3: Odoo API | M |
| Task 4: Marketplace UI | M |
| Task 5: Config UI | M |
| Task 6: Detail UI | M |
| Task 7: Operations | S |
| Task 8: Dashboard | S |
| Task 9: E2E Testing | L |
| Task 10: Docs | S |

**Critical Path**: Task 1 → Task 2 → Task 3 → Task 4-8 (parallel) → Task 9 → Task 10

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Helm CLI 執行失敗 | 完整的錯誤處理 + 日誌記錄 |
| K8s 權限不足 | RBAC 配置驗證 + 文檔說明 |
| 前端狀態不同步 | Polling + 明確的狀態機 |
| Subdomain 衝突 | 唯一性驗證 + 錯誤提示 |

## Tasks Created

| # | Task | Parallel | Depends On | Effort |
|---|------|----------|------------|--------|
| #5 | PaaS Operator Service | false | - | L |
| #6 | Odoo Models & Seed Data | true | - | M |
| #7 | Odoo API & Operator Client | false | #5, #6 | M |
| #8 | Marketplace UI | true | #7 | M |
| #9 | Configuration & Launch UI | true | #7 | M |
| #10 | Service Detail UI | true | #7 | M |
| #11 | Service Operations | true | #10 | S |
| #12 | Dashboard Integration | true | #7 | S |
| #13 | E2E Testing | false | #8-#12 | L |
| #14 | Documentation & Deployment | false | #13 | S |

**Summary:**
- Total tasks: 10
- Parallel tasks: 6 (#6, #8, #9, #10, #11, #12)
- Sequential tasks: 4 (#5, #7, #13, #14)
- Estimated total effort: ~80-120 hours

**Dependency Graph:**
```
#5 ───┬──> #7 ───┬──> #8 ──┐
      │          │         │
#6 ───┘          ├──> #9 ──┤
                 │         │
                 ├──> #10 ─┼──> #11
                │         │
                └──> #12 ─┴──> #13 ──> #14
```

## Reference Documents

- Technical Spec: `docs/spec/cloud-services.md`
- Design Mockups: `resource/stitch_paas_web_app_shell_global_navigation_2026-01-16/`
- Existing Workspace: `src/models/workspace.py`
- OWL Router: `src/static/src/paas/core/router.js`
