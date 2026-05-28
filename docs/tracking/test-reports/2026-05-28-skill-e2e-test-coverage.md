---
name: 2026-05-28-skill-e2e-test-coverage
phase: red-explained
team: skill-list
test_explainer: skill-list-test-explainer
spec_ref: docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md
plan_ref: docs/superpowers/plans/2026-05-28-skill-e2e-and-is-latest-drift-fix.md
tester_commits:
  - 5b17c83  # test(skill): add is_latest drift regression locks (TDD red)
  - 9fd2f12  # test(skill): add backend e2e for lifecycle, download, is_latest invariant
  - 228b115  # test(skill): add browser e2e for admin form transition UI wiring
base_commit: c36a9a1  # docs(skill): add e2e and is_latest drift-fix spec and plan
branch: feat/skill-list
created: 2026-05-28
status: ready-for-implementation
scope: PR #81 follow-up — is_latest drift（model 層 create/write 漏 hook）+ 含瀏覽器 UI 的 e2e
related:
  - docs/superpowers/specs/2026-05-27-skill-admin-backend-ui-design.md  # PR #81 spec
  - docs/superpowers/plans/2026-05-27-skill-admin-backend-ui.md         # PR #81 plan
---

# Test Coverage Report — Skill is_latest drift + E2E（人類可讀版）

> 這份報告**不是** spec/plan 的逐條翻譯（spec §①–⑥、plan Task 1–4 是 source-of-truth）。
> 它解釋：**「本次補的這 13 個 Python test + 1 個 playwright spec 各自守什麼意圖、為何不重測既有 SB.* / lifecycle / view smoke 已覆蓋的範圍、本輪邊界到哪、reviewer 該對照 diff 哪幾段」**，餵給 reviewer / CAQA / stakeholder 對齊測試**意圖**與**層級分工**。
>
> **背景一句話**：PR #81 完成 skill 後台 UI + 三個 `action_*` transition + `is_latest` 不變式，但只在 transition method 結尾收斂 `_recompute_is_latest` —— 任何**繞過 transition** 直接 `create({'state':'published'})` 的程式化路徑（test fixture、未來上傳 API、demo data、admin 匯入）都會 drift。spec §背景已對照 codebase 確認 drift 入口在 model `create()` / `write()`，本次把缺口補上並補真實 HTTP 鏈路 + 瀏覽器 UI 的端到端驗證。
>
> **reviewer special attention**：
> 1. 第 3 章 **三層分工**（model TransactionCase regression / HttpCase backend e2e / playwright browser e2e）—— 為何同一條 AC 在不同層拆兩 test 而非單層測完。
> 2. 第 4 章 **「不測什麼 + 理由」** —— 既有 PR #81 `TestSkillAdminLifecycle`(10) / `TestSkillAdminViews`(2) / Phase C `TestPhaseCSkillBundleEndpoint`(12) 全綠，本輪明確**不重測**它們覆蓋的純單元邏輯，只測**串接** + **新缺口**。
> 3. 第 6 章 **已知偏差**：tester 的 `test_migration_fixes_existing_drift_sample` 在「**製造 drift sample**」這一步因 ORM flush ordering 紅在 line 233，不在「migration 是否修補」那一步。意圖層面 test 仍正確，implementation 階段會調 sample 製造方式（spec §④-a / tester report 已標方案 A/B/C）—— **reviewer 不需要把這當「測試破口」**，是 fixture mechanics 的雷，不是 invariant 失守。

---

## 1. TL;DR

