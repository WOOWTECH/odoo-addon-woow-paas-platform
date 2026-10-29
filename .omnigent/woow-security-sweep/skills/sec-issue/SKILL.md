---
name: sec-issue
description: woow-security-sweep 自動開 issue：用 issue_sync.py 把人判成立或 Jev 高信心成立的 finding 開到 targets 指定的非 public Gitea repo（disclosure.issues_to，預設 mirror-internal/<repo>），並依人的決定更新或關閉；label 規則與報告寫法。
---

# sec-issue

`<WS>`＝runner workspace（取得方式見 AGENTS.md「固定路徑」），指令裡一律代入字面值。

判定成立的 finding 開成 Gitea issue，讓修正有工單可追。issue 開在 targets 的 **`disclosure.issues_to`**
（沒寫時是 `mirror-internal/<repo>`）。腳本寫入前會確認目標**不是 public**（repo 是 private，或 org 是
limited／private），否則整批中止。⛔ 不開在 GitHub，也不開在 Gitea 的 public org（例如 `ha-components`）：
未修先開等於公開漏洞細節。

## 指令

```bash
python3.12 <WS>/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-issue/scripts/issue_sync.py --state-dir <WS>/.omnigent-runtime/woow-security-sweep sync --run-dir <本輪目錄>/<repo 名>
```

- 每個 target 各跑一次；**冪等**，重跑不會重複開單
- `--dry-run`：只列出會做的事，不寫 Gitea、不寫 shadow log
- ⛔ 不要自己用 tea／curl 開 issue、留言、改 label（`p_gitea_issue_via_script` 會擋）。腳本負責寫死
  owner、遮蔽秘密值、去重；繞過它這些檢查就都沒了

## 什麼時候跑

AGENTS.md 每輪流程的兩個時間點各跑一次：

1. carryover 之後：Jev 高信心的不等人回答就先開
2. 記錄完人的決定之後：人判成立的開單、人判誤報的關單

## 腳本做什麼

| 情況 | 動作 | 來源 label |
|---|---|---|
| 人判 `true_positive`，action 為 fix／track／escalate | 開單 | `sweep/human` |
| 還沒有人工決定，Jev 判 `true_positive` 且信心 ≥ `gates.yaml` 的 `issues.jev_min_confidence` | 開單 | `sweep/jev` |
| `sweep/jev` 的 open 單，後來人判成立 | 留言、換 label | `sweep/human` |
| 已開的 open 單，人判誤報或 action 為 drop／accept_risk | 留言、換 label、關閉 | `sweep/false-positive` |
| 已開的 open 單，carryover 判 `resolved`（`file_removed`／`not_redetected`；人判成立過的也算） | 留言（原因代碼＋head SHA）、關閉 | 不改 |
| `resolved` 的 finding 還沒開單 | 不開 | — |
| 單已被人手動關閉 | 不動 | — |

每張單都掛 `woow-security-sweep`（標示來源）與 `severity/<level>`。`sweep/*`、`severity/*` 是
exclusive scope，同 scope 只會留一個。缺的 label 腳本會自己建。

去重靠兩層：shadow log 的 `type: issue` 紀錄，以及 issue body 裡的
`<!-- woow-security-sweep:fp=… -->` 標記。

## 輸出與報告

腳本印出 JSON：`opened`、`confirmed`、`closed`（各附 issue 連結；因 resolved 關的另附 `reason`）、
`skipped`（附原因）、`errors`。

- exit 0：正常
- exit 4：部分動作失敗，失敗的列在 `errors`，其餘照常完成。寫進報告，下一輪會重試
- exit 2：設定錯誤（例如 `issues_to` 格式不對、目標是 public、查不到目標的可見度）。寫進報告第一段，本輪不再重試

把結果寫進 report.md 的「issue」一節（格式見 sec-report）。Inbox 摘要題可附開出的單數。
`skipped` 原因是「遠端找不到」的，代表有人拿掉了 `woow-security-sweep` label；只回報，不重開。
