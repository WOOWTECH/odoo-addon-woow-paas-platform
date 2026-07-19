# Pattern: hidden template 全鏈呈現層過濾（is_hidden SSOT）

> 適用：新增或修改 `/woow` 的 controllers **曝光點**（list 端點、by-id 端點、dashboard 計數、訂閱/發票清單）時必看。平台自動開通的隱藏服務（如 0 元 HA PaaS，#336）以 `template.is_hidden` 為 SSOT，靠 **DRY helper 過濾整條呈現鏈**——template → service → subscription → invoice → workspace。核心定性：這是 **/woow 呈現層過濾，不是 ACL**；新開一個曝光點卻忘了接 helper，hidden 鏈就從那個點漏出來。

## 機制（SSOT ＋ DRY helper 鏈）

單一開關在 template 層：`is_hidden`（`src/models/cloud_app_template.py:221`，help 文字即完整語意宣告）。下游 service / subscription / invoice / workspace **不各自存 hidden flag**，全部由 mixin helper 從 template 推導（`src/controllers/ha_paas.py:61-82`）：

| Helper | 推導方式 |
| --- | --- |
| `_hidden_template_ids(env)` | `search([('is_hidden','=',True)])` |
| `_hidden_service_ids(env)` | service.template_id ∈ hidden templates |
| `_hidden_subscription_ids(env)` | hidden service 的 `subscription_id` 攤平（state-agnostic，含 cancelled——歷史 0 元發票也不可漏進 billing 歷史） |
| `_hidden_workspace_ids(env)` | hidden service 的 `workspace_id`（靠 D2 不變式成立，見下） |

兩個 helper 設計要點（`ha_paas.py:40-58` docstring）：

1. **sudo justification**：helper 讀的是「哪些 record 屬 hidden 鏈」這個平台級全域事實，結果只用於 `('id','not in', hidden_ids)` 式**排除過濾**（subtractive）、從不把跨租戶 row 回給 caller——不是 anti-pattern 的 sudo-list-and-return。
2. **helper 一律收呼叫端傳入的 `env`**，不在 mixin 內引用 module-level `request`——否則綁定成 `ha_paas.request`，既有單測 patch 各 controller 自己模組的 `request`（`paas.request` 等）會 patch 不到。

## 兩類曝光點、兩種寫法

**List 端點 → domain 排除**（漏接＝hidden 直接出現在清單）：

- Marketplace templates：`src/controllers/paas.py:799` `domain = [..., ('is_hidden','=',False)]`
- Cloud services list：`paas.py:2164`、`:2179` `('template_id','not in', self._hidden_template_ids(...))`
- 訂閱清單：`src/controllers/billing.py:790`、`:918`
- 發票清單：`billing.py:1004-1005`（`('sh_subscription_id','not in', hidden_sub_ids)`——`not in` 對 NULL 為真 → topup 發票不受影響）
- Dashboard：`src/controllers/dashboard.py:106-110`（primary sub）、`:190-193`（workspace 剔除）、`:202-211`（服務計數 defense-in-depth：即使某 hidden service 意外不住 hidden workspace 也不計入）

**By-id 端點 → 先 `exists()` 再補 hidden guard，回 not found**（Q2 拍板：閉合最完整、代價低）：

```python
# paas.py:839（template by-id；:926、:964、:1070、:2219 等同型）
if not template.exists() or not template.is_active or template.is_hidden:
    return {...not found...}
```

- service 側同型：`paas.py:964`、`:1070`、`:1093`、`:1189` … `service.template_id.is_hidden` → not found
- billing 側：`billing.py:824`（sub by-id）、`:1063`（invoice by-id）
- generic create 也拒收 hidden template（`paas.py:2218-2219`）——hidden service 只能經 provision 端點建。

## 定性邊界（⚠ 三條，誤判即出事）

1. **非 ACL**：租戶知 id 仍可構造 JSON request；guard 只是回 not found 的呈現層閉合，不是權限模型。真正的 ACL 仍由 ir.model.access / record rule 承擔。
2. **Odoo 原生 `/my` portal 與 `/report` PDF 完全繞過此層**（`cloud_app_template.py:224-227` help 明文；prod 留意）。本 repo 方針是「儘量不使用 Odoo 原生網頁」，但原生路由存在即繞過。
3. **0 元發票走 filter 路線、不做源頭抑制**（Q3 拍板）：billing cron 每期照建 0 元 `out_invoice`（真記錄），只在 /woow 清單過濾——**不動 #324 計費路徑**。源頭抑制需先證明「cron 推進 next_payment 不依賴發票 posted 狀態」，未證明前不做。

另注意同名異物：plan 層另有 `storefront_hidden`（`sh_subscription_plan_visibility.py:28`，過濾前台 plans，如 `paas.py:936-938`）——那是 **plan 可見性**，與 template 層 `is_hidden` 是兩條獨立軸，勿混用。

## D2 不變式（`_hidden_workspace_ids` 的成立前提）

**hidden-template service 只住 provision 當下自建的專屬 hidden workspace，且該 workspace 不含其他 service。** 由 provision code 保證：`ha_paas.py:254-273` `_create_ha_paas_service` 每次 provision 先 `workspace.create({'name':'HA PaaS','owner_id':user.id})` 再建唯一一個 service——workspace 是 fresh 專屬的，不重用租戶既有 workspace。`_hidden_workspace_ids` 直接取 hidden service 的 `workspace_id`（`ha_paas.py:77-82`），若有人改 provision 讓 hidden service 落進共用 workspace，dashboard 的 workspace 剔除就會**連坐隱藏租戶正常 workspace**——改 provision 流程前先對照這條不變式。

## Checklist（新增/修改曝光點時）

- [ ] list 端點：domain 接 `_hidden_template_ids` / `_hidden_subscription_ids` / `_hidden_workspace_ids` 排除？
- [ ] by-id 端點：`exists()` 之後補 `is_hidden`（或 `template_id.is_hidden` / hidden sub membership）→ 回 not found？
- [ ] helper 用 mixin 現成的，收 `env` 參數、不自己重寫 search（DRY——hidden 語意變更只改一處）？
- [ ] 若涉及發票：確認走 filter、沒有動到 #324 計費路徑？
- [ ] 若涉及 provision / workspace 歸屬：D2 不變式仍成立？

## 證據（file:line）

- SSOT 欄位：`src/models/cloud_app_template.py:221`（help 含定性宣告 `:224-227`）
- DRY helper mixin＋設計註解：`src/controllers/ha_paas.py:40-82`
- List 過濾：`src/controllers/paas.py:799`、`:2164`、`:2179`；`src/controllers/billing.py:790`、`:918`、`:1004-1005`；`src/controllers/dashboard.py:106-110`、`:190-211`
- By-id guard：`paas.py:839`、`:926`、`:964`、`:1070`、`:2218-2219`；`billing.py:824`、`:1063`
- D2 provision 保證：`src/controllers/ha_paas.py:237-277`
- 拍板來源：`docs/plans/2026-Q3/2026-07-12-336-ha-paas-provision.md`（D4/D2 決策段＋Open Questions Q2/Q3，2026-07-12 Eugene 拍板）

## See also

- [[orm-vs-endpoint-acl-test-coverage]] — helper `.sudo()` 的 justification 框架（subtractive filter 非 sudo-list-and-return）；repo-lint baseline 已納入。
- [[template-vs-plan-property-boundary]] — 何時開新 template（HA PaaS 獨立 template＋`is_hidden=True` 的上游決策）。
- 原始來源：`docs/plans/2026-Q3/2026-07-12-336-ha-paas-provision.md` D4/Q2/Q3/D2 段。
