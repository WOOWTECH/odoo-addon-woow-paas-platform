[481 lines total]
# PaaS 上線推進看板（2026-08-21 精簡版）

> ⚠ 標題日期是初版建立日；**2026-08-27 已大幅精簡**（削 70%），做法與還原路徑見文末「📐 本輪修剪紀錄」。

> **本 note 只放 open issue 的分群結果與仍生效的規則**；證據、過程、量測值下沉到耐久載體（issue 留言／PR comment／`docs/`／memory）。
>
> **對帳重算法（給推導方式不給死數字）**：`tea api -X GET "/repos/{owner}/{repo}/issues?state=open&type=issues&limit=50&page=N"` 逐頁抓到「該頁不足 50 筆」為止，再與看板表格**第一個 cell** 的 `**#N**` 取集合差。六個 repo：main `odoo-addons/woow_paas_platform`、ci `woow-paas/paas-odoo-ci`、cd `woow-paas/paas-app-bundle-cd`、charts `woow-paas/woow-paas-charts`、ar `woow-paas/paas-agent-runtime`、sh `ha-components/woow_paas_smart_home`。
> 🔴 `limit` 被 server 截到 50（不逐頁會靜默漏抓）；🔴 不帶 `type=issues` 會把 PR 算進來；🔴 owner 猜錯回 404、與「issue 不存在」分不出來；🔴 live 快照有效期是分鐘級，寫入前重抓。
>
> **發版現況**：prod 版號**用前自己量**（落差基準以 prod DB 實裝版號為準，不寫死——不沿用看板上任何舊版號）。
>
> **修剪紀錄**：歷次全文存底見 `docs/archived/`；本輪（2026-08-27，issue-leader，Eugene 授權大幅精簡）＝清逐輪工作日誌＋濃縮分群表＋只留 open issue。

> 🔴 **MEMORY.md 索引解凍**：可新增索引行，規則＝link＋一句 hook、症狀字面留在 hook；任何 session 動 `MEMORY.md` 前**必先重讀磁碟**、**只逐行 edit 禁整檔重寫**。

## Leader 工作原則

> 本段只放**仍生效的規則**；證據、量測值、歸因過程不放。

- `issue-leader` 分配 issue、獨家改本 note（一律 `maestri note edit`）；Leader 對本 note **唯讀**，要改請 `issue-leader` 改。
- Leader 一次只做一個 issue（或同組相關 issues），不排程無關工作；全部完成後 `git pull` 並 `/clear` 才接下一張。
- 取用請 `issue-leader` 在「🔒 認領中」登記＋該 issue 列標 🔒；完成請 `issue-leader` 兩處移除。**無登記＝無人處理。**
- 本 note 不當工作記憶，工作記錄自己 `maestri note create`；本 note 定期清已關 issue、同步 `tea` 可讀到的 open issue。
- 🔴 **manifest 版號（#691 改制；gate＝develop 唯一 required check）**：一般 PR 不動 `src/__manifest__.py`，版號須等於 origin/develop（併完 diff 須為空）。只有「新增 `src/migrations/<X>/` 目錄」的 PR 才 bump（元組嚴格大於 develop）；只改既有 migration 目錄＝一般 PR。`release/*` 分支才 bump。取號＝`max(develop, 其他在飛會 bump 的 PR head)+1`、多人同時由 issue-leader 指定、量完立刻 commit。低於 develop 一律紅（先 merge origin/develop）。權威：`scripts/ci/manifest-version-gate.py` docstring、`docs/guides/development/merge-train-acceptance.md` §1。
- 🔴 **Merge 授權（Eugene 08-22 裁）**：純 docs／純 bug fix／test-only（零生產碼）／純 CI-config，**且無待討論取捨**的 PR，issue-leader 於 CI 全綠＋覆核 diff 後直接 merge、不等 Eugene——類別**按精神非字面窮舉**；含取捨／行為權衡／動生產碼行為／feat／refactor／CI 政策類 ⛔ 待 Eugene。PR 標題一律掛「⛔ 勿代 merge」前綴（永不拿掉）。類別終判與 merge 動作只歸 issue-leader；merge 前照 multi-PR 驗收工具箱（manifest 同值撞號、`git diff origin/<base>` 非空、base 前進重驗）。
- ⚠ **磁碟／沙盒 slot**：並行 disk-heavy 測試環境慣例上限 2（非機器 gate，真約束是磁碟餘裕不是 slot 數，判「能不能再起一個」看 `df -k` 不看計數）；起前向 issue-leader 登記；free <10Gi 回單 slot、abort 線 6Gi 全隊適用；收工立刻 destroy＋清理並回報 `df -k` 原始讀數，teammate 照保全範本後才 dismiss；slug ≤26。⚠ 機器 Avail 有隊外活動自漂，單點差值不得歸因單一 agent。
- 🔴 **docker-desktop hostPath 內容 gate（plugin 起）**：local `create.sh` 逐條比對 host/node digest，不一致或探針失效即 rc=1——紅了＝node 副本凍結或探針壞，不是你的碼；正解 `docker cp` 進**自己專屬**的 node 路徑再建，別覆蓋共用路徑。驗「測的是我這份碼」看 host↔node↔pod 同檔 sha，不看路徑存在／exit 0。驗 digest 必帶 `LC_ALL=C sort`。細節見 memory `reference_k8s_sandbox_test_gotchas.md`、`topic_probe_failure_modes.md`。
- 🔴 **升裁選項組必先驗共同前提**：三個選項共用一個未驗前提＝一個未驗前提偽裝成三個選項——不會以「某選項有缺陷」暴露，裁了才發現全不可行；至少一個選項必須是「先驗證前提」；轉呈者（含 issue-leader）先問「共同前提驗過沒」。
- 🔴 **查重／對帳分頁到底**：`tea issues ls --limit` 封頂 50 且無截斷提示，一律 `tea api .../issues?state=all&limit=50&page=N`；輸出行數剛好等於上限就重數。
- 🔴 **CI status 判「新跑非回收」用 `target_url` 的 run/job id（單調變大）＋`created_at`**，⛔ 不用 status `id`（每 commit 從 1 自編、非全域）。
- 🔴 **保全落點掛在不隨 dismiss 消失的載體**（派工者自建 note／repo 檔案／issue 留言）——teammate 節點上的 note 一 dismiss 即失聯，「dismiss 前讀得到」零鑑別力；先搬到自己載體再 dismiss。⚠ 足跡以被刪物 `du` 為證，df 差額不得當足跡交差。
- 🔴 **未署名訊息作者欄留空**：所有 maestri 訊息同一通道、「從哪到達」對作者零鑑別力；不用語境補作者；轉述不確定的事寫「未署名、作者不明、供對時序」。
- 🔴 **覆核紀律**：①集合比對通過 ≠ 預測成立——預測含成因時成因逐條比；②解釋一件事會生產它的識別字（例：PR body 解釋「為何不關」⇒ 種出 close token，命中不改 commit，併後驗單、錯關重開）。
- 🔴 **驗令紀律**：執行類指令（起沙盒／merge／刪除）不憑語氣或參數正確性認令，判準必含至少一項「發令當下才量得到的新鮮狀態」；憑證用原始量測（`df -k` 的 Avail KiB＋mount，不用 `df -h`）。輸入框出現來源不明未送出指令：**不執行**、回報 issue-leader；⚠ Claude Code 的輸入建議功能會在輸入框留下未送出殘句，非任何人下令，但仍不執行、仍回報。
- 🔴 **通道與 check**：maestri 訊息可能靜默丟失——「沒收到回報」不得推論成「還沒做」，催前先 `check` 或自查產物。`maestri check` 在對方 idle 時是凍結快照；判狀態＝問對方＋自己獨立量（`git status`／`ls` 實作物）。指示須指向 `.scratch/` 下實際存在的檔案、`cat` 驗過才動——來源面（是不是自己 leader 下的）在 teammate 端結構上驗不了，只有內容面驗得了。
- 🔴 **merge 一落地先通知相關 leader，再開始任何長輪詢**（Gitea 側全掛 Eugene 帳號，不通知＝別人查不出是誰併的）。

## 🔒 認領中

> 🔴 **08-28 起認領有兩個載體，講清楚哪個權威**（PR #952 落地 `docs/agents/triage-labels.md`）：
> **本 note 仍是權威**（它帶推理與狀態）；tracker 的 `agent-*` label 是**投影**，方便在 Gitea 側看得到。
> ⇒ **issue-leader 每次上鎖／除鎖必須同步兩邊**，否則就是雙寫漂移。三組 label 的用法：
> `agent-assignee:leaderN`＝這張單歸誰（對應本表的 Leader 欄）；
> `agent-context:local-cc`／`:remote-pi`＝在哪個執行環境跑（**五個 leader 目前皆 local-cc**；
> leader4 雖連著 `paas-platform#pi`，但那是它可用的資源、不是它自己的執行環境）；
> `agent-status:in-progress`＝**當下真的在跑**才掛，卡 Eugene／排佇列／PR 等決策**一律不掛**
> ——全掛上去這個 label 就沒有鑑別力了。**完成或停止時移除。**
>
> ⚠ **文件宣告 ≠ tracker 實況**（08-28 issue-leader 對帳）：`triage-labels.md` 的五個 triage label 裡
> **只有 `ready-for-agent` 真的存在**，`needs-triage`／`needs-info`／`ready-for-human`／`wontfix`
> **tracker 上沒有**。⛔ issue-leader 未擅自建——本 repo 已有 `cat-discuss`／`pending`／`derived`
> 一套在用，硬加會變兩套真相；而該 doc 自己就寫「Edit the right-hand column to match whatever
> vocabulary you actually use」⇒ **要建、還是把右欄改成映射到既有 label，待 Eugene 裁。**
>
> ⚠ **順帶發現的分類異常**：**#864 掛的是 `A3-ci-release`**，但它是金流 bug（ECPay 付款鏈、
> 錢包不入帳），本 note 自己也寫「#864 金流現行 bug」⇒ 疑應為 `A2-billing`。⛔ 未改，待裁。

