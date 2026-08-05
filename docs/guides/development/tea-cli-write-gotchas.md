# reference tea cli write gotchas

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_tea_cli_write_gotchas`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：tea CLI 跨 repo 留言/關 issue 的 arg 順序陷阱 + config token 不能直連 API

git-prod.woowtech.io 用 `tea` CLI。對**非當前 repo**（如從 woow_paas_platform 操作 `woow-paas/paas-app-bundle-cd`）的 issue/PR 留言、關閉時踩到兩個陷阱：

**1. `tea comment` / `tea issue close` 的 arg 順序：flags 一律在前、positional index 其次、comment body 放最後。**
語法是 `tea comment [options] <index> [body]`。若把 flags 夾在 index 與 body 之間（如 `tea comment 18 --repo woow-paas/paas-app-bundle-cd --login eugene "body"`），body 不被當 positional → tea 開 `$EDITOR` 等輸入 → **非互動環境直接掛住**（實測 2 分鐘 timeout 才被 kill）。正解：
```
tea comment --repo woow-paas/paas-app-bundle-cd --login eugene <index> "$BODY"
tea issue close --repo woow-paas/paas-app-bundle-cd --login eugene <index>
```
跨 repo 讀（`tea issue <n> --repo <owner>/<repo> --login eugene`）一向正常，只有 write 撞 arg 順序。

**2. tea config 的 token 直連 Gitea API：可能可用，但別當預設路徑。**
`~/Library/Application Support/tea/config.yml`（macOS）的 login 區塊有 `token`（~864 chars）+ `refresh_token` ＝ OAuth2 登入。access token 短效、tea 會自動 refresh，**config 裡那顆常常已過期 → 直連 `/api/v1/...` 拿 401**。所以**日常寫入一律走 tea CLI**（會自動 refresh）。

但 2026-07-25 #423 實測：剛用 tea 寫過（token 才被 refresh）之後，拿 config 裡那顆 token 直連 API **是可以的**——`GET /api/v1/repos/<o>/<r>/issues/<n>/comments` 回 200 並取到完整 18091 字 body。⚠ 同一顆 token 打單筆 `.../issues/comments/<id>` 卻回 **403**，且 python-urllib 預設 User-Agent 容易被擋（加 `User-Agent: curl/8` 後才過）。→ **拿它當「寫入後複驗」的第二信號可以，別拿它當主要寫入通道**；要穩定直連 API 就另開 personal access token。

**2a. 那個 UA 封鎖回的是 403，而 403 天然會把你推向「token 沒權限」的錯方向——用對照組一步排除。**
（2026-08-03 #552／PR#561 實踩：要 `PATCH /pulls/561` 改 PR body，python-urllib 回 **403**，body 是 `error code: 1010`＝**Cloudflare 的瀏覽器指紋封鎖**，不是 Gitea 的授權層。）

判準是**打一個絕對不需要特殊權限的唯讀 endpoint**：

```
GET /api/v1/user            urllib → 403   ⇒ 連「我是誰」都問不到 ⇒ 與 repo 權限無關
GET /api/v1/user            curl   → 200   ⇒ 差異只在 UA
```

⇒ 三個 endpoint（GET pulls／GET user／POST comments）**全部 403** 這件事本身就是訊號：真的權限不足會有選擇性（讀得到、寫不到），**全面 403 ＝ 根本沒進到應用層**。`error code: 1010` 是 Cloudflare 而非 Gitea 的錯誤格式，也可以直接認。

改用 `curl`（預設 UA 就過，不必偽裝瀏覽器 UA——實測兩者皆 200）。順帶：Gitea 的 `PATCH /pulls/<n>` 成功回 **201** 不是 200，用 `%{http_code}` 判成功別只認 200。

⚠ 探針副作用：定位時我拿 `POST .../comments` 當對照組——**它若成功就會在 PR 上留下一則垃圾留言**。這次因為全部被 CF 擋掉所以沒有副作用，但那是運氣。**寫入型 endpoint 不該拿來當診斷探針**，改用唯讀的 `GET /user` 就足夠分辨。

**2a-bis. 🔴 `invalid_client` 與「token 過期」是兩種故障，處置相反——而 2b 的處方對它無效。**
（2026-08-04 實踩，全隊阻斷約數小時。）

```
Failed to refresh token: oauth2: "invalid_client"
  "cannot load client with client id: \"<uuid>\""
