# Pattern: identity / invariant guard 別用 `env.su` / group 豁免 —— 用 private sentinel escape hatch

> 適用：一個 model 有「**identity anchor / 不可變欄位**」（`subdomain_prefix`、`canonical_name`、`reference_id` 之類，hostname / DNS / tunnel / FQDN 從它推導），且**沒有任何內部合法路徑需要在事後改它**（rename = delete + recreate）。要守這個不變量，guard 條件**不能寫成 `not self.env.su` / `not has_group('base.group_system')` 豁免** —— 因為 Odoo `TransactionCase` 預設就是 **su env**，guard 的 deny 分支永遠走不到 → 測試**假綠**（`AccessError not raised` 不會發生，但你以為被守住了，其實是 guard 整條被跳過）；同時任何 `sudo()`'d controller / pipeline 路徑也靜默繞過。正解：guard **無條件擋所有人（含 superuser）**，只留一把 **private sentinel escape hatch** —— hatch 仍然是一把 context key（如 `sa_internal_prefix_write`），但它的**值**必須是模組層級的私有物件（`_INTERNAL_IDENTITY_WRITE`）、判斷用 `is` 身分比對，給唯一合法的內部維修 / 初始化路徑開門。**不可以是裸真值**：`/web/dataset/call_kw` 把 caller 的 context 原封不動餵給 ORM，任何 JSON 表達得出來的值（`True` / `1` / `"true"`…）只要在 RPC payload 多塞一個 key 就能打開（#522）。JSON 帶不動物件身分，sentinel 正是靠這點關門。

## When to use

當下列條件**同時**成立：

1. **欄位是 identity anchor / 不可變 invariant**：值一旦定下就不該再變，且**沒有任何內部合法路徑**需要事後改它（route prefix、canonical name、reference id —— 改了會讓 hostname / DNS / ingress / public FQDN 漂移）。
2. **你想在 model `create()` / `write()` 用 guard 擋變更 / 注入**。
3. **guard 的測試跑在 `TransactionCase`（= su env）**，或 model 有 `sudo()`'d 內部寫入路徑。

→ 三者成立 → **不可**用 `not self.env.su` / `not has_group(...)` 當 guard 條件。那會讓 su-env 測試假綠 + sudo 路徑靜默繞過。改用 private sentinel escape hatch（context key 的值是模組層級私有物件、以 `is` 比對，**不是** `True`）。

**不適用**：

- 欄位**確實有**內部合法的「事後改值」需求，且該需求只該開放給 admin / sudo（如 scope-flip 防護，sudo pipeline 要能正常設 scope）→ 那是 [[scope-flip-write-guard-not-ir-rule]] 的場景，`env.su` + `has_group` 雙閘放行是對的。差別在：**那條的不變量是「值轉移受限」（有合法改值路徑、只是要 gated），本條的不變量是「值不可變」（根本沒有合法改值路徑）**。混淆兩者就會錯用豁免條件。

## 問題敘事（Security Access P1 實際撞牆）

### 第一撞 —— route 的 `subdomain_prefix` 不可變 guard，su-env 假綠

`SecurityAccessRoute.write()` 原本寫：

```python
if 'subdomain_prefix' in vals and not self.env.su:
    ... raise UserError(...)
```

意圖：擋使用者改 route 的 `subdomain_prefix`（hostname / DNS / ingress 全從它推導，rename = delete + recreate）。但 `test_route_prefix_immutable`（今天在 `src/tests/test_security_access.py:447`）跑在 `TransactionCase` —— **預設 su env**。`not self.env.su` 在 su env 下恆 `False` → guard 整條被跳過 → 期望的 `UserError` 沒拋 → 那次 sandbox **963 tests 唯一一條紅**。
修法（commit `5088a31`）：把條件改成 `not self.env.context.get('sa_internal_prefix_write')` —— su 也擋，內部維修路徑用 context key 放行（目前**沒有任何路徑設這個 key**，它是留給未來 data-repair migration 的逃生口）。——注意這只是第一版。#522 / PR#537 之後條件再收緊成 **sentinel 身分比對**（`self.env.context.get('sa_internal_prefix_write') is not _INTERNAL_IDENTITY_WRITE`，`src/models/security_access.py:1179-1181`），因為裸真值那版在 `call_kw` 上是可偽造的。「目前沒有任何路徑設這個 key」這句**至今仍成立**：production code 只有 `:1180` 一個**判斷**處，全 repo 沒有任何路徑**設定**它（唯一寫入處是 `src/tests/test_security_access_identity_anchor_522.py:300` 的負向偽造測試，斷言必須 raise；另 `docs/plans/2026-Q2/2026-06-06-security-access-p1-mainline.md:2061` 是歷史計畫文字）。

