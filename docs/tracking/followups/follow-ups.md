# Follow-ups — PaaS refactor cycle 追蹤

> 本 doc 累積跨 refactor cycle 沒處理 / 刻意延後的事項。每條附 (severity, scope origin, 動手建議, 對誰處理)。Reporter 不解決，列冊讓未來 PR / issue 持續追蹤。

**State as of 2026-06-26**：
- **Smart Home 半年贈點 epic — PR #233 兩條 deferred follow-up 全 RESOLVED（2026-06-26，code + stg verify）✅**：PR #233（feat:Smart Home 半年贈點 eWallet 送點數，merged → develop，merge commit `f44198d`）建立時 body 列了兩條 deferred，已在同 epic 收尾全數解決——**① F-SHGP-1：sh_journal_id auto-config 潛在 prod 風險 RESOLVED**（commit `a7d0d2c`）：`company.sh_journal_id`（subscription invoice 的 sale journal）原由 post_init_hook **install-only** 設定、無 `-u` 路徑補設、fallback 僅 warning → 全新 onboard 環境若 sale journal 在本模組 install 之後才 ready，`sh_journal_id` 會永久空 → `_generate_invoice_vals` 回 `journal_id=False` → `account.move.create` 撞 NOT NULL → 被 `_process_subscription_billing` outer except **靜默吞掉**（不可觀測、餘額不動）。現役 stg/prod 已驗 journal 在、不受影響，但屬新環境真風險。修法按 PR body 建議**兩條都做**：`res.company._ensure_subscription_journal`（冪等、已設不覆寫）同掛 post_init（install）+ `data/subscription_journal_config.xml` 的 `<function>`（每次 `-u`，仿 `oauth_scope_updates.xml` install+upgrade 雙路徑）+ `_get_or_create_cycle_invoice` journal_id 仍空時 fallback 公司 sale journal + warn。TDD red→green（green 3/3 兩機制都觸發、red neuter 雙機制後 1 failed/1 error）+ `-u` 升級實證 `sh_journal_id` NULL→自動補上（docker-desktop standalone，`@tagged('post_install')` 避 at_install journal 時序假綠）。**② F-SHGP-2：停服段需真 operator RESOLVED（verify-before-merge，2026-06-26 stg）**：原 PR body 說「留 merge 後 deploy-stg 完整驗」，改提前在 stg 驗——`workflow_dispatch` 從 feat/first-sm 建 stg image（**注意：deploy-stg.sh 的 tag-push 流程預設烤 `develop`〔build-image.yml stg-* tag default addon_ref=develop〕，要指定 ref 須走 dispatch input + `--image` 部署**）→ `deploy-stg --image` 部署 dev-sandbox-stg（helm upgrade in-place、DB 保留）→ odoo shell 部署最小 PostgreSQL cloud_service（真 operator helm install StatefulSet 1/1）→ **per-sub/per-entity 直呼（刻意不跑全域 cron，免誤動 stg 其他 11 筆訂閱）**驅動完整鏈：override sub 可計費+failures=5 → `_process_subscription_billing(sub,6)` settle 失敗（wallet 0<99、`no_token` auto-topup 失敗）→ failures=6≥threshold → `state=cancel` → `_enforce_single` → `_do_suspend` → **`client.scale_release(replicas=0)` → operator 實際把 StatefulSet 縮到 0/0、pod 終止** → `cloud_service.state=suspended`。順帶在真 stg 實證 ① 的 fix（「auto_invoice created invoice=7 amount=99」無靜默失敗）。測完 `_do_cleanup` helm uninstall + 刪 service/sub/invoice + `kubectl delete ns paas-ws-stg-5a105e8b` 全清、零殘留。**「merge 後重驗 deploy-stg」已查證多餘**：develop 71 commit 對 `cloud_service.py` 的 +65 行全落在 cleanup/unlink 區（`_do_cleanup`/`_cleanup_helm`/`unlink`，行 ~1128 後），`_do_suspend`（992-1027）/`scale_release` 未動 → pre-merge 驗證等同覆蓋 merged develop（ORM cancel/dunning 狀態機另由既有 `TestT19SubscriptionFailureHandling` mock-settle 覆蓋）。**merge 衝突處理**：feat/first-sm 落後 develop 71 commit，唯一衝突 `src/hooks.py` post_init_hook（develop 加 HA redirect/device-grant hook、我把 inline journal 換成 `_ensure_subscription_journal()`）→ 解法保留兩個 HA hook + 用我的冪等 ensure（back-merge `fc9ad89` 後 mergeable）。memory：[[smart-home-half-year-gift-points]]。
- **naming-system env token L1-L3（PR #214 merged → develop，2026-06-24；已上 stg + **prod operator promote 2026-06-25**〔chart 0.1.16 / image 20260624.1022 / PAAS_ENV=prod，paas-cd PR #27〕，stg+prod 兩端 live 驗證）✅**：在 CF 命名注 env token，使 prod/stg 共用同一 woow-k3s cluster + Cloudflare account 的資源可辨識（設計 plan `docs/plans/2026-Q2/2026-06-21-naming-env-token-prod-stg-plan.md`；調查見 `docs/tracking/cf-{ns-prod-stg-audit-report,env-token-paas-env-feasibility}.md`）。**階段 1（L3，純 Odoo）完成**：`src/services/naming.py` 三個 `make_*_subdomain` 加 `env` 參數（`_env_token` slugify、格式 `paas-{env}-{type}-`、**prod/未設＝空 token byte-identical**、stg＝`paas-stg-sa-`）；`security_access.py:_generate_canonical_name` 讀 `woow_paas_platform.deploy_env` config_parameter（**只 SA 注 token** → tunnel name / DNS comment / policy name 隨 canonical 帶；CS/SM 對外 subdomain **不注**＝決策 DP2）；`woow_deploy_env` Selection（Settings）+ 卸載清理。**TDD**（純函式 harness RED→GREEN + sandbox 全套件 **1307/1307 綠、0 回歸**——prod 空 token 使既有硬編斷言不變紅）+ **5-lens 對抗式審查**（無 blocker；minor `_env_token` 非白名單值已 TDD 補 slugify）。**階段 2 in-repo（L1+L2 operator code）完成**：`config.py` `paas_env` Field + `field_validator`（白名單 fail-closed）、`helm.py` `build_managed_ns_labels()` 單一 source（create_namespace + `agent_runtimes._ensure_namespace` 兩注入點 lock-step、apply 前 `validate_label_value`）、`main.py` 啟動 log paas_env。**238 unit + 3 integration（real kubectl，validator↔kubectl 契約）綠**、4-lens 對抗式審查無 blocker（修 manifest-capture 測試強度 + 啟動 log fail-open 觀測）。**階段 2 cross-repo 已完成並上 stg**：paas-charts chart 0.1.16（`PAAS_ENV` env 孔 `| quote` fail-loud + render test，已 publish）、paas-cd Fleet（PR #26，scout 接手 paas-cd 卡死的 bump）chart pin 0.1.16 + operator image `20260624.1022` + `config.paasEnv: stg`（prod 未動）。operator-v20260624.1022 CI 656 success。**stg live 驗證全通**：operator `PAAS_ENV=stg`、新 ns 帶 `paas.woowtech.io/env=stg`、新 SA `canonical_name=paas-stg-sa-`。**F-ENV-1 RESOLVED（2026-06-24）**：原 gap「stg `deploy_env` 無自動設值 → 靜默產 prod 風格名」已修——`.sandbox.yml` 的 `config_params_stg:` 加 `woow_paas_platform.deploy_env: "stg"`（stg-only，local/ci-local 不污染），create.sh 部署 stg 沙盒時自動 set_param；operator 端 `PAAS_ENV` 走 Fleet（已 live）。**F-ENV-1b RESOLVED（2026-06-24）**：prod-release SOP 已補「兩端 env 一致」檢查——`docs/guides/deployment/prod-release-sop.md` §6.4（operator `PAAS_ENV` ⟺ Odoo `deploy_env` 必同為 `prod`，否則 split-env：ns label 與 CF 命名各說各話）+ §5.2.1 worked-example gate 加 `paasEnv: "prod"` 檢查 + 頂部 2026-06-24 更新 note。**F-ENV-2（L4 reconciler）/ F-ENV-3（marketplace `--create-namespace` bypass）已抽出為下方獨立 open 條目**（避免被本 ✅ 條目埋住；L1-L3 已 ship，兩者現為可動手項）。
- **F-ENV-2：naming epic L4 — 帳號級反向 reconciler（🚧 partial — TS-1~4 MVP ✅ merged 2026-06-26；剩 Phase D/E + 自動刪除 gated on DP，effort 餘 L，priority:medium）**：**首版範圍切分 TS-1+TS-2+TS-3+TS-4 全數完成並合併 develop**（dry-run 反向 net + 誤刪守衛標記層，**零刪除**）——TS-1 async 守線 #234 / TS-2 list-all+分頁+golden #235 / TS-3a operator inventory 端點 #236 / TS-3b Odoo 左集 #238 / TS-3c/d 差集 #239 / TS-4 設計修訂 #242 / TS-4 S1 operator `shared_tunnel_id` #244 / TS-4 S2-4 Odoo 分類層 #247。可 `kubectl exec` 打 `GET /api/reconcile/inventory` + Odoo `reconcile_orphans(env)` 看分類後孤兒清單。每片經對抗式設計批判 + 對抗式 code review 雙重守線。**剩餘 deferred（皆超出 dry-run MVP、不阻塞）**：① **operator image 尚未 build+deploy**（`extra/paas-operator/` 的 TS-2/3a/4-S1 改動需 `operator-v*` build → Fleet bump → stg→prod 兩拍才 live；端到端跑前的硬前置）② **Phase D**（cron + `/woow` 報表 UI；過渡期用 kubectl exec）③ **Phase E**（ns/PVC 反向 GC，動 operator + cross-repo cron lock-step）④ **DP-MARKER backfill**（開任何 CS `reconcilable=True` 的硬前置）⑤ **shared tunnel 硬編完整-UUID belt-and-suspenders cross-check**（§C.4 #1，需 live cluster 取真 UUID）⑥ **未來開 SA route DNS eligible kind 時**（DNS 進 inventory）須補左集 DNS 覆蓋 + 確認 cross-env 結構 gate 已就位（已在 `_is_reconcilable` 落地，option A）。⑦ **自動刪除**全程 gated on DP-MARKER + DP-RACE + DP-UNRECON。設計權威 `docs/plans/2026-Q2/2026-06-25-l4-reconciler-design.md` §C.4。以下為 L1-L3 ship 時的原始 context：L1-L3 已 ship（見上「naming-system env token L1-L3 ✅」條），辨識性已補，但「**發現既有孤兒**」仍缺，是孤兒治理的真解。需 operator 新增 list-all-tunnels / list-all-access-apps / list-all-access-policies（含 `per_page=500`/分頁——`per_page=100` 截斷漏孤兒教訓）+ 對 Odoo 現存 stored-id 取差集產 **dry-run 孤兒清單**；env 分流靠 `PAAS_ENV`（已 live）+ DNS content 指 shared tunnel（stg `55953bfa`/prod `328c3a28`）+ dual-format prefix（須同認 `paas-sa-` 與 `paas-stg-sa-` 兩格式，免把舊 record 當孤兒）。判 Access policy 孤兒以 CF app 實際引用為準、不信 DB 白名單。**完整設計步驟見專屬 design doc `docs/plans/2026-Q2/2026-06-25-l4-reconciler-design.md`**（2026-06-25 多 agent 撰寫+對抗式驗證，file:line 已對 live code 校正）：含 Phase A-E（A=operator list-all+reconcile router / B=Odoo 左集+差集+completeness gate / C=誤刪守衛 env-pin fail-closed / D=dry-run inventory+/woow 報表 / E=ns·PVC 反向 GC）、**五條誤刪鐵律 R1-R5**、**三類 un-reconcilable（dedicated tunnel / Access app / legacy 無 token）永久人工**、thin-slice MVP（TS-1~4 ~2.5 週交付 dry-run 反向發現、零刪除）、9 個 DP 待決點。**安全模型（design doc §安全模型）：首版完全零自動刪除（CI grep gate + `dry_run=False` 422）、發現與刪除刻意分離（刪除權留人手）、人工審在 `/woow` 後台報表頁+單筆即時重查；誤刪 prod 由 R3 env-pin fail-closed + prod-DB「可達≠有權威」+ R1 結構 allowlist + R5 protected tripwire 多層防，且這些守衛是開自動刪前的硬前置**。原始 §階段 3 摘要見 `docs/plans/2026-Q2/2026-06-21-naming-env-token-prod-stg-plan.md`。根因 pitfall [[cf-ns-orphan-no-account-gc]]。對處理：單獨立項排程（不依賴 L1-L3，token 只讓判 env 更乾淨）。
  - **對抗式審查補強（2026-06-25 understand workflow 4-reader→synth→critique 抓出，建 L4 設計前必納）**：
    1. **「不信 DB 白名單」要收窄**：只對 *Access policy 孤兒判定* 成立；**dedicated tunnel**（`paas-sm-*` / `paas-sa-*` 自建專用，本身無指向 shared tunnel 的 CNAME → DNS content 判不了 env）的 env 分類**反而要靠對照仍存在的 prod DB 取差集**（共用 account 限制）。一概「不信 DB」會錯失這條判定路徑。
    2. **刪前 GET 驗證要一般化**：in-use（409）guard 不只 reusable policy；DNS / tunnel / Access app 刪前都先 `GET` 驗 content / name / connections 再刪，否則 DNS/tunnel/app 的誤刪面未上鎖。
    3. **resync 漂移風險要一般化**：不只 `cf_policy_id`，任何被 `sync_access_app` resync 重建過的 stored-id（`dns_record_id` / `access_app_id`）差集前都要防「DB id 已變但資源合法 → 誤判孤兒」。
    4. **TOCTOU 重算**：刪除階段必須**即時重新 list-all + 重算差集**，不可拿陳舊 dry-run 清單刪（報表與刪除之間新建、命名落孤兒前綴範圍的合法資源會被誤殺）。
    5. **「誤認誤刪」激進失敗盲區**：前綴 filter 太寬 + 舊格式無 env marker + 無 DNS content 兜底時，舊 dedicated 資源會「判不了 env → 可能誤歸 stg 而刪了 prod」。原設計只談保守的「漏認→放過」，未鎖此激進路徑。
    6. **分頁須 cursor 迭代到底**：`per_page=500` 仍會在 zone >500 筆時截斷，只有 cursor 迭代到底才安全；CF pagination contract（`next_cursor` vs page offset）待查證。
    7. **mcp sidecar 雙命名孤兒**：CloudService mcp route deploy 用 `svc-*-{nssuffix}-mcp`、delete 用 `paas-cs-*-mcp`（命名不對稱 → 有 sidecar 的 service 刪除漏 mcp route），兩種命名都有歷史孤兒 → L4 盤 mcp **必須同認兩種前綴**。
    8. **DB↔CF 分歧殘留判定缺設計**：best-effort cleanup 只 catch `CloudflareException` → 暫態 httpx 錯逸出 → Odoo savepoint rollback → 半完成 CF 物件殘留；L4 反向對帳遇此如何判，尚無設計。
    9. **跨 repo lock-step 部署守線**：動 PVC/GC 那塊時，operator code 與 cross-repo `woow-paas-charts` cronjob template **必須 lock-step**（漏一邊重炸 fleet-split / 部署漂移）。
    10. **成熟度校正（勿把「修復中/已修」當「L4 全新」）**：PVC `orphaned-at` grace-period GC 機制**已存在但曾壞掉**（label 值含冒號過不了驗證、上線 ~74 天從沒生效），已由 operator #207/#208（epoch 秒 + `validate_label_value`）修復上 stg → L4 只需在反向掃描涵蓋殘餘 PVC/PV，非全新；**namespace 無 TTL** 才是 L4 真正待建。另：L1-L3「已 ship」精確語意為「**已上 stg 驗證、prod 待 promote**」，prod 仍空 token。
    11. **誤刪風險守衛（Q1 非-paas / Q2 跨-env，2026-06-25 over-deletion-risk workflow 抓出；建 L4 前必內建，否則不該開自動刪除）**：共用 CF account/zone（`woowtech.io`）上 list-all→set-diff→delete，**Q1**（zone-wide list-all 撈到官網 apex/MX/行銷非-paas 紀錄 → DB 查無 → 誤判孤兒）與 **Q2**（stg reconciler 靠「prod DB 查無＝孤兒」判 dedicated tunnel，prod DB 不可達 fail-open → 刪 prod tunnel）**naive 皆會誤刪**。鐵律「**寧可漏刪也不可誤刪**」落地五條：① **allowlist not denylist**（只刪能證明是自己建的，CF 資源建立時蓋 ownership 證據 + backfill；現行只有前綴、CF 無帳號級 marker）② **前綴 AND 內容雙證** ③ **env-pin fail-closed**（reconciler pin 自己 `PAAS_ENV`、env 判不出就不刪留人工、prod-DB-diff 先驗可達性）④ **dry-run + 刪前對 CF 與 Odoo 都重查**（防 TOCTOU）+ 分頁 cursor 到底 ⑤ **protected denylist**（apex/MX/官網/shared tunnel 本體硬編絕不可碰）。**最深結構洞**（對抗式校正）：正向雙證對 **dedicated tunnel**（CNAME 指自身、非 shared tunnel）與 **Access app**（無 metadata 欄位）**結構性失效**——而真實事故 3 條孤兒 tunnel 全是這類 → reconciler **連既有孤兒 backlog 都無法安全自動刪**，這三類（dedicated/Access/legacy）永遠只能 dry-run+人工。**前置止血（候選 F-ENV-4）**：`smart_home.py:202` 無條件 unlink（缺 `security_access.py:497-511` fail-loud guard）是事故 3 條孤兒 tunnel 的製造源，先修以止血。完整分析見 `docs/tracking/2026-06-25-fenv2-l4-reconciler-explainer.md` §9。
- **F-ENV-3：marketplace `--create-namespace` 繞過 ns env label（🚧 open，priority:medium，pre-existing 非 naming epic regression）**：marketplace 新租戶 ns 若走 helm `--create-namespace`（`releases.py:52-58` 直傳 `helm.install`、`helm.py:261` 對新 ns 加 flag 但**不帶 label**）→ 建出 BARE ns（無 `paas-managed`、無 `paas.woowtech.io/env`）→ 繞過 `build_managed_ns_labels`。**happy path 安全**（`cloud_service.py:442` 先建 labelled ns → `--create-namespace` 變 no-op），但「ns 不存在時直接 install」路徑會漏 label。對處理：`releases.install` 在 `--create-namespace` 後 reconcile 補 `build_managed_ns_labels`，或一律改走 `create_namespace()`、禁 marketplace 倚賴 `--create-namespace`。
- **F-ENV-4：SmartHome 無條件 unlink 止血（✅ RESOLVED 2026-06-25，code，TDD + 4-lens 對抗式審查）**：`smart_home.py:202` 的 `_do_cleanup`/`action_delete` 在 `delete_tunnel` 暫態失敗時**只 log warning、接著無條件 `self.unlink()`** → DB 列蒸發、CF tunnel 變成無 stored-id 的永久孤兒。**這正是 cf-ns-orphan 事故 3 條 dedicated tunnel 孤兒的製造源**（2026-06-25 over-deletion-risk workflow 對抗式校正點名）。對照 `security_access.py:497-511` 的 `unlink` 在 cleanup 失敗時 `raise UserError` 保留紀錄供 retry —— **SmartHome 缺這道對稱 fail-loud guard**。對處理：SmartHome `_do_cleanup`/`unlink` 補 fail-loud（tunnel 刪除失敗則 raise、保留 record 供 retry），與 SecurityAccess 對稱。**獨立於 L4（F-ENV-2）但是其前置**：不先止血，L4 reconciler 永遠追不上新生的「無 marker、無 stored-id、無 shared-tunnel 內容」un-reconcilable 孤兒（這類正是任何 ownership 守衛都通不過、只能人工的那批）。根因 pitfall [[cf-ns-orphan-no-account-gc]]；完整分析見 `docs/tracking/2026-06-25-fenv2-l4-reconciler-explainer.md` §9。**已修（2026-06-25，TDD RED→GREEN + 4-lens 對抗式審查 + 全套件 1309 綠零回歸，docker throwaway Odoo 跑測〔kind hostPath 不可用〕）**：`smart_home.py` 新增 `_cleanup_tunnel()`（刪成功清 `tunnel_id`、失敗保留）+ `unlink()` fail-loud override（鏡像 `security_access.py:494-519`：cleanup 後仍有 tunnel 則 `raise UserError` 保留 row+tunnel 供 retry）+ `action_delete` 委派 unlink；對抗式審查 **M1** 補 `_do_cleanup`（cron 走 `_do_cleanup` 非 `unlink`，鏡像 SA：刪失敗落 `state='error'` 非 `deleting` 假成功 → 下次 cron retry）。新增 4 test（unlink/action_delete fail-loud + happy-path 清空 + cron `_do_cleanup` error）共 20 TestSmartHome 綠。**M2 抽出為 F-ENV-5**（workspace cascade 繞過 unlink，SA+SmartHome 共通、不在止血範圍）。
- **F-ENV-5：workspace `ondelete='cascade'` 繞過 tunnel/helm cleanup（**✅ RESOLVED 2026-06-25 —— SA + SmartHome + CloudService 三型全修**，code）**：SmartHome（`smart_home.py:50`）與 SecurityAccess（`security_access.py:61`）的 `workspace_id` 皆 `ondelete='cascade'`。workspace 刪除時 Odoo 走 DB 層 FK DELETE、**繞過子 record 的 Python `unlink()` override** → 帶 live tunnel 的 SA/SmartHome 被 cascade 時 F-ENV-4 的 fail-loud guard 不生效、tunnel 直接變孤兒、無 retry 錨點（F-ENV-4 對抗式審查 M2 抓出；regression-lens 誤判「cascade 被 guard 卡住」其實不成立——cascade 根本不進 Python unlink，真正風險是**靜默漏 tunnel**）。**對兩個 model 都成立**，故須一致地修、勿只修一半。**SA+SmartHome 已修（2026-06-25，TDD RED→GREEN + 3-lens 對抗式審查 + 全套件 1315 綠、MCP invariant 仍綠）**：採**方案 B**（非 A —— 預審發現三個 `workspace_id` 皆 `required=True`，`set null` 違反 NOT NULL，A 被否決）。`workspace.py` 新增 `unlink()` override：`for workspace in self` 用 `sudo().search([('workspace_id','=',ws.id)]).unlink()` 對 SecurityAccess + SmartHome 顯式 ORM-unlink（讓 F-ENV-4 fail-loud guard 跑、cleanup 失敗則 raise 穿透 → 整筆 rollback），再 `super().unlink()`；保留 `ondelete='cascade'` 當繞過-override 路徑的 DB-integrity safety net。precedent = [[cloud-service-unlink-override-cleanup]]（DB cascade 繞過 Python unlink）。新增 `test_workspace_cascade_cleanup.py` 6 test（SA/SmartHome teardown、fail-loud rollback、happy、both-children、batch）。註：使用者面 workspace「刪除」是 `action_archive` 軟刪（`paas.py`），hard `unlink()` 僅 admin/ORM/migration/未來 hard-delete cron 觸發，本案為**縱深防禦/未來防呆**。
  - **CloudService ✅ RESOLVED（2026-06-25，TDD RED→GREEN + 3-lens 對抗式審查 + 全套件 1320 綠、MCP invariant + `_do_cleanup` helm test 仍綠）**：`cloud_service.py` 新增 `_cleanup_helm()`（冪等 helm uninstall：成功/404-已不存在/operator 未設定→True，configured operator 真失敗→False；不寫 state；失敗保留 `helm_release_name` 供 retry）+ `unlink()` for-loop 加 helm fail-loud guard（`if helm_release_name+namespace: if not _cleanup_helm(): raise UserError`，其後既有 sidecar-MCP cleanup 不變）；`workspace.unlink` 加 `service_ids.sudo().unlink()` route CloudService。**預審 C2 校正**：原以為「`_do_cleanup` helm 失敗非 fail-loud、需改」是錯的 —— `_do_cleanup` **早已** fail-loud-as-error（helm 失敗 → `state='error'` + return、不寫 `deleting`，被 `test_major2_do_cleanup_helm_fail_sets_error_state` 鎖），**不需改**；真 gap 只有 `unlink()` 缺 helm guard。controller 刪除路徑（`paas.py:2329`）自己先 uninstall 再 unlink = double-helm，靠 `uninstall_release` 對已刪 release 回 **404 → 冪等成功** 化解（controller 自己也 catch 404 佐證）。新增 5 CloudService/混合 test。**M-1 殘留（low）**：controller delete try/except 只接 `PaaSOperatorError`，unlink 的 helm-fail `UserError` 會傳播（正常路徑因 double-helm 404 不觸發；真觸發時 UserError = Odoo 標準 user-error + 正確 rollback，可接受）—— 可在 controller 補 `except UserError` 使 API error shape 一致（純一致性、非 bug）。**M-2 註**：MCP invariant test 的 MCP 存活是因它**非 `sidecar_backed`**（`_collect_sidecar_user_mcps` filter 排除）、非「project 存活」，已記於 `workspace.unlink` docstring。根因 pitfall [[cf-ns-orphan-no-account-gc]]。
- **F-ENV-6：prod Fleet `paas-operator-registry` secret drift（**✅ RESOLVED 2026-06-25 —— 根因更正 + 移除殘留 key**，ops/cross-repo paas-cd，prod-only）**：2026-06-25 operator prod promote（chart 0.1.16，paas-cd PR #27）觸發 Fleet reconcile，bundle `paas-app-bundle-paas-operator` 顯示 `0/1 Modified`——`paas-operator-registry` secret 的 `registry-server`/`registry-username` 被 Fleet desired 算成 `null`（patch `{"data":{"registry-server":null,"registry-username":null}}`）。**根因更正（經 paas-charts 複核 + live 證據）**：原記的「同名 `registry:` map 跨 `valuesFiles`+`valuesFrom` 拆 key、非 deep-merge → server 被覆蓋掉」是**誤診**（沿用 2026-06-21 CLAUDE.md 舊框架）——chart `secret-registry.yaml` **自 0.1.6 起就單 key 模型**（全史只一個 commit `be310d3`、0.1.8↔0.1.16 逐字相同，只渲染 `registry-password`），`registry.server`/`username` 一直走 **plaintext deployment env**（live `REGISTRY_SERVER=jcr-prod.woowtech.io` / `REGISTRY_USERNAME=woow-prod` 皆 env `value` 非 `secretKeyRef`）。那兩個 live secret key 是 **chart 外殘留**（帶 `kubectl last-applied-configuration` annotation = 手動 apply、pre-0.1.6 / 舊 Fleet-local-chart 時代）、**零 consumer**（全叢集只有 operator deploy 引用此 secret 的 `registry-password`）。Fleet desired 只含 `registry-password` → drift = **live 多了 desired 沒有的殘留 key**。**修法**：`kubectl -n paas-system patch secret paas-operator-registry --type=json -p='[{"op":"remove","path":"/data/registry-server"},{"op":"remove","path":"/data/registry-username"}]'`（收斂到 Fleet desired，**非**改 CD repo——git/chart desired 本來就對）→ bundle `ready:1`、GitRepo `5/5`、operator pod 0 restart、plaintext env 不受影響。**原判「force-sync → null 掉 imagePullSecret → prod image pull 斷」延遲炸彈不存在**（server/username 非 secret 來源）。教訓：判 Fleet drift 先看 `modifiedStatus.patch` 方向 + **查 chart 模板實際渲染哪些 key，別假設 secret 裡的 key 都是 chart 渲染的**。詳見 [[fleet-split-registry-values-merge-drift]]（已更正誤診）+ `prod-release-sop §10.1`；對應 [paas-app-bundle-cd#18](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/18)（已關閉）。
- **F-ENV-7：SmartHome dedicated tunnel 補 env token（Option C，DP2 部分推翻）（✅ RESOLVED 2026-06-25，code + **live stg 驗證通過**）**：重新討論 DP2「CS/SM 不注 token」發現 CS+SM 綁定**不對稱**——**CS** 走 shared tunnel（DNS `content` 指 stg `55953bfa`/prod `328c3a28` 已分 env）+ ns prefix + L2 label → 早已可辨識，注 token 冗餘且白改對外 URL（**CS 維持不注**）；**SM** 走 dedicated tunnel、**無** content 線索、**無** namespace（純 CF-to-home）→ 共用 CF account 上無法自證 env，是 problem-1 唯一真缺口（即 L4「dedicated tunnel env_hint=unknown」那筆）。**Option C（外科手術）**：env token 只注 SM **內部 CF tunnel 顯示名**（新 `_generate_tunnel_name` → `paas-stg-sm-…`），**對外 subdomain/hostname/tunnel_route 維持乾淨**（`_generate_subdomain` 不變）——完全比照 SA（token 騎內部 `canonical_name`、不騎 route 對外 `subdomain_prefix`）。**make-or-break 已驗**：operator `tunnels.py:54-88` 的 `name` 只當 CF tunnel 顯示名、DNS 從 `request.hostname` 抽（`:79-82`）不碰 `name` → tunnel 名與對外 hostname 可分離 → **純 Odoo 改動、零 operator 改動、無跨 repo lock-step**。**對稱安全**：SM teardown 走 stored `tunnel_id`、不重算名 → 改名零反查破壞（[[external-resource-env-token-naming]] 守則 3/4）。**實作（TDD RED→GREEN + 對抗式審查 8 lens + 全套件 1326 綠零回歸，docker throwaway 跑測）**：`smart_home.py` 加 `_naming_slug`（去重）+ `_generate_tunnel_name`（讀 `deploy_env` config）、`action_provision` 的 `create_tunnel(name=)` 改傳 token 名；`test_smart_home.py` 加 4 test（tunnel 名帶 token+subdomain 乾淨 / prod 預設零變更 / provision token-name 乾淨-hostname / prod regression lock），既有 DP2-lock `test_subdomain_has_no_env_token_even_when_deploy_env_stg` 續綠。決策記錄＋設計見 plan `docs/plans/2026-Q2/2026-06-21-naming-env-token-prod-stg-plan.md` §「DP2 部分推翻：SmartHome 走 Option C」。**Caveats**：① 既有 stg SM tunnel（改前建）名維持 `paas-sm-`，新建才帶 token → stg 永久混格（舊筆交 L4 認雙格式）；② **live stg 驗證通過（2026-06-25）**——deploy-stg 部署 `dev-sandbox-stg-smtoken`（image `paas-odoo-stg:20260625.0803` = gitea develop 含本改動、`deploy_env=stg` 由 F-ENV-1 自動設值、operator_url 指真 `paas-stg-system` operator），經 JSON-RPC 真建 SmartHome 並 `action_provision`（打真 operator → 建真 CF tunnel）：讀回 `tunnel_name=paas-stg-sm-115223fa-5c25b8a1`（**內部名帶 stg token** ✓）、`subdomain=paas-sm-115223fa-5c25b8a1` + `tunnel_route=https://paas-sm-…woowtech.io`（**對外乾淨無 token** ✓）、同 hash 尾段證同資源只差 token、state=active。驗畢 `action_delete` 經 F-ENV-4 unlink 拆 tunnel（record 刪除 ⟺ tunnel cleanup 成功 → 零孤兒），sandbox destroy。
- **docs 對抗式審查與更新（docs/audit-param-archive-cleanup，PR #205 merged → develop，2026-06-19）✅**：全 `docs/`（~245 檔）做對抗式審查 + 更新，4 個 multi-agent workflow（~190 agent；流程 = read-only 審查 → 每條發現派獨立 skeptic 重查 live repo 嘗試反駁 → 只動站得住的）。**對抗式審查駁回 9 條誤判（4 audit + 5 archive）+ Section 4 還原 6 處過度修改**。3 commit、全域相對連結壞鏈 0。**① 參數/過時對齊（29 檔）**：helm chart 已移出本 repo（`extra/paas-operator/helm/` 空、commit `651d9f8`）→ `./helm` 指令改標 Fleet GitOps（指向 `prod-release-sop §5.2`）；sandbox 生命週期指令移植 woowprd plugin（in-repo 只剩 test/extend/e2e-seed/seed-from-stg.sh）→ 改 `/woowprd:k8s-dev-sandbox` 或 kubectl；Python 3.10+→3.11+、JWT 效期 3600s→7 天（#155）、repo 名 `woow-paas-agent-runtime→paas-agent-runtime`、`ai_agent/ai_provider→ai_assistant`、`fetchAgents→fetchAssistants`、`workspace.access→workspace_access`、CI `.docs-check.yml→.lychee.toml`、pattern 計數硬編移除；code 對齊——webhook 已接線（operator `notify_state_update`）/ `use_agent_runtime` auto-enable / cloud-services DNS 實走 Cloudflare Tunnel（非 Traefik Ingress）/ API spec 日期。**② archive/去重（10 搬移）**：6 個已 shipped/promote 的 superpowers plan + 2 spec → `docs/archived/superpowers/`（自管理區 plans 16→12、specs 13→11；承重 seed spec 留原位）；meeting-goal-progress/ecpay-test 從 `guides/` → `tracking/test-reports/`（改名加日期，原為時間點報告）。**③ Section 4 doc-rot（16 檔）**：~107 處硬編原始碼行號刷新（逐一 grep live code 對映）/ ACL pattern doc 對齊現行 ir.rule M:N——domain 用複數 `cloud_service_ids`、controller mirror 維持單數 `cloud_service_id`（**對抗式審查擋下反向漂移**：ir_rules.xml 已 M:N，但 ai_assistant.py:130/133/135、paas.py:1542 仍走 deprecated 單數）/ `cloud-service-project-decouple` + `security-access-bare-tunnel-zero-route` 加 superseded·divergence 標註（只標註、不改寫決策）/ n8n 部署機制 threading→ir.cron + subdomain/namespace 新格式 / owl-test-helpers→Hoot、memory-slug wikilink→inline code。**3 條 follow-up F-DOC-1~3 列冊**：**F-DOC-1**（low/docs）Section 4 刻意延後 3 項——oauth2-architecture 等 ~11 檔缺 frontmatter（docs/README 定為非強制，宜批次）/ `decisions/agent-runtime-operator-chart-install-contract.md` 根層 loose 檔可重歸 `decisions/refactor/` 或 README 補述（非孤兒、被 follow-ups wikilink 引用）/ `reference/features` 5 個 spec orphan（orphan check Phase 2 未啟用，皆有效規格）；**F-DOC-2**（low/code）controller（`ai_assistant.py:130/133/135`、`paas.py:1542`）仍 walk deprecated 單數 `cloud_service_id`（= `cloud_service_ids[:1]`），ir.rule 已走複數 M:N——須確認「project 的 cloud_service 限單一 workspace」不變式成立，否則 controller 只查首個 service 的 workspace 與 ir.rule 查全部間有潛在 ACL 落差；**F-DOC-3**（low/docs）~107 處硬編行號為治標刷新、會隨 code 再漂移，未來可改「函式名 + grep 軟錨點」根治。
- **OAuth callback host-flexible redirect（feat/refine-callback-page，PR #203 merged → develop，2026-06-18）✅ stg E2E 全綠**：跨 repo feature（paas authorization server + HA component `feat/custom-oauth-callback-page`），把 client `woow-ha-smart-home` 的 redirect_uri 從固定 `my.home-assistant.io` 精確比對放寬成 host-flexible（路線一：callback 直接落使用者本機 HA、component 自家 view 渲染 WOOW 品牌頁，消除第三方 relay 頁、token 全程不離本機）。`oauth_client` 加 `redirect_uri_match_mode` Selection（`exact`/`ha_callback`）+ `_check_ha_callback_uri`（path 鎖 `/auth/external/woow/callback`、scheme 按 host 類別〔公網強制 https、loopback/私有/`.local` 允許 http，RFC 8252〕、拒 query/fragment/userinfo）+ `_is_local_host` + `ha_callback⇒require_pkce` constrains + healer（`noupdate=1` sticky → `<function>`+post_init 雙路徑翻正既有 DB、移除舊 relay；非 superset）。**controller 零改動**（token endpoint `:486` 是「與發 code 一致」比對非白名單，放寬只動 `check_redirect_uri` 一處即兩端生效）。跨 repo 常數 `HA_CALLBACK_PATH` 對齊 HA `const.py:WOOW_AUTH_CALLBACK_PATH`。**驗證**：45 tests 0 failed + URI 邏輯實測 32 case + 對抗式 security review（修 **HIGH-1**：`urlsplit` 解析前吃 `\t\r\n` 的 parser-confusion，進 `urlsplit` 前 fail-closed）+ Odoo 升級語意 review；stg image `20260618.1238` 部署 + smoke 實證 healer 翻正既有 stg DB（`ha_callback`+移除 relay、接受 `homeassistant.local`(http)/`*.ui.nabu.casa`(https)、拒舊 relay 與公網 http）+ **ha-paas-component 真實 HA E2E 全程通**（authorize 接受 localhost callback → paas 同意 → 方向 A 品牌頁 → PKCE `/oauth2/token` 換 token → 抓 workspace → entry 建立）。spec `docs/plans/2026-Q2/2026-06-18-oauth-callback-host-flexible-redirect.md`。**F-OAC-1 新增（prod lock-step，priority:high）**：prod 發版（paas `develop→main→v* tag`）必須與 HA component prod 發版 **lock-step** —— paas healer 在 prod `-u` 會移除 `my.home-assistant.io`，對 prod 是 **breaking change**，須 component 先具備新 callback、paas 才移除舊 relay，否則切換空窗「重新授權 / 新接入」使用者落空（`refresh_token` 不帶 redirect_uri、既有 token 續期不受影響，影響面限切換期重授權者）。stg E2E 已綠；**prod 時程待 Eugene 排**（依約本 cycle 不發 prod）。對處理：reporter / 未來 prod release PR。
- **agent 生命週期 D1–D6 deploy-stg e2e + 兩個 e2e-only 發現（2026-06-14）✅ 全部閉環**：真 cluster e2e 驗證 D1–D6（reaper 真 scale-to-0、GC 真 teardown、unlink fail-block）並抓到 **mock 測不到的兩個缺陷**：(1) **stg operator image 過舊** → D1 兩步驟 teardown 半套生效（只 helm uninstall、namespace+LLM secret 殘留）；修 `operator-v20260614.0841` build → Fleet bump（`paas-app-bundle-cd` PR #17）→ 重驗 namespace 真刪。是 [[agent-runtime-stg-enablement-gap-chain]] Gap #1 第二實例 —— **deploy-stg 只 build addon、operator 走 Fleet，凡動到 `extra/paas-operator/` 的 feature 須另 build+Fleet**。(2) **drift/webhook reaped 的 session 永久免疫 GC**（`reaped_since` 只在 reaper 路徑 stamp）；修 PR #200（`write()`/`create()` 集中強制 invariant + migration 18.0.1.0.40 backfill stg 6 筆），見 [[reaped-since-single-path-stamp-gc-immunity]]。完整模組 1264 測試綠 + 多 agent 對抗驗證。**剩餘**：#200 merged 後重 deploy-stg 驗 migration 實際 backfill 那 6 筆（B 選項，未執行）。
- **sandbox extra_addons `${VAR}` 展開 + create.sh install 硬化（2026-06-13，feat/sandbox-onboarding）✅ 收 onboarding follow-up #7**：補 local 模式第三方相依 gap——`woow_paas_platform` 的 6 個第三方 depends（`ai_base_gt` 等，不在本 repo）在 plugin local 沙盒從未被掛載 → `odoo -i` 在依賴解析靜默失敗（被 `create.sh` 管線吞成 warning）→ 「看似建好實際空」的壞 sandbox（[[floor-env-relative-path-empty-ext-addons]] 同源）。**兩處 lock-step**：①**plugin**（`woowprd-local`）— `create.sh` MODE 判定上移到 `load_sandbox_yml` 前；`common.sh` extra_addons 解析加 **mode-aware `${VAR}` 展開**（只 local 模式解析 + 展開 `${NAME}` 大括號形式，參數間接展開非 eval、無命令注入；引用變數缺值/未展開 → fail-loud `return 1`；ci-local/stg 直接略過——相依隨 image bake；`create.sh` 加 **install-state 硬化**：`odoo -i` 後查 `ir_module_module.state='installed'`，非 installed 紅字 `exit 1` 不在壞 DB 跑 seed）+ `common_test.sh` 補 3 案（展開成功/缺值 fail-loud/非 local 略過）②**repo** — `.sandbox.yml` 宣告 `extra_addons: ["${PAAS_CI_ADDONS_PATH}"]` + `env_optional` 加 `PAAS_CI_ADDONS_PATH`（值由 onboard 寫進 profile，每台機器路徑不同不進 git）。**驗證**：plugin 52 單測全綠（含新 3 案）+ 對真實 FLOOR/profile 整合 4 案（local 展開/stg 略過/local 缺值 fail-loud/onboard 無 MODE 略過）+ helm-template 證 `addons_path` append `/mnt/extra-addons-ext-0` + extraMount hostPath + **docker-desktop live build 黃金驗證**（`dev-sandbox-feat-sandbox-onboarding`：`ai_base_gt` 載入、`Verified: woow_paas_platform installed`、DB 直查 7 模組全 `installed`、`Sandbox created`）。**模式可用性**：local（修前❌→修後✅）、ci-local（✅ image bake）、stg（✅ image bake，不受影響）。**deploy-stg 真 stg 回歸不受此變更影響**（stg 走 image，create.sh 略過 extra_addons）。
- **Security Access P2.5 Per-route Access 重構（2026-06-10，feat/security-access-p2-access-per-route，PR 待 merge → develop）**：把 P2 的 Service-wide Access（整 SA 一個 CF Access app + 一套共用 policies/login/session）重構成 **Per-route**（每條 Application Route = 一個 Access app，各自 Protected toggle + policies + login methods + session）。**起因**：使用者 review stg 發現 Application tab 的 Protected 欄不該存在（保護應在 Access tab 逐 app 設）+ Primary badge / primary_hostname 殘留。**決策**：只做 Per-route 廢 Service-wide、login/session 全下放 per-route、先做 P2.5（P3 Domains 暫緩）。**5-phase ultracode**（~30 commits）：①model（access_policy→route_id、route 加 access 三件組、廢 SA 層 7 欄位、per-route sync、migration 18.0.1.0.37）②ACL（related-store security_access_id walk 驗證 create 時序無 B1 leak + cross-user 測試）③BFF（per-route policy/config + route_id 跨 SA pin + HttpCase）④前端（移 Protected 欄、Access tab per-route Protected Applications、headed 驗證 console 0 error）⑤全量 5-lens 對抗式審查。**operator 不變**（仍 per-app，Odoo 每 protected route 各打一次 sync → 不用重建 operator image）。Odoo **0 failed of 1215**。審查跨-phase 接縫抓到 **migration 漏 reconcile SA `access_enabled` master toggle** major（P2 access_enabled=False 的 is_protected route 在 P2 是 public，per-route 升級後會被 Phase-5 resync 誤 gate → migration step 0 清 is_protected 保 public）已修。**stg 遷移注意**（見下方 P2.5 §）：既有 test SA DB 搬 per-route + **手動 teardown 舊 SA 層單一 CF app**（per-route re-sync 重建 N 個 app）；prod 無 P2 Access 資料（operator 舊）→ migration prod no-op。前端 SA-level access BFF bridge 待 merge 後移除（dead path）。
- **Security Access P2 Access Control（Cloudflare Zero Trust）+ stg 發佈 + 真 Cloudflare 寫路徑 E2E（2026-06-08，PR #165 merged → develop）**：Access App / reusable Policy CRUD / Login Methods（Google + Email OTP）/ 逐 route is_protected / session duration。5-phase ultracode workflow（48 commits、Odoo 0 failed of 1107、operator 175）+ 5-lens loop-until-dry 對抗式審查（rounds 1-5，19 confirmed 全修）。**stg operator image-only Fleet bump**（`20260608.0307`，PR paas-app-bundle-cd#10）→ pod 起、Access endpoints live。**真 Cloudflare Access 寫路徑 E2E（operator 級，直接打 `PUT /api/access/sync`）**：CREATE app+policy+IdP 綁定 ✓ / idempotent re-sync ✓ — 但抓到 **operator delete 路徑 3 bug**（CF DELETE 回 202 被誤判失敗 + 孤兒 + in-use 刪序），**修復 PR #166 merged**（develop `38220bb`、image `20260608.0541`、Fleet bump PR paas-app-bundle-cd#11）→ **live 重驗 teardown 全綠**（CREATE/RE-SYNC/TEARDOWN 全 200、`orphaned=[]`、CF 零殘留）。知識持久化：[[operator-cf-access-delete-202-mock-contract]]（pitfall）。**token scope 前置**：stg token 加帳戶級 Access:Apps&Policies(Edit) + Access:Org/IdP(Read)（F-SA-3 RESOLVED）。**F-SA-3 / F-SA-4 新增**（見 §F-SA P2；F-SA-4=prod token 同前置，priority:high）。
- **Security Access P1 主線 + stg 一起發佈 + 真 Cloudflare E2E（2026-06-07，PR #159 merged → develop）**：訂閱/Tunnel/多 route 宣告式同步/503 凍結/後台/前端 6-tab/HA API 平行串接。6-phase ultracode workflow（73 commits、Odoo 1055 tests 0 failed、operator unit 154 + integration 7）+ 5-lens × 4 輪 loop-until-dry 對抗式審查（52 confirmed：1 critical provision 冪等孤兒 tunnel / 15 major / 36 minor 全修）。知識持久化 5 篇（3 pattern + 2 pitfall：`[[unittest-mock-dual-namespace-patch]]` / `[[su-env-exempt-guard-false-green]]` / `[[two-stage-remote-provision-idempotency]]` / `[[seed-noupdate-sticky-template-links]]` / `[[owl-template-crash-class-syntax]]`）。**stg 發佈鏈（4-agent canvas 協作：paas-platform / paas-app-bundle-cd / woow-paas-charts / deploy-stg）**：operator image `20260606.2308`（含 #159 SA + #158 TTL stopgap，image-only Fleet bump，chart 0.1.13 不動，PR paas-app-bundle-cd#9）→ addon deploy-stg（develop `c45ffdb`）→ dev-sandbox-stg → **真 Cloudflare E2E 5/6 PASS**（provision 真 tunnel + primary route DNS `paas-sa-…woowtech.io` dig 解析 CF edge ✓ / `PUT /routes` 宣告式同步 + DNS reconcile ✓ / availability 雙層 ✓ / 刪除 unlink 連動清 CF tunnel+DNS ✓；503 凍結依交辦 SKIP），advisory 2（token DNS scope）閉環、清理乾淨。**F-SA-1 + F-SA-2 新增**（見 §F-SA；皆 stg 環境 / 平台命名慣例層，超出 SA P1 範圍，gitea #162/#163，priority:high）。
- **F-SBX-5 已修（feat/skill-binding-ambiguity，2026-06-07，issue #156）**：skill 同名歧義 + 雙綁撞名修復，四層 lock-step —— ① `skill._compute_display_name` 後綴 version+scope（`test-echo v0.1.0 (System)`，`_rec_names_search=['name','version']`，後綴不進 wire）② `agent_template._check_no_duplicate_skill_name`（@api.constrains('skill_ids')，**不放行 superuser**，一條守全 3 寫入路徑：form 直寫/inverse/fork）③ `_build_skill_snapshot` dict-by-name dedup（user 蓋 system、與 bundle endpoint tie-break 同向、絕不 raise；`_effective_skill_ids` frozen 未動）④ 保守 migration `18.0.1.0.34`（(user,system) 配對自動刪 system rel、同 scope 同名 AMBIGUOUS 只 warning 留人工）。23 個新測試（display_name 7 / constraint 8 / dedup 5 / migration 3）+ 全套 1405 tests 0 fail 0 error（dev-sandbox 實跑，migration 另做 odoo-shell E2E 注入實證 + 冪等）。pitfall doc：[`skill-same-name-dual-bind-snapshot-collision`](../pitfalls/skill-same-name-dual-bind-snapshot-collision.md)（含存量鎖死預期行為 + 同 scope 人工清理 snippet + is_effective 雙 True 已知顯示行為）。**附帶發現（未修，tooling）**：`k8s-dev-sandbox/scripts/test.sh:110` 以 `${SLUG}-odoo-dev-sandbox` 組資源名，長 branch slug 被 helm 截斷（實際 `...-odoo-dev-sandb`）→ secret 查無 exit 2；workaround 直接 kubectl exec 正確資源名。
- **PR #153 skill binding UX stg 驗證（deploy-stg，2026-06-06）**：image `20260606.1010`（develop@`939b3a3`）部署 + PR #153 未結事項「pod 真載入 skill + 完整串流」**首次全鏈路驗通**（wizard user-scope 上傳 → copy-on-write fork 兩層綁定 → 同名 shadow system → snapshot push → pod ConfigMap reload → lazy fetch 200+SHA256 → atomic extract → frontmatter parse → subprocess exit 0 → tool_result/text_chunk/done）。E2E 逐層揭露 3 發現（皆非 PR #153 回歸）：**F-SBC-1 新增**（stg seed bundle 三重違規 + wizard 缺上傳結構驗證，issue **#154**）、**F-SKT-1 再確認**（31h pod **first-fetch** 也 401——不只 re-fetch，issue **#155**）、**F-ARL-4 新增**（LLM tool-call 一律空 kwargs，**paas-agent-runtime#23**）。驗證 artifacts 留 stg project 27 'PR153-skill-verify'（含合規 bundle 參考件 test-echo 0.1.2）。
- **Portal task agent chat（share read-mode 擴充，2026-06-05）**：原生 portal task/project 頁加 agent chat + status（spec / plan：`2026-06-05-portal-task-agent-chat-*`）。workflow 執行 T1-T7 TDD + 兩輪對抗式審查：ACL round 抓 **1 critical + 2 high**（portal leg 誤成 5 個 sudo-browse mutation endpoint 的唯一閘 → read-mode portal user 可封存/改名 project、注入 task；修 `_portal_write_denied()` fail-closed guard `2cece77`，含攻擊重現測試）；final round 再抓 `_create_project` 漏網 + chat/upload 零測試覆蓋（`47a1773`）；polish round 收 lockstep 精確化（collaborator leg 原生是 exact partner、非 child_of 家族）+ `project_stages` portal guard + 測試假綠強化（`63eec6b`）+ 前端 label 共用 / safeHtml 縱深 / ai_stream try-catch 等價（`7c307be`）。E2E（headed）抓到 `html_sanitize` 漏進 frontend bundle（`7edd749`）並重演本機版 enablement gap chain（[[floor-env-relative-path-empty-ext-addons]] + gap-chain pitfall 本機變體節；**F-GC-2 實證再現**）。全模組 902 tests 全綠。**F-PORTAL-1~3 新增**（見 §Portal task agent chat follow-ups）。
- Cycle 1（cloud-service ↔ project decouple, 2026-05-17）：F1-F11，B1 已 RESOLVED、PR APPROVED WITH CAVEATS
- Cycle 2（mcp cloud_service_id → project_id decouple, 2026-05-18）：F12-F20 新增，PR APPROVED WITH CAVEATS round 2，4 BLOCKER/MAJOR 全 close
- Cycle 3（Project Detail page, issue #48, 2026-05-20）：F21-F28 新增，APPROVED WITH CAVEATS，B-1 fresh-install fix verified；AC #28 reviewer E2E 完成 10/11 pass + 1 FAIL（E2E-G1，不擋 merge）；R-1 / R-2 hard caveats 收於 reporter phase（commit pending），R-3 / R-6 / E2E-G1 + 其他 polish 進 follow-up issue
- Cycle 4（MCP refactor URL redesign + unbound project ACL, issue #49 + #51, 2026-05-21）：**F22 + F23 RESOLVED**；F-X1~F-X8 新增；reviewer round 2 APPROVED WITH CAVEATS，3 hard caveat (HC-1/2/3) 全 closed in PR；1 new MINOR (SC-6b)；20 條 soft caveat (SC-1~SC-20) 分散 reporter inline / dedicated follow-up / batch 三類處理
- Cycle 5（Agent runtime pod Phase A — schema M:N + 4 new models + 16 rule + migration, 2026-05-22 → 2026-05-23）：CAQA round 1 BLOCKED（B-1：4 new model 無 ir.rule scope-restrict）→ impl round 2 補 16 rule + PA-5 Python gate + SK-6 path (a) field-level mask；CAQA round 2 APPROVED WITH CAVEATS；**B-1 closed + OQ-A-2 closed**；reviewer round 1 抓 4 HC + 28 SC（CAQA 漏抓 HC-1/HC-2/HC-3/HC-4）；leader 拍板 HC-3 + HC-4 hotfix in this PR，HC-1 + HC-2 標 reporter follow-up；impl round 3 補 `agent_template._check_project_ids_grantor_authority` + `skill._anchor_bundle_attachment` + `ir.attachment` row-level rule 兩條 + 8 test method；144/144 PASS + Full suite 664/0 fail；CAQA round 3 + reviewer round 2 APPROVED WITH CAVEATS；**HC-3 + HC-4 closed in PR；HC-1 + HC-2 + EC-3a + EC-4a → F-PA-13~16**；F-PA-1~F-PA-12 已於 round 2 reporter cycle 落地；18 invariants 守線（PR #47 + #54 共 11 條 + Phase A 7 條 + Phase A #8 新增）+ 2 new pattern doc（`constraint-input-trust-expansion` + `attachment-row-level-acl-and-anchor-stamping`）
- **agent-runtime-pod epic Phase B**（image source，**`paas-agent-runtime` repo**，2026-05-23）：14 production + 14 test + 11 commit + PR #1（CI green + merge 中）；CAQA Prism + reviewer Quorum 皆 **APPROVED WITH CAVEATS，0 BLOCKER**；HC-1（IP-literal SSRF）/ HC-2（ws_server claims authoritative — [[constraint-input-trust-expansion]] 的 image 端同型，pattern lens 兌現）/ subprocess rlimit real-trigger test / SC-7（role-scope tool cache）全 **round 2 內 resolved**；CI gate 三度價值（envelope 12→15 / cache path §3 vs §6 / checkout node-in-container）；carry-over SC-1~SC-9 + wiring assumptions 的 **canonical home = `paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md`**（cross-repo，不在本 doc 重列）；epic durable + pitfall 見本 repo `docs/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md` + `docs/tracking/pitfalls/ci-workflow-container-missing-node.md`
- **agent-runtime-pod epic Phase C**（epic wiring 收口：operator endpoints + skill-bundle + SSE bridge，2026-05-24）：`9479cb2..HEAD` = 12 commit / 29 檔 / +3980；3 impl commit（`6abb490` operator / `ec226b7` Odoo wiring / `d3aa60e` M.9 版號無關）+ C-1 hardening `7daf1f4`（+ red test `1305b77`）；CAQA **PASS WITH CAVEATS**（8 caveat + C-1 disputed + 6 info）+ reviewer **APPROVE WITH CAVEATS**（8 caveat 全同意 + 新 N-1~N-5；0 blocker）；**C-1 disputed severity（security-auditor 判 blocker vs CAQA 判 caveat）經 leader 裁決走 hardening `7daf1f4` MOOTED**（reviewer 獨立驗 fail-closed，既非接受 caveat 亦非 block，是修掉）；守線全過（HC-2 第三度 token-authoritative / HC-4 machine-auth 獨立路徑 / B1 gate 在 spawn 之前 / frozen contract 逐欄位 / resume race / dual-secret / migration idempotent）；cross-repo C-1 `token_use` **三端對齊**（operator 簽 + Odoo verify + pod WS verify `paas-agent-runtime` PR #2 `ba6b417`，**sign-first-then-verify zero-downtime**）；Phase-versioned test 第二次（M.9 `d3aa60e`，Cycle 1 retire test_ac2 是第一次）→ 抽 [[phase-versioned-test-assertion]]；OQ-R-1（design §5.6/§5.5/§2.15 `3cab339` 補 A-1/C-3/C-5）+ OQ-R-2（`ba6b417` merge）皆 addressed；**Phase D enablement gate must-fix（A-1/C-2/C-3/N-1/P-2/C-1-A 嚴格）見下方 §Phase D enablement gate**；epic durable 見 `docs/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md`
- **agent-runtime-pod epic Phase D**（enable-gate caveat hardening — must-fix close + admin 啟用面 + cross-repo wiring，2026-05-25）：`a9a4d1f..HEAD` = 4 commit（`5398f92` red / `8d152f7` fix F-PC-1~6+F-PA-14+charts / `e2f1624` feat enablement surface / `e94991d` test fixtures）/ 29 檔 / +1224-43；CAQA **PASS WITH CAVEATS**（0 blocker / 2 net-new caveat / 9 info / 3 carried-forward）+ reviewer **APPROVE**（5-agent 無方向性分歧，全 proceed to Reporter）；**must-fix 全正確關閉目標 caveat**（F-PC-1~6 + F-PA-14 在本 repo close；F-PC-7 A 嚴格 = `paas-agent-runtime` `feature/f-pc-7-token-use-strict`（`a8b3ce2` red + `dabb275` feat），report 以 cross-repo「given/盤證」處理，**merge 到 main + deploy-order 屬 flip-gate**；F-PC-3 設計 pivot auto-gen→**K8s Secret env(b)** fail-closed，CAQA 評為比 auto-gen 更佳）；**HC-2/F-PA-14 經 security-auditor 獨立確認 airtight**（延伸 Odoo framework-substrate 驗 `request.env.context` 不 merge request body → indirect injection 關閉）—— [[constraint-input-trust-expansion]] 第 4 度 instance（owner-bootstrap hint，/simplify 自抓收緊 normal-user 不讀 hint）；2 net-new caveat（D-CAVEAT-1 namespace secret 跨 3 進程同值 + charts `optional:true` partial 風險 / D-CAVEAT-2 token_use deploy-order + 1h 窗口 + operator rollback）收 plan §Pilot runbook flip-gate；R-1~R-5 reviewer 新發現無 high-severity（R-1/R-5 comment reporter 順手補、R-2/R-3/R-4 見下 follow-up）；OQ-D-1（design §2.15/§5.5/§5.6/§3.4 reconcile）+ OQ-D-2（C-4/C-6/P-1 刻意延後 confirm）+ OQ-D-3（flip-gate runbook）皆 addressed；**epic Phase A-D code-ready 完成**（flip / 部署 / 2 週觀察由使用者依 runbook 自跑）；takeaway durable：[[credential-claim-test-fixture-coupling]] pattern + [[cross-repo-artifact-landing-assumption]] pitfall + HC-2 forward-invariant 落 [[constraint-input-trust-expansion]]；epic durable 見 `docs/tracking/agent-runtime-pod-phase-d-enable-gate-2026-05-25.md`
- **agent-runtime-pod epic Pilot e2e**（Step 6 flip 實跑 → transport round-trip 閉環，2026-05-26）：使用者 task 63 送 chat → odoo SSE→WS bridge → pod `/v1/chat` → echo `text_chunk` → odoo `_persist_ai_response`（channel 16 OdooBot 回覆）→ 前端顯示，**transport round-trip 完整閉環**（ws_token 三方同值 HS256 auth 驗過 invoker_role=owner）。實跑串出 **9-gap chain**（見 [[agent-runtime-pilot-e2e-gap-chain]] pitfall），cluster 側由 paas-platform 連續修復破關：**PR #66**(operator ns-prefix 衍生+paas-managed label) / **#67**(operator projectId forward) / **#68**(odoo websockets dep) / **#69**(odoo↔pod WS envelope 契約 doc 交 image team) + 一串 ephemeral patch 撐到閉環；2 process learning 抽 pattern（[[spawn-install-values-missed-forward]] + [[plan-gap-stop-and-report]]）；skill-bundle 實際 fetch 待真 LLM agent 行使（echo 純 ack 不用 skills、連通已證 pod→odoo 200 + endpoint 存在 + NP 雙向，非 infra gap）；**durable 回收清單見下方 Index F-PILOT-1~10**（分 Tier 1 ephemeral 會被 Fleet/restart 洗 / Tier 2 系統性 chart-operator 缺口每 project 都撞 / plan-level 設計拍板）。**durable 收尾（2026-05-26）**：軌 2 直接轉正項 **F-PILOT-1/2/3/5/6/7 全 durable**——operator code `PR #66/#67/#71`（ns-prefix 衍生 / projectId / odooNamespace forward，3 missed-forward + contract test 鎖）merge develop、operator image `7b49f70` 建成；odoo websockets `PR #68` + image `20260526.1221` bake；charts `0.1.1`（chart-side default 修）；paas-cd pin 4 項（operator-stg image `7b49f70` + `AGENT_RUNTIME_CHART_VERSION=0.1.1` + `AGENT_RUNTIME_REPO_URL` external + odoo image `20260526.1221`）→ survive `helm upgrade --reuse-values` / Fleet sync。**剩 F-PILOT-4(registry provision) + F-PILOT-8(LLM cred) plan-gap 走 design 拍板，F-PILOT-3 ns-重建邊界 informational LOW（見下方詳述）**
- **agent-runtime-pod epic Phase E0**（pilot-revealed production gap — F-PILOT-4 registry pull / F-PILOT-8 LLM credential，**operator green**，2026-05-27）：`b46f0af..HEAD` = 4 commit / 3 檔 / +245（`76836b8` red → `e2a3d7b` T4.1a mock-fix → `3d7e083` operator green → `cdf5368` E0-R-1 lock）；CAQA **PASS WITH CAVEATS**（0 blocker / 3 caveat / 6 info / 1 out-of-scope）+ reviewer **APPROVE**（4 方無方向性分歧）；**scope = operator unit only**（`extra/paas-operator/`），cross-repo chart/cluster 僅 lock-step caveat。operator 兩條 provisioning（F-PILOT-4 registry pull secret forward — 複用 `helm.py:649`、imagePullSecrets 只帶 secret 名不帶 creds / F-PILOT-8 per-tenant `{release}-llm` Secret — keyless 早退）**正確且 scope 收斂**；**明文 LLM key 5 點獨立重驗不洩漏**（零 logger / 0600 tmpfile / generic handler / KubectlException message-only / Optional[str] 無 422 echo）；per-tenant 隔離 INV-2 成立。**E0-R-1**（key-absent 負分支 missing-negative，mutation 證實刪 guard 全 43 test 仍綠）tester `cdf5368` 補 `test_T81c` mutation-resistant 鎖 → [[credential-claim-test-fixture-coupling]] §missing-negative 形態。**T4.1a** 撞錯 test mock 對象（`ensure_registry_secret` 屬 KubernetesService 非 HelmService）→ 停下修 **test**（`e2a3d7b`）非 bend impl（會 AttributeError 炸 prod）→ [[plan-gap-stop-and-report]] §impl/test 層延伸。3 caveat 路由：**E0-CAVEAT-1**（orphan 明文 LLM Secret on teardown）→ **F-PILOT-10 HARD gate**（echo 階段邏輯不可能、真 LLM 上線前必收 + holistic teardown-lifecycle design）；**E0-CAVEAT-2**（chart `LLM_API_KEY` secretKeyRef optional:true）+ **E0-CAVEAT-3**（registry creds blast radius / SA-minimal RBAC + 建議 pull-only-scoped）→ cross-repo lock-step **owner paas-charts**；out-of-scope helm.py argv repo-read token（gap C `df6348e`）→ **F-GC-3**。報告 commit + 2 pattern doc 更新（credential-claim + plan-gap-stop-and-report）見下
- **skill-list cycle 6**（PR #81 follow-up — is_latest drift fix「model 層」+ 含瀏覽器 UI 的 e2e，2026-05-28）：`c36a9a1..HEAD` = 8 commits / 10 檔 / +859 -7（4 test commit + 1 fix + 1 feat migration + 1 docs test-coverage + 1 selector align）。CAQA **pass-with-medium-caveats**（high 0 / medium 2 / info 9）+ reviewer **approve with comments**（blocker 0 / major 7 / minor+nit+info 22；4-agent 無方向性分歧）。**根因修法落 model `create()` / `write()` hook**（[[invariant-recompute-on-orm-write-hook]] 抽 pattern — derived flag / 不變式不能只在 user-facing transition method 維護；ORM 任何寫入路徑（test fixture / 未來上傳 API / demo data / admin 匯入）都會 drift），配套幂等 migration `18.0.1.0.28` 對既有資料 backfill（無資料 no-op）。skill-related test **41/41 全綠**、playwright 4 階段 + 4 screenshot 視覺驗證符合 spec §④-b、不變式守線：transition method 既有 recompute 路徑（`action_publish` / `_deprecate` / `_archive_skill`）保留 belt-and-braces、`_recompute_is_latest` 內部 write `{'is_latest': ...}` 不踩 hook 條件 → 不遞迴。**tester 撞 fixture mechanics 雷**（HttpCase 內 `action_publish` 後接 raw SQL UPDATE 製造 drift sample 被 buffered ORM write flush 覆蓋 → `invalidate_recordset()` 只清 cache、不清 buffer）依 [[plan-gap-stop-and-report]] 守線停下回報 → impl 階段方案 A（`flush_all` 前後夾）落地，抽 [[httpcase-orm-flush-ordering-raw-sql]] pitfall。Phase C `MIG.1` 從硬編 `18.0.1.0.27` 改 monotonic `>= 27` —— [[phase-versioned-test-assertion]] 第三度落地（27→28 不撞紅，後續每次 bump 都不會撞）。2 caveat（C1 winner 演算雙寫雷 / C2 hook O(group_size) 單筆同步成本）+ 7 major（F1-F7 reviewer 抓，全非阻斷）+ 22 minor/nit/info 在 follow-up cycle 6 列冊；epic durable 收尾 + PR description / comment 草稿交 leader 用 tea CLI 操作。**Umbrella issue #84** 彙總 F-SK-1~9 search anchor（https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/84）。
- **agent-runtime-pod epic F-PILOT-10**（真 LLM agent — image 真 LLM runtime + client LLM cred + teardown design，**3 PR merged**，2026-05-27）：image **PR#6 → main `6945ea5`**（真 LLM runtime：langgraph_runtime / loader / cache / url_validator）+ client **PR#76 → develop**（`agent_runtime_client` 帶 LLM cred，收 F-PILOT-8 client-side 鏈漏）+ teardown **PR#77 → develop**（design §8.4 holistic teardown，E0-CAVEAT-1 HARD gate 的設計回應）；develop = **`be6746d`**。CAQA **PASS WITH CAVEATS**（0 unconditional blocker / 1 conditional-escalation C10-1 SSRF / 5 caveat / 4 info）+ reviewer **APPROVE**（pilot stg，0 mandatory blocker，3-agent + CAQA 無方向性分歧）。**4 核心控制驗到位**：tool sandbox 無 command injection（list-args + stdin JSON + sha256-pinned entrypoint）/ fail-closed（empty-bearer 拒 + key-absent→echo 不洩 env/cred）/ credential 兩端不入 log / agent-loop DoS bound（`MAX_TOOL_ROUNDS=8`）。**empty-bearer lock-step 做對**（[[credential-claim-test-fixture-coupling]] 正面範例，對比 E0 key-absent 缺口）；mock TDD 5 切片完。**三層 gate 見下方 §F-PILOT-10 review takeaway**：Tier1 真 cluster 部署前 **C10-1 NP backstop**（paas-charts woow-k3s 已實測 ✓）/ Tier2 真 LLM 對外前 **R10-1 observability + C10-3 byte cap + C10-2 interpreter allowlist** / Tier3 一般 follow-up（C10-4/C10-5/R10-2/R10-3/R10-4）。pattern：[[plan-gap-stop-and-report]] 再現（Gap 3 hybrid + C10-1 cross-repo NP gate）。
- **agent-runtime-pod epic F-PILOT-10 真 LLM round-trip 達成 🎉**（cluster e2e，2026-05-27，stg woow-k3s）：真 LLM 完整閉環（「你好8」12:58、**19s**、MiniMax-M2.7 真 LLM 生成、無 error、乾淨 disconnect、browser 真 LLM 回話且理解 history）。cluster + 完整 UI round-trip 串出 **8-gap chain**（mock/caqa/reviewer **全看不到**——render/deploy/真 NP/真 LLM/cold-start timing 在 mock 之下、部署狀態在靜態 review 之外，唯 cluster + 完整 round-trip 暴露）→ [[agent-runtime-fpilot10-cluster-e2e-gap-chain]] pitfall。真因聚類：**missed-forward**（G2/G6/G7，**optional value silent 漏轉** — 比 pilot required 硬 fail 更陰，contract test 須涵蓋 optional）+ **deploy gap**（G1 operator 自身 image stale ≠ pin agent-runtime image / G8 operator chart 無 extraEnv / G5 externalEgress 沒 enable）+ **transport timing**（G3 cold-start race relay 無 retry）+ **observability**（G4 真 LLM 失敗 silent close 無 error frame）。**3 道防線**（integration test render-to-pod-spec 涵蓋 optional + ensure_registry_secret real-K8s / deploy checklist 驗 running image 非 values pin / observability success-path log）+ 8-gap durable 見下方 §F-PILOT-10 cluster e2e gaps；pattern [[spawn-install-values-missed-forward]] 加 optional 深化。**[最終收尾 2026-05-27]** G8 externalEgress env ephemeral→durable（operator chart `0.1.11` + PR#79 + values-stg + survive redeploy）= F-PILOT-10 **唯一 ephemeral caveat 已解** → **cd 側全部署 durable**；**G1/G2/G5/G6/G7/G8 全 CLOSED**，剩 G3/G4 後續改善 + R10-1/C10-*/R10-*/F-PILOT-11 gate/test follow-up（後續 phase）。**F-PILOT-10 真 LLM epic 完整收口**（code + review + round-trip + 8-gap durable + 沉澱完整）。
- **Cycle 6（decouple project frontend + controller 收尾, 2026-05-28）**：plan A+A+A signed-off（D1=A 允許完全 unbound project / D2=A controller create 後 `message_subscribe` 補 follower / D3=A modal 無 workspace 隱藏 cloud_service dropdown）→ phase agent pod 接力（tester TDD red T1-T4 → implementer 7 改動點 → CAQA round 1 BLOCKED BLOCKER A1 `_list_tasks` sudo bypass `cross-workspace-task-data-leak` family 第 2 instance → tester 補 T4 + impl `1 行` drop sudo → CAQA round 2 re-verdict APPROVED WITH CAVEATS → reviewer 4-agent verdict 無方向性分歧 APPROVE WITH CAVEATS → reporter 沉澱 → **user 拍板帶 F-DP-2 進本 PR** → tester 補 T5 + impl drop sudo + Task.sudo unify → CAQA mini re-verdict APPROVED WITH CAVEATS 不變 → **reviewer 略過**（per leader+user 拍板）→ reporter final addendum）。**累計 8 改動點 + 1 unify refactor + 15 條 test/TC 全綠**：controller `ai_assistant.py:1037-1040`（D1）+ `:1186-1190`（TC4 follow-on `_list_projects` drop sudo, 變體 A）+ `:1230-1238`（D2 message_subscribe）+ `:1325-1329`（BLOCKER A1 fix `_list_tasks` drop sudo, 變體 A）+ `:972-978`（F-DP-2 fix `api_support_stats` drop sudo + empty domain, 變體 B under-access）+ `:980-986`（Task.sudo unify, 變體 C same-method 半套狀態 close）+ frontend `CreateProjectModal.js:117-160` 三分支 dispatch + `CreateProjectModal.xml:40-63` workspace optional + dropdown + `support_service.js:267-292` `createUnboundProject`；15 條 test/TC 全綠（T1-T5 endpoint + B1-B5 backend baseline + Playwright TC1-TC5 UI）。**B1 family（`cross-workspace-task-data-leak`）controller layer 收口 progress**：5 instance 4 closed = `_list_projects` (A) + `_list_tasks` (A) + `api_support_stats` accessible_projects (B) + Task.sudo unify (C) close in PR；剩 `_get_project_stages`（CAVEAT #2，F-DP-1）= P1 follow-up（需 design）。**3 caveats 維持 follow-up**：F-DP-1（_get_project_stages design）+ F-DP-3（_check_project_access docstring）+ F-DP-4（frontend rule of three）；F-DP-2 CLOSED in PR；F-DP-5~F-DP-8 reviewer polish 維持 trivial follow-up。Pattern doc `[[orm-vs-endpoint-acl-test-coverage]]` 升級為 **5 instance critical mass + anti-pattern 三變體 family (A over-access / B under-access / C same-method 半套)** + perf justification 章節；新 pattern `[[phase-leader-brief-as-pointer-not-source]]`；3 新 pitfall `[[decouple-project-frontend-worktree-dev-pitfalls]]` + `[[orm-test-green-endpoint-leak-blindness]]`（含三變體 narrative）。Reporter sentencing 完成 + CLAUDE.md addendum landed（§Known patterns 補 3 pointer）；commit + PR 由 leader 跟 user 對焦後執行。
- **deploy-tooling fix #85 + #86**（dev sandbox create/destroy/deploy-stg 腳本，2026-05-29）：create.sh init gate 改查 `woow_paas_platform.state='installed'`（**#85A** 半 init 自癒，取代「ir_module_module 表存在即 skip」）+ destroy.sh 預設砍 PVC + reclaim 綁定 PV（`--keep-data` 退場）+ **`--context` 必填守門**（MAJOR：新增破壞性 cluster-scoped `delete pv` 需防 cross-cluster 誤刪，mirror create.sh `a62b75b`）+ deploy-stg.sh `kubectl exec` 補 `-i`（**#86** here-doc web.base.url SQL 投遞）。對抗式 workflow（6 agents：#86 雙 lens **pass** / #85 雙 lens **concern → MAJOR context 守門 + PV race + stderr 遮蔽 由 leader 補修**）+ docker-desktop pre-flight 全綠（gate 四情境邏輯 / 守門 rc=1 / 自建 Retain PV 完整 destroy 機制：預設 reclaim + `--keep-data` 保留）+ **真實 woow-k3s e2e**（`dev-sandbox-stgfix85` 拋棄式，未碰現有 stg）：gate 攔到 stg image 出廠 DB `state='uninstalled'` → 裝 woow_paas_platform → `/woow` **303 非 404**；`INSERT 0 2` web.base.url+freeze 落地；destroy 揭露 filestore PVC `helm.sh/resource-policy:keep`（確切洩漏源）→ 清掉 postgres+filestore 2 PVC + 2 longhorn PV **零殘留**。docs（SKILL.md + CLAUDE.md）同步 destroy `--context` 必填 + `--keep-data`。3 follow-up **F-DT-1~3** 列冊（皆 pre-existing / reviewer nit，超出 #85/#86 scope）。
- **PR #96 stg-deploy 驗證 follow-up**（skill dual-scope crash + scope-flip write guard + upload wizard 上 stg 後的對抗式複查，2026-05-30）：develop `e57d20a` → stg image `20260530.1424` 部署健康（nginx/odoo/postgres 全 Running；login / `/woow` dashboard / skill list / upload wizard / scope-flip guard code-in-image 全 pass；後端零 500 / 零 AttributeError）。**對抗式 workflow（3 agents：mcp_server 可利用性 + `__init__.py` 修法 + skeptic）複查出 2 條 follow-up**：**F-SDS-1（HIGH/security）= sibling model `mcp_server` 缺 PR #96 在 `skill.py:150-167` 補的 scope-flip `write()` guard，raw ORM 可觸發跨租戶提權**（skeptic `refuted:false` 高信心；endpoint 因 writable-field allowlist `paas.py:1393` 安全）；**F-SDS-2（low/cleanup）= `src/__init__.py:4` 無條件 `from . import tests`，production 每次啟動載入 test framework**（finding #1，stg 報告抓到的非阻斷 ERROR）。兩條皆**非阻斷 stg**；driver 拍板「只記冊不動 code」+ 還原 verification agent 自作主張的 `__init__.py` 刪行。**彙總 issue #98**（https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/98）。pattern lens：[[scope-flip-write-guard-not-ir-rule]] 的 sibling 兌現（mcp_server 是 skill 修補的缺漏雙生）。
- **issue #98 fix（fix/issue-98 → PR #99 merged into develop `800cc1c`，2026-05-31）✅ F-SDS-1 + F-SDS-2 全 RESOLVED**（PR：https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/99）：TDD 紅綠（RED 4 安全測試 `AccessError not raised` → 加 `mcp_server.write()` + `mcp_tool.write()` scope-flip / re-point guard → GREEN）；`test_mcp_scope_flip_guard.py` 13 test（ORM 負向 + 單欄位變體 + no-op 放行 + admin/sudo boundary + mcp_tool re-point〔system + 跨租戶 user 注入，對抗式自審加嚴〕+ HttpCase endpoint allowlist 對等）；**完整套件 782 test 收集、零 mcp/skill 回歸**（11 失敗皆 pre-existing 環境性）；完整性 grep 確認 skill+mcp_server 為僅有 2 個 scope-flip sibling 皆已 guard；F-SDS-2 刪 `src/__init__.py:4` 經 sandbox 實測 test 仍全收集。驗證環境：docker-desktop k8s-dev-sandbox `dev-sandbox-fix-issue-98`。
- **Skill upload wizard 後端空狀態無上傳入口（manual stg 探索，2026-05-31）→ F-SKU-1 + issue #102**：後端 Skills list view（`action_skill` / `/odoo/action-542`）的 skill 上傳走 `action_skill_upload_wizard`（`binding_view_types="list,form"` contextual action），Odoo 18 只把它掛進「**選取記錄後**」才出現的 ⚙ Actions 選單；list view 又 `create="false"`（`skill_views.xml:8`）無 New 鈕 → **列表為空時沒有任何上傳第一個 skill 的入口**（只能靠 agent-runtime API `/api/agent-runtimes/skill-bundle` 灌第一筆），但 nocontent help 寫「No skills yet」給人可上傳錯覺。stg 實測（playwright-cli）確認：breadcrumb 齒輪選單僅「Export All」、勾選 hello-world 後 Actions 選單才有「Upload Skill」。**建議修法**：仿 MCP Servers list view 的固定「New」按鈕，在 Skills list view 放一顆**不依賴選取**的「Upload Skill」按鈕（開 wizard action），救空狀態。**彙總 issue #102**（https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/102）。
- **平台預設 agent template seed（feat/seed-agent-tpl，2026-06-01）✅**：補 #112 刻意延後的「新環境開箱即用 / `ai.config` seed 化」——新增 seed `agent_template_default`（public+published+unbound，ref `ai_config_woowbot`）+ `post_init` `_ensure_default_agent_template`（未設才指、不覆寫 admin 已選）+ S-1~S-5 測試（version `18.0.1.0.30`）。同時收斂 stg-enablement gap-chain gap #3「逐 project 手動綁 template」痛點。sandbox A/B 驗證：新測試全綠、12 個既有失敗（payment_provider / base.user_demo / base.TWD 環境缺漏）與本變更無關。spec：`docs/superpowers/specs/2026-06-01-seed-agent-template-design.md`。
- **agent_session state-sync webhook（§5.5 迴路閉合，feat/agent-runtime-state-sync-webhook，2026-06-01）**：修「`agent_session.state` 永遠卡 `absent`」根因——operator 的 `webhook_dispatcher.dispatch_state_update` 從沒被 production 呼叫 + 兩端 `AGENT_RUNTIME_WEBHOOK_SECRET` 未注入 + drift cron 排除 `absent`。**Tasks 1-5 in-repo（TDD，operator unit 138 綠）**：operator 新增 `notify_state_update`（best-effort / no-op-without-secret / never-raises）+ 經 `BackgroundTasks` 從 `spawn_or_attach`（每回合）+ `reap` 發出 webhook；Odoo 端 `create.sh` 注入 `agentRuntime.webhookSecret`（chart `secrets.yaml` / `odoo-deployment.yaml` 早已接好，helm template 驗 value 流通）+ deploy-stg / SKILL.md 警告。**對抗式 4 維 workflow 審查**（21 agents）：17 findings / 9 confirmed / **0 blocker/high**；in-PR 修 2 條——**YAML-quoting bug**（`create.sh` 把 secret 用雙引號 scalar 輸出沒 escape `"`/`\`，registry 密碼最易中 → 改 `yaml_sq` 單引號 + `''` doubling，nasty-value round-trip 驗過）+ **notify-on-every-attach 加註解**（標記為刻意的自癒機制，防未來被「優化」成 transition-only 重引 bug）。**cross-repo 對外部署（Tasks 6-8）待執行**：woow-paas-charts `paas-operator` chart 加 `AGENT_RUNTIME_WEBHOOK_SECRET` env + 升版發佈 → paas-app-bundle-cd Fleet bump + operator secret 加 `webhook-current` key（kubectl，持久）→ build `operator-v*` → 重部署 stg → e2e 驗 `absent→active`。**6 條延後 F-ASW-1~6**：①(low) standalone `/resume` 不發 reaped→active notify（死路徑，無 prod caller）②(low) notify 最壞 ~33s 背景 hang（flaky Odoo；defense-in-depth：'active' notify 單次嘗試 / 縮 timeout）③(low) `create.sh` per-block EXIT trap 重設脆弱（整併成 `_TMP_FILES` 陣列 + 單一 trap）④(medium) RACE：cold-start webhook 404（session row 尚未 commit）— 源頭修：operator 呼叫前先 commit session；現靠 notify-on-every-attach 下一回合自癒 ⑤(medium) drift cron 排除 `absent` → 救不回 race'd cold-start row — 改納入近期建立的 `absent`（bounded，避免輪詢從沒 spawn 的 project）；與④耦合 ⑥(low) cold-start 送 `spawning` 早於 pod Ready、無專屬 spawning→active transition（靠 cron / 下次 attach）— readiness-gated 'active' webhook 或註解。plan：`docs/superpowers/plans/2026-06-01-agent-runtime-state-sync-webhook.md`；架構說明：`docs/guides/development/agent-runtime-chat-architecture.md`。
- **agent-runtime LLM secret provision-on-attach fix（fix/agent-runtime-llm-secret-provision-on-attach → PR #122，2026-06-02）**：修「task chat AI 永久回 echo『收到你的訊息…round-trip 已接通』」根因——operator `_provision_llm_secret` **只在 cold-start 跑**（`agent_runtimes.py:242`，在 `if not _release_exists()` 內）；attach/resume 既不 re-provision Secret 也不重啟 pod，所以「pod 已存在後才在 ai.config 設/改 LLM key」永遠接不上 → pod 永久 keyless → `_echo_stream`。**DB 設定全對也會中**（key 來源只認 resolved `agent_template.model_config_id.api_key`，無 `default_ai_config_id`/env fallback）。fix：`spawn_or_attach` attach/resume 改以 `agent.woow.tw/llm-checksum` deployment annotation 偵測 credential 變動，變動才 `_provision_llm_secret`（idempotent）+ attach `rollout_restart_deployment`（unchanged 完全 no-op 不 churn）+ cold-start stamp checksum；修前 spawn 的 keyless pod 無 annotation → 下次 chat 自癒一次；新增 `KubernetesService.get_deployment_annotation`。operator unit **141 綠**（+3 new test、6 既有 attach/resume 補 checksum no-op）。**stg live 驗證端到端通**：teardown 強制 cold-start（立即解；stg operator `20260601.1502` 尚無此 fix）→ pod 取得 `LLM_API_KEY`+MiniMax config → 真 LLM 回覆（MiniMax-M4.7 自我介紹）。pitfall `docs/tracking/pitfalls/agent-runtime-llm-key-after-coldstart-stuck-echo.md`；記憶 `reference_agent_runtime_llm_key_after_coldstart`。**PR #122 待 review + operator rebuild/redeploy 才在 stg 生效**（在那之前換 key 仍需手動 teardown）。**2 條 follow-up F-ARL-1/2**（見下摘要 + gitea #123/#124）。
- **agent-runtime cold-start retry（#123）+ skill/mcp on-publish push（#111）（feat/reine-code-start，2026-06-02）✅**：純 Odoo 端。**Phase 0 refactor**：snapshot/resolve（`_platform_default_agent_template` / `_resolve_agent_template` / `_build_mcp_snapshot` / `_build_skill_snapshot`）從 controller `ai_assistant.py` **下沉到 `agent_session` model**（chat 與 on-publish 共用單一來源），controller 改 thin delegate（行為等價）。**#123 cold-start retry（F-ARL-1 RESOLVED）**：client `_request` per-call timeout 分級（spawn 90s / invalidate 10s / 其餘 default 30s）+ `spawn_or_attach` bounded retry（idempotent → transient timeout/connection 重試、4xx 不 retry）+ controller `relay()` WS connect bounded retry（backoff、deadline 60s；retry 只包 connect 不重送 user_message，保留 `__aenter__/__aexit__` F-PC-6 清理）+ 推 `status:spawning` 進度 event + 前端 `AiChat.js` 加 `spawning` connectionState（黃「AI 啟動中…」非紅錯誤）。**#111 on-publish push**：client `invalidate_skills` + `agent_session._push_snapshot_to_running_pods(kind, scope, project_id)`（best-effort：列舉 `state='active'` session，system=全部 / user=單一；逐 pod try/except warning continue，單 pod 失敗不中斷 publish）；`skill.write`(state 變動) 觸發 skill 推送、`mcp_server.write`(name/url/transport 變動) 觸發 mcp 推送（mcp_tool 不改 snapshot 故不觸發）。plan `docs/superpowers/plans/2026-06-02-agent-runtime-coldstart-onpublish.md`；架構說明 `docs/guides/development/agent-runtime-chat-architecture.md`；pitfall G3（`agent-runtime-fpilot10-cluster-e2e-gap-chain.md`）OPEN→CLOSED。**驗證（docker-desktop k8s-dev-sandbox `dev-sandbox-feat-reine-code-start`，2 輪）**：19 新測試全綠、**0 自造回歸**——首跑抓到 2 個下沉 refactor 回歸〔controller delegate 用 `request.env` 在無 HTTP context（既有測試直接 `Controller()._build_*()`）炸 `object is not bound` + `_resolve_agent_template` 回傳 `None`→empty recordset 破既有 `assertIsNone` 契約〕→ 修 `56d7d5f`（delegate 改 `project.env` + `_resolve` 回 `template or None`）→ 重跑確認 `22→14`，正好少修的 8 個。**殘留 14 個與本 PR 零交集**（diff 完全不碰 `ai_client` legacy / ecpay / oauth2 / smart_home / router / `_agent_runtime_autoenable_ready` 判定；重跑殘留紋絲不動）＝ sandbox 環境性 pre-existing（與歷史 11-12 個一致）→ **F-ARC-1**。

- **agent-runtime MiniMax tool-calling fix（#129，2026-06-02）**：跨 3 repo 協作（paas-agent-runtime + woow-paas-charts + platform owner）。根因 = MiniMax-M2.7 OpenAI-相容串流把同一 tool_call `index` 的多個 delta 重複送非 null `function.name` → langchain chunk-merge 對 name 字串串接（`langchain_core/utils/_merge.py:64`，`name` 不在 skip-set `{id,output_version,model_provider}`）→ `'hello-worldhello-world'` → tool lookup miss → doubled name 汙染 history → round-2 空串流 `ValueError("No generation chunks were returned")`（`chat_models.py:965-968`）。producer（skills.json snapshot）逐行證乾淨，doubling 純 runtime 動態。**runtime 修（PR #9 merged main `92798e4`）**：Path 2（保 `streaming=True`）`tools_by_name` 權威字典正規化整數倍自我重複 name + empty-stream guard（`code=llm_empty_response`/`retryable=true`）+ `_run_tool` structured logging + main 追加 `llm_error` 對 `UnsafeURLError` 設 `retryable=False`、transient 留 None。**platform 修（commit `cd9c903` @ `fix/tool-loading`）**：SSE gateway 兩層 lock-step——新增純函式 `_agent_error_sse_data` 轉發 runtime 的 `code→error_code`+`retryable`（原 error 分支只送 `error` 字串丟掉 additive 欄位）+ 前端 `ERROR_MESSAGES.llm_empty_response` friendly 訊息 + `state.errorRetryable` gating（預設 true 零回歸，`retryable:false` 才隱藏重試）+ done handler `full_response` 不變量註解（守「前端不依賴逐 delta」）+ `TestPhaseCSseErrorForwarding` 3 pod-free 測試（docker-desktop k8s-dev-sandbox 真 Odoo harness `0 failed of 3`；stash 重跑證 `SSE5` `no_message!=no_agent` 為既有失敗、非本修回歸）。**deploy chain（woow-paas-charts owner 確認）**：stg live agent-runtime imageTag 由 **`paas-app-bundle-cd` Fleet** 覆寫（chart 預設 appVersion/imageTag 仍 `1.0.0`，與 live `1.1.2` drift），上 stg 真正槓桿是 Fleet **不是** woow-paas-charts。**3 follow-up F-129-1~3（detail-only）**。

- **agent-runtime (B) readyz 三態鏈閉環 🎉**（4-lane maestri canvas 協作：paas-platform / Helmsmith / Fleet / deploy-stg（+ Anvil runtime image），2026-06-03 → 06-04）：**D-2 坐實**——agent-runtime 1.2.0 加了 `/livez`+`/readyz` 但 stg 實測 3 顆 ws pod probe 全 tcpSocket（operator 釘 chart 0.1.8 無 probe.style switch + operator 從未 forward）→ readyz 從沒被 kubelet 用過。**Phase 0 五項 lock-step**：**1a** operator `probe.style` passthrough（**PR #141** merged develop `4b77fe8`：config `agent_runtime_probe_style`〔env `AGENT_RUNTIME_PROBE_STYLE`，實證綁定〕+ `_build_install_values` forward **top-level** `values["probe"]["style"]`〔chart 0.2.0 讀 **top-level**、非 agentRuntime-nested——對實際 chart `values.yaml:260-263`/`_helpers.tpl:106-108` 核實，擋掉一個會 silent no-op 的 nested 建議〕+ 非法值 fail-loud `raise ValueError`（operator 端 allowlist `{tcpSocket,httpGet}`，chart-version-independent）+ set/unset-absent/invalid 3 test + probe.style 納入 forward-completeness 契約——missed-forward 家族第 6 例，[[spawn-install-values-missed-forward]]；operator suite 144 綠）+ **5th** Helmsmith operator chart **0.1.13**（`config.agentRuntime.probeStyle` → env wire，比照 externalEgress G8；operator chart 全 explicit env 映射、**無通用 extraEnv passthrough** 故新 env 必 bump chart——cross-repo 慣例記住）+ **1b** operator image **`20260604.0038`**（`operator-v20260604.0038` @ `4b77fe8`，build-operator 時間戳流程；Fleet JCR HTTP 200 直驗）+ **1c** Fleet pin（operator chart 0.1.13 + image `20260604.0038` + `probeStyle=httpGet` + agent-runtime chart 0.2.0 + imageTag≥1.2.0 **必須同組**，否則 `/readyz` 404 kill loop）+ **1d** reap → **② httpGet 兩態驗證 PASS** + hello-world 迴歸 PASS。**③④ 同鏈完成**：chart 0.3.0/0.3.1 preStop + image **1.2.1**（uvicorn drain 75）lockstep 上 stg；**T7** in-flight 砍線（1012 + audit ws_disconnect）+ **T8** preStop 端到端（+1.1s draining 503 / +37s 優雅退無 SIGKILL）全 PASS，恢復面 hello-world 兩輪過。**operator forward lane 端到端全程正確、無 missed-forward 再發**（forward-contract test + 對實際 chart 核實兩道防線兌現）。本鏈無新生 open follow-up；F-AR-1 的 ③ runtime-drain 半隨本鏈落地（見 F-AR-1 列進度註）。

- **agent-context-enrichment 落地**（feat-enrich-agent-context，2026-06-05）：統一 task agent chat 三條路徑（A SSE-legacy / B chatter / C agent-runtime pod）的 system context 注入到單一 builder `agent_session._build_system_context(channel)`（per-turn 即時重算、永不 raise）。注入 Project / Task / Cloud Services 背景（白名單 + fail-closed `cloud_app_template.agent_context_keys` + 兩層 sanitize）。同時修兩個既有缺口：**A 路徑 raw helm_values 洩漏**（`ai_assistant.py` 舊 `_get_cloud_service_context_for_channel` dump 全文 → 改走白名單 builder，secret 不再進 prompt）與 **C 路徑 pod system_prompt 從未生效**（`_build_runtime_system_context` 把 `agent_template.system_prompt` + 背景 context 組成 `user_message.payload.system_context` per-turn additive 欄位送入 pod）。spec `docs/superpowers/specs/2026-06-04-agent-context-enrichment-design.md`（D1-D7）；plan `docs/superpowers/plans/2026-06-05-agent-context-enrichment.md`；ws-protocol v1.1 additive 註記見 `docs/reference/api/agent-runtime-ws-protocol.md` §3。**2 條 follow-up F-CTX-1/2 列冊**。

- **skill-binding-ux 落地**（feat/mac-skill-binding-ux，2026-06-05）：後台 project form 一頁完成 skill「歸屬（`skill.project_id`）+ 生效（`agent_template.skill_ids` m2m）」兩層管理、skill form 開放 admin 編輯 scope/project、/woow 前台 project 設定頁以 feature flag 唯讀化。不動 skill↔project m2o schema；生效層抽單一公式 `agent_session._effective_skill_ids`（snapshot 與 `skill.is_effective` UI 欄同源、防 anti-pattern #3 drift）；共用 template 走單一 `copy(default=)` copy-on-write fork（不 sudo 繞 subset constraint）；前台唯讀由 `woow_paas_platform.portal_project_settings_editable` flag（預設 off）同時 gate 寫入 endpoint（spec 盤點的 `paas.py` detail/mcp + `ai_assistant.py` project update 三支；final review 補上 legacy service-centric mcp-servers 第四支，見 F-SBX-4）與 OWL 渲染（two-layer lock-step）。spec `docs/superpowers/specs/2026-06-05-skill-binding-ux-design.md`；plan `docs/superpowers/plans/2026-06-05-skill-binding-ux.md`；pattern `docs/decisions/patterns/resource-ownership-vs-template-activation.md` 更新 push 觸發點 + is_effective mitigation。**follow-up F-SBX-1~5 列冊**（F-SBX-5 為 2026-06-06 stg 手測新增）：
  - **F-SBX-1**（low/feature）：portal settings flag（`woow_paas_platform.portal_project_settings_editable`，預設 off）— 未來要開放只需翻 Settings → Woow PaaS 參數即可；開放前須確認會議決議（2026-06-03 議題 H）。
  - **F-SBX-2**（low/limitation）：MCP inline form 無 sync/test 鈕 — admin 在 project form 建 MCP 後須跳獨立 MCP view 完成 sync 才達 `state='connected'`（legacy chat 路徑 `discuss_channel.py:240` 才吃得到）。後台一站式若要補齊，另案在 inline form 加 sync/test 按鈕。
  - **F-SBX-3**（medium/security-decision）：project owner 經 RPC 仍可寫 dedicated template（`agent_template_rule_project_admin` 既有授權允許 `base.group_user` 對 project-bound template 全 CRUD）— 本案僅做 UI 收斂（`groups="base.group_system"`），未收緊 model 授權。若會議意圖是連 project owner 都不可自編 template，須另案收緊該 ir.rule + 補對等測試（two-layer lock-step）。對應 Gitea issue：[odoo-addons/woow_paas_platform#152](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/152)。
  - **F-SBX-4**（low/decision-pending）：legacy service-centric write endpoint `/api/workspaces/<wid>/services/<sid>/mcp-servers`（`paas.py:1295`，`api_service_mcp_servers`）的 write_actions（create/update/delete/sync/test）已於 final review 補上同一支 portal flag gate（`paas.py:1329-1332`，避免 flag-off 時 workspace owner/admin 經此舊路徑繞過唯讀）— 是 spec §3「全部三支寫入 endpoint」盤點外被發現的第四支。此 endpoint 是 cloud-service 早期的 service-centric MCP 設定路徑，已被 project-centric route 取代；**是否整支 deprecate（移除而非僅 gate）待另案決策**。在 deprecate 前 flag gate 是正確的 lock-step 補強。
  - **F-SBX-5**（medium/UX+runtime，gitea **#156**，2026-06-06 stg 手測新增；**已修 2026-06-07** feat/skill-binding-ambiguity，四層 lock-step，詳見 State 段同日 bullet + [`skill-same-name-dual-bind-snapshot-collision`](../pitfalls/skill-same-name-dual-bind-snapshot-collision.md)）：skill 綁定下拉**無法區分同名 system / user-scope skill** — skill 無 `display_name` 覆寫，many2many_tags 只顯 `name`；跨桶同名合法（`skill_uniq_user_proj_name_version` per-project + `skill_uniq_system_name_version` system 桶）→ stg project 28 實證下拉出現兩顆一模一樣的 `test-echo`（id 4 system 0.1.0 vs id 7 own 0.1.0；project 27 的 id 5/6 有被 domain 正確過濾，**非 data leak**）。選錯選到 system 顆 → pod 載入 system 版 bundle、自己那行 `Active in Pod` 維持 unchecked（症狀離原因遠）。連帶 runtime 地雷：`_effective_skill_ids`（`agent_session.py:313`）同名 shadow 只處理「bound 蓋 system」單向，**同名兩顆都 bound** 目前 view domain 與 `_check_skill_subset` 都不擋 → snapshot 出現兩個同名 entry，pod 按 name 落地撞名。修法：① skill `_compute_display_name` 後綴 version+scope ② template 內 bound names 唯一性 constraint（或 `_inverse_resolved_template_skills` 寫入前驗證），兩層 lock-step。

## Active backlog 摘要（open / pending only — 不含已 done）

> 本 block 只列**尚未處理**的 F#（status NOT done/resolved/closed），供開頭快速掃描；詳情與 origin/severity 見下方 `## Index` 同號列。**不刪除、不重編號歷史**——已 RESOLVED/CLOSED 的 F1 / F15 / F21 / F22 / F23 / F24 / F-DP-2 / F-PILOT-1 / F-PILOT-2 / F-PILOT-3 / F-PILOT-5 / F-PILOT-6 / F-PILOT-7 仍保留在 Index 表原列，僅不在此摘要重列。新增或關閉 F# 時請同步更新本摘要。

| 系列 | open / pending F# | 一句話狀態 |
|------|-------------------|-----------|
| Cycle 1 | F2, F3, F4, F5, F6, F7, F8, F9, F10, F11 | 多為「待新 PR / 待設計」；F6→F11 串聯（sanitizer whitelist 升級觸發後一起評估） |
| Cycle 2 | F12, F13, F14, F16, F17, F18, F19, F20 | 「待新 PR / batch / next refactor cycle」；F19 為 round 1 minor batch 集合點 |
| Cycle 3 | F25, F26, F27, F28 | F25 已開 follow-up issue #52；F26/F27/F28 待開 polish / test / architectural issue |
| Cycle 4 | F-X1, F-X2, F-X3, F-X4, F-X5, F-X6, F-X7, F-X8 | dedicated/cluster/future PR；F-X7 等 F-X1 cron 改 sidecar_backed 後才能 drop column |
| Cycle 5 | F-PA-1 ~ F-PA-16（全部）| F-PA-13（Phase B/C 必修）+ F-PA-14（Phase D-F 必修）為 active 前置 hard 提醒；其餘 small PR / docstring / reporter 順手 |
| Cycle 6 (skill-list) | F-SK-1 ~ F-SK-9（全部）| F-SK-1/2 medium caveat；F-SK-3~9 reviewer major（皆非阻斷 ship，next touch 順手 / 排線圖時處理）|
| Cycle 6 (decouple frontend) | F-DP-1, F-DP-3, F-DP-4, F-DP-5, F-DP-6, F-DP-7, F-DP-8, F-DP-9 | F-DP-1 需 design PR（`_get_project_stages` 收口）；F-DP-3~8 reporter polish；F-DP-9 track-only passive observation |
| deploy-tooling #85/#86 | F-DT-1, F-DT-2, F-DT-3 | 皆 pre-existing / reviewer nit，超出修正 scope；小 PR / future PR |
| PR #96 stg-deploy follow-up | ~~F-SDS-1, F-SDS-2~~ ✅ 全 RESOLVED | **F-SDS-1 + F-SDS-2 皆於 PR #99（fix/issue-98 → develop）修復**（mcp_server + mcp_tool scope-flip `write()` guard + 13 test 紅綠 + `__init__.py` cleanup；issue **#98**、PR **#99**）。詳見 Index 同號列 |
| skill upload UX (manual stg) | F-SKU-1 | low/UX — Skills list view 空狀態無上傳入口（upload wizard binding action 只在選取後 ⚙ Actions 選單出現 + `create="false"` 無 New 鈕）；建議仿 MCP「New」放固定 Upload 按鈕救空狀態。issue **#102** |
| agent-runtime gap C / pilot（detail-only，不在 Index 表）| F-GC-1, F-GC-2, F-GC-3, F-PILOT-4, F-PILOT-8, F-PILOT-9, F-PILOT-10, F-PILOT-11 | F-GC-1/2/3 credential/durability hygiene 待排程；F-PILOT-4/8 operator green 但剩 cross-repo chart lock-step（E0-CAVEAT-2/3）；F-PILOT-9 未來 Phase；F-PILOT-10 code merged + round-trip 達成，剩 G3/G4 + production gate；F-PILOT-11 需 design 排程。詳見下方 `## F-PILOT` / `## F-GC-*` / `## F-PILOT-11` 段 |
| agent-runtime LLM（2026-06-02，detail-only）| ~~F-ARL-1~~ ✅, F-ARL-2, F-ARL-3, F-ARL-4 | **F-ARL-1（#123）✅ RESOLVED**：client timeout 分級(spawn 90s / invalidate 10s) + spawn idempotent retry(transient timeout/connection 重試、4xx 不 retry) + relay WS connect bounded retry(backoff，deadline 60s；retry 只包 connect 不重送 user_message) + 前端 `status:spawning`「AI 啟動中…」進度（黃，非紅錯誤）；plan `docs/superpowers/plans/2026-06-02-agent-runtime-coldstart-onpublish.md`（同 F-PILOT-10 G3 的 UX 延伸已收口）。原症狀（UX/robustness，gitea **#123**）：cold-start 首則訊息常因 pod 啟動時序失敗（operator spawn `Read timed out 30s` → pod WS `Connect call failed`），需按「重試」第 3 次才成功。**F-ARL-2**（observability，gitea **#124**，**修在 `paas-agent-runtime` repo**）`langgraph_runtime` 的 `stream: LLM path`/`LLM init`/`LLM stream complete` 為 info 級被 root logger(WARNING) 濾掉,`kubectl logs` 只見 structured event,debug LLM 行為拿不到 trace；建議 app logger 設 INFO 或 LLM trace 走 structured logger。**F-ARL-3**（feature/UI，gitea **#126**）Projects 總覽頁加平台層「Agent」狀態卡（鏡像 AI Connection）；已 brainstorm 拍板 **config-only**（無 live operator ping）+ **並排兩欄**,後端 `/api/ai/agent-status`（ready = enabled + 預設 template + template model 有 key + operator 已設定）、前端 `ProjectsOverview` 加卡 + `ai_service.fetchAgentStatus`;設計細節見 issue #126。F-ARL-1 已於本 PR（feat/reine-code-start）以 code 修復（含 #111 on-publish push 同 PR）；F-ARL-2 無 code（修在 `paas-agent-runtime` repo）；F-ARL-3 為 #126 feature。**F-ARL-4**（medium/runtime，gitea **paas-agent-runtime#23**，2026-06-06 PR #153 stg 驗證新增，**修在 `paas-agent-runtime` repo**）LLM tool-call **一律傳空 kwargs**——使用者訊息顯式要求 `{"input": "PR153-VERIFY"}` 仍空、skill subprocess stdin 收 `{}`（對照：pod 內直餵 stdin 跑 script 正常輸出）；同 turn 伴隨 `normalized doubled tool name 'test-echotest-echo'` 警告（1.1.3 normalization 有作動，args 半邊疑殘留）。兩假設待 trace 切分：① loader `StructuredTool` 餵 raw JSON-schema dict（`_DEFAULT_INPUT_SCHEMA`）被 langchain-core silent 吞 args ② MiniMax-M2.7 args 序列化出包。建議先補 F-ARL-2 同方向的 tool-call args trace（raw LLM args vs 進 `_run` 的 kwargs）。stg 環境：agent-runtime 1.3.0、project 27、合規 bundle test-echo 0.1.2。 |
| agent-runtime 測試環境耦合（#123/#111，2026-06-02）| ~~F-ARC-1~~ ✅ | **✅ RESOLVED（2026-06-02，commit 702b746〔9 個〕+ 7dbac81〔ecpay×5〕，全部 test-side 修復、零 production 變動 → 全模組 `0 failed, 0 error(s) of 843 tests`）**。ecpay 最終真因是 `_ensure_active_payment_method` 用預設 `active_test=True` 搜不到 inactive 的 ecpay payment.method（非 COA/journal 缺漏——sandbox 本就有 generic_coa + sale journal + USD），改 `active_test=False` 找到並激活即解。以下為原始診斷紀錄。medium/test-infra — sandbox 全模組測試 14 個 pre-existing 失敗。**2026-06-02 全模組實測釘死**（docker-desktop `dev-sandbox-fix-pre-existing`，`4 failed, 10 error(s) of 843 tests`，14 個與清單完全一致；環境忠實：operator/ai_assistant/agent_template 已 seed、ECPay provider=test、TWD inactive、`--without-demo` 無 base.user_demo）。**4 FAIL**：auto_enable c3（`True is not false` test:171）/ b2（`1 != 0` test:221）+ sse_gateway SSE5（`'no_message' != 'no_agent'` test:270）+ router ac20（regex 不匹配 test:59）；**10 ERROR**：ecpay×5（setUp:38 `NotNullViolation: payment_method_id`，5 個共用 setUp）+ oauth2×3（http.py:1395 `TypeError: returns an invalid value: MagicMock`）+ smart_home×2（`KeyError 'base.user_demo'`）。**⚠️ 分類修正**：原列「長期環境缺漏」的 **oauth2×3 與 router×1 其實不是環境耦合**（單獨跑也紅）——oauth2 是 test helper（`test_oauth2.py:584-628`）直接呼叫 `@route` method 並 mock `render/redirect` 回傳 MagicMock，被 `route_wrapper`→`Response.load` 拒收（test/Odoo 版本不相容；修：fake_render/redirect 回傳合法 Response 或改 HttpCase）；router 是 stale TDD 斷言 vs 產品演進（bare `ai-assistant` route 已被 `1d90953` 刪，phase-versioned-test-assertion family；修：更新斷言對齊 sub-path 新合約）。**真環境耦合僅 10 個**（auto_enable×2 / sse_gateway×1 / ecpay×5 / smart_home×2），修法：setUp fail-isolation（c3/b2 清 `_TOGGLE/_URL/_KEY/_DEFTMPL`、SSE5 設 `ai_auto_reply=False`、smart_home 自建 user 取代 base.user_demo）或 sandbox seed 補強（ecpay 補激活 `payment.method` + TWD currency）。教訓：workflow 靜態分析（含對抗覆核）曾 high-confidence 誤判 oauth2 會綠 → 測試生死須實跑 sandbox。gitea **#131**（已回填實測 comment） |
| agent-runtime #129 deploy chain（detail-only） | F-129-1, F-129-2, F-129-3 | **F-129-1**（medium/deploy-infra，owner woow-paas-charts）`bump-agent-runtime` repository_dispatch 無 handler——paas-agent-runtime `build-and-trigger.yml` 推 v* tag 時 build image + curl dispatch 給 woow-paas-charts，但該 repo 只有 `publish-charts.yaml`（push main + `charts/**` 觸發），沒接 dispatch → 事件 no-op。**根因更深（owner 查證）**：Gitea 1.25.4 根本無 `repository_dispatch` 端點 → 那個 dispatch 本就永遠 404。**chart-half RESOLVED（2026-06-02，paas-agent-runtime owner）**：auto-bump 改成 agent-runtime CI 直推 charts repo（charts PR #11）+ 移除永不觸發的 handler。**但 operator-pin 半邊仍手動**：CI 直推只同步 chart（charts repo），不碰 operator 的 Fleet pin（paas-app-bundle-cd `values-stg.yaml` 的 imageTag + chartVersion）→ 每次 release 仍須手動 bump 那兩值（見 F-129-3）。未驗前提：`CI_GITEA_TOKEN` 對 charts 寫權 + charts main 不擋 CI 直推（下次推 tag 時 CI log 會曝成敗）。**F-129-2**（low/hygiene，owner woow-paas-charts）chart default appVersion/imageTag pin `1.0.0` 與 stg live `1.1.2`（Fleet 覆寫）drift，純 cosmetic 對齊（新環境/無 Fleet 覆寫 install 才吃 default）；做的話需確切 tag 字串（不帶 `v`，如 `1.1.3`）+ 確認 image 已 build push。**F-129-3**（deploy-tracking）#129 上 stg 鏈（2026-06-02 進度）：① ✅ paas-agent-runtime 推 `v1.1.3` tag → CI build `agent-runtime:1.1.3` image；② ✅ charts bump `0.1.6`（charts PR #10）；③ ✅ **stg runtime DONE（2026-06-02，paas-app-bundle-cd PR #2 merged）**：`values-stg.yaml` `operator.agentRuntime` imageTag→`1.1.3` + chartVersion→`0.1.6`，Fleet reconcile 綠、operator pod rolling 重啟、新 spawn 吃 1.1.3（上游已驗 chart 0.1.6 在 helm registry、image 1.1.3 在 JCR）；既有 per-project pod **不會** reap→respawn 自然汰換到新 image（2026-06-02 查 `extra/paas-operator/src/api/agent_runtimes.py` 更正先前假設）——`spawn_or_attach` 只看 release 是否存在（`:302`）、**不比 image tag**；image.tag 僅在 cold-start install 注入（`:112-115`）；reap（`:431` 只 scale 0）→ resume（`:342-350` scale 回 1）仍掛**舊** image。要換 image 須逐顆 `DELETE /api/agent-runtimes/{hash}`（`:507` helm uninstall → 下次 chat cold-start 注入新 tag），或未來 auto-roll 走 in-place `helm upgrade --set image.tag`（保 PVC，見 F-AR-1）。stg 已實證（2026-06-02 teardown test4=j2et → cold-start=1.1.3）。**prod（paas-operator）未動，另議**；④ ✅ platform PR **#133** 已 merged 進 develop（`52e0304`，2026-06-02 11:33）→ ⏳ 待 paas-odoo-ci 打包 paas-odoo image → deploy-stg。**注意：#129 的 retryable/`llm_empty_response` 契約是雙邊的——runtime 半邊（1.1.3）已上 stg 會 emit，但 Odoo SSE gateway 轉發（#133）上 stg 前，stg 上 retryable/friendly 訊息 UX 仍不完整（核心 doubled-name tool-calling bug 已由 runtime 1.1.3 修，可先驗）**。owner 跨 agent-runtime / **paas-app-bundle-cd** / platform |
| agent-runtime auto-roll（版本 bump 不自動 roll，2026-06-02 canvas 協作） | F-AR-1 | **F-AR-1**（medium/structural，**operator-half owner = 本 repo `extra/paas-operator/`**）per-ws pod image tag 在 operator/chart bump 後不會自動 roll（見上 F-129-3 更正），每次發版須手動逐顆 DELETE-release——結構性缺口，下次發版會再撞。**四半 lock-step 設計（2026-06-02 與 Anvil/Helmsmith/Fleet 在 maestri canvas 談定，SSOT = canvas note `agent-runtime-auto-ro`）**：① chart 半（Helmsmith）pod annotation `agent.woow.tw/expected-app-version = {{ .Chart.AppVersion }}`（+ 之後 `expected-{skills,template,mcp}-hash` 獨立 key）+ `strategy: Recreate` + `terminationGracePeriodSeconds: 120`；② **operator-reconcile 半（本 repo，待實作）** reconcile 判定 = operator env `AGENT_RUNTIME_IMAGE_TAG`(desired) vs pod annotation(actual) diff；app-version mismatch → in-place `helm upgrade --set image.tag --wait`（timeout **240s** ≥ grace + cold-start，保 PVC，非 DELETE 大法）；hash mismatch → 只 re-patch ConfigMap 輕量 reload 不 roll；觸發點預設 lazy-at-next-`spawn_or_attach`（不另建 poller），緊急安全修才 active sweep；③ runtime 半（Anvil）SIGTERM graceful drain handler（draining flag → /readyz 503 → 等 in-flight turn 收完 → 退，wait-cap ~110s）+ uvicorn `timeout_graceful_shutdown`，與 1.2.0 `/livez`+`/readyz` 同一 PR；④ deploy 半（Fleet）Fleet roll operator 本體。**grace 120s 三方 lock-step（chart / runtime drain-cap / operator --wait）任一改要同步**；`_MAX_TOOL_ROUNDS`(8) / per-call timeout 改會動 ceiling，Anvil 承諾知會重算。契約 grounded：root-level 同設計。**等 Eugene 綠燈 Anvil 開工 + 三 PR sync merge**。**[2026-06-04 進度]** readyz 三態鏈（見 State 段同日 bullet）把 **③ runtime-drain 半落地**：image 1.2.1 SIGTERM/preStop graceful drain（uvicorn drain 75）+ chart 0.3.0/0.3.1 preStop，stg T7 in-flight 砍線 + T8 preStop 端到端驗過；原設計 grace 120s/drain ~110s 與實落 drain 75 的數值以已上 stg 的 0.3.x/1.2.1 為準。剩 **② operator-reconcile 半（本 repo，仍待實作）**；① chart annotation（`expected-app-version`）半是否已隨 0.3.x 帶上待 Helmsmith 確認。**[2026-06-05 進度]** ② 落地前的**手動升版路徑（DELETE reap → cold-start）已持久化為雙 repo 正式 runbook**：platform 視角 [`docs/guides/deployment/agent-runtime-image-upgrade-sop.md`](../../guides/deployment/agent-runtime-image-upgrade-sop.md)（PR #151；§2 記載本條現狀、§3 為 stg 5/5 實跑驗證的 reap SOP）↔ runtime 視角 paas-agent-runtime `docs/runbooks/image-upgrade-sop.md`（PR #21，#22 互鏈），F-AR-1 落地前以該 SOP 為唯一換 image 程序；F-AR-1 結案時兩份 SOP 須同步改寫（手動 reap 降級為 fallback）。owner 跨 operator(本 repo) / agent-runtime / woow-paas-charts / paas-app-bundle-cd |
| ~~deploy-stg tooling（既有 sandbox 不 fire addon migration）~~ ❌ RETRACTED | ~~F-DT-4~~ | **F-DT-4 撤回（2026-06-02，非 gap）**：原報「標準 deploy-stg 在既有 sandbox 不 fire `woow_paas_platform` migration」**錯誤**。實測：#135 deploy 後 stg `installed_version=18.0.1.0.31` + demo skill `bundle_sha256=6a8e7f87`（18.0.1.0.31 migration 已 fire、noupdate seed 已更新），且**未跑任何手動 `-u woow_paas_platform`**。真因 = Odoo `-u <module>` **cascade 到 dependents**：`create.sh:402` 的 `-u web` 連帶升所有 `depends on web` 的模組（含 `woow_paas_platform`）→ version-bump migration 在標準 deploy 就 fire。`:537` install-guard 跳過的只是 `-i` install 區塊，不影響 helm `odoo.updateModules=web` 觸發的 `-u web` cascade。**create.sh 無須改**。教訓：`-u <module>` 升「該模組 + 其 dependents」，不是只該模組（原分析把 cascade 方向搞反）。 |
| deploy-stg tooling（upgrade 丟 agent-runtime env secret，2026-06-02） | F-DT-5 | **F-DT-5**（medium/deploy-infra，owner deploy-stg tooling = 本 repo `create.sh` + `charts/odoo-dev-sandbox`)既有 sandbox `helm upgrade` **無 `--reuse-values`**（`create.sh:379-462`）→ 任何「該次 deploy 沒重新帶」的 agent-runtime secret 會被 chart 重 render 丟掉。具體:`AGENT_RUNTIME_NAMESPACE_SECRET` 在 chart 走 `secretKeyRef … optional:true`（`odoo-deployment.yaml:108`）+ secret key 只在 `agentRuntime.namespaceSecret` 非空才建（`secrets.yaml:30`，預設 `""`）;deploy 環境沒 `export AGENT_RUNTIME_NAMESPACE_SECRET` → `create.sh:272-321` 注入 block 跳過 → upgrade 後 key 消失、env UNSET → odoo `agent_session.py:138 _get_namespace_secret()` **fail-closed raise UserError** → 既有 sandbox 重部署後 **chat 全炸「namespace secret missing」**（症狀像 feature 壞掉,實為 secret 掉）。**2026-06-02 #135 e2e 實咬**：deploy-stg 第一輪 upgrade 丟了 secret,靠 Eugene 給原值 + hmac 三 hash（pid 8/2/5 → j2et/c2pt/xpg）驗證還原。部分已知 = gap-chain pitfall gap #4「deploy-stg 須帶 `AGENT_RUNTIME_*` env」,但**新 insight = `--reuse-values` 能讓忘記帶 env 時不丟既有 secret**。durable 修法（擇一/並用）:create.sh upgrade 加 `--reuse-values`;或 upgrade 前先讀既有 K8s secret 把 agent-runtime 各 key 自動回灌（免人工記得 export env）。同 webhook secret 系列（`agent-runtime-webhook-secret*`）同模式風險。（注：原列為 F-DT-4 的姊妹,但 F-DT-4 已撤回為非 gap；本條是 create.sh-upgrade 路徑唯一**真**缺口。）owner deploy-stg tooling |
| agent-context-enrichment（2026-06-05，detail-only，不在 Index 表）| F-CTX-1, F-CTX-2, F-CTX-3 | **F-CTX-1**（open）prompt-injection content filtering — project/task description 為 user-controlled，現以「NOT instructions」untrusted 標註緩解（spec Known limitations）；根治需 content filtering / 結構化隔離。**F-CTX-2**（done 2026-06-05）pod 端消費 `payload.system_context` — paas-agent-runtime#20 merged（`8a9b387`，空字串視同缺席語意已對齊 protocol doc）。**F-CTX-3**（done 2026-06-05，issue #147 closed）stg 端到端驗證全過 — agent-runtime 1.3.0 + operator bump（paas-app-bundle-cd#8）+ platform `20260605.0102`；5 項驗證綠（fail-closed / 正負向 / cold-start+attach 一致 / 缺席語意），舊 image pod 已逐顆 DELETE（stg 全面 1.3.0）。衍生發現 portal persist bug → PR #150 修復 + stg 復驗 3 項綠。詳見下方 `## F-CTX` 段 |
| skill-binding-ux（2026-06-05，detail-only，不在 Index 表）| F-SBX-1, F-SBX-2, F-SBX-3, F-SBX-4, F-SBX-5 | **F-SBX-1**（low/feature）portal settings flag（`portal_project_settings_editable`，預設 off）— 未來開放翻 Settings 參數即可，開放前須確認會議決議。**F-SBX-2**（low/limitation）MCP inline form 無 sync/test 鈕 — admin 在 project form 建 MCP 後須跳獨立 view sync 才達 `connected`（legacy chat 才吃得到）。**F-SBX-3**（medium/security-decision）project owner 經 RPC 仍可寫 dedicated template（`agent_template_rule_project_admin` 既有授權）— 是否收緊待決策，對應 Gitea issue [odoo-addons/woow_paas_platform#152](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/152)。**F-SBX-4**（low/decision-pending）legacy service-centric `/api/workspaces/<wid>/services/<sid>/mcp-servers`（`paas.py:1295`）write actions 已補同一 portal flag gate（`paas.py:1329`，final review 第四支）— 整支是否 deprecate 待另案決策。**F-SBX-5**（medium/UX+runtime，gitea **#156**，**已修 2026-06-07** feat/skill-binding-ambiguity）skill 綁定下拉無法區分同名 system / user skill + 同名雙綁 snapshot 撞名；修以四層 lock-step（display_name 後綴 / no-dup constraint / snapshot dedup / 保守 migration `.34`），pitfall doc `skill-same-name-dual-bind-snapshot-collision`。詳見上方 State 段 2026-06-07 bullet |
| skill-bundle token TTL — 長壽 pod 無法 re-fetch（2026-06-03 #135 e2e 撞） | F-SKT-1 | **F-SKT-1**（medium/operator+runtime，owner = 本 repo operator token 簽發 `extra/paas-operator/` + Anvil runtime 401 處理）`SKILL_BUNDLE_TOKEN` 1h TTL;operator **attach 路徑**（pod 已 running、replicas>0）**不重簽** token（只有 `_resume_pod` `agent_runtimes.py:347`〔replicas==0 resume〕與 cold-start 才簽）→ 活超過 1h 沒被 reap 的 pod,token 過期後任何 skill-bundle re-fetch（新 skill publish / cache-miss）→ Odoo `agent_runtime_skill` 回 **401**。**2026-06-03 #135 e2e 撞**：test4 pod 13:27 cold-start、token ~14:27 過期、~16:00 re-fetch root-level bundle 撞 401;workaround = scale-0 → resume 重簽 fresh token（Eugene 授權,成功,e2e 後續 PASS）。**設計傾向（待與 Anvil 開）**：① runtime 收 401 → 向 operator/Odoo 換 fresh token（refresh endpoint,根治,Anvil runtime 半）;② stopgap = 拉長 skill-bundle token TTL ≥ pod reap-TTL（operator 半,簡單但活超過 TTL 仍撞）。**不採**每次 attach 重簽（`set_deployment_env` 改 deployment spec → 每訊息 rollout churn pod + 切 in-flight turn）。**[2026-06-06 PR #153 stg 驗證再確認 + 開 issue #155]**：31h pod（project 10）**first-fetch 也撞**——pod 從未調用過 skill（`/workspace` cache 全空）、lazy 首次 tool_call 即 401 → 該 pod 一輩子載不了任何 skill（影響面比「re-fetch」更廣：所有活過 1h 的 pod 的**全部** skill 功能）；Odoo `agent_runtime_skill.py:82` exp 驗證 fail-closed 行為正確。token 簽發點 `extra/paas-operator/src/services/jwt_signer.py:74`（`exp_seconds=3600`）。gitea **#155**。owner 跨 operator(本 repo) + agent-runtime(Anvil) |
| skill bundle 上傳缺結構驗證（2026-06-06 PR #153 stg 驗證） | F-SBC-1 | **F-SBC-1**（medium/data-integrity，gitea **#154**）`skill_upload_wizard.action_upload` 只做 size cap + sha256（「Odoo never parses the bundle」by design）→ 不合 runtime 契約的 bundle 照收，直到 pod 內**首次 invoke** 才 tool_error（錯誤浮現點離上傳極遠難歸因）。stg seed `test-echo 0.1.0` 三重違規實證：tar 包 wrapper dir（SKILL.md 不在 root）+ 夾帶 macOS `._SKILL.md`（打包未設 `COPYFILE_DISABLE=1`）+ frontmatter 缺必填 `name` + 無 script——**stg 上 skill 在 2026-06-06 前從未成功執行過**；hello-world 0.1.0 同家族（entrypoint mismatch，[[hello-world-skill-entrypoint-mismatch]]）。修法：① wizard 上傳時 parse tar fail-fast（root SKILL.md + frontmatter name/description 必填；entrypoint 檔存在 / AppleDouble 為 warning 級）——契約 SSOT 在 paas-agent-runtime `runtime/skills/{cache,loader}.py`，驗證規則演進需 lock-step ② stg 資料修復：重傳合規 system-scope bundle（合規參考件 = project 27 'PR153-skill-verify' 的 user-scope `test-echo 0.1.2`，stg 首個全鏈路通過的 skill）。owner paas-platform（wizard）+ stg data。**[2026-06-07 已修 — PR #157 + stg 資料修復完成]** ① wizard 結構驗證（fail-fast）已實作，**真 Odoo sandbox `TestSkillUploadWizard` 28 測試全綠**（PR **#157**，含對抗式 review 三鏡頭抓到的 3 must-fix：`getmembers()` eager 物化 DoS → streaming 先擋後讀〔200k-member 炸彈 89x 快/63x 省記憶體〕、special-file〔FIFO/device〕false-negative 硬擋、stdlib 扁平解析 → `yaml.safe_load` 精確鏡像 runtime〔tab 縮排/未閉合引號等 pod 端會炸的文法錯上傳當下即擋〕；`__manifest__.py` 宣告 `external_dependencies: yaml`）。② stg 資料修復（odoo shell 換 attachment，Eugene 授權）：違規 `test-echo 0.1.0` 的 **system(skill id=4，0 binding)** + **user(id=7，綁 odd2 template)** 兩筆 bundle 已替換成合規 bundle（sha `fe2bf1ed`、validator CLEAN、重新匯出確認）。**三項精確化發現**（validator 實測）：(a) **hello-world 0.1.0 (system id=1) 結構其實合規**（SKILL.md root + 完整 frontmatter + scripts/main.py），其 entrypoint mismatch 屬 runtime `paas-agent-runtime#14`、非 bundle 結構違規 → **不需資料修復**（上方「hello-world 0.1.0 同家族」敘述據此更正）；(b) system test-echo (id=4) 違規但 0 binding〔孤兒〕；(c) 真正被綁定使用的違規是 user test-echo (id=7, odd2 template)。**pod 端到端執行驗證（fetch→extract→subprocess exit 0）待 F-SKT-1 #155 stopgap 部署 stg 後**——當前所有 stg agent pod 活超 1h、token 全過期（正是 #155 現象），自簽 token serve 驗證受 classifier 安全邊界擋下，完整串流走 #155 部署後 verify session（「單層修復不可觀測」紀律）。 |

## Index

| # | Severity | Origin | Cycle | 主題 | Status |
|---|----------|--------|-------|------|--------|
| F1 | medium | refactor | 1 | ACL 對等性 checklist 持久化 | doc-only ✅（本 doc 已寫進 [[odoo-addon-std-rule-symmetric-deactivation]]） |
| F2 | medium | refactor | 1 | `_sanitize_helm_values` 同名異實重命名 | 待新 PR |
| F3 | medium | refactor | 1 | Cross-workspace incompatible extras 注 AI prompt | 待設計 |
| F4 | medium | pre-existing | 1 | `_check_project_access` / `_check_task_access` 對 unbound IDOR | 待新 PR |
| F5 | medium | refactor (deferred) | 1 | Migration xmlid drift 防呆 assertion | 使用者 accept 不加本 PR；待新 PR |
| F6 | medium | refactor | 1 | D9 階段 2 — sanitizer 升級 whitelist 模式 | 等 telemetry，待新 PR |
| F7 | minor | refactor | 1 | BottomNav mobile vs Sidebar D4 拆分後 UX 一致性 | 待設計 |
| F8 | minor | refactor (test) | 1 | Test hardening — AC#11 / AC#14 / B1 group_project_user / sanitizer fallback | 待新 PR |
| F9 | minor | refactor | 1 | Sanitizer / `_loadCloudServices` 等 silent fallback observability | 待新 PR |
| F10 | minor | pre-existing | 1 | `_list_projects` with sudo + empty domain 跨 tenant 列 | D5=A 放大；待新 PR |
| F11 | minor | refactor | 1 | Sentinel `***REDACTED***` i18n 化 | 由 F6 觸發後一起評估 |
| F12 | medium | pre-existing | 2 | Controller `helm_values` 漏 sanitize（違反 cycle 1 Inv #6） | 與 F2 合併 or 獨立 hotfix |
| F13 | medium | refactor | 2 | B1 task ACL deactivation 對齊 `18.0.1.0.23` pattern | next refactor cycle `18.0.1.0.25` |
| F14 | medium | refactor (caveat) | 2 | Cleanup hook `mapped` 路徑 belt-and-braces `sudo()`（CAQA C1） | 待新 PR |
| F15 | info | refactor | 2 | `_do_cleanup` retry-on-error semantics 文件化（R2-1） | doc invariant inline + [[mcp-project-decouple]] §Inv |
| F16 | info | refactor | 2 | `mcp_server` / `mcp_tool` rule-count parity lint check（R2-2） | 待新 PR |
| F17 | minor | refactor (test) | 2 | `state='error'` re-entry test case（R2-3） | 待新 PR |
| F18 | trivial | refactor | 2 | `_create_admin_user` helper docstring 過 cautious（R2-4） | reporter 順手清 or 留 |
| F19 | minor | refactor | 2 | Round 1 17 minor / info 集合（silent-failure / type-design / migration assertion）| 留 batch 處理 |
| F20 | minor | refactor (simplify) | 2 | Controller manual check helper 抽取 + `_get_user_mcp_tools` mixin | 待新 PR |
| F21 | medium | refactor (cycle 3) | 3 | URL pattern 統一 + service-centric MCP endpoint deprecation (OR-1 / OR-6) | gitea #50 ✓ |
| F22 | medium | refactor (cycle 3) | 3 | D5 invariant 重設計 cluster — sidecar MCP auto-create / rebind race / `@api.constrains` ORM enforcement (OR-2 / OR-4 / OR-5 / R-4) | ✅ **RESOLVED cycle 4** — see [[mcp-url-redesign]]，issue #49 closed |
| F23 | medium | spec gap (cycle 3) | 3 | Unbound project ACL — endpoint 對 unbound 路徑無防 enumeration (OR-7 / R-4 partial) | ✅ **RESOLVED cycle 4** — see [[mcp-url-redesign]]，issue #51 closed（合 PR #49） |
| F-X1 | medium | refactor (cycle 4) | 4 | Cron `_cron_retry_mcp_sync` 仍 query `auto_created=True`，新 sidecar-backed row 不掃 (CAQA M1) | dedicated PR — `mcp_server.py:214-217` |
| F-X2 | medium | pre-existing + refactor (cycle 4) | 4 | `ai_assistant._check_project_access` bound-but-no-workspace blanket allow (CAQA M2) + paas.py `_resolve_project_access` 與 ai_assistant.py drift edge case (reviewer SC-6b) + F4 task path + F10 list path 整理 | cluster PR with F4 / F10 |
| F-X3 | low | refactor (cycle 4) | 4 | Codebase-wide comment ref noise — 4 處 production code 殘留過去 PR plan/OQ/CAQA refs (CAQA L1, reviewer SC-19) | reporter phase / batch with F19 |
| F-X4 | low | refactor (cycle 4) | 4 | URL helper duplication — `_compute_sidecar_url_hint` (controller) + `_model_build_mcp_endpoint_url` (model) algorithm 重複 (CAQA L2, plan OR-X8) — 含 SC-13 dead code 評估 | F19 follow-up |
| F-X5 | low | refactor (cycle 4) | 4 | SSRF error message IPv4 vs IPv6 格式微不一致（IPv6 用 `[{host}]`、IPv4 raw） (CAQA L3) | future PR / 統一格式 |
| F-X6 | trivial | refactor (cycle 4) | 4 | `test_mcp_d5_removal.py:194-200` 註解寫「D5 active」但 D5 已拔 stale (CAQA L4, reviewer SC-10) | reporter 順手清 or batch |
| F-X7 | medium | refactor (cycle 4 deferred) | 4 | OQ-2 (b) keep deprecated `auto_created` field — 等 F-X1 cron 改 sidecar_backed 後 drop column 安全 | follow-up after F-X1 |
| F-X8 | low | tester surface (cycle 4) | 4 | `scripts/test-addon.sh` worktree env setup gaps surfaced during cycle 4 test runs | dev script PR / batch with F24 sub-(b) |
| SC-cluster | minor | reviewer (cycle 4) | 4 | Round 1 reviewer 20 條 soft caveat（SC-1~SC-20）— SC-1/SC-2 doc updates by reporter; SC-3~SC-6/SC-6b 已聚 F-X1/F-X2; SC-7/SC-8/SC-9/SC-10/SC-11/SC-12 doc clean by reporter; SC-13 → F-X4; SC-14 (`write_actions` naming) → future PR; SC-15~SC-18 test gaps → batch with cycle 1 F8 + cycle 2 F17 + cycle 3 F27; SC-19 → F-X3; SC-20 → F-X4/F-X5 | 分散處理 |
| F24 | trivial | doc (cycle 3) | 3 | Issue #48 carve-out comment（OR-3）+ worktree env gaps（OR-8）+ declaration↔migration pitfall（OR-9） | reporter phase ✅ (本批已落) |
| F25 | important+ | reviewer (cycle 3) | 3 | Frontend UX hardening + error handling — McpServersSection delete/sync/test 沒 else (R-3) + `_delete_project_mcp_server` 沒 try/except + update 路徑沒 savepoint parity (R-6) + CloudServiceBindingSection rebind-from-unbound dropdown 空 (E2E-G1) | Follow-up issue: #52 |
| F26 | minor | reviewer (cycle 3) | 3 | Polish cluster — `workspace_id` 缺 `readonly=True`（R-5）/ `support_service.js` optimistic merge stuck spinner（R-9）/ `ir_rules.xml:217-238` comment 改 zh-tw（R-10） | 新 polish issue 待開 |
| F27 | minor | reviewer (cycle 3) | 3 | Test gaps G1 / G3 / G6（R-7）— update whitelist 範圍 / sync/test ACL matrix / XSS sanitize 未測 | tester 補 red 或 follow-up issue |
| F28 | long-term | architectural (cycle 3) | 3 | Description rich-text editor 升級 — `document.execCommand` (legacy) → ProseMirror / TipTap (R-8) | architectural issue，等 editor 需求變寬時開 |
| F-PA-1 | medium | refactor (cycle 5) | 5 | `agent_session._get_namespace_secret` fallback default `_DEFAULT_SECRET` hardcoded — 公開 git repo 可預計算 hash (CAQA round 1 M-3) | install hook hard guard PR — `agent_session.py:7` |
| F-PA-2 | medium | refactor (cycle 5) | 5 | `cloud_service._sidecar_url_prefix` return None for missing helm metadata → broad filter 可能誤殺其他 service sidecar-backed MCP (CAQA round 1 M-4) | fixture / pre-deploy edge case PR — `cloud_service.py:1175-1194` |
| F-PA-3 | medium | refactor (cycle 5) | 5 | `project.cloud_service_id` stored compute 失去明示 `ondelete='set null'` — verify Odoo 18 default 後補明示 (CAQA round 1 M-5) | small PR — `project_project.py:18-27`；need Odoo 18 stored compute M2O FK default verify |
| F-PA-4 | medium | refactor (cycle 5 deferred) | 5 | Phase A task #14-18 五個 view file defer — agent_template_views / agent_session_views / skill_views / project_access_views / project_external_mcp_setting (含 SSRF risk 警示)；leader 拍板 OQ-A-3 = defer Phase B/C | Phase B/C admin UI batch |
| F-PA-5 | low | refactor (cycle 5) | 5 | `skill_rule_project_user` perm_write/perm_create=False — design §3.2 line 1528「user W/C only own draft」implementation 簡化為 conservative R only；ir.rule 無法表達「own draft」需 Python state guard (CAQA round 2 C-A-1) | Phase B/C 補時加 state='draft' + uploaded_by_uid=self Python gate |
| F-PA-6 | low | refactor (cycle 5) | 5 | `ASSIGNABLE_ROLES_BY_ROLE[ROLE_OWNER]` 包含 ROLE_OWNER；design §4.3 line 2090 不含 owner — implementation 寬鬆（owner 可邀請其他 owner）；business intent 確認 (CAQA round 2 C-A-2) | leader / planner 拍板 design 是否更新；implementation 若改窄需 Python gate 同步 |
| F-PA-7 | low | refactor (cycle 5) | 5 | `skill.bundle_attachment_id` `groups='base.group_system'` field-level mask — 同時擋 project owner/admin 透過 backend form 直接 attach；admin 必須走 ORM sudo / API endpoint upload (CAQA round 2 C-A-3) | Phase B/C bundle upload API + maintainer doc update |
| F-PA-8 | low | refactor (cycle 5) | 5 | `discuss_channel.py:262` comment stale — 寫「project's cloud_service_id」但 implementation 已用 `task.primary_cloud_service_id` hybrid spec (CAQA round 1 I-1) | reporter 順手清 or batch |
| F-PA-9 | low | refactor (cycle 5) | 5 | `cloud_service._do_cleanup` 沒明示 `self.ensure_one()` — multi-record 在 helper 內 raise 行為不清楚 (CAQA round 1 I-3) | small refactor PR — `cloud_service.py:1086-1101` |
| F-PA-10 | low | refactor (cycle 5) | 5 | `project_task.primary_cloud_service_id` docstring「The compute acts as a default」描述不精確 — Odoo readonly=False + computed 是 implicit default idiom (CAQA round 1 I-4) | reporter 順手清 docstring |
| F-PA-11 | low | refactor (cycle 5) | 5 | `project_project.create()` override SUPERUSER_ID skip → migration step 9 fallback to `create_uid` partner，demo / install hook project owner 可能為 OdooBot partner (CAQA round 1 I-5) | Phase B/C 評估 demo data project 預設 owner 政策 |
| F-PA-12 | low | refactor (cycle 5) | 5 | `project_access._compute_user_id` 用 `sudo().search([('partner_id', '=', X)], limit=1)` 無 deterministic ordering (CAQA round 1 I-6) | small PR — 加 `order='id ASC'` |
| F-PA-13 | medium | reviewer (cycle 5 round 1 HC-1) | 5 | `project_project._inverse_cloud_service_id` 用 `[(6, 0, [X.id])]` replace semantics — Phase A migration 後既有 1-service M:N row 沒實際 break；**Phase B/C 多 service binding active 前必修**（否則 controller `cloud_service_id = X` write silently drop 其他 binding） | Phase B/C 必修 PR — `project_project.py:88-99` + `paas.py:1594` + `ai_assistant.py:944` |
| F-PA-14 | medium | reviewer (cycle 5 round 1 HC-2) | 5 | `project.sudo().create()` (在 `ai_assistant.py:946`) bypass `project_project.create()` override 對 `SUPERUSER_ID` skip bootstrap → creator silently 無 `project_access` owner row → 對 4 個新 model 的 16 條 ir.rule 全 fail → creator **看不到自己 project** 上 agent_template / agent_session / skill / project_access；**Phase D-F use_agent_runtime active 前必修** | Phase D-F 必修 PR — `project_project.py:122` + `ai_assistant.py:946` |
| F-PA-15 | info | reviewer (cycle 5 round 2 EC-3a) | 5 | HC-3 hotfix `_check_project_ids_grantor_authority` 對 `is_superuser()` + `has_group('base.group_system')` bypass — Phase B/C 加 controller endpoint 用 `agent_template.sudo().write({'project_ids': ...})` path 不 enforce constraint，必須對齊 [[two-layer-acl-lockstep]] pattern 在 controller carve-out 補 grantor check | Phase B/C controller layer touch PR 提醒 — `agent_template.py:94-122` |
| F-PA-16 | info | reviewer (cycle 5 round 2 EC-4a) | 5 | `skill._anchor_bundle_attachment` 對既有 attachment 的 `res_model` overwrite 是 destructive — 若 admin (`base.group_system`) 把已存在的 `mail.message` attachment（`res_model='mail.message'`）assign 給 `skill.bundle_attachment_id`，anchor 把 attachment 的 `res_model` overwrite 成 `'woow_paas_platform.skill'`，切斷原 mail.message linkage；docstring 已警告但需 maintainer doc 補強 | reporter 順手 / Phase B/C bundle upload API PR — `skill.py:108-129` docstring |
| F-SK-1 | medium | refactor (cycle 6) | 6 | `_recompute_is_latest`（`src/models/skill.py:202-220`）與 migration（`src/migrations/18.0.1.0.28/post-fix-is-latest.py:19-40`）共用「`(published_at desc, id desc)` winner」演算但兩份相同副本 — 未來既有 spec follow-up「semver 精確排序」演進時若漏改其一 → fresh deploy vs upgrade deploy 行為不一致 silent drift（CAQA C1 / 同源 reviewer F1 二階 model 內部兩條路徑差異） | 抽 `Skill._compute_is_latest_winner(group_records)` classmethod model + migration 共用；或 next semver 演進 PR 內 lock-step 改兩處 + 補 invariant test 鎖兩條路徑等效 |
| F-SK-2 | medium | refactor (cycle 6) | 6 | model hook 對單筆 published create / write 即同步 search + sort + N×write 全 `(project_id, name)` group — admin 後台逐筆操作 negligible；批匯 N 個同 group → O(N²)（CAQA C2 / 同源 reviewer F2 跨 group N+1 放大） | 短期 reminder；中長期 batch upload API 出現時 hook 改 group-aware（`@api.model_create_multi` 收 distinct group → 一次性 recompute） |
| F-SK-3 | major | reviewer (cycle 6 F1) | 6 | write hook 對 `deprecated → published` re-publish 用 `if not skill.published_at` guard 不刷新 `published_at`，與 `action_publish` 一律 stamp `fields.Datetime.now()` 不一致 — 兩條路徑對同操作給不同 winner 排序 | 加 regression test `test_write_state_to_published_from_deprecated_keeps_old_published_at` 顯式鎖語意；或在 `_recompute_is_latest` docstring 明寫「兩條路徑可能對 deprecated→republish 得到不同 winner」`src/models/skill.py:125-129` |
| F-SK-4 | major | reviewer (cycle 6 F2) | 6 | write override `for skill in self: skill._recompute_is_latest(...)` 對來自 K 個不同 `(project_id, name)` group 的 N 筆 recordset 跑 N 次 search + N 次 sort（admin 多選 50 skills mass action_archive 場景）。idempotent 安全、worst-case 跨 group 放大 | dedupe group key set 後再 recompute（`published_at` 補刷必須 per-skill）`src/models/skill.py:117-130` |
| F-SK-5 | major | reviewer (cycle 6 F3) | 6 | 截圖路徑硬編 `/tmp/skill-e2e-screenshots/` — POSIX-only，Windows 不可攜；jsdoc / README 未標 caveat `tests/e2e/skill/skill-admin-form.spec.ts:34` + `tests/e2e/skill/README.md:40-44, 56-61` | jsdoc / README 標「目前僅 POSIX；Windows 需 env override」caveat，或改 `process.env.SCREENSHOT_DIR ?? path.join(os.tmpdir(), 'skill-e2e-screenshots')` 同步更新註解 |
| F-SK-6 | major | reviewer (cycle 6 F4) | 6 | README / spec 註解「leader 強調曾因此誤判 action_deprecate」口頭歸屬入庫 — 一年後讀者無從定位 `tests/e2e/skill/README.md:67` + `tests/e2e/skill/skill-admin-form.spec.ts:22-25` | 改事實陳述（「曾發生過：sandbox pod 跑舊 code 導致誤判 action_deprecate」），或刪脈絡只留 pitfall（轉 [[httpcase-orm-flush-ordering-raw-sql]] 等 doc） |
| F-SK-7 | major | reviewer (cycle 6 F5) | 6 | 測試註解硬編「PR #81」「Plan §Phase C Task 13」脆弱 anchor（平台 / 修訂 artifact 易漂移失效）`src/tests/test_skill_is_latest_drift_regression.py:5`、`src/tests/test_skill_e2e_lifecycle_download.py:16`、`src/tests/test_agent_pod_phase_c_skill_bundle.py` | 改 in-repo anchor（檔名 + class 名），如「既有 `TestSkillAdminLifecycle` 已覆蓋」；plan task 引用補 stable summary |
| F-SK-8 | major | reviewer (cycle 6 F6) | 6 | playwright `loginAdmin` `await page.fill('input[name="db"]', DB_NAME).catch(() => {})` 過寬吞錯 — selector timeout / element disabled / network crash 全被吞 → debug 錯位 `tests/e2e/skill/skill-admin-form.spec.ts:39` | narrow 到「element 不存在」：`const dbField = page.locator('input[name="db"]'); if (await dbField.count() > 0) await dbField.fill(DB_NAME);` |
| F-SK-9 | major | reviewer (cycle 6 F7) | 6 | playwright `getByText(skillName, { exact: false }).first().click()` 對同名 skill 多版本 / cross-test pollution row silent wrong-row click → 開錯 form、後續 assert 失敗訊息誤導 `tests/e2e/skill/skill-admin-form.spec.ts:52` | `SKILL_E2E_NAME` 加 unique suffix（backend test 同 pattern `uuid.uuid4().hex[:8]`），或 `exact: true` + 由 fixture 注入 record id 走 data-id selector + `assert count() === 1` 顯式守線 |
| F-DP-1 | medium | refactor (cycle 6 CAQA CAVEAT #2) | 6 | `_get_project_stages` 雙重洞：list endpoint 無 access check（read-leak） + `_ensure_default_stages` 在 list path fallback 走 `TaskType.sudo().create()` 對 attacker-supplied project_id 寫 stage（write surface）— pre-existing；`cross-workspace-task-data-leak` family 在 controller layer 剩餘 instance | dedicated design PR — `ai_assistant.py:1046, 1139-1177`；需 design「`_ensure_default_stages` 移到 explicit write endpoint」 |
| F-DP-2 | minor | refactor (cycle 6 CAQA CAVEAT #3) | 6 | ~~`api_support_stats` `accessible_projects` 走 sudo + bound-only leg 漏 unbound~~ | ✅ **CLOSED in this PR** — `ai_assistant.py:972-986` drop sudo + empty domain（變體 B under-access close）+ Task.sudo unify（變體 C same-method 半套 close, caqa option (b) recommended）；T5 endpoint test `test_project_decouple_frontend_endpoint.py:845-1014` 鎖 own-data visibility + Bob bare-stats=0 反向 false-green |
| F-DP-3 | info | refactor (cycle 6 CAQA CAVEAT #4) | 6 | `_check_project_access` 對 unbound project 走 follower-only check (`user_partner in project.message_partner_ids`)，與 ir.rule `project_rule_workspace_member` unbound clause 的 dual sub-leg（`message_partner_ids` OR `create_uid`）非對等鏡像 — D2=A 設計副效應：`sudo().create()` 後 `create_uid=SUPERUSER`，caller 不會走 create_uid sub-leg；message_subscribe 補 follower sub-leg → controller follower-only check 與「實際生效 leg」對齊；若未來 D2 切 D2=B (with_user create) 需同步動本 helper | reporter polish — `ai_assistant.py:48-55` docstring 補述「intentionally narrower than ir.rule unbound dual-leg：assumes D2=A subscribe-after-sudo-create idiom」 |
| F-DP-4 | minor | refactor (cycle 6 reviewer arch-1) | 6 | Frontend 三分支 dispatch (`cs / ws / unbound`) 在 component `onSubmit` 而非 service 層 — 本 PR scope acceptable（modal 是唯一 caller，local cohesion）；rule of three：等第 2 caller 出現再抽 `supportService.createProjectAuto({workspaceId?, cloudServiceId?, ...payload})` helper | future PR — `CreateProjectModal.js:138-147`；only-if `SupportProjectsPage` / 其他組件加 create 入口時觸發 |
| F-DP-5 | trivial | refactor test (cycle 6 reviewer M1 / comment-analyzer C1) | 6 | T1 / test 模組 docstring 引用 `ai_assistant.py:1038-1040`（dispatcher guard）+ `:1228`（sudo create）為 pre-fix 行號，post-fix 漂移 2-4 行（D1 guard 移到 :1037-1038 / D2 message_subscribe 在 :1230-1238）— test module docstring line 35-48 已用「Red expectation today (pre-implementation)」框定，可解讀但脆 | reporter polish — `test_project_decouple_frontend_endpoint.py:38,47,169-173,204,316` 改 stable function-name reference（`_create_project` / `_dispatch_project_action`）取代行號 |
| F-DP-6 | trivial | refactor test (cycle 6 reviewer M2 / comment-analyzer C2) | 6 | T3 / T4 docstring 引用 `ai_assistant.py:1186`（_list_projects） + `:1325`（_list_tasks）為 first comment line，actual call 在 :1190 / :1329（drop sudo 後的真實 search 行）— 同 M1 性質，post-fix line drift | reporter polish — `test_project_decouple_frontend_endpoint.py:366,529,579,599,763,766` 改 function-name reference（`_list_projects` / `_list_tasks`）取代行號 |
| F-DP-7 | trivial | refactor test (cycle 6 reviewer M3 / pr-test-analyzer minor) | 6 | T3 bound-project case 建立 project after 沒明確 `assertEqual(project.cloud_service_id, svc_alice)` 守 bound binding — 若未來 bound create path 退化為 unbound（如 cloud_service_id ignored），T3 subset / leak assertion 仍綠（B2 backend baseline + Playwright TC3 + T3 owner-survive 三層守已覆蓋，故非 must-fix） | reporter polish — `test_project_decouple_frontend_endpoint.py:497` 後加 1 行 `self.assertEqual(project.cloud_service_id, svc_alice)` |
| F-DP-8 | trivial | refactor (cycle 6 reviewer M4 / comment-analyzer I1) | 6 | `_list_projects` 註解寫「creator / follower sub-legs」technically 正確（對 ir.rule literal）但**沒明寫 create_uid sub-leg 在 endpoint create path 被 sudo() 戳穿**；讀者交叉讀 D2=A 註解才能補完此 cross-coupling | reporter polish — `ai_assistant.py:1186-1189` 註解末加 1 句「create_uid sub-leg defeated by sudo().create() — see _create_project D2=A comment for follower compensation」 |
| F-DP-9 | track-only (P3) | observation (cycle 6 final addendum reporter retro §5) | 6 | Variant C (same-method 半套 sudo) 是 [[orm-vs-endpoint-acl-test-coverage]] 三變體中**唯一沒有 test 抓得到**的 anti-pattern — 純靠 reviewer + pattern doc 立論一致性 audit catch。本條目追蹤未來 reviewer 1-layer audit 是否成功用 pattern doc 三變體章節抓出新出現的 variant C instance（含 false positive 也算）| open（passive observation，無 timeline；若 N 個 cycle 後無新 instance 觸發可關此 entry）— `api_support_stats:980` Task.sudo unify 是首次 close 的 variant C 反例（與 F-DP-2 同期 close）|
| F-DT-1 | minor | pre-existing (deploy-tooling #86) | — | `deploy-stg.sh` `init_odoo_system_params` 用 `db_name="woow_${slug}"`（保留 dash），與 `create.sh` `DB_NAME="woow_$(echo "$SLUG" \| sed 's/-/_/g')"`（dash→underscore）不一致 — slug 含 dash 時 #86 web.base.url upsert 連到不存在 DB（`-i` 修法後從 silent no-op 變 fail-loud abort，反而放大可見性）；預設 slug `stg` 無 dash 不觸發，故 stgfix85 e2e 刻意用無 dash slug 迴避 | 小 PR — `deploy-stg.sh:163` 改 `woow_$(echo "$slug" \| sed 's/-/_/g')` 對齊 create.sh |
| F-DT-2 | minor | pre-existing (deploy-tooling #85) | — | stg mode `create.sh` DB-init pip-wait 探針 `kubectl exec deploy/odoo -- python3 -c "import packaging"` 在 baked stg image 從不成功 → 每次 deploy 空等滿 900s 才走 fallback 手動 `pip install`（stgfix85 e2e 實測多花 ~15 分鐘） | 調查 stg image python path / 改用容器 readiness 條件；或 stg mode 縮短/跳過 pip-wait（image 已 bake 套件）— `create.sh:416-437` |
| F-DT-3 | low | reviewer (deploy-tooling #85 workflow) | — | create.sh DB-init 成功訊號靠 `odoo … 2>&1 \| grep … \| head -20` pipe exit code 而非 odoo 真 exit（half-init gate 改動後此弱訊號更常被觸發）；gate 對 `to upgrade`/`to install` 中間態走 install path 但 `odoo -i` 對非 uninstalled module 為 no-op（需 `-u`，destroy+recreate 無法 drain 中間態） | future PR — `create.sh:461-467` 用 `PIPESTATUS`；gate 中間態走 `-u` 或 README/SKILL 註明不處理中間態卡死 |
| F-SDS-1 | high | reviewer/security (PR #96 stg-deploy 複查) | — | sibling model `mcp_server` 缺 scope-flip `write()` guard — 非 admin `base.group_user` 可 raw ORM `write({'scope':'system','project_id':False})` 把自己 user-scope server flip 成平台公開（跨租戶載入），ir.rule 用 pre-write 狀態判定擋不住；endpoint 因 writable-field allowlist（`paas.py:1393`）+ create 硬編 scope='user' 安全。skeptic `refuted:false` 高信心。連帶查 `mcp_tool` 是否能 re-point 到 system-scope server | ✅ **RESOLVED**（PR #99 → develop）— `mcp_server.write()`（`mcp_server.py:134`）+ `mcp_tool.write()`（re-point guard，連帶查兌現）鏡像 `skill.py:150-167`；`test_mcp_scope_flip_guard.py` 13 test（ORM `with_user()` 負向 full-flip + 單欄位變體 + no-op 放行 + admin/sudo boundary + mcp_tool re-point〔system + 跨租戶 user 注入〕+ HttpCase endpoint allowlist 對等）**紅綠驗證**（RED：4 安全測試 `AccessError not raised`→GREEN 13/13）；完整套件 **782 test 收集、零 mcp/skill 回歸**。**完整性**：grep 確認 skill+mcp_server 為僅有的 2 個 `scope`(system/user)+`project_id` sibling，皆已 guard。pattern [[scope-flip-write-guard-not-ir-rule]] 的 sibling 兌現 |
| F-SDS-2 | low | reviewer (PR #96 stg-deploy finding #1) | — | `src/__init__.py:4` 無條件 `from . import tests` → production 每次啟動 import `tests/__init__.py`（83 modules + odoo.tests.common），stg 報告抓到非阻斷啟動 ERROR/告警。Odoo 18 test loader 自己 import `tests` package（`_get_tests_modules`），core addon（web/project/mail）根 `__init__.py` 皆 0 個 test import，移除不影響 `--test-enable` 收集（已驗證 83 modules 仍 discover）| ✅ **RESOLVED**（PR #99 → develop）— 刪 `src/__init__.py:4`（未動 `src/tests/__init__.py`）；docker-desktop sandbox 完整套件 `-u woow_paas_platform --test-enable` 實測 **782 test 仍全數收集**、模組正常載入 |
| F-SKU-1 | low | manual stg 探索 (2026-05-31) | — | Skills list view（`action_skill` / `/odoo/action-542`）skill 上傳入口在空狀態觸不到：`create="false"`（`skill_views.xml:8`）無 New 鈕 + upload wizard 走 `binding_view_types="list,form"` contextual action，Odoo 18 只掛進「選取記錄後」的 ⚙ Actions 選單（breadcrumb 齒輪僅 Export All）→ 列表為空時無法上傳第一個 skill（只能 agent-runtime API），nocontent help「No skills yet」誤導。stg playwright-cli 實測確認。| ✅ **RESOLVED**（PR #103 → develop，branch `fix/F-SKU-1`）— `view_skill_list` 加 `<header>` + `display="always"` 的「Upload Skill」按鈕（`type="object"` → `Skill.action_open_upload_wizard()` 回傳 wizard `act_window` dict）+ 改寫 nocontent help；`type="object"` 對齊 repo 既有 working precedent（`cloud_odoo_addon_views.xml:58-59`）避 list arch 同檔 action xmlid forward-ref 風險；regression test 補於 `test_skill_admin_views.py`（display='always' 存在性 + 方法 modal action）；靜態 + 4 面向對抗式 review 全 pass + **runtime 驗證通過**（docker-desktop sandbox 真 Odoo 18，`TestSkillAdminViews` 4 test 0 failed/0 error；全套件其餘 12 fail/error 為無關子系統環境敏感 baseline，正交）。issue **#102** |

下方逐條細節 + 觸發條件 + 建議落點。F1-F11 維持 cycle 1 原文。F12-F20 是 cycle 2。F21+ 是 cycle 3 (issue #48 Project Detail page)。F-X1+ 是 cycle 4 (MCP refactor URL redesign)。F-PA-1+ 是 cycle 5 (Agent runtime pod Phase A)。F-PA-13+ 是 cycle 5 round 1 reviewer 抓 CAQA 漏抓 4 HC 中 leader 拍板 defer 的 2 條（HC-1 / HC-2）+ round 2 hotfix 落地後 reviewer 獨立 walk 抽出的 2 條 edge case 提醒（EC-3a / EC-4a）。F-SK-1+ 是 cycle 6 skill-list（PR #81 follow-up — is_latest drift fix model 層 + 含瀏覽器 UI 的 e2e）。F-DP-1+ 是 cycle 6 (decouple project frontend + controller 收尾，2026-05-28；4 CAQA caveats + 4 reviewer minor polish + 1 final addendum track-only F-DP-9 variant C audit lookback)。F-DT-1+ 是 deploy-tooling fix #85/#86（dev sandbox create/destroy/deploy-stg 腳本，2026-05-29；皆 pre-existing / reviewer nit，超出修正 scope）。F-SDS-1+ 是 PR #96 stg-deploy 驗證複查（skill dual-scope 上 stg 後對抗式 workflow 抓出的 sibling/cleanup follow-up，2026-05-30）。F-SKU-1+ 是 skill upload wizard 後端 list view 空狀態無上傳入口（manual stg 探索，2026-05-31；issue #102）。

---

## F1 — ACL 對等性 checklist 持久化（doc-only）

- **Severity**: medium（不處理會在後續類似 PR 再炸 B1-class leak）
- **Status**: ✅ 已透過本批 docs 處理：
  - Pattern: [[odoo-addon-std-rule-symmetric-deactivation]]
  - Pitfall: [[cross-workspace-task-data-leak]]
  - Decision invariant: [[cloud-service-project-decouple]] §Invariants 4-5
- **下一步**：把 pattern doc 引用加進 CLAUDE.md，讓未來 dev / agent 改 ACL 時自動 surface。**部分已落地**（issue #93 追蹤）：CLAUDE.md「Known patterns」段已收錄 3 條 ACL pattern 引用 + [`docs/decisions/patterns/README.md`](../../decisions/patterns/README.md)（pattern 索引）。

---

## F2 — `_sanitize_helm_values` 同名異實重命名 / 抽 utils

- **Severity**: medium（語意混淆風險，不是 functional bug）
- **Origin**: 本次 refactor 新加的 `models/discuss_channel.py:41` `_sanitize_helm_values`（AI prompt sanitizer，pattern + blacklist + `***REDACTED***`）與 pre-existing `controllers/paas.py:1460` `_sanitize_helm_values`（API response sanitize，`secret` key 下 truncate `[:8] + '...'`）同名異實
- **影響**：未來維護者 grep `_sanitize_helm_values` 拿到兩個結果，可能誤用一個 sanitizer 在錯誤的 surface（如把 API sanitizer 用在 AI prompt 上 → 漏 password / token；或把 AI sanitizer 用在 API response → 改變既有前端 behavior）
- **建議落點**：
  - 重命名 `models/discuss_channel.py::_sanitize_helm_values` → `_sanitize_helm_values_for_ai_prompt`，並同步調整 callers
  - 或抽出共用 module-level helper（如 `src/services/helm_values_sanitizer.py`），明確兩個 mode（`for_api_response` / `for_ai_prompt`），controller / discuss_channel 都改 import
- **何時**：下次任一邊 sanitizer 邏輯需要動的 PR 順手做（不需要單獨 PR）

---

## F3 — Cross-workspace incompatible extras 仍注入 AI prompt

- **Severity**: medium（非 secret leak，但違反多租戶資訊隔離精神）
- **Origin**: 本次 refactor 對 task cross-project move 設計成「extras 保留」（AC #13），但 `_get_cloud_service_context` 走 `sudo()` 取 extras（`src/models/discuss_channel.py:267-280`），sanitizer 只去 secret 不去 cross-workspace
- **觸發路徑**：
  1. 使用者 A 在 W1 為 task 加 W1 服務 X 為 extras
  2. Task 被 cross-project move 到 W2（B 的 project）
  3. W2 member 在該 task chatter 觸發 AI 自動回覆
  4. AI prompt 內包含 X 的 service.name / subdomain（公開域名）/ template name / sanitized helm_values
- **洩漏內容**：service.name（可能含租戶業務語意）、subdomain、template name、helm_values（secret 已 redact 但保留 chart 配置）
- **建議落點**：
  - `_get_cloud_service_context` 過濾 `incompatible_extra_cloud_service_ids`，cross-workspace extras 即使存在也不注入
  - 或對 cross-workspace extras 注入時降級為「placeholder 提示 + service name only」（無 helm_values / subdomain）
- **設計待議**：兩個方向各有 UX tradeoff（過濾 = AI 不知道有 extras / placeholder = 部分資訊但複雜），由使用者決定

---

## F4 — `_check_project_access` / `_check_task_access` 對 unbound project 全放行（IDOR）

- **Severity**: medium（pre-existing IDOR；本次 refactor **放大 surface**）
- **Origin**: `src/controllers/ai_assistant.py:43-61` 對 `project.cloud_service_id IS NULL` 直接 return True，跳過所有 access check
- **Pre-refactor 情況**：unbound project 前端阻擋無法建立，路徑無實際 trigger
- **Post-refactor 放大**：D5=A 拔前端 gate，unbound project 可正常建立 → IDOR surface 浮上水面
- **可觸發 endpoint**：
  - `api_support_projects(action='get/update/delete', task_id=X)` — 對猜中 ID 的 unbound project / task 可直接 R/U/D
  - `_list_projects` / `_list_tasks` 前端不顯示 unbound（但 API endpoint 知 ID 即可操作）
- **建議落點**：
  - `_check_project_access` / `_check_task_access` 對 unbound 改檢查 `project.message_partner_ids` / `create_uid`，與 ir.rule 對齊
  - 或統一改走 ORM 路徑（`with_user(env.user).project.browse(id).check_access('write')`），讓 ir.rule 自動把關
- **何時**：下個 security audit / IDOR cleanup PR；不阻擋本 refactor merge

---

## F5 — Migration xmlid drift 防呆 assertion（deferred）

- **Severity**: medium（隱性風險）
- **Origin**: 本次 refactor reviewer cleanup #6；使用者 Q1 拍板「accept 不加本 PR」
- **問題**：`src/migrations/18.0.1.0.23/post-migrate.py:93-107` 的 3-row UPDATE 無 row-count 防呆。若 Odoo 18.x 某 patch 改 `task_visibility_rule` 等 xmlid，UPDATE 命中 < 3 rows，rowcount 仍 success，B1 leak 靜默回來
- **建議落點**：
  ```python
  cr.execute("""
      SELECT COUNT(*) FROM ir_model_data
      WHERE module='project' AND model='ir.rule'
        AND name IN ('project_public_members_rule',
                     'task_visibility_rule',
                     'task_visibility_rule_project_user')
  """)
  actual = cr.fetchone()[0]
  if actual != 3:
      raise RuntimeError(
          f"Odoo std rule xmlid drift detected: found {actual}, expected 3. "
          "B1 leak might silently return — abort migration. "
          "See docs/tracking/pitfalls/cross-workspace-task-data-leak.md"
      )
  ```
- **何時**：下次 migration touch 或 Odoo 版本升級前必加
- **替代防線**：Pre-deploy verify SQL（已寫進 PR description 的 deploy checklist + [[odoo-addon-std-rule-symmetric-deactivation]]）— 但靠手動跑

---

## F6 — D9 階段 2：sanitizer 升級 whitelist 模式

- **Severity**: medium（pattern 比 whitelist 鬆，但本次先 ship pattern 是對的 — 詳見 [[cloud-service-project-decouple]] §7）
- **Origin**: plan D9 拍板「A → B」，本次只做 A
- **觸發條件**：
  - 階段 1 telemetry 累積夠 dataset（哪些 keys 實際出現在 helm_values、哪些被 redact）
  - Pen-test / security audit 標示「pattern 漏網」實例
  - 或新 chart 上線 PR review 時發現需要更嚴的策略
- **建議落點**：
  - 新 helper `_sanitize_helm_values_whitelist(helm_values_json, allowlist)`，allowlist 由 chart-specific 配置驅動
  - `_sanitize_obj` 改為「key 不在 allowlist → 整 value redact」而非「key 匹配 pattern → redact」
  - 過渡期可雙模式並存，A 與 B 對同一 input 跑後 log diff 找出 pattern 漏網案例
- **何時**：階段 1 已 ship 至少 N 個月 + 有 telemetry 數據後

---

## F7 — BottomNav mobile vs Sidebar D4 拆分後 UX 一致性

- **Severity**: minor（PaaS 主要 desktop）
- **Origin**: `src/static/src/paas/layout/bottom_nav/BottomNav.js:18-24` mobile bottom nav 仍把 `ai-projects` / `ai-project-kanban` 算進 AI 入口；desktop sidebar 已拆出獨立 `Projects` entry
- **症狀**：mobile 上點到 Project Kanban 會 highlight "AI" tab
- **建議**：BottomNav 加 `Projects` entry 對齊 Sidebar；或把 mobile AI tab 內 sub-menu 加上 Projects
- **何時**：mobile UX 下次 sprint，或 D4 sidebar 確認最終 UX 形態時一起調整

---

## F8 — Test hardening

- **Severity**: minor（implementation 都對；test 是「future regression 強守線」缺塊）
- **Origin**: pr-test-analyzer + reviewer R7 / R8
- **缺塊**：
  - **AC #11 domain test 是 string-grep stub**：只 grep `'workspace_id'` literal 出現於 `field.domain.repr()`，未跑真實 ORM filter — 未來 implementation 用 `[('workspace_id', '!=', False)]` 之類無意義 domain 也能過關
  - **AC #14 dedup test format-brittle**：用 `count('Primary Svc') == 1` 偵測 dedup，prompt 模板改字（如 `### Primary Service`）可能繞過 — implementation 用 `extras - primary` recordset 層級 dedup 是對的，但 test 不擋未來 implementation 改成字串層級操作
  - **B1 regression 沒涵蓋 `group_project_user` write/unlink leg via `task_visibility_rule_project_user`**：PaaS 不給租戶 group_project_user，但 paranoia 加 1 條 sibling test 防 future role grant
  - **AC #15 sanitizer 深度 >5 / 非 JSON helm_values fail-closed path 未被測**：implementation `_sanitize_helm_values` 已 fail-close 為 `''`，無 test 守
- **建議**：每條對應加 1 條 test。落在下個 touch testing 的 PR

---

## F9 — Silent fallback observability

- **Severity**: minor（不破功能，但 silent fallback 影響 debug 與 incident 反查）
- **Origin**: silent-failure-hunter R9
- **缺塊**：
  - `_sanitize_helm_values`（`src/models/discuss_channel.py:41-49`）JSON parse 失敗 silently 回 `''`，無 log。Helm chart 把 `helm_values` 存 non-JSON 時 AI 拿不到 context，使用者完全不知為什麼
  - `_sanitize_obj` depth > 5 silently 回 `_REDACTED` literal，與真實 redacted value 無辨識
  - `CreateProjectModal._loadCloudServices` catch 後沒 set `state.error`（不一致於 `_loadWorkspaces`），dropdown 靜默變空使用者誤判「無 service」
  - `_post_ai_reply` 外層 except 把 programming error 跟 transient AI failure 包成同一個 generic notification — 影響 incident triage
- **建議**：逐條加 `_logger.warning` + observable signal（log level 降到 warning 足夠，不是錯誤）
- **何時**：observability 下次 sprint 或下個 touch 對應檔案的 PR

---

## F10 — Pre-existing `_list_projects` with sudo + empty domain 跨 tenant 列

- **Severity**: minor（pre-existing 不在本 refactor 引入）
- **Origin**: reviewer 偽陽性 verification 順帶發現
- **狀況**：`_list_projects` 在某 endpoint 用 `domain=[]` + `sudo()` browse → 跨 tenant 列所有 project；前端 `SupportProjectsPage` 呼叫 `fetchAllProjects()` 走無 workspace 過濾的 endpoint
- **與本次 refactor 的關係**：D5=A 拔前端阻擋後**放大可見度**（以前看不到的 unbound project 現在會出現在頁面），但 leak shape 是 pre-existing 的 controller-level filter gap，不是本次 refactor 引入
- **建議**：`_list_projects` 改走 ORM 路徑用 `with_user(env.user)`，依靠 ir.rule（已收緊）自動隔離 — 一致於 [[paas-workspace-member-acl-tightening]] 的整體方針
- **何時**：與 F4 IDOR cleanup 同一個 PR 處理較自然（都是 ai_assistant.py controller）

---

## F11 — Sentinel `***REDACTED***` i18n 化

- **Severity**: minor（純 audit token）
- **Origin**: plan §Open Q4 / PR description follow-up
- **判斷**：本次拍板 hardcode `***REDACTED***` 是對的（[[cloud-service-project-decouple]] §9 Q4）— 它是 audit grep-able token，不是 user-facing 文案
- **何時觸發**：若 F6（D9 階段 2 whitelist）執行時順帶評估是否需要 i18n（whitelist 模式可能會把 redacted value 暴露給更多 surface）
- **建議**：F6 PR 內一併評估；若決定 i18n 化，要同步改 plan / test / pitfall doc 內所有 `***REDACTED***` literal 引用

---

---

# Cycle 2 — MCP `cloud_service_id → project_id` Decouple (2026-05-18)

> Cycle 2 完成於 2026-05-18，commit chain `5c5d980 → 9f147d8`。Reviewer round 2 APPROVED WITH CAVEATS。新 F12-F20 條目來自 CAQA C1 / F12 + round 1 verdict 17 minor + round 2 verdict 4 new minor + reporter durable docs 寫作時順帶整理。

## F12 — 控制器 `helm_values` 漏 sanitize（pre-existing，違反 cycle 1 Inv #6）

- **Severity**: medium
- **Origin**: CAQA `docs/decisions/reviews/caqa-2026-05-18.md §F12` out-of-scope finding（CAQA 跨 cycle 1 + cycle 2 漏抓兩次）
- **Location**: `src/controllers/ai_assistant.py:609-610` — SSE streaming path 注入 `service.helm_values` 到 AI system prompt **未走 `_sanitize_helm_values`**；對比 `src/models/discuss_channel.py:303` non-streaming path 有 sanitize
- **Violation**: cycle 1 Invariant #6（AI prompt 注入 `helm_values` 必須經 `_sanitize_helm_values`）
- **為何沒在本次擋下**：本次 implementation diff 未改此段 code，無新增 surface。本次 verdict 評估「是否引入 / 加劇」— pre-existing 不擋
- **觸發路徑**：user 開啟 SSE chat 且 task 連到 bound project + `chat_enabled=True` + `ai_auto_reply=True` 才會 fire（非任何 user 隨意觸發）
- **建議落點**：
  - 路徑 1：與 F2（同名異實重命名 / 抽 utils）合併處理，徹底解決
  - 路徑 2：若評估 AI chat 已上 stg / 即將 prod，獨立 hotfix PR 立刻修（機械式 1-line fix：`service.helm_values` → `_sanitize_helm_values(service.helm_values)` 並 import）
- **何時**：next AI chat 相關 PR 一併處理

## F13 — B1 task ACL deactivation 對齊 `18.0.1.0.23` pattern（next refactor cycle `18.0.1.0.25`）

- **Severity**: medium
- **Origin**: round 2 verdict 42 reds baseline survey（commit 9f147d8 §Out-of-scope 42 reds）
- **狀況**：`test_cross_workspace_task_isolation` AC4 與 `test_project_decouple` 在 round 2 baseline 仍 red — failure pattern 是 B1 task regression，root cause 在 migrations `18.0.1.0.22/0.23` std-rule deactivation timing，與本次 cycle 2 無關
- **下個 cycle 動作**：建立 `src/migrations/18.0.1.0.25/post-migrate.py` 對齊 `18.0.1.0.23` project_public_members_rule pattern — single UPDATE + IN clause deactivate task std rules
- **不擋 cycle 2**：reviewer round 2 已驗 baseline survey 方法學健全（git stash → re-run baseline → 同 reds reproduce），cycle 2 commit range 內 0 改動這些 file → 無 blast radius
- **何時**：next refactor cycle，把這 42 reds 收尾

## F14 — Cleanup hook `mapped` 路徑 belt-and-braces `sudo()`（CAQA C1）

- **Severity**: medium
- **Origin**: CAQA `docs/decisions/reviews/caqa-2026-05-18.md §C1`
- **Location**: `src/models/cloud_service.py:1134-1136` + `:1192-1194`（`_do_cleanup` step 1 + `unlink()` override）
- **現狀**：
  ```python
  user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
      lambda s: s.scope == 'user'
  )
  if user_mcps:
      user_mcps.sudo().unlink()
  ```
  `mapped('mcp_server_ids')` 走 ir.rule（如果 caller context 不是 sudo）— 目前 caller 都是 cron / subscription_lifecycle_mixin / controller 直 unlink，預期都是 sudo / admin context，但 `_do_cleanup` 與 `unlink()` 都是 public method
- **風險**：未來 callable surface 擴大（如 non-sudo caller）+ user 對某些 user-scope MCP 無 read 權限（cross-workspace edge case） → `mapped` 漏掉那些 row → 沒 unlink → 變 zombie
- **建議落點**：
  - A：`self.project_ids.mapped('mcp_server_ids').sudo().filtered(...).unlink()` — sudo mapped 結果
  - B：`self.sudo().project_ids.mapped('mcp_server_ids').filtered(...).sudo().unlink()` — sudo 整條 walk
- **何時**：next touch `cloud_service.py` cleanup path PR 順手做

## F15 — `_do_cleanup` retry-on-error semantics 文件化（R2-1）

- **Severity**: info（doc invariant only，無 code change）
- **Origin**: round 2 verdict R2-1（code-reviewer 提的 info caveat）
- **狀況**：subscription cron 在 cancel 後再 trigger `_do_cleanup` 是 retry-friendly 設計（state='error' 不擋）— 後人若加 `if state == 'error': return` short-circuit guard 會打破 auto-retry
- **建議落點**：
  - 在 [[mcp-project-decouple]] §Invariants 新增「`_do_cleanup` 必須 re-entry safe — `state='error'` 不該 short-circuit return」一條（已部分於 cycle 2 finalize 補 — see Inv #4 helm fail state ordering description）
  - 在 `cloud_service.py:1119` docstring 加 retry note
- **何時**：reporter inline 處理（cycle 2 完成）— 或下個 touch `_do_cleanup` PR

## F16 — `mcp_server` / `mcp_tool` rule-count parity lint check（R2-2）

- **Severity**: info
- **Origin**: round 2 verdict R2-2（code-reviewer 提）
- **狀況**：無 automated lint 檢查 `mcp_server` / `mcp_tool` rule-count parity。依賴人眼 / [[mcp-project-decouple]] §Invariant #5 doc。任何後人加第 4 對 rule 漏對等 = B1-style sub-model leak
- **建議落點**：
  - A：post-migrate 加 invariant assertion — `SELECT COUNT(*) FROM ir_rule WHERE model_id IN (mcp_server, mcp_tool) GROUP BY model_id` — 兩個 count 不等 raise
  - B：CI lint script grep `ir_rules.xml` 內 mcp_server vs mcp_tool record count
  - C：dev-only `make check-mcp-rule-parity` target
- **何時**：next hardening sprint / next ir_rules.xml touch PR

## F17 — `state='error'` re-entry test case（R2-3）

- **Severity**: minor
- **Origin**: round 2 verdict R2-3（pr-test-analyzer 提）
- **狀況**：`_do_cleanup` 在 `state='error'` 時 re-entry 行為未鎖 test — 後人若加 short-circuit guard（見 F15）會打破 retry，無 regression lock
- **建議落點**：`src/tests/test_mcp_project_lifecycle.py` 加 case：
  ```python
  def test_do_cleanup_reentry_after_error_state(self):
      """If _do_cleanup left state='error' (helm uninstall fail),
      a subsequent _do_cleanup call must retry (not short-circuit)."""
      service = self._make_service_in_error_state(...)
      # _do_cleanup again should retry helm uninstall, not no-op
      with patch(...) as mock_helm:
          service._do_cleanup()
          mock_helm.assert_called_once()
  ```
- **何時**：next test hardening PR

## F18 — `_create_admin_user` helper docstring 過 cautious（R2-4）

- **Severity**: trivial
- **Origin**: round 2 verdict R2-4（pr-test-analyzer 提）
- **狀況**：`_create_admin_user` helper 的 docstring 對 `(4, ...)` stale-cache 行為的 claim 是 unverified Odoo 民俗，但實際用 `(6, 0, ...)` 是 idiomatic — 無害 over-cautious
- **建議落點**：clean docstring，移除 unverified Odoo 民俗 claim，保留「use `(6, 0, ...)` 的 idiomatic reason」就好
- **何時**：reporter 順手清 or 留下個 touch 順手做（trivial — 不擋任何 PR）

## F19 — Round 1 17 minor / info 集合（cross-reference round 1 verdict）

- **Severity**: minor / info（mixed）
- **Origin**: `docs/decisions/reviews/pr-review-verdict-mcp-2026-05-18.md §Minor / Info`（commit `e9a5927`）— round 1 reviewer 5-agent 平行 review 提的 17 條 minor，本 verdict 不擋 round 2 approval
- **內容 cluster**（不在本 doc 重複列每條，避免 ephemeral 雜訊）：
  - **Silent failure** (silent-failure-hunter H2/H3/M1-M5)：cleanup step 1 unlink 無 try/except、SSE streaming MCP fail silent fallback、migration pre-flight assertion 缺失、cron retry 無 user notification — 7 條
  - **Type design** (type-design-analyzer F1/F5/F6/F7)：D4 partial SQL CHECK 雙保險、CSV 1,1,1,1 可緊縮、`auto_created` write protection、`scope` 二元假設 — 4 條
  - **Test coverage hole** (pr-test-analyzer)：archived project + user-scope MCP 場景、localhost URL fallback 場景、`_simulate_auto_create` helper drift — 3 條
  - **Code clarity** (code-reviewer)：Controller "MCP Server not found" 對 system-scope misleading、`self.project_ids[:1]` 非確定性 ordering — 2 條
  - **CAQA typo**：21 AC → 25 AC — ✅ DONE（reporter phase 已修）
- **建議落點**：
  - 多數 silent-failure cluster 等 F9 observability sprint 一併 batch
  - Type design 等 next mcp_server.py touch PR 順手評估
  - Test coverage 跟 F8 batch
  - Code clarity 等 next paas.py touch PR 順手做
- **何時**：分散 batch，不獨立 PR

## F20 — Controller manual check helper + `_get_user_mcp_tools` mixin abstraction（/simplify）

- **Severity**: minor
- **Origin**: round 1 verdict §`/simplify` 後是否還有可簡化處
- **狀況**：
  - Controller manual check pattern `if not server.exists() or server.project_id.cloud_service_id.id != service.id` 在 `paas.py:1335/1353/1366/1384` 重複 4 次 — 可抽 `_check_mcp_server_belongs_to_service(server, service)` helper（與 F19 minor #12 一併）
  - `_get_user_mcp_tools` 在 `discuss_channel.py:310` 與 `_get_user_mcp_tools_for_channel` 在 `ai_assistant.py:614` chain walk 邏輯一樣，可抽 mixin function。Controller path 自己重寫一遍是 cursor invalidation 考量（line 415 註解有提及）— **可不簡化，但 surface 此 duplication 給未來 maintainer**
- **建議落點**：next touch 對應 file 的 PR 順手做。Test #26（AC #14）已是 behavioural lock，helper-wrap-safe（helper 內部走舊鏈仍會被抓）— 抽 helper 不會破測試
- **何時**：next paas.py / ai_assistant.py 整理 PR

---

---

# Cycle 3 — Project Detail Page (Issue #48, 2026-05-20)

> Cycle 3 完成於 2026-05-20，final commit `32cf414`。Reviewer `approved with caveats`。R-1（doc dead refs）+ R-2（BasicInfoSection seed sanitize）由 reporter phase 收（commit pending，leader 統一 amend）。其餘 caveats 進 follow-up issue / 既有 #49 #50 #51。本 cycle 新加 F21-F28。OR-* 編號對應 spec `docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md` §Open risks，R-* 編號對應 reviewer report `docs/tracking/reviewer-report-2026-05-20-project-detail-page-issue-48.md`。

## F21 — URL pattern 統一 + service-centric MCP endpoint deprecation（OR-1 / OR-6）

- **Severity**: medium（架構 debt，不擋 ship）
- **Origin**: spec §Open risks OR-1 + OR-6；CAQA 確認；reviewer follow-up alignment
- **OR-1**：本 cycle 新增 `/api/support-projects/<id>/mcp-servers` project-centric endpoint，但舊 service-centric MCP endpoint（`/api/cloud-services/<sid>/mcp-servers`）仍保留為 dead code。需要 deprecate 流程（grep call sites → 移除前端 / 後端引用 → drop endpoint）
- **OR-6**：URL pattern 不一致 — 部分 endpoint 用 `/api/workspaces/<wid>/...` nesting，部分用 root-level `/api/support-projects/<pid>/...`。建議全部統一到 `/api/workspaces` nesting
- **Status**: ✓ 已 surface 在 gitea #50（合併 OR-1 + OR-6）
- **何時**：#50 排程

## F22 — D5 invariant 重設計 cluster（OR-2 / OR-4 / OR-5 / R-4）

- **Severity**: medium（D5 是物理現實 invariant，目前靠 multi-layer enforcement，不夠收斂）
- **Origin**: spec §Open risks OR-2/4/5；reviewer R-4
- **D5 invariant**: user-scope MCP 必須掛 bound project（`project.cloud_service_id IS NOT NULL`）。目前 enforcement 分散三層：
  - **Endpoint manual query**（rebind / unbind 走 controller 手動 query 擋）— `paas.py` `_update_support_project` D5 leg（OR-4）
  - **ORM 沒 `@api.constrains`**（`project.project.cloud_service_id` 沒 model-layer enforcement，靠 controller / 前端 gate）— R-4
  - **Rebind race condition**（同時 rebind + create user MCP 可能繞過 D5）— OR-2
  - **Sidecar auto-create + URL 機制**（D5 violation 後的 sidecar lifecycle 沒設計完）— OR-5
- **Status**: ✓ 已 surface 在 gitea #49（合併 OR-2 / OR-4 / OR-5 / R-4 一次重設計）
- **建議**：#49 同 PR 內考慮加 `@api.constrains('cloud_service_id')` model-layer enforcement，把 D5 收斂到 ORM 層，endpoint manual query 變 redundant 可刪
- **何時**：#49 排程

## F23 — Unbound project ACL — 缺防 enumeration / spec gap（OR-7 / R-4 partial）

- **Severity**: medium（pre-existing IDOR 性質，與 F4 同源 — F4 是 ai_assistant.py controller，本條是 paas.py support-projects endpoint）
- **Origin**: spec §Open risks OR-7（spec deferred 不測 unbound）；CAQA §C-3b；reviewer
- **狀況**：endpoint `/api/support-projects/<id>` 對 unbound project（`cloud_service_id IS NULL`）路徑走 OR-7 carve-out 給 `_SyntheticAccess(ROLE_OWNER)`，**沒有**統一防 enum 訊息層（與 bound non-member 路徑不對稱）。任意 internal user brute-force id 都拿 ROLE_OWNER → 可讀整 payload + update name/description
- **OR-7 三選項**：
  - A：creator-only carve-out（`create_uid == user.id OR user.partner_id in message_partner_ids`）
  - B：unbound 一律走防 enum 訊息（與 bound non-member 一致）
  - C：unbound project 廢除（要求 user 必先 bind cloud service）
- **Status**: ✓ 已 surface 在 gitea #51（OR-7 ACL spec gap）+ partial 重疊到 #49（D5 重設計時一併）
- **何時**：#51 排程，與 F4 同一 PR 處理較自然

## F24 — Reporter phase 已落的 doc work（OR-3 / OR-8 / OR-9）

- **Severity**: trivial（doc-only）
- **Origin**: reviewer follow-up alignment 標 reporter phase
- **Status**: ✅ 本批 reporter phase 已落
- **明細**：
  - **OR-3** — issue #48 comment 澄清 D6 status carve-out（碰到 issue 字面誤會「Status 欄位」一事）：已在 reporter phase 之前 post on gitea #48 comment
  - **OR-8** — worktree env setup gaps：sub-(a) `hooks.py` `pycryptodomex` import 已在本 cycle implementation 內補；sub-(b) `PAAS_CI_ADDONS_PATH` / `requirements.txt` pre-install 等 dev env gap 屬 worktree-sync skill / dev script 範疇，本 cycle 不展開
  - **OR-9** — declaration data ↔ migration 1:1 對稱 hardening：本 cycle B-1 fresh-install 重蹈 task pair pattern 的根因；已補進 [[cross-workspace-task-data-leak]] §Hardening `declaration-data ↔ migration 1:1 對稱`
- **何時**：✅ 完成

## F25 — Frontend UX hardening + error handling cluster（R-3 / R-6 / E2E-G1）

- **Severity**: important+（UX silent failure / data-loading gap — issue #48 是 UI page，UI 功能 regression / silent failure 是主功能 regression）
- **Origin**: reviewer R-3 + R-6（silent-failure-hunter critical NEW + important）+ reviewer AC #28 E2E 2026-05-20 step 11 FAIL（E2E-G1）
- **R-3 細節**：`McpServersSection.js:136-161` 三個 method 都用 `if (result.success)` 沒 `else`。Backend 回 `{success: false}` 時 UI 不 surface error（最嚴重 deleteServer：row 仍在 UI 上但 user 以為已刪）。Real surface 場景：sync / test 在 sidecar 斷時 fall-through 至此
- **R-6 細節**：`_delete_project_mcp_server`（`paas.py:1653`）沒 try/except 包 unlink；`_update_project_mcp_server`（L1638）+ `_update_support_project`（L1504）沒 savepoint（vs `_create_project_mcp_server` 有 savepoint）
- **E2E-G1 細節**：`CloudServiceBindingSection.js` `loadAvailableServices` 對 unbound project（`workspace_id == null`）沒 fetch services → dropdown 只剩 "Unbound" 選項，無法在 Project Detail 頁完成 rebind from unbound state。Workaround：Create modal 可指定 `cloud_service`；Same-session unbind 後可走通（cached），reload 或從 list 頁進來踩到
- **Status**: Follow-up issue: #52 (已 post on gitea, 涵蓋 R-3 + R-6 + E2E-G1)
- **建議 fix direction (E2E-G1)**：A：`loadAvailableServices` 對 `workspace_id == null` fall-back fetch all-accessible-workspaces 同 Create modal 行為；B：UI 顯示 disabled dropdown + 提示文案
- **何時**：下一個 sprint 處理（leader 已 post issue #52）

## F26 — Polish cluster (R-5 / R-9 / R-10)

- **Severity**: minor（style / behavior polish，不擋 ship）
- **Origin**: reviewer R-5 / R-9 / R-10
- **明細**：
  - **R-5** — `project.project.workspace_id` 是 compute field 沒 `readonly=True`：silent write 不報錯但無效，misleading（type-design-analyzer）。1-line fix
  - **R-9** — `support_service.js:469-471 + 487-490` `updateProject` optimistic merge 在 `try` 內、`finally clear loading`：若 merge throw 會 stuck spinner（code-reviewer）。1-2 line fix
  - **R-10** — `ir_rules.xml:217-238` comment block 英文（CLAUDE.md 要求 zh-tw），其他相鄰 rule 都用 zh-tw（code-reviewer）。Style polish
- **建議落點**：新 polish issue「project-detail page polish — post-merge maintainability sweep」（reviewer report Soft caveat tracked 表已建議標題）
- **何時**：next paas.py / project.project / ir_rules.xml touch PR 順手做，或單獨 polish issue

## F27 — Test gaps G1 / G3 / G6（R-7）

- **Severity**: minor（implementation 都對；test 是「future regression 強守線」缺塊）
- **Origin**: reviewer R-7（pr-test-analyzer）
- **缺塊**：
  - **G1** — `update` action 的 whitelist scope / `project_id` 寫入是否被 reject 未測
  - **G3** — `sync` / `test` action 的完整 ACL matrix（owner / admin / member / non-member × success / fail）不完整
  - **G6** — XSS sanitize 在 description 寫入 + 讀取雙路徑未被 test 守
- **建議**：每條對應加 1-2 條 test
- **何時**：next test hardening PR；可與 cycle 1 F8 + cycle 2 F17 一併 batch

## F28 — Description rich-text editor 升級（long-term，R-8）

- **Severity**: long-term architectural
- **Origin**: reviewer R-8
- **狀況**：本 cycle `BasicInfoSection` 用 `document.execCommand("bold"/"italic"/...)` 實作最小 rich-text。API legacy，瀏覽器仍支援但 spec 已 deprecated。產品演進到需要 more 編輯能力（如 mention / embed / collaborative editing）時，需要切到 ProseMirror / TipTap / Lexical 之類正式 rich-text framework
- **與本 cycle 的關係**：本 cycle ship 的 execCommand 寫法**沒有錯**（minimum viable rich-text，符合 issue #48 scope），但記在 follow-up 為了告訴未來 maintainer「進一步功能需求要 framework 切換」
- **何時**：產品需求變寬時開 architectural issue 評估

---

---

# Cycle 4 — MCP refactor URL redesign + unbound project ACL (Issue #49 + #51, 2026-05-21)

> Cycle 4 完成於 2026-05-21，PR 預計 merge to develop。Reviewer round 2 APPROVED WITH CAVEATS — HC-1 / HC-2 / HC-3 三條 hard caveat 全 closed in PR；1 條 new MINOR (SC-6b)；20 條 soft caveat 分散處理。F22 + F23 **RESOLVED**（cycle 4 收尾，issue #49 + #51 closed）。本 cycle 新加 F-X1~F-X8 + SC-cluster batch。

## F-X1 — Cron `_cron_retry_mcp_sync` 對新 sidecar-backed row gap (CAQA M1)

- **Severity**: medium（UX caveat，非 security / data loss）
- **Origin**: CAQA `docs/tracking/caqa-report-2026-05-20-mcp-refactor.md` M1 + impl finding #4 + reviewer SC-3
- **Location**: `src/models/mcp_server.py:214-217`
- **狀況**：cron 仍 `search([('auto_created', '=', True), ('state', '=', 'draft')])`，行為差異：
  - **Legacy row**（PR #47 既有 `auto_created=True AND sidecar_backed=True`）→ cron 仍 retry sync ✓
  - **新 sidecar-backed row**（cycle 4 UI shortcut 創建，`auto_created=False AND sidecar_backed=True`）→ cron **不會** retry ✗
  - **External row**（user 自填 URL）→ cron 不掃（design intent — external 由 user 自管理）✓
- **風險**：sidecar K8s service / Cloudflare route 啟動可能 lag → user 點 button 後 first sync 失敗 → user 必須手動 retry（比 PR #47 auto-create + cron retry 雙保險差）
- **建議落點**：cron query 改 `sidecar_backed=True` 或廢除 cron（user UI 已有 sync button）
- **何時**：dedicated PR；F-X7 等此 PR 完成後 drop `auto_created` column 才安全

## F-X2 — `_check_project_access` workspace_id NULL edge case + paas.py drift cluster (CAQA M2 + SC-6b + F4 / F10)

- **Severity**: medium（pre-existing + cycle 4 silent 擴大 surface）
- **Origin**: CAQA M2 + reviewer SC-6 + reviewer round 2 NM-R1 (SC-6b) + cycle 1 F4 + cycle 1 F10
- **三個獨立但同 controller IDOR cleanup PR scope**：
  1. **`ai_assistant.py:_check_project_access:54-55`** — `if not project.cloud_service_id.workspace_id: return True` blanket allow（pre-existing，本 PR 不擴大 / 不收緊；實際 PaaS 平台幾乎不會發生 bound service 沒 workspace_id）
  2. **`paas.py:_resolve_project_access:1510-1515`** — `if not workspace:` 同 treatment 涵蓋 2 case：(i) unbound project（follower check ✓，HC-2 hotfix）；(ii) bound-but-no-workspace（**同樣 follower check**，silent 收緊比 ai_assistant.py 嚴）→ `_SyntheticAccess` docstring「lock-step with ai_assistant.py」承諾 drift（reviewer round 2 NM-R1）
  3. **`_check_task_access`** (F4 task path) + **`_list_projects`** (F10 list path) — cycle 1 留 follow-up，cycle 4 plan AC #43 明文 out of scope
- **解法建議**（reporter 階段 surface 三選一）：
  - (a) paas.py 拆 case：`if not project.cloud_service_id:` 走 follower check，`if not project.cloud_service_id.workspace_id:` 與 ai_assistant.py 對齊 blanket allow（最簡）
  - (b) ai_assistant.py 同步收緊 bound-no-workspace（最嚴，security 最強）
  - (c) docstring 補一句 caveat 明示「lock-step only for unbound; bound-no-workspace edge case left blanket-allow in ai_assistant.py per CAQA M2 follow-up」（最弱、doc-only）
- **建議落點**：cluster PR 統一處理 `ai_assistant.py` controller 整體 IDOR — F4 + F10 + F-X2 三者**同一 controller / 同一 attack surface 概念**，分批改 surface 容易再 drift
- **何時**：next security audit / IDOR cleanup PR

## F-X3 — Codebase-wide comment ref noise cleanup (CAQA L1 + reviewer SC-19)

- **Severity**: low（maintenance debt，非本 PR 引入）
- **Origin**: CAQA L1 + impl /simplify pass surface + reviewer SC-19 confirm
- **狀況**：4 處 production code 殘留過去 PR（PR #47 / PR #53）的 plan/OQ/CAQA refs：
  - `ir_rules.xml:251`「CAQA B-1 (re-loop 2026-05-20)」— PR #53 留下
  - `support_service.js:454`「Plan AC #17 / #21」— PR #53 留下
  - `McpServersTab.js:12`「Plan AC #22 / #24」— PR #47 / #53 留下
  - `html_sanitize.js:26`「CAQA C-3, re-loop 2026-05-20」— PR #53 留下
- **狀況補充**：本 PR 已 verify implementation /simplify pass 對 cycle 4 自己加的 plan/OQ refs 已清；tests/ 內可能更多但 reviewer SC-19 不要求清測試
- **建議落點**：codebase-wide grep cleanup — batch with cycle 2 F19 「code clarity」cluster
- **何時**：next polish PR / 順手清

## F-X4 — URL helper duplication 收乾 (CAQA L2 + plan OR-X8 + reviewer SC-13)

- **Severity**: low（plan 明文 defer）
- **Origin**: plan OR-X8 + CAQA L2 + reviewer SC-13（code-reviewer grep verify `_model_build_mcp_endpoint_url` no caller in `src/`）
- **狀況**：兩 method algorithm 完全重複（cluster.local / subdomain / localhost fallback）：
  - `cloud_service.py:_model_build_mcp_endpoint_url` (model side) — PR #47 留下；給 model context 用（cron / non-request caller）— **code-reviewer grep 顯示無 caller**，可能 dead code
  - `paas.py:_compute_sidecar_url_hint` (controller side) — cycle 4 改名（從 `_build_mcp_endpoint_url`）；給 controller context 用（sidecar-hint endpoint）
- **建議落點**（兩種路徑）：
  - (a) verify `_model_build_mcp_endpoint_url` 真的 dead code → 刪；controller side 改抽 module-level helper 共用
  - (b) 保留兩個 method 但抽 algorithm 到共用 helper（`_compute_mcp_url(release, namespace, port, path, subdomain, paas_domain)` module function），兩 method 都呼叫
- **何時**：next paas.py / cloud_service.py touch PR；可獨立做也可 batch with F-X3

## F-X5 — SSRF error message IPv4 vs IPv6 format 微不一致 (CAQA L3)

- **Severity**: low（不影響 security）
- **Origin**: CAQA L3 + reviewer SC-20
- **Location**: `src/controllers/paas.py:_validate_mcp_url:60, 64`
- **狀況**：IPv6 用 `[{host}]` 格式（line 60, 64），IPv4 用 raw host — 一致性微差
- **建議**：統一格式（如統一用 `repr(host)` 或統一加 brackets）
- **何時**：next paas.py touch PR / 順手清

## F-X6 — `test_mcp_d5_removal.py` 註解 stale (CAQA L4 + reviewer SC-10)

- **Severity**: trivial
- **Origin**: CAQA L4 + comment-analyzer M-R5 SC-10 confirm
- **Location**: `src/tests/test_mcp_d5_removal.py:194-200`
- **狀況**：`test_d4_user_scope_requires_project_id_after_refactor` 註解寫「Note: today the D5 constraint method is also live」但 post-impl D5 已拔 — 註解 slight stale；同檔 `test_project_mcp_project_centric.py:21-28` stale module docstring 引用已刪 `TestProjectMcpD5OrmConstraint`（reviewer SC-12）也屬於此 cluster
- **建議落點**：reporter 順手清 or batch with F-X3 codebase-wide cleanup
- **何時**：reporter phase / 順手 / batch

## F-X7 — OQ-2 (b) keep deprecated `auto_created` 後續 column drop (deferred)

- **Severity**: medium（schema cleanup debt）
- **Origin**: cycle 4 implementation phase + OQ-2 leader 拍板 (b) keep deprecated
- **狀況**：cycle 4 implementation phase grep `auto_created` 顯示 10+ regression test 引用 → leader 拍板 (b) keep deprecated（rename 會炸）；migration `18.0.1.0.25` 只 UPDATE backfill 不 DROP COLUMN；field 標 `string='Auto Created (deprecated)'` + help `'Deprecated; use sidecar_backed for new code paths'`
- **依賴**：F-X1（cron query 改 `sidecar_backed`）必須先完成
- **後續動作**：
  1. F-X1 cron PR merge 後 → grep cleanup：把 test fixture / 任何 production code 對 `auto_created` 的 reference 改 `sidecar_backed` 或刪
  2. 寫 migration `18.0.1.0.<future>/post-migrate.py`：`ALTER TABLE woow_paas_platform_mcp_server DROP COLUMN IF EXISTS auto_created`
  3. `mcp_server.py` field 定義刪除
- **何時**：F-X1 完成後的 next cleanup PR

## F-X8 — `scripts/test-addon.sh` worktree env setup gap (tester surface)

- **Status**: ✅ OBSOLETE（2026-06-13，`chore/retire-docker-compose-dev-env`）—— `scripts/test-addon.sh` 連同整條 docker-compose worktree dev 路徑已退役（k8s-dev-sandbox plugin 取代）。本 gap（PAAS_CI_ADDONS_PATH override / requirements pre-install / 共用 PG 切換）在 plugin 流程已不適用：第三方相依改由 `.sandbox.yml extra_addons ${VAR}` 解析掛載、`up`/`test` 走容器、requirements 由 chart 處理。無需再修。
- **Severity**: low（dev env，非 production）
- **Origin**: cycle 4 tester phase 試 run 時 surfaces；不在 plan AC 範圍
- **狀況（歷史）**：worktree 環境跑 `./scripts/test-addon.sh` 時對某些 setup case 有 gap（如 PAAS_CI_ADDONS_PATH / requirements.txt pre-install / 共用 PostgreSQL 切換 — 與 cycle 3 F24 sub-(b) 同 cluster）

---

## Cycle 4 SC-cluster batch（reviewer round 1 20 條 soft caveat 收尾分布）

| SC | 內容 | 落點 |
|----|------|------|
| SC-1 | `docs/decisions/patterns/mcp-acl-6-rule-split.md` 5 段 update 為 dual-leg variant | **reporter phase inline** ✅（已更新） |
| SC-2 | `docs/decisions/refactor/mcp-project-decouple.md` Invariants 段：Inv #3 D5 deleted / Inv #4 cleanup filter sidecar_backed / Inv #5 dual-leg / Inv #7 URL helper rename / 新 Inv #9/#10/#11 | **reporter phase inline** ✅（已更新，Inv #9/#10/#11 canonical home 在 [[mcp-url-redesign]]） |
| SC-3 | CAQA M1 cron `_cron_retry_mcp_sync` 對新 sidecar-backed row gap | F-X1 ↗ |
| SC-4 | CAQA M2 `_check_project_access` bound-but-no-workspace blanket allow | F-X2 ↗ |
| SC-5 | `_list_projects` F10 sudo + empty domain leak | F10 / F-X2 cluster ↗ |
| SC-6 | `_check_task_access` propagation 與 plan AC #43 wording 不同步（silent 收緊 — security 角度正面） | F-X2 cluster ↗ |
| SC-6b | paas.py `_resolve_project_access` 對 bound-no-workspace edge case 與 ai_assistant.py drift | F-X2 cluster ↗ |
| SC-7 | `ir_rules.xml:145-155` workspace_user comment 改寫（leg 1 「sidecar-backed flow」標籤錯）| reporter phase / batch with F-X3 |
| SC-8 | `_check_project_access` docstring 補 task path mirror + bound-no-workspace edge case | reporter phase / batch with F-X3 |
| SC-9 | `auto_created` help text 補 actionable hint（指向 drop column TODO） | reporter phase / batch with F-X3 |
| SC-10 | `test_mcp_d5_removal.py:194-200` 移除「D5 active」過時假設 | F-X6 ↗ |
| SC-11 | `migrations/18.0.1.0.25/post-migrate.py:44-46` warning branch comment 改事實陳述 | reporter phase / batch with F-X3 |
| SC-12 | `test_project_mcp_project_centric.py:21-28` stale module docstring 拔 `TestProjectMcpD5OrmConstraint` 引用 | F-X6 ↗ |
| SC-13 | `cloud_service.py:_model_build_mcp_endpoint_url` dead code（code-reviewer grep no caller） | F-X4 ↗（URL helper duplication 一併處理） |
| SC-14 | `write_actions` 變數名含 `sidecar-hint` (read-only) 誤導 | future PR rename `gated_actions` 或拆兩 set |
| SC-15 | H.4 cross-workspace assertion 比 H.3 弱（缺 `browse().read()` AccessError leg） | follow-up test 補強 / batch with F27 |
| SC-16 | `test_url_rejects_unsupported_scheme` 沒鎖 error wording | follow-up test 補強 / batch with F27 |
| SC-17 | F group `_assert_rejected` IPv6 4-char host substring matcher loose | follow-up test 補強 / batch with F27 |
| SC-18 | SSRF empty host / port 0 / userinfo edge case 未測（by-design defer #52） | F25 / #52 follow-up |
| SC-19 | Codebase-wide comment ref noise debt（CAQA L1 confirm） | F-X3 ↗ |
| SC-20 | CAQA L2 URL helper duplication + L3 SSRF error format IPv4 vs IPv6 | F-X4 / F-X5 ↗ |

20 條 SC 全分配。SC-1 + SC-2 已 reporter phase inline 處理；SC-3~SC-6/SC-6b/SC-10/SC-12/SC-13/SC-19/SC-20 聚到 F-X1~F-X6；SC-14~SC-18 等 future PR / batch with cycle 1-3 既有 F8/F27 test gap cluster。

---

## F-PA-1 — `agent_session._get_namespace_secret` fallback `_DEFAULT_SECRET` hardcoded

- **Severity**: medium（security caveat；B-1 修了後攻擊面 severity 降，但 install 預設仍應 reject）
- **Origin**: CAQA round 1 M-3 — `src/models/agent_session.py:7` 寫死 `_DEFAULT_SECRET = b'woow-paas-agent-namespace-fallback-secret'`
- **影響**：公開 git repo → attacker 取得 default secret → 預計算 hash for any project_id；B-1 修了後 attacker 無法直接 ORM create agent_session（rule 擋），但可預測 victim project 的 K8s namespace 名稱（reconnaissance value）
- **建議落點**：
  - install hook 加 hard guard：`ir.config_parameter` 取 `woow_paas_platform.agent_namespace_secret`，若空 / 短於 32 bytes → raise `ValidationError`
  - 或改 default 為 `secrets.token_bytes(32).hex()` 隨機生成（per-install 不同），首次 install 寫入 config_parameter
- **何時**：dedicated PR 或 Phase B（paas-agent-runtime 建置時一併補）

---

## F-PA-2 — `cloud_service._sidecar_url_prefix` None fallback → broad-filter 誤殺風險

- **Severity**: medium（M:N 下 fixture / pre-deploy state 可能踩；production 影響低 — service deploy 後才有 sidecar-backed MCP）
- **Origin**: CAQA round 1 M-4 — `src/models/cloud_service.py:1175-1194` `_sidecar_url_prefix()` 對 service 沒 `helm_release_name` / `helm_namespace` 時 return `None`；`_collect_sidecar_user_mcps` 收到 None → 走 broad filter（清所有 candidate）
- **觸發路徑**：
  - project X 綁 service A（無 helm metadata，可能 pre-deploy / fixture）+ service B（有 helm metadata，已 deploy）
  - service A unlink → `_sidecar_url_prefix()` return None → broad filter → 清 X 上所有 sidecar-backed MCP，包含 service B 的 sidecar MCP
- **建議落點**：
  - fallback 改用 `cloud_service.id` 作為 namespace marker（不存在於其他 service URL）— 例如 `f'__service_{self.id}__'` 永不 match `s.url.startswith(...)`，等效於不清
  - 或改為 raise / warn 強制 helm metadata 一致性（service 必有 helm name / namespace）
- **何時**：cleanup safety net hardening PR / Phase B（paas-agent-runtime 建置時可同步檢視 fixture path）

---

## F-PA-3 — `project.cloud_service_id` stored compute 失去明示 `ondelete='set null'`

- **Severity**: medium（correctness uncertainty；不一定是 bug 但 best practice 應該保留明示）
- **Origin**: CAQA round 1 M-5 — `src/models/project_project.py:18-27` 改 `cloud_service_id` 從 plain Many2one with `ondelete='set null'` 到 stored compute + inverse，**失去明示 ondelete**
- **影響**：
  - Odoo 18 對 stored compute Many2one with `store=True` 的 FK constraint default ondelete 依賴 framework default fallback（未驗證 Odoo 18 對 stored compute 的具體行為）
  - 若 Odoo 18 默認 `set null` → 行為等價；若 `restrict` → cloud_service unlink 卡死
  - M:N relation table `project_cloud_service_rel` 已是 `ON DELETE CASCADE`（pre-migrate 明文），所以 unlink 物理上 OK；但 stored compute column 是否同步 recompute 是 Odoo internal 行為，需 verify
- **建議落點**：
  - verify Odoo 18 stored compute M2O FK constraint default ondelete（grep `Many2one.ondelete` 在 Odoo 18 `fields.py`）
  - 明示 `ondelete='set null'` 對齊舊版本契約：`fields.Many2one(..., compute='...', inverse='...', store=True, ondelete='set null')`
- **何時**：small clarity PR / 順手做

---

## F-PA-4 — Phase A 5 view files defer to Phase B/C

- **Severity**: medium（admin operational gap，本身非 security / data 風險）
- **Origin**: OQ-A-3 leader 拍板 defer — plan §Phase A task #14-18 列 5 個 view file，implementation 全沒做：
  - `src/views/agent_template_views.xml`
  - `src/views/agent_session_views.xml`
  - `src/views/skill_views.xml`
  - `src/views/project_access_views.xml`
  - `src/views/project_external_mcp_setting.xml`（含 SSRF risk 警示）
- **影響**：
  - Admin 後台無法直接管理 4 個新 model（必須走 ORM / DB）
  - `external_mcp_allowed` 無 admin UI 開關（必須 ORM `with_user(admin).project.write({'external_mcp_allowed': True})`）
  - `project_external_mcp_setting.xml` 含 SSRF risk 警示 — Phase A enable external_mcp_allowed 時 admin 看不到風險提示
- **建議落點**：
  - Phase B/C admin UI batch PR — 配套 16 ir.rule（已落地）+ 4 new model 後台 form / tree / search view
  - `project_external_mcp_setting.xml` 必含 zh-tw SSRF risk 警示文字（design 預期）
- **何時**：Phase B 或 Phase C admin UI batch

---

## F-PA-5 — `skill_rule_project_user` 「only own draft」design intent 簡化

- **Severity**: low（implementation 比 design 嚴；不是 leak surface）
- **Origin**: CAQA round 2 C-A-1 — design §3.2 line 1528 `skill_rule_project_user`「R=True, W/C=True (only own draft), U=False」；implementation 簡化為 R only，因 ir.rule 無法表達「own draft」需 Python state guard
- **影響**：
  - Phase A user role 無法在自己 draft skill 上 W/C（必須升 admin role）
  - 對 Phase A scope（schema-only，skill upload API 留 Phase B/C）影響 marginal
- **建議落點**：
  - Phase B/C skill upload API 同 PR 補 Python state guard：
    - `@api.constrains('state', 'uploaded_by_uid')` 或 `write()` override 對 user role：只允 `state='draft' AND uploaded_by_uid=self.env.uid`
    - rule `skill_rule_project_user` perm_write=True + perm_create=True；Python gate 守 row 內容
- **何時**：Phase B/C skill upload API PR 同 commit

---

## F-PA-6 — `ASSIGNABLE_ROLES_BY_ROLE[ROLE_OWNER]` 包含 ROLE_OWNER vs design 出入

- **Severity**: low（business intent 出入；implementation 寬鬆但合理）
- **Origin**: CAQA round 2 C-A-2 — `src/models/project_access.py:20-25` `ASSIGNABLE_ROLES_BY_ROLE[ROLE_OWNER] = [ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST]`；design §4.3 line 2090 `ASSIGNABLE_ROLES_BY_OWNER = ['admin', 'user', 'guest']` 不含 owner
- **影響**：
  - implementation 允許 owner grant 另一 owner（multiple owner 是 design 允許的 case，line 2070 `other_owners` 概念）
  - design 漏寫或 implementation 過寬，需 leader / planner 拍板
- **建議落點**：
  - (a) update design §4.3 ASSIGNABLE_ROLES_BY_OWNER 加 owner（business intent confirm）
  - (b) update implementation 把 owner 拿掉，加 ownership transfer 專用 method（business intent confirm 「owner 必須 transfer 而非 grant」）
- **何時**：leader / planner 拍板後 doc-only 或 impl PR

---

## F-PA-7 — `skill.bundle_attachment_id` `groups='base.group_system'` 同擋 project admin

- **Severity**: low（design intent acceptable but maintainer documentation 不足）
- **Origin**: CAQA round 2 C-A-3 — `src/models/skill.py:42-47` `bundle_attachment_id = fields.Many2one('ir.attachment', groups='base.group_system')` SK-6 path (a)；field-level mask 同時擋 project owner/admin 透過 backend form 直接 attach
- **影響**：
  - Project owner/admin 必須走 ORM sudo / API endpoint upload bundle，不能用 Odoo backend form
  - design intent 是 protect bundle binary from non-system；implementation 選最嚴（合理）但 maintainer 看 form view 時會困惑
- **建議落點**：
  - Phase B/C skill upload API endpoint 補（必走 sudo create attachment + ORM create skill 兩步）
  - maintainer doc：`docs/decisions/patterns/skill-bundle-attachment-field-level-mask.md` 或 inline comment 補：「project admin 不能透過 backend form attach bundle，必須走 API」
- **何時**：Phase B/C skill upload API PR 同 commit + reporter 補 maintainer doc

---

## F-PA-8 — `discuss_channel.py:262` stale comment

- **Severity**: trivial (comment-only)
- **Origin**: CAQA round 1 I-1 — `src/models/discuss_channel.py:262` comment 寫「task's primary service (project's cloud_service_id) first」；implementation 已走 `task.primary_cloud_service_id` (hybrid spec)
- **建議落點**：reporter 順手清 — comment 改寫對齊 hybrid spec：「task's primary service (computed default + drift handler, see §7.0.3)」
- **何時**：reporter phase 或 batch

---

## F-PA-9 — `cloud_service._do_cleanup` 缺 ensure_one()

- **Severity**: low (defensive style)
- **Origin**: CAQA round 1 I-3 — `src/models/cloud_service.py:1086-1101` `_do_cleanup` 內走 helper `_sidecar_url_prefix()` / `_collect_sidecar_user_mcps()` 都 ensure_one；但 `_do_cleanup` 本身沒明示 single-record semantics
- **影響**：multi-record `_do_cleanup` 呼叫會在 helper 內 raise，行為意圖不清楚；後續 maintainer 看 method body 不知 single-record assumption
- **建議落點**：method 第一行加 `self.ensure_one()` 明示 — 與 helper 一致
- **何時**：small clarity PR / 順手做

---

## F-PA-10 — `primary_cloud_service_id` docstring 描述不精確

- **Severity**: trivial (docstring clarity)
- **Origin**: CAQA round 1 I-4 — `src/models/project_task.py:38-39, 74-89` `primary_cloud_service_id` docstring 寫「The compute acts as a default」描述不精確（Odoo readonly=False + computed 是 implicit default idiom）
- **建議落點**：docstring 改寫對齊 Odoo idiom：「Stored Many2one with computed default — `readonly=False` makes the compute act as initial value; explicit writes persist via the `inverse` lift, drift resets only when current pick is no longer in the project's bound set.」
- **何時**：reporter 順手清

---

## F-PA-11 — `project_project.create()` SUPERUSER_ID skip → OdooBot owner risk

- **Severity**: low (功能對齊 design，但 OdooBot owner 概念上奇怪)
- **Origin**: CAQA round 1 I-5 — `src/models/project_project.py:108-110` 對 `SUPERUSER_ID` skip bootstrap；migration step 9 補做但 fallback 到 `pp.create_uid`（可能是 OdooBot）
- **影響**：demo / install hook 創建的 project owner = OdooBot partner；design §4.3「real human owner」明文 — OdooBot 不算
- **建議落點**：
  - 評估是否 demo data project 預設不需 owner row（design intent 確認）
  - migration step 9 加 OdooBot detection：若 fallback uid = SUPERUSER_ID，skip + log warning「project_id=X demo / install hook created；no human owner — admin 手動指定」
- **何時**：Phase B/C 或 owner 政策 PR

---

## F-PA-12 — `project_access._compute_user_id` 無 deterministic ordering

- **Severity**: low (single partner 對應多 user 邏輯上不該發生，但 limit=1 沒明示 ordering)
- **Origin**: CAQA round 1 I-6 — `src/models/project_access.py:81-87` `_compute_user_id` 走 `sudo().search([('partner_id', '=', X)], limit=1)` 無 deterministic ordering
- **影響**：若同 partner_id 對應多 user 行（不該發生但 Odoo 不擋），limit=1 結果不確定
- **建議落點**：加 `order='id ASC'` 保 deterministic：`sudo().search([...], limit=1, order='id ASC')`
- **何時**：small clarity PR / 順手做

---

## F-PA-13 — `project_project._inverse_cloud_service_id` replace semantics silent drop M:N bindings

- **Severity**: medium（silent contract change + latent regression at Phase B/C multi-bind activation；CAQA 漏抓，reviewer round 1 抓出標 HC-1）
- **Origin**: reviewer round 1 HC-1（silent-failure-hunter C-2）— `src/models/project_project.py:88-99` `_inverse_cloud_service_id` 把 `cloud_service_id = X` write 透過 `[(6, 0, [X.id])]` replace 整個 `cloud_service_ids` M:N 集
- **狀況**：
  - Phase A migration 後既有 1-service M:N row 沒實際 break（每 row 1 service，replace = noop）
  - **Phase B/C user 透過 UI add 第二 service 後**，任何 controller `cloud_service_id = X` write 會 silently drop 其他 binding，無 log / 無 warning / 無 error
  - 下游影響：`workspace_ids` 收縮 / `task.primary_cloud_service_id` drift / `agent_template.project_ids` 評估改變 / ir.rule visibility 退化 / cleanup lifecycle 連帶錯亂
- **Caller surface（grep verified）**：
  - `src/controllers/paas.py:1594` `vals['cloud_service_id'] = target.id` in `_update_support_project`（rebind path）
  - `src/controllers/ai_assistant.py:944` `vals['cloud_service_id'] = int(cs_id)` in `_create_project`（create path）
- **本 PR 製造？**：是（M2O → stored compute + inverse 是本 PR 引入）；controller caller 是 pre-existing legacy write
- **leader 拍板**：(a) approve with hard caveat — Phase B/C multi-bind activation 前必修
- **建議落點**：
  - 路徑 1（推薦）：`_inverse_cloud_service_id` 加 `_logger.warning` 當既有 set 非空 replace；同時更新 `cloud_service_id` field `help=` 文字明示 "deprecated alias — write replaces full m:n set, prefer cloud_service_ids"
  - 路徑 2：改 additive `(4, X.id)` 語意 — 但變動 controller layer 既有 1-service rebind 行為（不是純 additive，會破 unbind 與 rebind 邏輯），需配套 controller layer 改 m:n 語意
- **何時**：**Phase B/C multi-bind activation 前必修**

---

## F-PA-14 — `project.sudo().create()` bypass `project_access` bootstrap → creator 看不到自己 project 上 4 model

- **Severity**: medium（latent functional regression — 不是 security IDOR，是 own-project visibility gap；CAQA 漏抓，reviewer round 1 抓出標 HC-2）
- **Origin**: reviewer round 1 HC-2（silent-failure-hunter C-1）— `src/models/project_project.py:122` create() override `if self.env.uid == SUPERUSER_ID: return projects` skip bootstrap；對 `ai_assistant.py:946` 唯一 production sudo create path（grep verified）→ bootstrap branch skip → 新 project 無 `project_access` owner row
- **狀況**：
  - Creator 仍可看到 project itself（透過 `project_rule_workspace_member` leg 2 carve-out `create_uid = user.id`）
  - 但對 4 個新 model（`agent_template` / `agent_session` / `skill` / `project_access`）的 16 條 ir.rule 全 walk `project_id.project_access_ids` → no row exists → creator 完全看不到自己 project 上的這些 records
  - **Phase A 沒人 active 4 model**（dormant）→ 無 visible impact today
  - **Phase B-D `use_agent_runtime` active 後 immediate functional regression** — creator 不能管理自己 project 的 templates / skills / sessions
- **與 F-PA-11（OdooBot owner edge case）區分**：F-PA-11 是 demo / install hook fallback；本條 F-PA-14 是 production controller 透過 `ai_assistant.py:946` 觸發的 silent visibility gap — 嚴重得多
- **本 PR 製造？**：是（SUPERUSER skip + 4 model ir.rule 是本 PR）；ai_assistant.py sudo create 是 pre-existing
- **leader 拍板**：(a) approve with hard caveat — Phase D-F `use_agent_runtime` activation 前必修
- **建議落點**：
  - 路徑 1（推薦）：`project_project.create()` 對「`is_superuser()` 但 `env.user.partner_id` 是 real human」case 仍走 bootstrap；透過 context flag（e.g. `_skip_project_access_bootstrap=True`）讓 cron/install/migration 顯式 opt-out
  - 路徑 2：controller fix — `ai_assistant.py:946` 改 `request.env['project.project'].with_user(env.user).create(vals)` 不走 sudo（需 verify create 對 std `project.project` 沒 ACL 阻擋 base.group_user — std `ir.model.access.csv access_project_project_internal` 已是 1,1,1,1 應該允許）
- **何時**：**Phase D-F use_agent_runtime active 前必修**

---

## F-PA-15 — controller sudo path 對 grantor authority constraint bypass — Phase B-E lock-step 提醒

- **Severity**: info（reviewer round 2 EC-3a — round 1 HC-3 hotfix 落地後 reviewer 獨立 walk surface 的 edge case；Phase A 沒 controller 走此 path，dormant）
- **Origin**: reviewer round 2 §12.4 EC-3a — HC-3 hotfix `_check_project_ids_grantor_authority` 對 `is_superuser()` 與 `has_group('base.group_system')` 兩條 bypass；Phase B/C 加 controller endpoint 用 `agent_template.sudo().write({'project_ids': ...})` 時，constraint 不 enforce — 必須在 controller layer 對齊 [[two-layer-acl-lockstep]] pattern 補 grantor check
- **狀況**：
  - Phase A grep verify `agent_template.*sudo\(\)\.write` in `src/controllers/` → 0 hit（dormant）
  - Phase B-E 預期加：admin UI form save / webhook endpoint / cron rebind — 任一 sudo path 都要 mirror Python grantor check
- **建議落點**：
  - Phase B-C controller layer 加 `_check_template_grantor_authority(template, project_ids)` helper（與 `agent_template._check_project_ids_grantor_authority` 雷同），controller sudo path 前 call
  - 對齊 [[two-layer-acl-lockstep]] Layer 1 (ir.rule + Python `@api.constrains`) + Layer 2 (controller helper) 三層 lock-step
- **何時**：Phase B/C 加 agent_template 相關 controller endpoint touch PR

---

## F-PA-16 — `skill._anchor_bundle_attachment` destructive overwrite — maintainer doc 補強

- **Severity**: info（reviewer round 2 EC-4a — round 1 HC-4 hotfix 落地後 reviewer 獨立 walk surface 的 edge case；docstring 已警告但 maintainer doc 補強需求）
- **Origin**: reviewer round 2 §12.4 EC-4a — `skill._anchor_bundle_attachment` 若 admin (`base.group_system`) 把已存在的 `mail.message` attachment（`res_model='mail.message'`）assign 給 `skill.bundle_attachment_id`，anchor 把 attachment 的 `res_model` overwrite 成 `'woow_paas_platform.skill'` → 切斷與原 mail.message 的 `res_model` 反向查路徑
- **狀況**：
  - 對 admin 路徑是預期破壞性 anchoring（admin 是 curating attachment ownership）
  - 但 admin 在 backend form attach 既存 attachment 時可能不知道 res_model 會被改寫 → silent doc gap
- **建議落點**：
  - `skill.py:108-129` `_anchor_bundle_attachment` docstring 已警告（NB destructive — 切斷 mail.message linkage）；建議補加 inline comment 在 attachment write 行明示
  - Phase B/C bundle upload API endpoint 補：upload pipeline 走「create new attachment」而非「re-assign existing」path，繞過 destructive overwrite scenario
  - reporter maintainer doc 補：`docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md` 已 list 此 anti-pattern；建議 admin backend form 加 view-level warning（Phase B/C view 補時順手）
- **何時**：reporter 順手清 / Phase B/C bundle upload API PR

---

# Cycle 6 — Decouple Project Frontend + Controller 收尾 (2026-05-28)

> Plan A+A+A signed-off 後 7 改動點 + CAQA round 1 BLOCKER A1（`_list_tasks` sudo bypass，`cross-workspace-task-data-leak` family 第 2 instance）`1 行` close + 14/14 test/TC 全綠（T1-T4 endpoint + B1-B5 backend baseline + Playwright TC1-TC5 UI）。CAQA re-verdict + reviewer 4-agent verdict 全 APPROVED WITH CAVEATS。Cycle 6 follow-up 分兩類：F-DP-1~F-DP-4 是 CAQA caveat（同 controller 同 family 漏掃 + 設計副效應）、F-DP-5~F-DP-8 是 reviewer minor polish（comment-analyzer + pr-test-analyzer，全 trivial）。

## F-DP-1 — `_get_project_stages` 雙重洞（CAQA CAVEAT #2）

- **Severity**: medium（data leak + write surface；pre-existing）
- **Origin**: CAQA report `docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md` Security CAVEAT #2（line 158-189）+ §同源 sudo bypass scan
- **Location**: `src/controllers/ai_assistant.py:1046`（endpoint @route）+ `:1139-1177`（`_get_project_stages` 內 `TaskType.sudo().search` + `_ensure_default_stages` fallback `sudo().create`）
- **Attack vector**：
  - **Read leak**：attacker enumerate 任意 `project_id` → 拿到該 project 的 stage list（含自訂 stage names，可能洩漏 tenant business 語意如 `"ClientX Q3 Roadmap"`）
  - **Write surface via fallback**：attacker 命中尚無 stages 的 project_id → `_ensure_default_stages` 走 `TaskType.sudo().create()` 寫入 `New / In Progress / Done` 三 stage 並 `project_ids: [(4, project_id)]` 綁到 victim project → mass-create / clutter stage data
- **與 cycle 6 BLOCKER A1 關係**：同 controller、同 endpoint family（`/api/support/projects/<id>/stages` 與 `/api/support/projects` 對稱），`cross-workspace-task-data-leak` B1 family 在 controller layer 剩餘 instance。Cycle 6 close `_list_projects` + `_list_tasks` 兩 instance，剩 `_get_project_stages` + `api_support_stats`（F-DP-2）。
- **建議落點（需 design）**：
  1. 加 access check（呼叫 `_check_project_access`，與 `_update_project` / `_delete_project` 同 idiom）
  2. 評估是否能去 `_get_project_stages` 內的 `.sudo()`（`project.task.type` 可能無 ir.rule，需先查）
  3. `_ensure_default_stages` 應該移到「使用者首次以 owner 身分進 project」的合法路徑（如 `_create_project` 內部 / explicit POST endpoint），不是 list endpoint 副作用
- **何時**：P1 follow-up，建議與 F-DP-2 同期 fix；不阻擋 cycle 6 merge

---

## F-DP-2 — `api_support_stats` 漏 unbound leg（CAQA CAVEAT #3）✅ CLOSED in this PR

- **Status**: ✅ **CLOSED in cycle 6 PR**（user 拍板帶進本 PR + caqa option (b) Task.sudo unify 同期收口）
- **Severity (original)**: minor（UX 不一致 bug，**不是** cross-tenant leak）
- **Origin**: CAQA report Security CAVEAT #3（line 331-360）
- **Closure**：
  - **Fix landed**：`src/controllers/ai_assistant.py:972-978` drop sudo + `domain=[]`（變體 B under-access close）
  - **Task.sudo unify landed**：`src/controllers/ai_assistant.py:980-986` drop `Task.sudo()` + comment 補述 dual-leg 對等性（變體 C same-method 半套狀態 close, caqa mini re-verdict §2 對等性已驗）
  - **Test landed**：`src/tests/test_project_decouple_frontend_endpoint.py:845-1014` `test_t5_api_support_stats_includes_unbound_owned_projects` 鎖 Alice own-data visibility (`assertGreaterEqual(alice_total, 2)`) + Bob bare-stats=0 反向 false-green 守
  - **CAQA mini re-verdict**：APPROVED WITH CAVEATS（不變）— `docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md:717-901`
- **Pattern doc reference**：本 closure 收進 [[orm-vs-endpoint-acl-test-coverage]] 變體 B + 變體 C critical mass（5 instance 4 closed），是該 pattern 的「同 method 一致性 + perf justification 不抵 readability」立論的 cornerstone instance

---

## F-DP-3 — `_check_project_access` follower-only docstring 補述（CAQA CAVEAT #4）

- **Severity**: info（docstring drift；不是 leak、不是 UX bug）
- **Origin**: CAQA report Security CAVEAT #4（line 190-214）
- **Location**: `src/controllers/ai_assistant.py:48-55`
- **狀況**：`_check_project_access` 對 unbound project 走 follower-only check (`user_partner in project.message_partner_ids`)，與 ir.rule `project_rule_workspace_member` unbound clause 的 **dual sub-leg**（`message_partner_ids` OR `create_uid`）非對等鏡像。
- **為何不算漏洞**：cycle 6 D2=A 設計下，`sudo().create()` 後 `create_uid=SUPERUSER`，caller 不會走 create_uid sub-leg；message_subscribe 補 follower sub-leg → controller follower-only check 與「實際生效的 leg」對齊；若未來 D2 切 D2=B (`with_user(user).create()` 讓 `create_uid=user`)，controller follower-only check 會拒 create_uid 應該放行的 row → controller layer 比 ir.rule layer **更嚴**，安全側 fail-closed（不是 IDOR）但 UX bug（合法 owner 被擋）。
- **建議落點（docstring 補述）**：
  ```python
  # _check_project_access docstring 末端加：
  """
  Note: This unbound check is intentionally narrower than the ir.rule
  ``project_rule_workspace_member`` unbound dual-leg
  (``message_partner_ids`` OR ``create_uid``). This narrower check
  assumes the D2=A subscribe-after-sudo-create idiom — the only
  legitimate unbound creator path goes through controller
  ``_create_project`` which calls ``message_subscribe`` after
  ``sudo().create()``. If D2 ever switches to D2=B
  (``with_user(user).create()`` letting ``create_uid=user``), this
  helper MUST be updated to mirror the dual-leg, otherwise legit
  owners will be denied.
  """
  ```
- **何時**：reporter polish；不阻擋；下個 ai_assistant.py touch PR 順手做

---

## F-DP-4 — Frontend 三分支 dispatch 在 component（reviewer Caveat-arch1）

- **Severity**: minor（架構 future evolution；rule of three）
- **Origin**: CAQA Architecture caveat（line 244-263）+ reviewer report §8（line 180）
- **Location**: `src/static/src/paas/components/modal/CreateProjectModal.js:138-147`
- **狀況**：三 mode dispatch (`cs / ws / unbound`) 寫在 component `onSubmit` 內：
  ```js
  let result;
  if (this.state.cloudServiceId) {
      result = await supportService.createProjectForService(...);
  } else if (this.state.workspaceId) {
      result = await supportService.createProject(...);
  } else {
      result = await supportService.createUnboundProject(payload);
  }
  ```
- **評估**：
  - ✅ Service 層 keep simple（3 個獨立 method，無 mode switching）
  - ✅ Modal 是唯一三 mode 都會碰到的 caller，分支寫在 modal 內 = local cohesion
  - ⚠️ 未來若有第 2 個 caller（如 `SupportProjectsPage` 自家 trigger create / 其他 component），會被迫複製 dispatch
- **建議落點（rule of three）**：未來第 2 caller 出現時抽 `supportService.createProjectAuto({workspaceId?, cloudServiceId?, ...payload})`，由 service 層 dispatch；簡化 caller。本 PR 不抽。
- **何時**：future PR，只在第 2 caller 出現時觸發

---

## F-DP-5 — T1 / 模組 docstring 行號 pre-fix 漂移（reviewer M1, comment-analyzer C1）

- **Severity**: trivial（polish；test docstring archaeology by design）
- **Origin**: reviewer report §7 M1（line 154）+ comment-analyzer C1（reviewer report §3）
- **Location**: `src/tests/test_project_decouple_frontend_endpoint.py:38, 47, 169-173, 204, 316`
- **狀況**：T1 / test 模組 docstring 引用 `ai_assistant.py:1038-1040`（dispatcher guard）+ `:1228`（sudo create）為「today's red」狀態行號。Post-fix 漂移 2-4 行（D1 guard 移到 :1037-1038 / D2 message_subscribe 在 :1230-1238）。
- **為何不阻擋**：test module docstring line 35-48 已用「Red expectation today (pre-implementation)」框定，讀者可解讀為「TDD red 留下的 archaeology」；但脆 — 後人 grep `ai_assistant.py:1038` 對不到 actual line。
- **建議落點（polish）**：改 **stable function-name reference** 取代 line 號。例：
  ```
  # before: ai_assistant.py:1038-1040
  # after:  ai_assistant.py::_dispatch_project_action (D1 guard, pre-fix)
  ```
- **何時**：reporter polish；不阻擋；下個 test touch 順手做

---

## F-DP-6 — T3 / T4 docstring 行號 first-comment-line drift（reviewer M2, comment-analyzer C2）

- **Severity**: trivial（polish；同 F-DP-5 性質）
- **Origin**: reviewer report §7 M2（line 155）+ comment-analyzer C2
- **Location**: `src/tests/test_project_decouple_frontend_endpoint.py:366, 529, 579, 599, 763, 766`
- **狀況**：T3 / T4 docstring 引用 `ai_assistant.py:1186`（`_list_projects`）+ `:1325`（`_list_tasks`）是 **first comment line of fix block**，actual call 在 :1190 / :1329（drop sudo 後的真實 search 行）。讀者 grep `:1186` 看到註解 block 起點，不是 fix site。
- **為何不阻擋**：技術正確 + accurate，只是不精確
- **建議落點（polish）**：同 F-DP-5，改 function-name reference（`_list_projects` / `_list_tasks`）取代 line 號
- **何時**：與 F-DP-5 同期；trivial

---

## F-DP-7 — T3 bound case 缺 cloud_service binding assertion（reviewer M3, pr-test-analyzer minor）

- **Severity**: trivial（polish；非 must-fix）
- **Origin**: reviewer report §7 M3（line 156）+ pr-test-analyzer minor
- **Location**: `src/tests/test_project_decouple_frontend_endpoint.py:484-497`
- **狀況**：T3 bound-project case 建立 project 後沒明確 `assertEqual(project.cloud_service_id, svc_alice)` 守 bound binding。若未來 bound create path 退化為 unbound（如 `cloud_service_id` ignored），T3 subset / leak assertion 仍綠（B2 backend baseline `test_ac3_bound_project_workspace_id_unchanged` + Playwright TC3 + T3 owner-survive 三層守已覆蓋，故非 must-fix）。
- **建議落點**：1 行 polish 加在 line 497 後：
  ```python
  self.assertEqual(project.cloud_service_id, svc_alice)
  ```
- **何時**：reporter polish；不阻擋；下個 test touch 順手做

---

## F-DP-8 — `_list_projects` 註解可補 D2=A cross-coupling 說明（reviewer M4, comment-analyzer I1）

- **Severity**: trivial（polish）
- **Origin**: reviewer report §7 M4（line 157）+ comment-analyzer I1
- **Location**: `src/controllers/ai_assistant.py:1186-1189`
- **狀況**：`_list_projects` 註解寫「creator / follower sub-legs」technically 正確（對 ir.rule literal），但**沒明寫 create_uid sub-leg 在 endpoint create path 被 sudo() 戳穿**（因為 controller `_create_project` 走 sudo()，`create_uid=SUPERUSER`）。讀者交叉讀 D2=A 註解才能補完此 cross-coupling。
- **建議落點（polish）**：在 `_list_projects` 註解末加 1 句：
  ```
  Note: the create_uid sub-leg is in practice defeated by ``sudo().create()``
  in _create_project; the follower (message_partner_ids) sub-leg is the only
  active gate for unbound projects — see _create_project D2=A comment for the
  message_subscribe compensation.
  ```
- **何時**：reporter polish；不阻擋

---

## F-DP-9 — Track variant C audit usage（Task.sudo unify lookback，passive observation）

- **Severity**: track-only (P3)
- **Origin**: cycle 6 final addendum reporter retro §5 — variant C 是 [[orm-vs-endpoint-acl-test-coverage]] 三變體中**唯一沒有 test 抓得到**的 anti-pattern
- **背景**：Variant C (same-method 半套 sudo) 的特殊性：
  - A (over-access cross-tenant leak) → HttpCase cross-user test 抓
  - B (under-access own-data invisibility) → own-data assertion test 抓
  - **C (same-method 半套狀態)** → 不會 fail test（input-side drop sudo 後 `accessible_X.ids` 已 ir.rule filter，output-side 殘留 sudo() 在 ACL 角度 safe），純靠 reviewer + pattern doc 立論一致性 audit catch
- **首次 close 反例**：`api_support_stats:980` Task.sudo unify（與 F-DP-2 同期 close，cycle 6 PR）— 是 variant C 在本 codebase 唯一已 close 的 instance
- **觀察目標**：未來 reviewer 1-layer audit 是否成功用 pattern doc 三變體章節抓出新出現的 variant C instance？
  - **Success path**：reviewer 看到 controller method 內 input drop sudo 但 output 留 sudo() → trigger 反射檢查 → 若 perf justification 不充分 → 推 unify
  - **False positive 也算 success**：reviewer flag 一個 variant C suspect 但討論後判 acceptable（有充分 perf measurement / ORM constraint 證據）→ 證明 pattern doc 三變體章節**抓得到 surface**，false positive 是健康訊號
  - **Failure mode**：reviewer 看到同 method 留 sudo() 沒 trigger 反射 → 直接通過 → pattern doc 三變體章節 reading-layer 失效（需要再升級 pointer 或 audit checklist）
- **如何追蹤**：
  - reviewer phase report 內若提到「variant C suspect」/「Task.sudo unify lookback」/「same-method sudo audit」相關 keyword → 記入本條目註腳
  - 6 個 cycle 後（~2026 Q3-Q4）盤點：若 0 instance 觸發 → 關此 entry（pattern doc 三變體章節維護價值已驗證 / surface 漸少）；若 ≥1 instance 觸發 → 評估 pattern doc 是否需強化 audit checklist
- **不限制條件**：本條目**不擋任何 PR / merge**，純 passive observation；維護成本接近零（reviewer 自然會引用 pattern doc，本條目只是聚焦未來統計）
- **狀態**：open（無 timeline，passive observation by design）
- **References**:
  - [[orm-vs-endpoint-acl-test-coverage]] §Anti-pattern 三變體 + §Why 變體 C 不只是 perf trade-off — pattern doc full narrative
  - `docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md` Mini Re-verdict §2（line 717-901）— Task.sudo unify 對等性驗證 + caqa option (a/b/c) reasoning（pattern doc § Perf justification 的源頭）
  - `docs/tracking/pitfalls/orm-test-green-endpoint-leak-blindness.md` §Anti-pattern 三變體 family — variant C 為何沒 test 抓得到的詳述

---

> Phase B（agent-runtime pod image source）的 code / test / CI / carry-over 全在 **`paas-agent-runtime` repo**。其 carry-over SC 的 **canonical home 是該 repo 的 `docs/PHASE_B_HANDOFF.md`**（不在本 woow_paas_platform follow-ups.md 重列每條，避免 cross-repo 雙寫漂移）。本段只記 **woow_paas_platform 側需要知道的連動點 + 指標**。

### PB-ref — Phase B carry-over canonical home

- **Canonical home**：`paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md`（§1 後續 phase 必補 / §2 Phase C wiring assumptions / §3 環境部署 note / §4 其餘 SC / §5 design 回標 planner）
- **Phase B epic durable**（woow_paas_platform 側）：`docs/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md`
- **carry-over SC 摘要**（細節見 PHASE_B_HANDOFF.md）：
  - **SC-4 / SC-5**（glue phase 必補）：mcp/template reloader 缺 tool_set 失效 hook / stub 雙 done（契約已由 planner 4e65966 裁定 ws_server 是 done 唯一 producer）
  - **SC-1 / SC-3 / SC-6 / SC-8 / SC-9** + audit_logger emit guard + base_reloader snapshot live dict：follow-up，不阻塞
- **狀態**：Phase B PR #1 merge 中；上述 SC 由 Phase C/D 接力，**追蹤責任在 paas-agent-runtime repo**（本段僅 epic 視角指標）

### PB-link-1 — F-PA-15 / F-PA-16 是 paas-agent-runtime `cache.py` 的跨 repo 前置依賴 ⚠

- **連動**：`paas-agent-runtime` repo `runtime/skills/cache.py` 假設 woow_paas_platform 側 skill bundle attachment 已 **anchor `res_model='woow_paas_platform.skill'` + `public=False`** + row-level ACL（Inv A2）落實；pod 守「絕不直連 `ir.attachment`」（grep 確認全 runtime 零直連）
- **woow_paas_platform 側對應 follow-up**：[[#F-PA-15]]（controller sudo path 對 `_check_project_ids_grantor_authority` lock-step）+ [[#F-PA-16]]（`_anchor_bundle_attachment` destructive overwrite doc）— 兩者落實狀態是 Phase C wiring 的前置條件
- **行動**：**Phase C wiring 啟動前，必須確認 woow_paas_platform 的 anchor stamping + row-level ACL（F-PA-15 / F-PA-16 + Phase A Inv A2 / HC-4 hotfix）已落 production**；否則 pod fetch bundle 時 attachment delegation target 不對 → 可能 fetch 失敗或繞過 row-level ACL
- **canonical**：`paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md` §2「F-PA-16 連動（woow_paas_platform 側，跨 repo）」

### PB-link-2 — woow-paas-ci 待核對（push 前協調 ci agent）

- `paas-agent-runtime` repo `.gitea/workflows/build-and-trigger.yml` 假設 JCR push secret 名（`JCR_USERNAME` / `JCR_PASSWORD`）/ runner label / Gitea API host — 需與 woow-paas-ci agent 核對
- **canonical**：`paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md` §3「woow-paas-ci 待核對」
- 與本 repo 既有 cross-repo CI 流程（CLAUDE.md §Release & Build paas-odoo Image）為**不同 pipeline**（paas-odoo image vs agent-runtime image），勿混

---

## Cross-cutting: 給未來 PR 作者的 hint

每個改動下列檔案的 PR，自查本 doc 對應條目：
- `src/security/ir_rules.xml` / `src/security/ir.model.access.csv` → F1 / F4 / F16 / F24 (declaration↔migration 對稱) / F26 (R-10 zh-tw comment) / F-X3 (cycle 4 comment ref noise — `ir_rules.xml:251` PR #53 ref) / cycle 5 16 new rule + project/task M:N walk + 2 ir.attachment rule（HC-4 hotfix `ir_attachment_rule_skill_bundle_*`）
- `src/migrations/<v>/post-migrate.py` → F5 (cycle 1 xmlid drift 防呆) / F13 (cycle 2 task ACL 對齊) / F-X7 (cycle 4 drop `auto_created` column after F-X1) / F-PA-11 (cycle 5 SUPERUSER bootstrap OdooBot owner detection)
- `src/models/discuss_channel.py` → F2 / F3 / F6 / F9 / F-PA-8 (cycle 5 stale comment line 262)
- `src/models/cloud_service.py` → F14 / F15 / F-X4 (URL helper duplication `_model_build_mcp_endpoint_url`) / F-PA-2 (cycle 5 `_sidecar_url_prefix` None fallback) / F-PA-9 (cycle 5 `_do_cleanup` ensure_one)
- `src/models/mcp_server.py` → F19 (type design / test coverage cluster) / F-X1 (cron `_cron_retry_mcp_sync` 改 sidecar_backed) / F-X7 (drop `auto_created` field)
- `src/models/project_project.py` → ~~F22 (D5 `@api.constrains`)~~ RESOLVED cycle 4 / F26 (R-5 `workspace_id` readonly) / F-PA-3 (cycle 5 cloud_service_id stored compute ondelete) / F-PA-11 (cycle 5 SUPERUSER bootstrap) / F-PA-13 (cycle 5 round 1 HC-1 inverse replace semantics) / F-PA-14 (cycle 5 round 1 HC-2 SUPERUSER skip → project_access bootstrap gap)
- `src/models/project_task.py` → F-PA-10 (cycle 5 primary_cloud_service_id docstring clarity)
- `src/models/project_access.py` → F-PA-6 (cycle 5 ASSIGNABLE_ROLES_BY_OWNER vs design) / F-PA-12 (cycle 5 `_compute_user_id` ordering)
- `src/models/skill.py` → F-PA-5 (cycle 5 `skill_rule_project_user` own draft Python guard) / F-PA-7 (cycle 5 `bundle_attachment_id` field-level mask doc) / F-PA-16 (cycle 5 round 2 `_anchor_bundle_attachment` destructive overwrite doc 補強)
- `src/models/agent_template.py` → F-PA-15 (cycle 5 round 2 EC-3a controller sudo path 對 `_check_project_ids_grantor_authority` bypass 提醒 — Phase B/C controller layer 補)
- `src/models/agent_session.py` → F-PA-1 (cycle 5 namespace_secret hardcode → install hook hard guard)
- `src/views/` (Phase A defer) → F-PA-4 (cycle 5 5 view files agent_template_views / agent_session_views / skill_views / project_access_views / project_external_mcp_setting incl. SSRF risk 警示)
- `src/controllers/ai_assistant.py` → F4 / F10 / F12 / F20 / F-X2 (bound-no-workspace edge case + drift cluster) / F-PA-14 (cycle 5 round 1 HC-2 `project.sudo().create()` bypass project_access bootstrap) / F-DP-1 (cycle 6 `_get_project_stages` 雙重洞 line 1046 + 1139-1177) / ~~F-DP-2 (cycle 6 `api_support_stats` 漏 unbound leg line 972-986)~~ ✅ CLOSED in cycle 6 PR / F-DP-3 (cycle 6 `_check_project_access` follower-only docstring 補述 line 48-55) / F-DP-8 (cycle 6 `_list_projects` 註解 D2=A cross-coupling line 1186-1189)
- `src/controllers/paas.py` → F19 / F20 / ~~F22 (D5 重設計)~~ RESOLVED cycle 4 / ~~F23 (unbound ACL)~~ RESOLVED cycle 4 / F25 (R-6 try/except + savepoint) / F-X2 (`_resolve_project_access` drift) / F-X4 (`_compute_sidecar_url_hint` duplication) / F-X5 (SSRF error format) / F-PA-13 (cycle 5 round 1 HC-1 `vals['cloud_service_id'] = X` write silent replace M:N)
- `src/tests/test_*.py` → F8 / F17 / F19 / F27 (G1 / G3 / G6 test gaps) / F-X6 (cycle 4 D5 stale comment) / SC-15~SC-17 (cycle 4 test gaps) / F-DP-5 (cycle 6 T1 docstring line drift `test_project_decouple_frontend_endpoint.py:38,47,169-173,204,316`) / F-DP-6 (cycle 6 T3/T4 docstring line drift `:366,529,579,599,763,766`) / F-DP-7 (cycle 6 T3 bound assertEqual `:497`)
- `src/static/src/paas/layout/` → F7
- `src/static/src/paas/pages/project-detail/` → F25 (R-3 McpServersSection error display + E2E-G1 CloudServiceBindingSection rebind-from-unbound) / F28 (R-8 editor migration) / F-X3 (`McpServersTab.js:12` comment noise)
- `src/static/src/paas/services/support_service.js` → F26 (R-9 optimistic merge order) / F-X3 (`support_service.js:454` comment noise) / F-DP-4 (cycle 6 三分支 dispatch 在 `CreateProjectModal.js:138-147` — rule of three, future PR)
- `scripts/test-addon.sh` / dev tooling → F-X8 (worktree env gap)

## Phase D enablement gate — CAQA + reviewer Phase C 彙整（flag flip 前 must-fix）

**2026-05-24** · **狀態 OPEN** — 全部 flag-gated，**不阻 Phase C inert merge**（`use_agent_runtime=False` 不走 WS path）；**Phase D enable `use_agent_runtime` flag 前，must-fix 區必全 close**。來源：CAQA Phase C（A-1/C-3/C-5 plan-alignment，commit `3cab339` 初列 F-PC-1/2/3）+ reviewer Phase C（cross-check 8 caveat 全同意 + 新 N-1~N-5，§12 把 gate 精化為 **must-fix / follow-up 兩層**）。完整論述見 `docs/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md`。

**2026-05-24 更新 · Phase D code-ready 拍板（D-1/D-2/D-3，使用者 sign-off）**：本 gate 進 **code-ready 收口**（不 flip / 不部署 / 不做 2 週觀察 —— 那塊使用者自跑，見 plan §Pilot runbook）。**D-1** F-PC-4 = **global fail-closed（所有 router）**，接受 ~6 支 operator fixture 全改；**D-2** F-PC-3 **升 must-fix 等級納入**、走 K8s Secret env **`AGENT_RUNTIME_NAMESPACE_SECRET`**（殺 `_DEFAULT_SECRET`，缺即 fail-closed）→ **design §2.15 待 follow-up 改寫**（auto-gen→env，code 完成後另開，本次不改 design）；**D-3** F-PA-14 一併納入（最低優先，care HC-2 bootstrap 語義）。新增凍結 env：`AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS`（F-PC-2）+ `AGENT_RUNTIME_NAMESPACE_SECRET`（F-PC-3）。task 拆解 + 每條 red test 入口見 plan `docs/plans/2026-Q2/agent-runtime-pod-plan.md` Phase D（含 Tester hand-off checklist）。

**OQ 閉合狀態（review 後 leader 協調已 addressed）**：
- **OQ-R-1（plan-source 偏離 escalate planner）**：design §5.6（A-1 `extra_headers`）/ §5.5+§2772（C-3 webhook dual-secret）/ §2.15（C-5 namespace auto-gen）三處 design 文字為源、impl 偏離 → 已由 commit `3cab339` 補 ⚠ 標註 + 相容寫法（§5.6 `_hdr_kw`）→ **addressed**（design 同步完成；impl 端落實仍待 Phase D，見下）。
- **OQ-R-2（cross-repo C-1 WS 對稱半邊）**：Phase B pod WS verifier 對稱拒絕 `token_use != 'ws'` 已由 `paas-agent-runtime` repo PR #2 `ba6b417`（**B transitional** — 缺 claim 放行的過渡語意）merge → **addressed**（A 嚴格收緊見下方 F-PC-7；**[2026-05-26 更正] A 嚴格已 merge main PR #3 `bb31676` + build 進發佈 image，stg closed**）。

### must-fix（`use_agent_runtime` flip 前必全 close）

| # | caveat | 位置 | 為何 flip 前必修 | escalate |
|---|--------|------|----------------|----------|
| **F-PC-1** | A-1 websockets `extra_headers` | `ai_assistant.py:650` + `requirements.txt:10` | websockets ≥14 移除 `extra_headers` 改名 `additional_headers`；`>=12.0` 無上界 → flag-on + 裝 ≥14 即 **每條對話首次 connect `TypeError`** 失敗。**做**：釘 `websockets>=12.0,<14` 或 code 相容 both kwargs + 實機驗 WS connect | planner（design §5.6 為源，`3cab339` 已補相容寫法）|
| **F-PC-2** | C-3 webhook dual-secret | `agent_runtime_webhook.py:62` | 單 secret；rotation 過渡窗口（1h）reject `previous`-signed HMAC（cron 兜底但可用性 gap）。**做**：webhook verify 補 `current`/`previous` fallback（與 skill-bundle token verify 共用 pattern）| planner（design §5.5 為源，`3cab339` 已補 ⚠）|
| **F-PC-4** | C-2 operator auth fail-open | `extra/paas-operator/src/main.py:89-91` | `api_key` 未設時 middleware fail-open；Phase C 使 operator 成 **token 簽發者** → fail-open 時可為**任意 project_id** 簽 properly-scoped skill_bundle_token → **跨租戶 bundle 外洩原語**（C-1 對此無解，拿到的是目標 project 合法 token，故 C-2 才是跨 project 風險真正放大器）。**做**：`api_key` 未設 → fail closed / 503（**[2026-05-24 D-1 拍板] global，所有 router**）| — |
| **F-PC-5** | N-1 client `raise_for_status` | `agent_runtime_client.py:32-33` | `_request` 無 `raise_for_status` → operator error（401 bad-key / 500 helm）不 raise → `runtime_info` 變錯誤 dict → `pod_ws_url=None` → `websockets.connect(None)` 被 `:721` 廣捕 → browser 收 **cryptic WS error 而非真因**（error 通道失能，疊 N-2）。**做**：加 `raise_for_status` 或顯式 status 檢查，拋 typed error 帶 operator `detail` | — |
| **F-PC-6** | P-2 async-gen `aclose()` | `ai_assistant.py:678-729` | `finally` 只 `loop.close()` 未先 `loop.run_until_complete(agen.aclose())`；**每條路徑（含正常 done break）relay 都 abandon 在 `yield` → `async with websockets.connect` 的 `__aexit__` 不執行 → pod 側 WS 洩至 ping_timeout(≤60s)**（reviewer 比 CAQA 描述廣：非只 client 斷線）。**做**：drain loop 補 `aclose()` | — |
| **F-PC-7** ✅ **stg closed** | C-1 A 嚴格收緊（cross-repo）| `paas-agent-runtime` `runtime/transport/auth.py` | **[2026-05-26 更正]** A 嚴格（缺 `token_use` claim → reject）**已 merge main**（PR #3 `bb31676`，origin/main 已到 `3263352`/PR#4）+ **build 進所有發佈 image**（v1.0.0 / v1.0.1 echo 皆 **unconditional A 嚴格、無 env toggle**）+ **stg pilot 已驗運作**（pod 收到 user_message = WS accept 通過 `verify_token` = operator-stg 已簽 `token_use` empirical）→ stg 過渡放行已封死。原 2026-05-25「WS verifier 仍 B transitional / 待 operator prod 全簽」係 **stale clone 誤判**（B transitional 從沒 build 成 image）。**剩**：prod operator 全簽 `token_use` gate = 未來式（見 prod 上線 runbook） | cross-repo（stg 已 closed；prod gate 列 runbook）|

### follow-up（enable 後 / 較低優先）

> **[2026-05-25 Phase D 收口]** must-fix 已 **code-closed**：F-PC-1~6 + F-PA-14 在本 repo（`8d152f7`/`e2f1624`/`e94991d`，CAQA+reviewer 確認全正確關閉、HC-2 security-auditor 獨立 airtight）；**F-PC-7 A 嚴格（`paas-agent-runtime` `dabb275`）[2026-05-26 更正] 已 merge main（PR #3 `bb31676`，origin/main 已到 `3263352`/PR#4）+ build 進所有發佈 image（unconditional A 嚴格、無 toggle）+ stg pilot 已驗運作 → F-PC-7 stg closed**（2026-05-25「本 reporter 在 clone 未見 merge / 最後 main merge 為 PR #2 `ba6b417` B transitional」係 **stale clone 誤判**，B transitional 從沒 build 成 image）；**剩 prod operator 全簽 `token_use` gate = 未來式**（stg-pilot-only 階段無 prod agent pod，見 prod 上線 runbook）。剩餘 = **D-CAVEAT-1/2 部署 invariant**（非 code）。下列 C-4/C-6/P-1 **經 leader OQ-D-2 confirm 為刻意延後**（pilot-gate / accepted-risk），非靜默丟棄。

- **F-PC-3 (C-5) namespace secret auto-gen** — `agent_session.py:11,122-127` `_DEFAULT_SECRET` 硬編碼公開 fallback；design §2.15 規定的「migration 首次部署 auto-gen random」未實作 → fresh install silent 落公開 secret → `paas-ws-pa-{hash}` 可預測。**CAQA 初判 gate（F-PC-3）→ reviewer §6/§12 降 follow-up**：只洩 namespace 命名，K8s RBAC + 獨立簽章 secret（`agent-runtime-auth-secret`）兩層仍 gate 實際 access，故非即時洞。**仍建議 enable 前一併修**（design §2.15 規定）：fresh install auto-gen random 寫入 `ir.config_parameter woow_paas_platform.agent_namespace_secret`。escalate planner（design §2.15 為源，`3cab339` 已補 ⚠）。與 [[#F-PA-1]]（Phase A 同源 install hook hard guard）同一落點。 **[2026-05-24 D-2 拍板] 升 must-fix 納入 Phase D code-ready，機制改 env(b)**：不走 auto-gen / ir.config_parameter，改讀 K8s Secret env `AGENT_RUNTIME_NAMESPACE_SECRET`（缺即 fail-closed），與 skill-bundle/webhook env pattern 一致；**design §2.15 待改寫**（auto-gen→env）。詳見 plan Phase D WS1。
- **C-4** webhook replay/freshness（`agent_runtime_webhook.py:30-37,82`）— HMAC timing-safe（`hmac.compare_digest`）但無 nonce/timestamp 窗口；合法 POST 可 replay 翻 `agent_session.state`（cron 自癒）。Phase D webhook 改動只動 HMAC verify（single→dual），**未碰 state-write 邏輯**，故 C-4 carried-forward 非 Phase D 引入。**[2026-05-25 OQ-D-2 leader confirm 刻意延後]** 但 **enabler 欄位 `last_state_event_ts` 已就位**（Phase C 加，目前只 stamp 不擋 stale）→ 未來補「新事件 ts ≤ 既有則跳過 state 寫入」即可。方向：簽章涵蓋 `ts` 拒舊事件 / per project_hash 單調 `sequence_number`。
- **C-6** operator 盲信 `project_hash` 格式（`agent_runtimes.py:38` + `mcp_invalidator.py:18-35`）— `validate_namespace` 只檢 `paas-ws-` 前綴不檢 hash 尾（list-arg 排除 shell injection，疊 C-2 放大）。方向：邊界驗 `^[a-z2-7]{16}$`。
- **P-1** SSE worker-pin（`ai_assistant.py:635`）— 每 active conversation 佔 1 Odoo sync worker（design §6.8 認列 tradeoff，公式 `peak_concurrent_active_conv(50)+baseline(16)=66`）。方向：Phase D 依公式 size worker pool + 確保 pod envelope 必以 `done`/`fatal` 收尾（永遠串流的 pod 釘死 worker）。
- **N-2** dispatcher retry（`webhook_dispatcher.py:41-52`，**未接線** — grep 確認 operator 內無 production caller，只 def + test）— retry 只看 `status_code>=500`，忽略 httpx ConnectError/Timeout（最常見瞬斷零 retry）+ 耗盡後 silent give-up。**接線前修**：wrap `httpx.RequestError` 視 retryable + 耗盡 `logger.error`。
- **N-3** exc 洩漏泛化（`ai_assistant.py:625,726`，= CAQA C-7 加強）— `str(exc)` 可能含 pod 內部 `ws://` cluster DNS 洩到 browser；server 端 `_logger.exception` 已對（保留）。方向：client 端泛化訊息 + correlation id，細節只進 log。
- **N-4** operator namespace/release 命名契約未 assert（`tests/test_agent_runtimes.py:291-292`）— `_NS`/`_RELEASE` dead 常數 + 常數缺 `pa-` 分隔符（impl 命名跨檔一致 `paas-ws-pa-{hash}`，純 test 衛生非 impl bug）。方向：補 `_names()` call-args 斷言（鎖 hash→namespace→RBAC 邊界值）。
- **N-5** SHA256-over-raw-bytes invariant 僅隱含（`test_..._skill_bundle.py` SB.1）— 加一行 `sha256(resp.content)==skill.bundle_sha256` 直接斷言廉價硬化 contract（pod 端責任，acceptable）。
- **gap-B（flip-gate runbook）operator `/health` `signs token_use` 機器驗訊號** — CAQA/reviewer 建議 operator `/health` 暴露 `signs token_use: true`，把 pilot flip-gate runbook step 2 的人工「確認 operator 簽 `token_use`」轉成機器可驗訊號（D-CAVEAT-2）。**[2026-05-25 gap-B 拍板] 列 follow-up（Phase E+ fleet 規模補機器驗）**；屬 `paas-operator` 改動。**pilot fallback**：step 2 人工確認 operator 部署版本 ≥ `token_use` 簽發版本。詳 `docs/guides/deployment/agent-runtime-pilot-flip-gate-runbook.md` step 2 + §Gap。
- **gap-C（flip-gate runbook）secret 跨進程同值 startup 斷言** — D-CAVEAT-1：namespace/auth/webhook secret 改 per-process env 後，消費進程（Odoo web/cron worker + operator）任一不同值或空值 → namespace hash 靜默分歧（webhook 永遠 404 / 同 project 不同 worker 算出不同 `paas-ws-pa-{hash}`）；charts `optional:true` 使 partial 部署不在 deploy 時炸。方向：hash 消費點（`agent_session._get_namespace_secret` / `agent_runtime_webhook` verify）加 startup/health 一致性斷言，把靜默分歧前移成「啟動即報」。**[2026-05-25 gap-C 拍板] 列 follow-up（Phase E+ 補斷言）**。**pilot fallback**：runbook step 4 手動 `sha256sum` 兩端比對 + `printenv` 驗 5 env 非空。詳 runbook step 4 + §Gap。
- **info（6）**：C-7（exc 洩漏 SSE，併 N-3）/ Q-1（`datetime.utcnow()` deprecated，`ai_assistant.py:659`，方向 `datetime.now(timezone.utc)`）/ Q-2（HS256 verify 雙實作 — 跨 process/repo 無法共享 module，刻意非 DRY 違規）/ Q-4（`new_state` 寫入無 Selection 預檢 → out-of-enum 得 500 非 silent corruption）/ P-3（cron drift 5min sequential `get_status`）/ P-4（operator `verify_token` + jti cache 似未被 router 取用，operator 只簽 / pod 才驗 → 可移除或待用 helper）。

**gate 驗收**：Phase D 第一次 spawn-or-attach 真起 pod（enable flag）前，CAQA / reviewer 再 verify 一輪 — 特別 **F-PC-1 實機 WS connect**（websockets 版本對）/ **F-PC-5 + F-PC-6 error 通道 + WS lifecycle**（operator error 正確 surface + 每條對話無 WS 洩漏）/ **F-PC-7 A 嚴格**（**[2026-05-26 更正] stg 已 closed**：`dabb275` 已 merge main PR #3 `bb31676` + build 進發佈 image unconditional + stg pilot 已驗；**prod operator 全簽 `token_use` gate = 未來式**，移至 prod 上線 runbook）。對應 plan `agent-runtime-pod-plan.md` Phase D §「Phase D enable gate」+ §Pilot runbook flip-gate caveats。

### Phase D reviewer R-findings（R-2/R-3/R-4 — minor，非阻 merge）

reviewer Phase D §6 新發現，無一 high-severity / 無一 code 缺陷。**R-1**（HC-2 read-site SET-SITE invariant comment）+ **R-5**（demo template `is_public` vs draft comment tidy）已由 reporter 本 cycle **順手補 comment**（`8d152f7` 後的 docs commit）。以下 3 條是 code/test 改，**非 reporter 改**，Phase D pilot 前或 Phase E 補：

- **R-2** F-PC-6 aclose log level（`ai_assistant.py:741`）— best-effort except 落 `_logger.debug`，systemic close-handshake 失敗在 prod（INFO/WARNING）不可見。方向：升 `info`/`warning`（impact bounded — 內層 `loop.close()` 仍回收 socket）。CAQA 已列 info，reviewer R-2 再確認。
- **R-3** F-PC-2 empty-PREVIOUS 獨立負例（`test_agent_pod_phase_d_hardening.py` F-PC-2 群）— empty-slot 不放寬 invariant **code 正確但無獨立負例**，未來 refactor 拿掉 empty-guard 不會 red。方向：補一條 `empty PREVIOUS + 任意 sig → 401` 負例。
- **R-4** bootstrap hint int-coerce（`project_project.py:133`，security-auditor optional/defensive）— `bootstrap_owner_partner_id` 未 `int()`-coerce 即 `browse()`；今天 unreachable（無 untrusted set-site，見 [[constraint-input-trust-expansion]] §Forward-looking invariant）。方向：一行 `int()` guard 防未來 careless trusted caller 傳 string。
- **何時**：Phase D pilot 前或 Phase E（code/test PR，非 reporter）。

## F-GC-1 — SKILL_BUNDLE_TOKEN secretKeyRef hardening（gap C pilot）

- **Severity**: minor（security hardening；pilot 可接受現狀）— ⚠️ **TTL 拉長後此 pilot-接受理由需重審，可能上調 severity**（見下方「為何 pilot 接受」更新）。
- **Origin**: agent-runtime-pod epic gap C（operator chart install + WS transport，2026-05-25，PR #63）
- **問題**: operator 把 `SKILL_BUNDLE_TOKEN` 以 **plain env value** 注入 pod（install 走 helm `values.env.SKILL_BUNDLE_TOKEN`、resume 走 `kubectl set env`）→ token 明文落 pod spec（`kubectl get pod -o yaml` / `describe` 可見）。
- **為何 pilot 接受（原始論證）**: token 短壽（exp 1h）+ 每次 resume 重簽重注 → 明文風險窗 ≤1h 且輪替；且 plain env 是 operator resume（`kubectl set env`）機制的硬相容需求 — secretKeyRef 會被 `kubectl set env` 的 plain value 覆蓋成 drift。leader 已認同 plain env、secretKeyRef 列此 follow-up。
- **⚠️ 接受理由需重審（issue #155 TTL config 化後，2026-06-06）**: 上述「明文風險窗 ≤1h 故可接受」前提已被打掉。issue #155 stopgap 把 `SKILL_BUNDLE_TOKEN` 的 TTL 改 config-driven（`skill_bundle_token_ttl_seconds`，env `SKILL_BUNDLE_TOKEN_TTL_SECONDS`），**預設 7 天（604800s）** — 明文暴露窗從 ≤1h 擴大到 ≤7d（168 倍）。原本 minor/pilot-可接受是建立在短壽輪替上的，TTL 拉長後 plain env vs secretKeyRef 的 risk 評級需重審，**可能上調 severity**；secretKeyRef hardening 的排程優先序應一併重評。
- **hardening 方案（redesign，非 chart-only）**: operator 改為管 per-release K8s Secret（存 token）+ resume 時 patch 該 Secret；chart 改 `env.valueFrom.secretKeyRef`。需 operator + chart lock-step。
- **owner**: paas-platform（operator）+ paas-charts（chart）；**待 leader 排程**。
- 對照: [[agent-runtime-operator-chart-install-contract]] §Q2b / §替代方案、design §6.5。

## F-PILOT — agent-runtime pilot e2e durable 回收清單（2026-05-26）

> Step 6 flip 實跑閉環時打的 ephemeral patch + 揭露的系統性 chart/operator 缺口 + plan-level 設計缺口。**這份是分派藍圖**（leader 拿去派各 repo agent 轉正）。根因鏈見 [[agent-runtime-pilot-e2e-gap-chain]]、process learning 見 [[spawn-install-values-missed-forward]] + [[plan-gap-stop-and-report]]。

| # | tier | 主題 | lane | plan-level |
|---|------|------|------|-----------|
| F-PILOT-1 | 1 (ephemeral, Fleet sync 洗) | operator image f952f14 → pin | paas-cd | no |
| F-PILOT-2 | 1 (ephemeral, Fleet sync 洗) | operator repoUrl external → chart default | paas-charts(PR#3)+paas-cd | no |
| F-PILOT-3 | 1 → ✅ durable (image bake + pin; 殘留 ns-重建邊界 LOW) | odoo websockets → PR#68+rebuild+pin | paas-platform+deploy | no |
| F-PILOT-4 | 2 (系統性, 每 project 撞) | 動態 ns registry pull secret provision + imagePullSecrets forward | paas-platform(operator)+設計 | **YES** |
| F-PILOT-5 | 2 (系統性) | NP odooNamespace forward + chart default | paas-platform(operator)+paas-charts | minor |
| F-PILOT-6 | 2 (系統性) | chart probe /health→tcpSocket | paas-charts ± image | no |
| F-PILOT-7 | 2 (系統性) | chart strategy=Recreate (RWO PVC) | paas-charts | no |
| F-PILOT-8 | plan-level | LLM credential 注入 | 設計拍板 + image | **YES** |
| F-PILOT-9 | 3 (feature) | agent_template 管理 UI + 綁定 UI | paas-platform 未來 Phase | no |
| F-PILOT-10 | image lane | echo→真 LLM chat + skill-bundle fetch | paas-agent-runtime image team | no |

### Tier 1 — ephemeral，Fleet sync / pod restart 會洗掉（優先轉正保 pilot 穩定）

**F-PILOT-1** — operator image `f952f14` 由 `kubectl set image` 暫頂
- 問題: operator chart Fleet sync / helm-upgrade 用 chart image.tag 重設 → 回退舊 image → 撞回第 1 關（ns-prefix validate_namespace 500）+ 第 3 關（projectId）
- 修法: paas-cd 在 stg values pin `f952f14`（含 PR #66 + #67，已 merge develop）
- lane: **paas-cd** ｜ ⚠️ 與 F-PILOT-2 **必須原子化一起上**（merge PR#3 觸發 Fleet sync 時 image 沒同步 pin 就回退）

**F-PILOT-2** — operator `AGENT_RUNTIME_REPO_URL=external` 由 `kubectl set env` 暫頂
- 問題: 同上 Fleet sync 回退 → helm pull 撞 401（Gitea index tarball 公開 host vs operator 內部 DNS、helm 不跨 host 帶 basic-auth）
- 修法: paas-charts **PR #3** operator chart default repoUrl 改 external + paas-cd 移除 CD pin（環境無關值該繼承 chart default）
- lane: **paas-charts(PR#3) + paas-cd**

**F-PILOT-3** — odoo `websockets` 由 `pip install` 暫頂進 running pod
- 問題: odoo pod restart / redeploy 失去 pip install → `_stream_agent_runtime` import websockets ImportError → 「WS client unavailable」
- 修法: **PR #68** merge（requirements.txt 加 websockets）+ paas-odoo image 重建（image tag `20260526.1221` bake requirements）+ redeploy dev-sandbox-stg（paas-cd pin odoo image）
- lane: **paas-platform(PR#68) + deploy**
- ✅ durable 範圍: survive odoo pod restart / `helm upgrade --reuse-values` / Fleet sync（image 已 bake websockets，realistic 威脅消除）
- ⚠️ 殘留邊界（informational, LOW）: 全新 `create.sh` install（ns 重建）的 odoo image 來自 deploy-stg `--image` 參數、非 chart default（chart values.yaml odoo default = 通用 `odoo:18.0`、非 paas-odoo）。「終極 durable（survive ns 重建）」靠 deploy-stg 流程**永遠** build from develop（已含 PR#68）→ 每次 deploy-stg 都帶 websockets；chart `odoo:18.0` default 只在「不走 deploy-stg、裸 create.sh 不帶 --image」才會中，但那會部署無 woow_paas_platform addon 的通用 odoo（整個 app 不見、非僅 websockets），不是正常情境 → 無需緊急動作，正常 deploy-stg 已覆蓋
- lane（殘留）: **paas-platform/charts**（如要硬化：deploy-stg/create.sh 把 stg-mode odoo image default 指向最新 paas-odoo、而非 chart 通用 default）

### Tier 2 — 系統性 chart/operator 缺口（每個新 project 都撞；本 pilot 靠 patch 撐，operator 重 helm install 會洗、rollout-restart 保留）

**F-PILOT-4** — 動態 per-project ns 無 registry pull secret + chart 沒帶 imagePullSecrets ⚠️ **plan-level**
- 問題: agent-runtime 私有 image（jcr-prod）；operator `_ensure_namespace` 只建裸 labeled ns、無 dockerconfigjson；chart imagePullSecrets 預設空、operator 沒 forward → ImagePullBackOff 401（匿名 pull 被拒）
- 修法: operator cold-start 把 registry pull secret provision 進動態 ns + forward `imagePullSecrets`（chart 已支援 value）
- lane: **paas-platform(operator) + 設計拍板**: creds 來源（copy operator 自身 jfrog-registry / 新 config / 沿用 marketplace paas-registry 機制）+ 租戶 ns 放 registry creds 的安全
- plan-level: **YES**
- **[設計已拍 2026-05-26 · Track B]** creds 來源 = **沿用 marketplace `paas-registry`**（複用 operator 既有 `helm.py:649 ensure_registry_secret`，非 copy / 非新 config）；安全邊界 = **落租戶 ns 但收緊 pod SA/RBAC 不可讀 secret**（imagePullSecrets 只給 kubelet，pod process 讀不到）。design §8.1 / plan §Phase E0（WS-4a/b/c + T4.1-4.4；安全 INV-1 可測：pod 試讀 secret → Forbidden）。
- **[Phase E0 operator green 2026-05-27 · `3d7e083`]** operator 端**已落**：cold-start `k8s_service.ensure_registry_secret(ns)`（複用 `helm.py:649`，與 marketplace `create_namespace` byte-identical 同紀律）+ `_build_install_values` forward `imagePullSecrets:[{name: settings.registry_secret_name}]`（**只帶 secret 名、不帶 creds**）。CAQA PASS WITH CAVEATS / reviewer APPROVE，operator **無新增洩漏面**。**剩 cross-repo lock-step（owner paas-charts，GA / pilot-e2e 前 hard gate）= E0-CAVEAT-3**：chart **SA-minimal RBAC**（pod SA 無 `secrets` get/list/watch；INV-1 唯一防線，失效 = 平台級 jcr-prod creds blast radius）+ **建議 registry creds pull-only-scoped**（registry IAM 層 defense-in-depth，非 operator code）。

**F-PILOT-5** — NP `odooNamespace` 沒被 operator forward → chart 錯 default
- 問題: chart NP ingress(odoo→pod WS)+egress(pod→odoo skill-bundle)都用 `.Values.networkPolicy.odooNamespace`，default `paas-system`（placeholder）≠ 實際 odoo ns（dev-sandbox-stg）；operator `_build_install_values` 只 forward egressWorkspaceIds、沒 forward odooNamespace → pod↔odoo 兩向被 agent pod 自身 NP 擋（refused）
- 修法: operator forward `networkPolicy.odooNamespace` + chart 修錯 default（理想設 required）
- lane: **paas-platform(operator) + paas-charts** ｜ minor design: operator 取 odoo ns 來源（parse ODOO_BASE_URL FQDN vs 新 config）

**F-PILOT-6** — chart probe path ↔ image health endpoint 不匹配
- 問題: chart liveness/readiness `httpGet /health`，但 agent image 不 serve `/health`（404）→ readiness 永不 ready + liveness 殺 → CrashLoopBackOff → service 不 route
- 修法: chart 改 `tcpSocket:ws(8000)`（最穩，uvicorn listen 即過）；或對的 health path；或 image team 補 `/health`
- lane: **paas-charts ± image team**

**F-PILOT-7** — chart RollingUpdate + RWO PVC → re-spawn Multi-Attach deadlock
- 問題: deployment 預設 RollingUpdate，新 pod 起來前舊 pod 不釋放 RWO workspace PVC → 新 pod Multi-Attach 卡 ContainerCreating
- 修法: chart `strategy: Recreate`（image agent 已 ephemeral patch 驗證可解）
- lane: **paas-charts**

### Plan-level 設計拍板

**F-PILOT-8** — LLM credential 注入（真 LLM agent）⚠️ **plan-level**
- 問題: echo 收口不用 LLM；真 agent 要模型金鑰，但「金鑰怎麼到 pod」未設計（via skill-bundle？template model_config？專用 secret？）；綁 template model_config_id 後 echo 行為不變（echo 不行使 LLM）
- lane: **設計拍板 + paas-agent-runtime image team** ｜ echo → 真 LLM 前必拍
- plan-level: **YES**
- **[設計已拍 2026-05-26 · Track B]** 注入路徑 = **專用 per-release K8s Secret + secretKeyRef env**（非 plain env / 非 skill-bundle 夾帶；LLM key 長壽，比 `SKILL_BUNDLE_TOKEN`(F-GC-1) 更該收緊）；隔離 = **per-tenant key**（`agent_template.model_config_id → ai.config.api_key` 已關聯，洩漏圈限單租戶）。產品策略（自帶 key/代付/quota/輪替）留 design open、不阻 code。design §8.2 / plan §Phase E0（WS-8a/b/c + T8.1-8.4；INV：secretKeyRef 不落明文 + per-tenant 隔離）。
- **[Phase E0 operator green 2026-05-27 · `3d7e083`]** operator 端**已落**：`_provision_llm_secret`（**keyless 早退** → 有 api_key 才建 per-release `{release}-llm` Secret、stringData、`paas-managed` label；INV-2 per-tenant 隔離成立，release+ns 皆 keyed by hash）；api_key 隨 spawn request（解耦 operator↔Odoo DB）。**明文 key 5 點獨立重驗不洩漏**（`agent_runtimes.py` 零 logger / `apply_manifest` 0600 tmpfile argv 只有檔名 / generic exception handler 不 echo body / `KubectlException.__str__` message-only / `Optional[str]` 無 422 echo）。**E0-R-1**（key-absent 負分支 missing-negative，mutation 證實刪 guard 全 43 test 仍綠）已由 tester `cdf5368` `test_T81c` mutation-resistant lock 關閉 → 見 [[credential-claim-test-fixture-coupling]] §missing-negative 形態。**剩 cross-repo + lifecycle caveat**：
  - **E0-CAVEAT-2**（owner paas-charts lock-step）：chart `LLM_API_KEY` secretKeyRef **必須 `optional:true`**（與 Phase D charts `optional:true` pattern 一致）；否則 keyless（echo pilot 主路徑）pod `secretKeyRef` 指向不存在 `{release}-llm` → kubelet `CreateContainerConfigError` → **pod 起不來，阻 pilot e2e**。operator 無法防（conditional-create 契約正確，失效全在 chart 端）。
  - **E0-CAVEAT-1**（orphan 明文 LLM Secret on teardown）→ 見 **F-PILOT-10 HARD gate bind**（真 LLM 上線前必收）。

### Tier 3 / 其他 lane

**F-PILOT-9** — agent_template 管理 UI + project 綁定 UI（Phase D 故意延後）
- 問題: 無 backend view/menu/action 建/發佈/綁 agent_template；project form 只曝露 use_agent_runtime、沒 agent_template_id；pilot 靠 dev-mode generic view / ORM 種
- lane: **paas-platform 未來 Phase** ｜ severity low（pilot 可用 seed）

**F-PILOT-10** — echo → 真 LLM chat 邏輯 + skill-bundle 實際 fetch（image lane）
- 問題: image 1.0.1 echo 收口（收 user_message → 1 個 echo text_chunk，done 由 ws_server）；真 LLM 處理 + skill-bundle fetch 未實作
- 修法: 依 [[agent-runtime-ws-protocol]]（PR #69）契約實作 pod `/v1/chat` 回覆 + skill-bundle fetch
- lane: **paas-agent-runtime image team**（與 F-PILOT-8 LLM cred 連動）
- **[E0-CAVEAT-1 HARD gate bind 2026-05-27]** orphan 明文 LLM Secret on teardown：operator `delete_runtime` 只 `helm uninstall`、`reap` 只 scale-0，**全程無 `kubectl delete ns`**（auditor sweep 確認）→ out-of-band（`apply_manifest`，無 owner-reference）建的 `{release}-llm` 明文租戶 key **不被 helm uninstall 回收**，殘留 ns 直到外部清。**echo 階段邏輯上不可能 orphan**（無 api_key → `_provision_llm_secret` 早退不建 secret），故 Phase E0 **非 blocker**；但 **F-PILOT-10 真 LLM 上線（會帶真 api_key）前必收 = HARD gate（非 someday / optional）**。屆時做 **holistic teardown-lifecycle design**（LLM Secret + registry secret reap + ns-delete **一起設計**，**非** piecemeal 加一行 `kubectl delete secret`）；方向：teardown 刪 ns（一次收全部）/ 或 chart 接管 Secret 使 uninstall 回收。CAQA 已路由 **planner**（plan-completeness gap：design per-release Secret 生命週期隱含假設 ns teardown 回收，但 operator 從不刪 ns）。owner: paas-platform(operator) + planner（design §8.2 teardown 補寫）。

### F-PILOT-10 review takeaway — 三層 gate（3 code PR merged 2026-05-27）

> image PR#6→main `6945ea5` / client PR#76 + teardown PR#77→develop `be6746d`。CAQA **PASS WITH CAVEATS**（0 unconditional blocker / 1 conditional-escalation / 5 caveat / 4 info）+ reviewer **APPROVE**（pilot stg，0 mandatory blocker，3-agent + CAQA 無方向性分歧）。4 核心控制驗到位（無 command injection / fail-closed / cred 不入 log / `MAX_TOOL_ROUNDS=8`）；empty-bearer lock-step 做對（[[credential-claim-test-fixture-coupling]] 正面範例，對比 E0 缺口）。caveat 按 **pilot stg 非 production** 分三階段 gate：

**Tier 1 — 真 cluster 部署前（leader cross-repo gate）**
- **C10-1 SSRF NP backstop** — url_validator 是 defense-in-depth，主防線 = cross-repo NetworkPolicy（paas-charts）；殘留 DNS-to-metadata / redirect / trailing-dot（validator 不 resolve DNS 的本性）。**paas-charts woow-k3s 已實測確認 ✓**（kube-router 擋 metadata/public）。**部署時（真 LLM `externalEgress.enabled` 開放後）NP 改 except-CIDR 須重測：LLM API 通 + `169.254.169.254` metadata 仍 blocked**；prod CNI ≠ woow-k3s 須各自確認（Calico/Cilium enforce egress ✓；**flannel 不 enforce → NP no-op，升 HIGH**）。owner: paas-charts + leader 部署 gate。已收：image **#52**（關 redirect-following + trailing-dot 正規化，便宜 defense-in-depth，in-progress）。

**Tier 2 — 真 LLM 對外前（production rollout gate）**
- **R10-1 observability（最該收）** — `langgraph_runtime.py` 全檔零 server-side log/audit；8 條 degradation 路徑（echo-fallback / config downgrade / tool failure / round exhaustion / MCP skip / overrides drop / keyless-vs-misconfigured）pod log 全不可見 → misconfig real-LLM 靜默降級成 echo 無訊號。方向：degradation 路徑補 `audit_logger.emit_event` / `_logger.warning`（echo-fallback / bad-config / tool-error / max-rounds 各一）。owner: image。
- **C10-3 agent loop byte cap** — `full_response` / `messages` / MCP `result_str` 無 byte cap（subprocess skill 有 256KB cap，MCP ainvoke 回傳無）→ 8 輪 × N tool_calls memory/cost 放大。方向：cap 每 tool-result + 總量。owner: image。
- **C10-2 interpreter allowlist** — skill `runtime`/`entrypoint` sha256-pinned 但 author-controlled、未 interpreter-allowlist（v1 author 可信內可接受）；authorship 開放前 allowlist 已知 interpreter。owner: image。

**Tier 3 — 一般 follow-up（較低）**
- **C10-4** MCP transport allowlist — `if not url: continue` safe-by-omission，補 explicit `transport in {http,sse}` 白名單。owner: image。
- **C10-5** model_overrides key allowlist — owner/admin 可寫 JSON merge 進 model_config（client `agent_runtime_client.py:60`），pod 端只讀 4 key → 額外 key inert（低）。方向：client 端 allowlist override key。owner: paas-platform。
- **R10-2** round-exhaustion signal + test — `MAX_TOOL_ROUNDS=8` 耗盡無 final answer（dangling tool frames）+ 無 test。方向：耗盡時 emit synthetic「已達工具上限」text_chunk + 補 test。owner: image。
- **R10-3** bad-JSON model_overrides 負例 test — `_build_llm_credential` bad-JSON guard 存在但無 test（CL.6b 只測 valid merge）。方向：補 bad-JSON 負例（cheap）。owner: paas-platform。
- **R10-4** dead-path cleanup — loader eager-builder（`build_skill_tool` / `get_skill_tool` / `_TOOL_CACHE` 新 lazy flow 無 caller）+ `cache.py:74` SHARED_SECRET fallback（Odoo A-strict 401 fail-closed 非 bypass）。方向：cleanup pass 確認移除（非阻斷）。owner: image。

**已收（不重列）**：image **#52**（redirect + trailing-dot defense-in-depth，C10-1 image 半）；[[#F-PILOT-11]]（富 skill args_schema，Gap 3 hybrid 延後項，paas-platform 已記）。
**E0-CAVEAT-1 teardown**：已由 **PR#77** design §8.4 holistic teardown sign-off 回應（D1-b 隨 ns 清 / D2-a `delete_runtime`=helm uninstall+kubectl delete ns 雙步冪等 / Gap2 GC 擴掃孤兒 / StorageClass=longhorn Retain）；impl（WS-10a operator delete ns / WS-10c chart SC）不可逆待 leader 協調（plan Phase E0-T）。

### F-PILOT-10 cluster e2e gaps（真 LLM round-trip 達成 2026-05-27 — 8-gap durable 回收）

> 🎉 真 LLM round-trip 達成（「你好8」12:58、19s、MiniMax-M2.7、無 error、乾淨 disconnect、browser 理解 history）。cluster + 完整 UI round-trip 串出 8-gap chain（mock/caqa/reviewer 全看不到 — 見 [[agent-runtime-fpilot10-cluster-e2e-gap-chain]] pitfall）。每關 ephemeral patch 解封到 round-trip，durable 分軌如下。

| gap | durable 修法 | lane | status |
|-----|-------------|------|--------|
| **G1** operator 自身 image stale | 升 operator 自身 image（forward 邏輯生效）；deploy checklist 驗 running image 版本（非 values pin）| paas-cd/ci | ✅ **CLOSED** — operator image `20260527.1214` deployed |
| **G2** imagePullSecrets 沒 render | operator forward + **chart imagePullSecrets default backstop** | paas-platform(operator) + paas-charts | ✅ **CLOSED** — chart backstop（paas-charts gap-C delivery「imagePullSecrets backstop」）|
| **G3** odoo cold-start race | odoo relay 加 **cold-start retry**（connect 失敗退避重試到 pod ready；現第一次 connect 必 refused） | paas-platform(odoo) | **OPEN**（後續改善；round-trip 非 blocker — pod ready 後即通）|
| **G4** image silent close（無 error frame）| image：真 LLM 失敗送 **error frame** + **success-path log**（非只 error，= R10-1 延伸） | paas-agent-runtime image | **OPEN**（後續改善 = R10-1 production gate）|
| **G5** externalEgress 沒 enable | chart NP `externalEgress.enabled` + except-CIDR（= C10-1 deploy gate；真 LLM 通 + 169.254 metadata 仍擋）| paas-charts + deploy | ✅ **CLOSED** — `values-stg config.agentRuntime.externalEgress.enabled=true`（隨 G8 durable）|
| **G6** chart 漏 wire `LLM_MODEL_CONFIG` env | chart 補 env 映射 + render-to-pod-spec integration test | paas-charts | ✅ **CLOSED** — chart `LLM_MODEL_CONFIG` wire（paas-charts gap-C delivery）|
| **G7** operator 沒 forward externalEgress | operator `_build_install_values` forward + contract test 涵蓋此 **optional** key（[[spawn-install-values-missed-forward]]）| paas-platform(operator) | ✅ **CLOSED** — operator forward **PR#79**（image `20260527.1214`）|
| **G8** paas-operator chart 無 `extraEnv` | **paas-operator chart `extraEnv`** 注入 operator config env（`EXTERNAL_EGRESS_ENABLED`，原靠 ephemeral `kubectl set env`、redeploy 丟）| paas-charts(operator chart) | ✅ **CLOSED** — operator chart **0.1.11** env wire（render test 鎖）+ **survive redeploy 驗證**（`helm get manifest` 渲染鐵證，非 ephemeral patch）|

> **[G8 durable closed 2026-05-27 — F-PILOT-10 唯一 ephemeral caveat 已解]** externalEgress env 從 ephemeral（`kubectl set env`）轉 durable 三件套：operator chart `0.1.11` extraEnv（G8）+ operator code PR#79 forward（G7）+ `values-stg externalEgress.enabled=true`（G5）→ `helm get manifest` 渲染鐵證 + survive redeploy。**cd 側全部署 durable**（operator `20260527.1214` / agent `1.1.1` / agent chart `0.1.4` / operator chart `0.1.11`，externalEgress env 全進 git+chart）。**G1/G2/G5/G6/G7/G8 全 CLOSED**（deploy/missed-forward gaps landed durable）；**剩 G3（cold-start retry，platform）+ G4（observability success-path，image）為後續改善 follow-up**（round-trip 非 blocker），+ production-rollout gate（R10-1/C10-2/C10-3）+ C10-4/C10-5 + R10-2/R10-3/R10-4 + [[#F-PILOT-11]] 仍 open。

**真因聚類**（見 pitfall）：missed-forward（G2/G6/G7，**optional value silent 漏轉** — contract test 須涵蓋 optional）+ deploy gap（G1/G8/G5）+ transport timing（G3）+ observability（G4）。

**3 道防線 durable（分對應 lane）**：
1. **integration test**（paas-platform operator + image）：render-to-pod-spec（真 `helm template` → assert pod spec 有 imagePullSecrets / `LLM_MODEL_CONFIG` env / NP externalEgress，**含 optional 非只 required**）+ `ensure_registry_secret` real-K8s + contract test 涵蓋 optional。
2. **deploy checklist**（paas-cd）：驗 operator **running image 版本**（非 values pin，G1）+ 渲染 pod spec 真有 env/secret/NP。
3. **observability**（image）：success-path log + degradation error frame（G4 = R10-1 延伸）。

> **與 review takeaway 三層 gate 對應**：G4→R10-1（Tier2 observability，本 cluster e2e **印證其必要**）；G5→C10-1（Tier1 NP，本輪 externalEgress enable 即此 deploy gate）。其餘 G1/G2/G3/G6/G7/G8 為 cluster e2e 新暴露的 durable（charts lane G8 / platform lane G2/G3/G6/G7 / cd lane G1）。

## F-GC-2 — deploy-stg/create.sh 重跑洗掉 odoo agent-runtime 啟用（durability）

- **Severity**: medium（durability；pilot 一次性 OK，但每次重 deploy-stg 後 odoo 端 agent-runtime 會被關掉）
- **Origin**: agent-runtime-pod epic gap C / odoo (i) pilot 部署（2026-05-25，PR #64 + deploy-stg 4a/4b）
- **問題**: odoo 端 agent-runtime 啟用走 (4b) `helm upgrade stg --reuse-values --set agentRuntime.* --set networkPolicy.allowAgentRuntimePods=true`（chart from-values，PR #64）。但 `create.sh`（k8s-dev-sandbox / deploy-stg 用）是 `helm upgrade $SLUG --set <stg-mode 值>` **fresh set、無 `--reuse-values`**（`create.sh:294`）→ **下次 deploy-stg/create.sh 重跑會把 agentRuntime（secret keys）+ allowAgentRuntimePods 洗回 chart 預設（空 / false）→ odoo 端 agent-runtime 被關掉、skill-bundle/auth/namespace secret env 消失**。
- **為何 pilot 接受**: 一次性 e2e 跑 4a→4b 即可、值撐到下次 deploy；每次重 deploy-stg 後重跑 (4b) 即恢復。secret 值刻意不進腳本/CI（敏感、使用者提供）是對的設計。
- **durable 修法（腳本層，非 chart-only）**: (i) odoo agent-runtime secret 改 **existingSecret**（pre-create K8s secret、像 operator 端 paas-cd 慣例）→ create.sh redeploy 不碰、secret 不落 helm values；(ii) `allowAgentRuntimePods=true`（非敏感）做成 create.sh stg-mode 預設 或 stg values file 固定套。
- **owner**: paas-platform（odoo-dev-sandbox chart / deploy-stg lane）+ paas-cd（secret 慣例）；**pilot 驗證後排程**。
- 對照: [[agent-runtime-operator-chart-install-contract]]（§附帶 WS transport / NP）、本檔 F-GC-1（同 pilot 另一 hardening）、F-PILOT-3（同源 deploy-stg stg-mode 重跑 durability；odoo 端另一軸 = image default，本條 = agentRuntime secret env + NP flag）。
- **狀態（2026-05-26 verify）**: **保留 open**。軌 2 durable 收尾（agent-runtime chart `0.1.1` / operator+paas-cd existingSecret / F-PILOT-3 odoo image）**皆未涵蓋本條** —— 實查 develop `create.sh` 仍為 fresh `--set`（HELM_INSTALL_ARGS 僅 `sandbox.branch/ttl/mode` 等，**無** agentRuntime / `secrets.existingSecret` / `allowAgentRuntimePods` / `--reuse-values` 任何 wiring；chart 雖具 `secrets.existingSecret` + `agentRuntime.*` + `allowAgentRuntimePods` 機制，但 stg-mode 部署流程未啟用），F-GC-2 技術結論在現況仍成立、durable 修法 (i)+(ii) 尚未實作 → **不標 RESOLVED**。

## F-GC-3 — `helm.py` argv log 含 repo-read token（credential-in-logs，E0 CAQA 同源掃出）

- **Severity**: low-medium（credential-in-logs hygiene；**非本輪 E0 diff 引入**、非租戶 LLM key）
- **Origin**: Phase E0 CAQA 同源 secret-provisioning sweep 順帶掃出（明標非 E0 阻斷 / 非 caveat）；token 由 gap C `df6348e` 引入
- **問題**: `helm.py:77`（及 `:481` / `:1121`）`logger.info(f"Executing: {' '.join(cmd)}")` log 完整 argv → `helm install/upgrade` 的 `--password <agent_runtime_repo_read_token>`（Gitea **repo-read token**）落 INFO log
- **方向**: redact argv 的 `--password` 值（log 時遮罩）/ 或改 file·env 傳 token 不進 argv
- **owner**: paas-platform(operator)；**非 GA hard gate**（repo-read token scope 有限），但 credential-in-logs 應排程清
- 對照: [[credential-claim-test-fixture-coupling]]（同 epic credential hygiene 紀律）；`docs/tracking/caqa-report-2026-05-27-agent-runtime-pod-phase-e0.md` §同源 code path 檢查 + §out-of-scope follow-up

## F-PILOT-11 — 富 skill args_schema（tool `parameters`）進 spawn snapshot（需 design 排程）

- **Severity**: low（enhancement；**F-PILOT-10 用 single-string `input` fallback 已足夠**跑通 tool 註冊，本條只為「富 args schema」品質升級）
- **Origin**: F-PILOT-10 Gap 3（image Slice 2 skill→StructuredTool 註冊撞契約 gap，2026-05-27 hybrid 拍板）
- **問題**: pod 註冊 StructuredTool 需 `args_schema`（= SKILL.md frontmatter `parameters` JSON Schema）。hybrid 後 snapshot 帶 name/version/sha256/**description**（已補），但**刻意不帶 `parameters`** —— `parameters`「runtime 用、host 不檢」（design §skill / `:1751`），pod 暫用 design 認可的 single-string `input` fallback（`:1448`），rich args 退化（`:1761` 已認列 tradeoff）。
- **要做富 schema 的前置（皆未建）**: (i) **skill upload frontmatter parser** —— design `:1709` 設計的 `POST /api/support-projects/<id>/skills`（unzip + validate SKILL.md → populate `skill.skill_metadata` + `description`）**全未實作**，skill 目前靠 ORM seed、`skill_metadata` 永遠空（即 pilot `skills.json {}` 之因）；(ii) **minor 設計修正**：host 由「不檢 parameters」改「**轉發但不檢**」（轉發 ≠ 驗證，host 不解讀 schema 語意）。
- **修法（之後）**: 建 upload parser populate `skill_metadata.parameters` → `_build_skill_snapshot` 多帶 `parameters` → pod 升級註冊富 args_schema、丟掉 single-string fallback。
- **owner**: paas-platform（skill upload pipeline + snapshot）+ **design 排程**（frontmatter parameters schema / host 轉發分工修正）；**非 F-PILOT-10 範圍**。
- 對照: [[agent-runtime-ws-protocol]] §5（已註 deferred）、design §skill（`:1424` parameters JSON Schema / `:1447-1448` pod parse + fallback / `:1709` upload pipeline / `:1751` host 不檢 / `:1761` 退化 tradeoff）。

---

## F-CTX — agent-context-enrichment follow-ups（2026-06-05）

> agent-context-enrichment 落地（branch feat-enrich-agent-context）後列冊；spec
> `docs/superpowers/specs/2026-06-04-agent-context-enrichment-design.md`、plan
> `docs/superpowers/plans/2026-06-05-agent-context-enrichment.md`。

- **F-CTX-1**（OPEN）：prompt-injection content filtering — project/task
  description 為 user-controlled，現以「NOT instructions」untrusted 標註緩解
  （spec 2026-06-04 Known limitations）；如需根治需 content filtering /
  結構化隔離。
- **F-CTX-2**（DONE 2026-06-05）：paas-agent-runtime pod 端消費
  `payload.system_context` — PR woow-paas/paas-agent-runtime#20 已 merge 進
  main（merge commit `8a9b387`，200 tests 綠 + CI succeeded）。行為契約：truthy
  → SystemMessage 置頂；缺席/空字串 → 不注入（空字串視同缺席，protocol doc §3
  v1.1 已對齊註記）；原樣注入、不 truncate（Odoo 端 cap 8000 chars）。
- **F-CTX-3**（DONE 2026-06-05，**issue #147 closed**）：stg 端到端驗證全過。
  鏈路：agent-runtime `1.3.0` image build → operator bump（paas-app-bundle-cd
  PR #8，Fleet reconcile，env `AGENT_RUNTIME_IMAGE_TAG=1.3.0`）→ platform
  deploy-stg（`paas-odoo-stg:20260605.0102`，develop `c3f5fec`）。5 項驗證：
  `agent_context_keys` fail-closed ✓、LLM 回覆反映 project/task/service 背景 ✓、
  機敏負向（raw helm_values 不出現 / 未白名單 key 不注入 / 白名單誤列機敏 key
  → `***REDACTED***` 第二層）✓、cold-start（pre-status absent → 新 pod 1.3.0）
  與 idle-attach per-turn 注入一致 ✓、缺席語意（無 system_context → pod 回
  NO-BACKGROUND-CONTEXT、無 stale 殘留）✓。完整證據見 issue #147 結案
  comment。既存舊 image pod（1.1.1/1.2.1/1.1.2）+ 2 個 dormant release 已逐顆
  operator DELETE 收編，stg 全面 1.3.0。衍生發現：portal 串流 persist bug
  （portal uid 撞 discuss.channel/mail.message ir.rule → AI 回覆不落 channel）
  → PR #150 修復（`_persist_ai_response` 改 system identity + portal-uid
  HttpCase 回歸測試）+ stg 復驗 3 項綠（落盤+重整可見 / log 0 AccessError /
  internal 路徑不受影響）。

---

# Cycle 6 — skill-list (PR #81 follow-up — is_latest drift fix model 層 + 含瀏覽器 UI e2e, 2026-05-28)

> Cycle 6 完成於 2026-05-28，`c36a9a1..5ac2081` = 8 commits / 10 檔 / +859 -7。CAQA `pass-with-medium-caveats`（high 0 / medium 2 / info 9）+ reviewer `approve with comments`（blocker 0 / major 7 / minor+nit+info 22）。**根因修法** = model `create()` / `write()` hook 收斂 `_recompute_is_latest`（[[invariant-recompute-on-orm-write-hook]] 抽 pattern）+ 配套幂等 migration `18.0.1.0.28` 對既有資料 backfill；**fixture mechanics 雷**（HttpCase raw SQL drift sample 被 buffered ORM write flush 覆蓋）抽 [[httpcase-orm-flush-ordering-raw-sql]] pitfall。skill-related test 41/41 全綠，playwright 4 階段 + 4 screenshot 符合 spec §④-b。F-SK-1~F-SK-2 = CAQA C1/C2 同源 reviewer F1/F2；F-SK-3~F-SK-9 = reviewer F1-F7 全。

## F-SK-1 — winner 演算雙寫雷（model + migration 兩份相同副本，未抽 classmethod）

- **Severity**: medium（CAQA C1 + 同源 reviewer F1 major 二階）
- **Origin**: CAQA Quality Q3 + Architecture A2 同源、Reviewer code-reviewer M1 確認 + comment-analyzer M3 確認；spec §② 拍板可接受（migration 一次性、model 常駐生命週期不同），plan §Task 3 Step 1 備註過 fallback 抽 classmethod 但 implementation 選方案 A 沒抽
- **Location**:
  - `src/models/skill.py:202-220` — `_recompute_is_latest` model 常駐
  - `src/migrations/18.0.1.0.28/post-fix-is-latest.py:19-40` — `migrate` 一次性
- **問題**：兩份結構相同副本（`(published_at desc, id desc)` 取 winner + if-guard 寫 `is_latest`）；演算結果目前等效（`test_migration_fixes_existing_drift_sample` + `TestSkillIsLatestDriftRegression` 各自驗）
- **為何留 caveat**：既有 spec follow-up「semver 精確排序」會改 winner 排序規則 — 規則改一邊忘改另一邊 → fresh sandbox 看似正常、prod upgrade 跑舊 migration → 隱性 drift（fresh deploy vs upgrade deploy 行為不一致）；「兩份相同邏輯」maintainability debt build-up over time
- **建議落點**：
  - 抽 `Skill._compute_is_latest_winner(group_records)` classmethod（純函式、no SQL），model `_recompute_is_latest` + migration 共用
  - 或在本 doc + `_recompute_is_latest` docstring 標 explicit「winner 排序規則演進時 lock-step 改兩處」reminder
  - 補 invariant test 鎖兩條路徑等效（同一份 drift recordset 各跑 model recompute / migration migrate 結果相同）
- **何時**：semver 精確排序 PR 出現時（owner = paas-platform skill team）；不阻擋本 PR ship

## F-SK-2 — hook 對單筆操作 O(group_size) 同步成本（跨 group N+1 放大）

- **Severity**: medium（CAQA C2 + 同源 reviewer F2 major 跨 group 視角）
- **Origin**: CAQA Performance P1 + Reviewer code-reviewer M2 確認
- **Location**: `src/models/skill.py:104-114, 117-130`
- **問題**：hook 每次踩中條件即同步：1 次 SQL search 全 `(project_id, name)` group + Python sort O(group · log group) + per-record if-guard write。對 batch ORM create N 筆同 group published → O(N²)（每次 search group size 越來越大；批匯 50 版本 → ~1225 records processed）；對混 group recordset N 筆（跨 K 個 group）→ N 次 search + sort（admin 多選 50 skills mass action_archive 場景）
- **為何留 caveat**：當前場景（admin 後台逐筆操作 + spec §背景 5「skill 尚未對使用者開放」+ 預期版本數小）影響 negligible；既有 `TestSkillAdminLifecycle` 10/10 + 全 `-u` 41/41 skill-related test 通過驗證 perf 對 test runtime 無感；spec 非目標明示不擴 HTTP 上傳 API
- **建議落點**：
  - 短期：本 tracker reminder「批匯 / mass admin 動作出現時改 group-aware」
  - 中長期 batch upload API 出現 → 新 method 用 `@api.model_create_multi` collect distinct `(project_id, name)` group → 對每 group 一次性 recompute（O(distinct_group) 而非 O(N²)）
  - mass action（如 multi-select archive）→ write override 內 dedupe group key set 後再 recompute（`published_at` 補刷仍須 per-skill）
- **何時**：HTTP batch upload API / mass admin action 排上路線圖時

## F-SK-3 — write hook 對 `deprecated → published` re-publish 不刷新 `published_at`

- **Severity**: major（reviewer F1，**非阻斷 ship**）
- **Origin**: reviewer code-reviewer M1
- **Location**: `src/models/skill.py:125-129`
- **問題**：`action_publish` 一律 stamp `published_at = fields.Datetime.now()`（`skill.py:168-171`）；繞過 transition 走 `skill.write({'state': 'published'})` 時，hook 的 `if not skill.published_at` guard 對「已有舊 timestamp 的 deprecated」**不刷新** → 兩條路徑對同操作（re-publish a deprecated skill）給出不同 `published_at`，影響 `_recompute_is_latest` 的 `(published_at desc, id desc)` 排序語意
- **與 spec 對照**：spec §① 字面是「補 published_at（只在缺時補）」與 hook 邏輯一致 → spec 已 take 此差異；reviewer 提醒「reader 容易誤改」
- **建議落點**：
  - (a) 加 regression test `test_write_state_to_published_from_deprecated_keeps_old_published_at` 顯式鎖語意
  - (b) 或在 `_recompute_is_latest` docstring 明寫「兩條路徑可能對 deprecated→published re-publish 得到不同 winner」
  - 兩者擇一即可；推薦 (a)（test 比 docstring 強）
- **何時**：next touch `src/models/skill.py` 順手；不獨立 PR

## F-SK-4 — write override 對混 group recordset N+1 放大

- **Severity**: major（reviewer F2，**非阻斷 ship**；與 F-SK-2 同源 perf，**不同視角**：F-SK-2 是單 group batch；F-SK-4 是跨 group recordset）
- **Origin**: reviewer code-reviewer M2
- **Location**: `src/models/skill.py:117-130`
- **問題**：`for skill in self: skill._recompute_is_latest(...)` 對 self=N 筆來自 K 個不同 `(project_id, name)` group 的 recordset → N 次 search + N 次 sort。idempotent 安全、worst-case 跨 group 放大。具體場景：admin 多選 50 skills mass action_archive → `write({'state': 'archived'})` 對 self=50 → 50 次 recompute（多數 if-guard no-op）
- **建議落點**：dedupe group key（dedupe `(project_id, name)` set 後再 recompute；但 `published_at` 補刷必須 per-skill —— 拆兩 loop 或在同一 loop 內分別處理）
- **何時**：mass admin action 路線圖出現時與 F-SK-2 一併處理

## F-SK-5 — playwright 截圖路徑硬編 `/tmp` 不可攜

- **Severity**: major（reviewer F3，**doc maintainability**；多平台 CI 才會顯）
- **Origin**: reviewer comment-analyzer M1
- **Location**: `tests/e2e/skill/skill-admin-form.spec.ts:34` (`const SCREENSHOT_DIR`) + jsdoc `:15` + `tests/e2e/skill/README.md:40-44, 56-61`
- **問題**：POSIX-only 假設；註解未標 caveat；Windows / 部分 CI 沒 `/tmp` 直接炸；jsdoc 與 README 把 `/tmp/skill-e2e-screenshots` 寫死當「截圖位置」事實
- **建議落點**：
  - jsdoc / README 標 caveat「目前僅 POSIX；Windows 需 env override」
  - 或改 `process.env.SCREENSHOT_DIR ?? path.join(os.tmpdir(), 'skill-e2e-screenshots')`、同步更新註解
- **何時**：next playwright spec touch 順手；Windows / 跨 OS CI 排上來時必修

## F-SK-6 — README / spec 註解「leader 強調」口頭歸屬入庫

- **Severity**: major（reviewer F4，**doc maintainability**；一年後讀者無從定位）
- **Origin**: reviewer comment-analyzer M2
- **Location**: `tests/e2e/skill/README.md:67` + `tests/e2e/skill/skill-admin-form.spec.ts:22-25`
- **問題**：「leader 強調曾因此誤判 action_deprecate」這類**當下對話脈絡**入庫後，半年後讀者沒有 channel 可定位「哪個 leader / 哪場討論 / 哪次誤判」。README 是**正式文件**、不該保留口頭歸屬
- **建議落點**：改寫成事實陳述，e.g.「曾發生過：sandbox pod 跑舊 code → 修 model 後沒 rollout restart → 透過瀏覽器測 action_deprecate 看到舊行為而誤判邏輯有 bug」；或刪脈絡只留 pitfall（轉引 [[httpcase-orm-flush-ordering-raw-sql]] 或新建 sandbox rollout pitfall）
- **何時**：next playwright spec touch 順手；不獨立 PR

## F-SK-7 — 測試註解硬編「PR #81 / Plan §Phase C Task 13」脆弱 anchor

- **Severity**: major（reviewer F5，**doc maintainability**；PR 編號 / plan task 編號為平台或修訂 artifact，會漂移失效）
- **Origin**: reviewer comment-analyzer M3
- **Location**:
  - `src/tests/test_skill_is_latest_drift_regression.py:5`
  - `src/tests/test_skill_e2e_lifecycle_download.py:16`
  - `src/tests/test_agent_pod_phase_c_skill_bundle.py`（既有但被本 cycle 新註解延續）
- **問題**：PR #81 / plan task 編號是平台層 artifact，時間久了：(a) PR 被搬到 issue tracker / 別的平台 → URL 死；(b) plan 檔名 / 章節重命名 → anchor 失效。讀者看 code 註解只能 grep 個 PR # 對不上現況
- **建議落點**：改用 in-repo anchor：
  - 「既有 `TestSkillAdminLifecycle` 已覆蓋」（用 class 名 + 檔名）
  - 「見 `docs/decisions/patterns/invariant-recompute-on-orm-write-hook.md`」（用 doc 相對路徑）
  - plan task 引用補 stable summary（不只引 `§Task X` anchor，順帶寫該 task 的一句意圖）
- **何時**：next touch 各檔順手；不獨立 PR

## F-SK-8 — playwright `loginAdmin` `.catch(() => {})` 過寬吞錯

- **Severity**: major（reviewer F6，silent-failure）
- **Origin**: reviewer silent-failure-hunter major-1
- **Location**: `tests/e2e/skill/skill-admin-form.spec.ts:39`
- **問題**：`await page.fill('input[name="db"]', DB_NAME).catch(() => {})` 無條件吞掉**任何**例外（selector timeout / element disabled / network crash），會導致 debug 錯位（後續 fill 報的錯不是 root cause）
- **建議落點**：narrow 到「element 不存在」：
  ```ts
  const dbField = page.locator('input[name="db"]');
  if (await dbField.count() > 0) await dbField.fill(DB_NAME);
  ```
- **何時**：next playwright spec touch；F-SK-9 一併處理

## F-SK-9 — playwright `getByText.first()` silent wrong-row click

- **Severity**: major（reviewer F7，silent-failure）
- **Origin**: reviewer silent-failure-hunter major-2
- **Location**: `tests/e2e/skill/skill-admin-form.spec.ts:52`
- **問題**：`await page.getByText(skillName, { exact: false }).first().click();` — list 同 name 多版本 / cross-test pollution row 都會被 `first()` 吃到、click 成功**但開到錯 form** → 後續 assertion 失敗訊息誤導
- **建議落點**：
  - `SKILL_E2E_NAME` 帶 unique suffix（與 backend test 同 pattern `uuid.uuid4().hex[:8]`）
  - 或 `exact: true` / 由 fixture 注入 record id 走 data-id selector
  - 加 `expect(await page.getByText(...).count()).toBe(1)` 顯式守線（不留 silent 漂移空間）
- **何時**：與 F-SK-8 一併處理

## F-SDS-1 — sibling model `mcp_server` 缺 scope-flip `write()` guard（PR #96 修了 skill、mcp_server 漏）

- **Severity**：HIGH（security — 跨租戶提權 / 平台公開外洩）
- **Origin**：PR #96（skill dual-scope）上 stg 後的對抗式複查 workflow（3 agents，2026-05-30）；prior workflow 已標為 follow-up，本次確認可利用性（skeptic `refuted:false` 高信心）。
- **根因**：PR #96 在 `src/models/skill.py:150-167` 補了 `write()` guard，擋住「非 admin 把自己 `scope='user'` 的 skill flip 成 `scope='system'`（平台公開、載進每個租戶 pod）」。sibling model `woow_paas_platform.mcp_server`（`src/models/mcp_server.py`）有**完全相同的形狀**：`scope` Selection（system/user，line 49-57）、`project_id` M2O（line 58-64）、同一條 `@api.constrains _check_scope_project_consistency` D4 不變式（line 118-132）—— 但**沒有 `write()` override，import 只有 `ValidationError` 沒有 `AccessError`**。
- **攻擊路徑（raw ORM / `call_kw` RPC，已確認 exploitable）**：
  - `base.group_user` 租戶對自己擁有的 `scope='user'` mcp_server 呼叫 `write({'scope':'system','project_id':False})`。
  - `ir.model.access.csv:14`（`access_mcp_server_user`）給 `base.group_user` `perm_write=1`。
  - 唯一 write-applicable 的 `mcp_server_rule_workspace_user`（`ir_rules.xml:156-172`）domain AND 在 `('scope','=','user')` 上；Odoo 用 **pre-write** 狀態判定 write rule（仍是 `scope='user'`、owner 是 workspace member/follower）→ 放行。post-write `scope='system'` 不再 re-check。
  - `@api.constrains` 只檢查 scope↔project_id 互斥一致性，`{scope:'system', project_id:False}` 內部一致 → 不擋（僅「提示」攻擊者也要清 project_id，而那筆 write 已一起做了）。
  - 結果：user-scope row 變 `scope='system'`，被 `mcp_server_rule_system_readonly`（`ir_rules.xml:134-143`）對全體 `base.group_user` 可讀 → 跨租戶外洩。與 skill.py:150-167 剛關掉的洞同型。
- **endpoint 為何安全（不阻斷現況）**：`_update_mcp_server`（`paas.py:1377-1398`）的 writable-field allowlist（`paas.py:1393`：`name/url/transport/api_key/headers_json/description`）丟掉 client 的 scope/project_id；`_create_mcp_server` 硬編 `scope='user'`/`project_id=project.id`（`paas.py:1372-1373`）；且 recordset `.sudo()`（`paas.py:1364,1382`）。暴露面 = raw ORM RPC + 任何未來 forward scope 的 endpoint。
- **建議落點**：fix PR — 在 `src/models/mcp_server.py` 鏡像 `skill.py:150-167`：import 改 `from odoo.exceptions import AccessError, ValidationError`（line 6），於 `_check_scope_project_consistency` 後（~line 133）加 `write()` guard（`('scope' in vals or 'project_id' in vals) and not self.env.su and not self.env.user.has_group('base.group_system')` → 逐筆比對實際值變動 raise `AccessError`）。對現有 endpoint 安全（`.sudo()` → `env.su=True` → 跳過 guard），對 `action_sync_tools`/`action_test_connection` 安全（只寫 state 欄位）。
- **連帶**：查 `mcp_tool` 是否能被 re-point 到 system-scope `server_id`（若可，同型風險，鏡像 guard）。補 ORM `with_user()` 負向測試（鏡像 `test_skill_dual_scope.py:165` `test_group_user_cannot_flip_user_skill_to_system`：assert flip raise `AccessError` + 單欄位變體 + no-op write 不 raise）+ HttpCase 對等，per [[orm-vs-endpoint-acl-test-coverage]]。
- **pattern**：[[scope-flip-write-guard-not-ir-rule]] 明文要求的 sibling —— mcp_server 是 skill 修補的缺漏雙生；亦與 F16（mcp_server/mcp_tool rule-count parity lint）同源「sibling model parity」家族。
- **✅ RESOLVED（PR #99 → develop merge `800cc1c`，2026-05-31）**（https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/99）：
  - `src/models/mcp_server.py:5` import `AccessError`；`mcp_server.py:134` 加 `write()` guard 鏡像 `skill.py:150-167`（`('scope' in vals or 'project_id' in vals)` and not `env.su` and not `group_system` → 逐筆比對實際值變動 raise `AccessError`）。
  - **連帶查兌現**：`mcp_tool` 確認可 re-point（ir.rule pre-write 放行 + perm_create=False forgery 阻擋被繞）→ `src/models/mcp_tool.py:2` import `AccessError` + `write()` guard。**對抗式自審加嚴**：不只擋 re-point 到 system-scope（載進全租戶），亦擋 re-point 到他人 workspace 的 user-scope server（注入單一 victim，victim AI 會載入）—— 合法流程從不 re-point tool（`action_sync_tools` 寫 `server_id=self.id` 為 no-op；grep 確認無 controller/wizard 改 `server_id`），故對非 admin 一律擋「`server_id` 實際變更」。
  - **測試** `src/tests/test_mcp_scope_flip_guard.py`（13 test，註冊於 `src/tests/__init__.py`）：`TestMcpServerScopeFlipGuard`（11，鏡像 `test_skill_dual_scope.py:165` fixture 取自已驗證為綠的 `test_mcp_acl_isolation.py`）= full-flip + scope-only + project-clear 單欄位變體 + no-op 放行 + admin/sudo boundary + mcp_tool re-point〔system + 跨租戶 user〕；`TestMcpEndpointScopeFlipDefense`（2 HttpCase）= legacy endpoint allowlist 丟 client scope/project_id + create 硬編 user-scope（per [[orm-vs-endpoint-acl-test-coverage]] endpoint 對等守線）。
  - **驗證**（docker-desktop k8s-dev-sandbox `dev-sandbox-fix-issue-98`）：**RED** = 4 安全測試 `AssertionError: AccessError not raised`（full flip + tool re-point）/ `ValidationError`（單欄位被 D4 擋而非 guard）；**GREEN** = 13/13 pass；**完整套件** `-u woow_paas_platform --test-enable --test-tags woow_paas_platform` = 782 test，零 mcp/skill 回歸（11 個失敗皆 pre-existing 環境性：smart_home 缺 `base.user_demo` / ecpay TWD 非活動貨幣 / oauth2 PKCE MagicMock / SSE5 flag）。
  - **完整性**：grep 確認全 repo 僅 `skill` + `mcp_server` 兩 model 有 `scope`(system/user) Selection + `project_id` M2O 形狀，皆已 guard；無第三遺漏 sibling。

## F-SDS-2 — `src/__init__.py:4` 無條件 `from . import tests`（production 載入 test framework）

- **Severity**：low（cleanup / startup hygiene；非阻斷）
- **Origin**：PR #96 上 stg 報告 finding #1（非阻斷啟動 ERROR/告警）。
- **根因**：`src/__init__.py:4` 無條件 `from . import tests`，而 `src/tests/__init__.py` import 83 個 test module（含 `.integration`）→ production 每次啟動都 import 整套 test framework（`odoo.tests.common` 等），emit 告警。
- **為何可安全移除（已驗證）**：Odoo 18 test loader 自己 import 模組的 `tests` subpackage（`odoo/tests/loader.py` `_get_tests_modules` → `importlib.import_module('.tests', ...)`），不依賴根 `__init__.py`。所有 Odoo 18 core addon（web/project/mail/bus/sale/account）根 `__init__.py` 皆 0 個 test import。移除後 `--test-enable` 仍收集到 83 個 test module（discovery 只依賴 `src/tests/__init__.py` 的 `from . import test_*`，該檔不動）。CI（`.gitea/workflows`）/ k8s-dev-sandbox `test.sh` / manifest 皆不依賴此行。
- **建議落點**：小 PR — 刪 `src/__init__.py:4`（**勿**動 `src/tests/__init__.py`）。驗證：`./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags woow_paas_platform --stop-after-init` 仍收集到 test。
- **本次處理**：driver 拍板還原 verification agent 自作主張的刪行（恢復原狀），改列冊由獨立小 PR 處理。
- **✅ RESOLVED（PR #99 → develop，2026-05-31）**：刪 `src/__init__.py:4`（未動 `src/tests/__init__.py` 的 `from . import test_*`）。與 F-SDS-1 同 PR（#99）落地，docker-desktop sandbox `-u woow_paas_platform --test-enable --test-tags woow_paas_platform` 實測 **782 test 仍全數收集、模組正常載入**——印證 Odoo 18 test loader 自 import `tests` subpackage、不依賴根 `__init__.py`。

## F-SKU-1 — Skill upload wizard 後端 list view 空狀態無上傳入口（仿 MCP「New」放固定 Upload 按鈕）

- **Severity**：low（UX / discoverability；非阻斷——agent-runtime API 仍可上傳）
- **Origin**：manual stg 探索（2026-05-31，stg.woowtech.io，playwright-cli 實測截圖）；driver 拍板列冊 + 開 issue #102。
- **根因**：Skills list view（`view_skill_list` / action `action_skill` / `/odoo/action-542`）刻意設 `create="false"`（`src/views/skill_views.xml:8`），skill 不直接手建；上傳改走 `action_skill_upload_wizard`（`src/views/skill_views.xml:148-155`），以 `binding_model_id` + `binding_view_types="list,form"` 綁定為 contextual action。Odoo 18 的 binding contextual action 在 list view **只掛進「選取記錄後」才出現的「⚙ Actions」選單**，不在無選取時 breadcrumb 旁的齒輪選單（該選單僅「Export All」）。
- **後果**：列表為空（一筆 skill 都沒有）時，沒有記錄可勾選 → 永遠不會出現「Actions」按鈕 → **後端完全沒有上傳第一個 skill 的入口**，只能先靠 agent-runtime API（`/api/agent-runtimes/skill-bundle`）灌第一筆。但 list view 的 nocontent help（`action_skill` 的 `help`，`src/views/skill_views.xml:91-100`）寫「No skills yet」，給人「可從此開始上傳」的錯覺。
- **stg 實測**（已有一筆 hello-world skill）：breadcrumb 齒輪選單 = 僅「Export All」❌；勾選 hello-world 後的「⚙ Actions」選單 = Export / Delete / **Upload Skill** ✅。
- **建議落點**：fix PR — 參考 MCP Servers list view（左上固定「New」按鈕 + 「Add your first MCP Server」空狀態 help），在 `src/views/skill_views.xml` 的 `view_skill_list` 放一顆**不依賴選取**的固定按鈕「Upload Skill」（`type="action"` 指向 `action_skill_upload_wizard`），讓空狀態也能上傳第一個 skill。退而求其次：在 `action_skill` 的 nocontent help 內放一個直接開 wizard 的按鈕／連結救空狀態。
- **彙總 issue**：#102（https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/102）。
- **狀態：RESOLVED**（2026-05-31，PR #103 → develop，branch `fix/F-SKU-1`）：在 `view_skill_list` 加 `<header>` + `display="always"` 的「Upload Skill」按鈕（`type="object"` → 新增 `Skill.action_open_upload_wizard()` 回傳 wizard `act_window` dict），不依賴選取即可開 wizard，補上空狀態入口；同步改寫 `action_skill` nocontent help 指向新按鈕。落地選 `type="object"`（非建議落點原寫的 `type="action"`）以對齊 repo 既有 working precedent（`cloud_odoo_addon_views.xml:58-59` 的 `sync_from_catalog` header 按鈕）並避開 list arch 內 action xmlid 同檔 forward-reference load-order 風險。Regression test 補於 `test_skill_admin_views.py`（按鈕存在且 `display='always'` + 方法回傳 modal action）。靜態驗證（py_compile + XML well-formed + 4 面向對抗式 review 全 pass）+ **runtime 驗證通過**（docker-desktop k8s-dev-sandbox local 模式真 Odoo 18，模組含 6 個第三方 deps 乾淨安裝；`TestSkillAdminViews` 4 test **0 failed / 0 error**，含新 `test_skill_list_has_always_visible_upload_button` + `test_open_upload_wizard_returns_modal_action`；全套件其餘 2 failed + 10 error 皆在無關子系統〔oauth2 PKCE / ecpay invoice / smart home ACL / agent-runtime SSE flag / router alias〕的 fresh-sandbox 環境敏感既有 baseline，與 F-SKU-1 改動正交）；瀏覽器 E2E 由使用者依需要自跑。

## See also

- [[cloud-service-project-decouple]] §Consequences — cycle 1 決策後仍要持續注意的負面影響
- [[mcp-project-decouple]] §Consequences + §Round 2 改動敘事 — cycle 2 決策後仍要持續注意的負面影響
- [[mcp-url-redesign]] §Consequences + §Round 2 改動敘事 — cycle 4 決策後仍要持續注意的負面影響
- [[odoo-addon-std-rule-symmetric-deactivation]] — F1 / F13 的 doc 形式
- [[cross-workspace-task-data-leak]] §Hardening 建議 — F5 的更完整 context
- [[direct-unlink-bypass-cleanup-hook]] — F14 的根因 context
- [[system-mcp-create-acl-bypass]] — F16 的根因 context
- [[tag-scoped-test-misses-full-u-failures]] — F19 「test coverage hole」cluster 的根因 context
- [[two-layer-acl-asymmetric-deny]] — F-X2 的根因 context（cycle 4 HC-2）
- [[legacy-endpoint-scope-blindness]] — cycle 4 HC-1 的根因 context（plan AC scope blindness）
- [[constraint-input-trust-expansion]] — F-PA-15 的根因 context（cycle 5 round 1 HC-3 同 family — m2m write constraint grantor authority check；Phase B HC-2 是其 image 端同型 instance）
- [[attachment-row-level-acl-and-anchor-stamping]] — F-PA-16 的根因 context（cycle 5 round 1 HC-4 — bundle attachment defense in depth；Inv A2 = Phase B pod 零直連 ir.attachment 守線的 woow_paas_platform 側依賴）
- [[invariant-recompute-on-orm-write-hook]] — F-SK-1~F-SK-4 的根因 context（cycle 6 — derived flag / 不變式不能只在 user-facing transition method 維護；ORM `create()` / `write()` hook 兜底所有寫入路徑；avoid recursion = 內部 write vals 不含 trigger key；本次 PR 落地的 pattern）
- [[scope-flip-write-guard-not-ir-rule]] — F-SDS-1 的根因 context（PR #96 在 `skill.py:150-167` 兌現的 pattern；sibling model `mcp_server` 缺同款 `write()` guard = 缺漏雙生，raw ORM scope-flip 跨租戶提權）
- [[orm-vs-endpoint-acl-test-coverage]] — F-SDS-1 的 test 守線 context（endpoint allowlist 安全 ≠ ORM 安全；fix 需補 `with_user()` 負向 + HttpCase 對等）
- [[httpcase-orm-flush-ordering-raw-sql]] — cycle 6 fixture mechanics 雷（HttpCase 內 `action_publish` 後接 raw SQL UPDATE 製造 drift sample 被 buffered ORM write flush 覆蓋；`invalidate_recordset()` 只清 cache、不清 buffer；修法：raw SQL 前後夾 `flush_all`）
- `docs/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md` — Phase B image source 完成 durable（cross-phase learnings：CI gate 三度價值 / HC-2 cross-phase 驗證 / 2 process learning）
- [[ci-workflow-container-missing-node]] — Phase B B-5 抽 pitfall（CI `container: <lang-image>` + checkout → node exit 127 false green）
- `docs/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md` — Phase C wiring 收口 durable（cross-phase learnings：C-1 disputed severity 第三條路 MOOTED / cross-repo `token_use` 三端 sign-first-then-verify zero-downtime / phase-versioned test 第二次）
- [[phase-versioned-test-assertion]] — Phase C M.9 第二次撞牆抽 pattern（phase test 勿硬編當時 schema 值，用 monotonic `>=` / 存在性斷言）
- `paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md` — Phase B → C/D carry-over canonical home（cross-repo；SC-1~SC-9 + wiring assumptions）
- `paas-agent-runtime` repo `runtime/transport/auth.py` + PR #2 `ba6b417` — Phase C C-1 WS 側對稱補丁（token_use B transitional，F-PC-7 A 嚴格收緊前置）
- `docs/tracking/agent-runtime-pod-phase-d-enable-gate-2026-05-25.md` — Phase D enable-gate hardening 收口 durable（epic A-D code-ready 完成；3 takeaway：forward-coupling test fixtures / cross-repo charts 落點 / HC-2 trust-expansion 第 4 度）
- [[credential-claim-test-fixture-coupling]] — Phase D takeaway 抽 pattern（硬化共用 token/secret/api_key 路徑 → 自建該 credential 的 test fixture 須 lock-step 補 claim；Phase B/C/D 3 建立點）
- [[cross-repo-artifact-landing-assumption]] — Phase D takeaway 抽 pitfall（plan 假設 artifact 落 repo X，實際落 repo Y；impl 前勘查 catch）
- `paas-agent-runtime` repo F-PC-7 WS verifier A 嚴格（`a8b3ce2`+`dabb275`）— **[2026-05-26 更正] 已 merge main（PR #3 `bb31676`，origin/main `3263352`/PR#4）+ build 進所有發佈 image（unconditional 無 toggle），stg closed**；prod operator 全簽 gate = 未來式（見 prod 上線 runbook）
- 原始 review（合成來源，本 doc 引用不複製）：
  - `docs/decisions/reviews/caqa-2026-05-16.md` §Caveat 清單（M1-M4 + L1-L3 — cycle 1）
  - `docs/decisions/reviews/caqa-2026-05-18.md` §Caveat / Out-of-scope（C1 + F12 — cycle 2）
  - `docs/decisions/reviews/pr-review-verdict-2026-05-17.md` §Recommendation（reporter follow-up 1-9 整合 — cycle 1）
  - `docs/decisions/reviews/pr-review-verdict-mcp-2026-05-18.md` §Minor / Info（17 條 round 1 minor — cycle 2）
  - `docs/decisions/reviews/pr-review-verdict-mcp-round2-2026-05-18.md` §新 minor / info（R2-1 ~ R2-4 — cycle 2 round 2）
  - `docs/decisions/reviews/pr-description-draft.md` §Follow-up（cycle 1 候選清單）
  - `docs/tracking/caqa-report-2026-05-20-project-detail-page-issue-48.md` §Blocker + §Caveat（B-1 + C-1..C-3b — cycle 3）
  - `docs/tracking/reviewer-report-2026-05-20-project-detail-page-issue-48.md` §Hard caveats + §Soft caveat tracked（R-1 ~ R-10 — cycle 3）
  - `docs/tracking/caqa-report-2026-05-20-mcp-refactor.md` §Medium / Low findings（M1 / M2 / L1-L4 — cycle 4）
  - `docs/tracking/reviewer-report-2026-05-20-mcp-refactor.md` §Hard Caveats + §Soft Caveats + §Appendix A round 2 closure（HC-1/2/3 + SC-1~SC-20 + NM-R1 — cycle 4）
  - `docs/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md` §6 Blocker + §7 Caveat + §13 Round 2 verify + §14 Round 3 HC-3/HC-4 verify（B-1 + M-1~M-5 + I-1~I-6 + C-A-1~C-A-3 — cycle 5）
  - `docs/tracking/reviewer-report-2026-05-22-agent-runtime-pod-phase-a.md` §3 4 HC Hard Caveats + §5 28 SC Soft Caveats + §12 Round 2 HC-3/HC-4 closure（HC-1~HC-4 + SC-1~SC-28 + EC-3a/EC-4a — cycle 5）
  - `docs/tracking/caqa-report-2026-05-24-agent-runtime-pod-phase-c.md` §7 C-1 disputed + §8 caveat（C-1~C-6/A-1/P-1/P-2 — Phase C）/ `docs/tracking/reviewer-report-2026-05-24-agent-runtime-pod-phase-c.md` §5 C-1 hardening + §6 cross-check + §7 N-1~N-5 + §12 Phase D gate（Phase C）
  - `docs/tracking/caqa-report-2026-05-25-agent-runtime-pod-phase-d.md` §四面向 + §Caveat（D-CAVEAT-1/2 + 9 info + 3 carried-forward — Phase D）/ `docs/tracking/reviewer-report-2026-05-25-agent-runtime-pod-phase-d.md` §4 HC-2 airtight + §6 R-1~R-5 + §7 carried-forward（Phase D）
  - `docs/tracking/caqa-report-2026-05-27-agent-runtime-pod-phase-e0.md` §四面向 + §Caveat（E0-CAVEAT-1/2/3 + out-of-scope — Phase E0 operator green）/ `docs/tracking/reviewer-report-2026-05-27-agent-runtime-pod-phase-e0.md` §5 兩分支 + §6 R-1~R-5（Phase E0）
  - `docs/tracking/caqa-report-2026-05-27-agent-runtime-fpilot10-code-prs.md` §四面向 + §Caveat（C10-1~C10-5 + 4 info — F-PILOT-10 真 LLM）/ `docs/tracking/reviewer-report-2026-05-27-agent-runtime-fpilot10-code-prs.md` §5 C10-1 NP gate + §6 R10-1~R10-4 + §11 三層 gate（F-PILOT-10）
  - `docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md` §Acceptance + §設計 + §非目標（cycle 6 spec）/ `docs/superpowers/plans/2026-05-28-skill-e2e-and-is-latest-drift-fix.md` §Task 1-4（cycle 6 plan）/ `docs/tracking/test-reports/2026-05-28-skill-e2e-test-coverage.md` §第 3 章三層分工 + §第 4 章不測什麼 + §第 6 章已知偏差（cycle 6 test 意圖層說明）— CAQA / reviewer 報告為本 cycle ephemeral，路徑見上方 Cycle 6 段落（不在 in-repo doc，PR 完成 / branch 清理後消失）

### Portal task agent chat follow-ups（2026-06-05）

- **F-PORTAL-1 — portal invoker_role 'guest' 語意（design note，medium-forward）**：portal user 走 chat 時 `_get_project_access_role` 回 `'guest'`（無 project_access row）→ spawn payload `invoker_role='guest'`。design §4.3 矩陣定義 guest 為 read-only（不可 invoke / 不可觸發 spawn），但 controller 與 pod 目前皆未 enforce，portal chat 因此可用。**若未來 pod 端開始 enforce invoke 矩陣，portal user 需要明確 role 對映**（或 share 時自動建 project_access row），否則 portal chat 會**無錯誤訊息地靜默失效**（同 [[agent-runtime-stg-enablement-gap-chain]] class 失敗模式）。owner：paas-platform + paas-agent-runtime lock-step。來源：portal-task-agent-chat plan Task 9。
- **F-PORTAL-2 — deny / not-found 統一錯誤訊息未實作（low，spec 偏差列冊）**：spec §安全模型 5 要求「deny 與 not-found 回同樣錯誤防 ID 列舉」，但既有 `/api/ai/*` 實際區分 `Channel not found` / `Access denied`（spec 對現況描述有誤；plan §風險 6 已自承維持現狀）。影響：portal user 可探知某 channel/project id 存在但無權（輕微存在性洩漏）。統一化會波及 /woow 前端 `ERROR_MESSAGES` 對映與既有測試 → 留待專輪收口。spec 已加實作後記對齊。
- **F-PORTAL-3 — portal chat UI 與 AiChat 的共用層第二階段（low，rule-of-three 待機）**：本輪抽出 `ai_stream.js`（SSE 協議 consumer）+ 直接共用 `ai_service` / `markdown_parser` / `html_sanitize` / `agent_pod_state`；**訊息列表 push / scrollToBottom / streaming 暫存渲染 / 中斷 salvage 仍在 `PortalTaskChat` 與 `AiChat` 各一份**（final review medium），且 portal 端刻意無自動重連（scope 縮減，spec 實作後記記錄）。第三個 chat surface 出現時（如後台 form chat 分頁 epic）再抽 `ai_chat_core` 完整版，避免過早抽象。

## F-SA — Security Access P1 follow-ups（2026-06-07）

> Security Access P1 真 Cloudflare E2E（deploy-stg 在 dev-sandbox-stg）抓到的兩條，
> 性質皆**超出 SA P1 範圍**——是 stg 環境設定 + 平台跨服務命名慣例層，非 SA code bug。
> SA P1 本身判定完成（真環境 5/6 PASS、資源在 woowtech.io 正常運作、清理鏈乾淨）。
> 決定性約束：**Cloudflare universal/wildcard SSL 只覆蓋一層 `*.woowtech.io`，
> `*.stg.woowtech.io`（sub-subdomain）沒有有效憑證**——stg 服務只能落在 `*.woowtech.io`，
> stg/prod 隔離靠「名稱前綴」而非 subdomain 層級。

- **F-SA-1**（OPEN，**priority:high**，scope=stg 環境 config / **跨服務**，gitea **#162**）：
  **stg Odoo `paas_domain` 應從 `stg.woowtech.io` 改為 `woowtech.io`**。根因：`*.stg.woowtech.io`
  無 Cloudflare wildcard SSL，stg 把 service hostname 放在 `.stg.woowtech.io` 那層本就拿不到
  有效憑證。SA provision 把它暴露出來——`create_tunnel` 收 Odoo 全名建了
  `.stg.woowtech.io` DNS（23:39:29），同一次 `PUT /routes` reconcile 因 operator
  `CLOUDFLARE_DOMAIN=woowtech.io` 把它刪掉改建 `.woowtech.io`（23:39:30）→ Odoo 顯示的
  `primary_hostname` 懸空、實際 DNS/ingress 全在 `*.woowtech.io`。**不只 SA**：stg 上所有走
  tunnel 的服務（smart home `create_tunnel` 全名、cloud service MCP route）都受同一 domain
  不一致影響（smart home 在 stg 也是沒 SSL 的 `.stg`）。prod 無此問題（Odoo 與 operator
  皆 `woowtech.io`）。修法：stg Odoo `ir.config_parameter['woow_paas_platform.paas_domain']`
  → `woowtech.io`（dev-sandbox-stg 的 sandbox/Helm values 或 Settings）。owner=deploy-stg /
  stg 環境維運。prod 不動。

- **F-SA-2**（OPEN，**priority:high**，scope=平台跨服務命名慣例 + SA code 硬化，gitea **#163**）：
  **(a) 環境前綴命名慣例**：stg/prod 共用 `woowtech.io` zone（SSL 約束所致），需靠
  hostname **名稱前綴**區分環境，方向 `paas-stg-sa-{hash}` / `paas-stg-sm-…`（operator
  namespace 已是 `paas-ws-stg-`，但 Cloudflare hostname 這端尚未統一加 stg）。屬
  `services/naming.py` 的 `paas-cs-`/`paas-sm-`/`paas-sa-` 三者共同的跨服務慣例，宜**整批**
  讓 naming 吃環境前綴（從 config_parameter / operator namespace prefix 推導），**SA 單獨先加
  會與其他未標記服務不一致**，故不在 SA P1 內單做。**(b) SA code 硬化（併入 a 一起做）**：
  SA provision 打 operator 兩次有**兩個 domain source of truth**——`create_tunnel` 用 Odoo
  傳的全 hostname、`PUT /routes` 只傳 prefix 由 operator 接自己的 `CLOUDFLARE_DOMAIN`，兩值不等
  即分歧（F-SA-1 的直接成因）。根治：讓 `sync_tunnel_routes` / `PUT /api/tunnels/{id}/routes`
  也吃 **Odoo 傳的全 hostname**（與 `create_tunnel` 一致），SA 全程以 Odoo domain 為單一
  source of truth、operator `CLOUDFLARE_DOMAIN` 設什麼都不影響 SA。一旦 (a) 的環境前綴落地、
  全 hostname 要一起傳，故 (a)(b) 合併一個 PR 做免兩次白工。owner=platform（本 repo addon +
  operator）。次要 nit（同 PR 順手）：① CF tunnel soft-delete 後 operator `GET` 仍回 200
  inactive（未映射 deleted_at，觀測性）② route `to_dict()` 不含 `dns_record_id`（DB 有存）。

## F-SA — Security Access P2 Access Control follow-ups（2026-06-08）

> P2 Access Control（Cloudflare Zero Trust）live stg 寫路徑 E2E 抓到的 token scope 前置 +
> operator delete-path bug（後者已修，見 [[operator-cf-access-delete-202-mock-contract]]）。

- **F-SA-3**（**RESOLVED for stg**，2026-06-08；scope=Cloudflare token 前置）：
  **P2 Access 寫路徑需「Access: Apps and Policies (Edit)」在帳戶 (Account) scope，不是區域 (Zone)**。
  operator 的 Access app/policy 是帳戶級 endpoint（`/accounts/{aid}/access/{apps,policies}`）。
  stg token `woow-paas-stg` 一開始把該權限加在**區域**層 → create policy 回 `code 10000
  Authentication error`；改成**帳戶**層後 live 寫測全綠。IdP 列表也是帳戶級，需「Access:
  Organizations, Identity Providers and Groups (Read)」（亦已加）。stg token 現有：Account
  Tunnel(Edit) + Account Access:Org/IdP(Read) + Account Access:Apps&Policies(Edit) + Zone
  Email Routing(Edit) + Zone DNS(Edit)。**留檔供 prod 與其他環境對照**。

- **F-SA-4**（**token scope DONE 2026-06-08；operator bump 仍 OPEN**，priority:high，scope=prod 上線前置）：
  **prod 要啟用 P2 Access 前置兩件，token 部分已完成。**
  - ① **prod token scope（✅ DONE 2026-06-08）**：prod Cloudflare token `woow-paas`（**與 stg
    `woow-paas-stg` 不同 token**）已設帳戶級「Access: Apps and Policies (Edit)」+「Access:
    Organizations/IdP (Read)」+ Tunnel(Edit) + Zone DNS/Email(Edit)，與 stg 已驗通設定一致
    （視覺比對；未 live 驗，因 prod operator 尚無 Access endpoint，見 ②）。性質同 P1 advisory-2 DNS scope。
  - ② **prod operator image bump（OPEN）**：prod operator（`paas-system/paas-operator`）目前跑
    **`20260525.0054`——pre-P1/P2 的舊 image，連 Access endpoint 都沒有**。bump 到 `20260608.0541`+
    才有 P2 Access endpoints **且**含 delete-path 修復（PR #166）；一併也帶上 P1 operator 變更（prod 落後多版）。
    走 Fleet image-only（`paas-operator/values-prod.yaml`，paas-app-bundle-cd）。bump 後可比照 stg 做
    operator 級 live 寫測閉環（含 token live 驗證）。owner=platform / prod 維運。

- **F-SA-5**（**post-migration 觀測 / OPEN**，priority:low，scope=P2.5 升級後 stg operator review；2026-06-10）：
  **P2.5 migration `18.0.1.0.37` 的 policy fan-out 可讓「多 protected route」的 SA 升級後落在 per-SA
  policy quota 之上，擋住後續使用者新增 policy。** quota 語意在 P2.5 改了——`_check_policy_quota`
  count 的是 `search_count([('security_access_id','=',sa.id)])`＝該 SA 跨**所有** protected route 的
  policy 總量；但 per-SA 上限（`plan.sa_max_access_policies`）沒變。migration step 2 fan-out 把每條
  legacy policy 複製到每條 protected route（`cr.sa_skip_policy_quota` bypass 讓 migration 本身不中斷），
  故 N legacy policy × M protected route → 升級後 N×M 列都算進同一個未變的 cap。**結果**：該 SA 升級後
  可能 count > quota，下一次使用者 `_create_policy`（BFF）被「quota exceeded」拒絕，直到手動刪到 cap 以下；
  純改 name/decision/rule 不受影響（`@api.constrains` 對那些 vals 不 fire）。**stg-only**（prod migration
  no-op、prod operator 仍 pre-P2）。已在 `post-migrate.py` docstring（step 2「POST-MIGRATION QUOTA
  CAVEAT」）標註。**建議**：升級後請維運人員 review 多 protected route SA 的 per-SA policy 配額；或日後
  考慮在 count 端對「同 route 內完全相同的 fanned policy」去重。owner=platform / stg 維運。

- **F-SA-6**（**dead bridge 移除 / OPEN**，priority:low，scope=P2.5 controller cleanup；2026-06-10）：
  **移除 P2.5 SA-level Access bridge（`/access-config` SA-level endpoint + `_create_policy` 的
  no-route_id fallback）。** Phase 1 加這條 bridge 是讓尚未遷移的 Phase 4 前端能繼續用 SA-level 寫法
  （`_update_access_config` 把 `access_enabled`→primary route `is_protected`；`_create_policy` 無
  `route_id` 時 fallback 到 primary route），原 comment 寫「Remove the bridge once Phase 4 ships」。
  **Phase 4 已在本 PR（`feat/security-access-p2-access-per-route`）shipped**：`service.js` 移除
  `updateAccessConfig`/`updateRoute`，`createPolicy(workspaceId, saId, routeId, vals)` 一律帶
  `route_id`，`updateRouteAccess` 打 per-route endpoint。故 SA-level `/access-config` POST 與
  no-route_id `_create_policy` 分支**已無任何 in-repo client 觸發**。
  **本 cycle 刻意不移除的理由**：① severity=minor ② bridge 仍有完整安全測試鎖契約（positive
  `test_update_access_config` / `test_update_access_config_whitelist_only` /
  `test_security_access_endpoints.py` owner 翻開 + 非 member/role-gate 負向；移除須一併刪/改 ~5 條
  test）③ deploy 窗口內帶舊快取 JS 的 in-flight 瀏覽器仍可能打舊 endpoint，過早移除會 break ④ 此面
  **非安全發散**——bridge 走同一組 `_get_sa_or_error` cross-SA pin + 同一 whitelist + 同一
  `Policy.create`，只是作用在 primary route 而非 client 指名 route，跟 per-route 路徑 lock-step。
  **動手建議**：確認 stg/prod 都已部署含本 PR 的前端、in-flight client 窗口過後，刪 `api_sa_access_config`
  + `_update_access_config` + `_sa_access_route` + `_create_policy` 的 `else` fallback 分支，並同步刪除
  上述 5 條 bridge-specific 測試（per-route endpoint 的對等測試已涵蓋安全契約）。owner=platform。

- **F-SA-7**（**cosmetic / OPEN**，priority:low，scope=operator reusable-policy 命名；2026-06-11，live stg E2E 實證）：
  **per-route 模型下，reusable policy 的 CF 顯示名前綴用 SA 層 `canonical_name`，不是該 route 自己的
  `app_name`（subdomain）→ CF dashboard 上非 primary route 的 policy 掛著 primary route 的 subdomain 前綴，
  可讀性誤導。** `extra/paas-operator/src/services/cloudflare.py:1322` 寫死
  `name=f"{canonical_name}: {p['name']}"`，而 app 本身的名字（同函式 line ~1332）正確用 per-route 的
  `app_name`。Odoo `_build_access_payload`（`security_access.py:1249`）同時送 `canonical_name`（SA 層、=
  primary/canonical route 的 subdomain）與 `app_name`（this route 的 subdomain），operator 取了前者當 policy
  前綴。**實測（2026-06-11 live stg，test SA id=2）**：route `paas-test-sa-1` 的 policy 在 CF 顯示成
  `paas-sa-5a105e8b-5ee42426: stg-verify-allow`（`paas-sa-5a105e8b-5ee42426` 是另一條 primary route 的
  subdomain）。**功能完全無損**：per-route 隔離 live 驗證通過（兩條 route 各自獨立 app／session／idp／policy、
  零交叉洩漏；operator 以 `existing_policy_ids` 按 **id** reconcile、CF reusable policy 以 id 為鍵，故同 SA 跨
  route 的同名 policy 雖在 dashboard 顯示同名仍 by-id 安全）。**唯一影響**：CF dashboard 看不出 policy 屬於哪條
  route。**修法**：operator line 1322 前綴改用 payload 的 `app_name`（this route 的 subdomain）取代
  `canonical_name`。需 operator image 重建 → **建議併入下次 operator bump**（如 F-SA-4 ② 的 prod bump 一起帶）。
  owner=platform。

## 2026-06-13 — SA Policy rule 子模型過渡 compat shim 待移除

**(severity: low, scope: SA policy P-rule Phase 1, 動手建議: Phase 3/4 收尾後移除, 對誰: platform)**

P-rule Phase 1（rule 子模型 + AND/OR/NOT + Bypass + selector everyone/country + precedence sparse）在
`src/models/security_access_access_policy.py` 的 `create()` 加了一個 **transitional 過渡相容層**：傳入扁平
`rule_type`/`rule_value`（且未給 `rule_ids`）時自動展開成單條 `group='include'` rule。目的是讓 in-flight
controller（`_create_policy` 仍送扁平欄位）與既有 ~30 處測試 fixture 在 Phase 1→3 過渡期零 churn 保持運作。

**待辦**：Phase 3（controller 改送 include/require/exclude 三組）+ Phase 4（前端 builder）落地後，移除該 shim，
並把仍用扁平 kwargs 的測試 fixture 改走 `rule_ids`。移除後扁平 `rule_type`/`rule_value` 不再是合法 create 輸入。
設計依據見 `docs/plans/2026-Q2/2026-06-13-sa-policy-rule-submodel-{design,implementation}.md`（design D6）。

> **已解鎖（2026-06-13）**：PR #193 merge 進 `develop`、Phase 5 live stg E2E 全鏈打通（前端確實送三組 payload、stg 真 CF 驗綠），
> 前置條件（前端 ship 三組）已滿足。此 shim + controller `_create_policy`/`_update_policy` 扁平 fallback **現在可獨立開 PR 退場**。
> 退場時連同改測試 fixture 走 `rule_ids`，並驗升級路徑（`.39` 既有 DB 已遷成子模型，扁平輸入已無生產來源）。

附帶 infra note：本 repo `.sandbox.yml` **未宣告 `extra_addons`**，docker-desktop local sandbox 跑 create 會因
addons_path 缺第三方相依（ai_base_gt 等在 `paas-odoo-ci/addons`）而模組裝不起來；暫以 `create --values`
覆寫 `codeMount.extraAddonsHostPath` 繞過。建議把 `extra_addons: ["${PAAS_CI_ADDONS_PATH}"]` 補進
`.sandbox.yml`（對齊 floor-env-relative-path-empty-ext-addons pitfall 精神）。owner=platform。
