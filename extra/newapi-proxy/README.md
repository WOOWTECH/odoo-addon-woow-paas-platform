# newapi-proxy

PaaS Odoo 呼叫 new-api 的**唯一**通道，也是 PaaS 出錯或被攻破時的那道牆（AI 模型分頁設計 §1.4，票 LLM-01a）。

- 程式碼在這裡（`extra/newapi-proxy/`），CI 建 image、只推不可變的 `<commit sha>` tag 到 JCR。
- 部署由 new-api 部署 repo（Gitea `woow-paas/Woow_k3s_newapi`）的 chart component `proxy` 以 **digest** 釘住：stg 在 `new-api-stg`、prod 在 `new-api`（N1）。AP／OP 兩把服務帳號 PAT 只放在 new-api chart 的 Secret；Odoo 只拿簽章金鑰。
- 不碰 Odoo、不碰資料庫；重放紀錄放在同一個 release 的 Valkey（專屬 ACL 使用者，只能 `SET` 自己前綴的 key）。Python 3.12＋Starlette（只用 Request／Response）＋httpx＋uvicorn（h11）。

```
Odoo web／cron ──(HMAC 簽章＋nonce＋X-Woow-Identity＋X-Woow-Client-IP)──► newapi-proxy:8080 ──(單一 XFF＝合成 IP；AP／OP 由代理注入)──► newapi:3000
                                                                            │  └─ 重放紀錄 SET NX EX ──► newapi-valkey:6379（ACL 使用者 newapi-proxy）
kubelet／helm test／Odoo 健康檢查 ──► newapi-proxy:8081（/healthz、/readyz、/metrics）
```

## 線上契約（給 `src/services/newapi_client.py`，LLM-01b）

### 稽核呼叫識別（LLM-12b）

每個請求由代理產生獨立 UUID v4 `attempt_id`，同一次呼叫的 access 與 audit 紀錄共用它；
`sending`、`forwarded`、`connect`、`reset`、`timeout`、`cancelled` 等結果可據此配對。
即使上游沒有回傳 `X-Oneapi-Request-Id`，仍保留同一個 `attempt_id`。它不取自使用者標頭，
也不取代上游 `request_id`；相同內容的兩次呼叫會有不同 ID。

這只是事件配對欄位。stdout 紀錄尚不構成持久、完整的對帳來源；收集器必須另外證明保存期間、
環境歸屬與覆蓋水位。不能將 `sending` 與後續結果各算一次入帳，也不能將缺少結果視為未執行。
共用同一簽章金鑰的 Odoo DB 無法僅靠 `kid` 區分，啟用 I3 前須完成身分隔離或跨 DB 認領。

### 請求標頭

| 標頭 | 內容 |
|---|---|
| `X-Woow-Proxy-Kid` | 金鑰 id（`[A-Za-z0-9_-]{1,64}`） |
| `X-Woow-Proxy-Ts` | unix 秒；代理只接受 \|now − ts\| ≤ 30 秒 |
| `X-Woow-Proxy-Nonce` | **每個請求重新產生**的隨機值，`[A-Za-z0-9_-]{22,64}`、至少 128 bit（Python：`secrets.token_urlsafe(16)`，22 字元）。重送（含 busy／connect 之後的重送）也要換新 |
| `X-Woow-Proxy-Sig` | 小寫 hex 的 HMAC-SHA256（見下） |
| `X-Woow-Identity` | `ap`｜`op`｜`user`｜`anon`｜`bg` |
| `X-Woow-Client-IP` | 只有 `user` 必帶：租戶合成 IP；其他身分**不得**帶 |
| `Authorization` | 只有 `user` 帶（`Bearer <UP 或開戶 JWT>`；`/api/user/login` 例外不帶）；其他身分帶了一律 400 |
| `X-Security-Proof` | 只有 `POST /api/user/access_tokens` 帶（必帶）；其他路由帶了一律 400 |
| `X-Woow-Read-Timeout` | 選填，1–30 整數秒；PaaS 設成自己的 read timeout 減 1 秒。不帶＝`PROXY_DEFAULT_READ_TIMEOUT`（預設 10）。這是**整個請求**的期限（見〈期限〉） |

同一個標頭出現兩次＝400。其餘進來的標頭（`X-Forwarded-For`、`X-Real-IP`、`Forwarded`、`X-Forwarded-*`、`Cookie`、`Origin`、其他 `X-Woow-*`…）**全部不轉送**：代理對上游只送自己組的標頭。

### 簽章

```
string_to_sign = method "\n" canonical_path "\n" canonical_query "\n"
                 hex(sha256(body)) "\n" ts "\n" nonce "\n" kid "\n"
                 identity "\n" client_ip
sig            = hex(HMAC-SHA256(secret 的 ASCII bytes, string_to_sign))
```

