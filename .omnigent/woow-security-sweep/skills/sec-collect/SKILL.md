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
| `meta.json` | branch、head／last SHA、範圍（full／incremental 與原因）、`stack`、`deleted_files`、`upstream`、`fork`、`tool_scanned`、`review`（分塊輪審，見下）、`config_changed`、`unsafe_members`、`fetch_error` |
| `config.json` | 合併後設定：public `.security/sweep.yaml`（**只讀 origin 上的 targets `branch`，沒寫就是預設分支**；沒有這個檔就用 `stacks/<stack>.yaml` 的 defaults）＋ private `targets/<repo>.yaml`；另有 `stack`（版號檔、修正驗證方式）與 `repo.host`／`repo.web_url` |
| `findings.jsonl` | 正規化 findings：`id`、`fingerprint`、`tool`、`rule`、`path`、`line`、`severity_tool`、`message`、`snippet`；新格式 deep review 另有 `verdict`、`title`、`root_cause`、`trace`、`conditions`、`blockers`、`validation_plan` 等 |
| `tools.json` | 每個工具 `ok`／`unavailable`／`skipped`／`error` |
| `src/` | 預設分支 HEAD 的快照（git archive），deep review worker 讀這裡 |
| `upstream/<名>/` | `context.upstream` 列的上游 repo 快照，只給 worker 當上下文，不跑工具、不產生 finding |

要讀懂的訊號：

- `config_sources.public` 以 `default` 開頭：repo 還沒有 `.security/sweep.yaml`，套用 stack 的保守預設（不自動修）；
  後面有「＋targets scan_exclude」代表 targets 追加了排除路徑，報告要寫明排除了哪些。
- `config_changed: true`：`.security/` 有變動，已自動產生一筆 `security-config-changed` finding，必轉人工。
- `unsafe_members` 非空：快照時擋下了指向外部的 symlink 等成員，已產生 high finding。
- `fetch_error` 非空：fetch 失敗，掃的是本機舊的 origin ref，報告要寫明。
- `tools.json` 裡的 `unavailable`／`error`：**一定要寫進報告**，不能讓「沒掃到」看起來像「沒問題」。
- `fork` 非空：targets 設了 `since_upstream`，只掃 fork 相對上游 merge-base 改過的檔案；報告寫出 merge-base、
  領先／落後的 commit 數。`fork.fetch_error` 非空代表這輪沒 fetch 到上游、用的是上一次的 ref。
  上游從沒 fetch 成功過 ⇒ `collect run` exit 2（不會退回全量），照 AGENTS.md 回報。
- `tools.json` 的 `advisory`：上游已公告的 advisory 比對 fork 版號，命中的是 `tool: advisory` 的 finding
  （fingerprint `advisory:<GHSA id>`），每輪都會查，與有沒有新 commit 無關。
- `upstream` 裡 `status` 不是 `ok` 的上游（找不到 clone、git 失敗）：報告「工具與資料缺口」寫明——
  worker 會把它們的行為當成 needs_validation 的 blocker。上游 clone 要放在目標 repo 的同層目錄、origin 指向同一個 repo。
- `review` 非 null：targets 有 `review_units`（大 repo 分塊輪審）。掃描工具照常掃整個範圍，只有 deep review 分塊：
  - `mode: units`（全量）：`units` 是本輪要審的塊（最久沒審的優先，各有 `files`、`entry_points`、`missing_paths`、
    `last_reviewed`），`next_due` 是下一輪的塊。`unassigned_files`（`cover` 底下沒歸到任何塊的檔案）、
    `unassigned_routes`（抽得到卻沒有任何塊收的 route）、`missing_paths`（對不到檔案的 paths）、
    `extract_errors`（parse 失敗的檔案）都代表 targets 該更新了，**報告要列出來**。
  - `mode: changed`（增量）：`files` 是本輪要審的變動檔案（最多 `max_files` 個），`overflow` 是超過上限沒審的，
    `outside_count` 是不屬於任何塊的變動檔案數。`files` 是空的 ⇒ 本輪不派 deep review。
  - 手動試跑可以用 `--mode full --units <塊>,<塊>` 指定要審的塊（`forced: true`）。

## 併入 deep review 的結果

review worker 回報的 jsonl 區塊**原樣**用 Write 寫成檔案，再：

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py import --run-dir <本輪目錄>/<repo 名> --input <jsonl 檔>
```

分塊輪審（`review.mode` 是 `units`）的回報一定要加 `--unit <塊>`（該張工單的 `unit`）：finding、hardening、
coverage 會標上塊名，並記在 `review_imports.jsonl`；沒帶、帶了本輪沒有的塊、或不是分塊輪次卻帶了 ⇒ exit 2。

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

閘門 A 跑完才推進（否則下一輪會漏掉這段 commit）。分塊輪審的輪次也在這裡記下哪幾塊審完：只有用 `--unit`
成功併入過回報的塊才算（輸出的 `units_reviewed`），worker 沒回報的塊下一輪會再排到。

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py --state-dir <WS>/.omnigent-runtime/woow-security-sweep commit --run-dir <本輪目錄>/<repo 名>
```
