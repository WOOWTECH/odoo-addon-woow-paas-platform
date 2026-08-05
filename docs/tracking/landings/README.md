# landings/ — 已收官 issue 的落地紀錄

每份記錄一張 issue（或一組相關 issue）**當初是怎麼收的**：改了什麼、PR 編號、
驗證方式、以及當次挖出來的教訓。檔名帶 issue 編號，直接 `ls | grep <編號>` 即可定位。

> 來源：2026-08-05 自 Claude memory 目錄遷移的 67 篇 `project_*_landing` 紀錄。
> 分類依據見 [`../memory-inventory-2026-08-05.md`](../memory-inventory-2026-08-05.md)。

**這裡放什麼**：已 merge / 已上線、事實不再變動的收官紀錄。

**這裡不放什麼**：
- 還在進行中的追蹤項 → [`../followups/`](../followups/)
- 「症狀 → 根因」型的除錯對照 → [`../pitfalls/`](../pitfalls/)
- 可複用的設計原則 → [`../../decisions/patterns/`](../../decisions/patterns/)

⚠ 收官紀錄裡的**跨任務教訓**若還會再犯，應另外抽成 pattern / pitfall 文件——
留在 landing 檔裡只有查得到當初那張單的人才看得到。
