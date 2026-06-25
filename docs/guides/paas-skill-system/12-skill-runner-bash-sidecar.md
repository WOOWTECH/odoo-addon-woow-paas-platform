---
title: skill-runner（bash sidecar / Drop B）
category: guides
status: active
created: 2026-06-09
updated: 2026-06-09
tags: [paas, skill-system, skill-runner, bash-sidecar, drop-b, kill-switch, security]
related:
  - 05-skill-runtime-lifecycle.md
  - 06-pod-internals.md
  - 07-deployment.md
  - 09-security-model.md
source: docs/superpowers/specs/2026-06-08-skill-claude-code-model-redesign-design.md
---

# skill-runner（bash sidecar / Drop B）

> Claude Code 模型的 `bash:execute` 跑的是「使用者上傳、不受信任的任意 bash」。skill-runner 是用來
> **安全地執行那些 bash** 的 sidecar。本篇講它是什麼、為什麼是 sidecar、以及驅動它的單一 kill-switch。
>
> **STG 已上線並驗證（2026-06-09）；prod 待 Fleet flip**。設計依據見
> `docs/superpowers/specs/2026-06-08-skill-claude-code-model-redesign-design.md`，
> 跨 repo land 步驟見 `docs/superpowers/plans/2026-06-08-skill-claude-code-model-LAND-runbook.md`。

## 🎯 一句話

skill-runner = agent pod 內**多掛的一個 sidecar container**（跑 `bash_bridge` daemon），用**異於 agent
的非特權 UID** 執行 `bash:execute` 的指令，避免 untrusted skill bash 偷走同 pod 的平台憑證或 fork-bomb
餓死 runtime。

> ⚠️ 別跟 `mcp_server.sidecar_backed` 搞混——那是「某個 MCP server 跑在 cloud_service 的 sidecar pod」，
> 與 skill 執行無關。

## 🧱 為什麼非得是 sidecar（不是 in-process subprocess）

`bash:execute` 的 bash 子行程**必須用異於 agent 的 UID** 跑，兩個硬理由：

1. **`/proc` 旁路**：agent 跑 `USER 10001` 且通常是 container PID 1。若 bash 同 UID → 攻擊者
   `cat /proc/1/environ` 直接撈 `LLM_API_KEY`，env scrub 形同虛設。
2. **`RLIMIT_NPROC` per-UID DoS**：`RLIMIT_NPROC=16` 是 per-UID 累計；同 UID 的 untrusted bash fork 與
   runtime 共用配額，fork-bomb 可餓死 runtime。

但 agent container 非 root（`USER 10001`、無 `CAP_SETUID`）→ **程序內無法 setuid**。所以拍板用
**同 pod 的 sidecar container**，由 k8s `securityContext.runAsUser` 宣告式給異 UID，並取得 **PID
namespace 隔離**——比「同 container 異 UID」更強：bash **看不到** `/proc/<agent-pid>`，不只靠檔案權限。

| 設定 | 值（stg 實測） |
|---|---|
| sidecar `runAsUser` | `10002`（agent 為 `10001`） |
| `shareProcessNamespace` | **unset**（PID ns 隔離） |
| `supplementalGroups` | `[10003]`（共享 `/workspace` PVC 的 fsGroup） |
| 共享 volume | `/workspace` PVC（agent↔sidecar 指令通道走這） |
| daemon | `bash_bridge`（agent-runtime image ≥ `1.4.0`） |

執行隔離複用既有 `subprocess_runner` 的 `_safe_env`（env allowlist）/ `_set_rlimits` / `_capped_*`，
只是改在 sidecar 內、用異 UID 跑。

## 🔌 單一 kill-switch：`skillRunner.enabled`

**一個 boolean 驅動整條鏈，全程 fail-closed**：

```
paas-cd 設 operator env AGENT_RUNTIME_SKILL_RUNNER_ENABLED=true（land 才設）
  → operator _build_install_values 顯式 --set skillRunner.enabled=<bool> 給 agent-runtime chart
    → 同一 chart 值同時驅動：
        ① 起 bash_bridge sidecar container
        ② agent container gate env=true → runtime 暴露 bash:execute
```

- **預設 `false`**（`extra/paas-operator/src/config.py` 的 `agent_runtime_skill_runner_enabled`）。未 flip
  前 `bash:execute` 不暴露、sidecar 不起——一切 fail-closed。
- **operator 顯式雙向 forward**（`agent_runtimes.py` 的 `values["skillRunner"] = {"enabled": ...}`，
  true/false 都設）。原因：sidecar-aware chart **預設 `skillRunner.enabled=true`**；若採「關閉就省略」，
  chart 預設值會贏 → sidecar 卡 ON、operator env 永遠關不掉。顯式設才讓 env 成為**雙向真正的 kill-switch**。
- **硬約束**：flip 時 agent-runtime image 必須 `≥1.4.0`（含 `bash_bridge`）+ chart `0.4.0` 到位，否則在
  舊 image 上開會 **crashloop**。paas-cd 用 fleet-drift-check + JCR HEAD 確認 image 已 publish 才 flip。
- **incident kill-switch**：翻 `AGENT_RUNTIME_SKILL_RUNNER_ENABLED=false` → **下次 spawn 生效**（非即時撲滅
  live pod；即時隔離走 L2 手段）。既有 pod 需 respawn 才套用新 config。

## 🧩 編排途中抓到的 4 個跨組件 gap（land 前已全閉）

1. operator 未 forward `skillRunner.enabled` → silent no-op → 補 operator forward。
2. chart-default-true 讓 operator env 失效 → 改 operator always-explicit（authoritative kill switch）。
3. operator↔chart env 名 lock-step → 讀碼驗 `AGENT_RUNTIME_SKILL_RUNNER_ENABLED` 一字不差。
4. agent container 缺 gate env → `bash:execute` 永遠 fail-closed no-op（Drop B 揭露）→ 補 agent-container env。

## ⚠️ 殘留風險（誠實標註，本期未解）

pod : workspace = 1:1 → pod 邊界即租戶邊界，**無跨租戶外洩**。但 bash 與 agent **同 network namespace**：
若 `externalEgress` 開到 LLM provider，untrusted bash 仍可循該路徑用偷到的 key 外送（異 UID 擋不住網路
向量；雲端 IMDS `169.254.169.254` 已被 NetworkPolicy except 擋）。IPC / Unix socket / `/dev/shm` 同 pod
不隔離。完整封死需 PID/NET/IPC 全隔離或 gVisor/Kata / 短命 sandbox pod（v2）。追蹤
`woow-paas/paas-agent-runtime#26`。詳見 [Skill 系統安全模型](09-security-model.md)。

## 📁 相關文件

- [Skill 運行時生命週期](05-skill-runtime-lifecycle.md) — `load_skill` / `bash:execute` 在生命週期的位置
- [agent-runtime Pod 內部](06-pod-internals.md) — sidecar 在模組地圖、執行序列
- [Agent Runtime Pod 部署機制](07-deployment.md) — operator 的 `skillRunner.enabled` forward
- [Skill 系統安全模型](09-security-model.md) — 異 UID / PID-ns 安全模型
