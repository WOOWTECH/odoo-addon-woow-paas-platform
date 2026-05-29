---
name: agent-runtime-pod-phase-b-image-source-2026-05-23
phase: phase-b-completion
epic: agent-runtime-pod
team: agent-runtime-pod
reporter: agent-runtime-pod-reporter
created: 2026-05-23
status: completed
verdict: APPROVED WITH CAVEATS (CAQA Prism + reviewer Quorum，0 BLOCKER)
cross_repo:
  - paas-agent-runtime（image source repo；本 report 引用其 commit / file 以 repo 名 + repo-root 相對路徑標明）
related:
  - docs/plans/2026-Q2/agent-runtime-pod-design.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md
  - docs/tracking/pitfalls/ci-workflow-container-missing-node.md
  - docs/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md
  - docs/tracking/reviewer-report-2026-05-22-agent-runtime-pod-phase-a.md
  - docs/tracking/followups/follow-ups.md
  - "paas-agent-runtime repo：docs/PHASE_B_HANDOFF.md（Phase B → C/D carry-over canonical home）"
---

# Phase B Durable — agent-runtime pod image source（cross-repo epic 紀錄）

> 本 doc 是 `agent-runtime-pod` epic 的 **Phase B 完成 durable**，由 epic reporter 寫於 woow_paas_platform（epic durable home）。Phase B 的 code / test / CI / review 全在 **`paas-agent-runtime` repo**（與 woow_paas_platform 分離）；本 doc 不重複 PHASE_B_HANDOFF.md 的 carry-over 清單（那是 Phase B→C/D 的 canonical home），而是記錄 **cross-phase learnings**：CI gate 三度價值、HC-2 對 [[constraint-input-trust-expansion]] pattern 的 cross-phase 驗證、2 條 process learning。

## 1. Phase B 交付 summary

| 項目 | 內容 |
|------|------|
| **Repo** | `paas-agent-runtime`（新 repo；agent runtime pod image source）|
| **範圍** | per-project agent runtime pod 的 image 端：transport（WS/SSE）/ skills（bundle fetch + subprocess）/ state（ConfigMap hot reload）/ security（auth / SSRF / audit）/ build（Dockerfile + Gitea CI）|
| **Production** | 14 production module（5 subsystem，不含 5 個 `__init__.py`）|
| **Test** | 14 test 檔（13 `test_*` module + `conftest.py`）|
| **Commit** | 11 production/review commit（`1b844ef` scaffold → `bb5f7d5` SC-9 pin，不含 `6530110` empty init）|
| **PR** | paas-agent-runtime PR #1（CI green + merge 中）|
| **Verdict** | CAQA（Prism）+ reviewer（Quorum）皆 **APPROVED WITH CAVEATS，0 BLOCKER**；Inv A2 守住（pod 全 runtime 零直連 `ir.attachment`，grep 確認）|

### 5 subsystem 對應 design §

| Subsystem | Production module | design § |
|-----------|-------------------|----------|
| **transport** | `runtime/transport/{envelope,auth,ws_server}.py` | §5（WS envelope schema + HS256 auth + jti replay cache）|
| **skills** | `runtime/skills/{cache,loader,subprocess_runner}.py` | §3（lazy bundle fetch + tar.gz unpack + subprocess sandbox）|
| **state** | `runtime/state/{base,mcp,skill,template}_reloader.py` | §5.4（ConfigMap hot reload — WS push primary + disk re-read）|
| **security** | `runtime/security/{audit_logger,jti_cache,url_validator}.py` | §6（audit logging + jti replay defense + SSRF guard）|
| **runtime stub** | `runtime/langgraph_runtime.py` | §5.8（LangGraph runtime stub — glue phase 換 real impl）|

> **重要 scope 界定**：Phase B 的 `langgraph_runtime` 是 **stub**（`yield Event(type="done")`，無真 tool_set / 無真 LangGraph）。多個 review finding「Phase B 不爆」的原因都是「stub，Odoo 未接」— 後續 glue phase 接 real LangGraph 後才 active。詳見 PHASE_B_HANDOFF.md §SC-4 / §SC-5。

## 2. 2-round review 結果（CAQA Prism + reviewer Quorum）

Phase B review 採雙 reviewer 角色：**CAQA Prism**（多 lens 切角 — 含用既有 pattern doc 當 lens）+ **reviewer Quorum**（多 agent 投票）。兩者 verdict 一致 APPROVED WITH CAVEATS，0 BLOCKER。

### 4 條 high-severity finding 全 round 2 內 resolved（非 carry-over）

