# project pending label issue carrier 20260720

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_pending_label_issue_carrier_20260720`。盤點依據見 [memory-inventory-2026-08-05](../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：2026-07-20 起待拍板/未開項一律以 tea issue 為權威載體＋label；issue-for-prod note 只留一行指針（160→140 行）

2026-07-20 Eugene 拍板的追蹤約定：**待拍板/暫緩/佇列項的權威載體＝tea issue 本體**（脈絡不足就補留言），note `issue-for-prod` 只留一行指針、勿擴寫。repo 新建 label `pending`（灰 8b949e，「暫緩：卡決策或外部前提未定，待拍板後再動工」）。

兩批執行（note 160→140 行、〈⏸ #267〉〈🗂 未開 issue 清單〉兩段刪除）：

1. **待拍板批**：#267（補脈絡留言＋priority high→low）/#316/#319/#323/#352 掛 pending；新開 **#381**（AC/CF end-user 認證策略＋CF ticket U7 併載）。
2. **未開清單批**：新開 **#382–#393** 12 張 fast-follow（pvc-cleanup image/F-ENV-8/F-NETPOL-1/CF tunnel 清理/#315 防線三件/SA 層 MCP e2e/agent_session state machine/prod SMTP/CS hostname CF Access 實測/service_type 殘段/禮品卡 SOP/MCP 效能觀察）＋ **#394**（SA IdP 歸屬，pending）。開單前 staleness 查證擋掉兩張殭屍：#268 測試債（已隨 #356/PR #358 激活）、capability 欄位建模（已隨 Wave A/B 落地）；#382 附 07-20 live 實查證據（prod+stg 皆 bitnami/kubectl:latest）。

**Why:** note 減量——狀態散落 note 對看 issue 的人不透明，且 note 反覆膨脹。
**How to apply:** 新待辦直接開 issue（決策類掛 pending）；已拍板即摘 label 並留言記結論；note 端僅動指針行；**開單前先 staleness 查證**（清單日期舊於近期 merge 波就逐項驗）。列全＝`tea issues list --labels pending`。相關 [[reference_tea_cli_write_gotchas]]。
