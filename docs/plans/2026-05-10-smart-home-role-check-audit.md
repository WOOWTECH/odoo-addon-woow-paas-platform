# Smart Home Role Check Audit：把 mutating endpoints 拉到 Cloud-Service 同水位

**Status：** Backlog — issue #24 https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/24

**Goal：** PR #23 (#23) 已經把 Smart Home 的 `skip-trial` / `skip-trial-estimate` 兩個 endpoints 補上 admin/owner role check，pattern 對齊 cloud-service。但 SH 既有的四個 mutating endpoints — `create`、`delete`、`provision`、`change-plan` — 仍然只 gate 在「workspace 成員」這層，意思是任何 `role='user'` 或 `role='guest'` 的 member 都能對 workspace owner 的資源做破壞性或影響 billing 的操作。這個 PR 把這四個 endpoints 補齊到跟 cloud-service 一樣的 `[OWNER, ADMIN]` 門檻，一次到位、測試一起補。同時順手把 SH 六個有 access gate 的 endpoint 錯誤訊息統一成 `"Insufficient permissions"`（取代 PR #23 的 `"Admin access required"` 字面）— cloud-service 端的 wording 統一另開 issue 追蹤，這個 PR 不動。

**Architecture：** Role check 就是 PR #23 已經建立的兩行 pattern — `workspace.check_user_access(request.env.user)` 接 `if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]: return {"success": False, "error": "Insufficient permissions"}`。放置位置刻意分開：對於以 `action` 做 dispatch 的兩個 routes（`/api/workspaces/<wid>/smarthomes` 與 `/api/workspaces/<wid>/smarthomes/<id>`），role check 放在**每個 mutating action branch 開頭**，**不**放在 endpoint 最頂端，這樣 read-only 的 `list` / `get` / `refresh_status` 還能讓所有 workspace 成員使用。對於兩個專用 routes（`change-plan`，以及這次要改 wording 的兩個既有 skip-trial endpoints），role check 放在 function 開頭、`_get_workspace_or_error` 通過後立刻檢查。**這個 PR 不抽 helper** — SH 端補完後共 6 處重複，contained 範圍可控；要抽的話得連 `paas.py` 一起處理，那是另一個維度的工作（Q10 留 follow-up）。

**Tech Stack：** Odoo 18（Python 3.11），前端零變動。Backend 測試透過 `kubectl exec` 在 `dev-sandbox-smart-home-sub` 跑；sandbox playwright 跑四個 mutating flow + 兩個改了 wording 的 skip-trial flow 的手動 E2E。

**Branch：** `feature/sh-role-check-audit`（worktree 規劃在 `.maestri/floors/woow_paas_platform--featuresmart-home-role-check-audit`，由 implementation agent 在 PR #23 merge 後從乾淨的 `develop` 切出來）。

**Reference design：**
- PR #23 SH 端的 skip-trial role check 在 `src/controllers/smart_home.py:292-294`（`api_smarthome_skip_trial_estimate`）和 `:330-332`（`api_smarthome_skip_trial`）— 這個 PR 對四個 mutating endpoints 複製同一份 pattern，唯一差異是 wording 改成 `"Insufficient permissions"`。
- Cloud-service 對應位置：`src/controllers/paas.py:865-867`（`api_service_change_plan`）、`:946-948`（`api_service_skip_trial_estimate`）、`:968-971`（`api_service_skip_trial`）；另外 `api_workspace_services` 的 create dispatch 在 `:1021-1022`，`api_workspace_service` 的 update / delete dispatch 在 `:1066-1067` / `:1074-1075`，是 dispatch 內 role check 的正規寫法樣本。

---

## Brainstorm decisions（11 題 open questions 的決議）

這 11 題是在動任何 code 之前先 brainstorm 出來的；記在這裡是讓 reviewer 看得到 *為什麼* 設計成這樣，而不只是 *做了什麼*。

