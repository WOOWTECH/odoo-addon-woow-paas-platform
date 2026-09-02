# PaaS 上線推進看板（2026-08-21 精簡版）

> **本 note 只放 open issue 的分群結果**，供 Eugene 決定先做什麼。歷史過程、驗證證據、教訓段一律不放——它們在各 issue／PR 留言／`docs/`／memory 有持久載體，這裡只留指標。
>
> **對帳重算法（給推導方式不給死數字）**：`tea api -X GET "/repos/{owner}/{repo}/issues?state=open&type=issues&limit=50&page=N"` 逐頁抓到「該頁不足 50 筆」為止，再與看板表格**第一個 cell** 的 `**#N**` 取集合差。六個 repo：main `odoo-addons/woow_paas_platform`、ci `woow-paas/paas-odoo-ci`、cd `woow-paas/paas-app-bundle-cd`、charts `woow-paas/woow-paas-charts`、ar `woow-paas/paas-agent-runtime`、sh `ha-components/woow_paas_smart_home`。
> 🔴 `limit` 被 server 截到 50（不逐頁會靜默漏抓）；🔴 不帶 `type=issues` 會把 PR 算進來；🔴 owner 猜錯回 404、與「issue 不存在」分不出來；🔴 live 快照有效期是分鐘級，寫入前重抓。
>
> **發版現況**：prod 現跑 **v18.0.1.0.151**（08-18 rollout，證據在 cd PR#116 留言 7773）；落差基準以 prod DB 實裝版號為準，不寫死。🔴 **operator 側待發**：#826＋#832＋#839 三修已併 develop、均未隨任何 operator release（leader2 08-22 量測：`b030a16a` 不在 `operator-v20260821.2328` ancestor）——下次 operator 發版同批帶上。
> **上次修剪**：2026-08-23（issue-leader）——刪 20 列已關（14 張：#282/#289/#525/#533/#732/#740/#798/#822/#824/#829/#836/#841/#846/#850；認領表 8 列＋分群表 12 列）；派工政策段／驗令紀律段的已收官批次記錄與 MEMORY 拆檔驗證證據一併刪除（只留仍生效的規則與殘項）；已關的 #805 交叉指標刪除；建議優先順序的 #684（併 #544）已收官故重排。tea 六 repo 對帳 open＝main 144／ci 5／cd 11／charts 4／ar 3／sh 1；⚠ **main 有 8 張 open 尚未列入看板**（#870／#873／#874／#877／#878／#883／#884／#886）——本輪只做刪除、補列待指示。修剪前全文存 `docs/archived/paas-issue-memory-2026-08-23-pre-clean.md`。前次：2026-08-22 09:55Z（issue-leader）——刪 5 列已關、補 6 列 open、main repo 集合差歸零，全文存 `docs/archived/paas-issue-memory-2026-08-22-pre-clean.md`。

> 🔴 **MEMORY.md 索引解凍（2026-08-23 拆檔後生效）**：可新增索引行，規則＝link＋一句 hook、症狀字面留在 hook；任何 session 動 `MEMORY.md` 前**必先重讀磁碟**（各 session 的注入快照已全數過期）、**只逐行 edit 禁整檔重寫**。殘項：真實 bytes 上限本地查無設定（疑 built-in）續用 25000 保守值；traps 檔 assertRaises 三層整理仍待。

## Leader 工作原則

> 本段只放**仍生效的規則**；證據、量測值、歸因過程一律不放（載體＝issue／PR 留言／`docs/`／memory；本輪精簡前全文存 `docs/archived/paas-issue-memory-2026-08-24-pre-clean.md`）。

- `issue-leader` 分配 issue、獨家改本 note（一律 `maestri note edit`）；Leader 對本 note **唯讀**，要改請 `issue-leader` 改。
- Leader 一次只做一個 issue（或同組相關 issues），不排程無關工作；全部完成後 `git pull` 並 `/clear` 才接下一張。
- 取用請 `issue-leader` 在「🔒 認領中」登記＋該 issue 列標 🔒；完成請 `issue-leader` 兩處移除。**無登記＝無人處理。**
- 本 note 不當工作記憶，工作記錄自己 `maestri note create`；本 note 定期清已關 issue、同步 `tea` 可讀到的 open issue。
- 🔴 **manifest 版號（#691 改制；gate＝develop 唯一 required check）**：**A.** 一般 PR 不動 `src/__manifest__.py`，版號須**等於**當下 origin/develop（併完 `git diff origin/develop -- src/__manifest__.py` 須為空）。**B.** 只有「新增 `src/migrations/<X>/` 目錄（base 側 tree 不存在）」的 PR 才 bump——元組比較嚴格大於 develop、且新目錄至少一個 X＝新版號；新增目錄沒 bump ⇒ 紅 `migration-needs-bump`；只改既有 migration 目錄＝一般 PR。**C.** `release/*` 分支才 bump，相等 ⇒ 紅 `release-needs-bump`、不吃 out-of-scope 豁免。B／C 取號＝`max(develop, 其他在飛會 bump 的 PR head)+1`、多人同時由 issue-leader 指定、量完立刻 commit；被追過 ⇒ 重 bump＋目錄改名。低於 develop 一律紅（先 `git merge origin/develop`）；舊制已 bump 的分支改回 develop 現值。權威：`scripts/ci/manifest-version-gate.py` docstring、`docs/guides/development/merge-train-acceptance.md` §1。
- 🔴 **Merge 授權（Eugene 08-22 裁）**：純 docs／純 bug fix／test-only（零生產碼）／純 CI-config，**且無待討論取捨**（修法唯一明確）的 PR，issue-leader 於 CI 全綠＋覆核 diff 後直接 merge、不等 Eugene——類別**按精神非字面窮舉**；含取捨／行為權衡／動生產碼行為／feat／refactor／CI 政策類 ⛔ 待 Eugene。PR 標題一律掛「⛔ 勿代 merge」前綴（永不拿掉），尾段二選一：「待 Eugene 覆核」或「issue-leader 覆核後併」；類別終判與 merge 動作只歸 issue-leader、中途翻類就改標題；merge 前照 multi-PR 驗收工具箱（manifest 同值撞號、`git diff origin/<base>` 非空、base 前進重驗）。
- ⚠ **磁碟／沙盒 slot**：並行 disk-heavy 測試環境上限 **2**、起前向 issue-leader 登記（協調用）；free <10Gi 回單 slot、abort 線 6Gi 全隊適用；排隊與 abort 估算都按 k8s sandbox 口徑（首拉 ~2.5–3.5GB；容器路線 ~0.2GiB 不能拿來算）；收工立刻 destroy＋清理並回報 `df -k` 原始讀數、teammate 照保全範本後才 dismiss；slug ≤26。⚠ 機器 Avail 有隊外活動、以數百 MB 自漂 ⇒ 單點 df 差值不得歸因單一 agent 的沙盒生命週期。slot 現況看「五線在飛狀態」段。
- ⚠ **local 沙盒已知環境紅（兩機制，非你的碼）**：A）`.sandbox/post-create.sh` 把 `paas_domain` seed 成含 slug 值 ⇒ 斷言裸 `woowtech.io` 的 2F → **#804**；B）`AGENT_RUNTIME_NAMESPACE_SECRET` 未注入 ⇒ agent_session fail-closed 的 2–3E（口徑差待 #798 對照判掉）→ **#853**（兩候選成因未分辨）。歸因報告載體＝PR #843 body。⚠ #822（PR #851）「補上該 env 即 0F/0E」的環境＝本機手建容器（docker -e 直接注入、非 k8s 沙盒；此定性只在歸因報告、PR #851 本文未寫）⇒ 證機制、不證 local 沙盒注入得進去（`.sandbox.yml:47` 寫該 env 僅 stg 模式生效）；local 注入判準＝pod 內 `printenv`。
- 🔴 **docker-desktop hostPath 內容 gate（#814，plugin `951f49c4` 起）**：local `create.sh` 逐條比對 host/node digest，不一致或探針失效即 rc=1——紅了＝node 副本凍結（凍結點 ≤ node 容器建立、重啟不刷新）或探針壞，不是你的碼；正解 `docker cp` 進**自己專屬**的 node 路徑再建（別覆蓋共用 `paas-odoo-ci/addons`），`--allow-stale-node-copy` 不准自用、先問 issue-leader；隔離副本過渡作法已退場、新沙盒直接用共用路徑。驗「測的是我這份碼」看 host↔node↔pod 同檔 sha，不看路徑存在／exit 0。⚠ 驗 digest 必帶 `LC_ALL=C sort`（BSD/GNU 排序差 ⇒ 假漂移）；macOS `wc -c` 補前導空白、busybox 不補（`tr -d " "` 收斂）；刷新前已掛載的 pod 看舊 inode、要重啟；node 內舊副本 `addons.bak-20260822`（19M）刪不刪待 Eugene 裁；docker-desktop 上的歷史計費綠燈要重驗（舊副本缺 wallet migration＋billing SSOT 測試）；`~/gantry-47/paas-odoo-ci/addons` 是 0.0.4 誘餌（清掉或標 STALE 待 Eugene 裁）、查第三方 addon 用**內容**驗前綴。細節：memory `reference_k8s_sandbox_test_gotchas.md`、`topic_probe_failure_modes.md`、#827。
- 🔴 **升裁選項組必先驗共同前提（#385 A 案作廢後訂）**：三個選項共用一個未驗前提＝一個未驗前提偽裝成三個選項——不會以「某選項有缺陷」暴露，裁了才發現全不可行；至少一個選項必須是「先驗證前提」，內文標 ⚠ 但沒進決策結構＝對裁決者不存在；轉呈者（含 issue-leader）先問「共同前提驗過沒」。
- 🔴 **查重／對帳分頁到底**：`tea issues ls --limit` 封頂 50 且無截斷提示，一律 `tea api .../issues?state=all&limit=50&page=N`；輸出行數剛好等於上限就重數（`--state all` 與 `open` 同數字＝上限訊號非資料）。
- 🔴 **CI status 判「新跑非回收」用 `target_url` 的 run/job id（單調變大）＋`created_at`**，⛔ 不用 status `id`（每 commit 從 1 自編、非全域）；三項一起用會讓錯的 id 被對的兩項掩護，不要混進判準。
- 🔴 **保全落點掛在不隨 dismiss 消失的載體**（派工者自建 note／repo 檔案／issue 留言）——teammate 節點上的 note 一 dismiss 即失聯，「dismiss 前讀得到」零鑑別力；先搬到自己載體再 dismiss（dev-836 保全範本待補此條，leader3 執行）。⚠ df 差額不得當足跡交差、足跡以被刪物 `du` 為證；三件 doc 修正（df 格／首行也逐字驗／對照表定稿後才算）批一張 PR、#849 後 leader3 開，載體 #886 留言 9423；併入後刪此句。
- 🔴 **未署名訊息作者欄留空**：所有 maestri 訊息同一通道、「從哪到達」對作者零鑑別力；不用語境補作者（ghost 的形狀＝沒署名＋內容合理＋收端補作者）；轉述不確定的事寫「未署名、作者不明、供對時序」——語氣是傷害的一部分，「已確認、可以停」是在要求對方停止驗證。
- 🔴 **覆核紀律**：①集合比對通過 ≠ 預測成立——預測含成因時成因逐條比（#803 紅集合對稱差空、成因卻是前提 AssertionError 非預測的 KeyError ⇒ GREEN 後會空過、輸出與真守住一模一樣；#803 沒放行是靠測試自帶的前提斷言、非覆核方法抓到）；②解釋一件事會生產它的識別字（註解寫「本 cron 不走 provision 開閘」⇒ repo-lint sentinel regex 掃進違規；PR body 解釋「為何不關」⇒ 種出 close token）。
- 🔴 **驗令紀律（保留為常規防線；ghost 系列已結案＝Eugene 本人誤觸焦點視窗，但通道存在、且已兩次擋下真問題）**：執行類指令（起沙盒／merge／刪除）不憑語氣或參數正確性認令，判準必含至少一項「發令當下才量得到的新鮮狀態」；憑證用原始量測（`df -k <mount>` 的 Avail KiB＋mount；`df -h` 會把同一真值渲染成 11 或 12），判準是「發令者報得出當下讀數」不是「兩端數字相等」。輸入框出現來源不明未送出指令：不執行、回報 issue-leader；canvas 操作前確認焦點終端。
- 🔴 **通道與 check**：maestri 訊息可能靜默丟失——「沒收到回報」不得推論成「還沒做」，催前先 `check` 或自查產物。`maestri check` 在對方 idle 時是凍結快照（連計時器都不動）——「送了控制字元、check 沒變」既不能推論沒送到、也不能推論還在；判狀態＝問對方＋自己獨立量（`git status`／`ls` 實作物）。check 看到非預期畫面（面板／設定／對話框）一律當異常回報；「面板開著」與「凍結快照」不可區分、送 ESC 才能判。teammate 寫共用 memory 前先問自己 leader 落點、由 leader 向 issue-leader 宣告。

