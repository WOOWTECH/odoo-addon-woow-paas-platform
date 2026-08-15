# CS Access Control：cloud service 重用 SA Access 機制（design）

> 狀態：拍板完成（2026-08-15 Eugene 十題拍板；本檔＝grilling 收斂產物）。
> 來源：#521 三層失效鏈分析（2026-08-15 comment）＋ SA 內部結構探查（同日，file:line 坐實）。
> Refs：#521（主 issue，本案即其「載體」缺口的補課）、#381（enforcement 計數基準上位決策，本案 out of scope）、#352（防繞過守衛）、#271/#301（in-cluster dataplane 與 -mcp 未納管）、#739（同構權限缺陷，驗證項）。

## Goal

CS（cloud service）訂閱買了 AC 席位（plan `ac_max_access_seats > 0`）後，CS detail 頁出現 **Access 頁籤**，可設定 CF Access 保護（整站開/關＋policy 管理），底層重用 SA 既有 policy/rule 機制與 operator access sync。**席數 enforcement 不在本案**——留在 #521 等 #381 拍板。

同時治掉擋路缺陷：`_check_policy_quota` 無 0 豁免（CS access 變體 `sa_max_access_policies=0` ⇒ 第一條 policy 就 `ValidationError`）。

## 拍板決策（2026-08-15，Eugene）

| # | 題 | 拍板 |
|---|---|---|
| 1 | 範圍 | **MVP＝可用性**（載體＋UI＋quota 修正）；席數 enforcement out of scope，留 #521 等 #381 |
| 2 | 載體形態 | **入口長在 CS 上、SA 無感**：CS 自己長 access 設定，重用 policy/rule model＋operator sync（錨點＝CS subdomain）；**不建 SA 實例**（SA route 的 DNS 機制會把 CNAME 改指 SA 專屬 tunnel，CS 流量走平台共用 tunnel，掛進去即斷線——「隱形 SA」是死路） |
| 3 | 超額行為（將來 enforcement） | **擋寫入**（`ValidationError`，與既有 quota 檢查同款）；不做自動計費 |
| 4 | policy 數量限制 | **`_check_policy_quota` 改 0=unlimited**（與同檔 `_check_emails_per_policy` 語意對齊）；CS 變體維持 `sa_max_access_policies=0`＝政策數不限，不發明新數字 |
| 5 | 保護粒度 | **整個 subdomain 開/關**（等同 SA route `is_protected` 語意，operator payload 現況即 hostname 級）；UI 明示「開啟後所有訪客都需通過 Access 驗證」；path 級不做 |
| 6 | 頁籤顯示條件 | 只有 `ac_max_access_seats > 0` 的 CS 顯示 Access 頁籤；無席位不顯示、不做 upsell |
| 7 | trial 期間 | **開放**（trial 是活訂閱，lifecycle mixin 照管；不加 trial 特例分支） |
| 8 | 計數基準（spec 記載方式） | 現行傾向＝定價文件 §4.3（跨 route email 去重、硬上限掛方案層）；**最終拍板繫於 #381**（可能改為 CF seat 用量基準）。本檔只引用、不新增拍板 |
| 9 | policy model 形狀 | **擴充既有 `SecurityAccessAccessPolicy`**：錨點改 exclusive arc（`route_id` XOR `cloud_service_id`）；rule 子模型、#352 守衛、quota 檢查、AddPolicyModal 原地重用；不開平行 model（守衛會長兩份） |
| 10 | CS 的 access 欄位集 | **整組鏡射 SA route**：`is_protected`、`login_google`、`login_email_otp`、`session_duration`、`access_app_id`、policy 清單、`access_unresolved_idp_types` |

## 架構：載體形狀

```
CS detail Access tab ──> CS access endpoints（新）──> cloud_service access 欄位（鏡射 SA route）
                                                        │
                                                        └─> operator PUT /api/access/sync
                                                            （hostname 錨、與 tunnel 零耦合；
                                                             錨點＝CS subdomain，DNS/tunnel 不動）
policy：SecurityAccessAccessPolicy（exclusive arc：route_id XOR cloud_service_id）
        └─ rule 子模型（include/require/exclude）原地重用，#352 bulk-grant 守衛原地生效
```

技術前提（探查坐實，file:line 見 #521 分析與探查報告）：

- CF Access app 是帳號級、以 hostname 為錨的物件，payload 無 tunnel 欄位（`extra/paas-operator/src/models/schemas.py:762-791`、`extra/paas-operator/src/services/cloudflare.py:1430-1446`）⇒ 掛到 `paas-cs-*` 無結構性阻礙。
- CS 與 SA 同一個 CF zone/domain（operator 全域單例，`extra/paas-operator/src/config.py:93-98`）。
- CS 的公開 route 現況完全不受 Access 納管（`docs/decisions/agent-dataplane-in-cluster-no-cf-access-ip-bypass.md` §校正 2026-07-05）。
- `ac_max_access_seats` 本來就是 capability-orthogonal、跨三類（OQ-C 拍板）；CS/SH 接線排期的決策載體＝OQ-L（`docs/plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md:386`）。**本案即 OQ-L 的 CS 半邊落地。**

