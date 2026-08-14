---
name: decision-519-cloud-service-provision-gate
status: accepted
created: 2026-08-14
updated: 2026-08-14
phase: "#519 方向 2 實作（提案 A ＋ B 同批）"
team: repo-leader（woow-paas-platform）
deciders:
  - Eugene（2026-08-14 裁決：只走方向 2；A ＋ B 同批）
related:
  - src/models/cloud_service.py
  - src/models/security_access.py
  - docs/decisions/patterns/su-env-exempt-guard-false-green.md
---

# #519：cloud_service 的建立閘（private sentinel）與 subscription 綁定守衛

> 這份是 #519 方向 2 的**設計權威**。原始評估寫在 repo 外的非版控路徑，會隨環境
> 消失，所以要點落在這裡。行號一律當會漂，自己重新定位。

## 裁決（2026-08-14，Eugene）

1. **只走方向 2**（model 層 `create()` 守衛）。方向 1（收斂 ACL）與方向 3
   （cron fail-closed）本輪不做。
2. 「任何 `base.group_user` 可自建 workspace 並自動成為 owner」是**產品設計**，
   不是缺陷 ⇒ 不動 ACL／`ir.rule`／role。
3. 提案 **A ＋ B 同批**完成。

方向 1 為什麼對本單無效：owner 資格是**可自我滿足**的——任何 `base.group_user`
帳號可免費自建 workspace（`ir_rules.xml` 的 `workspace_rule_anyone_create` domain
恆真）→ `workspace.py` 的 `super().create()` 之後用 `.sudo()` 自動補一列
`workspace_access(role=owner)` → 滿足 #676 落地的
`cloud_service_rule_owner_admin_manage` → 裸 ORM 建得出 cloud_service。#676 擋掉的
是 `user`／`guest`／非成員，擋不到「自己開一個 workspace 當 owner」。

## 提案 A — `create()` 的 private sentinel 閘

### 不變式

> **沒有任何 RPC 呼叫端**（裸 ORM／XML-RPC／JSON-RPC／`call_kw`，不論身分、不論
> 送什麼 vals 或 context）能讓一筆 `cloud_service` 落地。只有明示開閘的平台
> 伺服器端 in-process 碼建得出來。

### 判準讀什麼、不讀什麼（每一項都是刻意的）

| 來源 | 讀？ | 理由 |
|---|---|---|
| `env.context[KEY]` 的 **key** | 讀，但**不是判準** | `call_kw` 把呼叫端 context 整包換進 env，key 字串當然抄得到 |
| `env.context[KEY]` 的**值身分**（`is _INTERNAL_PROVISION`） | **唯一判準** | JSON-RPC／XML-RPC 的型別集合表達不出「某個模組層級私有 class 的那一個實例」 |
| `vals`（raw 或 `_add_missing_default_values` 解析後） | **不讀** | 租戶對 `ir.default` 有完整 CRUD（Odoo 18 base `ir.model.access.csv` 的 `access_ir_default_group_user` 是 1,1,1,1，且 `ir.default.set()` 是 `@api.model`、不查權限）⇒ `default_<欄位>` 有**持久化**載體，讀 vals 的守衛會被繞過。不讀 vals ⇒ 這條陷阱在本形狀下結構上不存在 |
| `env.su` | **不讀** | `TransactionCase` 預設就是 su env ⇒ deny 分支不可達 ⇒ 測試假綠（見 [[su-env-exempt-guard-false-green]]） |
| `env.user` / `env.uid` | **不讀** | `controllers/ha_paas.py` 的 `auth='none'` 路徑下 `env.user` 是空 recordset，讀它會誤判 |
| workspace role / `has_group()` | **不讀** | 讀了就把「自建 workspace 即為 owner」這條產品設計變成守衛的旁路 |
| 記錄狀態 | 不讀 | A 不需要 |

### 為什麼值必須是 sentinel、不能是 `True`

`/web/dataset/call_kw` 把呼叫端送來的 context **整包**換進 env
（`odoo/api.py::call_kw`：`context = kwargs.pop('context', None) or {}` →
`recs.with_context(context)`，位置參數＝取代不是 merge）。#522 在 18.0 上**實測**
過同一件事：一個只有 `base.group_user` 的租戶送
`context={'sa_internal_identity_write': true}` 就改寫了 `canonical_name`，新值真的
commit 進 Postgres。JSON 載不了物件身分。

本閘與 `models/security_access.py` 的 `_INTERNAL_IDENTITY_WRITE` 同構，刻意逐字沿用
它的形狀，不另發明。

### 殘留風險

唯一真實的殘留風險是「未來新增一條 sudo'd controller，把呼叫者 context 往下傳並
自己開閘」。這條用 `scripts/audit/repo-lint.py` 的 allowlist gate 關掉：sentinel 只
能出現在允許清單內的檔案，新增開閘路徑就會撞 gate ⇒ 強制走審查。

