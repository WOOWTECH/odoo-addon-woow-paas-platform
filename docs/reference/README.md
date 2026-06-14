# reference/ — 查找導向

「**是什麼 / 有什麼**」的事實規格。API contract、feature spec、architecture diagram、設計檔（.pen）都歸這。

## 進入條件

- 描述「是什麼 / 有什麼」、需**被多份文件引用**
- 對象是要查找事實或對照規格的人

## 退出條件

- spec 被取代且**新檔已寫好** → `archived/`
- 引用方都已切到新 spec 後才能 archive

## 子目錄

| 路徑 | 內容 |
|------|------|
| [`api/`](api/) | API contract：[`woowtech-paas-api-specification`](api/woowtech-paas-api-specification.md)（整站串接規格）、[`agent-runtime-ws-protocol`](api/agent-runtime-ws-protocol.md)（Agent-Runtime Pod WebSocket 協定）、[`ha-component-integration`](api/ha-component-integration.md)（HA component 串接總覽：概念 / OAuth / Smart Home API，SA 速覽）、[`security-access-ha-integration`](api/security-access-ha-integration.md)（Security Access HA 端點權威規格） |
| [`features/`](features/) | 功能規格（workspace、cloud-services、subscription-integration） |
| [`architecture/`](architecture/) | 架構生命週期圖（目前：[`n8n-cloud-service-lifecycle`](architecture/n8n-cloud-service-lifecycle.md)） |
| [`pencil/`](pencil/) | `.pen` 設計檔（圖表） |

## 命名

- `kebab-case`，禁空格與底線
- features/ 內檔名去掉 `-feature-spec` 後綴（如 `workspace.md` 而非 `workspace-feature-spec.md`）
- 版本號用 frontmatter 維護，不入檔名

## 寫作慣例

- 描述「事實」，不論述「為什麼」（why 屬於 [`../decisions/`](../decisions/)）
- 含實作狀態註記時，用明確 frontmatter 欄位（如 `implementation_status: phase1-complete`）
- API 路徑 / endpoint 用 code block 框住，方便 grep
- 改 spec 要同步更新引用方 — CI link check 會抓 broken link
