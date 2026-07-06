---
title: "#271 內網 MCP bypass — 防 regress 守衛設計"
date: 2026-07-06
issue: 271
status: approved
related:
  - "#301 operator 移除 MCP sidecar 公開 CF route（本設計上游）"
  - "#283 W0 驗證叢集 egress 公網 IP 非 Pod CIDR"
  - "#163 / #162 SA domain source of truth"
---

# #271 內網 MCP bypass — 防 regress 守衛設計

## 1. 背景與問題框架翻轉

issue #271 原始框架為「Security Access 內網自動 bypass」：當使用者的 CloudService
route 開啟 Cloudflare Access 保護（`is_protected=True`）後，內網消費端（agent-runtime
pod、Odoo MCP client）若「經該服務的公開 CF hostname」連線會撞 CF 登入頁 → 連線失敗。
issue 的原始待做傾向 seed 一條 `decision=bypass` + `rule_type=ip_range` 的 policy。

兩項查證翻轉了這個框架：

- **W0（#283）**：叢集對外 egress 公網 IP 為 `114.32.21.18`，**非** Pod CIDR
  （K3s 預設 `10.42.0.0/16`）。issue 自己標註的「預設填 Pod CIDR」會填錯值、對內網互通無效。
- **code-level 查證（本設計 §2）**：route (a)（內網走 in-cluster DNS、完全繞過
  Cloudflare）**已大致是現況**。

**因此 #271 的真正缺口不是「建 bypass 機制」，而是「沒有守衛擋內網 MCP url regress
成公開 hostname」。** 本設計採 route (a) 的正確落地：以防 regress 守衛釘死 invariant，
不做 CF seed policy（見 §7 非目標）。

## 2. 現況查證（Explore，2026-07-06）

| 查證點 | 結論 | 證據 |
| --- | --- | --- |
| agent pod 連 CS MCP | 逐字讀 `mcp_server.url`，無獨立指向公開邏輯 | `src/models/agent_session.py:454-459`（`_build_mcp_snapshot`） |
| Odoo chat 連 CS MCP | 同樣逐字讀 `mcp_server.url` | `src/models/ai_client.py:480-494` → `mcp_server._get_mcp_client_config()` |
| 部署 CS | **不自動建** mcp_server 列（只寫 auth token） | `src/models/cloud_service.py:481-486` |
| Add Sidecar prefill | `_compute_sidecar_url_hint` 給 **in-cluster** DNS | `src/controllers/paas.py:2747-2775` |
| 指向公開 hostname 的 code | 全是**無 caller 的 dead code**（public-first） | `src/services/paas_operator.py:846`（`get_mcp_endpoint_url`）、`src/models/cloud_service.py:978`（`_model_build_mcp_endpoint_url`） |
| seed bypass policy | grep 全 0 命中，issue 假設成立 | policy 只由使用者端點 `security_access.py:650,786` 建立 |

`mcp_server.url`（`src/models/mcp_server.py:21-25`，單一 `Char`）是唯一 source of
truth，兩個消費端都最終讀它逐字連線。「內網走哪條路」＝這格字串放的是 in-cluster DNS
還是公開 hostname。

**真正的實作缺口：**

1. **主缺口＝防 regress 守衛缺失。** `_validate_mcp_url`（`paas.py:53-81`）只擋
   non-http(s) scheme 與 cloud metadata IP，**放行任意 public https**。四個寫入端點
   （`paas.py:1386 / 1421 / 1720 / 1758`）容許使用者把 `sidecar_backed=True` 的 MCP url
   設／改成公開 hostname，且 `paas.py:1735-1737` 明文放棄 URL↔flag 一致性檢查。
2. **連帶缺口＝teardown 對稱性依賴 url 前綴。** `_sidecar_url_prefix`
   （`cloud_service.py:1365-1381`）＝ `http://{release}-mcp.{ns}.svc.cluster.local`，
   `_collect_sidecar_user_mcps`（`:1361-1362`）用 `url.startswith(prefix)` 收列；
   若 sidecar-backed 列帶公開 url，前綴不命中 → teardown 漏收 → 孤兒列。
3. **非缺口：** 無活路徑走公開（活路徑都 in-cluster）；route (a) 本就不需 seed
   bypass/ip_range policy（grep-0 成立）。指向公開的程式純屬 dead code。

## 3. Invariant

```
sidecar_backed = True  ⟹  url 為 in-cluster DNS
                          （hostname 以 '.svc.cluster.local' 結尾，且 scheme == 'http'）
```

`sidecar_backed = False` 的外部 MCP 不受此約束，仍可填任意 public https（維持現有彈性）。
此 invariant 與既有 `mcp_server.py:118-132` `_check_scope_project_consistency`（D4
invariant）、`:134-152` scope-flip `write()` guard 屬同一守衛家族，鏡像其寫法即可。

## 4. 設計

**共用判定 helper（避免 two-layer 漂移）：** in-cluster 判定（scheme + `.svc.cluster.local`
結尾）抽成**單一** helper（例如 `mcp_server._is_in_cluster_url(url)` classmethod），①model
constrains 與 ②controller validate 都呼叫同一份，避免兩層各寫一份判定造成 lockstep 漂移
（memory `two-layer-acl-lockstep`）。

### ① Model 層主守衛（對所有寫入含 sudo 生效）

於 `src/models/mcp_server.py` 新增 `@api.constrains('url', 'sidecar_backed')`
`_check_sidecar_url_in_cluster`，緊鄰既有 `_check_scope_project_consistency`：