| Finding | 內容 | Resolution | Commit |
|---------|------|-----------|--------|
| **HC-1** | `url_validator` 對 decimal / hex / octal IP literal 形式的 SSRF bypass（如 `http://2130706433/` = `127.0.0.1`）| 補 IP-literal 正規化後再比對 — 各種 integer / hex / octal 形式都先 canonical 化 | `726b5b3`（B-2 Anvil）|
| **HC-2** | `ws_server` audit 歸因 + thread scope + role 授權 取 **client-controlled payload** 而非 **signed token claims** → repudiation + 提權 surface | claims authoritative — security 決策一律取已驗證 token claims（§5.3 權威），payload 同名欄僅作 LangGraph input | `f848ea8` + `ca57abd`（B-1 Conduit，planner ruling 4e65966）|
| **subprocess rlimit real-trigger test** | subprocess sandbox 的 rlimit（FSIZE / NOFILE / NPROC / AS）只有「設了」沒「真的擋」的 test | 補 real-trigger test（Linux-gated；macOS dev box 跳過 AS cap）| `726b5b3`（B-2 Anvil）|
| **SC-7（升 round 2 處理）** | `loader._TOOL_CACHE` key 不含 invoker_role → role 混淆風險 | role-scope tool cache（cache key 含 role）；HC-2 收斂後（claims authoritative）role wiring 留 glue phase | `726b5b3` |

### Carry-over SC（Phase B 範圍外，→ Phase C/D；canonical home = PHASE_B_HANDOFF.md）

不在本 doc 重列每條（避免與 PHASE_B_HANDOFF.md 雙寫漂移）。cluster 摘要：

- **SC-4 / SC-5**（reloader tool_set 失效 hook 缺 / stub 雙 done）— glue phase 接 real LangGraph 時必補；契約已由 planner 裁定（ws_server 是 done 唯一 producer）
- **SC-1 / SC-3 / SC-6 / SC-8 / SC-9** + audit_logger emit guard + base_reloader snapshot live dict — follow-up，不阻塞 Phase B
- 詳見 `paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md` §1 / §4

## 3. CI gate 三度發揮價值（cross-phase learning ★）

Phase B 最值得 durable 的 learning：**CI gate 在 hand-off 後三度抓到「signed-off 內容其實有問題」**，每次都比「人肉 hand-off 信任」更早爆。三次性質遞進（doc stale → doc 自相矛盾 → CI infra 本身壞）：

| # | CI gate 抓到 | 性質 | 修法 | Commit / repo |
|---|------------|------|------|--------------|
| **1** | WS envelope frozen count **12 vs 15** | signed-off schema 數字 stale — design §5.2 envelope frozen count 簽過 12，實際 schema 已長到 15 | doc fix 對齊實際 schema | `e74788d`（woow_paas_platform）|
| **2** | skill cache path **§3 vs §6** | 兩個 signed-off section 互相矛盾 — §3 寫的 cache path 與 §6.2 不一致 | doc fix 補 `cache/` 層對齊 §6.2 | `7b30ca6`（woow_paas_platform）|
| **3** | **checkout node-in-container CI infra bug** | CI workflow 本身從沒成功跑過 pytest — `container: python:3.11` + `actions/checkout@v4`（JS action 需 node）→ exit 127 → 後續 step 全 skip → pytest 從沒跑到 | runner host 跑 + `setup-python`（checkout 留 host）| `5b2f9ed`（paas-agent-runtime；B-5）|

**為什麼這是 generalizable learning（非單次運氣）**：

- 前兩次（envelope / cache path）證明 **「signed-off ≠ 正確」** — design doc 簽核當下對，但 schema / section 後續漂移，CI 數字一致性 check 是唯一持續守線的東西。人肉 hand-off 會信任「簽過了」。
- 第三次（checkout node）證明 **「CI green ≠ CI 真的在驗」** — 若沒人發現 exit 127 後 step 全 skip，CI 會顯示「沒失敗」而 pytest 從沒跑到（false green）。這直接導出 §5 的 process learning。
- 三次的共同根：**自動 gate 比人的記憶 / 信任更早、更冷靜地抓到 drift**。Phase A reporter durable 已經把 pattern / invariant 寫進 design doc + CLAUDE.md，讓 CI / reviewer 有 lens 可用；Phase B 兌現了這個投資。

第 3 個（checkout node-in-container）已抽成獨立 pitfall：[[ci-workflow-container-missing-node]]。

## 4. HC-2 = `constraint-input-trust-expansion` pattern 的 image 端同型（cross-phase 驗證 ★）

