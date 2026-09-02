# PaaS 上線推進看板（2026-08-16 清理版）

> **重新推導方式（給推導方式不給死數字，下次自己重算）**：
> `tea api -X GET "/repos/{owner}/{repo}/issues?state=open&type=issues&limit=50&page=N"` 逐頁抓到「該頁不足 50 筆」為止，
> 再與看板表格**第一個 cell** 裡的 `**#N**` 取集合差（只抓第一個 cell，才不會把內文引用的「承 #503」「Refs #66」誤當看板項目）。
> 三個會讓你拿到錯數字的坑：
> 🔴 **`limit` 會被 server 端截到 50**——帶 `limit=100` 也只回 50，不逐頁抓會**靜默漏抓**。
> 🔴 **不帶 `type=issues` 會把 PR 一起算進來**。
> 🔴 **六個 repo 的 owner 不同**——`sh` 掛在 `ha-components`，不是 `woow-paas`；owner 猜錯回 **404**，而 404 跟「這張 issue 不存在／已刪」在輸出上完全分不出來。
> 🔴 **對帳的 live 快照有效期是分鐘級，不是小時級**——寫入前重抓一次，不要用流程開頭那份。
>
> **2026-08-16 清理（leader1）**：移除已關 issue 的看板列與歷史段，共 19 張——main `#32／#503／#519＊／#524／#559／#583／#694／#717＊／#723／#724`、ci `#30／#43／#47／#54／#86／#98／#102／#107／#112`——**每張都在寫入前經 `tea api` 逐張重查 `state=closed` 才刪**；帶 ＊ 的兩張各留一列（#519 留 leader4 明示保留的教訓段、#717 留 prod seed 尾巴）。ci#86 的「首次真實 /approve 驗封緘」待辦已併記到 ci#66 列。**本次只做「刪已關」，未做「補新開」**——已由同日第二輪同步補齊（見下行）。
> **2026-08-16 第二輪同步（leader1）**：① 補列五張 08-16 新單——**#753**→A1（`default_<field>` context 繞過 SA identity anchor 剝除，重開 #522 的洞）、**#761**→A3（deploy.md approval gate 補文近收尾；**08-17 已由 PR #764 關單、列已刪**）、**#758**／**#760**→A4（SPA detail page 沿用舊資料／policy create 雙重 sync）、**#750**→A5（0=unlimited 收尾兩缺口，觸發族群現為空集合）；每張經 agent 讀全文分類，細節在各分段列內。② 移除 **#728**（2026-08-15 由 PR **#742** `Closes` 關單；開單時懸著的 `WOOWPRD_PLUGIN_ROOT` 語意尾巴已在該 PR 拍板＝plugin 根＋fallback，無殘留）。③ 分群修正：**ci#48** 由 A1「agent runtime 沙箱」子群移至 A3——內容是 Gitea branch protection／CI 治理，不符 A1「租戶可實際利用」定義，列文未動。④ 對帳：看板 142 列＝live 142 筆 open（main 121＋ci 5＋cd 8＋charts 4＋ar 3＋sh 1，寫入前重抓）。
> **發版現況（08-18 leader4 更新；前值 v149 完整紀錄下沉至本行末尾括號）**：**v18.0.1.0.151**（tag `1115baf7`）——image `20260818.0400`（digest `f6aebc93…`）經 paas-odoo-ci#122 Eugene 本人 /approve（04:02:55Z）build+push（phase 2 step 級 8 步全 success 非 skipped、APPROVAL_SEAL 校驗過、平台側 skopeo 抽 image 內 manifest 實讀 .151）；**CD pin 已完成**（cd PR#116 Eugene 08-18 07:17:28Z merge，values-prod 三處 pin 同改＋cd 側一手 digest 實證含正負對照與漂移複查；build 核可證據補於 PR 留言 7761）；**叢集端 rollout 已完成並驗收八項全綠（08-18 rollout-v151 實測，證據留言 cd PR#116 留言 7773；leader4 另獨立抽驗 DB=.151/installed、pod digest、留言實存三項）**：Fleet commit=99b94b5c Ready 1/1（未 force sync）、gate 實跑 `-u` 且 **.150 migration pre/post 執行痕跡逐字可見**（結尾複驗行 18.0.1.0.149 -> 18.0.1.0.151）、DB `latest_version=18.0.1.0.151`、#768 schema 落地（⚠ 三張 route 相關表 prod 目前 0 筆＝功能尚未被使用的空集合恆真，已標注非查詢失效）、smoke 全過、中斷窗約 3 分 32 秒。**prod 現跑 v151**，落差基準以此為準。發版含 #771（SH 型 access 加購接線，Closes #768）＋#772（HA woowtech-ha image tag seed）。⚠ 兩個觀察項仍在（v149 起）：pod imageID repository 段回報 `paas-odoo-stg` alias（digest 正確，調查中）；readiness 暖機本輪 3 分 06 秒（+36s vs v149，僅記錄）。<br>（v149 前值紀錄：tag `7cf8c947`、image `20260817.0717`、digest `44b7e924…`＝**本次回滾錨點**（⚠ 本輪含 .150 migration，回滾≠單純改回 pin，回滾配方與警語見 cd PR#116 values-prod 註）；其 build 核可 ci#121、CD pin cd PR#109、rollout 驗收與 #765 seed 落地實證等完整紀錄見 cd PR#109 留言；更早 v147 紀錄見 cd PR#107。）

## Leader 工作原則

- `issue-leader` 負責分配  issue 和改本 note
- Leader 需要改  note 請 `issue-leader` 改，Leader 只能做唯讀。
- Leader 一次只能做一個 issue 或是有同樣相關的 issues ，不能排程無關的工作。都完成後，須要 git pull 並 /clear 後才可以處理新的 issue
- 若取用時需請  `issue-leader` 標記已認領，讓其它 leader 知道有人在處理。完成記得請 `issue-leader` 標記完成。
- `## 🔒 認領中` 章節中記錄正在處理的 leader - issue. 若 issue 也請 leader 在下面的 issue 中標記完成。
- 不要把這個 note 當做工作時的記憶區，這只能放 leader 的領取記錄和各 leader 目共享的 note。若需要工作記錄簿請 leader 自行開 create note 記錄。
-  `issue-leader` **動這份 note 一律用 `maestri note edit`
- 本 note 每隔一段時間會
	- 清除已完成的 issue，所以不應該留下過時記錄。
	- 同步 tea cli 可以讀到的 issue


## 🔒 認領中

> 動工前在此登記＋在該 issue 所在列標 🔒；完成後兩處都自行移除。**無登記＝無人處理。**

