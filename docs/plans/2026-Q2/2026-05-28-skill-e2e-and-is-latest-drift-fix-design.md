---
title: Skill is_latest drift 修正 + E2E 測試補強
category: plans
status: done
created: 2026-05-28
updated: 2026-07-19
source: docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md
promoted-at: 2026-07-19
---

# Skill is_latest drift 修正 + E2E 測試補強

## 背景

PR #81 已合上 skill 後台管理 UI + 三個 transition method（`action_publish` / `action_deprecate` / `action_archive_skill`）+ `is_latest` 不變式 + `_recompute_is_latest` helper。但 PR #81 spec §156-160 預埋了一個 follow-up：

> 任何繞過 transition method、直接 `create/write({'state':'published'})` 的路徑會繞過 `_recompute_is_latest`，使 published skill `is_latest` 恆為 default False。

planner 對照 codebase 後確認：

1. **drift 不在 controller 層，在 model 層**：`Skill.create()`（`src/models/skill.py:103-108`）只呼叫 `_anchor_bundle_attachment()`、`Skill.write()`（:110-115）只在 `bundle_attachment_id` 變動時 anchor — **兩者皆不呼叫** `_recompute_is_latest`。
2. **`agent_runtime_skill.py` 只有 download endpoint**（GET `/api/agent-runtimes/skill-bundle`），**不存在** HTTP 上傳 API。view 的「uploaded via the agent-runtime API」是設計願景，非現況。
3. **目前唯一踩中 drift 的真實路徑是 test fixture**：`src/tests/test_agent_pod_phase_c_skill_bundle.py:261-267` 的 setUp 直接 `create({'state':'published', ...})`，未呼叫 action_publish。
4. **`is_latest` 目前無 production 消費端**（`_build_skill_snapshot` 用 `state == 'published'`，非 is_latest；grep 全 repo 無其他消費）。drift 現為 latent data-correctness bug，非 active 功能故障。
5. Phase D 完成、enable-gate 關閉、skill 尚未對使用者開放。

同時 PR #81 的測試只在 model/view 層（TransactionCase），沒走 HTTP 鏈路、沒有瀏覽器 UI 驗證。本 spec 補上：

1. **Model 層收斂修法**：`create()` / `write()` 偵測 `state == 'published'` 時觸發 `_recompute_is_latest` + 補 `published_at`（單一維護點、根因修復）。
2. **幂等資料修補 migration**：保護 prod 即使有殘留 drift 資料也能自動修正；無資料時 no-op。
3. **含瀏覽器 UI 的 e2e 測試**：後端 HttpCase 走「lifecycle ↔ download endpoint 可見性 ↔ is_latest 串接」+ playwright-cli 操作後台 form 驗證 UI wiring。

## 已核准的關鍵決策（leader 與使用者拍板）

| Gap | 決策 | 理由 |
|-----|------|------|
| **Gap 1 — 修法定位** | Model 層收斂：`create()` / `write()` 偵測 `state == 'published'` 時呼叫 `_recompute_is_latest`、必要時補 `published_at` | 根因修復；單一維護點涵蓋 test fixture / 未來上傳 API / demo data / admin 匯入；不依賴呼叫端紀律 |
| **Gap 2 — 資料修補** | 寫幂等 post-migration `src/migrations/18.0.1.0.28/post-fix-is-latest.py`，掃所有 `(project_id, name)` group 重算 is_latest | 使用者選擇「不查 prod、不管現況都安全」；幂等所以無資料 no-op、有資料自動修補 |
| **Gap 2 連帶 — Manifest bump** | 27 → 28；連帶把 `test_agent_pod_phase_c_skill_bundle.py:201` 的硬編 `'"version": "18.0.1.0.27"'` assertion 改 monotonic `>= 27` | 不 bump migration 不會跑；硬編 assertion 是 `phase-versioned-test-assertion` pitfall，每次 bump 都會撞紅 |
| **Gap 3 — E2E HTTP 鏈路** | ORM `create(draft)` → playwright-cli 後台 form button 真實 UI+HTTP → 既有 download endpoint 驗證 published 可下載/非 published 404 → is_latest 以 ORM 斷言 + form/list badge 驗證 | 不擴 scope 補新 endpoint；download endpoint 是 skill feature 唯一既有真實 HTTP 鏈路；form button 走真實 UI+HTTP |

