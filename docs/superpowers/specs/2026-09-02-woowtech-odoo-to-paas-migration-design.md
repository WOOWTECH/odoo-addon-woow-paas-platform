# woowtech-odoo → paas 平台移植：設計 spec

- 日期：2026-09-02　狀態：draft，待 Eugene review
- 來源：brainstorming session 2026-09-01～02，六項裁決與全部量測見文末附件
- 相關單：#994（已合）、#996（潛伏地雷，另追）、GitHub WOOWTECH/Woow_odoo_portal_user_ui PR #2（待 merge）

## 1. 目標與非目標

**目標**：把 prod `woowtech-odoo` namespace 裡的 Odoo（`aiot.woowtech.io`，公司 ERP＋官網＋金流）整個併進 paas 平台的 `paas-odoo`（`paas-system`），成為**單一 Odoo instance**，帳號與金流以 aiot 現有資料為準。

**為什麼是合併而不是收編成租戶**（Eugene 09-01 裁決）：金流與現有帳號都在 aiot，平台又還沒有真實用戶（prod DB 87 MB、2 user、0 訂單、website 未裝、唯一 workspace 是測試帳號）⇒ 現在是唯一能「整包換底、零 ETL」的時間窗，平台有用戶後就沒了。

**非目標**：
- 不改 ERP 功能、不升 Odoo 版本（兩邊都是 18.0）
- 不動 paas 平台的租戶部署機制（marketplace／operator 不受影響）
- 不把 `woowtech-odoo` ns 裡的 mcp-odoo×3／n8n 一併收編（Phase 5 只改指向）
- 不在本 spec 決定切換日期

## 2. 方案：基底翻轉

以 aiot 的 DB 為底、整包 restore 進 `paas-odoo-postgresql`，平台模組（`woow_paas_platform` 及其 depends）**疊在它上面**；paas 現行 DB `bitnami_odoo` 保留當 rollback，不覆蓋。

否決的替代：
- **O1 收編成 workspace**：帳號／金流兩套並存，正是 Eugene 不要的
- **逐筆 ETL 合併進 paas DB**：paas 側沒有值得保留的資料，ETL 是純成本

## 3. 現況（09-02 實測，全部唯讀）

### 3.1 aiot（`woowtech-odoo` ns）
| 項目 | 值 |
|---|---|
| Odoo | stock `odoo:18.0` image，`workers=6`、`max_cron_threads=2`、`limit_time_cpu=600`／`real=1200`、`limit_memory_soft=4G`／`hard=5G`、`proxy_mode=True`、`list_db=False`、`dbfilter=^odoo$` |
| DB | `pgvector/pg17`，DB 名 `odoo`／user `woow-odoo`，**207 MB**；extensions＝pg_trgm 1.6（10+ 個 index 在用）、plpgsql、**vector 0.8.3（孤兒：0 欄位／0 index／0 相依，可 DROP）** |
| filestore | **1.9 GB**（`/var/lib/odoo/filestore/odoo`） |
| 規模 | 202 installed modules（**28 個非官方**＋l10n_tw）、35 active users、637 partners、1968 posted moves、website 已裝 |
| addons | PVC `/mnt/extra-addons`，57 個模組目錄（只 28 個有裝）；`clone.sh` 是手動跑的、對 clone 失敗只 WARN；`_repos/*` 16 個目錄**全無 .git**，且與 clone.sh 清單不同組；活目錄含 sed patch 與手改（`woow_hr_portal` 與 `_repos` 差 19 檔） |
| 對外 | cloudflared token tunnel（hostname 在 CF dashboard）、nginx sidecar |
| 備份 | `pg-backup` cronjob 每日 03:30 `pg_dump -Fc` 留 7 份到 backup PVC；**filestore／addons 無備份**；web database manager 被 `list_db=False` 擋 |
| 週邊 | mcp-odoo／mcp-odoo-admin／mcp-odoo-social（＋proxy）、n8n、n8n-mcp 都指 `woowtech-odoo-svc` |

### 3.2 paas（`paas-system` ns）
| 項目 | 值 |
|---|---|
| paas-odoo | 自建 image（bitnami 佈局），bitnami odoo chart 28.2.10，Fleet 管理（`paas-app-bundle-cd`） |
| DB | `paas-odoo-postgresql`（bitnamilegacy 17.6），DB `bitnami_odoo` 87 MB，PVC 10Gi；paas-odoo PVC 20Gi |
| 入口 | `paas.woowtech.io`（traefik ingress） |
| 備份 | `pg-backup` cronjob 每日 03:30（29 天前加）＋5Gi backup PVC |
| 內容 | 2 users（Administrator＋alan）、1 workspace「HA PaaS」＋1 running HA service（ns `paas-ws-8594c519`，PVC `longhorn-delete`）、`ir_config_parameter` 45 key |

