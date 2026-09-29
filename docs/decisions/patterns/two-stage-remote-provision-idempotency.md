# Pattern: remote-resource provision 必須冪等

> 適用：一個 method **建立遠端資源**（CF tunnel、雲端 DNS、外部 SaaS 帳號、k8s namespace…）。
> 只要**有任何一條路徑會讓遠端資源誕生、而它的 id 沒回寫進 DB**，record 就會落在 `error` 而遠端資源**已經誕生且仍存活**。
> 最容易辨認的來源是「建立」與「後續步驟」分成兩段（第二段失敗）；但**不是唯一來源**——遠端把部分失敗回成 HTTP 200 ＋具名旗標（本 repo 的 `dns_failed`）也會造成同一形狀，**呼叫端沒讀就等於沒看見**（#484，見 §機制解釋）。
> 此時若前端把 retry 按鈕掛在 `error` 上、retry 又無腦重跑建立段 → 在遠端**再建一條**，第一條失去 DB 引用 = 永久孤兒（token 有效、持續計費、靜默資源/帳單外洩）。核心守則：**遠端建立步驟必須被 stored-id guard 包住，讓每條失敗路徑的 retry 都安全**。

## 問題敘事（本案實際撞牆過程）

Security Access (SA) 的 `action_provision` 是**兩段式**：

1. **Stage 1 — `client.create_tunnel`**：Cloudflare 實體 tunnel 誕生、operator 回 `tunnel_id` / `tunnel_token` 並寫進 record、同時建 primary DNS record。
2. **Stage 2 — `_sync_routes_to_operator`**：re-sync 把 `http_host_header`（`originRequest`）補上去，因為 `create_tunnel` 的單一 ingress rule 不帶 host header。

第二段失敗（operator 連線抖、route UNIQUE clash、ValidationError…）→ `state='error'`，**但 tunnel 已經在 CF 存在**。前端 SA 表單的 Provision 按鈕條件是 `invisible="state not in ('pending', 'error')"`（`src/views/security_access_views.xml:13`）——所以 `error` 狀態使用者**看得到 Provision 按鈕**，自然會再按一次。

修復前的 retry 直接從頭重跑：第二次 `create_tunnel` 在 CF **又建一條全新 tunnel**——

- 第一條 tunnel 從此沒有任何 DB row 指向它 → **永久孤兒**：token 仍有效、CF 端持續計費、no one is watching。
- 接著 primary route 又撞 DB `UNIQUE`（canonical_name 不變）→ 再次落 `error`。使用者看到的是「retry 還是失敗」，**完全察覺不到背後已經多漏了一條付費 tunnel**。

這是 Phase 6 對抗審查抓到的**唯一一條 critical finding**。

> ⚠ 兩點限定，2026-08 補記：
> （a）本段描述 2026-06 當時的 SA 形態。軸2 改成 bare tunnel（`security_access.py:245-248` 的 `NOTE: there is deliberately NO SA-level "primary_hostname" field, and no auto-created primary route`、`:477` 起的 `軸2: build a BARE tunnel`）之後已無自動 primary route，**`UNIQUE` clash 那一半不再成立**——見 [[security-access-bare-tunnel-zero-route]]。孤兒 tunnel 那一半不受影響。
> （b）「第二次 `create_tunnel` 在 CF **又建一條**」這句的來源是上述對抗審查，**是讀碼推論不是 live CF 觀測**。CF 是否強制 tunnel 名稱唯一至今未實測（見 §守則末的「已知殘留缺口」）。

## 機制解釋（為什麼會這樣）

這份 doc 原本拿 **SmartHome `action_provision`** 當「單段、免 guard」的對照組，理由是「`create_tunnel` 要嘛整段成功要嘛整段沒發生」。**那個對照組已在 #484 被推翻**（commit `d9f49a6e`），本段保留只為說明推翻的理由：

