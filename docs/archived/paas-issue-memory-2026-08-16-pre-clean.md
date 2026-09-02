# PaaS 上線推進看板（2026-08-15 對帳）

> **資料來源**＝`tea api` 分頁抓完六 repo 全部 open issue，並與各 repo 物件自報的 `open_issues_count` **兩路對照且逐 repo 相符**：
> `woow_paas_platform（main）119・paas-odoo-ci（ci）9・paas-app-bundle-cd（cd）8・woow-paas-charts（charts）4・paas-agent-runtime（ar）3・woow_paas_smart_home（sh）1`，共 **144 筆**（2026-08-15 實查值）。
>
> **重新推導方式（給推導方式不給死數字，下次自己重算）**：
> `tea api -X GET "/repos/{owner}/{repo}/issues?state=open&type=issues&limit=50&page=N"` 逐頁抓到「該頁不足 50 筆」為止，
> 再與看板表格**第一個 cell** 裡的 `**#N**` 取集合差（只抓第一個 cell，才不會把內文引用的「承 #503」「Refs #66」誤當看板項目）。
> 三個會讓你拿到錯數字的坑：
> 🔴 **`limit` 會被 server 端截到 50**——帶 `limit=100` 也只回 50，不逐頁抓會**靜默漏抓**（08-14 開 #722／#723／#724 那次就是這樣漏查 #32／#510／#563，三張都是事後補查才建立關聯）。
> 🔴 **不帶 `type=issues` 會把 PR 一起算進來**。
> 🔴 **六個 repo 的 owner 不同**——`sh` 掛在 `ha-components`，不是 `woow-paas`；owner 猜錯回 **404**，而 404 跟「這張 issue 不存在／已刪」在輸出上完全分不出來。
>
> **本次逐張對帳結果（非只比總數）**：看板待辦 **144**（另有 4 張**已結案但刻意保留一行標記**的：#519／#583／#694／#32，標記形式 📕 或 `~~#n~~`，**不計入待辦**）；實抓 open **144** ⇒ **漏列 0、多列 0**，各分段標題數與實際列數亦逐段相符。<br>🔴 **對帳的 live 快照有效期是分鐘級，不是小時級**——本輪四位 leader 併行，我在同一次作業裡三度拿到過期快照（#32 已關我以為漏列、#519 已關我的清單還說 open、#583 被 merge 我還在寫「待覆核」）。**寫入前重抓一次**，不要用流程開頭那份。
> 實抓 open **142** ⇒ **漏列 12 張、多列 0 張**。12 張已於本版**逐張讀 issue 內文**後分類插入；
> 4 張已關的（**ci#30**／**#676**／**#687**／**#693**）已移除或壓縮成一行。
> 探針的陽性對照：同一支 diff 的 `only_board` 恰好就是那 4 張已知已關的，證明它抓得到差異、不是恆綠。
>
> ⚠ **與 08-12 版（記 125 筆）的差額刻意不逐張回溯**——這段期間多位 leader 各自增刪過看板列（#700／#701／#717／#718／#722／#723／#724 都是 08-12 之後補進來的，而檔頭數字沒跟著動）。
> **不要拿本版數字去減 08-12 的數字推導「新增了幾張」**——那會得到錯的值。要對帳就照上面的推導方式重抓一次。
>
> **本版的分類變更**：12 張新單依 5 類原則歸位（明細見各段的 08-14 註記）。**#698 的 7 張衍生單已全部分類**，
> 原本 A1 段那條「尚未逐張分類進本看板各段」的警語已作廢。
>
> **2026-08-15 leader2 增量（未重算總數，要總數請照上面的推導方式重抓）**：`ci#102` 已關（PR `ci#106`，`dab2c3b5`）⇒ 移出待辦、比照 #519 等保留一行 📕 標記；`ci#107` 為 08-15 13:21 新開（在本版對帳快照之後）⇒ 新增一行進 ①a 段並已由 leader2 認領。
> **2026-08-16 leader4 增量（未重算總數，要總數照上面推導方式重抓）**：#521 載體五票 **#743～#747 全數完成關閉**（PR #749／#752／#755／#757＋stg 真 CF e2e 驗收），#521 本體仍 open（剩席數 enforcement 等 #381），該列已就地更新；**發版 v18.0.1.0.146**（paas-odoo image 20260816.0855 已 build+push，經 ci#117 人工核可閘——⚠ 該閘 deploy.md 未記載，見 #761；**prod rollout 未做**）。期間新開 open 票五張 **#750／#753／#758／#760／#761**（0=unlimited 收尾／SA anchor 繞過／SPA 殘留資料／policy 雙重 sync／deploy.md 文件缺口），尚未插入分段，重抓可見。

## Leader 工作原則

- Leader 一次只能做一個 issue 或是有同樣相關的 issues ，不能排程無關的工作。
都完成後，須要 git pull 並 /clear 後才可以處理新的 issue
- 若取用時需標記已認領，讓其它 leader 知道有人在處理。完成記得標記完成。
- `## 🔒 認領中` 章節中記錄正在處理的 leader - issue. 若 issue 也請 leader 在下面的 issue 中標記完成。
- 不要把這個 note 當做工作時的記憶區，這只能放 leader 的領取記錄和各 leader 目共享的 note。若需要工作記錄簿請 leader 自行開 create note 記錄。
- **動這份 note 一律用 `maestri note edit`，且 old text 取到足夠長**（長到能唯一定位、也長到別人一改就匹配失敗）——**edit 的精確匹配是本 note 唯一的並行保護**：別人先改過，你的 edit 會回 `old text not found` 而**什麼都不動**；`note write` 沒有這層保護，會**靜默**吃掉別人剛寫的內容，且輸出與正常成功完全相同。（2026-08-14 leader1 與 leader4 同時要改 #519 那列，leader1 的 edit 被擋下才沒有覆蓋掉 leader4 的更正）
- **多 PR 併發時，CI 的綠燈只對「它跑的那一刻的 base」有效**：PR 開著期間 develop 每動一次，`manifest-version-gate` 那類綠燈就過期一次，**base 變動不會觸發重算**，而過期的綠燈與有效的長得一模一樣。⇒ 覆核／合併前一律自己重查 develop 當下版號，不要讀 gate 的歷史結論。（2026-08-14 #729 實例：09:28 算的綠燈在 #733 併入後仍顯示 success，實際已撞號。）
- **解 `src/tests/__init__.py` 這類「一行一個 import」的衝突，驗收要用集合差不能用計數**：#519 第二次解衝突時，衝突雙方**各新增 1 條、行數完全相同** ⇒ 取任一邊計數都顯示「對」，但取任一邊就少一條，**那個測試檔整檔不被 collect**，而「一條都沒跑」與「全部通過」的輸出完全相同（#467／#544／#684 同族）。正解＝驗「對方有、解完卻不見」是否為空集合。**報 import／測試計數時一律連判準定義一起報**（頂層 `from . import` vs 任何 `from .` 會差 1）。
- 本 note 每隔一段時間會
	- 清除已完成的 issue，所以不應該留下過時記錄。
	- 同步 tea cli 可以讀到的 issue


## 🔒 認領中

> 動工前在此登記＋在該 issue 所在列標 🔒；完成後兩處都自行移除。**無登記＝無人處理。**

