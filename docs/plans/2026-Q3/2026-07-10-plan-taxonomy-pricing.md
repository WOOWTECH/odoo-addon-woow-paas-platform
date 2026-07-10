# WOOW PaaS 整體 Plan 架構與訂價方式報告（2026-07-10）

> **收件人**：Eugene（業主）
> **整合來源**：2026-07-10 三大類架構裁決＋已拍板定案全集（權威）＋「訂閱方案總表」（30 筆，同目錄 `2026-07-10-subscription-plan-matrix.md`）＋ Plan Taxonomy 設計稿／AC 掛層分析／CF 50-seat 查證（session 工作產物，關鍵結論已收入本檔）＋ repo 現況複驗（`src/__manifest__.py:3` 現值＝`18.0.1.0.51`，PR #326 已 merged）。
> **拍板記錄（2026-07-10 Eugene）**：OQ-A ✅ 3 值 enum／OQ-B ✅ 定名 `ac_max_access_seats`／OQ-C ✅ capability 欄位不綁 service_type——seed PR（base .52）解鎖。
> **本報告職責**：整體 plan 架構（taxonomy）、schema 設計、訂價方式、對前波總表/checklist 的差分修訂、開放問題。逐筆 seed/migration 明細仍以總表為權威，本報告只在「因分類變更需要改動」處指名。

**狀態凡例（全文標記，三態）**：

| 標記 | 意義 |
|---|---|
| 【已拍板】 | Eugene 已裁決，零改動空間，工程照做 |
| 【建議待拍】 | 本報告給出建議與選項，需 Eugene 拍板後動工 |
| 【未來佔位】 | 本波只建欄位/結構佔位，不接計費與 enforcement |

---

## ① 一頁摘要

### 架構圖：三大類 × 跨類加值層

```
                WOOW PaaS 訂閱方案架構（sh.subscription.plan 共用一張表）
                新欄位 service_type ∈ {cloud_service, smart_home, security_access}

┌──────────────────────────────┬─────────────────────────┬─────────────────────────┐
│ ① Cloud Service（22 檔）     │ ② Smart Home（4 檔）    │ ③ Security Access（4 檔）│
│                              │                         │                         │
│ ▸ 一般 service               │ ▸ Home Assistant        │ ▸ default template      │
│   Odoo  T1/T2/T3             │   Connect      月 150   │   5 Application 月 1000 │
│         1000/3000/5000       │   + MCP        月 400   │   10 Application月 2000 │
│   n8n   T1/T2/T3             │   (ha_mcp_tools)        │   (sa_max_routes=5/10)  │
│         1000/3000/5000       │                         │                         │
│ ▸ MCP 類 service（併入 CS）   │                         │                         │
│   Odoo MCP  單階 3000        │                         │                         │
│   n8n  MCP  單階 3000        │                         │                         │
│   Hermes    T1/T2/T3         │                         │                         │
│             1500/4500/6000   │                         │                         │
│  （offering＝template；MCP    │ （offering＝1 筆薄       │ （offering＝1 筆薄       │
│   由 template.category='mcp' │   template）            │   template）            │
│   承載，不設第 4 大類）       │                         │                         │
└──────────────┬───────────────┴────────────┬────────────┴────────────┬────────────┘
               │      三大類服務皆有 Cloudflare Application route      │
               └────────────────────────────┬────────────────────────┘
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│              跨類加值能力層（capability 欄位，不綁 service_type）                │
│  ▸ Access Control   500/seat/月【已拍板】   （enforcement 現況：僅 SA 已接線）   │
│  ▸ Email Routing    未定價【未來佔位】      （CF 免費；建議先當 tier quota 賣點）│
│  ▸ Backup           未定價【未來佔位】      （僅 CS 有 PVC，短期唯一可行對象）   │
└────────────────────────────────────────────────────────────────────────────────┘
   年繳＝月繳×10（全域）｜trial 統一 7 天｜每「檔位×週期」＝一筆獨立 plan record
```

### 核心設計決定（5 條）

1. **三大類＝`service_type` 3 值 enum；MCP/Hermes 全歸 `cloud_service`**【已拍板方向；enum 字面值 confirm＝OQ-A】。MCP-ness 由既有 `cloud_app_template.category='mcp'` 承載（marketplace 已是一等 tab），不設第 4 大類——對齊裁決字面「MCP 屬 Cloud Service 底下可建立的服務項目」。
2. **offering 承載＝template M2M，三類統一**【建議待拍（工程定案）】。掃描坐實 SH/SA 各有 1 筆薄 template＋自己的 plan M2M（並非「無 template」），CS 多 template；零新 model、零新機制。
3. **跨類加值（AC/Email Routing/Backup）＝capability 欄位層**【AC 機制已拍板；Email/Backup 未來佔位】。物理上全落 shared plan 表；用 capability 前綴（`ac_`/`email_routing_`/`backup_`）、**不綁 service_type**（三類都有 CF route，加值刻意跨類）；「上限欄位」與「計費 product」分兩層。
4. **訂價公式＝base(offering×tier)＋線性加值**【base 價格全定、AC 500/seat、年=月×10、trial 7 天皆已拍板】。base 與加值解耦；route 容量與 AC 席數是兩條正交軸。
5. **工程守線**【seed 鐵律已拍板＋新 guard 建議待拍】：既有 3 條 template 互斥 constraint 原封不動、僅 +1 條「條件式 guard」（orphan plan 放行）；16 筆改價**直接 migration force-write**（grandfathering 已拍板＝不需要）；migration 一律 xmlid 定位（撞價實例：SA Entry Yearly 舊 1000＝5App 新月價）；noupdate=1；既有 xmlid 原地沿用為最低檔。