- `canonical_path`：原樣的路徑。代理只收已經是 canonical 的路徑（見〈canonical 化〉），所以就是送出去的那串。
- `canonical_query`：query 依 key 排序、`k=v` 以 `&` 連接、**不做 percent-encoding**（允許的字元本來就不需要）；沒有 query 就是空字串。建議 PaaS 直接照這個順序送。
- `body`：送出去的 bytes（GET／DELETE 是空的）。
- `nonce`、`kid`：就是 `X-Woow-Proxy-Nonce`、`X-Woow-Proxy-Kid` 的值（簽章綁定 kid，換標 kid 會驗不過）。
- `client_ip`：`user` 是 `X-Woow-Client-IP` 的值，其他身分是空字串。
- **重放紀錄**：每個 `(kid, nonce)` 只接受一次，跨所有 replica。代理先查自己的記憶體（第一層），再到 Valkey `SET newapi-proxy:replay:<kid>:<nonce> 1 NX EX 62`（第二層、共用）；任一層看過＝401 `auth`（log reason `auth_replay`）。62 秒＝2×30＋2：ts 最多在 `ts+30` 秒前都會被接受，第一次最早可能在 `ts−30` 就到，紀錄必須活過整段。
- Valkey 連不上或沒有明確回答（逾時、驗證失敗）→ **503 `busy`**（reason `replay_store`），請求沒有送出；client 換新的 nonce 重送。new-api 自己的限流也靠同一個 Valkey，所以它掛的時候 new-api 本來就會回 500。
- 兩個合法、內容相同的請求在同一秒送出（連點、兩個分頁、兩條 cron thread），nonce 不同，簽章也不同，兩個都會過。
- 參考實作：`newapi_proxy/signing.py`（`signed_headers()`、`new_nonce()`）；只用標準函式庫的同款在 `scripts/signed_request.py`，`tests/test_proxy_script_and_server.py` 讓兩者保持一致。

### 金鑰（N23）

env 格式 `kid:secret[,kid:secret]`：第一把是現用、後面是下一把（輪替不停機，代理接受清單內任一把）。kid `[A-Za-z0-9_-]{1,64}`、secret `[A-Za-z0-9_-]{32,256}`；值會先 strip。格式不合、kid 重複、同一個 kid 或 secret 同時出現在兩組 → 代理不啟動（exit 78），錯誤訊息只寫變數名與問題，不帶值。

| 組 | Odoo 端 env | 能做什麼 |
|---|---|---|
| `WEB` | `WOOW_NEWAPI_PROXY_AUTH_WEB`（web deployment） | 只能用 `user` 身分的互動路由（下表 WEB 欄 ✓）；不能 login、verify、access_tokens、sessions，不能用 ap／op／anon／bg |
| `CRON` | `WOOW_NEWAPI_PROXY_AUTH_CRON`（cron deployment） | 全部身分與路由；`bg` 只有 CRON 能用 |

代理端對應 `PROXY_AUTH_WEB`、`PROXY_AUTH_CRON`（chart 從 Secret 帶入）。Odoo 一律用清單的**第一把**簽；輪替步驟在 Woow_k3s_newapi `docs/OPERATIONS.md`。

### 身分與合成 IP（§1.4.2-2、-4）

| 身分 | 上游 `Authorization` | `X-Forwarded-For` |
|---|---|---|
| `ap` | 代理注入 AP（`PROXY_AP_TOKEN`） | `198.18.0.1` |
| `anon` | 無 | `198.18.0.2` |
| `op` | 代理注入 OP（`PROXY_OP_TOKEN`） | `198.18.0.3` |
| `bg` | 代理注入 AP | `198.18.0.4`（讓 break-glass 在 new-api 稽核裡一眼可辨） |
| （代理自己的 readiness） | 無 | `198.18.0.5`（不是身分；不和 anon 共用 new-api 的 GA 限流桶） |
| `user` | 轉送 PaaS 帶的 UP／JWT（帶的若是 AP 或 OP 本身 → 403） | `X-Woow-Client-IP`，必須在 `PROXY_TENANT_IP_RANGE` 內、點分十進位無前導零 |

`PROXY_TENANT_IP_RANGE`：prod `198.18.1.0-198.19.255.255`（block 0）；stg `198.18.17.0-198.19.255.255`（只收 block 1–31，`198.18.0.0 + 256 + block×4096 + id`）。

### canonical 化（〔REV〕17）

看的是**未解碼**的 `raw_path` 與 query string：

- 路徑含 `%`、`\`、`;`、`..`、`//`、`.` 段、空白／控制字元、非 ASCII，或不是 `/[A-Za-z0-9/_.-]*` → 400。
- 路徑參數：`{id}` 是 `1`–`2147483647`、不得有前導零；`{sid}` 是 `[A-Za-z0-9_-]{1,128}`。對不上樣板 → 403。
- 轉送的 URL 一律由樣板＋驗證過的參數**重組**，不轉送 client 原始路徑。
- query：只收 `[A-Za-z0-9_.-=&:/]`（沒有 `%`、`+`、`;`）、每段剛好一個 `=`、值不能空、key 不能重複；每條路由宣告允許的 key 與值格式，多一個 key（例如 new-api 的別名 `ps`）就 400。

### 路由白名單（DESIGN §1.4.2-5；LLM-02 補齊）

