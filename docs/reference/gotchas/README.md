# gotchas/ — 框架與基礎設施的行為陷阱

「照直覺做會錯、踩過才知道」的**事實**：某個 API 在特定條件下靜默失敗、
某個 flag 的預設值與文件不符、某版工具改了契約。

> 來源：2026-08-05 自 Claude memory 目錄遷移。
> 分類依據見 [`../../tracking/memory-inventory-2026-08-05.md`](../../tracking/memory-inventory-2026-08-05.md)。

**與相鄰目錄的分工**：

| 這裡（`reference/gotchas/`） | 相鄰 |
|---|---|
| 「這個東西**就是這樣運作**」——外部框架／基礎設施的既定行為，我們改不了 | [`../../tracking/pitfalls/`](../../tracking/pitfalls/)：「我們的系統**當時壞了**」——有症狀、有時間點、有根因 |
| 陳述事實，讀者用來預防 | [`../../decisions/patterns/`](../../decisions/patterns/)：陳述**我們選擇怎麼做**，有 hook 強制閱讀 |

⚠ 這裡的內容會隨上游版本過期。寫的時候標明**實測版本與日期**，
不要寫成無時效的斷言。