### 數字速覽

- **30 筆定案 plan**（CS 22／SH 4／SA 4）＝ **16 沿用改價**（migration force-write，plan+product 共 32 筆）＋ **14 新增**（`-u` 自建，plan+product 共 28 筆）。
- 另 **AC 席數 add-on**（500/seat/月【已拍板】；載體 carrier 待拍＝OQ-D，可後補不 block seed PR）。
- 4 筆 placeholder（postgresql 2＋anythingllm 2）不動；redis 有 template 無 plan（維持總表 Q9 追蹤）。PR 後 seed 總量 34 plan＋34 product。
- **版號基準（本報告新複驗）**：manifest 現值 **.51**（PR #326 已 merged）⇒ 本波定價 PR＝**.52**、migration 目錄＝`18.0.1.0.52/`（總表 checklist A1/D 段需順移，見 ⑤）。

---

## ② 三大類 Plan 樹全表

> 價格全數【已拍板】。全域規則【已拍板】：年繳＝月繳×10（機械先 seed、後台可改）；trial 統一 7 天（現況 seed 已全 7 天、無異動）；`sh_override_product=True` ⇒ `plan.sh_plan_price` 權威、`product.list_price` 必同步改。
> 「規格」欄：MCP 統一規格【已拍板、#268 已對齊 template 不動】；Odoo/n8n 三階規格＝目標部署值 draft、Hermes 是否分階未定——均屬 OQ-H【建議待拍】，不 block 改價。

### 樹狀總覽

```
WOOW PaaS Plans
├── ① Cloud Service (service_type='cloud_service')            〔22 檔〕
│   ├── 一般：Odoo Cloud T1/T2/T3 ×（月/年）＝6
│   │        n8n  Cloud T1/T2/T3 ×（月/年）＝6
│   └── MCP 類（category='mcp'，本次由「AI 類」併入）：
│            Odoo MCP 單階 ×2、n8n MCP 單階 ×2、Hermes T1/T2/T3 ×6＝10
├── ② Smart Home (service_type='smart_home')                  〔4 檔〕
│   └── Connect ×2、+MCP（ha_mcp_tools）×2
└── ③ Security Access (service_type='security_access')        〔4 檔＋AC 加值〕
    └── 5 Application ×2、10 Application ×2、AC 席數 500/seat/月
```

### 全表（服務項目 × 檔位 × 月/年價；每列＝月＋年兩筆 plan record）

| # | 大類 | 服務項目 | 檔位 | 月價 | 年價(×10) | XML id（monthly／yearly） | 異動（舊價→新價） | 規格 |
|---|---|---|---|---:|---:|---|---|---|
| 1 | CS-一般 | Odoo Cloud | T1（1–3 人） | 1000 | 10000 | `plan_odoo_monthly`／`plan_odoo_yearly`（沿用） | 改價 499/4990→＋改名＋掛規格 | 2vCPU/2Gi/20G/workers=2（draft） |
| 2 | CS-一般 | Odoo Cloud | T2（3–6 人） | 3000 | 30000 | `plan_odoo_t2_monthly`／`_yearly` | **新增** | 2vCPU/4Gi/30G/workers=3（draft） |
| 3 | CS-一般 | Odoo Cloud | T3（6–10 人） | 5000 | 50000 | `plan_odoo_t3_monthly`／`_yearly` | **新增**（軟上限刻意≈T2【已拍板】） | 3vCPU/4Gi/40G/workers=4（draft） |
| 4 | CS-一般 | n8n Cloud | T1（1–3 人） | 1000 | 10000 | `plan_n8n_monthly`／`plan_n8n_yearly`（沿用） | 改價 199/1990→（漲幅最大） | 1vCPU/1Gi/10G（draft） |
| 5 | CS-一般 | n8n Cloud | T2（3–6 人） | 3000 | 30000 | `plan_n8n_t2_monthly`／`_yearly` | **新增** | 1vCPU/2Gi/20G（draft） |
| 6 | CS-一般 | n8n Cloud | T3（6–10 人） | 5000 | 50000 | `plan_n8n_t3_monthly`／`_yearly` | **新增**（軟上限≈T2） | 2vCPU/2Gi/30G（draft） |
| 7 | CS-MCP | Odoo MCP Server | 統一單階 | 3000 | 30000 | `plan_mcp_odoo_monthly`／`_yearly`（沿用） | 改價 only 499/4990→ | 1vCPU/1G/5G【已拍板，template 不動】 |
| 8 | CS-MCP | n8n MCP Server | 統一單階 | 3000 | 30000 | `plan_mcp_n8n_monthly`／`_yearly`（沿用） | 改價 only 499/4990→ | 同上 |
| 9 | CS-MCP | Hermes MCP Server | T1（1–3 人） | 1500 | 15000 | `plan_mcp_hermes_monthly`／`_yearly`（沿用，原地改 T1） | 改價 499/4990→＋改名 | 現 flat 1vCPU/1G/5G；分階未定（OQ-H） |
| 10 | CS-MCP | Hermes MCP Server | T2（3–6 人） | 4500 | 45000 | `plan_mcp_hermes_t2_monthly`／`_yearly` | **新增** | 未定（OQ-H） |
| 11 | CS-MCP | Hermes MCP Server | T3（6–10 人） | **6000** | 60000 | `plan_mcp_hermes_t3_monthly`／`_yearly` | **新增**（定案 6000，取代會議 6500【已拍板】） | 未定（OQ-H） |
| 12 | SH | Smart Home | Connect（純 remote access） | 150 | 1500 | `plan_smarthome_monthly`／`_yearly`（沿用） | 改價 100/1000→ | `mcp_integration=''` |
| 13 | SH | Smart Home | + MCP | 400 | 4000 | `plan_smarthome_mcp_monthly`／`_yearly` | **新增** | `mcp_integration='ha_mcp_tools'`（欄位 .50 已落地【已拍板】） |
| 14 | SA | Security Access | 5 Application | 1000 | 10000 | `plan_security_access_entry_monthly`／`_yearly`（沿用） | 改價 100/1000→＋quota 3→5＋改名 | `sa_max_routes=5` |
| 15 | SA | Security Access | 10 Application | 2000 | 20000 | `plan_security_access_pro_monthly`／`_yearly`（沿用） | 改價 300/3000→＋改名 | `sa_max_routes=10`（已=10 不動） |

