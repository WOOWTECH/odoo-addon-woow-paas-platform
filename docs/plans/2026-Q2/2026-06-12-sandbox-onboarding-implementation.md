# k8s-dev-sandbox Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推薦）或 superpowers:executing-plans 逐 task 執行。步驟用 checkbox（`- [ ]`）追蹤。

**Goal:** 為 `/woowprd:k8s-dev-sandbox` 補上三層設定模型的第③層（user-scope 執行期供值）：新增 `onboard` 子命令、`~/.sandbox/{project-key}/config.env` profile、`.sandbox.yml` 的 `env_required`/`env_optional`/`config_params(_{mode})` 宣告區塊，讓任意 addon repo 的任意 worktree 零互動開沙盒跑 e2e。

**Architecture:** repo 宣告需求（`.sandbox.yml`，committed）、user 供值一次（profile，私有）、worktree 自動繼承（project-key 由 remote URL / git-common-dir **物理路徑**推導）。解析優先序固定一條鏈：CLI flag > 當下 env > profile > `.sandbox.yml` > 自動偵測。profile 載入採**兩段式**：字面值先套、`!command` 延後到 `require_kube_context` 切好 context 才求值（防跨 cluster 誤讀）。`create.sh` 對 `env_required` 缺值雙段 fail-fast、對 `config_params` 做 diff-aware desired-state 收斂（fresh + upgrade 路徑，失敗 fail-loud）。

**Tech Stack:** Bash（**須相容 macOS /bin/bash 3.2**：禁 `declare -A`、禁 nameref、空 array 用 newline-string 取代、**單引號跳脫禁 `${v//\'/\'\'}` idiom**——3.2 會保留反斜線，一律用 `local q="'"; ${v//$q/$q$q}`）、BSD awk、kubectl/psql、Claude Code plugin。

**Spec:** `docs/plans/2026-Q2/2026-06-12-sandbox-onboarding-design.md`

**Branch:** woow repo 在 `feat/sandbox-onboarding`；plugin repo（`~/.claude/plugins-local/woowprd`）直接 commit `master`（沿用該 repo 既有慣例）。

> **驗證修訂記錄（2026-06-12）：** 本 plan 經 4-lens 對抗式驗證 workflow（錨點 / bash 正確性 /
> parser 沙盒實跑 / 設計一致性；session workflow `wf_929b3c1a-7e9`）實跑查核後修訂。
> 已併入：4 blockers（`pwd -P` 物理路徑、onboard 未宣告 key 靜默丟棄、`!command` 兩段式
> 延後求值、（與 #1 同根））+ 6 majors（revision 驗證法、3.2 引號跳脫（含既有 `yaml_sq`
> 與 woow `pre-install.sh` 同病灶）、/dev/tty prompt、config_params 失敗 fail-loud、
> onboard 拒擷取 ambient KUBE_CONTEXT、deploy-stg 行為差異補記）+ 22 minors。

---

## 前置事實（grounding 已查證）

- plugin `common.sh` 現況：`load_sandbox_yml`（`:246-350`）只解析 layout / module / db_prefix / post_create_hook / install_modules / extra_addons；頂層 key regex `^([a-zA-Z_][a-zA-Z0-9_]*):` **錨定行首**（縮排行不匹配）；list 項 regex `^[[:space:]]+-`。**無任何 profile / `.env` 載入。**
- `create.sh` 現況順序：arg 解析（`:52-98`）→ 先決條件 + `require_kube_context`（`:100-127`）→ `resolve_repo_root` + `load_sandbox_yml`（`:130-131`）→ … → DB init gate `MODULE_STATE != "installed"`（`:424`）內含 post-create hook（`:458-470`）與 restart（`:474-476`）；**else 分支（`:477-479`）什麼都不跑**。
- `require_kube_context`（common.sh:355-376）有副作用：`kubectl config use-context` 切**全域** current-context。任何 `!command` 求值必須在它之後（驗證 blocker：否則打到 ambient context，可能是 woow-k3s）。
- `destroy.sh` 完全不 source profile → D6（destroy 不吃 profile 的 KUBE_CONTEXT）**零改動達成**。
- 本 Floor 實測 `git rev-parse --git-common-dir` 回 `.git`（相對路徑）→ Floor 是**獨立 clone 非 worktree**；worktree 中該命令回主 repo `.git` 的 **realpath（物理）絕對路徑** → 正規化必須統一用 `pwd -P`，否則 macOS `/tmp`、`/var`（→ `/private/...` symlink）下 worktree 與主 repo 算出不同 key（驗證 blocker，已實證 `pwd -P` 修復）。
- `${v//\'/\'\'}` 置換在 /bin/bash 3.2 **保留反斜線**（`it's` → `it\'\'s`，SQL 寫錯資料）；bash 4.3+ 才正確。既有 `common.sh yaml_sq`（:394-398）與 woow `.sandbox/pre-install.sh yaml_sq`（:24）同病灶——**現行 common_test.sh 用 /bin/bash 跑本來就 1 FAILED**，本機 PATH 的 Homebrew bash 5.3 遮住了。
- `deploy-stg.sh`：`init_odoo_system_params` 定義於 `:175-213`（含說明註解），呼叫於 `:264`（--image 捷徑）與 `:388`（主路徑）；`:230-234` 有 AGENT_RUNTIME_NAMESPACE_SECRET 未設的 Note（profile 上線後文案會與事實相反，要改）。
- woow repo `.sandbox.yml` 現有 key：layout / module / install_modules / db_prefix / post_create_hook。
- `common_test.sh` 現況 90 行、`# --- yaml_sq ---` 段在 `:86`、ALL PASS 判定 `:89-90`。
- 本機**未裝 shellcheck**：驗證步驟以 `bash -n`（/bin/bash 3.2 + Homebrew bash 雙跑）為必跑；shellcheck 標 optional（裝了才跑）。
- parser 既有行為：`line="${line%%#*}"` 先剝行內註解 → **`config_params` 值不可含 `#`**（限制寫進 SKILL.md，parser 升級列 follow-up）。

---

## File Structure

| File | 責任 | 動作 |
|------|------|------|
| plugin `scripts/lib/common.sh` | 新增 (g) `compute_project_key`、(h) `sandbox_profile_file`、(i) `load_profile`（兩段式）+ `_profile_key_denied` + `eval_profile_commands` + `profile_pending_has`、(j) `require_declared_env`（`--pre`/final 雙段）、(k) `merged_config_params`、(l) `render_config_value`；`load_sandbox_yml` 擴充三個宣告區塊；**修既有 `yaml_sq` 的 3.2 bug** | Modify |
| plugin `scripts/lib/common_test.sh` | 新函式 + parser 擴充的單測（無 cluster 依賴；**顯式 `/bin/bash` 跑**） | Modify（append） |
| plugin `scripts/create.sh` | 重排（repo→profile 字面→yml→pre fail-fast→context→`!cmd` 求值→final 檢查）、`apply_config_params`（fail-loud + 漂移收斂訊息）、結尾失敗 gate | Modify |
| plugin `scripts/onboard.sh` | 新子命令：宣告讀取 → 收集（`--set`/`/dev/tty` prompt/`--check`）→ 未宣告 `--set` key 也寫入 → 拒擷取 ambient KUBE_CONTEXT → 寫 profile → dry-run 驗證 | Create |
| plugin `SKILL.md` | onboard 子命令、三層模型、優先序、`!cmd` 求值時機、desired-state 語義、文法限制 | Modify |
| woow `.sandbox.yml` | 補 `env_optional`（desc 註明僅 stg 生效）+ `config_params_stg` | Modify |
| woow `.sandbox/pre-install.sh` | 修 `yaml_sq` 3.2 bug（同 idiom） | Modify |
| woow `.claude/skills/deploy-stg/scripts/deploy-stg.sh` | 刪 `init_odoo_system_params` + 兩呼叫點；改 `:230-234` Note 文案 | Modify |
| woow `CLAUDE.md` | K8s Dev Sandbox 段補 onboard 一句話 | Modify |

**驗證手段：** ① 純函式 → `/bin/bash scripts/lib/common_test.sh`（**顯式系統 bash**，再用 Homebrew bash 跑一次）；② script → `bash -n` 雙版本（shellcheck optional）；③ 端到端 → docker-desktop fixture repo 走 fail-fast → onboard → up → 驗 param → 改值重 up → down（Phase 6，全部 kubectl 顯式 `--context docker-desktop`）。

---

## Phase 1：`lib/common.sh` 第③層基礎（TDD）

### Task 1.1：先寫失敗測試

**Files:**
- Modify: `~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/scripts/lib/common_test.sh`

- [ ] **Step 1：把既有 trap 行（`:23`）改為涵蓋 KTMP**

舊：`TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT`
新：`TMP="$(mktemp -d)"; KTMP="$(mktemp -d)"; trap 'rm -rf "$TMP" "$KTMP"' EXIT`

- [ ] **Step 2：在 `# --- yaml_sq ---` 段（`:86`）之前插入以下測試區塊**

（注意：斷言對可能未定義的 SBX_* 一律 `${VAR:-}`，避免 RED 階段 `set -u` 提前中止整個套件。）

