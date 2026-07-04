# Finding: project↔CloudService 綁定授權只在 controller 層 gated、model 層無守（defense-in-depth 缺口）

> 來源：#280/#279 租戶網路隔離 spec 過程，docs-leader 附帶請查「`cloud_service_ids` 寫入是否 ACL-gated 到成員 workspace」（2026-07-04）。**非即刻可利用洞**（主路徑已 gated），但屬 `constraint-input-trust-expansion` / `two-layer-acl-lockstep` family 的 defense-in-depth 缺口，值得 follow-up。**追蹤 issue：[#289](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/289)。**
>
> Pattern：[`constraint-input-trust-expansion`](../../decisions/patterns/constraint-input-trust-expansion.md)、[`two-layer-acl-lockstep`](../../decisions/patterns/two-layer-acl-lockstep.md)、[`orm-vs-endpoint-acl-test-coverage`](../../decisions/patterns/orm-vs-endpoint-acl-test-coverage.md)

## 事實

`project.workspace_id(s)` 從 `cloud_service_ids` 綁定推導（`project_project.py:141-149`：`workspace_ids = cloud_service_ids.mapped('workspace_id')`）。綁定授權目前分佈：

| 層 | 現況 |
| --- | --- |
| **controller create**（`ai_assistant.py:1523 _create_project` via `api_support_projects:1300`）| ✅ gated：`1329 sudo().browse(cloud_service_id)`（繞 CS ir.rule）緊接 **`1332 _check_workspace_access(cloud_service.workspace_id)`** → 驗寫入者是該 CS workspace 成員才綁 |
| **controller update**（`_update_project:1578`）| `vals` 只收 name/description、**不 rebind CS** → 無 gap（也無 rebind UX）|
| **model**（`project.cloud_service_ids`）| ❌ **無 `@api.constrains` / `write()` guard**（只有 compute）；`cloud_service.py` 亦無 workspace 一致性 guard |
| **ir.model.access.csv** | project M2M 對 `base.group_user` 全開（授權靠 ir.rule + controller，非 model guard）|

## 風險

授權是 **controller-only**、無 model-level defense-in-depth：任何**其他/未來寫 `cloud_service_ids` 的路徑**（新 endpoint、data import、`.sudo()` 內部流程、admin action、XML-RPC）**沒複製 `_check_workspace_access` 就開跨租戶綁定洞**。諷刺：`_update_project:1583` 註解自標「two-layer-acl-lockstep invariant #4」守 name/description editable gate，卻**沒有對等 model-level 守綁定授權本身**——正是該 pattern 警告的「單層 ACL、他層 silent divergence」。

一旦跨 workspace 綁定成立，`project.workspace_ids` 變多值 → agent 的 `egressWorkspaceIds`（leg-1 NP）跨 workspace 放行 → **NP 租戶隔離被資料層繞過**（NP 正確、但輸入不可信）。

## stg 現況（2026-07-04 雙查收斂）

12 agent project **全部 `cloud_service_ids=[]`、0 跨-workspace**（leader `multi_ws=0/12` + docs-leader）。→ 跨-ws 綁定**零使用** → 加 constraint **零回歸**。

## 建議

加 model-level **`@api.constrains('cloud_service_ids')`**（defense-in-depth，authority check）：

**✅ Eugene 拍板 Model B（允許跨 workspace 綁定，保留產品功能）**（2026-07-04）→ constraint **只做 authority check、不加 same-workspace 限制**：綁的**每個** CS 的 workspace 都必須有寫入者為成員（`workspace_access` 查核）。跨 workspace 綁定合法，前提是寫入者是**每個**目標 workspace 的成員。**不可**用 `len(workspace_ids) <= 1`（會砍「support project 同時達多 workspace CS」產品功能）。

> 早期曾因 stg 零使用傾向 Model A（same-workspace constraint），Eugene 2026-07-04 否決：support project 須能同時達多 workspace 的 CS。故 leg-2 走 per-reachable-workspace label `woow.tw/reach-ws-<W>`（見 #279 spec §7）。

## 驗證/測試要求

- backend `TestCase` + `with_user()`：非成員 user 綁跨 workspace CS → `ValidationError`（不可只測 controller，須測 model constraint 本身，避免 [[orm-vs-endpoint-acl-test-coverage]] 假綠）。
- Model B：寫入者為成員的跨 workspace 綁定**放行**；綁到寫入者**非成員** workspace 的 CS → `ValidationError` fail-loud。
