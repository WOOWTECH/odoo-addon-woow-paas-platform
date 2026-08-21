---
title: merge train 每一節的驗收清單（多張 PR 依序併入 develop 時，解衝突後怎麼驗）
category: guides
status: active
created: 2026-08-21
updated: 2026-08-21
related: [docs/decisions/patterns/README.md, docs/tracking/followups/follow-ups.md]
---

# merge train 每一節的驗收清單

> 來源：2026-08-20/21 六張 PR（#795 → #796 → #797 → #801 → #800 → #802）依序併入 develop 的實戰蒸餾。
> 每一節都是「把 `origin/develop` 併進 PR 分支 → 解衝突 → 自驗 → 交 owner／協調者覆核 → Eugene merge」。
> 逐格都有一次真實命中或一次真實假綠當出處（括號內），不是理論清單。
> 現算式驗證腳本逐字保全在 PR #801 `issuecomment-8176`（主）與 PR #800 `issuecomment-8190`（補：tea 備援信源／3d 反方向／判準 B），
> 開工先照抄它們，不要各自重寫一份平行版本。

## 適用情境

- develop 在你 PR 開著期間前進了（別人 merge 了），你的 PR `mergeable=false`。
- 多張 PR 同時在飛、都動 `src/__manifest__.py` 與 `src/tests/__init__.py`（在本 repo 幾乎是必然：每張 bump 版號、每張加測試檔）。
- 🔴 **每 merge 一張，剩下每一張都會在同一位置重新產生同一個衝突** ⇒ 解衝突要「輪到你前一刻」才做，提早解會白做；協調者按順序叫號。

## 0. 方法

- `git merge origin/develop` 進 PR 分支；**不 rebase、不 force-push**（分支可能不是你的、歷史別改寫）。
- 分支若已被別的 worktree checkout，`git worktree add <dir> --detach FETCH_HEAD` 再 `git push origin HEAD:<branch>`；⛔ 不 `--force`、⛔ 不動別人的 worktree（含只是 `cd` 進去 fetch）。
- 解完用 `tea api /repos/<owner>/<repo>/pulls/<n>` 看 `mergeable=true`（push 後首次可能 `false`，幾秒後重查）。
- 🔴 **停下來問 owner／leader 的判準是「有沒有要裁決的取捨」**，不是「衝突集合變大了沒」或「有沒有衝突標記」——文字乾淨但語意要選邊的檔，用後者當判準會答錯（#801 讓 #800 交集 2→3，但 `repo-lint.py` 兩側改在不同段、auto-merge 乾淨 ⇒ 不必停）。

## 1. `src/__manifest__.py`

- 版號**元組比較**嚴格大於 develop（不比字串；位數進位時字串會說謊），且 **≠ 任何 open PR head 的版號**。
  查法：`pulls?state=open` → 各 head **全長** sha → `contents/src/__manifest__.py?ref=<sha>`（短 sha 會回 `object does not exist`）。
- 🔴 **另驗「develop→merge 的 manifest diff 只有版號那一行」**。整份取分支側會吃掉 develop 側版號以外的改動（多一個 data 檔、多一條 depends），而所有人只會看版號那一行。
  （#797 這次安全是因為 #796 剛好只 bump 版號——是運氣不是解法安全。）
- **開 PR 時的取號規則**：`max(develop, 所有 open PR head 的 manifest) + 1`，**不是** `develop + 1`——後者在多張 PR 平行開時必撞（#796 與 #797 皆取 `.162`、manifest blob sha 完全相同；#800 晚 36 分鐘開仍取 `.162`）。
  ⚠ 它只在 push 當下成立，合併前仍要重驗；merge train 每前進一格，所有人的期望值就過期一次。詳見 #691。
- 撞號只能**一邊**動（兩邊同時讓號會在對方那裡重生）；後開的讓。

## 2. `src/tests/__init__.py`（含 `src/tests/integration/__init__.py` 等子目錄的）

兩邊都在檔尾 append「註解區塊＋import 行」，base 該處為空 ⇒ **add/add 衝突**，解法最自然的「挑一邊」會靜默吃掉一個測試模組（取 develop 側掉你 PR 自己的 RED 測試檔；取 PR 側掉已上線的別人測試檔），檔還在、只是永遠不被 collect、CI 全綠零訊號（#467／#544／#684 家族）。

- **(a) AST 判準 B 的集合聯集**：
  `{(n.module, a.name) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 1 for a in n.names}`
  resolved == develop ∪ PR head，**兩個方向**的差集皆空。
  🔴 **不要**多加 `module is None`（判準 A）——它只涵蓋 `from . import x`，漏 `from .integration import test_ecpay_invoice_real`；同一份檔 A=236／B=237，差的那一行掉了 A 不會紅。
  🔴 **不要用計數**：衝突雙方各加一行、行數相同 ⇒ 單邊數字同時等於 develop 也等於 PR（236＝236，正解 237），計數判準在這裡不是鈍、是恰好被滿足。不要用 grep：行內註解會騙過 regex。
