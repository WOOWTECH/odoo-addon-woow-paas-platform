---
title: Skill Bundle 與 JWT 認證
category: guides
status: active
created: 2026-05-25
updated: 2026-05-25
tags: [paas, skill-system, skill-bundle, jwt, security]
related:
  - README.md
  - 03-skill-design.md
  - 09-security-model.md
---

# Skill Bundle 與 JWT 認證

> Skill Bundle 是 Skill 的封包格式；Pod 透過 `SKILL_BUNDLE_TOKEN`（JWT）向 Odoo 驗證身份並下載。對應程式碼：`src/controllers/agent_runtime_skill.py`、`extra/paas-operator/src/services/jwt_signer.py`。

## 🌐 下載總流程

```mermaid
sequenceDiagram
    participant Pod as agent-runtime
    participant Odoo as Odoo 端點
    participant DB as Skill 表
    participant ATT as ir.attachment

    Pod->>Pod: 從 SKILL.md 取 skill_name + version
    Pod->>Odoo: GET /api/agent-runtimes/skill-bundle<br/>?skill_name=&version=（Bearer JWT）
    Odoo->>Odoo: _verify_skill_bundle_token()<br/>① HS256 驗章（CURRENT→PREVIOUS）<br/>② exp 未過期<br/>③ token_use == "skill_bundle"<br/>④ project_id 取自 JWT（忽略 URL）
    Odoo->>DB: 查 project_id + name + version + state='published'
    DB-->>Odoo: skill record
    Odoo->>ATT: 讀 bundle_attachment_id（base64 decode）
    Odoo-->>Pod: 200 raw .tar.gz bytes
    Pod->>Pod: sha256 校驗 → 解壓 → 快取
```

> route 是 `GET /api/agent-runtimes/skill-bundle`（`agent_runtime_skill.py:86-89`），回傳的是 **raw tar.gz bytes**，不是 JSON 包裝。

## 🔐 JWT 宣告（claims）

operator 的 `sign_skill_bundle_token()` 簽出（`jwt_signer.py:94-119`），演算法 **HS256（純 stdlib，無 PyJWT）**，效期 **3600 秒（1h）**：

| Claim | 值 | 用途 |
|---|---|---|
| `iss` | `"paas-operator"` | 簽發者識別 |
| `aud` | `"agent-runtime"` | 接受者驗證 |
| `token_use` | `"skill_bundle"` | **跨 scope 防禦**：ws token 不可拿來打此端點 |
| `project_id` | `int` | **權威身份**：Odoo 以此查庫，URL 參數忽略 |
| `workspace_ids` | `list[int]` | Pod 的 NetworkPolicy egress 白名單 |
| `project_hash` | `str` | namespace / release 命名 |
| `iat` / `exp` | ts | 簽發 / 到期（`exp = iat + 3600`） |
| `jti` | uuid hex | 唯一識別，供 **operator 端** replay 防禦 |

## ✅ 下載端點的驗證步驟

`_verify_skill_bundle_token()`（`agent_runtime_skill.py:62-80`）+ 後續檢查：

1. **HS256 驗章** — 先試 `AGENT_RUNTIME_AUTH_SECRET_CURRENT`，失敗再試 `..._PREVIOUS`
2. **exp** — 過期即拒
3. **`token_use == "skill_bundle"`** — 否則 403（`:105`）
4. **`project_id` 取自 JWT** — 不信任 URL 的 `project_hash`（`:109`，HC-2）
5. **查庫** — `project_id + name + version + state='published'`（`:113-119`）
6. **回傳** — 讀 `bundle_attachment_id`、base64 decode、回 raw bytes（`:125-133`）

## 🔑 雙 Secret 輪換

```mermaid
sequenceDiagram
    participant Pod
    participant Odoo as Odoo /skill-bundle

    Note over Pod,Odoo: 平常：operator 用 CURRENT 簽
    Pod->>Odoo: Bearer（CURRENT 簽的 JWT）
    Odoo->>Odoo: 試 CURRENT → 成功 ✓

    Note over Pod,Odoo: 輪換期：Pod 還拿著 PREVIOUS 簽的舊 JWT
    Pod->>Odoo: Bearer（PREVIOUS 簽的 JWT）
    Odoo->>Odoo: 試 CURRENT → 失敗，試 PREVIOUS → 成功 ✓
```

換 secret 時不必同時更新所有 Pod：新的用 CURRENT 簽，在途的舊 token 仍可被 PREVIOUS 驗過，達成零停機輪換。

## 🔎 jti replay 防禦在哪？（常見誤解）

> **不在這個下載端點，而在 operator。** `jwt_signer` 用一個 process 內的字典 `_JTI_SEEN` 記錄看過的 jti，TTL = **65 分**（1h token + 5 分時鐘偏移），lock 保護（`jwt_signer.py:33-35,177-179`）。Odoo 的 `/skill-bundle` 端點本身**不查 jti**，只做上面 6 步。完整安全分層見 [Skill 系統安全模型](09-security-model.md)。

## 🚫 安全檢查點小結

1. **簽章** — HS256（CURRENT→PREVIOUS）
2. **token_use** — 防 ws token 盜用 → 403
3. **project_id 權威** — URL 的 project_hash 完全忽略
4. **state=published** — 只供應已發布版本
5. **jti replay** — 由 operator 端把關（非此端點）

## 📁 相關文件

- [PaaS Skill 系統總覽](README.md)
- [Skill 設計與發布](03-skill-design.md) — bundle 怎麼產生
- [Skill 運行時生命週期](05-skill-runtime-lifecycle.md) — Pod 拿到 bytes 後做什麼
- [Skill 系統安全模型](09-security-model.md) · [PaaS Skill 術語表](11-glossary.md)
