# template-vs-plan-property-boundary — 開新 template 還是掛新 plan 的判準

> **Pattern**：CloudAppTemplate 與 sh.subscription.plan 的職責分界＝**屬性落在哪一層，決定商品變體要「同 template 掛新 plan」還是「另開一筆 template」**。判準一句話：**需要不同的 template 級屬性（最常見＝image）就必須開新 template；只差 plan 級屬性（價格／規格／席數／可見性）就同 template 多 plan。**

## 兩層職責表

| 層 | 屬性 | 變體機制 |
|---|---|---|
| **template 級**（`cloud.app.template`） | `helm_default_values` 內的 **image**（repository/tag/digest）、chart 來源與版本、`customPostInitScripts`（init script 本體）、`category`／`slug`、marketplace 卡片（名稱/描述/`is_active`）、`max_per_partner` | 不同值＝**另開 template record** |
| **plan 級**（`sh.subscription.plan` 平台擴充欄位） | 價格（`sh_plan_price`）、部署規格（`cs_vcpu`/`cs_ram_gb`/`cs_storage_gb`/`cs_odoo_workers`——runtime overlay 疊進 helm values）、AC 席數（`ac_max_access_seats`）、trial 開關、前台可見性（`storefront_hidden`）、計費週期 | 不同值＝**同 template 掛新 plan**（M2M `subscription_plan_ids`） |

**技術根據**：plan 的規格 overlay（`CloudService._apply_plan_spec_overlay`，#332）只疊 `resources`／`persistence.size`／workers 字串替換——**刻意不碰 image 與 chart**。要換 image 沒有 plan 級機制，只能開新 template。

## 何時必看

- 新商品變體要上架，猶豫「開新 template 還是加 plan」時。
- 有人提議「在 plan 上加 image 欄位」時——先讀本檔（那會把 template 職責滲進 plan，overlay 機制與 sticky noupdate 治理全部複雜化；正解是開新 template）。

## 現成案例

| 案例 | 判定 | 依據 |
|---|---|---|
| Odoo CS 三階（T1/T2/T3） | 同一筆 `cloud_app_odoo` ↔ 6 plans | 同 image，只差價格與 cs_* 規格（taxonomy §3.3 Option B 拍板） |
| MCP 標準版 vs Manage 版（#338） | 兩筆 template（`cloud_app_mcp_odoo` / `cloud_app_mcp_odoo_manage`） | 同是 Odoo MCP 但 **image 不同**（`odoo-mcp-admin` vs `odoo-manage-mcp-admin`）＋連線模型不同 |
| free HA PaaS（#336，未來） | 獨立 template（掛 Elmo 輕量 lite image、`max_per_partner=1`、隱藏） | 付費 HA（若有）必為完整版 image → 屆時另開一筆，可見性也各自乾淨（free 永遠隱藏、付費公開） |

## 反向邊界（本 pattern 不管的）

同 template 內「per-tier 規格不生效」問題（plan 規格怎麼流進部署）由 #332 runtime overlay 解；template seed 的 sticky noupdate 治理見 matrix doc §2.3——兩者都不是開新 template 的理由。