```bash
# --- compute_project_key（worktree / clone / 無 remote 收斂；物理路徑） ----------
mkdir -p "$KTMP/clone1" "$KTMP/clone2"
git -C "$KTMP/clone1" init -q && git -C "$KTMP/clone1" remote add origin "https://git-prod.woowtech.io/woow-paas/some_repo.git"
git -C "$KTMP/clone2" init -q && git -C "$KTMP/clone2" remote add origin "git@git-prod.woowtech.io:woow-paas/some_repo.git"
k1="$(compute_project_key "$KTMP/clone1")"; k2="$(compute_project_key "$KTMP/clone2")"
assert_eq "project-key clone https==scp" "$k1" "$k2"
assert_eq "project-key name part" "${k1%-*}" "some_repo"
# 無 remote：同 repo 的 worktree → 同 key（mktemp 在 /var symlink 下，逼出 pwd -P 需求）
mkdir -p "$KTMP/norem" && git -C "$KTMP/norem" init -q -b main
( cd "$KTMP/norem" && git commit -q --allow-empty -m x && git worktree add -q "$KTMP/norem-wt" -b wt )
k3="$(compute_project_key "$KTMP/norem")"; k4="$(compute_project_key "$KTMP/norem-wt")"
assert_eq "project-key worktree shares" "$k3" "$k4"
k5="$(compute_project_key "$KTMP/clone1")"
if [[ "$k3" == "$k5" ]]; then echo "FAIL: distinct repos same key"; fails=$((fails+1)); else echo "ok: distinct repos distinct keys"; fi

# --- load_profile（env 先贏 / !cmd 延後 / deny-list / KUBE_CONTEXT literal-only） --
export SANDBOX_HOME="$KTMP/sbxhome"
PKEY="$(compute_project_key "$KTMP/clone1")"
mkdir -p "$SANDBOX_HOME/$PKEY"
cat > "$SANDBOX_HOME/$PKEY/config.env" <<'ENV'
# comment line
PF_PLAIN=hello
PF_PRESET=from_profile
PF_CMD=!printf cmd_value
PF_BADCMD=!false
IMAGE_TAG=evil-injection
ENV
unset PF_PLAIN PF_CMD PF_BADCMD IMAGE_TAG 2>/dev/null || true
export PF_PRESET=from_env
load_profile "$KTMP/clone1" >/dev/null 2>&1
assert_eq "profile plain"        "${PF_PLAIN:-}"  "hello"
assert_eq "profile env wins"     "${PF_PRESET:-}" "from_env"
assert_eq "profile deny-list"    "${IMAGE_TAG:-<unset>}" "<unset>"
assert_eq "profile !cmd deferred (not yet)" "${PF_CMD:-<unset>}" "<unset>"
if profile_pending_has PF_CMD; then echo "ok: pending has PF_CMD"; else echo "FAIL: PF_CMD not pending"; fails=$((fails+1)); fi
eval_profile_commands >/dev/null 2>&1
assert_eq "profile !cmd eval after" "${PF_CMD:-}" "cmd_value"
assert_eq "profile !cmd fail -> unset" "${PF_BADCMD:-<unset>}" "<unset>"
# KUBE_CONTEXT=!... → load_profile fail-loud
printf 'KUBE_CONTEXT=!echo nope\n' >> "$SANDBOX_HOME/$PKEY/config.env"
if load_profile "$KTMP/clone1" >/dev/null 2>&1; then echo "FAIL: KUBE_CONTEXT=!cmd accepted"; fails=$((fails+1)); else echo "ok: KUBE_CONTEXT=!cmd rejected"; fi
# 壞行 fail-loud
printf 'PF_OK=x\n' > "$SANDBOX_HOME/$PKEY/config.env"
printf 'GARBAGE LINE\n' >> "$SANDBOX_HOME/$PKEY/config.env"
if load_profile "$KTMP/clone1" >/dev/null 2>&1; then echo "FAIL: garbage profile accepted"; fails=$((fails+1)); else echo "ok: garbage profile rejected (fail-loud)"; fi

# --- load_sandbox_yml：env_required / env_optional / config_params(_mode) -------
mkdir -p "$KTMP/decl/src"; : > "$KTMP/decl/src/__manifest__.py"
cat > "$KTMP/decl/.sandbox.yml" <<'YML'
module: decl_mod
env_required:
  - "REQ_ONE: first required"
env_optional:
  - "OPT_ONE: first optional"
config_params:
  web.base.url: "http://{{slug}}.local:8080"
  some.flag: "True"
config_params_stg:
  web.base.url: "https://{{slug}}.woowtech.io"
YML
REPO_ROOT="$KTMP/decl"; load_sandbox_yml "$KTMP/decl" >/dev/null 2>&1
assert_eq "env_required parsed" "${SBX_ENV_REQUIRED:-}" "REQ_ONE"$'\t'"first required"$'\n'
assert_eq "env_optional parsed" "${SBX_ENV_OPTIONAL:-}" "OPT_ONE"$'\t'"first optional"$'\n'
# require_declared_env：--pre（pending 放行）與 final（缺值擋）
unset REQ_ONE 2>/dev/null || true
PROFILE_PENDING_CMDS="REQ_ONE=printf later"$'\n'
if require_declared_env --pre >/dev/null 2>&1; then echo "ok: --pre passes with pending"; else echo "FAIL: --pre blocked despite pending"; fails=$((fails+1)); fi
PROFILE_PENDING_CMDS=""
if require_declared_env >/dev/null 2>&1; then echo "FAIL: final passed with missing REQ_ONE"; fails=$((fails+1)); else echo "ok: final blocks missing REQ_ONE"; fi
export REQ_ONE=given
if require_declared_env >/dev/null 2>&1; then echo "ok: final passes when set"; else echo "FAIL: final blocked when set"; fails=$((fails+1)); fi
# 非法 env 名 → fail-loud
cat > "$KTMP/decl/.sandbox.yml" <<'YML'
module: decl_mod
env_required:
  - "BAD-NAME: dash not allowed"
YML
if load_sandbox_yml "$KTMP/decl" >/dev/null 2>&1; then echo "FAIL: bad env name accepted"; fails=$((fails+1)); else echo "ok: bad env name rejected"; fi
# 還原宣告檔（merge/render 測試用）
cat > "$KTMP/decl/.sandbox.yml" <<'YML'
module: decl_mod
config_params:
  web.base.url: "http://{{slug}}.local:8080"
  some.flag: "True"
config_params_stg:
  web.base.url: "https://{{slug}}.woowtech.io"
YML
load_sandbox_yml "$KTMP/decl" >/dev/null 2>&1

# --- merged_config_params + render_config_value --------------------------------
m_local="$(merged_config_params local)"
assert_eq "merge local url"  "$(printf '%s\n' "$m_local" | awk -F'\t' '$1=="web.base.url"{print $2}')" "http://{{slug}}.local:8080"
assert_eq "merge local flag" "$(printf '%s\n' "$m_local" | awk -F'\t' '$1=="some.flag"{print $2}')" "True"
m_stg="$(merged_config_params stg)"
assert_eq "merge stg override" "$(printf '%s\n' "$m_stg" | awk -F'\t' '$1=="web.base.url"{print $2}')" "https://{{slug}}.woowtech.io"
assert_eq "merge stg keeps base" "$(printf '%s\n' "$m_stg" | awk -F'\t' '$1=="some.flag"{print $2}')" "True"
SLUG=myslug NAMESPACE=ns1 DB_NAME=db1 MODE=stg
assert_eq "render slug" "$(render_config_value 'https://{{slug}}.woowtech.io')" "https://myslug.woowtech.io"
assert_eq "render multi" "$(render_config_value '{{namespace}}/{{db_name}}/{{mode}}')" "ns1/db1/stg"
unset SLUG NAMESPACE DB_NAME MODE SANDBOX_HOME PF_PLAIN PF_PRESET PF_CMD PF_BADCMD PKEY REQ_ONE
```

- [ ] **Step 3：RED 確認（用系統 bash 跑）**

Run: `/bin/bash ~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/scripts/lib/common_test.sh; echo "exit=$?"`
Expected: exit=1。失敗形貌：部分新斷言 FAIL（command not found）、部分空值比對假 ok、**且既有 `yaml_sq quote escape` 在 /bin/bash 3.2 下本來就 FAIL**（pre-existing，Task 1.2 一併修）。腳本可能在 `set -u` unbound 處提前中止——紅燈訊號即可，不要求完整跑完。

### Task 1.2：實作新函式 + parser 擴充 + 修 yaml_sq

**Files:**
- Modify: `~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/scripts/lib/common.sh`

- [ ] **Step 1：修既有 `yaml_sq`（3.2 相容；既有測試在系統 bash 轉綠）**

舊（`:394-398`）：

```bash
yaml_sq() {
    local v="${1:-}"
    v="${v//\'/\'\'}"
    printf "'%s'" "$v"
}
```

新：

```bash
yaml_sq() {
    # 3.2 注意：${v//\'/\'\'} 在 bash<4.3 會把反斜線留進輸出（a'b → a\'\'b）——
    # 用變數形式的 pattern/replacement 跨版本一致。
    local v="${1:-}" q="'"
    v="${v//$q/$q$q}"
    printf "'%s'" "$v"
}
```

- [ ] **Step 2：在 `yaml_sq` 之後 append 新函式群**

