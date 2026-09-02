# PaaS 上線推進看板（2026-08-21 精簡版）

> **本 note 只放 open issue 的分群結果**，供 Eugene 決定先做什麼。歷史過程、驗證證據、教訓段一律不放——它們在各 issue／PR 留言／`docs/`／memory 有持久載體，這裡只留指標。
>
> **對帳重算法（給推導方式不給死數字）**：`tea api -X GET "/repos/{owner}/{repo}/issues?state=open&type=issues&limit=50&page=N"` 逐頁抓到「該頁不足 50 筆」為止，再與看板表格**第一個 cell** 的 `**#N**` 取集合差。六個 repo：main `odoo-addons/woow_paas_platform`、ci `woow-paas/paas-odoo-ci`、cd `woow-paas/paas-app-bundle-cd`、charts `woow-paas/woow-paas-charts`、ar `woow-paas/paas-agent-runtime`、sh `ha-components/woow_paas_smart_home`。
> 🔴 `limit` 被 server 截到 50（不逐頁會靜默漏抓）；🔴 不帶 `type=issues` 會把 PR 算進來；🔴 owner 猜錯回 404、與「issue 不存在」分不出來；🔴 live 快照有效期是分鐘級，寫入前重抓。
>
> **發版現況**：prod 現跑 **v18.0.1.0.151**（08-18 rollout，證據在 cd PR#116 留言 7773）；落差基準以 prod DB 實裝版號為準，不寫死。
> **上次修剪**：2026-08-21 12:10Z（issue-leader）——刪本日已關的 #806／#785／#684／#544 四列；同日稍早——刪 20 張已關、補 9 列（7 張未列 open＋#777／#787 原無自己的列）、全段壓縮（166KB→約 54KB）；修剪前全文存 `docs/archived/paas-issue-memory-2026-08-21-pre-clean.md`。

## Leader 工作原則

- `issue-leader` 負責分配 issue 和改本 note；Leader 對本 note **唯讀**，要改請 `issue-leader` 改（一律 `maestri note edit`）。
- Leader 一次只做一個 issue（或同組相關的 issues），不排程無關工作；全部完成後 git pull 並 /clear 才接下一張。
- 取用時請 `issue-leader` 在「🔒 認領中」登記＋在該 issue 列標 🔒；完成請 `issue-leader` 兩處移除。**無登記＝無人處理。**
- 不把本 note 當工作記憶；要工作記錄自己 `maestri note create`。
- 本 note 定期清除已完成 issue、同步 `tea` 可讀到的 open issue，不留過時記錄。
- 🔴 **manifest 版號判準**（#691 改制；PR #817 2026-08-21 merged → develop `b521bc70`，gate 已上線＝develop 唯一 required check）：**A. 一般 PR 不動 `src/__manifest__.py` 版號**——必須**等於**當下 origin/develop（併完 develop 後 `git diff origin/develop -- src/__manifest__.py` 須為空；版號 > develop 而非下列兩類 ⇒ gate 紅）。**B. 只有「新增 `src/migrations/<X>/` 目錄（base 側 tree 不存在）」的 PR 才 bump**：元組比較嚴格大於 develop、且本 PR 新增的目錄至少一個 X == 新版號；新增目錄卻沒 bump ⇒ 紅（`migration-needs-bump`）；只改既有 migration 目錄＝一般 PR、不 bump。**C. 發版才 bump**：`release/*` 分支（`/woow:deploy` 產生）嚴格大於 develop，相等 ⇒ 紅（`release-needs-bump`）、不吃 out-of-scope 豁免。B／C 取號＝`max(develop, 其他在飛且會 bump 的 PR head 的 manifest)+1`、多人同時由 issue-leader 指定、量完立刻 commit；被發版 PR 追過 ⇒ gate 紅、重 bump＋目錄同步改名（預期中的**可見**競爭，不再靜默）。版號低於 develop 一律紅（＝先 `git merge origin/develop`）。權威：`scripts/ci/manifest-version-gate.py` docstring、`docs/guides/development/merge-train-acceptance.md` §1。⚠ 手上分支若照舊制已 bump，**改回 develop 現值**再 push，否則 gate 紅。
- ⚠ **磁碟暫時性約束（2026-08-21 起，Eugene 清完剩 15Gi／99%）**：同一時間只讓一位 leader 跑 k8s 沙盒／拉 image，起沙盒前 `maestri ask "issue-leader"` 排隊；收工立刻 destroy 沙盒＋dismiss teammate（teammate session 是磁碟大戶，dismiss 一個實測回 3Gi）。磁碟回到寬裕後由 issue-leader 刪本條。
- 🔴 **docker-desktop 沙盒 hostPath 內容 gate（#814，woow-dev plugin `951f49c4` 起，2026-08-21 已重裝生效）**：local 模式 `create.sh` 逐條比對 hostPath 的 host/node 正規化 digest，**不一致或探針失效都拒建 rc=1**。紅了＝node 上那條路徑是凍結舊副本（凍結點 ≤ node 容器建立、重啟不刷新）或探針壞了，不是你的碼有問題；正解＝`docker cp` 進**自己專屬**的 node 路徑再建（別覆蓋共用 `paas-odoo-ci/addons`），逃生門 `--allow-stale-node-copy` **不准自行使用**、要先問 issue-leader。驗「沙盒測的是我這份碼」看 host↔node↔pod 同檔 sha，不看路徑存不存在、不看 create.sh exit 0。✅ **共用副本已刷新（08-22，leader2 執行）**：9 模組漂移全 0、全樹 digest `4c5531a3…`（host==node；`.bak` 對照不同值＝判準有鑑別力）；舊副本留 `addons.bak-20260822`（node 內 19M，刪除由 Eugene 裁）。⚠ 三個殘留注意：**驗 digest 必帶 `LC_ALL=C sort`**（BSD/GNU 排序不同 ⇒ 假漂移）；同族：macOS `wc -c` 補前導空白、busybox 不補 ⇒ 跨 host/容器逐檔比對「全部不同」可能比的是格式不是內容（`tr -d " "` 收斂）；**刷新前已掛載的 pod 看的仍是舊 inode、要重啟**（本次 0 pod 無此情形）；歷史上在 docker-desktop 驗過的**計費相關綠燈要重驗**（舊副本缺 wallet migration＋billing SSOT 測試）。隔離副本過渡作法退場、新沙盒直接用共用路徑（#814 gate 續守）。⚠ **誘餌目錄**：`~/gantry-47/paas-odoo-ci/addons` 是停在 0.0.4 的舊 checkout（與正版 `~/Documents/woow/woow-paas-repo/paas-odoo-ci/addons` 尾段同名、兩邊都有合法內容 ⇒ 路徑打得開零鑑別力）；查第三方 addon 一律用**內容**（版本/mtime/digest）驗前綴，清掉或標 STALE 待 Eugene 裁。

