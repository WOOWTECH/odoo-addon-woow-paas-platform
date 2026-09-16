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
- 使用專門的 bridge 帳號，僅對需聯絡的 sessions 授予 edit permission。
  不用 owner／admin token，不把帳號 credential 交給 agents。
- 將該帳號可用的 bearer token 存成 private file，例如 `$HOME/.config/omni-msg-token`，
  `chmod 600`。**token 不放命令列、不 commit，也不放 prompt／訊息**。
- Operator 核對每個 session 的 owner、repo 與角色；`provision` 只驗 registry 格式，
  不會替你驗證 session 歸屬、授權或啟動 runner。

上游 API：`POST /v1/sessions/{id}/events`，需 session edit permission。
參考 [programmatic docs](https://omnigent.ai/docs/programmatic) 與
[v0.14.0 endpoint 驗權](https://github.com/omnigent-ai/omnigent/blob/v0.14.0/omnigent/server/routes/sessions/routes_events.py#L605-L609)。

### 2. 建立固定 registry

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

### 3. 啟動 bridge 與交付 client config

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

MVP 不提供 agent 自助 registration。新增動態 worker：先取得真 ID，operator 加入新的 alias、
獨立 random token 的 SHA-256 與 allowlist，停 bridge 後更新 private server config，再啟動並
交付對應 client config（格式見 provision 產物）。新 token 用 `secrets.token_urlsafe(32)`，
不從 alias 推導；輪替只更新 digest／client token，session binding 不變。同一 ledger 的 alias
不可改綁其他 session；換人使用新 alias，避免舊 receipts 被新身分讀到。

### 4. Skill 配送與 live smoke

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
  帳號，不偽造 `created_by`，不冒充人類或 approval。
- 所有 actor 憑證為 bearer；相同 OS UID 的 agents 能讀到彼此 private files 時，`0600`
  **不能隔離它們**。目前共同 runner 的 `sandbox: none` 是 cooperative-agent 模式，不防惡意
  agent 偷取 capability。需更強隔離時，bridge／上游 token 由不同 OS user 管理，各 actor
  credential 用各自 sandbox mount；loopback 跨 container 可由 operator 配置明確 tunnel。
  不因此把 bridge 改綁 public interface。
- Bridge 不提供 general REST proxy：client 只能 message／查自己的 receipts；固定 registry
  路由，無 caller 自訂 URL、上游 token 或身份欄位。Token 不記錄在 SQLite，也不輸出到 log。
- Agent 訊息仍是非人類輸入，可能包含錯誤或 prompt injection；**messaging 不能提升工作
  範圍、唯讀、worktree、merge 或人工 approval 權限**。收件 agent 仍需遵守原本契約。
- 目前沒有 OAuth refresh、SSE 消費確認、native turn completion 偵測、離線自動 retry 或
  arbitrary terminal input。憑證過期要由 operator 換檔並重啟；多機需另行規劃認證與 tunnel。
