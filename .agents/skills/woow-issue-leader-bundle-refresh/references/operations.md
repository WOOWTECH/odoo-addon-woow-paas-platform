# Bundle refresh 操作 reference

由 [skill](../SKILL.md) 到達本檔。這是受 owner gate 控制的 recipe，不是自動部署 script。
執行範圍是 Omnigent 開發工具，不涉及 Odoo／K8s／Cloudflare 或產品發版 gate。

## Source 與 API

- Source snapshot 取固定、已合併 SHA 的 tracked files，包含 `.omnigent/_shared` 與 builder。
  檢查後只打包 `.omnigent/woow-issue-leader`；保證 bundle 內沒有 `__pycache__`、`.pyc`、
  未追蹤檔或本 operator skill。不要從有工作雜檔的 live repo 直接 `rglob` 打包。
- **Snapshot 必須以 `umask 022` 解出。** 指紋把 `mode & 0o111` 當整數納入，涵蓋 owner／group／
  other 三個 execute bit，所以**同樣的檔案內容在不同 umask 下會算出不同指紋**：executable 檔
  在 `umask 022` 下是 `0755`（`0o111`），在 private 目錄慣用的 `umask 077` 下變成 `0700`（`0o100`）。
  取到 0700 的 artifact 目錄時尤其容易誤用。打包後以 `git ls-tree -r <SHA>` 的 mode 對帳 executable
  bit（`100755` ↔ 有 exec、`100644` ↔ 無），不一致就重做 snapshot，不要改 rule 去遷就。
- Omnigent 0.14 的打包實作是 `omnigent.cli._bundle(Path)`；它是 private API，先核對安裝版本
  與 signature，若不同就停下調整 recipe。它可能展開 YAML 中的環境變數；最終 tar 的內容才
  是審核對象，任何未預期 env expansion／secret 都必須停止並隱去值。
- 經 operator 認證：`GET /v1/sessions/{anchor}/agent` 取得 agent ID/version，
  `GET /v1/sessions/{anchor}/agent/contents` 取得 tar.gz；更新是
  `PUT /v1/sessions/{anchor}/agent`，multipart 欄位 `bundle`。傳已核准的同一份 tar bytes，
  不在 apply 時重新打包。PUT 會更新 shared agent，不只是 anchor 的私有副本。
- HTTP client 明確使用真實 `User-Agent`（例如 `omni-msg`），無 redirect／proxy，token 從
  operator private file 讀取、不放 argv／log。上游 token 有原帳號權限，bridge ACL 不會縮權。
- 動態 API 以已安裝版本的 schema/source 為準；不猜 launch／resume 路由。本流程不需要
  `omni run/start/stop/upgrade` 或新增 session。

既有唯讀 CLI（所有路徑變數先由計畫解析成絕對路徑）：

```bash
omni-msg fingerprint --bundle "$BEFORE_BUNDLE"
omni-msg fingerprint --bundle "$CANDIDATE_BUNDLE"
omni-msg registrations --state-dir "$STATE_DIR"
```

Fingerprint validator 拒絕 unsafe archive 時停止。比對 archive 只在 bounded memory 讀取已
驗證的 regular files；不用 `extractall`。指紋算法、限制與拒絕語意的權威是
[operator guide](../../../../.omnigent/omni-msg/README.md)／
[root_admission.py](../../../../.omnigent/omni-msg/omni_msg/root_admission.py)。

## Policy 格式與精準替換

`provision --auto-roots` 的**輸入**是 `{"roots": {...}, "rules": [...]}`；正在使用的
`server.json` 是另一個格式：

```json
{
  "server_url": "https://operator-selected-server.example",
  "token_file": "/operator/private/token-file",
  "registration": {
    "roots": {},
    "poll_seconds": 5,
    "auto_roots": [{
      "agent_name": "woow_issue_leader",
      "owner": "operator-confirmed-owner",
      "host_id": "operator-confirmed-host-id",
      "workspace": "/operator-confirmed/workspace",
      "bundle_fingerprint": "omni-msg-bundle-v1:replace-with-canonical-output"
    }]
  }
}
```

這只是結構示例，不是可直接 provision 的 production config。Actual schema 見
[cli.py](../../../../.omnigent/omni-msg/omni_msg/cli.py) `serve()`／
[root_admission.py](../../../../.omnigent/omni-msg/omni_msg/root_admission.py) `validate_rules()`。

- 用舊 rule **五欄完全相等**定位，必須只有一筆。替換它的 `bundle_fingerprint`；其餘四欄、
  其他 rules、explicit `roots`、poll_seconds、server_url、token_file 均不變。