- **(b) 🔴 行級 multiset 聯集（含空行，sum 語意不是 max）**：develop 側新增行遺失 0、PR 側新增行遺失 0、兩側都沒有的行 0。
  **必要、不是備援**——AST 對註解零鑑別力：#796 在該檔除 1 行 import 另加 8 行註解、#797 另加 7 行；develop 相對 #801 的 base 新增 19 行（2 import＋15 註解＋2 空行），AST 只看得到 2 行；越後面的節比例越糟。
- **(c) 遞迴孤兒／幽靈掃描**：遞迴 `src/tests/**`、**逐層**讀該目錄自己的 `__init__.py`（`src/tests/integration/` 有自己的），`test_*.py` 對 import 的孤兒 0、幽靈 0。幽靈非 0 代表解析器壞了。「檔在」與「被匯入」要**分開驗**——兩者都會讓孤兒掃描回乾淨，只看孤兒數會把「把測試刪了」誤判成「修好了」。
- **(d) 🔴 class 有沒有被 select（與 (a)–(c) 不同層）**：(a)–(c) 守的是「檔有沒有被 collect」，`TagsSelector` 是**逐 class** 過濾——模組已被 import、但 class 的基底無法傳遞地到達 Odoo `TestCase`（只到 `unittest.TestCase`）⇒ 該 class 全部測試靜默排除、DEBUG level 訊息、從未執行。#801 自己的 `test_tunnel_config_wire_769.py` 就是這樣在 merge 當下死掉 4 條（#806），而 (a)–(c) 全部正確通過。判準＝逐 class 基底可傳遞到 Odoo TestCase；混合檔（同檔既有 Odoo TestCase 又有 plain class）必須**部分紅**，不是放行。機器守線見 #684（＋#544 合單）。
- 報條數一律**連判準定義一起報**（A／B、含／不含空行、頂層／遞迴、`.py`／`test_*.py` 各差 1，不是誰算錯）。

## 3. 非衝突檔（最危險的失敗不在衝突檔）

merge commit 可以在**非衝突檔**上把另一側的內容悄悄還原；repo-lint 綠、manifest 對、import 集合對、`py_compile` 過、**而且本 repo 沒有任何 CI 跑 Odoo 測試**（#546）⇒ 零訊號。

- **判準＝全樹 blob sha 逐檔比對**（`git ls-tree -r`，或 trees API——**必翻頁並驗 `total_count`**：`per_page=1000` 單頁只回 884/1519 blobs 且 `truncated=true`，不翻頁漏 42% 而差異集合看起來完全正常；compare API 對本 repo 不回 `files` 欄位）：
  - `merge vs develop` 的差異路徑集合 **==** PR 自己改的檔集合（多 0 少 0）；
  - develop 側、PR 沒碰的檔 ⇒ 逐位元等於 develop（#796 的修法檔在 #797 的 merge 後 `828a4921` 逐位元存活）；
  - **3d** PR 側、develop 沒碰的檔 ⇒ 逐位元等於**舊 head**（「merge vs develop 有差異」≠「等於 PR 版」，差異檔可能是第三個值）。
- **3a 🔴 期望集合不可固化**：一律在執行當下由 `BASE=merge-base(old_head, origin/develop)` 現場推導（`diff(BASE, old_head)`、`diff(BASE, origin/develop)`）。develop 從 `5c536a83` 到 `84afb5d8` 後，同一支腳本的「develop 側改檔」4→8、「必須逐位元相同的檔」2→6、判準 B 集合 237→238——吃舊常數時那四個檔被還原也不會紅，輸出與正確時一模一樣。
- **3b 🔴 `assert_develop_is_fresh`**：腳本開頭 `git ls-remote origin refs/heads/develop` 對本地 `origin/develop`，不一致就 `exit`。本 repo 30 幾個 worktree **共用同一個 `.git`**，本地 ref 可能被別人推前、也可能落後，兩種都零訊號；「ref 變了」與「我自己抓到了」在輸出上不可區分，有鑑別力的只有指令 exit code 與 `ls-remote`。git 掛掉時可用 `tea api branches/develop` 當備援信源並印出用了哪條（8190 有這格；8176 會直接 `exit(2)`）。
- **3c 🔴 覆核腳本必須在 merge 前的 develop 上跑**：PR 已 merge 後再跑，`merge vs develop` 差異＝0 檔，「PR 側全存活: True」是對空集合判定＝恆真、輸出與真通過一樣。期望集合非空是執行前提，空集合要 `exit` 不是 `pass`；PR 已 merge 就拿 `merge_commit^1` 當基準，並驗 `tree(develop after merge) == tree(PR head)` 確認 merge 沒夾帶。
- 陽性對照：把必然不成立的檔（如 `src/__manifest__.py`）塞進期望集合，判定必須翻 `False`，否則判準恆真。

