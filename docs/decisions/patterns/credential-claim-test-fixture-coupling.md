# Pattern: credential / claim 硬化的 test-fixture forward-coupling

> 適用：你在**硬化一條共用的 credential / claim 路徑** —— 對 token 加一個 required claim（如 `token_use`、`channel_id`）、把原本 optional 的 secret / api_key 變成 required（fail-closed）、或收緊某個 verify。凡是**自建該 credential 走這條路徑的 test**（fixture 內 `_sign(...)` 自簽 token、`api_key=""` 預設、`setUp` 不設某 env），都與這次硬化 **forward-coupled** —— 它們必須 lock-step 補上新 claim / secret，否則該 test 會紅（好：暴露 coupling）或假綠（壞：若 test 繞過被硬化的 gate）。**硬化一條 auth 路徑，等於同時宣告「所有自建該 credential 的 test fixture 都要同步」**。補完 fixtures 後的 green 才是「coupling 已全 accounted」的訊號，不是「沒事」。

## When to use

當你的改動屬於下列任一，且 codebase（含跨 repo）有 test 自行構造該 credential：

1. **對 token 加 required claim** —— verifier 從「沒這 claim 也過」改成「缺 claim 即 reject」（如 F-PC-7 `token_use` A 嚴格、Phase B HC-2 require `channel_id`/`invoker_*`）。
2. **把 optional credential 變 required（fail-closed）** —— 如 F-PC-3 namespace secret 從「缺則用公開 fallback」改「缺即 `raise`」；F-PC-4 operator `api_key` 從「未設則 pass-through」改「未設即 503」。
3. **收緊 verify**（dual→single、加 allowlist…），使原本能過的自建 credential 不再過。

→ 三者的共同點：**被硬化的不是某個 caller，是「這條路徑接受什麼 credential」的契約**；所有自建 credential 餵進這條路徑的 test 都在契約變更的 blast radius 內。

## What it looks like

### 本 epic 的 3 個建立點（forward-coupling 先例）

| Phase | 硬化 | 自建 credential 的 test 受影響點 |
|-------|------|----------------------------------|
| **B** | HC-2 require `channel_id`/`invoker_*` claims | `paas-agent-runtime` WS test 的 `_sign(...)` helper 須補這些 claim，否則 auth-layer reject |
| **C** | F-PC-3 phase_c client/webhook fixtures | `test_agent_pod_phase_c_client_webhook` 自建 secret / 簽 token 的 fixture 須對齊新 verify |
| **D** | F-PC-4 fail-closed + F-PC-3 fail-closed | `e94991d`：~6 個 operator fixture（`api_key=""`→真 key 或改斷言 503）+ 3 個 Odoo session-creating test 的 `setUp`（補設 `AGENT_RUNTIME_NAMESPACE_SECRET` env，否則 `agent_session.create` fail-closed raise）|

### forward-coupling blast radius（Phase D F-PC-3 範例）

`agent_session.create()` 頂端**無條件**呼 `_get_namespace_secret()`（先於「是否真需算 hash」判斷）。F-PC-3 把它改 fail-closed 後，**任何** create `agent_session` 的 test —— 即使該 test 根本不關心 namespace hash —— 都會在 `setUp` 撞 fail-closed raise，除非補設 env。這就是 forward-coupling 的 blast radius：**硬化點的 fail-closed 沿著「誰會碰到這條路徑」擴散到看似無關的 test**（CAQA Phase D 評為 intentional「偏嚴」而非 bug）。

### 正確的反應 vs 危險的反應

- ✅ **fixture lock-step 補 claim/secret** —— 紅 → 補 fixture → 綠。綠代表 coupling 已 accounted。
- ✅ **改斷言**（如 F-PC-4：`api_key=""` 的 test 從「期望 pass」改「期望 503」）—— 斷言反映新契約。
- ❌ **在 test 裡繞過被硬化的 gate**（mock 掉 verify、給 test 開後門 skip fail-closed）—— 製造**假綠**：test 綠了但根本沒走真實硬化路徑，硬化的保護在 test 維度蒸發，未來回歸無守線。
- ❌ **只補到紅的那幾個 test 就收手** —— 漏掉跨 repo / 跨檔的自建點（如另一個 repo 的 `_sign` helper）→ 該處 false-green 或在別的 CI 才紅。

## Why this pattern

- **紅是訊號不是障礙**：硬化後 fixture 紅，正是「這條路徑的契約變了，且這個 test 真的走它」的證據。把紅當「煩」而 mock 掉 gate，等於把硬化的價值在 test 維度刪除。
- **假綠是真風險**：credential 硬化的 test 若繞過 gate，會綠得很安心 —— 但回歸時（有人不小心放寬 verify）沒有任何 test 變紅。`pr-test-analyzer` 對這類「fixture 是 mock-theater 還是真行為探針」要逐條判（Phase D R-3：F-PC-2 empty-PREVIOUS 不放寬 invariant code 對但**無獨立負例** → 未來 refactor 拿掉 guard 不會紅，就是這類缺口）。
- **跨 repo 更易漏**：自建 credential 的點可能散在不同 repo（Odoo 端 fixture + `paas-agent-runtime` 端 `_sign`）。硬化一端時，另一端的自建點不在同個 CI、不會在本 repo 紅 → 必須主動 grep 跨 repo 的自建點。
- **grep 自建點再硬化**：動手前先 grep credential 的所有構造點（`_sign(`、`api_key=`、相關 env 的 `setenv`/fixture），列出 blast radius，硬化與 fixture 更新同一個 PR/commit。Phase D 把 fixture 更新放 `e94991d`（test commit）與 fix `8d152f7` 同 cycle，就是這個紀律。

## See also

- 落地 case：`docs/tracking/agent-runtime-pod-phase-d-enable-gate-2026-05-25.md` §takeaway；impl `e94991d`（Phase D forward-coupling fixtures）；CAQA Phase D §Quality info（forward-coupling blast radius）。
- 觸發本 pattern 的硬化（同 epic）：[[constraint-input-trust-expansion]]（HC-2 / token_use claim 硬化 → 自建 token 的 test 受 coupling）。
- 同 family（test 工程品質）：
  - [[phase-versioned-test-assertion]] —— 姐妹 pattern：那條是「test 斷言內容鎖死 schema 快照」，本條是「硬化 credential → 自建該 credential 的 fixture lock-step」；都屬「test 本身要跟著演進，否則紅得 confusing 或假綠」。
  - [[tag-scoped-test-misses-full-u-failures]] —— 「驗證範圍不對 → 漏抓」；本 pattern 的假綠變體是「fixture 繞過 gate → 漏守硬化」。
- 上游：fail-closed 的 credential gate（`raise`/503）會沿「誰碰到這條路徑」擴散到看似無關的 test `setUp` —— 這是 fail-closed 的預期代價，不是 bug。
