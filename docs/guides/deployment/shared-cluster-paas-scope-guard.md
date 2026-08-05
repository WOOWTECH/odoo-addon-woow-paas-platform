# feedback shared cluster paas scope guard

> 自 Claude memory 遷移（2026-08-05）。原 slug `feedback_shared_cluster_paas_scope_guard`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：Eugene 鐵律：woow-k3s 是共用叢集——平台側任何盤點/清理/修改只能動 PaaS 前綴資源，其他 ns 的 PVC/PV/volume/資源絕不碰

**Eugene（2026-07-08）：「我們是處理 paas 平台產生的 PVC/PVC 喔，因為該平台（叢集）也有別的 ns 的 pvc/pv 的使用，你不要動到別人的東西。這很重要。」**

**Why**：woow-k3s 同時承載 PaaS 平台**與**大量平台外部署（客戶 odoo 實例 mujimed-odoo/lisaaccounting-odoo/changching-odoo…、demo woowhademo/ha-demo、hermes 系、gitea、torchmedia…）。實例：2026-07-08 盤點 48 顆 Released PV 時初判「全是租戶/sandbox 拆除殘留」，歸屬實查後**僅 1+6+2 顆屬平台側、~39 顆是別人的**——若 SOP 沒加歸屬過濾就會刪掉其他部署的資料還原窗口。cluster-wide 掃描的「總數」永遠混著別人的東西。

**How to apply**（權威 namespace 清單，2026-07-08 Eugene 拍板並已寫入 **project CLAUDE.md「kubectl」段**）：

kubectl 在 woow-k3s context **只能操作**：`paas-ws-*`（prod 租戶）｜`paas-ws-stg-*`（stg 租戶）｜`paas-system`（prod 平台系統）｜`paas-stg-system`（stg 平台系統）｜`paas-cloudflare-system`（prod CF tunnel/ingress）｜`paas-stg-cloudflare-system`（stg CF tunnel/ingress）｜`dev-sandbox-*`（開發 sandbox）。**其餘 namespace 絕對不碰**。

**適用範圍（2026-07-08 Eugene 補充）**：此鐵律綁「**paas 平台的開發/維運**」事務（woow_paas_platform repo 及其派出的 agent/workflow）。`woow-machines-operator` agent（/Users/eugene/cmux/woow-machines-operator，prod/stg 主機全域維運）**必須能完整操作所有 ns**、不受此限——需要動非清單 ns 時的正確路徑＝升級給該 agent／叢集運維，不是平台側自己動手。

1. 任何 cluster-wide 掃描（PV、ns、NP、volume、tunnel…）產出後，**先按 ns 歸屬分流**再下結論；報告裡平台側/非平台側分列；唯讀盤點僅供分流，非清單內資源只列不動。
2. 無法歸屬（如無 PV 的 Longhorn 第三態 volume）→ 用 metadata 溯源（volume.status.kubernetesStatus）；溯源不出來就**不動、列報告**。
3. 叢集級設定（default SC、Longhorn settings、default recurring-job-group）影響全叢集含別人的 workload——動之前與叢集運維協調，不由平台側單方改。
4. 對外派工（workflow/subagent/其他 agent）涉及 kubectl 時，此清單要寫進 prompt 鐵律。

首個落地案例：issue #319（Released PV 清理 SOP 每步帶 scope 過濾）。相關：[[longhorn-retain-orphan-semantics]]。

## 🔑 CD lane 的操作邊界：**push 是非破壞性的，merge to main 才是人工 gate**（2026-08-04 自 `.68` rollout 記錄救回）

平台 maestro **可以**在隔離 worktree 直接：建 branch → 自行 push → `tea` 開 PR。
**這些都是非破壞性的**，不需要人類代跑。
🔴 **人工 gate 只在 `merge to main` 那一步**（Eugene web merge）。

⇒ **每一步仍要獨立驗，不採信自己的「我做了」**：
`git ls-remote`（branch commit）＋ `tea api pulls/<N>/files`（變更檔範圍）。

⚠ **這條的價值在於它劃清了「哪些要等人、哪些不用」**——把整條 CD lane 當成「都要人類代跑」會白等，
把 merge 也當成可自動化則會繞過 human-in-the-loop（同檔記的 approval gate 事故就是後者）。

📌 **救回經緯**（值得記，因為它是刪除流程的失效實例）：本條原在 `project_prod_rollout_20260717_68`，
該檔於 2026-08-04 因「教訓已收斂進 `prod-release-sop.md`」被刪。
**但刪除前只驗了 5 條「本波新知」中的 2 條**——leader3 自陳「我只驗了容易驗的那一半」並要求逐條複驗，
dispatcher 複查發現本條**完全沒進 SOP**、且全庫 `merge to main 才是人工 gate` 命中 0
⇒ 從備份救回。
🔑 **教訓：「教訓已進 docs」這個刪除閘是對的，但它必須逐條驗，不能抽樣。**