- 預設是 replacement，不 append 第二個 fingerprint 延長舊內容的信任；多版本過渡另案核准。
- 用 strict JSON decoder 拒絕 duplicate keys，以當前版本 validators 檢查整份 staged config。
  驗證只做純函式，不建 Store／Registrar、不 launch `serve` 當作 syntax check。
- Active config 留在穩定 private 位置；以同目錄 exclusive temporary file、0600、flush/fsync、
  atomic replace 更新，確認 owner／mode 與完整欄位 diff。`provision` 是首次配置工具，不用它
  重建現有 actors／clients／ledger；本流程不改 state-dir、port 或 client directory。

## 影響預覽與停止條件

共享 agent 與 admission 是**兩份盤點**：前者包含 explicit roots／非匹配 sessions，後者包含
其他 agent_id 但相同可信內容的 roots。完整分頁，不能只看 UI 第一頁或兩個熟悉的 IDs。

`RootAdmission.discover()` 是純讀取 metadata／bundle 的 classifier；不建立 Registrar。
對目前 policy 作一次實際 GET 預覽；對候選計畫，用相同 snapshot 的 **in-memory projection**
只替換已核對 shared agent_id 的 bundle bytes，再套 proposed rule，推算 shared PUT 後的結果。
明確標為預測，不偽造 server 證據。單純拿新 hash 查現在的舊 bundle，不是 after 預覽。
Explicit IDs 傳入 `explicit_ids` 保持 precedence；另行盤點它們的 bundle 影響與 descendants。

每個受影響 session（不只 root）列：實際 owner、host、workspace、agent ID/version、
parent/archive、edit permission、runner binding/online、工作/pending 狀態、before/after group、
通知／撤權原因；shared query 必須含 children／archived，不能略過非 admission bindings。
依 [runtime gates](runtime-gates.md) 分類與核對必要 API／process／工作證據、freshness、
owner 的 stored-only／cold-cut 範圍；這份 reference 是 runtime 分類的唯一權威。

- `ready=true` 不表示 runner online；host online 也不表示其 sessions 可執行。
- `active`／`unknown`、未預期納管／撤權或未核准的 shared collateral：`BLOCKED`。
  有完整證據的 `dormant-offline` 可規劃保持 offline 的 stored-only 更新；`idle-online` 需
  具體 cold-cut 方案。Owner 核准 plan digest 前均不 apply，不為過 gate 改 metadata 或喚醒。
- 啟用新規則可能讓既有但未登記 roots 收到首次設定通知；這會產生 model turn，必須列入
  owner 核准範圍。不能宣稱「只換 fingerprint，完全不會喚醒任何 session」。
- Runtime recovery 要另案授權具體 IDs 與副作用，結束後重做 plan；不從 history 的舊 PID
  或一次 bootstrap 授權推論這次也能重啟。

## Plan artifact

保存 Markdown 或 JSON 均可，但以下欄位不得省略；最後計算整份 plan 的 SHA256 給 owner：

| 類別 | 必填 |
| --- | --- |
| Source | integration ref、完整 merged SHA、工具版本、檢查結果、每個 bundle path/mode 的 diff |
| Target | server origin、SSH host、anchor session、shared agent ID/version、scope 四欄 |
| Artifacts | before/candidate tar 絕對路徑與 raw SHA256、old/new canonical fingerprint、config before SHA256 |
| Policy | 完整 old/new rule、精準替換位置、explicit roots／其他 rules 保持的驗證 |
| Runtime | active config/state/client directory/port、supervisor 與精準 bridge stop/start 方法、PID/binding snapshot；每個 session 的分類、API/process/工作 evidence 路徑與時間、telemetry 限制及有效時間窗 |
| Impact | 全部 shared bindings（含 archived／非 admission）及 descendants 的 IDs、before/after admission、撤權／新增能力／首次通知、scope 拒絕原因與 collateral |
| Gates | 完整分頁與分類，無 active/unknown/dispatching；每個 dormant 的 stored-only／future-use 策略、每個 online 的 cold-cut/freeze 前置條件與負責者；owner 核准完整 IDs、範圍與此 plan digest |
| Recovery | backup 位置、允許的停機時間與 rollback 範圍、unknown 時的對帳方式、驗收與停止條件 |

Plan 與 artifacts 在 private、持久目錄保存，不能放會自動清除的 `/tmp`，不能把 live active
config 誤認成可清理的 backup。Human-readable report 不包含 token／client bearer／完整機密內容。

## Apply

**僅在主 skill Step 4 的 owner gate 已通過時執行。** 一名 operator writer；owner 授權沒有
自動授權 subagent delegation。若另有明確派工，交接必須含同一 plan 與邊界，不並行修改。

