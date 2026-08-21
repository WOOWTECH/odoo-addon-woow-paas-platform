# project 478 marketplace price label

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_478_marketplace_price_label`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#478 marketplace「Free」價格誤導修正收官（PR#498 merged 7df85991、.88）——根因是硬編字串非 fallback；起價判準須與 plans 端點同源；注入缺陷要打在使用者看得到的量上"

2026-07-31：marketplace 卡片與 configure header 一律顯示「Free」的價格誤導修正。
issue **#478** ／ PR **#498**（`fix/478-marketplace-price-label` → develop，`3070b8aa`，
版號 `.87`）。裁決＝**A 為主、B 為底**（列表 API 帶起價；查無價格不渲染標籤；真 0 元才 Free）。

**根因比 issue 推測更直接**：issue 猜「falsy → fallback 成 Free」，實查是 `AppCard.js`
的 getter **寫死 `return "Free"`** ＋ header 硬寫字面值，**同時** `_format_template`
從未回過價格欄位。兩件事疊加 ⇒ 只修一邊都不會好。**動手前實查根因**（而不是照 issue
的推測改）是這次省下白工的關鍵。

**最關鍵的設計判斷——可見性判準必須與前台 plans 端點同源**（`active` ＋ 非
`storefront_hidden`）。不同源會讓卡片標一個使用者點進去看不到的價格，那只是把「謊報
免費」換成「謊報數字」。兩個具體坑：
- `active` **顯式 filtered**，不倚賴 x2many 讀取是否套 `active_test`；stg 剛 archive 的
  0 元 plan 268 是現成反例（漏掉就把起價拉回 0，#478 原地復活）。
- `storefront_hidden` 藏著席數變體（open-design +1access 5100），計入會讓 `tier_count`
  憑空多一個看不到的價位（卡片會多出沒有依據的「起」字）。

**前端兩處共用 `utils/pricing_label.js`**：卡片與 header 各寫一份必漂移——#478 自己就是
「兩個判準」的產物，[[project_464_reset_button_visibility_e2e]] 的 F-449-4 是同型前例。

**撞 repo-lint controller-sudo gate（27→28）的處理**：沒有繞過，照 gate 要求舉證——
補 4 條 cross-user negative test（不同使用者起價相同／`pricing` 不含容器型值／隱藏價位
不外洩／hidden template 不曝光），update baseline 並把 justification 寫進 commit
message。**對照組要用非 admin 的 internal user 而不是 portal**：實測 portal 連
`cloud_app_template` 都讀不到（ACL 只開 Internal User），拿它當對照只會測到「它進不來」。

**測試**：RED 9/9（統一 `KeyError: 'pricing'`，順帶證明測試檔真的被跑到——#467 坑）；
全套 `1 failed / 0 error of 2098`（唯一紅＝#492 既有紅），2082→2098 正是新增的 16 條。

**UI 驗證一次掃全卡片**比逐項截圖有效率也更有說服力：Hermes Agent 4500（無「起」）／
n8n 750 起／Hermes MCP 1500 起／Open Design 4500（變體正確排除）／無 plan 不渲染／
掛 0 元後 Free。踩到的環境坑見
[[reference_odoo_assets_filestore_missing_blank_spa]]。

**Review 三條 finding（leader3，已修於 `ed7079ca`）**，最有價值的一條在第 1 項的**修法
本身也不夠**：
1. 「跨端點一致性」測試名實不符——docstring 說綁死兩端點，實際就地重寫一份過濾條件
   ＝**拿實作驗實作、恆綠**。改成實打 `api_template_plans` 後跑綠，但**注入式驗證
   （拿掉 pricing 的 storefront_hidden 過濾）證明它仍然綠**：席數變體比 base 貴 ⇒
   起價不變、**只有 `tier_count` 變**，而我第一版只比對起價。補上 `tier_count` 比對
   後同一注入立刻紅。**教訓：驗證守線的偵測力時，注入的缺陷要打在「使用者看得到的
   那個量」上，而不是最容易想到的那個量。**
   順帶修掉真分歧：pricing 只認 month/year 而 plans 端點不濾 unit ⇒ day/week 商品
   「卡片無標籤、選卡區有價目」。選擇對齊（月→年→其他），判準＝有價格就該顯示。
2. 又把商品目錄寫死成契約（n8n 750/4、opendesign tier_count 1…）——**我自己寫過
   [[feedback_test_pins_transient_fact_as_contract]] 卻再犯**。全改合成 fixture；真實
   資料只留兩條「不變式」掃描（付費不得標 Free、卡片價格與價位數須與 plans 端點一致）。
   特別坑：錨在 `storefront_hidden` 上的斷言——那是明文暫時狀態（Wave C picker 落地
   前），翻正時會在功能完全正確的情況下爆在一個講權限的測試名下，誤導性最高。
3. 曝光測試對任何 pricing 實作都不可能變紅（曝光由 controller domain 決定、在
   pricing 之前）⇒ 刪除，理由寫成註解避免被「補回」。
＋ 自行裁決：freemium（0 元入門＋付費階）只寫「Free」是 #478 的鏡像 ⇒ 改「Free 起」。

**被 leader3 推翻、刻意未改**：審查員宣稱「pricing 濾 active、plans 端點沒濾 ⇒ 兩側
分歧」。實查 `fields.py` 的 `Many2many.convert_to_record_multi` 在 `active_test` 預設
True 時就已 filtered(active) ⇒ 情境不成立，顯式 `filtered('active')` 是縱深冗餘。

**前端純函式的划算驗法**：`formatTemplatePrice` 無依賴，複製成 `.mjs` 用 node 跑八種
分支比對，比重建 web 容器＋清 assets 快一個數量級。

**Rebase 收尾（PR 因 #499／#500 併入而 conflicting）**：head `4810dcc0`、base `a088f7b3`、
版號順延 `.88`（`.87` 被 #500 用掉；`.87` migration 對停在 `.86` 的 DB 照跑——直接套用
[[reference_odoo_migration_version_window]] 的視窗語意，沒重查）。唯一衝突＝
`src/tests/__init__.py` 兩邊各 append 一行 ⇒ **兩行都留、依時序排**。
- **基準會變，別沿用舊的**：#499 修掉 #492 那條長期既有紅後，全套從「1F 扣掉已知紅」
  變成 **0F/0E**（本次 2130）。沿用舊基準會讓真回歸被當成「那條已知紅」放過。
- **rebase 後要回頭掃 commit message 裡的陳舊數字**：GREEN 的「1 failed of 2098」與
  「repo-lint 10 條」在新 base 都不成立，留著會讓後人以為這 PR 帶著一條紅合併。
  改寫手法＝`format-patch` 保存尾段 → `reset` → `amend` → `am` 重放。

**收官**：Eugene 2026-07-31 merge（develop `7df85991`、版號 `.88`、`Closes #478`）。
stg 升 .88 ＋ UI 閉環驗證（卡片顯示真實起價）由 hermes-plan 執行中。