Phase A reporter phase 抽的 [[constraint-input-trust-expansion]] pattern，在 Phase B image 端 **以完全不同的技術 surface 重現** — 證明 pattern 不是綁 Odoo ORM 的 single instance，而是 cross-stack 的安全反模式。

### 同型對照

| 維度 | Phase A（Odoo ORM）HC-3 | Phase B（image WS transport）HC-2 |
|------|------------------------|----------------------------------|
| **Surface** | `agent_template.project_ids` Many2many write | `ws_server` WS envelope handler |
| **Attacker-controlled input** | m2m field 內 element（attacker 加 `[(4, victim)]`）| WS envelope `payload`（client 自填 body）|
| **被誤信的推導** | ir.rule `'any'` OR-semantics 對 m2m 評估「任一 element 滿足即放行」| audit 歸因 / thread scope / role 授權 直接取 `payload` 同名欄 |
| **攻擊後果** | cross-project template binding 注入 → Phase D system_prompt / mcp hijack | repudiation（偽造 audit 歸因）+ 提權（偽造 invoker_role）|
| **Authoritative source（修法）** | Python `@api.constrains` per-element grantor authority check | signed token `claims`（§5.3 權威，HS256 verified）|
| **核心抽象** | **security 決策信任 attacker-controllable input → 必改用 authoritative source 重新推導** | 同 |

### Pattern lens 兌現

- **CAQA Prism 用 [[constraint-input-trust-expansion]] pattern 當 lens 抓到 HC-2** — 不是巧合發現，是拿 Phase A 抽的 pattern 主動 scan image 端「哪裡用 client input 做 security 決策」。
- 這驗證 reporter durable 的真正價值：pattern doc 不只是「記錄過去」，而是「給下個 phase / 下個 reviewer 一個可複用的 attack lens」。Phase A 投資的 pattern doc 在 Phase B 兌現成「主動抓 bug 的工具」。

### Fix（已收斂）

`ws_server.py` 明文鎖死契約（comment 引用 HC-2 ruling）：

```python
# HC-2 ruling：security 決策（audit 歸因 + thread scope + role 授權）一律取已驗證的
# token claims（§5.3 權威），不取 client-controlled payload（防 repudiation / 提權）。
# payload 同名欄僅作 LangGraph input，NOT security decision。
channel_id = claims["channel_id"]            # claims，非 payload
invoker_user_id = claims["invoker_user_id"]  # claims，非 payload
invoker_role = claims["invoker_role"]        # claims，非 payload（role 授權；wiring 留 glue phase）
```

- `ca57abd` 進一步在 **auth layer** require `channel_id` / `invoker_user_id` / `invoker_role` claims（fail-closed — 缺 claim 在 verify_token 階段就拒，不留 fallback 給 payload）。
- planner commit `4e65966`（design §5.3）裁定 claims authoritative 為設計層契約。
- **carry-over（SC-7）**：`loader._TOOL_CACHE` key 需含 `invoker_role`（role-authz wiring 在 glue phase 補，否則 role 混淆）— 見 PHASE_B_HANDOFF.md §2。

> **建議**：[[constraint-input-trust-expansion]] pattern doc 的 "See also / 真實落地 case" 已可加一行 cross-phase 驗證指向本 report（Phase B HC-2 同型 instance）。reporter 已於本 cycle 補（見 §7）。

## 5. 2 條 process learning（durable）

### Learning 1 — CI workflow 本身要先驗「能跑」，不只驗 pytest pass

B-5（checkout node-in-container）的根：implementer 用 `container: python:3.11` 跑 pytest，**只想到「pytest 需要 python」，沒想到「actions/checkout@v4 是 JS action 需要 node」**。slim language image 沒 node → checkout exit 127 → 後續 step 全 skip → CI 顯示沒失敗（false green）而 pytest 從沒跑到。

- **可行做法**：新 CI workflow 第一次跑，必須確認「pytest 真的有執行 + 有 collect 到 test」（看 log 有 `collected N items` / `N passed`），不是只看 workflow 結論 green。
- **更強守線**：CI 加一個「assert pytest 真的跑了」的 sentinel（如 `pytest tests/ | tee out && grep -q "passed" out`），讓 false green 變紅。
- 已抽 pitfall：[[ci-workflow-container-missing-node]]。

### Learning 2 — 三層 green ≠ CI green（只有真 CI 是 authoritative）

Phase B test 經過三層環境，**只有最後一層是 authoritative gate**：