| 身分 | 方法與路徑 | WEB | query | body 政策 |
|---|---|---|---|---|
| anon、ap、user | `GET /api/status` | | | |
| ap、user | `GET /api/pricing` | | | |
| op | `GET /api/option/` | | | |
| ap | `POST /api/user/` | | | 建使用者 |
| ap | `GET /api/user/` | | `p`、`page_size`（1–100）、`sort_by`（只收 `id`）、`sort_order`（`asc`／`desc`） | 全部使用者分頁（餘額刷新，LLM-02b；PaaS 自己以前綴過濾）。**回應由代理重組**（見〈回應與錯誤〉） |
| ap | `GET /api/user/search` | | `keyword`（必須符合 `PROXY_USERNAME_REGEX`）、`p`、`page_size` | |
| ap | `GET /api/user/{id}` | | | |
| ap、bg | `POST /api/user/manage` | | | ap：add_quota（含目標預檢與每小時總量）；bg：只有 `mode: subtract`（事後扣回，目標預檢、不吃加額度預算） |
| ap | `GET /api/log/` | | `p`、`page_size`、`type`、`start_timestamp`、`end_timestamp`、`model_name`、`token_name`、`group`、`request_id`、`username`（必須符合 `PROXY_USERNAME_REGEX`） | 不收 `channel`、`upstream_request_id` |
| ap | `GET /api/log/stat` | | 同上去掉分頁與 `request_id` | |
| ap | `GET /api/data/`、`GET /api/data/flow` | | `start_timestamp`、`end_timestamp`、`username`（同上） | |
| ap | `GET /api/data/users` | | `start_timestamp`、`end_timestamp` | |
| ap | `GET /api/verify/methods` | | `scope`（`<a-z_>.<a-z_>`） | |
| bg | `PUT /api/user/` | | | 只收 `{"id", "group": "default"｜"free_blocked"}`；代理先以 AP 讀該使用者、確認是 PaaS 使用者，再帶回 `username`／`display_name`／`remark`、`role: 1`，只改 `group` |
| user | `POST /api/user/login` | | | 登入（不帶 Authorization） |
| user | `POST /api/verify` | | | verify |
| user | `GET /api/user/access_tokens` | | | |
| user | `POST /api/user/access_tokens` | | | 建 UP（必帶 proof） |
| user | `DELETE /api/user/access_tokens/{id}` | | | 撤孤兒／舊 UP（必帶 `access_token.revoke` 的 proof） |
| user | `DELETE /api/user/sessions/{sid}` | | | |
| user | `GET /api/audit/self` | | `p`、`page_size` | |
| user | `GET /api/user/self` | ✓ | | |
| user | `GET /api/user/models`、`GET /api/user/self/groups` | ✓ | | |
| user | `POST /api/token/` | ✓ | | 建金鑰 |
| user | `PUT /api/token/` | ✓ | `status_only`（只收 `true`） | 整筆更新（建金鑰的欄位＋`id`）；`status_only=true` 只收 `{"id", "status": 1｜2}` |
| user | `POST /api/token/batch` | ✓ | | 批次刪除：`{"ids": [1–100 個不重複的正整數]}` |
| user | `GET /api/token/` | ✓ | `p`、`page_size` | |
| user | `GET /api/token/{id}`、`DELETE /api/token/{id}` | ✓ | | |
| user | `POST /api/token/{id}/key` | ✓ | | reveal 預檢（無 body） |
| user | `GET /api/log/self` | ✓ | `p`、`page_size`、`type`、`start_timestamp`、`end_timestamp`、`model_name`、`token_name`、`group`、`request_id` | |
| user | `GET /api/log/self/stat` | ✓ | 同上去掉分頁與 `request_id` | |
| user | `GET /api/data/self`、`GET /api/data/flow/self` | ✓ | `start_timestamp`、`end_timestamp` | |

同一條路由給好幾個身分時（`/api/status`、`/api/pricing`、`/api/user/manage`），body 政策依身分決定規則。其他一律 403＋`X-Woow-Proxy-Error: policy`，特別是 `/v1/*`、`/pg/*`、`/api/option/*`（op 的 GET 除外）、`/api/channel/*`、`/api/redemption/*`、`/api/user/topup*`、`/api/user/auth/*`、ap／user 的 `PUT /api/user/`、`POST /api/token/batch/keys`、`GET /api/log/self/export`、`GET /api/log/export`、`GET /api/token/search`、`PATCH /api/user/access_tokens/{id}`。代理埠（8080）上沒有任何不需簽章的路徑：`/healthz`、`/readyz`、`/metrics` 在管理埠（8081）。

`bg`（break-glass，〔REV〕1）：**預設關**（`PROXY_BREAK_GLASS_UNTIL` 未設）。只有 CRON 金鑰、而且 now < `PROXY_BREAK_GLASS_UNTIL`（最多 24 小時後；設得更遠視同關閉並告警）才放行；只有上表兩條路由（上游一律帶 AP、來源 IP `198.18.0.4`）。每次使用都寫稽核 log（`action: bg`，`op` 是 `user_group`／`manage_subtract`）、一行 WARNING `break_glass_used`（log 告警的鉤子）、並累加 `newapi_proxy_break_glass_total`／`_calls_total`。

### body 政策（§1.4.2-5、§3.3）

所有 JSON body：UTF-8、無 BOM、無重複 key、無 NaN／Infinity、最外層是 object、≤ 256 KB（超過 413）、標頭送完後 5 秒內要送完（逾時 408 `body_timeout`）。驗過的欄位**重新序列化**後才送出；new-api 解 JSON 不分大小寫又忽略未知欄位，所以規則一律是「剛好這些欄位、剛好這些型別」（`true` 不算 `1`、`1.0` 不算整數）。違反 → 403 `policy`。