> 🔓 08-25 Eugene 明示解凍分派：AC entitlement 相關單（#381／#521／#773／#272；同批 #750 已關）＋#107（已完成，見「上線 gate 對照」）；08-23 總體暫停分配政策對其餘單不變。

| Issue | Leader | 起始 | 狀態 |
| --- | --- | --- | --- |
| **#381＋#521＋#773**（同組）🔒 | leader1 | 2026-08-25 | #381 拍板題已全數清空（end-user 認證策略、計數基準、對外販售前提等皆已裁）。#521 四題已裁（#521 留言 9960，08-26：grandfather 一律擋／P-J 納 bypass／報表分母只算 in_progress／0 席語意維持）且 PR #895 已併（08-27 live 查 merged）；剩 Q0 量測、OQ-D 重跑、P-J 實作，皆非拍板格；#773 選項組**已完成**（leader1，08-26，`.scratch/options-773-convergence.md`；T1–T5 五題、共同前提 CP-A～CP-E 已驗、CP-F／CP-G 未驗故 T1 含 1-C「先驗前提」格），issue-leader 08-28 抽驗兩格載重主張於今日 develop 仍成立 ⇒ 裁決 sheet 已呈：`.scratch/ruling-sheet-773-2026-08-28.md`（T1–T5 五題）。衍生 **#890**（A5，derived+pending，繫 #381 拍板）。 |
| ~~#849~~ ✅ **已關**（08-27 15:41Z，留言 10212）| leader3 | — | 修復載體＝PR #929（merge `c8685e27`、fix commit `3a850d25`），守衛 `_reject_derived_projection_writes` 已接 policy／rule 兩側。🔴 **`wt-849`／`fix/849-related-anchor-projection` 已完全被取代**——issue-leader 08-28 以 sha 比對確認該分支唯一的 RED 測試檔與 develop 內同名檔 **完全相同**（`9f5d7820…`）⇒ 該 worktree 與遠端分支可清。⇒ **leader3 手上空**；殘留在 #940／#941／#896。 |
| **#272** 🔒（佇列尾、未開工） | leader2 | 2026-08-25 | 排既有佇列之後（#860 已由 PR #947 完成並關單，佇列頭剩 #155）、不插隊；輪到時 preset 草案先經 issue-leader 過 Eugene 再實作。 |
| ~~#155~~ ✅ 已關 | leader2 | 2026-08-28 | 🔓 Eugene 08-28 明示解凍。⚠ **issue body 已過期**：operator 側 stopgap **已實作且已接線**（`config.py:88` `skill_bundle_token_ttl_seconds` 預設 604800／7 天；`sign_skill_bundle_token` 全 repo 2 個呼叫者〔`agent_runtimes.py:374`／`:407`〕**皆有傳** `exp_seconds`——issue-leader 08-28 AST 資源側枚舉）。⇒ 本輪是**先量已部署現況**（operator image 版本＋CD values 有無設 `SKILL_BUNDLE_TOKEN_TTL_SECONDS`，該 env 住 `paas-app-bundle-cd` 跨 repo），非重做修法。🔴 連帶欠一筆：7 天 TTL 讓 F-GC-1「≤1h 所以可接受」的前提失效（明文寫在 `config.py:78-85`），severity 待重評。工單 `.scratch/wo-155-leader2.md`。 |
| **#949** 🔒 | leader1 | 2026-08-28 | 🔓 Eugene 08-28 明示解凍派工。CS change-plan 歸屬閘 fail-open（`paas.py:1157`）；§4 量測已完成：🔴「今天可達」證偽（`conceal_instances` 閘更早 return）⇒ 潛伏缺陷、已降 medium；「擋」不打壞任何合法流程 ⇒ 已照「擋」開工（刪 truthiness 前綴，⛔ 非 and→or，CS 無 template-less 格故不需 family 判準）。第二處同型 `paas.py:3291` 已授權一併修。✅ **prod 已量（Eugene 08-28 授權，issue-leader 執行，全程 SELECT；#949 留言 10244）＝影響面 0**：風險組合（上架∧不隱藏∧不遮蔽∧零方案∧有服務）零筆，12 個可見 template 最少也掛 2 個方案；另補驗「方案全被封存」邊界亦 0（319/319 皆 active）⇒ 合併安全性疑慮解除。工單 `.scratch/wo-949-leader1.md`。 |
| **#864** 🔒 | leader4 | ≤2026-08-26 | 六格裁決已落（Eugene 08-27，#864 留言 10190）：格1/2/3a 修、格4 先驗、格5 一起查、格6 body 重寫（已落地）；3b 發票政策值未裁。🔴 **格4 主命題已實打證實（08-28，留言 10323）**：3DS 路徑上 callback 早於 commit 到達 ⇒ 查無 tx ⇒ 回 `1|OK` 叫 ECPay 停止重送 ⇒ tx 與 topup 永久 `pending`、錢包永不入帳、**零 failure_code／零告警／零重送**；N 組（唯一變因＝callback 改在 commit 後）則正常入帳。🔴 兩組 HTTP 完全同形 ⇒ 只看 HTTP 分不出兩個天差地別的結局。🔴 `charge exception=0` ⇒ **本失效與 catch-all 吞例外無關，補 raise 無效**。⚠ 窗口寬度未量（P-2′ 未做）⇒ 不宣稱發生機率。範圍重定（08-28，留言 10221）：P-1 判定路徑 A（no-3DS inline `_set_done`）不可達 ⇒ 主標的改為路徑 B 的 callback-before-commit race ＋ 孤兒 callback（仍在 10190「含 pay_callback 追查」射程內，非新範圍）；路徑 A 保留為 counterfactual、ECPay 一手 contract 待 Eugene 對外協調。🔴 **prod 與 stg 的 `payment_transaction` 皆 0 筆**（issue-leader 量，含陽性對照）＝整條 ECPG 付款鏈從未跑過，沙盒這趟是第一次端到端。衍生 #948。 🔴 **P-2′ 窗口已量（留言 10325）**：mock 下 7／8／11 ms，但真實窗口＝該值＋整趟 ECPay 往返（交易在整趟外呼期間都開著）；⚠ 反向亦成立——3DS callback 是使用者完成後數秒才到 ⇒**自然狀態下不會撞到**，R 組證明的是「撞到時會怎樣」⛔ 不是「常常撞到」。 🔴 **O 組的危害面不受窗口寬度影響**（任何原因的「callback 查無 tx」都走同一分支、同一個 `1|OK`）⇒ 修法標的移到「查無 tx 時的行為」，**#948 因此從漂移單升格成修法路徑的一部分**。 🔴 **A 組成立（08-28，留言 10336）**：auto-topup 的 `CHARGED_NOT_CREDITED` 保護在伺服器端真 40001 下**完全不落地**（`failure_code`／`failure_message` 皆空），而 `charge_via_token` 兩組回傳相同⇒ 從呼叫端看「保護生效了」。issue-leader 讀碼判定 prod 形狀**更糟**：`sh_subscription_fix.py:404` 的 savepoint 內 create+charge 零 commit ⇒ topup 與 cycle invoice **整列不存在**，對帳側看到的是零。⇒ 格1／2／3a 修法**必須**用獨立交易寫告警。 ⬜ 一格待補：40001 後 `ROLLBACK TO SAVEPOINT` 救不救得回交易（救不回＝一次 40001 毒掉整輪 cron）。 |
| **#964** 🔒 | paas-platform#pi #2 | 2026-08-28 | label taxonomy 審計與遷移（Eugene 08-28 指派 pi-agent CLI）。Phase 1＝45 張 cat-discuss/pending 逐張唯讀分類（實際卡點五分類＋mattpocock 五標籤 fit 判定，none-fits 比例＝轉換資訊損失量測）；⛔ Phase 2（label 異動）需 Eugene 裁選項後經 issue-leader 轉發才動。🔴 **pi #2 在遠端＝互不可讀檔案系統**（Eugene 08-28 指正；首發帶路徑的派工已作廢重發）：工單以 ask \$(cat) 全文內嵌遞送（送達已驗，畫面逐字出現）；**其產出與開工 marker 一律落 #964 留言**，⛔ 不走 .scratch。⚠ agent-assignee:* 只有 leader1–5，pi 無對應＝#952 詞彙缺口，記在 #964 body。 |
| **#956** 🔒 | leader5 | 2026-08-28 | develop 的 odoo-tests 自 PR #947 起轉紅。**issue-leader 只派歸屬重量、⛔ 不含修**（修法含「測試寫錯 vs 實作回歸」的行為判斷題，卡 Eugene）。🔴 **leader5 盲測（未給我方數字）結果與 issue-leader 先前在 PR 分支上算的那組完全一致**：4 條＝`TestSh740NoExternalCallWhenDenied` 的 A1/A2/A3 ＋ `TestPaaSCreateAndSkipErrorTaxonomy567.test_skip_trial_operational_error_is_reraised`。他多做了一個基準：`c8685e27`（#947 併入**前**）run 5611 ＝ **0 failed of 3556、五 gate 全 PASS** ⇒ 「這 4 條落在 #947 區間內」是**量測不是推論**。✅ **740 那 3 條已脫離 Eugene 桌面**：issue-leader 08-28 驗到 #947 把原始例外原封留在 `smart_home.py:137` 的 `_logger.exception("...: %s", e)` ⇒ 錨點換到 server log 可拿到**完全相同的判別式**（含區分 `smart_home` vs `smart_home_route` 的右括號技巧），租戶可見字串一字不動 ⇒ **純測試改動、無取捨、issue-leader 權限內**。⬜ 剩 567 那 1 條——卡的不是決策是**證據**：traceback 只在容器內 `/tmp/test.log`，job 結束即失（見 #963）。衍生 **#963**。 |

