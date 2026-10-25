# woow-security-sweep-launcher（前台 agent）

woow-security-sweep 的前台。**本檔給維護者看**，不進 agent 的 prompt（prompt 是 `AGENTS.md`）。
整體關係見 `.omnigent/README.md` §woow-security-sweep 整體關係；設計見 #1978（階段 B）。

```
woow-security-sweep-launcher/   claude-sdk／claude-sonnet-5-5／medium／spawn: false
├── config.yaml                 產物（build.py 產生，⛔ 不要手改）
├── build.py                    產生 config.yaml、複製 skill 副本 ＋ 8 項驗收（--check）
├── AGENTS.md                   前台 prompt（手改）
└── skills/
    └── woow-security-sweep-launch/SKILL.md   副本（正本在 .agents/skills/，⛔ 不要手改）
```

- **為什麼是 claude-sdk**：UI picker 會濾掉頂層 `*-native`（`.omnigent/README.md` §UI picker）。
  前台不需要 Inbox——人就在對話框前；判斷題由 sweep 本體（claude-native）送進 Inbox。
- **不 spawn**：跑一次是觸發 sweep 的暫停中手動排程「立即執行」（`launch.py run`），與每日排程同一條路。
- **腳本在主 checkout**：bundle 只帶 SKILL.md，`launch.py` 一律從
  `<WS>/paas/woow_paas_platform/.agents/skills/woow-security-sweep-launch/scripts/` 執行。
  改 launch.py 只要主 checkout 更新，不必發新版 bundle；改 SKILL.md 或 AGENTS.md 才要。
- 守線：沿用 sweep 的 P-secret／P-forge／P-gitea（直接 import sweep 的 `build.py`，CEL 與測試向量共用）
  與 merge／approve／tag／escape，另加前台專屬兩條：
  - `p_schedule_via_script`：不直接用排程的 MCP 工具、不直接打 `/v1/scheduled-tasks`
  - `p_state_via_script`：不直接寫 `<WS>/.omnigent-runtime/woow-security-sweep/`

## 驗收

```bash
python3 .omnigent/woow-security-sweep-launcher/build.py           # 重產 config.yaml ＋ skill 副本
python3 .omnigent/woow-security-sweep-launcher/build.py --check   # 8 項驗收（1／3 需要本機 omnigent）
```

不需要 omnigent 的項目（drift、skill 副本、symlink、harness）另在
`.omnigent/woow-security-sweep/tests/test_launcher_build.py`，由 CI 的 security-sweep-tests 跑。

## 註冊與發新版（走 setup skill）

1. 在 sweep 主機的 `<WS>` 執行一次（做法同 woow_issue_leader；`sys_session_create` 建的是 sub-agent，
   不會出現在 picker）：

   ```bash
   cd <WS> && omnigent run paas/woow_paas_platform/.omnigent/woow-security-sweep-launcher --server <url>
   ```

2. 記下該 session 的 id：`setup.py --workspace <WS> record-agent --agent launcher --session <id>`
   （存在 `<state>/launcher-agent.json`）
3. `setup.py --workspace <WS> doctor`：`launcher` 一項通過（紀錄在、讀得到、harness 不是 `*-native`）
4. 改版：`bundle-plan --agent launcher` → 使用者核准 `plan_digest` → `bundle-apply --agent launcher --plan … --digest …`
