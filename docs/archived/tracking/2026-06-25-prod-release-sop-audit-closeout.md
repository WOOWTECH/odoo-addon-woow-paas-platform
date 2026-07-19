---
name: 2026-06-25-prod-release-sop-audit-closeout
phase: ops / docs closeout（已 shipped，非 draft）
scope: prod 發佈 SOP 整理收尾 —— §10.1 稽核 6 條全數結案 + Fleet operator-registry drift 修復 + agent-runtime prod 狀態複驗 + 跨 repo 文件對齊
related_sop: docs/guides/deployment/prod-release-sop.md
related_pitfall: docs/tracking/pitfalls/fleet-split-registry-values-merge-drift.md
related_followup: docs/tracking/followups/follow-ups.md（F-ENV-6）
created: 2026-06-25
updated: 2026-06-25
status: archived
archived-from: docs/tracking/reports/2026-06-25-prod-release-sop-audit-closeout.md
archived: 2026-07-19
archived-reason: "一次性交付/稽核收口報告"
---

# 發佈紀錄 — prod 發佈 SOP 整理收尾 + 稽核 issue 全數結案（2026-06-25）

> 本 doc 是「整理 prod 發佈 SOP」goal 一輪收尾的**發佈紀錄**：把這批已 merge 進 `develop` 的 docs 變更、唯一一次 prod 寫入、跨 repo 文件對齊與 agent 協調，集中成一份可回查的交付清單。**所有項目皆已 shipped**（非 draft）。

---

## A. 已 merge 的 PR 清單

### woow_paas_platform（→ `develop`）