## 🔒 認領中

| Issue | Leader | 起始 | 狀態 |
| --- | --- | --- | --- |
| **#546** 🔒 | leader1 | 2026-08-21 | **Eugene 2026-08-21 裁**（照 issue-leader 建議，issue 留言 8460）：8233 §6 A＋B 照做——B＝以 Eugene 分支 `ci/run-odoo-tests`@`4fc1c7ee` 為基礎（commit 數自己量：`git rev-list --count $(git merge-base origin/develop origin/ci/run-odoo-tests)..origin/ci/run-odoo-tests`，08-21 leader1 量得 15；留言 4771 的 27 是別的基準）merge develop 開 PR（觸發沿用 08-03 拍板 B+C：develop push＋cron，不掛 PR、不 block merge），與 #684 repo-lint gate 重疊處以 develop 為準；A＝收集完備性判準以 Odoo 測試進套件、跑在 B 下（可拆第二張 PR）。觸發全套前先問 issue-leader（runner capacity 1）。**B 段已交付 PR #819 ⛔ 待 Eugene 覆核**（head `55f004ec`、三 CI 綠、6 檔、0 Closes、manifest 不動、merge-tree 乾淨；issue 留言 8476）；待裁 A＝`check_upgrade_readiness()` 目標改為「兩狀態滿足時提醒回 #546 重裁觸發策略」（動到 4751 設計、Eugene 裁）；全套 `workflow_dispatch` 第一趟已跑（run 4629，head `70d3f7a5`，11m48s vs 4751 記 6 分）：**測試 0F/0E of 3277**（#816 新執行到的那批全過）、C0 PASS、**C5 FATAL**＝唯一 known-failure（#543 刻意紅的 `test_cursor_bypass_flag_cleared_after_migrate`）這次**過了** ⇒ exit 2 短路 C2–C4；**歸因完（08-21）＝(a) #543 真修好**（`0fd3ba67` 08-03 加 `cr.sa_skip_bulk_grant_check` bypass 三處、`9b1c084b` 同日修好探針「從未跑到 migrate()」；哨兵測試是空虛安全確認——只 assertFalse flag、不斷言 rule 建出 ⇒ 對「修好與否」無鑑別力）。**Eugene 08-22 裁：三件照建議**（issue 留言 8497）＝**四項已落地**（self-test 4 陽 8 陰、known-failures 已空**哨兵行保留**、哨兵補 rule 建出斷言、upgrade_readiness 定案零改動；FAILURE_LINE 拿到第一次真實紅燈陽性對照、#543 判準憑 WARNING 跨層佐證成立——**皆本機證據，#543 留言明標非 CI**）；盲區另開 **#821**（ ⇒ C5 報綠，Refs #546 #684）；**run 4679 端到端全綠**（17 step 全過、C0/C2/C3/C4/C5 PASS、0F/0E of 3277、11m34s＝streak 第一筆）；但證據擷取步驟被抓到**按位置截斷**（ 只留 install 前 20 秒的 WARNING＝761 筆的 5.3%，p_rule migration 那段全被截掉 ⇒ 「grep 回 0」是探針失效非證據）——修法已放行＋**併修 streak 灌水縫**（summary 主動邀 append 但不判事件型別/ref ⇒ workflow_dispatch 也會被人工抄進「develop push 連綠」檔＝可照抄範本勝過正文警告同型；非 develop push 改印「不計入 streak」）。🔴 **更正：#543 的 CI 級佐證其實已在 run 4679 驗到**——載體是哨兵測試自己的斷言（migrate 前 0 條 rule→後 1 條 include/email_domain ⇒ 兩條分支都走到且守衛未 raise），不依賴被截斷的 WARNING dump（那只是 cross-check）。**段 B 收工**：run 4691 兩判準過（17 步全綠 0F/0E of 3277＋兩條指紋落在測試中段、INFO 級那條是舊形狀結構上收不到的＝判別式可用）；**#543 已關**（證據留言 8583、關單說明 8587 引 Eugene 裁決）；PR #819 head 凍結 `9a5ab24c` 待 Eugene；`wt546-*` 容器清理中；**A 段交付＝PR #830 ✅ 08-22 已併（merge `d4290c25`）**（head `48ede57d`、2 檔 +516、CI 綠、版號不動、#546 留言 8618；gate 自己滿足自己的判準＝8233 §6 自洽；「先證探針活著」排在所有否定性判準之前且 assert 紅＝#828 的反面示範；四項未驗誠實列 §8 含「兩通道對現樹都回 0 零鑑別力」）。原裁決＝走 (c) runtime introspection：不搬 repo-lint AST（實證搬過去＝永遠綠——`_addon_source_tree_present` 對容器版面 fail-open 且先於所有反向對照；lint 側無 odoo；hook 模式 except 靜默 exit 0）；兩通道定位寫死＝runtime 是 ground truth、AST 是 PR-time 早期鏡像，分歧是資訊不是歧義（同 #819 的 C2 vs C3）。衍生：fail-open 已開 **#828**（換 root 即靜默全綠；控制變因兩棵樹同 sha 測試檔；不猜 blame、標明今天無實際損害）；⚠ 我方 agent 未授權 `docker start` 他人容器 wo787-base（mount 源已被清＝空目錄）事故已認、處置＝停回原狀。PR head 現 **`f3b1988c`**（08-22 sync develop：解 `src/tests/__init__.py` 與 #831 的同位 append 衝突、兩段都保留；CI 綠＋mergeable=true＋manifest 逐字＝develop，issue-leader API 複驗一致；leader1 本地用 repo-lint job 完整指令序列預跑）。**merge train 08-22 全數收官**＝#825✅ #826✅ #830✅ #832✅ #819✅（merge `16b5fc04`）。#819 最後一關：head `28430e0d` 是 Eugene 的 update-branch（非 leader1 push），自帶新 workflow `test-collection-audit` 首跑 C2 紅——ccox agent `dev-819` 修復（紅因＝update-branch 帶進 17 個新 test class 而 baseline 未重產；17/17 逐條對到來源 PR＋ast 獨立計數＋三形狀陽性對照後 `--update-baseline`，head `f657a4b4` 三 context 全綠後 Eugene 併）。develop 版號全程 `18.0.1.0.175` 未動＝#691 改制後首次整列 merge train 驗證。收尾：**#546 已關**（09:21Z，證據留言 8709/8713）、分支/wt-546 已清（陰陽對照齊）、**run 4763 第一趟真實 B 觸發全綠**（3343 測試 0F/0E、12m14s；C0/C2/C3/C4/C5 五格逐 step 驗實跑——⚠ 沒有 C1 是設計、已交還 repo-lint；streak「應計入」分支這趟走到、「dispatch 不計入」分支由 run 4691 反向實證）；兩筆待辦已落 **#837**（(a) 檔頭假句＋(b) green-streak 未建；append 誰做／不做／自動化 loop guard 三問**待 Eugene 裁**；issue 只記帳不代裁）；dev-819 收尾**已完成**（wt-819-fix 已清、issue-leader 複驗；靜默丟棄格開成 **#836**，行號在 `16b5fc04` 逐行複驗＋API body 逐字讀回驗證）；#830 分支／wt-546a 清理**已完成**（08-22：is-ancestor 雙驗 rc=0、ls-remote 刪前1行刪後0行含陰陽對照、porcelain 空；#819/wt-546 確認未動仍 `9a5ab24c`）。 |
| **#648** 🔒 | leader2 | 2026-08-21 | **Eugene 08-21 派**：CS `state='running'` 早於 Public URL 可解析——補 CS `dns_failed` 鏡射 SM（永久縫）＋把 Public URL 可達性納入 `_is_release_ready`（暫時縫）；方向三選一由實作者選型、PR 寫理由；動到 operator 要本機跑真 K8s integration tests；跑測試前排沙盒。**順序（issue-leader 08-22 改）：#648 先合、#531 後合**——⚠ dev-648 **也**撞同一 weekly rate limit（**dev-648 已復活**：Esc＋probe 10 秒回 ok、帳號切換後週限不適用；⚠ 中斷期間的佇列訊息被清空、範圍限制已重送並確認讀到；備份分支 `backup/648-wip-20260822`＝`4b3d0ec2` 已 push、主分支仍未 push 保持可重寫）。呼叫鏈查證（leader2）：`update_tunnel_config`（呼叫者僅 create_route/delete_route＝#648 側）與 `configure_tunnel_ingress`（呼叫者僅 api/tunnels.py＝#531 側）**兩條鏈不相交**，邊界乾淨。兩張都動 `extra/paas-operator/src/services/cloudflare.py`，#648 只動 `create_route`／`create_dns_record` 路徑＋`releases.py`／`schemas.py`，不碰 route sync／ingress／ownership 段；#531 合入後 #648 `git merge origin/develop` 再開 PR。進度：**(b) 沙盒兩輪收工**（R1 12F+3E/23→R2 0F0E、預測對帳無紅旗、跨輪單變因 sha 成立、隔離副本判別式過、destroy＋NotFound 陽性對照；🔴 真正產出＝逐條看 traceback 攔下 6 格「紅在錯的分支、R2 照樣轉綠」的假訊號）；覆核抓到一條**註解賣沒有的功效**（省探測理由對 production 不成立——兩份 poll 在窗口判斷前已無條件探測）已令訂正；#827 補 bind-mount rm-rf 陷阱留言＋「mv 換目錄項 ⇒ pod 要重啟才見新副本」。**交付＝PR #832 ⛔ 待 Eugene 覆核、CI 全綠**（head `bf64892c`——初版 CI 紅＝repo-lint 自我測試段兩檔（floor 對新 gate `route-contract-golden` 零覆蓋＝守線可被靜默整組刪除，已用 `--update-baseline` 修、baseline 只增 2 行不減）；診斷過程的 `grep|head` 截斷視野教訓進 memory；Odoo 雙輪 R1 16F+6E/33→R2 0F0E、integration 41 passed/4 skip（KubeVirt 明知故略逐條列）、#814 gate 端到端證共用副本刷新生效、golden 契約＋route-contract-golden repo-lint gate、operator 未越界 #531 地盤零命中；relayed/verified 分列、已知限制四條明寫；worktree＋備份分支留到 merge 後清理）。**08-22：#826/#830/#831 皆已併入 develop `d4290c25`；Eugene 裁示衝突改由新招 ccsx agent `dev-832` 處理**（leader2 停手未進 merge；dev-832 於 wt-832-fix detached 完成：真衝突僅 `src/tests/__init__.py` 四塊 append 全留，`schemas.py`／`repo-lint.py` auto-merge 後逐塊語意審過、檔頭計數 32 條正確）。**08-22 已併（merge `67d33c34`）、issue #648 已關**；清理**全部完成**（08-22：dev-832 wt-832-fix 已清；leader2：遠端 fix/648＋backup 已刪——backup 21 條獨有行逐行驗證為被升級版涵蓋、非遺失；wt-648 移除、dev-648 dismiss、#648 補 rollout 留言含 `is-ancestor rc=1` 證據）；衍生 **#834**（clear-on-view 旗標零測試——症狀與「DNS 真沒好」畫面不可區分）＋**#835**（repo-lint 檔頭條目位置無 gate＝#832 CI 紅根因、注入錨點家族第三例）；dev-648 保全原文存 canvas note「dev-648 保全原文」（75 行，issue-leader 可讀）——第 3 項 leader2 已查證（`--update-baseline` 會連帶重產 `baselines/` 四檔；repo-lint.py:1739／3882 兩份清單刻意 in-code 防的就是這個、但 baselines/ 仍暴露且自保做法無文件）、第 4 項＝兩個已記載坑的復發、第 5-6 項待 issue-leader merge train 後收整；⚠ 含 operator 側碼、未隨 `20260821.2328` image ⇒ **prod 生效等下一次 operator release（與 #531 同批帶上）**。 |
| **#531** 🔒 | leader3 | 2026-08-21 | **Eugene 08-21 派**：operator `PUT /api/tunnels/{id}/routes` 補 tunnel 歸屬驗證（縱深防禦、不動 #522 平台側）；歸屬權威來源選型寫進 PR；⛔ 不得對真 Cloudflare 發跨歸屬 PUT 實測（不可逆）——用 mock／integration tests；合併前本機真 K8s integration tests。08-21 裁：共用 helper 一次套 `/routes`＋`/config`＋`DELETE /{id}`（缺 claim 的端點走兩段 rollout）；RED 落 unit 層（TestClient＋mock CF）、integration 41 支當合併門檻；**沙盒／叢集 slot 已核准（15:55Z，Avail 33.3Gi、0 個沙盒）**。進度：RED 到位（`tests/test_tunnel_ownership.py` 11 紅／4 陽性對照綠，三端點現況皆 200 接受跨歸屬）；設計＝顯式 `tunnel_name` claim 欄位（不重用 comment）、DELETE 走 query param、CF 回 name 空 ⇒ fail-closed、claim-missing 三端點停 stage 1＋翻預設另開 issue；衍生 #820（operator 未發版）＋wt531 將開「create-loop 同名 DNS 靜默 repoint」issue（不進本 PR）。**順序（08-22 改）：#648 先合、#531 後合**（**Eugene 08-22 裁 A＝等到 08-24、不加值**；WIP 已保全 `7cb9ab78` push 遠端 `fix/531-operator-tunnel-ownership`、無 PR、版號未動；叢集 slot 已歸還）（同檔 `cloudflare.py`；#531 只動 `sync_tunnel_routes`／`reconcile_tunnel_dns`／`configure_tunnel_ingress`／ownership helper＋`tunnels.py`，不碰 `create_route`／`create_dns_record`／`releases.py`）；叢集 slot 也是 #531 先。（此順序後被 08-22 merge train 實況取代：#826 先併）**08-22 收工：交付 PR #826（三端點 tunnel ownership 驗證＋DNS scope 測試）已併 develop `ecbc76eb`、issue #531 自動關**；分支／wt-531 清理＋wt531 dismiss **已完成**（08-22：ls-remote 刪前 1 行刪後 0 行含陽性對照、porcelain 空、HEAD `17738bbd` 在 develop 內；repo 另有 2 筆非本線 stash 未動）；⚠ 修在 operator 碼、未隨 `20260821.2328` image ⇒ **prod 生效要等下一次 operator release**（下次發版清單帶上）。 |
| **#514** 🔒 | leader4 | 2026-08-21 | **Eugene 08-21 派**：租戶 ns baseline **egress** 收斂（`_apply_baseline_netpol` 現只有 Ingress）——照 issue 待辦：盤點各 template 合法出向 → 設計規則（拒 RFC1918／ClusterIP、放行 DNS＋必要平台端點、外網通）→ dev sandbox 演練 → **預設關閉／可逐 ns 開啟＋單 ns 回滾路徑**再開 PR；⚠ issue 自陳 blast radius 大、屬 fast-follow。#428 載體在 cd repo＋需重啟視窗，不在本單。進度：待辦 1＋2 完成（分支 `security/514-baseline-egress` tip `4aeb7ed3` 已 push；設計 542+60 行）；🔴 兩個推翻工單前提的發現：**#427 body 那句「租戶→odoo webhook 回呼」是錯的**（webhook 發送端是 operator、在 paas-system）＋**agent ns `paas-ws-pa-*` 落在 glob 內卻沒有 baseline netpol**（`_ensure_namespace` 不呼叫 `_apply_baseline_netpol`＝D-2）；**NetworkPolicy allow-union ⇒ chart 已有 allowAllEgress 則 ns 級 deny 無效**，10/14 template 的 chart 在 charts repo ⇒ 實作前要跨 repo 查證；**wt514-survey 已 ESC 復活**（33% context、原設計作者，跨 repo 查證派回它）；charts 查證走本機既有 clone 絕對唯讀（九支 chart、四支第三方不在該 repo 留 §11）；待 Eugene：**D-1~D-4 拍板（digest 已出，建議＝D-1 C主B副＋fail-closed／D-2 C 分波（A 可接受但須明寫分波）／D-3 C 獨立可關／D-4 ns label＋模組常數）**；**design PR #825 ✅ 08-22 Eugene 已併（merge `290ce895`）**（+804 docs、無 auto-close token——⚠ 連 backtick 裡的 Closes 都可能被 Gitea 解析，教訓已記）。charts 查證：**6 支自家 chart 全已有 egress default-deny** ⇒ 缺口只剩三類（第三方 chart pod／n8n MCP sidecar／CDI importer），blast radius 大縮；**F-14**＝原設計不限 port 的外網放行會 union 放寬 open-design 既有的嚴格 egress（安全回歸）；**提問四升 blocker**（haos chart 註解自陳 CDI importer 以 ingress-only 為前提 ⇒ egress 落地新開 HAOS 卡 ImportInProgress）；hermes-agent 0.2.0 已取件（fetch 授權、無 checkout）⇒ 🔴 **結論翻盤：4 支自家 chart（mcp-server×7 template／ha-paas／haos／ha-full） 預設 true、自 render  allow-all ⇒ ns 級 baseline deny 對 10/14 在售 template 安全價值＝零**、對已收斂的 2 支（open-design／hermes-agent 0.2.0：ipBlock+限port+dnsKubeDnsOnly）不限 port 的 baseline 是放寬；真沒被蓋到的只剩 n8n＋sidecar＋CDI importer＋bitnami 類。修法方向改組：**(甲) charts 側翻 4 支 allowAllEgress 預設吃安全 ipBlock 套件（載體 woow-paas-charts）＋(乙) baseline 只收沒有 chart NP 的三類且必須限 port**——待 Eugene 裁；D-1 的形狀 chart 端已給（apiServerCidrs/Ports＋「PLATFORM: inject per cluster (#404)」）、平台只需餵值。**第 1 段交付完＋已併**：PR #825 終版（最終 head `fa529fa1`＝ec73d713 後再 +3 筆 docs 微調、+971 docs、CI 綠；兩 teammate 已保全後 dismiss；08-22 併入 develop `290ce895`）。🔴 核心結論＝**A 群 10 個在售 template 的 egress 全開、本案做完也一樣**（唯一解法是翻 4 支 chart 的 `allowAllEgress`＝charts repo、與本案正交；#514 結案不得被讀成「租戶 egress 已收斂」）；egress 側 6/6 chart 自管 ⇒ `podSelector:{}` 在 egress 方向不成立（與 ingress 不對稱）。三路線 §5.4：R1 原設計限 port（只救 C 群）／R2 operator per-release NP（消 F-14/F-7，bitnami label 已證、**n8n 未查＝尚不能當可行**）／R3 治本——⚠ **08-22 成本更正（leader4 自抓）：不是改 values，是移植機制**（四支 chart 的  另一邊是 **DNS-only** 不是安全 ipBlock ⇒ 直接翻＝HA 整合／HACS／mcp 租戶自填 endpoint 全斷；正解＝把 open-design/hermes-agent 的 ipBlock 形狀移植進四支＋各自 bump，issue 草稿在 PR #825 §12、風險 ha-full 最高 haos 最低）。D-1 已關（E-4 不做）。**待 Eugene：R1/R2/R3 取捨＋D-2／D-3（587）／D-4**。遠端分支＋wt-514 清理**已完成**（08-22：ancestor 複驗 rc=0、ls-remote 輸出空、worktree porcelain 空後 remove exit 0、develop 取檔驗到終版內容；leader4 teammate 全 dismiss）；issue 不關，實作屆時從新 develop 開新 worktree。 |