## 🔒 認領中

| Issue | Leader | 起始 | 狀態 |
| --- | --- | --- | --- |
| **#803** 🔒 | leader5 | 2026-08-22 | **Eugene 08-22 派**：Access 週期性收斂 cron（掃 access_protection_lost=True 重推、冪等、批次節流自選型）；⚠ 與 #798 同檔——#854 已併、排序前提解除；⛔ 真 CF 零寫操作。**08-22 五面調查＋推翻式複驗收官**（4 斷言被否證；patch target 修正＝三腿各自 client 路徑非 MOCK_REAP_CLIENT——照抄會讓否定斷言無聲通過）。🔴 決定性發現：cron interval 在 `<data noupdate=1>` 內＝**上線後改不動、選定即實質定案**（repo 兩處註解自證）。**題 2 issue-leader 裁 C**（游標式＋每輪彙總一行：掃 N 推 M 擋 K——不堵死、不靜默、不洪水；第一版不做時間基準、要做＝新欄位＋migration＋bump 連鎖、另開單）；**題 3 裁不開 force 旁路**（_resync_after_fail_open docstring 的正面主張＋#536 偽造面＋旗標是 UI 訊號非髒東西；SA suspended 是否等價 SH 503 列 teammate 必查、理由照實寫）；**題 1 Eugene 08-22 拍板＝B（15 分鐘，比照 reach_reconciler）**；⚠ noupdate=1 值即定案、五段推導寫進 XML 註解。dev-803（ccou）已代招。工單 `dispatch-803-leader5.md`＋leader5 的 workorder-dev-803-draft.md |

> **待派／派工政策（Eugene 08-23 拍板・五線分工）**：目標＝先功能上線。🔴 **08-23 Eugene 二裁：暫停分配新 issue——在飛的收尾到自然決策點、leader 收完手上不 pull 下一張、待命；恢復需 Eugene 明示。** **派工只從 ② 抽**；衍生守線單開單即貼 `derived`＋`pending` 凍結、開單≠排程，收割日（leader1 守門，衍生累積約 15 張或每週觸發）才批次處理。🔴 **`derived` 定義（08-23 拍板）＝主題延續**：該單的**主題**是另一張單的殘缺／續集／同族延伸才貼；「做別的事時發現的」（出處）不算、出處交給 body `Refs` 慣例——出處定義零鑑別力（leader1 量 18/18 全中＝不鑑別）。🔴 **抽單鍵（08-23 二裁）＝`derived` ∧ ¬`pending`**：`pending` 保持它自己的語意（卡決策待拍板），衍生單的「不排程」由 `derived` 本身＋政策表達 ⇒ 衍生∧沒卡決策＝收割日可直接動工；`derived`∧`pending` 交集＝收割不抽、走 Eugene。定義變更後的標籤修正由守門人（leader1）執行、可 remove 逐張記理由。`launch-first`＝**#107**（Eugene 點名；同批的 #36 已收官）。label 體系（A0–A5／cat-*／derived／pending／launch-first＋cat-out-of-scope 共 13 個）已建置（dev-labels）。
>
> **五線在飛狀態（08-23）**：**leader1**＝測試／docs／守線專責＋衍生單守門（手上空）。**leader2**＝A4 功能 bug（下一棒 #107 中文化 → #833／#860／#155）。**leader3**＝🔴 **#849 RED-A 量完＝確認現行跨租戶安全 bug、已升 Eugene**：租戶真 HTTP RPC 回 200 無 error，把自己一列 policy 的 `security_access_id` 寫成任意整數 id ⇒ 值落地、`route_id` 不動 ⇒ ir.rule 歸屬翻到別 workspace（4 次重複形狀相同；攻擊者失 write、**受害者獲跨租戶 read** 前後對照已量 False→True；攻擊後該列對所有租戶 write-locked）。🔴 **比 #848 寬**：#848 需共同成員（讀得到目標錨點），本單前提只 ⑴對自己一列 policy 有 write ⑵給得出整數 id。prod v151 **結構暴露**（兩投影欄在＋ir.rule 走它們＋#798/#848 守衛一道都沒上；⚠ 結構暴露≠已被利用、prod/stg 未實打）。🔴 **最重未驗格＝配額 DoS**（`_check_policy_quota` 純讀碼零執行；若成立＝把自己列推到別人 SA 吃掉配額且 write-locked 刪不掉）＝明標未驗升 Eugene、沙盒留可立即補。rule model 三欄（`security_access_access_policy_rule.py:57/65/71` 全 related+store 未宣告 readonly）併入 #849 範圍、不落單。⚠ **標題誤導待 Eugene 定**（現標題把錯機制寫最顯眼、更正在第二則留言）。證據＝#849 留言序 [9202, 9433, 9445]；沙盒 `dev-sandbox-849split` 留（TTL 08-24T03:55Z）、dev-849 停待命。**leader4**＝🔴 **#864 A 級四支查證＋10-agent 對抗覆核完成、升 Eugene 中**：四支判定維持（`api_invoice_pay` 最高優先／`api_topup` 不補-需重構／`cancel`／`bind_result` 可補）但**理由整批換**。🔴 **真正該動＝兩份報告都漏的現行金流 bug（不需並發、可能 prod 就有）**：A) `api_invoice_pay` 半成交（發票 posted＋點數扣＋無 payment＋無 reconcile＋顯示失敗、journal 沒設就觸發）B) `bind_result` 綁卡失敗前端仍收 ok C) skip-trial 發票**永不開**非多開（方向相反、default manual）。🔴 **bug class 重定義＝判準改「try 內有無同步 SQL」非「有無 catch-all」**；新判準下四支乾淨翻轉＝**三支攔得到、只 `api_subscription_cancel`（純 write 無外部副作用）inert**（⚠ try 內有同步 SQL 是必要非充分、還要 row 被並發改）。🔴 **`api_topup` 最嚴重推論（未驗、需並發）**：40001 落 try 內→吞→tx abort→cache-only→flush 對 abort tx 下 SQL→`InFailedSqlTransaction` 不在重試白名單→整交易回捲 ⇒ **ECPay 已扣款但 topup＋`payment.transaction` INSERT 全消失**（比半成交更糟＝連記錄都沒有；prod 唯讀查詢驗不了、要並發實打＝下輪第一標的）。🔴 **機制根據**（`sql_db.py:162-166` 的 `clear()` 五個呼叫點在一般 try/except 都不觸發 ⇒ dirty 值＋precommit callback 原封留著）：**非 DB 例外**（UserError）→ tx 健康 → commit flush 寫進 DB＝**半成交**（多做了、使用者看到失敗）；**DB 例外**（40001）→ tx abort → 回捲＝**扣款無記錄**（少記了、外部副作用留著）⇒ 兩者是同一判準維度的兩個方向、都與 `retrying()` 無關 ⇒ 補 `except OperationalError: raise` 對它們完全無效。未驗環境前提＝prod `company.auto_invoice`／`ecpay_demo_mode` 值（需 prod 唯讀查詢，待 Eugene 授權）。半成交 A 獨立成單／`api_payment_method_delete` ＋ vendored `payment_token.py:55` 獨立列＝進 follow-ups 恢復後開。dev-864a 保全**不 dismiss**（note `wt864-preserve` 依附 leader4 節點＋磁碟 `issue/864-*` 持久位置含 workflow-script.js＝14 agent 完整 prompt 下輪起點）。證據＝#864 留言 9453／9457／9458。**leader5**＝A1 provision／tunnel 鏈；**#385 P5 runbook Eugene 已授權步驟 1-5**（prod `paas-system` pod odoo shell 唯讀對帳、零寫入 CI-gate 保證、末行 rollback；硬條件＝gate E 記憶體餘裕 <500Mi 即停，防 cgroup OOM 殺主行程；CF token §4 另案另授權；第一發先報 issue-leader 確認）。🔴 #385 的 `reconcile_orphans(env)` 是**模組層函式非 model method**、`execute_method` 打不到（leader5＋dev-385 雙驗），且 inventory 回應不含 policy decision/include ⇒ `allow + everyone` 那格拿不到。🔴 **保全分支 `chore/803-preserve-followups`（遠端 `b7167c84`、⛔ 未開 PR、⛔ 不可清）**＝`verify-cron-wiring.py` +162 通用 gate ＋ 交接包 landing ＋ follow-ups F-803-1／F-803-2 ＋ `docs/index.md` 修正；剩 7 檔（含 4MB log）保全到 `artifacts-803`。🔴 **develop（`bcf37935`）`docs/index.md` 既有偏差＝總數 655／landings 69、實際應 656／70**（PR #887 併入時只更新總數漏 landings 格；`gen-docs-index --check` 在 `git add` 前重產故當下綠、且非 required check 不擋 merge、產生器與檢查器共用同一漏法）；修正已在保全分支、恢復派工後隨 PR 帶走。
>
> 🔴 **CI status 判準（08-23 兩次踩到）**：前窗口 `pending`/`total` 為 0、或 contexts 少於全套時，判準須「**期望 context 名字逐個出現**」而非只看數量（前 ~12 輪 context 一個都沒註冊，任何 `>=N` 都攔不住；issue-leader 曾誤判 #885「還沒跑」為「永遠卡死」）。⚠ `odoo-tests.yml` **無 `pull_request` 觸發**、PR 階段本就只有四個 context，backstop 要併入 develop 後 push 才觸發——工單別寫「等第五個 context」。⚠ 任何 PR head 引用一律當下重查、不沿用上輪值。

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
- 第 1 層：「要不要做／做什麼」未拍板（內文在問「該不該」「誰能」「哪一派對」）⇒ **③**。
- 第 2 層：「該做無疑，只是怎麼修未選型」（「修法方向未定案」「三個選項各有取捨」）⇒ **留 ②**——選型是實作的一部分。
- 兩層都要在危害所屬的 ② 分段補 🔴 **交叉指標**指回去（分類 ≠ 嚴重度）。
- 🔴 判定要讀 issue 原文，不讀二手轉寫（同一句「修法未定案」在 #578 是第 1 層、在 #732 是第 2 層）。
- 首批適用：#710／#704（→A1 指標）、#773（→A2 指標）；存量已於 2026-08-18 全數對齊。