- operator 的 `POST /tunnels` 本身就是多步的（建 tunnel → 取 token → 設 ingress → 建 DNS，`extra/paas-operator/src/api/tunnels.py` 的 `create_tunnel`），而且 **DNS 失敗被就地吞成 HTTP 200 ＋ `dns_failed=True`**（同檔 `except CloudflareException` → `dns_failed = True` 那段；schema 見 `extra/paas-operator/src/models/schemas.py` 的 `dns_failed` 欄位）。呼叫端只看例外看不到這件事：一次 HTTP 200 也可能代表「tunnel 已誕生且在計費、但 hostname 不會解析」。
- 所以 SmartHome 的 `error` 一樣可以與一條活著的 tunnel 共存，`create_tunnel` 從來不是原子的。SmartHome 現在**也有 stored-id guard**（`src/models/smart_home.py:194` `if self.tunnel_id:` → `raise UserError(...)`，且刻意擺在任何 write 之前）。

真正的判準不是「幾段」，而是：**這個 method 有沒有任何一條路徑，會在遠端留下資源卻沒把它的 id 寫回 DB**。答案是「有」就要 guard——分段只是最容易辨認的一種來源。

（描述層的補充：SmartHome 的 `action_provision` 內確實只有一次 operator 呼叫，字面上仍是「單段」。被 #484 推翻的是**推論**——「所以不需要 guard」——不是那個描述。）

SA 把 provision 拆成兩段（為了補 host header），就**打破了那個不變量**：現在 `error` 有兩種來源——

- stage 1 失敗 → 遠端**沒有**資源（retry 重建是對的）；
- stage 2 失敗 → 遠端**已有**資源（retry 重建會留孤兒）。

retry 路徑無法靠 `state` 區分這兩種，因為它們都叫 `error`。能區分的是**有沒有 stored remote id**（`tunnel_id`）。**單段 method 的「免 guard 安全」是它的單段性換來的，不是天生的；一旦加第二段，這份安全就得用 guard 重新買回來。（#484 後推廣：「免 guard 安全」永遠是某個結構性質換來的——加第二段、或遠端把部分失敗回成 HTTP 200，都會讓它失效。SmartHome 正是後者。）**

## 守則（何時適用、怎麼做）

任何「**建立遠端資源**」的 method（有沒有後續步驟都算），三件事缺一不可：

1. **遠端建立必須被 stored-id guard 包住**。method 開頭先判「remote id 是否已存在」：
   - 已存在 → **絕不重建**（`security_access.py:413` `if self.tunnel_id:` 分支）。
   - 不存在 → 才走建立段。
   - ⚠ **guard 分支要 self-heal 還是 fail-loud，取決於「有沒有一段可重跑、且真能修好遠端的後續步驟」，不可照抄。** SA 有（`:419 _sync_routes_to_operator()` → `:426 _sync_all_routes_access()` → `:427-429` 收斂回 `active` / `active_no_route`）；SmartHome 起初判定自己沒有（「沒有 route payload」），guard 分支選了 **fail-loud**；Smart Home 開通重試修正了這個判斷——它的 desired state 恰好一條 hostname，每一格（`subdomain`、`ha_port`）都已在 DB，所以 `error`／`pending` ＋ 既有 tunnel 現在走 `_converge_existing_tunnel()`（`sync_tunnel_routes` 一條 route → 補 primary route → 收斂回 `active`），其他 state（`active`／`suspended`／`provisioning`）仍 fail-loud。教訓：「沒有可重跑的後續步驟」要逐格核對 desired state 是否真的缺資料，不要因為「沒有 route 模型」就下結論——那個結論讓卡在 `error` 的租戶沒有任何自助出路。
2. **每個 `except` 分支都要問兩個問題**：「此刻遠端是什麼狀態？」「retry 會發生什麼？」並據此分類：
   - operator / 連線錯誤 → `state='error'` fail-loud，但**保留 stored id**，讓下一次 retry 走 guard 分支收尾（guard 分支 `security_access.py:430`／`:440`；first-provision 分支 `:526`／`:532`）。
   - `AccessError`（field-group 拒絕之類的**安全訊號**）→ **propagate，不可降級**吞成 `state='error'`——降級會把安全拒絕偽裝成良性 provisioning 失敗（`security_access.py:449` 註解 +`:457` `raise`、`:538` 註解 +`:550` `raise`，兩處對稱）。
   - 其餘 broad `Exception` → fail-loud 進 `error`，但同樣**保留 stored id**（`security_access.py:458-466`、`:551-565`）。
