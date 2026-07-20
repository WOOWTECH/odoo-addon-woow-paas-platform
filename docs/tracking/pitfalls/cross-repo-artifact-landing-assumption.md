# Pitfall: plan 假設 cross-repo artifact 落點，實際落本 repo（charts 落點）

> 來源：`agent-runtime-pod` epic Phase D（WS1 charts，落點修正 2026-05-24）。plan 原假設「Odoo K8s Secret env mount」這塊 chart 工作落 **cross-repo `woow-paas-charts`**（因為 agent-runtime **pod** 的 chart 確實在那），勘查後發現它其實屬 **本 repo `charts/odoo-dev-sandbox`**（dev-sandbox 的 Odoo deployment chart 在 app repo，不在 charts repo）。**impl 前的勘查 catch 到，省下跨 repo 改錯地方的成本。**

## Symptom

- plan / design 寫某個部署 artifact（chart / values / secret mount / manifest）「在 repo X」，動手時發現該 artifact 其實在 repo Y（或本 repo）。
- 典型訊號：照 plan 去 repo X 找對應檔案 **找不到**，或找到的是「相鄰但不同用途」的 chart（如本 epic：`woow-paas-charts` 有 agent-runtime **pod** 的 chart，但**沒有** dev-sandbox Odoo deployment 的 chart）。
- 若沒勘查直接動手：在 repo X 加了一份 chart 改動 → 部署時該 env **沒出現在真正跑 Odoo 的 deployment**（改錯 chart），或在 X 重造一份本該在 Y 既有的 chart。

## Why it happens

### 「同一系統的所有 K8s artifact 都在 charts repo」是錯的心智模型

cross-repo 系統的 chart 落點常按**部署單元歸屬**切，不是「全部塞 charts repo」：

| 部署單元 | chart 落點（本 epic）|
|----------|---------------------|
| agent-runtime **pod**（per-project runtime）| `woow-paas-charts/charts/agent-runtime/`（cross-repo，design §3.4 line 3256）|
| **dev-sandbox Odoo**（跑 addon 的 Odoo + PG + nginx）| **本 repo** `charts/odoo-dev-sandbox/`（CLAUDE.md K8s Dev Sandbox 章）|
| production Odoo CD | `woow-paas-cd`（另一 repo，留 Phase G）|

「要給 Odoo 注入 secret env」直覺聯想到「agent-runtime 的 secret 都在 woow-paas-charts」→ 但**注入對象是 Odoo deployment**，而 Odoo 的 dev-sandbox chart 在本 repo。**artifact 的落點由「它修改誰的 deployment」決定，不由「它屬於哪個 feature」決定。**

### plan 階段看不到 repo 邊界

planner 寫 plan 時對「哪個 chart 在哪個 repo」是按假設寫的；plan 的抽象層（「mount secret env 到 Odoo」）不強制 planner 去核對實體 chart 的 repo 歸屬 → 假設容易與實況偏。

## How to avoid

1. **impl 前勘查 artifact 的實體落點**（本 epic 做對的點）：動 chart / manifest / values 前，先 `ls` / grep 確認該 deployment 的 chart **實際在哪個 repo / 哪個路徑**，不照 plan 字面去找。一條 `helm template` 或 `ls charts/` 就能定位。
2. **按「修改誰的 deployment」定位 chart**，不按 feature 名：要加的 env 注入給 Odoo → 找 Odoo 的 deployment chart（本 repo `charts/odoo-dev-sandbox`），不是 agent-runtime feature 的 pod chart。
3. **plan 落點寫明 repo + 路徑**，並標「待勘查確認」：plan 提 artifact 時附 `<repo>/<path>` 而非只寫 feature 名；發現偏差就地修正 + 留 annotation（本 epic plan 留「落點修正 2026-05-24」）。
4. **dev/stg vs production chart 分清**：本 epic dev-sandbox（`charts/odoo-dev-sandbox`）先做，production Odoo CD（`woow-paas-cd`）留 Phase G —— 同一個 secret env 在不同環境的 chart 落點不同，別一次假設全部。

## Related

- 落地 case：`docs/archived/tracking/agent-runtime-pod-phase-d-enable-gate-2026-05-25.md` §takeaway；plan `docs/plans/2026-Q2/agent-runtime-pod-plan.md` Phase D WS1 charts 條（「落點修正 2026-05-24」+ Tester hand-off `⚡ charts | 本 repo charts/odoo-dev-sandbox`）；凍結 5 env 表。
- chart 落點事實：`CLAUDE.md` §K8s Dev Sandbox（`charts/odoo-dev-sandbox` 在本 repo）；design §3.4 line 3256（agent-runtime pod chart 在 `woow-paas-charts`）。
- 同 family（plan 假設 vs 實況，勘查/實作時才現形）：
  - [[plan-vs-odoo-schema-mismatch]] —— plan 引用不存在的 schema 欄位；本 pitfall 是 plan 假設錯 artifact 的 repo 落點。兩者都根於「plan 抽象層未核對實體事實」。
- 部署 invariant 連動：本 epic charts secretKeyRef 用 `optional:true` → partial 部署不在 deploy 時炸（見 [[follow-ups]] §Phase D enablement gate D-CAVEAT-1）。

## 補記（2026-07-06 自 CLAUDE.md 索引化搬運）

後續演變：本案落點 `charts/odoo-dev-sandbox` 於 2026-06 已隨 **woow-dev plugin** 移出（in-repo 副本移除）；現在動 dev-sandbox chart 一律改 plugin 的 `skills/k8s-dev-sandbox/charts/`。epic Phase E-G 續動 charts/CD 跨 repo 時本 pitfall 仍適用——按「修改誰的 deployment」定位 chart，非按 feature 名。