Please use 'tea login oauth-refresh eugene' to manually refresh the token.
```

| 故障 | 訊號 | 處置 |
| --- | --- | --- |
| **access token 到期** | `401` | ✅ 隨便跑個 tea 指令即自動 refresh（見 2b） |
| **OAuth client 註冊失效** | **`invalid_client`** | 🔴 **refresh 機制本身壞了**，跑再多次 tea 也不會好 ⇒ **只能人工 `tea login oauth-refresh <user>`**（互動式，agent 做不到，要請人跑） |

⚠ **2b 那句「主動 refresh ＝ 隨便跑一個 tea 指令」在這個故障上是錯的處方**，而它的措辭會讓人一直重跑
tea 指令、以為只是還沒 refresh 到。**判別只看錯誤字串：`401` 還是 `invalid_client`。**

⚠ **破壞範圍比想像大**：本 repo 的 git HTTPS 憑證本來就不通，
`git -c http.extraheader="Authorization: token <tea token>"` 是**唯一繞法** ⇒ tea 一掛，
**`fetch` / `push` / 開 PR / 讀 issue 全停**，不只是「tea 指令不能用」。
⚠ `gh-origin`（GitHub mirror）**不能當 fallback**——實測 develop 落後（見 [[reference_repo_two_release_trains]] 的
「merge≠上線」同族：mirror≠同步）。

⚠ 附帶：故障時 tea 把 warning 印進 **stdout**，`tea api ... | python -c "json.load"` 會炸在
`Extra data: line 1 column 5`——**那個 JSON 解析錯誤是症狀不是原因**，先看 stdout 第一行。

**2a-ter. 🔴 Gitea 查不到 DB 時回的是 `401 invalid username, password or token`——基礎設施故障偽裝成憑證問題，而 2b 的處方會讓你無限重試。**
（2026-08-04 實踩，全隊阻塞。leader5 實查：pgpool CrashLoopBackOff、重啟 9 次；`/api/healthz` 恢復後回 200 且 `database:ping pass`。）

同一場故障我這側看到的是**寫入回 `HTTP 500` ＋ 空訊息本體**（`{"message":"","url":".../api/swagger"}`），
而**同一顆 token 的 `GET` 同時回 200**。我據此推了兩次因果、兩次都錯：
先說「是端點側、與內容無關」（只量了 `/pulls` 一個端點的排他性結論），
再說「真因是 token 過期」（刷新 token 後寫入成功就宣稱因果，**沒做對照**——
沒有在不刷新的情況下重試同一個寫入，而 DB 恢復時間與刷新時間重疊 ⇒ 完全可能是巧合）。

⇒ **兩條與歸因無關、仍然成立的判準**：

1. **`GET` 回 200 不能證明寫入會成功。** 同一顆憑證、同一時刻，讀寫表現不同 ⇒
   別拿唯讀對照組去證明「token 沒問題」（2a 用唯讀 endpoint 排除權限問題仍有效，
   但那條排除的是**權限**，不是**可寫性**）。
2. **錯誤訊息的措辭不是它所屬層級的證據。** `401` 可能是 DB 掛了、`500` 可能是 DB 掛了。
   判準改成問「**這個症狀能不能分辨憑證問題與後端故障？**」——不能的話，
   **先打 `GET /api/healthz` 看 `database` 那一項**，再決定要不要碰 token。

⚠ 這條與下面 2b 直接衝突：2b 寫「突然 401 ＝ token 到期」。**那句話在後端健康時才成立**；
DB 掛掉時照它做會無限 refresh 而永遠不會好。先 healthz，再 2b。

**2b. 「原本能用的直連 API 突然 401」＝ token 到期，不是權限或 endpoint 問題。**
（⚠ **前提：後端健康**。先看 2a-ter，DB 故障時症狀相同但處方相反。）（2026-07-28 `.80` 發版實踩：同一條查 CI run 的 curl 前一輪 200、下一輪 401，背景輪詢腳本因此空轉。）

- **先讀 `token_expiry` 再猜別的**：config 裡 `token_expiry` 是 unix timestamp，直接換算就知道是不是過期（實例：`1785232931` = 18:02:11，當時 18:20 → 已過期 18 分鐘）。**效期約 78 分鐘**，長流程（發版、build 輪詢）中途過期是常態。
- **主動 refresh ＝ 隨便跑一個 tea 指令**：`tea repos list --limit 1 >/dev/null` 之後 config 裡的 `token` 會被換成新的（`token_expiry` 同步往後跳），再重讀就能直連。不需要重新 `tea login add`。
- **長時間 poll 腳本必須每圈重讀 token、並在 401 時觸發 refresh**，否則會靜默空轉到逾時：
  ```bash
  TOKEN=$(grep -m1 "      token:" "$CFG" | sed 's/.*token: *//' | tr -d '"'"'"' ')
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: token $TOKEN" "$API")
  [ "$CODE" = "401" ] && { tea repos list --limit 1 >/dev/null 2>&1; continue; }
  ```
  ⚠ 抓 token 別用 `grep -A6 "git-prod"`——會依 config 行序漂移抓到 `refresh_token`（同樣 858 chars，看起來很像，但打 API 一律 401）。用縮排錨定 `"      token:"` 取第一筆。

**3. `tea pr create` 從 repo dir 跑、不要帶 `--repo owner/repo`**（會被當本地路徑報 `local repository required`）；tea 從 origin remote 推斷。PR target 用 `--base develop`（本 repo PR 慣例 merge 進 develop）。

**4. `tea api -X POST` 觸發 workflow_dispatch 的 input：`-f "inputs[key]=val"` 巢狀 key 語法不會被解析成巢狀 JSON → input 空值。**（2026-07-16 Sentinel #357 e2e 實踩，有實質後果）。用 `-f "inputs[image_name]=..."` dispatch `build-image.yml` 時 `image_name` 收到空值 → CI 走 **prod build 路徑**、卡在人工核可 gate（誤建 issue #13、200 分逾時）。**正解＝`-d` 傳 raw JSON body**：`tea api -X POST -r <repo> ".../actions/workflows/<wf>/dispatches" -d '{"ref":"<branch>","inputs":{"addon_ref":"<branch>","tag":"<ts>","image_name":"paas-odoo-stg"}}'`。安全網：agent 發現 runner 卡太久 → `/reject`（ALLOW_SELF_APPROVE=true）中止、未消耗完整逾時、未誤建 prod image；prod build 的人工核可 gate 也證明有效（沒人核可不會建）。

**5. harness auto-mode classifier 對「shared 生產資源變更」與「kubectl delete ns」要求人類本人同意——不接受另一 agent 經 maestri 的核准。**（2026-07-16 Sentinel e2e 實證）。operator 降回/Fleet unpause（Elmo 共用生產）＋ `kubectl delete ns` 兩類操作，即使 leader 已 maestri 批准，classifier 仍擋、要求 agent 用 `AskUserQuestion` 向人類確認。這是 harness 額外防線、與 maestri 核准機制**互補不衝突**：maestri 批准＝團隊層授權、classifier＝harness 層對真人的最終 gate。→ 派 agent 做共用生產寫入時預期會有這道人類 gate，別當成阻塞。

**6. `tea pulls list` 會回「空表」假陰性——PR 明明存在也列不出來。**（2026-07-25 實踩，差點造成重複開 PR）。PR #429 確實 open，但 `tea pulls list --repo odoo-addons/woow_paas_platform` 回傳零列（同一顆 tea、DEFAULT login 正確、同一 session 稍早列得出 #417/#418/#420/#426）。**後果**：leader 據此判定 teammate「幻影交付、PR 沒開成」，若照此指示重開就會產生重複 PR。

**驗 PR 是否真存在，用最硬的信號 `git ls-remote origin 'refs/pull/<N>/*'`** —— Gitea 只在 PR 真的存在時才建 `refs/pull/N/head`，且不經 tea 的 API 層。次強信號是 `tea pr <N> --repo <repo>`（單筆查詢正常，只有 list 會假空）。同族陷阱：`tea actions runs list` 只列 completed（見 [[reference_tea_actions_runs_list_completed_only]]）——**通則：tea 的 list 類子命令會靜默漏列，任何「查不到＝不存在」的結論都要換信號複驗**，這正是 [[reference_dash_devtcp_false_negative]] 講的「單探測異常先疑方法」。

**6b. `tea issues list` 不分頁會靜默截斷，而它正是查重的工具。**（2026-08-03 實測）同一個 repo：`--state open` 不分頁回 **30 筆**，`--page 1/2/3 --limit 50` 抓完 sort -u 是 **80 筆**。沒有任何警告或省略號。⇒ **「搜過既有 open issue、無重複」這個結論，若用不分頁的 list 得出，涵蓋的可能不到一半。**
同日 dispatcher 用 `--limit 50` 查，**低編號被截掉、因此誤報 #29 已關閉**——同一個坑、同一天、兩個人各踩一次，而他當時剛立完「查無結論先問範圍是不是全集」那條規則。⇒ 這正是 [[feedback_absence_conclusion_scope_not_universe]]：**規則知道了不代表查法會自動變對，工具的預設值會替你決定範圍。**
正解：一律逐頁抓到空頁為止再 `sort -u`，或直接走 Gitea API；並在回報查重結論時附上**抓到幾筆**，讓別人能看出範圍。

**7. `tea comments list --output json` 的 `body` 欄位是被截斷的，會讓成功的長留言看起來像寫爛了。**（2026-07-25 #423 實踩）。貼了一則 18091 字的報告到 issue，`tea comments list --repo <r> <n> --output json` 回來的 `body` **只有 55 字元**（剛好第一行標題），且 `user`／`created_at` 皆為 `None`——看起來像「留言被截斷、只寫進標題」。**實際完全沒事**：改用 Gitea API `GET /api/v1/repos/<o>/<r>/issues/<n>/comments` 讀回，`len(body)` ＝ 18091、頭尾與所有段落齊全。

→ **`tea` 的 `--output json` 是「顯示用」序列化、不是 API 原樣**，別拿它驗證寫入完整性。與第 6 點同族（tea list 類子命令不可信），驗寫入請直讀本體——**優先 `tea issues <n> --comments`（見 7c），Gitea API 只是備援**（見第 2 點的可用條件）。這是 [[feedback_never_fabricate_tool_output]] 的「寫入後重讀目標本體」在 tea 上的具體作法。

⚠ **預設表格模式同樣截斷，而且更容易誤判成「留言沒送出」**（2026-08-03 #518 實踩）。`tea comments list` 的 `BODY` 欄只印被終端寬度截掉的第一行 ⇒ **拿內文關鍵字去 grep 它是零鑑別力的查法**：內文明明在，命中永遠是 0。當時我 grep `TOCTOU|invalidate_recordset` 得到 0，一度分不清是「留言沒送出」還是「探針壞了」——**兩者輸出一模一樣**。
⇒ 正確驗法是**看有沒有新的一列**（comment ID＋timestamp＋標題前綴），不要驗內文；要驗內文改用 `tea issues <n> --comments`（見 7c）。這與第 6 點的通則同源：**tea 的 list 類輸出只能證明「有這筆」，不能證明「內容是什麼」。**

⚠ **`-o yaml` 也一樣截斷**（2026-08-03 #546 實測，三種輸出格式全中）——別把它當成 json 截斷的繞法。該次的判別靠**陽性對照**：拿同法查*別人*在另一個 issue 的留言，body 長度同為 32–66 且結尾都是 `…` ⇒ 證明是 tea 的普遍行為，不是我的留言沒送出。**「我的寫入壞了」與「我的讀取工具壞了」輸出一模一樣時，對照組是唯一能分開它們的東西。**

⚠⚠ **上一條的正解（「改驗有沒有新的一列」）自己也會被 pipeline 吃掉——`cut` 撞多位元組字元會靜默漏列。**（2026-08-03 T2 實踩。）照規則改驗列而不驗內文，用
`tea comments list <n> | grep -E '^│ [0-9]' | cut -c1-100` 控制寬度，結果 macOS 的 `cut`
對中文／emoji 丟 `cut: stdin: Illegal byte sequence` **到 stderr**，stdout 卻**照常印出前面幾列**就停
⇒ 我看到 2 則、實際有 3 則，**漏掉的正是剛送出的那一則**（ID 4614），形狀完全等同「我剛發的留言沒送出」。
錯誤訊息混在 stderr、前面的列又正常印出來，所以看起來就只是「就這麼多筆」。

⇒ 驗列數時**不要在 pipeline 裡放逐位元組處理的工具**。要控寬度就用 `LC_ALL=C`，或直接
`grep -oE '^│ [0-9]+ │ @[a-z]+ │ [0-9-]+ [0-9:]+'` 只抓 ID／author／timestamp 三欄。

⚠ **2026-08-03 更正範圍：這不是 `cut` 的問題，是 macOS 預設 locale 下「任何逐位元組處理的工具」的問題。**
同一天三個工具各中一次、三個不同的人：`cut`（驗留言列數，靜默漏列）、`sort`（排 memory 索引行長度，
`Illegal byte sequence` 到 stderr）、`tr`（處理廣播輸出）。
⇒ **本條原本點名 `cut`，會讓人以為換個工具就安全**——實際上判準是「這個工具會不會逐位元組走字串」，
不是工具名。`awk`／`rev`／`fold`／`expand` 同族。
⇒ 解一律是 `LC_ALL=C`（或改用 `grep -oE` 這種不做寬度處理的抓法）。
⚠ 失效方向兩種、要分開認：`cut` 是**靜默漏列**（stdout 照印前幾列就停），`sort`／`tr` 是**丟到 stderr**
——後者只要沒把 stderr 併進來就看得見，前者看不見。**點名工具的另一個代價是連失效形態也一起漏掉。**
🔑 **兩條疊起來的意思是：內文不可信（tea 自己截斷）、列數也不一定可信（你自己的 pipeline 截斷）——
兩層都繞過才算真的驗到。**「換一種查法複驗」若換的是同一條 pipeline 的上游，等於沒換。

**7c. 截斷是 `comments list` 這個子命令的行為，與 `--output` 無關——六種格式全中。正解是換子命令 `tea issues <n> --comments`，不是換格式、也不是走 API。**
（2026-08-03 #519 實測補完。）上面幾條逐次點名 table→json→yaml，讀起來像「還有沒中的格式可以試」。實際把 `simple / table / csv / tsv / yaml / json` **六種全跑過，body 一律截在第一行加 `…`** ⇒ 沒有任何 `--output` 值能取回全文，別再逐個試。

⇒ 真正可用的是 **`tea issues <n> --comments`**（issues 子命令的 detail view，含全部留言本體）。#519 三則留言共 34,676 字元一次取回，`wc -l` 490 行。**這是 tea 原生路徑，不需要 token、不碰網路層。**

🔴 **本檔上面兩處原本寫著「要驗內文只能走 Gitea API」（第 7 點那兩段，已於本次一併改掉）。** 我照著做，兩次被 harness classifier 擋下（讀 credential 檔＋外連），繞了一大圈才發現 tea 自己就能做——**而本檔第 7 點的 quoted-heredoc 那段早就寫著用 `tea issue <n> --comments` 讀回複驗**。同一份檔案，一處知道、兩處說不可能，而我先讀到的是說不可能的那兩處。

⇒ 這是 [[reference_hook_enforced_docs_drift_actively_teach_wrong]] 說的最強漂移訊號的實例：**doc 內部自相矛盾**。而且漂移的正是「處方」那一層——主結論（tea 的 list 輸出不可信）到今天仍完全正確，錯的是「所以你只能走 API」。**處方最容易過期，因為它綁定的是當下可用的工具集，而工具集會被權限、環境、版本改變。**
⇒ 可執行的判準：**在一份 memory / doc 裡看到「只能用 X」時，先在同一份檔案裡 grep 有沒有提到別的做法**。自相矛盾不需要外部知識就能發現，成本極低，而它命中時省下的是一整條死路。

**7d. `tea issues <n> --comments` 取回的是「渲染後」文字：markdown 表格會被重排成 ASCII 表，欄位分隔線 `|` 被插進句子中間 ⇒ 用去空白的扁平化字串 grep 表格內文，會得到假 0。**
（2026-08-03 #547 實踩。）照 7c 的正解取回全文、依 [[feedback_never_fabricate_tool_output]] 用 `tr -d ' \n'` 扁平化後查四個獨特字串：**兩個命中、兩個回 0**。而回 0 的那兩個**都在 markdown 表格的 cell 裡**，命中的兩個都在表格外的散文。原因是渲染器把長 cell 硬折行，折點插入 `        |        ` ⇒ `把良性巧合` + `|` + `封成規範`，去掉空白與換行之後仍留著 `|`，字串就對不上了。改成 `tr -d ' \n|'` 後三個全部命中。

⇒ **驗 tea 留言落地時，扁平化要連 `|` 一起去除**（`tr -d ' \n|'`）。更一般地：**探針要跟著載體的渲染規則走**，而不是只跟著原文走。
🔑 這次沒有誤判成「留言沒送出」，靠的是**同一批探針裡混了表格內與表格外的字串**——散文那兩個命中，證明留言本身在，於是 0 只能是探針的問題。若我四個字串**全部**取自表格，四個全 0，形狀會完全等同「留言沒送出」。
⇒ 可帶走的：**設計字串探針時，刻意讓樣本橫跨載體的不同區塊（表格內／外、code block 內／外）**——這比事後加陽性對照更便宜，因為它在同一次查詢裡就把「被測物壞了」與「探針壞了」分開。與第 6 點「換一種查法複驗，若換的是同一條 pipeline 的上游等於沒換」同源。

**7e. 「假回 0」有兩種**成因相反**的形態，處方互相解不了對方；分辨器是**先用 `grep -F` 試一次**。**
（2026-08-03 #566 與同日 dispatcher 各踩一種，leader5 實測四種組合。）

| 原文 | pattern | 結果 |
| --- | --- | --- |
| 有 `*` | BRE（`*` 當量詞） | **0** ← dispatcher 踩的 |
| 有 `*` | `grep -F` | 1 ✅ |
| 無 `*`（渲染剝掉） | `grep -F` 純文字 | 1 ✅ |
| 無 `*`（渲染剝掉） | `grep -F` 帶 `*` | **0** ← 我踩的 |

- **我踩的**：渲染**剝掉** markdown ⇒ 原文裡星號**不存在** ⇒ 正解是用純文字 pattern。
- **dispatcher 踩的**：BRE 把 `*` 當量詞 ⇒ 原文裡星號**存在** ⇒ 正解是 `grep -F`。

⇒ **分辨器：先 `-F` 試一次。** `-F` 命中 ⇒ pattern 側問題（正則量詞）；`-F` 仍 0 ⇒ 原文側問題
（渲染剝掉／重排）或內容真的不在。

⚠ 我這次實際看到的渲染是**把 `**` 移位**而不是單純剝掉：原文
`**\`AccessDenied\` 是 \`UserError\` 的平行子類**` 被印成
`AccessDenied** 是 **UserError** 的平行子類` ⇒ 連 7d 的 `tr -d ' \n|'` 都還不夠，
要**連 `*` 與反引號一起剝**（`re.sub(r'[\s*\`_]+', '', text)`）才對得上。
⇒ 7d 說「探針要跟著載體的渲染規則走」，這條補上**規則不只是剝除、也可能是移位**。

