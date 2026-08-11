# #556 workspace_access 授權重構 landing

## 根因（三層，缺一都補不齊）

`woow_paas_platform.workspace_access` 是 workspace 成員名冊 + 角色表，過去只有 ir.rule
一層守，而那一層對本 model 是**不夠的**：

1. **ACL `unlink=0` 且沒有第二列**（`ir.model.access.csv`）——連合法 owner 都不能刪除
   任何成員列。`_remove_member`（`paas.py`）因此對任何非 superuser 呼叫者一律拋框架層
   `AccessError`，是一個既有功能性 bug，不只是「清不掉入侵者」。
2. **`ir.rule` 對 create/write 方向自我滿足**——
   - `workspace_access_rule_create` 是 `[(1,'=',1)]` 恆真＋`perm_create=True`：任何
     `base.group_user` 都能對任意 workspace 建任意 role 的 access 列（含 `role='admin'`）。
   - `workspace_access_rule_member_access` 的 `perm_write=True` 沒有任何 role 條件，只要求
     「是該 ws 成員」——任何成員可 write 該 ws **任何一列**（含把自己升級）。
3. **model 層完全沒有 `create()`/`write()`/`unlink()` 守衛**——`_check_owner_count` 是唯一的
   `@api.constrains`，只在新值是 `owner` 且已有另一個 owner 時擋，對 `role='admin'` 完全
   不設限；也沒有任何欄位擋掉「非 owner 呼叫者改/刪 owner 那列」。

第 2 點單靠 ir.rule 補不完：Odoo 的 record rule **create** 檢查發生在 **INSERT 之後**
（`odoo/models.py` 的 `_create` 尾端 `records.check_access('create')`），而
`workspace_id.access_ids` 的 `'any'` 子句是**現讀**——攻擊者裸 ORM
`create({'workspace_id': victim, 'user_id': me, 'role': 'admin'})` 時，EXISTS 子條件被
**自己剛插入的那一列**滿足，rule 照樣放行。純 ir.rule 重構（下方「方案 A」）對這個
payload 完全無效，只對 `role='user'` 或省略 role 的**弱 payload**有效——用弱 payload 驗收
會得到假綠。

## 修法：方案 B（rule 收緊 + model create/write/unlink guard），非單靠 ir.rule

### ir.rule：退役 2 條、新增 4 條

退役 `workspace_access_rule_member_access` / `workspace_access_rule_create`（原本住在
`<data noupdate="1">` 區塊）。新增 4 條（`noupdate="0"`，緊接 `project_access` 的四條
之後，1:1 對照該樣板）：

| xmlid | R/W/C/U | domain 要點 |
|---|---|---|
| `workspace_access_rule_member_read` | T/F/F/F | 成員可讀該 ws 全部 access 列 |
| `workspace_access_rule_owner_admin_manage` | T/T/T/T | 呼叫者自己那列 `role in [owner,admin]` 時（`'any'` 綁同一列） |
| `workspace_access_rule_self` | T/F/F/F | 自己那列（`member_read` 的今日子集，刻意保留見 XML 註解） |
| `workspace_access_rule_admin_full` | T/T/T/T | `base.group_system` bypass |

`'any'` 是關鍵：把 `user_id` 與 `role` 綁在**同一列** access row。若寫成
`[('...access_ids.user_id','=',user.id), ('...access_ids.role','in',[...])]`（無 `'any'`），
Odoo 的 O2M cross-tuple 語意會讓攻擊者用「別人那列的 role」＋「自己那列的 user_id」湊出
通過。

### model guard（`src/models/workspace_access.py`）——create 方向的必要條件，不是縱深

- `ASSIGNABLE_ROLES_BY_ROLE`：grantor 既有角色 → 可授予的目標角色。
- `create()`：對每個 vals 算 `effective_role = vals.get('role') or ROLE_USER`，過
  `_check_assignable_role`。**不可早退**（見下方偏離#2）。
- `write()`：改 `role` 時同樣過 `_check_assignable_role`；**任何**對 `role=='owner'` 列的
  write（不限於改 role 欄位）都先過 `_check_owner_row_protected`。
- `unlink()`：owner 列先過 `_check_owner_row_protected`，再擋「移除最後一個 owner」
  （抄 `project_access.unlink()`）。兩個檢查順序刻意：授權判定（`AccessError`）先於不變式
  判定（`ValidationError`），讓兩種拒絕在測試裡可分辨。
