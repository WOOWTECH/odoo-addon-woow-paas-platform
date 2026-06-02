# Pitfall: agent-runtime LLM key 在 pod cold-start *之後* 才設 → 永久 echo（provisioning 只在 cold-start）

> 來源：stg debug session 2026-06-02。症狀是 `dev-sandbox-stg` 的 `/woow` 任務 chat，AI 一直回固定罐頭「收到你的訊息:「…」。agent runtime round-trip 已接通。」，且使用者**已經**在 woow Settings 把 default config / assistant / agent template / OpenAI-compatible config 全設好。逐層 live 驗證後發現 **Odoo DB 端設定全對**，真正卡點是 operator 的一個 provisioning 不對稱缺口：`_provision_llm_secret` 只在 cold-start 跑，pod 已存在後才設/改 key 永遠接不上。
>
> 與 [[agent-runtime-stg-enablement-gap-chain]] 區分：那篇是「**從沒啟用過**」的 5 層 fail-closed gap（flag/template/secret/NP 都沒到位，echo 代表 round-trip 終於通）；本篇相反——**啟用全到位、echo 卻持續**，因為 key 是在 pod spawn 後才補的，而 provisioning 不會回頭補。echo 字串本身的來源與 keyless gate 見 cross-repo `paas-agent-runtime/runtime/langgraph_runtime.py:178-190`（`LLM_API_KEY` 不存在 → `_echo_stream`）。

## Symptom

- 任務 chat 開 AI Auto-Reply，**不論輸入什麼**都回同一句「收到你的訊息:「{原文}」。agent runtime round-trip 已接通。」（echo 是 deterministic = LLM 從沒被呼叫）。
- 可能同時看到 **OdooBot 與 AI Assistant 兩個 author 都回同一句**（runtime path 落 `base.partner_root`、legacy path 落 assistant partner；兩條都因同一個缺 key 條件 dead-end）。
- Odoo DB 端**檢查全對**：project `use_agent_runtime=True`、綁到 published agent_template、template 的 `model_config_id` 指向的 ai.config 已填 `api_key`/`api_base_url`/`model`。
- 但 agent pod `LLM_API_KEY=UNSET`，且該 workspace ns **沒有 `{release}-llm` Secret**；pod AGE **早於**「填 key 的時間」。

## Why it happens — provisioning 與 credential-change 不對稱

LLM key 流向 pod 的唯一橋：`_build_llm_credential`（`src/services/agent_runtime_client.py:43-48`）只讀 **resolved `agent_template.model_config_id.api_key`**（不讀 `res.config.settings` 的 `default_ai_config_id`/`default_ai_assistant_id`，也無 env fallback）。有 key 才把 `api_key`+`llm_model_config` 放進 spawn body（`:127-131`）。

operator 端 `extra/paas-operator/src/api/agent_runtimes.py`：

| 路徑 | 條件 | 是否 `_provision_llm_secret` | 是否重啟 pod |
| --- | --- | --- | --- |
| cold-start | `not _release_exists()` | ✅ `:242` | n/a（pod 才剛建，帶 Secret） |
| attach | release 存在 + replicas≥1 | ❌（修前） | ❌ |
| resume | release 存在 + replicas==0 | ❌（修前，`_resume_pod` 只重簽 token + scale） | scale，但不重 provision |

`{release}-llm` Secret 由 `_provision_llm_secret` 建立，pod 用 `optional:true` 的 secretKeyRef 取 `LLM_API_KEY`（env 只在 pod 啟動時讀，不 hot-reload）。

**時間線（本案）**：① pod 31h 前 cold-start，當時 ai.config 還沒 key → operator 沒建 `{release}-llm` Secret → pod `LLM_API_KEY` 空 → echo。② 使用者**之後**才把 key 填進 ai.config。③ 之後每次 chat → release 已存在、replicas=1 → 走 **attach** → 不 re-provision、不重啟 → pod 永遠 keyless → **永久 echo**。reap→resume 也救不了（兩者都不 provision）。唯一會重新 provision 的是「release 不存在」的 cold-start。