| Issue | Leader | 起始 | 狀態 |
| --- | --- | --- | --- |
| 📕 **#783 已關** | leader1 | 2026-08-21 | ✅ **完成並驗收**：PR **#800** 由 Eugene merge（2026-08-21T02:49:57Z，merge commit `c74eeee4`，develop **`.167`**；#783 同秒經 `Closes` 自動關）。修法＝**選項 2：拿掉 context 通道**（動**讀取端** `cf_access_policy_reap._pending_for`，不是注入端——注入端只包一層擋不住那堆 sync 觸發點）＋ repo-lint 第 28 條新增 `status="removed"` 防它被加回來（含 meta-test，其中「執行期算出的 key」是**明示保留的盲點**、鎖住現況並註明變紅時要更新清單）。<br>**設計理由／六格 merge 驗收／逐檔取哪一側見 PR #800 留言 8183；operator 端帳號級跨租戶坐實與 #787 B 段更正見 #783 留言 8008／8016／8025**——此處刻意不重述。<br>⚠ **產物已驗，不是只看 merge 成功**：develop 上 `DOOMED_CTX_KEY` 常數不存在、`_pending_for` 不讀 context、`status="removed"` 在（正負向探針各半）。⚠ 但 **merge 後沒有任何人在 develop 上跑過 Odoo 測試**（#546 無 CI），綠燈全部來自 teammate 本機沙盒（已銷毀）。<br>⚠ 殘留：遠端分支 `fix/783-doomed-ids-sentinel` **未刪**，等 Eugene 對兩條已併入分支的統一裁決。merge 驗證腳本（8176 未涵蓋的三格）保全在 #800 留言。<br>🔑 **本輪教訓，別的 leader 會再撞**：① **trees API 必須翻頁並驗 `total_count`**——`per_page=1000` 單頁只回 884 blobs／`truncated=True`，翻頁後 1519，**不翻頁漏 42% 而差異集合看起來完全正常**；② **判準是「有沒有要 leader 裁決的取捨」，不是「衝突集合有沒有變大」**——用「auto-merge 乾淨」這個**現象**當判準，遇到「文字乾淨但語意要選邊」的檔會給出錯答案；③ **查名字不查性質**：merge 後字面 grep key 名仍回 True（兩處都是註解／docstring），AST 掃 `ast.keyword` 命中 0 才是程式面乾淨的證據。|
| **#786** ✅ 已關 | leader4 | 2026-08-21 | ✅ **已關**：PR **#796** 由 Eugene merge（2026-08-21 01:17:33Z，merge commit **5c536a83**，develop **.162**；issue #786 同一秒被 `Closes` 自動關）。leader4 收尾驗證：① 內容真的落地不是只有版號——`git diff --stat 826f5f6d 5c536a83` 非空（4 檔 +1020/-10，含新測試檔 `test_access_policy_reanchor_786.py` 804 行）；② repo-lint 在 5c536a83 全綠（29 條全列，含 #792 新增的 access-gate-no-ledger-read），並先注入重複 method 取得**陽性對照**（紅 1 條、指位正確）再還原（`git status --porcelain` 空）⇒ 綠燈有鑑別力不是探針壞掉；③ 覆核 ccou 已 dismiss、驗證 worktree 已移除。（原始範圍敘述保留於下）範圍：AccessPolicy.write() 換錨點時 _access_sync_targets() 在 super().write() **之後**求值 ⇒ 舊錨點永不重推、CF 側殘留無人回收（後果同 #528）。動 security_access_access_policy.py。版號 **.162**（off develop 2258630e/.160；push 前重驗 manifest diff 非空且嚴格大於，不信預留） |
| **#785** 🔒 | leader4 | 2026-08-20 | 🟢 **已認領、序列化約束已解除、現在可派**（2026-08-21 03:49Z：擋它的兩張都已進 develop——leader2 的 sh-787-fix 於 08-21 前併入、PR #802 於 03:49:42Z merge（`0eb11503`）⇒ **無任何序列化約束**；開工時版號取當時 `max(develop, 所有 open PR head)+1`，**開工當下自己重量**——08-21 04:05Z 後量得 develop tip `4f77a151`／`.168`、open PR 0（#807 docs-only merge 後 tip 已不是 `0eb11503`，版號仍 `.168`））。原暫緩理由與重疊分析：（2026-08-21 重疊面已由 #794 實作者逐 hunk 對回 base 側 method 名核過）：**(a) `_capture_access_teardown()` 本體＝零重疊**（#802 一個字沒動，只以未變更的 context 行出現）；**(b) `smart_home.py` 消費端的 continue／failed 計數＝直接重疊**——#802 改了 `_teardown_routes_access()` 的**簽章**（加 `torn_down=None`）並在成功分支插了 3 行，#785 要動的帳就在同一個 method、同一個迴圈；另 `smart_home_route.unlink()` 裡的 `for info in teardown: if not info: continue` 也已被 #802 從 list comprehension 改寫成 for 迴圈。⇒ 衝突是**會直接 conflict 的顯性型**（簽章變了）不是靜默型，但仍是白工。⚠ 只做 (a) 不做 (b) 在功能上等於沒交付（區分兩種 None 只有在消費端會據以分流時才有意義）⇒ **不拆半張**，整張等 merge。ccou 已招待命。<br>🔴 **優先序已下修**（issue-leader 2026-08-20 旁證）：#739＋#769 若都成立，代表租戶側 SH 生命週期從未真的跑通過 ⇒ 本張缺陷的真實曝險可能一直是零；#794 是真租戶在用的 SA 腿且為無聲 fail-open，先做它。⚠ ~~另有 leader3 的 #739＋#769 也碰 smart_home.py ⇒ 排程算三方~~ **同日更正（issue-leader 轉 leader3 切法）**：leader3 的 PR A（#739）只動 `src/controllers/smart_home.py` 約 857-860、PR B（#769）只動 operator 側 `extra/paas-operator/src/api/tunnels.py`，**Odoo models 零改動** ⇒ 本張排程**只需算 sh-787-fix 一方**。（更正留痕不刪：我先前寫「算三方」是照未精確的第一版轉述寫的。）<br>🔴 **旁證，排優先序時看**：#739＋#769 若都成立，代表**租戶側 Smart Home 整條生命週期從來沒真的跑通過** ⇒ 本張的 SH teardown 缺陷在真實世界的曝險可能一直是零，優先序可下修（但不影響修法正確性）。ccou 已招待命。範圍：SH _capture_access_teardown 捕獲失敗回 None 且完全無 log、消費端 continue 且不計入 failed ⇒ 呼叫端 fail-loud 判成功；兩種 None〔沒東西要拆 vs 有東西但推不動〕語意混同。版號＝開 PR 當時 develop+1 |
| **#794** ✅ 已關 | leader4 | 2026-08-21 | ✅ **已關**：PR **#802** 由 Eugene merge（2026-08-21 **03:49:42Z**，merge commit **`0eb11503`**，develop **`.168`**；issue #794 同秒被 `Closes` 關）。**merge 後零 open PR，merge train 結束**。🔴 **leader4 的 tree 等價驗證**：`tree(develop 0eb11503)` 與 `tree(PR head 1b1fad4d)` **逐字相同**（皆 `e7d0ccce4b8c676f014dcdb6c2997ad2a8a837d1`），develop tip 的 parents ＝ `c74eeee4`＋`1b1fad4d` 的標準 merge ⇒ **在 1b1fad4d 上做的分析等價於在 develop 上做**，語意格覆核不必重跑（省一整輪重工；判準是 tree sha 不是 commit sha）。語意格結論與沙盒實跑測試結果另行留言進 #794。原覆核中紀錄：（2026-08-21 03:27Z：merge train 最後一節由 Eugene 指派 ccsx 解衝突並 push，新 head **`1b1fad4d`**＝merge commit，parents `8737436d`＋develop `c74eeee4`、merge-base `826f5f6d`（leader4 用本地 git 獨立驗過三者），Gitea mergeable=true、CI 兩項 success；issue-leader 已用本地 git 做**第二次**獨立驗算（11 路徑==PR_SIDE、BOTH 6 檔、判準 B 雙向空、行級 multiset 含空行 missing 0 extra 0、`DOOMED_CTX_KEY`／`sa_unlinked_cf_policy_ids` 在 src 排除 tests 後零實碼命中＝leader4 事前標的地雷格取到 develop 側、#783 未回退），逐檔說明在 PR 留言 **8197**。🔴 **leader4 接手兩格**：① **語意格**——#796 的 `_access_sync_targets()` 求值時機 × #800 的 doomed-ids sentinel × #794 的 teardown 落帳／`torn_down` 累加器，三者在同一條刪除路徑上的交集（ccsx 與 issue-leader 都沒驗、文字無衝突）；最可疑的是**換錨點路徑上 `torn_down` 記到的是舊錨點還是新錨點**（記錯＝補償打錯對象且靜默）。② 🔴 **Odoo 測試全案從未有人跑過**——`.gitea/workflows` 裡沒有任何 job 跑 Odoo test-tags（#546）⇒ 「CI 兩項 success」綠的是 manifest gate 與 repo-lint，**不是測試**；leader4 額外派 teammate 在 docker-desktop 沙盒實跑，要求能分辨「全過／有紅／根本沒被 collect」三種結果。）原交付紀錄：head `8737436d`／11 檔 +2730-261／CI success；**經三輪回修＋三輪獨立覆核，最終 PASS**）。🔴 **`mergeable` 與 `.168` 兩個值已於 2026-08-21 01:17Z 起失效，別再照抄**：#796 併入後四張 open PR（#797／#801／#800／#802）**全部 mergeable=false**——撞點是 `src/__manifest__.py` 的 `"version"` 那一行（兩側同行皆改＝邏輯必然，不是異常）＋ `src/tests/__init__.py` 的 add/add；leader4 獨立佐證＝`git diff --stat 826f5f6d 5c536a83` 顯示 #796 本身就動了這兩個檔。⛔ **衝突要等 merge train 輪到前一刻才解**（順序 #797→#801→#800→#802，每 merge 一張 `src/tests/__init__.py` 就重生同一個衝突，提早解＝白做）；解時版號**重取** `max(develop, 當時所有 open PR head)+1`，`.168` 只是當時的值。`src/tests/__init__.py` 驗收只認 **AST 聯集**，且**判準用 B 不用 A**（2026-08-21 issue-leader 全隊更正）：**B** ＝ `{(n.module, a.name) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 1 for a in n.names}`；**A**（多一個 `module is None`）只涵蓋 `from . import x`，**涵蓋不到 `from .integration import test_ecpay_invoice_real`** ⇒ 同一份 develop 檔 A=236／B=237，掉的若正是那一行，**A 不會紅、症狀是整檔不被 collect**。判準：resolved 集合 == develop 集合 ∪ PR head 集合、兩方向差集皆空。🔴 **報條數一律連判準定義（A 或 B）一起報**——計數本身無鑑別力（曾實算 develop 236＝PR 236 而正解是 237）。#797 的 merge commit `991743ea` 已由 issue-leader 用 B 重驗（develop 237／舊 head 237／聯集 238／resolved 238、雙向差集空、integration 那行在）。🔴 **覆核抓到兩個 HIGH，都是「補償安靜地什麼都沒做」**：① **第五腿** `SecurityAccess.unlink()`——PR 原本的排除理由「`_cleanup_cloudflare` 不 raise」**驗錯了對象**（真正造成回捲的 raise 在**呼叫端**、且在 partial success 之後），而且它比原涵蓋的腿**更直接可達**（單筆刪 SA 即可）；修法用兩層 savepoint，關鍵洞察是 `unlink()` 自己的累加器在那格**是空的**（pre-call 已清光），真正的受害者只有 `action_delete()`（租戶按刪除走的那條）看得到。② **`torn_down` 空轉**（leader4 的資源側枚舉工作流挖到、leader4 自驗、覆核複驗後擴大到**四腿**）——`_teardown_*` 在 `return True` 前呼叫落帳，而落帳明文 re-raise `OperationalError` ⇒ append 被跳過 ⇒ 補償拿空清單。**與「補償根本沒被接上」在列狀態上不可區分**；修法＝append 移到 operator 回 2xx 當下、落帳之前（⚠ 刻意不動 #791 的 re-raise 設計、不動回傳值語意，各由 G3 釘住）。🔴 **實作推翻了工單兩個前提**：① 我工單寫的「TestCursor 與外層共用交易＝假綠」**方向相反**——實測 Odoo 18 只有 HttpCase 會 enter_test_mode，TransactionCase 用真 Cursor ⇒ `registry.cursor()` 是**真的第二條連線**、看不到未 commit 的 fixture ⇒ 補償在單測裡**整支安靜地什麼都不做**（已寫進 memory）；② **是四條腿不是三條**——`SmartHome.unlink()` 的 `_teardown_routes_access()` 同型且**後果最重**（raise 的一個可達來源是「tunnel 刪不掉」，而 tunnel 活著正代表 hostname 照常服務、CF 上卻已無 Access app）。修法：獨立 cursor 分兩筆交易（先記事實後 best-effort 重推）、永不 raise、`SET LOCAL lock_timeout=3s`、新旗標 `access_protection_lost`（`is_protected` 一字未動 ⇒ **無產品可見變更**）、整合探針用 `BaseCase` 跑三筆真交易**帶對照值**。⚠ **窗口關閉仍不保證**（平台無週期性 Access re-sync）⇒ 衍生 **#803**（cron 收斂者，open、未認領）。🔴 查證推翻了 issue 設想的修法載體：**同交易內補償寫不可能**——raise 會丟棄整筆交易，`cr.postrollback` 被 `Cursor._close()` 再 rollback 沖掉，`_settle_after_commit` 也只是同交易 savepoint 包裝 ⇒ 唯一載體是自開 cursor 的獨立交易，而 `src/models/` 下 `odoo.registry(` **零命中**＝model 層新形狀。🔴 **只清 id 關不掉安全窗口**（平台無週期性 Access re-sync，只有 provision／resume／寫 Access 欄位三個收斂點）⇒ 補償要含訊號欄位＋best-effort 立即重推。🔴 **是三腿不是兩腿**：新發現 `CloudService.unlink()`（`cloud_service.py:4639-4646`）完全同型，**註解還把缺口逐字寫成設計意圖** ⇒ 做三腿共用 helper。不動 operator（清 app_id 後 payload 走 POST 建新）。⛔ 禁自動降 `is_protected`（產品可見變更，未拍板）。風險：TestCursor 與外層共用同一 PG 交易＝**單測綠證明不了活過真 rollback**、subxid cache 溢位可能單執行緒互鎖成 hang。~~SH 同型形狀改同兩檔，同上序列化；~~⚠ ~~併計 leader3 的 #739＋#769~~ **同日更正**：leader3 兩張是 controller 側＋operator 側、**Odoo models 零改動** ⇒ 本張只需算 sh-787-fix 一方。🔴 旁證：#739＋#769 若都成立代表租戶側 SH 生命週期從未真的跑通過（見 #785 列同段）。[security] **無聲 fail-open**：route 的 CF Access app 被拆後 DB 回捲 ⇒ 列帶 is_protected=True 復活但 CF 已無 app ⇒ UI 顯示已保護、實際免登入即可存取，四條偵測管道全照不到（真 registry 實測 PROBE-1~7 坐實，判別式＝改 service_url 走 DNS/ingress 路徑、sync_access_app 根本不被呼叫）。修法優先 issue 內文第三條路「補償寫」——raise 後在回捲不到的地方清空 access_app_id／cf id，讓下次 sync 重建新 app；**只動 Odoo 側、不動 operator**，避開選項 B 的反轉風險與真 K8s integration test 成本。版號＝開 PR 當時 develop+1 |
| **#739 ＋ #769** ✅ **兩張皆已修復並 merge（2026-08-21）** SH provision／lifecycle | leader3 | 2026-08-20 → 08-21 結案 | ✅ **#739＝PR #797**（merge commit `84afb5d8`，01:54:26Z）／**#769＝PR #801**（merge commit `e356066b`，02:07:53Z）。🔴 **#739 的產品裁決（Eugene，2026-08-21，經 issue-leader 轉達）＝「是 bug，照修」**——fail-closed **不是** intended，sudo 修法成立；裁決全文與「它決定了什麼／沒決定什麼」留在 #739 `issuecomment-8155`（**載體在 issue，不在本看板**，本列刻意不重述）。⚠ 該裁決**只涵蓋 SH，未涵蓋 SA**。衍生 follow-up＝**#805**（SA 的 `action_provision` docstring 自陳 `mirroring smart_home.action_provision exactly`，#797 merge 後成為假的，且**沒有任何 gate 會因此變紅**；含第二份同義載體在 SA 測試 docstring、第三處在 `test_smart_home_authz_524.py` 的新註解——⚠ 第三處**抓不到**於 #805 原文的字串判準，那是已知盲區見 `issuecomment-8180`）。覆核紀錄：#797 → 我對 merge commit 的獨立覆核 PASS；#801 → `issuecomment-8171`（含六格表與口徑）；#801 的**現算式**驗證腳本（不吃常數）逐字存在 #801 `issuecomment-8176`——**#800／#802 可直接取用**。<br>🔴 **交付後追記（2026-08-21，merge train 全部落地後的終態掃描）：#801 帶進去一個缺陷＝#806。**`test_tunnel_config_wire_769.py` 的 `TestConfigureTunnelWireShape` 基底是 **`unittest.TestCase`**，全檔無任何 Odoo TestCase ⇒ `TagsSelector.check()` 因 `test_tags` 不存在而 `return False`，**腿 2 的 4 條從 merge 那刻起一條都沒跑過**。<br>🔴 **這一格不是清單漏了，是清單結構上看不到**：機制 A（未匯入）**不成立**，該檔有被正確匯入 ⇒ 覆核時做的「判準 B 雙向差集空／孤兒 0／幽靈 0／行級聯集 0-0-0」**全部正確且全部通過**。**清單守的是「檔有沒有被 collect」，這個缺陷是「class 有沒有被 select」——兩個不同的層。**⇒ **建議驗收清單補一格**：逐 class 判定基底能否可傳遞地到達 Odoo TestCase。<br>影響有界（勿誇大）：腿 1（pytest）與腿 3（repo-lint gate）都活著，實查腿 3 **確有查 client 側**（斷言 `configure_tunnel` 兩欄位走 `_request(data=...)`＋跨層比對＋絕對錨點）⇒ **契約今天仍被靜態守著**。死掉的是腿 2 唯一不可替代的斷言——驗 `requests` 送出前 `PreparedRequest` 的**實際序列化結果**；腿 3 只看得到原始碼裡 `data=` 的**字面形狀**，看不到 `_request()` 把它變成什麼。<br>⚠ **值得記的形態**：那個檔**自己的 docstring 就在警告 #544／#684 家族**，檔頭還寫著「每一腿都放在『跑得到』的地方，不互相假裝」。針對**一條**進入該家族的路徑（skip）做了完整防禦，然後從**另一條**路徑（基底選錯）掉進去。**寫警告與選基底是兩個沒有交會的動作，警告寫得越詳盡越不會回頭檢查自己。**<br>🔴 **機制 B 的基準因此動了：9 class／3 檔 → 10 class／4 檔**（見「甲」段）⇒ **這個類別不是歷史債，它以每輪新增的速度在累積**，而且 gate／CI／code review 沒有任何一個會紅。**這是 #467／#544／#684 那支 gate 最直接的實據，並同時證明：只做 `__init__` 匯入對帳的 gate 抓不到本案。**<br>develop 終態其餘乾淨（皆附陽性對照）：tip `0eb11503`／`.168`；孤兒 0／幽靈 0（判準 B ＋遞迴逐層 `__init__`）；本輪六張 PR 的 14 個新測試檔「檔在」與「被匯入」**兩格分開驗**皆 True；repo-lint `REPO_LINT_RULES` keys 30 ＝檔頭編號清單 30（`^def check_` 34 是另一個分母）。<br>【原始 scope 紀錄保留於下，供追溯】兩張原文已讀，錨點已自行重新定位。🔴 **本列只記符號名不記行號**——這輪每次 merge 都在動它們，數字請自己在當下 HEAD 上 grep 符號名取得：**#739**＝`models/smart_home.py` 的 `tunnel_token` 欄位帶 `groups='base.group_system'` × `controllers/smart_home.py` 的 `_provision_smarthome` 以**非 sudo** 租戶 recordset 呼叫 `action_provision`；**#769**＝operator `api/tunnels.py` 的 `update_tunnel_config` 三個裸 `str` 參數（FastAPI 判 query param）× Odoo `services/paas_operator.py` 的 `configure_tunnel` 組 `data` dict 送 JSON body。（**為什麼改成這樣**：同一個 `tunnel_token` 欄位區塊在 `2258630e` 是 `:314-317`、在 `826f5f6d` 是 `:318-321`；我 08-20 第一版把它寫成「`.160` 的 `:318-321`」——**當下是錯的、隔一次 merge 又碰巧變對**，這種錯不會以衝突現形，只會被下一個人照抄。）🔴 **撞檔實況（leader3 自行重驗，與 issue-leader 的靜態對照有出入，以此為準）**：leader2 的 `sm_force_access_sync` setter 在 `smart_home.py:917`，而 `:917` **不是「733／917 附近的 hunk」——它就在 `_do_resume`（`:874-922`）的函式體內**，正是 #769 唯二呼叫者之一（另一個 `_do_suspend` `:835`，`configure_tunnel` 呼叫點 `:850`／`:889`）。⇒ **規避法＝#769 修在 operator 側**（裸 str 換成 request body model），`src/models/smart_home.py` **零改動** ⇒ 與 sh-787-fix 及 leader4 的 #785／#794 完全不接觸；#739 只碰 `src/controllers/smart_home.py` ＋ 新增 HttpCase 測試，同樣不碰 `:733`／`:917`。**切法＝兩張 PR**（#739 純 Odoo 可獨走；#769 跨 `extra/paas-operator/`、合併前必跑本機真 K8s integration tests，綁一起會讓 #739 卡在 operator 驗證上）。版號**不預留**——開 PR 當下取 develop+1 並重驗 `git diff origin/develop <tip> -- src/__manifest__.py` 非空且嚴格大於。worktree：branch `fix/739-provision-sudo`／`fix/769-tunnel-config-wire`，皆 sibling 目錄、非 scratchpad。🔴 **路徑刻意不寫死，用 `git worktree list | grep -E '739|769'` 現查**——我 08-20 才把實際路徑寫進來，sh739-fix 同一輪（23:37）就自己把 worktree 搬到另一層，寫死的路徑**十幾分鐘就腐爛，且腐爛時 `git -C <舊路徑>` 回的是 `No such file or directory`，與「worktree 被砍掉、工作沒了」在輸出上完全相同**（本次虛驚一場：reflog 證實 rebase 前無 commit、無遺失）。PR 標 ⛔ 勿代 merge、`Refs #739 #769`。teammate 兩位（ccou `sh739-fix`／`sh769-verify`）。**#769 第一輪 verify-only 已交件＋leader3 獨立覆核 PASS ⇒ 破裂 CONFIRMED**（詳見 A4 的 #769 列，不在此重述）；已裁決修 operator 側並派出第二輪（修＋兩側契約測試＋本機真 K8s integration tests＋RED first）。**#739 已交件＝PR #797**（`⛔ 勿代 merge`、`Refs #739`、base `826f5f6d`、tip `cf4dc400`、`.161→.162`；leader3 逐項獨立覆核，**機械面全 PASS**：6 檔無一碰 `src/models/smart_home.py`、版號 diff 非空且嚴格大於、baseline 內容集合差零移除且唯一新增 code-level sudo 即該顆、既有測試 `9 insertions/0 deletions` 零斷言被動、`tests/__init__.py` 兩種判準皆零遺失、新測試檔是 `HttpCase`／13 條／明文禁用 TransactionCase 驅動受測路徑）。<br>🔴 **但覆核揪出一個 CI 抓不到的設計意圖衝突，已擋在 merge 前、待 Eugene 裁決**：sibling model `security_access.py` 的 `action_provision` docstring 逐字寫著非 system owner 撞 tunnel_token 的 AccessError **"That is the intended fail-closed behaviour"**、**"do NOT 'fix' a portal AccessError here by wrapping the whole method in .sudo()"**、且自陳 **"mirroring smart_home.action_provision exactly"**；而且那條鎖它的測試**真的存在**——`src/tests/test_security_access.py:929 test_provision_token_write_fails_closed_for_non_system_owner`，docstring 逐字「…應 fail-closed（AccessError），**而非被某人改 sudo 後悄悄放行**。**鏡像 smart_home 的非 sudo provision 行為**」。⇒ **#739 的修法正是那條測試被寫來防的動作，只是它掛在 SA、改的是 SH ⇒ 不會紅、CI 全綠、零訊號。**🔴 **這不代表 #739 錯**：docstring 擔心的是擴大 token 的**寫＋讀**面，而本修法只動寫側（讀側 `to_dict()` 不含 token、專屬 sudo 端點仍是唯一讀路徑）⇒ 實際擴大的只是「owner 可讓**平台**把**平台鑄造**的值寫進一個他**仍讀不到**的欄位」。但 ⚠ 交件者「只掛在呼叫上、不是整個 method」的論證**不成立**（sudo 過的 recordset 讓 method body 整段 su，`to_dict()` 本來就不在 method 內），已要求重寫該格。**真正待裁決的是產品面：若 fail-closed 是 intended，`/woow` 那個租戶端點就是設計上永遠不會成功的死端點。**已要求交件者在 PR 補「與既有明文設計意圖的衝突」段（逐字引用兩份原文）、改寫該論證、並把 SA 那張後續票的框架從「同型缺陷」改成「兩邊語意已分歧、需一次裁決對齊」。修法本身按住不動。<br>另交件者自報 (b)：例外被吞成 `success:false`＋DB 停在 `provisioning` 的形狀**在 base 與修法後都在**，本票只拿掉觸發源；不順手修的理由是「讓例外冒出去回捲」是否安全需先裁決（#484 的 dns_failed 路徑自陳不安全），且落點很可能在別的 leader 持有的 `smart_home.py` ⇒ 建議另開設計票。<br>**#769 已交件＝PR #801**（`⛔ 勿代 merge`、`Refs #769`、base `826f5f6d`、head `c4b16a30`、`.165`；leader3 獨立覆核 **PASS**）。落地＝leader3 裁決的 **operator 側修法＋三腿分家**：腿1 行為/operator（`tunnels.py` 三個裸 `str` → `TunnelConfigUpdateRequest` body model，**刻意不做 body/query 雙接受**，依據＝stg 14 天 access log 無第三方用 query 形態）、腿2 行為/平台（client 真實序列化）、腿3 結構/跨層（repo-lint 新 gate `tunnel-config-wire-shape`）。**禁區乾淨**：8 個改動檔，`src/models/smart_home.py` 與 `src/services/paas_operator.py` 皆未碰。覆核實查：版號全表互斥（develop `.161`／#796 `.162`／#797 `.163`／#800 `.164`／#801 `.165`）、repo-lint 檔頭 34 == `^def check_` 實數 34、registry baseline 零刪除只 +2、`tests/__init__.py` 兩判準集合差 lost 皆 EMPTY。gate 形狀值得抄：除了「兩端相等」還加了**絕對錨點**（`operator_side != "body"` 也算違規）——註解自陳「一致地都用 query 也會通過相等那格」，正是本 repo 記過的「兩側一起壞成同一個值 ⇒ 相等反而容易成立」；另有分類器自檢與「目標檔讀不到＝違規不是乾淨」。<br>🔴 **#769 修法帶出一個新缺口，已擋住、待 Eugene 裁決（與 #739 是同一形狀，故合併呈報）**：`security_access.py:1021-1029` 的 `_do_resume` 有 `if restore_state == 'error':` heal 分支（註解自陳失敗模式：*would leave a working service permanently flagged 'error' (cron branch 2 only resumes 'suspended', so it never gets revisited)*），**SH 沒有**（`restore_state == 'error'` 命中 SA 1／SH 0，陽性對照 `restore_state` SA 5／SH 3）。配上 `smart_home.py:872` 的 `pre_suspend_state` 保留 `'error'`、`_is_halted()` 預設只認 `'suspended'` 且 SH 無 override （override 只在 `cloud_service.py:2407`）⇒ **#769 修好後第一次真停權會把 `pre_suspend_state` 記成 `'error'`，日後 resume 把 tunnel 健康的 SH 還原成 `error` 並永久卡住**。今天休眠只因為 suspend 根本跑不完 ⇒ **這個修法會把它自己創造出來的族群種進永久卡住的狀態**。leader3 建議：#801 照原樣 merge、heal 分支另開小 PR 立刻做（SA 那段是已上線已註解的鏡像，五行左右；實查 `smart_home.py` 目前無任何 open PR／worktree 持有，動得了）。<br>另附兩則交件者查證（**只回報未修**）：(A) 訂閱已 expire/close/cancel 那批**會**自我收斂（cron 分支 3/4 的排除清單不含 `'error'`），訂閱仍有效卻卡 `error` 那批**完全不會**（`_is_halted()` 早退，全 repo 無 cron/migration/reconciler 會碰）；(C) `_do_cleanup()` 走的是契約正常的另一個端點、**一直有效** ⇒ 今天一台 cancel 掉的 SH 的真實命運是「服務照常跑、第 6 個月當天 tunnel 連同 DNS 被永久刪除」，修好後才變成正常兩段式——**這次修的不只是補上停權，是把無預警硬刪的懸崖改回有中間態的斜坡**（建議寫進發版說明）。<br>---<br>**2026-08-21 merge train（leader3 實查，非轉述）**：#796 已 merge（develop `5c536a83`／`.162`）。🔴 **四張 open PR 當時全部 `mergeable=false`，不只被點名的那張**——版號互斥 ≠ 可合併：每張都把同一行 `"version"` 從 `.161` 改成不同值，develop 一動就必衝，這是必然不是異常。而且**每 merge 一節，剩下每一張都會在 `src/tests/__init__.py` 同一位置重新長出同一個 add/add 衝突** ⇒ 解掉一張不會讓其他張變乾淨。#797 的衝突由 ccsx 解成 merge commit（parents = 舊 head ＋ develop），**leader3 獨立覆核 PASS**。<br>🔴 **merge commit 覆核的三格（已被 issue-leader 蒸餾成全隊驗收清單；清單本體在別人的 scratchpad，會被清掉，所以判準本身記在這裡）**：① **非衝突檔會被 merge commit 悄悄還原**——衝突檔大家都會驗，但另一側的**修法本體**通常不在衝突清單裡；判準＝全樹 blob sha 逐檔比，`merge vs develop` 的差異路徑集合必須**恰等於**該 PR 自己改的檔（多 0 少 0）。⚠ 錯了會全靜默：repo-lint 綠、manifest 對、import 集合對、`py_compile` 過，而**沒有任何 CI 會跑 Odoo 測試**（見下）。② **manifest 整份取分支側會吃掉 develop 側版號以外的改動**（多一個 `data` 檔、多一條 depends），而所有人只會去看版號那一行 ⇒ 判準＝驗 `develop→merge` 的 manifest diff **只有版號行**。③ **`tests/__init__.py` 要用行級聯集＋遞迴掃**：行數必須 = base +（develop 新增）+（PR 新增），兩側新增行遺失各 0、憑空多出 0（比 import 名集合更強，連註解區塊都涵蓋）；孤兒掃描要**遞迴 `src/tests/**` 逐層 `__init__`**（`integration/` 有自己的），且 AST 判準要用 **B**＝`level==1` 全收（含 `from .integration import ...`），判準 A（多加 `module is None`）會漏掉那一行、掉了也不會紅。**報條數一律連判準 A/B 一起報**。<br>🔴 **查重救回一張重複票，順帶三則實查（2026-08-21，對真 develop）**：(1) **`#467`（open，2026-07-29）就是「測試檔沒被 `__init__` 匯入」這張票本身，而且它的 body 早就逐字寫好了同一支 gate 的規格**（AST 掃 `test_*.py` 對 `__init__` 匯入）並指名兩個可照抄先例（`extra/paas-operator/tests/test_integration_marker_guard.py`、`tests/test_async_harness_guard.py`，**今天都還在**）。它的第 1 項（補匯入）已做、**第 2 項（gate）沒做**。⇒ 要做就做成「#467 的留言＋一支 gate」，**不要開新票**。⚠ 驗第 1 項時「被匯入」與「檔案被刪掉」都會讓孤兒掃描回乾淨——只驗孤兒數會把「把測試刪了」誤判成「修好了」，兩格要分開驗。(2) 🔴 **只做 import 對帳會是假綠**：`#544`（open）是同症狀不同機制（有 import 但整檔只有 `unittest.TestCase` ⇒ 被 tag 過濾排除），**今天損害仍是活的**。🔴 **判準必須是 class 粒度、不是模組粒度**——`TagsSelector` 是逐 class 過濾。逐 class 重算（遞迴掃全部 `test_*.py`）：`test_naming.py` 6 個 class 全排除、`test_reconcile_delete_gate.py` 2 個全排除、🔴 `test_reconciler_diff.py` **4 個 class 裡 2 個會跑、1 個被排除**（混合檔）。⚠ **模組級判準會放行混合檔**，而檔內那些 class 繼續是死的——更糟的是 gate 存在會讓人以為已涵蓋。⚠ **#467 的留言把 `test_reconciler_diff.py` 稱為「關鍵對照組：同樣含 `unittest.TestCase` 卻**有**被執行」——那句話對『模組』為真，照字面拿去寫 gate 就會寫出有盲區的 gate**（本 repo 記過的形態：可照抄的例子勝過正文警告）。leader3 實際就是被它帶偏，建了一個「必須放行」的對照組，而正確答案是「必須部分紅」，**對照組通過反而增加了信心**。這也機械地解釋了 #544 記 47 與 #684 記 51 為何對不上：**兩個粒度**，差的那一格正是混合檔，模組級結構上看不見。**票面寫推導方式不要寫數字**——leader3 自己量到的是與兩張票都不同的第三個值。只收 #467 會讓票面長成「測試收集已有守線」而下一個人不再查第二種機制。<br>⚠ **本 (1)(2) 兩段大半與看板『甲 · 測試到底有沒有在跑』段重複，那段更完整**（四張票、成本分級、已把 #684 列第一順位並列出三個現成支點、以及「#544／#684 重疊需先合單重量」）。**以那段為準**；本段只保留它沒有的三項：③ repo-lint 非 required、class 粒度、47/51 的機械解釋。🔴 附一則方法論教訓：leader3 的查重用「被操作物件的名字」搜，撈到 #467／#544 卻**完全漏掉 #684**——因為那組字對應的是 #467 的機制，而 #684 是**另一個機制**（`unittest.TestCase`／`TagsSelector`／`test_tags`），物件不同、一字不中。**「依物件名查重」只在你已知全部機制時才完備；同症狀有兩種機制時，會漏掉你還不知道的那一種。**(3) 🔴 **repo-lint 不是 required status check，紅了擋不住 merge**——權威來源 `tea api /repos/.../branch_protections`：develop 的 `status_check_contexts` **只有 `manifest-version-gate`**。⇒ 任何「補 gate 就擋住了」的說法都是假宣稱，它是**看得見的紅燈不是閘門**。想讓它變 required 必須**先刪掉 `repo-lint.yml` 的 paths 白名單**（paths 沒命中時 context 不存在，而 required 判準是「存在且成功」⇒ 只動 docs 的 PR 永遠卡死；該檔自己記載了 PR#599／#605 的實證）。好消息：白名單已含 `src/**` 與 `scripts/audit/**` ⇒ 新 check 讀 `src/tests/` 不必動它。背景＝`#546`（open）：**PR 的唯一實質 gate 就是 repo-lint 靜態檢查**，2237 條 Odoo 測試沒有任何 CI 會跑（Phase 1 已實測完、Phase 2 卡觸發策略裁決）。(4) 今天這輪撞號**不必另開票**——`#691`（open）已分析過同一場競賽，且已指出「`max(develop, 所有 open PR head)+1` 只在 push 當下成立、合併前仍要再驗」；建議把新證據補進該票留言（看板會被覆蓋，issue 不會）。<br>**2026-08-20 更新（PR #795 merge 後）**：base 移到 `826f5f6d`/`.161`，兩個 worktree 已通知 rebase。🔴 **更正我自己先前經 issue-leader 轉出的「#739／#769 與 #795 零檔案重疊」——對 #739 是錯的**：實查 `git diff --name-only 2258630e 826f5f6d` ⇒ #795 動了 `src/__manifest__.py` **和 `src/tests/__init__.py`**，兩個都是 #739 也會動的檔。後者正是本 note 開頭「集合差不能用計數」那條警告的標的（#467／#544／#684 家族）⇒ 已把「rebase 後用集合差驗、報任何條數一律連判準定義一起報」寫進 sh739-fix 的工單。**#769 側零重疊經實查成立**（該 diff 的 `extra/paas-operator/` 前綴命中 0），operator 側修法與 sh-787-fix 的接觸面維持為零
| 📕 **#778 已關**（2026-08-20T06:10:35Z 隨 PR #790 merge 關單；本體 PR #782 早前已 closed）| leader1 | 2026-08-20 | ✅ **已收工／leader1 檔期釋出（2026-08-20 標）——本列不再是進行中，保留僅為教訓與移交紀錄**；本輪平台側零殘留待辦，唯一未了結者已移交外部載體（上游 WOOWTECH/sh_subscription#18＋平台追蹤 #725，皆非 leader1 檔期）。<br>完成並驗收（2026-08-20；⚠ 改動已落在 develop 的 `422fb135`（**不是 develop tip，別拿它當現況基準**——08-20 tip 已推進到 `2258630e`/.160）、**待發版才帶出 prod**；移交仍 open＝上游 #18＋平台追蹤載體 #725）：①上游 WOOWTECH/sh_subscription（在 GitHub，用 gh；本 note 開頭「sh 掛在 ha-components」與現況不符）PR #10–#16 全 merge，對平台影響稽核完＝無可刪平台碼、無測試 symbol drift（報告在 leader1 暫存）。②billing SSOT 升級演練實跑通過：舊 DB 升 wallet 18.0.1.3.0 → cron 收斂 active、param 守住 opt-out；紅燈先行三條、「-u wallet 會級聯重跑平台 guard」實測坐實。新發現兩則：🔴 docker-desktop hostPath＝node 啟動當下凍結副本（local 沙盒可能整場測舊碼、三個成功訊號全無鑑別力，已入 leader1 memory gotcha）；🔴 升級中約 2 秒已 commit 的 cron 關閉窗口（升級中斷會停在計費關閉且零錯誤＝#782 靜默告警的真實 use case）。③三項已完成並經 leader1 驗實體＋dismiss（2026-08-20）：**PR #790**（⛔ 勿代 merge、Refs #778、CI 綠；button_upgrade 路線被 Odoo 核心在測試 harness 明文擋下，改 convert_file 重播 billing_ssot_guard.xml，RED→GREEN 證據與覆蓋層級明講在 PR 描述）＋ #778 留言 7920 記錄窗口＋ stale sandbox 已銷毀（docker-desktop 現無任何 dev-sandbox ns）。**PR #790 已由 eugene merge 完成**（2026-08-20T06:10:34Z，merge commit `422fb135`；leader1 08-20 驗 `git merge-base --is-ancestor` 確認確為 `origin/develop` 祖先＝真落地，非擱淺）⇒ #778 平台側全數收尾，僅待發版帶出 prod。附帶：#725 複查完（2026-08-20，留言 7927）＝**仍 open，不建議關單**。上游 PR #15 只擋「同一張重複 renew」，擋不住「cancel 剛鑄的 draft 子單→再 renew」的交替鏈（已 fork 對抗驗證＋leader1 第一手覆核 add700be：cancel_allowed:148 放行 draft、renew cancel∈RENEWABLE:162、去重:160 只認同一張）＝無限鑄幣仍成立、成本 1→2 RPC/顆。leader1 上一輪兩個判斷被推翻：①「#15 部分解掉」錯；②「cancel 按鈕 template 未收斂」殘留查無此物（Close 按鈕:106 內層已有 sh_is_close_by_customer 守衛）。平台防線不變（draft 不被平台認可，變現仍擋）。關單前置＝prod 實裝 sh_subscription 需含 #15（未查），但即便含也不能關。**已在上游開執行單** WOOWTECH/sh_subscription#18（security/bug，含機制行號＋3 修法方向未選型，Refs #15＋Gitea #725；#725 回連留言 7930）。agent 第一手覆核另發現：PR #15 修法 4（到期誤判）被同一步繞過——到期 cron（_sh_auto_expired_subscription）只排除 draft，鏈路把子單洗成 cancel 就被算進 renewed_children，來源訂閱到期誤判 renewed（下游影響未評估，已寫進 #18）|
| 📕 **#788** 已關（2026-08-20 PR #789 merged by eugene，develop .157，gate `internal-ctx-key-registry` 上線）| leader2 | 2026-08-20 | ✅ 完成並驗收：base 追平 develop 後 merge（tip 9e88f2ac→de7f4d04），leader2 獨立覆核雙面 PASS（registry 5 pending＝#787 無一誤升 enforced／自選 probe leader2_probe_zz 驗 (ii)(iii) 真會擋、還原回綠）＋ **merge 後實跑 develop repo-lint --all 全綠**（#781/#528 併入無新未登記閘、全隊未被擋）。設計＝registry in-code 常數（Eugene 未否決）。承接 **#787**（5 把 SH/CS/ECPay 鑰匙 registry pending＋可達性待逐把沙盒 RPC 驗）🔄 leader2 認領中（見「🔒 認領中」表 #787 列）。gate-788 已 dismiss、worktree/分支已清 |
| **#787** 🔒 資安追蹤 | leader2 | 2026-08-20 | 🔄 進行中：context-key 偽造家族普查（承 #536/#788；A 段 5 把 truthy 閘掛 registry pending，逐把驗可達性再修）。**本輪先驗第 4 把 `cs_skip_access_sync`**——已派 ccou「cs-787-reach」（worktree `wt-787-cs`＠origin/develop de7f4d04）。機制：`cloud_service.write()` 內 `not ctx.get('cs_skip_access_sync') and any(k in vals for k in _CS_ACCESS_SYNC_FIELDS)`（觸發欄 is_protected/login_google/login_email_otp/session_duration），**生產碼零 setter、只有 tests 設** ⇒ 租戶若經真 call_kw 塞入即 fail-open（DB 標已保護、operator/CF 從未推送）。驗法：真 web JSON-RPC 以 base.group_user＋陽性對照（觀測 `_sync_access_to_operator()` 跑沒跑、非 write 成不成功），⛔ 禁 TransactionCase/with_user（遮 field groups＝#739/#753 假綠）。**scope（teammate 提、leader2 覆核同意）**：#676 後 write 收窄到 workspace owner/admin（ir_rules.xml:103），但 owner 依 #519/#524 可自我滿足 ⇒ owner-scope 仍＝租戶可偽造；另補跑 role='user' 對照（預期 ir.rule 先擋、與 key 無關）。verify-only（git 只動 probe787/ 8 檔、src/scripts 零改）。**✅ cs_skip_access_sync＝CONFIRMED reachable**（2026-08-20；teammate 真 web JSON-RPC 交件＋leader2 靜態獨立覆核，非重跑其 recipe）：租戶自建 workspace 即得 owner（`workspace.create` sudo bootstrap owner row／`project_project.py` else 分支唯一給租戶 owner 處，test_FPA14 鎖）→ ir.rule owner_admin_manage `perm_write=t`＋四觸發欄皆無 groups＋ir.model.access 1,1,1,1 ⇒ 可經真 call_kw 塞 `cs_skip_access_sync` 讓 access 欄位落地但 operator/CF sync 靜默跳過（fail-open；四欄全可攻，實測 session_duration DB=8h／operator 收到 1h、零錯誤 drift）。雙探針＋陽性對照＋role='user' 對照（ir.rule 先擋、key 無關）皆對上；`_get_operator_client` fail-loud＋access_app_id@3361 觀測點靜態確認。PoC 留 worktree/沙盒未外流。**Eugene 2026-08-20 裁：修**。清理已做實（cs-787-reach dismiss／沙盒 probe787 刪／verify worktree 移；⚠ 發現直接打字進 teammate 終端不觸發、只有 maestri ask 會，故清理由 leader2 手動執行）。**已派新 ccou『cs-787-fix』**（worktree wt-787-cs-fix＠fix/787-cs-sentinel、off develop 00be40da）：#536 私有 sentinel＋judge（truthy 非 sentinel＝租戶偽造 ⇒ WARNING＋fail-safe、sync 照跑）／registry pending→enforced／RED-first 測試／manifest .158→.159／無 migration／PR ⛔ 勿代 merge Refs #787。**✅ PR #793 交件＋leader2 獨立覆核 PASS**（靜態驗：修法 _is_cs_sync_control fail-safe＋fail-loud 逐字對 #536、讀取點改道；測試 A2·A3·B2·G2 對 pre-fix 裸 truthy 必紅、A1·B1·G1·G3 控制格全綠、A/B 真 call_kw 偽造；registry enforced schema 齊；9 fixture 遷 helper；base 00be40da 未 stale、manifest .158→.159 無撞號。**自跑** repo-lint --all＋gate 自測全綠。未 re-run 容器 RED/GREEN，改靜態驗測試邏輯＋自跑 gate）。**✅ 已 merge（2026-08-20 PR #793 merged by eugene，develop dfb9b5b3、.159）**：cs_skip_access_sync 上 develop（讀取點改道、gate enforced）、**merge 後 leader2 實跑 develop repo-lint --all 全綠**（全隊不被擋）、清理完成（cs-787-fix dismiss／wt-787-cs-fix 移／兩 branch 刪／沙盒零殘留）。⇒ **#787 A 段 5 把裡第 1 把落地**。餘：**SH 三把（sm_route_cascade_unlink／sm_skip_access_sync／sm_force_access_sync）2026-08-20 派 ccou『sh-787-reach』驗完（sh-787-reach 交件＋leader2 靜態獨立覆核）**：① `sm_skip_access_sync`(write:362)＝**CONFIRMED reachable**（同 cs、四觸發欄全可攻 fail-open）② 同 key create:332＝**NOT reachable**（ACL create=0；唯一 sudo provision 路徑更早死在 tunnel_token field groups=group_system——靜態預測「可達」被實測推翻＝with_user 會遮的假綠）③ `sm_route_cascade_unlink`(:379)＝**守衛被 forge 關掉、但刪除 ACL 擋死**（route/smart_home/workspace 三層 unlink=0；forge 確抵 :379 翻分支 UserError→AccessError、守衛對租戶防護＝0）④ `sm_force_access_sync`(:558)＝**CONFIRMED reachable**（forced 略過 _access_is_live state 閘 line423、停權 SH 仍強推 CF Access＋回寫 app_id；⚠ 危害有界：停權 ingress 已 503、強推不恢復流量 inferred＋docstring 佐證）。leader2 驗 tunnel_token groups／forced 分支／三層 unlink ACL 全對上、git 只 probe787sh/ src 零改。**修法建議：三把一併 #536 sentinel（一張 PR）、sm_skip＋sm_force 必修、sm_route_cascade defense-in-depth（後兩者各有 production setter 要遷 helper）**。🔴 另發現 3 超範圍缺口（非 context-key 家族）：租戶可 call_kw 直寫 smart_home.state=suspended（自我滿足 sm_force 前置）／smart_home_route.write() 無訂閱閘（過期訂閱仍可改 Access）／租戶似乎刪不掉也開通不了自己 SH（inferred）——待裁另開單。**✅ 2026-08-20 SH 三把已 merge（PR #795 merged by eugene、826f5f6d、develop .161；leader2 覆核 PASS〔四讀取點 fail-safe＋fail-loud、兩 setter 遷 helper、registry 三把 enforced、C 組例外型別翻轉判準、自跑 repo-lint＋gate 綠、命名不撞〕＋merge 後 develop repo-lint --all 全綠＋teammate/worktree/沙盒/branch 全清）**。原派 ccou『sh-787-fix』（worktree wt-787-sh-fix＠fix/787-sh-sentinel、off develop 2258630e/.160）——另立 sync-control sentinel（🔴 不共用 _INTERNAL_SM_ROUTE_IDENTITY_WRITE，sh_model fixture 會連帶關守衛；🔴 命名不與既有 marker 同名/互子字串）、三讀取點＋兩 production setter（smart_home.py:733/:917）＋測試佈景遷 helper、registry 三把 pending→enforced、RED-first（cascade 用例外型別翻轉當判準）、bump .161、PR ⛔ 勿代 merge Refs #787。verify 資產已清（sh-787-reach dismiss／沙盒刪／worktree 移、branch verify/787-sh 保留 REPORT@eba0d904）。交件後 leader2 獨立覆核。✅ **協調已了（2026-08-20）**：PR #795 開出時 leader2 已通知 leader1＋經 issue-leader hub 轉 leader3/leader4；merge 後 issue-leader 已以『已 merge、序列化解除』轉三位 ⇒ leader1 #783／leader4 #785·#794 現可從新 base .161 開工、leader3 #769 零重疊、#739 models 零重疊但撞 manifest＋tests/__init__〔#467/#544/#684 集合差驗坑，見上〕。原定：sh-787-fix PR 一開出即 ask **leader1／leader3／leader4 三位**附 PR 號（**只有 leader4** 真撞：#785〔smart_home_route.py＋smart_home.py〕＋#794 同改我這兩檔 ⇒ 要拿 diff 避 hunk、他等我 merge 後才開。**leader3 部分撞**〔2026-08-20 leader3 二次更正〕：#739／#769 走 controllers/smart_home.py〔非 models/〕與 operator tunnels.py〔避開 :917 _do_resume〕＝models 零重疊；但 🔴 **#739 撞 src/__manifest__.py＋src/tests/__init__.py**（#739 也要在該 __init__ 註冊 HttpCase）——tests/__init__.py 正是 #467/#544/#684 的集合差驗坑（雙方各 append import、計數驗被灌水、少一條整檔不 collect＝一條沒跑與全通過同輸出）⇒ #739 rebase 後須集合差驗、報條數連判準定義（leader3 已入 sh739-fix 工單）。#769 才真零重疊〔operator 命中 0〕。leader1 #783 序列化〔registry＋manifest 撞〕、等我 merge）；**merge 後同時 ping 三位：leader1〔#783〕／leader3〔#739·#769〕／leader4〔#785·#794〕**。版號 .161＝我、.162＝#786 leader4 平行（與我零重疊、現可開），各自 merge 前重驗嚴格大於、不信預留槽位。（金流子系統、可達性未知）維持 pending 未動；3 值注入面〔第一項 sa_unlinked_cf_policy_ids ＝ #783、leader1 接；leader2 實查 operator 後更正定性為**跨租戶**（原『受 app scope 限制』係承襲 #536 未查證誤植、delete_reusable_policy 純 id 帳號級 DELETE、已 comment #787）；與 leader1 講定**序列化**＝leader1 等 leader2 SH PR merge 後從新 base 開，因 manifest＋repo-lint registry 兩張會撞、smart_home_route.py 本身不撞（sa_unlinked 消費端已由 #528 PR#781 搬到 cf_access_policy_reap.py）〕、停權家族另案、及 2 個超範圍授權缺口（租戶可自我停權 SH／smart_home_route.write 無訂閱閘）待 Eugene 裁開單；#787 tracking 仍 open |
| 📕 **cd#115** 已關（2026-08-20T07:18:39Z，輪替完成並驗收）| leader3 | 2026-08-18 | ✅ **完成**：prod／stg 現各持一顆獨立 token（`woow-paas` id `72016035…` 指紋 `0a7747526513`／`woow-paas-stg` id `43ea14d5…` 指紋 `8997146a0aad`），懸置憑證已隨 Roll 作廢，letsencrypt 那顆未動且 ClusterIssuer Ready。完整證據與未驗清單見 cd#115 收尾留言，勿在此重述。<br>以下為過程紀錄（**留著不刪：近失與判準教訓**）——唯讀前置全過——baseline 指紋 stg/prod 相等（與 #111 記錄一致無漂移）、探針負向對照通過、DRY 演練（佔位字串）stg/prod 皆過未寫入，證據留言在 cd#115。(A) 已拍板＝**拆兩顆**（08-18 Eugene）；操作卡已交付，待 Eugene：(B) CF dashboard 鑄兩顆 token＋自己終端跑 cf_token_set（stg 先、全綠再 prod）(C) 全綠後 CF dashboard Revoke 舊 token（🆕 輪替只換叢集指向、**舊 token 不會失效**，expires_on:null）。另 🆕 **cd#117**（施工副產物外洩票，見 A1 段）已開、待 Eugene 併裁。🔴 新 token 只能 Eugene 在 CF dashboard 鑄、值不得進對話框／逐字稿／agent 會 Read 的檔案。<br>🔴 **2026-08-19 身分翻轉（leader3，證據在 cd#115 留言 6／7）**：叢集 prod＋stg 實際在用的是 **`woow-paas-stg`**（id `43ea14d5…`、**無客戶端 IP 篩選**）；**`woow-paas` 才是值下落不明的懸置憑證**（它才有 IP 篩選）。⇒ **照 08-18 舊前提去「revoke 那顆值不明的 stg」＝殺掉 prod＋stg 正在跑的憑證**，且無任何部署訊號、要到下次租戶操作才炸。判別式＝叢集出口 IP 不在 `woow-paas` 的白名單內，但 pod 內用該 token 打 CF 會通。<br>⛔ 第三顆 **`woow-paas for letsencrypt` 在用中不是孤兒**（消費者＝`cert-manager` ns 的 secret `cloudflare-api-token` ＋ ClusterIssuer `letsencrypt-prod`），處置清單要標「刻意不動」，盤點基準是**三顆**不是兩顆。<br>🔴 **IP 篩選不是本案的緩解因子**（有篩選的是沒外洩那顆）⇒ #111 的曝險評估**維持原強度、不下修**。<br>改推 **(乙) 鑄兩顆新的**（(甲) Roll 只換值不改設定：補不上 IP 篩選，且對 `woow-paas` 做 Roll＝再造一顆懸置憑證，它該 Delete/Revoke）；鑄新時順手縮權限（兩顆現存物件都有 `電子郵件路由規則:編輯`，operator 呼叫面未使用）＋修命名（現在 prod 跑的是叫 `-stg` 的那顆）。<br>文件面已了結：**#111 closed／PR #114 merged**（fb302fb，merge 後以 main 版函式對叢集重跑正查負查皆綠）。<br>🔴 **近失成真（08-20）**：Eugene 對 `woow-paas-stg` 執行 Roll ⇒ **prod 與 stg 的 CF 控制面同時停擺**（兩側 pod verify 皆 `Invalid API Token 1000`），完全如上述翻轉所預測。資料面未受影響（cloudflared 用 tunnel 憑證、`Running 66d` 0 restart），停擺期間無實際失敗呼叫。⇒ 最終走法不是操作卡的「鑄新」而是**對兩個既有物件各 Roll 一次**（Roll 保留權限樹，消掉「手動勾漏」風險）＋ 對 `woow-paas` 額外移除客戶端 IP 篩選（其白名單不含叢集出口 `114.32.21.18`，不移除則 prod 打不通）。<br>🔑 **SOP 教訓（本次實戰驗證 PR #114 的探針）**：`cf_token_set` 執行後出現過一次 **「相等 OK 但指紋＝baseline」——正確抓到「寫入根本沒發生」**；若驗收只寫「三側相等」會判成功。判準必須是「三側相等 **且** 指紋 ≠ baseline」。另 prod 有 2 replica，`deploy/` 只會挑一顆，須逐 pod 驗才排除「一顆還舊」。<br>⏳ **殘留（未隨本單關閉）**：① 客戶端 IP 白名單刻意先不設——Beacon（ccsx，woow-machines-operator）唯讀查證結論＝4 台共用 NAT 出口 `114.32.21.18`、追蹤檔自 2026-03-14 無第二個對外 IP，但**ISP 合約／路由器 WAN／備援線路皆查無文件 ⇒ 固定性「未確認」**，要設得先拿到線路類型；② **cd#117**（`auth.apiKey`＋registry 密碼進 agent 逐字稿）仍 open 待裁決 |
| **#519** 📕 **已關 2026-08-14T23:34Z** | leader1 | 2026-08-14 | ✅ **完成並驗收**：PR #727／#729／#734 三張全 merge，develop 到 140、`src/migrations/18.0.1.0.140/` 已落地；設計與證據見 issue 留言 **7081**／**7093**，勿在此重述。衍生 **#725**（sh_subscription 鑄幣口）仍 open、無人認領（見文末 ⊘ 段）。<br>⚠ **本列保留原因**：以下錯因與教訓段為 leader4 明示要留，看板週期清理不刪；施工過程紀錄已於 2026-08-16 清理移除（可追溯 issue 留言）。<br>🔴 **2026-08-14 leader4 更正自己寫的東西**：本行尾端原本被我追加了一段「仍 open、最新留言 7081、**Eugene 尚未裁決**、三個方向沒有哪一個中選」——**那是錯的，已刪**，以本列開頭 leader1 寫的裁決結果為準。<br>錯因留著不刪，因為它會再犯：我的查證抓到的最新留言是 **7081**（`2026-08-14T06:06:44Z`），而 Eugene 的裁決是留言 **7093**（`2026-08-14T07:06:52Z`）——**查證與寫入之間隔了一小時，我把快照當現況寫了進來**，還在括號裡加了「只補新鮮度」五個字，讓一段已經過期的敘述看起來比周圍更可信。<br>🔑 **「補新鮮度」這個動作本身也需要新鮮度**：落筆前要重驗一次，不能拿查證當下的結果直接寫。而且這種錯**不會以衝突現形**——它安靜地躺在同一列的另一端，跟開頭的正確敘述並存，誰讀到最後就信誰。⇒ 往同一列追加狀態時，**先讀完整列**確認不會和既有敘述打架 |