## 提案 B — `subscription_id` 的歸屬／狀態／唯一性守衛

A 關掉的是「誰能建」，關不掉「已合法建立的 service 事後把 `subscription_id` 改指向
別人的活訂閱」。B 補這一格。

### 不變式

> 任一 cloud_service 的 `subscription_id` **在綁上去的那一刻**必然是一筆
> `sh_partner_id == workspace_id.owner_id.partner_id` 且 `state='in_progress'`
> 的訂閱；且一筆訂閱最多被一筆 cloud_service 指向。

### 🔴 這是 transition guard，不是常駐不變式（實作時修正的一格）

原始評估把 B 寫成「任一 cloud_service 的 `subscription_id` **必然**是一筆
`in_progress` 訂閱」。**那個寫法在今天的碼上是錯的**：訂閱到期／關閉之後仍然合法
地綁在服務上——`_cron_enforce_subscription_lifecycle` 正是靠「找得到指向已過期訂閱
的服務」才做得到停權。寫成常駐不變式的實作會把「訂閱已到期但服務還在停權流程裡」
這個**正常狀態**判成違規，而且它會在最不該紅的地方紅（停權流程自己）。

⇒ 狀態這一格只在**綁定真的改變的那一次**檢查。判別式是「讓一筆已到期的訂閱留在
原位再寫一次同值，必須不被擋」（§9 的 B5，實作在
`test_cloud_service_subscription_binding_519.py::test_B5_*`）。

### 另一格：解除綁定一律拒絕

`_cron_enforce_subscription_lifecycle` 的 domain 是 `subscription_id != False`
⇒ 把指標清空不是「回到未開通狀態」，是**永久脫離生命週期管理**（停權／結算都再也
摸不到它）。全 repo 只有兩處寫這個欄位，都是指向新訂閱，沒有任何合法路徑會清空它。
（尚未掛訂閱的服務寫 `False` 不算解除綁定——marketplace 下單流程在建立與掛訂閱之間
就處在那個狀態。）

### 洞怎麼來的

Odoo 18 寫 Many2one **不檢查 comodel 的 ACL／ir.rule**（`odoo/models.py` 的
`write()` 只 `check_access('write')` 被寫入的 model 自己；`Many2one.write()` 只更新
cache）。`subscription_id` 沒有 `check_company=True`、沒有 `@api.constrains`、
`write()` override 也完全沒碰它 ⇒ 租戶可以把**任意存在的訂閱 id**（含別人的）綁到
自己的 service，id 可暴力枚舉、不需要對該訂閱有讀權限。實務攻擊：自己的訂閱被停權
後改指向別人的活訂閱，逃過 `_cron_enforce_subscription_lifecycle`。

### 🔴 判準**必須**含 `state == 'in_progress'`

第三方 `sh_subscription` 的 `/renew-subscription`（`auth="user"`）用
`sudo().create()` 讓任何登入使用者拿自己的訂閱當範本再鑄一筆，`sh_partner_id` 仍是
自己 ⇒ **租戶鑄得出屬於自己的訂閱**。只驗「訂閱存在且屬於我」會被無限鑄幣打穿。

擋得住的原因是鑄出來的停在 `state='draft'`（欄位 `default='draft'`），而平台的判準一律
是 `in_progress`。`state` 被寫成 `in_progress` 的地方，整個 `sh_subscription` 只有
`sh_subscription_confirm()` 一處；它的觸發面是：`sale_order.py`（要能確認 SO）、平台
自己的 mixin、以及後台表單的 Confirm 按鈕（`call_kw` 要過 ACL，而租戶對
`sh.subscription.subscription` 零 ACL row）。四支 `sh_subscription` cron
（auto_invoice／auto_expired／auto_reminder／renew_button_visible）沒有任何一支把
draft 轉 `in_progress`。

⚠ 這個保證**錨定在第三方 addon 的行為上**，vendored addon 一升版就可能無聲漂掉 ⇒
必須有一條「綁 draft 訂閱要被擋」的釘樁測試，它同時是漂移偵測器。第三方 addon 的鑄
幣口本身另立 **#725** 追蹤，本輪不修。

### 🔴 唯一性用 DB partial unique index，不用 Python COUNT

`cloud_service.py` 自己已經記過：Odoo 的 cursor 是 **REPEATABLE READ**，取
advisory lock **不會**刷新 snapshot ⇒ check-after-act 在並發下無效（那正是
`max_per_partner` 至今仍可繞的原因，#518）。而「一筆訂閱至多對應一筆 service」正好
就是 partial unique index 表達得出的「至多一列」。