## 4. 裁決紀錄
| # | 題 | 裁決 |
|---|---|---|
| 1 | alan 的 HA workspace／service | **連資料一起拋**（alan 確認為測試帳號） |
| 2 | 28 個自製模組進 CI 的來源 | **gitea `mirror-internal`**：9 個 GitHub 找得到的走 mirror；20 個找不到／孤兒的走單一 snapshot repo |
| 3 | canonical domain（`web.base.url`） | **維持 `aiot.woowtech.io`** |
| 4 | 切換窗 | 未定；Phase 2 排練綠後再排。**切換不刪舊環境** |
| 5 | snapshot repo 形式 | 單一 `mirror-internal/woowtech-erp-addons-snapshot` |
| 6 | `woow_portal_ui` theme css（prod 07-23 > GitHub 06-19） | 推回 GitHub（PR #2）再走 mirror |

## 5. Phase 1 — image（可先行、零風險）

**產出**：`paas-odoo-ci` 的 image 內含 ERP 28 個自製模組＋pip 依賴。先上線跑現行 `bitnami_odoo`——多出的模組沒安裝＝死碼，行為零改變。

**模組來源三路**：
| 來源 | 模組 |
|---|---|
| gitea mirror（釘 commit） | odoo_core_patch_browser_enhancement、im_livechat_ai、woow_line_base、woow_odoo_line_liff、woow_odoo_livechat_line、woow_portal_ui（PR #2 merge 後） |
| paas-odoo-ci 已有 | ecpay_invoice_tw、payment_ecpay（版本 18.0.1.1.1 兩邊相同，不動） |
| snapshot repo `10013d5e0870` | barcode_scanner_×7、cloud_link×2、sales_enhance、woow_hr_portal×4、woow_hr_payroll、auth_oauth_proxy_fix、new_customer_default_language、woow_notification_enhancement、hide_menu_user、order_line_sequences |

**pip**：`install.sh` 的 15 個（fs==2.4.16、vuefinder-wsgi==1.5.2、munch、cachetools、google-auth-oauthlib、openpyxl、ofxparse、qifparse、pycryptodomex、PyPDF2、python-barcode、anthropic、openai、google-genai、pyOpenSSL）∪ manifest 宣告的 6 個（python-barcode、qrcode、Pillow、pycryptodomex、PyPDF2、requests）＋本地 wheel `fs_googledrivefs-2.4.4`。manifest 不是完整來源（cloud_link 靠 fs 卻沒宣告），故取聯集。

**patch**：clone.sh 的 sed 已烤在活目錄，snapshot 即成品。Odoo core `http.py` ProxyFix patch **先不做**——平台走 traefik 帶正確 header；Phase 2 驗 redirect／asset URL 若壞再補。

**驗收**：新 image 對現行 `bitnami_odoo` 全模組測試，與 base 差集為空（#994 用的 base/head 對照法）。

## 6. Phase 2 — sandbox 排練（決定性驗證）

1. 用 `KUBECTL_NS_GUARD=off` 唯讀 exec 取 aiot 的 `pg_dump -Fc`＋filestore tar（web manager 走不通）
2. restore 進 k8s-dev-sandbox（Phase 1 image）：`pg_restore` 略過 `CREATE EXTENSION vector`（`pg_restore -l` 產清單、刪該行、`-L` 餵回）；filestore 放 `data_dir/filestore/<db名>`
3. **neutralize**：關 cron／outgoing mail／payment provider（Odoo 18 restore 有 neutralize；手動 restore 則 `UPDATE ir_cron SET active=false`、`ir_mail_server` 停用、`payment_provider` state=disabled）
4. `-u all` 對齊 stock vs bitnami build 的模組版本
5. 裝 `woow_paas_platform`（拉 depends）
6. **檢核點**：ERP 抽查（登入、會計報表、payroll、LINE webhook 路由存在）、`/woow` 起得來、marketplace 列得出 template、模組衝突清單（ERP 與平台 depends 的交集：ai_base_gt 家族、sh_subscription 家族——ERP PVC 有但沒裝，預期無衝突，**要驗**）
7. 記錄 restore 耗時（預估 <30 min）

## 7. Phase 3 — 保護（Eugene 的硬要求）