## 設計

### ① Model 層 — `create()` / `write()` 收斂 `_recompute_is_latest`

**`Skill.create()`**（修改 `src/models/skill.py:103-108`）：

```python
@api.model_create_multi
def create(self, vals_list):
    skills = super().create(vals_list)
    for skill in skills:
        skill._anchor_bundle_attachment()
        # Drift 防護：若直接建 state=published（繞過 action_publish），
        # 補 published_at 並維護 is_latest 不變式。
        if skill.state == 'published':
            if not skill.published_at:
                skill.published_at = fields.Datetime.now()
            skill._recompute_is_latest(skill.project_id.id, skill.name)
    return skills
```

**`Skill.write()`**（修改 `src/models/skill.py:110-115`）：

```python
def write(self, vals):
    result = super().write(vals)
    if 'bundle_attachment_id' in vals:
        for skill in self:
            skill._anchor_bundle_attachment()
    # Drift 防護：若 vals 改了 state（含程式化路徑），且結果 state=published，
    # 補 published_at + recompute；其他 state 變動也 recompute 以維持不變式。
    if 'state' in vals:
        for skill in self:
            if skill.state == 'published' and not skill.published_at:
                skill.published_at = fields.Datetime.now()
            skill._recompute_is_latest(skill.project_id.id, skill.name)
    return result
```

**避免遞迴的關鍵**：

- `_recompute_is_latest` 內部 write 的 vals 是 `{'is_latest': ...}`，**不含 `state`** → 不踩 `'state' in vals` 條件，自然不遞迴。
- `published_at` 補蓋的 `skill.published_at = ...` 內部走 `write({'published_at': ...})`，vals 亦不含 `state`，不踩條件。
- 不需要 context flag 等額外保護機制。

**published_at 補蓋時機要在 recompute 之前**：否則 `_recompute_is_latest` 的 `sorted(key=lambda s: (s.published_at or epoch, s.id), reverse=True)` 會把剛建的 published skill 排序錯（被 epoch 當墊底），winner 算錯。

**既有三個 transition method 行為不變**：它們仍會在 write 後呼叫 `_recompute_is_latest`，新 hook 加一次 recompute（同一筆 group），結果一致，無破壞性。

### ② Migration — 幂等 post-migration

**檔案**：`src/migrations/18.0.1.0.28/post-fix-is-latest.py`

**函數**：

```python
def migrate(cr, version):
    """Backfill is_latest invariant for all skills.

    幂等：對每個 (project_id, name) group 重算 is_latest，保證不變式
    （at most one is_latest=True per group, must be most-recently-published
    published skill）。

    無資料 → no-op；有 drift 樣本 → 自動修補。

    跑兩次結果一致（所有 skill 已是 desired，內部 write 條件不寫 → no-op）。
    """
    from odoo import api, SUPERUSER_ID, fields as o_fields
    env = api.Environment(cr, SUPERUSER_ID, {})
    Skill = env['woow_paas_platform.skill'].sudo()
    all_skills = Skill.search([])
    if not all_skills:
        return  # 無資料 no-op
    epoch = o_fields.Datetime.to_datetime('1970-01-01 00:00:00')
    groups = {}
    for s in all_skills:
        groups.setdefault((s.project_id.id, s.name), []).append(s)
    for (project_id, name), members in groups.items():
        published = [s for s in members if s.state == 'published']
        winner = sorted(
            published,
            key=lambda s: (s.published_at or epoch, s.id),
            reverse=True,
        )[:1]
        winner_id = winner[0].id if winner else None
        for s in members:
            desired = s.id == winner_id
            if s.is_latest != desired:
                s.is_latest = desired
```