---

## 分類原則（Eugene 2026-08-12 定，5 類）

| 類 | 定義 | 現在做嗎 |
| --- | --- | --- |
| **② 實作類** | 直接推進上線 | ✅ token 優先 |
| **③ 需討論類** | 需求/策略未定 | ⏸ 先問 Eugene，別自行開工 |
| **①a docs 文件知識類** | 開發常識、文件沉澱 | ⏸ 先問 Eugene，別自行開工 |
| **①b 固化自動驗證** | test/lint/audit/docs pattern 等可程式化省 token 的驗證 | ⏸ 先問 Eugene 要不要做＋附成效評估 |
| **⊘ 非 paas 管轄** | 內文自陳歸屬 woow-machines-operator | ⊘ 不排進來 |

### 🔴 補充判準（Eugene 2026-08-18 定）：「今天可被利用／被繞過，但修法界線未定」怎麼歸類

**分類按「**要做什麼**是否已定」排，不按危害排、也不按「怎麼修」是否已定排。**

判準分兩層，🔴 **第二層是 2026-08-18 存量盤點時補的，缺了它會把一批明確的 bug 誤凍在 ③**：

1. **「要不要做／做什麼」未拍板 ⇒ ③**。典型訊號：內文在問「該不該」「誰能」「哪一派對」「要不要接受這個風險」——
   那是產品／策略決策，實作者代決會做錯人。
