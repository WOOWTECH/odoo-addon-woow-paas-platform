---
name: decision-agent-dataplane-in-cluster-no-cf-access-ip-bypass
status: accepted
created: 2026-07-04
updated: 2026-07-04
phase: 上線前驗證 gate #283（CF Access IP bypass 前提驗證，硬前置 #271）
team: repo-leader（woow-paas-platform）
deciders:
  - repo-leader（本 gate 驗證 + leader spot-verify）
  - docs-leader（W0 排序 owner）
related:
  - src/models/cloud_service.py
  - src/controllers/paas.py
  - src/controllers/ai_assistant.py
  - docs/decisions/namespace-prefix-enforcement-app-layer-not-rbac.md
issues:
  - "#271（W2：agent→CS MCP 互通撞 CF Access is_protected 層 — 本 gate 解鎖，收斂 route (a)）"
  - "#279（W1：NetworkPolicy egress + workspace-id label 的 in-cluster MCP dead-path — 與本 gate 兩層區分、勿混判）"
  - "#280（W1：cross-tenant default-deny NetworkPolicy）"
  - "#284（姊妹 W0 gate：operator RBAC 不 enforce ns prefix）"
---

# Decision：agent 資料面全 in-cluster，#271 走 route (a)（in-cluster DNS），不開 CF Access IP bypass

> 適用：任何要處理 #271（叢集內服務撞 CF Access 登入頁）、或提議用「填 IP 當 CF Access bypass 白名單」讓內網服務免 Access 的人。

## TL;DR（結論）

1. **「填 Pod CIDR（K3s `10.42.0.0/16`）當 CF Access bypass 白名單」的技術前提不成立** — CF Access 的 IP 規則比對的是 **CF edge 看到的 `cf-connecting-ip`**（＝叢集公網出口 IP），不是叢集內 Pod IP。Pod 封包出叢集經 LAN gateway SNAT，源 IP 已被改寫，CF 端**永遠看不到** `10.42.x.x`。
2. **#271 應走 route (a)：讓叢集內服務走 in-cluster DNS（`*.svc.cluster.local`），完全繞過 CF edge** — 這**本就是現行設計**；agent 資料面（agent↔CS MCP、Odoo↔agent WS、agent→Odoo）**全部 in-cluster、無任何路徑經公開 CF hostname**，故 CF Access（#271 層）對叢集內互通根本不介入。#271 若真發生＝某條路徑被誤配成走公開 hostname，正解是**改回 in-cluster DNS，不是開 IP bypass**。
3. **不採 route (c)（填叢集出口公網 IP）** — 粗粒度、安全降級、脆弱，且解一個對 agent 資料面不存在的問題（見「三路線比較」）。

## Ground truth（dev-sandbox-stg 真流量實測，woow-k3s，2026-07-03）

**A. CF edge 看到的來源 IP = 叢集公網出口 IP，非 Pod IP**

從 dev-sandbox-stg pod 內 `curl https://www.cloudflare.com/cdn-cgi/trace`：

```
ip=114.32.21.18   colo=TPE   loc=TW   warp=off   http=http/2   tls=TLSv1.3
```

- `114.32.21.18` 屬 HINET／中華電信公網段，是**公網出口 IP**，**非** Pod CIDR（`10.42.x.x`）。
- 4 節點（ubuntuserver-31zix／-59mqj／-pggrg／-z0v3w）各釘測一次，**皆同一出口 IP**（測時無多 WAN／SNAT pool 分歧）。節點本身 `EXTERNAL-IP <none>`、私網 `192.168.10.0/24` → 對外必經 LAN gateway SNAT。

**B. 冒煙：受保護 hostname 撞 Access（pod 出口 IP 不在任何 bypass）**

pod curl 2 個 stg `is_protected` route（`paas-stg-1` / `paas-stg-test1.woowtech.io`）→ `HTTP/2 302` → `woowtech.cloudflareaccess.com/cdn-cgi/access/login/…`，JWT meta `auth_status=NONE`、`service_token_status=false`。→ pod 出口 IP `114.32.21.18` 確實在 CF edge 被 Access 攔到登入頁。

