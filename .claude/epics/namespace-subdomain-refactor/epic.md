---
name: namespace-subdomain-refactor
status: backlog
created: 2026-03-01T14:55:36Z
progress: 0%
prd: .claude/prds/namespace-subdomain-refactor.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/131
---

# Epic: Namespace & Subdomain Naming Refactor

## Overview

將所有 PaaS 資源命名從流水號 (`workspace.id`) 改為 hash-based 命名，統一使用 `paas-{type}-{ws_hash}-{resource_hash}` 格式。這是一個純後端重構，不涉及前端變更，影響範圍集中在 2 個 controller/model 檔案 + 7 個測試檔案。

## Architecture Decisions

1. **Hash 來源**：使用 `workspace.slug`（已有 UNIQUE constraint）的 MD5 hash 前 8 字元作為 workspace 識別碼，避免跨 DB 的 ID 碰撞
2. **Salted hash for resources**：沿用現有 Cloud Service 的 `md5(reference_id + name)[:8]` 模式，Smart Home 新增 `reference_id` 欄位以對齊
3. **共用工具函式**：在 `src/services/naming.py` 新增工具模組，避免重複邏輯散佈在 controller 和 model 中
4. **不遷移現有資料**：已部署的 namespace/subdomain 保持不變，僅新建資源使用新命名

## Technical Approach

### 新增共用模組 `src/services/naming.py`

```python
import hashlib

def generate_ws_hash(slug: str) -> str:
    return hashlib.md5(slug.encode()).hexdigest()[:8]

def generate_resource_hash(reference_id: str, name: str) -> str:
    return hashlib.md5((reference_id + name).encode()).hexdigest()[:8]

def make_namespace(slug: str) -> str:
    return f"paas-ws-{generate_ws_hash(slug)}"

def make_service_subdomain(slug: str, reference_id: str, name: str) -> str:
    return f"paas-cs-{generate_ws_hash(slug)}-{generate_resource_hash(reference_id, name)}"

def make_smarthome_subdomain(slug: str, reference_id: str, name: str) -> str:
    return f"paas-sm-{generate_ws_hash(slug)}-{generate_resource_hash(reference_id, name)}"
```

### 受影響的生產程式碼

| 檔案 | 變更 |
|------|------|
| `src/services/naming.py` | 新增：命名工具函式 |
| `src/services/__init__.py` | import naming |
| `src/controllers/paas.py:1093-1099` | 改用 `make_namespace()` + `make_service_subdomain()` |
| `src/models/smart_home.py` | 新增 `reference_id`，改用 `make_smarthome_subdomain()` + tunnel name 對齊 |
| `src/migrations/18.0.1.0.3/post-migrate.py` | 為既有 smart_home 記錄產生 reference_id |
| `src/__manifest__.py` | version bump to 18.0.1.0.3 |

### 受影響的測試程式碼

7 個測試檔案中的硬編碼 `paas-ws-{id}` / `paas-{id}-` / `sh-` 格式需更新。

## Implementation Strategy

4 個任務，按順序執行（後 3 個可平行但為降低衝突建議序列化）：

1. **Foundation** - 建立 naming 工具模組 + 單元測試
2. **Cloud Service** - 重構 namespace + service subdomain 命名
3. **Smart Home** - 新增 reference_id + 重構 subdomain/tunnel 命名 + migration
4. **Test Suite** - 更新所有測試檔案中的硬編碼命名

## Task Breakdown Preview

- [ ] Task 1: Create `naming.py` utility module with hash functions and unit tests
- [ ] Task 2: Refactor cloud service namespace + subdomain in `paas.py` controller
- [ ] Task 3: Add `reference_id` to Smart Home, refactor subdomain/tunnel naming, add migration
- [ ] Task 4: Update all test suites with new naming format

## Dependencies

- **Task 1** blocks Tasks 2, 3, 4
- **Task 3** requires migration file（`18.0.1.0.3`）+ manifest version bump
- 無外部相依（PaaS Operator 不需要修改）

## Success Criteria (Technical)

| 指標 | 目標 |
|------|------|
| 新建 namespace 格式 | `paas-ws-{8hex}` |
| 新建 service subdomain 格式 | `paas-cs-{8hex}-{8hex}` |
| 新建 smart home subdomain 格式 | `paas-sm-{8hex}-{8hex}` |
| Tunnel name = subdomain | 一致 |
| 測試通過 | 所有測試（除已知 4 個 pre-existing failures） |
| 現有資料不受影響 | 已部署的 namespace/subdomain 不變 |

## Estimated Effort

- **Overall**: 4 tasks, ~2-3 小時實作
- **Critical path**: Task 1 → Task 2/3（可平行）→ Task 4
- **Risk**: 低（純重構，影響範圍明確，有完整測試覆蓋）

## Tasks Created

- [ ] #132 - Create naming utility module (parallel: false, blocks all)
- [ ] #133 - Refactor cloud service namespace and subdomain naming (parallel: true)
- [ ] #134 - Refactor smart home naming and add reference_id (parallel: true)
- [ ] #135 - Update all test suites for new naming format (parallel: false)

Total tasks: 4
Parallel tasks: 2 (#133, #134)
Sequential tasks: 2 (#132, #135)
Estimated total effort: 2.5 hours