```bash
# =============================================================================
# (g) compute_project_key — onboarding profile 的 project 識別
# =============================================================================
# Worktree 與多 clone（maestri Floor）都要收斂到同一個 key：
#   ① origin remote URL 正規化（scheme://、user@、.git 後綴剝除；scp 形第一個 ':' → '/'）
#      → worktree 與 clone 同 key
#   ② 無 remote → git common dir 絕對路徑（同 repo 的 worktrees 仍同 key）
#   ③ 非 git repo → repo 目錄絕對路徑
# 輸出 "{name}-{hash8}"；name = id 最後一段（repo 名）正規化，hash8 防同名不同源相撞。
# 路徑一律 `pwd -P` 物理化：git 對 worktree 回傳 realpath，主 repo 走相對 .git 絕對化，
# 兩者在 symlink 路徑（macOS /tmp、/var → /private/...）下會字串不同 → key 漂移（實證）。
# remote URL 改名/搬家 → key 漂移、舊 profile 變孤兒（design 風險 #6：接受，重跑 onboard）。
compute_project_key() {
    local repo="${1:-${REPO_ROOT:-}}"
    if [[ -z "$repo" || ! -d "$repo" ]]; then
        _log_err "compute_project_key: repo root '${repo}' is not a directory"; return 1
    fi
    repo="$(cd "$repo" && pwd -P)"
    local id=""
    id="$(git -C "$repo" remote get-url origin 2>/dev/null || true)"
    if [[ -n "$id" ]]; then
        id="${id%%[[:space:]]*}"
        id="${id%/}"; id="${id%.git}"
        id="$(printf '%s' "$id" \
            | sed -E 's#^[A-Za-z][A-Za-z0-9+.-]*://##; s#^[^@/]+@##; s#:#/#' \
            | tr '[:upper:]' '[:lower:]')"
    elif id="$(git -C "$repo" rev-parse --git-common-dir 2>/dev/null)"; then
        case "$id" in /*) : ;; *) id="${repo}/${id}" ;; esac
        id="$(cd "$id" && pwd -P)"
    else
        id="$repo"
    fi
    local name hash
    name="$(basename "$id")"
    [[ "$name" == ".git" ]] && name="$(basename "$(dirname "$id")")"
    name="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g' | cut -c1-40)"
    [[ -z "$name" ]] && name="repo"
    if command -v shasum >/dev/null 2>&1; then
        hash="$(printf '%s' "$id" | shasum -a 256 | cut -c1-8)"
    else
        hash="$(printf '%s' "$id" | sha256sum | cut -c1-8)"
    fi
    printf '%s-%s\n' "$name" "$hash"
}

# =============================================================================
# (h) sandbox_profile_file — profile 路徑（$SANDBOX_HOME 可覆寫，預設 ~/.sandbox）
# =============================================================================
sandbox_profile_file() {
    local repo="${1:-${REPO_ROOT:-}}" key
    key="$(compute_project_key "$repo")" || return 1
    printf '%s/%s/config.env\n' "${SANDBOX_HOME:-$HOME/.sandbox}" "$key"
}

# =============================================================================
# (i) load_profile — user-scope profile 兩段式載入
# =============================================================================
# 第一段（本函式）：只 export 字面值；`!command` 行記入 PROFILE_PENDING_CMDS 不求值。
# 第二段（eval_profile_commands）：呼叫端在 require_kube_context 切好 context 後執行
# —— `!cmd` 常含 kubectl（如撈 operator secret），在 ambient context 下跑會跨 cluster
# 誤讀、甚至撈到「存在但錯」的 secret 靜默寫進 helm values（fail-closed 擋不住非空錯值）。
# 規則：
#   - 「當下 env 先贏」以變數值是否非空判定（與全工具 `${VAR:-}` 慣例一致）。
#   - KUBE_CONTEXT 只能是字面值（!command 與 context 切換語義矛盾）→ fail-loud。
#   - 工具內部變數名（deny-list）不接受注入 → warn 跳過。
#   - 檔案解析錯誤 fail-loud（return 1）。
# 只有 create.sh / onboard.sh 載入 profile；destroy.sh 刻意不載（design D6）。
load_profile() {
    local repo="${1:-${REPO_ROOT:-}}"
    PROFILE_FILE="$(sandbox_profile_file "$repo")" || return 1
    PROFILE_PENDING_CMDS=""
    export PROFILE_FILE
    if [[ ! -f "$PROFILE_FILE" ]]; then
        _log_info "load_profile: no profile at ${PROFILE_FILE}（可用 onboard 建立）"
        return 0
    fi
    local line key val
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
        else
            _log_err "load_profile: unparseable line in ${PROFILE_FILE}: ${line}"
            return 1
        fi
        if _profile_key_denied "$key"; then
            _log_warn "load_profile: '${key}' 是工具內部變數名 — 忽略（profile 供 env，不是旗標注入面）"
            continue
        fi
        if [[ "${val:0:1}" == "!" ]]; then
            if [[ "$key" == "KUBE_CONTEXT" ]]; then
                _log_err "load_profile: KUBE_CONTEXT 只能是字面值（修 ${PROFILE_FILE}）"
                return 1
            fi
            [[ -n "${!key:-}" ]] && continue
            PROFILE_PENDING_CMDS="${PROFILE_PENDING_CMDS}${key}=${val:1}"$'\n'
            continue
        fi
        [[ -n "${!key:-}" ]] && continue
        export "${key}=${val}"
    done < "$PROFILE_FILE"
    _log_ok "load_profile: loaded ${PROFILE_FILE}"
    [[ -n "$PROFILE_PENDING_CMDS" ]] && _log_info "load_profile: !command 條目延後到 kube context 切換後求值"
    return 0
}

# profile 不可注入的工具內部變數名（create.sh 旗標儲存變數 + 推導座標 + SBX_*）。
# 例：profile 寫 IMAGE_TAG=xxx 會在不帶 --image 時把 MODE 靜默翻成 ci-local。
_profile_key_denied() {
    case "$1" in
        SBX_*|PROFILE_FILE|PROFILE_PENDING_CMDS|PROJECT_KEY|REPO_ROOT|REPO_FLAG|\
        IMAGE_TAG|MODE|MODE_OVERRIDE|EXTRA_VALUES|EXTRA_ARGS|BRANCH|BRANCH_OVERRIDE|\
        MODULE_OVERRIDE|DB_PREFIX|DB_PREFIX_OVERRIDE|LANG_CODE|TTL|PGADMIN|INGRESS|\
        ASSUME_YES|HELM_CMD|SLUG|NAMESPACE|DB_NAME|PRIMARY_MODULE|INSTALL_MODULES|\
        PATH|HOME|IFS|LABEL_DOMAIN)
            return 0 ;;
        *) return 1 ;;
    esac
}

# 第二段：求值 PROFILE_PENDING_CMDS（呼叫前提：require_kube_context 已切好 context）。
# 失敗或輸出為空 → 視同未設（warn；缺值語義交 require_declared_env final）。恆 return 0。
eval_profile_commands() {
    [[ -n "${PROFILE_PENDING_CMDS:-}" ]] || return 0
    local line key cmd out
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        key="${line%%=*}"; cmd="${line#*=}"
        [[ -n "${!key:-}" ]] && continue
        if ! out="$(bash -c "$cmd" 2>/dev/null)" || [[ -z "$out" ]]; then
            _log_warn "profile: ${key} 的取值指令失敗或輸出為空 — 視同未設"
            continue
        fi
        export "${key}=${out}"
    done <<< "$PROFILE_PENDING_CMDS"
    return 0
}

# pending 清單是否含某 key（require_declared_env --pre 放行判定用）
profile_pending_has() {
    [[ -n "${PROFILE_PENDING_CMDS:-}" ]] || return 1
    printf '%s' "$PROFILE_PENDING_CMDS" | grep -q "^${1}="
}

# =============================================================================
# (j) require_declared_env — env_required fail-fast / env_optional warn
# =============================================================================
# 讀 SBX_ENV_REQUIRED / SBX_ENV_OPTIONAL（load_sandbox_yml 設定；每行 "NAME<TAB>desc"）。
# 兩段式：`--pre`（context/!cmd 之前）對「缺值但 pending !cmd 可能補」放行、不印 optional
# warn —— 讓無 !cmd 的 repo（多數）在不碰 cluster 前就 fail-fast；final（!cmd 求值後）
# 全量判定。required 缺 → 列清單 + onboard 指引 return 1；optional 缺 → warn（不靜默）。
require_declared_env() {
    local premode="${1:-}" missing="" name desc
    while IFS=$'\t' read -r name desc; do
        [[ -n "$name" ]] || continue
        if [[ -z "${!name:-}" ]]; then
            if [[ "$premode" == "--pre" ]] && profile_pending_has "$name"; then continue; fi
            missing="${missing}  - ${name}: ${desc}"$'\n'
        fi
    done <<< "${SBX_ENV_REQUIRED:-}"
    if [[ "$premode" != "--pre" ]]; then
        while IFS=$'\t' read -r name desc; do
            [[ -n "$name" ]] || continue
            [[ -z "${!name:-}" ]] && _log_warn "optional env 未設: ${name}（${desc}）— 相關功能將降級/停用"
        done <<< "${SBX_ENV_OPTIONAL:-}"
    fi
    if [[ -n "$missing" ]]; then
        _log_err "缺少必要 env（.sandbox.yml env_required 宣告）："
        printf '%s' "$missing" >&2
        _log_err "補法：/woowprd:k8s-dev-sandbox onboard --repo <repo>（記錄一次、worktree 共用），或 export 後重試。"
        return 1
    fi
    return 0
}

# =============================================================================
# (k) merged_config_params — config_params(+config_params_<mode>) 合併輸出
# =============================================================================
# 讀 SBX_CONFIG_PARAMS_RAW（每行 "<scope>\t<key>\t<value>"；scope=* 或底線化 mode 名）。
# 輸出每行 "key<TAB>value"，mode 行覆寫 base 行；無宣告輸出空。
merged_config_params() {
    local mode="${1:?merged_config_params: mode required}"
    mode="${mode//-/_}"
    [[ -n "${SBX_CONFIG_PARAMS_RAW:-}" ]] || return 0
    printf '%s\n' "$SBX_CONFIG_PARAMS_RAW" | awk -F'\t' -v m="$mode" '
        $1 == "*" { if (!($2 in seen)) { order[++n] = $2; seen[$2] = 1 }; val[$2] = $3; next }
        $1 == m   { if (!($2 in seen)) { order[++n] = $2; seen[$2] = 1 }; val[$2] = $3 }
        END { for (i = 1; i <= n; i++) print order[i] "\t" val[order[i]] }
    '
}

# =============================================================================
# (l) render_config_value — {{slug}}/{{namespace}}/{{db_name}}/{{mode}} 模板替換
# =============================================================================
# 只做字串替換（design：不做運算）；變數取當下 env 的 SLUG/NAMESPACE/DB_NAME/MODE。
render_config_value() {
    local v="${1:-}"
    v="${v//\{\{slug\}\}/${SLUG:-}}"
    v="${v//\{\{namespace\}\}/${NAMESPACE:-}}"
    v="${v//\{\{db_name\}\}/${DB_NAME:-}}"
    v="${v//\{\{mode\}\}/${MODE:-}}"
    printf '%s\n' "$v"
}
```

- [ ] **Step 3：擴充 `load_sandbox_yml` 解析三個新區塊（6 個改點）**

改點 ①（`:254-256` 變數重置區）— 舊：

```bash
    SBX_LAYOUT="" SBX_MODULE="" SBX_INSTALL_MODULES="" SBX_DETECTED_MODULES=""
    SBX_DB_PREFIX="" SBX_EXTRA_ADDONS="" SBX_POST_CREATE_HOOK=""
    local yml_install_modules="" yml_extra_addons=""
```

