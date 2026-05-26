# Pitfall: agent-runtime pilot e2e 的 9-gap chain（flip 實跑才一關關現形）

> 來源：`agent-runtime-pod` epic Pilot e2e（Step 6 flip 實跑 → round-trip 閉環，2026-05-26，stg woow-k3s）。Phase A-D 全部 code-ready + 單元測試綠 + CAQA/reviewer approved，但**第一次真的 flip 跑 e2e**，從「使用者送 chat」到「agent 回話」之間串出 **9 個依序現形的 gap**——每修好一個才露出下一個。沒有一個是單元測試抓得到的（全是 mock 掩蓋的 cross-component 契約 / 部署 config / 動態 ns provision），正是 BUG-7/8/9「integration-only」class 的放大版。

## Symptom

flip pilot project + 送 chat 後，錯誤訊息**層級逐關往後跳**（每修一關、下一關才現）：

```
Access denied（前端）→ spawn-or-attach 500（validate_namespace）→ 500（helm pull 401）
→ 500（chart render projectId）→ ImagePullBackOff → CrashLoopBackOff（probe）
→ WS client unavailable（odoo dep）→ 已連線但無回覆（agent echo 未實作）
```

**關鍵觀察**：錯誤層級往後跳 = 進展訊號（前一關過了）。但若沒一關關拆，看到的只是「500 / 紅字」，會誤判成單一 bug 反覆。

## The 9-gap chain（依現形順序）

| # | gap | 根因 | 修法 | lane | class |
|---|-----|------|------|------|-------|
| 1 | chat 「Access denied」 | seed 用 superuser 建 task → channel 成員只有建立者、登入的 admin 非 channel member → `_check_channel_access` 擋（spawn 前就擋） | seed: admin 入 channel + task 改派 admin | seed/ops | **seed data** |
| 2 | spawn-or-attach 500 | `_names` hardcode `paas-ws-pa-` 但 operator env `NAMESPACE_PREFIX=paas-ws-stg-` 收緊 → `validate_namespace` 在 helm 跑前就 raise | operator `_names`/`mcp_invalidator` 從 `settings.namespace_prefix` 衍生（PR #66） | paas-platform | **missed-forward** |
| 3 | helm pull 401 | Gitea Helm registry index.yaml 的 tarball URL 是**公開 host** `git-prod.woowtech.io`，operator `--repo` 用**內部 cluster DNS** → helm 抓 index 帶 creds，抓 tarball 換 host 不帶 → 401 | repoUrl 改用公開 host（index/tarball 同 host） | paas-charts/paas-cd (config) | **config 不一致** |
| 4 | chart render 500 | chart `_helpers.tpl` validate 要 `projectId`，但 operator `_build_install_values` 只 forward `projectHash` | operator forward `projectId`（PR #67）+ 全盤對齊 required values + contract test | paas-platform | **missed-forward** |
| 5 | ImagePullBackOff | agent image 私有（jcr-prod），但 operator 建的動態 ns 無 dockerconfigjson + chart imagePullSecrets 預設空、operator 沒 forward → 匿名 pull 401 | operator provision registry secret 進 ns + forward imagePullSecrets | paas-platform + **設計** | **plan-gap** |
| 6 | CrashLoopBackOff | chart probe `httpGet /health`，但 image 不 serve `/health`（404）→ readiness 永不過 + liveness 殺 | chart 改 `tcpSocket:ws`（或 image 補 /health） | paas-charts ± image | **chart↔image 契約** |
| 7 | re-spawn Multi-Attach | chart 預設 RollingUpdate + RWO workspace PVC → 新 pod 起來前舊 pod 不放 PVC | chart `strategy: Recreate` | paas-charts | **chart config** |
| 8 | 「WS client unavailable」 | odoo `_stream_agent_runtime` 用 `websockets.connect()` 橋接 pod，但 `websockets` 不在 requirements.txt → ImportError | requirements 加 `websockets`（PR #68）+ paas-odoo rebuild | paas-platform | **missing dep** |
| 9 | 已連線但 agent 不回話 | image 1.0.0 收 user_message 即關、不產回覆 frame（echo 收口前）；且 template 無 LLM model_config（綁了仍不變 → 確認是 image chat 邏輯，非 seed） | image 實作 `/v1/chat` 回覆（依 PR #69 [[agent-runtime-ws-protocol]] 契約）+ LLM cred 注入設計 | image team + **設計** | **image lane + plan-gap** |

## Why it happens

### 單元測試 mock 掉所有 integration 邊界

operator 單元測試 mock `helm_service` / `k8s_service` → `validate_namespace`、helm pull、chart render、image pull、NP、probe **全沒被行使**。gap 2-7 都在 mock 之下，CI 全綠。**只有真 flip 跑 e2e 才會逐一現形**（integration test 也只覆蓋部分，且用的 prefix/ns 剛好不撞）。

### 部署 config 與 code 預設脫鉤

gap 2（NAMESPACE_PREFIX）、gap 3（repoUrl）：code 預設值（`paas-ws-`、internal DNS）在某環境 work，但部署 config 為了別的需求（環境隔離、私有 registry）改了值，**打破了 code 裡的隱含假設**。

### 動態 per-project ns 的 provision 漏洞（gap 5）

operator 動態建的 ns 是「新生兒」——沒有 marketplace ns / odoo ns 既有的 registry secret / imagePullSecrets。**「pod 怎麼從私有 registry 拉 image」這個 provision 步驟在 plan 裡沒有 owner**（plan-gap，非漏轉）。

## How to avoid / 對策

1. **flip 前做一次「mock 之下全是什麼」盤點**：列出單元測試 mock 掉的每個 integration 邊界（helm install/pull、k8s ns/secret/NP、probe、image pull、跨進程 auth、odoo↔pod transport），每個都是潛在 gap。
2. **修一個 missed-forward 時全盤對齊**（見 [[spawn-install-values-missed-forward]]）：gap 2 + gap 4 同 class，修 gap 4 時一次對齊 chart **所有** required values，避免 gap 4.5。
3. **部署 config override code 預設時，回頭驗 code 的隱含假設**：改 `NAMESPACE_PREFIX` / repoUrl 時，grep code 裡 hardcode 那個值的地方。
4. **動態 ns 的 provision 清單**：operator 建 ns 時，把「pod 跑起來需要 ns 裡有什麼」列全（labels for NP、registry pull secret、quota…），不是只建裸 ns。
5. **plan-gap 停下回報、別硬塞**（見 [[plan-gap-stop-and-report]]）：gap 5（registry provision）、gap 9（LLM cred）都是 design 缺口，agent 停下 escalate 而非硬塞假值。
6. **ephemeral 解封續測 + durable 分軌**：每關用 ephemeral patch（set image/env、copy secret、patch NP/probe）解封讓 e2e 往下測，durable 修正分軌進 PR / 設計（見 `follow-ups.md` F-PILOT-1~10）；重驗用 `rollout restart` 保留 ephemeral patch、勿走 operator 重 helm install（會用 chart default 打回 gap 5/6/7）。

## See also

- [[spawn-install-values-missed-forward]] — gap 2/4/5 的 missed-forward class + 對策
- [[plan-gap-stop-and-report]] — gap 5/9 的 plan-gap 守線
- [[agent-runtime-ws-protocol]] — gap 9 image 端實作依循（PR #69 契約）
- `docs/tracking/pitfalls/ci-workflow-container-missing-node.md` — 同 epic 的另一 integration-only false-green 教訓
- `follow-ups.md` §F-PILOT — 本 chain 的 durable 回收清單（分派藍圖）