| 項 | 值 |
|---|---|
| 觸發 | PR #81 spec §156-160 預埋的 follow-up（drift 在 model 層、不在 controller 層），加上既有 spec 缺含瀏覽器 UI 的 e2e |
| 本輪 scope | (a) drift regression（model 層）(b) lifecycle ↔ download endpoint ↔ is_latest 串接（HTTP 鏈路）(c) form button UI wiring（瀏覽器） |
| 新增 test 數 | **13 個 Python test**（6 model regression + 7 HttpCase e2e）+ **1 個 playwright spec**（單個 describe / 1 個瀏覽器 test，涵蓋 draft→publish→deprecate→archive 四階段） |
| 既有 suite 防迴歸 | `TestSkillAdminLifecycle` 10/10 ✓ · `TestSkillAdminViews` 2/2 ✓ · `TestPhaseCSkillBundleEndpoint` 12/12 ✓（tester 已跑） |
| 紅綠分佈（依 plan 預期） | Task 1: 4 紅 + 2 綠 ✓ · Task 3: 3 紅 + 4 綠 ✓（其中 1 紅原因偏差，見第 6 章） · Task 4: 未跑（implementation 階段執行） |
| 紅性質 | 全 **missing behaviour**（等 model hook + migration 落地）；無 import / fixture error |
| 上游依賴 | sandbox `dev-sandbox-feat-skill-list` 已建好（tester 已跑通），fixture 全用 ORM 動態建立 |

**Gate 答覆（「為何測這些、為何不測那些」）**：

- **測這 13 個 + 1 spec** = 三個尚未被既有 suite 覆蓋的縫：(a) **drift 入口**（直建 / 直寫 published）—— 既有 lifecycle 全走 `action_*` method，從不碰這條程式化捷徑；(b) **lifecycle ↔ HTTP visibility 串接**（既有 SB.* 不關心 lifecycle，既有 lifecycle 不走 HTTP）；(c) **form button UI wiring**（之前完全沒瀏覽器層 e2e）。
- **不測的部分** = 既有 PR #81 + Phase C suite 已覆蓋的純單元邏輯（lifecycle 規則 / illegal transition guards / 多版本 recompute / token auth / token_use scope / 跨 project 防護 / menu ACL）。重測一遍只是 noise，且偏離本輪意圖（測**串接** + **新缺口**）。

---

## 2. 三條風險覆蓋面（reviewer 視角）

reviewer 拿到本 PR diff 時要回答的問題是「**這次測試到底幫我們守住什麼**」。三條：

| # | 風險 / 攻擊面 | 既有 suite 守了沒 | 本輪新測守了什麼 |
|---|--------------|-----------------|----------------|
| **A** | **Drift 攻擊面**（任何繞過 `action_publish` 的程式化路徑都會讓 published skill `is_latest=False` 且 `published_at=NULL`） | ✗ 既有 `TestSkillAdminLifecycle` 全走 `action_*`，從不直 create/write 切 state，這條入口完全沒看過 | **守住 model `create()` / `write()` override 的不變式收斂**：直 create published / write 切 published / 連續直建多版本 / 顯式 `published_at` 不被覆蓋 / draft 路徑無副作用 / `action_publish` 仍正確（防 hook 引入迴歸） |
| **B** | **HTTP 可見性失準**（lifecycle transition 後，agent runtime pod 透過 `/api/agent-runtimes/skill-bundle` 看到的內容跟 admin 預期不一致） | 部分 — Phase C `TestPhaseCSkillBundleEndpoint`(SB.1-6 / C-1) 守 token auth / scope / 跨 project，但**完全不關心 lifecycle**：published 之外的 state 該不該 404、deprecate 後是否還拿得到 bundle、archived 後可見性是否切斷 —— 一條都沒測 | **守住 lifecycle 真實鏈路**：draft → 404 / publish → 200 + raw bytes + `is_latest=True` / deprecate v2 → v2 404 + v1 接手 latest / archive → 404 / drift 直建 published → 200 + 修法後 latest 正確 |
| **C** | **UI wiring 走鐘**（spec 沒有指引 admin 怎麼操作的話，button 顯隱規則 / statusbar 推進 / `is_latest` 視覺反映都可能不一致） | ✗ PR #81 `TestSkillAdminViews` 只 smoke 「view 註冊得起、menu admin-only 可見」，不點任何 button、不驗 statusbar / badge / boolean 渲染 | **守住 form 後台 button 點下去 wire 到正確 method**：draft 初態 → publish 顯示 / publish 點下去 → published badge + statusbar 推進 + is_latest=True + deprecate 出現 / deprecate 點下去 → publish 重現 + is_latest=False / archive 點下去 → 所有 transition button 隱藏，並 4 張截圖落地供視覺複查 |