**幂等性論證**：
- 跑一次：drift 修補（has-drift 樣本變 is_latest=True，其餘 False）
- 跑兩次：所有 skill 已符合 desired，內部 `if s.is_latest != desired` 不成立 → 無 write
- 無資料：`Skill.search([])` 為空 → early return

**Rollback 路徑**：
- `is_latest` 是 derived 欄位，重算即修正、無破壞性
- 不需 down migration
- 若需要 rollback 整個 fix：還原 model code（移除 create/write recompute）後 drift 樣本會再出現於新建路徑，但已修補的舊資料仍正確直到下次 transition 自動重算
- migration 本身為純資料層、不依賴 K8s env secret 或外部服務

### ③ Manifest bump 27 → 28 + 改硬編 assertion

**`src/__manifest__.py:3`**：`"version": "18.0.1.0.27"` → `"version": "18.0.1.0.28"`

**`src/tests/test_agent_pod_phase_c_skill_bundle.py:201`** 從硬編改為 monotonic（`phase-versioned-test-assertion` pattern）：

```python
# 從：
self.assertIn(
    '"version": "18.0.1.0.27"', content,
    "MIG.1: __manifest__.py must bump version to '18.0.1.0.27' ..."
)

# 改為：
import re
m = re.search(r'"version":\s*"18\.0\.1\.0\.(\d+)"', content)
self.assertIsNotNone(
    m, "MIG.1: manifest must declare an 18.0.1.0.N version",
)
self.assertGreaterEqual(
    int(m.group(1)), 27,
    "MIG.1: manifest version must be >= 18.0.1.0.27 (Phase C anchor; "
    "monotonic — future phases may bump higher, see "
    "phase-versioned-test-assertion pattern).",
)
```

理由：Phase C test 鎖的是「Phase C 上線之後」的 schema，用 monotonic `>= 27` 鎖 intent，不硬編當時 phase 的快照值；本次 bump 與未來 phase bump 皆不撞紅。

### ④ E2E 測試策略

#### ④-a 後端 HttpCase — lifecycle ↔ download endpoint 可見性 ↔ is_latest 串接

新增 `src/tests/test_skill_e2e_lifecycle_download.py`（HttpCase）。

**借用既有工具**（避免重造）：
- Phase C 的 `_skill_bundle_token` / `_sign_hs256` HS256 signer（從 `test_agent_pod_phase_c_skill_bundle.py` import 或 copy 一份）
- Phase C 的 `_set_env` env-secret setup pattern

**測什麼**：

| Test | 動作 | 斷言 |
|------|------|------|
| `test_draft_create_then_download_404` | ORM `create(draft)` + attach bundle | download 回 404（state 非 published gate） |
| `test_publish_via_action_then_download_200_and_is_latest_true` | `action_publish` | download 回 200 + raw bytes == 預期；`is_latest=True`；`published_at != NULL` |
| `test_deprecate_then_download_404_and_latest_reassigned` | v1+v2 publish（v2 latest） → deprecate v2 | v2 download 404、v2 `is_latest=False`、v1 `is_latest=True` |
| `test_archive_then_download_404` | publish → archive | download 404、`is_latest=False` |
| `test_create_published_directly_drift_regression_via_http` | 直接 `create({'state':'published'})` | download 200（state gate 過）+ **`is_latest=True`（修法後）** |
| `test_write_state_to_published_drift_regression_via_http` | `create(draft)` → `write({'state':'published'})` | download 200 + **`is_latest=True`（修法後）** |
| `test_migration_fixes_existing_drift_sample` | 純 SQL update `is_latest=False` 於 published skill（模擬 drift） → 呼叫 migration `migrate(cr, version)` → 重新讀 | `is_latest=True`（migration 修補成功） |

**不測什麼**（信任既有覆蓋）：

