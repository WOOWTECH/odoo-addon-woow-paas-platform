# Pattern: identity / invariant guard 別用 `env.su` / group 豁免 —— 用 context-key escape hatch

> 適用：一個 model 有「**identity anchor / 不可變欄位**」（`subdomain_prefix`、`canonical_name`、`reference_id` 之類，hostname / DNS / tunnel / FQDN 從它推導），且**沒有任何內部合法路徑需要在事後改它**（rename = delete + recreate）。要守這個不變量，guard 條件**不能寫成 `not self.env.su` / `not has_group('base.group_system')` 豁免** —— 因為 Odoo `TransactionCase` 預設就是 **su env**，guard 的 deny 分支永遠走不到 → 測試**假綠**（`AccessError not raised` 不會發生，但你以為被守住了，其實是 guard 整條被跳過）；同時任何 `sudo()`'d controller / pipeline 路徑也靜默繞過。正解：guard **無條件擋所有人（含 superuser）**，只留一把 **context-key escape hatch**（如 `sa_internal_prefix_write`）給唯一合法的內部維修 / 初始化路徑開門。

## When to use

當下列條件**同時**成立：

1. **欄位是 identity anchor / 不可變 invariant**：值一旦定下就不該再變，且**沒有任何內部合法路徑**需要事後改它（route prefix、canonical name、reference id —— 改了會讓 hostname / DNS / ingress / public FQDN 漂移）。
2. **你想在 model `create()` / `write()` 用 guard 擋變更 / 注入**。
3. **guard 的測試跑在 `TransactionCase`（= su env）**，或 model 有 `sudo()`'d 內部寫入路徑。

→ 三者成立 → **不可**用 `not self.env.su` / `not has_group(...)` 當 guard 條件。那會讓 su-env 測試假綠 + sudo 路徑靜默繞過。改用 context-key escape hatch。

**不適用**：

- 欄位**確實有**內部合法的「事後改值」需求，且該需求只該開放給 admin / sudo（如 scope-flip 防護，sudo pipeline 要能正常設 scope）→ 那是 [[scope-flip-write-guard-not-ir-rule]] 的場景，`env.su` + `has_group` 雙閘放行是對的。差別在：**那條的不變量是「值轉移受限」（有合法改值路徑、只是要 gated），本條的不變量是「值不可變」（根本沒有合法改值路徑）**。混淆兩者就會錯用豁免條件。

## 問題敘事（Security Access P1 實際撞牆）

### 第一撞 —— route 的 `subdomain_prefix` 不可變 guard，su-env 假綠

`SecurityAccessRoute.write()` 原本寫：

```python
if 'subdomain_prefix' in vals and not self.env.su:
    ... raise UserError(...)
```

意圖：擋使用者改 route 的 `subdomain_prefix`（hostname / DNS / ingress 全從它推導，rename = delete + recreate）。但 `test_route_prefix_immutable`（`src/tests/test_security_access.py:377`）跑在 `TransactionCase` —— **預設 su env**。`not self.env.su` 在 su env 下恆 `False` → guard 整條被跳過 → 期望的 `UserError` 沒拋 → 那次 sandbox **963 tests 唯一一條紅**。
修法（commit `5088a31`）：把條件改成 `not self.env.context.get('sa_internal_prefix_write')` —— su 也擋，內部維修路徑用 context key 放行（目前**沒有任何路徑設這個 key**，它是留給未來 data-repair migration 的逃生口）。

### 第二撞 —— 對抗審查 round 抓到同 family（`canonical_name` / `reference_id`）

修完 route 後，審查回頭看 `SecurityAccess` 的 `canonical_name` / `reference_id` write guard —— 寫的是同一個豁免 `not self.env.su and not self.env.user.has_group('base.group_system')`。同病：

- **su-env 假綠**：deny 分支在 `TransactionCase` 下走不到，測試沒真正驗到 deny。
- **create-time 注入缺口**：guard 只在 `write()`，但 `super().create()` **不經過 `write()`**，且 `create()` override 只在 `canonical_name` 缺失時生成、從不 pop caller 傳入的值 → workspace member 直接 ORM `create({'canonical_name': 'paas-sa-victim'})` 就能 squat 一個受害者命名空間的 identity（它決定 tunnel name / 主 `subdomain_prefix` / DNS comment stamp / public FQDN，只有平台級 UNIQUE 擋「已被用過的 prefix」，沒用過的 victim-style canonical 任你搶）。

修法兩 commit：
- `a8f1ba7` —— 在 `create()` 剝除 caller 傳入的 `canonical_name` / `reference_id`（先用 su/system 條件，與 write guard 對齊覆蓋 create 方向）。
- `16fd427` —— 把 write guard **與** create-time strip **同時**改成 context-key escape hatch（`sa_internal_identity_write`），徹底拿掉 `env.su` 豁免，關閉 su-env 假綠；並補 su-env 測試證明 guard 在 `TransactionCase` 下真的會 fire（`test_canonical_name_write_guard_fires_under_su` / `test_canonical_name_injection_stripped_under_su`，`src/tests/test_security_access.py:42,54`）。

## 機制解釋（為什麼會這樣）

