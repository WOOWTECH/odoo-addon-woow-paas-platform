---
title: Skill 系統安全模型
category: guides
status: active
created: 2026-05-25
updated: 2026-05-25
tags: [paas, skill-system, security, threat-model]
related:
  - README.md
  - 10-roadmap-phases.md
  - ../deployment/agent-runtime-pilot-flip-gate-runbook.md
---

# Skill 系統安全模型

> 把散落各篇的安全機制整合成一張威脅地圖。核心假設：**Pod 是半信任的**——它可能被攻擊者影響，所以每道介面都要能獨立把關。

## 🛡️ 防禦分層

```mermaid
graph TB
    subgraph L1["🔑 身份與簽章"]
        A1["HS256 雙 secret 輪換"]
        A2["token_use 分流（C-1）"]
        A3["project_id 權威（HC-2）"]
        A4["jti replay（operator + Pod）"]
    end
    subgraph L2["🌐 網路"]
        B1["NetworkPolicy egress 白名單<br/>（workspace_ids）"]
        B2["url_validator SSRF guard（Pod）"]
    end
    subgraph L3["🗄️ 資料"]
        C1["bundle attachment<br/>row-level ACL + anchor stamping"]
        C2["namespace secret fail-closed<br/>（D-CAVEAT-1）"]
        C3["owner-bootstrap SUPERUSER-only<br/>（F-PA-14）"]
    end
```

## 🔑 雙 Secret 輪換（簽章與 webhook 各一組）

```mermaid
graph LR
    subgraph 簽章["Token 簽/驗"]
        S1["AGENT_RUNTIME_AUTH_SECRET_CURRENT"]
        S2["AGENT_RUNTIME_AUTH_SECRET_PREVIOUS"]
    end
    subgraph Webhook["Webhook 驗章"]
        W1["AGENT_RUNTIME_WEBHOOK_SECRET（裸名）"]
        W2["AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS"]
    end
    NEW["新訊息"] -->|"用 CURRENT 簽"| S1
    OLD["在途舊訊息"] -->|"用 PREVIOUS 驗"| S2
```

驗證一律「先 CURRENT、失敗再 PREVIOUS」，所以換 secret 不必同時更新所有 Pod，零停機。

## 🎯 核心防禦原語

| 代號 | 機制 | 防什麼 |
|---|---|---|
| **C-1** | `token_use` 斷言（`ws` ≠ `skill_bundle`） | 拿 ws_token 盜打 `/skill-bundle` → 403 |
| **HC-2** | `project_id` 以 **JWT 為準**，忽略 URL | 改 URL 參數越權讀他人 bundle |
| **jti replay** | operator（`_JTI_SEEN`）+ Pod（`jti_cache`）各一個，TTL 65 分 | 重放擷取到的 token |
| **NetworkPolicy** | egress 白名單 = `workspace_ids` | Pod 橫向打其他 workspace |
| **url_validator** | Pod 建 MCP client 前驗 URL，拒 link-local / IMDS | Pod 內 SSRF |
| **D-CAVEAT-1** | namespace secret **fail-closed** | secret 未設時退化成弱 hash（改為直接 `UserError`） |
| **F-PA-14** | owner-bootstrap hint 僅 SUPERUSER 讀 | 透過 HTTP 注入 context 冒充 owner |
| **attachment ACL** | row-level ACL + anchor stamping | 跨專案讀取 bundle 二進位 payload |

## 🧨 威脅 → 對策

```mermaid
graph LR
    T1["偽造 token"] --> M1["HS256 + HC-2"]
    T2["跨 scope token 盜用"] --> M2["token_use C-1 → 403"]
    T3["token 重放"] --> M3["jti blocklist（operator + Pod）"]
    T4["Pod 橫向移動"] --> M4["NetworkPolicy egress"]
    T5["Pod 內 SSRF"] --> M5["url_validator 擋 link-local/IMDS"]
    T6["弱 namespace hash"] --> M6["fail-closed UserError"]
    T7["冒充 owner"] --> M7["SUPERUSER-only hint"]
    T8["越權讀 bundle"] --> M8["attachment row-level ACL"]
```

## ⚠️ 跨 repo 部署順序風險（D-CAVEAT-2 / F-PC-7）

`token_use` 的嚴格驗證在 Pod 端（`paas-agent-runtime` 的 F-PC-7，已實作於 `runtime/transport/auth.py`）。**翻開 enable-gate 前，必須先把 F-PC-7 merge 到 main 並部署**，否則存在 1 小時在途 token 的空窗。狀態追蹤見 [Agent Runtime 演進史 (Phase A~G)](10-roadmap-phases.md)；實際翻 gate 步驟見 repo 的 `docs/guides/deployment/agent-runtime-pilot-flip-gate-runbook.md`。

## 📚 本 repo 對應 pattern（`docs/decisions/patterns/`）

- `constraint-input-trust-expansion.md` — 對應 HC-2（輸入信任邊界）
- `attachment-row-level-acl-and-anchor-stamping.md` — bundle 附件兩層防護
- `credential-claim-test-fixture-coupling.md` — credential 硬化後 test fixture 的連動紀律

## 📁 相關文件

- [PaaS Skill 系統總覽](README.md)
- [Skill Bundle 與 JWT 認證](04-skill-bundle-jwt.md) — 下載端點驗證
- [Webhook 回呼與 ws_token](08-webhook-ws-token.md) — 回呼簽章
- [agent-runtime Pod 內部](06-pod-internals.md) — Pod 端 auth / jti / url_validator
- [Agent Runtime Pod 部署機制](07-deployment.md) · [Skill 設計與發布](03-skill-design.md) · [PaaS Skill 術語表](11-glossary.md)