### 第二撞 —— 對抗審查 round 抓到同 family（`canonical_name` / `reference_id`）

修完 route 後，審查回頭看 `SecurityAccess` 的 `canonical_name` / `reference_id` write guard —— 寫的是同一個豁免 `not self.env.su and not self.env.user.has_group('base.group_system')`。同病：

- **su-env 假綠**：deny 分支在 `TransactionCase` 下走不到，測試沒真正驗到 deny。
- **create-time 注入缺口**：guard 只在 `write()`，但 `super().create()` **不經過 `write()`**，且 `create()` override 只在 `canonical_name` 缺失時生成、從不 pop caller 傳入的值 → workspace member 直接 ORM `create({'canonical_name': 'paas-sa-victim'})` 就能 squat 一個受害者命名空間的 identity（它決定 tunnel name / 主 `subdomain_prefix` / DNS comment stamp / public FQDN，只有平台級 UNIQUE 擋「已被用過的 prefix」，沒用過的 victim-style canonical 任你搶）。

修法兩 commit：
- `a8f1ba7` —— 在 `create()` 剝除 caller 傳入的 `canonical_name` / `reference_id`（先用 su/system 條件，與 write guard 對齊覆蓋 create 方向）。
- `16fd427` —— 把 write guard **與** create-time strip **同時**改成 context-key escape hatch（`sa_internal_identity_write`，**當時的值是裸真值**），徹底拿掉 `env.su` 豁免，關閉 su-env 假綠；並補 su-env 測試證明 guard 在 `TransactionCase` 下真的會 fire（`test_canonical_name_write_guard_fires_under_su` / `test_canonical_name_injection_stripped_under_su`，`src/tests/test_security_access.py:50,62`）。**這一步後來被第三撞取代。**

### 第三撞 —— 逃生口本身被 RPC 偽造（#522 / PR#537）

前兩撞把豁免從 `env.su` 換成 context key，su-env 假綠是關掉了，但逃生口的**值**還是裸真值。`/web/dataset/call_kw` 把 caller 的 context 原封不動交給 ORM —— `addons/web/controllers/dataset.py:32-36` 直接 `call_kw(request.env[model], method, args, kwargs)`（kwargs 未過濾），`odoo/api.py:530-532` 再 `kwargs = dict(kwargs)` / `context = kwargs.pop('context', None) or {}` / `recs = recs.with_context(context)`。注意那是**位置參數**＝整包取代不是 merge（`odoo/models.py:6362-6376` 的 `with_context` docstring 明證），所以那次呼叫的 `env.context` 完全由 caller 決定。

⇒ **任何能經 `/web/dataset/call_kw` 觸及本 model 的非系統帳號**，只要在 payload 多塞一把 `context: {"sa_internal_identity_write": true}`，guard 覆蓋的每個欄位就全開；18.0 實測 `canonical_name` 被改寫並 commit 進 Postgres（#522 的 sandbox 實測用的是一個只有 `base.group_user` 的 workspace owner，見 `src/tests/test_security_access_identity_anchor_522.py:88-94`）。

> ⚠ 前提限定：**正式環境租戶帳號實際落在哪個 Odoo group 尚未定案**（`CONTEXT.md:20`；signup 出來的是 portal，本 addon ACL 對 portal 零命中，前台寫入由 controller 以 `.sudo()` 代跑）。本條的守線理由**不依賴**那個答案 —— 只要有任何一條 sudo'd controller 路徑會把 caller context 帶進 ORM，裸真值 hatch 就是開的。