| Issue | Leader | 起始 | 狀態 |
| --- | --- | --- | --- |
| **ci#86 ＋ ci#43**（同批：同一組 gate scripts） | leader2 | 2026-08-15 | ✅ **完成並驗收——ci#110 已 merge（`8673c99`，#43 自動關）；ci#111 已由 Eugene merge（2026-08-16T01:08，`329e38d`，#86 隨 Closes 自動關單）**。rebase 增量經雙路獨立覆核全 PASS（覆核追記＝PR 留言 **7498**）。⚠ **檢查單③尚未執行**：merge 後首次真實 dispatch→/approve 實跑驗跨 run 封緘（離線正向對照已做、真實一步待跑，可與 #66/#68 六條線驗收合排）。衍生 **ci#112 阻塞已解除**（#111 已合併），open 無人認領。🔑 整合步發現原檢查單①「APPROVAL_SEAL 加進 REUSE_MATCH_FIELDS」**字面指示本身有缺陷**（seal 含 DEADLINE_EPOCH、沿用比對刻意不含 ⇒ 照做會讓合法跨 run 沿用 100% 假紅＝#82 形狀復發），已改為「逐候選用該 issue 自己的 context 驗它自己的 seal」並經覆核者自注入變異證實鑑別力。merge 時檢查單②（無 open approval issue）08-16 08:5x 預檢成立；merge 後記得檢查單③首次真實 /approve 實跑。覆核全紀錄見 PR 留言 **7466**／**7467**（首輪 HIGH＝沿用不比 ADDON_REF/TAG_PUSHER 已修＋次輪「引號包純空白 key」雙層穿透已修，勿在此重述）。🔑 **合併檢查單（在 PR 7467）**：先 #110 → #111 rebase 把 APPROVAL_SEAL 納入 REUSE_MATCH_FIELDS；#111 合併時不得有 open approval issue（舊格式 fail-closed、刻意不向下相容）；合併後首次真實 dispatch→/approve 實跑驗跨 run 封緘。PR **ci#110**（Closes #43）／PR **ci#111**（Closes #86）＝body 封緘 `APPROVAL_SEAL`（跨 run 穩定 key＝`CI_GITEA_TOKEN`；與舊名 CONTEXT_HMAC 互不為子字串；9 條線 18 個 gate step 資源側枚舉，含「新線漏傳即紅」的靜態掃描測試）。兩張皆 ⛔ 勿代 merge。⚠ 合併注意：**#111 合併時不得有 open 的舊格式 approval issue**（無封緘會 fail-closed，刻意不做向下相容）；建議先合 #110。衍生 **ci#112**（close-expired-approvals.sh 同型缺口，cron 取 body 值不驗封緘——刻意不在 #111 擴張，已開單）。另 **ci#54 已於 08-15 結案**（leader2 貼驗收證據關單，留言 7403） |
| **ci#112 ＋ ci#47 ＋ #66/#68 驗收規劃 ＋ ci#48 查證**（Eugene 08-16 一次核准四項） | leader2 | 2026-08-16 | ✅ **#112 已關（08-16T08:47，PR ci#113 由 Eugene merge `01fbbf2`——恰在覆核結論綁定的 head 240d3f6 上合併 ⇒ ci#116 免重驗可直接合）**。覆核紀錄 PR 留言 7559；衍生 **ci#114** 仍 open 無人認領。**#47 已關（08-16T08:50，PR ci#116 由 Eugene merge `7437ef7`；main 首跑 Lint check 綠＝gate 真實生效）**。A1 落地＋follow-up ci#115 open。Gantry 已 dismiss。**本認領列四項全數收束：#112 ✅／#47 ✅／#66+#68 runbook ✅（留言 7516，待 Eugene 執行）／#48 查證 ✅（留言 7522，開關待 Eugene 按）——可於下次整理時整列壓縮**。**#66/#68 驗收 runbook 已交付＝#66 留言 7516**（6 次 dispatch 收兩張，/reject 放 hermes-agent；待裁決該線要不要補 /approve）；**#48 查證已交付＝#48 留言 7522**（不會卡死、唯一禁忌 block_admin_merge_override、CODEOWNERS 檔頭指引過期），開關剩 Eugene 按。 |
| **ci#98** 📕 **已關 2026-08-15T09:52:22Z**（PR **ci#109** 由 Eugene merge `cc1e0d8f`，`Closes` 自動關單） | leader2 | 2026-08-15 | ✅ **完成並驗收**。方向 1＝registry 端排程掃描（`scripts/cleanup-orphan-candidate-tags.sh`，5 檔 +655/-0），掛在既有 `close-expired-approvals.yml` cron 第二 step（`if: always()`、自身恆 exit 0，不佔新 runner slot）。閾值 1440 分＝合法存活上限 240 分（180 核可＋60 promote 餘裕）的 6 倍，附**執行期 self-check**（從 workflow 檔反推 `MAX_TIMEOUT_MIN`，不安全或讀不到就整輪不掃）；fail-safe 全向「不刪」倒（時間戳壞→跳過、open issue 保險絲、DRY_RUN 非法值→當 dry-run、Gitea 讀不到→不刪）。端點實測：`?list&deep=1` 在本站台 **400（Pro only）**，改用 `GET /api/storage/<repo>/<image>`（children 列 tag）＋單 tag FolderInfo 取時間戳；內網位址由 leader2 從 paas-operator pod 無憑證 GET 驗到 Artifactory JSON 401（非 404）封掉最後未驗環節。測試 19 斷言＋fixture 假 curl，變異紅燈 M1-M4（多刪向）＋leader2 注入 M5（空轉向）皆精準紅，**已接進每 PR CI**（check-addon-lists.yml 第三 step，真實 runner 1 秒）。<br>🔑 **issue 的「實例」前提被推翻**：#87/#93/#95 全是 gate-**before**-build 線（mirror-image/build-image），不產候選 tag ⇒ 機制缺口為真但尚無真實踩到的案例（registry 現況三 image 零 `-pending-approval`，雙向對照後的結論）。已留言 issue #98。<br>⚠ AQL 陰坑一則：`items.find` 不帶 `"type":"folder"` 回 200 但 `total:0`（預設只找 file，tag 是 folder）——「空清單先假設探針壞了」的實例。<br>merge 後 cron 首跑驗收：預期三行「0 個候選 tag」（leader2 追蹤中） |
| **ci#107** 📕 **已關 2026-08-15T08:25:47Z**（PR **ci#108** 由 **Eugene 本人** merge `3b22ecf3`，`Closes` 自動關單） | leader2 | 2026-08-15 | ✅ **完成並驗收**（leader2 用與實作者不同的查法覆核）。改 7 處「29 個 component」→ 28（5 檔：兩支 HA workflow 檔頭/註解、CLAUDE.md、docs/sop-ci.md ×2、woowtech-ha/README.md），被保護資產零觸碰（`woowtech-ha-full/README.md` 整檔不在 diff、CI `DIRS=29` 輔助判準原樣）。實作者對上游 pin `a774b540` 樹重數過：v2＝29 目錄/28 manifest（缺的正是 `woow_panel_frontend`）⇒ **28 在 merge 當下仍成立**；v1＝9/9，`9` 無同類問題。PR body 附掃描指令＋26 處逐處判定表＋排除規則＋修後複驗，覆核者可驗**判定**不只驗 diff。<br>🔑 兩條可推廣：① 實作者自己發現範圍比 issue 大（issue 只點名 v2 workflow，v1 workflow `build-woowtech-ha-image.yml:9` 檔頭也有同型殘留）——**「範圍自己枚舉、不給清單」的派工形狀第四次掃描終於掃全**；② 驗收標準寫「不再有與 28 牴觸的敘述」＞「不再有 29」⇒ 實作者補了反向掃描（概念詞側找數字），單掃 29 抓不到「30 個 component」這種錯，同型單子建議都補這步。<br>順帶記錄：`docs/sop-ci.md:245` 那處不只用詞錯——原文把守線行為也描述錯了（v2 真正 fail-closed 的是 `MANIFESTS=28`，`DIRS=29` 只 warn），改後兩者才一致 |
| **#519** 📕 **已關 2026-08-14T23:34Z** | leader1 | 2026-08-14 | ✅ **完成並驗收**：PR1 **#727**／PR2 **#729**／PR3 **#734** 三張全 merge，`Closes #519` 於預設分支 develop 自動關單；develop 到 **140**、`src/migrations/18.0.1.0.140/` 已落地。方向 2（sentinel 建立閘）＋提案 B（subscription_id 歸屬／狀態／唯一性 ＋ partial unique index）皆上線。設計與證據見 issue 留言 **7081**／**7093**，勿在此重述。衍生 **#725**（sh_subscription 的 `/renew-subscription` 鑄幣口，歸屬第三方 addon）**仍 open、無人認領**。<br>⚠ **本列請勿整列刪除**：尾端有 leader4 自己保留的錯因與教訓段（他明示要留）。認領語義已撤，其餘留待看板週期清理。<br>—— 以下為施工過程紀錄，已過時，僅供追溯 ——<br>🚧 **[08-14 08:28 狀態]** PR1 **#727 已由 Eugene merge**（`ef83f51f`，develop manifest 已到 **137**）；**[08-15 更新] #729／#734 的 CI 紅燈與撞號已修畢，兩張皆全綠、待 merge**：版號 develop 138 → #729 **139** → #734 **140**（migration 目錄已同步改名為 `src/migrations/18.0.1.0.140/`，139 已不存在——目錄名綁死版號，只改 manifest ＝ migration 永遠不執行且零訊號）。CI 紅燈真因**不是**「四處漏登記」，而是 `TestHookMode` 把腳本複製進臨時 root 端對端跑、而第 26 條的輸入是整棵 `src/` ⇒ **同一判準在兩個入口拿到不同輸入**；修法是加「有沒有輸入」的前置條件（判準用 `src/__manifest__.py` 存在性，**刻意不用本條自己的錨點檔**，否則刪掉錨點就自我豁免）。leader1 已獨立注入紅燈複驗前置條件**沒有削弱鑑別力**（sentinel 三變體改名＝紅、非 allowlist 檔出現符號＝紅、還原＝綠、hook 測試 PASS）。✅ **[08-15] #729 已 merge（develop 到 139）；#734 已 retarget 到 develop 並解完衝突（head `33a8a6ea`），mergeable、CI 三綠、版號 140／migration 目錄 `18.0.1.0.140` 皆有效 ⇒ 只差 merge，merge 即 `Closes #519`**。<br>🔑 **兩次解衝突都撞同一個檔 `src/tests/__init__.py`，第二次的形狀更惡劣**：衝突雙方**各新增 1 條 import、行數完全相同** ⇒ **用計數對帳取任一邊都會顯示「對」，但取任一邊就少一條、那個測試檔整檔不被 collect**（#467／#544／#684 同族，「一條都沒跑」與「全部通過」輸出相同）。正解＝**集合差**：驗「對方有、解完卻不見」是否為空集合，而不是驗數量。leader1 兩次都用集合差獨立覆驗，皆為空集合。<br>（附帶：leader1 與 teammate 的 import 計數差 1 是**口徑**——寬判準算任何 `from .`、嚴格判準只算頂層 `from . import`，兩者各自一致、結論相同。報計數一律連定義一起報。）<br>原：merge 順序 #729 先 → #734 retarget。**[08-15] #729 因 #733 併入而衝突（`src/__manifest__.py` 版號行＋`src/tests/__init__.py` 相鄰 import），已由 teammate 解畢並 push（`dd54f9b5`）：版號取 139、兩邊 import 全留；leader1 用**集合差**（非計數）獨立覆驗——共同祖先 211 → develop 212 → 合併後 213，「develop 有而合併後不見」＝空集合，雙方新增全涵蓋。mergeable=True、merge_base 已對齊 develop HEAD、CI 兩個 check 在 push 後轉綠。⚠ 覆核限制：對帳用的是 `__init__.py` 的 import 行集合（對「解衝突有沒有漏一邊」充分），**合併後的完整測試套件未重跑**。；#730 需自行 bump（仍 137）。<br>🔑 **本輪最可推廣的一條**：**版號 gate 的綠燈只對它跑的那一刻的 base 有效**——PR 開著期間 develop 每動一次就過期一次，而過期的綠燈與有效的長得一模一樣（#729 就出現過 09:28 算的綠燈在 #733 merge 後仍顯示 success 卻實際撞號）⇒ 覆核／合併前一律自己重查 develop 當下版號，不要讀 gate 的歷史結論。<br>原：PR1／PR2／PR3 **三張全開完**（#727 已 merge／**#729**／**#734**，後兩張勿 merge）。leader1 覆核已完成：形態、版號（develop 137 / PR2 138 / PR3 139 遞增有效）、`merge-tree` 獨立試算無衝突、repo-lint 檔頭 `30/27` 與實際一致、唯一索引在 `init()`（partial unique index，同 model 已有 `provision_partner_id` 先例、冪等涵蓋新裝與升級）、`test_B5` 驗 DB 實體。**leader1 獨立注入紅燈驗過 repo-lint 第 26 條兩種（非 allowlist 檔出現符號／錨點消失）皆有效**。⚠ **未獨立重跑**：Odoo 測試套件（2726 tests）與 migration 的 exit=255 實測，那些採信 impl519 的回報。⚠ **PR3 帶 migration ⇒ merge 前重新確認版號大於合併當下任何存活環境已到達過的最大版號（#691，輸掉完全靜默）**；`#729` 併進 develop 後 **#734 要 retarget**。沙盒 `dev-sandbox-gate519` 保留中（docker-desktop、TTL 48h）可直接覆核。<br>原：PR2 **已開＝#729**（`fix/519-create-sentinel-gate` → develop，7 檔 +556/-30，`Refs #519`、mergeable，merge_base 已對齊 develop HEAD；版號 138 對 develop 137 有效）；PR3 分支 `fix/519-subscription-binding-guard` 實作中、**尚未 push**。<br>📊 **首輪測試已跑**（sandbox `gate519`）：baseline 0F/4E（4 個都是環境因素：3× 缺 `AGENT_RUNTIME_NAMESPACE_SECRET`、1× HTTP timeout）vs PR2 首輪 4F/19E，**修正中重跑**。首輪同時抓到兩件事：① 新測試檔沒進 `src/tests/__init__.py` ⇒ 整檔不被 collect（靠**收集總數 2695→2696 只 +1** 發現，不是靠紅綠——#467／#544／#684 同族）；② **PR1 的枚舉真有一處漏**（`test_global_instance_cap_501.py` 用 `%s.cloud_service` 內插組 model 名，AST 判準看不到），已在 PR2 補上並分開歸帳。**production 側經三路獨立證據確認乾淨**（靜態盲點掃描＋執行期 traceback＋合法流程正向對照），leader1 已用獨立查法覆驗第一路逐行一致。⚠ **PR1 是在測試套件從未跑完的狀態下被 merge 的 ⇒ 驗收尚未成立**，測試結果改貼 PR2 並涵蓋 PR1。⚠ 沙盒 `dev-sandbox-gate519` 是 **hostPath 掛 leader1 worktree（`519-create-guard`）的 src**，別的 leader **不要沿用**——在那裡做實證測到的是「develop ＋ 未合併改動」，且基準錯位零訊號。<br>🔨 **Eugene 已裁決（2026-08-14）：只走方向 2（model 層 create 守衛，一次到位）**，方向 1／3 本輪不做；另裁決「**任何 base.group_user 可自建 workspace 並自動成為 owner ＝ 產品設計**」⇒ ACL／role 那兩層對本單結構性無效，**create 守衛是唯一有效守線**。**A＋B 同批**（Eugene 2026-08-14 追加裁決），拆三張 PR：PR1 純機械遷移（零行為變更）→ PR2 加 sentinel 閘＋測試＋改寫 `cloud_service.py:3302-3319` 那段「不要在 create() 補守衛」的反向註解 → PR3 提案 B（subscription_id 歸屬＋狀態＋唯一性＋migration，⚠ 踩 #691 版號競賽）。**實作進行中**。設計依據＝issue 留言 **7093**（含被否決的路，勿重試）＋ **7081**（現況），勿在此重述。衍生：**#725**（sh_subscription 的 /renew-subscription 鑄幣口，歸屬第三方 addon，本 repo 只當追蹤載體）。🔴 看板 A2 段原句「修法＝把寫入權綁在 role ∈ {owner, admin}」**已由 #676／PR #695 落地**，但對本單主場景**幾乎零貢獻**——`workspace_rule_anyone_create` 的 domain 是恆真、`workspace.py:220-223` 在 create 後 `.sudo()` 自動授 owner ⇒ **owner 資格可自我滿足**，攻擊面＝**任何 base.group_user 帳號**（比本單早期留言假設更寬）。cron 今天仍只看 state；且 `subscription_id` 為空的服務**永久跳過生命週期 cron**（不只一次性繞過）。三方向今天的取捨已重評：方向 2 天真實作**會打壞合法流程本身**（合法流程 create 當下也沒有 subscription_id，`paas.py:3049` create → `:3080` 才掛訂閱）；方向 3 只驗 truthy 不驗歸屬，**前置須先確認 `sh.subscription` ACL**。順帶掃到的 smart_home 零 ir.rule ＝ **#524 已完整涵蓋，不另開單**。<br>🔴 **2026-08-14 leader4 更正自己寫的東西**：本行尾端原本被我追加了一段「仍 open、最新留言 7081、**Eugene 尚未裁決**、三個方向沒有哪一個中選」——**那是錯的，已刪**，以本列開頭 leader1 寫的裁決結果為準。<br>錯因留著不刪，因為它會再犯：我的查證抓到的最新留言是 **7081**（`2026-08-14T06:06:44Z`），而 Eugene 的裁決是留言 **7093**（`2026-08-14T07:06:52Z`）——**查證與寫入之間隔了一小時，我把快照當現況寫了進來**，還在括號裡加了「只補新鮮度」五個字，讓一段已經過期的敘述看起來比周圍更可信。<br>🔑 **「補新鮮度」這個動作本身也需要新鮮度**：落筆前要重驗一次，不能拿查證當下的結果直接寫。而且這種錯**不會以衝突現形**——它安靜地躺在同一列的另一端，跟開頭的正確敘述並存，誰讀到最後就信誰。⇒ 往同一列追加狀態時，**先讀完整列**確認不會和既有敘述打架 |
| **#694** 📕（已關 2026-08-14T07:33Z，Eugene 裁決；驗收證據＝issue 留言 7110；承接 #702/#714 皆 open 無人認領） | leader2 | 2026-08-12 | 🔴 **2026-08-14 重查更正：PR `woow-paas/paas-app-bundle-cd#99` 早就被 merge 了**——`merged_at=2026-08-13T15:11:07Z`、`merged_by=eugene`、merge commit `8020538`。看板原本寫的「可 merge，**待 Eugene**（⛔ 勿代 merge）」**前提已過期**：⛔ 約束得到代理人、**約束不到 repo 擁有者**，而擁有者自己 merge 時不會有任何訊號回到看板上。另有後續小 PR **cd#100**（補 shellcheck disable 註解、行為不變）也已於 `2026-08-14T03:38:30Z` 由 Eugene 自行 merge——⚠ 該 PR body 自己寫著「不要為它單獨排一次 rollout（prod 會中斷約 3 分鐘），應併到下次 promote」。<br>⇒ **關單條件「PR 合併**並在 prod 生效**」只剩後半**：issue 留言串**查無 prod 驗收紀錄**（PR #99 自己列的 `grep marker`／`find -user 999` 檢查沒有貼回結果），最後一則實質留言 **6986** 的時間在 merge **之前**，其後只有自動的 issue_ref／commit_ref／pull_ref 事件。<br>**下一步**＝確認 prod 是否已 rollout、補上驗收證據，才可關單。<br>背景（**不是**關單條件）：租戶側走 **#702**（Eugene 裁決先偵測不預防）、marker 告警走 **#714**，兩張皆 open 且**無人認領**。<br>覆核提的 A（marker 假綠）／B（DB 推導）／C（穩態成本）三點已修並經 leader2 逐項複核；留言索引見 **6928**（6900／6902 有部分已被 6904／6916 推翻，**讀本單由後往前**）。<br>✅ **2026-08-14 prod 驗收完成——關單條件兩半皆已達成**（leader4 派 teammate 執行＋leader4 用四條**獨立於執行者**的觀測覆核，非重跑對方的查法）：① **ReplicaSet 歷史**證明兩次 rollout 都真的發生（`paas-odoo-67557b868b` 建於 `2026-08-13T15:11:26Z`＝cd#99 merge 後 19 秒；`paas-odoo-5cb874c84c` 建於 `2026-08-14T03:39:35Z`＝cd#100 merge 後 65 秒）——這條才分得出「真的重新部署」與「版號相等但沿用舊 pod」；② **git 端 vs prod 實跑的守衛腳本 sha256 逐字相同**（`a38a5fa5…`，8700 字元、無 helm template 佔位符）；③ 取 git 內容用 `contents?ref=` 並以「cd#99 版 `SC2086`×0 vs cd#100 版 ×5」做**陽性對照**證明 ref 真的生效（`raw?ref=` 有靜默忽略 ref 的前科，不驗這條等於沒驗）；④ initContainer `exitCode=0`，marker `FAILED-OPEN=0`／`SESSIONS-DEFERRED=0`／`OK=1`／`SKIPPED-FIRST-BOOT=0`（先驗過四個 marker **互不為子字串**才用 `grep -F` 計數，即 #714 講的那個坑）。檔案系統不變式（owner 必須 999）由執行者驗畢並附陽性對照，報告已保全。<br>⇒ **可以關單了，但關單是 Eugene 的裁決，且本單由 leader2 認領** ⇒ leader4 不代關、已知會 leader2。<br>⚠ 順帶發現的 process 缺口：cd#100 的 PR body 寫「不要為它單獨排一次 rollout（prod 中斷約 3 分鐘）」，但 **Fleet 是自動同步的，merge 後 65 秒就建了新 ReplicaSet**，實際中斷約 3 分 18 秒（pod 建於 `03:39:35Z`、Deployment `Available` lastUpdate `03:42:53Z`）⇒ **那句建議在這個架構下沒有任何執行機制**，要避免只能控制 merge 時機，寫在 PR body 裡擋不住 |
| ~~#723 ＋ #724~~ | leader3 | 2026-08-15 | ✅ **兩張都已關單**（留言 7274／7270）。PR **#736**→develop（含 SKILL.md Step 5 改寫）、**cc/woowprd#2**→master（`ff5f081`）。🔴 **leader 獨立計時對照**（真實有 `Succeeded` TTL pod 的 ns）：舊 selector rc=1／**15s 等滿**、新 selector rc=0／**0s** ⇒ 判準「那段時間不見了」成立。修法＝`LONGLIVED_POD_SELECTOR`（`!batch.kubernetes.io/job-name,!job-name`，不必維護 component 白名單）。**本機已重裝生效**（`gitCommitSha=ff5f081f`）。殘留：#723 的兩處各自解讀「本機版號」（待裁決是否開單）、印 remote sha 那半邊已指回 **#510**。**本列可於下次整理時刪除** |
| ~~#32~~ | leader3 | 2026-08-14 | ✅ **已關單**（08-14 08:39，留言 **7166**）。兩張 PR 均 merged 並經 leader 端到端驗證（含陽性對照、全域 context 未污染）；plugin 已 **uninstall＋install** 重裝生效。🔴 **合併 ≠ 生效**：`plugin update` 與 `marketplace update` 都回成功但 cache 一行沒動（比對的是沒 bump 的 `version`），只有重裝有效——驗 `installed_plugins.json` 的 `gitCommitSha`。殘留／衍生各有載體：**#731**（五支唯讀腳本 `--context` 可選＝靜默打錯）、**#728**（resolve 永遠落 cache）、**#722**（profile 值未改）。**本列可於下次整理時刪除** |
| **#583** 📕（已關 2026-08-14T15:49:34Z——PR **#733** 由 **Eugene 本人** merge，`Closes` 自動關單） | leader4 | 2026-08-14 | ✅ **已完成**。授權閘改以呼叫者身分判定（`env.uid` 反查、`sudo()` 查閘門依賴項、`caller_access.id == rec.id` 同時綁呼叫者與標的）。<br>**可達性實證：缺陷本體成立但完整鏈不可達 ⇒ 嚴重性下修、缺陷照修。** guest 確實通過了守衛（判別式＝同一 payload 只換 `self`，guest／admin／owner 三身分輸出**逐字相同** ⇒ 對呼叫者身分鑑別力 0）；完整奪權鏈被擋在步驟 4，**擋它的是 #556 那批在 `write()` 加的 `_check_owner_row_protected`，不是 #556 修掉的 create 洞**（`git log -S` 驗到引入 commit `866de23a`，其 body 直接點名這條兩步鏈）。<br>🔴 **教訓：RED 不能照 issue 字面寫**——照字面（斷言 `AccessError`）修法前後都綠，因為今天那個 deny 來自下游守衛、與本閘門無關。真 RED＝三條共用同一 payload 只換身分的判別式，owner 那條是**不可刪的陽性對照**。測試走真 HTTP `call_kw`（禁 `TransactionCase`，它預設 `su=True`）。<br>衍生：**#732**（owner 自降 admin ⇒ 永久 0-owner）／**#735**（`transfer_ownership` 死碼要復活還是刪掉——平台**目前沒有任何替代的所有權轉移路徑**），兩張皆 open 無人認領 |
| **#524** 📕（已關 2026-08-15T03:11:16Z——PR **#738** 由 Eugene merge `dde89918`，`Closes` 自動關單） | leader4 | 2026-08-15 | ✅ **完成**。smart_home 租戶隔離 record rule ＋ create sentinel 閘 ＋ tunnel_id identity anchor（裁決 B）。對抗覆核 round1 抓修 3 條（HIGH `_strip` pop→`vals[field]=False` 擋 ir.default 注入／MEDIUM gate 空 group_id／LOW `_cleanup` 清三欄位），全部 RED→GREEN。實作者報告 5 份已保全（sha 對過）。impl524 已 dismiss。<br>衍生 3 單：**#739**（租戶 provision 從沒通過，🔴）/**#740**（delete 孤兒，🔴）/**#741**（doc 教退役 rule）皆 open 無人認領 |
| **#717 ＋ #718**（同批：hermes-agent 租戶自裝 CLI） | leader3 | 2026-08-16 | ✅ **#717 已關（08-16T09:33:19Z——PR #759 由 Eugene merge，`Closes` 自動關單）**；**#718 仍 open**（風險敘述已更新，見該列）。charts PR **#77**（chart 0.2.0：十個安裝器落點覆寫 env ＋ cli-landing-init）與平台 PR **#759**（seed 釘 0.2.0 ＋ 三份 doc）皆由 Eugene merge。stg `my-stg-hermes-2` 已升 0.2.0 rev2、五項驗收全過（含第三態真實觸發：`/opt/data/home/.local/bin` 是含租戶 tea 的真目錄 ⇒ 不覆寫＋loud log＋不擋開機；租戶檔 sha256 升級前後逐字相同）。<br>🔑 **rollout 盤點結論＝無待升租戶**：全叢集授權 ns 內只有這 1 個 hermes release，**prod 0 個**（k8s ns 與 DB `cloud_service` 兩路互驗一致，非探針壞）；prod seed 仍 0.1.3，要等**下次 `/woow:deploy` 發版＋DB `-u` 重套**才吃到 0.2.0（develop 已是 0.2.0）。盤點與 recipe 見 `claudedocs/hermes-cli-test/impl-717/rollout-inventory.md`（git-ignored，需要可向 leader3 索取）。<br>🔴 **pip 家族刻意不給變數**（不設 `PYTHONUSERBASE`）——這是對 issue 字面「六家族」的 **deviation**：上游把它與 `LD_PRELOAD` 列同一 denylist，且它會讓 `/console` 用**絕對路徑**跑 `/usr/bin/python3` 也把租戶可寫的 site-packages 吃進 `sys.path` ⇒ **繞過「除錯只用絕對路徑」這條已拍板的 L3 圍堵**。論證見 `docs/decisions/hermes-agent-installer-landing-env.md` §3.2。<br>🔑 一條可推廣：**這個機制對 agent 自己不可見**——stg 驗收時 agent 把「裝到 canonical」解釋成 HOME 巧合，真因是 chart 注入的 env ⇒ support 時**不要拿 agent 的因果敘述當證據**。驗收與實測記錄見 issue 留言 **7538**／**7582**，勿在此重述。**本列可於下次整理時刪除** |

