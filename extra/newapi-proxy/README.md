# newapi-proxy

PaaS Odoo 呼叫 new-api 的**唯一**通道，也是 PaaS 出錯或被攻破時的那道牆（AI 模型分頁設計 §1.4，票 LLM-01a）。

- 程式碼在這裡（`extra/newapi-proxy/`），CI 建 image、只推不可變的 `<commit sha>` tag 到 JCR。
- 部署由 new-api 部署 repo（Gitea `woow-paas/Woow_k3s_newapi`）的 chart component `proxy` 以 **digest** 釘住：stg 在 `new-api-stg`、prod 在 `new-api`（N1）。AP／OP 兩把服務帳號 PAT 只放在 new-api chart 的 Secret；Odoo 只拿簽章金鑰。
- 不碰 Odoo、不碰資料庫；Python 3.12＋Starlette（只用 Request／Response）＋httpx＋uvicorn（h11）。

```
Odoo web／cron ──(HMAC 簽章＋X-Woow-Identity＋X-Woow-Client-IP)──► newapi-proxy:8080 ──(單一 XFF＝合成 IP；AP／OP 由代理注入)──► newapi:3000
```

## 線上契約（給 `src/services/newapi_client.py`，LLM-01b）

### 請求標頭

| 標頭 | 內容 |
|---|---|
| `X-Woow-Proxy-Kid` | 金鑰 id（`[A-Za-z0-9_-]{1,64}`） |
| `X-Woow-Proxy-Ts` | unix 秒；代理只接受 \|now − ts\| ≤ 30 秒 |
| `X-Woow-Proxy-Sig` | 小寫 hex 的 HMAC-SHA256（見下） |
| `X-Woow-Identity` | `ap`｜`op`｜`user`｜`anon`｜`bg` |
| `X-Woow-Client-IP` | 只有 `user` 必帶：租戶合成 IP；其他身分**不得**帶 |
| `Authorization` | 只有 `user` 帶（`Bearer <UP 或開戶 JWT>`；`/api/user/login` 例外不帶）；其他身分帶了一律 400 |
| `X-Security-Proof` | 只有 `POST /api/user/access_tokens` 帶（必帶）；其他路由帶了一律 400 |
| `X-Woow-Read-Timeout` | 選填，1–30 整數秒；PaaS 設成自己的 read timeout 減 1 秒。不帶＝`PROXY_DEFAULT_READ_TIMEOUT`（預設 10） |

同一個標頭出現兩次＝400。其餘進來的標頭（`X-Forwarded-For`、`X-Real-IP`、`Forwarded`、`X-Forwarded-*`、`Cookie`、`Origin`、其他 `X-Woow-*`…）**全部不轉送**：代理對上游只送自己組的標頭。

### 簽章

```
string_to_sign = method "\n" canonical_path "\n" canonical_query "\n"
                 hex(sha256(body)) "\n" ts "\n" identity "\n" client_ip
sig            = hex(HMAC-SHA256(secret 的 ASCII bytes, string_to_sign))
```

- `canonical_path`：原樣的路徑。代理只收已經是 canonical 的路徑（見〈canonical 化〉），所以就是送出去的那串。
- `canonical_query`：query 依 key 排序、`k=v` 以 `&` 連接、**不做 percent-encoding**（允許的字元本來就不需要）；沒有 query 就是空字串。建議 PaaS 直接照這個順序送。
- `body`：送出去的 bytes（GET／DELETE 是空的）。
- `client_ip`：`user` 是 `X-Woow-Client-IP` 的值，其他身分是空字串。
- 同一個 Sig 在 60 秒內只接受一次（每個 replica 各自的記憶體快取；重啟歸零，已知近似）。
- 參考實作：`newapi_proxy/signing.py`（`signed_headers()`）；只用標準函式庫的同款在 `scripts/signed_request.py`，`tests/test_proxy_script_and_server.py` 讓兩者保持一致。

### 金鑰（N23）

env 格式 `kid:secret[,kid:secret]`：第一把是現用、後面是下一把（輪替不停機，代理接受清單內任一把）。kid `[A-Za-z0-9_-]{1,64}`、secret `[A-Za-z0-9_-]{32,256}`；值會先 strip。格式不合、kid 重複、同一個 kid 或 secret 同時出現在兩組 → 代理不啟動（exit 78），錯誤訊息只寫變數名與問題，不帶值。