1. 重讀 plan 與 artifacts，驗 digest／raw SHA／canonical fingerprint。重新 GET metadata／
   bundle，讀 active config 與完整 shared/admission snapshot；與 before 比對 agent version／
   內容、rule/config SHA、完整 scope、binding、model override、工作／通知狀態；依 runtime
   gates 重做 session 分類、process／工作證據與 cold-cut/freeze 查核。證據過期或任何方案
   前提 drift 即重做 plan，舊核准失效；缺 freeze 證據不能繼續，不能靠 owner 核准補缺。
2. 0700 backup 目錄保存 before bundle/config/client files、SQLite consistent backup（SQLite
   backup API，不直接複製正在 WAL 寫入的單一 db 檔）、registration/receipt IDs 與安全 hashes、
   supervisor 啟動資料。保留原 token-file；備份不是重配 credential。
3. 核對 message／registration 無 dispatching。只 graceful stop 計畫中的 **omni-msg bridge**，
   等請求結束、ledger lock 與 listener 釋放，再做最終一致性 backup／baseline；無法排空則停止。
   原 Omnigent host／runners、CLI REPL 與 candidate bridges 保持。無法確定程序身份不發 signal。
   先停 registrar 是為避免下一輪發現「新 bundle＋舊 fingerprint」而提前撤權；不是原子交易。
4. 需要更新時 PUT 計畫的 tar 一次。重新 GET metadata/contents，確認 agent ID 未換、version
   符合本次更新、canonical fingerprint＝核准值，raw hash 與傳輸預期一致。若 server 原已是
   candidate（policy-only 計畫）則不 PUT。狀態不明走下節，不能 timeout 後再 PUT 一次。
5. 用 staged config 執行精準 rule replacement、純函式 validators、欄位 diff 與 atomic write。
   使用同一 binary、state-dir、port、client directory、supervisor 恢復 bridge；單一 listener。
   不移動 state、不升級 CLI、不改全 runner 的 `OMNI_MSG_CONFIG`。
6. 驗兩輪**不同完成時間**、`ready=true` 且未到期的 registrations。實際 matched/rejected、
   members/groups 與核准 projection 一致；DB integrity ok、無 dispatching。所有 before
   message/registration rows 與原 client hashes 保留；新增資料另列，不能只比較 row count。
   核對舊 notification IDs 沒重播，新 root config 各自 0600、無跨 group capability。
   對 dormant 確認保持 offline、沒有未核准 turn；重新核對活動／bindings。意外喚醒或切換
   前提改變時保存 evidence 並停止，僅在原 rollback 授權內處理，不擅自 stop runner。
7. 若需驗 client `list`，只在 owner 核准的本機查詢範圍做；它不證明 agent 已執行。觀察已核准
   的新 registration 原通知，須有實際 shell call、exit0/stdout self/contacts 才記 consumed；
   `accepted`、Skill.args、assistant 聲稱都不算。無新通知也不為了測試建立 root／發 turn。
   Stored-only 的成功只代表 stored bundle／admission continuity；逐 ID 分列 runtime
   adoption 的證據或「本次未驗證」。Dormant 下次使用前的 agent version／harness／override
   與載入 gate 由 plan 指定負責者接手；沒有授權的 fresh-session 驗證就回報缺口，不為了
   驗收而喚醒舊 sessions，也不聲稱新 instructions 已對全部 sessions 生效。

## Unknown／rollback

- PUT、restart、notice 結果不明：`PARTIAL/UNKNOWN`，保存 before/after、request/item IDs 與
  原錯誤。先 GET/讀保存 evidence 對帳；確認副作用之前不重送、不另建 session。
- 只有計畫事先核准的 rollback 才能執行。恢復 before bundle **與** before config 的配對，
  並恢復同一 bridge；動手前確認它們仍是本次 writer 留下的狀態，沒有別人後續的更新。
  Bundle 回退通常也會增加 server version，不能偽報 version 回到舊數字。
- SQLite backup 用於保全／對帳，**不直接覆蓋已接收新資料的 live ledger**；保留新 credentials／
  receipts／通知痕跡。回退可能撤銷新納管能力，也要核對並報告，不能把它寫成無損成功。
- 新 error／DENY、超時、scope 不明或沒有 rollback 授權：停止，報明 bundle/config/bridge 各自
  已完成到哪、bridge 是否仍停著，請 owner 決定；不靠放寬 rule／換 port／換 ledger 掩蓋失敗。
- 對話／報告含 Markdown 或 command 時，透過 `subprocess.run([...], shell=False)` 或安全檔案
  輸入傳遞；不把它插入會解譯 backticks／`$()` 的 shell 字串。報告文字不是 executable code。
