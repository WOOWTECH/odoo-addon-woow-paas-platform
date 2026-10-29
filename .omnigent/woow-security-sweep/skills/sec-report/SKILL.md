---
name: sec-report
description: woow-security-sweep 的報告與 Inbox 提問：report.md 格式、AskUserQuestion 的題目挑選與選項、秘密值遮蔽、high 以上的修正待辦（依 disclosure.high_fix_via）與 baseline 片段。
---

# sec-report

## report.md（每輪必寫，路徑 `<本輪目錄>/report.md`）

Inbox 的提問只是通知與決策入口；**完整紀錄一律在 report.md**（Inbox 項目逾時或被略過也不會遺失）。

```markdown
# 安全掃描 <YYYY-MM-DD>（<排程名或 targets>）

<一句話結論：幾個 repo、幾筆待決、有沒有 high 以上、有沒有工具沒跑到>

## 各 repo
| repo | 範圍 | 新 finding | 待人決定 | Jev 建議丟棄 | 工具狀態 |

## 待決 finding（依嚴重度排序）
### <decision id>｜<repo> <path>:<line>｜<level>
- 工具：<tool> <rule>（deep review 新格式另寫 `verdict`；驗證過的加「已驗證」）
- 說明：<message 摘要>
- 路徑：<trace 的 file:line 依序>（新格式才有）
- 待確認：<blockers>；本機驗證：<validation_plan.local 摘要>；owner 確認：<validation_plan.deployment 摘要>（needs_validation 才有）
- Jev：<verdict>（信心 <c>），建議 <proposed>；理由：<reasons>
- 程式碼：<snippet，秘密值改成 ***>

## 驗證者結論（每週輪次；沒派就寫「本輪未派」）
| finding id | repo | 標題 | 驗證者結論 | 主要修正 |
<rejected 的列在這裡（rejected.jsonl），不進待決；超過上限沒驗證的列為「未驗證」>

## hardening（不構成 finding；hardening.jsonl，可在 Inbox 升級）
| hardening id | repo | 位置 | 建議 |

## entry point 涵蓋（coverage.jsonl）
| repo | entry | 狀態 | 說明 |
<not_reviewed／blocked 排前面；工單有 entry point 但 worker 沒回報 coverage 的，列為「未回報」>

## 之前的 finding 本輪狀態（prior_status.jsonl）
| fingerprint | repo | 狀態（fixed／not_checked） | 依據 |

## 前幾輪未決（carryover.jsonl；依嚴重度排序）
| decision id | repo | 位置 | 工具／rule | Jev | 首次出現 | 已出現輪數 |

## 本輪自動結案（resolved.jsonl；沒有就寫「無」）
| decision id | repo | 位置 | 工具／rule | 原因（reason_code） | 人先前的決定 |

## 已決未進 baseline（decision 帶 prior_human；不再問）
| decision id | repo | 位置 | 人的決定（日期） |

## issue（issue_sync 兩次執行的合計；見 sec-issue skill）
| 動作 | repo | issue | 來源 | 位置 |
<opened／confirmed／closed 逐筆列；skipped、errors 另列原因；都沒有就寫「本輪無變動」>

## Jev 建議丟棄（供抽查）
## 工具與資料缺口
## 給人的待辦（high 以上的修正待辦／baseline 片段）
```

## AskUserQuestion（送進 Inbox）

使用者的 Omnigent Inbox 只會顯示 **AskUserQuestion**（claude-native 的 elicitation）。
⛔ 不要用 `sys_call_async`／`sys_read_inbox` 當通知管道，使用者看不到。

一次最多 4 題。候選＝本輪待決＋carryover（帶 `prior_human` 的不算），挑題順序：

1. high／critical（`proposed` 不論是什麼）
2. 硬規則命中、`config_changed`、`unsafe_members`、疑似 prompt injection
3. Jev 建議 `drop` 的（Phase 2 最先要放開的就是它，最需要人工標記）
4. 其餘依嚴重度；同級時 carryover 的 `seen_runs` 多的優先（拖越久越該問）

