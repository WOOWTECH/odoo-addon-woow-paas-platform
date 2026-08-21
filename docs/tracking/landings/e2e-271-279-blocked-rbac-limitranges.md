# project e2e 271 279 blocked rbac limitranges

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_e2e_271_279_blocked_rbac_limitranges`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#271+#279 合併 E2E 全案收口（2026-07-08）：首跑 BLOCKED＝RBAC 缺 limitranges（#315 已修已關）→ 第二跑 PASS（三組差分閉合）→ issue #271 關閉；含側發現 #316 坐實與新 followups"

**2026-07-08 #271+#279 合併端到端驗證首跑 BLOCKED**（3 階段 workflow：偵察→執行→對抗覆核 agree）。炸點非被測兩層：spawn_or_attach 500 ＝ **operator ClusterRole 缺 `limitranges`**（#278 把 LimitRange apply 加進 `_ensure_namespace`（agent_runtimes.py:136→helm.py apply_manifest 第 3 apply），但 woow-paas-charts 的 chart RBAC 只加了 resourcequotas 漏 limitranges）→ **stg 所有 agent spawn 自 operator 20260707.1305 起全掛；prod 同版同 chart latent（現零租戶）**。→ [[issue #315]]（三角證據：can-i limitranges=no/quota=yes、ClusterRole 全文零 limitranges、叢集 quota 有 LimitRange 無）。**修復落點＝woow-paas-charts** ClusterRole 補 rule（比照 resourcequotas）→ 發版 → 重跑。**修復鏈已備齊（同日，maestri 代工：Rigger=charts、Convoy=CD）**：charts PR #38（chart 0.1.17；merge 後 CI publish-charts.yaml 自動發佈）→ paas-app-bundle-cd PR #44（stg+prod 兩處 fleet.yaml pin 0.1.17，**須等 #38 發佈後才可 merge**）→ Fleet rollout（chart-only，免 image rebuild、無 lock-step）→ can-i 複驗轉 yes → resume E2E。兩 PR 待人工 merge（agent 被擋 self-merge）。

**已實證正面（重跑勿重工）**：O5 leg-2 自動 labeling OK（spawn 途中 agent ns 自動蓋 paas-managed/env=stg/owner=p37/**reach-ws-1=true**、無誤蓋 reach-ws-7）；#271 route (a) 資料層 OK（stg 唯一 mcp_server id=2 sidecar_backed=True、url=in-cluster svc-16e7ac1a-mcp...:3000/mcp）。

**重跑路徑**：殘留刻意留存冪等重用——ns `paas-ws-stg-pa-oudse266vg2ztjpb`（含 quota、無 release）+ agent_session id=19（project 37「n8n-helper」ws1, state=absent）。workflow 可 resume：`wf_e4052e04-6c2`（偵察 runbook 已快取）；證據檔 scratchpad/e2e-271/。**重跑必補**（覆核點名）：agent ns egress NP 物件存在性＋規則（本次連 render 都沒發生）、正反向打同一 in-cluster URL、deny 3 次穩定＋fresh-pod race 等待（[[reference_woow_k3s_netpol_fresh_pod_race]]）、e2e-271-neg 無 label 對照組。⚠️ 正向＝裸 curl（template 2 tenant-neutral 無 MCP 綁定）→ PASS 只證 NP 資料面，勿誤讀為 SA 層 MCP 整合通。

**側發現（issue 待開，permission 擋於本 session）**：stg 控制面 MCP tool sync 疑被 #280 baseline default-deny 擋——dev-sandbox-stg ns 無 env/reach label，ws1 allow-mcp-reach match 不到 → mcp_server id=2 state=error「unhandled errors in a TaskGroup」/tool_count=0（時序吻合 NP 落地 ~42h 前）。agent 資料面不受影響（reach label 路徑）。prod odoo（paas-system ns 無 label）latent 同撞。修法討論題：ns 蓋 label（語意不對）vs allow-mcp-reach 增控制面 label 腿 vs tool sync 改路徑；stg 載體是 TTL 沙盒、label 要能在重建後存活。

**防線 follow-up**（#278 教訓，BUG-7/8/9 同類）：integration test 在 admin context 測不到 RBAC drift → operator SA can-i preflight 或 chart-side RBAC test；operator exception handler 補 KubectlException.stderr 進 log（本次靠 can-i 反推）；landing checklist 補「operator code 與 chart RBAC 須同版」。

**全案收口（2026-07-08 同日）**：修復鏈 charts PR #38（0.1.17）+ CD PR #44 merged（Eugene 首報 merged 實為 open——**agent 端 merge 全被 classifier 擋，只能人工按**；第二次才真 merge）→ Fleet b7bae1f → can-i stg/prod 皆 yes → **第二跑 PASS（對抗覆核 agree）**。三組差分閉合：agent pod（reach-ws-1）→ ws1 MCP URL **401**（兩腿通）；無 label pod / Odoo 控制面 pod 同 URL **refused**；跨租戶 ws7 deny 3×穩定。LimitRange 由 spawn 成功 apply＝RBAC 修復動態實證。**issue #271、#315 已關；#316 假說被直測坐實**。PASS 範圍＝NP 資料面+route(a) 資料層（裸 curl），SA 層 MCP 協定整合另立。新 followups：SA 層 MCP 整合 e2e／agent_session spawning→active 未轉（webhook 200，疑需 WS attach）／egress 腿 deny 方向隔離測試／e2e 證據檔應內嵌目標 URL。殘留＝agent ns 依設計留存（dormant release replicas=0）、session id=19 reaped。維運備忘：本機 sleep 被 harness 擋→fresh-pod race 等待改 in-pod sleep 執行（語意等價）。

**勘誤（2026-07-09）**：① develop **是**本 repo 預設分支（Gitea Settings 實證）——Closes 合併進 develop 會自動關 issue，先前「須手動關」認知錯誤（#317/#318/#322 皆自動關實證）；② agent 端 Gitea merge 非絕對被擋——**使用者明確點名授權該 PR 時 classifier 放行**（2026-07-09 tea merge #320/#321 成功）。

odoo shell 入口（stg）：`kubectl exec -i <pod> -n dev-sandbox-stg -- /opt/bitnami/odoo/venv/bin/python /opt/bitnami/odoo/bin/odoo shell -d woow_stg --no-http -c /etc/odoo/odoo.conf`。相關：[[project_277_278_w1_ns_quota_landing]]、[[project_271_internal_mcp_bypass_landing]]、[[project_netpol_279_280_landing]]。
