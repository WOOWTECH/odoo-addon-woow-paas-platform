# woow-security-sweep

每日安全掃描與修正的 Omnigent bundle。**本檔給維護者看**，不進 agent 的 prompt（prompt 是
`AGENTS.md`）。設計討論與決策見 #1921。

```
woow-security-sweep/          claude-native／claude-sonnet-5-5[1m]／high／spawn: true
├── config.yaml               產物（build.py 產生，⛔ 不要手改）
├── build.py                  產生 config.yaml ＋ 7 項驗收（--check）
├── AGENTS.md                 leader prompt（手改）
├── gates.yaml                Jev model 版本、mode（shadow／enforce）、門檻
├── targets/<repo>.yaml       各 repo 的 private 上下文（⚠ 不可複製到 public repo）
├── skills/
│   ├── sec-collect/          scripts/collect.py：設定合併、增量範圍、掃描工具、findings
│   ├── sec-gate/             scripts/jev_gate.py：Jev 閘門、路由、shadow log、校準
│   ├── sec-dispatch/         用 woow-issue-leader/generate-worker.py 派 worker
│   └── sec-report/           report.md 與 Inbox 提問
└── tests/                    unittest（CI：.gitea/workflows/security-sweep-tests.yml）
```

## 流程

```
排程（prompt 帶 targets）
 → collect run（每個 repo）→ [deep review worker → collect import]
 → jev_gate triage（閘門 A）→ jev_gate carryover（帶入前幾輪未決）
 → report.md ＋ AskUserQuestion（Inbox）
 → jev_gate human（記錄人的決定）
 → 人選「修正」且 ≤ medium ⇒ fix worker → review worker → jev_gate ready（閘門 C）→ 再問人
 → collect commit（推進 last_sha）
```

**Phase 1（shadow）**：`gates.yaml` 的 `mode: shadow` ⇒ 每筆 decision 的 `effective` 一律是
`human`。Jev 的 `proposed` 只拿來和人的決定比對。`tests/test_jev_gate.py` 有一條測試鎖住
committed 的 mode 必須是 shadow——改成 enforce 要人拍板、改測試、走 PR。

**Phase 2**：`jev_gate.py calibrate` 每個閘門 ≥ 50 筆後，依信心區間的一致率逐項放開
（先放「丟誤報」與「開 draft PR」）。

## 設定的兩層

| 位置 | 內容 | 可見度 |
|---|---|---|
| 目標 repo 的 `.security/sweep.yaml` | `scan`、`always_human`、`autofix`、`verify` | public；**只讀 origin 預設分支** |
| 本目錄 `targets/<repo 目錄名>.yaml` | `context`、`review_focus`、`history`、`baseline`、`disclosure` | private（本 repo） |

collect.py 只從 private 檔取上面五個 key，private 不能放寬 public 的規則。repo 沒有
`.security/sweep.yaml` ⇒ 保守預設（不自動修、high 以上轉人工）。`.security/` 有任何變動 ⇒
自動產生 `security-config-changed` finding、必轉人工（防止有人在 PR 裡偷加豁免）。

## Jev（TypeSafe System One）

- API：`POST https://api.typesafe.ai/v1/systemone`，Bearer 認證；題型 choice／score／noul
- key：`~/.config/typesafe/api_key`（`chmod 600`），**只有 jev_gate.py 讀**；可用
  `TYPESAFE_API_KEY_FILE` 覆寫路徑。leader 的 `p_secret_no_api_key_read` 擋讀取
- `gates.yaml` 釘版本號（`jev-1.13.0`），⛔ 不用 `jev-latest`：alias 換版後答案會變、門檻失效。
  換版要重跑 calibrate
- 問題一律英文：官方文件註明 CJK 準確度較差
- 冒煙測試：`python3.12 skills/sec-gate/scripts/jev_gate.py models`

## 上線步驟

bundle 不綁主機：`<WS>` 是排程的 workspace，agent 開工時由 `$OMNIGENT_RUNNER_WORKSPACE` 取得（#1934）。