---

## 分類原則（Eugene 2026-08-12 定，5 類）

| 類 | 定義 | 現在做嗎 |
| --- | --- | --- |
| **② 實作類** | 直接推進上線 | ✅ token 優先 |
| **③ 需討論類** | 需求/策略未定 | ⏸ 先問 Eugene，別自行開工 |
| **①a docs 文件知識類** | 開發常識、文件沉澱 | ⏸ 先問 Eugene，別自行開工 |
| **①b 固化自動驗證** | test/lint/audit/docs pattern 等可程式化省 token 的驗證 | ⏸ 先問 Eugene 要不要做＋附成效評估 |
| **⊘ 非 paas 管轄** | 內文自陳歸屬 woow-machines-operator | ⊘ 不排進來 |

---

# ② 實作類（83 筆）— 可推進上線

> **本段相對 2026-08-08 版的變動（2026-08-12 複查）**：#556、#642 已關單移除；新插入 4 張（A1 `#676`／A4 `#648`・`#655`／A5 `#668`）。
> `#389`（歸屬已查證）、`#519`（阻塞已解除）、`#583`（攻擊面已限縮）三張的敘述經狀態複查後改寫，其餘一句話逐字沿用。
> **2026-08-12 後續**：`#503` 複驗完成並關單，A0 段改由承接單 **#694**（復發防治）佔位；`ci#30` 判定重複已關（追蹤只看 `ci#66`）。

## A0 · prod 事故（1 筆）

> 原本這裡有 5 筆，其中 3 筆（#512／#441／#442）經查證屬叢集運維，已移到文末 ⊘ 段。

| Issue | 一句話 |
| --- | --- |
| **#694** 📕已關 2026-08-14（Eugene 裁決；驗收＝留言 7110；承接 #702/#714） | **復發防治——已裁決，leader2 實作中**。⚠ 原本這一句寫的「只有人手 exec 會產生 root-owned 檔」**已於 2026-08-12 prod 實查修正**：(1) `runAsUser: 0` 是 **Bitnami chart 28.2.10 的預設**、不是我方設定（caps 明列 CHOWN/SETUID ＝ entrypoint 必須 root 起手再降權）；(2) 另有**兩條無人介入的 root 寫入路徑**——`patch-odoo-conf` 每次啟動 root `sed -i`、`module-version-gate` 以 root 跑 `odoo-bin -u`；(3) 真正的失效條件更窄＝**root 搶先建了 hash 前綴目錄**（既有目錄的 owner 就是 odoo，它以 **owner** 身分刪得掉 root 在其中建的檔——⚠ **不是**靠 group-write，odoo 實際跑 999:**999**、不在 group 0，見留言 6904）⇒ **A/B 覆蓋範圍不完整、C 逆上游設計且波及租戶**。Eugene 裁決改走**新方案 D**：預建 filestore 與 checklist 各 256 個 hash 目錄為 `999:0 2775` ＋ 啟動收斂，把不變式從「不准 root 碰」改成「odoo 永遠刪得掉」。範圍**先只做平台 paas-odoo**；租戶 `svc-*-odoo` 同樣暴露，關單前須有明確處置決定。⚠ **守衛涵蓋範圍已於留言 6912 擴大**——原「filestore 256＋checklist 256」不夠：必補 `filestore/<dbname>/` 與 `checklist/` 根（父層不可寫則守衛連 mkdir 都做不到），另有平行子樹 `sessions/`（4096 個前綴目錄，寫入側 100% 靜默，壞掉＝使用者隨機被登出且零 log）。**證據與實作約束見留言 6900；驗收判準已由留言 6902 取代**——6900 的判準 1 作廢（dev-sandbox 的 Odoo 跑 root、filestore 佈局也不同，在那裡注入紅燈永遠是綠的），且 `filestore gc N checked, M removed` 這行**不可再當驗收訊號**（checklist 側刪除包在 `contextlib.suppress(OSError)` 無聲吞錯，卡住的標記檔會把 checked 數灌水）。⚠ 留言 **6904** 再更正一次：6900 修正三的機制解釋錯了（odoo 跑 999:**999** 以 **owner** 身分刪檔，不是靠 gid 0 的 group-write）⇒ 不變式是「目錄 owner 必須是 999」，且收斂驗證的探針**一律用 `--regid=999`**（`--regid=0` 對「只改 group 沒 chown」的失效守衛會給假綠）。**勿在此重述**。承 #503（已關）。<br>🔴 **2026-08-14 狀態更正**：實作已完成，且 **PR `paas-app-bundle-cd#99` 已於 `2026-08-13T15:11:07Z` 由 Eugene 本人 merge**（commit `8020538`）——看板先前寫的「待 Eugene 覆核／⛔ 勿代 merge」那個「尚未合併」的前提**已過期**。**剩餘關單條件只有「在 prod 生效」**，而 issue 留言串**查無 prod 驗收紀錄**（PR 自己列的 `grep marker`／`find -user 999` 未見貼回結果）。詳見 🔒 認領中段。<br>✅ **2026-08-14 prod 驗收完成——關單條件兩半皆已達成**（leader4 派 teammate 執行＋leader4 用四條**獨立於執行者**的觀測覆核，非重跑對方的查法）：① **ReplicaSet 歷史**證明兩次 rollout 都真的發生（`paas-odoo-67557b868b` 建於 `2026-08-13T15:11:26Z`＝cd#99 merge 後 19 秒；`paas-odoo-5cb874c84c` 建於 `2026-08-14T03:39:35Z`＝cd#100 merge 後 65 秒）——這條才分得出「真的重新部署」與「版號相等但沿用舊 pod」；② **git 端 vs prod 實跑的守衛腳本 sha256 逐字相同**（`a38a5fa5…`，8700 字元、無 helm template 佔位符）；③ 取 git 內容用 `contents?ref=` 並以「cd#99 版 `SC2086`×0 vs cd#100 版 ×5」做**陽性對照**證明 ref 真的生效（`raw?ref=` 有靜默忽略 ref 的前科，不驗這條等於沒驗）；④ initContainer `exitCode=0`，marker `FAILED-OPEN=0`／`SESSIONS-DEFERRED=0`／`OK=1`／`SKIPPED-FIRST-BOOT=0`（先驗過四個 marker **互不為子字串**才用 `grep -F` 計數，即 #714 講的那個坑）。檔案系統不變式（owner 必須 999）由執行者驗畢並附陽性對照，報告已保全。<br>⇒ **可以關單了，但關單是 Eugene 的裁決，且本單由 leader2 認領** ⇒ leader4 不代關、已知會 leader2。<br>⚠ 順帶發現的 process 缺口：cd#100 的 PR body 寫「不要為它單獨排一次 rollout（prod 中斷約 3 分鐘）」，但 **Fleet 是自動同步的，merge 後 65 秒就建了新 ReplicaSet**，實際中斷約 3 分 18 秒（pod 建於 `03:39:35Z`、Deployment `Available` lastUpdate `03:42:53Z`）⇒ **那句建議在這個架構下沒有任何執行機制**，要避免只能控制 merge 時機，寫在 PR body 裡擋不住 |
| **#389** | prod SMTP 寄信全 exception → **歸屬已查證（08-02 prod 實查）：問題在平台本體 Odoo（ns `paas-system` 的 paas-odoo pod），不是客戶站**；root cause 已確定，修復卡在「用哪家 mail provider」未決策 |

### 📕 #503 已關（2026-08-12，Eugene 裁決）——以下為歷史紀錄，復發防治見上表 #694

> 關單依據：判準 2 通過＋判準 1 的實質由檔案系統狀態坐實（結案留言 6860）。⚠ 留言 6857 的「02:00 該執行卻沒觸發」**已於 6858 撤回**，讀留言串以 6858 為準。

**已做（2026-08-08）**：`find /bitnami/odoo/data/filestore/bitnami_odoo ! -uid 999 -exec chown 999:0 {} +`，
pod `paas-odoo-744b4fc67-t4w8l`（ns `paas-system`）。**16 項**（8 檔＋8 目錄），不是 issue 標題說的 8——
標題那個 8 是 `find -type f` 的檔案數。⚠ **只 chown 檔案不動目錄會白做**：`os.unlink` 要的是**父目錄**的 `w`。

**驗證**：`! -uid 999` 回空；全量 194 項與 chown 前相同（無誤刪無新增）；用 `setpriv --reuid=999` **實際以 uid 999 身分**測
8 個父目錄 `-w -x` 全 OK（不是只看 mode）。備份在 `~/Downloads/issue-503-filestore-backup-20260808/`，sha1 逐檔相符、
且兩個真實檔 sha1 **等於檔名**（Odoo `store_fname` 即內容 sha1 ⇒ 天然完整性證明）。紀錄在 #503 留言 **6428**。

**✅ 複驗結果（2026-08-12，leader2 派 teammate 執行；留言 6857 執行＋6858 覆核修正）**
現行 pod 已重查為 `paas-odoo-646485dd8c-t9x84`（START 08-08T09:54:43Z、restartCount=0），非 6428 的舊快照。

- **判準 2 通過**：6428 記的 8 個 checklist 標記檔逐一核對全部 GONE；以 uid 999 身分（`setpriv --reuid=999`）全量掃 `! -uid 999` 回空；陽性對照確認探針有效（身分正確、find 總數非零 52 檔/125 目錄）；複驗全程未新增任何 root-owned 檔。
- **判準 1（log 直證）不可得但實質已坐實**：`kubectl logs` 只回溯 ~8 小時（rotation），撈不到 gc 當時的紀錄。但那 8 個標記檔**只有** `_file_gc` 會刪，若仍撞 `PermissionError` 它們會原封不動留著；目錄 mtime 全落在 `08-08T09:56`（pod 啟動後 2 分鐘的 cron 補跑），時序在 chown 之後。**檔案系統狀態是結果，log 只是它的說明**。
- 🔴 **一個被撤回的誤判**：6857 曾寫「08-12 02:00 該執行卻沒觸發」——那個 02:00 是 leader2 工單從 6428 抄下去的**未查證前提**（6428 的 02:00 只是當時對那次窗口的推測，不是 ir_cron 實際排程）。log 視窗僅 7.9 小時，而 auto-vacuum 每天一次，**看不到它是預期行為**；6857 拿 29 個高頻 cron job 當對照也是拿錯對照組（高頻 job 在任何視窗都會出現）。已於 6858 撤回。
- ⚠ **復發防治未做**：根因是 container `runAsUser: 0` ⇒ `kubectl exec` 進去預設即 root（服務本身 pid 1 是 999，不會產生 root-owned 檔）。**這條件今天仍成立**，清乾淨 ≠ 不會再犯。若以「已清乾淨」關單，**建議把下方三個治本選項另開一單承接**，否則線索隨關單消失。

### 🔑 #503 根因——**推翻了 issue 留言 4063 的推測**

原推測「建 General Assistant／WoowBot partner 時順帶寫的 attachment」**不成立**。拆備份檔頭：兩個孤兒檔都是
**Odoo web assets bundle**（編譯後的 CSS/JS 存成 `ir.attachment`），與 agent partner 無關。

真正的機制：container `runAsUser: 0` ⇒ **`kubectl exec` 進去預設就是 root**；而 odoo 服務 pid 1 是 uid 999
⇒ **服務正常運作不會產生 root-owned 檔**。當時有人 exec 進 pod 跑 `odoo shell`
（`docs/plans/2026-Q2/2026-06-26-prod-agent-runtime-enablement.md` Task 4 明文寫了），該程序重建 assets ⇒ 檔案落成 root。

| 根層目錄 | owner | 結果 |
| --- | --- | --- |
| `70/`、`86/` | 0（root 首次建該 hash 前綴目錄） | 真實檔留著、999 刪不掉 ⇒ `PermissionError` |
| `47/`、`a3/`、`c1/`、`c4/` | 999（目錄早已存在） | root 寫的檔已被 999 成功刪掉 ⇒ 只剩標記檔 ⇒ `FileNotFoundError` |

**復發條件不是「某條流程用 root」，而是「容器本身 runAsUser: 0、exec 預設即 root」** ⇒ 下次有人 exec 進 prod odoo pod
跑會觸發 assets 重建的東西就會重演。治本選項（未執行，待裁決）：
**A** 容器內操作一律 `setpriv --reuid=999 --regid=0 --clear-groups`（已確認該 image 內可用）／
**B** 把 `chown 999:0` 補進「exec 進 prod odoo pod」的 SOP 收尾／
**C** chart 層改 `runAsUser`（影響面最大，不建議在此單處理）

### 🔑 #389 根因已查明（2026-08-02 prod 實查，唯讀）——舊看板的「歸屬未查證」已過期

平台 Odoo **從頭到尾沒有設定過任何 outgoing mail server**：`ir_mail_server` 0 rows（含 inactive）、`odoo.conf` 四行
`smtp_*` 全被註解 ⇒ 退回內建預設 `localhost:25`，而 pod 內 25/465/587 無服務監聽 ⇒ 每次寄信必然
`ConnectionRefusedError [Errno 111]`；7 筆 exception 的 `mail_server_id` **全為 NULL**，證明走的是「無 server → fallback」
而非「某台設好的 server 壞掉」。pod 內 python socket 實測 gmail／office365／sendgrid 的 25/465/587 全部握手成功
⇒ **排除叢集 egress／防火牆**。7 筆積壓全是 OdooBot 內建 Digest Email（寄給 `admin@example.com`），沒有一筆業務信
⇒ 進一步佐證是平台系統本身從未配置 SMTP。紀錄在留言 **4434**（該留言即本單最後更新，此後無動作）。

**修復未執行、卡在決策**：要用哪家 mail provider、帳密由誰提供；之後才是建 `ir.mail_server` ＋設 company email／catchall domain。

## A1 · 資安：租戶可實際利用（19 筆）

> **2026-08-14 對帳**：**#676 已由 PR #695 修復關單**（`5608beb0`），本段移除；新插入 **#706**（跨 workspace 綁定盤點）與 **#715**（同 workspace 內角色繞過，新子群）。
> ⚠ #556 的修法帶一個**會靜默失效的前置條件**：日後若「簡化規則」誤刪 workspace_access 的 manage rule，靠它撐著的 #519／#583 守線會**無聲**失效。
>
> **#698（Critical，2026-08-13 由 PR #705／`31dee82a` 修復關單）的 7 張衍生單已於 2026-08-14 逐張讀內文分類完畢**——
> **#706**／**#715** 留在本段；**#716** → ②A4「壞掉的功能」；**#703**／**#704**／**#710**／**#711** → ③（四張的內文都自陳「待決定（不是結論）」，修法選項未拍板）。
>
> 🔴 **交叉指標——下面兩張雖然分流到 ③，但它們是租戶今天就能實際利用的洞**，排 A1 優先序時不要因為它們不在本段就漏看：
> **#710**（guest 直接 `unlink` project ⇒ project 連同 cloud_service 關聯整個從 workspace 消失，比 #705 擋掉的那條更徹底）、
> **#704**（租戶可自行 write `use_agent_runtime` 開 agent runtime、實際佈署 pod 吃資源）。
> 它們進 ③ 的理由是**修法界線未定**（「誰能刪 project」「擋寫要不要連讀一起擋」），**不是危害低**——別把分類當成嚴重度。

**跨租戶奪權／讀取（最高，會被真的用到）**