> **五線在飛狀態**：**leader1**＝測試／docs／守線專責＋衍生單守門（#381/#521/#773 鎖在身、全卡 Eugene 拍板＝無可動工項）。**leader2**＝A4 功能 bug（手上空）。🔴 **#860＋#567 已於 08-27 15:22Z 由 PR #947 併入 develop 並自動關單**（body 的 `Closes #860`／`Closes #567`）——✅ **歸屬已結案：Eugene 本人 session 開的**（08-28 親口確認）⇒ 非治理事件，「⛔ 勿代 merge」前綴規則不適用於他本人的 PR，⛔ 勿再重複升級。⇒ 佇列頭 #155 已於 08-28 經 Eugene 明示解凍派工（見認領表）。**leader3**＝#849（見上表）。**leader4**＝**#864** 續工中——六格裁決已落（#864 留言 10190，詳見上表）；bug 枚舉軸消歧與逐支判定以留言 9453／9458 為權威；格4 驗證沙盒已建（`dev-sandbox-864topup`，docker-desktop，issue-leader 08-28 兩 context 驗過無跨叢集；TTL 24h）；因 #472 使 ci-local build 不可用，改用 stg image `20260820.0807` ⇒ 引入「image 比分析基準舊一週」前提，P-0b 已擴為兩棵樹資源側全枚舉。舊 dev-864a 產物**零損失**（落點在 `AREA-odoo/issue/`，三數字對上）。**leader5**＝A1 provision／tunnel 鏈；**#385** runbook 步驟 1-5 已授權（prod odoo shell 唯讀對帳、零寫入 gate、記憶體 <500Mi 即停）；`reconcile_orphans(env)` 為模組層函式非 model method、`execute_method` 打不到。develop `docs/index.md` 既有計數偏差待隨下個 PR 修正。

> 🔴 **CI status 判準**：前窗口 `pending`/`total` 為 0、或 contexts 少於全套時，判準須「**期望 context 名字逐個出現**」而非只看數量。⚠ `odoo-tests.yml` **無 `pull_request` 觸發** ⇒ PR 階段照不到全套 odoo-tests；⚠ **PR 階段的 context 數已是 5 不是 4**（2026-08-28 於 PR #959 逐名實得：`Docs Link Check / lychee`、`manifest-version-gate`、`paas-operator-unit-tests / unit-tests`、`repo-lint / repo-lint`、**`test-collection-audit / test-collection-audit`**）——清單會長，判準一律「期望 context 逐名出現」⛔ 不寫死份數。⚠ 任何 PR head 引用一律當下重查，不沿用上輪值。

## 🚀 上線 gate 對照（launch-readiness report 覆核）

> 來源＝外部 agent 產出的 `launch-readiness-report.md`＋issue-leader live 覆核（`tea` 逐張＋看板／docs／內部一致性四路交叉）。⚠ 報告內部計數自相矛盾、部分項目 Tier 分類前後不一致，引用它先對本段，別引它的數字。

✅ **priority label 事件已裁（Eugene：認）**——`priority:high/medium/low` 三個 label 納入正式體系。⚠ 範圍限定：①是否進抽單鍵（`derived`∧¬`pending`）未裁；②現有標值為機器初判、未經覆核；③逐張覆核歸屬與時機待定。

- **開放 gate ≠ 本 repo 工作序**（報告 Tier-1 把兩者混在一表）。gate 清單＝Eugene 對外協調／拍板，不佔 leader：⊘ 段（#440／#512／#442／#443／#444／#441 備份鏈＋#725 歸 `sh_subscription`）、上游 #718（圍堵 runbook 已在 `docs/guides/deployment/hermes-agent-console-support-runbook.md`）、**#389 mail provider 決策（唯一 A0）**、憑證輪替 cd#24／cd#78／cd#117。
- **本 repo 工作序（暫停分配解除後）**：在飛收尾（#849＋#848 一起、#864 金流現行 bug、~~#803~~（已關，08-28）、#385）→ cd#76（付費開通靜默失敗）→ #155／#162／#163 → #823 → ~~#860＋#567~~（已由 PR #947 完成，08-27）→ #427／#514（待 R1–R3 裁）。#107（launch-first）已完成。報告 Tier-2 裡的 #472／#510／#25／#319 屬開發體驗／技術債，非上線前置。
- **報告已過期項（引用前對 live）**：#739 已修（PR #797）待關單；#787 A 段已交付、待 Eugene 關或續追；#531／#798 在報告產出前一日已關；‼ **#36 派工政策段記「已收官」但 issue 仍 open（掛 `launch-first`）**——待 Eugene 關單或更正記載。
- **真缺的三個文件決策題**：使用者故障回報 SOP、SLA、launch-readiness checklist。其餘三項已有載體：監控＝`docs/guides/deployment/k8s-setup.md` §7.4、rollback＝`docs/guides/deployment/prod-release-sop.md` §8、容量＝`docs/plans/2026-Q3/2026-07-27-haos-kubevirt-v1-design.md` §6.5／§8。

## 分類原則（Eugene 2026-08-12 定，5 類）

| 類 | 定義 | 現在做嗎 |
| --- | --- | --- |
| **② 實作類** | 直接推進上線 | ✅ token 優先 |
| **③ 需討論類** | 需求/策略未定 | ⏸ 先問 Eugene，別自行開工 |
| **①a docs 文件知識類** | 開發常識、文件沉澱 | ⏸ 先問 Eugene，別自行開工 |
| **①b 固化自動驗證** | test/lint/audit/docs pattern 等可程式化省 token 的驗證 | ⏸ 先問 Eugene 要不要做＋附成效評估 |
| **⊘ 非 paas 管轄** | 內文自陳歸屬 woow-machines-operator | ⊘ 不排進來 |

### 🔴 補充判準（Eugene 2026-08-18 定）：「今天可被利用／被繞過，但修法界線未定」怎麼歸類

- 分類按「**要做什麼**是否已定」排，不按危害、也不按「怎麼修」是否已定。
- 第 1 層：「要不要做／做什麼」未拍板 ⇒ **③**。
- 第 2 層：「該做無疑，只是怎麼修未選型」⇒ **留 ②**——選型是實作的一部分。
- 兩層都要在危害所屬的 ② 分段補 🔴 交叉指標指回去（分類 ≠ 嚴重度）。
- 🔴 判定要讀 issue 原文，不讀二手轉寫（同一句「修法未定案」在不同 issue 可能落不同層）。
- 首批適用：#710／#704（→A1 指標）、#773（→A2 指標）；存量已於 2026-08-18 全數對齊。

## 🔴 仍生效的規則與判準（自逐輪日誌萃取，2026-08-27 削減時保留）

- **發版驗收 image 是否真的滾動，只認 `imageID` 的 `@sha256:` 之後那段做絕對比對，不比對 repo path、也不比較 tag 字串** —— repo path 不同、digest 相同是 containerd 對同一 digest 只存一份的快取行為（例：報 `paas-odoo-stg`），不是拉錯；「gate 與主容器 imageID 相同」只是相對比較，對「完全沒 rollout」零鑑別力，要另外對 values 裡的新 digest 做絕對比對，且要看 pod 的 `imageID` 不能只看 deploy spec 寫的 tag。
- **CD chart 的 digest pin 語意因檔案而異，改前要查該檔本身**：`paas-operator*/values*.yaml` 的 image 段只有 repository/tag/pullPolicy、沒有 digest pin，改 tag 即生效；`paas-odoo/values-prod.yaml` 與 chart的 `pvcCleanup.image` 才有 digest pin（此時只改 tag 可能變 no-op，或跨 repo digest 拼出 ImagePullBackOff）。
- **`/web/signup` 走 template `copy()`，新註冊帳號的 `lang` 不受 `ir.default` 影響** —— `_create_user_from_template()` 用 `template_user.copy(values)`，`copy()` 把來源值塞進 vals，`ir.default` 全程不參與；要換新帳號預設語系須改樣板本身，不是改 `ir.default`。
- **`lang_install` 全鏈（`_update_translations`→`_load_module_terms`→`TranslationImporter.save()`）零 `cr.commit()`**，commit 點在 `_button_immediate_function`（`ir_module.py:603/605`）不在此鏈，故整段可安全 dry-run 後 rollback 再決定要不要真跑。
- **Odoo `res_lang.active` 未啟用語系時是 `NULL` 不是 `false`**，判斷式寫 `= false` 會漏判；SQL 用 `string_agg` 之類字串聚合查詢時 NULL 值會讓整列被靜默丟掉（已連撞兩次），含可能為 NULL 的欄位要逐列查、不能只信聚合結果。
- **`maestri check <role>` 對「另一個 leader 有沒有收到訊息」零鑑別力** —— check 期間 canvas 焦點常停在同一個終端，連續兩次 check 可能回同一個 role 畫面；驗開工要看任務特異產物，不要靠 check 判斷別人收到沒。
- **驗沙盒／測試結果前先拿陽性證據證明模組真的裝上**（如 `ir_module_module=installed` 落檔）；在那之前，測試綠紅都不可信。
- **`ps aux | grep -c <pattern>` 拿來數測試進程會給假數字**（曾假回 2、成因未追出）——要數進程數改看完整進程列表，不要信 `grep -c` 的計數。
- **`git merge-tree` 說 clean 不代表合併結果語意正確** —— 那只保證不衝突；有交集的熱點檔（兩邊都可能各自新增內容，如 `__init__.py`）要把合併結果的樹實際產出來檢查（例如比對前後 import 數量）。
- **驗「既有功能沒壞」不要自己戳，改從新版服務的 log 找一筆確定不是自己打出來的流量**（例如來源 IP 不在自己這個 ns 的真實請求），比自己重新觸發一次更有說服力。
- **`build-operator.sh` 的 `git tag` 直接 tag 本機 HEAD、全程無 fetch/pull**，只警告「未 push」不警告「落後 origin」——打 release tag 前務必先 `git pull --ff-only` 確認未落後（stale-base 家族的新實例，腳本「ahead 有警告／behind 無警告」的不對稱本身就是雷）。
- **驗 Gitea Actions tag-triggered run 用 API 的 `path` 欄位過濾，不要用 `runs list` 的第一筆** —— list 按 run id 遞減非時間序、WORKFLOW 欄印的其實是 commit 訊息、tag run 的 BRANCH 欄是空的，三者都會給假陰性／假陽性。
- **平台發版有人工核可 gate（留言精確等於 `/approve`，`PROD_APPROVERS` 白名單、且 `ALLOW_SELF_APPROVE=true` 故 pusher 可自簽），operator 發版沒有這道 gate**（tag 直接觸發 build，走 repo 內 workflow）——兩條發版線的授權模型不同，別把其中一條的假設套到另一條；`/approve` 是由 `issue_comment` 觸發全新一次 run（其 path 顯示 `@refs/heads/main` 非 tag ref，屬預期非異常）。
- **CI 期望 context 清單會長大，別寫死舊的份數** —— 判準是「期望 context 逐名出現」，develop 曾中途新增第 4 個 context，沿用舊清單會漏判。
- **`Closes #NNN` 語法本身觸發 auto-close 不穩定，目前找不到單一假說能解釋**（同樣 body/title 都寫 `Closes #NNN`，有的觸發有的沒觸發）——⛔ 遇到判準要留但別再宣稱「已解謎」；比較可靠的是 commit subject 寫成 `fix: #NNN …` 這種緊鄰單號的形態，這種形態穩定會觸發（Refs 混在同一行後半無效）。
- **關 issue 前若還有「未量測」的殘留格，不要留在同一張已修完的 bug 單裡** —— 那會讓單子變得永遠關不掉；正解是把未量測格逐字搬進新開的 derived 單，本單照常關。
- **判斷一張 PR 是不是 Eugene 本人開的，可用 transcript 查歸屬（例：內容+時間戳+起始目錄）**——查實屬本人 session 開的，就不算治理事件，「勿代-merge」前綴規則不適用於本人自己的 PR。
- **HA（`sh_subscription` 系）側 repo 完全沒有 CI**（0 statuses、無 workflows 目錄）——PR body 附的測試結果是作者本機產物，覆核不能把它當獨立 gate，要另外用資源側方法（如把待驗函式從 base/head 各自原始碼抽出來單獨跑真值表）驗證。