### 跨類加值（不是獨立 plan 檔位，是掛在上表任一訂閱上的加購）

| 加值 | 計價 | 內含量 | 上限欄位（plan 層） | 狀態 |
|---|---|---|---|---|
| **Access Control 席數** | **500／seat／月** | M=0（全加購起算） | `sa_max_access_emails`【已拍板名】（建議更名 `ac_max_access_seats`＝OQ-B） | 機制【已拍板】；載體 carrier【建議待拍】OQ-D；年繳席數是否 ×10＝OQ-D 子題 |
| Email Routing | 未定 | — | `sa_max_email_rules`（建議更名 `email_routing_max_rules`＝OQ-E） | 【未來佔位】（連 model 都尚未實作） |
| Backup | 未定 | — | `backup_enabled`／`backup_max_snapshots`／`backup_retention_days`（新佔位） | 【未來佔位】（operator 零 snapshot API） |

### 參考列（不在 30 筆定價 scope；backfill 仍會處理，見 ⑤）

| 服務 | 現價 月/年 | 處置 |
|---|---|---|
| CS — PostgreSQL（placeholder） | 99／990 | 不動（總表 Q9）；backfill `service_type='cloud_service'` |
| CS — AnythingLLM（placeholder，**無 template＝orphan**） | 299／2990 | 不動（Q9）；backfill `cloud_service`，新 guard 對其放行 |
| CS — Redis | 無 plan（僅 template） | 不動（Q9） |

**檔數核對**：CS＝6+6+2+2+6＝22；SH＝4；SA＝4；合計 **30** ＝ 16 沿用改價＋14 新增（與總表淨變動統計一致）。

---

## ③ Schema 設計

### 3.1 `service_type` enum 值定案建議【建議待拍＝OQ-A（confirm 級）】

```python
service_type = fields.Selection(
    [('cloud_service', 'Cloud Service'),
     ('smart_home', 'Smart Home'),
     ('security_access', 'Security Access')],
    string='Service Type',
)
```

**定案建議＝3 值、不設 `mcp` 第 4 值**。理由四條：

1. **裁決字面**：「MCP（n8n MCP、odoo MCP、Hermes MCP）也屬於 Cloud Service **底下**可建立的服務項目」——MCP 是 CS 貨架上的商品，不是第 4 家店。
2. **MCP-ness 已有單一真相**：MCP template 本來就是 `cloud_app_template` record（3 筆，`category='mcp'`）；再設 `service_type='mcp'` 是雙重編碼，必漂移。
3. **constraint 邊界對齊**：`service_type` 是「三個 template model 邊界」的去正規化影子；加第 4 值會讓 enum 邊界與 template model 邊界錯位，既有互斥 constraint 語意錯亂。
4. **前端現況已如此**：CS marketplace 用 `category` tab（`mcp` 已是一等 tab）、三大類 create-flow 已按三 template model 分流。

**對造最強論點（誠實記錄）**：若未來 MCP 要有「頂層獨立導覽/landing」，平級 enum 才自然——但此路已被本次裁決前置否決；真要走是下一次產品裁決的事，屆時 enum 再擴。

**為何要「存」`service_type` 而非從 template link 衍生**（關鍵取捨）：
- **orphan plan 反查不出來**：AnythingLLM 有 2 plan＋2 product 但無任何 template record，反查三個 M2M 全空。
- **backfill 與前端 filter 要純量**：D4 一次性歸類、前端/API 單欄過濾；反查＝3 次 search 且對 orphan 無解。
- **一致性有既有 constraint 兜底**：互斥 constraint 保證 plan 最多屬一個 template family，存下的值只需與該唯一 family 對齊（由 3.3 新 guard 看守）。

### 3.2 加值能力欄位佈局：二分原則

> 物理上三類欄位全在同一張 `sh.subscription.plan`（各 `_inherit` 檔只是加欄位）；「放哪層」實際是 **(a) 命名前綴、(b) 落哪個 inherit 檔、(c) 要不要綁 service_type** 三件事。

