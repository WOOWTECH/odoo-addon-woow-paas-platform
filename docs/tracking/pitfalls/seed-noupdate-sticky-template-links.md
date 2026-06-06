# Pitfall: seed record 用 `noupdate="1"` → ir.model.data flag 是 sticky 的 → 後續 data 檔在 `-u` 對該 record 的更新被靜默跳過

> 來源：Security Access P1，2026-06-07 UI 驗證抓到。stg/sandbox（升級路徑）的 `/woow` Security Access 詳情頁，前端 `PlanSelector` 顯示「Template has no subscription plans configured」，但 fresh install 與單元測試全綠。根因：`security_access_default` seed 用了 `noupdate="1"`，Odoo 把 `ir.model.data.noupdate` flag **持久化（sticky）寫進 DB**，於是 `subscription_template_links.xml`（`noupdate=0`）對同一筆 record 的 M2M 寫入在每次 `-u` 都被悄悄略過 → `woow_security_access_template_plan_rel` 0 筆。
>
> 程式守衛（防再犯）：commit `ebe6ec1` ① seed 改 `noupdate="0"` 對齊鄰居慣例 ② links 檔加 `<function>` healer 翻正既有 DB 的 sticky flag。

## Symptom

- `/woow` → Security Access 詳情頁，`PlanSelector` 顯示 **「Template has no subscription plans configured」**；訂閱無法選方案。
- 只發生在**先裝後升**（`-i` 過、後續 `-u`）的 DB（sandbox / stg / prod 都走這條），fresh install 不重現。
- psql 查得 `ir_model_data.noupdate = 't'`（`module='woow_paas_platform'`、`name='security_access_default'`），且 `woow_security_access_template_plan_rel` 該 template 0 筆。
- 後端單元測試**全綠**——它們自建 template/plan，不走 seed 路徑（見下）。

## Why it happens — sticky noupdate + 跨檔更新

### 1. `noupdate` flag 一旦寫進 ir.model.data 就是 sticky 的

XML `<data noupdate="1">` 不只影響「本次 load」——Odoo 把 `noupdate=True` **寫進該 record 的 `ir.model.data` row**，並在後續每次升級時讀回。之後**任何** data 檔（即使自己是 `noupdate="0"`）對同一個 XML id 做 record-level 更新時，ORM 看到目標 record 的 `ir.model.data.noupdate=True` → **靜默 skip**，不報錯、不 log。

```
security_access_templates.xml  <data noupdate="1">  →  寫 ir_model_data(security_access_default).noupdate = t  (sticky)
subscription_template_links.xml <data noupdate="0">  →  對 security_access_default 寫 subscription_plan_ids M2M
                                                          ↑ 目標 record noupdate=t → skip → rel 0 筆
```

`noupdate` 是 record-level 屬性，不是「哪個檔在改」的屬性 —— 寫的人是誰、在哪個檔、用什麼 noupdate 都不重要，只看**被改的 record** 那筆 flag。

### 2. 偏離鄰居慣例埋雷

鄰居 `smart_home_templates.xml` / `cloud_app_templates.xml` 都用 `noupdate="0"`，且都有註解寫明意圖（「module updates reset templates to canonical default」「helm values/image tags stay current on upgrade」）。`security_access_templates.xml` 卻獨自用 `noupdate="1"` 且無理由註解 —— 與「會被 links 檔更新的 seed」直接衝突。

### 3. 為什麼測試全綠沒抓到 — fresh install 遮蔽 gap

- **單元測試**自建 template/plan（`create()`），完全不經過 seed 的 `ir.model.data`，sticky flag 不存在 → 永遠綠。
- **fresh install**：`-i` 走 install mode，**所有** data 檔一律套用（install 時 noupdate 不擋首次建立），M2M 在同一次 init 內被寫進去 → 看起來正常。
- 只有 **「先裝後升」的 DB** 中招：record 早在某次 install 用 sticky flag 建好，之後的 `-u` 才碰到「目標 record noupdate=t」的 skip。**這正是 stg / prod 的真實路徑**。

> 一句話守線：**fresh install 綠 ≠ 升級路徑綠**。seed 變更的驗證必須走 `-u` 升級一個既有 DB。

## 同 family 前例（同 PR）

同 PR 的 OAuth scope（`src/data/oauth_scope_updates.xml`，G-H3）撞過同一類問題的鏡像版：`oauth_clients.xml` 是 `noupdate="1"`，`post_init_hook` 又**只在 fresh install 跑** → 既有 DB 的 OAuth client 永遠拿不到新 scope。解法相同：用 `<function>`（`_ensure_ha_smart_home_sa_scopes`，每次 `-u` 必執行、不受 noupdate 管、idempotent）補既有 DB。**「seed/hook 只覆蓋 fresh install、既有 DB 用 `<function>` healer 補」是這兩處共用的 pattern。**

