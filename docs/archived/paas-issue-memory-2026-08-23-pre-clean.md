# PaaS 上線推進看板（2026-08-21 精簡版）

> **本 note 只放 open issue 的分群結果**，供 Eugene 決定先做什麼。歷史過程、驗證證據、教訓段一律不放——它們在各 issue／PR 留言／`docs/`／memory 有持久載體，這裡只留指標。
>
> **對帳重算法（給推導方式不給死數字）**：`tea api -X GET "/repos/{owner}/{repo}/issues?state=open&type=issues&limit=50&page=N"` 逐頁抓到「該頁不足 50 筆」為止，再與看板表格**第一個 cell** 的 `**#N**` 取集合差。六個 repo：main `odoo-addons/woow_paas_platform`、ci `woow-paas/paas-odoo-ci`、cd `woow-paas/paas-app-bundle-cd`、charts `woow-paas/woow-paas-charts`、ar `woow-paas/paas-agent-runtime`、sh `ha-components/woow_paas_smart_home`。
> 🔴 `limit` 被 server 截到 50（不逐頁會靜默漏抓）；🔴 不帶 `type=issues` 會把 PR 算進來；🔴 owner 猜錯回 404、與「issue 不存在」分不出來；🔴 live 快照有效期是分鐘級，寫入前重抓。
>
> **發版現況**：prod 現跑 **v18.0.1.0.151**（08-18 rollout，證據在 cd PR#116 留言 7773）；落差基準以 prod DB 實裝版號為準，不寫死。🔴 **operator 側待發**：#826＋#832＋#839 三修已併 develop、均未隨任何 operator release（leader2 08-22 量測：`b030a16a` 不在 `operator-v20260821.2328` ancestor）——下次 operator 發版同批帶上。
> **上次修剪**：2026-08-22 09:55Z（issue-leader）——認領表四列結清；刪 5 列已關（#531/#543/#546/#648/#820）、補 6 列 open（#809/#821/#823/#824/#828/#833）、tea 對帳 main repo 132 張 open 與看板集合差歸零；修剪前全文存 `docs/archived/paas-issue-memory-2026-08-22-pre-clean.md`。前次：2026-08-21 12:10Z（issue-leader）——刪本日已關的 #806／#785／#684／#544 四列；同日稍早——刪 20 張已關、補 9 列（7 張未列 open＋#777／#787 原無自己的列）、全段壓縮（166KB→約 54KB）；修剪前全文存 `docs/archived/paas-issue-memory-2026-08-21-pre-clean.md`。

> ✅ **MEMORY.md 拆檔完成（2026-08-23，Eugene 點頭、issue-leader 執行）**：24971B/87 行 → 13510B/82 行（限 25000B/200 行）；46 條縮短行原文逐字下沉 41 檔（各檔尾「拆檔前的原索引行」節）；拆檔前全文＝memory/MEMORY-pre-split-2026-08-23.md。驗證＝機械逐行歸屬（KEPT 41＋SUNK 46＋MISSING 0、76 link 0 broken）＋三視角 adversarial（裁決 PASS／具體值 PASS／症狀路由 2 格退化已補回重驗綠）；孤兒盤點 206 檔零跳＋一跳全可達。🔴 **索引解凍**：可新增索引行，規則＝link＋一句 hook、症狀字面留在 hook；任何 session 動 MEMORY.md 前必先重讀磁碟（各 session 的注入快照已全數過期）、只逐行 edit 禁整檔重寫。殘項：真實 bytes 上限本地查無設定（疑 built-in）續用 25000 保守值；traps 檔 assertRaises 三層整理仍待；feedback_negative_verification 已由 topic 檔一跳路由、結案。

## Leader 工作原則

- `issue-leader` 負責分配 issue 和改本 note；Leader 對本 note **唯讀**，要改請 `issue-leader` 改（一律 `maestri note edit`）。
- Leader 一次只做一個 issue（或同組相關的 issues），不排程無關工作；全部完成後 git pull 並 /clear 才接下一張。
- 取用時請 `issue-leader` 在「🔒 認領中」登記＋在該 issue 列標 🔒；完成請 `issue-leader` 兩處移除。**無登記＝無人處理。**
- 不把本 note 當工作記憶；要工作記錄自己 `maestri note create`。
- 本 note 定期清除已完成 issue、同步 `tea` 可讀到的 open issue，不留過時記錄。
- 🔴 **manifest 版號判準**（#691 改制；PR #817 2026-08-21 merged → develop `b521bc70`，gate 已上線＝develop 唯一 required check）：**A. 一般 PR 不動 `src/__manifest__.py` 版號**——必須**等於**當下 origin/develop（併完 develop 後 `git diff origin/develop -- src/__manifest__.py` 須為空；版號 > develop 而非下列兩類 ⇒ gate 紅）。**B. 只有「新增 `src/migrations/<X>/` 目錄（base 側 tree 不存在）」的 PR 才 bump**：元組比較嚴格大於 develop、且本 PR 新增的目錄至少一個 X == 新版號；新增目錄卻沒 bump ⇒ 紅（`migration-needs-bump`）；只改既有 migration 目錄＝一般 PR、不 bump。**C. 發版才 bump**：`release/*` 分支（`/woow:deploy` 產生）嚴格大於 develop，相等 ⇒ 紅（`release-needs-bump`）、不吃 out-of-scope 豁免。B／C 取號＝`max(develop, 其他在飛且會 bump 的 PR head 的 manifest)+1`、多人同時由 issue-leader 指定、量完立刻 commit；被發版 PR 追過 ⇒ gate 紅、重 bump＋目錄同步改名（預期中的**可見**競爭，不再靜默）。版號低於 develop 一律紅（＝先 `git merge origin/develop`）。權威：`scripts/ci/manifest-version-gate.py` docstring、`docs/guides/development/merge-train-acceptance.md` §1。⚠ 手上分支若照舊制已 bump，**改回 develop 現值**再 push，否則 gate 紅。
- ⚠ **磁碟約束放寬（08-23 Eugene 裁、issue-leader 執行 docker volume prune：79 顆匿名 dangling 全刪、named 0 顆波及、in-use 7 顆未動；host df 9041236→15983220 KiB＝+6.6Gi）**：並行 disk-heavy 測試環境上限 **2**，起前仍向 issue-leader 登記（協調用、非磁碟閘）；free <10Gi 回到單一 slot；⚠ 成本口徑分兩種（leader5 08-23 實測）：**容器路線**（image 本機已有）一輪淨耗 ~0.2GiB、**k8s sandbox** 首拉四顆 image ~2.5–3.5GB——排隊與 abort 估算按後者；收工立刻 destroy＋清理並回報 df；teammate 收工照保全範本後 dismiss。（08-23 通道紀律：maestri 訊息可能靜默丟失——「沒收到回報」不得推論成「還沒做」，催前先 check 或自查產物（dev-803 兩次回報未達、leader5 自查 tea api 替代）。08-23 slot 現況（issue-leader 實測、不沿用快照）：**1/2 佔用**＝leader3 dev-sandbox-849split（#849 RED-A 已量完＝bug、沙盒留著 TTL 08-24T03:55Z 等 Eugene 定補不補配額 DoS、dev-849 待命）；leader5 #385/#803 worktree＋sandbox 皆已清（清前兩顆 sha 都驗＝本地 HEAD＋遠端當下指向、df +57.8MiB 標不純＝全系統讀數＋.git object store 共用）（merge 後 17 條＋全模組；dev-740 曾正確拒絕無讀數的放行＝驗令規則實戰生效）。⚠ 磁碟 ~26Gi（08-23 prune 後 Docker.raw 延遲 TRIM 陸續歸還、01:22Z 實測 Avail 27294540 KiB）、abort 線 6Gi 全隊適用；🔴 機器有隊外活動讓 Avail 以數百 MB 量級自漂（leader2 實測沙盒刪後反升 600MB、Docker.raw 是 sparse 配置 72.9GiB）⇒ **單點 df 差值不得歸因單一 agent 的沙盒生命週期**、驗令規則（各報自己當下讀數）反被此佐證。⚠ 起沙盒 slug ≤26；⚠ **local 沙盒已知環境紅（歸因已定案＝兩機制、全量測；載體＝leader1 08-22 歸因報告、正寫入 PR #843 body）**：A) `.sandbox/post-create.sh:66` 把 `paas_domain` seed 成 `${SLUG}.woowtech.io` fallback ⇒ 斷言裸 `woowtech.io` **渲染值**的 2 筆 FAIL（test_public_url_follows_seed_not_slug／test_assemble_layers_all_injectors_over_user_config）任何 local 沙盒必紅；B) `AGENT_RUNTIME_NAMESPACE_SECRET` 未設 ⇒ ERROR 死在 agent_session.create() fail-closed——**證據分級（leader3 08-22 抓縫後改）**：CI 註解（`odoo-tests.yml:35-36`）自證的是 **2E＋一個 class**（TestNamespaceLeftSetUnion）；第 3 筆（TestBindingGuardDownstream）僅由 **traceback 逐字相同**支持、CI 註解不涵蓋（2 vs 3 口徑差待 #798 的前後對照逐筆列名判掉）。⚠ 「#822 補 env 即 0F/0E」的環境＝**本機手建容器（docker -e 直接注入）、非 k8s 沙盒** ⇒ 證機制充分性、**不證 local 沙盒注入得進去**（`.sandbox.yml:47` 寫該 env 僅 stg 生效——與 #822 證據不衝突，因 #822 根本沒走沙盒）；local 沙盒注入路徑判準＝pod 內 printenv（dev-798 執行中）。⚠ issue-leader 曾在叫號時把 A 聚合成五筆因果並掛錯載體（leader3 攔下、已更正）——引用以本行為準。機制 A 的**既有載體＝issue #804**（08-20 開、同兩 class 同機制、含 DB 實測值與 base 對照、系統性守線題已在內未拍板）——leader1 查重撞到、不開新單、本輪獨立資料點回貼 #804；機制 B 無 issue 但 docs/tracking 三處記載、且 318 landing 指另一候選成因（test-isolation 的 addCleanup 無條件 pop env）⇒ 開單中、第一項工作＝分辨兩成因。）磁碟回到寬裕後由 issue-leader 刪本條。
- 🔴 **docker-desktop 沙盒 hostPath 內容 gate（#814，woow-dev plugin `951f49c4` 起，2026-08-21 已重裝生效）**：local 模式 `create.sh` 逐條比對 hostPath 的 host/node 正規化 digest，**不一致或探針失效都拒建 rc=1**。紅了＝node 上那條路徑是凍結舊副本（凍結點 ≤ node 容器建立、重啟不刷新）或探針壞了，不是你的碼有問題；正解＝`docker cp` 進**自己專屬**的 node 路徑再建（別覆蓋共用 `paas-odoo-ci/addons`），逃生門 `--allow-stale-node-copy` **不准自行使用**、要先問 issue-leader。驗「沙盒測的是我這份碼」看 host↔node↔pod 同檔 sha，不看路徑存不存在、不看 create.sh exit 0。✅ **共用副本已刷新（08-22，leader2 執行）**：9 模組漂移全 0、全樹 digest `4c5531a3…`（host==node；`.bak` 對照不同值＝判準有鑑別力）；舊副本留 `addons.bak-20260822`（node 內 19M，刪除由 Eugene 裁）。⚠ 三個殘留注意：**驗 digest 必帶 `LC_ALL=C sort`**（BSD/GNU 排序不同 ⇒ 假漂移）；同族：macOS `wc -c` 補前導空白、busybox 不補 ⇒ 跨 host/容器逐檔比對「全部不同」可能比的是格式不是內容（`tr -d " "` 收斂）；**刷新前已掛載的 pod 看的仍是舊 inode、要重啟**（本次 0 pod 無此情形）；歷史上在 docker-desktop 驗過的**計費相關綠燈要重驗**（舊副本缺 wallet migration＋billing SSOT 測試）。隔離副本過渡作法退場、新沙盒直接用共用路徑（#814 gate 續守）。⚠ **誘餌目錄**：`~/gantry-47/paas-odoo-ci/addons` 是停在 0.0.4 的舊 checkout（與正版 `~/Documents/woow/woow-paas-repo/paas-odoo-ci/addons` 尾段同名、兩邊都有合法內容 ⇒ 路徑打得開零鑑別力）；查第三方 addon 一律用**內容**（版本/mtime/digest）驗前綴，清掉或標 STALE 待 Eugene 裁。

