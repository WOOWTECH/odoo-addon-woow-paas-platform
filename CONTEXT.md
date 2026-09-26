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
> ⚠ **而本 addon 的 security 層完全沒有 portal 的位置**：`ir.model.access.csv` 與 `ir_rules.xml` 的每一列／每一條都只授權給 `base.group_user` 或 `base.group_system`，**`base.group_portal` 零命中**。意思是**租戶身分無法直接用 ORM 存取本 addon 的任何 model**。`src/controllers/ha_paas.py` 有一段註解逐字記載這個事實與它的因應。
>
> ⚠ **但「所以 controller 都以 `.sudo()` 代為執行」是錯的，別這樣接下去推**（2026-09-01 更正）。實況是**逐 controller 而異**：`ha_paas.py` 那條路確實全程 sudo，而 `/api/workspaces` 的 `_list_workspaces`／`_create_workspace`（`src/controllers/paas.py:290`／`:374`）用的是**裸 `request.env`**——於是那兩支對 `base.group_portal` 直接 AccessError。**描述任何 endpoint 的授權時，去讀那一支自己有沒有 sudo，不要套用通則。**
>
> ⇒ 由此推論安全性時要小心：**「我們加了 record rule」不等於「租戶受到該規則約束」**——規則綁在 `group_user` 上，而租戶不在那個群組裡。反過來，任何**確實持有 `base.group_user`** 的帳號（內部員工、維運、被手動升級者）才是那些規則的實際受眾。

> ✅ **已裁決（2026-09-01，prod 實測）**：**租戶的 Odoo group role 是 `base.group_portal`，而且只有它。** 量測方式＝對 prod（`woow-k3s` / `paas-system` / `paas-odoo-postgresql-0` / db `bitnami_odoo`，實裝版號 `18.0.1.0.176`）跑唯讀 SQL 讀 `res_groups_users_rel` 全量。結果：唯一的真實租戶帳號由 `base.public_user` 建立（＝`/web/signup` 自助註冊，unauthenticated request 的 `env.uid` 就是 public user，而 `sudo()` 不改 uid），群組只有 `base.group_portal`，且其 `res_users.write_date` 停在建立後 0.8 秒——`write()` 在欄位分派前就無條件 bump `write_date`（`odoo/models.py:4727-4730`，m2m-only 寫入亦然）⇒ **「先 internal 後被降級」已被推翻**。持有 `base.group_user` 的帳號全是基礎設施（root／admin／`base.default_user` 樣板／封存的 bot），**零個是租戶**。
>
> ⇒ 因此 `src/models/subscription_lifecycle_mixin.py:115` 的「典型租戶（只有 `base.group_user` 的內部使用者）」是**設計時的想像，不是實況**。它與 `src/tests/test_subscription_mixin_cascade_sudo_29.py:103`／`:381`／`:486` 的 fixture 同源。⚠ 這個身分前提不只在那一支：`src/tests/` 275 支裡有 89 支釘 `base.group_user`、只有 7 支用 `base.group_portal`——**改動這條敘述前先想清楚波及面**。
>
> ⚠ **仍開著的一格**：`docs/guides/deployment/admin-roles.md:13` 的「Portal user ✅ 完整功能」射程未定（是「看 subscription」還是「整個 `/woow`」）。這格要問當初寫的人，**不要據此推論**。

> ⚠ **`res_users.share` 不是第二個訊號。** 它是 stored computed，唯一依據就是 `groups_id` 裡有沒有 `base.group_user`（`odoo/addons/base/models/res_users.py:386` 欄位定義、`:531-534` 的 `_compute_share`）。**「`share=True`」與「非 `base.group_user`」是同一個事實的兩種讀法**，並列出現時不構成互相佐證——拿它們當兩把尺互證是零證據。

**Workspace**：租戶的資源容器，同時是該租戶在 K8s／Cloudflare 上的**身分錨點**。它擁有的東西掛在自己身上（`cloud_service`、`smart_home`、`security_access`、`agent_session`、`project_project`／`project_task`，以及成員的 `workspace_access`），恰有一個 `owner_id`（`required`、`ondelete='restrict'`），狀態只有 `active` 與 `archived` 兩種。model 見 `src/models/workspace.py:24`。
_Avoid_: 「專案」「租戶」（租戶是**人**，workspace 是**容器**，一個租戶可以有多個）「namespace」（那是 workspace 的投影，不是它本身）