| 類別 | 定義 | 前綴 | service_type 約束 |
|---|---|---|---|
| **type-specific** | 只對某一類有意義（部署規格、路由條數、HA MCP 旗標） | `cs_`／`sa_`／（SH 用 `mcp_integration`） | 軟 guard（僅該類可非預設） |
| **capability-orthogonal** | 任何有 CF route 的服務皆可加購（AC／Email Routing／Backup） | `ac_`／`email_routing_`／`backup_` | **無**（刻意跨三類）——OQ-C |

**欄位總表**：

| 能力維度 | 欄位 | 性質 | 命名處置 | Enforcement 現況 | 狀態 |
|---|---|---|---|---|---|
| CS 部署規格 | `cs_vcpu`/`cs_ram_gb`/`cs_storage_gb`/`cs_odoo_workers`（新增） | type-specific (CS) | 新設 | 未接（OQ-H／總表 Q4） | 【建議待拍】 |
| SA 路由容量 | `sa_max_routes` | type-specific (SA 結構：路由「條數」；CS/SH 天生各 1 條) | 沿用、保留 `sa_` | 已接（ORM＋controller 雙層） | 沿用 |
| SH MCP 旗標 | `mcp_integration`（Char，`''`/`'ha_mcp_tools'`） | type-specific (SH) | 沿用（.50 已落地【已拍板】） | 唯讀透傳（`to_dict()['mcp']`＋`get_tunnel_token()`） | 沿用；建議補硬 guard「非空⟹smart_home」【建議待拍】 |
| **AC 席數上限** | 拍板名 **`sa_max_access_emails`**；建議更名 **`ac_max_access_seats`** | **cross-category** | OQ-B（見下方語意守線） | SA 待接；CS/SH aspirational | 欄位【已拍板】、命名【建議待拍】 |
| AC policy 數 | `sa_max_access_policies` | cross-category（現僅 SA enforce） | **不改名**（已線上 enforce，改名有 churn 與回歸風險） | 已接（SA 雙層） | 沿用 |
| AC per-policy email | `sa_max_emails_per_policy`（20） | cross-category 軟護欄【已拍板保留】 | 不改名（同上） | 已接（SA ORM 單層） | 沿用 |
| Email Routing 規則數 | `sa_max_email_rules` → 建議 **`email_routing_max_rules`** | **cross-category** | 改名便宜（零 enforce 碼要動，只 seed＋顯示） | 零實作（連 model 都無） | 【未來佔位】＋命名【建議待拍】OQ-E |
| Backup | `backup_enabled`/`backup_max_snapshots`/`backup_retention_days`（新佔位） | **cross-category**（技術上僅 CS 有 PVC 短期可行） | 通用前綴，勿用 `cs_backup_*` 鎖死 | 無（operator 零 backup API） | 【未來佔位】OQ-F |
| SA 自訂網域 | `sa_max_custom_domains` | type-specific-ish (SA) | 沿用 | 未接（總表既有 P3） | 沿用 |

**AC 席數欄位語意守線（不可漂移）**：這個欄位是**上限/天花板（MAX）**，不是「內含席數」。定價＝「內含 M=0＋在其上加購」，enforcement 語意＝「跨該訂閱全部 route 的 `email_list` **去重**位址數 ≤ 0＋已購 AC seat qty」【已拍板】。更名（OQ-B）只換前綴、**MAX 語意不變**——嚴禁偷渡成 `ac_included_seats` 之類「內含」語意。

**假朋友警告**：`mcp_enabled`（cloud_app_template 上）是「掛不掛 MCP sidecar」旗標（唯一 True 是一般 n8n），**不是** MCP offering 判別器；真判別器＝`category=='mcp'`。本設計一律不碰 `mcp_enabled`。

### 3.3 Constraint 設計

**既有 3 條互斥 constraint（`_check_no_cross_service_type_plans`，三 template model 對稱）完全不動**。論證：MCP template 本來就是 `cloud_app_template` record、其 plan 本來就走 CS 家族 M2M（`woow_template_plan_rel`）；「MCP 歸 CS」在 template 層早已成立，本次只是 plan 分類欄位對齊，互斥語意原樣正確。

**僅新增 1 條「條件式 guard」**【建議待拍】——存下的 `service_type` 必須與 owning template family 一致、orphan 放行：

```python
_TEMPLATE_FAMILY = {
    'woow_paas_platform.cloud_app_template':       'cloud_service',
    'woow_paas_platform.smart_home_template':      'smart_home',
    'woow_paas_platform.security_access_template': 'security_access',
}

@api.constrains('service_type')
def _check_service_type_matches_template_family(self):
    for plan in self:
        families = {st for model_name, st in self._TEMPLATE_FAMILY.items()
                    if self.env[model_name].sudo().search_count(
                        [('subscription_plan_ids', 'in', plan.id)])}
        if not families:
            continue   # orphan plan（如 AnythingLLM）→ 放行，否則 backfill 會炸
        if plan.service_type and plan.service_type not in families:
            raise ValidationError(...)
```

定位：縱深防禦（主保證仍是三 create-flow 各掛自家 template＋既有互斥）；抓的是 backfill 寫錯值、開發者手改、未來 seed 掛錯 family。**淨改動＝+1 條 guard、既有 3 條原封不動。**

### 3.4 與既有 `sa_*`／`mcp_integration` 的相容總結