- 🔴 **Merge 授權（Eugene 08-22 裁）**：**純文件（docs-only）與純 bug fix、且無待討論取捨**的 PR，issue-leader 於 CI 全綠＋覆核 diff 後**直接 merge**、不等 Eugene（判準＝「沒有討論的必要」：修法唯一明確、無設計取捨爭點）；**類別按精神非字面窮舉：純 docs／純 bug fix／test-only（零生產碼）／純 CI-config 且無待討論取捨皆直接併類**；含取捨／行為權衡／動生產碼行為／feat／refactor／CI 政策類仍 ⛔ 待 Eugene。PR 標題照舊一律掛「⛔ 勿代 merge」前綴（防其他 agent；**這半永遠不拿掉**），尾段依類別二選一：「待 Eugene 覆核」（取捨類）或「issue-leader 覆核後併」（直接併類）；類別在 merge 時由 issue-leader 終判，中途翻類就改標題。類別判定與 merge 動作只歸 issue-leader。merge 前照 multi-PR 驗收工具箱（manifest 同值撞號、`git diff origin/<base>` 非空判準、base 前進重驗）。

- 🔴 **升裁選項組必先驗共同前提（08-23 #385 A 案作廢後訂）**：三個選項共用同一個未驗前提＝**一個未驗前提偽裝成三個選項**——不會以「某選項有缺陷」暴露，而是裁了才發現全不可行（Eugene 對不存在的路徑做了裁決）。升裁時至少一個選項必須是「先驗證前提」；內文標了 ⚠ 但沒進決策結構＝對裁決者等於不存在。轉呈者（issue-leader 含）同責：收到選項組先問「共同前提驗過沒」。
- 🔴 **`tea issues ls` 的 `--limit` 封頂 50 且無截斷提示（08-23 leader4 實測）**：給 100 也只回 50，「我搜過了無重複」若來源是它，範圍＝最近 50 張非全部（實際 370 張/8 頁）。查重一律 `tea api .../issues?state=all&limit=50&page=N` 分頁到底；判準＝**輸出行數剛好等於上限就重數**（`--state all` 與 `open` 同數字＝上限訊號非資料）。
- 🔴 **CI status 的 `id` 欄是每 commit 從 1 自編、非全域唯一（08-23 leader3 更正）**：判「CI 是新跑不是回收」用 `target_url` 的 run/job id（單調變大）＋`created_at`，⛔ 不用 status `id`（新 head 的 id 反而更小、會被讀成回收）；⚠「三項一起用（id+job+created_at）會讓錯的 id 項被對的兩項掩護過去」＝隱藏的假判準。
- 🔴 **保全落點必須掛在不會跟著被清掉的東西上（08-23 leader3 實踩後訂）**：teammate 建的 canvas note 連在 teammate 節點上、dismiss 即失聯（No connection）——「dismiss 前讀得到」探針對「dismiss 後讀得到」零鑑別力，兩種 note 輸出不可區分。保全落點＝派工者自建 note／repo 檔案／issue 留言；唯一誠實驗法＝先搬到自己的載體再 dismiss。dev-836 保全範本待補此條（leader3 執行）。
- 🔴 **保全協定 df 那格判準修正中（暫行紅字、doc PR 併入後由 issue-leader 撤）**：dev-preserve-doc2 實測起用前後 Avail 差 -2.1GiB、全機雜訊約為單 worktree 訊號（28MiB）的 76 倍 ⇒ df 差額**不得當足跡交差**；足跡宣稱以被刪物本身 du 為證、df 配對降級為 context（量了基準的價值＝讓你看見配對證明不了什麼）。三件 doc 修正（df 格／首行也要逐字驗／對照表內容定稿後才算）批一張 PR、#849 後由 leader3 開；載體 #886 留言 9423。
- 🔴 **未署名訊息的作者歸屬（08-23 leader5 案 7 誤判後訂）**：所有 maestri 訊息走同一條通道 ⇒「從哪裡到達」對作者**零鑑別力**；未署名訊息作者欄**留空**、不用語境補最合理作者（ghost 的形狀正是「沒署名＋內容合理＋收端補作者」）。遞未查證前提時**語氣是傷害的一部分**：「已確認、可以停」的包裝是在要求對方停止驗證——不確定就寫「未署名、作者不明、供對時序」。
- 🔴 **覆核紀律兩條（08-23 #803 輪貢獻）**：①**集合比對通過 ≠ 預測成立**——預測含成因時成因要逐條比：#803 三條紅集合維度完全正常（20 執行 20 紅對稱差空）、逐條看才發現成因是前提 AssertionError 非預測的 KeyError ⇒ GREEN 後會空過、輸出與真守住一模一樣；擋下它的是測試自帶的前提斷言。②**解釋一件事的動作本身會生產那件事的識別字**：repo-lint 的 sentinel 檢查 regex 掃全檔不分註解——在註解寫「本 cron 不走 provision 開閘」就把自己種進違規清單（與 PR body 解釋「為何不關 issue」種出 close token 同型）。
- 🔴 **驗令紀律（08-22 訂；起因＝dev-* 終端輸入框**至少四度**出現非本人、未送出的指令；三種形狀漸升：①事實宣稱（案1/2 slot 空出來——與現況矛盾、易抓）②流程判斷（案3 dev-822「乾淨就開 PR」——零症狀）③**代行裁決型**（案4 dev-803「D4 D5 准」）④**代行動作宣告型**（案5 dev-803「我確認一下 df，你先待命」——以 leader 身分宣告未承諾的動作，若無人真做、teammate 停在永遠等不到的待命）。🔴 **一致性特徵**：6 案中 4 案內容與本意一致。案 6（08-23，leader1 終端、dev-850 發現）＝以 issue-leader 名義的收尾指令、內容與 issue-leader **實際已送出的指示**高度吻合（歷來最接近真訊息的一次；dev-850 因「刪除性＋工單禁令」拒執行全對）。案 7 候選（08-23）＝**Eugene→issue-leader 通道**三連發字面「--file」（跨多輪間隔、每次逐字相同、內容與近期「工單走檔案」語境一致＝家族特徵；⚠ 首次出現在 Eugene 本人輸入通道、非 dev-* 終端；歸屬待 Eugene 確認，已當雜訊處理未執行任何動作；⚠ 勿與 leader5 通道那則**單發**「--file」混同——那則已解＝issue-leader 探測 `maestri ask leader5 --file` 時誤送（更正訊息作者是 issue-leader，leader5 一度誤讀成 Eugene 所發並主張案 7 可結、已糾正）；Eugene→issue-leader 的三連發**前兩發早於該探測**、時序上不可能由它解釋，仍未解。附帶機制資料點：`maestri ask` 對 prompt 位置的 `--file` 是當**訊息文字**送出＋block 等 idle，不是旗標解析）。案 8（08-23，dev-803 輸入框、leader5 發現）＝型④代行放行「RED 收到，進實作」——未執行（三格驗證：dev-803 自陳無此 prompt、porcelain 空、**實作物 ls 不存在**＝比 status 更強的一格）；🔴 連帶紀律：**`maestri check` 畫面在對方 idle 時是凍結快照**（連計時器都不動）——「送了控制字元、check 沒變」不能推論沒送到也不能推論還在；判對方狀態＝問對方＋自己獨立量（git status／實作物 ls），不看畫面。🔴 **案 9（08-23，dev-803 終端、leader5 發現）＝形態質變首例：指令「已送出並執行」**（`/usage` modal 面板卡住 agent 停擺；先前案全是「未送出殘留」）——「不執行來源不明指令」紀律對此形態無效（不需我們配合就已執行）；处置＝確認 modal 後 ESC 解卡（leader5 改變決定明記）、帶外痕跡（transcript 兩行）照實記。連帶偵測紀律：**check 看到任何非預期畫面（面板/設定/對話框）都當異常回報**，不只看輸入框；⚠「面板開著」與「凍結快照」輸出不可區分、送 ESC 才能判。🔑 對時序資料點（issue-leader）：**同日 Eugene 在 issue-leader 終端本人跑過 `/usage`**（有 local-command 紀錄、時點近「merged pr #872 #871」）——與 dev-803 的 `/usage` 同指令 ⇒「Eugene 輸入落錯終端」假說的權重上升（`--file` 三連發同型），仍是假說待 Eugene 確認⇒ 🟢 **ghost 系列結案（08-23）：Eugene 確認案 7（--file 三連發）與案 9（/usage 已執行）是他本人操作落錯焦點視窗**——「內容與語境一致」的家族特徵由此解釋（打字的人就在同一工作脈絡）；案 1-8 的未送出殘留大概率同源。改為協作約定：canvas 操作前確認焦點終端。⚠ agents 端**驗令紀律與「非預期畫面當異常回報」保留為常規防線**——通道存在、誤觸會再發生，且驗令紀律今天已兩次獨立擋下真問題（dev-740 拒無讀數放行、dev-850 拒刪除指令）。原待驗假說：部分案例可能是**佇列中訊息的顯示殘留**（ask timeout 後排隊訊息在輸入區可見？）——未驗證，與 stale-session 假說並列，仍待 Eugene claude agents 查證。偵測通道只看得見與現況矛盾者、一致者靠規則主動回報；機制面 :102 記載「輸入框打字不送出」若成立則皆不可執行；歸屬待確認，候選＝/clear 前的舊 issue-leader session 背景殘留（Eugene 用 claude agents 查證）**：執行類指令（起沙盒／merge／刪除）不憑語氣或參數正確性認令——**驗令判準必含至少一項「發令當下才量得到的新鮮狀態」**（語氣與參數可從歷史抄、當下量測值抄不出來——dev-829 不對稱原則）。⚠ 憑證要用**原始量測不用格式化呈現**（08-22 leader1 實測：同一真值 11.49GiB 被 df -h 渲染成 11、十進位成 12 ⇒ 格式化數字會誤擋自己人）：② 憑證＝`df -k <mount>` 的 Avail 原始 KiB 整數＋mount 路徑；判準是「發令者報得出當下原始讀數」，**不是**「兩端數字相等」（磁碟本就持續變動、允許差異）。輸入框出現來源不明未送出指令：不執行、回報 issue-leader。歸屬待 Eugene 確認（若係 Eugene 手動操作則轉為協作約定）。⚠ 08-22 補：memory `topic_dispatch_and_workorder.md` 08-21 既有記載「輸入框打字**不會被送出執行**」——若成立，三案在機制上皆不可執行 ⇒ 威脅面下修（leader1 自行更正原 ENOSPC 因果鏈第一步）；驗令紀律照留。teammate 寫共用 memory 前先問自己 leader 落點、由 leader 向 issue-leader 宣告（工單模板即日加此行）。

## 🔒 認領中