🔑 通則不變且再次坐實：**留言的 grep 是單向可信——命中＝真有，回 0 可能只是渲染／正則打敗探針。**
所以「回 0」永遠不可以直接寫成「內容沒送出」，必須先換一種查法。

**7. comment/PR body 含反引號時，雙引號字串會被 zsh 命令替換吃掉——且「留言成功」。**
（2026-07-30 #486 實踩。）`tea comment --repo ... 486 "…帶了 `Closes #486`…"` 裡被反引號
包住的 `Closes #486` 被 zsh 當成 command substitution 執行掉，stderr 只丟一句
`file name too long: Closes`，但**留言照樣送出**，只是那一段變成空字串（顯示成
「（base develop，）」）。技術文字幾乎必然帶反引號（code span），所以這是常態風險
不是特例；錯誤訊息又極易被 `| tail` 蓋掉 → 典型假成功。

正解：body 先用 **quoted heredoc**（`<<` 後接單引號的結束標記＝內容零展開）寫成檔案，
再用 `"$(cat file)"` 傳入——命令替換的*輸出*不會被重新掃描，反引號因此安全。
⚠ 別把那個 heredoc 再包進另一個 heredoc（巢狀會被外層先吃掉）；要寫含 heredoc 的
內容就改用 python 寫檔。同一個坑在 `maestri ask` 也成立（見
[[reference_maestri_ask_shell_quoting]]）：**tea / maestri / gh 任何把長文字當 argv
傳的寫入，一律走 quoted heredoc，並讀回實體複驗**（`tea issue <n> --comments`）。

