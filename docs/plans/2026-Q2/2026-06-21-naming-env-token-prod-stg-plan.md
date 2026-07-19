---
title: Naming-System Env Token / PAAS_ENV / 反向 Reconciler 分階段實作計畫
category: plans
status: draft
created: 2026-06-21
updated: 2026-06-21
related:
  - docs/archived/tracking/cf-env-token-paas-env-feasibility.md
  - docs/archived/tracking/cf-ns-prod-stg-audit-report.md
  - docs/decisions/patterns/external-resource-env-token-naming.md
---

> **狀態**：範圍已拍板（2026-06-22）—— 本 epic 做 **L1-L3**（階段 0 L5 守線 + 階段 1 L3 + 階段 2 L1+L2），**L4 移出列 follow-up**。決策記錄與 Phase 0 audit 見文末。可進入階段 0/1（TDD 實作）。設計依據見 related 的可行性報告。
> **產出方式**：6 層平行設計 workflow（13 agents）+ 對抗式審查（6 層逐一）+ scout 主線複核（policy name 校正）。

# Naming-System Env Token / PAAS_ENV / 反向 Reconciler — 分階段實作計畫

## 總覽

**目標**：為平台自產的內部命名（SA `canonical_name`、CS/SM `subdomain`）與租戶 namespace 加上**顯式環境標記（env token / label）**，讓 prod/stg 共用同一 CF account + cluster 的資源可被辨識、查詢化，並補上「stored-id 蒸發即永久失蹤」的孤兒治理缺口（帳號級反向 reconciler）。

**範圍（L1-L4 + 橫切 L5）**：
- **L1**：`PAAS_ENV` / `deploy_env` 單一顯式 env 來源原語（Odoo config_parameter + operator pydantic Field，禁前綴隱式推導）。
- **L2**：operator 為新建 ns 打 `paas.woowtech.io/env=stg|prod` 結構化 label，並把 `validate_label_value` 守衛拉到 ns apply 點。
- **L3**：Odoo `naming.py` 單一根注入 env token（`canonical_name` 派生物 = tunnel name + DNS comment + **Access policy name** 帶 token；**DNS hostname / Access app name 走 `subdomain_prefix`、不帶 token**）。
- **L4**：operator 帳號級 list-all 反向 reconciler（dry-run、唯讀、認雙格式前綴），孤兒真解。

**核心紀律（每階段不可省）**：`pre-review checklist`（改前驗證 file:line 現況）→ `TDD red-first`（先寫會紅的測試）→ `adversarial review`（做完跑 false-green lens）。所有測試斷言用 **monotonic / 存在性** 鎖 intent，不硬編完整字串快照（`phase-versioned-test-assertion`）。

**maestri agent 分工一句話**：`scout` 負責本 repo Odoo 端（`src/`，L1 Odoo carrier / L3 / L5 migration / L4 Odoo client）；`paas-charts` 負責 operator src（`extra/paas-operator/`，L1 operator carrier / L2 ns label）；`paas-ci` 負責 `operator-v*` tag build image；`paas-cd` 負責 `paas-app-bundle-cd` Fleet values 每環境注入。

---

## 階段順序與依賴圖

```
階段 0（前置/橫切，L5）  migration 範式 + grep audit + fixture-coupling 清單 + 對抗式 lens 模板
   │（不 ship code，是 L1-L4 全部能安全落地的守線骨架）
   ▼
階段 1（L3，純 Odoo）  naming.py 加 env 參數 + 三呼叫點接 config + lock-step 改 test
   │  單 repo 閉環、走 deploy-stg 即生效、零 cross-repo lock-step
   │  ★ 可最先且獨立 ship ★
   ▼
階段 2（L1+L2，跨 3 repo）  operator deploy_env Field + ns env label + validate 前置
   │  必須同一顆 operator image（L1+L2 同 build）+ Fleet values bump 才生效
   │  build-operator + paas-cd Fleet 三方 lock-step
   ▼
階段 3（L4，operator + Odoo client）  list-all reconciler + dual-format + dry-run
      effort 最高、孤兒真解、獨立於 L1-L3（有 token 只是判 env 更乾淨）
```

| 階段 | 內容 | effort | repo / agent | 阻塞下一階段？ |
|---|---|---|---|---|
| 0 | L5 守線骨架（migration 範式、grep audit、lens 模板） | S | 本 repo / scout | **是**（fixture 清單供階段 1） |
| 1 | L3 naming.py env token（純 Odoo） | M | 本 repo / scout | 否（與 L1/L2 正交，可獨立 ship） |
| 2 | L1 deploy_env + L2 ns label | L | 本 repo operator + paas-charts + paas-ci + paas-cd | 否（L4 不硬依賴） |
| 3 | L4 反向 reconciler | XL | operator + 本 repo Odoo client / paas-charts + paas-ci + scout | — |

**關鍵分水嶺**（`agent-runtime-stg-enablement-gap-chain.md` D1 第二實例教訓）：改 `src/` → `deploy-stg` 帶得上；改 `extra/paas-operator/` → `deploy-stg` **帶不上**，必須 `build-operator`（`operator-v*` tag → CI）+ paas-cd Fleet bump。**每階段第一問固定是「這次有沒有動到 `extra/paas-operator/`？有 → 也要 build+Fleet」**。

---

## 階段 0：L5 橫切守線骨架（前置，不 ship feature code）

### ① pre-review checklist
- 跑全域 grep audit，產出 fixture-coupling lock-step 清單（見下方「跨層風險與 fixture-coupling 清單」），確認零遺漏。
- 確認 migration 範式：對照既有 `src/migrations/18.0.1.0.40/post-migrate.py` 的 model-helper + 冪等慣例；下一個未用版號為 **`18.0.1.0.41`**（現 `src/__manifest__.py:3` 為 `18.0.1.0.40`）。
- 確認 `_CONFIG_PARAMETERS` 位置：`src/hooks.py:123-131`（namespace_prefix 在 `:131`）。
- 確認對抗式審查 lens 模板（su 豁免 / label unit-mock 盲 / 硬編快照 / 對稱 / lock-step / B1 leak）可重用。

### ② TDD 測試清單
階段 0 不寫 feature test，只**驗證既有守線基線綠**（作為「未越界」硬證據基準）：跑 `test_naming.py` + `test_security_access.py` 全綠並記錄條目數，供階段 1 diff 比對。

### ③ 改動站點
- 無 code 改動。產出物：fixture-coupling 清單文件、lens checklist（內化進本計畫）。

### ④ 跨 repo lock-step
- 無。

### ⑤ 對抗式審查焦點
- grep audit 完整性：確認 `src/tests/` 與 `extra/paas-operator/tests/` 所有硬編 `paas-sa-/cs-/sm-/ws-` 格式斷言都進清單。
- **審查已抓（L5 gap）**：env token 注入點與 `naming.py` 實際結構**不符** — `make_*_subdomain`（`naming.py:19/24/29`）目前**無 env 參數**，`_generate_canonical_name`（`security_access.py:202-207`）只是無參呼叫。階段 0 必須先拍板注入層級（見決策點 A），否則階段 1 blast radius 漏盤。

### ⑥ stg 端到端驗證點
- 無（純守線骨架）。

### ⑦ rollback
- 無 code，無需 rollback。

---

## 階段 1：L3 — Odoo naming.py 注入 env token（純 Odoo，可最先 ship）

