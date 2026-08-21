# project 271 internal mcp bypass landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_271_internal_mcp_bypass_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。

W1 launch blocker #271「Security Access 內網自動 bypass」執行落地。

**Framework flip（非顯而易見）**：原以為要「seed CF bypass policy」，實則 route (a)（sidecar 內網直連 `http://...svc.cluster.local`）**已是 de facto 狀態**，本 epic 改為**防 regress 守衛** —— 把「`sidecar_backed=True ⟹ url 為 in-cluster`（scheme=='http' 且 hostname 以 `.svc.cluster.local` 結尾）」固化成 invariant。不誤傷合法需求：外部/獨立/公開 MCP 一律 `sidecar_backed=False`，access control 由獨立的 `SecurityAccessRoute` CF 層負責（與此 invariant 正交；Workflow 3 skeptic 對抗查證全 refuted）。#271≠#279（後者是 NetworkPolicy 租戶隔離，別混判）。

**狀態（2026-07-06）**：PR **#308** open → develop（No Conflicts），branch `feat/271-internal-mcp-bypass-guard`（Gitea origin=git-prod.woowtech.io）。6 task 全 TDD（watch RED→GREEN）：① model `@api.constrains`+共用 `_is_in_cluster_url` helper（無 su 豁免，two-layer-acl-lockstep）② controller `_validate_mcp_url` 雙層 friendly error（4 端點 pre-write）③ migration `18.0.1.0.43` healer（row-by-row `filtered` 與 constraint lock-step）④ 未部署不顯示 Add Sidecar（`cloud_service_deployed` flag+前端 `canAddSidecarMcp` gate）⑤ 刪 dead code ⑥ teardown 對稱+反轉既有 orthogonality 斷言。全模組 1471 tests：#271 六 class 全綠，唯二紅是 pre-existing `TestDemoSystemSkill` filestore env noise（與 #271 零交集）。

**測試路徑**：k8s-dev-sandbox local mode 壞（docker-desktop k8s VM 看不到 macOS hostPath，見 [[reference_docker_build_local_odoo_test]]）→ 用 docker-build-local recipe，環境 `fllm271-test:local`+pg `fllm271pg`（network `fllm271net`）+testdb 常駐；rebuild=rsync worktree src + sed 刪 fastembed + docker build（dctx271 在 scratchpad）。

**2 處執行偏離（非湊綠、PR body 有記）**：Task 2 pre-write guard 額外修掉 project-update catch-ValidationError-未-rollback 的 partial-persist；Task 3 healer 加 `flush_all()` 讓 ORM write 對 raw-SQL 測試可觀察（生產由 migration 後 commit 自然 flush，無害）。

**狀態（2026-07-06 全部收尾）**：PR #308 merged + worktree/remote branch 已清 + stg 部署（image `20260706.0745` from develop、migration `18.0.1.0.43` post-migrate 執行）+ **Task 4 headed 前端 gating 已驗 PASS**（stg 截圖+DOM 雙確認：未部署頁無 Add Sidecar、已部署有；seed 已清 stg pristine 12 project/5 CS）。**prod 需留意**：`portal_project_settings_editable` config flag off 時整個 MCP section（含 Add Sidecar/External）不渲染 → prod 開這功能要先開 flag。#271 剩 prod 上線（code 隨 addon `18.0.1.0.43` 上 prod、healer 自動跑）+ #271+#279 合併端到端連通驗證（見 [[reference_woow_playwright_e2e_gotchas]]）。其餘 W1 blocker：#268/#275/#278 未動。