# ② 實作類 — 可推進上線

## A0 · prod 事故

> #512／#441／#442 屬叢集運維，已移到文末 ⊘ 段（非已關，是移出本段）；#503／#694 已關單，殘餘承接在 #702／#714（列於其他分段）。

- **#389** prod SMTP 寄信全 exception，根因＝平台本體 Odoo 從未設定 outgoing mail server —— 待決 mail provider／帳密由誰提供

## A1 · 資安：租戶可實際利用

> 🔴 **交叉指標**：以下分流到 ③ 的單，危害其實落在 A1（修法界線未定，不是危害低），排序時一併看（Eugene 2026-08-18 裁定為通用判準，規則全文見開頭「分類原則」段）。
> - #710 guest 可直接 unlink project，project 與 cloud_service 關聯整個消失
> - #704 租戶可自行 write `use_agent_runtime` 開 agent runtime、佈署 pod 吃資源
> - #152 project owner 可繞過 UI 寫 dedicated agent_template 敏感欄位；admin-only 選項未擇一
> - #711 `_check_workspace_access` 只驗成員不驗 role，`sudo().create()` 豁免綁定守衛；4 選項未拍板
> - #385 CF 孤兒盤點其中一條疑為放行規則；待決清哪些
> - #721 租戶今天即可經 safe_eval 讓資料層等同 superuser（spike，不含實作承諾）；OS 層逃逸子題見 #774
> - #799 CF 推送生效但 DB 回捲，policy 孤兒殘留；待決回捲策略是否改 partial success

**跨租戶奪權／讀取（最高，會被真的用到）**

- **#718** 上游 hermes install_cmd 無 approval gate、shell 注入可達 —— 歸屬上游，已回報＋內部圍堵記錄，不自行 patch
- **#842** 🆕 tombstone-check 只驗本 account，跨 account tunnel 可能誤判孤兒（未實測）；Refs #824
- **#883** 🆕 shared tunnel 保護判定未分環境，prod/stg 共用 tunnel 易被誤清；修法未選型；Refs #385
- **#960** 🆕 `ir.cron` 接線無守線——`code` 裡 method 名拼錯會**靜默失敗**，而「模組安裝成功／data 載入無誤／單元測試全綠」三個綠燈**全部照樣成立**（測試直呼 method、不走 ir.cron）。🔴 檢查器**已寫好**（`scripts/audit/verify-cron-wiring.py`，AST＋xml.etree，17 筆全 repo 適用，開發期三格注入式陽性對照都如期紅），成本只在「收進 repo-lint」＝動 gate 行為 ⇒ **動手前要 Eugene 點頭**。labels: cat-quality-gate / A5-techdebt / priority:medium。Refs #803
- **#961** 🆕 `access_resync` cron 的 `gated>0` 就升 WARNING ⇒ 長期停權租戶每 15 分鐘一次＝96 次／日 log 洪水。`gated` 是常態、`failed` 才是異常。⚠ **已合併的碼**（PR #876，08-23）⇒ 走正常 PR。⚠ `test_D4` 不綁 level ⇒ 改 level 不會讓它紅，**改時要順手補釘 level 的斷言**。labels: derived / A5-techdebt / priority:low。Refs #803
- **#963** 🆕 `odoo-tests.yml` 的 C0／C5／C2／C3／C4 **五個 gate 全無 `if: always()`**（而同 workflow 的 `原始觀測落地`／`摘要`／`收尾` 都有）⇒ **C5 一紅，C2／C3／C4 整個不跑**，🔴 而 C3 正是產生「哪些測試真的被執行了」那份證據的 gate ⇒ **證據恰好在有失敗時消失**。08-28 量 #956 時實撞：判準③ 只能用鑑別力較弱的替代量測（總數 +18 精確等於 #947 兩新檔的 18 個 `def test_`，但同時等量增減會互相抵銷）。同族第二格：失敗的 traceback 拿不到（workflow 只 grep `: (FAIL|ERROR): `），**#567 那條現在就卡在這**。⚠ 修法動 CI 行為 ⇒ 待 Eugene；驗收**必須做陽性對照**（故意讓 C5 紅一次，證明 C3/C4 輸出真的出現）。labels: cat-quality-gate / A3-ci-release / priority:medium。Refs #956 #691

> 🔴 **develop 現在是紅的**（08-28 量）：`odoo-tests / full-suite` 在 tip `fc3661e5` 為 **failure**（`2026-08-27T15:37:24Z`，runs/5681），時點緊接 PR #947 併入（15:22Z）之後；`paas-operator-unit-tests` 綠。⚠ `odoo-tests.yml` **不掛 `pull_request`** ⇒ 每張以 develop 為 base 的 PR都繼承一個測試沒過的基準，而 PR 的 CI 綠燈**結構性照不到**。#956 有先前在 PR 分支上做的歸屬（740×3 ＝行為改了沒更新既有測試；567×1 ＝ #947 自帶的紅），🔴 **那是 PR 分支上量的，要修之前必須對 develop 這次的 run 重量一次**，⛔ 不得直接沿用。**誰修 #956 仍待 Eugene。**
> ⚠ issue-leader 併 PR #962（`3376826f`）觸發了 develop 的 odoo-tests 重跑（runs/5755，pending）——該 PR 零 `src/` 改動（觸發原因是 `scripts/audit/*.py` 不在 `paths-ignore` 內），🔴 **若這輪紅，紅會掛在那顆 merge sha 上，⛔ 不得讀成「#962 弄紅了 develop」**；判準＝失敗的測試名是否與 runs/5681 相同，**不是看紅綠**。leader5 已掛 monitor。

> ✅ **待刪遠端分支 `fix/849-related-anchor-projection` 那格已消解**（08-28 issue-leader 量）：該分支 `70a5da79` 雖非 develop 祖先，但其唯一內容 `test_policy_related_anchor_projection_849.py` 的 blob與 develop 版**逐位元組相同**（`a4069caf`），且 `src/tests/__init__.py` 已註冊、baseline 逐 class 有登錄⇒ **刪不刪都不損失內容**；而 repo 慣例是不清已併分支（leader5 量：518/539）⇒ **留著，⛔ 不再列為待裁**。
- **#848** 🆕 access policy 三錨點欄位在 model 層無歸屬守線；非今日線上可利用；Refs #798
- ~~**#849**~~ **已關**（08-27，PR #929）——跨租戶 read 洩漏**與配額 DoS 皆已量測成立**（DoS 於 08-26 在 `dev-sandbox-849-quota` 實測：攻擊者寫投影欄即永久佔走受害者 1 格配額，且租戶側**無任何身分可 unlink**、只剩 superuser；結論已搬進 #849 留言）。⛔ 舊描述（inverse 委派）已翻案勿引用（#940）。殘留：#941／#940／#896＋存量受害列處置。Refs #848 #798
- **#823** 🆕 翻 operator tunnel 歸屬驗證預設值——翻前缺 claim 的 tunnel 仍不受保護
- **#706** 🆕 既有跨 workspace 綁定回溯盤點（`cloud_service_binding_guard` 只擋新寫入）；唯讀盤點，盤完才決定要不要清
- **#787** context-key 偽造家族普查：A 段已交付 enforced，衍生的測試命名問題待開單，B 段由 #783 承接；本單待 Eugene 關或續追

**憑證外洩**

- **cd#24** prod registry 密碼明文躺在 last-applied annotation
- **cd#78** 待輪替已外洩的 Gitea helm repo read:package token
- **cd#117** 🆕 yq 過濾漏防 base64，stg fleet secret 兩值明文外洩；輪替與否待 Eugene 裁決
（**#567／#860 已關**——PR #947，08-27 併；例外收斂含 taxonomy 分層，billing Phase B 不在該 PR scope ⇒ billing 側殘留未追，見 #864）

- **#584** 🔄 §一密碼輪替已裁不用輪；§二 4 條未查證線索仍未處理、單未關

**網路隔離**