- `TestPhaseCSkillBundleEndpoint`（SB.1-6 + C-1）已涵蓋 token auth、token_use scope、跨 project 防護、過期 token、wrong-secret、token 權威性 → **本 e2e 不重測**，假設它們已綠
- HTTP 上傳 API（不存在，scope 不擴）
- Model lifecycle 純單元邏輯（PR #81 `TestSkillAdminLifecycle` 10 個 test 覆蓋多版本 recompute、illegal transition guards、re-publish） → **本 e2e 不重測**，只測「lifecycle ↔ HTTP 可見性」串接

#### ④-b 瀏覽器 e2e — playwright-cli 後台 form UI wiring

新增 `tests/e2e/skill/skill-admin-form.spec.{js|ts}`（playwright spec）。

> 路徑放在 `tests/e2e/skill/`（新目錄）— 與 Odoo 的 `src/tests/`（Python TransactionCase / HttpCase）分離，因為 odoo test runner 不跑 playwright。CI/手動執行兩種模式皆支援。具體選 JS 或 TS、用 spec 還是 .sh 互動腳本，由 tester 在 plan Task 4 落地時決定（playwright-cli 兩種都支援）。

**前置**：
- ORM 預先建一筆 draft skill（透過 odoo shell / REST / fixture file），供瀏覽器操作對象
- 任何 model `.py` 改動後 → `kubectl --context docker-desktop rollout restart deploy/feat-skill-list-odoo-dev-sandbox-odoo -n dev-sandbox-feat-skill-list` 並等 rollout 完成（leader 強調，否則 serve 進程用舊 code 會誤判 button 行為）

**測什麼（UI wiring，非 model 邏輯）**：

| 場景 | 操作 | 視覺斷言 |
|------|------|---------|
| draft form 初態 | admin 登入 → Skills menu → list → 開 draft skill | state badge=Draft (info)、statusbar 高亮 Draft；Publish + Archive button 可見、Deprecate 隱藏；`is_latest` checkbox=False |
| Publish 點擊 | 點 Publish | 等待 form save → state badge=Published (success)、statusbar 高亮 Published；Deprecate + Archive 可見、Publish 隱藏；`is_latest`=True |
| Deprecate 點擊 | 點 Deprecate | state badge=Deprecated (warning)、statusbar 高亮 Deprecated；Publish + Archive 可見、Deprecate 隱藏；`is_latest`=False |
| Archive 點擊 | 點 Archive | state badge=Archived (muted)、statusbar 高亮 Archived；所有 transition button 隱藏 |
| 截圖 | 每個關鍵狀態切換 | screenshot 落地 `/tmp/skill-e2e-screenshots/` 供視覺驗證 |

**不測什麼**（信任既有覆蓋 / 或 model test 覆蓋）：

- Lifecycle 規則正確性（from archived 不能 publish 等）→ model `TestSkillAdminLifecycle` 覆蓋；瀏覽器不重測，只測 button 點下去 wire 到正確 method
- is_latest 跨多版本 recompute → model test 覆蓋；瀏覽器只 smoke 單一版本確認 wiring
- Menu admin-only → PR #81 `TestSkillAdminViews` 覆蓋；瀏覽器只實際登入 admin 驗 menu 可見作為前置
- bundle 下載 / token auth / ACL / ir.rule → 已被既有 Phase C + ir.rule test 覆蓋
- Non-admin 視角（list 看得到 vs 看不到）→ Phase A ACL test 覆蓋，本次不重測

### ⑤ 既有 fixture 評估（`test_agent_pod_phase_c_skill_bundle.py:261-267`）

修法後 fixture 行為改變：

| 修法前 | 修法後 |
|--------|--------|
| `setUp` 直接 `create({'state':'published'})` → `is_latest=False`（drift） | 同樣 create → `is_latest=True`（create override 觸發 recompute） |

**評估**：

- 該 fixture 目的是測 download endpoint 規則，**不關心** `is_latest`
- Download endpoint 用 `state == 'published'`（非 is_latest），fixture 行為改變**不影響** SB.1-6 / C-1 任一斷言
- 但語意上 fixture 仍走「程式化捷徑」而非 idiomatic `action_publish` 路徑