修法：逃生口的值改成模組層級私有 sentinel 物件，判斷用 `is`（`src/models/security_access.py:64-69`、`:91`、`:94-102`）。JSON 表達不出物件身分 → 線上打不開；in-process 呼叫端照舊。同一輪還把「手工逐欄位列舉」換成宣告式清單 `_IDENTITY_ANCHOR_FIELDS` / `_ROUTE_IDENTITY_ANCHOR_FIELDS`（`:50-61`），因為 #522 的第一個缺陷是 `tunnel_id` 從來沒被列進去。

## 機制解釋（為什麼會這樣）

1. **`TransactionCase` 是 su env**：Odoo 後端測試預設以 superuser context 跑。任何 `not self.env.su` 條件在測試裡恆 `False`，guard 的 deny 分支**結構上不可達** → `assertRaises` 不會觸發，但你不會看到紅（因為根本沒進 raise 那行）→ 假綠：你以為「測試證明 guard 擋住了」，實際上「測試證明 guard 沒被執行」。要真的驗到 deny，要嘛 guard 對 su 也擋（本 pattern），要嘛測試顯式 `with_user(non_admin)` 退出 su（但那只覆蓋 user 路徑，仍漏 sudo 路徑）。
2. **`sudo()` 路徑靜默繞過**：`env.su` 豁免不只放行測試，也放行任何 `record.sudo().write(...)` 的內部 / controller 路徑。identity invariant 是「對**所有人**包含 superuser 都不可變」的 hard invariant，用 su 當逃生口等於把逃生口開給每一條 sudo 呼叫。
3. **`create()` 不經 `write()`**：`super().create()` 不會走 model 的 `write()` override，所以「只在 `write()` 放 guard」對 create-time 注入完全無效。不可變欄位的守線**必須同時覆蓋 create（vals 注入）與 write（事後改值）**，缺一邊就是半套。
4. **per-call 明示 opt-in 才是對的逃生口形狀**：`record.with_context(...).write({...})` 是一次呼叫的明示宣告，只有唯一合法的初始化 / 維修路徑會設；不像 `env.su` 是一整類呼叫的隱式屬性。預設沒人設它 → 預設對所有人關閉 → 測試在 su env 下也能真正走到 deny。
5. **但 context 是使用者可寫的**：`call_kw` 把 caller 的 context **整包換進** env（`odoo/api.py:530-532` 是 `with_context(context)` 位置參數，不是 merge），所以「key 存在且為真」這種判斷等於把逃生口開在 RPC payload 上。逃生口必須綁在 **JSON 表達不出來的東西**上 —— 模組層級私有 sentinel 物件 + `is` 比對：

   ```python
   class _InternalIdentityWrite:
       __slots__ = ()

   _INTERNAL_IDENTITY_WRITE = _InternalIdentityWrite()

   def _internal_identity(records):
       return records.with_context(sa_internal_identity_write=_INTERNAL_IDENTITY_WRITE)

   def _is_internal_identity_write(env):
       return env.context.get('sa_internal_identity_write') is _INTERNAL_IDENTITY_WRITE
   ```

   `is`（不是 `==`）：`==` 會被自訂 `__eq__` 或同值物件騙。sentinel 私有（`_` 開頭、不 export 給 controller）：能 import 到它的就是 server 端自己的程式碼。

## 守則（怎麼做）

