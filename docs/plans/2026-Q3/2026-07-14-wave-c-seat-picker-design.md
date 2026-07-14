# Wave C：AC 席數三步選擇器（design）

> 狀態：拍板完成（2026-07-14 Eugene 三題拍板）；來源盤點＝Sonar《Wave C 三步 plan 選擇器現狀盤點》（wave-c-picker-audit-sonar.md，全 file:line 坐實）。
> 歸檔位置：`docs/plans/2026-Q3/2026-07-14-wave-c-seat-picker-design.md`（隨 PR-C1 落地）。

## Goal

251 plans 中 208 個 AC 席數變體（全 `storefront_hidden=True`）目前前台完全不可購。Wave C 補上呈現層：

1. **新購三步**：選方案（base plan 卡片）→ 選 AC 席數（變體）→ 下單（送最終 plan_id）。
2. **既有服務改席數／改方案**：三 surface 的 change-plan 前端 UI（現況完全沒有 UI，service client 已存在）。

## 拍板決策（2026-07-14）

| # | 題 | 拍板 |
|---|---|---|
| 1 | base↔變體對映承載 | **xmlid 反查**（`ir.model.data`）——零 schema 變動、零 re-seed；綁 generator xmlid 慣例 `{base}_ac{n}_{cycle}` |
| 2 | 變體取得路徑 | **擴充現有三個 `*_template_plans` 端點**：base plan dict 附 `variants: [{seats, plan_id, price}]`；**`storefront_hidden` 不翻**（變體永不作為獨立卡片；Wave B「完成才翻開」註記以此路徑達成原意圖=前台可購，flag 字面不翻） |
| 3 | scope | **新購三步＋改席數 UI 一起做**（同一 wave、可分 PR） |

## 事實基礎（摘自盤點，實作前不需重查）

- **後端下單已相容變體**：三 surface create gate＝`plan not in template.subscription_plan_ids`，變體已 M2M link → 放行（`paas.py:2099`／`security_access.py:426`／`smart_home.py:474`）。change-plan 4 入口同 gate 語意（#342 查證），變體同過。
- **換 plan 不觸發 redeploy**（#342 code 層定案）——變體 `cs_*`＝同 tier base（生成測試斷言），改席數只改計費：**語意上正確**，無部署副作用。
- **三 surface 共用單一 `PlanSelector`**（`src/static/src/paas/components/subscription/PlanSelector.js`，受控卡片列、emit `plan.id`）——改一處覆蓋三面。
- **席數範圍依 tier**：odoo 1-3/1-7/1-11、n8n 1/1-3/1-6/1-10、SA/SH 全 1-10、mcp 固定 1（退化為單選）。**前端不硬編**：席數選項＝該 base 的 `variants[].seats` 實際集合，tier 差異自然解決。
- **價格為 manifest 絕對值**（非 base+600×n runtime 公式）——UI 直接顯示變體全價，零價格運算。
- **cycle 天然分離**：monthly/yearly base 是兩筆 plan、各有自己的變體集（`_ac{n}_monthly`／`_ac{n}_yearly`）——variants 掛各自 base 下，三步不需處理 cycle 維度。
- **7 個 template 有變體**：odoo(42)/n8n(40)/mcp×3(各2)/SH(40)/SA(80)＝208；Hermes、postgresql 無變體（variants 為空陣列即可，前端 fallback 單步）。

## 架構

### 後端（PR-C1）

**共用 helper**（放 `sh_subscription_plan_visibility.py` 旁或 mixin）：

```
_attach_variants(template) -> {base_plan_id: [{seats, plan_id, price}, ...]}
```

1. 取 `template.subscription_plan_ids` 中 hidden 變體（`ac_max_access_seats>0 ∧ storefront_hidden`）。
2. 批次反查 xmlid（`ir.model.data` 一次 search 全部變體，禁逐筆 N+1）。
3. parse `_ac{n}_{cycle}` → 重組 base xmlid → 批次 resolve base plan id。
4. group by base、按 seats 排序。

**fail-loud 邊界**：hidden 變體 xmlid 不匹配 `_ac{n}_` 慣例 → `_logger.error`＋跳過該筆（端點不炸——選購可用性優先），同時新增守衛測試在 CI 層 fail-loud（全庫斷言每筆 hidden 變體都能反解出存在的 base）。運行時寬容＋測試時嚴格，兩層分工。