- `_check_owner_count` 的 `search_count` 改 `.sudo()`——constraint 不該被 ir.rule 過濾（此前
  純屬僥倖：能 create 的人本來就看得到同 ws 全部列，rule 收緊後這個僥倖不再成立）。

### 三處刻意偏離 `project_access` 樣板（審查 / 未來維護者不要「修正」回去）

1. **`ASSIGNABLE_ROLES_BY_ROLE[admin]` 含 `'admin'`**（`project_access` 是
   `[user, guest]`，admin 不能授 admin）。workspace 側 `_invite_member` 一直允許
   「admin 邀 admin」，照抄 project_access 的表會直接打壞現行產品行為。安全上無損：
   授予者自己已是 admin，授予 admin 不是提權；owner 仍只有 owner 能授。
2. **role 缺省時不可 early-return**（`project_access.py:134` 是
   `if not target_role or not project_id: return`）。workspace_access 版用
   `vals.get('role') or ROLE_USER`，讓「省略 role」與「role='admin'」兩種 payload
   走同一道守衛，不依賴 Selection default 的巧合覆蓋。
3. **加 owner 列保護**（`_check_owner_row_protected`，project_access 沒有）：非 owner
   呼叫者不得 write/unlink 一列 `role=='owner'` 的記錄，`unlink()` 也不得移除最後一個
   owner。project_access 目前有同型潛在洞（無此保護），**刻意不在本 PR 順手修**，記
   follow-up。

### migration：DELETE 退役、新規則一律新 xmlid（`18.0.1.0.122`）

`ir_model_data.noupdate=true` 一旦成立：

- `-u` 時 `_load_records` 完全不重寫該 record 的欄位（原地改 domain 是永久 no-op）；
- `_build_update_xmlids_query` 的 `ON CONFLICT DO UPDATE` 不含 `noupdate` 欄，把
  `<record>` 從 `noupdate="1"` 搬到 `noupdate="0"` 也翻不了旗標；
- 從 XML 刪掉 `<record>` 也不會自動清除既有 rule 列（`_process_end` 只清
  `COALESCE(noupdate,false) != true` 的）。

三條合起來：不下 migration，舊的 2 條脆弱 rule 在既有安裝上永久存活、與新 4 條以 OR
語意併存，`-u` 之後修復靜默失效（`-i` fresh install 卻是全綠——典型假綠）。因此：

- **用 DELETE（`post-migrate.py`），不用 `active=False`**——本次是 2→4 條的語意重組，
  沒有 1:1 domain 可原地改；`active=False` 若要走 `<function>` 停用，兩個 `<record>`
  仍得留在 XML 才能讓 `ref()` 在 fresh install 找得到，等於「宣告一條規則再把它關掉」。
- **新規則一律用新 xmlid，不重用舊名**——重用舊名有致命順序風險：XML 載入時撞到既存
  `noupdate=true` 的同名 xmlid 而 skip（不更新）→ migration 接著把它 DELETE 掉 →
  該 rule 整條消失且不會被重建（ir.rule 對某 model+operation 完全沒有規則時等於
  **不設限**，比「規則太鬆」更糟）。
- migration 目錄 `18.0.1.0.122` **必須配** `src/__manifest__.py` 版號同步 bump——只建
  目錄不 bump manifest＝永不執行且無任何錯誤訊息（`odoo/modules/migration.py::compare`
  的執行條件是 `installed_version < 目錄版號 <= manifest 版號`）。
- **版號視窗陷阱的第二個實例（rebase 時親身撞到）**：本 PR 開發期間 `develop` 併發
  前進到 `18.0.1.0.121`（純 manifest bump，無對應 migration）。若沿用原提的 `119`，
  `installed_version=121` 對 `目錄版號=119` 是 `121 < 119` 為假 ⇒ migration **不會
  執行**，舊脆弱 rule 在已裝到 121 的環境上永久不會被退役——與「只建目錄不 bump
  manifest」同一種失效形狀，只是這次是 manifest **被別人併發 bump 超過**，不是自己
  漏 bump。rebase 時把 manifest 與 migration 目錄**一起**改成 `122`（嚴格大於
  develop 當下的 121）解決；已用 121-base 版號視窗實測驗證（見下方「write-pivot」段
  的驗收記錄）。

### `workspace.py:81` 的 sudo bootstrap（陷阱2：雞生蛋）

