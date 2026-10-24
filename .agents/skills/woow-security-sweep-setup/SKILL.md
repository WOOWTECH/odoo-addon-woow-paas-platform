---
name: woow-security-sweep-setup
description: 在一台主機上安裝、檢查或更新 woow-security-sweep：doctor 檢查環境與權限、註冊或接手共用的 sweep agent、以 PUT 原地更新 bundle（agent_id 不變）。新同事第一次設定、每次 bundle 改版發布、或排程跑不起來時使用。
---

# woow-security-sweep setup

本 repo 的 **operator workflow**，不配送進 sweep bundle。權威檔在 `.agents/skills/`；
`.claude/skills/` 是指向同一目錄的 symlink。整體關係見
[.omnigent/README.md §woow-security-sweep 整體關係](../../../.omnigent/README.md#woow-security-sweep-整體關係)。

- Claude Code：`/woow-security-sweep-setup`；Pi：`/skill:woow-security-sweep-setup`
- 腳本：`python3.12 <本目錄>/scripts/setup.py --workspace <WS> <子命令>`
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
| `gitleaks`／`uvx`／`tea` | 工具存在 | 缺 gitleaks 或 uvx ⇒ 該掃描工具標 unavailable；缺 tea ⇒ 不能開 issue |
| `jev_key`／`jev_api` | key 檔權限 600、`jev_gate.py models` 成功 | 沒有 key ⇒ 閘門 A 全部轉人工 |
| `target:<repo>` | repo 是 git checkout、origin 指向 GitHub WOOWTECH 正本 | |
| `issues:<repo>` | tea 登入對 `mirror-internal/<repo>` 有 push 權限 | issue_sync 以 tea 登入者的身分開單 |
| `identity`／`host` | `/v1/me`；本機 host 屬於你、online | **排程只能建在自己的主機**（server `resolve_host_owner`：別人的 host 回 403）⇒ setup 在哪台跑就裝在哪台 |
| `agent` | `<state>/agent.json` 記錄的 anchor 讀得到、名稱與 id 相符 | session-scoped agent 對 anchor session 有 READ 權限即可使用（`validate_session_agent`） |
| `agent_owner` | 你對 anchor 的權限等級 | 原地更新（PUT）需要 OWNER（等級 4）⇒ 共用者只能用、不能發新版（`warn`，不是錯） |
| `schedule:<id>` | agent_id 是現行的、host 是本機、workspace、`permission_mode: auto`、有費用上限 | 換 agent 時 permission_mode 與 max_cost_usd 不會沿用；create 不會存 max_cost_usd |
| `schedule_overlap` | 同一天有沒有兩個排程掃同一個 repo | 兩輪同時推進同一份 state 會重複出題 |

## 2. 第一次在這台主機設定

先讓 doctor 除了 `agent` 以外都通過，再依情況二選一：

- **共用維護者的 agent**（維護者已把 anchor session 分享給你，至少 READ）：
  `setup.py --workspace <WS> record-agent --session <維護者給的 anchor session id>`
- **自己註冊一份**（沒人分享，或你要自己維護）：
  1. `sys_session_create(config_path=<doctor 的 fix 印出的路徑>, title="woow-security-sweep 註冊（未送 prompt）")`
     ⛔ 不帶 message：只是註冊，不要觸發掃描
  2. `setup.py --workspace <WS> record-agent --session <回傳的 conversation_id>`

再跑一次 doctor，`agent` 通過後才建排程。排程欄位見 sweep 的 README §上線步驟；
`agent_id` 取 `<state>/agent.json`，`host_id` 取 doctor 輸出的 `host_id`（有給 workspace 就必填）。
⚠ create 後立刻再 `sys_scheduled_task_update` 一次 `max_cost_usd`（create 不會存）。

## 3. 發布新版（bundle 改版 merge 之後，只有 anchor 擁有者能做）

1. 主 checkout 切到最新整合分支：`git -C <WS>/paas/woow_paas_platform merge --ff-only origin/develop`
2. `setup.py --workspace <WS> bundle-plan`：用 `git archive` 取**已 commit** 的檔案、跑
   `build.py --check`、打包，與 server 上的 bundle 逐檔比對（sha256＋執行位元）。
   - `noop: true` ⇒ 回報 NO-OP，結束
   - 否則把 `diff`（新增／刪除／修改的路徑）、`source_sha`、`plan_digest` 交給人，**停在這裡**
3. 人核准**這個** `plan_digest` 之後：
   `setup.py --workspace <WS> bundle-apply --plan <plan_dir> --digest <plan_digest>`
   - 會先確認 server 上的 bundle 仍是計畫時的版本，再 PUT，之後讀回驗證 agent_id 不變、內容一致
   - 失敗訊息寫「先 GET 對帳」時：⛔ 不要直接重跑 apply，先跑 doctor 與 bundle-plan 看現況
4. 再跑 doctor：`agent` 的版本號應該 +1，排程不需要改（agent_id 沒變）

計畫目錄在 `<state>/setup/plan-*`（0700），保留當紀錄。

## 回報格式

```
主機：<host_name>（<host_id>），使用者 <user>
doctor：ready=<true|false>；fail：<id 清單>；warn：<id 清單>
agent：<agent_id> v<version>（自己的／共用 <recorded_by>）
動作：<record-agent／bundle-plan（NO-OP｜PLAN-READY digest=…）／bundle-apply（DEPLOYED v…）／無>
```