**8. `Closes #n` 會不會自動關，取決於**遠端**預設分支——本機 `origin/HEAD` 快取會騙人。**
同日實例：本機 `git symbolic-ref refs/remotes/origin/HEAD` 回 `origin/main`，遠端實際
預設分支卻是 `develop`。照本機快取判斷就會誤以為 base=develop 的 PR 不會自動關 issue
（於是多此一舉手動關，或反過來漏關）。權威查法是問遠端：

```
git ls-remote --symref origin HEAD    # → ref: refs/heads/develop	HEAD
```

**9. issue 附件（PDF 等）換版只能走 Gitea API；且 API 的 403 要先當 Cloudflare WAF、不是權限。**
（2026-07-31 #423 定價 PDF 換版實作。）`tea` 沒有管附件的子命令，走 assets API：
`GET|POST /repos/<o>/<r>/issues/<n>/assets`、`DELETE .../assets/<attachment_id>`
（POST 用 multipart `-F "attachment=@file"` ＋ query `?name=<filename>`）。**先上傳新檔、
SHA256 比對下載回來的 bytes、再刪舊 id**，順序反了就只剩備份可救。保留同檔名可讓既有
留言裡「本 issue 附件 xxx.pdf」的指稱繼續成立，但**舊 attachment 的 `/attachments/<uuid>`
直連 URL 會 404**——貼到 issue 以外（LINE／簡報）的連結會斷。