→ **填 Pod CIDR 當 bypass 白名單永遠 match 不到。** 會議 6c 前提否證（RFC1918 私網經 SNAT 改寫，CF 端看不到 `10.42.x.x`）。

## 為什麼 agent 資料面不經 CF edge（route (a) 的依據，leader 唯讀 spot-verify）

| 路徑 | 解析 | 依據 |
|---|---|---|
| agent pod → CS MCP | `http://{release}-mcp.{ns}.svc.cluster.local:{port}{path}`（in-cluster） | `src/models/cloud_service.py:984-989`（有 helm release/ns 時一律 in-cluster；公開 subdomain 只是**無 helm release** 的 fallback，`:991-996`） |
| （同上，controller hint） | 同 in-cluster URL | `src/controllers/paas.py:2747-2762`，**明文註解**「Cloudflare routes the main app port only, not the sidecar … external URL via subdomain won't work for the MCP sidecar」 |
| Odoo → agent pod（chat WS） | operator 回傳的 `pod_ws_url`（internal svc） | `src/controllers/ai_assistant.py:782-820` |
| agent pod → Odoo（skill/webhook） | operator env `ODOO_BASE_URL` = internal svc | stg 已驗證 |

**關鍵洞察（2026-07-05 W0 覆核校正）**：agent↔CS MCP 互通走 in-cluster DNS（`cloud_service.py:985-990`：有 helm release/ns 一律回 `svc.cluster.local`），**不經 CF edge**，故 CF Access 對 agent 資料面不介入——這才是 route (a) 成立的實質依據。⚠️ 但原始論據「MCP sidecar 埠**根本不被** CF tunnel 路由」**經活叢集實測證明為錯**（見下方校正段）：operator 在 CF-enabled 時**確實會**替 sidecar 建 `{subdomain}-mcp` 公開 route 且已 live。route (a) 靠的是 **agent 端不使用該公開 URL**（Odoo 丟棄 operator 回的 `mcp_endpoint_url`、預填走 in-cluster），不是「架構上不可能經 CF」。

## ⚠️ 校正（2026-07-05 W0 覆核活叢集複驗，issue #301）

原 doc「CS 的 MCP sidecar 埠不被 CF tunnel 路由 / 公開 hostname 對 MCP 本就不通」的論據，經活叢集實測**推翻**：

- operator `_create_sidecar_service_and_route`（`extra/paas-operator/src/api/releases.py:594-600,719`）在 CF-enabled 時**無條件**替 MCP sidecar 建 `{subdomain}-mcp.{cloudflare_domain}` 公開 route；stg 實測 `curl https://svc-…-mcp.woowtech.io/` → `HTTP/2 200`（origin 直達，非 302 到 Access）。
- 該公開 route **未被 CF Access 納管**（operator 建 route 不掛 Access policy；SecurityAccess `is_protected` 對它零認知）、**NP 亦刻意放行** cloudflared→sidecar（#280 baseline `allow-cloudflared` 無 port filter）→ 實際保護**只剩 sidecar 自身強制 bearer token**（`/mcp` 無 token→401；但 `/` `/health` 裸露 `n8n-mcp` 版本指紋）。
- 此公開 route 對功能為**死重**（agent 走 in-cluster、Odoo 丟棄該 URL）。

**本 doc 結論不變**（agent 資料面全 in-cluster、#271 走 route (a)、不開 IP bypass）——agent 用 in-cluster DNS、不依賴這條公開 route。但**不要再用「sidecar 埠不被 tunnel 路由 / 公開 hostname 本就不通」當論據**（`src/controllers/paas.py:2747-2762` 同源錯誤註解已一併校正）。該死重公開 route 的移除追蹤見 **#301**。

## #271 vs #279：兩層症狀，勿混判

- **#279**（W1）：`cloud_service.py:984-989` 回的 in-cluster MCP URL，被 **NetworkPolicy egress + workspace-id label** 擋成 dead-path。**與 CF 無關**（根本不經 CF edge）。
- **#271**（W2）：SecurityAccess `is_protected` 層 — 「經公開 CF hostname 連線撞 Access 登入頁」。