切換前必做、全部離叢集副本：
- DB：當天 `pg_dump -Fc`（凍結後）＋既有每日 dump 各一份
- filestore：tar `/var/lib/odoo/filestore/odoo`（1.9 GB）
- addons：已有 snapshot tar（SHA256 `3bbee796…`）
- manifests／configmaps／secrets：`kubectl get -o yaml` 匯出，加密存放
- **驗證還原得起來**（Phase 2 就是這個驗證）才准動舊環境

## 8. Phase 4 — 切換（離峰窗，日期待定）

1. aiot 凍結：cloudflared scale 0（外部進不來），Odoo 本體保持 up 供最終備份
2. 最終 `pg_dump -Fc`＋filestore tar → 離叢集副本 → 校驗
3. restore 進 `paas-odoo-postgresql` 成**新 DB**（名 `woow_prod`；不碰 `bitnami_odoo`）；filestore 進 paas-odoo PVC `filestore/woow_prod`；略過 vector extension
4. 對新 DB `-u all`、裝 `woow_paas_platform`
5. **replay 設定**（不抄值、重設）：`woow_paas_platform.{operator_url,operator_api_key,apiserver_cidrs,deploy_env,namespace_prefix,paas_domain,grace_period_days,cleanup_after_months,sa_default_service_url,topup_payment_enabled,default_enable_agent_runtime}`；`default_agent_template_id`／`default_ai_assistant_id`／`default_ai_config_id` 存 record id，**必須重選**；`sh_subscription_wallet.billing_ssot`；ecpay 三鍵以 aiot 現有的為準（金流在那邊）；`web.base.url=https://aiot.woowtech.io`
6. 改 paas-odoo 的 `ODOO_DATABASE_NAME`／`dbfilter` → `woow_prod`（`paas-app-bundle-cd` Fleet values，GitOps）；odoo.conf 覆寫 `workers≥4`、`limit_time_cpu=600`、`limit_time_real=1200`、memory limit 對齊 aiot
7. 改 `paas-system/pg-backup` cronjob 目標 DB → `woow_prod`
8. 驗證：ERP 抽查、`/woow`、金流 sandbox 交易、LINE webhook
9. DNS：`aiot.woowtech.io` 從舊 tunnel CNAME 切到平台 ingress；traefik ingress 加該 host；`paas.woowtech.io` 保留為別名
10. 舊 `woowtech-odoo` ns：**只 scale 0，不刪**，保留 ≥2 週

**Rollback**（任一步失敗）：DB 指回 `bitnami_odoo`＋DNS 切回舊 tunnel＋舊 ns scale 回 1。兩段各自獨立可退。

## 9. Phase 5 — 收尾
- mcp-odoo×3／n8n 改指向 `paas-odoo.paas-system.svc`；**paas-system NetworkPolicy 可能擋跨 ns**，要開洞或收編
- 密碼全輪換（舊 deployment 的 last-applied annotation 有明文 admin／SMTP 密碼）
- alan 的 HA：刪 release＋ns `paas-ws-8594c519`（PVC 隨 `longhorn-delete` 消失，已裁決）
- 觀察期滿除役舊 ns（另一個明確動作，不在切換內）

## 10. 已知陷阱（實撞過的）
- `(4, ref())` 累積：改動既有 ir.rule 綁定時舊的不會移除（#996）；`-u` 升級後要對帳
- 刪 XML record 後同一次 `-u` 測不到（`_process_end` 在測試之後）
- `woow-test.sh` 測試失敗仍 exit 0；`tea actions runs list` 的 completed 對失敗 run 一樣
- paas-odoo **沒有 stg 對應環境**；PR CI 結構上不跑 odoo-tests ⇒ 排練＝唯一的模組測試證據
- zsh：`for m in $VAR` 不 word-split、`$id[...]` 是陣列下標、`status` 是唯讀變數
- GitHub「not found」對私有無權與不存在回同一句，探針零鑑別力

## 11. 開放項
- 切換日期（Phase 2 綠後排）
- PR #2 merge（Eugene，WOOWTECH 帳號）→ 觸發 mirror sync
- mcp／n8n 最終歸屬（Phase 5 之後）
- `http.py` ProxyFix 是否需要（Phase 2 驗）

## 附件（`~/Documents/woow/AREA-odoo/woow-addons/`）
`woowtech-odoo-migration-state.md`（裁決＋進度）、`woowtech-odoo-{pvc,db}-inventory.log`、`woowtech-odoo-manifests.tsv`、`woowtech-odoo-repos-inventory.tsv`、`woowtech-odoo-live-vs-{repos,mirror}-diff.*`、`erp-live-addons-snapshot/`（tar＋SHA256SUMS）、`erp-mirrors/`、`erp-snapshot-repo/`、`erp-upstream/`、`gitea-mirror-setup.log`、`woow_portal_ui-theme-css.diff`