1. **「沒有任何內部合法路徑需要改」的不變量** → guard **無條件擋（含 superuser）** + 留一把 **private sentinel escape hatch**（給未來 data-repair migration / 唯一初始化路徑）。不要用 `env.su` / `has_group` 豁免，**也不要用裸真值 context key** —— 那把逃生口是可從 RPC payload 打開的。
2. **不可變欄位的 guard 必須同時覆蓋 create 與 write**：`write()` 擋事後改值；`create()` override pop 掉 caller 傳入的 identity key（因為 `super().create()` 不經 `write()`）。兩個方向用**同一個** escape-hatch 條件，對稱關閉。不同「維修語意」可以用**不同的 context key**、但共用同一顆 sentinel（例：prefix 改名用 `sa_internal_prefix_write`、anchor 寫入用 `sa_internal_identity_write`，兩者都比對 `_INTERNAL_IDENTITY_WRITE`），讓一條只該改名的 migration 不會順便獲得搬 tunnel id 的能力（理由寫在 `src/models/security_access.py:1176-1178`）。
3. **guard 的測試在 su env 下要能真正走到 deny 分支**：在 `TransactionCase`（su env）直接 `write` / `create` 注入，斷言 `UserError` / 值被剝離。若這條測試在你「拿掉 guard body」後**仍綠**，代表它根本沒打到 guard —— 那是假綠，不是守線。
4. **用 sentinel 前先查三個序列化邊界**（sentinel 只活在 process 內；一旦被序列化就會靜默降級成「hatch 沒帶到」，症狀是合法內部路徑突然被自己的 guard 擋住）：
   - **manifest 沒有 `queue_job` / 任何 job runner**：job 的 context 走 JSON 進 DB，sentinel 到不了 worker。本 repo 現況：`src/__manifest__.py` 的 `depends` 無 `queue_job`，全 repo 也無 `with_delay`。
   - **沒有把 context 整包塞進回傳 dict 或欄位**：action dict / JSON response 會被序列化，sentinel 直接讓 `json.dumps` 爆掉。本 repo 目前只掃到一處把 `env.context` 複製進 dict：`src/models/skill.py:327-330`，且已 key-filter 到 `default_scope` / `default_project_id`。
     ⚠ 這是**以幾組固定語法形態掃出來的**（`context.items()` / `dict(self.env.context)` / `context.copy()` / `**self.env.context` 之類）；「把 `env.context` 當引數傳出去、之後才被序列化」的形態抓不到，所以別把它當全稱命題。
   - **沒有 `ir.cron` 靠 `context` 欄位帶這把 key**：cron 的 context 是 DB 上的字串、走 `safe_eval`，寫不出物件。本 repo 現況：`src/data/*.xml` 內所有 `ir.cron` 記錄都沒有 `context` 欄位。
   三項有任一不成立，就不是不能用 sentinel，而是**那條路徑得在 process 內重新取得 hatch**（在 job / cron 的執行函式裡才呼叫 `_internal_identity()`），不要把 hatch 放進被序列化的 payload。

## 證據（commit / 檔案:行號）

- `5088a31` `fix: enforce route prefix immutability for superuser env` —— route prefix guard 從 `not self.env.su` 改 context key。現況：`src/models/security_access.py:1160`（`SecurityAccessRoute.write`）、`:1179-1181`（`sa_internal_prefix_write` sentinel 條件）。暴露點 `src/tests/test_security_access.py:447` `test_route_prefix_immutable`（當時 963 tests 唯一紅）。
- `a8f1ba7` `fix: strip injectable identity anchors at security access create time` —— `create()` override pop `canonical_name` / `reference_id`，補 create 方向。
- `16fd427` `fix: close su-env false-green in security access identity guard` —— write guard + create strip 一起改 `sa_internal_identity_write` context key、拿掉 `env.su` 豁免、補 su-env 測試。現況：`src/models/security_access.py:332`（`create`）、`:342`（create strip）、`:351-353`（初始 canonical_name 寫入走 `_internal_identity()`）、`:356`（`write`）、`:367-368`（write reject）。
- **#522 / PR#537** —— 逃生口從裸真值改私有 sentinel、guard 改吃宣告式 anchor 清單。現況：`src/models/security_access.py:50-61`（`_IDENTITY_ANCHOR_FIELDS` / `_ROUTE_IDENTITY_ANCHOR_FIELDS`）、`:64-102`（sentinel + `_internal_identity()` + `_is_internal_identity_write()`）、`:105-137`（create strip / write reject 兩個共用 helper）、`:1118-1119`（route create strip）、`:1167-1168`（route write reject）。守線測試整檔 `src/tests/test_security_access_identity_anchor_522.py`，關鍵三條：`:266` `test_forged_context_does_not_open_the_hatch`、`:293` `test_forged_context_does_not_open_the_prefix_hatch`、`:305` `test_forged_context_does_not_open_the_hatch_at_create`；正向對照 `:340` `test_internal_sentinel_still_writes_server_owned_anchors`。
- su-env 守線測試：`src/tests/test_security_access.py:50` `test_canonical_name_write_guard_fires_under_su`、`:62` `test_canonical_name_injection_stripped_under_su`；cross-user 負向：`:882` create 注入剝離（canonical_name）、`:895` 同（reference_id）、`:906` write deny。