**Gitea 陷阱（本次新踩）**：force-push 後 `tea pulls <n>` 仍顯示 `Conflicting files`，
但 `git merge-tree` 判定可乾淨合併——**以 `tea api /repos/<org>/<repo>/pulls/<n>` 的
`mergeable` 欄位為準**（實測 API 回 `mergeable: True` 而 tea 視圖是舊快取）。同理
merge 後立刻 `git fetch` 可能還沒看到（我就早了一步誤判「沒 merge 成功」）——遠端
狀態要以再 fetch 一次或 API 為準，不要用單次 fetch 的結果推翻使用者的陳述。

**stg 收尾閉環（2026-07-31，`.86`→`.88`）**：image `20260731.0723@sha256:69744696…`，
CI digest／pod imageID／pod 內 manifest／DB 版號**四方一致**。recipe
（[[reference_stg_upgrade_recipe_dev_sandbox]]）原樣套用零意外，upgrade job 同樣 39s、
log 0 error。**#478 的閉環證據**：marketplace 12 張卡片全顯真實價、**全頁 Free 次數 0**
（Hermes Agent／HAOS／Open Design 4500 無「起」、n8n 750 起、Hermes MCP／Odoo 1500 起、
MCP 系列 3000），configure header 與卡片一致；對照組是上次同一頁全部寫 Free 的截圖。
svc41/43/50/60 全 running 零回歸。

**順帶第一次在真環境看到 migration 視窗生效**：跳版 `.86`→`.88` 時，#500 建的
`18.0.1.0.87/post-migrate` **照跑**（log 有 `Running migration [18.0.1.0.87>]`），因為
`installed .86 < 目錄 .87 <= manifest .88`——正是 [[reference_odoo_migration_version_window]]
的結論。它回報 `no stuck suspended service — nothing to converge`，DB 側交叉印證 stg
4 個服務全 running、零 suspended、無殘留記號。

相關：[[project_494_hermes_pricing_landing]]（#478 症狀是在該次 stg 收尾時現形並取得
「Hermes MCP 也顯示 Free ⇒ 普遍症狀非回歸」的佐證）