> 待派（Eugene 2026-08-21 起）：無。（08-21 第三批：#805 (b) → leader5、#546 → leader1；第四批：#648 → leader2、#531 → leader3、#514 → leader4；五位全部在飛。）（08-21 第二批：#691 → leader1 **PR #817 已 merge、issue 自動關**；#814 → leader3 **cc/woowprd PR #4 已 merge、plugin 已重裝 `951f49c4`、issue 已關**；#815 → leader4 **PR #818 已 merge、issue 自動關**；五位 leader 全部待命。）（08-21 派出並全部 merge：#787 A 段 PR #812、#806 PR #808、#785 PR #813、#684＋#544 PR #811＋#816、#805 (a) PR #810；五位 leader 全部待命。）

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
| **#531** 🔒 leader3（08-21 派） | operator `PUT /api/tunnels/{id}/routes` 無 tunnel 歸屬驗證，且為整份 ingress 覆寫 |
| **#289** | project↔CloudService 綁定授權缺 model-level guard |
| **#282** | `workspace.slug` 補 write guard（identity-anchor 不可變） |
| **#706** 🆕 | 承 #698／PR #705 的既有綁定盤點——`cloud_service_binding_guard` 只擋新寫入、不回溯，既有跨 workspace 的 project↔cloud_service 綁定不會自動消失，且經 `_reconcile_agent_runtime_reach` 餵給 agent pod 的 egress allowlist 與 `reach-ws-*` label 的 reach 授權照樣有效。跨 workspace 綁定本身是合法設計、無法機械判定攻擊或合法 ⇒ 本單只做唯讀盤點，盤完才決定要不要清、怎麼清（若需 migration，照 #691 新制 bump＋目錄同號，見上方工作原則）；讀 prod DB 那段走 `woow-operator` agent |
| **#787**（資安追蹤；A 段 ✅ 交付、leader2 08-21 收工） | context-key 偽造家族普查（承 #536／#788）：A 段 5 把 truthy 閘**全部 enforced**（registry pending 已空）——`cs_skip_access_sync` PR #793（merged）、`sm_*` 三把 PR #795／`ccd987b2`（merged）、`woow_force_issue` **PR #812 已 merge（08-21 11:39Z）**（.174；portal 身分真 call_kw 可達、fail-safe 方向相反＝不開立且必須吵、ECPay cron 只撿 pending/failed ⇒ 靜默早退會變「帳本 issued、ECPay 空」；獨立覆核七點證據在 PR 留言 8313）。衍生：`test_acl_p9` 測試名誇大（#787 留言 8311，待開單）；B 段由 #783 承接。issue 待 Eugene 關或續追。 |
| **#798** 🆕（2026-08-21 補列；承 #786 覆核開） | `cf_policy_id` 無任何欄位級寫入守衛（無 `groups=`、無 identity-anchor、ACL 1,1,1,1；route 側同類欄有三件套）⇒ 租戶裸 RPC 對自己 workspace 的 policy 列寫 `cf_policy_id`，而 #792 `_trusted_ids_for()` 把列上值當 trusted ⇒ 繞過 #781／#792 為 #783 立的防線（同 sink 另一腿）。今天可達的是自傷格（真 policy 成永久 CF 孤兒、靜默）；跨租戶刪需帶外取得 id。develop 既有形狀、非 #796 引入；修法三選一未選型（第 2 層 ⇒ 留 ②）。 |
| **#822** 🆕（2026-08-22 leader5 開；承 #805(b) 盤點） | SA `action_provision` 第 2 步（identity write）失敗時 CF `create_tunnel` **零補償** ⇒ tunnel_id 沒落地、冪等守衛失效 ⇒ 每次重試多一顆計費孤兒 tunnel；無自癒路徑；🔴 對 #385 的意義＝79 顆候選的**產生源**之一、清掉會再生；sudo 修法（#805 PR）只消 AccessError 這條觸發，其他失敗形狀仍在。全靜態推導未實跑。 |
| **#803** 🆕（2026-08-21 補列；承 #802 明載殘缺） | 平台沒有週期性 Access re-sync ⇒ #802 的 best-effort 補償重推失敗後，`access_protection_lost=True` 的列要等租戶下一次自己觸發 sync（可能永遠不來）。本單＝補那個收斂者：一條 cron 掃旗標列重推 `_sync_access_to_operator()`，四腿（SA route／SH route／CloudService／SmartHome）都要涵蓋。歸 A1＝延遲成本是對外 hostname 多開放一段時間（⚠ 兩可：A4 資料一致性）。動手前：頻率給推導方式；被 skip 的列要不要永遠重試（#791 F1 佇列堵死形狀）；停權中要不要開 force 旁路（拍板題，作者傾向沿用 #802 不改產品行為）。 |

