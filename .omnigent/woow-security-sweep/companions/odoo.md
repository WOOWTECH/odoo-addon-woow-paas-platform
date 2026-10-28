# Odoo addon hunting

woow-security-sweep 自己的 companion，格式比照 `third_party/cloudflare-security-audit/` 的 companion 檔：
`ticket.py` 依 `targets/<repo>.yaml` 的 `review_classes`（例如 `odoo.md#sudo() scope creep`）原文擷取
`Core discipline`、指定類別、`Universal moves`、`Validation rules` 放進工單。類別名稱（粗體）是引用鍵，
⛔ 改名要同步改 targets 與 `tests/test_ticket.py`。

#### When to use this file

目標是 Odoo addon（有 `__manifest__.py`、`controllers/` 的 `@http.route`、`models/` 的 ORM model）。
和 `ATTACK-CLASSES.md` 一起用：那邊問「這個主體能不能做這件事」，這裡補 Odoo 讓權限判斷失準的固定位置
（`sudo()`、route 的 `auth`／`csrf`、RPC 可呼叫的 public method、record rule、欄位 `groups`、portal 帳號）。

## Core discipline (include in every agent prompt for this domain)

```
- 每個 sudo()／SUPERUSER_ID／with_user() 都要寫出：哪個 request 可控的值選中了記錄、之前做了哪個身分檢查、結果流到哪（回應、render、write、外呼）。
- route 的 auth 與 csrf 是起點不是結論：auth='none' 沒有 env user，auth='public' 是 public user；csrf=False 的 POST 需要別的綁定（簽章、token＋state、或只讀）。
- 群組要寫實名：public、portal（share）、internal、某個 group、Settings 管理員。說明寫入端需要哪個 group，以及那個 group 是否本來就有聲稱的權限（例如已隱含 base.group_system）。
- 控制在上游 addon（__manifest__.py 的 depends）時，先讀工單提供的 upstream 唯讀快照；快照裡沒有、而且決定性時才列 needs_validation。
- Odoo core 的行為（call_kw 派發、related_sudo、Html sanitizer、redirect(local=True)）不在快照裡；決定性時寫明要驗證的 Odoo 18 行為，不要憑記憶定案。
```

## Route and session attack classes (subagent_type: `general`)

**Public route before identity check**
`auth='public'`／`auth='none'` 的 route 在驗證身分或簽章之前就做 `sudo()` 讀寫、建立記錄、或呼叫外部 API。比對同一效果的所有 route（含 legacy、booking、JSON 版本），最弱的那條就是實際政策。

**csrf=False state change**
`type='http'` 且 `csrf=False` 的 route 會改狀態（登出、綁定、改設定、寫記錄），卻沒有簽章、one-time state、或其他與瀏覽器 ambient credential 無關的綁定。只讀 route 不算。

**Session establishment outside credential check**
手動設 `request.session.pre_uid`／`pre_login`、`finalize()`、`authenticate()` 繞過 `res.users._check_credentials`：比對 MFA、停用帳號、`share` 檢查、session rotation 是否在每條登入路徑都有。

**Redirect target and URL building**
`request.redirect()`、`werkzeug.utils.redirect`、組 URL 的字串串接：使用者可控的 `redirect`／`r`／`next`／`target` 參數是否走白名單或 safe local path；反斜線、`//`、編碼形式。

## ORM authority attack classes (subagent_type: `general`)

**sudo() scope creep**
`sudo()` 之後的 `search`／`browse` 沒有把結果綁到 `request.env.user`、partner 或 company；id 來自 request；`limit=1` 搭配不確定的排序；`active_test` 讓封存記錄消失或出現。

**Record rule and multi-company gaps**
沒有 `ir.rule`、model 有 `company_id` 卻沒有 `check_company`／company domain、portal 存取靠 controller 自己過濾而不是 record rule。比對 controller 過濾和 ORM 直接存取（RPC、export）是否一致。

**Field-level groups and related fields**
秘密或敏感欄位沒有 `groups=`（`password=True` 只影響畫面）；`res.config.settings` 或其他 TransientModel 的 related 欄位預設 `related_sudo=True`；`compute_sudo`；`read()` 經 RPC 回傳整列。

**Public method RPC exposure**
名稱不以 `_` 開頭的 method 都能經 `/web/dataset/call_kw` 呼叫：AbstractModel（沒有 ACL 列）、`@api.model` method、先做外部副作用才碰到 ACL 的 `action_*`。比對同一 model 其他 method 有沒有 `check_access`／`has_group`。

**Portal and share user provisioning**
以 SUPERUSER 建 `res.users` 卻沒給明確 `groups_id`（沿用 `base.default_user` 範本群組）、只移除 internal group、login 去重與封存帳號、email 與 login 同步、以 email 比對既有 `res.partner` 綁定身分。

## Output and template attack classes (subagent_type: `general`)

**QWeb and Markup escaping**
`t-raw`、`t-out` 搭配 `Markup()` 包住使用者資料、controller 用 f-string 組 HTML 或 inline `<script>`、`fields.Html(sanitize=False)` 或關掉 `sanitize_attributes`／`sanitize_style`。JSON 進 `<script>` 要確認 `</` 被斷開。

## Data and lifecycle attack classes (subagent_type: `general`)

**ir.config_parameter as trust boundary**
誰能寫哪些 key（Settings 管理員，或某個 group 經 `sudo().set_param` 鏡像寫入）；讀出的值被插進頁面、URL、JS 或用來選 secret。

**Migrations and hooks raw SQL**
`cr.execute` 用字串格式化、`post_init_hook`／migration 大量改 `res_users`、`ir_model_data`；只在升級時跑、值來自常數的通常不是 finding。

**Mail and notification side channels**
`mail.notification`／`mail.message` 的 hook 以 sudo 讀 tracking values、followers、金額再推到外部管道：是否跳過欄位 `groups`、收件人的存取範圍、opt-out 旗標。

## Universal moves (apply across the above)

- 對每個 `sudo()` 畫出「request 值 → 選中的記錄 → 輸出或副作用」三段，任何一段沒有身分綁定就往下追。
- 同一個效果的所有入口（http route、json route、RPC method、cron、webhook、mail hook）並排比對，最弱的入口就是實際政策。
- 讀到 `limit=1`、`active_test`、`search([('email', '=', ...)])` 時，問「有兩筆符合時選誰」「封存的那筆去哪了」。
- 上游 addon 的檢查（token 驗證、ACL、record rule）要在 upstream 快照裡找到實際程式碼，而不是只看呼叫端的函式名稱。

## Validation rules (apply before reporting ANY finding here)

1. 寫出 Odoo 主體與它取得的具體能力；主體所在的 group 本來就能做的事不是 finding。
2. 單獨一個 `sudo()` 不是 finding：要有 request 可控的選取、缺少的綁定、以及可觀察的輸出或副作用。
3. RPC 曝露要寫出：method 為 public（無 `_` 前綴）、讓呼叫者到得了的 model ACL、以及 method 在沒有額外檢查時會做什麼。
4. 預設 sanitize 的 `fields.Html` 本身不是 XSS；要有 sanitize 被關、`Markup()` 包原始輸入、或 sanitizer 之外的 sink。
5. 決定性事實在 Odoo core 或沒有提供快照的上游 addon ⇒ `needs_validation`，寫明要驗證的確切行為與本機 fixture；在快照裡找得到 ⇒ 不能以「不在範圍內」當 blocker。