> **審查已抓 + scout 主線校正（L3 核心 gap，最致命）**：原始設計「tunnel name / DNS comment / Access policy name 三者經 `canonical_name` 免費繼承 token」**方向對，但對抗式審查把 Access app name 與 policy name 混為一談、誤把 policy name 一起否定**。scout 複核 `canonical_name` vs `subdomain_prefix` 兩條派生鏈，正確歸屬：
> - `canonical_name` 派生（**會帶 token**）：tunnel name（`security_access.py:368`）、DNS comment（`:547`）、**Access policy name**（`cloudflare.py:1322` `f"{canonical_name}: {p['name']}"`，canonical 經 `:594`/`:1200` 傳入 sync_access_app）。
> - `subdomain_prefix` 派生（**不帶 token**，user 輸入、route 層 required）：DNS hostname（`:530`/`:555`/`_compute_hostname :918-925`）、**Access app name**（`:777`/`:1105`/`:1203`，主路徑 = `subdomain_prefix`，僅空時 fallback canonical）。
>
> **本階段對 `canonical_name` 注 token → tunnel name + DNS comment + policy name 三者一起帶；DNS hostname / Access app name 一律不注**（對外 URL 由 per-env `paas_domain` 區分）。L3 e2e 斷言驗 **tunnel name + DNS comment +（若該 SA 有設 Access）policy name 帶 token**，絕不對 DNS hostname / Access app name 驗 token（會假紅或永遠測不到）。

### ① pre-review checklist
- 確認注入層級決策（決策點 A）：env token 加在 `naming.py` 三個 `make_*_subdomain` 的新 `env` 參數，default 為 prod-equivalent（空 token），三呼叫點傳入。
- 確認 env 來源用新 config_parameter `woow_paas_platform.deploy_env`（**不沿用** `namespace_prefix` 隱式推導，`external-resource-env-token-naming` 守則 2）。
- 確認 prod token 為**空字串**（env=prod → `paas-sa-` byte-identical 不變）→ 既有硬編快照在預設 prod 下不假紅。
- 確認 immutable guard **不動**：token 只在 `_generate_canonical_name()`（`security_access.py:202-207`）內算入，經 create 首算分支（`:227-237`）一次寫入；`write()` guard（`:241-253`）與 create pop（`:221-225`）保持 context-key `sa_internal_identity_write`，**絕不新增 `not env.su` / `has_group` 豁免**。
- 確認 token 不可塞進 `vals_list` 預設（會被 `:223` `vals.pop('canonical_name')` 靜默吞掉 → pure-function 綠、record 不帶 token 假綠）。
- **審查已抓（L1 gap）**：`deploy_env` 用 **`fields.Selection([('prod','Production'),('stg','Staging')])`** 而非 `Char` — Char 自由輸入會讓 admin 誤填 `STG`/`production ` 產非法 DNS name。naming helper 內對 token 仍做 `.lower()` + strip 防呆，非白名單 fallback `prod`。
- 確認 `_CONFIG_PARAMETERS`（`hooks.py:131` 同段）新增 `'woow_paas_platform.deploy_env'`。
- **審查已抓（CS/SM 決策矛盾）**：原設計決策面說「CS/SM 注 token 須獨立決策、可能 breaking」，實作面卻無條件接（`paas.py:2005` / `smart_home.py:127`）。**本計畫拍板**：CS/SM 因 subdomain = 對外 URL，**首版只接 `env` 參數但傳 `deploy_env`**（與 SA 一致），但列為**決策點 B** 待使用者確認 stg 既有對外 URL 相容性；若使用者選不注，change_sites 5/6 改傳 `env='prod'`。

### ② TDD 測試清單（red-first）

| test 名 | 意圖 | 斷言 | 防哪個 false-green |
|---|---|---|---|
| `test_make_sa_subdomain_prod_has_no_env_token` | prod/預設不插 token | `make_security_access_subdomain(s,r,n,env='prod').startswith('paas-sa-')` 且 `'paas-stg-' not in`；與不傳 env 結果**完全相等** | prod 被誤插 token 全環境漂移；default 偏離 prod-equivalent |
| `test_make_sa_subdomain_stg_carries_env_token` | stg 帶 token 且仍平台前綴起頭 | `r=...env='stg'`；`'stg' in r` 且 `r.startswith('paas-')` 且 `r != prod版`（存在性+差異性，**不硬編** `'paas-stg-sa-'`） | token 沒生效假綠；硬編快照 phase-versioned 假紅 |
| `test_make_subdomain_env_token_dns_safe_all_envs` | 最長 slug × {prod,stg} × {cs/sm/sa} 皆合法 | `len<=63` 且 `==lower()` 且 `re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', r)` | 非法字元/尾 dash/超長（#208 命名側） |
| `test_make_namespace_prod_stg_distinct_and_safe` | namespace 既有行為不回歸 | `make_namespace(s,'paas-ws-') != make_namespace(s,'paas-ws-stg-')` 且兩者 match k8s regex（不硬編 16） | 動簽章意外破壞 namespace |
| `test_canonical_name_uses_deploy_env_config` | **端到端 model 層**：SA create 依 config 注 token | `set_param deploy_env='stg'` → create SA → `sa.canonical_name` 含 `'stg'` 且 `startswith('paas-')`；prod/不設 → 不含 stg token | naming 改了但 `security_access.py:207` 沒讀 config 傳入（注入沒接通假綠） |
| `test_smarthome_subdomain_uses_deploy_env` | **端到端**：SM `_generate_subdomain`（`smart_home.py:127`）接通 | `set deploy_env='stg'` → `home._generate_subdomain()` 含 stg 且 `startswith('paas-')` | 三呼叫點漏改 SM |
| **`test_cs_subdomain_uses_deploy_env`（審查已抓補測）** | **CS（`paas.py:2005`）端到端驗 token 接通** — 原設計缺此測 | 走 `_create_cloud_service` controller 路徑（或直呼蒐集點）：`set deploy_env='stg'` → CS `subdomain` 含 stg；prod 不含 | **CS 呼叫點漏接無守線**（原計畫 CS 只在 pure-function 層出現） |
| `test_canonical_name_immutable_under_su_with_token` | token 後 immutable 仍對 su 生效 | create（deploy_env=stg）→ `with_user(SUPERUSER) write({'canonical_name':...})` → `assertRaises(UserError)`（deny 分支真跑到） | 為注入改成 env.su 豁免 → TransactionCase su env deny 分支跳過假綠（`su-env-exempt-guard-false-green`） |
| `test_deploy_env_config_parameter_roundtrip` | Selection 欄位綁 config_parameter | `set woow_deploy_env='stg'` → execute() → `get_param('woow_paas_platform.deploy_env')=='stg'`（斷言硬寫**字面 key**，非反讀欄位定義） | config_parameter key 打錯/沒持久化假綠 |
| `test_deploy_env_in_config_parameters_cleanup_list` | 卸載清理 | `'woow_paas_platform.deploy_env' in hooks._CONFIG_PARAMETERS`（用 `in`，不依賴 len/index） | 漏加殘留 ir.config_parameter |

> **審查已抓（test_naming.py 條數誤記）**：原計畫稱 11 條 startswith，實機只有約 8 條 startswith（`test_naming.py:57/72/84/102/103/117`）+ 數條 len/split 硬編（`:58/:63/:75/:78/:79/:86/:89/:90/:119`）。預設 prod 空 token 下這些 byte-identical **不假紅**，**不需改**；只有新增的 stg 測試用存在性斷言。

