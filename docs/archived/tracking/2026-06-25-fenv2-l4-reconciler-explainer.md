---
title: F-ENV-2 L4 帳號級反向 Reconciler — 會做什麼 + 對抗式審查補強
category: tracking
created: 2026-06-25
related:
  - docs/plans/2026-Q2/2026-06-21-naming-env-token-prod-stg-plan.md
  - docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md
  - docs/tracking/followups/follow-ups.md
source: 2026-06-25 understand workflow（4-reader → synth → critique）
status: archived
archived-from: docs/tracking/2026-06-25-fenv2-l4-reconciler-explainer.md
archived: 2026-07-19
archived-reason: "L4 reconciler 說明檔；設計權威=docs/plans/2026-Q2/2026-06-25-l4-reconciler-design.md"
---

# F-ENV-2 L4 帳號級反向 Reconciler — 會做什麼

> **一句話**：L4 是整個孤兒清理體系裡**唯一能「反向」發現孤兒的機制**。現有所有清理都從 Odoo DB 的 stored-id 出發；L4 反過來從 Cloudflare 帳號 / K8s cluster 列舉實體資源，對 Odoo 已知集合**取差集**，撈出「Odoo 已不認得、但雲端還活著」的資源。

## KPI 速覽

| 指標 | 值 |
| --- | --- |
| Effort | **XL**（跨 operator + Odoo + K8s，每步要安全閘） |
| 優先度 | medium（🚧 open） |
| operator 待新增 list-all 能力 | **4 類**（tunnels / access-apps / access-policies / zone-DNS） |
| 對抗式審查補強條目 | **10 條**（建 L4 設計前必納） |
| 相依 | **不依賴** L1-L3（token 只讓判 env 更乾淨） |
| prod 影響 | 反向刪除高風險 → dry-run + 多重 guard |
| 誤刪風險（Q1 非-paas / Q2 跨-env） | **naive 皆會** → 需正向 allowlist + env-pin fail-closed + protected denylist（見 §9） |

---

## (1) 解決什麼問題 — record-driven 清理的盲區

目前 Odoo 側**所有**外部資源清理都是 **record-driven（紀錄驅動）**：先在 DB 找到一筆現存 record → 讀它存的 stored-id → 拿 id 叫 operator 刪對應雲端資源。

落點：

- **CloudService** 存 `helm_release_name`（`src/models/cloud_service.py:147`）+ `helm_namespace`（`:151`），清理 `_do_cleanup`（`:1069`）只遍歷**現存** record。
- **SecurityAccess** 存 `tunnel_id`（`security_access.py:94`）+ route 的 `dns_record_id` / `access_app_id` + policy 的 `cf_policy_id`，清理 `_cleanup_cloudflare`（`:756`）只遍歷 `route_ids.filtered('access_app_id')`。
- **SmartHome** 存 `tunnel_id`（`smart_home.py:79`），清理 `_do_cleanup`（`:348`）直接拿 `tunnel_id` 刪。
- **每日 cron**（`subscription_lifecycle_cron.py:39`）`search([('subscription_id','!=',False)])` 全程遍歷現存 record。

**致命弱點**：stored-id 只存在 Odoo DB 這一份。只要 record 在雲端資源被刪前先消失（id 變 `NULL` 或整列被刪），Odoo **永遠生不出識別符** → 沒有任何 code path 列得出那些雲端資源 → operator 不知道要刪「誰」。

觸發路徑：

- `k8s-dev-sandbox destroy` / stg DB 重置 / 手動 `kubectl delete`；
- **甚至正常刪除路徑**：`SmartHome.action_delete`（`smart_home.py:183`）在 `delete_tunnel` 暫態失敗時只 log warning、接著**無條件** `unlink()`（`:202`），DB 列就此蒸發。對照 `SecurityAccess.unlink`（`security_access.py:511`）失敗時 `raise UserError` 保留供 retry，**SmartHome 缺這道 fail-loud guard**。

結果即 `cf-ns-orphan-no-account-gc.md` 那次清掉的 **3 policy + 28 DNS + 3 tunnel + 30 ingress rule + 3 ns** —— 永久孤兒，無限累積。L4 就是要補上這個「反向發現」的洞。

---

