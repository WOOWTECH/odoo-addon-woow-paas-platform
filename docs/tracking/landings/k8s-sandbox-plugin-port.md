# project k8s sandbox plugin port

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_k8s_sandbox_plugin_port`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：把 k8s-dev-sandbox skill 移植成 woow-dev（時名 woowprd）plugin 通用 skill 的可行性結論與雙根架構決策

**2026-07 名稱異動**：plugin 已拆分為 marketplace `woowprd-local` 下的 `woow-dev`（invocation `/woow-dev:k8s-dev-sandbox`；源 repo `~/.claude/plugins-local/woowprd` 不變）。下文 `woowprd` 為當時名稱。

目標：把 in-repo 的 `/k8s-dev-sandbox` skill 搬進 `~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/`，讓任意 odoo addon repo（單一 src/、根即 addon、多 addon 子目錄三種樣態）都能開沙盒跑 e2e。2026-06-11 用 5-agent workflow + claude-code-guide 查證做了完整可行性評估。

結論：**可行、需重構、工作量 L**。無架構級障礙；工作量在「三者解耦」——① 工具自帶資產(chart/scripts) 跟 plugin 走、② 被測使用者 repo(src/.env/git) 跟 cwd 走、③ woow 平台 seed(operator/ECPay/WoowBot AI/journal) 剝離成 opt-in hook 預設不跑。現在三者全靠同一個 `PROJECT_ROOT`(`create.sh:35-36` 從 script 位置上推四層)綁死，搬進 plugin 後①②分裂、③對任意 addon 失敗。

**已查證的 Claude Code plugin 機制**（官方 code.claude.com/docs plugins-reference + skills，repo 裡查不到）：
- skill 觸發時 Bash 的 **cwd = 使用者專案根**（= `${CLAUDE_PROJECT_DIR}` = Claude Code 啟動目錄）→ `git rev-parse --show-toplevel` 可定位使用者 repo。
- 定位 plugin 自帶資產三重保險：`BASH_SOURCE` 相對推導(零 env 依賴最穩) > `${CLAUDE_SKILL_DIR}`(skill 專用) > `${CLAUDE_PLUGIN_ROOT}`(env var,harness 注入 Bash)。
- 命名空間 `/woowprd:k8s-dev-sandbox`；放進 `skills/` 自動發現、無需註冊 plugin.json。
- **硬限制**：plugin 不能引用 plugin root 外的檔案(`../shared` 被擋)→ chart 必須實體 bundle 進 plugin，不能 symlink 出去。

**雙根定位設計**：`PLUGIN_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)` 給 chart；`resolve_repo_root` 優先序 `--repo` → `${CLAUDE_PROJECT_DIR}` → `git rev-parse --show-toplevel`，**fail-loud 不 fallback 到 plugin 根**。

**三大難點解法**：
1. k8s context 顯式指定——現況已做對(`create.sh:46-152` 強制 `--context`+驗證+`cluster-info`)，直接搬零改。
2. 三樣態偵測順序 (b 根 manifest)→(a src/manifest，woow 現況)→(c 掃子目錄 manifest)命中即停；非 addon 過濾＝`__manifest__.py` + 名符 `^[a-z][a-z0-9_]*$` + 忽略 `{setup,docs,.git,__pycache__,node_modules}`。chart 已有半成品接縫 `codeMount.moduleName/hostPath/extraAddonsHostPath`(`odoo-deployment.yaml:138`)，升成 `codeMount.mounts[]`+`mountWholeDir`。
3. 相依 addons：local 走 `.env` 多路徑→多 hostPath(`extraMounts[]`)；ci-local/stg 維持 image bake；fail-loud 驗相依目錄含 ≥1 `__manifest__.py`(呼應 [[floor-env-relative-path-empty-ext-addons]] 空目錄騙過檢查坑)。

**硬限制**：local 模式用 hostPath(`odoo-deployment.yaml:166`)→ 只有 node=本機的 `docker-desktop` 能用，遠端 `woow-k3s` 看不到本機檔案必須走 image-baked。

下一步：① 30 分鐘變數實測(複製 scripts 進 plugin，woow repo 內呼叫確認雙根) ② 抽 `lib/common.sh`(雙根+detect_layout+fail-loud) ③ chart bundle+三模式參數化 ④ 平台 seed 剝離成 `.sandbox/post-create.sh` opt-in hook ⑤ 第三方 repo 端到端驗收。完整評估報告在本次 session workflow wl955fg4m 輸出。

**進度(2026-06-11)**：
- design+implementation 雙檔已寫好並進 **PR #178**(`docs/k8s-sandbox-plugin-port` 分支)：`docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-{design,implementation}.md`。implementation plan 含完整可落地 `lib/common.sh`(6 函式)、bash 單測、chart 11 個新 values key+range 掛載、7 支 script per-file 改點表、post-create hook 14 變數契約、三樣態驗收、6 phase。
- 順帶修了 chart `ttl-cronjob.yaml` 的 `bitnami/kubectl:latest`(受 [[bitnami-legacy-image-move]] 影響)→ values-driven `ttl.image` 預設 `bitnamilegacy/kubectl:1.33.4-debian-12-r0`，獨立 **PR #177**(`fix/ttl-cronjob-bitnami-kubectl`)。
- 落地精煉：`create.sh --set postgres.database="$DB_NAME"` 讓 DB 名單一真相、消 script↔`_helpers.tpl` 前綴 lock-step。
- 尚未動 plugin 實體檔(`~/.claude/plugins-local/woowprd/skills/`)，仍是純規劃；實作從 implementation plan Phase 0(變數實測)起。grounding 細節在 session workflow wmrfi6i1o 輸出。

**實作開工(2026-06-11，feat 分支)**：
- 分支：woow `feat/k8s-sandbox-plugin-port`(off develop)、plugin `feat/k8s-dev-sandbox`(off master)。
- **Phase 1 ✅** plugin `0aa1218`：`lib/common.sh`(雙根/三樣態/load_sandbox_yml)+`common_test.sh` 21/21 綠(bash 5.3)。後加 `export LABEL_DOMAIN`(統一 dev-sandbox.io、免 woow label override)。
- **Phase 2 ✅** woow `e575ae5`：chart 11 個新 values key + 去品牌(labelPrefix/dbPrefix/nsPrefix/secretName)+ codeMount 向後相容 range(mounts[]/extraMounts[] 空時 fallback legacy)。helm lint 0 + 6 情境 template 驗(含 TTL lock-step / woow override)。
- **Phase 3 ✅(7/8)** plugin `04d2309`：destroy/list/status/logs/extend/test/build 泛化(workflow wmugqjtdr 平行產出+複核)。leftover 字面全清、bash -n 全綠。test.sh DB 名改 compute_db_name(chart secret 無 component label)。
- **Phase 3 create.sh ✅** plugin `7a32bee`：雙根/compute_db_name+--set postgres.database 單一真相/codeMount 依 layout 注入 mounts[]/install gate+MODULES 用 SBX_*/剝離 woow stg+SQL seed。順序修為 seed→restart(ICP 快取重載)。smoke test：`create.sh --help` exit0、無 --context fail-loud(無叢集驗 source+resolve_plugin_root+require_kube_context 串通)。
- **Phase 4 ✅** woow `693ef62`：`.sandbox.yml`(layout/module/db_prefix=woow_/hook)+雙 hook 解 plan gap——`.sandbox/pre-install.sh`(install-time，agent-runtime secret 從 operator k8s secret 動態撈+stg NP/storageClass/JCR registry，寫 YAML 到 create.sh 給的 EXTRA_VALUES_FILE)+`.sandbox/post-create.sh`(post-install，operator 探索+ir.config_parameter/ECPay/AI/journal SQL)。pre-install YAML 經 helm template 消費驗證。
- **Phase 5 ✅** plugin `e46f566`(bundle chart 26 檔)+`ad7a83f`(SKILL.md)。plugin skill 結構完整：SKILL.md(name+description)+8 腳本+common.sh+bundled chart。/reload-plugins 後即 /woowprd:k8s-dev-sandbox。
- **docs Phase 4 雙 hook 文件 ✅** woow docs 分支 `4c13efb`(PR #178)。
- **Phase 6 三樣態 e2e ✅(docker-desktop 實證)**：(a)src 完整 create→module installed、(b)root detect_layout、(c)multi mountWholeDir→2 module installed；destroy.sh(helm uninstall+PVC+ns+PV reclaim)兩次。/woowprd:k8s-dev-sandbox 已被發現。e2e 找到並修 3 個真 bug：
  1. `/tmp` 非 Docker Desktop 共享路徑(hostPath FailedMount)→ 測試 addon 須放 /Users 下(環境 gotcha，非工具 bug)。
  2. chart addons_path 預設寫死未掛載的 `/mnt/extra-addons-ext` → 乾淨 addon 跑 odoo --dev reload 的 inotify watch 不存在目錄而 CrashLoopBackOff。修：addonsPath 預設只 /mnt/extra-addons，ext 由 configmap 在 legacy/extraMounts 實際有掛時才 append(woow `39f40ee`/plugin `a5a03c1`)。
  3. create.sh `import packaging` pip-wait 是 woow-specific；官方 odoo:18.0 對 bare python3 不暴露 packaging → 乾淨 addon 卡 900s。修：移除迴圈改靠 ODOO_READY(readiness probe 已把關 entrypoint pip)(plugin `ef47ec0`)。
- **follow-up(未修)**：layout(c) mountWholeDir 把整個 repo 掛進去，Odoo db.initialize 掃**所有**子目錄；malformed `__manifest__.py`(空檔/語法錯)會 ast.literal_eval crash 整個 init。真實 repo manifest 都合法故 happy-path OK；更穩做法是 layout(c) 改逐 addon 掛載(mounts[] 多項)而非 mountWholeDir。
- **PR**：#177(ttl fix)、#178(design+impl docs)、**#182(woow feat：chart 通用化+.sandbox hooks+addons_path fix→develop)**。plugin repo 無 remote=純本機，skill 在 plugin `feat/k8s-dev-sandbox` 分支(working tree 已啟用)。
- **deprecation ✅** woow feat `ea15071`(PR #182)：CLAUDE.md 段改推薦 /woowprd:k8s-dev-sandbox(in-repo scripts 收進 deprecated fallback details)、in-repo SKILL.md 加 DEPRECATED 標頭。**未刪 in-repo skill**——硬依賴：`deploy-stg.sh:232,354` 呼叫 in-repo create.sh、`tests/e2e/skill` 用 e2e-seed.sh、e2e-seed/seed-from-stg woow 專屬未進 plugin。使用者選安全方案(標 deprecated 保留)。
- **plugin feat→master ✅**(2026-06-11)：fast-forward 合併到 plugin `master`(`ef47ec0`)、feat 分支已刪。master 為 skill 權威分支(working tree 已啟用)。
- **layout(c) 逐 addon 掛載 ✅** plugin master `67bf36c`：common.sh export SBX_DETECTED_MODULES、create.sh layout=multi 改逐 addon 注入 mounts[i]、common_test 加斷言。docs(c) 更新 PR #178(`279818a`，rebase 過 develop merge)。docker-desktop e2e 實證：repo 含 malformed not-a-mod 零 crash、mod_one+mod_two installed、~60s。
- **三 PR 全併 develop ✅**(2026-06-11)：#178(design/impl docs)、#183(docs layout c 修正)、**#182(feat chart 通用化+.sandbox hooks)**。feat/docs 分支已刪(remote+local)。
- **pre-merge adversarial review ✅**(13 agents 5 維度，workflow wwjenv7jg)：併 #182 前抓到 **4 blocker + 1 major 同根因**——去品牌把 chart 預設改 generic(dbPrefix sandbox_ / labelPrefix dev-sandbox.io)，但 deploy-stg 仍依賴的 in-repo create.sh/extend/list/status 硬編 woow 又不傳 profile 給 helm = split-brain。修(commit `bd94f67`)：① create.sh HELM_INSTALL_ARGS 加 `--set postgres.database=$DB_NAME`(DB 名單一來源，消除 chart 預設改名即壞耦合) + `--set sandbox.labelPrefix=sandbox.woow.tw`(對齊 ttl-cronjob/extend/list/status 硬編 selector，否則 extend patch 錯 key→cron 照原 TTL 刪 ns **資料遺失**) ② pre-install.sh agent-runtime 改 **fail-closed**(auth/webhook 缺任一整段不寫含 namespaceSecret，避免半開 401 漂移)。helm template 三方驗證：feat+修正=`woow_stg`+`sandbox.woow.tw`、與 **develop 修前逐字相同**(透明還原，deploy-stg 零行為改變故免 stg 實測)；無修正=`sandbox_stg`+`dev-sandbox.io`(重現 bug)。**教訓：de-brand 改 chart 預設值=潛在 lock-step 斷裂，凡仍硬編舊值且不傳 profile 的 caller(deprecated in-repo 腳本)都要顯式釘樁。**
- **deferred chart-robustness minors**(unreachable by current callers，未修)：① configmap extraMounts+legacy extraAddonsHostPath 並存時 addons_path 含未掛載 ext→inotify crash ② mountWholeDir:true+2 mounts 渲染重複 mountPath(layout c 改逐 addon 後 create.sh 已不用 mountWholeDir，moot) ③ local codeMount 全空 render 空 hostPath。create.sh 三者都不觸發。
- **deploy-stg 遷移 plugin 已實作（PR #185，待 stg 實測）**(2026-06-12，worktree feat/deploy-stg-migrate-plugin)：deploy-stg 委派 plugin create.sh（resolver: WOOWPRD_PLUGIN_ROOT→plugins-local→cache 版本 glob）、加 --repo/--context/--yes、保留 init_odoo_system_params(web.base.url)+db_name dash 對齊；移除 in-repo 純重複腳本(create/destroy/list/status/logs/build，淨 −1922 行)；保留 e2e-seed/seed-from-stg/test/extend(woow-test.sh 用 extend)；in-repo SKILL 縮為 woow e2e/test 工具。**label 決策 D1=dev-sandbox.io**(plugin native，用 /woowprd: 管理；放棄 #182 的 sandbox.woow.tw 釘樁)。**plugin master `48b3779`**：create.sh 失敗清理加 install-only 守衛(upgrade 失敗不刪 ns/PVC 保資料)+--yes flag。本地驗證全過(bash -n/common_test/helm template woow_stg+dev-sandbox.io/resolver)。**對既有 dev-sandbox-stg：helm upgrade 就地修補、release(stg)/ns/DB(woow_stg)不變→資料安全，只 label sandbox.woow.tw→dev-sandbox.io**。**待**：woow-k3s stg 實測(live cluster 動作需使用者授權；auto-mode classifier 已擋我直接跑)→驗 DB/seed/label/agent-runtime/tunnel→過了併 develop。tests/e2e/skill 未受影響(仍走保留的 in-repo woow 工具)。
- **live stg 實測發現 split-brain（2026-06-12）**：woow-k3s `dev-sandbox-stg` 現處破壞狀態——odoo.conf `db_name=sandbox_stg`（woow_paas_platform=**uninstalled**）→ Odoo serving 它 → `/woow` 404；好資料在 `woow_stg`（woow installed、web.base.url 設）卻沒服務。helm `revision 23`(07:36) 用 chart 預設 dbPrefix(`sandbox_`)+dev-sandbox.io label+registry creds(woow-prod) 部署，**非我的遷移 deploy-stg**（我帶 --repo→.sandbox.yml woow_）。是某次 raw plugin create.sh 沒帶 --repo 之類造成（#182 review 的 B1/B3/B4 split-brain 真實重現）。**遷移碼本身正確**（helm template 驗 woow_stg+dev-sandbox.io；D1 label selector/TTL/pods/Odoo serving /web/login 200/兩 DB 皆完整 都驗過）。**hardening**：deploy-stg CREATE_ARGS 加 `--db-prefix woow_`(commit `8a8fc4b`)，即使 .sandbox.yml 沒讀到也保證 woow_stg、與 init_odoo_system_params 落點一致。**已修復（2026-06-12，使用者授權選項 2）**：`helm upgrade stg <plugin-chart 0.1.1==deployed> --reuse-values --set postgres.database=woow_stg`（dry-run 確認只改 db、不刪 registry secret）+ rollout restart → revision 24、odoo.conf db_name=woow_stg、odoo log `INFO woow_stg`+跑 woow cron、**`/woow` 404→303**（路由復原）、/web/login 200。split-brain 解除。
- **遷移驗證狀態**：db/label 核心邏輯經此 live 修復實證（woow_stg+dev-sandbox.io 都對）；deploy-stg.sh plumbing(resolver/args) 靜態驗（bash -n+resolver bash 實測）。**唯一未跑**：完整遷移 deploy-stg.sh 端到端（需 JCR creds 給 registry secret，使用者自跑）。stg 現在 woow_stg+dev-sandbox.io，下次跑遷移 deploy-stg(--db-prefix woow_) 會 helm upgrade 同 woow_stg、乾淨。
- **待做**：① （可選）使用者帶 JCR creds 跑一次遷移 deploy-stg 做端到端 gold-standard 驗證 → 併 PR #185（或憑現有 static+live 證據直接併）② ⚠️ 輪換 transcript 洩漏的 woow-prod JCR 密碼 ③ 上述 3 個 deferred chart minors。
- ⚠️ 2026-06-11 測試 pre-install.sh 時把 `woow-prod` JCR 密碼印進 transcript(我 cat temp 檔疏失，憑證未進任何 git)，已建議使用者輪換。
- 分支：woow `feat/k8s-sandbox-plugin-port`(Phase 2 chart `e575ae5`+Phase 4 `.sandbox` `693ef62`)、plugin `feat/k8s-dev-sandbox`(5 commits 0aa1218→ad7a83f)。grounding workflow：wmrfi6i1o(規格)+wmugqjtdr(7 腳本)。