**選擇**：**(a) 不動 fixture，加註解說明 fixture 現在依賴新的 create-time recompute**

- 風險最低（不動既有 504 行 test 檔）
- 加註解讓未來讀 code 的人知道 setUp `is_latest=True` 是來自 create override 而非顯式 publish
- 選項 (b)「改走 action_publish」列為**可選 follow-up**，不在本 spec 範圍

### ⑥ 測試邊界總覽

| 層級 | 覆蓋 | 工具 / Class | 來源 |
|------|------|--------------|------|
| Model unit（既有） | lifecycle transition 規則 + is_latest 多版本 recompute + illegal guards | `TestSkillAdminLifecycle` (TransactionCase) | PR #81 |
| Model unit（既有） | view/action/menu 註冊 + admin-only menu | `TestSkillAdminViews` (TransactionCase) | PR #81 |
| Bundle endpoint unit（既有） | SB.1-6（token auth、project 權威）+ C-1（token_use scope） | `TestPhaseCSkillBundleEndpoint` (HttpCase) | Phase C |
| Model regression（**NEW**） | drift 修法：create/write 直建 published 後 is_latest 正確、不變式收斂 | `TestSkillIsLatestDriftRegression` (TransactionCase) | Task 1 |
| Backend e2e（**NEW**） | lifecycle ↔ download endpoint 可見性 ↔ is_latest 串接 + migration 修補驗證 | `TestSkillE2ELifecycleDownload` (HttpCase) | Task 3 |
| Browser e2e（**NEW**） | 後台 form transition button UI wiring + statusbar/badge 渲染 | playwright-cli spec（headless + screenshot） | Task 4 |

## Acceptance criteria

1. **使用者透過 admin 後台 form 點 Publish/Deprecate/Archive button** → state、statusbar、badge、`is_latest` 在 UI 即時反映（browser e2e）。
2. **系統會阻止** 任何繞過 `action_publish` 直接 `create/write({'state':'published'})` 的路徑產生 drift：published skill 必有 `is_latest=True`（若為該 group 最新）、`published_at` 必有時戳（model regression test + backend e2e）。
3. **跑 migration** 對既有 drift 樣本 → 不變式恢復；對無 drift 資料 → no-op；跑兩次結果一致（backend e2e）。
4. **lifecycle transition 經 form button → 真實下載可見性切換**：published 可被 pod 下載（200 + raw bytes 正確）、deprecated/archived/draft 不可下載（404）（backend e2e）。
5. **既有 SB.1-6 + C-1 + lifecycle 10 個 test 仍綠**（無迴歸），含 Phase C `MIG.1` 升級為 monotonic 後不撞紅。

## 非目標（Non-goals / YAGNI）

- **新增 HTTP 上傳 API** — 確認 scope 不擴（Gap 3 已拍板 (A) 不採 (B)/(C)）
- **改 ACL / ir.rule / std field 型態** — CLAUDE.md「Known patterns」守線、不動（attachment-row-level-acl、constraint-input-trust-expansion、odoo-std-field-no-override）
- **重測既有覆蓋過的 SB.1-6 / C-1 / lifecycle 純邏輯** — 信任既有 test，e2e 只測串接 + 新缺口
- **改 `test_agent_pod_phase_c_skill_bundle.py:setUp` 走 action_publish** — 列為可選 follow-up
- **盤點 prod 資料** — 使用者拍板「不查 prod、靠幂等 migration 自動修」
- **OWL 前端 skill 管理頁** — 既有 spec 已列 non-goal，本次延續
- **semver 精確排序** — `is_latest` 仍以 `published_at` desc 近似最新（既有 spec 同樣 follow-up）

## 實作注意 / 待驗證

