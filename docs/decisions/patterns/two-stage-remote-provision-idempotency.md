# Pattern: 多段式 remote-resource provision 必須冪等

> 適用：一個 method **先建立遠端資源**（CF tunnel、雲端 DNS、外部 SaaS 帳號、k8s namespace…），**再執行後續步驟**（補 config、寫關聯 row、re-sync）。只要「建立」與「後續」分成兩段，第二段失敗就會讓 record 落在 `error`，但遠端資源**已經誕生且仍存活**。此時若前端把 retry 按鈕掛在 `error` 上、retry 又無腦重跑第一段 → 在遠端**再建一條**，第一條失去 DB 引用 = 永久孤兒（token 有效、持續計費、靜默資源/帳單外洩）。核心守則：**遠端建立步驟必須被 stored-id guard 包住，讓每條失敗路徑的 retry 都安全**；加段就要加 guard。

## 問題敘事（本案實際撞牆過程）

Security Access (SA) 的 `action_provision` 是**兩段式**：

1. **Stage 1 — `client.create_tunnel`**：Cloudflare 實體 tunnel 誕生、operator 回 `tunnel_id` / `tunnel_token` 並寫進 record、同時建 primary DNS record。
2. **Stage 2 — `_sync_routes_to_operator`**：re-sync 把 `http_host_header`（`originRequest`）補上去，因為 `create_tunnel` 的單一 ingress rule 不帶 host header。

第二段失敗（operator 連線抖、route UNIQUE clash、ValidationError…）→ `state='error'`，**但 tunnel 已經在 CF 存在**。前端 SA 表單的 Provision 按鈕條件是 `invisible="state not in ('pending', 'error')"`（`src/views/security_access_views.xml:13`）——所以 `error` 狀態使用者**看得到 Provision 按鈕**，自然會再按一次。

修復前的 retry 直接從頭重跑：第二次 `create_tunnel` 在 CF **又建一條全新 tunnel**——

- 第一條 tunnel 從此沒有任何 DB row 指向它 → **永久孤兒**：token 仍有效、CF 端持續計費、no one is watching。
- 接著 primary route 又撞 DB `UNIQUE`（canonical_name 不變）→ 再次落 `error`。使用者看到的是「retry 還是失敗」，**完全察覺不到背後已經多漏了一條付費 tunnel**。

這是 Phase 6 對抗審查抓到的**唯一一條 critical finding**。

## 機制解釋（為什麼會這樣）

對照同 codebase 的 **SmartHome `action_provision`（`src/models/smart_home.py:135`）是單段**：`create_tunnel` 一次做完全部，沒有第二段 re-sync。對單段 method，**「`error` ⇒ 尚未產生遠端資源」這個不變量恒成立**——`create_tunnel` 要嘛整段成功要嘛整段沒發生，所以即使 retry 無腦重跑也安全，**不需要 guard**。

SA 把 provision 拆成兩段（為了補 host header），就**打破了那個不變量**：現在 `error` 有兩種來源——

- stage 1 失敗 → 遠端**沒有**資源（retry 重建是對的）；
- stage 2 失敗 → 遠端**已有**資源（retry 重建會留孤兒）。

retry 路徑無法靠 `state` 區分這兩種，因為它們都叫 `error`。能區分的是**有沒有 stored remote id**（`tunnel_id`）。**單段 method 的「免 guard 安全」是它的單段性換來的，不是天生的；一旦加第二段，這份安全就得用 guard 重新買回來。**

## 守則（何時適用、怎麼做）

任何「**建立遠端資源 + 後續步驟**」的 method，三件事缺一不可：

1. **遠端建立必須被 stored-id guard 包住**。method 開頭先判「remote id 是否已存在」：
   - 已存在 → **絕不重建**，只重跑後續步驟（re-sync）並嘗試恢復 `active`（`security_access.py:263` `if self.tunnel_id:` 分支）。
   - 不存在 → 才走第一段建立。