⚠ 同一顆 token：curl 打 assets POST/DELETE 全過，python `urllib` 打 comments POST 卻回
**403 + body `error code: 1010` + `Server: cloudflare`**。那是 CF 的 browser-integrity 擋
User-Agent，**與 token 效期（第 2b 點的 401）是不同故障**。判別式：看 response body 有沒有
`error code: 1010` 與 CF header——有就換 curl 重送，別去 refresh token 或懷疑 scope。
長 body 用 `python3 -c` 產 JSON 檔再 `curl --data-binary @file`，一併避開第 7 點的反引號坑。

**8. `tea comments <idx>` 不帶子命令是 `add` 的簡寫，不是 `list`——拿它取證會得到空回應，誤判成「留言沒送出」。**（2026-08-02 #525 實踩）。help 明寫「When invoked with an issue/PR index and an optional body, behaves like 'tea comments add'」。我用 `tea comments --repo <r> 525` 想讀回複驗，輸出只有 34 字元、所有陽性對照字串命中 0，看起來像留言完全沒進去。**正確讀法是 `tea comments list --repo <r> <n>`**，一查就看到留言好好在（ID 4422）。

⚠ 這個誤判的危險在於**下一步**：若當下判定「沒送出」而補送一次，就會留下重複留言；而且不帶 body 的那次呼叫本身是 add 語義（實測 body 為空時不會建出空留言，但別依賴這點）。

