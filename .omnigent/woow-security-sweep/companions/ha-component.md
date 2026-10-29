# Home Assistant custom component hunting

woow-security-sweep 自己的 companion，格式比照 `third_party/cloudflare-security-audit/` 的 companion 檔：
`ticket.py` 依 `targets/<repo>.yaml`（或 stack profile）的 `review_classes` 原文擷取 `Core discipline`、
指定類別、`Universal moves`、`Validation rules` 放進工單。類別名稱（粗體）是引用鍵，⛔ 改名要同步改
`stacks/ha-component.yaml`、targets 與 `tests/test_ticket.py`。

#### When to use this file

目標是 Home Assistant custom integration（`manifest.json`、`config_flow.py`、`async_setup_entry`、
`HomeAssistantView`、`websocket_api`、`hass.services`）。和 `ATTACK-CLASSES.md` 一起用：那邊問「這個主體能不能做這件事」，
這裡補 HA 讓權限判斷失準的固定位置。

## Core discipline (include in every agent prompt for this domain)

```
- HA 的主體要寫實名：未登入（經 tunnel／Nabu Casa／LAN 可達的任何人）、一般使用者（non-admin）、admin、owner、本機其他 process。admin 在 HA 上幾乎等於 root：admin 才做得到的事通常不是 finding。
- 每個入口寫出它的驗證方式：HomeAssistantView.requires_auth、websocket_api.require_admin、async_register_admin_service、config flow（只有 admin 能開）、webhook（webhook id 本身就是憑證）。
- 平台或雲端 API 的回應是不可信資料：它決定要不要啟動子行程、下載什麼、安裝什麼程式碼時，寫出驗證那份資料的機制（簽章、pin 的 sha256、白名單）。
- 秘密值放在哪裡要寫清楚：config entry data（.storage 明文，等同 admin 可讀）、entity state／attribute（所有已登入使用者可讀，且進 recorder 歷史）、log、diagnostics、process argv。
- 決定性事實在 HA core 行為或部署方式（是否開 tunnel、Nabu Casa、反代）時列 needs_validation，寫明要驗證的確切 HA 版本行為或部署設定。
```

## Entry surface attack classes (subagent_type: `general`)

**Unauthenticated HTTP view**
`requires_auth = False` 的 `HomeAssistantView`（OAuth callback、webhook 型 view、靜態頁）任何人都打得到：檢查參數重複、state 驗證是否綁定 flow、回應是否反射輸入、是否觸發寫入或外呼、錯誤訊息洩漏。比對同一效果的其他入口。

**Non-admin websocket and service surface**
`websocket_api.async_register_command` 沒有 `require_admin`、`hass.services.async_register`（不是 admin service）註冊的 service，所有已登入使用者（含 non-admin）與 automation 都能呼叫。寫出 non-admin 呼叫後能讀到什麼、能停掉或改變什麼。

## Configuration and identity attack classes (subagent_type: `general`)

**Config flow and outbound URL trust**
config flow、options flow、reauth 收到的 host／URL／token，以及平台回應裡的 URL，會不會讓 HA 對內網或任意主機發請求（SSRF）、關掉 TLS 驗證（`verify_ssl=False`）、把憑證送到錯的主機；redirect_uri 是否取自可被偽造的 header。

**Token and credential lifecycle**
OAuth access／refresh token、平台發的 tunnel token、HA long-lived token 的取得、儲存、更新、撤銷：移除 integration 時是否真的撤銷、refresh 失敗時是否退回較弱的模式、同一份憑證能不能被兩個 instance 共用。

## Disclosure attack classes (subagent_type: `general`)

**Diagnostics, logs, and events as readers**
`async_get_config_entry_diagnostics` 沒用 `async_redact_data` 遮蔽 token；`_LOGGER` 或子行程 stderr 寫入 token／user code；`hass.bus.async_fire` 的事件內容、entity state／attribute 帶秘密 URL（non-admin 可讀、recorder 永久保存）。

**Files under www and config paths**
寫進 `config/www`（以 `/local/` 不經驗證公開）、`custom_components`（等於安裝可執行程式碼）、`.storage` 或其他 config 路徑的檔案：路徑是否由外部資料組成、是否檢查 `..`／symlink、下載的壓縮檔解開時是否擋路徑逃逸。

## Execution attack classes (subagent_type: `general`)

**Subprocess and downloaded binaries**
`asyncio.create_subprocess_exec`／`subprocess` 的 argv 是否含平台給的值、秘密是否放在 argv（本機 `/proc/<pid>/cmdline` 可讀）；下載的執行檔或 zip 是否釘版本與 checksum、是否只以「檔案存在」判斷已安裝、更新時是否原子替換。

## Universal moves (apply across the above)

- 列出每個入口的「誰能觸發 → 需要什麼憑證 → 做了什麼副作用」，同一副作用的所有入口並排比對，最弱的就是實際政策。
- 對每份秘密值畫出「取得 → 儲存 → 傳遞 → 顯示／記錄 → 撤銷」，任何一段落到 non-admin 可讀的地方就往下追。
- 平台回應驅動的動作（啟動 tunnel、自動安裝、重設連線）要寫出平台被冒充或回傳惡意值時會發生什麼。
- 讀到 `requires_auth = False`、`verify_ssl=False`、`releases/latest`、`extractall`、`async_register(` 時一定停下來追完。

## Validation rules (apply before reporting ANY finding here)

1. 寫出 HA 主體與它取得的具體能力；admin 本來就能做的事（改設定、裝 integration、讀 .storage）不是 finding。
2. non-admin 曝露要寫出：入口沒有 admin 檢查的那一行，以及 non-admin 呼叫後可觀察的結果（讀到的秘密、中斷的服務、改變的狀態）。
3. 未驗證入口要寫出：從網路可達的前提（tunnel／Nabu Casa／LAN），前提不在原始碼時列 needs_validation。
4. 下載或安裝的供應鏈問題要寫出：誰能控制來源、缺少的驗證（版本 pin、checksum、簽章），以及執行的身分。
5. HA core 的行為（auth middleware、websocket 權限、recorder、`_decode_jwt`）不在快照裡；決定性時寫明要驗證的 HA 版本與行為，不要憑記憶定案。