- `sa_max_routes`：真・SA 結構容量，保留 `sa_` 前綴與 SA-only 語意，不受本次影響。
- `mcp_integration`：SH type-specific；400 檔 seed 字面值精確＝`'ha_mcp_tools'`【已拍板、.50 已落地】；建議加硬 guard「非空⟹`service_type='smart_home'`」。
- `sa_max_access_policies`／`sa_max_emails_per_policy`：**心智上重歸類為 AC capability（跨類）但不改名**；日後 CS/SH 接 AC 時直接沿用同欄位。
- **Enforcement 逐格誠實聲明**：欄位可現在就跨三類建模，但 enforcement 今天只有「SA × AC」一格是真的（route.is_protected→policy→operator sync）；CS/SH 的 AC 欄位＝建模先行、尚未生效（aspirational），**不可對外宣稱三類今天即一致 enforce**（接線排期＝OQ-L）。

### 3.5 檔案佈局與後台 view

```
src/models/
  sh_subscription_plan_service_type.py   # 新：service_type ＋ 3.3 guard
  sh_subscription_plan_cs_spec.py        # 新：cs_*（type-specific, CS；OQ-H 拍 Option B 後）
  sh_subscription_plan_sa_quota.py       # 既有：sa_max_routes 等（policy 兩欄暫留、註解標記 AC capability）
  sh_subscription_plan_sh_mcp.py         # 既有：mcp_integration
  sh_subscription_plan_ac.py             # 新：AC 席數上限欄位（OQ-B 定名後）
  sh_subscription_plan_email_routing.py  # 未來：email_routing_max_rules
  sh_subscription_plan_backup.py         # 未來：backup_* 佔位
```

後台 view（現況 plan 無 form/tree 可編這些欄位，建議本波補最小 form）：**type-specific 欄位隨 `service_type` 顯隱（`invisible=`）；capability 欄位永遠顯示**（跨三類，任何 plan 都可能設）。

---

## ④ 訂價方式

### 4.1 訂價公式

```
月訂閱總額 = base_plan_price(offering, tier)        ← ② 全表【已拍板】
           + AC_seats × 500                        ← 【已拍板】Access Control per-seat
           + email_routing_fee                     ← 【未來佔位】未定價（4.4）
           + backup_fee                            ← 【未來佔位】未定價（4.5）

年訂閱總額 = 月 × 10【已拍板】；trial 統一 7 天【已拍板】
```

兩條設計原則：**(1) base 與加值解耦**——base＝「offering×tier」階梯價（一筆 plan 一個 `sh_plan_price`），加值＝線性可疊加、與掛哪類無關；**(2) 加值是跨類獨立軸**——SA 完整帳單＝route 階梯價（1000/2000）＋AC seats×500；route 容量（`sa_max_routes`）與席數容量（AC 席數上限）是兩條正交軸。

### 4.2 base 階梯價

每「offering × tier × 週期」＝一筆 plan record；`sh_override_product=True` ⇒ `plan.sh_plan_price` 權威、`product.list_price` 同步。base 不含任何加值。價格全數【已拍板】（② 全表）；T3 軟上限刻意≈T2（多收的是人數餘裕與支援、非等比算力）【已拍板方向，可持續性列總表 Q10 觀察】。

### 4.3 Access Control 席數加值

**已拍板機制（全集）**：500／seat／月；方案內含 M=0（從 0 起全加購）；計數＝跨該訂閱全部 route 的 email **去重**；硬上限掛**方案層總量**（新欄位，B 路線；**明確否決 per-route 上限**）；既有 per-policy 20 留軟護欄；UI 簡化版＋系統底層產完整 CF policy；CF 成本內含售價、**不對使用者顯示**。

**CF seat 事實與風險（查證坐實，影響售價防線）**：CF Access 免費席位是 **per-ACCOUNT 共用池 50**（非 per-domain）；達標＝**第 51 人硬擋**（服務中斷風險，**非**自動扣款）；service token 不佔 seat；跨過門檻需升級付費版、**疑似全席計費（成本懸崖）**——計費單位（authenticated-seat vs listed-email）待坐實＝OQ-K。⟹ 落地配套：50 席池用量監控＋銷售端席數護欄，在 OQ-K 坐實前不要讓全帳號售出席數逼近 50。

**未拍板＝carrier（席數怎麼掛進 `sh_subscription`）＝OQ-D**。取決於一個未驗事實：**`sh_subscription` 是否支援同一訂閱多條 line 且 qty 可變**（該模組在另一 repo，需問其負責 agent 或 grep 已安裝模組）：

| | Carrier A（若支援可變 qty line）＝**推薦** | Carrier B（若不支援）＝fallback |
|---|---|---|
| 機制 | 一個「AC Access Seat」product（500/月）以可變 qty 掛進**同一**訂閱 | AC 席數自成一筆訂閱，link 回服務 |
| 優點 | 單一訂閱/發票；續訂、退點、**#318 刪除聯動天然生效**（`_settle_on_entity_delete` 隨主訂閱一起結算） | 不依賴多 line 能力 |
| 缺點 | 依賴 sh_subscription 多 line 能力（未驗） | 服務刪除時 #318 聯動**碰不到** AC 訂閱→需額外聯動碼，否則 AC 孤兒續扣（#324 同類失效） |