⇒ **用來取證的指令，其語義要先確認過**——別拿一個寫入指令去做讀取複驗。陽性對照全滅時，先懷疑「我的取證方法錯了」，再懷疑「寫入失敗」；兩者的輸出長得一樣。

**9.〔2026-08-03〕`-o json` 的欄位名／型別與 Gitea API 不一致，照 API 文件寫必踩。**

| 你以為 | 實際 | 症狀 |
| --- | --- | --- |
| `tea issues list -o json` → `number` | **`index`** | `KeyError: 'number'` |
| `tea pulls <n> -o json` → `head` 是物件、可取 `head.sha` | **`head` 是字串（分支名）** | 取不到 sha，無法直接驗「PR 頁面看得到這顆 commit」 |

⇒ **驗「PR 上有沒有我剛 push 的 commit」要走兩步鏈**：
`tea pulls <n> -o json` 取 head **ref 名** → `git ls-remote origin refs/heads/<ref>` 取 SHA → 與本地 `git rev-parse HEAD` 逐字比對。

🔑 這條與第 8 點同源：**拿一個工具取證前，先確認它回的東西是不是你以為的東西。**
差別在第 8 點是「指令語義搞錯」，這條是「欄位結構搞錯」——
前者輸出為空（還會引起懷疑），後者直接丟 `KeyError` 或**悄悄取到 `None`**（不會）。
⇒ 用 `-o json` 時先 `print(sorted(d[0].keys()))` 看一次實際欄位，不要照 Gitea API 文件推。

⚠ 同族但機制不同、已記在別處的：`tea issues <n> -o json` 的 **`comments` 欄位不可信**
（回 `0` 或 `[]` 而實際有內容）⇒ 要讀留言只能用 `tea issues <n> --comments`。

相關 tea 既有陷阱（deploy 流程）見 [[deploy-stg-gotchas]]。

## Gitea 服務端間歇性寫入故障：面貌與辨識（2026-08-04 實測）

**症狀**：`tea pulls edit` 回 `Error: could not edit pull request: `（**冒號後空白**）；
REST 直打回 `HTTP 500` 且 body 是 `{"message":"","url":".../swagger"}`。**兩層都把 5xx 吞成空字串。**

🔴 **空的錯誤訊息不是「沒事」，是錯誤被吞掉的樣子。** 故障前約半小時有一次「空 `Error:`、重試就成功」
——那是前兆，當時被當成偶發雜訊放過了。

**故障期間的分層面貌**（用來判斷「還能做什麼」，別一律當成全站不可用）：

| 路徑 | 狀態 |
| --- | --- |
| 所有讀取，含走 indexer 的 `?q=` search | 200 ✅ |
| **git 資料面寫入**（`POST/DELETE /branches`，即 push/tag 這一側） | 201 / 204 ✅ |
| issue 子系統寫入（comment 建立、label 建立/修改、PR PATCH、訂閱刪除） | **500，body 空** ❌ |
| `GET /api/healthz` | **`pass`**，連 `database:ping` 都 pass |

⇒ **`healthz` 對這種故障零鑑別力**——不要拿它當「服務正常」的證據。

🔴 **本檔第 2a-ter 節說「先打 `/api/healthz` 看 database」，與這一句表面矛盾——兩個都對，講的是不同故障。調和如下（不要挑順眼的信）：**

| 故障 | 症狀 | `/api/healthz` |
| --- | --- | --- |
| **DB 掛掉**（pgpool CrashLoopBackOff） | 讀寫**全部** 401「invalid token」 | **424 / `database:ping fail`** ⇒ **決定性** |
| **issue 子系統寫入故障**（同日稍早） | 讀 200、git 面 201、issue 寫入 500 | **200 / `pass`** ⇒ **零鑑別力** |

⇒ **`healthz` 是單向可信**：
- **`fail` ⇒ 確定壞了**（直接停手查基礎設施，不要碰 token）
- **`pass` ⇒ 什麼都沒證明**（子系統可以壞著而 healthz 照樣綠）