> 🔴 **它有兩個識別碼，只有一個是伺服器權威的，而它們決定的東西不同。**
>
> - `ws_hash`——8 hex、`readonly`、伺服器產生、**從不由租戶輸入推導**。**K8s namespace（`paas-ws-{ws_hash}`）與公開子網域（`paas-cs-/sa-/sm-{ws_hash}-...`）吃的是它**（#677；見 `src/controllers/paas.py:3386`、`:3388`、`src/controllers/ha_paas.py:273-274`）。
> - `slug`——URL 友善識別碼，由 `name` 推導 ⇒ **租戶影響得到**。只用在 URL，**不參與任何基礎設施命名**。
>
> ⚠ 兩者都有 `UNIQUE` constraint，但角色不對稱：`unique_ws_hash` 是 #677 的**唯一**真正守線，不是 belt-and-braces。跨租戶碰撞檢查在 Python 層做不到——`_generate_slug()` 的 `search_count` 走呼叫者身分、受 `ir.rule` 限制，而攻擊者正好站在「看不見受害者」的位置上 ⇒ 那種檢查對他恆回 0、靜默放行。DB constraint 不看 `ir.rule` 也不怕 TOCTOU（見 `src/models/workspace.py:46-56` 的逐字說明）。**寫任何碰基礎設施命名的 code 前先確認你吃的是 `ws_hash` 不是 `slug`。**

> ⚠ **Workspace 不是計費單位。** 訂閱掛在**每一個服務**上（`subscription_id` 來自 `src/models/subscription_lifecycle_mixin.py`，由 CloudService／SmartHome／SecurityAccess 三個 model 各自持有），workspace 本體沒有 `subscription_id` 欄位。所以「這個 workspace 的訂閱狀態」不是一個有定義的問題——要問的是「這個 workspace 裡的哪個服務」。

> ⚠ **建立 workspace 有兩條路，而租戶只走得通其中一條**——見下方 **Tenant（租戶）** 的說明，那裡有 2026-09-01 的 prod 量測。

**Workspace role**：租戶在**單一 workspace 內**的角色，一個 workspace 成員一筆，存於 `woow_paas_platform.workspace_access` 的 `role` 欄位（`required=True`、`default='user'`）。階層由低到高 `guest < user < admin < owner`（見 `src/models/workspace_access.py` 的 `ROLE_HIERARCHY`）。**同一個人在不同 workspace 可以是不同 role**；每個 workspace 恰有一個 `owner`（由 model 層 `_check_owner_count` 強制），轉移要走 `transfer_ownership`。
_Avoid_: 「群組」「權限等級」「帳號角色」

> ⚠ **`ASSIGNABLE_ROLES` 與 `ASSIGNABLE_ROLES_BY_ROLE` 是兩個名字很像、分居兩層、職責不同的常數——別把前者讀成「model 層沒有守衛」。**（2026-09-05 更正，#1052：本段原本寫「強制點只在 controller 層，model 層沒有對應守衛」，那是過度的敘述。）
>
> - **model 層有兩道守衛**（`src/models/workspace_access.py`）：`_check_assignable_role`（grantor-aware，查表 `ASSIGNABLE_ROLES_BY_ROLE`，接在 `create()`／`write()` 上——非 owner 授不出 owner、`user`／`guest` 授不出任何角色）與 `@api.constrains` 的 `_check_owner_count`（同一 workspace 第二個 owner 直接 `ValidationError`）。⚠ 前者對 `env.is_superuser()`（含任何 `.sudo()` recordset）豁免、後者不豁免；所以「一個 workspace 恰一個 owner」對 sudo 路徑也成立，「誰能授誰」對 sudo 路徑不成立。
> - **controller 層另有一把 `ASSIGNABLE_ROLES`**（只含 admin／user／guest），只被 `src/controllers/paas.py` 的 `_invite_member`／`_update_member_role` 讀，用途是 **payload-shape 驗證**：把「指派 owner」在進 ORM 之前就擋成乾淨的錯誤，逼它走 `transfer_ownership`（demote-then-promote，走 sentinel 開閘）。它擋的形狀 model 層也擋得住（會撞 `_check_owner_count`），差別只在錯誤形態。
> - **真正的洞不是「第二個 owner」**（constraint 擋住），而是 **sudo 路徑對 `_check_assignable_role` 的豁免**：任何 `.sudo()` 的寫入都能替 `user`／`guest` 角色的呼叫者授出 `admin`。描述授權時仍要標明是哪一層擋的、以及那一層對 su 是否生效。