## 修法（commit `ebe6ec1`）

1. **seed 改 `noupdate="0"` 對齊慣例**（`src/data/security_access_templates.xml:3-8`）：加理由註解，與 smart_home / cloud_app 一致；module update 重置 template 為 canonical default。新 fresh install 不再寫 sticky flag。
2. **links 檔加 `<function>` healer**（`src/data/subscription_template_links.xml:41-48`）：
   - `<function>` 元素**每次 `-u` 必執行，不受目標 record 的 noupdate 管**——這是翻正既有 sticky DB 的唯一 in-band 手段。
   - healer = `security_access_template._ensure_default_template_links`（`src/models/security_access_template.py:53`）：
     - 找到 `security_access_default` 的 `ir.model.data` row，若 `noupdate=True` → 翻成 False（清掉歷史 sticky flag）。
     - **僅在零 plan 時**回填 4 個預設 plan（entry/pro × monthly/yearly）；template 已有 plan 就 `return`，**不覆蓋 admin 自選清單**。
     - idempotent，每次 `-u` 重跑安全。

## 證據

- 偏離點：`src/data/security_access_templates.xml`（修前 `<data noupdate="1">`）vs 鄰居 `src/data/smart_home_templates.xml:3` / `src/data/cloud_app_templates.xml:3`（皆 `noupdate="0"` + 意圖註解）。
- 跨檔更新點：`src/data/subscription_template_links.xml` 對 `security_access_default` 寫 `subscription_plan_ids`。
- 前例：`src/data/oauth_scope_updates.xml:3-13`（G-H3 `<function>` 同類修法）。
- manifest load order（三者皆在 data list）：`src/__manifest__.py:25`（templates）、`:28`（links）、`:63`（oauth scope）。
- 實證：psql `SELECT noupdate FROM ir_model_data WHERE module='woow_paas_platform' AND name='security_access_default';` → 修前 `t`；`-u` 跑過 healer 後翻為 `f`，且 `woow_security_access_template_plan_rel` 補回 4 筆。
- fix commit：`ebe6ec1`（"fix: heal sticky noupdate template links and owl component prop syntax"）。

## 守則（何時適用、怎麼做）

1. **會被其他 data 檔更新的 seed record，禁用 `noupdate="1"`**。若某筆 seed 之後會被 links / cross-file 更新碰到，必須 `noupdate="0"`，否則 sticky flag 讓所有後續更新靜默失效。確實需要保護「使用者已客製、升級別覆蓋」的欄位時，用 `<function>` healer 在 model 層做 conditional back-fill（如本案「僅零 plan 時回填」），而非整筆 `noupdate=1`。
2. **seed / data 變更的驗證一定要走 `-u` 升級既有 DB**，不能只看 fresh install 或單元測試。「fresh install 綠 ≠ 升級路徑綠」——install mode 會套用全部檔案、遮蔽 sticky / hook-only-on-install 這類只在升級路徑暴露的 gap。
3. **healing 既有 DB 用 `<function>`**（每次 `-u` 必執行、不受 noupdate 管、寫成 idempotent + admin-respecting）；`post_init_hook` 只覆蓋 fresh install，不能依賴它修既有 DB。

## 偵測 / 測試建議

- **migration / `-u` 測試**：建一個既有 DB（先 `-i` 一個舊版、再 `-u` 到含此 seed 的版本），斷言 `template.subscription_plan_ids` 非空、`ir.model.data` 的 `noupdate=False`。純 `create()` 的單元測試補不到這條路徑。
- **grep guard**：CI lint 掃 `src/data/*.xml` 中 `noupdate="1"` 的 record，比對是否有其他 data 檔（或 `<function>`）對同一 XML id 做更新 → 命中即 fail，要求加理由註解或改 `noupdate="0"`。
- **HttpCase / 前端煙霧測試**：升級路徑 DB 上打 PlanSelector 對應的 endpoint，斷言回傳 plan 列表非空——這正是本案最終在 UI 抓到的層級，ORM 層綠時它仍能擋。
- **psql 一鍵診斷**（既有 DB 排查）：
  ```sql
  SELECT noupdate FROM ir_model_data
   WHERE module='woow_paas_platform' AND name='security_access_default';
  -- 't' = sticky，後續 -u 對該 record 的更新都被略過
  ```
