---
title: Runbook：agent-runtime image 升版 SOP（operator / platform 視角）
category: guides
status: active
created: 2026-06-05T07:11:48Z
updated: 2026-06-05T07:11:48Z
tags: [paas, agent-runtime, operator, image-upgrade, reap, helm, fleet]
related:
  - docs/guides/development/agent-runtime-chat-architecture.md
  - docs/tracking/pitfalls/agent-runtime-coldstart-cpu-throttle-timeout.md
  - docs/tracking/pitfalls/agent-runtime-stg-enablement-gap-chain.md
  - docs/tracking/followups/follow-ups.md
  - https://git-prod.woowtech.io/woow-paas/paas-agent-runtime/pulls/21
---

# Runbook：agent-runtime image 升版 SOP（operator / platform 視角）

## 目的

agent-runtime（per-project workspace pod）image 升版後，**既存 pod 不會自動換 image**——
凍結在 spawn 當下的 tag，直到 release 被 uninstall 後重新 cold-start 才吃新版。本文件記錄
operator / platform 端的完整升版程序：tag 注入鏈、手動 reap SOP（2026-06-05 stg 實跑驗證）、
reap 後已知行為，以及「為什麼必須手動」的現狀備註。

**視角分工**：本文件是 **operator / platform 半邊**（tag 怎麼進 pod、怎麼安全汰換既存 pod）。
**runtime 半邊**（image build、release tag、CI auto-bump chart）見 paas-agent-runtime repo
[`docs/runbooks/image-upgrade-sop.md`](https://git-prod.woowtech.io/woow-paas/paas-agent-runtime/pulls/21)（PR #21），兩份互為對照、不重複論述。

---

## 1. Tag 注入鏈（image tag 怎麼到 pod 上）

```
paas-app-bundle-cd values-stg.yaml          ← 手動 bump（agentRuntime.imageTag + chartVersion）
  └─ Fleet reconcile（~30–60s lag）
       └─ paas-operator chart → operator Deployment env AGENT_RUNTIME_IMAGE_TAG
            └─ settings.agent_runtime_image_tag        ← extra/paas-operator/src/config.py:47
                 └─ cold-start helm install 注入 image.tag
                      ← extra/paas-operator/src/api/agent_runtimes.py:122（_build_install_values）
```

關鍵性質：

- **注入只發生在 cold-start `helm install`**（`_build_install_values`，`agent_runtimes.py:122`）。
  resume 路徑（`_resume_pod`，`agent_runtimes.py:302`）只重簽 SKILL_BUNDLE_TOKEN + set env +
  scale 1，**不重 render** → 既存 release 永遠是舊 image。
- `config.py:47` 的 default `"1.0.0"` 是 **fallback，僅在 env 缺席時生效**。正常部署 Fleet 一定
  set `AGENT_RUNTIME_IMAGE_TAG`，看到 pod 跑 1.0.0 多半是 env 沒注入（去查 operator chart
  values→env wire 與 Fleet values），不是 fallback 該背的鍋。
- **Fleet reconcile 有 ~30–60s lag**：merge Fleet values 後立刻查 operator deploy env 可能還是
  舊值。reap 前**務必**確認 `kubectl get deploy paas-operator-stg -n paas-stg-system -o yaml` 的
  env 已是新 tag、且 pod 已 rolling（新 ReplicaSet、age 小），否則 reap 後 cold-start 回舊
  image 白做。
- imageTag 與 chartVersion 是**兩個不同版號**：`agentRuntime.imageTag` = pod image
  （appVersion，如 `1.2.1`）、`agentRuntime.chartVersion` = agent-runtime **chart** 版號
  （如 `0.3.1`）。CI auto-bump 只動 charts repo，**Fleet operator pin 兩值都要手動 bump**
  （F-129-1 chart 半已解、operator-pin 半仍手動，見 follow-ups）。

## 2. 現狀備註：為什麼必須手動 reap

- `spawn_or_attach`（`agent_runtimes.py:320`）的 cold-start gate **只看
  `_release_exists()`**（`:325`），**不比 image tag**。release 存在（含 replicas=0 的 dormant
  release）→ 走 attach / resume → **還是舊 image**。
- 所以 `kubectl scale --replicas=0` 對換 image **是白做**（下次 chat 走 resume 拉回舊 image）。
  注意 scale-to-0 對 **SKILL_BUNDLE_TOKEN 重簽是有效的**（resume 會重簽）——兩件事別混。
- `POST /api/agent-runtimes/{hash}/reap`（`agent_runtimes.py:454`）是 **idle reap =
  scale-to-0**（保留 release / PVC），**不是**升版用的路徑。
- **auto-roll（F-AR-1）未落地**：四半 lock-step 設計中，③ runtime graceful-drain 半已隨
  1.2.1 / chart 0.3.x 上 stg，**② operator-reconcile 半（本 repo `extra/paas-operator/`）仍待
  實作**。落地前，唯一換 image 路徑 = 本文件的 DELETE reap → cold-start。進度見
  [`docs/tracking/followups/follow-ups.md`](../../tracking/followups/follow-ups.md) F-AR-1 條。

## 3. Reap SOP（實際執行程序）

> 2026-06-05 stg 實跑（5 release 全數汰換）驗證的程序。以下以 stg 為例；prod 把
> `paas-operator-stg` / `paas-stg-system` / `paas-ws-stg-pa-` 換成對應名稱。

### 3.1 前置：確認新 tag 已落到 operator

```bash
kubectl --context woow-k3s get deploy paas-operator-stg -n paas-stg-system \
  -o jsonpath='{.spec.template.spec.containers[0].env}' | tr ',' '\n' | grep -A1 IMAGE_TAG
kubectl --context woow-k3s get pods -n paas-stg-system   # 確認 operator pod 已 rolling（age 小）
```

### 3.2 盤點：用 helm / deployment 盤，別只看 running pod

```bash
helm --kube-context woow-k3s list -A | grep agent-
# 或
kubectl --context woow-k3s get deploy -A | grep paas-ws-
```

**必須含 scaled-to-zero 的 dormant release**：它們沒有 running pod，但 resume 會以舊 image
拉起。2026-06-05 stg 實例：pod 盤只見 3 顆、helm 盤才見全 5 顆——只盤 pod 會漏一半。

### 3.3 三重核對 project_hash + KEEP 名單防呆

每顆要 reap 的 release，核對三個來源一致才動手：

1. **pod ns**：`paas-ws-stg-pa-{hash}`（stg NAMESPACE_PREFIX 有 `stg-` infix）
2. **agent_session**：odoo shell（stg DB `woow_stg`）撈
   `env['woow_paas_platform.agent_session'].sudo().search([])` 列 project_id / name /
   `project_namespace_hash`（hash 演算法見 `src/models/agent_session.py:108`：
   base32(hmac_sha256(secret, project_id)[:10])）
3. **project**：確認該 project 確實屬於要升版的環境 / 租戶

同時列一份 **KEEP 名單**（不該動的 release 明文列出），逐顆 DELETE 前先對 KEEP 名單防呆——
這一步擋的是「hash 看起來都長一樣、複製貼上貼錯顆」的人為事故。

### 3.4 in-flight = 0 確認

確認該 project 沒有進行中的對話（agent_session `state` 非 streaming 中、pod log 無 active
turn）再 reap。runtime ≥1.2.1 有 SIGTERM graceful drain（in-flight turn 收完才退），但 SOP
仍以「挑沒人用的時間動手」為第一道防線。

### 3.5 逐顆 DELETE（= helm uninstall，非 scale-to-0）

走 operator 設計端點，**別裸 `helm uninstall`**（operator 是 release 的 owner，繞過它會留
state 不一致面）：

```bash
kubectl --context woow-k3s port-forward -n paas-stg-system svc/paas-operator-stg 8000:80
# API key 在 paas-operator-stg-secret 的 key `api-key`
curl -X DELETE -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/agent-runtimes/{project_hash}
```

`DELETE /api/agent-runtimes/{hash}`（`agent_runtimes.py:530`）= **helm uninstall 整個
release**（`:534`）——這才是升版路徑；`POST .../{hash}/reap` 只 scale-to-0，換不了 image（§2）。

### 3.6 等資源完全消失再觸發 cold-start

```bash
kubectl --context woow-k3s get pod,pvc -n paas-ws-stg-pa-{hash}
```

要等該 ns 的 pod + PVC **完全消失（非 Terminating）**再觸發下一次 chat。否則新 install 撞
同名 PVC 殘影。

### 3.7 驗證：端到端，不可只看 image = 新版

下一次 chat 觸發 cold-start 後：

- `kubectl get pod -n paas-ws-stg-pa-{hash} -o jsonpath='{.items[0].spec.containers[0].image}'`
  確認 tag 是新版——**這只是必要條件**。
- **必須端到端驗**：chat 回 tool_result / pod log 出現新版預期行為。normalize 類 runtime bug
  在舊 image 上 snapshot 也可能「看起來對」；且重 spawn 後有空快照 race、skill SKILL.md
  fetch 失敗等下游失敗面（同屬 cold-start 凍結家族，見
  [`agent-runtime-llm-key-after-coldstart-stuck-echo.md`](../../tracking/pitfalls/agent-runtime-llm-key-after-coldstart-stuck-echo.md)）。

## 4. Reap 後已知行為

| 行為 | 機制歸屬 | 說明 |
|------|---------|------|
| **PVC 連帶刪除** | helm 行為（chart 設計） | agent-runtime chart `templates/pvc.yaml` 無 `helm.sh/resource-policy: keep`（2026-06-05 stg reap 5/5 實證；runtime owner 確認是 chart 設計的正確行為）。PVC 只含 lazy-fetch skill cache（可重抓，重拉後首次 chat 略慢）；chat 史在 Odoo DB → 資料安全。「保 PVC」只存在於 F-AR-1 auto-roll 的 `helm upgrade` 路徑。 |
| **空 namespace 保留** | operator 設計 | uninstall 不刪 ns（供 resume / 下次 cold-start 復用）。**兩層別混**：ns 保留 = operator 設計、PVC 消失 = helm 行為。 |
| **DELETE 不發 state webhook → session 漂移自癒** | operator 設計 | `delete_runtime` 只 helm uninstall、不呼叫 `webhook_dispatcher` → Odoo `agent_session.state` 短暫停留在舊值（如 `active`）。自癒機制：`spawn_or_attach` **每次** attach / cold-start 都 re-notify state webhook（`agent_runtimes.py:430` 起的註解，刻意非 transition-only）+ drift cron → 下一次 chat turn 收斂。看到 stale state 不用手修。 |
| **cold-start 首次 chat 較慢** | 已知 perf 特性 | operator CPU bump 後 stg 實測 ~11s；client 端已有 spawning 進度 + retry。見 [`agent-runtime-coldstart-cpu-throttle-timeout.md`](../../tracking/pitfalls/agent-runtime-coldstart-cpu-throttle-timeout.md)。 |

## 相關

- **runtime 視角 SOP**（build / release / CI auto-bump 半邊）：paas-agent-runtime
  [`docs/runbooks/image-upgrade-sop.md`（PR #21）](https://git-prod.woowtech.io/woow-paas/paas-agent-runtime/pulls/21)
- **F-AR-1 auto-roll 設計與進度**：[`docs/tracking/followups/follow-ups.md`](../../tracking/followups/follow-ups.md)
- **chat 資料流 / secret 關係**：[`agent-runtime-chat-architecture.md`](../development/agent-runtime-chat-architecture.md)
- **stg 啟用 gap chain**（升版撞牆時的環境前提對照）：[`agent-runtime-stg-enablement-gap-chain.md`](../../tracking/pitfalls/agent-runtime-stg-enablement-gap-chain.md)
- **operator 程式碼**：`extra/paas-operator/src/api/agent_runtimes.py`（spawn_or_attach `:320` /
  image 注入 `:122` / idle reap `:454` / DELETE `:530`）、`extra/paas-operator/src/config.py:47`