| 層 | 環境 | 為什麼 **不** authoritative |
|----|------|---------------------------|
| 本機 macOS | dev box | `subprocess_runner.py` `if sys.platform != "darwin"` 跳過 RLIMIT_AS — macOS 根本沒跑 address-space cap test |
| arm64 container | local docker | 架構與 prod（Linux x86）不同；rlimit / tarfile filter 行為可能差 |
| **真 CI（Linux + setup-python）** | Gitea runner | **唯一 authoritative** — prod pod 也是 Linux，rlimit 5 條全 enforce |

- **可行做法**：聲稱「測過了」必須說清楚「哪一層測過」。本機 / container 綠不代表 CI 綠（B-5 正是本機 / container 可能綠但真 CI 連 pytest 都沒跑到）。
- **對齊 `verification-before-completion` 精神**（superpowers skill）：authoritative gate（真 CI）的 output 才算數，不是 dev box 的綠。
- RLIMIT_AS dev/prod 差異 leader 已接受（dev 非信任邊界，不跑 untrusted skill）— 見 PHASE_B_HANDOFF.md §3。

## 6. cross-phase invariant 守線

| Invariant | Phase B 守線 | Evidence |
|-----------|------------|----------|
| **Inv A2**（[[attachment-row-level-acl-and-anchor-stamping]]）| ✓ pod 全 runtime **零直連 `ir.attachment`** | grep 確認；`cache.py` 透過 Odoo HTTP endpoint fetch bundle，依賴 woow_paas_platform 側 anchor stamping（`res_model='woow_paas_platform.skill'` + `public=False`）+ row-level ACL — 跨 repo 連動見 §F-PA-15 / F-PA-16 |
| **Inv A1**（[[constraint-input-trust-expansion]]）| ✓ image 端同型（HC-2）已收斂 claims authoritative | §4 |
| design §5.3 token claims 權威 | ✓ ws_server + auth layer 雙重 enforce | `f848ea8` + `ca57abd` |
| design §6.4 subprocess bytes stdout/stderr | ✓ impl 修 design pseudocode `stdin=PIPE` + `input=` 的 CPython `ValueError` | PHASE_B_HANDOFF.md §5 |

> **跨 repo 連動風險（F-PA-15 / F-PA-16 提醒兌現窗口）**：`paas-agent-runtime` 的 `cache.py` 假設 woow_paas_platform 側已落實 attachment anchor stamping + row-level ACL（Phase A Inv A2 / HC-4 hotfix）。Phase C wiring 前必須確認 woow_paas_platform 的 F-PA-15（controller sudo path lock-step）/ F-PA-16（anchor destructive overwrite doc）狀態 — 見 [[follow-ups]] §Cross-cutting。

## 7. reporter phase 本 cycle 落地

- **本 report**：`docs/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md`（Phase B 完成 durable + cross-phase learnings）
- **新 pitfall**：`docs/tracking/pitfalls/ci-workflow-container-missing-node.md`（B-5 CI infra bug 抽 pitfall）
- **follow-ups cross-ref**：`docs/tracking/followups/follow-ups.md` 加 Phase B carry-over cross-ref 指向 paas-agent-runtime `docs/PHASE_B_HANDOFF.md`
- **CLAUDE.md**：評估 2 process learning 是否進 Known patterns（見 §5 判斷）
- **既有 pattern doc cross-phase 驗證**：[[constraint-input-trust-expansion]] 已可在 "See also" 引用本 report 作 Phase B HC-2 同型 instance

## See also

- Epic 主文件：
  - `docs/plans/2026-Q2/agent-runtime-pod-design.md` §1.3.2 Inv A1 + Inv A2（Phase A #8）/ §5（transport）/ §6（security）/ §3（skills）
  - `docs/decisions/reviews/pr-description-draft-agent-runtime-pod-phase-a.md` — Phase A PR description
- Phase A durable（同 epic 前一階段）：
  - `docs/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md`
  - `docs/tracking/reviewer-report-2026-05-22-agent-runtime-pod-phase-a.md`
- Pattern doc（cross-phase 兌現）：
  - [[constraint-input-trust-expansion]] — Phase B HC-2 是其 image 端同型 instance（§4）
  - [[attachment-row-level-acl-and-anchor-stamping]] — Inv A2，Phase B pod 零直連 ir.attachment 守線（§6）
- Pitfall（Phase B 抽）：
  - [[ci-workflow-container-missing-node]] — B-5 CI infra bug
- Carry-over canonical home（cross-repo）：
  - `paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md` — Phase B → C/D carry-over（SC-1~SC-9 + wiring assumptions + design 回標）
- Follow-up 追蹤：
  - `docs/tracking/followups/follow-ups.md` §Cycle 5（F-PA-1~F-PA-16）+ Phase B cross-ref