新：

```bash
    SBX_LAYOUT="" SBX_MODULE="" SBX_INSTALL_MODULES="" SBX_DETECTED_MODULES=""
    SBX_DB_PREFIX="" SBX_EXTRA_ADDONS="" SBX_POST_CREATE_HOOK=""
    SBX_ENV_REQUIRED="" SBX_ENV_OPTIONAL="" SBX_CONFIG_PARAMS_RAW=""
    local yml_install_modules="" yml_extra_addons="" ename edesc
```

改點 ②（`:260` 迴圈區域變數）— 舊：`local line key val cur_list=""`
新：`local line key val cur_list="" cur_map=""`

改點 ③（list-item 分支的 `case "$cur_list"`，`:267-270`）— 在 `extra_addons)` case 之後加：

```bash
                    env_required|env_optional)
                        ename="${val%%:*}"; ename="${ename%"${ename##*[![:space:]]}"}"
                        if [[ "$val" == *:* ]]; then
                            edesc="${val#*:}"; edesc="${edesc#"${edesc%%[![:space:]]*}"}"
                        else
                            edesc=""
                        fi
                        if [[ ! "$ename" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
                            _log_err "load_sandbox_yml: ${cur_list} 項目不是合法 env 名: '${val}'（格式 \"NAME: desc\"）"
                            return 1
                        fi
                        if [[ "$cur_list" == "env_required" ]]; then
                            SBX_ENV_REQUIRED="${SBX_ENV_REQUIRED}${ename}"$'\t'"${edesc}"$'\n'
                        else
                            SBX_ENV_OPTIONAL="${SBX_ENV_OPTIONAL}${ename}"$'\t'"${edesc}"$'\n'
                        fi
                        ;;
```

改點 ④（list-item 分支 `continue` 之後、頂層 key 分支之前）— 插入 map-entry 分支：

```bash
            # config_params(_mode) 區塊內的縮排 "key: value" 項（key 可含 '.'）。
            # 限制：值不可含 '#'（上方 ${line%%#*} 已剝行內註解）——文件已載明。
            if [[ "$line" =~ ^[[:space:]]+([A-Za-z0-9_.][A-Za-z0-9_.-]*):[[:space:]]*(.*)$ ]]; then
                if [[ -z "$cur_map" ]]; then
                    _log_err "load_sandbox_yml: 縮排的 map 項出現在 config_params 區塊之外: ${line}"
                    return 1
                fi
                key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
                val="${val%"${val##*[![:space:]]}"}"
                val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
                SBX_CONFIG_PARAMS_RAW="${SBX_CONFIG_PARAMS_RAW}${cur_map}"$'\t'"${key}"$'\t'"${val}"$'\n'
                continue
            fi
```

改點 ⑤（頂層 key 分支，**釘行號 `:277` 的 `cur_list=""`** —— 同名字串在 `:291`/`:302` 的 inline-array arm 內也有，用下面兩行一起換保唯一）— 舊：

```bash
                cur_list=""
                case "$key" in
```

新：

```bash
                cur_list=""; cur_map=""
                case "$key" in
```

並在 `case "$key"` 的 `extra_addons)` case（含其整個 inline-array 處理）之後加：

```bash
                    env_required)  cur_list="env_required" ;;
                    env_optional)  cur_list="env_optional" ;;
                    config_params) cur_map="*" ;;
                    config_params_*) cur_map="${key#config_params_}" ;;
```

改點 ⑥（`:347` export 行）— 在既有 export 行之後加一行：

```bash
    export SBX_ENV_REQUIRED SBX_ENV_OPTIONAL SBX_CONFIG_PARAMS_RAW
```

- [ ] **Step 4：GREEN 確認（雙 bash）**

Run: `/bin/bash .../lib/common_test.sh && bash .../lib/common_test.sh`
Expected: 兩個版本皆 `ALL PASS`（含既有斷言零回歸；yaml_sq 在 3.2 轉綠）。

- [ ] **Step 5：語法檢查（shellcheck optional）**

Run: `bash -n .../lib/common.sh && /bin/bash -n .../lib/common.sh; command -v shellcheck >/dev/null && shellcheck -x .../lib/common.sh || echo "(shellcheck not installed — skipped)"`
Expected: bash -n 無輸出。

- [ ] **Step 6：Commit（plugin repo）**

```bash
cd ~/.claude/plugins-local/woowprd
git add skills/k8s-dev-sandbox/scripts/lib/common.sh skills/k8s-dev-sandbox/scripts/lib/common_test.sh
git commit -m "feat: common.sh 新增 onboarding 第③層（project-key/兩段式 profile/env 宣告/config_params）+ 修 yaml_sq bash3.2 跳脫"
```

---

## Phase 2：`create.sh` 整合（兩段式 profile / 雙段 fail-fast / config_params）

### Task 2.1：重排載入順序

**Files:**
- Modify: `~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/scripts/create.sh:100-142`

新順序（repo → profile 字面 → yml → pre fail-fast → context/helm/chart → `!cmd` 求值 → final 檢查）：

- [ ] **Step 1：在「先決條件檢查」（`:100`）之前插入**

```bash
# --- 解析被測 repo + onboarding profile（第一段：字面值）---
# profile（~/.sandbox/{project-key}/config.env）字面值要在 require_kube_context 之前載：
# KUBE_CONTEXT 可由 profile 補（CLI flag / 當下 env 先贏）。`!command` 條目此時只記
# pending、不求值 —— 求值必須等 context 切好（防跨 cluster 誤讀，見 load_profile 註解）。
# destroy.sh 刻意不載 profile（防誤刪紅線：銷毀必須顯式 --context）。
REPO_ROOT="$(resolve_repo_root --repo "${REPO_FLAG:-}")" || exit 1
load_profile "$REPO_ROOT" || exit 1

# --- 解析 .sandbox.yml（純 FS，提前到 cluster 接觸前）---
load_sandbox_yml "$REPO_ROOT" || exit 1
# 第一段 fail-fast：缺 env_required 且無 pending !cmd 可補 → 不碰 cluster 直接擋
require_declared_env --pre || exit 1
```

- [ ] **Step 2：原 `:129-131` 區塊改為只留 PRIMARY_MODULE 推導**

舊：

```bash
# --- 解析被測 repo + .sandbox.yml（layout / module / db_prefix / extra_addons / hook）---
REPO_ROOT="$(resolve_repo_root --repo "${REPO_FLAG:-}")" || exit 1
load_sandbox_yml "$REPO_ROOT" || exit 1
```

新（REPO_ROOT / load_sandbox_yml 已上移，此處接原 `:132` 起的 PRIMARY_MODULE 邏輯）：

```bash
# --- module / db_prefix 推導（.sandbox.yml 已於開頭載入）---
```

- [ ] **Step 3：在先決條件區塊結束（`All prerequisites met` 之後）插入第二段**

```bash
# --- onboarding profile 第二段：context 已切好，安全求值 !command 條目 ---
eval_profile_commands
# 最終 env 宣告判定（required 缺 → fail；optional 缺 → warn）
require_declared_env || exit 1
```

- [ ] **Step 4：重排副作用記錄（行為改變，皆已評估）**

1. repo 壞 + context 壞時：舊版先報 context 錯**且已切走全域 current-context**才解析 repo；新版 repo 錯先出、context 完全不動（淨改善）。
2. helm 未裝的機器：profile **字面值**已載入才報 helm 缺（無害——字面值載入無副作用；`!cmd` 仍在 helm/context 檢查後）。
3. 無 `!cmd` 的 repo（多數）：缺 required env 在離線 / VPN 未開時也能拿到 onboard 指引（`--pre` 段），不會先撞 30s cluster 連線錯。

- [ ] **Step 5：語法檢查**

Run: `bash -n .../create.sh && /bin/bash -n .../create.sh`
Expected: 無輸出。

### Task 2.2：`apply_config_params`（diff-aware、fail-loud、漂移收斂可觀測）

**Files:**
- Modify: `~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/scripts/create.sh`（DB init 段 + 結尾）

- [ ] **Step 1：在 `# --- 檢查並初始化資料庫 ---` 之前定義函式**

```bash
# --- 宣告式 config_params 套用（.sandbox.yml；diff-aware、冪等、desired-state）---
# 每次 create/up 都跑（fresh 與 upgrade 路徑）：宣告值是 desired-state，hook / 人工改過
# 的同名 key 會被收斂回宣告值（印「收斂漂移值」讓 flip 可觀測）——所以勿宣告 hook 或
# 人工也會改的 key。只在值實際改變時寫入；呼叫端依 CONFIG_PARAMS_CHANGED 決定 restart
# （解 ir.config_parameter in-process cache）。個別鍵失敗計入 CONFIG_PARAMS_FAILED 並
# 印出 psql stderr；create.sh 結尾對 FAILED>0 以非零退出（宣告 seed 是功能正確性前提，
# 不可靜默降級——deploy-stg 的 web.base.url 失敗會直接回歸 ECPay Mixed Content bug）。
# 函式本身恆 return 0（set -e 安全）。
apply_config_params() {
    CONFIG_PARAMS_CHANGED=0
    CONFIG_PARAMS_FAILED=${CONFIG_PARAMS_FAILED:-0}
    local pairs key val cur esc_k esc_v out q="'"
    pairs="$(merged_config_params "$MODE")"
    [[ -z "$pairs" ]] && return 0
    echo -e "${BLUE}Applying declared config_params (.sandbox.yml)...${NC}"
    while IFS=$'\t' read -r key val; do
        [[ -n "$key" ]] || continue
        val="$(render_config_value "$val")"
        esc_k="${key//$q/$q$q}"; esc_v="${val//$q/$q$q}"
        if ! cur="$(kubectl exec -n "$NAMESPACE" "statefulset/${PG_STATEFULSET}" -- \
            psql -U odoo -d "$DB_NAME" -tAc \
            "SELECT value FROM ir_config_parameter WHERE key='${esc_k}'" 2>/dev/null)"; then
            echo -e "${RED}  config_params: 讀取 ${key} 失敗（DB 未就緒？）${NC}" >&2
            CONFIG_PARAMS_FAILED=$((CONFIG_PARAMS_FAILED+1)); continue
        fi
        if [[ "$cur" == "$val" ]]; then
            echo -e "  = ${key}（不變）"
            continue
        fi
        if out="$(kubectl exec -n "$NAMESPACE" "statefulset/${PG_STATEFULSET}" -- \
            psql -U odoo -d "$DB_NAME" -v ON_ERROR_STOP=1 -qc \
            "INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
             VALUES ('${esc_k}', '${esc_v}', 1, NOW(), 1, NOW())
             ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = NOW(), write_uid = 1;" 2>&1)"; then
            if [[ -n "$cur" ]]; then
                echo -e "  ${GREEN}↻ ${key} = ${val}（收斂漂移值，原值: ${cur}）${NC}"
            else
                echo -e "  ${GREEN}+ ${key} = ${val}${NC}"
            fi
            CONFIG_PARAMS_CHANGED=1
        else
            echo -e "${RED}  config_params: 寫入 ${key} 失敗：${out}${NC}" >&2
            CONFIG_PARAMS_FAILED=$((CONFIG_PARAMS_FAILED+1))
        fi
    done <<< "$pairs"
    return 0
}
```