2. **「該做無疑，只是怎麼修未選型」⇒ 留 ②**。典型訊號：內文寫「修法方向未定案」「三個選項各有取捨」
   「需設計決策」——**選型是實作的一部分**，不是策略未定。

**兩層都要在它「危害所屬的那個 ② 分段」補一條 🔴 交叉指標指回去**（A1 資安／A2 金流／A4 功能…），
排該段優先序的人才不會因為它不在本段就漏看。

- **為什麼不把「修法未定」全丟 ③**：② 的定義是「可推進上線」，③ 的政策是「⏸ 別自行開工」。
  只看「修法定了沒」的話，`#739`（SH provision 從來沒成功過）、`#155`（pod 活過 1 小時 skill 必 401）
  這種**明確壞掉的功能**會被標成「別自行開工」——**A4「壞掉的功能」子群會被整段搬空**。
  2026-08-18 的盤點實際跑出這個結果（16 張候選），Eugene 當場裁決加上第二層區分，最後只有 2 張真的該搬。
- **為什麼不能只留 ③ 不加指標**：③ 讀起來像「不急」，而 #710 這種是今天就能被利用的洞。
  **分類 ≠ 嚴重度**，交叉指標就是拆開這兩件事的載體。
- 🔴 **判定要讀 issue 原文，不能讀二手轉寫**：同一句「修法方向未定案」，在 `#578` 是「對外 API 契約要不要改」（第 1 層 ⇒ ③），
  在 `#732` 是「三條守衛寫法哪條不打壞合法流程」（第 2 層 ⇒ 留 ②）。**光看摘要片段會判反**——
  2026-08-18 盤點時 leader2 就照 agent 的引用片段把 #732 判成該搬，讀了原文才推翻。
- **首批適用**：#710／#704（→ A1 指標）、#773（→ A2 指標）。
- **存量追溯**：Eugene 裁決**全部對齊**——2026-08-18 已執行完一輪，結果見文末裁決段。

---

# ② 實作類（89 筆）— 可推進上線

## A0 · prod 事故（1 筆）

> 原本這裡有 5 筆：3 筆（#512／#441／#442）經查證屬叢集運維，已移到文末 ⊘ 段；#503 與其復發防治 **#694** 均已關單並完成 prod 驗收（證據在各該 issue 留言，2026-08-16 清理移除歷史段）——殘餘承接：租戶側偵測走 **#702**、marker 告警走 **#714**（皆在 ②A4／②A3 有列）。

| Issue | 一句話 |
| --- | --- |
| **#389** | prod SMTP 寄信全 exception → **歸屬已查證（08-02 prod 實查）：問題在平台本體 Odoo（ns `paas-system` 的 paas-odoo pod），不是客戶站**；root cause 已確定，修復卡在「用哪家 mail provider」未決策 |

### 🔑 #389 根因已查明（2026-08-02 prod 實查，唯讀）——舊看板的「歸屬未查證」已過期

平台 Odoo **從頭到尾沒有設定過任何 outgoing mail server**：`ir_mail_server` 0 rows（含 inactive）、`odoo.conf` 四行
`smtp_*` 全被註解 ⇒ 退回內建預設 `localhost:25`，而 pod 內 25/465/587 無服務監聽 ⇒ 每次寄信必然
`ConnectionRefusedError [Errno 111]`；7 筆 exception 的 `mail_server_id` **全為 NULL**，證明走的是「無 server → fallback」
而非「某台設好的 server 壞掉」。pod 內 python socket 實測 gmail／office365／sendgrid 的 25/465/587 全部握手成功
⇒ **排除叢集 egress／防火牆**。7 筆積壓全是 OdooBot 內建 Digest Email（寄給 `admin@example.com`），沒有一筆業務信
⇒ 進一步佐證是平台系統本身從未配置 SMTP。紀錄在留言 **4434**（該留言即本單最後更新，此後無動作）。

**修復未執行、卡在決策**：要用哪家 mail provider、帳密由誰提供；之後才是建 `ir.mail_server` ＋設 company email／catchall domain。

## A1 · 資安：租戶可實際利用（23 筆）

> ⚠ #556 的修法帶一個**會靜默失效的前置條件**：日後若「簡化規則」誤刪 workspace_access 的 manage rule，靠它撐著的 #519／#583 守線會**無聲**失效。
>
> **#698（Critical，2026-08-13 由 PR #705／`31dee82a` 修復關單）的 7 張衍生單已於 2026-08-14 逐張讀內文分類完畢**——
> **#706**／**#715** 留在本段；**#716** → ②A4「壞掉的功能」；**#703**／**#704**／**#710**／**#711** → ③（四張的內文都自陳「待決定（不是結論）」，修法選項未拍板）。
>
> 🔴 **交叉指標——下面兩張雖然分流到 ③，但它們是租戶今天就能實際利用的洞**，排 A1 優先序時不要因為它們不在本段就漏看：
> **#710**（guest 直接 `unlink` project ⇒ project 連同 cloud_service 關聯整個從 workspace 消失，比 #705 擋掉的那條更徹底）、
> **#704**（租戶可自行 write `use_agent_runtime` 開 agent runtime、實際佈署 pod 吃資源）。
> 它們進 ③ 的理由是**修法界線未定**（「誰能刪 project」「擋寫要不要連讀一起擋」），**不是危害低**——別把分類當成嚴重度。
> ✅ **2026-08-18 Eugene 已裁決**：這個做法（留 ③ ＋ 在危害所屬分段補交叉指標）**升格為通用判準**，
> 不再是這兩張的個案處置——規則全文見開頭「分類原則」段的補充判準。
>
> 🔴 **同批（2026-08-18 存量盤點）再補四張——同樣分流在 ③、危害落在 A1，排本段優先序時一併看**：
> **#152**（project owner 可繞過 UI、直接經 ORM/RPC 寫 dedicated `agent_template` 的 `skill_ids`／`mcp_server_ids`／`system_prompt`；待決＝要不要限縮 admin-only，選項 A/B 未擇一）、
> **#711**（`_check_workspace_access` 只驗成員資格**不驗 role**，隨後 `sudo().create()` ⇒ `env.su` ⇒ **豁免 #705 的綁定守衛**；4 個互斥選項未拍板，其一是產品行為變更）、
> **#385**（CF 孤兒盤點：**其中一條是 `allow + everyone`** ⇒ 孤兒不只是垃圾，可能是還掛在 Access app 上的**放行規則**；待決＝清哪些。成因與修法方向見 #528）、
> **#721**（租戶已有 `group_system`＋`erp_manager` ⇒ `ir.actions.server(state='code')` → `_run_action_code_multi` → `safe_eval(mode='exec')`，
> 而傳進去的 `'env': self.env` **是本尊不是受限代理** ⇒ 資料層今天就等同 superuser；spike 單、不含實作承諾）。
> ✅ **#721 的承接已落地（2026-08-18）：開了 #774**（在本段網路隔離子群）。⚠ 標的與盤點者原建議**不同**：盤點者建議追蹤「資料層等同 superuser」，開單時讀 #721 原文後改追 **§5.2 的 OS 層逃逸未驗證**——前者在租戶自己的實例內是設計後果（爆炸半徑＝自己的 DB），後者才是叢集面且**結論會翻掉 #721 的核心判定**。兩種讀法都寫進 #774 內文，若認為前者也需要獨立載體可直接說。

**跨租戶奪權／讀取（最高，會被真的用到）**