### ③ 改動站點（file:line + repo + agent）
| file:line | 改動 | repo / agent |
|---|---|---|
| `src/services/naming.py:18,23,28` | 三個 `make_*_subdomain` 各加 `env=''` 參數；新增 helper：`env in ('','prod') → ''`，else → `f"{env.lower().strip()}-"`（DNS-safe）。`make_namespace` 不動 | 本 repo / scout |
| `src/models/res_config_settings.py:31`（namespace_prefix 後） | 新增 `woow_deploy_env = fields.Selection([('prod','Production'),('stg','Staging')], config_parameter='woow_paas_platform.deploy_env', default='prod', help='部署環境 prod\|stg，CF 命名 env token；禁從 namespace_prefix 隱式推')` | 本 repo / scout |
| `src/views/res_config_settings_views.xml`（namespace_prefix field+label 後，約 `:39-43`） | 加 `<label for="woow_deploy_env"/>` + `<field name="woow_deploy_env"/>` + row div **三件成對**（審查已抓：勿只插 field） | 本 repo / scout |
| `src/hooks.py:131` | `_CONFIG_PARAMETERS` append `'woow_paas_platform.deploy_env'` | 本 repo / scout |
| `src/models/security_access.py:207` | `_generate_canonical_name` 讀 `self.env['ir.config_parameter'].sudo().get_param('woow_paas_platform.deploy_env','prod')` → 傳 `env=` 給 `make_security_access_subdomain`。**guard 不動** | 本 repo / scout |
| `src/models/smart_home.py:127` | `_generate_subdomain` 同樣讀 config 傳 `env=` | 本 repo / scout |
| `src/controllers/paas.py:2002-2005` | 已讀 ns_prefix；新增讀 `deploy_env` 傳 `env=` 給 `make_service_subdomain`（**待決策點 B 確認注/不注**） | 本 repo / scout |
| `src/tests/test_naming.py` + `test_security_access.py` + `test_smart_home.py` + 新 CS 測試 | 新增上述 red-first 測試 | 本 repo / scout |

### ④ 跨 repo lock-step
- **無**。L3 不碰 `extra/paas-operator/`，operator `generate_subdomain`（`cloudflare.py:719`）不吃 `naming.py`，A 類 delete 全走 stored 值天然對稱（tunnel teardown 用 `self.canonical_name`、DNS/Access delete 用 stored `subdomain_prefix`，雙 stored 對稱腿）。**單 repo 閉環、deploy-stg 一條路驗完。**

### ⑤ 做完的對抗式審查焦點
- **guard 假綠**：`git diff src/models/security_access.py` 確認**零** `env.su` / `has_group` 新增；`test_canonical_name_immutable_under_su_with_token` 確實打到 deny 分支。
- **CS/SM 接通**：三條路徑各跑一條真 create（SA/SM/CS）驗 stored 值帶 token，非只測 pure function。
- **硬編快照**：新測無完整字串快照（`'paas-stg-sa-xxxx-yyyy'`），用 `in` / 差異性 / regex。
- **fixture-coupling**：`test_security_access.py:330/497/600/736` 把 `subdomain_prefix` 設成 `sa.canonical_name`（審查已抓）— 預設 prod 空 token 下不變紅；確認這些不需 lock-step（subdomain_prefix 走 user 輸入路徑，與 token 無關）。
- **deploy_env Char→Selection**：確認 UI 端已限白名單 + naming helper 有 `.lower().strip()` 防呆。

### ⑥ stg 端到端驗證點（hard gate，審查已抓：原設計只列 risk 未升格）
1. deploy-stg 前**顯式** `set deploy_env='stg'`（否則 `default='prod'` fail-safe 會讓 stg 資源偽裝成 prod、辨識性靜默落空、**無報錯**）。
2. stg 真建一筆 SA → 查 live CF：**tunnel name 含 stg token** + **DNS comment 含 stg token** +（若該 SA 有設 Access）**policy name 含 stg token**（playwright-cli / `kubectl exec` operator）。**不驗** DNS hostname / Access app name（走 subdomain_prefix）。
3. delete 該 SA → 再查 tunnel/DNS/policy 三者皆不存在（create↔delete 對稱、孤兒零殘留）。

### ⑦ rollback
- addon 退回前一 git tag（`v18.0.1.0.40`），`naming.py` 還原。
- migration 若已跑（階段 1 本身無 migration；backfill 在 L5）：因只新不改、冪等，留著無害，不需反向 migration。

---

## 階段 2：L1 + L2 — PAAS_ENV 原語 + ns env label（跨 3 repo，同 image lock-step）

> **審查已抓（L2 動機框架失準）**：原計畫把 ns 路徑風險寫成「#208 silent-swallow / 吞成 WARNING / 74 天沒發現」。實機：`create_namespace`（`helm.py:684`）與 `_ensure_namespace`（`agent_runtimes.py:90`）走 `kubectl apply -f` 經 `_run_command`，非零 returncode **直接 raise**（非 label_pvcs `:1200` 的 silent warning）。**動機應改寫為「把 fail-loud 邊界從 kubectl 回傳提前到 Python pre-validation，便於 unit-catch + 避免 yaml dump 落檔才失敗」**，不要誤導 reviewer 以為不修就重演 74 天盲區。

### ① pre-review checklist
- **審查已抓（L1 硬阻塞）**：`config.py:19` 目前是 `namespace_prefix`，repo grep `deploy_env`/`PAAS_ENV` **零命中** → L1 是**新增非確認**。L2 first commit 前先 `grep deploy_env extra/paas-operator/src/config.py` 有命中才能開工（否則 `settings.deploy_env` AttributeError）。
- L1 operator Field **default 必為空字串 `""`**（= unknown，啟動不 crash），**絕不** default `prod`/`stg`（未設值環境靜默貼錯 label 比 fail 更糟）。fail-closed 責任下推 L2 消費點（empty 時 omit label）。
- L1 operator Field 加 `@field_validator`：只接受 `{"", "prod", "stg"}`（含大寫如 `Prod` 一律 reject），非白名單 config load 即 `ValidationError`。
- L1 白名單每值須先過 `validate_label_value`（`helm.py:93`）自證合法：`stg`/`prod` 合法、`paas-ws-stg-`（尾 dash）非法。
- **審查已抓（路徑錯）**：所有引用更正為 `extra/paas-operator/src/services/helm.py:93`（在 `services/` 子目錄），測試 import `from src.services.helm import validate_label_value`，否則 import error 整檔 collection 失敗。
- **審查已抓（測試檔不存在）**：`test_res_config_settings.py` / `test_hooks.py` / `test_config.py` 三檔**目前不存在**，change_sites 須明列**新建**並確認 Odoo TransactionCase 基底 + test tag（否則 `--test-tags /woow_paas_platform` 收集不到 = 假綠）。
- **審查已抓（Settings 全域單例污染）**：`config.py:107 settings = Settings()` 是 import-time 單例；新 validator 後若 CI runner 殘留非法 `PAAS_ENV`，import config 即 raise 連坐無關測試。tdd 一律 `Settings(_env_file=None)` 或 `monkeypatch.delenv`。
- 確認舊 image + 新 Fleet env 向後相容（**校正**：原稱「預設 `extra='ignore'`」事實錯——pydantic-settings 預設其實是 `extra='forbid'`，但 `forbid` **只約束 init / dotenv 傳入鍵、不約束任意 OS 環境變數**）→ 舊 operator image（無 `paas_env` field）遇 Fleet 注入的 `PAAS_ENV` 環境變數仍**不 crash**（OS env 的未宣告 key 被忽略）。**勿據「extra=ignore」前提**在別處用未知 key 做 `Settings(**kwargs)` / dotenv，那會被 forbid 擋。
- L2 label key 用 `paas.woowtech.io/env`（對齊既有 `paas.woowtech.io/orphaned-at`），非裸 `env`。
- L2 空值語義：`deploy_env` 空時**完全 omit** label（不打 `env=` 空 value），backward-compatible（本機 docker-desktop）。
- L2 兩注入點 lock-step：`helm.py:684` create_namespace + `agent_runtimes.py:93` `_ensure_namespace`（`agent_runtimes.py:282` 是 LLM secret 非 ns，正確排除）。
- **審查已抓（validate 落點）**：validate 放**呼叫端**（create_namespace 與 _ensure_namespace 各自驗 env value），**不下沉** `apply_manifest`（`helm.py:1066` 泛用、多 caller，下沉會擴大 blast radius + untested 回歸）。
- Fleet 注入避開 split-values drift（`fleet-split-registry-values-merge-drift`）：`PAAS_ENV` 走 operator Deployment **獨立 env list entry**（非與 registry map 同名跨 `valuesFiles`+`valuesFrom` 拆 key）。prod registry secret drift 休眠中，rollout 時一併重整、勿單獨碰。