---

# ② 實作類（96 筆）— 可推進上線

## A0 · prod 事故（1 筆）

> #512／#441／#442 經查證屬叢集運維，已移到文末 ⊘ 段；#503 與其復發防治 #694 均已關單——殘餘承接：租戶側偵測走 **#702**、marker 告警走 **#714**（皆在 ②A4／②A3 有列）。

| Issue | 一句話 |
| --- | --- |
| **#389** | prod SMTP 寄信全 exception；歸屬已查證＝問題在平台本體 Odoo（ns `paas-system` 的 paas-odoo pod）從頭到尾沒有設定過任何 outgoing mail server，不是客戶站（根因細節見留言 4434）。修復未執行、卡在決策：用哪家 mail provider、帳密由誰提供；之後才是建 `ir.mail_server`＋設 company email／catchall domain |

## A1 · 資安：租戶可實際利用（22 筆）

> 🔴 **交叉指標**——下列分流到 ③（理由＝修法界線未定，**不是危害低**，別把分類當成嚴重度），但都是租戶今天就能實際利用的洞、危害落在 A1，排本段優先序時一併看。✅ Eugene 2026-08-18 裁：「留 ③＋在危害所屬分段補交叉指標」升格為通用判準（規則全文見開頭「分類原則」段）。
> - **#710**：guest 直接 `unlink` project ⇒ project 連同 cloud_service 關聯整個從 workspace 消失（比 #705 擋掉的那條更徹底）
> - **#704**：租戶可自行 write `use_agent_runtime` 開 agent runtime、實際佈署 pod 吃資源
> - **#152**：project owner 可繞過 UI、直接經 ORM/RPC 寫 dedicated `agent_template` 的 `skill_ids`／`mcp_server_ids`／`system_prompt`；待決＝要不要限縮 admin-only（選項 A/B 未擇一）
> - **#711**：`_check_workspace_access` 只驗成員資格不驗 role，隨後 `sudo().create()` ⇒ `env.su` ⇒ 豁免 #705 的綁定守衛；4 個互斥選項未拍板（其一是產品行為變更）
> - **#385**：CF 孤兒盤點，其中一條是 `allow + everyone` ⇒ 孤兒可能是還掛在 Access app 上的放行規則；待決＝清哪些（成因與修法方向見 #528）
> - **#721**：租戶已有 `group_system`＋`erp_manager` ⇒ `ir.actions.server(state='code')` → `safe_eval(mode='exec')` 且 `'env'` 是本尊不是受限代理 ⇒ 資料層今天就等同 superuser（spike 單、不含實作承諾）；承接已落地 → **#774**（本段網路隔離子群，追的是 §5.2 的 OS 層逃逸而非資料層）
> - **#799**：CF 推送已生效但 DB 那筆回捲 ⇒ policy 孤兒殘留＋Access app 強制集合與 DB 分歧（replay 路徑單一錨點即可觸發、回 success 無聲）；待決＝要不要放棄「整批回捲、不留 ghost row」改 partial success（③ 架構列）

**跨租戶奪權／讀取（最高，會被真的用到）**

| Issue | 一句話 |
| --- | --- |
| **#718** | 上游 hermes `web_server.py` 未接 approval gate——`/api/memory/providers/{name}/setup` 的 `install_cmd` 走 `shell=True`＋`bash -c` 無危險指令守衛，已登入租戶可達且 `check_cmd` 被動觸發（Plugins 頁載入即跑、Install 按鈕守不到）。歸屬上游 ⇒ 已決策回報上游＋內部記錄、不自行 patch。🔴 #717 的 chart 0.2.0 落地後風險敘述已變：`install_cmd` 從「無 gate 但跑了也裝不起來」變成會成功（落點 `/opt/data/.local/bin`）——非新洞，但排修復優先級以新敘述為準；圍堵載體＝`docs/guides/deployment/hermes-agent-console-support-runbook.md`，根治仍是上游接 approval gate。詳見 issue 留言 7582；架構事實見 `docs/decisions/hermes-agent-command-execution-planes.md` |
| **#842** 🆕（08-22 leader2 開；#824 覆核衍生、低優先） | tombstone-check 的 tunnel 存活判定只涵蓋本 account——指向他人 account 的 cfargotunnel 目標會被判成孤兒而准接收（同 account 綁定下理論不出現、未實測）；Refs #824 |
| **#848** 🆕（08-22 leader3 開；#798 順帶發現、可達性低） | access policy 三個錨點 Many2one 在 model 層無歸屬守線——今天擋著的三樣機制都**不是為此設計**（check_access 是 #786 排序碼副作用、改動者無義務保住）；已驗/未驗照 #824 框架分列、明寫非今日線上可利用；Refs #798 |
| **#849** 🆕（08-22 leader3 開；issue-leader 觸發的欄位定義閱讀升級而成） | 兩個 stored related（smart_home_id/security_access_id）**已驗確實裝著 inverse 委派**（fields.py `setup_related`：readonly 不在 related_attrs 繼承清單 ⇒ 條件成立裝上 `_inverse_related`、寫的是**來源列**）——繞過「在錨點欄位加守衛」類修法的側門、route 端也無守衛；🔴 緩解事實併載：inverse 無 sudo ⇒ 目標列 ACL/ir.rule 仍適用；端到端未驗；Refs #848 #798 |
| **#823** 🆕（08-22；#531 stage 2） | 翻 operator `TUNNEL_OWNERSHIP_REQUIRE_CLAIM` 預設值——stage 1（#826）已併，翻預設前缺 claim 的 tunnel 仍不受歸屬驗證保護 |
| **#706** 🆕 | 承 #698／PR #705 的既有綁定盤點——`cloud_service_binding_guard` 只擋新寫入、不回溯，既有跨 workspace 的 project↔cloud_service 綁定不會自動消失，且經 `_reconcile_agent_runtime_reach` 餵給 agent pod 的 egress allowlist 與 `reach-ws-*` label 的 reach 授權照樣有效。跨 workspace 綁定本身是合法設計、無法機械判定攻擊或合法 ⇒ 本單只做唯讀盤點，盤完才決定要不要清、怎麼清（若需 migration，照 #691 新制 bump＋目錄同號，見上方工作原則）；讀 prod DB 那段走 `woow-operator` agent |
| **#787**（資安追蹤；A 段 ✅ 交付、leader2 08-21 收工） | context-key 偽造家族普查（承 #536／#788）：A 段 5 把 truthy 閘**全部 enforced**（registry pending 已空）——`cs_skip_access_sync` PR #793（merged）、`sm_*` 三把 PR #795／`ccd987b2`（merged）、`woow_force_issue` **PR #812 已 merge（08-21 11:39Z）**（.174；portal 身分真 call_kw 可達、fail-safe 方向相反＝不開立且必須吵、ECPay cron 只撿 pending/failed ⇒ 靜默早退會變「帳本 issued、ECPay 空」；獨立覆核七點證據在 PR 留言 8313）。衍生：`test_acl_p9` 測試名誇大（#787 留言 8311，待開單）；B 段由 #783 承接。issue 待 Eugene 關或續追。 |
| **#803** 🔒 leader5（08-22 派；承 #802） | 平台沒有週期性 Access re-sync ⇒ #802 的 best-effort 補償重推失敗後，`access_protection_lost=True` 的列要等租戶下一次自己觸發 sync（可能永遠不來）。本單＝補那個收斂者：一條 cron 掃旗標列重推 `_sync_access_to_operator()`，四腿（SA route／SH route／CloudService／SmartHome）都要涵蓋。歸 A1＝延遲成本是對外 hostname 多開放一段時間（⚠ 兩可：A4 資料一致性）。動手前：頻率給推導方式；被 skip 的列要不要永遠重試（#791 F1 佇列堵死形狀）；停權中要不要開 force 旁路（拍板題，作者傾向沿用 #802 不改產品行為）。 |

**憑證外洩**

