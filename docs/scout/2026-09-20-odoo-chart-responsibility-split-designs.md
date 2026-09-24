---
title: 官方 odoo image ＋ 自家 chart 的責任分配：三取向對抗設計與批判
category: scout
status: draft
created: 2026-09-20
updated: 2026-09-21
related:
  - docs/scout/2026-09-20-bitnami-chart-official-odoo-prototype.md
  - docs/scout/2026-09-18-odoo-bitnami-coupling-inventory.md
  - docs/scout/2026-09-18-harumi-odoo-paas-migration-plan.md
---

# 官方 odoo image ＋ 自家 chart 的責任分配：三取向對抗設計與批判

> Wayfinder map [#1134](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1134) 的設計偵察產出，是 [#1141「odoo chart 的共用設計」](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1141) 的輸入。
> 產出方式（2026-09-20）：以 [#1136 的實測](2026-09-20-bitnami-chart-official-odoo-prototype.md)為前提，三個**刻意對立**的取向各出一份責任分配設計 → 每份各自被一支對抗式批判打 → 最後一支完整度 critic 掃三份都漏的洞。共 7 個 agent；批判方被要求回到原始碼核對，所以多數指控帶 `檔案:行號`（行號以 2026-09-20 的 `origin/develop` 為準）。
> **本文件保存的是批判與 critic 的全文**——三份設計本身全部被判「不可照此執行」，全文另存為 asset：[`assets/1141/responsibility-split-designs.json`](assets/1141/responsibility-split-designs.json)。批判裡「站得住的部分」引用到設計細節時，到該檔查。

## 0. 怎麼讀這份文件

- **要知道結論** → §1（共同致命根源）與 §2（後續裁決的去向）。
- **要動手設計 chart／改平台管線** → §3 各取向的「致命傷」逐條看，每條都是一個會在 helm 綠燈下靜默壞掉的具體情境。
- **要排下一步實測** → §4 的「未經實測的假設」與「建議的下一顆 prototype」（後者已開成 [#1154](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1154)）。

⚠ 批判是 agent 產出。下列 §1 的四條已由人工抽驗屬實；其餘指控在據以動工前，請先自行核對它引用的行號。

## 1. 三份設計的共同致命根源

三份都被判不可執行，但**原因不是責任分層選錯**，而是三份都假設「`helm upgrade` 是一條平台每次都把完整意圖重送一遍的管線」——實際上平台有三條輸入是 **create-only**，任何責任分配放上去都會在既有服務上靜默 no-op：

| # | 事實（已抽驗） | 後果 |
|---|---|---|
| 1 | `_apply_plan_spec_overlay` 全 repo 只有一個非測試呼叫點：`src/controllers/paas.py:3419`（create） | 三份都宣稱「修好 C015（plan 變更對既有服務生效）」**全部不成立**；所有 `workers>0` 的連動守線永遠不會被觸發，因為 workers 停在建立當下的值 |
| 2 | reconfigure 的 `upgrade_kwargs`（`src/controllers/paas.py:3762`）只有六鍵 ＋ `reset_values=True`，**沒有 `sensitive_values`** | 憑證改走 Secret 的設計，租戶第一次按「儲存設定」就把站打掉 |
| 3 | `helm_default_values` 只在 create 讀；upgrade 的 base 是 `svc.helm_values`（create 當下的 merge 快照，`src/models/cloud_service.py:1578`） | 「改 seed 的 image tag 推給既有服務」在既有服務上是 no-op，而模組升級閘偏偏靠它當輸入 |
| 4 | `init_odoo` 的 client 逾時 `HELM_OPERATION_TIMEOUT = 120`（`src/services/paas_operator.py:18`）< operator 端 `_wait_for_ready` 的 `30 × 5 = 150s`（`extra/paas-operator/src/services/odoo_init.py:23`） | 與取向無關、換官方 image 後必炸。已開 [#1152](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1152) |

好消息：重算層**已經存在**——`_assemble_deploy_values`（`src/models/cloud_service.py:1585` 起）每次 deploy／upgrade 都重跑 5 個 injector。第 1 條的修法是把 plan overlay 搬進同一層，不是重構。

## 2. 後續裁決的去向

本文件**不記錄決策**，只記錄設計材料。決策在票上，這裡只給指針：

- 六個責任分配岔路（升級閘歸屬、憑證通道、master password、websocket 分流、是否共用 chart、addon 落點）的裁決與附帶條件 → [#1141 的留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1141)。
- chart 路線（自建、IMIO/helm-odoo 為骨架）→ [#1140 的 resolution](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1140)。
- prod 回退窗口 → [#1142 的留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1142)。

## 3. 各取向的設計摘要與對抗批判


### 取向 A：image 擔責

**設計的自我定位**：取向 A（image 擔責）：把 bootstrap、admin 建立、odoo.conf 組裝、模組升級閘、addon 選擇全部收進自家 `cloud-odoo` image 的 `woow-entrypoint`，等於用官方 lineage 重做一個「可驗證、會大聲失敗」的 Bitnami entrypoint；chart 退化成只產 K8s 物件＋把參數用 env／投影 Secret 檔餵進去，平台只填 values 不再知道容器內路徑。賭注是「entrypoint 邏輯與 odoo 版本同生共死 ⇒ 放在同一個 build artifact 裡最不會漂」，而且只有 image 內部知道這顆 odoo 真正吃哪些 `odoo.conf` 選項、`/all-addons` 裡真的有哪些 addon，所以「寫錯鍵不報錯」這一類風險（C004／C005／C018／C066／S1）可以在 boot 當下就被打成非零離開碼；代價是每改一行 entrypoint 都要吃 `paas-odoo-ci` 的人工 `/approve` build gate，回饋迴圈以天計。

**人日自估**：**合計約 22～32 人日**（單人，含 agent 輔助；**不含**人工 `/approve` 與 review 的等待時間，不含 paas-odoo-self 另案）。比盤點 §1.2 的 27～47（中位數 35）略低，原因是取向 A 讓一批條目「消失而不是重寫」：C001／C002／C003／C004／C018／C025 都是整段刪除，C008／C047 因為 bootstrap 移到 initContainer 而自動成立。

拆解：

| 項目 | 人日 | 內容 |
|---|---|---|
| image：`woow-entrypoint` 本體 | 4～6 | conf render＋選項名驗證（`/etc/woow/odoo-options.json`）、env 契約驗證、addon symlink、bootstrap（advisory lock／`to_regclass`／ORM 設 admin）、module upgrade gate、SMTP ssl 的 `ir.mail_server` upsert、filestore／websocket 自檢 |
| image：Dockerfile.cloud 改寫＋CI | 1～2 | 換 base（釘 digest）、pip 改系統（PEP 668）、manifest 產生、目錄權限、`selfcheck` smoke |
| image：entrypoint 測試套件 | 1～2 | pytest／bats；gate 前能抓多少算多少（這筆是取向 A 的必要保費，不可省） |
| chart：odoo-tenant（薄） | 4～6 | Deployment／Service／PVC／獨立 postgres／nginx sidecar／NetworkPolicy／`values.schema.json`（全樹 `additionalProperties:false`＋條件約束）／render 測試 |
| 平台：seed＋values 組裝＋憑證鏈 | 4～6 | seed 全段重寫、addon builder 塌縮、`_inject_storage_class`、`_build_expose_config` 明帶、plan overlay 改寫、`has_admin_gui=True` 接上 #357、`odoo_tenant_image_tag` config parameter（含 `_CONFIG_PARAMETERS` 登記與測試） |
| 平台：同 PR 必改的測試 | 1.5～2 | C019／C020／C021／C033／C034／C035／C036 ＋ schema 快照契約測試 ＋ sensitive 全量掃描 |
| operator：路徑／selector／ODOO_RC／expose 驗證／chart 名守門 ＋ 測試 | 2～3 | C049～C053、C056～C058、S1 第二道、in-place upgrade 守門；integration test 沒有 CI runner，要本機跑真 K8s |
| dev sandbox 端到端＋stg 既存實例處置＋升級／降版演練 | 3～5 | 首次 init 耗時量測（對 helm timeout／startupProbe／平台等待）、`-u all` 時長、svc-42487178 銷毀重建、伴生 MCP 重新設定 |
| docs（odoo-service-config／兩份 ADR／gotcha）＋ `gen-docs-index.py` | 1～2 | C088～C092、C110、C111、C112 |

**風險加碼（不在上表）**
- `/approve` gate 的等待：若 entrypoint 需要 3 輪 prod build，日曆時間加 3～10 天，人日不變但排程要留。
- 若 `odoo-tenant-1.0.0` 之後拿得到而要對帳／合併：＋2～4 人日。
- 若 stg 的 svc-42487178 要做資料遷移而非銷毀重建：＋2～4 人日。
- 若同一套 entrypoint 要同時吞下平台 Odoo（paas-odoo，Fleet／bundle-cd 那一整套 initContainer 與兩段式 promote）：那是另案的 20～35 人日，但**設計階段就要把契約留好**，否則會做成兩份 entrypoint。

**責任歸屬一覽**（做法全文見 asset）：

| 機制 | 歸屬 |
|---|---|
| ① customPostInitScripts（fix-admin-credentials.sh／odoo-conf-system.sh）：官方 entrypoint 沒有 init hook，靜默不執行 | image（主）＋ chart（只餵 env） |
| ② 所有 ODOO_* env 無人消費（ODOO_EMAIL／ODOO_PASSWORD／ODOO_DATABASE_*／ODOO_SKIP_MODULES_UPDATE） | image（契約定義與驗證）＋ chart（values.schema.json 擋錯路徑） |
| ③ 自動建 DB 與 admin（odooSkipInstall=false）不存在；官方 `-i base` 後 admin 固定 admin/admin | image（bootstrap 子命令）＋ chart（宣告一個跑同一顆 image 的 initContainer）＋ platform（憑證供給） |
| ④ 非首次啟動自動 `--update=all` 的升級語意消失（image 升版後 code 與 DB schema 脫節） | image（module upgrade gate，仍在 bootstrap 子命令內） |
| ⑤ addon 注入：`/opt/bitnami/odoo/selected-addons`＋`/all-addons/`＋`/bitnami/odoo` PVC 慣例＋customPostInitScripts sed | image（複製與 addons_path 全包）＋ platform（只填一個清單鍵） |
| ⑥ SMTP：chart 的 smtp* → ODOO_SMTP_* env → entrypoint 寫 odoo.conf，全失效 | image（conf render ＋ ssl 的 ir.mail_server 特例） |
| 形狀差異 A：filestore 路徑 `/bitnami/odoo` → `/var/lib/odoo` | image（宣告並自檢）＋ chart（掛 PVC） |
| 形狀差異 B：workers>0 時 `/websocket` 必須走 8072（8069 回 500） | image（偵測＋拒絕開機）＋ chart（nginx sidecar 或多埠 Service） |
| 形狀差異 C：PVC 的 `app.kubernetes.io/instance` label（孤兒 PVC 延遲清理靠它） | chart（唯一可能） |
| 形狀差異 D：expose 的 service/port 自動偵測會挑錯（Bitnami 只是剛好排對） | platform（明帶）＋ chart（命名契約）＋ operator（驗在不在） |
| 形狀差異 E：odoo.conf 在 image 內 `/etc/odoo/odoo.conf`、uid 101、operator 三支 exec 腳本全指 Bitnami 路徑 | image（導出路徑）＋ operator（改讀 env） |
| 橫切：憑證不得明文落 helm values／Deployment env（租戶看得到 helm_values） | platform（供給通道）＋ chart（Secret→投影檔）＋ image（只讀檔） |

#### 批判的總判決

方向大致正確（bootstrap 進 initContainer、addon 用全量清單、schema 當機器守線、版號比對當冪等判準都站得住），但**以目前這個形狀落地，第一天就會把租戶站打掉**：設計把憑證從 `helm_values` 搬進 `config.sensitive.*`，卻沒看 reconfigure 這條最高頻路徑根本不送 `sensitive_values`（`src/controllers/paas.py:3762`），再加上自己新增的 fail-loud（exit 78），等於把「租戶按一次儲存設定」變成「站台下線」。另一條同級的是 `--atomic` + `--timeout 15s`（`extra/paas-operator/src/api/releases.py:49,942,1126`）對上會跑 `-u` 的 initContainer：rollback 會發生在 schema migration 半途，正好落進設計自己的「DB 比 image 新就拒絕開機」，變成永久磚化。

fail-loud 的方向是對的，但這份設計把「開機失敗」當成免費的 fail-loud——在一個**已經在線、正在營運**的租戶站上，exit 78 不是 fail-loud，是 outage。必須把失敗分級：**部署期可以硬失敗（首裝、schema、contract skew），運行期只能降級 + 回報（addon 不見、SMTP 探測、websocket 路由）**。這一條沒補之前不應開工。

另外「同一套 chart 同時服務租戶 odoo 與平台 Odoo」與「全樹 `additionalProperties:false`、chart 裡不得有任何 shell」在 inventory §5 的實況下（C078/C082/C083/C084/C098/C100/C105–C109）不可能同時成立，這是動工前必須先裁決的岔路，不是 open question。

人日估算（22～32）不可信：它低於 inventory 的 27～47，卻同時新增了一支要長期維護的 entrypoint 程式、一份從零的 chart、一套 contract 協定，而且 image 的回饋迴圈以天計、operator 的 odoo exec 路徑今天零 integration test。

#### 致命傷與嚴重缺陷

**1. 🔴 fatal**

- **設計的主張**：橫切『憑證走 config.sensitive.* → chart render 成 Secret → projected volume 0400』＋ image 端『憑證檔不存在或為空 → exit 78，絕不 fallback』，而平台端只要沿用既有 #357 供給鏈即可
- **為什麼會壞**：#357 供給鏈只覆蓋三條路徑：install（src/models/cloud_service.py:1384→:1463）、action_redeploy（:946→:954）、reset-admin-password（:1128→:1161）。**租戶最高頻的 reconfigure 路徑沒有**：src/controllers/paas.py:3762 的 `upgrade_kwargs` 只有 namespace/release_name/chart/values/version/`reset_values=True`，沒有 `sensitive_values`；而 helm.py:841 的 `--reset-values` 會丟掉上一版 stored values。今天無害只因為 odoo 的 `has_admin_gui=False`、憑證住在 `helm_values` 裡跟著 `_assemble_deploy_values` 一起送；設計把 `has_admin_gui=True` 並把 admin/smtp/db 密碼搬進只走 sensitive 通道的 `config.sensitive.*`，這條路就變成「每次 reconfigure 都把憑證從 Secret 抹掉」。更糟的是 `_build_mcp_sensitive_values`（:2175）在 secret record 缺席時回 `{}`，它的 docstring 明寫這個回傳值安全的前提是『chart 的 Secret 與 config.json 是 write-only-if-absent、chart 對 admin_password 無 self-generate』——那是 mcp-server chart 的性質，新 odoo chart 必須自己重建同款不變式，設計完全沒提。
- **具體情境**：租戶在 ConfigurationTab 改 SMTP port 後按「儲存設定」（或 AddonsTab 勾一顆 addon、EditDomainModal 改網域）→ paas.py:3780 背景 `helm upgrade --reset-values` 且無 sensitive_values → chart 重新 render 憑證 Secret，`admin_password`/`smtp_password`/`db_password` 全變空 → 新 pod 的 bootstrap 讀到空的 /run/woow/credentials/admin_password → 依設計 exit 78 → Init:Error → 租戶正在營運的 Odoo 下線，而 helm 回報 upgrade 成功、平台 UI 停在 upgrading 直到 age timeout 翻 error。若 postgres 密碼也在同一棵子樹，PGDATA 早已用舊密碼初始化 ⇒ 連 rollback 都救不回來。

**2. 🔴 fatal**

- **設計的主張**：『bootstrap 任一步非零 → initContainer 失敗 → pod 永不 Ready → 平台停在 deploying』『冪等來源是版號比對 ⇒ 重啟、reset_values upgrade、rollback 再 roll forward 都安全』
- **為什麼會壞**：operator 的 reset-config 路徑是 `atomic=True` + `timeout_seconds=RESET_HELM_TIMEOUT_SECONDS`＝**15 秒**（extra/paas-operator/src/api/releases.py:49,942,1126）。`--atomic` 蘊含 `--wait`：pod template 一變（新 Secret／新 image tag），helm 會等新 pod Ready，等不到就**自動 rollback 到上一版**。新設計的 initContainer 至少要跑 wait-for-psql（官方腳本寫死 `--timeout=30`）＋`to_regclass`，核心升版時還要跑 `odoo -u all`（inventory §6 第 9 點明記此耗時未量測）。也就是說 reset 路徑在新 chart 上**必然**超時；而 rollback 發生時 DB 的 schema 可能已經被 `-u` 改到一半或改完，rollback 把 image 換回舊 digest ⇒ 正好命中設計自己的『DB 的 core release 比 image 新 → exit 78 拒絕開機』。平台端 cloud_service.py:1128 的註解已經預告過這個形狀（『reset 夾帶 chart 升版時…可能撞上 --timeout 15s』『重按不會收斂』），設計沒有把它算進去。
- **具體情境**：租戶按「重設管理員密碼」（設計把 has_admin_gui 打開才有這顆按鈕）→ operator `helm upgrade --atomic --timeout 15s` → 新 pod 的 bootstrap 正在跑 `-u all` → 15s 到、helm rollback → 舊 image 的 pod 起來、bootstrap 讀到 `woow.image.release` 比自己新 → exit 78 → CrashLoopBackOff 永不恢復；此時唯一的復原路徑是手動 helm rollback/forward + 人工進 DB 改 config parameter，而平台 UI 只會顯示 error。

**3. 🔴 fatal**

- **設計的主張**：『image 回退＝把 config parameter 改回舊 digest，不需要重 build、不吃 /approve gate』＋『contract 只往上加，image 可同時支援 N 與 N-1（SUPPORTED_CONTRACTS={2,3}），所以 image 先行不會打壞線上的舊 chart』
- **為什麼會壞**：三處自相矛盾且各自有洞：(a) 設計同時宣告 chart 的 `values.schema.json` 是 `contract: {const: 2}`、image 開機『比對兩者，不等 ⇒ exit 78』、以及 image 支援集合 {2,3}——`const` 加嚴格相等就不可能有集合語意，錯誤訊息『chart contract 2 ≠ image contract 3』寫死的也是相等判斷。一旦 chart 升到 contract 3，任何回退到只支援 2 的 image 都會 CrashLoop，所謂『gate-free rollback』就消失了。(b) 回退與『DB 比 image 新 ⇒ exit 78』互斥：只要曾成功開機一次並記帳，跨 core release 的回退就是永久拒絕開機。(c) **記帳對非本設計建立的 DB 沒有定義**：從 Bitnami 遷移過來、從 stg clone、或從備份還原的 DB 都沒有 `woow.image.release`/`woow.bootstrap.version`，設計沒說這時該做什麼——要嘛當成『沒變、跳過』（code 與 schema 脫節，正是機制 ④ 存在的理由），要嘛當成『首次』而對一顆有真實資料的 DB 無預警跑 `-u all`。
- **具體情境**：新 chart 已發到 contract 3、image 也升到 3 並在 prod 跑了兩週；發現 entrypoint 有 bug 要回退 image → 改 config parameter → 所有租戶 pod 起來後比對 chart contract 3 vs image contract 2 → 全機隊 CrashLoop，而修法是再走一次天級的 /approve build。另一個版本：維運用 seed-from-stg 把 stg DB clone 進 dev sandbox 驗證，bootstrap 看不到記帳、判成首次 → 直接在 clone 上跑 `-i base`/`-u all`，量測結果失真而沒人察覺。

**4. 🟠 serious**

- **設計的主張**：『同一套設計同時服務租戶 odoo cloud_service 與平台 Odoo』，差別只是保留 `WOOW_UPGRADE_ON_IMAGE_CHANGE=0` 一個旗標；chart『template 裡不得出現任何 shell script』、`values.schema.json` 全樹 `additionalProperties:false`
- **為什麼會壞**：inventory §5 列出平台 Odoo 現況靠的是：`customPostInitScripts` 一次性 hook（C078）、三支掛在 /bitnami/odoo 的 initContainer（C098）、patch-odoo-conf 依賴 `is_app_initialized`（C082）、filestore-hashdir-guard（C083）、module-version-gate 連 CONF/PY/MANIFEST 與首次部署判斷（C084）、`extraContainerPorts`（C105）、`service.extraPorts`（C106）、頂層 `networkPolicy:`（C100）、ingress `extraPaths`/`extraRules`（C107）、扁平 probe 鍵（C108）、netpol 埠（C109）、以及 `ODOO_SKIP_MODULES_UPDATE` 的兩段式 promote SOP（C097）。一個拒絕 `extraVolumeMounts`/`initContainers`/`extraEnv` 的 schema **無法表達**這些；要表達就得開逃生門，而逃生門一開，『additionalProperties:false 讓寫錯鍵當場失敗』這個整份設計的最大賣點就同時失效（Bitnami 殘鍵也會再度被接受）。設計把這件事放在 open_questions 第 3 條，但它其實是動工前的岔路：選錯會做成兩份 entrypoint＋兩份 chart，或做成一份什麼都擋不住的 chart。
- **具體情境**：chart 依設計做成薄 chart 並上線租戶；三個月後要把平台 Odoo 搬過來，發現 Fleet 的 values-prod.yaml 有 11 類鍵無處可放 → 為平台開 `extraInitContainers`/`extraEnv`/`extraVolumes` 三個自由欄位 → 租戶側的 schema 同時鬆綁 → 某次 seed 改動把 `customPostInitScripts` 貼回 values，helm 綠燈、pod Ready、script 靜默不執行（正是 #1136 §3 的第一列），回到原點。

**5. 🟠 serious**

- **設計的主張**：『`_model_init_odoo` 不改語意（bootstrap 已在 initContainer 完成，Ready 即 DB 已 init ⇒ C008／C047 自動成立）』
- **為什麼會壞**：post-deploy init 的實體是 operator exec 進 pod 跑 update_module_list.py，它的**第 1 步是解析 odoo.conf 取得 db_name**（extra/paas-operator/src/services/odoo_init.py:237 docstring、update_module_list.py 檔頭流程 1），第 4 步的 stale cleanup 還依賴 conf 裡被 append 過的 addons_path（該檔檔頭 #1052 註解明寫『改任一邊的路徑先看另一邊』）。但設計的 chart `odoo.config` 鍵表只有 proxy_mode/list_db/db_filter/workers/max_cron_threads/limit_*，**沒有 db_name**——DB 名只走 `WOOW_DB_NAME` env。只要 render 出來的 /run/woow/odoo.conf 少了 `db_name`（或 addons_path 的語意與 image 內的 symlink 佈局對不上），這支腳本就拿不到 DB 名而失敗。
- **具體情境**：首裝一切順利：helm 綠燈、initContainer 完成、pod Ready、租戶能登入。接著 cron 跑 post-deploy init → update_module_list 讀不到 db_name → `_record_init_failure` 連五次（src/models/cloud_service.py:3611 起、src/controllers/paas.py:4321）→ 服務被標成 `state=error`、UI 顯示故障，但站台其實活得好好的；租戶看到紅字、客服進來查，這是最典型的『helm 綠燈、pod Ready、行為錯』。

**6. 🟠 serious**

- **設計的主張**：『workers>0 而 WOOW_WEBSOCKET_ROUTED 不為 1 → exit 78』＋ chart schema `allOf`：`odoo.config.workers > 0` ⇒ `websocket.routed === true`；『每次開機重 render 還順手修掉 C015 的 plan 變更不生效』
- **為什麼會壞**：(a) 這個雙向約束在平台端**沒有 writer**：`_apply_plan_spec_overlay` 只有一個呼叫點，`src/controllers/paas.py:3419`（建立服務時），而且它只疊 resources/persistence/workers，不會去寫 `websocket.routed`。付費 plan（cs_odoo_workers>0）建立時 values 只有 workers>0 而 routed 缺席 ⇒ helm schema 直接擋掉 install。(b) 若把 `routed` 預設成 true 來繞過，則 workers=0 的 free plan 會把 /websocket 導到沒有 listener 的 8072，回到『UI 一切正常、只有即時通知靜默死掉』——設計自己點名要消滅的壞法。(c) 『開機重 render 修掉 C015』是錯的：env 的值來自 helm values，而 values 裡的 workers 只在**建立當下**被 overlay 寫入一次，plan 升級不會重跑 overlay（C015 的真正成因），重 render 只是把同一個舊值再寫一次。
- **具體情境**：租戶買 2-worker 的方案建站 → create 走 overlay 寫入 odoo.config.workers=2 → helm install 被 values.schema.json 的 allOf 擋下 → 付費方案完全無法開通（loud，但等於上線當天下架付費 plan）。若為了解這個問題把 routed 預設 true → 免費方案的租戶開站後 Discuss/即時通知永遠不會動，而且沒有任何錯誤訊號。

**7. 🟠 serious**

- **設計的主張**：機制 ⑤ fail_loud (b)：『DB 裡 state in (installed, to upgrade) 且屬自管 catalog、但磁碟上不存在的模組 → exit 78 列名』
- **為什麼會壞**：這把一個既有的、被刻意設計成**降級**的狀況升級成停機，而且觸發者是 image 端的變動、修復途徑卻要吃 /approve gate。update_module_list.py 的檔頭已經寫明這個場景的既有處置是『清掉 stale record，因為 update_list() 不會刪已從檔案系統消失的 addon』，並且明說『空 catalog 幾乎一定是產生失敗，不是真的沒有 addon』——也就是說 catalog／磁碟不一致是**可預期**的營運狀態，不是不變式被破壞。設計把它當成必須拒絕開機的錯誤，等於讓 `Dockerfile.cloud` 的一次 addon 下架／改名／catalog 產生失敗，變成所有裝過那顆 addon 的租戶在下一次 pod 重排（node drain、evict、plan 變更）時集體 CrashLoop。
- **具體情境**：image 新版把某顆冷門 addon 從 /all-addons 移除（或 generate-catalog 在新 base image 上偵測條件失效、產出空 catalog，C072/C073 已標 breaks）→ 平台換 image tag → 裝過該 addon 的 N 個租戶在重啟瞬間全部 Init:Error 下線 → 修復需要改 image → 人工 /approve → build → 推 registry → 換 tag → redeploy，以天計；期間沒有任何 chart／平台層的止血手段。

**8. 🟠 serious**

- **設計的主張**：『ODOOCONF_* 的選項名會被拿去比對 image build 時烤進 /etc/woow/odoo-options.json 的真實選項集，打錯一個字就 exit 78』——以此宣稱 C004／C005／C018／C066／S1 這一類風險『可以在 boot 當下被打成非零離開碼』
- **為什麼會壞**：『名字是不是 Odoo 的合法 option』不等於『這個 option 可以被外面設定』。odoo/tools/config.py 的 options dict 同時含 `db_host`／`db_user`／`db_password`／`data_dir`／`addons_path`／`admin_passwd`／`server_wide_modules`／`dev_mode`。而上游 entrypoint 的 `check_config()` 只在 conf **沒有**該鍵時才補 CLI 參數 ⇒ 任何能進到 render 的 `ODOOCONF_db_host` 會完全取代 chart 給的 DB 端點。也就是說 image 端唯一的門是『名字存在』，真正的授權邊界仍在 chart schema 白名單——與『image 擔責、只有 image 知道真相、所以把驗證放 image』的整個立論相反（image 這一層擋不住任何 escalation，只擋 typo）。更直接的自相矛盾：機制 ① 把 `admin_passwd`／`addons_path`／`data_dir` 明列進 ODOOCONF_ env 清單，橫切那條卻說憑證只准走投影檔、偵測到明文 env 就 exit 78（而它列的偵測對象只有 PASSWORD／WOOW_ADMIN_PASSWORD／ODOOCONF_smtp_password，漏了 admin_passwd）。
- **具體情境**：未來某個 PR 為了讓租戶自訂 db_filter，把 `odoo.config` 整個子樹加進 helm_value_specs 的 allowed keys（#559 的 ACL 是逐鍵白名單，加一個群組很自然）→ 租戶送 `odoo.config.db_host` 指向同 namespace 的另一顆 postgres 或自己的 DB → chart schema 若同步放寬即通過、image 驗證通過（是合法 option）→ 這台 Odoo 靜默接到別的 DB 上。

**9. 🟡 minor**

- **設計的主張**：『一個 env 對應一個 odoo.conf 選項名』，並逐鍵列出 proxy_mode／list_db／db_filter／workers／…／gevent_port／addons_path／data_dir
- **為什麼會壞**：`db_filter` **不是** Odoo 的 option 名。odoo/tools/config.py:164 是 `group.add_option("--db-filter", dest="dbfilter", ...)`，官方 image 隨附的 odoo.conf 範例寫的也是 `; dbfilter = .*`。依設計自己的規則，chart 宣告的 `odoo.config.db_filter` 會 render 成 `ODOOCONF_db_filter`，被 image 的 options 比對判成非法 → exit 78。這不只是筆誤：它證明『鍵名＝option 名、不做翻譯』這個契約把 Odoo 不規則且會變動的命名（`dbfilter`、`gevent_port`（舊名 longpolling_port）、config.py:695 的 `outdated_options_map` 把 `xmlrpc_port` 靜默改寫成 `http_port`）直接曝露成租戶面的 values 鍵與 #559 的 ACL 鍵——上游一次改名就是一次租戶面契約變更。
- **具體情境**：chart 依設計發版，第一次在 dev sandbox 裝就 CrashLoop（還算好的）；更壞的版本是上游某次把某個 option 併進 outdated_options_map，image 的 options 集合仍收得到新名、chart 還在送舊名，於是某個 plan 的 limit/filter 設定在升 image 後靜默變成預設值。

**10. 🟠 serious**

- **設計的主張**：『任何 ^(ODOO_|WOOW_|ODOOCONF_) 但不在已知集合的 env 一律 exit 78——舊 Bitnami 值被誰複製回來都會當場炸掉』
- **為什麼會壞**：Kubernetes 預設 `enableServiceLinks: true`，kubelet 會把同 namespace 每個 Service 以 docker-link 形式注入 env：一個叫 `odoo` 的 Service 就會產生 `ODOO_PORT`、`ODOO_SERVICE_HOST`、`ODOO_SERVICE_PORT`、`ODOO_PORT_80_TCP_ADDR`…。租戶 namespace 裡不是只有這個 chart：inventory §3.2 記載同 namespace 還住著兩個伴生 MCP 服務（svc-7dd7970c odoo-manage-mcp、svc-fbc97612 odoo-mcp），Service 命名不完全由這份 chart 決定；平台 Odoo 那側的 Service 名更是 `paas-odoo` 系列。設計的 chart values 清單裡沒有 `enableServiceLinks: false`，也沒有把檢查限縮成『image 自己定義的前綴且非 K8s 注入形狀』。
- **具體情境**：任何人在該 namespace 建一個名為 odoo 的 Service（或未來 chart 把主 Service 改名為含 odoo 的短名）→ 所有 pod 拿到 ODOO_PORT=tcp://10.43.x.x:80 → entrypoint 判為未知 ODOO_* → exit 78 → 整個 namespace 的 odoo pod CrashLoop，錯誤訊息指向一個沒有人設定過的 env。

**11. 🟡 minor**

- **設計的主張**：chart `values.schema.json`『全樹 additionalProperties: false（頂層與每個物件層）』，頂層鍵列舉為 contract/image/imagePullSecrets/odoo/websocket/persistence/postgres/service/resources/nodeSelector/tolerations/affinity/networkPolicy/podAnnotations/config.sensitive
- **為什麼會壞**：這份列舉**沒有 `global`**，但平台 `_inject_image_pull_secrets`（src/models/cloud_service.py:1847-1864）無條件 `values.setdefault('global', {})['imagePullSecrets']`，operator `_ensure_image_pull_secrets`（releases.py:128 起）另外 setdefault 頂層 `imagePullSecrets`，而 helm 的 schema 驗證吃的是 coalesce 後的完整 values。inventory §3.1 第 5 點已經明寫『頂層 additionalProperties: false，**並宣告 global**』——設計在 chart_shape 的敘述裡提到要吃 global.imagePullSecrets，卻沒把它放進被驗證的鍵表。另外兩處注入的形狀也要一起對：operator 注入的是頂層 list，平台注入的是 global 底下的 list，兩者的 item 型別（string vs {name}）必須同時被 schema 接受。
- **具體情境**：chart 發版、seed 切換、第一次 `helm install` 直接被 schema 擋下：`Additional property global is not allowed`，而且是在背景執行緒或 operator 裡失敗，平台 UI 只看得到一句 upgrade failed。屬於會在 dev sandbox 第一天撞到、但足以證明『鍵表是照設計稿抄的、沒有照實際注入點對過』。

**12. 🟠 serious**

- **設計的主張**：『serve 每次開機把 odoo.conf 重新 render 到 tmpfs /run/woow/odoo.conf 並 export ODOO_RC』『bootstrap 與 serve 是同一顆 image 的兩個子命令』
- **為什麼會壞**：initContainer 與主容器是兩個 container：`/run/woow` 必須是 **pod 級的 emptyDir（medium: Memory）並且兩個 container 都掛**，否則 bootstrap render 的 conf 與 serve 用的不是同一份；憑證 projected volume、`/var/lib/odoo` PVC、`/mnt/extra-addons/selected` emptyDir 也都必須在 initContainer 重新宣告一次（K8s 不會繼承）。設計只在『形狀差異 A』把 filestore 的 st_dev 自檢放進 `serve`，bootstrap 完全沒有這道檢查——而 `-i base` 正是在 bootstrap 裡跑的，那一步會建立 filestore。更隱蔽的是 Dockerfile `ENV ODOO_RC=/run/woow/odoo.conf` 指向一個**還沒 render 就不存在**的檔：官方 entrypoint 的 `check_config()` 對不存在的檔做 `grep -q -E ... "$ODOO_RC"`，在 `set -e` 下直接 exit 2 且不印任何訊息；operator 的 exec 腳本（改讀 `os.environ['ODOO_RC']` 後）也會在任何非 serve 的 container（如 debug exec 進 initContainer）拿到不存在的路徑。
- **具體情境**：chart 的 initContainer 漏掛 PVC（最常見的 K8s 疏漏，因為主容器有掛、看起來就對）→ bootstrap 的 `-i base` 把 filestore 寫進 ephemeral 層 → initContainer 結束、資料蒸發 → 主容器起來後 filestore 是空的，DB 裡 ir_attachment 指向不存在的檔 → 租戶首次上傳/列印前都看不出來，之後才發現附件全壞；設計的 st_dev 自檢在 serve 跑，而 serve 看到的 /var/lib/odoo 確實是 PVC，檢查通過。

**13. 🟡 minor**

- **設計的主張**：bootstrap 流程『pg_advisory_lock(hashtext(...)) → wait-for-psql.py → SELECT to_regclass('public.ir_module_module') → 為 null 才 odoo -d $DB -i base』
- **為什麼會壞**：前兩步都預設『目標 DB 已存在且可連線』，但誰建這顆 DB 沒有定義：官方 `wait-for-psql.py` 連的是 `dbname='postgres'`（只證明 server 活著，不證明租戶 DB 存在），而 `to_regclass` 與 advisory lock 都要連上租戶 DB。DB 真正的建立者是 `odoo -d <db>` 自己（odoo/cli/server.py:153 `_create_empty_database`），也就是流程的**第 4 步**——順序倒置。內建 postgres 時因為 `POSTGRES_DB` 剛好先建好而看不出來，一旦走 `odoo.database.host` 指外部 DB（設計自己在 chart 鍵表裡留了這個入口，平台 Odoo 更必然走它）就會在第 3 步就 OperationalError。附帶：`_create_empty_database` 在權限不足時只 log INFO 就往下走（server.py:155-160，`InsufficientPrivilege`），所以『沒有 CREATEDB 權限』這個情況不會以建 DB 失敗的形式報出來。advisory lock 也必須下在一顆保證存在的 DB（postgres）上，否則鎖與被保護的動作不在同一個生命週期。
- **具體情境**：平台 Odoo 或任何用外部／共用 postgres 的實例首次部署 → bootstrap 在 to_regclass 那步就 `FATAL: database "woow_x" does not exist` → initContainer 失敗 → 看起來像連線設定錯，實際是流程順序錯；除錯者會去查 Secret、NetworkPolicy、DNS，而不是去看誰該建 DB。

**14. 🟡 minor**

- **設計的主張**：『設完密碼立刻用 res.users._crypt_context().verify() 自驗，不過 → exit 1』
- **為什麼會壞**：`res.users.password` 在 Odoo 18 是 `compute='_compute_password', inverse='_set_password'` 的非儲存欄位（odoo/addons/base/models/res_users.py:373-375），ORM 讀回來是空字串；真正的 hash 由 `_set_encrypted_password` 用 raw SQL 寫進 res_users.password 欄位（:439-451）。所以 `verify(plaintext, user.password)` 會拿到空字串、永遠驗不過（或永遠被跳過，取決於怎麼寫）。要自驗只能走 raw SQL 取 hash，或呼叫 `_check_credentials(credential, env)`（:453，注意 18 的簽名是 credential dict，不是舊的 (password, env)）。設計其餘部分（『ORM 自動雜湊，不需要 passlib／psycopg2 手刻 SQL』）是對的——錯的只有自驗那一步，但那一步正是設計用來取代 fix-admin-credentials.sh 靜默放過的關鍵防線。
- **具體情境**：entrypoint 照設計寫成 `ctx.verify(pw, user.password)` → 每次都 False → 每次 bootstrap 都 exit 1，或反過來被寫成 try/except 吞掉而永遠不檢查；前者是所有首裝都失敗，後者是這道防線根本不存在而沒人知道。

#### 設計裡沒有證據支撐的宣稱

- 『22～32 人日、比盤點的 27～47 略低』——同時新增一支要長期維護的 entrypoint 程式（含 conf render／契約驗證／bootstrap／upgrade gate／SMTP reconcile／三道自檢）、一份從零的 chart、一套跨 repo contract 協定，卻估得比 inventory §1.2 更低；而 inventory §1.3 第 5 點已明記回饋迴圈長（PR CI 不跑 Odoo 測試、operator odoo exec 路徑零 integration test、image build 卡人工 gate）。估算沒有把『entrypoint 前三次一定寫錯』的天級迴圈與既存實例處置算進去。
- 『首次 init 耗時』『-u all 在大租戶 DB 上的時長』完全沒有量測值（inventory §6 第 9 點自承未量測），但設計同時依賴它來決定 startupProbe、helm --timeout、平台部署等待與 `_check_transition_timeout` 的門檻。#1136 只量到空 DB 的『install→Ready 約 2 分鐘、registry 載入 60s』，不能外推到有資料的 DB。
- 『nginx sidecar：/websocket → 8072、其餘 → 8069，Service 單一 port 80 指向 sidecar』從未實作或實測。#1136 只驗到直連 8072 的 handshake 是 101、8069 是 500；經過 sidecar 之後還要處理 Upgrade/Connection header、proxy_read_timeout、X-Forwarded-* 與 proxy_mode 的互動（proxy_mode=True 下 header 錯會讓 url_root／郵件連結變錯，且是靜默的）。
- 『chart 把 config.sensitive.* render 成 Secret』的**跨 upgrade 冪等性**未設計：chart 端若有任何 randAlphaNum 自產預設，配上平台所有路徑都用的 `reset_values=True`，每次 upgrade 都會重生密碼；若改用 lookup／write-only-if-absent，就要像 mcp-server chart 一樣把不變式寫下來並測。設計只說『operator 在記憶體 merge』，沒說 Secret 在沒有 sensitive_values 時 render 成什麼。
- 『operator 新增：既存 release 的 chart 名稱與請求不一致 ⇒ 拒絕 in-place upgrade』只描述了拒絕，沒描述**拒絕之後租戶的服務會變成什麼**。seed 是 noupdate="0"，`-u` 當下所有既存 odoo 服務的 template 就指向新 chart，於是它們的下一次 reconfigure／plan 變更／redeploy 全部被守門擋下 ⇒ 服務進入『活著但任何設定都改不了』的狀態，設計沒有給出這段期間的 UI 訊息與遷移動作。stg 的 svc-42487178 之外，平台 Odoo 與叢集上 17 個 standalone odoo 站的處置也完全沒有涵蓋。
- 『filestore 從 /bitnami/odoo 搬到 /var/lib/odoo』只寫了新佈局，沒有任何**資料搬遷**程序（PVC 內容從 Bitnami 佈局複製到官方佈局、hashdir 權限、uid 1001→101 的 chown 成本）。inventory C083 專門有一支 filestore-hashdir-guard 在處理這類權限問題，設計只說『原則上移除』。
- 『catalog.json 一致性』（平台 image 的 Dockerfile 與租戶 image 的 Dockerfile.cloud）今天沒有守線（C010／C058 交界，inventory §6 第 11 點），而設計新增的『installed 但磁碟不存在就 exit 78』會把這個既有風險放大成停機風險——設計把它列在 open_questions，但同時已經把依賴它的 fail-loud 寫進 P0。
- 『image build 時 python3 -c "import odoo; print(list(odoo.tools.config.options))" 產生真實選項集』未實測：`config.options` 是在 `_parse_config` 期間才被 optparse 的 my_default 填滿（odoo/tools/config.py:376-380），純 import 時的內容與完整選項集是否相同需要在官方 image 內實跑確認；同時 Odoo 的 config loader 對 conf 檔裡的未知鍵是靜默接受（:703 `self.options[name] = value`），所以這份白名單是唯一的守線，更需要實證。
- 『爭取一條不經 gate 的 cloud-odoo-dev:<sha> tag stream』與『開發期用 ConfigMap 覆蓋 entrypoint』兩個緩解都未經裁決，而取向 A 的可行性高度依賴其中之一——沒有它們，entrypoint 的每一輪除錯都是天級。設計自己也承認 ConfigMap 覆蓋是『新的靜默失效面＋租戶可控的 RCE 面』。
- 『helm release 的 stored values 會夾帶 sensitive_values』這一點的曝露面沒有評估：operator 以 `-f` 把 merged values 交給 helm，helm 會把該次 supplied values 存進 sh.helm.release.v1.* Secret。租戶讀不到（平台沒有回傳 values 的端點，已查 releases.py 無此路徑），但『憑證只在 tmpfs 上活 0400』這句敘述對叢集內的實際持久化狀態是過度宣稱。
- 『charts repo（woow-paas-charts）的發版是否也有人工 gate』未確認——設計的發版順序（image → chart → seed）與 contract 升版節奏整個建立在『chart 是分鐘級』這個假設上。

#### 站得住、值得採納的部分

1. **bootstrap 放 initContainer、不放 operator post-deploy exec**——與 #1136 實測（報告 §2「DB 建立／admin 建立」列）與 inventory §3.3 的結論一致，理由（Ready 即 DB 已 init ⇒ C008/C047 自動成立；`Init:Error` 可區分；log 不被主程序洗掉）成立。
2. **保留官方 `/entrypoint.sh` 當最後一棒**，而且「把 `db_password` 寫進自家 render 的 conf 就不會再出現在 `ps`」這個推論**是對的**：上游 18.0 `entrypoint.sh` 的 `check_config()` 是 `if ! grep -q ... ; then DB_ARGS+=(...)`，只有 conf 沒寫該鍵才補 CLI 參數（這是少數有上游原始碼支撐且查證無誤的宣稱）。`PASSWORD_FILE` 分支也確實存在。
3. **addon 介面塌縮成「永遠寫全量清單」**：`_remove_addon_helm_values`（`src/models/cloud_service.py:1744`）與 `ODOO_SELECTED_ADDONS_PATH`（:34）整段刪除、取消勾選＝寫空陣列——C018 那個 bug class 真的會消失，這是整份設計最紮實的一段。symlink 取代 `cp -r` 也對。
4. **升級閘的冪等判準用版號而不是「第幾次開機」**，且「先做事後記帳」（成功才寫回 release/digest）方向正確；`ODOO_RELEASE` 是 build arg（官方 Dockerfile `ARG ODOO_RELEASE`）確實拿得到。
5. **`values.schema.json` 全樹 `additionalProperties:false`** 作為 C004/C005/C013/C018/C066 那一整類「寫錯鍵不報錯」的機器守線——inventory §3.1 第 5 點就是這個處方（但它同時要求「並宣告 `global`」，見下）。
6. **釘 image digest**、**expose 明帶 `service_name`/`service_port`**（取代 operator `releases.py:1606` 的自動偵測，S1）、**`ODOO_RC` 由 image export、operator 三支腳本改讀 env**（C050/C052/C053）、**PVC label 的 chart render 測試**（C066）——這四條都是低風險純收益。
7. **contract 版本號**的想法本身對（三種發版速度之間唯一的鎖步機制），只是實作語意要改（見 flaw #3）。


### 取向 B：chart 擔責

**設計的自我定位**：取向 B「chart 擔責」：image 退化成「官方 odoo:18.0 ＋ addon payload ＋ pip 依賴」四行 Dockerfile，bootstrap／admin／odoo.conf／模組升級閘／addon 落點／websocket 分流全部收進自家 chart `odoo-tenant` 的 initContainer ＋ ConfigMap ＋ Secret；賭注是「行為改版只走 chart 版號、永遠不必過 image build 的人工 /approve gate」，代價是最脆弱的 shell 邏輯被放到目前最沒有自動驗證的那一層（chart repo 沒有真 K8s CI runner）。

**人日自估**：**合計 29.5～48.5 人日，中位數約 38**（盤點 §1.2 的基準是 27～47、中位數 35；取向 B ≈ +3，多出來的全在 chart 的可測性投資與端到端驗證）。

| 項目 | 盤點基準 | 取向 B | 差異理由 |
|---|---|---|---|
| 自家 chart | 5～8 | **8～12** | chart 變厚：conf-render ＋ bootstrap ＋ module gate ＋ nginx sidecar ＋ values.schema.json ＋ render tests；另含把 shell 抽成 `files/*.sh` 並寫 stub 驅動測試的投資（≈2 人日，是本取向刻意付的保險費）|
| cloud-odoo image | 1～2 | **1** | 只剩 FROM／pip／chown／USER 四處 |
| seed／values／init／expose | 3～5 | **3～5** | addon builder 變小，但 capability 欄位與 sensitive 通道擴充是新增，互相抵銷 |
| operator 常數對齊 ＋ helper 收斂 | 1～1.5 | **1～1.5** | 不變 |
| 同 PR 必改的測試 | 1～1.5 | **1.5～2** | 多出 vendored schema 契約測試與 carrier 斷言 |
| dev sandbox 端到端 | 2～3 | **3～4** | 多一項：initContainer 之間的交互（volume 權限、env 注入、securityContext）只能在真 K8s 驗 |
| P1 功能對等 | 10～18 | **8～14** | addon 重做在取向 B 反而便宜（平台只填一個 list）；plan overlay／SMTP 兩條路／憑證 reset／升級閘照舊 |
| P2 清理 | 4～9 | **4～9** | 不變 |

**估算假設**：(1) 熟悉本 repo 的一位工程師＋agent 輔助；不含 review 與 `/approve` 的等待時間。(2) `odoo-tenant-1.0.0` **拿不到**（盤點假設 2 不成立），已計入 chart 的 8～12。(3) stg `svc-42487178` 銷毀重建、不做資料遷移。(4) 不含 paas-odoo-self（另案 20～35）。

**報酬在之後**：每一次 bootstrap／odoo.conf／升級語意的修改，從「image build ＋ 人工 approve ＋ 平台 seed bump」三步變成「chart 發版 ＋ seed bump 版號」兩步，而前者的等待時間不可控。以每季 2～3 次調整估，一年省下的不是人日而是**不可預期的排程阻塞**。

**責任歸屬一覽**（做法全文見 asset）：

| 機制 | 歸屬 |
|---|---|
| M1 customPostInitScripts 之一：odoo-conf-system.sh（sed 注入 proxy_mode／list_db／workers／limit_*）失效 | chart |
| M2 customPostInitScripts 之二：fix-admin-credentials.sh（pbkdf2 雜湊 admin 密碼、寫死 id=2）失效 | chart + platform-odoo |
| M3 所有 ODOO_* env（ODOO_EMAIL／ODOO_PASSWORD／ODOO_DATABASE_*／ODOO_SKIP_MODULES_UPDATE）無人消費 | chart |
| M4 自動建 DB 與 admin（odooSkipInstall=false）不存在 | chart |
| M5 非首次啟動自動 --update=all 的升級語意消失（image 升版後 code 與 DB schema 脫節） | chart |
| M6 addon 注入（Bitnami initContainer ＋ customPostInitScripts ＋ /bitnami/odoo PVC 慣例、sed 改 addons_path） | chart + platform-odoo |
| M7 SMTP：chart 把 smtp* 轉成 ODOO_SMTP_* env 由 entrypoint 寫進 odoo.conf → 全失效 | chart + platform-odoo |
| 形狀差異①：filestore 路徑（官方 /var/lib/odoo vs Bitnami /bitnami/odoo）與 uid 101 權限 | chart |
| 形狀差異②：workers>0 時 /websocket 必須走 8072（8069 回 500） | chart |
| 形狀差異③：PVC 的 app.kubernetes.io/instance label（孤兒 PVC 延遲清理靜默失效） | chart |
| 形狀差異④：expose 的 service/port 靠 operator 自動偵測（Bitnami 剛好排對） | chart + platform-odoo |
| 形狀差異⑤：image_ref 三段式組裝、imagePullSecrets 路徑、storageClass 路徑三個注入契約 | chart + platform-odoo |
| 形狀差異⑥：plan overlay（cs_odoo_workers／cs_ram_gb）靠字串替換 customPostInitScripts | platform-odoo |
| 形狀差異⑦：operator exec 路徑全寫死 Bitnami（container 名、selector、odoo.conf、venv python） | chart + platform-odoo |
| 橫切：「寫錯鍵不報錯」這一整類（C004／C005／C018／C066／S1 的共同 bug class） | chart |

#### 批判的總判決

**不可照此執行。取向 B 的分層直覺（image 薄、chart 厚）是對的，但這份設計有三條會讓它在上線第一週就靜默壞掉的錯誤，且全部來自「沒有去讀平台 upgrade 路徑實際送了什麼」。**

三條致命傷有一個共同根源：設計假設 `helm upgrade` 是一條「平台每次都把完整意圖重新送一遍」的管線，實際上不是——
(1) reconfigure 不送 `sensitive_values`，(2) `helm_default_values` 只在 create 讀，(3) `_apply_plan_spec_overlay` 只在 create 呼叫。設計把 bootstrap／admin／升級閘全押在「chart 每次 render 都拿得到最新輸入」上，而這個前提在本 repo 的三條 upgrade 路徑上都不成立。

值得注意的是，這三條**不是取向 B 特有的**——取向 A（Bitnami＋覆寫）也會撞到同樣的 (2) 和 (3)。所以正確的順序是：**先修平台的 upgrade 管線，再談 chart 長什麼樣**。具體最小前置：
- `_assemble_deploy_values` 增加一條「從 template 重新 coalesce 平台管理鍵（image、chart profile 預設）」的步驟，或把 image digest 改放 chart values.yaml（走 #411 既有的 chart-default coalesce 保證）。
- reconfigure 的 `upgrade_kwargs` 補 `sensitive_values`（這是既有 bug，不是新設計造成的，但新設計會把它從「無害」變成「致命」）。
- `_apply_plan_spec_overlay` 移到 `_assemble_deploy_values` 裡（它本來就是純函式、有現成測試）。

這三項各約 0.5～1 人日，但設計沒有把它們列進 29.5～48.5 的估算裡，而它們是整個取向能否成立的前提。所以 effort_days 的估算基礎不成立——不是數字高低的問題，是缺了 critical path 上的項目。

次要但必須在寫第一行 template 之前定案的：updateStrategy（現行 seed 明寫 Recreate，新 chart 的 12 鍵裡沒有它，而逐層 `additionalProperties:false` 讓平台補不進去）、`admin_passwd` 在 values 契約裡沒有位置、以及 chart 發版與 image digest 的循環依賴。

**建議的下一步**：不要進 chart 撰寫。先開一張「平台 upgrade 管線三條 create-only 路徑」的票，在 dev sandbox 上用現行 Bitnami chart 驗證修好之後，同一組行為（改 image digest 能傳到既有服務、reconfigure 不吞 Secret、改 plan 會重算 values）在真 K8s 上跑過一次。這三件事驗過之後，取向 B 的其餘部分（M1／M3／M4／M6／形狀差異①②③）大體可以直接寫。

#### 致命傷與嚴重缺陷

**1. 🔴 fatal**

- **設計的主張**：M2：「reset 走既有 `_reset_via_helm_secret`（helm upgrade 換 Secret ＋ rollout restart）→ 重啟後 fingerprint 不符 → 改密碼」；fail_loud (1)「config.sensitive.admin_password 沒送達 → chart `_helpers.tpl` 的 `required` 讓 helm install 當場失敗」
- **為什麼會壞**：平台的 reconfigure 路徑根本不送 sensitive_values。`src/controllers/paas.py:3760-3775` 組的 `upgrade_kwargs` 只有 namespace／release_name／chart／values／version／reset_values 六個鍵，**沒有 sensitive_values**；而 operator 端 `extra/paas-operator/src/services/helm.py:840` 加的是 `--reset-values`、`:860-862` 只 merge「這次呼叫傳進來的」sensitive_values。`_build_mcp_sensitive_values` 全 repo 只有三個呼叫點：create（cloud_service.py:1384）、action_redeploy（:946）、reset（:1128）——reconfigure 不在其中。

現行 has_admin_gui 的 chart 之所以沒被這個洞咬到，是因為它們的 chart 是「write-only-if-absent」語意——`src/models/cloud_service.py:2190-2205` 的註解親口寫明：「回 {} 讓 helm 沿用現有 Secret……chart deployment.yaml 的 config.json 是 write-only-if-absent，既有 config.json 不動」。M2 的 fingerprint 主動比對，正好把這層容忍度拆掉。
- **具體情境**：租戶在 ConfigurationTab 把 SMTP host 從 smtp.gmail.com 改成 smtp.office365.com 並存檔 → controller 走 reconfigure → `helm upgrade --reset-values -f <只有非敏感 values 的檔>`。

分支 A（照設計加了 `required`）：helm render 當場失敗 → 任何一次 reconfigure（改網域、改 addon、改 SMTP）對 odoo 服務永久 500。整個 reconfigure 功能對這個 template 報廢。

分支 B（為了不壞而拿掉 `required`）：`<rel>-odoo` Secret 的 `admin-password` 被 render 成空字串 → `checksum/secret` annotation 改變 → pod rollout → bootstrap initContainer 算 sha256("") 與 DB 的 `woow.admin_password_fp` 不符 → 用 ORM 把租戶的 admin 密碼改成空字串（或 `${ADMIN_PASSWORD:?}` exit 1 讓 pod 永不 Ready）。租戶改了一個 SMTP 欄位，登入密碼就沒了，而 helm 全程綠燈。

同一個機制也吃掉 smtp_password：設計把它放進同一個 `config.sensitive` 子樹，所以 conf-render 的 `${SMTP_PASSWORD:?}` 會在每次 reconfigure 後失敗。

**2. 🔴 fatal**

- **設計的主張**：upgrade_story：「image 升版：……改平台 seed `helm_default_values.image.digest`（刻意放 seed 不放 chart：image 版本是平台的營運決策）→ 租戶下次 redeploy／reconfigure 走 `_assemble_deploy_values` 的 `reset_values` 全量管線帶下去 → pod 重建 → bootstrap 第二段偵測 digest 不同 → 跑 `-u`」
- **為什麼會壞**：`helm_default_values` 是 **create-only** 的輸入，reset_values 管線根本不讀它。`_assemble_deploy_values` 的組裝 base 是 `svc.helm_values`（`src/models/cloud_service.py:1578`），而它自己的 docstring（:1551）就寫著「``svc.helm_values`` —— template 預設（``template.helm_default_values``）＋ user config ＋ MCP api key ＋ plan overlay，**於 service create 時已 merge 定案**」。全 repo 讀 `helm_default_values` 的地方只有 `src/controllers/paas.py:3353`（create）、`src/controllers/ha_paas.py:297`（HA provision create）與 `cloud_app_template.py:401`（gateway path 判讀）——沒有任何 upgrade 路徑。

`src/models/cloud_app_template.py:388-392` 的 docstring 甚至把這個性質當成已知陷阱在講：「**存量服務**：``helm_values`` 是 create 當下的快照，本欄新增 ``gateway`` 鍵之前建立的服務其快照裡根本沒有這個鍵」。

歷史前例也坐實：`src/migrations/18.0.1.0.2/post-migrate.py` 修 Bitnami postgres image 搬遷時，寫的是 `UPDATE woow_paas_platform_cloud_app_template SET helm_default_values = %s`——**只改 template，一行都沒碰 cloud_service.helm_values**。當時租戶側 0 個實例所以沒人發現。

諷刺的是正確落點就在旁邊：放 chart 自己的 values.yaml，`reset_values` 的 chart-default coalesce（#411 的既有保證）就會自然帶下去。設計明確選了錯的那一邊，而且把它寫成「刻意」。
- **具體情境**：2026-12 官方 odoo:18.0 修了一個 RCE。平台照 upgrade_story 走完整套：paas-odoo-ci 過 /approve 建新 cloud-odoo、推 JCR、改 seed 的 `helm_default_values.image.digest`、merge、`-u woow_paas_platform`。

既有租戶下次 reconfigure → `_assemble_deploy_values` 從 `svc.helm_values` 取出**建立當天的舊 digest** → helm upgrade 送舊 digest → Deployment 的 image 一個字都沒變 → pod 不重建 → bootstrap 第二段比對 `WOOW_IMAGE_REF`（舊）vs `ir.config_parameter('woow.odoo_image_ref')`（舊）→ 相同 → 「不動」。

helm 綠燈、pod Ready、`woow.odoo_image_ref` 一致、M5 的所有 fail-loud 條件都沒被觸發。租戶永遠停在建立當天的 core，而平台的儀表板上看不出任何異常——這正是 M5 這整條機制號稱要消滅的那種失效。

**3. 🔴 fatal**

- **設計的主張**：chart_shape：「頂層鍵刻意壓到 12 個，且全部是平台會寫或租戶會改的東西」（image／imagePullSecrets／global／odoo／addons／bootstrap／upgrade／config／websocket／service／persistence／postgres／resources），搭配「values.schema.json **逐層** additionalProperties:false」
- **為什麼會壞**：12 個鍵裡沒有 `updateStrategy`，而現行 seed 明寫 `"updateStrategy": {"type": "Recreate"}`（`src/data/cloud_app_templates.xml:242`）——那不是裝飾，是 RWO PVC 上單副本 Deployment 的必要設定。逐層 `additionalProperties:false` 還讓平台連事後補都補不進去（送 `updateStrategy` 會被 schema 擋成未知鍵）。

K8s Deployment 預設 `RollingUpdate`（maxSurge 25%，單副本時向上取整為 1）⇒ 新 pod 會在舊 pod 還持有 volume 時被建出來。woow-k3s 的 `longhorn-delete` 是 RWO。

而且平台這一層完全看不見：`HelmService.install()`／`upgrade()`（`extra/paas-operator/src/services/helm.py:665,830`）都**沒有 `--wait`**（只有 reset 路徑 `releases.py:942` 用 atomic=True），所以 helm 立刻回 success；`_is_container_ready`（`src/models/cloud_service.py:2658`）只要看到「一顆 Running 且 containers 全 ready 的 pod」就回 True——舊 pod 正好滿足。
- **具體情境**：情境 A（RWO，最可能）：租戶改設定 → helm upgrade → 新 ReplicaSet 建 pod → Longhorn 拒絕第二次 attach → 新 pod 永久 `ContainerCreating` / Multi-Attach error → helm 已回 success、舊 pod 仍 Ready → 平台顯示 running。**新的 odoo.conf、新的 addon 選擇、新的 image 一行都沒生效，而且沒有任何地方會紅。**這是整份設計裡最純粹的靜默失效，而它出現在設計自己用來「解決靜默失效」的那一層。

情境 B（若 filestore 改 RWX 或多 replica）：新 pod 的 bootstrap initContainer 對著舊 pod 正在服務的同一個 DB 跑 `odoo -u all`。Odoo 的 module upgrade 會改 schema、重寫 ir_model_data，與線上流量並行 ⇒ 資料損壞級事故。

**4. 🟠 serious**

- **設計的主張**：形狀差異⑥：「這是『商業規格 → chart values』的映射……只是載體從 shell 字串換成型別化的鍵。**附帶修好「post-init 只跑一次、plan 變更對既有 PVC 不生效」**」；以及 M1 why_here 的「放 chart 還順手修掉 C015」
- **為什麼會壞**：問題從來不在載體，在呼叫點。`_apply_plan_spec_overlay` 全 repo 只有**一個**呼叫者：`src/controllers/paas.py:3419`，在 `_create_service` 裡、`service_vals` 寫入之前。`_assemble_deploy_values`（upgrade／reconfigure／redeploy 三條路共用）從頭到尾沒有 overlay 這一步。

把 `values['customPostInitScripts']` 的字串替換換成 `values['odoo']['config']['workers'] = n`，只是把一段 create 期執行的程式碼改寫成另一段 create 期執行的程式碼。plan 變更之後依然沒有任何東西會重算。

設計還在 fail_loud (2) 說「新寫法沒有 `and scripts` 這個 and 條件，漏設即驗證失敗」——但那段程式碼在 upgrade 路徑上根本不會被執行到，fail-loud 也就不會響。C015 沒有被解決，只是換了一個外觀。
- **具體情境**：租戶從 Starter（cs_odoo_workers=0）升級到 Business（cs_odoo_workers=4、cs_ram_gb=8）。sh_subscription 扣款成功、plan 改了、UI 的「已配置規格」顯示 4 workers 8GB（因為 `_spec_display_values` 直接讀 plan）。

但沒有任何路徑重跑 overlay ⇒ `svc.helm_values` 裡 `odoo.config.workers` 仍是 0、`resources.limits.memory` 仍是 2Gi。下一次 redeploy 送的是舊值。租戶付了 Business 的錢、拿到 Starter 的容器，而 UI 顯示一切正常。

連帶：`workers>0 ⇒ websocket.mode=sidecar` 的 `fail` 守線也永遠不會被觸發，因為 workers 永遠是 0。

**5. 🟠 serious**

- **設計的主張**：M6：「`_remove_addon_helm_values`（:1744）**整支刪除**（整鍵覆寫成空 list 天然就是移除）」「C018 那個 bug class（拔除靠名稱比對、漏掉就殘留）整類消失」
- **為什麼會壞**：沒有任何程式碼會寫出那個空 list。`_assemble_deploy_values` 的 addon 分支閘門是 `if self.odoo_addon_ids and template.slug == 'odoo'`（`src/models/cloud_service.py:1639`）——odoo_addon_ids 為空時整段被跳過，不會 merge `{'addons': {'selected': []}}`。

reconfigure 側：`merged_values = self._deep_merge(existing_values, nested_user_values)`（`src/controllers/paas.py:3679`）。addon 選擇不是 helm_value_spec（它走 `odoo_addon_ids` 這個 m2m 欄位），所以 `nested_user_values` 裡沒有 `addons` 鍵；`_deep_merge`（:2957-2965）對缺席的鍵不動作 ⇒ `existing_values` 裡的舊 `addons.selected` 原封保留。設計要刪掉 `:3691` 的 `else: service._remove_addon_helm_values(merged_values)` 之後，就沒有任何人負責清掉它了。

「整鍵覆寫」只有在**每次都無條件寫出該鍵（含空 list）**時才成立；設計描述的 diff（「`_build_odoo_addon_helm_values` 縮成三行」）沒有改那個 `if self.odoo_addon_ids` 閘門。
- **具體情境**：租戶勾了 `woow_pos_extension`，用了三個月後在 AddonsTab 取消勾選最後一個 addon → `_assess_addon_removal` 放行 → `odoo_addon_ids` 清空 → merged_values 仍帶 `addons.selected: ['woow_pos_extension']` → helm upgrade 綠燈 → addon-selector initContainer 照舊從 `/all-addons/woow_pos_extension` 複製 → conf 的 addons_path 照舊含 selected 目錄 → 模組還在檔案系統上、在 Apps 列表裡還看得到。

UI 顯示「已移除」，容器裡還在。與 C018（found-in-verify 的那條 silently-wrong）同一個 bug class，只是從「initContainers list 沒濾乾淨」換成「selected list 沒被覆寫」。

**6. 🟠 serious**

- **設計的主張**：cross_repo_friction：「**發版順序不可調換**：(1) chart 0.1.0 發版 → (2) image 過 `/approve` 建出新 base 的 cloud-odoo → (3) 平台 seed PR」；chart_shape：「`image`：`required: [repository, digest]`……digest required 是 M5 fingerprint 的前提」
- **為什麼會壞**：這個順序有循環依賴。Helm 的 `values.schema.json` 會對 chart **自己的 values.yaml** 生效（`helm lint --strict` 就是在驗這件事，而設計指定它是 chart repo CI 的主要 gate）。digest 依設計「刻意放 seed 不放 chart」⇒ chart 的 values.yaml 裡 `image.digest` 只能是空字串或缺席 ⇒ `required: [repository, digest]` 當場讓 `helm lint --strict` 紅 ⇒ **chart 0.1.0 發不出去**，除非先有一個真實存在的 digest，而那要等 image 過 `/approve`。步驟 (1) 依賴步驟 (2)。

第二個循環在同一段：設計要求「chart repo 存一份『平台 `_assemble_deploy_values` 實際輸出』的 fixture 跑 helm template」。那份 fixture 的內容取決於平台 seed 的新 values 形狀，而 seed PR 是步驟 (3)。步驟 (1) 的 CI 依賴步驟 (3) 的產物。

這對取向 B 特別痛，因為它的賣點就是「行為改版只走 chart 版號」——但第一次發版就卡在 image gate 上，而且卡法是循環而非排隊。
- **具體情境**：工程師照順序做：寫完 chart、跑 `helm lint --strict` → `image.digest is a required property` → 拿不到 digest（image 還沒 build）→ 只好把 digest 從 required 拿掉、填一個假值、或改成 tag。

三條退路都把 M5 的地基抽掉：M5 的 fail_loud (1) 明寫「沒有 digest，fingerprint 退化成 tag，tag 重用會靜默跳過升級」。於是第一次發版就在最不引人注意的地方（chart 的 values.yaml 預設值）把整個模組升級閘降級成不可靠，而且之後沒有任何測試會提醒。

**7. 🟠 serious**

- **設計的主張**：M3：「內建 postgres 的密碼由 chart `randAlphaNum 24` 自產、以 `lookup` ＋ `.Release.IsUpgrade` 雙判沿用既有 Secret」；fail_loud (5)：「反向守線：lookup 在 helm template／--dry-run 回空會 render 出新密碼 → chart test 必須對 upgrade 情境斷言密碼不變」
- **為什麼會壞**：設計指定的那條守線在它指定的地方寫不出來。`lookup` 在 `helm template` 與 `--dry-run` 下**一律回空 map**（Helm 明訂行為，因為沒有 cluster connection）；而 chart repo 的測試慣例就是 `helm template` ＋ 斷言（設計自己引用 `charts/mcp-server/tests/` 的 6 支 .sh），而且 chart repo 沒有 K8s CI runner（設計在 open_questions 1 自己承認）。所以「對 upgrade 情境斷言密碼不變」這條測試只有兩種可能：斷言反了（永遠看到新密碼、測試綠但守的不是同一件事），或根本寫不出來。

更糟的是它與已存在的 `--atomic` 路徑交互：`extra/paas-operator/src/api/releases.py:942` 與 `:1126`（reset 與 reset-config 兩條路）都是 `atomic=True`。`--atomic` 失敗時 helm 會再跑一次 rollback，過程中重新 render。任何一次 render 沒撈到既有 Secret 就產生新的 postgres 密碼，而 PVC 上的 DB 還是舊密碼。設計在 open_questions 5 點名了這個風險，然後仍然把它留在正式設計裡。
- **具體情境**：租戶按「重設管理員密碼」→ `_reset_via_helm_secret` → operator `reset_release_config`（`releases.py:942`，`atomic=True`，timeout n 秒）→ bootstrap initContainer 要等 DB、跑 odoo shell 改密碼，在大一點的 DB 上超過 n 秒 → `--atomic` 觸發 rollback → helm 重新 render 上一版。

若這次 render 的 `lookup` 因為任何原因回空（rollback 路徑、RBAC 不足、或 `.Release.IsUpgrade` 在 rollback 情境下的值與預期不同），postgres Secret 被寫成一把新密碼，而 PVC 上的 PostgreSQL 仍用舊密碼初始化過。odoo 從此連不上 DB，**且不會自我修復**——每次重啟都用同一把錯密碼。租戶按了一個「重設密碼」按鈕，換來一台需要人工進 DB 改密碼才能救的服務。

**8. 🟠 serious**

- **設計的主張**：M1：「initContainer `conf-render`……再 append 敏感段 db_password／**admin_passwd**／smtp_password，值由 valueFrom.secretKeyRef 的 env 取得」；chart_shape：「`config.sensitive`：`additionalProperties: false`，**鍵名固定兩個**」（admin_password、smtp_password）
- **為什麼會壞**：設計內部自相矛盾：M1 要 render 三個敏感值，values 契約只給兩個槽位，而且明文鎖死 `additionalProperties: false`。Odoo 的 `admin_passwd`（database manager 的 master password）在契約裡沒有位置。

三種補法都有問題：(a) 拿 `config.sensitive.admin_password` 兼用 ⇒ 租戶的 Odoo 登入密碼 == DB manager master password，一把洩漏等於整台 DB 可被 drop／dump；(b) chart 自產 randAlphaNum ⇒ 回到 lookup 那條問題（每次 render 換一把，而且租戶永遠不知道是什麼）；(c) 不寫 ⇒ 用官方 odoo.conf 的預設——我查了官方 `odoo/docker` 18.0 的 odoo.conf，`admin_passwd` 是**註解掉的**（`; admin_passwd = admin`），代表落到 Odoo 內建預設值。

`list_db = False` 只擋資料庫列表，不擋 `/web/database/manager` 底下其他帶 master password 的動作。盤點 §3.3 的「風險與待量測」第三點已經點名這件事（「就算 list_db=False 也應該設一個隨機值做縱深防禦」），設計沒有承接。
- **具體情境**：chart 依契約只發兩個 sensitive 鍵，conf-render 的 `admin_passwd` 段因為拿不到值而被省略（或 `${ADMIN_PASSWD:?}` 讓 pod 起不來，工程師為了讓它起來就拿掉那一行）。

結果：每一台租戶 odoo 的 database master password 都是 Odoo 內建預設。`proxy_mode=True` 加 Cloudflare tunnel 意味著這些站全部公開可達。任何知道預設值的人對 `https://<tenant>.woowtech.io/web/database/manager` 送一次 backup／drop 請求就能取走或毀掉整個租戶資料庫。helm 綠燈、pod Ready、schema 驗證全過——因為這個鍵在 schema 裡根本不存在。

**9. 🟡 minor**

- **設計的主張**：多處 fail_loud 宣稱：「initContainer Error → pod 永不 Ready → 平台 `_check_transition_timeout` 轉 error **並帶 log**」（M1(2)、M2(2)、M4(1)、M5(2)、形狀差異①(1) 都靠這句話當兜底）
- **為什麼會壞**：這條兜底既不快也不帶 log。`_is_container_ready`（`src/models/cloud_service.py:2658`）只判「有沒有一顆 Running 且 containers 全 ready 的 pod」——initContainer 失敗時 pod 狀態是 `Init:Error`，走的是「不 Ready」這條，和「還在拉 image」完全無法區分。真正翻 error 要等 `DEPLOY_TIMEOUT_MINUTES_DEFAULT = 30`（`src/models/cloud_service.py:355`）。

「並帶 log」沒有依據：平台拿到的是 operator status 回的 pods 列表，裡面沒有 initContainer 的 termination message、沒有 exit code、沒有 log。設計把 7 條 fail_loud 都掛在這個兜底上，實際上得到的是「30 分鐘後一個沒有原因的 error」。

這把設計主打的 fail-loud 變成 fail-slow-and-opaque——對維運而言，和靜默失效的差別只有「最後有一行紅字」。
- **具體情境**：conf-render 的 `test -w /var/lib/odoo` 因為 fsGroup 在 Longhorn 上沒如預期生效而 exit 1（盤點 §6 第 13 點列為未量測項）。

租戶按下建立 → 服務卡在 `deploying` 30 分鐘 → 翻成 `error`，`error_message` 是一句泛用的 pod-not-ready。維運要自己 `kubectl logs <pod> -c conf-render` 才知道原因，而叢集鐵律規定只能動 `paas-ws-*` 等特定 namespace、且要先找到是哪一顆 pod。

設計宣稱的「fsGroup 設錯當場炸，而不是等到租戶第一次存附件才 500」只有前半句成立——確實不會等到存附件，但「當場」是 30 分鐘後、沒有原因。

**10. 🟡 minor**

- **設計的主張**：M1 why_here：「官方 image 的 /etc/odoo/odoo.conf 是 **root 所有**、烤在 layer 裡，唯一不改 image 的覆蓋法就是 chart 用 emptyDir 整個蓋掉」；M1 evidence：「官方 odoo/docker 18.0 **entrypoint.sh** 的 `: ${ODOO_RC:=/etc/odoo/odoo.conf}`」
- **為什麼會壞**：兩項事實都錯，而它們是 M1 選擇「emptyDir 蓋整個 /etc/odoo」而非 ConfigMap 的唯一論據。

我取了官方 `odoo/docker` 18.0 的 Dockerfile：`:89` 是 `RUN chown odoo /etc/odoo/odoo.conf`——conf **屬於 odoo（uid 101）**，不是 root。所以「唯一的覆蓋法」不成立：ConfigMap 以 subPath 掛單檔、或讓 initContainer／主容器直接改寫那個檔，都是可行選項。

`ODOO_RC` 也不在 entrypoint.sh 裡——我取的 entrypoint.sh 從第一行到最後一行沒有 `ODOO_RC:=` 這個 default assignment；它是 Dockerfile `:98` 的 `ENV ODOO_RC=/etc/odoo/odoo.conf`。差別有實質後果：ENV 可以被 podSpec 的 `env` 覆蓋（而且逐層 additionalProperties:false 之後沒人擋得住有人在 chart template 裡加一條），entrypoint 裡的 `:=` default 則不會被靜默改掉。

結論（用 emptyDir）也許仍然對，但它是靠兩個錯誤的事實推出來的，而設計在其他地方（M3、M5）示範了它會去查原始碼——這裡沒查。
- **具體情境**：沒有直接的執行期故障，但有 review 期的故障：任何人照著 M1 的論據去評估「要不要改用 ConfigMap subPath 讓 conf 可被 `kubectl get cm` 檢視」時，會被「root 所有、只能整個蓋掉」勸退，於是接受一個讓 odoo.conf 完全不可觀測（emptyDir medium: Memory，只能 exec 進去看）的設計。

而 M1 自己又宣稱 fail_loud (4)「chart render test 斷言 ConfigMap 含 `proxy_mode = True`／`list_db = False`」——但最終 conf 是 initContainer 在 emptyDir 裡拼出來的，ConfigMap 只有 tpl。render test 驗的是模板，不是租戶容器裡真正生效的那份檔案。

#### 設計裡沒有證據支撐的宣稱

- 【已證偽，列此存查】「改 seed 的 `helm_default_values.image.digest` → 租戶下次 redeploy／reconfigure 走 reset_values 管線帶下去」——`_assemble_deploy_values` 的 base 是 `svc.helm_values`（create 快照），`helm_default_values` 只在 `controllers/paas.py:3353` 與 `controllers/ha_paas.py:297` 讀。
- 【已證偽】「官方 image 的 /etc/odoo/odoo.conf 是 root 所有」——官方 18.0 Dockerfile:89 是 `RUN chown odoo /etc/odoo/odoo.conf`。
- 【已證偽】evidence 引用「官方 entrypoint.sh 的 `: ${ODOO_RC:=/etc/odoo/odoo.conf}`」——entrypoint.sh 沒有這一行，ODOO_RC 來自 Dockerfile:98 的 ENV。
- 【已證偽】「形狀差異⑥附帶修好 plan 變更對既有服務不生效」——`_apply_plan_spec_owerlay` 唯一呼叫點是 `controllers/paas.py:3419`（create），換載體不改變呼叫點。
- 【已證偽】「`_remove_addon_helm_values` 整支刪除，整鍵覆寫成空 list 天然就是移除」——`_assemble_deploy_values:1639` 的 `if self.odoo_addon_ids` 閘門讓空選時完全不寫該鍵。
- 「initContainer Error → 平台轉 error **並帶 log**」——平台只有 30 分鐘 deploy timeout（cloud_service.py:355）與泛用的 not-ready 理由；沒有任何程式碼收集 initContainer 的 log 或 termination message。需要實作才成立。
- 「helm `--atomic` rollback 情境要實測」（設計自己列為 open_question 7）——但 reset 路徑已經是 `atomic=True`（releases.py:942、:1126），所以這不是未來才要測的邊角，是 reset 功能的主路徑。在 dev sandbox 跑過之前，M2 的整條 reset 語意都是未驗證的。
- 「nginx sidecar 聽 8080，Service 只開一個 port 80 → 8080」與 operator `generate_service_url` 的實際行為未對照。設計只引用 ExposeConfig 支援 service_name/service_port，沒有驗證 Cloudflare tunnel route 建立後 targetPort 的解析路徑。
- 「workers=0 時 8069 的 /websocket 是否可用」——#1136 只測了 workers=2。若可用，小 plan 可完全免除 nginx sidecar（設計自己在 open_questions 8 提出），這會改變 chart 的複雜度估算，但沒有量測。
- 「bootstrap 首次 init 約 2 分鐘」只在 #1136 的空 DB ＋ 單一 release 上測過。`-u all` 在真實大小租戶 DB 上的耗時完全沒有數據，而它直接決定 startupProbe.failureThreshold、helm timeout、平台 30 分鐘 timeout 三者能否對齊。
- 「`python3 -m pip install --break-system-packages` 之後 operator exec 的 `python3` 找得到套件」——官方 image 是 ubuntu:noble（已驗 Dockerfile:1），但 odoo 本體與第三方套件是否在同一個 `sys.path`、`update_module_list.py` 的 `import odoo` 在該 image 內是否成立，都沒有實測。這是 operator `/init/odoo` 能不能跑的前提。
- 「chart 固定契約：主 Service 名＝release fullname 裸名」與 operator 的 orphan PVC selector、scale、suspend/resume 三條路徑的交互沒有驗證。`extra/paas-operator/tests/test_scale.py:3` 的「Bitnami 系以 StatefulSet 部署」假設在 odoo 退出後是否仍正確，設計只說「docstring 更正」（C063），沒有查 scale 的實際分支邏輯。
- 「imagePullSecrets 取聯集後套在所有 container 與 initContainer」——平台送的 `global.imagePullSecrets` 是**字串陣列**（`_inject_image_pull_secrets`，cloud_service.py:1859-1863 的 `existing.append(secret_name)`），operator 頂層 setdefault 的形狀未查。chart 若期待 `[{name: x}]` 物件陣列，schema 會擋（吵，可接受）；若 schema 寫成 array of string 而 template 直接 toYaml 進 podSpec，會產出非法 Deployment。兩邊的型別契約沒有在設計裡寫死。
- 「vendored values.schema.json 在平台端做部署前驗證，錯誤落在租戶看得到的 UI」——`jsonschema` 是否在 Odoo pod 的依賴裡（`src/__manifest__.py` 沒有 external_dependencies 宣告這一項）未查。若不在，這條「主樑的前半段」在平台側跑不起來。
- 「effort_days 29.5～48.5」——未計入本批評指出的三項 upgrade 管線前置改動（sensitive_values on reconfigure、image digest 落點、plan overlay 呼叫點搬家），也未計入 updateStrategy／master password 的設計補完。這些在 critical path 上，估算基礎不成立。

#### 站得住、值得採納的部分

確實站得住、不必重想的部分：

1. **M3 的「db 參數全寫進 odoo.conf、一個 `ODOO_*` env 都不發」是正確的，而且比設計自己講的更有依據。** 我實際取了官方 `odoo/docker` 18.0 的 `entrypoint.sh`：`check_config()` 的 `if ! grep -q -E "^\s*\b${param}\b\s*=" "$ODOO_RC"` 確認只在 conf 沒有該鍵時才追加 `--db_*` 到 argv。更關鍵的是 `wait-for-psql.py` 也**優先讀 `ODOO_RC`**（`config_options.get('db_host') or args.db_host or os.environ.get('PGHOST')`），所以 DB_ARGS 為空時它不會退化去連預設的 `db`/`odoo`——這是設計沒查、但剛好成立的一個關鍵前提。`ENV ODOO_RC=/etc/odoo/odoo.conf` 在 Dockerfile:98 確認存在。

2. **M4 把 bootstrap 放 initContainer、不用 Helm hook Job、不用 operator post-deploy exec，理由是硬的。** 我確認 `HelmService.install()`（`extra/paas-operator/src/services/helm.py:665-700`）**沒有加 `--wait`**，所以 2 分鐘的 bootstrap 不會撞 helm 的 5 分鐘 subprocess 上限；而 `_is_container_ready`（`src/models/cloud_service.py:2658`）要求「至少一顆 Running 且容器全 ready 的 pod」，initContainer 執行期間 pod 不算 Ready ⇒「Ready 即代表 DB 已 init」這個語意真的成立，C008／C047 真的被解掉。這一條是整份設計最扎實的推理。

3. **「先做事、成功才寫標記」的三個冪等支點在機制上正確**（`to_regclass` ＋ `base` installed 雙判準、fingerprint 比對、digest 比對）。#1136 已實測第 1 點。第 2、3 點的邏輯本身沒問題——壞的是輸入到不了（見 fatal 1／2），不是判準本身。

4. **`values.schema.json` 逐層 `additionalProperties:false` 作為 fail-loud 主樑是對的，而且是自建 chart 相對於「Bitnami 硬套」唯一買得到的真東西。** #1136 §5 明說硬套「C004／C005／C018 那類靜默失效一條都沒解」，這個判斷我認同。

5. **dotted spec key 可行，不需要額外設計。** 我確認現行 seed 已經有 `auth.anthropicApiKey`、`main.config.GENERIC_TIMEZONE` 這種點分 key（`src/data/cloud_app_templates.xml`），`_unflatten_dotpath_keys` ＋ `_filter_allowed_helm_values` ＋ `_sanitize_helm_values` 三層都吃得下。所以把 `odooEmail` 換成 `odoo.admin.login`、`smtpHost` 換成 `odoo.smtp.host` 不會動到 #535／#559 的機制。

6. **`ExposeConfig.service_name`／`service_port` 確實已存在**（`extra/paas-operator/src/models/schemas.py:44,48`，設計引用的行號正確，只是路徑少寫了 `models/`），形狀差異④「平台明帶、把 operator 自動偵測降級成永遠用不到的 fallback」可以直接做，operator 不用改。

7. **形狀差異③（PVC 的 `app.kubernetes.io/instance` label）值得保留，而且我補一個設計沒提到的佐證：** operator 的 PVC-keep post-renderer（`extra/paas-operator/src/pvc_keep_post_renderer.py:38`）只對頂層 `kind: PersistentVolumeClaim` 蓋 `helm.sh/resource-policy: keep`，**不碰 StatefulSet 的 `volumeClaimTemplates`**。所以 postgres 用 StatefulSet 時它的 PVC 不會被 helm 刪（本來就不會），但也完全不受 keep 保護機制管——孤兒清理的 label 是唯一的回收途徑，設計把它列為 chart 契約是對的。

8. **image_delta 的四項判準（addon payload／catalog／pip 依賴／digest pin 必須是 layer 內容）推理正確**，`/all-addons` 目錄名必須保留的理由（`update_module_list.py:48` ＋ `cloud_odoo_addon.py:9`）也查證無誤。官方 base 確實是 `ubuntu:noble`（Dockerfile:1），`--break-system-packages` 的必要性成立。


### 取向 C：平台／operator 擔責

**設計的自我定位**：取向 C＝「chart 與 image 都退化成通用 runtime，所有 PaaS 語意由平台 model／controller ＋ paas-operator 的 exec 路徑重建」。核心裝置有兩個：(1) **odoo.conf Secret** —— 平台純函式渲染整份 conf（含已 hash 的 admin_passwd、smtp_password），經 operator 寫成 K8s Secret，chart 只用三個通用原語（extraVolumes／extraVolumeMounts／extraEnvVars 設 ODOO_RC）把它掛進去，憑證一格都不進 helm values 也不進 Deployment 的明文 env；(2) **階段化 provisioning endpoint** —— operator 新增一支冪等、每次只推進一個階段、每次都秒回的 `/odoo/provision`，由平台既有的狀態機驅動。賭注是：平台已經有狀態機、#559 ACL／#535 遮罩、#357 憑證記錄與 catalog 白名單，把 bootstrap 收進來只是讓「誰決定這台 Odoo 長怎樣」回到單一權威，代價是 operator 多一條對租戶 pod 執行任意程式碼的路徑、以及 C047 的重試預算必須重新設計（本文正面解掉它）。

**人日自估**：**合計約 24～34 人日**（單人約 5～7 週，中位數 29）。與盤點 §1.2 的 P0＋P1（23～38）同量級，但分布明顯不同：chart 更輕、operator 更重。

| 項目 | 人日 | 說明 |
|---|---|---|
| chart `odoo-runtime` 首版 ＋ `values.schema.json` ＋ render test | 3～4 | 比盤點的 5～8 少，因為不含 bootstrap／addon／憑證邏輯；多出的是 schema 與 render test |
| `Dockerfile.cloud` 換官方 base（含 pip 旗標、`chown -R odoo /all-addons`、catalog 一致性驗收） | 1～1.5 | 與盤點同（C067／C068） |
| operator：conf Secret 端點 ＋ 階段化 provision（7 階段）＋ detached exec/sentinel | 4～5 | 新工，取向 C 的主體 |
| operator：module_upgrade 的 scale-to-0 ＋ 一次性 upgrade pod | 2～3 | 新能力（`run_oneshot_pod`），RWO 與停權語意的互動最花時間 |
| operator：`odoo_exec.py` 收斂（C049～C057 七條）＋ Cloudflare 多規則（`create_route`） | 2～2.5 | 收斂本身不難，但要同時改兩支既有服務不破壞現行行為 |
| 平台：conf renderer ＋ chart profile ＋ 刪 addon values 路徑（C001～C004、C015、C018、C023～C025、C046） | 4～5 | 刪的比加的多，但 controller／model 兩份 post-deploy init 的收斂要算進來 |
| 平台：狀態機改造（`init_phase`、只算 failed、`initializing` 納入 age timeout）＋ #357 接入（`has_admin_gui`、`odoo_orm` 載體、reset） | 2～3 | 風險最高的一塊，會動到 n8n 共用的 `_record_init_failure` |
| 平台 seed 切換（C012／C013／C014／S3／S4）＋ 既存實例守門 ＋ migration | 1.5～2 | 含 stg `svc-42487178` 銷毀重建與伴生 MCP 重設 |
| 測試：平台單元（改寫 C019～C021、C033～C036、C054 ＋ 新增）＋ operator unit | 2～3 | |
| **operator integration test（真 K8s，無 CI runner）** | 2～3 | 取向 C 不可省的一塊；盤點 §2.8 確認現況為零 |
| dev sandbox 端到端驗證（首裝→登入→addon→reconfigure→換密碼→image 升版→websocket） | 2～3 | 與盤點同 |

**估算假設（與盤點 §1.2 一致的部分不重述，只列本取向特有的）：**

1. `/web/health` 在 DB 未 init 時回 200 成立（見 open_questions 第 1 條）。**不成立的話整個取向 C 的驅動層要改成 chart initContainer，本估算作廢，回到取向 A／B。**
2. detached exec 在 kubectl exec 連線關閉後能存活（`setsid nohup`），且 sentinel 能區分「還在跑」與「跑到一半被 pod 重啟殺掉」。不成立的話 `db_init` 與 `module_upgrade` 都要改走一次性 pod，＋2～3 人日。
3. 不含 review、prod build `/approve` 的人工等待，也不含平台 Odoo 自身（Fleet 那半條路）。
4. `odoo-tenant-1.0.0` 拿不到（設計已不假設）；拿到的話 chart 那 3～4 人日可能省一半。

**責任歸屬一覽**（做法全文見 asset）：

| 機制 | 歸屬 |
|---|---|
| 失效1a：customPostInitScripts / odoo-conf-system.sh（proxy_mode、list_db、workers、limit_*） | platform-odoo（渲染）＋ operator（佈建）＋ chart（只掛載） |
| 失效1b：customPostInitScripts / fix-admin-credentials.sh（admin 密碼 pbkdf2 雜湊、寫死 `WHERE id=2`） | platform-odoo（持有密碼）＋ operator（ORM 寫入） |
| 失效2：所有 `ODOO_*` env（ODOO_EMAIL／ODOO_PASSWORD／ODOO_DATABASE_*／ODOO_SKIP_MODULES_UPDATE）無人消費 | platform-odoo（seed 重寫）＋ chart（只留通用 extraEnvVars） |
| 失效3：自動建 DB 與 admin（`odooSkipInstall=false`）不存在 | operator（執行）＋ platform-odoo（驅動與計數） |
| 失效4：非首次啟動自動 `--update=all` 的升級語意消失（image 升版後 code 與 DB schema 脫節） | operator（執行）＋ platform-odoo（判定與閘門） |
| 失效5：addon 注入（initContainer ＋ customPostInitScripts ＋ `/bitnami/odoo` PVC 慣例；路徑常數 `/opt/bitnami/odoo/selected-addons`、`/all-addons/`） | image（唯讀倉庫）＋ operator（複製）＋ platform-odoo（清單） |
| 失效6：SMTP（chart 把 smtp* 轉成 ODOO_SMTP_* env 由 entrypoint 寫進 odoo.conf） | platform-odoo（渲染／持有）＋ operator（ORM 建 record） |
| 形狀差異：filestore 路徑（官方 `/var/lib/odoo` vs Bitnami `/bitnami/odoo`）與 uid 101 / fsGroup | chart |
| 形狀差異：workers>0 時 `/websocket` 必須走 8072（8069 回 500） | platform-odoo（決定）＋ operator（Cloudflare 多規則）＋ chart（開第二個 port） |
| 形狀差異：expose 自動偵測挑錯 Service／port | platform-odoo（明帶）＋ chart（命名契約） |
| 形狀差異：PVC 的 `app.kubernetes.io/instance` label（孤兒 PVC 延遲清理的唯一依據） | chart |
| 額外：operator exec 路徑的寫死常數（container 名、pod selector、interpreter、conf 路徑） | operator（收斂）＋ chart（契約） |
| 額外：image_ref 三段式組裝的兩份實作，新 schema 下會組出 `docker.io/:latest` | platform-odoo |
| 額外：imagePullSecrets 兩層（私有 JCR image 拉不下來且平台沒有錯誤訊號） | chart（兩個鍵都吃）＋ operator（既有 setdefault 不動） |
| 額外：seed 切換本身與『既存 release 不得 in-place upgrade 成新 chart』 | platform-odoo（seed ＋ 守門） |

#### 批判的總判決

**不可照此執行。核心裝置成立，責任分配不成立。**

conf Secret ＋ `ODOO_RC` 這個機制我從上游原始碼確認過，站得住；`init_odoo` 的 120s vs 150s 落差是真發現；對 C047 重試語意的診斷也對。但「platform-heavy」這個取向落到本 repo 的現行管線上，有四條會讓服務在 helm 綠燈下壞掉或根本裝不起來的結構缺口，其中三條彼此獨立、不是同一個 bug 的不同切面：

1. **語意鍵 vs `additionalProperties:false` 互斥**——沒有分隔層，spec 值必經 `helm_values` 進 helm（`cloud_service.py:1577`）。這兩個決定只能二選一。
2. **`has_admin_gui=True` 會把 `config.sensitive.admin_password` 送進新 chart**（`:2175` 只 gate on has_admin_gui，不看 carrier）。
3. **postgres 憑證沒有產生者**——schema 禁字面密碼、chart 不產、平台不碰 kubectl、operator 沒被指派。
4. **conf Secret 的產生時機把 `conf` 排成 provision 第一階段，而 provision 在 pod Ready 之後才跑**——不是循環相依就是退化成 image 預設 conf（`list_db=True` ＋ 預設 master password 對外開著）。

另外兩條是取向層級的，不是實作細節：**module_upgrade 的 scale-to-0 與 `upgrading` 的 30 分 age timeout ＋ `_rescale_halted_after_upgrade` 的既有 replicas 擁有者正面衝突**；以及**「同一套 chart 同時服務平台 Odoo」在 `additionalProperties:false` 之下不成立**——缺口不在設計自陳的驅動層，而在 chart 本身。

而設計的兩個地基假設都沒有證據：prototype 是用 **initContainer 先把 DB 建好才讓主容器起來**（`assets/1136/values-official.yaml`），從來沒有讓主容器對著空 DB 跑過；detached exec 的存活性也零實測。取向 C 的整個驅動層就架在這兩格上。

**建議的收斂方向**（不是重寫，是把責任挪回有證據的那一邊）：
- 保留 conf Secret ＋ `ODOO_RC`、保留「只算 failed」的重試語意、保留明帶 service_name/port、保留把 addon 移出 values。
- `db_init` 與 `admin` 退回 **chart bootstrap initContainer**（prototype 已實測的唯一配置，也是盤點 §3.3 的建議）。這同時解掉地基假設 1、併發鎖、detached exec 三態、以及 conf Secret 的時序問題——因為 initContainer 天然序列化、天然在主容器之前、失敗就是 pod 不 Ready。
- provision 只留「pod 已健康之後才有意義」的階段：`addons`、`module_upgrade`、`smtp`。這三個本來就需要一個活著的 Odoo。
- 把 conf Secret 的 PUT 明確排在 `create_namespace` 與 `create_release` 之間，並在文件裡寫死這個順序。
- 先花半天把兩件事量掉再談估算：(a) 空 DB 下 `/web/health` 與 readinessProbe 的實際行為（且要帶著 conf 裡有 `db_name` 的情況量）；(b) 官方 image 裡到底有沒有 rsync（我已從 Dockerfile 反證沒有，但值得一個 `docker run --rm odoo:18.0 which rsync` 落實）。這兩格的答案會直接改寫責任分配表的一半。

#### 致命傷與嚴重缺陷

**1. 🔴 fatal**

- **設計的主張**：失效2：「helm_value_specs 全部改成語意鍵（admin.email／smtp.*／demo_data），由平台的 conf renderer／provision 階段消費，一個都不再直接落成 chart values」＋ chart 頂層 additionalProperties: false
- **為什麼會壞**：平台根本沒有「語意鍵」與「chart values」的分隔層，而設計也沒有新增一層。租戶送上來的值走 `template._filter_allowed_helm_values()`（spec 白名單）→ `_unflatten_dotpath_keys` → `_deep_merge(existing_values, ...)` → `service._apply_helm_values(merged)`（src/controllers/paas.py:3387-3388、:3673-3676），落進 `helm_values`；而 `_assemble_deploy_values` 的第一行就是 `values = self._strip_mcp_sensitive_values(self._helm_values_dict())`（src/models/cloud_service.py:1577）——helm_values 整份就是送去 helm 的 values。所以「spec 鍵不落成 chart values」在現行管線下不可能成立，除非另造一個 tenant-answers 欄位與整條 reconfigure 路徑，而 platform_changes 沒有列這件事。
- **具體情境**：租戶在建立表單填 Administrator Email／SMTP Server → helm_values 出現頂層 `admin`、`smtp` 鍵 → `helm install` 被 values.schema.json 的 additionalProperties:false 當場擋下 → 每一台 odoo cloud_service 都建不起來。反過來若為了讓它過而放寬 schema，`smtp.password` 就明文長期留在 helm_values 裡，直接違反設計自己的「憑證一格都不進 helm values」與硬約束。

**2. 🔴 fatal**

- **設計的主張**：失效1b：「`cloud_app_odoo` 設 `has_admin_gui=True`（C007 指出它現在是預設 False，整條鏈對 Odoo 從沒生效）」，密碼走 `odoo_orm` 載體、只經 exec stdin
- **為什麼會壞**：`_build_mcp_sensitive_values`（src/models/cloud_service.py:2175-2207）只以 `if not self.template_id.has_admin_gui: return {}` 當閘門，**完全不看 `admin_password_carrier`**；閘門一開它就回 `{'config': {'sensitive': {'admin_password': ...}}}`，由部署／升級呼叫點當 `sensitive_values` 併進 helm（src/models/cloud_service.py:1462-1463）。新 chart 的 values 骨架沒有 `config` 這個頂層鍵。設計只提「secret 缺席時改成 raise」，沒有提要把注入本身關掉。
- **具體情境**：seed 把 has_admin_gui 打開 → 每一次 install／upgrade 都多送一段 `config.sensitive.admin_password` → 撞 additionalProperties:false → helm 直接失敗，連第一台都裝不起來；若為了讓它過而在 schema 裡宣告 `config`，就等於把 admin 密碼送進一個沒人消費的鍵，回到 C014a「UI 成功、沒人消費」的同一個形狀，而設計正是宣稱要消滅它。

**3. 🔴 fatal**

- **設計的主張**：chart_shape 第 6 條：「postgres.auth 只接受 existingSecret，不接受字面 password —— 從 schema 層杜絕『密碼寫進 values』」
- **為什麼會壞**：設計沒有指派任何人產生那個 Secret。chart 被 schema 禁止產生（不收字面密碼就無從 render Secret）；平台依 CLAUDE.md 不碰 kubectl；operator 端 platform_changes 只寫了「`patch_secret`（helm.py:2725）補 create-if-absent」，那是給 conf Secret 用的，沒有 postgres 憑證的鑄造與寫入流程。而 `_render_odoo_conf` 又必須把 `db_password` 寫進 conf（否則官方 entrypoint 會改走 `--db_password <明文>` 進 argv），代表平台其實必須持有這個密碼並讓它與 Secret 同值——這條雙寫的同步流程也沒有被描述。
- **具體情境**：fresh install：chart render 出參照 `existingSecret` 的 Deployment，但 namespace 裡沒有那份 Secret → pod 卡 `CreateContainerConfigError`／`ContainerCreating` → `_is_container_ready`（src/models/cloud_service.py:2700-2715）永遠 False → 撐滿 `deploy_timeout_minutes`（預設 30 分，src/models/cloud_service.py:355）→ 服務寫成 `error`，而 `error` 不在任何 poll 的 domain 裡、沒有東西救得回來。

**4. 🔴 fatal**

- **設計的主張**：「階段序：conf → db_init → admin → addons → module_upgrade → smtp → done」，由平台既有的狀態機驅動；chart 只用 extraVolumes／extraVolumeMounts／extraEnvVars 把 conf Secret 掛進去
- **為什麼會壞**：provision 是 post-deploy init，只在 `_is_release_ready`（含 `_is_container_ready`）為真之後才跑（src/controllers/paas.py:4321 的 odoo 分支、src/models/cloud_service.py:3592）。但 pod 要 Ready 就得先把 conf Secret 掛起來 ⇒ 「conf 由 provision 第一階段建立」是循環相依。實務上 namespace 在 `client.create_namespace()`（src/models/cloud_service.py:1434）就先建好了，所以正解是在 create_namespace 與 create_release 之間先 `PUT /odoo/config`——但設計文本沒有這一條，而且那樣一來 `conf` 階段的 revision 比對也失去了它宣稱的意義。
- **具體情境**：兩種收尾都壞。(a) Secret 掛載非 optional：pod 永遠起不來 → 30 分 timeout → error。(b) 為了讓它起來把掛載設成 `optional: true`：ODOO_RC 指到不存在的檔，官方 entrypoint 的 `grep -q ... "$ODOO_RC"` 失敗於是照樣用 env 補 DB 參數、Odoo 則退回 image 內建預設（odoo/docker 18.0 odoo.conf：`addons_path = /mnt/extra-addons`、`list_db` 預設 True、`admin_passwd` 未設、無 proxy_mode）⇒ helm 綠燈、pod Ready、租戶登得進去，但 `/web/database/manager` 對外開著、master password 是預設值、Cloudflare tunnel 前面沒有 proxy_mode、選的 addon 一個都不在 addons_path 裡。這是整份設計裡最大的靜默失效，而且它正好發生在設計聲稱被結構性擋掉的那一格。

**5. 🔴 fatal**

- **設計的主張**：失效4：module_upgrade 以 `patch_deployment` replicas→0 → 一次性 upgrade pod 跑 `-u <list>` → replicas→1；「平台必須把狀態寫成 `upgrading` 而非 `running`」
- **為什麼會壞**：`upgrading` 正好是 `_TIMEOUT_GOVERNED_STATES = ('deploying', 'upgrading')`（src/models/cloud_service.py:391）的成員，預算是 `deploy_timeout_minutes`（預設 30 分，:355）。scale 到 0 之後 `_is_container_ready` 回 `HARD: no pods found for this release`（:2707），也就是整個升級窗口都被判定成「未就緒」。另一半更糟：`_rescale_halted_after_upgrade`（:996）的存在本身就證明「任何 helm upgrade 都會把 replicas 依 chart 預設渲染回去」——設計替 module_upgrade 加了第二個 replicas 擁有者，卻只處理了「停權中的服務不得被喚醒」這一個方向。
- **具體情境**：(a) 大 DB／多模組的 `-u` 超過 30 分鐘 → 服務被寫成 `error`，同時停在 replicas=0，租戶全黑且沒有任何 poll 會回來救它。(b) 升級窗口內任何 reconfigure／改網域／plan 變更進來（走 helm upgrade，reset_values=True）→ replicas 被渲染回 1 → 主 pod 與 upgrade pod 搶同一顆 RWO PVC（主 pod Pending），或更糟：兩個 Odoo process 同時對同一個 DB 跑 schema 變更。

**6. 🟠 serious**

- **設計的主張**：形狀差異：`_build_expose_config` 改成規則陣列，含 `/websocket`→8072 那一條；fail_loud 是「conf renderer 在 workers>0 時強制檢查 expose 規則裡有 /websocket，沒有就 raise」
- **為什麼會壞**：expose 只在 fresh install 走。`_build_expose_config` 的 docstring 自己寫著「僅 fresh install 用，upgrade endpoint 無 expose 參數，升級路徑不用此值」（src/models/cloud_service.py:1658-1668），而 `upgrade_release`（src/services/paas_operator.py:378-388）確實沒有 expose 參數。而 workers 是**plan overlay** 決定的（C015，`cs_odoo_workers`），plan 變更走的正是 upgrade 路徑 ⇒ 規則永遠不會補上。設計的 fail_loud 也救不了：renderer 是純函式，看不到 Cloudflare 的現況；它只能檢查自己剛組出來的那個陣列，那永遠為真，零鑑別力。
- **具體情境**：租戶從 2 workers 的方案升到 4 workers（或平台把 workers 從 0 調起來）→ conf 的 workers 生效、pod 滾完、helm 綠燈、平台狀態 running → 但 Cloudflare 仍只有一條指向 port 80 的 catch-all → `/websocket` 打到 8069 回 500（prototype 已實測 8069→500、8072→101）→ 該租戶 Odoo 的即時通知、Discuss、longpolling 全部壞掉，平台一路沒有任何訊號。

**7. 🟠 serious**

- **設計的主張**：失效5：「把清單從 /all-addons/ rsync 到 /var/lib/odoo/extra-addons/，--delete 掉不在清單內的目錄」；image_delta 明文只做四件事、⛔ 不准加別的
- **為什麼會壞**：官方 `odoo:18.0` 沒有 rsync。已核對 odoo/docker 18.0 Dockerfile：base 是 `FROM ubuntu:noble`，apt 安裝清單（Dockerfile:12-38）只有 ca-certificates／curl／dirmngr／fonts-noto-cjk／gnupg／libssl-dev／node-less／python3-* 系列／xz-utils，另在 :53-63 補 postgresql-client（這解釋了 prototype 為何能跑 pg_isready／psql）。rsync 不在其中。而 image_delta 又把「只做四件事、不准加第五樣」寫成取向 C 的分界線與檔頭註解。
- **具體情境**：provision 的 `addons` 階段 exec `rsync` → `command not found`（exit 127）→ 每一台有選 addon 的 odoo 都卡在 addons 階段 → 5 次 failed → error。要修就得改 `Dockerfile.cloud` 重 build 並撞 prod `/approve` gate——正是設計宣稱要把慢 gate 壓到「一季一兩次」的那個東西，在第一次上線就先撞一次。

**8. 🟠 serious**

- **設計的主張**：失效1a 的 fail_loud (b)：「`_render_odoo_conf` 渲染後先自我驗證（必要鍵全在、addons_path 每一段在 image 內存在），缺一個就 raise UserError」；同時宣稱它是純函式
- **為什麼會壞**：自相矛盾且不可實作：平台的純函式沒有租戶容器的檔案系統，無從驗證 image 內的路徑存在。另外這條檢查本身建立在一個錯誤前提上——官方 image 的預設 conf（odoo/docker 18.0 `odoo.conf`）只有 `addons_path = /mnt/extra-addons`，完全沒有列 core addons 路徑，卻是官方唯一支援的組態，證明 Odoo 自己從套件路徑找 core addons、不必寫進 addons_path。設計堅持顯式寫出 `/usr/lib/python3/dist-packages/odoo/addons`（且 open_questions 11 自承未實機 `ls` 過）是憑空多引入一條會寫錯的路徑。
- **具體情境**：這條 fail_loud 落地時只會退化成「必要鍵在不在」的字串檢查，addons_path 那半條做不到 → 顯式寫錯 core 路徑（各 dated tag 的 dist-packages 佈局若有差異）→ conf 寫得出來、pod 起得來、`/web/health` 綠燈，但 core addons 找不到，Odoo 在第一次載 registry 時整個炸掉或 marketplace 的 addon 全部 uninstallable。設計把一個「不存在的保護」寫進了裁決依據。

**9. 🟠 serious**

- **設計的主張**：「`POST /odoo/provision`：冪等、每次只推進一個階段、每次都在 30s 內回」，由平台既有狀態機驅動
- **為什麼會壞**：沒有任何鎖或 CAS，而第一個階段偏偏是不可重入的 `-i base`。`_get_service` 在 `deploying/upgrading/initializing` 時會呼叫 `_update_service_status` → `_run_post_deploy_init`（src/controllers/paas.py:3593-3595、:4200），而這條路是**前端 poll 5 秒一次、cron 60 秒一次**（`_is_container_ready` docstring 明載，src/models/cloud_service.py:2676），且每個開著服務頁的瀏覽器分頁各打各的、model 與 controller 兩份實作並存。今天這條路做的是冪等的 module list refresh，所以競態無害；bootstrap 不是。
- **具體情境**：租戶開兩個分頁看部署進度（或前端 poll 與 cron 撞在同一秒）→ 兩個請求都讀到「sentinel 不存在」→ 各自 detach 一個 `odoo -d $DB -i base` → 同一顆空 DB 上兩個 base 安裝並行寫 schema → DB 進入半初始化且無法判定的狀態，而 sentinel 只會記到其中一個 pid。

**10. 🟠 serious**

- **設計的主張**：「`db_init` 用 detached exec（`setsid nohup`）起 `odoo -d $DB -i base`，把 pid／結束碼／起始時間寫進 PVC sentinel」，並把 exec 當成七個階段共用的主要供給通道
- **為什麼會壞**：官方 image 的容器 PID 1 就是 Odoo server（ENTRYPOINT 最後 `exec odoo "$@"`），不是 init：不收屍、不轉發訊號。`kubectl exec` 起的 process 在連線關閉後被 reparent 到它 ⇒ (a) 結束後變 zombie 永遠留著；(b) livenessProbe 重啟、OOM、node drain、helm upgrade 滾 pod 都會連帶殺掉它，sentinel 落在「有 pid、沒有結束碼」的第三態；(c) 第二個完整的 Odoo process 與主 process 共用同一個 container memory limit（設計沿用 2Gi），`-i base` 的尖峰很可能觸發 cgroup OOM，而被 kernel 挑中的未必是它。設計把這整條只列成 open question，但它是七個階段的共同執行模型，不是一格待確認。
- **具體情境**：`-i base` 跑到一半 pod 因 OOM 重啟 → sentinel 顯示「pid 存在但 process 不在」→ 若判成「還在跑」服務永遠 pending（而 pending 不計 retry、`initializing` 又不在 age timeout 內 ⇒ 永久卡）；若判成「跑完成功」則接著在半初始化的 DB 上跑 admin／addons 階段，之後每一步的錯誤訊息都會指錯方向。

**11. 🟠 serious**

- **設計的主張**：「同一套設計同時服務租戶 odoo cloud_service 與平台 Odoo」——共用 chart ＋ image ＋ conf 渲染邏輯，只有驅動層不共用
- **為什麼會壞**：chart 那一半也不共用。設計給的 values 骨架沒有 ingress、networkPolicy、sidecars、nodeSelector／tolerations／affinity、PDB、serviceAccount、replicaCount、多 host；而平台自身現在用到的至少有 networkPolicy（C100 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:973`）、ingress extraPaths／extraRules（C107 `:294`）、extraContainerPorts／service.extraPorts（C105／C106 `:234,243`）、三支 initContainer（C098 `:428,802,938`）、扁平 probe 鍵（C108 `:943`）、備份與 operator netpol 依賴的 label（C085／C086／C087）。頂層 `additionalProperties: false` 會讓這份 values 一條都塞不進去。
- **具體情境**：真要同時服務兩者，只有兩條路：(1) 平台 Odoo 另開一支 chart —— 那共用物只剩 image，任務的硬約束當場不成立；(2) schema 開一堆逃生門（extraObjects／passthrough）—— 那 fail-loud 的整個立論垮掉，而 fail-loud 正是這份設計相對 prototype 路線的唯一賣點。設計自陳「只做到一半」，但它把缺口定位在驅動層，實際缺口在 chart 本身。

**12. 🟠 serious**

- **設計的主張**：失效1a 的 fail_loud (c)：「Secret 上壓 `woow.tech/conf-revision` annotation ＝ 內容 sha256，pod template 的 podAnnotations 帶同一個值，provision 的 conf 階段比對兩者 —— 『Secret 改了但 pod 沒重啟』這個經典靜默失效被結構性擋掉」
- **為什麼會壞**：比對的兩個值都是平台在同一次渲染算出來的，且比的是 **Deployment 的 podTemplate**，不是跑著的 pod。helm upgrade 成功時兩者必然相等（這個檢查是恆真的），唯一抓得到的是「PUT 成功但 helm upgrade 沒做／失敗」這一種。它抓不到真正會發生的那幾種：Deployment 已更新但 pod 還是舊 ReplicaSet、`updateStrategy: Recreate` 下新 pod 起不到（PVC 還被舊 pod 掛著、image pull 失敗）、rollout 卡住。要證明「跑著的 process 真的讀到新 conf」只能進 pod 讀 `/woow/conf/odoo.conf` 或比對 `/proc/1` 的啟動時間。
- **具體情境**：租戶改了 SMTP 或平台改了 workers → PUT 寫新 Secret、helm upgrade 回 200（Deployment spec 已更新）→ Recreate 策略下新 pod 因 PVC 尚被 Terminating 的舊 pod 佔住而 Pending 數分鐘 → provision 的 conf 階段比對 Deployment 與 Secret 兩個相等的 annotation → 回 `done` → 一路走到 `running`，但服務跑的仍是舊 conf。這正是這條宣稱要結構性擋掉的那個失效。

**13. 🟠 serious**

- **設計的主張**：「憑證一格都不進 helm values 也不進 Deployment 的明文 env」＝ 憑證安全不變式重建完成；args 的 `not: {pattern: "^--db_password"}` 保證憑證不進 argv
- **為什麼會壞**：conf Secret 是以 readOnly 掛進**主容器**的，而 Odoo 的管理員就是租戶本人，管理員可以用 `ir.actions.server` 的 Python code 讀取容器內任意檔案 ⇒ `db_password`、`smtp_password`、`admin_passwd` 對租戶仍然可讀。真正被這個設計改善的只有「不經平台 API 明文回傳」，而那一點今天的 #535 spec-driven 遮罩（src/tests/test_credential_mask_spec_driven_535.py）與 #559 欄位 ACL 已經做到。argv 那條規則也只約束平台自己：官方 entrypoint 的 `check_config` 是 `if ! grep -q "^\s*\b${param}\b\s*=" "$ODOO_RC"` 才補 argv——所以擋住 argv 的其實是「conf 裡必須有 db_password」這個隱性條件，不是 schema 規則；conf 漏寫 db_password 時 entrypoint 會自己把明文塞進 argv，而 schema 規則對此無能為力。
- **具體情境**：若平台 SMTP 是共用 relay（設計沒有排除這個情況），任一租戶的 Odoo 管理員即可從 conf 讀出 relay 憑證 ⇒ 用平台的寄信身分發信。另一路：renderer 某次沒寫 db_password（例如 postgres Secret 尚未就緒的降級分支）→ `--db_password <明文>` 進 argv，pod 內任何 `ps` 都看得到，而所有 schema 檢查都是綠的。

**14. 🟡 minor**

- **設計的主張**：「operator 新增 `PUT /api/releases/{ns}/{rel}/odoo/config`，把文字寫成 K8s Secret（`patch_secret`，helm.py:2725，需補 create-if-absent）」
- **為什麼會壞**：兩個具體缺口。(1) `patch_secret` 對每個 value 跑 `self._is_base64(v)` 啟發式（helm.py:2747、:2791-2798），對「內容剛好通過 base64 round-trip」的字串會跳過編碼——把一整份租戶可影響內容（smtp_user、db_name、addons_path）的文字灌進去時，這是一條由資料決定行為的分支，不該留在憑證路徑上。(2) 這個 Secret 不是 helm 管的，`helm uninstall` 不會刪；operator 的 uninstall 路徑只按 `app.kubernetes.io/instance` 撈 PVC（extra/paas-operator/src/api/releases.py:356）。設計對 chart 的 PVC label（C066）要求得很細，卻對自己新造的 Secret 沒有任何生命週期安排。
- **具體情境**：每刪一個 odoo 服務，namespace 裡就留下一份含 db_password／smtp_password 的孤兒 Secret，沒有任何清理路徑也沒有告警——與 C066 同一個形狀（持續累積、永遠不會被發現），只是這次累積的是憑證而不是儲存成本。

**15. 🟡 minor**

- **設計的主張**：effort_days：合計 24～34 人日，「與盤點 §1.2 的 P0＋P1（23～38）同量級，但 chart 更輕、operator 更重」
- **為什麼會壞**：工作量嚴格大於盤點的 P0+P1，估值卻更低。盤點的 P0+P1 假設 bootstrap 由 chart initContainer 負責（§3.3），取向 C 則額外要做：7 階段 provision、detached exec + sentinel 三態、一次性 upgrade pod、`ExposeConfig` 的破壞性改版（shared，影響 15 個 template）、`create_route` 多規則（shared）、operator 重新發版（blast radius 涵蓋 n8n／hermes-agent／HAOS 全部租戶），外加設計自承「沒有完整算進去」的 controller／model 兩份 post-deploy init 收斂。chart 的 3～4 人日又要同時含獨立 postgres（pgvector、existingSecret、PVC）、values.schema.json 六條約束、render test，以及平台 Odoo 的需求集合。
- **具體情境**：以 24～34 人日排程 → 在 operator（無 integration CI）與 chart 兩個最容易超時的區塊同時失準 → 因為發版順序是硬相依（image → chart → operator → seed），任一環延誤後面全部順延，而每一次 `Dockerfile.cloud` 的來回都要再撞一次 prod `/approve` 人工 gate。

#### 設計裡沒有證據支撐的宣稱

- 【地基 1】官方 odoo:18.0 在 DB 尚未 init 時 `/web/health` 回 200、容器維持 Ready。設計自己列為 open question 1，但嚴重性被低估：prototype 的所有實測都是用 initContainer 先建好 DB 才讓主容器起來（`assets/1136/values-official.yaml` 的 bootstrap initContainer），這一格是零證據，而不是「沒覆蓋到」。而且設計會在 conf 裡寫 `db_name`，主 process 對著一個已指定但未初始化的 DB 的行為（registry load 失敗會不會拖垮 nodb route）比「完全沒有 -d」更未知。不成立則整個驅動層作廢。
- 【地基 2】detached exec（`setsid nohup`）在 kubectl exec 連線關閉後存活，且 sentinel 能分辨「還在跑／跑完成功／跑到一半被殺」三態。零實測。而官方 image 的 PID 1 是 Odoo server（ENTRYPOINT 最後 `exec odoo`）、不是 init，這會直接影響收屍與訊號轉發。
- 官方 image 內有 rsync。**我已從 odoo/docker 18.0 Dockerfile 反證：沒有**（apt 清單只到 postgresql-client）。設計對這個核心動作沒有給任何證據。
- `chown -R odoo /all-addons` 一行就夠。設計沒有說明現行 `copy-addons.sh` 的 COPY 發生在 `USER odoo` 之前或之後、以及 catalog.json 的 owner——官方 image 在 `USER odoo` 之後的 COPY 預設 owner 仍是 root，順序錯了這行 chown 會在錯的時機執行。
- `/usr/lib/python3/dist-packages/odoo/addons` 在 18.0 各 dated tag 之間穩定。設計自承是從 Dockerfile 推得、未實機 `ls`。而官方預設 conf 根本不列 core 路徑（`addons_path = /mnt/extra-addons`），代表這整條需求可能本來就不存在。
- `values.schema.json` 的 `additionalProperties: false` 擋得住「所有殘留鍵」。沒有人盤過平台實際送進 helm 的 values 全集：`_inject_image_pull_secrets`（`cloud_service.py:1850`）是**無條件**對所有 template 注入 `global.imagePullSecrets`；`_inject_storage_class`、`_inject_allowed_origins`、`_inject_dashboard_public_url`、`_inject_apiserver_cidrs` 是 slug-gated；operator 的 `_ensure_image_pull_secrets`（`releases.py:128`）還會 setdefault 頂層鍵。這六條注入器對新 chart schema 的相容性需要逐條驗過，否則 fail-loud 會在第一天就變成「所有 odoo 都裝不起來」。
- conf 檔必須有 `[options]` section header。`wait-for-psql.py` 直接 `config.items('options')`，沒有這個 header 會拋 `NoSectionError` 而不是 fall back ⇒ 容器在 Odoo 啟動前就退出。renderer 的「必要鍵檢查」清單裡沒有它。
- cloudflared 的 ingress path 規則嚴格依陣列順序匹配，且同一 hostname 可以有多條。設計自承待確認。另外沒被提到的：`create_route`（`cloudflare.py:892`）是對**全域 tunnel config** 的 read-modify-write（`:948-975` 還要 pop/append catch-all），沒有樂觀鎖；同一 hostname 兩條規則會把寫入窗口拉長，並行建立服務時後寫覆蓋先寫的機率上升。
- 在主 process 活著時跑 `-i <new addons>` 是安全的。設計對 `-u` 要求 scale-to-0（理由是「在主 pod 內 exec 跑 -u 會與服務中的 process 競爭同一個 DB」），卻讓 `addons` 階段在同一顆活著的 pod 裡跑 `-i`——`-i` 同樣會改 schema、載 XML data、觸發 registry reload。兩者風險為何不同，設計沒有給理由也沒有證據。
- 平台自身 Odoo（Fleet GitOps，`paas-app-bundle-cd`）能用同一支 `odoo-runtime` chart。設計自陳「驅動層不共用」，但 chart 那一半也沒有驗證過——見 fatal 清單第 11 條列出的 values 鍵。
- operator 能從本機 chart 目錄安裝。cross_repo_friction 建議「在 dev sandbox 用本機 chart 目錄 ＋ stg image 把四環先串起來驗完」，但 operator 在叢集內跑、`ReleaseCreateRequest.chart` 的描述是 `repo/chart or path`（`schemas.py:59`）——那個 path 是 operator 容器內的路徑，不是開發機的。這條驗證捷徑能不能成立沒有被查證，而發版順序是硬相依，它不成立的話每一環都要各等一次 gate。
- `_assess_addon_removal`（`src/controllers/paas.py:2213`）的 fail-closed 保護足以讓 rsync `--delete` 安全。該函式只守「租戶主動取消勾選」這一條路徑，而 `addons` 階段會在每次 redeploy／image 升版時無條件重跑；若 `odoo_addon_ids` 因任何原因（migration、資料修復、catalog 不一致，C010 vs C058）與 DB 實況不符，`--delete` 會在 `initializing` 期間直接刪掉仍被標記為 installed 的模組目錄 ⇒ 下一次 registry load 整個 DB 起不來。設計自己點名 catalog 一致性會被 `--delete` 放大，但沒有把「provision 路徑不經過 `_assess_addon_removal`」這件事算進去。

#### 站得住、值得採納的部分

**一、最有價值的一條，我逐行核對過、確實成立，而且盤點沒點名：**

`init_odoo` 的 client 端預算是 `HELM_OPERATION_TIMEOUT = 120`（`src/services/paas_operator.py:713`，常數在同檔 `:17`），而 operator 端 `odoo_init.py` 的 `_wait_for_ready` 自己就是 `MAX_READY_RETRIES=30 × READY_RETRY_DELAY=5` ＝ 150s（`extra/paas-operator/src/services/odoo_init.py:23-24`，呼叫在 `:86`）。Bitnami 下 DB 在 Ready 前就建好、這支幾乎立刻回，所以從沒被撞到；換官方 image 後必炸。這與 #459/#461 在 reset 路徑踩過的是同一個形狀（`src/services/paas_operator.py:18-21` 就是那次留下的階梯註解）。**這一條單獨就值得開票，與取向選擇無關。**

**二、對 C047 的診斷正確，而且連鎖推得對：**

`_record_init_failure`（`src/models/cloud_service.py:3607-3627`）現在把「還沒好」和「壞了」算成同一件事，5 次是時間預算不是失敗預算。設計提出「只有 failed 才計 `init_retries`」是對的方向；更重要的是它自己推出了後果——`_TIMEOUT_GOVERNED_STATES = ('deploying','upgrading')`（`:391`）刻意不含 `initializing`，`init_retries` 是它唯一的上界，拿掉計數就會出現永久卡的服務。這個連鎖是真的。（補法與 #489 的既有裁決正面衝突，那段註解明寫「兩套機制夾擊同一個狀態只會讓失敗歸因更難」，且 `_is_public_url_ready` 的 DNS 寬限窗被 clamp 在同一把尺上——要改就得重新論證那條不變式，不能只加一個新參數。）

**三、對 prototype 的事實更正是對的：**

prototype §3 說官方 `/etc/odoo/odoo.conf` 是 root 所有。實際 odoo/docker 18.0 `Dockerfile` 有 `RUN chown odoo /etc/odoo/odoo.conf`。設計的更正成立。

**四、conf Secret ＋ `ODOO_RC` 這個核心裝置，是整份設計裡唯一被上游原始碼直接支撐的部分：**

官方 image `ENV ODOO_RC=/etc/odoo/odoo.conf` 是明訂介面；entrypoint 的 `check_config` 以 `if ! grep -q "^\s*\b${param}\b\s*=" "$ODOO_RC"` 判斷是否要補 argv，`wait-for-psql.py` 也自己 `configparser.read(os.environ['ODOO_RC'])`。所以覆寫 ODOO_RC 確實能一次接管 Odoo 與 entrypoint 兩邊的 DB 參數，而且 conf 裡寫了 db_password 時 entrypoint **不會**把它塞進 argv。這個機制站得住，值得保留。

**五、其餘站得住的判斷：**

- 用 ORM 改 admin 密碼取代 `UPDATE res_users ... WHERE id=2`（現行 seed `src/data/cloud_app_templates.xml:242` 的 `fix-admin-credentials.sh`），prototype t1136b 已實測可行。
- SMTP 雙軌的理由正確：Odoo 18 把 conf 的 `smtp_ssl` 當 STARTTLS（`ir_mail_server.py:441-442`，盤點 §6 第 6 點已查證），conf 層沒有 implicit TLS 的表達力，`smtpProtocol=ssl` 只能走 `ir.mail_server` record。
- 把 addon 從 helm values 挪走、讓 `_build_odoo_addon_helm_values`（`src/models/cloud_service.py:1671`）／`_remove_addon_helm_values`（`:1744`）／C004 的 image_ref 三段式組裝（`:1639-1649`）一起變成死碼，方向正確——C018 的拔除邏輯確實是「因為存成 values 陣列」才存在的問題，換成宣告式目標目錄就不需要它。
- 明帶 `service_name`／`service_port` 取代自動偵測：`extra/paas-operator/src/api/releases.py:1591-1592` 確實是「有值就跳過偵測」，這條可以今天就做、零風險。
- `values.schema.json` ＋ chart render test 把 C005／C006／C066／S1 這類靜默失效轉成 install 當場失敗，方向正確——但它與「語意鍵」是互斥的（見 fatal 1）。
- `_strip_mcp_sensitive_values`（`:2216`）與 controller `_sanitize_helm_values` 兩邊都要補新敏感路徑：這個提醒是對的，那段 docstring 明寫是 path-aware、#530 踩過。


## 4. 完整度 critic：三份都漏掉的

### 4.1 沒有人接手的缺口

#### harumi 整份移植需求在三份設計裡「一次都沒出現」——三份都只設計 greenfield bootstrap，沒有「匯入既有 Odoo DB」這條路

- **為什麼重要**：harumi 正是這次換 lineage 的起因：耦合盤點 §0 與 harumi 計畫 §0.1 明載「路線 C 升格」的理由就是讓 harumi 與叢集上其餘 16 站共用同一套 template 與 runbook。但三份設計的 bootstrap 判準一律是「DB 空 → -i base；DB 非空 → 跳過」，而 pg_restore 進來的 harumi DB 是第三態：121 個模組（image 的 /all-addons 只有 3 顆）、有沒有 git 出處的客製 addon、admin 不保證是 base.user_admin/uid 2、web.base.url 指向舊 hostname、且完全沒有 A/B 用來記帳的 woow.image.release 之類 config parameter。A 的 upgrade gate 對這顆 DB 會判成「沒變、跳過」（code 與 schema 脫節）或「首次」（對有真實資料的 DB 跑 -i base/-u all）；B、C 同樣沒有定義。沒有一份提供 skip-bootstrap／import 模式的旗標。
- **該由誰負責**：平台（新增 import／skip-bootstrap 語意與 runbook）＋決策者：這是「新 template 是否必須同時是搬遷載具」的產品決策，harumi 是營業中的 POS+ECPay 站
- **證據**：harumi 計畫 §3 D1「沒有匯入既有 DB 的機制」；§3 A 表 A1～A10（core、pgvector、role、DB 名、filestore 擁有者、database.uuid/secret）；§3 C 表 C1「/mnt/extra-addons 沒有 git 副本」。三份設計全文搜尋 harumi 零命中，只在 cross_repo_friction 把「叢集上 17 個 standalone odoo 站」當作拿不到 chart 原始碼的理由。

#### 租戶自訂 hostname：三份都只回 subdomain，而 harumi 的硬需求是保留 harumi-odoo.woowtech.io

- **為什麼重要**：`custom_domain` 是純 bookmark 欄位（`src/models/cloud_service.py:627` 只有 help 字串、沒有任何消費者），`_build_expose_config`（`src/models/cloud_service.py:1658-1669`）只回 `{'enabled': True, 'subdomain': ...}`。A、B 只把它擴成明帶 service_name/service_port，C 擴成規則陣列，三份都沒把 hostname 變成可設定的東西。harumi 的桌面 QR code（`/pos-self/<id>?access_token=…`）與 ECPay 回呼都綁 hostname，D5 標為 blocker。這件事要 chart／operator／平台三方一起改（tunnel ingress rule 的 hostname 來源在 operator），現在沒有任何一方認領。
- **該由誰負責**：平台（欄位語意與 UI）＋operator（`extra/paas-operator/src/services/cloudflare.py` 的 create_route 與 reconcile 孤兒判定）；需先裁決「自訂網域是不是這次的範圍」
- **證據**：`src/models/cloud_service.py:627`（custom_domain 僅 bookmark）、`src/models/cloud_service.py:1658-1669`（expose 只讀 subdomain）；harumi 計畫 §3 D5「必須保留原 hostname」＋「平台沒有對應機制」

#### 備份：租戶 cloud_service 今天完全沒有備份機制，而換 chart 是唯一能把它做進 chart 的視窗——三份 chart 都沒有 backup CronJob，三份設計全文沒有出現「備份」二字

- **為什麼重要**：`grep -ni backup src/models/cloud_service.py` 只命中一行無關註解；operator 側也查無租戶備份。harumi 現況是每日 03:30 `pg_dump -Fc` 到同叢集 backup PVC（7.9MB／35 秒），搬進平台等於退化。harumi 計畫把路線 C 的賣點之一明寫成「chart 含備份」，而三份的 chart_shape 頂層鍵表（A 19 鍵、B 12 鍵、C 骨架）沒有一個有 backup。B、C 又同時宣告逐層 `additionalProperties:false`，代表事後也補不進去。
- **該由誰負責**：chart（CronJob 與目的地 PVC）＋平台（保留期與租戶可見的還原動作）；#512 只是同型旁證，本缺口本身沒有 issue
- **證據**：`grep -rni backup src/models/cloud_service.py` 僅 `:1186` 的無關註解；harumi 計畫 §2 對照表「備份」列（來源有 CronJob、PaaS **無**）與 §3 D6（並註明「也沒有專門追蹤這個缺口的 issue」）；路線 C 推薦理由「chart 含備份」

#### postgres PVC 從 StatefulSet volumeClaimTemplate 變成 chart 顯式 PVC，會被 operator 的 post-renderer 蓋上 `helm.sh/resource-policy: keep`——刪除／重裝語意與 PGDATA 密碼重用三份都沒算

- **為什麼重要**：`extra/paas-operator/src/pvc_keep_post_renderer.py:31-38` 只對頂層 `kind: PersistentVolumeClaim` 蓋 keep，不碰 volumeClaimTemplates。Bitnami 時代 DB PVC 來自 STS 樣板，post-renderer 看不到；三份都把 postgres 改成 Deployment＋顯式 PVC，於是 (a) `helm uninstall` 之後客戶的**整顆資料庫**留在叢集，回收唯一途徑變成 C066 的孤兒 PVC 延遲清理（prod 90 天 grace），這是資料保留與成本政策的改變，沒人裁決；(b) 同名 release 重裝會接回一顆已用**舊密碼**初始化的 PGDATA，而三份的 postgres 密碼來源各不相同（A `config.sensitive`＋PASSWORD_FILE、B chart `randAlphaNum`＋`lookup` 自產、C 要求 `existingSecret` 但沒有任何一方產生它），沒有一份定義密碼如何跨 PVC 重用存活。#1136 是手動 helm 跑的（沒經過 post-renderer），所以這個形狀從未被觀察到。
- **該由誰負責**：operator（post-renderer 與 uninstall 的 PVC 分流）＋平台（刪除服務的資料保留政策）＋chart（postgres 密碼是否可重新推導）
- **證據**：`extra/paas-operator/src/pvc_keep_post_renderer.py:31-38`；C066 `extra/paas-operator/src/api/releases.py:356`；prototype §5「helm uninstall 會把 chart 管的 odoo PVC 刪掉（重裝時 PVC 還在 Terminating，pod 卡 Pending）」＝未經 operator 的對照組；harumi 計畫 §2「longhorn-delete ＋ resource-policy: keep ＋ prod 90 天 grace」

#### odoo 的 DB role 從「非 superuser」變成 superuser，三份都沒提；pgvector extension 由誰建也沒人認領

- **為什麼重要**：Bitnami 的 postgresql 子 chart 建的 `bn_odoo` 不是 superuser；#1136 換成 `pgvector/pgvector:pg17` 之後實測「`bn_odoo` 是 superuser＋createdb」——因為官方 postgres image 的 `POSTGRES_USER` 一律是 superuser。三份都只寫 `postgres.auth: {username, database}` 直接套官方／pgvector image，沒有一句談 role 權限。租戶就是自己 Odoo 的 admin，Odoo admin 能透過 server action 執行任意 Python／SQL ⇒ superuser role 等於租戶可在 postgres 容器上 `COPY … FROM PROGRAM`。另一半是：`vector` extension 非 trusted、建立需要 superuser（harumi A2 已查證），三份沒有一份說誰在什麼時機建它。這是換 lineage 順手丟掉的既有安全不變式，不是新增風險。
- **該由誰負責**：chart（建 role 時明確降權、extension 由 initdb script 以 superuser 預建）；平台需要一條「租戶 DB role 不得是 superuser」的驗收／測試
- **證據**：prototype §2 最後一列「`bn_odoo` 是 superuser＋createdb」；harumi 計畫 §3 A2「`vector.control` 沒有 trusted = true，`CREATE EXTENSION vector` 需要 superuser，`bn_odoo` 不是 superuser」；三份 chart_shape 的 postgres 區塊均無權限欄位

#### tenant-facing 的 `loadDemoData` 在三份設計裡都被接到一個實際不會被消費的鍵——正是三份都宣稱要消滅的 C014a bug class

- **為什麼重要**：seed 現在有租戶可見的 optional spec `loadDemoData`（`src/data/cloud_app_templates.xml:243`）。A 定義 `WOOW_BOOTSTRAP_LOAD_DEMO`、B 定義 `bootstrap.withoutDemo`、C 定義語意鍵 `demo_data`，但三份寫出來的 bootstrap 指令**一律寫死** `--without-demo=all`（A 機制③、B M4、C 的 `db_init` 階段）。結果：租戶勾了 demo data、helm 綠燈、pod Ready、沒有任何訊號，而 demo 資料不會載入。耦合盤點 §6 第 12 點已把「`--without-demo`／`--with-demo` 與 `loadDemoData` 的對應需要核實」列為待確認，三份都沒核實就把鍵留在租戶表單上。值得單獨點名，因為三份的核心賣點都是 fail-loud，而這是它們自己新造的靜默失效。
- **該由誰負責**：平台（決定這個租戶選項留不留）＋image/chart（若留，bootstrap 指令必須真的分支）
- **證據**：`src/data/cloud_app_templates.xml:243` 的 optional spec `loadDemoData`；耦合盤點 §6 第 12 點；三份設計的 bootstrap 指令字面（皆含 `--without-demo=all`）

#### 共用 `/all-addons` catalog 沒有 per-tenant 可見性，而本次決策（harumi 與 17 站共用同一 template）必然要把租戶專屬 addon 塞進共用候選池——三份都把 `/all-addons` 當「原封保留的唯讀倉庫」帶過

- **為什麼重要**：harumi 需要 `pos_self_order_enhancement`（無 git 出處）與 `payment_ecpay`（目前只在 `paas-addons.list`，不在 `cloud-addons.list`），以及 4 個不在現行 image 的 pip 套件（google-genai、markdown、pycryptodome、pycryptodomex）。把它們加進共用 `cloud-addons.list` 等於曝光給所有租戶（harumi C3/C4 明列，並註明「addon 層級沒有可見性控制」）。三份的 image_delta 都只說「`/all-addons/` 與 catalog.json 原封保留」，沒有一份談 addon 可見性、per-tenant image variant、或 pip 集合由誰決定與如何擴張。這直接關係到「同一套設計能不能服務 harumi」。
- **該由誰負責**：平台（CloudOdooAddon 的可見性模型）＋paas-odoo-ci（catalog 是否分層）；需裁決「租戶專屬 addon 進共用 image 還是走專屬 image」
- **證據**：harumi 計畫 §3 C3、C4、C5、C6；`src/models/cloud_odoo_addon.py:9` 的 `CATALOG_PATH`、`extra/paas-operator/src/services/odoo_scripts/update_module_list.py:48` 的 `MANAGED_CATALOG_PATH`（C010／C058 交界）

#### 新 chart 有兩個 workload（odoo＋postgres Deployment），三份都沒有替 postgres 宣告單副本／Recreate 不變式；A、B 連 odoo 的 `updateStrategy` 都從 values 契約裡漏掉

- **為什麼重要**：現行 seed 明寫 `"updateStrategy": {"type": "Recreate"}`（`src/data/cloud_app_templates.xml:242`），那不是裝飾，是 RWO PVC 上單副本 Deployment 的必要設定。C 的骨架有 `updateStrategy`，A 的 19 個頂層鍵與 B 的 12 個頂層鍵都沒有，而兩者又同時宣告全樹／逐層 `additionalProperties:false` ⇒ 平台事後補不進去。更普遍的是：三份沒有一份替**新增的 postgres Deployment** 指定 strategy 或 replicas 上界。RollingUpdate 預設下 maxSurge 會讓第二顆 pod 去 attach 同一顆 RWO Longhorn volume，新 pod 卡 Multi-Attach、舊 pod 仍 Ready，而 `helm install`／`upgrade` 都沒有 `--wait`（`extra/paas-operator/src/services/helm.py` 的 args 組裝不含 wait），`_is_container_ready`（`src/models/cloud_service.py:2658`）看到舊 pod 就回 True ⇒ helm 綠燈、平台 running、設定沒生效。
- **該由誰負責**：chart（兩個 workload 各自的 strategy 與 replicas 鎖死＋render test）
- **證據**：`src/data/cloud_app_templates.xml:242` 的 updateStrategy；`extra/paas-operator/src/services/helm.py` install/upgrade 參數組裝（無 `--wait`）；`src/models/cloud_service.py:2658` 起的 `_is_container_ready`

#### 訂閱停權／復權（scale 0↔1）與新 boot path 的互動，只有 C 在 module_upgrade 一處提到，沒有一份把它當成常態路徑設計

- **為什麼重要**：`scale_release`（`extra/paas-operator/src/api/releases.py:680-720`）以 `app.kubernetes.io/instance` 撈**所有** Deployment＋StatefulSet 一起 scale ⇒ 新 chart 下停權會連 postgres 一起停，復權時 odoo 與 postgres 同時回 1。三份都把 bootstrap（A、B 的 initContainer）或升級閘（C 的 provision）放在 pod 啟動路徑上，因此**每一次欠費復權都會重跑一次 bootstrap／版本比對，對著一顆冷啟動的 postgres**；A、B 又把大量情況設計成 exit 78。結果是「繳完錢按復權」變成一條有 outage 風險的路徑，而且沒有 rollback。另有 `_rescale_halted_after_upgrade`（`src/models/cloud_service.py:996`）這個既有的 replicas 擁有者，與 C 的 scale-to-0 升級正面相撞。
- **該由誰負責**：平台狀態機（停權／復權與 initializing 的關係）＋chart（bootstrap 在冷 DB 下的重試預算）
- **證據**：`extra/paas-operator/src/api/releases.py:680-720`（三種 workload 一律都查都處理）；`src/models/cloud_service.py:996` `_rescale_halted_after_upgrade`；harumi 計畫 §3 D7 ①⑥

### 4.2 三份設計互相矛盾之處（每一條都指向一個要人拍板的問題）

#### 新 image 版本如何傳到「既有」服務（模組升級閘的輸入）

- **三份各自的立場**：A：在 `_assemble_deploy_values` 新增 `_inject_odoo_image`，從 `ir.config_parameter` 注入 `image.tag`。B：改 seed 的 `helm_default_values.image.digest`，並明寫「刻意放 seed 不放 chart」。C：改 template 的 `helm_default_values.image.tag`（或 per-service 覆寫）。
- **這個矛盾其實在問什麼**：已驗證 `helm_default_values` 的非測試消費點只有 `src/controllers/paas.py:3353`（create）、`src/controllers/ha_paas.py:297`、`src/models/cloud_app_template.py:401`；upgrade 路徑的 base 是 `svc.helm_values`（`src/models/cloud_service.py:1577`，docstring 在 `:1551` 自陳是 create 當下的 merge 快照）⇒ B 與 C 的機制在既有服務上靜默 no-op，而升級閘偏偏靠它當輸入。真正要回答的是：**`svc.helm_values` 是 create 快照這件事要不要改？平台要不要有「平台管理鍵每次部署重新 coalesce」的概念？** 這不是三選一的偏好，是動工前必須先修的管線前置，由平台 owner 拍板。

#### plan 變更（cs_odoo_workers／cs_ram_gb／cs_storage_gb）如何套到既有服務

- **三份各自的立場**：A：「每次開機重 render conf 順手修掉 C015」。B：「形狀差異⑥附帶修好 plan 變更對既有服務不生效」。C：「`_render_odoo_conf` 取代 C015」。三份都宣稱解掉 C015。
- **這個矛盾其實在問什麼**：已驗證 `_apply_plan_spec_overlay` 全 repo 只有一個呼叫點：`src/controllers/paas.py:3419`（create），三份都沒動它 ⇒ 三份「修好 C015」的宣稱都不成立，只是把 create 期執行的一段程式碼換成另一段 create 期執行的程式碼。與上一條同根：**誰負責在 upgrade 路徑上重算衍生值？** 這個問題不回答，三份裡所有 workers>0 的連動守線（A 的 exit 78、B 的 `_helpers.tpl fail`、C 的 renderer raise）都永遠不會被觸發，因為 workers 永遠停在建立當下的值。

#### admin／db／smtp 憑證的供給通道

- **三份各自的立場**：A：沿用 `config.sensitive.*`（明說刻意不發明新路徑，以繼承 `_strip_mcp_sensitive_values` 的保證）＋projected 檔。B：`config.sensitive` 只留兩個鍵，postgres 密碼另由 chart `randAlphaNum`＋`lookup` 自產。C：全新的 `PUT /odoo/config` operator 端點把整份 conf 寫成 helm 管不到的 Secret，postgres 則要求 `existingSecret`，但沒有任何一方被指派去產生它。
- **這個矛盾其實在問什麼**：已驗證 `_build_mcp_sensitive_values` 只在 create（`src/models/cloud_service.py:1384`）、redeploy（`:946`）、reset（`:1128`）被呼叫，**reconfigure 不送 sensitive_values**（`src/controllers/paas.py:3760-3775` 的 upgrade_kwargs 只有六個鍵，且 `reset_values=True`）。A 與 B 都會在租戶第一次按「儲存設定」時被咬；C 繞過 helm 反而多出一顆沒有生命週期的孤兒 Secret。要回答的是：**憑證留在 helm 的 values 流裡（那就必須補 reconfigure 的 sensitive 通道），還是整個搬出 helm（那就必須為那些 Secret 定義建立、輪替與刪除的擁有者）？** 這是二選一的架構裁決，不是實作細節。

#### Odoo database manager 的 master password（`admin_passwd`）

- **三份各自的立場**：A：列進 `ODOOCONF_*` 清單，並在 `_build_mcp_sensitive_values` 增加 `master_password`。B：`config.sensitive` 明文鎖死只有 admin_password／smtp_password 兩個鍵，`admin_passwd` 沒有位置（落到 Odoo 內建預設）。C：由平台 renderer 直接寫進 conf Secret。
- **這個矛盾其實在問什麼**：耦合盤點 §3.3 的「風險與待量測」第三點已指名「就算 list_db=False 也應該設一個隨機值做縱深防禦」。三份對同一把憑證給出「平台鑄造的 sensitive 值／不存在／conf 內容」三種答案，其中一種等於所有租戶站的 `/web/database/manager` 用同一個公開預設值，而這些站全部經 Cloudflare tunnel 公開可達。要回答的是：**master password 是平台密鑰（可輪替、永不顯示）、租戶密鑰，還是根本不設？** 這也決定 `list_db=False` 是唯一防線還是縱深防禦的一層。

#### workers>0 時 8069／8072 的分流發生在哪一層

- **三份各自的立場**：A：chart 的 nginx sidecar（或多埠 Service），由 image 端 `WOOW_WEBSOCKET_ROUTED` 開機檢查咬住。B：workers>0 強制 `websocket.mode=sidecar`，由 `_helpers.tpl` 的 `fail` 擋。C：**明確不放 sidecar**，改由 Cloudflare tunnel 同 hostname 多條 ordered 規則，並要求改 `extra/paas-operator/src/services/cloudflare.py` 的 `create_route`（現況一個 hostname 只認一條規則）。
- **這個矛盾其實在問什麼**：表面上是「sidecar vs 入口層」的偏好，實際在問：**租戶的 L7 入口是一跳（Cloudflare）還是兩跳（Cloudflare → pod 內 nginx）？** harumi 的入口 nginx 同時做四件事：`/web/assets`、`/web/static` 反代快取、`/websocket` → 8072、`/private_…/` → `mcp-odoo-proxy:8001`、`client_max_body_size 100m`（harumi 計畫 §3 D8）。後三者裡只有路由那一件 Cloudflare 規則做得到，快取與 100m 上限做不到 ⇒ 選 C 的形狀等於宣告 harumi 搬不進來。這條矛盾必須和「這個 chart 要不要承接 harumi」一起裁決，不能分開處理。

#### 同一套 chart 要不要同時服務平台 Odoo（paas-odoo）

- **三份各自的立場**：A：列為 open question，只預留一個 `WOOW_UPGRADE_ON_IMAGE_CHANGE=0` 旗標。B：完全沒處理（頂層 12 鍵、逐層 `additionalProperties:false`）。C：自陳「只做到一半，驅動層不共用」，但把缺口定位在驅動層。
- **這個矛盾其實在問什麼**：任務硬約束寫死「同一套設計要同時服務兩邊」，而盤點 §5 列出平台 Odoo 現況至少用到 11 類額外鍵（C078 customPostInitScripts、C082 patch-odoo-conf、C083 filestore-hashdir-guard、C084 module-version-gate、C098 三支 initContainer、C100 networkPolicy、C105/C106 extraContainerPorts 與 service.extraPorts、C107 ingress extraPaths/extraRules、C108 扁平 probe 鍵、C109 netpol 埠、C097 兩段式 promote）。要回答的是：**fail-loud 的 `additionalProperties:false` 與平台 Odoo 的逃生門，哪一個先讓步？** 讓 schema 讓步 ⇒ 三份共同的最大賣點消失（Bitnami 殘鍵會再度被接受）；讓平台 Odoo 讓步 ⇒ 那是另一筆 20～35 人日，且缺口不在驅動層而在 chart 的 values 契約本身。這是動工前的岔路，不是 open question。

#### 被選中的 addon 檔案落在哪裡：ephemeral 還是 PVC

- **三份各自的立場**：A：從 `/all-addons` symlink 到 emptyDir `/mnt/extra-addons/selected`。B：複製到 emptyDir。C：rsync `--delete` 到 filestore PVC 的 `/var/lib/odoo/extra-addons`。
- **這個矛盾其實在問什麼**：在問：**租戶勾選的第三方 addon 程式碼，是「租戶的持久狀態」還是「image 的衍生物」？** 選 PVC（C）⇒ 它會跟著 `resource-policy: keep` 與孤兒 PVC 90 天 grace 一起被保留，image 回退後磁碟上會留著較新的 addon 程式碼，而 `--delete` 一旦 catalog 與 DB 實況不符就會刪掉仍標記為 installed 的模組目錄。選 emptyDir（A、B）⇒ addon 集合純由 image 決定，但新 image 下架一顆 addon 時，所有裝過它的租戶會在下一次 pod 重排當下集體出問題（A 選擇 exit 78，B 沒有說法）。這個歸屬決定了 image 升版的 blast radius 與「刪除服務時要不要一併保留第三方程式碼」，目前三份各選一邊、都沒論證。

### 4.3 未經實測的假設

- operator 的 exec 路徑在官方 image 內完全可行：container 名、系統 `python3` 能 `import odoo`、`parse_config` 讀得到新 conf、`odoo shell` 在 uid 101 下可經 kubectl exec 跑。#1136 完全沒碰這條路，而它是 `post_deploy_init_type='odoo'` 每次部署必走的路徑，也是 `_assess_addon_removal`（`src/controllers/paas.py:2213`，service 必須 running 才裁決、否則 fail-closed）的唯一資料來源。盤點 §2.8 明載「這三支腳本的容器名、conf 路徑都沒有測試斷言，也沒有 integration test」，唯一 canary 是 `extra/paas-operator/tests/test_odoo_init_cleanup.py:543` 的直譯器字串比對。
- 平台實際送進 helm 的 values 全集能通過新 chart 的 `additionalProperties:false`。從沒有人把 `_assemble_deploy_values` 的真實輸出拿去 render 過：`_inject_image_pull_secrets`（`src/models/cloud_service.py:1847-1864`）對**所有** template 無條件寫 `global.imagePullSecrets`（字串陣列）、operator `_ensure_image_pull_secrets`（`extra/paas-operator/src/api/releases.py:128`）另外 setdefault 頂層鍵（item 型別未對）、`_inject_storage_class`（`:1785`）、`_apply_plan_spec_overlay` 寫頂層 `resources`／`persistence`。A 的頂層鍵表甚至沒列 `global`，而盤點 §3.1 第 5 點原話是「頂層 additionalProperties: false，**並宣告 global**」。
- `-i base` 與 `-u all` 在**有真實資料**的租戶 DB 上的實際耗時。#1136 只量到空 DB（install→Ready 約 2 分鐘、registry 載入 60s）。這個數字同時決定 startupProbe.failureThreshold、helm `--timeout`、`DEPLOY_TIMEOUT_MINUTES_DEFAULT=30`（`src/models/cloud_service.py:355`）、`init_retries=5`、以及 reset 路徑寫死的 `--atomic --timeout 15s`（`extra/paas-operator/src/api/releases.py:942,1126`）能不能對齊。盤點 §6 第 9 點自承未量測。
- `loadDemoData` 與 `--without-demo`／`--with-demo` 的對應（盤點 §6 第 12 點明列待核實）。三份都保留了租戶可見的 demo 旗標，卻都在 bootstrap 指令裡寫死 `--without-demo=all`，等於自己造了一個新的「鍵存在但沒人消費」。
- websocket 分流的端到端行為。#1136 只量到直連 8072 回 101、8069 回 500。**經過 nginx sidecar** 之後的 Upgrade/Connection header、proxy_read_timeout、X-Forwarded-* 與 `proxy_mode=True` 的互動（header 錯會讓 url_root 與郵件連結靜默變錯）沒有跑過；**Cloudflare 同 hostname 多條 ordered 規則**（C 的路線）也沒有跑過，而 `create_route` 現況是整份 tunnel config 的 read-modify-write、無樂觀鎖、且一個 hostname 只認一條。另外 `workers=0` 時 8069 的 `/websocket` 是否可用完全沒量——若可用，小 plan 可以完全免除 sidecar，這會改變 chart 的複雜度估算。
- `helm uninstall` → operator post-renderer 蓋 `resource-policy: keep` → 同名 release 重裝接回舊 PGDATA 的行為。三份的 postgres 密碼機制（`config.sensitive`＋PASSWORD_FILE／chart `randAlphaNum`＋`lookup`／`existingSecret` 無產生者）沒有一份在這個情境下被驗過；#1136 是手動跑 helm（沒經過 post-renderer），觀察到的反而是 PVC 被刪、重裝卡 Pending，剛好是反向的對照組。
- 訂閱停權（`scale_release` replicas=0）→ 復權（1）在一顆 boot path 含 bootstrap／升級閘的 chart 上的行為，以及 `_rescale_halted_after_upgrade`（`src/models/cloud_service.py:996`）這個既有 replicas 擁有者與新設計的互動。三份都沒跑過，C 只在 module_upgrade 一處提到互斥需求。
- 官方／pgvector postgres image 下 odoo role 的權限等級是可接受的。#1136 觀察到的是 superuser＋createdb，但沒有人測「把它降成非 superuser 之後 Odoo 還能不能建 DB、能不能跑」，也沒人測 `CREATE EXTENSION vector` 由誰、在哪一刻執行（非 trusted，需要 superuser）。
- `/all-addons` 在 `USER odoo`（uid 101）下讀得到，以及 `python3 -m pip install --break-system-packages` 裝的套件與 operator exec 用的 `python3` 落在同一條 `sys.path`。官方 image 在 `USER odoo` 之後的 COPY 預設 owner 仍是 root，三份的 image_delta 對 chown 的時機語焉不詳（只有 C 說加一行 `chown -R odoo:odoo /all-addons`，沒說在哪一步）。這同時是 addon 機制與 operator `/init/odoo` 能否跑起來的前提。
- 一顆從外部 pg_restore 進來的既有 Odoo DB（harumi）能通過三份中任何一份的 bootstrap 流程而不被破壞。三份的冪等判準都只分「空／非空」兩態，對「非本設計建立、沒有 woow.* 記帳、模組版本未知、admin 不是 uid 2」這第三態沒有定義行為。
- C 的兩個地基假設零實測：(a)「官方 odoo:18.0 在 DB 未 init 時 `/web/health` 回 200、容器維持 Ready」——#1136 的所有實測都是 initContainer 先把 DB 建好才讓主容器起來（`assets/1136/values-official.yaml`）；(b)「detached exec（`setsid nohup`）在 exec 連線關閉後存活」——官方 image 的 PID 1 是 Odoo server 本身、不是 init，不收屍也不轉發訊號。
- 官方 image 內有 `rsync`（C 的 addons 階段核心動作）。官方 odoo:18.0 的 base 是 ubuntu:noble，apt 清單只到 postgresql-client；三份的 image_delta 都宣告「只做這幾件事、不准加別的」，而 rsync 不在那幾件裡。

### 4.4 建議的下一顆 prototype

不要先寫 chart，也不要先動 image build 的 /approve gate。下一個最該實測的是：**在 dev sandbox 裡，用平台自己的程式碼路徑（不是手動 helm）驅動一顆官方-lineage 的 odoo release，跑完一輪完整生命週期**。

做法：本機 build 一顆 `FROM odoo:18.0` 的 cloud-odoo 候選 image（只加 /all-addons＋pip＋chown，不寫 entrypoint、不過 prod gate），配上 #1136 已證明會開機的那三個 values 檔，透過 `/woow-dev:k8s-dev-sandbox` 起一個 paas-ws 形狀的 release，然後**用平台的 UI／API** 依序走：建立 → post-deploy init（`/init/odoo`）→ 勾一顆 addon → `_assess_addon_removal` 預檢 → reconfigure（只改 SMTP 一個欄位）→ 換 plan → 停權／復權 → 改 seed 的 image tag 後 redeploy → uninstall → 同名重裝。

它一次量掉最多的未知，而且全部是平台側、不需要新 chart：
1. operator exec 路徑在官方 image 內到底跑不跑得起來（目前零覆蓋，卻是每次部署必走，也是 addon 移除 fail-closed 的唯一資料來源）；
2. 三條 create-only 管線（`helm_default_values` 只在 `src/controllers/paas.py:3353` 讀、`_apply_plan_spec_overlay` 只在 `:3419` 呼叫、reconfigure 的 upgrade_kwargs 不送 `sensitive_values`）的實際行為——這三條同時決定 A 的升級閘輸入、B 與 C 的 image 版本傳遞、以及三份的憑證通道能不能成立；
3. post-renderer 蓋 `resource-policy: keep` 之後，uninstall→重裝接回舊 PGDATA 會怎麼壞；
4. 停權／復權把 postgres 一起 scale 之後，bootstrap 在冷 DB 上的實際耗時，用來校準 30 分 deploy timeout／`init_retries=5`／reset 路徑的 `--atomic --timeout 15s`。

第二優先、可並行的一顆小實測（約半天）：把平台 `_assemble_deploy_values` 對 odoo template 的真實輸出 dump 出來，拿去對一份手寫的嚴格 `values.schema.json` 跑 `helm template`。三份設計的 fail-loud 主樑全部建立在這件事上，卻沒有人看過那份輸出長什麼樣（A 的頂層鍵表甚至沒有 `global`，而平台對所有 template 無條件注入它）。

⛔ 建議明確**不要**在這兩顆實測完成前開始寫 chart 或 entrypoint：三份設計的分歧有一半以上會被這兩顆實測的結果直接消掉；另一半（harumi 的入口層與 hostname、master password、平台 Odoo 共用與否、addon 落點）是需要人裁決的岔路，不是寫程式能解決的。

參考檔案（絕對路徑）：
- docs/scout/2026-09-20-bitnami-chart-official-odoo-prototype.md
- docs/scout/assets/1136/values-official.yaml（及 values-v2-workers.yaml、values-v3-pgvector.yaml）
- 耦合盤點與 harumi 移植計畫在 `origin/docs/harumi-odoo-paas-scout` 分支的 `docs/scout/` 下（工作目錄 woow_paas_platform）