### ② TDD 測試清單（red-first）

| test 名 | 意圖 | 斷言 | 防哪個 false-green |
|---|---|---|---|
| `test_deploy_env_config_parameter_roundtrip` | Odoo Selection 綁 config_parameter | （同階段 1，若 L1 Odoo carrier 併此階段）`get_param('woow_paas_platform.deploy_env')` 往返；硬寫字面 key | key 打錯假綠 |
| `test_operator_paas_env_field_exists_and_defaults_empty` | operator Field 預設空 | `Settings(_env_file=None).paas_env == ''`（斷言**是空**，非具體環境） | 預設貼 prod/stg 致 dev sandbox 自稱 prod |
| `test_operator_paas_env_reads_from_env_var` | env var 注入鏈 | `monkeypatch PAAS_ENV=prod` → `Settings().paas_env=='prod'`；`stg`→`'stg'`（驗注入鏈非 mock 物件） | Field 名/大小寫不符 → Fleet 設了讀不到 split-brain |
| `test_operator_paas_env_rejects_invalid_value_fail_closed` | 白名單 fail-closed | `PAAS_ENV='paas-ws-stg-'/'staging'/'production'/'Prod'` → `ValidationError`；`''`/`'prod'`/`'stg'` 不 raise | prefix 字面/大寫非法值靜默通過貼成 label |
| `test_paas_env_whitelist_values_are_legal_k8s_label_values` | L1↔L2 契約 | `for v in {'prod','stg'}: validate_label_value('paas.woowtech.io/env',v)` 不 raise；`'paas-ws-stg-'` raise | 白名單值帶尾 dash → L2 atomic 拒（#208） |
| `test_create_namespace_stamps_env_label_when_deploy_env_set` | marketplace ns 帶 label | **patch `yaml.safe_dump`（或 `NamedTemporaryFile`）捕獲 ns_manifest dict** → `labels['paas.woowtech.io/env']==deploy_env` 且 `labels['paas-managed']=='true'` | **審查已抓**：mock subprocess + temp 檔 finally 已刪 → 拿不到 labels → 被逼成只驗 call_count 假綠 |
| `test_create_namespace_omits_env_label_when_deploy_env_empty` | 空值 omit | `'paas.woowtech.io/env' not in labels` 且 paas-managed 在 | 空值打 `env=` 污染查詢 |
| `test_ensure_namespace_stamps_env_label_lockstep` | agent ns lock-step | mock `apply_manifest` → `c.args[0]['metadata']['labels']` 含 env + paas-managed | 兩注入點只改一處 divergence |
| **`test_ensure_namespace_omits_env_label_when_empty`（審查已抓補測）** | agent 路徑 omit 對稱 | agent 路徑 deploy_env 空時 `'paas.woowtech.io/env' not in labels` | agent 路徑只測 set 漏測 empty omit |
| `test_create_namespace_validates_env_label_before_apply` | 守衛在 apply 前 | deploy_env 非法 → `create_namespace` raise 且 `subprocess.run` **未被以 apply 呼叫** | 繞過 validate → 非法 value 才到 kubectl |
| `test_ns_env_label_is_selectable_by_kubectl`（integration，真 kubectl） | label 真落地可選 | `test_namespace` fixture + deploy_env='stg' → `kubectl get ns -l paas.woowtech.io/env=stg -o jsonpath` 含該 ns 且值=='stg' | mock 測不到真 kubectl label 驗證（#208） |
| `test_real_kubectl_rejects_illegal_env_label_value`（integration） | validator↔kubectl 契約一致 | `validate_label_value(...,'paas-ws-stg-')` raise；真 `kubectl label ... env=paas-ws-stg-` returncode!=0 且 stderr 含 'invalid' | validator 放行但 kubectl 拒（契約漂移） |

> integration 測試**無 CI runner**，須本機 real kubectl 跑過再 merge（`CLAUDE.md` 規定），掛 `pytest.mark.integration` + `test_namespace`/`app_client` fixture。

### ③ 改動站點
| file:line | 改動 | repo / agent |
|---|---|---|
| `extra/paas-operator/src/config.py:19`（namespace_prefix 後） | 加 `paas_env: str = Field(default='', ...)` + `@field_validator('paas_env')` 限 `{'','prod','stg'}`。註解標：值由 cross-repo paas-app-bundle-cd Fleet 注入（對齊 `config.py:76-77`），空＝unknown | 本 repo operator / paas-charts |
| `extra/paas-operator/src/main.py`（namespace prefix log 後、helm fail-fast 前） | `logger.info(f"Deploy env (PAAS_ENV): {settings.paas_env or '(unset)'}")`（審查已抓：插在 helm 初始化前，否則 helm 不可用時永不印） | 本 repo operator / paas-charts |
| `extra/paas-operator/src/services/helm.py:684` | create_namespace：`settings.paas_env` 非空時 labels 加 `paas.woowtech.io/env`；**apply 前**對 value 跑 `validate_label_value` | 本 repo operator / paas-charts |
| `extra/paas-operator/src/api/agent_runtimes.py:90-93` | `_ensure_namespace` labels 同步加 env（lock-step 同讀 `settings.paas_env`、同經 validate） | 本 repo operator / paas-charts |
| `extra/paas-operator/tests/test_config.py`（**新建**） | default 空 / env 注入 / 白名單 fail-closed / 過 validate_label_value 四組 | 本 repo / scout |
| `extra/paas-operator/tests/test_helm.py` | create_namespace stamp/omit/validate-前置/prefix 字面拒（patch yaml.safe_dump 捕 dict） | 本 repo / scout |
| `extra/paas-operator/tests/test_agent_runtimes.py` | `_ensure_namespace` env label lock-step + omit | 本 repo / scout |
| `extra/paas-operator/tests/integration/test_ns_env_labeling.py`（**新建**） | 真 kubectl `-l` 命中 + 拒非法 value | 本 repo / scout |
| `src/models/res_config_settings.py` / `src/hooks.py` / `src/tests/test_res_config_settings.py`（**新建**）/ `test_hooks.py`（**新建**） | L1 Odoo carrier（若未隨階段 1 一併做） | 本 repo / scout |

### ④ 跨 repo lock-step（順序 + 未同步後果）
```
1. paas-charts: operator config.py 加 PAAS_ENV Field + helm/agent ns label stamp（同一顆 image）
2. paas-ci: operator-v{ts} tag → CI build 含 L1+L2 的 image
3. woow-paas-charts (operator Deployment chart): templates/deployment.yaml env block 開 `- name: PAAS_ENV` 孔 + values.yaml `paasEnv: ""` 預設
4. paas-cd: paas-app-bundle-cd values-stg.yaml 設 paasEnv: stg / values-prod.yaml 設 prod（獨立 env entry，避 split-values drift）
5. paas-cd: Fleet bump operator image tag 到步驟 2 產出值 → Fleet sync
```
- **L1+L2 必須同 image**：只加 label 邏輯沒加 Field → label value 落空/default。
- **漏 Fleet bump（最致命）**：image build 出但沒 bump → stg 跑舊 image、L1/L2 靜默半套生效，addon 測試全綠卻看不出（`gap-chain` D1 第二實例）。
- **chart 未開孔**：Fleet 設了值不落 pod env → operator 永遠讀空 → 全 ns 不帶 label（單層不可觀測）。
- **Fleet 填非法值**（尾 dash）→ operator validator pod 啟動 raise CrashLoop（fail-loud，但擋 operator 上線，故 Fleet 值務必先過白名單）。
- 三方任一先上都安全（image 空字串 fail-closed 無害 / 舊 image ignore extra env），但**全到齊功能才生效**。

