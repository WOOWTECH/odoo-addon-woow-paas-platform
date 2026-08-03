# Woow PaaS Platform

多租戶 PaaS 平台（Odoo 18 addon）：租戶透過 marketplace 部署容器化應用到 Kubernetes。

## Language

（glossary 待 /domain-modeling session 逐步填入；格式——**術語**：一至兩句定義它「是什麼」，_Avoid_: 列棄用同義詞。只收本專案特有概念，通用工程詞不收。#411 案術語穩定後自此晉升。）

**Odoo group role**：Odoo 框架層的**全站**身分，一個帳號一組，決定它在整個 Odoo 實例裡是什麼。本專案只用到兩個——`base.group_user`（內部使用者，**所有租戶都是這個**）與 `base.group_system`（系統管理員／維運）。`ir.rule` 的 `groups` 欄位綁的就是這一層。
_Avoid_: 「使用者權限」「Odoo 角色」「群組角色」

**Workspace role**：租戶在**單一 workspace 內**的角色，一個 workspace 成員一筆，存於 `woow_paas_platform.workspace_access` 的 `role` 欄位（`required=True`、`default='user'`）。階層由低到高 `guest < user < admin < owner`（見 `src/models/workspace_access.py` 的 `ROLE_HIERARCHY`）。**同一個人在不同 workspace 可以是不同 role**；每個 workspace 恰有一個 `owner`（由 model 層 `_check_owner_count` 強制），轉移要走 `transfer_ownership`。
_Avoid_: 「群組」「權限等級」「帳號角色」

> ⚠ **`ASSIGNABLE_ROLES`（只含 admin／user／guest，用來擋「指派 owner」）的強制點只在 controller 層**（`src/controllers/paas.py` 兩處），model 層沒有對應守衛。所以「owner 不可被指派」這句話只在**走 BFF 的路徑上**成立，不是 model 不變式——描述授權時要標明是哪一層擋的。

> ⚠ **兩者撞名，而且會撞在同一個檔案裡。** `ROLE_USER = 'user'`（Workspace role 的第二低階）與 `base.group_user`（Odoo group role）只是碰巧同字，語意完全無關：一個 Workspace role 是 `owner` 的租戶，Odoo group role 仍然是 `base.group_user`。
>
> 在 `src/security/ir_rules.xml` 裡兩者並排出現——`<field name="groups" eval="[(4, ref('base.group_user'))]"/>` 是**軸一**（這條規則套用在誰身上），`domain_force` 裡的 `role` 是**軸二**（滿足什麼條件才放行）。讀規則時先分清楚哪個欄位屬哪一軸，否則會把「已經綁了 group_user」誤讀成「已經驗過角色」。
>
> 要在 `domain` 驗軸二，必須用 `any` operator 把 `user_id` 與 `role` 綁在**同一列** access——否則 `role='user'` 的成員會靠 owner 那一列的 `role` 值 cross-tuple 矇混通過。現成正確寫法見 `ir_rules.xml` 的 `security_access_rule_owner_admin_manage`。
