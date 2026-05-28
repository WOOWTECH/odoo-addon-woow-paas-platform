# Pitfall: F-PILOT-10 真 LLM cluster e2e 的 8-gap chain（mock/caqa/reviewer 全看不到，唯 cluster + 完整 UI round-trip 暴露）

> 來源：`agent-runtime-pod` epic **F-PILOT-10 真 LLM round-trip**（2026-05-27，stg woow-k3s）。三份 code PR（image #6 / client #76 / teardown #77）merged、mock TDD 5 切片全綠、CAQA PASS WITH CAVEATS + reviewer APPROVE，但**第一次真的在 cluster 跑真 LLM + 完整 browser round-trip**，從「使用者送 chat」到「真 LLM 回話」之間串出 **8 個依序現形的 gap**——每修好一個才露出下一個。**🎉 最終達成**：「你好8」12:58、19s、MiniMax-M2.7 真 LLM 生成、無 error、乾淨 disconnect、browser 真 LLM 回話且理解 history。
>
> **與 [[agent-runtime-pilot-e2e-gap-chain]]（pilot echo 9-gap）的關鍵差異**：那次是 **echo** round-trip（不需外網、不跑 LLM）；這次是 **真 LLM**，第一次行使 external egress + 真 LLM 失敗路徑 + optional chart values，暴露了 echo 階段結構上碰不到的一層。且本輪 missed-forward 撞的是 chart **optional** value（**silent** 失敗，比 pilot 的 required-value 硬 render fail 更陰）。

## Symptom

flip 真 LLM project + 送 chat 後，現象**逐關往後跳**（每修一關、下一關才現）：

```
operator 新邏輯沒生效（stale image）→ pod ImagePullBackOff（imagePullSecrets 沒 render）
→ 連上但第一次 connect refused（cold-start race）→ pod 連上但無回覆、靜默斷線（LLM 失敗無 error frame）
→ LLM egress 被擋（externalEgress 沒開）→ pod 無 model config（chart 漏 wire env）
→ operator 沒 forward egress 設定 → operator config env 無處注入（chart 無 extraEnv）
→ ✅ 真 LLM 回話
```

**關鍵觀察（同 pilot）**：現象往後跳 = 進展訊號。但本輪多數 gap **不是紅字 500**，而是「pod 起了卻行為錯」（拉不到 image / 無回覆 / egress 被擋 / 無 config）——**silent**，比 pilot 的硬 render fail 更難定位（沒 render error 可循，要 `kubectl logs`/`describe`/`get pod -o yaml` 一路挖到 runtime）。

## The 8-gap chain（依現形順序）

| # | gap | 根因 | 修法 | lane | class |
|---|-----|------|------|------|-------|
| **G1** | operator 新 forward 邏輯沒生效 | **operator 自身 image stale**：pin agent-runtime image/chart version 升的是「operator 部署**出去**的 pod 用什麼」，**不是 operator 自己跑什麼**；operator deployment 還跑舊 image，PR 的新 forward 邏輯沒進去 | 升 operator **自身** image（`operator-v*` tag → rebuild → pin operator deployment） | paas-cd/ci | **deploy gap** |
| **G2** | pod ImagePullBackOff | operator(stale，G1) 沒 forward `imagePullSecrets` value **+ chart 無 default backstop** → pod 拉私有 jcr-prod image 失敗 | operator forward（待 G1）+ **chart 補 default backstop**（operator 沒傳時 chart 仍帶得到） | paas-platform(operator) + paas-charts | **missed-forward（+ chart default）** |
| **G3** | 第一次 connect refused | **cold-start race**：odoo `_stream_agent_runtime` 太早 connect（pod 還沒 ready），relay **無 retry** → 第一次必失敗 | odoo relay 加 cold-start retry（connect 失敗退避重試到 pod ready） | paas-platform(odoo) | **transport timing** |
| **G4** | pod 連上但無回覆、靜默斷線 | **image silent close**：真 LLM 失敗（egress 不通 / config 錯）pod **沒送 error frame**、直接 close WS → 前端只見斷線無因；degradation 路徑零 observability（= R10-1） | image：LLM 失敗送 error frame + degradation/ success 路徑補 log | paas-agent-runtime image | **observability** |
| **G5** | 真 LLM egress 被擋 | chart NP `externalEgress` **沒 enable**：echo 不需外網從沒行使，真 LLM 要打 `api.minimax`/openai → pod egress 被自身 NP default-deny 擋 | chart NP `externalEgress.enabled=true` + except-CIDR（LLM 通 + `169.254` metadata 仍擋，C10-1） | paas-charts + deploy | **deploy/egress config** |
| **G6** | pod 無 model config | **chart 漏 wire** `LLM_MODEL_CONFIG` env：operator 傳了，chart template 沒把它映射進 pod env → pod `_load_llm_model_config` 拿不到 model/api_base | chart 補 `LLM_MODEL_CONFIG` env 映射 | paas-charts | **chart 漏 wire（missed-forward chart 半）** |
| **G7** | externalEgress 沒生效 | **operator 沒 forward externalEgress**：`_build_install_values` 沒把 externalEgress 設定 forward 給 chart → chart NP 用 default（不開） | operator forward externalEgress（`_build_install_values`）+ contract test 涵蓋此 optional key | paas-platform(operator) | **missed-forward** |
| **G8** | operator config env 無處注入 | **paas-operator chart 無 `extraEnv`**：operator 自身要的 config env（如 `EXTERNAL_EGRESS_ENABLED`）沒有 chart 注入機制 → 現靠 ephemeral `kubectl set env`、redeploy 丟 | paas-operator chart 加 `extraEnv`（durable 注入 operator config env） | paas-charts(operator chart) | **deploy gap** |