**憑證外洩**

| Issue | 一句話 |
| --- | --- |
| **cd#24** | prod registry 密碼明文躺在 `paas-operator-registry` 的 last-applied annotation（`priority:high`） |
| **cd#78** | 輪替已外洩的 Gitea helm repo `read:package` token |
| **cd#117** 🆕 | cd#115 施工副產物：yq 過濾 regex 沒防 base64 `+/=` ⇒ stg fleet secret 的 `auth.apiKey`（用途待查）與 `registry.password`（JCR，username=woow-prod）明文進 agent 逐字稿、已隨 tool result 離開本機邊界（CF token 未外洩）。⚠ 待 Eugene 裁決兩值輪替與否；與 cd#24 關聯——若 stg/prod registry 同顆密碼，本次是第二個外洩載體 |
| **#567** | 例外原文外洩面：`MissingError` 未收斂＋paas.py 3 處／billing.py 5 處 bare `str(e)` |
| **#584** 🔄 | 依判準第 2 層自 ③ 搬入：§一 已由 Eugene 2026-08-05 裁「bn_odoo 密碼不用輪替」。🔴 搬進 ② 不代表結案——同則裁決寫明「§二 的 4 條未查證線索仍未處理，本單不因 §一 裁決而關閉」，剩下是待辦：4 條線索（含與 #556 相關的 prod 帳號歷史）要不要查、誰查；查 prod 帳號歷史那段走 `woow-operator` agent |