### ⑤ 對抗式審查焦點
- **兩注入點 lock-step**：grep `paas-managed` 全 operator，確認 `helm.py:684` + `agent_runtimes.py:93` 都加了 env label，無第三漏網 ns apply 點（grep `create_namespace=True` 確認無路徑繞過讓 helm `--create-namespace` 建無 label ns）。
- **validate 在 apply 前**：故意非法值跑 integration，確認 raise 在 `kubectl apply` 之前。
- **空值一致**：本機 docker-desktop（空）跑一遍，ns 完全不帶 env label，零行為改變。
- **integration 真命中**：確認掛 `pytest.mark.integration` + `test_namespace` + 真 `kubectl get -l`，非 mock 殘留。
- **Fleet split-values drift**：deploy 後查 `bundledeployment.status.modifiedStatus.patch`（live→desired）確認 `paasEnv` 沒被算成 null。

### ⑥ stg 端到端驗證點（三段都要過，審查已抓升格 merge-gate）
1. `kubectl -n paas-stg-system get deploy <operator> -o jsonpath` → **image tag ≥ 目標 build**。
2. `kubectl exec <operator-pod> -- env | grep PAAS_ENV` → **stg=stg**（非空）。
3. 新建一個 ns → `kubectl get ns -l paas.woowtech.io/env=stg` 回非空且新 ns 在內。
> 只驗 label 看不出 image 沒 bump（前層擋著）— 三段缺一不可。

### ⑦ rollback
- **Fleet 軌**：values revert（git revert + force-sync）→ 上一版 operator image 不認 env 但功能不受影響。
- **image 軌**：operator image 退回 `operator-v*` 前一 tag（ns label 純 metadata，退掉不影響 routing）。
- **Odoo 軌**：L1 Odoo carrier 隨 addon tag 退回。

---

## 階段 3：L4 — operator 帳號級反向 reconciler（孤兒真解）

> **審查已抓（最高衝擊 — prefix 過濾漏盤）**：原計畫前綴集合寫死 `{paas-sa-, paas-sm-, paas-cs-}`，但 CS 的 mcp sidecar route 實際 CF 名是 `{release}-{ns_suffix}-mcp`（`generate_subdomain` `cloudflare.py:719` → `releases.py:251,704`），**根本不以 `paas-cs-` 開頭**（`cf-ns-orphan-no-account-gc.md` 載明 `svc-*-mcp` 孤兒源）。用原前綴集合會**系統性漏掉所有 CS sidecar route 孤兒**。**修**：前綴集合須涵蓋 operator `generate_subdomain` 真實產出樣式（`{release}-*` / `*-mcp`），或改「非平台白名單反扣」。

> **審查已抓（dual-format 押錯）**：原計畫圍繞 `paas-stg-sa-` vs `paas-sa-` 設計過濾，但 L3 注 token 前該格式**不存在**；真正存在的 dual-format 是 `paas-cs-*`（主 route）vs `svc-*-mcp`（sidecar）。L3 後置的 `paas-stg-*` 測試標 skip / L3-gated。

> **審查已抓（integration 無 CF cred）**：`tests/integration/README.md:75-95` 明文 integration runner **無 Cloudflare credentials**。L4 真 CF 契約守線**不能**靠現有 integration harness（會 skip = 沒守線，正中 #208 盲區）。**修**：改走 **stg `kubectl exec` 打 operator endpoint** 的 live 驗法（比照 `operator-cf-access-delete-202` live 驗證），或先補 README:91 的 sandbox-zone gated cred。

### ① pre-review checklist
- 確認 operator 現況 list-all = 0（grep `cfd_tunnel` 只有單-id；Access 只有 `list_identity_providers`）→ L4 是新功能。
- 確認分頁前例：`_list_dns_records_by_content` 寫死 `per_page=100`（曾漏 15 筆）→ 新 list-all 必 `per_page=500` + 迴圈翻頁（讀 `result_info.total_count`/空止）。
- **審查已抓（差集左集 policy 三層 walk）**：SA 的 `cf_policy_id` 在三層巢狀 `SA → route → access_policy_ids[].cf_policy_id`（`security_access.py:772/1099/1179`），非主表。`reconcile_orphans` 蒐集必須**三層展開**，漏走任一層 → 活躍 policy 被當孤兒 → 人工照單刪 = 最高衝擊事故。
- 確認差集左集完整：SA `tunnel_id`/route `dns_record_id`/`access_app_id`/policy `cf_policy_id`、CS `subdomain`+`helm_namespace`（**CS 走 shared tunnel 無 dedicated tunnel_id**）、SM `tunnel_id`+`subdomain`。
- 確認 CF tunnel list-all 過濾 `deleted_at` 非 null（CF 軟刪仍列出）。
- 確認 policy 孤兒判定**不信 DB 白名單**（`sync_access_app` resync 漂移）→ `GET /access/apps/{id}/policies` 看 CF 實際引用 + `409 in-use` 兜底。
- 確認 token scope（帳戶級）：list-all-tunnels 需 `Cloudflare Tunnel:Read`、list Access 需**帳戶級** `Access:Apps&Policies(Read)`（dry-run 只需 Read）。
- 確認 env 分流無 token 退化：靠 `DNS content 指 shared tunnel`（stg `55953bfa` / prod `328c3a28`）+ 前綴反推；**審查已抓**：dedicated tunnel（SA/SM）連此線索都沒 → env_hint 須**誠實標 `unknown`**，不得瞎猜成 stg。
- 確認 dry-run 為**首版唯一行為**（`dry_run` 預設 True 且首版拒收 False fail-loud），無任何 delete 路徑。
- **審查已抓（async 假綠）**：每個 async test 帶 `@pytest.mark.asyncio`（漏帶靜默 skip）；建議 conftest 設 `asyncio_mode=auto` 從機制消除。

### ② TDD 測試清單（red-first）

| test 名 | 意圖 | 斷言 | 防哪個 false-green |
|---|---|---|---|
| `test_list_all_tunnels_paginates_beyond_single_page` | 翻頁不截斷 | mock 兩頁 → 第二頁某 id ∈ 結果 且跨頁筆數 `>= per_page+1`；`client.get` 被呼叫 `page>1` 次 | per_page 截斷漏孤兒（漏 15 筆前例） |
| `test_list_all_tunnels_filters_real_naming_formats`（審查已抓改寫） | **認真實命名格式** | mock 混入 `paas-sa-*`、`{release}-{ns}-mcp`、`svc-*-mcp`、`paas-cs-*`、非平台 `random` → 平台格式全留、`random` 濾掉 | **dual-format 漏認 CS sidecar 孤兒**（原計畫漏） |
| `test_list_all_tunnels_excludes_soft_deleted` | 軟刪排除 | `deleted_at` 非 null 的 paas-sa-* `not in` 結果 | 已刪當活孤兒誤導刪除 |
| `test_reconcile_diff_marks_only_unreferenced_as_orphan` | 差集核心 | CF `{A,B,C}` × Odoo `{A,B}` → `orphan=={C}`（set 運算） | 差集方向反/漏 stored-id 誤刪活躍 |
| `test_reconcile_collects_three_level_sa_policy_ids`（審查已抓補測） | **SA→route→policy 三層 walk** | 多 route 多 policy 的 SA，**全 policy 的 cf_policy_id 都進左集** | 漏走巢狀層 → 活躍 policy 當孤兒（最高衝擊） |
| `test_reconcile_is_dry_run_no_delete_calls` | 零副作用 | `delete_tunnel`/`_delete_access_app`/`delete_reusable_policy`/`_delete_dns_record` 全 `call_count==0`；result 有 orphan list | 發現耦合進刪除（共用 prod CF account 最危險回歸） |
| `test_access_policy_orphan_judged_by_cf_app_reference_not_db` | 不信 DB 白名單 | mock `GET /access/apps/{id}/policies` 回 P 仍引用（即使 P∉DB 白名單）→ `P not in orphan`；list-policies 被呼叫 | resync 漂移誤刪在用 policy |
| `test_reconcile_env_split_degrades_without_token` | 不硬依賴 L1 | 無 token 仍回非空候選（fixture 注入 shared tunnel id 變數，**不硬編** stg id）；有 token ⊇ 無 token 識別力 | L4 被誤做成 hard-require PAAS_ENV |
| `test_dedicated_tunnel_env_hint_is_unknown_without_token`（審查已抓補測） | **dedicated tunnel 誠實標 unknown** | 無 token 時 SA/SM `env_hint=='unknown'`（不猜 stg）；shared-tunnel CS 才用 content 反推 | 瞎猜致人工把 prod 當 stg 刪 |
| `test_reconcile_endpoint_dry_run_contract` | HTTP 契約 | FastAPI TestClient 200，body 四類 key，每筆含 id/name/env_hint，**無 `deleted`/`removed` 欄位** | endpoint↔Odoo client shape drift（BUG-8） |
| `test_reconcile_live_stg_readonly`（審查已抓改 live） | **真 CF 契約**（不走無 cred 的 integration） | stg `kubectl exec` operator 打 reconcile endpoint → 回值每筆有 id/name/deleted_at；零 delete | mock 遮蔽真 CF list-all 契約（#208） |