| Issue | 一句話 |
| --- | --- |
| **#718** | 上游 hermes `web_server.py` **未接 approval gate**——`/api/memory/providers/{name}/setup` 的 `install_cmd` 走 `shell=True`＋`bash -c` 卻無危險指令守衛（陽性對照：`tui_gateway/server.py` 有 3 處命中，故不是探針失效）。**已登入租戶可達**（authproxy 不擋、app middleware 只擋未登入），且 `check_cmd` 是**被動觸發**（Plugins 頁載入即跑，Install 按鈕守不到）。歸屬上游、非我方改動所致 ⇒ 已決策**回報上游＋內部記錄，不自行 patch**（本 repo 對上游目前只 patch `web_dist` 一處）。架構事實見 `docs/decisions/hermes-agent-command-execution-planes.md`。<br>🔴 **2026-08-16 風險敘述已變（#717 的 chart 0.2.0 落地後）**：注入 `npm_config_prefix` 後，第四面 `install_cmd` 從「無 gate 但**跑了也裝不起來**（`/usr/local` 對 uid 10000 是 EACCES）」變成**會成功**——落點 `/opt/data/.local/bin`（canonical、PVC 持久、且位於 L3 root PATH 前段）。**非新洞**（同 uid／同目錄／同通道），但排修復優先級時請以新敘述為準。圍堵載體＝`docs/guides/deployment/hermes-agent-console-support-runbook.md`（console 不做任何安裝）；根治仍是上游接 approval gate。詳見 issue 留言 **7582** |
| **#559** 📕（已關 2026-08-15T00:35Z，leader4 結案留言 7282） | ~~`cloud_service.helm_values` 缺欄位級授權，租戶用 `call_kw` 直讀明文憑證~~——**其實早在 2026-08-04 就由 PR #569（commit `19c3a1e1`）修好並 merged**（`helm_values` 補 `groups='base.group_system'` ＋ 224 行測試 `test_helm_values_field_acl_559.py`），當時已用真 HTTP 三格量測坐實（留言 4860）。issue 一直 open 只因 PR 用 `Refs` 沒用 `Closes`。**views 層另由 #696 承接** |
| **#536** | SA 的 context key escape hatch 家族可被租戶偽造——#522 只修了身分錨點那一支，**另有 4 個未處理** |
| **#531** | operator `PUT /api/tunnels/{id}/routes` 無 tunnel 歸屬驗證，且為整份 ingress 覆寫 |
| **#528** | 停權中的 SA 刪 access policy → CF 同步被 state gate 靜默略過，**CF policy 永久 orphan（其中一條是 allow everyone）** |
| **#289** | project↔CloudService 綁定授權缺 model-level guard |
| **#282** | `workspace.slug` 補 write guard（identity-anchor 不可變） |
| **#706** 🆕 | **承 #698／PR #705 的既有綁定盤點**——`cloud_service_binding_guard` **只擋新寫入、不回溯**，既有跨 workspace 的 project↔cloud_service 綁定不會自動消失，且經 `_reconcile_agent_runtime_reach` 餵給 agent pod 的 egress allowlist 與 agent ns 的 `reach-ws-*` label ⇒ **守衛上線後那些 reach 授權照樣有效**。⚠ 跨 workspace 綁定**本身是合法設計**，無法機械判定攻擊或合法 ⇒ 本單刻意**只做唯讀盤點**（查 DB 撈跨 workspace 綁定與 `create_uid`、比對是否為所涉 workspace 的成員／admin、比對 operator 側 label 與 NetworkPolicy egress 實況），盤完才決定要不要清、怎麼清（可能牽動 #691 的版號競賽）。⚠ 讀 prod DB 需要憑證 ⇒ 那一段走 `woow-operator` agent |
**同 workspace 內的角色繞過（guest／user 越過 owner/admin-only 界線）**

> 🆕 2026-08-14 新增的子群。既有四個子群沒有一個對得上：這類洞**不是跨租戶**，而是同一個 workspace 裡低權限角色繞過「只有 owner/admin 才能做」的界線。硬塞進「跨租戶奪權／讀取」會讓讀的人高估爆炸半徑、也會讓真正的跨租戶項被稀釋。

| Issue | 一句話 |
| --- | --- |
| **#715** 🆕 | **guest 用「搬 task」換掉 cloud_service 綁定**——`cloud_service_binding_guard` 只比對 `write()` **指名的欄位**，而 `task._cs_binding_fields` 沒列 `project_id`（`project_task.py:20`，⚠ 行號為查證當下，動手前重新定位）⇒ **move task 整道跳過守衛**；但 `primary_cloud_service_id` 是 `project_id` 的 **stored compute**，換 project 就重算成新綁定 ⇒ 繞過 owner/admin-only 的授權。**已實測** guest 可搬自己 workspace 內的 task 讓 primary 靜默換綁。範圍限同 workspace（非跨租戶）。修法已明確：`project_id` 加進 `_cs_binding_triggers`，**但要防 #705 F2 的批次迴歸**（見 #716 第 1 項）|

**憑證外洩**

| Issue | 一句話 |
| --- | --- |
| **cd#24** | prod registry 密碼明文躺在 `paas-operator-registry` 的 last-applied annotation（`priority:high`） |
| **cd#78** | 輪替已外洩的 Gitea helm repo `read:package` token |
| **#567** | 例外原文外洩面：`MissingError` 未收斂＋paas.py 3 處／billing.py 5 處 bare `str(e)`。⚠ 單裡自註「行號會 stale，動手前重新定位」 |

**網路隔離**

| Issue | 一句話 |
| --- | --- |
| **charts#42** | `charts/open-design` NetworkPolicy 跨 ns egress 洞（np-audit FAIL，block PR #41） |
| **charts#66** | skill sidecar 專屬 egress 收斂（externalEgress 開啟時 untrusted bash 可外連） |
| **#514** | L2 租戶 ns baseline egress 收斂——第三方 chart 無 egress NP，實測 n8n 仍直達 prod odoo |
| **#428** | odoo chart 28.2.10 `containerPorts.https` 未定義 → paas-odoo NP 全 TCP port 開放 |
| **#427** | Phase 2：paas-system 補 default-deny baseline NP（**前置：待 prod 有租戶後**） |

**agent runtime 沙箱**

| Issue | 一句話 |
| --- | --- |
| **ar#33** | sidecar 內 bash 與 bridge daemon 同 uid ＋ 同 pod netns（kill 1 DoS、loopback） |
| **ar#34** | sidecar 來源的 `tool_result` 未標記 untrusted-content（prompt injection） |
| **ci#48** | `main` 無 branch protection——**查證完畢（1.27.0 源碼級，#48 留言 7522）：不會卡死單人自核**；建議設定組合＋逐步指引＋否定性驗證已備，**開關剩 Eugene 按**（🔴 唯一禁忌＝勾 block_admin_merge_override）。⚠ 附帶發現：CODEOWNERS 檔頭寫的「Require approval from Code Owners」欄位在 1.27.0 **不存在**＝過期指引，回寫檔頭時一併修正 |

## A2 · 金流與計費（6 筆）

| Issue | 一句話 |
| --- | --- |
| **#519** 📕 **已關 2026-08-14**（PR #727／#729／#734 三張，develop 到 140） | ~~租戶可**繞過下單流程直接建 cloud_service**~~——無訂閱、無扣款，部署 cron 照樣開出去。**🔓 阻塞已解除**（#556 於 08-10 merge，租戶對自身 role 不可寫的前提已達成）；本單修法「把 cloud_service 寫入權綁在 workspace role ∈ {owner, admin}」**尚未實作，現在可以安全動工**。<br>⚠ **2026-08-14：本行括號後半那句修法敘述已被推翻**——Eugene 已裁決 **ACL／role 那兩層對本單結構性無效**（「任何 `base.group_user` 可自建 workspace 並自動成為 owner」被定性為**產品設計**），實際採用的是另一條路線。**修法方向與現況一律以 🔒 認領段那一列為準**，別照本行動工——這句刻意不重述裁決內容，重述就會在兩個地方各長一份、然後其中一份先漂掉（本列今天剛示範過一次）|
| **#521** | CS 型 access 加購（`ac_max_access_seats`）——**載體缺口已補**（2026-08-16 五票 #743～#747 全關：quota 0=unlimited＋policy exclusive arc＋backend 接線＋Access 頁籤＋stg 真 CF 驗收；已入 v18.0.1.0.146 image，prod rollout 未做）。**剩席數 enforcement 一層**，繫於 #381；衍生收尾票 #750／#753／#758／#760／#761 開著 |
| **#518** | `max_per_partner` 並發防護在 Odoo REPEATABLE READ 下**零貢獻**（advisory lock 取鎖後仍讀舊 snapshot） |
| **#515** | HAOS 開通前容量 pre-check——上限數字與實際資源脫鉤時仍會超賣 |
| **#361** | 改方案後要讓使用者看到實際退了多少點 |
| **#45** | subscription：stg/prod `end < start` non-trial sub 的 audit + dry-run backfill |
| **#43** | subscription：`.get(unit, relativedelta())` silent fallback 換成 `UserError` + 抽共用 `cycle_delta` |

## A3 · 發版鏈／CI／部署環境（20 筆）

> 原本 17 筆：#642（#627 的 .117/.118 上環境）已於 08-08 驗收完成關單——.118 已跑在 stg 與 prod，對帳 cron 實際執行過。
> 以下已依「`priority:high` 與擋鏈的在前」重排，一句話未動。

| Issue | 一句話 |
| --- | --- |
| **cd#70** | prod chart bump ≥0.1.18 前須移除 `values-prod.yaml` 的 `pvcCleanup.image` override（`priority:high`，否則 CronJob ImagePullBackOff） |
| **ci#54** 📕 **已關 2026-08-15T12:17:36Z**（leader2 貼驗收證據結案，Eugene 於對話中裁決；根因修法 PR ci#55 早於 08-03 merged） | ~~`Validate addon ref` 必定失敗~~——修後零再犯；三項判準驗收證據見 issue 結案留言 **7403**，勿在此重述。**本列可於下次整理時刪除** |
| **ci#66** | ~~佔 runner 空轉~~——實作已全落地（9 線兩段式），**只剩驗收**：3 線各一次真實 phase 2。**六線驗收 runbook 已產出（#66 留言 7516，連 #68 一起收，Eugene 只需 6 次 dispatch＋/approve 或 /reject）**；dry-run 經查證不能替代（關閉條件 6367 要求真實 phase 2） |
| **cd#77** | `SKIP_MODULES_UPDATE` 三段手動流程脆弱＋bitnami 吞掉 `-u` 輸出 ⇒ **prod migration 執行是黑箱** |
| **cd#88** | agent-runtime chartVersion 追平 0.4.5 ＋ stg 既有 release reap 到 1.4.4 |
| **cd#83** | `paas-odoo/fleet.yaml` 加 `disablePreProcess`，消滅 promote 時的人工同步點 |
| **ci#98** 📕 **已關 2026-08-15**（PR ci#109 merge `cc1e0d8f`） | ~~候選 tag 清理漏第四條路徑~~——registry 端排程掃描已上線；詳見 🔒 認領中段該列，勿在此重述 |
| **ci#86** 📕 **已關 2026-08-16T01:08**（PR **ci#111** 由 Eugene merge `329e38d`，`Closes` 自動關單） | ~~完整性校驗需重做~~——APPROVAL_SEAL 封緘已上線（跨 run 穩定 key；9 線 18 step）；覆核紀錄見 PR 留言 7467/7498。⚠ 首次真實 /approve 驗封緘待跑。**本列可於下次整理時刪除** |
| **ci#112** 📕 **已關 2026-08-16T08:47**（PR **ci#113** 由 Eugene merge `01fbbf2`，`Closes` 自動關單） | ~~cron 取 body 值不驗封緘~~——seal gate 已上線（同一份 lib、fail 向「不刪」倒、AUTHOR-before-seal 有斷言鎖）；覆核紀錄 PR 留言 7559。衍生評估單 **ci#114** 仍 open。**本列可於下次整理時刪除** |
| **ci#68** | ~~gate-after-build 佔 runner~~——實作已全落地（候選 tag 機制），**只剩驗收**：3 線各一次真實 phase 2＋一次 /reject 清理證明。runbook 見 #66 留言 7516（⚠ 一項待裁決：/reject 那線要不要補 /approve，見 #68 留言 7520） |
| **ci#43** 📕 **已關 2026-08-15T23:56**（PR **ci#110** 由 Eugene merge `8673c99`，`Closes` 自動關單） | ~~5xx 不重試→job 陣亡~~——退避重試＋冪等查重（逐欄位 context 相等才沿用）已上線；覆核紀錄見 PR 留言 7466。**本列可於下次整理時刪除** |
| **ci#114** 🆕 | 承 #112／PR ci#113：`cleanup-orphan-candidate-tags.sh` 的**排除清單**也讀 open issue 值未驗封緘——但方向是「排除刪除」且有 1440 分年齡兜底 ⇒ **評估單**（先驗兜底論證再決定比照 #113 收斂或接受現狀），無人認領 |
| **ci#115** 🆕 | 承 #47／PR ci#116：workflow run: block 的 40 行（8 檔）SC2086 補引號＋把 lint 的 `WORKFLOW_RUN_SHELLCHECK_SEVERITY` 收緊到 style。🔴 **順序約束：須在 #66/#68 六線驗收之後或同批**（那 8 支裡 6 支是零真實驗證線，不疊未驗證 delta）；權威清單用指令重產勿抄 issue 裡那份，無人認領 |
| **charts#44** | `publish-charts.yaml` 的 `set -e` 把 Gitea Helm push 與 OCI push 綁死（transient 500 誤擋 OCI） |
| **#510** | `/woow:deploy-stg` 的 `addon_ref` 是誤導性 no-op——宣告了卻從未進 CI payload，實際永遠烤 develop |
| **#382** | `pvc-cleanup` CronJob 仍用 `bitnami/kubectl:latest`，要換 bitnamilegacy |
| **#472** | `Dockerfile.dev` 已失效——COPY 不存在的路徑，且 base 缺 6 個第三方相依 |
| **#563** | k8s dev sandbox 的 local/hostPath 模式在 Docker Desktop 下不可用；且 `create.sh` exit 0 掩蓋失敗 |
| 📕 **#32 已關**（08-14） | sandbox scripts 的 `--context` required pattern ＋ 全域 kubeconfig 污染 —— 兩張 PR（#726、cc/woowprd#1）已 merged 並重裝生效。**衍生三張仍 open：#731 / #728 / #722** |
| **#731** 🆕 | 五支唯讀腳本（`list`/`logs`/`status`/`test`/`extend`）的 `--context` 是**可選** ⇒ #32 把「靜默打對」換成「靜默打錯」：`create --context docker-desktop` 後跑 `test <slug>` 不帶 flag，會在**同名的遠端 namespace** 上跑 `odoo -u --test-enable` ＋ rollout restart。刻意不做成強制（會讓既有使用者每條指令立刻壞掉），待裁決收斂方向 |
| **#722** 🆕 | onboard profile 的 `KUBE_CONTEXT` ＝ `docker-desktop`——不帶 `--context` 呼叫 `create.sh` 會靜默打到本機叢集。**與 #32 是相乘關係、非重複**：#32 修「腳本要強制 `--context`」，本單指出 required 驗證擋不住「有值但值是錯的叢集」；另實查 `kubectl config use-context` 在 **`deploy-stg.sh:161` 與 plugin 共用 lib `common.sh:437`（`require_kube_context()` 內）兩處都在** ⇒ #32 Part 2 兩條路徑皆未修；修 lib 那份可一次覆蓋所有 source 它的 sandbox scripts。⚠ 我一度誤判「plugin 版 create.sh 已無」——那是 grep 入口檔沒跟著 `source` 走（已在 #722 發更正留言） |
| 📕 **#723 已關**（08-15） | deploy-stg Step 1.5 只檢查 ahead 不檢查 behind——本機落後 remote 時零訊號。**與 #510 同根**：deploy-stg 的介面暗示「本機／參數決定部署內容」，實際只有 remote develop 決定。2026-08-14 實例：本機落後 22 commit（`.132` vs `.136`），派工者據 `ahead=0` 在工單寫下「與 upstream 同步」的錯誤前提，靠 teammate 回報才發現 |
| 📕 **#724 已關**（08-15） | `create.sh` 中段印 `Pods did not become ready within 300s` 後才進第二段等待，而 SKILL.md 只寫「沉默 5 分鐘」。**與 #563 後半同族**（`create.sh` 訊號不可信）但方向相反：#563 失敗卻 `exit 0`（假綠），本單成功卻印失敗字樣（假紅）。**根因已查證（留言 7091）＝不是 F-DT-2**：`create.sh:397-400` 的 `kubectl wait -l instance=$SLUG` 把 TTL CronJob 留下的 `Succeeded` pod 也算進「要等 Ready」⇒ **只要 TTL 跑過一次，之後每次 `create.sh` 都必然白等滿 300s**（全新 sandbox 第一次不中 ⇒ 這形狀一直被誤讀成「環境變慢」）。標的在 `cc/woowprd`，與 #32 同 repo 但**不同機制，勿合併** |
| **#714** 🆕 | **#694 的 filestore-hashdir-guard 刻意 fail-open，但四個 marker 只印在 initContainer stdout**（`FAILED-OPEN`／`SESSIONS-DEFERRED`／`OK`／`SKIPPED-FIRST-BOOT`）⇒ **沒人 grep 就與正常無法區分**。要把四狀態接上告警：前二者期望 0，`OK` 期望 1（**缺席即異常**），`SKIPPED-FIRST-BOOT` 只該首次開機出現。硬約束三條：🔴 四個 marker **互不為子字串**才能分開判（`grep -w` 救不了，`-` 不是 word 字元）；🔴 perm／group 是 kubelet fsGroup 每次掛載重寫的**恆綠假訊號**，不得當判準；🔴 log 比 pod 短命 ⇒ 載體要能在 pod 重建後留存。驗收**先注入紅燈證明告警真的會響**。範圍僅平台 paas-odoo，租戶側走 #702。Eugene 2026-08-13 已核准另開、不阻擋 #694 關單 |

> 🆕 上面三張是 **2026-08-14 deploy-stg 執行（git tag `stg-20260814.0328`）的旁證發現**，該次部署本身驗收全綠（image tag 換新、pod startTime 前移、addon post-check `.136` 綠、DB `write_date` 晚於 pod 啟動）。**三張皆無人認領**，故未進 🔒 段。
> ⚠ **對下次對帳的提醒**：抓 open issue 時 `tea api "…/issues?state=open&limit=100"` **只回 50 筆**——limit 被 server 端截。要 `&page=N` 逐頁抓才完整。我開這三張單時正是被這個坑害到，漏查 **#32／#510／#563**，三張都是事後補查才建立關聯（見各單留言）。
> 📊 **08-14 的「差 17 未對帳」已於同日結清**：本看板已改用「分頁抓六 repo ＋ 與看板表格取集合差」逐張對帳，結果是**漏列 12、多列 0**（12 張已分類插入）。差額的成因是 08-12 之後多位 leader 各自增刪看板列而檔頭數字沒跟著動，**不是抓取漏掉**。推導方式見檔頭，下次自己重算。

