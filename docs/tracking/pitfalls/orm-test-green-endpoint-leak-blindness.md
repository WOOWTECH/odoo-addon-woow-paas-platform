---
name: orm-test-green-endpoint-leak-blindness
description: B1-B5 backend ORM test 全綠 + plan「後端已完整支援」描述，遮蔽 controller layer sudo bypass 第 2、3、4 instance — 直到 CAQA grep 才發現 _list_projects/_list_tasks/_get_project_stages 漏掃
metadata:
  type: pitfall
---

# Pitfall: B1-B5 ORM test 全綠 + plan 「後端已完整支援」描述 = controller endpoint 安全的錯覺

> 來源：cycle 6（decouple project frontend + controller 收尾，2026-05-28）。Plan §1.C 寫「後端已完整支援（**無需改動**）」並列 4 條證據（model + ir.rule + test + create override），但**漏掃了同 controller 內的 list endpoint** — `_list_projects` / `_list_tasks` 都用 `request.env[X].sudo().search(domain)` 繞 ir.rule。本 pitfall 是 [[orm-vs-endpoint-acl-test-coverage]] pattern 的 symptom-side 紀錄：pattern 是 prescription（不可繞），pitfall 是「我們為什麼又踩到了一次」的故事。

## Symptom

- TDD red phase：tester 寫 T1 (unbound create + creator visibility) + T2 (cross-user deny visibility) + B1-B5 backend baseline，**全綠之後** plan A+A+A signed-off 進 implementation
- Implementation 改 controller D1+D2+D3（前端 dispatcher guard + message_subscribe）+ frontend modal + support_service.js — **完全沒碰 `_list_projects`**
- 在 playwright TC4（跨 user ACL 隔離）驗證時，Bob 切到 `/woow#/projects` page → 看到 Alice 建的 unbound project（leak）
- TC4 紅 → 才回頭看 `_list_projects`（line 1186 with sudo）→ 發現 `sudo().search([('cloud_service_id.workspace_id', '=', workspace.id)])` 在 bare-URL `domain=[]` 時 leak 整 db
- 補 TC4 follow-on（drop sudo）— 進 CAQA
- CAQA round 1：grep `sudo().search()` 同 controller → 抓到 **`_list_tasks` (line 1325) 完全同源**（與 `_list_projects` 對稱）→ BLOCKER A1
- 補 BLOCKER A1 fix（drop sudo 1 行 + T4 endpoint test 完全對稱 T3）— round 2 APPROVED WITH CAVEATS
- 順便撈出 `_get_project_stages` (line 1046, 1139-1177) 雙重洞 + `api_support_stats` (line 974) 漏 unbound leg = CAVEAT #2 / #3 follow-up

→ **同 controller 同 family 共 4 instance**（2 closed + 2 follow-up）；TDD red phase + 5 condition backend ORM test（B1-B5）+ plan「後端已完整支援」描述**全部沒抓到** — 直到 CAQA grep + playwright TC4 才暴露。

## Why it happens

### 3 個遮蔽同時發生

**遮蔽 1：Plan §1.C「後端已完整支援」的證據選擇**

Plan `2026-05-28-decouple-project-from-workspace-frontend-plan.md` line 49-55 寫：

```
### C. 後端已完整支援（**無需改動**）
| 位置 | 證據 |
| `project_project.py:43-48, 106-109` | workspace_id 為 computed-from-cloud_service_id 的 optional 欄位 |
| `ir_rules.xml:65-78` project_rule_workspace_member | dual-leg domain：bound → workspace member；unbound → follower OR creator |
| `test_project_decouple.py:46-59, 169-198` | AC#1/#4/#5/#6 既有 test 覆蓋 unbound create + ACL |
| `project_project.py:114-161` create() override | 自動 bootstrap project_access_ids 給 creator 當 owner |
```

4 條證據都是「model + rule + ORM-layer test」的 stack — **完全沒掃 controller layer 是否真有 follow 這些 ir.rule**。

**遮蔽 2：B1-B5 backend test 走 `with_user()` ORM 路徑**

`test_project_decouple.py` 的 5 條 test 都用 `Model.with_user(user).search(...)` idiom：

