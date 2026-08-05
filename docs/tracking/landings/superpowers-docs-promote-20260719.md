# project superpowers docs promote 20260719

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_superpowers_docs_promote_20260719`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：2026-07-19 superpowers 批次整理：≤07-10 的 39 檔全搬出（29 promote/10 archive），promote 政策改為 git mv 不留原檔

2026-07-19 執行 superpowers 文件批次整理（Eugene 指示：07-10 含當天以前全部 promote 進 docs 後刪原檔）。結果：**39 檔全搬出 docs/superpowers/**——29 promote（docs/plans/2026-Q2/ ×15、2026-Q3/ ×13、docs/decisions/patterns/seed-agent-template-design.md ×1）＋10 archive（docs/archived/superpowers/，frontmatter 帶 status: archived + archived-reason，逐檔索引在該區 README）。80 檔 inbound 引用改寫（含 CLAUDE.md:289、pattern-guard-map.json seed-default-template class、8 個 src/tests+migration docstring、tests/e2e/skill）。留在 superpowers 的只剩 07-12 後 4 份 plan（wave-a/336/wave-b/issue-368）。

**政策變更（superpowers/README 已改寫）**：promote＝git mv 搬走不留原檔（舊政策「不刪原檔」廢止）；「已落地且知識被主 docs 等值覆蓋」→ archive 而非 promote。2026-05-28 skill-e2e plan/spec 同名互撞→spec 加 `-design` 後綴解。

附帶抽出 4 份新知識檔＋2 份補記（Eugene 核准）：pitfalls session-info-deleted-after-boot、sudo-getter-acl-cache-priming-false-green（自 [[reference_odoo_sudo_getter_cache_priming_test]] 的 repo 版）；patterns ns-quota-hard-limits-limitrange-lockstep；sidecar-backed-flag-lifecycle 補 #271 反轉；ha-component-integration.md 補 mcp key 契約。第 6 份（operator PVC over-label pitfall）**驗證後 skip**——#281 已修（releases.py:246 per-release selector），寫入前驗證機制攔下 stale 主張。

**批次二（同日，PR #376 merged 2026-07-19 commit 903d4658）**：邊界推進到 07-13（含）——3 檔再搬出：336-ha-paas-provision（promote Q3，D1–D6+Q1–Q5 拍板唯一權威）、wave-b-reseed-generator（promote Q3，PR-B2 權威表實測仍現行、plan-catalog-update.md 兩處承重引用同步改）、wave-a-reseed-mechanisms（archive，知識已入 boundary pattern+SOP）。抽出 3 處：新 pattern hidden-template-presentation-chain（is_hidden 呈現層非 ACL、/my //report 繞過、D2 不變式）＋cloud-services.md 補 cs_* overlay 段＋boundary doc 補 storefront_hidden 不用 active 的 rationale 與 HA PaaS 案例更新。superpowers 只剩 07-19 issue-368 一檔（untracked）。**⚠️ 教訓（本檔曾誤記）**：批次二一度只做在分支 `docs/superpowers-promote-0713`/PR #376 open、未 merge，卻被記成「已完成」；2026-07-19 後續 session 查證 develop 原檔仍在才發現，補按合併＋刪分支。**「已完成」的主張必錨定到 merge commit / PR merged 狀態，不可只憑「做在分支」。**

**Why**: 減少 docs/superpowers 文件量、知識入可被引用的主結構；矛盾點（同名互撞、load-bearing spec 搬家）須同步改 hook map。
**How to apply**: 未來 promote 照新政策（git mv＋改寫 inbound 引用＋frontmatter source 溯源）；搬 load-bearing 檔前先 `git grep <basename>` 建引用清單；批次搬移後注意全域字串替換會誤傷 frontmatter `source:` 行（本次踩過，已修）。