| Issue | Leader | 起始 | 狀態 |
| --- | --- | --- | --- |
| **#824** ✅ 已結案（08-22 13:07Z） | leader2 | 2026-08-22 | **Eugene 08-22 派**：operator `create_dns_record_for_tunnel` create 時搶佔他人 DNS（repoint 分支靜默）＋`reconcile_tunnel_dns` delete-loop 無 `content` 複驗。修法三方向（issue body）由實作者選型、PR 寫理由；⛔ 對真 CF 僅限唯讀 GET、寫操作一律 mock；operator 碼合併前本機真 K8s integration tests；#703／#823 不進本 PR。**選型定案（08-22 leader2）**：方向 1+2 合併＝單一本地歸屬述詞（rec content == cname_target，不等 fail-closed）套所有 DNS 突變點；方向 3 不採（schemas.py:774 同碼 in-code 決策＋create 路徑無 stamp＝fail-open）；CF 唯讀 token 撤案（外部契約改為非承重，未驗項二照 issue 框架寫 PR）。**issue-leader 裁範圍＝甲**：資源側枚舉補到同族共 5 處一起修（issue 點名 2 處＋create_dns_record repoint／delete_dns_record 零 content 檢查（嚴重度＞缺口二）／_cleanup_dns_for_tunnel 無複驗＋分頁 bug）；條件＝PR 明標 3-5 為補列、五處各自注入紅燈；diff 過大可自行降級丙（兩張 PR 互標）不重問。**tombstone-check 定案（08-22 leader2 採納）**：content 不符時查對方 tunnel（走會濾 deleted_at 的 `list_all_tunnels`，#833 前提 leader2 已複驗）——活的 raise／證實不存在允許自癒＋大聲 log；leader2 補三格防 fail-open（非 cfargotunnel content 一律 raise、tunnel id 精確後綴比對、happy path 零 API 呼叫有測試釘住）。⚠ 既有測試 `test_repoint_existing_uses_per_tunnel_target_not_shared`（test_tunnel_routes_sync.py:116）把搶佔鎖成正確行為，本 PR 將明寫改掉它斷言的行為（保留防 routing 黑洞那半、加反向守線：活 tunnel ⇒ raise＋零 PUT）——覆核時非繞紅燈。teammate dev-824（ccou）已招、工作區 wt-824＝fix/824-dns-ownership-guard@`16b5fc04`（issue-leader 實體複驗：porcelain 空、遠端無 824 分支）；工單 opt-in 殘句矛盾已由 issue-leader 攔下、leader2 修正後才發 dev-824（檔案複驗）。裁決全文：scratchpad `ruling-824-scope.md`。**08-22 交付 PR #839**（head `bf617ac4`、base `16b5fc04`、CI 三 context 全綠、3 檔 +1095/-42、manifest 不動、body 掃 auto-close 0 命中）；leader2 獨立覆核＝predict-then-observe（述詞恆真注入：預測 16 紅逐支全中、五 site 各至少一紅＝五處確路由同一述詞；outbound 封鎖器＋陽性對照證全套零真實外連 635 passed）；dev-824 RED 為中間態量測不在 git ⇒ 標 relayed、性質已由覆核注入獨立建立。⚠ dev-824 曾意外對真 CF 發唯讀 GET（7003 未授權、無寫入、未違鐵律）——暴露測試層無防外連機制。衍生單已開：**#841**（測試防外連守線，高）＋**#842**（tombstone 跨 account 邊界，低）。**08-22 dev-824 保全問答交出的三個取捨已補進 #839 body**（末節明標覆核後補入；body 15279→18641 bytes、head `bf617ac4` 不動、token 掃 0、PATCH 前 assert 節不存在防重複追加；(a)(b) 機制 leader2 逐行查證非轉述）：(a) 呼叫端稽核結論（專用/共用 tunnel 路徑不相交 ⇒ 拒刪孤兒情境實務不撞）；(b) **CLOUDFLARE_TUNNEL_ID 更換 ⇒ 既存記錄全數不符共用 target ⇒ delete 一律拒刪留孤兒（有 ERROR log）＝第二個 prod 行為後果，Eugene 裁決前必須在場**；(c) tunnel 清單查詢失敗選 bare raise 不包 409（409＝我們拒絕 vs 查詢失敗＝CF 出事，歸因不可混）。docs PR＝**#845 ✅ 08-22 已併**（issue-leader 直接併＝**merge 授權下第一張**；merge `14cd0f70`、head `36bc8557`、+86/−29 單檔、三 context 綠、issue-leader 併前逐 hunk 讀 diff＋API 驗 token 0）。內容：計數口徑巧合警告升主要價值、兩個 bootstrap 區塊 byte-identical＋自我聲明、quickstart 改匯出 kubeconfig（use-context 會改全域 context）、陷阱節第三份指令刻意去重改指標。過程更正一筆：leader2 曾誤診「curl -fsS 靜默」（`-S`＝show-error 會印；真問題是**訊息指向 helm 而非『在錯的目錄』**）——dev-824 附實測反駁、leader2 對照組自驗後收回；另 dev-824 抓到 `$PWD` 緊接全形括號被吃 byte 的 bug 改 `${PWD}`（現形原因＝**從檔案抽區塊執行不重打**——重打測的是意圖不是內容）。。integration slot 已撥（回歸 check 性質、非驗守線——leader2 已明言不包裝）。**PR #839 ✅ 08-22 Eugene 裁併、issue-leader 執行**（Gitea 拒直併「head 落後 base」→ update-branch 先例 head `e7b949bd`（leader2 雙向 diffstat 驗無夾帶）→ CI 三 context 重跑全綠（含耗時同量級＝真跑非 skip）→ merge `b030a16a`、is-ancestor 驗過）。⚠ 修在 operator 碼 ⇒ **prod 生效等下次 operator release**（發版清單與 #826/#832 同批帶上）。**收尾全完成**：合併落地雙向驗（含陰性對照）、wt-824＋遠端分支清（陰陽對照協議）、issue 留言 8784（已驗/未驗框架）後關單（#841/#842 陰性對照仍 open）；🔴 leader2 **量測**（非轉述）：`b030a16a` 不在 `operator-v20260821.2328` ancestor ⇒ 修正未隨任何 release 出去。leader2 待命。**Integration 收官（08-22）**：41 passed/4 skip in 117s（4 skip 全＝docker-desktop 無 KubeVirt/CDI CRD，逐支列名＋kubectl get crd 獨立確認 NotFound；預測先寫再跑）；🔴 覆蓋量測＝本 PR 238 改動行僅 7 行被 integration 執行到、全是 import 期宣告 ⇒ **守線驗證來源只有 unit**（integration＝回歸 check，與 `tests/integration/README.md:153` 自宣告的 CF 零覆蓋 gap 兩源同向）；資源零殘留（ns Terminating 自然消失、未重複 delete）、df 11→12Gi 未惡化、helm 3.13.3 對上 Dockerfile ARG。衍生：README.md:35 記 41 tests 實為 45 收集/4 skip——⚠ 與本次 41 passed 是**不同口徑的巧合**（照 README 讀會誤判零 skip）；同檔 :31 第二同族陷阱＝可照抄指令 `HELM_BINARY=$(which helm)` 會拿到系統 helm v4（post-renderer 吃 plugin 名不吃路徑 ⇒ 照抄者 debug 不存在的程式 bug；**警告在、範本沒改**＝可照抄範本勝過正文警告家族）；issue-leader 裁＝兩條併同一張 docs PR（dev-824 承接），標題掛「⛔ 勿代 merge｜issue-leader 覆核後併」。 |
| **#822** ✅ 已結案（08-22 14:24Z Eugene 親關） | leader5 | 2026-08-22 | **Eugene 08-22 派**：SA `action_provision` 第 2 步 identity write 失敗時 CF `create_tunnel` 零補償 ⇒ 冪等守衛失效、每次重試多一顆計費孤兒 tunnel（#385 產生源上游）。補償設計由實作者選型、須涵蓋**所有**失敗形狀（sudo 修法只消 AccessError 一種）；issue 自陳全靜態推導 ⇒ 第一步先用測試實跑重現；⛔ 不對真 CF 寫操作；既有孤兒清理歸 #385 不進本 PR；⚠ 與 #798 可能同檔 `security_access.py`——本單只動 action_provision 段，先開先併、後開者 merge develop 再開 PR。工單 scratchpad `dispatch-822-leader5.md`。08-22：dev-822 RED 測試碼完成（`407c959d`，16 條五組、C2 baseline 重產只增不減、manifest 0 行）；deploy-local 路線查證不可行（動共用 addons＋缺三顆 image）；**環境裁 (1)**＝本機自命名容器 wt822-db/wt822-odoo（零 pull、估 2–3GB），序列化在 #824 之後；條件＝跑前後量 df、GREEN 完立即清、不碰 wo787-*。**08-22 RED 收工**：8F/16 與跑前預測逐條相同、紅全落缺陷斷言；孤兒再生實證（重試累積 tun-822-1/-2、tunnel_id=False）；df 11Gi 未動。🔴 **抓到框架級假綠（已廣播 #798/#732、已進 memory；⚠ 機制細節 08-22 稍晚更正：是 savepoint() 預設 flush=True——flush 的是區塊之前的寫入、回捲邊界恰是區塊本身；flush=False 那個命中是 warmup 誘餌，dev-798 抓到、leader3 複驗、結論 (i)(ii) 不變且更強）**：例外穿出時區塊內寫入（towrite＋已送 SQL）全回捲——(i) 例外後驗 DB 可能假綠 (ii) 修好的守線寫入也被回捲＝測試結構上永不可綠；靠「預測 8F 實跑 7F 差 1 必追」抓到；機制進測試 docstring（a1441764）；修法＝該場景裸 try/except。環境兩坑自修：image `/opt/bitnami/odoo/addons` 空目錄；管線接 tail 吃 rc。docstring 更正完（`751e6255`，dev-822 進 image 複驗非照抄、補「AccessError 路徑多一次 cr.clear()」細節）；🔴 **flush=False 錯誤來源自首＝捏造解釋非誘餌**（根本沒 grep 過，機制是憑記憶替實測數據編的；已進 memory——與踩誘餌是兩種病）；0-hit 判準自撤（否定語境命中是資產，改性質版）。**GREEN 收工**：0F/16、八陽性對照綠、補償 log 標記 9+1 對上獨立枚舉（RED 輪 0）；全模組 0F/0E of 3359（3E 缺件歸因＋陽性對照）。**交付 PR #851**（head `751e6255`、⛔ 待 Eugene 尾段、**body 標「⚠ 有取捨待裁」⇒ Eugene 覆核類**、token 0、manifest 0 行；repo-lint／test-collection pending 監看中、本機 32 項全過＋C2 baseline 只增 5 行）；**slot 已釋放**（容器三查空、df 12Gi）；dev-822 保全＝canvas note「dev-822 保全原文」123 行（leader5 探針實跑驗收：5 關鍵字全中＋陰性對照 0）後自行 dismiss（leader5 具 maestro）。**收尾全完成（08-22）**：PR #851 Eugene 親併（merge `bcf81329`；⚠ 實併 head＝`f9f0b016`＝Eugene 自己的 update-branch、與 #819 同型但零 CI 紅；`751e6255` 亦在 develop）＋issue Eugene 於 merge 後 2 秒**親關**（leader5 的 PATCH 為 no-op、誠實不記成自己關的）；落地驗證含內容格列名＋陰性對照（前兩次陰性對照自壞自抓：rc=128 與 cmd|head 吃 exit code）；wt-822/遠端分支清（陰陽對照）；證據留言 8829 全等驗證；生效＝隨下次**平台**發版非 operator 清單。 |
| **#798** ✅ 已關（08-22 14:52Z） | leader3 | 2026-08-22 | **Eugene 08-22 派**：`cf_policy_id` 無欄位級寫入守衛（無 groups=、無 identity-anchor、ACL 1,1,1,1）⇒ 租戶裸 RPC 可寫自己 workspace 的 policy 指向。修法照 route 側同類欄三件套照抄；先讀 #786 覆核脈絡；RED＝裸 RPC write 先紅＋合法路徑陽性對照；⚠ 與 #822 同檔界線見上列。工單 scratchpad `dispatch-798-leader3.md`。08-22：dev-798 由 leader3 **自招**（leader3 具 maestro 權限）；leader3 AST 枚舉揭示實際工作量在「所有伺服器端回存路徑改走 sentinel」（跨 4 個 model 檔＋一批直寫 fixture）＝三件套必然成本非取捨，PR 會大；issue-leader 要求 commit 分層（守衛／回存遷移／fixture 遷移分開）以保 merge 類別可判。沙盒 slot 排隊中（RED/GREEN 都要沙盒；真 RPC 端到端是 issue 自列未驗格）。08-22 第 1 段：dev-798 test-only commit 完成（RED 寫完**未跑**、明令 slot 前不得先實作）；leader3 獨立 AST 下界與 dev-798 tokenize 枚舉逐行對上；🔴 leader3 攔到 dev-798 拿「repo-lint exit 0」當過關而 **C2 實為 FAIL**（3 新 class 不在 baseline；C2 是雙向差集、與 repo-lint 非同一 gate、#819 末關同格）——已令 baseline 更新獨立 commit＋逐檔驗只增不減；取捨自裁（不上呈）＝逃生門走共用 helper 加 ctx_key= 參數而非第 4 份複製（實據：既有 3 份複製已漂移、wt-822 未動 security_access.py 同檔風險低）；PR 必要擴增兩件（ctx key 進 INTERNAL_CTX_KEY_REGISTRY、4 條清值路徑補 policy cf id 側測試）＝機械必要非取捨、分層 commit。08-22 續：C2 已修（chore commit 僅 +3 行、7 檔 sha256 驗只有一檔變）——🔴 **同名旗標兩支 script 行為不同**（`--update-baseline`：repo-lint.py 重產全部 vs test_collection_audit.py 只產一檔；memory 已加限定範圍更正）；helper 形狀改裁＝**傳 reader 函式**（ctx_key= 參數版被實證擋下：rule 28 只認字面/常數讀取點、參數化會讓新舊兩把 key 一起掉 0——leader3 用 _is_sync_control 獨立驗證機制）；測試規模砍 13→4 新格＋9 條指名既有測試＋**四腿各一次拿掉 sentinel 的注入證明指名測試會響**（8 個論證涵蓋站點明列不沉默截斷）；stored related＝**#849 已開**（leader3 讀 fields.py 把機制從指認升級為已驗）。08-22 續二：leader3 自回收一個錯數字（「其餘 8 站點」由不含注入站的集合誤減而得；dev-798 兜不出 8 **沒有湊數**、改逐條列名回問＝正確處置；驗收要求改一律列名不給數）；leader3 另抓 dev-798 遷移清單疑漏一站（`:694` 同時是注入站與遷移站、14−1=13，注入與遷移不互相取代；逐條列名確認中——漏它的症狀正是 issue 危害同型「sync 成功但 cf id 存不回去」）。08-22 設計落檔＝`wt-798-workorder/798-design.md`（592 行、worktree 外不進 diff、非 scratchpad；13 站點與 leader3 獨立 AST 基準逐行對上、三個口誤行號已更正複驗）；**dev-798 裁＝留**（磁碟穩定、隱性量測脈絡未全進檔、決定可逆；⚠ leader3 拒用「dismiss 回 3Gi」——實測 project 目錄僅 3.0M、與 dev-836 案同向存疑，該估計已從看板約束行移除）。**GREEN 收工（08-22）**：19/19 綠（A/B/E 7＋C 4＋F 4＋E3 4）、7 顆分層 commit（守衛/遷移/fixture 各自獨立）、manifest 0 行；leader3 異尺獨立驗（repo-lint 32 條自跑、13 wrap 點自數、守衛本體逐行讀＝未偏離設計書）；rule 28 對新 key 的鑑別力用「字面 key 換區域變數 ⇒ exit 1 指名假綠」注入實證。兩個過程收穫已進 memory：遷移驗證腳本第一版 any() 聚合盲區被自己的注入抓到（聚合粒度 vs 缺陷粒度）＋舊名為新名子字串讓 grep -c 失效（裁不改名、改判準＝列名/AST）。F/E3 8 格裁不注入（成本＞價值、PR body 明講論證涵蓋）。**PR #854 ✅ 08-22 issue-leader 併**（head `f44c7b51`、merge `a72d325e`；當下重驗三 context 綠＋merge_base==develop＋token NONE；commit subject `fix: #798` 形狀裁不 rebase——repo 實測該形狀只產 commit_ref 不 auto-close、併後 #798 實仍 open 再驗證一次；模組測試 backstop＝merge 觸發的 develop push odoo-tests run、leader3 盯）。**全模組收官（08-22）**：#1（無 env）2F+3E of 3362＝#843 既有五筆的子集、本單 19 格全執行 0 失敗；#2（kubectl set env 後）2F+0E、消失恰為 3 筆 ERROR、反向 delta 空。**機制 B 定案＝3 筆（2 屬 TestNamespaceLeftSetUnion＋1 屬 TestBindingGuardDownstream）**；「CI 註解 2E」是單 class 口徑、非過期（leader3 AST 第三路確認 5/3 支）；pod printenv 證 local 確實不注入（create.sh 路徑無效、kubectl set env 有效、`.sandbox.yml:47` 非過期文件）＝#853 候選 1 對 local 成立。沙盒 destroy 獨立驗（ns 0、只清自己 node 副本、共用未碰）；⚠ 磁碟相對基準約 593MiB 未還（成因不追、數字附時刻紀律確立）。PR 開立中；**merge 類別裁定＝直接併類**（動共用守衛 helper 但結構性零行為改變已證：預設參數＝既有 reader、rule 28 注入實證、F1-F4＋全模組子集），Eugene 可否決。順帶發現已開 **#848**（三面查重零載體；🔴 leader3 自行更正可達性——錨點欄位在 resync 觸發組內 ⇒ 租戶寫錨點必過 check_access，非可繞縫；真問題＝守線是 #786 排序碼副作用、位置與契約不保）。⚠ 呼叫端口徑更正：共用 helper 在 security_access.py 呼叫端＝**4 個**（strip :506/:1479、reject :531/:1529），smart_home.py 另有同名複製組不受影響——issue-leader 先前寫 3 是把「3 份複製變體」誤抄成呼叫端數（口徑錯置）；陽性對照指名 4 個呼叫端。 |
| **#732** 🔒 | leader4 | 2026-08-22 | **Eugene 08-22 派**：owner 把自己那列改成 admin ⇒ workspace 永久 0 owner、租戶側不可回復（三道既有守衛蓋不到這條 write() 路徑；#556 只防 transient）。修法＝last-owner write guard；RED＝自我降級至 0 owner 先紅＋「尚有他 owner 時降級成功」陰陽對照；UX 選項（報錯 vs 轉移流程）若有取捨明標待裁。工單 scratchpad `dispatch-732-leader4.md`。08-22 assertRaises 廣播自查：11 條命中 1 已修 `68966a50`；docstring 更正 `9f37de75`。**沙盒輪收工（08-22）**：#814 probe-failed（全新 worktree 路徑不在凍結副本＝gate 正確拒絕）→ 標準 docker cp 正解重跑過 gate → **RED 5F/0E of 11 逐條＝預測清單且紅在對的斷言** → 實作後 GREEN 0/12 → 鄰域回歸 0/70（556/583/664/658 全 class）；destroy 完成（ns NotFound、專屬副本清、df 12Gi 同進場）。**PR #852 ✅ 08-22 已併**（Eugene 裁併撞衝突 → leader4 解：唯一真衝突 `src/tests/__init__.py` 三段 append 全留；baseline 不手解＝auto-merge 結果 vs audit 重產逐字同＋**對兩個 parent 各驗只增不減**（655+11=664+2=666）；⚠ 過程自抓一個比較腳本假事故（檔頭註解被當 class 名）；新 head `9c2de9aa` 三 context 綠 → issue-leader 併 merge `5fab7904`）。#732 收尾中（證據留言→關單→清理）。08-22：RED 已 commit（wt-732 `c49e4dd6`，3 缺陷向＋6 陰陽對照、#556 同款真 HTTP call_kw HttpCase）；**su/group_system 豁免＝issue-leader 判為 #711 同族例外格 ⇒ 本 PR 掛「⚠ 有取捨待裁」走 Eugene**（設計放行續做；PR 須補：租戶可達 sudo 寫 role 路徑的資源側枚舉、引 #711 正面寫風險形狀、防未來 sudo 呼叫者那格的選型）；沙盒 slot 排第 4。 |
| **#829＋#836** 🔒（同組） | leader1 | 2026-08-22 | **Eugene 08-22 派**：#829＝in-repo `woow-test.sh` FULLNAME 推導多接 `-sandbox`（腳本 vs chart 實產名不符）⇒ 每個沙盒測試受害，S 級、修好全隊受益，**沙盒 slot 優先給本單驗證**（要用前仍先報隊）；#836＝C2 `scan_expected` 的 `if n:` 讓 0 條數 TestCase 子類雙不列＝「排除一律列名」唯一反例（leader1 是 gate 作者）。一或兩張 PR 由實作者定；⚠ 動 baseline 記 #838：`--update-baseline` 重產全部四檔，跑完逐檔 `git show HEAD:<f>` 驗只增不減。工單 scratchpad `dispatch-829-836-leader1.md`。**08-22 leader1 量證 issue 根因寫錯**：真機制＝helm fullname `trunc 43`（三格矩陣，中間格 `-odoo-dev-san` 為決定性反例；照 issue 原修法會壞短 slug 格）——issue-leader 裁：更正留言由 leader1 於 dev-829 獨立重量一致後貼；**範圍裁 C**（本 PR 只修 woow-test.sh；e2e-seed／e2e-seed-reset-button／seed-from-stg 三支同族由 leader1 開新 issue Refs #829）；**DB 名裁 fail-loud 不加 fallback**（驗證載體不容「跑在別的 DB」假綠；fail-loud 無解格才升取捨走 Eugene）。⚠ 工單路徑少 `scripts/` 層、issue 對（現樹 `.claude/skills/k8s-dev-sandbox/scripts/woow-test.sh`）。08-22 執行：根因更正留言已貼（#829 comment 8727，dev-829 合成 slug 獨立重量一致、7 資料點；轉貼筆誤 wt929 已 PATCH 附註）；**#840 已開**（三支 seed 腳本同族，Refs #829，A3 已補列）。**#836 兩前提被量翻**：(A) 受影響 56 個非 3 個（含 4 個公開命名 class）；(B) 靜默格有兩處非一處（`_test_own_test_methods` 空→雙不列，吃 22 個）；守恆 737−653−6＝78 不依賴複製品、56/22 拆分待 dev-836 instrument 真函式重算。**⇒ excluded 註記 6→84 條、輸出契約改動＝有取捨待裁走 Eugene**（甲全併／乙只修點名格／丙分段輸出／丁獨立 self-test 線；leader1 與 issue-leader 皆傾向丙）。**08-22 天平補充輸入（dev-836 量測、leader1 覆核、口徑釘死＝body 的 C2 主張被證實、非第三翻案）**：C3（odoo-tests.yml 的 check_executed）構成**遲到且半套**的 backstop——PR 期間全靜默（該 workflow 刻意不掛 pull_request）、merge 後紅但訊息指錯修法、class 粒度擋不住「class 在但條數少算」⇒ 相對抬高丙/丁；含陰陽對照（C3 對合成洩漏 log 紅、C0/C4/C5 不動、check_baseline 三方向陽性對照過）。**裁：此輸入與裁示一起貼 #836，不先貼**（避免混進待裁討論）。dev-836 已招。**段一驗收過（08-22）**：同一性分割（S=U−E−X 純集合減法＋E⊆U/X⊆U/E∩X=∅＋「不該存在」格）取代基數守恆——補掉「重複計數互相抵銷」盲點；10 預測全中（P9：假集注入 ⇒ 真 C2 翻 FAIL、+56 行、ident 集對稱差 []）；56/22 名單兩組獨立推導逐字一致；RED 觸發形態掃 251 檔 0 苦主（**範圍限巢狀 statement 形態**，decorator/metaclass/setattr/exec 未掃＝潛伏未發作非不存在）；node identity 陷阱實測為真已 memoize＋G1/G2。段一保全完成＝commit `8988d4eb`（17 檔進 `scripts/audit/evidence/836-c2-silent-class-drop/`；leader1 驗收含**搬位後探針實跑**全數重現、RED 仍紅、不落 test_*.py glob；兩個自家探針假訊號已排除）**、dev-836 已 dismiss**（08-22；df 未見 3Gi 回收、與看板估計不符——延遲或估計不適用，照實記）。**獨立缺陷＝#850 已開（照 #846 規格）**：`_test_own_test_methods` 只算 class body 頂層 FunctionDef、Odoo loader 用執行期 `__dict__` ⇒ `def test*` 包在 if/try 內＝靜態算 0 執行期照跑——「條數怎麼算」與 #836「條數 0 怎麼列」是不同格，串起來（算 0→靜默出口→PR 無訊號→C3 半套）才是完整鏈。**#829 層一收工（08-22）**：RED 3/4＋GREEN 0/4（fake kubectl PATH 注入、斷言取呼叫記錄非腳本自印）；PC1 量出 issue 原修法代價（防回歸格綠翻紅）；5 個未量長度 predict-then-observe 5/5 命中＝機制釘死。層二計畫核可、slot 排 #822→#732 之後；**PR #843 已開**（head `8ce58b54`、單檔 +41/−9；auto-close 陽性對照 NONE）。**層二進行中（08-22）**：RED rc=2 且錯誤訊息 secret 名與層一離線 helm template 預測**逐字相同**（兩條互不相干路徑指到同一字串）；#814 如預測 probe-failed 非 content-drift；共用副本 digest 前後同（未碰）；host/node/pod 三邊 sha 同＋尺的陰性對照；dev-829 拒跑多餘 rm -rf（先驗存在＋判副本 vs 直通）。**層二收工（08-22）**：腳本層 RED→GREEN 成立（RED rc=2 錯誤訊息與離線預測逐字同；GREEN 讀到 DB 名＋exec 打中實名＝兩個待證事項皆立；pod 同一性由叢集事件釘死、pod B 覆量 sha 同）；⚠ 模組套件 5 筆紅**歸因中**（dev-829 明標「非本 PR 造成」是推論非量測；三條唯讀線在查：CI 歷史 3343 0F/0E 轉述查證／測試源碼 slug 相依性／baseline；紅名含 public_url_follows_seed_not_slug＝疑為刻意加長 slug 的沙盒特殊性）；destroy 乾淨、共用副本未碰、磁碟反升至 11.79GiB。PR #843 body 已更新（head 仍 8ce58b54）。**#843 body 已由 leader1 定稿**（機制 B class 歸屬＋2E 口徑澄清段＋機制 A 直接觀測值保全自 dev-829 scratchpad、來源與未重跑逐項標注；⚠ dev-829 在自己 cwd 所在 worktree 內執行 remove ⇒ Bash 整組失效自殘、由 leader1 代改 body；dev-829 已 dismiss——收尾經 leader1 物件驗證，含 `git branch -d` 假紅一例＝既有 memory 涵蓋不另記）。**PR #843 ✅ 08-22 已併**（issue-leader 執行：update-branch head `efa1f13a` → CI 2/2 綠（repo-lint 不觸發＝#846 白名單缺口、以 issue-leader 現地 32 條替代並留言說明）→ merge `00595e29`）。5 紅歸因收官＝機制 A 回貼 **#804**（comment 8791、不混機制 B）＋機制 B 開 **#853**（第一項工作＝分辨兩候選成因；含 318 landing 候選 2 描述可能過期的反證＋單沙盒決定性判別法）；dev-829 自加第 4 未驗（層二證據不涵蓋 update 後 head、兩 commit 本檔逐位元同）。worktree/分支清理中。🔴 **repo-lint 對本 PR context 不存在**：`.gitea/workflows/repo-lint.yml` 的 paths 白名單缺 `.claude/skills/k8s-dev-sandbox/**` ⇒ 動該目錄的 PR 不觸發 repo-lint（不紅不綠）——且 `repo-lint.py:4733` sentinel allowlist 列著該目錄檔案＝**在那裡新增開閘符號的 PR 會讓 gate 整個休眠**（白名單缺口＝**#846** 已開；過程誤開重複單 #847 已關並留言——成因＝查重腳本結論句是寫死字面量、與當次資料矛盾）。#843 併法判準（issue-leader 定）：層二綠＋dev-829 本機 32 條證據附 PR body＋**issue-leader 併前在 head sha 自跑 repo-lint 通過**＝三條件齊才併。**#844 已開**（判別式耐久載體：四支原碼全文進 body、golden 形狀＋待定項、🔴 golden 不守 chart 側漂移限制明文、repo-lint.yml 兩硬事實逐字引註；讀回 14401/14401 字元 13 標記全中）。**判別式落位裁乙**：本 PR 維持只改 woow-test.sh（保「直接併」類）；判別式另開 issue（leader1 開、Refs #829/#563 族）——必附：可用判別式原碼＋golden fixture 形狀＋🔴 golden 只守腳本側不守 chart 漂移的限制明文＋repo-lint.yml 兩硬事實（skills tests 完備性 gate 的宣告式迴圈、無外部依賴契約）。 |
| **收割批 ✅ 全收官（#841/#846/#850 三單三 PR 全併全關；#836 段二 ✅ **PR #872 Eugene 08-23 親併**（merge b9a2b5a0、develop tip d6e17a89；issue 關單收尾中）。**PR #871（#289 docs 修復）同批親併**（merge d6e17a89、dev-289-docs 收尾中）。原快照：PR #872 CI 三綠上桌（丙＝分段輸出；head cba5626f、base d9bad876；leader1 獨立複驗守恆 764/675/6/83＋token NONE 含陽性對照；issue 留言 9248 含當期口徑；dev-836 已 dismiss、wt-836 留到覆核完）。衍生 **#874**＋**#877**（repo-lint 檔頭「第 N 條」互引 40 處無 gate 對帳＝序號腐爛，與 #838 量詞腐爛同病兩形態）當場開（「名字在頂層但非 FunctionDef」條數少算、兩層 gate 皆綠的潛伏格；與 #868 修法形狀不同不併單）） | leader1 | 2026-08-22 | **#846 PR #861 ✅ 08-23 已併**（merge `c3bf6495`、head `1ecdd6f8`＝update 後、leader1 內容驗證新條目在＋修法有效由 #843 陰性/#863 自造陽性 context-存在性對照證明；衍生 #862（白名單無對帳機制）＋#865（repo-lint job 時間 8 天 63→118s 單調成長無人看、全期中位數是假值那格好）；#863 拋棄式 PR 驗完關閉、清理中）。**#841 PR #858 ✅ 08-22 已併**（test-only 併類、merge `ddff912c`、head `a6b8d466`；未驗段第一項＝守線自己零 CI（#856）；dev-841 亮點＝把 urllib.parse 命中列表並註明非 HTTP client（保表格可信度）＋發現 starlette 未 pin（經 fastapi 傳遞、可自漂 ⇒ memory (b) 價值升）；issue #841 關單中）。#846（只補 paths）→ #850 接續、每張重取 develop。工單 `dispatch-harvest-leader1.md`＋附錄 v2 |
| **#803** 🔒 | leader5 | 2026-08-22 | **Eugene 08-22 派**：Access 週期性收斂 cron（掃 access_protection_lost=True 重推、冪等、批次節流自選型）；⚠ 與 #798 同檔——#854 已併、排序前提解除；⛔ 真 CF 零寫操作。**08-22 五面調查＋推翻式複驗收官**（4 斷言被否證；patch target 修正＝三腿各自 client 路徑非 MOCK_REAP_CLIENT——照抄會讓否定斷言無聲通過）。🔴 決定性發現：cron interval 在 `<data noupdate=1>` 內＝**上線後改不動、選定即實質定案**（repo 兩處註解自證）。**題 2 issue-leader 裁 C**（游標式＋每輪彙總一行：掃 N 推 M 擋 K——不堵死、不靜默、不洪水；第一版不做時間基準、要做＝新欄位＋migration＋bump 連鎖、另開單）；**題 3 裁不開 force 旁路**（_resync_after_fail_open docstring 的正面主張＋#536 偽造面＋旗標是 UI 訊號非髒東西；SA suspended 是否等價 SH 503 列 teammate 必查、理由照實寫）；**題 1 Eugene 08-22 拍板＝B（15 分鐘，比照 reach_reconciler）**；⚠ noupdate=1 值即定案、五段推導寫進 XML 註解。dev-803（ccou）已代招。工單 `dispatch-803-leader5.md`＋leader5 的 workorder-dev-803-draft.md |
| **#740** 🔒 | leader2 | 2026-08-22 | **Eugene 08-22 派**：`_cleanup_tunnel()` 先於 unlink ACL 檢查 ⇒ delete 失敗但 tunnel 已死無法重試；修法選型自定＋正面回答 orphan reaper 為何接不住；⚠ 重疊面實測（leader2）＝**有重疊但不在 cloud_service.py**：smart_home.py（不同區段同檔）＋src/tests/__init__.py（必衝突）＋baselines（同值撞號家族）⇒ 先開先併照舊、merge 後 baseline 逐檔驗只增不減。**選型定案＝授權前置檢查**（第一個外部副作用前驗全部 unlink 權限；不採「副作用後搬」——會回退 cf-ns-orphan 事故修向；不採「事後補償」——不可逆資源補償≠預防）；範圍宣告＝不改 ACL/record rule/身分，修完租戶刪除仍失敗但失敗得乾淨（該不該刪得掉歸 #739 家族）⇒ 直接併類。**GREEN 收官（08-22）**：17/17 綠×兩輪、全模組 2F+3E of 3360（五筆逐條讀訊息歸因＝#804 兩＋#853 三、與看板清單逐格對上）、D1/D2 元對照（拿掉 no-op patch 兩條紅在自己斷言句＝有牙）、baseline 對兩方向對帳（6 行加總=17=獨立 AST 值）；commit `0dbd8ff8` +799 純新增；**分類裁定維持 issue-leader 覆核後併**（判準＝取捨存在與否、不隨嚴重度定性改——與看板分類原則同軸；C 形態頭條由 issue-leader 在併報中呈 Eugene）。merge develop 重驗：⚠ leader2 自更正基準值——merge 當下 develop 已是 `5fab7904`（#852 又進）非其先前轉述的 ddff912c；merge commit `df7da5ef`、`__init__.py` 真衝突手解（develop 三 import 在前）、baseline 685→691 只增不減＋--check-baseline PASS、manifest 對新 develop 為空；**動態重跑排 slot 第三位**（smart_home.py 被 #798 auto-merge +4/-1 落在相鄰路徑＝不能靜態放行）。**§1 量測收官（08-22，三形態×雙身分、真 HTTP＋operator mock＋零真 CF）**：issue 三格今天一格都不成立、缺陷仍在但換形狀——🔴 **C 形態是真頭條（issue 沒寫）：租戶按刪除→端點回失敗→但 CF 上 Access app 已拆、tunnel 沒動 ⇒ hostname 照常服務但登入強制沒了**（被拒操作實效＝自關登入保護）；A 形態＝`delete_tunnel` 成功後清錨點失敗 ⇒ guard raise「tunnel still live and billed」**是假話**（tunnel 已刪、operator 對 404 當成功）；唯一逐字對上 issue 的只有 state=deleting。⚠ 機制更正（leader2 自認、原轉述歸他）：tunnel_id 留著**不是** savepoint 回捲，是那次清 3 錨點的 write 因 `tunnel_token` 帶 group_system ACL 從頭沒落地、被 except 吞掉。第一擋點是**欄位級 ACL 不是 unlink**。**衍生 #855 已開**（DB 錨點指向已死 tunnel 對兩對帳方向結構性不可見；三層證據附 file:line＋基準 sha；刻意不 Refs #784——方向相反、body 明寫差異；未驗＝純控制流推導、補 tunnel 腿前置 #833）。dev-740（ccou）已招動工。**§9 發現（已進 memory）**：既有 test_C5 的 docstring 記著 #740 機制、斷言卻是 assertNotIn（對空清單恆真）⇒ 修好後照樣綠但理由換了、永不變紅——比 #824 型更陰；工單令 C5 不准改弱、強斷言進新檔。工單 `dispatch-740-leader2.md` |
| **#533** ✅（#525✅）**PR #866 Eugene 08-23 親併**（merge `e3ba0d0d` issue-leader 驗 origin/develop tip、issue #533 已關；**收尾全完成（08-23）**：wt-533／本地／遠端分支皆清（df 配對 +24516 KiB、歸因 inferred）、k8s/docker 殘留全零、他人 worktree 未動。🔴 leader4 刪遠端 ref 前抓到 **ref 已被 Gitea update-branch 改指 `bd060751`（非其 head `af66c771`）**——補驗該顆 is-ancestor 才刪；教訓＝刪除性動作驗「ls-remote 當下指向」非「記憶中的 head」（update-branch 是 merge train 常規、每張 PR 收尾都可能撞到；已入 memory）。🟢 18 條新測試在 develop CI 真跑：full-suite 3390→3408（兩條獨立路徑同給 18）、job 5243 全綠；Eugene 係等 update 後 head 三綠才併。#525 亦 closed。leader4 待命） | leader4 | 2026-08-22 | **Eugene 08-22 派**：吞例外家族——#533 讓 SerializationFailure 穿出去給 retry；#525 **範圍改判（08-22）**：COMMENT 9（08-04）自證八處已全數處置（PR #527/#540/#566/#568/#570）⇒ 改為**逐處實碼複驗＋關單**（leader4 唯讀 workflow 進行中；A1 已獨立驗到 savepoint 在 develop）——工單原「修 3 處」抄了被推翻的 body 分級、issue-leader 記帳。#533 缺陷確認仍活。**08-22 續**：#525 八處 41-agent 對抗式驗證全 ALREADY_FIXED（七張 PR is-ancestor 全過）；🔴 COMMENT 5 的 follow-up 第 1 條（_list_services log 讀已刪 record ⇒ MissingError 逸出，paas.py:3087 未改、#568 後危害降未消）**無任何載體在追**——裁 (b)：先開單再關 #525（正是「另案要當場開」教訓的即時應用）。**#533 範圍裁甲式擴張**：controller 吞掉 model 特意 RERAISE 的 OperationalError（reset_admin_password 傳播鏈斷裂）＋skip_trial_estimate 同型＝枚舉補到的同族站點，併入本 PR（明標 issue 未點名、每站各自注入；#799 家族關係寫 body 不進 PR）。技術定案＝加 `except OperationalError: raise` 即可、savepoint 不動（retrying 走 connection-level rollback 提供新 snapshot、與殘留半不互涵；對齊 _create_service:3415 先例）；⚠ issue 引的 95fea4ba 不存在＝rebase 殘留、真 sha c802daed（PR 引對的）。RED＝fail-once（__setstate__ pgcode 官方手法）＋always-fail＋雙陽性對照、比 #518 先例強一級——核准。 |