| # | 問題 | 決議 | 理由 |
|---|---|---|---|
| Q1 | 範圍：四個 mutating endpoints 一次補齊還是分批？ | **A — 一個 PR 補齊四個。** | 四個 endpoint 是同一個邏輯改動重複四次；分批沒帶來測試或部署上的減壓，反而 doubles review/merge cycle，git log 上也看不出「補齊 audit」的意圖。Issue #24 的 Acceptance 也是把四個寫在一起。 |
| Q2 | 角色矩陣：每個 endpoint 該允許哪些 role？ | **A — 四個全部 `[OWNER, ADMIN]`，完全 mirror cloud-service。** | CS 用這個矩陣在 production 跑了一陣子，沒有「member 想自助但被擋住」的回報。SH 和 CS 在 wallet 模型上同構（都消耗 workspace owner 點數），矩陣分歧會讓使用者心智模型混亂。日後想放寬（白名單加 role）比想收緊（破壞既有 client）便宜。 |
| Q3 | 既有資料：stg 上有沒有 non-admin 已經建過 SH？要不要 grandfather？ | **A — 跑 SQL audit，但不論結果都不 grandfather。** | 加 role check 不會 retroactively 改動任何既存 SH 記錄 — read paths（`get` / `list` / `refresh_status`）對所有 member 維持開放。唯一受影響的是「未來 non-admin 的 delete/provision/change-plan 呼叫被擋」，這本來就是 #24 的目的。Grandfather 機制（migration script 或白名單例外）的成本遠高於它解的問題。Audit 本身 5 分鐘就跑完，能讓 design doc 更紮實，但 audit 結果不會反過來改設計。 |
| Q4 | 測試結構：抽 helper 還是 paste 範本？ | **C — 沿用 PR #23 的 fixture-per-class，每個 endpoint 兩個測試（admin happy + non-admin reject），不抽 helper。** | PR #23 的 `TestSmartHomeSkipTrialEndpoints`（`src/tests/test_smart_home_subscription_flow.py:225-349`）已經是現成範本。複製四份讓每個測試獨立可讀；抽 `_assert_role_check` helper 會逼讀者先跳到 helper 才看得到 assertion。在這個量級重複是好事。 |
| Q5 | PR 切割：一個 PR 還是分開？ | **A — 一個 PR（與 Q1 連動）。** | 四個 endpoint 的 role check 是同一個邏輯改動，測試結構也一致，彼此沒有 order-dependent 風險，沒理由分。 |
| Q6 | 部署時機：merge 後立刻 `/woow:deploy-stg` 還是攢一波？ | **A — merge 後立刻 stg deploy。** | 這是已知的 access escalation surface（即便影響有限），security 修補不該 batch。PR #23 已經建立「merge → 立刻 deploy-stg」的節奏，stg deploy 成本低（一次 CI build + helm upgrade），batch 沒實質 ROI。 |
| Q7 | 錯誤訊息：跟 cloud-service 既有的 mix（`Permission denied` / `Admin access required`）還是統一？ | **統一成 `"Insufficient permissions"`**，覆蓋 SH 六個 access-gated endpoints（新加的四個 + PR #23 的兩個 skip-trial）。Cloud-service 不動，另開 cleanup issue 追蹤。 | (1) `"Insufficient permissions"` 是 role-name-agnostic — 矩陣若以後放寬（例如 `USER` 允許 create），wording 不用一個個 endpoint 改。(2) PR #23 的 `"Admin access required"` 能用但 "Admin" 字面對 role 名稱選擇耦合太緊；這個 PR 反正要動相鄰程式碼，順手換最划算。(3) 把 wording 改動 scope 限制在 SH 端，CS client 不會看到 string 變動，PR blast radius 可控。前端已 grep 確認：`src/static/` 完全沒有對這幾組字串做 match，純後端 cleanup。 |
| Q8 | Provision 的「triggers tunnel creation」語意要不要單獨開洞？ | **A — `[OWNER, ADMIN]`，與其他三個一致。** | Tunnel 創建消耗 K8s/network 資源（namespace quota、networking policy），即便 provision 本身不直接扣錢，這些資源也屬於 admin 治理範圍。對齊 CS 的 provision，使用者心智模型「兩種 service type，一套 access policy」。基於 `create_uid` 的例外（建立者可以 re-provision）被否決，因為生命週期複雜度（離職、轉手）超過便利性。 |
| Q9 | 用 dispatch 的 endpoint，role check 放哪一層？ | **A — 放在每個 mutating action branch 開頭、`_check_operation_allowed` 之前。** | 完全 mirror `paas.py:1066-1080` 的 `api_workspace_service`。兩個原因：(a) read-only 的 `get` / `refresh_status` 必須對所有 member 開放，endpoint top 一刀切會擋掉這些，加 whitelist 又讓 dispatch 變糾結；(b) 先檢查 access 再檢查 subscription state，non-admin 看到的是精準的 `"Insufficient permissions"` 而不是順帶的 `"subscription is in trial"` — access 是「該不該理你」的問題，應該先答。 |
| Q10 | 趁這次抽 `_require_admin_role()` helper？ | **A — 這個 PR 不抽。** Merge 後另開 follow-up issue 追蹤「unify role check pattern across SH + CS controllers」（與 PR #23 → #24 的接力模式相同）。 | 這個 PR focus 在「四個 endpoint 補齊」；抽 helper 會牽出 (1) 若要跨 controller 抽到 CS 端，blast radius 擴散到 cloud-service regression，或 (2) 只抽 SH 端 helper，等於在同 PR 同時放兩個維度的改動，分散 reviewer 注意力。解耦成 follow-up 讓兩個 PR 都簡單，而且 helper 的 shape 能依 12 個 call site 的全貌設計，不會被偏看 6 個 SH 的視角綁死。 |
| Q11 | 要不要多寫 owner happy path 測試？ | **A — 不寫。** | Role check 就是一個 `[ROLE_OWNER, ROLE_ADMIN]` 的 boolean，OWNER 和 ADMIN 走完全相同的分支。多寫 owner 測試沒拉到覆蓋率，只多維護成本。PR #23 已建立這個先例，一致比完整重要。 |