> ⚠ **ci#66 的關閉條件**（08-07 複驗收窄，別憑「PR 都合了」就關）：`build-open-design-image.yml`／
> `build-open-design-console-image.yml`／`build-woowtech-ha-image.yml` 三條 gate-before-build 線**各要真跑過一次 phase 2**。
> 已 merge 的 PR #76／#82／#92 全部掛在 #66 底下（body 皆寫 `Refs #66`）。
> 舊看板「疑重複」段的 **ci#30 是同一起事故（approval issue #62／PR #628）的原始回報**，實際修法工程 100% 在 #66 進行、
> 且 #66 已涵蓋 #30 列的方向（含「調高 runner capacity」＝方案 B）⇒ **追蹤只看 #66**；ci#30 已於 08-12 裁決關單（見文末疑重複段）。

## A4 · 功能缺口／bug／UX（32 筆）

**壞掉的功能**

| Issue | 一句話 |
| --- | --- |
| **#648** | `priority:high`——CloudService 翻 `state='running'` **早於 Cloudflare DNS 真的可解析**，使用者看到綠燈卻拿 `ERR_NAME_NOT_RESOLVED`：`_is_release_ready`（`cloud_service.py:2089-2186`）全文無 DNS 判定、`create_route` 的 DNS 失敗被 `cloudflare.py:699-704` 就地吞掉、release schema 也未鏡射 #484 的 `dns_failed` 旗標。單裡三個方向（補 `dns_failed`／`_is_release_ready` 加 `route_reachable` 子判定／前端軟警告）**自註待討論、非定案**。⚠ 行號是開單當下的，動手前重新定位 |
| **ar#23** | LLM tool-call 一律傳空 kwargs：**skill 執行時 input 參數整個遺失**（stg 1.3.0 / MiniMax-M2.7） |
| **#155** | `SKILL_BUNDLE_TOKEN` 1h TTL：pod 活超過 1 小時後 skill-bundle fetch **必 401** |
| **#316** | stg 控制面 MCP tool sync 疑被 baseline default-deny 擋（`state=error`／`tool_count=0`） |
| **#207** | operator mcp sidecar route 的 deploy/delete 命名不對稱 → 有 sidecar 的 CloudService 漏 mcp CF route |
| **#388** | `agent_session` spawning→active 未轉：spawn webhook 200 但 state 停在 spawning |
| **#562** | postgresql／redis template 重新上架前必須補 image override（Bitnami 搬遷後預設只剩 latest） |
| **#655** | paas-operator `helm.py` 在 `create_namespace` 與 #278 A1 整組 overwrite **兩處**把 ResourceQuota 的 `persistentvolumeclaims` 寫死 `"10"`，沒比照 cpu/mem/storage 走 Odoo 端 `_compute_ns_quota()` 動態算——已知案例：HAOS 每台佔 3 顆 PVC，**同 workspace 開 4 台以上就撞死上限**。⚠ K8s hard quota **沒有「0＝不限制」語意**（0 是禁止建立），要不限只能省略該 key |
| **#739** 🆕 | **租戶經 `/woow` 的 Smart Home provision 從來沒有成功過**（🔴 既有 base 缺陷，#524 實作時發現、實測復現於 base 碼）。`tunnel_token` 自 model 誕生（PR #115）就帶 `groups='base.group_system'`，而 `_provision_smarthome`（`controllers/smart_home.py:521`）用**非 sudo** 的租戶 recordset 呼叫 `action_provision()`，其 `self.write(tunnel_vals)` 含 tunnel_token ⇒ 租戶必撞 `AccessError`，記錄卡 `state='provisioning'`。既有測試沒抓到＝全在 `TransactionCase`（superuser env，欄位級 groups 不生效）。修法：provision 寫入點走 sudo（憑證本就平台鑄造），配 **HttpCase** 回歸測試。⚠ 行號為開單當下 |
| **#716** 🆕 | **#705 守衛的三個殘留缺口（post-merge 複驗）**：① `cloud_service_binding_guard.py:190`（⚠ 行號為開單當下）`create` 對整批 `vals_list` 取**聯集**決定要比對哪些欄位，卻把結果**套到整批 records** ⇒ 批次建兩筆 task、只有第一筆明寫該欄位時，**第二筆連帶被送授權，合法操作被誤擋**（F2 批次路徑復發）；② 反向候選集讀 `project_ids.ids` **預設濾掉 archived project** ⇒ 封存綁定被解掉卻沒進候選集（僅影響移除方向，非新越權）；③ docstring 稱 `(0,0,vals)` 靠 project mixin 兜底，**實測從未被呼叫，理由是假的**——同一個 guard 的**第六輪同型態假宣稱**。三項皆已附失敗測試與修法方向；②③ 內文自陳「不構成新越權、今天不可利用」|

**資料一致性／並發**

| Issue | 一句話 |
| --- | --- |
| **#549** | reach reconciler 的 cron 路徑同樣 lock-then-recount（#518 機制第四站，且是原型） |
| **#533** | `api_service_skip_trial` 吞掉並發類 DB 例外——`SerializationFailure` 被 `except Exception` 收走，retrying 永不重試 |
| **#525** | paas.py 吞例外→靜默 commit 同構縫（`priority:high`）：3 處確認該修、5 處形狀同構因果不同 |
| **#578** | rollback 是第六條帳本失準路徑且方向相反；修它須先改 operator `rollback_release` 的 response_model |
| **#363** | reset saga 並發：client 逾時後重按會與 in-flight saga 競態，rollback 可能覆蓋較新密碼 |
| **#342** | change plan 後 CS 規格 re-apply（#332 殘段） |
| **#732** 🆕 | **owner 把自己那列改成 `admin` ⇒ workspace 永久 0 個 owner、租戶側不可回復**。三道既有守衛全都涵蓋不到這條路徑：`_check_owner_row_protected` 的 docstring 明寫只擋 **non-owner caller**（呼叫者就是 owner ⇒ 直接通過）；「移除最後一個 owner」掛在 `unlink()`（這是 `write()`）；`_check_owner_count` 只在**新值是 owner** 時檢查（新值是 admin ⇒ 整道不觸發）。⚠ 不是沒人想過——#556 的 landing doc 自己就寫著「`_check_owner_count` 對 `role='admin'` 完全不設限」，而補出來的守衛防的是 **transient** 0-owner（降級後自我升級那條鏈），漏的是**永久** 0-owner。後果兩層：① `ASSIGNABLE_ROLES_BY_ROLE['admin']` 不含 owner ⇒ **只有 `group_system` 救得回來**，每次發生都要人工介入；② 只動了 access 列的 role，`workspace.owner_id` 不會跟著變，而 `paas.py::_delete_workspace` 的授權判準用的正是 `owner_id` ⇒ 兩個真相來源從此不一致。**不需要惡意**：owner 想自己降級（交接到一半、誤操作）就會踩到，且沒有任何錯誤訊息。⚠ 第②點的下游我只查到 `_delete_workspace` 一處，**沒做完整的資源側枚舉** |
| **#702** 🆕 | **承 #694：租戶 `svc-*-odoo` 的 root-owned 資料目錄偵測**（Eugene 已裁決**先偵測不預防**）。租戶與平台同用 Bitnami chart、同樣 `runAsUser: 0`，但**租戶沒有 initContainer** ⇒ 唯一 root 路徑是人工 `kubectl exec`，發生後**無守衛收斂**。本單只建唯讀掃描器，三條判準約束：🔴 只認 **owner**（mode／group 被 fsGroup 每次掛載強制覆寫、**恆綠**，拿來判斷就是假綠）；🔴 逐租戶讀 `/proc/1/status` 取**真實 uid** 再掃（**不可假設 uid=999**）；🔴 範圍含 filestore **與 `sessions/` 的 4096 個前綴目錄**（rename 失敗靜默吞掉，症狀是使用者隨機被登出且零 log）。驗收**先注入 `root:root 2775`（不是 2755）紅燈證明抓得到**，全程唯讀 |
| **#740** 🆕 | **租戶 delete 回失敗、但 Cloudflare tunnel 已先被刪、`tunnel_id` 清空後無法重試**（🔴 既有 base 缺陷）。`unlink()` 的 `_cleanup_tunnel()`（送 `delete_tunnel` + 清 tunnel_id）在 `super().unlink()` 的 ACL 檢查**之前**，而 ACL `unlink=0` ⇒ 端點回失敗、DB 列還在、tunnel 已死、tunnel_id 已清 ⇒ **沒有東西能驅動重試**（重試分支與 reconciler 反查鍵都判 `if self.tunnel_id:`）。⚠ 方向與 `cf-ns-orphan-no-account-gc` 相反（DB 列活、tunnel 死），既有 orphan guard 一條都不會響。修法：ACL 通過後才送外部呼叫，或把「外部已刪未落 DB」做成可對帳狀態（別打壞既有 fail-loud guard）|

**migration 地雷（舊 DB 還原會炸）**

| Issue | 一句話 |
| --- | --- |
| **#543** | `18.0.1.0.39` migration 撞 #352 bulk-grant 守衛——從舊備份還原的 DB 會炸在 `.39` |
| **#575** | `.37` migration 的 fan-out 對 pre-`.39` DB 撞 `_check_at_least_one_include`（同家族第二顆） |

**UX／前端**

| Issue | 一句話 |
| --- | --- |
| **#107** | 在地化：`/woow` OWL app 英文 UI → 繁體中文（zh_TW） |
| **#272** | Security Access「Add Policy」改為分情境淺顯選單 |
| **#36** | change-plan UI for SH + CS（modal、trial→no-trial 確認） |
| **#57** | sidecar retire 後 cloud-scope MCP 缺 UI affordance（不知去哪重建） |
| **#56** | Project list 顯示不可訪問的 demo workspace card（admin 點進去 access denied） |
| **#52** | project-detail UX hardening + error handling |
| **#391** | `service_type` 殘段：backend view 依 type 顯隱＋`mcp_integration` 僅 SH 可非空 constraint |

**環境／命名**

| Issue | 一句話 |
| --- | --- |
| **#163** | F-SA-2 環境前綴命名慣例 `paas-stg-*` ＋ SA route-sync 單一 domain source of truth（`priority:high`） |
| **#162** | F-SA-1 stg Odoo `paas_domain` 應改為 `woowtech.io`（`*.stg.woowtech.io` 無 SSL，跨服務） |
| **#384** | F-NETPOL-1 reach-ws label 反向 GC sweep——agent teardown 後 grant 殘留 |
| **#717** 📕（已關 2026-08-16T09:33:19Z——PR **#759** 由 Eugene merge，`Closes` 自動關單；chart **0.2.0** 已落地、stg 已驗收，明細見「🔒 認領中」段 leader3 那列） | hermes-agent 租戶自裝 CLI 在部分入口叫不到——根因**不是 rc 沒持久化**，而是同一 container 內有**四個指令執行面**（console root／agent tool／`!` bang-escape／memory-provider setup），各自 `$HOME` 不同 ⇒ 同名 `~/.local/bin` 指向不同實體目錄，而三面 PATH 交集全在 image layer（重建即失）。解法＝chart 注入落點覆寫 env（不經 `$HOME`，四面皆達）＋ initContainer 建 canonical 目錄與 symlink。🔴 **必須與 L3 信任邊界圍堵同批發**：initContainer 一旦建出 `/opt/data/.local/bin`，就把一條今日潛伏的 root PATH shadowing 通道轉為現行。實作落點在 `woow-paas-charts`。架構事實見 `docs/decisions/hermes-agent-command-execution-planes.md`（衍生 **#718**） |

## A5 · 技術債／重構（6 筆，有餘力再做）

| Issue | 一句話 |
| --- | --- |
| **#415** | 收斂 HA provision 與 marketplace **兩套平行的 CloudService 建立邏輯**（已漏鏡射兩次） |
| **#509** | PR#505 review follow-ups——G4 EnvVar replace 分支盲區＋H1 gate 獨立守線檔＋H2/H3 文字債 |
| **#668** | #657／PR#666 兩輪獨立覆核衍生三項，**皆已實測確認非現行可利用洞**（刻意不在 #666 修）：① grantor lookup 用 `limit=1` + `_order='role desc'` 吃的是**字串字典序不是權限高低**，根因已被 #666 的 user_id anchor 擋住但查法仍依賴會鬆動的前提（`workspace_access` 有同型查法要一併處理）；② X8 o2m 攻擊測試的**可達性陽性對照與攻擊斷言分離**，日後動壞對照會讓四條攻擊測試靜默退化成零鑑別力的綠燈（建議搬進 helper 讓每條攻擊自帶可達性斷言）；③ `invited_by_uid` 可被 context 偽造（純稽核欄位、不被任何 rule／守衛使用）⇒ **事故追查時不可信** |
| **#84** | skill `is_latest` drift fix + e2e（PR #81, F-SK-1~9） |
| **#50** | 全 repo API URL pattern 統一到 cloud_service nesting |
| **#25** | 統一 SH/CS controller 的 role check pattern（抽 `_require_admin_role`） |

---

# ③ 需討論類（28 筆）— 需求／策略未定，別自行開工

> 舊看板 16 筆全數複查仍 open，**一句話逐字沿用**；本輪（08-09～08-12）新開的 4 筆（**#667／#687／#681／#691**）插入對應子群。
> 兩處因關聯單狀態變動加註 ⚠ 複查說明（#584、sh#9），原句未改。

## 政策與商業規則（先決定「要什麼」）

| Issue | 要 Eugene 決什麼 |
| --- | --- |
| **#323** | 使用者主動取消訂閱**不退點、不拆服務**——與「刪除服務退點」行為不一致，哪個對？ |
| **#589** | unbound project 存取判準在 4 條 ir.rule ＋ 1 個 controller helper 之間 **2:2 分裂**（follower-only vs follower OR create_uid）——要決哪一派對，不是誰偏離 |
| **#667** 🆕 | `project_access` **成員資格不蘊含 `project.project` 記錄可讀**——實測連 project 的 admin 成員都讀不到該 project 本體（`project_rule_workspace_member` 對 unbound project 只認 follower 或 `create_uid`）。要決：**這是不是預期行為？** 若否 → `project.project` 得加一條走 `project_access_ids` 的成員 rule（⚠ Odoo 核心模型、與其他 addon 的 rule 是 OR 合併，**爆炸半徑大，須先盤點既有 rule**）；若是 → 要明文寫下「成員讀 project 本體的正規路徑」（follower？controller sudo 投影？）並標進 `project_access` docstring。⚠ 出自 #657／PR#666 兩輪獨立覆核，**明確定性為設計面問題、不是安全洞**（但它讓覆核者的 o2m 攻擊段整段自我作廢，也會讓人寫出誤導性 fixture） |
| **#152** | dedicated `agent_template` 的 RPC 寫入是否應限縮為 admin-only |
| **#381** | AC/CF end-user 認證策略——end-user 走應用層／service token？Access seat 留管理者？（含 CF 成本懸崖） |
| **#394** | SA IdP 限縮 Google+OTP 的歸屬（#272 未明文） |
| **#735** 🆕 | **`transfer_ownership` 要復活還是刪掉？** 它同時是「沒有任何呼叫者的死碼」（`src/static/` 與 `src/controllers/` 皆 0 命中，陽性對照：同檔 `_invite_member` 在 controllers 命中 2 處）**和**「就算呼叫也走不完」——兩道獨立缺陷：① #556 那批引入的 grantor 降級問題（方法先把呼叫者自己降成 admin，第二次要指派 owner 時 `ASSIGNABLE_ROLES_BY_ROLE['admin']` 不含 owner ⇒ `AccessError`，實測復現）；② #658 的 `owner_id` identity anchor。🔴 **兩道要一起處理**，只修一道會變成「換個地方死」（②不是它今天壞掉的原因）。<br>🔴 **決策的關鍵輸入：沒有替代路徑**——`transfer_ownership` 是唯一設計來做這件事的 method 而它壞了，#658 把 `owner_id` 錨成租戶唯讀連繞路也封了，admin 也指派不出 owner ⇒ **租戶側今天做不到任何一種所有權轉移**，只有 `group_system` 改得動。⚠ 「沒人回報過」也可能只是還沒人需要（我沒查過客服紀錄）。<br>⚠ **與 #732 的修法會打架**：#732 若採「禁止 owner 列變成非 owner」，那正是 `transfer_ownership` 第一步要做的事 ⇒ **決定復活之前要先看 #732 的裁決**。<br>本單另一半的意義：這件事已被記錄至少三次（556 landing 的 Not-in-scope、#658 複驗、#583 實證）卻從無載體承接，每次碰到這塊碼的人都重新發現一遍（#681 的具體案例）|
| **#711** 🆕 | **同一個動作、兩層不一致的界線，要決定對齊哪一邊**：`POST /api/support/cloud-services/<id>/project`（經 `ai_assistant.py::_create_project`）只用 `_check_workspace_access`（`ai_assistant.py:104`，⚠ 行號為開單當下）驗**成員資格**（`search_count>0`，**不驗 role**），隨後 `sudo().create()` 觸發 `env.su` ⇒ **繞過 #705 剛加的 `cloud_service_binding_guard`（那條要 owner/admin）**。所以：走 ORM RPC 要 owner/admin，走 sudo 產品端點任何成員（含 guest）就能綁。**跨租戶已被 workspace 閘擋住，非提權漏洞**。4 個互斥選項待裁決：收緊端點對齊 owner/admin（**產品行為變更，user 角色會失去現有能力**）／放寬 guard 到任何成員（**與 #676 不一致**）／維持現狀只補 docstring／順手清 `_create_project` 內已死的 legacy `params.get('cloud_service_id')` 分支（不影響安全，可獨立做）。判準參考 `docs/decisions/patterns/two-layer-acl-lockstep.md` |