> ⚠ **兩者撞名，而且會撞在同一個檔案裡。** `ROLE_USER = 'user'`（Workspace role 的第二低階）與 `base.group_user`（Odoo group role）只是碰巧同字，**語意完全無關、也不互相蘊含**：一個 Workspace role 是 `owner` 的人，Odoo group role 可能是 `base.group_portal`——兩軸各自獨立。
>
> 在 `src/security/ir_rules.xml` 裡兩者並排出現——`<field name="groups" eval="[(4, ref('base.group_user'))]"/>` 是**軸一**（這條規則套用在誰身上），`domain_force` 裡的 `role` 是**軸二**（滿足什麼條件才放行）。讀規則時先分清楚哪個欄位屬哪一軸，否則會把「已經綁了 group_user」誤讀成「已經驗過角色」。
>
> 要在 `domain` 驗軸二，必須用 `any` operator 把 `user_id` 與 `role` 綁在**同一列** access——否則 `role='user'` 的成員會靠 owner 那一列的 `role` 值 cross-tuple 矇混通過。現成正確寫法見 `ir_rules.xml` 的 `security_access_rule_owner_admin_manage`。

**Tenant（租戶）**：使用本平台的外部客戶身分。其 Odoo group role 恆為 `base.group_portal`（見上），Workspace role 則視 workspace 而定。**它不是「有 workspace 的人」的同義詞，也不是「註冊過的人」的同義詞**——三者在 prod 上是三個不同的集合。
_Avoid_: 「使用者」「客戶」「會員」「portal user」（最後一個指的是 Odoo group role 這一軸，不是這個角色）

> ⚠ **「租戶」先前從未被定義，這正是身分矛盾的成因。** 不同作者各指一件事：實作 `/woow` 前台的人指「會登入 `/woow` 用 workspace 的人」，寫 `subscription_lifecycle_mixin` 的人指「持有訂閱的內部帳號」，寫 `admin-roles.md` 的人指「`/web/signup` 註冊進來的外部人」。三個指涉在 code 裡碰不到頭，於是各自的敘述都「對」，合起來卻互相矛盾。**新增任何以「租戶」為主詞的敘述前，先指明你講的是哪一個集合。**

> 🔴 **成為 workspace owner 有兩條路，而租戶只走得通其中一條。** `src/controllers/paas.py:374`（`/api/workspaces` action=create）**不走 sudo**，而 `src/security/ir.model.access.csv:2`／`:3` 只授權 `base.group_user`／`base.group_system` ⇒ **`base.group_portal` 身分打不通這條**。另一條是 `src/controllers/ha_paas.py:268` 的 provision，走 `.sudo()` 且 `_guard_partner()`（`ha_paas.py:114-126`）只排除 public／root partner、**不檢查任何 Odoo group** ⇒ 租戶只要持有 `ha:provision` scope 的 token 就走得通。owner 的 access 列一律由 `src/models/workspace.py:219-223` 無條件 sudo 寫入、`user_id` 取自 `workspace.owner_id` 而非 `env.user`。
>
> 2026-09-01 prod 實測：唯一的 workspace（`name='HA PaaS'`）確實是第二條路建的——四個判準全中（名稱、`create_uid` ＝租戶自己、對應 `cloud_service` 的 `conceal_instances=t`、`ha:provision` token 早於 workspace 建立時間 19 分鐘）。**⇒ `/woow` 的 workspace 建立流程從未被任何真實租戶走過。** 談「租戶在前台能做什麼」時，這是必須先講出來的前提。

> ⚠ **稽核欄不能用來分辨走了哪條路。** Odoo 的 `.sudo()` 不改 `env.uid`（`odoo/models.py:6291`），而 `create_uid` 取的就是 `env.uid`（`odoo/models.py:5130`）⇒ 兩條路的 `create_uid` 都會是租戶本人。能分辨的是「租戶自己執行」vs「他人代建」，不是「有沒有 sudo」。