---

## Out of scope

- **抽 `_require_admin_role()` helper（Q10）。** Merge 後另開 follow-up issue，跨兩個 controllers 一次處理。
- **統一 `paas.py` 的錯誤 wording（Q7 邊界）。** Cloud-service 在這個 PR 維持既有的 `"Permission denied"` / `"Admin access required"` mix。Cleanup issue 會跟 Q10 follow-up 一起開，讓 wording 統一和 helper 抽取在同一個 CS-side cleanup 出貨。
- **動 wallet / billing / subscription 邏輯。** 這個 PR 純粹是「加 access gate」+「改字串」，model 層完全不動。
- **對 read-only endpoints 加 role check**（`api_smarthomes` action=`list`、`api_smarthome_detail` action=`get`/`refresh_status`、`api_smarthome_templates`、`api_smarthome_template_plans`）。Read paths 用 workspace 成員身分 gate 就夠，與 cloud-service 一致。
- **`paas.py:787` `api_odoo_addons_sync`** — 那是 `request.env.user.has_group('base.group_system')`（Odoo 系統 admin）gate，跟 workspace-scoped `[OWNER, ADMIN]` 矩陣是不同層次，不在這個 audit 範圍內。
- **前端改動。** 已 grep 確認 `src/static/` 對 `Admin access required` / `Insufficient permissions` / `Permission denied` 三組字串都沒有 match，client 是直接把 server 的 `error` 字串顯示出來，wording 改動純後端 cleanup，UI 不用配合改。

---

## Files & responsibilities

### Backend（commit 1：`fix(smart-home): require admin role for mutating endpoints`）

| File | Change |
|---|---|
| `src/controllers/smart_home.py` | 在四個位置加上 `access = workspace.check_user_access(request.env.user)` / `if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]: return {"success": False, "error": "Insufficient permissions"}`：(1) `api_smarthomes` dispatch 內 `action == "create"` 分支，在呼叫 `_create_smarthome` 之前（約現行 line 52-57）；(2) `api_smarthome_detail` dispatch 內 `action == "delete"` 分支，在 `_check_operation_allowed` 之前（約 line 89-94）；(3) `api_smarthome_detail` dispatch 內 `action == "provision"` 分支，在 `_check_operation_allowed` 之前（約 line 95-100）；(4) `api_smarthome_change_plan` 開頭，在 `_get_workspace_or_error` 通過之後（約 line 121-123）。同時把既有兩個 skip-trial endpoint 的 wording 改掉：`api_smarthome_skip_trial_estimate`（line 294）和 `api_smarthome_skip_trial`（line 332）從 `"Admin access required"` 改成 `"Insufficient permissions"`。 |

### Tests（commit 2：`test(smart-home): cover role check on four mutating endpoints + wording unification`）