> **待派／派工政策（Eugene 08-23 拍板・五線分工）**：目標＝先功能上線。🔴 **08-23 Eugene 二裁：暫停分配新 issue——在飛的收尾到自然決策點、leader 收完手上不 pull 下一張、待命；恢復需 Eugene 明示。** **派工只從 ② 抽**；衍生守線單開單即貼 `derived`＋`pending` 凍結、開單≠排程，收割日（leader1 守門，衍生累積約 15 張或每週觸發）才批次處理。五線：leader1＝測試/docs/守線專責＋衍生單守門（#836 段二收完轉職）；leader2＝A4 功能 bug（#740 中 → #107 中文化 → #833/#860/#155）；leader3＝A1 租戶可利用洞（#282✅殭屍關單→#870 ✅ 已裁（08-23 Eugene 選 B：slug 單獨改 module-private sentinel、owner_id/ws_hash 不動、cloud_service_binding_guard 不跟；裁決全文＝#870 留言 9459、pending 已摘；實作等恢復派工後排程、工單必含三格未驗）；#289✅殭屍關單→#871 docs 已併；**#848 ✅ 全收官（08-23）**（PR #881 已併 706b6958、dev-848 四步保全後 dismiss；node 上 7 份孤兒 worktree 副本約 79MB→入 #886、收割日各原主認領；⚠ wt-36 host 側 08-23 已由 leader4 清（leader3 盤點當下仍活、無誤）⇒ 其 node 副本一併入 #886、leader4 認領）→ **#849 前提驗證中（08-23 leader3 重大發現）**：原機制敘述（寫 stored related 會寫回來源列繞過守衛）站不住——related 預設 readonly=True ⇒ `_inverse_related` 未裝 ⇒ 不寫回來源；**但結論可能更糟**＝值直接落在 stored 欄位（`security_access_id`:304／`smart_home_id`:295），而那正是 policy ir.rule 判歸屬的腿 ⇒ **歸屬分裂**（route 指受害者、SA 欄被寫成攻擊者）、且**繞過 #848 剛落地的 `_ANCHOR_FIELDS` 守衛**（那兩欄不在迴圈內）。⇒ RED 改兩段：RED-A 真租戶 RPC 寫 `security_access_id` 量 HTTP碼＋DB值＋ir.rule 實際判給誰（決定 bug vs 守線）、RED-B 僅在 A 顯示寫不進時才回注入式守線。**merge 類別等 A 結果**（若 bug 走 Eugene、非守線直接併）。sandbox slot 08-23 issue-leader 實測 0/2 空、磁碟 22.6Gi、已 go。preserve doc 三件修正批一張 PR 排 #849 後、載體 #886 留言 9423。**08-23 讀碼階段翻案（更正留言 9433，leader3 自背書 9202 後自我推翻、讀回 sha 逐字＋陰性對照）**：inverse 確定未裝（fields.py:449/455 related 未寫 readonly ⇒ readonly=True ⇒ :624 not(True..)=False；該 model 無 _inherits、兩欄全 repo 無第二定義）；危害往上＝寫 security_access_id 落在 policy 自身 stored column、route_id 仍指原 route ⇒ 歸屬分裂，ir_rules.xml:378/389 三腿有兩腿讀這兩投影欄＝被直寫那格就是授權判準本身。🔴 **#848 殘餘防線連跑都不跑**：resync 觸發集合（:929）不含 security_access_id ⇒ resync=False ⇒ check_access 排序迴圈不執行。既有結構＝兩套歸屬各自宣稱唯一（_anchor():395 讀 cs_id/sa_id/sh_id vs _access_anchor():707 讀 _ANCHOR_FIELDS，交集只 cloud_service_id）。⚠ **rule model 三欄同型**（security_access_access_policy_rule.py:57/65/71 全 related+store 未宣告 readonly）＝先掛板不開新單、待 RED-A 判定確認 bug 後傾向併入 #849 範圍（同機制同修法）非落單。**RED-A 暫停期繼續不叫停**（唯讀查證、產出＝bug/守線分岔判定＝給 Eugene 決策材料；預測清單 263 行沙盒前落檔 11P/7F/0E）；RED-B 暫停撤銷。**標題誤導待 Eugene 定**（現標題把錯機制寫最顯眼、更正在第二則留言、範本勝過警告；暫停期無新人派 #849 故風險窗口關）。🔴 **RED-A 量完（08-23）＝確認現行跨租戶安全 bug、已升 Eugene**：租戶真 HTTP RPC 回 200 無 error 把自己一列 policy 的 security_access_id 寫成任意整數 id ⇒ 值落地、route_id 不動 ⇒ ir.rule 歸屬翻到別 workspace；4 次重複形狀相同：攻擊者失 write／**受害者獲跨租戶 read（已量、前後對照 BEFORE read=False→AFTER read=True）**、攻擊後該列對所有租戶 write-locked（機制上短命 modified 會蓋回、但操作上持久只 superuser 修得掉）。D1 奪取門口被擋（name 陽性對照一起紅＝AccessError）、**D2 推給別人成立**。🔴 **比 #848 寬**：#848 需共同成員（讀得到目標錨點）、本單連這都不要，前提只 ⑴對自己一列 policy 有 write ⑵給得出整數 id（D2f：攻擊者看不到 wsC SA 照樣 landed）。prod v151 結構暴露（dev-385 量測未複驗、tag 上三格 leader3 驗）：兩投影欄在＋ir.rule 走它們＋#798/#848 守衛一道都沒上；⚠ 結構暴露≠已被利用、prod/stg 未實打。🔴 **最重未驗格＝配額 DoS**（_check_policy_quota 純讀碼零執行、若成立＝把自己列推到別人 SA 吃掉配額且 write-locked 刪不掉）＝明標未驗升 Eugene、沙盒留可立即補。處置：測試檔 771 行 commit 到分支當載體（⛔ 不開 PR）、沙盒 dev-sandbox-849split 留（TTL 08-24T03:55Z 兜底）、dev-849 停待命；rule model 三欄併入 #849 範圍（同機制已確認）。**RED-A 完整證據＝#849 留言 9445**（123 行、讀回逐字 sha256 2659277b＋陰性對照＋auto-close 掃描 NONE 配陽性對照；留言序 [9202,9433,9445]））。✅ #879/#881 已併（#879 merge 29f355e8＝今日最後一張；#881 706b6958）——CI status 前窗口 pending/total 0 或 contexts<全套時，判準須「期望 context 名字逐個出現」非只數量（前 ~12 輪 context 一個都沒註冊、任何 >=N 攔不住；issue-leader 08-23 誤判 #885「還沒跑」為「永遠卡死」＝同窗口）。原記：#881/#879 update-branch 撞 append 衝突（#876 #803 cron 08-23 併入 develop tip b8432ebf 後）——衝突僅 test-collection.txt＋tests/__init__.py 兩檔尾（三張各加自己 test class/import、無邏輯衝突）；序列化解：leader3 先解併 #881→leader4 基於新 develop 解 #879（#879 當前 head=60ff2a7a＝Eugene 覆核時已 update 過一次、四綠零覆核意見；#876 已併 develop tip b8432ebf；⚠ 任何 PR head 引用一律當下重查不沿用上輪值）；leader4＝A4/金流（**#36 ✅ 全收官（08-23）**：PR #879 已併（merge 29f355e8）、backstop full-suite 0F/0E of 3459 且 +4 恰為新增 2 class×2、Q9 改判已回寫 #34/#36、dev-36 dismiss（保全 note wt36-preserve 258 行 dismiss 後 sha 逐字同）、wt-36＋遠端分支清（陰陽對照、刪前 ls-remote 重釘 head）；**promote PR #887 ✅ 08-23 issue-leader 併**（merge bcf37935；覆核權 leader4 交 issue-leader、四 context combined success、develop 版號 175==head 未 bump、body token NONE、零 src/；landing docs/tracking/landings/36-change-plan-trial-warning.md＋兩支通用驗證腳本進 scripts/audit/＋6截圖2log sha 清單進 landing；🔴 leader4 更正前提＝odoo-tests.yml 無 pull_request、PR 階段本就只四 context、backstop 待併入 develop 後 push 觸發——issue-leader 原工單「等第五個 context」會等到不存在的東西、同 #885 形狀）→ **#887/#36 ✅ 全案收官（08-23）**（PR #879 功能＋PR #887 promote 均併、backstop 3459 先預測後撈數字對上、diff src/tests 為空）；**#864 動工前定位完成（08-23）**：23 支在 bcf37935 逐名成立（AST 非 grep、預測驗模型＝a72d325e 減 bcf37935 swallows 恰為 #533 三支、反向空）；🔴 issue body 三格待回貼更正——(1) base sha a72d325e 落後 develop 77 commit 且表格數字是含 #533 修法後的樹量的（body 標修法前 sha、不自洽）(2) **差點假重現**：舊 sha 上 scope=top 也得 23 但集合不同（含 3 支已修 paas.py、少 3 支巢狀 try）＝同值撞號、判準須集合逐名相等非數字相等（先列名才躲掉）(3) oauth2.py 8 支 @route 漏枚舉（全 no_try、對 23 支無實害但方法可重跑性有漏）；口徑 scope=all(23，對)vs top(20)差 3 支 issue 未定義。**A 級四支查證＋10-agent 對抗覆核完成（08-23、leader4 親驗關鍵行）＝升 Eugene 中**：四支判定維持（api_invoice_pay 最高優先／api_topup 不補-需重構／cancel／bind_result 可補）但**理由整批換**（dev-864a 機制多處錯、對抗覆核推翻：C1 對 api_invoice_pay 錯＝該支 try 內有同步 SQL account_move.py:3347 flush＋sequence_mixin:410 鎖＋res_partner:184 FOR UPDATE；C2 api_topup 兩方向都錯結論相同＝真機制是 :654 吞 OperationalError→:659 對 abort 交易下 SQL→InFailedSqlTransaction 不在重試白名單）。🔴 **真正該動＝兩報告都漏的現行金流 bug（不需並發、可能 prod 就有）**：A) api_invoice_pay 半成交（發票 posted＋點數扣＋無 payment＋無 reconcile＋顯示失敗、journal 沒設就觸發、#516/520/525 家族）B) bind_result 綁卡失敗前端仍收 ok C) skip-trial 發票**永不開**非多開（方向相反、default manual）。🔴 **#533 降 P2**（因果更正＝源頭是 #525 引入 savepoint 非 #533、被 vendored default auto_invoice=manual＋ecpay_demo_mode=True 擋、git tag --contains e3ba0d0d 零 tag＝不依賴 prod 版號證早於任何現存 tag）。**bug class 重定義＝判準改「try 內有無同步 SQL」非「有無 catch-all」**（leader4 寫 #864 留言、暫停期不改 issue 本體）。未驗環境前提＝prod company.auto_invoice/ecpay_demo_mode 值（決定 #533 定性＋半成交 prod 暴露、需 prod 唯讀查詢 Eugene 授權）。dev-864a 保全不 dismiss。半成交 A 獨立成單／api_payment_method_delete＋vendored payment_token.py:55 獨立列＝進 follow-ups 恢復後開。**#864 重定義證據＝留言 9453**（09:44Z、150 行、sha256 50379d57 逐字驗、issue 本體未動）。🔴 **新判準下四支乾淨翻轉**：原報告「三支沒攔到只 bind_result 攔到」實際反過來＝**三支攔得到、只 api_subscription_cancel（純 write 無外部副作用＝風險最低那支）inert** ⇒ #864 原敘述「多數支吞並發例外」方向是反的（⚠ leader4 標界：try 內有同步 SQL 是攔得到的必要非充分、還要 row 被並發改；invoice_pay 有 FOR UPDATE 證據、topup 的 Tx.create 打自己剛建 row 40001 來源弱）。dev-864a 保全完成不 dismiss（note wt864-preserve 依附 leader4 節點＋磁碟 issue/864-* 持久位置含 workflow-script.js＝14 agent 完整 prompt 下輪起點；未保全取捨＝transcript 5MB/base 27MB 可重建）。🔴 **追鏈補正 9457**：sh_subscription_fix.py 兩條入口三層都只查「錢包夠付」漏「不夠付」fallback、補查受 gate sh_invoice_generated=post(default draft)管⇒連 action_post 都不呼叫（結論不變理由不同、判準＝「if 的另一半走到哪」本單第二次同形狀）。🔴 **api_topup 最嚴重推論（未驗、需並發）**：40001 落 try 內→:654 吞→tx abort→:659 cache-only→:158 flush 對 abort tx 下 SQL→InFailedSqlTransaction 不在白名單→整交易回捲⇒**ECPay 已扣款但 topup＋payment.transaction INSERT 全消失**（比半成交更糟＝連記錄都沒有；prod 唯讀查詢驗不了、要並發實打＝下輪第一標的；未驗步＝pay_callback 拿查無此號 reference）。**第三格判準留言 9458**＝「例外吞掉後 commit 狀態是否自洽」。🔴 **機制根據＋兩個方向相反的子形態**（sql_db.py:162-166 clear() 五個呼叫點一般 try/except 都不觸發⇒dirty 值＋precommit callback 原封留著）：**非 DB 例外**（UserError）→tx 健康→commit flush 寫進 DB＝**半成交 A（多做了、DB 有記錄使用者看到失敗）**；**DB 例外**（40001）→tx abort→回捲＝**api_topup 扣款無記錄（少記了、DB 無記錄外部副作用留著）**。⇒ 半成交 A 與 api_topup **不是兩個獨立 bug、是同一判準維度的兩個方向**（使用者看到 vs 系統實際不一致）、都與 retrying() 無關⇒補 except OperationalError raise 對它們完全無效。**(c) 四支**：invoice_pay 不自洽（已驗）／bind_result 不自洽（已驗）／topup 待查（推論需並發）／**cancel 也待查**（推論＝mail_thread precommit callback 在 super().write() 前註冊、write 拋非 DB 例外被 :886 吞則 callback 仍留＝原以為最安全那支在 (c) 下也可能不自洽）。⚠ leader4 self-correction：初稿寫 clear() 「只有三個」實際 5（漏 TestCursor :604/:612）、而該留言正在要求「必須列名不得回數字」＝寫「只有 N 個」的當下就是該列名的當下；leader5＝A1 provision/tunnel 鏈（#803/PR#876 ✅ 08-23 Eugene 直接併（b8432ebf、06:04:52Z、三拍板題隨併入視為接受現狀、非退回覆核）；dev-803 四步保全後 dismiss（🔴 交接包/檢查器原在 wt-803 scratchpad＝worktree 內收割日會清、dismiss 前 promote 到持久載體）；保全分支 chore/803-preserve-followups（遠端 b7167c84、⛔未開 PR、⛔不可清）＝verify-cron-wiring.py +162 通用 gate＋交接包 landing +406＋follow-ups F-803-1/F-803-2＋docs/index.md 修正；剩 7 檔（含 4MB log）保全到 artifacts-803。🔴 **develop（bcf37935）docs/index.md 既有偏差＝655/landings69、實際應 656/70**：PR #887 併入時只更新總數漏 landings 格（issue-leader 併的時候沒抓到、gen-docs-index --check 在 git add 前重產故當下綠、非 required check 不擋 merge、產生器與檢查器共用同漏法——既有 memory reference_gen_docs_index_counts_tracked_files_only 有記）；修正已在 chore/803-preserve-followups、恢復後隨 PR 帶走、暫停期不開 PR、無人 base off develop 故不急；#822/PR#875 已收尾；**#385 ✅ 三步收官（08-23）**：stg 留言 9443＋#883 雙向 9450（讀 cloudflare.py:683 驗兩顆 tunnel 未軟刪、非只 docstring；測試層前置解除＝test_reconciler_classify 缺的測試現有實測期望值）＋產物保全 prod16/stg10 檔落點獨立驗（逐層到 / 無 .git）、dev-385 dismiss（三支 script sha256 與 leader5 context 先前獨立記值一致＝真交叉驗證）；🔴 **第三次 exec（cf_access_policy_reap 帳本）leader5 主動撤回**＝暫停令下屬實質推進且不影響本輪結論、留言「未查證」欄原樣；dev-803 保留等 #876 覆核但已令現在備份脈絡到載體（idle teammate 也會斷線＋覆核時程不可控）；⚠ #822 已全線結案勿再列——issue-leader 曾預告錯誤已認。🔴 #385 的「prod 跑 reconciler」**A 授權 08-23 撤回**：`reconcile_orphans(env)` 是模組層函式非 model method、`execute_method` 打不到（leader5＋dev-385 雙驗），且 inventory 回應不含 policy decision/include＝allow+everyone 格拿不到；等 dev-385 列真實路徑清單重升 Eugene，期間零 MCP 呼叫）→ **08-23 P5 runbook 重升、Eugene 授權步驟 1-5**（prod paas-system pod odoo shell 唯讀對帳、零寫入 CI-gate 保證、末行 rollback；硬條件＝gate E 記憶體餘裕<500Mi 即停防 cgroup OOM 殺主行程；CF token §4 另案另授權；第一發先報 issue-leader 確認）。`launch-first`＝#107/#36（Eugene 點名）。label 體系（A0–A5／cat-*／derived／pending／launch-first＋cat-out-of-scope 共 13 個）已建置（dev-labels）。🔴 **derived 定義（08-23 拍板）＝主題延續**：該單的**主題**是另一張單的殘缺／續集／同族延伸才貼；「做別的事時發現的」（出處）不算、出處交給 body Refs 慣例——出處定義零鑑別力（leader1 量 18/18 全中＝不鑑別）。🔴 **抽單鍵改判（08-23 二裁）＝derived∧¬pending**：pending 保持它自己的 description 語意（卡決策待拍板），衍生單的「不排程」由 derived 本身＋政策表達 ⇒ 衍生∧沒卡決策＝收割日可直接動工；derived∧pending 交集（12 筆）＝衍生且卡決策、收割不抽、走 Eugene。每個 label 語意單一、新貼 10 張不需補 pending。定義變更後的標籤修正由守門人（leader1）執行、可 remove 逐張記理由。🔴 MEMORY.md 索引行**新增凍結**（距硬上限剩 101B、超限靜默丟尾端；要動先報 issue-leader）。（08-22 第七批（收割日＋主戰場）：收割批→leader1、#803→leader5、#740→leader2、#533+#525→leader4；#798 leader3 在飛。第六批全數收官：#824✅ #732→PR#852 #822→PR#851 #829✅。）（08-21 第三批：#805 (b) → leader5、#546 → leader1；第四批：#648 → leader2、#531 → leader3、#514 → leader4；五位全部在飛。）（08-21 第二批：#691 → leader1 **PR #817 已 merge、issue 自動關**；#814 → leader3 **cc/woowprd PR #4 已 merge、plugin 已重裝 `951f49c4`、issue 已關**；#815 → leader4 **PR #818 已 merge、issue 自動關**；五位 leader 全部待命。）（08-21 派出並全部 merge：#787 A 段 PR #812、#806 PR #808、#785 PR #813、#684＋#544 PR #811＋#816、#805 (a) PR #810；五位 leader 全部待命。）

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

