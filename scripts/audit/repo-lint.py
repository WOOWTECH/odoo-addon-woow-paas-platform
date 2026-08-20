#!/usr/bin/env python3
"""repo-lint：把「假綠家族」pitfall 轉成機械 gate 的靜態檢查工具。

三十四個 check 函式，歸為下列三十條規則（其中兩條各由多個函式實作：第 7 條
capability-gate 由四個函式——slug 集合成員／admin-gui 錨點／gateway 文案錨點／
Access 頁籤錨點；第 22 條
security-rule-lifecycle 由兩個函式——ir.rule 的出生地／退役名字。這個一對多關係的顯式
映射表是 `REPO_LINT_RULES`，由第 17 條機械對帳，所以上面這兩個數字不會再靜默漂掉）。
每條都對應一個已翻車的 pitfall，訊息內附文件指針：

1. controller-sudo   src/controllers/*.py 的 .sudo( 呼叫點數量對 baseline 白名單；
                     新增 sudo() 必須有意識地更新 baseline（附 justification）。
                     依據 docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md
2. owl-template      src/static/src/**/*.xml 的 OWL template 語法陷阱：
                     component（大寫開頭 tag）上用 t-att / t-att-* / t-attf-*、
                     t-on-* 帶不存在的按鍵 modifier（只支援 .stop/.prevent/.self/.capture）。
                     依據 docs/tracking/pitfalls/owl-template-crash-class-syntax.md
3. data-noupdate     src/data/*.xml 新增 noupdate="1" 的檔案要對 baseline；
                     noupdate 是 sticky 的，fresh install 綠 ≠ 升級路徑綠。
                     依據 docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md
4. reuse-values      src/**/*.py 出現 reuse_values=True 即 fail；helm --reuse-values 讓
                     chart 升版新增預設永不生效（既存 release 升級靜默漏，#411 Gap 2）。
                     依據 docs/decisions/patterns/helm-reset-values-unified-pipeline.md
5. seed-topology     純靜態 parse src/data/subscription_{products,template_links}*.xml，
                     算六個拓撲數對 baseline。改 seed 當下就被擋、附「持有計數的位置」
                     清單，取代「三週後 CI 才紅、紅在無關的 test 名下」（#433）。
                     依據 docs/decisions/patterns/seed-topology-invariants.md
6. test-env-restore  src/tests/**/*.py 出現 addCleanup(os.environ.pop, ...) 即 fail；
                     那是無條件刪除而非還原原值，會洗掉外部注入的 env，害字母序在後
                     的測試 fail-closed raise（單獨跑綠／全套跑紅，#436）。
                     正解 patch.dict(os.environ, {...})（start/stop 或 context manager）。
7. capability-gate   前端能力 gate 的雙向守線（#460 S2）：
                     (a) src/static/src/**/*.{js,xml} 不得用 slug 做集合成員判斷
                         （X.includes(t.slug) / SLUGS.indexOf(x)）——那是自建第二判準；
                     (b) Reset 密碼按鈕的 t-if 鏈必須追得到 has_admin_gui，
                         不論 getter 叫什麼名字（鎖「唯一的對形態」，擋任何第三判準）；
                     (c) 宣告 gateway 憑證的文案（"Gateway API Key"）其 t-if 鏈必須追得到
                         has_gateway——改判 has_admin_gui 會讓 8 個模板裡沒有 gateway 的
                         那 7 個對租戶宣告一件永遠不會發生的事（#530）。
                     (d) Cloud Service 的 Access 頁籤（setTab('access')）其 t-if 鏈必須
                         追得到 ac_max_access_seats——席位是**方案**屬性不是模板屬性，
                         改判 slug／category 今天剛好對、下一個 AC 方案就漏（#746 拍板 6）。
                     依據 docs/decisions/patterns/capability-gate-single-source-field.md
8. conceal-instances src/**/*.py（除 src/tests/）不得用 `template_id.is_hidden` 判**既有
                     service** 的可見性——實例層判準是 template_id.conceal_instances；
                     is_hidden 只剩曝光層語意。照抄舊 gate ＝「暫時下架一個商品」會把租戶
                     手上 state=running 的服務一起變成 Service not found（#471／#464）。
                     依據 docs/decisions/patterns/hidden-template-presentation-chain.md
9. reset-budget-ladder  reset 密碼的三層 timeout 預算必須嚴格外大於內、且算式自洽
                     （前端 JS 90s > 平台 client 75s > operator hard cap 65s，後者
                     ＝2×helm --timeout ＋ CLI 開銷 ＋ exec ＋ cleanup ＋ slack）。
                     #461／F-449-3 的根因就是三個預算彼此不知道對方；`--atomic` 失敗
                     還會吃第二次 --timeout（rollback wait），少算就在 rollback 途中
                     kill helm ⇒ release 卡 pending-rollback（真機實測）。
                     依據 docs/decisions/patterns/reset-async-budget-ladder.md
10. transition-anchor-utc  src/**（除 src/tests/）寫 deployed_at / last_upgraded_at
                     / last_release_sync_at（#627 (i) 納入，同屬 operator 事實時間錨）
                     不得用 `datetime.now()`（本機時區）——Odoo Datetime 契約是 UTC
                     naive，非 UTC 伺服器上 age timeout 會靜默失效或誤殺，而容器時區
                     是 UTC 所以**測不出來**，只能靠 gate（#489）。
11. assertlogs-numeric-level  src/tests/** 的 assertLogs/assertNoLogs 不得用字串
                     `'INFO'` 當 level——Odoo 的 addLevelName(RUNBOT=25, "INFO") 覆寫
                     了名稱→數值表，字串會解析成 25 ⇒ `_logger.info()`(20) 全被擋掉：
                     assertLogs 恆紅（且紅得像被測 code 掉了 log）、assertNoLogs 恆綠。
                     改傳數值 logging.INFO（#492）。
12. mixin-subscription-sudo  src/models/subscription_lifecycle_mixin.py 內不得裸讀
                     `self.subscription_id`——那是對 sh.subscription.subscription 的
                     relation traversal，而 workspace 擁有權不給 Odoo 群組，典型租戶
                     會在 env cache 落空時撞 AccessError（SH delete/provision、
                     CS update/delete 全中，#29）。統一走 `_sub()`（sudo）。
                     依據 docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md
13. config-parameter-uninstall  res_config_settings.py 每個 `config_parameter=` 的 key
                     必須登記進 src/hooks.py 的 `_CONFIG_PARAMETERS`。Odoo 卸載 addon
                     **不會**清 ir.config_parameter，漏登記＝卸載後留下孤兒設定（重裝
                     時被讀到、值卻是上一輪的）。CLAUDE.md 一直明載這條義務卻零機械
                     保護——而「少寫一行」正是 review 最抓不到的形態（#501）。
                     只查單向（宣告了沒登記）；反向不擋，清單裡有歷史遺留與非 settings
                     來源的 key。
14. duplicate-class-methods  同一個 class body 內重複定義同名 method 即 fail。Python
                     靜默採用後者、前者整段失效（無錯誤、無警告、無 log），在 Odoo model
                     上等於「新加的 override 把既有守衛整個關掉」。#501 PR 實例：新增的
                     `write()` 覆蓋掉 #279 擋 workspace 重指派的租戶隔離守衛，針對性測試
                     涵蓋不到、只有全套會紅。測試檔同樣涵蓋（重複 test method ＝前一個
                     永不執行的無聲假綠）。
15. pattern-doc-registry  pattern doc 的三方登記表必須互為雙射（目錄實況 ／
                     `.claude/hooks/pattern-guard-map.json` 的 docs 條目 ／
                     `docs/decisions/patterns/README.md` 索引），且 map 的每個 docs
                     條目都必須是**存在的檔案路徑**。#547 的根因是沒有任何機制會在
                     doc 與 code 分歧時提醒任何人，而三份名單當下就已經對不上
                     （32／30／31）；pattern-guard.py 又完全不驗證它印出來的路徑存在
                     ⇒ 讀不到的東西被當必讀件餵給模型。
                     ⚠ 只保證名單雙射與路徑存在，**不驗證 doc 內容與程式碼相符**。
                     依據 docs/decisions/patterns/README.md「維護慣例」
16. availability-reason-labels  src/models/cloud_app_template.py 的
                     `_reject_reason_for_new_instance` 能回的每個 reason slug，
                     src/static/src/paas/utils/availability_label.js 的 REASON_LABELS
                     都要有對應文案。缺一個就掉進 fallback ⇒ 對租戶顯示**另一個狀態**
                     的描述（#552：新增 unpinned_chart 時租戶看到「已售罄」，一句假話，
                     還會讓營運去查根本不存在的名額問題）。與 7(c) 同一個家族——兩邊
                     各自都「看起來對」，沒有任何測試會變紅。只查涵蓋（後端 ⊆ 前端），
                     前端多留舊 slug 的文案無害。
17. repo-lint-docstring-registry  本檔檔頭與實作互為雙射：上面的編號條目名 ↔
                     `REPO_LINT_RULES` 映射表 ↔ `run_all()` 實際呼叫的 check_*，
                     編號 1..N 連續遞增，開頭宣告的兩個數字也要對得上。#596 修好了
                     檔頭三處過期內容（「十五個 check」與實際不符、漏登記一條、11 與
                     12 顛倒），但那是症狀——**沒有任何 gate 會在下次漂移時變紅**，
                     同一個形態會原樣復發且復發時一樣零訊號。檔頭不是裝飾：它是後續
                     維護者判斷「這支工具守了什麼」的第一手資料，說謊的成本不是美觀
                     問題。順帶守兩條同型不變式：定義了 check_* 卻沒進 run_all（gate
                     寫好了沒人跑）、進了 run_all 卻沒掛 run_hook 分派（＝#596 本體，
                     CI 會抓而 hook 完全沉默）；後者可用 `REPO_LINT_HOOK_EXEMPT`
                     具名豁免。解析失敗在 `--all` 下算違規、不算無事——自檢寫成
                     fail-open 就是「永久沉默且看起來永遠是綠的」。
                     🔴 本條守的是**一致性**，不是**存在性**：把一條 check 的四處
                     一起刪掉，四處依然完全一致 ⇒ 全綠（#613 實測）。補那個洞的是
                     `repo_lint_registry_floor()`＋`baselines/repo-lint-registry.txt`
                     ——baseline 登記過的規則／函式名一個都不准消失，要調降必須顯式跑
                     `--update-baseline`。它由 `run_all()` 直接呼叫、刻意不叫
                     `check_*`（叫了就會被本條的雙射收編，變成同樣「四處一起刪就靜默
                     消失」的一員）；反過來「run_all／run_hook 有沒有呼叫它」由本條的
                     第 (6) 點守，兩者互相守著對方的存在。
                     ⚠ 只對帳名單／編號／數字，**不驗證條目描述與 check 行為相符**。
                     ⚠ 4b 只驗 ast 層「函式名有沒有出現在 run_hook」，**不驗執行時
                     到不到得了**（搬進永不匹配的分支照樣綠，#612）。可達性由
                     scripts/audit/test_repo_lint_hook_dispatch.py 用真實 repo 路徑
                     實跑 run_hook() 驗證。
18. kubectl-ns-whitelist-sync  kubectl 可操作 namespace 的白名單同時寫在 CLAUDE.md
                     的表格、docs/guides/deployment/shared-cluster-paas-scope-guard.md
                     的散文，與 .claude/hooks/kubectl-ns-guard.py 的 ALLOWED_NAMESPACES
                     常數（2026-08-16 起的 Bash 層機器守線）裡，三份副本的集合必須相
                     等。2026-08-06 實地教訓：平台自己
                     的 Fleet 注入 secret 全住在 `fleet-local`，paas-app-bundle-cd 的
                     文件早就把 `kubectl -n fleet-local` 寫成標準程序，而本 repo 的白
                     名單漏列它 ⇒ 兩份文件對同一個 namespace 給出相反指引，讀者照先讀
                     到的那份做，兩個方向都零訊號。不去重成一份是因為三份服務不同讀者
                     （CLAUDE.md 每 session 自動載入；guard doc 是 pattern-guard hook
                     推給人讀的敘事版，抽走清單等於被強制讀的那份不再直接寫出規則；
                     ns-guard hook 要毫秒級判定，清單必須內嵌成常數）
                     ——**必要重複就該機械化同步**。
                     ⚠ 只比對三份副本是否相等，**不驗證清單本身完整**（漏列一個實際
                     在用的 namespace 需要對照叢集實況，屬於人的工作）。
                     依據 docs/guides/deployment/shared-cluster-paas-scope-guard.md
19. poll-release-facts-single-source  poll 有**兩份實作**（cron 走
                     src/models/cloud_service.py 的 `_poll_and_update_status`、租戶打
                     端點走 src/controllers/paas.py 的 `_update_service_status`），
                     帳本回寫的欄位集合必須來自同一個 producer `_release_facts`：兩份
                     都要呼叫它，且兩份內都不得再把 helm_revision／helm_chart_version
                     當 dict 字面鍵列出。#627 的形態：兩份各自組 write vals，controller
                     那份寫了 chart_version、cron 那份沒寫，而唯一斷言「release_facts
                     必須到得了每個 write 分支」的測試**整份只驅動 controller** ⇒ cron
                     那份整份沒回寫也全綠，#552 層① 的第五條回寫路徑在無人值守時失效，
                     stg 實地漂移 3 天零告警。**同一功能有兩份實作時，覆蓋率要問的是
                     「每一份都被驅動了嗎」。**
                     ⚠ 只守**結構**（單一 producer 沒被繞過），不驗證兩份跑出來的值
                     相同——那由 src/tests/test_poll_mirror_release_facts_627.py 的
                     `TestBothPollImplementationsWriteTheSameFields` 實跑兩份比對。
                     形態層擋「結構已退化但還沒漂」，行為層擋「改個寫法繞過形態判準」。
                     依據 docs/tracking/pitfalls/reaped-since-single-path-stamp-gc-immunity.md
20. doc-symbol-anchor  docs/decisions/patterns/** 與 docs/tracking/pitfalls/**
                     （pattern-guard hook 的必讀件）裡形如 `path/to/file.py:symbol`
                     的錨點，該符號今天必須存在於該檔。**只驗存在性**——不驗行號、
                     不驗內容、不驗語意，所以沒有閾值、不會誤報。#616 當初寫「不建議
                     做 gate」，否決的是**驗行號**那條路（要容忍合理位移就得有閾值，
                     鬆了恆綠、緊了天天誤報）；「def X 還在不在」是另一條路，不在那
                     句話的涵蓋範圍內。scope 限縮到 hook 必讀件是實測決定的：全 docs/
                     有 2473 處帶行號的錨點 ⇒ gate 絕不能要求「不得有行號」；
                     docs/plans/ 的 27 組 path:symbol 更有 13 組符號已不存在——那是
                     **時點文件**（某季的設計稿），符號後來改名是正常的，納入等於要求
                     歷史文件跟著 code 走。限縮到必讀件時「找不到」＝0。
                     三條處理規則（都是實測撞出來的，不是預想）：(a) 圍欄內不驗——
                     two-layer-acl-asymmetric-deny.md 的 ```bash 區塊裡是 `grep -rn`
                     的逐字輸出重現，那些指標的用途是**舉證**不是導航，過期是正常的
                     甚至必要的，改掉等於偽造史料；(b) repo 外的檔跳過——
                     `binary.py:content_common` 是 Odoo 核心，「檔不存在」與「檔在、
                     符號不在」必須分開判；(c) 路徑必須唯一解析——repo 裡有兩個
                     `ai_assistant.py`（只有 controllers 那個有該符號），用 basename
                     取第一個命中會製造**純由解析策略產生的假陽性**（#606 的
                     memory-integrity「不讓同 basename 冒充入口」同型）。
                     ⚠ 只驗符號存在，**不驗 doc 的敘述與該符號的行為相符**。
                     依據 #626（處方出處 #616／#623／PR #625）
21. hermes-agent-013-seed  cloud_app_hermes_agent record 必須對齊 chart 0.1.3：
                     ``helm_chart_version`` 不得低於 `0.1.3`（floor，不是
                     pin——升版自由，降版回到 webui 時代才擋）、
                     ``helm_default_values`` 不得含 `webui` block 或
                     `dashboard.externalPath`。0.1.3 移除 webui container、
                     dashboard 改走 root URL（nginx `/` 直連），任一條件失效 ⇒
                     OAuth ``redirect_uri`` 打到 404 subpath（chart 用舊
                     subpath 轉發、平台仍組 root URL）。守線只鎖「block 不在」
                     與「版號不得倒退」這兩個最小必要條件；image pin /
                     dashboard 內容細節由 test_hermes_agent_seed_424 機械斷言。
                     依據 feat/hermes-agent-013-cleanup（chart 0.1.3 contract change）
22. security-rule-lifecycle  ir.rule 宣告的**出生地**與**退役名字**兩件事（#659 ①②，
                     兩個函式；同一個機制的兩端＝`ir_model_data.noupdate` 的 sticky 語意）：
                     (a) 任何 `<record model="ir.rule">` 落在 `noupdate="1"` 區塊即 fail。
                         noupdate 是 sticky 的：`-u` 不重寫欄位、把 record 搬到
                         `noupdate="0"` 區塊翻不了旗標、從 XML 刪掉也不會清除 ⇒ 規則
                         寫錯**只能下 migration DELETE 修**。這正是 #556 的可修復性
                         根因，而第 3 條 data-noupdate 只掃 `src/data/*.xml` 的**檔案
                         層**，`src/security/ir_rules.xml` 從來不在射程內——授權規則
                         全住在那裡。既有兩條（#658 的待辦）由 in-code grandfather
                         清單收錄，**雙向**對帳（修好了要求把那一行刪掉，否則同名
                         record 日後會無聲繼承豁免）。
                     (b) #556 已用 migration `18.0.1.0.122` DELETE 退役的兩個 xmlid
                         不得再有 `<record>` 宣告。帶回來的後果不對稱：既有環境的
                         migration 只跑一次不會再刪，fresh install 卻照 XML 重建
                         ⇒ 授權洞無聲重開、零測試會紅。判準是「有沒有 `<record id=…>`
                         宣告」而**不是**「檔案裡有沒有這個字串」——那兩個名字今天仍
                         合法出現在註解／migration／landing doc 裡。
                     ⚠ 只驗宣告位置與名字，**不驗規則的 domain 對不對**。
                     依據 docs/tracking/landings/556-workspace-access-authz-landing.md
23. workspace-access-rule-directions  `woow_paas_platform.workspace_access` 的
                     `perm_write` / `perm_create` / `perm_unlink` **各至少要有一條對
                     `base.group_user` 生效**的 ir.rule（#659 ③，#519 的地基）。缺任
                     一方向：`_get_rules` 對租戶查無 rule ⇒ `_compute_domain` 回空
                     domain ＝該方向**在規則層完全不設限**。這是最容易被「這條規則看
                     起來多餘」順手簡化掉、失效又完全無聲的一種。判準**與 ACL 脫鉤**
                     （縱深防禦；訊息會即時讀出 ACL 實值，但不寫死——寫死的版本印在
                     #556 修復前的樹上就是假話，#659 驗收 F3）。
                     🔴 判準刻意**不是**「這個 model 有沒有任一條
                     ir.rule」：`_get_rules` 的 SQL 只收 `r.global OR r.id IN (使用者
                     所屬 group 的 rule)`，所以只掛 `base.group_system` 的
                     `*_rule_admin_full` 對純 base.group_user 帳號完全不構成限制；本
                     repo 每個 model 都配一條 admin_full ⇒ 寬判準會把「洞開著」讀成
                     「有守」，而且補一條 admin_full 就能讓 gate 轉綠卻沒修任何東西
                     （#524 在 prod DB 實測過的假綠形態）。`perm_read` 刻意不納入：
                     #659 指定的是寫入面，且今天有 workspace_access_rule_member_read
                     ——寫在這裡免得下一個人以為讀方向也被守著。
                     ⚠ 只驗**存在性**，不驗 domain 寫得對不對。
                     依據 docs/tracking/landings/556-workspace-access-authz-landing.md
24. one-time-credential-render  後端一次性憑證 payload（src/controllers/paas.py 的
                     `_gateway_credentials`）交付的每一個 key，前端
                     `OneTimeCredentialsModal.js` 的 `buildOneTimeCredentials` 都要
                     讀得到（`data.<key>`）。沒讀到＝那一格永遠不顯示，而**一次性**
                     憑證錯過就沒有第二次（平台刻意不做回讀端點，只能 Reset 換發）。
                     與第 16 條 availability-reason-labels、第 7(c) 條同一個家族：
                     兩邊各自都「看起來對」，本 repo 沒有前端測試基礎設施，沒有任何
                     測試會變紅。這是 #672 的另一半——那次是後端整個沒交付 dashboard
                     URL（後端測試守得住），本條守的是「交付了但前端沒渲染」。
                     只查涵蓋（後端 ⊆ 前端），前端多讀一個後端不再送的 key 無害。
                     ⚠ 只驗 key 有沒有被讀到，**不驗文案對不對**——hint 指錯區、漏了
                     指路（#672 的直接成因）一個都抓不到，那留給人。
                     依據 docs/tracking/landings/530-gateway-landing-542-baseline.md
25. workspace-rule-directions  同第 23 條的判準，套在
                     `woow_paas_platform.workspace`（workspace **本體**，不是成員列）。
                     #658：這個 model 原本只有一條 perm_read+perm_write 合一、domain
                     無 role 條件的 rule，任何 guest 成員都能改 `owner_id`（訂閱帳單
                     partner ＋ max_per_partner 配額的錨）與 `slug`（K8s namespace／
                     公開 subdomain 的唯一輸入）。修復把讀寫拆開後，`perm_write` 只剩
                     `workspace_rule_owner_admin_manage` 一條在守 —— 它被「順手簡化」
                     掉的當下，租戶對 workspace 本體的寫入就完全不設限，而且無聲。
                     刻意寫成獨立一條而不是併進第 23 條：第 23 條的名字已經進了
                     `baselines/repo-lint-registry.txt` 這個只准長不准縮的 floor，改名
                     要跑 `--update-baseline`，而那支會一次重產**全部** baseline、
                     順手吸收掉別處新冒出來的違規（#613 記錄有案）。附加一條不會。
                     🔴 本條與第 23 條共用實作，兩者唯一的差別是 model 錨點那**一個
                     字串** ⇒ 把它複製貼上成第 23 條的值，本條就靜默退化成第 23 條的
                     複本（#675 覆核實測：workspace 本體四條 rule 全刪掉仍然 EXIT=0；
                     打錯字會紅、複製貼上不會）。所以「兩個錨點互異、且各自真的對得到
                     一條宣告出來的 ir.rule」是被機械檢查的前提，見
                     `_rule_direction_anchor_violations`。
                     依據 docs/tracking/landings/556-workspace-access-authz-landing.md
26. provision-sentinel-allowlist  #519 的 cloud_service 開閘符號
                     （`_INTERNAL_PROVISION` / `_internal_provision` /
                     `woow_internal_provision`）只准出現在 allowlist 列名的檔
                     （外加 `src/tests/`）。閘的不變式是「沒有任何 RPC 呼叫端建得出
                     cloud_service」；RPC 端偽造不了 sentinel（JSON 載不了物件身分），
                     所以剩下**唯一**的繞法是讓平台自己的碼替攻擊者開閘——一支新的
                     sudo'd controller 把呼叫者的 vals 往下送並開閘，閘就等於沒有，
                     而那件事不會讓任何測試變紅，只會讓 diff 多一行。
                     🔴 本條有兩個自身的假綠面，各配一個對照：(a) sentinel 若被改名，
                     判準的 regex 會掃不到任何東西而恆綠 ⇒ 定義檔
                     `src/models/cloud_service.py` 必須真的含有那幾個名字，否則本條
                     直接紅；(b) allowlist 不准養殭屍——列了卻已經不再開閘的檔要拿掉，
                     否則名單會慢慢長成「反正都在名單上」而失去審查意義。
                     依據 docs/decisions/519-cloud-service-provision-gate.md
27. sh-subscription-group-isolation  平台不得把第三方 `sh_subscription` 的
                     `group_user_sh_subscription` / `group_manager_sh_subscription`
                     用 `implied_ids` / `groups_id` 發給任何群組。#519 提案 B 的兩個
                     判準（訂閱的 `sh_partner_id` 與 `state` 租戶都改不動）錨定在
                     「租戶對 `sh.subscription.subscription` 零 ACL row」這個**第三方**
                     前提上；那個前提被一行 `implied_ids` 破壞掉時，B 會**無聲**失效
                     ——不會有任何測試變紅，因為 B 的測試驗的是守衛擋不擋得住，不是
                     租戶有沒有別的路可以走。
                     🔴 判準是**授予的形狀**不是「有沒有提到這個名字」：`_sub()` 的
                     docstring 寫著「read ACL 只發給這兩個群組」（那是 sudo traversal
                     存在的理由），回歸測試也具名引用它們來斷言租戶**沒有**被塞進去
                     ——把那些判成違規會逼人刪掉守線的說明文字。反過來，本條自己的
                     「零命中」可能是掃描範圍壞掉，所以配一個一定找得到的字串
                     （`base.group_user`）當存在性對照。
                     依據 docs/decisions/519-cloud-service-provision-gate.md

28. internal-ctx-key-registry  「context key 當內部呼叫憑證」的註冊表 gate
                     （#788，承 #536 岔路① 與普查 #787）。`/web/dataset/call_kw` 把
                     呼叫端送來的 context **整包**換進 env ⇒ 任何拿 context key 當
                     「這是內部呼叫」逃生門、而判定只看 truthy 的守衛，租戶一行 JSON
                     就能開閘（#522 在 18.0 上實測成功）。這個 bug class 已反覆出現
                     ≥5 次（#519／#522／#524／#536／#744 各修一批），而在本條之前
                     **沒有任何機制**會在第 6 次出現時變紅——runtime 的 WARNING 只在
                     既有 key 被偽造時才印，新寫的一把根本不經過它。
                     三條判準：(i) registry 上 status=enforced 的 key，所有生產讀取點
                     都要走 sentinel 身分判定（`is` / `is not`），不得有裸 truthy 布林
                     guard；(ii) 這些 key 在 `src/`（`src/tests/` 除外）不得出現
                     `key=True` / `{'key': True}` 之類 truthy 字面值——那是把閘退回可
                     偽造形狀的另一半，而且**無聲**（sentinel 判定不認 True，只會在
                     runtime 印一行 WARNING）；(iii) **涵蓋率**——任何被生產碼讀取且
                     當 truthy 布林 guard 用的 context key 都必須在 registry 上，可標
                     `pending`（已知未修，必附單號）／`not-security`（非安全決策）豁免，
                     但要寫理由。🔴 (iii) 才是本條的核心：少了它，新增的 key 不登記就
                     等於不受管，而「不登記」正是這個 bug class 每一次復發的形狀。
                     🔴 三條都是否定性判準（「找不到違規」），所以各配陽性對照：
                     (a) 一個 `context.get` 都沒掃到 ＝ 掃描器／範圍壞了，不是乾淨；
                     (b) enforced 條目的 sentinel 符號必須真的在 `defined_in` 定義、
                     登記的 reader 必須存在且真的做 `is` 比對、而且該 key 至少要有一個
                     判定點（#536 三把**沒有任何直接讀取點**，只經
                     `_is_sync_control(env, KEY)`——少了這格它們會恆綠）；
                     (c) 豁免條目的定義性質就是「現在是裸 truthy 閘」⇒ 掃描器必須找得
                     到它的布林 guard 讀取點，找不到＝掃描器壞了或這把已被修好（請改
                     status）。沒有 (c)，掃描器回空集合時 (iii) 永遠是綠的。
                     ⚠ 覆蓋邊界：只認 `<x>.context.get(K)` / `._context.get(K)` /
                     `.context[K]` 三種載體且 K 解得出字面值；只管 **truthy 布林閘**
                     ——#787 B 段的「值注入」型偽造面（`sa_unlinked_cf_policy_ids` /
                     `mcp_admin_password` / `bootstrap_owner_partner_id`）形態與修法都
                     不同，不在本條範圍，把本條的綠讀成「所有 context 偽造面都守住了」
                     是錯的。
                     依據 docs/decisions/519-cloud-service-provision-gate.md

29. access-gate-no-ledger-read  `filtered()` 的判準（以及它一層內呼叫到的
                     同檔方法）不得讀待回收帳本 `cf_access_policy_reap`
                     （`_pending_ids` / `_pending_for` /
                     `_access_teardown_payload_ids`）。#784 F6：為了「刪除時順手
                     清舊帳」而讓 gate 聯集帳本 ⇒ 只要 operator 回報過某顆 policy
                     刪不掉，那筆帳掛在帳本上（錨點還活著、dead-anchor cron 依設計
                     不收）⇒ 閘永遠回 True ⇒ SA 永久卡 `state='error'`，每輪
                     lifecycle cron 白打一次 operator。修法把「這列乾淨了沒」與
                     「這趟送哪些 id」拆成兩個方法，但那只是**命名**紀律：兩個名字
                     相近、語意相反、無型別區隔，而最危險的那格誤用是**靜默**的
                     （把 filter 換成 payload 版會選中每一條 route，然後因為
                     `_teardown_route_access()` 的「沒 id 就回 True」早退而完全沒有
                     輸出——不打 operator、不寫欄位、不留 log）。判準刻意不硬編方法
                     名；已知限制（只追一層、只追同檔）寫在函式 docstring。
30. tunnel-config-wire-shape  `PUT /api/tunnels/{tunnel_id}/config` 的兩端參數綁定
                     形狀必須一致，且必須是 body。#769：operator 端把 `hostname` /
                     `service_url` 宣告成裸 `str` ⇒ FastAPI 綁成 query param，而平台
                     client 把同兩個欄位組成 dict 走 JSON body 送 ⇒ **每一次呼叫必回
                     422**（實跑取證：`loc` 是 `["query","hostname"]`）。破裂期間兩端
                     的測試**全部是綠的**：operator 的 `tests/` 從來沒有一支打過這個
                     端點，平台的 `test_subscription_lifecycle.py` 用 `MagicMock()`
                     當 client、HTTP 序列化整層被 mock 掉。唯一受害者是 SmartHome 的
                     停權／復權，而它的例外被 `except Exception` 吞成 `state='error'`
                     ⇒ 自上線起零訊號。
                     🔴 這條**只能**住在 repo-lint：兩端住在不同的部署單元，
                     `extra/paas-operator/` 不進 paas-odoo image（`Dockerfile.dev`
                     只 `COPY src/`），fastapi 也不在 Odoo 的 venv 裡 ⇒ 沒有任何一個
                     測試執行環境同時看得到兩端。放進 `src/tests/` 再對缺檔 skip 就
                     落進 #544／#684 家族（skipped 算進 testsRun 卻不出現在 summary，
                     「沒跑」與「通過」輸出完全相同）。
                     ⚠ 只比對**綁定的邊**（body／query），不驗型別、不驗 required、
                     不驗語意——把本條的綠讀成「這條 wire 一定會通」是錯的。行為那
                     兩腿在 extra/paas-operator/tests/test_tunnel_config_wire_769.py
                     與 src/tests/test_tunnel_config_wire_769.py。其餘已知限制
                     （只認 `@router.<m>("<path>")` 註冊形態、client 只認字面值
                     dict）寫在函式 docstring。

用法：

  scripts/audit/repo-lint.py --all                # 全 repo 掃描（CI / merge 前手動）
  scripts/audit/repo-lint.py --update-baseline    # 接受現況為新 baseline
  scripts/audit/repo-lint.py --hook               # PostToolUse hook 模式（stdin 吃 hook JSON，只掃該檔）

hook 模式輸出 {"decision":"block","reason":...} 讓 model 立即自我修正；CLI 模式違規 exit 1。
Fail-open：hook 模式下任何內部錯誤 → exit 0，絕不擋開發。
"""
import ast
import csv
import glob
import io
import json
import os
import re
import sys
import tokenize
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASELINE_DIR = os.path.join(SCRIPT_DIR, "baselines")
SUDO_BASELINE = os.path.join(BASELINE_DIR, "controller-sudo.txt")
NOUPDATE_BASELINE = os.path.join(BASELINE_DIR, "data-noupdate.txt")
SEED_TOPOLOGY_BASELINE = os.path.join(BASELINE_DIR, "seed-topology.txt")

SUDO_DOC = "docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md"
OWL_DOC = "docs/tracking/pitfalls/owl-template-crash-class-syntax.md"
NOUPDATE_DOC = "docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md"
REUSE_VALUES_DOC = "docs/decisions/patterns/helm-reset-values-unified-pipeline.md"
SEED_TOPOLOGY_DOC = "docs/decisions/patterns/seed-topology-invariants.md"
CAPABILITY_GATE_DOC = "docs/decisions/patterns/capability-gate-single-source-field.md"
HIDDEN_CHAIN_DOC = "docs/decisions/patterns/hidden-template-presentation-chain.md"
RESET_LADDER_DOC = "docs/decisions/patterns/reset-async-budget-ladder.md"
ASSERTLOGS_LEVEL_DOC = (
    "docs/tracking/pitfalls/assertlogs-string-level-shadowed-by-odoo.md")

# #461 reset 預算階梯：三層各自的常數位置（跨 repo 內兩條發版線，故一定要跨檔算）。
RESET_LADDER_FRONTEND = "src/static/src/paas/services/cloud_service.js"
RESET_LADDER_PLATFORM = "src/services/paas_operator.py"
RESET_LADDER_OPERATOR = "extra/paas-operator/src/api/releases.py"
RESET_LADDER_HELM = "extra/paas-operator/src/services/helm.py"
RESET_LADDER_FILES = (
    RESET_LADDER_FRONTEND, RESET_LADDER_PLATFORM,
    RESET_LADDER_OPERATOR, RESET_LADDER_HELM,
)

# #471：既有實例的隱藏判準（實例層）vs 只擋上架曝光的舊旗標（曝光層）。
CONCEAL_FIELD = "conceal_instances"
CONCEAL_WRONG_FIELD = "is_hidden"

# #29：mixin 內部的 subscription traversal 唯一入口是 `_sub()`（sudo）。
MIXIN_SUDO_FILE = "src/models/subscription_lifecycle_mixin.py"

OWL_ALLOWED_MODIFIERS = {"stop", "prevent", "self", "capture", "synthetic"}

# 前端能力 gate 的唯一判準欄位（後端 payload 直接鏡射 template 欄位，#460 S1）。
ADMIN_GUI_FIELD = "has_admin_gui"
# 「哪顆按鈕」的 XML 端錨點：handler 名比 t-if 的 getter 名穩定（getter 可以更名，
# 但按鈕綁的 action 更名就是改功能本身）。
ADMIN_GUI_BUTTON_HANDLER = "resetAdminPassword"
# #530 gateway 憑證交付的能力欄位（payload 鏡射 service._gateway_public_url() 非空）。
GATEWAY_FIELD = "has_gateway"
# 「哪段文案」的 XML 端錨點：向租戶宣告會發／已發 gateway 憑證的字面文案。錨在文案
# 本身而非某個元素 id——這條規則要守的就是「這句話會不會對不該看到的人出現」。
GATEWAY_COPY_RE = re.compile(r"gateway\s+api\s+key", re.IGNORECASE)
# #746 CS Access Control：Access 頁籤的能力欄位（payload 鏡射方案的席位上限，
# `_get_subscription_info()` 直接搬 sh_subscription_plan_id.ac_max_access_seats）。
ACCESS_SEATS_FIELD = "ac_max_access_seats"
# 「哪顆頁籤」的 XML 端錨點：同 ADMIN_GUI_BUTTON_HANDLER 的理由，錨在 handler
# 呼叫（`setTab('access')`）而非 getter 名或按鈕文字——前兩者可以更名，改掉這個
# 字面就是改掉「這顆頁籤切到哪裡」本身。
ACCESS_TAB_HANDLER_RE = re.compile(r"setTab\(\s*['\"]access['\"]\s*\)")
# t-if 表達式裡不算 component member 的字（OWL 運算子 / literal / 內建 namespace）。
OWL_EXPR_NON_MEMBERS = {
    "and", "or", "not", "in", "true", "false", "null", "undefined",
    "props", "state", "this", "new", "typeof", "env",
}

# seed 拓撲數字一變，這些位置就要一起被檢視。刻意記「檔案＋具名位置」而非行號
# ——行號會隨無關的編輯腐化，符號名不會。
SEED_TOPOLOGY_DEPENDENTS = (
    "src/tests/test_plan_group_keys_368.py：_seeded_link_refs() 對 links.xml 做集合"
    "等值（拓撲成長不必改），但若這次是「刻意隱藏已掛 template 的 base」，"
    "必須顯式加進同檔的 _INTENTIONALLY_HIDDEN 並在 PR 說明理由。",
    "src/tests/test_subscription_tax_clear_366.py："
    "TestSeedProductCorpusIntact._MIN_SEEDED_PRODUCTS（backstop 下界，seed 成長時調高）。",
    "src/tests/test_plan_variants_generated.py：GENERATED 檔，不要手改——"
    "要改請改 scripts/seed/generate_plan_variants.py 的 _TESTS_TEMPLATE 後重跑 generator。",
    "scripts/seed/test_generate_plan_variants.py："
    "TestClassify.test_counts_per_family 的 family 分佈（每 family 幾個變體）。",
    "docs/plans/2026-Q3/2026-07-19-cloud-services-220-manifest.md："
    "變體的單一事實源，改價／增減變體從這裡開始。",
)

_CYCLE_SUFFIX_RE = re.compile(r"_(monthly|yearly)$")


def repo_root():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(SCRIPT_DIR))


def read_baseline_pairs(path):
    """讀 `value<TAB>relpath` 形式的 baseline，回 dict。"""
    result = {}
    if not os.path.exists(path):
        return result
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                result[parts[1]] = parts[0]
    return result


_SUDO_CALL_RE = re.compile(r"\.sudo\(")


def _blank_range(out, src, start, end):
    """把 out[start:end] 的非換行字元換成空白（換行留著才不會位移行號）。"""
    for i in range(start, end):
        if src[i] != "\n":
            out[i] = " "


# f-string 的 replacement field 裡是**真的**呼叫點（``f"{rec.sudo().name}"``），必須被算到。
# Python 3.12 起 tokenize 把 f-string 拆成 FSTRING_START／FSTRING_MIDDLE／FSTRING_END，
# 字面部分才是 FSTRING_MIDDLE、field 內是一般 token ⇒ 留白 STRING 與 FSTRING_MIDDLE
# 自然就保留了 field。3.11 以下整個 f-string 是單一 STRING token，直接留白會把 field
# 裡的真呼叫一起吃掉 ⇒ **靜默少算**，正是本 check 要防的假陰性。
# 這裡 fail-loud 而不是寫一條 ≤3.11 相容分支：本專案兩個執行環境都 >= 3.12
# （CI 的 docker.gitea.com/runner-images:ubuntu-latest 實測 3.12.3），一條在任何實際
# 路徑上都跑不到的分支，等於在 gate 自己的實作裡留一段沒有東西會驗的程式碼（#576）。
_MIN_PYTHON = (3, 12)


def _blank_python_comments_and_strings(src):
    """把 Python 的 COMMENT／STRING／FSTRING_MIDDLE 原地換成**等長空白**。

    註解與字串字面裡的 ``.sudo(`` 不是呼叫點——docstring 在描述用法、錯誤訊息
    在引述寫法，兩者在語意上都不生效。一併留白字串不會漏掉動態呼叫：動態呼叫
    （``getattr(rec, "sudo")()``）根本不長成 ``.sudo(`` 這個字面。

    ⚠ 必須逐 token 換成**等長**空白，不可用 " ".join(tokens) 重組來源：後者會在
    ``record.sudo(`` 中間插入空白，regex 全部對不上，量出來每個 controller 都是
    0——而 0 看起來像個合理答案（「所以全都是註解？」）。留白策略與既有的
    _strip_xml_comments 相同，行號與所有非註解字元的相對位置都保留。

    語法錯誤的檔會讓 tokenize 丟 TokenError／IndentationError／SyntaxError：
    **往外拋**，由呼叫端 fail-loud。不可 except 後退回全文計數——那會讓一個語法
    錯的檔靜默回到「註解也算呼叫點」的舊行為。
    """
    if sys.version_info < _MIN_PYTHON:
        raise RuntimeError(
            "repo-lint 需要 Python >= %d.%d 才能正確計數 controller 的 .sudo( 呼叫點"
            "（目前是 %d.%d.%d）。3.11 以下 tokenize 把整個 f-string 當成單一 STRING "
            "token，留白時會連 replacement field 裡的真呼叫一起吃掉 ⇒ 靜默少算，正是 "
            "#576 要修的假陰性。本專案 CI（docker.gitea.com/runner-images:ubuntu-latest）"
            "與開發環境都已 >= %d.%d，故此處 fail-loud 而不做相容降級。"
            % (_MIN_PYTHON + sys.version_info[:3] + _MIN_PYTHON))
    line_starts = [0]
    for idx, ch in enumerate(src):
        if ch == "\n":
            line_starts.append(idx + 1)
    out = list(src)

    def offset(pos):
        row, col = pos
        return line_starts[row - 1] + col

    blanked = (tokenize.COMMENT, tokenize.STRING, tokenize.FSTRING_MIDDLE)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in blanked:
            _blank_range(out, src, offset(tok.start), offset(tok.end))
    return "".join(out)


def controller_sudo_count(fp):
    """單檔的 ``.sudo(`` 呼叫點數——check 與 --update-baseline 的**唯一**入口。

    兩邊共用同一個函式，是為了讓「baseline 與 check 對同一份檔案得出不同數字」
    在結構上不可能發生。#576 之前是兩處各寫一次 re.findall，只改一邊就會漂移，
    症狀是「跑完 --update-baseline 之後 check 仍然紅」。
    """
    with open(fp, encoding="utf-8", errors="replace") as f:
        src = f.read()
    return len(_SUDO_CALL_RE.findall(_blank_python_comments_and_strings(src)))


# ---------------------------------------------------------------- checks

def check_controller_sudo(root, only_file=None):
    """回傳 violations list；baseline 是每檔 .sudo( 次數上限。"""
    baseline = {k: int(v) for k, v in read_baseline_pairs(SUDO_BASELINE).items()}
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/controllers/*.py")))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        try:
            count = controller_sudo_count(fp)
        except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
            violations.append(
                "%s：tokenize 失敗（%s）——這個檔語法有問題，.sudo( 呼叫點無從判定。"
                "本 check 刻意不退回全文計數（那會讓語法錯的檔靜默回到「註解也算呼叫點」"
                "的舊行為，是 #576 修掉的缺陷）。先修語法再跑一次。" % (rel, exc))
            continue
        allowed = baseline.get(rel, 0)
        if count > allowed:
            violations.append(
                "%s：.sudo( 呼叫點 %d 個 > baseline %d 個。controller layer 的 sudo() 會繞過 ir.rule，"
                "是 B1-class data leak 的主要來源（見 %s）。若這個新 sudo() 有正當理由"
                "（如 platform public-read），請補 HttpCase cross-user negative test，"
                "並跑 scripts/audit/repo-lint.py --update-baseline 把 justification 寫進 commit message。"
                % (rel, count, allowed, SUDO_DOC))
    return violations


def current_sudo_counts(root):
    """--update-baseline 的來源；判準與 check_controller_sudo 共用同一個入口。

    這裡刻意**不** catch tokenize 例外：語法錯的檔要讓 --update-baseline 直接炸，
    不能靜默寫出一個用舊判準（含註解）算出來的 baseline。
    """
    counts = {}
    for fp in sorted(glob.glob(os.path.join(root, "src/controllers/*.py"))):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        counts[rel] = controller_sudo_count(fp)
    return counts


def check_owl_template(root, only_file=None):
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/static/src/**/*.xml"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        try:
            tree = ET.parse(fp)
        except ET.ParseError as exc:
            violations.append("%s：XML parse error（%s）——連 well-formed 都沒過，Odoo 載入會炸。" % (rel, exc))
            continue
        for elem in tree.iter():
            tag = elem.tag
            if not isinstance(tag, str) or "}" in tag:
                continue
            is_component = tag[:1].isupper()
            for attr in elem.attrib:
                if is_component and (attr == "t-att" or attr.startswith("t-att-") or attr.startswith("t-attf-")):
                    violations.append(
                        "%s：component <%s> 上用了 %s。OWL component 的 prop 直接吃 expression"
                        "（寫 name=\"expr\" 不加 t-att-），t-att- 只給 HTML element 用；"
                        "這會在 runtime 摧毀整個 OWL root → 全站白屏（三層測試都攔不到，見 %s）。"
                        % (rel, tag, attr, OWL_DOC))
                if attr.startswith("t-on-") and "." in attr:
                    mods = attr.split(".")[1:]
                    bad = [m for m in mods if m not in OWL_ALLOWED_MODIFIERS]
                    if bad:
                        violations.append(
                            "%s：<%s> 的 %s 用了 OWL 不支援的 modifier %s（OWL≠Vue，只支援 "
                            ".stop/.prevent/.self/.capture；按鍵判斷一律在 handler 內讀 ev.key，見 %s）。"
                            % (rel, tag, attr, "/".join(bad), OWL_DOC))
    return violations


_NOUPDATE_DECL_RE = re.compile(r"<data[^>]*\bnoupdate=[\"']1[\"']")


def data_file_declares_noupdate(fp):
    """單檔是否有**生效中**的 ``<data noupdate="1">``。

    比對前先剝 XML 註解（沿用既有的 _strip_xml_comments，它已經是「留白不刪除」
    的正確策略）：被 ``<!-- -->`` 包住的舊寫法在語意上不生效。把它當成生效宣告，
    會逼人為一個**不存在的** sticky 風險去更新 baseline，而 baseline 一旦先被撐大，
    日後真的有人在該檔加 ``noupdate="1"`` 時 gate 就不會響了——假陽性當場製造出
    一個假陰性（#576）。

    check 與 --update-baseline 共用這**唯一**入口，讓「baseline 與 check 對同一份
    檔案得出不同判定」在結構上不可能發生（同 controller_sudo_count 的理由）。
    """
    with open(fp, encoding="utf-8", errors="replace") as f:
        return _NOUPDATE_DECL_RE.search(_strip_xml_comments(f.read())) is not None


def check_data_noupdate(root, only_file=None):
    baseline = set(read_baseline_pairs(NOUPDATE_BASELINE))
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/data/*.xml")))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if data_file_declares_noupdate(fp) and rel not in baseline:
            violations.append(
                "%s：新增了 noupdate=\"1\" 的 data 檔。noupdate flag 會寫進 ir.model.data 變 sticky，"
                "其他 data 檔對同一 record 的 -u 更新會被靜默 skip（fresh install 綠 ≠ 升級路徑綠，見 %s）。"
                "若確定要 noupdate（如 cron / admin-curated seed），跑 --update-baseline 收錄。"
                % (rel, NOUPDATE_DOC))
    return violations


def current_noupdate_files(root):
    """--update-baseline 的來源；判準與 check_data_noupdate 共用同一個入口。"""
    files = []
    for fp in sorted(glob.glob(os.path.join(root, "src/data/*.xml"))):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if data_file_declares_noupdate(fp):
            files.append(rel)
    return files


def _reuse_values_true_lines(src):
    """回傳出現 reuse_values=True 的行號 list（AST 判定，避開字串/註解/docstring）。

    兩種形態都抓：
      1. 呼叫點 keyword arg：``foo(..., reuse_values=True)``
      2. 函式 signature default：``def f(..., reuse_values=True)``（client default 亦屬修復對象）

    用 AST 而非 regex——test 的 assert 訊息字串 '不得再傳 reuse_values=True' 這種
    描述文字會被 regex 誤報，AST 只認真正的 keyword/arg 節點。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # 不 parse 的檔略過（fail-open，對齊本工具哲學）
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg == "reuse_values"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True):
                    lines.append(kw.value.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            pairs = list(zip(a.args[len(a.args) - len(a.defaults):], a.defaults))
            pairs += list(zip(a.kwonlyargs, a.kw_defaults))
            for arg, default in pairs:
                if (default is not None and arg.arg == "reuse_values"
                        and isinstance(default, ast.Constant) and default.value is True):
                    lines.append(arg.lineno)
    return sorted(set(lines))


def check_reuse_values(root, only_file=None):
    """平台 code（src/**/*.py）出現 reuse_values=True 即 fail。

    #411：helm --reuse-values 讓 render 用舊 release 的 coalesced values 當 base，
    chart 升版新增的預設值永不生效（既存 release 升級靜默漏 chart 新預設，Gap 2
    bug class）。平台既存 release 升級一律走全量 values 組裝 ＋ reset_values=True。
    依據 docs/decisions/patterns/helm-reset-values-unified-pipeline.md
    """
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        for line_no in _reuse_values_true_lines(src):
            violations.append(
                "%s:%d：出現 reuse_values=True。#411：helm --reuse-values 讓 chart 升版新增"
                "預設永不生效（既存 release 升級靜默漏 chart 新預設，Gap 2 bug class）。平台既存"
                " release 升級一律走全量 values 組裝 ＋ reset_values=True（見 %s）。"
                % (rel, line_no, REUSE_VALUES_DOC))
    return violations


def _fold_int_constants(src, seed=None):
    """回傳 module 層 ``NAME = <int 算式>`` 的 {name: value}（小型常數折疊）。

    只認 int 字面值、以及由已解析常數／字面值組成的 ``+`` / ``*`` 算式——夠用來讀
    `2 * RESET_HELM_TIMEOUT_SECONDS + HELM_CLI_OVERHEAD_SECONDS` 這種預算算式，
    又不需要 import 模組（operator 的套件不在平台的 sys.path 上，import 不可行）。
    解不開的 assignment 直接略過（缺鍵由呼叫端 fail-loud，不靜默當 0）。

    ``seed`` 供跨檔的名字：算式可以引用**別的模組 import 進來的**常數
    （releases.py 的 subprocess 預算就引用 helm.py 的 HELM_CLI_OVERHEAD_SECONDS），
    那個名字在本檔沒有 assignment，必須由呼叫端先解好餵進來。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    resolved = dict(seed or {})

    def value_of(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            return resolved.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
            left, right = value_of(node.left), value_of(node.right)
            if left is None or right is None:
                return None
            return left + right if isinstance(node.op, ast.Add) else left * right
        return None

    for node in tree.body:  # module 層即可：預算常數不該藏在 function 裡
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        val = value_of(node.value)
        if val is not None:
            resolved[target.id] = val
    return resolved


def _read_repo_file(root, rel):
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def check_reset_budget_ladder(root):
    """#461：reset 的三層 timeout 預算必須嚴格外大於內，且算式自洽。

    為什麼要一條 gate 而不是註解：#461（F-449-3）的根因就是「前端 30s / 平台 120s /
    operator ≈560s 三個預算**彼此不知道對方**」——每一層各自都看起來合理，合起來
    卻保證冷 image cache 時必逾時。散文攔不住這件事，它已經被攔失敗過一次了。

    兩層算式各自釘：
      (a) subprocess == 2 * helm --timeout + CLI 開銷
          `--timeout` 是 per-operation，`--atomic` 失敗時 helm 會再跑一次帶 wait 的
          rollback ⇒ 只給 n+slack 會在 rollback 進行中 SIGKILL helm ⇒ release 卡
          pending-rollback、Secret 停在沒人持有的新密碼（真機實測，見 doc）。
      (b) hard cap == subprocess + exec + cleanup + kubectl slack
    然後跨層：前端 > 平台 > operator hard cap。

    依據 docs/decisions/patterns/reset-async-budget-ladder.md
    """
    violations = []

    def fail(msg):
        violations.append("%s（見 %s）" % (msg, RESET_LADDER_DOC))

    js = _read_repo_file(root, RESET_LADDER_FRONTEND)
    operator_src = _read_repo_file(root, RESET_LADDER_OPERATOR)
    platform_src = _read_repo_file(root, RESET_LADDER_PLATFORM)
    helm_src = _read_repo_file(root, RESET_LADDER_HELM)
    if js is None or operator_src is None or platform_src is None or helm_src is None:
        # 檔案被搬走／改名：不能靜默放行，否則階梯守衛整條失效。
        fail("預算階梯的來源檔缺失（預期 %s）" % ", ".join(RESET_LADDER_FILES))
        return violations

    front = re.search(
        r"^const\s+RESET_REQUEST_TIMEOUT_MS\s*=\s*(\d+)\s*;", js, re.MULTILINE)
    helm = _fold_int_constants(helm_src)
    # operator 的 subprocess 算式引用 helm.py import 進來的開銷常數，故先解 helm.py
    # 再把它當 seed 餵進去（否則那條算式解不開，會被誤報成「缺常數」）。
    op = _fold_int_constants(operator_src, seed={
        k: v for k, v in helm.items() if k == "HELM_CLI_OVERHEAD_SECONDS"
    })
    plat = _fold_int_constants(platform_src)

    needed_op = (
        "RESET_HELM_TIMEOUT_SECONDS", "RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS",
        "RESET_EXEC_TIMEOUT_SECONDS", "RESET_CLEANUP_EXEC_TIMEOUT_SECONDS",
        "RESET_KUBECTL_SLACK_SECONDS", "RESET_HARD_CAP_SECONDS",
    )
    missing = [k for k in needed_op if k not in op]
    if missing:
        fail("%s 缺少預算常數 %s" % (RESET_LADDER_OPERATOR, ", ".join(missing)))
    if "HELM_CLI_OVERHEAD_SECONDS" not in helm:
        fail("%s 缺少 HELM_CLI_OVERHEAD_SECONDS" % RESET_LADDER_HELM)
    if "RESET_CONFIG_TIMEOUT" not in plat:
        fail("%s 缺少 RESET_CONFIG_TIMEOUT" % RESET_LADDER_PLATFORM)
    if front is None:
        fail("%s 缺少 RESET_REQUEST_TIMEOUT_MS" % RESET_LADDER_FRONTEND)
    if violations:
        return violations

    n = op["RESET_HELM_TIMEOUT_SECONDS"]
    overhead = helm["HELM_CLI_OVERHEAD_SECONDS"]
    subprocess_budget = op["RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS"]
    hard_cap = op["RESET_HARD_CAP_SECONDS"]
    platform_budget = plat["RESET_CONFIG_TIMEOUT"]
    frontend_seconds = int(front.group(1)) / 1000

    if subprocess_budget != 2 * n + overhead:
        fail(
            "RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS=%s 不等於 2×%s（helm --timeout）"
            "＋%s（CLI 開銷）=%s。`--atomic` 失敗要吃兩次 --timeout（upgrade wait ＋"
            "自動 rollback wait），少算會在 rollback 進行中 kill helm ⇒ release 卡"
            " pending-rollback、Secret 停在沒人持有的新密碼"
            % (subprocess_budget, n, overhead, 2 * n + overhead))

    parts_sum = (
        subprocess_budget + op["RESET_EXEC_TIMEOUT_SECONDS"]
        + op["RESET_CLEANUP_EXEC_TIMEOUT_SECONDS"] + op["RESET_KUBECTL_SLACK_SECONDS"]
    )
    if hard_cap != parts_sum:
        fail("RESET_HARD_CAP_SECONDS=%s 不等於各子步之和 %s（改了子步沒同步硬上限）"
             % (hard_cap, parts_sum))

    if not platform_budget > hard_cap:
        fail(
            "平台 RESET_CONFIG_TIMEOUT=%ss 必須 > operator RESET_HARD_CAP_SECONDS=%ss"
            "；否則平台會在 operator 還沒答完就斷線 ⇒ 密碼不落庫、租戶得重按（#461 原症狀）"
            % (platform_budget, hard_cap))
    if not frontend_seconds > platform_budget:
        fail(
            "前端 RESET_REQUEST_TIMEOUT_MS=%ss 必須 > 平台 RESET_CONFIG_TIMEOUT=%ss"
            "；否則瀏覽器先 abort ⇒ 後端修好了租戶也看不到成功（#459 實測成功案例"
            "也要 49.9s／76.7s）" % (frontend_seconds, platform_budget))
    return violations


def _is_os_environ(node):
    """node 是 ``os.environ`` 或 ``environ``（from os import environ）嗎？"""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name) \
            and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _env_unconditional_cleanup_lines(src):
    """回傳「把 os.environ 的刪除方法直接當 cleanup callable 傳」的行號。

    只認**直接參照**（``addCleanup(os.environ.pop, KEY, None)``）。repo 既有的
    ``_set_env`` / ``_env_guard`` 是 lambda / 內層函式先存 prev 再條件還原 ——
    語意正確，不在此列，故誤報空間趨近零。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # 語法壞掉自有 Odoo 載入會炸，這裡不重複報
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("addCleanup", "addClassCleanup"):
            continue
        first = node.args[0]
        if (isinstance(first, ast.Attribute)
                and first.attr in ("pop", "__delitem__", "clear")
                and _is_os_environ(first.value)):
            bad.append(node.lineno)
    return sorted(bad)


def check_test_env_restore(root, only_file=None):
    """src/tests/**/*.py 不得用 ``addCleanup(os.environ.pop, ...)`` 還原 env。

    #436：這個形狀是**無條件刪除**，不是「還原原值」。測試跑完把**外部注入**的
    env（K8s Secret 注入的 AGENT_RUNTIME_NAMESPACE_SECRET）整個刪掉，字母序在後的
    測試讀不到就 fail-closed raise —— 單獨跑全綠、只有全套跑才紅，而且紅在無辜的
    test 名下（典型假綠家族：綠不代表對，是順序剛好）。
    """
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/tests/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        for line_no in _env_unconditional_cleanup_lines(src):
            violations.append(
                "%s:%d：addCleanup(os.environ.pop, ...) 是**無條件刪除**而非還原原值，"
                "會把外部注入的 env 洗掉，害字母序在後的測試 fail-closed raise（#436，"
                "單獨跑綠／全套跑紅、且紅在無辜的 test 名下）。改用 patch.dict —— "
                "原本沒有就刪、原本有就還原：\n"
                "          patcher = patch.dict(os.environ, {KEY: VALUE})\n"
                "          patcher.start()\n"
                "          self.addCleanup(patcher.stop)\n"
                "        （test method 內可直接用 with patch.dict(...) context manager；"
                "需要條件式還原的多變數情境見 test_agent_pod_phase_d_hardening.py 的 "
                "_env_guard。）" % (rel, line_no))
    return violations


# #492：odoo/netsvc.py:338 有 `logging.addLevelName(logging.RUNBOT, "INFO")`
# 且 `logging.RUNBOT = 25`。addLevelName 會**雙向**寫表，於是 stdlib 的
# `logging._nameToLevel['INFO']` 被由 20 覆寫成 25。而 unittest 的
# `_log.py:37` 正是 `self.level = logging._nameToLevel.get(level, level)`
# ⇒ `level='INFO'` 在 Odoo 進程裡實際是 setLevel(25)，把所有 `_logger.info()`
# （20）擋在門外。（不傳 level 反而安全：`_log.py:39` 用的是數值 logging.INFO。）
# 只列**確定被 Odoo 覆寫**的名字：其餘 level 名的字串形式仍與數值等價，不必為了
# 一致性去改動無辜的既有測試（本 repo 現存的 'DEBUG' / 'WARNING' 用法沒有問題）。
ASSERTLOGS_SHADOWED_LEVELS = {"INFO": (25, 20)}


def _assertlogs_string_level_hits(src):
    """回 [(lineno, level 名)]：assertLogs/assertNoLogs 用了被覆寫的字串 level。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None)
        if name not in ("assertLogs", "assertNoLogs"):
            continue
        level = next((kw.value for kw in node.keywords if kw.arg == "level"), None)
        if level is None and len(node.args) >= 2:
            level = node.args[1]        # assertLogs(logger, level) 的 positional 形式
        if (isinstance(level, ast.Constant) and isinstance(level.value, str)
                and level.value.upper() in ASSERTLOGS_SHADOWED_LEVELS):
            hits.append((level.lineno, level.value.upper()))
    return hits


def check_assertlogs_numeric_level(root, only_file=None):
    """src/tests/**：``assertLogs`` 的 level 不得用字串 ``'INFO'``（#492）。

    這條守的是**恆紅／恆綠的自我否定斷言**——不是被測行為，而是斷言本身失效：

    * ``assertLogs(logger, level='INFO')`` 在 Odoo 進程裡等於 setLevel(**25**)
      （見 ASSERTLOGS_SHADOWED_LEVELS 上方的成因），於是任何 `_logger.info()`
      都捕捉不到 → 測試恆紅（`no logs of level INFO or higher triggered`），
      **且紅得像是被測 code 掉了 log**，實際上生產行為完好；
    * 反向同樣危險：`assertNoLogs(..., level='INFO')` 會漏放 20–24 級的 log，
      恆綠、零鑑別力。

    #492 就是前者：#461 加的 `test_chart_version_drift_is_logged` 從**引入當下
    就紅**（在 59e17eb2 上實測同一訊息），一路帶到 develop 破掉 0F/0E 基線。
    改用數值 ``logging.INFO`` 即可——不經過那張被覆寫的名稱表。
    """
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/tests/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        for line_no, level_name in _assertlogs_string_level_hits(src):
            shadow, real = ASSERTLOGS_SHADOWED_LEVELS[level_name]
            violations.append(
                "%s:%d：assertLogs/assertNoLogs 的 level 用了字串 '%s' —— Odoo 的 "
                "odoo/netsvc.py `logging.addLevelName(logging.RUNBOT, \"INFO\")`"
                "（RUNBOT=%d）覆寫了 stdlib 的 `logging._nameToLevel`，而 unittest 的 "
                "_log.py:37 正是查那張表 ⇒ 這個字串被解析成 %d 而非 %d："
                "`_logger.%s()` 全部落在門檻外，assertLogs 恆紅（訊息長得像被測 code "
                "掉了 log，實際上生產行為好好的）、assertNoLogs 恆綠。"
                "改傳**數值**：level=logging.%s。依據 %s"
                % (rel, line_no, level_name, shadow, shadow, real,
                   level_name.lower(), level_name, ASSERTLOGS_LEVEL_DOC))
    return violations


def _field_value(field):
    """讀 <field> 的值：eval 屬性優先，否則 text。"""
    if field.get("eval") is not None:
        return field.get("eval").strip()
    return (field.text or "").strip()


def _record_fields(record):
    return {f.get("name"): _field_value(f) for f in record.findall("field")}


def _is_true(value):
    return value in ("True", "1")


def seed_topology_counts(root):
    """純靜態 parse seed XML，回六個拓撲數（絕不碰 DB）。

    base / 變體的區分用語意（ac_max_access_seats > 0 ＝席數變體）而非檔名，
    這樣把變體寫進錯的檔也會被算出來。
    """
    data_dir = os.path.join(root, "src/data")
    plan_files = sorted(glob.glob(os.path.join(data_dir, "subscription_products*.xml")))
    link_files = sorted(glob.glob(os.path.join(data_dir, "subscription_template_links*.xml")))

    base_plans, variant_plans, sub_products = 0, 0, 0
    hidden_plan_xmlids = set()
    for fp in plan_files:
        for rec in ET.parse(fp).getroot().findall(".//record"):
            model, xmlid = rec.get("model"), rec.get("id")
            fields = _record_fields(rec)
            if model == "sh.subscription.plan":
                seats = fields.get("ac_max_access_seats") or "0"
                if seats.isdigit() and int(seats) > 0:
                    variant_plans += 1
                else:
                    base_plans += 1
                if _is_true(fields.get("storefront_hidden")):
                    hidden_plan_xmlids.add(xmlid)
            elif model == "product.product" and _is_true(fields.get("sh_product_subscribe")):
                sub_products += 1

    # links.xml（不含 *_variants）＝ base 掛 template 的權威來源：<data noupdate="0">
    # ＋全部 (6, 0, [...]) 全量 reset ⇒ DB 鏡射檔案。變體用 (4,) append，不入 base 拓撲。
    refs = set()
    for fp in link_files:
        if fp.endswith("_variants.xml"):
            continue
        for rec in ET.parse(fp).getroot().findall(".//record"):
            for field in rec.findall("field"):
                if field.get("name") == "subscription_plan_ids":
                    refs |= set(re.findall(r"ref\('([^']+)'\)", field.get("eval") or ""))

    visible = refs - hidden_plan_xmlids
    return {
        "base_plans": base_plans,
        "variant_plans": variant_plans,
        "subscription_products": sub_products,
        "linked_bases": len(refs),
        "visible_linked_bases": len(visible),
        "tiers": len({_CYCLE_SUFFIX_RE.sub("", ref) for ref in refs}),
    }


def check_seed_topology(root, only_file=None):
    """seed 拓撲六數對 baseline；一變就 fail，並附「該一起檢視的位置」清單。

    #433：成因不是「有人寫了硬編值」，而是「改 seed 時 bump 了 3 個持有計數的
    檔案、漏掉 2 個，因為沒有共用清單」。這條 gate 把「三週後 CI 才紅、且紅在
    無關的 test 名下」提前成「改 seed 當下就被擋、附帶待檢清單」。
    """
    baseline = read_baseline_pairs(SEED_TOPOLOGY_BASELINE)
    if not baseline:
        return []  # 尚未建立 baseline → 不擋（fail-open，對齊本工具哲學）
    try:
        counts = seed_topology_counts(root)
    except (ET.ParseError, OSError):
        return []  # seed XML 壞掉自有 data-noupdate / Odoo 載入會炸，這裡不重複報
    drifted = [(key, baseline.get(key), value)
               for key, value in sorted(counts.items())
               if baseline.get(key) != str(value)]
    if not drifted:
        return []
    detail = "、".join(
        "%s %s→%d" % (key, old if old is not None else "(未收錄)", new)
        for key, old, new in drifted)
    return [
        "seed 拓撲已變動（%s）。這代表 src/data/subscription_*.xml 的 seed 拓撲被改了；"
        "請逐一檢視下列持有相關計數／集合斷言的位置，確認都已同步，再跑 "
        "scripts/audit/repo-lint.py --update-baseline 收錄新拓撲（見 %s）：\n"
        % (detail, SEED_TOPOLOGY_DOC)
        + "\n".join("      - " + dep for dep in SEED_TOPOLOGY_DEPENDENTS)
    ]


def _strip_js_noise(src):
    """把 JS 的註解與字串字面值換成等長空白（行號／offset 全部保留）。

    形態 regex 只該看真正的程式碼：ServiceDetailPage 的 getter docstring 本身就在
    講「原本這裡是硬編碼的三筆 slug 白名單」，不剝除就會自我誤報。
    regex literal（``/["']/g``）有機會被誤當字串起點而多剝一段——那只會少報不會
    誤報，對齊本工具 fail-open 的哲學。
    """
    out = list(src)
    i, n, state = 0, len(src), None
    while i < n:
        ch = src[i]
        if state is None:
            if ch == "/" and i + 1 < n and src[i + 1] in "/*":
                state = "line" if src[i + 1] == "/" else "block"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if ch in "'\"`":
                state, out[i] = ch, " "
                i += 1
                continue
            i += 1
            continue
        if state == "line":
            if ch == "\n":
                state = None
            else:
                out[i] = " "
            i += 1
            continue
        if state == "block":
            if ch == "*" and i + 1 < n and src[i + 1] == "/":
                out[i] = out[i + 1] = " "
                state = None
                i += 2
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue
        # 字串字面值內
        if ch == "\\":
            out[i] = " "
            if i + 1 < n and src[i + 1] != "\n":
                out[i + 1] = " "
            i += 2
            continue
        if ch == state:
            state, out[i] = None, " "
        elif ch != "\n":
            out[i] = " "
        i += 1
    return "".join(out)


def _strip_xml_comments(src):
    """XML 註解換等長空白（同理：註解裡引述壞形態不該被當違規）。"""
    return re.sub(r"<!--.*?-->",
                  lambda m: re.sub(r"[^\n]", " ", m.group(0)), src, flags=re.DOTALL)


def _balanced_close(text, open_idx):
    """回 text[open_idx] 的括號（``(`` 或 ``{``）對應的閉合 index；找不到回 None。"""
    open_ch = text[open_idx]
    close_ch = {"(": ")", "{": "}"}[open_ch]
    depth = 0
    for j in range(open_idx, len(text)):
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
            if depth == 0:
                return j
    return None


_MEMBERSHIP_CALL_RE = re.compile(r"\.\s*(?:includes|indexOf)\s*\(")
_SLUG_WORD_RE = re.compile(r"\bslug\b")
_SLUG_RECEIVER_RE = re.compile(
    r"\b[\w$]*slugs?\s*\.\s*(?:includes|indexOf)\s*\(", re.IGNORECASE)


def _slug_membership_hits(text):
    """回 [(行號, 片段)]：以 slug 做「集合成員判斷」的兩種形態。

    1. **判準是 slug**：``X.includes(t.slug)`` / ``X.indexOf(slug) !== -1``
       ——不管集合是陣列 literal 還是抽出去的常數，都抓得到。
    2. **集合叫 slug**：``MCP_SLUGS.includes(x)`` / ``slug.includes('mcp-')``
       ——擋「先把 slug 存成區域變數再比」與 prefix 猜能力這兩種繞法。

    刻意**不**抓 ``filter`` / ``find`` / ``map``：那是列表操作（marketplace 篩選、
    排序）不是布林判準，抓了會誤殺合法用途。單值 identity 比較
    （``template.slug === 'odoo'``，如 isOdooService）同樣不在形態內。
    """
    hits = {}
    for m in _MEMBERSHIP_CALL_RE.finditer(text):
        open_idx = m.end() - 1
        close_idx = _balanced_close(text, open_idx)
        arg = text[open_idx + 1:close_idx] if close_idx else text[open_idx + 1:open_idx + 120]
        if _SLUG_WORD_RE.search(arg):
            hits[m.start()] = text[m.start():(close_idx + 1) if close_idx else m.end()]
    for m in _SLUG_RECEIVER_RE.finditer(text):
        hits.setdefault(m.start(), m.group(0))
    result = []
    for pos, snippet in sorted(hits.items()):
        flat = " ".join(snippet.split())
        result.append((text[:pos].count("\n") + 1,
                       flat[:70] + "…" if len(flat) > 70 else flat))
    return result


def check_slug_capability_gate(root, only_file=None):
    """前端（src/static/src/**/*.{js,xml}）不得用 slug 集合成員判斷決定能力。

    #460：Reset 密碼按鈕的顯示判準是硬編碼三筆 slug 白名單，後端授權判準卻是
    ``template.has_admin_gui``（#357 C01）——兩端必然漂移：8 個 has_admin_gui=True
    的模板有 5 個後端放行、前端按不到按鈕，**修復對租戶不可達等於沒修**。能力一律
    由後端欄位表達（payload 鏡射 model 欄位），前端不維護自己的名單。
    依據 docs/decisions/patterns/capability-gate-single-source-field.md
    """
    violations = []
    if only_file:
        files = [only_file]
    else:
        files = sorted(
            glob.glob(os.path.join(root, "src/static/src/**/*.js"), recursive=True)
            + glob.glob(os.path.join(root, "src/static/src/**/*.xml"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        text = _strip_xml_comments(src) if fp.endswith(".xml") else _strip_js_noise(src)
        for line_no, snippet in _slug_membership_hits(text):
            violations.append(
                "%s:%d：用 slug 做集合成員判斷（%s）。前端能力 gate 不得自建 slug 名單"
                "——能力由後端欄位表達（service payload 的 template.%s，#460 S1）。slug "
                "名單是第二判準，與後端授權判準（#357 C01）必然漂移：8 個 %s=True 的模板"
                "曾有 5 個後端放行、前端按不到，修復對租戶不可達。缺欄位就補後端 payload，"
                "不要在前端補名單。（單值 identity 比較如 template.slug === 'odoo' 不在本"
                "規則內；若這裡確實不是能力判準，請在 PR 說明並調整本規則 scope，見 %s）"
                % (rel, line_no, snippet, ADMIN_GUI_FIELD, ADMIN_GUI_FIELD,
                   CAPABILITY_GATE_DOC))
    return violations


def _js_member_bodies(stripped, name):
    """回 class member ``get name() {...}`` / ``name() {...}`` 的 body 列表。"""
    bodies = []
    for m in re.finditer(r"(?:get\s+)?\b%s\s*\(\s*\)\s*\{" % re.escape(name), stripped):
        open_idx = m.end() - 1
        close_idx = _balanced_close(stripped, open_idx)
        bodies.append(stripped[open_idx:close_idx + 1] if close_idx else stripped[open_idx:])
    return bodies


def _gate_js_sources(xml_path):
    """XML 對應的 component JS（同名優先，否則同目錄全部）——已剝註解／字串。"""
    same = os.path.splitext(xml_path)[0] + ".js"
    paths = [same] if os.path.exists(same) else sorted(
        glob.glob(os.path.join(os.path.dirname(xml_path), "*.js")))
    sources = []
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as f:
            sources.append(_strip_js_noise(f.read()))
    return sources


def _gate_reaches_field(exprs, js_sources, field):
    """t-if 表達式（含祖先鏈）是否直接或經 getter 追到 ``field``。"""
    for expr in exprs:
        if field in expr:
            return True
        for name in set(re.findall(r"[A-Za-z_$][\w$]*", expr)) - OWL_EXPR_NON_MEMBERS:
            for src in js_sources:
                if any(field in body for body in _js_member_bodies(src, name)):
                    return True
    return False


def _gate_exprs_with_ancestors(elem, parents):
    """elem 自身 → 祖先鏈上的所有 t-if / t-elif 表達式（gate 可能掛在外層 wrapper）。"""
    exprs, node = [], elem
    while node is not None:
        exprs += [node.get(attr) for attr in ("t-if", "t-elif") if node.get(attr)]
        node = parents.get(node)
    return exprs


def _first_line_matching(path, pattern):
    with open(path, encoding="utf-8", errors="replace") as f:
        return next((i for i, line in enumerate(f, 1) if pattern.search(line)), 1)


def check_admin_gui_gate_anchor(root):
    """Reset 密碼按鈕的顯示 gate 必須（直接或經 getter）追得到 has_admin_gui。

    check_slug_capability_gate 擋的是「已知的壞形態」；這條反過來鎖「唯一的對形態」
    ——不論 getter 叫什麼名字、判準寫成哪種樣子，只要按鈕的 t-if 鏈追不到
    has_admin_gui 就 fail。第三個判準（category / mcp_enabled / 任何新名單）因此在
    review 之前就被機器攔下（#460 S2）。

    Fail-open：找不到按鈕（功能下架 / handler 更名）不擋——形態守線由上一條負責。
    """
    violations = []
    handler_re = re.compile(r"\b%s\b" % re.escape(ADMIN_GUI_BUTTON_HANDLER))
    for fp in sorted(glob.glob(os.path.join(root, "src/static/src/**/*.xml"), recursive=True)):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        try:
            tree = ET.parse(fp)
        except ET.ParseError:
            continue  # XML 壞掉由 check_owl_template 報，這裡不重複
        buttons = [elem for elem in tree.iter()
                   if any(handler_re.search(v or "") for v in elem.attrib.values())]
        if not buttons:
            continue
        line_no = _first_line_matching(fp, handler_re)
        parents = {child: parent for parent in tree.iter() for child in parent}
        js_sources = _gate_js_sources(fp)
        for elem in buttons:
            exprs = _gate_exprs_with_ancestors(elem, parents)
            if _gate_reaches_field(exprs, js_sources, ADMIN_GUI_FIELD):
                continue
            violations.append(
                "%s:%d：Reset 密碼按鈕（%s）的顯示 gate 追不到 %s（t-if 鏈：%s）。按鈕的 "
                "t-if（含祖先）必須直接引用該欄位，或引用一個 body 讀該欄位的 getter"
                "——那是與後端 reset 授權判準（controllers/paas.py 的 "
                "service.template_id.%s，#357 C01）唯一收斂的判準。前端一旦長出第二判準"
                "（slug 名單、category、mcp_enabled…），就會回到 #460：後端放行、租戶"
                "按不到，修復不可達（見 %s）。"
                % (rel, line_no, ADMIN_GUI_BUTTON_HANDLER, ADMIN_GUI_FIELD,
                   "／".join(exprs) or "(無 t-if)", ADMIN_GUI_FIELD, CAPABILITY_GATE_DOC))
    return violations


def check_access_tab_gate_anchor(root):
    """Access 頁籤的顯示 gate 必須（直接或經 getter）追得到 ac_max_access_seats。

    與 check_admin_gui_gate_anchor 同型（鎖「唯一的對形態」），錨點換成頁籤的
    `setTab('access')`。#746 CS Access Control 拍板 6：**只有 `ac_max_access_seats
    > 0` 的服務看得到 Access 頁籤**，無席位不顯示、不做 upsell。

    席位是**方案**屬性（`ac_` 家族＝capability-orthogonal 的上限值，OQ-C 拍板），
    不是模板屬性——所以第二判準的形狀在這裡特別誘人也特別錯：`template.slug` 或
    `category` 今天剛好對，明天多一個 AC 席位方案就漏，而漏的方向一如 #460 是
    「後端有設定、租戶看不到頁籤」＝修復不可達。

    Fail-open：找不到頁籤（功能下架 / handler 更名）不擋——形態守線由
    check_slug_capability_gate 負責。

    依據 docs/decisions/patterns/capability-gate-single-source-field.md
    """
    violations = []
    for fp in sorted(glob.glob(os.path.join(root, "src/static/src/**/*.xml"),
                               recursive=True)):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        try:
            tree = ET.parse(fp)
        except ET.ParseError:
            continue  # XML 壞掉由 check_owl_template 報，這裡不重複
        tabs = [elem for elem in tree.iter()
                if any(ACCESS_TAB_HANDLER_RE.search(v or "")
                       for v in elem.attrib.values())]
        if not tabs:
            continue
        line_no = _first_line_matching(fp, ACCESS_TAB_HANDLER_RE)
        parents = {child: parent for parent in tree.iter() for child in parent}
        js_sources = _gate_js_sources(fp)
        for elem in tabs:
            exprs = _gate_exprs_with_ancestors(elem, parents)
            if _gate_reaches_field(exprs, js_sources, ACCESS_SEATS_FIELD):
                continue
            violations.append(
                "%s:%d：Access 頁籤（setTab('access')）的顯示 gate 追不到 %s"
                "（t-if 鏈：%s）。頁籤的 t-if（含祖先）必須直接引用該欄位，或引用一個 "
                "body 讀該欄位的 getter——那是設計拍板 6 的判準本體（方案的 AC 席位"
                "上限，由 _get_subscription_info() 直接鏡射）。改判 template slug／"
                "category 會回到 #460：席位是方案屬性不是模板屬性，下一個 AC 方案就"
                "漏，而且漏的方向是租戶看不到頁籤＝修復不可達（見 %s）。"
                % (rel, line_no, ACCESS_SEATS_FIELD,
                   "／".join(exprs) or "(無 t-if)", CAPABILITY_GATE_DOC))
    return violations


def check_gateway_copy_gate_anchor(root):
    """宣告 gateway 憑證的文案，其顯示 gate 必須（直接或經 getter）追得到 has_gateway。

    與 check_admin_gui_gate_anchor 同型（鎖「唯一的對形態」），錨點換成**文案本身**
    ——這條守的不是某顆按鈕在不在，而是「這句話會不會對不該看到的人出現」。

    #530 review finding：Reset 提示語原本判 ``hasAdminGui``，但實際交付判準是
    ``service._gateway_public_url()`` 非空。seed 裡 has_admin_gui=True 的模板有 8 個
    （mcp-odoo／mcp-odoo-manage／mcp-n8n／mcp-k8s／mcp-vk／mcp-opendesign／
    open-design／hermes-agent），其中只有 hermes-agent 有 gateway ⇒ **7 個模板的租戶
    被告知一件永遠不會發生的事**。這類錯誤不會有任何測試變紅：後端測後端判準、前端
    沒有能力測試，兩邊各自都「看起來對」（#460 的同一個家族，只是這次漂移在文案）。

    ``hasAdminGui and hasGateway`` 這種複合 gate 通過：兩個都是後端鏡射欄位，本規則
    只要求鏈上追得到 has_gateway。

    Fail-open：沒有這類文案（拿掉／改寫成不提 gateway 憑證）就不擋——與按鈕那條同
    紀律，形態守線由 check_slug_capability_gate 負責。

    依據 docs/decisions/patterns/capability-gate-single-source-field.md
    """
    violations = []
    for fp in sorted(glob.glob(os.path.join(root, "src/static/src/**/*.xml"),
                               recursive=True)):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        try:
            tree = ET.parse(fp)   # ET 丟掉 XML 註解 → 註解裡引述本文案不會被誤報
        except ET.ParseError:
            continue  # XML 壞掉由 check_owl_template 報，這裡不重複
        # 文字節點：元素自身的 text ＋ 直屬子元素的 tail（都屬於這個元素的內容）
        copy_elems = [
            elem for elem in tree.iter()
            if any(GATEWAY_COPY_RE.search(t or "")
                   for t in [elem.text] + [child.tail for child in elem])
        ]
        if not copy_elems:
            continue
        line_no = _first_line_matching(fp, GATEWAY_COPY_RE)
        parents = {child: parent for parent in tree.iter() for child in parent}
        js_sources = _gate_js_sources(fp)
        for elem in copy_elems:
            exprs = _gate_exprs_with_ancestors(elem, parents)
            if _gate_reaches_field(exprs, js_sources, GATEWAY_FIELD):
                continue
            violations.append(
                "%s:%d：宣告 gateway 憑證的文案，其顯示 gate 追不到 %s（t-if 鏈：%s）。"
                "該文案的 t-if（含祖先）必須直接引用該欄位，或引用一個 body 讀該欄位的 "
                "getter——那是與後端交付判準（service._gateway_public_url() 非空，"
                "controllers/paas.py 的 _gateway_credentials 用同一個）唯一收斂的判準。"
                "改判 %s 會讓 8 個 has_admin_gui 模板裡沒有 gateway 的那 7 個對租戶宣告"
                "一件永遠不會發生的事，而兩邊各自都不會報錯（#530／#460 同家族，"
                "見 %s）。"
                % (rel, line_no, GATEWAY_FIELD,
                   "／".join(exprs) or "(無 t-if)", ADMIN_GUI_FIELD, CAPABILITY_GATE_DOC))
    return violations


_HERMES_AGENT_CHART_FLOOR = (0, 1, 3)  # chart 0.1.3 起 webui 移除、dashboard 走 root


def check_hermes_agent_013_seed(root):
    """hermes-agent record 必須對齊 chart 0.1.3 seed 形態（防 regression）。

    0.1.3 移除 webui container、dashboard 改走 root URL（nginx `/` 直連），
    對應的平台 seed 形態：

    - ``helm_chart_version`` 不得低於 ``0.1.3``（**floor，不是 pin**：升版是
      常態要放行，降版回到 webui 時代才擋。比較用 int tuple，不可用字串——
      ``'0.1.10' < '0.1.3'`` 字串比較是 True，但語意上 0.1.10 > 0.1.3）
    - ``helm_default_values`` 不得含 ``webui`` block（chart 不再 render 那顆
      container）
    - ``helm_default_values.dashboard`` 不得含 ``externalPath``（dashboard 不再
      走 subpath）

    任一條件失效 ⇒ OAuth ``redirect_uri`` 會打到 404 subpath（chart 用舊 subpath
    轉發，平台仍組 root URL），租戶拿到一條看起來正常的死鏈。``#424①`` 的合約
    變更（`HERMES_DASHBOARD_PUBLIC_URL` 由 ``/dashboard`` 變 root URL）正是
    為了把這條路徑鏈完全打通。

    反例：有人升 chart 但沒同步 seed、或從舊 chart 回退卻留著舊 block。守線
    本身不驗 image pin / dashboard 內容細節（那些由 test_hermes_agent_seed_424
    機械斷言）；本條只守「block 不在」與「版號不得倒退」這兩個**最小必要條件**
    ——升版時最容易漏改、漏改後整鏈不動。
    """
    violations = []
    target_id = 'cloud_app_hermes_agent'
    # 同一個 id 的 record 會跨多個 data 檔分次宣告欄位（Odoo 慣例：
    # subscription_template_links.xml 只補 subscription_plan_ids，不重複整個
    # template）。逐檔獨立驗證會把「這個檔沒宣告 helm_chart_version」誤判成
    # 「缺欄位」——必須先把同一個 id 的所有 <record> 跨檔彙整成一顆有效
    # record，語意才對得上 Odoo 實際的欄位合併行為。
    records = []  # [(rel, <Element record>), ...]
    for fp in sorted(glob.glob(os.path.join(root, "src/data/*.xml"))):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        try:
            tree = ET.parse(fp)
        except ET.ParseError:
            continue
        for rec in tree.iter('record'):
            if rec.attrib.get('id') == target_id:
                records.append((rel, rec))
    if not records:
        return violations
    files = sorted({rel for rel, _ in records})

    # 1) chart version 不得低於 floor（0.1.3）；floor 不是 pin，升版必須放行
    version_hit = None
    for rel, record in records:
        for fld in record.iter('field'):
            if fld.attrib.get('name') == 'helm_chart_version':
                version_hit = (rel, (fld.text or '').strip())
                break
        if version_hit:
            break
    if version_hit is None:
        violations.append(
            "%s：cloud_app_hermes_agent 缺 helm_chart_version 欄位（已跨全部"
            "宣告此 id 的檔案彙整，皆未宣告）" % "／".join(files))
    else:
        rel, raw_version = version_hit
        parts = raw_version.split('.')
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            violations.append(
                "%s：cloud_app_hermes_agent.helm_chart_version = %r "
                "版號形態無法解析（必須是 N.N.N 形式的整數版號），不予放行"
                % (rel, raw_version))
        else:
            version_tuple = tuple(int(p) for p in parts)
            if version_tuple < _HERMES_AGENT_CHART_FLOOR:
                violations.append(
                    "%s：cloud_app_hermes_agent.helm_chart_version = %r "
                    "低於 floor %s——這是 floor 不是 pin：升版自由，降版才擋。"
                    "chart 0.1.3 移除 webui container、dashboard 改走 root "
                    "URL；平台端 `_inject_dashboard_public_url` 與 "
                    "`HERMES_DASHBOARD_PUBLIC_URL` 合約已對齊 0.1.3，version "
                    "不得回退到 webui 時代（見 PR feat/hermes-agent-013-cleanup）"
                    % (rel, raw_version,
                       ".".join(str(n) for n in _HERMES_AGENT_CHART_FLOOR)))

    # 2) helm_default_values 不得含 webui / dashboard.externalPath（逐一檢查每個
    #    有宣告這個欄位的 record 區塊，不只挑第一顆——理論上任何一份檔案重新
    #    宣告了帶 webui 的 helm_default_values 都必須被抓到）。
    for rel, record in records:
        for fld in record.iter('field'):
            if fld.attrib.get('name') != 'helm_default_values':
                continue
            raw = (fld.text or '').strip()
            if not raw:
                continue
            try:
                values = json.loads(raw)
            except (ValueError, TypeError):
                continue  # JSON 壞掉由其他 check 報
            # webui block（key 開頭的 `"webui":`）
            if 'webui' in values:
                violations.append(
                    "%s：cloud_app_hermes_agent.helm_default_values 仍含 `webui`"
                    " block。chart 0.1.3 起 webui container 已移除（nginx `/` 直連"
                    " dashboard）；seed 留著會被 chart 當成 subpath 轉發，OAuth "
                    "redirect_uri 與平台 `_inject_dashboard_public_url` 對不起"
                    "來" % rel)
            # dashboard.externalPath
            dash = values.get('dashboard')
            if isinstance(dash, dict) and 'externalPath' in dash:
                violations.append(
                    "%s：cloud_app_hermes_agent.helm_default_values.dashboard"
                    ".externalPath 仍存在。chart 0.1.3 起 dashboard 走 root URL，"
                    "留著 externalPath 會被 chart 當成 subpath 轉發，OAuth "
                    "redirect_uri 打到 404 subpath" % rel)
            break
    return violations


# ------------------------------------------------------------ #659 ir.rule 四條守線
# 四條守線共用同一份「module 全部 <record> 宣告」的靜態視圖（`module_record_declarations`）：
#   (1) check_security_rule_noupdate        ir.rule 不得**出生**在 noupdate="1" 區塊
#   (2) check_retired_rule_xmlid            退役的 xmlid 不得**復活**
#   (3) check_workspace_access_rule_directions  workspace_access 的寫入方向不得**留空**
#   (4) check_workspace_rule_directions     workspace 本體的寫入方向不得**留空**（#658）
# 前兩條是同一個機制的兩端（`ir_model_data.noupdate` 的 sticky 語意），後兩條守的是
# 活的授權面 ⇒ 前兩條合成一條規則、後兩條各自成一條（見檔頭 22／23／**25**——
# 第 24 條是 one-time-credential-render，與本組無關；原本這裡寫「24」，是 #658
# 附加第四條守線時沒跟上檔頭實際落點的過期指標，#675 覆核 F1 順手查證修正）。
IR_RULE_MODEL = "ir.rule"
SECURITY_RULES_REL = "src/security/ir_rules.xml"
# 只給違規訊息用（印 ACL 實值），**不參與任何 pass/fail 判準**——見
# `_model_acl_note` 與 `_rule_direction_violations` 的 docstring。
ACL_CSV_REL = "src/security/ir.model.access.csv"
SECURITY_RULE_DOC = "docs/tracking/landings/556-workspace-access-authz-landing.md"
# 自家 module 的 xmlid 前綴：`<record id="woow_paas_platform.x">` 與 `<record id="x">`
# 在 Odoo 眼裡是同一個 record，判準必須看得出來。只剝自家前綴——`base.group_user`
# 之類的外模組 xmlid 原樣保留（剝掉會讓 group 判定錯位）。
MODULE_XMLID_PREFIX = "woow_paas_platform."

# (1) 已經住在 noupdate="1" 區塊的既有 ir.rule：**只准縮不准長**的 grandfather 清單。
# 🔴 #658 把原本列在這裡的 workspace_rule_member_access / workspace_rule_create 退役掉
# 之後，這份清單已經空了——**空是正確的終態，不要為了「留個範例」塞東西回來**。
# 🔴 刻意寫成 in-code 常數而不是 `baselines/*.txt`：`--update-baseline` 會一次重產**全部**
# baseline，任何人為了別的理由（改 seed 拓撲、新增一條 check）跑它，就會把新冒出來的
# 違規一起靜默吸收——那是 baseline 機制的已知代價，而這份清單守的是安全面。寫在 code
# 裡時「放行一條新的 rule」必然出現在同一處 diff，覆核看得到。
SECURITY_RULE_NOUPDATE_ALLOWED = {}

# (2) 已用 migration DELETE 退役的 xmlid：不得再有 <record> 宣告。
# 值＝「退役它的 migration 版號（出處 issue）」。issue 編號寫在**值**裡而不是違規訊息
# 的字面裡：訊息原本寫死「是 #556 已用 migration … 退役的」，#658 加進兩個條目之後那句話
# 對它們就是假的（同一個坑 #659 驗收 F3 記過一次——訊息不得寫死當下才成立的事實）。
RETIRED_RULE_XMLIDS = {
    "workspace_access_rule_member_access": "18.0.1.0.122（#556）",
    "workspace_access_rule_create": "18.0.1.0.122（#556）",
    "workspace_rule_member_access": "18.0.1.0.131（#658）",
    "workspace_rule_create": "18.0.1.0.131（#658）",
}

# (3) 守哪個 model 的哪幾個方向。model 端錨在 `<field name="model_id" ref="…"/>` 的
# xmlid（那是 XML 端**唯一**的 model 身分），方向只列寫入三向——`perm_read` 今天有
# `workspace_access_rule_member_read` 守著，而 #659 指定的是寫入面；不納入是**刻意的
# 決定**而不是漏掉，寫在這裡免得下一個人以為讀方向也被守著。
WORKSPACE_ACCESS_MODEL_REF = "model_woow_paas_platform_workspace_access"
# workspace **本體**（#658）。與上面那個是不同的 model：一個是成員關係列、一個是
# 被保護的資源本身，兩者的 rule 集合互相獨立，缺哪一邊都不會讓另一邊變紅。
# 🔴 這兩行**相鄰宣告**、而兩條 check 共用 `_rule_direction_violations` ⇒ 把下面這個
# 值複製貼上成上面那個是有訊號地雷的：#675 覆核實測，這麼一改之後 workspace 本體四條
# rule 全刪掉 gate 仍然 EXIT=0（打錯字會紅、複製貼上不會）。所以「兩個錨點必須互異、
# 且各自真的有 ir.rule」現在是被 gate 檢查的前提，見 `_rule_direction_anchor_violations`
# ——不要把那道前提當成多餘的自我檢查刪掉。
WORKSPACE_MODEL_REF = "model_woow_paas_platform_workspace"
# 租戶帳號實際持有的群組。`base.group_system` 不算——見 (3) 的 docstring。
TENANT_GROUP_XMLID = "base.group_user"
IR_RULE_WRITE_DIRECTIONS = ("perm_write", "perm_create", "perm_unlink")


def _security_rule_xml_files(root):
    """module 裡可能宣告 `<record>` 的 XML（排除 `src/static/` 的 OWL template）。

    刻意**不**綁死 `src/security/ir_rules.xml` 單一檔名：ir.rule 可以宣告在任何 data
    XML，而「有人把 record 搬到別的檔」不該讓守線靜默解除——那正是「selector 從未命中
    的 no-op gate」家族（本 repo 記錄有案：gate 內部判準逐行讀都對，但整條沒在跑，
    而它的名字照樣印在通過清單裡）。
    """
    files = []
    for fp in sorted(glob.glob(os.path.join(root, "src/**/*.xml"), recursive=True)):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if rel.startswith("src/static/"):
            continue
        files.append((rel, fp))
    return files


def _security_rule_watches(rel):
    """三條守線的 hook 分派條件：module 的非 static XML。

    與 `_security_rule_xml_files` 用同一個判準（前綴／後綴），所以「hook 接得到的檔」
    與「CI 掃描的檔」在結構上不可能分岔。寫成 `rel == SECURITY_RULES_REL` 會讓 record
    搬到別的 data 檔時，守線與 hook 一起沉默。
    """
    return (rel.startswith("src/") and rel.endswith(".xml")
            and not rel.startswith("src/static/"))


def _noupdate_attr_is_true(raw):
    """`noupdate="…"` 的值判定，**逐字對齊 `odoo/tools/convert.py` 的 `str2bool`**。

    🔴 Odoo 有**兩個**同名 `str2bool`，語意不同，而 XML 的 `noupdate` 走的是
    `convert.py` 那份：

      * `odoo/tools/convert.py`：`return value.lower() not in ('0', 'false', 'off')`
        ——**純黑名單**，只有這三個字（外加空字串走 default）是 False，其餘**全部**是
        True。`no` / `No` / `f` / `n` / `banana` 都是 True，而且**永不 raise**。
      * `odoo/tools/misc.py`：白名單／黑名單雙表，認不得會 raise。convert.py **沒有**
        import 它（該檔只從 misc import `file_open, file_path, SKIPPED_ELEMENT_TYPES`），
        所以它對 noupdate 完全不適用。

    這個區分是本檔踩過的實例：原本照 misc.py 那份寫（白名單），於是
    `<data noupdate="no">` 底下的 ir.rule 在 Odoo 是 sticky noupdate、gate 卻放行
    ⇒ 一條確定可用的靜默繞過路徑（#659 驗收 F1，端到端坐實 repo-lint EXIT=0）。
    現實分佈上 Odoo 18 核心 addons 只出現 `1`/`0`/`True`/`False`，本 repo 只有 `1`/`0`
    ——所以它不是「明天就會撞到」，但**寫自然否定的人會靜默中招，想繞過的人有確定路徑**。

    ⚠ **空字串／全空白由呼叫端負責，本函式不處理**：`nodeattr2bool` 的
    `if not node.get(attr)` ／ `if not val` 是回 `default`（＝繼承外層），而本函式對
    `""` 與 `"   "` 都回 **True**（`"" not in ('0','false','off')`）。那個守衛住在
    `_effective_noupdate` 的 `if raw is not None and raw.strip():`。本函式是具名的獨立
    helper、可能被別處重用，所以這件事必須寫在這裡——否則下一個呼叫端會以為它自己就是
    完整的 `nodeattr2bool`，而 `noupdate=""` 會被判成 sticky（誤擋方向）。

    ⚠ 本函式只服務 `noupdate` 這一條路徑。`perm_*` / `active` 走的是 `<field>` 的值
    （`eval` 交給 `safe_eval`；純 text 則命中 `convert.py:421-422` 的 str2bool），判定
    不同，由 `_field_literal_bool` 負責——兩條路徑刻意不共用一個函式，共用就會有一邊被
    另一邊的需求拖偏（F1 的成因正是共用）。

    要重新核對 Odoo 端請直接對真樹做（不要照抄本 docstring）：用 `ast` 從
    `odoo/tools/convert.py` 抽出 `str2bool` / `nodeattr2bool` 的函式原文再 exec，
    餵值比對。實測 `odoo-17.0` 與 `odoo-18.0` 兩份樹的 `convert.py::str2bool` 逐字相同。
    """
    return raw.strip().lower() not in ("0", "false", "off")


def _field_literal_bool(raw, default):
    """`<field name="perm_write" eval="True"/>` 這類**布林欄位值**的判定。

    這條路徑**不經** `nodeattr2bool`（那是 XML 屬性用的，見 `_noupdate_attr_is_true`）。
    `<field>` 的布林值在 Odoo 有兩條不同的路：

      1. **`eval="…"`（常態）**：`_eval_xml` 交給 `safe_eval` ⇒ 拿到的是 Python 物件
         （`True` / `False` / `1` / `0`），`_tag_record` 的 `isinstance(f_val, str)` 為
         False，**不會**再過任何 str2bool。
      2. **純 text `<field name="perm_write">0</field>`**：`_eval_xml` 回字串 ⇒ 命中
         `convert.py` 的 `elif field_type == 'boolean' and isinstance(f_val, str):
         f_val = str2bool(f_val)`（`odoo/tools/convert.py:421-422`）——**還是那份純
         黑名單 str2bool**，不是 Python 的 `bool()`。

    本函式只認字面，認不得回 `default`（呼叫端一律傳保守值，見 `_rule_flag`）。

    ⚠ 本 docstring 原本寫「純 text 形態在 Odoo 走 ORM 的 Boolean 轉換 ⇒ `bool('no')`
    ＝ True」——**機制引用錯了**，而且錯得有後果：照 `bool()` 推論會得出
    `<field name="perm_write">0</field>` 在 Odoo 是 True（`bool('0')` ＝ True），
    實際上 `str2bool('0')` ＝ False，**答案相反**。本函式對 `0` 判 False（＝與 Odoo
    一致），所以**程式碼一直是對的，只有解釋會把下一個人導向錯結論**。這正是 F1 的
    成因形態（引用了同名但不同語意的函式），剛被咬過所以連「原本錯在哪」一起留著。

    ⚠ 已知偏差（全部落在**過嚴誤報**方向，刻意不修）：純 text 的 `no` / `n` / `f` /
    任何認不得的字，Odoo 的黑名單判 True、本函式判 False。逐格實測（Odoo 端用 ast 從
    真 convert.py 抽出 str2bool 再 exec）：`1/True/true/on/yes/t/y` 與
    `0/false/False/off` 共 11 格一致，`no/n/f/banana` 4 格過嚴。方向是「gate 說沒涵蓋、
    實際涵蓋了」＝紅不是綠，不會製造假綠；而現實中沒人用 text 形態寫布林欄位。
    寫下來是為了不讓下一個人以為這格已經對齊（#659 驗收 F2 同型：偏差要寫成已知，
    不要留一個看起來完備的判準）。
    """
    val = (raw or "").strip().lower()
    if val in ("y", "yes", "1", "true", "t", "on"):
        return True
    if val in ("n", "no", "0", "false", "f", "off"):
        return False
    return default


def _strip_module_prefix(xmlid):
    """`woow_paas_platform.x` → `x`；其餘（含 `base.` 開頭）原樣保留。"""
    if xmlid.startswith(MODULE_XMLID_PREFIX):
        return xmlid[len(MODULE_XMLID_PREFIX):]
    return xmlid


def _effective_noupdate(elem, parents):
    """這個 record 的**生效** noupdate，回 `(bool, 決定它的那個屬性原文或 None)`。

    回原文而不只是 bool，是為了讓違規訊息印出**實際**寫在檔案裡的值。原本訊息逐字
    寫「落在 `noupdate="1"` 區塊」，但 sticky 的值不只 `1`（見下）——用 `noupdate="no"`
    繞過時訊息會叫讀者去找一個檔案裡不存在的字串（同 F3 那族：訊息斷言了它沒驗過的
    東西）。

    對齊 Odoo 18 `xml_import._tag_root`（`odoo/tools/convert.py`）——它對每一層
    `<odoo>`／`<data>`／`<openerp>` 做 `nodeattr2bool(el, 'noupdate', self.noupdate)`
    推進一個 stack，所以**最近的顯式宣告勝出、其餘繼承外層**。因此：

      * `<odoo noupdate="1">` 底下的裸 record 也算 noupdate（不是只有 `<data noupdate="1">`
        才算）——只認 `<data>` 的判準會被這個合法佈局整條繞過；
      * `<odoo noupdate="1"><data noupdate="0">` 底下的算 noupdate=0（內層覆寫外層）。

    「顯式宣告」的判準也逐字對齊 `nodeattr2bool`：屬性不存在、空字串、或只有空白
    （`if not node.get(attr)` ／ `if not val`）都回 default ＝**繼承外層**，所以這裡
    是 `continue` 往上找而不是當成 False。

    值本身的判定見 `_noupdate_attr_is_true`（convert.py 的**純黑名單** str2bool）：
    只有 `0` / `false` / `off` 是 False，`no` / `f` / `n` / 任何認不得的字都是 True，
    而且 Odoo 端**永不 raise**（`noupdate="banana"` 模組照樣載得起來、且是 sticky）。
    本 docstring 原本寫「Odoo 那邊會 raise」——那是引用了 `misc.py` 那份不相干的
    str2bool，結果碰巧對而理由是錯的（#659 驗收 F1）。
    """
    cur = parents.get(elem)
    while cur is not None:
        raw = cur.get("noupdate")
        if raw is not None and raw.strip():
            return _noupdate_attr_is_true(raw), raw.strip()
        cur = parents.get(cur)
    return False, None


def module_record_declarations(root):
    """回 `(records, errors)`：module 全部 `<record>` 宣告的靜態視圖。

    `records` 每項是 `{"rel", "xmlid", "model", "noupdate", "fields"}`；`fields` 是
    `{欄位名: <field> Element}`——保留 Element 才讀得到 `ref=` 屬性（既有的
    `_record_fields` 只讀 eval／text，對 `<field name="model_id" ref="…"/>` 會拿到
    空字串，用它做 model 判定就是一條永不命中的 selector）。

    用 XML parser 而不是 regex 是刻意的：屬性順序（`<record id model>` vs
    `<record model id>`）、單／雙引號、多個或巢狀的 `<data>` 區塊、註解裡的假 record
    ——四種形態 regex 都會做錯，而做錯的方向是**靜默零命中**。`ET.parse` 預設就丟掉
    註解，所以「註解裡提到退役的 xmlid」天然不會誤報（那兩個名字今天仍合法出現在
    `ir_rules.xml` 的註解、migration 與 docstring 裡）。

    `errors` 是 parse 不了的檔，呼叫端一律當違規（fail-loud）。
    """
    records, errors = [], []
    for rel, fp in _security_rule_xml_files(root):
        try:
            tree = ET.parse(fp)
        except (ET.ParseError, OSError, ValueError) as exc:
            errors.append((rel, exc))
            continue
        parents = {child: parent for parent in tree.getroot().iter()
                   for child in parent}
        for rec in tree.getroot().iter("record"):
            fields = {}
            for fld in rec.findall("field"):
                name = fld.get("name")
                if name and name not in fields:
                    fields[name] = fld
            sticky, raw = _effective_noupdate(rec, parents)
            records.append({
                "rel": rel,
                "xmlid": _strip_module_prefix(rec.get("id") or ""),
                "model": rec.get("model") or "",
                "noupdate": sticky,
                # 決定 noupdate 的那個屬性原文（訊息用；沒有任何祖先宣告時是 None）
                "noupdate_raw": raw,
                "fields": fields,
            })
    return records, errors


def _ir_rule_premise(records, errors, what):
    """三條守線共用的 fail-loud 前提：parse 失敗、或一條 ir.rule 都抽不到。

    `src/security/ir_rules.xml` 今天有數十條 ir.rule；抽到 0 條**只可能**是 selector
    壞了。而這三條守線裡有兩條是負向判準（「不得出現 X」「不得住在 Y」），負向判準
    在空集合上恆真 ⇒「探針從未命中」與「乾淨」的輸出完全相同。所以回違規、不回 []。
    """
    violations = [
        "%s：XML parse 失敗（%s）——「%s」的輸入讀不到。解析失敗當違規而不是無事，"
        "否則這條守線會在檔案格式一改的當下永久沉默且看起來永遠是綠的"
        % (rel, exc, what) for rel, exc in errors]
    if not any(rec["model"] == IR_RULE_MODEL for rec in records):
        violations.append(
            "%s：整個 module 掃不到任何 `<record model=\"%s\">`——「%s」的 selector 壞了"
            "（該檔今天有數十條）。負向判準在空集合上恆真，這時的「沒有違規」與「探針"
            "從未命中」輸出一模一樣（見 %s）"
            % (SECURITY_RULES_REL, IR_RULE_MODEL, what, SECURITY_RULE_DOC))
    return violations


def check_security_rule_noupdate(root):
    """`<record model="ir.rule">` 不得住在 `noupdate="1"` 區塊（#659 ①）。

    #556 的**可修復性**根因就是這個：rule 出生在 `noupdate="1"` 區塊，而
    `ir_model_data.noupdate` 是 sticky 的 ⇒ (a) `-u` 不會重寫欄位、(b) 把 `<record>`
    搬到 `noupdate="0"` 區塊也翻不了旗標、(c) 從 XML 刪掉同樣不會清除它 ⇒ 唯一的修法
    是下 migration DELETE。一條寫錯的授權規則因此**不能用改 XML 修好**，而寫的當下
    完全沒有訊號。

    既有第 3 條 `data-noupdate` 擋不到這個形態：它只掃 `src/data/*.xml` 的**檔案層**
    （「這個檔有沒有宣告 noupdate="1"」），`src/security/ir_rules.xml` 從來不在射程內，
    而授權規則正好全住在那裡。

    `SECURITY_RULE_NOUPDATE_ALLOWED` 是**只准縮不准長**的 grandfather 清單，而且**雙向**
    對帳：清單裡的 xmlid 若已經不在 noupdate 區塊（#658 修好了），這裡會要求把那一行
    刪掉。理由不是整潔——留著的話，日後有人以同一個 xmlid 新建一條 rule 進 noupdate
    區塊會被**靜默放行**（清單比它豁免的東西活得久，下一個同名 record 無聲繼承它）。
    """
    what = "ir.rule 不得出生在 noupdate=1 區塊"
    records, errors = module_record_declarations(root)
    violations = _ir_rule_premise(records, errors, what)
    seen_allowed = set()
    for rec in records:
        if rec["model"] != IR_RULE_MODEL or not rec["noupdate"]:
            continue
        xmlid = rec["xmlid"]
        if xmlid in SECURITY_RULE_NOUPDATE_ALLOWED:
            seen_allowed.add(xmlid)
            continue
        raw = rec.get("noupdate_raw")
        hint = ""
        if raw is not None and raw.lower() not in ("1", "true"):
            # 印實值，並說明為什麼這個字也是 sticky——否則讀者會以為只有 "1" 算。
            hint = ("（Odoo `convert.py::str2bool` 是純黑名單：只有 `0`／`false`／`off` "
                    "是 False，`%s` 一樣讓整個區塊 sticky）" % raw)
        violations.append(
            "%s：`<record id=\"%s\" model=\"ir.rule\">` 落在 noupdate=\"%s\" 區塊%s。"
            "`ir_model_data.noupdate` 是 sticky 的：`-u` 不重寫欄位、把 record 搬到 "
            "noupdate=\"0\" 區塊翻不了旗標、從 XML 刪掉也不會清除 ⇒ 這條規則寫錯之後"
            "**只能下 migration DELETE 修**（#556 的可修復性根因）。把它宣告在 "
            "`<data noupdate=\"0\">` 區塊；真的必須 noupdate 才加進 "
            "`SECURITY_RULE_NOUPDATE_ALLOWED` 並寫下理由（見 %s）"
            % (rec["rel"], xmlid or "(無 id)", raw if raw is not None else "1", hint,
               SECURITY_RULE_DOC))
    for xmlid in sorted(set(SECURITY_RULE_NOUPDATE_ALLOWED) - seen_allowed):
        violations.append(
            "%s：`SECURITY_RULE_NOUPDATE_ALLOWED` 還列著 `%s`（%s），但現在已經沒有"
            "任何 noupdate=\"1\" 區塊裡的 ir.rule 叫這個名字——已經修好了就把那一行"
            "刪掉。留著的話，日後有人用同一個 xmlid 新建一條 rule 進 noupdate 區塊會"
            "被**靜默放行**（豁免比它豁免的東西活得久）"
            % (REPO_LINT_SELF_REL, xmlid,
               SECURITY_RULE_NOUPDATE_ALLOWED[xmlid]))
    return violations


def check_retired_rule_xmlid(root):
    """已退役的 ir.rule xmlid 不得以 `<record>` 復活（#659 ②）。

    今天列管四個：`workspace_access_rule_member_access` / `workspace_access_rule_create`
    （#556，migration `18.0.1.0.122`）與 `workspace_rule_member_access` /
    `workspace_rule_create`（#658，migration `18.0.1.0.131`）。四個都出生在
    noupdate="1" 區塊、改 XML 修不動（見 ① 的 docstring），只能 DELETE 退役。
    ⚠ 權威清單是 `RETIRED_RULE_XMLIDS` 本身，不是這段散文——新增條目時**不必**
    回來改這裡的枚舉，但也不要把這裡讀成完整清單。把 `<record>` 帶回來（rebase／merge 復原、或有人
    「補回不小心刪掉的規則」）的後果是不對稱的：

      * 既有環境：migration 只跑一次，不會再 DELETE ⇒ 一時看不出來；
      * fresh install：XML 照樣重建它們 ⇒ 洞原樣重開，而且沒有任何測試會變紅。

    判準是「有沒有 `<record id=…>` 宣告」，**不是**「檔案裡有沒有這個字串」——那兩個
    名字今天仍**合法**出現在 `ir_rules.xml` 的註解、migration 與 landing doc 裡，用
    字串判準會把史料當違規（假陽性當場逼人刪掉註解，而註解正是下一個人唯一的線索）。
    `ET.parse` 丟掉註解，所以這個區分是解析層免費拿到的。

    不限 `model="ir.rule"`：同一個 xmlid 換一個 model 復活同樣會在 `ir_model_data`
    佔回那個名字，而「退役」的語意是這個名字整個不再使用。
    """
    what = "退役 xmlid 不得復活"
    records, errors = module_record_declarations(root)
    violations = _ir_rule_premise(records, errors, what)
    for rec in records:
        version = RETIRED_RULE_XMLIDS.get(rec["xmlid"])
        if not version:
            continue
        violations.append(
            "%s：`<record id=\"%s\" model=\"%s\">` 是已用 migration %s DELETE "
            "退役的 xmlid，不得再宣告。帶回來的後果不對稱：既有環境的 migration 只跑"
            "一次不會再刪，而 fresh install 會照 XML 重建它 ⇒ 授權洞無聲重開，且沒有"
            "任何測試會紅。要恢復這條規則請開新 issue 走新 xmlid ＋ 新 migration，"
            "不要復用退役名字（見 %s）"
            % (rec["rel"], rec["xmlid"], rec["model"] or "(無 model)", version,
               SECURITY_RULE_DOC))
    return violations


def _rule_model_ref(fields):
    """rule 綁的 model xmlid（`<field name="model_id" ref="…"/>`）；抽不到回 None。

    `ref=` 屬性是常態，`eval="ref('…')"` 也合法 ⇒ 兩種都接。只認 `ref=` 的話，換成
    eval 形態的當天判定會靜默變成「這條 rule 不綁任何 model」。
    """
    fld = fields.get("model_id")
    if fld is None:
        return None
    ref = (fld.get("ref") or "").strip()
    if ref:
        return _strip_module_prefix(ref)
    hit = re.search(r"ref\(\s*['\"]([^'\"]+)['\"]", fld.get("eval") or "")
    return _strip_module_prefix(hit.group(1)) if hit else None


def _rule_group_xmlids(fields):
    """rule 掛的 group xmlid 集合；**沒宣告 `groups` 欄位回 None**（＝ global rule）。

    回 None 與回 `set()` 的分辨是刻意的，但兩者底下其實混著**兩種語意不同**的輸入，
    這裡如實寫清楚（#659 驗收 F2）：

      * **沒宣告 `groups` 欄位** → None。Odoo 的 `global` 是 computed（`ir_rule.py ::
        _compute_global` ＝ `not rule.groups`），global rule 對所有人 AND 生效 ⇒ 對租戶
        構成限制 ⇒ 呼叫端算它涵蓋。這格判對。
      * **宣告了但一個 ref 都抽不到**，而這裡面又分兩種：
        - `eval="[]"` / `eval="[(6, 0, [])]"`（**顯式空 m2m**）：Odoo 那邊 `not groups`
          為真 ⇒ 同樣是 global ⇒ 實際涵蓋全體租戶，而本函式回空集合、呼叫端判「不涵蓋」
          ⇒ **過嚴誤報**（gate 紅、實際有守）。方向安全，刻意不修行為——修它要能區分
          「空 list」與「看不懂的 eval」，而後者必須維持保守。已知偏差寫在這裡，不要
          讀成「這格已經對齊」。
        - 看不懂的 eval 形態（變數、函式呼叫）：回空集合＝當「不涵蓋租戶」，紅不是綠。
          這才是原本 docstring 寫的 fail-loud 理由真正適用的那一半。
    """
    fld = fields.get("groups")
    if fld is None:
        return None
    raw = (fld.get("eval") or fld.text or "")
    return set(re.findall(r"ref\(\s*['\"]([^'\"]+)['\"]", raw))


def _rule_flag(fields, name):
    """讀 ir.rule 的布林欄位（`perm_*` / `active`）。

    🔴 **欄位沒宣告＝True**：Odoo 的 `ir.rule.perm_read/write/create/unlink` 與
    `active` 全部 `default=True`。把「沒寫」當 False 會讓一條省略 `perm_write` 的
    rule（合法且真的套用在 write 上）被判成沒守 write 方向——判定與 Odoo 實際行為
    相反。認不得的值當 False（保守方向：算成「這個方向沒被涵蓋」⇒ 紅）。

    ⚠ 走 `_field_literal_bool` 而**不是** `noupdate` 那條路徑的判定：這裡是 `<field>`
    的 `eval`（`safe_eval` 的 Python 字面），`noupdate` 是 XML 屬性（`nodeattr2bool` 的
    純黑名單）。兩者在 Odoo 就是不同的函式，gate 這邊也刻意不共用——F1 的成因正是
    共用一個 helper 讓 noupdate 那條路徑被字面判準拖偏。
    """
    fld = fields.get(name)
    if fld is None:
        return True
    return _field_literal_bool(fld.get("eval") or fld.text or "", False)


def _model_acl_note(root, model_ref):
    """違規訊息用的 ACL 實值子句；讀不到／查無該列一律回空字串。

    只服務**訊息**，不參與 pass/fail（判準與 ACL 脫鉤，見呼叫端 docstring）。所以這裡
    刻意 fail-soft：讀不到就少一句話，不會把一條安全守線變成「因為 csv 讀不到而紅」。

    存在的理由是反面：原本這句話把 `1,1,1,1` **寫死在訊息裡**，而那是今天才成立的值
    ⇒ 印在別的樹上就是假話（#659 驗收 F3）。即時讀出來的值不會過期；讀不到時省略整句
    比印一個猜測好。
    """
    path = os.path.join(root, ACL_CSV_REL)
    try:
        with open(path, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, UnicodeDecodeError, csv.Error):
        return ""
    for row in rows:
        model = _strip_module_prefix((row.get("model_id:id") or "").strip())
        group = (row.get("group_id:id") or "").strip()
        if model != model_ref or group != TENANT_GROUP_XMLID:
            continue
        perms = [(row.get("perm_" + name) or "0").strip()
                 for name in ("read", "write", "create", "unlink")]
        return ("；%s 今天給 %s 的 ACL 是 %s（read,write,create,unlink）——本判準與這個"
                "值無關，寫出來只是讓你判斷今天是否可被利用"
                % (ACL_CSV_REL, TENANT_GROUP_XMLID, ",".join(perms)))
    return ""


def _rule_direction_anchor_violations(records, model_ref, sibling_ref, what):
    """第 23／25 條的**錨點**前提：兩個 model 錨點互異，且各自真的有 ir.rule 在身上。

    🔴 存在的理由是一條實測過的靜默逃逸（#675 覆核 F1）：把 `WORKSPACE_MODEL_REF`
    改成 `WORKSPACE_ACCESS_MODEL_REF` 的值——也就是**複製貼上**的典型結果——之後，

        違規數=0；再把 workspace 本體四條 rule 全刪（剩餘 <record> 數 = 0）：
        違規數=0     EXIT=0

    **四條 rule 全刪掉、gate 仍然全綠。** 打錯字會紅（錨點對不到任何 rule ⇒ 三個
    方向全紅），但複製貼上到「另一個真的存在、而且真的有 rule 的 model」不會——而
    共用實作（`_rule_direction_violations`）＋兩個相鄰宣告的常數正是誘發複製貼上的
    形態。這與 #612／#613 記錄的「兩層判準退化成一層的複本」同型：判準本身逐行讀
    都對，錯的是它**看的是誰**。

    所以兩層都要，缺一不可，而且它們抓的不是同一件事：

      1. **互異**（`model_ref != sibling_ref`）：擋的就是上面那個複製貼上。兩個錨點
         相同時第 25 條看的是第 23 條那組 rule ⇒ 第 25 條退化成第 23 條的複本。
         兩個呼叫端互相把對方傳進來當 `sibling_ref`，所以不論被複製貼上的是哪一個
         常數，**兩條 check 都會紅**（單向傳只有一條會紅，而報紅的那條會是「被抄走
         的受害者」那一條，訊息指向錯的地方）。
      2. **各自有靶**：`model_ref` 至少要對到一條**宣告出來的** `<record
         model="ir.rule">`。這一層刻意**不看** `active` ／ groups ／方向旗標（那是
         判準層的事）——它證明的只是「錨點指到的 model 真的存在於這份 XML 裡」。
         `_ir_rule_premise` 只斷言 module 層抽得到 ir.rule，對 per-model 的錨點零
         鑑別力；沒有這一層，「錨點指到一個沒有任何 rule 的 model」與「這個 model
         的 rule 被刪光了」兩種病因的輸出完全相同（都是三條方向違規），而修法方向
         相反（前者要改常數，後者要補 rule）。

    ⚠ **已知殘留**（寫出來，不要讀成「錨點已經被完全釘死」）：把錨點改成**第三個**
    真的有 rule 的 model（例如 `model_woow_paas_platform_cloud_service`）時，這兩層
    都攔不下來——互異成立、靶也存在。那不是複製貼上的形態（要另外去別處抄一個名字
    來），本 gate 不涵蓋。

    那一格今天由 `scripts/audit/test_repo_lint_security_rules.py` 的
    `TestWorkspaceModelDirectionAnchor` 那支**注入**測試扛著
    （`test_flipping_the_workspace_manage_rule_reds_gate4_…`），而且扛住它的是這兩行：

        fixture.flip_perm(self.MANAGE_RULE, "perm_write")   # 錨在寫死的 rule xmlid
        got = rl.check_workspace_rule_directions(fixture.root)
        self.assertEqual(len(got), 1, ...)          # ← 漂到別的 model ⇒ got 縮成 []
        self.assertIn("perm_write", got[0], ...)    # ← 漂到「剛好也缺方向」的 model 時接手

    它有效的理由是**注入錨在真相源**（寫死的 `MANAGE_RULE` xmlid），不是錨在被測的
    `WORKSPACE_MODEL_REF` 上：翻 workspace manage rule 的 `perm_write` 對別的 model
    沒有任何影響，錨點一漂注入就打空。
    🔴 **不要把 `MANAGE_RULE` 改成從 `WORKSPACE_MODEL_REF` 推導**（例如「找出綁在這
    個錨點上的那條 manage rule」）——注入會跟著錨點一起漂，這一格就沒有守衛了。

    ⚠ 同 class 的 `test_the_two_direction_gates_look_at_disjoint_non_empty_rule_sets`
    **不**守這一格（它在這種漂移下必然是綠的，理由寫在它自己的 docstring 裡），不要
    把它讀成這裡的守衛——本段上一版就是那樣寫的，而照那個寫法「簡化」掉注入測試，
    唯一的守衛會在沒有任何訊號的情況下消失（#675 覆核 §1.1）。
    """
    violations = []
    if model_ref == sibling_ref:
        violations.append(
            "%s：「%s」與它的姊妹 check 的 model 錨點是同一個值（`%s`）——兩條 check "
            "共用 `_rule_direction_violations`，錨點一旦相同，後者看的就是前者那組 "
            "ir.rule ⇒ **兩層判準退化成一層**。實測後果：workspace 本體的 ir.rule "
            "全部刪光，gate 仍然 EXIT=0（#675 覆核 F1）。兩個常數相鄰宣告、實作又共用"
            "，複製貼上不會有任何訊號（打錯字才會紅）。請確認 `WORKSPACE_MODEL_REF` "
            "指的是 workspace **本體**、`WORKSPACE_ACCESS_MODEL_REF` 指的是成員關係列"
            % (REPO_LINT_SELF_REL, what, model_ref))
    declared = any(rec["model"] == IR_RULE_MODEL
                   and _rule_model_ref(rec["fields"]) == model_ref
                   for rec in records)
    if not declared:
        violations.append(
            "%s：「%s」的 model 錨點 `%s` 在整個 module 裡對不到任何一條宣告出來的 "
            "`<record model=\"%s\">`。這一層不看 active／groups／方向旗標（那是判準層"
            "的事），它證明的只是「錨點指到的 model 真的存在於這份 XML」⇒ 這條紅代表"
            "**錨點壞了**（常數打錯字、model 改名、或指到一個根本沒有 rule 的 model）"
            "，**不是**「少寫了幾條 rule」。`_ir_rule_premise` 只斷言 module 層抽得到 "
            "ir.rule，對 per-model 的錨點沒有鑑別力（#675 覆核 F1）"
            % (SECURITY_RULES_REL, what, model_ref, IR_RULE_MODEL))
    return violations


def _rule_direction_violations(root, model_ref, sibling_ref, what):
    """第 23／25 條共用的實作：`model_ref` 的三個寫入方向各要有一條租戶生效的 rule。

    🔴 刻意**不叫 `check_*`**：叫了就會被第 17 條那組雙射（檔頭條目 ↔
    `REPO_LINT_RULES` ↔ `run_all()` 實際呼叫）當成一條獨立的 check 函式，而它不是
    ——它是兩條 check 的共用實作。同一個理由見 `_repo_lint_registry_floor`。

    🔴 **共用實作就是 #675 覆核 F1 的成因**：兩條 check 之間唯一的差別只剩 `model_ref`
    這一個字串，於是「把它複製貼上成姊妹 check 的值」會讓後者靜默變成前者的複本。
    `sibling_ref` 參數存在的唯一目的就是讓那件事變成違規——見
    `_rule_direction_anchor_violations`。

    涵蓋一個方向的條件：`active` 且該 `perm_*` 為真 且（沒宣告 groups＝global／或
    groups 含 `base.group_user`）。判準與 ACL 脫鉤，理由見兩個呼叫端的 docstring。
    """
    records, errors = module_record_declarations(root)
    violations = _ir_rule_premise(records, errors, what)
    violations += _rule_direction_anchor_violations(
        records, model_ref, sibling_ref, what)
    covered = {direction: [] for direction in IR_RULE_WRITE_DIRECTIONS}
    for rec in records:
        if rec["model"] != IR_RULE_MODEL:
            continue
        if _rule_model_ref(rec["fields"]) != model_ref:
            continue
        fields = rec["fields"]
        if not _rule_flag(fields, "active"):
            continue
        groups = _rule_group_xmlids(fields)
        if groups is not None and TENANT_GROUP_XMLID not in groups:
            continue  # 只掛 group_system（或其他群組）⇒ 對租戶不構成限制
        for direction in IR_RULE_WRITE_DIRECTIONS:
            if _rule_flag(fields, direction):
                covered[direction].append(rec["xmlid"])
    for direction in IR_RULE_WRITE_DIRECTIONS:
        if covered[direction]:
            continue
        violations.append(
            "%s：`%s` 沒有任何一條對 `%s` 生效的 ir.rule 涵蓋 `%s` 方向（active 且 "
            "%s=True 且 groups 含 %s 或無 groups）。Odoo `_get_rules` 對租戶查無 rule "
            "⇒ `_compute_domain` 回空 domain ＝**該方向在規則層完全不設限**，任何內部"
            "帳號對任何租戶的列都沒有列層級過濾（#519 的地基）。⚠ 只掛 "
            "base.group_system 的 `*_rule_admin_full` **不算**涵蓋：它對純 "
            "base.group_user 帳號完全不生效，補它只會讓 gate 轉綠而洞還在（見 %s）%s"
            % (SECURITY_RULES_REL, model_ref, TENANT_GROUP_XMLID,
               direction, direction, TENANT_GROUP_XMLID, SECURITY_RULE_DOC,
               _model_acl_note(root, model_ref)))
    return violations


def check_workspace_access_rule_directions(root):
    """`workspace_access` 的每個寫入方向都要有一條**對租戶生效**的 ir.rule（#659 ③）。

    `perm_write` / `perm_create` / `perm_unlink` 各至少一條。缺任一方向，Odoo 的
    `ir.rule._get_rules` 對該使用者查無 rule、`_compute_domain` 回空 domain
    ＝**該方向在規則層完全不設限**。這是 #519 的地基，也是最容易被「這條規則看起來
    多餘」順手簡化掉、且失效**完全無聲**的一種。

    判準**刻意與 ACL 脫鉤**：本 check 不讀 `ir.model.access.csv`，缺方向就紅，即使
    ACL 今天恰好擋著該操作。理由是縱深防禦——ACL 日後被放寬時規則層必須已經在，而
    「ACL 現在擋著」這件事沒有任何守線盯著（編輯 ACL csv 接不到本 gate）。
    ⚠ 因此違規訊息**不得寫死 ACL 的現值**：#659 驗收 V2 實測，原本逐字寫「ACL 給
    base.group_user 的是 1,1,1,1」的訊息印在 #556 修復前的樹上時是假話（那時是
    `1,1,1,0`）。訊息現在由 `_model_acl_note(root, model_ref)` 即時讀出實值，讀不到就
    整句省略（訊息用途，不 fail-loud）。（#658 把它從 `_workspace_access_acl_note()`
    改名並加上 `model_ref` 參數，好讓第 24 條 gate 共用同一支；這行 docstring 當時漏改，
    指著一個已經不存在的函式名。）

    🔴 判準刻意**不是**「這個 model 有沒有任一條 ir.rule」：`_get_rules` 的 SQL 只收
    `r.global OR r.id IN (使用者所屬 group 的 rule)`，所以一條只掛 `base.group_system`
    的 `*_rule_admin_full` 對純 `base.group_user` 租戶帳號**完全不構成任何限制**。本
    repo 的命名慣例正好讓每個 model 都配一條 `admin_full` ⇒ 寬判準會把「洞開著」讀成
    「有守」，而且「補一條 admin_full」就能讓 gate 轉綠卻沒修任何東西。這個假綠不是
    理論風險：#524（smart_home 無 record rule）當初就是拿嚴判準配陽性對照在 prod DB
    實測才收斂的。實測本 repo 現況：刪掉 `workspace_access_rule_owner_admin_manage`
    之後，寬判準三個方向全綠（`admin_full` 頂上去了），嚴判準三個方向全紅。

    因此涵蓋一個方向的條件是：`active` 且 該 `perm_*` 為真 且（沒宣告 groups＝global
    ／或 groups 含 `base.group_user`）。

    ⚠ 只驗**存在性**（哪個方向有沒有規則），不驗 domain 寫得對不對——一條
    `[(1, '=', 1)]` 的 group_user rule 會被算成「有守」。domain 語意由
    `src/tests/` 的行為測試守（見 docs/tracking/landings/556-workspace-access-authz-landing.md）。
    """
    return _rule_direction_violations(
        root, WORKSPACE_ACCESS_MODEL_REF, WORKSPACE_MODEL_REF,
        "workspace_access 寫入方向存在性")


def check_workspace_rule_directions(root):
    """`workspace` **本體**的每個寫入方向都要有一條對租戶生效的 ir.rule（#658）。

    判準與 `check_workspace_access_rule_directions`（#659 ③）逐字相同，只是換一個
    model——兩個 model 的 rule 集合互相獨立，守住成員關係列不代表守住了被保護的資源
    本身。#658 的洞正是這個分岔：`workspace_access` 在 #556 收窄成四條 role-aware
    rule 之後，`workspace` 本體仍只有一條 perm_read+perm_write 合一、domain 無 role
    條件的 `workspace_rule_member_access`，任何 guest 成員照樣能改 `owner_id`
    （帳單 partner ＋ 配額計數的錨）與 `slug`（K8s namespace／subdomain 的唯一輸入）。

    修復後 `perm_write` 方向只剩 `workspace_rule_owner_admin_manage` 一條在守，
    `perm_unlink` 只剩 `workspace_rule_owner_unlink`。後者今天**不放行任何東西**
    （ACL 的 `perm_unlink=0` 擋在前面，而 ir.rule 只會限縮不會授予），所以它看起來
    最像「多餘、順手刪掉」的那一條——這條 gate 就是為了讓那一刀變紅。

    ⚠ 與第 23 條一樣只驗**存在性**，不驗 domain 寫得對不對：一條
    `[(1, '=', 1)]` 的 group_user rule 會被算成「有守」。domain 語意由
    `src/tests/test_workspace_authz_658.py` 的行為測試守。

    🔴 本 check 與第 23 條之間唯一的差別就是 `WORKSPACE_MODEL_REF` 這一個字串，所以
    「它到底指向哪個 model」本身是要被守的前提，而不是可以靠讀 code 目視確認的細節
    ——#675 覆核實測：把它複製貼上成 `WORKSPACE_ACCESS_MODEL_REF` 的值之後，本 check
    退化成第 23 條的複本，workspace 本體四條 rule 全刪掉仍然 EXIT=0。那道前提現在由
    `_rule_direction_anchor_violations` 機械檢查（互異 ＋ 各自有靶）。
    """
    return _rule_direction_violations(
        root, WORKSPACE_MODEL_REF, WORKSPACE_ACCESS_MODEL_REF,
        "workspace 寫入方向存在性")


def _template_is_hidden_via_service_lines(src):
    """回傳 ``<x>.template_id.is_hidden`` 屬性鏈的行號 list（AST 判定）。

    只認「經由 ``template_id`` 取到 template 再讀 ``is_hidden``」這個形態——那必然是
    在判**某個既有 service 的**可見性。曝光層的 ``template.is_hidden``（直接持有
    template 變數，如 marketplace domain 與 template by-id）不匹配，因此 B 組四處
    不會被誤報。

    用 AST 而非 regex：註解與 docstring 裡本來就會出現 `template_id.is_hidden`
    這串字（本次拆分的說明文字就是），regex 會全部誤報。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # 不 parse 的檔略過（fail-open，對齊本工具哲學）
    lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == CONCEAL_WRONG_FIELD
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "template_id"):
            lines.append(node.lineno)
    return sorted(set(lines))


def check_conceal_instances_gate(root, only_file=None):
    """既有 service 的可見性判準不得是 ``template_id.is_hidden``（#471）。

    #471：原本一個 ``is_hidden`` 同時承載「不上架」與「既有實例整條鏈消失」，於是
    任何「暫時下架」的一般商品都會把租戶手上正在跑的服務一起從前台抹掉——詳情頁
    打不開、rollback／change-plan／MCP 設定全回 Service not found（#464 實測
    hermes-agent）。拆成 ``is_hidden``（曝光層）＋ ``conceal_instances``（實例層）後，
    唯一的退化路徑就是**有人新增一個既有服務的操作端點、照抄舊 gate**。這條 gate
    把那次照抄從「三個月後租戶回報服務不見了」提前成「寫下去當下就被擋」。

    依據 docs/decisions/patterns/hidden-template-presentation-chain.md
    """
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if rel.startswith("src/tests/"):
            continue  # 測試會刻意提及舊判準（紅燈對照、回歸描述）
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        for line_no in _template_is_hidden_via_service_lines(src):
            violations.append(
                "%s:%d：用 `template_id.%s` 判既有 service 的可見性。實例層判準是 "
                "`template_id.%s`——`%s` 只剩**曝光層**語意（marketplace 不列／template "
                "詳情與 plans 不可讀／generic create 拒收，那四處直接持有 template 變數、"
                "不經 template_id）。照抄舊 gate 的後果是「暫時下架一個商品」把租戶手上 "
                "state=running 的服務一起變成 Service not found（#471／#464）。"
                "曝光層 gate 請改成直接讀 template 的形態，見 %s"
                % (rel, line_no, CONCEAL_WRONG_FIELD, CONCEAL_FIELD,
                   CONCEAL_WRONG_FIELD, HIDDEN_CHAIN_DOC))
    return violations


def check_transition_anchor_utc(root, only_file=None):
    """轉移狀態的時間錨不得用 ``datetime.now()`` 寫入（#489）。

    ``deployed_at`` / ``last_upgraded_at`` 從 #489 起是 age timeout 的**判定用**
    錨，不再只是前台顯示。Odoo 的 Datetime 欄位契約是 **UTC naive**，而 Python
    的 ``datetime.now()`` 回的是**本機時區** naive——兩者在非 UTC 的伺服器上不
    同基準：

      * UTC+8 → 錨比 ``fields.Datetime.now()`` 大 8 小時 → age 恆為負 →
        **逾時保護靜默失效**；
      * UTC-x → age 虛增 → **誤殺**（而誤殺不可自癒：poll 只撈轉移狀態，
        寫進 error 後沒有任何路徑會把它 poll 回 running）。

    測試環境的容器時區是 UTC，所以這個缺陷**測不出來**——只能靠 gate。
    （合併前 `controllers/paas.py` 的 reconfigure 路徑正是這個寫法。）
    """
    # #627 (i)：`last_release_sync_at` 同屬「從 operator 事實 stamp 的時間錨」，
    # 一併納入——同一類坑（Odoo Datetime 契約是 UTC naive，`datetime.now()` 回本機
    # 時區），成本幾乎為零。
    anchor_fields = {"deployed_at", "last_upgraded_at", "last_release_sync_at"}
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if rel.startswith("src/tests/"):
            continue
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value in anchor_fields):
                    continue
                if not (isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Attribute)
                        and value.func.attr == "now"):
                    continue
                # 合格：fields.Datetime.now()。不合格：datetime.now() /
                # datetime.datetime.now()（本機時區）。
                owner = value.func.value
                if isinstance(owner, ast.Attribute) and owner.attr == "Datetime":
                    continue
                violations.append(
                    "%s:%d：`%s` 用 datetime.now() 寫入。#489 起這是 age timeout 的判定"
                    "用錨，必須與 fields.Datetime.now() 同基準（Odoo Datetime 欄位是 UTC "
                    "naive；datetime.now() 是本機時區）。非 UTC 伺服器上這會讓逾時保護"
                    "靜默失效或誤殺，而測試容器是 UTC → 測不出來。"
                    % (rel, getattr(value, "lineno", key.lineno), key.value))
    return violations


def check_mixin_subscription_sudo(root, only_file=None):
    """``subscription_lifecycle_mixin`` 內不得裸讀 ``self.subscription_id``（#29）。

    ``subscription_id`` 指向 ``sh.subscription.subscription``，其 read ACL 只發給
    ``group_manager_sh_subscription`` / ``group_user_sh_subscription`` /
    ``base.group_portal``。workspace 擁有權是**平台側**概念、不連帶給 Odoo 群組，
    所以 mixin 內部一裸讀，典型租戶（只有 ``base.group_user``）就在 env cache 落空
    時撞 AccessError——SH delete / provision、CS update / delete 回
    "You are not allowed to access sh.subscription.subscription"。

    這條缺陷**測不出來也看不出來**的原因是 cache：admin 端的任何前置讀取都會餵飽
    transaction 級 cache，讓後續 user-scoped traversal 不再查 ACL（#27 當年就是被
    這個假象騙過，把稽核界線畫在 controller）。production 沒有這個假象——每個 HTTP
    request 都從空 cache 開始。

    修法是把 traversal 收斂到唯一入口 ``_sub()``（回 ``self.sudo()
    .subscription_id``）。這條 gate 守的就是「唯一入口」這個不變式：新增方法時照抄
    舊寫法會在編輯當下被擋，而不是三個月後由租戶回報。
    """
    violations = []
    target = os.path.join(root, MIXIN_SUDO_FILE)
    if only_file and os.path.abspath(only_file) != os.path.abspath(target):
        return violations
    if not os.path.exists(target):
        return violations
    with open(target, encoding="utf-8", errors="replace") as f:
        src = f.read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return violations  # fail-open，對齊本工具哲學
    for node in ast.walk(tree):
        # 只認 `self.subscription_id`（Name self）。`self.sudo().subscription_id`
        # 的 value 是 Call，不匹配；docstring / 註解裡的同名字串也不匹配（AST）。
        if (isinstance(node, ast.Attribute)
                and node.attr == "subscription_id"
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            violations.append(
                "%s:%d：裸讀 `self.subscription_id`。mixin 內部的 subscription "
                "traversal 一律走 `self._sub()`（sudo）——workspace 擁有者拿不到 "
                "sh_subscription 的 Odoo 群組，裸讀會讓典型租戶在 cache 落空時撞 "
                "AccessError（#29；controller 端同 class 缺陷見 #27／PR #28）。"
                "sudo 在這裡不是權限放大：controller 已先做完 workspace 歸屬 + "
                "role 兩道閘。"
                % (MIXIN_SUDO_FILE, node.lineno))
    return violations


CONFIG_PARAM_HOOKS_FILE = "src/hooks.py"
CONFIG_PARAM_DOC = "CLAUDE.md「使用 config_parameter 時的卸載清理」"


def _declared_config_parameters(root):
    """全 `src/**/*.py` 上宣告的 `config_parameter=` 字面（AST，避開註解／字串）。

    回 `(found, errors)`，`found` 是 `{key: "rel:lineno"}`、`errors` 是字串 list。

    ⚠ **掃全 src 而不是死寫 `res_config_settings.py` 一個檔**。目前 repo 內 17 個
    宣告確實全在那一檔，但那是**現況、不是約束**——`config_parameter=` 是欄位層的
    kwarg，任何 `TransientModel` 都能用。死寫單檔的話，有人在別處宣告就靜默漏掉，
    而 gate 照樣回綠 ⇒ 這條守線變成「靠遺漏成立的保證」，正是它自己要防的形態。
    掃全 src 之後，涵蓋範圍與「哪個檔宣告」解耦。

    `src/tests/` 排除：測試可能為了驗 gate 本身而寫假宣告，計入會誤報。

    SyntaxError 必須被接住並轉成 violation——本 gate 排在
    `check_duplicate_class_methods` 之前，讓例外往外拋會使整輪 repo-lint 以
    traceback 中斷，**其餘 13 條 gate 的違規完全不輸出**。那是「一條爆掉全部不跑」
    的靜默失效，比本 gate 自己漏抓更嚴重。
    """
    found, errors = {}, []
    for fp in sorted(glob.glob(os.path.join(root, "src/**/*.py"), recursive=True)):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if rel.startswith("src/tests/"):
            continue
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                tree = ast.parse(f.read())
        except SyntaxError as exc:
            errors.append(
                "%s 有語法錯誤（line %s），無法檢查 config_parameter 宣告：%s"
                % (rel, exc.lineno, exc.msg))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "config_parameter" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    found.setdefault(kw.value.value, "%s:%d" % (rel, node.lineno))
    return found, errors


def _uninstall_cleanup_parameters(path):
    """`hooks.py` 的 `_CONFIG_PARAMETERS` list 字面（AST）。回 `(keys, error)`。

    ⚠ **只掃 module scope（`tree.body`），且多個指派要 fail-loud**。原本用
    `ast.walk` 並在第一個匹配就 return，有兩個獨立問題：

    1. 取到的是**第一個**指派，但 Python 生效的是**最後一個**。有人在檔案後段
       再寫一次 `_CONFIG_PARAMETERS = [...]`（靜默覆蓋前者），本檢查會拿前者去
       比對 ⇒ 實際生效的清單漏登記時**不會報**。
    2. 那個盲區正好落在 `check_duplicate_class_methods` 的覆蓋範圍之外（它只看
       class body、不看 module scope），而 `hooks.py` 全檔都是 module-level
       ⇒ 兩條 gate 的盲區交集把這一條整條關掉。

    現在偵測到多個 module-level 指派就回 error 而非猜一個——這種寫法本身就是
    可疑的，靜默取最後一個只會把問題往後推。`ast.walk` 改成只看 `tree.body`
    順帶排除「函式內同名區域變數」的誤判。
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except SyntaxError as exc:
        return None, "%s 有語法錯誤（line %s），無法檢查卸載清理清單：%s" % (
            CONFIG_PARAM_HOOKS_FILE, exc.lineno, exc.msg)
    assigns = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_CONFIG_PARAMETERS" not in names:
            continue
        assigns.append(node)
    if not assigns:
        return None, ("%s：找不到 module scope 的 `_CONFIG_PARAMETERS` 指派，"
                      "卸載清理的守線失效。" % CONFIG_PARAM_HOOKS_FILE)
    if len(assigns) > 1:
        return None, (
            "%s：`_CONFIG_PARAMETERS` 在 module scope 被指派 %d 次（line %s）。"
            "Python 生效的是最後一個，前面的全被靜默覆蓋——這種寫法讓「清單裡有什麼」"
            "無法一眼判定，本檢查拒絕猜。請合併成單一指派。"
            % (CONFIG_PARAM_HOOKS_FILE, len(assigns),
               ", ".join(str(a.lineno) for a in assigns)))
    node = assigns[0]
    if not isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
        return None, ("%s:%d：`_CONFIG_PARAMETERS` 不是 list/tuple/set 字面，"
                      "本檢查只認字面量（見 docstring 的覆蓋邊界）。"
                      % (CONFIG_PARAM_HOOKS_FILE, node.lineno))
    return {e.value for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)}, None


def check_config_parameter_uninstall(root, only_file=None):
    """`config_parameter` 欄位的 key 必須進 `hooks.py` 的 `_CONFIG_PARAMETERS`。

    Odoo 卸載 addon **不會**清 `ir.config_parameter`，所以每個 `config_parameter=`
    都要在 uninstall hook 的清單裡登記一筆，否則卸載後留下孤兒設定（重裝時被讀到、
    值卻是上一輪的）。CLAUDE.md 明載這條義務，但在 #501 之前**沒有任何機械保護**
    ——漏了只能靠 review 抓，而 review 抓不到「少寫一行」。

    只查一個方向（宣告了卻沒登記）。反向（登記了卻沒有對應欄位）刻意不擋：清單裡
    本來就有歷史遺留與非 settings 來源的 key（`_CONFIG_PARAMETERS` 現有 25 個 key，
    其中 8 個是 `get_param` 呼叫點生出來的、不對應任何欄位），擋它會逼人刪掉仍需
    清理的項目。

    ⚠ **覆蓋邊界**：兩側都只認**字面量**。`config_parameter=` 若寫成運算式
    （字串串接、變數、f-string），或 `_CONFIG_PARAMETERS` 用推導式產生，本檢查會
    **靜默略過**、不報 violation —— 綠燈不代表沒有漏登記的 key。目前 repo 內兩側
    都是字面量（17 個宣告 / 一個 list literal），維持這個形態本檢查才有效力；要改成
    動態產生時，請連同本檢查一起重寫，不要以為它還在守。

    宣告側掃**全 `src/**/*.py`**（排除 `src/tests/`）而非死寫單一檔案，理由見
    `_declared_config_parameters` 的 docstring。
    """
    hooks_path = os.path.join(root, CONFIG_PARAM_HOOKS_FILE)
    if not os.path.exists(hooks_path):
        return []
    # 不變式跨「全 src 的宣告」與 hooks.py 兩側，動到任一個 src/*.py 都整條重算。
    # 宣告可能出現在任何檔，只認固定兩個檔名的切片會讓新檔案的宣告漏檢。
    if only_file:
        rel = os.path.relpath(
            os.path.abspath(only_file), os.path.abspath(root)
        ).replace(os.sep, "/")
        if not (rel.startswith("src/") and rel.endswith(".py")):
            return []
    declared, declared_errs = _declared_config_parameters(root)
    registered, registered_err = _uninstall_cleanup_parameters(hooks_path)
    # 任一側取不到就 fail-loud 並**停在這裡**：拿殘缺的一側去比對只會產生誤導的
    # violation 清單（例如語法錯誤讓 declared 為空 ⇒ 「零違規」＝假綠）。
    if declared_errs or registered_err:
        return declared_errs + ([registered_err] if registered_err else [])
    violations = []
    for key, where in sorted(declared.items(), key=lambda kv: kv[1]):
        if key not in registered:
            violations.append(
                "%s：config_parameter %r 沒有登記進 %s 的 `_CONFIG_PARAMETERS`。"
                "Odoo 卸載 addon 不會清 ir.config_parameter，漏登記＝卸載後留下孤兒"
                "設定（重裝時被讀到、值卻是上一輪的）。把這個 key 加進該清單即可"
                "（見 %s）。"
                % (where, key, CONFIG_PARAM_HOOKS_FILE, CONFIG_PARAM_DOC))
    return violations


DUP_METHOD_DOC = "docs/decisions/patterns/README.md"


def _accessor_or_overload(node):
    """`@x.setter/getter/deleter` 與 `@overload` 是**刻意**的同名成對定義，豁免。"""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Attribute) and dec.attr in (
                'setter', 'getter', 'deleter'):
            return True
        name = dec.id if isinstance(dec, ast.Name) else (
            dec.attr if isinstance(dec, ast.Attribute) else None)
        if name == 'overload':
            return True
    return False


def _duplicate_methods(class_node):
    """回傳 [(method_name, first_lineno, dup_lineno)]——只看 class body 的直接子節點。"""
    seen, dups = {}, []
    for child in class_node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _accessor_or_overload(child):
            continue
        if child.name in seen:
            dups.append((child.name, seen[child.name], child.lineno))
        seen[child.name] = child.lineno
    return dups


def check_duplicate_class_methods(root, only_file=None):
    """同一個 class body 內不得重複定義同名 method。

    Python **靜默接受**重複定義，後者覆蓋前者——沒有錯誤、沒有警告、沒有 log。
    在 Odoo model 上這等於「新加的 override 把既有的 override 整個關掉」，而既有
    的那個往往是別人辛苦補的守衛。

    實例（本檢查的來源，#501 PR #517）：為了擋「先建成 deleting 再 write 回來」
    的上限繞道，新增了第二個 `CloudService.write()`，靜默覆蓋掉 #279 Path B 那個
    「不可變更已部署服務的 workspace」的租戶隔離守衛。針對性測試涵蓋不到那條
    路徑，**只有全套會紅**（`TestReachSyncTriggers.test_cs_workspace_reassign_on_live_ns_rejected`
    → `UserError not raised`），而全套很貴、又容易被不乾淨的基準掩蓋成「本來就紅」。

    測試檔同樣涵蓋：重複的 test method 會讓前一個**永遠不執行**，是無聲的假綠。

    ⚠ **覆蓋邊界**（同 config-parameter-uninstall 的標準，寫明才不會被誤信）：
    - 只比對**同一個 class body 的直接子節點**。跨 class 不比（那是正常的繼承
      覆蓋）；巢狀 class 各自獨立比對。
    - `if` / `try` / `with` 區塊**內**的 `def` **不納入**比對——條件式定義是合法
      形態（依版本／可選相依定義不同實作）。代價：「兩個分支各定義一次且都會執行」
      這種真的會覆蓋的寫法抓不到。目前 repo 內沒有這種用法；若日後出現，這條
      gate 對它是靜默的。
    - `@property` 的 `setter/getter/deleter` 與 `typing.overload` 豁免（刻意同名）。
    - 只掃 `src/**/*.py`。
    - 🔴 **不含 module scope**：module-level 的重複 `def` 或重複變數指派（例如
      `hooks.py` 全檔都是 module-level）**不在本 gate 的範圍**。這個盲區曾經與
      `config-parameter-uninstall` 的盲區交集、把那一條整條關掉（它取第一個
      `_CONFIG_PARAMETERS` 指派，而 Python 生效的是最後一個 ⇒ 有人在後面再指派
      一次就靜默覆蓋，兩條 gate 都看不見）。該條已改成自己偵測多重指派並
      fail-loud；本 gate 的邊界維持不變，但**新增 module-level 不變式時不要指望
      這條 gate 守它**。
    """
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp) or not fp.endswith(".py"):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if not rel.startswith("src/"):
            continue
        with open(fp, encoding="utf-8", errors="replace") as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                continue  # 語法錯誤由 Python 自己報，這裡不重複
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for name, first, dup in _duplicate_methods(node):
                violations.append(
                    "%s:%d：class `%s` 重複定義 `%s()`（前一個在 :%d）。Python 靜默"
                    "採用後者、前者整段失效——沒有錯誤也沒有警告，而前者往往是既有的"
                    "守衛（#501 實例：新增的 write() 覆蓋掉 #279 擋 workspace 重指派的"
                    "租戶隔離守衛，只有全套會紅）。要擴充既有 method 就**併進那一個**，"
                    "不要新增第二個定義。（見 %s）"
                    % (rel, dup, node.name, name, first, DUP_METHOD_DOC))
    return violations


# ---------------------------------------------------------------- #547 登記表雙射

PATTERN_DOC_DIR = "docs/decisions/patterns"
PATTERN_DOC_INDEX = "docs/decisions/patterns/README.md"
PATTERN_GUARD_MAP = ".claude/hooks/pattern-guard-map.json"
# 本 check 的處方與「維護慣例」都寫在索引檔自己裡（1. 同步登錄索引 2. 有路徑才進 map）。
PATTERN_REGISTRY_DOC = PATTERN_DOC_INDEX

# 刻意**不**納 pattern-guard-map 的 pattern doc（＋理由）。
#
# 依據 docs/decisions/patterns/README.md「維護慣例」第 2 條：「純判斷類（無路徑可綁）
# 的 pattern **只留本索引**」。這兩份守的是 agent hand-off 當下的**事件**，不是某個
# 檔案路徑——硬塞進 map 只會讓某個 class 的必讀清單再長一份、稀釋 payload（#547 的
# 危害機制正是「這份 doc 一定會被讀到」）。
#
# 🔴 這裡不是待辦清單，是設計裁決，每加一筆都要在 PR 說明理由。而且它是**雙向**被
# 守的：列在這裡卻其實已經進了 map（或檔案已不存在）一樣會紅，所以它不能拿來洗白
# 「還沒登記」的 doc。
PATTERN_DOC_MAP_EXEMPT = {
    "docs/decisions/patterns/plan-gap-stop-and-report.md":
        "agent 執行 hand-off 撞到 plan gap 時的處置紀律；觸發條件是事件不是路徑",
    "docs/decisions/patterns/phase-leader-brief-as-pointer-not-source.md":
        "brief 與 source-of-truth 衝突時的處置紀律；同上，無檔案路徑可綁",
}

# 只收「同目錄的裸 .md 檔名」。舊寫法 `([^)]+)` 完全不設限，「完整檔案清單」段裡
# **任何** markdown 連結都被當成登記項——外部 URL 經 os.path.basename 之後會變成一份
# 憑空的 pattern doc（`https://…/x.md` → `x.md`），訊息還寫「改名／刪除沒同步？」，
# 把人導去找一個從來不存在的檔案（#576）。含 :// 或路徑分隔的目標一律不是登記項。
_PATTERN_INDEX_LINK_RE = re.compile(r"^\s*-\s*\[[^\]]*\]\(([A-Za-z0-9._-]+\.md)\)\s*$")
_PATTERN_INDEX_WIKILINK_RE = re.compile(r"^\[\[([^\]]+)\]\]$")
_PATTERN_INDEX_COUNT_RE = re.compile(r"完整\s*(\d+)\s*條")


def _pattern_doc_files(root):
    """目錄實況：`docs/decisions/patterns/*.md`（索引檔 README.md 自己不算一份）。"""
    out = set()
    for path in glob.glob(os.path.join(root, PATTERN_DOC_DIR, "*.md")):
        base = os.path.basename(path)
        if base == "README.md":
            continue
        out.add("%s/%s" % (PATTERN_DOC_DIR, base))
    return out


def _pattern_guard_map_entries(root):
    """回 (entries, errors)。entries 是 (class, path, raw) 三元組。

    路徑取條目的 em-dash 前段——map 的登記格式是 `路徑 — 一句何時必看`
    （README.md「維護慣例」2 明訂）。取**第一個** em-dash 之前，所以描述裡再出現
    em-dash 不影響。
    """
    path = os.path.join(root, PATTERN_GUARD_MAP)
    if not os.path.exists(path):
        return [], ["%s 不存在——pattern-guard hook 的對照表是本檢查的登記表之一，"
                    "缺檔等於整條守線消失（見 %s）。"
                    % (PATTERN_GUARD_MAP, PATTERN_REGISTRY_DOC)]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError) as exc:
        # fail-loud：解析不了就**停在這裡**。拿空清單去比對會得出「map 什麼都沒登記」
        # 這種誤導的 violation 海，真因（JSON 壞了）反而被淹掉。
        return [], ["%s 解析失敗：%s。pattern-guard.py 對此是 fail-open（`except: allow()`）"
                    "⇒ JSON 一壞，11 個 class 的必讀守線全部靜默失效，**沒有任何訊號**"
                    "（見 %s）。" % (PATTERN_GUARD_MAP, exc, PATTERN_REGISTRY_DOC)]
    entries, errors = [], []
    classes = data.get("classes")
    if not isinstance(classes, list):
        return [], ["%s 缺少 `classes` 陣列（見 %s）。"
                    % (PATTERN_GUARD_MAP, PATTERN_REGISTRY_DOC)]
    for idx, cls in enumerate(classes):
        name = cls.get("class") or "#%d" % idx
        pattern = cls.get("pattern")
        if not pattern:
            errors.append("%s：class `%s` 沒有 `pattern`，pattern-guard.py 會 KeyError "
                          "→ 全域 fail-open（見 %s）。"
                          % (PATTERN_GUARD_MAP, name, PATTERN_REGISTRY_DOC))
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                # pattern-guard.py:64 對 re.error 是 `continue` ⇒ 這個 class 整個
                # 消失、**不會有任何錯誤訊息**。編不起來的 regex 必須在 CI 就紅。
                errors.append("%s：class `%s` 的 pattern 編不起來（%s）。pattern-guard.py "
                              "對 re.error 是靜默 `continue` ⇒ 這個 class 的守線整條"
                              "消失且沒有任何訊號（見 %s）。"
                              % (PATTERN_GUARD_MAP, name, exc, PATTERN_REGISTRY_DOC))
        for raw in cls.get("docs") or []:
            entries.append((name, raw.split("—", 1)[0].strip(), raw))
    return entries, errors


def _pattern_index_registers(root):
    """回 (toc, topic, counts, errors)：README 索引的三個登記面。

    - toc   ：「完整檔案清單」的 markdown 連結目標
    - topic ：「何時必看」各主題表**第一欄**的 `[[slug]]`（只認第一欄——描述欄裡的
              交叉引用不是登記，把它算進去會讓「只被別人提到、自己沒有列」的 doc
              誤判成已登記）
    - counts：頂部「完整 N 條」宣告的所有 N
    """
    path = os.path.join(root, PATTERN_DOC_INDEX)
    if not os.path.exists(path):
        return set(), set(), [], [
            "%s 不存在——它是 pattern doc 的人工索引，缺檔等於 CLAUDE.md 指針化之後"
            "沒有任何地方列得出完整清單（見 %s）。"
            % (PATTERN_DOC_INDEX, PATTERN_REGISTRY_DOC)]
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    toc, topic, section = set(), set(), None
    for line in lines:
        if line.startswith("## "):
            if line.startswith("## 完整檔案清單"):
                section = "toc"
            elif line.startswith("## 何時必看"):
                section = "topic"
            else:
                section = None
            continue
        if section == "toc":
            m = _PATTERN_INDEX_LINK_RE.match(line)
            if m:
                toc.add("%s/%s" % (PATTERN_DOC_DIR, os.path.basename(m.group(1))))
        elif section == "topic" and line.lstrip().startswith("|"):
            first = line.strip().strip("|").split("|")[0].strip()
            m = _PATTERN_INDEX_WIKILINK_RE.match(first)
            if m:
                topic.add("%s/%s.md" % (PATTERN_DOC_DIR, m.group(1)))
    # 「完整 N 條」只認**頂部宣告**（第一個 `## ` 之前）。舊寫法 findall 全檔，訊息卻
    # 寫「頂部宣告」⇒ 任何人往散文寫一句含數字的話（「本節的判準完整 7 條」）就會觸發，
    # 而訊息把人導向頂部那行——那裡的數字已經是對的，照著去改永遠修不好（#576）。
    # 選限縮範圍而不是改寫訊息：訊息已經對讀者承諾了「頂部」這個語意，讓實作對齊承諾
    # 比放寬承諾好，而且限縮同時消滅了假陽性，改訊息只是讓假陽性比較好懂。
    header_end = next((i for i, line in enumerate(lines) if line.startswith("## ")),
                      len(lines))
    counts = [int(n) for n in
              _PATTERN_INDEX_COUNT_RE.findall("\n".join(lines[:header_end]))]
    return toc, topic, counts, []


def _registry_diff(violations, actual, registered, what, fix, extra="", reverse=True):
    """把兩個集合的差報成 violation。

    🔴 這裡刻意寫成集合差而不是 `for x in 登記表: assert exists(x)`。後者是宣告式
    清單的經典假綠洞：**把一筆從登記表拿掉＝迴圈少跑一輪＝照樣綠**，缺陷沒修但顏色
    是對的（見 seed-topology-invariants.md：會下降的量不可用 monotonic 守衛）。

    `reverse=False` 用在「登記表 → 目錄」那個方向**已經有更精確的訊息**的情況
    （map 那一側由 dangling-ref 逐筆點名 class，豁免清單那一側有自己的訊息）。
    刻意不做的事要寫成參數而不是註解——INJ-4 實測過：註解寫「反向不報」而程式碼照
    報，同一次注入被報成兩筆。
    """
    for rel in sorted(actual - registered):
        violations.append(
            "%s：目錄裡有這份 pattern doc，但 %s 沒有登記它。%s%s（見 %s）"
            % (rel, what, fix, extra, PATTERN_REGISTRY_DOC))
    if not reverse:
        return
    for rel in sorted(registered - actual):
        violations.append(
            "%s：%s 登記了它，但 `%s/` 裡沒有這個檔案（改名／刪除沒同步？）。"
            "登記表指向不存在的檔案，讀的人會以為那份守線還在（見 %s）"
            % (rel, what, PATTERN_DOC_DIR, PATTERN_REGISTRY_DOC))


#: #627：poll 有兩份實作，帳本回寫的欄位集合必須來自同一個 producer。
POLL_MODEL_FILE = "src/models/cloud_service.py"
POLL_CONTROLLER_FILE = "src/controllers/paas.py"
POLL_RELEASE_FACTS_FILES = (POLL_MODEL_FILE, POLL_CONTROLLER_FILE)
#: (檔案, 函式名)：兩份 **poll** 實作的進入點——它們會做狀態轉移，故受判準 3／4 管。
_POLL_IMPLEMENTATIONS = (
    (POLL_MODEL_FILE, "_poll_and_update_status"),
    (POLL_CONTROLLER_FILE, "_update_service_status"),
)
#: #627 (i)：對帳 cron 是**第三個** release 事實寫入者。它受判準 1／2 管（必須經由
#: producer、不得列字面鍵），另外受**判準 5** 管：它絕不可以寫 `state`。
_RECONCILE_IMPLEMENTATION = (POLL_MODEL_FILE, "_cron_reconcile_release_facts")
#: 判準 1／2 的適用範圍＝所有 release 事實寫入者（兩份 poll ＋ 對帳 cron）。
#: #627 的教訓就是「N 份實作各寫一次」，所以新增寫入者必須加進這裡。
_RELEASE_FACT_WRITERS = _POLL_IMPLEMENTATIONS + (_RECONCILE_IMPLEMENTATION,)
#: 這些欄位只能由 `_release_facts` 產出，不得在任何寫入者內逐條列出。
_RELEASE_FACT_KEYS = ("helm_revision", "helm_chart_version",
                      "last_release_sync_at")
_RELEASE_FACTS_PRODUCER = "_release_facts"


def check_poll_release_facts_single_source(root):
    """release 事實的寫入必須經由單一 producer，且逐分支帶到（#627）。

    #627 的形態：poll 有**兩份實作** —— `PaasController._update_service_status`
    （租戶打端點時走）與 `CloudService._poll_and_update_status`（**cron** 走）。
    兩份各自組自己的 write vals，於是 controller 那份寫了 `helm_chart_version`、
    cron 那份只寫 `helm_revision`。而唯一斷言「`release_facts` 必須到得了每個 write
    分支」的測試**整份只驅動 controller** ⇒ cron 那份整份沒回寫也全綠，#552 層① 的
    第五條回寫路徑在無人值守時失效，stg 實地漂移 3 天無告警。

    ## 五條判準

    1. **每個寫入者都要呼叫 `_release_facts`**（兩份 poll ＋ #627 (i) 的對帳 cron）。
       沒呼叫＝自己組 vals，收斂被繞過。
    2. **任何寫入者內都不得出現 release 事實欄位當 dict 字面鍵**。出現＝逐路徑列欄位
       的寫法回來了，正是 #627 的成因。
    3. **兩份 poll 內每一個設定 `'state'` 的 `.write({...})` 都必須帶
       `**release_facts` 展開。** ⚠ 這條是 v2 補的：v1 只驗判準 1／2，而**刪掉單一
       分支的展開時兩者都不會紅**（`calls_producer` 仍為 True、也沒新增字面鍵）——
       實測確認過。v1 的 docstring 卻宣稱「守單一 producer 沒被繞過」，那句話在分支層
       是假的。**宣告範圍大於守備範圍就是在製造假的安心感。**
    4. **每份 poll 至少要有一個「設 `'state'` 的 write」**。判準 3 對「一條 write 都
       沒有」是**恆真**的 ⇒ 整段被重構掉時 gate 會靜默變成永遠綠。刻意用**下限 1**
       而不是釘死當下的 4：釘死等於把「現在有 4 條分支」這個當下事實寫成契約，合理的
       重構（合併分支）會誤殺。守的是「gate 還有目標」，不是「分支數不許變」。
    5. **對帳 cron 不得寫 `state`。** 那條 cron 的核心安全性質就是「只對帳、不推狀態
       機」——對 running 服務跑狀態轉移，叢集一時不健康就會把租戶服務打成 `error`，
       而 error 沒有任何路徑會自己 poll 回來。這條把那個性質變成機械強制。

    ## 例外機制：**刻意不提供**

    今天的誤報數是 0（實測：兩份 poll 各 4 條 write 全部設 `'state'` 且全部已帶展開；
    404／`deleting` 分支是 `unlink()` 不是 `write()`，天生不在判準 3 範圍內）。

    日後若真的出現正當例外，本 gate **不提供程式碼端豁免**（沒有 `# noqa` 之類的
    出口）——作者必須來改這個函式並寫下理由，那個 diff 帶著具名理由、review 看得到。
    ⇒ 「白名單＋具名理由」的載體就是本原始碼本身。刻意不預先建一份空白名單：那等於
    在沒有任何使用者的情況下先造好一個逃生門，而**空的**白名單失效時更沒人會發現。

    ## 覆蓋邊界（寫明，不重蹈 v1 的過度宣稱）

    - 只看**列名函式本體內**的 `.write()`。若有人把 write 搬進被它們呼叫的 helper，
      AST 走不到 ⇒ 看不見。這是已知邊界。
    - 只守**結構**，不驗證 `_release_facts` 回傳的內容對不對、也不驗證各實作跑出來的
      值相同——那是行為層的事，由
      `src/tests/test_poll_mirror_release_facts_627.py` 的
      `TestBothPollImplementationsWriteTheSameFields` 實跑比對。
      形態層擋「結構已退化但還沒漂」，行為層擋「改個寫法繞過形態判準」。

    🔴 fail-loud：檔案讀不到、parse 不了、找不到目標函式，一律當違規。靜默
    `return []` 會讓這條 gate 在函式改名的當下永久沉默且看起來永遠是綠的。
    """
    violations = []
    producer_seen = False
    trees = {}

    for rel in dict.fromkeys(f for f, _ in _RELEASE_FACT_WRITERS):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                trees[rel] = ast.parse(f.read())
        except (OSError, SyntaxError, ValueError) as exc:
            violations.append(
                "%s：讀不到或 parse 失敗（%s）——release 事實單一 producer 的守線整條"
                "失去鑑別力。解析失敗當違規而不是無事，否則本 gate 會靜默恆綠（#627）"
                % (rel, exc))

    def _find(rel, func_name):
        tree = trees.get(rel)
        if tree is None:
            return None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                return node
        return None

    if _find(POLL_MODEL_FILE, _RELEASE_FACTS_PRODUCER) is not None:
        producer_seen = True

    def _state_writes(func):
        """該函式內所有「設 `'state'` 的 `.write({...})`」。"""
        found = []
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write"
                    and node.args
                    and isinstance(node.args[0], ast.Dict)):
                continue
            keys = [k.value for k in node.args[0].keys
                    if isinstance(k, ast.Constant)]
            if "state" in keys:
                found.append(node.args[0])
        return found

    # 判準 1／2：所有寫入者
    for rel, func_name in _RELEASE_FACT_WRITERS:
        target = _find(rel, func_name)
        if target is None:
            violations.append(
                "%s：找不到 release 事實寫入者 `%s`。函式改名或被移走時本 gate 會失去"
                "目標，而失去目標與「沒有違規」的輸出一模一樣 ⇒ 當違規處理。改名請一併"
                "更新 `_RELEASE_FACT_WRITERS`（#627）" % (rel, func_name))
            continue

        if not any(isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr == _RELEASE_FACTS_PRODUCER
                   for n in ast.walk(target)):
            violations.append(
                "%s::%s 沒有呼叫 `%s`——release 事實必須來自單一 producer。自己組 write"
                " vals 就是 #627 的成因：N 份實作各漏各的，而只驅動其中一份的測試全綠"
                % (rel, func_name, _RELEASE_FACTS_PRODUCER))

        for node in ast.walk(target):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if (isinstance(key, ast.Constant)
                        and key.value in _RELEASE_FACT_KEYS):
                    violations.append(
                        "%s::%s 第 %d 行把 `%s` 當 dict 字面鍵寫死——release 事實欄位"
                        "只能經由 `%s` 取得。逐路徑列欄位正是 #627 的成因（今天漏一個"
                        "欄位、明天加第三個欄位時再漏一次）"
                        % (rel, func_name, getattr(key, "lineno", -1),
                           key.value, _RELEASE_FACTS_PRODUCER))

    # 判準 3／4：兩份 poll 的每一條 state-write 都要帶展開，且至少要有一條
    for rel, func_name in _POLL_IMPLEMENTATIONS:
        target = _find(rel, func_name)
        if target is None:
            continue  # 判準 1 已經點名過
        writes = _state_writes(target)
        if not writes:
            violations.append(
                "%s::%s 內找不到任何「設 `'state'` 的 write」——判準 3 對零條 write 是"
                "**恆真**的，這代表本 gate 對該函式已失去鑑別力。整段被重構掉時要當違規"
                "而不是靜默通過（#627 判準 4）" % (rel, func_name))
            continue
        for dict_node in writes:
            unpacked = [ast.unparse(v) for k, v in
                        zip(dict_node.keys, dict_node.values) if k is None]
            if "release_facts" not in unpacked:
                violations.append(
                    "%s::%s 第 %d 行的 write 設了 `'state'` 卻沒有帶 `**release_facts`"
                    "——每一條狀態轉移都必須同時把 operator 回報的 release 事實落庫，"
                    "否則那條分支就是下一個 #627（v1 的判準只看函式層，刪掉單一分支的"
                    "展開不會紅，實測確認過）"
                    % (rel, func_name, getattr(dict_node, "lineno", -1)))

    # 判準 5：對帳 cron 不得推狀態機
    rel, func_name = _RECONCILE_IMPLEMENTATION
    target = _find(rel, func_name)
    if target is not None:
        for dict_node in _state_writes(target):
            violations.append(
                "%s::%s 第 %d 行寫了 `'state'`——對帳 cron 的核心安全性質是**只對帳、"
                "不推狀態機**。對 running 服務做狀態轉移，叢集一時不健康就會把租戶服務"
                "打成 `error`，而 error 沒有任何路徑會自己 poll 回來（#627 (i)）"
                % (rel, func_name, getattr(dict_node, "lineno", -1)))

    if not producer_seen:
        violations.append(
            "%s 沒有定義 `%s`——release 事實的欄位集合失去單一來源，#627 的收斂被還原了"
            % (POLL_MODEL_FILE, _RELEASE_FACTS_PRODUCER))

    return violations


def check_pattern_doc_registry(root):
    """pattern doc 的三方登記表必須互為雙射，且 map 指到的路徑必須是存在的檔案。

    #547 的根因：`docs/decisions/patterns/` 有 6 份文件教的做法與現行程式碼相反、
    16 份行號過期，而**沒有任何機制**會在任何時候提醒任何人回頭看 doc。這條 check
    補的是那一環的**地基**——先讓「同一份名單有三個登記處而且現在就對不上」變成紅的。

    三方（實測 develop c723a44d：目錄 32 / map 30 / README 索引 31，三個數字互不相等）：

    1. `docs/decisions/patterns/*.md` 的目錄實況（索引檔 README.md 自己不算）
    2. `.claude/hooks/pattern-guard-map.json` 的 `docs` 條目（＋刻意豁免清單）
    3. `docs/decisions/patterns/README.md` 的「何時必看」表與「完整檔案清單」，
       外加頂部「完整 N 條」的計數宣告

    再加一條 dangling-ref：**map 的每個 docs 條目都必須是存在的檔案路徑**。
    `pattern-guard.py` 全檔只 `json.load` 這份 map，對 docs 條目沒有任何
    `os.path.exists` ⇒ 它會把根本不存在的路徑當必讀件印給模型（實例：本 check
    上線前 map 裡有一筆 `CLAUDE.md「Seeded…」條`，那不是路徑）。

    ⚠ **覆蓋邊界**（必須寫明，否則下一個人會把「這條綠」讀成「這些 doc 是對的」，
    那正是 #547 的原型錯誤換一個載體重演）：

    - 本 check 只保證**三方名單互為雙射、且 map 指到的檔案存在**。
    - 它**不驗證** doc 的內容與程式碼是否相符——contradicts-code 與行號過期一份都
      抓不到。
    - 它**不涵蓋**否定式散文主張（例 `cloud-service-unlink-override-cleanup.md`
      寫「不是 M2M」而 `cloud_service.py` 是 `fields.Many2many`），也**不涵蓋**同名
      符號的語意改變（`sa_internal_identity_write` 從 `True` 變成模組層 sentinel
      物件——符號還在、語意全變）。這兩類**留給人**，本 gate 不假裝涵蓋。
    - `docs/tracking/pitfalls/` 只被 dangling-ref 涵蓋（map 有指到才驗存在），
      **沒有**雙射——那個目錄的索引另有慣例，不在本 check 範圍。
    """
    violations = []
    entries, map_errors = _pattern_guard_map_entries(root)
    violations.extend(map_errors)
    if map_errors and not entries:
        return violations  # map 整份壞掉，往下比對只會產生誤導的 violation 海

    # (1) dangling-ref：全部 docs 條目（不限 patterns/），路徑部分必須是存在的檔案。
    for cls_name, path, raw in entries:
        if not os.path.isfile(os.path.join(root, path)):
            violations.append(
                "%s：class `%s` 的必讀件 %r 不是存在的檔案路徑。pattern-guard.py 不驗證"
                "它印出來的路徑，會把讀不到的東西當必讀件餵給模型；登記格式是"
                "`路徑 — 一句何時必看`（em-dash 前必須是 repo 相對路徑）（見 %s）"
                % (PATTERN_GUARD_MAP, cls_name, path, PATTERN_REGISTRY_DOC))

    actual = _pattern_doc_files(root)
    mapped = {p for _, p, _ in entries
              if p.startswith(PATTERN_DOC_DIR + "/") and p.endswith(".md")
              and os.path.basename(p) != "README.md"}
    exempt = set(PATTERN_DOC_MAP_EXEMPT)

    # (2) 目錄 ↔ map（＋刻意豁免）。集合等值，不是「迭代登記表」。
    for rel in sorted(mapped & exempt):
        violations.append(
            "%s：同時出現在 %s 與 `PATTERN_DOC_MAP_EXEMPT`（刻意不納管清單）。"
            "兩者互斥——已經有路徑可綁就不該掛在豁免清單上，請從豁免清單移除（見 %s）"
            % (rel, PATTERN_GUARD_MAP, PATTERN_REGISTRY_DOC))
    for rel in sorted(exempt - actual):
        violations.append(
            "%s：列在 `PATTERN_DOC_MAP_EXEMPT`（刻意不納管清單）卻不在 `%s/` 裡。"
            "豁免比它豁免的東西活得久＝下一份同名 doc 會無聲繼承這個豁免，"
            "請刪掉這筆（見 %s）"
            % (rel, PATTERN_DOC_DIR, PATTERN_REGISTRY_DOC))
    # 反向（登記表有、目錄沒有）在這一腿關掉：map 那側由上面 (1) 的 dangling-ref
    # 逐筆點名 class（訊息更精確），豁免清單那側由緊接在上面的兩條專屬訊息負責。
    # 開著會讓同一個根因被報成兩筆——INJ-4 實測。
    _registry_diff(
        violations, actual, mapped | exempt, PATTERN_GUARD_MAP,
        "有觸發路徑的 pattern 要在對應 class 的 `docs` append 一行"
        "（`路徑 — 一句何時必看`）；",
        extra="純判斷類（無路徑可綁）則加進 `scripts/audit/repo-lint.py` 的 "
              "`PATTERN_DOC_MAP_EXEMPT` 並在 PR 寫下理由，**不要**為了讓 gate 變綠"
              "硬塞進 map。",
        reverse=False)

    toc, topic, counts, index_errors = _pattern_index_registers(root)
    violations.extend(index_errors)
    if index_errors:
        return violations

    # (3) 目錄 ↔ README 的兩個登記面。
    _registry_diff(violations, actual, toc, "%s 的「完整檔案清單」" % PATTERN_DOC_INDEX,
                   "補一行 `- [slug](slug.md)`；")
    _registry_diff(violations, actual, topic, "%s 的「何時必看」表" % PATTERN_DOC_INDEX,
                   "在對應主題表補一列 `| [[slug]] | 何時觸發 / 必看 |`；")

    # (4) 頂部的計數宣告。它是人讀索引時唯一的「是不是完整」訊號，漂掉就沒人會發現
    #     少了一份。零命中同樣算紅——那句宣告被刪掉＝這個訊號消失。
    if not counts:
        violations.append(
            "%s：頂部找不到「完整 N 條」的計數宣告。那是讀索引的人唯一能自證清單完整"
            "的地方，刪掉它等於把「少了一份」變回無聲（見 %s）"
            % (PATTERN_DOC_INDEX, PATTERN_REGISTRY_DOC))
    for n in counts:
        if n != len(actual):
            violations.append(
                "%s：頂部宣告「完整 %d 條」，但 `%s/` 實際有 %d 份 pattern doc。"
                "新增／刪除 doc 時要一起更新這個數字（見 %s）"
                % (PATTERN_DOC_INDEX, n, PATTERN_DOC_DIR, len(actual),
                   PATTERN_REGISTRY_DOC))
    return violations


# ---------------------------------------------------------------- modes

AVAILABILITY_JUDGE_FILE = "src/models/cloud_app_template.py"
AVAILABILITY_JUDGE_FUNC = "_reject_reason_for_new_instance"
AVAILABILITY_LABEL_FILE = "src/static/src/paas/utils/availability_label.js"
AVAILABILITY_LABEL_MAP_RE = re.compile(
    r"const\s+REASON_LABELS\s*=\s*\{(.*?)\}", re.S)
AVAILABILITY_LABEL_KEY_RE = re.compile(r"^\s*(\w+)\s*:", re.M)
# #596：這條不變式跨後端判準與前端文案表兩個檔，hook 模式要靠這個集合前置判斷
# 「有沒有動到它」（同 RESET_LADDER_FILES 的形狀）。
AVAILABILITY_REASON_FILES = (AVAILABILITY_JUDGE_FILE, AVAILABILITY_LABEL_FILE)


def check_availability_reason_labels(root):
    """後端能回的每個 availability reason，前端都要有明確文案。

    #552 層 4 的實地教訓：``availabilityLabel`` 原本是
    ``reason === "paused" ? "暫停販售" : "已售罄"`` —— 一個三元式，**任何**新增的
    reason 都會掉進 else 被渲染成「已售罄」。新增 ``unpinned_chart``（平台端沒釘
    chart 版本）時，租戶看到的會是「已售罄」：一句**假話**，而且會讓營運去查根本
    不存在的名額問題。

    這類漂移沒有任何測試會變紅（後端測後端判準、本 repo 沒有前端測試基礎設施），
    與 check_gateway_copy_gate_anchor 是同一個家族——兩邊各自都「看起來對」。

    判準只要求**涵蓋**（後端 ⊆ 前端），不要求相等：前端多留一個舊 slug 的文案是
    無害的（後端不再回它而已），少一個才是說謊。
    """
    judge_path = os.path.join(root, AVAILABILITY_JUDGE_FILE)
    label_path = os.path.join(root, AVAILABILITY_LABEL_FILE)
    if not (os.path.exists(judge_path) and os.path.exists(label_path)):
        return []  # fail-open：檔案搬家由其他 gate／測試負責，這裡不重複報

    with open(judge_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    judge = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == AVAILABILITY_JUDGE_FUNC),
        None)
    if judge is None:
        return ["%s：找不到 %s，availability reason 涵蓋檢查失效（探針壞掉不是通過）"
                % (AVAILABILITY_JUDGE_FILE, AVAILABILITY_JUDGE_FUNC)]

    # 回傳字串常數 ＝ 一個 reason slug；`return None`／`return` 是放行，不算。
    backend = {
        n.value.value for n in ast.walk(judge)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    }
    if not backend:
        return ["%s：%s 一個 reason 字串都抽不到——探針失去鑑別力（後端 ⊆ 前端 "
                "在空集合上恆真）" % (AVAILABILITY_JUDGE_FILE, AVAILABILITY_JUDGE_FUNC)]

    with open(label_path, encoding="utf-8") as f:
        js = f.read()
    map_match = AVAILABILITY_LABEL_MAP_RE.search(js)
    if not map_match:
        return ["%s：找不到 REASON_LABELS 對照表。若改回三元式，任何新 reason 都會被"
                "靜默渲染成別的狀態（#552）" % AVAILABILITY_LABEL_FILE]
    frontend = set(AVAILABILITY_LABEL_KEY_RE.findall(map_match.group(1)))

    missing = sorted(backend - frontend)
    if missing:
        return ["%s：後端 %s 會回 %s，但 REASON_LABELS 沒有對應文案 ⇒ 前端會顯示"
                "fallback 而不是這個狀態的真實描述（#552）"
                % (AVAILABILITY_LABEL_FILE, AVAILABILITY_JUDGE_FUNC, missing)]
    return []


# ------------------------------ 一次性憑證：後端交付 ⊆ 前端渲染（#672）

ONE_TIME_CRED_BACKEND_FILE = "src/controllers/paas.py"
ONE_TIME_CRED_BACKEND_FUNC = "_gateway_credentials"
ONE_TIME_CRED_MODAL_FILE = (
    "src/static/src/paas/components/modal/OneTimeCredentialsModal.js")
ONE_TIME_CRED_BUILDER = "buildOneTimeCredentials"
# builder 的 body：從 `export function buildOneTimeCredentials(` 到第一個行首 `}`。
ONE_TIME_CRED_BUILDER_RE = re.compile(
    r"export function buildOneTimeCredentials\s*\([^)]*\)\s*\{(.*?)\n\}", re.S)
# 前端讀後端 payload 的唯一形態：`data.<key>`（`data && data.foo` 也命中）。
ONE_TIME_CRED_DATA_KEY_RE = re.compile(r"\bdata\.([A-Za-z_][A-Za-z0-9_]*)")
# 跨檔不變式，hook 模式靠這個集合前置判斷「有沒有動到它」（同 AVAILABILITY_REASON_FILES）。
ONE_TIME_CRED_FILES = (ONE_TIME_CRED_BACKEND_FILE, ONE_TIME_CRED_MODAL_FILE)


def _one_time_cred_backend_keys(func):
    """從 `_gateway_credentials` 的 AST 抽出它會交付的 payload key。

    兩種形態都收：`creds['x'] = ...`（現況）與 `return {'x': ..., 'y': ...}`
    （#672 之前的形狀）。只認常數字串 key——動態 key 抽不出來也不該靜默略過，
    但那形態今天不存在，真的出現時 backend 集合會少一個名字，由呼叫端的
    「抽不到任何 key ⇒ 違規」與這裡的涵蓋判準一起收斂到「看得見的紅」。
    """
    keys = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)):
                    keys.add(target.slice.value)
        elif isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


def check_one_time_credential_render(root):
    """後端交付的每一項一次性憑證，前端都要渲染得出來。

    #672 的另一半。那次的缺口是**後端**根本沒交付 dashboard URL，租戶手上 MCP 的
    Dashboard 區三格缺一格 ⇒ 管理面整組失效；後端測試補得上。但同一條邊界還有反向
    的一段沒有任何守線：後端交付了、前端的 `buildOneTimeCredentials` 忘了讀，那一格
    就永遠不顯示——而**一次性**憑證錯過就沒有第二次（平台刻意不做回讀端點，租戶只能
    跑 Reset 換發，而 Reset 會連帶換掉 gateway key，等於逼他重接一次線）。

    這類漂移沒有任何測試會變紅：後端測後端 payload（本 repo 的測試就是這樣寫的），
    而前端沒有測試基礎設施。與 `check_availability_reason_labels`／
    `check_gateway_copy_gate_anchor` 同一個家族——兩邊各自都「看起來對」。

    判準只要求**涵蓋**（後端 ⊆ 前端）不要求相等：前端多留一個後端不再送的 key，
    效果只是那段 `if` 永遠不成立（無害）；少一個才是租戶拿不到東西。

    ⚠ 覆蓋邊界：只驗 key 有沒有被讀到，**不驗文案**。hint 指錯區、根本沒指路（#672
    的直接成因就是 Admin Password 沒說要填進 MCP）本 gate 一個都抓不到。把這條綠讀成
    「憑證視窗說得清楚」是錯的。
    """
    backend_path = os.path.join(root, ONE_TIME_CRED_BACKEND_FILE)
    modal_path = os.path.join(root, ONE_TIME_CRED_MODAL_FILE)
    if not (os.path.exists(backend_path) and os.path.exists(modal_path)):
        return []  # fail-open：檔案搬家由其他 gate／測試負責，這裡不重複報

    try:
        with open(backend_path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError) as exc:
        return ["%s：讀不到或 parse 失敗（%s）——一次性憑證的涵蓋檢查失效"
                % (ONE_TIME_CRED_BACKEND_FILE, exc)]
    func = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == ONE_TIME_CRED_BACKEND_FUNC),
        None)
    if func is None:
        return ["%s：找不到 %s，一次性憑證的涵蓋檢查失效（探針壞掉不是通過）。改名的話"
                "本 check 的常數要跟著改" % (ONE_TIME_CRED_BACKEND_FILE,
                                            ONE_TIME_CRED_BACKEND_FUNC)]
    backend = _one_time_cred_backend_keys(func)
    if not backend:
        return ["%s：%s 一個 payload key 都抽不到——探針失去鑑別力（後端 ⊆ 前端 在空"
                "集合上恆真）" % (ONE_TIME_CRED_BACKEND_FILE, ONE_TIME_CRED_BACKEND_FUNC)]

    with open(modal_path, encoding="utf-8") as f:
        js = f.read()
    body = ONE_TIME_CRED_BUILDER_RE.search(js)
    if not body:
        return ["%s：找不到 %s 的函式本體。它是兩個入口（建立／Reset）共用的唯一組裝點，"
                "拆掉的話「哪些憑證會被顯示」就散回各頁各寫一份（#530）"
                % (ONE_TIME_CRED_MODAL_FILE, ONE_TIME_CRED_BUILDER)]
    frontend = set(ONE_TIME_CRED_DATA_KEY_RE.findall(body.group(1)))

    missing = sorted(backend - frontend)
    if missing:
        return ["%s：後端 %s 會交付 %s，但 %s 沒有讀 data.<key> ⇒ 那一格永遠不顯示，"
                "而一次性憑證錯過就沒有第二次（#672）"
                % (ONE_TIME_CRED_MODAL_FILE, ONE_TIME_CRED_BACKEND_FUNC, missing,
                   ONE_TIME_CRED_BUILDER)]
    return []


# --------------------------------------- kubectl namespace 白名單雙寫對帳（2026-08-06）

KUBECTL_NS_CLAUDE_MD = "CLAUDE.md"
KUBECTL_NS_GUARD_DOC = "docs/guides/deployment/shared-cluster-paas-scope-guard.md"
# 第三份副本（2026-08-16）：Bash 層 PreToolUse hook 的 fail-closed 白名單常數。
# hook 要在「不讀別的檔」的前提下毫秒級判定 ⇒ 清單必須內嵌 ⇒ 又一份必要重複，
# 同樣交給本 gate 機械化同步，不靠人記得三改。
KUBECTL_NS_HOOK_FILE = ".claude/hooks/kubectl-ns-guard.py"
KUBECTL_NS_DOC_REF = "見 %s「適用範圍」段" % KUBECTL_NS_GUARD_DOC
# CLAUDE.md 側：`### ` 標題錨在「只能操作以下 namespace」，清單是它到下一個標題之間的表格。
KUBECTL_NS_HEADING_RE = re.compile(r"^#{2,4} .*只能操作以下 namespace.*$", re.M)
KUBECTL_NS_NEXT_HEADING_RE = re.compile(r"^#{1,6} ", re.M)
# 表格列的第一格必須以 backtick 名字開頭；`| Namespace | 用途 |` 與 `| --- | --- |` 天然被濾掉。
KUBECTL_NS_ROW_RE = re.compile(r"^\|\s*`([^`]+)`", re.M)
# guard doc 側：單行散文，項目以全形直線分隔，每項第一個 backtick 才是 namespace
#（`fleet-local`（**僅限 `paas-*` 前綴資源**）這種帶限定語的項目會有第二個 backtick）。
KUBECTL_NS_GUARD_LINE_RE = re.compile(r"^kubectl 在 woow-k3s context.*只能操作.*$", re.M)
KUBECTL_NS_TOKEN_RE = re.compile(r"`([^`]+)`")
# hook 側：ALLOWED_NAMESPACES tuple 字面量，抽引號字串。
KUBECTL_NS_HOOK_TUPLE_RE = re.compile(r"ALLOWED_NAMESPACES\s*=\s*\(([^)]*)\)", re.S)
KUBECTL_NS_HOOK_ITEM_RE = re.compile(r"\"([^\"]+)\"|'([^']+)'")


def _kubectl_ns_from_claude_md(text):
    """回傳 (namespace 集合, 錯誤訊息 or None)。抽不到一律當錯誤，不回空集合。"""
    heading = KUBECTL_NS_HEADING_RE.search(text)
    if not heading:
        return set(), ("%s：找不到「只能操作以下 namespace」那個標題 ⇒ 白名單對帳失去"
                       "鑑別力。標題改寫時要一起更新 KUBECTL_NS_HEADING_RE（%s）"
                       % (KUBECTL_NS_CLAUDE_MD, KUBECTL_NS_DOC_REF))
    rest = text[heading.end():]
    nxt = KUBECTL_NS_NEXT_HEADING_RE.search(rest)
    section = rest[:nxt.start()] if nxt else rest
    names = {m.group(1) for m in KUBECTL_NS_ROW_RE.finditer(section)}
    if not names:
        return set(), ("%s：該段落抽不到任何表格列 ⇒ 兩份清單的比對會在空集合上恆真"
                       "（探針壞掉不是通過）（%s）" % (KUBECTL_NS_CLAUDE_MD, KUBECTL_NS_DOC_REF))
    return names, None


def _kubectl_ns_from_guard_doc(text):
    """回傳 (namespace 集合, 錯誤訊息 or None)。抽不到一律當錯誤，不回空集合。"""
    line = KUBECTL_NS_GUARD_LINE_RE.search(text)
    if not line:
        return set(), ("%s：找不到「kubectl 在 woow-k3s context…只能操作…」那一行 ⇒ 白名單"
                       "對帳失去鑑別力（%s）" % (KUBECTL_NS_GUARD_DOC, KUBECTL_NS_DOC_REF))
    body = line.group(0).split("：", 1)[-1]
    names = set()
    for seg in body.split("｜"):
        token = KUBECTL_NS_TOKEN_RE.search(seg)
        if token:
            names.add(token.group(1))
    if not names:
        return set(), ("%s：那一行抽不到任何 namespace ⇒ 比對會在空集合上恆真"
                       "（探針壞掉不是通過）（%s）" % (KUBECTL_NS_GUARD_DOC, KUBECTL_NS_DOC_REF))
    return names, None


def _kubectl_ns_from_hook(text):
    """回傳 (namespace 集合, 錯誤訊息 or None)。抽不到一律當錯誤，不回空集合。"""
    m = KUBECTL_NS_HOOK_TUPLE_RE.search(text)
    if not m:
        return set(), ("%s：找不到 ALLOWED_NAMESPACES tuple ⇒ 白名單對帳失去鑑別力。"
                       "常數改名時要一起更新 KUBECTL_NS_HOOK_TUPLE_RE（%s）"
                       % (KUBECTL_NS_HOOK_FILE, KUBECTL_NS_DOC_REF))
    names = {a or b for a, b in KUBECTL_NS_HOOK_ITEM_RE.findall(m.group(1))}
    if not names:
        return set(), ("%s：tuple 抽不到任何 namespace ⇒ 比對會在空集合上恆真"
                       "（探針壞掉不是通過）（%s）"
                       % (KUBECTL_NS_HOOK_FILE, KUBECTL_NS_DOC_REF))
    return names, None


def check_kubectl_ns_whitelist_sync(root):
    """kubectl 可操作 namespace 的白名單寫在三個檔案裡，三份必須逐字相等。

    2026-08-06 實地教訓：平台自己的 Fleet 注入 secret 全住在 ``fleet-local``，而
    ``paas-app-bundle-cd`` 的 CLAUDE.md／README 早就把 ``kubectl get secret -n
    fleet-local``、``kubectl patch bundle -n fleet-local`` 寫成標準平台程序——但本
    repo 的白名單**沒有列入它**。於是兩份文件對同一個 namespace 給出相反指引，而讀
    者會照先讀到的那份做。

    🔴 這條守的不是「清單內容對不對」（那是人的判斷），是**兩份副本不許分歧**。
    白名單型守線漏列一個實際天天在用的項目，效果不是更安全，而是守線被繞過（照 CD
    repo 那份做）或讓人卡住（照本 repo 這份做）——兩個方向都不會有任何訊號。

    為什麼不去重成一份：三份服務不同讀者。CLAUDE.md 每個 session 自動載入，agent
    不讀額外文件就要拿得到清單；guard doc 是 pattern-guard hook 推給人讀的敘事版，
    把清單抽走等於被強制讀的那份不再直接寫出規則；kubectl-ns-guard hook（2026-08-16
    起的第三份）要在不讀別的檔的前提下毫秒級判定，清單必須內嵌成常數。**必要重複
    就該機械化同步**，而不是靠人記得改一份要同步其餘——這正是本 gate 存在的理由。

    ⚠ 覆蓋邊界：只比對三份**副本**的 namespace 集合是否相等。清單漏了某個實際在用
    的 namespace（＝#618 修的那個問題本體）本 gate 一個都抓不到——那需要對照叢集實
    況，屬於人的工作。把這條綠讀成「白名單是完整的」就是原型錯誤換一個載體。
    """
    sources = (
        (KUBECTL_NS_CLAUDE_MD, _kubectl_ns_from_claude_md),
        (KUBECTL_NS_GUARD_DOC, _kubectl_ns_from_guard_doc),
        (KUBECTL_NS_HOOK_FILE, _kubectl_ns_from_hook),
    )
    missing = [rel for rel, _ in sources
               if not os.path.exists(os.path.join(root, rel))]
    if missing:
        # 🔴 刻意不 fail-open：這三個檔是守線本體，任一不見就是清單少一份副本
        #    ——而「少一份」正好是本 gate 無法察覺該份分歧的狀態。
        return ["%s：檔案不存在 ⇒ kubectl namespace 白名單少了一份副本，對帳失去"
                "鑑別力（%s）" % ("／".join(missing), KUBECTL_NS_DOC_REF)]

    copies, errors = {}, []
    for rel, extract in sources:
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            names, err = extract(f.read())
        copies[rel] = names
        if err:
            errors.append(err)
    if errors:
        return errors

    # 點名「是哪一個、在哪幾份」而不是「差幾個」：只報數量等於報了一個沒法行動
    # 的訊號。訊息同時列出「有的」與「缺的」兩邊，讀者不用自己反推。
    order = [rel for rel, _ in sources]
    parts = []
    for name in sorted(set().union(*copies.values())):
        have = [rel for rel in order if name in copies[rel]]
        if len(have) == len(order):
            continue
        lack = [rel for rel in order if name not in copies[rel]]
        parts.append("`%s` 只在 %s 有、缺於 %s"
                     % (name, "／".join(have), "／".join(lack)))
    if not parts:
        return []
    return ["kubectl namespace 白名單三份副本分歧（%s）⇒ 副本會對同一個 namespace "
            "給出相反指引，而讀者照先讀到的那份做。改一份必須同步三份（%s）"
            % ("；".join(parts), KUBECTL_NS_DOC_REF)]


# ------------------------------------- doc 的 code 符號錨點還在不在（#626／#616）

# scope 刻意只收 pattern-guard hook 的必讀件。這不是保守，是**實測**（01cca0f2 上量）：
# 全 docs/ 有 2473 處帶行號的 code 錨點（散在 225 份 doc）⇒ gate 絕不能要求「不得有
# 行號錨點」，那會一次紅 2473 處；docs/plans/ 的 27 組 path:symbol 更有 13 組符號已經
# 不存在——那是**時點文件**，描述當時的設計，符號後來改名是正常的。限縮到必讀件時
# 「符號找不到」＝0，今天可以綠上線。本 gate 只驗**已經用符號名的那些**＝「守住已經
# 修好的」而非「強制全部修好」，存量行號錨點漸進替換（同 seed-topology 那種 floor）。
DOC_SYMBOL_ANCHOR_DIRS = ("docs/decisions/patterns", "docs/tracking/pitfalls")
# 只認 .py／.js：這兩種本 repo 有可靠的符號抽法（AST／去噪後的識別字集合）。.xml 的
# record id、.yml 的 key 不匹配錨點形態 ⇒ **不檢查**，也不假裝檢查過（要納入請先想好
# 那個副檔名的符號抽法，不要放寬 regex 讓它掉進「抽不到就算過」）。
DOC_SYMBOL_ANCHOR_EXTS = ("py", "js")
DOC_SYMBOL_ANCHOR_REF = "見本檔檔頭第 20 條與 #626"

# 圍欄：行首（≤3 空白）連續 3 個以上的 ` 或 ~，收在同字元、不短於開頭的那一行。
_MD_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*$", re.M)
# 錨點形態：`<路徑><.py|.js>:<符號>`。左界 (?<![\w./-]) 擋掉把別的字尾當路徑開頭；
# 符號允許 `Class.method` 這種點鏈（每一段都要在該檔裡找得到，見下）。
# 冒號後**必須**是識別字起頭，所以 `foo.py:581`、`foo.py:43-61`、`foo.py:81 _x` 這些
# 帶行號的舊式指標天然不匹配——本 gate 不驗行號，也不逼人把行號拿掉。
_DOC_SYMBOL_ANCHOR_RE = re.compile(
    r"(?<![\w./-])([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:%s)):"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    % "|".join(DOC_SYMBOL_ANCHOR_EXTS))

# 候選索引要剪掉的目錄。虛擬環境／node_modules 裡有成千上萬個 .py，不剪掉會讓
# **每一個** bare basename 都變成「歧義」⇒ 一個純由掃描範圍製造的整片假陽性。
# `.claude/worktrees` 同理而且更陰險：在主 checkout 上跑 --all 時，每個 worktree
# 都是整個 repo 的副本，不剪掉就是「所有錨點同時歧義」。
_DOC_SYMBOL_ANCHOR_PRUNE_NAMES = frozenset((
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
    # `.maestri/` 只住 maestro plugin 的 role 與其下的 worktree checkout
    # （典型路徑 `.maestri/roles/<uuid>/.claude/worktrees/<branch>/`）。剪掉理由
    # 與上方 `.claude/worktrees` 相同：它是整個 repo 的副本，不剪會讓每一個 bare
    # basename（`paas.py`、`helm.py`、`ha_paas.py`...）都被掃出兩個候選，純由掃
    # 描範圍製造整片假陽性。`_DOC_SYMBOL_ANCHOR_PRUNE_RELS` 抓的是 top-level
    # `.claude/worktrees`，覆蓋不到這個嵌套變體。
    ".maestri",
))
_DOC_SYMBOL_ANCHOR_PRUNE_RELS = frozenset((".claude/worktrees",))
_JS_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _blank_md_fences(text):
    """把 markdown 圍欄區塊換成**等長空白**（換行保留，行號不位移）。

    🔴 這條排除規則不是體貼，是實測撞出來的：
    docs/tracking/pitfalls/two-layer-acl-asymmetric-deny.md 的 ```bash 區塊裡是
    `$ grep -rn …` 的**逐字輸出重現**，用來重建「AC #42 漏列 paas.py」這個事件。
    那些指標的用途是**舉證**不是導航，過期是正常的、甚至是必要的——改掉等於偽造
    史料。同一份 doc 的圍欄裡還有 5 處不帶目錄的 `ai_assistant.py:_check_project_access`
    （引述當時的 code 註解），沒有這條規則它們會各自被判成「basename 歧義」。

    未閉合的圍欄一律當「開到檔尾」——寧可少驗幾個錨點，也不要把後半份 doc 的程式碼
    範例當成散文來解析。
    """
    out = list(text)
    opened = None
    for m in _MD_FENCE_RE.finditer(text):
        token = m.group(1)
        if opened is None:
            opened = m
        elif token[0] == opened.group(1)[0] and len(token) >= len(opened.group(1)):
            _blank_range(out, text, opened.start(), m.end())
            opened = None
    if opened is not None:
        _blank_range(out, text, opened.start(), len(text))
    return "".join(out)


def _doc_symbol_anchors(text):
    """回傳 doc 裡的 [(行號, 路徑, 符號)]（圍欄內的已被排除）。

    刻意**不**要求錨點被 backtick 包起來。慣例上它們都是 inline code，但 backtick
    只是排版；把它當觸發條件的話，拿掉一對 backtick 就能讓 gate 靜默——那正是本
    工具整個檔在防的形態。實測過兩種寫法在今天的 scope 上結果**完全相同**（18 個
    錨點一字不差），所以選了不可被排版繞過的那個。
    """
    body = _blank_md_fences(text)
    return [(body.count("\n", 0, m.start()) + 1, m.group(1), m.group(2))
            for m in _DOC_SYMBOL_ANCHOR_RE.finditer(body)]


def _repo_code_files(root):
    """repo 內所有可被錨點指到的 code 檔（repo 相對路徑，正斜線）。"""
    files = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [
            d for d in dirnames
            if d not in _DOC_SYMBOL_ANCHOR_PRUNE_NAMES
            and ("%s/%s" % (rel_dir, d) if rel_dir else d)
            not in _DOC_SYMBOL_ANCHOR_PRUNE_RELS
        ]
        for name in filenames:
            if "." in name and name.rsplit(".", 1)[-1] in DOC_SYMBOL_ANCHOR_EXTS:
                files.add(("%s/%s" % (rel_dir, name)) if rel_dir else name)
    return files


def _resolve_anchor_file(path, files, top_dirs):
    """把錨點的路徑解析成唯一的 repo 相對路徑。

    回傳 `(rel, status, candidates)`，status ∈ ``exact`` / ``unique`` /
    ``ambiguous`` / ``dead`` / ``outside``。

    🔴 **嚴格解析**：解析不到唯一的檔就 fail，不挑第一個命中。理由是實測——repo 裡
    有兩個 `ai_assistant.py`（`src/models/` 沒有 `_check_project_access`、
    `src/controllers/` 有），#626 的可行性評估用 basename glob 取第一個命中時命中了
    `src/models/` 那個，於是報「符號找不到」：**一個純粹由解析策略製造的假陽性**。
    另一種選擇是「任一候選有該符號即通過」，但那是相反方向的說謊——`post-migrate.py`
    在本 repo 有 33 個同名檔，「33 個裡有一個有」對讀者的導航價值是零。嚴格解析的
    代價只有一次性的：把錨點補足目錄前綴（實測全 scope 只需補 2 處）。

    ``dead`` 與 ``outside`` 的分界：路徑帶目錄、且第一段是 repo 內**實際存在的頂層
    目錄**（`src/`、`extra/`…）就當它宣稱自己是 repo 相對路徑 ⇒ 找不到＝指標已死；
    否則當它是 repo 外的檔（Odoo 核心的 `binary.py`、`addons/base/models/ir_rule.py`、
    別的 repo 的 `paas-agent-runtime/…`）⇒ 跳過。沒有這個分界，`src/models/typo.py:x`
    這種打錯字的錨點會被「檔不存在就跳過」靜默吃掉。
    """
    norm = path.lstrip("./")
    if norm in files:
        return norm, "exact", [norm]
    candidates = sorted(r for r in files if r.endswith("/" + norm))
    if len(candidates) == 1:
        return candidates[0], "unique", candidates
    if candidates:
        return None, "ambiguous", candidates
    head = norm.split("/", 1)[0]
    if "/" in norm and head in top_dirs:
        return None, "dead", []
    return None, "outside", []


def _anchor_file_symbols(rel, abs_path):
    """該檔今天有哪些符號名。回 `(集合, 錯誤訊息 or None)`；抽不出來時集合是 None。

    Python 走 AST：def／class 名、任何 Store context 的 Name（module 常數、Odoo 的
    欄位宣告、class 屬性）、`self.x = …` 的屬性名、import 進來的名字。收 import 是
    刻意的——符號搬到別的 module 但仍 import 進這個檔時，doc 的指標**還是走得到**。

    JS 沒有 parser 可用（scripts/audit 的硬契約是不得有外部依賴），判準是「剝掉註解
    與字串字面值後，這個識別字還在程式碼裡出現」。比 Python 側寬：抓得到「改名／
    刪掉」（定義與呼叫點會一起消失），抓不到「定義搬走、只剩呼叫」。寬的方向是**不
    誤報**，符合本 gate「純存在性、無閾值」的定位。
    """
    try:
        with open(abs_path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return None, "讀不到這個檔（%s）" % exc
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            return None, "Python 語法錯誤，抽不出符號表（%s）" % exc
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                names.add(node.attr)
            elif isinstance(node, ast.alias):
                names.add((node.asname or node.name).split(".", 1)[0])
        return names, None
    return set(_JS_IDENT_RE.findall(_strip_js_noise(text))), None


def check_doc_symbol_anchor(root):
    """hook 必讀件裡的 `path:symbol` 錨點，符號今天必須還在（#626）。

    PR #625（#616／#623）把四份 doc 的絕對行號指標換成**符號名主鍵**，處方有了，
    但沒有任何機制守它：明天有人改了 code 卻沒改 doc，不會有任何東西變紅——「驗過
    一次 ≠ 會被持續驗證」。這條 check 就是那個機制。

    判準是**純存在性**（def X 在不在），沒有閾值所以不會誤報，也刻意不驗行號：
    #616 當初否決 gate 的理由「要容忍合理位移就得有閾值，鬆了恆綠、緊了天天誤報」
    只成立於「驗行號」那條路。

    ⚠ 只驗符號存在，**不驗 doc 的敘述與該符號的行為相符**（那要讀懂散文，不是靜態
    檢查能做的）。
    """
    # 這個 root 到底有沒有本 check 的輸入領域？宣告目錄的**頂層**（今天全是 `docs/`）
    # 一個都不存在時，這裡根本不是本 repo 的 checkout——hook 模式的 root 由
    # CLAUDE_PROJECT_DIR 決定，可以被指到任何地方（別的 repo、測試用的 tmp fixture、
    # sparse checkout）。對「不是我的樹」報違規只是噪音，而且因為本 check 對**任何**
    # .py／.js 編輯都會被分派到，那個噪音會變成「每次編輯都 block」。同
    # check_availability_reason_labels 的 fail-open 判準：輸入不在就不重複報。
    # ⚠ 分界刻意畫在頂層目錄、不是宣告目錄本身：`docs/` 還在而
    #   `docs/tracking/pitfalls/` 不見了，那正是要抓的漂移，下面的 floor 會紅。
    #   實證：這條分界是被 test_repo_lint_docstring_registry.py 的 hook fixture
    #   （root 只有 scripts/audit/repo-lint.py 一個檔）逼出來的，不是預想。
    if not any(os.path.isdir(os.path.join(root, rel_dir.split("/", 1)[0]))
               for rel_dir in DOC_SYMBOL_ANCHOR_DIRS):
        return []
    violations = []
    docs = []
    for rel_dir in DOC_SYMBOL_ANCHOR_DIRS:
        found = sorted(glob.glob(os.path.join(root, rel_dir, "**", "*.md"),
                                 recursive=True))
        if not found:
            # floor 折進迴圈：宣告了目錄卻一份 doc 都沒掃到，與「全部通過」的輸出
            # 一模一樣。目錄改名／搬家時要紅在這裡，不是靜默少跑一圈
            # （memory: feedback_declarative_list_gate_needs_floor_folded_in）。
            violations.append(
                "%s/：宣告在 scope 裡卻一份 .md 都沒掃到 ⇒ 這個目錄的錨點整批不會被"
                "檢查，而輸出與「全部通過」相同（%s）" % (rel_dir, DOC_SYMBOL_ANCHOR_REF))
        docs.extend(found)
    if not docs:
        return violations

    files = _repo_code_files(root)
    top_dirs = {n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))}
    symbol_cache = {}
    for doc_path in docs:
        rel_doc = os.path.relpath(doc_path, root).replace(os.sep, "/")
        try:
            with open(doc_path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            violations.append("%s：讀不到這份 doc（%s）⇒ 它的錨點整份沒被檢查（%s）"
                              % (rel_doc, exc, DOC_SYMBOL_ANCHOR_REF))
            continue
        for line, path, symbol in _doc_symbol_anchors(text):
            where = "%s:%d" % (rel_doc, line)
            shown = "`%s:%s`" % (path, symbol)
            target, status, candidates = _resolve_anchor_file(path, files, top_dirs)
            if status == "outside":
                continue  # repo 外的檔（Odoo 核心／別的 repo）：不是我們能保證的東西
            if status == "ambiguous":
                violations.append(
                    "%s：錨點 %s 的路徑 `%s` 在 repo 內有 %d 個候選（%s）⇒ 解析不到"
                    "唯一的檔。請把路徑補到足以唯一識別（例如 `src/controllers/%s`）。"
                    "挑第一個命中是不行的——那會製造純由解析策略產生的假陽性／假陰性"
                    "（%s）"
                    % (where, shown, path, len(candidates), "、".join(candidates),
                       path, DOC_SYMBOL_ANCHOR_REF))
                continue
            if status == "dead":
                violations.append(
                    "%s：錨點 %s 的路徑以 repo 內既有的頂層目錄 `%s` 開頭，卻找不到"
                    "這個檔（也沒有任何檔以它為路徑後綴）⇒ 指標已死。若它指的是 repo "
                    "外的檔，請不要寫成 repo 相對路徑的樣子（%s）"
                    % (where, shown, path.lstrip("./").split("/", 1)[0],
                       DOC_SYMBOL_ANCHOR_REF))
                continue
            if target not in symbol_cache:
                symbol_cache[target] = _anchor_file_symbols(
                    target, os.path.join(root, target))
            names, error = symbol_cache[target]
            if names is None:
                violations.append(
                    "%s：錨點 %s 指到的 %s %s ⇒ 判不了「符號還在不在」。探針壞掉不算"
                    "通過（%s）" % (where, shown, target, error, DOC_SYMBOL_ANCHOR_REF))
                continue
            missing = [seg for seg in symbol.split(".") if seg not in names]
            if missing:
                violations.append(
                    "%s：錨點 %s 的符號 %s 在 %s 裡已經不存在了 ⇒ doc 的 code 指標指向"
                    "死路。改 code 時要一起改 doc（或把指標換成今天真實存在的符號名）"
                    "——符號名主鍵的用途就是「不必跟著行號漂」，但前提是符號本身還在"
                    "（%s）"
                    % (where, shown, "／".join("`%s`" % s for s in missing), target,
                       DOC_SYMBOL_ANCHOR_REF))
    return violations


def _doc_symbol_anchor_watches(rel):
    """這個檔被編輯時要不要重跑 doc-symbol-anchor（hook 分派用）。

    兩類輸入都要接住：scope 內的 doc（錨點本體），以及**任何** .py／.js——改 code
    可能讓 doc 的符號錨點失效，而且新增一個同 basename 的檔會讓原本唯一解析的錨點
    變成歧義。刻意不寫成 `src/**`：被引用的符號也住在 extra/paas-operator/**。
    """
    if "." in rel and rel.rsplit(".", 1)[-1] in DOC_SYMBOL_ANCHOR_EXTS:
        return True
    return rel.endswith(".md") and any(
        rel.startswith(d + "/") for d in DOC_SYMBOL_ANCHOR_DIRS)


# --------------------------------------------- repo-lint 檔頭 ↔ 實作對帳（#598）

REPO_LINT_SELF_REL = "scripts/audit/repo-lint.py"
# 本條守的是**本檔自己**，沒有外部 pattern doc；權威敘述就是上面的檔頭。
REPO_LINT_REGISTRY_REF = "見本檔檔頭與 #598"

# #613：登記表的不可下降 floor。檔頭對帳守的是**一致性**，把一條 check 的四處
# （檔頭條目／映射表／run_all／run_hook）**一起刪掉**時四處仍然一致 ⇒ 全綠，守線
# 就這樣消失。floor 補的是**存在性**：baseline 登記過的名字一個都不准消失。
REPO_LINT_REGISTRY_BASELINE_REL = "scripts/audit/baselines/repo-lint-registry.txt"
REPO_LINT_REGISTRY_BASELINE = os.path.join(BASELINE_DIR, "repo-lint-registry.txt")
# floor 的兩份輸入（本檔 ＋ baseline 檔）——動到任一份，hook 都要重算。
REPO_LINT_REGISTRY_FILES = (REPO_LINT_SELF_REL, REPO_LINT_REGISTRY_BASELINE_REL)
# 🔴 floor 刻意**不叫 `check_*`**。叫了就會被上面那組雙射（檔頭 ↔ REPO_LINT_RULES ↔
#    run_all ↔ run_hook）收編，於是它自己也變成「四處一起刪就靜默消失」的一員——
#    那正是它要修的形態。名字獨立出來，run_all() 直接呼叫，而「run_all/run_hook 有沒有
#    呼叫它」由 check_repo_lint_docstring_registry 的第 (6) 條守。兩者互相守著對方：
#    刪掉任何一個都會紅，兩個一起刪才會綠——而那是一個大到瞞不住的 diff。
REPO_LINT_FLOOR_FN = "repo_lint_registry_floor"

# --------------------------------------------------------------------------- #
#  第 28 條：內部憑證 context key 註冊表（#788；承 #536 岔路①、普查 #787）        #
# --------------------------------------------------------------------------- #
#
# 反模式：拿一個 context key 當「這是內部呼叫」的憑證，而判定只看 truthy。
# `/web/dataset/call_kw` 把呼叫端送來的 context **整包**換進 env
# （`odoo/api.py::call_kw`，位置參數＝取代不是 merge）⇒ 任何 `base.group_user`
# 租戶都能用一行 JSON `context={"<key>": true}` 把那個閘打開。#522 在 18.0 上實測
# 成功過，#536 在 SA 家族又修了一批——這個 bug class 至今已反覆出現 ≥5 次，而在
# 本條之前**沒有任何機制**會在第 6 次出現時變紅：runtime 的 WARNING 只在既有的
# key 被偽造時才印，新寫的一把根本不經過它。
#
# 正解是「值的物件身分」而不是「值的真假」：模組私有 sentinel 實例，JSON／XML-RPC
# 的型別集合表達不出它（見 INTERNAL_CTX_KEY_DOC「為什麼值必須是 sentinel、不能是
# True」）。本條把那個紀律變成機械判準，三件事一起守：
#
#   (i)   registry 上 status=enforced 的 key，其**所有生產讀取點**都要走 sentinel
#         身分判定（`is` / `is not`），不得出現裸 truthy 布林 guard。
#   (ii)  同上這些 key 在 `src/`（生產碼）不得出現 `key=True` ／ `{'key': True}`
#         之類的 truthy 字面值——那是「值不是 sentinel」的另一半，寫了就等於自己
#         把閘退回可偽造的形狀（`src/tests/` 豁免：測試就是要偽造 payload）。
#   (iii) **涵蓋率**：任何被生產碼讀取、且拿來當 truthy 布林 guard 的 context key
#         都必須在 registry 上。可標 `pending`／`not-security` 豁免，但要寫理由，
#         而寫理由這個動作會出現在 diff 裡被覆核。**沒有 (iii) 這條 gate 就等於
#         沒做**：新增一把 key 只要不登記就完全不受管，而那正是本條要防的形態。
#
# 🔴 每一條都配陽性對照（見 check 內的 (a)/(b)/(c)），理由是這條 gate 的所有判準
#    都是**否定性**的（「找不到違規」）：掃描器壞掉、符號改名、範圍掃錯，輸出都與
#    「真的乾淨」一模一樣。對照的形狀抄第 27 條的 `_SH_SUB_SCAN_CONTROL`。
INTERNAL_CTX_KEY_DOC = "docs/decisions/519-cloud-service-provision-gate.md"

# 生產碼的範圍。`src/tests/` 排除：測試偽造 payload 正是它的工作（#788 明列）。
# `src/migrations/` **刻意包含**——migration 在升級時真的會跑，它是生產碼。
INTERNAL_CTX_KEY_SCAN_ROOT = "src"
INTERNAL_CTX_KEY_SKIP_PREFIXES = ("src/tests/",)

# 註冊表。
#
# 🔴 刻意是 in-code 常數而不是 `baselines/*.txt`：`--update-baseline` 會一次重產
#    **全部** baseline 檔（第 17 條檔頭已載明同樣的理由），把 registry 放進去等於
#    讓「新增一把未登記的鑰匙」可以被一句 `--update-baseline` 靜默收編——那正好把
#    (iii) 的價值歸零。形狀抄第 26 條的 `PROVISION_SENTINEL_ALLOWLIST`。
#
# 欄位語意：
#   status      'enforced'     —— 已 sentinel 化，(i)(ii) 全套生效。
#               'pending'      —— 已知的同形態裸 truthy 閘，**尚未**修（必附 issue）。
#                                 豁免 (i)(ii)，但仍受 (iii) 管：它在清單上、攤在
#                                 diff 裡，修好一把就把 status 翻成 enforced。
#               'not-security' —— 讀了、也是 truthy 閘，但不是安全決策（必附理由）。
#   sentinel    enforced 專用：模組私有 sentinel 的符號名。
#   defined_in  enforced 專用：sentinel（與 reader）的定義檔，陽性對照的錨點。
#   reader      enforced 選用：把 sentinel 判定收斂成一支私有函式時填它。有些 key
#               **沒有任何直接的 `context.get`** 讀取點（#536 三把走
#               `_is_sync_control(env, KEY)`），此時它是唯一的判定點來源——沒有這
#               個欄位，那三把會因為「掃不到讀取點」而恆綠。
#   issue       單號（pending 必填）。
#   why         為什麼是這個 status。豁免必填，enforced 也寫（給讀 diff 的人）。
INTERNAL_CTX_KEY_REGISTRY = {
    # --- #536：SA 的三把 sync-control 開關（PR #779 已 sentinel 化） -------- #
    "sa_skip_route_sync": {
        "status": "enforced",
        "sentinel": "_INTERNAL_SYNC_CONTROL",
        "defined_in": "src/models/security_access.py",
        "reader": "_is_sync_control",
        "issue": "#536",
        "why": "抑制對 operator 的宣告式路由推送；偽造 ⇒ DB 標 is_protected 而 CF "
               "從未建 Access app（fail-open）。",
    },
    "sa_rule_replace": {
        "status": "enforced",
        "sentinel": "_INTERNAL_SYNC_CONTROL",
        "defined_in": "src/models/security_access.py",
        "reader": "_is_sync_control",
        "issue": "#536",
        "why": "整組 rule 替換途中跳過「≥1 include」檢查；偽造 ⇒ 租戶可留下 0 條 "
               "include 的 policy。",
    },
    "sa_force_access_sync": {
        "status": "enforced",
        "sentinel": "_INTERNAL_SYNC_CONTROL",
        "defined_in": "src/models/security_access.py",
        "reader": "_is_sync_control",
        "issue": "#536",
        "why": "繞過 Access 推送 liveness gate 的狀態那一半；偽造 ⇒ suspended／"
               "deleting 的 SA 仍能把自己 route 的 Access 設定推去 Cloudflare。",
    },
    # --- #522 / #524 / #744：身分錨點寫入權 -------------------------------- #
    "sa_internal_identity_write": {
        "status": "enforced",
        "sentinel": "_INTERNAL_IDENTITY_WRITE",
        "defined_in": "src/models/security_access.py",
        "issue": "#522",
        "why": "SA 身分錨點（tunnel_id／canonical_name…）的唯一寫入豁免；#522 在 "
               "18.0 上實測「裸 truthy 版」可由租戶 RPC 偽造成功。",
    },
    "sa_internal_prefix_write": {
        "status": "enforced",
        "sentinel": "_INTERNAL_IDENTITY_WRITE",
        "defined_in": "src/models/security_access.py",
        "issue": "#522",
        "why": "subdomain_prefix 的 data-repair 豁免。刻意與身分錨點分成兩把鑰匙："
               "改名的 migration 不該因此也能搬 tunnel id。",
    },
    "woow_internal_provision": {
        "status": "enforced",
        "sentinel": "_INTERNAL_PROVISION",
        "defined_in": "src/models/cloud_service.py",
        "issue": "#519",
        "why": "cloud_service 建立閘。開閘路徑另受第 26 條 allowlist 管制。",
    },
    "woow_cs_access_identity_write": {
        "status": "enforced",
        "sentinel": "_INTERNAL_CS_ACCESS_IDENTITY_WRITE",
        "defined_in": "src/models/cloud_service.py",
        "issue": "#744",
        "why": "CS Access 身分錨點（access_app_id）的唯一寫入豁免；偽造 ⇒ 之後的 "
               "sync／teardown 會去動別人帳號裡的 Access application。",
    },
    "woow_smart_home_platform_order": {
        "status": "enforced",
        "sentinel": "_PLATFORM_ORDER",
        "defined_in": "src/models/smart_home.py",
        "issue": "#524",
        "why": "smart_home 只能從平台開通流程建立的閘。",
    },
    "woow_smart_home_internal_identity_write": {
        "status": "enforced",
        "sentinel": "_INTERNAL_IDENTITY_WRITE",
        "defined_in": "src/models/smart_home.py",
        "issue": "#524",
        "why": "SH 身分錨點的唯一寫入豁免（形狀同 #522）。",
    },
    "woow_sm_route_access_identity_write": {
        "status": "enforced",
        "sentinel": "_INTERNAL_SM_ROUTE_IDENTITY_WRITE",
        "defined_in": "src/models/smart_home_route.py",
        "issue": "#744",
        "why": "SH route 的 Access 身分錨點寫入豁免（形狀同 #522／#744）。",
    },
    # --- #787 A 段：同形態的一批（逐把驗可達性後再修） ------------------- #
    #
    # ⚠ 刻意不寫「還剩幾把」：那個數字每修好一把就腐爛一次，而它腐爛時沒有任何
    #   訊號。要現況就數下面 `issue == "#787"` 且 `status == "pending"` 的條目。
    #
    # 🔴 掛在這裡的目的不是放行，是**攤開**：它們原本只活在一張 issue 表裡，沒有
    #    任何機制會讓讀 code 的人知道。status 從 pending 翻成 enforced 那一刻，
    #    (i)(ii) 自動對它生效——修好一把的驗收條件因此是機械的，不靠人記得。
    "sm_route_cascade_unlink": {
        "status": "enforced",
        "sentinel": "_INTERNAL_SM_SYNC_CONTROL",
        "reader": "_is_sm_sync_hatch_open",
        "defined_in": "src/models/smart_home_route.py",
        "issue": "#787",
        "why": "SH cascade 刪除用的旁路，繞過 primary route 不可刪的 UserError。"
               "#787 實測：守衛**確實被偽造的 context 關掉**（真 call_kw unlink 的"
               "例外型別從 UserError 翻成 AccessError ＝ 守衛沒擋、流程往下撞 ACL），"
               "但刪除本身被 base.group_user 的 unlink=0 擋死 ⇒ 這把是 "
               "defense-in-depth：ACL 哪天鬆一格它就是最後一道。已改私有 sentinel。",
    },
    "sm_skip_access_sync": {
        "status": "enforced",
        "sentinel": "_INTERNAL_SM_SYNC_CONTROL",
        "reader": "_is_sm_sync_hatch_open",
        "defined_in": "src/models/smart_home_route.py",
        "issue": "#787",
        "why": "抑制 route create／write 後的 Access 宣告式推送（fail-open 且無聲）。"
               "#787 實測：`write` 那個讀取點**租戶可達**——自建 workspace 即 owner，"
               "smart_home_route_rule_owner_admin_manage 給完整寫入 ⇒ 真 call_kw 偽造"
               "後 is_protected 落地而 Cloudflare 完全不知情。`create` 那個讀取點被 "
               "ACL create=0 擋住（租戶到不了），但判準 (i) 對 enforced key 的**所有**"
               "生產讀取點生效，一併改。生產側只讀不設，helper 仍留著當可偽造寫法的"
               "唯一替代品。已改私有 sentinel（形狀同 #536）。",
    },
    "sm_force_access_sync": {
        "status": "enforced",
        "sentinel": "_INTERNAL_SM_SYNC_CONTROL",
        "reader": "_is_sm_sync_hatch_open",
        "defined_in": "src/models/smart_home_route.py",
        "issue": "#787",
        "why": "SA `sa_force_access_sync` 的 SH 同構版，繞過 route liveness gate 的"
               "狀態那一半。#787 實測**可達**：SH 停權中，租戶真 call_kw 改自己 route "
               "的 Access 設定並偽造這把，設定照樣推去 Cloudflare 並把 app_id 回寫進 "
               "DB（ir.rule 的 owner/admin domain 不濾 SH 的 state）。正當 setter 是 "
               "smart_home._do_resume 的收斂。已改私有 sentinel。",
    },
    "cs_skip_access_sync": {
        "status": "enforced",
        "sentinel": "_INTERNAL_CS_SYNC_CONTROL",
        "reader": "_is_cs_sync_control",
        "defined_in": "src/models/cloud_service.py",
        "issue": "#787",
        "why": "抑制 cloud_service.write() 的 access sync（#745）。#787 A 段五把裡"
               "第一把**實測可達**的：租戶自建 workspace 即 owner，"
               "`cloud_service_rule_owner_admin_manage` 給完整寫入 ⇒ 真 call_kw 打得"
               "進 write()，偽造後 `is_protected` 落地而 Cloudflare 不知情"
               "（fail-open 且無聲）。已改私有 sentinel（形狀同 #536）。",
    },
    "woow_force_issue": {
        "status": "pending",
        "issue": "#787",
        "why": "#787 A 段：讓帶 woow_skip_upstream_hook 的發票仍真的向 ECPay 開立"
               "（正當 setter 是 ecpay_invoice_cron）。account.move 是 ERP 模型，"
               "租戶能否經 RPC 觸達未驗證 ⇒ 🔴 不得當已坐實的後門轉述。",
    },
    # --- 非安全決策 -------------------------------------------------------- #
    "skip_ai_reply": {
        "status": "not-security",
        "why": "discuss_channel.message_post 的重複 AI 回覆抑制（SSE handler 已處理）。"
               "偽造的後果是呼叫者自己那則訊息不觸發 AI 回覆——不繞過任何授權、狀態或"
               "配額判定，也不影響別的租戶。",
    },
    "from_paas_chat": {
        "status": "not-security",
        "why": "同上：PaaS chat API 走 SSE 串流自行產生回覆，這裡先跳過以免阻塞 HTTP "
               "response。偽造只影響呼叫者自己那一則訊息的回覆路徑。",
    },
}

_INTERNAL_CTX_KEY_STATUSES = ("enforced", "pending", "not-security")
# 豁免（非 enforced）的兩種 status。它們的共同定義性質就是「這把鑰匙**現在**是裸
# truthy 閘」⇒ 掃描器必須在生產碼裡找得到它的布林 guard 讀取點。找不到有兩個可能：
# 掃描器壞了（假綠），或這把已經被修好／刪掉（清單腐爛）。兩者都要有人來看。
_INTERNAL_CTX_KEY_EXEMPT_STATUSES = ("pending", "not-security")


def _internal_ctx_key_watches(rel):
    """hook 分派判準：與 check 的掃描範圍共用同一個定義，兩者不可能分岔。

    registry 住在 repo-lint.py 自己，所以動它也要重跑（改 registry ↔ 改 src 是同一
    條不變式的兩端，而壞掉的常常是另一端）。
    """
    if rel == REPO_LINT_SELF_REL:
        return True
    return (rel.startswith(INTERNAL_CTX_KEY_SCAN_ROOT + "/")
            and rel.endswith(".py")
            and not rel.startswith(INTERNAL_CTX_KEY_SKIP_PREFIXES))


def _internal_ctx_key_prod_files(root):
    """生產碼（src/ 扣掉 src/tests/）的 .py 清單，rel path 排序。"""
    files = []
    base = os.path.join(root, INTERNAL_CTX_KEY_SCAN_ROOT)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root).replace(os.sep, "/")
            if rel.startswith(INTERNAL_CTX_KEY_SKIP_PREFIXES):
                continue
            files.append((rel, fp))
    return sorted(files)


def _ctx_parent_map(tree):
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _ctx_func_owner_map(tree):
    """node → 包住它的最內層 FunctionDef（沒有就不在表裡）。"""
    owner = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            owner[node] = fn          # 由外而內走，最內層最後寫入 ⇒ 勝出
    return owner


def _ctx_module_str_consts(tree):
    """module 層 `NAME = '字面字串'` 的對照表。"""
    consts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                consts[target.id] = node.value.value
    return consts


def _ctx_context_read(node):
    """這個節點是不是「從 context 讀一個 key」？是就回傳 key 的 AST 節點。

    認兩種載體：`<x>.context.get(K)` / `<x>._context.get(K)` 與 `<x>.context[K]`。
    ⚠ 已知邊界：把 `self.env.context` 先存成區域變數再 `.get()` 掃不到。今天全 repo
    沒有這種寫法；真的出現時本條會對那一處沉默（不是誤報），而 (iii) 的涵蓋率也就
    看不到它——這是本條**唯一**已知的假陰性形狀，刻意寫在這裡而不是假裝沒有。
    """
    if isinstance(node, ast.Call):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr in ("context", "_context")
                and node.args):
            return node.args[0]
        return None
    if isinstance(node, ast.Subscript):
        if (isinstance(node.value, ast.Attribute)
                and node.value.attr in ("context", "_context")):
            return node.slice
        return None
    return None


def _ctx_resolve_key(key_node, local_consts, global_consts):
    """key 的 AST 節點 → 字串。解不出來回 None（那不是違規，是「本條看不到」）。"""
    if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
        return key_node.value
    if isinstance(key_node, ast.Name):
        if key_node.id in local_consts:
            return local_consts[key_node.id]
        # 跨檔：`from .security_access import _CTX_SKIP_ROUTE_SYNC` 這種 import 進來
        # 的 key 常數。名字撞號（同名不同值）時放棄解析，寧可看不到也不要指錯人。
        return global_consts.get(key_node.id)
    return None


def _ctx_bool_only(node, parent):
    """這個節點的**值**是不是只被當真假值消費？

    認：`if/while/三元/assert` 的 test、`not X`、`bool(X)`、comprehension 的 if，
    以及**本身處在上述位置的** BoolOp 的成員。

    🔴 BoolOp 要遞迴看外層而不是直接算數，關鍵在區分這兩者：
        `if not ctx.get(K) and ...:`     → BoolOp 在 If.test ⇒ 布林 guard（要管）
        `for x in ctx.get(K) or []:`     → BoolOp 在 For.iter ⇒ 值的使用（不管）
    後者是 #787 B 段的「值注入」型偽造面，形態不同、修法不同，刻意不在本條範圍。
    """
    p = parent.get(node)
    if p is None:
        return False
    if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not):
        return True
    if (isinstance(p, ast.Call) and isinstance(p.func, ast.Name)
            and p.func.id == "bool" and p.args and p.args[0] is node):
        return True
    if isinstance(p, (ast.If, ast.While, ast.IfExp)) and p.test is node:
        return True
    if isinstance(p, ast.Assert) and p.test is node:
        return True
    if isinstance(p, ast.comprehension) and any(i is node for i in p.ifs):
        return True
    if isinstance(p, ast.BoolOp):
        return _ctx_bool_only(p, parent)
    return False


def _ctx_is_compare(node, parent, sentinel):
    """節點是不是 `X is <sentinel>` / `X is not <sentinel>` 的一邊？"""
    p = parent.get(node)
    if not (isinstance(p, ast.Compare) and len(p.ops) == 1
            and isinstance(p.ops[0], (ast.Is, ast.IsNot))):
        return False
    for other in [p.left] + list(p.comparators):
        if other is node:
            continue
        if isinstance(other, ast.Name) and other.id == sentinel:
            return True
    return False


def _ctx_assigned_then_compared(node, parent, owner, sentinel):
    """`value = ctx.get(K)` ＋同一支函式裡 `value is <sentinel>` 也算判定。

    #536 的 `_is_sync_control` 就是這個形狀（那支的 key 是參數、解不出字面值，所以
    今天走不到這裡）。收它是因為它是安全且常見的寫法；不收會逼人為了過 gate 把
    可讀的兩行改寫成一行。
    """
    p = parent.get(node)
    if not (isinstance(p, ast.Assign) and len(p.targets) == 1
            and isinstance(p.targets[0], ast.Name)):
        return False
    var = p.targets[0].id
    scope = owner.get(node)
    if scope is None:
        return False
    for sub in ast.walk(scope):
        if not (isinstance(sub, ast.Compare) and len(sub.ops) == 1
                and isinstance(sub.ops[0], (ast.Is, ast.IsNot))):
            continue
        names = {n.id for n in [sub.left] + list(sub.comparators)
                 if isinstance(n, ast.Name)}
        if var in names and sentinel in names:
            return True
    return False


def _ctx_truthy_const(node):
    """truthy 字面值？（True／1／非空字串…；False／0／None 不算）"""
    return isinstance(node, ast.Constant) and bool(node.value)


def _internal_ctx_key_scan(root):
    """掃一次生產碼，回傳本條三個判準共用的**唯一**事實來源。

    三條判準共用同一個掃描入口是刻意的：分成兩支各自走訪的話，「(iii) 掃得到、
    (i) 掃不到」這種分岔會讓其中一條靜默恆綠（第 1／3 條 baseline 的同款教訓——
    check 與 --update-baseline 必須共用唯一入口）。
    """
    facts = {
        "parse_errors": [],     # [(rel, exc)]
        "reads": [],            # [(key, rel, lineno, node, parent, owner, tree)]
        "truthy": [],           # [(key, rel, lineno, shape)]
        "reader_calls": [],     # [(reader_name, key, rel, lineno)]
        "files": 0,
        "read_sites": 0,        # 解不出 key 的也算（存在性對照用）
        "sentinel_defs": {},    # rel → {module 層被賦值的名字}
        "func_defs": {},        # rel → {函式名: FunctionDef}
        "text": {},             # rel → 原始碼
    }
    files = _internal_ctx_key_prod_files(root)
    parsed = []
    for rel, fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                src = f.read()
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError) as exc:
            facts["parse_errors"].append((rel, exc))
            continue
        facts["files"] += 1
        facts["text"][rel] = src
        parsed.append((rel, tree))

    # 第一輪：全域的 key 常數對照表（跨檔 import 的 `_CTX_*` 要解得出來）。同名不同
    # 值時整個名字作廢——指錯人比看不到更糟。
    global_consts, clashed = {}, set()
    for rel, tree in parsed:
        for name, value in _ctx_module_str_consts(tree).items():
            if name in global_consts and global_consts[name] != value:
                clashed.add(name)
            global_consts[name] = value
    for name in clashed:
        global_consts.pop(name, None)

    readers = {e.get("reader") for e in INTERNAL_CTX_KEY_REGISTRY.values()
               if e.get("reader")}

    # 第二輪：實際採集。
    for rel, tree in parsed:
        parent = _ctx_parent_map(tree)
        owner = _ctx_func_owner_map(tree)
        local_consts = _ctx_module_str_consts(tree)
        facts["sentinel_defs"][rel] = {
            t.id for n in tree.body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)}
        facts["func_defs"][rel] = {
            n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in ast.walk(tree):
            key_node = _ctx_context_read(node)
            if key_node is not None:
                facts["read_sites"] += 1
                key = _ctx_resolve_key(key_node, local_consts, global_consts)
                if key is not None:
                    facts["reads"].append(
                        (key, rel, node.lineno, node, parent, owner))
                continue
            if isinstance(node, ast.Call):
                # (ii) 的一半：`with_context(key=True)` 這類 keyword truthy 字面值。
                for kw in node.keywords:
                    if (kw.arg in INTERNAL_CTX_KEY_REGISTRY
                            and _ctx_truthy_const(kw.value)):
                        facts["truthy"].append(
                            (kw.arg, rel, node.lineno,
                             "%s=%r" % (kw.arg, kw.value.value)))
                # reader 判定點：`_is_sync_control(env, _CTX_SKIP_ROUTE_SYNC)`
                fname = (node.func.id if isinstance(node.func, ast.Name)
                         else node.func.attr if isinstance(node.func, ast.Attribute)
                         else None)
                if fname in readers:
                    for arg in list(node.args) + [k.value for k in node.keywords]:
                        key = _ctx_resolve_key(arg, local_consts, global_consts)
                        if key is not None:
                            facts["reader_calls"].append(
                                (fname, key, rel, node.lineno))
            elif isinstance(node, ast.Dict):
                # (ii) 的另一半：`{'key': True}` 與 `**{KEY_CONST: True}`。
                for k_node, v_node in zip(node.keys, node.values):
                    if k_node is None or not _ctx_truthy_const(v_node):
                        continue
                    key = _ctx_resolve_key(k_node, local_consts, global_consts)
                    if key in INTERNAL_CTX_KEY_REGISTRY:
                        facts["truthy"].append(
                            (key, rel, node.lineno,
                             "{%r: %r}" % (key, v_node.value)))
    return facts


def _internal_ctx_key_registry_shape():
    """registry 自己的結構自檢。壞掉的 registry 會讓下面三條判準無聲失去鑑別力。"""
    problems = []
    for key, entry in sorted(INTERNAL_CTX_KEY_REGISTRY.items()):
        where = "`INTERNAL_CTX_KEY_REGISTRY['%s']`" % key
        if not isinstance(entry, dict):
            problems.append("%s 必須是 dict（現在是 %r）" % (where, entry))
            continue
        status = entry.get("status")
        if status not in _INTERNAL_CTX_KEY_STATUSES:
            problems.append(
                "%s 的 status=%r 不在 %s ——status 決定 (i)(ii) 對它生不生效，打錯字"
                "會靜默變成豁免" % (where, status, "／".join(_INTERNAL_CTX_KEY_STATUSES)))
            continue
        if not (entry.get("why") or "").strip():
            problems.append(
                "%s 沒有寫 `why`。豁免與強制都要寫理由——#788 要的就是「豁免這個動作"
                "會出現在 diff 裡被覆核」，沒有理由的條目讓那件事失效" % where)
        if status == "enforced":
            for field in ("sentinel", "defined_in"):
                if not (entry.get(field) or "").strip():
                    problems.append(
                        "%s status=enforced 卻沒有 `%s`——那是本條陽性對照的錨點，"
                        "少了它「掃不到違規」與「掃描器壞掉」無法區分" % (where, field))
        elif status == "pending" and not (entry.get("issue") or "").strip():
            problems.append(
                "%s status=pending 卻沒有 `issue`。pending＝已知未修，沒有單號就沒有"
                "任何東西會讓它被修——那是永久豁免換一個名字" % where)
    return problems


def check_internal_ctx_key_registry(root):
    """#788：內部憑證 context key 必須走私有 sentinel，且全部登記在案。

    守的 bug class：拿 context key 當「這是內部呼叫」的憑證、判定只看 truthy。
    `call_kw` 把呼叫端 context 整包換進 env ⇒ 租戶一行 JSON 就能開閘（#522 實測
    成功）。#522／#524／#519／#744／#536 各修過一批，第 6 次不會有任何訊號——
    runtime WARNING 只在既有 key 被偽造時印，**新寫的一把根本不經過它**。

    三條判準（(iii) 是核心：少了它，新 key 不登記就等於不受管）：

    (i)   enforced key 的所有生產讀取點都走 sentinel 身分判定（`is` / `is not`），
          不得有裸 truthy 布林 guard。
    (ii)  enforced key 在生產碼不得出現 truthy 字面值（`key=True`／`{'key': True}`）
          ——那是把閘退回可偽造形狀的另一半。`src/tests/` 豁免。
    (iii) 任何被生產碼讀取、且當 truthy 布林 guard 的 context key 都必須在 registry
          上；可標 pending／not-security 豁免，但要寫理由。

    陽性對照（否定性結論必配，見 memory: feedback_negative_verification_needs_positive_control）：
      (a) 掃描本身：一個 context 讀取點都沒掃到 ＝ 掃描器／範圍壞了，不是乾淨。
      (b) enforced：sentinel 符號必須真的在 `defined_in` 定義、reader（若宣告）必須
          存在且真的拿 sentinel 做 `is` 比對、而且該 key 至少要有一個判定點。三者
          任一不成立，「找不到裸 truthy 讀取點」這個綠燈就是假的。
      (c) 豁免條目：它們的定義性質就是「現在是裸 truthy 閘」⇒ 掃描器必須找得到它的
          布林 guard 讀取點。找不到 ⇒ 掃描器壞了，或這把已被修好／刪掉（清單腐爛）。
          這一格讓 (iii) 不可能做成恆綠：判準若只有「有沒有多出來的 key」，掃描器
          回空集合時它永遠是綠的。

    ⚠ 覆蓋邊界（刻意寫下來，不假裝沒有）：
      * 只認 `<x>.context.get(K)` / `<x>._context.get(K)` / `<x>.context[K]` 三種載體，
        且 K 要解得出字面值（module 層常數會跨檔解析）。先把 context 存成區域變數
        再讀的寫法掃不到。
      * 只管 **truthy 布林閘**。#787 B 段的「值注入」型偽造面（`sa_unlinked_cf_policy_ids`
        / `mcp_admin_password` / `bootstrap_owner_partner_id`）形態不同、修法不同，
        不在本條範圍——把本條的綠讀成「所有 context 偽造面都守住了」是錯的。
      * (ii) 只認**字面值**：`key=True` 與 `{'key': True}` 兩種載體。動態組出來的
        dict 再展開（`ctx = {...}; rec.with_context(**ctx)`）看不到——那需要 dataflow
        分析，而本條刻意停在 AST 形態比對。這不是漏洞的入口（sentinel 判定照樣不認
        那個值，閘不會開），是「本條抓不到那個寫法」的誠實邊界：真正的後果是有人那樣
        寫時本條不會提醒他「這一行不會開閘」。
      * 只看**平台自己的** src/。第三方 addon 的 context key 不在管轄內。
    依據 docs/decisions/519-cloud-service-provision-gate.md
    """
    violations = []
    if not _addon_source_tree_present(root):
        return violations          # 不是本 addon 的 root，本條沒有輸入（見上方註解）

    ref = "見 %s 與 #536／#787／#788" % INTERNAL_CTX_KEY_DOC
    self_rel = REPO_LINT_SELF_REL

    for problem in _internal_ctx_key_registry_shape():
        violations.append("%s：%s（%s）" % (self_rel, problem, ref))

    facts = _internal_ctx_key_scan(root)

    # 🔴 parse 失敗當違規，不當無事：本條的所有判準都是否定性的，讀不到的檔案
    #    「沒有違規」與「有違規但看不到」輸出完全相同。
    for rel, exc in facts["parse_errors"]:
        violations.append(
            "%s：讀不到或 parse 失敗（%s）——本條對這個檔失去鑑別力，而輸出與「掃過"
            "而且乾淨」完全相同（%s）" % (rel, exc, ref))

    # (a) 掃描存在性對照。
    if facts["read_sites"] == 0:
        violations.append(
            "%s：`check_internal_ctx_key_registry` 掃了 %d 個生產檔卻連一個 "
            "`context.get(...)` 都沒看到——掃描範圍或 AST 判準壞了，這條的「零違規」"
            "是假的（%s）" % (self_rel, facts["files"], ref))

    bool_sites = {}        # key → [(rel, lineno)]  只收「當真假值消費」的讀取點
    for key, rel, lineno, node, parent, owner in facts["reads"]:
        if _ctx_bool_only(node, parent):
            bool_sites.setdefault(key, []).append((rel, lineno))

    reads_by_key = {}
    for key, rel, lineno, node, parent, owner in facts["reads"]:
        reads_by_key.setdefault(key, []).append((rel, lineno, node, parent, owner))

    judged = {}            # key → 判定點數（sentinel 比對 or reader 呼叫）
    for key, entry in sorted(INTERNAL_CTX_KEY_REGISTRY.items()):
        if entry.get("status") != "enforced":
            continue
        sentinel = entry.get("sentinel") or ""
        defined_in = entry.get("defined_in") or ""
        count = 0
        # (i) 逐個讀取點判形狀。
        for rel, lineno, node, parent, owner in reads_by_key.get(key, []):
            if _ctx_is_compare(node, parent, sentinel):
                count += 1
                continue
            if _ctx_assigned_then_compared(node, parent, owner, sentinel):
                count += 1
                continue
            if _ctx_bool_only(node, parent):
                violations.append(
                    "%s:%d：`%s` 是 registry 上 status=enforced 的內部憑證 key，卻被"
                    "當**裸 truthy** 布林 guard 讀取。`call_kw` 把呼叫端 context 整包"
                    "換進 env ⇒ 任何租戶用 `context={\"%s\": true}` 就能開這個閘"
                    "（#522 實測成功過）。判準必須是值的**物件身分**："
                    "`... is %s`（%s）"
                    % (rel, lineno, key, key, sentinel or "<sentinel>", ref))
            else:
                violations.append(
                    "%s:%d：`%s` 是 status=enforced 的內部憑證 key，但這個讀取點既沒有"
                    "拿 `%s` 做 `is` 比對，也不是「先賦值再於同函式內比對」的形狀——"
                    "本條判定不出它是不是 sentinel 判定，因此不放行。改成顯式 `is` "
                    "比對，或把判定收斂進一支私有 reader 並登記在 registry 的 "
                    "`reader` 欄位（%s）"
                    % (rel, lineno, key, sentinel or "<sentinel>", ref))
        count += sum(1 for _f, k, _r, _l in facts["reader_calls"] if k == key)
        judged[key] = count

        # (b) 陽性對照：sentinel 符號、reader、判定點三者都要真的在。
        if defined_in not in facts["sentinel_defs"]:
            violations.append(
                "%s：registry 說 `%s` 的 sentinel 定義在 `%s`，但掃描範圍裡沒有這個"
                "生產檔（改名／搬家／打錯字都會讓本條對這把鑰匙恆綠）（%s）"
                % (self_rel, key, defined_in, ref))
        elif sentinel not in facts["sentinel_defs"][defined_in]:
            violations.append(
                "%s：`%s` 在 `%s` 裡找不到 module 層的 `%s`。這個符號名就是本條的"
                "判準——它一消失，所有 `is` 比對都比不中，gate 會掃不到任何東西而"
                "**恆綠**。改名請同步改 registry，刪除請連同這個條目一起刪並說明"
                "（%s）" % (self_rel, key, defined_in, sentinel, ref))
        reader = entry.get("reader")
        if reader:
            fn = facts["func_defs"].get(defined_in, {}).get(reader)
            if fn is None:
                violations.append(
                    "%s：registry 說 `%s` 的判定收斂在 `%s` 的 `%s()`，但那支函式不"
                    "存在。有些 key **只**經由 reader 判定、沒有任何直接讀取點，"
                    "reader 一消失本條就對它完全沉默（%s）"
                    % (self_rel, key, defined_in, reader, ref))
            else:
                ok = any(isinstance(n, ast.Compare) and len(n.ops) == 1
                         and isinstance(n.ops[0], (ast.Is, ast.IsNot))
                         and any(isinstance(o, ast.Name) and o.id == sentinel
                                 for o in [n.left] + list(n.comparators))
                         for n in ast.walk(fn))
                if not ok:
                    violations.append(
                        "%s：`%s()`（%s）是 registry 登記的 sentinel reader，但它的"
                        "函式體裡沒有任何 `is %s` 比對——reader 自己被改成裸 truthy "
                        "判定的話，所有走它的 key 會一起靜默失守，而本條原本一個都"
                        "抓不到（%s）"
                        % (self_rel, reader, defined_in, sentinel, ref))
        if judged[key] == 0:
            violations.append(
                "%s：`%s` 標成 enforced，但生產碼裡找不到**任何**判定點（既沒有 "
                "`is %s` 比對，也沒有帶這個 key 的 `%s()` 呼叫）。「找不到裸 truthy "
                "讀取點」在這種狀態下是假綠：這把鑰匙可能已經沒人用了（請刪條目）、"
                "或判定被改寫成本條認不得的形狀（%s）"
                % (self_rel, key, sentinel or "<sentinel>",
                   entry.get("reader") or "reader", ref))

        # (ii) 生產碼不得有 truthy 字面值。
        for tkey, rel, lineno, shape in facts["truthy"]:
            if tkey != key:
                continue
            violations.append(
                "%s:%d：`%s` 是 status=enforced 的內部憑證 key，生產碼卻寫了 truthy "
                "字面值 `%s`。sentinel 判定只認模組私有實例 ⇒ 這一行**不會開閘**"
                "（無聲失效，只會在 runtime 印一行 WARNING），而且它會讓下一個人以為"
                "「傳 True 就行」而把守衛改回可偽造的形狀。請改用該模組的開閘 helper"
                "（%s）" % (rel, lineno, key, shape, ref))

    # (iii) 涵蓋率：生產碼裡當 truthy 布林 guard 用的 key 一律要在 registry 上。
    for key in sorted(bool_sites):
        if key in INTERNAL_CTX_KEY_REGISTRY:
            continue
        where = "、".join("%s:%d" % s for s in bool_sites[key][:5])
        violations.append(
            "%s：context key `%s` 被生產碼當 **truthy 布林 guard** 使用（%s），但它"
            "不在 `INTERNAL_CTX_KEY_REGISTRY` 上。`call_kw` 把呼叫端 context 整包換進"
            " env ⇒ 這種閘預設是租戶可偽造的。請二擇一：改成模組私有 sentinel 判定並"
            "登記 status=enforced；或登記 status=not-security／pending **並寫下理由**"
            "（豁免要出現在 diff 裡被覆核，這正是本條存在的意義）（%s）"
            % (bool_sites[key][0][0], key, where, ref))

    # (c) 豁免條目的陽性對照 ＋ 殭屍條目。
    for key, entry in sorted(INTERNAL_CTX_KEY_REGISTRY.items()):
        status = entry.get("status")
        if status in _INTERNAL_CTX_KEY_EXEMPT_STATUSES and key not in bool_sites:
            violations.append(
                "%s：`%s` 登記為 status=%s（＝「這把現在就是裸 truthy 閘」），掃描器"
                "卻在生產碼裡找不到它的布林 guard 讀取點。兩種可能，兩種都要有人看："
                "①掃描器／解析壞了（那麼 (iii) 的「沒有多餘的 key」是假綠）；②這把已經"
                "改成 sentinel 或被刪掉了（請把 status 改成 enforced，或移除條目）"
                "（%s）" % (self_rel, key, status, ref))
    # 刻意**沒有**第 26 條那種獨立的「殭屍條目」檢查：這裡的殭屍已經被上面兩格
    # 涵蓋——enforced 的殭屍撞 `judged == 0`，豁免的殭屍撞 `not in bool_sites`。
    # 再加一條只會讓同一件事報兩次，而重複的訊息會稀釋掉真正該看的那一條。
    return violations



# 檔頭的編號條目（＝規則）→ 實作它的 check 函式名。
#
# 🔴 這張表是「一條規則可以由多個函式實作」這個一對多關係的**唯一顯式表達**，也是
#    檔頭與 run_all() 之間唯一的橋。刻意不寫成 `函式數 - 2` 這類差額常數：那種寫法
#    在第二條一對多規則出現的當下會**靜默算對總數卻對錯人**（#598 工單明列）。
# 🔴 新增一條 check 要同時動四處：這張表、檔頭條目、run_all()、run_hook() 分派。
#    四處互相對帳，少改任何一處都會紅——那正是 #596 缺的機制（當時只修了症狀）。
# 順序即檔頭條目順序；run_all() 的通過訊息直接由它產生，好讓「守了哪些線」少一份
# 手抄清單。實證：PR #608 在 .gitea/workflows/repo-lint.yml 寫「17 條 check」，而
# 當時檔頭寫「十八個 check 函式、十六條規則」，run_all() 的通過訊息又列了 17 個
# 名字——三份散文三個數字，#596 修完不到一天就長出第四處同型漂移。
# 「這個 root 是不是本 addon 的原始碼樹」的判準。
#
# 🔴 為什麼需要它：下面這條 gate 的輸入是**整棵 src/**，而 hook 模式會被餵到不是本
# repo 的 root——`scripts/audit/test_repo_lint_docstring_registry.py` 的 TestHookMode
# 就是把 repo-lint.py 複製進一個臨時 root 端對端跑整支腳本（那是 #596 唯一該有的
# 驗收：hook 分派真的接得到這個檔嗎）。在那種 root 底下「掃不到任何 src/ 檔案」會被
# 本條的**存在性對照**判成違規，於是一個乾淨的檔案被 block。
#
# ⚠ 判準刻意用 `src/__manifest__.py` 而不是本條自己的錨點檔：
#   * 真 repo 裡 manifest 一定在 ⇒ 存在性對照**全部照常生效**，即使有人把
#     `src/models/cloud_service.py` 整個刪掉也照樣紅（那才是要抓的漂移）。
#   * manifest 都不在的 root，本 addon 根本不存在，本條沒有輸入可談。
#   ⇒ 這是「有沒有輸入」的前置條件，不是把判準放寬。
_ADDON_MANIFEST_REL = "src/__manifest__.py"


def _addon_source_tree_present(root):
    return os.path.exists(os.path.join(root, _ADDON_MANIFEST_REL))


PROVISION_SENTINEL_DOC = "docs/decisions/519-cloud-service-provision-gate.md"
PROVISION_SENTINEL_DEF = "src/models/cloud_service.py"
# 開閘符號**只准**出現在這些檔（外加 src/tests/ 整個目錄）。新增一條開閘路徑就會
# 撞 gate ⇒ 強制走審查。這正是 #519 提案 A 唯一真實的殘留風險：未來某支 sudo'd
# controller 把呼叫者 context 往下傳並自己開閘。
PROVISION_SENTINEL_ALLOWLIST = {
    PROVISION_SENTINEL_DEF,                      # sentinel 的定義處
    "src/controllers/paas.py",                   # marketplace 下單
    "src/controllers/ha_paas.py",                # HA provision（auth=none + Bearer）
    ".claude/skills/k8s-dev-sandbox/scripts/e2e-seed-reset-button.sh",   # odoo shell seed
}
PROVISION_SENTINEL_ALLOW_PREFIXES = ("src/tests/",)
# 判準的錨點。三個一起找是刻意的：只找函式名的話，把 sentinel 改名成別的字
# （`_provision_ok` 之類）會讓整條 gate 掃不到任何東西 ⇒ 恆綠。下面的 (a) 用定義
# 檔當陽性對照把這個假綠關掉。
_PROVISION_SENTINEL_RE = re.compile(
    r"_INTERNAL_PROVISION|_internal_provision|woow_internal_provision")


_PROVISION_SENTINEL_ROOTS = ("src/", "scripts/", "extra/", ".claude/")
_PROVISION_SENTINEL_EXTS = (".py", ".sh", ".js", ".xml")


def _provision_sentinel_watches(rel):
    """hook 分派判準：與 check 的掃描範圍共用同一個定義，兩者不可能分岔。"""
    return (rel.startswith(_PROVISION_SENTINEL_ROOTS)
            and rel.endswith(_PROVISION_SENTINEL_EXTS))


def check_provision_sentinel_allowlist(root, only_file=None):
    """#519 開閘符號的 allowlist：只有列名的檔（＋ src/tests/）能開閘。

    A 的不可偽造性錨定在「開閘的地方就這幾處、而且每一處都被審過」。RPC 端偽造
    不了 sentinel（JSON 載不了物件身分），所以剩下唯一的路是**讓平台自己的碼替
    攻擊者開閘**——一支新的 sudo'd controller 把呼叫者的 vals 往下送並開閘，閘就
    等於沒有。那件事不會讓任何測試變紅，只會讓 diff 多一行。
    依據 docs/decisions/519-cloud-service-provision-gate.md
    """
    violations = []
    if not _addon_source_tree_present(root):
        return violations          # 不是本 addon 的 root，本條沒有輸入（見上方註解）
    # (a) 陽性對照：定義檔必須真的有這些符號。沒有＝符號被改名／被刪，下面的掃描
    #     會一個都掃不到而恆綠——那是「探針壞掉時輸出與沒問題一模一樣」的形態。
    def_src = _read_repo_file(root, PROVISION_SENTINEL_DEF)
    if def_src is None or not _PROVISION_SENTINEL_RE.search(def_src):
        violations.append(
            "%s：找不到 #519 的開閘符號（`_INTERNAL_PROVISION` / "
            "`_internal_provision` / `woow_internal_provision`）。本條的判準就是這幾個"
            "名字，錨點消失時整條 gate 會掃不到任何東西而恆綠——改名請同步改"
            "`_PROVISION_SENTINEL_RE`，刪除請連同本條一起刪並說明（見 %s）。"
            % (PROVISION_SENTINEL_DEF, PROVISION_SENTINEL_DOC))

    exts = _PROVISION_SENTINEL_EXTS
    if only_file:
        files = [only_file]
    else:
        files = []
        for sub in ("src", "scripts", "extra", ".claude"):
            base = os.path.join(root, sub)
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames
                               if d not in (".git", "node_modules", "__pycache__",
                                            "worktrees")]
                for fn in filenames:
                    if fn.endswith(exts):
                        files.append(os.path.join(dirpath, fn))
    seen = set()
    for fp in sorted(files):
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if rel == REPO_LINT_SELF_REL:
            continue                     # 本檔自己就寫著那幾個名字
        src = None
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        if not _PROVISION_SENTINEL_RE.search(src):
            continue
        seen.add(rel)
        if rel in PROVISION_SENTINEL_ALLOWLIST:
            continue
        if rel.startswith(PROVISION_SENTINEL_ALLOW_PREFIXES):
            continue
        violations.append(
            "%s：出現 #519 的 cloud_service 開閘符號，但這個檔不在 allowlist 上。"
            "開閘＝讓一條路徑繞過「沒有任何 RPC 呼叫端建得出 cloud_service」這條"
            "不變式；新增開閘路徑必須顯式加進 `PROVISION_SENTINEL_ALLOWLIST` 並在 PR "
            "裡說明為什麼那條路徑的授權是自足的（見 %s）。"
            % (rel, PROVISION_SENTINEL_DOC))
    # (b) allowlist 不准養殭屍：列了卻已經不再開閘的檔要拿掉，否則 allowlist 會慢慢
    #     長成「反正都在名單上」而失去審查意義。only_file 模式看不到全 repo，跳過。
    if not only_file:
        for rel in sorted(PROVISION_SENTINEL_ALLOWLIST - seen):
            violations.append(
                "%s：列在 `PROVISION_SENTINEL_ALLOWLIST` 但檔案裡已經沒有開閘符號"
                "（殭屍條目）。請從 allowlist 移除——名單只反映**現在真的會開閘**的"
                "路徑才有審查價值（見 %s）。" % (rel, PROVISION_SENTINEL_DOC))
    return violations


SH_SUB_GROUP_DOC = "docs/decisions/519-cloud-service-provision-gate.md"
# 第三方 sh_subscription 的兩個群組。平台**永遠不該**引用它們：一旦其中任何一個被
# implied 給 `base.group_user` / `base.group_portal`，租戶就能直接改自己訂閱的
# `state` / `sh_partner_id`，#519 提案 B 的兩個判準同時失效——而且是**無聲**失效。
_SH_SUB_GROUP_NAMES = ("group_user_sh_subscription", "group_manager_sh_subscription")
# 反向的存在性對照：這個字串在被掃的檔案裡一定找得到。找不到＝掃描範圍錯了／檔案讀
# 不到，那時候「零命中」是探針壞掉，不是「真的沒有」。
_SH_SUB_SCAN_CONTROL = "base.group_user"


def check_sh_subscription_group_isolation(root, only_file=None):
    """平台不得把 sh_subscription 的群組發給租戶群組。

    #519 提案 B 的判準（訂閱的 `sh_partner_id` 與 `state` 租戶都改不動）錨定在
    **第三方 addon 的 ACL** 上：租戶對 `sh.subscription.subscription` 零 ACL row。
    那個前提哪天被一行 `groups_id` / implied_ids 破壞掉，B 會靜默失效——沒有任何
    測試會紅，因為 B 的測試驗的是守衛擋不擋得住，不是租戶有沒有別的路可以走。
    依據 docs/decisions/519-cloud-service-provision-gate.md
    """
    violations = []
    if not _addon_source_tree_present(root):
        # 同第 26 條：不是本 addon 的 root，本條沒有輸入。今天的分派條件
        # （`rel.startswith("src/")`）剛好踩不到 TestHookMode 的合成 root，但那是
        # 巧合不是設計——同一個陷阱擺著就會有人踩。
        return violations
    exts = (".xml", ".csv", ".py")
    if only_file:
        files = [only_file]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, "src")):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
            for fn in filenames:
                if fn.endswith(exts):
                    files.append(os.path.join(dirpath, fn))
    control_seen = False
    for fp in sorted(files):
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        if _SH_SUB_SCAN_CONTROL in src:
            control_seen = True
        # 判準是**授予的形狀**（`implied_ids` / `groups_id` 與群組名同一行），不是
        # 「有沒有提到這個名字」。單純提到它的地方全都是本條想要的紀律，不是違規：
        # `subscription_lifecycle_mixin._sub()` 的 docstring 就寫著「read ACL 只發給
        # 這兩個群組」（那正是 sudo traversal 存在的理由），而
        # `test_subscription_mixin_cascade_sudo_29` 具名引用它們來斷言租戶**沒有**被
        # 塞進去。把那些判成違規會逼人刪掉守線的說明文字。
        # ⚠ 已知限制：`implied_ids` 與群組名被寫成跨行時掃不到。真實的授予寫法
        # （`eval="[(4, ref('...'))]"` 單行、`groups_id = [(4, ...)]` 單行）都在同一行。
        for line_no, line in enumerate(src.splitlines(), 1):
            if not ("groups_id" in line or "implied_ids" in line):
                continue
            for name in _SH_SUB_GROUP_NAMES:
                if name in line:
                    violations.append(
                        "%s:%d：引用了第三方 sh_subscription 的群組 `%s`。#519 提案 B "
                        "的兩個判準（訂閱的 sh_partner_id 與 state 租戶都改不動）錨定在"
                        "「租戶對 sh.subscription.subscription 零 ACL」上——把這個群組"
                        "發給租戶群組會讓 B **無聲**失效（沒有任何測試會紅）。真的需要"
                        "的話請在 PR 裡說明並同步重評 B（見 %s）。"
                        % (rel, line_no, name, SH_SUB_GROUP_DOC))
    # 探針壞掉時輸出與「真的沒問題」一模一樣 ⇒ 用一個一定找得到的字串當存在性對照。
    if not only_file and not control_seen:
        violations.append(
            "scripts/audit/repo-lint.py：`check_sh_subscription_group_isolation` 掃了 "
            "%d 個檔卻連 `%s` 都沒看到 —— 掃描範圍壞了，這條的「零命中」是假的。"
            % (len(files), _SH_SUB_SCAN_CONTROL))
    return violations


REPO_LINT_RULES = {
    "controller-sudo": ("check_controller_sudo",),
    "owl-template": ("check_owl_template",),
    "data-noupdate": ("check_data_noupdate",),
    "reuse-values": ("check_reuse_values",),
    "seed-topology": ("check_seed_topology",),
    "test-env-restore": ("check_test_env_restore",),
    "capability-gate": ("check_slug_capability_gate",
                        "check_admin_gui_gate_anchor",
                        "check_gateway_copy_gate_anchor",
                        "check_access_tab_gate_anchor"),
    "conceal-instances": ("check_conceal_instances_gate",),
    "reset-budget-ladder": ("check_reset_budget_ladder",),
    "transition-anchor-utc": ("check_transition_anchor_utc",),
    "assertlogs-numeric-level": ("check_assertlogs_numeric_level",),
    "mixin-subscription-sudo": ("check_mixin_subscription_sudo",),
    "config-parameter-uninstall": ("check_config_parameter_uninstall",),
    "duplicate-class-methods": ("check_duplicate_class_methods",),
    "pattern-doc-registry": ("check_pattern_doc_registry",),
    "availability-reason-labels": ("check_availability_reason_labels",),
    "repo-lint-docstring-registry": ("check_repo_lint_docstring_registry",),
    "kubectl-ns-whitelist-sync": ("check_kubectl_ns_whitelist_sync",),
    "poll-release-facts-single-source": ("check_poll_release_facts_single_source",),
    "doc-symbol-anchor": ("check_doc_symbol_anchor",),
    "hermes-agent-013-seed": ("check_hermes_agent_013_seed",),
    "security-rule-lifecycle": ("check_security_rule_noupdate",
                                "check_retired_rule_xmlid"),
    "workspace-access-rule-directions": ("check_workspace_access_rule_directions",),
    "one-time-credential-render": ("check_one_time_credential_render",),
    "workspace-rule-directions": ("check_workspace_rule_directions",),
    "provision-sentinel-allowlist": ("check_provision_sentinel_allowlist",),
    "sh-subscription-group-isolation": ("check_sh_subscription_group_isolation",),
    "internal-ctx-key-registry": ("check_internal_ctx_key_registry",),
    "access-gate-no-ledger-read": ("check_access_gate_no_ledger_read",),
    "tunnel-config-wire-shape": ("check_tunnel_config_wire_shape",),
}

# 刻意不掛 hook 分派的 check：{函式名: 理由}。現況是空的（每一條都掛得到 hook）。
# 要豁免就必須寫下理由——豁免這個動作會出現在 diff 裡可被覆核，靜默漏掛不會
# （#596 本體＝漏掛 run_hook：CI 會抓、hook 完全沉默，而兩者的輸出一模一樣）。
REPO_LINT_HOOK_EXEMPT = {}

# 條目格式：行首 `N. rule-name` ＋兩個以上空白 ＋說明；續行一律縮排（縮排的行不會
# 被當成新條目）。`(?=\s)` 是為了不把名字截半就收工。
_REPO_LINT_ENTRY_RE = re.compile(r"^(\d+)\.[ \t]+([a-z0-9][a-z0-9-]*)(?=\s)", re.M)
# 計數宣告只認**檔頭區**（第一條編號條目之前）。#576 的教訓：findall 掃全文會被
# 條目說明裡任何一句含數字的散文觸發，而訊息把人導向開頭那行——那裡的數字已經是
# 對的，照著去改永遠修不好。限縮範圍同時消滅假陽性，改寫訊息只是讓假陽性好懂。
_REPO_LINT_FUNC_COUNT_RE = re.compile(r"([〇零一二兩三四五六七八九十\d]+)個\s*check\s*函式")
_REPO_LINT_RULE_COUNT_RE = re.compile(r"([〇零一二兩三四五六七八九十\d]+)條規則")
_CN_NUMERALS = {"〇": 0, "零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
                "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_int(text):
    """「十九」／「19」→ 19；認不得回 None。只支援 0–99（本檔的規模）。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    head, sep, tail = text.partition("十")
    if not sep:
        return _CN_NUMERALS.get(text) if len(text) == 1 else None
    if len(head) > 1 or len(tail) > 1:
        return None
    tens = 1 if not head else _CN_NUMERALS.get(head)
    ones = 0 if not tail else _CN_NUMERALS.get(tail)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


def _repo_lint_check_calls(node):
    """node 底下所有 `check_*(...)` 呼叫的函式名（含重複；不保證原始碼順序）。"""
    return [n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id.startswith("check_")]


def _repo_lint_module_literal(tree, name):
    """讀 module 層 `NAME = <literal>` 的值；找不到或不是 literal 回 None。"""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        try:
            return ast.literal_eval(node.value)
        except (ValueError, SyntaxError, TypeError):
            return None
    return None


def _dups(seq):
    return sorted({x for x in seq if seq.count(x) > 1})


def _repo_lint_calls_named(node, name):
    """node 底下有沒有 `name(...)` 呼叫（floor 不是 check_*，走不了上面那個 helper）。"""
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == name for n in ast.walk(node))


def repo_lint_registry_names(root):
    """本檔目前登記了哪些規則與哪些 check 函式——floor 與 baseline 產生器的單一真相源。

    回傳 `(規則名 list, run_all() 呼叫的 check 函式名 list)`，兩者都已排序；讀不到／
    parse 不了／映射表不成形回 **None**。回 None 而不是回空 tuple 是刻意的：呼叫端要
    有辦法分辨「今天一條都沒有」與「探針壞了」，前者該紅在別的地方，後者必須自己紅
    ——`⊆` 判準在空集合上恆真，讓探針失敗靜默退化成空集合就等於永久假綠。
    """
    try:
        with open(os.path.join(root, REPO_LINT_SELF_REL), encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError):
        return None
    rules = _repo_lint_module_literal(tree, "REPO_LINT_RULES")
    run_all_fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                       and n.name == "run_all"), None)
    if not isinstance(rules, dict) or not rules or run_all_fn is None:
        return None
    return sorted(rules), sorted(set(_repo_lint_check_calls(run_all_fn)))


def repo_lint_registry_floor(root):
    """不可下降的 floor：baseline 登記過的規則／check 函式，一個都不准消失（#613）。

    第 17 條（檔頭對帳）守的是**四處一致**，不是**存在**。把一條 check 的四處一起
    刪掉，四處依然完全一致 ⇒ `--all` 全綠，通過訊息只是少一個名字，沒有任何訊號。
    實測（develop 0c64397c、未加本 floor 時）：刪掉 `conceal-instances` 的四處
    exit=0；刪掉 `repo-lint-docstring-registry` 自己的四處也 exit=0。

    形狀比照 `baselines/seed-topology.txt`：要調降必須顯式跑 `--update-baseline`，
    那個動作會出現在 diff 裡被覆核，靜默少一條不會。

    🔴 判準是 **baseline ⊆ 現況**（不是相等），而且記的是**名字不是數量**：

      * 記名字：純數量的 floor 對「刪掉 A、同時新增 B」完全沉默（總數不變）。記名字
        嚴格更強，而且違規時說得出**是哪一條**不見了——「差幾個」是沒法行動的訊號。
      * 用 ⊆：相等會讓每一次合法新增都被擋到有人跑 `--update-baseline` 為止，而一條
        會擋住正當工作的 gate 活不久（#613 明列「新增一條 check 必須維持綠」）。
      * ⚠ ⊆ 的已知代價：**新增但還沒進 baseline 的規則不受 floor 保護**（它不在
        baseline 裡，刪掉時 floor 沉默）。補法是新增 check 後跑 `--update-baseline`。
        這個缺口有可執行的紀錄，見 test_repo_lint_registry_floor.py 的
        `test_the_known_gap_of_a_subset_floor`——寫成測試而不是散文，才不會過期成假話。

    🔴 fail-loud：baseline 讀不到／是空的／本檔 parse 不了，一律當違規。`⊆` 在空集合
    上恆真，所以「探針壞掉」與「全部通過」的輸出完全相同——那正是本 floor 要防的形態
    換一個載體重演。
    """
    rel = REPO_LINT_REGISTRY_BASELINE_REL
    ref = ("要調降就跑 `%s --update-baseline`——那個動作會出現在 diff 裡被覆核，"
           "靜默少一條不會（#613）" % REPO_LINT_SELF_REL)
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        return ["%s：讀不到 floor baseline。沒有它，「把一條 check 的四處一起刪掉」"
                "就完全沒有訊號——四處仍然一致，檔頭對帳照樣全綠（%s）" % (rel, ref)]
    recorded = set(read_baseline_pairs(path))
    if not recorded:
        return ["%s：floor baseline 裡沒有任何登記。空的 baseline 讓「baseline ⊆ 現況」"
                "恆真（空集合是任何集合的子集），探針失去鑑別力而輸出與「全部通過」"
                "一模一樣（%s）" % (rel, ref)]
    names = repo_lint_registry_names(root)
    if names is None:
        return ["%s：讀不到或 parse 失敗，floor 拿不到現況登記表——解析失敗當違規而不是"
                "無事，否則這條 floor 會靜默恆綠（%s）" % (REPO_LINT_SELF_REL, ref)]
    rules, fns = names
    current = {"rule:" + name for name in rules} | {"fn:" + name for name in fns}
    violations = []
    for entry in sorted(recorded - current):
        kind, _, name = entry.partition(":")
        if kind == "rule":
            violations.append(
                "%s：baseline 登記過規則 `rule:%s`，現在的 `REPO_LINT_RULES` 已經沒有"
                "它了。整條守線被刪掉時四處仍然一致 ⇒ 檔頭對帳全綠、通過訊息只是少一"
                "個名字，沒有任何人會發現（#613）。%s" % (rel, name, ref))
        else:
            violations.append(
                "%s：baseline 登記過 check 函式 `fn:%s`，現在的 run_all() 已經不呼叫"
                "它了——寫好的 gate 連同它的登記一起消失，零訊號（#613）。%s"
                % (rel, name, ref))
    return violations


def check_repo_lint_docstring_registry(root):
    """本檔的檔頭 docstring 與實作必須互為雙射（#598）。

    檔頭是「這支工具到底守了哪些線」的唯一敘述來源，也是 #596 當初能被發現的線索
    來源。在此之前它是純散文：新增 check 卻忘了改檔頭**不會有任何訊號**，同一個
    漂移原樣復發。#596／PR #597 修的是症狀（三處過期內容），這條 check 補的是機制
    ——判準永遠是「改壞它，什麼會變紅？」。

    對帳四件（前三件是 #598 指定的最低要求）：

    1. 數量：檔頭開頭宣告的「N 個 check 函式／M 條規則」要對得上 run_all() 實際
       呼叫的 check_* 個數與檔頭編號條目數。一對多（7. capability-gate 由三個函式
       實作）走 `REPO_LINT_RULES` 這張顯式映射表，**不是**寫死差額。
    2. 編號 1..M 連續且嚴格遞增（#596 的第三處問題就是 11 與 12 顛倒）。
    3. 名稱互為雙射：條目名 ↔ `REPO_LINT_RULES` 的鍵 ↔ run_all() 呼叫的函式名。
       只比數字的話，「數量相等但名字錯配」完全抓不到。
    4. 兩條同型不變式：module 層定義了 check_* 卻沒進 run_all()（gate 寫好了沒人
       跑）、進了 run_all() 卻沒掛 run_hook() 分派（＝#596 本體：CI 會抓、hook 完全
       沉默，而兩者的輸出一模一樣）。後者可用 `REPO_LINT_HOOK_EXEMPT` 具名豁免。

    每一條違規都點名**是哪一個**（哪個函式沒登記／哪個條目沒有實作／哪兩個編號錯位），
    不是「差幾個」——只報數量等於報了一個沒法行動的訊號。

    🔴 fail-loud：讀不到／parse 不了／檔頭抽不到條目，一律當違規。這是 repo-lint
    檢查自己，若解析失敗就 `return []`，它會在檔頭格式一改的當下**永久沉默、而且
    看起來永遠是綠的**——那正是本 check 要防的形態換一個載體重演。（檔頭最後一行的
    「Fail-open」講的是 hook 模式的全域 except：hook 不擋開發，`--all` 必須紅，
    兩者不衝突。）

    ⚠ 覆蓋邊界：只保證名單／編號／數字三者對得上，**不驗證條目的描述文字與該 check
    的實際行為相符**。描述寫錯、pitfall 指針過期、依據文件被刪，本 gate 一個都抓不到
    ——那留給人。把這條綠讀成「檔頭寫的都是真的」就是 #547 的原型錯誤換一個載體。
    """
    ref = REPO_LINT_REGISTRY_REF
    self_rel = REPO_LINT_SELF_REL
    path = os.path.join(root, self_rel)
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError) as exc:
        return ["%s：讀不到或 parse 失敗（%s）——檔頭對帳整條失去鑑別力。解析失敗當"
                "違規而不是無事，否則這條 check 會靜默恆綠（%s）" % (self_rel, exc, ref)]

    doc = ast.get_docstring(tree)
    if not doc:
        return ["%s：module docstring 不見了。它是這支工具唯一的規則敘述來源，刪掉它"
                "等於把「守了哪些線」變回無人知曉（%s）" % (self_rel, ref)]

    first = _REPO_LINT_ENTRY_RE.search(doc)
    if not first:
        return ["%s：檔頭抽不到任何 `N. rule-name` 編號條目——探針失去鑑別力（雙射在"
                "空集合上恆真）。格式是行首 `1. controller-sudo   說明…`、續行縮排"
                "（%s）" % (self_rel, ref)]
    entries = [(int(n), name) for n, name in _REPO_LINT_ENTRY_RE.findall(doc)]
    header = doc[:first.start()]

    top = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    missing_fn = [n for n in ("run_all", "run_hook") if n not in top]
    if missing_fn:
        return ["%s：找不到 %s()——檔頭對帳的實作側探針壞掉，不是通過（%s）"
                % (self_rel, "()／".join(missing_fn), ref)]

    rules = _repo_lint_module_literal(tree, "REPO_LINT_RULES")
    if not isinstance(rules, dict) or not rules:
        return ["%s：讀不到 `REPO_LINT_RULES` 映射表（或它是空的）。那是「規則 ↔ 實作"
                "函式」一對多關係的唯一顯式表達，沒有它就只剩寫死差額常數一途，"
                "第二條一對多規則出現時會靜默算對總數卻對錯人（%s）" % (self_rel, ref)]
    exempt = _repo_lint_module_literal(tree, "REPO_LINT_HOOK_EXEMPT")
    if not isinstance(exempt, dict):
        return ["%s：讀不到 `REPO_LINT_HOOK_EXEMPT`（要是 dict literal）。它是 hook "
                "分派的具名豁免清單，讀不到就無法分辨「刻意不掛」與「漏掛」（%s）"
                % (self_rel, ref)]

    called = _repo_lint_check_calls(top["run_all"])
    called_set = set(called)
    hooked = set(_repo_lint_check_calls(top["run_hook"]))
    defined = {n.name for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name.startswith("check_")}
    doc_names = [name for _, name in entries]
    num_of = {name: num for num, name in entries}
    violations = []

    # (1) 編號 1..N 連續且嚴格遞增。
    nums = [n for n, _ in entries]
    expected = list(range(1, len(entries) + 1))
    if sorted(nums) != expected:
        for n in _dups(nums):
            violations.append(
                "%s：檔頭編號 %d 出現 %d 次（%s）——重號會讓「第幾條」指到兩個地方，"
                "後續全部錯位（%s）"
                % (self_rel, n, nums.count(n),
                   "／".join(nm for k, nm in entries if k == n), ref))
        for n in expected:
            if n not in nums:
                violations.append(
                    "%s：檔頭缺編號 %d（共 %d 條，編號應為 1..%d 連續）——是不是刪掉一"
                    "條卻沒把後面重新編號？（%s）"
                    % (self_rel, n, len(entries), len(entries), ref))
        for n in sorted(set(nums) - set(expected)):
            violations.append(
                "%s：檔頭條目 `%s` 編號 %d 超出總數 %d（%s）"
                % (self_rel, dict(entries).get(n, "?"), n, len(entries), ref))
    elif nums != expected:
        # 集合對但順序不對 ＝ 純置換，逐一點名錯位的那幾條（#596 的 11／12 顛倒）。
        for pos, (n, name) in enumerate(entries, start=1):
            if n != pos:
                violations.append(
                    "%s：檔頭第 %d 個條目 `%s` 的編號寫成 %d——編號必須嚴格遞增"
                    "（兩條顛倒時兩邊都會被點名，#596 的第三處問題就是這個）（%s）"
                    % (self_rel, pos, name, n, ref))

    # (2) 條目名 ↔ REPO_LINT_RULES 的鍵：雙射。
    for name in _dups(doc_names):
        violations.append(
            "%s：檔頭有 %d 條條目都叫 `%s`——名稱是對帳的鍵，重名會讓其中一條無論"
            "怎麼漂移都對得上（%s）" % (self_rel, doc_names.count(name), name, ref))
    for name in sorted(set(doc_names) - set(rules)):
        violations.append(
            "%s：檔頭第 %d 條 `%s` 在 `REPO_LINT_RULES` 裡沒有對應項——條目說有這條"
            "規則，卻查不到是哪個函式實作它（%s）"
            % (self_rel, num_of[name], name, ref))
    for name in sorted(set(rules) - set(doc_names)):
        violations.append(
            "%s：`REPO_LINT_RULES` 登記了 `%s`（%s），檔頭卻沒有這條編號條目——實作"
            "側有守線、敘述側查無此條，讀檔頭的人不會知道它存在（%s）"
            % (self_rel, name, "／".join(rules.get(name) or ("?",)), ref))

    # (2b) 順序也要一致。`REPO_LINT_RULES` 的順序就是 run_all() 通過訊息的順序，
    #      與檔頭編號順序分岔會讓兩份清單看起來像在講不同的東西。上面的註解已經
    #      宣稱「順序即檔頭條目順序」——沒被強制的宣稱遲早變成假話，所以宣稱了就
    #      得有 gate（memory: feedback_comment_env_state_claims_expire）。
    if set(doc_names) == set(rules) and list(rules) != doc_names:
        for pos, (want, got) in enumerate(zip(doc_names, rules), start=1):
            if want != got:
                violations.append(
                    "%s：`REPO_LINT_RULES` 第 %d 項是 `%s`，檔頭第 %d 條卻是 `%s`"
                    "——兩份清單順序不一致（通過訊息由映射表產生，會與檔頭編號對不上）"
                    "（%s）" % (self_rel, pos, got, pos, want, ref))
                break

    # (3) REPO_LINT_RULES 的函式 ↔ run_all() 實際呼叫：雙射。
    mapped, owner = [], {}
    for rule, fns in rules.items():
        if isinstance(fns, str) or not isinstance(fns, (list, tuple)) or not fns:
            violations.append(
                "%s：`REPO_LINT_RULES['%s']` 必須是非空的函式名 tuple（現在是 %r）"
                "——寫成字串會被逐字元拆開，對帳會得出一堆單字母的假違規（%s）"
                % (self_rel, rule, fns, ref))
            continue
        for fn in fns:
            mapped.append(fn)
            owner.setdefault(fn, rule)
    for fn in _dups(mapped):
        violations.append(
            "%s：`%s` 被 %d 條規則同時登記——一個函式只能屬於一條規則，否則函式數與"
            "規則數的差額對不回去（%s）" % (self_rel, fn, mapped.count(fn), ref))
    for fn in sorted(set(mapped) - called_set):
        violations.append(
            "%s：`REPO_LINT_RULES` 說 `%s`（規則 %s）是實作，但 run_all() 沒有呼叫它"
            "——檔頭宣稱守著的線根本沒人跑（%s）"
            % (self_rel, fn, owner.get(fn, "?"), ref))
    for fn in sorted(called_set - set(mapped)):
        violations.append(
            "%s：run_all() 呼叫了 `%s`，但 `REPO_LINT_RULES` 沒有任何一條規則登記它"
            "——新增 check 忘了同步檔頭，正是 #596 的形態（%s）" % (self_rel, fn, ref))
    for fn in _dups(called):
        violations.append(
            "%s：run_all() 呼叫 `%s` %d 次——同一條 check 跑兩遍，違規訊息會重複、"
            "函式計數也會多算（%s）" % (self_rel, fn, called.count(fn), ref))

    # (4a) 定義了卻沒進 run_all()：gate 寫好了沒人跑。
    for fn in sorted(defined - called_set):
        violations.append(
            "%s：module 層定義了 `%s` 卻沒有出現在 run_all()——寫好的 gate 一次都不會"
            "被執行，而輸出與「跑了而且沒事」完全相同（%s）" % (self_rel, fn, ref))
    for fn in sorted(called_set - defined):
        violations.append(
            "%s：run_all() 呼叫 `%s`，但本檔 module 層沒有這個函式定義（%s）"
            % (self_rel, fn, ref))

    # (4b) 進了 run_all() 卻沒掛 run_hook() 分派 ＝ #596 本體。
    for fn in sorted(called_set - hooked - set(exempt)):
        violations.append(
            "%s：`%s` 在 run_all() 裡，卻沒有出現在 run_hook() 的分派——CI 會抓、"
            "hook 模式完全沉默，而「沒違規」與「根本沒跑到」的輸出一模一樣（#596）。"
            "確定不需要 hook 就把它加進 `REPO_LINT_HOOK_EXEMPT` 並寫下理由（%s）"
            % (self_rel, fn, ref))
    for fn in sorted(set(exempt) - called_set):
        violations.append(
            "%s：`REPO_LINT_HOOK_EXEMPT` 列了 `%s`，但 run_all() 根本沒有這條 check"
            "——豁免比它豁免的東西活得久，下一個同名函式會無聲繼承它（%s）"
            % (self_rel, fn, ref))
    for fn in sorted(set(exempt) & hooked):
        violations.append(
            "%s：`%s` 同時列在 `REPO_LINT_HOOK_EXEMPT` 與 run_hook() 分派裡，兩者"
            "互斥——已經掛上了就把豁免刪掉，別讓清單教下一個人錯的事（%s）"
            % (self_rel, fn, ref))

    # (5) 檔頭區的兩個計數宣告。零命中同樣算紅——那個訊號被刪掉了。
    for label, regex, actual, what in (
            ("N 個 check 函式", _REPO_LINT_FUNC_COUNT_RE, len(called_set),
             "run_all() 實際呼叫的 check_* 個數"),
            ("M 條規則", _REPO_LINT_RULE_COUNT_RE, len(entries),
             "檔頭的編號條目數")):
        claims = regex.findall(header)
        if not claims:
            violations.append(
                "%s：檔頭區（第一條編號條目之前）找不到「%s」的宣告。那是讀的人唯一"
                "能自證清單完整的訊號，刪掉它等於把「少了一條」變回無聲（%s）"
                % (self_rel, label, ref))
            continue
        for raw in claims:
            claimed = _cn_int(raw)
            if claimed is None:
                violations.append(
                    "%s：檔頭的「%s」寫成 %r，看不懂（只支援 0–99 的中文數字或阿拉伯"
                    "數字）——看不懂就等於沒有這個訊號（%s）" % (self_rel, label, raw, ref))
            elif claimed != actual:
                violations.append(
                    "%s：檔頭宣告「%s」＝%d，但 %s＝%d。新增／刪除 check 時要一起更新"
                    "這個數字（#596 的第一處問題就是它停在「十五個」而實際 18 個）（%s）"
                    % (self_rel, label, claimed, what, actual, ref))

    # (6) floor 的存在性（#613）。上面五條全部是「四處互相對帳」，對「四處一起消失」
    #     一律沉默——補那個洞的是 `repo_lint_registry_floor()`，而它**不是** check_*，
    #     所以不在任何一組雙射裡。沒有這一條，有人把 floor 從 run_all() 拿掉就會靜默
    #     恆綠，#613 的洞原樣復發。反向（有人刪掉本 check 的四處）由 floor 守：兩者
    #     互相守著對方，刪掉任何一個都會紅。
    for holder in ("run_all", "run_hook"):
        if not _repo_lint_calls_named(top[holder], REPO_LINT_FLOOR_FN):
            violations.append(
                "%s：%s() 沒有呼叫 `%s`——那是登記表的不可下降 floor，拿掉它之後「把一"
                "條 check 的四處一起刪掉」會完全沒有訊號（四處仍然一致，本 check 照樣"
                "全綠）。它與本 check 互相守著對方的存在，刻意不寫成 check_* 以免被"
                "同一組雙射收編（#613）（%s）"
                % (self_rel, holder, REPO_LINT_FLOOR_FN, ref))
    return violations


def run_all(root):
    # 🔴 floor 排在最前面，而且**直接寫在這裡**、不做成第 21 條 check（#613）。
    #    理由是驗收條件本身：「把 check_repo_lint_docstring_registry 自己的四處一起
    #    刪掉必須紅」。任何寫在某條 check 裡的 floor 都會跟著那條 check 一起被刪掉，
    #    然後靜默恆綠——那是本 issue 要修的形態換一個載體重演。run_all() 是這支工具的
    #    入口，刪掉它工具就不存在了；而「run_all() 有沒有呼叫 floor」由第 17 條的
    #    (6) 守。
    violations = (repo_lint_registry_floor(root)
                  + check_controller_sudo(root)
                  + check_owl_template(root)
                  + check_data_noupdate(root)
                  + check_reuse_values(root)
                  + check_seed_topology(root)
                  + check_test_env_restore(root)
                  + check_slug_capability_gate(root)
                  + check_admin_gui_gate_anchor(root)
                  + check_gateway_copy_gate_anchor(root)
                  + check_access_tab_gate_anchor(root)
                  + check_conceal_instances_gate(root)
                  + check_reset_budget_ladder(root)
                  + check_transition_anchor_utc(root)
                  + check_assertlogs_numeric_level(root)
                  + check_mixin_subscription_sudo(root)
                  + check_config_parameter_uninstall(root)
                  + check_duplicate_class_methods(root)
                  + check_pattern_doc_registry(root)
                  + check_availability_reason_labels(root)
                  + check_repo_lint_docstring_registry(root)
                  + check_kubectl_ns_whitelist_sync(root)
                  + check_poll_release_facts_single_source(root)
                  + check_doc_symbol_anchor(root)
                  + check_hermes_agent_013_seed(root)
                  + check_security_rule_noupdate(root)
                  + check_retired_rule_xmlid(root)
                  + check_workspace_access_rule_directions(root)
                  + check_one_time_credential_render(root)
                  + check_workspace_rule_directions(root)
                  + check_provision_sentinel_allowlist(root)
                  + check_sh_subscription_group_isolation(root)
                  + check_internal_ctx_key_registry(root)
                  + check_access_gate_no_ledger_read(root)
                  + check_tunnel_config_wire_shape(root))
    if violations:
        print("repo-lint：%d 個違規" % len(violations))
        for v in violations:
            print("  ✗ " + v)
        return 1
    # 規則清單由 REPO_LINT_RULES 產生，不手抄。手抄的那版活到最後是這樣的：它列了
    # 17 個名字（16 條規則＋誤列的 gateway-copy-gate），而同一個檔的檔頭寫「十八個
    # check 函式、十六條規則」——同一支工具三個數字。少一份清單就少一處會漂的散文。
    print("repo-lint：全部通過（%s）" % " / ".join(REPO_LINT_RULES))
    return 0


def update_baseline(root):
    os.makedirs(BASELINE_DIR, exist_ok=True)
    counts = current_sudo_counts(root)
    with open(SUDO_BASELINE, "w", encoding="utf-8") as f:
        f.write("# controller .sudo( 呼叫點數量上限（repo-lint.py --update-baseline 產生）\n")
        f.write("# 調升前必讀 %s\n" % SUDO_DOC)
        for rel, count in sorted(counts.items()):
            f.write("%d\t%s\n" % (count, rel))
    files = current_noupdate_files(root)
    with open(NOUPDATE_BASELINE, "w", encoding="utf-8") as f:
        f.write("# 允許 noupdate=\"1\" 的 data 檔（repo-lint.py --update-baseline 產生）\n")
        f.write("# 新增前必讀 %s\n" % NOUPDATE_DOC)
        for rel in files:
            f.write("1\t%s\n" % rel)
    topology = seed_topology_counts(root)
    with open(SEED_TOPOLOGY_BASELINE, "w", encoding="utf-8") as f:
        f.write("# seed 拓撲計數（repo-lint.py --update-baseline 產生）\n")
        f.write("# 純靜態 parse src/data/subscription_{products,template_links}*.xml，"
                "與 DB 無關。\n")
        f.write("# 這裡的數字變動時，check_seed_topology 會列出所有該一起檢視的位置。"
                "調整前必讀 %s\n" % SEED_TOPOLOGY_DOC)
        for key, value in sorted(topology.items()):
            f.write("%d\t%s\n" % (value, key))
    names = repo_lint_registry_names(root)
    if names is None:
        print("✗ 讀不到 %s 的登記表（parse 失敗？）——floor baseline 不更新。"
              "把讀不到寫成「產生一份空的」等於一鍵關掉這條守線（#613）"
              % REPO_LINT_SELF_REL)
        return 1
    registry = ["rule:" + name for name in names[0]] + ["fn:" + name for name in names[1]]
    with open(REPO_LINT_REGISTRY_BASELINE, "w", encoding="utf-8") as f:
        f.write("# repo-lint 登記表的不可下降 floor（repo-lint.py --update-baseline 產生）\n")
        f.write("# 這份清單**只准長不准縮**：少了任何一個名字，run_all() 會點名是哪一個"
                "不見了。\n")
        f.write("# 整條守線被刪掉時「四處」仍然一致 ⇒ 第 17 條檔頭對帳全綠，只有這份 "
                "floor 會紅（#613）。\n")
        f.write("# 左欄的 1 沒有意義（沿用 read_baseline_pairs 的 value<TAB>key 格式），"
                "判準是右欄名字的集合。\n")
        for entry in sorted(registry):
            f.write("1\t%s\n" % entry)
    print("baseline 已更新：%s（%d 檔）、%s（%d 檔）、%s（%d 個計數）、%s（%d 個名字）"
          % (os.path.relpath(SUDO_BASELINE), len(counts),
             os.path.relpath(NOUPDATE_BASELINE), len(files),
             os.path.relpath(SEED_TOPOLOGY_BASELINE), len(topology),
             os.path.relpath(REPO_LINT_REGISTRY_BASELINE), len(registry)))
    return 0


def run_hook():
    try:
        payload = json.load(sys.stdin)
        file_path = (payload.get("tool_input") or {}).get("file_path") or ""
        if not file_path:
            return 0
        root = repo_root()
        rel = os.path.relpath(os.path.abspath(file_path), os.path.abspath(root)).replace(os.sep, "/")
        if rel.startswith(".."):
            return 0
        abs_path = os.path.abspath(file_path)
        # #461 預算階梯是**跨檔**不變式（前端 JS ／ 平台 client ／ operator 常數各在
        # 一處），所以只要動到其中任一檔就整條重算，不做 only_file 切片。
        ladder = (check_reset_budget_ladder(root)
                  if rel in RESET_LADDER_FILES else [])
        # #552／#596：availability reason 的涵蓋同樣是**跨檔**不變式（後端 judge ／
        # 前端 REASON_LABELS），壞掉的是另一個檔 ⇒ 一樣整條重算、不做 only_file 切片。
        availability = (check_availability_reason_labels(root)
                        if rel in AVAILABILITY_REASON_FILES else [])
        # #627：poll 的兩份實作分別在 model 與 controller，欄位集合是**跨檔**不變式
        # ⇒ 動到任一份就整條重算，不做 only_file 切片（同 ladder／availability）。
        poll_facts = (check_poll_release_facts_single_source(root)
                      if rel in POLL_RELEASE_FACTS_FILES else [])
        # #626：doc 的符號錨點同樣是**跨檔**不變式，而且兩端都會壞——改 doc（寫錯符號
        # 名）與改 code（把被引用的符號改名／刪掉）都讓它失效，所以兩類輸入都要接住，
        # 一樣整條重算。分派條件寫成函式而不是 rel 前綴列表：被引用的 code 同時住在
        # src/** 與 extra/paas-operator/**，而 docs/tracking/pitfalls/** 底下**沒有**
        # 任何既有分支會接到（漏掛的話 CI 會抓、hook 完全沉默＝#596 的形態本身）。
        anchors = (check_doc_symbol_anchor(root)
                   if _doc_symbol_anchor_watches(rel) else [])
        # #613：floor 的輸入是**兩份**（本檔的登記表 ＋ baseline 檔），而壞掉的常常是
        # 另一份——有人「調降」baseline，或有人刪掉一條 check 的四處。跨檔不變式，
        # 一樣整條重算、不做 only_file 切片（同 ladder／availability／poll_facts）。
        floor = (repo_lint_registry_floor(root)
                 if rel in REPO_LINT_REGISTRY_FILES else [])
        # #659：三條 ir.rule 守線的輸入是**module 的全部非 static XML**（record 可以
        # 宣告在任何 data 檔），而且第三條本身是跨檔存在性判準 ⇒ 不做 only_file 切片，
        # 動到任一份就整條重算（同 ladder／availability／poll_facts／floor）。
        # 分派條件與掃描範圍共用 `_security_rule_watches`／`_security_rule_xml_files`
        # 的同一個判準，兩者不可能分岔。src/security/*.xml 不在下面任何分支底下
        # （只有 src/static 與 src/data 的 .xml 有分支），漏接就會是「CI 會抓、hook
        # 完全沉默」的 #596 形態。
        security_rules = (check_security_rule_noupdate(root)
                          + check_retired_rule_xmlid(root)
                          + check_workspace_access_rule_directions(root)
                          + check_workspace_rule_directions(root)
                          if _security_rule_watches(rel) else [])
        # #672：一次性憑證的涵蓋也是**跨檔**不變式（後端 payload ／ 前端組裝點），
        # 而且壞掉的常常是另一個檔——後端補一項憑證、前端忘了讀，紅的是前端那份。
        # ⇒ 不做 only_file 切片，動到任一份就整條重算（同 ladder／availability）。
        one_time_cred = (check_one_time_credential_render(root)
                         if rel in ONE_TIME_CRED_FILES else [])
        # #519：開閘 allowlist 是**跨檔**不變式（allowlist ↔ 符號實際出現的位置），
        # 而且它的第二個判準——allowlist 有沒有養殭屍條目——只有全 repo 掃得出來
        # ⇒ 不做 only_file 切片，動到任何一個受監看的檔就整條重算。
        provision = (check_provision_sentinel_allowlist(root)
                     if _provision_sentinel_watches(rel) else [])
        # #519 提案 B：判準是「全 repo 都不准出現那兩個群組名」＋一個存在性對照，
        # 兩者都要全 repo 掃才成立 ⇒ 不做 only_file 切片。
        sh_sub_groups = (check_sh_subscription_group_isolation(root)
                         if rel.startswith("src/")
                         and rel.endswith((".xml", ".csv", ".py")) else [])
        # #788：不變式跨「registry（住在本檔）↔ 全部生產碼」兩端，而且壞掉的常常是
        # 另一端——新寫一把未登記的 truthy 閘紅在 src/，把 sentinel 改名紅在 registry
        # 的陽性對照。涵蓋率判準本身也只有全樹掃得出來 ⇒ 不做 only_file 切片，動到
        # 任一端就整條重算（同 provision／sh_sub_groups）。
        internal_ctx = (check_internal_ctx_key_registry(root)
                        if _internal_ctx_key_watches(rel) else [])
        # #769：不變式跨「operator endpoint ↔ 平台 client」兩端，而且壞掉的常常是
        # 另一端（改 client 的送法紅在 operator 的宣告，反之亦然）；判準本身也只有
        # 同時讀兩端才成立 ⇒ 不做 only_file 切片，動到任一端就整條重算。
        tunnel_wire = (check_tunnel_config_wire_shape(root)
                       if _tunnel_config_wire_watches(rel) else [])
        if re.match(r"^src/controllers/.*\.py$", rel):
            violations = check_controller_sudo(root, only_file=abs_path)
            violations += check_reuse_values(root, only_file=abs_path)
            violations += check_conceal_instances_gate(root, only_file=abs_path)
            violations += check_transition_anchor_utc(root, only_file=abs_path)
            violations += check_duplicate_class_methods(root, only_file=abs_path)
        elif re.match(r"^src/.*\.py$", rel):
            violations = check_reuse_values(root, only_file=abs_path)
            # #784 F6：判準完全落在單一檔案內（filtered 的 lambda ↔ 同檔方法
            # 定義）⇒ 可以只切這一檔重算，不必全樹掃。
            violations += check_access_gate_no_ledger_read(
                root, only_file=abs_path)
            violations += check_conceal_instances_gate(root, only_file=abs_path)
            violations += check_transition_anchor_utc(root, only_file=abs_path)
            violations += check_mixin_subscription_sudo(root, only_file=abs_path)
            violations += check_config_parameter_uninstall(root, only_file=abs_path)
            violations += check_duplicate_class_methods(root, only_file=abs_path)
            if rel.startswith("src/tests/"):
                violations += check_test_env_restore(root, only_file=abs_path)
                violations += check_assertlogs_numeric_level(root, only_file=abs_path)
        elif re.match(r"^src/static/src/.*\.xml$", rel):
            violations = check_owl_template(root, only_file=abs_path)
            violations += check_slug_capability_gate(root, only_file=abs_path)
            violations += check_admin_gui_gate_anchor(root)
            violations += check_gateway_copy_gate_anchor(root)
            violations += check_access_tab_gate_anchor(root)
        elif re.match(r"^src/static/src/.*\.js$", rel):
            # anchor 走全域：改 JS（getter 被刪／改名／改判準）壞掉的是 XML 那顆按鈕。
            violations = check_slug_capability_gate(root, only_file=abs_path)
            violations += check_admin_gui_gate_anchor(root)
            violations += check_gateway_copy_gate_anchor(root)
            violations += check_access_tab_gate_anchor(root)
        elif re.match(r"^src/data/.*\.xml$", rel):
            violations = check_data_noupdate(root, only_file=abs_path)
            if re.match(r"^src/data/subscription_(products|template_links).*\.xml$", rel):
                violations += check_seed_topology(root)
            # hermes-agent 0.1.3 seed 形態：不分 only_file，因為對 hermes-agent
            # record 來說，**唯一**的 carrier 是 src/data/cloud_app_templates.xml，
            # 編到該檔就是改它；其他 src/data/*.xml 動到時也順便跑（cheap parse，
            # 找不到 hermes-agent record 就跳過）
            violations += check_hermes_agent_013_seed(root)
        elif re.match(r"^(docs/decisions/patterns/|\.claude/hooks/)", rel):
            # #547：不變式跨「目錄／map／README 索引」三方，壞掉的是**另外兩份名單**
            # 而不是你剛編輯的那個檔 ⇒ 不做 only_file 切片，整條重算
            # （同 check_admin_gui_gate_anchor 的形狀）。
            violations = check_pattern_doc_registry(root)
        elif rel == REPO_LINT_SELF_REL:
            # #598：本 check 的輸入就是這個檔自己（檔頭 ↔ REPO_LINT_RULES ↔ run_all
            # ↔ run_hook），沒有 only_file 切片可言——編輯者改的就是被檢查的那一份。
            # 🔴 漏掛這一支就是 #596 的形態本身：CI 會抓、hook 完全沉默，而「沒違規」
            # 與「根本沒跑到」的輸出一模一樣。check 自己會對帳這件事（4b），所以下一個
            # 人漏掛時會在 CI 被點名，而不是靠誰記得。
            violations = check_repo_lint_docstring_registry(root)
        elif rel in (KUBECTL_NS_CLAUDE_MD, KUBECTL_NS_GUARD_DOC):
            # 白名單的兩份副本互為對方的判準：改哪一邊，壞掉的都是「兩份是否一致」這個
            # 跨檔不變式本身 ⇒ 不做 only_file 切片，動到任一份就整條重算。
            violations = check_kubectl_ns_whitelist_sync(root)
        elif (ladder or availability or poll_facts or anchors or floor
                or security_rules or one_time_cred or provision or sh_sub_groups
                or internal_ctx or tunnel_wire):
            # extra/paas-operator/** 與 docs/tracking/pitfalls/** 不在上面任何分支，
            # 但前者持有階梯的內層常數、後者是 doc-symbol-anchor 的一半輸入；
            # baselines/repo-lint-registry.txt（floor 的另一份輸入）與
            # src/security/*.xml（三條 ir.rule 守線的主要 carrier）同樣不在上面任何
            # 分支。跨檔集合搬到不匹配任何分支的位置時，這裡也接得住。
            violations = []
        else:
            return 0
        violations += ladder
        violations += availability
        violations += poll_facts
        violations += anchors
        violations += floor
        violations += security_rules
        violations += one_time_cred
        violations += provision
        violations += sh_sub_groups
        violations += internal_ctx
        violations += tunnel_wire
        if violations:
            print(json.dumps({
                "decision": "block",
                "reason": "[repo-lint] 剛才的編輯觸發假綠 gate，請立即修正：\n"
                          + "\n".join("✗ " + v for v in violations),
            }))
        return 0
    except Exception:
        return 0  # fail-open：hook 模式絕不擋開發



# --------------------------------------------------------------------------- #
#  29. access-gate-no-ledger-read                                              #
# --------------------------------------------------------------------------- #

#: 待回收帳本（`cf_access_policy_reap`）的查詢入口。判準寫「性質」不寫「哪個
#: 方法叫這個名字」：這三個是**唯一**能讓一段程式碼看到帳本內容的符號，所以
#: 「有沒有讀帳本」等價於「有沒有出現這三個之一」。
_LEDGER_QUERY_SYMBOLS = (
    "_pending_ids",
    "_pending_for",
    "_access_teardown_payload_ids",
)


def _ledger_symbols_in(node):
    """`node` 子樹裡出現的帳本查詢符號（去重、保序無所謂）。"""
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in _LEDGER_QUERY_SYMBOLS:
            found.add(sub.attr)
        elif isinstance(sub, ast.Name) and sub.id in _LEDGER_QUERY_SYMBOLS:
            found.add(sub.id)
    return found


def check_access_gate_no_ledger_read(root, only_file=None):
    """`filtered()` 的判準（以及它一層內呼叫到的方法）不得讀待回收帳本。

    #784 F6 的形狀：`_access_teardown_ids()` 為了「刪除時順手清舊帳」聯集了
    `_pending_ids()`，而它同時被兩個 **gate** 用著——`_cleanup_cloudflare` 挑
    route 的 filter、`_do_cleanup` 的「清乾淨了沒」閘。後果是只要 operator 回報
    過某顆 policy 刪不掉，那筆帳就掛在帳本上（錨點還活著 ⇒ dead-anchor cron 依
    設計不收）⇒ 閘對該 route 永遠回 True ⇒ **SA 永久卡在 `state='error'`**，每輪
    lifecycle cron 白打一次 operator，七天後只多一行 stuck warning，無解。

    修法是把「這列乾淨了沒」與「這趟送哪些 id」拆成兩個方法。但那只是**命名**
    紀律：兩個名字相近、語意相反、沒有型別區隔，下一個新增呼叫端的人一樣會選
    錯，而最危險的那格誤用是**靜默**的——把 filter 換成 payload 版會選中每一條
    route，然後因為 `_teardown_route_access()` 自己的「沒 id 就回 True」早退而
    完全沒有輸出：不打 operator、不寫欄位、不留 log。所以要一條機械 gate。

    判準（刻意不硬編任何方法名）：

    1. 任何 `X.filtered(lambda ...)` 的 lambda **本體**不得出現帳本查詢符號；
    2. 那個 lambda 在**同一個檔案內**呼叫到的方法，其定義也不得出現。

    ⚠ **已知限制 1**：第 2 點只追一層，而且只追同檔定義的方法。一個「gate →
    helper → helper 讀帳本」的三層寫法抓不到；跨檔（把 gate 抽到別的 model）也
    抓不到。這是刻意的成本取捨——再深就要做跨檔呼叫圖，而目前這兩個 gate 都是
    「lambda 直接呼叫同檔方法」的形狀。真要繞過它得刻意寫成三層，那已經不是
    「順手選錯名字」而是另一種行為了。這條限制寫在這裡，不要靠讀者自己發現。

    ⚠ **已知限制 2 —— 同名碰撞（本規則最容易靜默失效的一格）**：`filtered()`
    的 lambda 參數是**另一個 model** 的記錄（`self.route_ids.filtered(lambda r:
    r.foo())` 裡的 `r` 是 route，不是外層那個 class），所以「`r.foo` 指的是哪個
    class 的 `foo`」靜態上**無法可靠決定**——外層 class 不是正確的 scope。

    第一版把同檔所有定義攤平成 `{名字: 節點}`，同名的後者覆蓋前者。那有**兩個
    方向**的失效，而且行為取決於 `ast.walk()` 的走訪順序這個實作細節：

    * **假陰性**（更嚴重）：同檔兩個 `_has_access_teardown()`、只有其中一個讀
      帳本 ⇒ 解析到乾淨那版 ⇒ **F6 的實際形狀漏網**，正是本規則存在的理由；
    * **假陽性**：兩個不相關 class 各有一個 `to_dict()`、其中一個讀帳本 ⇒ 完全
      不相干的 `filtered(lambda r: r.to_dict())` 被冤枉。

    ⚠ 這不是假想：`src/models/security_access.py` 現在就有 5 個同檔重名
    （`__repr__` / `create` / `write` / `unlink` / `to_dict`），只是目前沒有一個
    被用在 filter 判準裡。任何一次讓同名方法出現在同一檔案的重構——哪怕與
    Access／帳本完全無關——都會踩到。

    所以本規則**不猜**：名字在同檔解出**唯一**定義時才追進去；解出兩個以上時
    直接報「無法判定」讓人來拆（訊息與「確定讀了帳本」那條刻意不同字樣，看得出
    是哪一種）。代價是「同一檔案裡有兩個同名方法、而且其中一個被當 filter 判準」
    會被硬擋下來——那個組合很罕見，而且擋下來要求的正是「把名字弄清楚」。
    """
    violations = []
    for path in sorted(glob.glob(os.path.join(root, "src", "**", "*.py"),
                                 recursive=True)):
        if only_file and os.path.abspath(path) != os.path.abspath(only_file):
            continue
        rel = os.path.relpath(path, root)
        with open(path, encoding="utf-8", errors="replace") as f:
            source = f.read()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        # 🔴 收成 name -> [節點]，**不是** name -> 節點：同名時後者覆蓋前者會讓
        # 解析結果取決於 ast.walk() 的走訪順序（見 docstring 已知限制 2）。
        defs = {}
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(n.name, []).append(n)
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "filtered"):
                continue
            for arg in call.args:
                if not isinstance(arg, ast.Lambda):
                    continue
                line = getattr(arg, "lineno", getattr(call, "lineno", 0))
                direct = _ledger_symbols_in(arg.body)
                if direct:
                    violations.append(
                        "%s:%d：`filtered()` 的判準直接讀待回收帳本（%s）。"
                        "filter 是 **gate**，gate 讀帳本會讓一筆永遠收不掉的帳把"
                        "判準釘死成永遠為真（#784 F6：SA 永久卡 state='error'）。"
                        "要送出去的 id 用 payload 版、判準只看列狀態。"
                        % (rel, line, ", ".join(sorted(direct))))
                for sub in ast.walk(arg.body):
                    if not (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)):
                        continue
                    targets = defs.get(sub.func.attr)
                    if not targets:
                        continue
                    if len(targets) > 1:
                        # 同名碰撞 ⇒ 不猜。挑一個會在兩個方向上都靜默出錯，而
                        # 挑哪一個取決於走訪順序（docstring 已知限制 2）。
                        violations.append(
                            "%s:%d：`filtered()` 的判準呼叫 `%s()`，但同一個檔案裡"
                            "有 %d 個同名定義（%s），無法判定它解到哪一個 ⇒ **無法"
                            "判定這個判準有沒有讀待回收帳本**。請把名字弄清楚（改名"
                            "或把判準換成不會撞名的方法）——本規則刻意不猜：猜錯的"
                            "兩個方向分別是「F6 形狀漏網」與「無辜的 filter 被冤枉」，"
                            "而且都是靜默的。"
                            % (rel, line, sub.func.attr, len(targets),
                               ", ".join("%s:%d" % (rel, t.lineno)
                                         for t in targets)))
                        continue
                    target = targets[0]
                    hits = _ledger_symbols_in(target)
                    if not hits:
                        continue
                    violations.append(
                        "%s:%d：`filtered()` 的判準呼叫 `%s()`，而它讀待回收帳本"
                        "（%s，定義在 %s:%d）。gate 讀帳本會讓一筆永遠收不掉的帳"
                        "把判準釘死成永遠為真（#784 F6：SA 永久卡 state='error'）。"
                        % (rel, line, sub.func.attr, ", ".join(sorted(hits)),
                           rel, target.lineno))
    return violations

# --------------------------------------------------------------------------- #
#  30. tunnel-config-wire-shape                                                #
# --------------------------------------------------------------------------- #

#: 這條 wire 的三份輸入。兩端住在**不同的部署單元**：operator 是獨立服務（自己的
#: `extra/paas-operator/Dockerfile`、自己的 CI），平台 addon 打包的只有 `src/`
#: （`Dockerfile.dev` 是 `COPY src/ /mnt/extra-addons/woow_paas_platform/`）。
#: ⇒ **沒有任何一個執行環境同時看得到兩端**：operator 的 pytest 看不到 client，
#: Odoo 的測試看不到 operator。repo-lint 跑在 repo 上，是唯一兩端都在的地方。
TUNNEL_CONFIG_OPERATOR_REL = "extra/paas-operator/src/api/tunnels.py"
TUNNEL_CONFIG_SCHEMAS_REL = "extra/paas-operator/src/models/schemas.py"
TUNNEL_CONFIG_CLIENT_REL = "src/services/paas_operator.py"
#: 判準寫「資源」不寫「函式名」：`update_tunnel_config` 在 operator 內有**兩個同名
#: 定義**——API handler（api/tunnels.py）與 CloudflareService 的方法
#: （services/cloudflare.py）。照名字找會挑到錯的那一個，所以入口是 route 裝飾器。
TUNNEL_CONFIG_ROUTE_METHOD = "put"
TUNNEL_CONFIG_ROUTE_PATH = "/{tunnel_id}/config"
TUNNEL_CONFIG_CLIENT_METHOD = "configure_tunnel"
TUNNEL_CONFIG_FIELDS = ("hostname", "service_url")
TUNNEL_CONFIG_REF = "見 #769（實跑取證：JSON body ⇒ 422，loc=['query',...]）"

#: FastAPI 的預設綁定規則：非 path 的**裸純量**參數 ⇒ query param。
_TUNNEL_WIRE_SCALARS = {"str", "int", "float", "bool"}
_TUNNEL_WIRE_MARKERS = {"Body", "Query", "Path", "Header", "Cookie", "Form", "File"}


def _tunnel_config_wire_watches(rel):
    """hook 分派判準：與 check 的輸入清單共用同一組常數，兩者不可能分岔。"""
    return rel in (TUNNEL_CONFIG_OPERATOR_REL, TUNNEL_CONFIG_SCHEMAS_REL,
                   TUNNEL_CONFIG_CLIENT_REL, REPO_LINT_SELF_REL)


def _tunnel_wire_name(node):
    """`Name` / `Attribute` 的尾端名字；其他回 None。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _tunnel_wire_marker(node):
    """`Body(...)` / `Query(...)` 這類 FastAPI 明示 marker 的名字。"""
    if isinstance(node, ast.Call):
        name = _tunnel_wire_name(node.func)
        if name in _TUNNEL_WIRE_MARKERS:
            return name
    return None


def _tunnel_wire_subscript_elts(node):
    sl = node.slice
    return list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]


def _tunnel_wire_ann_marker(ann):
    """`Annotated[X, Body()]` 裡的 marker（沒有回 None）。"""
    if isinstance(ann, ast.Subscript) and _tunnel_wire_name(ann.value) == "Annotated":
        for extra in _tunnel_wire_subscript_elts(ann)[1:]:
            marker = _tunnel_wire_marker(extra)
            if marker:
                return marker
    return None


def _tunnel_wire_ann_base(ann):
    """剝掉 `Annotated` / `Optional` / `Union` 後最內層的型別名。"""
    if ann is None:
        return None
    if isinstance(ann, ast.Subscript):
        outer = _tunnel_wire_name(ann.value)
        elts = _tunnel_wire_subscript_elts(ann)
        if outer == "Annotated":
            return _tunnel_wire_ann_base(elts[0])
        if outer in ("Optional", "Union"):
            for elt in elts:
                base = _tunnel_wire_ann_base(elt)
                if base not in (None, "None"):
                    return base
            return None
        return outer
    return _tunnel_wire_name(ann)


def _tunnel_wire_defaults(func):
    """{參數名: default 節點}。posonly+args 的 default 靠尾端對齊，kwonly 各自對位。"""
    positional = list(func.args.posonlyargs) + list(func.args.args)
    out = {}
    defaults = list(func.args.defaults)
    if defaults:
        for arg, default in zip(positional[-len(defaults):], defaults):
            out[arg.arg] = default
    for arg, default in zip(func.args.kwonlyargs, func.args.kw_defaults):
        if default is not None:
            out[arg.arg] = default
    return out


def _tunnel_wire_schema_models(source):
    """schemas.py 的 `BaseModel` 子類 → 它宣告的欄位名集合。"""
    models = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(_tunnel_wire_name(base) == "BaseModel" for base in node.bases):
            continue
        models[node.name] = {
            stmt.target.id
            for stmt in node.body
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        }
    return models


def _tunnel_wire_endpoint_binding(operator_source, schema_models):
    """operator 端：{欄位名: 'path'|'query'|'body'|'unknown'} ＋ problems。

    problems 非空一律當違規——本條的判準是否定性的（「hostname 不在 query」），
    解析不出來時「沒有違規」與「有違規但看不到」的輸出完全相同。
    """
    problems = []
    tree = ast.parse(operator_source)

    func = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            target = dec.func
            if not (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "router"
                    and target.attr == TUNNEL_CONFIG_ROUTE_METHOD):
                continue
            if (dec.args and isinstance(dec.args[0], ast.Constant)
                    and dec.args[0].value == TUNNEL_CONFIG_ROUTE_PATH):
                func = node
    if func is None:
        problems.append(
            "找不到被 `@router.%s(%r)` 裝飾的 handler"
            % (TUNNEL_CONFIG_ROUTE_METHOD, TUNNEL_CONFIG_ROUTE_PATH))
        return {}, problems

    path_params = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
                                 TUNNEL_CONFIG_ROUTE_PATH))
    defaults = _tunnel_wire_defaults(func)
    binding = {}
    for arg in (list(func.args.posonlyargs) + list(func.args.args)
                + list(func.args.kwonlyargs)):
        name = arg.arg
        if name in path_params:
            binding[name] = "path"
            continue
        marker = (_tunnel_wire_marker(defaults.get(name))
                  or _tunnel_wire_ann_marker(arg.annotation))
        if marker == "Body":
            binding[name] = "body"
            continue
        if marker == "Query":
            binding[name] = "query"
            continue
        base = _tunnel_wire_ann_base(arg.annotation)
        if base in schema_models:
            # request model ⇒ 它宣告的每個欄位都住在 body
            for field in schema_models[base]:
                binding[field] = "body"
            continue
        if base in _TUNNEL_WIRE_SCALARS:
            binding[name] = "query"      # FastAPI 對裸純量的預設綁定
            continue
        binding[name] = "unknown"
    return binding, problems


def _tunnel_wire_client_binding(client_source):
    """平台 client 端：{欄位名: 'body'|'query'} ＋ problems。

    `_request(..., data=X)` → JSON body（client 內 `json=data`）；
    `_request(..., params=X)` → query string。
    """
    problems = []
    tree = ast.parse(client_source)

    funcs = [node for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name == TUNNEL_CONFIG_CLIENT_METHOD]
    if len(funcs) != 1:
        problems.append(
            "client 方法 `%s` 找到 %d 個定義（預期剛好 1 個）——同名碰撞時挑哪一個"
            "取決於走訪順序，兩個方向都是靜默出錯"
            % (TUNNEL_CONFIG_CLIENT_METHOD, len(funcs)))
        return {}, problems
    func = funcs[0]

    local_dicts = {}
    for stmt in ast.walk(func):
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = (stmt.targets if isinstance(stmt, ast.Assign)
                       else [stmt.target])
            if isinstance(stmt.value, ast.Dict):
                keys = {k.value for k in stmt.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                for target in targets:
                    if isinstance(target, ast.Name):
                        local_dicts[target.id] = set(keys)
            for target in targets:
                # `data['x'] = ...` 這種事後追加也算進去
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)):
                    local_dicts.setdefault(target.value.id, set()).add(
                        target.slice.value)

    binding = {}
    calls = 0
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call)
                and _tunnel_wire_name(node.func) == "_request"):
            continue
        calls += 1
        for kw in node.keywords:
            where = {"data": "body", "params": "query"}.get(kw.arg)
            if where is None:
                continue
            if isinstance(kw.value, ast.Dict):
                keys = {k.value for k in kw.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            elif isinstance(kw.value, ast.Name):
                keys = local_dicts.get(kw.value.id)
            else:
                keys = None
            if keys is None:
                problems.append(
                    "`%s` 的 `%s=` 解不出欄位名（%s）——本條對這個方向失去鑑別力"
                    % (TUNNEL_CONFIG_CLIENT_METHOD, kw.arg,
                       ast.dump(kw.value)[:80]))
                continue
            for key in keys:
                binding[key] = where
    if calls == 0:
        problems.append(
            "`%s` 裡找不到 `self._request(...)` 呼叫——client 已經不從這裡出海，"
            "本條掃的是一個空殼" % TUNNEL_CONFIG_CLIENT_METHOD)
    return binding, problems


#: (b) 分類器自檢的兩份合成輸入。兩者只差「裸 str」與「request model」。
_TUNNEL_WIRE_SELFTEST_SCHEMAS = {"_SelfTestRequest": {"hostname", "service_url"}}
_TUNNEL_WIRE_SELFTEST_QUERY_SRC = (
    "router = APIRouter(prefix='/api/tunnels')\n"
    "@router.put('/{tunnel_id}/config')\n"
    "async def h(tunnel_id: str, hostname: str, service_url: str):\n"
    "    pass\n"
)
_TUNNEL_WIRE_SELFTEST_BODY_SRC = (
    "router = APIRouter(prefix='/api/tunnels')\n"
    "@router.put('/{tunnel_id}/config')\n"
    "async def h(tunnel_id: str, request: _SelfTestRequest):\n"
    "    pass\n"
)
#: (c) client 解析自檢：同一個 dict，一個走 data= 一個走 params=。
_TUNNEL_WIRE_SELFTEST_CLIENT_BODY_SRC = (
    "def configure_tunnel(self, tunnel_id, hostname, service_url):\n"
    "    data = {'hostname': hostname, 'service_url': service_url}\n"
    "    return self._request('PUT', f'/x/{tunnel_id}/config', data=data)\n"
)
_TUNNEL_WIRE_SELFTEST_CLIENT_QUERY_SRC = (
    "def configure_tunnel(self, tunnel_id, hostname, service_url):\n"
    "    data = {'hostname': hostname, 'service_url': service_url}\n"
    "    return self._request('PUT', f'/x/{tunnel_id}/config', params=data)\n"
)


def _tunnel_wire_selftest():
    """分類器自檢。壞掉的分類器會讓下面的跨層比對無聲失去鑑別力。"""
    problems = []

    query_binding, query_problems = _tunnel_wire_endpoint_binding(
        _TUNNEL_WIRE_SELFTEST_QUERY_SRC, _TUNNEL_WIRE_SELFTEST_SCHEMAS)
    body_binding, body_problems = _tunnel_wire_endpoint_binding(
        _TUNNEL_WIRE_SELFTEST_BODY_SRC, _TUNNEL_WIRE_SELFTEST_SCHEMAS)
    for label, errs in (("裸 str", query_problems), ("request model", body_problems)):
        for err in errs:
            problems.append("endpoint 分類器對合成輸入（%s）解析失敗：%s" % (label, err))
    for field in TUNNEL_CONFIG_FIELDS:
        if query_binding.get(field) != "query":
            problems.append(
                "endpoint 分類器把合成的**裸 str** `%s` 判成 %r（應為 'query'）"
                "——它分不出破裂形態，本條的綠燈是假的"
                % (field, query_binding.get(field)))
        if body_binding.get(field) != "body":
            problems.append(
                "endpoint 分類器把合成的 **request model** 欄位 `%s` 判成 %r"
                "（應為 'body'）——它分不出修好的形態，本條會恆紅或恆綠"
                % (field, body_binding.get(field)))

    client_body, client_body_problems = _tunnel_wire_client_binding(
        _TUNNEL_WIRE_SELFTEST_CLIENT_BODY_SRC)
    client_query, client_query_problems = _tunnel_wire_client_binding(
        _TUNNEL_WIRE_SELFTEST_CLIENT_QUERY_SRC)
    for label, errs in (("data=", client_body_problems),
                        ("params=", client_query_problems)):
        for err in errs:
            problems.append("client 解析器對合成輸入（%s）解析失敗：%s" % (label, err))
    for field in TUNNEL_CONFIG_FIELDS:
        if client_body.get(field) != "body":
            problems.append(
                "client 解析器把合成的 `data=` 欄位 `%s` 判成 %r（應為 'body'）"
                % (field, client_body.get(field)))
        if client_query.get(field) != "query":
            problems.append(
                "client 解析器把合成的 `params=` 欄位 `%s` 判成 %r（應為 'query'）"
                % (field, client_query.get(field)))
    return problems


def check_tunnel_config_wire_shape(root):
    """#769：`PUT /api/tunnels/{tunnel_id}/config` 兩端的參數綁定形狀必須一致。

    守的 bug class：**跨部署單元的 wire 契約，兩端各自的測試都蓋不到**。
    operator 端把 `hostname` / `service_url` 宣告成裸 `str` ⇒ FastAPI 綁成 query
    param；平台 client 把同兩個欄位組成 dict 走 JSON body 送 ⇒ **每一次呼叫必回
    422**（實跑取證：`loc` 是 `["query","hostname"]` / `["query","service_url"]`）。
    而兩端的測試在破裂期間**全部是綠的**：operator 的 `tests/` 從來沒有一支打過
    這個端點；平台的 `test_subscription_lifecycle.py` 用 `MagicMock()` 當 client，
    HTTP 序列化整層被 mock 掉。唯一的受害者是 SmartHome 的停權／復權，而它的例外
    被 `except Exception` 吞成 `state='error'` ⇒ 從 2026-04-11 上線起零訊號。

    為什麼這條只能住在 repo-lint（**不是**偷懶，是沒有別的地方可放）：兩端住在不同
    的部署單元，`extra/paas-operator/` 不進 paas-odoo image（`Dockerfile.dev` 只
    `COPY src/`），而 fastapi 不在 Odoo 的 venv 裡。⇒ 沒有任何一個測試執行環境同時
    看得到兩端。放進 `src/tests/` 再對缺檔 skip，就會落進 #544／#684 那個家族——
    skipped 算進 testsRun 卻不出現在 summary，「沒跑」與「通過」輸出完全相同。

    判準（跨層比對，兩端都必須解得出來）：

    (i)   operator endpoint 對 `hostname` / `service_url` 的綁定必須是 `body`。
    (ii)  平台 client 送這兩個欄位的方式必須是 `body`（`_request(data=...)`）。
    (iii) 兩端相等——(i)(ii) 各自寫死一個期望值還不夠：真正的不變式是「兩端一致」，
          寫成兩個獨立常數會在有人**同時**改兩邊時仍然綠，而那正是要守的東西。

    陽性對照（否定性結論必配）：
      (a) 三份輸入檔必須存在且 parse 得動；endpoint handler 與 client 方法必須各自
          **剛好找到一個**。找不到 ＝ 掃描器／範圍壞了，不是乾淨。⚠ `update_tunnel_config`
          在 operator 內有兩個同名定義（API handler 與 `CloudflareService` 方法），
          所以入口刻意走 route 裝飾器而不是函式名。
      (b) 分類器自檢：對合成的「裸 str」必須判 query、對合成的「request model」必須
          判 body。少了這格，一個把什麼都判成 body 的壞分類器會讓 (i) 恆綠。
      (c) client 解析器自檢：對合成的 `data=` 判 body、`params=` 判 query。理由同 (b)。

    ⚠ 覆蓋邊界（刻意寫下來）：
      * 只認 `@router.<method>("<path>")` 這一種註冊形態。改用 `add_api_route()` 或
        把 path 拆成變數拼接就掃不到——那時 (a) 會紅（找不到 handler），不會靜默放行。
      * client 端只認 `_request(data=...)` / `_request(params=...)` 兩種載體，且欄位名
        要能從**字面值 dict** 解出來。動態組 key（`data[k] = v` 的 k 是變數）看不到。
      * 只比對**綁定的邊**（body / query），不驗型別、不驗欄位是否 required、也不驗
        欄位語意。把本條的綠讀成「這條 wire 一定會通」是錯的——行為那兩腿在
        `extra/paas-operator/tests/test_tunnel_config_wire_769.py`（真 app 送 body ⇒ 2xx）
        與 `src/tests/test_tunnel_config_wire_769.py`（client 真實序列化）。
      * 只守這**一條** wire。同 router 其他端點的形狀不在管轄內。
    """
    violations = []
    if not _addon_source_tree_present(root):
        return violations          # 不是本 addon 的 root，本條沒有輸入

    ref = TUNNEL_CONFIG_REF
    self_rel = REPO_LINT_SELF_REL

    for problem in _tunnel_wire_selftest():
        violations.append("%s：%s（%s）" % (self_rel, problem, ref))

    sources = {}
    for rel in (TUNNEL_CONFIG_OPERATOR_REL, TUNNEL_CONFIG_SCHEMAS_REL,
                TUNNEL_CONFIG_CLIENT_REL):
        path = os.path.join(root, rel)
        try:
            with io.open(path, encoding="utf-8") as handle:
                sources[rel] = handle.read()
        except (IOError, OSError, UnicodeDecodeError) as exc:
            violations.append(
                "%s：讀不到（%s）——本條的判準是否定性的，讀不到的檔「沒有違規」"
                "與「有違規但看不到」輸出完全相同（%s）" % (rel, exc, ref))
    if len(sources) != 3:
        return violations          # (a) 已經紅了，繼續往下只會產生誤導訊息

    try:
        schema_models = _tunnel_wire_schema_models(sources[TUNNEL_CONFIG_SCHEMAS_REL])
    except SyntaxError as exc:
        violations.append("%s：parse 失敗（%s）（%s）"
                          % (TUNNEL_CONFIG_SCHEMAS_REL, exc, ref))
        return violations

    try:
        endpoint_binding, endpoint_problems = _tunnel_wire_endpoint_binding(
            sources[TUNNEL_CONFIG_OPERATOR_REL], schema_models)
    except SyntaxError as exc:
        violations.append("%s：parse 失敗（%s）（%s）"
                          % (TUNNEL_CONFIG_OPERATOR_REL, exc, ref))
        return violations
    for problem in endpoint_problems:
        violations.append("%s：%s（%s）" % (TUNNEL_CONFIG_OPERATOR_REL, problem, ref))

    try:
        client_binding, client_problems = _tunnel_wire_client_binding(
            sources[TUNNEL_CONFIG_CLIENT_REL])
    except SyntaxError as exc:
        violations.append("%s：parse 失敗（%s）（%s）"
                          % (TUNNEL_CONFIG_CLIENT_REL, exc, ref))
        return violations
    for problem in client_problems:
        violations.append("%s：%s（%s）" % (TUNNEL_CONFIG_CLIENT_REL, problem, ref))

    if endpoint_problems or client_problems:
        return violations          # 解析已經壞了，比對結果沒有意義

    for field in TUNNEL_CONFIG_FIELDS:
        operator_side = endpoint_binding.get(field)
        client_side = client_binding.get(field)

        if operator_side is None:
            violations.append(
                "%s：`%s` 端點完全沒有 `%s` 這個參數——欄位被改名或刪掉了，而呼叫端"
                "還在送它（%s）"
                % (TUNNEL_CONFIG_OPERATOR_REL, TUNNEL_CONFIG_ROUTE_PATH, field, ref))
            continue
        if client_side is None:
            violations.append(
                "%s：`%s()` 沒有送 `%s`，但端點要它——兩端對這個欄位的存在性就不一致"
                "（%s）"
                % (TUNNEL_CONFIG_CLIENT_REL, TUNNEL_CONFIG_CLIENT_METHOD, field, ref))
            continue

        # (iii) 核心：兩端相等。
        if operator_side != client_side:
            violations.append(
                "%s：`%s` 的 wire 兩端對不上——operator 把 `%s` 綁在 **%s**"
                "（%s），平台 client 送在 **%s**（%s 的 `%s()`）。這個組合每一次呼叫"
                "都是 422，而兩端的測試都不會紅（%s）"
                % (self_rel, TUNNEL_CONFIG_ROUTE_PATH, field, operator_side,
                   TUNNEL_CONFIG_OPERATOR_REL, client_side,
                   TUNNEL_CONFIG_CLIENT_REL, TUNNEL_CONFIG_CLIENT_METHOD, ref))
            continue

        # (i)(ii)：一致還不夠——一致地都用 query 也會通過上面那格，但那是
        # #769 明確否決的修法方向（唯一呼叫者送 body、同 router 其他變更型端點
        # 全部吃 body model、stg 14 天 access log 顯示沒有第三方在用 query 形態）。
        if operator_side != "body":
            violations.append(
                "%s：`%s` 的 `%s` 兩端一致地綁在 **%s**，但 #769 的裁決是收斂到"
                "**body**（request model）。同 router 的 `POST /api/tunnels`、"
                "`PUT /{tunnel_id}/routes` 都吃 request model，這條是唯一例外（%s）"
                % (self_rel, TUNNEL_CONFIG_ROUTE_PATH, field, operator_side, ref))
    return violations


def main(argv):
    if "--hook" in argv:
        return run_hook()
    root = repo_root()
    if "--update-baseline" in argv:
        return update_baseline(root)
    return run_all(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