| Issue | 一句話 |
| --- | --- |
| **cd#24** | prod registry 密碼明文躺在 `paas-operator-registry` 的 last-applied annotation（`priority:high`） |
| **cd#78** | 輪替已外洩的 Gitea helm repo `read:package` token |
| **cd#117** 🆕 | cd#115 施工副產物：yq 過濾 regex 沒防 base64 `+/=` ⇒ stg fleet secret 的 `auth.apiKey`（用途待查）與 `registry.password`（JCR，username=woow-prod）明文進 agent 逐字稿、已隨 tool result 離開本機邊界（CF token 未外洩）。⚠ 待 Eugene 裁決兩值輪替與否；與 cd#24 關聯——若 stg/prod registry 同顆密碼，本次是第二個外洩載體 |
| **#567** | 例外原文外洩面：`MissingError` 未收斂＋paas.py 3 處／billing.py 5 處 bare `str(e)` |
| **#860** 🆕（08-22 leader2 開；#740 衍生、#567 家族該單未含本處） | 租戶刪除端點 `_delete_smarthome` 把 Odoo 例外原文整段回租戶，含 model 技術名＋允許群組名（實測回應含 `(woow_paas_platform.smart_home_route)`＋群組名）；與 #524 已認定形態同型（該次只動 refresh_status、delete 留著）；判準承 #553/#567；Refs #524 #567 #740 |
| **#584** 🔄 | 依判準第 2 層自 ③ 搬入：§一 已由 Eugene 2026-08-05 裁「bn_odoo 密碼不用輪替」。🔴 搬進 ② 不代表結案——同則裁決寫明「§二 的 4 條未查證線索仍未處理，本單不因 §一 裁決而關閉」，剩下是待辦：4 條線索（含與 #556 相關的 prod 帳號歷史）要不要查、誰查；查 prod 帳號歷史那段走 `woow-operator` agent |

**網路隔離**

| Issue | 一句話 |
| --- | --- |
| **charts#42** | `charts/open-design` NetworkPolicy 跨 ns egress 洞（np-audit FAIL，block PR #41） |
| **charts#66** | skill sidecar 專屬 egress 收斂（externalEgress 開啟時 untrusted bash 可外連） |
| **#514**（設計已併、實作待裁） | L2 租戶 ns baseline egress 收斂。design PR #825 已併；🔴 核心結論＝A 群 10 個在售 template egress 全開、本案做完也一樣（治本＝charts repo 把 open-design/hermes-agent 的 ipBlock 形狀移植進 4 支 `allowAllEgress` chart，草稿 PR #825 §12）；待 Eugene 裁 R1/R2/R3（§5.4）＋D-2／D-3（587）／D-4 後重新認領實作 |
| **#428** | odoo chart 28.2.10 `containerPorts.https` 未定義 → paas-odoo NP 全 TCP port 開放 |
| **#427** | Phase 2：paas-system 補 default-deny baseline NP（**前置：待 prod 有租戶後**） |
| **cd#85** 🔄 | 依判準第 2 層自 ③ 搬入：body 已載 Eugene 2026-08-02 裁「切斷——控制面不應對公網開放」。`paas-api.woowtech.io` 是未版控的 prod 控制面公網入口且走 stg tunnel；方向已定＝切斷，剩執行前置：#427 的 NP 上 prod 後這條入口即失效 ⇒ 先確認還有誰在用、順帶盤點其他 tunnel。⚠ 與 #427 是同一個切斷動作的兩端，排序要一起看 |
| **#774** 🆕 | 承 #721 §5.2：驗證租戶 Odoo 的 `safe_eval` 能否經非 dunder 方法鏈間接觸達 `subprocess`／`socket`／檔案系統逃到 OS 層（資料層等同 superuser 不是本單標的——爆炸半徑侷限自己的 DB）；成立則落點是 root 容器＋egress 不受限的網路位置＝叢集面，且 #721 §1.3 的「OS 層才是自灌 addon 的真增量」整個翻掉。🔴 兩個前提（uid=0 root／egress `{}`）取自 #721 開單當時、本單未重驗，#514 正在改後者 ⇒ 動手前先看 #514 進度。驗收：否定性結論必配陽性對照、只能寫範圍限定敘述（不得寫「safe_eval 是安全的」）；用 dev sandbox 不碰 stg/prod；找到出口先回報不公開 PoC |

**agent runtime 沙箱**

| Issue | 一句話 |
| --- | --- |
| **ar#34** | sidecar 來源的 `tool_result` 未標記 untrusted-content（prompt injection） |

## A2 · 金流與計費（6 筆）

> 🔴 交叉指標 **#773**（分流 ③、危害落本段）：CS 降級到無 AC seat plan 後 access policy／CF Access 規則仍生效 ⇒ 付費界線被繞過；進 ③ 因收斂未拍板、非危害低。與 #521 分工：#521＝建不起來，#773＝建起來後不會被收回

| Issue | 一句話 |
| --- | --- |
| **#521** | CS 型 access 加購（`ac_max_access_seats`）——載體缺口已補，剩席數 enforcement，繫於 #381；衍生收尾票 #753→A1、#758／#760→A4、#750→A5 |
| **#518** | `max_per_partner` 並發防護在 Odoo REPEATABLE READ 下零貢獻（advisory lock 取鎖後仍讀舊 snapshot） |
| **#515** | HAOS 開通前容量 pre-check——上限數字與實際資源脫鉤時仍會超賣 |
| **#361** | 改方案後要讓使用者看到實際退了多少點 |
| **#45** | subscription：stg/prod `end < start` non-trial sub 的 audit + dry-run backfill |
| **#43** | subscription：`.get(unit, relativedelta())` silent fallback 換成 `UserError` + 抽共用 `cycle_delta` |

## A3 · 發版鏈／CI／部署環境（32 筆）