題目寫法：`<repo> <path>:<line>（<level>）<一句話說明>。Jev 建議：<proposed>`；needs_validation 在後面加
`待確認：<第一個 blocker>`。選項固定用（description 寫清楚後果）：

| label | 後果 |
|---|---|
| 成立，修正 | ≤ medium 派 fix worker；≥ high 產生修正待辦給你（advisory 草稿或 `-security` repo 步驟） |
| 成立，先追蹤 | 產生 baseline tracked 片段 |
| 誤報 | 記為誤報，產生 baseline 片段供你決定要不要加 |
| 接受風險 | 產生 baseline accepted 片段（6 個月後到期） |

**hardening 升級題**：finding 題目不到 4 題、而且本輪有 hardening 時，用剩下的一題問
「要把哪幾條 hardening 升級成 finding？」（`multiSelect: true`；選項最多 4 條，`label` 用位置、`description` 用建議內容，
優先挑 `context.sensitive_code` 裡的檔案）。其餘 hardening 只列在 report.md。人選了的每一條：

1. `collect promote --hardening-id <id>` ⇒ 新的 `finding_id`
2. `jev_gate triage --run-dir <dir> --only <finding_id>` ⇒ 從 decisions.jsonl 找到它的 decision id
3. `jev_gate human --id <decision id> --label true_positive --action track --note "Inbox 升級自 hardening"`

沒有待決 finding 時只問一題「本輪摘要」（選項：已閱、要求 full 重掃），讓 Inbox 有紀錄；有 hardening 時改問升級題。
超過 4 筆的部分寫在 report.md，題目裡註明「另有 N 筆見 report.md：<路徑>」。
沒問到的會由下一輪的 carryover 再帶出來；只有跑過 carryover 才能這樣告訴人。

## 秘密值遮蔽（送出前逐題檢查）

- gitleaks finding 只寫 rule 與位置，不貼片段
- 片段中長度 ≥ 20 的英數字串、`Bearer …`、`-----BEGIN … KEY-----`、`password=…` 一律改成 `***`
- ⛔ 不貼 `config.json` 的 `context`／`baseline` 原文到 GitHub（它們是 private）

## high／critical 且人選「修正」：給人的待辦

共同內容：標題（中性）、受影響版本範圍（從 `config.json` 的 `stack.version_files` 讀版號）、影響、
重現條件（needs_validation 附 `validation_plan.local`）、建議修法、CVSS 估計、修正驗證方式（`stack.fix_verify`）。
再依 `disclosure.high_fix_via` 附步驟：

| high_fix_via | 步驟 |
|---|---|
| `github_security_advisory`（預設） | GitHub repo → Security → Advisories → New draft advisory → Start a temporary private fork。private fork 不跑 Actions，修正要在本機驗證 |
| `internal_security_repo` | 在 Gitea `mirror-internal` 建**非鏡像** repo `<repo>-security`，推入正本預設分支；修正分支只推到這裡、在這裡開 PR 與跑 CI；發版時由人把修正推回 public 正本，commit 與 PR 描述保持中性 |
| `private_pr` | repo 非公開，可直接在正本開修正 PR；仍由人決定、由人 merge |

`advisory` 工具的 finding（上游已公告、fork 版號落在受影響範圍）：修法是同步上游到修正版以上，
待辦寫出 GHSA、受影響範圍、修正版與 fork 目前版號。根因在上游原樣程式碼、上游還沒公告的問題
（deep review 寫成 hardening「上游問題」）：附上游 SECURITY.md 的通報方式，由人整理可重現的 PoC 再通報。

## baseline 片段（追蹤／誤報／接受風險）

```yaml
  - id: <decision id>
    fingerprint: <finding 的 fingerprint>
    status: accepted | tracked
    reason: <人的說明>
    approved_by: <回答者>
    expires: <今天 + 6 個月，YYYY-MM-DD>   # accepted 必填
```

告訴人：貼進 `woow_paas_platform/.omnigent/woow-security-sweep/targets/<repo>.yaml` 的 `baseline:`
後 commit。⛔ 你不自己改。