**欄位動態語意建議**：seed 靜態 0（內含 M=0）＋執行期上限＝0＋訂閱上已購 AC seat qty；enforcement 比對「distinct email 位址數 ≤ 內含＋已購」。欄位可先落（default 0），**不 block seed PR**；AC product 待 OQ-D 拍板後補（總表 C5 原案不變）。年繳席數是否存在（×10？）＝OQ-D 子題。

### 4.4 Email Routing 訂價結構【建議待拍＝OQ-E；實作為未來佔位】

事實前提：CF Email Routing 本身**免費**、不佔 Access seat；平台側目前**零實作**（連規則 model 都沒有）。三種變現結構：

| 選項 | 結構 | 適用 |
|---|---|---|
| E1 flat add-on | 每月固定價，內含 N 條規則 | 想獨立變現、單純 |
| E2 per-rule qty | 每條規則 Y/月（同 AC per-seat 模式） | 用量差異大 |
| **E3 內含在 tier（建議先做）** | 不獨立收費；`email_routing_max_rules` 當**方案差異化 quota**（高階多、低階少/0） | CF 免費⇒成本壓力低，先當 tier 賣點 |

建議：**先 E3**——落地成本最低（只 seed quota＋補 enforcement），不動計費；日後要獨立變現再升 E1/E2。前提：需先補「規則 CRUD model＋operator CF Email Routing API 封裝」，E3 的 quota 才 enforce 得動。

### 4.5 Backup 訂價結構【建議待拍＝OQ-F；實作為未來佔位】

事實前提：backup＝volume snapshot，**只對有 PVC 的服務有意義**（CS 有；SH/SA 無）；operator 零 backup API；#317 chart-owned PVC keep 已落地＝資料保護前置已具備，但 snapshot/restore 生命週期整套未做——**最遠的 Future**。

- **欄位層級建議**：通用 `backup_*` 前綴（cross-category 佔位、與 AC/Email 一致），**不要** `cs_backup_*` 鎖死；enforcement CS 先行、SH/SA 純佔位（default 關）。
- **訂價結構**：**B1 flat add-on（建議）**＝每月 X 內含 N snapshots／保留 D 天；B2 per-GB-month（需計量基建，晚點）；B3 內含高階 tier（當賣點）。本波**只建欄位佔位、不接 enforcement、不定價**。

### 4.6 加值結構總結

| 加值 | 載體 | plan 上限欄位 | 計費 | 落地順序 |
|---|---|---|---|---|
| Access Control | qty add-on product（A，待驗）或獨立訂閱（B） | AC 席數上限欄位（OQ-B 定名） | 500/seat/月【已拍板】 | SA 先（已接線）→CS/SH（OQ-L） |
| Email Routing | 先 E3（quota 差異、不收費） | `email_routing_max_rules` | 未定【未來佔位】 | 需先補 model＋operator API |
| Backup | B1 flat add-on（未來） | `backup_*` 佔位 | 未定【未來佔位】 | 最遠；需 operator snapshot API |

**共通原則**：加值上限＝plan 欄位（enforcement 天花板）；加值計量＝qty add-on product 或 tier 內含（計費）。**上限與計量分兩層**，靠 enforcement constraint 對齊。

---

## ⑤ 對前波總表／checklist 的差分修訂

> 前波＝「訂閱方案總表」（`subscription-plan-matrix.md`）。**淨結論先講：本次分類變更不改任何價格、不改任何 xmlid、不增減 plan 筆數**——改的是 (a) 敘事歸屬（AI 類→CS 子家族）、(b) 新欄位 `service_type` 的取值、(c) checklist 若干項的具體化與版號順移。

### 5.1 版號順移（本報告新複驗，總表需更正）

| 項 | 總表原文 | 修訂 |
|---|---|---|
| 現況基準 | 「版號 `18.0.1.0.50`；本波 PR base＝.51」 | manifest 現值已＝**.51**（PR #326 merged，已複驗 `src/__manifest__.py:3`）⇒ 本波 PR＝**.52** |
| **A1** | `.50` → `.51` | **`.51` → `.52`** |
| **D 段標題** | `src/migrations/18.0.1.0.51/post-migrate.py` | **`src/migrations/18.0.1.0.52/post-migrate.py`** |

### 5.2 章節/敘事修訂

| 總表位置 | 原內容 | 修訂 |
|---|---|---|
| §1 摘要表「服務」欄 | 「AI — Odoo MCP／n8n MCP／Hermes」 | 改「CS-MCP — …」（歸屬併入 Cloud Service） |
| §6「分表 — AI 類」整段 | 獨立平級分類 | 併為「Cloud Service 的 MCP 子家族」（`template.category='mcp'`）；內容（價格/xmlid/筆數）全數不變 |
| §2.1 全域常數 | plan 端 11 欄 | **12 欄**：加 `service_type`（新 plan XML 一律自帶對應值） |
| Q1（grandfathering） | 「最高急迫、migration 前必查必拍」 | **已拍板 resolve＝不需要**（prod 無人用、stg 純測試）→16 筆直接 force-write |
| Q2（enum 值） | 「PR blocker、MCP 拆不拆懸而未決」 | **已由裁決導出 resolve→3 值、MCP 全歸 `cloud_service`**；降為 confirm（OQ-A） |
| Q3／Q4／Q6 | 開放 | 維持開放，分別深化為 **OQ-D（carrier fork）／OQ-H／OQ-I**（見 ⑥） |
| Q5（前端 picker） | 開放 | 維持；補記：`service_type` 提供 scalar filter 佐證，但 picker 真鑰匙仍是 template M2M（E2） |
| Q7/Q8/Q9/Q10/Q11 | 開放 | 不受本次分類變更影響，維持原追蹤 |

