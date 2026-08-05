# project 471 conceal instances split

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_471_conceal_instances_split`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#471 is_hidden 拆兩語意收官：PR#473（conceal_instances 實例層／is_hidden 曝光層）＋repo-lint 第8條 gate；衍生 check_addon_removal param bug"

2026-07-30 完成 **#471**（方向 (b)，Eugene 當日拍板）：template 層 `is_hidden` 拆成兩軸 → **PR #473**（target develop，未 merge）。

**分流**（對照 issue 的 12 處 gate 盤點表）：A 組 8 處 ＋ `HiddenChainMixin._hidden_template_ids` 推導根 → `conceal_instances`；B 組 4 處（marketplace／template by-id／plans／generic create）維持 `is_hidden` 並補「勿順手統一」註解；C 組 `reset-admin-password` 補 gate（放在 `exists()` 後、`has_admin_gui` **前**——能力判準 400 不該遮蔽存在性 not found，否則洩漏 conceal 服務存在）。

**seed 盤點**：`is_hidden=True` 恰 2 筆——`ha-paas`（加 conceal=True，行為零變化）、`hermes-agent`（不設 conceal，既有服務恢復可用）。

**`-u` 免 migration 已實測**（非推理）：沙盒 DB 由不含該欄位的 image 安裝（證據＝首輪 3 次 `AttributeError`），`-u` 後 `ha-paas=t`、其餘全 **NULL 而非 false**（Odoo 不回填 default）。NULL 讀為 False 且不匹配 `('=',True)` ⇒ **falsy＝不隱藏，fail-safe 方向正確**。⚠️ 該 DB `latest_version` 已等於 manifest `.81`，所以**就算寫了 migration 也會靜默 no-op**（F4 pitfall）——這類 seed 修復想靠 migration 必須先 bump 版號。

**守線三層**：model `_check_conceal_implies_hidden`（conceal ⟹ hidden，同時擋「解封只翻 is_hidden 留半套」）＋ repo-lint 第 8 條 `check_conceal_instances_gate`（AST 抓 `template_id.is_hidden`，曝光層直接持有 template 變數故不誤報；已做紅燈對照）＋ 行為矩陣測試（兩語意**成對**比對，單測一邊證不出拆分成立）。

**零回歸硬證**：全量 branch `6F/11E of 1933` vs baseline（同沙盒、純 origin/develop、同指令）`6F/11E of 1900`，**失敗清單逐條相同、差集空**。那 17 條是 stg 沙盒環境依賴（`paas-stg-` env token 前綴、`BlockedRequest` 打真 operator），與變更無關。歸因一定要跑 baseline 取差集，不要憑「檔案沒碰到」下結論。

**衍生（已閉環）**：`api_check_addon_removal` 讀 `request.get_json_data()`＝**整個 request body**（`odoo/http.py:1810`），不是 JSON-RPC `params`；前端走標準 envelope ⇒ 永遠讀到 `removed_addon_ids=None` → 提早回 `{'success':True,'modules':{}}`，**addon 移除安全檢查與其後所有 gate 從未執行**。→ **#474／PR #476**（正規 params 簽名＋前端對話框接通＋#473 workaround 回正；關鍵紅燈＝conceal gate 測試改回正規 params 即紅；行為變更＝首次真擋、首次真打 operator、非 running 回錯）。再衍生 **#477（medium）**：`_update_service` 守衛兩個 fail-open 縫——operator 回 HTTP 200＋success:false 時 client 不 raise、掉出 if 續跑 helm upgrade（同一判斷在預檢端點是 fail-closed，兩路方向相反＝根因要抽單一裁決點）；#476 後預檢巧合形成 defence-in-depth 但 TOCTOU/直接 RPC 仍命中。

上游脈絡 [[project_464_reset_button_visibility_e2e]]（該次實測發現 is_hidden 讓既有服務詳情頁整個打不開）、[[project_460_reset_button_gate_landing]]；環境 recipe 見 [[reference_odoo_test_amd64_woowk3s_cp_overlay]]。