| 路由 | 只接受 |
|---|---|
| `POST /api/user/` | `{"username": 符合 PROXY_USERNAME_REGEX, "password": 32–128 個可見 ASCII, "display_name": "PaaS <數字>", "role": 1}` |
| `POST /api/user/manage` | `{"id": >0, "action": "add_quota", "mode": "add", "value": 1–156,250,000}`；送出前代理以 AP `GET /api/user/{id}` 確認 `username` 符合前綴、`role==1`、`group=="default"`；再加每小時總量上限（`PROXY_ADD_QUOTA_HOURLY_CAP_POINTS`÷`PROXY_REPLICAS`，滑動一小時，超過 429 `budget`）。**預留的額度何時還回去**：確定沒送出（預檢沒過、connect），或 new-api 的回答證明沒執行——HTTP 429、5xx 且 body 空、body 帶 `code`、HTTP 200 `success:false` 帶 `message_key`；結果未知（reset／timeout、`success:false` 沒有 `message_key`、`new_api_panic`、非 JSON）照算 |
| `POST /api/user/login` | `{"username": 符合前綴, "password": 32–128}` |
| `POST /api/verify` | `{"method": "password", "scope": "access_token.generate"｜"access_token.revoke", "context": …, "password": …}`；generate 的 `scopes` ⊆ UP scope、`expires_at` 在 now+1 小時 1 分～now+92 天；revoke 的 context 只有 `token_id` |
| `POST /api/user/access_tokens` | `{"name": "woow-paas-up-<id>-<YYYYMMDDHHMM>", "scopes": ⊆ UP scope, "expires_at": 同上}` |
| `POST /api/token/` | §3.3 的 10 個欄位一個不少；`name` 是 `wp_<數字>_[a-z0-9]{4}`；`group` ∈ {default, free}；`allow_ips` 是空字串；`cross_group_retry`、`allow_user_quota_query` 是 false；`group=free` 時 `model_limits_enabled=true`、`model_limits` ⊆ `PROXY_FREE_MODELS`、`unlimited_quota=false`、`remain_quota=1`（保險絲） |
| `POST /api/token/{id}/key` | 無 body。代理先用**同一把 UP、同一個合成 IP** `GET /api/token/{id}`：`name` 符合 `wp_<數字>_…`、`created_time` 在 120 秒內（容許 new-api 時鐘快 5 秒）才轉送，否則 403 `denied`（〔REV〕2） |
| `PUT /api/token/` | 整筆：上一列的規則＋`id`（new-api 會覆寫每一個送去的欄位，免費金鑰的保險絲照樣守）；`?status_only=true`：只收 `{"id", "status": 1｜2}`（3／4 是 new-api 自己的狀態） |
| `POST /api/token/batch` | `{"ids": [...]}`：1–100 個不重複的正整數 |
| `POST /api/user/manage`（bg） | `{"id": >0, "action": "add_quota", "mode": "subtract", "value": 1–156,250,000}`；目標預檢同 add，但 `group` 可以是 `default` 或 `free_blocked`；不吃加額度預算 |
| `PUT /api/user/`（bg） | `{"id": >0, "group": "default"｜"free_blocked"}`；代理以 AP `GET /api/user/{id}` 確認是 PaaS 使用者（前綴、`role==1`、`group` ∈ {default, free_blocked}、`display_name` ≤ 20、`remark` ≤ 255），送出 `{id, username, display_name, group, remark, role: 1}`——不帶密碼、`admin_permissions`（那兩個要 admin proof） |

UP scope＝`api_key:read`、`api_key:reveal`、`api_key:write`、`profile:read`、`usage:read`。

### 預檢（reveal、add_quota）的回答怎麼轉給 client

受保護的那個呼叫（`POST /api/token/{id}/key`、`POST /api/user/manage`）**只有**在預檢 GET 回 HTTP 200 `success:true` 且通過檢查時才送出。其他情況它都沒有送出，代理依預檢的回答回覆：

| 預檢的回答 | 代理回覆 | client 分類 |
|---|---|---|
| 連不上、逾時、斷線；HTTP 429（帶回 `Retry-After`）；5xx 且沒有 `code`；`success:false` 沒有 `message_key`（例：DB 暫時錯誤）；非 JSON | 502 `connect`，reason `<reveal｜manage>_precheck_<…>` | NotExecuted，可重送 |
| 任何狀態碼、body 帶 `code`（例：UP／AP 被撤或過期 401 `AUTH_UNAUTHORIZED`、`ACCESS_TOKEN_EXPIRED`；403 `ACCESS_TOKEN_SCOPE_DENIED`）；或 HTTP 200 `success:false` 帶 `message_key` | **原樣轉回** new-api 的狀態碼與 body，另加 `X-Woow-Proxy-Precheck: reveal｜manage`，不帶 `X-Woow-Proxy-Error` | 依 `code`／`message_key` 判成 Definite（§3.4：UP 401 → 重簽；AP 401／403 → circuit、停 outbox、告警） |
| `success:false`、`message` 是 `record not found` | 403 `denied`，reason `reveal_not_found`／`manage_not_found` | Definite、確定沒執行；不開 circuit |
| `success:true` 但不符合條件 | 403 `denied`，reason `reveal_window`／`reveal_name`／`reveal_token_mismatch`／`manage_target` | Definite、確定沒執行；不開 circuit |