Plan B 收緊 create 方向後，一個全新使用者對自己那個**全新** workspace 完全沒有任何
既有 access 列（`grantor=None`）——model guard 會把自己的 owner-bootstrap 一起擋死。
修法：`workspace.py:81` 的 `workspace_access.create(...)` 改 `.sudo()`，owner 繼續從
`workspace.owner_id`（server 側已決定的欄位）取，**不要**照抄 `project_access.py` 的
`env.uid == SUPERUSER_ID` 分支——HA PaaS 自動開通（`ha_paas.py`）走 public user、
`env.uid` 從不等於 `SUPERUSER_ID`，照抄會讓那條路徑漏建 owner 列（該路徑本身呼叫
`Workspace.sudo().create(...)`，外層 sudo 已讓內層 `self.env.su=True`，不受此問題影響，
但若照抄 uid 分支去判斷，反而會誤判走錯分支）。

### write-pivot：identity anchor guard（獨立驗收發現，補在同一 PR）

實作完成後的獨立對抗式驗收（verify556，opus，全新 context）另外找到 A1 的第二條
payload——analysis.md 與最初的攻擊向清單都沒列到：任何 `base.group_user` 都能先合法
自建一個 workspace（P6 設計行為，她在那裡是 owner），再把自己那列 access row 用
`write([row_id], {'workspace_id': 受害workspace, 'role': 'admin'})` **搬進**受害
workspace。兩層都沒守到：

1. **ir.rule 層**：Odoo 的 `write()` 只在寫入**前**查 rule，寫完不重查——「從我是
   owner/admin 的來源 ws 搬一列到受害 ws」通過的是**來源**狀態的
   `owner_admin_manage`（對照：`create` 是 INSERT **後**才查，才有 #556 原本那個洞；
   pivot 是同一族問題在 write 方向的鏡像）。
2. **model guard 層**：`write()` 完全沒看 `vals` 裡的 `workspace_id`——
   `_check_assignable_role(vals['role'], rec.workspace_id.id)` 用的是**舊**（來源）
   workspace，角色檢查整個問錯了工作區；`user_id` 同理沒人看。

**修法（approach b，identity anchor）**：`workspace_id`／`user_id` 是建立後不可變的
anchor——非 superuser 且非 `group_system` 的呼叫者，若 `vals` 要把這兩個欄位改成不同
值，一律 `AccessError`（沿用既有 guard 的 su/group_system 豁免，與 B1/B2 一致）。理由：
沒有任何合法租戶流程需要重指派一列 access row 屬於哪個 workspace／哪個 user（invite／
改角色／移除都只動 `role`）。屬 [[scope-flip-write-guard-not-ir-rule]] 的射程——
`workspace_id` 就是 tenancy scope 欄位。

比較用「值是否真的改變」而非「key 是否在 vals 裡」（同一 pattern 的既有教訓）：no-op
送出原值＋同時改 `role` 的合法流程（陽性對照鎖住）不受影響。

`TestWorkspaceAccessWritePivotGuard`（`test_workspace_access_authz_556.py`）是永久回歸
測試，改寫自 verify556 的兩份探針（`/tmp/woow-556/pivot-probe.py` 攻擊向、
`/tmp/woow-556/anchor-probe.py` 繞道與過度修正對照），涵蓋：一步 pivot（改
`workspace_id`+`role`）、第三方植入的 pivot 版（只改 `workspace_id`）、owner 改別人
列的 `user_id`、呼叫者改自己列的 `user_id`、`copy()` 繞道（底層走 `create()`，驗證
真的沒有另一條路）、workspace 的 one2many `access_ids` inverse command（`(1, id,
vals)` 與 `(4, id)`）繞道——**其中 `(4, id)` 那條配了獨立的 sudo 陽性對照**（若拿掉
guard，同樣的命令形態在 sudo 下必須真的把列搬過去；沒有這條對照，「沒位移」有可能只
是命令形態本身無效，不能當作守住的證據），以及三條陽性對照（no-op anchor 值＋合法改
role、真實端點 `update_role`、sudo 路徑）。本機用 docker harness 以 `git stash` 暫時
拿掉這段 guard 重跑：3 條攻擊測試如預期紅（`{'result': True}`，攻擊真的成功），2 條
控制測試維持綠（非 fixture 壞掉）；補回 guard 後全綠、既有回歸零受影響。