## 工程 checklist（非決策，實作必辦）

1. **quota 0 語意**：`_check_policy_quota`（`src/models/security_access_access_policy.py:229-265`）加 0=unlimited 豁免，與 `_check_emails_per_policy`（同檔 :201-227）對齊；兩處互相註記。**前置驗證**：確認沒有既有 SA plan 依賴「0＝封鎖」語意（seed 與 stg/prod plan 實查）。
2. **policy exclusive arc**：`route_id` XOR `cloud_service_id` 的 constraint；`security_access_id` stored related 改為可空、quota 計數與 ir.rule walk 分流（CS 側走 `cloud_service_id.workspace_id`）。
3. **ir.rule**：CS 錨定的 policy 需要 workspace member 讀／owner+admin 寫的對應規則（`src/security/ir_rules.xml:243-268` 的 CS 版）。
4. **operator sync 觸發**：CS 的 access 設定寫入後打 `PUT /api/access/sync`（per-hostname）；controller 權限形狀比照 SA，但見 checklist 8。
5. **reconciler 左集擴充**：`src/services/reconciler.py:53-90` 目前只從 SA route 蒐 `access_app_id`/`cf_policy_id`——CS 長出的 Access app 若不進左集會被判孤兒清掉。
6. **CF reusable policy 命名**：前綴現況寫死 SA `canonical_name`（operator `cloudflare.py:1419`，F-SA-7）；CS 要有自己的前綴且帶 env token（遵守 `docs/decisions/patterns/external-resource-env-token-naming.md`；注意 CS subdomain 本身無 env token 的既況——命名不可反推自 subdomain）。
7. **lifecycle**：CS 刪除路徑補 Access teardown（比照 `_teardown_route_access`，`src/models/security_access.py:713-750`；清不掉要 fail-loud 保留 id 供重試）；suspend 不動 Access app（CS 停機由 helm 層處理）。
8. **#739 同構驗證**：SA/SH 的 `action_provision` 非 sudo 寫 `groups=` 欄位在租戶路徑上從未成功過（#739，SH 實測）；CS 新增的 sync 路徑必須以**租戶身分真 RPC** 驗證，不可只跑 superuser `TransactionCase`（欄位級 `groups=` 在 superuser env 不生效）。
9. **API payload**：`_format_service`（`src/controllers/paas.py:3985-4031`）與 `_get_subscription_info`（`src/models/subscription_lifecycle_mixin.py:578-599`）補 entitlement 欄位（`ac_max_access_seats`）與 access 設定。
10. **前端**：`ServiceDetailPage` 加 Access tab（顯示條件見拍板 6）；`core/router.js:16` 的 tab regex 加 `access`；`AddPolicyModal` 把 `securityAccessService` hard import 改注入；SA detail 的 per-route 卡片區塊（`SecurityAccessDetailPage.xml:215-374`）抽成共用元件。
11. **#271 邊界驗證**：開啟保護後，任何殘留走公開 hostname 的叢集內路徑會 302 到 Access 登入頁；驗證 CS 相關內部呼叫（agent dataplane、health check）皆走 in-cluster DNS。

## Out of scope（明確不做）

- 席數 enforcement（含計數基準拍板）——#521 續追，繫於 #381。
- path 級保護、無席位 upsell UI。
- `-mcp` 公開 route 納管（現況未受 Access 保護，known gap，#301 校正段已記載）。
- Service Token selector 回歸（#381 若拍「end-user 走 service token」才需要；當初刻意移除，加回是額外工，operator/Odoo/前端三側白名單都要動）。

## 孤兒訂閱（#521 決策點 4）

**由設計本身消解**：sub 59（my-stg-odoo-1）、sub 65 無需任何遷移——policy 錨在 CS 上，功能上線後其 plan 既有的 `ac_max_access_seats=1` 直接讓 Access 頁籤出現、立即可用。

## 風險與爆炸半徑

- **CF Access seat＝per-account 共用池 50 席**，第 51 人認證當下硬擋＝跨租戶事故面。本案不做 enforcement，席位消耗仍無守衛——與現況相同，非本案新增風險；#521/#381 續追，CF ticket U7 草稿已備未送。
- 開整站保護會擋掉該 CS 的所有訪客（含對外站的匿名流量）——UI 明示後果（拍板 5）。
- quota 0 語意翻轉影響所有 `sa_max_access_policies=0` 的 plan（由「一條都不准」變「不限」）；checklist 1 的前置驗證負責確認無人依賴舊語意。