**三端點擴充**（`paas.py:931`／`security_access.py:228`／`smart_home.py:288`）：現有 `filtered(not storefront_hidden)` 迴圈保持，每個 base dict 加 `variants`（無變體＝`[]`）。dict 內容：`{seats, plan_id, price}`（＋SA 端點循例帶 quota 欄位若前端需要；第一版 seats/plan_id/price 足夠）。

**change-plan 後端**：不改（gate 已放行變體；#342 的 re-apply 議題與席數變體無關——同規格）。

### 前端（PR-C2 新購、PR-C3 改席數）

**`PlanSelector` 擴充**（單點改動覆蓋三 surface）：

- props 契約：plans 陣列元素多 `variants` 欄位；`onSelect` 語意不變（emit 最終 plan_id——base 或變體）。
- 內部：選中卡片若 `variants.length>0` → 卡片內/下方展開「AC 席數」第二步（segmented control 或 stepper，選項=variants 的 seats 集合＋「0（不加購）」＝base 自身）；選 0 → emit base id；選 n → emit 對應變體 plan_id、價格顯示同步切換。
- mcp 退化：variants 僅 1 席 → 呈現為單一「+1 access seat」附加選項（同一機制、無特例分支）。
- 無 variants（Hermes/postgresql/HA PaaS 0 元…）→ 行為與現狀 bit-for-bit 相同。

**三 surface create 頁**：payload 不變（`plan_id` 單一整數）——收斂全部發生在 PlanSelector 內部，`AppConfigurationPage`／`CreateSecurityAccessModal`／`CreateSmartHomeModal` 僅隨 props 透傳 variants。

**改席數／改方案 UI（PR-C3）**：

- 三 surface 的 service detail 面加「變更方案」入口 → `ChangePlanModal`（新元件，內嵌複用擴充後的 PlanSelector）→ 確認 → 呼叫既有 `changePlan(planId)` client（CS/SA 已存在；SH client 存在性=實作前查證點 V1）。
- modal 顯示：目前 plan（含席數）→ 新選擇 → 通用告知文案「按未使用比例退點後以新方案重新計費」（精確退款預覽 API 存在性=查證點 V2；有就顯示數字、沒有第一版用通用文案，不擋 wave）。
- `#342` 附帶發現的 `allocated_*` stale 屬後端 re-apply 議題，不在本 wave scope（變體同規格、顯示值不變，無 UX 矛盾）。

### 守衛與測試

- `test_variants_never_reach_storefront` 語意不變（變體不出現於頂層可見清單）。
- 新增後端測試：variants 欄位正確性（每 base 的 variants 數＝該 tier 席數上限×該 cycle、seats 排序、plan_id 反查正確、price 與 seed 值一致）＋xmlid 反解全庫斷言（fail-loud 測試層）。
- 前端 E2E（headed，playwright-cli）：三 surface 選變體下單、mcp 單席、無變體 fallback、改席數 modal 全流程。
- 版號：PR-C1 零 seed 改動仍 bump（asset/code 慣例）；取號依版號隊列（開 PR 前 re-check open PRs，暫定 .64 起）。

## 風險與緩解

| 風險 | 緩解 |
|---|---|
| xmlid 慣例漂移（未來 generator 改名） | 全庫反解斷言測試釘死慣例；generator doc 已載明改價 SOP 須重跑 generator |
| N+1 xmlid 反查拖慢 plans 端點 | helper 批次 search `ir.model.data`（單次 in-query） |
| PlanSelector 契約變動波及三 surface | 受控元件、`onSelect` 契約不變；無 variants 時行為 bit-for-bit 相同（既有 surface 零感知） |
| 改席數退款金額與使用者預期落差 | modal 明示退點規則文案；精確預覽列為 V2 查證點，有 API 就上數字 |

## 實作前查證點（小、隨 PR-C3 開工做）

- V1：SH change-plan 前端 client 是否存在（`workspace_service.js` grep changePlan/change-plan）。
- V2：退款預覽可用 API（`_compute_unused_refund` 是否已有 controller 曝露）。
