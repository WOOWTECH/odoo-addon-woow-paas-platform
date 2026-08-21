# project 423 w6 stg e2e round1

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_423_w6_stg_e2e_round1`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#423 W6 stg e2e 首輪（2026-07-30）：六項驗收跑完、挖出三缺陷（#479 阻斷/#482 fast-follow/#478 UX）、hotwired 實測 R8=74s；正式重跑 gate＝chart 0.1.2＋operator SA patch＋seed pin 0.1.2"

**#423 W6 首輪（2026-07-30，hotwired）結論：不放行 prod，卡 #479**。六項：W6.0/1/3/4/5/6 ✅、W6.2 🔴。全程 haos-w5b 執行、leader2 逐項 object 驗收；報告=#423 comment 3743（247 行）。

**三缺陷（都是 registry 正式路徑首跑挖出，W1 PoC 走 http source 測不到）**：
1. **chart 0.1.0 secretRef 缺陷**（→ chart 0.1.1 已修+publish）：datavolume.yaml 把 dockerconfigjson 的 paas-registry 塞進 CDI `secretRef`（要 accessKeyId/secretKey）。**CDI v1.64 CRD 無 imagePullSecret 欄位**；pullMethod=node 憑證正解＝**租戶 ns default SA 的 imagePullSecrets**（SA admission 併入 importer pod）→ operator PR #480 實作（兩層 ensure 綁同一 helper）。熱線 PoC：patch SA＋手工去 secretRef 的 DV → import 74s Succeeded。
2. **#479 trusted_proxies design gap（v1 阻斷）**：HAOS 的 HA Core 未設 trusted_proxies 拒絕一切帶 XFF 請求（400）；CF tunnel 必帶 XFF。07-26 Elmo 材料分析早預警、07-27 design 漏接；Elmo 沒撞到＝他的 VM 從客戶 .tar 還原（config 自帶 http block）。**兩種 400 要辨識**：JSON body `onboarding_required`＝HA 正常語意；純文字「400: Bad Request」＝trusted_proxies 拒絕。修法 (f)＝chart 0.1.2 XFF 剝除 nginx（in-cluster 實證 400→302；PR #64）；(a) golden image 烤入留 v1.1。CF origin 實查＝主 Service DNS（從 operator pod 用其 CF 憑證查 tunnel configurations）→ 改 Service selector 佈線有效。
3. **#482 停權服務被 redeploy 後無法復權（fast-follow）**：upgrade 尾端寫 state=deploying、_do_resume guard 只認 suspended、poll 等不到 Halted VM 的 ready → 卡死。**_is_halted() 聯集守衛是對的**（VM 全程沒被喚醒）。脫困 runbook：`write({'state':'suspended'})` 再 `_do_resume()`。容器型未測不可外推。

**實測數字（hotwired 單次樣本，無 W1 基準——R8 W1 沒量過，design 30–60 分是 http 路徑二手估計）**：import 74s／端到端 100s／suspend 54s／resume 54s／HA Core 首次就緒 ~13min／resume 後應用層回歸 165s。**registry+node 路徑 quota 峰值 135.7 GiB／2 顆 PVC**（無 scratch；http 是 203.5/3 顆）→ min_storage_gb=72 維持不動（涵蓋 http escape hatch）。quota 穩態 2015m/4.43Gi/67.84Gi 與 W1 逐位一致＝W5b min_* 建模成立。

**擴散型 pitfalls**：
- **stale 主 checkout 連坑三 agent**（include_vm／CLAUDE.md helm 行／runStrategy）：查平台現況一律 `git grep <p> origin/develop -- <path>`；已 ff 主 checkout 至 bdeb562a 拔根。
- **cloudflared keep-alive＋conntrack pinning**（推論級）：Service endpoints 變更只影響新連線，持續探測讓連線池保溫走舊 endpoint → 改 selector 後「零新流量」誤判架構無效。
- **PVC 的 helm.sh/chart 標籤不隨 upgrade 更新**（owner=cdi-controller），盤點以 DV 標籤為準。
- nginx 剝除代理兩教訓：proxy_pass 必指獨立 -vm 後端（指主 Service＝自迴圈、症狀 499）；websocket Connection 用 map 會整個卡死、硬編 "Upgrade" 正常。
- Gitea run 層 status 可能永遠 stale in_progress（1294/1299 兩例）→ merge gate 看 job 層 conclusion（見 [[reference_tea_actions_runs_list_completed_only]]）。

**第二輪（正式重跑，2026-07-31）已收官**：六項全綠、全程零手工（#423 comment 3829）。R8 第二樣本＝下單→running 98s（import 58s）。重跑再挖一缺陷：**#486 scale_release 擇一 gate**（`if not deployments and not statefulsets:` 才查 VM——0.1.2 的 proxy Deployment 讓 suspend 只停 proxy、VM 照燒 2000m，帳面 suspended；operator 還回 200 讓呼叫端無從得知）→ PR #488 拆 gate 三 workload 無條件處理＋刪掉「有容器時不得查 VM」那條**保護著缺陷的測試**（教訓：斷言某查詢不會發生＝鎖實作非護行為）。**我的兩個斷言被 live 否證入案**：「scale 兩半都做」（漏讀 gate）、「conntrack pinning」（真因＝手搭 nginx 卡死，cloudflared log 是 connection refused）。W6.4 加碼驗證 chart 層 xffProxyReplicas-由-runStrategy 縱深真的生效。#482 於重測再現、runbook 有效。收官狀態：#479/#486 closed、#482 fast-follow（priority:high）、#478 low、R4 觀察項；stg operator=167f8d1@81d0ea79；prod rollout 另行走兩條發版線（⚠ prod operator chart bump ≥0.1.18 前必先處理 CD#70）。相關：[[project_424_item1_hermes_agent_cs_landing]]、[[reference_helm_partial_image_override_digest_merge]]、[[reference_ha_trusted_proxies_peer_ip_based]]。
