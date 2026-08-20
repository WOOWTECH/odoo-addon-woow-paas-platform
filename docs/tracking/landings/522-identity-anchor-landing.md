# project 522 identity anchor landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_522_identity_anchor_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#522 / PR#537 — SA 身分錨點改宣告式清單＋escape hatch 改 sentinel；連帶揭露既有 canonical_name 守衛也是可繞的"

2026-08-02 landing。PR **#537**（`fix/522-tunnel-id-immutable` → develop），manifest `18.0.1.0.94`。

**issue 原描述是錯的，但結論成立**（調查階段的量測，leader4）：原單說「租戶填 `service_url` 就能把公開 hostname 指向他人叢集內服務」——不成立。SA 的 cloudflared connector 由**租戶自己在自家主機跑**（token 經 `controllers/ha_api.py:363` 發給租戶），實測 woow-k3s 全叢集 366 pods / 33 個 cloudflared，**零個在 `paas-ws-*`**；所以 `service_url` 是在租戶自己網路解析。
⇒ 兩個推論：①**補 `service_url` 驗證是 no-op 控制**——預設值 `http://localhost:80` 就是租戶自家反代，它本來就能轉發到任何地方，鎖死也不改變這條 route 能服務什麼；②issue 建議的修法方向（限制 `service_url` 指向本 workspace service DNS）會打爛產品主要用法。真正該鎖的是 tunnel 歸屬。

**原缺陷**：租戶 workspace owner 經 call_kw 寫得進 `security_access.tunnel_id`。根因不是漏一個欄位，是「保護逐欄位手寫」——`create()` pop 兩個字面值、`write()` 擋同樣兩個，後加的 `tunnel_id` 沒人補。repo 內 `test_update_access_config_whitelist_only` 早就送 `tunnel_id: attacker-tunnel` 並斷言被丟掉，但**只守到 controller whitelist**，ORM 那條沒守，測試一直綠。

**修的過程量到第二個更深的缺陷**：escape hatch 是 client 可偽造的 context key ⇒ 見 [[reference_odoo_call_kw_context_is_client_forgeable]]。這意味著**只加欄位不改 hatch＝交出「看起來修好、實際仍可繞」的補丁**，與 #522 本身同形態。

**做法**：`_IDENTITY_ANCHOR_FIELDS`（canonical_name / reference_id / tunnel_id / tunnel_name / tunnel_token）＋ `_ROUTE_IDENTITY_ANCHOR_FIELDS`（dns_record_id / access_app_id），create 剝離與 write 拒絕同源；hatch 改 sentinel。`subdomain_prefix` 刻意不入清單（create 當下使用者合法自選，只是事後不可變），保留獨立 key。

**測試設計教訓**見 [[feedback_declarative_list_gate_needs_floor_folded_in]]。

**未處理、已附影響評估給 dispatcher 開單**（同族 client 可偽造 context key）：
- `sa_skip_route_sync` — **最有意義的一條**：route 推送是宣告式全量，被抑制的 route 之後會隨任何一次合法異動一起上線，而 `is_protected` 的 Access app 建立正是被抑制那步 ⇒ **使用者標為 protected 的 route 可能無 gate 上線**。只能對自己資源施作。
- `sa_force_access_sync` — suspended/deleting 狀態仍能推自己的 Access 設定（停權未全面阻斷 operator 呼叫）。
- `sa_force_delete_primary` — **死鍵**，src/models 與 src/controllers 全域無人讀取，只剩 `test_security_access.py:739` 還在傳。
- `sa_rule_replace` — 低，最終態仍由 policy write() 全量收斂。

> **後續（2026-08-19，#536 已修）**：上面四條裡的三個活鍵
> （`sa_skip_route_sync` / `sa_force_access_sync` / `sa_rule_replace`）已全部改成
> 模組私有 sentinel（key 名不變、值改成 `_INTERNAL_SYNC_CONTROL`），死鍵
> `sa_force_delete_primary` 連測試殘留一起清掉。
> 實測（真 `/web/dataset/call_kw`、只有 `base.group_user` 的租戶）：修前
> 6 failed / 0 error of 13，修後 0 failed / 0 error of 17。
> 守線測試 `src/tests/test_sa_sync_ctx_forgery_536.py`。
>
> 盤點時**低估了 `sa_rule_replace`**：它不只抑制過渡同步，還跳過
> `rule.unlink()` 裡的 `_check_at_least_one_include()`，而那條
> `@api.constrains('rule_ids')` 對「直接 unlink 一條 rule」根本不會觸發 ⇒ 那行
> 顯式呼叫是唯一防線，偽造後租戶留得下 0 條 include 的 policy（RPC 實測回
> `result: True`）。**「這個 key 只是效能／體驗開關」這種歸類要逐個讀取點看，
> 不能看 key 的名字。**
- `smart_home` 有同型無守衛欄位，依分工未擴張（#524）。

**狀態**：2026-08-02 已由 Eugene merge 進 develop（develop manifest = `18.0.1.0.94`，含 commit `a0123f4b`，issue #522 由 `Closes` 自動關閉），驗證容器 `wpp522-*`／network `net522`／worktree `/private/tmp/wt-522`／本地分支**皆已清除，不要以為還在**；remote 分支依本 repo 慣例保留（merged 分支不刪 remote）。

**測試環境 recipe**（重建時用，補 [[reference_odoo_test_docker_desktop_paas_image]]）：baseline 0F/0E of 2204 → 本 PR 0F/0E of 2218。HTTP 端到端探針要在容器內用 `/opt/bitnami/odoo/venv/bin/python` 跑（系統 `python3` 沒有 psycopg2，會 `ModuleNotFoundError`）。
⚠ **同一個容器裡起過 HTTP server 後再跑 `--test-enable`，8069 被佔 → odoo 印 "Address already in use" 直接退出 → 我的 grep 後輸出全空**。空輸出不是通過；跑測試前先 `pkill -f http-port`。

**版號插曲**：一度 bump 到 `.93` 與 PR#517 撞同值——兩邊同值時 git 認定無衝突、rebase 後 diff 空白、**衝突根本不會出現**，所有以「有沒有 conflict」為訊號的檢查都不會響（見 [[reference_rebase_silently_eats_manifest_bump]]）。改 `.94` 收掉。反過來，merge 後 develop 到 `.94` 而其餘 open PR 停在 `.93` 時是三方相異、git **會**報衝突，風險換成「解衝突時順手選 PR 側把版號降回去」——解完固定跑 `git diff origin/develop -- src/__manifest__.py` 確認方向往上。
