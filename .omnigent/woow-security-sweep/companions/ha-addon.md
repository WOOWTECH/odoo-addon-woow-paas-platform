# Home Assistant add-on hunting

woow-security-sweep 自己的 companion，格式比照 `third_party/cloudflare-security-audit/` 的 companion 檔：
`ticket.py` 依 `targets/<repo>.yaml`（或 stack profile）的 `review_classes` 原文擷取 `Core discipline`、
指定類別、`Universal moves`、`Validation rules` 放進工單。類別名稱（粗體）是引用鍵，⛔ 改名要同步改
`stacks/ha-addon.yaml`、targets 與 `tests/test_ticket.py`。

#### When to use this file

目標是 Home Assistant add-on repository（`<addon>/config.yaml`、`Dockerfile`、`run.sh`／s6 service、ingress web UI、
對外 port 的服務）。和 `ATTACK-CLASSES.md` 一起用；add-on 裡的 MCP server 另外搭配 `AI-AND-LLM.md` 的 tool 類別，
建置與發佈流程搭配 `SUPPLY-CHAIN-AND-RELEASE.md`。

## Core discipline (include in every agent prompt for this domain)

```
- 先讀 add-on 的 config.yaml：privileged、full_access、host_network、host_pid、apparmor、hassio_api、hassio_role、homeassistant_api、auth_api、docker_api、devices、map、ingress、ports、panel_admin。每個 finding 都要寫出它借用了哪一項權限。
- 入口分三種寫實名：經 ingress（Supervisor 代理、HA 已驗證，來源 172.30.32.2）、host／LAN 上的 port（不經 HA 驗證）、容器內其他 process。同一個服務同時有 ingress 又開 port，兩邊要分開審。
- SUPERVISOR_TOKEN、HA token、後端服務憑證的權限範圍要寫清楚（hassio_role 與 homeassistant_api 決定能對 Supervisor／Core 做什麼）。
- add-on 的 options 由 HA admin 設定；只有在值會跨到較低信任的入口（例如 port 上的未驗證請求、MCP client）時，才算越界。
- 決定性事實在 Supervisor 行為或部署設定（使用者有沒有映射 port、反代）時列 needs_validation，寫明要觀察的設定。
```

## Container authority attack classes (subagent_type: `general`)

**Add-on privilege surface**
config.yaml 要的權限比功能需要的多：`full_access`、`privileged` capability、`host_network`、`docker_api`、`hassio_role: manager／admin`、`map` 的 rw 目錄、`devices`。寫出攻破 add-on 內服務的人會因此多拿到什麼（host、其他 add-on、Supervisor 管理權）。

**Supervisor and Home Assistant token handling**
`SUPERVISOR_TOKEN` 或 Core token 是否被寫進 log、回應、前端、子行程環境或可被 port 上的請求讀到；呼叫 Supervisor／Core API 的參數是否來自不可信輸入（例如由 MCP tool 引數決定要呼叫哪個 service）。

## Network entry attack classes (subagent_type: `general`)

**Ingress and exposed ports**
ingress 服務是否只接受 172.30.32.2 的連線、是否信任可被偽造的 `X-Remote-User-*`／`X-Ingress-Path`；`ports` 開出去的服務（MCP endpoint、API）用什麼驗證、token 是否放在 URL、有沒有 Origin／Host 檢查與 CSRF 防護。

## Execution attack classes (subagent_type: `general`)

**Options to shell and templates**
`/data/options.json`、`bashio::config`、環境變數、使用者上傳的設定被插進 shell 指令、設定檔模板、`subprocess` 引數或 URL：是否加引號與驗證、能不能注入額外參數或換行。

**Container build inputs**
Dockerfile 的 base image 是否以 digest 釘版、`ADD`／`curl | sh` 抓遠端內容、build 時安裝未釘版本或未驗 hash 的套件、build context 帶進秘密、最後以 root 執行；`build.yaml`／CI 發佈 image 時是否綁定審過的 commit。

## Universal moves (apply across the above)

- 畫出每個服務的「入口（ingress／port／內部）→ 驗證 → 能呼叫的 Supervisor／Core／後端 API」，同一能力的所有入口並排比對。
- 對每份 token 畫出「來源（Supervisor 注入、使用者設定、自動產生）→ 存放（/data、記憶體、子行程環境）→ 送出 → 顯示 → 輪替」。
- 預設開啟的功能（tool 全開、寫入全開、permissive 模式）視為實際政策，不是「使用者可以自己關」。
- 讀到 `0.0.0.0`、`host_network`、`hassio_role`、`ports:`、`curl`、`@main`、`latest` 時一定停下來追完。

## Validation rules (apply before reporting ANY finding here)

1. 寫出入口的實際可達性：ingress（需 HA 登入）或 port（需使用者映射到 host）；可達性取決於部署設定時列 needs_validation。
2. 權限過大本身是 hardening；要成為 finding 必須有一條可達的路徑讓較低信任的主體用到那份權限。
3. token 洩漏要寫出：讀到它的主體、它的權限範圍、以及能拿它做什麼。
4. 供應鏈問題要寫出：誰能控制那個輸入（上游維護者、registry、PR 作者），以及它在哪個身分下執行。
5. Supervisor、Docker、HA core 的行為不在快照裡；決定性時寫明要驗證的版本與行為，不要憑記憶定案。