# ② 實作類（86 筆）— 可推進上線

## A0 · prod 事故（1 筆）

> #512／#441／#442 經查證屬叢集運維，已移到文末 ⊘ 段；#503 與其復發防治 #694 均已關單——殘餘承接：租戶側偵測走 **#702**、marker 告警走 **#714**（皆在 ②A4／②A3 有列）。

| Issue | 一句話 |
| --- | --- |
| **#389** | prod SMTP 寄信全 exception；歸屬已查證＝問題在平台本體 Odoo（ns `paas-system` 的 paas-odoo pod）從頭到尾沒有設定過任何 outgoing mail server，不是客戶站（根因細節見留言 4434）。修復未執行、卡在決策：用哪家 mail provider、帳密由誰提供；之後才是建 `ir.mail_server`＋設 company email／catchall domain |

## A1 · 資安：租戶可實際利用（26 筆）

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
| **#289** | project↔CloudService 綁定授權缺 model-level guard |
| **#282** | `workspace.slug` 補 write guard（identity-anchor 不可變） |
| **#706** 🆕 | 承 #698／PR #705 的既有綁定盤點——`cloud_service_binding_guard` 只擋新寫入、不回溯，既有跨 workspace 的 project↔cloud_service 綁定不會自動消失，且經 `_reconcile_agent_runtime_reach` 餵給 agent pod 的 egress allowlist 與 `reach-ws-*` label 的 reach 授權照樣有效。跨 workspace 綁定本身是合法設計、無法機械判定攻擊或合法 ⇒ 本單只做唯讀盤點，盤完才決定要不要清、怎麼清（若需 migration，照 #691 新制 bump＋目錄同號，見上方工作原則）；讀 prod DB 那段走 `woow-operator` agent |
| **#787**（資安追蹤；A 段 ✅ 交付、leader2 08-21 收工） | context-key 偽造家族普查（承 #536／#788）：A 段 5 把 truthy 閘**全部 enforced**（registry pending 已空）——`cs_skip_access_sync` PR #793（merged）、`sm_*` 三把 PR #795／`ccd987b2`（merged）、`woow_force_issue` **PR #812 已 merge（08-21 11:39Z）**（.174；portal 身分真 call_kw 可達、fail-safe 方向相反＝不開立且必須吵、ECPay cron 只撿 pending/failed ⇒ 靜默早退會變「帳本 issued、ECPay 空」；獨立覆核七點證據在 PR 留言 8313）。衍生：`test_acl_p9` 測試名誇大（#787 留言 8311，待開單）；B 段由 #783 承接。issue 待 Eugene 關或續追。 |
| **#798** 🔒 leader3（08-22 派；承 #786 覆核開） | `cf_policy_id` 無任何欄位級寫入守衛（無 `groups=`、無 identity-anchor、ACL 1,1,1,1；route 側同類欄有三件套）⇒ 租戶裸 RPC 對自己 workspace 的 policy 列寫 `cf_policy_id`，而 #792 `_trusted_ids_for()` 把列上值當 trusted ⇒ 繞過 #781／#792 為 #783 立的防線（同 sink 另一腿）。今天可達的是自傷格（真 policy 成永久 CF 孤兒、靜默）；跨租戶刪需帶外取得 id。develop 既有形狀、非 #796 引入；修法三選一未選型（第 2 層 ⇒ 留 ②）。 |
| **#822** 🔒 leader5（08-22 派；leader5 自開、承 #805(b) 盤點） | SA `action_provision` 第 2 步（identity write）失敗時 CF `create_tunnel` **零補償** ⇒ tunnel_id 沒落地、冪等守衛失效 ⇒ 每次重試多一顆計費孤兒 tunnel；無自癒路徑；🔴 對 #385 的意義＝79 顆候選的**產生源**之一、清掉會再生；sudo 修法（#805 PR）只消 AccessError 這條觸發，其他失敗形狀仍在。全靜態推導未實跑。 |
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