### 回應與錯誤（§1.4.2-6、-7）

上游的狀態碼與 body 原樣回傳，標頭只留 `Content-Type`、`X-Oneapi-Request-Id`、`Retry-After`（`Set-Cookie` 等一律丟掉），另加 `Cache-Control: no-store`。

**例外：`GET /api/user/`（`ap.user.list`）的 body 由代理重組**（`newapi_proxy/responses.py`）。rc.44 的 `GetAllUsers` 用 `Unscoped()` 列出整張 `users` 表、只拿掉 `password` 與 `access_token`，也不像 `GET /api/user/{id}` 會檢查 `canManageTargetRole`——原樣轉回的話，CRON 金鑰讀得到 root 與管理員的 email、`setting`（webhook secret、Gotify token、Bark 網址）、`aff_code`、Stripe 與 OAuth id；stg 還看得到其他沙盒的使用者。所以：

- HTTP 200 `success:true`：只回 `{success, message, data: {page, page_size, total, items}}`；每個 item 投影成 `{id, username, quota, used_quota, request_count, status, DeletedAt}`，**只限** `username` 符合 `PROXY_USERNAME_REGEX`、而且這幾個值的型別都對（整數；`DeletedAt` 是 null 或 RFC 3339）；其他使用者（root、管理員、別的前綴、型別不對的）只剩 `{"id": <id 或 null>, "username": null}`。**item 數量不變**：Odoo 以「這頁比 page_size 短」判斷一輪讀完，少回幾筆會讓它提早收尾。
- 其他 JSON object（`success:false`、錯誤狀態碼）：只留 client 分類用得到的 `success`、`message`、`message_key`、`code`、`error.type`。空 body 照轉。
- 形狀不對（不是 JSON、重複 key、`data`／`items`／某個 item 不是預期型別、分頁欄位不是非負整數）：**fail closed**，回 500 `internal`（reason `response_*`），上游的 body 一個 byte 都不轉。代理自己產生的錯誤一定帶 `X-Woow-Proxy-Error`，body 是 `{"success":false,"message":"newapi-proxy: <class>","proxy_error":"<class>","reason":"<code>"}`（401 的 reason 一律只寫 `auth`，細節只進 log）。

| `X-Woow-Proxy-Error` | HTTP | 什麼時候 | client 分類建議（§3.4） |
|---|---|---|---|
| `bad_request` | 400 | 格式不合（路徑、query、標頭、JSON、client IP） | Definite（PaaS bug） |
| `too_large` | 413 | body > 256 KB | Definite（PaaS bug） |
| `body_timeout` | 408 | 標頭送完後 5 秒內 body 沒送完 | NotExecuted（沒轉送） |
| `auth` | 401 | 缺簽章或 nonce、kid 不認得、ts 超過 ±30 秒、簽章錯、重放 | Definite（PaaS bug／設定錯），circuit 開 |
| `policy` | 403 | 白名單外、WEB 越權、body 政策、bg 未開 | Definite（PaaS bug），circuit 開 |
| `denied` | 403 | 預檢判定**這一把金鑰／這一個使用者**不行（見上表） | Definite、確定沒執行；**預期內的業務結果，不開 circuit**（reveal 失敗 → 鏡像 `failed_reveal`；add_quota → 訂單人工處理） |
| `budget` | 429 | 加額度每小時總量用完 | NotExecuted＋告警 |
| `busy` | 503 | 該身分 in-flight 滿了（reason `inflight`）、共用重放紀錄無法確認（`replay_store`） | NotExecuted（換新 nonce 重送） |
| `config` | 503 | 該身分需要的 PAT 沒設定 | NotExecuted＋告警 |
| `connect` | 502 | 沒連上 new-api、期限不夠送出（`deadline`）、或預檢沒有明確回答（受保護的請求**沒送出**），預檢遇到 429 會帶回 `Retry-After` | NotExecuted（可重送） |
| `reset` | 502 | 送出後斷線、回應超過 1 MiB | Unknown（寫入不重送） |
| `timeout` | 504 | 請求已送出、期限內沒收完回應 | Unknown（寫入不重送） |
| `internal` | 500 | 代理本身的未預期例外；`GET /api/user/` 的回應形狀不對（reason `response_*`） | Unknown |

代理**不重試**（httpx `retries=0`、不跟 redirect、不讀 proxy env）；寫入用一次性連線（`Connection: close`），不會因為連線池裡過期的 socket 把寫入變成「結果未知」。httpx／h11 的例外只轉成分類，例外文字不進 log 也不進回應。

### 期限（§3.5、§1.4.2-7）

`X-Woow-Read-Timeout` 是**從請求進到代理開始算**的總期限：讀 body、重放紀錄、預檢、上游呼叫全部在這之內做完，讓分類一定由代理給出、不會變成 Odoo 先逾時的 Unknown。