- [ ] **Step 2：fresh 路徑接線（post-create hook 之前；D5）**

錨點：`POST_HOOK="${REPO_ROOT}/${SBX_POST_CREATE_HOOK:-.sandbox/post-create.sh}"` 行之前插入：

```bash
    # 宣告式 config_params 先套（D5：宣告式為 base、post-create hook 可覆寫——僅首次；
    # 之後每次 up 宣告值會收斂回來，見 apply_config_params 註解）。
    # restart 由本分支既有的 rollout restart 統一處理。
    apply_config_params
```

- [ ] **Step 3：upgrade 路徑接線（else 分支 `:477-479`）**

新：

```bash
else
    echo -e "${GREEN}Database '$DB_NAME' already initialized (${PRIMARY_MODULE} installed).${NC}"
    # upgrade 路徑也套宣告式 config_params；只有值真的變了才 restart
    # （避免 up 既有沙盒每次白白重啟）。
    apply_config_params
    if [[ "${CONFIG_PARAMS_CHANGED:-0}" -eq 1 ]]; then
        echo -e "${BLUE}config_params changed — restarting Odoo to reload ir.config_parameter cache...${NC}"
        kubectl rollout restart -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}"
        kubectl rollout status -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" --timeout=120s 2>/dev/null || true
    fi
fi
```

- [ ] **Step 4：結尾失敗 gate（存取資訊輸出之後、腳本最末）**

```bash
# 宣告 seed 失敗不可靜默（恢復舊 deploy-stg 的 fail-loud 語義；沙盒本體可用，
# 但宣告參數不完整 —— 修因後重跑 create 即可冪等收斂）。
if [[ "${CONFIG_PARAMS_FAILED:-0}" -gt 0 ]]; then
    echo -e "${RED}config_params: ${CONFIG_PARAMS_FAILED} 個宣告參數套用失敗（見上方錯誤）— exit 1${NC}" >&2
    exit 1
fi
```

- [ ] **Step 5：語法檢查 + Commit（plugin repo）**

```bash
bash -n .../create.sh && /bin/bash -n .../create.sh
cd ~/.claude/plugins-local/woowprd
git add skills/k8s-dev-sandbox/scripts/create.sh
git commit -m "feat: create.sh 接兩段式 profile + 雙段 env fail-fast + desired-state config_params（fail-loud）"
```

---

## Phase 3：`onboard.sh` 子命令

### Task 3.1：寫 `onboard.sh`（完整檔）

**Files:**
- Create: `~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/scripts/onboard.sh`（chmod +x）

- [ ] **Step 1：寫入完整內容**

