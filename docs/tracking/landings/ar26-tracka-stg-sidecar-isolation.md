# project ar26 tracka stg sidecar isolation

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_ar26_tracka_stg_sidecar_isolation`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："ar#26 軌A stg 實機驗證 PASS——untrusted skill bash 拿不到平台憑證；含 bash_execute 驗證的正確打法與 tools=0 假綠坑"

2026-08-02 完成 `woow-paas/paas-agent-runtime#26` 軌A（上線前 gate）：stg 開拋棄式 project 38 spawn 全新 agent pod，驗 untrusted skill bash（sidecar uid 10002）**拿不到** LLM_API_KEY / SKILL_BUNDLE_TOKEN / SHARED_SECRET / LLM_MODEL_CONFIG、掃不到 agent 主行程、讀不到 auth 簽章祕密檔。**三項全 PASS**，驗完 project／namespace／PVC 全拆乾淨。

**How to apply**（下次驗同類隔離直接照用）：

- **不必等 LLM。** `bash_execute` tool 的內層 `_run` 只是 `bash_bridge.call_bridge(cmd, _default_bash_cwd())` 的薄包裝（`runtime/skills/tools.py`）。從 **agent container（uid 10001）** `kubectl exec` 呼叫 `call_bridge`，走的是同 socket／同 daemon／同 `spawn_bash`／同 uid 10002／同 PID ns，證據強度等價。LLM 只是觸發器，不是安全邊界。
  - 界線要寫進報告：exec 進 **agent** container 呼叫 call_bridge＝站在 tool 實際執行的位置（✅）；exec 進 **skill-runner** container 直接下指令＝站在沙箱裡面、繞過 bridge（❌）。指令是否真落在 uid 10002 用 `id` 自證。
  - 未驗的 LLM 觸發層要誠實標明屬功能性非隔離性，別讓報告看起來全鏈都驗過。

- **🔴 `bash_execute` 只在「有生效 skill」時才註冊**（`_resolve_tool_set`：`if _snapshot_skill_entries(...)` 才 append）。用空 `skill_snapshot` spawn → log `LLM astream start (tools=0)` → 就算全鏈活著也驗不出東西＝**假綠發生在工具註冊層**。spawn 必須照 controller 用 `agent_session._build_skill_snapshot(proj, tpl)`，log 應為 `tools=2`（load_skill + bash_execute）。

- **雙層 env 防護，兩層數字不同**：sidecar **container** env 7 鍵（kubectl 可驗）；bash **子行程** env 只有 4 鍵（`subprocess_runner._safe_env` 硬編 allowlist `PATH`/`HOME=/tmp`/`LANG`/`PROJECT_HASH`）。

- **PID ns 隔離雙向可證**：sandbox 內 `total=5` 個 PID、PID 1＝`python -m runtime.skills.bash_bridge`；agent container 內 PID 1＝`uvicorn runtime.transport.ws_server:app`。兩邊互不相見。

- **auth secret 的機制是「不存在」不是「拒絕」**：`/run` 只有 `lock` 與 `skill-bridge`，`/run/secrets` 整個不存在（ls exit 2 / No such file or directory）。與 pod spec「skill-runner 未掛 agent-auth volume」獨立互證。

- **非 finding**：`/run/skill-bridge/sock/token`（48 hex、0640 skillrunner:skillbridge）sandbox 讀得到，但那是 sidecar daemon 自己 `os.urandom(24).hex()` 產的 agent→sidecar handshake token，沙箱本來就是執行方，讀到不增加能力。

**環境卡點紀錄**：stg 唯一有 key 的 `ai.config` 是 MiniMax（`https://eugene-minimax.woowtech.io/v1`），該日 CF Tunnel 回 530/1033（公網同樣掛，非叢集問題），host 不在可操作 ns 內。WS 入向只有 6 種 type、**無直接 tool-invoke envelope**，所以 LLM 死＝真實 tool 路徑打不通——這正是改走 call_bridge 的原因。

---

## 續章：1.4.4 bridge 啟動自檢「不誤殺」驗證（同日，軌B 收尾）

`sandbox_invariants.check_invariants()` 在 daemon 啟動時量測三項（env allowlist／PID ns／auth secret 不可讀），違反則**拒所有 exec 但不 crashloop**（sidecar 無 readinessProbe，掛掉會拖垮整顆 pod）⇒ **從 pod 狀態完全看不出來**，只能主動驗。結果 PASS。

- **🔴 pod label 會讓你驗出相反結論**：`app.kubernetes.io/version` 停在 chart 的 appVersion（1.4.2），實際 image 是 operator `AGENT_RUNTIME_IMAGE_TAG` 覆寫上去的 1.4.4。**只有 `.status.containerStatuses[].image` / `imageID` 是權威**（同 [[feedback_rollout_workorder_verify_baseline_live]] 的「image 認 pod digest」）。
- **最有力的鑑別項是 exit code 透傳**：拒服務在 `handle_request` 早退、固定回 `exit_code=-1`，所以送 `echo x; exit 42` **拿得回 42** 就證明真的走到 `spawn_bash`——比「沒看到 `BRIDGE_INVARIANT_VIOLATED` 字串」這種否定性推論強。
- **兩側獨立互證**：daemon 自己有 per-exec log（`bash exec: exit_code=… cmd_chars=… cwd=…`，1.4.1 起）。筆數要等於送出的 case 數，且它記的 exit_code 要和收到的回傳一致。
- **誤殺風險面在 k8s service-link 注入**：sidecar env 實測 32 鍵，其中 **16 鍵是 service-link 自動注入**（`<SVC>_SERVICE_HOST/PORT/PORT_<NAME>`、`KUBERNETES_*`）——chart 看不到、本機 docker 也測不到，只有真 pod 有。chart 宣告的 sidecar env 已被 charts CI 封閉性斷言鎖住（charts PR#68），所以真誤殺時**歸因直接跳過 chart**，查 base image 自帶鍵或 service-link 那側。
- allowlist 用 prefix（`PYTHON*`／`LC_*`）而非逐一列舉，防 base image 升版新增一鍵就誤殺；`LC_*` 是因為 CPython PEP 538 會在 `LANG` 指向 image 沒有的 locale 時自行塞 `LC_CTYPE`。

探針設計的坑另存 [[feedback_negative_verification_needs_positive_control]]。相關：[[reference_agent_runtime_stg_verify]]、[[reference_agent_runtime_respawn_mechanism]]