| 組 | Odoo 端 env | 能做什麼 |
|---|---|---|
| `WEB` | `WOOW_NEWAPI_PROXY_AUTH_WEB`（web deployment） | 只能用 `user` 身分的互動路由（下表 WEB 欄 ✓）；不能 login、verify、access_tokens、sessions，不能用 ap／op／anon／bg |
| `CRON` | `WOOW_NEWAPI_PROXY_AUTH_CRON`（cron deployment） | 全部身分與路由；`bg` 只有 CRON 能用 |

代理端對應 `PROXY_AUTH_WEB`、`PROXY_AUTH_CRON`（chart 從 Secret 帶入）。

### 身分與合成 IP（§1.4.2-2、-4）

| 身分 | 上游 `Authorization` | `X-Forwarded-For` |
|---|---|---|
| `ap` | 代理注入 AP（`PROXY_AP_TOKEN`） | `198.18.0.1` |
| `anon` | 無 | `198.18.0.2` |
| `op` | 代理注入 OP（`PROXY_OP_TOKEN`） | `198.18.0.3` |
| `bg` | 代理注入 AP | `198.18.0.4`（本票新增，讓 break-glass 在 new-api 稽核裡一眼可辨） |
| `user` | 轉送 PaaS 帶的 UP／JWT（帶的若是 AP 或 OP 本身 → 403） | `X-Woow-Client-IP`，必須在 `PROXY_TENANT_IP_RANGE` 內、點分十進位無前導零 |

`PROXY_TENANT_IP_RANGE`：prod `198.18.1.0-198.19.255.255`（block 0）；stg `198.18.17.0-198.19.255.255`（只收 block 1–31，`198.18.0.0 + 256 + block×4096 + id`）。

### canonical 化（〔REV〕17）

看的是**未解碼**的 `raw_path` 與 query string：

- 路徑含 `%`、`\`、`;`、`..`、`//`、`.` 段、空白／控制字元、非 ASCII，或不是 `/[A-Za-z0-9/_.-]*` → 400。
- 路徑參數：`{id}` 是 `1`–`2147483647`、不得有前導零；`{sid}` 是 `[A-Za-z0-9_-]{1,128}`。對不上樣板 → 403。
- 轉送的 URL 一律由樣板＋驗證過的參數**重組**，不轉送 client 原始路徑。
- query：只收 `[A-Za-z0-9_.-=&:/]`（沒有 `%`、`+`、`;`）、每段剛好一個 `=`、值不能空、key 不能重複；每條路由宣告允許的 key 與值格式，多一個 key（例如 new-api 的別名 `ps`）就 400。

### 路由白名單（本票：tracer 用得到的；其餘 LLM-02 補）

| 身分 | 方法與路徑 | WEB | query | body 政策 |
|---|---|---|---|---|
| anon | `GET /api/status` | | | |
| op | `GET /api/option/` | | | |
| ap | `POST /api/user/` | | | 建使用者 |
| ap | `GET /api/user/search` | | `keyword`（必須符合 `PROXY_USERNAME_REGEX`）、`p`、`page_size` | |
| ap | `GET /api/user/{id}` | | | |
| ap | `POST /api/user/manage` | | | add_quota（含目標預檢與每小時總量） |
| user | `POST /api/user/login` | | | 登入（不帶 Authorization） |
| user | `POST /api/verify` | | | verify |
| user | `GET /api/user/access_tokens` | | | |
| user | `POST /api/user/access_tokens` | | | 建 UP（必帶 proof） |
| user | `DELETE /api/user/sessions/{sid}` | | | |
| user | `GET /api/user/self` | ✓ | | |
| user | `POST /api/token/` | ✓ | | 建金鑰 |
| user | `GET /api/token/` | ✓ | `p`、`page_size` | |
| user | `GET /api/token/{id}` | ✓ | | |
| user | `POST /api/token/{id}/key` | ✓ | | reveal 預檢（無 body） |
| user | `GET /api/log/self` | ✓ | `p`、`page_size`、`type`、`start_timestamp`、`end_timestamp`、`model_name`、`token_name`、`group`、`request_id` | |

其他一律 403＋`X-Woow-Proxy-Error: policy`，特別是 `/v1/*`、`/pg/*`、`/api/option/*`（op 的 GET 除外）、`/api/channel/*`、`/api/redemption/*`、`/api/user/topup*`、`/api/user/auth/*`、`PUT /api/user/`、`POST /api/token/batch/keys`、`GET /api/log/self/export`。