- **charts#42** open-design NetworkPolicy 跨 ns egress 洞
- **charts#66** skill sidecar 專屬 egress 收斂未做
- **#514** L2 租戶 ns baseline egress 收斂，design 已併、實作待 Eugene 裁 R1/R2/R3＋D-2/D-3/D-4 後重新認領
- **#428** odoo chart containerPorts.https 未定義 → NP 全 TCP port 開放
- **#427** Phase 2 default-deny baseline NP，前置：待 prod 有租戶後
- **cd#85** 🔄 方向已定「切斷」prod 控制面公網入口，待 #427 NP 上線後執行＋盤點其他 tunnel
- **#774** 🆕 承 #721，驗證 safe_eval 能否逃逸到 OS 層；前提（root／egress 全開）未重驗，待看 #514 進度；驗收需陽性對照

**agent runtime 沙箱**

- **ar#34** sidecar 來源的 tool_result 未標記 untrusted-content（prompt injection）
- **#958** 🆕 #929 守衛關掉受害者唯一自救路 ＋「無存量受害列」的普查只涵蓋 5 個受守欄位中的 1 個（prod 的 0 是無資料型 0）⇒ 存量受害列若存在，今天只剩 superuser 能解、而我們不知道有沒有；pending（處置屬產品／安全取捨）。Refs #849 #941 #929
- **#949** 🔒 CS change-plan 歸屬閘 fail-open（`paas.py:1157` 的 truthiness 前綴讓零 plan template 整段跳過）⇒ 任何 plan 都換得成；SA/SH 已修、CS 是 #750 的殘留。🔴 **「今天可達」已於 08-28 證偽並更正**（issue-leader 開單時只看 `is_active`、漏 `conceal_instances`；`paas.py:1137` 更早 return ⇒ 潛伏非現行洞，已降 priority:medium）。第二處同型 fail-open 在 `paas.py:3291`（create 路徑），已授權一併修。Refs #750 #773
- **#941** `rule.write()` 對真錨點 `policy_id` 沒有授權守衛——`_assert_anchor_move_authorised` 只接在 `policy.write()` 側

## A2 · 金流與計費

> 🔴 **#773** 與 #521 分工：#521＝加購席位建不起來，#773＝CS 降級到無 AC seat plan 後 access policy／CF Access 規則仍生效、建起來後不會被收回（付費界線被繞過）；因收斂未拍板，暫列他分群非危害低

| Issue | 一句話 |
| --- | --- |
| **#521** 🔒 | CS 型 access 加購（`ac_max_access_seats`）——載體缺口已補，剩席數 enforcement，繫於 #381 |
| **#518** | `max_per_partner` 並發防護在 Odoo REPEATABLE READ 下零貢獻（advisory lock 取鎖後仍讀舊 snapshot） |
| **#515** | HAOS 開通前容量 pre-check——上限數字與實際資源脫鉤時仍會超賣 |
| **#361** | 改方案後要讓使用者看到實際退了多少點 |
| **#45** | subscription：stg/prod `end < start` non-trial sub 待 audit + dry-run backfill |
| **#43** | subscription：`.get(unit, relativedelta())` silent fallback 待換成 `UserError` + 抽共用 `cycle_delta` |

## A3 · 發版鏈／CI／部署環境

| Issue | 一句話 |
| --- | --- |
| **#827** | docker-desktop node 共用第三方 addon 副本是另一條版本線（`sh_subscription` 版本落差）——計費相關沙盒綠燈驗的是殘缺 wallet；⛔ rm-rf 刷新禁用，走 cp→驗證→mv 原子換。Refs #814 #563 |
| **cd#70** | prod chart bump ≥0.1.18 前須移除 `values-prod.yaml` 的 `pvcCleanup.image` override（priority:high，否則 CronJob ImagePullBackOff） |
| **ci#66** | 三條 gate-before-build 線待各真跑一次 phase 2 驗收（併 ci#68 一起收） |
| **cd#77** | `SKIP_MODULES_UPDATE` 三段手動流程脆弱＋bitnami 吞掉 `-u` 輸出，prod migration 執行是黑箱 |
| **cd#88** | agent-runtime chartVersion 待追平 0.4.5 ＋ stg 既有 release reap 到 1.4.4 |
| **cd#83** | `paas-odoo/fleet.yaml` 待加 `disablePreProcess`，消滅 promote 時的人工同步點 |
| **ci#68** | 只剩驗收：3 線各一次真實 phase 2＋一次 /reject 清理證明；⚠ 待裁決：/reject 線要不要補 /approve |
| **ci#114** | `cleanup-orphan-candidate-tags.sh` 排除清單讀 open issue 值未驗封緘，評估單無人認領 |
| **ci#115** | workflow run: block SC2086 補引號＋收緊 shellcheck severity；🔴 順序約束：須在 ci#66/ci#68 六線驗收後或同批，無人認領 |
| **ci#48** | `main` 無 branch protection——已查證不會卡死單人自核，開關剩 Eugene 按（🔴 唯一禁忌＝勾 block_admin_merge_override） |
| **charts#44** | `publish-charts.yaml` 的 `set -e` 把 Gitea Helm push 與 OCI push 綁死（transient 500 誤擋 OCI） |
| **#510** | `/woow:deploy-stg` 的 `addon_ref` 是誤導性 no-op——宣告了卻從未進 CI payload，實際永遠烤 develop |
| **#382** | `pvc-cleanup` CronJob 仍用 `bitnami/kubectl:latest`，待換 bitnamilegacy |
| **#472** | `Dockerfile.dev` 已失效——COPY 不存在的路徑，且 base 缺 6 個第三方相依 |
| **#563** | k8s dev sandbox 的 local/hostPath 模式在 Docker Desktop 下不可用；`create.sh` exit 0 掩蓋失敗 |
| **#840** | 三支 seed 腳本（e2e-seed／e2e-seed-reset-button／seed-from-stg）同族沙盒資源名推導寫死、隨 slug 長度變形。Refs #829 |
| **#855** | DB 錨點指向已死 CF tunnel，對帳兩方向結構性不可見＋cron 於訂閱存活時永不觸發；補 tunnel 腿前置 #833。Refs #740 #794 |
| **#857** | `_list_services` catch-all 自己會拋，MissingError 逸出使單一服務失敗升級成整批清單 500；⚠ 修時勿順手收緊 catch-all。Refs #525 #568 #572 |
| **#864** 🔒 | controller except 鏈三格判準全端點盤點——catch-all 對交易正確性的影響（body 依留言 10190 格6 重寫、舊定義存底留言 10199，⛔ 勿再引用舊定義）。Refs #533 #518 #799 #822 |
| **#859** | tunnel 已刪但 fail-loud guard 宣稱它還活著要求重試——清錨點 ACL 失敗被吞成保留待重試＋operator 對 404 當成功。Refs #740 |
| **#862** | repo-lint paths 白名單與 check 實際輸入沒有對帳機制——#846 只補一格、另七個輸入檔仍在白名單外。Refs #846 |
| **#865** | repo-lint job 執行時間單調成長而無人在看；成因刻意不猜。Refs #846 #856 |
| **#834** | #648 的 clear-on-view 路徑（`_refresh_public_url_dns_flag`）零測試覆蓋，壞掉症狀無自然訊號只能靠測試守。Refs #648 |
| **#835** | repo-lint 檔頭條目放到「用法：」之後照樣全綠——gate 斷言清單無「條目所在區塊」檢查。Refs #832 |
| **#837** | test-collection gate 首趟落地後兩筆帳待處理（baseline 檔頭過期敘述／green-streak.txt append 流程待裁）。Refs #546 |
| **#838** | repo-lint `--update-baseline` 一次重產全部 baseline——靜默調降風險缺文件保護，修法三選項列於單內。Refs #832 #835 |
| **#877** | repo-lint 檔頭「第 N 條」互相引用、無 gate 對帳編號——清單插入/重排即靜默指錯。Refs #838 |
| **#731** | 五支唯讀腳本 `--context` 可選 ⇒ #32 把「靜默打對」換成「靜默打錯」；待裁決收斂方向 |
| **#722** | onboard profile `KUBE_CONTEXT`＝`docker-desktop`，`create.sh` 不帶 `--context` 靜默打到本機叢集；與 #32 相乘非重複 |
| **#714** | filestore-hashdir-guard 刻意 fail-open，四個 marker 只印 initContainer stdout ⇒ 需接告警；僅平台 paas-odoo，租戶側走 #702 |
| **#956** 🔴🆕 **develop 的 odoo-tests 自 PR #947 起轉紅**（`c8685e27` 綠／`fc3661e5` 紅，回歸點乾淨）——`test_sh_unlink_authz_preflight_740` 三條斷言舊的「技術訊息洩漏」契約、未隨收斂同步更新；test-only 修法。⚠ 又一個 #911 缺口實例。Refs #947 #911 #740 |
| **#957** 🆕 對**執行中**沙盒用 `rm -rf` 刷新掛載路徑會打斷 bind-mount ⇒ 模組 not installable ⇒ **0 條測試卻 exit 0 全綠**；判別式＝逐 class 數 `Starting` 條數。Refs #563 #814 #943 |
| **#951** 🆕 `--image` 隱含 ci-local，但 pull secret 與 `imagePullSecrets` 兩處都被 `mode=="stg"` guard ⇒ 私有 registry image 必 ImagePullBackOff；🔴「我這台 docker 拉得到」對「叢集拉得到」零鑑別力（不同憑證域）。修法機械可判＋須同批改 `.sandbox.yml` 的過期敘述。Refs #814 #829 #563 |
| **#886** | docker-desktop node 沙盒 hostPath worktree 副本無清理機制——孤兒清單隨每次收尾持續漂移，需各原主認領 |
- **#911** i18n／改形批次的 merge gate 需 pre-merge 全套 odoo-tests 實跑證據（#903 兩紅前科的制度化）
- **#921** `docs/index.md` 同步無 gate，手改會無聲漂移——`gen-docs-index.py --check` 應接進 repo-lint／CI
- **#926** `i18n_python_ledger` 零 CI 執行、零自測，且 repo-lint 非 required——接進 repo-lint 並裁是否升格
- **#939** i18n 抽取缺口（Python 側）：巢狀在具名引數裡的 `_()` 不會被 babel 抽成 msgid，ledger docstring 的「唯一形態」主張已被證偽
- **#943** `create.sh` 的 hostPath parity gate 拒絕建立時仍 `exit 0` ⇒ 看 `$?` 的自動化會誤判成功
- **#948** 🆕 vendored `payment_ecpay_ecpg` 停在 18.0.0.3.0、上游已 18.0.0.3.3 逾三月——差的正是孤兒 callback 的 dead-letter 稽核；prod/stg 皆 0 筆交易＝上線前可觀測性缺口非既有事故。追平與否待裁（pending）。Refs #864 #827
- **#944** `prod-release-sop` §5.1.2 的 PR-A/PR-B 兩段式已被 cd#77 的 module-version-gate 取代，該段沒回頭更新——⛔ 別照那段做