**manifest／migration 從 118 一路提到 `18.0.1.0.122`，不是 `119`**：write-pivot 修法
本身是純 model guard（`write()` 內的 Python 判斷），不涉及 ir.rule／ACL／data XML，
不需要自己的 migration——這件事沒變。但本 PR 開發期間 `develop` 併發前進到
`18.0.1.0.121`（另一批 commit 連續 bump manifest、無對應 migration，純為過 CI 的
manifest-version-gate）。rebase 回 `origin/develop` 時撞到兩個問題：(a) `src/__manifest__.py`
的版號欄位衝突（唯一衝突檔，安全碼零衝突）；(b) 更嚴重的——沿用原提的 `119` 會落入
migration 版號視窗陷阱本身：`installed_version=121` 對 `目錄版號=119` 是
`121 < 119` 為假，migration **不會執行**，舊脆弱 rule 在已裝到 121 的環境上永久不會
退役，修復靜默失效（與陷阱 1 同一種失效形狀，這次是版號被**別人併發超前**而非自己
漏 bump）。解法：manifest 與 migration 目錄一起改成 `122`（嚴格大於 121），整個 PR
（含 write-pivot 補丁）以同一個模組版號原子落地。已用 docker harness 的 `-i` fresh
install、`-u` 升級（118-base），以及 rebase 後新增的**121-base 版號視窗實測**（以
`origin/develop`@121 `-i` 安裝、再用 rebased branch `-u` 升級，確認 log 印出
`Running migration [18.0.1.0.122` 且該行之後舊兩條 rule 被退役、四條新 rule 恰好
存在、A1 攻擊仍被擋）三條路徑都驗過 guard 在。

**已知非問題（審查時不必深究）**：guard 用 `vals['workspace_id'] != rec.workspace_id.id`
做 int 比較。真實 Odoo client（`/web/dataset/call_kw` 與 OWL 前端）一律送 Many2one
的整數 id，已實測驗證；若有人手寫 raw HTTP 送字串型 id（非 Odoo 官方 client 行為），
`'3' != 3` 恆真、guard 一律視為「有改變」而擋下（保守方向，不會漏擋，只是理論上可能誤
擋一個沒人會送的畸形 payload）。

## 與 #519 的關係

修復後 `role` 對非成員 / 一般 `user`/`guest` 成員都不可寫（row-level 靠 ir.rule 收窄、
create-level 靠 model guard 補齊 ir.rule 在 create 方向的自我滿足漏洞）。#519 若要把
`cloud_service` 寫入權綁在 `role ∈ {owner, admin}`，這個前提現在成立。**但前提依賴
`workspace_access` 的 write/create/unlink 方向各至少存在一條 rule**——若未來有人以
「簡化規則」為由刪掉 manage rule，`perm_write` 方向會退回「無規則＝不設限」，#519 的
守線會同時失效且無聲。這條存在性守衛建議之後補進 `scripts/audit/repo-lint.py`（本次
未做，見下方 Not in scope）。

## 測試（`src/tests/test_workspace_access_authz_556.py`，HttpCase，group_user-only fixture）

全部走真 HTTP RPC：ORM 層攻擊（A1/A1'/A2/A3/A4）走 `/web/dataset/call_kw`；controller
層（A5/P1-P4/P6-P7）走 `/api/workspaces/...`；P8 走 `/api/ha-paas/provision`。fixture user
一律只有 `base.group_user`（P7/既有 525 測試沿用 `admin` 是唯二例外，測的正是
`group_system` bypass 通道本身）。

- **A1**（核心鑑別測試，`role='admin'`）／**A1'**（弱 payload 對照 `role='user'` 與省略）／
  **A2**（既有 user 成員自我升級）／**A3**（第三方植入，含 uid 1）／**A4**（admin 降級
  owner→自我升 owner 的 write 與 unlink 兩個方向）——皆須 `AccessError`。
- **A5**：`_remove_member` 修復後由「必拋框架層 AccessError」變成「owner 呼叫成功、列真的消失」。
- **P1-P8**：owner 邀請 user/admin、admin 邀 admin（鎖偏離#1）、owner 改角色/移除非
  owner 成員、user 成員仍可 list members / 讀 cloud_service、任一 group_user 建
  workspace 恰產生一列 owner access（鎖 sudo bootstrap）、`admin`/`group_system` 對
  workspace_access 四向 CRUD 全通（鎖 ACL admin 列 + `admin_full` rule）、HA PaaS
  provision（sudo 路徑）仍能開通並建出 owner 列。
