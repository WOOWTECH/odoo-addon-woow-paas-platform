# reference hermes agent web dist patch constraints

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_hermes_agent_web_dist_patch_constraints`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：改 hermes-agent 前端 bundle 的三個硬約束：/assets 路徑寫死、chunk 環狀引用、assets 無 Cache-Control

要對 `nousresearch/hermes-agent` 的 `web_dist` 動刀（patch 編譯後 bundle）時，
這三點決定了「改名 cache-bust 只能怎麼做」——2026-07-27 於 image
`@sha256:a2a0a2da…` 實測：

1. **`/assets` 路徑不能改**。`web_server.py` 有
   `application.mount("/assets", StaticFiles(directory=WEB_DIST/"assets"))`、
   一條 `@application.get("/assets/{filename}.css")`，而且
   `dashboard_auth/middleware.py` 的 auth bootstrap 白名單也是 `/assets/` 前綴。
   改目錄名 → 404 或被擋在認證外。
2. **chunk 之間是環狀引用**。lazy chunk 反過來 `import "./index-<hash>.js"`
   （約 20 個檔都引用進入點）。只改名單一檔案會讓引用它的檔內容變動 → 那個檔
   又得改名 → 級聯無法收斂。可行解＝**所有 `.js` 一起換名**；chunk 間走相對
   路徑，改名後引用一次改寫到位。`.css`/`.woff2` 內容未變可不動。
3. **`assets/*` 不帶 `Cache-Control`**（Starlette StaticFiles 預設），走啟發式
   快取；`index.html` 則是 `no-store, no-cache, must-revalidate`。所以
   index.html 永遠新鮮，是 cache-bust 唯一可靠的施力點。

另外兩個實用事實：
- dashboard 預設 `0.0.0.0:9119`，但**非 loopback 綁定強制要求 auth provider**
  （`HERMES_DASHBOARD_BASIC_AUTH_USERNAME`+`_PASSWORD` 或
  `HERMES_DASHBOARD_OAUTH_CLIENT_ID`），否則 `start_server` fail-closed；
  `HERMES_DASHBOARD_INSECURE` 自 2026-06 hardening 起已無效。
  bundled basic provider 走**表單+cookie 非 HTTP Basic**，`curl -u` 只會拿到
  302 → `/login`。
- 容器預設 CMD 是互動 TUI，`docker run -d` 無 TTY 會自己 `Goodbye!` 退出
  （exit 0）。要留活著測 dashboard：`docker run -d … <image> sleep infinity`
  （wrapper 會把可執行的第一個 arg 直接 exec，s6 仍照常供養 dashboard service）。

用途脈絡見 [[project_424_item2_hermes_agent_image]]。