**Standalone Odoo 站**：直接部署在 woow-k3s 上、**不由平台 `cloud_service` 管理**的 Odoo 站，一站一個 namespace（例：`harumi-odoo`）。它不屬於任何 Workspace，也不是任何 Tenant 的服務；搬進平台後才成為某個 Workspace 裡的 odoo `cloud_service`。
_Avoid_: 「客戶站」（「客戶」是 Tenant 的棄用同義詞）、「tenant 站」「odoo-tenant 站」（會和 **Tenant** 撞名；`odoo-tenant` 只是部署它的 helm chart 名稱）

**平台 Odoo**：`paas-system` 裡的那一套 Odoo（`paas-odoo`），跑本 addon 與 `/woow`，也就是平台本身；#1032 之後同時承載公司 ERP 與官網。它和 Tenant 在 Workspace 裡部署的 odoo `cloud_service` 是兩回事，即使兩者可能共用同一個 chart 或 image。
_Avoid_: 「主站」「paas odoo」（容易與租戶的 odoo `cloud_service` 混淆）

**Odoo 管理員帳密**：odoo `cloud_service` 的 DB 裡那個管理員使用者（`base.user_admin`）的 login 與密碼，Tenant 用它登入自己的 Odoo。login 是建立服務時填的 email；密碼由平台產生，可由平台重設。
_Avoid_: 不加限定詞的「admin password」「管理員密碼」——這個詞在本專案有四個指涉（本條與下三條），曾因此把一條只對 admin GUI 密碼成立的重設流程誤當成對 Odoo 也成立

**master password**：Odoo 的資料庫管理密碼（`odoo.conf` 的 `admin_passwd`），管的是建立／備份／還原／刪除 DB，**不是登入密碼**。在 Tenant 的 odoo `cloud_service` 上由平台持有、不顯示給 Tenant；`list_db=False` 時 Odoo 沒有任何入口會收它。
_Avoid_: 「admin 密碼」「管理員密碼」

**admin GUI 密碼**：`has_admin_gui` 的 template（image 內建管理介面者）由平台供給的那一組 GUI 登入密碼；#357 的「重設管理員密碼」指的是它。它和上面兩條是三個不同的密鑰，供給與重設的通道也各不相同。

**DB superuser**：PostgreSQL 角色屬性 `rolsuper=true` 者，本專案的 odoo chart 內固定名為 `postgres`。管的是 `CREATE EXTENSION`／`CREATE DATABASE`／`CREATE ROLE` 這類叢集層操作；其密碼是與上面三條並列、但完全獨立的**第四把**密鑰，由平台持有，只給 chart 的 bootstrap initContainer、還原 Job 與人工還原 runbook 用，不給跑 Odoo 的那個 DB 角色（`odoo`，非 superuser）。
_Avoid_: 不加限定詞的「superuser」「管理員密碼」；也不要把 Odoo group role 的 `base.group_system`（Odoo 框架層的群組，不是 PostgreSQL 角色）叫成「系統管理員」而與本條混用。

**Odoo superuser**：Odoo ORM 層的 `SUPERUSER_ID`（值為 1），`sudo()` 切換到的身分，繞過所有 `ir.rule`／`ir.model.access` 檢查，**不是一個可登入的帳號**。與「Odoo 管理員帳密」（`base.user_admin`，一個真實可登入、屬於 `base.group_system` 的一般使用者）是兩個不同的 `res.users` 概念，不可互換；與「DB superuser」更是不同層（Odoo vs PostgreSQL）。
_Avoid_: 「admin」「系統管理員」——描述 `sudo()` 行為時講「Odoo superuser」，不要與「Odoo 管理員帳密」或「DB superuser」混稱。

**憑證來源**：服務上任一帶身分入口的憑證，按「由誰決定」分成兩類——**租戶自設**（Tenant 自己填進去的第三方系統帳密，如 SMTP 帳密、`auth.*ApiKey`；平台替他保管並在每次重新部署時原樣送回）與**平台鑄造**（部署時由平台產生，如 DB 密碼、master password、初始 Odoo 管理員密碼、admin GUI 密碼、n8n owner 密碼）。鑄造的再分**不可見**（DB 密碼、master password）與**可見可重設**（admin GUI 密碼、n8n owner 密碼）。判準與 seed 上必須留下的痕跡以 `docs/decisions/patterns/default-credential-functional-test-blindspot.md` 為權威，本條只供查找。
_Avoid_: 「租戶自填憑證」「平台產生密鑰」（同一條軸線另造的詞）、不加限定詞的「憑證」（分不出是哪一類）