3. **UI 的 retry 按鈕條件要和 guard 語義對齊**。按鈕可以照舊掛在 `error`（使用者要能重試），但**只有在 method 內部有 guard**時這才安全——retry 進來會命中 guard 分支而非重建。按鈕條件與 guard 是一組契約，不能只改一邊。

附帶守則：**後續步驟自己也要冪等**。SA 後來改成 **bare tunnel、零自動 primary route**（`security_access.py:245-248`、`:477` 起，見 [[security-access-bare-tunnel-zero-route]]），原本的 primary-route create-or-restamp 已不存在。**守則不變**，現在的實例落在 route 層：`_sync_routes_to_operator` 把 operator 回來的 server-owned DNS anchor **就地 restamp 而非重建**（`security_access.py:687` `_internal_identity(route).dns_record_id = rec_id`，僅在 `route.dns_record_id != rec_id` 時才寫）。

**已知殘留缺口：stored-id guard 是必要條件、不是充分條件。**
遠端建立端若在「資源已誕生」之後才失敗、且**不做補償刪除也不回傳 id**，那條孤兒在結構上抓不到（沒有 id 可存）。本 repo 現存實例：`extra/paas-operator/src/api/tunnels.py` 的 `create_tunnel` 在 step 1（`cloudflare_service.create_tunnel`）成功後，step 2 取 token 或 step 3 設 ingress 若拋 `CloudflareException`，就直接 `raise HTTPException` 回 4xx/5xx，**沒有任何補償性 `delete_tunnel`**（該呼叫只存在於同檔的 DELETE endpoint）。此時 tunnel 已在 CF、id 從未回到 Odoo。後果**取決於一件本 repo 沒實測過的事——CF 是否強制 tunnel 名稱唯一**，兩種形狀都要寫出來：

- **若 CF 接受重名** → 每次 retry 多一條孤兒 tunnel。
- **若 CF 拒絕重名** → 第一條孤兒仍在，且該 record **永久卡死**（兩個呼叫點的 tunnel 名稱都是確定性的——`security_access.py:483 name=self.canonical_name`、`smart_home.py:220 name=self._generate_tunnel_name()`——所以每次 retry 都送一模一樣的名字，會一直撞同名 4xx）。

**兩種形狀都得靠遠端建立端修**，這點不受影響。但**處方不同**：若 CF 拒絕重名，「部分失敗時仍把 id 回給呼叫端」（`dns_failed` 那個形狀）**並不足夠**，真正需要的是補償刪除或 adopt-by-name。
⚠ 這件事**尚未實測**：operator 端 `cloudflare.py` 的 `create_tunnel` 不做 pre-check、不處理重名，非 2xx 就 raise；`extra/paas-operator/tests/test_tunnels.py` 的 16 條測試裡只有 `:166 test_create_tunnel_api_failure`，**無任何重名測試**。要定案得在 stg CF 帳號用同名打兩次 `POST /accounts/{id}/cfd_tunnel`（**對外不可逆動作，需事前授權**）或查 CF 官方 error code。

## 證據（commit / 檔案:行號）

- **idempotency guard 起點**：commit `0d1b6de` `fix: add idempotency guard to security access action_provision`。
- **主修**：commit `766f904` `fix: make provision primary route idempotent and propagate field-group denials`（`src/models/security_access.py` +57 行；同步改 controller 與兩支測試）。相關前序：`ed82f7a`（stage-2 非 operator 錯誤 fail-loud + retry 重建缺失的 primary route）、`6b11fb7`（unlink 孤兒 tunnel fail-loud）。
- **guard 分支**：`src/models/security_access.py:413`（`if self.tunnel_id:` → 只 re-sync 不重建）。
- **失敗路徑分類**：`security_access.py:430`／`:440`（連線／operator 錯 → error，guard 分支）、`:526`／`:532`（同上，first-provision 分支）、`:449`+`:457` / `:538`+`:550`（`AccessError` → re-raise 不降級）、`:458-466` / `:551-565`（broad → error 但保留 `tunnel_id`）。
- **後續步驟冪等的現行實例**：`security_access.py:687`（route DNS anchor 就地 restamp）。
- **第二支 guard（#484 之後）**：`src/models/smart_home.py:165` `action_provision` → `:194` `if self.tunnel_id:` fail-loud；commit `d9f49a6e`。**它曾是本 doc 的「單段免 guard」對照組，現已不是。** 後來 guard 分支再改成「`pending`／`error` 收斂、其他 state fail-loud」（`SmartHome._converge_existing_tunnel`，測試 `src/tests/test_sh_provision_retry.py`）。
- **UI retry 按鈕條件**：`src/views/security_access_views.xml:13` `invisible="state not in ('pending', 'error')"`。