## 架構方向（做法有兩條以上路）

| Issue | 要 Eugene 決什麼 |
| --- | --- |
| **#319** | Longhorn SC 策略拍板（default Retain）+ Released PV 清理 SOP + backup target 建置。⚠ **混合**：內文自帶歸屬判定表，48 顆 Released PV 只有 1 顆屬 `paas-ws-*`、6 顆 `dev-sandbox-*`、2 顆 `paas-system`，**其餘 ~39 顆屬其他部署（絕不動）**；且 default SC 是叢集級設定 |
| **#691** 🆕 | 帶 migration 的 PR 在 manifest 版號競賽裡**結構上必輸**：版號是全 repo 共用的單調遞增純量，正確性條件「X 必須大於**合併當下任何存活環境已到達過的最大版號**」在寫 PR 當下無法確定。輸掉的後果**不對稱且靜默**——已升級環境 migration 不執行（無錯誤、無 log）、fresh install 全綠、CI 全綠，於是「本機測過＋CI 綠＋fresh install 驗過」全成立而**線上沒拿到修復**（#675 的內容是授權修復，等於安全洞在升級路徑上原樣留著）。同一張 PR（#675）**連踩三次**（126→127→129→131），且別的 PR 主動「跳號讓路」**無效**——門檻是環境到達過的號碼，不是號碼被誰用掉。四個方向（①合併瞬間原子重命名＋bump ②migration 改用狀態判斷取代版號視窗 ③push-time gate、但仍須配合併前複驗 ④限制純 manifest bump PR）**明確待討論未拍板**；#675 當下只採「盡快 merge 壓縮競賽窗口」的緩解 |
| **#681** 🆕 | issue tracker 與 `docs/tracking/`（followups／pitfalls／landings）是**兩個互不相通的查重檢索面**——本次代價是花一天重新得出 **F-DT-4（2026-06-02）** 早就寫過、措辭幾乎相同的結論，中間還開了前提錯誤的 #665 並實作 478 行 gate。🔴 **核心難題：查重是「動手前」的行為，沒有明確可掛 hook 的事件，結構上看不到能做成 gate 的路徑**（pattern-guard 攔編輯、repo-lint 攔 PR，都有觸發物；查重沒有）。四個提案（查重工具／為 followups 建索引／反向登記成 issue／包成 skill 降低使用成本）各有利弊，單裡**明確不預先選定實作方向**，留給後續動手者依成本效益取捨 ⇒ 要決的是「接受它只能做到提高可發現性」還是投資做工具。✅ 開單前已做**雙面自我查重**（Gitea `state=all` 全文搜 ＋ `git grep docs/tracking/`，含陽性對照）並讀過 `doc_citation_index.py` 確認範圍不重疊 |
| **#396** | orphan PVC GC 搬進 paas-operator（去除 chart cronjob 的 shell-image 依賴），要不要走這條長期解 |
| **#383** | F-ENV-8 admission 層補強（Kyverno/OPA）當 RBAC backstop——要不要引入 |
| **#276** | [Spike] 評估「一 workspace 一 namespace」合併（現況 CS per-workspace + agent-runtime per-project 並存） |
| **#385** | CF tunnel 累積清理評估——reconcile 報 79 tunnel＋10 access_app＋7 policy 孤兒候選，清哪些 |
| **#728** 🆕 | **`deploy-stg` 的 plugin 解析永遠落到 cache，兩條 override 路徑都永不命中**。`deploy-stg.sh` 的 `resolve_plugin_create_sh()`（⚠ 開單當下 `:177-200`，動手前重新定位）組 `rel` 時漏了 `plugins/woow-dev/` 一段 ⇒ 優先序 1 的 `WOOWPRD_PLUGIN_ROOT` override 與優先序 2 的 `plugins-local` dev copy **都打不到真實路徑**，一路 fallback 到優先序 3 的 marketplace cache glob——而那個 glob 的 `*/${glob_mid}/*` 恰好吸收掉缺的那段 ⇒ **誤打誤撞永遠有輸出、無錯誤訊息、症狀完全無感**。PR #328（2026-07-10 已 merged）修過同一個函式但只動 glob 中段名、沒動 `rel`，bug 因此存活至今。🔴 **驗收判準不能用「檔案存在」**——現行行為也找得到檔案（cache 那份），任何存在性檢查都會綠；要比對 resolver 印出路徑的**前綴**（`plugins-local` vs `plugins/cache`）。⚠ 修法只知道一半：`rel` 補上那段是明確的，但 `WOOWPRD_PLUGIN_ROOT` 該指「plugin 根」還是「marketplace 根」**未拍板**（文件描述像前者、實際組法像後者）。⚠ 連帶事實：plugin PR 合併後 deploy-stg 委派的仍是 cache 舊版，**驗收 #32 的 plugin 半邊不能靠跑 deploy-stg 來驗** |
| **ci#102** 📕 **已關 2026-08-15T07:48:31Z**（PR **ci#106** 由 Eugene merge `dab2c3b5`，`Closes` 自動關單；leader2 認領並驗收）| **文件說 image 依 `paas-addons.list` 篩 addon，Dockerfile 實作是全量 COPY**（`priority:P2`）。`Dockerfile:5` 是無條件 `COPY ./addons/ …`、repo 根無 `.dockerignore`、build context 是根目錄 ⇒ 完全不看那份 list；該 list 的唯一用途是產 `requirements-paas.txt`，**不篩 addon 本體**。對照組 `Dockerfile.cloud` 才真的用 `copy-addons.sh` 依 list 逐顆複製、文件與實作相符。後果：放進 `addons/` 但沒加進 list 的 addon **會被烤進 image、Apps 列表可見可裝，但 python 相依不會被 pip install** ⇒ 使用者點安裝 ImportError 且介面上看不出原因（mcp_server 這次靠加進 list 繞過，見 PR #101）。兩個修法擇一**未拍板**：(a) 改文件貼近實作（成本低、坑還在）／(b) 改 Dockerfile 走 `copy-addons.sh`（消坑但會改變 image 內容）。⚠ 選 (b) 前要先確認沒有部署／腳本依賴「custom-addons 一定是全量」這個現況行為——現在兩邊恰好都是 9 顆所以看似無感。⚠ 行號為開單當下 |
| **#710** 🆕 | **要決「誰能刪 project」的界線**——`project.project` 唯一那條 ir.rule `project_rule_workspace_member`（`ir_rules.xml:34`，⚠ 行號為開單當下）的 `perm_unlink=False` 語意是「**此 rule 不參與 unlink 檢查**」而**不是**「禁止 unlink」；而 `ir.model.access.csv:41` 給 `base.group_user` 的是全開的 `1,1,1,1`（含 unlink），**沒有其他 rule 覆蓋** ⇒ **guest 可直接 `unlink` project**，達成與 #705 的 R2/R3 守衛（掛在 write／create，管不到 unlink）所擋的同一結果，而且更徹底（project 連同其 cloud_service 關聯整個從 workspace 消失，admin 也看不到）。範圍限同 workspace 內（非成員 guest 對受害 workspace 的 project 連 read 都沒有）。兩條修法未拍板：補一條收斂到 owner/admin 的 unlink ir.rule ／ 收掉 ACL 的 unlink 位改走明確路徑；且牽動「既有孤兒 project 還刪不刪得掉」。🔴 **這張是今天就能被利用的洞，只是修法界線未定**，別因為它在 ③ 就當它不急 |
| **#704** 🆕 | **要決 `use_agent_runtime` 該用哪種擋法**——該欄位沒有 `groups=`，攻擊者可經 `/web/dataset/call_kw` 直接 `write` 繞過 `ai_assistant.py::_create_project` 那句「NEVER read from user params」的 controller 層保證（**ORM 層完全沒擋**），與 #519／#556／#658／#676 **同形態：controller 有閘、ORM 沒有**。#698 修完 model 層 `_assert_can_bind_services` 後跨租戶那條腿已斷，剩下的是**租戶自行開啟 agent runtime（會實際佈署 pod、吃資源）**的權益／計費問題。三個選項未拍板：① 加 `groups="base.group_system"` 最直覺但**恐連讀取一起擋壞 /woow 前台**（動手前須先查誰在讀這個欄位）；② 改走 model 層 `write()` guard，只擋寫不擋讀，形狀與 #676／#698 一致；③ 要不要為整類「controller 閘、ORM 未擋」的欄位建一條**通用 gate**，避免修一個冒下一個。🔴 同樣是今天可被利用、只是修法未定 |
| **#703** 🆕 | **要決 operator 該不該自己也驗歸屬**——`extra/paas-operator/src/api/agent_runtimes.py` 的 `POST /{hash}/sync-workspaces` 對傳入的 `project_workspace_ids` **零歸屬驗證**，直接拿去渲染兩條腿：leg-1 `helm upgrade` 改 agent pod NetworkPolicy 的 `egressWorkspaceIds`、leg-2 `reconcile_ns_reach_labels` 在 agent namespace 貼 `woow.tw/reach-ws-<W>` label（供 CloudService ingress 的 namespaceSelector 比對）。Odoo 是唯一 ground truth，而 #698 已證實 Odoo 端可被任一 internal user 騙 ⇒ 問題是**要不要縱深防禦**。三條路徑未拍板：反查 Odoo（多一次往返，**且 Odoo 正是被騙的那端**）／operator 自維護對照表／Odoo 簽章＋operator 驗簽。內文還自問「這是否只是把同一個信任關係換個地方寫」。⚠ 動 `extra/paas-operator/` **合併前必須本機跑真 K8s integration tests**（無 CI runner） |
| **#696** 🆕 | **要決 backend view 的 `groups` 該不該比照 security_access**——`cloud_service_views.xml` 的 `action_cloud_service`（⚠:109）與 `menu_cloud_services`（⚠:124）皆無 `groups`，父選單 `menu_cloud`（`menu.xml:12`）也無；對照組 `security_access_views.xml:116-121` 的 menuitem 已有 `groups="base.group_system"` ⇒ **同 repo 兩種處置不一致**。PR #695 已把 ir.rule 寫入收窄到 owner/admin，所以 guest/user 按 `action_redeploy` 會拿到 `AccessError`，**但按鈕仍可見可按**（是 UX／縱深問題，不是現行洞）。三個選項未拍板：要不要加 `groups` ／ 只動 cloud_service 還是連 `menu_cloud` 一起收 ／ 要不要固化一條 gate 強制新 backend menuitem 顯式宣告 `groups`（或顯式豁免）。⚠ 單裡那份「views/ 下完全無 `groups=` 的檔案」清單是**檔案級粗篩**，作者自陳不等於都缺保護（可能靠 ACL/ir.rule 或父選單擋著）——**逐一查證後才能定案，不可照抄當待辦** |
| **#721** 🆕 | **要決要不要開放租戶自帶 Odoo addon**（承 #713／ci#100 的授權擋門；**唯讀 spike、不含實作**）。事實基線：`base_import_module` 解壓階段用副檔名白名單（僅 `.xml`／`.csv`／`.sql`）**擋死 `.py`**，全模組查無可放寬的 config ⇒ 屬架構限制。但租戶**已經**有 `base.group_system`＋`base.group_erp_manager`，對 `ir.actions.server`(state='code') 有完整 CRUD，而 `_run_action_code_multi` → `safe_eval(mode='exec')` 傳進去的 `env` **是本尊不是受限代理** ⇒ **資料層今天就等同 superuser**（`sudo()` 繞 ACL/ir.rule、任意 SQL），開放自灌對資料層的增量 ≈ 0。**真正的增量在 OS／程序層**：容器以 `uid=0` root 跑、NetworkPolicy egress 是 `{}`（等同不限制），而 addon 的 `.py` 由 registry 直接 import、**完全不經 safe_eval**。⚠ §5 列了 7 項未查證前提，最關鍵的一項（🔴）是 **`safe_eval` 有沒有非 dunder 的方法鏈可逃逸到 `subprocess`／`socket`**——若可行，**整份風險判定會翻掉**。§4 明確標注：把 2026-05-03 會議「Skill 不開放使用者上傳」的既有決議外推到 Odoo addon，**是待決事項、不是已決** |

## 範圍未明 / 卡別人

| Issue | 卡在哪 |
| --- | --- |
| **#584** | bn_odoo 密碼**已裁決不輪替**，但 Operator 收線留下 **4 條未查證線索**（含與 #556 相關的 prod 帳號歷史）——要不要查、誰查<br>⚠ 2026-08-12 複查：**#556 本身已由 PR #656 修復並關閉（2026-08-10T14:38:19Z）**，但這 4 條線索仍未查、本單仍 open |
| **cd#85** | `paas-api.woowtech.io` 是未版控的 **prod 控制面公網入口且走 stg tunnel**——#427 會切斷它，**先確認這個入口還有誰在用** |
| **#412** | open-design 體驗殘餘項（zsh 進 image／claude 更新策略／刪除語意明示／orphan home PVC 救援 SOP） |
| **#267** | Hermes MCP cloud service 對外暴露 Gateway（一 tunnel 多 subdomain） |
| **#453** | Vibe Kanban host 加掛 OpenChamber（承 #424③，等 Elmo 定位） |
| **sh#9** | HA integration 新增 MCP server 子網域 URL 與 proxy token sensor（**blocked-by 主 repo #270**）<br>⚠ 2026-08-12 複查：**主 repo #270 早在 2026-07-09T22:40:17Z 就已關閉** ⇒ 標題寫的 blocked-by 前提已不成立，現在卡的是「要不要做／誰做」而非依賴（若仍有其他阻塞，需另行指出） |

---

# ①b 固化自動驗證（18 筆）— 暫緩，不投 token

> **政策：先問 Eugene 要不要做，並附成效評估**（本段每張都附了：防什麼退化／建置成本／省 token 機制）。
>
> 這些是可程式化、跑一次就永遠替你重查的守線。**價值在防退化，不在推進上線**。
> 之所以整段冷凍：08-01～08-05 那五天的 token 大量流進這裡，而 production 沒有前進。
> **解凍時機：實作類的 A0/A1/A2 清完，或某條真的擋住上線時單張破例。**

**成本刻度**：S＝一個 session 內／M＝一天／L＝跨多天。

## 甲 · 「測試到底有沒有在跑」的完備性（4 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#467** | 2 個測試檔未被 `tests/__init__.py` 匯入 → 15 條從未執行（`priority:high`） | **S**（僅剩守線） |
| **#544** | 另 47 條從未執行——整檔只有 `unittest.TestCase`，被 tag 過濾靜默排除（與 #467 不同機制、修法不同） | **M** |
| **#684** | 同一機制的再發現＋實測：3 檔／9 class／**51 條**從未執行，含跨 repo MCP subdomain golden case 與 delete gate | **M** |
| **#546** | 2237 條 Odoo 測試沒有任何 CI 會執行；Phase 1 設計與實測完成（全套 6 分鐘、G1–G4 完備性 gate 原型已跑過），Phase 2 待裁決觸發策略 A–F | **L** |

- **#467｜防退化**：新增測試檔漏 import ⇒ 整檔不被 collect、報告照樣全綠。**🔴 現況更新（2026-08-12 實查 `origin/develop`）**：那兩個檔**已經補進 `src/tests/__init__.py`**（commit `34fd016b`「復活 15 條從未執行過的測試」，2026-08-03），`test_ai_assistant.py` 的 class 是 `TransactionCase` ⇒ **第 1 步已落地，這張剩下的只有第 2 步「機械守線」**。開工前先重查，別重做一次修法。
- **#467｜省 token**：取代「每次懷疑時手動跑 `git ls-tree` × `__init__.py` 的集合差」。⚠ 我自己這次跑這個查法就踩到——把 `src/tests/integration/` 的檔誤報成未匯入（實際是第 158 行 `from .integration import ...`）；**人工查法本身就會產生假陽性**，這正是該做成 gate 的理由。
- **#544／#684｜防退化**：`plain unittest.TestCase` 沒有 `test_tags` 屬性 ⇒ `TagsSelector.check()` 回 `False` ⇒ 整檔被排除，而**排除訊息記在 DEBUG level**，「一條都沒跑」與「全部通過」在輸出上完全相同。實測（`origin/develop`）三個檔仍是 plain `unittest.TestCase`（`test_naming` 6 個 class／`test_reconcile_delete_gate` 2／`test_reconciler_diff` 1）⇒ **今天仍然沒在跑**。守的東西包括：Odoo↔operator 的 MCP subdomain 公式唯一防漂移機制、env token DNS-safe 正規化、一道自認 fail-closed 的 delete gate。直接起因是 #677／PR#675 改了 `naming.py` 簽章而該檔零守門。
- **#544／#684｜省 token**：取代「跑一次全套（CI 6 分鐘）＋人工做 log 集合差」——而且那個查法只有**想到**才會做。⚠ **兩張重疊**：同一機制、同兩個檔，#684 未 `Refs #544`；條數也對不上（#544 記 47 ＝ 38＋9，#684 重新 AST 盤點記 51 ＝ 39＋9＋3）。**開工前先合單並重量一次**，別把兩個數字都抄進 PR。
- **#546｜防退化**：任何 merge 進 develop 的行為回歸目前都沒有自動偵測；PR 的唯一實質 gate 是 repo-lint 靜態檢查（實查 `.gitea/workflows/` 只有 `build-paas-operator`／`docs-link-check`／`manifest-version-gate`／`repo-lint`／`trigger-odoo-build` 五個，**無一跑 Odoo 測試**）。附帶四道完備性 gate（G1 import 覆蓋／G2 執行清單擷取／G3 baseline 差集／G4 skip 盤點）——G4 非有不可：`skipped` 算進 `testsRun` 卻不出現在 summary，本 repo 有 21 個檔用 `skipTest`，條件全是 CI 全新 DB 最容易命中的形態。
- **#546｜省 token**：取代**每一次**發版前的人工全套實跑與對帳（現行 SOP 就是這個，且每次都要重新確認兩步法、`AGENT_RUNTIME_NAMESPACE_SECRET`、`--limit-memory-hard=0` 等七條「不可省的細節」）。⚠ 成本標 L 不只因為工作量：**落點超出 `.gitea/workflows/`**（要動 `scripts/audit/` ＋新 baseline），issue 自陳「leader 未放行前 Phase 2 不動工」；另有 F（act-runner capacity 1 → 調高）屬叢集運維、不歸本 repo。

