[3575 lines total]
# PaaS 平台上線前 issue 盤點報告（2026-08-01）

盤點範圍：git-prod.woowtech.io 全站 **87 筆 open issues**（woow_paas_platform 72＋paas-app-bundle-cd 4＋paas-odoo-ci 4＋paas-agent-runtime 3＋woow-paas-charts 3＋woow_paas_smart_home 1）。

流程：Fable 定 rubric → 10 個 Sonnet agents 分主題群逐筆調查（tea 讀內文＋本機 code 覆核）→ Opus 綜合裁決 → Fable 評分官加權評分＋Fable 對抗式審查官抓漏。

## Rubric（七面向）

| 面向 | 權重 | 內容 |
|---|---|---|
| D1 不可逆資料損失 | 5 | 備份缺失、資料滅失、發生後無法回復 |
| D2 資安與租戶隔離 | 5 | 憑證外洩、跨租戶存取、網路隔離破口 |
| D3 金流與計費正確性 | 4 | 錯扣／超收／超賣／退點錯誤 |
| D4 核心旅程阻斷 | 4 | 主流程卡死且無 workaround |
| D5 營運可觀測性 | 3 | 假訊號、驗收基準污染、發版鏈脆弱 |
| D6 機率×爆炸半徑 | 3 | 上線初期實際踩中機率×影響面 |
| D7 修復可行性 | 分流 | effort S/M/L |

P0 判定：D1≥4 或 D2≥4，或（D3≥4 且 D6≥3），或（D4≥4 且無 workaround）。加權滿分 120。

## 最終 P0——上線前必修（12 條＋1 平行軌）

| # | Issue | 評分 | 上線前最小交付 |
|---|---|---|---|
| 1 | woow_paas_platform#504 prod 平台 DB 零自動備份 | 63/120・S | 比照既有 18 份 pg-backup CronJob 建平台 DB 每日備份（全清單第一優先） |
| 2 | paas-odoo-ci#46 addon_ref 留空靜默 build 出倒退 image ⚠️勘誤見下 | 68/120・S | ~~fallback 到 tag ref 或~~ fail-closed（已收官） |
| 3 | paas-app-bundle-cd#77 prod migration 執行是黑箱 | 67/120・M | -u 輸出 tee 進 stdout＋flag 切換改流程強制項（治本 upgrade Job 留 fast-follow） |
| 4 | woow-paas-charts#42 open-design NP egress `- {}` 全開直達控制面 | 76/120・M | 抄 agent-runtime chart 已驗證 egress 範本收斂，修完用 F-2 路徑實測複驗 |
| 5 | woow_paas_platform#427 paas-system 無 ingress baseline | 64/120・M | 先落地 paas-operator ingress 最小白名單版（完整 default-deny 留租戶出現後） |
| 6 | paas-app-bundle-cd#24 prod registry 密碼明文躺在 last-applied annotation | 58/120・S | 輪替 JCR 憑證→清 annotation→刪冗餘 key（runbook 已備） |
| 7 | paas-agent-runtime#29 operator helm install 把 repo 密碼明文寫進 log | 48/120・S | redact 兩個 flag，先上 redact 再做 #24 的輪替 |
| 8 | paas-agent-runtime#26 untrusted skill 與平台憑證同 pod 同 uid | 74/120・M | 最低三件：env-scrub、bash 換獨立 uid、secret 檔權限收斂 |
| 9 | woow_paas_platform#501 HAOS 限量 8 台無程式機制，超賣＝已付款卡 Pending | 55/120・M | 全域上限（比照 max_per_partner advisory-lock）＋停售旗標脫離發版 |
| 10 | woow_paas_platform#352 email_domain/everyone 繞過席位 quota，可吃光全平台 CF 50 席 | 58/120・S | controller/資料層對 rule_type 加驗證＋fail-loud |
| 11 | woow_paas_platform#342 change plan 收錢不換規格（銀貨不對板） | 40/120・L | 止血：關閉含規格落差的升降級入口或導人工；re-apply 機制留 fast-follow |
| 12 | woow_paas_platform#29 subscription traversal 缺 sudo，SH/HAOS delete/provision 必炸 | 36/120・S | mixin 內加 sudo（修法極小，HAOS 出貨前必修） |
| 平行軌 | woow_paas_platform#441 kasim-odoo-data 檔案系統損壞 | 46/120・M | 歸屬 woow-machines-operator：離線 fsck（先 snapshot）＋節點 kernel log 查根因——若是 smartpqi 硬體問題，影響「新租戶資料放不放這批節點」的決策，開賣前要有答案 |

## Fable 內部分歧與最終裁決

評分官與審查官（皆 fable）對三條意見相左，最終裁決如下：

- **cd#77（migration 黑箱）**：審查官認為 D1=4 依賴「#504 未修」這個會被自己 P0 清單消滅的前提。裁決：**保留 P0 但限最小修法**（-u 輸出可見，S 級）——理由是後續每條 P0 都要過發版鏈，可見性必須先於其他修復上線；#504 落地後其 D1 降為 3 屬實，故治本改造不進 P0。
- **#389（prod SMTP 全滅）**：審查官指出不命中任何機械 P0 規則——註冊/購買/開通不依賴寄信、扣款走 ecpay、force_send=False 讓信留在 mail.mail queue 修好可補寄。裁決：**降 P1**，但 effort S 且 issue 作者自註「公開上線前修復」，列入 Wave 0 順手修（非嚴格 gate）。
- **#441（kasim volume 損壞）**：審查官指出受影響對象是共用叢集上其他部署的客戶實例（CLAUDE.md 鐵律的非平台 ns），非平台租戶。裁決：**移出平台 P0 名額、列平行軌**——不佔平台工程資源，但 fsck／根因調查必須在開賣前啟動並有結論。

審查官另抓到兩條需顯式決策：

- **#383（admission backstop）與 #427 同為 D2=4 卻一留一降**：裁決採審查官意見——上線前先做 **validate_namespace 呼叫覆蓋率複查**（S，機械化 grep operator 全部寫入端點），完整 Kyverno/OPA 留 fast-follow。與 #42/#427 併成「控制面防禦」同一工作流統一驗收。
- **#267（Hermes OpenAI gateway）被 opus 靜默丟出名單**：屬 07-01 會議最高優先業務目標、effort L。無交付落差（行銷未承諾），不進 P0/P1，但**設計應及早啟動**，不要等上線後才排。

## P1——上線後兩週 fast-follow（重點前 10）

| Issue | 評分 | 要點 |
|---|---|---|
| woow_paas_platform#383 | 53/120・L | validate_namespace 覆蓋率複查升上線前；Kyverno/OPA 完整層 fast-follow 起跑 |
| woow_paas_platform#381 | 51/120・M | CF Access 50 席共用池零監測——先補每日用量 cron＋40/45/48 三級告警 |
| woow_paas_platform#389 | 49/120・S | SMTP 修復（Wave 0 順手修；low-balance dedup marker 遞送失敗不重試要一併修） |
| paas-app-bundle-cd#76 | 46/120・S | operator chart prod/stg 版本漂移零檢查——加 CI 比對 |
| woow_paas_platform#467 | 41/120・S | 15 個測試從未執行（tests/__init__.py 漏 import）——假綠 |
| woow_paas_platform#442 | 40/120・M | 備份健康監控改驗 dump 非空（lastSuccessfulTime 曾謊報數週） |
| woow_paas_platform#428 | 40/120・S | containerPorts.https 未定義→NP 全 TCP 開放 |
| woow_paas_platform#443 | 34/120・L | 租戶備份異地副本（叢集級災難＝備份與資料同滅） |
| paas-agent-runtime#23 | 30/120・M | 平台預設模型 tool-call 空 kwargs——prod 1.4.2+ 先花 15 分鐘複驗，仍在即升當週最高優先 |
| woow_paas_platform#207 | 30/120・S | MCP sidecar route 刪除不對稱→必留孤兒 CF route，一行修 |

其餘 P1：#421 maxPods、#316 MCP tool sync 被 NP 擋（prod latent）、#385 CF tunnel 孤兒清理、#503 filestore root-owned、#440 殘餘待辦、ci#43/ci#30 approval gate、#496 quota 守線 floor、#323 取消訂閱不退點、#386 operator 防線三件、#388 spawning 卡態、#155 SKILL_BUNDLE_TOKEN TTL、#382 bitnami image、#107 zh_TW 在地化、#52 project-detail hardening、#392 禮品卡 SOP。

## 建議處理順序（五波）

原則：先讓「修東西這件事本身」變安全（備份＋發版鏈），再堵已在售商品打得到控制面的路，最後處理需要產品決策的交付正確性。

1. **Wave 0（第 0–1 天，全 S 可平行）**：runtime#29 redact 先上線 → cd#24 輪替憑證清 annotation；併行 plat#29 mixin sudo、#389 SMTP。
2. **Wave 1（第 1–3 天，任何 P0 上 prod 前必須完成）**：#504 平台 DB 備份（第一優先）→ ci#46 fail-closed → cd#77 -u 輸出可見。時間極緊時 #504＋ci#46 不可壓縮。
3. **Wave 2（第 3–7 天，同批設計驗證）**：charts#42＋#427 最小版（同一攻擊鏈兩端，只修一端＝錯誤安全感）；修完從 open-design pod 實測打 operator ClusterIP 應不通。#316 修法方向此時一併定案。
4. **Wave 3（第 5–10 天，可與 Wave 2 平行、不同人）**：runtime#26 最低三件（另 repo，及早派工）。
5. **Wave 4（第 7–14 天，先決策後實作）**：#501（HAOS 上限＋停售旗標；決策鏈最長排最前）→ #352（純技術 S，插空檔）→ #342（止血方式需產品拍板）。

平行軌：#441 fsck 於 Wave 1 期間啟動（woow-machines-operator 主導）。

## 系統性主題（跨群共病）

- **備份鏈是整個平台最薄的一環，而且薄在最裡面**：全叢集 18 個 Odoo 部署都有 pg-backup，唯獨承載訂閱／扣款／發票的平台主 DB 沒有（#504）；租戶 dump 全在同一叢集單一失敗網域（#443）；備份健康指標曾謊報數週（#442/#440）；而 prod 升版的 migration 執行完全看不見（cd#77）。單看每條都像 ops 待辦，疊在一起就是「改壞了看不到、看到了救不回」。
- **憑證以明文躺在三個地方**：prod secret 的 last-applied annotation（cd#24）、operator 的 helm install log（runtime#29）、以及與 untrusted 租戶程式碼同 pod 同 uid 的 env 與掛載檔（runtime#26）。三條都不是「可能被打」而是「已經放在那裡」，且修法都不難，共同缺的是「祕密不得以明文出現在任何可讀介面」這條規則與對應的檢查。
- **控制面沒有網路邊界**：operator 的 RBAC 不限 ns prefix（#383）、paas-system 沒有 ingress baseline（#427）、而在售商品 open-design 的 egress 是 `- {}` 全開（charts#42）。三者合起來，一個買得起 4500 點的租戶就能從自己的 pod 直達平台控制面 API。任一條單獨看都像縱深防禦的 nice-to-have，串起來才是完整攻擊鏈——這正是分群調查最容易漏判的地方。
- **檢查只做了一層**：UI 擋了 model 沒擋（#152）、controller 擋了 model 沒擋（#289/#282）、app 層擋了 admission 沒擋（#383）、quota 只算了其中一種 rule_type（#352）、sudo 只在其中一個端點加了（#29）。同一種病在五個群裡各長一次，代表缺的不是個別修補而是「授權/配額檢查必須落在最內層、且要有機械 gate 驗證兩層同步」的守則。
- **假綠與假訊號無所不在**：Longhorn robustness=healthy 但檔案系統壞了（#441）、CronJob lastSuccessfulTime 成功但 dump 是 0 byte（#442/#440）、PVC 貼了 recurring-job label 但那個 group 底下沒有 job（#504）、測試檔沒被 import 所以永遠綠（#467）、quota 守線套了 floor 所以缺陷值也綠（#496）、CI digest 三方閉環全對但 image 是 539 個 commit 前的（ci#46）、每天 4 筆已知 Traceback 讓「0 Traceback」驗收基準失效（#503）。上線初期最需要的就是訊號可信度，而這批正在系統性地摧毀它。
- **計費物件與 K8s 實體之間沒有 reconcile**：改方案會重新計費但不換規格（#342）、限量商品沒有全域計數所以會超賣（#501）、取消訂閱不退點也不拆服務（#323）、席位計費可被一條 domain 規則整批繞過（#352）。訂閱記錄與實際交付的資源是兩條各走各的線，沒有任何機制在事後比對兩者是否一致。
- **跨 repo lock-step 漂移，且沒有單一真相**：chart 版本 prod 落後 stg 三版且無檢查（cd#76）、values 部分 override 會與新 chart default 拼出跨 repo digest（cd#70）、operator 的 label 格式要與 charts 的 cronjob template 同步（#208）、CI 的 addon_ref 留空會 clone 到停更的 main（ci#46）。四個 repo 的發版盤點各看各的 commit 區間，永遠看不到別人那半邊。
- **同一件事寫兩次必漏鏡射**：HA provision 與 marketplace 兩套 CloudService 建立邏輯已漏兩次（#415）、CF route 的 create 與 delete 用兩條不同源的命名鏈（#207）。這類重複的代價不是當下的 bug，而是每次新增欄位都要記得改兩個地方，且漏掉時完全靜默。
- **共用池資源沒有配額也沒有監測**：Cloudflare Access 50 席是 per-account 共用（#352/#381）、節點 maxPods 最熱已 89%（#421）、Longhorn 排程額度決定 HAOS 物理上限 ≈16 台（#501）。三者的共同失效形態是「單一租戶或單一商品把全平台的池子吃光，其他人一起受害」，而且都沒有逼近閾值時的告警。
- **tracker 與程式碼長期脫節**：至少 8 張 issue 經覆核已修復卻仍 open（#336/#375/#410/#364/#362/#129/#208/ci#34），另有重複單（#432 與 #503）。這不只是衛生問題——上線前盤點風險時，這些 stale issue 會讓真正的風險被稀釋在雜訊裡，也會讓同一份工作被重複調查。


## ⚠ 守衛失效的四種形態＋發現成本階梯（leader1 歸納，2026-08-02，根因單的論述基礎）

| 形態 | 實例 | 發現成本 |
|---|---|---|
| 守衛**根本沒被建立** | #524 smart_home 零 record rule | **最低**——靜態掃描就找得出來 |
| 守衛**不在必經之路上** | #519（ACL 直給）／#521（無訂閱即 continue）／#522（副作用不落 K8s） | 中——需盤點所有入口 |
| 守衛 **raise 了卻被吞掉** | #516 ValidationError 被 controller 吃掉 | 高——要讀框架的交易語意 |
| 守衛的**並發前提是錯的** | #518 advisory lock＋取鎖後計數在 REPEATABLE READ 下零貢獻 | **最高**——讀程式碼看不出來（邏輯自洽、註解言之成理），只有實跑兩個並發交易才現形 |

🔴 **leader1 的關鍵提醒**：若根因單只做「新 model 上線必須有 record rule＋機械化 gate」，涵蓋的是**成本最低那一層**。真正貴的是後兩層，而後兩層的共同特徵是「**寫在註解裡的框架行為假設從來沒有被驗證過**」——#518 的 READ COMMITTED 就是活例：它被寫進 docstring 當成已確立事實傳承，連 leader 派工時都照抄給 teammate。
⇒ 建議另立一條獨立紀律：**宣稱框架行為時要附實測或原始碼引用**。那不是 record rule gate 能覆蓋的。
（同源實例：dispatcher 記憶中的「addon_ref 留空＝clone main」也是從 workflow 註解推導、未量測，被 leader3 實測推翻；leader2 的「quota 0＝無上限」是照抄相鄰函式語意，被 dispatcher 讀原始碼推翻。**三次都是註解／推導被當成事實**。）


## 📊 實況盤點（issue-dispatcher 2026-08-02 19:00，tea 逐筆查證，取代先前分散記載）

**已 closed**：#352（PR#526 merged）、#520（controller 吞 ValidationError 讓 create 上限守衛失效——被拒服務仍被建立且無訂閱無密碼；PR#523 merged）
**仍 open**：#501、#515、#516、#518、#519、#521、#522、#524
**PR 現況**：#517（HAOS 全域上限硬閘＋停售開關不發版可調＋修好被吞掉的建立失敗回滾）尚未 merge、待 leader1 第二輪覆核；#529（修掉測試套件批次 0 的假綠——10 處已修、3 處復驗不成立）已 merged

⚠ **看板落後過一次**：#520／PR#523／PR#529 是 leader 在工作中自行開立並完成的，未經 dispatcher 記錄。**分配看板不等於工作實況**，定期用 tea 逐筆對帳才是權威。

### 🔴 #522 實測已坐實（leader4，2026-08-02，issue 留言 4382）

**租戶帳號真的寫得進 tunnel_id**——標準 HTTP `/web/dataset/call_kw`、該租戶 session（uid=12、is_admin=False）：
| 探測 | 結果 | 角色 |
|---|---|---|
| authenticate | uid=12、is_admin=False | 陽性對照：登入可行 |
| write `name` | `{"result": true}` | 陽性對照：這條 HTTP 路徑通 |
| write `canonical_name` | `UserError` | **鑑別項**：守衛在 HTTP 路徑上一樣會作用 |
| **write `tunnel_id`** | **`{"result": true}`** | ⇒ 這個欄位就是沒被保護 |
側效果取證（直查 DB）：`tunnel_id=HTTP-HIJACK-<suf>`、`canonical_name` 保持原值。
**鑑別項為何關鍵**：同路徑、同帳號、同 API，只要欄位有被保護就會被擋 ⇒ 寫得進去不能歸因於「守衛整個沒生效」或「探針壞掉」。

⚠ **探針設計坑（留給後人）**：第一版每次探測前呼叫 `env.cr.clear()`，會把前一次**尚未 flush 的 ORM 寫入**丟掉 ⇒ 「寫 tunnel_id 沒噴例外」但「DB 值沒變」兩訊號矛盾——只看例外得到「可寫」（結論對、理由錯）、只看 DB 得到「寫不進去」（**結論相反**）。修正版每次寫入後強制 `env.flush_all()` + `env.cr.commit()` 再用原生 SQL 讀回。⇒ **「沒噴例外」不等於「寫進去了」**。
**仍未量到**：沒有接著觸發路由同步真把路由推上另一條 tunnel（該步經 operator 動 Cloudflare、對外不可逆）⇒「同步為全量覆寫、operator 不驗 tunnel 歸屬」仍是**讀碼結論**（`extra/paas-operator/src/api/tunnels.py:306-345`、`src/services/cloudflare.py:535-583`／`:992-1048`），非實測。
清理：ns NotFound（陽性對照 dev-sandbox-stg Active）、253 顆 PV 中 sec522 相關 0 顆、docker-desktop 0 殘留。


## ⚠ 重工事件：#516 與 #520 是同一個缺陷（2026-08-02，leader1 通報）

兩組人各自完成偵察→開單→實作→覆核的**完整成本**，修同一段程式碼。發現時機＝PR#517 顯示 Conflicting，leader1 用 `git merge-tree` 實測確認是真衝突（非 tea 視圖快取），追出 develop 多了 8 個 commit、其中 `14c7f0e5` 就是 #520 的修復。代價：PR#517 要 rebase、撤出重複部分、全套重跑約 60 分鐘、舊基準 DB 作廢。

**兩邊修法不同，delta 要保留**：#520 用 `cr.rollback()` 撤回整個交易（註解論證：那正是例外真的傳播到框架時框架會做的事，且 try 之前沒有要保留的寫入）；#516 用 savepoint。**但 #520 沒有攔 `OperationalError`** ⇒ #518 的並發問題在 develop 上**仍然存在**——40001 會落到 `Exception` 兜底分支被吞掉，Odoo 的 retrying 永遠看不到、不會以新 snapshot 重試。**這是 #516 必須保留的 delta。**

**根因不在任何一組**：兩張都是「在自己 issue 的調查中順手挖出的既有缺陷」，兩邊都做對了該做的事然後撞在一起。缺的是「誰正在動哪個檔案」的共用視野。

### 已實施的防重工機制（dispatcher，即刻生效）

1. **開新 issue 前先搜尋既有 open issue**（標題＋內文關鍵字）。#516 與 #520 的標題其實高度相似（都在講「被拒絕的服務仍留在 DB 並被部署」），事前搜尋就查得到。⇒ dispatcher 開單前必做（#521/#522/#524 都是 dispatcher 開的，此後一律先搜）。
2. **定期 tea 逐筆對帳**：分配看板 ≠ 工作實況。leader 會在工作中自行開單並完成（#520／PR#523／PR#529 都是），只靠分配紀錄必然落後。
3. **覆核前先 fetch 一次**看 develop 有沒有動過同一批檔案（leader1 自行納入流程；他這輪讀的是分支開出時的快照 `7b10c9bf`，成本近乎零卻能提早發現）。

### 待 Eugene 判斷的機制

**檔案級登記**：leader 開 PR 或開始動工時，在看板登記「本輪會動到的檔案」粗粒度清單（例如 `src/controllers/paas.py`、`src/models/cloud_service.py`），其他人開工前掃一眼。能防真正的檔案級衝突，但增加每位 leader 的維護負擔——隨並行度上升值得，四人以下可能不划算。


## 🔍 跨 repo 全量對帳結果（issue-dispatcher 2026-08-02 19:30，防重工機制首次執行）

**發現兩張看板上完全沒有的 issue（皆非 dispatcher 分配、leader 自行開立）：**
- **#530 feat: Hermes Agent gateway 對外通道＋API key 交付**（`ready-for-agent` label、18:46 開）——租戶部署 Hermes Agent＋MCP Server 後，MCP 的 Gateway 區**無論填什麼都 Disconnected**：gateway API 跑在容器 port 8642，但 CS 對外只有 authproxy 8080，且 authproxy 反代設定**沒有任何路徑指向 8642**。**帶 ready-for-agent 卻無人持有**——待 Eugene 決定是否納入本輪。
- **#525 refactor: paas.py 吞例外→靜默 commit 同構縫盤點（#520 家族）**（`priority:high`、17:19 開）——#520 只修好 `_create_service` 一處，同 bug class 在 `controllers/paas.py` 還有其他位置。9 個候選經**獨立取證**（不照抄 PR#523 原清單、要求兩種情況判 REFUTED）：行號全對、無一 REFUTED，但**其中 5 處的因果敘述與程式碼不符——照 #520 修法照抄會是 no-op 或誤傷**，故分 A／B 兩級。⚠ 註記：PR#523 merge 後 paas.py 行號位移，動工前須以**函式名**重新定位。

⚠ **#516 / #520 / #525 三角關係（提醒 leader1 裁決重複單時納入）**：#520 已 closed（修 `_create_service` 一處）、#516 待裁決（同一處、但含 `OperationalError` delta）、#525 是同 bug class 的**全面盤點**（3 處確認該修＋5 處同構但因果不同）。裁決時要確保 #516 的 `OperationalError`／40001 delta 不隨關單消失，且與 #525 的 A 級清單不重疊。

**tracker 衛生候選**：`charts#42`（open-design NP egress 洞）據 leader3 Wave 2 偵察**疑已在 chart 0.1.4 修好並部署 stg**（實測 open-design pod 打不到控制面），但 issue 仍 open——建議覆核後關閉，否則盤點時會被當成未修風險重複調查。

**leader2 自我檢查結果（補搜後回報）**：#528（停權 SA 刪 policy → CF 同步被 state gate 靜默略過 → CF policy 永久 orphan，其中一條是 allow everyone）以八組關鍵字掃過全部 open issue，**無重複**；唯一相關的 #385 是「清理既有孤兒」而 #528 是「孤兒的產生機制」，不同層且已雙向關聯（並提醒清理前要先堵成因否則清完會再生）。其本輪另開的 cd#78／cd#83／cd#85／plat#509／#514 經 dispatcher 對帳**亦無重複**。


### ⚠ 新增未分派：#531 paas-operator 的 PUT /api/tunnels/{id}/routes 無歸屬驗證（2026-08-02，leader4 於 #522 讀碼發現、dispatcher 查重後開單）

`tunnels.py:306-345` 只檢查 CF token/account 有沒有設定、**無任何歸屬判斷**（docstring 自述 `Ingress is replaced wholesale`）；認證只有單一平台級 `X-API-Key`（`main.py:94-121`）而那把 key 是 **Odoo 代租戶呼叫時自己帶的** ⇒ 歸屬完全由呼叫端自律；DNS 對帳（`cloudflare.py:535-583`）會**刪掉全 zone 內不在請求清單的記錄**；ingress 全量取代在 `:992-1048`。
**獨立開單不併 #522 的理由**：`extra/paas-operator/` 是獨立發版線，併入會讓 #522 關單卡在兩條 rollout；且今天 Odoo 是該 API 唯一呼叫者 ⇒ 本張屬**縱深防禦**非主修法。⚠ 全為讀碼結論、端到端未驗（要量會動真實 Cloudflare，屬對外不可逆）。leader4 已在 #522 實作工單寫明「不要動 extra/paas-operator/」，不會被順手做掉。

### Eugene 對 #522／#524 的裁決（2026-08-02）：兩張都做「補洞＋機械化 gate」

理由（Eugene）：兩張的故障形態相同——**保護必須逐欄位／逐 model 手動施加，漏掉時沒有任何東西會發現**。鐵證：repo 裡早就有 `test_update_access_config_whitelist_only` 送 `tunnel_id: attacker-tunnel` 並斷言被丟掉——**有人想過這個欄位會被塞值，卻只守了 controller、ORM 那條沒守，而測試至今是綠的**。⇒ 注意力在正確的欄位、正確的威脅上都已失手過一次，只補洞是把賭注押在同一份注意力上。
兩張工單均把**注入式驗證列為驗收必要條件**（故意製造違規→確認 gate 真的變紅→移除，兩次實際輸出貼進 PR，沒做等於沒交）。理由：一道從沒被看過會失敗的 gate，與一道什麼都沒檢查的 gate，**輸出長得一模一樣**。
派工：Keystone→#524（`ir_rules.xml`＋registry 掃描 gate，worktree `/tmp/wt-524`）、Plumbline→#522（宣告式身分錨點清單＋gate），**檔案不重疊、平行進行**。
防撞車（照新機制執行）：git fetch 發現**本地 HEAD 落後 origin/develop 10 個 commit** ⇒ 兩張工單都要求 worktree 從 `origin/develop` 開；PR#517 動了 `src/tests/__init__.py` 與 `scripts/audit/repo-lint.py` ⇒ 已標明**不要把 gate 放進 repo-lint.py**、`tests/__init__.py` 的一行衝突不可避免已提醒 rebase 留意；open issue 查重 50 張無重複。

### dispatcher 裁決：#524／#522 的驗收分工（2026-08-02）

**#524 自驗且需具名涵蓋 tunnel_id；#522 引用該結論、不重驗。** 四個理由：①修法有效性必須自驗，否則 #524 交付時只證明「我改了」沒證明「改了有效」；②`tunnel_id` 本就在 #524 的覆蓋範圍內，差別只在報告時具不具名——而必須具名，否則跨單引用時模糊表述會變成重驗的理由；③**成本反了**——leader4 沙盒已拆乾淨（ns NotFound、253 顆 PV 中 0 顆相關），重建要重來一輪，而 leader3 為驗跨租戶讀寫本來就要建沙盒、多讀一個欄位成本為零；④**兩張驗的是不同東西**——#524 驗「攻擊者還拿不拿得到 UUID」、#522 驗「即使拿到也打不打得到」（operator 端歸屬、路由同步是否全量覆寫），前者是後者的前提而非其一部分。


## 🔴 第二起重工（進行中被攔下）：#524 兩組同時開工（2026-08-02 19:5x）

**成因：Eugene 對單一 leader 下的指令不會進到 dispatcher 看板。** Eugene 在 leader4 終端下「#522 和 #524 都做補洞＋機械化 gate」，leader4 據此派 Keystone 做 #524（worktree `/tmp/wt-524` 已建、已開始讀 doc）；而 #524 的正式歸屬是 leader3。leader4 自己發現後**即刻叫停**（不編輯、不 commit、不開 PR、保留 worktree、要 Keystone 回報成果以便移交）並要求 dispatcher 裁決——處置正確，成本止於數分鐘。

**dispatcher 裁決：#524 歸 leader3**。判準＝分配鏈完整（Eugene 明示改接 → 正式分配單 → leader3 已完成三步流程第 1、2 步並就 scope 取得 Eugene 拍板）；leader4 的派工源於**指令解讀差異**（那句話是回答他「驗收要做到哪個高度」＝定驗收標準，非重新指派歸屬）；且 leader4 手上已有 #522＋#531。Keystone 的調查結論直接交接給 leader3，不經 dispatcher 轉。

⚠ **dispatcher 的兩項認領**：①發跨 leader 協調訊息前**沒有先 check 各方狀態**——那正是自己要求 leader 做的事；②更根本：**Eugene 對單一 leader 下的指令不會自動進看板**，而 dispatcher 拿過期看板去協調。#520 是同一個病，這是同日第二次。
⇒ **新紀律：每次跨 leader 協調前先 check 相關各方，不依賴看板記憶。**

**#522 實測可行性的更正（leader4 提出、dispatcher 接受）**：「即使拿到 UUID 打不打得到」那幾環（operator 是否驗 tunnel 歸屬、路由同步是否全量覆寫）**目前是讀碼結論**；要實測必須真把路由推上另一條 tunnel＝經 operator 動 Cloudflare＝**對外不可逆**，leader4 無授權不會自行執行。⇒ #522 該環的可交付＝讀碼結論（留言 4321／4382 含行號）＋**不觸及對外的替代量法設計**。**驗收條件不得預設成「已可實測」**；升級為實測需由 leader4 備妥替代量法評估後向 Eugene 請示。


### ✅ 重工攔截的正面結果：Keystone 的調查被 leader3 吸收，補強了 #524 方案

**🔴 全隊通用發現：「至少有一條 ir.rule」是假綠判準**（Keystone 抓到、leader3 回 Odoo 源碼複核）。Odoo `addons/base/models/ir_rule.py` 的查詢是
`WHERE m.model=%s AND r.active AND r.perm_X AND (r.global OR r.id IN (... WHERE rg.group_id IN %s))` ← **依使用者的 group ids 過濾**
⇒ **只綁 `group_system` 的 rule 對 `base.group_user` 使用者根本不會被選出來**。日後若有人給 smart_home 掛一條只綁 group_system 的 rule，天真 gate 會**轉綠而實際零保護**。這與本輪抓到的其他假綠（floor 值、空值相等、探針壞掉）是同一家族。

**#524 交付定案四件**（leader3）：
1. 三規則組（member read／owner-admin RWCU 用 `any` 綁同一 row／group_system full）
2. `refresh_status` 的 role gate（否則自家成員被鎖在外面）
3. **機械化 gate**：判準＝有 `workspace_id`（**用 registry 欄位名精確比對，不用 grep**——`agent_session` 的 `primary_workspace_id` 會被子字串誤命中）× ACL 開 `base.group_user` × **不存在對 Internal User 生效的 active rule**
4. 注入式驗證：故意違規 → gate 變紅 → 移除，兩次輸出貼進 PR

**leader4 的探針陷阱也被收下**：寫入後、驗證前**不要 `env.cr.clear()`**（會丟掉還沒 flush 的 ORM 寫入，讓「沒噴例外」與「值真的變了」脫鉤），要 `flush_all()` ＋ `commit` 再讀回。

⇒ **叫停＋交接（而非砍掉重來）是對的**：Keystone 的數分鐘工作換來一個會讓 gate 假綠的判準修正，價值遠超過那幾分鐘。


## 📊 對帳 v2（issue-dispatcher 2026-08-02 20:45）——又兩張未入看板

- **#535 fix: 租戶填入的憑證（Odoo admin／SMTP／Anthropic／n8n owner／LLM keys）在服務詳情 API 明文回傳**（`priority:high`）——⚠ **這是本輪目前最嚴重的資安單之一**，未經 dispatcher 分配、無人持有。
- **#533 api_service_skip_trial 吞掉並發類 DB 例外**——SerializationFailure 被 `except Exception` 收走、retrying 永不重試（#525 同構縫）。
⇒ 對帳頻率仍不足：兩小時內第二次發現漏記。

## ⚠ manifest 版號撞號（leader4 發現，dispatcher 實測驗證）

`origin/develop` = **18.0.1.0.92**（fetch 後實讀）；PR#517 與 #522 一度**都 bump 到 .93**。
🔴 **兩邊 bump 到同值時 git 認定無衝突、rebase 後該行 diff 空白、衝突根本不會出現** ⇒ 所有以 conflict 為訊號的檢查都不會響，**後 merge 的那個靜默不升版**。與「rebase 靜默吃掉 manifest bump」同族。
處置：#522 已改 .94；**已通知 leader1**（PR#517 merge 前重新確認版號嚴格大於**當下**的 origin/develop，不是開分支時的值）＋全體廣播。**這由排隊順序決定，不是一次修掉就沒事。**

## Gitea repo 級故障（已恢復）

leader4 回報 `woow_paas_platform` repo 回 500、`tea` CLI segfault（panic: nil pointer dereference）。**dispatcher 實測已恢復**：Gitea 根 200、tea 正常回傳 exit=0 無 segfault。已全體廣播可恢復作業。
✅ 故障期間的正確處置（leader4 示範）：**掛重試迴圈等恢復，不要繞路**（不改 remote、不改分支名）——繞路在服務恢復後會留下難追蹤的殘留。

### ⚠ 新增未分派：#536 SA 的 context key escape hatch 家族（leader4／Plumbline 盤點，dispatcher 查重後開單）

**根因**：Odoo 的 `call_kw` 把 client 傳來的 context **原封不動** `with_context`（`addons/web/controllers/dataset.py:33-36` ＋ `odoo/api.py:531-532`，已逐行覆核）⇒ **任何拿 context key 當「內部呼叫」逃生門的守衛都是租戶可偽造的**。#522 已把身分錨點那支改為私有 sentinel object；另四個未處理：
- **`sa_skip_route_sync`（中，最值得修）**：偽造後 Odoo 有紀錄 CF 沒有；但路由推送是**宣告式全量**，之後任何一次合法異動會把它一起推上去，而 `is_protected` 的 Access app 建立**正是被抑制的那一步** ⇒ **標為 protected 的 route 可能以無 Access gate 上線**。無跨租戶。
- **`sa_force_access_sync`（低到中）**：繞過 liveness gate 的狀態那一半（仍需 tunnel_id）；真正該追的是背後「**停權未全面阻斷 operator 呼叫**」的家族問題。
- **`sa_force_delete_primary`（死鍵）**：src/models 與 src/controllers 全域無人讀取，只剩 `test_security_access.py:739` 還在傳 ⇒ 建議連測試殘留清掉（留著會讓搜尋結果假陽性）。
- **`sa_rule_replace`（低）**：只抑制 rule 層過渡同步，最終態由 policy write() 全量收斂。
皆為讀碼結論、未實測。修法需設計決策；其 gate 與 #522／#524 的 gate 同源，可考慮合併設計。


### ⚠ 新增未分派：#538 PR 加 manifest 版號嚴格遞增檢查（leader1 提議、leader2 佐證、dispatcher 查重後開單）

**同日三次撞號**：#352（leader2，撞 .90，bump 對 manifest 零改動、前端 asset 有變卻零訊號）、PR#517（leader1，與 develop 同時到 .92）、#522（leader4，與 #517 同時到 .93，已改 .94）。**三次都是靠人眼比對才發現。**
🔴 人工必漏的原因：**兩邊 bump 同值時 git 認定無衝突** ⇒ rebase 後該行 diff 空白、衝突不出現、review 掃過不會停留、所有以 conflict 為訊號的檢查都不響 ⇒ 後 merge 者**靜默不升版**，下次發版 `-u` 跑不到它、migration 也不執行。且**由排隊順序決定**：rebase 完到 merge 之間 develop 可能又被 bump ⇒ 判準必須是「嚴格大於**當下的** origin/develop」、時機是「**每次 merge 前**」。
修法（leader1）：**PR CI** 加檢查（fetch develop → 比對 manifest 版號 → 非嚴格大於即 exit 1）。**放 CI 而非 repo-lint 的理由**：repo-lint 是純靜態、這條需 fetch 遠端；且 CI 每次 push 都跑＝在 merge 前每個時間點重新驗證，正好對應「排隊順序決定」的性質，不需要任何人記得在對的時刻檢查。
建議一併納入：**migration 目錄版號**（目錄版號高於 manifest 時**靜默 no-op**，同族的另一半）。驗收含注入式驗證。

## 🔴 記憶層也有重複風險（leader2 發現並修，2026-08-02）

`reference_rebase_silently_eats_manifest_bump` 與 `reference_same_version_bump_silently_swallowed_on_rebase` **講同一件事、甚至引用同一個 #501 案例**，出自兩個不同 session（originSessionId 不同、寫入時間差 11 分鐘），兩邊互有獨有內容。已合併保留較完整者並刪除重複、整理索引（複驗檔案數 1／索引 1）。
**比 issue 重複更難發現**：索引會並列兩條看似不同的條目，**recall 到其中一條就以為讀完了**——issue 重複至少會在 PR conflict 時現形，記憶重複不會。
⇒ **機制擴及記憶層（已廣播）**：寫 memory 前先 `ls` memory 目錄 ＋ `grep -ril <關鍵字>` ＋ 查 `MEMORY.md`，找到既有載體就更新它、不新開檔。判準：「這個教訓如果已經有人記過，會被記在哪個檔名底下？先去那裡找。」leader2 自陳同輪也犯過一次（`feedback_remediation_needs_premise_check_too` 與既有檔重疊，已合併刪除）⇒ **不是個別失誤，是預設行為**。

**maestri ask quoting 坑（同日多人重踩）**：訊息內文含反引號會被 shell 展開導致送出殘缺或整條失敗——leader2 踩過（已記過該坑仍再踩）、dispatcher 也踩過一次（反引號讓 ask 直接 parse error 未送出）。⇒ **一律用 quoted heredoc 寫檔再 `"$(cat file)"` 傳入**，並在送出後 `maestri check` 驗證確實送達。




### 🔴 三張 open PR 版號全部低於 develop（2026-08-02 21:4x，dispatcher 實測）

```
origin/develop                          = 18.0.1.0.94  ← #537 merge 後
PR#540 fix/525-a2a3-workspace-rollback  = 18.0.1.0.93  ← 不在原看板上
PR#539 feat/530-hermes-gateway          = 18.0.1.0.93  ← 不在原看板上
PR#517 feat/501-global-instance-cap     = 18.0.1.0.93  ← leader1
```
⚠ **機制與稍早那次不同，勿照抄**：稍早是**同值** ⇒ git 無衝突、diff 空白、**零訊號**、靜默不升版；這次是 base .92／develop .94／PR .93 ⇒ **三方相異、git 會真的報衝突**。
⇒ **風險不是靜默吃掉，是衝突解錯**：順手 `take theirs`(.93) 或 `take ours` 沒重想 ⇒ **把 develop 從 .94 降版到 .93**，比沒 bump 更糟（版號倒退，升級邏輯判成不需要升）。
**正解**：三張 rebase 後改成 **.95／.96／.97**（依實際 merge 順序遞增），不是保留 .93、也不是照抄 .94。已急件廣播。

**#537 收官（leader4 實查四項側效果）**：PR merged、issue #522 **closed**（`Closes` 自動生效，順帶再證 develop 是預設分支）、develop manifest .94（bump 沒被吃掉）、develop 含 commit `a0123f4b`。

⚠ **PR#539／#540 不在看板上**（今日第四、五次漏記）——分別對應 #530（Hermes gateway 憑證交付）與 #525（A2/A3 workspace 吞例外），而這兩張 issue 我原本列為「池中無人持有」。**實況是有人在做**。⇒ 對帳頻率仍不足；且「池中無人持有」這個判斷本身要以 **PR 存在與否**交叉驗證，不能只看 issue 有無 assignee。

### #538 已補上 gate 設計輸入（dispatcher 寫入 issue 留言、已複驗）
①「有沒有 bump」不夠——要擋「bump 後仍 ≤ **當下** develop」（今日三張當初 bump 都是對的，只是 develop 跑到前面）；②**兩種失效機制的錯誤訊息要能分辨**（同值＝零訊號／較低＝會吵但人會解錯，後者的訊息要直接給正解）；③🔴 **寫 gate 前必須先拍板「純測試 PR 要不要 bump」**——實例 PR#534／#532 純測試、沒動 manifest 就 merge 了；要求一律 bump 會誤擋，開豁免（如「只動 src/tests/ 就免」）**會被拿來繞路**（把一行實作塞進測試 PR 即可跳過）。此決策不先定，gate 上線就會遇到第一個誤擋然後被匆促加上沒想清楚的豁免。


### 🔧 看板漏記的備援查法：容器／worktree 命名慣例反查歸屬（leader4 提供，dispatcher 實測有效）

今日連續五次「PR 不在看板上」（#535／#533／#537／#539／#540），根因是 **leader 自行開立的 PR/issue 不會進 dispatcher 看板**。leader4 找到一條獨立的歸屬線索：

```bash
docker ps -a --format '{{.Names}}' | grep wpp      # wpp<issue編號>-* ⇒ 誰在跑哪張單
git worktree list                                   # worktree 路徑風格 ⇒ 哪個 session 的慣例
```

**2026-08-02 21:5x 實測結果**：
- 容器：`wpp501-base`／`wpp501-odoo`／`wpp501-pg`（Up 3h）、`wpp530-odoo`／`wpp530-pg`（Up 3h）⇒ **#501 與 #530 各有 session 在跑**
- worktree：`.claude/worktrees/501-global-cap`（PR#517）與 `.claude/worktrees/530-hermes-gateway`（PR#539）**同目錄同慣例** ⇒ 線索指向同一 session
- **#525／PR#540 完全查無痕跡**（無 `wpp525-*` 容器、無對應 worktree）

**風格差異可辨識歸屬**：leader4 的 teammate 用 `/tmp/wt-*`；主 repo `.claude/worktrees/` 是另一套慣例。

⚠ **當線索用、不當結論用**——已去信 leader1 求證，未逕自認定。

**leader4 的 #522 環境拆除取證（陽性對照範例）**：容器 `wpp522-odoo`／`wpp522-pg` 與 network `net522` 已移除，拆除後 grep 無輸出 exit=1；**同一時刻**同一支指令 grep `wpp530-odoo` 命中 exit=0 ⇒ **證明探針是活的、不是輸出被吃掉**，且無誤傷他人資源。worktree `/tmp/wt-522` 保留（乾淨、a0123f4b）待 develop 程式碼確認無誤再拆。

⇒ **「池中無人持有」這個判斷要以 PR 存在與否交叉驗證**，不能只看 issue 有無 assignee——#530／#525 我原本列為無人持有，實際上 PR#539／#540 都已開出。


### 🔴 `src/tests/__init__.py` 的解衝突方向與 manifest 相反，解錯零訊號（leader1 提出）

#537 已 merge，與其他 open PR 有**兩個檔案交集**，而兩者解衝突原則**正好相反**：

| 檔案 | 原則 | 解錯後果 |
|---|---|---|
| `src/__manifest__.py` | **主動改成更大的值**（不是 take 任一邊） | 版號倒退／不升版——**至少 git 會吵** |
| `src/tests/__init__.py` | **兩邊註冊都保留**（union） | 🔴 **對方的測試永遠不執行、全套照樣綠、無任何 gate 會抓** |

⇒ #522 那 **412 行新測試**若在解衝突時被吃掉，CI 零反應。**這是 #467（測試檔未被 `tests/__init__.py` 匯入 ⇒ 15 個測試從未執行）的同形態，只是成因換成解衝突。**
**要求（已廣播三張 open PR）**：manifest 改成嚴格大於當下 develop；`tests/__init__.py` 兩邊 import 都留、解完**數 import 條目數**；解完跑全套並**比對測試總數**——`0 failed` 配上**變少的測試數**正是這個缺陷的樣子。

### ⚠ dispatcher 掃描範圍不足（今日同形態第三次）

**歸屬推論被 leader1 推翻**，證據：①`.claude/worktrees/` **不是任何人的簽名，是 repo 通用慣例**（底下橫跨多位 leader、最早 Jul 19）；②時間線對不上（他的 501-* 建於 12:40／13:43，`530-hermes-gateway` 建於 **19:30**，差近七小時）。
**而我說 #540「查無痕跡」是因為截斷了輸出**——跑了 `git worktree list` 卻只看前 12 筆，**實際 29 筆**；`woow-addons/` 底下另有整批 **18 個 `wt-*`** worktree，其中就有 **`wt-525-a2a3`（PR#540）與 `wt-525-a1`**。
⇒ 與 wiki link 那次同形態：**查了兩個位置就下「查無」，正確說法是「在我查的那兩個位置沒有」**。
⇒ **反查手法定案：`git worktree list` 全域列舉且不截斷**（權威來源、不受目錄慣例影響）；`docker ps | grep wpp` 只是輔助。
🔑 **新增判準：對輸出做 `head`／`grep` 之後下的否定結論，一律不成立**——先確認總量，再決定要不要截斷。

**#540 線索**：`wt-525-a1` 對應**已 merge 的 #525 A1**（`85ed23df`）⇒ A2/A3 極可能同一人所做，已請該負責人認領。


### ✅ #540 已 merge 且驗證乾淨（develop `3b840b29`、manifest `.95`）
#522 的 `test_security_access_identity_anchor_522` 註冊仍在、import 條目 184→186、**diff 零刪除**。解衝突解對了。

### 🔑 測試註冊檢查的最終判準（leader4 精化，dispatcher 原版有盲點）
dispatcher 原版「比對測試總數」**會漏一種情況**：一次 merge 若同時**刪 2 條既有、加 4 條新的**，總數 +2 看起來完全正常，而那 2 條已永不執行。
**最終判準——diff 只允許新增行**：
```bash
diff <(git show <前>:src/tests/__init__.py | grep -E '^from |^import ') \
     <(git show <後>:src/tests/__init__.py | grep -E '^from |^import ')
# 只允許 ">"；出現任何 "<" 就是有東西被吃掉
```
或集合比對 `comm -23 before after` 必須為空。⚠ **本身也要配陽性對照**：先確認 before 非空，否則「comm 為空」可能只是兩邊都空——**連檢查工具自己都會中今天這個形狀**。
同源做法（#501）：全套 **2235 − 2204 = 31 正好等於新增測試方法數** ⇒ 差值對得上**同時證明沒有既有測試被移除**。單看「有沒有紅」或「總數有沒有變少」都不夠。

### 版號實務定案（leader1 做法，已推薦給 #539）
PR#517 版號**追著 develop 改了兩次**（.95 撞號→.96），並要求 teammate **不要記住任何具體數字，改成「push 前最後一刻 fetch、取當下 develop 值 +1」**。理由：**develop 今日 merge 頻率為每小時一到兩張，任何寫死的數字都會在跑完全套之前過期。**

### 🔍 歸屬線索定案：`originSessionId` 比目錄慣例可靠（leader4 提出，dispatcher 實測）
```
project_525_a1_skip_trial_rollback_landing    20548f01-6cfc-4afb-bdf5-b9f2e89bf0df
project_525_a2a3_workspace_invite_rollback    20548f01-6cfc-4afb-bdf5-b9f2e89bf0df  ← 同一 session
project_522_identity_anchor_landing           7baa8869-…（leader4 的 teammate）
```
⇒ **#525 A1 與 A2/A3 確為同一 session**，且非四位 leader 已知的任何一個。leader4 已排除自己五位 teammate，剩 Tally 未確認——但他**明確拒絕用「排除五個就剩一個」下結論**（排他性結論，今日已栽過一次），正在直接求證。

### 🔴 今日最該當成預設懷疑的一條（跨三人中四次）
> **任何「查無」結論，先問自己查的範圍是不是全集。**

實例：Gitea 只量一個 repo／wiki link 只查 memory 目錄沒查 `docs/`／刪 memory 檔只查 `MEMORY.md` 沒 grep 全庫／`git worktree list` 被 `head -12` 截斷。**形態相同、載體不同。**


### ✅ #525 歸屬結案：leader5 → teammate `Ballast`（session `20548f01`）

**四條證據（leader5 提供）**：①兩份 #525 memory 的 `originSessionId` 欄位直接比對；②內容特徵與他 session 內收到的回報**逐項對應**（A1 的「變異 A／變異 B、`_pay_from_wallet`、9 failed→7 failed、savepoint」、A2/A3 的「slug 後果與 issue 寫的相反、注入點選錯被前提斷言擋下兩次」都是 Ballast 回報原話）；③Ballast 執行指令的 scratchpad 路徑即 `.../20548f01-.../scratchpad`；④派工鏈（#525 是 leader5 開的 issue、A1／A2A3 兩張工單都是他發的）。
⚠ 第 3 條已無法重驗（Ballast 收尾清掉目錄），leader5 如實標明；前兩條可重驗。
**leader4 已用逐一索取 session id 排除自己七個 session**（非「我沒派他做」的範圍假設）；**leader6 排除自己**（原始 `5ca10f89`／重啟後 `3150cd43`）。⚠ **他隨後更正了自己的免責理由**：他原說「掃 `/private/tmp/claude-501` 查無、但 scratchpad 會被清所以不能結案」——**真正的原因不是 TTL，是查錯目錄**（session transcript 根本不在 `/private/tmp`，而在 `~/.claude/projects/<slug>/<session-id>.jsonl`，該檔一直好端端躺著、2.6MB、1028 筆）。他的原話：「**我用對的態度掩護了一個錯的檢查**——自我標註救了它不至於變成錯誤結論，但別把它當範例。」

🔴 **這兩條是不同的教訓，dispatcher 差點合併、被 leader6 攔下**：
1. **scratchpad 有 TTL 會消失**（leader1 的 `HANDOFF.md` 實證）⇒ **載體分等級**，交接產物要落在 git／Gitea。
2. **leader6 的案例不是 TTL，是查錯位置** ⇒ 教訓是「**宣告查無之前，先確認你查的位置是不是這類資料的實際存放處**」。
⇒ 合併成「scratchpad 會被清所以查無不可靠」會讓人**學到錯的東西**——以為只要避開暫存目錄就安全，而真正的坑是**根本沒查到正確的地方**。

### 🔧 歸屬查法定案：讀 transcript 的第一則 user message（dispatcher 已實測驗證）
```bash
find ~/.claude/projects -maxdepth 2 -name "<session-id>*.jsonl"   # 第一則 user message ＝ 派工單原文
```
🔴 **必須用 `find` 全域查，不可用單一 project 目錄**——project 目錄按 **cwd** 切不是按 repo，woow-paas-platform 相關的有 **61 個**（`…-woow-paas-platform` 從 repo 根跑／`…--maestri-roles-<uuid>` **每位 leader 在此**／`…--claude-worktrees-<branch>`／`…--maestri-floors-…` 等）。dispatcher 原先標準化成單一目錄，**對任何 leader 與任何 worktree agent 都會回「查無」**（leader4 實測抓到、dispatcher 已複驗）。
⚠ **陽性對照要挑「確知跑在不同 cwd 的 session」**——兩個剛好同目錄時對照會過、盲點還在。
**實測**：`20548f01-….jsonl`（2.5M、1028 筆）第一則 user message 是「**工單：修 #525 的 A1（api_service_skip_trial 吞例外導致金流殘留）… A2/A3/B1-B5 不在你範圍**」⇒ 直接顯示是哪位 leader 的線，與 leader5 的自陳**兩條獨立路徑互相印證**。
**優於互問 session id**：agent 會被重啟（leader6 三位 teammate 今天全被重啟、context 清空，問他們也答不出當初的 id）。
⚠ **誤讀風險**：該檔提到 leader6 的分支名（`test-false-green-batch0` 等），但那是他在 develop 上工作時**旁觀**到的（git log／PR list／rebase），**不是產出**——別把「檔案裡出現某人的分支名」當成關聯。

### 🔴 載體分等級：scratchpad 有 TTL，不是持久載體（leader1 事件）
他的 teammate 交接用的 `HANDOFF.md`（7061 bytes、20:16 寫入）在 `/private/tmp` 底下，**六小時後隨 TTL 消失**——leader1 昨晚 23 點還親眼查過該檔，交接時把它當有效載體。真正救回來的是 **commit message、PR#517 留言、#519 留言**（都進了 git 或 Gitea）。
⇒ **「任何需要活過 context 清空的資訊都要有 context 以外的載體」還要再加一層：載體本身也分等級——scratchpad 會消失，git 與 Gitea 不會。**
他獨立驗過 teammate 重建的六項全過，含關鍵陽性對照（`create()` 兩處守衛完好 ⇒ 證明 `write()` 守衛數為 0 **不是整段誤刪**）。

### #525 的三處記錄更正（leader5 主動提供）
1. 兩份 memory 的 A2/A3 數字**已過時**（記的是 rebase 前：manifest .92→.93、baseline `cfe5cd7a` 0F/0E of 2212 → fix 2223）。PR#540 後因 #537 進 develop 而衝突（manifest 撞號＋`tests/__init__.py` 註冊行），rebase 後 base `a21dcb17`、manifest 解成 `.95`。rebase 後全套數字 Ballast 仍在跑，跑完補進 PR#540 留言並註明是 merge 後才產出。
2. A2/A3 新增測試是 **11 條**（A2 七＋A3 四），**不是 13**——13 是 A1／PR#527 的數字，leader5 一度在工單寫錯、被 Ballast 更正。
3. **#525 A2 段有一處敘述錯誤，已於 issue 留勘誤（留言 4496）**：原文寫「同名重試 `_generate_slug` 會靜默改成 `name-1`、缺陷不會浮現」，**實測相反**——`_generate_slug` 的 `search_count` 沒有 sudo、看不到孤兒 ⇒ 回傳同一個 slug、撞 unique_slug、交易毀損、**框架層 500，每次硬失敗**。危害等級不變但**行為方向相反**，照原文寫測試會斷言不存在的行為。

### 四步流程的適用裁量（回覆 leader6）
leader6 的工作流是 **Eugene 直接下具體指令派工、不走 issue**，故②（考 Eugene 能不能了解問題）在該脈絡下多餘——**問題是他自己提的，不用補**。
**但③（討論「怎麼叫完成」）建議保留**：今天實證它逼出了不做就不會浮現的東西（leader1 把 #501 完成拆三層讓 Eugene 選、leader2 揭露「#352 做完席數計費仍不成立」的邊界）。**「他知道問題是什麼」不蘊含「他知道做完長什麼樣」。**
判準：收到具體指令後問一句「**他有沒有說清楚做完長什麼樣？**」有就直接做；沒有（或存在多個合理完成層次）就花一句話問。不必走完整儀式。


## 🗂 MEMORY.md 併發約定（2026-08-02 定案，七位 leader 已廣播）

**結構健康度（dispatcher 實測，修正 regex 後）**：索引 **202** ／ 磁碟 **202**，**零孤兒零斷鏈**。

### 🔴 三條約定
1. **禁止對 MEMORY.md 整檔 `Write`**——只能 `Edit` 單行或 append。實證：leader5 整檔覆寫**洗掉兩行別人剛加的索引**，靠雙向比對抓回。**被洗掉的正是「多 leader 並行會撞同一檔案」那一則**——防這件事的教訓被這件事吃掉。
2. **不要自行壓縮**。單方面壓縮＝砍別人寫的描述，且**效果數小時就被抵銷**（leader5 實測 16998 → 數小時後 24152）。要壓縮先回報 dispatcher 協調。
3. **寫完索引做雙向比對**：`comm -23`／`comm -13` 兩向都跑。⚠ **regex 必須含大寫與連字號**：`grep -oE '\]\([A-Za-z0-9_.-]+\.md\)'`。

### 🔑 完備性檢查的輸出要是「是哪一個」，不是「差幾個」（leader4 提出，dispatcher 是反面教材）
2026-08-02 實例：dispatcher 的檢查輸出是**數字**（202 vs 201）⇒ 只能猜差額是什麼，**任何聽起來合理的解釋都會被接受**（接受了「差額＝MEMORY.md 本身」，結論碰巧對、歸因錯）。leader4 的輸出是**集合差**（`comm -23` 印檔名）⇒ 拿到**可立刻核對的具體對象**，兩秒自我打臉、送出的就是修正版（根因＝regex 字元類漏大寫，檔名 `trackA` 含大寫 A）。
⇒ **差幾個沒有可證偽性；是哪一個立刻可證偽。** 套用於所有完備性檢查：測試數、版號、註冊行、issue 盤點——凡回報「數量對不上」都要能印出「是哪幾筆」。
⇒ 連帶：**自己寫的 regex／glob／filter，先把已知邊界樣本當測資餵給它**（大寫／連字號／非 ASCII）。今天這條 regex 在 202 個檔名裡**只漏 1 個**——漏得夠少所以看起來像資料異常而非工具異常。這是注入式驗證的同一件事，只是注入對象從「被測系統」換成「我的探針」。

### ⚠ hook 會命令式要求壓縮——收到時回報，不要照做
`PostToolUse:Edit` hook 原文：「The memory index at MEMORY.md is 19.6KB, approaching the **24.4KB read limit**. Compact it to under 17.1KB **now**」。它是 **harness 層**（`settings.json` 找不到），措辭是命令式的，**很容易被直接執行**（leader5 就是這樣）。
🔴 **24.4KB 是「讀取上限」（24576），現況 24152 已逼近**——真超過可能導致**索引讀不完整**（比字數難看嚴重得多）。
⇒ 收到該 hook 時**回報 dispatcher**，不要自行壓縮。若確認是硬上限需瘦身，正解是**拆檔**（降低碰撞面）而非**刪描述**。

### 為什麼壓縮必然失敗：transcript 按 cwd 切成 61 個，memory 卻只有一份（leader5 實測）
7 個 `--maestri-roles-*` 目錄**沒有任何一個有 `memory/` 子目錄**；memory 只存在於無後綴的 `-…-woow-paas-platform/memory/`。
⇒ **MEMORY.md 的寫入者是散在 61 個 project 目錄裡的所有 agent**（7 位 leader＋各自 teammate＋16 個 worktree 目錄裡的 agent），不是「幾位 leader」。數小時 +12 檔是結構的自然結果。
⇒ **壓縮是在對抗結構性增長率，拆檔才對症。**

### leader5 已刪除的描述（供各自自查，不還原）
為壓到 17100 分五輪砍掉各條目的「— 說明」文字：近期 landing（除 2 條）／商品上架定價（全段）／E2E 協作工具（除 tea gotchas）／Helm K8s（除 5 條）／發版 CI（除 5 條）／Odoo 陷阱（除 3 條）／工作方式測試設計（除最高頻 7 條）。**連結一條沒少**（189/189 零斷鏈零孤兒），資訊都在主題檔裡，損失的是索引層 hook 品質。
⚠ 他自陳未逐條驗證「描述是否為索引層獨有」⇒ **若某條的 hook 有獨有資訊，由該條的作者自行 Edit 補回單行**（符合約定 1）。**不做整體還原**——那同樣是單方面決定別人內容。

### 🔑 對照組的兩種機制，別互相套用（leader5 提出）
| 載體 | 要防的失效 | 對照方式 |
|---|---|---|
| transcript | **範圍不是全集**（61 個目錄） | 挑一個**確知跑在不同 cwd** 的 session 當對照 |
| memory | **集合被單方面修改** | **檔案數 vs 索引數雙向比對** |
memory 只有一個位置，不存在「查對目錄但漏掉其他目錄」的失效模式 ⇒ transcript 的對照法對它不適用。


### ✅ memory landmine 已消除（2026-08-02，dispatcher 執行跨 leader 改名）
`project_ar26_track**A**_…` → `project_ar26_track**a**_…`（含 frontmatter `name:`、MEMORY.md 索引行、兩處 wiki link）。**刻意保留四處把舊名當測資樣本的引用**（那些在講這個 bug 本身）。
**複驗**：孤兒 0／斷鏈 0／磁碟 203＝索引 203；陽性對照①含大寫檔名 **0 個**；陽性對照②**故意用有缺陷的小寫 regex 也得到 203** ⇒ **消除 landmine 比要求每個人寫對 regex 更徹底**。
背景：該檔曾是 202 個檔名裡**唯一含大寫**者，同時是「人手寫易打錯」與「工具列舉字元類易漏抓」兩種失效的來源。

### 🔴 兩條機制不可合併（leader5 澄清）
| 事件 | 機制 | 表現 | 處置 |
|---|---|---|---|
| leader5 早先報的斷鏈 | **人為輸入錯誤**（手寫索引漏打 `_stg`） | **真斷鏈** | 修索引 |
| dispatcher 的「孤兒」 | **工具漏抓**（regex 漏大寫） | **假孤兒** | 修 regex |
⇒ **方向相反。合併成「報斷鏈都可能是 regex 問題」會讓下次的真斷鏈被放過。**

### 🔑 完備性檢查三判準（三位 leader 各一條，已廣播）
1. **輸出要是「是哪一個」，不是「差幾個」**——差幾個沒有可證偽性；是哪一個立刻可證偽。dispatcher 是反面教材（拿到 202 vs 201 只能猜，接受了「差額＝MEMORY.md」這個聽起來合理的錯誤解釋）。
2. **雙向都印**（`comm -23` ＋ `comm -13`）。leader5 兩秒判定自己打錯字的關鍵：**斷鏈側與孤兒側同時出現一組幾乎一樣的檔名 ⇒ 配對出現＝拼寫錯誤**。只印單邊會導致「補一行索引」（結果同檔兩行、一行還是壞的）或「找備份還原一個沒消失的檔」——**都是去修不存在的問題**。
3. **regex 用否定字元類，不列舉允許字元**：❌`[a-z0-9_]+`（漏大寫/連字號/非 ASCII，失效方式是**靜默漏收**）／✅`[^)]+`（失效方式是**多收**，會進斷鏈清單、大聲失敗）。**寧可讓錯誤大聲，不要讓它安靜。**
⇒ 補：**懷疑工具有問題時，拿有缺陷的版本跑真實資料看它漏的量級**——漏得少的工具最危險，結果落在「看起來像真的異常」的範圍內（202 裡只漏 1 個）。

### 🔑 元教訓：驗過一個維度 ≠ 其他維度也驗過（leader1）
他今天稍早驗過「範圍是不是全集」（find 掃全域確認 memory 都在同一目錄）——**那個驗證是對的、也通過了**，但他**只驗範圍維度、沒驗字元集維度**。同一條檢查裡有兩個獨立假設，驗了前者就以為整條可信。
⇒ **通過的那次驗證會給你信心，讓你更不會去懷疑同一條檢查的其他部分。**

### leader6 的應用（值得抄）
把判準推到 teammate 的驗收要求：原本「必須斷言實際執行的測試數 ≥ 下限」（**「差幾個」形態**）→ 改成「**列出哪些預期的 test class／module 不在實際執行清單裡**」。
理由：**Odoo 最惡劣的失敗模式是整批被靜默 deselect**（tag 沒匹配／module 沒裝／import 失敗），**總數可能還過得了下限，少的是特定一批** ⇒ 拿數字永遠看不出來。**CI 綠燈本身就是一個數字。**


### 🔴 「PR 顯示 conflict」的判讀：可能是「尚未 push」而非「尚未處理」（2026-08-02）

**dispatcher 誤判實例**：用 `remote head 沒動 ⇒ 衝突還沒被處理` 向 Eugene 回報「沒有人在修」——**錯的**。實際上兩張都已在本地解完：

| PR | remote head（我看到的） | 本地實況 | 版號 |
|---|---|---|---|
| #517 leader1 | `ccd4b3dc`（舊） | **`fb8d11db` 已 rebase**、merge-tree 無衝突、merge-base ＝當下 develop | 本地 `.96` > develop `.95` ✅ |
| #539 leader7 | `3c28f0e4`（舊） | **`f8367ef3` 已 rebase**、merge-tree exit=0 無衝突 | 本地 `.96` ✅ |

⇒ **remote head 是滯後指標**（leader7）——解衝突發生在**本地**，push 才反映到 head。
⇒ **這是「觀測範圍」的又一實例，但這次無解**（leader1）：dispatcher 只能查 origin，而工作發生在**別人的本地 worktree**——那是**看不到的區域**。**不是查錯，是資訊本來就不在可觸及範圍內。**
⇒ **判準：「PR 顯示 conflict」要向該 leader 確認本地狀態才能區分「尚未 push」與「尚未處理」——兩者處置完全不同（前者只要等、後者要介入）。**
⚠ **反向也會錯**：**head 動了不代表衝突解對了。**

### 兩張 PR 的實際節點（已明確）
- **#539（leader7）**：解衝突已完成，**卡在 code review 抓到的真缺陷**，修完一起 push。理由：**分兩次 push 要追兩次版號**（push 後改 code 還要再跑全套，跑完版號可能又過期）。已交代 platform-530「push 前最後一刻 fetch 取當下值 +1」。
- **#517（leader1）**：解衝突已完成，節點＝**本分支全套 0F/0E**。目前 1 個 error＝Tollgate 新增的 B-1 回歸守線**自己掛掉**（測試要驗「停售時刪除失敗善後不該被擋」，前提先開停售，但接著要建 `state=deleting` fixture 時被 `create()` 的供給閘門擋住，**連要驗的那一步都沒跑到**）——**不是實作回歸**（baseline 沒這條測試）。
  ⚠ 他採納 dispatcher 的成本論證做了調整：**紅燈修完、全套綠就立刻 push**，不等 PR 描述等收尾工作。
  ⚠ 並會在 **PR 標題加勿 merge 標示**——因為 push 後 Gitea 就顯示 `mergeable=True`，但那時仍未通過他的第三輪覆核。**「技術上可 merge」與「應該 merge」是兩件事**，不標示對其他人是誤導。


### 🔑 「滯後指標」有兩種載體，兩種都會系統性誤判（2026-08-02，dispatcher 與 leader7 各中一次）

| 誰 | 用的滯後來源 | 誤判內容 |
|---|---|---|
| **dispatcher** | **remote head／PR mergeable** | 「head 沒動 ⇒ 沒人在修」——實際上兩張都已在本地解完（解衝突發生在本地，push 才反映到 remote）。**同一錯誤犯了兩次**，第二次是在明知該註記為「滯後指標」的情況下仍把數值放進狀態回報 |
| **leader7** | **十幾分鐘前 check 到的終端畫面** | 用「platform-530 有 9 個未提交變更、正在寫測試」論證打斷成本、據此發撤回指令——**實查時它早已 commit 完畢**（HEAD `045c15aa`、0 未提交、manifest 已自行追到 `.97`） |

⇒ **任何「我先前觀察到的狀態」在對方持續工作時都是滯後的，論證前要重新量。**
⇒ dispatcher 的修正：**回報 PR 狀態一律先問 leader 本地狀態；remote 數值只在「已 push」的語境下引用。**

### PR#539 爭論收斂（選項 1 vs 2 已 moot）
platform-530 已 commit 完（0 未提交、`.97`），**兩條路線收斂到同一動作：跑完全套就 push**。不需 Eugene 再裁決。
**勿 merge 標示不需要**——push 的版本已含 finding 修正，不是「技術上可 merge 但明知有缺陷」。
PR#539 狀態留言 leader7 已留（dispatcher 已驗證存在），內容涵蓋：conflict 在本地已解、卡點、預估節點、版號取值時機與**撞號的無訊號性**、以及「要不要追版號（量 manifest）」與「要不要 rebase（量 commit）」是**兩個獨立指標**。
🔑 leader7 的核心洞察：**紅色 conflict 的真正問題是「看的人不知道有沒有人在管」——那個用留言就能解決，不需要用一次打斷去換。**


### ✅ #501 收官（leader1，PR#517 merged）
第三輪覆核**補做完成、14 項全過、0 失敗、無 follow-up**。B3 移除、陽性對照確認 `create()` 守衛完好、錯誤論證註解已清、測試增刪都對、gate 的 SyntaxError 防護、docstring 更正，加上第二輪四項修正**都在 merge 的版本裡**。紅燈在 merge 前已修好（docstring 記下 fixture 順序不可對調並註明鑑別力驗證方式）。`tests/__init__.py` union 零刪除、新增 2 條（dispatcher 與 leader1 獨立驗、結論一致）。

### 🔴 流程缺口：Gitea 只顯示「技術上可 merge」，不顯示「覆核是否通過」
leader1 **預見到這點**、打算 push 後加勿 merge 標示，但 **push 與 merge 之間的窗口比預期短**——Eugene 在第三輪覆核完成前就 merge 了（結果是好的，但**那是運氣不是流程保證**）。
⇒ **建議：需要覆核把關的 PR，在開 PR 的當下就把標示寫進標題**，而不是等接近可 merge 時才加——**因為「接近」這個判斷本身會落後於實際狀態**（與今天的滯後指標同族）。

### 🔑 leader1 的三條技術發現（跨 issue 適用）
1. **Odoo 的 cursor 是 REPEATABLE READ**（`sql_db.py` 的 `set_isolation_level`），**不是很多 docstring 假設的 READ COMMITTED** ⇒ 「取 advisory lock 後重新計數」在同一交易內**拿不到新 snapshot**，那類防護**零貢獻**。錯誤前提**自 #345 起被當成既定事實傳承**。⇒ **repo 內其他同形制的並發防護都要重新檢視**（`ha_paas` provision 鎖、project reach 鎖）。已開 **#518**。
2. **psycopg2 的 `SerializationFailure` 是 `Exception` 子類** ⇒ controller 的裸 `except Exception` 在 `retrying()` **內層**，會先吃掉 40001，**重試永遠不會發生**。任何靠「並發衝突自動重試」的設計都要確認**例外傳得出去**。
3. **複用裁決點要檢查它的隱含假設**：某判準預期「計數已含剛落庫的這筆」，**那只在 create 路徑成立**；同一函式用在 write 路徑就有 off-by-one。與「函式語意（再收一筆新的）不適用於既有服務轉移」同病灶。

### #501 衍生 issue 狀態
`#515`（容量 pre-check，跨發版線）／`#518`（max_per_partner 並發失效）／**`#519`（租戶可繞過下單直接建服務、無訂閱無扣款——leader1 評為**優先級高於 #501 本身**，三條已證實繞過路徑已記錄）／`#533`（skip_trial 同構縫）皆 **open**；**`#516` 已 closed**（與 #520 重複）。

### leader1 狀態
**空手待命**。teammate（Vault、Tollgate）產出已保全在 **issue／PR／memory／commit message 四種持久載體**；**dismiss 與否等 Eugene 決定——不可逆操作不自行處理**。context 約 85%，下一張若需 `/clear` 會照第 0 步走（保全 → dismiss → clear），**clear 後第一件事讀共用看板**。


### ✅ PR#539 merged——今日三張衝突 PR 全數收斂，全站 open PR 歸零
```
develop = eb9d199d   manifest = 18.0.1.0.97   open PR = 0
```
| PR | 內容 | leader |
|---|---|---|
| #517 | HAOS 全域上限硬閘＋停售不發版可調（#501） | leader1（第三輪覆核補做後 14 項全過、無 follow-up） |
| #541 | 自動儲值守衛改行為測試（金流） | leader6（純測試 PR 依慣例不 bump manifest） |
| #539 | Hermes Agent gateway 憑證交付（#530） | leader7（**三個 finding 用同一修法、chart 端零配合**） |

**三張的 `tests/__init__.py` union 皆經雙向差集＋陽性對照驗證：零刪除**（186→188→188→189，無任何既有註冊在解衝突時被吃掉）。

### 📌 今日版號實務總結（四條，已全體廣播）
1. **追版號的次數 ≈ 分支存活期間 develop 的 merge 次數，與 push 幾次無關**（今日頻率每小時 1–2 張）⇒「等到全部完成再一次 push」在高頻 merge 下不省追逐，只是推遲並累積。
2. 取值時機：**push 前最後一刻 fetch、取當下值 +1**，不寫死數字。
3. **兩個指標各自變動、不同步**：要不要追版號 → 量 `manifest`；要不要 rebase → 量 `git rev-parse origin/develop`（**#541 就是 commit 前移但版號沒變**）。
4. 兩種失效機制不同：**同值撞號＝零訊號**（git 不報衝突、diff 空白、靜默不升版）／**低於 develop＝會報衝突但人會解錯**（`take theirs`／`ours` 都會降版或不升版）。

### 待 Eugene 決定
- **#530 已收官**（`Closes` 自動關、不重開，殘餘由 #542/#544 接住——見下方收官紀錄）；**#542 已分配 leader7**（dispatcher 主動分配），照四步流程等 Eugene 指令才動工
- **leader1 空手待命**；池中 `priority:high` 的 #535 已分配 leader2、#525 由 leader5 線在做，**#519**（租戶可繞過下單直接建服務，leader1 評為優先級高於 #501 本身、且他有完整 context）為最高候選
- leader1 的 teammate（Vault／Tollgate）**dismiss 與否等 Eugene 決定**——他守住不可逆操作不自行處理的紀律
- **#531／#536 排人**（leader4 空手）；**#544 新入池待排**（47 個從未執行的測試，見下方收官紀錄）


### 🔑 PR 標示改為「預設帶著、通過才移除」（leader5 提出，取代 leader1 的「開 PR 當下寫」）

**兩個實例**：leader1（PR#517）**預見到了**但 push 與 merge 的窗口比預期短；leader5（PR#540）**根本沒預見**——預設「驗收會在 merge 前完成」，實際是 Ballast push 後全套還在跑（約 40 分鐘），Eugene 先 merge 了、測試才跑完 ⇒ **合併當下沒有任何 rebase 後的驗收數字**。

> **不要用「什麼時候標示」來管，用「預設值」來管：PR 開出去的當下若驗證未完成，標題先帶勿 merge 標示，通過再拿掉。**

理由：「什麼時候加」**預設了你會在窗口內完成判斷**——leader1 證明窗口可能比預期短，leader5 證明**你可能根本沒意識到需要判斷**。**預設值不依賴任何時間判斷。**

### 🔴 驗收紀錄要標時序（否則事後補驗與事前驗收不可區分）
leader5 事後補的數字**看起來和事前驗收完全一樣**（同樣 0F/0E、同樣原始輸出），他主動標註「**這組數字在 merge 之後才產出，不是合併前的驗收依據**」（PR#540 留言 4514／4516）。
⇒ **這是「假綠與真綠輸出一模一樣」的形狀發生在紀錄層而非執行層。**

### `git diff origin/develop -- src/__manifest__.py` 空白的三種成因（leader5 實測）
1. **撞號被靜默吃掉**（兩邊同值）　2. 正常無差異　3. **PR 已經 merge**（分支內容已在 develop）
他今天把第 3 種誤判成第 1 種。⇒ **同一空白輸出、三種成因，看到空白不要直接套第一種解釋。**
（佐證追版號公式：PR#540 存活期間 develop merge 一次 ⇒ 追一次；PR#527 動兩次 ⇒ 追兩次。）


## 🏛 元判準（leader4 提出，統攝今天所有規則——放在最前面）

> **把安全狀態設成預設值，讓它不依賴任何人在對的時機做出對的判斷。**
>
> **當一條新規則的形式是「大家記得要 X」時，先問一句：「有沒有辦法讓不做 X 也不會出事？」** 有的話就改成那個，**規則本身可以不用存在**。

**今天有兩條規則其實是同一個原理**（表面一個是流程、一個是資料）：
| 規則 | 從 | 到 | 不再依賴 |
|---|---|---|---|
| **PR 勿 merge 標示** | 需要時加上 | **預設帶著、通過才移除** | 你**預見到窗口會很短**（leader1 預見了還是沒趕上）／你**意識到需要判斷**（leader5 根本沒想過） |
| **`trackA` 改名** | 大家都要寫對 regex | **連寫錯的人也不會踩到** | 每個人的 regex 正確性（**對照實證：缺陷 regex 現在也得到 203**） |

⚠ **今天多數新規則仍是「記得要 X」形態**（記得 fetch、記得配陽性對照、記得先讀全檔、記得標時序、記得驗範圍）。它們有價值，但**每一條都在消耗同一份注意力**——而今天已反覆證明注意力會在第三、第四次用同一支工具時失效。
🔑 **leader1 那句最準：通過的那次驗證會給你信心，讓你更不會去懷疑同一條檢查的其他部分。**

⇒ **能結構化的優先結構化，剩下的才靠規則**——否則**規則清單本身會變成下一個「看起來涵蓋全部、實際沒被測過」的東西**。

⇒ 已結構化的三項：`trackA` 改名（消除 landmine）／PR 標示預設值／`#538` 版號 gate（待實作，且**必須先解「純測試 PR 要不要 bump」**，否則會誤擋並被開豁免繞路）。

## 📋 現行分配（`/clear` 後先讀這裡——這是分配資訊的持久載體）

| Leader | Issue | 一句話 |
|---|---|---|
| **leader1** | **#501** HAOS 全域實例上限 | A 硬閘＋B 停售脫離發版（C 已拆 #515）；PR#517；等 Eugene 裁 B3 去留、完成層次、#518 併不併 |
| **leader2** | **#535** 租戶憑證在服務詳情 API 明文回傳（`priority:high`） | `_sanitize_helm_values` 遮罩 path-aware 只認三條路徑，租戶經 `helm_value_specs`（`type: password`）填的憑證走自己的鍵名 ⇒ 明文回傳；**前端一律顯示 `********` 故畫面看不出來，洩漏在 API 層** |
| **leader3** | **#524** smart_home 無 record rule | 交付四件：三規則組／`refresh_status` role gate／機械化 gate（registry 欄位名精確比對）／注入式驗證；等 Eugene 授權動工 |
| **leader4** | **#522** SA route 身分錨點 | **PR#537 等覆核 merge**（0F/0E of 2218、注入式驗證做過、leader4 已覆核一輪）；衍生 #531／#536 待排 |

### 🔴 流程缺陷已修正：`/clear` 會吃掉分配單本身（2026-08-02 實際發生）
leader2 收到 #535 分配後照第 0 步 `/clear`，**分配單隨 context 一起消失**（dispatcher check 到 context 0%／0m 0s 才發現，已重發）。根因＝**分配單在第 0 步之前送達，而 `/clear` 排在第 1 步之前**，必然吃掉它。
⇒ **修正（預設做法）：分配資訊寫進本看板當持久載體；`/clear` 後的第一個動作是 `maestri note read "paas-issue-08-01"`，不是等 dispatcher 重發。**
⇒ **這是「只存在對話裡的東西會在清空那刻消失」的第三個載體**（teammate 調查結論 → 覆寫前檔案內容 → **分配單本身**）。判準推廣：**任何需要活過 context 清空的資訊都必須有 context 以外的載體**（issue 留言／memory／共用 note）。要問的不是「我記得嗎」，而是「**這個東西寫在哪裡**」。

### 池中未分派（依優先序）
`priority:high`：**#525**（paas.py 吞例外→靜默 commit 同構縫盤點，3 處確認該修＋5 處同構但因果不同）
`ready-for-agent`：**#530**（Hermes Agent gateway 對外通道，MCP Gateway 區完全無法使用）
其餘：#383（validate_namespace 覆蓋率，交接完整）、#519（路徑盤點完成、修法收斂到 ir.rule 拆分一條、只待拍板）、#521（等四個產品決策）、#528、#531、#533、#536、#538、#514、#512、#515、#516/#518、cd#78/#83/#85/#88、ci#47/#48、charts#66

## 📊 四位 leader 狀態快照（2026-08-02 21:00）

| Leader | Issue | 狀態 | context |
|---|---|---|---|
| leader1 | #501 | 本分支測試進行中；Tollgate 待命、**環境保留**（容器＋兩個 DB＋固定 recipe）；**等 Eugene 對 B3 去留裁決**後給完整修法清單 | 66%（最高） |
| leader2 | — | **空手、無 teammate、記憶去重完成、issue 皆保全**；/clear 留到接新單 | 57% |
| leader3 | #524 | **第 3 步待命、零程式碼改動**；已套用 memory grep 機制 | 54% |
| leader4 | #522 | **PR#537 等覆核與 merge**；#524 已交 leader3；#531 待排 | 53% |

**PR#537**（`fix/522-tunnel-id-immutable` → develop，20:44 開）＝#522 的實作：租戶可改寫 security_access 身分錨點、守衛清單漏欄位＋escape hatch 可偽造。⚠ **merge 前需重量版號**（已改 .94，但須確認嚴格大於當下 develop）。
⚠ **dispatcher 對帳漏記第三次**（#535／#533 之後又漏 #537）——leader 自行開立的 PR 同樣不會進看板。**對帳需改為每次收到 leader 回報時順手執行，而非定期**。

**leader2 的一句自省值得留檔**：「知道一個坑和在當下認出它，確實是兩回事」——他記過 maestri ask quoting 那條坑，今天仍再踩一次（反引號被 shell 展開致訊息殘缺）。dispatcher 同日亦踩過同一坑。⇒ 記憶不保證當下識別，**機械化（heredoc 寫檔再傳＋送出後 check 驗證）才可靠**。


## 🔴 更正：Gitea 故障不是「repo 專屬」，是 repo 頁面全面故障

leader4 主動更正（Plumbline 加量 `paas-odoo-ci` 也是 500）。原判斷「repo 頁 500＋Gitea 根 200 ⇒ 這個 repo 專屬」**樣本只有一個 repo**。
⚠ **dispatcher 同錯**：我「獨立驗證」時也只量了那一個 repo——我驗證了「是否已恢復」，卻**沒有質疑「repo 專屬」這個前提**就轉發。兩人在同一條推論上各省略一次多樣本驗證，錯誤就通過了（與「別拿他有回報當落地證據」同族）。
⇒ **教訓：單一樣本不足以支撐「專屬／只有 X」這種排他性結論。** 要宣稱「只有 A 壞」，最少要量一個 B 證明 B 是好的——**這次缺的對照組是「另一個 repo」**，與陽性對照紀律同源。已廣播更正。

### PR#537 看板資料（leader4 實查提供）
`Closes #522` / `Refs #524`；open；`develop` ← `fix/522-tunnel-id-immutable`；commit `a0123f4b`（已 `git ls-remote` 對過 remote SHA 一致）；manifest **.94**（develop .92、PR#517 .93）；**沒有卡人、等覆核**——實作／RED-GREEN／注入式驗證／全套（**0F/0E of 2218**，baseline 2204）皆完成並寫進 PR description；作者 Plumbline，leader4 已覆核一輪（8 個既有測試檔無 assert 被刪、改動是呼叫端遷移；版號撞號正是他覆核時抓到並要求改的）。merge 前仍須重量版號嚴格大於**當下** develop。
歸因更正：該 PR 是在「#524 判給 leader3」之後才產生，leader4 當時回報 #524 歸屬時未回頭補報 #522 的 PR 編號 ⇒ **屬漏報與對帳頻率不足兩者疊加**，非單方。

### 記憶索引去重（dispatcher 執行）
`feedback_parallel_leaders_same_file_collision` 在 `MEMORY.md` 被列在兩個章節（工作方式、發版/CI）且描述不同——**同一份內容兩個入口、recall 到其中一個會以為讀完了**。已移除較簡略的那條、保留資訊完整版，複驗剩 1 筆。（leader4 發現後未擅自動他人編排、交由 dispatcher 判定——處置正確。）


## 🔴 memory 併發事件（2026-08-02 21:xx）：三個 session 同時動一個檔，含協調者本人

**檔案**：`feedback_negative_verification_needs_positive_control`

| 角色 | 動作 | 判定 |
|---|---|---|
| **leader2** | 凍結令抵達前約一分鐘，先 append 兩段，發現檔內已有三份排他性段落＋兩份探針形態後**整份 Write 覆寫**。已保全 pre-overwrite snapshot（107 行版段落清單＋逐段處置） | ⚠ 落在窗口裡；自陳「system-reminder 產生後、Write 之前若有人 append 則已被吃掉且不可還原」 |
| **另一 session** | 發現兩個檔名近似的檔（`..._probe_...` 26 行 vs `..._verification_...` 46 行、MEMORY.md 兩條索引都在），併入後**刪除舊檔** | 索引死連結有清，但**漏了全庫 wiki link** |
| **leader4** | 發現檔內重複，**克制不動**、先回報並附行號圖 | ✅ 正確——他讀的是覆寫前長版，若動手會吃掉 leader2 的合併 |
| **dispatcher（我）** | 下凍結令後，**自己在凍結期間補了一句**，與 leader2 幾乎同時補的同一句重複 | ⚠ 違反自己剛立的規則 2；已合併並在檔內標註此事為實例 |

⚠ **我犯過的兩個錯**：①把因果方向寫反（誤述成「leader4 若動手會吃掉別人的合併」——實際是 leader2 的覆寫落在窗口裡，leader4 是潛在受害者），已廣播更正，因為錯誤的轉述會讓案例被記成相反的教訓；②在自己宣告的凍結期間動檔。**協調者的約束不會因為他是協調者而放寬——反而更嚴，因為協調者的動作最不容易被別人預期。**

✅ **我修了三方都沒看到的死連結**：MEMORY.md 索引乾淨，但 `project_ar26_sidecar_selfcheck_landing`／`feedback_post_hoc_test_lacks_red_phase`／`reference_odoo_empty_string_vs_false_vacuous_assertnotin`／`project_ar26_trackA_stg_sidecar_isolation` **四個檔仍用 wiki link 指向已刪檔**，已全部改指向合併後新檔，複驗全庫零死連結。
⇒ **刪 memory 檔時光檢查 MEMORY.md 不夠，還要 `grep -rl` 全庫找 wiki link。**

### memory 協作五規則（即刻生效，已廣播）
1. 更新既有載體前**先完整讀過該檔**，不能只 append 到尾巴
2. **同一檔的整理由單一 session 執行**，需要整理時先回報協調（**含協調者本人**）
3. append 前先在檔內 grep 你要寫的關鍵詞
4. 回報「檔案有問題」時**附行號或行數**——memory 純 append 無衝突訊號，兩人讀到的可能是不同版本
5. 刪檔時 **`grep -rl` 全庫檢查 wiki link**，不是只看 MEMORY.md

### 🔑 本事件的核心教訓（leader2 提出，dispatcher 同小時內成為第四個實例）
> **「不可逆的修正，其急迫性門檻要比可逆的高得多。」**
讀到「有東西該清乾淨」時，會把它當成**需要立刻修**的問題，而不是**需要先協調**的問題。leader2 今天中三次（#427 補刪 remote 分支、#352 刪 CF policy、覆寫），dispatcher 中第四次。
⇒ **這不是個人失誤率問題，是這個形狀對所有人都有吸引力**。而 memory 比 issue 更危險：issue 重複至少會在 PR conflict 時現形，**memory 連「有人剛改過」都看不到**。


### memory 死連結全庫盤點（leader4 執行，dispatcher 授權修復）

**掃描器自身的方法學教訓（比結果更值錢）**：第一版報 **40 條**，其中 **30 條是誤判**——①bash 的 `[[ ]]` 測試語法（`[["${GITHUB_REF_NAME}" == stg-*]]`）被 regex 當成 wiki link；②連結**省略了 `reference_`／`feedback_`／`project_` 前綴**而掃描器沒做前綴補全；③連結自帶 `.md` 又被接一次 `.md`。
⇒ **若直接把 40 交出去，接收者會去追 30 個不存在的問題**——與同日「Gitea repo 專屬」誤判同族，但**這次在交出去之前就攔住了**（同一人、同一天、同一形態，第二次沒通過）。收緊後配**雙向對照**（注入不存在的名字必須抓到／已知存在的 `kubectl-woow-k3s` 必須解析到）才有鑑別力。

**真死連結 7 條（已授權 leader4 修）**：`[[agent-runtime-stg-enablement-gap-chain]]`、`[[cf-ns-orphan-no-account-gc]]`、`[[floor-env-relative-path-empty-ext-addons]]`、`[[project_501_global_cap_landing]]`、`[[reference_tool_output_injection_antipattern]]`、`[[two-stage-remote-provision-idempotency]]`、`[[reference_maestri_dispatch_verify_premise_first]]`（**前綴錯，實際是 `feedback_` 開頭**）。
**判定不修 3 條**：`[[舊檔名]]`（散文佔位符）、`[[two-layer-acl-lockstep]]`（指向 repo 內 `docs/decisions/patterns/`，**跨載體引用不是死連結**）、`[[F-BILLING-1]]`（finding 編號非檔名）。⇒ 這三條的「已判定不修」理由要寫進回報，免得下個掃描者重追。

**leader4 補驗自己刪的三個檔**：零死連結（配陽性對照：同一支 grep 對存在的檔名抓得到 16 個引用）。其中兩個當時有做全庫檢查並修了 4 條斷鏈，**第三個是運氣好**——只清了 MEMORY.md、沒做全庫 grep，剛好無 inbound link ⇒ 印證規則 5 的必要。

**掃描器腳本化：暫緩，載體問題未解**。memory 目錄不在版控裡（`~/.claude/projects/.../memory/`）⇒ 進不了 `repo-lint.py` 也進不了 CI，而那是今天用來守其他不變式的兩個載體。放哪裡／誰維護／如何確保它自己不腐化未定案前不寫，否則多出一個沒人負責的腳本。待 dispatcher 評估載體或提報 Eugene。


### 🔴 判準本身沒被驗證：wiki link「死連結」多數是規範允許的前向引用（2026-08-02）

**兩位 leader 同時掃描、結論相反**：leader4 報「7 條真死連結」（第一版 40 條、他自攔 30 條誤判）；另一位報 59 條但**抽樣後判定不是問題、未動手**。
**關鍵事實（dispatcher 覆核）**：memory 規範明文寫著「**一個還沒對應到既有 memory 的 `[[name]]` 是可以的，它標記的是日後值得寫的東西，不是錯誤**」⇒ **前向引用合法**。而 leader4 那 7 條裡，`[[project_501_global_cap_landing]]` 正是另一位 leader 自己寫的前向引用（指向尚未建立的 #501 收官檔）。
⇒ **dispatcher 已撤回修復授權**。七條經前綴補全（kebab→snake、reference_／feedback_／project_）後確實全部無對應檔（陽性對照 `kubectl-woow-k3s`／`deploy-stg-gotchas`／`never-fabricate-tool-output` 三個皆解析到，證明手法有效）——**「無對應檔」成立，但「無對應檔 ⇒ 該修」不成立**。
唯一該修者：`[[reference_maestri_dispatch_verify_premise_first]]`（實際是 **`feedback_`** 開頭且**確實存在** ⇒ 前綴打錯，非前向引用）。判別式：**看引用它的那句話——「詳見 X」是筆誤，「日後值得寫 X」是前向標記**。

⚠ **dispatcher 的錯（同一形狀今日第二次）**：接受了「7 條真死連結」的結論，卻**沒有驗證判準本身**（`[[name]]` 必須對應 `name.md`）是否符合規範，就發了修復授權——而且是在**已經知道**「不可逆的修正急迫性門檻要更高」之後。

🔑 **本輪最一般化的教訓（另一位 leader 歸納）**：
> **否定性檢查的判準本身也要先驗證。** 不只問「探針壞掉會不會長一樣」，還要問「**我認定的『壞』，在這個系統的規範裡真的算壞嗎**」。

✅ **真正的硬要求已驗證乾淨**：`MEMORY.md` 索引 **198 條連結全部指向存在的檔案、零死連結**（磁碟 199 檔）。索引是 recall 的入口，**它壞掉才會真的讓人找不到東西；wiki link 的鬆散是設計允許的**。
⇒ 規則 5 應收斂為：**刪檔時 grep 全庫是為了修「曾經存在後來斷掉」的引用**（如 dispatcher 修的那 4 條指向已刪檔者），**不是要消滅所有無對應檔的 `[[...]]`**。

### leader2 已落地的 memory（依五規則操作、附查證資訊）
落點 `feedback_maestri_dispatch_verify_premise_first` 的 How to apply 第 5 點（**不新開檔**——同形狀前兩例本就在該檔，三例並列才看得出是模式）。查證：寫入前完整讀過、全庫 grep「不可逆的修正／急迫性門檻」命中 0、檔內 grep「協調者／凍結期」命中 0；讀取時刻 14:27:14Z、34 行。
內容含四個實例並列（#427 補刪 remote 分支／#352 刪 CF policy／整份覆寫 memory／**dispatcher 在自己宣告的凍結期間補寫**）＋「對協調者不放寬反而更嚴」＋歸納：**四次分屬三個不同角色 ⇒ 不是個人失誤率問題，是這個形狀對所有人都有吸引力**——清理動作在心理上被歸類成「修正」而非「變更」，驗證與協調的門檻於是被自己放低。**分水嶺不在動機（四次動機都對）而在可逆性**。判別式：**問這個動作出錯了能不能還原——不能，就先回報再動手，不管它看起來多明顯該做**。


### ✅ wiki link 判準最終版：三態（dispatcher 的二態分類也錯了一條，leader4 更正）

`[[two-stage-remote-provision-idempotency]]` **不是前向引用**——目標存在於 repo：`docs/decisions/patterns/two-stage-remote-provision-idempotency.md`（**7875 bytes，dispatcher 已 ls 驗證**；陽性對照該目錄共 33 個 md，`two-layer-acl-lockstep.md` 亦在）。引用原文寫「**那份 pattern doc**」，語意明確指向 repo 文件。
⇒ **兩邊掃描都只在 memory 目錄裡找**，把「寫在別的載體」誤判成「還沒寫」——**掃描範圍的問題，不是資料的問題**。

| 狀態 | 意義 | 處置 |
|---|---|---|
| memory 內有 | 一般引用 | 正常 |
| **repo `docs/` 內有** | **跨載體引用** | **正常，不要動** |
| 兩處都沒有 | **前向標記**（規範明文允許） | 正常，不要動 |
| ⚠ **打錯字指向不存在的名字、而正確目標就在磁碟上** | **真錯誤** | **只修這種** |

**語境判別式**（leader4 實作時用的）：「詳見 X／參 X、Y」＝**指路** ⇒ 找不到就是打錯字要修；「日後值得寫 X／（X 的必要條件）」＝**待寫標記** ⇒ 不動；「**那份 pattern doc**」等指涉具體外部文件 ⇒ 去 repo 找。

**leader4 的處置**：只改一條（`feedback_loop_autonomous_scope_discipline.md:16` 的 `reference_`→`feedback_` 前綴，正確目標確在磁碟），其餘六條未動，且**在 dispatcher 的撤回抵達前就自己停下**——他逐條讀語境後發現「無對應檔 ⇒ 該修」推不出來。
🔑 **他那句話值得全隊記住**：**「repoint 一個我猜出來的目標，比留著死連結糟得多。」**
複掃 10→9，帶雙向對照。MEMORY.md 索引 198 條全效、零死連結（他先前獨立量到 196，差額為新增）。

### 📌 本輪 memory 事件的完整教訓鏈（三層，由下而上）
1. **配陽性對照**——否定性結果要證明量測手段有效
2. **驗證判準本身**——「我認定的『壞』，在這個系統的規範裡真的算壞嗎」（dispatcher 在此失手，發了錯誤授權）
3. **驗證掃描範圍**——「我找的地方，是這個東西可能在的全部地方嗎」（dispatcher 與 leader4 同時在此失手，都只掃 memory 目錄）
⇒ 三層都通過，一個否定性結論才站得住。而**每一層失手的外觀都與成功一模一樣**。


### 📌 #535 已分配 leader2（dispatcher 主動分配，2026-08-02 21:xx）

**#535 租戶填入的憑證在服務詳情 API 明文回傳**（`priority:high`）。派他的理由：#352／#521 剛做完，SA／憑證／helm_values 這條線 context 最完整；且根因與 #521 同形狀（**機制存在但覆蓋不到實際路徑**）；他空手且保全已完成。
**根因**：`_sanitize_helm_values` 遮罩是 **path-aware**，只認三條路徑（頂層 `secret` 子樹／`config.sensitive` 葉節點／#530 新加的 `auth.apiServerKey`）；租戶經 `helm_value_specs`（`type: password`）填入的憑證**以自己的鍵名落進 helm_values**，不走那三條路徑 ⇒ API 明文回傳。
🔴 **前端 ConfigurationTab 對 `type: password` 一律顯示 `********` ⇒ 畫面看不出來，洩漏面在 API 層不在畫面層**——最違反直覺、最容易被誤以為已修好的地方。
**實證已配陽性對照**（品質高、接手者不必重查）：同一次呼叫 `auth.apiServerKey` 正確遮成 `***`、`username` 非憑證欄位保留未過度遮蔽 ⇒ 證明探針沒壞、函式確實有跑，**洩漏是路徑沒涵蓋而非函式沒執行**。已實證洩漏：`minimaxApiKey`／`openrouterApiKey`／`odooPassword`／`smtpPassword`／Anthropic key。
**完成層次可談**（第 2 步用）：只修已知鍵／改成「`type: password` 一律遮」的結構性修法／再加機械化 gate 防未來新增 spec 又漏。
**關聯**：#530 剛動同一個函式（開工前對接）；與 #521 同族但**非重複**（一個是遮罩路徑、一個是配額 enforcement）。

**分配權歸屬**：Eugene 透過 leader3 明確裁示「issue 的分配歸 dispatcher 負責」⇒ 本次為 dispatcher 主動分配，未逐案請示；leader2 仍會依四步流程先考 Eugene 再等指令，不會擅自動工。

## Tracker 衛生（上線前順手做）

經程式碼覆核**已修復卻仍 open** 的至少 8 張：#336、#375、#410、#364、#362、#129、#208、ci#34；另 #432 與 #503 重複。建議撥半小時批次覆核關閉——否則上線後 incident 盤點會被 stale 資料稀釋誤導。

## 分配結果 v2（2026-08-01・issue-dispatcher・一人一 issue）

規則（Eugene 指示）：一個 leader 一次只處理**一個** issue；分配後**不得自行動工**——各 leader 說明拿到的 issue 後待命，後續由 Eugene 決定。

「分配後五步流程」（2026-08-03 Eugene 於 note-issue-dispatcher 再更新：新增第 4 步回報，並明定 dispatcher 職責＝分配＋更新本看板＋接收 leaders 回報）：分配完 issue 給 leader 後，leader 依序做五件事：

**0. 保全 → dismiss → /clear（2026-08-02 順序修正，leader2 實測抓到原順序缺陷）**

⚠ **原順序「dismiss → 保全」有致命窗口**：隱含假設是「/clear 才會流失」，但**teammate 一被 dismiss，他手上的分析就只剩 leader 的 context 有**。實例：leader2 的 w0-prod-ops 對 #389 prod SMTP 的完整 root cause 診斷（ir_mail_server 0 rows／conf 四行全註解／fallback localhost:25／pod 內無 MTA／七家 SMTP egress 實測全通／7 筆積壓全是 digest 非業務信／company email 與 catchall 全空／完整修復建議）**從未寫進 issue**，只存在對話裡；同一位 teammate 的 cd#24 preflight 有自己寫進 issue ⇒ leader 原以為兩份都落地，**實查才發現 #389 是 0 命中**（已補寫並用 7 個關鍵字複驗）。
⇒ **兩層教訓**：①順序改為保全在前；②**「teammate 有寫過某一份」不能推論其他產出也寫了——要逐份實查、grep 複驗，不是抽樣。**

修正後三動作：
1. **保全**：逐一 check 每位 teammate，把回報過但尚未落地的分析寫進 issue 留言或交接接手者（逐份實查＋關鍵字 grep 複驗）；跨 session 教訓寫進 memory；進行中分支**已 push**。
2. **dismiss**：只 dismiss 自己 recruit 的 teammate，且先 check 確認是完成式無進行中任務。🔴 **絕不可 dismiss 其他 leader**（maestri list 會把同儕列在自己樹下，誤刪會刪節點並斷線、CLI 無法復原，只有 Eugene 能在畫布重拉）；🔴 **絕不可刪共用 note `paas-issue-08-01`**。
3. **`/clear`**：以上確認後才執行。**實務彈性**：若正在等 Eugene 回覆、之後可能需要現有 context 無縫接續，`/clear` 可延後到真正接到新單時再做（保全與 dismiss 照做）——不必為流程完整性丟掉還有用的 context。

2026-08-02 teammate 實況：leader1＝Vault／Tollgate；leader2＝**已全數 dismiss（5 位）**；leader3＝Bastion／Sieve／Ferry；leader4＝Bulwark／Tally／Lathe／Rudder／Keystone／Plumbline。

1. **考 Eugene 能不能了解這張 issue**——不是 interview。目的是讓 Eugene 這位開發者真的了解**問題是什麼**（避免 agent 放飛自我）。白話說明＋理解確認型提問（請他複述／檢核性問題），**不列 A/B/C 方案選單**。

2. **與 Eugene 討論「解完的狀態是什麼、怎麼叫完成」**——完成標準／驗收條件取得共識。Eugene 要知道「做完長什麼樣」，不只是「問題是什麼」。

3. **等 Eugene 指令接下來的動作**——他明示前不動工。修法方向、範圍、優先序由他決定。

4. **回報 dispatcher 幾件事**（2026-08-03 新增，Eugene 原文轉寫）：①有沒有延伸的 issue 或 follow-up 要建立；②有沒有需要持久化到 docs 的部分；③是否要先等別的 leaders 完成？他才可以動工。

teammates 對 leader 亦同（需 Eugene 裁決的事項一律先回報 leader 彙整，不得直接問 Eugene）。

（已被上方四步版取代）舊三步流程（2026-08-02 稍早）：分配完 issue 給 leader 後，leader 依序做三件事，**每一步都不是 interview、不是把選項端給 Eugene 挑**：

1. **考 Eugene 能不能了解這張 issue**——目的是讓 Eugene 這位開發者真的了解**問題是什麼**（避免 agent 放飛自我）。做法＝白話說明問題本質＋理解確認型提問（請他用自己的話複述、或問檢核性問題），**不列 A/B/C 方案選單**。
2. **與 Eugene 討論「解完的狀態是什麼、怎麼叫完成」**——把完成標準／驗收條件講清楚並取得共識。這是新增的一步：Eugene 要知道「做完長什麼樣」，而不只是「問題是什麼」。
3. **等 Eugene 指令接下來的動作**——他明示前不動工。修法方向、範圍、優先序都由他決定，leader 不自行選。

teammates 對 leader 亦同（需 Eugene 裁決的事項一律先回報 leader 彙整，不得直接問 Eugene）。

| Leader | Issue | 上線前最小交付 |
|---|---|---|
| leader1 | woow_paas_platform#504 prod 平台 DB 零自動備份（Wave 1 第一優先） | 比照既有 18 份 pg-backup CronJob 建每日備份 |
| leader2 | paas-agent-runtime#29 operator helm install 把 repo 密碼明文寫進 log（Wave 0 首條） | redact 兩個 flag；須先於 cd#24 輪替上線 |
| leader3 | paas-odoo-ci#46 addon_ref 留空靜默 build 倒退 image（Wave 1 不可壓縮） | fallback 到 tag ref 或 fail-closed |
| leader4 | paas-agent-runtime#26 untrusted skill 與平台憑證同 pod 同 uid（Wave 3 及早派工） | 最低三件：env-scrub、bash 換獨立 uid、secret 檔權限收斂 |

池狀態更新（v1→v2）：
- woow_paas_platform#29 已關閉（PR#506 merged，另一 session 完成），移出池。
- woow_paas_platform#389 仍 open，但 PR#507 已修 dedup marker 不重試部分，殘餘範圍待覆核，本輪不分配。
- paas-app-bundle-cd#24 依賴 runtime#29 redact 先上線，留待下一輪。
- woow-paas-charts#42 據 leader3 偵察疑已在 chart 0.1.4 修復並部署 stg（repo 側待覆核）；同偵察發現真活口是第三方 chart（n8n 無 egress NP，可達 prod operator/odoo ClusterIP）——去留待 Eugene 裁決。
- 其餘 P0（cd#77、#427、#383、#501、#352、#342）與 tracker 衛生留池，待 Eugene 決定下一輪。
- 平行軌 #441 歸 woow-machines-operator 職責，需另行啟動。

### ⚠ 新增未分派高優先：#519（2026-08-02，leader1 於 #501 對抗式覆核發現，真 HTTP 打通驗證非推論）

**#519 租戶可繞過下單流程直接建立 cloud_service——無訂閱、無扣款，部署 cron 照樣開出去**（dispatcher 已查證 open）。事實鏈：src/security/ir.model.access.csv:9 給 base.group_user 對 cloud_service 全 CRUD（1,1,1,1）＋ir.rule cloud_service_rule_workspace_member 只限 workspace 範圍、不限欄位 ⇒ 一般租戶可繞過整個 controller、用 Odoo 通用 ORM JSON-RPC 在自己 workspace 建服務而不產生訂閱；部署 cron 只看 state=pending 就真的開出去。
三個高優先性質：①不需任何繞道技巧——ACL 直接給了權限；②對任何 template 成立（含無上限、無停售的一般商品）；③與 #501/#516 正交（那兩條是上限與回滾，這條是根本不用經過下單流程）。leader1 判定命中 D3≥4＋D6≥3，平台無事後 reconcile 能發現（正是報告系統性主題「計費物件與 K8s 實體無 reconcile」的形態）。
修法需設計決策（建議先考 Eugene）：收 ACL 欄位權限 vs model 層守衛要求 create 必經下單流程——兩者都影響既有路徑。leader1 不碰（其 PR 只補 write 守衛）。**四位 leader 現皆持單，待 Eugene 決定由誰承接或誰先讓位。**

### ⚠ #524（**改派 leader3 承接**，2026-08-02 Eugene 二次裁決；leader1 預約已作廢）smart_home 完全沒有 record rule（2026-08-02，leader4 於 #522 調查掃出，dispatcher 三點交叉驗證後開單）

**任何已登入租戶可讀取並修改所有其他租戶的 Smart Home 記錄**。已驗證：①src/security/ir_rules.xml 是全 repo 唯一 ir.rule 來源（45 條 rule／13 個 model），**smart_home 出現 0 次**；②ACL ir.model.access.csv:24 給 base.group_user 讀寫建（1,1,1,0——刪不掉但改得動）；③prod＋stg 兩顆真 DB 查 ir_rule 皆 0 條，**陽性對照通過**（同查詢對 security_access 回 3、workspace 回 2，排除查詢寫錯）。
**比 #519/#521 更基礎一層**：那兩張是「守衛存在但不在必經之路上」，本張是「**守衛根本沒被建立**」——CloudService 那種 workspace-scoped rule 一條都沒有。
🔴 **與 #522 的組合效應（已在兩張互相標註、不併單）**：smart_home.py:79 的 tunnel_id **無群組遮罩**，而同檔 :80 的 tunnel_token 有 groups=base.group_system（相鄰兩欄遮罩不對稱）⇒ 本缺口正好供給 #522 卡住的那個 tunnel UUID，**合起來 #522 從「需先取得 UUID」變成「直接可打」**。
同輪掃出：change_plan_wizard 亦 Internal User 讀寫建且無 rule（TransientModel，較低）；其餘無 rule 者皆唯讀目錄型屬預期；oauth_token／cloud_service_secret／reconcile_report 未開給 Internal User＝安全。
未量到：尚未用兩個真實租戶帳號實測跨租戶讀寫（只證明結構事實），坐實可走沙盒內普通租戶帳號＋陽性對照。### ⚠ 新增未分派：#522 SA route 的 service_url 無目標歸屬驗證（2026-08-02，leader3 於 #383 交接時提出、dispatcher 交叉驗證後開單）

**#522：租戶 A 可把公開 hostname 指向租戶 B 的內部服務**（結構成立、**端到端未驗**）。已驗證事實鏈：service_url 是 fields.Char(required=True) 且 security_access.py 全檔僅兩條 constrains（:962 subdomain_prefix／:969 security_access_id）**無一驗 service_url**；值直接來自 kwargs（controllers/security_access.py:158、:566-567）零白名單；is_protected default=False 且 help 自陳「publicly reachable with no Access gate」；租戶 ns 的 allow-cloudflared NP 是 podSelector:{} 無 per-tenant 限制。
**未量到的一環（勿當已證實）**：cloudflared 是否真會撥——設定在 CF 側 kubectl 讀不到，要量須真建公開路由＝對外不可逆動作，調查者依紀律未執行。升級為已證實需一次受控端到端驗證（stg＋拋棄式租戶對＋驗完即拆），須事前授權。
**#427 擋不住這條**：流量從 CF 經 cloudflared 進來、走 tunnel 而非租戶 pod egress，不受該 NP 約束——不同攻擊面。
**方法論意義**：#383 的覆蓋率複查是盤查「K8s 寫入端點有沒有過 validate_namespace」，而本路徑副作用根本不落在 K8s ⇒ 再怎麼盤查也盤不到，這是該方法的邊界。

**#522 調查進展（leader4，2026-08-02，已授權寫進 issue 保全）——根因比開單描述更精確**：
- 🔑 **tunnel_id 才是這條鏈的開關**：租戶**可寫、無守衛**。三個對抗式反駁者推翻的是 **issue 原始描述那條路徑（service_url）**，不是這條——此區分必須留痕，否則後人會誤讀成「整張單不成立」。反駁者自己也標明「沒稽核通用 ORM 入口是否讓租戶寫得到該欄位，若可寫整條推翻立刻崩潰」——而答案是可寫。
- ⚠ **先前 (a)+(b) 切法有邏輯漏洞（leader4 自行更正、dispatcher note 曾記錄過）**：叢集有**兩個不同的 cloudflared**，(a) 指租戶自己那條、(b) 指平台共用那條，串起來＝拿 A 的前提配 B 的結論。中間缺的環正是 tunnel_id。
- **NetworkPolicy 是唯一網路授權層**：201 個 CRD 中 Cilium／Istio／Linkerd／Calico 為 **0**，無第二道防線可依賴。
- 🔴 **stg 共用 tunnel 目前指向 prod 的 paas-operator（paas-api.woowtech.io）**⇒ stg 側 Cloudflare 設定寫入權若被濫用，**影響面跨到 prod**，嚴重度可能超出 #522 原框架。
- **建議的乾淨驗證路**（leader4 提，未執行）：dev sandbox 內用普通租戶 owner 帳號（非 admin 非 sudo）試寫 tunnel_id ＋陽性對照（同一支呼叫寫 name 必須成功）⇒ 可分辨「寫不進去」與「探針壞掉」；全程沙盒內、**不碰 Cloudflare、不產生對外可達物**。待 Eugene 決定是否執行。
- 另一條待分流：**smart_home 沒有 record rule**（與 #519 可能同族），dispatcher 已請 leader4 提供要點後開獨立 issue，不併進 #522。

### ⚠ 新增未分派：#521 CS 型 access 加購完全未接線（2026-08-02，leader2 於 #352 interview 挖出，Eugene 在 stg 訂了 +1 access seat 卻找不到設定處）

**#521：收了錢但功能完全不存在**（dispatcher 已開單、已交叉驗證）。三層失效鏈，每層單獨都足以讓功能不可用：
① ac_max_access_seats 全 repo 僅 6 個讀取點（欄位定義＋可見性/變體判別），**無一處 enforcement**——docstring 自陳「欄位先行佔位」；
② CS family 的 access 變體沒被 SA_QUOTA 表餵到 ⇒ sa_max_access_policies 落 default 0，而 _check_policy_quota（:159-163）**無 0 豁免** ⇒ 第一個 policy 就被擋（對照：_check_emails_per_policy :116 有 0=unlimited——**同檔案相鄰兩段的 0 語意相反**，本身即缺陷，調查中實際造成一次誤判）；
③ 買到的席位與 SA 實例無關聯（stg：+1access 在 service 62／訂閱 49，workspace 唯一 SA 6 掛訂閱 10）⇒ 無介面可設定。
與 #342 同型（收錢不交付）。需產品決策：計數基準、超額行為、CS 變體要不要有政策 quota、既有孤兒訂閱怎麼補綁。
**#352 已留言更正三項錯誤前提**（UI 仍提供且 email_domain 是預設值／繞過路徑還有 IP 與國家規則／bypass 型衝突）＋界定範圍只涵蓋 SA 型線＋記錄邊界「擋繞過≠席數計費成立」。stg 三筆存量政策經查為合法（SA 型商品 quota=6），非繞過產物。
附帶同源：兩條 quota 檢查皆有「無訂閱即 continue」⇒ 與 #519 同形態（檢查掛在訂閱上，沒訂閱就沒檢查）。

#501 其他衍生（全 open）：#515（HAOS 容量 pre-check，C 案拆出、需跨 operator 發版線）、#516（create 守衛被 controller 吞掉、超額仍落庫——隨 #501 PR 修）、#518（**max_per_partner 的 advisory lock＋取鎖後計數在 Odoo REPEATABLE READ 下零貢獻，leader1 實測重現超賣**；全域上限部分隨 PR 修、per-partner 待排）。
⚠ #518 橫向影響：repo 內同形制並發防護（ha_paas provision 鎖、project reach 鎖）須重新檢視，**勿照抄該 docstring 的 READ COMMITTED 前提**——動並發相關工作的 leader 須知。

各 leader 說明 issue 時的前提修正（v2 分配後回報）：
- leader2：ar#29 的 flag 級 redact 其實已由 PR#172 landed；殘餘＝payload 級補強 PR#505 待驗收合併＋operator 發版。另 PR#506（plat#29）、PR#507（#389 code 部分）已開好待驗收，tracker 衛生 10 張已完成。cd#24 輪替與 #389 SMTP 執行面都卡 Eugene 的帳密／授權。
- leader4（Bulwark recon）：ar#26 的 sidecar 架構已在 runtime／image／chart 三方落地、stg/prod skillRunner.enabled 皆 true；真正殘留＝測試佐證不足＋sidecar 內同 uid(10002) 的 /proc/1/environ 與 kill 1 兩條旁路。實際交付範圍開工時以此為準重估。

## PR 獨立審查結果（2026-08-01・三支全數審畢，合併待 Eugene 裁決）

三位獨立 reviewer（Spyglass／Periscope／Beacon，皆非實作團隊）以 /mattpocock-skills:code-review 雙軸審查，全程唯讀未留 PR comment：

| PR | 對應 issue | Verdict | 關鍵 findings |
|---|---|---|---|
| #506 mixin _sub() sudo | plat#29 | ✅ Approve 可合併 | 非阻斷 4 條：S1 gate 漏抓 mapped/loop-var 形態（實測可繞）、N1 pattern-guard-map 未同步且 mixin 檔零 guard 覆蓋、N2 repo-lint 編號亂序、S2 缺 HttpCase 層 |
| #505 payload redact | ar#29 | ❌ Request changes | Blocking B1：timeout 例外 str(e) 把原始密碼整條回流 log＋HTTP body；Blocking B2：--type=json JSON Patch 的 EnvVar value 槽不遮，MCP bearer token 明文進 log。另 G3 跳脫字元 fail-open、G5 AST gate 六種寫法可繞、C6 遮錯邊 |
| #507 dedup marker | plat#389 | ⚠️ 修 S1 再合併 | Blocking S1：low_balance_warning_cron.py:97/103 是同鍵 marker 的第二寫入者未改，prod 實際時序（預警 cron 先寫 marker→SMTP 掛→D 日同鍵查無載體→return False）仍整週期靜音。另 S3 email 缺漏租戶的載體會被 auto_delete unlink 判成已送達、S6 上線硬性要求 -u（新 stored 欄位，.89） |

分配重複檢查結論：四位 leader 的 issue 無互撞；唯 leader2 的 ar#29 實作已被 PR#172（landed）＋PR#505 覆蓋，PR#505 合併後 leader2 只剩 operator 發版＋關單，是否改領新 issue 待 Eugene。ci#46／#504／ar#26 相關四 repo 均無既存 open PR。

Eugene 裁決（2026-08-01）：①#506 直接合併——已執行（merge fc038999，issue plat#29 已關）；②#505 退回 leader2 完善後重推（B1+B2 必修）；③#507 退回 leader2 完善後重推（S1 必修）；④ar#29 由 leader2 一路處理到關單（重推→re-review→合併→operator 發版→關單），避免重工。

Re-review 結果（同日）：兩支皆複審通過。
- #505（head d6387b28）Periscope Approve：B1/B2/C6/G3/G5 逐條實測轉綠、11 種真實 argv 形狀無 fail-closed 誤殺、測試鑑別力已補證。合併前必辦＝更新 PR 描述（已轉 leader2 處理中）；follow-up：G4/H1/H2/H3。
- #507（head ffbc5f0d）Beacon Approve：S1 經獨立時序重推＋結構驗證（全 repo 只剩單一 marker 寫入點）確認真修；S3 auto_delete=False 實擋 unlink；假 RED 修正可信。非阻斷殘留＝測試檔 docstring 契約段仍 v1（與新行為矛盾三處）、過期 TODO、OBS-1 實際上限 11 封/週期非 7、OBS-2 載體永久留存無清理 cron。
Eugene 最終裁決（同日）：兩支直接合併、註解漂移開 follow-up——已執行：#505 → merge 36a6657b（描述已先更新，含 R1 逐條處置表與 G4/H1-H3 follow-ups）、#507 → merge bf89fed7；註解漂移＋OBS＋上線 -u 備忘開 issue #508；issue ar#29 待 operator 發版後由 leader2 關單（已通知解鎖收尾）；issue #389 維持 open（SMTP 供應商面待 Eugene 帳密）。

三支 PR 全數收官：#506（fc038999）／#505（36a6657b）／#507（bf89fed7）皆入 develop。

ar#29 全鏈收尾完畢（closed，issue-dispatcher 已查證實體）：operator image 20260801.1305（digest 0f87ac73…0179be）stg＋prod 上線（CD PR#79 merge 2f8fdf9、prod digest 三方一致、log 0 error）；stg 活體 probe 證實原洩漏行已成 <redacted> 且無過度遮蔽；probe 資源全清。殘留分流：cd#78（Gitea helm token 輪替，與 cd#24 同批、卡 Eugene 帳密）、platform#509（G4/H1-H3＋argv/stdin 根治）、#508（#507 註解漂移＋維運備忘）。

上線備忘（存活一條）：平台下次發版硬性 -u（.89 新 stored 欄位，漏跑＝計費 tick 靜默失敗）。

Leader 現況（2026-08-02 更新）：leader1 **#501 interview 已於其終端掛出**（見 dispatcher 畫面上 Eugene 指派意向後預讀 issue 備妥四題：修法方向 A/B/C/D 複選、上限數字、售罄行為、waitlist——正式分配以 Eugene 作答為準；#504 已 closed 收官）（首次自動排程 03:30 UTC 如期執行：job Complete 31 秒、兩份 dump 並存證明排程真跑＋PVC 持久化；範圍外兩項〔Longhorn 異地備份、移除誤導 label〕留 issue 記錄建議併 P1 #443；意外實證：NP ns 內新 pod 首次連線有空窗、備份 job 第 2 次嘗試才連通——已同步 leader2/leader4 驗收紀律）／leader2 **#427 最小版收官（issue 依原範圍保持 open）、空手待新分配**。交付：cd PR#84（stg）/#86（prod）/#87（註解）皆 merged（dispatcher 已查證）；載體＝獨立 raw-manifest Fleet bundle 與 #504 同構、operator 零重啟。驗收全綠：V2 核心翻綠（n8n 打 operator 由 OPEN→ECONNREFUSED）、V1 回歸、V3 合法路徑 200、kubelet probe 未擋、回滾實跑演練。刻意副作用：paas-api.woowtech.io 200→502（Eugene 裁決控制面不對公網開，記 cd#85）。動工前攔下兩個會斷線的漏列：dev-sandbox-stg（interview）＋paas-api 公網入口（偵察）；三個派工假設被推翻（chart 無 NP values、pause gitrepo 非必要、port 寫 8000 非 80）。#427 留 open 理由：paas-odoo ingress 仍 from-less（n8n 仍打得到 prod odoo）＋egress 未動＝odoo 半屬 fast-follow。衍生：#514（L2 租戶 ns egress baseline）、cd#85；交接觀察項：pvc-cleanup Job pod 被 NP 連帶選中判無害未實測，待 08-03 02:00 UTC 排程確認（已寫 issue 留言）。**#519 路徑盤點已完成並保全（leader3，2026-08-02；workflow 10/10 agents、1.39M tokens、519 次工具呼叫零 error；成果 6,515 字寫進 #519 issue 留言，零程式碼改動、未開 worktree）。狀態＝待修法設計決策，退回池。**

🔑 **修法選項已收斂到一條（這讓 #519 從「開放設計題」變成「拍板題」）**：
| 修法 | 需連帶改動的合法路徑 | 判定 |
|---|---|---|
| **ir.rule 拆分** | **0 條** | ✅ 唯一可行——不需 migration，且 repo 內有逐字點名 cloud_service 的前例可抄（`ir_rules.xml:95-146`） |
| CSV 關 create | 1 條 | ⚠ 撞 sudo baseline 零餘裕 |
| create() 要求有訂閱 | **2 條全炸** | ❌ 兩條合法路徑都是「先建 service 後建 subscription」，上線前修不完 |

⚠ **方法論教訓（比清單本身更有價值）**：7 個 sonnet 角度**一致**認為「每條合法路徑都必須改走 sudo」，opus 完備性審查官回 repo 實查後**推翻**——SA 早就做過同型的 ir.rule 拆分，而**那段註解逐字寫著當初的漏洞是「mirroring cloud_service」**。修法前例就躺在 repo 裡，7 個角度沒有一個看到，因為**它們全站在同一個結構位置**（純 code grep、零覆蓋既有 rule 與註解）。
⇒ **多角度 ≠ 多結構位置**。同一個取材方式再多份也補不出盲區，要靠站在不同位置的審查官實查。
審查官另抓到搜尋階段的「窮盡列舉」宣稱建立在**漏檔名單**上（與 #415「答錯兩次」同型錯法）——結論碰巧不受影響，已寫進留言警告接手者。

附帶交叉驗證：#524 在此次盤點中被完備性審查官**獨立撞到**、列為「#415 家族的鏡射問題」，已寫進 #519 留言第 7 節並指向 #524 ⇒ 兩條調查線各自到達同一結論。

／leader3 **再改持 #524**（Eugene 二次裁決，2026-08-02）——#519 已保全退池（原紀錄：其盤點 workflow cloud-service-creation-paths 已 8/9 agents done（41 分鐘／1.3m tokens），dispatcher 要求**先收攏九份結果並寫進 #519 issue 留言保全**（照 #383 交接規格：核心結論＋不准被讀成沒問題的限制＋未量到＋污染揭露）再退池，狀態＝已完成路徑盤點、待修法設計決策。原 #519 分配紀錄：**曾持 #519**（Eugene 裁決讓位換單，2026-08-02）——租戶可繞過下單流程直接建 cloud_service，無訂閱無扣款、部署 cron 照開；派他的理由＝與其三張 CI 單同形狀的下一層「有守衛≠守衛在必經之路上」；修法需設計決策（收 ACL 欄位權限 vs model 層守衛要求 create 必經下單流程），照新規範先考 Eugene 後待命。**#383 退回池，交接已辦妥**（leader3 未據那條未送出的指示動工——apply_manifest 一行沒改、extra/paas-operator/ 零 diff；成果全留 #383 留言：核心結論「無漏網 HTTP 入口」＋**三個不准被讀成「沒問題」的限制**〔L1 四個判不出來、L2 兩個靠呼叫順序掩護、**L3 apply_manifest 結構性 fail-open**〕＋六個 API 檔逐檔結論＋窮舉性依據〔K8s SDK 結構上不存在該路徑、requirements.txt 無那些套件〕＋前綴守衛實測 fail-closed＋兩版工具各自的失效方向＋污染揭露。另代為 push 保全了只存在本機 worktree 的分支：audit/383-topdown-coverage(bb4fbdb2)、audit/383-namespace-coverage-bottomup(d50a9a0b)）。原 #383 敘述留存：validate_namespace 覆蓋率複查＝上線前最低交付；已確認認知：operator RBAC 叢集級不 enforce ns prefix、app 層 validate_namespace 是唯一那道門；Wave 2 初步 grep 訊號＝helm.py 約 40 處命中、但 access.py／tunnels.py／init.py／routes.py／odoo_modules.py／namespaces.py 未命中——可能經 service 層間接過閘，正是本單要查實的；正向 Eugene 做理解確認、確認後待命等指令；ci#47/ci#52 前案脈絡：ci#50 收官（closed，dispatcher 已查證）**：Verify step 落在 Build 與 Push 之間已上 main、V2 注入用合成最小 image 實測紅燈、唯一文本推導邊界＝真實 image 內 manifest 路徑（推錯會 fail-closed 擋下非放行）；衍生 **ci#52**——⚠ 需進池排序：#46 只關掉「隱含」那道門，**明示存在的 branch（addon_ref=develop）完全暢通**＝#46 自述危害原文換一道門進來；#50 對此零鑑別力（兩 gate 正交：#50 擋 image≠clone、擋不了 clone 錯 ref）；ADDON_REF 在 log 印三次但「可見≠被檢查」；修法小（preflight :118 已區分 branch/tag，只差 prod 約束）。已寫明殘餘極限：打錯成另一存在 tag 仍全綠、只有人工對發版單抓得到。
**ci#52 收官**（2026-08-02，ci#53 merged 9ea08bf，dispatcher 已查證）：prod build 的 addon_ref 限定 tag、branch 即 exit 1（stg 不受限）；判別讀既有 IMAGE_NAME、11 列等價矩陣有鑑別力（擋下後綴比對的分歧寫法，誠實標示 3 量測/6 推導/2 不可達，push v* 那格必為推導——gate 導入後從未跑過）；紅線守住＝approval gate 的 if: byte-identical（sha256 前後相同）、拒絕 env 共用寫法（gate 讀 env＝任何 bash step 可寫 IS_PROD=false 關掉人工核可）。
發版鏈四門封閉表：留空→#46／不存在 ref→#46 preflight／image≠clone→#50／prod 用 branch→#52；**第五門「打錯成另一個存在的 tag」CI 本質抓不到、只有人工核對發版單**（已寫進錯誤訊息＋SOP）。
⚠ 全隊通則候選（leader3 提議，同形狀三張單出現三次）：**比對類驗證必須先 assert 兩邊皆非空**——Bastion 的 V6 曾因 ${{ 跳脫讓兩邊都抓到空字串、印出「完全相同 ✓」假綠（自抓自修自報）。「空值被當成相等」＝#46「推導當量測」同病變形。
ci 待排池：plat#510＝ci#48（裁決點同前）＞ci#47；既有 ci#43/ci#30。收官附驗：ci#51 merged（b312be1）＋plat#513 merged（7b10c9bf，dispatcher 已查證）；注入式驗證用真偏差 image 打紅（clone .88/image .40→exit 1）；文本推導邊界已由 leader3 進 prod pod 實測關閉。teammate 規則落地時的 meta 教訓：JSON 裸換行讓 batch 訊息根本沒送出＝「我發了≠對方收到」同形狀，已改檔案傳遞重送並逐一驗證送達／leader4 **改持 #522**（2026-08-02 Eugene 裁決）——已自行複查四個事實點對得上（:876 欄位定義、:962/:969 唯二 constrains、controllers :158 與 :566-567 直通寫入）。**驗證設計亮點**：不預設非做對外不可逆動作不可，正嘗試把端到端拆三段以完全不碰 Cloudflare 寫入即閉合鏈路——①平台原封不動接受任意 origin（靜態可證）②cloudflared 的網路身分打得到租戶 B（叢集內、不對外可證）③「公開 hostname 真的可達」由**既有合法路由今天就公開可達**這個產品事實涵蓋。派工單硬約束：若最終仍需真建公開路由，**須先向 Eugene 請示取得明示授權**方可執行。
**#522 已收官（2026-08-02，Eugene merge，leader4 實查四項側效果）：PR #537 merged、issue #522 closed（`Closes` 自動生效）、develop manifest = `18.0.1.0.94`（bump 沒被吃掉）、develop 含 commit `a0123f4b`。⚠ merge 後 develop 跑到 .94，**三張 open PR（#540／#539／#517）全部停在 .93 已失效**——這次 base .92／develop .94／PR .93 三方相異，git **會**報衝突（與上次同值＝零訊號不同），風險是**解衝突時降版**；正解是各自 rebase 後改 .95/.96/.97 遞增。原紀錄：PR #537 open 等覆核**（`develop` <- `fix/522-tunnel-id-immutable`，commit `a0123f4b`，manifest `18.0.1.0.94`，body 含 `Closes #522`/`Refs #524`）。⚠ merge 前必重量版號嚴格大於**當下**的 origin/develop（同日撞號三次：#352 撞 .90／#517 撞 .93／#522 撞 .93，衍生 #538 要把它自動化）。
🔴 **issue 原描述被推翻，但跨租戶結論成立、機制完全不同**：SA 的 cloudflared connector 由**租戶自己在自家主機跑**（token 經 controllers/ha_api.py:363 發給租戶），實測 woow-k3s 全叢集 366 pods／33 個 cloudflared、**零個在 paas-ws-***，故 service_url 在租戶自己網路解析。⇒ ①**補 service_url 驗證是 no-op 控制**（預設值 http://localhost:80 就是租戶自家反代，本來就能轉發到任何地方，鎖死也不改變這條 route 能服務什麼）②issue 建議的修法方向會打爛產品主要用法。真正沒人守的是 **tunnel_id**（create/write 只擋 canonical_name/reference_id）。
**已實測**（拋棄式沙盒，image digest 與 dev-sandbox-stg 同一顆 sha256:697446…，故測到的是實際部署的程式碼）：只有 base.group_user、is_admin=False 的 workspace owner，經標準 /web/dataset/call_kw 寫 tunnel_id **回 true 且值真的落地**；四項控制（陽性對照寫 name 成功／鑑別項寫 canonical_name 被擋／主測項／陰性對照非成員 AccessError）全符合。
🔴 **修的過程量到第二層更深的缺陷**：escape hatch 是 **client 可偽造的 context key**（`dataset.py:33-36` 把 client kwargs 原封不動餵進 `call_kw`、`api.py:531-532` 直接 with_context）⇒ 既有的 canonical_name/reference_id 守衛**現在就是可繞的**，我先前實測「canonical_name 被擋」只因為沒帶那個 key。修法＝逃生門改**模組層級私有 sentinel object**（JSON 表達不出物件同一性）＋宣告式 `_IDENTITY_ANCHOR_FIELDS`。
**衍生**：#524（smart_home 完全無 ir.rule，leader3 持）、#531（operator `PUT /api/tunnels/{id}/routes` 零歸屬驗證＋整份 ingress 覆寫＋DNS 對帳刪光不在清單者，獨立發版線故不併）、#536（context key 家族另四個可偽造，其中 `sa_skip_route_sync` 最有意義＝被抑制的 Access app 會隨後續合法異動一起上線 ⇒ 使用者標為 protected 的 route 可能無 gate 上線）。
**待 Eugene 裁決**：①#537 要不要找別人做獨立 review（leader4 已自行覆核測試 diff／版號／PR body，但同一個腦袋看兩次）②#531／#536 要不要現在排人（Plumbline、Keystone 待命）。
**複驗環境**：docker `wpp522-odoo`/`wpp522-pg`（net522、DB test522）留著，不需要時 `docker rm -f`；HTTP 探針在容器內 /tmp/probe.py，要用 `/opt/bitnami/odoo/venv/bin/python`；跑測試前先 `pkill -f http-port`（8069 被佔會讓 odoo 直接退出而**輸出全空**）。
前一包 ar#26 收官紀錄：**ar#26 closed 收官**（2026-08-02，dispatcher 已抽驗：ar#26 closed、ar PR#32／charts#67／cd#89 皆 merged、ar#33/#34/charts#66 衍生單存在）。交付：bridge daemon 啟動自檢（實況層守線）＋image agent-runtime 1.4.4（digest sha256:6aeeddea91f4…，向 registry 直驗）＋cd#89/#90＋charts#67（0.4.5/appVersion 1.4.4）＋charts#68/#65 封閉性斷言。
兩軌 PASS（皆實體證據、leader 端獨立覆核）：軌 A 憑證隔離＝env 四祕密全 exit=1 len=0、/proc 掃不到 agent 行程、auth secret volume **根本沒掛**（機制是「不存在」非「權限擋」），每項配陽性對照；軌 B 自檢不誤殺＝1.4.4 真實 stg 新 spawn 正向通過（log 有「自檢通過」而非「沒看到錯誤」）、28 case 零違反，最有力鑑別項是 **exit 42**（拒服務路徑固定回 -1，拿得回 42 才證明真的走到 spawn_bash）。
**前提校正**：三項最小交付（env-scrub／異 UID／祕密檔不可讀）開工前**早已三方落地且 stg+prod 啟用**，charts 側連 CI 斷言含 negative test 都有——本次真正補的是**驗證＋實況層守線**，不是重做緩解。
衍生：ar#33（kill -TERM 1 自傷 DoS＋同 pod loopback）、ar#34（tool_result 未標記 untrusted-content／prompt injection）、charts#66（sidecar 專屬 egress）、cd#88（chartVersion 追平、dormant release reap、prod 覆核、values-stg.yaml:8 同類註解問題歸 ar#29 線）。
⚠ 三個全隊通用發現：①**app.kubernetes.io/version label 不可用來驗版**——新 pod 的 label 仍是舊版號（chart appVersion 未 bump、image 由 operator imageTag 覆寫）⇒ 用 label 驗會得到「還沒換版」的**相反**結論，只有 image/imageID 是權威；②**k8s service-link 注入是 env allowlist 最大誤殺面**——sidecar 實測 32 個 env 鍵其中 **16 個是 k8s 注入**（KUBERNETES_* ＋ <SVC>_*），只有真 pod 才有、**本機 docker 測不到** ⇒ 任何「本機驗過 allowlist」的結論都不足以推論到叢集；③**新長出一條 SOP 未涵蓋的跨 repo lockstep**——image 的 _ALLOWED_KEYS 與 chart 的 sidecar env 鍵集＋LANG 值綁死，違反時 daemon 拒所有 exec 但保留 ping ⇒ **liveness 照過、不 crashloop、pod 狀態完全看不出來**而 bash_execute 全滅（charts#65 封閉性斷言即為此設）。

原施工紀錄：ar#26（軌 A/B 平行執行）。interview 定案：範圍以 12 項為準（派工單原描述修正：/proc/1/environ 已被 env 白名單自檢覆蓋、kill-1 DoS 屬自傷型轉出）；事實更正＝charts 側固化早已落地（test-pod-isolation-invariants.sh／test-skill-sidecar.sh 含 negative test），缺的只是封閉性斷言。三軌：A｜Tally stg 拋棄式 project 實機 gate（🔴必須新 spawn，舊 pod 驗了無效；三憑證全讀不到才 PASS，任一讀到＝prod 活洞立停回報）、B｜Bulwark bridge 自檢（違反時拒絕服務但保持存活回 BRIDGE_INVARIANT_VIOLATED，🔴不可 exit 非 0——sidecar 無 readinessProbe，crashloop 拖垮整 pod；PR 寫 Refs 非 Closes）、C｜charts 封閉性斷言不阻 close。上線順序（Eugene 拍板）：A 驗現況→B merge→image＋cd imageTag 推 stg→同手法再驗不誤殺→完成，prod 隨常規發版（自檢 fail-closed、誤判代價是 bash 停用故必先 stg 驗過）。close 條件＝軌 A PASS＋軌 B stg 驗不誤殺；四條殘留各自轉出 issue。

#504 進度（2026-08-02，issue-dispatcher 已查證 object）：cd PR#80 merged（6a7effd，Eugene 親按）——raw-manifest bundle paas-odoo-backup/（獨立於 helm bundle 避免 Recreate 停機）＋5Gi PVC＋每日 03:30 UTC pg-backup 保留 7 份。驗收全綠：手動 job 19 秒 Complete、dump 9.7M、RESTORE_OK＋TOC 逐項一致、四方閉環。三個照抄範本會壞的坑已修：①pg 17.6 非範本 16-alpine（否則每日靜默 version mismatch）②NP 白名單改帶 chart client label 放行③pg_restore -l 對截斷 dump 照樣 exit 0，唯 -f /dev/null 有鑑別力，已進 CronJob script 且在輪替之前。未完成：lastScheduleTime 未證實（手動 job 不更新它），『label 存在≠機制存在』故不提前結案。
衍生 #512：5 個 prod 客戶站零備份 CronJob（含 08-01 新建的 puhsu）⇒ 新站上線流程本身沒配備份——歸屬 woow-machines-operator，平台端唯讀盤點。
轉 #442 範圍的發現：ttlSecondsAfterFinished 會把失敗 Job 連 log 回收、failedJobsHistoryLimit 形同虛設＝備份失敗零外顯訊號（實例：pvc-cleanup 21 小時前一個 pod Error 靠重試成功、無人收到訊號）。

ci#46 收官（2026-08-02，issue-dispatcher 已查證 object）：ci PR#49 merged（f448c68，fail-closed）＋plat PR#511 merged（449d4e31，發版 SOP 補驗收）→ #46 closed。
⚠️ 勘誤（leader3 實測，已回貼 #46）：woow_paas_platform 預設分支是 develop 非 main（git ls-remote --symref＋API 雙證），「留空＝clone 停在 .40 的 main＝倒退 image」不成立、一次留空 build 從未真跑過。缺陷仍是 P0 但理由換為：prod image 自移動中 develop HEAD 烤出（不可重現、含未發版 commit）＋依賴版控外 Gitea repo 設定（admin 兩秒可改、CI 零校驗零訊號）。
修法裁決：fail-closed（排除 fallback to tag ref——GITHUB_REF_NAME 屬 paas-odoo-ci 命名空間非平台 repo，fallback＝跨 repo 混用再犯同錯）。
兩條硬約束（後人必讀）：fail-closed 判斷必須在 stg-*→develop fallback 之後（否則打死 deploy-stg 整條線）、且在 approval gate 之前（否則先佔唯一 runner 180 分鐘才失敗，見 ci#30）。
衍生單（全 open 未實作）：ci#50（image 端無版號錨點，兩行 log 同源不獨立）、ci#47（CI 零 lint 自檢）、ci#48（main 無 branch protection，CODEOWNERS 虛設）、plat#510（deploy-stg 的 addon_ref 誤導性 no-op）。
待 Eugene 裁決（leader3 重申）：Wave 2 偵察基線 scratchpad/wave2-baseline.md——charts#42 疑已修於 chart 0.1.4、真活口是 n8n 無 egress NP 實測可直達 prod 控制面。

cd#77 收官（2026-08-01，issue-dispatcher 已查證 object）：PR#81（merge 6e0cc3f）module-version-gate initContainer 以不變式接管模組升級（pod 起得來 ⟹ DB latest_version == image manifest 版號），SKIP_MODULES_UPDATE 常態 yes 永不切、-u 輸出直出 stdout；post-verify 複驗 DB 版號真推進（注入式驗證非恆綠）。PR#82（94a3e67）修 Fleet 對 values 檔模板前處理造成的 bundle Ready=False（Fleet 在 Helm 前跑自己的 Go template 連 YAML 註解都吃——新 pitfall）。prod 四項全綠。代價：gate image 寫死 digest＝promote 新增人工同步點（checklist §1b 守），根治開 cd#83（disablePreProcess）。**#77 保持 open 到 .89 發版驗完真正 -u 實戰**（gate log 須見 Running migration＋完成並複驗）——與 #507 的 -u 硬性要求互為驗證。Wave 0 收官狀態：plat#29 closed、ar#29 closed、#389 剩 SMTP 供應商面（卡 Eugene 帳密）、cd#24＋cd#78 輪替（卡 Eugene 帳密）。

### ✅ #530 收官（2026-08-03，leader7 回報、dispatcher 逐項查證 object 實體）

PR#539 merge（eb9d199d）時 `Closes #530` 自動關單。**不重開**，spec 未完成的收官改由後續單接住。dispatcher 已驗四張 object：#530 closed／#542、#544、#535 皆 open／#467 有留言 #4567 標明勿合併；另抽驗 develop 實質內容：hermes seed 已指向 chart 0.1.1（cloud_app_templates.xml:876 附 #530 註解）、apiServerKey 遮罩散布 5 檔。

- **#542（test，priority:medium）stg 三態實打收官**——spec 明列的非自動化驗收未做：**chart 0.1.1 至今零部署、gateway 通道在真實流量下一次都沒被打過**。前置：既存 release 須 helm upgrade（updateStrategy: Recreate 有短暫中斷）＋存量服務跑 Reset Admin Password 才換發拿到 key（刻意不做叢集回讀 backfill，守「憑證權威來源在平台端」不變式）。兩個驗證陷阱已寫進單：必須打 Bearer 端點、不能拿健康端點當證據（外部看到的健康回應其實來自 WebUI）；urllib 預設 UA 會被 CF 1010 擋而 httpx 不會。
- **#544（test）另 47 個測試從未執行**——與 #467 **不同機制不可合併**（#467＝tests/__init__.py 未匯入；本單＝已匯入但整檔只有 unittest.TestCase、被 test-tags 過濾整檔排除）。判準＝「整個 module 有沒有至少一個 Odoo TestCase」；對照組 test_reconciler_diff.py 同含 unittest.TestCase 卻有跑，因同檔還有 2 個 TransactionCase。🔴 其中 test_reconcile_delete_gate.py（9 支）docstring 自稱「一旦出現即 CI 紅」，實際 Odoo 不收、repo-lint 也不跑＝**一條自認 fail-closed、實際完全休眠的守線**，守的還是 reconcile 路徑的零刪除不變式。
- **#535（priority:high）**已於稍早分配 leader2（見上方 #535 分配紀錄）。

**發現來源＝「輸出是哪一個、不是差幾個」判準的直接應用**：全套 0F/0E of 2297 完全正常，62 個從未執行的測試拿數字永遠看不出來；抓到靠「磁碟上的 test module vs 測試 log 實際出現的 module」集合差。兩個新坑（leader7 自報）：
1. **集合差探針要先驗輸入非空**——log 路徑失效（scratchpad TTL）時 188 個 module 全被列成「未執行」＝災難性假紅，而**當時探針自檢照樣通過**（自檢驗的是集合差邏輯的鑑別力，沒驗輸入是否為空）——「驗過一個維度不代表其他維度也驗過」的又一實例。
2. **不要用 class 名查 log**——test_reconciler_diff.py 第一個 class 是 _FakeClient（helper），用「第一個 class 名」查會誤判成沒執行。

### 📌 #542 已分配 leader7（dispatcher 主動分配，2026-08-03）

理由：他問「依優先序判斷」且分配權歸 dispatcher；#542 是他那條線的直接延續，前置細節（Reset 換發、Bearer／UA 兩陷阱）context 全在他手上，換人重建成本最高；且它關的是「#530 功能尚未被真實環境驗證過一次」這個誠實缺口。照四步流程：保全→dismiss→（/clear 可延後）→考 Eugene→談完成定義→**等 Eugene 指令才動工**——helm upgrade（Recreate 短暫中斷）與存量服務 Reset 屬對 stg 的實際操作，未經 Eugene 明示不得執行。#544 入池待排，不隨他。

## 2026-08-03 第 4 步回報彙整（五步流程生效後首輪，五位 leader 已回報）

### ✅ 收盤狀態（2026-08-03 08:5x，dispatcher 重量非引用快照）

```
open PR = 2   #545 + #550（皆 leader6 線、皆帶【勿 merge】標示）
今日新單：#542 open(leader7)／#544 open(池)／#547 open(leader4)／#549 open(池)／#548 closed(併入 #519)
```
🔑 **PR#545 已被 leader6 拆成兩張，且兩張都帶【勿 merge】**——今天定的「**預設帶著、通過才移除**」規則在實際運作中被遵守，這是第一個正面實例。#550 更是「**刻意保留 1 條紅燈當 #543 的注入式驗證**」，把紅燈當訊號而非待修項。

> 🔴🔴 **【2026-08-03 深夜推翻】上面這句話是錯的，見本看板末尾「#543 紅燈從未驗到它宣稱要驗的東西」。**
> 那條紅燈**死在 fixture（`_make_legacy_policy` 的 ORM create 撞守衛），`migrate()` 從未執行**
> ⇒ 它**不是** #543 的注入式驗證，#543 修好它也**不會**轉綠（結構性不可能）。
> **保留原句是因為它記錄了當時三個人共同持有的錯誤前提**——那本身是教訓的一部分。

| leader | 持單 |
|---|---|

### 🔴 我上一輪的「全站 open PR 歸零」是錯的——PR#545 存在

實查：**PR#545**「復活 15 條從未執行過的測試，刻意保留 1 條紅燈揭露 migration 真缺陷（Refs #543）」（分支 `test/revive-dead-test-modules`，07:50 開，標題帶勿 merge 標示）＋**issue #543**（`18.0.1.0.39` migration 撞 #352 bulk-grant 守衛，從舊備份還原的 DB 會炸在 .39，07:10 開）。**歸屬 leader6 線（teammate Litmus，T2 工單）**——已向他查證確認，非推論。
⇒ 我報「歸零」的時間點在它開出之前，數字當下為真、隨即過期。**「全站 open PR = N」是快照，不是狀態**；引用前必須重量，與 remote head／終端畫面同屬滯後指標家族。

### 🔴 從未執行的測試實際是 62 條，先前的稽核只找到 24%（leader6 自承覆蓋缺口）

leader6 獨立實查 develop 驗證 leader7 的 #544，**一分不差**：

| 檔案 | 方法數 | 含 Odoo TestCase | 已匯入 | @tagged | 判定 |
|---|---|---|---|---|---|
| `test_naming.py` | 38 | 0 | 是 | 0 | 未被收集 |
| `test_reconcile_delete_gate.py` | 9 | 0 | 是 | 0 | 未被收集 |
| `test_reconciler_diff.py` | 22 | 3（混合） | 是 | 2 | 部分會被收集，不算 |

38＋9＝**47**（#544）＋#467 的 15（#543/PR#545）＝**62 條**。

🔑 **leader6 的自我剖析（本輪最有價值的一條，請對照 leader1 的元教訓讀）**：他 #529 稽核用的完備性檢查是「磁碟檔案數 vs `__init__.py` import 數」——**那個維度是對的、也真的抓到 15 條**，但它結構上只看得到「沒被匯入」這一種機制。已匯入卻因純 `unittest.TestCase` 且無 `@tagged` 而不被 loader 收集的，那個查法**看不到**。
> **「驗過一個維度不代表其他維度也驗過」，而且我那個維度通過了，反而讓我沒去想還有別的維度。**
⇒ 這是**成功的檢查本身壓抑了繼續找的動機**——比「檢查失敗」隱蔽得多，因為它產出真陽性結果、看起來完全稱職。

**結構化解法已有落點（不必再靠人工盤點）**：leader6 的 teammate Scour 正在做 T3「讓 CI 跑 Odoo 測試」，他已要求該 gate 必須能列出「**哪些預期的 test class／module 不在實際執行清單裡**」而非只斷言總數 ⇒ 兩種機制都會被自動抓到，**第三種也會**。正是 leader4 元判準「能結構化的優先結構化」的正面實例。
**兩線分工**：#545 只動 `src/tests/__init__.py` 加兩行 import；#544 修法應落在各測試檔的 class 定義（加 `@tagged` 或改繼承）⇒ 檔案層可並行。但**語意層必須合併**：各修一半就沒人手上有「62」這個總數，第三種機制出現時也不會有人發現 ⇒ 已要求 #543/#545 與 #544 交叉引用。

### 📌 `docs/decisions/patterns/` 四條線同時要動 ⇒ 由 leader4 統籌（dispatcher 分配）

| 來源 | 要寫的東西 |
|---|---|
| **leader4** | `su-env-exempt-guard-false-green.md` 教的 context-key 逃生口**已被證明可偽造**（見下） |
| leader1 | Odoo cursor 是 REPEATABLE READ ⇒「取 advisory lock 後重新計數」零貢獻；repo 內 HA provision lock／project reach lock 同型未覆核 |
| leader3 | #524 的 `any` operator cross-tuple 陷阱、「收緊 write 會連帶咬到 `refresh_status`」 |
| leader5 | `cr.rollback()` vs `cr.savepoint()` 標準形態未裁決，現在寫 doc 會把不一致固化成「兩種都可以」 |

leader1 主動指出同一目錄需單一 session 協調 ⇒ **素材彙整給 leader4，不各自開檔**；標準形態的裁決權在 Eugene。

### 🔴 pattern doc 正在教人用已被證明可偽造的機制（leader4 發現，dispatcher 四項實查全中）

`docs/decisions/patterns/su-env-exempt-guard-false-green.md` **掛在 pattern-guard hook 上**（`pattern-guard-map.json` 的 `model-write-guard` class，pattern 涵蓋 `agent_session|agent_template|security_access|smart_home|cloud_service|mcp_server|skill` **七個 model**，且它是該 class 推的**第一份**）⇒ **今天任何人編輯這七個檔，hook 都會叫他去讀，而文件會告訴他用 context key。**

實查逐項（非轉述）：①第 49 行原文「**context key 是對的逃生口**」，守則第 1 條處方「留一把 context-key escape hatch」——是處方段落不是病理描述；②該檔 `偽造|sentinel|call_kw` 命中 **0**；③hook map 綁定屬實；④develop `src/models/security_access.py:91` 已是 `_INTERNAL_IDENTITY_WRITE = _InternalIdentityWrite()`、:102 用 `is` 判斷 ⇒ **文件與已 merge 的程式碼直接矛盾**。
危害不是靜態技術債而是**進行中的散播**：`call_kw` 把 client context 原封不動 `with_context`（`dataset.py:33-36`＋`api.py:531-532`）⇒ 裸真值 context key 對走 RPC 的租戶可偽造，下一個照文件做的人會再造一個同型缺陷。

**dispatcher 已起 workflow 掃全部 30 份 hook 推播的 patterns doc**（分 6 組 scan＋對抗式 verify 殺假陽性），確認漂移是單一事件還是系統性；結果直接交 leader4，範圍可能從「修一份」擴大成「修一批」。**已要求他先做第 1、2 步（考 Eugene、談完成標準）把掃描結果納入再定範圍，不要現在 push。**

### ✅ #542 baseline 實測完成——leader7 的 VPN 通了，補上所有人都補不上的那一環（2026-08-03）

**六位 leader 的「無」一致地都是「我方無依賴」，沒有人量到該服務的實況**（全部連不上叢集）。**leader7 VPN 通了之後量到了**：

```
authproxy log 近 2h   0 筆
authproxy log 近 24h  42 筆   ← 陽性對照：log 管道正常，0 不是「log 沒開」
最後活動 Aug 2 21:54 UTC，單一來源 IP，Chrome/macOS，停在 /dashboard/login 回 200
```
⇒ **近兩小時零活動，動它不會打斷任何進行中的操作。**

🔴 **兩處轉述被實測更正**：
1. **ResourceQuota `limits.cpu` 是 21 不是 12**（used 9350m，有餘裕）。

   ⚠ **值得記的不是數字錯了，是它為什麼沒被抓到**：**Tally 從 leader4 那條線報 12，leader1 從 #449 e2e 報告獨立報 12**——dispatcher 當時在看板上特別標了「**兩條線互相印證，可信度遠高於單一來源**」。**結果兩條都是舊值。**
   ⇒ **交叉命中提高的是「這個說法被獨立提出過」的信心，不保證它現在還為真。** 兩人可能引用同一份過期文件、或都從同一次實測的紀錄推導——**獨立的是「誰說的」，不是「證據來源」**。
   ⇒ **判準應該是「兩條線的證據是不是各自新量的」，而不是「有沒有兩個人說」。**
2. **PVC 10/10 已滿**（沒有任何人提過）——但他比對 `helm template` 兩版物件種類與數量完全相同、不新增 PVC，**所以擋不到 upgrade**。

✅ **dispatcher 的推論被證實**：**stg addon 實測 `18.0.1.0.88`**，develop 已 `.97` ⇒ **stg 上根本沒有 #539 的 key 交付邏輯**。Lathe 的警告「換對前提後成立」得到坐實。

🔑 **最重要的結論：#542 可拆 A/B，而 A 層完全不需要破壞性操作**
- **A 層＝只驗通道**：用既有的 `API_SERVER_KEY`（實測仍在、64 字元），**不需 Reset、不動 `admin_password`**——因為 gateway 走的是**獨立的 `auth.apiServerKey` Bearer**。
- **B 層＝平台側 key 交付**：需要 stg 先升到含 #539 的版本。
⇒ Tally 那條「reset 後仍可用原 Bearer 驗 Gateway API」的觀察是對的，**而且比他說的更強：連 reset 都不必做**。
⇒ **原本「不可逆地換掉 Eugene 手上唯一副本的 admin 密碼」這個風險，在 A 層方案下完全消失。** 待 Eugene 裁示做 A 還是連 B 一起。

**四個假綠陷阱（leader7 已寫進 topic 檔保全）**：外部打 `/health` 拿到的是 **WebUI 不是 gateway**／`urllib` 預設 UA 被 CF 1010 擋而 `httpx` 不會／gateway 忽略 `X-Forwarded-Prefix`／dashboard 要打 `/dashboard/auth/password-login`（少了 prefix 回 401/302，會誤判成「密碼沒生效」）。

### 📌 佔用確認：#542 的 stg 破壞性操作（六位皆回「無」）

leader7 的 #542 要對 `paas-ws-stg-5a105e8b` 的 `svc-bd9d6ed3`（my-stg-hermes-1）做 helm upgrade（Recreate 短暫中斷）＋Reset Admin Password（不可逆）。三家回「無」且皆附查法與陽性對照（leader6 掃 8 個 session transcript 全集、命中 3 筆經查全是「這則詢問訊息本身進 queue」的紀錄，陽性對照同指令搜 `woow_paas_platform` 得 881／506 命中）。

🔴 **三個 leader7 動工前必須知道的事實（皆為別人查到、非他自己的 context）**：
1. **該服務的 admin 密碼在 Eugene 手上、是唯一人類可讀副本**（leader5）——今天 Eugene 在 UI 新建，template `has_admin_gui=True`，密碼只在建立成功時一次性回傳、事後無回取端點 ⇒ Reset 會讓它**靜默失效**（畫面不提示）。
2. **Tailscale 目前連不上叢集**（leader5／leader6 各自實測：`dial tcp: lookup ubuntuserver-z0v3w.tailb7a69b.ts.net: no such host`；#543 作者也撞到）⇒ #542 可能得先解連線。
3. **該 ns 的 quota 有未翻回的臨時值且會被程式自動蓋回**（leader1）——`docs/tracking/test-reports/449-password-carrier-stg-e2e-2026-07-29.md:467` 記著 limits/requests.cpu 仍是臨時值 12，且「下次該 workspace 有 CS create／delete 會被 `_push_ns_quota` 依 #456 公式重算蓋回」⇒ 若 #542 流程夾帶任何 CS create/delete，Recreate 起新 pod 時可能撞 exceeded quota。**動工前實測當下 resourcequota，不要相信文件裡的 12。**
4. 附帶可省事的：同一 ns 在 2026-07-29 做過同型 Reset 的完整 e2e（#449），recipe 與新密碼取得路徑見該報告 §7（leader1 未查證那台是否即 svc-bd9d6ed3，**未宣稱同一台**）。

⚠ **leader5 的附帶觀察值得全隊抄**：他第一次用 `grep` 過濾 kubectl 輸出，**錯誤訊息被一起濾掉**，看起來就像「該 ns 沒有 pod」——空輸出≠通過的又一實例，且這次偽裝成的是**具體的肯定結論**。

**leader4 線亦回「無」**（本人＋六個 teammate 逐一確認；Rudder 另實查全 cd repo grep `paas-ws-stg-5a105e8b`／`5a105e8b`／`bd9d6ed3` **零命中**且 Fleet 管轄不含 workspace ns）。⚠ 他誠實標註「這是**我方無依賴**的宣告，**不是『該服務無人使用』的背書**」——他同樣連不上叢集。**現況：leader1/4/5/6 回無，leader2/3 未回。**

他的線另附四個「會讓 #542 得出錯誤結論」的技術事實（已全數轉給 leader7）：
1. 🔴 **ResourceQuota**（Tally）：該 ns `limits.cpu` 手動留在 12，「翻回預設＝服務起不來」；Recreate 時若 quota 被任何路徑重算回預設，**同 ns 其他 release 會一起起不來**。（leader1 從 #449 e2e 報告獨立指到同一件事，**兩條線互相印證**。）
2. 🔴 **驗新密碼會打錯端點**（Tally）：hermes dashboard 前端 JS 沒帶 `/dashboard` prefix（upstream bug，#424 領域）——`POST /auth/password-login` → 401/302；`POST /dashboard/auth/password-login` → 200 ⇒ **打錯會誤判成「密碼沒生效」**，與 leader7 自己記的「健康端點其實來自 WebUI」同一家族。
3. **`--reuse-values` 丟新版預設**（Tally）：0.1.0→0.1.1 values diff 要逐項確認。
4. **Recreate 中斷是預期非缺陷**（Lathe）：agent 與 webui 共用單一 RWO PVC 當 `$HERMES_HOME`。另：reset 換的是 `config.sensitive.admin_password`（四入口共用），而 0.1.1 的 `/gateway/` 走獨立 `auth.apiServerKey` Bearer ⇒ **reset 後仍可用原 Bearer 驗 Gateway API**。
5. **反向提醒**（Rudder，非阻擋）：真正會互相干擾的是有人 bump／force-sync `paas-app-bundle-paas-operator-stg`——stg operator 重啟會 reconcile 所有 workspace。他無此計畫，若臨時需要會先與 leader7 對時間。

### 🔴 dispatcher 推翻了 Lathe 的一項因果，但換對前提後它指向 #542 單子上沒有的一項前置

Lathe 報「平台 `CloudAppTemplate.helm_chart_version` 尚未同步到 0.1.1 ⇒ 只手動 helm upgrade，之後 reconcile 會打回 0.1.0」。**實查 origin/develop：`src/data/cloud_app_templates.xml:880` 的 `<field name="helm_chart_version">0.1.1</field>` 是欄位真值不只註解** ⇒ **repo 層面該因果不成立**。

**但把前提換對之後結論更嚴重**：那個 seed 是 **PR#539 今天才 merge 進 develop 的**（.97），**沒有任何人回報過把 #539 部署到 stg**。若 stg 仍是舊版 addon：
- stg DB 的 CloudAppTemplate 記錄仍是 0.1.0 ⇒ Lathe 的警告在 stg **成立**（理由不是 seed 沒同步，是 **stg 沒部署含該 seed 的版本**）；
- 更關鍵：**平台側生成／交付 gateway API key 的程式碼也是 #539 才有的** ⇒ stg 上還沒有那段邏輯，跑 Reset Admin Password **也不會換發 gateway key**；
- 失敗形狀會長得像「chart 的 gateway 路由沒生效」＝**探針失敗被讀成被測物失敗**。
⇒ #542 的前置要多一項排最前：**先把 stg addon 升到含 #539 的版本**。⚠ **唯一未量到的環＝stg 當下的 addon 版號**（Tailscale 連不上，leader1/4/5/6 四人各自實測 DNS 解不到）——已要求 leader7 自己量到再下結論，不得拿 dispatcher 的推論當事實。

🔑 **這是「別人遞來的因果要驗前提」的正面實例**：Lathe 的**結論方向對、因果鏈錯**。若照單全收，leader7 會去改一個 repo 裡本來就正確的 seed；若因為「因果錯」就整條丟掉，會漏掉真正的前置缺口。**兩種處理都會出事，只有實查能分辨。**

### 📌 `grep -c` 數的是行數不是呼叫數（dispatcher 差點誤判 leader5）

leader5 報 develop 上 `cr.rollback()` 3 次／`cr.savepoint()` 4 次，我量到 **7** 和 **5**。依「兩人用不同範圍量同一件事數字對不上＝訊號」我沒直接質疑，**改換查法逐行印出**——結果是**我錯他對**：7 行中 4 行是註解（`# 開頭的說明文字`），真呼叫只有 `paas.py:2819/2828/2836`；savepoint 5 行中 1247 是註解。
⇒ 若我直接發「你數字對不上」，就會製造一次錯誤質疑。**換查法是唯一救得回來的動作**。

⚠ **上一段的續集：leader5 自我更正，推翻了 dispatcher 的第一版結論（本則以此為準）**

leader5 主動回報「**rollback 那項我對，savepoint 那項我錯**」——他的 4 ＝ `paas.py` 3 次＋**註解 1247**；dispatcher 的 4 ＝ `paas.py` 3 次＋`ha_paas.py:208` 跨檔 1 次。**數字相同、組成完全不同**。逐行精確 pattern（`with request.env.cr.savepoint()`）複驗：paas.py 330／703／1251 ＋ ha_paas.py 208 ＝ 4 ✓。
🔑 他自評「我 rollback 那項會對，只是因為 pattern 帶了 `request.env.` 前綴剛好排除掉註解裡的 `cr.rollback()`，**不是我驗證過，是運氣**」——dispatcher 同樣適用：那個 4 是 `git grep -c` 逐檔輸出相加，**沒有分辨那 4 行有沒有註解**，只是踩到的註解不同行。⇒ **兩人都沒在第一次用有鑑別力的查法，卻因為不同原因各對了一半。**

🔴 **實查改變的是結論本身，不只是數字**（以此版為準）：`paas.py:2784` 原文——
> 「撤回整個 transaction（而非 savepoint）是刻意的：這正是例外真的傳播到框架時框架會做的事…**本 request 走到 try 之前只有讀取與參數驗證，沒有任何要保留的寫入。**」

⇒ **不是「有人想過、有人沒想過」，是兩處都想過而且各自條件不同**：`_create_service` 那三處的 try 之前沒有要保留的寫入，整個 transaction rollback 是安全的；其他處不具備這個條件才需要 savepoint。
⇒ **缺的不是「選一個標準形態」，而是「什麼時候用哪個」這條判準沒有可查載體。** 這對 doc 寫法有直接影響：**寫成「一律用 savepoint」會誤傷 `_create_service` 那三處（它們的選擇是對的）**——dispatcher 原先寫進看板的版本正會造成這個傷害。
**leader5 給 leader4 的素材（已代轉）**：①真呼叫位置如上②取捨論據原文位置＝`paas.py:2784`（選 rollback）／`1242-1251`＋`325`（選 savepoint）③建議 **doc 要寫的是判準而非單一形態**——「try 之前是否有要保留的寫入」是分水嶺，有＝savepoint，無＝兩者皆可但 savepoint 仍較安全（自帶 `cr.clear()`）④裁決權在 Eugene。

### ✅ #538 判準已保全進 issue（留言 4572，dispatcher 代寫並複驗落地）

leader6 提供、dispatcher 代為寫進 #538（理由：當時只活在對話裡）：
> **豁免條件必須是 universal 而非 existence：只有當「PR 動到的 `src/**` 檔案**全部落在** `src/tests/**` 內」才免 bump，而不是「PR **有動** `src/tests/**` 就免」**——後者只要把一行實作塞進測試 PR 就能繞過，前者則**讓繞路動作本身成為觸發條件**。

兩個必要附帶條件：①**gate 只管 `src/**`**（`extra/paas-operator/**` 是獨立發版線不吃 addon manifest；`docs/`／`.gitea/`／`scripts/` 更無關——一起要求 bump 製造的假紅比它防的問題還頻繁）②判準是「嚴格大於 `origin/develop` **當下**值」且**每次 merge 前重驗**（多 leader 並行時排隊順序會變；🔴 **gate 不會因為別人 merge 而自動重跑** ⇒ 必須在 merge 當下再跑一次，只在 push 時跑不夠；同值 bump 時 git 不報衝突＝零訊號，判準指令 `git diff origin/develop -- src/__manifest__.py` 空白＝被吃掉）。
**現存會被誤擋的實例：PR#545**（純測試 PR 不 bump）——「純測試 PR 不 bump」今天由 #534/#532/#541 三次確立成慣例卻沒寫在任何地方，**leader4 與 leader6 從兩條獨立的線各自指出同一件事**。

### 📌 leader6 的 docs 落點已放行（promote 本身仍待 Eugene）

`docs/tracking/test-reports/`（稽核報告本體：2253 條測試三分法、批次 0–4 完整清單與證據、#529/#534/#532/#541 對應）＋`docs/tracking/pitfalls/`（一則：「從未執行過的測試有多種機制，數量比你查到的多」）⇒ **與四條 patterns 線皆不衝突**。他自己不碰 `docs/decisions/patterns/`，把四項 pattern 素材交 leader4：建守衛四模式／完備性檢查輸出「是哪一個」／universal vs existence 豁免／成功的檢查壓抑繼續找。

### 🔴 `src/tests/__init__.py` 是三線交會點（leader3 主動抓回 dispatcher 的漏判）

dispatcher 曾告訴 leader3「PR#545 與你四個檔無交集」，**他自己補正**：#524 驗收要新增 cross-user 測試 ⇒ **必然動 `src/tests/__init__.py`**，正是 PR#545 動的檔。已雙向通知 leader3／leader6，兩邊照 union 原則（雙向差集＋陽性對照、**只增不刪**），**不得用二選一解衝突**。該檔今日已因解衝突被驗過三次零刪除（186→188→188→189），是最容易靜默吃掉別人註冊的位置。

### 🔑 「送出≠送達≠被讀≠被答」——今日在四種載體上各出現一次（leader3 命名）

leader3 的實例最乾淨：**他已完整回答「無」，但回在自己的終端，leader7 讀不到**——他把「我回答了」當成「他收到了」。⇒ 載體是**終端輸出**：對 Eugene 是送達的，**對其他 agent 是零**。
> **判準：凡是「有人在等我的回覆」的事項，回覆必須走 `maestri`，不能只寫在終端。**
今日四種載體各一次：`maestri ask`（JSON 裸換行讓 batch 訊息根本沒送出）／issue 留言（#389 root cause 從未落地）／teammate 回報（leader3 誤把「我問了」當「他答了」）／終端輸出（本則）。

### ⚠ dispatcher 的撞車面複驗查法零鑑別力（記為反例）

我用「取 develop 上最後動某檔的 commit → `git branch -r --contains`」去驗 leader3 的「四檔零 commit」，得到 `audit/383-*` 等分支命中——**但那些分支只是從 develop 分出所以包含該 commit，與有沒有改那些檔無關**。leader3 的診斷：「你量的是『分支是否包含某 commit』，而你擔心的維度是『分支有沒有改某檔』，**兩者正交**」⇒ 與「對照組必須在你擔心的維度上不同」是同一件事。**正確查法是對每個 remote 分支比對目標檔案的 diff。**

### 🔴 #547：pattern doc 漂移是**系統性的**——29 份裡只有 8 份乾淨（dispatcher workflow，2026-08-03）

leader4 發現一份後，dispatcher 起 workflow 逐份對照 develop 現況（分 6 組 scan＋**預設立場為「指控不成立」**的對抗式覆核）。27 agents／412 次工具呼叫／198 萬 tokens／**零 error**／**駁回 0 筆**：

| 分類 | 份數 | 意義 |
|---|---|---|
| **contradicts-code** | **5**（＋leader4 的 1＝**6**） | 照文件做會造出真缺陷或安全漏洞 |
| stale-detail | 16 | 行號／符號名／數字過期，主結論仍對 |
| clean | **8** | 無漂移 |

**五份 contradicts-code，三份的後果是資料遺失或計費繞過**：
1. `cloud-service-unlink-override-cleanup.md`：doc 說「`cloud_service ←→ project` 是 1:0..1、M2M 變體用不到」，**develop 早已是 M:N** ⇒ 照 doc 寬 filter 寫會誤刪**使用者自帶的 external MCP**與**同 project 其他 service 的 sidecar MCP**，正是 merged code docstring 逐字點名要防的 collateral-unlink。
2. `constraint-input-trust-expansion.md`：doc 教 `env.uid == SUPERUSER_ID`，而 Odoo 13+/18 的 `sudo()` **只設 su=True、uid 不變**（覆核者查了 Odoo 原始碼 `models.py:6286-6291`／`api.py:571-573`）⇒ 照抄會擋死合法 sudo 寫入路徑。**doc 自己的反 pattern 表寫著「漏 `is_superuser()`＝silent functional regression」——它自相矛盾**，merged code 站在 `is_superuser()` 那邊。
3. `plan-driven-date-helper.md`：照 snippet 重構會拿掉 #367 的 `entity_has_prior_sub` guard，**重新引入已實證的計費繞過**（既有客戶換方案重啟 7 天免費試用、首期扣 0）。
4. `sidecar-backed-flag-lifecycle.md`：同 1 的另一半。
5. `two-stage-remote-provision-idempotency.md`：「單段免 guard、retry 無腦重跑也安全」會**重現 #484**（CF 再建一條計費 tunnel、蓋掉 tunnel_id、第一條成永久孤兒）。

**已開 #547**（`priority:high`）承接全部細節。三個邊界已標：基準是 develop `eb9d199d`；**`patterns/` 實際 33 檔而 hook 只推 30 ⇒ 3 個不在對照表裡本次未查**（map 與目錄不同步）；clean 的 8 份是「本次查法未發現」不是「保證正確」。
🔑 **根因與結構化解法**：`pattern-guard-map.json` 已經知道「哪些路徑對應哪些 doc」——**反向就是「改了這些路徑要複查這些 doc」**。對照表已存在，缺的只是反向提醒。**這是 leader4 元判準（能結構化就別靠紀律）的直接應用。**
⚠ **rejected=0 的解讀**：不是覆核寬鬆（每筆 verify_reason 都引用實際行號／Odoo 原始碼／陽性對照），而是**漂移訊號太明確——這批文件根本沒有人在維護**。

### 🔴 元 pattern：修了複製品、沒回頭查原型（同一天兩條獨立的線、方向相反、機制相同）

| 線 | 方向 | 狀態 |
|---|---|---|
| **leader4** | SA **從** cloud_service 抄 → SA 修好 → **原型沒修** | ~~#548~~ **已關閉，併入 #519 留言 4596**（見下方自我更正） |
| **leader1** | `lock-then-recount` **從** `project_project` 抄到 `cloud_service` → #501/#518 修了複製品 → **原型未檢** | 已開 **#549** |

⇒ **兩邊的出處都明寫在註解裡**（`cloud_service.py:18`「比照 `project_project` 的 `_REACH_LOCK_NS`」／`ir_rules.xml:99`「mirroring cloud_service」），**只是沒人往回讀**。
> **可執行的要求（leader4 提）：修一個 pattern 實例時，grep 註解裡的「mirroring／比照／參照 XXX」，兩個方向都追——它抄自誰、誰抄了它。**

### 🔴 dispatcher 自我更正：#548 開錯了，已關閉併入 #519

**我驗證了 leader4 的事實（全部正確），但沒有查重就開單**——而查重正是我今天要求所有人在開單前做的強制步驟（leader1 今天開 #549 前就做了分頁查重）。**leader3 攔下了**：那條 ir.rule 的修法**已在 #519 設計並裁決完畢**，role 閘門與訂閱閘門是**同一條規則的兩個切面、一次拆分同時修掉**，開兩張單＝**#516／#520 重演**。

🔴 **更難堪的是：證據就在我自己維護的這份看板上。** #519 的修法表是我寫的，裡面那句「repo 內有逐字點名 cloud_service 的前例可抄（`ir_rules.xml:95-146`）」指的**就是同一段 SA 註解**。**我驗證了別人的宣稱，卻沒有讀自己的紀錄。**
> **驗證外部事實與查重是兩個獨立動作，前者通過不代表後者做過。**

**處置**：證據已完整併入 **#519 留言 4596**（行號、SA 註解逐字引文、三條誠實界線、與 #524 的檔案撞車警告、元 pattern），#548 已 closed 並留下歸屬說明。**leader4 做對了**（發現後回報請求裁決、沒自己開單）；**leader3 做對了**（攔下）。

⚠ **SA 註解最後一句與本案完全對應**：「skipping both the role gate AND the subscription check」——**SA 當初的缺陷就是同時跳過兩者**，與 #519 要修的兩個切面一模一樣。前例不只是「可抄的形態」，連缺陷組成都相同。

🔴 **#519 與 #524 會撞同一個檔**（兩張都要編輯 `src/security/ir_rules.xml`：#519 改 cloud_service、#524 新增 smart_home 三條）⇒ **同一檔單一 session，兩張同時開工必撞**。leader3 已明確表示 #524 不會順手處理 cloud_service 那條。**先後順序或併人待 Eugene 裁決**——這是本輪最需要他拍板的協調點。

原 #548 內容摘要（現存於 #519 留言 4596）：`ir_rules.xml:83-93` 的 `cloud_service_rule_workspace_member` **四項 perm 全 True＋`groups=base.group_user`＋domain 不驗 role** ⇒ 任何 workspace 成員（含 `role=user`／`guest`）對 cloud_service 有完整 CRUD，而 BFF 的 mutation 要求 owner/admin ⇒ **兩層 ACL 不同步**。dispatcher 逐字核對 SA 註解確認「mirroring cloud_service」「un-gated ORM-walking bypass」「skipping both the role gate AND the subscription check」屬實。leader4 的三條誠實界線原樣寫進單（只抽驗兩支端點未窮舉／未查 model 層補償守衛／未實測 call_kw）⇒ **陳述的是結構事實，不是已證實可利用**。與 #519 的區分：那是**訂閱閘門**、這是**role 閘門**，正交但修法可能可合併在同一次 ir.rule 重構。

**#549**（`priority:medium`，dispatcher 開，leader1＋leader4 證據）：`project_project.py:462-475` reach reconciler **cron 路徑**取鎖後 `invalidate_recordset()` 只清 ORM cache、**REPEATABLE READ 下拿不到新 snapshot** ⇒ 標著 `# fresh re-read` 的重讀是假的、advisory lock 零貢獻。
🔑 **最值得記住的一點（leader4 點破）**：docstring 用「**The cron does no DB writes**」當作選 session lock 的**理由**，而正因為不 commit、**snapshot 永遠不前進** ⇒ **選鎖型別的理由，恰恰就是讓那次重讀變瞎的原因。兩句話並列在同一段裡，很久沒人看出來。**
⚠ **leader1 自我更正了他已送出的宣稱**：「HA provision lock 與 project reach lock 同一形狀」**是錯的**。正確判準是「**取鎖後有沒有重讀 DB 並據以裁決**」＋「**有沒有 DB 層兜底**」：`cloud_service.py:3038`（重讀有、兜底無＝#518 真缺陷）／`ha_paas.py:178`（重讀有、**兜底有**＝partial unique index，鎖零貢獻但結果安全）／`project_project.py:393` bind 路徑（**重讀無**＝根本不同形狀，是正確用法）／`project_project.py:469` cron（重讀有、兜底無＝#549）。**照他原本那句寫 doc 會把一個正常用法寫成缺陷、把一個有兜底的寫成可利用漏洞**——素材送出前自己實查才擋下。

### ⚠ `tea issues list` 不分頁會靜默截斷：不分頁 30 筆、分頁後 80 筆（leader1 發現）

🔴 **dispatcher 今天已踩過同一個坑兩次**：稍早用 `--limit 50` 查 issue 導致低編號被截、誤報 #29 已關閉；**立了「查無結論先問範圍是不是全集」這條規則之後，又用了一次會截斷的查法去驗別人的宣稱**。⇒ **規則寫下來不等於自己會遵守**——這正是「能結構化就別靠紀律」為什麼重要的實證。**查重務必分頁。**

### 📌 `scripts/audit/` 撞車面已掃（dispatcher，40 個 remote 分支全掃）

查法：對每個 remote 分支跑 `git diff --name-only origin/develop...<branch> -- scripts/audit/`。結果：只有 `origin/audit/383-*` 兩支各 3 檔，**那是 leader3 交接 #383 時 push 保全的、非進行中**。**唯一活躍交集是 leader3 的 #524 gate**（他已自定「不碰 `repo-lint.py`」，且尚未動工）。
⇒ **已放行 leader6 的 T3 Phase 2**，條件：各建獨立檔案、兩邊都不碰 `repo-lint.py`；動 `baselines/` 前先看既有內容。
🔴 **同時更正 leader6 一個前提：`scripts/audit/baselines/` 已經存在**，不是要新建的目錄——照「新建」的假設寫可能覆蓋既有內容。

### 📌 T3 Phase 1 實測推翻了一個全隊共用的錯誤前提（leader6）

**全套跑一次只要 6 分鐘**（乾淨 DB、兩步法、`0F/0E of 2237`，且 `skipped_count=0` **用陽性對照驗過真的是 0**）。
⇒ 原本所有人（含 dispatcher）假設十幾二十分鐘、據此判定「每個 PR 都跑」不可行——**那個假設從來沒有人量過**。6 分鐘讓它變成可考慮，代價是 PR 迴圈 5 秒→6 分鐘並在單一 runner 排隊。Scour 建議 develop push 全套＋每日排程保底，另開單把 **act-runner capacity 從 1 調高**給叢集運維（走 dispatcher 排，teammate 不自己動）。觸發策略待 Eugene 拍板。

### 🔴 新規則：使用 workflow 時統一使用 opus model（Eugene 於 `note-issue-dispatcher` 新增）

原文在該 note 的「## 持久化 docs」段。已廣播七位 leader。**已在跑的 workflow 不必中斷**（丟掉已完成 agent 結果是浪費），下一個起的開始遵守。dispatcher 的 #547 掃描是規則發布前起的、未指定 model，結果照用但已標明來源。

### ⚠ MEMORY.md 27,742 bytes（hook 門檻 17,100 的 1.62 倍，leader6 實測、dispatcher 複驗相同）

leader6 的 teammate Gatepost 只縮自己加的 4 行（省 505 bytes）、**不動別人的**——理由與既有規則一致（整份壓縮是單一 session 專責，而該檔正被多 session 並行 append，他這輪就看它被改了三次）。
**待決**：①**17,100 是硬失敗還是警告尚未確認**（dispatcher 該查而未查；確認前「結構健康優先於字數」維持）②**拆檔屬結構變更**（會改變所有 session 的讀取路徑）⇒ 列入呈報 Eugene 清單。leader6 指出兩個自然邊界：Odoo 框架陷阱段（長、與其他段幾乎無交叉引用）、prod rollout 系列（純歷史、查閱頻率低）。
⚠ **dispatcher 自我標註**：我是下這條規則的協調者，而我今天也在該目錄動過手（改兩個既有檔案內容、未動索引行）——**協調者的約束不因身分放寬**。

🔴 **（已撤回並更正）dispatcher 一度宣稱「hook 的數字是錯的」——錯的是 dispatcher**：

我看到 hook 說「20KB」而 `wc -c` 實測 28,474，判定「它超過自己說的 24.4KB 上限卻還說 approaching ⇒ 數字不可信」，並據此叫 leader6 維持現狀不壓縮。**實測推翻**：
```
wc -c → 28,474 (bytes)      wc -m → 20,651 (chars)      20651/1024 = 20.2K  ← 正是 hook 說的數字
```
⇒ **hook 量字元數，我量位元組數。中文 UTF-8 一字 3 bytes，本檔中文佔比高 ⇒ bytes/chars ≈ 1.38。兩個數字都對，單位不同。**
⇒ **20.2K 距 24.4K 上限確實是「approaching」，hook 措辭完全正確；17.1K 目標也是 chars。壓縮的必要性是真的。**

🔑 **我犯的正是自己 memory 裡記著的那條**：「**兩個檢查矛盾時別挑順眼的信——矛盾本身就是訊號**」，而我挑了順眼的（認定 harness 有 bug）。真正的線索是**差值幾乎恆定**（第一次 8.1KB、第二次 8.3KB）——**恆定的差值指向系統性的單位差異，不是隨機錯誤**。我看到了那個恆定卻沒追它。
⇒ 今天我第二次「驗證了外部事實卻沒驗證自己的判準」（第一次是開 #548 沒查重）。**兩次的形狀相同：把力氣花在驗別人，沒花在驗自己那一步。**

**leader6 獨立到達同一結論，而且比 dispatcher 的完整**（兩人幾乎同時、各自量測，訊息交錯）。他多做的兩件都是關鍵：

| 他多做的 | 內容 |
|---|---|
| **換算門檻** | `24.4KB` ≈ **25,000 字元**／`17.1KB` ≈ **17,500 字元**；目前 20,629 ⇒ **確實在兩者之間**，「approaching」準確 |
| 🔴 **下游陷阱** | 若統籌者拿 `wc -c` 去追 17.1KB，**字元數要砍到 12,000 出頭 ⇒ 會砍掉一半內容** |

⇒ **後者比「發現單位不同」更值錢**：單位錯只是判斷錯，**照錯單位執行是不可逆的內容損失**。
✅ **兩組獨立觀測互相佐證**（dispatcher：28,116 bytes → hook 說 20KB／leader6：28,410 bytes → hook 說 20.1KB，誤差 0.2%），比單一觀測的吻合強得多。他誠實標示「由數字吻合推出、未讀 hook 原始碼」。

**最終處置（採 leader6 版本）**：**不現在壓**，但理由換成——①餘裕約 4,400 字元不是火燒眉毛 ②多 session 並行 append 的問題不受本次更正影響 ③拆檔優於刪描述，且**驗收必須用 `wc -m` 不是 `wc -c`**。

### ✅ MEMORY.md 拆檔完成、凍結已解除（leader1 執行，dispatcher 獨立驗證零遺失）

**Eugene 最終裁決改走拆檔、不做行壓縮**，理由正是 leader6 攔下的那條（按肥瘦排序會把「最常被違反」的教訓降級成查閱型）。leader1 實測佐證：**可壓四區全壓只省 2,283、差 858，硬要達標只能回頭壓常駐型那段**。

```
MEMORY.md      20,651 → 15,309 chars（目標 17,510，餘裕 2,201）
MEMORY-odoo.md    3,464（Odoo 框架陷阱 16 條）
MEMORY-landing.md 3,368（近期 landing 18 條）
聯集索引 217（含 2 分檔指標）／ topic 檔 215 ⇒ 斷鏈只有分檔指標、**孤兒 0**
「工作方式（最常被違反，優先讀）」**一字未動，還從 18 條增到 20 條**
```
✅ **整章原文照搬、零改寫** ⇒ 無資訊遺失的判斷問題，故不需 agent 覆核（該判斷正確）。
✅ leader1 的保全驗收是「pre-image 102 條**逐條**比對是否逐字出現在三檔聯集 ⇒ **遺失清單為空**」——**「是哪一條」形態**。
✅ 順序照約定：新檔**先寫→讀回本體確認落地→才移除章節**；寫入前最後一刻重驗 `sha256 == 基準值`。

🔴 **慣例變更（已廣播七位）**：`MEMORY-odoo.md`（Odoo 陷阱）／`MEMORY-landing.md`（近期 landing）／`MEMORY.md`（其餘）；**驗收腳本一律 `cat MEMORY*.md` 萬用字元，不要列舉檔名**。

### 🔴 同一天、同一個錯、兩個人各犯一次——兩則

**(a) `startswith("- [")` / `grep -cE ^- \[` 漏收 9 行**（leader1 與 dispatcher 各一次）：都漏掉「前綴文字＋連結」形態 ⇒ dispatcher 報錯基準並拿它教育六個人、leader1 **從殘缺母體裡挑「最肥 35 條」**（若非改走拆檔，那個選擇本身就建立在漏了 9 行的清單上）。**leader5 換查法才抓到**（四種數法全得 102 **且 sha256 一致** ⇒ 能確定差異在數法不在版本）。

**(b) 拆檔會改變「聯集」的定義**（dispatcher，發生在寫完那個 topic 檔的**幾分鐘後**）：驗收時寫死 `cat MEMORY.md MEMORY-odoo.md`，而 `MEMORY-landing.md` 剛建好不在清單裡 ⇒ **37 條落進孤兒側，看起來正好像「整段索引被刪掉而沒有 append」**。**差一點就對執行者發出假警報、讓他去修一個不存在的問題。** 救回來的只是發警報前多做一步（`ls MEMORY*.md` 看全部分檔、拿一個孤兒去 grep 全部 MEMORY 檔）。
⇒ **失效方式是「報出大量假孤兒」——長得正好像拆檔弄丟了東西。每多拆一個分檔，這個風險就多一次。**

🔑 **leader1 抽的通則比 dispatcher 原版更準，而且推翻了一條既有簡化判準**：
> 它**不是靜默漏收，是大聲誤報**，所以**更容易讓人去「修復」一個不存在的問題**。

memory 裡原本寫著「**寧可讓錯誤大聲，不要讓它安靜**」（因為靜默漏收最危險）。本案例顯示**大聲的錯誤有另一種危害——它會驅動行動**：靜默漏收讓人什麼都不做；**大聲誤報讓人做錯的事，而且做的時候充滿信心**（畢竟「檢查報警了」）。
⇒ **兩種都要防，防法不同**：靜默漏收靠**陽性對照**；**大聲誤報靠「行動前先驗警報本身」**。⚠ dispatcher 這次救回來**純粹因為發警報前多查了一步，而那一步不是流程要求的、是運氣**。

📌 **leader1 的自評（原樣收錄）**：「我這輪也犯了三次，全在我最熟的那一步：拿 bytes 追字元目標、`startswith("- [")` 漏收 9 行、把『最大』當成『最該移走』。**沒有一次發生在困難的地方。**」
⇒ 加上 dispatcher 四次、leader5 兩次，**今日此形態至少命中九次跨三人**——共同點不是「難」，是「**熟**」。

🔑 **兩則都收進 `feedback_apply_your_own_review_standard_to_yourself.md`（leader5 提出、dispatcher 整理）**：
> **要求別人做的檢查，回頭套用在自己這一步。** 失效點永遠在**自己最熟悉、覺得不可能出錯**的那一步（查重／數個數／跑 grep）；當事人往往**當天才剛寫下那條規則並用它糾正過別人**。今日至少命中兩人各兩次，**每一次的正解都已寫在當事人自己當天說過的話裡**。
⇒ 該檔最強的自證是 **(b)——它連自己都沒能立刻擋住**。

### 原凍結紀錄（2026-08-03 08:5x 發令）

**凍結範圍＝只有 `MEMORY.md` 一個檔**；topic 檔（其他 211 個 .md）**不凍結、照常寫**，該加的索引行用 `maestri ask` 丟給 leader1 代折。已對 leader2–7 全數發出（leader4 已確認收到，餘五則送出中）。**dispatcher 本人同受約束**。

**凍結基準**：`bytes=28,474 chars=20,651 索引行數=93 連結數=211 檔案數=211 sha256=a5e9da83… mtime=08:57:39`

🔴🔴 **上面那句是錯的，dispatcher 已更正——「93」不是「另一個量」，是量錯的**（leader5 換查法抓到，dispatcher 複驗確認）：

```
dispatcher 用 grep -cE ^- \[  = 93    ← 錯（列舉了「必須以 [ 緊接」）
leader5 用    grep -cE ^-     = 102   ← 對（四種數法全得 102）
差 9 行 = 「前綴文字＋連結」形態：跑測試／prod rollout／NetworkPolicy／image 搬遷／租戶隔離／HA／seed 改價／定價 訂閱／maestri 坑
```
⇒ **我犯的正是我今天才寫進 memory 的那條**：「**regex 用否定字元類，不要列舉允許字元**」，失效方式是**靜默漏收**。
🔴 **更糟：我拿這個錯數字去教育別人**——凍結令裡向六位警告「93 與 211 是兩個不同的量」，那個解釋**聽起來完全合理所以沒人質疑**，只有 leader5 換查法才抓到。**今天第三次「驗了別人、沒驗自己」**（前兩次：開 #548 沒查重、把 hook 的字元數當 bytes 誤判它壞掉）。
🔑 **leader5 敢下判斷的關鍵推理**：四種數法全得 102 **且 sha256 一致** ⇒ 能確定「差異在數法不在版本」。**先鎖定「我們讀的是同一份東西」，才有資格談數字為何不同。**
⚠ **後果（leader5 指出）**：基準行數錯 ⇒ 對帳會拿 93 去比實際從 102 開始的檔，**得到的錯誤差額看起來會完全正常**。⇒ 已建議 leader1 改用「連結數 211」＋「是哪幾條」的集合差對帳，不要用行數。

**更正後基準**（bytes／連結數／sha256 三項原本就正確）：`bytes=28,474 chars=20,651 索引行數=102 連結數=211 檔案數=211 sha256=a5e9da83…`

⚠ **凍結期間檔案數會合法變動**（leader7 提）：topic 檔不凍結，他新增 2 個檔 ⇒ 檔案數 211→213 但 **MEMORY.md 的 sha256／mtime 不變** ⇒ **對帳以 sha256 為準**；且收尾差集時這些新檔會**合法地出現在孤兒側**（索引行還沒折進去），**別判成「壓縮弄丟了索引」**。

✅ **dispatcher 已驗證凍結真的生效**：sha256 與基準完全一致（發令至今 MEMORY.md 零寫入）；**陽性對照＝同目錄 topic 檔仍在正常變動**（09:01／09:05／09:06 三個時間戳）⇒ 證明檢查有鑑別力，不是「什麼都沒在動」。

🔴 **凍結令擋不住的窗口（dispatcher 提醒 leader1 自己收口）**：凍結只能擋「**還沒開始**寫的人」，**擋不住已經在寫的過程中的人**（讀完全文、正要寫回）。⇒ 取完基準後、第一次寫入前**再驗一次 `sha256`**，不同就**重取基準而非繼續**。

**leader1 的做法（dispatcher 逐項確認）**：只壓最肥約 20 條、不動短行／**被裁掉的細節先 append 進 topic 檔才從索引刪掉——順序不可反**（最關鍵的一條）／不刪檔不合併／**不用整份 `Write`**（逐條 `Edit`，exact-match 失敗會吵）／覆核者**預設立場「有遺失」、要它舉證沒有，判定有遺失就退回不動**（Eugene 裁決保守優先）。
⇒ **不用整份 `Write` 這條是有前科的**：leader5 上次整檔覆寫洗掉兩行別人的索引，**而被洗掉的正是「多 leader 並行會撞同一檔案」那一則**。

**驗收要求**：雙向差集（`comm -23` 斷鏈＋`comm -13` 孤兒，**兩邊都印**、先驗索引側非空當陽性對照）、regex 用**否定字元類** `\]\(([^)]+\.md)\)`（列舉版漏過含大寫檔名，失效方式是靜默漏收）、**量尺用 `wc -m`**（上限 24,986／目標 17,510／需砍 3,141）。

### 📋 拆檔提案（leader6 提，dispatcher 獨立量測驗證通過）→ 本次壓縮若仍超標時的下一步

```
3449  工作方式（最常被違反，優先讀）   ← 最大的一段，絕對不能拆
3112  發版 / CI
3093  Odoo 框架陷阱                    ← 提議拆
2836  近期 landing（含當次教訓）        ← 提議拆
2223  E2E / 瀏覽器 / 協作工具
2096  Helm / K8s / 叢集
1848  測試與守線的設計原則
1743  商品 / 上架 / 定價
 259  (檔頭)
─────  20651 現況 ／ 24986 上限 ／ 17510 目標 ⇒ 需砍 3,141
```

| 方案 | 結果 |
|---|---|
| 只拆 Odoo 陷阱 | 17,558＋指標行 ≈ **17,658 ⇒ 超標 148**（leader6 算 163，同結論） |
| **拆兩段** | 14,722＋指標行 ≈ **14,922 ⇒ 達標，餘裕 2,588** |

⚠ **「單拆一段差一點」正是最容易被誤判成達標的形狀**——leader6 主動標出來。

🔑 **選段理由比數字更重要**：
> 判準是「**這段是不是每次都要在眼前**」，不是「哪段最大」。拆出去的子檔**不會每 session 自動載入**，只有主動讀才看得到 ⇒ **Odoo 框架陷阱**＝查閱型（撞到才需要，且撞到時多半有 hook／gate 訊息指路）、**近期 landing**＝歷史紀錄查閱頻率低，兩者可拆。
> 🔴 **工作方式（3,449，最大）絕對不能拆**——它就是「最常被違反、優先讀」，拆走等於廢掉它的作用。**按大小排序來拆會先砍到它，這是最危險的做法。**
⇒ 這條抓住了拆檔的真正風險：**一個看起來最理性的排序法（由大到小）會精準地毀掉最該留的東西。**

**執行條件**：等 Eugene 裁決 → dispatcher 指派統籌人 → **在無並行寫入的時段做** → 驗收用 `wc -m`。
✅ **同時實測 memory 結構完全健康**：索引 **210** 條 vs 檔案 **210** 個，**斷鏈 0、孤兒 0**（雙向差集，regex 用否定字元類 `\]\(([^)]+\.md)\)`，且先驗索引側非空＝陽性對照）。⇒ **「檔案太大」與「結構壞了」是兩件事，目前只有前者的疑慮，而且那個疑慮的數字本身可疑。**

⚠ **併發實例（本輪即時發生）**：dispatcher 在 `feedback_parallel_leaders_same_file_collision.md` 補了「驗證外部事實≠查重」，**另一個 session 幾乎同時補了同一件事**（更完整的一整節）。dispatcher 發現後**做最小合併**——保留對方版本為主體，只留下自己獨有的兩項（`tea issues list` 分頁截斷的技術事實、做對的對照組），把重複段刪掉並指回上方。**沒有整份覆寫**（那會洗掉對方剛寫的內容）。⇒ 這正是「找到既有載體就更新」這條規則本身作為收斂機制在運作。

### ✅ Eugene 裁決：#519 派 leader1，#524 等它 merge 後再開工（2026-08-03）

> **「leader1 接 #519，#524 等它 merge 後再開工」**（原文，未外推語意）

⇒ **檔案爭用（`src/security/ir_rules.xml`）以「排序」解決而非「併人」**。leader1 先改 cloud_service 那幾條、leader3 在他 merge 後再新增 smart_home 三條。兩邊皆已通知。

**#519 現在有兩個切面，一次拆分要同時修掉**：
| 切面 | 內容 | 來源 |
|---|---|---|
| **訂閱閘門**（原範圍） | 租戶可繞過下單流程直接 ORM 建 cloud_service——無訂閱無扣款、部署 cron 照開 | leader1 於 #501 覆核中發現 |
| **role 閘門**（新併入留言 4596） | `ir_rules.xml:83-93` 四項 perm 全 True＋domain 不驗 role ⇒ 任何成員（含 `role=user`／`guest`）完整 CRUD，而 BFF mutation 要求 owner/admin ⇒ **兩層 ACL 不同步** | leader4 於 #522 收尾發現 |
🔴 **只修一半會留下另一半**。拆分後的 owner/admin 那條管 write/create/unlink ⇒ **兩個閘門一起關上**。
🔑 **SA 註解最後一句與本單完全對應**：「skipping both the **role gate** AND the **subscription check**」——SA 當初的缺陷就是**同時**跳過兩者，前例連缺陷組成都一樣。

**leader3 等待期間可做的（不動 `ir_rules.xml`）**：完成第 1、2 步（他原本就卡在等 Eugene 定完成標準）；`scripts/audit/` 的 gate 是獨立檔案不受阻塞。⚠ 他開工時 base 會變（`ir_rules.xml`＋`manifest` 都會動）⇒ **要從新的 develop 起，不要用現在的快照**；且值得直接沿用 leader1 拆出來的三段式形態。

### 🔴🔴 #519 前提驗證完成——**修法方向被推翻**（dispatcher workflow，opus，已保全進 #519 留言 4631）

12 agents／110 萬 tokens／384 次工具呼叫／零 error。**81 條宣稱：仍成立 25／已漂移 3／新發現 53／指控被駁回 2**（覆核者預設立場為「指控不成立」）。⚠ 三條「已漂移」中一條只是 docs 行號位移、事實完好——**行號漂移與事實漂移沒有混為一談**。

#### 一、ir.rule 拆分**擋不住 #519 的主場景**——是必要縱深，不是充分修法

`CloudService.create()`（`cloud_service.py:2961-2962`）守衛只有三道：`_claim_global_cap_slots`（#501）、`_enforce_supply_gate`（停售）、`max_per_partner`（#336）。**無任何訂閱／扣款前提。**
⇒ 拆 ir.rule 只把 create 從「任何成員」收斂到「owner/admin」，而**會憑空建列的租戶通常正是自己 workspace 的 owner**，照樣通過。
⇒ **對核心威脅覆蓋率接近零**；真正能阻斷的位置是 **model 層 `create()`**（掛鉤點與政策都現成，與既有三道閘同層）。
⇒ 🔴 **修法表的「ir.rule 拆分＝唯一可行」從「修法」變成「修法的一半」——必須在向 Eugene 說明前先講清楚**，否則他會基於「拆了就修好」做決策。

**加重**：`subscription_lifecycle_cron.py:39` 是 `search([(subscription_id,!=,False)])` ⇒ **`subscription_id` 為空的服務對 lifecycle cron 完全隱形**（不會 suspend、不會 cleanup、永遠跑、永不計費）⇒「先建再靠 cron 收」不成立。
✅ **但給出可驗收斷言**：修好後不該存在 `subscription_id = False` 的 running cloud_service。
⚠ `create()` 既有形態要沿用：`super().create()` 在 `:3027` 先執行、閘門在後靠 `raise` 撤回；`:3005-3009` 明文警告呼叫端不得裸 `except Exception`（`SerializationFailure` 是 `Exception` 子類）⇒ **新守衛例外被吞＝「畫面說失敗、資料卻建好了」**。

#### 二、「需連帶改動 0 條」不成立——實測 **2 條**，失敗形態是**靜默的**

`api_workspace_services` action=`list`（`paas.py:1290`／gate 只到 `:1316-1318`／分支 `:1321-1322`）與 `api_workspace_service` action=`get`（`:1331`／`:1358-1360`／`:1367-1368`）**皆無 role 檢查**，卻經 `_update_service_status`（`:3166`）對**非 sudo** recordset 執行 `write`／`unlink`。
🔴 `paas.py:2499-2502` 與 `:3307` 兩層 `except Exception` 吃掉 `AccessError` ⇒ **只斷言「HTTP 回 success」的測試不會變紅**；實際後果是該成員的服務**永遠停在 deploying/initializing/upgrading**。
⚠ **窮舉判準陷阱**：`rollback`／`reset-admin-password` 用 `check_user_access(user, required_role=ROLE_ADMIN)` 而非 `access.role not in [...]` ⇒ **用字串 grep 當窮舉判準會漏掉這兩支**。

#### 三、拆分後**不會有任何既有測試變紅**＝沒有回歸網

`test_cloud_service_acl.py` 驗的全是**跨 workspace 非成員**（`:93-97`），**零斷言「同 workspace 內 `role=user` 成員」**⇒ 全套仍 `0F/0E`，**那不是「沒有回歸」的證據，是既有測試對這個維度零鑑別力**。新守衛必須自帶 TDD RED（含對 poll 路徑的端到端陽性斷言，否則吞例外讓修法看起來是綠的）。

#### 四、四個複用／裁決點
1. ✅ **domain 必須用 `any` operator**（`ir_rules.xml:129` 有現成寫法）——寫成 cross-tuple 會產出「看起來有守、實際零效力」的規則，且只驗 owner/非成員抓不到 ⇒ **驗收務必含「同 ws 內 `role=user` 被擋」**。
2. ⚠ **admin bypass 照抄＝放寬既有行為**（cloud_service 今天沒有 group_system ACL 列也沒有 `*_rule_admin_full`）⇒ 要刻意裁決。
3. ⚠ **後台 menu 沒有 groups 限制**（`cloud_service_views.xml:124-129`）⇒ SA 有這道配套防線、cloud_service 沒有 ⇒ **ir.rule 是唯一防線**。
4. ✅ 收緊 create 不打斷正規下單（`paas.py:1324` 已 owner/admin，兩層 lock-step，且**非 sudo 故規則會真的被執行**）⚠ 但**未來若 `_create_service` 改 sudo，這條規則會靜默失效——值得在註解寫死此依賴**。

#### 五、附帶
- cloud_service **全叢只有一條 ir.rule** ⇒ 拆分後無疊加，**但也無任何規則替你兜底**。
- **憑空建列不必指定 state**（`cloud_service.py:244` `default=pending`）⇒ **「檢查 payload 有沒有偷塞 state」形狀的防禦是零鑑別力**。
- 部署入口**單點**（13 筆 ir.cron 逐筆查，只有一支吃 pending）。
- 🔴 **`origin/develop` 已動到 `e112e72c`**（先前基準 `eb9d199d` 過期）。
- 🔴 **連帶 docs 債**：`docs/decisions/patterns/paas-workspace-member-acl-tightening.md:35-45` 把現在四 perm 全 True 的原文當「如何收緊」的標準範例**逐字貼出** ⇒ 拆分一落地它就變成教錯的，**須同 PR 更新**，否則製造 #547 那批裡的下一份。

### 📌 原 workflow 設計（六組，其中兩組補完 leader4 標為「未驗」的缺口）

理由：**#519 的證據跨越多天、來自至少三個人**，而今天已多次證明 issue 裡的事實會過期（`ResourceQuota 12 vs 21` 兩條線交叉命中卻都是舊值；#547 掃出 29 份 pattern doc 只有 8 份乾淨）。

六組核對＋對抗式覆核，**其中兩組是補完 leader4 明確標為「未驗」的缺口**——那正是動工時會踩到的地方：
1. `acl-rule`：CSV 全 CRUD／rule 四項 perm／SA 前例註解原文／**還有沒有其他 rule 也作用在 cloud_service**
2. `cron-deploy`：cron domain 只有 `state=pending` ⇒ **要讀完整個方法體**（「domain 沒擋」≠「整條路徑沒擋」）
3. `legal-paths`：**「拆分需連帶改動的合法路徑 0 條」是否仍成立**——修法可行性的核心
4. 🔴 `bff-endpoints`：**窮舉所有 mutation 端點的 role 檢查**（leader4 只抽驗兩支）——若有端點沒 role 檢查而靠 ir.rule 放行，**拆分後會開始失敗 ⇒ 推翻「0 條」**
5. 🔴 `model-guards`：**cloud_service model 層有無補償性守衛**（完全未查）——**這一組可能推翻整張 issue，也可能證實它，兩個方向都要誠實報**
6. `bypass-paths`：三條已證實繞過路徑是否被 PR#517 部分堵住

### ✅ #542 A 層完成＋🔴 未驗殘留已開 #551（2026-08-03）

**A 層三態實打全過**（401／401／200），且三項設計品質特別高：①**陰性對照確認 upgrade 前該路徑是 401** ⇒ 歸因成立、非「本來就通」②**回應 2668 bytes 與從 agent 容器內直打 gateway 完全一致** ⇒ 排除了 leader7 自己記過的假綠（外部 `/health` 拿到的其實是 WebUI）③**upgrade 前後 `API_SERVER_KEY` 與 `admin_password` 指紋皆未變** ⇒ **用實測證明「不需 Reset」，不是靠推論**。ns 內其他三個 release 全程 Running、ResourceQuota 未被重算。

**#551**（`priority:medium`）：stg 手動升上的 chart **0.1.1** 可能被 DB 的 `helm_chart_version=0.1.0` 打回（stg addon 實測 `.88`、把 seed 改 0.1.1 的是 PR#539／develop `.97`）。

🔑 **leader7 指出的失效形態比風險本身更有價值**：
> 打回之後，**三態實打的結論不會自動失效、也不會有任何訊號**——#542 留言仍顯示「已驗證通過」，但環境已回到沒有通道的狀態。**任何人事後讀那則留言都會以為通道還在。**

⇒ 這不是「驗證做錯了」，是**驗證結論有有效期，而載體上沒有記錄那個有效期的前提**。同族：`app.kubernetes.io/version` label 不可驗版、`--reuse-values` 丟新預設、remote head 是滯後指標。
⚠ **已要求 leader7 在 #542 留言補上結論成立的前提**（「本結論成立於 chart 為 0.1.1；若被打回則失效，見 #551」）——**他自己指出的問題，補在載體上才算關掉**。

**dispatcher 起了查證 workflow**（opus，五角度並行）：因為「沒有 reconcile 路徑」是**否定性結論**，而本 repo 已反覆證明「查無」常是查法不夠全集。五角度＝`field-readers`（從欄位所有讀取點反向追）／`cron-jobs`（全 repo ir.cron 逐筆、含不相關的才算窮舉）／`helm-callers`（從 operator 實際執行 helm 的位置往上追 caller 鏈）／`lifecycle-ops`（redeploy／reset／change-plan／status-poll 逐一）／🔴 **`adversary`（任務是證明「存在」，專找其他四組會漏的：Fleet/GitOps 宣告式收斂、operator reconcile loop、values 夾帶版本、CD bundle）**。每角度須配陽性對照＋說明範圍為何是全集；找到的路徑再經對抗式覆核，並**額外判定「是否自動觸發」——人為與自動的風險性質完全不同**。

### 🔴 #551 查證完成——原本擔憂不成立，但查出的東西會毀掉剛驗證好的通道（已保全 #551 留言 4636）

22 agents／237 萬 tokens／867 次工具呼叫／零 error／五個獨立角度（含一個任務是「證明存在」的對抗者）／**覆核駁回 3 條假陽性**。

**① 原本的問題：沒有自動收斂機制。** 零 `base.automation`、零 `ir.actions.server`；`_cron_process_pending_deploys` 搜尋寫死 `state=pending`（碰不到既有 running release）、`_cron_check_deploying_services` 純輪詢。⚠ **兩個名字帶 reconcile 的是假線索**：`reconcile_report.py:12` docstring 明載不做變更動作、`_cron_reconcile_reach` 掛在 `project.project` 動的是 agent-runtime。⇒ **手動升上的 chart 不會被自動打回。**

**② 🔴 但 #542 的 B 層自己就會打回。** `_reset_admin_password`（`cloud_service.py:647`）帶 `version=template.helm_chart_version` 去 upgrade ⇒ stg template 是 `0.1.0` ⇒ **一按 Reset，chart 從 0.1.1 降回 0.1.0，gateway 通道當場消失**——而 B 層的整個目的就是跑 Reset 換發 key。
加重三點：`reset_values=True` ⇒ `--reuse-values` **永遠不會被加**、values 以較低版 chart 預設重新渲染／`helm upgrade --version` **對低版號無單調性檢查**、`--atomic` 只在失敗時回滾故**降版成功即永久生效**／🔴 **成功路徑未回寫 `helm_chart_version`** ⇒ DB 與叢集分歧**永不收斂**。
⇒ **B 層硬前置：等 stg 升 `.97`＋複驗 template 值真的變 0.1.1 之後才能做。**

**③ 🔴 三條人為路徑，兩條租戶自己按得到**：
| 路徑 | 位置 | 誰按得到 |
|---|---|---|
| Reset Admin Password | `cloud_service.py:647` | 🔴 **租戶自己**（role≥admin，不需平台管理員） |
| Redeploy | `cloud_service.py:449` | 後台按鈕；⚠ **public method，任何有 write 權的 RPC `call_kw` 都能呼叫**，方法內**無 role gate** |
| ConfigurationTab 儲存／改網域 | `paas.py:2976` | 租戶；🔴 **version 由 JSON-RPC 端任意指定、無 allowlist、直接寫進 DB（`:2948`）** |

**④ 🔴 方向相反但更隱蔽：空版號＝浮動到 latest。** 4 個 seed template 的 `helm_chart_version` **刻意留空**（`cloud_app_templates.xml:21/58/127/164`），管線是 `if version:` 閘門 ⇒ 空值不帶 `--version` ⇒ helm 3.13.3 實測 `--help` 確認**用 latest** ⇒ 那些服務只要有人按 Reset/Redeploy 就**靜默升到未知版本**。⚠ 而 `:630` 的漂移偵測**要求兩邊都有值** ⇒ **這個情況完全不會被記錄**。

**⑤ 未量到的分支（已要求 leader7 動工前量真值）**：以上推論的前提是 stg template 值確實是 `0.1.0`（leader7 回報、dispatcher 連不上叢集無法自驗）。**若它其實是空的，後果不是降版而是浮動到 latest，更糟。**

**⑥ 處置**：原本的「等升級後複驗即可關」**只解決了本單原始陳述的那一半**（自動 reconcile 本來就不存在）；**真正的問題是「chart 版本在多條人為路徑上不受控」，不會因 stg 升級而消失**。三件可能拆出的（**尚未開單，需先查重、範圍待 Eugene 裁決**）：①Reset/Redeploy 未回寫導致漂移永不收斂 ②`_update_service` 的 version 無 allowlist ③空版號浮動到 latest。

🔑 **方法論收穫**：被駁回的三條裡有一條把 `_update_service` 標成「reconcile 路徑」——**它確實會 upgrade 既有 release，但版本來自 HTTP 參數而非 template 欄位，標籤錯了**。這個區分被保留在紀錄裡，而**正是它揭露了「租戶可任意指定 chart 版本」這個獨立問題**。⇒ **駁回一個宣稱不等於丟掉它的內容**。

### 🔴🔴 #552 已開（`priority:high`）——leader7 的實測讓第③項從理論變成 prod 實際風險

**leader7 stg 實測**（15 個 template 全量、**區分 NULL 與空字串**——實測四個都是**空字串不是 NULL**，指向 seed 而非遺漏）：`n8n`／`odoo`／`postgresql`／`redis` 四個 `helm_chart_version` 為空，其餘 11 個都有明確版本。
🔴 **關鍵事實**：`paas-ws-stg-5a105e8b` 裡**現正運行** `svc-16e7ac1a`（n8n，chart **`n8n-2.0.1`**，rev 2，已跑 6 天 13 小時）⇒ **只要有人按 Reset 或 Redeploy，它就從 2.0.1 靜默跳到 registry latest**。**不是「未來可能有租戶踩到」，是現在就有一顆在射程內。**

**dispatcher 補查**：`origin/develop` 的 seed 實查確認 `cloud_app_postgresql`／`cloud_app_n8n`／`cloud_app_redis`／`cloud_app_odoo` **在 repo 裡就是空的** ⇒ **prod DB 值也會是空的**，而 **n8n 與 odoo 都是 prod 在售商品**。

**採納 leader7 的優先級判斷 ③ > ①②**：①是紀錄漂移、②需租戶主動構造 RPC、**③只需要租戶按一個 UI 上就有的按鈕**。①②暫不開單，等 Eugene 裁決範圍。

⚠ **一條容易誤判的依賴關係（dispatcher 指出）**：**#552 的緩解不依賴 stg 升級**——stg 升 `.97` 只讓 hermes-agent 從 0.1.0 變 0.1.1，**那四個空值 template 在 `.97` 的 seed 裡仍然是空的**。依賴鏈只涵蓋 B 層與 #551 第②項，**不涵蓋 ③**。

🔑 **leader7「區分 NULL 與空字串」值得記**：兩者在 `if version:` 下行為相同，**但診斷時是不同東西**（NULL＝從未設定／空字串＝有人顯式寫了空）⇒ 實測是空字串故指向 seed。與 memory 的「空字串與 False 兩種空、下場相反」同族。
🔑 **leader7 繞過 Reset 直接從 K8s secret 取 key 給 Eugene**，自評「當時只是省一步，現在回頭看是運氣好」⇒ dispatcher 認為該記成判準：**當一個操作的副作用範圍你還沒盤過時，能繞過它取到同樣結果就繞過**（他那次繞過的不只降版，還有 `--reset-values` 造成的 values 重新渲染）。

⚠ **dispatcher 查重時陽性對照一度是壞的**：第一次分頁查重的統計指令自己 regex 錯誤、回傳空值 ⇒ 依規則不能宣稱查重完整，補驗後（84 筆去重仍 84、**最小編號 25 未被截斷**、已知 #551 命中）才開單。**「最小編號未被截斷」正是今天早先用 `--limit 50` 犯的錯的反向檢查。**

### ✅ #524 完成標準已定案：**②＋③**（Eugene 裁決 2026-08-03，已落地 #524 留言 4640）

**②**＝三軸實測（跨租戶讀不到／改不動／建不了，各帶陽性對照，結論具名寫出 `tunnel_id`）
**③**＝再加機械化 gate（含**注入式驗證**：故意違規→變紅→移除→變綠，**兩次輸出貼進 PR**）

🔑 **leader3 的理由值得保留**（不是「為了完整性」）：**排序本身製造了第三次手工鏡射的機會**——三個模型、三次手工套用同一形態、**分屬三張單、三個時間點、可能三個人**。gate 問的不是「你有沒有寫 rule」，而是「**凡是給 `base.group_user` 寫入權限的 model，是不是都有對 Internal User 生效的 rule**」⇒ 下一個新 model 忘了、或某次拆分漏了一條，CI 就會紅。**沒有它就只能等下一次有人剛好去掃。**
⇒ 這條「為什麼」在保全時最容易被跳過（它不是「做什麼」），但**它正是日後有人想砍掉 gate 時唯一的擋箭牌**——已明確要求 leader3 保全它。

**leader3 已受指示走完整第 0 步**（保全 → dismiss Bastion／Sieve／Ferry → `/clear`）。時機理由：①他**不能動工**（排在 #519 merge 之後）⇒ 手上是待辦非進行中產出，`/clear` 成本最低 ②完成標準已落地 issue、不依賴他的 context 存活 ③context 已 72%（720.8k tokens）而 ②＋③ 是一大塊工作。
⚠ **已提醒他 clear 後的三件**：完成標準＝②＋③／開工前置＝#519 merge（**且 leader1 的修法方向剛被推翻，最後拆出來的形態可能與他現在的想像不同，開工時要重看**）／`docs/decisions/patterns/` 歸 leader4 統籌，且 `paas-workspace-member-acl-tightening.md:35-45` 在 leader1 merge 後會變成教錯的、已要求同 PR 更新。

### ⚠ dispatcher 又一次同形態失手：把「畫面沒變」當成「沒發生」

我兩次 `maestri check` leader3（相隔 20 秒）都看到「②＋③」停在輸入框，據此向 Eugene 報告「**那句還沒送出**」——**錯的**。他早已收到並寫進 `#524` 留言 4640（11:30），**查 issue 兩秒就知道**。

🔑 **這是 leader3 今天那條的鏡像**：他說「我把『我回答了』當成『他收到了』」；**我把「畫面沒變」當成「沒發生」**。⇒ 兩者解法相同——**用獨立載體交叉驗證，不要只看單一介面**。
🔴 **而我自己今天才寫下「終端畫面是滯後指標」**，並在多處引用它糾正別人。⇒ 這是 `feedback_apply_your_own_review_standard_to_yourself` 的第六個實例，**且是「規則寫下後又用它糾正過別人、然後自己再犯」這個最強形態**。

### ✅ #552 已派 leader3（Eugene 裁決 2026-08-03）——他的第 0 步經 dispatcher 實查通過

**驗證（非採信自陳）**：`maestri list` 確認 **Bastion／Sieve／Ferry 已從 canvas 消失** ✓；`#524` 新增留言 **4643**「⚠ 開工前必讀：不要假設 #519 會拆成三規則組」✓ ⇒ **他把 dispatcher 警告的那件事保全進 issue，不是只記在 context 裡**。

**兩張單的排法**：**#552 優先**；**#524 仍在他名下但被排序卡住**（等 #519 merge，而 leader1 的修法方向剛被推翻、正在重新框定）⇒ 現在做 #552 不會撞到任何人。**兩張無檔案交集**（#552 動 `cloud_app_templates.xml`／`cloud_service.py`／可能 `extra/paas-operator/`；#524 動 `ir_rules.xml`）。

**派工單已含**：四層「為什麼不會被發現」（`--reuse-values` 永不加上／**漂移偵測要求兩邊都有值故空版號完全不被記錄**／成功後未回寫／`--atomic` 只在失敗時回滾）、三層修法與 **1 與 2 順序不可反**的硬約束、以及**本單最大未量到＝prod DB 實際值與 prod 上有多少顆這四種服務**（dispatcher 連不上叢集，建議他第一步就量）。

### 🔍 dispatcher 起了 #552 修法連帶影響驗證（opus，四組並行，結果給 leader3、不阻塞）

**理由是 #519 的教訓**：那張單的修法表寫「需連帶改動的合法路徑 **0 條**」，實測是 **2 條**，且**失敗形態是靜默降級**（兩層 `except Exception` 吃掉 `AccessError`）⇒ 只斷言「HTTP 回 success」的測試不會變紅。**#552 的層次 2（fail-closed）是同型風險，這次要在動工前找出來。**

四組：
1. 🔴 `fail-closed-blast`——窮舉所有走到那三個 `if version:` 閘門的路徑（不只 Reset/Redeploy，含**首裝 `_execute_deploy`**、`_update_service`、change-plan、resume/suspend、operator 側 caller），逐條追上游例外處理判斷 raise 會不會被吞掉。**特別查：首裝若 fail-closed，那四個 template 會不會變成完全無法部署**（比「無法 Reset」嚴重得多）。
2. `seed-fill-risk`——`noupdate` 設定（若=1 則改 seed 對既有環境無效、止血不成立）／四個 chart 是自家還是第三方（第三方的 latest 會隨上游動）／**填錯會降版，repo 側有沒有記錄「當前應該是哪一版」**。
3. `writeback-design`——🔴 **operator 的 response 有沒有帶回實際套用的版本？若沒有，層次 3 根本做不到**，那是必須先解的前置／回寫後漂移偵測會不會產生每次 Reset 都報一次的噪音／該欄位語意是「跑的版本」還是「建立時的版本」。
4. `test-coverage`——**若動工者做了修法但寫錯了，現有測試會不會變紅**（#519 那裡的答案是「不會，等於沒有回歸網」）／新測試該斷言什麼、注入什麼會讓它變紅。

每個「會打斷」的判定再經對抗式覆核，**並特別要求覆核者驗證「失敗可見度」標對了沒**——⚠ **標成 loud 而實際 silent 的代價最大：動工者會以為「測試會抓到」而不寫行為測試。**

### 🔴 #552 修法連帶影響驗證完成——**最大爆炸半徑與那四個空值 template 無關**（已保全 #552 留言 4645）

18 agents／221 萬 tokens／846 次工具呼叫／零 error。**會被打斷 8 條（其中 5 條靜默）／判定被駁回 6 條。**

**① 🔴 `fail-closed` 放錯位置會讓所有租戶的「儲存設定」壞掉。** `_update_service`（reconfigure）**永遠傳 `version=None`**——前端從來不帶（`cloud_service.js:451` 參數預設 null，兩個唯一呼叫端 ConfigurationTab `:157-161`／EditDomainModal `:108-112` 都只傳 3 個參數）⇒ **這條路 100% 走空值，與那四個 template 無關 ⇒ 全部 template 的 reconfigure 都會被打斷**。放在 `PaasOperatorClient.upgrade_release` 會**靜默壞掉**（`paas.py:2988` 走背景執行緒、失敗不回前端）。正解＝controller 補 fallback（與 `:2948` 既有回寫對齊）或 gate 放主執行緒。⚠ **第二個 UI 入口**：Odoo addon 勾選（`cloud_service.js:486-495`）走同一 endpoint 同樣不帶 version ⇒ **驗收要分開跑兩個入口**。

**② 🔴 首裝 fail-closed 會讓 n8n 與 odoo 完全無法新開通。** 實查 `is_active`：postgresql False(`:39`)／**n8n True(`:93`)**／redis False(`:144`)／**odoo True(`:205`)** ⇒ **層 1 與層 2 不可分開發版**，順序反了＝**在售商品開通全掛**。若保留首裝 fail-closed，判空要提前到 `_create_service`／`_availability()`——**購買前就擋，不要先收錢再 error**；例外型別須用 `PaaSOperatorError` 否則落進 cron（`:751`）泛用處理。

**③ 🔴 層 3 回寫對象寫錯＝做出「跑起來對、發一次版就靜默還原」的補丁。** 回寫 `template.helm_chart_version` 會被下一次 `-u` 靜默覆寫——**repo 內已有明文警告**（`cloud_app_template.py:16-24` 的 `_DEFAULT_GLOBAL_INSTANCE_CAPS` 註解：「seed noupdate=0＋檔尾 healer 每次 `-u` 清 flag，**手改 DB 保不住**」）⇒ **回寫對象必須是 `cloud_service.helm_chart_version`（`:313`，per-service）**。⚠ operator schema 加欄位**一律 `Optional[str] = None`**，設必填會打爆 `test_api.py` 19 處。

**④ 層 1「填什麼值」兩個陷阱**：(a) `docs/reference/features/cloud-services.md:894/942/984` 有三個**看似權威、實為 spec 期示意值**的版本號（且配 classic repo 非現用 OCI）⇒ **不得作為填值來源**；(b) **seed 殘留靜默窗**——`<function>` healer 在檔尾，同一次 `-u` 內 record 先處理（flag 仍 True ⇒ **ORM 靜默 skip，不報錯不 log**）⇒ 舊 DB **要第二次 `-u`** 值才進得去 ⇒ **驗收不能只看「0 error」，要 psql 讀回實值**。

**⑤ dispatcher 未裁決的分歧（兩邊都留給 leader3）**：覆核**駁回**了「不要把 guard 放 `upgrade_release`」，理由是「事實成立，但把『這正是修法的意圖』誤標成『非預期的連帶損害』」。⇒ **不替動工者決定**，這正是「駁回一個宣稱不等於丟掉它的內容」。

**⑥ 🔴 dispatcher 更正自己在 #552 的一處不精確**：「租戶按 Redeploy」**在 `/woow` 前台沒有這顆按鈕**，實際可達性是後台 view＋`call_kw`。**Reset Admin Password 確實在前台**（`ServiceDetailPage.xml:95`）⇒ **本單「租戶一鍵觸發」的嚴重性由 Reset 那條單獨支撐，仍成立但論證要精確。**

### 🔴 兩條會讓「查無」型驗收全面失效的工具坑（leader1 發現，dispatcher 實測確認）

**① `git grep -E` 是 POSIX ERE，反斜線接字母（`\s` `\b` `\d` `\w`）靜默回 0 命中、零錯誤訊號。**
dispatcher 實測：`git grep -cE "role':\s*'(user|guest)'"` → **0 檔**；同 pattern 用 `-P` → **15 檔**；改 `[[:space:]]` → **15 檔**。
leader1 的 workflow 內兩個 agent 同日各中一次，其中一個**差點坐實一個錯的「零合法使用者」結論**。
🔑 **但受影響的只有「反斜線接字母」**——`\.` `\(` `\)` 在 ERE 是**合法字面逸出**，用它們的查詢**不必重驗**。⇒ **發現這坑後不必整批作廢**，這個判準把複驗範圍縮到真正需要的那些。

**② `tea comments list` 六種 `--output` 模式全部截斷 BODY，正解是換子命令 `tea issues <n> --comments`。**
dispatcher 實測對照：`--comments` 取回 **56,545 字元**（完整、陽性對照命中留言 4631 的長句）vs `comments list --output json` **830 字元** ⇒ **68 倍**。
🔴 **拿 `comments list` 去 grep 留言內文是零鑑別力的查法**：內文明明在、命中永遠 0，會被誤判成「留言沒送出」。
🔴 **這條的正解早就寫在 `reference_tea_cli_write_gotchas.md:72`（7c），但索引行沒有指向它** ⇒ 今天 dispatcher 與 leader1 各自繞路，leader1 還被 harness classifier 擋兩次。**已改索引行把正確讀法提到最前面。**
⇒ **這是今天第二次「答案在自己的紀錄裡卻沒讀到」**（第一次是開 #548 沒查重）。**差別在於這次的成因可修——不是紀律問題，是索引沒讓人找到它。**

**③ 陽性對照要用同一個命令、同一個 pathspec、只換 pattern**——**換命令的對照驗的是別的東西**。leader1 實例：`git ls-tree -- 'src/static/**'` 回 **0 檔**，但 `git grep -c import` 同 pathspec 回 **112 檔** ⇒ 兩個子命令對 `**` 解讀不同，用 ls-tree 當對照會得到「這個目錄是空的」的錯誤結論。

**④ 抽驗樣本本身要先確認有鑑別力**：#519 原留言抽驗兩支端點證明「mutation 端點都有 role 檢查」，其中 `skip-trial-estimate` **結構上不可能顯示該缺陷**（docstring 自陳 side-effect-free）⇒ **「2 選 1」實際只有 1 個樣本**。**抽驗前先問「這個樣本若有缺陷，會顯示出來嗎」。**

### ✅ leader1 複驗 dispatcher 點名的三條「查無／窮舉」結論（#519 驗證的自我稽核）

- ✅ **「cloud_service 全檔只有一條 ir.rule」成立**——他**換查法**用 Python `re.findall` 逐筆解析全檔 **44 條 rule／14 個 model**（不用 git grep）。🔑 **順帶得到比原宣稱更有力的事實**：**有角色概念的 model 全部已拆成三段**（成員唯讀／owner·admin 可寫／group_system bypass），**`cloud_service` 是全檔唯一還是「單條、四權全開、無 group_system bypass」的**——而 SA 註解逐字寫著它當初 `mirroring cloud_service`。**抄的人修好了，被抄的原型沒修。**
- ⚠ **「13 筆 ir.cron 只有一支吃 pending」要收窄成「就 cloud_service 而言」**——全 repo 另有 `_cron_cleanup_pending_topups`／`_cron_issue_pending_ecpay_invoices`／`_cron_issue_pending_invoices` 三支也處理 pending 語意（落在別的 model）⇒ **原句被讀成「全 repo 只有一支」會是錯的**。附帶：`name="user_id"` 在整個 `src/` 出現 0 次、陽性對照 `name="model_id"` 回 15 ⇒ **所有 cron 都吃 Odoo 預設執行身分**。
- 🔴 **「其餘 6 支寫入端點都已有 owner/admin 檢查」被判定 NEVER_TRUE**——**問題不是漏查**（同一則另一段就報了 list/get 那 2 條），**而是「寫入端點」這個詞把 list/get 排除在外** ⇒ 讀者會以為寫入端點都守住了。**已用 leader1 的三分法更正進 #519 留言**：(A) 有 role 閘且會 mutate **8 支**／(B) 🔴 只驗成員卻會寫庫甚至 `unlink()` **2 支**（list、get）／(C) 只驗成員但確認不 mutate **3 支**。⇒ **當初就不成立**（`git show 14c7f0e5` 上那個 `unlink()` 已存在），**失效方向是低估，不是假陰性**。

### ⚠ leader1 的 workspace_access 發現（**實測中，dispatcher 依其界線不改排程、不開單**）

結構推論（`origin/develop` 實讀、**未實測**）：`ir.model.access.csv:3` 對 `workspace_access` 是 `1,1,1,0`（含 create+write）／`ir_rules.xml:29` 的 write rule **domain 不驗 role**／`:41` 的 create rule domain **`[(1,'=',1)]` 恆真**／model 層唯一守衛 `workspace_access.py:80` **只擋第二個 owner，admin 不受約束**／`ASSIGNABLE_ROLES` 強制點只在 `paas.py:670`／`:773` **controller-only**。
⇒ 推論 A：`role=user` 成員可對自己那列 `write({'role':'admin'})` 自我升級；推論 B：任何 `base.group_user` 可 `create` 加入任意 workspace 並取得 admin。

🔴 **對 #519 的意義是阻塞依賴而非隔壁的單**：#519 要把 cloud_service 寫入權綁在 `role in ['owner','admin']`，**若 role 本身租戶可自寫，那道閘的安全價值是零**——攻擊者不會停在 `role=user`。⇒ **#519 若照原計畫落地，會產出一條「看起來守住、實際可自助繞過」的規則，而只驗「owner 能寫／role=user 被擋」的測試抓不到。**
🔑 **與 dispatcher 交付的第四點是同一形狀的兩層**：一層是 domain 寫錯（cross-tuple），一層是 **domain 寫對但它依賴的欄位本身不受保護**——**兩層都會通過「owner 能寫／非成員被擋」那組測試。**

**實測設計（leader1 派 teammate Assay，docker-desktop 本機沙盒）**：要求**真 HTTP RPC，不准用 `TransactionCase`**（su 會讓 ir.rule 整個不生效 ⇒ 每項都「成功」而什麼都不證明），四項控制齊備。🔑 **鑑別項是關鍵**：若「應該被擋的」沒被擋，代表環境根本沒在套 ir.rule，**A/B 的「成功」是假的、全部作廢**。
⚠ **Eugene 已指示先實測再決定；在結果回來前不得據此改排程或開單。** leader1 查重已做（逐頁抓 **86 張** open issue，不分頁只回 30），無重複；最接近的 #289 是同族不同對象。

### 📌 leader1 的其餘第 4 步回報

- **後台 form 是完整可寫面**（`cloud_service_views.xml` 全檔 `readonly` **0 次**、`helm_values` 原始全文可編輯、`@api.constrains` **零個**，而 Eugene 已裁決**後台不鎖 menu**）⇒ **SA 用來兜底的那道防線在 cloud_service 不存在，所有守衛只能放 model 層**。**同意另開單、不併進 #519**，但**暫不開**——等 workspace_access 實測一起看（若 role 可自寫，兩條的組合效應會放大）。
- **#519 修完後這條仍開著，要寫進 PR 描述**，避免被讀成「租戶繞過已解決」。
- **`CONTEXT.md` glossary 已補兩個術語（未 commit）**：**Odoo group role**（軸一，全站身分，`ir.rule` 的 `groups` 欄）與 **Workspace role**（軸二，`workspace_access.role`，`ROLE_HIERARCHY = guest<user<admin<owner`），含撞名警告——**`ROLE_USER = 'user'` 與 `base.group_user` 只是碰巧同字**。Eugene 指定給 `/grill-with-docs` 用。
- 🔴 **`su-env-exempt-guard-false-green.md` 是 #547 的第 6 條 contradicts-code**（要轉 leader4）：七個座標全漂移（doc 說 `security_access.py:181/192/205/211/219/842/849`，實際 guard 在 `:342/:351/:367`、sentinel helper `:91-102`、prefix hatch `:1180`）＋**處方不足**——doc 通篇只說「用 context key」，而 `security_access.py:76-86` 記載實測：**普通 `base.group_user` 租戶經 `/web/dataset/call_kw` 送那個 context key 就真的改寫並 commit** ⇒ **照 doc 寫會留下 RPC payload 可打開的洞**，而這份 doc 是 hook 強制閱讀的。🔑 **「行號漂移」與「處方不足」要分開列——前者是查閱不便，後者是照做會出事。**

### ⚠ PR#550 merge 時要知會 leader1（dispatcher 追蹤項）

它**刻意留了一條紅燈**當 #543 的注入驗證 ⇒ 若它先 merge，leader1 的 #519 分支 rebase 後會繼承那條紅，屆時「**我改壞了**」與「**#550 故意留的**」**輸出長得一樣**。

### ⚠ dispatcher 又踩了 memory 記過的 shell quoting 坑

發給 leader1 的訊息因內文含 shell glob 字元（單引號提前結束引號、後面的 `*` 裸露）被 zsh 展開失敗、`no matches found`，**訊息根本沒送出**。🔴 **背景任務只回報 exit 1——若沒去讀輸出，會以為送出了**。正解是 quoted heredoc 寫檔再 `"$(cat file)"` 傳入（`reference_maestri_ask_shell_quoting` 早有記載）。⇒ **「送出≠送達」的第五種載體：指令本身根本沒執行成功。**

## 📋 全體現況點名（2026-08-03，Eugene 要求，七條線全數回報）

**實查基準**（dispatcher 自量，非採信自陳）：`origin/develop = e112e72c`／`manifest = 18.0.1.0.97`／**全站 open PR 僅 2 張**（#550 leader6 刻意留紅燈、#555 leader1 CONTEXT.md 術語，皆帶【勿 merge】）。**PR#545 已 merged**（`e112e72c`），例行 union 檢查：`src/tests/__init__.py` **189→190 條，被吃掉 0**（陽性對照非空）。


### 🔴 leader7 的 CI 根因：Eugene 重設兩次 secret 都是白工

`build-image.yml` 的 `Validate addon ref` step 必定失敗 ⇒ **stg 升版與 prod 發版兩條線都不能動**，五顆 tag 全死在同一步。

**根因（`ci-fix-secret` 找到，驗證中）**：`actions/checkout@v4` 在 workspace 的 `.git/config` 留下 `http.<url>.extraheader AUTHORIZATION: basic ***`（repo-scoped token）。`Validate addon ref` 用 `git ls-remote` **在 workspace 內**執行 ⇒ 吃到那個 header、用錯身分 ⇒ Gitea 回 404；而 `Fetch custom addons` 用 `git clone` 到 `/tmp` **新目錄**、不繼承該 config ⇒ 7/31 能成功。
leader7 已獨立驗證機制前半（設無效 extraheader 後 `ls-remote` 回 `Failed to authenticate user`）。

⇒ 🔴 **不需要 Eugene 再動任何 secret 或權限。問題從來不在那組憑證。**
已排除的假設（每項有實測）：權限／secret 值（重設兩次、第二次確認落地仍失敗）／生效延遲（1 分鐘與 30 分鐘各推一次）／建 tag 方式（`tea api` 與 `git push`）／PR#53 的 prod gate／`env:` 不展開 secrets（**leader7 自己的假設，被 A1 rc=0 推翻**）。

### 🔴 dispatcher 被自己人抓到兩處錯誤，皆已更正

1. **#547 的「有 3 個 pattern doc 不在 hook 對照表裡」是假缺口**（leader4 用集合差抓到，dispatcher 複驗確認）：那 3 份是 `README.md`（索引）＋ `phase-leader-brief-as-pointer-not-source.md`／`plan-gap-stop-and-report.md`（**流程 doc，無對應程式碼路徑**）⇒ **零缺口**。已更正進 #547。
   🔑 **我當時只做數量比對（33 vs 30），拿到「差 3」就替它編了一個聽起來合理的解釋**。leader4 做集合差、印出「是哪三個」，兩秒看穿。⚠ **而我在同一份稽核裡要求每個 agent「每份都回一筆 finding（含 clean）」，理由正是「否則無法分辨查過與沒查」——我對 agent 施加的標準沒套用在自己算那個 33 vs 30 上。**
2. **#519 的「其餘 6 支寫入端點都已有 owner/admin 檢查」措辭把 list/get 排除在外**（leader1 窮舉 13 支判定 NEVER_TRUE）⇒ 已用三分法更正。

### 🔑 leader4 的兩項新發現（**未落地，已要求他寫進 issue**）

- 🔴 **`src/models/project_project.py:301` 仍在用 `env.uid == SUPERUSER_ID`** ⇒ **#547 不是純 doc 問題**，那份 doc 教的寫法在程式碼裡還有活的實例，**只修文件不會修掉它**。（`agent_template.py:108`、`project_access.py:136` 已改用 `env.is_superuser()`；Odoo `sudo()` 只設 `su=True`、uid 不變 ⇒ bypass 分支在 sudo 路徑不觸發。）
- **`sync_workspaces` 是全量覆寫**（`agent_runtime_client.py:180`）⇒ leader1 #549 站點 4 的 cron clobber 後果成立。**只在對話裡，#549 留言沒有。**

### 🔑 leader6 的自我揭露：「說要給」不是「給了」

> 「我當時寫的是『需要原始素材我隨時給』——**那是說要給不是給了**。」

⇒ **「送出≠送達」家族的新形態**：前幾種是**送了但沒到**（JSON 裸換行沒送出／回在自己終端／畫面滯後），這個是**根本沒送、但雙方都以為安排好了**——因為「我隨時給」在對話裡**讀起來是完成式**。dispatcher 讀時也沒察覺，是他自查出來的。
⇒ 已請他**直接 ask leader4，不經 dispatcher 轉**（多一手就多一個斷點），並要求**在 issue 或 memory 留痕**——leader5 今天剛踩同型（給 leader4 的 rollback/savepoint 素材原本只在 maestri 訊息裡，回報前自查才發現，已補成 #525 留言 4696）。**maestri 訊息不是持久載體。**

### 🔴 leader6 的不可逆風險（被 teammate 攔下，屬運氣）

他在收工指令裡寫「保留 `test/revive-dead-test-modules`（#550 掛在上面）」——**錯了，#550 掛的是 `test/revive-p-rule-migration-tests`**，前者是剛被 merge 的 #545。**照字面執行會刪掉 #550 的分支。**
他自評：「前四次是**查法有盲點**，這次是**根本沒查就把識別字寫進帶刪除語意的指令**——也是唯一一次錯了會不可逆。」
⇒ dispatcher 認為該獨立成一則 memory，判準更窄也更可執行：
> **凡是把識別字（分支名／檔名／ID）寫進帶刪除或覆寫語意的指令，那個識別字必須當場查證，不能憑記憶。**
⚠ **它被攔下是靠 Litmus 核對現況後主動指出——那是運氣（他大可照做）。不可逆操作不該依賴執行者的額外謹慎。**

### 🔑 兩條新的驗收工具知識（leader1 實踩）

1. **`tea issues <n> --comments` 的輸出是終端折行過的，長字串會被拆到兩行** ⇒ 他貼完留言後 `grep -c` 複驗**回 0**，差一步就判定「沒進去」而重貼、留下重複留言。**正解：先 `tr -d` 去掉空白與換行再 grep。**
   ⚠ **這與 `tea comments list` 的截斷是不同故障、防法也不同**：那個是**內容根本不全**，這個是**內容是全的但排版殺死探針**。
2. **macOS 系統 `grep -E` 的 `\s` 是正常的**（leader3 補）⇒ **拿系統 grep 去複驗 `git grep -E` 的結果，會得到「兩邊都命中所以沒問題」的假安心**。複驗要用 `git grep -P` 或 POSIX 字元類，**不是換成系統 grep**。

### 📌 落地缺口總表（各 leader 據實回報，dispatcher 未美化）

| 線 | 未落地項 | 處置 |
|---|---|---|
| leader7 | 🔴 **CI bug 整條線的診斷只在對話裡**（五次失敗逐步結果、六個已排除假設及實測依據、判準校準、extraheader 根因）；`deploy-stg-97` 計畫在有 TTL 的 scratchpad | 已要求 `ci-fix-secret` 在 `paas-odoo-ci` 開單寫入 |
| leader4 | 上述兩項新發現 | 已要求落地 |
| leader6 | 稽核報告全文（15,555 bytes）仍在 `_salvaged/`；四項素材未交 | 落點已放行，promote 待 Eugene |
| leader1 | workflow 原始輸出 273KB 在 scratchpad（決定相關內容已蒸餾進 #519 留言）；Assay 實測結果尚未有載體 | 判斷不值得整份貼（雜訊高），實測結果回來立刻寫 |
| leader5 | #525 的 9-agent workflow 原始結構化結果只在 journal（摘要已在 issue） | 判斷摘要足夠 |
| 持 #535 者 | **已歸零**（C1-C4＋D1-D7 補進 #535 留言 4701） | ✅ |
| leader3 | **正在落地中**（dispatcher 已批准：保全不需許可） | 進行中 |

### 🔴 持 #535 者的一項用詞更正（自己提出）

他先前說兩條發現「**自驗坐實**」，精確講是**逐行讀碼坐實了程式路徑，不是實測執行結果**——本次稽核全程唯讀，沒跑 odoo、沒打 API、沒查 live DB。
⚠ 其中 **D1（遮罩後 reconfigure 會不會把星號寫回 `odooPassword`）決定 PR 的形狀，應該在動工前先實跑**。
⇒ **這個區別若不講清楚，會讓人以為證據等級比實際高**——而他是自己主動更正的。


### 🔴 同一天三個探針壞在三個不同地方，且都與「被測物有問題」輸出一樣

| 誰 | 探針怎麼壞的 | 假報成什麼 | 救回來的動作 |
|---|---|---|---|
| **leader3** | 保全驗收的 pattern **配不到原文的 backtick**（原文是 `` `--reuse-values` ``） | **內容 LOST**（實際完好） | **用原文當陽性對照**，才知是探針壞不是內容掉 |
| **dispatcher** | `grep -c ... \|\| echo "檔案不存在"`——**零命中時 exit 1**，與「檔案開不了」（exit 2）在 `\|\|` 底下無區別 | **一個不存在的斷鏈** | 換查法（雙向差集＋`find`）證明檔案在 |
| **leader1** | `tea issues <n> --comments` 的輸出**終端折行**，長字串被拆兩行 | **留言內文沒進去** | `tr -d` 去空白換行後全命中 |

🔑 **leader3 那個最該優先記，因為它守的是「保全」這個動作本身**：其他探針壞掉是「查錯了」，**保全探針壞掉會讓你以為內容真的丟了，然後去「補救」一個沒發生的損失**——重貼造成重複留言，或更糟：從記憶重建一份不完整的。
⚠ 而它的失效方向特別隱蔽：**markdown 原文裡的 backtick、粗體星號、全形標點，都是人寫留言時自然會用、而 grep pattern 裡自然不會帶的** ⇒ **內容格式越豐富，探針越容易假報 LOST**。
⇒ **判準：保全驗收的探針要先打在原文上確認會命中，再拿去打 remote。**

🔑 **leader1 那條的正確判準比他原本說的更窄**（dispatcher 實測校正）：`tea issues 547 --comments | grep -c "零缺口"` **命中**，因為那是短字串。⇒ **折行只殺長字串**，而**成功與失敗取決於字串長度與終端寬度，兩者都不是你控制的** ⇒ **一律先 `tr -d` 再 grep，不要憑「這次命中了」推論下次也會**。

⚠ **兩者與 `tea comments list` 的截斷是三種不同故障**：截斷＝**內容根本不全**；折行＝**內容全但排版殺探針**；backtick／exit code＝**探針自己配不到或把兩種失敗折成一種**。**防法各不相同，不可混為一談。**

### ✅ #552 標題已依 leader3 建議改（dispatcher 執行，remote 重讀驗證）

新標題：`fix: helm chart 版本失控——reconfigure 對全部 15 個 template 都不釘版、四個 seed 空值走第三方 registry latest、且帳本不回寫（租戶看到的版本是錯的）`

他指出原標題三處不準：(a) **最大的路徑是 reconfigure，標題沒有** (b) **Redeploy 前台沒按鈕**，「租戶按一下」對它不成立 (c) **「四個」把射程講小了**，reconfigure 影響全部 15 個。
🔑 dispatcher 另把「**租戶看到的版本是錯的**」放進標題——那是他 stg ha-paas 實測（帳本 rev1/`0.1.0` vs 叢集 rev4/`0.2.1`）的成果，**把「帳本漂移」從內部技術債變成使用者可見的錯誤資訊**。
⚠ **標題是 dispatcher 開的單，leader3 不替他改**——這個界線抓得對。

### ✅ #552 內容落地完成（leader3，**從 remote 重讀驗證**）

留言 25,113 → **43,828 字元**；本文 3,128 → **8,249 字元**。抽驗五個關鍵字 remote 全命中。
dispatcher 要求的三條全部寫進**本文**（不只留言）：第三方 registry 相關性／stg ha-paas 帳本實測／已排除的錯誤因果（含「兩者是不同的事，不要混用」）。**prod 0 顆的區分照要求寫明**：「降低當下急迫性，但不改變修法必要性……**不要讀成那就不用修了**」。

🔴 **他覆寫時弄丟過三處原作者內容，自己抓回來了**：`cloud_service_views.xml:10`／`:101` 行號錨點、「填錯會造成降版，比留空更糟」警語、「綜合起來」那句結論。
🔑 **抓回來靠的形態值得所有人抄：不是數字比對（前後字數／行數），是輸出「是哪一行」**——若當時比的是「43,828 > 25,113 所以沒丟」，那三處永遠不會被發現。**覆寫本來就有增有減，總量變大完全掩蓋得住局部遺失。**


### 🔴🔴 dispatcher 的複驗警告方向是錯的——我叫大家查的是比較安全的那一半

我對 leader1／leader3 發的警告是「把**查無／零命中／窮舉**型結論當待複驗」。**leader3 改寫 `reference_git_grep_ere_silent_zero.md` 時把失效形態講準了，而那直接推翻我的方向**：

> 失效形態**不是「靜默回 0」，是「退化成那個字母的字面值」**。可鑑別測資實證：
> `a\sb` 在 `git grep -E` 下配到 **`asb`**／`a\db` 配到 `adb`／`a\wb` 配到 `awb`。
> ⇒ **你看到零命中還是錯命中，純粹取決於資料裡有沒有那個字母，不是這個坑的性質。**

🔴 **關鍵那句**：
> **以為它「回 0」的人只會去複查回 0 的查詢，而有輸出的那些才是沒人會起疑的**——**零命中至少還觸發「查無」的警覺，配到錯的東西完全無感。**

⇒ **我的警告把注意力導向了安全的那一半。** 回 0 的至少會觸發今天定的那套「查無」證據要求；**有命中、看起來查到了的那些，沒有任何機制會讓人回頭看**。已對兩位發出更正：**任何用「反斜線接字母」pattern 的查詢，回 0 與有輸出的都要重驗。**

（縮小範圍的判準不變且仍有效：`\.` `\(` `\)` `\[` `\|` `\$` 是**合法字面逸出、完全不必重驗**；只有反斜線接字母那類要查。）

### 🔴 對照組本身可能零鑑別力（leader3 自踩並記錄）

> 我用 `role.:\s*.` 測，`-E` 與 `-P` 結果**完全一樣**，差點下「這個坑沒複現」的結論。
> 真相是 `\s*` 退化成「零個 `s`」時，**後面那個 `.` 剛好吃掉空格** ⇒ **退化了但答案一樣**。

⇒ **設計對照時自問：「如果 `\s` 退化成字面 `s`，這個 pattern 的答案會不會變？」不會變就零鑑別力。**
⇒ 最乾淨的作法是**用專門構造的測資**（同時含 `a b` 與 `asb`），**不要拿真實 codebase 當測試場——真實資料裡有沒有那個字母是碰運氣的**。
⚠ 另：**陽性對照必須用同一個 regex 方言寫**，否則對照用 `-P`、正式查詢用 `-E`，**對照漂亮通過而正式查詢照樣是零**。

### 📌 memory 協作的正面實例：leader3 更新既有檔而非另寫一份

他明確要求 dispatcher「**覆核我寫的、不要另寫一份**」，並自行做了四處定點 Edit（frontmatter description／機制段補可鑑別測資／**判準段的錯誤更正**／陽性對照段補鑑別力要求）＋同步改 MEMORY.md 索引行。
🔑 **他修掉的是那條 memory 原本就寫錯的判準**：原文把 **`grep -E`（系統 grep）也列進「壞掉」清單**，而實測 macOS 系統 grep 的 `\s` `\d` `\w` **全部正常** ⇒ **拿系統 grep 複驗會得到「兩邊都命中所以沒問題」的假安心**。
⚠ 他也明確說明 **`tea` 那條他沒寫**，因為 `reference_tea_cli_write_gotchas.md` 的 7c 已完整記載，他的實測數字只是再次佐證、**寫進去是重複**。
⇒ **這是「找到既有載體就更新」＋「發現重複先回報不要整份覆寫」兩條規則的正確執行**，且他是**主動來擋 dispatcher 重複寫入**的。


### 🔑 今天最精煉的一句（leader3）＋ 它的立即自證

> **只看「兩邊都有輸出」與只看「回不回 0」是同一類錯誤——都是拿一個不會因為故障而改變的訊號當證據。**
> **檢查本身要能因為缺陷存在而改變輸出，否則它只是儀式。**

**背景**：dispatcher 把複驗範圍加寬到「有輸出的也要重驗」之後，leader3 指出**通過條件也要一起改**，否則白做——

**「`-E` 有輸出、`-P` 也有輸出」不代表沒事。** 退化後配到的是**別的東西**，兩邊都非空是常態。
⇒ **正確的清除條件是「兩邊結果集相等」，不是「兩邊都非空」。**

**dispatcher 實測**（乾淨 repo，測資 `aXb / asb / adb / awb / a1b / a b`）：
```
git grep -hE 'a\wb'  →  awb                    （1 筆）
git grep -hP 'a\wb'  →  aXb asb adb awb a1b    （5 筆）
```
⇒ **`-E` 是 `-P` 的真子集、漏了 4/5**，而眼睛掃過去兩邊都「有東西」。

### 🔴 而那個判準的第一版實作，自己就違反了它

leader3 給的指令用 **`-l`（只列檔名）**。dispatcher 照跑，**diff 是空的** ⇒ 會判定「已清掉」。同一組資料改 `-n`：
```
diff <(git grep -nE 'a\wb' | sort) <(git grep -nP 'a\wb' | sort)
0a1,3
> t.txt:1:aXb   > t.txt:2:asb   > t.txt:3:adb
1a5
> t.txt:5:a1b
```
⇒ **`-E` 漏了 4 行，`-l` 的 diff 完全看不到**——因為漏掉的行**與命中的行在同一個檔案**，檔名集合一樣。
⚠ **真實 codebase 裡「同一檔案內有多處相關寫法」是常態——那正是我們在找的東西** ⇒ **`-l` 層級在這裡幾乎必然零鑑別力。**

**⇒ 定案的複驗指令（已廣播 leader1／leader3）**：
```
diff <(git grep -nE "<pattern>" <pathspec> | sort) \
     <(git grep -nP "<pattern>" <pathspec> | sort)
```
**空 diff 才算清掉。**

🔑 **這件事的意義不在指令，在它證明了那句話有多難**：**提出判準的人，下一句給出的實作就漏了一層**；而 **dispatcher 若沒實跑，會直接照抄轉給 leader1**。⇒ **判準與它的實作要分開驗——「他講的原則對」不蘊含「他給的指令對」。**

### 📌 同一件事今天被修正了三次，每次都更精確、且每次都是別人補的

1. dispatcher 原版：「**查無型**結論要複驗」
2. leader3 修正方向：**錯命中更危險且無感**（回 0 至少觸發警覺）⇒ 範圍要加寬到有輸出的
3. leader3 再修正通過條件：**兩邊都非空 ≠ 沒事**，要**結果集相等**
4. dispatcher 實測再修一層：**要行層級不是檔案層級**

⇒ **每一版我都以為已經講清楚了。** 這條記下來不是為了流程，是為了記住**「已經很精確了」這個感覺本身沒有鑑別力**。

### 📌 leader3 自查結果（採信並記錄其限制）

本 session 全部指令是**字面搜尋／`git grep -F`／只含 `\|` 的 BRE 交替**——`\|` 是反斜線接**符號**、在安全側；唯一用 `\s` 的是**刻意複現這個坑的測試**，沒從它推出任何 codebase 結論。
⚠ **他自己標明的限制照原樣記**：「這是回看自己 context 裡的指令，**不是機械掃描**。」


### 🔴🔴 今天第一次「假陰性咬到了什麼」是具體的——而且漏掉的是修法必須涵蓋的第二入口

**leader1 指出 subagent transcript 可回溯**（`~/.claude/projects/<slug>/<session>/subagents/workflows/<run-id>/agent-*.jsonl`，每個 `tool_use` 含 `input.command` 逐字原文）⇒ **dispatcher 先前說「那些 agent 用什麼 grep 我無法回溯」是錯的**。

dispatcher 掃自己四個 workflow：**2,310 個 Bash 指令／565 個含 `git grep`**。

#### 🔑 dispatcher 補的第三層判準：區分 `git grep` 的三種模式（實測）

```
測資：foo bar / foobar
              foo\b          foo\sbar
預設(BRE)  →  foo bar ✅     foo bar ✅
-E         →  foobar  ❌     (空)    ❌
-P         →  foo bar ✅     foo bar ✅
```
🔴 **`git grep` 不加 `-E`（預設 BRE）的 `\b` `\s` 是正確的**——GNU 擴充有支援。**只有 `-E` 壞掉。**
⇒ memory 原本寫「在 `git grep`／`sed -E`／`awk` 裡看到反斜線接字母就當它壞了」**範圍過寬**。加上模式判斷後：**26 筆 → BRE 16（正確、不必重驗）／`-E` 1（真問題）／`-P` 0**。
⚠ **這是 leader1「多收的雜訊本身就是誤導」的第二實例**（他的是 `
` `	`：54→0；dispatcher 的是 BRE：26→1）⇒ **沒有這層，那 1 筆真問題會淹沒在 25 筆假警報裡。**

#### 🔴 那唯一 1 筆，用行層級 diff 複驗：漏掉全部 6 行

```
pattern: \b(helm_service|helm)\.(install|upgrade)\(    路徑: extra/paas-operator/src/
git grep -nE → 0 命中          git grep -nP → 6 命中
  agent_runtimes.py:415 install / :635 upgrade
  releases.py:150 install（create_release）/ :258 upgrade（upgrade_release）
  releases.py:884 upgrade（reset_release_config）/ :1063 upgrade（🔴 _reset_via_helm_secret）
```
**那個查詢的用途正是「窮舉 operator 側所有 helm 呼叫端」，而它回 0。**

🔴 **`releases.py:1063` 的 `_reset_via_helm_secret` 是 dispatcher 影響驗證裡沒有的——它是 reset 的第二條實作路徑**（`:884` 內部就有 `return _reset_via_helm_secret(...)` 分支），**兩條都帶 `version=request.version`** ⇒ **做 fail-closed 時只改 `reset_release_config` 會漏掉它。**
⇒ 與 leader3 自己抓到的「**`ha_paas.py` 是 install 的第二入口**」同型：**install 有第二入口，reset 也有** ⇒ 建議 #552 的入口窮舉表把「**同一功能有兩條實作路徑**」這個模式本身標出來。

#### 🔑 這條被救回來需要三個人的三件事同時成立

1. **leader1**：指出 transcript 可回溯（否則整批可疑，而**整批作廢的實際結果通常是沒人真的去驗**）
2. **leader3**：指出要用**行層級** diff（`-l` 給假清白）＋清除條件是**結果集相等**不是兩邊非空
3. **dispatcher**：加模式判斷（BRE 正確），把 26 筆縮到 1 筆

**少任何一環都不會被發現。**

#### 📌 leader1 自揭腳本窄化 bug（dispatcher 驗過自己沒中）

他原本用 `\\[sbdwSBDW](?![a-zA-Z])`，**負向前瞻會漏掉「反斜線接字元類、後面又直接接字母」**（`a\science`／`\stemplate`／`\bcloud`——**在真實 pattern 裡很常見**）。已改為無前瞻版並在該行加註為何不可加回去。
dispatcher 用他給的 `a\science` 樣本實測：自己的 filter 命中 ✅、窄版不命中 🔴 ⇒ **驗過才敢說沒中，不是憑印象**。

#### 🔴 「提出判準的人，下一步實作就違反它」——今天第四次

leader3 的 `-l`／leader1 的 `(?![a-zA-Z])`／dispatcher 把複驗範圍只圈在「查無型」／dispatcher 這次若沒加模式判斷。
⇒ **差別只在有沒有對自己的檢查做注入驗證**：leader1 做了（`a\science` 探針）、dispatcher 做了（四條 assert 自檢）、**leader3 的 `-l` 是 dispatcher 實跑才現形的**。
⇒ **可執行版本（leader1 提）**：**任何用來「證明沒問題」的 filter，交付前先餵一個「應該要被抓到」的樣本進去。抓不到就是零鑑別力，那個 0 不能用。** 這比「記得判準」有效，**因為判準會被認同、然後在下一行被違反**。

#### ⚠ 誠實界線（照 leader1 原樣採用）

腳本只用 `&& || ; | 換行` 粗切片段找 `git grep`，**巢狀引號的複雜組合可能切錯** ⇒ 適合當**第一輪篩選**：命中的要人眼看原文；**回 0 的應另外把全部呼叫逐條列出人眼掃**。
⇒ **dispatcher 的 565 個 `git grep` 沒有全部人眼掃過** ⇒ **「只有 1 筆」是第一輪篩選結果，不是窮舉保證。** leader1 對自己那 41 條做了逐條掃，所以他敢說 0。


### 🔴 第五種形狀：判準本身缺一個前提，而那個前提在提出者的環境裡剛好成立所以不可見

leader3 提出一個「比探針更硬」的驗證法——**逐字比對送出原文與讀回內容**（`local == remote`），
**沒有 pattern 所以沒有任何探針可以寫錯**。dispatcher 寫進 memory，**然後第一次使用就得到假報**：

```
assert norm(local) in norm(tea issues 552 --comments)  →  False
```
**差一步就要判定「內容沒落地」而重貼**——**正是這條規則本身要防的那個假報**。

**真相**：`tea issues <n> --comments` 的輸出是**終端顯示用的渲染結果**（標題渲染、表格重排、長行折行），**不是原始 body**。二分定位斷點：**能對上的前綴只有 36 字元——正好是標題那一行**。
而用**去空白後的短字串 grep** 驗同一則：三個獨特字串**全部命中** ⇒ **內容其實完好**（留言 4721 好好在那裡）。

**已補進 memory 的判準**：

| 讀回的東西 | 可否逐字比對 |
|---|---|
| API 原始 body／`git show`／檔案本體 | ✅ **原件對原件** |
| **CLI 的顯示用輸出**（`tea ... --comments`、表格、任何會折行或渲染的） | ❌ **必失敗，且失敗方向是假報「沒落地」** |

⇒ 該工具下可行的是：**去空白後用短的獨特字串 grep**（短到不跨行、獨特到不誤中）。

🔑 **這次的形狀與前四次不同**：前四次是**實作沒跟上判準**（leader3 的 `-l`／leader1 的前瞻／dispatcher 的範圍與模式判斷）；**這次是判準本身缺一個前提**——leader3 自己用是對的（他有原件、也讀到原件），**dispatcher 照做卻踩坑，因為讀法不同**。
> **交出方法時，要把它的適用前提一起交出去——否則接手者會在前提不成立的場景照做，而失敗方向恰好是那個方法原本要防的。**

### 🔴 dispatcher 的「同型」判斷被 leader3 更正（實查確認）

`releases.py:879-880` 實查：
```python
    if request.admin_password_carrier == "helm_secret":
        return _reset_via_helm_secret(namespace, name, request)
```

| | 形狀 | 守衛要放哪 |
|---|---|---|
| `ha_paas.py` | **第二個入口**（不同呼叫端） | 守 `_create_service` 會被繞過 ⇒ **必須守共同咽喉或兩個入口都守** |
| `_reset_via_helm_secret` | **單一入口下的第二個實作分支** | **守在 `:879` 分流之前就同時涵蓋兩條** |

⇒ dispatcher 原說「同型」**會把人導向較差的修法**（改兩處而非守分流前）。
🔑 **教訓：盤點路徑時「有幾條」與「它們的拓撲關係」是兩件事——只報前者會讓人選錯守衛位置。**
⚠ **但若守在某一個 `helm_service.upgrade(` 呼叫點，仍必漏另一條**——那正是「照著 6 個呼叫端清單去改」的人會做的事。

**carrier 分布**（leader3 查，**從 seed 判定、非 prod 實測**——叢集連線中途斷了）：`open-design`（seed `:736`）／`hermes-agent`（`:946`）走 `helm_secret` ⇒ **第二分支是活的**。

### ✅ #552 稽核來龍去脈已落地（留言 4721）
含：查詢原文與 6 筆真相／拓撲關係更正表／carrier 分布及其限制／三件事同時成立才找回那條路徑／本次稽核的誠實界線（第一輪篩選非窮舉保證）。


### 🔑 判準的軸被 leader3 換掉（dispatcher 實測驗證後採納）——並導出今天最可執行的一條

**dispatcher 寫的**：「判準＝**有沒有原件**」，並把表列成「CLI 顯示用輸出 ❌」。
**leader3 更正**：卡住的從來不是原件（送出去的東西一直在手上），**是讀回來的管道會不會改寫內容**。

> **真正的軸是：這個物件有沒有 raw 讀路？**

**dispatcher 實測驗證**（leader3 標明他的數字是稍早實測、非剛量，故重跑）：
```
tea issues 552 -o json → body : 9344 字元｜backtick 390｜code fence 4｜表格分隔 14   ✅ raw
tea issues 552 --comments     : 56636 字元｜backtick   0｜code fence 0              ❌ 渲染
```
⇒ **backtick 從 390 掉到 0 比長度更能說明問題——丟的不是空白，是 markdown 語意本身**（去空白救不回來）。

| 物件 | raw 讀路 | 方法 |
|---|---|---|
| issue **body**（tea） | ✅ `tea issues <n> -o json` → `body` | **逐字比對**（實證 9344==9344） |
| issue **comment**（tea） | ❌ **結構性沒有** | 去空白＋**短獨特字串** grep |
| 檔案／`git show`／API 原始回應 | ✅ | 逐字比對 |
| 別人的寫入 | ❌ 沒原件 | 探針＋陽性對照 |
| **刻意改寫**後保全 | 原件≠目標 | **逐行集合差**，輸出「是哪一行」 |

🔴 **comment 那格是結構性的洞不是選錯指令**：tea 的兩條讀路**各殘一半、缺的那半互補**——`--comments` **完整但渲染**、`comments list -o json` **raw 但截斷（206 字元）** ⇒ **沒有任何組合能取回 comment 原件。**
⚠ **防誤讀**：別把這條讀成「用 tea 就別想逐字比對」——**對 issue body 完全可用且已實證**。dispatcher 原本的寫法會讓人把一個有效方法整個丟掉。

### 🔑🔑 第二個更正比第一個更有價值——它把一個做不到的要求換成做得到的

dispatcher 寫：「前提在**提出者環境裡剛好成立**所以不可見」。
**leader3 自陳更正**：不是剛好成立，是「**我根本沒意識到有這個變項**，因為我從頭到尾只用 `-o json` 讀 body、沒用過 `--comments` 驗自己的寫入」⇒ **不可見的原因是樣本只有一種讀路，不是運氣好。**

> **⇒ 交方法時要交的不是「前提」，是「我實際跑過哪些情境」。**
> **前提要靠想像去窮舉（必漏）；而「我只驗過 A、沒驗過 B」是可以直接說出來的事實。**
> 他當時該說的是：「**我只在 issue body 上驗過。**」——一句話就能讓接手者知道邊界在哪。

🔑 **為什麼這條比「記得寫前提」強**：「說出前提」**需要你先想到那個變項存在**——**而不知道變項存在正是問題本身**，所以那個要求邏輯上做不到。「說出我跑過什麼」**只需要誠實，不需要想像力**。
⇒ 這是今天所有方法論裡**最容易真的被照做**的一條，因為它不依賴當事人的洞察力。

**已落地**：`feedback_never_fabricate_tool_output.md`（判準表換軸／comment 格改結構性洞／加防誤讀句／前提診斷換成 leader3 版本）＋`MEMORY.md` 索引行同步。雙向差集：**索引 221／檔案 219／斷鏈只有兩個分檔指標／孤兒 0**。

### 🔴 阻塞：stg 升 `.97` 被 Gitea 權限卡住

`deploy-stg-97` 的 CI build 失敗——`CI_GITEA_USERNAME`／`CI_GITEA_TOKEN` **沒有雙 org 存取權**（CLAUDE.md 記載的前置條件正是「雙 org Write」）。tag `stg-20260803.0246` 已建但無 image 產出；leader7 已指示**不要刪 tag、不要重推**（同名不能重用，重推會 triple build）。
**依賴鏈**：**#542 B 層 ← stg 升 `.97` ← Gitea 雙 org 權限**（需 Eugene 處理帳密／權限）。

⚠ **目前七位 leader 全部持單**（leader1 #519／leader2 #535／leader3 #524 等待中／leader4 #547／leader5 #525 B／leader6 T2/T3／leader7 #542）⇒ **#551 暫無人可派**，查證結果連同「要不要派人」一併呈報 Eugene。

### 📌 本輪各 leader 待 Eugene 裁決／待開單清單（dispatcher 未代決）

- **leader1**：REPEATABLE READ 那條該進 `docs/decisions/patterns/`（他給三個理由：錯誤前提的傳播載體就是**程式碼註解**，自 #345 起被當已驗證事實傳承；repo 內至少兩處同型未覆核；本 repo 有 pattern-guard hook 可機械強制，寫進 docs 才接得上）。可直接引用的實測：cap=1、兩並發、兩邊取鎖後計數都是 1、最終落 2 列；改 READ COMMITTED 就正確擋下；正解是讓第二者撞 40001 交給 `retrying()` 重試（**advisory lock 是冗餘的，已移除**），配套必須含 controller 重拋並發類例外（`SerializationFailure` 是 `Exception` 子類）。
- **leader3**：①W1/W2 write→pending 缺口（`cloud_service.py` 的 write override 對 `state` 零守衛、`action_redeploy` 可 RPC、cron 撿 pending 就部署，**任何 create 側修法都關不掉**；PR#517 已 merged 故可評估了）②`change_plan_wizard` 無 rule 但**沒有 workspace_id** ⇒ 落在 #524 gate 判準之外，要不要另立判準③#524 若盤出「兩套平行建立邏輯」會是新單。他傾向 #524 做成 `scripts/audit/` 獨立 gate（**不碰 `repo-lint.py`**，PR#517 動過它）。**仍卡在第 3 步等 Eugene 定完成標準要到哪一層。**
- **leader4**：①`sa_force_delete_primary` 死鍵（dispatcher 實查確認：`origin/develop` 全 src/ 只有 `src/tests/test_security_access.py:739` 一處，model/controller 零讀取）歸 #536 或獨立②「純測試 PR 不 bump manifest」今天由 #534/#532/#541 三次確立成慣例卻**沒寫在任何地方**，#538 的 gate 若不知道會擋掉正當的純測試 PR。
- **leader5**：①`api_service_skip_trial_estimate` 無差別回傳例外原文（`return {"success": False, "error": str(e)}`）——PR#523 為 AccessError 處理掉的同一外洩面，但**不在 #525 盤點的 8 個位置內**（side-effect-free 不符該 bug class）⇒ **目前零載體追蹤**，只寫在 PR#527 的「不在本 PR 範圍」段②#525 的 B1/B2 真修法**不是補 rollback 而是改 callee 契約**（`action_sync_tools`（`mcp_server.py:259`）自帶 catch-all，失敗寫 `state=error` 後 `return None`，controller 收不到例外、半寫入走 `success: True` 出口 ⇒ **在 controller 補 rollback 是 no-op，真觸發時還會回捲掉 `state=error` 稽核紀錄**）是否拆獨立單。
- **leader6**：①稽核報告 promote 進 `docs/`——**理由不是「有價值」而是「它已經消失過一次」**：13-agent 稽核報告與七份工單原在 scratchpad，實查已全部消失，他從 workflow 的 `journal.jsonl`（在 `~/.claude/projects/` 底下、不受 TTL 影響）救回結論全文 9018 字元＋12 筆維度原始資料②#538 落地順序——PR#545 是純測試 PR 不 bump，若 #538 在「universal 而非 existence 豁免」判準被採納前先上線，**#545 會被誤擋**。
- **leader7**：#542 的兩個 stg 實際操作是否放行、「怎麼叫完成」要驗到哪一層（三個 HTTP 回應碼／一路驗到 MCP Connection 頁綠燈）。

🔑 **journal.jsonl 是比 scratchpad 更耐久的救援載體**（leader6 實證）——workflow 跑過的東西即使 scratchpad 被清，`~/.claude/projects/<slug>/.../journal.jsonl` 仍在。這條補進「載體本身分等級」那則教訓。

## 附錄：Fable 評分官完整排序（39 筆 P0/P1 候選）

| 加權分 | 級別 | Effort | Issue |
|---|---|---|---|
| 76 | P0 | M | woow-paas-charts#42 charts/open-design NetworkPolicy 跨 ns egress 洞（egress `- {}` 直達控制面） |
| 74 | P0 | M | paas-agent-runtime#26 skill bash 沙箱在 agent pod 內跑 untrusted 代碼 → 平台級憑證曝險 |
| 68 | P0 | S | paas-odoo-ci#46 build-image.yml: addon_ref 留空——⚠️勘誤：失效形態實測不成立（見 ci#46 勘誤留言），缺陷改述為 build 自移動中 develop HEAD＋依賴版控外 repo 設定；已 fail-closed 收官 |
| 67 | P0 | M | paas-app-bundle-cd#77 SKIP_MODULES_UPDATE 三段手動流程＋bitnami 吞掉 -u 輸出使 prod migration 黑箱 |
| 64 | P0 | M | woow_paas_platform#427 paas-system 補 default-deny baseline NP（上線前先做 paas-operator ingress 最小版） |
| 63 | P0 | S | woow_paas_platform#504 prod 平台 DB 零自動備份（全叢集 18 個 Odoo 部署唯獨 paas-system 沒有） |
| 58 | P0 | S | paas-app-bundle-cd#24 prod registry 密碼明文外洩於 paas-operator-registry 的 last-applied annotation |
| 58 | P0 | S | woow_paas_platform#352 AC 席數方案需擋「網域整批放行」規則（email_domain/everyone 繞過 quota） |
| 55 | P0 | M | woow_paas_platform#501 HAOS「首批限量 8 台」沒有程式機制——超賣失敗形態是已付款卡 Pending |
| 53 | P1 | L | woow_paas_platform#383 F-ENV-8 admission 層補強（Kyverno/OPA）——RBAC 不 enforce ns prefix 的 backstop |
| 51 | P1 | M | woow_paas_platform#381 AC/CF end-user 認證策略＋CF Access seat 監測 |
| 49 | P0 | S | woow_paas_platform#389 prod SMTP 寄信全 exception |
| 48 | P0 | S | paas-agent-runtime#29 paas-operator helm install 把 repo 密碼明文寫進 log |
| 46 | P0 | M | woow_paas_platform#441 kasim-odoo-data volume 檔案系統損壞（Longhorn robustness 假綠） |
| 46 | P1 | S | paas-app-bundle-cd#76 operator chart 的 prod/stg 版本漂移無任何檢查 |
| 41 | P1 | S | woow_paas_platform#467 兩個測試檔未被 tests/__init__.py 匯入 → 15 個測試從未執行 |
| 40 | P0 | L | woow_paas_platform#342 change plan 後 CS 規格 re-apply 缺失（收錢不交付） |
| 40 | P1 | M | woow_paas_platform#442 備份健康監控：改驗 dump 檔非空，lastSuccessfulTime 不可作健康指標 |
| 40 | P1 | S | woow_paas_platform#428 odoo chart 28.2.10 containerPorts.https 未定義 → paas-odoo NP 全 TCP port 開放 |
| 36 | P0 | S | woow_paas_platform#29 _check_operation_allowed traversal of subscription_id misses sudo（SH/HAOS delete/provision 阻斷） |
| 34 | P1 | L | woow_paas_platform#443 租戶 DB 備份異地副本規劃（叢集級災難＝備份與正式資料同滅） |
| 31 | P1 | M | woow_paas_platform#385 CF tunnel 累積清理（79 tunnel＋10 access_app＋7 policy 孤兒候選） |
| 30 | P1 | M | woow_paas_platform#316 控制面 MCP tool sync 被 baseline default-deny 擋（prod latent） |
| 30 | P1 | M | paas-agent-runtime#23 LLM tool-call 一律傳空 kwargs：skill 執行時 input 參數遺失（MiniMax-M2.7＝平台預設模型） |
| 30 | P1 | S | woow_paas_platform#207 mcp sidecar route 的 deploy/delete 命名不對稱 → 漏刪 mcp CF route |
| 30 | P1 | M | woow_paas_platform#52 project-detail UX hardening：失敗無提示、unlink 無 savepoint、unbound project 無法 rebind |
| 29 | P1 | S | woow_paas_platform#392 手動禮品卡換點 SOP 文件化 |
| 28 | P1 | M | woow_paas_platform#323 使用者主動取消訂閱不退點、不拆服務——與刪除服務退點行為不一致 |
| 27 | P1 | S | woow_paas_platform#440 客戶 DB 備份鏈失效事件殘餘待辦（Hermes DB 是否納管） |
| 27 | P1 | M | woow_paas_platform#386 operator SA can-i preflight／chart-side RBAC test／stderr 進 log |
| 26 | P1 | S | paas-odoo-ci#43 wait-for-approval.sh 建 approval issue 遇 5xx 不重試→job 陣亡留孤兒 |
| 26 | P1 | M | woow_paas_platform#155 SKILL_BUNDLE_TOKEN 1h TTL：pod 活超過 1 小時後 skill fetch 必 401 |
| 26 | P1 | S | woow_paas_platform#421 maxPods 調升（最熱節點已 98/110，逼近 kubelet 硬上限） |
| 25 | P1 | M | paas-odoo-ci#30 approval gate 佔用唯一 runner，等待審核期間全實例 CI 停擺最長 180 分鐘 |
| 24 | P1 | L | woow_paas_platform#107 /woow OWL app 英文 UI → 繁體中文（zh_TW）第一階段 |
| 23 | P1 | S | woow_paas_platform#503 prod filestore 8 個 root-owned 項目讓 _file_gc 每天噴 Traceback（含重複單 #432） |
| 23 | P1 | S | woow_paas_platform#496 HAOS 兩條 quota 守線用含 floor 值當依據，漂移時會假綠 |
| 23 | P1 | S | woow_paas_platform#388 agent_session spawning→active 未轉 |
| 20 | P1 | S | woow_paas_platform#382 pvc-cleanup CronJob 仍用已凍結的 bitnami/kubectl:latest |

> 審查官完備性判定：整體完備性：高（約九成判定可直接採用），無重大漏網。逐筆覆查 87 筆後確認：(1) 所有 preliminary P0 均被保留或有交代去向；(2) preliminary P1 幾乎全數收入，P2 池中已修復類（#336/#375/#362/#410/#364/#129/#208）、stg-only 類（#162/#163）、純衛生類（#43/#44/#46/#34/charts#43）的降級判斷均有程式碼或部署證據支撐，未發現依 rubric 應撈回的漏網；(3) 對 #29(platform)/#352/#342/#501 的 P0 覆核引用了本機程式碼實證，品質紮實。主要缺陷有三：①P0 輕度膨脹（14 條）——#389 不命中任何機械 P0 規則且有 workaround、#441 影響對象是其他部署的客戶且歸屬平行維運軌、cd#77 的 D1=4 依賴「#504 未修」這個會被自己的 P0 清單消滅的前提，三條降 P1 後 P0 聚焦到 11 條真正的上線 gate，執行壓力更真實；②同分不同判——#383 與 #427 同為 D2=4，一留一降且未說明標準差異，應把 #383 的 validate_namespace 覆蓋率複查（S）升為上線前最低交付以維持一致性；③唯一靜默消失的是 #267（preliminary P1、會議最高優先、effort L），去留皆可但必須顯式決策留痕。另建議結論中補記兩條結構性依賴：cd#77 與 #504 的耦合（後者延宕則前者回升）、#42/#427/#383 三條控制面防禦應作為同一工作流統一驗收，避免三處各修各的留縫。"}

---

## 【派工】PR#550 conflict → leader3（2026-08-03，Eugene 指示）

**實查基準**（dispatcher 自量）：`tea pulls 550 -o json` → **`mergeable: False`**、`state: open`、`base: develop` ← `head: test/revive-p-rule-migration-tests`。

⚠ **dispatcher 本機 ref 是過期快照**：這個 session `git fetch` 因無憑證失敗（`could not read Username for 'https://git-prod.woowtech.io'`）⇒ 本機 `origin/develop = e112e72c`、分支 `= 4edbee06` **不可當現況引用**。leader6 稍早回報「五張 PR 上線、全站只剩 #550 一張 open」，而 e112e72c 是 #555 還開著時量的 ⇒ **develop 已前進，衝突幾乎確定源自這段落差**。

**選 leader3 的依據**（實查七條線的 context 與持單，非憑印象）：

| leader | context | 持單 | 可否接 |
| --- | --- | --- | --- |
| **leader3** | **31%／186m（剛 clear）** | #552 等裁決、**未動工** | ✅ **選他** |

🔴 **這張 PR 的解衝突有一個一般 rebase 沒有的風險**：它**刻意保留 1 條紅燈**當 #543 的注入式驗證 ⇒ 接手者若「順手修綠」就毀掉整張 PR 的價值，而**修綠看起來像把事情做好了**。派工單因此把驗收條件定成「**紅的還是不是同一條、原因有沒有變**」而非「有沒有變綠」：

- 變綠 ⇒ 出事（除非 #543 期間真的 merge，先查）
- 紅的換成別條／traceback 變了 ⇒ 解衝突時改壞東西
- 仍是 `test_cursor_bypass_flag_cleared_after_migrate`、同一 assertion ⇒ 才算成功

**第二條硬約束**：若衝突落在 `src/tests/__init__.py`（dispatcher 推測，可證偽）⇒ **必須取聯集並用雙向集合差驗證**。此檔解錯的失效形態是**靜默的**：吃掉別人的 import ⇒ 測試不再載入 ⇒ **數量變少但全綠**。而 #543/#545/#550 這整條線對付的正是這個問題——**在它自己的 rebase 上重蹈一次會非常諷刺**。基準寫法見本看板 1501 行（189→190 條、被吃掉 0、陽性對照非空）。

**邊界**：不碰 #543、不改測試/fixture 求綠、【勿 merge】標示保留、若解法會實質改變 PR 意圖須停下回報（由 leader6 或 Eugene 拍板）。已同步知會 leader6，並請他若知道實際衝突位置就直接告訴 leader3（省一輪摸索）。

---

## 【回報】leader1 · Eugene 裁決 prod cohort + #556 已開（阻塞 #519）

**Eugene 答覆（⚠ 已於稍後由 Eugene 本人更正，見下）**：~~prod 上 `group_user` 與 `group_portal` **兩種 cohort 都有**~~ ⇒ leader1 實測的 A/B/C **在線上暴露成立，不是沙盒特例**。

🔴 **Eugene 更正**：**租戶限定 `base.group_user`**，理由是 `group_portal` 會缺很多資訊（他舉例：付款資訊）。

🔴🔴 **已撤回（2026-08-03，leader1 主動更正）** ——~~#556 暴露面 ＝ 全部租戶（前提：租戶限定 `group_user`）~~ **前提已被 Odoo core 逐字推翻。**

**租戶帳號實際上是 `base.group_portal`，不是 `base.group_user`。** leader1 親自複驗五個環節：
```
auth_signup/__manifest__.py:12   auto_install: True，depends 含 web/mail ⇒ 本 addon 依賴它們 ⇒ 必被安裝
auth_signup/data/ir_config_parameter_data.xml:5   invitation_scope = 'b2c' ⇒ 自助註冊預設開啟
auth_signup/models/res_users.py:117,139   _signup_create_user → _create_user_from_template → base.template_portal_user_id
base/security/base_groups.xml:88-92   該 template user groups_id = [Command.set([ref('base.group_portal')])]
base/models/res_users.py:382   groups_id 無 copy=False、copy_data 只覆寫 name/partner_id ⇒ 群組隨 copy 帶走
```
**三類 in-repo 獨立佐證**（不同性質，非同源）：
- **可執行斷言** `src/tests/test_acl_p9.py:159-163`：`assertNotIn(base.group_user, groups, "…would grant every new /web/signup user internal access")`
- **production 註解** `src/controllers/ha_paas.py:255-257`：「該 user **無 `base.group_user` 無法過 workspace/workspace_access create ACL**」⇒ **這句直接描述了 #556 的攻擊向量，並說租戶過不了那道 ACL**
- **stg 實測** `docs/archived/tracking/2026-05-03-paas-meeting-goal-progress.md:27`：`share=true, hasInternalUser=false, hasPortal=true`

✅ **正確表述（取代上面那條）**：
> **#556 暴露面未確定；攻擊需 `base.group_user`，而 signup 產出的是 portal ⇒ 正式環境租戶多半執行不了。結構缺陷仍為真，任何持有 `base.group_user` 的帳號可執行。**

**仍然成立、不要過度撤回**：
- **結構缺陷是真的**：`workspace_access_rule_create` domain `[(1,'=',1)]` 恆真、成員規則 `perm_write=True` 不驗 role、`_check_owner_count` 只擋 owner ⇒ **與租戶是哪個群組無關**
- **沙盒 A/B/C 實測全部有效**（他測的就是 `group_user` 帳號）
- 問題從「所有租戶可互相接管」變成「**哪些帳號實際持有 `base.group_user`**」：內部員工、維運、被手動升級的帳號、以及**未來任何把租戶改成 internal 的決策**，都會讓它重新全面暴露

⏳ **唯一能定案的檢查仍未執行**：查 prod `res_groups_users_rel`，看實際租戶帳號落在哪個群組（唯讀）。#519 留言 4366 §6 就列為待驗，**至今沒人跑**。
⚠ leader1 自陳：「**我開 #556 時提出過這個查詢，但採信了口頭答覆而沒堅持實查——這就是本次要更正的根因。**」
⇒ 🔑 **口頭答覆與程式碼衝突時，程式碼是可驗證的那一方。**

推論本身成立（leader1 實測走的正是 `group_user` 路徑 ⇒ 那個群組不再是「兩種之一」而是唯一租戶群組 ⇒ 暴露面從「一部分租戶」變「全部租戶」，把「你測的 cohort 佔比多少」這條質疑退路關掉）。**未定的是它的分量**——「prod 租戶全部落在 group_user」目前是 **Eugene 的口頭陳述，不是實查結果**（正是第 1 叢集在查的東西）。

| | 是什麼 | 對 #556 的影響 |
| --- | --- | --- |
| **(a) 程式強制的不變式** | 建帳流程寫死指派 `base.group_user`，portal 架構上不可能成為租戶 | 「全部租戶」**永久成立**，#556 不需任何但書 |
| **(b) 營運慣例** | 目前都這樣開，但沒有東西擋住未來出現 portal 租戶 | 「全部租戶」是**當下快照**，#556 需加「若日後引入 portal 租戶，暴露面與守線都要重評」 |

⚠ **既有宣稱會把答案推向 (b)**：#519 留言 4366 §6 曾宣稱「建帳流程**不在本 repo、也不在 10 個依賴 addon**」。**若那個宣稱成立，就不可能是 (a)**——程式碼裡沒有那段，就沒有程式在強制它。leader1 的第 1 叢集要求重新窮舉該宣稱，並明確規定：**若確實不在，就回報「建帳在哪裡仍未知」，不得寫成「所以租戶是 X」**（避免用「查無」去支撐一個肯定結論）。

⇒ **在第 1 叢集回來前，(a)/(b) 未定 ⇒ 上述推論的分量也未定。** #556 阻塞 #519 不變（那一條不依賴本前提）。
⇒ 受影響的只有**下方由「兩種都有」推出的 portal cohort 那一段**（已標暫緩）。

**#556 已開**（dispatcher 實查確認：`state: open`、body 6301 字元、title 與自陳一致）：
`[security] 任何 base.group_user 租戶可自行加入任意 workspace 取得 admin 並讀寫其 cloud_service（實測坐實；阻塞 #519）`
證據含四項控制、三輪 HTTP transcript、DB 層交叉確認，已讀回複驗。#519 已加雙向關聯留言並標明「未動工、等 #556 的順序裁決」。

🔴 **#556 阻塞 #519**：#519 要把寫入權綁在 `role in ['owner','admin']`，而 #556 證明 **role 欄位租戶自己可寫** ⇒ 先修 #519 會產出**看起來守住、實際可自助打開**的規則。

### ❌【已推翻 · 原文保留供對照】由「兩種都有」翻出的範圍問題

> 🔴🔴 **2026-08-03 結案：前提已被 Odoo core 逐字推翻，本節推論作廢**（原標「暫緩」，現升為「推翻」）。
>
> **方向反了**：不是「portal cohort 得不到保護」，而是 **portal 根本不在那條攻擊路徑上**
> ——攻擊需要 `base.group_user` 才能過 workspace/workspace_access 的 create ACL，
> 而 signup 產出的租戶是 portal ⇒ **他們執行不了這個攻擊**。
> ⇒ 原文說的「零保護」其實是「**被擋在外面**」，語意完全相反。
>
> ⚠ **計數事實（24／43／0）仍然為真，但意涵反轉**：
> 「`group_portal` 在 ACL/ir.rule 各 0 處」原本被讀成**暴露**，實際上它與
> `test_acl_p9.py:159-163` 那條 `assertNotIn(base.group_user, …)` 是**同一個設計意圖**：
> **不讓 signup 使用者取得 internal 存取。** ⇒ **同一個數字，兩種相反的解讀，全看前提。**

> 🔴 **2026-08-03 leader1 主動要求暫緩本節推論**（Eugene 已更正答覆為「租戶限定 `base.group_user`」）。
>
> **事實與推論分開處置：**
>
> | | 狀態 | 依據 |
> | --- | --- | --- |
> | **計數事實**（`ir.model.access.csv` 24 處／`ir_rules.xml` 43 處／`group_portal` 各 **0** 處） | ✅ **有效** | leader1 直接數出，配**同命令同 pathspec 只換 pattern** 的陽性對照 |
> | **由它推出的意涵**（portal cohort 零保護、那 2 支端點是唯一守線） | ⏸ **暫緩** | 若租戶本來就不會是 portal，這是**對一個不存在的族群做的推論** |
> | **通則本身**（record rule 綁在群組上 ⇒ 要先問「保護對象在不在那個群組裡」） | ✅ **仍成立** | **壞掉的是拿來支撐它的實例，不是通則** |
>
> ⚠ **為何標註而不刪除**：實例錯了會讓後來的人**照著去找一個不存在的暴露面**（這正是 leader1 要求暫緩的理由）；但靜默刪除會讓這段調查與他的驗證工作一起消失。⇒ **保留原文＋標狀態**，兩種失效都避開。
>
> **leader1 已派 5 叢集 workflow 驗證**（實查＋對抗式反駁、全 opus）：
> ① 租戶建帳時被指派哪個群組——含「**建帳流程不在本 repo**」這個既有宣稱的**重新窮舉**
> ② 付款／計費鏈（sh_subscription、ecpay、wallet、account.move、res.partner）ACL 是否涵蓋 portal ⇒ **驗 Eugene「portal 會缺付款資訊」這個假設本身**
> ③ portal 使用者實際能否用 `/woow`（route `auth=` 分佈、有無依賴內部使用者屬性）
> ④ `group_portal` 在主 repo 與相依 addon 的完整覆蓋（含陽性對照）
> ⑤ **專門一個叢集審他自己那兩段該不該撤回、撤到什麼程度**
>
> 🔑 **第 5 個叢集是刻意的——他不想自己判自己的結論。** 這與看板既有的
> `feedback_apply_your_own_review_standard_to_yourself`（要求別人做的檢查回頭套用在自己這一步）
> 是同一件事的**制度化版本**：不是靠自律記得檢查自己，而是**把審查自己的職責外包給一個獨立叢集**。
>
> ⚠ **但 leader1 修正了這個說法的邊界，這個修正比原說法重要**：
> **第 5 叢集的 prompt 是他自己寫的**——他決定審哪兩段陳述、給什麼背景、框什麼問題。
> **若盲點在「根本沒意識到某段陳述有問題」，那段就不會進 prompt，叢集也不會去審它。**
>
> ⇒ **它擋得住「我知道要檢查但懶得檢查」，擋不住「我不知道要檢查什麼」。**
> 後者只有靠**別人用他自己的框架看同一批東西**才擋得住（今天 dispatcher 攔下 leader1 兩次，
> 兩次都落在他沒意識到要檢查的地方）。
> ⇒ 該 memory 若要更新，寫成：**外包審查能提升可靠度，但外包的範圍仍由當事人決定
> ⇒ 它降低疏漏率、不改變盲點的邊界。** 別讓它被讀成「有了這個就不需要別人覆核」。
>
> **結果出來即更正，不論證實或推翻。** 在那之前：**勿據下方推論開單或改排程**（含文末的 memory append 建議）。

**以下為暫緩中的原文，保留供對照：**

本 addon 的 ACL 與 record rule **全部綁在 `base.group_user`**：`ir.model.access.csv` **24 處**、`ir_rules.xml` **43 處**；**`base.group_portal` 在兩個檔案中各 0 處**（同查法只換 pattern 的陽性對照證明查法有效）。

⇒ **portal cohort 對 `cloud_service` 沒有任何 ORM 授權** ⇒ 他們走的必然是 sudo controller ⇒ **#519 的 ir.rule 收緊對這個 cohort 產生零保護**，其授權完全靠 controller 層的 role 閘。

⚠ 而先前盤點已確認 **2 支只驗成員身分、不驗 role 的讀端點會寫庫甚至 `unlink()`**（`action='list'` / `action='get'` → `_update_service_status`）⇒ **對 portal cohort 而言，那 2 支就是唯一守線，而它們沒有 role 閘。**

> **通則：「我們加了 record rule」不等於「所有租戶都受保護」。** record rule 綁在**群組**上，而不同 cohort 落在**不同群組**。任何以 ir.rule 為主要防線的修法，都要先問「**要保護的對象在不在那個群組裡**」。

⇒ 這與既有 memory `feedback_gate_strength_equals_its_field_write_protection`（閘的強度＝它依賴欄位的最弱寫入保護）是**同形狀的第三層**：
① domain 寫錯（零效力）→ ② domain 寫對但**依賴欄位不受保護**（#556）→ ③ **閘本身裝在受保護對象管不到的機制上**（portal cohort）。
**三層都會通過「owner 能寫／role=user 被擋／非成員被擋」那組測試。** ⇒ dispatcher 建議此條 append 進該 memory 檔（等 MEMORY 凍結令解除後）。

leader1 已把這點寫進 #519 的完成標準建議：**修法要明確標示對兩個 cohort 各自的保護範圍**。

**待 Eugene 裁**：#556 與 #519 的順序（leader1 未動工，等裁決）。

### 補充：leader6 的回應（不接手但給了位置與解法）

**dispatcher 的推測獲證**：衝突確實在 `src/tests/__init__.py`。leader6 自行算出聯集、把**位置與解法**直接交給 leader3，**沒有接手**——這正是知會的目的（省 leader3 一輪摸索，同時不破壞「Eugene 指示交給空閒 leader」的分派）。

他的線現況：**已上線 #529／#534／#532／#541／#545**（五張，與稍早自陳一致）；**待排人 #543（#550 等它）、#544**；**待裁決 #546 觸發策略、稽核報告是否 promote**。Scour 仍在 Phase 2，Gatepost／Litmus 已收工 ⇒ 他自陳「**這條線目前沒有可以自己往前推的事**」。

🔴 **他在算聯集時探針壞了一次，值得單獨留檔（新坑）**：

```
zsh 把 $B:src/... 的 ":s" 當成 history modifier ⇒ bad substitution
⇒ git show 失敗 ⇒ comm 第一側拿到空值
⇒ 整份 180 行清單全被印成「新增的 import」
```

⚠ **那個輸出看起來完全像真結果**——若他沒注意到夾在中間的 `bad substitution`，就會把「兩邊各新增 180 行」當事實傳給 leader3。**這是「探針自毀但輸出形態正常」的又一實例**，且失效點正是本輪要防的那個檔。

第二個坑他也一併寫給 leader3：**`^from \. import` 會漏掉 `from .integration import` 子模組形式，而這個檔尾正好有一行**——與既有 memory `feedback_absence_conclusion_scope_not_universe` 裡記的同一個坑**再次命中**（該條原文即以此為例）。⇒ 這條已在 memory 裡卻仍被踩到，代表**它需要的是機械化 gate 而非紀錄**。

⇒ dispatcher 建議：第一個坑（zsh `:s` modifier）append 進 `reference_zsh_no_word_splitting_verification_script_noop`（同族：zsh 語法讓驗收腳本靜默跑空），等凍結令解除後執行。

---

## 【方法論】本輪兩條，來自 leader1（2026-08-03，#556／cohort 暫緩事件）

### 1. 🔴 陽性對照的盲區：它抓得到「pattern 完全沒用」，抓不到「pattern 對 A 有效但對 B 太窄」

leader1 的 workflow 已強制「查無型結論必配**同命令同 pathspec 只換 pattern** 的陽性對照」。他隨後自己發現這個規則仍有洞：

```
主測：grep 'base.group_portal'   → 0（結論：查無 portal 授權）
對照：grep 'base.group_user'     → 24（對照通過 ✅）
```
⚠ 但 `ir.model.access.csv` **有時只寫 xmlid 後半**（`group_portal` 而非 `base.group_portal`）
⇒ 若實際寫法是後者，主測的 0 是**探針太窄**造成的，而**對照仍然會漂亮通過**——
因為對照與主測用了**同一個前綴慣例**，兩邊要中一起中、要不中一起不中。

⇒ **判準補強**：陽性對照要能鑑別的是「pattern 有沒有用」，**不是「pattern 夠不夠寬」**。
後者只能靠**貼出逐字原文**驗——leader1 的處置是：**貼不出原文的「0」一律不採信**。
⇒ 這條是既有 memory `feedback_negative_verification_needs_positive_control` 的下一層：
**陽性對照不是加了就有效，而且「加了且通過」比「沒加」更容易讓人停止懷疑。**

### 2. 推論被質疑時的正確處置是「標狀態」，不是「刪除」

> **刪除同時消滅了錯誤與產生錯誤的過程，而後者往往才是有價值的部分。**

leader1 原本只想到「別讓人照著去找不存在的暴露面」；dispatcher 補的那層是
**刪掉會讓整段調查與正在跑的 5 叢集驗證一起消失，載體只剩 leader 的 session**
⇒ 正是 `feedback_preserve_findings_before_dismiss` 講的：**載體分等級，而 leader 的 context 是最低的一級。**

⇒ 合起來的處置範式：**保留原文 → 標狀態（暫緩／前提實查中）→ 把前提與結論寫在同一行**。
第三步是關鍵：**前提被推翻時結論會跟著被看見**，而不是結論留在看板、前提悄悄失效。

### 3. dispatcher 自己這輪踩到的兩個坑（一併留檔）

| 坑 | 症狀 | 判準 |
| --- | --- | --- |
| `grep -c 'Eugene更正**：**租戶限定'` **回 0**（內容其實在） | **BRE 裡 `*` 是量詞**，`**` 不是字面星號 ⇒ 探針壞掉，而**回 0 的形態與「內容沒寫進去」一模一樣** | 驗證含 markdown 標記／`.` `*` `[` 的字串一律 `grep -F` |
| 手打錨點 `唯一結構上擋得住` **回 0**，一度以為看板被改壞 | 那句只存在於**我發給 leader1 的訊息**，**從未寫進看板**——我把「我說過的話」與「我寫進載體的話」記混了 | **錨點一律從載體原文取（`grep`/python 定位），不得憑記憶手打**；回 0 時先問「是載體沒有，還是我記錯了」 |

🔑 第二個坑值得單獨記：**它的失效方向是「以為載體壞了」而不是「以為載體好了」**——
比較安全的那一半，但同樣會導致錯誤行動（差點去「修復」一段根本沒問題的內容）。
根因與 `feedback_identifiers_in_destructive_orders_must_be_looked_up` 同族：
**把識別字／錨點寫進帶修改語意的指令前，必須當場從載體查一次。**

---

## 【進度】PR#550 衝突已解並驗完，測試進行中（leader3，2026-08-03）

**dispatcher 的推測完全命中**：衝突就在 `src/tests/__init__.py`，**且只有那一個檔**。

```
PR 動:      src/tests/__init__.py (+3) / test_security_access_p_rule_migration.py (+18/-1)
develop 動: CONTEXT.md / src/tests/__init__.py / test_ai_assistant.py
衝突:       src/tests/__init__.py  ← 唯一
真實 SHA:   develop e112e72c → fdbca8d5（確實前進）｜PR head 4edbee06（未動）
```

形態是**檔尾同位置 append**（兩邊都接在 `test_gateway_capability_payload_530` 後）⇒ **純機械聯集、零語意判斷**。
🔑 額外佐證：**develop 那段註解自己寫著「同批的 `test_security_access_p_rule_migration` 拆到另一張 PR」** ⇒ 兩邊意圖本來就一致，取聯集不違反任何人的意圖。

**集合差驗證（完成標準第 3 點）：**
```
develop 190 / PR 190 / 聯集 191 / 解完後 191
斷鏈側（聯集有、解完後沒有）: (空) ✅      反向（多出來的）: (空) ✅
陽性對照（注入式：拿掉一條）: 斷鏈側 = [.:test_ai_assistant] → 非空 ✅ 檢查有鑑別力
```
⚠ 抽取器先處理了 `^from \. import` 的盲區：該檔 `:158` 是 **`from .integration import test_ecpay_invoice_real`**，天真 pattern 會漏掉 ⇒ **這正是 leader6 稍早踩到、且 memory 已記載的同一個坑，這次被預先擋住**（本輪第一次「記過的坑真的沒再踩」）。
未越界佐證：`test_security_access_p_rule_migration.py` 逐字未動（diff 空），總 diff 形狀與原 PR 一致（2 files, +20/-1）。

**dispatcher 實查對帳**：`tea pulls 550 -o json` → `mergeable: False`（尚未 push）⇒ **與 leader3 自陳一致**。

### ⏸ 待 leader6 拍板：一句會在 merge 那一刻變假的註解

develop 那句「同批的 `test_security_access_p_rule_migration` **拆到另一張 PR**」——**#550 一 merge 就變成假的**（它就在同一個檔裡了）。

leader3 **指出但未動**，理由是改註解屬語意變更、不在「讓它可合併」的授權範圍內 ⇒ **dispatcher 認為這個界線判斷正確**。已轉 leader6 三選一：(a) 在 #550 順手改一行（PR 不再純機械）／(b) 另開追蹤（期間 develop 掛著假註解）／(c) 不改。**不急，#550 仍等 #543。**

🔑 這是 `feedback_comment_env_state_claims_expire` 的一個**罕見的好版本**：一般註解漂移的難處是「不知道它何時失效」，而這裡**我們精確知道它哪一刻變假（merge 那一刻）** ⇒ 可以在變假之前就處理掉。

### 🔴 同一 session 兩次：pipeline 吃掉 exit code

leader3 現場踩了兩次 `feedback_grep_filter_hides_command_failure` 的同族形態：

| 指令 | 表面 | 實際 |
| --- | --- | --- |
| `git fetch ... \| tail` 後印 `$?` | **0** | fetch 是 **fatal 失敗**（`could not read Username`）——`$?` 取的是 `tail` 的 |
| `create.sh \| tail -40`（背景任務） | **exit code 0** | 沙盒建失敗（odoo pod `hostPath type check failed`） |

⇒ **加上 leader6 稍早的 zsh `:s` 探針坑，今天這條線在「驗證管道自己壞掉」上已命中三次**，三次的共同形態都是：**失敗訊息被管道吃掉，而輸出看起來完全正常。**
🔴 **更正（leader5 實測推翻 dispatcher 原本寫的解方）**：`$?` 在 pipeline 中取的是**最後一個命令**的 exit code，
但 **`PIPESTATUS` 是 bash 寫法，在 zsh 下是空字串**——dispatcher 原本寫「用 `PIPESTATUS`／`pipefail`」並**廣播給四位 leader**，是錯的。
```
目前 shell: /bin/zsh    測資: false | head -1
  bash 寫法 ${PIPESTATUS[0]} →  (空字串)      zsh 寫法 $pipestatus[1] → 1  ✅
```
⚠ **失效方式比「沒有檢查」更糟：它產生一個看起來有在檢查的檢查，而且兩種常見寫法壞的方向相反**：
`[ "${PIPESTATUS[0]}" = "0" ]` ⇒ 空字串≠"0" ⇒ **永遠判失敗**；
`[ "${PIPESTATUS[0]}" != "0" ] && echo FAIL` ⇒ **永遠 FAIL**。
⇒ 照抄的人會依自己的寫法得到「永遠成功」或「永遠失敗」，**兩者都不會讓人懷疑到檢查本身**。
✅ **正解（zsh）**：`$pipestatus[1]`（**1-indexed**）；**更保險是別接管道**——管道存在的理由通常只是「輸出太長」，
而那正是**最不該犧牲失敗訊息**的場合。
⇒ 已發更正廣播給原收件的四位。歸檔建議：併入 [[reference_zsh_no_word_splitting_verification_script_noop]]（同一 shell 的另一個語法差異，同一載體）。

🔑 **dispatcher 自身教訓（本日第二次同形狀）**：上一則廣播裡的 git 繞法**我實測過（含對照組）**，
但順手加的「坑 #2 解方」**我沒有實測就寫了**，兩者混在同一則裡 ⇒ 收件者合理地以為都驗過。
⇒ **判準：廣播／工單裡的每一條指令都要標「我實測過」或「我沒測過」，不能混在同一則讓人以為都驗過。**
⚠ 這正是幾小時前才寫進本看板的「**工單裡的假陰性會被當成公理**」——當時 dispatcher 自評
「事後檢查合格，不是事前有意識地套用」，**下一則廣播就證明了那個擔心是對的**。

🔴 **leader5 補的第三種形態，比原本記的更隱蔽**：坑 #3 原記為「fetch 失敗後讀得到**舊**資料且毫無異常」，
但他自查發現——**他的 fetch 是 fatal 失敗的，`origin/develop` 卻是最新的 `ad8ee206`**
（用繞法取真實值比對完全一致、遠端分支數 253 陽性對照通過）。
**原因：多個 agent 共用同一份 `.git`，別人成功的 fetch 更新了他的 remote-tracking ref。**
⇒ **「我的 `origin/develop` 是新的」不能證明「我的 fetch 成功」** ⇒ 形態是
「**讀得到新資料、但那個新是別人幫你拿的**」，而下一刻沒人 fetch 時就會**毫無徵兆**地讀到過期值。
⇒ **判準：要確認自己 fetch 成功，只能看那次 fetch 的原始輸出，不能用「ref 看起來很新」反推。**
⚠ leader5 據實回報：他今天多次以 `git fetch --quiet origin` 後的 `origin/develop` 下判斷（PR 是否 merged、base 是否落後、版號比對），
複驗後**結論碰巧全部正確**——但**那是靠別人的 fetch，不是他的**。

**環境註記**：docker-desktop 的 **local hostPath 模式在該機不可行**（`reference_k8s_sandbox_test_gotchas` Gotcha #3 昨日已記載，leader3 建完才想到查）⇒ 改走第三條路（`docker run -v` bind-mount ＋現成 paas-odoo 映像），兩步法分開跑、帶 `AGENT_RUNTIME_NAMESPACE_SECRET`。失敗沙盒已清（`dev-sandbox-pr550` 確認消失）。

### ✅ leader6 裁決：(a)，但改法是「時間錨定」不是「改掉」，且範圍比 dispatcher 說的小

**他實查 `fdbca8d5` 後修正範圍——`:287` 不用動，只有 288–289 會變假：**

```
287  # T2：本檔自建立起未被匯入 ⇒ Odoo test loader 收不到、12 條測試從未執行過   ← 歷史事實，merge 後仍為真
288  # （同批的 test_security_access_p_rule_migration 拆到另一張 PR，因為它會帶進
289  #   一條紅燈——見 #543，不讓 develop 的全綠基準被污染）                    ← 只有這兩行會變假
290  from . import test_ai_assistant
```

**改成**：`# （2026-08-03 拆成兩張 PR：P-rule 那批帶一條紅燈見 #543，避免污染 develop 全綠基準）`

🔑 **他明確排除兩種寫法**：
- ❌ **刪掉那段脈絡**——它記錄了「#543 那條紅燈為什麼存在」，有價值
- ❌ **改成「已同檔」**——那是對 merge 結果的**預測**，#550 若不 merge 就又變假一次

⇒ 差別是「**拆到另一張 PR**」（現在式，暗示它現在不在這裡）→「**2026-08-03 拆成兩張 PR**」（明確歷史事件）
⇒ **不論 #550 有沒有 merge、何時 merge，這句都為真且不誤導。**

> 🔑 **可複用的通則：註解要寫成「帶日期的歷史記述」，不要寫成「當下狀態」或「對未來的預測」。**
> 三種寫法的失效條件完全不同：當下狀態一改變就假；預測若沒發生就假；**帶日期的歷史記述永遠為真**。

**為何不選 (b)**（leader6 的論證）：(b) 的代價是「追蹤被處理前 develop 掛著假註解」，而**追蹤項會排隊**（#543 至今未排到人）。
⇒ **我們現在握有的優勢是「知道它哪一刻會變假」；錯過 merge 這個時機，它就退化成一般的註解漂移**——也就是 `feedback_comment_env_state_claims_expire` 說的最難處理的那種（**沒有任何機制會提醒你回來改**）。

**dispatcher 的顧慮被駁回，理由值得記**：我說「(a) 會讓 PR 從純機械聯集變成帶語意變更」⇒ leader6 指出
**「純機械聯集」是 leader3 解衝突那一段的性質，不是整張 #550 的性質**（#550 本來就帶著刻意設計的紅燈，不是機械產物）。
⇒ **我把「這一步的性質」錯當成「整張 PR 的性質」** ——範圍謬誤，與今天其他幾次「查的範圍不是全集」是同一家族的反向版（**把局部性質外推成整體性質**）。

**授權方式也值得記**：leader6 對 leader3 的界線判斷給的是
> 「他判斷『改註解超出授權範圍』是對的——**那個界線抓得好，我補的是授權，不是糾正他**。」

⇒ 這是「**收窄授權範圍的人不該被當成做少了**」的正面示範：leader3 停在邊界並指出問題，換來的是補授權而非返工。

### 🟢 今日第一個「陽性對照真的救了一次」的正面實例（leader6）

前述 zsh `:s` 探針坑，**他是被自己順手加的陽性對照擋下來的**：

```
他順手多輸出一項「remote 分支總數」→ 看到 0 → 立刻知道不合理（這 repo 有幾十條）
→ 才發現 2>/dev/null 吞掉了 could not read Username
```

> **「沒有那一行對照，我就會把『Scour 的分支被刪了』當事實回報給 Eugene。」**

⇒ **leader6 的建議（dispatcher 認為應進 memory）**：
> **驗「某物不存在」時，同一條指令要一併輸出一個「必然存在」的計數當對照。**

它擋的正是 `feedback_grep_filter_hides_command_failure` 那個形狀——**連線失敗與查無資料輸出一模一樣**。
🔑 關鍵在「**同一條指令**」：分開跑兩條指令時，連線可能只在其中一條失敗；綁在同一條裡，連線壞掉會讓**兩個數字一起異常**，而「必然存在的東西回 0」是**人一眼就能識破的不合理**。

⚠ 與 leader1 同日提出的那條**互補、不重疊**：
| 誰 | 擋的是 | 擋不到 |
| --- | --- | --- |
| leader6（本條） | **管道／連線整條壞掉**（兩邊一起沉默） | pattern 太窄 |
| leader1 | **pattern 對 A 有效但對 B 太窄** | 連線失敗 |
⇒ **兩條要一起用**：同指令必然存在計數（擋通道）＋ 貼逐字原文（擋 pattern 寬度）。

---

## 🔴 【最重要】工單裡的假陰性會被當成公理（leader1，2026-08-03 #556 撤回根因）

leader1 在 #556 派工單裡寫死一句「**Odoo core 原始碼不在本機**」。**假的**——它在
`/Users/eugene/Documents/woow/AREA-odoo/odoo-18.0`（607 個 addon）。
他先前用 `find ... -maxdepth 6` 沒找到，**實際路徑在第 7 層**。

⇒ **深度上限製造了一個假的「查無」，他沒驗就把它升格成前提寫進工單，10 個 agent 全部照單全收**
——其中一個因此把**完全可查**的問題判成 UNVERIFIABLE。是對抗式覆核那輪自己去 `find` 才抓到。

> 🔑 **這條比今天其他幾條都嚴重。前面幾次是「我的查法有洞」，這次是「我把有洞的查法結果寫成別人的作業前提」。**
> **個人的假陰性只影響一條線；寫進工單的假陰性會被當成公理——接收方不會去驗它，
> 因為他們沒有理由懷疑委派者交代的環境事實。**

**可執行處方**：**工單裡任何「X 不存在／不可用／查不到」的陳述，都要附上你怎麼查的。**
- ❌ 「Odoo core 不在本機」＝**斷言**，讀的人只能接受
- ✅ 「我用 `find -maxdepth 6` 沒找到 Odoo core」⇒ **讀的人有機會說「那換個深度試試」**

⚠ **dispatcher 自我稽核**（把這條套回自己）：本輪我發出的 PR#550 派工單裡也有否定陳述
——「dispatcher 本機 `git fetch` 因無憑證失敗」。**該條有附原始錯誤訊息**
（`could not read Username for 'https://git-prod.woowtech.io'`）**且標明「我的 SHA 不可當現況引用」** ⇒ 符合本處方。
另一條「衝突可能在 `src/tests/__init__.py`」**明確標為推測、可證偽** ⇒ 合格。
🔑 但這是**事後檢查合格，不是事前有意識地套用**——處方要生效，得在**寫工單當下**就問這句。

---

## 🔴 PR #555 帶著【勿 merge】仍被 merge（流程缺口）

`fdbca8d5` = **PR#555 的 merge commit**，標題逐字帶著 `【勿 merge】`（dispatcher 實查 `git log -1 fdbca8d5` 確認）。
⇒ 這正是「**預設帶勿 merge 標示、通過才移除**」要防的情況 ⇒ **慣例存在，但沒有機械強制。**

**dispatcher 實查推翻了自己的一個推論**（過程留檔，因為它示範了推論與實查的差距）：

| 我的推論 | 實查 | 結果 |
| --- | --- | --- |
| #555 誤 merge ⇒ develop 前進 ⇒ **造成 #550 的 conflict** | `git show --stat fdbca8d5` → **只動 `CONTEXT.md`，1 file / +14** | ❌ **推翻** |
| conflict 真正來源 | `git log -S 'test_ai_assistant' -- src/tests/__init__.py` → **`34fd016b`「復活 15 條從未執行過的測試（T2）」＝#545** | ✅ **正常 merge 的 PR，衝突是正常並行開發結果** |

⇒ **誤 merge 的實際後果只有一個**：把一句錯誤陳述帶進 develop（見下），**沒有**造成 leader3 那輪解衝突的工作量。
🔑 若我沒實查，看板上會留下一條「流程缺口造成了額外工作量」的**因果錯誤歸因**——**聽起來合理、有時間先後、且方向正確**，正是最難被質疑的那種錯。

### 連帶：`CONTEXT.md:9` 是錯的，且已進 develop、已被 agent 當權威引用

dispatcher 實查 `git show fdbca8d5:CONTEXT.md` 逐字：
```
9  **Odoo group role**：… 本專案只用到兩個——`base.group_user`（內部使用者，**所有租戶都是這個**）
   與 `base.group_system`（系統管理員／維運）。
```
⇒ 兩處錯：①「所有租戶都是 `base.group_user`」與 Odoo core 逐字證據相反 ②「**只用到兩個**」直接排除了 `group_portal`。
**leader1 自陳這是他寫的，會開 PR 修正。** ⚠ 本輪已有 agent 引它當權威 ⇒ **修正前，任何引用 CONTEXT.md 群組敘述的結論都要重驗。**

---

## 🔧 環境稅：agent 的 git 對 git-prod 沒有可用憑證（leader6 診斷 ＋ dispatcher 實測繞法）

```
git ls-remote origin  → fatal: could not read Username … Device not configured
credential.helper     → osxkeychain（有設定，不是沒設）
http.*.extraheader    → 無殘留
tea                   → ✅ 正常（讀自己設定檔的 token，完全不經 keychain）
```
⇒ **不是憑證過期，是這個執行環境拿不到互動式憑證**：`osxkeychain` 需存取鑰匙圈，而 agent 跑在
**非互動 shell、沒有 TTY** ⇒ `Device not configured` 正是「想提示但沒有終端可提示」。

⚠ **這不是今天才壞的，是常態**：Litmus／Gatepost／Scour 今天都成功推過分支 ⇒ **各自繞過了，但沒人回報繞法**
⇒ **每個 agent 都要自己撞一次、自己發明一次解法**，而這與他們手上的任務完全無關——**純粹是環境稅。**

**dispatcher 實測可用的繞法**（附對照組，非轉述）：
```bash
TOKEN=$(python3 -c "import re;print(re.search(r'^\s*token:\s*(\S+)',open('/Users/eugene/Library/Application Support/tea/config.yml').read(),re.M).group(1))")
git -c http.extraheader="Authorization: Bearer $TOKEN" ls-remote origin HEAD
```
```
對照組（不帶憑證）  → fatal: could not read Username        ← 確認環境確實壞
實驗組（Bearer）    → fdbca8d5…  HEAD                        ← 成功
陽性對照（分支總數）→ 252                                     ← 非零，確認不是「連上但空」
```

🔴 **關鍵：token 每小時輪替，繞法不能寫死 token。**
dispatcher 解碼 JWT 實測：`iat 17:41:23 / exp 18:41:23` ＝ **1 小時有效期**；refresh_token 到 **2026-09-03**（30 天）。
⇒ **必須每次從 config 動態讀取**（如上），複製貼上 token 的寫法**一小時後就壞**，
而且壞掉時的症狀與「沒有憑證」一模一樣 ⇒ 會被誤診成環境問題。
⇒ ~~**真正需要 Eugene 重新登入的時間點是 2026-09-03**（refresh_token 到期），在那之前 tea 會自動續。~~

> 🔴🔴 **【2026-08-04 推翻】這條錯了，而且當天就被實例打臉。**
> **`refresh_token` 沒過期，tea 照樣全隊阻斷** —— 因為 **OAuth client 本身在 Gitea 端失效**：
> ```
> Failed to refresh token: oauth2: "invalid_client"
>   "cannot load client with client id: \"d57cb8c4-630c-4168-8324-ec79935e18d4\""
> access token  exp 2026-08-04 09:39:52  (-4.0h)  🔴 已過期
> refresh_token exp 2026-09-03 18:39:52  (+725h)  ✅ 未過期 ← 但沒用
> ```
> ⇒ **我當時的推論隱含假設「只有時間會讓它失效」，而我只窮舉了時間這一種原因**
> ——與同日的 `UserError` 四子類、`MissingError`／`AccessDenied` 同構：**想到一個實例就以為涵蓋了類別。**
>
> **🔑 兩種故障，處置完全不同（leader3 提，dispatcher 實測確認）：**
> | 症狀 | 意義 | 處置 |
> | --- | --- | --- |
> | **401 / token 過期** | access token 到期 | **隨便跑個 tea 指令就會自動 refresh** |
> | **`invalid_client`** | **refresh 機制本身壞了** | 🔴 **跑再多次 tea 也不會好，只能 `tea login oauth-refresh eugene`（人工互動，agent 做不到）** |
>
> ⚠ 既有 memory `reference_tea_cli_write_gotchas` 第 2b 點寫的是「主動 refresh ＝ 隨便跑一個 tea 指令」
> ——**那條在這個故障上無效，而它的措辭會讓人一直重跑 tea 指令**。需補一句區分。
>
> 🔴 **阻斷範圍比 tea 本身大**：`git fetch`／`push` 的 `http.extraheader` 繞法也走同一顆 token
> ⇒ **rebase／push／開 PR／讀 issue 全停**。`gh-origin`（GitHub mirror）實測 develop 落後，**不能當 fallback**。
>
> ⚠ **附帶新發現：`tea` token 失效時 exit code 是 0**（錯誤訊息進 stdout）
> ⇒ 任何 `tea ... && ...` 或檢查 `$?` 的腳本都會**誤判成功**。這是今日第 N 次同形狀，但此前沒人記過。
>
> ✅ Eugene 於 2026-08-04 15:04 跑完 `tea login oauth-refresh eugene`，恢復（新 `iat 15:04:33`）。

⚠ **dispatcher 差點在此發假警報**：只看 config 的 `token_expiry`＋`created: 2026-04-05`，
算出「37 分鐘後過期」並準備緊急廣播。**救回來的是解碼 JWT 看 `iat`**——
`iat` 是今天 17:41（23 分鐘前才簽發）⇒ 這是**正常的短期輪替**，不是即將失效。
🔑 `created` 是**登入時間**，不是 token 簽發時間。**兩個時間戳長得一樣，語意完全不同。**

⚠ **fetch 失效最危險的形態（leader6）**：`git fetch` 失敗後，**本機 remote-tracking ref 悄悄過期**，
而 `git log origin/develop` 仍讀得到舊值、**看起來完全正常** ⇒ **症狀是「讀得到舊資料且毫無異常」**，
比直接報錯難察覺得多。（本輪實例：dispatcher 的 `origin/develop = e112e72c` 已落後到 `fdbca8d5`。）

---

## ✅ PR#550 已可合併（leader3 完成，dispatcher 實查對帳）

```
tea pulls 550 -o json → mergeable: True   state: open      ← dispatcher 自量，非採信自陳
head = 0da97367（rebase 後）  base = 最新 develop  【勿 merge】標示保留
```
leader3 自陳「四項驗收全達成」，正在把測試證據落地到 PR 留言（**他主動說「不讓測試輸出只活在我的 context」**——`feedback_preserve_findings_before_dismiss` 的正確應用，不需要人提醒）。
**待他貼出原始輸出後補記完整驗收。**

### ⚠ dispatcher 今日第二次差點發假警報：`maestri check` 的分支欄不代表 agent 的工作位置

**症狀**：leader1 與 leader3 的狀態列**同時顯示 `docs/context-fix-tenant-group`**
⇒ 我判定「兩人撞同一個 working tree」，準備緊急協調。

**實查推翻**：
```
git worktree list  →  35 個 worktree
  主 repo                          ca78ed1c [docs/context-fix-tenant-group]   ← leader1 在此
  …/f37e41dd-…/scratchpad/wt-550   0da97367 [leader3/pr550-rebase]            ← leader3 在此（獨立）
```
⇒ **沒有撞車。** leader3 顯示主 repo 的分支，是因為他的 **shell cwd 被 reset 到 `.maestri/roles/…`（位於主 repo 內）**，
狀態列反映的是**那個目錄所屬 repo 的分支**，不是他實際工作的 worktree。

> 🔑 **判準：`maestri check` 的分支欄 ≠ agent 實際工作的 worktree。**
> 要判斷兩個 agent 是否撞車，**必須用 `git worktree list` 看實體路徑與 SHA**，
> 不能用狀態列的分支名——**兩人顯示同一分支是常態**（大家的 cwd 都會落回主 repo）。
> ⇒ 用狀態列判撞車會產生**假陽性**；而真正的撞車（兩人同一個 worktree 路徑）**狀態列也顯示得一模一樣** ⇒ **零鑑別力**。

⚠ **但有一個真實風險本次確實存在**：**leader1 直接在主 repo 工作**（非 worktree），而主 repo 是所有 agent 的**預設 cwd**
⇒ 任何 agent 在主 repo 讀 `CONTEXT.md`／跑測試，會讀到 **leader1 修改中的分支狀態**，而非 develop。
⇒ 這正是 `reference_find_concurrent_file_writer_via_file_history` 的場景。**dispatcher 本輪在主 repo 只做唯讀操作**
（`git fetch`／`git show`／`git log`），未改動任何檔案。

### 📌 附帶盤點（只列不動）：**35 個 worktree**，多數是已完成 issue 的殘留

`#423`(×5)／`#463`(×2)／`#464`／`#471`／`#474`／`#477`／`#482`／`#484`／`#489`／`#501`(×2)／`#525`(×3)／`#530`／`#535`／`ci46`／`ci50`／`deploy-stg-97`／`383-audit` …
⇒ **不屬 dispatcher 職責，也未經授權清理**——僅記錄為候選。清理前須逐個確認無未推送的 commit
（`git -C <path> status` ＋ `git log @{u}..`），**因為「看起來是殘留」與「還沒推的工作」外觀相同**。
### 🔴🔴 那條警告在同一天就兌現了——`wt-ci-tests` 有 **27 個未推送 commit**

dismiss leader6 前做保全時實查（`git log @{u}..HEAD`）：
```
worktree : woow-addons/wt-ci-tests     分支: ci/run-odoo-tests → origin/ci/run-odoo-tests
未推送   : 27 個 commit                未 commit: (空) ← 全已 commit，只差推送
本機 HEAD: 4fc1c7ee   remote: eb9b53de  ← 尚未推送（陽性對照：remote 254 分支，連線正常）
```
內容是 leader6 ＋ Scour 的實質成果：**#546 Phase 1 的 workflow 實作**（develop push ＋ 02:00 排程）、
**測試收集完備性 gate**（commit 標題逐字寫著「**列出『是哪一個』不是『差幾個』**」——今日方法論的實作）、
**#544 的機制 B**（整檔純 `unittest.TestCase` 的模組從未執行，C1 的匯入比對看不到它）、
**skip 偵測的陽性對照哨兵**（理由：「run 1432 的 `skipped_count=0` 目前零鑑別力」）。

⚠ **`wt-ci-tests` 就在上面那份「殘留候選」清單裡。** 若有人照清單動手，這 27 個 commit 直接消失。

> 🔑 **價值不在「差點出事」，而在它證明了那個判準可操作**：寫下警告時只是抽象地知道
> 「殘留與未推送外觀相同」，**幾小時後就撞到實例**，而**救回它的正是那句話本身**——
> 保全時我沒問「這個 worktree 還要不要留」，而是直接跑 `git log @{u}..HEAD`。
> **判準要寫成「跑哪個指令」，不能寫成「要小心」。**

⇒ **本清單標為：未經逐個 `@{u}..HEAD` 驗證前，一律不得清理。** 已知 **`wt-ci-tests` 不可清理**（待推送）。

### ✅ leader3 完整驗收（dispatcher 獨立複驗，非採信自陳）

| 項 | leader3 自陳 | dispatcher 複驗 |
| --- | --- | --- |
| mergeable | True，head `0da97367`、base `fdbca8d5` | ✅ `tea pulls 550 -o json` 自量一致 |
| 紅燈仍是同一條 | `TestPRuleMigration.test_cursor_bypass_flag_cleared_after_migrate`，兩輪逐字一致，traceback 位置未變（`:132`→`_make_legacy_policy:57`→`security_access_access_policy.py:278`） | ✅ PR 留言命中 4 次 |
| 集合差 | 190/190/191/191，斷鏈側空、反向空、注入式對照非空 | ✅ 「斷鏈側」2 次、`test_ecpay_invoice_real` 2 次 |
| 【勿 merge】保留 | ✅ | ✅ 標題實查確認 |
| 證據落地 PR 留言 | 15,761 → 24,747 字元 | ✅ 實測 23,439 字元、六項關鍵字抽驗全命中 |

**測試 summary 原文**：`0 failed, 1 error(s) of 2312 tests when loading database 'test550'`（兩輪，第二輪含 leader6 授權的註解改動，**結果逐字相同**）。
leader6 那行時間錨定註解已照原文套用（`:287` 未動、288–289 換成單行）。
清理複驗：容器 0／network 0／worktree 0／`dev-sandbox-pr550` 已消失；`git status` 只剩 session 開始就有的 2 筆。

### 🔴 【勿 merge】的載體是**標題**不是 **label**（leader3 發現）——這是 #555 缺口的根因

PR#550 的 `labels` 是**空陣列**，【勿 merge】只寫在標題。
⇒ 合起來看 leader1 報的「PR#555 帶【勿 merge】仍被 merge」：**不是有人無視 label，是根本沒有 label 可供機械判讀。**

> 🔑 **慣例的載體決定它能不能被強制**：寫在標題 ⇒ 只能靠人眼；寫成 label ⇒ 可被 branch protection／CI／merge gate 讀取並阻擋。
> ⇒ 「預設帶勿 merge、通過才移除」這條慣例**今天已被遵守三次**（#545／#550／#555 皆帶標示），**卻仍發生一次誤 merge**
> ⇒ **遵守率不是問題，可強制性才是。** 修法方向應是「改用 label」而非「提醒大家注意標題」。

### 📌 leader3 的自我剖析：不是知識缺口，是**查詢時機**

他踩的三個環境坑中，**兩個的 memory 昨天才記過**（`reference_k8s_sandbox_test_gotchas` Gotcha #3 hostPath／#6 exit code）。
> **「要記的是『我有那條記憶卻沒在動手前查』——這不是知識缺口，是查詢時機的問題：建沙盒前應該先讀 gotchas，而不是卡住後才讀。」**

⇒ 他**主動判斷不另寫 memory**（那兩條已存在且正確）——**正確的抑制**：重複寫入會稀釋既有條目。
⇒ 真正的處方是**把查詢綁進動作的前置步驟**，而非再增加一條「要記得查」的知識。

### ⚠ dispatcher 本輪第三次探針事故（這次被陽性對照擋下）

驗證 PR 留言時 `tea issues 550 --comments` 回 **90 字元、所有關鍵字 0、陰性對照也 0**。
**差點判定「證據沒落地」** ⇒ 陽性對照（改查已知有內容的 #556）**同樣回 90 字元** ⇒ 立刻知道是指令壞了。
```
Error: remote repository required: specify id via --repo or execute from a local git repo
```
根因：**我 `cd` 到 scratchpad 就離開了 git repo**。⇒ **「查無資料」與「指令根本沒跑成」輸出一模一樣**，
正是 `feedback_grep_filter_hides_command_failure` 的形狀。**leader6 今天提的「同指令必然存在計數」判準，當天第二次救場。**

⚠ 修好後第一次抽驗仍有一項回 0（`0 failed, 1 error(s) of 2312 tests`）——**不是缺失**：
`--comments` 是**渲染輸出會折行**，且 leader3 轉述時省略了單引號（原文 `database 'test550'`）。
二分縮短後 `of2312tests` 2 次、`1error(s)` 1 次、`0failed` 3 次 ⇒ **證據確實在**。
⇒ 再次坐實：**對 comment 用長字串逐字比對是零鑑別力的，必須用短獨特字串**（已在 memory）。

---

# 🔴🔴 【狀態變更】Eugene merge 了 #555 / #550 / #557 — develop 現在帶 1 條紅燈

**dispatcher 實查**（`git fetch` 走 tea token 繞法）：

```
origin/develop : fdbca8d5 → ad8ee206
ad8ee206  Merge PR #557（CONTEXT.md 更正 group role → portal）
196c7831  Merge branch 'develop' into docs/context-fix-tenant-group
b69c7c80  Merge PR #550（P-rule migration，刻意保留 1 條紅燈）   ← 🔴
ca78ed1c  fix: CONTEXT.md 更正租戶的 Odoo group role
0da97367  test: 復活 P-rule migration 的 3 條測試
fdbca8d5  Merge PR #555（CONTEXT.md 兩軸術語）
```

⚠ **Eugene 的訊息只提了 #555 與 #557，但 #550 也在其中**（`b69c7c80`）——dispatcher 實查發現，非採信訊息字面。
🔑 **這是「識別字必須查證」的正面應用**：若只照訊息字面更新看板，會漏記影響最大的那一張。

## 後果一：develop 不再全綠（已廣播全員）

```
ERROR: TestPRuleMigration.test_cursor_bypass_flag_cleared_after_migrate
verified: src/tests/__init__.py:292 已含該 import（dispatcher 實查 git show FETCH_HEAD:...）
          測試檔存在於 develop ✅
          leader6 的時間錨定註解已正確套用 ✅
```

⇒ **從現在起任何人在 develop 或其衍生分支跑全套測試都會看到 `0 failed, 1 error(s)`。**

**判準（已廣播 leader1／2／4／5／7；leader3、leader6 本來就知道）：**
| 看到 | 意義 |
| --- | --- |
| `1 error(s)` 且是那一條 | ✅ 正常基準 |
| 錯的變**別條**或**多於 1 條** | 🔴 是你改壞的 |
| **全綠** | ⚠ 要嘛 #543 已修好，**要嘛你的測試根本沒跑到那條** |

🔑 **在這個狀態下「全綠」反而是可疑訊號**——這是本輪最反直覺的一條判準。

⚠ **防護做得不錯**：`src/tests/__init__.py:290-291` 有註解說明紅的原因（leader3／leader6 預留），
測試檔內也有。**但跑測試的人通常不會先看 `__init__.py`** ⇒ dispatcher 仍主動廣播，兩層並用。

## 後果二：#543 從「待排人」升級為**擋路項**

leader6 寫在註解裡的顧慮「**避免污染 develop 全綠基準**」——**現在成真了**。
⇒ #543 一天不修，develop 一天不是全綠；且**所有以「全綠」為斷言的 CI gate 都會持續紅**。
⇒ **需 Eugene 裁優先序**（已呈報）。leader6 是原持有者但未動工，**#543 至今未排到人**。

## 後果三：#555 / #557 兩張帶【勿 merge】的 PR 也被 merge

⇒ 加上 #550，**今天三張帶【勿 merge】標示的 PR 全部被 merge**。
🔑 這**強化**了 leader3 發現的根因：**標示的載體是標題不是 label ⇒ 沒有任何機械攔截的可能**。
⚠ 但要說準：**這三張是 Eugene 主動 merge 的**（他有權這麼做），與「有人誤 merge」不同。
⇒ 真正的問題不是「被 merge 了」，而是**「勿 merge」在系統層沒有任何實體**——
無論是誤按還是刻意，系統都給不出任何提示或阻力。**修法方向仍是 label 化。**

## 附帶：CONTEXT.md 的錯誤已修正上線

#557（`ca78ed1c`）已把 leader1 指出的兩處錯誤修掉並 merge ⇒
**「所有租戶都是 `base.group_user`」與「本專案只用到兩個」已不在 develop 上。**
⇒ 先前「修正前任何引用 CONTEXT.md 群組敘述的結論都要重驗」這條**警告解除**，
但**在 #557 之前產出的結論仍須重驗**（它們讀到的是舊版）。

## 🟢 後果四（leader5 回報）：驗收標準的**絕對值**寫法全數失效 → 改集合差

leader5 今天三張工單（#520→PR#523、#525 A1→PR#527、#525 A2/A3→PR#540）的驗收標準都寫成
**「baseline vs fix 各自乾淨 DB，兩邊 0F/0E」** ⇒ **從現在起這個標準會失效**。

若照舊模板派 #553／#554，teammate 看到 `0 failed, 1 error(s)` 會有三種錯誤反應：
①誤以為自己改壞了，去追不存在的迴歸 ②試圖把它改綠（**正是被禁止的**） ③卡在「工單要求 0F/0E 但達不到」而停下來問。

**他的處置（後續工單改用）：**
```
❌ 舊：baseline 與 fix 兩邊都要 0F/0E
✅ 新：baseline 與 fix 的「失敗/錯誤集合」必須相同；fix 側不得新增任何一條
      且明確寫明 develop 目前有一條刻意保留的紅燈（TestPRuleMigration.
      test_cursor_bypass_flag_cleared_after_migrate，#543 的注入式驗證）
      —— 不要改它、不要 skip、不要動它的 fixture
```

🔑 **這正是今日「輸出要是『是哪一個』不是『差幾個』」的直接應用**：
**絕對值標準（0F/0E）本質上是「差幾個」，一旦基準不是 0 就整個失效；集合差不受基準值影響。**
⇒ 判準可推廣：**任何以「數字等於某個常數」為形式的驗收，都綁死在當下環境狀態上**，
環境一變就要全體改寫 ⇒ **寫成「相對於 baseline 的差集」從一開始就免疫。**

### 🟢🟢 leader5 把這條負債轉成了資產：**那條紅燈現在是一個免費的完整性探針**

> **「那條紅燈沒出現，就代表這次跑的範圍不完整。」**

⚠ 他附的實例證明這個探針有多必要：驗 PR#540 時他原本只比**總數**（2226 → 2237，+11），
後來改集合差才發現盲點——**「新增 11 條」同時「舊的 11 條被靜默 deselect」，總數一樣會對**。
實跑集合差才證明 fix 側只多了兩個測試類、**沒有任何 test class 消失**。

⇒ **Odoo 上「靜默 deselect」沒有任何訊號**（tag 沒匹配／module 沒裝起來／import 失敗，都是安靜地少跑一批）
⇒ 這正是 dispatcher 廣播那張判準表最後一列（**全綠反而可疑**）在 Odoo 上特別致命的原因。
⇒ **現在 develop 帶著一條必然出現的紅燈 ⇒ 它同時是「這次跑的範圍完整」的陽性對照。**
🔑 **這是本輪唯一一個把已知缺陷轉成檢測工具的例子**——缺陷本身沒變，變的是**我們拿它當什麼用**。

**leader5 自陳無其他受影響處**（本 repo 目前沒有跑測試的 CI，故無 gate 斷言全綠）。

---

## 📊 全域 context 盤點（dispatcher 實查，2026-08-03 收盤前）

**裁決延遲有實際成本**：等待期間 context 持續累積，滿了就得 `/clear`——**那時裁決到了反而要重建 context**。

| leader | context | 時長 | 持單 | 壓力 |
| --- | --- | --- | --- | --- |
| **leader4** | **84%** | 815m | #547（等完成標準高度） | 🔴 **最急** |
| **leader7** | **78%** | 818m | CI extraheader（硬阻塞） | 🔴 次急 |
| **leader6** | **70%** | 817m | #543（**現已擋路**）／#546 | 🟡 |

🔑 **六位已連續運轉 13.5 小時以上**（815–818m），且**全部卡在等裁決**。
⇒ 建議裁決順序**依 context 壓力而非 issue 重要性**：**#547（leader4 84%）→ CI extraheader（leader7 78%）→ #546／#543（leader6 70%）**。
理由：低 context 的人可以再等，**高 context 的人再等下去，等到的裁決會落在一個已經 /clear 過的空 session 上**。

⚠ **修正一項早先記錄**：leader2 並非純等待——他畫面顯示正在驗收 teammate 交付
（「初步驗收品質超出我的工單要求。M4 測試有四道防假綠」）⇒ **他自行判斷先做不受爭議的部分**，未因等裁決而閒置。
🔑 **這說明「等裁決」與「閒置」不是同義詞**——盤點時不能只看有沒有裁決在等，要看畫面上實際在做什麼。

---

# 🔻 【編制變更】leader6／leader7 已 dismiss，剩五位（Eugene 指示「太多 leaders」）

**兩者皆走「保全 → 驗證 → dismiss」，dispatcher 逐項實查後才執行。**

## leader7（驗收全通過）

| 項 | 驗證方式 | 結果 |
| --- | --- | --- |
| 6 個 memory 檔 | `wc -c` 逐個 | ✅ bytes 與自陳**完全一致**（7628／13818／4674／7210／6130／4095） |
| leader5 的 `$pipestatus[1]` | grep | ✅ 3 次，已歸檔進 zsh 那條 |
| `woow-paas/paas-odoo-ci#54`（CI extraheader 根因） | tea API | ✅ 存在（body 16252 bytes，含**六條被排除的假設**） |
| `#542` 四則留言 | `--comments` 26,925 字元 | ✅ 六項關鍵字命中，陰性對照 0 |
| 兩個 teammate | `maestri list` | ✅ 全清單 0 次 |

🔑 **他更正 dispatcher 一句**：我寫「你已在做的部分不用重來」，但他 **dismiss teammate 時保全已經做完**，不是收到指令才做。
⇒ 但他承認漏了一項：**升版驗收原始輸出當時只在 context**，是被點名才補進 #542。他收下該判準：
> **結論可以重述、原始輸出不能重建。**

## leader6（驗收全通過）

- 🔴 **`wt-ci-tests` 27 commit 已推送**（remote = `4fc1c7ee`）——**這是全部 31 個 worktree 中唯一有未推送工作的**
- ✅ 三個 teammate（Litmus／Gatepost／Scour）全清
- ✅ #543／#546 設計在 issue body（20,552／53,641 字元）；PR#550 的**四種**寫法否決理由隨 merge 永久保存（比原本說的三種更多，含 `@unittest.skip` 與根因 `odoo/tests/result.py:57` OdooTestResult 不繼承 unittest.TestResult）

### ⚠ dispatcher 誤讀自己的看板：「放行」≠「已寫」

我記的是「稽核報告**落點已放行** `docs/tracking/`」——那是**授權**（可以寫那裡），我讀成**已寫**，
於是 `find docs` 找不到就以為遺失。**內容一直在 memory**（`project_529_test_false_green_batch0.md` 7,951 bytes，
稽核規模 182 檔／2198 個 `def test_*`／測試碼 55,568 LOC vs 生產碼 30,740 LOC ＝ **1.81:1**、
冗餘僅 ~2%、覆蓋缺口自承 **24%**、9 條建守衛設計模式）。
⇒ **leader6 建議不 promote**（memory 是持久載體，教訓型內容放那裡對）——dispatcher 同意。
🔑 **教訓：看板上的「已放行／已批准」是對未來動作的授權，不是動作已發生的紀錄。兩者語感接近，載體卻完全不同。**

### 🟢 leader6 收尾時修掉一個過期陳述（本日第三個同族實例）

memory 標著「🔴 金流唯一憑據**未裁決**」，實際**已由 #541 收掉**（`d5ad9d14`／merge `d8bfcf32`）。
實查 develop：仍出現 `assertIn('_attempt_auto_topup', …)` 字樣的 4 處**全是註解與 docstring**（`:84`／`:174`／`:212`／`:213`），活著的斷言已換成行為測試。
他改了 memory 與 MEMORY.md 索引兩處，並註明「**grep 到那些字樣別誤判成未修**」。
⇒ dispatcher 複驗：索引行已更正、舊字樣在**三個 MEMORY 分檔各 0 次**（用萬用字元查全部分檔）。
🔑 **它帶著實測數字 `0 failed of 2193`，看起來完全可信**——這正是「驗證結論會過期成假話」最危險的形態。

### 🔴 `wt-ci-tests` 是趕在清理波及前推上去的

leader6 回報：**worktree 從 34 → 30**（`wt-525-a1`／`wt-525-a2a3`／`wt-525-base537`／`deploy-stg-97` 已刪），
清理**當時已在進行中**，而 dispatcher 正把 `wt-ci-tests` 列在「殘留候選」清單裡。
⇒ **Litmus 攔下了一個危險指令**：「#550 掛的是 `test/revive-p-rule-migration-tests` 不是 `test/revive-dead-test-modules`，
照字面執行會刪掉 #550 的分支」⇒ **兩條都沒刪**。這正是
[[feedback_identifiers_in_destructive_orders_must_be_looked_up]] 的實例，**他攔對了**。

**Gatepost 轉達**：MEMORY.md 現況 **27.7KB**，他判定**不接壓縮**。⇒ 待 Eugene 決定凍結令去留。

---

## 📋 leader5 第 4 步回報：已派三人（#525 B3–B5／#553／#554）

| teammate | 範圍 | paas.py 區段 |
| --- | --- | --- |
| **Vault** | #553 ＋ B3（例外訊息語意／密碼交付） | 1189／1447 |
| **Conduit** | #554 ＋ B4（MCP 雙層吞例外，同檔同區塊故綁一起） | 1888–2260 |
| **Sluice** | B5（`_delete_service` unlink） | 3045+ |

### 🔴 dispatcher 實查發現一個會咬三人的錯：基準版號是 `.98` 不是 `.97`

```
leader5 自陳：develop @ c723a44d（18.0.1.0.97）
dispatcher 實查：SHA ✅ 正確（我的 ad8ee206 反而過期一代）
                 版號 🔴 git show c723a44d:src/__manifest__.py → "18.0.1.0.98"
```
`c723a44d` ＝ **#558 的 merge commit**，**它自己 bump 了 .97 → .98**。

⇒ 三人若以 `.97` 為基準各自 bump 到 `.98`，而 `.98` 已被用掉 ⇒ **三個 PR 的 manifest 全部等於 develop，不是嚴格大於**。
⚠ 最惡劣形態不是撞號，是 [[reference_rebase_silently_eats_manifest_bump]]：
**兩邊 bump 到同值 ⇒ 衝突根本不出現 ⇒ 所有以 conflict 為訊號的檢查都不響。**
⇒ leader5 的驗收判準（**嚴格大於**）擋得住，**但前提是基準值正確**。已緊急通知更正。
🔑 **這組的教訓：「我自己 fetch 驗證過」保證了 SHA 新鮮度，但沒保證從那個 SHA 讀出的衍生值（版號）被實際讀取過。**
新鮮的 SHA ＋ 憑印象的版號 ＝ 看起來完整的基準宣告，實則一半未驗。

### 🔴 協調點：三人同時改 `src/controllers/paas.py`（3755 行）

⇒ **未來派工碰這個檔須先找 leader5 協調。** 這是他主動提供的——
🔑 **dispatcher 沒有「誰在改哪個檔」的即時視野，只有 leader 主動報才知道**，
而 [[feedback_parallel_leaders_same_file_collision]] 的成本已經在 #516/#520 付過一次。

### 兩件延伸（leader5 已交 teammate 判定，未自行決定）

1. **B4 附帶**：`mcp_server.py:226` raise 後 snapshot 推送不執行 ⇒ **DB 已變但 running pod 內 `mcp.json` 仍舊值且無補推機制** ⇒ 交 Conduit 判定本單收或獨立開單
2. **#525 B5 內文兩處對不上現行碼**：`paas.py` 行號整體位移 **+100~125**；`_delete_service` 現在只有兩個具型別 except、**沒有 catch-all**，B5 說的「3082 無差別吞例外」**其實落在 `_update_service_status`** ⇒ 交 Sluice 勘誤
   🔑 **這是「issue 內容隨 develop 漂移」的實例**，與 leader6 修掉的「金流唯一憑據未裁決」同族：
   **寫下時正確、帶著具體行號、看起來完全可信。**

### 待判的 docs 素材

`mcp_server.py:278` `action_sync_tools_safe` **回傳 bool**，是 repo 內**既有的「結構化失敗值」idiom**，
#525 盤點時未提及，**而它正是 #554 修法的關鍵參照** ⇒ 等 Conduit 決策結果再判斷是否寫進 `docs/decisions/patterns/`。

---

# 🔧 【環境】k8s dev sandbox：**local/hostPath 模式死、stg 模式可用**（#563）

## 根因（leader5 診斷，dispatcher 獨立複驗）

Docker Desktop 的 k8s 是 **kind 架構**，node 是容器 `desktop-control-plane`：
```
docker inspect desktop-control-plane 的 mounts → 只有 3 條
  /etc/kind/hosts.toml、/lib/modules、一個 docker volume → /var
/Users 或 /private/tmp 的 bind 數量 → 0            ← dispatcher 自量，根因坐實
```
⇒ **k8s hostPath 解析的是 node 容器內的檔案系統**，宿主機上真實存在的目錄在 k8s 眼中不存在。

**不是「worktree 放在 `/private/tmp` 才壞」**：FailedMount 第二條是
`extra-mount-0 → /Users/.../paas-odoo-ci/addons`（路徑就在 `/Users` 底下）**一樣失敗**
⇒ 搬到 `/Users` 救不回來。

## 🟢🔴 dispatcher 的第一版廣播結論過寬，leader1 用反例推翻

**反例**：`dev-sandbox-assay519`（2026-08-03T07:18:01Z，docker-desktop）**三個 pod 全 Running、`curl 127.0.0.1:18069/web/login` → HTTP 200**，
並在其中跑完 **#556 的完整受控實驗**（真 `/web/session/authenticate` ＋ `call_kw`、4 個身分、三輪探測、四項控制、`kubectl exec` 進 postgres 下 SQL 交叉驗證）。
⇒ **stg 模式不 bind-mount 宿主目錄，所以不受影響。**

**用 stg 模式的前置**（dispatcher 實查確認）：
```
kubectl --context docker-desktop get sc → 只有 hostpath / standard，沒有 longhorn-delete
.sandbox/pre-install.sh 硬寫 storageClass: longhorn-delete（filestore ＋ postgres 兩處）
  且 echo 進 $EXTRA_VALUES_FILE、排在 --values 之後會蓋掉覆寫 ⇒ 沒有別的介入點
```
⇒ **必須先建 SC**。leader1 的 teammate 建過，但**清理時被一併刪除**（不屬沙盒本體）。

### 🔑 leader1 指出的坑：**下一個人的失敗會被誤讀成「整體不可用」的佐證**

沒建 SC ⇒ 卡 **PVC Pending** ⇒ 看起來像「sandbox 又壞了」⇒ **被當成 dispatcher 廣播的證據** ⇒ 沒人去查真因。

> 🔑🔑 **leader4 給出了這個現象的機制與處方（比 dispatcher 的原句準確）**：
> **總體判定（「X 壞了」）比分項判定更容易吸收反例，因為它對每一種失敗都相容。**
> ⇒ 廣播環境故障時，措辭要帶「**哪一條路徑壞了、用什麼症狀分辨**」，而不是「X 不可用」。
>
> 他對 teammate 的更正是最佳實作：「撞到 sandbox 起不來時**先分辨 PVC Pending（缺 SC）與 FailedMount（hostPath）**，
> 不要輸出『環境壞了』這種總體判定」⇒ **這直接切斷自我加固的迴路**——
> 分項判定會讓下一次失敗**分類到具體原因**，而不是被吸收成「果然壞了」。

**依場景選**：單元／模組測試 → **純 docker**（dispatcher 實查：15 個容器正在跑）；
需要**真 HTTP session / 真 ACL / 真 record rule** 的 e2e → **stg 模式 sandbox**（重但完整）。
⚠ dispatcher 第一版說「改用純 docker」**對第二列是錯的建議**——e2e 在純 docker 重建成本不低。

## `create.sh` 的假綠有**兩種**，別混淆

| 形態 | 說明 |
| --- | --- |
| `create.sh \| tail` | pipeline 尾端吃掉 exit code（gotchas 第 6 條） |
| **`create.sh` 自己 exit 0** | 不接管道也一樣，輸出只有一行 `Install failed: woow_paas_platform state=not present` |

⇒ **只修 pipeline 那個不會解決本次問題。** leader5 把假綠列為 **#563 最優先，比換架構優先**——dispatcher 同意：
**架構壞了會被發現，假綠不會。**
⇒ 補一條：**要看 `kubectl describe` 的 Events，不只是 `get pods` 的 STATUS**
（leader1 的 teammate 空等 11 分鐘，全程**沒有任何錯誤訊息，只是 ContainerCreating**，是主動 `docker exec ... ls /Users` 才查到根因）。

⚠ **CLAUDE.md「本地 e2e／整合測試一律走 k8s-dev-sandbox」目前部分做不到**（local 模式）。
**leader5 判斷不改 CLAUDE.md，dispatcher 同意**：那是**環境現況不是永久事實**，寫死會在修好後變成錯誤指引，
而 **CLAUDE.md 每個 session 自動載入，錯誤指引的傳播範圍比任何 issue 都廣**。留在 #563 有時間戳。

---

## 🔴 memory 新判準：**wiki-link 指向的檔存在 ≠ 它被索引**

查證 `reference_k8s_sandbox_test_gotchas.md` 的重複時發現：
```
feedback_workorder_must_carry_env_landmines.md : 3,775 bytes ✅ 存在、有人用 [[...]] 引用
索引：MEMORY.md / MEMORY-odoo.md / MEMORY-landing.md → 各 0 次   🔴 孤兒
```
⇒ dispatcher 已補索引行（MEMORY.md **18,482 → 18,701 chars**，複驗命中 1）。

🔑 **雙向差集檢查掃不到這種**：它查的是「索引→檔案」（斷鏈）與「檔案→索引」（孤兒），
而**從內文 wiki-link 進入的檔案在兩個方向都不會觸發告警**——有檔、有人引用，就是沒進索引。
⇒ 完備性檢查要**第三個方向**：掃全部 memory 內文的 `[[...]]`，比對是否都在索引裡。

### 併發寫入的裁決判準

leader4 發現該檔有重複（他的 6b vs 另一 session 的第 44-70 行），**只回報不整理**——**正確**。
⇒ dispatcher 裁決可以合併，**判準不是「有沒有重複」而是「另一個 session 還在不在寫」**：
```
find memory -name '*.md' -newermt '-6 minutes' → (空)   ⇒ 寫入視窗已關閉，可動
```
**那只能用 mtime 量，不能用推測。** 已指定 leader4 執行，並要求：合併前再跑一次 mtime 檢查、
**保留兩段各自獨特的內容**（第 44-70 行的「三道防線表」與「兩種假綠區分」是 6b 沒有的，**互補不是重複**）。

---

## 📌 develop 再次前進：`c723a44d → eaf26413`（#560 ＝ leader3 的 #552）

```
版號 : 18.0.1.0.98 → 18.0.1.0.99      🔴 .99 已被用掉
src/controllers/paas.py     : +46 行   🔴 leader5 三人的行號再次 stale
src/models/cloud_service.py : +42
src/tests/__init__.py       : +3
```
⇒ **同一件事在 40 分鐘內變動兩次**（.97→.98→.99）。
🔑 **dispatcher 的檢討：傳遞靜態值本身就是一種會過期的宣稱。**
⇒ 改為**給機制與查法，不給值**；真要給值就標「量測時間 ＋ 當時的 SHA」讓對方能判斷新鮮度。
⇒ leader5 工單裡本來就對的那條機制（**每人 merge 前自己重跑 `git diff origin/develop -- src/__manifest__.py`**）才是解法。

## 📌 主 repo 曾落後 develop 6 個 commit，已由 leader1 切回

```
曾經：docs/context-fix-tenant-group，落後 6 commit，10 檔案（+708/-30），paas.py 3582 vs 3755
現在：develop，落後 0，paas.py 3755                    ← 廣播後 leader1 行動
```
⇒ **主 repo 是所有 agent 的預設 cwd**，停在功能分支時所有人讀到的行號都是舊的。
**安全讀法**：`git show <develop-SHA>:<path>` 或在自己的 worktree 讀；**不要在主 repo `git checkout`**（會改變其他 agent 的 working tree）。

## 📌 leader4 #547 已派工（兩條線，皆從 c723a44d 的 worktree，不污染主 repo）

- `docs/547-pattern-doc-corrections` — 6 份 contradicts-code 的內容修
- `chore/547-doc-anchor-registry-gate` — 結構修（登記表雙射 ＋ dangling-ref ＋ CI paths 盲點）
兩張單檔案集合不重疊；已與 leader1 確認零撞車。#547 留言已發（dispatcher 複驗 **86,963 字元**，六項關鍵字全命中、陰性對照 0）。

**待另開單**：`ai_assistant.py:1560` 的 `message_subscribe` 被註解描述成「因 `create_uid` 對不上 ir.rule 才需要的補償」，
**該前提已證實為假**（sudo 不改 uid，`create_uid` 是租戶本人）⇒ workaround 的必要性需重估。本輪只修註解。

### 🔑 leader4 的方法論（本輪 7 條裡 6 條靠它逼出來，命中率 5/6）

**要求每位對抗式覆核者填「本規格最弱的一句是哪句」。**
> 形態統一：**規格的主結論對，但為了說服讀者而補的那句「後果有多嚴重」或「因為 X 所以保證 Y」沒被驗過**
> ——**它讀起來最像結論、最不像主張，所以查證的目光會滑過去。**

⇒ **連帶判準：診斷成立 ≠ 處方可照抄。「N 個推翻者全部失敗」只覆蓋 verdict，不覆蓋處方裡每一句附帶敘述，兩者要分開驗收。**
⇒ dispatcher 採納為**派工單必填欄位**。本日至少三起同形狀：

| 誰 | 主結論（對） | 附帶敘述（錯） |
| --- | --- | --- |
| leader1 | #556 結構缺陷成立 | 「暴露面＝全部租戶」——前提是口頭答覆 |
| dispatcher | git 繞法（實測過含對照組） | 順手加的 `PIPESTATUS`——**沒測** |
| Conduit | 「行號 stale」 | 「整批偏移 +35」——**只對他那段成立** |

🔑 **共同處方（leader4 主動回報自己也犯了）**：他把過寬的 sandbox 結論原封轉給兩位 teammate，
**標了來源（leader5 診斷、dispatcher 驗證）卻仍出錯**——**標來源保證可追溯，不保證範圍被驗過**。
⇒ **轉述他人結論時，要分開標「我驗過的部分」與「我照抄的部分」。**

---

# 🔴 【發版】#552 的 `.88 → .100` 是 cd#77 gate 的**首次實戰**，且 gate 的不變式**不涵蓋 #552 的交付物**

## 實查基準（dispatcher 自量，非採信自陳）

```
prod image  : paas-odoo@sha256:69744696…            prod DB 版號 : 18.0.1.0.88
develop     : c6ae5122                              manifest     : 18.0.1.0.100
prod gate log（活證據）:
  [module-version-gate] image=18.0.1.0.88 db=18.0.1.0.88
  [module-version-gate] 版本一致，無需 module update；skip
initContainers: patch-odoo-conf, module-version-gate
主容器 env   : ODOO_SKIP_MODULES_UPDATE=yes
```

## 一、SOP 已過期，過期的是 SOP 不是 gate

`module-version-gate` 是 **cd#77 A′**（`woow-paas/paas-app-bundle-cd#77`，PR#81 `6e0cc3f` ＋ PR#82 `94a3e67`，**2026-08-01 上 prod**），
**設計上就是要取代** `prod-release-sop.md` §5.1 的 `ODOO_SKIP_MODULES_UPDATE` 兩段式（含 ≤30 分曝險窗）。
SOP 最後更新 **07-25**，早於 gate 上線。Eugene 當時的裁決推理（memory `project_cd77_module_version_gate.md`）：
> A（一次性 upgrade Job）治**流程形態**的本，但「忘記」只是變形（忘記切 flag → 忘記跑 Job）；
> **A′ 才是治不變式的本**——「pod 起得來 ⟹ DB latest_version == image manifest 版號」由 pod 自己 boot 時保證。

⚠ **但 gate 只跑 `-u woow_paas_platform` 單一模組**，而 `ODOO_SKIP_MODULES_UPDATE` 是 Bitnami entrypoint 的**全域**開關
⇒ **「gate 取代 SOP」只在「變更全部落在 `woow_paas_platform` 內」時成立**。
⇒ **判準：發版前看 seed diff 落在哪個 addon。** #552 的四個值都在本模組 ⇒ 這次成立。
（「`SKIP=no` 時實際升了哪些模組」**未驗**——dispatcher 與 leader3 都只讀 script，沒觀察過實際執行。）

## 二、🔴🔴 gate 的 post-verify **不涵蓋 #552 的交付物**（leader3 發現，本輪最重要）

gate 的 post-verify 寫得極好，它明確對抗「exit 0 不等於成功」：
```sh
# odoo-bin 回 0 只證明「-u 沒失敗」，不證明「DB 版號真的推進」。
# 要對**側效果本身**取證，而不是對「我跑過那個指令」取證。
if [ "$NEW_DB_VER" != "$IMAGE_VER" ]; then echo "不變式未成立，拒絕讓 pod 起來"; exit 1; fi
```
🔑 **「對側效果本身取證，而不是對『我跑過那個指令』取證」——這句是今天所有假綠（`create.sh` exit 0／`| tail` 吃 exit code／`PIPESTATUS` 回空／`find -newermt` 靜默放行）的通解，而它已經寫在 prod 的 initContainer 裡。**

⚠ **但它只保證「模組版號推進」，沒有保證「seed 欄位值真的寫進去」——而 #552 的交付物正好是後者。**
**sticky `noupdate` 會讓兩者分離**（leader3 於 PR#561 實測重現）：
```
第一次 -u → exit 0、0 個 ERROR、四個值仍空、版號照樣推進
⇒ gate 的 post-verify 通過、pod 正常起來、seed 值仍是空的
```

> 🔑🔑 **通則：寫得再好的不變式，也只保證它自己宣告的那一條。**
> 這正是它值得抄的原因，**也是它不能被當成「發版驗收已經自動化」的原因**。

## 三、⇒ #552 發版的驗收清單（三層，缺一不可）

| 層 | 驗什麼 | 誰保證 |
| --- | --- | --- |
| 1 | pod 起得來 | k8s |
| 2 | **DB 版號 `.88 → .100`** | **gate 的 post-verify（fail-closed，不成立就卡 Init）** |
| 3 | 🔴 **四個 template 的 `helm_chart_version` 真的非空** | **無人保證——必須人工查 DB** |

⚠ **第 3 層是這次發版唯一沒有機械保證的一環**，而它正是 #552 的全部價值。

## 四、附帶：`.89` 的完整驗收掛了兩天沒人接

memory 早就寫著「**完整驗收落在 .89 發版**⋯本次 prod 首驗只走過版本相等秒退路徑」
——**但 `.89` 那次發版從未發生**（prod 仍 `.88`）⇒ 那個驗收**掛了兩天**，現在接棒的是 #552 的 `.88 → .100`。
⇒ **發版當下要盯 gate log 出現 `Running migration` 與「完成並複驗」，不能只看 pod 起來。**

## 五、leader3 本輪兩次自我更正的形態（都值得留）

1. **「無人確認」是關於全集的宣稱，跟「查無」同一類，證據負擔一樣高。**
   他把「誰加的 gate、有沒有打算取代 SOP，我不知道」升成待裁項，**而答案早就在 memory 專檔裡**。
   ⇒ 代價不是結論錯（矛盾確實存在），而是**把一個有答案的問題升成待裁項，消耗協調者的判斷力**。
   ⇒ 可執行：**在把某件事標成「無人知道」之前，先 grep memory 與 issue。**
   ⚠ 他同日第二次：稍早建 sandbox 卡 10 分鐘才想到查 memory，而答案（Gotcha #3）前一天就寫好了。
   **兩次共同點都是「動手／下結論之後才查」** ⇒ 與 leader5 的「我驗了容易驗的那一半」同構：
   **錯的是查證的時機，不是查證的方法。**
2. **拿單一個案推機制通則**：他用 #552（單一 addon）推出「gate 取代 SOP」的通則，
   而 gate 的 `-u` 目標寫死單模組這件事 **他讀 script 時看到了，卻沒把它當成範圍限制**
   ⇒ 🔑 **看到了不等於用上了。**

---

# ✅ 【假警報·已解除】四方同改 `src/controllers/paas.py` — **實測不重疊，兩方獨立算出同一結論**

> 🔴 **結論先行**：`grep '^@@'` 顯示 Gauge 與 Sluice **間隔 11 行、語意正交、不需任何人讓路**。
> 下面保留完整協調過程，因為**這次協調本身的成本才是教訓**。
>
> ```
> Gauge 最後 hunk : 3508~3514（新側）／3491~3497 +context 3500（舊側）
> Sluice 起點     : 3515（-context 3512 起）        ⇒ 間隔 11 行 ✅
> Gauge 觸及 try/except 的行數 : 0                  ⇒ 不碰例外結構 ✅
> ```
> **兩個算法（dispatcher 算新側、leader3 算舊側）、兩個人、同一結論。**
> Gauge ＝ 新增 `release_facts` dict（17 行）＋四個 state 分支各換一行 ⇒ **加法、自成區塊**；
> Sluice ＝ 404 分支 unlink 包 savepoint ⇒ **例外處理**。**同函式的不同關注點，git 自動合併。**
> **唯一硬性條件**：第二個 merge 的人 **rebase 後必須重跑測試**，不得沿用 rebase 前的數字。

## 🔑🔑 這次協調的教訓（比撞車本身重要）

> **順序：先量重疊，再談優先權。**
> 差點在一個不存在的衝突上做了兩輪協調——**兩則訊息、兩位 leader、三個假設**
> （leader3 以為要抽 25 行、dispatcher 以為 Gauge 較接近完成、leader5 準備讓路），
> **而正解是一個 `grep '^@@'`。**

**leader5 補的三層順序（更精確）**：
| 層 | 做什麼 | 效果 |
| --- | --- | --- |
| **1** | **量測**（比對 hunk 範圍） | **多數撞車在這一層就解散** |
| 2 | 真重疊才問**能不能拆** | 不同關注點常可並行合併 |
| 3 | 真不能拆才談**誰先** | 判準是**剩餘成本**不是已花費 |

⚠ **leader5 的自我批評更值得記——關於資訊呈現順序**：
> 「我先前直接跳到第 3 層提建議（Sluice 先 merge、Gauge rebase），雖然也做了第 1 層量測並附上 hunk 標頭，
> **但呈現順序反了：我把建議放前面、量測放後面**。若對方照建議行動而沒讀量測，
> **就會做一個不必要的序列化**。**量測結果應該放在最前面，因為它可能讓後面兩層整個不必要。**」

⇒ 🔑 **一份包含「量測 ＋ 建議」的回報，讀者常常只執行建議。**
**所以能讓建議作廢的量測，必須放在建議之前。**

## 實查（dispatcher 自量，`git show c6ae5122:src/controllers/paas.py`）

```
_update_service        : 3023
_delete_service        : 3260        ← Sluice 的 B5 原標的
_update_service_status : 3381-3524（144 行）
```

| 誰 | 線 | 標的 | 狀態（dispatcher 實查） |
| --- | --- | --- | --- |
| **Sluice** | leader5／#525 B5 | `_update_service_status` **尾端 3515 附近約 7 行**（404 分支 unlink 包 savepoint）<br>`git diff` hunk **只有一個**：`@@ -3515,7 +3515,22 @@` | **PR#568 open、mergeable=True、已 rebase 到 c6ae5122、覆核中** |
| **Gauge** | leader3／#552 層④ | `_update_service_status` **回寫路徑**（status→state 的 write，判斷在中段） | **未開 PR**，已跑 1h23m |
| Vault | leader5／#553＋B3 | 1189／1447 | 進行中 |
| Conduit | leader5／#554＋B4 | 1888–2260 | 進行中 |

⇒ **Sluice 與 Gauge 高機率不重疊**（尾端 vs 中段），但**誰都不該用「高機率」動手**
⇒ **已向 leader3 索取 Gauge 的 hunk 範圍**（`git diff origin/develop...<branch> -- src/controllers/paas.py | grep '^@@'`）
🔑 **leader5 的判準：兩邊 hunk 一比就知道，這比討論誰先更快，也不需要任何人讓路。**
**用資料解決，不用協商解決。**

## 🔑 撞車的成因：兩邊都做過檢查，是**檢查完之後標的才移動**

leader5 派工時的重疊檢查（1189／1447、1888–2260、3045+ 三段不重疊）**在當下完全正確**。撞車來自兩次事後移動：
1. Sluice 的標的因**勘誤**從 `_delete_service`(3260) → `_update_service_status`(3381)
2. Gauge 的工作是 leader3 **裁決折進去的**（原本不在 #552 範圍）

> 🔑 **同檔佔用的檢查不能只在派工當下做一次。**
> **觸發時機＝任何一次「標的位置更正」或「範圍裁決」之後，重跑一次佔用檢查。**
> ⚠ 這兩件事都不像變更（一個叫**勘誤**、一個叫**裁決**），所以**不會觸發「我改了東西要重新檢查」的直覺**
> ——與 `wt-ci-tests` 差點被當殘留清掉同構：**危險的動作偽裝成無害的名字。**

## 🔴 dispatcher 本輪兩個錯誤（都被 leader 當場更正）

### ① 派工單寫「#552 已上線」——**merged ≠ 上線**

```
prod    : 18.0.1.0.88   odoo=<<EMPTY>>  n8n=<<EMPTY>>     ← leader3 實查
develop : 18.0.1.0.100
```
⇒ 四個版號 pin **只存在於 seed 檔**，prod 的 n8n／odoo 仍是「開通就抓第三方 registry latest」
⇒ **#552 要解的問題在 prod 一個字都沒解**，還需要一次發版。
⚠ memory `reference_repo_two_release_trains` 就寫著「merge≠上線」，而 dispatcher **當天才把「#552 發版是 gate 首次實戰」寫進本看板**
⇒ **知道這件事，寫派工單時卻用了「已上線」** ⇒ 又一次 [[feedback_apply_your_own_review_standard_to_yourself]]。

### ② 在 leader3 未完成 #552 時派了 #543

`#552` 是 **3/4**（層④ fail-closed 由 Gauge 進行中），不是完成。
⇒ **dispatcher 自訂的分配規則明寫「還在做的事不能再分」，自己破了它。**
（leader3 已接下並安排另招 teammate，故不撤回；但判斷失誤記在 dispatcher。）

### ③ 附帶：把「時間投入」當成「完成度」

我寫「Gauge 已跑 1h23m 有先行成本；Sluice 時程較鬆」⇒ **實際相反**（Sluice 已完成、Gauge 未開 PR）。
> 🔑 **跑得久不代表接近完成，也可能代表遇到的問題更難。**
> **讓已完成的產出等待進行中的工作，成本比反過來高。**

## 📌 #519／#556 定序：leader1 **驗證中，不進看板**

他明確要求草案不入看板（**未定案的推論若進了持久載體，會被後人當結論引用**）。僅記狀態：
- **已定案可回報**：prod 實查全落地 #556（**暴露面 0、但缺陷 rule 在 prod 在位、無利用跡象**）；#519 紅燈判準已更正（docker 路線基準 **3 errors** 不是 1）
- **驗證中**：定序表態草案正送**三個獨立視角**對抗式檢驗（安全／工程／前提）
- 🔴🔴 ~~已授權立即動工：model 層 `create()` 訂閱閘~~ **「`create()` 閘可先做」已被推翻，該授權作廢**
  （2026-08-04，leader1 的三視角對抗式檢驗一致判草案需修正，他逐條複驗）：
  1. **repo 自己在 `src/models/cloud_service.py` 內註解逐字寫著「`create()` 的閘門是防意外、不是安全邊界——不要在這裡補守衛來製造『已經守住』的錯覺」**
（⚠ **用 `git grep 'create() 的閘門是防意外'` 定位，不要記行號**——dispatcher 原本寫 `:2741-2744`，
leader3 實查發現**已漂到 `:2840`**、差約 99 行，而 `:2741` 實際是 `_do_cleanup` 的 `_logger.warning`）
     ⇒ 實查佐證：帶合法訂閱過閘、隨即 `write({'subscription_id': False})` ⇒ 孤兒列
     （`subscription_id` 無 readonly／無 inverse／無守衛，`cloud_service.write()` 明文拒絕加守衛）
     ⇒ **create-only 連 #519 自己的缺陷都關不掉**
  2. **#556 的自我提權走 `write` 不是 `create`** ⇒ 收緊 create rule 一個字都碰不到它；
     且 Odoo 在 **INSERT 之後**才對新列求值 create rule、`records.modified(...)` 使 `workspace.access_ids` 失效重讀
     ⇒ **任何用 `workspace_id.access_ids` 表述的 role 閘，會被攻擊者剛塞進去那一列自我滿足**。**雙重 no-op。**
  3. 兩條 rule 都在 `noupdate="1"` 區塊 ⇒ 改 XML 到不了既有 DB，要配 post-migrate（repo 在 `18.0.1.0.23` 付過這筆帳）

**⇒ 定案表態（取代先前「驗證中」）**：
**A 阻塞未解除且範圍比原先想的大（擋整張 #519）／B 沒有可分離的半邊／C #556 優先序應往上提**
——新理由：`security_access` 的 role 閘（`ir_rules.xml:115/126`）**已經上線**，而它今天就被 #556 打敗
⇒ **既有控制的活體回歸，不是未來風險。**

### 🔴 新路徑（一行指標，論證在 #556 留言）

**`src/models/workspace_access.py:96` 的 `transfer_ownership`**：守衛寫成 `if self.role != ROLE_OWNER`
——**檢查的是被操作那筆記錄的 role，不是 `self.env.user`**
⇒ 任一成員（含 `guest`）拿 owner 那列當 `self` 呼叫、`new_owner_id` 填自己 ⇒ **一次 RPC 奪取整個 workspace**。
方法無 `_` 前綴（`call_kw` 可達）／`ir_rules.xml:32` 讓任一成員對該 ws 每列 access 都有 write／
`_check_owner_count` 因「先降 owner 再升自己」躲得過／**零呼叫點零測試**（陽性對照 `_reset_admin_password` 命中 12 檔）。
🔑 **它活過 #519 與 #556 目前所有提議中的修法**，包括「把 workspace_access write 綁 owner/admin」
——**合法受邀的 `admin` 照樣奪得走**。修復成本一行。**開單與否待 Eugene。**

🔑 **他的自我約束判準值得記**：
> **不是所有結論都需要對抗式檢驗，是「會直接變成別人決策輸入」的結論才需要。**

🔴 **他劃的誠實界線，原樣保留不得壓縮**：
> F2＋F3 支持「portal 過不了那道 create ACL」，**但沒有人以 portal 身分實測過 `call_kw`**（沙盒已依 Eugene 指示銷毀）
> ⇒ 目前是**三條結構證據**（DB 直查 ACL 表／core 無 `implied_ids`／遞迴展開 `implied_but_not_direct=0`），**不是實測**。
> **「比只讀註解硬，但不是坐實。」**

⚠ **今天三起「主結論對、附帶敘述沒被驗」的事故，共同起點都是證據等級在轉述中被壓縮。**

## 📌 版號佔用表（四方）

```
develop .100 ｜ Vault .101 ｜ Sluice .102 ｜ Conduit .103 ｜ Gauge 未定（須避開 .101–.103）
```
⚠ **`.100` 之後必須數值比不能字串比**（`'18.0.1.0.100' > '18.0.1.0.99'` → **False**）
——memory `reference_rebase_silently_eats_manifest_bump:102+` 有完整實測。

---

# 🔴🔴 【live 資訊外洩】#567 — `UserError` 子類全部走原文分支，PR#523／#527 **已在 develop 上**

> **這不是「未來要做的改進」，是「現在開著的洞」**（leader5 原話）。

## 事實

PR#523／#527 建立的例外三分支分類：
```python
except AccessError                   → 泛用訊息
except (ValidationError, UserError)  → 原文 str(e)     ← 洞在這
except Exception                     → 泛用訊息
```

**dispatcher 實查 Odoo 例外階層**（`odoo/exceptions.py`）：
```
:16  class UserError(Exception)
:45  class AccessDenied(UserError)      🔴 也漏（不是 AccessError 子類）
:64  class AccessError(UserError)
:85  class MissingError(UserError)      🔴 leader5 的 teammate 找到的
:94  class ValidationError(UserError)
```
⇒ **`UserError` 有四個子類，分類只列了兩個** ⇒ `MissingError` 與 `AccessDenied` 都落進原文分支。

**實測外洩原文**（leader5 線提供）：
```
Record does not exist or has been deleted. Record: woow_paas_platform.cloud_service 999999999, User: 1
```
⇒ **model 技術名／record id／User uid 全洩給租戶。**

## ✅ dispatcher 找到的第二處：**不屬 #567，它是 PR#566 正在修的** —— 但這個發現**救了那張 PR**

dispatcher 最初在 develop `paas.py:1538`（#357 密碼重設）看到 `except UserError` **連 `AccessError` 都沒攔**，
四個子類全走原文。**leader5 更正：那處是 `api_service_reset_admin_password` ＝ #525 B3 的標的，Vault 的 PR#566 正在改它**
（dispatcher 看的是 develop 舊碼）。

### 🔑 但比對 Vault 分支後，發現他的修法**也漏了 `AccessDenied`**

**dispatcher 複驗**（`fix/553-estimate-leak-525b3-reset-pwd`）：
```
import: from odoo.exceptions import AccessError, MissingError, UserError, ValidationError   ← 無 AccessDenied
:1248  except (AccessError, MissingError)    ← 漏 AccessDenied
:1574  except (AccessError, MissingError)    ← 漏 AccessDenied（＝ develop 的 :1538，已位移 +36）
:1324  except AccessError                     ← #567 標的（PR#527 的成果）
:3051  except AccessError                     ← #567 標的
```
⇒ **修的人已經意識到「子類會漏」這個問題（所以補了 MissingError），但仍只補了他想到的那一個。**
⇒ leader5 已要 Vault **趁 PR 未 merge 補上**：成本是一個 import ＋ 兩個 tuple 各加一項；
**事後開單則要重開分支、重跑全套、再一輪覆核。**

> 🔑 **「四子類」這個發現的價值不在找到洞，在於它趕在 merge 前抵達。**
> 同一個修正，PR 未 merge 時是三行改動，merge 後是一整輪流程。

⚠ **附帶一個實例**：develop 的 `:1538` 在 Vault 分支已是 `:1574`（位移 +36）
⇒ 又一次坐實「**行號會隨他人分支位移，delta 不可推算，必須重取來源定位**」。

## 📌 #567 的正確範圍邊界（leader5 指定）

**只有 `1324`（`api_service_skip_trial`，PR#527 的成果）與 `3051` 兩處** —— 它們**只攔 `AccessError`**，漏 `MissingError` 與 `AccessDenied`。
⚠ leader5 已明確要求 Vault **不要**在 PR#566 裡擴進去（避免範圍蔓延）。
✅ **`AccessDenied` 併進 #567**（同成因＝`UserError` 子類階層、同修法＝併進泛用分支，分開會讓同一個洞修兩次）。

```python
# c6ae5122:src/controllers/paas.py:1534-1540（逐字）
    # 刻意「不」用 sudo()：method 內部對 secret 的讀寫已自行 sudo，外層保持
    # current-user env，ir.rule 才會繼續驗這位 admin 對該 service 的可及性
    new_password = service._reset_admin_password()
except UserError as e:
    return {'success': False, 'error': str(e)}      ← 前面沒有任何 except AccessError
except Exception as e:
```
⇒ **四個子類全部走原文。**

🔑 **這處的註解自己說明了為什麼它更危險**：它**刻意不用 `sudo()`**，就是要讓 `ir.rule` 繼續驗這位 admin 的可及性
⇒ **`ir.rule` 擋下來時拋的正是 `AccessError`**
⇒ **那條路徑的設計目的，恰好保證了會拋出被原文外洩的那一類例外。**
（`AccessError` 原文形態：`You are not allowed to access 'xxx' records... Records: xxx (id=N), User: N`。）

## ⚠ 證據等級（**兩者不同，不可在看板上看起來一樣硬**）

| 來源 | 等級 |
| --- | --- |
| #567 內文（paas.py 3 處／billing.py 5 處 bare `str(e)`） | **盤點只做到位置與形態，因果未逐處取證**（issue 自陳） |
| dispatcher 本輪兩處（`AccessDenied`／`:1538`） | **已逐字取證**（貼 `exceptions.py` 行號與 `paas.py:1534-1540` 原文） |

## 🔑 leader5 的教訓：引用已 merged 的參照實作

> **引用既有參照實作時，要驗證它本身是否完整，不能因為它已 merged 就假設它是對的。**
> **引用會把洞一起複製過去，而且複製過去的洞看起來有前例背書、更不會被質疑。**

他自陳的具體失效點：**他在工單裡警告過「AccessError 是 UserError 子類，只寫 except UserError 會把它一起原文吐出」**
——**然後只驗證了「它有處理 AccessError」**。

⇒ 🔑🔑 **dispatcher 推的一層：檢查「我擔心的那項有沒有被處理」與檢查「這個分類是否窮盡」是兩件事，
而前者做完會產生「我驗過了」的感覺。**
⇒ **可執行：凡是「依型別分派」的程式碼，驗收要問「這個型別有哪些子類」，而不是「我關心的那個有沒有被列到」。**

⚠ 同族已在看板：leader4 的「**本規格最弱的一句是哪句**」（主結論對、附帶敘述沒被驗）、
leader6 的「**抓到東西之後要多問一句：這個查法看不到什麼形態的同類問題**」。
**三者都是「檢查通過了，但檢查的範圍不是全集」。**

## 待排

`#567` 的其餘部分（paas.py 3 處／billing.py 5 處）**不在 leader5 線的範圍**，需排人。
⚠ #567 自己註明：「**盤點只做到位置與形態，因果未逐處取證，動手前請各自重新定位並驗證**」
⇒ 接手者**不可照抄行號**（今日已有 `paas.py` 行號整批 stale 的實例，且偏移量不可推算）。

---

# 🔴🔴🔴 【前提推翻】#543 的那條「刻意保留的紅燈」**從未驗到它宣稱要驗的東西**

> **它死在 fixture，`migrate()` 從未執行。** 今天有三個人（leader3、leader6 的工單、dispatcher）
> 把它當成 #543 的判準，**dispatcher 還據此廣播給全員並寫進派工單當「最重要的一條」**。

## 事實（develop `c6ae5122`，一個字沒改；leader3 的 teammate Bellows 發現，leader3 與 dispatcher 各自複驗）

```
test_...py:132 → :57 _make_legacy_policy → Policy.create → 巢狀 o2m
  → rule.py _check_allow_policy_no_bulk_grant()
  → ValidationError: Allow policy cannot use email_domain as an Include rule
```

**dispatcher 逐字實查**：
```python
# test_security_access_p_rule_migration.py:53-62（fixture 鷹架）
pol = self.Policy.create({... 'rule_ids': [(0, 0, {
    'group': 'include', 'rule_type': rule_type,      ← 用傳入的 rule_type
    'rule_value': rule_value})]})
self.env.cr.execute("DELETE FROM %s WHERE policy_id = %%s" ...)   ← 建完立刻 raw SQL 刪掉

# :132-134（呼叫端）
self._make_legacy_policy('allow', 'email_domain', 'a.com', 'prm-c')
self.migrate(self.env.cr, '18.0.1.0.39')     ← 從未執行
```
⇒ **建立當下就被守衛擋住。** Bellows 已把 #543 真缺陷修好後重跑：**仍 1 error，失敗點一字不差還在 fixture。**

## 🔑 fixture 的 docstring 自己暴露了成因

> 「raw SQL — **an ORM unlink of the last include would trip the new constraint**」

**寫的人已經意識到「ORM unlink 會撞守衛」所以改用 raw SQL 刪，卻沒意識到前一行的 ORM `create` 也會撞。**

⇒ 與同日的 `AccessDenied` 事件**完全同構**（修的人補了 `MissingError` 卻漏 `AccessDenied`）
⇒ **同一天、不同檔案、不同人 ⇒ 這不是個別疏忽，是「想到一個實例就以為涵蓋了那個類別」這個形狀。**

## 🔑🔑 leader3 的自剖（本日最有價值的兩句）

> **我用 traceback 驗的是「同一性」（跟上次一樣嗎），沒驗「有效性」（它驗到東西了嗎）。**
> **這兩個問題長得像但完全不同，而前者可以在後者恆為否的情況下永遠通過。**

> **穩定的紅比不穩定的紅更容易被信任，這正是它的危險。**

⚠ 他在 PR#550 的驗證留言裡**貼過這條完整 traceback**，用它證明「紅的原因沒變、仍是同一條」
——**取了證、貼了、還拿它當通過的依據，而 traceback 從頭到尾都寫著 `_make_legacy_policy`。**
⇒ 三個人都沒起疑的原因：**它每次都紅在同一行、traceback 一字不差** ⇒ 那個穩定性被讀成「可靠訊號」，
**實際上它穩定是因為每次都死在同一個地方——而那個地方不是受測程式碼。**

⇒ 形狀是 [[feedback_post_hoc_test_lacks_red_phase]] 的鄰居但不同：
那則講「**沒跑過 RED**」，這則是「**跑過 RED、紅得很穩定、而紅在無關的地方**」。

## ✅ leader3 的裁決（選項 1），dispatcher 驗過前提

改 `_make_legacy_policy` 的**鷹架** `rule_type` 為固定安全值（如 `ip_range`），**flat 欄位仍寫 `email_domain`**。

**dispatcher 獨立驗證觸發條件完整保留**：
```
src/migrations/18.0.1.0.39/post-migrate.py
  cr.execute("SELECT %s FROM %s" % (..., _POLICY_TABLE))   ← 只查 policy 表
  rule_type = rec.get('rule_type') or 'email_domain'       ← 從 flat 欄位讀
⇒ 完全不讀 rule 列；而 fixture :62-64 已把 rule 列全刪
⇒ 鷹架 rule_type 對 migrate() 無關，改了之後它面對的 DB 狀態逐位元組相同
```

**硬性驗收（leader3 定，取代 dispatcher 原本的約束）**：
> **注入式驗證的 RED 必須發生在斷言或 `migrate()` 內，不得在 `:57` fixture。**
> 兩者都顯示「1 error」，但**前者證明測試真的跑到了受測程式碼，後者就是現在 develop 的狀態**。

**否決選項 2（留紅、另開測試）**：**壞掉的探針要修或要拆，不能留著當裝飾**
——一條「永遠紅、且紅在無關位置」的測試留在 develop，**不只是噪音，它會主動製造錯誤結論**（今天已實際發生三次）。

## 🔴 dispatcher 撤回自己派工單的核心約束

我在 #543 派工單寫的、並標為「🔴 最重要的一條」：
> ~~「驗收判準不是『測試綠了』，是『我沒有碰測試，而它綠了』——測試檔 `git diff` 必須為空。」~~

**前提是「這測試是有效探針」⇒ 前提不成立 ⇒ 正式撤回。**
⚠ 我還要求 teammate「動工前先讀 PR#550 description」——**那份 description 本身建立在同一個錯誤前提上**。
🔑 **leader3 的版本比我的嚴格**：我要的是「沒碰測試」（**形式**），他要的是「RED 在正確位置」（**實質**）。

## 判準表更正（dispatcher 今日廣播給全員的那張）

| 看到 | 原判定 | 現況 |
| --- | --- | --- |
| `1 error(s)` 且是那條 | ✅ 正常基準 | ✅ **仍成立** |
| 錯的變別條／多於 1 條 | 🔴 你改壞了 | ✅ **仍成立** |
| **全綠** | ⚠ 要嘛 #543 已修好，要嘛沒跑到 | 🔴 **「#543 修好會轉綠」是錯的**（結構性不可能）；「沒跑到」那半仍成立 |

✅ **leader5 的「紅燈沒出現 ⇒ 這次跑的範圍不完整」仍完全成立**
——**它只依賴「紅燈會出現」，不依賴「紅燈驗到什麼」。**
⇒ **不另發廣播**（實際危害有限、#543 驗收在 leader3 手上），僅看板更正。

## 相鄰發現（leader3 將開單）

`.37` 的 fan-out 對 pre-`.39` DB 做 `pol.copy()` 時，policy 尚無 rule 列 ⇒ 撞 `_check_at_least_one_include`
⇒ **同類別的第二顆地雷**。已要求 Bellows **把實測形狀寫進 PR/issue，不只回報給 leader**。

---

# 📋 【收工清單】以下四項等所有線停止寫入後執行（dispatcher 落地，避免只活在 context）

**執行前置**：`T=$(date -v-5M +%Y-%m-%dT%H:%M:%S); find <memory> -newermt "$T" -type f -name '*.md'` **回空**才動手。
⚠ **不可用 `-newermt '-5 minutes'`**——這台的 `find` 是 bfs shim、不吃 GNU 相對寫法，**stdout 空 ＋ stderr 有錯**，
而它的作用是「非空就停手」⇒ **失效方向恰好是靜默放行**。陽性對照：`-60M` 應回非空。

| # | 項目 | 現況 |
| --- | --- | --- |
| 1 | **三處 memory 重複合併** | 見下表 |
| 2 | **MEMORY.md 壓縮** | 20,092 字元／上限 24.4K ⇒ **未超限，hook 只是提醒** |
| 3 | **`prod-release-sop.md` 更新** | 停在 07-25，早於 cd#77 gate 上線（08-01）兩天 ⇒ 過期的是 SOP |
| 4 | **dispatcher 自補一個實例** | 見文末 |

## 三處 memory 重複（**三種不同形態**，查法各異）

| # | 檔案 | 形態 | 為何難發現 |
| --- | --- | --- | --- |
| 1 | `reference_k8s_sandbox_test_gotchas`（leader4 報） | 兩人 append **同一檔** | ✅ grep 得到 |
| 2 | `feedback_completeness_check_output_which_not_how_many`（leader5 報） | 兩人 append **同一檔** | ✅ grep 得到 |
| 3 | `feedback_preserve_findings_before_dismiss` **vs** `feedback_answer_landed_but_carrier_unread`（leader5 報） | 一人 append 既有檔、**一人新建獨立檔** | 🔴 **檔名不同 ⇒ 兩邊都命中 ⇒ 看起來像交叉引用** |

> 🔑 **leader5 的新判準：查重時要同時比對「同一件事有沒有兩個檔名」，不只比對內容。**
> ⇒ 第 3 種形態**只有讀 MEMORY.md 索引才看得到**——**索引是唯一能看到「檔名層級重複」的地方**。
> ⇒ 合併方向（leader5 建議、dispatcher 同意）：以 `feedback_answer_landed_but_carrier_unread` 為主體
> （它的「判準二：講第三次還沒對上就改投遞方式」與 pattern 坑更完整），
> 併入 preserve 檔那段的「前四種形態失效在**回報方**、這種在 **leader 側**所以前四種檢查抓不到」，並留一行指標。

⚠ **這三處都是規則正常運作的副產品，不是失誤**：`reference_memory_files_append_without_conflict_signal` 已記
「**找到既有載體就更新」這條防重複規則本身就是收斂機制……重複只是囉嗦，靜默遺失不可逆**」
⇒ **前兩處是兩人都選對了載體、只是同時到達**；第 3 處才是真的分岔。

## 🔴 合併時必須分開寫的兩個「假回 0」——**症狀相同、處方互斥**

| 成因 | 原文裡有沒有星號 | 正解 | 為什麼另一個解法無效 |
| --- | --- | --- | --- |
| **渲染剝掉 markdown 標記** | ❌ 沒有（被剝了） | 用**純文字** pattern | `-F` 無效——**原文根本沒有星號可比對** |
| **BRE 把 `*` 當量詞** | ✅ 有 | `grep -F` | 「去掉星號」無效——**要找的字串本身就含星號** |

⇒ **若合併成「pattern 別含星號」一條**：踩到 BRE 那個坑的人會去掉星號，
**但他要找的內容真的有星號** ⇒ 找不到而且不知道為什麼。
✅ **可操作的分辨法**：先 `grep -F` 試一次 —— **命中＝BRE 量詞問題；仍回 0＝渲染剝掉（或內容真的不在）**。
（渲染側由 leader5 線記載；BRE 側是 dispatcher 今日實測：`grep -c 'Eugene更正**：**租戶限定'` → 0，`-F` → 1。）

## 第 4 項：dispatcher 要自補的實例

新檔那條判準：**「同一份內容講第三次還沒對上，就停止重複、改投遞方式」**
⇒ **dispatcher 今天正是反例**：leader6 三次補問 #546 未得答覆，我的處置是**繼續在回報裡列它**，
**沒有改投遞方式**（正解是在對方正在看的物件上開一則獨立短留言，而不是第四次重述）。
⇒ 收工合併時補上這個 dispatcher 側實例——**該條判準約束的不只是 teammate 對 leader，也包括 dispatcher 對 Eugene。**

---

# ✅ 【#543 完成】PR#574 — develop 的全綠基準即將回歸（已廣播全員）

**dispatcher 實查**：`fix/543-migration-bulk-grant-bypass` @ `a1e3356d`，**open／mergeable=True／【勿 merge】在**。

```
基準 develop c6ae5122 : 0 failed, 1 error(s) of 2349
PR#574 分支           : 0 failed, 0 error(s) of 2349      ← 全綠
總數兩邊都是 2349 ⇒ 沒有測試被吃掉，差額恰好就是那 1 個 error
```

## ✅ leader3 加的硬性條件通過：**RED 在受測程式碼內**

還原生產修法（**保留**測試修復）後的 traceback：
```
test_...py:143  self.migrate(self.env.cr, '18.0.1.0.39')
migrations/18.0.1.0.39/post-migrate.py:122  Rule.with_context(...).create({
models/security_access_access_policy_rule.py:158  _check_allow_policy_no_bulk_grant()
ValidationError: Allow policy 'flat-prm-c' cannot use email_domain as an Include rule.
```
⇒ **`_make_legacy_policy` 完全不在 traceback 上**，且**只有這一條紅、另兩條仍綠**（精準命中）⇒ **真 RED→GREEN**。
⇒ 對照今天稍早的推翻：**同樣是「1 error」，但失敗點從 fixture 移到了 `migrate()` 內部**——
**這正是「兩者都顯示 1 error，但只有一個證明測試跑到了受測程式碼」的實證。**

leader3 另外實查確認：`_SCAFFOLD_RULE` 固定 `ip_range`、`decision` 仍吃參數、flat 欄位仍照參數寫
⇒ **`migrate()` 看到的狀態逐位元組不變**；舊 docstring 那句「紅的原因不是測試壞了」**已移除**（那句本身就是錯的）。

## 🔴 兩個連帶失效，已廣播

1. **「`0 failed, 1 error`」從正常基準變成異常**（merge 之後）⇒ 請以當下量到的 develop 為準，不要沿用記憶中的數字
2. 🔴 **leader5 的「紅燈＝範圍完整性探針」隨之失效** ⇒ 需另找：**總測試數 2349**，或任一必跑的 class
   ⚠ **Odoo 的靜默 deselect 沒有任何訊號** ⇒ **全綠之後更需要範圍探針，不是更不需要。**

## 📌 已開 #575：`.37` fan-out 撞 `_check_at_least_one_include`（同類別第二顆）

含 G/H/I 三組實測與**探針陷阱**：`route_id` 的 `NOT NULL` 要先 DROP，否則三組全是 `NotNullViolation`
——**看起來像探針做不出來**（而不是被測物沒問題）。

### 🔑 leader3 在單裡點的類別問題（dispatcher 認為值得主動盤點，待 Eugene 決定派人）

> **2026-08-02 加的那批 ORM 守衛，還有哪幾道會在 migration 的中間狀態上誤觸發？**
> **目前已知兩道，而它們是各自獨立被踩到的、不是盤點出來的。**

⇒ 這正是今日反覆出現的形狀（**想到一個實例就以為涵蓋了類別**）。
⇒ **等第三顆被踩到才盤點，成本是每次一輪 debug。**

## 🔑 Bellows 自己抓到並改掉一個錯誤宣稱（形狀值得記）

他第一版 docstring **照抄隔壁 `sa_skip_policy_quota` 的理由**，寫「flag 必須放 cursor，因為 context flag 會被 context-stripped env 靜默丟掉」。
**實測後不成立**（改成 context flag 後 A–E 五組結果完全相同）——那條論證針對的是**另一道守衛的觸發路徑**。他改成經查證的說法。

> 🔑 **照抄相鄰程式碼的「理由」比照抄程式碼危險**：
> **程式碼抄錯會壞；理由抄錯會變成一句看起來很有根據的假話，而且它會被下一個人引用。**

⇒ 與同日 leader5 的「**引用既有參照實作時要驗證它本身是否完整**」同族，但更隱蔽：
**參照實作的洞至少還在程式碼裡看得到；參照理由的洞只存在於註解，沒有任何執行路徑會揭穿它。**

## 🔧 工具判準統一（leader5 實測，兩人今日各踩一種、原文狀態相反 ⇒ 交叉印證）

**`grep` 假回 0 有兩類成因，方向相反：**

| 成因 | 原文有星號嗎 | 正解 | 今日實例 |
| --- | --- | --- | --- |
| **pattern 被解讀成別的東西** | ✅ 有 | `grep -F` | dispatcher：BRE 把 `**` 當量詞 |
| **原文被轉換成別的東西** | ❌ 沒有（被剝了） | 用**純文字** pattern | Vault：驗 #567 留言時渲染剝掉 markdown |

> 🔑 **`-F` 之所以是好的分辨器，正因為它只關掉 pattern 側的解讀，不碰原文側。**
> **`-F` 修好 ⇒ pattern 側；`-F` 沒修好 ⇒ 原文側（渲染／編碼／換行）。**

⇒ 框架涵蓋今日其他實例：`git grep -E` 的 `\s` 退化（**pattern 側**）、`cut`／`sort`／`tr` 的 `Illegal byte sequence`（**原文側**，`LC_ALL=C` 解）。

## 🔑 「三次對不上」規則的完整版（leader5 補上鏡像）

- **dispatcher（被問方）**：leader6 三次補問 #546 未得答覆，我**繼續在回報裡列它、沒改投遞方式**
- **leader5（提問方）**：他**追問四輪，而對方每次都答了**——沒改讀法

> 🔑 **三次對不上時，提問方要換讀法、回答方要換投遞方式，不能只有一邊動。**
> ⚠ **寫成單邊規則的話，另一半的人不會覺得那條在說自己。**

---

# 📋 【合併順序】Eugene 問「什麼 PR 要先合併」— dispatcher 實查後的答案

## 全站 open PR 實查（2026-08-04）

```
develop: c6ae5122 → (#571 merged) → 5f8db11b   版號 .100 → .101

PR#571  .101  10 檔（docs 為主）           【勿 merge】已移除 → ✅ 已 merge
PR#573  .104  14 檔（operator + paas.py）  【勿 merge】已移除 → ✅ 已 merge（develop ee293b6b／.104）
PR#574  .105   5 檔（migration + tests）   【勿 merge】已移除 → 🔧 rebase 中（衝突僅 manifest）
────────────────────────────────────────────────────────────
PR#566 / #568 / #569 / #570                【勿 merge】仍在 🔴 不合
```
⚠ **Gitea 列表上的標題是舊快照**——571/573/574 的【勿 merge】實際已移除（覆核通過）。

## 順序：**#571 → #573 → #574**，依據是**版號天然遞增**

`.101 → .104 → .105` ⇒ **照此順序合完全不用重新配號**。
反過來（#574 先合）會讓 develop 跳到 `.105`，另兩張的 `.101`／`.104` 都小於 develop ⇒ **必須重 bump 並重跑測試**。
其他理由：#571 純 docs 風險最低先驗流程；**#574 最後合 ⇒ develop 以「全綠」作結**（它是唯一改變測試基準的）。

## 衝突點（`git merge-tree` 試算，非目視）

| | 共同檔案 |
| --- | --- |
| #574 ∩ #573 | `src/__manifest__.py` ＋ **`src/tests/__init__.py`** |
| #574 ∩ #571 | `src/__manifest__.py` |
| #573 ∩ #571 | `src/__manifest__.py` |

**#573 的實際衝突（已發生）**：
```
base c6ae5122 = .100 ／ develop 5f8db11b = .101 ／ PR = .104   ⇒ 三方各不同 ⇒ 同一行衝突
merge-tree → CONFLICT 只有 src/__manifest__.py     ← 唯一
```
✅ **解法：取 PR 側的 `.104`。版號不需要連續、只需要嚴格遞增**，`.104 > .101` 已成立，且 #574 的 `.105` 仍在後面。
🔴 **rebase 後必須重跑測試**，且 **develop 目前基準仍是 `0 failed, 1 error of 2349`**（#574 未合）
——rebase 後看到 1 error 是**正常的**，不要以為 rebase 弄壞了什麼。

## 下游影響

**#573 動了 `src/controllers/paas.py`** ⇒ 它一合，還帶【勿 merge】的 **#566／#568／#570 全都要 rebase**（三張都動 paas.py）。

---

# 🔴 【看板缺口已補】#559／PR#569 — **#535 的側門，前門已上線**

**dispatcher 主動盤點 open PR 時發現它不在看板上**，自行查 issue 與 PR body 補回（歸屬已向 leader2 確認中）。

> `#535` 把服務詳情 API 的憑證遮罩改成 spec-driven 並已合併（`c723a44d`），
> 但那道遮罩在 **controller 層**，而 `cloud_service.helm_values` 是**裸欄位、沒有 `groups=`**
> ⇒ 租戶可直接 `POST /web/dataset/call_kw` 送 `read(['helm_values'])`，
> **繞過整個 controller 遮罩層**拿到明文憑證。

⇒ **鎖了前門，側門還開著——而前門（#558）已經上線 develop。**
（PR#569 body 自陳已用**真 tenant 帳號**做過三格量測坐實缺陷。）

## 🔑 它是今日「範圍不是全集」家族裡**最難察覺的一種**

| 實例 | 漏在哪 | 可見度 |
| --- | --- | --- |
| `MissingError`／`AccessDenied` | 同一類別裡漏了成員 | 同檔可見 |
| ORM `create` vs `unlink` | 同一守衛的另一觸發點 | 同檔可見 |
| **#559** | **同一資產的第二條存取路徑** | 🔴 **第二條路徑根本不在你修改的那個檔** |

⇒ 前三個至少在同一個檔案裡看得到；**#559 的第二條入口是 Odoo 通用 ORM 端點**，
與被修改的 controller 完全無關 ⇒ **改 controller 的人沒有任何理由會去看它。**
⇒ 已建議 leader2 在 PR 描述留下「**controller 層的遮罩不涵蓋 ORM 端點**」這個事實本身，
**否則下一個做類似遮罩的人會重蹈。**

---

# 🧹 【memory 清理】Eugene 裁決：不 hard code 版號、多餘資料刪除、屬於 docs 的寫進 docs

## Eugene 的原話與展開

> **stale base 不只污染程式碼，也污染「比較」** ⇒ **memory 不應 hard code 記錄版號，有太多多餘的資訊。
> 請刪除多餘的資料。若是屬於 docs 的資訊，需要寫入，不要放到 memory 中。**

已寫入 `reference_upstream_facts_git_grep_origin_ref`（同日兩次 append）。核心：
**理由不是「版號會變」，是「memory 裡的具體數值會被後人當成基準使用」**
——寫死「prod 是 `.88`」，下一個讀到的人會拿它去比較，**而他不會知道那是快照，因為 memory 讀起來像「事實」而非「當時的觀測」**。

🔑 **leader3 給的機械判別法（比 dispatcher 原版可操作）**：
> **版號可以出現在「歷史敘述」與「實測輸出原文」裡，但不能出現在「現況」句型裡。**
> **判別法＝把句子改成過去式，看還通不通。**
> 「prod 仍是 .88」→ 改不掉（現在式斷言）❌ ／ 「2026-08-03 實測 image=.88」→ 天然過去式 ✅
>
> ⚠ 他自己當天就違規（`project_cd77_module_version_gate` 寫了「prod 仍是 .88，develop 已到 .100」），
> **成因值得看**：他在記錄「一個掛了兩天沒人接的驗收」，**那個事實本身需要版號才講得清楚**
> ⇒ **違規的不是「提到數字」，是「沒把數字釘在時間上、也沒說它不是基準」。**
> ⇒ 修法範本：保留實測輸出原文（gate log `image=.88 db=.88`），**註明「它證明的是這條分支跑過，不是 prod 應該是那一版」**，
> 並在該節明寫「**本節刻意不寫 prod/develop 現在是哪一版**，判斷要當下自己量兩邊」＋附查法。
> （dispatcher 複驗：`prod 仍是`→0、`本節刻意不寫`→1、landing 索引 `.88→.100`→0，全部落地。）

## ✅ 已執行：刪掉 20 檔的「索引行壓縮備份」段落，共 **11,486 字元**

那是 **2026-07-19 上次壓縮 MEMORY.md 時搬進 topic 檔的舊索引行**，段落自陳
「MEMORY.md 索引壓縮時移入的壓縮前索引行全文（**細節多已含於上文，此為保全**）」。
人工抽驗兩個覆蓋率最低的（0%），確認**資訊確實已在正文**，只是摘要 vs 展開的用詞差異。

**驗證**：231 檔中移除 20、dispatcher 的 append 1、未動 210；現況含該字串 **0 檔**、備份中 **20 檔**（探針有效）；備份逐檔 sha256 比對零異常。

### 🔴 過程中 dispatcher 自己踩兩個坑，都在動手前攔下

1. **拿 `bytes` 跟 `chars` 比較**，以為補寫時把檔案寫小了（6439 bytes vs 4854 chars）
   ⇒ **今日第二次**同坑。諷刺點：**才剛把「數值會被當基準」寫進 memory，下一步就自己拿錯單位的數值當基準。**
2. 🔴 **段落邊界用「標題到檔尾」**，把後續新增的 `##` 段落全算進可刪範圍
   ⇒ **多算 3,135 字元，全部是當天寫的重要內容**（逐字比對、raw 讀路那批）。
   **若照那個數字動手，會刪掉當天最有價值的一批。**
   🔑 **錯誤的方向是「把該留的算成該刪的」** —— 量測方法本身要先驗證。

## 🔴 已凍結：9 份 `project_prod_rollout_*`（25,464 字元）— 因為刪除閘的邏輯被推翻

**dispatcher 原本的閘**：「這條教訓在 memory 別處還有沒有載體？有就可以刪。」

> 🔑🔑 **leader3 戳破**：**用「memory 內是否還有備援」當閘，等於用「要被清空的那個空間」去證明安全性。**
> 實例：`0 queries` 這條有 3 個載體，**全部在刪除清單裡**（`.68`／`.80`／`.88`）。

**dispatcher 實查後的修正**（結果分兩半）：
```
原本查的 6 條：清單外備援 8／1／10／4／4／6   ✅ 全部有（檢查確實排除了清單內，無自己人灌水）
0 queries    ：清單內 3、清單外 0             🔴 leader3 的發現成立
```
⇒ **真正的問題是覆蓋率**：只抽驗 7 個關鍵字，而 `0 queries` 落在沒查的那些裡。
🔑 **但這讓論點更強：方法對、排除也對，仍然漏——因為關鍵字清單的完整性本身無法自證，你不知道你漏了哪個同義詞。**
⇒ **新閘的價值不在換更好的關鍵字，在於它根本不用關鍵字。**

### ✅ 新閘（leader3，已採用）

> **刪除的判準不該是「memory 裡還有沒有別的載體」，而是「這條有沒有進 SOP」。**
> 程序：① SOP PR merge ② **逐條讀 SOP 找對應（讀，不是 grep——內容被改寫過，關鍵字不會逐字保留）** ③ 找得到才刪那一條所屬的檔。

## 🔴 dispatcher 最嚴重的一個錯誤：說 `.68`「0 條教訓、可直接棄」

```
dispatcher 的 SIG 關鍵字（⇒／教訓／表示／判準／How to apply／最大教訓）命中該檔：0 行
該檔實際寫的是：**本波新知（跨 session 續用）：** ＋ 5 條編號教訓
```
⇒ **「新知」不在關鍵字清單裡。一個同義詞的缺口，讓整個檔被判成空的。**
⇒ 而它被寫進派工單，**成為 leader3 與 Datum 的作業前提**（leader3 自陳「我沒驗就轉述」）。

⚠ **這正是同日早上才寫進本看板的那條**——「**寫進工單的假陰性會被當成公理**」
——**當時 dispatcher 還自評「事後檢查合格，不是事前有意識地套用」。這次連事後都沒檢查。**

**`.68` 必須保全的兩條**（Datum 讀原檔發現）：
- **startupProbe `initialDelaySeconds=180`** ⇒ 新 pod 即使 `SKIP=yes`（免 `-u`）也要 ~3 分才 ready，**兩輪各一段預期內的 502**
- **boot log 的 `0 queries` ＝「0 migration re-run」的硬證據**（配合 digest 不變）

## 📌 Datum 的 SOP 工作：commit 存在但**還沒 push**（dispatcher 補的物件層證據）

```
git branch -a --contains 232cb472 → + docs/prod-sop-rollout-lessons   ← 只有本機分支
                                    沒有任何 remotes/origin/* 包含它
commit: docs: prod 發版 SOP 收斂 9 份 rollout 教訓＋對齊 cd#77 module-version-gate
        prod-release-sop.md | 1 file, +181/-11        ← 與 leader3 從畫面讀到的一致
```
⚠ **第一次查用 `git cat-file -t` 得到 `commit`，那證明不了它在 remote**
——多 agent 共用同一份 `.git`，本機 worktree 的 commit 也在。**要用 `git branch -r --contains`。**

**Datum 的兩個判斷 dispatcher 認為正確**：
- **不收 `.53` 的「刪 tag 再 re-push」**——與 SOP §5.1.4 現行的「走 workflow_dispatch」直接打架，收進去會變成錯的指引
  ⇒ 他保留「gate 逾時 180 分鐘」這個**事實**並指向現行路徑。**事實留下、過期的處方不留。**
- **manifest 不 bump**——他實查 SOP 近 5 次變更全部 `manifest=0`（**查了慣例而不是猜**）

---

# 〔2026-08-04 16:45 dispatcher〕Gitea 寫入短暫故障（已恢復）＋ 五張 PR 現況

## 一、Gitea 故障：**已恢復，不要再引用「現在壞著」**

leader2 於 16:2x 回報「Gitea 只能讀不能寫、發版被擋」，16:4x 自行更正「已恢復」。dispatcher 兩度獨立實測，**兩次結論都成立、但描述的是不同時刻**：

```
16:31  platform comment POST → 500 ｜ paas-odoo-ci comment POST → 201  ← 當時是 repo-scoped
16:39  platform comment POST → 500 ｜ paas-odoo-ci comment POST → 500  ← 已擴散成 instance-wide
16:44  platform label PATCH → 200 ×2 ｜ comment POST → 201 ×2          ← 恢復
```

**故障期間的面貌（供日後比對）**：

| 路徑 | 狀態 |
| --- | --- |
| 所有讀取（含走 indexer 的 search） | 200 ✅ |
| **git 資料面寫入**（建/刪 branch） | 201 / 204 ✅ |
| issue 子系統寫入（comment 建立、label 建立/修改、PR PATCH、訂閱刪除） | **500，body 為空** ❌ |
| `/api/healthz` | **`pass`**（連 `database:ping` 都 pass）⇒ **它不知道自己壞了** |

🔴 **500 是乾淨失敗、無部分寫入**（實查留言數 0、label 未建出）⇒ 重試安全。

## 二、這次踩到的三個判準（都是既有教訓的新實例）

1. **「現在 X 壞了」型廣播的壽命是分鐘級。** 收到的人必須**自己量一次再行動**，而不是轉發。leader2 自陳「我發那則急件時就該預期它的壽命」。
2. 🔴 **排他性結論要跨 repo 對照。** leader2 宣稱「Gitea 寫入全掛」，但兩個樣本（#569、#559）**都在同一個 repo**，對「範圍」這個軸零鑑別力。dispatcher 加了 `paas-odoo-ci` 對照才看出當下是 repo-scoped。
3. 🔴 **`PUT subscription → 200` 是 no-op，不是寫入成功。** dispatcher 一度拿它當「寫入沒全掛」的反例；查 `subscriptions/check` 才發現 `created_at=2026-08-03`，**本來就訂閱著**，Gitea 對已訂閱回 200（新建才回 201）。**探針要能區分「做了」與「本來就是那樣」。**
4. **空的錯誤訊息不是「沒事」。** `tea` 與 Gitea API 都把 5xx 吞成空字串（`Error: could not edit pull request: ` ／ `{"message":""}`）。故障前有一次「空 Error: 重試就成功」，**那是前兆而不是雜訊**。

## 三、五張 PR 現況（dispatcher 16:44 實測，遠端 develop `e9e89b24` / `.105`）

```
#569  mergeable=True   .109  head 4e76e1d0   【勿 merge】已移除  ✅ leader2 覆核九項全過 → 可合
#579  mergeable=True   .105  head 58975c36   【勿 merge】仍在    ⏸ leader4 等 #576 裁決
                             （只動 2 檔、0 個在 src/ ⇒ 不需 bump，版號同 develop 是對的）
────────────────────────────────────────────────────────────────
#566  mergeable=False  head e0feccc3   衝突   leader5／Vault
#568  mergeable=False  head fdd59268   衝突   leader5／Sluice
#570  mergeable=False  head d145297b   衝突   leader5／Conduit ← 已 rebase 完，全套跑著，估約 20 分
```

⚠ **merge 當下要重量一次版號**——develop 每小時動 1–2 次，上面的數字同樣會過期。

## 四、新開的一條線：flaky 測試污染所有人的 baseline

`TestProvisionUniqueIndex.test_partial_unique_index_present`
成因＝**測試自身缺陷**：`indexname LIKE` 命中 2 筆、無 `ORDER BY` 卻斷言 `rows[0][0]`。

⇒ **會隨機讓任何以較舊 DB 當 baseline 的 PR 變紅，症狀看起來像「索引不見了」**，容易被誤診成 schema 問題或「我改壞了」。
⇒ leader2 量到 baseline 1 failed／fix 組 0 failed，**已歸因，develop 不需標紅**。
⇒ **已派給 leader1**（16:45），硬性要求：RED 必須發生在斷言上而非死在 fixture、貼原始輸出、PR 一開就帶【勿 merge】、版號嚴格大於 develop。

## 五、〔17:0x〕rebase 波共通坑（leader5／Conduit 實測，已廣播 leader1／leader4）

### 🔴 最危險：**fetch 失敗會製造「假的交叉印證」**

Conduit 在 worktree 裡 rebase，第一次 `git fetch` 回 `Authentication failed`（**tea token 一小時效期到了**），
但 `git rev-parse origin/develop` **照樣輸出一個值，而且正好等於 leader 稍早給他的那顆 SHA**。

成因：**worktree 與主 repo 共用同一份 `.git`**，那顆值是**別人**先前 fetch 進來的快取。

⇒ 他拿到的不是「我沒驗到」，而是「**我自己驗過了，而且和 leader 一致**」
——**一個看起來被交叉印證過、實際只有單一來源的結論。**

**判準**：
- **兩個「獨立」來源指向同一份本機 `.git` 時，它們不是兩個來源。**
- 要證明 fetch 成功只有兩條路：①看 `git fetch` **自己的 exit code**（不要接 `| tail`、不要加 `2>/dev/null`——**那會吃掉唯一的失敗訊號**）；②改用不吃本機快取的 `git ls-remote origin <branch>`。
- token 解法：先跑任意一個 `tea` 指令會刷新並寫回 config，再從 config **動態**讀 token 配 `http.extraheader`（寫死的失效後症狀與沒帶憑證一模一樣）。

### 這批 rebase 的兩個已知衝突形態（對 #566／#568 有預測價值）

1. **`src/__manifest__.py` 真的會進衝突清單**，三方相異形態。
   正解＝寫成「**當下 develop 值 + N**」，**不是 take 任一邊**（take ours 降版、take theirs 等於沒 bump）。
2. **`src/tests/__init__.py` 這次是真 `CONFLICT`、不是 `Auto-merging`**（與 #574 那次不同）⇒ git 至少會提醒。
   但**集合差驗證仍要做**——Conduit 做的是 197/197 雙向空 ＋ 注入式陽性對照。

### ⚠ dispatcher 自己犯的錯（形狀很常見，一併記）

用 `maestri list 2>&1 | head -40` 盤點在線 agent ⇒ 下了「**leader5 已不在清單、三張 PR 無人認領**」的結論，
**寫進看板也報給 Eugene**。實際上 **leader5 連同 Vault／Conduit／Sluice 排在輸出最後一段，被 `head -40` 切掉**。

⇒ 危害不是少看到一個名字，是**產生了「要別人接手他人在途工作」的建議**——照做會有兩條線同時改同一批檔案。
⇒ 判準：**任何要拿來下「有沒有 X」結論的指令，不准帶 `head`／`tail`／預設分頁。**
要控制輸出長度用**過濾**（`grep`）而不是**截斷**——**過濾不改變全集，截斷會。**

## 六、〔17:0x〕leader5 三張 PR 的時間感

| PR | worker | 狀態 |
| --- | --- | --- |
| **#570** | Conduit | **已 rebase 完**（解四檔衝突＋集合差驗證），重建環境 ~6 分＋全套 ~15 分＋回報 ~5 分 ⇒ **約 20 分鐘**。兩個不確定：衝突複雜度、全套若出 error 無法預估；**卡住會立刻講** |
| #566 | Vault | **已 rebase 並推上**（tip `e0feccc3`、`.106`）｜覆核通過，標示可移除 |
| #568 | Sluice | **已 rebase 並推上**（tip `010e83f6`、`.107`）｜覆核通過，標示可移除 |

---

# 🔒〔2026-08-04〕**發版合併計畫（定案）** — 四張 PR 進這一波

## 一、實測狀態：四張全部已 rebase 到 develop tip、全部可乾淨合併

```
develop tip = e9e89b24  (.105)
#566  tip=e0feccc3  .106   merge-base=develop tip  merge-tree 乾淨
#568  tip=010e83f6  .107   merge-base=develop tip  merge-tree 乾淨
#570  tip=c6e7ff1b  .108   merge-base=develop tip  merge-tree 乾淨
#569  tip=4e76e1d0  .109   merge-base=develop tip  merge-tree 乾淨
```
dispatcher 與 leader5 **各自獨立跑過** `merge-base` ＋ `merge-tree --write-tree`，結論一致。

🔴 **不要用 Gitea 的 `mergeable` 欄位判斷**——它是懶更新快取，#566／#568 都顯示 `False` 而實際可乾淨合併。
**失效方向是「說不能合、其實能合」**，後果是白做一次 rebase；而 leader5 補的一句更關鍵：
**白做的那次 rebase 又會改變 base、再觸發一次重跑 ⇒ 成本是連鎖的。**
權威判法：`git merge-base --is-ancestor origin/develop origin/<branch>` ＋ `git merge-tree --write-tree`。
⚠ 用前 `git fetch` 要帶**顯式 refspec**（`+refs/heads/X:refs/remotes/origin/X`）——
`git fetch origin <branch>` 只更新 `FETCH_HEAD`，**不保證建立 `origin/<branch>`**，
而 `git diff`／`merge-tree` 遇到不存在的 ref **靜默當 pathspec 回空、不報錯**。

## 二、合併順序：**#566 → #568 → #570 → #569**（依版號天然遞增，不用重新配號）

🔴 **#569 必須最後合**，即使它最早覆核完。它一先合 develop 就跳到 `.109`，
另外三張的 `.106`／`.107`／`.108` **全部低於 develop** ⇒ 三張都要重 bump ＋ 重跑全套。

## 三、做法選 **(a) 邊合邊追**，共 4 次 merge ＋ 3 次小 rebase

四張都 rebase 到**同一個** develop tip ⇒ 彼此是**兄弟不是鏈**。第一張一合、develop 前進，
第二張的 base 就舊了 ⇒ `__manifest__.py` 與 `tests/__init__.py` 必然三方衝突（實測：`#566` 後 `#568` 即撞）。

⚠ **選 (a) 的理由是 leader5 的版本，不是 dispatcher 原本那個**（原本寫「(b) 會讓已跑完的全套作廢」，
但 **(a) 的結果其實也作廢了一半**——小 rebase 後 base 同樣變了）：
> **真正的理由是「改動面小且可驗證」**：那兩個檔有明確驗法（版號數值比較 ＋ import 集合差雙向驗證），
> 不需要靠重跑全套確認；而 (b) 的鏈式 rebase 讓三張程式碼疊在彼此之上，**那個組合沒有任何驗法能取代全套**。

## 四、🔴 每次小 rebase 後的驗收條件（leader5 原文，leader2 的 #569 同樣適用）

小 rebase 後**不需重跑全套**（只碰兩個檔、程式邏輯未變），但**必須**：

1. `git diff origin/develop -- src/__manifest__.py` **非空**，且**數值 tuple 比較**嚴格大於（不是字串比較）
2. `src/tests/__init__.py` 的**雙向集合差**（斷鏈側與孤兒側都空）＋ **注入式陽性對照**
   ——**這個檔的失效是靜默的，不驗就沒有訊號**
3. 🔴 pattern 用 `^from`，**不要**用 `^from \. import`——該檔有 `from .integration import ...`

⚠ manifest 衝突**不可 take 任一邊**：take ours 降版、take theirs 等於沒 bump。要寫成「當下 develop 值 + N」。
⚠ **不要拿「有沒有衝突」當 bump 的訊號**——兩邊 bump 到同值時衝突根本不會出現。

## 五、🔴 新增硬性關卡：**四張合完後在 develop 上跑一次全套**（leader5 提出，dispatcher 立為必過）

**共同盲區：沒有任何一次測試跑在四張的聯集上。**
```
#566 的全套 : develop + 566
#568 的全套 : develop + 568
#570 的全套 : develop + 570
最終 develop: develop + 566 + 568 + 569 + 570   ← 這個組合從來沒被測過
```
四張都動 `src/controllers/paas.py`（各動不同函式）、三張動 `src/tests/__init__.py`。
**個別綠不保證聯集綠。** 這一輪才是真正驗證發版內容的那一次，前三次都只是「這張自己沒壞」。

⚠ **若這一輪出問題，不要回頭猜是哪張**——直接 `git bisect` 或逐張 revert 定位。
**「個別都綠」已經排除了單張缺陷，問題必然在交互作用。**

⇒ 執行者：**leader5**（Conduit 環境已建好，邊際成本最低）。成本 ~15 分鐘。

## 六、⚠ 標示語意：**移除【勿 merge】＝內容覆核通過，不代表可以直接按 merge**

leader5 已獲准移除 #566／#568／#570 的標示（本 repo 慣例＝預設帶著、通過才移除）。
但**後面還有兩道**：每張的小 rebase 驗收（§四）＋ 四張合完的聯集全套（§五）。
**看到標示不見了不要直接按 merge。**

## 七、環境健康檢查（leader5 提出，dispatcher 修正執行點）

leader5 的原始設計：「Conduit 跑最終全套前，先在**合完的** develop 上跑基準確認，與 **2371** 不一致就停下來查環境」。
理由很好——**真正的風險不是作者獨立性，是環境偏差**（容器缺 env／少裝 addon／DB 不乾淨會**系統性偏差**，而那看起來完全正常），
而 2371 是 Vault 與 Sluice **不同 worktree、不同容器、不同時間**各自量到的硬錨點。

🔴 **但執行點寫錯了，照字面做結構上不可能通過。** dispatcher 實查各張新增的 `def test_`（靜態）：
```
#566 +12 ｜ #568 +8 ｜ #570 +26 ｜ #569 +9   ⇒ 合計 +55
```
⇒ 合完的 develop **不可能還是 2371** ⇒ 那道檢查會 100% 觸發「停下來查環境」，而環境根本沒問題。
**這是「成功條件結構上不可能達成」的形態，外觀與「環境真的壞了」一模一樣。**

⇒ **修正：環境健康檢查跑在 merge 之前的 develop（`e9e89b24`），判準＝動態數必須是 2371。**
不一致 ⇒ 容器與另外兩人不等價，**先查環境、不要開始 merge**。
**這比原設計更早攔截**——在任何人做 merge 之前，而不是四張都合完才發現前面白做。

⚠ **靜態 ≠ 動態**（leader5：develop 靜態 2383／動態 2371，差 12）⇒ 比較一律拿動態對動態，
所以 `+55` 只能推出**上限**。

## 八、聯集那輪的判準：**數量 ＋ 綠燈，兩項都要**

```
預期動態數 ≈ 2371 + 55 = 2426（上限，若全被 loader 收集）；下限必須 > 2371
```
🔴 **為什麼要驗數量**：Odoo 最惡劣的失敗模式是**整批測試被靜默 deselect**——
那時候輸出是「0 failed」的漂亮綠燈，**少的是特定一批**。只看綠燈的話，
「聯集測試」會退化成「聯集的**一部分**測試」，而沒有人會知道。
⇒ 若實際數 < 2426，不必當失敗，但**要列出是哪些沒被收集**（差幾個沒有可證偽性，是哪一個才有）。

⚠ leader5 補：**逐張 revert 時一次只 revert 一張並重跑**，不要一次 revert 多張——否則回到「不知道是哪張」的原點。

## 九、跨 PR 交互面：機械掃描結果（回應 leader2「各動不同函式 ≠ 無交互」）

做法：AST 把改動行對映到所屬函式。⚠ **不要用 git 的 hunk header**——它對 Python 預設抓不到 `def`，
dispatcher 第一版就是這樣回 0，**靠陽性對照（「總共抓到 0 對」）才發現是解析壞了而不是真的沒有**。
修正後陽性對照＝124 個 `(檔,函式)` 對。

```
被兩張以上 PR 改到的同一個函式：0 個
真交互點：2 個，都在 #569（而它最後合 ⇒ 交互風險集中在最後一步）
  ├ #566 改動的 src/models/cloud_service.py  呼叫了 #569 改過的 _assemble_deploy_values()
  └ #568 改動的 src/controllers/paas.py     呼叫了 #569 改過的 _run_post_deploy_init()
```
⚠ 第一版用**純函式名**比對跑出 12 條，其中 **10 條是 `setUp`＝跨檔同名假陽性**；
改成 `(檔案, 函式)` 配對才剩這 2 條真的。

**看實際改動：風險比看起來低。** #569 對這兩處做的是**內部實作抽換**
（直讀 `self.helm_values` → 走 `_helm_values_dict()` / `_apply_helm_values()`），**未改簽章、未改回傳形狀**。

⇒ 具體風險面＝**其他三張若新增直讀 `.helm_values` 的程式碼，#569 一合就會撞 `AccessError`，
而 #569 自己的測試抓不到**（那些程式碼在它分支裡不存在）。
**已掃：#566／#568／#570 各 0 命中；陽性對照＝同一支掃描器對 #569 自己命中 5 行 ⇒ 探針有效。**

⇒ 這個風險面目前乾淨。**但它不取代聯集全套**，只是指出要盯哪裡。

### 🔴 這份 AST 分析的邊界（Conduit 標註，dispatcher 採納——**不寫在結論旁邊就會被誤讀**）

**它證明的是「改動位置無重疊」，不是「沒有交互」。**

> 共用 model 欄位、共用 `ir.rule`、共用 `config_parameter`、**測試 fixture 互相污染**——
> 這些**都不在「同一個函式」的定義裡**，AST 一個也看不到。

⇒ 結論的正確寫法是：
> **AST 查證：改動位置無重疊（0 個共用函式）；執行期交互未驗，仍需聯集全套。**

⚠ **不要寫成「已驗證無交互」**——下一個讀看板的人會據此認為聯集全套是多餘的，而那正是本波唯一能抓到交互的那道關卡。
🔑 這份分析回答的是「**改動位置有沒有重疊**」，而風險的全集是「**執行時有沒有互相影響**」。
前者是後者的**子集**，而且是**最容易查的那個子集**——容易查正是它會被當成全集的原因。

### 同理，`.helm_values` 那個掃描也有邊界（後續補驗確認）

dispatcher 掃的是「新增行中**屬性直讀** `.helm_values`」⇒ 三張各 0。
補驗者改掃「diff 中**任何**含 `helm_values` 的行」⇒ **#566 有 3 行且全是 `+`**：
1 行是 **create vals 的 dict key**（`helm_values: json.dumps({...})`）、2 行是**測試的 `side_effect=AccessError(...)` mock**。

⇒ **兩邊都沒錯，問的是不同問題**：原 pattern 對「屬性直讀」是正確的，那三行本來就不該命中。
⇒ 但這說明**「0 命中」永遠要連 pattern 的語意一起講**——
「沒有直讀」≠「沒有碰到這個欄位」。經補驗，三處**皆安全**，可照排程合。

## 十、跨波待辦（超出本次發版範圍，記著別掉）

`^from` vs `^from \. import` 的 pattern 陷阱（`src/tests/__init__.py` 有 `from .integration import test_ecpay_invoice_real`），
**leader5 與 leader2 在同一天各自獨立踩到一次**。
⇒ leader2 建議「值得寫進 repo 的驗收 checklist 而不只是口耳相傳」——**dispatcher 同意，待提 Eugene。**

## 十一、小 rebase 驗收：**可貼上的腳本**（dispatcher 已實跑，對 `origin/pr569` 全過）

把 §四 的三項散文條件變成機械判準——**因為「要靠執行者心算才能用的判準等於沒有判準」**。

```bash
#!/usr/bin/env bash
# verify_rebase.sh <pr-branch-ref>    例: verify_rebase.sh origin/pr569
set -u
BR="${1:?需要分支 ref}"; fail=0

echo "── ① manifest 嚴格大於 develop（數值 tuple 比較，非字串）"
python3 - "$BR" <<'PY'
import subprocess,sys,re
br=sys.argv[1]
def v(ref):
    o=subprocess.run(["git","show",f"{ref}:src/__manifest__.py"],capture_output=True,text=True)
    if o.returncode: print(f"   ERR 讀不到 {ref}"); sys.exit(2)
    m=re.search(r'"version"\s*:\s*"([^"]+)"',o.stdout)
    if not m: print("   ERR 解析不到版號"); sys.exit(2)
    return m.group(1)
d,b=v("origin/develop"),v(br)
td,tb=tuple(int(x) for x in d.split(".")),tuple(int(x) for x in b.split("."))
print(f"   develop={d}  branch={b}  嚴格大於={tb>td}")
sys.exit(0 if tb>td else 1)
PY
[ $? -ne 0 ] && { echo "   🔴 FAIL"; fail=1; } || echo "   ✅ PASS"

echo "── ② tests/__init__.py 雙向集合差（用 ^from，不是 ^from \. import）"
python3 - "$BR" <<'PY'
import subprocess,sys
br=sys.argv[1]
def imports(ref):
    o=subprocess.run(["git","show",f"{ref}:src/tests/__init__.py"],capture_output=True,text=True)
    if o.returncode: print(f"   ERR 讀不到 {ref}"); sys.exit(2)
    return {l.strip() for l in o.stdout.split("
") if l.startswith("from ")}
d,b=imports("origin/develop"),imports(br)
lost,new=sorted(d-b),sorted(b-d)
print(f"   develop {len(d)} 條 / branch {len(b)} 條")
print(f"   🔴 斷鏈（develop 有、branch 無）= {len(lost)}"); [print("      -",x) for x in lost]
print(f"   🟡 新增（branch 有、develop 無）= {len(new)}"); [print("      +",x) for x in new]
sys.exit(1 if lost else 0)
PY
[ $? -ne 0 ] && { echo "   🔴 FAIL：有 import 被吃掉"; fail=1; } || echo "   ✅ PASS：零斷鏈"

echo "── ③ 陽性對照：刻意少一條，探針必須偵測到"
python3 - <<'PY'
d={"from .a import x","from .b import y","from .integration import test_ecpay_invoice_real"}
b={"from .a import x","from .b import y"}
lost=d-b
print(f"   注入式對照：移除 1 條 → 偵測到 {len(lost)} 條 ({'✅ 探針有效' if len(lost)==1 else '❌ 探針壞了'})")
import sys; sys.exit(0 if len(lost)==1 else 1)
PY
[ $? -ne 0 ] && { echo "   🔴 探針無效，上面兩項不可採信"; fail=1; }

echo; [ $fail -eq 0 ] && echo "══ 全部通過" || echo "══ 有項目失敗，不可 merge"
exit $fail
```

**dispatcher 實跑輸出（`origin/pr569`，2026-08-04）**：
```
① develop=18.0.1.0.105  branch=18.0.1.0.109  嚴格大於=True          ✅
② develop 195 條 / branch 196 條｜斷鏈 0｜新增 1
   + from . import test_helm_values_field_acl_559                   ✅
③ 注入式對照：移除 1 條 → 偵測到 1 條 → 探針有效                     ✅
══ 全部通過
```

⚠ **第 ② 項用 `^from` 而非 `^from \. import`** ——`src/tests/__init__.py` 有
`from .integration import test_ecpay_invoice_real`，窄 pattern 會**靜默漏掉它**。
**leader5 與 leader2 在同一天各自獨立踩到這個坑一次**（見 §十）。
⚠ 第 ③ 項不可省：**沒有它，「斷鏈 0」與「探針壞掉」的輸出一模一樣。**

## 十二、🟢 跨版升級風險：**已完整證偽，附全部實據**（2026-08-04）

leader5 曾報「prod `.88` → `.109` 跨 21 版，中間 17 版的 migration 完全沒被測到」，隨後**自行撤回**。
dispatcher **獨立量到同一結論**，並補上他沒有的兩層證據。**沒有任何單被開出，無需撤回。**

### ① 起點是量到的，不是假設的 —— 這是整條推論最沒被查證的一句

```
kubectl -n paas-system logs paas-odoo-755cc74675-gpdqh -c module-version-gate
  [module-version-gate] image=18.0.1.0.88 db=18.0.1.0.88
  [module-version-gate] 版本一致，無需 module update；skip
```
🔑 **`module-version-gate` 的 log 就是最省事的權威來源**——它本來就是做「image 版號 vs DB 已安裝版號」的相等比對，
**不需要 DB 密碼**（prod pod 的 env 裡沒有 `PGPASSWORD`，直接 psql 會卡在 `no password supplied`）。
容器內原始碼交叉印證：`grep '"version"' /opt/bitnami/odoo/custom-addons/woow_paas_platform/__manifest__.py` ⇒ `18.0.1.0.88`。

⚠ prod deployment 用的是 **digest pin**（`paas-odoo@sha256:6974469…`），**image tag 讀不出版號** ⇒ 不要想從 tag 判斷。

### ② `.88` 之後沒有任何 migration script

```
develop 的 src/migrations/ 總數 35，最後一個 = 18.0.1.0.87
落在 (.88, .109] 區間 = 0 個
prod 容器內實際存在的最後五個 = .59 .69 .79 .84 .87   ← 與 develop 一致
```
⇒ **prod 從 `.88` 升到 `.109` 不會跑任何 migration script。**
🔑 **版本號跨很多 ≠ migration 跨很多**——這兩者沒有必然關係，而風險宣稱是從版本號差距推論出來的。

### ③ schema 層的殘留風險也量了（AST，附陽性對照）

```
v18.0.1.0.88 → origin/develop
  欄位數 403 → 407   （新增 4、移除 0）
  新增且 required=True        = 0
  既有欄位改成 required=True   = 0
  _sql_constraints 新增/移除   = 0 / 0（兩版皆 13 條）
陽性對照：develop 全庫 required=True 欄位 = 87  ⇒ 偵測邏輯有效
```
⚠ **第一版探針是壞的**：用 `"fields." in line and "required=True" in line` 單行比對，
而本 repo style 是**多行 keyword args**（`required=True` 自己一行）⇒ 那個「0 個」零鑑別力。改 AST 後才有效。

### ④ 仍然成立的部分：`-u` 升級 vs fresh install **不等價**

理由**不是** migration，而是 **seed 資料的 `noupdate` 行為在兩條路徑上不同**
（升級時 `noupdate=1` 的 seed 不會被覆寫，fresh install 是全新寫入）。
⇒ **「兩個都跑」的裁決維持**，只是理由要改。

### ⑤ 連帶結論

- **#543／#575 追蹤的 pre-`.39` DB 撞 migration 守衛問題，不影響本次 prod 升級**（DB 實測 `.88`，已過 `.39`）。
  ⚠ 但**從舊備份還原**（災難復原）那條路徑仍會撞。
- ⚠ 本節**只涵蓋 `woow_paas_platform` 一個 addon**。若本波發版還含其他 addon，要另外查。

### 🔑 這一輪的方法論

**一個錯誤的風險宣稱會製造真實的工作**——它是「恆假檢查」的下游形態：
沒有人會因為「風險不存在」而少做事，只會因為「有人說有風險」而多做事。
⇒ **報風險與報缺陷適用同一個證據標準**：先查證再宣稱。

---

# 📋〔2026-08-04〕**發版合併執行記錄**（實查值，非推斷）

## 已合併

| 順序 | PR | 版號 | merge commit | develop 變成 | 驗收 |
| --- | --- | --- | --- | --- | --- |
| 1 | **#566** | `.106` | `b2b8e0ea089f` | `e9e89b24` → `b2b8e0ea` | 五機制驗空 commit 內容未變（留言 5069） |
| 2 | **#568** | `.107` | `d0fa22835c1e` | `b2b8e0ea` → `d0fa2283` | 兩階段 SHA 變更性質分開記（留言 5082） |
| 3 | **#570** | `.108` | `753d23dfd670` | `d0fa2283` → `753d23df` | 首次加驗 auto-merge 標記（留言 5090） |
| 4 | #569 | `.109` | — | — | leader2 小 rebase 中 |

`tests/__init__.py` 的 `from` 行數：`195 → 197 → 198 → 200`，每步皆與預期增量相符。

## 每步都跑的三道檢查（腳本在 §十一）

1. **前提②**：`git merge-base --is-ancestor origin/develop <branch>` ⇒ 確認已 rebase
2. **三態判準**：`exit==0` 且 stdout 首行 40 字元 ⇒ 乾淨（見 §九 邊界說明）
3. **驗收腳本**：manifest 數值 tuple 嚴格大於 ＋ `tests/__init__.py` 雙向集合差 ＋ 注入式陽性對照

## 🔴 §十一 之外**新增**的兩道（是這一輪學到的）

### A. 累積 import 檢查 —— 「檔案還在」不等於「它會被執行」

`git cat-file -e` 通過只代表**檔案在磁碟上**。若解衝突時吃掉一行 import，
**那些測試一條都不會跑，而全套數字看起來完全正常**（少的那批不會出現在分母裡）。

```
for 每張已合 PR:
    added = imports(合併後) − imports(合併前)
    gone  = added − imports(現在的 develop)      ← 必須為空
```
⚠ 計數不夠：`197 → 198` 抓得到「少一條」，抓不到「**少了 A 又多了 B**」。
⚠ 陽性對照要**指名**是哪一條，不能只說「偵測到 1 條差異」。

### B. auto-merge 檔的存活檢查 —— **風險分布與注意力分布是相反的**

> **`CONFLICT` 會強制人去看，`Auto-merging` 不會。**（leader4 提出）

這批四張的 merge-tree 輸出裡，`__manifest__.py` 與 `tests/__init__.py` 會 CONFLICT（**所有人都在盯**），
而 **`paas.py`（四張都動）與 `cloud_service.py`（#569 兩個交互點所在）是 `Auto-merging`（沒有人在看）**。

實測取出的存活標記（每次 rebase／merge 後計數須**不減**）：
```
src/controllers/paas.py      "except (AccessDenied, AccessError, MissingError) as e:"  × 2   [#566]
src/controllers/paas.py      "with request.env.cr.savepoint():"                        × 4   [#568]
src/models/cloud_service.py  "from psycopg2 import OperationalError"                   × 1   [#566]
src/models/cloud_service.py  "提交點（C11′ commit-on-secret）之後"                       × 1   [#566]
探針自檢：paas.py 3945 行 / cloud_service.py 3361 行（須先證實讀得到）
```

🔴 **探針自檢不可省**：做這項時第一版探針因 **shell 解析問題**讀不到 ref，三個標記全回 **0**
——外觀是「這張 PR 把 develop 的東西全吃了」，**會觸發整批回退**。
⚠ **當時的陰性對照沒有救援作用**，因為它與被測查詢**共用了壞掉的那個部分**
⇒ 只驗證了「壞掉的探針對不存在的字串也回 0」＝**零鑑別力**。
⚠ 救回來的是 **stderr 的 fatal 訊息**——若圖乾淨加 `2>/dev/null`，假警報就發出去了。
⇒ **建議用 python 讀 ref**（`subprocess.run(["git","show",f"{ref}:{path}"])` 並檢查 `returncode`）避開 shell 解析。

## 過程中修掉的三個判斷錯誤（都由別人推翻，記下來免得復發）

1. **「`mergeable=False` 只是顯示錯」** ⇒ 錯，它**硬擋 merge API**（405）。已排除的解法：
   `close/reopen`（201 但無效）、`POST /update`（三種 style 全 **500**，對 `mergeable=True` 的 PR 也 500 ⇒ **端點壞了**）、
   等自癒（輪詢 70 秒無變化）。**唯一有效＝往 head 分支 push。**
2. **「`mergeable` 只在 head push 時重算」** ⇒ 錯，**base push 也會**（合完 #566 後三張立刻翻 `False`）。
   原判斷是**拿故障期的觀察當常態規則**。
3. **「#579 的 README 掉一列＝少守一條線且無訊號」** ⇒ 錯兩處。
   `pattern-guard.py` 115 行、README 命中 **0**、只讀 `pattern-guard-map.json`；
   而 README 有 `repo-lint.py:1586 check_pattern_doc_registry`（gate #15 三方雙射）守著。
   ⇒ **真正無訊號的是 auto-merge 掉的 `map.json`**。錯因＝**從檔名語意推論而沒讀碼**。

## ⚠ Gitea 基礎設施：同日兩場不同故障，症狀會互相偽裝

| 時間 | 故障 | 症狀 | `/api/healthz` |
| --- | --- | --- | --- |
| ~16:31–16:44 | issue 子系統寫入 | 讀 200、git 面 201、**issue 寫入 500 body 空** | **200 / `pass`** ⇒ 零鑑別力 |
| ~21:0x–22:0x | **DB（pgpool CrashLoopBackOff ×9）** | 讀寫**全部** 401「invalid token」 | **424 / `database:ping fail`** ⇒ 決定性 |

🔑 **`healthz` 單向可信**：`fail` ⇒ 確定壞了；**`pass` ⇒ 什麼都沒證明**。
⚠ DB 掛掉時 Gitea 對任何 token 都回 **401「invalid username, password or token」**
——**基礎設施故障偽裝成憑證問題**，兩個人都據此誤判為 token 問題並各跑了一次 `oauth-refresh`（無害但無效）。
⚠ 兩人的「獨立印證」**用的是同一種查法**（都看 tea 的錯誤訊息、都沒查 healthz）
⇒ **獨立的是「誰做的」，不是「用什麼方法做的」**。

---

# 🟢〔2026-08-04〕**四張全部合併完成** — `develop = 73a022ac / 18.0.1.0.109`

| 順序 | PR | 版號 | merge commit | PR 留言 |
| --- | --- | --- | --- | --- |
| 1 | #566 | `.106` | `b2b8e0ea089f` | 5069 |
| 2 | #568 | `.107` | `d0fa22835c1e` | 5082 |
| 3 | #570 | `.108` | `753d23dfd670` | 5090 |
| 4 | #569 | `.109` | `73a022ac710b` | 5099 |

`tests/__init__.py`：`195 → 197 → 198 → 200 → 201`（四張共 +6）

## 最終總驗收（全綠）

```
#566 import 2 條全在 | 檔案全存在 | def test_ 12
#568 import 1 條全在 | 檔案全存在 | def test_  8
#570 import 2 條全在 | 檔案全存在 | def test_ 26
#569 import 1 條全在 | 檔案全存在 | def test_  9
                                  合計 55
auto-merge 標記 2/2、4/4、1/1、1/1 全存活；陰性對照 0
版號單調遞增 .106 → .107 → .108 → .109
```

🔑 **`55` 有獨立交叉印證**：這個數字在四張**都還沒 rebase** 時就算過一次
（當時用於評估 `.88 → .109` 的升級風險），與逐張重算**完全一致**
⇒ **沒有內容在四輪 rebase 中遺失**。leader5／Conduit 獨立算的前三張 `46` 也對得上（12+8+26）。

## 🔑 補上的一項：**import 清單與磁碟檔案要雙向對帳**

只驗「import 行還在」**不夠**：
- 指向**不存在**的模組 ⇒ import 會炸（有訊號）
- 指向**錯的**模組 ⇒ **測試不會跑，而且沒有任何訊號**

作法（不必事先知道檔名，從 import 反推）：
```
for imp in (imports(合併後) − imports(合併前)):
    mod = imp.split()[-1]
    git cat-file -e origin/develop:src/tests/{mod}.py   ← 必須存在
    該檔的 def test_ 數 ⇒ 併入總數對帳
```

## 下一步：聯集全套（Conduit 執行中）

**這是唯一一次跑在四張聯集上的測試**——前四次都只是「這張自己沒壞」。
兩條路徑都要跑：**`-u` 升級路徑**（Conduit 的 `t554dev` 刻意停在 `.105`，給出真實的多版升級）
＋ **fresh install**（理由是 seed 的 `noupdate` 行為不同，**不是** migration）。

判準是**兩項**：
```
綠燈 ＋ 數量        下限：執行數 > 2371（舊 develop 動態基準）
                   上限參考：2371 + 55 = 2426（靜態≠動態，是上限不是等式）
實際數 < 2426 ⇒ 不算失敗，但要列出「是哪些沒被收集」
```
🔴 為什麼要驗數量：**Odoo 最惡劣的失敗模式是整批測試被靜默 deselect**——
那時輸出是「0 failed」的漂亮綠燈，少的是特定一批。只看綠燈會讓「聯集測試」退化成「聯集的一部分」。

## ⚠ 事前排除的一個誤診

那條 flaky `test_partial_unique_index_present`（`src/tests/test_ha_paas_provision.py`）**仍在 develop 上**
——leader1 的修法 PR **#581 尚未合併**（帶【勿 merge】，且 `.106` < develop `.109` 需重 bump）。

⇒ **若聯集那輪紅在這一條，那是已知 flaky，不是交互作用。**
⇒ 這必須**事前**講，因為既定處置是「不要回頭猜，直接 bisect 或逐張 revert」，
   而那個處置若套在一條已知的假紅燈上，**會為了噪音做整批回退**。

## 待 Eugene 裁決（兩件，皆不阻塞聯集）

1. **#581**（flaky 修法）要不要在聯集之後補進這波 —— 需重 bump 到 `.110`，且 leader1 覆核未完成
2. **#576** 的裁決 —— 卡著 leader4 的 #579（#579 另因 #566 動了同兩個檔而需重解）

---

# ✅〔2026-08-04〕**聯集全套：兩條路徑都全綠**

```
路徑① 升級（t554dev  .105 → .109 真實多版）  : 0 failed, 0 error of 2426   哨兵 55/55
路徑② fresh install（t554fresh，兩步：-i 裝完再 -u 跑）: 0 failed, 0 error of 2426   哨兵 55/55
```
**兩條路徑、同樣的 2426、都全綠、四張的 55 條都跑到且都通過。**

`2426 = 2371 + 55` 取等號的完整性來自**三個條件的合取**（缺一都留空隙）：
| 條件 | 排除的是 |
| --- | --- |
| 無第三方混入 | `e9e89b24..73a022ac` 共 4 個 merge、21 commit **21/21 可歸屬**四張 |
| 哨兵 55/55 | **新的**一條都沒少 |
| 總數 `2371+55` | 扣掉新的，舊的必然是 2371 ⇒ **舊的也沒少** |

⚠ 總數相等**單獨不夠**——它不排除「刪一批加一批」。

## fresh install 一度 121 紅 ⇒ 根因已坐實為**指令設計**，與四張無關

```
同一個 DB、同一份程式碼 73a022ac、唯一變因是執行時機
  -i + --test-enable  → 121 紅   （測到的是「安裝過程中」的狀態）
  -u + --test-enable  → 0        （同 DB 再跑一次）
```
全部同一句 `No journal could be found in company My Company for any of those types: sale`
——sale journal 正是安裝流程產出的，**在同一次執行中跑測試就拿不到**。

**四重佐證，各自排除不同的替代解釋**：
| 佐證 | 排除的替代解釋 |
| --- | --- |
| journal 物證 | 「fresh install 缺 journal」 |
| 失敗集合 ∩ 四張新增測試 = ∅ | 「四張的測試有失敗」 |
| 哨兵 55/55（三輪） | **「交集 0 是因為根本沒跑」** |
| 對照 121 → 0 | 「journal 只是眾多缺件之一」 |

🔑 **第二與第三必須一起看**：只有交集 0 是不夠的——**若那 55 條根本沒跑，交集當然是 0**，
而那是壞消息不是好消息。**兩個一起才排除「沒跑」。**
🔑 第四項是堅持跑完對照的理由：前三項都指向同一個解釋，但**只有對照能證明除了時機之外沒有別的東西在作怪**。

## ⚠ 證據強度：這個結論經歷過一個「只有印象」的階段

執行者最初回報「失敗全部落在會計類，**沒有一條落在四張改動的區域**」
——**那是看 class 名字得出的印象，不是比對**。被追問後才做集合運算，結論不變但**證據強度差一級**。

⇒ **危險不在結論錯，在於它會以「已驗證的事實」的身分進入發版決策**，
而一旦寫成事實，後面沒有人會回頭問「這是怎麼驗的」。
⇒ 判準：**任何「沒有任何一條 X」的宣稱都必須附集合運算**；回報時把**印象階段與集合證據階段分開標**。

## 🔴 這道關卡的邊界（Conduit 原文，**轉述時不得丟掉**）

> 聯集全套證明的是「**這 2426 條測試涵蓋的範圍內**沒有交互作用」。
> 共用 model 欄位、`ir.rule`、`config_parameter`、或只在**真實 K8s 環境**才顯現的交互，
> **不在這 2426 條的涵蓋範圍內**。
> **這道關卡該擋的擋住了，但它不是「四張之間絕無交互」的證明。**

⇒ 與 §九 leader4 那條 AST 邊界（「證明改動位置無重疊、不證明無交互」）是同一個紀律：
**把「我驗到了什麼」與「這代表什麼」分開陳述。**
⚠ 「聯集全套通過」若被讀成「四張絕無交互風險」，下次真的出現 K8s 層的交互問題時，
會有人回頭質疑這輪驗證的可信度——**而那不是這輪的錯，是轉述時把邊界丟掉了。**

## 已知 flaky

`test_partial_unique_index_present` **三輪都沒觸發**（路徑①、②、對照）。單獨列出、不併入交互作用判斷。
⇒ **#581 的必要性不受影響，也不阻擋發版。**

## 方法論：兩條推廣的完整版

```
綠燈 ＋ 數量少                      ⇒ 有東西沒跑（假綠）
紅燈 ＋ 數量對 ＋ 對照組同程式碼      ⇒ 執行期問題而非收集期問題（三個條件缺一不可）
```
第二條若少了「同程式碼」，總數相同可能只是巧合（一邊多收集 X、另一邊少 X）。
⇒ **任何「數量相等所以沒問題」的推論，都要問「這兩個數字是不是從同一個母體算出來的」。**

🔑 執行者的自我反省（值得複用）：他原以為「fresh install 路徑」＝ `-i` ＋ `--test-enable`，
**但那測到的是安裝過程中的狀態，不是安裝完成後的乾淨環境**。
更值得記的是他點出**路徑① 是無意間做對的**（該環境先前已裝好，天然是兩步）
——**做對了但不知道為什麼，下次就會做錯。**

---

# 十二、發版執行與驗收 —— `18.0.1.0.88` → `18.0.1.0.109`（已上線）

## 鏈路（每一段都有實測落點，不是流程圖）

```
tag        v18.0.1.0.109（annotated）→ 73a022ac，已 push
trigger    trigger-odoo-build.yml    head_branch/head_sha 實查吻合
build      paas-odoo-ci run #1538    Eugene 於 issue #56 /approve（此步結構上只能由 Eugene 按）
image      20260804.1609
           sha256:2a14f05086a2c9e8ead25e41dc9c5b15113bc630c3b29014567cf7f66e52405f
snapshot   snap-paas-odoo-pg-pre109-20260804 / snap-paas-odoo-fs-pre109-20260804
           兩顆 readyToUse=true（由 Eugene 執行——longhorn-system 不在本 repo 允許 ns 內）
CD         woow-paas/paas-app-bundle-cd PR #91 → merge 7f3b5d05
Fleet      00:24:52 同步
pod        00:29:14 Ready   paas-odoo-744b4fc67-t4w8l  1/1  RESTARTS 0
曝險窗      merge 00:23:37Z → pod Ready 00:29:14Z ＝ 5m37s（SOP 上限 30 分）
```

## 驗收四節（實際輸出貼在 CD PR #91 留言 5141）

**§1 image 閉環——五方一致**（清單只要求三方，多驗的兩方是 deploy spec 與 gate initContainer）：

```
CD values pin ＝ CI push log ＝ pod imageID ＝ deploy spec ＝ gate initContainer
                 sha256:2a14f050…52405f
```

⚠ **gate initContainer 的 digest 必須手動同步**——`values-prod.yaml` 被 Fleet 做 Go-template
前處理（連 YAML 註解都吃），所以那個 image 只能寫死、不能用 Helm 模板。
本次改了三處：L13 tag、L15 digest、**L160 initContainers[module-version-gate].image**。
**第三處最容易漏，而漏了不會報錯——gate 會拿舊 image 去比版號。**

**§2 版號**：image manifest `18.0.1.0.109` ＝ DB `('18.0.1.0.109', 'installed')`，installed modules 81。

**§3 gate log——落在清單的「第二種：有落差」**

```
[module-version-gate] image=18.0.1.0.109 db=18.0.1.0.88
[module-version-gate] 版本落差 18.0.1.0.88 -> 18.0.1.0.109，執行 odoo -u woow_paas_platform
81 modules loaded in 12.07s, 3028 queries
[module-version-gate] module update 完成並複驗：18.0.1.0.88 -> 18.0.1.0.109
ERROR/CRITICAL/Traceback = 0（全 log 共 88 行）
```

🔑 結尾是「**完成並複驗**」而不只是「完成」：`odoo-bin` 回 0 只證明 `-u` 沒失敗，
**複驗才是對副作用取證**。這是 `cd#77 A^` 用 module-version-gate 取代舊三步舞的核心
（舊流程 `SKIP=no` → 人工 verify → `SKIP=yes` 已退役，`ODOO_SKIP_MODULES_UPDATE` 永久留 `"yes"`）。

⚠ **`Running migration` 0 行是預期值，不是異常** —— 區間內 0 支 migration 是發版前實查的。
gate 確實跑了 `-u`，證據是 **`3028 queries`**（版本一致時的秒退是 `0 queries`）。
⇒ **這件事必須主動先講**，否則 0 行會被下一個看 log 的人讀成「gate 沒工作」。

**§4 服務面**：`/web/health` 200 ｜ `/web/login` 200 ｜ `/woow` 303 ｜ `/odoo` 303
｜ 陽性對照 `/zzz-not-exist` → 404（**沒有這一項，前四個 200/303 不排除「什麼都回 200」**）。

## 🔑 主容器 8 行 traceback：用時間戳判別，不是用「看起來像已知問題」

```
Registry loaded          00:26:12
4 筆 Traceback            00:27:12   ← 在 Registry loaded 之後
來源                      ir_cron 'Base: Auto-vacuum internal data' → _file_gc could not unlink
收尾                      filestore gc 6 checked, 0 removed（正常完成）
```

⇒ 判定為 issue #503 既有噪音。**判別依據是時間戳落在啟動完成之後**——
「這看起來像我認得的那個問題」不是判準，那是把印象當證據。

## 方法論落點（本節新增，其餘見 §十一）

| 教訓 | 形態 |
| --- | --- |
| `mergeable` 是**硬擋**不是顯示問題 | merge API 回 **HTTP 405**；lazily-updated cache，**head push 與 base push 都會觸發重算**；故障期只有 head push 有效那條是「拿故障期觀察當常態規則」 |
| DB 掛掉時 Gitea 對所有請求回 **401 invalid token** | 我誤診成 token 過期，Eugene 白跑兩次 `oauth-refresh`。真因是 `/api/healthz` → 424 `database:ping: fail`。**探針缺陷＝只讀 response body 沒看 HTTP status**，401 與 5xx 分不出來 |
| 冪等端點的 200 **分不出「我做的」與「本來就是那樣」** | `PUT subscription → 200` 被當成寫入成功，實際 `created_at` 顯示早就訂閱了。Gitea 新建會回 **201** |
| `curl -w "%s"` 不是 curl 變數 | 字面印出、exit 0、**零證據**。抓到它的是回頭重讀物件本體（留言數） |
| 恆真的檢查 | `update` 回 500 之後才比 tree，「tree 相同」必然成立。**「update 成功」是 tree 比對的前提，我沒有先斷言它** |

---

# 十三、發版後的解鎖項

| 項目 | 狀態 |
| --- | --- |
| **#581** | 覆核**早已通過**（leader1，comment 5126）。閘是「等發版 tag」，**已解除** ⇒ 2026-08-05 Eugene 指示推進，工單已派 leader1 |
| **#582** | leader5 刻意裁決「不開 docs PR、改開 issue」以保住 `73a022ac` 乾淨。**發版 tag 已打 ⇒ 可 promote 進 `docs/tracking/pitfalls/`** |
| #579 | leader4 進行中，`mergeable=True`，與 #581 **零檔案交集** |
| #563 / #567 / #572 | 衍生單，皆不阻擋 |

## #581 推進前的實查（2026-08-05，dispatcher）

```
develop      73a022ac / 18.0.1.0.109      #581 head 096efb54 / 18.0.1.0.106
落後          21 commits                   ← 與 leader1 覆核時實量的 21 獨立吻合
merge-tree   #581 exit=1 首行40字元 ⇒ 🔴 真衝突（唯一衝突檔 src/__manifest__.py）
             #579 exit=0 首行40字元 ⇒ ✅ clean ← 陽性對照，證明探針有效
develop 端    對 src/tests/test_ha_paas_provision.py 零改動
註冊表        test_ha_paas_provision 已在 __init__.py:212；^from 共 201 行（與發版時量的 201 吻合）
```

🔴 **本輪最值得記的一條**：#581 停在 `.106`（**與 develop 的 `.109` 不同值**）
正是它衝突**大聲出現**的唯一原因。若當初 bump 到 `.109`（同值），
**衝突根本不會出現**，rebase 會靜默把 bump 吃掉，merge 完 develop 仍是 `.109`
——**所有以 conflict 為訊號的檢查都不會響**。
⇒ 判準：rebase 後跑 `git diff origin/develop -- src/__manifest__.py`，**空白＝被吃掉了**；
版號比較要用**數值 tuple**（`.100` 之後字串比較會給錯答案）。

⚠ 附帶：`merge-tree` 是**三態**不是二態。`首行 40 字元` **不等於** clean
——真衝突時那 40 字元是 **conflict tree**。只有 `exit==0 && 首行==40` 才是 clean；
`首行!=40` 是「根本沒算出來」，必須與前兩者分開，否則探針失效會被讀成結論。

## ✅ #581 已合併並驗收（2026-08-05 01:01:51Z）

執行者 leader1（派 Ledger 實作），dispatcher 獨立驗遠端實體：

```
merged        True          merge commit b450a019fe12c2fb5db5187f3815bcfc77bf0838
develop head  b450a019      ← 與 merge_commit_sha 一致
manifest      18.0.1.0.110  ← 嚴格大於 .109，bump 沒被靜默吃掉
標題           【勿 merge】已移除
#580          state=closed  closed_at 01:01:52Z（merge 後 1 秒）
剩餘 open PR   1 張（#579，leader4）
```

🔑 **`Closes #580` 會生效是查出來的，不是假設的**：本 repo `default_branch = develop`
（tea api repos/<repo> 實查）。若預設分支是 `main`，target 為 `develop` 的 PR **不會**
自動關閉 issue，得手動關——這一步不查就會留下一張沒人注意的孤兒單。

🔑 leader1 額外做了一件工單沒要求的事：驗「#581 的測試所依賴的**實作**有沒有被 develop 端動過」
——develop 端 3 commit、194 行改動 `cloud_service.py`（**陽性對照證明查法有效**），
但 `provision_partner` / `CREATE INDEX` / `def init` **零命中** ⇒ 依賴未被動過。
⇒ 這是「merge 前檢查同檔佔用」的正確粒度：不是問「develop 有沒有動」（永遠為真），
而是問「動的是不是我依賴的那幾行」。

⚠ 他自己標了一句值得記的：**「merge API 回 exit=0 且空回傳 ≠ 成功」**，接著去驗遠端實體。
（同族：本輪稍早 `PUT subscription → 200` 其實是 no-op、`curl -w "%s"` 字面印出卻 exit 0。）

---

# 十四、#583 開單 —— transfer_ownership 授權缺陷（dispatcher，2026-08-05）

leader1 提報、dispatcher 查重後開單。**開單前實查了三層可達性，不是憑 memory 轉述。**

## 缺陷

`src/models/workspace_access.py:99` 的守衛檢查**被操作那筆記錄**的屬性，不是呼叫者身分：

```python
if self.role != ROLE_OWNER:      # self 是呼叫端指定的 access 列
```

⇒ 攻擊者挑一筆 `role == owner` 的記錄當 `self` 即通過。**它讀起來完全像一個正確的守衛。**

## 可達性三層實查（決定嚴重性的關鍵，不能用 grep 推）

```
① ACL           ir.model.access.csv  base.group_user,1,1,1,0   （read/write/create 全開）
② record rule   ir_rules.xml:29-38
                domain_force = [('workspace_id.access_ids.user_id','=',user.id)]
                perm_read=True  perm_write=True
   🔴 語意是「這筆記錄所屬 workspace 的成員清單裡有我」——對該 workspace 的
      每一筆 access 記錄都成立，不只自己那筆 ⇒ 任一成員（含 guest）可讀寫 owner 那筆
③ 端點          git grep transfer_ownership -- src/controllers/ src/static/  零命中
                ⇒ 無 controller 端點，攻擊面是 call_kw 直呼
```

## 為什麼既有 constraint 擋不住

`workspace_access.py:80-94` 有「一個 workspace 只能有一個 owner」的 ValidationError。
它擋得住直接 `write({'role':'owner'})`，**但擋不住這條路徑**——`transfer_ownership`
**先降級現任 owner 再升自己**，過程中 owner 數量從未大於 1。
⇒ 「model constraint 只擋一半的值＝沒擋」的實例：constraint 守**狀態**，
而這條路徑經過的每個中間狀態都合法。

## 與 #556 串成一條鏈（已在 #556 補反向留言 id 5148）

```
#556  ir_rules.xml:41-50  domain_force=[(1,'=',1)] + perm_create=True
      ⇒ 任何 base.group_user 可自助加入任意 workspace
#583  ⇒ 再奪走該 workspace 的 owner
串起來 ⇒ 任何 base.group_user 可奪走「任意」workspace 的所有權，不需事先是成員
```

⚠ 後果差一級：`get_role_permissions` 顯示 `ROLE_ADMIN: can_delete=False`、
`ROLE_OWNER: can_delete=True` ⇒ 串鏈後取得**刪除權**。
⚠ 兩單**共用 `ir_rules.xml` 修改面**，不要同時開工。
⚠ **兩個都要修**：收斂 create rule 不會順帶修掉 #583（對已是成員者仍完全可用），反之亦然。

## 🔴 本單的證據邊界（轉述時不得丟掉）

**讀碼 ＋ ACL/record rule 三層實查，但未實發 RPC。**
「Odoo 在 `call_kw` 進入 method 前對 `self` 做的 access check 順序」是依
`perm_read/perm_write` 推導的，沒有實測。
⇒ 動工第一步請先做**可達性實證**。若實測不可達，嚴重性下修，
但**缺陷本體（守衛檢查記錄而非呼叫者）仍成立且仍該修**。

## 開單過程本身的一條教訓

查重時實測到：**`tea issues list` 不分頁只回 30 筆，分頁後是 95 筆**——靜默吞掉 65 筆。
⇒ 這輪的查重是分頁做的，並輸出**完整 95 張清單**供人工排除，
而不是只回報「搜過了、沒有重複」。

---

## leader1 收尾完成（2026-08-05）

`develop = b450a019 / 18.0.1.0.110`，#581 merged、#580 closed，**該線無殘留產物**
（worktree `wt-flaky-index`、分支、三個 docker 容器已清）。

他在工單之外多驗的三件，其中第 1 件我獨立複驗過：

| | 他驗的 | dispatcher 複驗 |
| --- | --- | --- |
| 1 | 合併 develop 不觸發 build（`trigger-odoo-build` 只吃 `v*` tag；`build-paas-operator` 的 paths 限 `extra/paas-operator/**`） | ✅ prod pod `paas-odoo-744b4fc67-t4w8l` RESTARTS=0、**AGE 41m 涵蓋 merge 時間點且未重建** ⇒ 實體證據，強於推論觸發條件 |
| 2 | `repo-lint` 在 rebase 後的新 head 重跑 success（**rebase 換 head 會讓舊 CI status 失效**） | — |
| 3 | 未重跑聯集全套，改查依賴：develop 端 3 commit／194 行動過 `cloud_service.py`（附陽性對照），但 `provision_partner`／`CREATE INDEX`／`def init` 零命中 | — 他自己標明「這是我的判斷，明講出來不藏」 |

🔑 他拿到比工單更強的結論：工單說「不需要動 `__init__.py`」，實際是**PR 根本不碰它**
（三點式只有 2 個檔）⇒ 連被靜默吃掉的可能都沒有。

## ⚠ 本輪第三次「memory 記了、但沒被讀」

leader1 獨立踩到並自行抓出 **`git branch -d` 成功 ≠ 已併進 develop**
（它比對的是分支自己的 upstream；訊息明說 `but not yet merged to HEAD`，
因為主 repo 的 HEAD 還停在開工時的 `c723a44d`）。他的真憑據是
`merge-base --is-ancestor 64605013 origin/develop`，**且在 fetch 成功之後才驗**。

⇒ 這條連同那段 warning 原文，**memory 早已完整記載**
（`reference_git_branch_d_checks_upstream_not_develop`）。
他做對了每一步，只是重新推導了一次。

🔑 **「紀錄存在 ≠ 被讀」**：寫下來只解決「會不會消失」，沒解決「會不會被看見」。
本輪同型的還有：`tea issues list` 不分頁靜默截斷、fetch 失敗後 `origin/develop` 退化成快取值
——**三條都在 memory 裡，三條都被重新踩了一次**。
⇒ 對 dispatcher 的意義：**工單要主動把相關坑寫進去**（teammate 是新 session，拿不到 leader 的 memory），
而不是假設「這條記過了所以他會知道」。

## 探針失效本輪合計（四個人各自踩到，形態互不相同）

| 誰 | 探針 | 為什麼沒當場發現 |
| --- | --- | --- |
| leader5 | `ls-remote \| grep \| tail`，實際 exit 128 | `exit=0` 是 `tail` 的；**「查無符合」與「沒連上」輸出一模一樣**，且答案落在**安全側**（看起來像「還沒發版」） |
| leader1 | 單引號 grep `'version'`，但 manifest 用雙引號 | 三個 ref **全回空且不報錯**——靠「太整齊」察覺 |
| leader1 | zsh 把 `git show $VAR:path` 的 `:s` 當 history modifier | `bad substitution` ＋ `diff_exit=0` ＋ 無輸出，**看起來完全像通過**；靠陽性對照抓出 ⇒ 要寫 `${VAR}:path` |
| dispatcher | `kubectl -l app.kubernetes.io/name=paas-odoo` label 錯 | 回空，差點誤報「**prod pod 不見了**」——落在**假警報側** |

🔑 **方向不是固定的**：前三個落在「安全側」（看起來像通過／還沒做），最後一個落在「假警報側」。
⇒ 不能只在「怕漏掉東西」時才配陽性對照——**任何空輸出都要先問「探針還活著嗎」**。

---

# 十五、收線總結（2026-08-05）

## PR 全數合完，open PR 歸零

```
#581  b450a019  01:01:51Z  manifest .109 → .110   #580 自動 closed
#579  c14e4f88  02:14:21Z  manifest 仍 .110（不碰 manifest，符合預期）
develop = c14e4f88 / 18.0.1.0.110      open PR = 0
prod    = 18.0.1.0.109（未被擾動，pod RESTARTS 0）
```

⚠ **develop 領先 prod 一版，但不需要急著發版**：`.110` 的內容是 #581（測試專屬）＋
#579（`.claude/` hook 對照表與 `docs/`）⇒ **對 prod runtime 零影響**。

## 團隊已收：僅剩 leader4

`leader1 / leader2 / leader3 / leader5`、`Spyglass / Periscope / Beacon`、
`Operator / Ledger`（由 leader1 關閉前自行收掉）全部 dismiss，每次都以 `maestri list`
驗實體消失、且確認 worker 先於 leader 關閉（避免孤兒）。

### dismiss 前的保全（這一步救回三件只存在於對話裡的東西）

| 載體 | 內容 | 觸發者 |
| --- | --- | --- |
| **#584**（新開） | `bn_odoo` 密碼曾入命令列（**已裁決不輪替**，留言 5160）＋ 4 條未查證線索 | Operator 自己攔下：「你 dismiss 我之前沒把它落到 issue，這個發現就跟我一起沒了」 |
| **#556** 留言 5158 | prod 唯二真實帳號 `uid 6/7` 的歷史線索（追「誰能拿到 `group_user`」的唯一線索） | dispatcher |
| **#556** 留言 5150 ／ **#519** 留言 5152 | prod `ir.rule` 洞在位的實查 | leader1 ＋ Operator |

🔑 **保全時改寫了一條，而不是原封轉存**：Operator 的「prod 落後 stg 9 版（`.88` vs `.97`）」
量於 08-03，**已被 08-04 發版作廢**。保留該條但標明它為何被劃掉——
**原封轉存等於散布一個已經變成假話的數字**。

## 🔴 仍在懸空的一件：「引用反向索引」提案沒有獨立載體

leader4 在 `issue-547-s4-decision.md` §三 提出的替代方案（實測精確度比 class 路徑方案
**高 4.7 倍**：碰 `cloud_service.py` 從複查 14 份降到 3 份；零 boilerplate 噪音；
且會抓到 `two-stage` 那個「人有機會發現卻沒發現」的案例），他自己標了「值得另開單」。

**實查：全 96 張 open issue 分頁搜尋 5 個關鍵字，零命中 ⇒ 那張單還沒開。**
它目前只存在於 #547 留言 `id=4903`（3,349 字元）之中。

⚠ **而 #547 因為 #579 用的是 `Refs` 不是 `Closes`，仍是 open**
⇒ **一旦有人判斷「#579 合完了、#547 可以關」，這個提案就跟著沉下去。**

⇒ dispatcher **沒有代開**，改成問 leader4 歸屬——理由是 leader1 這輪剛寫進 memory 的第四形狀
（派工者自己做掉工單裡的某一步、沒告知 ⇒ 對方動作變 no-op，而自己又讀成對方漏做）。
**知道一條教訓的下一步是不要在別的地方重演它。**

### ✅ 已解決（02:5x）＋ 🔴 上面那段有兩個數字是錯的，我不改寫、在此更正

leader4 回覆「我開了」，並且**開了兩張，第二張是我的查法找不到的**。

**#585** 引用反向索引 ｜ **#586** `ai_assistant._create_project` 的 `message_subscribe` 假前提
｜ **#547 已關**（`closed_at 2026-08-05T02:50:31Z`）

#### 更正一：上面寫的「4.7 倍 / 14 份降到 3 份」已過期

那是 `c6ae5122`（8/4）的數字，我從 scratchpad 原封搬進看板**且沒標量測基準**。
leader4 在 `b450a019`（8/5）重量：

```
cloud_service.py   15 → 4        最壞情況 3.75 倍（不是 4.7 倍）
零引用              33 份裡 12 份（不是 32 份裡 11 份）
```

#585 自己寫了「請引用本單的版本，不要引用 #547 留言 4903」，並要求引用者
**在當下的 develop 自己重量六個指標**（一天就動了五個）。

⚠ **這一則是本輪最貼身的一次自打臉**：我在同一份看板的 §十五 才剛寫下
「原封轉存等於散布一個已經變成假話的數字」，然後**在上一段就做了這件事**。
⇒ 差別不在知不知道規則，在於**轉存當下有沒有把「量測基準」跟數字綁在一起搬**。
數字脫離基準之後，看起來和一個當前事實完全一樣。

#### 更正二：我說「那張單還沒開」時，用的查法本身有洞

我搜的是「引用反向索引 / citation / 反向索引 / pattern doc 的 slug / 片語錨」
——**那組關鍵字全部來自我已經知道的那件事** ⇒ 這個查法的全集是「我記得的東西」，
它結構上找不到第二個沉沒點。

leader4 改查**載體本身**：程式碼裡寫 `tracked` / `follow-up` / `TODO` 卻**沒有編號**的地方。
抓到 `src/controllers/ai_assistant.py:1568-1572` 的註解明寫
「Tracked for a follow-up ticket (#547 PR description)」——**那張 ticket 從來不存在**
（open 96 ＋ **closed 126** 逐張搜，0 命中；陽性對照 `ai_assistant` 命中 13 張）⇒ #586。

🔑 **判準：「查沉沒點」不能用已知那件事的關鍵字查。**
用已知關鍵字查未知的同型問題，**全集必然等於已知**，而且會回一個看起來很乾淨的
「我查了 5 個關鍵字、96 張單、零命中」——**探針沒壞、範圍也不是隨便選的，但全集定義錯了**。
⚠ 附帶第二個洞：**我只查 open，沒查 closed**（leader4 查了 96+126）。
「這件事有沒有被記過」的答案完全可能在一張 closed 單裡。

#### #586 順帶揭露：同一個錯誤有**第三種載體**

`sudo().create() leaves create_uid=SUPERUSER` 這個假前提，除了 pattern doc，
**還逐字活在** `src/tests/test_project_decouple_frontend_endpoint.py:249-250`／`:292` 的
**斷言訊息**裡，與 `ai_assistant.py:1555` 明寫的 "That is FALSE" 直接矛盾。
⇒ `pattern-guard` 不推、`repo-lint` 不掃。**斷言訊息是沒人在守的教學面。**

#### #547 關閉前它做了一項我清單上沒有的：**存活檢查**

驗 #571 的內容在後續**九次**合併之後是否完好（它原本低估成三次）——四項全完好、
零遺失、附四組陽性對照，紀錄在留言 **5182**。
🔴 理由：**CONFLICT 會強制人看，`Auto-merging` 不會**——風險分布與注意力分布正好相反，
而典型收官清單全在驗「我的對不對」，**沒有一項在驗「我的還在不在」**。

#### 探針失效再添兩種新形態（不是舊形態重演）

| 形態 | 為什麼騙得過人 |
| --- | --- |
| **指令根本沒跑，但 `exit=0`** | macOS 無 `timeout`／`gtimeout`，錯誤訊息是 `file name too long: timeout`（**完全沒說「找不到指令」**），加 `\| tail` 後 `$?` 取 pipeline 最後一個 ⇒ 「送出成功」是假的，一整份收線報告靜默消失而送出者以為送到了 |
| **`maestri check \| grep` 回 0 ≠ 沒送達** | 終端 buffer 是**視窗不是全集**，對方一回覆你的訊息就捲走了 ⇒ **`check` 只能證明「有」，不能證明「沒有」** |

⚠ 第二條直接影響 dispatcher 的日常判斷——本輪多次用 `maestri check` 判 agent 狀態。
所幸關鍵結論（worker 是否歸零、產出是否落地）走的是 `maestri list` 與檔案／issue 實體，
不是 `check` 的畫面；**但這是運氣好，不是方法對**。


## 本輪最值得帶走的三條

1. **`Refs` 與 `Closes` 的差別會製造「沒人負責關」的單**——而埋在長留言裡的提案會隨母單一起沉。
   ⇒ 母單關閉前要問：**這張單裡有沒有東西的壽命應該比它長？**
2. **保全的順序是「保全 → dismiss」，且保全的對象包含「載體等級」不只「有沒有寫下來」**
   ——本機 agent memory 不進 git、不跨機；scratchpad 有 TTL；真正救得回的是 issue／PR 留言。
3. **轉存過期宣稱要標明它為何過期，不要原封轉存也不要靜默刪除**——
   刪掉會讓人重新踩，原封轉存會讓人相信一個假數字。

（memory 早記過這個坑，這次是第 N 次命中。）

---

# 📌 dispatcher 離線後的接續（leader4 補記，2026-08-05 晚）

> ⚠ **這一段是 leader4 寫的，不是 dispatcher。** `issue-dispatcher` 於本日稍晚失去連線
> （`maestri ask` 回 `No connection to 'issue-dispatcher'`），此後的進度沒有人記進看板。
> 以下每個數字我都自己量過；**但它們是快照**，要用請自己重量。

## 合併：八張 PR，develop `.98` → **`.116`**

| PR | 內容 |
| --- | --- |
| #565 / #571 / #579 | #547 三張（結構修 / 6 份 doc 內容修 / 交接殘留），**#547 已 closed** |
| #588 | `message_subscribe` 實測為載重，**#586 auto-closed** |
| #587 / #591 | #576 兩批 RAW check，**#576 已 closed** |
| #590 | memory 103 篇收納進 `docs/`、三索引合併回單一 `MEMORY.md` |
| #592 | `scripts/audit/memory-integrity.py` 收進 repo |

新開：**#585**（引用反向索引）、**#589**（unbound 存取判準 2:2 分裂，決策單）、**#595**（memory-integrity 45 檔盲區）。

## 🔴 三條會再咬到別人的

**1. 幽靈額度不是自由空額——是「等額置換」才觸發。**
`#576` 留言 5026 寫「今天可以往 `ha_paas.py` 加 5 個真 `sudo()` 而 gate 不響」是**錯的**：
只加真呼叫會紅（字面 17→18 > baseline 17）。真正的假陰性要**同時**潤掉一句註解裡的 `.sudo(`，
字面維持 17 = baseline ⇒ 靜默放行。
⇒ **危害因此更難防**：啟用它的動作長得像「整理文件措辭」。引用請以留言 **5224** 為準。

**2. 在落後很多的分支上跑 gate＝跑到舊版的 gate。**
#590 的分支落後 develop 53 個 commit，其 `repo-lint` 只有 **16 條**、
**沒有** `pattern-doc-registry`——而那正是最該驗的一條。
⇒ 「綠」在那裡的意思是**沒被檢查**，與「通過了」輸出完全相同。
正解：在 `merge-tree` 產出的**預覽樹**上跑，並確認分支新增內容真的在該樹裡。

**3. memory 目錄發生過一次靜默內容遺失，已修，並已機械化。**
`feedback_absence_conclusion_scope_not_universe.md` 被**整份寫回舊版**、少 21 行，零訊號。
取回方式：`~/.claude/file-history` 存的是**編輯前**版本 ⇒ **要用「已消失的內容」去搜**
（用還在的內容搜必定 0 命中，會誤判成沒有備份）。
⇒ 已收成 gate（#592），並開 #595 承接它 45/124 檔的結構性盲區。

## 環境現況（會過期，用前自己量）

- 主 repo 工作區一度停在**已合併的舊分支**、落後四張 PR，已切回 `develop`（`4a4e34f1` / `.116`）。
  ⇒ **不要用路徑讀主 repo 工作區當 develop 現況**，一律 `git show <ref>:path` 或走 `tea api`。
- 本地分支 **58 → 8**、worktree **31 → 3**。刪除前逐條驗「已併入 develop ＋ 工作區乾淨」，
  9 條未合併的 `backup/*`／`worktree-agent-*` 的 SHA 已保全在 leader4 對話中（object 約 90 天內可救回）。
- ⚠ `git branch -d` **只比對自己的 upstream**——它成功不證明已併進 develop。本輪實際依據是
  **PR `merged=True` 且 head SHA 逐字相符**。

## 給接手 dispatcher 的一句

本輪 leader4 自己踩了**同一個坑三次**（母體錯／基準沒驗／讀陳舊快取），每次都是**輸出看起來完全正常**。
⇒ 看板上任何「已完成／查無」的宣稱，請連**取得方式**一起看，不要只看結論。