⇒ 所以正確順序是：**先看 `healthz`（便宜、能一步排除 DB 層），`pass` 的話再往下自己做寫入探針**——
而不是把 `pass` 當成「伺服器沒問題、所以是我的憑證問題」。
🔑 這兩則記載差 5 小時、由兩個人分別寫下，**各自都正確、合起來才完整**；
單獨讀任何一則都會在另一種故障上做錯判斷。
⇒ 實務後果分岔很大：**打 tag → push → 觸發 CI 走 git 面，故障期間照樣能跑**；
只有 **merge PR** 卡住（它先做 git 再寫 DB，中途失敗會留下「develop 已前進但 PR 顯示 open」的髒狀態
⇒ **故障期間不要試 merge**）。

**500 是乾淨失敗、無部分寫入**（實查：留言數 0、label 未建出）⇒ **重試安全**。

🔴 **範圍會隨時間變，量到的是「那一刻」**：
```
16:31  woow_paas_platform comment POST → 500 ｜ paas-odoo-ci → 201   ← repo-scoped
16:39  woow_paas_platform comment POST → 500 ｜ paas-odoo-ci → 500   ← 已擴散成 instance-wide
16:44  label PATCH → 200 ×2 ｜ comment POST → 201 ×2                 ← 恢復（全程約數分鐘）
```
⇒ 兩個人分別在 16:31 與 16:39 量，**會得到互相矛盾但各自都對的結論**。
⇒ 判 range 一定要**跨 repo 對照**（同 repo 的兩個 issue 對「範圍」這個軸零鑑別力）；
⇒ 而**任何「現在 X 壞了」的廣播壽命是分鐘級**，收到的人要自己量一次再行動，不要轉發
（見 [[feedback_comment_env_state_claims_expire]]）。

## Gitea PR 的 `mergeable` 欄位是懶更新快取，會說「不能合」而其實能合（2026-08-04）

`GET /repos/{o}/{r}/pulls/{n}` 的 `mergeable` **不是即時算的**。實例：#566／#568 回 `mergeable=False`，
但同一時刻本機 `git merge-tree --write-tree` 兩張都**無衝突**。

**權威判法是 `merge-base`**：
```
git merge-base origin/develop origin/<pr-branch>
# 等於 develop tip ⇒ develop 是該分支的祖先 ⇒ 合併是 fast-forward ⇒ 結構上不可能衝突
git merge-tree --write-tree --name-only origin/develop origin/<pr-branch>   # exit 0 = 乾淨
```
⚠ 用前先 `git fetch` **帶顯式 refspec**（`+refs/heads/X:refs/remotes/origin/X`）——
`git fetch origin <branch>` 只保證更新 `FETCH_HEAD`，**不保證建立 `origin/<branch>`**。

🔴 **兩個指令對「ref 不存在」的反應完全不同，原本這裡把它們寫成同一件事，那是錯的**（2026-08-04 實測修正，git 2.55.0）：
- **`git diff` 會靜默當成 pathspec 回空、不報錯** ⇒ 真的無訊號（實例：據此得出「四張兩兩零交集」，實際四張兩兩都有交集）。
- **`git merge-tree` 會報錯**：`merge-tree: <ref> - not something we can merge`，**exit 1、stdout 空**。

⚠ 但有人量到 `merge-tree` 對不存在的 ref 回 **exit 0** ——**那是 exit code 穿過 pipe**：
```
git merge-tree ... 2>&1 | head -1 ; echo $?    → 0   ❌（拿到的是 head 的）
OUT=$(git merge-tree ... 2>&1)   ; echo $?     → 1   ✅
git merge-tree ... | head -1     ; echo $pipestatus[1]  → 1  ✅（zsh，1-indexed）
```

🔴 **判 merge-tree 結果要同時看 exit code 與 stdout，只看任一個都會錯、而且錯的方向不同：**

| 情境 | exit | stdout 首行 |
| --- | --- | --- |
| 乾淨可合 | **0** | 40 字元 tree hash |
| **真的有衝突** | **1** | **40 字元 tree hash（衝突樹！）** |
| ref 不存在／參數錯 | 1 | **空** |

⇒ **「有 tree hash 就是乾淨」是錯的**——衝突時 `--write-tree` 照樣寫出一棵衝突樹。
⇒ 只看 exit code 則分不出「有衝突」與「根本沒算成」。
⇒ 正解＝ `exit==0 && ${#first}==40` 才是乾淨；`${#first}!=40` 是**沒算成**（該修查法，不是結論）。

🔴 **上一版寫「失效方向是說不能合、其實能合，後果只是白做一次 rebase」——那個描述會讓人以為可以無視它。錯。**
（2026-08-04 實測修正。）**`mergeable=False` 是硬阻塞，不只是顯示錯：它會直接擋掉 merge API。**

```
git merge-base --is-ancestor origin/develop origin/pr566 → exit 0（develop 是祖先，結構上不可能衝突）
git merge-tree --write-tree origin/develop origin/pr566  → exit 0（無衝突）
POST /repos/{o}/{r}/pulls/566/merge                      → HTTP 405 {"message":"Please try again later"}
```
⇒ 正確的判準：**`mergeable=True` 可以信；`False` 時你自己算出「其實能合」是對的，但 Gitea 不會讓你合**
——必須先讓它**重算**。

**試過無效的解法（別再試一次）**：`PATCH state=closed` → `PATCH state=open`（兩次都回 **201**），
連查三次 `mergeable` 仍是 `False`。✅ 但 head 前後不變 ⇒ **無損害**。
🔑 **試錯本身不是問題，試完有沒有驗證副作用才是**——回報無效解法時要一併附「我確認了什麼沒被動到」。