### ③ 改動站點
| file:line | 改動 | repo / agent |
|---|---|---|
| `extra/paas-operator/src/services/cloudflare.py`（約 `:399` 後） | `list_all_tunnels()`：`GET /accounts/{aid}/cfd_tunnel` per_page=500 翻頁，過濾 `deleted_at` + name 前綴集合（含 `{release}-*-mcp` / `svc-*` / `paas-{sa,sm,cs}-` + L3 後 `paas-stg-*`） | 本 repo operator / paas-ci |
| `extra/paas-operator/src/services/cloudflare.py`（約 `:1148`） | `list_all_access_apps()` / `list_all_access_policies()` 分頁 + `list_policies_of_app(app_id)` | 本 repo operator / paas-ci |
| `extra/paas-operator/src/api/reconcile.py`（**新建** router `/api/reconcile`） | 唯讀 endpoint：接 Odoo stored-id body → 三 list-all + list-policies-of-app → 差集 → dry-run 四類候選 JSON（id/name/env_hint）。env_hint 用 PAAS_ENV 否則 DNS content 反推、dedicated tunnel 標 unknown | 本 repo operator / paas-ci |
| `extra/paas-operator/src/models/schemas.py` | `ReconcileRequest`（stored-id 集合）+ `ReconcileResponse`（OrphanCandidate{id,name,kind,env_hint}）；`dry_run` 預設 True 首版拒 False | 本 repo operator / paas-ci |
| `extra/paas-operator/tests/test_reconcile.py`（**新建**） | 上述單元測試（每 async test 帶 `@pytest.mark.asyncio`） | 本 repo / paas-ci |
| `src/services/paas_operator.py`（約 `:802`） | Odoo client `reconcile_orphans(stored_ids)`：蒐集 CS/SA(**三層 walk**)/SM stored-id，GET reconcile endpoint（DEFAULT_TIMEOUT 唯讀） | 本 repo / scout |
| `src/models/subscription_lifecycle_cron.py`（約 `:118`） | **首版採 cron+UI 報表**（決策點 D）：唯讀對帳 cron 呼叫 reconcile_orphans，候選寫 ir.logging / transient model 供 /woow 後台呈現；只報告不刪 | 本 repo / scout |

### ④ 跨 repo lock-step
- operator image（含 reconcile endpoint）**先**部署，Odoo `reconcile_orphans` client **後**上線（否則 404；BUG-8 class HTTP 契約對）。唯讀 dry-run，未同步最壞只是「對帳功能暫不可用」，不壞 provision/teardown。
- **prod token scope 為 prod rollout 硬阻塞前置**（審查已抓升格）：prod token 須含帳戶級 `Access:Apps&Policies(Read)` + `Tunnel:Read`，否則 list-all 403 在 prod 靜默空轉。reconciler 對 403 **fail-loud**（回明確錯誤，非空候選），否則「無孤兒」與「沒權限」無法區分。
- L4 唯讀列舉不需 k8s RBAC（純 CF API token）。

### ⑤ 對抗式審查焦點
- **差集左集完整性反查**：人工逐一核對蒐集來源是否漏（CS 無 dedicated tunnel_id 只有 subdomain+helm_namespace；SA route 的 dns_record_id 在子模型；policy 三層 walk）。
- **dry-run 真零副作用**：grep reconcile 路徑無任何 `delete_*`/`uninstall`/`kubectl delete`；integration/live 用 delete spy `assert_not_called`。
- **dual-format 完整性而非正確性**：反向問「還有沒有第三種格式漏認」（歷史 id ns `paas-ws-1/5/253` 前綴與 prod 撞、`svc-*-mcp` vs `paas-cs-*-mcp` 兩種 mcp 命名）。
- **async 假綠掃描**：逐一確認 `@pytest.mark.asyncio`；mock CF 回值 shape（`result_info`/`deleted_at`）與真 CF 一致。
- **env_hint dedicated tunnel 誠實 unknown**：構造 SA/SM 無 content 線索情境，確認標 unknown 非 stg。

### ⑥ stg 端到端驗證點
- stg `kubectl exec <operator-pod> -- curl /api/reconcile`（帶 stored-id body）→ 回 dry-run 候選 JSON，shape 對齊 Odoo client 期望，**零 delete**。
- 驗一筆已知 stg 孤兒（手動建後繞 Odoo unlink 刪 DB record）出現在候選清單；驗一筆活躍資源**不**出現。

### ⑦ rollback
- operator image 退回 `operator-v*` 前一 tag（reconcile endpoint 消失，Odoo client 呼叫回 404，對帳功能不可用但無副作用）。
- Odoo client / cron 隨 addon tag 退回。
- **無 migration、無 delete → 無資料面 rollback 需求**。

---

## 跨層風險與 fixture-coupling 清單

### 加 env token 會紅的測試斷言 lock-step 清單（grep audit 產出）
> 預設 prod 空 token 下這些**全部 byte-identical、不假紅、不需改**；**僅當改 fixture 走 stg 時**才須 lock-step 放寬為存在性斷言（`startswith('paas-')` + `in` + `<=63` + regex），**絕不刪除繞過**（刪除 = 假綠回歸無守線，`credential-claim-test-fixture-coupling`）。