## 偵測 / 測試建議

- **負向測試（核心）**：mock 讓 stage 2 失敗 → assert `state='error'` 且 `tunnel_id` 已 set；接著**再呼叫一次** `action_provision` → assert `create_tunnel` **只被呼叫一次**（spy call_count == 1）、record 收斂到 `active`、無第二條 tunnel。這條直接鎖死孤兒不再生。覆蓋見 `src/tests/test_security_access.py:210` `test_provision_retry_after_tunnel_exists_does_not_recreate`（`:228` `create_tunnel.call_count == 1`、`:236` `create_tunnel.assert_not_called()`）與 `:241` `test_provision_resync_failure_keeps_tunnel_for_retry`；SmartHome 側見 `src/tests/test_named_failure_fields_484.py` 的 `test_retry_after_dns_failure_does_not_create_a_second_tunnel` / `test_provision_on_an_active_smart_home_is_also_guarded`，以及 `src/tests/test_sh_provision_retry.py`（收斂／拒絕／失敗保留 error 的完整矩陣）。
  （原文寫「覆蓋見 `test_security_access_lifecycle.py`、`test_security_access_subscription_flow.py`」——兩檔都存在但**都不含 `create_tunnel`**，前者是 cron 測試檔（`:26 class TestSecurityAccessCron`，全部 test method 都是 `test_cron_*`）。2026-08 更正。）
- **`AccessError` 不降級測試**：`with_user(非 system)` 觸發 provision → assert raise `AccessError`（**不是**靜默 `state='error'`），守住安全拒絕不被吞。
- **grep 巡檢**：對任何呼叫 `create_*` / `provision` / 外部 SDK 建立 API 的 model method，檢查 method 開頭是否有 stored-id guard。判準是 **任一條路徑會在遠端留下資源而不回寫 id → 紅旗**（分段只是最好認的一種來源；另一種是遠端把部分失敗回成 HTTP 200 ＋具名旗標，例如 `dns_failed`——呼叫端沒讀就等於沒看見）。可順手比對 SmartHome（`smart_home.py:194`）與 SA（`security_access.py:413`）**兩支 guard 的分支差異**，決定你的 guard 分支該 self-heal 還是 fail-loud——**不要**再把 SmartHome 當「單段免 guard」的對照組，它已經不是了。
- **review checklist 一句話**：看到「provision/create 多了一個後續步驟」的 diff，問「retry 從 error 重跑會在遠端留下什麼？」——答不出「不會留孤兒」就缺 guard。

## See also

- 同 family（安全/不變式守線必須在 ORM / method 這層，不能假設上層擋得住）：[[scope-flip-write-guard-not-ir-rule]]、[[constraint-input-trust-expansion]]。本 pattern 是「冪等不變量必須在 method 內用 stored-id guard 守，不能靠 state 機讀心」的具體 instance。
- 對照失敗模式：`docs/tracking/pitfalls/` 內 SA / agent-runtime 各篇「error 狀態還掛著 retry 入口、單層修復不可觀測」的 gap-chain 敘事。
- 原始來源（合成材料、不複製）：commit `766f904` / `0d1b6de` / `ed82f7a` + 上列 `security_access.py` 行號；Phase 6 對抗審查 critical finding 紀錄見 `docs/tracking/followups/follow-ups.md` security-access 段。