### 真因聚類

- **missed-forward class**（operator `_build_install_values` 系統漏轉，與 pilot 同 class **再現**）：**G2 / G6（chart 半）/ G7**。⚠ **關鍵深化**：這次漏轉的是 chart **optional** value（imagePullSecrets / LLM_MODEL_CONFIG / externalEgress），漏轉 **silent**（chart 用 default/empty、pod 照起但行為錯），不像 pilot 撞 **required** value 的硬 render fail。→ contract test 必須**涵蓋 optional**，見 [[spawn-install-values-missed-forward]]。
- **deploy gap**：**G1**（operator 自身 image stale）/ **G8**（operator chart 無 extraEnv）/ **G5**（externalEgress 沒 enable，egress config 半）。
- **transport timing**：**G3**（cold-start race，relay 無 retry）。
- **observability**：**G4**（真 LLM 失敗 silent close、無 error frame、零 log）。

> G5 + G7 是 **externalEgress 的兩半**：G7 = operator 沒 forward（missed-forward），G5 = chart NP 沒 enable（deploy config）。兩半都到位 egress 才通。

## Why it happens（為何 mock + caqa + reviewer 全看不到）

### 三層驗證結構上碰不到 integration 邊界

| 層 | 驗了什麼 | 為何看不到本 chain |
|----|---------|-------------------|
| mock 單元測試（5 切片全綠）| operator/image/client 邏輯（mock K8s/helm/chart/LLM）| 不 render 真 pod spec（G2/G6）、不部署真 image（G1）、不打真 NP/egress（G5/G7）、不跑真 LLM（G4）、不真 cold-start timing（G3）|
| CAQA / reviewer（靜態 + SSRF battery）| code/diff 正確性、security 控制、契約對齊 | 看不到「operator deployment 跑 **stale** image」（部署**狀態**非 code）、「chart 無 default backstop」（cross-repo render）、「externalEgress 沒 enable」（cluster NP **狀態**）、「真 LLM silent close」（真 LLM **runtime 行為**）|
| **cluster + 完整 UI round-trip** | 真 image + 真 chart render + 真 NP + **真 LLM** + 真 browser | **唯一暴露本 8-gap 的地方** |

**核心**：integration gap 住在 repo/層之間的**接縫**裡——沒有任何單 repo 的 mock 或靜態 review 看得到。這是本 epic 反覆的 meta-pattern（CI green ≠ 真驗、mock green ≠ integrated），在「真 LLM round-trip」這個最完整的 e2e 放到最大。

### optional value 漏轉是 silent（比 required 更陰）

pilot 的 missed-forward 撞 chart **required** value → `_helpers.tpl` validate `fail` → spawn **500**（loud，馬上知道）。F-PILOT-10 的 missed-forward 撞 chart **optional** value（imagePullSecrets / LLM_MODEL_CONFIG / externalEgress）→ chart 用 default/empty、**pod 照樣起來**、只在 runtime 行為錯（拉不到 image / 無回覆 / egress 被擋）→ **沒有 render error 可循**，要一路 `describe`/`logs`/`get -o yaml` 挖。**這就是為什麼 contract test 不能只斷言 required key，要涵蓋 optional-but-load-bearing key。**

### operator 「自身 image」vs「部署出去的 artifact」是兩件事（G1）