`bg`（break-glass，〔REV〕1）：框架已在，**預設關**（`PROXY_BREAK_GLASS_UNTIL` 未設）。只有 CRON 金鑰、而且 now < `PROXY_BREAK_GLASS_UNTIL`（最多 24 小時後；設得更遠視同關閉並告警）才放行；每次使用都寫稽核 log 並累加 metric。本票沒有任何 `bg` 路由，`PUT /api/user/` 改分組與 `manage` subtract 在 LLM-02 加。

### body 政策（§1.4.2-5、§3.3）

所有 JSON body：UTF-8、無 BOM、無重複 key、無 NaN／Infinity、最外層是 object、≤ 256 KB（超過 413）。驗過的欄位**重新序列化**後才送出；new-api 解 JSON 不分大小寫又忽略未知欄位，所以規則一律是「剛好這些欄位、剛好這些型別」（`true` 不算 `1`、`1.0` 不算整數）。違反 → 403 `policy`。

| 路由 | 只接受 |
|---|---|
| `POST /api/user/` | `{"username": 符合 PROXY_USERNAME_REGEX, "password": 32–128 個可見 ASCII, "display_name": "PaaS <數字>", "role": 1}` |
| `POST /api/user/manage` | `{"id": >0, "action": "add_quota", "mode": "add", "value": 1–156,250,000}`；送出前代理以 AP `GET /api/user/{id}` 確認 `username` 符合前綴、`role==1`、`group=="default"`；再加每小時總量上限（`PROXY_ADD_QUOTA_HOURLY_CAP_POINTS`÷`PROXY_REPLICAS`，滑動一小時，超過 429 `budget`）。確定沒送出（預檢失敗、connect）才把額度還回去；結果未知（reset／timeout）照算 |
| `POST /api/user/login` | `{"username": 符合前綴, "password": 32–128}` |
| `POST /api/verify` | `{"method": "password", "scope": "access_token.generate"｜"access_token.revoke", "context": …, "password": …}`；generate 的 `scopes` ⊆ UP scope、`expires_at` 在 now+1 小時 1 分～now+92 天；revoke 的 context 只有 `token_id` |
| `POST /api/user/access_tokens` | `{"name": "woow-paas-up-<id>-<YYYYMMDDHHMM>", "scopes": ⊆ UP scope, "expires_at": 同上}` |
| `POST /api/token/` | §3.3 的 10 個欄位一個不少；`name` 是 `wp_<數字>_[a-z0-9]{4}`；`group` ∈ {default, free}；`allow_ips` 是空字串；`cross_group_retry`、`allow_user_quota_query` 是 false；`group=free` 時 `model_limits_enabled=true`、`model_limits` ⊆ `PROXY_FREE_MODELS`、`unlimited_quota=false`、`remain_quota=1`（保險絲） |
| `POST /api/token/{id}/key` | 無 body。代理先用**同一把 UP、同一個合成 IP** `GET /api/token/{id}`：`name` 符合 `wp_<數字>_…`、`created_time` 在 120 秒內（容許 new-api 時鐘快 5 秒）才轉送，否則 403（〔REV〕2） |

UP scope＝`api_key:read`、`api_key:reveal`、`api_key:write`、`profile:read`、`usage:read`。

### 回應與錯誤（§1.4.2-6、-7）

上游的狀態碼與 body 原樣回傳，標頭只留 `Content-Type`、`X-Oneapi-Request-Id`、`Retry-After`（`Set-Cookie` 等一律丟掉），另加 `Cache-Control: no-store`。代理自己產生的錯誤一定帶 `X-Woow-Proxy-Error`，body 是 `{"success":false,"message":"newapi-proxy: <class>","proxy_error":"<class>","reason":"<code>"}`（401 的 reason 一律只寫 `auth`，細節只進 log）。