### 5.3 Checklist 逐項修訂

| 項 | 原內容 | 修訂 | 狀態 |
|---|---|---|---|
| **A2** | 查 stg/prod active subs＋grandfathering 拍板寫入 migration | **拍板解除**；降級為 merge 前快速 sanity 複驗「16 plan 無 active 訂閱」（便宜保險、非 gate）；不需政策碼 | 【已拍板】 |
| **A3** | `service_type` enum 拍板（寫 code 前必做） | 本報告 §3.1 給定案建議（3 值）；剩 OQ-A confirm 即解鎖 | 【建議待拍】 |
| **B1** | 加 `service_type` Selection | 具體化：3 值＋**條件式 guard（orphan 放行）**＋落檔 `sh_subscription_plan_service_type.py` | 【建議待拍】 |
| **B2** | `sa_quota.py` 加 `sa_max_access_emails` | 視 **OQ-B**：若拍更名 `ac_max_access_seats` 則落檔 `sh_subscription_plan_ac.py`；default 0、MAX 語意、help 註明「0＋已購席數；email 去重」皆不變。**兩份 doc 務必統一一個名，避免各 seed 一個** | 命名【建議待拍】 |
| **B-新增建議** | — | B4：`sa_max_email_rules`→`email_routing_max_rules` 改名（未 enforce、便宜；OQ-E）；B5：`backup_*` 佔位欄位（OQ-F，可後補不 block） | 【建議待拍】＋【未來佔位】 |
| **C1–C3／C5** | — | 不變（C 段每筆 plan 補帶 `service_type`，隨 §2.1 12 欄自動涵蓋） | — |
| **C4** | 「AI-Hermes 新增 4 筆」 | 內容不變；歸類註記改 **CS-MCP**；XML 自帶 `service_type='cloud_service'` | — |
| **D4** | 「backfill 既有 20 筆（enum 未定）」 | **值明確化（見 5.4 表）**；重點：`plan_mcp_odoo_*`／`plan_mcp_n8n_*`／`plan_mcp_hermes_*` 共 6 筆一律 **`cloud_service`（非 `mcp`）** | 【建議待拍→OQ-A confirm 後即定】 |
| **D6** | grandfathering 處置碼（依 A2） | **移除**（拍板不需要；D1 force-write 即終案） | 【已拍板】 |
| **E2** | template M2M wiring「（smarthome／SA 無 template 不涉）」 | **更正**：SH/SA 各有 1 筆薄 template＋自己的 plan M2M（掃描坐實）。SA 無新檔仍不涉；**SH +MCP 新 2 檔須驗證/補 `smart_home_template` M2M 白名單 wiring**（否則建立流程驗證抓不到新檔） | 工程更正 |
| **G-新增建議 G6** | — | `service_type` 斷言：PR 後 34 筆 plan 全非空；MCP/Hermes 6 筆＝`cloud_service`；guard 對 AnythingLLM orphan 放行（不炸） | 【建議待拍】 |

### 5.4 D4 backfill 對照表（tree 集 ≠ backfill 集，勿混）

> §② 的 30 筆定價樹**排除** placeholder；但 **D4 backfill 集＝既有 20 筆全部**（含 4 筆 placeholder，否則前端/查詢遇 null）。

| 既有 plan（20 筆） | backfill `service_type` | 備註 |
|---|---|---|
| `plan_odoo_monthly/_yearly`、`plan_n8n_monthly/_yearly`（4） | `cloud_service` | 一般 CS |
| `plan_mcp_odoo_*`（2）、`plan_mcp_n8n_*`（2）、`plan_mcp_hermes_*`（2） | **`cloud_service`** | 本次重分類重點：**非 `mcp`** |
| `plan_postgresql_*`（2） | `cloud_service` | placeholder（inactive template） |
| `plan_anythingllm_*`（2） | `cloud_service` | placeholder、**orphan 無 template**→guard 放行 |
| `plan_smarthome_*`（2） | `smart_home` | |
| `plan_security_access_{entry,pro}_{monthly,yearly}`（4） | `security_access` | |

小計：`cloud_service` **14**（含 4 placeholder）／`smart_home` **2**／`security_access` **4** ＝ 20。新增 14 筆由 XML 自帶（12 `cloud_service`＋2 `smart_home`），免 backfill；PR 後 34 筆全覆蓋。

---

## ⑥ 開放問題（按急迫排序，每題附選項＋建議）

### A 組——seed PR 前置 blocker（不拍則欄位/seed 寫不出；三題約 10 分鐘可拍完）

| # | 問題 | 選項 | 建議 |
|---|---|---|---|
| **OQ-A** | `service_type` enum 值 confirm | (1) 3 值 `cloud_service`/`smart_home`/`security_access`；(2) 加 `mcp` 第 4 值 | **(1)**。裁決已導出（MCP 屬 CS 底下）、MCP-ness 由 template.category 承載；confirm 級 |
| **OQ-B** | AC 席數欄位命名 | (1) 保留拍板名 `sa_max_access_emails`；(2) 更名中性 `ac_max_access_seats`（net-new、零成本） | **(2)**。AC 跨三類後 `sa_` 前綴成誤名；**MAX/上限語意不變**（嚴禁偷渡成「內含」語意）；拍定後兩份 doc 統一一名 |
| **OQ-C** | 跨類 capability 欄位（AC/Email/Backup）要不要綁 service_type 硬約束 | (1) 不綁；(2) 綁 | **(1)**。三類都有 CF route、加值刻意跨類；綁了日後賣 CS-AC 就得拆。type-specific 欄位才加軟 guard |