- **`TestWorkspaceAccessWritePivotGuard`**：write-pivot identity anchor guard 的永久
  回歸測試，見上方「write-pivot」段。

同步更新 `test_invite_member_rollback_525.py` 檔頭與 `_invite_member`（`paas.py`）內的
兩處註解——它們原本寫「`check_access('create')` 的 AccessError 發生在 INSERT 之前」與
「`workspace_access_rule_create` 目前對 base.group_user 全開」，修復後兩句都是假話（前者
本來就不精確——ir.rule 的 create 檢查一律在 INSERT 之後；後者的 rule 已被本次退役），已
改寫為準確敘述（`feedback_comment_env_state_claims_expire` 家族）。

## Not in scope（另開 follow-up，不在本 PR 順手改）

- **`project_access` 有一模一樣形狀的 write-pivot 洞**（`project_access.py` 的
  `write()` 同樣只在 `'role' in vals` 時查、且查的是 `rec.project_id.id`）——與 #556
  修復前的 `workspace_access` 同一族問題，本 PR 刻意**不動 `project_access`**（避免擴大
  本次改動的爆炸半徑），記 follow-up；連同 analysis.md §4 原本已列的兩個
  `project_access` 潛在洞（`role` 缺省 early-return、無 owner 列保護）一起處理。
- ~~`workspace_rule_member_access`（`ir_rules.xml`，同一個 noupdate=1 區塊）
  `perm_write=True` 且 domain 無 role 條件——任何 `guest`/`user` 成員都能改 workspace
  本體的 name/description/state（含 archive）。#556 的相鄰問題，不是 #556。~~
  **已由 #658 修復**（migration `18.0.1.0.127`）。⚠ 當時這行低估了範圍：實際可寫的
  還有 `owner_id`（訂閱帳單 partner ＋ `max_per_partner` 配額的錨，也是
  `_delete_workspace` 的授權判準）與 `slug`（K8s namespace／subdomain 的唯一輸入），
  以及 `access_ids` / `service_ids` 兩個 o2m 反向通道。完整形狀見
  `src/tests/test_workspace_authz_658.py` 的檔頭。
- `invited_by_id` 無 `readonly`，攻擊者可偽造稽核欄位（`readonly` 不阻擋 ORM/RPC 寫入，
  真解要在 `create()` 內對非 superuser 強制覆寫，低嚴重度）。
- `scripts/audit/repo-lint.py` 的機械 gate（noupdate 覆蓋 `ir_rules.xml`、退役 xmlid
  黑名單、write/create/unlink 方向存在性守衛）——留 Phase 3b，等核心修復先驗過。
- **`transfer_ownership`（死碼）若未來被喚醒，會被新 anchor guard 打壞**：它連續兩次
  `self.write({'role': ROLE_ADMIN})` / `new_owner_access.write({'role': ROLE_OWNER})`，
  第二次呼叫時 grantor（呼叫者自己那列）已經因第一次 write 落地成 'admin'，
  `ASSIGNABLE_ROLES_BY_ROLE['admin']` 不含 `'owner'` ⇒ 會被 `_check_assignable_role`
  擋下（非 anchor guard，是既有的 B1 角色檢查）。今天無人呼叫，不影響本次修復；若日後
  要復活這條方法，必須改走 sudo 或重新設計成單一 atomic 呼叫。

## 相關 pattern

[[scope-flip-write-guard-not-ir-rule]]（`role` 是「有合法改值路徑、只是要 gated」，
`env.su`+`group_system` 雙閘放行正確，不是 su-env-exempt-guard 的 sentinel 場景）、
[[su-env-exempt-guard-false-green]]（守衛的 deny 分支在 `TransactionCase` 下結構上不可達，
本檔測試因此全走 HttpCase）、[[two-layer-acl-lockstep]] / [[orm-vs-endpoint-acl-test-coverage]]
（三個 controller 端點已在 controller 層做 `required_role=ROLE_ADMIN` 檢查，本次只是把
同一判準複製到 ORM 層，兩層對齊，controller 本身不需改動）、
[[paas-workspace-member-acl-tightening]]（`access_ids.user_id` 是 SSOT，不走
`owner_id`/`message_partner_ids`）。