## (2) 核心機制 — list-all → 取差集 → 孤兒候選

L4 的演算法本質是**集合差集**：

```
孤兒候選 = (CF-side 實際存在的資源集合) − (Odoo-side DB 已知 stored-id 白名單)
```

**CF-side（ground truth，雲端實際長怎樣）**：直接列帳號內所有 tunnel / Access app + 各 app 實際引用的 policy / zone 內所有 DNS / 所有租戶 ns + PVC，靠命名前綴家族過濾。

**Odoo-side（白名單，平台「認為」自己有什麼）**：三張表 stored-id 全撈 —— CloudService `helm_release_name`/`helm_namespace`、SecurityAccess `tunnel_id`/`dns_record_id`/`access_app_id`/`cf_policy_id`、SmartHome `tunnel_id`。

**取差集** —— CF 有、白名單沒有 = 孤兒候選。這是 record-driven 永遠做不到的方向。

> **反直覺設計**：判 Access policy 是否孤兒時**不能信 Odoo 白名單**，要拿 CF app 實際引用的 policy 當 ground truth —— 因為 `sync_access_app` resync 會重建 policy，造成 DB `cf_policy_id` 與 CF 實際引用漂移。

---

## (3) 需要 operator 新增哪些能力（目前全缺）

現行 `CloudflareService`（`extra/paas-operator/src/services/cloudflare.py`）是**單帳號、per-tunnel** 導向，只有 by-id 操作，**完全沒有帳號級 list-all**：

| 已有 | 行號 | 性質 |
| --- | --- | --- |
| `create_tunnel` / `delete_tunnel` | `:742` / `:1020` | 單一建/刪 |
| `reconcile_tunnel_dns` | `:439` | 只對**單一** tunnel |
| `_list_dns_records_by_content` | `:399` | 只能 by-content，`per_page=100` **寫死** |
| `sync_access_app` | `:1259` | per-SA reconcile，回 `orphaned_policy_ids` 但無帳號級審計 |
| `create/delete_reusable_policy` | `:1150` / `:1186` | 單一建/刪 |

**L4 必須新增**：

1. `list_tunnels()` —— 帳號內所有 tunnel
2. `list_access_apps()` + `GET /access/apps/{id}/policies` —— app 與其實際引用 policy
3. `list_access_policies()` —— 所有 reusable policy
4. **zone 全量 DNS 列舉 + 完整分頁**（cursor 迭代到底，見安全模型）
5. 對應 HTTP endpoint（`api/tunnels.py` / `api/access.py` / `api/routes.py` 現皆只有 per-resource）

> config 的 `CLOUDFLARE_ACCOUNT_ID` / `ZONE_ID`（`config.py:92-98`）是全域級 —— 對 list-all 方便，但**一個 API 呼叫就撈到並潛在誤刪 prod 全量資源**，風險本質高，務必配 env 分流 + dry-run。

---

## (4) env 分流 — 共用 account 上分 prod / stg

prod / stg 共用同一 CF account / zone，list-all 撈回**混 env**，分錯就誤刪。

- **(a) DNS content 指哪條 shared tunnel（主）**：stg → `55953bfa` / prod → `328c3a28`。
- **(b) dual-format 前綴（必須兩種都認）**：舊 `paas-{sa,cs,sm}-{hash}`（無 env marker）+ 新 `paas-stg-{sa,cs,sm}-`（L1-L3 注的 token）。漏認舊格式 → 舊資源變「認不得的孤兒」。
- **(c) dedicated tunnel 例外**：`paas-sm-*` / `paas-sa-*` 自建專用 tunnel **本身無指向 shared tunnel 的 CNAME** → DNS content 判不了 env，**反而要靠對照仍存在的 prod DB 取差集**（共用 account 限制）。

**L1-L3 的幫助**：已上 stg 並驗證（prod 待 promote）。新資源自帶 env token / ns `PAAS_ENV` label → L4 對**新**資源判 env 更乾淨；但 token **不溯及既往**，存量舊孤兒沒 token，最終仍靠 DNS content + prod DB diff 兜底。

---

## (5) 安全模型（反向刪除 = 誤刪可打掛 prod）