- 每次上游呼叫的預算＝剩餘時間：connect 取 `min(2 秒, 剩餘)`，整段（連線、送出、收標頭、收完 body）再包一層總期限；httpx 自己的 read timeout 是「每次 socket 讀取」，對方慢慢吐 bytes 時擋不住，所以另外包。
- 總期限到的時候，請求標頭還沒開始送（httpcore 的 `send_request_headers` trace 事件還沒發生）→ 502 `connect`；已經開始送 → 504 `timeout`。
- 預檢每次最多 3 秒，而且一定留至少 0.5 秒給受保護的呼叫；剩不到 0.5 秒就不送，回 502 `connect`（reason `deadline`）。
- 代理收到 SIGTERM 後最多等 40 秒讓進行中的請求做完（最長的請求是 30 秒）；chart 的 `terminationGracePeriodSeconds` 是 60（preStop 5＋40＋餘裕）。

### log、稽核、metric（§1.4.2-8）

- stdout 一行一個 JSON。access log 只有 method、路由名稱（樣板，不含 id）、status、上游 status、耗時、`X-Oneapi-Request-Id`、合成 IP、身分、kid、錯誤分類；**不記任何標頭值與 body**（nonce 也不記）。uvicorn 自己的 access log 關掉。
- 稽核 log（`"event":"audit"`）：每次 reveal（`token_id`）、每次 add_quota（`target_user_id`、`value`）、每次 bg（`op`、`target_user_id`，`value` 或 `group`），都記 kid、合成 IP、結果與 request id。**送出前先寫一筆 `result: "sending"`**，結果出來再寫一筆（`forwarded`＋status、`connect`、`reset`、`timeout`、`cancelled`；add_quota 另記 `budget: released｜kept`）；pod 在呼叫途中被殺（OOM、SIGKILL、關機逾時）時，對帳仍看得到「可能已執行」的那一筆。沒送出的寫 `not_sent`／`denied`／`precheck_relayed`；body 政策擋下的 add_quota（例如 `mode: override`）也記目標與 value（只記型別剛好是整數的 `id`／`value`）。這份紀錄 Odoo 改不到，對帳時拿來和 `llm_event` 比（§4.4 I3、LLM-14）。
- 第二道防線：每一行輸出前都經過遮罩（`nap_…`、`sk-…`、`Bearer …`、JWT、64 位 hex、以及設定裡的每一把金鑰、PAT 與 Valkey 密碼原文）；例外只記型別名稱。
- `/metrics`（管理埠，Prometheus 文字格式，數值一律完整輸出）：`newapi_proxy_requests_total`、`_rejections_total`、`_upstream_errors_total`、`_replay_store_errors_total`、`_readiness_checks_total`、`_reveal_total`、`_add_quota_total`、`_add_quota_value_total`、`_break_glass_total`（每次 bg 嘗試，`active`／`off`）、`_break_glass_calls_total`（放行的 bg 呼叫，依結果）、`_inflight`、`_add_quota_budget_used`。

### 健康檢查（管理埠 8081）

- 管理埠是另一個 listener，有自己的連線上限：代理埠被塞滿時 probe 照樣能答。每條連線的請求標頭必須在 5 秒內送完（uvicorn 本身沒有這個期限），不然直接關掉。
- `GET /healthz`：不碰上游（liveness）。
- `GET /readyz`：以 `198.18.0.5` 打上游 `/api/status`，並 `PING` 共用重放紀錄；兩者都通才 200（readiness：打通才 Ready）。上游回 429 算「打得通」。檢查**同一時間只跑一次**，結果成功時重用 5 秒、失敗時 1 秒，所以不管多少人打 `/readyz`，上游每 replica 最多每 5 秒（失敗時每秒）一次。另回 `upstream`、`replay_store`、`ap_days_left`、`op_days_left`（`PROXY_AP_EXPIRES_AT`／`PROXY_OP_EXPIRES_AT`，chart values `proxy.apExpiresAt`／`opExpiresAt`）、`break_glass`，不回任何金鑰值。PaaS 的健康檢查（LLM-02、LLM-14）讀 `http://newapi-proxy.<ns>.svc.cluster.local:8081/readyz`。
- 管理埠只收 GET、不得帶 query、只有這三個路徑（其他 404）。

## 設定（env）

