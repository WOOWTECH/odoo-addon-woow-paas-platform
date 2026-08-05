# project ar26 sidecar selfcheck landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_ar26_sidecar_selfcheck_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："ar#26 收官——sidecar 憑證隔離驗證＋實況層自檢；label 驗版陷阱、k8s service-link 是誤殺最大面、exit code 當鑑別項"

**2026-08-02 收官**。ar#26（untrusted skill bash 與平台憑證同 pod 同 uid）close，兩軌 PASS。
交付：ar PR#32（bridge daemon 啟動自檢）→ image **1.4.4** → cd#89（stg imageTag）＋cd#90（註解）
→ charts#67（chart 0.4.5）＋charts#68（sidecar env 封閉性斷言，closes charts#65）。
衍生：ar#33（kill-1 DoS＋loopback）、ar#34（prompt injection provenance）、charts#66（egress）、cd#88（hygiene）。

**前提大幅過時**：三項最小交付（env-scrub / 異 UID / 祕密檔不可讀）開工前**早已在
runtime＋image＋chart 三方落地、stg+prod 皆啟用**，charts 側連 CI 斷言都有（含 negative test）。
本次真正做的是**驗證**與**實況層守線**。→ 接手 issue 先查現況，issue 本文可能落後好幾個月。

## 三個可重用的技術發現

1. **`app.kubernetes.io/version` label 不可用來驗版**（會得到相反結論）：新 pod 的 label 仍是
   舊版號，因為 chart appVersion 沒 bump 而 image 是 operator `imageTag` 覆寫上去的。
   **只有 `image` / `imageID` 是權威**。
2. **k8s service-link 注入是 env allowlist 誤殺風險最大的一面**：sidecar 實測 **32 個 env 鍵，
   16 個是 k8s 注入**（`KUBERNETES_*` 8＋`<SVC>_*` 8）——**只有真 pod 才有、本機 docker 測不到**。
   任何「本機驗過 allowlist」都不足以推論到叢集。另有 base image 自帶的
   `GPG_KEY`/`PYTHON_VERSION`/`PYTHON_SHA256`（chart 也看不到）。
3. **用「非零且非固定的 exit code」當鑑別項**：拒服務路徑固定回 `exit_code=-1`，所以送
   `echo x; exit 42` **拿得回 42** 就證明真的走到 `spawn_bash`——比「沒看到 BRIDGE_INVARIANT_VIOLATED
   字串」強得多（後者相容於「探針根本沒跑到」）。這是陽性對照的精緻形式，見
   [[feedback_negative_verification_needs_positive_control]]。

## 新長出的跨 repo lockstep（SOP 未涵蓋）

image 的 `_ALLOWED_KEYS` 與 **chart 的 sidecar env 鍵集＋`LANG` 值**綁死。違反時 daemon
**拒所有 exec 但保留 ping** ⇒ liveness 照過、不 crashloop、**pod 狀態完全看不出來**，
而 `bash_execute` 全滅。charts#68 的封閉性斷言即為此而設（並涵蓋 `envFrom` 後門——
整包注入的鍵在 `.env[].name` 看不到，少了它整條斷言恆綠）。

`LANG` 是唯一「移除反而多一個鍵」的反直覺路徑：實測 `env -u LANG` → 注入 `LC_CTYPE`
（CPython PEP 538 locale coercion）；`C.UTF-8` 不觸發、`en_US.UTF-8` 會。目前不誤殺純因
runtime allowlist 有 `LC_*` prefix 放行——**那條 prefix 被收掉時，移除 LANG 就從無害變成 bash 全滅**。

## 流程面

- **不必等 LLM 就能驗 bash 隔離**：`bash_execute` 只是 `bash_bridge.call_bridge` 的薄包裝
  （`tools.py`），從 agent container 直接呼叫 `call_bridge` 證據等價；LLM 只是觸發器、不是安全邊界。
  stg MiniMax tunnel 掛掉（530/1033）時靠這條繞過。
- **空 skill_snapshot ⇒ `tools=0`**，bash tool 根本沒註冊 ⇒ **工具註冊層假綠**（「沒讀到」其實是
  「工具沒暴露」）。
- **cd repo 慣例是不刪已 merge 的 remote 分支**（實查 49 支、抽樣 12 支全已 merge），
  見 [[feedback_maestri_dispatch_verify_premise_first]]。

相關：[[project_ar26_tracka_stg_sidecar_isolation]]、[[feedback_comment_env_state_claims_expire]]、
[[reference_ci_rebuild_same_commit_identical_digest]]（agent-runtime 非 reproducible 的反例）。