| Issue | 一句話 |
| --- | --- |
| **#718** | 上游 hermes `web_server.py` **未接 approval gate**——`/api/memory/providers/{name}/setup` 的 `install_cmd` 走 `shell=True`＋`bash -c` 卻無危險指令守衛（陽性對照：`tui_gateway/server.py` 有 3 處命中，故不是探針失效）。**已登入租戶可達**（authproxy 不擋、app middleware 只擋未登入），且 `check_cmd` 是**被動觸發**（Plugins 頁載入即跑，Install 按鈕守不到）。歸屬上游、非我方改動所致 ⇒ 已決策**回報上游＋內部記錄，不自行 patch**（本 repo 對上游目前只 patch `web_dist` 一處）。架構事實見 `docs/decisions/hermes-agent-command-execution-planes.md`。<br>🔴 **2026-08-16 風險敘述已變（#717 的 chart 0.2.0 落地後）**：注入 `npm_config_prefix` 後，第四面 `install_cmd` 從「無 gate 但**跑了也裝不起來**（`/usr/local` 對 uid 10000 是 EACCES）」變成**會成功**——落點 `/opt/data/.local/bin`（canonical、PVC 持久、且位於 L3 root PATH 前段）。**非新洞**（同 uid／同目錄／同通道），但排修復優先級時請以新敘述為準。圍堵載體＝`docs/guides/deployment/hermes-agent-console-support-runbook.md`（console 不做任何安裝）；根治仍是上游接 approval gate。詳見 issue 留言 **7582** |
| 📕 **#536** 已關（2026-08-20 PR #779 merged by eugene，develop .153；SA sync-control 四鍵改私有 sentinel＋死鍵清除）| 承接：gate → **#788** 已做並 merge（PR #789、develop .157、gate `internal-ctx-key-registry` 上線、merge 後 develop repo-lint 全綠）、同型鑰匙普查 → **#787**（5 把 truthy 閘＋3 值注入面＋停權家族，皆存在坐實可達性未驗；registry 掛 pending）。#787 🔄 leader2 認領中（本輪先驗 cs_skip_access_sync，見「🔒 認領中」表）|
| 📕 **#753** 已關（2026-08-17 PR #767 merged by eugene，develop .149）| SA `_strip_identity_anchors` 用 `vals.pop()` 剝 identity anchor，但 `default_<field>` context 在 `super().create()` 內部才展開 ⇒ pop 抓不到——租戶帶 `default_tunnel_id`／`default_tunnel_token` 即可偽造身分錨點，**等於重開 #522 關掉的洞**（走 Odoo 內建 default 機制，與 #536 的 4 個自訂 escape hatch key 同根因、不同機制，非其第 5 項）。⚠ route 模型 `dns_record_id`／`access_app_id` 共用同支 helper 一樣中招（body 未點名，修別漏這半）。修法方向已定：比照 PR #752 CS 側 create 改壓 False；驗收要走非特權真 RPC（issue 自列，尚未跑，現況僅讀碼確認）。發現於 #744／PR #752 |
| **#531** | operator `PUT /api/tunnels/{id}/routes` 無 tunnel 歸屬驗證，且為整份 ingress 覆寫 |
| 📕 **#528** 已關（2026-08-20 PR **#781** merged by eugene，merge commit `68006c01`，develop `.157`；issue 經 Closes 自動關）| 停權中的 SA 刪 access policy → CF 同步被 state gate 靜默略過，**CF policy 永久 orphan（其中一條是 allow everyone）**。落地＝issue 方向 1＋3：doomed id 落 durable 帳本（新 model `cf_access_policy_reap`，**只收 DB 列值不收 context**；租戶零 ACL）＋三腿（SA/CS/SH）gate 收斂單一裁決處、帶殘帳被略過才 warning。✅ **方向 2（停權禁刪）Eugene 2026-08-20 裁「不做」**（採 leader4 建議，經 issue-leader 轉達；已留言在 #528）——理由：方向 1＋3 已擋住永久孤兒，方向 2 只會擋掉停權租戶清理自己 policy 的正常操作（正是本單事故當下在做的事：移除 allow-everyone）。查證要點：帳的消費者有四條，其中**兩條不需要 SA 曾經 resume**（cleanup teardown payload 併帳／刪列後錨點變 dead 由 cron 收）⇒「永遠不 resume」不會讓帳無主；方向 2 唯一買得到的是 resume 內部「先復原 ingress、後收 Access 帳」那個 DB/CF 分歧窗口，且它裝在 policy.unlink() 上會被三個 ondelete=cascade 路徑整個繞過。**此線結案，各 leader 不動。**測試 17 條嚴格 RED→綠；ccsu 獨立覆核 8/9 OK，唯一 CONFIRMED 缺口＝已揭露的 **#784** 姊妹路徑（刪整條 route，仍 open）。收尾開出 **#783–#786** 四張 follow-up（見下四列，皆 open）。過程教訓：merge #779 時真正的雷不在 git 報衝突處——全新方法 `_access_sync_skip_reason()` 裸讀 `sa_force_access_sync` 不會報衝突、卻會靜默重開 #779 剛 sentinel 化堵住的偽造面，已改用 `_is_sync_control()`（詳 PR 留言 7913）|
| 📕 **#783 已關**（2026-08-21 PR **#800** merged by eugene，merge commit `c74eeee4`，develop `.167`；issue 經 `Closes` 自動關。修法＝拿掉 context 通道本身〔動讀取端 `_pending_for`〕＋repo-lint 第 28 條 `status="removed"` 防復原；詳見 PR #800 留言 8183 與 #783 留言 8008／8016／8025，此處不重述）| **裸 context key `sa_unlinked_cf_policy_ids` 可偽造「要刪哪些帳號級 CF Access policy」的清單**——#536 家族漏列的第五把（#779 實查未涵蓋：其 diff 逐字 grep 命中 0）。PR #781 的帳本只收 DB 列值所以不落帳，但「帳本∪context」的 context 腿原封不動，偽造值照樣進 `existing_policy_ids` 刪除清單 |
| 📕 **#784** 已關（2026-08-20 PR **#791** merged by eugene，merge commit `00be40da`，develop `.158`；✅ **覆核回修 PR #792 已 merge**（2026-08-20 by eugene，merge commit `2258630e`，develop `.160`）——#791 曾在覆核完成前被 merge，F1[HIGH] cron 佇列自我堵死等 9 項的修復**遲了一步才進 develop**（曾有一段時間 develop 帶著會自我癱瘓的回收 cron）。四輪覆核全 CLOSED，過程另補：repo-lint 新 gate `access-gate-no-ledger-read`（同名碰撞不靜默挑一個、改報「無法判定」）、`cloud_service.py` 姊妹結構教訓指針、`test_E2` 斷言收窄（覆核者獨立推導認同：換掉的只是優化，維持原斷言反而是 #794 fail-open 的燃料）| **刪整條 SA route 時 Access teardown 被無 forced 旁路的 gate 靜默略過，且 policy 的 `ondelete=cascade` 是 DB 層 FK CASCADE ⇒ Python unlink（含 #781 落帳）整支不執行**——整個 access_app_id＋該 route 全部 cf id 孤兒且帳本零記錄，與 #528 同型但丟失面更大。CS/SH 對照組都做對了（無狀態閘／`forced=True`），獨 SA route 漏。**落地**＝三個出口都改（判準從 liveness 改成「手上還有沒有 id 要拆」，收斂成 `_access_teardown_ids()`／`_has_access_teardown()` 單一裁決處）＋dead-anchor 帳由新 hourly cron `_cron_reap_dead_anchors()` 回收（帳本擴 `kind`、`cf_policy_id`→`cf_object_id` 附 migration）。⚠ **產品可見變更**：停權／無 live tunnel 的 route 刪除時 CF 拆不掉，現在仍刪成功（落帳待 cron），不 fail-loud。🔴 **本輪最大教訓＝覆核晚於 merge**：ccsu 覆核判非 PASS（F1 cron 佇列被不可回收列堵死 ⇒ 每小時做 0 件事、零訊號，**回收機制自我癱瘓**；F3 批次 unlink 回滾撕裂；F9 orphaned 回流放大 #783 偽造面），但 PR 當時已被 merge ⇒ **有洞的版本已在 develop**，修復擱在 #792 等覆核。下次此類 PR 要在 merge 前完成覆核 |
| **#785** 🔒（2026-08-20 #528 收尾開；**leader4 認領，2026-08-21 03:49Z 起序列化約束解除、現在可派**——擋它的 sh-787-fix 與 PR #802（`0eb11503`）都已進 develop；見「🔒 認領中」表 #785 列；歸類兩可：機制 bug 暫列本段，下輪看板同步可分流）| SH `_capture_access_teardown` 捕獲失敗回 None **完全無 log**，消費端 `continue` 且不計入 failed ⇒ 呼叫端 fail-loud 判成功；且兩種 None（「沒東西要拆」vs「有東西但推不動」）語意混同 |
| 📕 **#794** 已關（2026-08-21 PR **#802** merged by eugene，merge commit `0eb11503`，**03:49:42Z**，develop `.168`；經三輪回修＋三輪獨立覆核＋merge train 最後一節解衝突；覆核抓到兩個 HIGH **都是「補償安靜地什麼都沒做」**——第五腿 `SecurityAccess.unlink()`（原排除理由驗錯了對象：raise 在呼叫端、且在 partial success 之後）與 `torn_down` 空轉（append 卡在 `if ok:` 而落帳會 re-raise `OperationalError`，四腿全中）。⚠ **Odoo 測試在 merge 當下仍從未有人跑過**（`.gitea/workflows` 無 test-tags job＝#546），leader4 另派沙盒實跑，結果留言進 #794）（原開單脈絡：優先評估 issue 內文提到的第三條路「補償寫」（raise 後在回捲不到的地方清空 `access_app_id`／cf id，讓下次 sync 重建新 app），因為它**只動 Odoo 側不動 operator**，避開選項 B 的反轉風險與真 K8s integration test 成本；版號＝開 PR 當時 develop+1（原預留 `.164` 作廢）。⚠ **develop 既有形狀、非 #791/#792 引入**）| route 的 `access_app_id` 指向**已不存在**的 CF Access app 時，之後每一次 sync 送 `enabled=True`＋stale app_id ⇒ operator `PUT /apps/<dead>` 拿 404 ⇒ `raise CloudflareException` ⇒ 該 SA 的 Access 同步**永久失敗**。🔴 **已裁決＝無聲 fail-open（未授權存取），`[security]`**——開單第一版寫成「每次 sync loud-fail 會出聲」**已被真 registry 實測推翻**（issue 內文已重寫並明載推翻，不是靜默改）。判別式＝**改 `service_url` 走 `_sync_routes_to_operator()`（DNS/ingress），`sync_access_app` 根本沒被呼叫**；只有改 `is_protected`／`login_*`／`session_duration` 才走 Access 路徑（那條才是次要的 loud-fail 子路徑）。⇒ 列帶 `is_protected=True` 復活、UI 顯示已保護、hostname 照常服務，但 CF 上已無 Access app ⇒ **任何人不需登入即可存取**；四條偵測管道實測全部照不到（帳本與 `super().unlink()` 同 savepoint 一起回捲、cron 因錨點活著不收、stuck warning 判準是帳本列、`reconciler.compute_orphans` 方向相反只抓「CF 有 DB 無」）。SH 同型且窗口更寬（`smart_home_route.py:382-397`，還沒有 SA 在 #792 加的分組）。⚠ 未實測的一格：「CF app 被刪後該 hostname 不再有登入強制」是 CF 語意、未對真 CF 打過，不成立則嚴重度下修。觸發面窄（controller 一次刪一條、SA cascade 保證 pass 1 恆空 ⇒ 真正到得了的是直接對 model 下多 id unlink 的 RPC）。修法【未拍板】兩選項：A 不改 sync、把「刪掉重建」變成可被發現的解法；B operator PUT 404 時退回 POST 自癒——⚠ B 要先確認 404 語意是否唯一，若「存在但無權讀」也回 404，自癒會多建一顆 app 並把舊的變真孤兒＝**把會出聲的問題換成 #528 家族的靜默孤兒**。⚠ 整條因果是讀 operator 原始碼推導、**未對真 CF 打過、無測試鎖** |
| 📕 **#786** 已關（2026-08-21 PR **#796** merged by eugene，merge commit `5c536a83`，**01:17:33Z**，develop `.162`；leader4 收尾已做實：merge 後 `git diff --stat 826f5f6d 5c536a83` 非空＝內容真的落地不是只有版號、repo-lint 在該 commit 全綠且以注入重複 method 取得陽性對照）| `AccessPolicy.write()` 換錨點時 `_access_sync_targets()` 在 `super().write()` **之後**求值 ⇒ 舊錨點永不重推、CF 側殘留無人回收（後果同 #528）。**不是忘了觸發，是觸發了但對象取錯**——症狀更難發現；現況僅 raw ORM 可達、無守線無測試 |
| **#289** | project↔CloudService 綁定授權缺 model-level guard |
| **#282** | `workspace.slug` 補 write guard（identity-anchor 不可變） |
| **#706** 🆕 | **承 #698／PR #705 的既有綁定盤點**——`cloud_service_binding_guard` **只擋新寫入、不回溯**，既有跨 workspace 的 project↔cloud_service 綁定不會自動消失，且經 `_reconcile_agent_runtime_reach` 餵給 agent pod 的 egress allowlist 與 agent ns 的 `reach-ws-*` label ⇒ **守衛上線後那些 reach 授權照樣有效**。⚠ 跨 workspace 綁定**本身是合法設計**，無法機械判定攻擊或合法 ⇒ 本單刻意**只做唯讀盤點**（查 DB 撈跨 workspace 綁定與 `create_uid`、比對是否為所涉 workspace 的成員／admin、比對 operator 側 label 與 NetworkPolicy egress 實況），盤完才決定要不要清、怎麼清（可能牽動 #691 的版號競賽）。⚠ 讀 prod DB 需要憑證 ⇒ 那一段走 `woow-operator` agent |
**同 workspace 內的角色繞過（guest／user 越過 owner/admin-only 界線）**

> 🆕 2026-08-14 新增的子群。既有四個子群沒有一個對得上：這類洞**不是跨租戶**，而是同一個 workspace 裡低權限角色繞過「只有 owner/admin 才能做」的界線。硬塞進「跨租戶奪權／讀取」會讓讀的人高估爆炸半徑、也會讓真正的跨租戶項被稀釋。

| Issue | 一句話 |
| --- | --- |
| 📕 **#715** 已關（2026-08-19 PR #775 merged by eugene，develop `37eedd5a`/.152；⚠ 行為變更＝guest/user 從此搬不動會改變綁定的 task）| **guest 用「搬 task」換掉 cloud_service 綁定**——`cloud_service_binding_guard` 只比對 `write()` **指名的欄位**，而 `task._cs_binding_fields` 沒列 `project_id`（`project_task.py:20`，⚠ 行號為查證當下，動手前重新定位）⇒ **move task 整道跳過守衛**；但 `primary_cloud_service_id` 是 `project_id` 的 **stored compute**，換 project 就重算成新綁定 ⇒ 繞過 owner/admin-only 的授權。**已實測** guest 可搬自己 workspace 內的 task 讓 primary 靜默換綁。範圍限同 workspace（非跨租戶）。修法已明確：`project_id` 加進 `_cs_binding_triggers`，**但要防 #705 F2 的批次迴歸**（見 #716 第 1 項）|

**憑證外洩**

| Issue | 一句話 |
| --- | --- |
| **cd#24** | prod registry 密碼明文躺在 `paas-operator-registry` 的 last-applied annotation（`priority:high`） |
| **cd#78** | 輪替已外洩的 Gitea helm repo `read:package` token |
| 📕 **cd#115** 已關（2026-08-20，輪替完成）| 承 cd#111（文件面已由 cd PR#114 修畢、2026-08-18 Eugene merge 自動關單）：**CF API token 輪替裁決**（`priority:high`）——token 明文已落本機持久載體、其中 5 筆直接貼進 Claude prompt（值已離開本機邊界）；`expires_on: null` 永不過期、服役約 4.7 個月；權限面 08-18 唯讀實測：DNS 337 筆＋Tunnel＋**17 個 Access 認證閘門**三面可及，stg 曝險＝prod（同顆）。評估建議輪替＋走 PR#114 新版 SOP（先 `DRY=1` 演練、驗「三側相等且指紋≠baseline」）；🔴 **輪替與否是 Eugene 裁決，且新 token 只有 CF dashboard 能鑄——agent 不自行輪替**。<br>📕 **已於 2026-08-20 輪替完成**（prod／stg 各一顆獨立 token、懸置憑證作廢）。**本列上述敘述是輪替前的狀態，勿當現況引用**；結果與教訓見「🔒 認領中」的 cd#115 列與 cd#115 收尾留言 |
| **cd#117** 🆕 | cd#115 施工副產物（08-18 agent 自報、leader3 代開票留痕）：yq 過濾 regex 沒防 base64 `+/=` ⇒ stg fleet secret 的 `auth.apiKey`（用途待查）與 `registry.password`（JCR，username=woow-prod）明文進 agent 逐字稿，**已隨 tool result 離開本機邊界**（CF token 未外洩，del() 有排除）。待 Eugene 裁決兩值輪替與否；⚠ 與 cd#24 關聯——若 stg/prod registry 同顆密碼，本次是第二個外洩載體 |
| **#567** | 例外原文外洩面：`MissingError` 未收斂＋paas.py 3 處／billing.py 5 處 bare `str(e)`。⚠ 單裡自註「行號會 stale，動手前重新定位」 |
| **#584** 🔄 | **（2026-08-18 依判準第 2 層自 ③ 範圍未明搬入：§一 已由 Eugene 2026-08-05 裁決「bn_odoo 密碼不用輪替」）** 🔴 **搬進 ② 不代表這張已結案**——同一則裁決留言逐字寫著「§二 的 4 條未查證線索仍未處理，本單不因 §一 裁決而關閉」。剩下的是**待辦不是待決策**：4 條線索（含與 #556 相關的 prod 帳號歷史）要不要查、誰查。⚠ 查 prod 帳號歷史需憑證 ⇒ 那段走 `woow-operator` agent |

**網路隔離**

| Issue | 一句話 |
| --- | --- |
| **charts#42** | `charts/open-design` NetworkPolicy 跨 ns egress 洞（np-audit FAIL，block PR #41） |
| **charts#66** | skill sidecar 專屬 egress 收斂（externalEgress 開啟時 untrusted bash 可外連） |
| **#514** | L2 租戶 ns baseline egress 收斂——第三方 chart 無 egress NP，實測 n8n 仍直達 prod odoo |
| **#428** | odoo chart 28.2.10 `containerPorts.https` 未定義 → paas-odoo NP 全 TCP port 開放 |
| **#427** | Phase 2：paas-system 補 default-deny baseline NP（**前置：待 prod 有租戶後**） |
| **cd#85** 🔄 | **（2026-08-18 依判準第 2 層自 ③ 範圍未明搬入：body 已載 Eugene 2026-08-02 裁決「切斷——控制面不應對公網開放」）** `paas-api.woowtech.io` 是未版控的 **prod 控制面公網入口且走 stg tunnel**。**方向已定＝切斷**，剩餘是執行前置：#427 的 NP 上 prod 後這條入口即失效 ⇒ **先確認還有誰在用**、順帶盤點其他 tunnel。⚠ 與 #427 是**同一個切斷動作的兩端**，排序要一起看 |
| **#774** 🆕 | **驗證租戶 Odoo 的 `safe_eval` 能否逃到 OS 層**（2026-08-18 開，承 #721 §5.2 —— 該 spike 是唯讀、不含實作承諾，這個前提原本無任何載體承接）。#721 已靜態確認租戶有 `group_system` ⇒ `ir.actions.server(state='code')` → `_run_action_code_multi` → `safe_eval(mode='exec')`，且 `'env'` 是**本尊不是受限代理** ⇒ **資料層今天就等同 superuser**（⚠ 那條**不是**本單標的：租戶是自己實例的管理員，爆炸半徑侷限在自己的 DB）。本單要驗的是**能不能經非 dunder 方法鏈間接觸達 `subprocess`／`socket`／檔案系統**——成立則落點是 **root 容器 ＋ egress 不受限的網路位置**＝叢集面，且 #721 §1.3 的「OS 層才是自灌 addon 的真增量」整個翻掉。🔴 **兩個前提取自 #721 開單當時、本單未重驗**（uid=0 root／egress `{}`）——**#514 正在改後者** ⇒ 動手前先看 #514 進度，否則會拿過期網路事實推嚴重度。🔴 驗收：否定性結論必配陽性對照（先在放寬環境證明探針抓得到），結論只能寫成**範圍限定敘述**不得寫「safe_eval 是安全的」；用 dev sandbox 不碰 stg/prod；找到出口先回報不公開 PoC |

**agent runtime 沙箱**

| Issue | 一句話 |
| --- | --- |
| **ar#34** | sidecar 來源的 `tool_result` 未標記 untrusted-content（prompt injection） |

## A2 · 金流與計費（6 筆）

> 🔴 **交叉指標（2026-08-18 依裁決補）——下面這張分流在 ③，但危害落在本段**，排 A2 優先序時不要漏看：
> **#773**（CS 降級到無 AC seat 的 plan 後，既有 access policy 與 CF Access 規則繼續生效 ⇒ **付費界線被繞過**；
> 且 `reconciler.py:88` 判孤兒看「DB 端指不指得到 cf id」，row 還在就判有主 ⇒ **這一類結構上不會上孤兒候選、零訊號**）。
> 它進 ③ 的理由是「要不要收斂／收斂到什麼狀態」未拍板（unlink／停用保留／生效到週期末，三者計費體感不同），**不是危害低**。
> ⚠ 與 #521 的分工：#521 是「**建不起來**」（席數 enforcement 未接線，繫於 #381），#773 是「**建起來之後不會被收回**」。

| Issue | 一句話 |
| --- | --- |
| **#521** | CS 型 access 加購（`ac_max_access_seats`）——**載體缺口已補**（2026-08-16 五票 #743～#747 全關：quota 0=unlimited＋policy exclusive arc＋backend 接線＋Access 頁籤＋stg 真 CF 驗收；已入 v18.0.1.0.146 image，prod rollout 未做）。**剩席數 enforcement 一層**，繫於 #381；衍生收尾票已入看板分段（#753→A1、#758／#760→A4、#750→A5；#761 已關）|
| **#518** | `max_per_partner` 並發防護在 Odoo REPEATABLE READ 下**零貢獻**（advisory lock 取鎖後仍讀舊 snapshot） |
| **#515** | HAOS 開通前容量 pre-check——上限數字與實際資源脫鉤時仍會超賣 |
| **#361** | 改方案後要讓使用者看到實際退了多少點 |
| **#45** | subscription：stg/prod `end < start` non-trial sub 的 audit + dry-run backfill |
| **#43** | subscription：`.get(unit, relativedelta())` silent fallback 換成 `UserError` + 抽共用 `cycle_delta` |

## A3 · 發版鏈／CI／部署環境（17 筆）

> 以下已依「`priority:high` 與擋鏈的在前」重排，一句話未動。