## 偵測 / 測試建議

- **grep 自查**：在 `create()` / `write()` override 的不可變欄位 guard 裡搜 `not self.env.su` / `has_group('base.group_system')`。命中時問自己「這個不變量有合法的內部改值路徑嗎？」沒有 → 該換 context key；有但只給 admin → 確認那是 [[scope-flip-write-guard-not-ir-rule]] 場景才保留豁免。
- **guard-removal 反測**：對守 invariant 的測試，暫時把 guard body 註解掉重跑 —— 若測試**仍綠**，它沒打到 guard（su-env 假綠 / 沒退出 su），不是有效守線。
- **create + write 雙向覆蓋**：每條 identity / 不可變欄位都要有「`write()` 改值被擋」**與**「`create()` 注入被剝離 / 被擋」兩條測試，且至少一條跑在 su env（`TransactionCase` 直接 create/write，不 `with_user`）證明 guard 對 superuser 也 fire。
- **grep 自查（第二形狀，#522 後才知道要查）**：搜 `env.context.get(` 出現在 guard 條件裡卻**沒有** `is <sentinel>` 的地方 —— 那就是裸真值逃生口。（`git grep -n "context\.get(" -- 'src/**/*.py' | grep -v "/tests/"`，再逐條問「這個 key 打開的是不是一條安全決策？」）判準不是「有沒有用 context」，是「**租戶能不能在 payload 表達出那個值**」。
- **forged-context 反測（必備）**：對每條 guard 加一條測試，用 `with_context(**{KEY: v})` 跑遍 `(True, 1, 'true', [1], {'x': 1})` 全部斷言 raise，並配一條「真 sentinel 仍能寫入」的正向對照 —— 否則一條「什麼都擋」的壞 guard 會讓所有負向斷言為了錯的理由變綠（範本見 `src/tests/test_security_access_identity_anchor_522.py:266` 與 `:340`）。

## See also

- 同族不同層：[[scope-flip-write-guard-not-ir-rule]] —— 那條講「scope / tenancy 不可變必須靠 model `write()` guard 而非 ir.rule」（**用 write guard** 這層）；本條講「write / create guard **本身的豁免條件**怎麼寫才測得到、才不被 sudo 繞過」（guard 的**條件**這層）。關鍵分野：scope-flip 的不變量是「值轉移 gated」（有合法改值路徑，`env.su` + admin 放行**是對的**）；本條的不變量是「值不可變」（**沒有**合法改值路徑，任何 su 豁免都是洞 + 假綠）。改 identity guard 前先分清自己在哪一邊。
- 同主旨（驗綠範圍 / 證據不對 → 漏抓）：[[orm-vs-endpoint-acl-test-coverage]]、pitfall `sandbox-module-test-false-green.md`（0 tests 也報綠）—— 三者共通：**測試「綠」不等於守線存在，要看它有沒有真的走到該守的分支**。
- 原始來源（合成材料、不複製）：commit `5088a31` / `a8f1ba7` / `16fd427` + plan `docs/plans/2026-Q2/2026-06-06-security-access-p1-mainline.md` ＋ #522 / PR#537（逃生口從裸真值 → 私有 sentinel；同輪把 guard 改吃宣告式 anchor 清單）。