- **避免遞迴**：write override 的觸發條件綁 `'state' in vals`；`_recompute_is_latest` 內部 write `{'is_latest': ...}` 不踩此條件，自然不遞迴。實作完跑 PR #81 既有 lifecycle 10 個 test 應仍綠。
- **published_at 補蓋順序**：必須先補 published_at 才呼叫 `_recompute_is_latest`，否則 winner 排序錯（被 epoch 當墊底）。
- **migration 環境依賴**：post-migration 不應依賴 K8s env secret 或外部服務 — 純資料層操作（純 ORM / SQL）。
- **monotonic assertion 改寫**：把 `test_agent_pod_phase_c_skill_bundle.py:201` 的 `assertIn` 改成 regex `>= 27`，避免本次與未來每次 bump manifest 都撞紅（`phase-versioned-test-assertion` pitfall — pattern doc 已記）。
- **rollout restart gotcha（瀏覽器 e2e 強制前置）**：local mode hostPath 改 `.py` 後 serve 進程不反映 → 瀏覽器驗證 lifecycle method 行為前**必須** `kubectl --context docker-desktop rollout restart deploy/feat-skill-list-odoo-dev-sandbox-odoo -n dev-sandbox-feat-skill-list` 並等 rollout 完成。leader 強調曾因此誤判 `action_deprecate`。
- **PAAS_CI_ADDONS_PATH**：sandbox `create.sh` 須帶 `PAAS_CI_ADDONS_PATH=/Users/eugene/Documents/woow/woow-paas-repo/paas-odoo-ci/addons`（缺第三方 addon 會 DB init 失敗）。`feat-skill-list` sandbox 已建好（PR #81 plan §17-25）。
- **OOM 釋放**：docker-desktop node 記憶體 ~4.7Gi，多 sandbox 會 OOM → 跑 e2e 前確認沒有其他 sandbox 佔用，必要時 `kubectl --context docker-desktop delete ns dev-sandbox-<other>` 釋放。
- **既有 fixture 行為變動**：`test_agent_pod_phase_c_skill_bundle.py:261-267` setUp 修法後 fixture skill `is_latest=True`（之前 False）；download endpoint 用 state 而非 is_latest，既有 SB.* 不受影響。加註解說明（無 code 變更）。
- **migration test 呼叫機制**：從 HttpCase / TransactionCase 內呼叫 `migrate(cr, version)` 需要 import migration module（檔案路徑 `src/migrations/18.0.1.0.28/post-fix-is-latest.py`，非 Python package）。實務做法是用 `importlib.util.spec_from_file_location` 動態載入，或在 test 內製造 drift 後跑 `self.env['woow_paas_platform.skill']`-side 同樣邏輯（避免直接呼叫 migration module）。實作細節由 tester 在 Task 3 落地時決定。

## 檔案異動清單

| 檔案 | 動作 |
|------|------|
| `src/models/skill.py` | 修改：`create()` / `write()` override 收斂 `_recompute_is_latest` + 補 `published_at` |
| `src/migrations/18.0.1.0.28/post-fix-is-latest.py` | 新增：幂等資料修補 |
| `src/__manifest__.py:3` | 修改：version 27 → 28 |
| `src/tests/test_agent_pod_phase_c_skill_bundle.py` | 修改：(a) :201 monotonic `>= 27`（移除硬編） (b) :261-267 setUp 加註解說明依賴 create-time recompute |
| `src/tests/test_skill_is_latest_drift_regression.py` | 新增：drift 修法回歸 test（TransactionCase）|
| `src/tests/test_skill_e2e_lifecycle_download.py` | 新增：後端 HttpCase（lifecycle ↔ download ↔ is_latest 串接 + migration 驗證）|
| `tests/e2e/skill/skill-admin-form.spec.{js\|ts}` | 新增：playwright-cli 瀏覽器 e2e（form UI wiring）|
| `src/tests/__init__.py` | 修改：註冊兩個新 Python test module |

## 對 PR #81 既有 spec 的 supersede 關係

PR #81 spec（`docs/plans/2026-Q2/2026-05-27-skill-admin-backend-ui-design.md`）§156-160 列為 follow-up 的「is_latest 雙寫漂移」**由本 spec 兌現修法**。本 spec 不修改 PR #81 spec 內容，僅在 PR #81 spec 引用列補本 spec 連結（透過 frontmatter `related:`）。