## A4 · 功能缺口／bug／UX

**壞掉的功能**

- **ar#23**（agent-runtime repo，非本 repo 編號，未列入 open-issues 清單可核）LLM tool-call 一律傳空 kwargs，skill 執行時 input 參數整個遺失
- ~~**#155**~~ **已關**（08-28）：stopgap 已實作＋已上線＋線上實測 `exp-iat=604800`（7 天）⇒ 原症狀不成立。⚠ 未做端到端「401 消失」實證。殘留拆三張：**#953**（chart 無 env 孔 ⇒ CD 設值 silent no-op，三份文件皆與實作矛盾）／**#954**（runtime 零 refresh；殘留形狀窄化為「連續活躍逾 7 天」，因 06-14 idle reaper 落地）／**#955**（F-GC-1 severity 重評欠帳 2.7 個月）
- **#316** stg 控制面 MCP tool sync 疑被 baseline default-deny 擋（`state=error`／`tool_count=0`）
- **#207** operator mcp sidecar route 的 deploy/delete 命名不對稱 → 有 sidecar 的 CloudService 漏 mcp CF route
- **#388** `agent_session` spawning→active 未轉：spawn webhook 200 但 state 停在 spawning
- **#562** postgresql／redis template 重新上架前必須補 image override（Bitnami 搬遷後預設只剩 latest）
- **#655** paas-operator `helm.py` 把 PVC quota 寫死固定值，未走 `_compute_ns_quota()`——多台 HAOS 同 workspace 會撞上限；K8s hard quota 無「不限制」語意，要不限只能省略該 key
- **#739** 租戶經 `/woow` 的 Smart Home provision 從來沒有成功過（`tunnel_token` 缺 sudo，`_provision_smarthome` 撞 `AccessError`，卡 `state='provisioning'`）——PR #797 已修復並 merge，**issue 仍 open，待 Eugene 關單或說明殘餘**

**資料一致性／並發**

- **#549** reach reconciler 的 cron 路徑同樣 lock-then-recount（#518 機制同族）
- **#363** reset saga 並發：client 逾時後重按會與 in-flight saga 競態，rollback 可能覆蓋較新密碼
- **#342** change plan 後 CS 規格 re-apply 殘留
- **#702** 租戶 `svc-*-odoo` root-owned 資料目錄唯讀掃描器（Eugene 已裁先偵測不預防）——只建掃描器不建守衛，判準只認 owner（mode／group 恆綠），讀 `/proc/1/status` 取真實 uid
- **#760** CS Access policy `create()` 打 2 次 operator sync（policy＋rule model 各一次），CF 上多建一條孤兒 policy；sync 第二次失敗即永久孤兒（非 blocker）

**migration 地雷（舊 DB 還原會炸）**

- **#575** `.37` migration 的 fan-out 對 pre-`.39` DB 撞 `_check_at_least_one_include`

**UX／前端**

- **#758** ServiceDetailPage 只在 `onMounted` 載資料，SPA 內從服務 A 切到 B（同 component 不重 mount）整頁沿用 A 的舊資料，影響所有頁籤；無資安面，修法方向已給
- **#272** 🔒 Security Access「Add Policy」改為分情境淺顯選單
- **#36** change-plan UI for SH + CS（modal、trial→no-trial 確認）
- **#57** sidecar retire 後 cloud-scope MCP 缺 UI affordance（不知去哪重建）
- **#56** Project list 顯示不可訪問的 demo workspace card（admin 點進去 access denied）
- **#52** project-detail UX hardening + error handling
- **#391** `service_type` 殘段：backend view 依 type 顯隱＋`mcp_integration` 僅 SH 可非空 constraint

**環境／命名**

- **#163** F-SA-2 環境前綴命名慣例 `paas-stg-*` ＋ SA route-sync 單一 domain source of truth
- **#162** F-SA-1 stg Odoo `paas_domain` 應改為 `woowtech.io`（`*.stg.woowtech.io` 無 SSL，跨服務）
- **#384** F-NETPOL-1 reach-ws label 反向 GC sweep——agent teardown 後 grant 殘留
- **#894** CF 已預告單筆 tunnel GET 的 `connections` 將回空陣列，而 `tunnel_status` 100% 由 `len(connections)` 判定——外部期限風險
- **#914** `/woow` 前端顯示時間有時區偏移——後端 naive UTC 字串被 `new Date()` 當成 local time（台北下差 8 小時）
- **#918** 4 處裸 `t-esc` 把後端日期原始字串直接印給租戶（SecurityAccessDetail ×3／McpServersSection ×1）
- **#920** ECPay 綁卡表單的 UI 語言寫死 `zh-TW`，不跟隨 Odoo 使用者語系（⚠ prod 已有 en_US 租戶）
- **#945** `/web/signup` 自助註冊繞過 `ir.default`——新帳號語系來自 `portaltemplate` 樣板（`copy()` 不是 `create()`）；Eugene 已裁不改樣板、開單追
- **#946** `zh_TW.po` 覆蓋率缺口——68% msgstr 仍為空，含 4 張核心維運表單使用者可見的英文
- **#942** #833 修完後的兩格未量測（既存記錄 DB↔CF 對帳、CF 軟刪保留期）＋ teardown 重試收斂機制只活在 docstring 裡

## A5 · 技術債／重構（有餘力再做）

- **#415** 收斂 HA provision 與 marketplace 兩套平行的 CloudService 建立邏輯（已漏鏡射兩次）
- **#509** PR#505 review follow-ups——EnvVar replace 分支盲區＋gate 獨立守線檔＋文字債
- **#668** #657／PR#666 覆核衍生三項，皆已實測確認非現行可利用洞（刻意不修）：grantor lookup 排序非權限高低；攻擊測試的可達性陽性對照與攻擊斷言分離；`invited_by_uid` 可被 context 偽造（事故追查時不可信）
- **#84** skill `is_latest` drift fix + e2e（PR #81）
- **#50** 全 repo API URL pattern 統一到 cloud_service nesting
- **#25** 統一 SH/CS controller 的 role check pattern（抽 `_require_admin_role`）
- **#319** Longhorn SC 策略：default SC 維持 `longhorn`(Retain)、租戶 PVC 於 chart values 明示 `storageClassName: longhorn`；剩餘＝Released PV 清理 SOP＋backup target。🔴 硬約束：多數 Released PV 屬其他部署絕不可動；default SC 是叢集級，動前須與叢集運維協調
- **#396** orphan PVC GC 搬進 paas-operator，去除 chart cronjob 的 shell-image 依賴（非緊急，屬技術債）
- **#901** repo 零 CJK 守線 ⇒ 修好的英文 source 可被無聲寫回中文；判準不可只用基本 CJK 區塊（全形標點／破折號等會漏）；第一個測試必須注入紅燈；Refs #107
- **#896** `_check_policy_quota` 數池子的 `search_count` 無 sudo——ir.rule 濾掉寫入者看不到的列 ⇒ count 偏低、配額守衛靜默放行；與 #849 方向相反，可達性未驗；Refs #849
- **#890** 租戶可見 ValidationError 硬編 CF 席位常數（`security_access_access_policy.py:536-549`）——今天是對的、過期時零 gate 會紅；修法 pending，繫 #381 席位模型拍板；Refs #521
- **#870** workspace 三錨點（slug/ws_hash/owner_id）共用 su+group_system 雙閘豁免；Eugene 已裁＝slug 單獨改 module-private sentinel（比照 #798 形狀），owner_id/ws_hash/binding_guard 不跟；今天無任何 slug 事後改值路徑＝非現行洞；等恢復派工排程
- **#915** 金額顯示改用 `formatCurrency`——`topup_plan.amount` 需先 Monetary 化（`wallet_topup` 已具備條件）
- **#916** `/woow` API 的日期序列化不一致（其餘 controller 的統一 ＋ 後端側守線）
- **#950** 🆕 `change_subscription_plan_wizard` 後台點不到但 ACL 對所有內部使用者全開（無 ir.rule／`new_plan_id` 無 domain／action 無擁有權檢查）；⚠ 租戶（portal）碰不到，屬內部權限過寬。待決＝該刪還是收斂 ACL，判準應與 #735 一致。Refs #773 #735
- **#940** #849 已翻案的「inverse 委派」機制敘述仍留在 3 處程式碼註解裡——包含守衛本體所在的檔

# ③ 需討論類 — 需求／策略未定，別自行開工

## 政策與商業規則（先決定「要什麼」）