| file:line | 斷言 | 預設 prod 行為 | stg fixture 時改法 |
|---|---|---|---|
| `src/tests/test_naming.py:57/72/84/102/103/117` | `startswith('paas-{cs,sm,sa,ws}-')` | 不變 | 放寬 `startswith('paas-')` |
| `src/tests/test_naming.py:58/63/75/78/79/86/89/90/119` | `len(ns)==16` / `len(parts)==4` / `len(result)==len('paas-sa-')+8+1+8` | 不變 | 改 `>=` / `<=63` monotonic，移除 split count 硬編 |
| `src/tests/test_security_access.py:32/63` | `canonical_name.startswith('paas-sa-')` | 不變 | 放寬 `startswith('paas-')` + `'sa' in` |
| `src/tests/test_security_access.py:330/497/600/736` | `'subdomain_prefix': sa.canonical_name`（fixture） | 不變（subdomain_prefix 走 user 路徑、與 token 無關） | 通常不需改；若 canonical 帶 token 則該 fixture 連帶帶 token，需確認測試 intent |
| `src/tests/test_security_access.py:111/178/211/243` | `'tunnel_name': 'paas-sa-x-y'`（mock 輸入值） | 不變（mock 輸入非 naming 輸出） | 多數不需改；僅 naming 輸出被斷言處（如 `:122` `==sa.canonical_name`、`:354` `comment==canonical_name`）才連動 |
| `src/tests/test_smart_home.py`（subdomain 斷言） | `startswith('paas-sm-')` | 不變 | 放寬 |
| `extra/paas-operator/tests/test_tunnel_routes_sync.py:111/231` | `comment=='paas-sa-x-y'`（多為 mock 輸入） | 不變 | 僅 naming 輸出被斷言處才改 |
| **operator src validator（審查已抓，非 test 但 lock-step）** `schemas.py` / `namespaces.py` / `helm.py:81` `validate_namespace` startswith `paas-ws-` | namespace 結構硬驗 | 不變（L3 不動 namespace） | 若 L1 改 namespace_prefix 約定須同步 audit |

### migration / backfill（L5，隨階段 1 或獨立）
- **`src/migrations/18.0.1.0.41/post-migrate.py`（新建）**：對舊 record 補 env 旁路標記，**只新增不改 `canonical_name`**（immutable，改會觸 guard + 破壞 stored-id 反查對稱）；冪等（重跑不重複）；呼叫 model helper（對照 `18.0.1.0.40` `_backfill_*` 慣例）。
- `src/__manifest__.py:3` version `18.0.1.0.40 → 18.0.1.0.41`（觸發 migration），測試斷言用 `>=` 不硬編 `==`。

### dual-format（永久負擔）
- `canonical_name` immutable → 注入只對新 record 生效，DB 新舊命名**永久混格**。任何 prefix 過濾 / L4 reconciler 必須同時認「有 token」與「無 token」+「CS 主 route `paas-cs-*`」與「sidecar `svc-*-mcp`」多格式（`external-resource-env-token-naming.md:40-41`）。

### 歷史 dead corner（明文標註，不誤判為 bug）
- 歷史 id 命名 ns `paas-ws-1/5/253` 前綴與 prod 裸 `paas-ws-` 相撞，token/label 無法回頭救 → migration 註解與 follow-up 明文標「已知 dead corner、不在回填範圍」。
- operator `generate_subdomain`（`cloudflare.py:719`）硬編 `replace("paas-ws-","")`：若 L1 把 stg namespace_prefix 改成 `paas-ws-stg-`，會殘留 `stg-` 在 ns_suffix → CS/MCP 對外 URL 變 `release-stg-xxxx`。**審查已抓**：本計畫 L3 規避（只動 naming.py 單一根、不碰 generate_subdomain），但若 L1 動 namespace_prefix，須把 `cloudflare.py:719` + `helm.py:81` prefix-literal audit 納入 L1 lock-step，禁止字面 replace。

---

## 對抗式審查可重用 checklist（每階段做完強制跑）

每階段完成後，逐項過：

1. **red-first**：本階段所有新測試是否先驗會紅（不是寫完就綠）。
2. **su 豁免 lens（false-green）**：`git diff` 確認**零** `not env.su` / `not has_group()` 混入 guard；TransactionCase（su env）下 deny 分支**確實跑到**（`su-env-exempt-guard-false-green`）。L4 純 operator 列舉無 Odoo guard → 此 lens 標 N/A。
3. **label unit-mock 盲 lens（false-green）**：外部資源 value（label/CF name）合法性是否測在**純語言邊界**（`validate_label_value` pure-Python）+ 有 integration/live 真環境驗證（mock kubectl/CF 測不到 atomic reject / 真 list-all 契約，#208）。
4. **硬編快照 lens（false-green）**：斷言用 `startswith` / `in` / 重算片段 / `<=63` / regex（monotonic·存在性），**無**完整字串快照（`phase-versioned-test-assertion`）。
5. **create↔delete 對稱**：create 端命名改動，delete 端反查是否同步（特別查重算路徑；A 類走 stored 值天然對稱、B 類 `generate_subdomain` 重算須意識）。
6. **lock-step**：跨 repo 改動順序對（image↔Fleet↔addon）；未同步中間態風險已列；改 `extra/paas-operator/` 必 build+Fleet。
7. **fixture-coupling**：grep audit 清單零遺漏；該紅的紅（非靜默繞過）、該 lock-step 的補對（非刪除）。
8. **B1 leak lens**：若動到 `base.group_user` 可見 model / ir.rule，跑 ORM-vs-endpoint ACL 對等審查（controller 不可 `.sudo()` 繞 ir.rule、補 HttpCase cross-user negative，`orm-vs-endpoint-acl-test-coverage` 三變體 family）。本計畫 L1-L4 **皆不動 model ACL / ir.rule**（只動命名、config_parameter、k8s label、唯讀列舉）→ 此 lens 標 **N/A，無 B1 leak 面**。

---

## 給使用者的決策點

1. **env token 格式**：建議 `paas-{env}-{type}-`（即 stg = `paas-stg-sa-` / `paas-stg-cs-` / `paas-stg-sm-`，prod = `paas-sa-` 空 token）。是否採此格式？或偏好其他（如 type 在前 `paas-sa-stg-`、或不含 `paas-` 前綴）？

2. **CS / SM subdomain 是否注 token**（**最需拍板**）：CS（`paas.py:2005`）/ SM（`smart_home.py:127`）的 subdomain **= 對外 URL**。注 token 會讓 stg 新建資源對外 hostname 與舊資源不一致（且 immutable → 新舊混格）。
   - 選項 A（建議保守）：**僅 SA `canonical_name` 注 token**（內部標籤，不影響對外 URL），CS/SM 傳 `env='prod'` 不注。
   - 選項 B：CS/SM 也注（辨識性更完整），但須先確認 stg 既有對外發佈 URL 相容性。
   - **未拍板前，階段 1 change_sites 5/6 預設按選項 A（CS/SM 不注）實作。**

3. **deploy_env config 欄位型態**：建議 `fields.Selection([('prod',...),('stg',...)])`（UI 端限白名單，防 admin 誤填非法 DNS name）。是否同意？或要求保留 Char（自由輸入 + naming helper 清洗）？

4. **deploy_env 預設值**：Odoo 端建議 `default='prod'`（fail-safe：未設＝prod 不亂插 token）；operator 端 `default=''`（unknown 不 crash，fail-closed 下推消費點）。是否同意此**兩端不同預設**的語義分工？

5. **L4 介面**：首版採 **cron + /woow 後台 UI 報表**（唯讀對帳，只報告不刪）還是 **operator endpoint + Odoo client 手動觸發**？建議 cron+UI（自動定期對帳、人工照手冊刪除）。

6. **是否要做 L4**：L4 effort 最高（XL）且獨立於 L1-L3。若僅需「新資源可辨識」（L1-L3 已達成），L4「發現既有孤兒」可延後或單獨立項。是否本 epic 一併做 L4，或先 ship L1-L3、L4 列 follow-up？

7. **prod promote 時機**：prod/stg 共用 CF account + cluster。建議**所有 stg 端到端驗證（含 create→delete 對稱、ns label 可查）完成後才 promote prod**，且 promote 時一併重整 prod 已知 registry secret drift（`fleet-split-registry-values-merge-drift`）。是否同意此 prod 後置 + drift 順帶重整策略？

