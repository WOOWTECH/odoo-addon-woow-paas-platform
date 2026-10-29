---
name: woow-security-sweep-setup
description: 在一台主機上安裝、檢查或更新 woow-security-sweep：doctor 檢查環境與權限、註冊或接手 sweep agent 與前台 agent（launcher）、以 PUT 原地更新 bundle（agent_id 不變）。新同事第一次設定、每次 bundle 改版發布、或排程跑不起來時使用。跑一次與建排程走 woow-security-sweep-launch skill。
---

# woow-security-sweep setup

本 repo 的 **operator workflow**，不配送進任何 bundle。權威檔在 `.agents/skills/`；
`.claude/skills/` 是指向同一目錄的 symlink。整體關係見
[.omnigent/README.md §woow-security-sweep 整體關係](../../../.omnigent/README.md#woow-security-sweep-整體關係)。

- Claude Code：`/woow-security-sweep-setup`；Pi：`/skill:woow-security-sweep-setup`
- 腳本：`python3.12 <本目錄>/scripts/setup.py --workspace <WS> <子命令>`
- 管兩個 agent（`--agent`）：`sweep`（排程跑的本體，claude-native，預設）與 `launcher`
  （前台，claude-sdk，出現在 UI picker）。跑一次、建排程、查狀態走 `woow-security-sweep-launch` skill。
- `<WS>`＝排程的 workspace（`<WS>/paas/woow_paas_platform` 是主 checkout、`<WS>/WOOW/` 是要掃的 repo）。
  在 Omnigent session 內可省略 `--workspace`（取 `$OMNIGENT_RUNNER_WORKSPACE`），但互動 session 的
  workspace 常常不是 `<WS>`，**明給比較安全**。

## ⛔ 原則

- **能不能用由檢查決定，不問人。** doctor 的每一項都對應一條由程式碼決定的規則（下表）。
  `fail` 就照 `fix` 處理後重跑，不要用「先跳過」繞過。
- Jev key 只用 stat 看存在與權限，**不讀、不印、不搜**；連線測試交給 `jev_gate.py models`。
- 原地更新一律 **plan → 人核准 plan digest → apply**。code review、merge 都不等於 apply 授權。

## 1. doctor（每次都先跑）

```bash
python3.12 <本目錄>/scripts/setup.py --workspace <WS> doctor --check-api
```

輸出 JSON，`ready: true` 才能往下。檢查項與判斷依據：

| id | 檢查 | 依據（不是人的偏好） |
|---|---|---|
| `workspace`／`state` | `<WS>` 下有主 checkout；state 目錄存在 | state 不存在＝第一次跑會從頭掃，舊的人工決定不會帶過來（換主機要先搬 state） |
| `python` | `python3.12`＋PyYAML＋`tarfile.data_filter` | collect.py 的快照安全檢查需要 data_filter（3.11.2 沒有） |
| `gitleaks`／`uvx`／`tea`／`gh` | 工具存在 | 缺 gitleaks 或 uvx ⇒ 該掃描工具標 unavailable；缺 tea ⇒ 不能開 issue；缺 gh ⇒ since_upstream 的 advisory 標 unavailable（warn） |
| `jev_key`／`jev_api` | key 檔權限 600、`jev_gate.py models` 成功 | 沒有 key ⇒ 閘門 A 全部轉人工 |
| `target:<repo>` | repo 是 git checkout、origin 指向 targets 的 `origin`（沒寫時是 GitHub WOOWTECH 正本） | 檢查哪些 repo：沒帶 `--targets` ⇒ `<state>/presets` 全部有效 preset 的 targets 聯集（輸出頂層 `targets_from: presets`）；一個 preset 都沒有才退回 LINE 三件組（`targets_from: default`）；帶了 `--targets` ⇒ `targets_from: args` |
| `issues:<repo>`（repo 集合同 `target:<repo>`） | tea 登入對 targets 的 `disclosure.issues_to`（預設 `mirror-internal/<repo>`）有 push 權限，且目標不是 public | issue_sync 以 tea 登入者的身分開單；目標 public 時 issue_sync 會拒絕開單 |
| `identity`／`host` | `/v1/me`；本機 host 屬於你、online | **排程只能建在自己的主機**（server `resolve_host_owner`：別人的 host 回 403）⇒ setup 在哪台跑就裝在哪台 |
| `agent` | `<state>/agent.json` 記錄的 anchor 讀得到、名稱與 id 相符 | session-scoped agent 對 anchor session 有 READ 權限即可使用（`validate_session_agent`） |
| `agent_owner` | 你對 anchor 的權限等級 | 原地更新（PUT）需要 OWNER（等級 4）⇒ 共用者只能用、不能發新版（`warn`，不是錯） |
| `launcher` | `<state>/launcher-agent.json` 存在、anchor 讀得到、名稱與 id 相符、harness 不是 `*-native` | UI picker 會濾掉頂層 `*-native`（`.omnigent/README.md` §UI picker）；前台 agent 必須 claude-sdk 才叫得起來 |
| `schedule:<id>` | agent_id 是現行的、host 是本機、workspace、`permission_mode: auto`、有費用上限；prompt 是 `preset: <名稱>` 且 preset 有效（或不超過 40 字元的單行） | 換 agent 時 permission_mode 與 max_cost_usd 不會沿用；create 不會存 max_cost_usd；**長 prompt 在 Omnigent 0.16.0 不會送出**（收合成 `[Pasted text]` 後偵測不到，只盲按一次 Enter，#1989） |
| `schedule_run:<id>` | 最近一次觸發超過 10 分鐘後，session 裡有沒有任何訊息 | run 紀錄的 `succeeded` 只代表「已觸發」；沒有訊息＝prompt 沒送出 |
| `schedule_overlap` | 同一天有沒有兩個排程掃同一個 repo（暫停中的排程不算；沒有 BYDAY 當成每天） | 兩輪同時推進同一份 state 會重複出題；手動排程平常是暫停的，只靠「立即執行」 |

## 2. 第一次在這台主機設定

先讓 doctor 除了 `agent` 以外都通過，再依情況二選一：

- **共用維護者的 agent**（維護者已把 anchor session 分享給你，至少 READ）：
  `setup.py --workspace <WS> record-agent --session <維護者給的 anchor session id>`
- **自己註冊一份**（沒人分享，或你要自己維護）：
  1. `sys_session_create(config_path=<doctor 的 fix 印出的路徑>, title="woow-security-sweep 註冊（未送 prompt）")`
     ⛔ 不帶 message：只是註冊，不要觸發掃描
  2. `setup.py --workspace <WS> record-agent --session <回傳的 conversation_id>`

接著註冊**前台 agent**（每台 sweep 主機一次；做法同 woow_issue_leader——`sys_session_create`
建的是 sub-agent，不會出現在 UI picker）：

1. 請使用者在這台主機執行（互動 CLI，⛔ agent 不代跑）：
   `cd <WS> && omnigent run paas/woow_paas_platform/.omnigent/woow-security-sweep-launcher --server <url>`
2. `setup.py --workspace <WS> record-agent --agent launcher --session <該 session 的 id>`

再跑一次 doctor，`agent` 與 `launcher` 都通過後，先為每個排程建一個 preset（本輪輸入放主機上的檔案，
排程 prompt 只寫名稱）：

```bash
setup.py --workspace <WS> preset --name line-daily --targets <WS>/WOOW/woow_line_base <WS>/WOOW/Woow_odoo_line_liff <WS>/WOOW/woow_odoo_livechat_line --mode incremental --deep-review auto --review claude-native --fix pi
```

輸出的 `schedule_prompt`（例：`preset: line-daily`）就是排程的 prompt。驗證規則與 sweep 共用
（`collect.py` 的 `validate_input`），寫得進去就代表 sweep 讀得懂。

然後用 **`woow-security-sweep-launch` skill** 建排程（每日／每週各一個 `schedule --rrule`，每個 preset
再建一個 `schedule --manual` 供「跑一次」用）。腳本會檢查同一天 targets 重疊、補設 `max_cost_usd`
（部分版本的 create 不存）、讀回逐欄核對；⛔ 不要直接用排程的 MCP 工具建。欄位見 sweep 的 README §上線步驟。

## 3. 發布新版（bundle 改版 merge 之後，只有 anchor 擁有者能做）

sweep 與前台各自發版，步驟相同，用 `--agent sweep|launcher` 指定（預設 sweep）。只改 `launch.py`
不必發版（腳本從主 checkout 執行）；改了前台的 AGENTS.md 或 launch skill 的 SKILL.md 才要發 launcher。

1. 主 checkout 切到最新整合分支：`git -C <WS>/paas/woow_paas_platform merge --ff-only origin/develop`
2. `setup.py --workspace <WS> bundle-plan --agent <sweep|launcher>`：用 `git archive` 取**已 commit** 的
   `.omnigent` 與 `.agents`、跑**該 bundle 自己的** `build.py --check`、打包，與 server 上的 bundle
   逐檔比對（sha256＋執行位元）。這兩個目錄底下 git 追蹤中的檔案有未 commit 的修改（含已 stage 的新檔）就拒絕；
   沒進 git 的檔案（例如本機的 `.omnigent/config.yaml`）不會被打包，不擋。
   - `noop: true` ⇒ 回報 NO-OP，結束
   - 否則把 `diff`（新增／刪除／修改的路徑）、`source_sha`、`plan_digest` 交給人，**停在這裡**
3. 人核准**這個** `plan_digest` 之後：
   `setup.py --workspace <WS> bundle-apply --agent <sweep|launcher> --plan <plan_dir> --digest <plan_digest>`
   （`--agent` 與計畫不符會拒絕）
   - 會先確認 server 上的 bundle 仍是計畫時的版本，再 PUT，之後讀回驗證 agent_id 不變、內容一致
   - 失敗訊息寫「先 GET 對帳」時：⛔ 不要直接重跑 apply，先跑 doctor 與 bundle-plan 看現況
4. 再跑 doctor：`agent`（或 `launcher`）的版本號應該 +1，排程不需要改（agent_id 沒變）

計畫目錄在 `<state>/setup/plan-*`（0700），保留當紀錄。

## 回報格式

```
主機：<host_name>（<host_id>），使用者 <user>
doctor：ready=<true|false>；fail：<id 清單>；warn：<id 清單>
agent：<agent_id> v<version>（自己的／共用 <recorded_by>）
launcher：<agent_id> v<version>（harness <harness>）｜未註冊
動作：<record-agent [--agent …]／bundle-plan（NO-OP｜PLAN-READY digest=…）／bundle-apply（DEPLOYED v…）／無>
```