| Issue | 一句話 |
| --- | --- |
| **#827** 🆕（2026-08-22 leader2 開；#814 gate 首次實戰擋下後的根因單） | docker-desktop node 的共用第三方 addon 副本是**另一條版本線**（`sh_subscription` 0.0.4 vs host 18.0.1.0.0；排除 `.DS_Store` 漂移 20 筆；node 缺 `sh_subscription_wallet` 的 migration＋3 支測試含 billing SSOT ⇒ **計費相關沙盒綠燈驗的是殘缺 wallet**）；⛔ rm-rf 刷新禁用（破壞先於還原且無備份），安全程序＝cp 暫存→驗證→mv 原子換留 .bak；過渡＝各自隔離副本。刷新由 leader2 於 #648 收工後執行。Refs #814 #563 |
| **#821** 🆕（08-22；#546 衍生的 C5 盲區） | 全套測試 workflow C5 對 setUpClass／tearDownClass 錯誤報綠——`_ErrorHolder` 的 logger 是 `unittest.suite`，FAILURE_LINE 抓不到 ⇒ class 級 setup 炸掉時 C5 仍 PASS |
| **cd#70** | prod chart bump ≥0.1.18 前須移除 `values-prod.yaml` 的 `pvcCleanup.image` override（`priority:high`，否則 CronJob ImagePullBackOff） |
| **ci#66** | 只剩驗收：三條 gate-before-build 線各真跑一次 phase 2（關閉條件留言 6367），runbook 留言 7516（連 #68 一起收）；ci#30 同一事故，追蹤只看 #66 |
| **cd#77** | `SKIP_MODULES_UPDATE` 三段手動流程脆弱＋bitnami 吞掉 `-u` 輸出 ⇒ prod migration 執行是黑箱 |
| **cd#88** | agent-runtime chartVersion 追平 0.4.5 ＋ stg 既有 release reap 到 1.4.4 |
| **cd#83** | `paas-odoo/fleet.yaml` 加 `disablePreProcess`，消滅 promote 時的人工同步點 |
| **ci#68** | 只剩驗收：3 線各一次真實 phase 2＋一次 /reject 清理證明；⚠ 待裁決：/reject 線要不要補 /approve（留言 7520） |
| **ci#114** 🆕 | 承 #112／PR ci#113：`cleanup-orphan-candidate-tags.sh` 排除清單讀 open issue 值未驗封緘，但方向是排除刪除且有年齡兜底 ⇒ 評估單，無人認領 |
| **ci#115** 🆕 | 承 #47／PR ci#116：workflow run: block SC2086 補引號＋`WORKFLOW_RUN_SHELLCHECK_SEVERITY` 收緊到 style；🔴 順序約束：須在 #66/#68 六線驗收後或同批，無人認領 |
| **ci#48** | `main` 無 branch protection——已查證不會卡死單人自核（留言 7522），開關剩 Eugene 按（🔴 唯一禁忌＝勾 block_admin_merge_override）；⚠ 附帶：CODEOWNERS 檔頭「Require approval from Code Owners」在 1.27.0 不存在，回寫檔頭時一併修正 |
| **charts#44** | `publish-charts.yaml` 的 `set -e` 把 Gitea Helm push 與 OCI push 綁死（transient 500 誤擋 OCI） |
| **#510** | `/woow:deploy-stg` 的 `addon_ref` 是誤導性 no-op——宣告了卻從未進 CI payload，實際永遠烤 develop |
| **#382** | `pvc-cleanup` CronJob 仍用 `bitnami/kubectl:latest`，要換 bitnamilegacy |
| **#472** | `Dockerfile.dev` 已失效——COPY 不存在的路徑，且 base 缺 6 個第三方相依 |
| **#563** | k8s dev sandbox 的 local/hostPath 模式在 Docker Desktop 下不可用；且 `create.sh` exit 0 掩蓋失敗 |
| **#840** 🆕（08-22 leader1 開；#829 衍生、範圍裁 C 產物） | 三支 seed 腳本（e2e-seed／e2e-seed-reset-button／seed-from-stg）同族沙盒資源名推導寫死、隨 slug 長度變形（trunc 43）；症狀分三級表在 issue body；Refs #829 |
| **#855** 🆕（08-22 leader2 開；#740 查證衍生） | DB 錨點指向已死 CF tunnel 對兩個對帳方向**結構性不可見**（右減左無可迭代資源、左減右只建 Access app 腿）＋cron 於訂閱存活時永不觸發；補 tunnel 腿前置 #833；Refs #740 #794 |
| **#857** 🆕（08-22 leader4 開；#525 未收的 follow-up） | `_list_services` catch-all 自己會拋——log 讀已刪 record 欄位 ⇒ MissingError 逸出、單一服務失敗升級成整批清單 500；#568 savepoint 後危害降未消（已驗/推論/結構事實三分標註）；⚠ 修時勿順手收緊 catch-all、測試避 #572 直呼假綠；Refs #525 #568 #572 |
| **#864** 🆕（08-22 leader4 開；#533 枚舉衍生盤點單） | 23 支 @route 的 except 鏈吞並發類例外 ⇒ retrying() 失效（定義隨數字給；A 金流 4／B 同型 skip-trial 4／C 其餘 15 三級逐支列名；6 支不攔 Exception 當正確形態對照組；A 級重播副作用逐支評估、ha_paas provision 與 #822 併看）；Refs #533 #518 #799 #822 |
| **#859** 🆕（08-22 leader2 開；#740 C/A 形態衍生） | tunnel 已刪但 fail-loud guard 宣稱它「還活著且在計費」要求重試——清 3 錨點的欄位級 ACL 失敗被 `_cleanup_tunnel` except Exception 吞成保留待重試＋operator 對 404 當成功 ⇒ 每次重試重複同一句假話；🔴「unlink 權與 tunnel_token 欄位權綁一起是 ACL 巧合非不變式、改 ACL 者無訊號」明寫；與 #855 差別（偵測缺腿 vs 訊息說謊）；Refs #740 |
| **#853** 🆕（08-22 leader1 開；#843 歸因衍生） | local 沙盒全套必見 3E（agent_session fail-closed）——成因兩候選未分辨（local 不注入 AGENT_RUNTIME_NAMESPACE_SECRET vs test-isolation env pop；318 landing 的候選 2 描述經抽查兩處疑過期、未窮舉不排除）；CI 側已單點補 env 而沙盒側無對應；單沙盒判別法在 body（⚠ 已被 #798 沙盒的 printenv＋delta 量測取代：候選 1 對 local 成立）；Refs #843 |
| **#862** 🆕（08-22 leader1 開；#846 範圍外衍生） | repo-lint paths 白名單與 check 實際輸入**沒有對帳機制**——#846 只補一格、另七個輸入檔仍在白名單外（含 CLAUDE.md 明載的跨 cluster 事故守衛 kube-context.sh）；兩路線並列不裁定（手動補清單 vs 機械對帳 self-test＋後者自身的枚舉盲區風險）；核心主張已量測（全 repo 無任何程式讀 repo-lint.yml、14 份散文契約指著它）；Refs #846 |
| **#865** 🆕（08-22 leader1 開；#846 耗時量測衍生） | repo-lint job 執行時間單調成長（8 天 p50 63→118s、排隊 0 秒＝純執行；41 天資料佐證、口徑表三單位並列）而無任何東西在看——#846 第二層與 #856 的共同輸入不是常數；成因刻意不猜；⚠ 逐日 p50 不取全期中位數（單調序列上全期 p50=42s 不描述任何一天）；Refs #846 #856 |
| **#856** 🆕（08-22 leader1 開；#841 驗收查證衍生） | `extra/paas-operator/` pytest 套件**零 CI**（push/schedule/pull_request 三種觸發都沒有）——🔴 #546 body 08-03 已辨識並承諾「另案」但**該案從未被開**（零訊號漏法：人人以為有人接了）；單子只記缺口不主張觸發策略（odoo-tests.yml 檔頭先例＝刻意不掛 pull_request 的 runner 經濟學＋paths-ignore 判準已引）；與 CLAUDE.md「integration 無 runner」分格明寫；Refs #841 #546 |
| **#834** 🆕（2026-08-22 leader2 開；dev-648 保全） | #648 的 clear-on-view 路徑（`_refresh_public_url_dns_flag`）零測試覆蓋——壞掉症狀「軟警告永遠不消失」與「DNS 真的沒好」不可區分，無自然訊號只能靠測試守；⚠ #832 的 `test_flag_is_cleared_once_dns_lands` 測的是 poll 路不是這條。Refs #648 |
| **#835** 🆕（2026-08-22 leader2 開；#832 CI 紅根因） | repo-lint 檔頭條目放到「用法：」之後照樣全綠——gate 斷言清單無「條目所在區塊」檢查；注入錨點寫死版面家族第三例（#598→#613→PR #620），這次形狀＝真實檔案可違反錨點預設版面而無人檢查，補法是把位置升格成 gate 性質、不是再改注入器。Refs #832 |
| **#837** 🆕（2026-08-22 leader1 開；run 4763 首趟後） | test-collection gate 首趟落地後兩筆帳：(a) baseline 檔頭「尚未與真實執行清單對帳」已被 C3 653/653 證偽＝過期敘述 (b) green-streak.txt 未建、run 4763 是第一筆合格紀錄、append 是刻意人工步（CI 自動 commit 會成觸發迴圈）——誰 append／不做／自動化 guard 形狀待 Eugene 裁。查重含分頁陷阱迴避（130 張 open 全掃、鄰居 #467 #836 #828 #821 皆已分格）。Refs #546 |
| **#838** 🆕（2026-08-22 issue-leader 開；dev-648 保全收整） | repo-lint `--update-baseline` 一次重產**全部** baseline——靜默調降風險 in-code 註解已自認兩處（`:1738`／`:3881`，因此那兩份清單刻意不放 baselines/）但四檔仍暴露、「只增不減」自保程序無文件；修法三選項列於單內。⚠ 未查 closed issues。Refs #832 #835 |
| **#731** 🆕 | 五支唯讀腳本 `--context` 可選 ⇒ #32 把「靜默打對」換成「靜默打錯」；刻意不做強制，待裁決收斂方向 |
| **#722** 🆕 | onboard profile `KUBE_CONTEXT`＝`docker-desktop`，`create.sh` 不帶 `--context` 靜默打到本機叢集；與 #32 相乘非重複；`kubectl config use-context` 仍在兩處 ⇒ #32 Part 2 皆未修 |
| **#714** 🆕 | #694 filestore-hashdir-guard 刻意 fail-open，四個 marker 只印 initContainer stdout ⇒ 與正常無法區分，要接告警（🔴 硬約束三條）；僅平台 paas-odoo，租戶側走 #702；Eugene 2026-08-13 核准另開、不阻擋 #694 關單 |

## A4 · 功能缺口／bug／UX（26 筆）

**壞掉的功能**

| Issue | 一句話 |
| --- | --- |
| **#833** 🆕（08-22；#648 收尾時發現） | operator `get_tunnel_status` 不濾 `deleted_at` 而 `list_all_tunnels` 有 ⇒ 軟刪除的 tunnel 被平台寫成「未連線」類錯誤狀態 |
| **ar#23** | LLM tool-call 一律傳空 kwargs：**skill 執行時 input 參數整個遺失**（stg 1.3.0 / MiniMax-M2.7） |
| **#155** | `SKILL_BUNDLE_TOKEN` 1h TTL：pod 活超過 1 小時後 skill-bundle fetch **必 401** |
| **#316** | stg 控制面 MCP tool sync 疑被 baseline default-deny 擋（`state=error`／`tool_count=0`） |
| **#207** | operator mcp sidecar route 的 deploy/delete 命名不對稱 → 有 sidecar 的 CloudService 漏 mcp CF route |
| **#388** | `agent_session` spawning→active 未轉：spawn webhook 200 但 state 停在 spawning |
| **#562** | postgresql／redis template 重新上架前必須補 image override（Bitnami 搬遷後預設只剩 latest） |
| **#655** | paas-operator `helm.py` 兩處把 ResourceQuota `persistentvolumeclaims` 寫死 `"10"`，沒比照 cpu/mem/storage 走 `_compute_ns_quota()`——HAOS 每台 3 顆 PVC，同 workspace 開 4 台以上撞死上限。⚠ K8s hard quota 無「0＝不限制」語意，要不限只能省略該 key |
| **#739** ✅ 已修復並 merge（PR #797，Eugene 裁「是 bug，照修」見 issue 留言 8155）、issue 仍 open——待 Eugene 關單或說明殘餘；衍生 #805 | **租戶經 `/woow` 的 Smart Home provision 從來沒有成功過**：`tunnel_token` 帶 `groups='base.group_system'`，`_provision_smarthome` 非 sudo 呼叫 `action_provision()` ⇒ 必撞 `AccessError`、卡 `state='provisioning'` |

**資料一致性／並發**

| Issue | 一句話 |
| --- | --- |
| **#549** | reach reconciler 的 cron 路徑同樣 lock-then-recount（#518 機制第四站，且是原型） |
| **#363** | reset saga 並發：client 逾時後重按會與 in-flight saga 競態，rollback 可能覆蓋較新密碼 |
| **#342** | change plan 後 CS 規格 re-apply（#332 殘段） |
| **#702** 🆕 | 承 #694：租戶 `svc-*-odoo` root-owned 資料目錄偵測（Eugene 已裁決先偵測不預防）。唯一 root 路徑是人工 `kubectl exec`、無守衛收斂；只建唯讀掃描器，判準只認 owner（mode／group 恆綠）、讀 `/proc/1/status` 取真實 uid、範圍含 filestore 與 `sessions/` |
| **#760** 🆕 | CS Access policy `create()` 打 2 次 operator sync（policy＋rule model 各一次），CF 上多建一條 reusable policy 再刪；update 路徑的抑制不可直接沿用，修法待定；sync #2 失敗 ⇒ 永久 CF 孤兒 policy。非 blocker（#747 已關）；A4／A5 兩可，孤兒窗口是歸 A4 的理由 |

**migration 地雷（舊 DB 還原會炸）**

| Issue | 一句話 |
| --- | --- |
| **#575** | `.37` migration 的 fan-out 對 pre-`.39` DB 撞 `_check_at_least_one_include`（同家族第二顆） |

**UX／前端**

| Issue | 一句話 |
| --- | --- |
| **#758** 🆕 | ServiceDetailPage 只在 `onMounted` 載資料，SPA 內從服務 A 切到 B（同 component 不重 mount）**整頁沿用 A 的舊資料**，影響所有頁籤（#746／PR #757 時發現）。無資安面；修法方向已給，順檢 SA／workspace detail page 是否同形 |
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

## A5 · 技術債／重構（9 筆，有餘力再做）

