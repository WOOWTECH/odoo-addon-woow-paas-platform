# Pattern: hidden template 全鏈呈現層過濾（曝光層 is_hidden ／ 實例層 conceal_instances）

> 適用：新增或修改 `/woow` 的 controllers **曝光點**（list 端點、by-id 端點、dashboard 計數、訂閱/發票清單）**或既有服務的操作端點**時必看。平台自動開通的隱藏服務（如 0 元 HA PaaS，#336）以 `template.conceal_instances` 為 SSOT，靠 **DRY helper 過濾整條呈現鏈**——template → service → subscription → invoice → workspace。核心定性：這是 **/woow 呈現層過濾，不是 ACL**；新開一個曝光點卻忘了接 helper，hidden 鏈就從那個點漏出來。

## ⚠️ 兩層語意（#471，2026-07-30 拍板）——先確認你要擋的是哪一層

原本 template 層只有 `is_hidden` 一個旗標，**同時**承載兩件事：「不上架」與「既有實例整條鏈消失」。對 HA PaaS 恰好無害（#336 決議該服務的租戶入口是 App、不是 /woow），套到「暫時下架的一般商品」就變成破壞性的——租戶手上 `state=running` 的服務詳情頁打不開、rollback／change-plan／MCP 設定全回 `Service not found`（#464 實測 hermes-agent）。上架／下架是常態營運動作，不該帶這個副作用。故拆成兩軸：

| 語意 | 欄位 | 擋什麼 | 誰在用 |
| --- | --- | --- | --- |
| **曝光層** | `is_hidden` | marketplace 不列、template 詳情／plans 不可讀、generic create 拒收。**完全不動既有實例。** | HA PaaS（＋任何待定價／暫時下架的商品） |
| **實例層** | `conceal_instances` | 既有 service／訂閱／發票／workspace 全鏈過濾 ＋ 既有服務的**所有**讀取／操作端點回 not found | 僅 HA PaaS |

判斷法：**你要擋的對象是「還沒買的人」還是「已經買了的人」？** 前者是曝光層、後者是實例層。絕大多數上架／下架需求只需要曝光層。

> **hidden-only 目前沒有 seed 樣本**（#494：唯一的 hidden-only 商品 hermes-agent 已定價上架，解封時只翻 `is_hidden`、`conceal_instances` 自始未設——正是本表想要的形態）。因此該語意的行為矩陣（`test_conceal_instances_split_471.py` / `test_hidden_template_chain.py`）改用**合成 fixture**（`make_hidden_only_template`），不再綁任何商品：受測的是語意，不該隨商品上下架而生滅。下次有商品要暫時下架時，直接翻它的 `is_hidden` 即可，**不要**順手加 `conceal_instances`。

**蘊含式（model 層 fail-loud）**：`conceal_instances=True ⟹ is_hidden=True`（`cloud_app_template.py` `_check_conceal_implies_hidden`）。反例語意矛盾——marketplace 賣得到、買完立刻從前台消失且不可操作。這也擋掉「解封時只翻 is_hidden、留下 conceal_instances=True」的半套變更：解封 conceal template 必須**兩個欄位一起翻**。

**機械強制**：repo-lint `check_conceal_instances_gate` —— `src/**/*.py`（除 `src/tests/`）出現 `template_id.is_hidden`（＝經 service 取 template 再判可見性）即 fail。唯一的退化路徑就是「新增一個既有服務的操作端點時照抄舊 gate」，這條把它從「三個月後租戶回報服務不見了」提前成「寫下去當下就被擋」。曝光層的四處直接持有 `template` 變數、不經 `template_id`，故不被誤報。

## 機制（SSOT ＋ DRY helper 鏈）

實例層單一開關在 template 層：`conceal_instances`（`src/models/cloud_app_template.py`，help 文字即完整語意宣告）。下游 service / subscription / invoice / workspace **不各自存 hidden flag**，全部由 mixin helper 從 template 推導（`src/controllers/ha_paas.py`）：

| Helper | 推導方式 |
| --- | --- |
| `_hidden_template_ids(env)` | `search([('conceal_instances','=',True)])` |
| `_hidden_service_ids(env)` | service.template_id ∈ hidden templates |
| `_hidden_subscription_ids(env)` | hidden service 的 `subscription_id` 攤平（state-agnostic，含 cancelled——歷史 0 元發票也不可漏進 billing 歷史） |
| `_hidden_workspace_ids(env)` | hidden service 的 `workspace_id`（靠 D2 不變式成立，見下） |

兩個 helper 設計要點（`ha_paas.py:40-58` docstring）：