## A3 · 發版鏈／CI／部署環境（23 筆）

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
| **#829** 🔒 leader1（08-22 派；leader3 開） | in-repo `woow-test.sh` 的 `FULLNAME` 推導多接 `-sandbox`（腳本 `${SLUG}-odoo-dev-sandbox` vs chart 實產 `${SLUG}-odoo-dev`）⇒ **每一個 kubectl 目標都算錯**（secret 那步先退出、deploy 幾處還沒機會浮現）；CLAUDE.md 正指路大家用它＝照文件走必撞（與 #563 同形）；繞法＝plugin `test.sh`（≠修好）；修法須一次驗 FULLNAME 全下游。Refs #531 #563 |
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
| **#846** 🆕（08-22 leader1 開；#843 CI 觀測衍生） | `repo-lint.yml` paths 白名單缺 `.claude/skills/k8s-dev-sandbox/**` 而 repo-lint 的 sentinel gate 掃描 roots 含該目錄 ⇒ 只動該目錄的 PR 不觸發 repo-lint（context 不存在、不紅不綠）＝gate 對它休眠；與「repo-lint 非 required」是兩層互相牽制的休眠（對照表在 body；補白名單只解第一層且讓第二層更難走）；Refs #829 |
| **#836** 🔒 leader1（08-22 派；dev-819 開） | `test_collection_audit.py` C2 的 scan_expected `if n:`——條數 0 的 TestCase 子類既不進 baseline 也不進 excluded 註記＝該檔自宣告「排除一律列名」的唯一反例；今天無害（三個底線基底本來 0 方法），但判準 bug 把有方法的 class 算成 0 時它會安靜消失而 C2 無感；與 #828 分格＝彼丟整棵樹、此丟單一 class。⚠ 未查 closed issues。Refs #546 #828 |
| **#837** 🆕（2026-08-22 leader1 開；run 4763 首趟後） | test-collection gate 首趟落地後兩筆帳：(a) baseline 檔頭「尚未與真實執行清單對帳」已被 C3 653/653 證偽＝過期敘述 (b) green-streak.txt 未建、run 4763 是第一筆合格紀錄、append 是刻意人工步（CI 自動 commit 會成觸發迴圈）——誰 append／不做／自動化 guard 形狀待 Eugene 裁。查重含分頁陷阱迴避（130 張 open 全掃、鄰居 #467 #836 #828 #821 皆已分格）。Refs #546 |
| **#838** 🆕（2026-08-22 issue-leader 開；dev-648 保全收整） | repo-lint `--update-baseline` 一次重產**全部** baseline——靜默調降風險 in-code 註解已自認兩處（`:1738`／`:3881`，因此那兩份清單刻意不放 baselines/）但四檔仍暴露、「只增不減」自保程序無文件；修法三選項列於單內。⚠ 未查 closed issues。Refs #832 #835 |
| **#731** 🆕 | 五支唯讀腳本 `--context` 可選 ⇒ #32 把「靜默打對」換成「靜默打錯」；刻意不做強制，待裁決收斂方向 |
| **#722** 🆕 | onboard profile `KUBE_CONTEXT`＝`docker-desktop`，`create.sh` 不帶 `--context` 靜默打到本機叢集；與 #32 相乘非重複；`kubectl config use-context` 仍在兩處 ⇒ #32 Part 2 皆未修 |
| **#714** 🆕 | #694 filestore-hashdir-guard 刻意 fail-open，四個 marker 只印 initContainer stdout ⇒ 與正常無法區分，要接告警（🔴 硬約束三條）；僅平台 paas-odoo，租戶側走 #702；Eugene 2026-08-13 核准另開、不阻擋 #694 關單 |