| Issue | 一句話 |
| --- | --- |
| **#415** | 收斂 HA provision 與 marketplace **兩套平行的 CloudService 建立邏輯**（已漏鏡射兩次） |
| **#509** | PR#505 review follow-ups——G4 EnvVar replace 分支盲區＋H1 gate 獨立守線檔＋H2/H3 文字債 |
| **#668** | #657／PR#666 覆核衍生三項，**皆已實測確認非現行可利用洞**（刻意不在 #666 修）：① grantor lookup `_order='role desc'` 吃字串字典序不是權限高低；② X8 o2m 攻擊測試的可達性陽性對照與攻擊斷言分離；③ `invited_by_uid` 可被 context 偽造 ⇒ 事故追查時不可信 |
| **#84** | skill `is_latest` drift fix + e2e（PR #81, F-SK-1~9） |
| **#50** | 全 repo API URL pattern 統一到 cloud_service nesting |
| **#25** | 統一 SH/CS controller 的 role check pattern（抽 `_require_admin_role`） |
| **#750** 🆕 | #743／PR #749 衍生兩項收尾：① `api_sa_change_plan` 對 template-less SA 跳過 plan-family 白名單 ② `_get_display_specs` 把 0 顯示成「0 policies」與新語意矛盾。觸發族群 08-15 查皆空集合 ⇒ 低優先，不擋 #521 主線、非 #743 收尾條件；⚠ 族群哪天非空，① 升級成 A1 等級、重歸類 |
| **#319** 🔄 | （2026-08-18 自 ③ 架構搬入：已有「## 決策」段）Longhorn SC 策略：default SC 維持 `longhorn`(Retain)、租戶 PVC 於 chart values 明示 `storageClassName: longhorn`；剩餘＝Released PV 清理 SOP＋backup target。🔴 硬約束：48 顆 Released PV 中 ~39 顆屬其他部署絕不動；default SC 是叢集級，動前與叢集運維協調 |
| **#396** 🔄 | （2026-08-18 自 ③ 架構搬入：原文已給技術方向）orphan PVC GC 搬進 paas-operator，去除 chart cronjob 的 shell-image 依賴。原單自陳「非緊急（#45 已止血）、屬技術債」——歸 A5 正是照這句 |

---

# ③ 需討論類（26 筆）— 需求／策略未定，別自行開工

## 政策與商業規則（先決定「要什麼」）

| Issue | 要 Eugene 決什麼 |
| --- | --- |
| **#323** | 使用者主動取消訂閱**不退點、不拆服務**——與「刪除服務退點」行為不一致，哪個對？ |
| **#589** | unbound project 存取判準在 4 條 ir.rule ＋ 1 個 controller helper 之間 **2:2 分裂**（follower-only vs follower OR create_uid）——要決哪一派對，不是誰偏離 |
| **#667** 🆕 | `project_access` 成員資格**不蘊含 `project.project` 記錄可讀**（連 admin 成員都讀不到）。要決是不是預期行為：若否 → 加一條走 `project_access_ids` 的成員 rule（⚠ Odoo 核心模型、爆炸半徑大）；若是 → 正規路徑明文寫進 docstring。出自 #657／PR#666 覆核，定性為設計面問題非安全洞 |
| **#152** | dedicated `agent_template` 的 RPC 寫入是否應限縮為 admin-only |
| **#381** | AC/CF end-user 認證策略——end-user 走應用層／service token？Access seat 留管理者？（含 CF 成本懸崖） |
| **#394** | SA IdP 限縮 Google+OTP 的歸屬（#272 未明文） |
| **#735** 🆕 | **`transfer_ownership` 要復活還是刪掉？** 死碼且就算呼叫也走不完——兩道獨立缺陷（#556 的 grantor 降級、#658 的 `owner_id` anchor）🔴 須一起處理，只修一道＝換個地方死。關鍵輸入：無替代路徑，租戶側今天做不到所有權轉移。⚠ 與 #732 修法會打架 ⇒ 復活前先看 #732 裁決。已記錄至少三次卻從無載體承接（#681 案例） |
| **#711** 🆕 | 同一動作兩層界線不一致，**要決對齊哪一邊**：sudo 產品端點 `_create_project` 只驗成員資格不驗 role ⇒ 繞過 #705 的 `cloud_service_binding_guard`，任何成員含 guest 可綁；跨租戶已被擋住、非提權漏洞。4 個互斥選項未拍板（放寬 guard 與 #676 不一致；清死碼可獨立做）。判準 `docs/decisions/patterns/two-layer-acl-lockstep.md` |
| **#773** 🆕 | CS 降級到無 AC seat plan 後，**既有 access policy 與 CF Access app／policy 繼續生效**。🔴 CF 孤兒偵測對此失明，與 #528 方向相反、不能共用 fix。⚠ 規格面 F-368-1／#342 已定案；本單是 entitlement 面（#744 缺「降級」）。要決：收不收斂、收到什麼狀態（3 選項）、CF 同步方式、是否與 #381 同輪。⚠ 歸類兩可：按危害應進 ②A2（同 #710／#704） |

## 架構方向（做法有兩條以上路）

| Issue | 要 Eugene 決什麼 |
| --- | --- |
| **#578** 🔄 | **要決對外 API 契約要不要改**（2026-08-18 自 ②A4 搬入）：rollback 是第六條帳本失準路徑且方向相反、#552 fail-closed 擋不到；修它必須先改 operator `rollback_release` 的 `response_model`——issue 原文「設計決策，不由實作者代決」。3 個待決點未拍板。⚠ 同函式另有 suspended 處理既有缺陷，可一併評估 |
| **ar#33** 🔄 | **要決「修掉還是明確接受並記錄」**（2026-08-18 自 ②A1 搬入）：sidecar 內 bash 與 bridge daemon 同 uid，可對 PID1 送 SIGTERM 觸發 drain；PID namespace 隔離不涵蓋 network ⇒ 可連 agent loopback 埠（⚠ 前瞻約束）。issue 自陳嚴重度低（自傷型）但可重複觸發 ⇒ 要的是一句裁決不是修法選型 |
| **#770** 🆕 | SH 第二條 route（使用者自建對外服務）——#768 A 案的後續啟用票：含 #484 dns_failed 失敗模型重設計、HA component 跨 repo lock-step、quota 語意拍板（sa_max_routes 0=none 與 sa_max_access_policies 0=unlimited 相反）。前置 #768 已關單（PR #771 merge）不再阻塞；需求時間點未定 |
| **#681** 🆕 | issue tracker 與 `docs/tracking/` 是**兩個互不相通的查重檢索面**——代價是重複得出 F-DT-4 的結論、還開了前提錯誤的 #665。🔴 核心難題：查重是「動手前」的行為、沒有可掛 hook 的事件，做不成 gate。4 個提案、單裡不預選方向 ⇒ 要決「接受只能提高可發現性」還是投資做工具 |
| **#383** | F-ENV-8 admission 層補強（Kyverno/OPA）當 RBAC backstop——要不要引入 |
| **#276** | [Spike] 評估「一 workspace 一 namespace」合併（現況 CS per-workspace + agent-runtime per-project 並存） |
| **#385** | CF tunnel 累積清理評估——reconcile 報 79 tunnel＋10 access_app＋7 policy 孤兒候選，清哪些 |
| **#710** 🆕 | **要決「誰能刪 project」的界線**——`project.project` 唯一 ir.rule 的 `perm_unlink=False` 是「不參與 unlink 檢查」非「禁止 unlink」，ACL 又對 `base.group_user` 全開 ⇒ **guest 可 `unlink` project**，比 #705 R2/R3 守衛擋的更徹底，限同 workspace。2 條修法未拍板，牽動既有孤兒 project 刪不刪得掉。🔴 今天就能被利用、只是修法界線未定，別當不急 |
| **#704** 🆕 | **要決 `use_agent_runtime` 該用哪種擋法**——欄位沒有 `groups=`，可經 `call_kw` 直接 `write` 繞過 controller 閘（ORM 沒擋），與 #519／#556／#658／#676 同形態。#698 修完後跨租戶腿已斷，剩租戶自行開啟 agent runtime（吃資源）的權益／計費問題。3 個選項未拍板（含要不要建整類通用 gate）。🔴 今天可被利用、只是修法未定 |
| **#703** 🆕 | **要決 operator 該不該自己也驗歸屬**——`POST /{hash}/sync-workspaces` 對 `project_workspace_ids` 零歸屬驗證就拿去渲染 NetworkPolicy／reach label。Odoo 是唯一 ground truth 而 #698 已證實 Odoo 端可被騙 ⇒ 要不要縱深防禦；3 條路徑未拍板，內文自問是否只是把信任關係換個地方寫。⚠ 動 operator 合併前必須本機跑真 K8s integration tests |
| **#696** 🆕 | **要決 backend view 的 `groups` 該不該比照 security_access**——cloud_service 的 action／menu 與父選單皆無 `groups`、security_access 有 ⇒ 同 repo 不一致。PR #695 已把寫入收窄到 owner/admin，guest/user 按鈕仍可見、只是拿 `AccessError`（UX／縱深、非現行洞）。3 個選項未拍板（含要不要固化 gate）。⚠ 單裡的檔案清單是粗篩、查證後才能定案 |
| **#721** 🆕 | **要決要不要開放租戶自帶 Odoo addon**（承 #713／ci#100；唯讀 spike）。租戶已有 `base.group_system` 且 `safe_eval` 的 `env` 是本尊 ⇒ 資料層今天已等同 superuser，真正增量在 OS／程序層。⚠ 7 項未查證前提，🔴 最關鍵：`safe_eval` 能否逃逸到 `subprocess`／`socket`，可行則判定翻掉。2026-05-03 會議「Skill 不開放上傳」決議外推到 addon 是待決非已決 |
| **#799** 🆕（2026-08-21 補列；08-21 改判 `[security]`，刻意不併 #794） | **要決：要不要把每一趟 operator 推送拆成自己的回捲單位**（照 `post-commit-bookkeeping-rollback-unit`）——等於放棄 policy `create()`／`write()`／`unlink()` 現行「raise ⇒ 整批回捲、不留 ghost row」的產品可見保證、改成 partial success；原文「要先拍板、範圍遠超一張單」⇒ 第 1 層歸 ③。缺口＝三個 override 用一顆 savepoint 包 N 次不可回捲的 CF 變更，第 k 趟失敗／replay 即 CF 已變、DB 回捲 ⇒ 先前趟次新建 policy 成無記錄孤兒、三種 decision 各有一側 fail-open。develop 既有形狀；與 #794 同根。⚠ 兩可：若視為第 2 層實作選型則進 ②A1。🔴 交叉指標已補 A1。 |

## 範圍未明 / 卡別人