| 主機 | host_id | `<WS>` |
|---|---|---|
| UbuntuServer-59mqj | `88ca3ca80a434609a3ed3a35d7bac513` | `/home/elmo/woow-repos` |
| Pi Web ChatGPT (pi3) | `f3cb408633ad4ddabcbabec0401c41d6` | `/data/pi-agent/home/woow-repos` |

1. **主機環境**（換主機時逐項確認）：
   - `python3.12` 可用且裝了 PyYAML（`collect.py` 需要 `tarfile.data_filter`，3.11.2 不行）
   - `gitleaks`（釘 8.30.1，對過官方 checksums）、`uvx`（semgrep／bandit 由它拉）
   - Jev key 在 `~/.config/typesafe/api_key`（`chmod 600`）；`claude` 已登入
   - `<WS>/WOOW/` 下有要掃的 repo；`<WS>/.omnigent-runtime/woow-security-sweep` 是 state
     （換主機要整個搬過去，否則人工決定與 carryover 會遺失）
2. 更新該主機的主 checkout：`<WS>/paas/woow_paas_platform` 切到最新 develop
   （AGENTS.md 與 skills 用這個路徑呼叫腳本）
3. 本機驗收：`python3.12 .omnigent/woow-security-sweep/build.py --check`、
   `python3.12 -m unittest discover -s .omnigent/woow-security-sweep/tests`
4. 註冊 agent：`sys_session_create(config_path="paas/woow_paas_platform/.omnigent/woow-security-sweep")`
   （路徑相對於 workspace），手動餵一次 prompt 試跑；記下 `agent_id`
5. 建排程（`sys_scheduled_task_create`）：

   | 欄位 | 值 |
   |---|---|
   | `agent_id` | 第 4 步的 id（bundle 每次改版重新上傳都會換 id ⇒ 要 update 排程） |
   | `rrule` | 每日 `FREQ=WEEKLY;BYDAY=TU,WE,TH,FR,SA,SU;BYHOUR=8;BYMINUTE=30`；每週 `FREQ=WEEKLY;BYDAY=MO;BYHOUR=8;BYMINUTE=30` |
   | `timezone` | `Asia/Taipei` |
   | `workspace` | 該主機的 `<WS>` |
   | `host_id` | 該主機的 host_id（有給 workspace 就必填，否則 400） |
   | `permission_mode` | `auto` |
   | `max_cost_usd` | `30`（config 另有 60 USD 硬上限） |
   | `prompt` | 見下 |

   ```yaml
   targets:
     - <WS>/WOOW/woow_line_base
     - <WS>/WOOW/Woow_odoo_line_liff
     - <WS>/WOOW/woow_odoo_livechat_line
   mode: incremental        # 每週：full
   deep_review: auto        # 每週：always
   roster: {review: claude-native, fix: pi}
   ```

   週一只跑每週那支（全量），避免兩支排程同時推進同一份 state。

## 已知限制與風險

| 項目 | 狀態 |
|---|---|
| worker 不繼承 `p_secret_no_api_key_read`／`p_forge_no_public_write` | 守線由 generate-worker.py 決定；目前靠工單紀律。fix worker 處理的是不可信的 repo 內容，Phase 2 前應把這兩條加進產生器 |
| CEL 守線是字串比對 | glob（`cat ~/.config/t*/a*`）等寫法擋不住；best-effort |
| AskUserQuestion 在排程 session 的等待上限 | 未實測。report.md 是完整紀錄，Inbox 項目消失也不遺失資料；第一次上線要確認 |
| gitleaks | runner 未安裝 ⇒ `tools.json` 標 `unavailable`；semgrep `p/secrets` 部分涵蓋 |
| pip-audit | 套件未釘版本 ⇒ 標 `skipped`；要掃得改在 Odoo image 層 |
| fix 用 claude-native | 需 #1729 合併；之前產生器只接受 implement + pi |
| symlink 逃逸 | 快照階段已擋並產生 high finding；fixer 在真 worktree 裡的路徑檢查仍是字串比對（見 #1729） |
| high／critical 的 private fork | Phase 1 只產 advisory 草稿，建立 advisory 與 private fork 由人操作 |
