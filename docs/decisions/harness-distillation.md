# project harness distillation

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_harness_distillation`。盤點依據見 [memory-inventory-2026-08-05](../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：Harness 蒸餾計畫狀態——pattern-guard hook + repo-lint gates 已裝並 live-fire 驗證；CLAUDE.md 減法與 /retro skill 待執行

2026-07-06 已落地（均 live-fire 驗證）：

- **pattern-guard PreToolUse hook**：`.claude/hooks/pattern-guard.py` + `pattern-guard-map.json`（7 classes：acl-security / controller-endpoint / model-write-guard / seed-data / seed-default-template / owl-template / paas-operator）。行為＝本 session 首次編輯守護路徑 → deny 一次列必讀 docs → 重試放行（marker 在 `$TMPDIR/claude-pattern-guard/`）。關閉：`CLAUDE_PATTERN_GUARD=off`。
- **repo-lint PostToolUse gate**：`scripts/audit/repo-lint.py`（controller-sudo 白名單 / owl-template / data-noupdate 三 check；baselines 在 `scripts/audit/baselines/`，sudo 存量 155 處 14 檔已 grandfather）。CLI：`--all` / `--update-baseline`。
- 兩者掛在專案 `.claude/settings.json` hooks 段；全程 fail-open。

**Task 2-5 已全部執行完且 PR #306 已 merge 進 develop（2026-07-06，merge commit 4e82e17，分支已刪）**：CLAUDE.md 48,931→28,064 chars（−42.6%；Known patterns 指針化 + pitfalls 31 條收斂進 `docs/tracking/pitfalls/README.md` 索引）、`/retro` skill 已建（harness 有熱載，當場生效）、repo-lint 進 CI（`.gitea/workflows/repo-lint.yml`，紅綠雙向已用 PR #305 驗證後關閉）。第二輪減法（design doc C 段）已做完且 merge（PR #307 → develop ce6dda9，分支已刪）：sandbox×2/deploy-stg/release 縮指針，CLAUDE.md 28,064→21,369 chars（自起點累計 −56.3%）；CI_GITEA_TOKEN 前置與 image-tag rationale 已搬進 `.claude/commands/woow/deploy.md`。CI 學習：fix 後 PR diff 不再觸碰 `src/**` 時 paths filter 讓 workflow 不跑——驗綠需推「乾淨 src 觸碰」commit。

**新守線的登錄點（勿再把守線散文寫回 CLAUDE.md）**：綁路徑 → `pattern-guard-map.json` append；可機械判定 → `repo-lint.py` 加 check；判斷類 → pattern/pitfall doc + README index。

**Auto-mode classifier 邊界**：寫 `.claude/hooks/` 設定或 settings.json hooks 段屬「self-modification」會被擋 → 先 AskUserQuestion 取得明確核准再寫（本次即此流程）。