兩者是「叢集內 agent→CS MCP 互通被擋」的**不同層**，修法不同：#279 修 NetworkPolicy／label；#271 修「別把該路徑配成走公開 hostname」。混判會導致對 #271 開錯藥（IP bypass）而完全沒解到 #279。

## 三路線比較（#271）

| 路線 | 評價 | 說明 |
|---|---|---|
| **(a) in-cluster DNS 直連，繞過 CF** | ✅ **推薦** | 即現行設計。零 CF 依賴、零 bypass 攻擊面。成本：audit 確保無路徑 regress 成公開 hostname（多數已是 in-cluster）。 |
| (b) CF Service Token | ◐ 條件性 | 僅適用「in-cluster client 確實必須經 CF edge 到受保護服務」的少數情況；可行、不依賴來源 IP。成本：provision + 注入 token header；且 SA policy-rule model 當初刻意移除 Service Token selector（`docs/plans/2026-Q2/2026-06-13-sa-policy-rule-submodel-design.md:163`），加回是額外工。 |
| (c) 填叢集出口公網 IP（`114.32.21.18`） | ❌ **最不推薦** | 粗粒度 bypass：**同 NAT 後所有**流量（4 節點所有 pod、含被入侵租戶 pod、甚至其他機器）全繞過 Access = 安全降級；且脆弱（IP 非 static／多 WAN 即失效）；且解一個對 agent 資料面不存在的問題。 |

## 內網 bypass 語意釐清 + 既有 `ip_range` 機制的正確用途（給 #271 實作者）

W0 handoff 要求分清兩種「bypass」語意——**它們設計完全不同、勿混判**：

| 語意 | 正解 | 填什麼 |
|---|---|---|
| **內網服務互打**（agent↔CS MCP、Odoo↔agent） | in-cluster DNS（route a），**根本不經 CF edge、Access 不介入** | 不填任何 IP（無需 Access policy） |
| **外部可信來源免逐次登入**（辦公室固定 IP / VPN 出口） | CF Access `ip_range` allow（正當、既有機制） | **該外部來源的公網 IP**，非叢集出口 IP |

operator 的 CF Access policy model **已支援** `rule_type="ip_range"`（`extra/paas-operator/src/services/cloudflare.py:1537`、`schemas.py:510`：`email_domain | email_list | ip_range | everyone | country`）。**本決策不動它**——route (c) 駁回的是「把**叢集出口公網 IP**（`114.32.21.18`）塞進 `ip_range` 當**內網**服務 bypass」（粗粒度、全 NAT 共用、安全降級）；而 `ip_range` 機制本身對**外部 edge 來源** allowlist 是正當用途，兩者勿混。

→ **#271 scope 收窄**：**不新增**把叢集出口 IP 當內網 bypass 的 Access policy、**不 seed** 類 `INTERNAL_BYPASS_CIDR`；既有 `ip_range` 保留給外部 edge 來源的正當場景。

## 出口 IP 定值性（僅 route (c) 才需要，故 moot）

`114.32.21.18` 是否 static／單 WAN／有無 SNAT pool，**無法從叢集內確定**，需 woow-operator 以站點實體網路知識確認。但 route (c) 既不推薦，此問題對本決策為 **moot**；僅在未來重新考慮 (c) 時才需 recruit woow-operator 釐清（含實測到的出口 IP 值 `114.32.21.18`）。

## 關聯

- **#271**：本 gate 解鎖 → 收斂 route (a)。
- **#279**：兩層區分（in-cluster NetworkPolicy 層，與 CF 無關）。
- **#280**：W1 cross-tenant default-deny NetworkPolicy — 網路層縱深。
- **#284**（姊妹 W0 gate）：operator RBAC 不 enforce ns prefix（`docs/decisions/namespace-prefix-enforcement-app-layer-not-rbac.md`）。
- memory `reference_woow_k3s_cf_access_egress_ip`：叢集出口 IP `114.32.21.18` 實測持久化。
