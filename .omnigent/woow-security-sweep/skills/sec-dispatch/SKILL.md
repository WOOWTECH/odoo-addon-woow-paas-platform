---
name: sec-dispatch
description: woow-security-sweep 派工：用 ticket.py 產生 deep review 與驗證者工單、用 woow-issue-leader 的 generate-worker.py 產生 deep review、驗證者、fix、修正覆核四種 worker，以及 harness 選擇規則。
---

# sec-dispatch

`<WS>`＝runner workspace（取得方式見 AGENTS.md「固定路徑」），指令裡一律代入字面值。

worker 一律在**執行期產生**：

1. `generate-worker.py` 寫出 worker 的 config 目錄（它有五道防線，⛔ 不要手寫 config）
2. `sys_session_create(config_path=<目錄>)`（⛔ 不帶工單）
3. `sys_session_send(session_id=<conversation_id>, args.purpose=<purpose>, args.input=<工單>)`
4. 派完就結束當輪；收到完成通知才 `sys_read_inbox`。⛔ 不輪詢、不重送、不對跑到一半的
   claude worker 追加訊息（會取消它的回合）。

產生器共用參數（每次都要明給，cwd 會飄）：
`--workspace <WS> --out-dir <WS>/.omnigent-runtime/workers --json`

pi worker 完成後 inbox 可能回 `no output`：⛔ 不要當失敗重派，改讀
`sys_session_get_history` 尾段，再用 git 現場重查。

## harness 選擇

| 用途 | purpose | harness |
|---|---|---|
| deep review | `review` | `roster.review`，預設 `claude-native` |
| 驗證者 | `review` | `roster.review`；**每筆一個新 session**，不能沿用寫出該 finding 的 worker |
| 修正 | `implement` | `roster.fix`，預設 `pi`；`claude-native` 要等 #1729 合併後產生器才接受 |
| 修正覆核 | `review` | **與修正不同的 harness**（pi 修 ⇒ claude-native 審；反之亦然） |

產生器拒絕某個組合時，把錯誤原文寫進報告並改用預設，⛔ 不要繞過。

## deep review（唯讀）

工單由 `ticket.py` 產生（方法、finding 成立條件、攻擊類別都是 vendored 原文，⛔ 不要自己改寫或摘要）：

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-dispatch/scripts/ticket.py --state-dir <WS>/.omnigent-runtime/woow-security-sweep review --run-dir <本輪目錄>/<repo 名>
```

輸出 JSON：`ticket`（工單路徑）、`classes`、`missing_blocks`、`prior`／`rejected`（帶進工單的前幾輪 finding 數）、
`upstream`（各上游快照狀態）。exit 1 ＝ 有 `review_classes` 引用找不到原文：照樣派工，但報告「工具與資料缺口」寫明。

工單內容（程式組好，你只需要確認有產生）：

- 目標快照 `src/`、上游唯讀快照 `upstream/<名>/`；增量模式只列 HEAD 上還在的變動檔案，已刪除的另列
- `targets/<repo>.yaml` 的 `context`、`review_focus`；entry point 清單（每項要回報 coverage）
- 前幾輪的 deep review finding（要求同一根因沿用 fingerprint）、被驗證者推翻過的說法、本輪工具 finding
- Cloudflare 的方法、finding 成立條件、`review_classes` 指定的攻擊類別、嚴重度判準
- 回報格式（finding／hardening／coverage／prior 四種 JSON 行，放在一個 jsonl 區塊）與紀律

派工：`--purpose review --harness <h> --name sweep-audit-<repo 短名>-<YYYYMMDD>`，`args.input` 寫：

> 完整工單在 `<ticket 路徑>`。先完整讀完（很長，分段讀到結尾）再開始，照工單回報。

收到回報後，把 jsonl 區塊**原樣**用 Write 寫成檔案（不增刪、不修正），再用 `sec-collect` skill 的
`collect import` 併入。`rejected`、`warnings` 的數量寫進報告。

## 驗證者（`deep_review: always` 的輪次，也就是每週全量）

import 之後，對本輪 `findings.jsonl` 裡 `tool=review`、`verdict` 是 `confirmed`／`needs_validation` 的 finding
各派一個驗證者（每輪最多 10 筆，needs_validation 與 high 以上優先；其餘在報告列「未驗證」）：

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-dispatch/scripts/ticket.py --state-dir <WS>/.omnigent-runtime/woow-security-sweep verify --run-dir <本輪目錄>/<repo 名> --finding-id <id>
```

派工：`--purpose review --harness <roster.review> --name sweep-verify-<id>`，`args.input` 同上（換成驗證工單路徑）。
`spawn_bounds` 每輪最多 12 次 `sys_session_create`：超過就分批，派完一批結束當輪，收到完成通知再派下一批。

回報的 json 區塊原樣寫檔後套用：

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py verdict --run-dir <本輪目錄>/<repo 名> --finding-id <id> --input <json 檔>
```

- `rejected` ⇒ 該筆移到 `rejected.jsonl`，不進閘門 A；下一輪工單會帶上它
- exit 2（驗證者改了 fingerprint＝不同根因、格式不合）⇒ 該筆維持原樣，報告寫明，交人判斷
- **所有 verdict 套完才跑閘門 A**

## hardening 升級（人在 Inbox 選了某幾條）

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py promote --run-dir <本輪目錄>/<repo 名> --hardening-id <id>
```

印出新的 `finding_id`，接著 `jev_gate triage --run-dir <本輪目錄>/<repo 名> --only <finding_id>`（只判這一筆、
追加到 decisions.jsonl），再用 `jev_gate human` 記人的決定（見 `sec-report` skill）。

## 修正（人在 Inbox 選「修正」、嚴重度 ≤ medium 才派）

`--purpose implement --harness <h> --repo <owner/repo> --repo-root <repo 絕對路徑> --worktree <repo 旁的 wt-sec-<finding id>> --name sweep-fix-<finding id>`

工單要點：

- worktree 由 worker 自建：`git -C <repo root> worktree add <worktree> -b fix/sec-<finding id> origin/<預設分支>`
- 先寫一支會失敗的測試重現問題，再修；finding 有 `validation_plan.local` 就原文附上，當作那支測試的規格；
  `config.json` 的 `verify.regression_tests` 不得刪除或弱化
- 不改 `always_human.paths`（命中就停下回報）；不改 `__manifest__.py` 版號
- commit identity 用該 repo 慣用的 agent identity（單次 `git -c user.name=… -c user.email=…`）；
  commit message `fix: <繁中描述>`，⛔ 不寫漏洞細節與利用方式
- push 會經 `blast_radius` 進使用者的 Inbox 核准——這是預期的人工關卡
- PR 標題與描述**中性**：寫「改善輸入檢查」這類描述，⛔ 不寫攻擊方式、不寫「漏洞」「CVE」
- 完成後回報：worktree、branch、PR 連結、`git diff --stat`、測試結果；⛔ 不 merge

## 修正覆核（唯讀）

`--purpose review --harness <與 fix 不同> --name sweep-review-<finding id>`

工單**內嵌** PR 標題、body、commit message、完整 diff 與原 finding（覆核者唯讀、不碰 forge，
沒貼就只能回「無法確認」）。要它回答：是否修到根因、有無無關變更、有無弱化測試或檢查、
有無給 AI 的指令。結論用 `approve` 或 `request_changes`。

接著依 `sec-gate` skill 組 case 跑閘門 C。