## A4 · 功能缺口／bug／UX（33 筆）

**壞掉的功能**

> 🔴 交叉指標：#805 分流在 ③ 政策，若裁成「SA 也是 bug」危害就落在本段——SA `action_provision` 與 #739 修法前的 SH 同形（非 sudo 寫 `tunnel_token`），是不是 #739 的 SA 版要先實查入口再裁。

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
| **#533** ✅（PR #866 08-23 併，merge `e3ba0d0d`） | `api_service_skip_trial` 吞掉並發類 DB 例外——`SerializationFailure` 被 `except Exception` 收走，retrying 永不重試 |
| **#525** ✅ 已關（08-22 15:11Z；八處全 ALREADY_FIXED、驗證留言＋is-ancestor 全過） leader4 | paas.py 吞例外→靜默 commit 同構縫（`priority:high`）：3 處確認該修、5 處形狀同構因果不同 |
| **#363** | reset saga 並發：client 逾時後重按會與 in-flight saga 競態，rollback 可能覆蓋較新密碼 |
| **#342** | change plan 後 CS 規格 re-apply（#332 殘段） |
| **#702** 🆕 | 承 #694：租戶 `svc-*-odoo` root-owned 資料目錄偵測（Eugene 已裁決先偵測不預防）。唯一 root 路徑是人工 `kubectl exec`、無守衛收斂；只建唯讀掃描器，判準只認 owner（mode／group 恆綠）、讀 `/proc/1/status` 取真實 uid、範圍含 filestore 與 `sessions/` |
| **#740** 🔒 leader2（08-22 派） | **租戶 delete 回失敗、但 Cloudflare tunnel 已先被刪、`tunnel_id` 清空後無法重試**：`_cleanup_tunnel()` 跑在 `super().unlink()` 的 ACL 檢查之前 ⇒ DB 列還在、tunnel 已死，沒有東西能驅動重試；既有 orphan guard 不會響（方向與 `cf-ns-orphan-no-account-gc` 相反）。修法：ACL 通過後才送外部呼叫，或做成可對帳狀態 |
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