| `X-Woow-Proxy-Error` | HTTP | 什麼時候 | client 分類建議（§3.4） |
|---|---|---|---|
| `bad_request` | 400 | 格式不合（路徑、query、標頭、JSON、client IP） | Definite（PaaS bug） |
| `too_large` | 413 | body > 256 KB | Definite（PaaS bug） |
| `auth` | 401 | 缺簽章、kid 不認得、ts 超過 ±30 秒、簽章錯、重放 | Definite（PaaS bug／設定錯），circuit 開 |
| `policy` | 403 | 白名單外、WEB 越權、body 政策、reveal 超過 120 秒、manage 目標不符、bg 未開 | Definite（PaaS bug），circuit 開 |
| `budget` | 429 | 加額度每小時總量用完 | NotExecuted＋告警 |
| `busy` | 503 | 該身分 in-flight 滿了（`PROXY_INFLIGHT_*`） | NotExecuted |
| `config` | 503 | 該身分需要的 PAT 沒設定 | NotExecuted＋告警 |
| `connect` | 502 | 沒連上 new-api；或 reveal／manage 的預檢失敗（受保護的那個請求**沒送出**），預檢遇到 429 會帶回 `Retry-After` | NotExecuted（可重送） |
| `reset` | 502 | 送出後斷線、回應太大 | Unknown（寫入不重送） |
| `timeout` | 504 | 讀取逾時 | Unknown（寫入不重送） |
| `internal` | 500 | 代理本身的未預期例外 | Unknown |

代理**不重試**（httpx `retries=0`、不跟 redirect、不讀 proxy env）；寫入用一次性連線（`Connection: close`），不會因為連線池裡過期的 socket 把寫入變成「結果未知」。httpx／h11 的例外只轉成分類，例外文字不進 log 也不進回應。

### log、稽核、metric（§1.4.2-8）

- stdout 一行一個 JSON。access log 只有 method、路由名稱（樣板，不含 id）、status、上游 status、耗時、`X-Oneapi-Request-Id`、合成 IP、身分、kid、錯誤分類；**不記任何標頭值與 body**。uvicorn 自己的 access log 關掉。
- 稽核 log（`"event":"audit"`）：每次 reveal（`token_id`）、每次 add_quota（`target_user_id`、`value`）、每次 bg，都記 kid、合成 IP、結果（forwarded／denied／not_sent／reset／timeout）與 request id。這份紀錄 Odoo 改不到，對帳時拿來和 `llm_event` 比（§4.4 I3、LLM-14）。
- 第二道防線：每一行輸出前都經過遮罩（`nap_…`、`sk-…`、`Bearer …`、JWT、64 位 hex、以及設定裡的每一把金鑰與 PAT 原文）；例外只記型別名稱。
- `/metrics`（Prometheus 文字格式）：`newapi_proxy_requests_total`、`_rejections_total`、`_upstream_errors_total`、`_reveal_total`、`_add_quota_total`、`_add_quota_value_total`、`_break_glass_total`、`_inflight`、`_add_quota_budget_used`。

### 健康檢查

- `GET /healthz`：不碰上游（liveness）。
- `GET /readyz`：以 anon（`198.18.0.2`）打上游 `/api/status`，`success:true` 才 200（readiness：上游打通才 Ready）；另回 `ap_days_left`、`op_days_left`（`PROXY_AP_EXPIRES_AT`／`PROXY_OP_EXPIRES_AT`，chart values `proxy.apExpiresAt`／`opExpiresAt`）、`break_glass`，不回任何金鑰值。
- 這三個路徑只認原樣的 `raw_path`、只收 GET、不得帶 query。

## 設定（env）

| 變數 | 預設 | 說明 |
|---|---|---|
| `PROXY_UPSTREAM_URL` | `http://newapi:3000` | 只收 `http://<host>[:<port>]` |
| `PROXY_AUTH_WEB`、`PROXY_AUTH_CRON` | — | 簽章金鑰清單（見上），至少一組非空 |
| `PROXY_AP_TOKEN`、`PROXY_OP_TOKEN` | — | `nap_…`；沒設＝該身分回 503 `config` |
| `PROXY_USERNAME_REGEX` | 必填 | `^…$` 錨定；prod `^wp\d{10}$`、stg `^ws[a-z]{2}\d{10}$`（`\d` 只算 ASCII） |
| `PROXY_TENANT_IP_RANGE` | `198.18.1.0-198.19.255.255` | 必須在這個範圍內 |
| `PROXY_ADD_QUOTA_HOURLY_CAP_POINTS` | `100000` | 全平台每小時加額度上限（點）；`0`＝完全不放行 add_quota |
| `PROXY_REPLICAS` | `1` | 上面的上限平均分給各 replica |
| `PROXY_INFLIGHT_USER`／`_AP`／`_OP`／`_ANON`／`_BG` | 32／8／2／2／2 | 各身分 in-flight 上限 |
| `PROXY_BREAK_GLASS_UNTIL` | 空（關） | unix 秒，最多 24 小時後 |
| `PROXY_AP_EXPIRES_AT`、`PROXY_OP_EXPIRES_AT` | 空 | unix 秒或 `YYYY-MM-DD` |
| `PROXY_FREE_MODELS` | `qwen3-4b-aicoder,qwen3-4b-cluster1` | 免費試用金鑰可用的模型 |
| `PROXY_DEFAULT_READ_TIMEOUT` | `10` | 秒，1–30 |
| `PROXY_PORT`、`PROXY_LOG_LEVEL` | `8080`、`info` | |