- **#323** 使用者主動取消訂閱不退點、不拆服務 —— 與「刪除服務退點」行為不一致，要決哪個對
- **#589** unbound project 存取判準在 4 條 ir.rule ＋ 1 個 controller helper 之間分裂（follower-only vs follower OR create_uid）—— 要決哪派對
- **#667** `project_access` 成員資格不蘊含 `project.project` 記錄可讀（連 admin 成員都讀不到）—— 要決是否為預期行為：否則需加成員 rule（⚠ Odoo 核心模型、爆炸半徑大）；是則需明文寫進 docstring
- **#152** dedicated `agent_template` 的 RPC 寫入是否應限縮為 admin-only
- **#381** 🔒 AC/CF end-user 認證策略 —— end-user 走應用層／service token？Access seat 留管理者？（含 CF 成本懸崖）
- **#394** SA IdP 限縮 Google+OTP 的歸屬（#272 未明文）
- **#735** `transfer_ownership` 要復活還是刪掉？死碼且就算呼叫也走不完，兩道獨立缺陷須一起處理，只修一道換個地方死；⚠ 與 #732 修法會打架，復活前先看 #732 裁決
- **#711** 同一動作兩層界線不一致，要決對齊哪邊：sudo 產品端點 `_create_project` 只驗成員資格不驗 role，繞過 #705 guard，任何成員含 guest 可綁；4 個互斥選項未拍板。判準見 `docs/decisions/patterns/two-layer-acl-lockstep.md`
- **#773** 🔒 CS 降級到無 AC seat plan 後，既有 access policy 與 CF Access app／policy 繼續生效 —— 要決收不收斂、收到什麼狀態、CF 同步方式、是否與 #381 同輪；⚠ 歸類兩可（可能屬 ②A2）
- **#873** seed 對席數變體 plan 無條件寫試用欄位、不看 base，可能讓變體購買拿到未標示的試用期 —— 要決三選一（變體繼承 base／API 據實顯示／不一致 fail-loud gate）。Refs #36 #367

## 架構方向（做法有兩條以上路）

- **#578** 🔄 要決對外 API 契約要不要改 —— rollback 是帳本失準路徑之一且方向相反，修它須先改 operator `rollback_release` 的 `response_model`；3 個待決點未拍板
- **ar#33** 🔄 sidecar 內 bash 與 bridge daemon 同 uid，可對 PID1 送 SIGTERM 觸發 drain（PID namespace 隔離不涵蓋 network）—— 要決修掉還是明確接受並記錄
- **#770** SH 第二條 route（使用者自建對外服務）—— 含 `dns_failed` 失敗模型重設計、HA component 跨 repo lock-step、quota 語意拍板；前置 #768 已關單不再阻塞，需求時間點未定
- **#681** issue tracker 與 `docs/tracking/` 是兩個互不相通的查重檢索面 —— 查重無法掛 hook 成 gate；4 個提案未預選方向，要決接受現況還是投資做工具
- **#383** F-ENV-8 admission 層補強（Kyverno/OPA）當 RBAC backstop —— 要不要引入
- **#276** [Spike] 評估「一 workspace 一 namespace」合併（現況 CS per-workspace + agent-runtime per-project 並存）
- **#385** CF tunnel 累積清理評估 —— 孤兒候選待決清哪些
- **#710** `project.project` 唯一 ir.rule 的 `perm_unlink=False` 不擋 unlink、ACL 對 `base.group_user` 全開，guest 可 `unlink` project —— 要決「誰能刪 project」的界線，2 條修法未拍板；🔴 今天就能被利用
- **#704** `use_agent_runtime` 欄位無 `groups=`，可經 `call_kw` 直接 `write` 繞過 controller 閘 —— 要決該用哪種擋法，3 個選項未拍板（含要不要建整類通用 gate）；🔴 今天可被利用
- **#703** operator `POST /{hash}/sync-workspaces` 對 `project_workspace_ids` 零歸屬驗證就拿去渲染 NetworkPolicy —— 要決 operator 該不該自己也驗歸屬，3 條路徑未拍板；⚠ 動 operator 合併前必須本機跑真 K8s integration tests
- **#696** cloud_service backend view 的 `groups` 未比照 security_access（同 repo 不一致）—— 3 個選項未拍板（含要不要固化 gate）；⚠ 單裡的檔案清單是粗篩、查證後才能定案
- **#721** 要決要不要開放租戶自帶 Odoo addon（唯讀 spike）—— 租戶已有 `base.group_system`，`safe_eval` 的 `env` 是本尊；🔴 最關鍵未查證前提：`safe_eval` 能否逃逸到 `subprocess`／`socket`
- **#799** 要決要不要把每一趟 operator 推送拆成自己的回捲單位 —— 等於放棄 policy `create()`／`write()`／`unlink()` 現行整批回捲保證、改成 partial success；原文「要先拍板、範圍遠超一張單」；與 #794 同根；⚠ 若視為第 2 層實作選型則進 ②A1

## 範圍未明 / 卡別人

- **#412** open-design 體驗殘餘項（zsh 進 image／claude 更新策略／刪除語意明示／orphan home PVC 救援 SOP）
- **#267** Hermes MCP cloud service 對外暴露 Gateway（一 tunnel 多 subdomain）
- **#453** Vibe Kanban host 加掛 OpenChamber（承 #424③，等 Elmo 定位）
- **sh#9** HA integration 新增 MCP server 子網域 URL 與 proxy token sensor —— ⚠ 標題寫 blocked-by 主 repo #270，但 #270 早已關閉、前提不成立，現在卡的是「要不要做／誰做」

# ①b 固化自動驗證 — 暫緩，不投 token

> **政策：先問 Eugene 要不要做，並附成效評估。** 這些是可程式化、跑一次就永遠替你重查的守線，**價值在防退化，不在推進上線**。**解凍時機：實作類的 A0/A1/A2 清完，或某條真的擋住上線時單張破例。**

**成本刻度**：S＝一個 session 內／M＝一天／L＝跨多天。

## 乙 · 測試品質／守線形狀

- **#844** repo-lint test-collectability 判別式（helm trunc 機制）無耐久載體，落地形狀（golden fixture）與待定項未拍板；⚠ golden 只守腳本側、chart fullname template 改動偵測不到（Refs #829 #563）— **M**
- **#868** repo-lint 三判準宣告鏡像執行期、實際只掃頂層（tree_facts／test_tags 只收頂層）；現樹 0 苦主，但執行期側未做同等級 exec 驗證 ⇒ 同族不等於同強度（Refs #850 #836 #862 #684 #356）
- **#572** 直呼被測碼寫的「無殘留／有回捲」測試會失去鑑別力（回捲的是測試框架本身，#520 家族大量產出此型）；19 條候選已分類，D 組 2 條確認有風險；歸類兩可（核心產出偏實作 vs 掃描 recipe 固化成 repo-lint 規則）— **M**
- **#438** repo-lint A 軌：AST 規則禁止 `assertEqual(len(<seed corpus>), <int≥5>)`，與已落地 B 軌 `check_seed_topology` 互補；⚠ 必須三段 AND，只看 AST 形狀誤報率約 90%，原型不在 repo 內、實作等於重寫（不建議優先做）— **M**
- **#777**（承 #716 附錄 G1f）`cloud_service_binding_guard.create` 的 key gate，`default_get` 三來源中 ③ 欄位自身 `default=` 與 ④ `precompute=True` stored compute 兩條 leg 零測試；G1f 探針曾未命中 RPC 走的路徑；今天五個相關欄位皆無此設定 ⇒ 現為 test-gap、非 security bug
- **#874**（#850/#836 衍生）repo-lint C2/C3 兩層都算不到 non-def 形態（賦值/setattr）造成的少算；與 #868 同族異形，未拍板修法（Refs #850 #836）
- **#884** `gen-docs-index.py` 產生器與 `--check` 共用 `git ls-files` 口徑 ⇒ 未追蹤的新 doc 兩邊都看不到；提案＝掃到 docs/ 未追蹤 .md 即 fail-loud

## 丙 · 跨 repo 的 CI 缺口

- **cd#76** operator chart 的 prod/stg 版本漂移無任何檢查（chart 原始碼在另一 repo，發版盤點看不到；只 promote image 不升 chart ⇒ 租戶付款後開通必然失敗且零訊號）；規則只是比對兩個 `fleet.yaml` 同一欄位，同型範本已有 `manifest-version-gate.yml`；⚠ 歸屬 `woow-paas-cd` agent，本 repo 只出規格，只需 warning 不 block — **S（跨 repo）**

## 丁 · memory 稽核工具

- **#595** `memory-integrity.py` 的 silent-truncation 有結構性永久盲區，且與「還沒有快照」輸出不可區分；3b（把兩種未涵蓋分開列名、不改判準）比 3a 重要，是本段成本最低一張，與 #683 重疊 — **S（3b）／M（3a＋3c）**
- **#683**（Refs #595，⚠ 一句話已過期）①②已部分落地（baseline PR #685 上線；hook PR #690 只掛 `--links`，truncation 觸發點仍缺），③ 高誤報率會讓工具自我瓦解仍在；⚠ Gitea 留言只記 #690 沒記 #685；先做 #595 3b 再決定要不要清帳 — **M（剩餘部分）**

## 戊 · 一次性實測，不產生持久 gate

> 三張不是固化 gate、跑完就沒了，省 token 機制≈無 ⇒ 放在本段名不副實；唯一例外是 #386 第 2 件。（#390 已於 2026-08-25 關單＝#747 的 stg e2e 已逐項驗掉）

- **#387** SA 層 MCP 協定整合 e2e（tool 呼叫／token 流通），既有 E2E 只蓋網路層 — **M**
- **#386**（承 #315）三件：operator SA `can-i` preflight／chart-side RBAC test／`KubectlException.stderr` 進 log；唯一可重複驗證的是第 2 件，建議拆單 — **M**
- **#393** 三 MCP（＋#375 新增三型）規格實跑效能觀察，#268 調規格時只驗 healthz+GUI 200；要等真實負載才能做 — **S（但要等真實負載）**
- **#938** 譯文 sink 掃描——370 條已翻 `_()` 訊息中，368 條未確認是否流向非顯示用途

## 己 · 組裝對帳補列

- **#44** sh_never_expire fixture variant（成效評估未做、成本未估）
- **#46** test nits，純 cosmetic，不產生新驗證能力，省 token 機制≈無；建議改列 ② 實作瑣事（成效評估未做、成本未估）

## 建議優先順序（若 Eugene 只肯投 2–3 張）

- 判準：**existing_infra 已有半套的優先**——成本落在「接上去」不在「發明」。
- **第一順位：cd#76** — 全段成本最低而後果最重；⚠ 執行歸屬 `woow-paas-cd` agent，本 repo 只出規格。
- **第二順位：#496 或 #595 的 3b**（只改輸出、一個 session 內可完）— ⚠ 見 concerns：#496 已關單，此建議前提可能已變動。
- 刻意不入前排：**#438** 原型不在 repo 內、實作等於重寫。