| PR | merge SHA | 主題 | 動到的檔 |
|----|-----------|------|---------|
| [#224](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/224) | `3fff1d1` | 修復 Fleet operator-registry drift（#18 已解）+ pitfall 誤診更正 + SOP §2 矩陣刷新 | `prod-release-sop.md`、`fleet-split-registry-values-merge-drift.md`、`CLAUDE.md`、`follow-ups.md`（F-ENV-6） |
| [#225](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/225) | `cceba98` | SOP §10.1 標記 issue #20（CD repo README drift）已解決 | `prod-release-sop.md` |
| [#226](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/226) | `b543f0f` | SOP §10.1 標記 issue #26（charts README OCI drift）已解決 | `prod-release-sop.md` |
| [#227](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/227) | `ee1bd9d` | SOP §10.1 標記 paas-odoo-ci#1（prod approval gate）已解 + §10 全數收斂 | `prod-release-sop.md` |
| [#229](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/229) | `8e67c5c` | SOP §2 觀察更新 agent-runtime prod 啟用狀態（已四向複驗未啟用） | `prod-release-sop.md` |

### paas-app-bundle-cd（→ `main`，跨 repo）

| PR | merge SHA | 主題 |
|----|-----------|------|
| [#28](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/pulls/28) | `34fdd23` | 更正 README cloudflare「尚未 pin」殘留 drift（#19 已 merge、實際已 pin chart 0.1.2 + image 2026.3.0）。純 README，不在任何 bundle 路徑 → 不觸發 Fleet reconcile |

---

## B. 唯一一次 prod 寫入（cluster 操作，非 PR）

**issue #18 — Fleet `paas-operator-registry` drift 修復**（`kubectl --context woow-k3s`）：

```bash
kubectl --context woow-k3s -n paas-system patch secret paas-operator-registry --type=json \
  -p='[{"op":"remove","path":"/data/registry-server"},{"op":"remove","path":"/data/registry-username"}]'
```

- **根因**（經 paas-charts 複核 + live 證據）：chart `secret-registry.yaml` 自 0.1.6 起就單 key 模型（只渲染 `registry-password`），`server`/`username` 走 plaintext deployment env；那兩個 live key 是 chart 外手動 `kubectl apply` 殘留、零 consumer。
- **驗證**：bundle `paas-app-bundle-paas-operator` → `ready:1`、GitRepo → `5/5`、operator pod 0 restart、`REGISTRY_SERVER/USERNAME` plaintext env 不受影響。
- **rollback**：備份 `paas-operator-registry-backup-20260625.yaml`（兩個值同為 plaintext env，一行可還原）。
- issue [paas-app-bundle-cd#18](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/18) 已留言 + 關閉。

---

## C. SOP §10.1 稽核 6 條 — 全數結案

| issue | 性質 | 結果 | 經手 |
|-------|------|------|------|
| **#18** Fleet operator-registry drift | ops | ✅ 移除殘留 key（§B） | 本 session 親修 |
| **#19** cloudflare 未 pin | ops | ✅ chart 0.1.2 + image 2026.3.0（殘留 README 註記由 CD PR #28 補） | 並行修 + 本 session 補殘留 |
| **paas-odoo-ci#1** 無 prod approval gate | CI | ✅ fail-closed 人工核可 gate（`PROD_APPROVERS` `/approve`，位在 build/push 前）+ repo var 已配置 | 並行修，本 session 複驗 |
| **#20** CD repo README drift | docs | ✅ README 重寫 5 component 結構表（逐項對齊 fleet.yaml） | 並行修 + 本 session 補殘留 |
| **#26** charts README OCI drift | docs | ✅ README 改「傳統 Gitea Helm Repo、非 OCI」+ 勿用 `oci://` caveat | 並行修，本 session 複驗 |
| **#5** 認知 drift（odoo CD 手動說法） | clarify | ✅ 已澄清（odoo 確由 Fleet GitOps 管） | 既有 |

§10 header 已加「✅ 全數收斂（2026-06-25 複驗）」總結註記。

---

## D. agent-runtime 在 prod 啟用狀態 — 四向複驗

**結論：prod 未啟用**（刻意，stg 為先導邊；非 bug）。

| 角度 | PROD | STG（對照）|
|------|------|-----------|
| operator deploy `AGENT_RUNTIME_*` env | 0 個 | 13 個 |
| CD `values-prod.yaml` `agentRuntime` 區塊 | 無 | 完整 |
| `fleet-local`/`paas-system` agent-runtime secret | 無 | repo + auth |
| `paas-ws-*` 租戶 ns | 0 prod | 11 `paas-ws-stg-*` |

要在 prod 啟用是獨立 rollout 工程（補 prod `agentRuntime` values + secrets + 走 5 層 stg-enablement gap chain），**非本 SOP 整理範圍**。SOP §2 觀察已更新此複驗（PR #229）、§5.3 既有描述驗證準確。

---

## E. 跨 agent 協調紀錄

- **pitfall 誤診更正**：原 `fleet-split-registry-values-merge-drift.md` 的「split-values 非 deep-merge 吃掉 server」框架經 paas-charts 複核確認為**誤診**（chart 從不渲染 server/username 進 secret），已加 RESOLVED + 根因更正，誤診段落 superseded 標記保留作教訓。
- **follow-up F-ENV-6 reconcile**：並行的 PR #223 把同一條 drift 記成 open + 沿用舊誤診；本 session rebase 時一併改成 RESOLVED + 正解，避免 develop 自相矛盾。
- **paas-cd agent 收尾**：paas-cd 卡在 obsolete branch `feat/operator-paas-env-config`（stg PAAS_ENV bump 審議因 3 gap 停下問使用者、未獲答覆而擱置）；該任務已被 scout PR #26（stg）/ #27（prod）做完上線。請 paas-cd 自行對 main+live 複核確認其 branch 整套已被涵蓋（`git log origin/main..HEAD` 回空、未 commit 變更僅過時註解、零 YAML 值差異），已切回 main（HEAD `34fdd23`）、丟棄 obsolete branch + 未 commit 變更、零殘留零例外。

---

## F. 不在本批（同期並行、別的工作流）

下列 develop 上同期 merge 的 PR **不屬本批**，是 F-ENV naming env-token 工作流（另一 session / scout）：#228（SmartHome dedicated tunnel env token）、#230（F-ENV-7 SmartHome live stg）。列此避免日後混淆。

---

## G. 淨結果

- prod 發佈 SOP 主檔（`prod-release-sop.md`）內容**準確、稽核清單全綠**，給人類 release 操作者與 harness agent 用。
- prod 服務全程零 outage；唯一 prod 寫入（#18 drift 修）已驗證 + 可 rollback。
- 跨 repo 文件（CD README、charts README）對齊實況，跨 repo issue #18/#20/#26/#1 全 closed。
- 一條可回查的 memory（`tea-cli-write-gotchas`）+ goal note 進度 log + 本發佈紀錄。