1. **sudo justification**：helper 讀的是「哪些 record 屬 hidden 鏈」這個平台級全域事實，結果只用於 `('id','not in', hidden_ids)` 式**排除過濾**（subtractive）、從不把跨租戶 row 回給 caller——不是 anti-pattern 的 sudo-list-and-return。
2. **helper 一律收呼叫端傳入的 `env`**，不在 mixin 內引用 module-level `request`——否則綁定成 `ha_paas.request`，既有單測 patch 各 controller 自己模組的 `request`（`paas.request` 等）會 patch 不到。

## 兩類曝光點、兩種寫法

**List 端點 → domain 排除**（漏接＝hidden 直接出現在清單）：

- Marketplace templates（**曝光層**，唯一不經 mixin 的 list）：`src/controllers/paas.py` `domain = [..., ('is_hidden','=',False)]`
- Cloud services list（實例層）：`paas.py` `_list_services` 兩處 `('template_id','not in', self._hidden_template_ids(...))`
- 訂閱清單：`src/controllers/billing.py:790`、`:918`
- 發票清單：`billing.py:1004-1005`（`('sh_subscription_id','not in', hidden_sub_ids)`——`not in` 對 NULL 為真 → topup 發票不受影響）
- Dashboard：`src/controllers/dashboard.py:106-110`（primary sub）、`:190-193`（workspace 剔除）、`:202-211`（服務計數 defense-in-depth：即使某 hidden service 意外不住 hidden workspace 也不計入）

**By-id 端點 → 先 `exists()` 再補 guard，回 not found**（Q2 拍板：閉合最完整、代價低）。**這裡是最容易挑錯欄位的地方**——判的是 template 本身還是某個既有 service？

```python
# 曝光層：直接持有 template 變數（template by-id / plans / generic create）
if not template.exists() or not template.is_active or template.is_hidden:
    return {...not found...}

# 實例層：經 service 取 template（既有服務的一切讀取／操作）
if (not service.exists() or service.workspace_id.id != workspace_id
        or service.template_id.conceal_instances):
    return {...not found...}
```

- 實例層 9 處（`paas.py`）：詳情／更新／刪除（三 action 共用一道 gate）、rollback、revisions、change-plan、skip-trial-estimate、skip-trial、check-addon-removal、mcp-servers、**reset-admin-password**
- billing 側（實例層，經 hidden sub membership）：`billing.py` sub by-id、invoice by-id
- 曝光層 4 處（`paas.py`）：marketplace list、template by-id、plans、generic create——hidden service 只能經 provision 端點建

**新增既有服務操作端點的鐵律**：gate 放在 `exists()` 之後、**能力／狀態判準之前**。`reset-admin-password` 修前把 `has_admin_gui` 檢查放在唯一位置且完全沒有隱藏 gate，於是 (a) conceal 服務會回「不支援 admin GUI」洩漏它存在，(b) 更糟的是「API 重設得了密碼、但唯一的 UI 入口（詳情頁）打不開」——同一個服務兩個端點對「它存不存在」給出相反答案（#471 C 組）。

## 定性邊界（⚠ 三條，誤判即出事）

1. **非 ACL**：租戶知 id 仍可構造 JSON request；guard 只是回 not found 的呈現層閉合，不是權限模型。真正的 ACL 仍由 ir.model.access / record rule 承擔。
2. **Odoo 原生 `/my` portal 與 `/report` PDF 完全繞過此層**（`cloud_app_template.py:224-227` help 明文；prod 留意）。本 repo 方針是「儘量不使用 Odoo 原生網頁」，但原生路由存在即繞過。
3. **0 元發票走 filter 路線、不做源頭抑制**（Q3 拍板）：billing cron 每期照建 0 元 `out_invoice`（真記錄），只在 /woow 清單過濾——**不動 #324 計費路徑**。源頭抑制需先證明「cron 推進 next_payment 不依賴發票 posted 狀態」，未證明前不做。

另注意同名異物：plan 層另有 `storefront_hidden`（`sh_subscription_plan_visibility.py:28`，過濾前台 plans）——那是 **plan 可見性**（擋購買選項），與 template 層的兩個旗標是第三條獨立軸，勿混用。命名上刻意避開在 template 層再叫 `storefront_hidden`（同名不同義正是本 repo 的既知陷阱類型）。三軸並存的分工：

- plan `storefront_hidden` → 這個**方案**不可選購
- template `is_hidden` → 這個**商品**不上架
- template `conceal_instances` → 這個商品的**既有實例**也不可見／不可操作
- **供給閘門**（#501，第四軸）→ 這個商品**照常上架、照常可看，只是現在買不到**

