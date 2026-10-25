---
name: woow-security-sweep-launch
description: 啟動 woow-security-sweep：檢查能不能跑、列出或建立 preset、跑一次（觸發 preset 的手動排程立即執行，判斷題進 Inbox）、建每日／每週排程、查進行中的輪次與排程狀態。使用者說「跑一次安全掃描」「幫我建 sweep 排程」「sweep 現在在跑嗎」時使用。
---

# woow-security-sweep launch

前台 agent（`.omnigent/woow-security-sweep-launcher`）與 CLI 的 Claude Code／Pi session 共用這一份。
正本在 repo 的 `.agents/skills/woow-security-sweep-launch/`；前台 bundle 裡的是 build 時複製的副本，
**腳本一律從主 checkout 執行**（bundle 裡沒有腳本）：

```bash
python3.12 <WS>/paas/woow_paas_platform/.agents/skills/woow-security-sweep-launch/scripts/launch.py --workspace <WS> <子命令>
```

- `<WS>`＝sweep 排程的 workspace（`<WS>/paas/woow_paas_platform` 是主 checkout、`<WS>/WOOW/` 是要掃的 repo）。
  一律明給 `--workspace`，並把 `<WS>` 代入字面值（指令裡不寫 `$` 變數、不寫反斜線）。
- 安裝、註冊 agent、發新版不在這裡：走 `woow-security-sweep-setup` skill。

## ⛔ 原則

- **排程一律經過 launch.py**：不直接用排程的 MCP 工具建立、修改、觸發排程（前台 agent 有守線擋）。
  腳本會做重疊檢查、補設費用上限、讀回核對，直接用工具這些都會漏掉。
- **不直接寫 `<WS>/.omnigent-runtime/woow-security-sweep/`（state）**：preset 用 `preset` 子命令寫，
  其餘 state 只由 sweep 自己寫。
- **能不能跑由腳本判斷，不猜**：腳本回 `錯誤：…`（exit 2）就把訊息原文告訴使用者，照訊息處理，不要繞過。
- 跑一次、建排程之前，先把 preset 內容（targets、mode、deep_review）列給使用者確認。

## 子命令

| 子命令 | 做什麼 | 什麼時候用 |
|---|---|---|
| `check` | setup 的 doctor（不打 Jev API）＋ 每個 preset 有沒有手動排程。`ready: true` 才往下 | 每次開始前 |
| `presets` | 列出 `<state>/presets/*.yaml`、驗證結果、哪些排程在用、手動排程 id | 使用者問「有哪些可以跑」 |
| `preset --name <名稱> --targets <repo 絕對路徑> ... [--mode incremental\|full] [--deep-review auto\|always\|never] [--review …] [--fix …]` | 建立或覆寫 preset（驗證規則與 sweep 共用） | 要掃新的組合 |
| `run --preset <名稱>` | **跑一次**：觸發該 preset 的手動排程「立即執行」 | 使用者要現在就掃 |
| `schedule --preset <名稱> --rrule "<RRULE>" [--name …] [--max-cost 30]` | 建定期排程 | 每日／每週排程 |
| `schedule --preset <名稱> --manual` | 建暫停中的手動排程（`run` 用） | `run` 回「沒有手動排程」時 |
| `status` | 排程（含下次觸發、上次結果）、進行中的 sweep session、最近三輪 | 使用者問「現在在跑嗎」「上次結果」 |

## 跑一次

1. `check` → `presets`，請使用者選 preset（或用 `preset` 建一個新的）。
2. `run --preset <名稱>`。腳本會：
   - 找該 preset 的手動排程（名稱 `woow-security-sweep 手動 <preset>`，暫停中）。沒有 ⇒ 回錯誤；
     經使用者同意後 `schedule --preset <名稱> --manual` 建一個再重跑 `run`。
   - 檢查有沒有 targets 重疊、還沒結束的 sweep session（執行中、Inbox 有待答題目、或 6 小時內有活動但
     還沒 commit）。有 ⇒ **拒絕觸發**，把 session 與原因告訴使用者，等它結束。
   - 觸發「立即執行」（與每日排程同一條路），回 `TRIGGERED` 與新 session 的 id（30 秒內沒出現就是 `null`）。
3. 告訴使用者：判斷題會進 **Omnigent Inbox**；本輪報告在 `<state>/runs/<時間>/report.md`。

## 建排程

`schedule --preset <名稱> --rrule "<RRULE>"`（RRULE 含 `;`，一定要加雙引號）：

- 同一天已有 targets 重疊的排程（暫停中的不算）⇒ 拒絕。常用組合：每日
  `FREQ=WEEKLY;BYDAY=TU,WE,TH,FR,SA,SU;BYHOUR=8;BYMINUTE=30`＋每週全量 `FREQ=WEEKLY;BYDAY=MO;BYHOUR=8;BYMINUTE=30`。
- 欄位固定：prompt `preset: <名稱>`、timezone `Asia/Taipei`、workspace `<WS>`、host 本機、
  `permission_mode: auto`、`max_cost_usd`（預設 30）。建好後腳本補設 `max_cost_usd`，再讀回來逐欄核對。
- 讀回不符 ⇒ 回錯誤並附排程 id：先 `status` 看現況，再用 setup skill 的 doctor 修正，⛔ 不要重建一個。

## 回報格式

```
動作：<check｜presets｜preset｜run｜schedule｜status>
結果：<TRIGGERED session=…｜CREATED id=…｜拒絕：<原因>｜…>
preset：<名稱>（targets：<repo 名清單>，mode，deep_review）
下一步：<Inbox 會收到題目／等進行中的輪次結束／…>
```
