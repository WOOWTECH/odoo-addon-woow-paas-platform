# Woow PaaS Platform

多租戶 PaaS 平台（Odoo 18 addon）：租戶透過 marketplace 部署容器化應用到 Kubernetes。

## Language

（glossary 待 /domain-modeling session 逐步填入；格式——**術語**：一至兩句定義它「是什麼」，_Avoid_: 列棄用同義詞。只收本專案特有概念，通用工程詞不收。#411 案術語穩定後自此晉升。）

**Odoo group role**：Odoo 框架層的**全站**身分，一個帳號一組，決定它在整個 Odoo 實例裡是什麼。與本專案相關的有三個——`base.group_portal`（外部使用者，`user.share = True`）、`base.group_user`（內部使用者）、`base.group_system`（系統管理員／維運）。`ir.rule` 的 `groups` 欄位與 `ir.model.access.csv` 的 `group_id:id` 欄位綁的都是這一層。
_Avoid_: 「使用者權限」「Odoo 角色」「群組角色」

> 🔴 **`/web/signup` 註冊出來的帳號是 `base.group_portal`。** 這條在 Odoo core 是強制的，不是設定：`auth_signup` 為 `auto_install: True` 且 depends 含 `web`／`mail`（本 addon 依賴兩者）⇒ 必被安裝；其 data 檔把 `auth_signup.invitation_scope` 設為 `b2c`；`_signup_create_user` 走 `_create_user_from_template` 複製 `base.template_portal_user_id`，而該 template user 的 `groups_id` 只有 `base.group_portal`；`res.users.groups_id` 無 `copy=False`、`copy_data` 也不動它 ⇒ 群組隨 copy 帶走。
>
> 本 repo 有**可執行的斷言**在守這件事：`src/tests/test_acl_p9.py` 的 `assertNotIn(base.group_user, groups, "…would grant every new /web/signup user internal access")`。改動 signup 相關設定前先跑它。
>
> ⚠ **而本 addon 的 security 層完全沒有 portal 的位置**：`ir.model.access.csv` 與 `ir_rules.xml` 的每一列／每一條都只授權給 `base.group_user` 或 `base.group_system`，**`base.group_portal` 零命中**。兩者合起來的意思是——**租戶身分無法直接用 ORM 存取本 addon 的任何 model，前台的寫入是由 controller 以 `.sudo()` 代為執行的**。`src/controllers/ha_paas.py` 有一段註解逐字記載這個事實與它的因應。
>
> ⇒ 由此推論安全性時要小心：**「我們加了 record rule」不等於「租戶受到該規則約束」**——規則綁在 `group_user` 上，而租戶不在那個群組裡。反過來，任何**確實持有 `base.group_user`** 的帳號（內部員工、維運、被手動升級者）才是那些規則的實際受眾。

> ⚠ **未解矛盾（勿當已裁決）**：repo 內對「典型租戶是哪種身分」有相反敘述——`src/models/subscription_lifecycle_mixin.py` 把典型租戶描述為 internal user，`docs/guides/deployment/admin-roles.md` 則說 portal user 有完整功能（後者與 `/api/workspaces` 的非 sudo 實作衝突）。**正式環境租戶帳號實際落在哪個群組，尚未有人查過 `res_groups_users_rel` 定案。** 在那之前，任何以「租戶是 X」為前提的設計都要把前提寫出來。

**Workspace role**：租戶在**單一 workspace 內**的角色，一個 workspace 成員一筆，存於 `woow_paas_platform.workspace_access` 的 `role` 欄位（`required=True`、`default='user'`）。階層由低到高 `guest < user < admin < owner`（見 `src/models/workspace_access.py` 的 `ROLE_HIERARCHY`）。**同一個人在不同 workspace 可以是不同 role**；每個 workspace 恰有一個 `owner`（由 model 層 `_check_owner_count` 強制），轉移要走 `transfer_ownership`。
_Avoid_: 「群組」「權限等級」「帳號角色」

> ⚠ **`ASSIGNABLE_ROLES`（只含 admin／user／guest，用來擋「指派 owner」）的強制點只在 controller 層**（`src/controllers/paas.py` 兩處），model 層沒有對應守衛。所以「owner 不可被指派」這句話只在**走 BFF 的路徑上**成立，不是 model 不變式——描述授權時要標明是哪一層擋的。

> ⚠ **兩者撞名，而且會撞在同一個檔案裡。** `ROLE_USER = 'user'`（Workspace role 的第二低階）與 `base.group_user`（Odoo group role）只是碰巧同字，**語意完全無關、也不互相蘊含**：一個 Workspace role 是 `owner` 的人，Odoo group role 可能是 `base.group_portal`——兩軸各自獨立。
>
> 在 `src/security/ir_rules.xml` 裡兩者並排出現——`<field name="groups" eval="[(4, ref('base.group_user'))]"/>` 是**軸一**（這條規則套用在誰身上），`domain_force` 裡的 `role` 是**軸二**（滿足什麼條件才放行）。讀規則時先分清楚哪個欄位屬哪一軸，否則會把「已經綁了 group_user」誤讀成「已經驗過角色」。
>
> 要在 `domain` 驗軸二，必須用 `any` operator 把 `user_id` 與 `role` 綁在**同一列** access——否則 `role='user'` 的成員會靠 owner 那一列的 `role` 值 cross-tuple 矇混通過。現成正確寫法見 `ir_rules.xml` 的 `security_access_rule_owner_admin_manage`。