# ①a docs 文件知識類 — 暫緩，不投 token

> 政策：先問 Eugene，別自行開工（Eugene 2026-08-12 裁定）。解凍時機：A0/A1/A2 清完，或某條真的擋住上線時單張破例。

- **#582** 四條測試方法陷阱，待 promote 進 `docs/tracking/pitfalls/` —— 純粹沒人搬
- **#508** PR#507 合併後註解漂移（3 處）＋2 條維運觀察 —— 註解與同檔測試矛盾；⚠ 兩可，OBS-2 清理 cron 若定案動手屬 ②
- **#392** 手動禮品卡換點 SOP 文件化 —— 有範本可抄（`docs/guides/smart-home-half-year-gift-points-admin-sop.md`）；⚠ 真有人發卡時變阻擋項
- **charts#43** `charts/open-design/Chart.yaml` description 仍寫舊 auth 語意 —— 僅 metadata stale，下次 chart bump 順手改
- **#421** 07-23 進度同步會議待辦分類 —— ⚠ 混合單，kubelet max-pods 部分屬⊘（叢集級，交叉 woow-machines-operator）
- **#699** 兩處 docstring 宣稱過期（`cloud_service.py` extra_vals 語意 / `test_workspace_authz_658.py` X13）—— 修法須寫成指得出證據的範圍限定敘述，指不出來就刪
- **#701** `tea` 寫入坑知識雙載體不同步（memory 持續在長，docs 凍結於 08-05）—— docs 版是嚴格子集，無示警
- **#741** `paas-workspace-member-acl-tightening.md` 貼著已退役的 rule 當範例 —— 🔴 該 doc 被 pattern-guard 列 acl-security 必讀，導人到已知有洞的範本
- **cd#112** `promote-acceptance-checklist.md` §3 判準把「版號落差」與「有 migration」混為一談 —— 正解：判準改比對 migration 行數與發版單是否一致
- **cd#113** `sop-cd.md` §8 坑3 的 prod/stg tunnelId 標「未經本輪查證」永不提醒去查 —— 待唯讀查出、收斂敘述並寫明查法與日期
- **#878** `harness-distillation.md` 的「三 check」量詞停在 07-06 落地快照 —— 需先裁 doc 身分（凍結加註 vs 改列名/指針）；#838/#877 同族、pending

## ⚠ 歸類待 Eugene 裁決

跨 ①a／①b／② 三段；主筆判斷不了、需一句話定案。

> 這裡列的以外，另有一條 2026-08-18 舊註記提及「兩可 4 張：#567／#750／#383／#412」，與本表當前 9→8 張的內容對不上（#750 已關、其餘三張也不在下表）——保留原樣待查，見 concerns。

| Issue | 目前放在 | 另一個合理歸屬 | 兩可理由 |
| --- | --- | --- | --- |
| **#572** | ①b | ② 實作（或拆兩半） | 核心是分析＋修 D 組兩條測試（實作）；只有 AST 掃描 recipe 固化成 repo-lint 規則那半才是 ①b |
| **#46** | ①b | ② 實作瑣事 | 純 cosmetic，不產生新驗證能力 |
| **#387** | ①b | ② 實作 | 一次性 stg 協定層 e2e；#316 可能是前置 |
| **#386** | ①b | ② 實作＋①b（建議拆單） | 三件混合：preflight／stderr 進 log 是實作，chart-side RBAC test 才是可重複驗證 |
| **#393** | ①b | ② 實作／觀察排程 | 一次性效能觀察，要等真實負載期才有意義 |
| **#706** | ② A1 | ③ 範圍未明 | 本單只做唯讀盤點歸②；但盤完要不要清、要不要 migration（牽動 #691）全未拍板 |
| **#702** | ② A4 | ② A5 | 建的是預防性偵測工具非修壞掉的東西；歸 A4 是主筆推論非內文自陳 |
| **#714** | ② A3 | ①b 固化自動驗證 | 產出是 prod 部署環境告警機制（歸 A3），但「防退化」價值與①b一致、只差載體是 K8s 監控 |

> ⚠ ①a 段 #508／#421 亦歸類兩可（另一歸屬 ② 實作／⊘ 非 paas 管轄，皆部分），理由已併入 ①a 各自列。

---

# ⊘ 非 paas 管轄 — 內文自陳執行歸屬不在本團隊

> 收兩種：(a) 內文自陳執行歸屬 woow-machines-operator（前 6 張）；(b) 執行歸屬在別的 addon repo（#725 屬 `sh_subscription`）。平台側改不動、只能繞過它防守，留在本 repo 當協調載體，排工作時直接跳過。#421（①a）／#584（③）為混合單，平台側有一塊，不移出。

- **#512** 5 個 prod Odoo 客戶站無備份 CronJob —— ns 全不在白名單；與 #440 不重疊（另一批租戶）
- **#441** kasim-odoo-data volume fsck —— 同判準；tenant 為 elmo agent 直接建立，非 PaaS 平台開通
- **#442** 備份健康監控改驗 dump 非空 —— 對象 18 家客戶站，不含 paas-system
- **#440** 客戶 DB 備份鏈失效事件（14/18 tenant）—— 本體已復原，衍生 #441–#444 全還 open，不建議關單
- **#443** 租戶 DB 備份異地副本規劃 —— ⚠ 範圍含平台主 DB，真要做時平台側有一塊
- **#444** R-3 NetworkPolicy 新 pod 編程空窗評估 —— label ops
- **#725** `sh_subscription` `/renew-subscription`（auth=user）任何登入者可 sudo 無限鑄造新訂閱 —— 執行歸屬 sh_subscription（Eugene 08-14 裁決開在本 repo，免線索斷在跨 repo 邊界）；#519 守衛判準即照此缺口設計；`/cancel-subscription` 同族嫌疑未查

---

# 疑重複

目前無疑重複（ci#30 已關，superseded by ci#66；08-14 那輪 12 張新單查重結論「無新增」，#721 vs #694/#702 非重複）。

# 現況（會過期，用前自己量）

- 量「develop 現況版號」的方法：交叉兩條獨立路徑（`tea api` branches ＋ contents API 解碼 `__manifest__.py`）對照 `git ls-remote origin develop`；⛔ 不要用本機工作區或本地 ref 當現況（30+ worktree 共用 `.git`，ahead=0 不代表同步）。當前版號見本檔頁首。
- 平台 paas-odoo 無 stg 對應環境。
- **#521**（AC 席位 enforcement）：grandfather 量測已完成，prod/stg 皆量到 0，但四種限定不可互相合併引用（詳細數值與逐字輸出見 `.scratch/521-measure-report.md` 及 issue 留言，不重複於此）。兩條口徑已於 08-26 裁定（#521 留言 9960）：P-J 納入 bypass；報表分母只算 in_progress（⛔ 不得外推成 enforcement 只作用在 in_progress——那題沒問沒拍，#895 守衛維持不分 state）。Q0（sudo 計數 vs 租戶身分計數是否一致）尚未量測，需能起 k8s dev sandbox 的 teammate 執行。持久載體：#381 留言 9640／9687／9965；衍生單 #924（不阻擋，已開，指出既有 `_check_policy_quota` 缺 sudo 對照）。決策點 3（quota=0＝unlimited 為合法狀態，非違規）與決策點 4（既有超標 record＝一律擋，#521 留言 9960）已拍板，見本檔頭段。

## ⚠ 2026-08-27 修剪時浮出、待 Eugene 或人工核實

這些是削減過程中發現的矛盾／斷線，**沒有自行決定**，列在此等處理：

- ‼ **#36** 派工政策段記「已收官」但 issue 仍 open（掛 launch-first）—— 待關單或更正記載。
- **operator 側 #826／#832／#839／#833 四張已關的單，是否已隨某次 operator release 真正帶到 prod** —— 原追蹤段落被刪，若尚未帶出會失去線索。
- **#804／#853「local 沙盒已知環境紅」是否真的修好** —— 兩張衍生單都已關，但不確定是 bug 修好後關單還是流程性關單；決定這條規則性知識該刪還是該留。
- **#833 被判定為 closed，但條目裡完全沒有解法敘述** —— 疑似誤關或重複單，值得覆核一次（#942 正是它的未量測殘留格）。
- **#833 條目提到一張「還沒編號的新單」（CF tunnel GET connections 將回空陣列）** —— 該條目已刪；查 #894 是否就是它，若是則已有編號。
- **`ci#48`「唯一禁忌＝勾選 `block_admin_merge_override`」** —— 綁在可能已關的單上；若該規則其實該轉正為平台鐵律，需人工判斷是否抽出獨立成條。
- **「建議優先順序」裡的第二順位引用了已關的 #496** —— 是否整條拿掉、只留 #595 的 3b。
- **「兩可 4 張仍待裁決：#567／#750／#383／#412」這行與現行「⚠ 歸類待 Eugene 裁決」表對不上** —— 疑為更早一輪的殘留記載，確認還有沒有意義。

## 📐 本輪修剪紀錄（2026-08-27，issue-leader，Eugene 授權）

- **做法**：清逐輪工作日誌（16 塊）＋ 濃縮分群表（每張 issue 一行）＋ 只留 open issue。
- **規模**：103,883 → 31,431 字元（**削 70%**）；482 → 422 行。
- **全文存底**：`docs/archived/paas-issue-memory-2026-08-27-pre-trim.md`（削錯可逐段撿回）。
- **驗收**：152 張 open issue **全部涵蓋、零遺漏**；零筆已關 issue 以條目形式殘留（機械比對 `tea` open 清單）。
- 🔴 **搶救回 18 張**：`#894 #911 #914 #915 #916 #918 #920 #921 #926 #938–#946` —— 它們先前**只活在被刪的日誌區**、從未進過分群表，直接照濃縮結果組裝會靜默消失。
- ⚠ 標題的「N 筆」一律移除，**沒有換上新數字**——換新數字等於替沒做過的清點背書；要數量請自己對 `tea` 重算（重算法見頁首）。