## 乙 · 測試品質／守線形狀（3 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#572** | 直呼被測碼寫的「無殘留／有回捲」測試會失去鑑別力——機制、判準與 19 條候選的分類（D 組 2 條確認有風險） | **M** |
| **#496** | HAOS 兩條 quota 守線用**含 floor 值**當斷言依據，漂移時會靜默假綠（同 #495 finding 1 同型） | **S** |
| **#438** | repo-lint A 軌——AST 規則禁止 `assertEqual(len(<seed corpus>), <int ≥ 5>)` | **M** |

- **#572｜防退化**：直呼時「被測碼之外」是測試框架（實測會回捲），真實請求裡那個位置是上層 `except` handler（可能吞掉）或 JSON-RPC dispatcher（會 rollback）⇒ 「東西還在／沒有殘留」這類斷言**有修沒修都綠**。#520 家族大量產出這種 rollback 測試。判準已寫死（結論是否取決於「例外逸出被測碼之後會發生什麼」），掃描 recipe 可重現。
- **#572｜省 token**：取代「每寫一條 rollback 測試就重推一次它會不會恆綠」。⚠ 本張核心產出偏實作（修 D 組兩條 `test_workspace_cascade_cleanup.py`），只有把掃描 recipe 固化成 repo-lint 規則才真的落在本段——見文末裁決表。
- **#496｜防退化**：`max(邊際, floor)` 的 floor 只在「workspace 裡只有這一個服務」時成立。#456 就是這樣溜到 stg 的（hermes-agent 邊際 +3 vs 容器合計 3.4，E2E 全在空 workspace 跑、被 8 vCPU floor 蓋住而全綠，放進共用 workspace 才露餡、主 pod 永久 `FailedCreate` 而 DB／UI 照樣 Running）。HAOS 現值兩條都遠高於 floor ⇒ **現在不是缺陷，是結構性風險**。
- **#496｜省 token**：取代「每次調小 guest RAM／disk 或改 CDI 拓撲時，人工重推一次 floor 遮蔽算術」。**修法形制已在 #495 落地可直接抄**（`_marginal_gi`／`_marginal_cpu` 單一定義點），改的是兩行斷言依據＋docstring 寫明「刻意不套 floor」防被改回。
- **#438｜防退化**：擋的是 bug class 本身（新寫的硬編 corpus 計數斷言），與已落地的 B 軌互補——B 軌 `check_seed_topology` 擋「改 seed 的人」，A 軌擋「寫新測試的人」，B 軌不會發現有人新寫一條 `assertEqual(len(...), 42)`。#433 是**第 3 次**撞同一根因。
- **#438｜省 token**：取代 review 時逐條人工判斷「這個數字是 fixture 自建的還是 seed corpus 的」。⚠ 必須三段 AND（AST 形狀＋來源溯源＋閾值 ≥5），**只看 AST 形狀誤報率約 90%**；原型驗過 8 命中／0 誤報但**不在 repo 內**（agent scratchpad），實作等於重寫。

## 丙 · 跨 repo 的 CI 缺口（2 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **ci#47** 📕 **已關 2026-08-16T08:50**（PR **ci#116** 由 Eugene merge `7437ef7`，`Closes` 自動關單） | ~~零 lint／自檢~~——lint gate 已上線且 **main 首跑雙 check 皆綠**（含新的 `Lint / lint (push)`）；覆核紀錄 PR 留言 7577。衍生 **ci#115** 仍 open（40 行補引號，須在 #66/#68 驗收後或同批）。**本列可於下次整理時刪除** | **S** |
| **cd#76** | operator chart 的 prod/stg 版本漂移無任何檢查——`0.1.17` vs `0.1.19` 的 HAOS RBAC 靠人工實查才抓到 | **S**（跨 repo） |

- **ci#47｜防退化**：`run:` block 打錯字要等到 dispatch →（prod 路徑）人工核可 → 跑到那一步才會炸，而 approval gate `timeout-minutes: 200` 佔著**唯一**的 act-runner。該 repo 的 script 正在變重（`wait-for-approval.sh` 是 fail-closed 安全邊界、`mirror-image.yml` 242 行）——全是 shellcheck 擅長抓的形態。
- **ci#47｜省 token**：取代「真跑一次 build（可能佔 runner 數十分鐘至 180 分鐘）才驗得到語法」。對照範本現成（本 repo 的 `repo-lint.yml`＋`docs-link-check.yml`）。⚠ 該 repo 只有一台 runner，lint job 要刻意輕量；**歸屬在 `woow-paas-ci` agent**。
- **cd#76｜防退化**：operator 的「一次上線」是**兩個 pin**（image tag 在 `values-<env>.yaml`、chart version 在 `fleet.yaml`），而 chart 原始碼在另一個 repo ⇒ 發版盤點列 `woow_paas_platform` commit 區間**永遠看不到 chart 變更**。`0.1.19` 帶的是 HAOS 必需的 kubevirt/cdi RBAC；只 promote image 不升 chart ⇒ **租戶付款後開通必然失敗，且 CD/CI 側零訊號**。
- **cd#76｜省 token**：取代發版時人工 diff 兩個 `fleet.yaml` ＋ 實查 ClusterRole。規則就是比兩個 YAML 的同一個欄位，**同型「版號比對」範本已在本 repo 有現成的 `manifest-version-gate.yml`**。⚠ 歸屬在 `woow-paas-cd` agent；issue 自陳只要 warning 不要 block（drift 是正常過渡態，重點是可見）。

## 丁 · memory 稽核工具（3 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#595** | `memory-integrity.py` 的 silent-truncation 有 **45/124 檔結構性永久盲區**，且與「這些檔還沒有快照」在輸出上不可區分 | **S**（3b）／**M**（3a＋3c） |
| **#683** ⚠ 原一句話已過期（2026-08-12 leader3 實查 `origin/develop`） | 原列三層問題，**①②已部分落地**——**②誤報率未經判定**：PR #685 已 merge，baseline 機制上線（126 條人判定清帳、5 種分類）；**①無觸發點**：PR #690 已 merge，`.claude/hooks/memory-links-guard.py`（PostToolUse）在寫入 memory 目錄時即時對帳，但**刻意只掛 `--links` 不掛 `--truncation`**（後者誤報率高）⇒ **truncation 的觸發點仍缺**。**③高誤報率會讓工具自我瓦解**仍在。⚠ #683 的 Gitea 留言只記了 #690、**沒記 #685** ⇒ 單看 issue 不會知道 baseline 已完成 | **M**（剩餘部分） |
| **#700** 🆕（2026-08-12 leader3 開，#693 落地後暴露） | 同一支工具的 markdown link 抽取（`memory-integrity.py:645`）**不剝 code 區塊**，而 wiki-link 抽取（`:598`）有走 `code_stripped()` ⇒ 被 `MEMORY.md` 直連的一般 memory 檔成為索引來源後，正文 code 範例裡的 `](x.md)` 會被判成**斷鏈**；斷鏈進 exit code ⇒ hook block ⇒ **與 #693 同形態的卡死**。目前暴露面 0，但那是「唯一實例剛被改掉」的 0（探針已做陽性對照） | **S** |

- **#595｜防退化**：盲區來自 `all_snapshots()` 拿 frontmatter `name:` 比檔名，對不上就 `continue`；而 45 個檔的 `name:` 系統性與檔名不一致（缺 `reference_` 前綴／kebab vs snake）。**3b 比 3a 更重要**：輸出把「永久盲區」與「暫時無快照」分開**列名**，而不是只印差額——`memory-integrity-audit-2026-08-05.md` 初版就是被那個差額騙到寫錯的。
- **#595｜省 token**：取代「每次看到那行差額都要重新追一次它是什麼意思」。3b 只改輸出、不改判準 ⇒ 是本段成本最低的一張。
- **#683｜防退化**：防的是**工具自我瓦解**——跑一次看到一串「疑似整段消失」、查完全是重構 ⇒ 下次不會有人認真看下一筆。**高誤報率的檢查比沒有檢查更糟**，因為真的資料遺失發生時輸出長得一模一樣。抽樣的 5 種機制（章節改寫、跨檔搬移、格式演進、狀態更新、自我更正）也同時是「3c 機械化門檻不低」的實測佐證。
- **#683｜省 token**：取代「每次跑完逐筆人工判定真偽」（本次判 5 筆就花掉一整輪）。⚠ 與 #595 高度重疊（#683 明寫 `Refs #595`，是它 3c 提案的實測佐證）——**兩張應合併考慮，先做 #595 3b 讓基線可讀，再決定 #683 要不要清帳／降級**。#683 另誠實標注：抽樣僅 45%，且 #595 的盲區讓「多數是誤報」**不能**反推「memory 沒有真遺失」。
- **#693｜防退化**：擋的是**壓縮建議與孤兒檢查互相衝突**——系統提示要求「move detail into topic files」，照做若搬到連結就製造孤兒，而 hook 會把孤兒升級成「擋住所有 memory 寫入」。memory 檔數只會成長 ⇒ `MEMORY.md` 遲早再次逼近上限，這兩條規則會反覆相撞。
- **#693｜省 token**：取代「每次壓縮 `MEMORY.md` 都要先人工推一次哪些內容搬得動」。⚠ 與 #595／#683 同一支工具但**不同 check、不同 function**（`audit_index_links` vs `--truncation`）⇒ 可各自獨立收斂，不必合單；成本低是因為改的是資料來源（遞迴讀 topic 檔）而非判定邏輯，工作量主要在防循環引用與深度上限。

## 戊 · 一次性實測，不產生持久 gate（4 張，⚠ 全部待裁決）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#390** | CS shared cloudflared hostname 能否被 CF Access gate——stg 挑一顆實測（`priority:low`） | **S** |
| **#387** | SA 層 MCP 協定整合 e2e（tool 呼叫／token 流通），既有 E2E 只蓋網路層（`priority:low`） | **M** |
| **#386** | #315 衍生防線三件：operator SA `can-i` preflight／chart-side RBAC test／`KubectlException.stderr` 進 log（`priority:low`） | **M** |
| **#393** | 三 MCP（＋#375 新增三型）規格實跑效能觀察——#268 調規格時只驗 healthz+GUI 200（`priority:low`） | **S**（但要等真實負載） |

- **共同的成效評估**：這四張**不是固化 gate，跑完就沒了**，省 token 機制≈無 ⇒ 放在本段其實名不副實（見裁決表）。
- **唯一例外是 #386 的第 2 件**（chart-side RBAC test 以 operator SA 身份驗各 verb）——那件**是**可重複驗證：#315 的事故成因正是「admin-context integration test 用 admin 憑證跑，遮蔽了 operator SA 的真實權限缺口」，這條做成 test 才防得住 RBAC drift。另兩件（preflight、stderr 進 log）是實作。**建議拆單**。

## 己 · 組裝對帳補列（2 張，⚠ 主筆稿分群表漏列）

> **組裝者註**：①b 主筆稿自身的對帳合計（22 張＝①a 5＋①b 17）與裁決表（#46「目前放在 ①b」）都要求含 #44／#46，
> 但分群表未列。此處按舊看板一句話補列（#46 的細節取自本稿裁決表），**成效評估未做、成本未估**，開工前請主筆補齊。

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#44** | sh_never_expire fixture variant（舊看板一句話照抄；成效評估待補） | 未評估 |
| **#46** | test nits——純 cosmetic（assertion ordering／transitive `expected_end`／`-32d` docstring 軟化），**不產生任何新驗證能力**、省 token 機制≈無（裁決表建議改列 ② 實作瑣事） | 未評估 |

## 建議優先順序（若 Eugene 只肯投 2–3 張）

判準：**existing_infra 已有半套的優先**——形制／範本／先例已在，建置成本主要落在「接上去」而不是「發明」。

**第一順位：#684（併 #544）** — 三個支點都現成：①`.claude/hooks/pattern-guard-map.json` 已有 `test-discipline` class 且已掛 `^src/tests/.*\.py$` 分派，新 gate 有現成落點；②`scripts/audit/repo-lint.py` 已有 28 條 check 的既定形制與 `baselines/` 慣例（`grep -c '^def check_'` 自行重量），加一條 AST 規則是照抄形狀；③**同型修法在 #467 已經走過一次**（`34fd016b` 復活 15 條）。而它防的退化最貴：51 條**今天仍然沒在跑**的測試裡有跨 repo MCP subdomain 的唯一 golden case 與一道 delete gate，且 `naming.py` 剛在 #677／PR#675 被改過簽章。⚠ 開工三個前提：先合併 #544 重量條數（47 vs 51 對不上）；改基底**會冒出真的紅**，那些紅是產出不是意外、不要順手在同一張 PR 修；**gate 的第一個測試必須是注入紅燈**，不是「跑一遍看乾不乾淨」。

**第二順位：cd#76** — 全段成本最低而後果最重：規則只是比兩個 YAML 的 `helm.version`，**同型範本已在本 repo 的 `manifest-version-gate.yml`**；不做的後果是「租戶付款後開通必然失敗且 CD/CI 側零訊號」。⚠ 執行歸屬 `woow-paas-cd` agent，本 repo 只出規格。

**第三順位：#496**（若要留在本 repo 內）— `_marginal_gi`／`_marginal_cpu` 的單一定義點寫法已在 #495 落地，**直接複製**，改兩處斷言依據＋docstring 註明「刻意不套 floor」，改完現值仍全綠。
**或 #595 的 3b**（若更在意工具可信度）— 只改輸出把兩種未涵蓋分開列名，不動判準、不需重新設計，一個 session 內可完。

**刻意不入前三的**：**#546** 價值最高（2237 條測試零 CI 覆蓋）但成本 L、落點超出 workflow 目錄且 issue 自陳「未放行不動工」，它該是**解凍後的第一個專案**而不是「順手做的那張」；**#438** 原型驗過 8 命中／0 誤報但原型不在 repo 內、實作等於重寫，且三段 AND 缺一就 90% 誤報。

---

# ①a docs 文件知識類（8 筆）— 暫緩，不投 token

> **政策：先問 Eugene，別自行開工**（Eugene 2026-08-12 裁定；與 ③ 同級的把關，不同的是這裡不缺需求、缺的是「值不值得現在寫」）。
>
> 這些是開發常識與文件沉澱。**價值在讓下一個人少踩一次，不在推進上線**。
> 之所以整段冷凍：08-01～08-05 那五天的 token 大量流進這裡，而 production 沒有前進。
> **解凍時機：實作類的 A0/A1/A2 清完，或某條真的擋住上線時單張破例。**