### B 組——加值落地（可與 seed PR 拆、必須有歸宿）

| # | 問題 | 選項 | 建議 |
|---|---|---|---|
| **OQ-D** | **AC carrier**（席數怎麼掛進 sh_subscription）＋年繳席數是否存在（×10？） | (A) qty add-on product 掛同一訂閱；(B) 獨立 AC 訂閱 link 回服務 | **先驗事實**：問 `sh_subscription` repo agent「一筆訂閱是否支援多 product line 且 qty 可變」。支援→**A**（單一發票；#318 刪除聯動天然生效）；否則→B＋**必補 AC 訂閱刪除聯動碼**（防 #324 類孤兒續扣）。欄位先落 default 0，不 block seed PR |
| **OQ-E** | Email Routing 訂價結構 | E1 flat／E2 per-rule／E3 內含 tier 不收費 | **先 E3**（CF 免費、成本壓力低）；欄位改名 `email_routing_max_rules`；前提＝補規則 model＋operator API（現況零實作） |
| **OQ-F** | Backup 欄位層級＋訂價結構 | 欄位：通用 `backup_*` vs `cs_backup_*`；訂價：B1 flat／B2 per-GB／B3 內含 | 欄位**通用 `backup_*`**（cross-category 佔位、CS 先實作）；訂價 **B1**；本波只佔位不 enforcement |

### C 組——工程／未來（不 block 本波 seed）

| # | 問題 | 摘要 | 建議 |
|---|---|---|---|
| **OQ-J** | AC 計費方案是否禁用 `email_domain`/`everyone` 規則 | 一條網域規則可放無限真人、quota 計 0→50 席池被耗盡 | **AC enforce 動工前必答；建議禁用**（讓 quota 等於可控 seat 上界） |
| **OQ-H**（原 Q4） | CS tier→部署規格映射 | Option B（plan 掛 `cs_*`，推薦）vs Option A（per-tier template）；建服務當下 plan 解析；Odoo workers=0→tier-aware（撞 template sticky noupdate）；Hermes 規格是否分階 | 定價正當性議題：不解則**三階＝同規格不同價**。可拆 follow-up，但要與改價 PR 明確掛鉤 |
| **OQ-K** | CF 計費單位＝authenticated-seat 或 listed-email | 決定「清單長度」是否為正確控制面；影響監控與成本懸崖評估 | 由 CF support ticket 坐實（查證問句已備）；坐實前設池用量監控＋銷售護欄 |
| **OQ-L** | CS/SH 的 AC 接線排期 | CS/SH 無 `is_protected`/policy/CF Access sync，「三類加值」對其今天無法 enforce | 排期接線，或先只賣 SA 的 AC（欄位已跨類建模，接線即生效） |
| **OQ-I**（原 Q6） | SA 5/10 App 檔其餘 quota（policies/domains/email_rules）是否隨檔調 | 拍板只給 `sa_max_routes=5/10` | 不影響價格、影響檔位價值感；與 D3 同支 migration 一次定 |
| **OQ-G** | Marketplace 是否要 MCP 視覺子分組 | CS grid 現況 odoo/n8n/mcp-* 混排（`mcp` 已是 category tab） | cosmetic 非 blocker；要更清楚就加 group heading，不涉 schema |

### 已拍板記錄（非開放，僅備查）

Grandfathering 不需要（16 筆直接 force-write）；SH 150/400（400＝+MCP，`ha_mcp_tools` .50 已落地）；SA 1000/2000（5/10 App）；CS Odoo/n8n 三階 1000/3000/5000（1-3/3-6/6-10 人、T3 軟上限≈T2）；MCP 統一 3000（1vCPU/1G/5G）；Hermes 1500/4500/6000（T3 取代會議 6500）；年繳＝月×10；trial 7 天；Project 隱藏；AC 500/seat/月、M=0、email 去重、硬上限掛方案層總量（否決 per-route）、per-policy 20 軟護欄、UI 簡化版＋底層完整 CF policy、CF 成本不對使用者顯示；seed 鐵律（noupdate=1、migration xmlid 定位、既有 xmlid 沿用為最低檔）。

### 建議拍板最小集（解鎖路徑）

1. **現在拍**：OQ-A＋OQ-B＋OQ-C（三題皆 confirm 級）→ 解鎖 seed 重定 PR（base .52）全部 schema/seed/migration 工作。
2. **同步啟動**：OQ-D 事實查驗（問 `sh_subscription` agent 多 line/qty 能力）→ 拍 carrier → 補 AC product（C5）。
3. **AC enforce 動工前**：OQ-J（禁網域規則）＋OQ-K（CF 計費單位坐實）。
4. **follow-up 排期**：OQ-H（tier 規格映射，定價正當性）、OQ-E/OQ-F（Email/Backup 結構）、OQ-L（CS/SH AC 接線）。