```python
def test_ac4_unbound_project_isolated_from_non_follower_same_workspace(self):
    # ...
    projects_seen = self.env['project.project'].with_user(bob).search([])
    self.assertNotIn(unbound_project, projects_seen)
```

Odoo ORM `with_user()` 走 ir.rule，所以 B1-B5 全綠 = ir.rule 設定對。**但 controller `request.env[X].sudo().search()` 是另一條 path**，B1-B5 完全沒 exercise 該 path。

→ TDD red phase 走完，tester / implementer / leader 都看到 14/14 全綠（B1-B5 + T1+T2），對 controller layer 假設「就跟 ORM layer 一樣安全」— 是**錯覺**。

**遮蔽 3：plan task list 寫「Controller `_create_project()` 在 create 後 subscribe self user 為 follower」— 把焦點導到 create path，不導到 list path**

Plan `§2 修正範圍清單 > Controller` 只列：
- 必改 1：line 1038-1040 移 dispatcher guard
- 必改 2：`_create_project()` 加 `message_subscribe`

→ tester 把 T1+T2 focus 在「create 後 creator 看得到 / 非 creator 看不到」，完全沒寫 T3「Bob 列 Alice 的 project」這條 attack vector。整個 plan 的「controller 收尾」字面上就是「create path 收尾」，**漏掉 list path**。

### 結果：plan + test + impl 三方一致地漏

- **plan**：§1.C 4 條證據沒掃 controller layer / §2 task list 只列 create path
- **tester**：依 plan task 寫 T1+T2 focus create + visibility，沒寫 list endpoint cross-user
- **implementer**：依 plan task 改 D1+D2+D3，沒掃 `_list_projects` 是否要對等改
- **leader**：依 plan + test 14/14 綠認定 implementation 完成

直到 **playwright TC4 跨 user 跑 UI** 才把 list endpoint leak 暴露 — **TC4 是這次唯一沒被遮蔽住的 layer**。

## How to avoid

### 1. Plan 寫「後端已完整支援」時，必須把 controller layer audit 顯式列入

不夠：「`ir.rule` dual-leg 已 ready + ORM-level test 全綠」  
要寫：「`ir.rule` dual-leg 已 ready + ORM-level test 全綠 **+ controller 內所有 list/get endpoint 已驗證不用 sudo() 繞 ir.rule** + 已對等補 HttpCase endpoint test cover cross-user negative」

具體 plan template 補：

```markdown
### C. 後端已完整支援（**無需改動**）

| Layer | 證據 | 狀態 |
|-------|------|------|
| L1: ir.rule + ACL | `ir_rules.xml:65-78` dual-leg domain | ✅ ready |
| L1: ORM-layer test | `test_project_decouple.py:46-59` B1-B5 | ✅ green |
| **L2: Controller layer 不 sudo bypass** | `grep -rn "request.env\[.*\]\.sudo()\.search" src/controllers/` 對相關 model 全分類 | ⚠️ 必驗 |
| **L3: HttpCase endpoint test** | `test_*_endpoint.py` cover cross-user negative | ⚠️ 必補 |
```

### 2. Tester 寫新 endpoint feature 必對等補 list endpoint cross-user test

TDD red 階段，對任何「endpoint 收尾」class 的 PR，**必須涵蓋以下 attack vector**：

```python
def test_t_endpoint_list_blocks_cross_user_leak(self):
    """Bob bare-URL POST 不 leak Alice 的 record"""
    # Alice 建 N records（含 bound / unbound / mixed shape）
    # Bob authenticate
    # Bob bare-URL POST without filter
    # assertFalse(alice_record_ids & bob_returned_ids)
    # bonus: assertFalse(missing_from_alice)
```

這條 test 不靠 plan AC 提示 — 它是 [[orm-vs-endpoint-acl-test-coverage]] pattern 規範的**通用守線**。

### 3. CAQA / reviewer 對 controller layer 改動 PR 必跑 grep audit

每次 PR diff 觸及 `src/controllers/*.py`，audit checklist 必含：

```bash
grep -rn "request\.env\[.*\]\.sudo()\.search\|sudo()\.browse\|sudo()\.read" src/controllers/
```

→ 對 hit list 逐處分類為「safe (server-authoritative / platform-shared / channel-verified) / pending / blocker」。