| 變數 | 預設 | 說明 |
|---|---|---|
| `PROXY_UPSTREAM_URL` | `http://newapi:3000` | 只收 `http://<host>[:<port>]` |
| `PROXY_AUTH_WEB`、`PROXY_AUTH_CRON` | — | 簽章金鑰清單（見上），至少一組非空 |
| `PROXY_AP_TOKEN`、`PROXY_OP_TOKEN` | — | `nap_…`；沒設＝該身分回 503 `config` |
| `PROXY_USERNAME_REGEX` | 必填 | `^…$` 錨定，而且只能是 `^w<小寫字母>` 開頭、後面只有小寫字母／數字、`[a-z0-9-]` 這類簡單字元集、`\d` 與 `{n}`／`{n,m}`（不能有 `.`、`*`、`+`、`?`、`\|`、括號或其他跳脫）；prod `^wp\d{10}$`、stg `^ws[a-z]{2}\d{10}$`（`\d` 只算 ASCII） |
| `PROXY_TENANT_IP_RANGE` | `198.18.1.0-198.19.255.255` | 必須在這個範圍內 |
| `PROXY_ADD_QUOTA_HOURLY_CAP_POINTS` | `100000` | 全平台每小時加額度上限（點）；`0`＝完全不放行 add_quota |
| `PROXY_REPLICAS` | `1` | 上面的上限平均分給各 replica；**大於 1 時必須設 `PROXY_VALKEY_ADDR`**，不然代理不啟動 |
| `PROXY_VALKEY_ADDR` | 空 | `<host>:<port>`，共用重放紀錄；空＝只有本機記憶體（只允許單一 replica） |
| `PROXY_VALKEY_USER`、`PROXY_VALKEY_PASSWORD` | `newapi-proxy`、— | Valkey ACL 使用者；密碼 `[A-Za-z0-9_-]{16,256}`（chart 自動產生） |
| `PROXY_INFLIGHT_USER`／`_AP`／`_OP`／`_ANON`／`_BG` | 32／8／2／2／2 | 各身分 in-flight 上限 |
| `PROXY_BREAK_GLASS_UNTIL` | 空（關） | unix 秒，最多 24 小時後 |
| `PROXY_AP_EXPIRES_AT`、`PROXY_OP_EXPIRES_AT` | 空 | unix 秒或 `YYYY-MM-DD` |
| `PROXY_FREE_MODELS` | `qwen3-4b-aicoder,qwen3-4b-cluster1` | 免費試用金鑰可用的模型 |
| `PROXY_DEFAULT_READ_TIMEOUT` | `10` | 秒，1–30 |
| `PROXY_PORT`、`PROXY_ADMIN_PORT` | `8080`、`8081` | 代理埠、管理埠（不能相同） |
| `PROXY_MAX_CONNECTIONS` | `128` | 代理埠的 uvicorn `limit_concurrency`（連線＋進行中請求）；管理埠固定 32 |
| `PROXY_LOG_LEVEL` | `info` | |

記憶體估算（chart 的 limit 384Mi 依此）：基本約 50 MiB；讀 body 時每條連線最多約 0.5 MiB（256 KB 的 bytearray＋一份 bytes），128 條≈64 MiB；上游回應上限 1 MiB、組回應時最多兩份，所有身分 in-flight 合計 46 個≈92 MiB；合計約 210 MiB。

## 開發與測試

```bash
cd extra/newapi-proxy
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-dev.txt
.venv/bin/python -m pytest          # 與 CI .gitea/workflows/newapi-proxy-unit-tests.yml 同式
```

- 測試直接對 ASGI app 送手工組的 scope（`tests/proxy_harness.py`），`raw_path` 一個 byte 都不經過 HTTP client 正規化；上游是 `httpx.MockTransport`，不連網；共用重放紀錄用記憶體假物件（多個 harness 共用一個＝多個 replica）。`test_proxy_script_and_server.py` 另起真的 uvicorn（127.0.0.1）以 raw socket 驗證 `%2e%2e`、`..`、absolute-form 等請求目標原樣進到代理、標頭期限，最後一支直接跑 `python -m newapi_proxy`（兩個埠、SIGTERM）。`test_proxy_replaystore.py` 對一個照 chart ACL 行為的假 Valkey 驗證 RESP client。
- 依賴鎖檔含 hash：改 `requirements*.in` 後重產（`--exclude-newer` 設成兩週前，不用剛發布的版本）：

  ```bash
  uv pip compile --python-version 3.12 --generate-hashes --exclude-newer <兩週前 YYYY-MM-DD> requirements.in -o requirements.txt
  uv pip compile --python-version 3.12 --generate-hashes --exclude-newer <兩週前 YYYY-MM-DD> requirements-dev.in -o requirements-dev.txt
  ```

## 建置與部署

- `.gitea/workflows/build-newapi-proxy.yml`：develop／main 有改到本目錄時自動跑，也可在任何分支手動 `workflow_dispatch`。先跑單元測試，再 build 並**只推** `newapi-proxy:<40 位 commit sha>`，**絕不推 `:latest`**。tag 已存在就不重建、不覆寫；查 tag 時只有 registry 明確說「沒有這個 manifest」才 build，其他錯誤（驗證、網路、5xx）job 直接失敗，不會猜。job log 最後印出 `image@sha256:…`。
- **合併後會有新的 sha**：develop 一律用 merge commit，所以 feature branch 的 image（給 stg 驗收用）和合併後 develop 上建出來的 image 是兩個不同的 sha tag。stg 驗收可以先釘 feature branch 的 sha；**合併後 stg 與 prod 都改釘 develop 合併 commit 的 sha 與 digest**（部署的 image 一律對得到 develop 上的 commit）。
- 部署：把 tag 與 digest 填進 Woow_k3s_newapi 的 `deploy/woow-k3s-stg.yaml`（或 prod 的 `deploy/woow-k3s.yaml`）`proxy.image.tag`／`proxy.image.digest`，走該 repo 的 PR 與 helm upgrade。chart 拒絕全 0 的佔位 digest。
- Secret（值不進任何 repo、檔案或 log）：AP／OP 在 `newapi-proxy-pats`（key `ap`、`op`，NA-01／NA-05）；代理端簽章金鑰在 `newapi-proxy-hmac`（key `web`、`cron`，格式見上）；stg 的 Odoo 端複本在 `paas-stg-system/paas-stg-newapi-proxy-client`（key `web`、`cron`）；Valkey 密碼由 chart 產生在 `newapi-secrets`（key `PROXY_VALKEY_PASSWORD`）。一律 `kubectl create`／`replace` 從 stdin 寫入，不要 `kubectl apply`。建立與輪替指令在 Woow_k3s_newapi `docs/OPERATIONS.md`。