**索引由 `CloudService.init()` 建，不是由 migration 建**（實作時修正的第二格）：
原始評估寫「B 因此需要一支 migration」，但本 model 已有同款先例——
`provision_partner_id` 的 partial unique index 就掛在 `init()` 裡，`CREATE UNIQUE
INDEX IF NOT EXISTS` 是 Odoo 原生 hook、每次 install / `-u` 自動跑、冪等，同時涵蓋
新裝與升級兩條路徑。migration 仍然需要，但它的職責只有一件事：**在那句 SQL 之前先
偵測既有重複列**，把 `psycopg2.UniqueViolation`（讀不出是哪幾筆）換成講得清楚的
錯誤。

migration 刻意 `raise` 而不是「偵測到重複就跳過建索引」：跳過的話升級會成功、索引
靜默不存在，於是**大家都以為並發保護在那裡、其實不在**——那正是本輪要修掉的假綠
形態。⚠ 這代表**有重複列的環境升級會停住**，是刻意的取捨；PR 內附維運用的偵測
查詢。

## 被實查否決的四種形狀（省下下一個人的試錯）

| 形狀 | 為什麼不行 |
|---|---|
| `cr.precommit` 掛「交易結束時必須有訂閱」 | `odoo/sql_db.py` 的 `BaseCursor.flush()` **就會** run precommit（不是只有 commit），且 `cr.savepoint()` 預設 `flush=True` ⇒ 合法流程中間只要經過任何第三方 `savepoint()` 就會提前誤殺。Odoo core 的 precommit 用例全是「聚合 + 交易結束前批次 finalize 副作用」，沒有一個是不變式攔阻 |
| `@api.constrains('subscription_id')` | `_validate_fields` 只對「本次 write/create 有動到」的欄位跑 check；create 沒帶該欄位就整道不觸發 |
| 「vals 帶了 `subscription_id` 就放行」 | m2o 盲寫可枚舉；且被 `ir.default` 持久化注入打穿 |
| 裸真值 context 旗標 / `env.su` / `has_group` 豁免 | 前者 #522 實測可偽造；後兩者會在每個 `TransactionCase`（su env）裡靜默停用守衛 |

另外**不重排** create／訂閱順序（先建訂閱再 create service）：
`sh_subscription_confirm()` 會 `send_mail(force_send=True)`，那是 rollback 撤不回的
外部副作用；重排會把所有可失敗的閘排到寄信之後，而 `_claim_global_cap_slots` 的
`SerializationFailure` 重試會**再寄一封**。今天的順序（`controllers/paas.py` 的
create 段註解就是為此寫的）沒有這個問題。

## 資源側枚舉（誰會讓一筆 cloud_service 落地）

⚠ 天真字面 grep `cloud_service'].create(` **連 production 的正例都抓不到**
（`controllers/paas.py` 走區域變數 `service_model`，而且 RHS 還是三元運算式）。
正確做法是先枚舉「誰持有 `env['woow_paas_platform.cloud_service']` 這個 handle」，
沿 `.sudo()` / `.with_context()` / `cls.X → self.X` / 回傳 handle 的 helper 傳播到
fixpoint，再找 receiver 落在那些 handle 上的 `.create(` / `.copy(`。

實測結果（`origin/develop` `f55eccc6`）與逐項排除見 PR1 的 body；要點：

- production 的直接建立點只有 `controllers/paas.py`（marketplace 下單）與
  `controllers/ha_paas.py`（HA provision，`auth='none'` + 自訂 Bearer，**繞過**
  `_create_service`）兩處。
- 另有 `.claude/skills/k8s-dev-sandbox/scripts/e2e-seed-reset-button.sh`
  （`odoo shell`、SUPERUSER、永久無訂閱）。
- XML/CSV seed data、migrations、SQL INSERT、change_plan／`_do_resume`／redeploy／
  clone、`services/reconciler.py`、operator webhook 全部逐項排除（附陽性對照）。
- **m2m inline `(0, 0, vals)`**（`project.cloud_service_ids`）也是一條落地路徑，
  純 `.create(` 枚舉抓不到它，必須另外掃。

## 落地狀態

| PR | 內容 | 行為變更 |
|---|---|---|
| PR1 | sentinel／`_internal_provision()` helper ＋ 把所有建立點改成經 helper | **無**（還沒有閘） |
| PR2 | `create()` 加 raise ＋ 驗收測試 ＋ repo-lint allowlist gate ＋ 後台 view `create="false"` ＋ 改寫 `cloud_service.py` 那段與本方向矛盾的舊註解 | 有 |
| PR3 | 提案 B：`subscription_id` 的歸屬＋狀態＋唯一性（含 migration 建 partial unique index） | 有 |

## 明確不在本輪範圍

- 第三方 `sh_subscription` 的 `/renew-subscription`／`/cancel-subscription` 鑄幣口與
  「租戶可自行把自己的訂閱打成非活躍」⇒ **#725**
- `smart_home` / `security_access` 共用同一個 subscription mixin，同款洞仍開著
  （B 只掛 CloudService，不掛 mixin——mixin 版的 blast radius 未查）
- production DB 裡有沒有管理員手動加的 ACL 列／手動掛的 group【未驗證】