2. **每個 `except` 分支都要問兩個問題**：「此刻遠端是什麼狀態？」「retry 會發生什麼？」並據此分類：
   - operator / 連線錯誤 → `state='error'` fail-loud，但**保留 stored id**，讓下一次 retry 走 guard 分支收尾。
   - `AccessError`（field-group 拒絕之類的**安全訊號**）→ **propagate，不可降級**吞成 `state='error'`——降級會把安全拒絕偽裝成良性 provisioning 失敗（`security_access.py:304`、`:414` 兩處對稱 re-raise）。
   - 其餘 broad `Exception` → fail-loud 進 `error`，但同樣**保留 stored id**（`security_access.py:313`、`:428`）。
3. **UI 的 retry 按鈕條件要和 guard 語義對齊**。按鈕可以照舊掛在 `error`（使用者要能重試），但**只有在 method 內部有 guard**時這才安全——retry 進來會命中 guard 分支而非重建。按鈕條件與 guard 是一組契約，不能只改一邊。

附帶守則：**後續步驟自己也要冪等**。本案 primary route 採 **create-or-restamp**——存在就更新 `dns_record_id` 不重建（`security_access.py:378-391` 的 `if not primary: create … elif dns_record_id: write`），避免 retry 撞自家 DB `UNIQUE`。

## 證據（commit / 檔案:行號）

- **idempotency guard 起點**：commit `0d1b6de` `fix: add idempotency guard to security access action_provision`。
- **主修**：commit `766f904` `fix: make provision primary route idempotent and propagate field-group denials`（`src/models/security_access.py` +57 行；同步改 controller 與兩支測試）。相關前序：`ed82f7a`（stage-2 非 operator 錯誤 fail-loud + retry 重建缺失的 primary route）、`6b11fb7`（unlink 孤兒 tunnel fail-loud）。
- **guard 分支**：`src/models/security_access.py:263`（`if self.tunnel_id:` → 只 re-sync 不重建）。
- **失敗路徑分類**：`security_access.py:285`（operator 錯 → error）、`:304` / `:414`（`AccessError` → re-raise 不降級）、`:313` / `:428`（broad → error 但保留 `tunnel_id`）。
- **primary route create-or-restamp**：`security_access.py:378-391`。
- **單段對照（不需 guard）**：`src/models/smart_home.py:135` `action_provision`（`create_tunnel` 即全部）。
- **UI retry 按鈕條件**：`src/views/security_access_views.xml:13` `invisible="state not in ('pending', 'error')"`。

## 偵測 / 測試建議

- **負向測試（核心）**：mock 讓 stage 2 失敗 → assert `state='error'` 且 `tunnel_id` 已 set；接著**再呼叫一次** `action_provision` → assert `create_tunnel` **只被呼叫一次**（spy call_count == 1）、record 收斂到 `active`、無第二條 tunnel。這條直接鎖死孤兒不再生。覆蓋見 `src/tests/test_security_access_lifecycle.py`、`test_security_access_subscription_flow.py`（`766f904` 補）。
- **`AccessError` 不降級測試**：`with_user(非 system)` 觸發 provision → assert raise `AccessError`（**不是**靜默 `state='error'`），守住安全拒絕不被吞。
- **grep 巡檢**：對任何呼叫 `create_*` / `provision` / 外部 SDK 建立 API 的 model method，檢查 method 開頭是否有 stored-id guard；若是兩段以上且**無** guard → 紅旗。可順手比對「同 codebase 的單段同類 method（如 SmartHome）」確認你不是把單段的免-guard 安全錯套到多段上。
- **review checklist 一句話**：看到「provision/create 多了一個後續步驟」的 diff，問「retry 從 error 重跑會在遠端留下什麼？」——答不出「不會留孤兒」就缺 guard。

## See also

- 同 family（安全/不變式守線必須在 ORM / method 這層，不能假設上層擋得住）：[[scope-flip-write-guard-not-ir-rule]]、[[constraint-input-trust-expansion]]。本 pattern 是「冪等不變量必須在 method 內用 stored-id guard 守，不能靠 state 機讀心」的具體 instance。
- 對照失敗模式：`docs/tracking/pitfalls/` 內 SA / agent-runtime 各篇「error 狀態還掛著 retry 入口、單層修復不可觀測」的 gap-chain 敘事。
- 原始來源（合成材料、不複製）：commit `766f904` / `0d1b6de` / `ed82f7a` + 上列 `security_access.py` 行號；Phase 6 對抗審查 critical finding 紀錄見 `docs/tracking/followups/follow-ups.md` security-access 段。
