# omni-msg

讓 Omnigent sessions 透過 **operator 顯式授權**的 bridge 主動向 parent／sibling／指定
session 發訊息。CLI 使用 Python 3.11+ 標準函式庫，支援 Linux／macOS；Windows 暫不支援
（private file 與 process lock 使用 POSIX API）。不修改 Omnigent、不放寬 `sys_session_send`。

```text
目前 session → omni-msg CLI → loopback bridge → Omnigent events API → 目標既有 session
                                  ↕
                            SQLite receipts
```

## 安裝與測試

在 repo 根執行，安裝位置應是 runner 的穩定工具環境，不用 PR worktree 的 editable install：

```bash
uv tool install ./.omnigent/omni-msg
omni-msg --help
python3 -m unittest discover -s .omnigent/omni-msg/tests -v
```

測試使用真正的 CLI subprocess、bridge HTTP server 與 **fake Omnigent HTTP server**，
不連真 session、不呼叫 LLM、無 K8s 操作。它們驗證 POST wire shape、雙向 routing、ACL、
reply correlation、重複發送、錯誤 ACK、restart recovery 與 private files；**不證明真實
harness 已被喚醒**。上線前還要做下方 live smoke。

## Operator 配置（opt-in）

### 1. 先建立／確認 sessions 與上游權限

- 用原有 Omnigent 流程建立 sessions，取得真實 `conversation_id`。CLI 不會建立、resume
  外部 harness process 或透過 terminal 注入 raw input。
- 建議使用專門的 bridge 帳號，僅對需聯絡的 sessions 授予 edit permission。
  若 operator 明確授權沿用其既有帳號（例如 smoke），上游 token 仍有該帳號的完整權限，
  本地 root／actor ACL 不會縮減它。帳號 credential 一律留在 bridge，不交給 agents。
- 將該帳號可用的 bearer token 存成 private file，例如 `$HOME/.config/omni-msg-token`，
  `chmod 600`。**token 不放命令列、不 commit，也不放 prompt／訊息**。
- Operator 核對每個 session 的 owner、repo 與角色；`provision` 只驗 registry 格式，
  不會替你驗證 session 歸屬、授權或啟動 runner。