### stg 驗收工具

`scripts/signed_request.py` 只用標準函式庫，在只有 python3 的沙盒 Odoo pod 裡就能跑（金鑰從 stdin 或 env 讀，絕不放 argv）。

**前置**（一般沙盒做不到，要先開）：沙盒 chart 的 Odoo egress NetworkPolicy 擋掉 10/8、只放行 `allowEgressToNamespaces` 列的 namespace（stg 是 `[paas-stg-system]`，`.sandbox/pre-install.sh`），加入 `new-api-stg` 的 opt-in（`WOOW_LLM_SANDBOX=1`）在 LLM-01b。LLM-01b 合併前，在驗收用的沙盒 namespace 另加一條只給這次驗收的 NetworkPolicy（NetworkPolicy 是聯集，加一條就放行，刪掉就還原；不動沙盒 chart 的 values）：

```bash
NS=dev-sandbox-<slug>
kubectl -n "$NS" create -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: llm-01a-acceptance-newapi-proxy
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/component: odoo
  policyTypes: ["Ingress", "Egress"]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: new-api-stg
          podSelector:
            matchLabels:
              app.kubernetes.io/component: proxy
      ports:
        - { protocol: TCP, port: 8080 }
        - { protocol: TCP, port: 8081 }
  # 回包鏡射（k3s 內建 enforcer 會把部分回包當成新的連線）
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: new-api-stg
          podSelector:
            matchLabels:
              app.kubernetes.io/component: proxy
EOF
POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/component=odoo -o jsonpath='{.items[0].metadata.name}')
kubectl -n "$NS" cp scripts/signed_request.py "$POD":/tmp/signed_request.py
```

代理那一側不用改：stg 的 `proxy.clients` 已放行所有 `dev-sandbox.io/managed: "true"` namespace 的 Odoo pod。驗收完 `kubectl -n "$NS" delete networkpolicy llm-01a-acceptance-newapi-proxy` 並刪掉 `/tmp/signed_request.py`（或直接銷毀沙盒）。

**跑法**：金鑰從 Odoo 端 Secret 經 stdin 直接 pipe 進去（不落地、不進 argv）：

```bash
P=http://newapi-proxy.new-api-stg.svc.cluster.local:8080
key() { kubectl -n paas-stg-system get secret paas-stg-newapi-proxy-client -o jsonpath="{.data.$1}" | base64 -d; echo; }
run() { kubectl -n "$NS" exec -i "$POD" -- python3 /tmp/signed_request.py --key-stdin "$@"; }

key cron | run GET $P/api/status                                       # anon，200
key cron | run --identity ap GET "$P/api/user/search?keyword=wsab0000000001"
key cron | run --unsigned GET $P/api/status                            # 401
key cron | run --ts-offset -31 GET $P/api/status                       # 401（過期）
key cron | run --repeat 2 GET $P/api/status                            # 第二次 401（重放；兩個 replica 都擋）
key cron | run --repeat 2 --resign GET $P/api/status                   # 兩次都 200（每次新 nonce）
key cron | run --identity ap GET $P/api/option/                        # 403
key cron | run --extra-header 'X-Forwarded-For: 114.32.21.18' GET $P/api/status
kubectl -n new-api-stg exec deploy/newapi-valkey -- valkey-cli --scan --pattern 'rateLimit:v2:ip:GA:*'
#   ↑ 看得到 198.18.0.2，看不到 114.32.21.18：new-api 記到的是合成 IP（驗收 5）
kubectl -n "$NS" exec "$POD" -- python3 -c "import urllib.request as u; print(u.urlopen('http://newapi-proxy.new-api-stg.svc.cluster.local:8081/readyz').read())"
```

輸出的 body 會把 key／token／password 類欄位與 `nap_`／`sk-` 遮掉（`--show-body` 取消遮罩，終端機有被記錄時不要用）。`user` 身分的請求要帶 UP：用 `--auth-stdin` 讓它從 stdin 的第二行讀（順序：金鑰、UP、body），例如 `{ key web; cat <UP 檔>; } | run --auth-stdin --identity user --client-ip 198.18.17.9 GET $P/api/user/self`（UP 要等 LLM-01b 開戶後才有）。

## 已知限制

- 加額度每小時總量仍是各 replica 各算 1/replica（設計 §1.4.2-5 的近似）；in-flight 也是各 replica 各自的。
- 重放紀錄的兩層：Valkey 沒有持久化，Valkey 重啟後 62 秒內，只有「同一個 replica」的記憶體擋得住重放；Valkey 記憶體吃緊時可能淘汰帶 TTL 的 key（`volatile-lru`），重放紀錄每筆很小、壽命只有 62 秒，實務上不會先被淘汰。
- in-flight 上限的數值（user 32、ap 8、op／anon／bg 2）是起點；new-api 每個回應慢 7 秒時的實測數字記在 LLM-02 的 PR。
- 能讀 `paas-system` Secret 或 exec 進 Odoo pod 的人就握有簽章金鑰，可以在 body 政策範圍內使用 AP（§1.4.1 末、§5.3）；代理擋的是「政策以外」的事。