**網路隔離**

| Issue | 一句話 |
| --- | --- |
| **charts#42** | `charts/open-design` NetworkPolicy 跨 ns egress 洞（np-audit FAIL，block PR #41） |
| **charts#66** | skill sidecar 專屬 egress 收斂（externalEgress 開啟時 untrusted bash 可外連） |
| **#514** 🔒 leader4（08-21 派） | L2 租戶 ns baseline egress 收斂——第三方 chart 無 egress NP，實測 n8n 仍直達 prod odoo |
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

## A3 · 發版鏈／CI／部署環境（17 筆）

| Issue | 一句話 |
| --- | --- |
| **#827** 🆕（2026-08-22 leader2 開；#814 gate 首次實戰擋下後的根因單） | docker-desktop node 的共用第三方 addon 副本是**另一條版本線**（`sh_subscription` 0.0.4 vs host 18.0.1.0.0；排除 `.DS_Store` 漂移 20 筆；node 缺 `sh_subscription_wallet` 的 migration＋3 支測試含 billing SSOT ⇒ **計費相關沙盒綠燈驗的是殘缺 wallet**）；⛔ rm-rf 刷新禁用（破壞先於還原且無備份），安全程序＝cp 暫存→驗證→mv 原子換留 .bak；過渡＝各自隔離副本。刷新由 leader2 於 #648 收工後執行。Refs #814 #563 |
| **#820** ✅ **已關（08-22）** | operator 發版含 #769 wire 修法完成：tag `operator-v20260821.2328`（digest `sha256:70146490…` push/pull 對帳）→ cd#118 stg（B/C 兩格對調＋tmp tunnel 422→200）→ cd#119 prod（bundle 1/1、雙 pod imageID 對帳、#748 半徑用 `odoo_scripts/` 不對稱標記驗、log 唯讀 0 error 帶陽性對照）；未驗清單留關單留言（SH 全鏈／prod 未發寫入請求／integration 零涵蓋該端點／base 漂移）。衍生 #829 #833 |
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
| **#829** 🆕（2026-08-22 leader3 開；#531 沙盒輪撞到） | in-repo `woow-test.sh` 的 `FULLNAME` 推導多接 `-sandbox`（腳本 `${SLUG}-odoo-dev-sandbox` vs chart 實產 `${SLUG}-odoo-dev`）⇒ **每一個 kubectl 目標都算錯**（secret 那步先退出、deploy 幾處還沒機會浮現）；CLAUDE.md 正指路大家用它＝照文件走必撞（與 #563 同形）；繞法＝plugin `test.sh`（≠修好）；修法須一次驗 FULLNAME 全下游。Refs #531 #563 |
| **#834** 🆕（2026-08-22 leader2 開；dev-648 保全） | #648 的 clear-on-view 路徑（`_refresh_public_url_dns_flag`）零測試覆蓋——壞掉症狀「軟警告永遠不消失」與「DNS 真的沒好」不可區分，無自然訊號只能靠測試守；⚠ #832 的 `test_flag_is_cleared_once_dns_lands` 測的是 poll 路不是這條。Refs #648 |
| **#835** 🆕（2026-08-22 leader2 開；#832 CI 紅根因） | repo-lint 檔頭條目放到「用法：」之後照樣全綠——gate 斷言清單無「條目所在區塊」檢查；注入錨點寫死版面家族第三例（#598→#613→PR #620），這次形狀＝真實檔案可違反錨點預設版面而無人檢查，補法是把位置升格成 gate 性質、不是再改注入器。Refs #832 |
| **#836** 🆕（2026-08-22 dev-819 開；#819 收官時發現） | `test_collection_audit.py` C2 的 scan_expected `if n:`——條數 0 的 TestCase 子類既不進 baseline 也不進 excluded 註記＝該檔自宣告「排除一律列名」的唯一反例；今天無害（三個底線基底本來 0 方法），但判準 bug 把有方法的 class 算成 0 時它會安靜消失而 C2 無感；與 #828 分格＝彼丟整棵樹、此丟單一 class。⚠ 未查 closed issues。Refs #546 #828 |
| **#837** 🆕（2026-08-22 leader1 開；run 4763 首趟後） | test-collection gate 首趟落地後兩筆帳：(a) baseline 檔頭「尚未與真實執行清單對帳」已被 C3 653/653 證偽＝過期敘述 (b) green-streak.txt 未建、run 4763 是第一筆合格紀錄、append 是刻意人工步（CI 自動 commit 會成觸發迴圈）——誰 append／不做／自動化 guard 形狀待 Eugene 裁。查重含分頁陷阱迴避（130 張 open 全掃、鄰居 #467 #836 #828 #821 皆已分格）。Refs #546 |
| **#838** 🆕（2026-08-22 issue-leader 開；dev-648 保全收整） | repo-lint `--update-baseline` 一次重產**全部** baseline——靜默調降風險 in-code 註解已自認兩處（`:1738`／`:3881`，因此那兩份清單刻意不放 baselines/）但四檔仍暴露、「只增不減」自保程序無文件；修法三選項列於單內。⚠ 未查 closed issues。Refs #832 #835 |
| **#731** 🆕 | 五支唯讀腳本 `--context` 可選 ⇒ #32 把「靜默打對」換成「靜默打錯」；刻意不做強制，待裁決收斂方向 |
| **#722** 🆕 | onboard profile `KUBE_CONTEXT`＝`docker-desktop`，`create.sh` 不帶 `--context` 靜默打到本機叢集；與 #32 相乘非重複；`kubectl config use-context` 仍在兩處 ⇒ #32 Part 2 皆未修 |
| **#714** 🆕 | #694 filestore-hashdir-guard 刻意 fail-open，四個 marker 只印 initContainer stdout ⇒ 與正常無法區分，要接告警（🔴 硬約束三條）；僅平台 paas-odoo，租戶側走 #702；Eugene 2026-08-13 核准另開、不阻擋 #694 關單 |

