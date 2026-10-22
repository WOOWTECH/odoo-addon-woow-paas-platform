---
name: sec-report
description: woow-security-sweep 的報告與 Inbox 提問：report.md 格式、AskUserQuestion 的題目挑選與選項、秘密值遮蔽、advisory 草稿與 baseline 片段。
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
- 工具：<tool> <rule>
- 說明：<message 摘要>
- Jev：<verdict>（信心 <c>），建議 <proposed>；理由：<reasons>
- 程式碼：<snippet，秘密值改成 ***>

## 前幾輪未決（carryover.jsonl；依嚴重度排序）
| decision id | repo | 位置 | 工具／rule | Jev | 首次出現 | 已出現輪數 |

## 已決未進 baseline（decision 帶 prior_human；不再問）
| decision id | repo | 位置 | 人的決定（日期） |

## Jev 建議丟棄（供抽查）
## 工具與資料缺口
## 給人的待辦（advisory 草稿／baseline 片段）
```

## AskUserQuestion（送進 Inbox）

使用者的 Omnigent Inbox 只會顯示 **AskUserQuestion**（claude-native 的 elicitation）。
⛔ 不要用 `sys_call_async`／`sys_read_inbox` 當通知管道，使用者看不到。

一次最多 4 題。候選＝本輪待決＋carryover（帶 `prior_human` 的不算），挑題順序：

1. high／critical（`proposed` 不論是什麼）
2. 硬規則命中、`config_changed`、`unsafe_members`、疑似 prompt injection
3. Jev 建議 `drop` 的（Phase 2 最先要放開的就是它，最需要人工標記）
4. 其餘依嚴重度；同級時 carryover 的 `seen_runs` 多的優先（拖越久越該問）

題目寫法：`<repo> <path>:<line>（<level>）<一句話說明>。Jev 建議：<proposed>`；
選項固定用（description 寫清楚後果）：

| label | 後果 |
|---|---|
| 成立，修正 | ≤ medium 派 fix worker；≥ high 產生 advisory 草稿給你 |
| 成立，先追蹤 | 產生 baseline tracked 片段 |
| 誤報 | 記為誤報，產生 baseline 片段供你決定要不要加 |
| 接受風險 | 產生 baseline accepted 片段（6 個月後到期） |

沒有待決 finding 時只問一題「本輪摘要」（選項：已閱、要求 full 重掃），讓 Inbox 有紀錄。
超過 4 筆的部分寫在 report.md，題目裡註明「另有 N 筆見 report.md：<路徑>」。
沒問到的會由下一輪的 carryover 再帶出來；只有跑過 carryover 才能這樣告訴人。

## 秘密值遮蔽（送出前逐題檢查）

- gitleaks finding 只寫 rule 與位置，不貼片段
- 片段中長度 ≥ 20 的英數字串、`Bearer …`、`-----BEGIN … KEY-----`、`password=…` 一律改成 `***`
- ⛔ 不貼 `config.json` 的 `context`／`baseline` 原文到 GitHub（它們是 private）

## advisory 草稿（high／critical 且人選「修正」）

寫進報告「給人的待辦」：標題（中性）、受影響版本範圍（`__manifest__.py` 版號）、影響、
重現條件、建議修法、CVSS 估計。附上步驟：GitHub repo → Security → Advisories →
New draft advisory → Start a temporary private fork。修正驗證要在本機 Odoo 跑（private fork
不跑 GitHub Actions）。

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