pin agent-runtime image tag / chart version 控制的是 operator **install 給租戶 pod** 的 image/chart；operator **自己**跑哪個 image 是另一條 deploy 軸。PR 改了 operator 的 forward 邏輯，要升 **operator 自身 image** 才生效——只 pin agent-runtime image 不會讓新 forward 邏輯上線（G2/G6/G7 的新修法因此「改了卻沒效」，誤判成沒修對）。

### echo → 真 LLM 暴露新一層（G4 / G5）

echo round-trip（pilot）不打外網、不跑 LLM → external egress 與 LLM-失敗-處理**從沒被行使**。真 LLM round-trip 第一次行使 → G5（egress 沒開）/ G4（LLM 失敗 silent close）只在真 LLM 現形。**「跑通 echo」不等於「跑通真 LLM」**——兩者行使的 code/infra 路徑不同。

## How to avoid / 對策 — 3 道防線

整條 chain 的對策收斂成 **3 道防線**（補上三層驗證各自的盲點）：

### 防線 1 — integration test（補 mock 看不到的 render/deploy/真 K8s 層）

- **render-to-pod-spec test**：operator `_build_install_values` → `helm template`（真 render）→ assert 渲染出的 **pod spec** 真有 `imagePullSecrets` / `LLM_MODEL_CONFIG` env / NP `externalEgress`——**含 optional key，非只 required**（直接擋 G2/G6/G7 的 silent 漏轉）。
- **`ensure_registry_secret` real-K8s integration test**：真 K8s（docker-desktop/kind）建 secret + 驗 pod 能拉私有 image（擋 G2，對齊 CLAUDE.md 既有 operator integration test 紀律）。
- **contract test 涵蓋 optional**：對 [[spawn-install-values-missed-forward]] 的 contract test 從「斷言所有 required key」擴成「**含 optional-but-load-bearing key**」（imagePullSecrets/LLM_MODEL_CONFIG/externalEgress）——optional 漏轉 silent，更需要 test 鎖。

### 防線 2 — deploy checklist（驗 running state，非 values pin）

- **驗 operator running image 版本**：`kubectl get deploy paas-operator -o jsonpath=...image` == 預期版本，**非只看 values 裡 pin 了什麼**（直接擋 G1：pin 了 agent-runtime image、operator 自身仍 stale）。
- **驗渲染出的 pod spec 真有需要的 env/secret/NP**：部署後 `kubectl get pod <agent-pod> -o yaml` 對照 imagePullSecrets / env / NP egress 真的在（擋 G2/G5/G6）。
- **operator config env 走 durable 注入**：`EXTERNAL_EGRESS_ENABLED` 等走 chart `extraEnv`、非 ephemeral `kubectl set env`（擋 G8：redeploy 不丟）。

### 防線 3 — observability（success path log，非只 error）

- **image success path 也 log**：LLM 開始 / 完成 / token 數 / 乾淨 disconnect 都 log，degradation 路徑送 error frame + log（擋 G4：真 LLM 失敗 silent close 無因 → 對齊 R10-1）。
- **讓「pod 起了但行為錯」在 log 可見**：optional 漏轉的後果（拉不到 image / 無 config / egress 擋）要能從 pod log 一眼看出，不用 debug 到 browser 才發現。

> **ephemeral 解封 + durable 分軌（同 pilot）**：本輪每關用 ephemeral patch（升 operator image、`kubectl set env EXTERNAL_EGRESS_ENABLED`、patch NP externalEgress、chart override）解封到 round-trip 達成；durable 修正分軌進 follow-ups（見 `follow-ups.md` §F-PILOT-10 cluster e2e gaps）+ 對的 lane。重驗勿走 operator 重 helm install（會用 chart default 打回 silent gap）。

## Related

- [[agent-runtime-pilot-e2e-gap-chain]] — pilot echo 9-gap chain（本 chain 的前作；echo vs 真 LLM 的差異見上方 blurb）
- [[spawn-install-values-missed-forward]] — G2/G6/G7 的 missed-forward class + **contract test 涵蓋 optional** 深化
- [[plan-gap-stop-and-report]] — 同 epic 守線（C10-1 SSRF cross-repo NP gate 等）
- [[credential-claim-test-fixture-coupling]] — 同 epic test-discipline
- `docs/tracking/pitfalls/ci-workflow-container-missing-node.md` — 同 epic「驗綠 ≠ 真驗」family
- `follow-ups.md` §F-PILOT-10 cluster e2e gaps — 本 chain 的 durable 回收 + lane 分派
- review-takeaway 三層 gate：`docs/tracking/reviewer-report-2026-05-27-agent-runtime-fpilot10-code-prs.md`（R10-1 observability / C10-1 NP 等本 chain 印證的 caveat）