1. **dry-run 先行** —— 先產孤兒 inventory 供人 review，確認才刪。
2. **TOCTOU 重算** —— 刪除階段必須**即時重新 list-all + 重算差集**，不可拿陳舊 dry-run 清單刪（中間新建、命名落孤兒前綴的合法資源會被誤殺）。
3. **不信 DB 白名單（僅限 policy 孤兒判定）** —— 看 CF app 實際引用（`GET .../policies`）。此漂移風險**一般化**到任何被 resync 重建過的 stored-id（`dns_record_id` / `access_app_id`）。
4. **刪前 GET 驗證（一般化）** —— in-use `409` guard 不只 policy；DNS / tunnel / app 刪前都先 `GET` 驗 content / name / connections 再刪。
5. **分頁 cursor 迭代到底** —— `per_page=500` 仍在 zone >500 筆時截斷，只 cursor 到底才安全（事故實證：`per_page=100` 截斷漏 15 個 `svc-*-mcp` 孤兒、誤判幻影）。
6. **DELETE 契約對齊真值** —— Access app/policy DELETE 真 CF 回 `202`（非 200），`delete_reusable_policy:1186` 已對齊，沿用勿退回（見 `operator-cf-access-delete-202-mock-contract`）。
7. **ns 無 TTL → L4 待建反向清**；**PVC 一起清**（殘餘 Released PV + 底層 Longhorn volume 計費）。
8. **誤認誤刪盲區** —— 前綴 filter 太寬 + 舊格式無 marker + 無 DNS content 時，舊 dedicated 資源「判不了 env → 可能誤歸 stg 而刪了 prod」的激進失敗。

---

## (6) 排程 / effort（XL）/ 與既有 cron 分工

跨 operator（多支 list-all + 分頁）+ Odoo（reconciler cron + 差集 + dry-run 報表）+ K8s（ns/PVC 反向清），每步要閘，且 **mock 全綠不夠、要 live CF E2E** 才驗得出孤兒辨識正確。

| | 既有 `subscription_lifecycle_cron` | L4 反向 reconciler（新） |
| --- | --- | --- |
| 方向 | **Odoo → CF**（record-driven） | **CF → Odoo**（resource-driven） |
| 入口 | `search(subscription_id)`（`:39`） | list-all CF / K8s |
| 觸發依據 | 訂閱狀態 | 差集 + env 分流 |
| 發現孤兒 | **不能**（id 蒸發就瞎） | **能**（唯一反向機制） |
| 風險 | 低 | 高 → dry-run + 多重 guard |

兩者**不互相取代**：日常生命週期走 subscription cron（便宜、精準），L4 是**較低頻**的反向對帳 net（先 dry-run 出報表、人工確認再刪）。

---

## (7) 與 L1-L3 的關係

- **L1-L3（已上 stg 驗證，prod 待 promote）補「辨識性」** —— 建立時注入 env token，讓**未來**新資源好認。
- **L4 補「發現既有孤兒」** —— 反向撈**存量**已漏的。
- **獨立**：L4 不依賴 L1-L3（token 不溯及既往，舊孤兒靠 DNS content + prod DB diff）。**互利**：L1-L3 普及後新資源自帶 marker，L4 判 env 越來越靠 token、越乾淨。

> **一句話**：L1-L3 讓「未來」孤兒好清，L4 把「過去 / 現在」孤兒撈回來，合起來才是完整帳號級反向 GC。

---

## (8) 對抗式審查 10 條補強（建 L4 設計前必納）

來源：2026-06-25 understand workflow 的 critique lens（4-reader → synth → adversarial critique）。