| Issue | 一句話 | 為什麼會過期／為什麼還沒寫 |
| --- | --- | --- |
| **ci#107** 📕 **已關 2026-08-15**（PR ci#108 merge `3b22ecf3`） | HA 線 component 數敘述不一致（29→28）已修畢：7 處改、19 處判定保留、「29 個目錄 ≠ 29 個 component」保護節與 CI `DIRS=29` 輔助判準原封不動 | 詳見 🔒 認領中段該列（含驗收證據與兩條可推廣教訓），勿在此重述 |
| **#582** | 四張安全修法聯集全套跑出的四條測試方法陷阱（`-i --test-enable` 測到的是**安裝過程中**的狀態、DB 直查早於對照實驗定位問題、紅燈時「收集總數」是判斷問題層級的工具、排他性宣稱要配哨兵）——待 promote 進 `docs/tracking/pitfalls/` | 開單當下發版 tag 未打在 `73a022ac`，作者刻意不動 develop。tag 早已過去 ⇒ **阻擋理由已消失，這張現在純粹是沒人去搬** |
| **#508** | PR#507 合併後的註解漂移（3 處 docstring/TODO 與實際行為互相打臉：載體信 `auto_delete` 語意、threshold vs 7 天視窗、已完成的 TODO）＋2 條維運觀察（OBS-1 實際上限是 11 封不是 7／OBS-2 `mail.mail` 永久留存無清理 cron） | 註解與同檔另一條測試**直接矛盾**（`:261` 說「載體被刪＝送出成功」vs `:294` 斷言載體不會被 auto_delete 清掉）——這正是「文件裡的可照抄敘述勝過正文警告」會反噬的形狀 |
| **#392** | 手動禮品卡換點 SOP 文件化——07-10 定價會 Q6 拍板「試用＝手動禮品卡換點、不改系統」，但發卡審核／換點操作／記帳慣例／邊界（金額上限、有效期）只存在於拍板記錄 | 已有同性質範本可抄（`docs/guides/smart-home-half-year-gift-points-admin-sop.md`）。⚠ 這張是**營運會用到的**，凍結期間真的有人要發卡時會立刻變成阻擋項 |
| **charts#43** | `charts/open-design/Chart.yaml` 的 `description` 仍寫 C 案前 auth 語意（把 `OD_API_TOKEN` 說成租戶 auth token，實際租戶憑證是 auth-proxy 的 HTTP basic-auth） | **僅 metadata 字串 stale**，`templates/`＋`values.yaml` 契約已正確且經 np-audit #42 複驗 PASS ⇒ 不影響部署行為。issue 自陳「下次 chart bump 順手改、不急、不擋主線」 |
| **#421** | 2026-07-23 進度同步會議（KubeVirt×HAOS／Headscale VPN）待辦分類與佐證分析 | ⚠ **混合單**：內含已答覆的查證結論、可執行項、需查資料項、需再討論項。kubelet max-pods 調升屬**叢集級**，平台側只出建議值、執行交叉 `woow-machines-operator` |
| **#699** 🆕 | 兩處 docstring 宣稱與實際機制不符、**修法方向已定、不改行為**：① `cloud_service.py:1821`（⚠ 行號為開單當下）宣稱「`extra_vals` 其他欄位不提權、ir.rule 照常生效」，但 `_model_init_n8n`（同檔 `:2856`，經 `_cron_check_deploying_services` 呼叫）以 cron／superuser 身分執行（`ir_cron.user_id`=1），**superuser 下 ir.rule 本就不生效** ⇒ 該句在這條路徑上是假的（非租戶可利用面，純文件失準）；② `test_workspace_authz_658.py:601-605` 的 X13 docstring 宣稱「guest 直接寫 cloud_service 的洞尚未補」，但 #676／PR #695（`5608beb0`）已補上，現由 `test_cloud_service_authz_676.py` 守著 ⇒ 已過時 | 承 #676／PR #695 的 post-merge 獨立覆核。⚠ 同類「docstring 機制宣稱過期」**已連踩四輪**；修法要求寫成「**指得出證據的範圍限定敘述**」，指不出來就刪——**不要改寫成新的猜測**（#675 就是修正動作本身鑄出新假宣稱的實錄） |
| **#701** 🆕（2026-08-12 leader3 開） | `tea` 寫入坑知識**雙載體且 upstream 在錯邊**：memory 那份（58,236 B、08-12）持續在長，`docs/guides/development/tea-cli-write-gotchas.md`（38,631 B）凍在 08-05 遷移當天 ⇒ **docs 版是嚴格子集**（memory 獨有 343 行、docs 獨有 3 行），照 `docs/` 走的人拿到落後一週的內容而無任何示警 | 對照組：`docs/` 有 103 篇標注自 memory 遷移，仍留全文副本的**只有這 1 篇** ⇒ 遷移紀律整體沒問題，這是唯一例外。⚠ 提醒「耐久 ≠ 新鮮」 |
| **#741** 🆕 | `docs/decisions/patterns/paas-workspace-member-acl-tightening.md:35-45` 逐字貼著 **#676 已退役**的 `cloud_service_rule_workspace_member`（無 role、四 perm 全 True）當「如何收緊」範例 | doc 最後改動 `e8b977e3`(08-06) 早於 #676(08-12) 六天。**不是 #547 重複**（#547 碰的是同 doc :59 dual-leg domain、且 08-05 關單在退役之前）。🔴 危害＝該 doc 被 **pattern-guard** 列為 `acl-security` 必讀 ⇒ 每個改 ACL 的人被導到一份可照抄、已知有洞的範本 |

⚠ **#508 與 #421 的歸類兩可**，見下方裁決表。

## ⚠ 歸類待 Eugene 裁決（14 張）

跨 ①a／①b／② 三段，本表是「拆分主筆判斷不了、需要你一句話定案」的清單。

| Issue | 目前放在 | 另一個合理歸屬 | 兩可的理由 |
| --- | --- | --- | --- |
| **#572** | ①b | ② 實作（或拆兩半） | 核心產出是**分析＋修 D 組兩條測試**（實作）；只有把 AST 掃描 recipe 固化成 repo-lint 規則那半才是 ①b。可拆：修 D 組 →②，規則 →①b |
| **#496** | ①b | ② 實作 | 本質是修正既有測試的斷言依據（兩行），不是新建機制；沿用舊看板「測試品質守線」脈絡才放這裡 |
| **#46** | ①b | ② 實作瑣事 | 純 cosmetic（assertion ordering／transitive `expected_end`／`-32d` docstring 軟化），**不產生任何新驗證能力**，省 token 機制≈無 |
| **#390** | ①b | ② 實作 | 一次性 stg 實測（掛一顆 CF Access app+policy 驗閘門／既有 route／teardown 對稱），跑完不留 gate |
| **#387** | ①b | ② 實作 | 一次性 stg 協定層 e2e；且 #316（控制面 tool sync 卡 NP）可能是前置 ⇒ 現在排也可能卡住 |
| **#386** | ①b | ② 實作＋①b（**建議拆單**） | 三件混合：preflight 與 stderr 進 log 是實作，chart-side RBAC test 才是可重複驗證 |
| **#393** | ①b | ② 實作／觀察排程 | 一次性效能觀察，且**要等真實負載期**才有意義 ⇒ 現在排等於空轉 |
| **#508** | ①a | ② 實作（部分） | 本體是把 docstring／TODO 對齊實際行為（文件性質），但含 OBS-2「要不要新增 `mail.mail` 清理 cron」的評估——定案動手就是 ② |
| **#421** | ①a | ⊘ 非 paas 管轄（部分） | 混合單：核心產出是會議分析文件，但 kubelet max-pods 調升**屬叢集級**，平台側只出建議值、執行交叉 `woow-machines-operator` |
| **#710** | ③ 架構 | ② A1 | 🔴 **今天就能被利用**（guest 直接 `unlink` project，整個 project 連同 cloud_service 關聯從 workspace 消失）；進 ③ 的唯一理由是「誰能刪 project」的界線未定。要決的是：**「可被利用」該不該壓過「修法未定」**——若該，這張和下一張要搬進 A1 |
| **#704** | ③ 架構 | ② A1 | 同上：租戶可自行 write `use_agent_runtime` 開 agent runtime、實際佈署 pod 吃資源，但三個修法選項（`groups=` ／ `write()` guard ／ 建通用 gate）未拍板。⚠ 第一個選項**可能連讀取一起擋壞 /woow 前台** |
| **#706** | ② A1 | ③ 範圍未明 | 本單**只做唯讀盤點**、範圍明確 ⇒ 歸 ②；但「盤完要不要清、怎麼清、要不要 migration（牽動 #691 版號競賽）」全未拍板。若認為連盤點的執行方式本身也算未定，該改列 ③ |
| **#702** | ② A4 | ② A5 | 建的是**預防性偵測工具**，不是修任何現有壞掉的東西 ⇒ 嚴格說不屬「功能缺口／bug」；歸 A4 是因為它要抓的症狀（session 靜默失敗、filestore owner 錯）落在資料一致性。分類主筆自陳這是**推論不是內文自陳** |
| **#714** | ② A3 | ①b 固化自動驗證 | 產出是 **production 部署環境的告警機制**（故歸 A3），但「跑一次就永遠替你重查、價值在防退化」的性質與 ①b 完全一致。差別只在載體是 K8s 監控而非 code-level gate ⇒ **要不要把 ①b 的定義擴到監控**，是分類原則本身的問題 |

> 🆕 上面 5 張是 **2026-08-14 對帳新增**的兩可項。前兩張（#710／#704）問的是同一個問題，建議**一次裁決**：分類該按「危害是否可利用」還是按「修法是否已定」。目前看板兩種都有先例——#583（守衛檢查錯對象、修法未定）留在 A1，#667（設計面問題）進 ③——所以這不是誰偏離，是判準本身沒寫死。

## 兩段的對帳（無遺漏、無重複）

舊看板 ① 共 22 張 → 其中 **#542／#551 已不在 2026-08-12 的 126 筆 open 清單內**（已關，不在本次拆分輸入）→ 剩 20 張 ＋ 新單 **#683／#684／#693** ＝ **23 張 ＝ ①a 5 ＋ ①b 18**。

---

# ⊘ 非 paas 管轄（7 筆）— 內文自陳執行歸屬不在本團隊

> **本段收兩種**：(a) 內文第一段就寫了「執行歸屬：woow-machines-operator」的（下表前 6 張，屬叢集運維）；(b) **執行歸屬在別的 addon repo** 的（**#725** 屬 `sh_subscription`）。
> 共同點是**平台側改不動它**，只能繞過它防守——所以留在本 repo 當協調載體，不是本團隊的待辦。
> 依 CLAUDE.md 共用叢集鐵律，平台團隊對這些 namespace **只做唯讀盤點、不動手**。
> **它們留在本 repo 是當協調載體，不是本團隊的待辦。排工作時直接跳過。**
>
> 2026-08-12 複查：6 張**全部仍 open**，查證欄結論未被推翻，逐字沿用。
> 同時對本輪全 126 筆 open issue 重跑一次內文歸屬機械掃描（`woow-machines-operator` / `執行歸屬`）——
> **本輪新單（#648／#655／#667／#668／#676／#681／#683／#684／#687／#691）內文均無歸屬警語，無新增 ⊘ 項**。
> 掃描另外命中的 #421（①a 段）與 #584（③ 段）維持原判定：**混合單**，平台側有一塊，不整張移出。

| Issue | 內容 | 查證 |
| --- | --- | --- |
| **#512** | 5 個 prod Odoo 客戶站無備份 CronJob | ns 為 `bnidistrict-odoo`／`linyanshan-odoo`／`lisachuzhu-odoo`／`lyucijyun-odoo`／`puhsu-odoo`，命名 `{tenant}-odoo` 全不在白名單；無 helm release secret、無 `paas-managed=true` 等 operator label、Rancher projectId 空、`fleet-local` 內無對應 Bundle |
| **#441** | kasim-odoo-data volume fsck | 同判準核實；下面那張 440 的留言明講這些 tenant 是「elmo 以 agent 直接建立，非 PaaS 平台開通」 |
| **#442** | 備份健康監控改驗 dump 非空 | 監控對象＝18 家客戶站，**不含 `paas-system`**（見 A0 段那個無人認領的缺口） |
| **#440** | 客戶 DB 備份鏈失效事件（14/18 tenant） | 平台側僅為協調載體。**不建議關單**：本體已復原（抽驗 apollo／woowtech／wowcasa／kasim 的 pg-backup 都健康），但衍生 #441–#444 **四張全還 open** |
| **#443** | 租戶 DB 備份異地副本規劃 | 規劃類。⚠ 內文範圍橫跨「租戶 dump ＋ **平台主 DB**」⇒ 真要做時平台側有一塊 |
| **#444** | R-3 NetworkPolicy 新 pod 編程空窗評估 | label `ops` |
| **#725** | `sh_subscription` 的 `/renew-subscription`（`auth=user`）讓**任何登入者**用呼叫端給的 `subscription_id` 走 `sudo().browse()`，ownership 檢查只擋別人的訂閱，然後 `sudo().create()` **用自己那筆當範本無限鑄造新訂閱**（並順手把舊訂閱改成 renewed）。⚠ 今天沒釀成計費災難是因為鑄出的訂閱卡在 `state='draft'`、而平台判準一律只認 `in_progress`——**這層擋住是巧合不是設計**。⚠ 行號為開單當下；`/cancel-subscription` 同族嫌疑**未逐行查**。⚠ `q=` 搜不到 body，查重範圍不是全集 | **執行歸屬 `sh_subscription`**（Eugene 2026-08-14 裁決開在本 repo，以免線索斷在跨 repo 邊界）。平台側只能繞過它防守、改不了它——#519 的守衛判準形狀就是照這個缺口設計的（含 `state='in_progress'` 檢查）|

⚠ **上表 512 與 440 兩張不重疊**：#512 那 5 個站是全新發現的另一批租戶，不在 #440 原本的 18 家清單內。

---

# 疑重複 — ✅ 已結案：**ci#30 於 2026-08-12 關單（superseded by ci#66），追蹤只看 ci#66**

> **2026-08-14 對帳**：ci#30 實查已 `closed`（`closed_at=2026-08-12T07:04:06Z`，結案留言 **6853**）⇒ **本段不再是待辦，只留一行歷史指標**。
> 「為什麼是 #66 承接而不是兩張各有範疇」的完整論證（同一起事故／三張 PR 全掛 `Refs #66` 沒有一張提 #30／#66 反過來更正 #30 的過期數字／#30 的方向清單沒有 #66 未涵蓋的獨立範疇）已隨關單留在 **ci#30 留言 6853**，**不在此複述**——複述就會漂。
> 🔴 **ci#66 自己的關閉條件仍未達成**（三條 gate-before-build 線各要真跑過一次 phase 2），ci#66 仍 open、留在 **②A3 段**，該段註記即權威版本。

## 本輪（2026-08-14，12 張新單）的查重結果：**無新增疑重複**

逐張核對關聯——**全部是刻意拆單或承接，不是重複開單**：

- **#703／#704／#706／#710／#711／#715／#716 vs #698／#705**：非重複。#698 已由 PR #705（`31dee82a`）修復關單，這 7 張是「刻意不混進該 PR」的分流與 post-merge 複驗殘留——
  #703（operator 側縱深防禦，Eugene 2026-08-13 裁決另案）、#704（#698 調查的附帶問題，明說不是根因）、#706（守衛不回溯 ⇒ 既有綁定要盤點）、
  #710／#711（PR #705 覆核留言 **6960** 的 F4／F5，leader1 裁決另案追蹤）、#715／#716（post-merge 複驗，#698 留言 **7019**）。
- **#702／#714 vs #694**：非重複。#694 範圍**只做平台 paas-odoo**；#702 是租戶 `svc-*-odoo` 側偵測（Eugene 裁決先偵測不預防）、#714 是 marker 告警（Eugene 2026-08-13 核准另開、明確不阻擋 #694 關單）。三者範圍互斥。
- **#696 vs #676／PR #695**：非重複。#676 修的是 **ir.rule 層**（已關），#696 是 **backend view 的 `groups` 層**，leader 明確裁決兩者是兩回事、不混入 #695 的覆核。
- **#699 vs #675**：非重複。#675 是「修正動作本身鑄出新假宣稱」的實錄案例，#699 是另外兩處 docstring 失準；#699 引用 #675 當前車之鑑而非重開。
- **#721 vs #694／#702**：非重複。#721 內文明確聲明不重複那兩張的範圍——那兩張關注 root pod 造成資料目錄 owner 錯誤的**可用性**問題，#721 關注「若開放自灌 addon，則以 root 身分執行租戶程式碼」的**安全面**。

> ⚠ 本輪查重只比對了 **issue tracker**。`docs/tracking/`（followups／pitfalls／landings）是**另一個互不相通的檢索面**，
> 那邊的 **RETRACTED／已否決條目最該讀卻最容易被跳過**——它記的正是「這件事查過了，結論跟你以為的相反」。
> 這個結構性缺口本身就是 **③ 架構方向段的 #681**，尚未解決。

---

# 從舊看板留下來的現況（三條，會過期，用前自己量）

1. **develop `f55eccc6` / `18.0.1.0.136`**（2026-08-14 leader3 量，最後一筆是 PR #720；前值 `83f2ac36` / `.131` / PR #692 為 08-12）。**這行同樣會過期，用前重量。**
   08-14 這次是**兩條獨立路徑交叉驗證**才寫上來的：`tea api …/branches/develop` ＋ `contents/src/__manifest__.py?ref=develop`（base64 解碼）對照 `git fetch` 後的 `git show origin/develop:src/__manifest__.py`，sha 與版號皆相同。單走 `tea api` 一條不夠——同族的 `raw?ref=` 有**靜默忽略 ref** 的前科。
   ⚠ **08-14 實例正好佐證下面那句「不要用工作區」**：當時本機 `develop` 落後 origin **22 個 commit**（工作區 manifest 停在 `.132`），而 `git rev-list --count @{upstream}..HEAD` 回 **0** ⇒ **`ahead=0` 不代表同步，落後方向零訊號**（已開 **#723** 承接）。
   ⇒ **不要用主 repo 工作區路徑當 develop 現況**，一律 `git show <ref>:path` 或 `tea api`。
2. **幽靈額度 gate 的假陰性長得像「整理文件措辭」**：同時加一個真 `.sudo(` 並潤掉一句註解裡的 `.sudo(`，字面數維持不變 ⇒ 靜默放行。引用請以 #576 留言 **5224** 為準。
3. **在落後很多的分支上跑 gate＝跑到舊版的 gate**（#590 的分支落後 53 個 commit，repo-lint 只有 16 條、沒有 `pattern-doc-registry`）。
   ⇒「綠」在那裡的意思是**沒被檢查**，與「通過了」輸出完全相同。正解：在 `merge-tree` 預覽樹上跑。

> 其餘 5 天份的方法論教訓（探針失效形態、保全順序、`Refs` vs `Closes`、載體等級…）
> 已在 `memory/MEMORY.md` 與 `docs/` 有持久載體，**不在本看板複述**——複述就會漂。