| Issue | 卡在哪 |
| --- | --- |
| **#412** | open-design 體驗殘餘項（zsh 進 image／claude 更新策略／刪除語意明示／orphan home PVC 救援 SOP） |
| **#267** | Hermes MCP cloud service 對外暴露 Gateway（一 tunnel 多 subdomain） |
| **#453** | Vibe Kanban host 加掛 OpenChamber（承 #424③，等 Elmo 定位） |
| **sh#9** | HA integration 新增 MCP server 子網域 URL 與 proxy token sensor（標題寫 blocked-by 主 repo #270）。⚠ 複查：#270 早已關閉 ⇒ 前提不成立，現在卡的是「要不要做／誰做」 |

---

# ①b 固化自動驗證（21 筆）— 暫緩，不投 token

> **政策：先問 Eugene 要不要做，並附成效評估。** 這些是可程式化、跑一次就永遠替你重查的守線，**價值在防退化，不在推進上線**；整段冷凍是因 08-01～08-05 的 token 大量流進這裡而 production 沒前進。**解凍時機：實作類的 A0/A1/A2 清完，或某條真的擋住上線時單張破例。**

**成本刻度**：S＝一個 session 內／M＝一天／L＝跨多天。

## 甲 · 「測試到底有沒有在跑」的完備性（1 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#467** | 2 個測試檔未被 `tests/__init__.py` 匯入 → 15 條從未執行（`priority:high`）。🔴 現況：那兩檔已補進 `__init__.py` 且 class 是 `TransactionCase` ⇒ 第 1 步已落地，只剩第 2 步「機械守線」（防新增測試檔漏 import 照樣全綠）；開工前先重查，別重做修法。 | **S**（僅剩守線） |

## 乙 · 測試品質／守線形狀（10 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#828** 🆕（08-22；#830 衍生） | repo-lint test-collectability 換 root 即靜默全綠——`_addon_source_tree_present` fail-open 排在所有存在性反向對照之前 | — |
| **#844** 🆕（08-22 leader1 開；#829 判別式落位裁乙產物、**可用原碼全文已附 body**） | #829 判別式（helm trunc 機制、5/5 預測命中那套）無耐久載體——落地形狀＝golden fixture、待定項與 🔴「golden 只守腳本側、chart fullname template 改動不會被發現」限制皆已明文；Refs #829 #563 | M |
| **#868** 🆕（08-23 leader1 開；#850 姊妹三腿） | repo-lint 三支判準「宣告要鏡像執行期、實際只掃頂層」（tree_facts 只收 module 頂層 ClassDef／inherited_flag／test_tags 只掃 class body 頂層）——腿 1 比 #850 更徹底（class 連 fact 都不建、到不了 #836 的出口）；五格家族表＋修任一不連帶；現樹 0 苦主（254 檔 parse 失敗 0＝0 有效；範圍限巢狀 statement）；⚠ 執行期側未做 #850 等級的 exec 驗證＝**同族不等於同強度**（已明寫）；Refs #850 #836 #862 #684 #356 |
| **#809** 🆕 | `test_content_type_is_json` 斷言無鑑別力——量的是 client session 預設 header，不是這次請求的性質 | — |
| **#572** | 直呼被測碼寫的「無殘留／有回捲」測試會失去鑑別力（回捲的是測試框架 ⇒ 斷言有修沒修都綠；#520 家族大量產出此型），19 條候選已分類（D 組 2 條確認有風險）。⚠ 歸類兩可：核心產出偏實作（修 D 組兩條），掃描 recipe 固化成 repo-lint 規則才落在本段——見文末裁決表。 | **M** |
| **#496** | HAOS 兩條 quota 守線用**含 floor 值**當斷言依據，漂移時靜默假綠（同 #495 finding 1 同型；#456 就是這樣溜到 stg）；現值遠高於 floor ⇒ 不是缺陷、是結構性風險。修法形制已在 #495 落地可直接抄（`_marginal_gi`／`_marginal_cpu`）。 | **S** |
| **#438** | repo-lint A 軌——AST 規則禁止 `assertEqual(len(<seed corpus>), <int ≥ 5>)`；與已落地 B 軌 `check_seed_topology` 互補（B 擋改 seed 的人、A 擋寫新測試的人），#433 是第 3 次撞同一根因。⚠ 必須三段 AND，只看 AST 形狀誤報率約 90%；原型不在 repo 內、實作等於重寫。 | **M** |
| **#776** 🆕（2026-08-21 補列） | pattern-guard hook 對 worktree 路徑恆靜默——用 `relpath(file_path, project_dir)` 比對全錨在 `^src/` 的 map regex，編輯 `.claude/worktrees/<x>/src/...` 必不匹配 ⇒ 靜默放行、與編輯非守護路徑輸出相同；而 teammate 一律用 worktree 施工 ⇒ CLAUDE.md「Known patterns」的機器強制層在團隊施工形態恆靜默、零訊號。修法三選項（剝前綴／git 解 repo-relative／suffix 匹配）未代決；驗收先注入紅燈。⚠ 兩可：②A3（修既有守線的 bug）。Refs PR #775；#547／#640 非重複 | 未估 |
| **#804** 🆕（2026-08-21 補列） | `TestAssembleDeployValues411`／`TestGatewayCapabilityPayload530` 斷言硬編 `woowtech.io`，而 k8s-dev-sandbox seed 會把 `paas_domain` 設成含 slug 的值 ⇒ 沙盒跑全模組必紅、已誤導過一輪（#802 回歸：同一份碼兩種結果，差異來源是環境不是程式碼）；負向對照在 base 碼同樣紅 ⇒ 與 #794／#802 無關。與 #131 同家族不同實例。修法三向未拍板（從 config 現算／setUp 自 seed 值／放寬斷言），另問要不要機械守線擋「測試硬編會被 sandbox seed 覆蓋的 config 值」整類。⚠ 與 #496 同型兩可（②）。Refs #131 | 未估 |
| **#777** 🆕（2026-08-21 補列；承 #716 附錄 G1f，#716 由 PR #775 關單） | `cloud_service_binding_guard.create` 的 key gate 走 `_add_missing_default_values`，`default_get` 三個來源只有前兩個（`default_<field>` context／`ir.default`）有測試，③ 欄位自身 `default=` 與 ④ `precompute=True` stored compute 兩條 leg **零測試**；且 #716 複驗時 G1f 那格探針（`patch.object(default_get)`）沒命中 RPC 走的 registry class ⇒ 綠燈無鑑別力。08-19 實查五個相關欄位今天都無 `default=`／`precompute` ⇒ **今天不可利用，是 test-gap 不是 security bug**（故不進 A1）。要做＝修探針鑑別力＋補 ③／④ 兩條 leg 的測試 | 未估 |

## 丙 · 跨 repo 的 CI 缺口（1 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **cd#76** | operator chart 的 prod/stg 版本漂移無任何檢查（chart 原始碼在另一 repo ⇒ 發版盤點看不到；只 promote image 不升 chart ⇒ 租戶付款後開通必然失敗且 CD/CI 側零訊號）。規則只是比兩個 `fleet.yaml` 同一欄位，同型範本已有 `manifest-version-gate.yml`。⚠ 歸屬 `woow-paas-cd` agent、本 repo 只出規格；只要 warning 不 block。 | **S**（跨 repo） |

## 丁 · memory 稽核工具（3 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#595** | `memory-integrity.py` 的 silent-truncation 有 45/124 檔結構性永久盲區，且與「還沒有快照」輸出不可區分。3b（輸出把兩種未涵蓋分開列名、不改判準）比 3a 重要、是本段成本最低的一張；與 #683 重疊，先做 3b 讓基線可讀。 | **S**（3b）／**M**（3a＋3c） |
| **#683** ⚠ 原一句話已過期 | ①②已部分落地（② baseline 已上線 PR #685、誤報率未判定；① PR #690 hook 刻意只掛 `--links` ⇒ truncation 觸發點仍缺），③ 高誤報率會讓工具自我瓦解仍在；⚠ Gitea 留言只記 #690 沒記 #685。`Refs #595`——先做 #595 3b，再決定 #683 要不要清帳／降級。 | **M**（剩餘部分） |
| **#700** 🆕（#693 落地後暴露） | 同一支工具的 markdown link 抽取不剝 code 區塊 ⇒ 正文 code 範例裡的 `](x.md)` 被判成斷鏈 ⇒ hook block ⇒ 與 #693 同形態的卡死。目前暴露面 0，但那是「唯一實例剛被改掉」的 0。 | **S** |

## 戊 · 一次性實測，不產生持久 gate（4 張，⚠ 全部待裁決）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#390** | CS shared cloudflared hostname 能否被 CF Access gate——stg 挑一顆實測（`priority:low`） | **S** |
| **#387** | SA 層 MCP 協定整合 e2e（tool 呼叫／token 流通），既有 E2E 只蓋網路層（`priority:low`） | **M** |
| **#386** | #315 衍生防線三件：operator SA `can-i` preflight／chart-side RBAC test／`KubectlException.stderr` 進 log（`priority:low`）。唯一可重複驗證的是第 2 件（以 operator SA 身份驗各 verb 才防得住 RBAC drift），另兩件是實作——建議拆單。 | **M** |
| **#393** | 三 MCP（＋#375 新增三型）規格實跑效能觀察——#268 調規格時只驗 healthz+GUI 200（`priority:low`） | **S**（但要等真實負載） |

- 四張不是固化 gate、跑完就沒了，省 token 機制≈無 ⇒ 放在本段名不副實（見裁決表）；唯一例外是 #386 第 2 件。

## 己 · 組裝對帳補列（2 張，⚠ 主筆稿分群表漏列）

> **組裝者註**：主筆稿對帳合計與裁決表要求含 #44／#46 但分群表未列，按舊看板一句話補列；成效評估未做、成本未估。

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#44** | sh_never_expire fixture variant（舊看板一句話照抄） | 未評估 |
| **#46** | test nits——純 cosmetic，不產生任何新驗證能力、省 token 機制≈無（裁決表建議改列 ② 實作瑣事） | 未評估 |

## 建議優先順序（若 Eugene 只肯投 2–3 張）

- 判準：**existing_infra 已有半套的優先**——成本落在「接上去」不在「發明」。
- **第一順位：cd#76** — 全段成本最低而後果最重；⚠ 執行歸屬 `woow-paas-cd` agent，本 repo 只出規格。
- **第二順位：#496**（直接複製 #495 寫法）**或 #595 的 3b**（只改輸出、一個 session 內可完）。
- 刻意不入前排：**#438** 原型不在 repo 內、實作等於重寫。