## 開發與測試

```bash
cd extra/newapi-proxy
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-dev.txt
.venv/bin/python -m pytest          # 與 CI .gitea/workflows/newapi-proxy-unit-tests.yml 同式
```

- 測試直接對 ASGI app 送手工組的 scope（`tests/proxy_harness.py`），`raw_path` 一個 byte 都不經過 HTTP client 正規化；上游是 `httpx.MockTransport`，不連網。`test_proxy_script_and_server.py` 另起真的 uvicorn（127.0.0.1）以 raw socket 驗證 `%2e%2e`、`..`、absolute-form 等請求目標原樣進到代理。
- 依賴鎖檔含 hash：改 `requirements*.in` 後重產（`--exclude-newer` 設成兩週前，不用剛發布的版本）：

  ```bash
  uv pip compile --python-version 3.12 --generate-hashes --exclude-newer <兩週前 YYYY-MM-DD> requirements.in -o requirements.txt
  uv pip compile --python-version 3.12 --generate-hashes --exclude-newer <兩週前 YYYY-MM-DD> requirements-dev.in -o requirements-dev.txt
  ```

## 建置與部署

- `.gitea/workflows/build-newapi-proxy.yml`：develop／main 有改到本目錄時自動跑，也可在任何分支手動 `workflow_dispatch`。先跑單元測試，再 build 並**只推** `newapi-proxy:<40 位 commit sha>`，**絕不推 `:latest`**；tag 已存在就不覆寫。job log 最後印出 `image@sha256:…`。
- 部署：把 digest 填進 Woow_k3s_newapi 的 `deploy/woow-k3s-stg.yaml`（或 prod 的 `deploy/woow-k3s.yaml`）`proxy.image.digest`，走該 repo 的 PR 與 helm upgrade。
- Secret（值不進任何 repo、檔案或 log）：AP／OP 在 `newapi-proxy-pats`（key `ap`、`op`，NA-01／NA-05）；代理端簽章金鑰在 `newapi-proxy-hmac`（key `web`、`cron`，格式見上）；stg 的 Odoo 端複本在 `paas-stg-system/paas-stg-newapi-proxy-client`（key `web`、`cron`）。一律 `kubectl create`／`replace` 從 stdin 寫入，不要 `kubectl apply`。

### stg 驗收工具

`scripts/signed_request.py` 只用標準函式庫，複製進沙盒 Odoo pod 就能跑（金鑰從 env 讀，絕不放 argv）：

```bash
P=http://newapi-proxy.new-api-stg.svc.cluster.local:8080
python3 signed_request.py GET $P/api/status                                  # anon，200
python3 signed_request.py --identity ap GET "$P/api/user/search?keyword=wsab0000000001"
python3 signed_request.py --unsigned GET $P/api/status                       # 401
python3 signed_request.py --ts-offset -31 GET $P/api/status                  # 401（過期）
python3 signed_request.py --repeat 2 GET $P/api/status                       # 第二次 401（重放）
python3 signed_request.py --identity ap GET $P/api/option/                   # 403
python3 signed_request.py --identity user --client-ip 198.18.17.9 --auth-env UP \
  --extra-header 'X-Forwarded-For: 114.32.21.18' GET $P/api/user/self        # new-api 記到的是 198.18.17.9
```

輸出的 body 會把 key／token／password 類欄位與 `nap_`／`sk-` 遮掉（`--show-body` 取消遮罩，終端機有被記錄時不要用）。

## 已知限制

- 重放快取、in-flight、加額度總量都在各 replica 的記憶體：重放只擋同一個 replica；重啟歸零（設計 §1.4.2 已知近似）。
- 本票只有 tracer 需要的路由與 `bg` 的框架；`op`／`bg` 完整用例、其餘路由、in-flight 調校在 LLM-02。
- 能讀 `paas-system` Secret 或 exec 進 Odoo pod 的人就握有簽章金鑰，可以在 body 政策範圍內使用 AP（§1.4.1 末、§5.3）；代理擋的是「政策以外」的事。
