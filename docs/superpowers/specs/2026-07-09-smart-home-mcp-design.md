---
title: Smart Home MCP（ha_mcp_tools 方案）設計
date: 2026-07-09
status: draft
related_issues:
  - "#270"
supersedes: "#270 原 MCP Connect 設計（2026-07-07：官方 mcp_server ＋固定 subdomain / 平台 proxy token gating ＋CF Access 疊層）"
repos:
  - woow_paas_platform（平台，本 repo）
  - woow_paas_smart_home（HA custom component，另 agent 的 repo）
---

# Smart Home MCP（ha_mcp_tools 方案）設計

## 1. 背景與 pivot

會議（2026-07-01）要 Smart Home 在既有 remote-access 之外新增「MCP」加值檔（150 純 remote access／400 remote access ＋ MCP）。#270 於 2026-07-02/07 的原設計走「官方 `mcp_server` ＋固定 mcp_subdomain/port ＋平台下發 proxy token gating ＋（v2）CF Access 疊層」，卡在**平台沒有 enforcement 點可把 MCP 當獨立方案收費**（資料面 hostname-only 直通、remote-access 客戶事實上已能自打 HA MCP），因此 pending。

2026-07-08 Elmo 對齊討論（`0-0.footage/meeting/docs/2026-07-08 smart home 的  mcp 規畫討論.jpg`）把方案**大幅簡化並定案**：

