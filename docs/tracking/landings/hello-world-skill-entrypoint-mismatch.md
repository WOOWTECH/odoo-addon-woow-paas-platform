# reference hello world skill entrypoint mismatch

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_hello_world_skill_entrypoint_mismatch`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：hello-world demo skill 執行失敗根因 — bundle script 在 src/main.py 但 runtime loader default entrypoint=scripts/main.py 且 SKILL.md 未宣告

stg hello-world demo skill 在 chat 觸發時 tool 會 fire 但回 **skill 執行 error**（`exit_code 2: python3.11: can't open file '.../hello-world/0.1.0/scripts/main.py': No such file or directory`，error 字串長 216）。

**根因**：bundle 內部 entrypoint 路徑不一致。
- hello-world bundle（`woow_paas_platform/src/data/demo_system_skill.xml` 的 `demo_attachment_hello_world`，#135-migrated sha `6a8e7f87270a60dd50a276d88a97069b2171ba7f3178e5a5f92302534df71f72`）tar 結構 = `['SKILL.md', 'src', 'src/main.py']` —— script 在 **`src/main.py`**。
- runtime `paas-agent-runtime/runtime/skills/loader.py:49`：`"entrypoint": data.get("entrypoint", "scripts/main.py")` —— 從 SKILL.md frontmatter 讀 `entrypoint`，**沒宣告就 default `scripts/main.py`**。
- hello-world SKILL.md frontmatter 只有 name/version/description/author，**無 `entrypoint`** → loader 找 `scripts/main.py` → 路徑兜不上。

**這 bug 長期被 #14 UX bug 遮住**：#14 修前，tool 後空總結 → error frame → 使用者看不到回覆 → 沒人看到 skill 其實 error。#14（runtime 1.1.4）surface tool_result 成可見回覆後才暴露。2026-06-02 session 記的「tool_result chars=216」其實就是這個 error，非真 greeting。可視為 #135 bundle 重構只修了 SKILL.md 放根目錄、沒一併把 entrypoint 對齊 runtime 慣例的半套。

**✅ RESOLVED（2026-06-03，issue #139 / PR #140 → develop，manifest .33）**：paas-platform 採「bundle 對齊 loader default」方案 —— bundle 重打包到 canonical `scripts/main.py`、SKILL.md frontmatter 顯式宣告 `entrypoint: scripts/main.py`，且**第二層缺陷一併修**（script 原本不符 `subprocess_runner.py` 的 stdin/stdout 契約 → 改寫成讀 stdin JSON `{"input":...}`、寫 greeting 到 stdout）。新 bundle tar 結構 `['SKILL.md','scripts','scripts/main.py']`、sha `88fd964a8d82c8d6d9868b65868f9885091517c9c064d835f7b90cacd22ea8b3`。`src/migrations/18.0.1.0.33/post-migrate.py`（idempotent，比照 #135）在既有 noupdate DB 升級時在地換 bundle bytes+sha。**stg 實證**：deploy .33 → reap test4 cold-start → WS e2e `tool_result exit_code 0 / stdout="Hello, world!"`（原本 exit_code 2 file-not-found）、frame `tool_call→tool_result→text_chunk→done`。**餘下 minor**：因 MiniMax 空總結走 #14 fallback，使用者看到的是 raw subprocess JSON wrapper（`{"exit_code":0,"stdout":"b'Hello, world!'"...}`，stdout 還是 bytes repr），非 clean greeting —— 功能正確，前綴/格式化是日後 UX polish follow-up（#14 PR 已註明前綴留後續）。

**驗證方法（WS e2e，免 UI，stg）**：odoo shell `client.spawn_or_attach(project, channel_id=<int>, ...)` 取 ws_url+JWT → `websockets.connect(ws_url, extra_headers={'Authorization':'Bearer '+tok})` → send `user_message` envelope（照 `controllers/ai_assistant.py:763`，body=觸發訊息如「請使用 hello-world 技能跟我打招呼」）→ 收 frame。skill 正常應回 greeting 文字；現況回上述 error JSON。判斷 #14 是否 work 看 frame 序列：`tool_call→tool_result→text_chunk→done`（work）vs `...→error`（未修）。詳見 [[reference-agent-runtime-stg-verify]]、[[reference-agent-runtime-respawn-mechanism]]。
