# pitfalls/ — 踩坑索引（debug 撞牆時對照）

> 每條 pitfall 一檔完整敘事（症狀 → 根因 → 修法 → 守線）。本表是唯一索引：**一行症狀 + 機械化狀態**。debug 撞牆時先掃症狀欄找同 class；已機械化者撞到 gate / guard 時訊息內附 doc 指針，不需預先記憶。

## 維護慣例（新增 pitfall doc 時）

1. **同步登錄本表一行**（字母序插入），格式：`slug 連結 | 一行症狀 | 機械化狀態`。CLAUDE.md 只留指向本表的一行入口，**勿把 pitfall 段落寫回 CLAUDE.md**（雙寫必漂移）。
2. **評估機械化**（`/retro` skill 的分流，按強度優先）：可 grep/parse 判定 → `scripts/audit/repo-lint.py` 加 check；綁特定路徑 → `.claude/hooks/pattern-guard-map.json` append；皆不可 → 狀態欄留 `—`。
3. 教訓已被 code guard / gate 取代時，更新狀態欄並讓 doc 記錄 guard 位置（file:line）。

## 索引（41 條）

| pitfall | 一行症狀 | 機械化狀態 |
| --- | --- | --- |
| [agent-runtime-coldstart-cpu-throttle-timeout](agent-runtime-coldstart-cpu-throttle-timeout.md) | cold-start spawn ~36s 撞 client timeout，根因 operator CPU limit 250m throttle（非同步等 pod-ready） | — |
| [agent-runtime-fpilot10-cluster-e2e-gap-chain](agent-runtime-fpilot10-cluster-e2e-gap-chain.md) | F-PILOT-10 真 LLM cluster e2e 的 8-gap chain（mock/caqa/reviewer 全看不到，唯 cluster + 完整 UI round-trip 暴露） | — |
| [agent-runtime-llm-key-after-coldstart-stuck-echo](agent-runtime-llm-key-after-coldstart-stuck-echo.md) | LLM key 在 pod cold-start 之後才設 → 永久 echo（provisioning 只在 cold-start；唯一解 teardown 重 cold-start） | — |
| [agent-runtime-pilot-e2e-gap-chain](agent-runtime-pilot-e2e-gap-chain.md) | agent-runtime pilot e2e 的 9-gap chain（flip 實跑才一關關現形） | — |
| [agent-runtime-stg-enablement-gap-chain](agent-runtime-stg-enablement-gap-chain.md) | stg 啟用的 5 層 fail-closed gap chain；單層修復不可觀測，須逐層驗到端到端；K3s NP 回 RST 非 timeout | — |
| [agent-runtime-webhook-statesync-deploy-hazards](agent-runtime-webhook-statesync-deploy-hazards.md) | webhook state-sync 在 stg 接通的 3 個部署層陷阱（含一次 namespace 資料全失） | — |
| [assertlogs-string-level-shadowed-by-odoo](assertlogs-string-level-shadowed-by-odoo.md) | `assertLogs(level='INFO')` 在 Odoo 進程是 setLevel(25)（netsvc addLevelName 覆寫 `_nameToLevel`）→ 守 `_logger.info` 的測試**引入即恆紅**、訊息卻長得像 code 掉了 log；`assertNoLogs` 版本則恆綠 | repo-lint `check_assertlogs_numeric_level`（第 11 條）|
| [cf-ns-orphan-no-account-gc](cf-ns-orphan-no-account-gc.md) | CF/ns 孤兒無帳號級反向 GC；繞過 Odoo unlink 的刪除 → stored id 蒸發成永久孤兒；SmartHome 連 fail-loud guard 都沒有 | — |
| [ci-addon-ref-empty-drifting-default-branch](ci-addon-ref-empty-drifting-default-branch.md) | 人工重跑 build 忘填 `addon_ref` → CI clone 移動中的預設分支 HEAD；CI 全綠、image_tag 與 digest 全新、pod digest 閉環全對，但 image 內容是另一個版本（要到 pod 跑 `-u` 才現形） | CI 兩道 fail-closed（`paas-odoo-ci/build-image.yml`，跨 repo）：`addon_ref` 必填（#46，擋本條）＋ image↔clone 版號比對擋在 push 前（#50，擋另一類）；SOP §11 checklist 1.5 降為複核（**ref 填錯 CI 一律全綠**，只有 `at ref:` vs 發版單抓得到） |
| [ci-local-kubectl-cp-root-owned-addons](ci-local-kubectl-cp-root-owned-addons.md) | ci-local sandbox 的 `/mnt/extra-addons` 整棵 root-owned、容器跑 `uid=100(odoo)` → `kubectl cp` 熱蓋檔案被 **`tar` 誤報成「Cannot open: File exists」**（同一個目標改用 `cat >` 則誠實回 Permission denied）；繞法 `kubectl debug --target` 借 root chown，**且 chown 必須用數字 `100:101`**——busybox debug image 用名字回 unknown user/group 卻不中斷指令，做完與沒做長得一樣 | — |
| [ci-local-stale-pod-green-tests-on-old-code](ci-local-stale-pod-green-tests-on-old-code.md) | ci-local 的 code 是 image 快照，pod 一重建就退回舊版、前一輪 `kubectl cp` 的修補全消失；**失效是靜默的**：`1/1 Running` 無警告、`woow-test.sh` 照跑照綠，而那個綠燈驗的是舊碼 → 沒先讀 pod 內版號就跑測試＝產出有測試數字佐證但錨點錯掉的假驗收 | — |
| [ci-workflow-container-missing-node](ci-workflow-container-missing-node.md) | CI `container: <lang-image>` 缺 node → checkout exit 127 → pytest 從沒跑到（false green）；本 repo Odoo CI 不受影響 | — |
| [cross-repo-artifact-landing-assumption](cross-repo-artifact-landing-assumption.md) | plan 假設部署 artifact 落 cross-repo X 實際落本 repo Y → impl 前按「修改誰的 deployment」勘查實體落點 | — |
| [cross-workspace-task-data-leak](cross-workspace-task-data-leak.md) | CAQA B1 incident：跨 workspace task 資料外洩 | — |
| [cs-binding-authority-controller-only-no-model-guard](cs-binding-authority-controller-only-no-model-guard.md) | project↔CloudService 綁定授權只在 controller 層 gated、model 層無守（defense-in-depth 缺口） | — |
| [decouple-project-frontend-worktree-dev-pitfalls](decouple-project-frontend-worktree-dev-pitfalls.md) | decouple-project frontend cycle 的 3 條 worktree 開發環境陷阱 | — |
| [default-agent-template-project-bind-silent-autoenable](default-agent-template-project-bind-silent-autoenable.md) | 平台預設 agent_template 被綁 project → auto-enable 靜默全失效（該綁的是 demo 不是 default） | code guard（`src/models/agent_template.py:125`）+ pattern-guard |
| [direct-unlink-bypass-cleanup-hook](direct-unlink-bypass-cleanup-hook.md) | direct `.unlink()` 繞 lifecycle cleanup → child wrapper zombie | — |
| [fleet-split-registry-values-merge-drift](fleet-split-registry-values-merge-drift.md) | Fleet secret drift 勿急套 split-merge 框架——本案真因是 chart 外殘留 key（✅ 2026-06-25 已解決＋根因更正） | — |
| [floor-env-relative-path-empty-ext-addons](floor-env-relative-path-empty-ext-addons.md) | Floor `.env` 相對路徑恰好掛到存在但空的目錄騙過 create.sh → module 靜默裝不起來；Floor 一律絕對路徑 | — |
| [httpcase-orm-flush-ordering-raw-sql](httpcase-orm-flush-ordering-raw-sql.md) | HttpCase 內 raw SQL 造的 drift sample 被 ORM buffered write flush 覆蓋 | — |
| [legacy-endpoint-scope-blindness](legacy-endpoint-scope-blindness.md) | plan AC 只 cover 新 endpoint，legacy 同源 code path 漏裝同層 security helper | — |
| [netpol-enforced-except-fresh-pod-startup-window-k3s-kube-router](netpol-enforced-except-fresh-pod-startup-window-k3s-kube-router.md) | NP 對新建 pod 有啟動編程空窗 → 用臨時 pod 測 NP 會系統性誤判「控制器全毀」 | — |
| [odoo-object-button-return-value-is-an-action](odoo-object-button-return-value-is-an-action.md) | 被 XML `<button type="object">` 綁著的 method 改回傳 dict → web client 當 action 執行、缺 `type` 就 throw（bool/None 才安全）；危險點是 **Python 呼叫端的完整盤點看不到 XML 這一端**；配套：改成 raise 則例外逸出 dispatcher ⇒ 不 commit ⇒ 連 except 裡剛寫的稽核紀錄一起回捲 | —（gate 已評估**否決**：AST 判不出變數 return ⇒ 必然大量漏抓 ⇒ 假安全感比沒 gate 更糟；理由見 doc 內「為什麼機械化狀態是 —」） |
| [odoo-session-info-deleted-after-boot](odoo-session-info-deleted-after-boot.md) | 前端直讀 bare `odoo.__session_info__` 恆 undefined（session service boot 後即 delete）→ flag 靜默失效，XML/Py 測試全綠唯 headed E2E 測得出；正解 `@web/session` | — |
| [operator-cf-access-delete-202-mock-contract](operator-cf-access-delete-202-mock-contract.md) | operator 包 CF Access 的 DELETE 白名單寫死 200，真 CF 回 202 → teardown 中斷孤兒 policy；mock 狀態碼要對齊真實 API | pattern-guard（paas-operator class） |
| [operator-route-none-conflates-disabled-and-failed](operator-route-none-conflates-disabled-and-failed.md) | 呼叫端把「沒拿到 route」當成失敗 ⇒ 在沒開 Cloudflare 的環境（`cloudflare_enabled` 預設 false）把**每一次部署**翻成 error；缺席同時代表「功能沒開」與「舊 operator 沒這個欄位」，兩者都不是故障 | pattern-guard（paas-operator ＋ model-write-guard class）＋ repo-lint `route-contract-golden`（守**另一個性質**：wire 形狀改了 golden 沒重產）。⚠「呼叫端把缺席當失敗」那一半的 gate 已評估**否決**（等價寫法太多必然漏抓）；指紋 gate 也**不涵蓋** `_create_cloudflare_route` 的行為漂移。兩條都擋不住 merge（repo-lint 非 required）。逐項見 doc 內「守線」段 |
| [orm-test-green-endpoint-leak-blindness](orm-test-green-endpoint-leak-blindness.md) | backend ORM test 全綠遮蔽 controller layer sudo bypass；ORM 綠 ≠ endpoint secure | repo-lint gate（controller-sudo）+ pattern-guard |
| [owl-template-crash-class-syntax](owl-template-crash-class-syntax.md) | OWL template 語法錯誤（component `t-att-`、`.enter` modifier）過三層測試、runtime 全站白屏 | repo-lint gate（owl-template） |
| [plan-vs-odoo-schema-mismatch](plan-vs-odoo-schema-mismatch.md) | plan 引用不存在的 Odoo 欄位——impl 前對 schema | — |
| [provision-e2e-green-on-chart-defaults](provision-e2e-green-on-chart-defaults.md) | provision e2e 綠但 release user-supplied values 空——驗證跑在 chart 預設上、平台 pin 傳導鏈從未被驗（07-13 假 PASS；0.2.x fail-loud 佔位 tag 逼現形） | 回歸測試（`test_provision_writes_template_helm_values`）；class 級驗法＝e2e 斷言 helm get values 非空且含平台獨有值 |
| [readonly-audit-default-report-overwrites-repo](readonly-audit-default-report-overwrites-repo.md) | 跑完「唯讀」盤點腳本後 repo 內已 commit 的報告變空骨架——唯讀鐵律只涵蓋叢集側，`--report` 預設值直指 repo 檔且無條件覆寫；空集合環境跑一次即靜默洗檔（exit 0 無警告） | 預設落點改 tmp（`scripts/audit/audit_411_reset_values.py:468`）+ 跨環境 fail-loud（`:484`）+ 回歸測試（`TestReportTarget`） |
| [reaped-since-single-path-stamp-gc-immunity](reaped-since-single-path-stamp-gc-immunity.md) | 下游 gate 欄位只在單一路徑 stamp → 他路徑到達 gating state 者永久免疫 GC | code guard（`src/models/agent_session.py:186-207`） |
| [sandbox-module-test-false-green](sandbox-module-test-false-green.md) | K8s dev sandbox `woow-test.sh` 0 tests 也報「✅ All tests passed」（false green） | — |
| [seed-noupdate-sticky-template-links](seed-noupdate-sticky-template-links.md) | seed `noupdate="1"` 寫進 ir.model.data 變 sticky → 其他 data 檔對同 record 的 `-u` 更新被靜默 skip；fresh install 綠 ≠ 升級路徑綠 | repo-lint gate（data-noupdate）+ pattern-guard |
| [skill-same-name-dual-bind-snapshot-collision](skill-same-name-dual-bind-snapshot-collision.md) | skill 同名雙綁 → snapshot 撞名 + 下拉無法區分同名 system/user skill | — |
| [sudo-getter-acl-cache-priming-false-green](sudo-getter-acl-cache-priming-false-green.md) | 測 sudo getter ACL fix 假綠雙陷阱：sandbox 灌 admin 群組讓受限 owner 不受限＋admin 讀過的 ORM cache 讓 owner env 繞過 ACL；必須 groups 硬重設＋`invalidate_all()` 直呼 getter（pre-fix 紅／post-fix 綠判準） | — |
| [system-mcp-create-acl-bypass](system-mcp-create-acl-bypass.md) | dual-leg rule + `perm_create=True` → 經 always-true leg 提權 | pattern-guard（acl-security class） |
| [tag-scoped-test-misses-full-u-failures](tag-scoped-test-misses-full-u-failures.md) | `--test-tags` scope-limited 驗綠 → cross-module stale schema reference 漏抓 | — |
| [two-layer-acl-asymmetric-deny](two-layer-acl-asymmetric-deny.md) | 兩層 ACL 收緊一層 silent 漏另一層——partial fix 比沒 fix 更糟 | pattern-guard（controller-endpoint class） |
| [x2many-replace-transient-empty-state-operator-sync](x2many-replace-transient-empty-state-operator-sync.md) | x2many `(5,0,0)+creates` 整組替換的瞬時空狀態觸發 child-model eager 外部 sync → operator 收到不合法中途態 422 | — |