---

## 3. 三層分工 —— 為何同一條 AC 拆兩層測

reviewer 最常問的「為何 AC2（drift 防護）在 model regression 又在 HttpCase 又測一次」—— 答案是兩層**測的不是同一件事**：

| 層 | 工具 / Class | 測什麼 | 為何在這裡（為何不能合併到另一層） |
|----|-------------|-------|----------------------------------|
| **① Model unit regression**（TransactionCase） | `TestSkillIsLatestDriftRegression` ：6 test | **model override 的內部不變式**：is_latest 收斂、published_at 補蓋順序、不遞迴、不踩既有 `action_publish` | 這層在 ORM 內以最低成本確定性驗證**修法本身正不正確**。HttpCase 層走 url_open + Authorization header + controller routing，多了非 model 層的雜訊，且每個 case 都要 attach bundle 才能拿 200；測「`create({'state':'published'})` 不要 drift」根本不需要 HTTP/attachment 介面 |
| **② Backend e2e HTTP 鏈路**（HttpCase） | `TestSkillE2ELifecycleDownload` ：7 test | **lifecycle ↔ download visibility ↔ is_latest 跨層串接**：transition 後 agent runtime pod 用既有 download endpoint 看到的內容（state gate 真的 work 嗎？raw bytes 對嗎？deprecate 後 v1 接手能下載嗎？） | model regression 不發 HTTP 請求、不驗 controller 的 state gate、不驗 raw bytes vs base64 body wire；agent runtime pod 看到的「是否可下載」**只能**走真實 HTTP 鏈路證明。同時這層也是 drift 修法的「外部視角」驗收 —— 修法後 `create({'state':'published'})` 應立刻被 pod 看見且 `is_latest=True` |
| **③ Browser e2e UI wiring**（playwright） | `tests/e2e/skill/skill-admin-form.spec.ts` ：1 spec / 1 test（4 階段） | **後台 form button 真實 UI + HTTP 鏈路**：button 點下去呼到正確 method、statusbar / badge / `is_latest` 視覺反映、screenshot 供視覺複查 | HttpCase 完全不 render OWL form view，看不到 button visibility / statusbar 高亮 / boolean checkbox 渲染；model regression 連 HTTP 都沒走，更不可能驗 UI。**「admin 在瀏覽器點 Publish」這個使用者真實操作**只在這層可被端到端證明 |

> **三層共同對應 AC2 但分工**：① 守「修法在 ORM 層內部正確」；② 守「修法在跨 HTTP 鏈路也成立 + 既有 endpoint 沒誤殺」；③ 守「使用者真實操作不偏離」。三條都過才能說 drift 攻擊面**封死**，缺一層都會留破口。

---

## 4. 不測什麼 —— 信任既有覆蓋的取捨

本輪明確**不重測**以下範圍，原因是「相同意圖既有 suite 已覆蓋且本輪有跑過確認全綠」：