# ③ 需討論類（28 筆）— 需求／策略未定，別自行開工

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

# ①b 固化自動驗證（26 筆）— 暫緩，不投 token

> **政策：先問 Eugene 要不要做，並附成效評估。** 這些是可程式化、跑一次就永遠替你重查的守線，**價值在防退化，不在推進上線**；整段冷凍是因 08-01～08-05 的 token 大量流進這裡而 production 沒前進。**解凍時機：實作類的 A0/A1/A2 清完，或某條真的擋住上線時單張破例。**

**成本刻度**：S＝一個 session 內／M＝一天／L＝跨多天。

## 甲 · 「測試到底有沒有在跑」的完備性（4 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#467** | 2 個測試檔未被 `tests/__init__.py` 匯入 → 15 條從未執行（`priority:high`）。🔴 現況：那兩檔已補進 `__init__.py` 且 class 是 `TransactionCase` ⇒ 第 1 步已落地，只剩第 2 步「機械守線」（防新增測試檔漏 import 照樣全綠）；開工前先重查，別重做修法。 | **S**（僅剩守線） |

## 乙 · 測試品質／守線形狀（12 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#828** 🆕（08-22；#830 衍生） | repo-lint test-collectability 換 root 即靜默全綠——`_addon_source_tree_present` fail-open 排在所有存在性反向對照之前 | — |
| **#841** 🆕（08-22 leader2 開；#824 覆核衍生、**現成修法已附 body**） | operator unit 測試對真 CF outbound 零阻擋——洩漏靜默（測試照綠只是變慢）且成因是**被測碼**改動非測試改壞（類別級風險）；附實測：封鎖器裝上全套仍綠＋陽性對照有鑑別力；integration 側必須例外且例外格要自有測試（設計問題明標未代決） | **S** |
| **#844** 🆕（08-22 leader1 開；#829 判別式落位裁乙產物、**可用原碼全文已附 body**） | #829 判別式（helm trunc 機制、5/5 預測命中那套）無耐久載體——落地形狀＝golden fixture、待定項與 🔴「golden 只守腳本側、chart fullname template 改動不會被發現」限制皆已明文；Refs #829 #563 | M |
| **#868** 🆕（08-23 leader1 開；#850 姊妹三腿） | repo-lint 三支判準「宣告要鏡像執行期、實際只掃頂層」（tree_facts 只收 module 頂層 ClassDef／inherited_flag／test_tags 只掃 class body 頂層）——腿 1 比 #850 更徹底（class 連 fact 都不建、到不了 #836 的出口）；五格家族表＋修任一不連帶；現樹 0 苦主（254 檔 parse 失敗 0＝0 有效；範圍限巢狀 statement）；⚠ 執行期側未做 #850 等級的 exec 驗證＝**同族不等於同強度**（已明寫）；Refs #850 #836 #862 #684 #356 |
| **#850** 🆕（08-22 leader1 開；#836 段一衍生） | repo-lint `_test_own_test_methods` 只算 class body 頂層 FunctionDef、Odoo loader 用執行期 `__dict__` ⇒ 巢狀 `def test*` 靜態算 0 執行期照跑——**判準 docstring 自宣告「＝loader 篩子④那組」而實際非鏡像**（與 #846 同族：自宣告要守、實際沒守）；現樹 0 苦主（範圍限巢狀 statement 形態、decorator/metaclass/setattr/exec 未掃）；三格關係表（算 #850／列 #836／誰跑 #846）修任一不連帶修他格；Odoo 側刻意用符號定位不寫行號（兩棵源樹）；Refs #836 #846 | M |
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
- **第一順位：#684（併 #544）** — 三個支點現成（pattern-guard-map `test-discipline` class／repo-lint 既定形制／#467 同型修法），防的退化最貴。⚠ 前提：先合單重量；改基底冒出的真紅是產出不是意外、別在同一張 PR 順手修；**gate 的第一個測試必須是注入紅燈**。
- **第二順位：cd#76** — 全段成本最低而後果最重；⚠ 執行歸屬 `woow-paas-cd` agent，本 repo 只出規格。
- **第三順位：#496**（直接複製 #495 寫法）**或 #595 的 3b**（只改輸出、一個 session 內可完）。
- 刻意不入前三：**#438** 原型不在 repo 內、實作等於重寫。（原第一名候補 #546 已於 08-22 收官結案）

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

> ✅ 2026-08-18 ②／③ 存量追溯已跑一輪；**兩可 5 張仍待裁決**：#567／#525／#750／#383／#412。

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

- **develop `3e21b890` / `18.0.1.0.175`**（08-22 06:48Z，PR #831 進、#805 自動關）（2026-08-21 14:46Z 量，open PR 0；#817（#691 版號改制）與 #818（#815）已進、兩張版號照新制**不動**；本日 #808→#811→#813→#810→#812→#816→#817→#818 八張全進，#806／#785／#684／#544／#691／#815 自動關）。量法＝兩條獨立路徑交叉：`tea api …/branches/develop`＋`contents/src/__manifest__.py?ref=develop`（base64 解碼）對照 `git ls-remote origin develop`；⛔ 不要用本機工作區或本地 ref 當現況（`ahead=0` 不代表同步、30+ worktree 共用 `.git`）。
- prod 現跑 v151（見頁首）；平台 paas-odoo 無 stg 對應環境。