**備份（一份備份）**：odoo `cloud_service` 在同一時刻產出、成對存放的三件東西——DB dump、filestore 快照、manifest；缺任何一件都不算一份備份。它住在該服務自己的 backup PVC，與資料本體不同 volume。
_Avoid_: 單獨的「dump」（只是 DB 那一半）；「快照」「Longhorn snapshot」（與資料同一顆 volume、同滅，是還原錨點不是備份）；reset 密碼流程裡 `config.json` 的暫存副本（程式碼裡也叫 backup，但與本條無關，改稱「暫存副本」）

**升級前備份**：會改 odoo image digest 的 helm upgrade 之前，由平台另外觸發、獨立保留的一份備份；它是**升級回退**（見下兩條）的資料來源——單獨還原它不構成退路，因為升級只能 forward-fix，還原後服務仍會用新 image 重跑同一批模組升級。每日排程的備份不算它。
_Avoid_: 「發版前 snapshot」（那是平台 Odoo 舊 SOP 裡的 Longhorn snapshot，語意見上條）、「唯一的退路」（2026-09-26 前的舊定義，少了釘回版本那一半）

**還原（一次還原）**：把某一份備份的 DB dump 與 filestore 快照寫回**同一個** odoo `cloud_service`（同 release、同 DB 名），讓服務回到那份備份的時點。先在旁路把整份還原好並驗證，再以改名瞬間切換；切換前的現況改名保留一段時間，是那次還原唯一的 undo。它作用在 prod 本身、不做中和；備份時點之後的資料一律丟失，而且外部世界（已寄出的信、金流與發票序號、POS 離線訂單）不會跟著回捲。由平台 ops 觸發，整段期間服務處於「還原中」，所有會喚醒或改動它的路徑都被擋住。同一機制只做前半段、不切換，稱「旁路取資料」，用來救單筆資料。
_Avoid_: 「rollback」「回滾」「回退」（那是 helm revision 回退，只換 image 與 manifest、不動資料）；「還原錨點」（Longhorn snapshot，見「備份」條）；「中和」條裡的「restore」指 prod clone 進沙盒的那一步，不是本條

**升級回退**：升級失敗時的單一 ops 動作——還原該次的升級前備份，**並且**把 odoo image 釘回那份備份所記錄的版本並鎖住（避免下一次重新部署又升回去）。兩件事按固定順序一起完成，只做其中一件都不構成退路。
_Avoid_: 「rollback」（租戶可自行觸發的 helm revision 回退，不動資料，對 odoo 服務已被拒絕）；不加限定詞的「還原」（少了釘回版本那一半）

**prod clone**：平台 Odoo（或任一 odoo 站）的 DB＋filestore 副本，來源可以是 live `pg_dump` 或一份備份（見上條）；它帶著 prod 的全部憑證、排程與身分，**未經中和前不得啟動**。
_Avoid_: 「正式副本」「prod 資料」（分不出是副本還是 prod 本身）

**中和**：restore 之後、Odoo 程序起來之前，把 prod clone「對外行動」的能力拔掉的那一組動作——停排程、廢外連憑證與通道、改身分、換掉進入面的密鑰、抹掉平台 DB 裡存放的租戶 live 憑證；第一層是 Odoo 官方的 `odoo neutralize`（`database.is_neutralized`），平台只補差。**任何會再啟動 Odoo 程序的步驟之後都要再中和一次**。
_Avoid_: 「去敏」「脫敏」（那是 PII 遮罩，中和的其中一小步、目的不同）；「neutralized」在 `src/models/sh_subscription_fix.py`（#324）另指「上游 buggy wallet cron 被 override 成 no-op」，與本條無關

**彩排**：在拋棄式 dev sandbox 上，用中和過的 prod clone 把一次 cutover（例：平台 Odoo 換 chart）從頭走到尾，量出未知並留下可覆核的證據；與正式切換的差異（cron 全停、operator 黑洞、密鑰已換…）必須列成差異表，不能當成正式切換的替身。
_Avoid_: 「排練」（`docs/plans/2026-Q3/2026-09-03-*` 對 #1032 Phase 2 的舊稱）、「演練」（issue #1162 標題與 harumi scout doc 的泛稱）