| Issue | 一句話 |
| --- | --- |
| **cd#70** | prod chart bump ≥0.1.18 前須移除 `values-prod.yaml` 的 `pvcCleanup.image` override（`priority:high`，否則 CronJob ImagePullBackOff） |
| **ci#66** | ~~佔 runner 空轉~~——實作已全落地（9 線兩段式），**只剩驗收**：3 線各一次真實 phase 2。**六線驗收 runbook 已產出（#66 留言 7516，連 #68 一起收，Eugene 只需 6 次 dispatch＋/approve 或 /reject）**；dry-run 經查證不能替代（關閉條件 6367 要求真實 phase 2）。⚠ 順帶合排項已消：**APPROVAL_SEAL「首次真實 /approve 驗封緘」（合併檢查單③，ci PR#111 留言 7467）經查已在 2026-08-16 過**——PR#111 merge（01:08Z）後首個真實 /approve＝ci#117（image 20260816.0855，leader4 抽驗 issue 存在時間吻合），run 3857 log 印「body 完整性校驗通過（APPROVAL_SEAL 相符）」；其後 ci#121（v149）、ci#122（v151）同樣通過（deploy-v151 08-18 查證，run log 為證據源） |
| **cd#77** | `SKIP_MODULES_UPDATE` 三段手動流程脆弱＋bitnami 吞掉 `-u` 輸出 ⇒ **prod migration 執行是黑箱** |
| **cd#88** | agent-runtime chartVersion 追平 0.4.5 ＋ stg 既有 release reap 到 1.4.4 |
| **cd#83** | `paas-odoo/fleet.yaml` 加 `disablePreProcess`，消滅 promote 時的人工同步點 |
| **ci#68** | ~~gate-after-build 佔 runner~~——實作已全落地（候選 tag 機制），**只剩驗收**：3 線各一次真實 phase 2＋一次 /reject 清理證明。runbook 見 #66 留言 7516（⚠ 一項待裁決：/reject 那線要不要補 /approve，見 #68 留言 7520） |
| **ci#114** 🆕 | 承 #112／PR ci#113：`cleanup-orphan-candidate-tags.sh` 的**排除清單**也讀 open issue 值未驗封緘——但方向是「排除刪除」且有 1440 分年齡兜底 ⇒ **評估單**（先驗兜底論證再決定比照 #113 收斂或接受現狀），無人認領 |
| **ci#115** 🆕 | 承 #47／PR ci#116：workflow run: block 的 40 行（8 檔）SC2086 補引號＋把 lint 的 `WORKFLOW_RUN_SHELLCHECK_SEVERITY` 收緊到 style。🔴 **順序約束：須在 #66/#68 六線驗收之後或同批**（那 8 支裡 6 支是零真實驗證線，不疊未驗證 delta）；權威清單用指令重產勿抄 issue 裡那份，無人認領 |
| **ci#48** | `main` 無 branch protection——**查證完畢（1.27.0 源碼級，#48 留言 7522）：不會卡死單人自核**；建議設定組合＋逐步指引＋否定性驗證已備，**開關剩 Eugene 按**（🔴 唯一禁忌＝勾 block_admin_merge_override）。⚠ 附帶發現：CODEOWNERS 檔頭寫的「Require approval from Code Owners」欄位在 1.27.0 **不存在**＝過期指引，回寫檔頭時一併修正 |
| **charts#44** | `publish-charts.yaml` 的 `set -e` 把 Gitea Helm push 與 OCI push 綁死（transient 500 誤擋 OCI） |
| **#510** | `/woow:deploy-stg` 的 `addon_ref` 是誤導性 no-op——宣告了卻從未進 CI payload，實際永遠烤 develop |
| **#382** | `pvc-cleanup` CronJob 仍用 `bitnami/kubectl:latest`，要換 bitnamilegacy |
| **#472** | `Dockerfile.dev` 已失效——COPY 不存在的路徑，且 base 缺 6 個第三方相依 |
| **#563** | k8s dev sandbox 的 local/hostPath 模式在 Docker Desktop 下不可用；且 `create.sh` exit 0 掩蓋失敗 |
| **#731** 🆕 | 五支唯讀腳本（`list`/`logs`/`status`/`test`/`extend`）的 `--context` 是**可選** ⇒ #32 把「靜默打對」換成「靜默打錯」：`create --context docker-desktop` 後跑 `test <slug>` 不帶 flag，會在**同名的遠端 namespace** 上跑 `odoo -u --test-enable` ＋ rollout restart。刻意不做成強制（會讓既有使用者每條指令立刻壞掉），待裁決收斂方向 |
| **#722** 🆕 | onboard profile 的 `KUBE_CONTEXT` ＝ `docker-desktop`——不帶 `--context` 呼叫 `create.sh` 會靜默打到本機叢集。**與 #32 是相乘關係、非重複**：#32 修「腳本要強制 `--context`」，本單指出 required 驗證擋不住「有值但值是錯的叢集」；另實查 `kubectl config use-context` 在 **`deploy-stg.sh:161` 與 plugin 共用 lib `common.sh:437`（`require_kube_context()` 內）兩處都在** ⇒ #32 Part 2 兩條路徑皆未修；修 lib 那份可一次覆蓋所有 source 它的 sandbox scripts。⚠ 我一度誤判「plugin 版 create.sh 已無」——那是 grep 入口檔沒跟著 `source` 走（已在 #722 發更正留言） |
| **#714** 🆕 | **#694 的 filestore-hashdir-guard 刻意 fail-open，但四個 marker 只印在 initContainer stdout**（`FAILED-OPEN`／`SESSIONS-DEFERRED`／`OK`／`SKIPPED-FIRST-BOOT`）⇒ **沒人 grep 就與正常無法區分**。要把四狀態接上告警：前二者期望 0，`OK` 期望 1（**缺席即異常**），`SKIPPED-FIRST-BOOT` 只該首次開機出現。硬約束三條：🔴 四個 marker **互不為子字串**才能分開判（`grep -w` 救不了，`-` 不是 word 字元）；🔴 perm／group 是 kubelet fsGroup 每次掛載重寫的**恆綠假訊號**，不得當判準；🔴 log 比 pod 短命 ⇒ 載體要能在 pod 重建後留存。驗收**先注入紅燈證明告警真的會響**。範圍僅平台 paas-odoo，租戶側走 #702。Eugene 2026-08-13 已核准另開、不阻擋 #694 關單 |

> ⚠ **ci#66 的關閉條件**（08-07 複驗收窄，別憑「PR 都合了」就關）：`build-open-design-image.yml`／
> `build-open-design-console-image.yml`／`build-woowtech-ha-image.yml` 三條 gate-before-build 線**各要真跑過一次 phase 2**。
> 已 merge 的 PR #76／#82／#92 全部掛在 #66 底下（body 皆寫 `Refs #66`）。
> 舊看板「疑重複」段的 **ci#30 是同一起事故（approval issue #62／PR #628）的原始回報**，實際修法工程 100% 在 #66 進行、
> 且 #66 已涵蓋 #30 列的方向（含「調高 runner capacity」＝方案 B）⇒ **追蹤只看 #66**；ci#30 已於 08-12 裁決關單（見文末疑重複段）。

## A4 · 功能缺口／bug／UX（33 筆）

**壞掉的功能**

> 🔴 **交叉指標（2026-08-21 依判準第 1 層補）——#805 分流在 ③ 政策，但若裁成「SA 也是 bug」，危害就落在本段**：SA `action_provision` 與 #739 修法前的 SH 同形（非 sudo＋寫 `groups='base.group_system'` 的 `tunnel_token`），租戶若有可達入口則 provision 必 AccessError——是不是 #739 的 SA 版，**要先實查入口再裁**，排本段優先序時別漏看。(a) 段的兩處假交叉引用修正不用等裁決。

| Issue | 一句話 |
| --- | --- |
| **#769** ✅ **已修復並 merge**（PR #801，2026-08-21T02:07:53Z，merge commit `e356066b`；leader3）| operator PUT /api/tunnels/{id}/config 疑似 wire 契約破裂（operator 端裸 str ⇒ FastAPI 判 query param、Odoo client 送 JSON body ⇒ 疑必 422）——唯二呼叫者 SH _do_suspend／_do_resume，例外被吞成 state=error。🔴 **2026-08-20 已實跑坐實＝CONFIRMED 破裂**（leader3 團隊 verify-only 一輪，測於 `826f5f6d`/`.161`；leader3 獨立覆核 PASS，覆核走**源碼側**預測執行中 app 的觀測、非重跑其 recipe）：真起 operator（uvicorn，同 Dockerfile CMD、釘版依賴、helm 3.13.3），**由 repo 內真實 Odoo client 送出**⇒ `HTTP 422`，body 的 `loc` 是 `["query","hostname"]`／`["query","service_url"]`——**判別式在 body 不在數字**。陽性對照：同值改 query param ⇒ 400（無 CF 憑證）／200（CF 換本地 stub，且 fake CF 收到完整 ingress payload＝handler 真的跑完）。四格互有鑑別力（422／400·200／401 錯 key／404 錯路徑）。`Body` 在 `tunnels.py` 的 import 清單裡根本不存在，同 router 其他變更型端點全吃 Pydantic model ⇒ `/config` 是唯一例外。**呼叫者枚舉（資源側，7 種入口＋Gitea 9 repo clone 逐一掃）：除 SH `_do_suspend`／`_do_resume` 外無人**，且 production 入口只有 ir.cron（無按鈕／controller）⇒ 只在停權／復權當天發作、還被 `except Exception` 吞成 `state='error'`。旁證：stg operator 近 14 天 13,172 行 access log 裡 `/config` 只 1 筆、來源是平台 Odoo ⇒ **無向後相容包袱**。**leader3 裁決＝修 operator 側**（改 request body model；不做 body/query 雙接受）——🔴 rollout 順序天生安全：Odoo 現在就在送 body ⇒「新 operator＋現行 Odoo」會動、無 lock-step、無回歸窗口。<br>🔴 **修好會帶產品可見行為變更（尚未裁決、不在該 PR 範圍）**：suspend／resume 從未真的生效過 ⇒ 修好後下一次 cron 會**真的**把停權租戶的 tunnel 切成 503；且既有卡在 `state='error'` 的 SH 會不會自我收斂**未查證**，已要求第二輪查證後回報，屆時再決定要不要另開收斂單。發現於 #768 Phase 0（留言 7664）；改 operator 須本機真 K8s integration tests（第一輪未跑，已列入第二輪必辦）|
| **#648** | `priority:high`——CloudService 翻 `state='running'` **早於 Cloudflare DNS 真的可解析**，使用者看到綠燈卻拿 `ERR_NAME_NOT_RESOLVED`：`_is_release_ready`（`cloud_service.py:2089-2186`）全文無 DNS 判定、`create_route` 的 DNS 失敗被 `cloudflare.py:699-704` 就地吞掉、release schema 也未鏡射 #484 的 `dns_failed` 旗標。單裡三個方向（補 `dns_failed`／`_is_release_ready` 加 `route_reachable` 子判定／前端軟警告）**自註待討論、非定案**。⚠ 行號是開單當下的，動手前重新定位 |
| **ar#23** | LLM tool-call 一律傳空 kwargs：**skill 執行時 input 參數整個遺失**（stg 1.3.0 / MiniMax-M2.7） |
| **#155** | `SKILL_BUNDLE_TOKEN` 1h TTL：pod 活超過 1 小時後 skill-bundle fetch **必 401** |
| **#316** | stg 控制面 MCP tool sync 疑被 baseline default-deny 擋（`state=error`／`tool_count=0`） |
| **#207** | operator mcp sidecar route 的 deploy/delete 命名不對稱 → 有 sidecar 的 CloudService 漏 mcp CF route |
| **#388** | `agent_session` spawning→active 未轉：spawn webhook 200 但 state 停在 spawning |
| **#562** | postgresql／redis template 重新上架前必須補 image override（Bitnami 搬遷後預設只剩 latest） |
| **#655** | paas-operator `helm.py` 在 `create_namespace` 與 #278 A1 整組 overwrite **兩處**把 ResourceQuota 的 `persistentvolumeclaims` 寫死 `"10"`，沒比照 cpu/mem/storage 走 Odoo 端 `_compute_ns_quota()` 動態算——已知案例：HAOS 每台佔 3 顆 PVC，**同 workspace 開 4 台以上就撞死上限**。⚠ K8s hard quota **沒有「0＝不限制」語意**（0 是禁止建立），要不限只能省略該 key |
| **#739** ✅ **已修復並 merge**（PR #797，2026-08-21T01:54:26Z，merge commit `84afb5d8`；Eugene 裁「是 bug，照修」；leader3）| **租戶經 `/woow` 的 Smart Home provision 從來沒有成功過**（🔴 既有 base 缺陷，#524 實作時發現、實測復現於 base 碼）。`tunnel_token` 自 model 誕生（PR #115）就帶 `groups='base.group_system'`，而 `_provision_smarthome`（`controllers/smart_home.py:521`）用**非 sudo** 的租戶 recordset 呼叫 `action_provision()`，其 `self.write(tunnel_vals)` 含 tunnel_token ⇒ 租戶必撞 `AccessError`，記錄卡 `state='provisioning'`。既有測試沒抓到＝全在 `TransactionCase`（superuser env，欄位級 groups 不生效）。修法：provision 寫入點走 sudo（憑證本就平台鑄造），配 **HttpCase** 回歸測試。⚠ 行號為開單當下 |
| 📕 **#716** 已關（2026-08-19 PR #775 merged by eugene；附錄 G1f 探針弱點本輪未修→**#777** 承接；② 的敘述有一半被實測推翻，詳 #716 留言）| **#705 守衛的三個殘留缺口（post-merge 複驗）**：① `cloud_service_binding_guard.py:190`（⚠ 行號為開單當下）`create` 對整批 `vals_list` 取**聯集**決定要比對哪些欄位，卻把結果**套到整批 records** ⇒ 批次建兩筆 task、只有第一筆明寫該欄位時，**第二筆連帶被送授權，合法操作被誤擋**（F2 批次路徑復發）；② 反向候選集讀 `project_ids.ids` **預設濾掉 archived project** ⇒ 封存綁定被解掉卻沒進候選集（僅影響移除方向，非新越權）；③ docstring 稱 `(0,0,vals)` 靠 project mixin 兜底，**實測從未被呼叫，理由是假的**——同一個 guard 的**第六輪同型態假宣稱**。三項皆已附失敗測試與修法方向；②③ 內文自陳「不構成新越權、今天不可利用」|

**資料一致性／並發**

| Issue | 一句話 |
| --- | --- |
| **#549** | reach reconciler 的 cron 路徑同樣 lock-then-recount（#518 機制第四站，且是原型） |
| **#533** | `api_service_skip_trial` 吞掉並發類 DB 例外——`SerializationFailure` 被 `except Exception` 收走，retrying 永不重試 |
| **#525** | paas.py 吞例外→靜默 commit 同構縫（`priority:high`）：3 處確認該修、5 處形狀同構因果不同 |
| **#363** | reset saga 並發：client 逾時後重按會與 in-flight saga 競態，rollback 可能覆蓋較新密碼 |
| **#342** | change plan 後 CS 規格 re-apply（#332 殘段） |
| **#732** 🆕 | **owner 把自己那列改成 `admin` ⇒ workspace 永久 0 個 owner、租戶側不可回復**。三道既有守衛全都涵蓋不到這條路徑：`_check_owner_row_protected` 的 docstring 明寫只擋 **non-owner caller**（呼叫者就是 owner ⇒ 直接通過）；「移除最後一個 owner」掛在 `unlink()`（這是 `write()`）；`_check_owner_count` 只在**新值是 owner** 時檢查（新值是 admin ⇒ 整道不觸發）。⚠ 不是沒人想過——#556 的 landing doc 自己就寫著「`_check_owner_count` 對 `role='admin'` 完全不設限」，而補出來的守衛防的是 **transient** 0-owner（降級後自我升級那條鏈），漏的是**永久** 0-owner。後果兩層：① `ASSIGNABLE_ROLES_BY_ROLE['admin']` 不含 owner ⇒ **只有 `group_system` 救得回來**，每次發生都要人工介入；② 只動了 access 列的 role，`workspace.owner_id` 不會跟著變，而 `paas.py::_delete_workspace` 的授權判準用的正是 `owner_id` ⇒ 兩個真相來源從此不一致。**不需要惡意**：owner 想自己降級（交接到一半、誤操作）就會踩到，且沒有任何錯誤訊息。⚠ 第②點的下游我只查到 `_delete_workspace` 一處，**沒做完整的資源側枚舉** |
| **#702** 🆕 | **承 #694：租戶 `svc-*-odoo` 的 root-owned 資料目錄偵測**（Eugene 已裁決**先偵測不預防**）。租戶與平台同用 Bitnami chart、同樣 `runAsUser: 0`，但**租戶沒有 initContainer** ⇒ 唯一 root 路徑是人工 `kubectl exec`，發生後**無守衛收斂**。本單只建唯讀掃描器，三條判準約束：🔴 只認 **owner**（mode／group 被 fsGroup 每次掛載強制覆寫、**恆綠**，拿來判斷就是假綠）；🔴 逐租戶讀 `/proc/1/status` 取**真實 uid** 再掃（**不可假設 uid=999**）；🔴 範圍含 filestore **與 `sessions/` 的 4096 個前綴目錄**（rename 失敗靜默吞掉，症狀是使用者隨機被登出且零 log）。驗收**先注入 `root:root 2775`（不是 2755）紅燈證明抓得到**，全程唯讀 |
| **#740** 🆕 | **租戶 delete 回失敗、但 Cloudflare tunnel 已先被刪、`tunnel_id` 清空後無法重試**（🔴 既有 base 缺陷）。`unlink()` 的 `_cleanup_tunnel()`（送 `delete_tunnel` + 清 tunnel_id）在 `super().unlink()` 的 ACL 檢查**之前**，而 ACL `unlink=0` ⇒ 端點回失敗、DB 列還在、tunnel 已死、tunnel_id 已清 ⇒ **沒有東西能驅動重試**（重試分支與 reconciler 反查鍵都判 `if self.tunnel_id:`）。⚠ 方向與 `cf-ns-orphan-no-account-gc` 相反（DB 列活、tunnel 死），既有 orphan guard 一條都不會響。修法：ACL 通過後才送外部呼叫，或把「外部已刪未落 DB」做成可對帳狀態（別打壞既有 fail-loud guard）|
| **#760** 🆕 | CS Access policy `create()` 打 **2 次 operator sync**（policy＋rule model 各觸發一次），CF 上多建一條 reusable policy 再刪——update 路徑的 `sa_rule_replace` 抑制未涵蓋 create，且作者明說**不可直接沿用**（該 context 同時抑制「≥1 include」檢查，語意衝突），修法待定。除耗 CF 共用帳號 rate limit，**兩次 sync 之間若 sync #2 失敗 ⇒ 永久 CF 孤兒 policy**（`orphaned_policy_ids` warning 不涵蓋這個中間態）。stg T5 e2e 發現、非 blocker（#747 已關）；A4／A5 兩可，孤兒窗口是歸 A4 的理由 |

**migration 地雷（舊 DB 還原會炸）**

| Issue | 一句話 |
| --- | --- |
| **#543** | `18.0.1.0.39` migration 撞 #352 bulk-grant 守衛——從舊備份還原的 DB 會炸在 `.39` |
| **#575** | `.37` migration 的 fan-out 對 pre-`.39` DB 撞 `_check_at_least_one_include`（同家族第二顆） |

**UX／前端**

