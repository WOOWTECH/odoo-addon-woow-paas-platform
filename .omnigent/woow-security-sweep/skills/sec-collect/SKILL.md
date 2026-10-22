---
name: sec-collect
description: woow-security-sweep 收集階段：用 collect.py 合併設定、決定增量範圍、跑 semgrep／bandit／gitleaks、正規化 findings、併入 deep review 結果、推進 last_sha。
---

# sec-collect

## 跑一個 target

```bash
python3 /home/elmo/woow-repos/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py --state-dir /home/elmo/woow-repos/.omnigent-runtime/woow-security-sweep run --repo-path <repo 絕對路徑> --run-root <本輪目錄> --mode <incremental|full>
```

產出在 `<本輪目錄>/<repo 名>/`：

| 檔案 | 用途 |
|---|---|
| `meta.json` | head／last SHA、範圍（full／incremental 與原因）、`config_changed`、`unsafe_members`、`fetch_error` |
| `config.json` | 合併後設定：public `.security/sweep.yaml`（**只讀 origin 預設分支**）＋ private `targets/<repo>.yaml` |
| `findings.jsonl` | 正規化 findings：`id`、`fingerprint`、`tool`、`rule`、`path`、`line`、`severity_tool`、`message`、`snippet` |
| `tools.json` | 每個工具 `ok`／`unavailable`／`skipped`／`error` |
| `src/` | 預設分支 HEAD 的快照（git archive），deep review worker 讀這裡 |

要讀懂的訊號：

- `config_sources.public == "default"`：repo 還沒有 `.security/sweep.yaml`，套用保守預設（不自動修）。
- `config_changed: true`：`.security/` 有變動，已自動產生一筆 `security-config-changed` finding，必轉人工。
- `unsafe_members` 非空：快照時擋下了指向外部的 symlink 等成員，已產生 high finding。
- `fetch_error` 非空：fetch 失敗，掃的是本機舊的 origin ref，報告要寫明。
- `tools.json` 裡的 `unavailable`／`error`：**一定要寫進報告**，不能讓「沒掃到」看起來像「沒問題」。

## 併入 deep review 的結果

review worker 回報的 finding 先用 Write 寫成 JSON lines（每行 `rule`、`path`、`message`，選填
`line`、`severity`），再：

```bash
python3 /home/elmo/woow-repos/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py import --run-dir <本輪目錄>/<repo 名> --input <jsonl 檔>
```

快照外的路徑、格式錯的行會被丟棄並列在 `rejected`——把數量寫進報告。

## 推進進度

閘門 A 跑完才推進（否則下一輪會漏掉這段 commit）：

```bash
python3 /home/elmo/woow-repos/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-collect/scripts/collect.py --state-dir /home/elmo/woow-repos/.omnigent-runtime/woow-security-sweep commit --run-dir <本輪目錄>/<repo 名>
```