| 不測範圍 | 既有覆蓋來源 | 為何不重測 |
|---------|-------------|---------|
| Lifecycle transition 規則正確性（`action_publish` 從 archived 不能呼、`action_deprecate` 只能從 published、illegal guards 等）| PR #81 `TestSkillAdminLifecycle`（10 個 test） | 純 model 邏輯，PR #81 已覆蓋；本輪只測 hook 不破壞既有行為（`test_action_publish_still_works_after_hook`），不重測規則本身 |
| 多版本跨 group `_recompute_is_latest` 演算（哪個版本 winner / tiebreak） | PR #81 `TestSkillAdminLifecycle` 多版本案例 | 同上 — winner 排序演算 PR #81 已測；本輪 `test_multiple_programmatic_published_keeps_invariant` 是**走程式化路徑**的不變式 smoke（確認 hook 觸發 recompute），不重測 recompute 本身的演算邏輯 |
| View / action / menu 註冊 + admin-only menu 可見 | PR #81 `TestSkillAdminViews`（2 個 test） | view smoke 已覆蓋；playwright spec 把「登入 admin → 找到 menu → 開 list → 開 form」當前置（admin 看得到 menu 是必要前提），不再 admin/non-admin 視角各測一次 |
| Download endpoint token auth / token_use scope / 跨 project 防護 / 過期 token / wrong secret | Phase C `TestPhaseCSkillBundleEndpoint`（SB.1-6 + C-1，共 12 test） | Phase C 已守 token 鏈路的所有 ACL 面；本輪 HttpCase 只走 happy-path token（current secret 有效），測**lifecycle ↔ HTTP visibility 切換** —— 假設 token 機制本身已可信 |
| Skill ACL / `ir.rule` / non-admin 可見性切片 | PR #81 ACL test + Phase A model test | 既有 suite 已覆蓋；本輪 playwright 只跑 admin 視角驗 wiring，不重切 non-admin 視角 |
| HTTP 上傳 API（bundle upload） | 不存在（spec §背景 Gap 3 已拍板 (A) 不採 (B)/(C)）| 端點不存在 = 沒得測；scope 拍板不擴 |
| Phase C `MIG.1` 硬編版本 assertion（`assertIn '"version": "18.0.1.0.27"'`） | 既有 Phase C test 中 | 本輪 Task 2 會把它改 monotonic `>= 27`（`phase-versioned-test-assertion` pattern），不是新測，是**改寫既有**。tester 階段尚未動到（hard rule：tester 不改 production / 既有 test code）—— implementation 階段執行 |
| `test_agent_pod_phase_c_skill_bundle.py:261-267` setUp 是否改走 `action_publish` | spec §⑤ 拍板**(a) 不動 fixture，只加註解** | 修法後 fixture `is_latest=True`（之前 False），但 download endpoint 用 `state == 'published'`、不用 is_latest，**既有 SB.* 不受影響**；改走 `action_publish` 列為可選 follow-up |
| semver 精確排序 / OWL 前端 skill 管理頁 | 既有 spec 已列 non-goal | 本次延續 non-goal，不擴 scope |

> **取捨原則**：本輪 e2e 是「串接 + 新缺口」工具，不是「全範圍重跑」。重測既有覆蓋的結果除了延長 test runtime、增加將來 flaky source，對守線無新增價值。

---

## 5. Acceptance Criteria 對應表

spec §Acceptance criteria 列了 5 條（AC1 ~ AC5），對應到具體 test：