## A4 · 功能缺口／bug／UX（31 筆）

**壞掉的功能**

> 🔴 交叉指標：#805 分流在 ③ 政策，若裁成「SA 也是 bug」危害就落在本段——SA `action_provision` 與 #739 修法前的 SH 同形（非 sudo 寫 `tunnel_token`），是不是 #739 的 SA 版要先實查入口再裁。

| Issue | 一句話 |
| --- | --- |
| **#648** 🔒 leader2（08-21 派） | `priority:high`——CloudService 翻 `state='running'` 早於 Cloudflare DNS 可解析，使用者見綠燈卻拿 `ERR_NAME_NOT_RESOLVED`（`_is_release_ready` 無 DNS 判定、`create_route` 的 DNS 失敗被就地吞掉、未鏡射 #484 的 `dns_failed`）。三個修法方向自註待討論、非定案 |
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
| **#533** | `api_service_skip_trial` 吞掉並發類 DB 例外——`SerializationFailure` 被 `except Exception` 收走，retrying 永不重試 |
| **#525** | paas.py 吞例外→靜默 commit 同構縫（`priority:high`）：3 處確認該修、5 處形狀同構因果不同 |
| **#363** | reset saga 並發：client 逾時後重按會與 in-flight saga 競態，rollback 可能覆蓋較新密碼 |
| **#342** | change plan 後 CS 規格 re-apply（#332 殘段） |
| **#732** 🆕 | **owner 把自己那列改成 `admin` ⇒ workspace 永久 0 個 owner、租戶側不可回復**：三道既有守衛都涵蓋不到這條 `write()` 路徑（#556 補的只防 transient 0-owner）。後果：只有 `group_system` 救得回來；`workspace.owner_id` 不跟著變，與 `_delete_workspace` 的授權判準不一致。不需要惡意、無錯誤訊息 |
| **#702** 🆕 | 承 #694：租戶 `svc-*-odoo` root-owned 資料目錄偵測（Eugene 已裁決先偵測不預防）。唯一 root 路徑是人工 `kubectl exec`、無守衛收斂；只建唯讀掃描器，判準只認 owner（mode／group 恆綠）、讀 `/proc/1/status` 取真實 uid、範圍含 filestore 與 `sessions/` |
| **#740** 🆕 | **租戶 delete 回失敗、但 Cloudflare tunnel 已先被刪、`tunnel_id` 清空後無法重試**：`_cleanup_tunnel()` 跑在 `super().unlink()` 的 ACL 檢查之前 ⇒ DB 列還在、tunnel 已死，沒有東西能驅動重試；既有 orphan guard 不會響（方向與 `cf-ns-orphan-no-account-gc` 相反）。修法：ACL 通過後才送外部呼叫，或做成可對帳狀態 |
| **#760** 🆕 | CS Access policy `create()` 打 2 次 operator sync（policy＋rule model 各一次），CF 上多建一條 reusable policy 再刪；update 路徑的抑制不可直接沿用，修法待定；sync #2 失敗 ⇒ 永久 CF 孤兒 policy。非 blocker（#747 已關）；A4／A5 兩可，孤兒窗口是歸 A4 的理由 |