| Issue | 一句話 |
| --- | --- |
| **#758** 🆕 | ServiceDetailPage 只在 `onMounted` 載資料，SPA 內從服務 A 切到 B（同 component 不重 mount）**整頁沿用 A 的舊資料**——影響所有頁籤、非 Access 專屬（#746／PR #757 抽元件時發現的既有缺陷）。限同 workspace 自己的服務，無資安面。修法方向已給（`onWillUpdateProps` 或 serviceId effect），要順檢 SA／workspace detail page 是否同形；驗收用 playwright A→B 切換對照 DB 值 |
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
| 📕 **#717 已關**（2026-08-16，PR #759；chart 0.2.0 已落地、stg 已驗收，明細見 issue 留言 **7538**／**7582**） | ⚠ 尾巴未收：**prod rollout 未做**；prod seed 吃到 0.2.0 的前提＝某次含 #759 的發版 rollout ＋ DB `-u` 重套——**08-16 的 v146 image（20260816.0855）是否已含 #759 未查證，用前先驗 image 內 seed 版本**。rollout 盤點結論＝無其他待升租戶（k8s ns 與 DB 兩路互驗）。風險連動已更新進 **#718** 列 |

## A5 · 技術債／重構（9 筆，有餘力再做）

| Issue | 一句話 |
| --- | --- |
| **#415** | 收斂 HA provision 與 marketplace **兩套平行的 CloudService 建立邏輯**（已漏鏡射兩次） |
| **#509** | PR#505 review follow-ups——G4 EnvVar replace 分支盲區＋H1 gate 獨立守線檔＋H2/H3 文字債 |
| **#668** | #657／PR#666 兩輪獨立覆核衍生三項，**皆已實測確認非現行可利用洞**（刻意不在 #666 修）：① grantor lookup 用 `limit=1` + `_order='role desc'` 吃的是**字串字典序不是權限高低**，根因已被 #666 的 user_id anchor 擋住但查法仍依賴會鬆動的前提（`workspace_access` 有同型查法要一併處理）；② X8 o2m 攻擊測試的**可達性陽性對照與攻擊斷言分離**，日後動壞對照會讓四條攻擊測試靜默退化成零鑑別力的綠燈（建議搬進 helper 讓每條攻擊自帶可達性斷言）；③ `invited_by_uid` 可被 context 偽造（純稽核欄位、不被任何 rule／守衛使用）⇒ **事故追查時不可信** |
| **#84** | skill `is_latest` drift fix + e2e（PR #81, F-SK-1~9） |
| **#50** | 全 repo API URL pattern 統一到 cloud_service nesting |
| **#25** | 統一 SH/CS controller 的 role check pattern（抽 `_require_admin_role`） |
| **#750** 🆕 | #743／PR #749（quota 0=unlimited 語意統一）前置驗證揪出的兩項收尾：① `api_sa_change_plan` 對 template-less SA 整段跳過 plan-family 白名單 ② `_get_display_specs` 把 0 顯示成字面「0 policies」與新語意矛盾。**兩者觸發族群 08-15 查皆空集合**（無 template-less SA、無 0-quota SA plan）⇒ 作者自陳低優先掛票，不擋 #521 主線、非 #743 收尾條件。⚠ 族群哪天非空，① 就升級成 A1 等級，屆時重歸類重排 |
| **#319** 🔄 | **（2026-08-18 依判準第 2 層自 ③ 架構搬入：body 已有「## 決策」段＝要做什麼已定）** Longhorn SC 策略：default SC 維持 `longhorn`(Retain)、租戶資料 PVC 一律於 chart values 明示 `storageClassName: longhorn`。剩餘＝Released PV 清理 SOP ＋ backup target 建置。🔴 **執行時的硬約束（原單自帶歸屬判定表）**：48 顆 Released PV 只有 1 顆屬 `paas-ws-*`、6 顆 `dev-sandbox-*`、2 顆 `paas-system`，**其餘 ~39 顆屬其他部署，絕不動**；且 default SC 是**叢集級**設定，動之前要與叢集運維協調 |
| **#396** 🔄 | **（2026-08-18 依判準第 2 層自 ③ 架構搬入：原文「## 目標（長期最佳解 / 方案 B）」已給具體技術方向）** orphan PVC GC 搬進 paas-operator，去除 chart cronjob 的 shell-image 依賴。⚠ 原單自陳「優先度：非緊急（#45 已止血）。屬技術債／架構優化」——歸 A5 正是照這句 |

---

# ③ 需討論類（26 筆）— 需求／策略未定，別自行開工

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
| **#773** 🆕（2026-08-18 leader2 開） | **CS 降級到無 AC seat 的 plan 後，既有 access policy 與 CF 上的 Access app／policy 繼續生效**——change-plan 全程不嘗試任何收斂（`_create_and_confirm_subscription` 只關舊訂閱＋算 refund＋建新訂閱，函式範圍內 `access|policy|sync|operator|cloudflare|cf_` **0 命中**）。🔴 **本單最重要的一點：既有 CF 孤兒偵測對這一類結構性失明**——`reconciler.py:88` 判孤兒的依據是「DB 端還有沒有 record 指向該 cf id」，降級後 policy row 與 `cf_policy_id` 原封不動 ⇒ 判「有主」，永遠不會上孤兒候選。**與 #528 方向相反**（#528＝DB 刪了 CF 還在 ⇒ 抓得到；本單＝兩集都有 ⇒ 依定義抓不到），兩者不能共用同一套偵測、也不能共用同一個 fix。⚠ **已有載體的部分不是本單新增**：「change-plan 不觸發下游 re-apply」已由 **F-368-1**／#342 定案（那是**規格面**）、「`ac_max_access_seats` 零 enforcement」已記於 #521 與 352 landing；本單新增的是 **entitlement 面** ＋ #744 的 lifecycle teardown 枚舉只有「刪除」「停權」**缺「降級」第三項**。要決：降級要不要收斂／收斂到什麼狀態（unlink／停用保留／生效到週期末——⚠ 第三種才與 F-368-1 記錄的既有 UX 承諾「下個計費週期生效」一致）／CF 同步走宣告式全推或最小拆除／是否與 #381 同輪拍板。⚠ 未查證：後台 raw ORM 改 `sh_subscription_plan_id`（無 `write()` override 攔截，結構上成立但後台 view 是否曝露未實測）、CF 現況有無真實案例、SH 側是否同構（共用同一 mixin，高度可能）。<br>⚠ **歸類兩可**：現列 ③（修法界線未定），但危害是**付費界線被繞過且零訊號** ⇒ 若判準改按危害排，應進 ②A2 —— 與 #710／#704 同一種兩可，可一併裁決 |
| **#805** 🆕（2026-08-21 開，承 #739 裁決） | **SA `action_provision` 的 fail-closed 是 bug 還是 intended？** SA 與修法前的 SH 同形（非 sudo 呼叫、寫 `groups='base.group_system'` 的 `tunnel_token`）；#739 的裁決（「租戶端點從來不會成功 ⇒ 是 bug，照修」，#739 留言 8155）**只涵蓋 SH、未涵蓋 SA**。若 SA 也是 bug ⇒ 同型 sudo 修法＋`test_provision_token_write_fails_closed_for_non_system_owner` 改語意；若 SA 刻意不同（例如 provision 只由平台流程驅動、無租戶入口）⇒ 「為什麼不同」必須寫進 docstring。⚠ 裁決前先**實查 SA provision 有沒有租戶可達入口**（#739 的結論來自真打 HTTP，不是讀碼），別直接套 SH 結論。<br>**(a) 段不用裁、無條件可做**：`security_access.py` docstring「mirroring smart_home.action_provision exactly」＋`test_security_access.py` docstring「鏡像 smart_home 的非 sudo provision 行為」兩處在 #797 merge 後已成**假的事實斷言**、且無任何 gate 會紅——只改敘述、不動 assert／測試方法名（動到就是溢出到 (b)）；判準＝改前 grep 兩種寫法非 0（陽性對照）、改後 0。開單時「檔被 #800／#802 持有」的不併修理由**已失效**（兩張 08-21 皆 merge、零 open PR），(a) 現在就能開。🔴 交叉指標已補在 ②A4「壞掉的功能」段 |

## 架構方向（做法有兩條以上路）

| Issue | 要 Eugene 決什麼 |
| --- | --- |
| **#578** 🔄 | **要決對外 API 契約要不要改**（2026-08-18 依判準第 1 層自 ②A4 搬入）：rollback 是第六條帳本失準路徑且方向相反（帳本比叢集**新**，租戶看到的版本號已不存在於叢集），而 `#552` 層 ④ 的 fail-closed 只擋空版號、**擋不到「有值但值是錯的」**。修它必須先改 operator `rollback_release` 的 `response_model`（`Dict[str,str]` → 帶版本資訊的型別）——issue 原文逐字「那是**設計決策，不是實作細節**」「這幾點是設計決策，**不由實作者代決**」。三個待決點：既有呼叫端向後相容／舊版 operator 在線時的容忍/`Dict[str,str]` 當初是否有意為之（未查證）。⚠ 同函式 `paas.py:1470` 另有既有缺陷（suspended 處理），不同件事但同路徑，動它可一併評估 |
| **ar#33** 🔄 | **要決「修掉還是明確接受並記錄」**（2026-08-18 依判準第 1 層自 ②A1 搬入）：sidecar 內 bash 與 bridge daemon **同 uid 10002**，bash 可對 PID1 送 SIGTERM 觸發 drain；另 PID namespace 隔離**不涵蓋 network** ⇒ sidecar 可連 agent 的 loopback 埠。issue 自陳嚴重度低（**自傷型**，pod:workspace=1:1、跨租戶無影響、liveness 會重啟），但「可重複觸發代表租戶可以把自己的 bash 打成不穩定，**值得有解或明確接受並記錄**」⇒ 要的是一句裁決不是修法選型。⚠ loopback 那半是**前瞻約束**（今天 agent 未在 loopback 開敏感埠，但不可假設同 pod network 有隔離）|
| **#770** 🆕 | SH 第二條 route（使用者自建對外服務）——#768 A 案的後續啟用票：provision 改 bare tunnel＋sync_tunnel_routes、#484 dns_failed 失敗模型重設計、dns_record_id 持久化、HA component 跨 repo lock-step、route CRUD、quota 語意拍板（sa_max_routes 0=none 與 sa_max_access_policies 0=unlimited 相反，SH 要不要新開 sm_max_routes）。前置 #768 已於 08-17 關單（PR #771 merge、route model 已落地），不再阻塞；需求時間點未定 |
| **#691** 🆕 | 帶 migration 的 PR 在 manifest 版號競賽裡**結構上必輸**：版號是全 repo 共用的單調遞增純量，正確性條件「X 必須大於**合併當下任何存活環境已到達過的最大版號**」在寫 PR 當下無法確定。輸掉的後果**不對稱且靜默**——已升級環境 migration 不執行（無錯誤、無 log）、fresh install 全綠、CI 全綠，於是「本機測過＋CI 綠＋fresh install 驗過」全成立而**線上沒拿到修復**（#675 的內容是授權修復，等於安全洞在升級路徑上原樣留著）。同一張 PR（#675）**連踩三次**（126→127→129→131），且別的 PR 主動「跳號讓路」**無效**——門檻是環境到達過的號碼，不是號碼被誰用掉。四個方向（①合併瞬間原子重命名＋bump ②migration 改用狀態判斷取代版號視窗 ③push-time gate、但仍須配合併前複驗 ④限制純 manifest bump PR）**明確待討論未拍板**；#675 當下只採「盡快 merge 壓縮競賽窗口」的緩解 |
| **#681** 🆕 | issue tracker 與 `docs/tracking/`（followups／pitfalls／landings）是**兩個互不相通的查重檢索面**——本次代價是花一天重新得出 **F-DT-4（2026-06-02）** 早就寫過、措辭幾乎相同的結論，中間還開了前提錯誤的 #665 並實作 478 行 gate。🔴 **核心難題：查重是「動手前」的行為，沒有明確可掛 hook 的事件，結構上看不到能做成 gate 的路徑**（pattern-guard 攔編輯、repo-lint 攔 PR，都有觸發物；查重沒有）。四個提案（查重工具／為 followups 建索引／反向登記成 issue／包成 skill 降低使用成本）各有利弊，單裡**明確不預先選定實作方向**，留給後續動手者依成本效益取捨 ⇒ 要決的是「接受它只能做到提高可發現性」還是投資做工具。✅ 開單前已做**雙面自我查重**（Gitea `state=all` 全文搜 ＋ `git grep docs/tracking/`，含陽性對照）並讀過 `doc_citation_index.py` 確認範圍不重疊 |
| **#383** | F-ENV-8 admission 層補強（Kyverno/OPA）當 RBAC backstop——要不要引入 |
| **#276** | [Spike] 評估「一 workspace 一 namespace」合併（現況 CS per-workspace + agent-runtime per-project 並存） |
| **#385** | CF tunnel 累積清理評估——reconcile 報 79 tunnel＋10 access_app＋7 policy 孤兒候選，清哪些 |
| **#710** 🆕 | **要決「誰能刪 project」的界線**——`project.project` 唯一那條 ir.rule `project_rule_workspace_member`（`ir_rules.xml:34`，⚠ 行號為開單當下）的 `perm_unlink=False` 語意是「**此 rule 不參與 unlink 檢查**」而**不是**「禁止 unlink」；而 `ir.model.access.csv:41` 給 `base.group_user` 的是全開的 `1,1,1,1`（含 unlink），**沒有其他 rule 覆蓋** ⇒ **guest 可直接 `unlink` project**，達成與 #705 的 R2/R3 守衛（掛在 write／create，管不到 unlink）所擋的同一結果，而且更徹底（project 連同其 cloud_service 關聯整個從 workspace 消失，admin 也看不到）。範圍限同 workspace 內（非成員 guest 對受害 workspace 的 project 連 read 都沒有）。兩條修法未拍板：補一條收斂到 owner/admin 的 unlink ir.rule ／ 收掉 ACL 的 unlink 位改走明確路徑；且牽動「既有孤兒 project 還刪不刪得掉」。🔴 **這張是今天就能被利用的洞，只是修法界線未定**，別因為它在 ③ 就當它不急 |
| **#704** 🆕 | **要決 `use_agent_runtime` 該用哪種擋法**——該欄位沒有 `groups=`，攻擊者可經 `/web/dataset/call_kw` 直接 `write` 繞過 `ai_assistant.py::_create_project` 那句「NEVER read from user params」的 controller 層保證（**ORM 層完全沒擋**），與 #519／#556／#658／#676 **同形態：controller 有閘、ORM 沒有**。#698 修完 model 層 `_assert_can_bind_services` 後跨租戶那條腿已斷，剩下的是**租戶自行開啟 agent runtime（會實際佈署 pod、吃資源）**的權益／計費問題。三個選項未拍板：① 加 `groups="base.group_system"` 最直覺但**恐連讀取一起擋壞 /woow 前台**（動手前須先查誰在讀這個欄位）；② 改走 model 層 `write()` guard，只擋寫不擋讀，形狀與 #676／#698 一致；③ 要不要為整類「controller 閘、ORM 未擋」的欄位建一條**通用 gate**，避免修一個冒下一個。🔴 同樣是今天可被利用、只是修法未定 |
| **#703** 🆕 | **要決 operator 該不該自己也驗歸屬**——`extra/paas-operator/src/api/agent_runtimes.py` 的 `POST /{hash}/sync-workspaces` 對傳入的 `project_workspace_ids` **零歸屬驗證**，直接拿去渲染兩條腿：leg-1 `helm upgrade` 改 agent pod NetworkPolicy 的 `egressWorkspaceIds`、leg-2 `reconcile_ns_reach_labels` 在 agent namespace 貼 `woow.tw/reach-ws-<W>` label（供 CloudService ingress 的 namespaceSelector 比對）。Odoo 是唯一 ground truth，而 #698 已證實 Odoo 端可被任一 internal user 騙 ⇒ 問題是**要不要縱深防禦**。三條路徑未拍板：反查 Odoo（多一次往返，**且 Odoo 正是被騙的那端**）／operator 自維護對照表／Odoo 簽章＋operator 驗簽。內文還自問「這是否只是把同一個信任關係換個地方寫」。⚠ 動 `extra/paas-operator/` **合併前必須本機跑真 K8s integration tests**（無 CI runner） |
| **#696** 🆕 | **要決 backend view 的 `groups` 該不該比照 security_access**——`cloud_service_views.xml` 的 `action_cloud_service`（⚠:109）與 `menu_cloud_services`（⚠:124）皆無 `groups`，父選單 `menu_cloud`（`menu.xml:12`）也無；對照組 `security_access_views.xml:116-121` 的 menuitem 已有 `groups="base.group_system"` ⇒ **同 repo 兩種處置不一致**。PR #695 已把 ir.rule 寫入收窄到 owner/admin，所以 guest/user 按 `action_redeploy` 會拿到 `AccessError`，**但按鈕仍可見可按**（是 UX／縱深問題，不是現行洞）。三個選項未拍板：要不要加 `groups` ／ 只動 cloud_service 還是連 `menu_cloud` 一起收 ／ 要不要固化一條 gate 強制新 backend menuitem 顯式宣告 `groups`（或顯式豁免）。⚠ 單裡那份「views/ 下完全無 `groups=` 的檔案」清單是**檔案級粗篩**，作者自陳不等於都缺保護（可能靠 ACL/ir.rule 或父選單擋著）——**逐一查證後才能定案，不可照抄當待辦** |
| **#721** 🆕 | **要決要不要開放租戶自帶 Odoo addon**（承 #713／ci#100 的授權擋門；**唯讀 spike、不含實作**）。事實基線：`base_import_module` 解壓階段用副檔名白名單（僅 `.xml`／`.csv`／`.sql`）**擋死 `.py`**，全模組查無可放寬的 config ⇒ 屬架構限制。但租戶**已經**有 `base.group_system`＋`base.group_erp_manager`，對 `ir.actions.server`(state='code') 有完整 CRUD，而 `_run_action_code_multi` → `safe_eval(mode='exec')` 傳進去的 `env` **是本尊不是受限代理** ⇒ **資料層今天就等同 superuser**（`sudo()` 繞 ACL/ir.rule、任意 SQL），開放自灌對資料層的增量 ≈ 0。**真正的增量在 OS／程序層**：容器以 `uid=0` root 跑、NetworkPolicy egress 是 `{}`（等同不限制），而 addon 的 `.py` 由 registry 直接 import、**完全不經 safe_eval**。⚠ §5 列了 7 項未查證前提，最關鍵的一項（🔴）是 **`safe_eval` 有沒有非 dunder 的方法鏈可逃逸到 `subprocess`／`socket`**——若可行，**整份風險判定會翻掉**。§4 明確標注：把 2026-05-03 會議「Skill 不開放使用者上傳」的既有決議外推到 Odoo addon，**是待決事項、不是已決** |

## 範圍未明 / 卡別人

| Issue | 卡在哪 |
| --- | --- |
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

