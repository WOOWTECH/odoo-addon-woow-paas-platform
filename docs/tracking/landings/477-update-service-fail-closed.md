# project 477 update service fail closed

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_477_update_service_fail_closed`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#477 _update_service 移除 addon 守衛兩個 fail-open 縫收斂單一裁決點（PR#481）；正交兩維度回傳與哨兵探針"

2026-07-30。#477＝`_update_service`（authoritative 寫入路徑）的 addon 移除守衛有兩個 fail-open 縫，**PR#481**（`fix/477-update-service-fail-closed` → develop，未 merge）。

- 縫 (a)：operator 對 `OdooModuleError` 回 **HTTP 200 ＋ `success:false`**，平台 client 只在 HTTP ≥ 400 才 raise → 舊碼 `if check_result.get('success'):` **沒有 else**，既無例外也進不了隔壁註明 `# Fail-closed` 的 `except`，直接掉出檢查區塊跑 helm upgrade。`find_pod` 失敗＝租戶 Odoo 不健康，**保護最該生效那一刻自己讓路**。
- 縫 (b)：`if removed_ids and service.state == 'running'`；前置閘只擋 pending/deleting/error → deploying/initializing/upgrading/suspended 四個進得了卻整段跳過。上游沒閘：`_check_operation_allowed` 是**訂閱**閘（與 state 正交＋group_system bypass）、前端 `canEdit` 不含 suspended。

**修法核心＝抽 `_assess_addon_removal` 單一裁決點，回兩個正交維度**（這是本案最可複用的設計）：`can_assess`/`error`＝「能不能裁決」（非 running／operator 未設定／`success:false`／例外都在此，兩路皆 fail-closed）；`modules`/`blocking`＝「裁決結果」。**必須分開**——advisory 預檢的 `success` 只反映第一維（installed 模組要回 `success=True` 才有東西給對話框列），把 blocking 併進 success 會反轉 #476 鎖住的矩陣。這是把「同一判準兩份實作必然漂移」（[[two-layer-acl-lockstep]] 家族）從補 diff 升級成結構不可能漂。

**跨發版線紀律**：operator 的 `HTTP 200 ＋ success:false` 契約**不動**（改它是 breaking change，且平台側當失敗處理即可）——純 `src/**`，見 [[reference_repo_two_release_trains]]。

測試法兩點見 [[reference_odoo_test_amd64_woowk3s_cp_overlay]]（下游哨兵探針、Odoo 不收繼承來的 `test_*`）。RED 7/10（六條哨兵洩漏＝縫 (a)×2＋縫 (b)×4，一條訊息漂移）→ GREEN 16/16；全量 1980 vs 純 develop baseline 1970，紅燈集合兩向差集皆空（那 17 條既有紅是環境依賴）。

**收官後同族唯讀盤點（→ issue #484）**：`success` 那一族已全乾淨（`init_n8n`/`init_odoo` 四處都有 else），但同族換載體還有兩處活縫——operator 把失敗放在**具名欄位**而非 `success`，呼叫端連欄位都沒讀：(1) `dns_failed`（`api/tunnels.py` create 吞 CF DNS 例外回 200-flag）在 `src/` **零命中** → `smart_home.py:181` 無條件寫 `state='active'`＋不解析的 URL，且**無 smart-home cron 可自癒**；對照組是同 operator 的 `api/routes.py` 對同一件事 raise（fail-loud）＝證明是漏接非取捨。(2) `pod_retrieval_error` 零命中 → `_is_container_ready` 讀到 `pods==[]` 回 True＝誤報就緒；**關鍵論點**：既有 follow-up F-423-1 拒絕修的理由（改 fail-closed 會讓所有服務多卡一輪 poll）**對這個子情形不成立**（正常過渡時該欄位是 None，零成本），那個 trade-off 從未被評估過；且 schema 把規則寫在容器欄位上（`vmi_retrieval_error` 說明「Mirrors pod_retrieval_error」），實作只落在 VM 分支。示範級對照：`check_route_availability` 三種失敗全明示 fail-closed、`sync_access_app` 把 operator 的 `unresolved_idp_types` fail-open 訊號持久化上前台。

**刻意未做**：前端 safe-states 尚未收成單一 wire-level 欄位（要在 `modules` 每筆加 `blocks_removal` 並刪前端 `SAFE_TO_REMOVE_STATES`，會動 #476 斷言）；`_filter_allowed_helm_values` 拒絕時不 rollback addon 寫入是既有 wart（另案）。