**關鍵檔案位置索引**（供執行 agent 定位）：`src/services/naming.py:18-29`、`src/models/security_access.py:202-253`（guard）/`:368`（tunnel name）/`:547`（DNS comment）/`:530,777,1105,1203`（subdomain_prefix 派生，不注 token）、`src/controllers/paas.py:2005`、`src/models/smart_home.py:127`、`src/hooks.py:123-131`、`src/models/res_config_settings.py:28`、`extra/paas-operator/src/config.py:19`、`extra/paas-operator/src/services/helm.py:93`(validate)/`:684`(create_namespace)、`extra/paas-operator/src/api/agent_runtimes.py:90-93`、`extra/paas-operator/src/services/cloudflare.py:719`(generate_subdomain)、`extra/paas-operator/src/api/releases.py:251,704`(mcp 命名)、`extra/paas-operator/tests/integration/README.md:75-95`(無 CF cred gap)、`src/__manifest__.py:3`(version 18.0.1.0.40→.41)。

---

## 決策記錄（2026-06-22 使用者拍板）

| 決策點 | 拍板 | 對計畫的影響 |
|---|---|---|
| **DP2 CS/SM 是否注 token** | **只 SA 內部命名注**（選項 A，保守） | 階段 1 change_sites 只接 SA（`security_access.py:207`）；CS（`paas.py:2005`）/ SM（`smart_home.py:127`）呼叫點傳 `env=''`（= prod 空 token），對外 URL **零變更**。`naming.py` 三個 helper 仍加 `env` 參數（為 L 彈性），但 CS/SM 不傳真值。原計畫的 `test_cs_subdomain_uses_deploy_env` / `test_smarthome_subdomain_uses_deploy_env` 改斷言為 **CS/SM 不帶 token**（鎖「不注」決策）。 |
| **DP1 token 格式** | **`paas-{env}-{type}-`** | stg = `paas-stg-sa-`、prod = `paas-sa-`（空 token）。`naming.py` env-helper：`env in ('','prod') → ''`，else → `f"{env}-"` 接在 `paas-` 後、`{type}-` 前。 |
| **DP6 L4 範圍** | **先 ship L1-L3、L4 列 follow-up** | 本 epic = 階段 0（L5 守線）+ 階段 1（L3）+ 階段 2（L1+L2）。**階段 3（L4）移出本 epic、列 follow-up**（單獨立項；孤兒「發現」延後，新資源「可辨識」先交付）。 |

**沿用建議預設（未另問）**：DP3 `deploy_env` 用 `fields.Selection`（限白名單防非法 DNS name）、DP4 兩端不同預設（Odoo `default='prod'` / operator `default=''` fail-closed）、DP7 prod 在所有 stg 驗證後才 promote 並順帶重整 registry secret drift。**DP5（L4 介面 cron+UI vs endpoint）隨 L4 follow-up 再定。**

## scout 校正（2026-06-22）

- **policy name 也帶 token**：對抗式審查把 Access app name 與 policy name 混為一談、誤把 policy name 一起否定。實機 `cloudflare.py:1322` `f"{canonical_name}: {p['name']}"`（canonical 經 `:594`/`:1200` 傳入）→ policy name 隨 canonical_name 帶 token。token-bearing 集合更正為 **tunnel name + DNS comment + policy name**；不帶 token 的是 DNS hostname + Access **app** name（走 subdomain_prefix）。已修正總覽、階段 1 審查註、階段 1 驗證點三處。

## Phase 0 fixture-coupling audit 結果（2026-06-22，唯讀）

grep 實證計畫的 fixture-coupling 分析**正確**：

- `test_naming.py:57/72/73/84/85/102/103/117`、`test_security_access.py:32/63/852`、`test_smart_home.py:55` 的 `startswith('paas-{type}-')` / split / len 斷言 —— **預設 prod 空 token 下 byte-identical、不變紅、不需改**。
- `test_security_access.py:111/178/211/243/876/892` 的 `'tunnel_name': 'paas-sa-x-y'` 與 operator `test_tunnel_routes_sync.py:107/111/231` 的 `comment=='paas-sa-x-y'` —— 皆 **mock 輸入 / forwarding 斷言**（非 naming 輸出），不受 token 影響。
- `test_security_access.py:330/497/600` 的 `'subdomain_prefix': sa.canonical_name` —— subdomain_prefix 走 user 路徑；預設 prod 下 canonical 無 token，不變。
- **結論**：預設 prod 下既有測試零變紅，blast radius 僅限新增的 stg 存在性測試。fixture-coupling lock-step 清單在「stg fixture 時」才放寬為存在性斷言、**絕不刪除繞過**。

## DP2 部分推翻：SmartHome 走 Option C（2026-06-25 拍板 + 已實作驗證）

**背景**：DP2（2026-06-22）把 CS+SM 綁在一起「subdomain 不注 token」。2026-06-25 重新討論時，讀實機程式碼發現這個綁定**不對稱**：

- **CS** 走 **shared tunnel**，DNS record 的 `content` 指向哪條 shared tunnel（stg `55953bfa` / prod `328c3a28`）**已經能分 env**；加上 ns prefix（`paas-ws-stg-`）+ L2 ns label → CS 早就 env-可辨識，CF 名注 token **冗餘**且白改對外 URL。**CS 維持 DP2 不注。**
- **SM** 走 **dedicated tunnel**，**無** shared-tunnel content 線索、**無** namespace（純 CF-to-home，L2 救不到）→ 在共用 CF account 上**完全無法自證 env**（正是 L4 plan「dedicated tunnel env_hint 只能標 unknown」那筆）。這是 problem-1 唯一真缺口。

**Option C（外科手術，只 SM、只注內部）**：把 SM 的 env token 注在 **內部 CF tunnel 顯示名**（`_generate_tunnel_name` → `paas-stg-sm-…`），**對外 subdomain / hostname / tunnel_route 維持乾淨**（`_generate_subdomain` 不變）。**完全比照 SA**（token 騎內部 `canonical_name`、不騎 route 對外 `subdomain_prefix`）。

**可行性 make-or-break（已驗）**：operator `tunnels.py:54-88` 的 `name` 只給 CF tunnel 顯示名（step 1），DNS subdomain 從 `request.hostname` 抽（`:79-82`）、**完全不碰 `name`** → tunnel 名與對外 hostname 可分離 → **純 Odoo 改動、零 operator 改動、無跨 repo lock-step、deploy-stg 可驗**。

**對稱安全**：SM teardown（`_cleanup_tunnel`/`unlink`）走 stored `tunnel_id`，**不重算 tunnel 名** → 改名零反查破壞（`external-resource-env-token-naming` 守則 3/4）。

**實作（commit 見 PR）**：`src/models/smart_home.py` 新增 `_naming_slug`（去重）+ `_generate_tunnel_name`（讀 `deploy_env` config）；`action_provision` 的 `create_tunnel(name=…)` 從 `subdomain` 改 `_generate_tunnel_name()`。`src/tests/test_smart_home.py` 加 4 測試（TDD red-first 驗過）：tunnel 名帶 token + subdomain 乾淨、prod 預設零變更、provision 傳 token 名乾淨 hostname、prod regression lock。**既有 DP2-lock 測試 `test_subdomain_has_no_env_token_even_when_deploy_env_stg` 續綠**（Option C 不動 subdomain）。全 suite **1326 綠**。

**Caveats**：① 既有 stg SM tunnel（改前建）名維持 `paas-sm-`，新建才 `paas-stg-sm-` → stg 永久混格（pattern doc「既有不回填」caveat；舊筆交給 L4 reconciler 認雙格式）。② **live stg 驗證仍 deferred**（unit mock 證不到真 CF；plan 階段 1 驗證點要求 deploy-stg 真查 CF tunnel 名帶 token + DNS hostname 乾淨）。

**更新後的 token-bearing 集合**：SA（tunnel name + DNS comment + policy name）+ **SM dedicated tunnel name**。對外 URL（SA route subdomain_prefix、CS subdomain、SM subdomain/hostname）一律不帶。