第四軸與前三者正交，且**不是欄位**：上限與停售是 `ir.config_parameter`（key 由 slug 推導），售罄則是**現算**（活躍實例數 vs 上限），對外只暴露 `_format_template` 的 `availability: {purchasable, reason}`。判準的唯一定義點是 `cloud_app_template._reject_reason_for_new_instance`，`CloudService.create()`／`write()`（權威閘門）與 marketplace payload（顯示層）都呼叫它。

**要「暫時不賣」時先分清楚要哪一軸**：翻 `is_hidden` 是**下架**（商品連同規格頁、plans 一起消失）；供給閘門是**顯示並標示「已售罄／暫停販售」**。#501 拍板限量商品走後者——誤翻 `is_hidden` 會讓想買的人連商品都找不到，誤翻 `conceal_instances` 更會鎖死已付費租戶手上 running 的服務。

## D2 不變式（`_hidden_workspace_ids` 的成立前提）

**hidden-template service 只住 provision 當下自建的專屬 hidden workspace，且該 workspace 不含其他 service。** 由 provision code 保證：`ha_paas.py:254-273` `_create_ha_paas_service` 每次 provision 先 `workspace.create({'name':'HA PaaS','owner_id':user.id})` 再建唯一一個 service——workspace 是 fresh 專屬的，不重用租戶既有 workspace。`_hidden_workspace_ids` 直接取 hidden service 的 `workspace_id`（`ha_paas.py:77-82`），若有人改 provision 讓 hidden service 落進共用 workspace，dashboard 的 workspace 剔除就會**連坐隱藏租戶正常 workspace**——改 provision 流程前先對照這條不變式。

## Checklist（新增/修改曝光點或既有服務端點時）

- [ ] **先分層**：擋的是「還沒買的人」（曝光層 `is_hidden`）還是「已經買了的人」（實例層 `conceal_instances`）？
- [ ] list 端點：domain 接 `_hidden_template_ids` / `_hidden_subscription_ids` / `_hidden_workspace_ids` 排除？（marketplace 例外——那是曝光層，直接判 `is_hidden`）
- [ ] by-id / 操作端點：`exists()` 之後補 guard → 回 not found？判準是 `template_id.conceal_instances`（實例層）而非 `template_id.is_hidden`（repo-lint 會攔）？
- [ ] gate 放在**能力／狀態判準之前**？（否則洩漏存在性，且與詳情頁答案不一致）
- [ ] helper 用 mixin 現成的，收 `env` 參數、不自己重寫 search（DRY——語意變更只改一處）？
- [ ] 若涉及發票：確認走 filter、沒有動到 #324 計費路徑？
- [ ] 若涉及 provision / workspace 歸屬：D2 不變式仍成立？
- [ ] 新增 hidden template 時：`test_seed_inventory_is_exactly_these_two` 會紅——停下來決定它屬哪一層，再更新該斷言。

## 證據（file:line）

- 兩個 SSOT 欄位＋蘊含式 constraint：`src/models/cloud_app_template.py`（`is_hidden` / `conceal_instances` / `_check_conceal_implies_hidden`，help 含定性宣告）
- DRY helper mixin＋設計註解：`src/controllers/ha_paas.py` `HiddenChainMixin`
- List 過濾：`src/controllers/paas.py`（marketplace domain ＋ `_list_services` 兩處）；`src/controllers/billing.py:790`、`:918`、`:1004-1005`；`src/controllers/dashboard.py:106-110`、`:190-211`
- By-id / 操作 guard：`paas.py` 曝光層 4 處 ＋ 實例層 9 處；`billing.py:824`、`:1063`
- 機械 gate：`scripts/audit/repo-lint.py` `check_conceal_instances_gate`
- 行為矩陣測試：`src/tests/test_conceal_instances_split_471.py`（兩語意成對比對）、`src/tests/test_hidden_template_chain.py`（HA PaaS 零回歸 ＋ `TestHiddenChainSsotIsConcealInstances`）
- D2 provision 保證：`src/controllers/ha_paas.py` `_create_ha_paas_service`
- 拍板來源：`docs/plans/2026-Q3/2026-07-12-336-ha-paas-provision.md`（D4/D2 決策段＋Open Questions Q2/Q3，2026-07-12 Eugene 拍板）；兩語意拆分＝issue #471 comment（2026-07-30 Eugene 拍板方向 (b)）

## See also

- [[orm-vs-endpoint-acl-test-coverage]] — helper `.sudo()` 的 justification 框架（subtractive filter 非 sudo-list-and-return）；repo-lint baseline 已納入。
- [[template-vs-plan-property-boundary]] — 何時開新 template（HA PaaS 獨立 template＋`is_hidden=True` 的上游決策）。
- 原始來源：`docs/plans/2026-Q3/2026-07-12-336-ha-paas-provision.md` D4/Q2/Q3/D2 段。