本 repo cycle 6 baseline（CAQA 已做）：
- `sudo().search` 33 處：30 safe + 3 pending（_get_project_stages [F-DP-1] + api_support_stats [F-DP-2] + 已 close 的 _list_projects/_list_tasks）
- `sudo().browse` 22 處：100% 後 access check
- `sudo().read` 0 處

未來 PR 對應 grep 結果增量必須**有交代**（新增 sudo 路徑必說明為何安全）。

## Real-world chain（cycle 6 timeline）

| # | 階段 | 事件 | 是否抓到 leak |
|---|------|------|-------------|
| 1 | Plan | §1.C 寫「後端已完整支援」+ 4 條證據 | ❌ 漏掃 controller |
| 2 | Tester TDD red | T1+T2 focus create + visibility，B1-B5 backend baseline | ❌ 沒 list endpoint test |
| 3 | Impl | D1+D2+D3 + modal + support_service.js | ❌ 沒碰 _list_projects |
| 4 | Tester green check | 14/14 backend + Playwright TC1-TC3 全綠 | ❌ TC4 還沒跑 |
| 5 | Playwright TC4 | Bob 跨 user 看 `/woow#/projects` → 看到 Alice unbound | ✅ **第 1 次抓到** _list_projects leak |
| 6 | Impl (TC4 follow-on) | `_list_projects` drop sudo | (close instance #1) |
| 7 | CAQA round 1 grep | `sudo().search` 同 controller 抓到 _list_tasks 對稱 | ✅ **第 2 次抓到** BLOCKER A1 |
| 8 | Tester + Impl (A1 fix) | 補 T4 endpoint test + `_list_tasks` drop sudo | (close instance #2) |
| 9 | CAQA round 2 同源 scan 第三輪 | 撈出 `_get_project_stages` 雙重洞 + `api_support_stats` 漏 unbound leg | ✅ **第 3、4 次發現**（`api_support_stats` 後續帶進本 PR；`_get_project_stages` F-DP-1 follow-up）|
| 10 | User 拍板帶 F-DP-2 進本 PR | tester 補 T5 + impl 落 `:974-978` drop sudo + empty domain（變體 B 反向：under-access） | (close instance #3) |
| 11 | Implementation flag Task.sudo() 決策點 | F-DP-2 fix 後 `:980` 仍留 `Task.sudo()` for perf | ⚠️ 新 anti-pattern variant 浮現 |
| 12 | CAQA mini re-verdict 對等性驗證 | option (b) unify 推薦：drop Task.sudo() 結果集對等（task ir.rule 透過 project_id walk）| ✅ **第 5 次抓到** 同 method 半套狀態（變體 C）|
| 13 | Impl Task.sudo unify | `:980-986` drop sudo + comment 補述 unify 對等性 | (close instance #4) |

→ **5 個遮蔽 layer 才有 1 個 catch（TC4）**；剩 3 個 instance 是 CAQA grep 撈出來的。沒有 CAQA grep audit step，本 PR 會帶 _list_tasks BLOCKER 進 production；沒有 playwright TC4，本 PR 連 _list_projects 都會 leak 上線。F-DP-2 帶進 PR 後又揭露第 5 個變體（C：same-method 半套狀態）— 這條**沒有 test 抓得到**（因為 Task.sudo() 在 ACL 角度 safe），純粹是 CAQA option (b) 對等性 reasoning + pattern doc 立論一致性的 audit catch — 變體 C 唯一可靠的守線是「pattern doc 明寫 anti-pattern 變體 C + reviewer 對同 method 內 sudo() 做 boundary 一致性檢查」。

## Anti-pattern 三變體（cycle 6 抽出來的 family）

實際撞到的 controller layer sudo bypass 不只「漏 leg = leak」一種。Cycle 6 走完才抽出**三變體 family**，全部都是「controller 不信任 ir.rule，自己手搓 ACL/scope」的不同表現：

| 變體 | Symptom | Cycle 6 instance | 抓到的方式 |
|------|---------|------------------|-----------|
| **A: Over-access** | cross-tenant leak | `_list_projects` (instance #1) + `_list_tasks` (instance #2) | Playwright TC4 + CAQA grep audit |
| **B: Under-access** | creator 看不到自家 row（UX bug 非 leak） | `api_support_stats` accessible_projects bound-only filter (instance #3) | CAQA round 2 第三輪 sudo scan + tester T5 own-data assertion |
| **C: Same-method 半套狀態** | boundary 不清楚（ACL-safe 但 pattern 立論被稀釋）| `api_support_stats` Task.sudo() 殘留 (instance #4) | CAQA mini re-verdict 對等性驗證 + pattern doc 立論一致性 audit |

**變體 C 的特殊性**：它**不會 fail test**（accessible_projects.ids 已 ir.rule filter，後續 Task.sudo() 在 ACL 角度 safe），它也**不會 surface UX bug**（結果集對等），唯一的「症狀」是 pattern doc 立論被同 method 內違反例稀釋 → reviewer 對 sudo() 必須逐條評估 → cognitive load 增加 → pattern lint future-proof 失效。

**Implementer perf 直覺的反思**（cycle 6 經驗）：F-DP-2 fix 之後 implementation 思維是「accessible_projects 已 ir.rule filter，Task.sudo() 在 ACL 角度 safe，留下省 ir.rule re-eval 的 perf」。**這個推理是對的，但仍應 unify**，因為：
1. `search_count` ir.rule re-eval 對 small ID list 是 microseconds 級 — perf 收益 marginal
2. **sudo() 是 security primitive，不是 perf primitive** — 誤用會讓 future reader 把 sudo() 當 perf cache idiom
3. **真有 perf 需求走 prefetch / batch / context flags**，不是 sudo()
4. 同 method 留半套狀態 = future reviewer 必須逐條評估每個 sudo() — pattern 立論 integrity 比 marginal perf gain 重要

**這條 anti-pattern 守線給未來的人**：寫 controller endpoint 時，若 method 內已 drop 一個 sudo() 走 ir.rule，**剩餘的 sudo() 不論 ACL-safe 與否都要 default-unify**；要保留必須在 comment 明寫「為何這條 sudo() 例外」+ 引用具體 perf measurement 或 ORM constraint 證據；否則一律 drop。

## Why this is hard to see

- **Backend ORM test 是「正確的 layer 守 ir.rule」**：B1-B5 沒做錯，它們守得對的層級
- **Plan 寫「後端已完整支援」字面正確**：model + rule + ORM 確實已 ready，但 plan 作者把「ORM 已 ready」推論成「整條 stack 已 ready」
- **Frontend playwright 是 UI behavior 不是 endpoint security**：TC1-TC3 都是「same user 操作 modal」，TC4 是 plan 內唯一 cross-user UI test — 唯一掃到 leak 的 layer
- **Cross-user leak 的攻擊面是 bare-URL POST**，不會在 same-user UI testing 出現

→ 必須**明確設計** L2/L3 守線（controller grep + HttpCase cross-user test），不靠 ORM test + UI test 自動覆蓋。

## Related

- [[orm-vs-endpoint-acl-test-coverage]] — 本 pitfall 的 prescription pattern（如何不再撞）
- [[cross-workspace-task-data-leak]] — B1 incident（ir.rule layer 缺）；本 pitfall 是同 family 第 2-4 instance 在 controller layer 的表現
- [[paas-workspace-member-acl-tightening]] §Controller filter 不夠 — 同 spirit 但只講「攻擊者用 ORM 繞 controller」方向；本 pitfall 是反方向「controller 自己 sudo 繞 ir.rule」
- [[legacy-endpoint-scope-blindness]] — 類似機制（plan AC 只 cover 新 endpoint，legacy same-source path 漏裝 helper）
- [[two-layer-acl-asymmetric-deny]] — 類似機制（一層收緊另一層 silent 漏，partial fix 比沒 fix 更糟）
- [[tag-scoped-test-misses-full-u-failures]] — 同類「test 設計 vs 真實隔離」斷層
- `docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md` §同源 sudo bypass scan — 33 處逐處分類，本 pitfall 的 audit baseline
- `docs/tracking/test-reports/2026-05-28-decouple-project-frontend-endpoint.md` — T1-T4 endpoint test 設計（cycle 6 補 L3 守線）