```bash
#!/usr/bin/env bash
#
# onboard.sh — 記錄 project 的執行期供值（三層設定模型第③層），寫 user-scope profile。
#
# Usage:
#   onboard.sh --repo <addon-repo> [OPTIONS]
#
# Options:
#   --repo <path>        被測 Odoo addon repo（必帶）
#   --context <name>     記錄預設 kubectl context（寫進 profile 供 create/up 用；
#                        destroy 不吃 profile，仍須顯式 --context）。只接受字面值。
#   --set KEY=VALUE      直接供值（可重複；agent 非互動路徑）。未宣告的 KEY 也會寫入。
#                        VALUE 可用 '!command' 形態（create 時、context 切換後動態求值）。
#   --check              只檢查不寫檔：列缺值清單；有 env_required 缺值 → exit 1
#   --non-interactive    缺值不 prompt（配 --set 用）
#   -h, --help
#
# Profile: ${SANDBOX_HOME:-~/.sandbox}/{project-key}/config.env（chmod 600）
#   - project-key 由 origin remote URL（fallback: git common dir 物理路徑）推導 ——
#     同 repo 的全部 worktree / clone 共用同一份 profile。
#   - 既有 KEY=!command 行：未被 --set / prompt 覆寫時原樣保留（不被求值結果覆蓋）。
#   - 宣告 env 的當下 shell export 值會被擷取寫入（onboarding 的目的）；
#     但 KUBE_CONTEXT 絕不從 ambient env 擷取（防誤把殘留的 prod context 寫成預設）。
#   - create 時解析優先序：CLI flag > 當下 env > profile > .sandbox.yml > 偵測。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

REPO_FLAG="" CONTEXT_FLAG="" CHECK_ONLY=false NON_INTERACTIVE=false
SET_PAIRS=""   # 每行 KEY=VALUE（bash 3.2 安全：不用 array）

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)    REPO_FLAG="$2"; shift 2 ;;
        --context) CONTEXT_FLAG="$2"; shift 2 ;;
        --set)
            if [[ ! "${2:-}" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
                _log_err "--set 格式須為 KEY=VALUE（KEY 為合法 env 名）: ${2:-<missing>}"; exit 1
            fi
            SET_PAIRS="${SET_PAIRS}${2}"$'\n'
            shift 2 ;;
        --check)   CHECK_ONLY=true; shift ;;
        --non-interactive) NON_INTERACTIVE=true; shift ;;
        -h|--help) sed -n '/^# onboard.sh/,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) _log_err "Unknown option: $1"; exit 1 ;;
    esac
done

# --context 視同 --set KUBE_CONTEXT=...（顯式來源；置於 SET_PAIRS 開頭最高優先）
[[ -n "$CONTEXT_FLAG" ]] && SET_PAIRS="KUBE_CONTEXT=${CONTEXT_FLAG}"$'\n'"${SET_PAIRS}"

REPO_ROOT="$(resolve_repo_root --repo "${REPO_FLAG:-}")" || exit 1
# 宣告解析 + extra_addons fail-loud 驗證（onboard dry-run 的一部分）
load_sandbox_yml "$REPO_ROOT" || exit 1
PROJECT_KEY="$(compute_project_key "$REPO_ROOT")" || exit 1
PROFILE_FILE="$(sandbox_profile_file "$REPO_ROOT")" || exit 1

echo -e "${BLUE}Repo:${NC}        ${REPO_ROOT}"
echo -e "${BLUE}Project key:${NC} ${PROJECT_KEY}"
echo -e "${BLUE}Profile:${NC}     ${PROFILE_FILE}"
echo ""

# --- 讀既有 profile（verbatim、不求值 —— onboard 絕不把 !cmd 換成求值結果）---
EXISTING_PAIRS=""
if [[ -f "$PROFILE_FILE" ]]; then
    while IFS= read -r _line || [[ -n "$_line" ]]; do
        _line="${_line%$'\r'}"
        [[ "$_line" =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ "$_line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            EXISTING_PAIRS="${EXISTING_PAIRS}${_line}"$'\n'
        else
            _log_err "既有 profile 有無法解析的行: ${_line}"
            _log_err "  請手修 ${PROFILE_FILE} 後重試。"
            exit 1
        fi
    done < "$PROFILE_FILE"
fi

lookup_pairs() { # $1=key $2=pairs（每行 KEY=VALUE）→ 印 value；rc 0=找到
    local line
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        [[ "${line%%=*}" == "$1" ]] && { printf '%s' "${line#*=}"; return 0; }
    done <<< "$2"
    return 1
}

# resolve_source <name>：設 RESOLVED_VAL / RESOLVED_SRC（set|env|profile|none）。
# onboard 不呼叫 load_profile —— 「env」一定是使用者當下 shell 真實 export 的值，
# 不會被 profile 求值結果污染（!cmd 行 verbatim 保留的前提）。
resolve_source() {
    RESOLVED_VAL="" RESOLVED_SRC="none"
    if RESOLVED_VAL="$(lookup_pairs "$1" "$SET_PAIRS")"; then RESOLVED_SRC="set"; return 0; fi
    if [[ -n "${!1:-}" ]]; then RESOLVED_VAL="${!1}"; RESOLVED_SRC="env"; return 0; fi
    if RESOLVED_VAL="$(lookup_pairs "$1" "$EXISTING_PAIRS")"; then RESOLVED_SRC="profile"; return 0; fi
    return 1
}

MISSING_REQUIRED=0
NEW_PAIRS=""        # 本次要寫入的 KEY=VALUE（來源 set / env 擷取 / prompt）

collect_one() { # $1=name $2=desc $3=required(true/false)
    local name="$1" desc="$2" required="$3" ans=""
    if resolve_source "$name"; then
        case "$RESOLVED_SRC" in
            set) NEW_PAIRS="${NEW_PAIRS}${name}=${RESOLVED_VAL}"$'\n'
                 echo -e "  ${GREEN}✓${NC} ${name}（--set）" ;;
            env) NEW_PAIRS="${NEW_PAIRS}${name}=${RESOLVED_VAL}"$'\n'
                 echo -e "  ${GREEN}✓${NC} ${name}（當下 env → 擷取寫入 profile）" ;;
            profile) echo -e "  ${GREEN}✓${NC} ${name}（profile 既有，原樣保留）" ;;
        esac
        return 0
    fi
    if [[ "$CHECK_ONLY" == true ]]; then
        if [[ "$required" == true ]]; then
            echo "MISSING required ${name} — ${desc}"
            MISSING_REQUIRED=1
        else
            echo "MISSING optional ${name} — ${desc}"
        fi
        return 0
    fi
    # prompt 一律走 /dev/tty：collect_one 在 herestring 迴圈內呼叫，fd0 不是 tty
    if [[ "$NON_INTERACTIVE" == false && -r /dev/tty ]]; then
        printf '  %s（%s）[空白略過]: ' "$name" "$desc" > /dev/tty
        IFS= read -r ans < /dev/tty || ans=""
        if [[ -n "$ans" ]]; then
            NEW_PAIRS="${NEW_PAIRS}${name}=${ans}"$'\n'
            return 0
        fi
    fi
    if [[ "$required" == true ]]; then
        _log_err "缺必要值: ${name}（${desc}）"
        MISSING_REQUIRED=1
    else
        _log_warn "略過 optional: ${name}（${desc}）"
    fi
    return 0
}

echo -e "${BLUE}宣告的 env（.sandbox.yml）：${NC}"
while IFS=$'\t' read -r _n _d; do
    [[ -n "$_n" ]] || continue
    collect_one "$_n" "$_d" true
done <<< "${SBX_ENV_REQUIRED:-}"
while IFS=$'\t' read -r _n _d; do
    [[ -n "$_n" ]] || continue
    collect_one "$_n" "$_d" false
done <<< "${SBX_ENV_OPTIONAL:-}"

# --- KUBE_CONTEXT：只接受顯式來源（--context / --set）或既有 profile ---
# 絕不擷取 ambient env：殘留的 KUBE_CONTEXT=woow-k3s 被靜默寫入會讓之後所有
# 零互動 up 打到 prod/stg cluster（cross-cluster 顯式性紅線，同 D6 精神）。
if _ctx="$(lookup_pairs "KUBE_CONTEXT" "$SET_PAIRS")"; then
    if [[ "${_ctx:0:1}" == "!" ]]; then
        _log_err "KUBE_CONTEXT 只能是字面值（不可用 !command）"; exit 1
    fi
    NEW_PAIRS="${NEW_PAIRS}KUBE_CONTEXT=${_ctx}"$'\n'
    echo -e "  ${GREEN}✓${NC} KUBE_CONTEXT（顯式）"
elif lookup_pairs "KUBE_CONTEXT" "$EXISTING_PAIRS" >/dev/null; then
    echo -e "  ${GREEN}✓${NC} KUBE_CONTEXT（profile 既有，原樣保留）"
elif [[ -n "${KUBE_CONTEXT:-}" ]]; then
    _log_warn "偵測到當下 env KUBE_CONTEXT=${KUBE_CONTEXT} — 未寫入 profile（防誤擷取）；要記錄請用 --context"
fi

# --- --set 的未宣告 key 也寫入（agent 自由供值；含 hook 消費的 env）---
while IFS= read -r _line; do
    [[ -n "$_line" ]] || continue
    _k="${_line%%=*}"
    if printf '%s' "$NEW_PAIRS" | grep -q "^${_k}="; then continue; fi
    if _profile_key_denied "$_k"; then
        _log_err "--set ${_k}: 工具內部變數名，拒收（profile 供 env，不是旗標注入面）"; exit 1
    fi
    NEW_PAIRS="${NEW_PAIRS}${_line}"$'\n'
    echo -e "  ${GREEN}✓${NC} ${_k}（--set，未宣告）"
done <<< "$SET_PAIRS"

if [[ "$CHECK_ONLY" == true ]]; then
    echo ""
    if [[ "$MISSING_REQUIRED" -eq 1 ]]; then
        _log_err "--check: 有 env_required 缺值（exit 1）"
        exit 1
    fi
    _log_ok "--check: 必要值齊備（profile 未變更）"
    exit 0
fi

# --- 組新 profile：header → 新值 → 既有未覆寫行（verbatim，含 !cmd 與自加 key）---
_compose_profile() {
    echo "# k8s-dev-sandbox onboarding profile — $(date '+%Y-%m-%d %H:%M')"
    echo "# project-key: ${PROJECT_KEY}"
    echo "# repo（onboard 當下）: ${REPO_ROOT}"
    echo "# 格式: KEY=VALUE 或 KEY=!command（create 時、context 切換後求值）。勿放進任何 git。"
    local line key
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        printf '%s\n' "$line"
    done <<< "$NEW_PAIRS"
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        key="${line%%=*}"
        if printf '%s' "$NEW_PAIRS" | grep -q "^${key}="; then continue; fi
        printf '%s\n' "$line"
    done <<< "$EXISTING_PAIRS"
}

umask 077
mkdir -p "$(dirname "$PROFILE_FILE")"
_compose_profile > "${PROFILE_FILE}.tmp"
mv "${PROFILE_FILE}.tmp" "$PROFILE_FILE"
chmod 600 "$PROFILE_FILE"
_log_ok "Profile 已寫入: ${PROFILE_FILE}"

# --- dry-run 驗證 ---
echo ""
echo -e "${BLUE}Dry-run 驗證...${NC}"
_FINAL_CTX="$(lookup_pairs "KUBE_CONTEXT" "$NEW_PAIRS" || lookup_pairs "KUBE_CONTEXT" "$EXISTING_PAIRS" || true)"
if [[ -n "$_FINAL_CTX" ]]; then
    # 注意：require_kube_context 會切換當前 kubectl context（與 create 行為一致）；
    # 先切好 context 再試跑 !cmd —— 與 create 的兩段式求值順序一致，驗證才不失真。
    if ! require_kube_context "$_FINAL_CTX"; then
        _log_err "context '${_FINAL_CTX}' 驗證失敗 — profile 已寫入，修正後重跑 onboard，或 create 時用 --context。"
        exit 1
    fi
fi
# !cmd 條目逐一試執行（非空輸出）；失敗只 warn（指令可能依賴 VPN / 之後才可用）
while IFS= read -r _line; do
    [[ -n "$_line" ]] || continue
    [[ "$_line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=\!(.+)$ ]] || continue
    if _out="$(bash -c "${BASH_REMATCH[2]}" 2>/dev/null)" && [[ -n "$_out" ]]; then
        echo -e "  ${GREEN}✓${NC} ${BASH_REMATCH[1]}（!cmd 可取值）"
    else
        _log_warn "  ${BASH_REMATCH[1]} 的 !cmd 目前取不到值（create 時會視同未設）"
    fi
done < "$PROFILE_FILE"

echo ""
if [[ "$MISSING_REQUIRED" -eq 1 ]]; then
    _log_err "仍有 env_required 缺值（已寫入既有進度）— 補值後重跑 onboard。"
    exit 1
fi
_log_ok "Onboarding 完成。之後在任何 worktree："
echo -e "  ${YELLOW}/woowprd:k8s-dev-sandbox up${NC}（context 由 profile 補；destroy 仍須 --context）"
```

- [ ] **Step 2：語法檢查 + chmod**

Run: `chmod +x .../scripts/onboard.sh && bash -n .../scripts/onboard.sh && /bin/bash -n .../scripts/onboard.sh`
Expected: 無輸出。

### Task 3.2：onboard 煙霧測試（無 cluster；tmp SANDBOX_HOME + fixture）

- [ ] **Step 1：跑非互動全流程（KUBE_CONTEXT= 隔離環境殘留）**

```bash
SKILL=~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox
T="$(mktemp -d)"; mkdir -p "$T/repo/src"; : > "$T/repo/src/__manifest__.py"
cat > "$T/repo/.sandbox.yml" <<'YML'
module: smoke_mod
env_required:
  - "SMOKE_REQ: smoke required"
env_optional:
  - "SMOKE_OPT: smoke optional"
YML
# (a) --check 缺 required → exit 1
SANDBOX_HOME="$T/home" KUBE_CONTEXT= bash "$SKILL/scripts/onboard.sh" --repo "$T/repo" --check; echo "check1=$?"
# (b) --set 供值（含未宣告 !cmd key）+ 非互動 → 寫檔 exit 0
SANDBOX_HOME="$T/home" KUBE_CONTEXT= bash "$SKILL/scripts/onboard.sh" --repo "$T/repo" \
  --set SMOKE_REQ=v1 --set "SMOKE_CMD=!printf dyn" --non-interactive; echo "onboard=$?"
# (c) 再 --check → exit 0；profile 內容與權限
SANDBOX_HOME="$T/home" KUBE_CONTEXT= bash "$SKILL/scripts/onboard.sh" --repo "$T/repo" --check; echo "check2=$?"
P="$T/home/$(SANDBOX_HOME="$T/home" bash -c "source $SKILL/scripts/lib/common.sh; compute_project_key '$T/repo'")/config.env"
ls -l "$P"; grep -E 'SMOKE_REQ=v1|SMOKE_CMD=!printf dyn' "$P"
# (d) 重跑 onboard（無 --set）→ !cmd 行 verbatim 保留（不被求值結果覆蓋）
SANDBOX_HOME="$T/home" KUBE_CONTEXT= bash "$SKILL/scripts/onboard.sh" --repo "$T/repo" --non-interactive >/dev/null
grep -q 'SMOKE_CMD=!printf dyn' "$P" && echo "verbatim-kept=ok"
# (e) ambient KUBE_CONTEXT 不被擷取
SANDBOX_HOME="$T/home" KUBE_CONTEXT=woow-k3s bash "$SKILL/scripts/onboard.sh" --repo "$T/repo" --non-interactive >/dev/null 2>&1
grep -q '^KUBE_CONTEXT=' "$P" && echo "FAIL: ambient ctx captured" || echo "ambient-ctx-refused=ok"
# (f) --set 內部變數名拒收
SANDBOX_HOME="$T/home" KUBE_CONTEXT= bash "$SKILL/scripts/onboard.sh" --repo "$T/repo" \
  --set IMAGE_TAG=evil --non-interactive >/dev/null 2>&1; echo "denylist=$?"
rm -rf "$T"
```