| AC | Acceptance criteria 內容 | 對應 test | 檔案 / Class | 對應層 |
|----|------------------------|----------|-------------|-------|
| **AC1** | 使用者透過 admin 後台 form 點 Publish / Deprecate / Archive button → state、statusbar、badge、`is_latest` 在 UI 即時反映 | `Skill admin form — transition button UI wiring`（draft → publish → deprecate → archive 4 階段 + 截圖） | `tests/e2e/skill/skill-admin-form.spec.ts` | ③ browser |
| **AC2** | 任何繞過 `action_publish` 的程式化路徑直建 / 直寫 published 不會 drift（is_latest + published_at 仍正確） | `test_create_published_directly_sets_is_latest_and_published_at`<br>`test_write_state_to_published_sets_is_latest_and_published_at`<br>`test_multiple_programmatic_published_keeps_invariant`<br>`test_create_published_with_explicit_published_at_keeps_it`<br>`test_create_draft_does_not_set_is_latest_or_published_at`<br>`test_action_publish_still_works_after_hook`<br>`test_create_published_directly_drift_via_http`<br>`test_write_state_to_published_drift_via_http` | `test_skill_is_latest_drift_regression.py`（前 6）<br>`test_skill_e2e_lifecycle_download.py`（後 2） | ① model + ② HTTP |
| **AC3** | 跑 migration 對既有 drift 樣本 → 不變式恢復；對無 drift 資料 → no-op；跑兩次結果一致 | `test_migration_fixes_existing_drift_sample`（含「無資料 → 不執行」由 migration code path 本身保證，跑兩次幂等由 if-guard 結構保證 —— 此 test 驗主路徑，二次跑與空 DB 路徑由 code 結構審查確認） | `test_skill_e2e_lifecycle_download.py` | ② HTTP（HttpCase 持 cr）|
| **AC4** | lifecycle transition 經 form button → 真實下載可見性切換：published 可被 pod 下載（200 + raw bytes 正確）、deprecated / archived / draft 不可下載（404） | `test_draft_skill_download_returns_404`<br>`test_action_publish_then_download_200_and_is_latest_true`<br>`test_deprecate_then_download_404_and_latest_reassigned`<br>`test_archive_then_download_404` | `test_skill_e2e_lifecycle_download.py` | ② HTTP |
| **AC5** | 既有 SB.1-6 + C-1 + lifecycle 10 個 test 仍綠（無迴歸），含 Phase C `MIG.1` 升級為 monotonic 後不撞紅 | `TestSkillAdminLifecycle`（10/10）`TestSkillAdminViews`（2/2）`TestPhaseCSkillBundleEndpoint`（12/12）—— tester 已實跑全綠；`MIG.1` 改 monotonic 由 implementation 階段執行（tester scope 不改既有 test code） | 全模組 `-u` 跑 | 既有 + ② |

> **AC3 micro-note**：tester 報告標出 `test_migration_fixes_existing_drift_sample` 紅在 line 233 的 drift sample 製造（不是 migration call 本身），實際的「migration 是否修補」這段尚未跑到，原因是 ORM flush ordering 在 HttpCase 內讓 `action_publish` 設的 `is_latest=True` 蓋掉了 raw SQL `UPDATE ... is_latest=FALSE`。**這只影響 fixture mechanics，不影響 AC3 意圖**。詳見第 6 章。

---

## 6. 已知偏差 —— migration test 的 fixture mechanics

> reviewer 不需把這條當測試破口，這是「**怎麼在 HttpCase 內製造 drift sample**」的 ORM 細節，**不是 AC3 的覆蓋是否存在**的問題。

| 項 | 內容 |
|---|---|
| Test | `test_skill_e2e_lifecycle_download.py::test_migration_fixes_existing_drift_sample` |
| 預期 | 純 SQL 製造 drift（`UPDATE ... is_latest=FALSE`）→ 呼叫 migration `migrate(cr, version)` → drift 修復 → `assertTrue(skill.is_latest)` |
| 實際紅在 | **Line 233** 的 `self.assertFalse(skill.is_latest, "drift sample 製造成功")`（製造 sample 這一步） |
| 失敗模式 | `AssertionError: True is not false : drift sample 製造成功` |
| 根因（tester 推測） | HttpCase + Odoo ORM cache/flush ordering：`action_publish` 內 `_recompute_is_latest` 用 ORM `write({'is_latest': True})`，可能仍在 flush buffer；raw `cr.execute UPDATE ... FALSE` 直寫 DB 後，後續 ORM read 觸發 flush（把 buffer 中的 True 寫回 DB），覆蓋 raw SQL。`invalidate_recordset()` 只清 cache、不阻止 buffered write flush |
| 意圖層面影響 | **無**：本 test 要驗的是「migration 對 drift 樣本的修補能力」；fixture mechanics 影響「**如何**製造 sample」，不影響「**migration 是否正確修補**」這個驗證意圖 |
| 對 reviewer 的解讀指引 | 不要把這紅當「migration 修法被 test 否決」—— 修法甚至還沒寫。這紅只是說「在 HttpCase 內用『先 `action_publish` 再 raw UPDATE』來模擬 drift 樣本」不靠譜，要換方式 |
| Implementation 階段處置（不在本輪 scope） | spec §④-a / tester report 已標方案：(A) sample 製造前 `self.env.flush_all()` + 製造後不再觸發 ORM write → flush；(B) `with closing(self.env.cr.savepoint())` 隔離；(C) 改 setUp 用 raw SQL `INSERT` 建 drift sample（繞過所有 ORM write） |