- MCP 底層改採 HA **社群** integration [`ha_mcp_tools`](https://github.com/homeassistant-ai/ha-mcp-integration)（B 案），走 **webhook Connect URL**（`https://<ha-domain>/api/webhook/<webhook-id>`），憑證＝祕密 URL 本身（或選配 `ha_auth`）。
- 平台**不做**任何 provision / token / route，只「記錄有沒有 MCP plan」並把旗標交給 HA component。
- HA component 收到旗標後，去看該 HA 上 `ha_mcp_tools` 在不在，若在就建一顆 HA sensor entity 反映狀態。
- Enforcement **軟性、不擋自灌**：HA 主機由我方販售、面向一般（非工程師）用戶，自行 HACS 裝 `ha_mcp_tools` 白嫖的風險可接受（Eugene 拍板）。

**原 #270 的固定 subdomain/port、平台 proxy token、CF Access 疊層設計全部作廢**；#270 note 段落中 2026-07-07 的技術塊多數 superseded，僅保留考古。

## 2. 產品模型

| 檔 | 內容 | plan `mcp_integration` |
| --- | --- | --- |
| 150 | 純 remote access（現有 Smart Home 方案） | `""`（空） |
| 400 | remote access ＋ MCP server | `"ha_mcp_tools"` |

- 400 為**獨立 plan**（客戶選 150 或 400，切換走既有 `change_plan` — #318/#322 已原子化＋按比例退點）。不是「150＋250 兩張訂閱疊加」。
- MCP 走既有 per-SH Cloudflare tunnel（HA 8123 上的一條 path）→ **零新 infra**。
- 「MCP ＝ `ha_mcp_tools`」以**字串 discriminator** 表達，未來若換/新增 MCP 後端只改字串、component 依字串分支，schema 不動。

## 3. 架構與資料流

```
                 (1) plan 旗標                     (2) 偵測 ha_mcp_tools
  Odoo 平台 ──────────────────────▶ woow_paas_smart_home ──────────▶ HA (ha_mcp_tools)
  sh.subscription.plan            (HA component 本來就會拉               │
  .mcp_integration                 home payload)                        │ (3) 建 HA sensor entity
        │                                                               ▼
        └── smart_home.to_dict() 回 `mcp: "<integration>"`        「MCP Server」sensor（HA 本地）
```

- 單向：平台 → component（`mcp` 字串）。**無反向 channel**，平台不回讀 sensor。
- MCP client（Claude.ai 等）連的是 HA 的 webhook Connect URL，經既有 tunnel → 使用者家 HA 8123；**平台不在資料面**。

## 4. 平台側（本 repo）— 只改兩處

### 4.1 plan 加 `mcp_integration` 欄位（採方案 ①：flat，不引 discriminator）

新增 `src/models/sh_subscription_plan_sh_mcp.py`，比照既有 `sh_subscription_plan_sa_quota.py`（`_inherit` classical extension、只加欄位、不覆寫 std 欄位型別）：

```python
from odoo import fields, models


class ShSubscriptionPlanShMcp(models.Model):
    """Smart Home MCP capability on subscription plans.

    Follows the odoo-std-field-no-override pattern (add-only), mirroring
    sh_subscription_plan_sa_quota. The value NAMES which HA MCP integration
    the plan grants, so the HA component can branch on it. Empty = no MCP.
    """

    _inherit = 'sh.subscription.plan'

    mcp_integration = fields.Char(
        string='SH MCP Integration',
        default='',
        help="Smart Home: which HA MCP integration this plan grants "
             "(e.g. 'ha_mcp_tools'). Empty = no MCP. Read by the HA "
             "woow_paas_smart_home component to decide whether to expose "
             "the MCP sensor entity.",
    )
```

- 字串（`Char`）而非 `Selection`：對齊 Elmo 的「`mcp: string`」與「可擴充 discriminator」意圖；component 端做 `mcp === "ha_mcp_tools"` 比對。（若日後要 UI 下拉＋值驗證可改 `Selection`，屬非破壞性演進。）
- 記得在 `src/models/__init__.py` 加 import。
- **這是全 `sh.subscription.plan` 共有的 sparse 欄位**（與現有 `sa_max_*` 同性質）；服務區隔的 discriminator 留待 ② refactor（見 issue-for-prod note ⑤、與定價 seed 重定同波做）。

### 4.2 `smart_home.to_dict()` 多回 `mcp` 字串

`src/models/smart_home.py`（既有 `to_dict()`，約 line 320）新增一個 key：

```python
def _get_mcp_integration(self):
    """Return the MCP integration string granted by the active plan, or ''."""
    self.ensure_one()
    plan = self.subscription_id.sh_subscription_plan_id if self.subscription_id else False
    return (plan.mcp_integration or '') if plan else ''

# in to_dict():
#   'mcp': self._get_mcp_integration(),
```

- 推導路徑沿用 SA 既有 pattern（`sub.sh_subscription_plan_id.<field>`，見 `security_access.py` 的 `sa_max_routes` 用法）。
- 無訂閱／非 MCP 檔 → `""`。
- **`mcp` 同時掛在 component 兩個可能的同步入口**，消除「掛哪」的相依（component 無論走哪條都拿得到、spec 不依賴另一 repo 現況）：
  1. `smart_home.to_dict()`（home detail，如上）。
  2. `ha_api.py` 的 `get_tunnel_token()`（`/api/smarthome/homes/<id>/tunnel-token`）——該端點**目前只回 `tunnel_token / tunnel_id / subdomain`（`ha_api.py:247`），需新增 `mcp` key**（同一 `_get_mcp_integration()` 推導）。`mcp` 為非敏感字串，與既有回傳並存無虞。

### 4.3 平台**不做**的事（明列以防範圍蔓延）

不 provision MCP、不產/保管任何 token、不開新 port/subdomain/route、不改 tunnel ingress、不回讀 sensor、不做硬 enforcement。

## 5. HA component 側（`woow_paas_smart_home`，另 agent 的 repo）— sensor 契約

> 本節是**跨 repo 契約**，由 `woow_paas_smart_home` agent 實作；平台側只保證送出 §4.2 的 `mcp` 字串。實際 entity id／state 機器值／HA API 偵測手法由該 agent 定案。

component 每次同步都把 sensor **對齊（reconcile）到當前 `mcp` 值**——冪等的期望狀態收斂，不是一次性建立：

- `mcp === "ha_mcp_tools"` → **確保存在**一顆 HA **sensor entity**，語意三態：
  - 偵測不到 `ha_mcp_tools` integration → **未安裝**
  - 已安裝、未啟動 → **已安裝未啟動**
  - 已安裝且運作中 → **運作中**
- `mcp` 為 `""`（或缺欄位）→ **確保不存在** sensor entity：**若先前（400 檔時）建過，須移除**——降頻 400→150、退訂、換非 MCP 檔都走這條。若使用者自行 HACS 裝了 `ha_mcp_tools` 但無 plan 旗標，與我方無關、不偵測不建。
- 平台 `mcp` 每次同步都廣播當前值（§4.2），故降頻時 component 會看到 `mcp` 由 `"ha_mcp_tools"` 轉 `""` 並據此移除 sensor——**平台不需額外「取消」信號**。
- sensor 純 **HA 本地** entity，供客戶／維運檢視；**平台不回讀**。

## 6. Scope out / 延後

- **② `service_type` discriminator**：`sh.subscription.plan` 的服務/功能區隔（讓 `mcp_integration`、`sa_*`、未來 CS backup/email 各歸其服務）留待未來 refactor，本 spec 採 ① flat。記於 issue-for-prod note ⑤。（② 動工時需自帶 spec/ADR，明確 discriminator ↔ view 顯隱／欄位適用哪個服務的對應；本 spec 不預先設計。）
- **定價金額 / seed 重定**：150/400 實際數字、Monthly/年繳四選項、400 plan record 的建立走另一條「定價 seed 重定」P0 軌（見 note ④）。本 spec 只定「兩檔結構＋`mcp_integration` 欄位語意」。為可測試，至少需一張帶 `mcp_integration="ha_mcp_tools"` 的（demo/test 或正式）plan——與定價軌協調。
- **硬 enforcement / 防自灌 / 平台讀 sensor**：不做（Eugene 拍板軟性）。
- **換檔退點**：沿用既有 `change_plan`（#318/#322），本 spec 不重做。

## 7. 驗收準則

平台側（本 repo，可自動化測）：

1. `sh.subscription.plan` 有 `mcp_integration` 欄位、default `""`、不覆寫任何 std 欄位。
2. 一張 `mcp_integration="ha_mcp_tools"` 的 plan → 綁該 plan 的 smart_home → `GET /api/smarthome/homes/<id>` 的 payload 含 `mcp: "ha_mcp_tools"`。
3. plan `mcp_integration=""` 或無訂閱 → payload `mcp: ""`。
4. 既有 remote-access smart home 行為不變；無新增 tunnel/route/token/port。

component 側（契約，`woow_paas_smart_home` agent 驗）：

5. 收到 `mcp="ha_mcp_tools"` → 出現 MCP sensor entity，三態隨 `ha_mcp_tools` 實況正確。
6. 收到 `mcp=""` → 無 MCP sensor entity——**含先前為 400 檔建立、降頻/退訂後 `mcp` 轉 `""` 的移除路徑**（§5 reconcile 對稱性）。

## 8. 協調事項（實作前）

**❗ Blocking 前置（recruit component agent 前需確認，否則 component 會基於錯誤假設實作）**：

- component 至少讀 `to_dict()` 或 `get_tunnel_token()` 其一、能拿到 `mcp`——**兩處平台都已掛（§4.2），故不阻塞平台實作**，但需確認 component 端確實消費。
- component 對「`mcp` 由 `"ha_mcp_tools"` → `""`」的 **sensor 移除**（降頻/退訂）確實實作（§5 reconcile 對稱性）。
- HA 版本 gate：`ha_mcp_tools` 最低 HA 版本、HACS 可得性。
- 是否已有 sensor 基礎設施可沿用（SA FR-9 sensor pattern）。

**非阻塞協調**：

- **定價軌**協調 400 plan record（承載 `mcp_integration="ha_mcp_tools"`）與價格數字。

<!-- peer-reviewed: 2026-07-09T06:33:08Z rounds=2 verdict=approved by=miniclaude -->
