---
name: sec-gate
description: woow-security-sweep 的 Jev 判斷閘門：jev_gate.py 的 triage（閘門 A）、ready（閘門 C）、human（記錄人的決定）、calibrate（校準統計）用法與 shadow mode 規則。
---

# sec-gate

`<WS>`＝runner workspace（取得方式見 AGENTS.md「固定路徑」），指令裡一律代入字面值。

Jev 只回答問題（choice／score／noul ＋ 信心值），**路由由 jev_gate.py 的程式碼決定**：
硬規則 → baseline → Jev 答案 → `gates.yaml` 門檻。⛔ 不要自己讀 Jev 的原始答案再「覺得」該怎麼走。

所有指令共用前綴（下稱 `JG`）：

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-gate/scripts/jev_gate.py --state-dir <WS>/.omnigent-runtime/woow-security-sweep
```

## 閘門 A：triage

`JG triage --run-dir <本輪目錄>/<repo 名>` ⇒ 寫 `decisions.jsonl`（每行含 `id`、`finding`、`answers`、
`info`、`proposed`、`effective`、`reasons`），並追加到 shadow log。stdout 是各 route 的數量。

| exit | 意義 | 你要做的 |
|---|---|---|
| 0 | 正常（個別失敗的筆數在 `errors`，那幾筆已 fail-closed 成 human） | 繼續 |
| 2 | 設定或 state 目錄錯誤 | 報告寫明、該 target 不 commit |
| 3 | API key 失效（401） | 全部轉人工；報告第一行寫「Jev 不可用」 |

route 的意思：

| proposed | 意思 |
|---|---|
| `drop` | 判誤報（高信心、≤ medium）或 baseline 已接受／已追蹤 |
| `issue_only` | 成立，但不適合自動修 |
| `auto_fix` | 成立、≤ medium、可小範圍修正 |
| `human` | 硬規則命中、`needs_validation`（決定性事實不在原始碼）、high 以上、Jev 不確定、疑似 prompt injection、或呼叫失敗 |

**shadow mode：`effective` 一律是 `human`**。`proposed` 只是給人參考、給校準用的，你不依它行動。

新格式 deep review finding 的 `trace`、`root_cause`、`conditions`、`blockers` 會一併送給 Jev。
`verdict: needs_validation` 不論 Jev 怎麼答都是 `human`（理由欄帶前幾個 blocker）；baseline 已追蹤／接受的照樣 `drop`。

只判部分 finding（hardening 升級後）：`JG triage --run-dir <本輪目錄>/<repo 名> --only <finding_id>`——
追加到既有 `decisions.jsonl`，已判過的 id 略過，不會清掉本輪其他筆。

decision 帶 `prior_human`（`label`／`action`／`ts`）⇒ 同一個 fingerprint 之前已經有人決定過、只是還沒進
baseline。不要再問，報告列在「已決未進 baseline」。

## 跨輪未決：carryover（每個 target 在 triage 之後都要跑）

`JG carryover --run-dir <本輪目錄>/<repo 名>` ⇒ 寫 `carryover.jsonl` 與 `resolved.jsonl`，stdout 是
`{"carried": N, "resolved": M}`。

- 帶進來的是：同 repo、前幾輪 Jev 判過、**人還沒決定**、本輪沒再偵測到也沒被判 resolved 的 finding。
  每行有 `decision_id`（記人的決定時用這個 id）、`finding`、`proposed`、`first_seen`、`seen_runs`。
- `resolved`（寫入 shadow log，之後不再帶；本輪新判的另列在 `resolved.jsonl`，每行有 `reason_code`）：
  - `file_removed`：finding 的檔案已不在本輪 HEAD。**不看工具狀態，人已決定過的也照判**，讓 issue_sync 關單。
    gitleaks 例外：秘密值仍留在 git 歷史，要人處理輪替，不因刪檔結案。
  - `not_redetected`：檔案本輪有掃到、該工具本輪正常，卻沒再偵測到（只判人還沒決定的）。
- 檔案還在但工具失敗、deep review 沒跑 ⇒ 不判 resolved，照樣帶（寧可多問）。
- 人在 Inbox 回答 carryover 項目：`JG human --id <decision_id> ...`，與本輪項目相同。

## 閘門 C：ready

修正 PR 的 CI、重掃、review 都有結果後，把 case 寫成 JSON：

```json
{"finding": {...decisions 裡那筆 finding...}, "attempt": 1, "ci": "pass|fail|pending",
 "rescan": {"finding_gone": true, "new_findings": 0},
 "review": {"verdict": "approve|request_changes", "summary": "..."},
 "changed_files": ["models/x.py"], "deleted_or_modified_tests": [],
 "diff": "<git diff 文字>", "commit_message": "..."}
```

`JG ready --case <case.json> --config <本輪目錄>/<repo 名>/config.json` ⇒ `ready`／`retry`／`human`／`abandon`。
shadow mode 下同樣只是建議；結果寫進報告並問人。

## 記錄人的決定（每個 Inbox 回答都要記）

`JG human --id <decision id> --label <label> --action <action> [--note "<人的補充>"]`

| 人在 Inbox 選的 | label | action |
|---|---|---|
| 成立，修正 | `true_positive` | `fix` |
| 成立，先追蹤 | `true_positive` | `track` |
| 誤報 | `false_positive` | `drop` |
| 接受風險 | `true_positive` | `accept_risk` |
| 需要更多資訊 | `needs_context` | `escalate` |
| 修正可以 merge（閘門 C） | `ready` | `merge` |
| 修正要再改（閘門 C） | `not_ready` | `request_changes` |

## 校準（人要求時才跑）

`JG calibrate --gate triage` ⇒ 依信心區間列出 Jev 與人的一致率、樣本數是否 ≥ 50。
⛔ 你不改 `gates.yaml`；把結果貼進報告，由人決定是否進 Phase 2。

## 冒煙測試

`JG models` 會列出帳號可用的 model 與 gates.yaml 釘的版本；不會印出 API key。