上游 API：`POST /v1/sessions/{id}/events`，需 session edit permission。
參考 [programmatic docs](https://omnigent.ai/docs/programmatic) 與
[v0.14.0 endpoint 驗權](https://github.com/omnigent-ai/omnigent/blob/v0.14.0/omnigent/server/routes/sessions/routes_events.py#L605-L609)。

### 2. App 新 root 也自動納管（opt-in）

不想每開一個 root 就登記 ID，使用 `provision --auto-roots`。Operator 一次核准**精確 owner、
host_id、runner workspace、agent_name 與 bundle 內容指紋**；符合規則的既有／新 top-level
sessions 會自動取得獨立 config。這裡比的是 server 回傳的實際 owner，**不是** viewer 的
`permission_level`（admin 看別人的 session 也可能是 4）。名稱只作搜尋與篩選，不能單獨授權。

先取得已覆核的 agent bundle（SDK 打包，或已驗證 session 的
`GET /v1/sessions/{id}/agent/contents`），在 operator 端計算指紋：

```bash
omni-msg fingerprint --bundle approved-agent.tar.gz
```

將輸出的完整 `omni-msg-bundle-v1:<64 hex>` 放入 `auto-roots.json`：

```json
{
  "roots": {},
  "rules": [{
    "agent_name": "woow_issue_leader",
    "owner": "eugene",
    "host_id": "host_replace_with_real_id",
    "workspace": "/home/elmo/woow-repos",
    "bundle_fingerprint": "omni-msg-bundle-v1:replace_with_fingerprint_output"
  }]
}
```

`owner`、`host_id` 用 server metadata 的值，不是顯示名稱或 SSH alias。Fingerprint placeholder
必須替換；缺欄位、不明欄位、重複規則或指紋格式錯誤都拒絕。

```bash
omni-msg provision \
  --directory "$HOME/.config/omni-msg-app-roots" \
  --auto-roots auto-roots.json \
  --server-url https://your-omnigent.example \
  --token-file "$HOME/.config/omni-msg-token"

omni-msg serve \
  --server-config "$HOME/.config/omni-msg-app-roots/server.json" \
  --state-dir "$HOME/.local/state/omni-msg-app-roots"
```

- App 照原流程用既有 `agent_id` 建立新 session，選擇已核准的 host／workspace；registrar
  不會 create、resume、stop runner 或修正 workspace。尚未 bind runner 或不符合規則時不發 credential。
- Root 的 alias 為穩定的 `root-<session ID hash>`，不是共用 agent name。同 agent_id／bundle
  的不同 sessions 仍然分組；各組與 descendants 都有自己的 tokens／contacts。
- `roots` 可保留原有手動指定的 root，這些 explicit entries 優先；從 0.2 的 `--roots` 模式
  轉來，即使改由規則納管，已登記 root 的 alias／credential／receipts 也不換。
  從更早的純 `--registry` 模式遷移時，請在 `roots` 保留原 root alias，不推測歷史 actor 角色。
- 規則也會檢查已存在的匹配 roots，啟用時可能投遞首次設定通知。不是全站自動授權，也不替
  bridge 帳號取得 server 權限；專用帳號仍必須能發現並有權 edit 新 sessions。
- 指紋涵蓋 sorted file paths、file contents、executable bits；忽略 gzip/tar 時間、uid/gid
  等包裝差異。Symlink／hardlink／special files、重複或逃逸路徑、不明 PAX 執行 metadata
  一律拒絕；不解壓到磁碟。輸入上限 compressed 8 MiB、unpacked 16 MiB、2048 entries。
- Bundle 內容／執行屬性改版後，須 operator 重新覆核並更新規則；**不是每個 session 核准**。
  使用 project skill [woow-issue-leader-bundle-refresh](../../.agents/skills/woow-issue-leader-bundle-refresh/SKILL.md)
  先做 plan，再經 owner 核准 apply（shared-agent 影響、bridge 暫停／更新、continuity／rollback）。
  指紋檢查每輪重做，同輪同 agent_id/version 可共用結果，不永久信任舊 cache。
- `registrations` 額外提供 `root_admission.scanned / matched / rejected`；錯 owner／host／
  workspace、同名但內容不同等會記錄安全的拒絕原因，不投遞通知。Session metadata、agent
  metadata、bundle 任一 phase 的 GET 回 403/404 時記為 `root_access_unavailable`；其他無法
  驗證的情況（oversize、HTTP 5xx、格式錯誤等）依 phase 分別記為 `session_metadata_unverifiable`、
  `agent_metadata_unverifiable`、`bundle_unverifiable`。這些紀錄只附安全的 phase／status／
  `error` code／大小 limit／已讀 bytes，不記 response body 或 credential，且只排除該 candidate；
  但本輪所有 root 層 detail 都取不到且有 upstream 不可用證據時整輪 fail closed（見第 3 節
  「安全與容量限制」）。Candidate detail 回的 `id` 與查詢不一致（`root_identity_mismatch`）則
  整輪 fail closed。
  Archive、失去資格或從查詢消失的 root 與其 branch 在下一輪撤權，舊 tokens 不再能 authenticate。
- 每輪分頁掃描最多 256 candidates，與 explicit roots 合計最多 16 roots；候選清單（全域 root
  inventory）的 API／分頁異常或超限 fail closed。請封存不再使用的 roots，避免長期累積。
- 這核對的是 **server 保存的 bundle**，不是正在執行的 instruction context attestation。
  已啟動 Pi 的 AGENTS hot reload 限制仍在；新內容應以 fresh session 驗證。同 UID 的 OS
  隔離限制亦未改變。不要只改名稱、workspace metadata 或放寬規則來掩蓋不符的 runtime。

### 3. 自動 descendants：每個 root 授權一次

適合動態 repo-leader／worker。Operator 準備 `roots.json`，值必須是真實的 **top-level**
session ID；不是 agent name，也不是任意 agent 自稱的身分：

```json
{
  "issue-leader-a": {"session_id": "root_session_a", "workspace": "/home/elmo/woow-repos"},
  "issue-leader-b": {"session_id": "root_session_b", "workspace": "/home/elmo/woow-repos"}
}
```

```bash
omni-msg provision \
  --directory "$HOME/.config/omni-msg-auto" \
  --roots roots.json \
  --server-url https://your-omnigent.example \
  --token-file "$HOME/.config/omni-msg-token"

omni-msg serve \
  --server-config "$HOME/.config/omni-msg-auto/server.json" \
  --state-dir "$HOME/.local/state/omni-msg-auto"

# 另一個 terminal：查的是有時間戳的 registrar snapshot，不會喚醒 sessions。
omni-msg registrations --state-dir "$HOME/.local/state/omni-msg-auto"
```

`serve` 內含 registrar，**不會另起 Omnigent runner**。每 5 秒（`provision --poll-seconds` 可調）
循 `GET /v1/sessions/{id}/child_sessions` 分頁與每個 session 的 metadata 盤點，不掃全站：

- 核對實際 parent、edit permission、archive 狀態；root 的 runner workspace 必須吻合計畫。
  Child 必須綁同一 runner；其 workspace 可繼承（null）或位於 root workspace 內。
- 每個 root 各自成組，同組成員雙向可聯絡；跨 root 不互通。相同 bundle name 不代表同組。
- 自動建立 `state-dir/clients/<alias>.json`（directory 0700／file 0600）。Root 使用 operator
  指定的 alias；descendant 的 `session-<hash>` alias 固定由 session ID 推導，經 ledger 核對不重綁。
- 以一次性的 `[omni-msg registration ...]` 設定通知，把**本人 config 路徑**交給原 session，
  不傳 credential。通知可能晚於第一張工單；既有流程照常，新 worker 不繼承 parent config。
- 沒有 HTTP registration／credential bootstrap endpoint。新 root 仍需 operator 改 private
  server config 並重啟 bridge；agents 不自行加 root、配發 credential 或指定自己的 group。

### Discovery budget 設定與升級

串行掃描多個 roots／descendants 可能超過舊版固定 20 秒；metadata latency 波動時，
60 秒也曾逾時。因此 root admission 與 tree traversal 的 metadata GET 改為每批最多 4 個
並行，整批 join 後才進行授權判斷／children 分頁；agent metadata、bundle、credential
與通知不平行化。Root admission 與 tree traversal 仍各自重新讀取 root metadata，保留
二次 scope／runner／identity 查核，不跨 phase 或跨輪 cache。每批最多排 4 個工作，所有
讀取沿用同一 deadline；錯誤仍按原有 subtree rejection／全輪 fail-closed 分流。

唯讀實境 probe 同為 85 次 GET：candidate 兩輪約 28.1／28.6 秒，member／admission 結果相同；
串行對照約 39.6 秒，另一輪已重現 60 秒逾時。這是間歇性 latency 下的效能證據，不保證
所有未來 scan 都能完成，也不代表 notification 消費或 live rollout 已驗證。
`provision --scan-seconds 60` 可在首次配置時指定；既有 deployment 不重新 provision，改用
active `server.json` 的 `registration.scan_seconds`。省略該欄位採 60 秒，舊 config 相容；
未知欄位、非有限數值、bool 或超出 1..120 秒均拒絕。單次 GET 仍為
`min(10, 整輪剩餘秒數)`，整輪 deadline 仍有限，大小／分頁／lineage 守線不變。

升級 installed CLI 與 graceful restart 是 operator rollout，不因 repo merge 自動執行。
沿用原 config／state／port／clients，先保全 receipts 與 notification IDs，排空 dispatching；
部署計畫須核對既有 runner 狀態及新納管通知的副作用。提高 budget 也會延長成功 snapshot
的有限 lease，代表 upstream 尚未完成下一輪驗證時的 stale ACL 視窗較長，不能視為即時撤權。

`registrations` 的 `ready` 代表最近一輪完整 discovery 成功，**不代表所有設定通知已消費**。
逐筆看 `members[].notification.delivery`：`pending / dispatching / accepted / rejected / unknown`。
`accepted` 仍只代表 API 排隊；`unknown`、`rejected` 不自動重送，operator 需先查原 session
history 的 `registration_id`，必要時手動交付同一個 config 路徑。重啟把未完成的 `dispatching`
標為 `unknown`；已配發 token、通知 ID 與 message receipts 都保留。

安全與容量限制：

- 每輪 discovery 預設最多 60 秒（`registration.scan_seconds` 可設 1..120 秒）、16 roots、
  1024 個 tree nodes、深度 16、每組 128 個成員；
  超限 fail closed。已完成且不再需 messaging 的 sessions 應由 operator 依原生命週期封存。
- Session detail 一律帶 `include_items=false`，JSON metadata response 上限為 1 MiB，與訊息 ACK
  的 64 KiB 上限及 bundle 的 8 MiB 上限分開。若 server 忽略 query，超過 1 MiB 仍會截斷並拒絕
  該 session。
- Archive／失去 edit permission／runner 或 workspace 不符的 branch 會在下一輪撤出 snapshot。
- 單一 session detail 異常（oversize、HTTP 4xx/5xx、格式錯誤）只排除該 session 與其未驗證的
  descendants，不配發 credential，其他 roots 照常；已配發的 credential 不換，恢復後同一 client
  config 繼續可用，也不重發設定通知。`registrations.session_rejections` 逐筆記錄 `session_id`／
  `reason`／`phase`／`status`／`error` code／`limit`／`read_bytes`，不保存 response body 或
  credential；403/404 記為 `session_access_unavailable`，其他記為 `session_metadata_unverifiable`。
- 下列情況撤銷 active ACL（`registrations` 為 `ready=false` 並附 error code，bridge 回 503
  `registration_unavailable`）：identity／lineage 不一致、全域 root inventory、child inventory、
  分頁、tree 上限、scan 逾時，以及本輪連一個 root 層 detail 都取不到且原因是 upstream 不可用
  （連線失敗／逾時或 HTTP 5xx）。Root 層 detail 同時涵蓋 root admission 的候選 detail 與 registrar
  的 root detail，任一邊取得可解析 detail（即使之後因 bundle、owner、archive 等被拒）就只局部化；
  agent／bundle phase 與 descendants 的失敗不列入。成功發布 snapshot 時，lease 為
  `max(30, 3 × poll_seconds, 本輪剩餘通知時間 + scan_seconds + poll_seconds)` 秒，
  涵蓋本輪通知、下輪等待及 scan；本輪剩餘通知時間不超過 `scan_seconds`，所以 lease
  上限為 `max(30, 3 × poll_seconds, 2 × scan_seconds + poll_seconds)`（目前參數界線下最多
  300 秒）。掃描失敗仍立即撤權；lease 逾期也拒絕 requests，不會永久沿用失效名單。
- 每個 request 使用同一代 ACL；已開始的投遞不追溯撤回。既有 session 不能換 root／alias，
  token file 被改動或遺失也不靜默重發，會 fail closed 等 operator 處理。
- State 綁定 server origin、bridge port 與 client directory；更換這些位置請用新 state。
  手動模式可保留；切換自動模式不會清除同 ledger 的舊 receipts，但只有當輪通過核對的 actors
  才能使用 bridge。候選測試建議用獨立 port 與全新 private config／ledger。
- 這是 capability 與 contacts 的邏輯隔離，**不是 OS sandbox 隔離**；相同 UID 能讀到別人
  private file 時仍能持有其 capability。不要以此宣稱 process identity attestation。

### 4. 人工模式：建立固定 registry

例如 `registry.json`（替換 `conv_*` 成已確認的真實 ID）：

```json
{
  "issue-leader": {
    "session_id": "conv_issue_leader",
    "allow": ["repo-leader", "implement-123"]
  },
  "repo-leader": {
    "session_id": "conv_repo_leader",
    "allow": ["issue-leader", "implement-123"]
  },
  "implement-123": {
    "session_id": "conv_worker",
    "allow": ["issue-leader", "repo-leader"]
  }
}
```

`allow` 是**有方向**的 allowlist；reply 也需反方向授權。重複 JSON key 直接拒絕，
不同 alias 不能綁相同 session 或 credential。
`list` 只列自己可聯絡的 aliases，不廣播整台 server 的 sessions。

```bash
omni-msg provision \
  --directory "$HOME/.config/omni-msg-paas" \
  --registry registry.json \
  --server-url https://your-omnigent.example \
  --token-file "$HOME/.config/omni-msg-token"
```

輸出目錄是 `0700`，裡面有 `server.json`（credential digest／session ACL）與
`clients/<alias>.json`（該 actor 的 bearer capability），檔案 `0600`。既有目錄拒絕覆寫。

### 5. 人工模式啟動與 client config

```bash
omni-msg serve \
  --server-config "$HOME/.config/omni-msg-paas/server.json" \
  --state-dir "$HOME/.local/state/omni-msg-paas"
```

預設只綁 `127.0.0.1:6778`；自訂 port 時 provision／serve 兩處用相同 `--port`。
上游必須 HTTPS（loopback HTTP 例外），不追 redirect、不使用 HTTP proxy。
服務用 foreground 跑法；正式常駐由 operator 的 process supervisor 管理，停止用 SIGINT
可等進行中的 requests 結束。未等完即中止時，下次啟動把 dispatching receipts 標為 unknown。

每個 session **只交付它自己的** client config，放在 runner 可讀的 private 位置，明確在
工單提供路徑。不要給共同 runner 設同一份 `OMNI_MSG_CONFIG`，否則所有 child 都會繼承同一
actor credential。可在每次呼叫指定：

```bash
omni-msg --config /private/implement-123.json list
omni-msg --config /private/implement-123.json send issue-leader "PR 已就緒"
```

只有已隔離的單一 session 環境才適合設定 `OMNI_MSG_CONFIG`。CLI 不信任
`OMNIGENT_SESSION_ID`／`--from` 等自稱身分，bridge 只按 credential 決定 sender。
它驗證的是 **capability 持有人**，不是對 harness process 身分的遠端 attestation。

所有模式都不提供 agent 自助 registration。若仍採人工模式，新增 worker：先取得真 ID，operator 加入新的 alias、
獨立 random token 的 SHA-256 與 allowlist，停 bridge 後更新 private server config，再啟動並
交付對應 client config（格式見 provision 產物）。新 token 用 `secrets.token_urlsafe(32)`，
不從 alias 推導；輪替只更新 digest／client token，session binding 不變。同一 ledger 的 alias
不可改綁其他 session；換人使用新 alias，避免舊 receipts 被新身分讀到。

### 6. Skill 配送與 live smoke

單一來源：[`../woow-issue-leader/skills/omni-msg/SKILL.md`](../woow-issue-leader/skills/omni-msg/SKILL.md)。
`build-bundles.py` 配送到 issue-leader／兩個 repo-leader；`generate-worker.py` 配送到各 worker。
已啟動的舊 session 不一定會自動看到新版 skill，需重新載入 bundle 或由 operator 提供內容。
CLI binary 與 credential **不**打包進 agent bundle。沒有配置或遇 policy DENY 時維持原流程，
不為了 messaging 關掉 `read_only_os`／其他 guardrails。

在兩個**專供測試且可寫**的既有 sessions（記為 A／B）驗收：

1. 核對各自 `list` 的 `self`／contacts，送 A→B 的 `ask`，保存 ID。
2. B idle 時應收到標為 agent message 的新 turn；確認使用的是原 conversation ID。
3. B 執行 `reply <ID>`；A 的 `check <ID>` 看見明確 reply。再驗 B→A 主動 `send`。
4. B 忙碌時再送一則無害 `send`，確認 queued 與實際消費時間，不能把 ACK 當已完成。
5. 驗未授權 alias、停止／離線 runner、重啟 bridge 後的 receipt；不讓 smoke 消息要求操作
   真產品碼、merge 或 approval。測試完保留 receipts 與 session 連結供覆核。

自動模式另需兩組 dedicated roots／descendants 驗：不人工交付 client config 的原 session
收到設定通知並能呼叫 CLI；新增 descendant 自動加入、同組雙向通訊、跨組兩向拒絕、第三個未
allowlist root 不登記、封存撤權、重啟保留 credential 與通知狀態。不要為 smoke 修改正式 runner。

App root admission 另需驗：使用同一 approved agent_id 的 JSON session-create 流程建立兩個
新 roots，不把 IDs 加進 policy；驗首次自動通知、自行 list、同組通訊、跨組拒絕，以及同名但
不同 bundle 的 root 無 credential／通知。Candidate 用獨立 port/config/ledger，不能碰正式 root。

此 repo 的 fake endpoint 測試不能取代以上步驟；**未完成 live smoke 就不得聲稱可投入無人值守**。

## 指令與回覆語意

```bash
omni-msg list
omni-msg send issue-leader "進度通知"
omni-msg ask repo-leader "需要決策"
omni-msg reply <request-id> "使用 develop"
omni-msg check <request-id>
```

- 所有結果為 JSON；長文字用 `-` 從 stdin 讀取，最多 UTF-8 8192 bytes。
- `ask` 非同步，回的是 receipt，不等待對方的 final response；避免 parent／worker 互等。
- `reply` 只允許原 ask 的接收者回一次，原 sender 用 `check` 讀到 `reply` 與 `answered`。
  不從「最近一則 assistant 回應」猜答案；對方必須明確呼叫 CLI。
- `send` 與 `reply` 無自動 ACK，不會因通知本身觸發無限互回。`check` 不發送 Omnigent
  events、也不讀 terminal。沒有 `--raw`、interrupt、archive、merge 或 approval 功能。

| delivery | 意義 |
|---|---|
| `dispatching` | 本地已保存，正在向 Omnigent 發送 |
| `accepted` | API 明確回 `queued: true`；不是 consumed／completed |
| `rejected` | 明確的驗權／驗值／policy 拒絕 |
| `unknown` | timeout、5xx、意外 ACK、bridge 中斷；可能已送達，禁止盲目重送 |

`answered: true` 只表示有 explicit reply 記錄，仍要看 `reply.delivery`；例如 reply 已落盤但
通知 parent 失敗，parent 主動 check 仍可讀取該答案。工作完成／測試通過不由 bridge 判定。
exit 0＝正常查詢或接受；exit 2＝設定／權限／transport 錯誤；exit 3＝receipt 為 unknown／rejected。

## 重複、失敗與恢復

CLI 在 POST **之前**把 `request_id` 印到 stderr，stdout 成功時有完整 receipt。
HTTP timeout 後保留 ID：先 `check <ID>`；需要重試 CLI→bridge request 時，原指令加
`--id <相同 ID>`，**相同 ID／payload 永遠不再次發送上游**，不同 payload 回 conflict。

本地落盤與遠端投遞不是一個 transaction，**不宣稱 exactly-once delivery**。結果不明時
回 unknown，不自動重送；由 operator 在目標 history 以 envelope ID 對帳。人工確認沒有送達後
才可用新 ID 發送。若接收者已做過工作，不因再看到同一邏輯訊息而重做。

SQLite private ledger 保存 body／reply／delivery／item_id 與時間；每個 actor 最多 10000 筆，
到限額拒絕，無自動刪除。Operator 備份後為新 workflow 使用新 ledger；備份也是機密。
每個 ledger 同時只能由一個 bridge process 開啟。未知投遞狀態不在 restart 時重播。

## 安全範圍與已知限制

- **有授權的能力擴充**，不是逃避 child-only 權限：部署前由 owner 核准可連線的 sessions，
  bridge 持有專用帳號權限，訊息 envelope 明示 actor 身分。上游 audit attribution 是 bridge
  所用帳號（專用或 operator 明確授權的既有帳號），不偽造 `created_by`，不冒充人類或 approval。
- 所有 actor 憑證為 bearer；相同 OS UID 的 agents 能讀到彼此 private files 時，`0600`
  **不能隔離它們**。目前共同 runner 的 `sandbox: none` 是 cooperative-agent 模式，不防惡意
  agent 偷取 capability。需更強隔離時，bridge／上游 token 由不同 OS user 管理，各 actor
  credential 用各自 sandbox mount；loopback 跨 container 可由 operator 配置明確 tunnel。
  不因此把 bridge 改綁 public interface。
- Bridge 不提供 general REST proxy：client 只能 message／查自己的 receipts；固定 registry
  路由，無 caller 自訂 URL、上游 token 或身份欄位。SQLite 可存 credential digest，
  不存 bearer 明文，token 也不輸出到 log。
- Agent 訊息仍是非人類輸入，可能包含錯誤或 prompt injection；**messaging 不能提升工作
  範圍、唯讀、worktree、merge 或人工 approval 權限**。收件 agent 仍需遵守原本契約。
- 目前沒有 OAuth refresh、SSE 消費確認、native turn completion 偵測、離線自動 retry 或
  arbitrary terminal input。憑證過期要由 operator 換檔並重啟；多機需另行規劃認證與 tunnel。