## 甲 · 「測試到底有沒有在跑」的完備性（5 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#467** | 2 個測試檔未被 `tests/__init__.py` 匯入 → 15 條從未執行（`priority:high`） | **S**（僅剩守線） |
| **#544** | 另 47 條從未執行——整檔只有 `unittest.TestCase`，被 tag 過濾靜默排除（與 #467 不同機制、修法不同） | **M** |
| **#684** 🛠 **Eugene 2026-08-21 裁：做**（#684＋#544 合單、gate 必須 class 粒度；裁決＋規格見 #684 留言 8214；待 Eugene 重開 leader 後派，認領照協定兩處登記） | 同一機制的再發現＋實測：3 檔／9 class／**51 條**從未執行，含跨 repo MCP subdomain golden case 與 delete gate；**2026-08-21 基準已變 4 檔／10 class（#801 的 test_tunnel_config_wire_769 基底 unittest.TestCase，見 #806）** | **M** |
| **#806** 🆕（2026-08-21 開；PR #801 帶入，leader3 自陳交付者）**⚠ 待 Eugene 裁：做不做／誰做**（leader3 等一句話；歸本段是因為它正是 #684 gate 要抓的那一格的**現行實例**，本身是實作項不是 gate） | `src/tests/test_tunnel_config_wire_769.py` 的 `TestConfigureTunnelWireShape` 基底是 plain `unittest.TestCase`（全檔無 Odoo TestCase）⇒ `TagsSelector.check()` 靜默排除，**#769 腿 2 的 4 條從 merge 那刻起一條都沒跑過**；機制 A（未匯入）**不成立**，只驗 import 的查法會說它正常。影響有界：腿 1（pytest）與腿 3（repo-lint）活著、契約仍被靜態守著；死的是腿 2 唯一不可替代的斷言（`PreparedRequest` 實際序列化）。修法＝基底換 `TransactionCase`（`_CapturingAdapter` 不動）。🔴 驗收：**RED 先行**——指名 `--test-tags /woow_paas_platform:TestConfigureTunnelWireShape` 證明現在選不到 → 換基底後 4 條可見且**真的綠**（從未跑過，不得假設綠；紅了先判測試過時或程式錯，不刪測試）→ 全 repo 逐 class 重掃確認被排除集合恰好少這一格（寫推導方式不抄數字） | **S** |
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

## 丙 · 跨 repo 的 CI 缺口（1 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **cd#76** | operator chart 的 prod/stg 版本漂移無任何檢查——`0.1.17` vs `0.1.19` 的 HAOS RBAC 靠人工實查才抓到 | **S**（跨 repo） |

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
| **#582** | 四張安全修法聯集全套跑出的四條測試方法陷阱（`-i --test-enable` 測到的是**安裝過程中**的狀態、DB 直查早於對照實驗定位問題、紅燈時「收集總數」是判斷問題層級的工具、排他性宣稱要配哨兵）——待 promote 進 `docs/tracking/pitfalls/` | 開單當下發版 tag 未打在 `73a022ac`，作者刻意不動 develop。tag 早已過去 ⇒ **阻擋理由已消失，這張現在純粹是沒人去搬** |
| **#508** | PR#507 合併後的註解漂移（3 處 docstring/TODO 與實際行為互相打臉：載體信 `auto_delete` 語意、threshold vs 7 天視窗、已完成的 TODO）＋2 條維運觀察（OBS-1 實際上限是 11 封不是 7／OBS-2 `mail.mail` 永久留存無清理 cron） | 註解與同檔另一條測試**直接矛盾**（`:261` 說「載體被刪＝送出成功」vs `:294` 斷言載體不會被 auto_delete 清掉）——這正是「文件裡的可照抄敘述勝過正文警告」會反噬的形狀 |
| **#392** | 手動禮品卡換點 SOP 文件化——07-10 定價會 Q6 拍板「試用＝手動禮品卡換點、不改系統」，但發卡審核／換點操作／記帳慣例／邊界（金額上限、有效期）只存在於拍板記錄 | 已有同性質範本可抄（`docs/guides/smart-home-half-year-gift-points-admin-sop.md`）。⚠ 這張是**營運會用到的**，凍結期間真的有人要發卡時會立刻變成阻擋項 |
| **charts#43** | `charts/open-design/Chart.yaml` 的 `description` 仍寫 C 案前 auth 語意（把 `OD_API_TOKEN` 說成租戶 auth token，實際租戶憑證是 auth-proxy 的 HTTP basic-auth） | **僅 metadata 字串 stale**，`templates/`＋`values.yaml` 契約已正確且經 np-audit #42 複驗 PASS ⇒ 不影響部署行為。issue 自陳「下次 chart bump 順手改、不急、不擋主線」 |
| **#421** | 2026-07-23 進度同步會議（KubeVirt×HAOS／Headscale VPN）待辦分類與佐證分析 | ⚠ **混合單**：內含已答覆的查證結論、可執行項、需查資料項、需再討論項。kubelet max-pods 調升屬**叢集級**，平台側只出建議值、執行交叉 `woow-machines-operator` |
| **#699** 🆕 | 兩處 docstring 宣稱與實際機制不符、**修法方向已定、不改行為**：① `cloud_service.py:1821`（⚠ 行號為開單當下）宣稱「`extra_vals` 其他欄位不提權、ir.rule 照常生效」，但 `_model_init_n8n`（同檔 `:2856`，經 `_cron_check_deploying_services` 呼叫）以 cron／superuser 身分執行（`ir_cron.user_id`=1），**superuser 下 ir.rule 本就不生效** ⇒ 該句在這條路徑上是假的（非租戶可利用面，純文件失準）；② `test_workspace_authz_658.py:601-605` 的 X13 docstring 宣稱「guest 直接寫 cloud_service 的洞尚未補」，但 #676／PR #695（`5608beb0`）已補上，現由 `test_cloud_service_authz_676.py` 守著 ⇒ 已過時 | 承 #676／PR #695 的 post-merge 獨立覆核。⚠ 同類「docstring 機制宣稱過期」**已連踩四輪**；修法要求寫成「**指得出證據的範圍限定敘述**」，指不出來就刪——**不要改寫成新的猜測**（#675 就是修正動作本身鑄出新假宣稱的實錄） |
| **#701** 🆕（2026-08-12 leader3 開） | `tea` 寫入坑知識**雙載體且 upstream 在錯邊**：memory 那份（58,236 B、08-12）持續在長，`docs/guides/development/tea-cli-write-gotchas.md`（38,631 B）凍在 08-05 遷移當天 ⇒ **docs 版是嚴格子集**（memory 獨有 343 行、docs 獨有 3 行），照 `docs/` 走的人拿到落後一週的內容而無任何示警 | 對照組：`docs/` 有 103 篇標注自 memory 遷移，仍留全文副本的**只有這 1 篇** ⇒ 遷移紀律整體沒問題，這是唯一例外。⚠ 提醒「耐久 ≠ 新鮮」 |
| **#741** 🆕 | `docs/decisions/patterns/paas-workspace-member-acl-tightening.md:35-45` 逐字貼著 **#676 已退役**的 `cloud_service_rule_workspace_member`（無 role、四 perm 全 True）當「如何收緊」範例 | doc 最後改動 `e8b977e3`(08-06) 早於 #676(08-12) 六天。**不是 #547 重複**（#547 碰的是同 doc :59 dual-leg domain、且 08-05 關單在退役之前）。🔴 危害＝該 doc 被 **pattern-guard** 列為 `acl-security` 必讀 ⇒ 每個改 ACL 的人被導到一份可照抄、已知有洞的範本 |

⚠ **#508 與 #421 的歸類兩可**，見下方裁決表。

## ⚠ 歸類待 Eugene 裁決（12 張待裁決 ＋ 3 張已裁決）

跨 ①a／①b／② 三段，本表是「拆分主筆判斷不了、需要你一句話定案」的清單。

> ✅ **2026-08-18 Eugene 裁決（#710／#704／#773 一次定案，並升格為通用判準）**：
> **① 分類按「修法是否已定」排，不按危害排** ⇒ 修法未定就留 ③；
> **② 但要在危害所屬的 ② 分段補 🔴 交叉指標**（已補：A1 段 #710／#704、A2 段 #773）。
> 規則全文已寫進開頭「分類原則」段的補充判準——**那裡是權威載體，本段只是裁決紀錄**。
>
> ✅ **② 存量追溯：2026-08-18 已執行完一輪（leader2 ＋ ccsx agent 六批平行掃）。**
> **範圍**：看板當日快照的 ②A0–A5 與 ③ 全段共 109 張（②84＋③25，扣除已裁決的三張樣板與兩張已關單）；
> 逐張 `tea api` 重抓、**109 張全部仍 open**。①a／①b／⊘ 三段不在範圍（不涉及「可被利用」）。
>
> **結果（判準第 2 層加上之後）**：
> - **②→③ 2 張**：**#578**（要決對外 API 契約要不要改）、**ar#33**（要決修掉還是明確接受風險）。兩張都已搬入 ③ 架構方向段。
> - **③→② 4 張**：**#319**／**#396** → A5、**#584**／**cd#85** → A1。四張的拍板紀錄都找得到逐字出處（#584 與 cd#85 的**在留言裡、不在 body**）。
> - **補交叉指標 4 張**：#152／#711／#385／#721（維持 ③，已在 A1 段補上）。
> - **兩可 5 張，仍待裁決**：**#567**（(a) 判嚴判鬆會導出相反結論）、**#525**（混合單，A 段修法已定、B1/B2 未定 ⇒ 要決拆不拆）、
>   **#750**（(a)=否只因觸發族群現查為空 ⇒ 要確認 template-less SA 租戶能否自行產生）、
>   **#383**（Eugene 指過方向但「該指示未正式送出」，算不算已拍板）、**#412**（三項中一項已完成兩項待拍板 ⇒ 建議拆單）。
>
> 🔴 **這一輪最貴的教訓，寫給下一個做同類盤點的人**：
> 第一版判準只問「修法定了沒」，跑出 **16 張** ②→③ 候選——含 #739（SH provision 從來沒成功過）、#155（pod 活過 1 小時 skill 必 401）
> 這類**明確壞掉的功能**，等於把 A4「壞掉的功能」子群整段凍進「別自行開工」。加上第 2 層（做什麼 vs 怎麼修）後**只剩 2 張**。
> ⇒ **判準的粗細決定盤點結果的數量級，不是決定邊緣幾張**。
> 🔴 **第二個教訓**：leader2 依 agent 回報的引用片段，一度把 **#732** 判成該搬（讀成「產品行為決策」），
> 讀原文才發現那是「三條守衛寫法哪條不打壞合法流程」＝第 2 層 ⇒ 留 ②。**判歸類要讀原文，摘要片段會判反**。
>
> ⚠ **本輪殘留限制（下一輪要補）**：
> ① ②段那批多數只讀 body 未讀留言 ⇒ 「拍板紀錄只藏在留言」的單可能被誤判成未拍板。
> leader2 事後對主 repo 14 張補抓 comments 覆核（唯一命中的 #518 讀完是技術補充非拍板）⇒ **主 repo 那批已補；ar#／cd#／charts# 等他 repo 未補**。
> ② 只掃看板既有列項，**看板本身若漏列某張本該在 ②③ 的 issue，本輪抓不到**。
> ③ 計數口徑待對帳：盤點者逐列點算 ②段得 84（扣兩張已關），與當時看板標題 85 差 1，**差額未查明**。

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
| **#710** | ③ 架構 | ② A1 | ✅ **2026-08-18 已裁決：留 ③，A1 段補交叉指標（已補）**。🔴 **今天就能被利用**（guest 直接 `unlink` project，整個 project 連同 cloud_service 關聯從 workspace 消失）；進 ③ 的唯一理由是「誰能刪 project」的界線未定。要決的是：**「可被利用」該不該壓過「修法未定」**——若該，這張和下一張要搬進 A1 |
| **#704** | ③ 架構 | ② A1 | ✅ **2026-08-18 已裁決：留 ③，A1 段補交叉指標（已補）**。同上：租戶可自行 write `use_agent_runtime` 開 agent runtime、實際佈署 pod 吃資源，但三個修法選項（`groups=` ／ `write()` guard ／ 建通用 gate）未拍板。⚠ 第一個選項**可能連讀取一起擋壞 /woow 前台** |
| **#773** 🆕 | ③ 政策 | ② A2（金流與計費） | ✅ **2026-08-18 已裁決：留 ③，A2 段補交叉指標（已補）**。🔴 **與上兩張同一個判準問題，但落點不同段**：CS 降級到無 AC seat 的 plan 後，既有 access policy 與 CF Access 規則繼續生效，**付費界線被繞過且零訊號**（`reconciler.py:88` 判孤兒看「DB 端指不指得到 cf id」⇒ row 還在就判有主，這一類**結構上不會上孤兒候選**）。進 ③ 的理由是「要不要收斂／收斂到什麼狀態」未拍板（unlink／停用保留／生效到週期末，三者計費體感不同），**不是危害低**。⚠ 它的另一個歸屬是 **A2 不是 A1**——危害是**付費界線**被繞過，不是權限被越過 |
| **#706** | ② A1 | ③ 範圍未明 | 本單**只做唯讀盤點**、範圍明確 ⇒ 歸 ②；但「盤完要不要清、怎麼清、要不要 migration（牽動 #691 版號競賽）」全未拍板。若認為連盤點的執行方式本身也算未定，該改列 ③ |
| **#702** | ② A4 | ② A5 | 建的是**預防性偵測工具**，不是修任何現有壞掉的東西 ⇒ 嚴格說不屬「功能缺口／bug」；歸 A4 是因為它要抓的症狀（session 靜默失敗、filestore owner 錯）落在資料一致性。分類主筆自陳這是**推論不是內文自陳** |
| **#714** | ② A3 | ①b 固化自動驗證 | 產出是 **production 部署環境的告警機制**（故歸 A3），但「跑一次就永遠替你重查、價值在防退化」的性質與 ①b 完全一致。差別只在載體是 K8s 監控而非 code-level gate ⇒ **要不要把 ①b 的定義擴到監控**，是分類原則本身的問題 |

> 🆕 上面 5 張是 **2026-08-14 對帳新增**的兩可項。前兩張（#710／#704）問的是同一個問題，建議**一次裁決**：分類該按「危害是否可利用」還是按「修法是否已定」。目前看板兩種都有先例——#583（守衛檢查錯對象、修法未定）留在 A1，#667（設計面問題）進 ③——所以這不是誰偏離，是判準本身沒寫死。
>
> 🔴 **2026-08-18（leader2）：#773 併入同一次裁決，本組成為三張（#710／#704／#773）**。三張問的是同一個判準問題——**分類該按「危害是否可利用」排，還是按「修法是否已定」排**——但 ⚠ **落點不同段，別誤讀成三張都進 A1**：#710／#704 的另一個歸屬是 **②A1（資安：租戶可實際利用）**，#773 是 **②A2（金流與計費）**。
>
> ⚠ **這次裁決的影響面大於這三張**：判準一旦寫死，之後每張「可利用但修法未定」的單都照它走；而看板現有的兩種先例（#583 修法未定仍留 A1／#667 設計面問題進 ③）會有**一邊需要回頭重歸類**。⇒ 裁決時建議連帶指定「舊單要不要追溯」，否則判準與存量會不一致。

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
| **#725** | `sh_subscription` 的 `/renew-subscription`（`auth=user`）讓**任何登入者**用呼叫端給的 `subscription_id` 走 `sudo().browse()`，ownership 檢查只擋別人的訂閱，然後 `sudo().create()` **用自己那筆當範本無限鑄造新訂閱**（並順手把舊訂閱改成 renewed）。⚠ 今天沒釀成計費災難是因為鑄出的訂閱卡在 `state='draft'`、而平台判準一律只認 `in_progress`——**這層擋住是巧合不是設計**。⚠ 行號為開單當下；`/cancel-subscription` 同族嫌疑**未逐行查**。⚠ ~~`q=` 搜不到 body~~（**2026-08-19 實測推翻**：本 Gitea 的 `?q=` 會搜 body——`tea api ?q=` 與 `--keyword` 兩入口正負對照皆過，判準記錄在 #776 更正段。仍在的限制＝單 repo、單頁 50 筆要逐頁、中文斷詞未驗證） | **執行歸屬 `sh_subscription`**（Eugene 2026-08-14 裁決開在本 repo，以免線索斷在跨 repo 邊界）。平台側只能繞過它防守、改不了它——#519 的守衛判準形狀就是照這個缺口設計的（含 `state='in_progress'` 檢查）|

⚠ **上表 512 與 440 兩張不重疊**：#512 那 5 個站是全新發現的另一批租戶，不在 #440 原本的 18 家清單內。

---

# 疑重複 — ✅ 已結案：**ci#30 已關（2026-08-12，superseded by ci#66；論證在 ci#30 留言 6853，不在此複述；ci#66 仍 open、關閉條件見 ②A3 段）**

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

1. **develop `4f77a151` / `18.0.1.0.168`**（2026-08-21 issue-leader 於 PR #807〔docs-only〕merge 後量，兩路一致：`tea api …/branches/develop` tip `4f77a151c139…`（commit 04:05:54Z）＋`contents/src/__manifest__.py?ref=develop` base64 解碼 `.168`；`git ls-remote origin develop` 回同一 sha（⚠ 不是讀本地 ref——30+ worktree 共用 `.git`，本地 ref 變了不算證據）；open PR 0。⚠ **tip ≠ `.168` 的 merge commit `0eb11503`**——#807 沒 bump 版號，「tip 前進、版號不動」是 docs-only merge 的正常結果不是漂移；拿版號反推 tip 會錯。前值 `2258630e` / `.160`（2026-08-20 leader1 於 PR #792 merge 後量，兩路一致：`git show origin/develop:src/__manifest__.py` 與 `tea api contents/...?ref=develop` base64 解碼皆 `.160`、tip sha 亦 git／tea 兩路相同；前值 `37eedd5a` / `.152` / PR #775 為 08-19，更早 `1115baf7` / `.151` / PR #772 為 08-18）。附帶 08-18 實查：**stg（`dev-sandbox-stg`）已 rollout `.151`**——image `paas-odoo-stg:20260817.0847`、DB `latest_version=18.0.1.0.151` 且 write_date（08:52）晚於 pod startTime（08:49）＝真跑了 `-u`、pod 內存在 `models/smart_home_route.py`（#768 code 在場的不對稱實證）；prod 08-18 已 rollout **v151**（詳見上方發版現況行）。**這行同樣會過期，用前重量。**
   量法＝**兩條獨立路徑交叉驗證**：`tea api …/branches/develop` ＋ `contents/src/__manifest__.py?ref=develop`（base64 解碼）對照 `git fetch` 後的 `git show origin/develop:src/__manifest__.py`，sha 與版號皆相同才算。單走 `tea api` 一條不夠——同族的 `raw?ref=` 有**靜默忽略 ref** 的前科。
   ⚠ **08-14 實例佐證「不要用工作區」**：當時本機 `develop` 落後 origin **22 個 commit**（工作區 manifest 停在 `.132`），而 `git rev-list --count @{upstream}..HEAD` 回 **0** ⇒ **`ahead=0` 不代表同步，落後方向零訊號**（#723 已承接並關單）。
   ⇒ **不要用主 repo 工作區路徑當 develop 現況**，一律 `git show <ref>:path` 或 `tea api`。
2. **幽靈額度 gate 的假陰性長得像「整理文件措辭」**：同時加一個真 `.sudo(` 並潤掉一句註解裡的 `.sudo(`，字面數維持不變 ⇒ 靜默放行。引用請以 #576 留言 **5224** 為準。
3. **在落後很多的分支上跑 gate＝跑到舊版的 gate**（#590 的分支落後 53 個 commit，repo-lint 只有 16 條、沒有 `pattern-doc-registry`）。
   ⇒「綠」在那裡的意思是**沒被檢查**，與「通過了」輸出完全相同。正解：在 `merge-tree` 預覽樹上跑。

> 其餘 5 天份的方法論教訓（探針失效形態、保全順序、`Refs` vs `Closes`、載體等級…）
> 已在 `memory/MEMORY.md` 與 `docs/` 有持久載體，**不在本看板複述**——複述就會漂。