**有效的解法**：`POST /repos/{o}/{r}/pulls/{n}/update?style=rebase`（需 repo `allow_rebase_update=True`），
或由分支擁有者做一次 push（Gitea 收到 head 分支 push 時會重算）。

🔴 **用 `update` 端點會改寫 SHA ⇒ 必須驗「內容有沒有變」，不能只看它成功了**：
```
git rev-parse <舊SHA>^{tree}
git rev-parse <新SHA>^{tree}      # 兩者相同 ⇒ 內容一個 byte 都沒變
```
🔑 **SHA 是「這次提交」的指紋，tree 是「內容」的指紋。** rebase 到一個已經是祖先的 base，
理論上 tree 必然不變——但**理論上不變要驗過才算數**，尤其是要進 prod 的東西。
- tree 相同 ⇒ 原本跑完的測試結果**仍然有效**，在 PR 留言記下「SHA X→Y、tree 相同（附兩個 hash）」保住可追溯性，不必重跑。
- tree 不同 ⇒ **那就不是 no-op**，必須重跑，不能放行。

**附帶一條排程用的結構事實**：多張 PR 各自 rebase 到**同一個** develop tip ⇒ 它們是**兄弟不是鏈**。
第一張一合、develop 前進，第二張的 base 就舊了 ⇒ **`__manifest__.py` 必然三方衝突**（每張都改同一行）。
⇒ 「四張都 rebase 好了」**不等於**「四張可以連續合」，實際是 **N 次 merge ＋ N−1 次小 rebase**。
排程時要把這個成本算進去，別承諾「都好了就一次合完」。

### `mergeable` 什麼時候會重算（2026-08-04 實測，修正一個差點被寫成常態的規則）

**常態：head 分支 push 與 base 分支 push 都會觸發重算。**
實測：merge 掉 `#566` 讓 develop 前進之後，其餘三張 PR **立刻**從 `mergeable=True` 翻成 `False`
（它們的 base 舊了、`__manifest__.py` 會三方衝突）⇒ **base push 確實會觸發。**

🔴 **但 DB 故障期間，重算佇列會卡死，而且 DB 恢復後不會替「已被標成 `False` 的 PR」補算。**
實測：DB 恢復、`healthz pass`、跨 repo 寫入 201 之後，輪詢 `#566/#568` 的 `mergeable` **70 秒完全無變化**。
那時能解的**只有 head push**——因為它是一個**新事件**，走的是另一條路徑，
而 `close/reopen`（201 但無效）、`POST /update`（三種 style 全 500）都不算。

🔑 **教訓在於差點寫錯的那一版**：當時的結論是「`mergeable` **只在** head push 時重算」。
那是**拿故障期的觀察當常態規則**——若寫進 memory，下次有人看到 base 動了而旗標沒更新，
會以為「規則就是這樣」而**不去懷疑伺服器狀態**。
⇒ 通則：**在異常期間觀察到的行為，要標明「這是故障期的行為」**，否則它會被當成系統的正常語意，
而它最擅長的就是在下一次正常運作時把人引開。

**7f. `tea pr <n> -o json` 的頂層 key 是 camelCase，不是 Gitea API 的 snake_case——用錯命名整批回 `None`，看起來像「這些欄位沒資料」。**
（2026-08-05 實測。）想查「這張 PR 是誰 merge 的」，照 Gitea API／GitHub 習慣寫 `merged_by` / `merged` / `number` ⇒ **三個 key 在那 22 個 key 裡都不存在**，`.get()` 一律回 `None`，且 `merged_by` 的型別是 `NoneType` 而非 dict（所以連「dict 裡缺子欄位」都不是）。

正確 key：
```
mergedBy（**`str`**，直接就是 login 字串，**不是 dict**）／hasMerged（bool）／index（PR 編號，不叫 number）
🔴 **連值的型別都不同**（我第一版把這句寫錯，已修）：`tea -o json` 的 `mergedBy` 是 `str`；Gitea REST 的 `merged_by` 是 **dict**（取 `.login`）。⇒ **就算猜對 key 名，照另一邊的形狀去取值一樣會失敗**，而失敗形態同樣是 `None`／AttributeError。實測差異：tea 22 key／0 底線／6 大寫；API 38 key／19 底線／0 大寫。
headSha／mergedAt／closedAt／diffUrl／mergeable／state／title／user／base／head
```
實測：`#565/#571/#574/#577` 全部 `hasMerged=True, mergedBy=eugene`；`#579` 未合故 `mergedBy=None`（**這個 None 才是真的**）。

⚠ 同一份資料在**兩條路徑上命名不同**：`tea -o json` 用 camelCase，Gitea REST API 用 snake_case。
兩人各走一條就會得到「你的診斷跟我的不一樣」，而**兩邊的量測都沒錯**——同 [[reference_odoo_two_source_trees_line_numbers_diverge]] 的形狀。⇒ 回報時要寫**是用哪條路徑取的**。

🔑 **這條的破綻是免費的、而且當時就印在輸出上**：探針同時印了 `index`，四次都是 `#?`。
**`index` 是 PR 物件不可能缺的欄位** ⇒ 它回 `None` 只可能是「我的 key 全錯」，不可能是「這個欄位沒資料」。
⇒ 見 [[feedback_negative_verification_needs_positive_control]]：**陽性對照常常已經在你的輸出裡，只是沒被讀**。