---

# ①a docs 文件知識類（10 筆）— 暫緩，不投 token

> **政策：先問 Eugene，別自行開工**（Eugene 2026-08-12 裁定）。價值在讓下一個人少踩一次，不在推進上線。**解凍時機：A0/A1/A2 清完，或某條真的擋住上線時單張破例。**

| Issue | 一句話 | 為什麼會過期／為什麼還沒寫 |
| --- | --- | --- |
| **#582** | 四條測試方法陷阱——待 promote 進 `docs/tracking/pitfalls/` | 開單當下的阻擋理由（發版 tag 未打）已消失 ⇒ **純粹沒人去搬** |
| **#508** | PR#507 合併後的註解漂移（3 處 docstring/TODO 與實際行為互打）＋2 條維運觀察（OBS-1 上限 11 封不是 7／OBS-2 `mail.mail` 無清理 cron） | 註解與同檔測試**直接矛盾**。⚠ 歸類兩可：另一歸屬 ② 實作（部分）——OBS-2 清理 cron 定案動手就是 ② |
| **#392** | 手動禮品卡換點 SOP 文件化——07-10 定價會 Q6 拍板「試用＝手動禮品卡換點、不改系統」，但審核／操作／記帳／邊界只在拍板記錄 | 有範本可抄（`docs/guides/smart-home-half-year-gift-points-admin-sop.md`）。⚠ **營運會用到**，真有人要發卡時立刻變阻擋項 |
| **charts#43** | `charts/open-design/Chart.yaml` 的 `description` 仍寫 C 案前 auth 語意（`OD_API_TOKEN` 非租戶 auth token） | **僅 metadata 字串 stale**、不影響部署行為；issue 自陳「下次 chart bump 順手改、不擋主線」 |
| **#421** | 2026-07-23 進度同步會議（KubeVirt×HAOS／Headscale VPN）待辦分類與佐證分析 | ⚠ **混合單**；歸類兩可：另一歸屬 ⊘ 非 paas 管轄（部分）——kubelet max-pods 調升屬**叢集級**，執行交叉 `woow-machines-operator` |
| **#699** 🆕 | 兩處 docstring 宣稱過期、**修法已定、不改行為**：① `cloud_service.py`「`extra_vals` 不提權、ir.rule 照常生效」在 `_model_init_n8n` 的 superuser 路徑上是假的；② `test_workspace_authz_658.py` X13「guest 直接寫 cloud_service 的洞尚未補」已由 #676／PR #695 補上 | 承 #676／PR #695 覆核。修法要寫成「**指得出證據的範圍限定敘述**」，指不出來就刪、**不要改寫成新猜測**（#675 即實錄） |
| **#701** 🆕 | `tea` 寫入坑知識**雙載體且 upstream 在錯邊**：memory 那份持續在長，`docs/guides/development/tea-cli-write-gotchas.md` 凍在 08-05 ⇒ **docs 版是嚴格子集**、無示警 | `docs/` 自 memory 遷移篇目中仍留全文副本的**只有這 1 篇** |
| **#741** 🆕 | `docs/decisions/patterns/paas-workspace-member-acl-tightening.md` 逐字貼著 **#676 已退役**的 `cloud_service_rule_workspace_member` 當「如何收緊」範例 | **不是 #547 重複**。🔴 該 doc 被 **pattern-guard** 列為 `acl-security` 必讀 ⇒ 改 ACL 的人被導到已知有洞的可照抄範本 |
| **cd#112** 🆕（2026-08-21 補列） | `docs/promote-acceptance-checklist.md` §3 把「含 `Running migration`」寫進預期輸出——但「有版號落差」與「有 migration」是兩件事（v147→v149 區間 0 支 migration、gate 仍實跑、log 無 migration 行 ⇒ 字面不符靠人放行；反向會把「沒 migration 行」讀成「gate 沒跑」去重跑／回滾）。正解＝判準改成「migration 行數與發版單一致、零支就是零行且要主動確認」，並靠另外兩個錨點區分「0 行」與「gate 沒跑」；順修「三種 vs 4 列」漂移。Refs cd#110／cd#109／cd#77 | PR cd#110 掃到但刻意不修（#110 只改指令形狀，這是判準語意）⇒ 獨立成單 |
| **cd#113** 🆕（2026-08-21 補列） | `docs/sop-cd.md` §8 坑 3 的 prod／stg tunnelId 標「未經本輪查證」——標記不會過期、不提醒人去查，最容易被整段當已驗證引用；而 tunnelId 在 `paas-cloudflare-system`／`paas-stg-cloudflare-system`（白名單內）唯讀查一下就有。要做：唯讀查出、收斂那句並寫明查法與日期、決定 id 寫全或遮；不一致則當設定議題另開單。⚠ 只做唯讀查證＋文件更新，不改 cloudflare values／manifest。Refs cd#110 | PR cd#110 刻意不修（收掉標記得先實查＝新事實斷言需自己的證據段）⇒ 獨立成單 |

## ⚠ 歸類待 Eugene 裁決（10 張）

跨 ①a／①b／② 三段；主筆判斷不了、需你一句話定案的清單。

> ✅ 2026-08-18 ②／③ 存量追溯已跑一輪；**兩可 4 張仍待裁決**：#567／#750／#383／#412。

| Issue | 目前放在 | 另一個合理歸屬 | 兩可的理由 |
| --- | --- | --- | --- |
| **#572** | ①b | ② 實作（或拆兩半） | 核心產出是**分析＋修 D 組兩條測試**（實作）；只有 AST 掃描 recipe 固化成 repo-lint 規則那半才是 ①b ⇒ 可拆 |
| **#496** | ①b | ② 實作 | 修正既有測試的斷言依據（兩行），不是新建機制；沿用舊看板「測試品質守線」脈絡才放這裡 |
| **#46** | ①b | ② 實作瑣事 | 純 cosmetic，**不產生任何新驗證能力** |
| **#390** | ①b | ② 實作 | 一次性 stg 實測（CF Access app+policy／route／teardown 對稱），跑完不留 gate |
| **#387** | ①b | ② 實作 | 一次性 stg 協定層 e2e；#316 可能是前置 ⇒ 現在排也可能卡住 |
| **#386** | ①b | ② 實作＋①b（**建議拆單**） | 三件混合：preflight 與 stderr 進 log 是實作，chart-side RBAC test 才是可重複驗證 |
| **#393** | ①b | ② 實作／觀察排程 | 一次性效能觀察，**要等真實負載期**才有意義 ⇒ 現在排等於空轉 |
| **#706** | ② A1 | ③ 範圍未明 | 本單**只做唯讀盤點** ⇒ 歸 ②；但「盤完要不要清、怎麼清、要不要 migration（牽動 #691）」全未拍板。若盤點執行方式也算未定，該改列 ③ |
| **#702** | ② A4 | ② A5 | 建的是**預防性偵測工具**，不是修現有壞掉的東西；歸 A4 是因它要抓的症狀落在資料一致性（主筆自陳是**推論不是內文自陳**） |
| **#714** | ② A3 | ①b 固化自動驗證 | 產出是 **production 部署環境的告警機制**（故歸 A3），但「價值在防退化」與 ①b 一致、只差載體是 K8s 監控 ⇒ **要不要把 ①b 定義擴到監控**，是分類原則本身的問題 |

> ⚠ ①a 段 **#508**／**#421** 亦歸類兩可（另一歸屬 ② 實作／⊘ 非 paas 管轄，皆部分），理由已併入 ①a 各自列。

---

# ⊘ 非 paas 管轄（7 筆）— 內文自陳執行歸屬不在本團隊

> 收兩種：(a) 內文自陳「執行歸屬：woow-machines-operator」（前 6 張）；(b) 執行歸屬在別的 addon repo（**#725** 屬 `sh_subscription`）。共同點＝平台側改不動、只能繞過它防守——留在本 repo 當協調載體，**排工作時直接跳過**。#421（①a）／#584（③）為混合單，平台側有一塊，不移出。

| Issue | 內容 | 查證 |
| --- | --- | --- |
| **#512** | 5 個 prod Odoo 客戶站無備份 CronJob | ns `{tenant}-odoo` 全不在白名單，無 helm release／operator label／Bundle。與 #440 不重疊：另一批租戶 |
| **#441** | kasim-odoo-data volume fsck | 同判準；#440 留言：tenant 為「elmo 以 agent 直接建立，非 PaaS 平台開通」 |
| **#442** | 備份健康監控改驗 dump 非空 | 對象＝18 家客戶站，不含 `paas-system`（見 A0 段缺口） |
| **#440** | 客戶 DB 備份鏈失效事件（14/18 tenant） | 不建議關單：本體已復原，衍生 #441–#444 全還 open |
| **#443** | 租戶 DB 備份異地副本規劃 | 規劃類。⚠ 範圍含平台主 DB ⇒ 真要做時平台側有一塊 |
| **#444** | R-3 NetworkPolicy 新 pod 編程空窗評估 | label `ops` |
| **#725** | `sh_subscription` `/renew-subscription`（`auth=user`）：任何登入者走 `sudo().browse()`＋`sudo().create()`，拿自己那筆當範本無限鑄造新訂閱；沒出事只因鑄出的卡在 `state='draft'`、平台只認 `in_progress`（巧合非設計）；`/cancel-subscription` 同族嫌疑未查 | 執行歸屬 `sh_subscription`（Eugene 2026-08-14 裁決開在本 repo，免線索斷在跨 repo 邊界）。#519 守衛判準即照此缺口設計（含 `state='in_progress'`） |

---

# 疑重複

- 目前無疑重複。ci#30 已關（superseded by ci#66，論證在 ci#30 留言 6853）；08-14 那輪 12 張新單查重結論「無新增」（#721 vs #694/#702 非重複，#721 看的是安全面）。

# 現況（會過期，用前自己量）

- **develop `bcf37935` / `18.0.1.0.175`**（2026-08-23 07:48:54Z 量；同時量到 open PR 0）。量法＝兩條獨立路徑交叉：`tea api …/branches/develop` ＋ `contents/src/__manifest__.py?ref=develop`（base64 解碼）對照 `git ls-remote origin develop`；⛔ 不要用本機工作區或本地 ref 當現況（`ahead=0` 不代表同步、30+ worktree 共用 `.git`）。
- prod 現跑 v151（見頁首）；平台 paas-odoo 無 stg 對應環境。
