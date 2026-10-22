# `_shared/`：worker prompt 的共用片段

八份 `_templates/<role>/AGENTS.md` 裡逐字相同的段落抽到這裡，模板用一行
`<!-- include: <片段名> -->`（獨占一行）引用；`generate-worker.py` 複製 AGENTS.md 時把該行
換成片段全文（含片段自己的 `## ` 標題）。⛔ 不支援巢狀 include、不支援參數。

| 片段 | 誰用 |
| --- | --- |
| `iron-rules.md` | 全部八份（ops_k8s 另保留它自己的「發版核可 gate」段） |
| `guard-strength.md` | 全部八份 |
| `grep-pattern-is-data.md` | implementer_pi／implementer_cc／verifier_cc／verifier_pi／scout_pi／tracker／researcher／ops_k8s |
| `cross-repo-readonly.md` | researcher／scout_pi／tracker／verifier_cc／verifier_pi（唯讀角色；實作 worker 用 `stay-in-worktree`） |
| `stay-in-worktree.md` | implementer_pi／implementer_cc |
| `report-readonly.md` | researcher／scout_pi／tracker／verifier_cc／verifier_pi（實作與 ops_k8s 的回報段各有專屬內容，不共用） |

守線：

- 片段不存在 ⇒ 產生器 fail loud（防線 2 的一部分，驗收第 4 項會紅）。
- 產出裡殘留 `<!-- include:` ⇒ 同上。
- 驗收第 20 項對**所有**模板解析 include（不論 ROSTER 是否引用），並驗
  每個片段至少被一份模板引用（孤兒片段＝沒人讀的文字）。

⚠ 片段裡提到的 policy 若只掛在部分角色（`P-path` 只掛 implement、`P-kubectl` 只掛 ops_k8s），
要在片段裡**寫明只掛在誰身上**——同一段文字會進到每一個角色的 prompt。