## 4. 編譯與靜態 gate

- `python3 -m py_compile` 所有改動檔；`python3 scripts/audit/repo-lint.py --all`。
- 報「N 條全過」要講口徑：`REPO_LINT_RULES` 的 key 數（檔頭編號清單）vs `^def check_` 支數（一條規則可對應多支 check），兩個分母都報。
- ⚠ `repo-lint` **不是** required status check（`branch_protections`：develop 只 required `manifest-version-gate`）——它是看得見的紅燈，不是閘門。

## 5. 語意格（解衝突的人代驗不了，owner 自己做）

文字不衝突 ≠ 語意乾淨：develop 側若改了同一 model 同一路徑（#796 改 `write()` 裡 `_access_sync_targets()` 的求值時機 vs #800 的 sentinel 同路徑；#794 的 teardown 落帳再疊上去），owner 要在 **merge 後的樹**上追呼叫鏈或跑測試坐實，不把 merge 前對 tea 抓下來的檔做的靜態分析寫成已驗。

## 6. 回報格式

新 head sha、`mergeable`、逐檔取哪側；每格 Claim ｜ Command run ｜ Key output line ｜ Confidence（verified／inferred／unverified）；「What I did NOT verify」（Odoo 測試沒 CI 跑，沒跑就明寫；`extra/paas-operator/` 若動到要跑本機 integration tests）；PR 留言逐檔說明（merge 之前貼，merge 後補記也比沒有好）。

## 7. 收尾：已併入分支的清理

- 🔴 **刪分支的清單不要用「人記得的名字」列舉**——同一 issue 號可能有多條腿（#787 有 `fix/787-cs-sentinel` 與 `fix/787-sh-sentinel`，名字只差三個字元），憑記憶列會漏。可機械化的判準是列性質：`git ls-remote --heads origin` 逐條 `git merge-base --is-ancestor <sha> <develop-tip>`，YES 的才是候選，並配陽性對照（一條明知未併入的分支必須回 NO，證明探針有鑑別力）。
- 🔴 **不要採信 `git branch -d` 的拒絕或放行**：它比的是**本地** `develop`——本地落後會給「not fully merged」假拒絕（實踩：本地停在落後 8 commit 的 develop），本地夠新的放行也沒證明遠端已併。一律對 `origin/develop`（先 fetch 或對 `ls-remote` 的值）算 `--is-ancestor`。
- 🔴 **共用 `.git` 下，「branch -d 回 not found」與「這條分支從來不存在」輸出完全一樣**（30+ worktree 共用 ref store，別人刪了你看不到）。要驗自己刪了什麼，先存一份刪前的 sha 快照；刪前記下 sha，`git branch <name> <sha>` 可完整還原（遠端同理：`git push origin <sha>:refs/heads/<name>`）。
- 遠端刪除走 `tea api -X DELETE /repos/<owner>/<repo>/branches/<name>`（不吃 git 憑證）；刪前逐條驗 ancestor，刪後 `GET /branches/<name>` 回 `not found` 才算數（不靠本地 ref）。
- 本地分支是各自的，但 ref 空間是共用的——看得到、刪得掉別人的，正因如此更不該憑推論去刪別人的；回報給 owner 自行處理。

## 本次 merge train 的落地紀錄（出處）

| 順序 | PR | issue | 版號 | 備註 |
| --- | --- | --- | --- | --- |
| 1 | #795 | #787 A 段 SH 三把 | .161 | 序列化起點 |
| 2 | #796 | #786 | .162 | 與 #797 撞 .162（blob 相同）→ #797 讓號 |
| 3 | #797 | #739 | .163 | 衝突由 ccsx 解，merge commit `991743ea`；卡 Eugene「fail-closed 是否 intended」裁決（裁：是 bug） |
| 4 | #801 | #769 | .165 | 覆核腳本晚於 merge 跑 ⇒ 空集合恆真（3c 出處）；腿 2 class 基底錯 ⇒ #806（2(d) 出處） |
| 5 | #800 | #783 | .167 | 衝突集合 2→3 但無取捨（0 的判準出處）；registry `status="removed"` |
| 6 | #802 | #794 | .168 | 語意地雷：`DOOMED_CTX_KEY` 必須取 develop 側（#783 不回退） |

相關：#691（版號競賽）、#467／#544／#684／#806（測試收集與執行）、#546（無 CI 跑 Odoo 測試）。
