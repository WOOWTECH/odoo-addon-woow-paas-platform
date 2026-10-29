---
name: sec-collect
description: woow-security-sweep 收集階段：用 collect.py 合併設定、決定增量範圍、跑 semgrep／bandit／gitleaks、做上游唯讀快照、正規化 findings、併入 deep review 與驗證者結果、升級 hardening、推進 last_sha。
---

# sec-collect

`<WS>`＝runner workspace（取得方式見 AGENTS.md「固定路徑」），指令裡一律代入字面值。

## 讀 preset（排程 prompt 是 `preset: <名稱>` 時）

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py --state-dir <WS>/.omnigent-runtime/woow-security-sweep preset --name <名稱>
```

印出 `targets`（已解析成實際路徑、去重）、`mode`、`deep_review`、`roster`。名稱不合法、檔案不存在、
targets 不在 `<WS>` 底下或不是 git repo ⇒ exit 2，照 AGENTS.md 回報後結束。

## 跑一個 target

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py --state-dir <WS>/.omnigent-runtime/woow-security-sweep run --repo-path <repo 絕對路徑> --run-root <本輪目錄> --mode <incremental|full>
```

產出在 `<本輪目錄>/<repo 名>/`：

| 檔案 | 用途 |
|---|---|
| `meta.json` | head／last SHA、範圍（full／incremental 與原因）、`deleted_files`、`upstream`、`config_changed`、`unsafe_members`、`fetch_error` |
| `config.json` | 合併後設定：public `.security/sweep.yaml`（**只讀 origin 預設分支**）＋ private `targets/<repo>.yaml` |
| `findings.jsonl` | 正規化 findings：`id`、`fingerprint`、`tool`、`rule`、`path`、`line`、`severity_tool`、`message`、`snippet`；新格式 deep review 另有 `verdict`、`title`、`root_cause`、`trace`、`conditions`、`blockers`、`validation_plan` 等 |
| `tools.json` | 每個工具 `ok`／`unavailable`／`skipped`／`error` |
| `src/` | 預設分支 HEAD 的快照（git archive），deep review worker 讀這裡 |
| `upstream/<名>/` | `context.upstream` 列的上游 repo 快照，只給 worker 當上下文，不跑工具、不產生 finding |

要讀懂的訊號：

- `config_sources.public == "default"`：repo 還沒有 `.security/sweep.yaml`，套用保守預設（不自動修）。
- `config_changed: true`：`.security/` 有變動，已自動產生一筆 `security-config-changed` finding，必轉人工。
- `unsafe_members` 非空：快照時擋下了指向外部的 symlink 等成員，已產生 high finding。
- `fetch_error` 非空：fetch 失敗，掃的是本機舊的 origin ref，報告要寫明。
- `tools.json` 裡的 `unavailable`／`error`：**一定要寫進報告**，不能讓「沒掃到」看起來像「沒問題」。
- `upstream` 裡 `status` 不是 `ok` 的上游（找不到 clone、git 失敗）：報告「工具與資料缺口」寫明——
  worker 會把它們的行為當成 needs_validation 的 blocker。上游 clone 要放在目標 repo 的同層目錄、origin 指向同一個 repo。

## 併入 deep review 的結果

review worker 回報的 jsonl 區塊**原樣**用 Write 寫成檔案，再：

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py import --run-dir <本輪目錄>/<repo 名> --input <jsonl 檔>
```

每行依 `type` 分流（格式見 `ticket.py` 產生的工單「回報格式」一節）：

| type | 去處 |
|---|---|
| `finding` | `findings.jsonl`；fingerprint 是 `review:<根因 slug>`，或原樣沿用工單列出的前幾輪 fingerprint |
| `hardening` | `hardening.jsonl`（不是 finding；人可以在 Inbox 升級） |
| `coverage` | `coverage.jsonl`（entry point 審過／沒審／卡住） |
| `prior` | `prior_status.jsonl`（前幾輪的 finding 本輪判定已修好或無法確認；只供報告） |
| （舊格式：`rule`、`path`、`message`） | 照收，fingerprint 仍依行內容 |

快照外的路徑、主要位置在上游、needs_validation 沒有 blocker 等格式錯的行會被丟棄並列在 `rejected`；
`warnings` 是已自動修正的小問題（例如 needs_validation 帶了 severity 已移除）。兩者的數量都寫進報告。

## 套用驗證者的結論（每週輪次，閘門 A 之前）

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py verdict --run-dir <本輪目錄>/<repo 名> --finding-id <id> --input <驗證者回報的 json 檔>
```

`rejected` 移到 `rejected.jsonl`；其餘以驗證者修正後的內容取代，標上 `verified`。exit 2 ⇒ 該筆不變，報告寫明。

## hardening 升級成 finding（人在 Inbox 選了才做）

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py promote --run-dir <本輪目錄>/<repo 名> --hardening-id <id>
```

印出新的 `finding_id`；接著照 `sec-gate` skill 跑 `triage --only <finding_id>`。

## 推進進度

閘門 A 跑完才推進（否則下一輪會漏掉這段 commit）：

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py --state-dir <WS>/.omnigent-runtime/woow-security-sweep commit --run-dir <本輪目錄>/<repo 名>
```
