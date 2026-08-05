# 自 memory 遷移的 103 篇——原索引 hook 逐字保存

> 2026-08-05 整併時，`MEMORY.md` / `MEMORY-odoo.md` / `MEMORY-landing.md` 內
> **整行只指向已搬走檔案**的索引行，逐字移到這裡。搬移後這些 hook 不再佔用
> 每個 session 的 context，但內容一字未刪。
>
> 連結路徑仍是舊的 memory 檔名；對照新位置見 `memory-inventory-2026-08-05.md`。

- [共用叢集 scope 鐵律](feedback_shared_cluster_paas_scope_guard.md)
- [#525 A2/A3（PR#540）](project_525_a2a3_workspace_invite_rollback.md) — 🔴 **issue 寫的「後果」跟行號一樣要自己驗**（實測是框架層 500 不是靜默 name-1）；前提斷言連擋兩次選錯的注入點；**造不出可達失敗就別硬造**——移除假注入的測試並註明原因
- [#520 半綠被整條紅掩蓋＋清單不可照抄](project_520_create_service_rollback_landing.md) — 測試是 AND、只顯示第一個失敗的斷言；teammate 順手盤的清單 9 處行號全中但 **5 處因果與程式碼不符**
- [#529/#534/#532 假綠修復（批次 0＋1）＋建守衛四模式；金流唯一憑據已由 #541 收掉（原標「未裁決」是過期陳述）](project_529_test_false_green_batch0.md)
- [#492 assertLogs level 遮蔽](project_492_assertlogs_level_shadow.md)
- [Prod Odoo PaaS 上線](project_prod_odoo_paas_not_installed.md)、[prod canary 煙測](project_prod_canary_smoke_20260708.md)
- [同 class 重複定義 method 靜默覆蓋](reference_python_duplicate_method_silently_overrides.md)
- [#501 HAOS 全域上限收官](project_501_global_cap_landing.md) — **Odoo 是 REPEATABLE READ ⇒ advisory lock＋取鎖後計數零貢獻**（實測重現超賣，錯誤前提自 #345 傳承）；`SerializationFailure` 是 `Exception` 子類會被 controller 吃掉；**刪掉一條零鑑別力測試又造了一條**；衍生 #515/#518/#519/#533
- [#504 平台 DB 每日備份](project_504_platform_db_backup_landing.md)
- [#424 Hermes Agent](project_424_item1_hermes_agent_cs_landing.md)（[Phase B seed](project_424_item1_phase_b_seed.md)、[OAuth image](project_424_item2_hermes_agent_image.md)）＋[bundle 三硬約束](reference_hermes_agent_web_dist_patch_constraints.md)
- [OpenDesign 上架](project_opendesign_cs_onboarding_380.md)＋[dotfiles 持久化](project_407_opendesign_dotfiles_persistence.md)、[三型 MCP 上架](project_mcp_3type_onboarding_375.md)、[MCP template 啟動契約](reference_mcp_template_launch_contract.md)、[MCP admin 映像重釘](reference_mcp_admin_cs_image_repin.md)、[Smart Home MCP](project_smart_home_mcp_design_20260709.md)
- seed／改價：[Wave A](project_wave_a_reseed_landed.md)＋[Wave B](project_wave_b_reseed_build.md)（改價 SOP＝改 manifest 重跑 generator；M2M links 必 noupdate=0）、[跨 repo seed 對 chart 核對](reference_cross_repo_seed_verify_against_chart_values.md)
- 定價／訂閱：[gap 分析](project_pricing_gap_analysis_20260709.md)、[會議衍生 issue](project_meeting_20260710_issue_landing.md)、[#324 重複扣款](project_324_subscription_dup_charge_incident.md)、[#327 退點視窗](project_327_refund_window_landing.md)、[#318 刪除聯動](project_318_subscription_unlink_landing.md)、[稅務bug＋選擇器](project_subscription_tax_bug_and_selector_redesign_20260718.md)
- [Cloud Odoo Addon Selection](project_cloud_odoo_addon_selection.md)、[#269 HA port removal](project_269_ha_port_removal_pick.md)、[訂閱整合](project_subscription_integration.md)、[wallet billing](project_wallet_billing.md)
- [woow playwright E2E gotchas](reference_woow_playwright_e2e_gotchas.md)
- [Security Access stg E2E](reference_security_access_stg_e2e.md)、[stg cloud service live-create E2E](reference_stg_cloud_service_live_create_e2e.md)（odoo shell 須 env.cr.commit）
- [docs 全面審查](project_docs_audit_20260719.md)＋[superpowers promote](project_superpowers_docs_promote_20260719.md)
- [controller 吞掉例外後交易照常 commit](reference_odoo_controller_swallowed_exception_still_commits.md) — 靠 `raise` 做 rollback 的守衛遇上 `except Exception`+回 200 就失效；症狀＝**畫面說失敗、資料卻是好的**；`AccessError` 是 `UserError` 子類
- [對 non-stored compute 欄位 create/write 靜默丟棄](reference_odoo_nonstored_compute_write_silently_dropped.md) — 不拋例外、DB 連欄位都沒有（實測 `create({wallet_balance:1000.0})` 讀回 0.0）⇒ fixture 前提**靜默不成立**；判準用 `_fields['x'].store/.inverse`；與 stored-compute-without-inverse 失效方式不同；🔴 本 repo 4 處實測後果全不成立＝誤導性死設定非缺陷
- [#559 helm_values 欄位級授權（PR#569）](project_559_helm_values_field_acl_landing.md) — **層級錯位**：遮罩做在 controller、授權邊界在 ORM ⇒ `call_kw` 直讀繞過整層（#535 鎖了前門、側門還開著）；**防護做得比資產最內層邊界更外面，就必然有繞過的路**，且第二條路徑**不在你改的那個檔裡**；repo 既有守則「檢查落最內層」的第四個實例、發生在守則寫下之後；`repo-lint` 的 controller-sudo gate **不亮是對的**（其 invariant 是記錄級，本案欄位級）但要寫明否則被讀成沒提權；連帶揭露**測試對租戶身分系統性零覆蓋**（既有測試幾乎都 root 呼叫）；附 flaky `TestProvisionUniqueIndex`（`LIKE` 命中 2 筆無 ORDER BY）＋**Gitea 寫入全掛時 5xx 被吞成空錯誤訊息**
- [#522 SA 身分錨點宣告式清單＋sentinel hatch](project_522_identity_anchor_landing.md)（PR#537）— 只加欄位不改 hatch＝交出「看起來修好、實際仍可繞」的補丁；連帶揭露既有 canonical_name 守衛也可繞；**issue 原描述被推翻**（SA connector 在租戶自家、實測全叢集零個在 `paas-ws-*` ⇒ 補 `service_url` 驗證是 no-op 控制，預設值就是租戶自家反代）
- [ar#26 收官](project_ar26_sidecar_selfcheck_landing.md)＋[軌A stg 憑證隔離](project_ar26_tracka_stg_sidecar_isolation.md)
- [ci#46/#50 平台側文件同步](project_ci46_platform_docs_sync.md)
- [#427 控制面 ingress 白名單](project_427_operator_ingress_baseline.md)
- [cd#77 A′ 版本感知 initContainer](project_cd77_module_version_gate.md) — 版號不等就自動跑 `-u`＋post-verify 不變式（fail-closed 卡 Init）；🔴 **截至 2026-08-03「不相等」那條分支 prod 從沒走過**（只驗過「相等就 skip」）⇒ **下一次 image 版號 ≠ DB 版號的發版才是首次實戰**（別記數字，當下自己量兩邊）；⚠ 它**只升 `woow_paas_platform` 一個模組**，seed 變更落在相依 addon 時仍需 `prod-release-sop.md` 的全域兩段式——**SOP 過期但不作廢**；post-verify 只驗版號推進、**不驗 seed 欄位真的寫進去**（sticky `noupdate` 會讓兩者分離）
- [#352 allow 政策整批放行](project_352_allow_policy_bulk_grant.md)
- [ar#29 operator log 洩密](project_ar29_operator_redact_closeout.md)、[#29 mixin sudo＋#389 marker](project_29_389_mixin_sudo_dedup_landing.md)（「我做了動作」≠「側效果發生了」）
- [#494 Hermes 定價](project_494_hermes_pricing_landing.md)、[#482 停權 redeploy](project_482_suspended_redeploy_landing.md)、[#484 具名失敗欄位](project_484_named_failure_fields.md)
- [#477 fail-open 家族](project_477_fail_open_family_closeout.md)＋[_update_service 守衛](project_477_update_service_fail_closed.md)＋[F-423-1 pods=[]](project_f4231_pods_empty_fail_closed.md)
- [#486 scale VM](project_423_w63_scale_vm_gate.md)、[#423 W6 SA imagePullSecrets](project_423_w6_default_sa_imagepullsecrets.md)、[#423 W6 stg e2e](project_423_w6_stg_e2e_round1.md)
- [#461 reset 非同步](project_461_reset_async_landing.md)、[#449 reset 密碼載體](project_449_reset_carrier_landing.md)、[#460 Reset gate](project_460_reset_button_gate_landing.md)、[#464 可見性 E2E](project_464_reset_button_visibility_e2e.md)、[#471 is_hidden 拆兩語意](project_471_conceal_instances_split.md)
- [#411 reset values 管線](project_411_reset_values_landing.md)、[#317 PVC keep](project_317_pvc_keep_landing.md)、[#404 apiServerCidrs](project_404_apiserver_cidrs_landing.md)、[#425 prod DB NetPol](project_425_pg_netpol_hotfix.md)、[#433 seed 拓撲](project_433_seed_topology_landing.md)
- [#422 maxPods＋備份鏈](project_422_maxpods_backup_chain_closeout.md)、[全套 0F/0E](project_test_suite_zero_red_20260728.md)、[#275 Project 隱藏](project_275_project_hide_toggle_landing.md)
- 租戶隔離 #271/#279/#280：[E2E 收口](project_e2e_271_279_blocked_rbac_limitranges.md)、[稽核](project_audit_271_279_280_20260708.md)、[內網 MCP bypass](project_271_internal_mcp_bypass_landing.md)、[NetPol](project_netpol_279_280_landing.md)、[W1 ns quota](project_277_278_w1_ns_quota_landing.md)
- [#530 gateway 落地＋#542 stg baseline](project_530_gateway_landing_542_baseline.md) — 通道兩側已 merge（PR#539 平台/.97、charts#69 chart 0.1.1）；四個假綠陷阱（**外部打 /health 拿到的是 WebUI 不是 gateway**／urllib UA 被 CF 1010 擋而 httpx 不會／gateway 忽略 X-Forwarded-Prefix／dashboard 要打 /dashboard/auth/password-login）；#542 baseline 實測＝stg addon **.88**（develop .97 ⇒ 平台側 key 交付邏輯根本不存在）、API_SERVER_KEY 仍在、quota cpu 21 但 **PVC 10/10 滿**（兩版 chart 物件相同故不擋）；**A/B 可拆：A 只驗通道零破壞、不動 admin_password**（gateway 走獨立的 auth.apiServerKey Bearer）
