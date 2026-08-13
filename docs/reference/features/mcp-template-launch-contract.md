# reference mcp template launch contract

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_mcp_template_launch_contract`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：MCP cloud template 的 config.mcp_server 啟動契約判別規則＋ivnvxd 套件命名衝突陷阱

平台 mcp-server chart 的 `config.nonSensitive.mcp_server.{command,args}` 是 admin 容器內要拉起的 MCP 子行程；mcp-admin-core `process.py:64` 直接 `exec [command]+args`、proxy 連 `mcp_server.port`（localhost HTTP，`proxy.py` 轉發 `/private_TOKEN/{path}`→`127.0.0.1:port/{path}`）。故**子行程必須在 port 上講 SSE/StreamableHTTP**。

**判別規則（決定 command）**：
- 底層 MCP server **stdio-only**（無 http transport）→ 經 core 的 `mcp_admin_core.mcp_sse_wrapper` 包：`command=python`、`args=[-m, mcp_admin_core.mcp_sse_wrapper, <module.path:server_obj>, --port, 8000]`。wrapper 需要一個 **module-level 低階 `Server` 物件**（`server:server`）。標準 `cloud_app_mcp_odoo` **首建時**（image `9d10944`、底層 `odoo-mcp-server 0.1.3` stdio-only）走這條，2026-07-14 重釘 image 後已改走下一條（#708）。
- 底層 server **原生 http**（有 `--transport`/自帶 port）→ 直跑其 console script。n8n（`command=n8n-mcp-http`、`args=[--port,3000]`）、**ivnvxd Manage（#338）**，以及**標準 `cloud_app_mcp_odoo`（image `7b5944c4` 起，#708）**走這條：`command=odoo-mcp`、`args=[--transport, streamable-http, --host, 127.0.0.1, --port, 8000]`。

**⚠ 重釘 image ＝ 必須重新套用上面這條判別規則（#708 的實際翻車）**：2026-07-14 commit `6fb93163` 把 odoo 的 image 從 `9d10944` 重釘到 `7b5944c4`（底層從 `odoo-mcp-server 0.1.3` 換成 tuanle96 `odoo-mcp`），diff 卻只有版號＋`image.tag` 兩行，同一個 JSON 裡的 `mcp_server.{command,args}` 沒跟著換 → wrapper 去 import 新 image 裡不存在的 `mcp_server_odoo`，**此後每一台新開的實例 `/mcp` 一律 502**。這個失敗沒有任何響亮訊號：pod `1/1 Ready`（探針只探 wrapper 自己的 `/healthz`）、admin GUI 綠、子行程 traceback 被 logging 設定吞掉。判別規則寫在這裡沒有用，因為重釘的人根本不會想到要來讀它——`src/tests/test_mcp_server_template_seed.py::TestMcpOdooTemplateSeed` 因此加了 `EXPECT_IMAGE_TAG` lockstep：image 一改測試就紅，把人帶回這條規則。

**#338 Manage 版真相（實查 image 打包源 woow-mcp-server #14）**：
- image 打包 **ivnvxd `mcp-server-odoo` 0.7.1**（FastMCP），正確啟動＝`command=mcp-server-odoo`、`args=[--transport, streamable-http, --host, 127.0.0.1, --port, 8000]`。
- ivnvxd 0.7.1 **無 module-level `server` 物件**（`server.py` 只有 `class OdooMCPServer`）→ wrapper spec `mcp_server_odoo.server:server` 對它會 ImportError。骨架 Wave A 猜的 `mcp_server_odoo_manage.server:server` 更是不存在。
- ⚠️ **命名衝突陷阱**：標準版底層 `odoo-mcp-server 0.1.3`（PyPI 名）與 Manage 底層 `mcp-server-odoo 0.7.1`（ivnvxd）**import 模組同為 `mcp_server_odoo`、但 API 完全不同**（前者有 `server:server`、後者 FastMCP 無）。**照抄標準版 launch spec 必壞**。動 MCP template 前先實查 image 的 `apps/<app>/pyproject.toml` dep + `uv.lock` 版本，別憑範本或記憶。
- proxy `proxy.py:55-69` 本就為 FastMCP 子行程的 TransportSecurityMiddleware 設計（改寫 Host→127.0.0.1、drop Origin）→ 原生 FastMCP 是既定路徑。
- 實測 de-risk（image 實際版本 ivnvxd 0.7.1 + mcp SDK 1.28.1）：`curl -X POST 127.0.0.1:8000/mcp`（proxy 送的裸路徑）→ **200 + mcp-session-id、無 307**。mcp 1.28.1 原生正確處理裸 `/mcp`，不需 `mcp_sse_wrapper` 繞的 307→`/mcp/` 洩漏。

**chart `app` 值**：只用於 `mcp.woowtech.io/app` label + `MCP_APP` env（image 源不消費）+ n8n-only secret 分支；selector/資源名皆 release-scoped（`app.kubernetes.io/instance={{.Release.Name}}`）→ **同 workspace 兩 CS 同 app 值不撞**。純 observability，可安全設變體名。

**XML 陷阱**：Odoo data 檔的**註解 `<!-- -->` 內不得含 `--`**（雙連字號，XML 規範禁止）。寫 `--transport`/`--port` 到註解會 not-well-formed；改 `transport=…、port=…`。`<field>` text 內的 `--` 合法（如 JSON args）。

相關：[[reference_mcp_admin_cs_image_repin]]（重釘 recipe）、[[project_smart_home_mcp_design_20260709]]（SH MCP）。