Expected: `check1=1`、`onboard=0`、`check2=0`、檔案 `-rw-------`、兩個 grep 命中、`verbatim-kept=ok`、`ambient-ctx-refused=ok`、`denylist=1`。

- [ ] **Step 2：Commit（plugin repo）**

```bash
cd ~/.claude/plugins-local/woowprd
git add skills/k8s-dev-sandbox/scripts/onboard.sh
git commit -m "feat: 新增 onboard 子命令（profile 收集 / --check / 拒擷取 ambient context / deny-list）"
```

---

## Phase 4：woow repo 接入（第一個使用者）

### Task 4.1：`.sandbox.yml` 補宣告

**Files:**
- Modify: `woow_paas_platform/.sandbox.yml`（檔尾 append）

- [ ] **Step 1：append**

```yaml

# 執行期 env 宣告（onboard 收集 / create warn 用；值放 ~/.sandbox/{project-key}/config.env）
env_optional:
  - "AGENT_RUNTIME_NAMESPACE_SECRET: agent-runtime 啟用開關＋Odoo 端 namespace hash（僅 stg 模式生效；未設→agent-runtime 整段關閉，legacy AI chat 不受影響）"
  - "JCR_USER: JCR registry pull 帳號（僅 stg 模式生效；cluster 已有 jfrog-registry secret 時可不設）"
  - "JCR_PASS: JCR registry pull 密碼（僅 stg 模式生效；同上）"

# 宣告式 ir.config_parameter seed（stg only：ECPay 3DS 回呼需 https base url，
# 否則 Chrome Mixed Content 擋綁卡/加值；取代 deploy-stg.sh 舊 init_odoo_system_params。
# local 不設 —— 與既有行為對齊，local 沙盒從未 seed 過 base url）
config_params_stg:
  web.base.url: "https://{{slug}}.woowtech.io"
  web.base.url.freeze: "True"
```

- [ ] **Step 2：用 plugin parser 驗證解析**

```bash
bash -c 'source ~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/scripts/lib/common.sh
load_sandbox_yml "$(git rev-parse --show-toplevel)" >/dev/null
echo "OPT=[$SBX_ENV_OPTIONAL]"; SLUG=stg MODE=stg
merged_config_params stg; merged_config_params local'
```

Expected: OPT 含三個名稱；`merged stg` 兩行（url → `https://{{slug}}.woowtech.io`、freeze → True）；`merged local` 輸出空。

### Task 4.2：deploy-stg 收斂

**Files:**
- Modify: `woow_paas_platform/.claude/skills/deploy-stg/scripts/deploy-stg.sh:175-213,230-234,264,388`

- [ ] **Step 1：刪函式定義（`:175-213` 整段，含開頭說明註解）**，原位換成：

```bash
# web.base.url + freeze 的 seed 已宣告式化：.sandbox.yml 的 config_params_stg，
# 由 plugin create.sh 在部署時 diff-aware 套用（值變才 restart、失敗則 create 非零退出）。
```

- [ ] **Step 2：刪兩個呼叫點**

`:264` `    init_odoo_system_params "$SLUG"` → 整行刪除。
`:388` 與其上的 `# --- Initialize Odoo system parameters (web.base.url + freeze) ---` 註解 → 兩行刪除。

- [ ] **Step 3：改 `:230-234` Note 文案（profile 上線後原文與事實相反）**

舊訊息說「not set — stays disabled」；新文案：

```bash
if [ -z "${AGENT_RUNTIME_NAMESPACE_SECRET:-}" ]; then
    echo -e "${YELLOW}Note: AGENT_RUNTIME_NAMESPACE_SECRET 未在當下 env 偵測到。${NC}" >&2
    echo -e "${YELLOW}  若 onboard profile 已記錄，create.sh 載入後仍會啟用 agent-runtime；兩處都沒有則保持停用（legacy AI chat 不受影響）。${NC}" >&2
    echo -e "${YELLOW}  持久供值：/woowprd:k8s-dev-sandbox onboard --repo \"\$(git rev-parse --show-toplevel)\" --set AGENT_RUNTIME_NAMESPACE_SECRET=<value>${NC}" >&2
fi
```

- [ ] **Step 4：行為差異記錄（commit message 與本 plan 同步）**

1. restart 語義：舊＝每次 deploy 無條件 rollout restart；新＝值沒變不 restart（換 image 的 helm upgrade 本來就會換 pod）。
2. **失敗語義（重要）**：舊＝`set -e` 下 seed 失敗 deploy-stg 立即非零；新＝由 create.sh 的 `CONFIG_PARAMS_FAILED` gate 在結尾非零退出（deploy-stg `set -e` 接力傳播）——同樣 fail-loud，失敗點後移。
3. 舊的 `kubectl wait pg pod` 前置等待：plugin 路徑由 create.sh 既有的 pod-ready 等待涵蓋。

- [ ] **Step 5：修 `.sandbox/pre-install.sh` 的 `yaml_sq`（同 3.2 病灶）**

舊（`:24`）：`yaml_sq() { local v="${1:-}"; v="${v//\'/\'\'}"; printf "'%s'" "$v"; }`
新：`yaml_sq() { local v="${1:-}" q="'"; v="${v//$q/$q$q}"; printf "'%s'" "$v"; }`

- [ ] **Step 6：語法檢查 + Commit（woow repo，`feat/sandbox-onboarding`）**

```bash
bash -n .claude/skills/deploy-stg/scripts/deploy-stg.sh && bash -n .sandbox/pre-install.sh
git add .sandbox.yml .sandbox/pre-install.sh .claude/skills/deploy-stg/scripts/deploy-stg.sh
git commit -m "feat: .sandbox.yml 宣告 env_optional + config_params_stg；deploy-stg 移除 init_odoo_system_params"
```

### Task 4.3：CLAUDE.md 一句話

**Files:**
- Modify: `woow_paas_platform/CLAUDE.md`（K8s Dev Sandbox 段，plugin 指令清單內）

- [ ] **Step 1：在指令範例區塊內補**

```bash
# 一次性 onboarding（記錄執行期 env 到 ~/.sandbox/{project-key}/，worktree 共用）
/woowprd:k8s-dev-sandbox onboard --repo "$(git rev-parse --show-toplevel)" [--context <ctx>] [--set KEY=VALUE]
```

- [ ] **Step 2：Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 補 k8s-dev-sandbox onboard 用法"
```

---

## Phase 5：plugin SKILL.md 文件

### Task 5.1：SKILL.md 補 onboard + 三層模型

**Files:**
- Modify: `~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/SKILL.md`

- [ ] **Step 1：子命令表加一列**（`create` 列之前）：

```markdown
| `onboard`              | 一次性記錄執行期供值     | `scripts/onboard.sh` |
```

- [ ] **Step 2：「步驟 2：路由執行」加 onboard 小節**（`create` 小節之前）：

````markdown
#### `onboard` - 一次性記錄 project 執行期供值

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/k8s-dev-sandbox/scripts/onboard.sh" \
     --repo "$(git rev-parse --show-toplevel)" [--context <ctx>] [--set KEY=VALUE ...] [--check]
```

- 寫 `~/.sandbox/{project-key}/config.env`（chmod 600；project-key 由 origin remote URL
  推導 → 同 repo 全部 worktree / clone 共用）。值可為字面或 `!command`。
- **`!command` 在 create 的 kube context 切換之後才求值**（防跨 cluster 誤讀）；指令內
  仍建議顯式 `kubectl --context "$KUBE_CONTEXT"`。KUBE_CONTEXT 本身只能是字面值，且
  onboard 絕不從 ambient env 擷取它（只認 `--context` / `--set`）。
- `--check`：只列缺值不寫檔；有 `env_required` 缺值 → exit 1（agent 判斷用）。
- 之後 `create`/`up` 自動載入 profile；解析優先序 **CLI flag > 當下 env > profile >
  `.sandbox.yml` > 自動偵測**。`destroy` 刻意不吃 profile 的 KUBE_CONTEXT（防誤刪紅線）。
````

- [ ] **Step 3：「重要提醒」補三條**：