```python
@api.constrains('url', 'sidecar_backed')
def _check_sidecar_url_in_cluster(self):
    # #271 invariant：sidecar-backed MCP 必走 in-cluster DNS，防 regress 成公開
    # hostname（撞 CF Access 登入頁 + teardown 前綴漏收孤兒）。
    for record in self:
        if not record.sidecar_backed:
            continue
        parsed = urlparse(record.url or '')
        host = (parsed.hostname or '').strip().lower()
        if parsed.scheme != 'http' or not host.endswith('.svc.cluster.local'):
            raise ValidationError(...)
```

**為什麼用 `@api.constrains` 而非 `write()` guard：** 這是 hard invariant（sidecar 就
該 in-cluster，對 admin/sudo 一視同仁），**不需 actor 豁免**。這剛好避開 memory
`su-env-exempt-guard-false-green` 陷阱——`write()` guard 內若寫 `not self.env.su` 豁免，
`TransactionCase`（su env）測試會跑不到 deny 分支（假綠）。`@api.constrains` 對所有
create/write 一致觸發，測試真的驗得到 sudo 路徑。與 scope-flip guard 分野：那條值可
gated 改（放行 admin 是對的），本條值對誰都不可為公開（任何豁免都是洞）。

### ② Controller 層 friendly error（defense-in-depth）

`_validate_mcp_url`（`paas.py:53`）加 `sidecar_backed: bool = False` 參數（或姊妹函式
`_validate_sidecar_mcp_url`）：`sidecar_backed` 為真時，url 非 in-cluster 即回 UI 友善
錯誤字串。四個寫入端點（`paas.py:1386 / 1421 / 1720 / 1758`）依各自 vals 的
`sidecar_backed` 旗標傳入。SSRF 黑名單與 `cluster.local` 允許註解（`paas.py:30-32`）
維持不動——與本守衛方向一致。

### ③ Teardown 對稱性

①② 保證 sidecar-backed 列 url 必 in-cluster 後，`_sidecar_url_prefix` 的 `startswith`
filter（`cloud_service.py:1361`）不再漏收。**此為 invariant 的自然結果，不需改
teardown 邏輯**，僅加測試守住對稱性。

### ④ Migration healer（既有壞列，決策：unset）

①的 constrains 上線後，既有「`sidecar_backed=True` + 公開 url」壞列的任何後續 write 會
炸。以防禦性 migration 先收斂：掃出壞列 → **`sidecar_backed = False`**（承認它實際是外部
MCP、脫離 CS 生命週期，保留使用者設定與 `tool_ids` 不刪資料）＋ log warn。正常情況
（prefill 給 in-cluster）應為 0 列，healer 有則修、無則 no-op——同 memory
`seed-noupdate` / `reaped-since` backfill 手法。migration 版號沿 `src/migrations/`
既有序列遞增。

### ⑤ Dead code 清理（決策：納入一併刪）

刪除兩段無 caller 的 public-first 建構邏輯，防未來誤 wire 重新引入 #301 曝險：

- `src/services/paas_operator.py:846-908` `get_mcp_endpoint_url`（已標 DO NOT WIRE）
- `src/models/cloud_service.py:978-999` `_model_build_mcp_endpoint_url`（無 caller）

刪除前再次 grep 確認 0 caller（防查證漂移）。

## 5. Residual risk（誠實揭露）

Model constrains 只驗「是 in-cluster」，無法驗「是**本** CS 的 sidecar」——mcp_server 僅
透過 `project_ids` 間接關聯，拿不到單一 `release`/`namespace`。若使用者刻意把 url 設成
**別的** CS 的 in-cluster sidecar（`other-release-mcp.other-ns.svc.cluster.local`），
constrains 放行、但 teardown 完整前綴比對不命中 → 該列變 teardown 孤兒。但這已**非公開
曝險**（仍在叢集內、受 NP 管），嚴重度遠低於原缺口，列為 residual 不在本次處理。

## 6. 測試計畫（TDD，RED→GREEN）

Odoo model / controller 測試（`tests/`）：

1. **model constrains**：`sidecar_backed=True` + 公開 url → `ValidationError`；
   + in-cluster url → 通過；+ 非 http scheme（https 的 in-cluster）→ 依 invariant 擋；
   `sidecar_backed=False` + 公開 url → 通過（外部 MCP 允許）。
2. **sudo 也擋**：以 `TransactionCase`（su env）建 `sidecar_backed=True` + 公開 url，
   斷言仍 raise——證明未落入 su-env 假綠。
3. **controller 4 端點**：create/update × mcp/project-mcp 傳 sidecar_backed + 公開 url
   → friendly error 字串。
4. **regress 案例**：既有 in-cluster sidecar 列，`_update` 改 url → 公開 → 擋。
5. **teardown 對稱**：sidecar-backed 列（保證 in-cluster）CS teardown 正確收、無孤兒。
6. **migration healer**：預置壞列 → 跑 healer → `sidecar_backed` 翻 False、資料保留。

## 7. 非目標（YAGNI）

- **不做 seed `ip_range` bypass policy**：route (a) 下內網不經 CF edge，此 policy 對內網
  互通無實際作用（W0 已證 egress IP 非 Pod CIDR）。
- **不做 CF Service Token**：機器端不經公開 hostname，無需 token。
- **不驗「本 CS」sidecar 身分**：model 拿不到單一 release/ns，交由 teardown 完整前綴
  filter 兜（見 §5 residual）。

## 8. 相關

- **#301**（operator 移除 MCP sidecar 公開 CF route）：本設計的上游。#301 斷了「部署自動
  建公開 route」，本設計斷「使用者手動把 url 設成公開」的最後一條 regress 路徑。
- **#283**：egress 公網 IP 證偽「填 Pod CIDR」。
- **#163 / #162**：SA domain source of truth（route hostname / domain 解析對齊）。
