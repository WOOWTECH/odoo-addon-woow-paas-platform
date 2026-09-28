---
name: retro
description: Session 收尾的教訓蒸餾。當 debug / incident / e2e 排錯 session 結束，或使用者說「/retro」「收尾」「這次學到什麼」時使用。掃描本次對話的翻車點，判定是否持久化，並選最強落點（gate > hook map > pattern doc > pitfall doc）。
---

# Retro：教訓蒸餾

## Steps

1. **掃描本 session**：根因是什麼？哪一刻「測試綠但實際壞」（假綠）？哪個前提假設被推翻？哪次 debug 繞了遠路、缺什麼指引本可直達？
2. **分類**：一次性事件（不持久化，口頭總結即可）vs 會重複的 class。判準：同 class 可能再發生、且下次撞到的 debug 成本 > 30 分鐘。
3. **選落點（按強度優先，選最強可行者）**：
   - a. 可機械判定（grep / parse 可攔）→ `scripts/audit/repo-lint.py` 加 check + `--update-baseline`
   - b. 綁特定路徑的 read-first 知識 → `.claude/hooks/pattern-guard-map.json` append docs 行或新增 class
   - c. 跨情境判斷類 → `/docs:new decisions <slug>`（pattern doc）+ 登錄 patterns/README.md 索引（含頂部計數）；無路徑可綁時另登記 `scripts/audit/repo-lint.py` 的 `PATTERN_DOC_MAP_EXEMPT`（repo-lint `pattern-doc-registry` 三方雙射會紅）
   - d. 敘事對照類 → `/docs:new tracking <slug>`（pitfall doc）+ 登錄 pitfalls/README.md 索引
4. **產出 diff 提案**給使用者核可後落檔；一個教訓可同時落 a+c（gate 攔 + doc 解釋為什麼）。
5. **反向檢查**：這次的教訓是否讓某條既有散文守線變多餘（已被 code guard / gate 取代）？是 → 提案把該條從 CLAUDE.md / index 降級或刪除。CLAUDE.md 不因 retro 而變長是本 skill 的硬約束。

## 紅線

- 不把已機械化的教訓再寫成散文提醒。
- 不新增「要注意 X」式無判準條目——寫不出觸發條件（何時、哪個檔、什麼症狀）的教訓還不成熟，留在 session 總結即可。