**migration 地雷（舊 DB 還原會炸）**

| Issue | 一句話 |
| --- | --- |
| **#543** | `18.0.1.0.39` migration 撞 #352 bulk-grant 守衛——從舊備份還原的 DB 會炸在 `.39` |
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

# ①b 固化自動驗證（21 筆）— 暫緩，不投 token

> **政策：先問 Eugene 要不要做，並附成效評估。** 這些是可程式化、跑一次就永遠替你重查的守線，**價值在防退化，不在推進上線**；整段冷凍是因 08-01～08-05 的 token 大量流進這裡而 production 沒前進。**解凍時機：實作類的 A0/A1/A2 清完，或某條真的擋住上線時單張破例。**

**成本刻度**：S＝一個 session 內／M＝一天／L＝跨多天。

## 甲 · 「測試到底有沒有在跑」的完備性（5 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
| **#467** | 2 個測試檔未被 `tests/__init__.py` 匯入 → 15 條從未執行（`priority:high`）。🔴 現況：那兩檔已補進 `__init__.py` 且 class 是 `TransactionCase` ⇒ 第 1 步已落地，只剩第 2 步「機械守線」（防新增測試檔漏 import 照樣全綠）；開工前先重查，別重做修法。 | **S**（僅剩守線） |
| **#546** 🔒 leader1（08-21 派） | 2237 條 Odoo 測試沒有任何 CI 會執行。**現況已非「Phase 2 待裁」**：Eugene 08-03 已拍板觸發 B+C（develop push＋02:00 台北 cron、不掛 PR、終點全套綠不 block merge，留言 4751）並在分支 `ci/run-odoo-tests`@`4fc1c7ee` 做完 Phase 2（未開 PR、commit 數用 rev-list 對 merge-base 現量、本機 worktree `../wt-ci-tests` 乾淨）；Eugene 08-21 裁 8233 §6 A＋B 照做 ⇒ 落地成 PR＋Odoo 測試形式的收集完備性 gate。 | **L→M**（設計與實作已在分支，剩同步 develop／去重疊／開 PR） |

## 乙 · 測試品質／守線形狀（6 張）

| Issue | 一句話 | 成本 |
| --- | --- | --- |
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
- 刻意不入前三：**#546** 價值最高但成本 L、issue 自陳「未放行不動工」，該是解凍後的第一個專案；**#438** 原型不在 repo 內、實作等於重寫。

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