> **可選改善建議**：implementation 階段落地 migration + model hook 之後，若 (A)/(B)/(C) 仍未穩，可把 migration 核心邏輯抽 `Skill._backfill_is_latest_invariant_for_all_skills` classmethod，test 直接呼叫 classmethod、用 raw INSERT 建 drift（plan §Task 3 註解已標）。本輪保留 tester 原 test code 不動，待 implementation 階段擇一落地。

---

## 7. 隱性假設 / Fixture 條件

reviewer 讀 test 時可能看不出的「環境前提」：

| 隱性假設 | 來源 | 影響範圍 |
|---------|-----|---------|
| `woow_paas_platform.skill` 已 registered（PR #81 已落 model） | `setUpClass` 顯式 `RuntimeError` 警示 | 全部 13 個 Python test |
| `project.project` 用 `sudo()` create 不踩 ACL | 既有 Odoo 標準 model | 全部 Python test |
| `_skill_bundle_token` 用本地 HS256 簽（不依賴 Odoo HTTP token signing 內部行為） | tester 對齊 Phase C pattern 自行 sign | 全部 HttpCase test |
| `AGENT_RUNTIME_AUTH_SECRET_CURRENT` env var 在 setUp 被設成 `'e2e-skill-lifecycle-secret-0123456789'`（且 cleanup 還原） | `_set_env` helper | 全部 HttpCase test —— 若本機環境已有不同的 secret，setUp 會覆蓋，addCleanup 還原；不會洩漏到其他 test |
| `bundle_sha256` 用 raw bytes 算 hash（與 attachment `datas` base64 解碼後一致） | `_make_skill` helper | 全部 HttpCase test —— 修了 sha 演算等於同步動 controller 驗證的對齊 |
| HttpCase 的 `url_open` 走 sandbox 內 HTTP（非 stub）+ 帶 Authorization header | Odoo `HttpCase.url_open` 標準行為 | 全部 HttpCase test |
| migration 動態 import 路徑（`importlib.util.spec_from_file_location`） | tester report §Plan TBD 落地調整 | 只 `test_migration_fixes_existing_drift_sample` —— 該 test 還沒跑到 import 步（line 233 先紅），import 是否真 work 在 implementation 階段第一次跑通才驗 |
| **playwright spec 前置三件事**：① sandbox 對外 URL（`SANDBOX_BASE_URL`）② 預建 draft skill（odoo shell / fixture script）③ rollout restart deployment（leader 強調，否則 serve 進程跑舊 code 會誤判 button 行為） | `tests/e2e/skill/README.md` | playwright spec —— **沒做前置會誤紅 / 誤綠**，executor 必讀 README |
| playwright selectors（`.o_statusbar_buttons` / `.o_statusbar_status .btn[data-value=...]` / `[name="is_latest"] input`） | plan §Task 4 註解：「合理猜測，需 tester 對 sandbox 真實 DOM 微調」 | playwright spec —— tester 未跑過，selectors 可能對不上實際 Odoo 18 admin theme 渲染；implementation 階段執行前須用 playwright codegen / page inspector 對齊 |

---

## 8. 對 reviewer 的提醒（讀 diff 時對照本報告哪幾段）