## 診斷 recipe（共用叢集只做 read-only）

1. pod 是否 keyless（最決定性）：
   `kubectl --context woow-k3s exec -n paas-ws-stg-pa-{hash} {pod} -- sh -c 'echo ${LLM_API_KEY:+SET}${LLM_API_KEY:-UNSET}'`
2. ns 是否有 `{release}-llm` secret：`kubectl get secret -n paas-ws-stg-pa-{hash}`（沒有 = 命中）。
3. pod log：`grep 'no LLM_API_KEY → echo fallback'`（vs 正常的 `stream: LLM path`）。
4. DB 端用 **psql 直查**（odoo shell 在 sandbox pod 記憶體不足會 `MemoryError`，且第二個 registry 會壓垮線上 odoo）：
   `kubectl exec -n dev-sandbox-stg deploy/...-odoo -- sh -c 'export PGPASSWORD=$POSTGRES_PASSWORD; psql -h $..._POSTGRES_SERVICE_HOST -U $POSTGRES_USER -d woow_stg -c "<SQL>"'`
   查：`ai_config`(`api_key`/`api_base_url` 有無)、`woow_paas_platform_agent_template.model_config_id`、`agent_template_project_rel`、`project_project.use_agent_runtime`、`ir_config_parameter` 的 `default_*`。

## Fix

### 立即解（讓現有正確設定生效）

teardown release 強制下次 chat 走 cold-start：

```bash
helm uninstall agent-pa-{hash} -n paas-ws-stg-pa-{hash} --kube-context woow-k3s
# 或 operator: DELETE {operator_url}/api/agent-runtimes/{project_hash}   (agent_runtimes.py:413)
```

再到任務 chat 送一則訊息 → cold-start → `_provision_llm_secret`（此時 key 已在）→ 新 pod 帶 `LLM_API_KEY`。驗證 §「診斷 recipe」1+3 轉為 `SET` / `stream: LLM path`。

### 結構性修（branch `fix/agent-runtime-llm-secret-provision-on-attach`）

讓 attach/resume 也能收斂 post-spawn 的 key 變動，且不在無變動時 churn pod：

- `_provision_llm_secret` 已 idempotent（`apply_manifest`），抽出 `_llm_secret_string_data` + `_llm_checksum`（SHA-256，存 digest 不存明文）。
- cold-start：install 後 `_stamp_llm_checksum` 把 checksum 寫到 **deployment metadata annotation** `agent.woow.tw/llm-checksum`（metadata annotation 不觸發 rollout，純記狀態，讓首次 attach 是 no-op）。
- attach/resume：比對 `_read_llm_checksum` vs desired；變了才 `_provision_llm_secret` +（attach 時）`rollout_restart_deployment`（resume 走 scale-up 自然帶新 Secret）+ 重新 stamp。
- 副帶自癒：修前 spawn 的 pod 沒 annotation（讀到 None ≠ desired）→ 下次 chat 自動 re-provision + restart 一次。
- 新增 `KubernetesService.get_deployment_annotation`；測試 `extra/paas-operator/tests/test_agent_runtimes.py`（既有 attach/resume 測試補 `get_deployment_annotation.return_value="none"` 維持 no-op；新增 3 個覆蓋 set-after-coldstart/unchanged-noop/cold-start-stamp）。**141 unit 全綠**；尚需 integration test（real k8s）+ operator rebuild+redeploy 才在 stg 生效。

> 仍未改：`_build_llm_credential` 無 `default_ai_config_id`/env fallback，故 key **必須**在 resolved template 綁的 ai.config 上（不是只在 woow Settings 的其他 AI 欄位）。這是另一條設計取捨，本次未動。
>
> 記憶體索引：[[reference_agent_runtime_llm_key_after_coldstart]]。