| File | Change |
|---|---|
| `src/tests/test_smart_home_subscription_flow.py` | 新增 test class `TestSmartHomeMutatingEndpointsRoleCheck`（或擴充既有 class — 由 implementation 選擇，依 Q4 建議用獨立 class 讓 diagnostic 更乾淨），共 8 個新測試：`test_create_admin_succeeds` / `test_create_non_admin_rejected`、`test_delete_admin_succeeds` / `test_delete_non_admin_rejected`、`test_provision_admin_succeeds` / `test_provision_non_admin_rejected`、`test_change_plan_admin_succeeds` / `test_change_plan_non_admin_rejected`。每個 reject test 都 assert `assertFalse(res['success'])` 與 `assertIn('permission', res['error'].lower())`（對齊統一後的 wording）。**另外** 把既有兩個 reject test 的 assertion 一起改：`test_skip_trial_estimate_non_admin_rejected`（line 314-315）與 `test_skip_trial_non_admin_rejected`（line 348-349），`assertIn('admin', ...)` 改成 `assertIn('permission', ...)`，因為 wording 統一了。 |

### Documentation（commit 3：`docs(smart-home): design doc for role check audit (#24)`）

| File | Change |
|---|---|
| `docs/plans/2026-05-10-smart-home-role-check-audit.md` | 這份文件本身。 |

### Follow-up（不在這個 PR，merge 後再開 — 對應 Q10 / Q7 Out of scope）

| Action | Owner |
|---|---|
| 開 issue：「Unify role check + wording across SH/CS controllers（抽 `_require_admin_role()` helper，CS 端 wording 統一成 `Insufficient permissions`）」 | Implementation agent 在 PR merge 時開。引用這份 design doc 的 Q10 與 Q7 brainstorm 列為理由。 |

---

## Migration notes

- **Q3 audit 結果：** **Clean — 真實 non-admin 建的 SH = 0 筆**。由 `woow-paas-sandbox` 在 `dev-sandbox-stg` 與 `dev-sandbox-smart-home-sub` 兩處跑 SQL audit。

  Schema 細節（與初稿 query 假設不同處）：
  - `woow_paas_platform_workspace_access` join 欄位是 `user_id`（指向 `res_users.id`），**不是 `partner_id`**。實際 audit 用 `wa.user_id = sh.create_uid`。

  `dev-sandbox-stg` 結果：

  | sh_id | name | workspace_id | creator | role |
  |---|---|---|---|---|
  | 1 | E2E Test Home | 1 | `__system__` (uid=1) | NULL (no workspace_access) |
  | 2 | E2E Stg Test Home | 27 | admin | **owner** ✅ |

  Row 1 是 Odoo superuser 用 `__system__` 建的 E2E test fixture，不算 user-facing case。

  `dev-sandbox-smart-home-sub` 結果：1 row（`Test SH E2E`），admin/owner，乾淨。

  **結論**：既有 SH 只有 admin/owner 或 superuser 建立，無需 grandfather。superuser-created E2E fixture 不算 user-facing case。若 prod 將來部署也照此模式（owner 主導），預期 blast radius = 0。

- **Roll-out 風險：** 可忽略。改動是 additive（多 reject 一些以前會放行的 path）；沒有 schema migration、沒有 data backfill、沒有相依 addon 版本變動。Roll-back 就是一次 revert。

- **API contract delta：** 六個 endpoint 的錯誤回應變動。對四個新加 role check 的 endpoints（`create` / `delete` / `provision` / `change-plan`），non-admin member 以前拿到的可能是操作本身的成功/失敗結果（create / provision 通常 `success: true`；delete / change-plan 是 `_check_operation_allowed` 的 business error）；現在固定拿到 `{"success": false, "error": "Insufficient permissions"}`。對兩個既有 skip-trial endpoints，response shape 不變，error 字串從 `"Admin access required"` 變成 `"Insufficient permissions"`。前端 client 對兩種字串都沒做 match，所以 UI 不用配合改。

---

## Test plan