1. **「不信 DB 白名單」要收窄** —— 只對 Access policy 孤兒判定成立；dedicated tunnel 的 env 分類**反而要靠 prod DB diff**。一概「不信 DB」會錯失這條判定路徑。
2. **刪前 GET 驗證要一般化** —— in-use（409）不只 policy；DNS / tunnel / app 刪前都 `GET` 驗 content/name/connections。
3. **resync 漂移風險要一般化** —— 不只 `cf_policy_id`，任何 resync 重建過的 stored-id（`dns_record_id`/`access_app_id`）差集前都要防「id 已變但資源合法 → 誤判孤兒」。
4. **TOCTOU 重算** —— 刪除階段即時重新 list-all + 重算差集，不可拿陳舊 dry-run 清單刪。
5. **「誤認誤刪」激進失敗盲區** —— 前綴 filter 太寬 + 舊格式無 marker + 無 DNS content → 舊 dedicated 資源「判不了 env → 誤歸 stg 刪 prod」。原設計只談保守的「漏認→放過」。
6. **分頁須 cursor 迭代到底** —— `per_page=500` 仍在 zone >500 筆時截斷；CF pagination contract（`next_cursor` vs page offset）待查證。
7. **mcp sidecar 雙命名孤兒** —— CloudService mcp route deploy 用 `svc-*-{nssuffix}-mcp`、delete 用 `paas-cs-*-mcp`（命名不對稱 → 有 sidecar 的 service 刪除漏 mcp route），兩種命名都有歷史孤兒 → L4 盤 mcp **必須同認兩種前綴**。
8. **DB↔CF 分歧殘留判定缺設計** —— best-effort cleanup 只 catch `CloudflareException` → 暫態 httpx 錯逸出 → Odoo savepoint rollback → 半完成 CF 物件殘留；L4 反向對帳遇此如何判，尚無設計。
9. **跨 repo lock-step 部署守線** —— 動 PVC/GC 那塊，operator code 與 cross-repo `woow-paas-charts` cronjob template **必須 lock-step**（漏一邊重炸 fleet-split / 部署漂移）。
10. **成熟度校正（勿把「修復中/已修」當「L4 全新」）** —— PVC `orphaned-at` grace-period GC 機制**已存在但曾壞掉**（label 值含冒號過不了驗證、上線 ~74 天從沒生效），已由 operator #207/#208（epoch 秒 + `validate_label_value`）修復上 stg → L4 只需在反向掃描涵蓋殘餘 PVC/PV，非全新；**namespace 無 TTL** 才是 L4 真正待建。另：L1-L3「已 ship」精確語意為「**已上 stg 驗證、prod 待 promote**」，prod 仍空 token。

---

## (9) 誤刪風險 + 必備守衛（Q1 非-paas / Q2 跨-env）

> 來源：2026-06-25 over-deletion-risk workflow（3-reader → synth → adversarial critique）。共用 CF account/zone（`woowtech.io`）上跑「list-all → set-diff → delete」對兩個誤刪風險**都真實**；naive 實作會誤刪，且**目前連擋它的東西都還沒寫** —— CF 資源無帳號級 ownership marker，只有 `paas-cs-/sa-/sm-` 前綴（`naming.py:19-31`），`cloudflare.py` 也無 `list_all_*`。

### Q1 — 會不會刪到「非 paas」的東西？**naive 會**

- **風險**：zone-wide DNS list-all 會把官網 apex / MX（收信）/ 行銷子網域一起撈進來 → 它們在 Odoo DB 查無 stored-id → 全落入「孤兒」候選。純靠 `paas-*` 前綴擋不住（前綴撞名 + paas 自己 legacy 無前綴）。人工事故（2026-06-18~19）正是**刻意不動官網本體 / shared tunnel 本體 / zone 本身**——這條紀律交給 naive 自動化就破。
- **守衛**：① **正向 ownership allowlist**（只刪「能證明是我建的」，不是「刪不像我的」）+ 建立時蓋 ownership 證據（DNS `comment` / canonical_name）+ backfill ② **前綴 AND 內容雙證** ③ **protected denylist**：apex / MX / 官網 / 行銷 / shared tunnel 本體硬編絕不可碰 ④ **永不刪 shared tunnel 本體**（只清指向它的 DNS / ingress rule）。

### Q2 — 會不會刪到「不同 PAAS_ENV」的東西（stg 殺 prod）？**naive 會**

- **最危險情境**：① prod DB 不可達 → stg reconciler 靠「prod DB 查無＝孤兒」判 dedicated tunnel → **fail-open → 直接刪掉 prod 的 SA / SmartHome tunnel**；② `PAAS_ENV` 漏注入 prod operator → 自我識別不能 → 把 prod 自己的當「來路不明」刪；③ 舊格式無 token 的 prod 資源被 stg 判「不屬 stg → 孤兒」誤刪。
- **守衛**：① reconciler 用 `PAAS_ENV` **pin 自己 env**、跨-env 一律 skip ② **fail-closed：env 判不出來就一律不刪、留人工**（直接堵掉上面三個情境）③ prod-DB-diff 跑前先驗可達性，連不上就 skip（fail-safe 非 fail-open）。`PAAS_ENV` config 基礎已就位（`config.py:107` 白名單 fail-closed、`helm.py:108` ns label、pattern 明文禁止從 `namespace_prefix` 隱式推導），但**還沒接到 CF 刪除決策上**。

