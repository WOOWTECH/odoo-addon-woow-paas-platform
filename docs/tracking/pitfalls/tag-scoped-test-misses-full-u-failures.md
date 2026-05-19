# Pitfall: `--test-tags` scope-limited 驗綠 → cross-module stale schema reference 漏抓

> CAQA / impl 用 `docker compose ... odoo --test-tags "/<module>:TestA,TestB,..."` 跑 scope-limited test 驗綠，結果**沒抓到既有 test 模組內的 cross-class stale schema reference**。Full `-u <module> --test-enable` 跑全 suite 才會 red。Round 1 BLOCKER 1 reviewer 補抓。

## Symptom

- TDD red phase 26 cases 全 red（預期），green phase 後 26 cases 全 green（預期）
- `docker compose run --rm web odoo -d ... --test-tags "/woow_paas_platform:Test{MigrationSchema,MigrationBehavior,Lifecycle,AclCrossWorkspaceIsolation,ChatConsumption,...,ProjectScopeConstraints}" --stop-after-init` → `0 failed, 0 error(s) of 26 tests`
- CAQA report APPROVED WITH CAVEATS（14 invariants 全 ✅）
- **但若跑** `docker compose run --rm web odoo -d ... -u woow_paas_platform --test-enable --stop-after-init`（**不帶 `--test-tags`，整 suite 跑**） → `ValueError: Invalid field 'cloud_service_id' on model 'woow_paas_platform.mcp_server'` 在 setUp 階段 raise，10 處 stale field reference
- 影響：full module 升級 (`-u`) 在 production / stg 跑時，所有 test 都會 import-time fail（即使這次 refactor「沒動」的 test class），整個 module update 卡住

**本 case 的 stale reference 位置**（commit `97b5313` 修復前 — `src/tests/test_mcp_integration.py`）：

```
line 211  TestAutoCreateMCPServer 整 class（6 tests）—— 用舊 schema cloud_service.user_mcp_server_ids
line 280  cs1.user_mcp_server_ids = [...]
line 303  cs.user_mcp_server_ids[0].cloud_service_id == cs
line 318  cs.user_mcp_server_ids
line 345  ...
line 358  ...
line 380  ...
line 384  cs1.user_mcp_server_ids[0].cloud_service_id ...
line 387  TestMCPServerCronRetry 整 class（6 tests）
line 429  mcp_server.cloud_service_id == service.id
line 597  TestMCPServerCascadeDelete 整 class（3 tests）
line 636  cloud_service_id 引用
line 660  cloud_service_id 引用
line 693  cloud_service_id 引用
```

3 個 class（15 個 test method）共 10 處 stale schema reference。CAQA 用 `--test-tags` scope 只跑 7 個新 class（含 `TestMcpProjectScopeConstraints`），這 3 個既有 class 從未進入測試。

## Why it happens

### CAQA / impl 用 `--test-tags` 的合理性 + 盲區

```bash
docker compose run --rm web odoo -d woow_feature_project_mcp_skill \
  -u woow_paas_platform --test-enable \
  --test-tags "/woow_paas_platform:TestMcpProjectDecoupleMigrationSchema,\
/woow_paas_platform:TestMcpProjectDecoupleMigrationBehavior,\
/woow_paas_platform:TestMcpProjectLifecycle,\
/woow_paas_platform:TestMcpAclCrossWorkspaceIsolation,\
/woow_paas_platform:TestMcpChatConsumption,\
/woow_paas_platform:TestAiAssistantControllerMcpToolsForChannel,\
/woow_paas_platform:TestMcpProjectScopeConstraints" \
  --stop-after-init --log-level=test
```

合理性：

- 只跑「跟本 refactor 相關」的 test class — 不浪費時間跑 subscription / OAuth / smart_home 等 unrelated suite
- Red phase 用 `--test-tags` 證 26 cases 全 red；green phase 用同一套 tags 證 26 cases 全 green — apple-to-apple

盲區：