1. **`TransactionCase` 是 su env**：Odoo 後端測試預設以 superuser context 跑。任何 `not self.env.su` 條件在測試裡恆 `False`，guard 的 deny 分支**結構上不可達** → `assertRaises` 不會觸發，但你不會看到紅（因為根本沒進 raise 那行）→ 假綠：你以為「測試證明 guard 擋住了」，實際上「測試證明 guard 沒被執行」。要真的驗到 deny，要嘛 guard 對 su 也擋（本 pattern），要嘛測試顯式 `with_user(non_admin)` 退出 su（但那只覆蓋 user 路徑，仍漏 sudo 路徑）。
2. **`sudo()` 路徑靜默繞過**：`env.su` 豁免不只放行測試，也放行任何 `record.sudo().write(...)` 的內部 / controller 路徑。identity invariant 是「對**所有人**包含 superuser 都不可變」的 hard invariant，用 su 當逃生口等於把逃生口開給每一條 sudo 呼叫。
3. **`create()` 不經 `write()`**：`super().create()` 不會走 model 的 `write()` override，所以「只在 `write()` 放 guard」對 create-time 注入完全無效。不可變欄位的守線**必須同時覆蓋 create（vals 注入）與 write（事後改值）**，缺一邊就是半套。
4. **context key 是對的逃生口**：它是 per-call 明示 opt-in（`record.with_context(sa_internal_identity_write=True).write({...})`），只有唯一合法的初始化 / 維修路徑會設它；不像 `env.su` 是一整類呼叫的隱式屬性。預設沒人設它 → 預設對所有人關閉 → 測試在 su env 下也能真正走到 deny。

## 守則（怎麼做）

1. **「沒有任何內部合法路徑需要改」的不變量** → guard **無條件擋（含 superuser）** + 留一把 context-key escape hatch（給未來 data-repair migration / 唯一初始化路徑）。不要用 `env.su` / `has_group` 豁免。
2. **不可變欄位的 guard 必須同時覆蓋 create 與 write**：`write()` 擋事後改值；`create()` override pop 掉 caller 傳入的 identity key（因為 `super().create()` 不經 `write()`）。兩個方向用**同一個** escape-hatch 條件，對稱關閉。
3. **guard 的測試在 su env 下要能真正走到 deny 分支**：在 `TransactionCase`（su env）直接 `write` / `create` 注入，斷言 `UserError` / 值被剝離。若這條測試在你「拿掉 guard body」後**仍綠**，代表它根本沒打到 guard —— 那是假綠，不是守線。

## 證據（commit / 檔案:行號）

- `5088a31` `fix: enforce route prefix immutability for superuser env` —— route prefix guard 從 `not self.env.su` 改 context key。現況：`src/models/security_access.py:842`（`SecurityAccessRoute.write`）、`:849`（`sa_internal_prefix_write` 條件）。暴露點 `src/tests/test_security_access.py:377` `test_route_prefix_immutable`（963 tests 唯一紅）。
- `a8f1ba7` `fix: strip injectable identity anchors at security access create time` —— `create()` override pop `canonical_name` / `reference_id`，補 create 方向。
- `16fd427` `fix: close su-env false-green in security access identity guard` —— write guard + create strip 一起改 `sa_internal_identity_write` context key、拿掉 `env.su` 豁免、補 su-env 測試。現況：`src/models/security_access.py:181`（`create`）、`:192`（create strip 條件）、`:205`（初始 canonical_name 寫入用 `with_context(sa_internal_identity_write=True)`）、`:211`（`write`）、`:219`（write guard 條件）。
- su-env 守線測試：`src/tests/test_security_access.py:42` `test_canonical_name_write_guard_fires_under_su`、`:54` `test_canonical_name_injection_stripped_under_su`；cross-user 負向：`:762` / `:775` create 注入剝離、`:786` write deny。

## 偵測 / 測試建議

- **grep 自查**：在 `create()` / `write()` override 的不可變欄位 guard 裡搜 `not self.env.su` / `has_group('base.group_system')`。命中時問自己「這個不變量有合法的內部改值路徑嗎？」沒有 → 該換 context key；有但只給 admin → 確認那是 [[scope-flip-write-guard-not-ir-rule]] 場景才保留豁免。
- **guard-removal 反測**：對守 invariant 的測試，暫時把 guard body 註解掉重跑 —— 若測試**仍綠**，它沒打到 guard（su-env 假綠 / 沒退出 su），不是有效守線。
- **create + write 雙向覆蓋**：每條 identity / 不可變欄位都要有「`write()` 改值被擋」**與**「`create()` 注入被剝離 / 被擋」兩條測試，且至少一條跑在 su env（`TransactionCase` 直接 create/write，不 `with_user`）證明 guard 對 superuser 也 fire。

## See also

- 同族不同層：[[scope-flip-write-guard-not-ir-rule]] —— 那條講「scope / tenancy 不可變必須靠 model `write()` guard 而非 ir.rule」（**用 write guard** 這層）；本條講「write / create guard **本身的豁免條件**怎麼寫才測得到、才不被 sudo 繞過」（guard 的**條件**這層）。關鍵分野：scope-flip 的不變量是「值轉移 gated」（有合法改值路徑，`env.su` + admin 放行**是對的**）；本條的不變量是「值不可變」（**沒有**合法改值路徑，任何 su 豁免都是洞 + 假綠）。改 identity guard 前先分清自己在哪一邊。
- 同主旨（驗綠範圍 / 證據不對 → 漏抓）：[[orm-vs-endpoint-acl-test-coverage]]、pitfall `sandbox-module-test-false-green.md`（0 tests 也報綠）—— 三者共通：**測試「綠」不等於守線存在，要看它有沒有真的走到該守的分支**。
- 原始來源（合成材料、不複製）：commit `5088a31` / `a8f1ba7` / `16fd427` + plan `docs/superpowers/plans/2026-06-06-security-access-p1-mainline.md`。
