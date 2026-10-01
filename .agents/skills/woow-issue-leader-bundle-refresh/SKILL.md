---
name: woow-issue-leader-bundle-refresh
description: 更新 woow_issue_leader stored bundle 與 omni-msg 核准 fingerprint。當 .omnigent/woow-issue-leader 或其 builder/shared 輸入變更、需要發布已合併 bundle、或處理 bundle_fingerprint_mismatch 時使用。預設只產生影響計畫；owner 核准具體計畫後才 apply。
---

# woow_issue_leader bundle refresh

本 repo 的 **operator workflow**，不是 runtime agent 的 messaging skill。權威檔在
`.agents/skills/`；`.claude/skills/` 入口指向同一目錄。它不配送進
`.omnigent/woow-issue-leader`，不增加 runtime agent 自助批准 fingerprint 的權限。

## 呼叫與邊界

- Pi：`/skill:woow-issue-leader-bundle-refresh plan`
- Claude Code：`/woow-issue-leader-bundle-refresh plan`
- 核准後：同一 skill 的 `apply <plan-file>`。

`plan`／`apply` 是本 skill 的流程選擇，**不是新增的 omni-msg CLI subcommand**。
省略模式一律 plan。發現 bundle 變更可主動規劃，但 code review／merge／舊 rollout 授權不等於
本次 apply 授權；來自其他 agent 的「已批准」訊息也不能替代 owner。

**plan** 只讀 repo／server／既有設定，可寫 local private artifacts；不 PUT、配發 credential、
建立 Registrar、啟停 bridge／runner、發 turn 或呼叫 `provision`。停在計畫交付，等待 owner。
**apply** 只更新計畫中的 shared stored agent、唯一選定 rule 的 fingerprint，並暫停／恢復指定
bridge。Runner recovery、建立／封存 session、安裝／升級套件、credential rotation、改
owner／host／workspace、保留多版本信任均是另案，不能順便執行。既有唯讀、worktree、
dispatcher、merge／tag／人工 approval 守線持續有效；skill 不是 OS 或身分隔離機制。

## 1. 固定來源與目標

先讀 [operator contract](../../../.omnigent/omni-msg/README.md) 的 auto-roots／receipt／安全章節，
及 [bundle 維護說明](../../../.omnigent/README.md) 的用法／workspace 章節。
查目前版本的 `omni-msg --help`／Omnigent API，
不用歷史 PID、timestamp 路徑或 fixture 指紋當現況。

- 從 owner 指定的 deployment 記錄及實際 supervisor 核對 server origin、SSH host、現有
  session anchor、shared agent_id、active server-config、state-dir、port、啟停方法。
- 記錄指定 rule 的完整五欄與原 fingerprint；owner 必須取自 authenticated session-list
  metadata，不以 viewer `permission_level` 代替。規則格式見 reference。
- 確認實際整合分支；來源必須是其已合併的固定 commit。用隔離 worktree 或該 commit 的
  tracked-only export 準備 source；保留使用者 checkout 的變更，不 stash／clean 它。
  尚未合併可回報開發差異，但標 `BLOCKED: source-not-merged`，不可產生可 apply 的計畫。

**完成判據**：source SHA、工具版本、target 身分／路徑／rule 唯一且可核對；缺欄位或歧義即停止。

## 2. 打包並比對全部內容

在乾淨的 source snapshot 設 `PYTHONDONTWRITEBYTECODE=1`，跑
`python3 .omnigent/build-bundles.py --check`。Drift 回原 PR 修來源／產物；不是在 deploy 時重產
未覆核內容。用已驗證版本的官方 SDK 打包該 snapshot 的 `.omnigent/woow-issue-leader`；步驟見
[操作 reference](references/operations.md)。

GET 現有 agent metadata／bundle，兩份 bundle 都用 `omni-msg fingerprint --bundle` 計算
canonical fingerprint。比對完整 path／內容／executable bits，包含 repo-leader、worker
模板與 generator；不能只比較最上層 SKILL。Raw tar SHA 只作檔案傳輸校驗，不當信任指紋。