- [ ] **Unit tests**（`kubectl exec` in `dev-sandbox-smart-home-sub`）：
  - 8 個新測試覆蓋四個 mutating endpoints（每個 endpoint admin-happy + non-admin-reject 各一）。
  - 2 個既有 PR #23 skip-trial reject 測試的 assertion 修改（`'admin'` → `'permission'`）。
  - 期望 green：`test-tags '/woow_paas_platform:TestSmartHomeMutatingEndpointsRoleCheck,/woow_paas_platform:TestSmartHomeSkipTrialEndpoints'`。
- [ ] **Sandbox playwright E2E**（port-forward，不走 Cloudflare DNS）：
  - 前置：workspace owner（admin role）+ 邀請一位 `role='user'` 的 member。
  - 以 member 身分：點 *Add Smart Home* → modal 開啟但 submit 後出現可見的 `"Insufficient permissions"` 錯誤；SH 清單不變。
  - 以 member 身分：進到既存 SH detail page → 點 *Delete* → 被擋，顯示 `"Insufficient permissions"`；refresh 後 SH 還在。
  - 以 member 身分：在同一 SH 點 *Skip Trial* → 被擋，顯示 `"Insufficient permissions"`（順便 regression 驗 wording 變更）。
  - 以 member 身分：點 *Change Plan* → 被擋，顯示 `"Insufficient permissions"`。
  - 以 admin 身分：四個流程重做一次，每個都正常完成。
  - Console：全程 0 errors、0 warnings。
- [ ] **Stg E2E（merge 後）：** 立刻 `/woow:deploy-stg`，在 `stg.woowtech.io` 重跑 admin/member matrix，確認 `paas-odoo-stg` 把 wording 變更與四個 endpoint gate 都正確包進去。

---

## Implementation hand-off note

給接手實作的 agent：

1. **Worktree 起點。** 等 PR #23 merge 後再從 `develop` 切 worktree，這樣可以直接繼承 PR #23 在 `smart_home.py:292-294, 330-332` 的 role-check pattern。建議名：`feature/sh-role-check-audit`，放在 `.maestri/floors/woow_paas_platform--featuresmart-home-role-check-audit`。

2. **Q9 放置位置很關鍵，不能放錯層。** `api_smarthomes` 和 `api_smarthome_detail` 兩個 endpoint，role check 要放**在 action branch 內**，不是 function 開頭。讀 `paas.py:1063-1082` 把 mirror pattern 看熟。放在 function 開頭會擋掉 `list` / `get` / `refresh_status`，破壞 workspace 瀏覽功能。

3. **Q7 wording 改動包含既有的 PR #23 兩處。** 在 `src/controllers/smart_home.py` grep `"Admin access required"`，會出現兩個 hit（line 294 與 332），兩個都要在同 PR 改成 `"Insufficient permissions"`，對應兩個 reject test 的 `assertIn('admin', ...)` 也要改成 `assertIn('permission', ...)`。**不要動 `paas.py` 的 wording** — 那是 Q10 follow-up 處理。

4. **測試 fixture 策略（Q4）。** PR #23 的 `TestSmartHomeSkipTrialEndpoints` setUp 已經建好 admin workspace + `_add_member(login, role)` helper（建 role='user' member）。可以擴充同 class 加 8 個新測試，或開 sibling class `TestSmartHomeMutatingEndpointsRoleCheck` 用同樣 setUp 形狀 — 兩者皆可，diagnostic 隔離考量下 sibling class 略勝。

5. **Q10 follow-up issue 在 merge 時開。** PR merge 時開一個 issue，標題類似 `"Unify role check pattern + wording across SH/CS controllers (extract _require_admin_role() helper)"`。引用這份 design doc 的 Q10 與 Q7 Out-of-scope。Follow-up 的 scope：抽共用 helper、CS 端 wording 統一成 `"Insufficient permissions"`、CS regression test 的 wording assertion 跟著改。規模估計與本 PR 相當。

6. **前端不用改。** 已用 `grep -rn "Admin access required\|Insufficient permissions\|Permission denied" src/static/` 確認沒有任何 match。Client 是把 server 的 `error` 字串原樣 render 到 toast/banner，wording 改動完全在後端。

7. **不要 pre-emptively 快取 `access`。** 每個 endpoint 各自重算 `access = workspace.check_user_access(request.env.user)`，因為 (a) 一次 indexed query 成本很低，(b) PR #23 已建立先例，(c) 若日後 refactor 把 dispatch 拆成多個 action handler，快取會讓 access 漂移不易察覺。