### ⚠️ 最深的結構性洞（對抗式校正抓到，必看）

**「正向 ownership 雙證」這條最強守衛，對 dedicated tunnel（`paas-sm-*` / `paas-sa-*`）結構性失效** —— 它們 CNAME 指向自己的 tunnel、不是 shared tunnel，「內容指向已知 shared tunnel」**永遠不成立**；CF Access app 又**沒有 metadata 欄位**可蓋 owner tag（`cloudflare.py:1334-1350`），policy 只有 name 帶 canonical_name 前綴 → allowlist 退化成跟前綴一樣弱的訊號。而真實事故裡**全部 3 條孤兒 tunnel 都正是這一類**。

→ 推論：**reconciler 連它當初要解的「既有孤兒 backlog」都無法安全自動刪**（那些 record 已 unlink、無 stored-id、無 env marker、無 shared-tunnel 內容 → 通不過任何守衛）。它只能自動清「**未來新洩漏、且帶完整 marker**」的資源；**dedicated tunnel / Access app / legacy 三類永遠只能 dry-run + 人工**。

### 鐵律與底線

**寧可漏刪（留孤兒，只是計費 / 整潔）也不可誤刪（打掛 apex/MX/官網/prod tunnel ＝ 線上事故）。** 五條：

1. **Allowlist not denylist** —— 只刪「能證明是我建的」。
2. **雙證** —— 前綴 AND 內容（DNS content / canonical_name comment / shared-tunnel ID）。
3. **env-pin fail-closed** —— reconciler pin 自己 `PAAS_ENV`，env 判不出就不刪、跨-env skip、prod DB 不可達就 skip dedicated tunnel。
4. **dry-run + 刪前對 CF 與 Odoo 都重查**（防 TOCTOU）、分頁 cursor 走到底（防 `per_page` 截斷而誤算孤兒）。
5. **protected 名單兜底** —— apex / MX / 官網 / 行銷 / shared tunnel 本體，硬編絕不可碰。

**在「正向 ownership marker + backfill」「env-pin fail-closed」「protected denylist」三者補齊前，L4 不該開自動刪除，只能 dry-run 出清單給人工確認**（＝ 2026-06 人工事故的做法）。

### 前置止血（獨立於 L4）

`smart_home.py:202` 的**無條件 unlink**（暫態失敗只 warning、缺 `SecurityAccess:497-511` 那種 fail-loud guard）**正是事故裡 3 條孤兒 tunnel 的製造源**。先修它（不再生產 un-reconcilable 孤兒）是 L4 的前置項，可獨立開條目（候選 F-ENV-4）。

---

## 仍要拍板的開放點

- **mcp sidecar 雙命名孤兒**辨識（svc-*-mcp 與 paas-cs-*-mcp 必須同盤）。
- **DB↔CF 分歧殘留**（sync 半途 rollback 的 CF 物件）如何判定。
- **dedicated tunnel env 判定**（prod DB diff）做在 operator 端 API 還是 Odoo 端比對；且 prod DB 可達性是「可被推翻的前提」，不可 hardcode 信任。
- **Access app / dedicated tunnel 的正向 ownership marker** 天生稀薄（無 metadata 欄位 / CNAME 指自身）→ 這兩類能否「永久只走人工」而非自動刪除。
- **與既有 cron 的刪除 race**：`subscription_lifecycle_cron` 也在同一 account 用 stored-id 刪 CF → 需 serialization / lease guard 避免互踩。

來源：本 session 兩個 workflow —— understand（6 agents、329k tokens）+ over-deletion-risk（5 agents、283k tokens、3/3 reader 直讀 code）。設計依據見 `docs/plans/2026-Q2/2026-06-21-naming-env-token-prod-stg-plan.md` §階段 3；根因 pitfall `docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md`。