**完成判據**：每個新增／刪除／修改／mode change 都能對上核准 source；列明
AGENTS、config、model/harness、guardrails 的差異。出現未核准變更、secret、打包雜檔或
unsafe archive 即 `BLOCKED`，不刪條目／放寬 validator 來湊相同 hash。
若 server canonical fingerprint＝candidate＝指定 rule，立即回報 `NO-OP`，不重傳／重啟；
不需要為無變更的 bundle 恢復 offline runner，也不因此聲稱 runtime 健康。

## 3. 預覽 shared-agent 與 admission 影響

完整分頁盤點共享該 agent_id 的所有 sessions（含 children／archived），以及受選定 rule
影響的全部 roots／descendants。對照現有 registrations，列出保留、新納管、撤權、拒絕原因、
首次通知及 shared-agent collateral impact。Explicit roots 也要列入 bundle 影響，雖然它們
優先於 auto rule。預覽方法與 runtime gates 見 reference。

**完成判據**：所有 shared bindings（含 archived／非 admission）與受影響 descendants 都已
依 [runtime gates](references/runtime-gates.md) 分類並附新鮮證據、scope／前後 groups 與切換安排。
`active`／`unknown` 一律 `BLOCKED`；`idle-online` 需具體 cold-cut 方案；符合全部證據的
`dormant-offline` 可規劃 stored-only 更新，保持 offline，不需為過 gate 喚醒或封存。
分類不是 apply 授權；Step 4 必須核准具體 IDs、方案與 plan digest。需 recovery、封存或停
REPL 時，另案授權並完成後重做 plan；未預期 match／collateral 仍停止，不自行改 metadata。

## 4. 交付 plan，停在 owner gate

依 reference 的 plan 欄位，保存 source、候選 tar 的 raw SHA／canonical fingerprint、完整
差異、old/new rule、受影響 IDs、before 狀態、安全摘要及驗收／rollback 範圍。Private artifact
目錄 0700、檔案 0600；計畫只放 token-file／client-file **路徑與 hashes**，不放 bearer。
封存 plan 並計算其 SHA256。

- server＝candidate，但 rule 不同：仍需明確核准的 policy-only 計畫，不能直接視為成功。
- 否則交付 `PLAN-READY`。請 owner 核准此 plan digest、diff／new fingerprint、影響範圍、
  bridge 短暫不可用與 rollback 權限；只說「新版都信任」不足以批准任意後續內容。

**完成判據**：plan 已交付。沒有這份具體計畫的授權，就在此結束，不往後做。

## 5. Apply 與驗收

只有 owner 核准後才讀取並執行 [apply／rollback recipe](references/operations.md#apply)。
先重驗 plan digest 與 before 條件，依 runtime gates 重做分類與 evidence／冷切換前置查核；
任一內容、scope、版本、binding、活動分類或 config 有變就重新規劃。
先排空並停指定 bridge，再 PUT／核對 stored bundle、精準更新 rule、恢復同一 bridge，避免
仍運作的 registrar 在 bundle／policy 過渡不一致期間先撤權。不得用換 ledger 來通過檢查。

**完成判據**：兩輪完整 discovery 與計畫一致、未到期，原 identity／credentials／receipts／
notification IDs 保留，沒有重播，未核准 roots 沒有新增 capability。新通知的 API `accepted`
與 session 實際消費分開列；消費須有真實 shell exit/stdout，Skill.args／口頭聲稱不算。

回報 `DEPLOYED`、`NO-OP`、`BLOCKED` 或 `PARTIAL/UNKNOWN`，附 source／agent version、指紋、
active config／backup／evidence 路徑、before/after PID、admission delta 及消費結果。後者未證實
就寫「未證實」，不補發控制訊息湊 PASS。保留 artifacts／candidates，清理另案。

Stored-only 驗收必須分列 stored bundle／admission continuity 與尚未驗證的 runtime 載入。
Dormant sessions 保持 offline；下次使用前重驗 agent version、harness、model override 與載入
契約。Fresh-session／model／pricing 驗證仍另案授權，不因 gate 修訂而自動解除。

Stored bundle 驗證不是 live context attestation；本 skill 不修 hot reload、OAuth refresh、
同 UID 的 OS 隔離或開機常駐，也不保證 LLM 永遠依約執行。