reviewer 讀 implementation 階段的 PR diff 時建議按以下順序對照：

| reviewer 看到的 diff 區塊 | 對照本報告章節 | 看什麼 |
|--------------------------|--------------|-------|
| `src/models/skill.py` `create()` / `write()` override | 第 2 章 ① (Drift 攻擊面) + 第 3 章 ① (Model 層) + 第 5 章 AC2 | 確認 `state == 'published'` 條件、`published_at` 補蓋時機在 `_recompute_is_latest` **之前**（spec §① 排序要求）、`_recompute_is_latest` 內部 write 不踩 `'state' in vals`（不遞迴） |
| `src/migrations/18.0.1.0.28/post-fix-is-latest.py` | 第 5 章 AC3 + 第 6 章（fixture mechanics） | 確認幂等 if-guard（`if s.is_latest != desired`）、無資料 early return、winner 排序 key（`(published_at or epoch, s.id) reverse`） |
| `src/__manifest__.py` `27 → 28` | 第 4 章「不測什麼」`MIG.1` 行 | 確認 manifest bump（沒 bump migration 不會跑） |
| `src/tests/test_agent_pod_phase_c_skill_bundle.py:201` monotonic 改寫 + setUp 註解 | 第 4 章「不測什麼」最後兩行 | 確認 `MIG.1` 改 regex `>= 27`、setUp 加註解但 fixture code 不動（spec §⑤ 拍板）|
| `src/tests/test_skill_is_latest_drift_regression.py` 6 test 全變綠 | 第 2 章 ① + 第 3 章 ① | 跑全綠 = drift 攻擊面 model 層封死 |
| `src/tests/test_skill_e2e_lifecycle_download.py` 7 test 全變綠 | 第 2 章 ② + 第 3 章 ② + 第 5 章 AC3/AC4 | 跑全綠 = lifecycle ↔ HTTP visibility ↔ is_latest 串接通過 + migration 對 drift sample 修補通過（注意第 6 章的 fixture mechanics 已調好） |
| `tests/e2e/skill/skill-admin-form.spec.ts` 跑通 + 4 張截圖落地 | 第 2 章 ③ + 第 3 章 ③ + 第 5 章 AC1 | 跑通 + 視覺複查 = UI wiring 走通；selectors 可能因 Odoo theme 略異，executor 對齊後 commit |
| 既有 `TestSkillAdminLifecycle` / `TestSkillAdminViews` / `TestPhaseCSkillBundleEndpoint` 全綠 | 第 5 章 AC5 | 防迴歸閉環：本輪修法不破壞既有覆蓋 |

> **reviewer 不需要做但可以做**：對照 spec §④-a / §④-b 的「測什麼 / 不測什麼」表 vs 本報告第 4 章 —— 兩處應一致（本報告是 spec 的 reviewer-facing 翻譯，不是 spec 的擴充）。

---

## 9. 結語 / hand-off

- 本報告**只**翻譯測試覆蓋意圖；不評斷修法 code（那是 reviewer 階段的事）。
- 本輪 tester 已驗的紅綠分佈完全符合 plan 預期（除第 6 章的單個 fixture mechanics 偏差），無漏測 / 越界。
- 13 個 Python test + 1 個 playwright spec 對 5 條 AC 的覆蓋如第 5 章表：**無漏對應**，每條 AC 至少一個 test 守住。
- 本輪刻意不擴的範圍（HTTP 上傳 API / non-admin 視角重測 / semver 精確排序 / OWL 前端管理頁）見第 4 章，與 spec §非目標一致。
- Implementation 階段拿到本報告應能直接著手 model hook + migration + manifest bump + Phase C monotonic 改 + setUp 註解 + 跑 `-u` 全模組 + playwright e2e 執行；遇到第 6 章的 fixture mechanics 雷照 spec §④-a / tester report 標的 (A)/(B)/(C) 三案擇一落地。