- **`--test-tags` 只 filter `_test_setup` 階段註冊的 test methods**，**不 filter test 模組的 import**
- 但 Odoo `--test-enable` 在 setUp 階段才會具體 load test class（透過 `TransactionCase.setUpClass`）
- 如果該 class 沒被 tag selector 命中 → setUp 不 run → stale field reference 不 raise
- → CAQA 看 `0 failed` 以為全綠，實際上有 3 個 class **根本沒跑過**

### 為什麼 CAQA `§同源 code path 檢查` 漏抓

CAQA grep verification:

```bash
grep -rn "user_mcp_server_ids" src/ --include="*.py" --include="*.xml"  # production code 0 hits ✅
```

CAQA 明文**排除 tests/**（line 219-234），理由：

> Test code 仍保留舊鏈引用（在 fixture 設計與 docstring 內）— 這是 tester 故意設計用來測「若 implementer 走舊鏈會被抓」的 regression lock，不算殘留。

這個 exclude 太寬：

- 「tester 故意保留舊鏈」只適用於**本次 refactor 新建的 5 個 test files**（test_mcp_acl_isolation.py / test_mcp_project_lifecycle.py 等）內的 fixture
- **既有 test_mcp_integration.py 內的 3 個 class** 是「本應一併更新但漏改」— 不是 tester 故意保留

CAQA 沒有 mechanism 區分這兩種 tests/ 內容 — 一律排除。

### 為什麼 tester 自己 commit 也漏抓

Tester commit `5c5d980` (red suite + 24 cases) + `3d35e79` (Gap fill +2) 只追加新 class 到 test_mcp_integration.py 尾巴，**沒清舊**：

- Plan §Test scope 建議「B. Constraint（補入既有 test_mcp_integration.py）」— plan 明文指 tester 動這個檔案
- 但 plan **沒明文要求 tester 同時清舊 cascade/auto-create test**
- Tester follow plan、不擴 scope，新增 `TestMcpProjectScopeConstraints` 在尾巴

→ Tester 漏抓的次因，主因是 plan blind spot：plan 沒明文「動 test_mcp_integration.py 時同時清/port 既有 class」。

### 為什麼 Implementation phase 也漏抓

Implementation commit `12cf255` (refactor) 完全沒動 `src/tests/`：

```bash
$ git show 12cf255 --stat -- src/tests/
# 0 changes
```

Implementation 視角：
- Production code 改完，跑 `--test-tags` 命中的 26 cases 全綠 ✅
- 既有 test 沒進 tag selector → 不會 see import-time fail → 以為 OK
- 不會 proactively grep `cloud_service_id` 在 tests/

### 為什麼 reviewer round 1 grep 才抓到

Reviewer 透過 `/pr-review-toolkit:review-pr` 跑 5 個 sub-agent 平行 review。code-reviewer agent **主動補 CAQA 排除的範圍**：

```bash
grep "user_mcp_server_ids" in src/                               → 2 hits ❌
   test_mcp_integration.py:380, 384
grep "cloud_service_id" in src/tests/ (mcp_server context)       → 9 hits ❌
   test_mcp_integration.py:280, 303, 318, 345, 358, 429, 636, 660, 693
```

Reviewer 同時跑 pr-test-analyzer agent，雙簽 confirm — 兩個 sub-agent 對同一 finding 獨立 raise。

## How to avoid

### 1. Green phase 必須跑 **full `-u <module> --test-enable`**（不只 tag-scoped）

Tester / impl 在 green phase 收尾必跑：

```bash
# 1. 先跑 tag-scoped 證 26/26 綠（apple-to-apple 對紅相）
docker compose run --rm web odoo -d <db> -u <module> --test-enable \
  --test-tags "/<module>:Test{Class1,Class2,...}" --stop-after-init

# 2. 再跑 full --test-enable 證整 module suite 0 red
docker compose run --rm web odoo -d <db> -u <module> --test-enable \
  --stop-after-init
```

**兩個 invocation 缺一不可**：
- (1) 證本次 refactor 的 regression 鎖死（red→green）
- (2) 證沒順帶炸到 module 內其他 test（cross-module stale reference catch）

不跑 (2) = 不知道是否炸到其他 class。

### 2. Tester / impl 改既有 test 文件時 grep 同檔內 cross-class 對舊 schema 的引用

每次 PR 改 `test_*.py` 時：

```bash
# 找該 test 文件內所有對 refactor 涉及欄位的 reference
grep -n "<old_field>\|<old_relation>" src/tests/<modified_test_file>

# 期望結果：所有 hit 都對應到「故意保留以驗 regression」的 fixture
# 否則 = stale reference，必須 port 或 retire
```

本 case 的具體應用：tester 在動 `test_mcp_integration.py` 時就該注意到 `TestAutoCreateMCPServer` / `TestMCPServerCronRetry` / `TestMCPServerCascadeDelete` 用了舊 schema。

### 3. Plan / decision doc 內明文「動既有 test file 時的 scope 處理」

Plan §Test scope 必須明寫：

> 若 plan 涉及 schema field 改名 / 移除，**所有引用該 field 的既有 test 必須一併 port 或 retire**。tester 在 red phase 動既有 test file 時，必須 grep 整個 file 找到 stale reference 並決定處理（port to new schema / retire as redundant / keep with comment 解釋 fixture rationale）。

本次 plan 沒寫這條 → tester 沒做 → BLOCKER 1。Future plan template 加此條 → 強制覆蓋。

### 4. CAQA 視角 — 區分「故意保留」vs「應改而漏改」

CAQA `§同源 code path 檢查` 應**不要全排除 tests/**，改成：

> Tests/ 內 stale schema reference 視為**疑似 BLOCKER**，除非：
> - 該 reference 在「本次 refactor 新建的 test files」內，且 fixture / docstring 明文標「故意保留以驗 regression」
> - 既有 test files 內的 stale reference 一律視為 BLOCKER（需 tester 補 port 或 retire）

CAQA invariant template 須加這個區分。對應到 R2-2 follow-up 之外的另一條 CAQA template improvement。

### 5. CI / dev sandbox 跑 `-u <module>` 而非 `--test-tags`

Dev sandbox / staging deploy 預設跑 `-u woow_paas_platform`（不帶 `--test-tags`），讓 import-time fail 在 deploy gate 階段就 surface。Production 升 module version 必須跑 full suite green 才放行。

對本 codebase：`scripts/test-addon.sh` 已是跑全 suite — 此 pitfall 在 CI / sandbox 是被 catch 的，**但 CAQA 視角是手動 tag-scoped**，所以 catch 不到。Tester / impl / CAQA 三個 role 自己跑 verify 時都需注意。

## Related

- [[mcp-project-decouple]] §Round 2 改動敘事 §1（CAQA exclude 範圍要 narrowly scope）+ §3（同源 code path 檢查必須 walk 全 unlink/delete path）
- [[cross-workspace-task-data-leak]] — 同質的「multi-agent / multi-view 補 single-reviewer 視角」教訓
- Code 引用：
  - `src/tests/test_mcp_integration.py:208-700` — 3 個 class port 落地（commit `97b5313`）
  - `src/tests/test_mcp_integration.py::TestAutoCreateMCPServer` / `TestMCPServerCronRetry` / `TestMCPServerCascadeDelete` — port 後對新 schema
- Commit / report：
  - commit `97b5313` — `test(refactor): re-loop tester fix for reviewer round-2 NEEDS REVISION`
  - `docs/decisions/reviews/pr-review-verdict-mcp-2026-05-18.md §BLOCKER 1` — round 1 完整證據鏈
  - `docs/decisions/reviews/caqa-2026-05-18.md §同源 code path 檢查` — CAQA 排除範圍（line 219-234）
- Odoo 上游證據：
  - Odoo `--test-tags` filter 機制（filter test methods，但 import-time `from ... import` 仍會跑 — stale schema 在 setUp 才 raise）
  - `TransactionCase.setUpClass` — class-level setup 觸發點