```markdown
- **三層設定模型**：①結構宣告 `.sandbox.yml`（repo、committed）②平台 seed `.sandbox/*.sh`
  （repo、committed）③執行期供值 `~/.sandbox/{project-key}/config.env`（user-scope、onboard 產物）。
  `.sandbox.yml` 可宣告 `env_required`（缺值 create fail-fast）/ `env_optional`（缺值 warn）
  — 項目格式 `"NAME: desc"`；`config_params(_{mode})` 宣告式 seed `ir.config_parameter`
  （支援 `{{slug}}`/`{{namespace}}`/`{{db_name}}`/`{{mode}}` 模板；mode 名含連字號時區塊名
  寫底線，如 `config_params_ci_local`）。
- **config_params 是 desired-state**：每次 create/up 都 diff-aware 收斂（值變才 restart、
  失敗 create 非零退出）——hook / 人工改過的同名 key 會被收斂回宣告值，**勿宣告人工也會
  改的 key**。文法限制：值不可含 `#`（行內註解剝除）、不支援多行值；`.sandbox.yml` 僅支援
  扁平 key＋宣告的 list/map 區塊，其他縮排結構 fail-loud。
- **profile 不進 git**：secrets / 本機絕對路徑住 user-scope profile；repo 內只放宣告。
  profile 不接受工具內部變數名（IMAGE_TAG 等 deny-list，防旗標注入）。
```

- [ ] **Step 4：Commit（plugin repo）**

```bash
cd ~/.claude/plugins-local/woowprd
git add skills/k8s-dev-sandbox/SKILL.md
git commit -m "docs: SKILL.md 補 onboard 子命令與三層設定模型"
```

---

## Phase 6：端到端驗收（docker-desktop；kubectl 一律顯式 `--context`）

### Task 6.1：fixture repo 全流程（驗收情境 1、3、5）

- [ ] **Step 1：建 fixture（root layout、合法技術名目錄）**

> **執行修訂（e2e 實撞）**：fixture **不可放 `/tmp`** —— local 模式 hostPath 由 Docker
> Desktop VM 的 kubelet 解析，macOS `/tmp` 不在 VM 檔案共享範圍 → pod 卡
> `FailedMount: hostPath type check failed`、Odoo 600s 不 ready。fixture 一律放
> `$HOME` 下（如 `$HOME/.sandbox-e2e/fixture_addon`）。這也順帶實證了 create.sh 的
> fail-loud 鏈：module install 失敗 → config_params 讀取失敗計數 → 結尾紅字 exit 1。

```bash
FIX="$HOME/.sandbox-e2e/fixture_addon" && rm -rf "$FIX" && mkdir -p "$FIX"
cat > "$FIX/__manifest__.py" <<'PY'
{'name': 'Fixture Addon', 'version': '1.0', 'depends': ['base'], 'installable': True}
PY
: > "$FIX/__init__.py"
cat > "$FIX/.sandbox.yml" <<'YML'
env_required:
  - "FIXTURE_TOKEN: e2e 測試用必要 env（驗 fail-fast / onboard 供值）"
config_params:
  web.base.url: "http://{{slug}}.local.test:8080"
  web.base.url.freeze: "True"
YML
cd "$FIX" && git init -q -b e2e-onboard && git add -A && git commit -qm init
```

- [ ] **Step 2：fail-fast（情境 3；`--pre` 段，不碰 cluster）**

Run: `bash $SKILL/scripts/create.sh --repo "$FIX" --context docker-desktop --yes`
Expected: 紅字列 `FIXTURE_TOKEN` 缺值 + onboard 指引，exit 1，**未跑 require_kube_context / helm**（無 !cmd → `--pre` 段直接擋）。

- [ ] **Step 3：onboard → create（profile 補 context；情境 1）**

```bash
bash "$SKILL/scripts/onboard.sh" --repo "$FIX" --context docker-desktop \
     --set FIXTURE_TOKEN=tok123 --non-interactive
bash "$SKILL/scripts/create.sh" --repo "$FIX" --yes        # 不帶 --context：profile 供
```

Expected: onboard exit 0（dry-run 切到 docker-desktop）；create 走到底、pod ready、module installed。

- [ ] **Step 4：驗 config_params 落 DB**

```bash
NS=dev-sandbox-e2e-onboard
PG=$(kubectl --context docker-desktop get statefulset -n $NS -l app.kubernetes.io/component=postgres -o jsonpath='{.items[0].metadata.name}')
kubectl --context docker-desktop exec -n $NS statefulset/$PG -- psql -U odoo -d sandbox_e2e_onboard -tAc \
  "SELECT key||'='||value FROM ir_config_parameter WHERE key LIKE 'web.base.url%' ORDER BY key"
```

Expected: `web.base.url=http://e2e-onboard.local.test:8080` + `web.base.url.freeze=True`。

- [ ] **Step 5：upgrade 路徑 diff-aware（情境 5；revision annotation 比對）**

```bash
DEP=$(kubectl --context docker-desktop get deploy -n "$NS" -l app.kubernetes.io/component=odoo -o jsonpath='{.items[0].metadata.name}')
rev() { kubectl --context docker-desktop get deploy -n "$NS" "$DEP" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}'; }
REV0=$(rev)
bash "$SKILL/scripts/create.sh" --repo "$FIX" --yes      # 無變更 → 不 restart
[ "$(rev)" = "$REV0" ] && echo "no-restart ok"
sed -i '' 's#local.test:8080#local.test:9090#' "$FIX/.sandbox.yml" 2>/dev/null || sed -i 's#local.test:8080#local.test:9090#' "$FIX/.sandbox.yml"
bash "$SKILL/scripts/create.sh" --repo "$FIX" --yes      # 值變 → 更新 + restart 一次
kubectl --context docker-desktop exec -n $NS statefulset/$PG -- psql -U odoo -d sandbox_e2e_onboard -tAc \
  "SELECT value FROM ir_config_parameter WHERE key='web.base.url'"
[ "$(rev)" = "$((REV0+1))" ] && echo "restart-once ok"
```

Expected: `no-restart ok`；第二次 create 印 `↻ web.base.url = ...:9090（收斂漂移值...）` 或 `+`、值更新為 `:9090`、`restart-once ok`。

- [ ] **Step 6：清理**

Run: `bash $SKILL/scripts/destroy.sh e2e-onboard --repo "$FIX" --context docker-desktop --force && rm -rf "$FIX" ~/.sandbox/fixture_addon-*`

### Task 6.2：worktree / 本 repo 驗證（情境 2 部分覆蓋）

- [ ] **Step 1：key 收斂驗證**

```bash
bash -c 'source ~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/scripts/lib/common.sh
echo "woow floor key = $(compute_project_key "$(git rev-parse --show-toplevel)")"'
```

Expected: 與另一個指向同 origin 的 checkout（另一 Floor / 主 repo）算出同一 key。
（**情境 2 完整版**——兩 worktree 各自 up、各掛各的 src——Task 1.1 已單測 key 收斂 +
Task 6.1 已驗單沙盒全流程，雙沙盒並行實跑列 follow-up，不阻擋本 plan。）

- [ ] **Step 2：deploy-stg 回歸（情境 4，延後執行）**

下次真正跑 `/woow:deploy-stg`（或 `--image` 變體）時驗：
`kubectl --context woow-k3s exec -n dev-sandbox-stg <pg> -- psql -U odoo -d woow_stg -tAc "SELECT key||'='||value FROM ir_config_parameter WHERE key LIKE 'web.base.url%'"`
Expected: `web.base.url=https://stg.woowtech.io` + `freeze=True`（來源改為宣告式）。**本 plan 不主動觸發 stg 部署**（成本高、影響共用環境）；標記為下次 deploy-stg 的驗證項。

---

## Self-Review

- **Spec coverage：** 設計各機制 → Phase 1（宣告/profile/key）/ Phase 2（整合）/ Phase 3（onboard）/ Phase 4（woow）/ Phase 5（文件）；D1-D7：D1（改點④⑤+merged）、D2（兩段式 !cmd + verbatim 保留；**修訂**：求值時機後移至 context 切換後）、D3（compute_project_key + `pwd -P`）、D4（雙區塊）、D5（Task 2.2 hook 前 + **desired-state 收斂語義明確化**）、D6（destroy 不載 profile + onboard 拒擷取 ambient context）、D7（全落 plugin）。✅
- **驗收覆蓋（誠實版）：** 情境 1（fixture 全流程，`test` 子命令不在範圍）、3、5 → Task 6.1；情境 2 **部分覆蓋**（key 收斂單測+驗證，雙沙盒並行 follow-up）；情境 4 延後至下次 deploy-stg。
- **Placeholder scan：** 全部 task 含完整碼與預期輸出。✅
- **Type consistency：** `SBX_ENV_REQUIRED`/`SBX_ENV_OPTIONAL`（`NAME\tdesc\n`）、`SBX_CONFIG_PARAMS_RAW`（`scope\tkey\tvalue\n`）、`PROFILE_PENDING_CMDS`（`key=cmd\n`）、`CONFIG_PARAMS_CHANGED`/`CONFIG_PARAMS_FAILED` 旗標（函式恆 return 0）——產出端與消費端格式一致。✅
- **bash 3.2 紀律：** 無 `declare -A`/nameref；newline-string 取代空 array；單引號跳脫一律 `$q` idiom（含修既有 yaml_sq 與 woow pre-install.sh）；測試顯式 `/bin/bash` 跑。✅
- **已知留尾（follow-up）：** parser 再擴就抽 python（design 風險 #1）；`config_params` 值含 `#` / 多行不支援；`env_optional_{mode}`（per-mode env 宣告）；extra_addons 的 `${VAR}` 展開（design F5 敘事缺口，design doc 已同步修正）；雙 worktree 並行 e2e；deploy-stg 真 stg 回歸。

## Execution Handoff

Plan 存於 `docs/plans/2026-Q2/2026-06-12-sandbox-onboarding-implementation.md`。執行採 Inline（同 session、interlocked bash 檔不適合平行 subagent），每 Phase 末驗證 + commit。

## 執行結果（2026-06-12，全 Phase 完成）

| Phase | 結果 | Commit |
|-------|------|--------|
| 1 common.sh | ✅ 單測雙 bash（/bin/bash 3.2 + Homebrew 5.3）ALL PASS，含既有 yaml_sq 3.2 紅轉綠 | plugin `8b2d215` |
| 2 create.sh | ✅ `bash -n` 雙版本過 | plugin `72624c0` |
| 3 onboard.sh | ✅ 煙霧六情境全過（check1=1 / onboard=0 / check2=0 / chmod 600 / verbatim-kept / ambient-ctx-refused / denylist=1） | plugin `6f6494b` |
| 4 woow repo | ✅ parser 實解 woow 宣告（OPT 三筆、merged stg 兩行、render=`https://stg.woowtech.io`、merged local 空） | woow `827c6ea`、`1e0bacc` |
| 5 SKILL.md | ✅ | plugin `1506920` |
| 6 e2e（docker-desktop） | ✅ 全鏈：fail-fast（不碰 cluster、onboard 指引）→ onboard → create 零 `--context`（profile 補）→ module installed → config_params 落 DB（含「收斂漂移值」訊息：Odoo init 自寫的 `http://localhost:8069` 被收斂）→ 無變更重跑「不變」不 restart（rev 持平）→ 改值重跑 `↻ ...:9090` + restart 恰一次（rev+1）→ destroy 零殘留 | — |

e2e 額外實證：
- **fixture 不可放 `/tmp`**（Docker Desktop VM hostPath 看不到 → FailedMount → Odoo 600s 不 ready）；第一輪失敗同時實證了 **fail-loud 鏈**：install 失敗 → config_params 讀取失敗計數 → create 結尾紅字 exit 1（正是 design 要的不可靜默降級）。
- woow floor project-key = `woow_paas_platform-a1ebac6d`（remote URL 推導；所有 Floor / worktree / clone 收斂同 key）。

**Deferred（不阻擋）**：deploy-stg 真 stg 回歸（下次 `/woow:deploy-stg` 時驗 `web.base.url=https://stg.woowtech.io`）；雙 worktree 並行 up e2e；follow-up 清單見 Self-Review。
