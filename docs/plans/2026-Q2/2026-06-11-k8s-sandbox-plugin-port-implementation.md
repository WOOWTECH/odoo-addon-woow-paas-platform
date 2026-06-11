# k8s-dev-sandbox 移植 woowprd Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推薦）或 superpowers:executing-plans 逐 task 執行。步驟用 checkbox（`- [ ]`）追蹤。

**Goal:** 把 in-repo 的 `/k8s-dev-sandbox` skill 移植成 `woowprd` plugin 內的通用 `/woowprd:k8s-dev-sandbox`，讓任意 Odoo addon repo（src 單一 / repo 根即 addon / 多 addon 子目錄三樣態）都能開 K8s 沙盒跑 e2e，並把 woow 平台特化 seed 剝離成 opt-in hook。

**Architecture:** 三層解耦 —— ① 工具自帶資產（helm chart、scripts）由 `BASH_SOURCE` 推 `PLUGIN_SKILL_ROOT`（零 env 依賴）定位；② 被測使用者 repo 由 `resolve_repo_root`（`--repo` → `$CLAUDE_PROJECT_DIR` → `git rev-parse`，fail-loud）定位；③ woow 平台 seed 移到 `${REPO_ROOT}/.sandbox/post-create.sh`，通用主線零 woow 分支。共用邏輯抽進 `lib/common.sh`。chart 以新 values key 參數化三樣態掛載與去品牌。

**Tech Stack:** Bash（`set -euo pipefail`）、Helm 3、Kubernetes、Claude Code plugin（skills/ 自動發現、`${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PROJECT_DIR}`）。

**Spec:** `docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-design.md`

**Branch:** `docs/k8s-sandbox-plugin-port`（design 已 commit；本 implementation 為其雙檔之 implementation 半）。實作落地建議另開 `feat/k8s-sandbox-plugin-port`。

---

## 前置事實（grounding 已查證）

- in-repo 7 支 script 有 `PROJECT_ROOT=$(cd "${SCRIPT_DIR}/../../../.." && pwd)` 四層上推（`destroy:31-32` / `status:29-30` / `logs:35-36` / `extend:31-32` / `test:33-34` / `build:16-17` / `create:35-36`；`list.sh` 無）。
- `e2e-seed.sh`、`seed-from-stg.sh` 綁 `woow_paas_platform.skill` model / stg 環境 / ECPay-operator → **externalize-woow-only，不進通用 plugin**。
- chart 已有接縫：`codeMount.{hostPath,moduleName,extraAddonsHostPath}`、`odoo.addonsPath`；品牌硬編集中於 `_helpers.tpl`（`woow_` db 前綴 :69、`dev-sandbox-` ns 前綴 :58）、`sandbox.woow.tw/*` label/annotation（散在 `values.yaml:13` / `namespace.yaml:12-23` / `ttl-cronjob.yaml:77`）、`extraPipRequirementsFile`（`values.yaml:32` 內嵌 module 名）。
- `ttl-cronjob.yaml` 的 `bitnami/kubectl:latest` → **已在 in-repo 修為 values-driven `ttl.image` 預設 `bitnamilegacy/kubectl:1.33.4-debian-12-r0`（PR #177，cp 時自然帶入，本 plan 不重做）**。
- 寫死 fullname 後綴 `-odoo-dev-sandbox`（`status:286` / `logs:124` / `test:110`）字面串接不可靠（helm fullname `trunc 43`，長 slug 截斷）；`e2e-seed.sh:172` 已用 label selector 動態查，為正解範本。

---

## File Structure

| File | 責任 | 動作 |
|------|------|------|
| `~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/scripts/lib/common.sh` | 雙根定位 / slug / db / detect_layout / load_sandbox_yml / context guard / yaml_sq | Create |
| `…/scripts/lib/common_test.sh` | common.sh 函式單測（bash 斷言，無外部框架） | Create |
| `…/scripts/{create,destroy,list,status,logs,extend,test,build}.sh` | 8 支子命令（由 in-repo 複製後改標頭 + 泛化） | Create（移植） |
| `…/charts/odoo-dev-sandbox/**` | 整包 bundle 進 plugin（含 PR #177 的 ttl.image 修正） | Create（複製 + 參數化） |
| `…/SKILL.md` | frontmatter `name: k8s-dev-sandbox`、子命令路由、`${CLAUDE_PLUGIN_ROOT}` + `--repo` 呼叫 | Create |
| `charts/odoo-dev-sandbox/values.yaml` | 新增 11 個 values key（見 Phase 2） | Modify（in-repo） |
| `charts/odoo-dev-sandbox/templates/{_helpers.tpl,odoo-deployment.yaml,odoo-configmap.yaml,namespace.yaml,registry-secret.yaml}` | 參數化 + 去品牌 + range 掛載 | Modify（in-repo） |
| `woow_paas_platform/.sandbox.yml` | woow repo 顯式宣告（db_prefix woow_、module、hook） | Create（in-repo） |
| `woow_paas_platform/.sandbox/post-create.sh` | 舊 create.sh 平台 seed 段（operator/ECPay/AI/journal） | Create（in-repo） |
| `woow_paas_platform/.claude/skills/k8s-dev-sandbox/` | 端到端驗收後移除（避免雙寫漂移） | Delete（最後） |
| `woow_paas_platform/CLAUDE.md` | k8s-dev-sandbox 範例改 plugin 叫法 | Modify（最後） |

**驗證手段（本 plan 的「測試」）：** ① bash 函式 → `bash …/lib/common_test.sh`（斷言）；② chart → `helm lint` + `helm template … | grep`；③ plugin 變數 → 探針 echo；④ 端到端 → 三樣態各建一個沙盒驗 `ir_module_module.state='installed'`。

---

## Phase 0：驗 plugin 變數模型（linchpin，最低成本最高槓桿）

> 設計文件已用官方文件確認 `${CLAUDE_PLUGIN_ROOT}` 為 env var、cwd = `${CLAUDE_PROJECT_DIR}`；本 phase 在目標 Claude Code 版本實測一次定生死，**沒過不要往下做**。

### Task 0.1：最小探針 skill

**Files:** Create `~/.claude/plugins-local/woowprd/skills/_probe/SKILL.md`

- [ ] **Step 1：建探針 skill**

```markdown
---
name: _probe
description: 暫時探針，驗 plugin 變數。用完即刪。
---
# Probe
執行：`bash "${CLAUDE_PLUGIN_ROOT}/skills/_probe/echo.sh"`
```

並建 `echo.sh`：

```bash
#!/usr/bin/env bash
echo "CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-<unset>}"
echo "CLAUDE_PROJECT_DIR=${CLAUDE_PROJECT_DIR:-<unset>}"
echo "PWD=$(pwd)"
echo "BASH_SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "git_toplevel=$(git rev-parse --show-toplevel 2>/dev/null || echo '<not a git repo>')"
```

- [ ] **Step 2：reload 並在 woow repo 內觸發**

Run（在 `woow_paas_platform` 目錄）：`/reload-plugins` 後 `/woowprd:_probe`
Expected：`CLAUDE_PLUGIN_ROOT` 指向 plugin 安裝根；`PWD` / `git_toplevel` 指向 `woow_paas_platform`；`BASH_SOURCE_DIR` 指向 plugin。

- [ ] **Step 3：判定**

若 `CLAUDE_PLUGIN_ROOT` unset → 改全程靠 `BASH_SOURCE`（plan 已如此設計，不受影響）。
若 `PWD` / `git_toplevel` 非使用者 repo → SKILL.md 強制要求 `--repo`（plan 已帶）。

- [ ] **Step 4：刪探針**

Run：`rm -rf ~/.claude/plugins-local/woowprd/skills/_probe`

---

## Phase 1：`lib/common.sh` 共用骨架

抽 8 項共用邏輯（`normalize_slug` 7 支重複、`PROJECT_ROOT` 四層上推、`to_epoch`/`format_duration` list/status 兩份重複、`LABEL_DOMAIN` 散落、fullname 字面串接不可靠）。

### Task 1.1：寫 `lib/common.sh`

**Files:** Create `…/skills/k8s-dev-sandbox/scripts/lib/common.sh`

- [ ] **Step 1：寫入以下完整內容**

```bash
#!/usr/bin/env bash
#
# lib/common.sh — shared helpers for the k8s-dev-sandbox plugin skill.
#
# Source me, don't execute me:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
#
# Every function is self-contained and may be called individually after
# sourcing. Functions print to stdout the value they resolve and return 0;
# on error they print to stderr (colourised) and return non-zero — the
# CALLER decides whether to `|| exit`. This keeps the lib `set -euo pipefail`
# friendly without forcing a hard exit from inside a library.
#
# Design reference:
#   docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-design.md
#
# The "雙根" (dual-root) split is the whole point of this file:
#   - PLUGIN_SKILL_ROOT : where the tool's own assets live (chart, sibling
#                         scripts). Derived from BASH_SOURCE, ZERO env deps.
#   - REPO_ROOT         : the user's addon repo (src/, .env, git branch,
#                         extra-addons). Resolved via --repo / CLAUDE_PROJECT_DIR
#                         / git, fail-loud, NEVER falling back to the plugin root.

# Guard against double-sourcing (idempotent).
if [[ -n "${_K8S_SANDBOX_COMMON_SH_LOADED:-}" ]]; then
    return 0 2>/dev/null || true
fi
_K8S_SANDBOX_COMMON_SH_LOADED=1

# --- Colours (only define if the caller hasn't already) -----------------------
: "${RED:=$'\033[0;31m'}"
: "${GREEN:=$'\033[0;32m'}"
: "${YELLOW:=$'\033[1;33m'}"
: "${BLUE:=$'\033[0;34m'}"
: "${NC:=$'\033[0m'}"

# Internal logging helpers — all to stderr so they never pollute a function's
# stdout "return value" (which callers capture via $(...)).
_log_err()  { printf '%b%s%b\n'  "$RED"    "$*" "$NC" >&2; }
_log_warn() { printf '%b%s%b\n'  "$YELLOW" "$*" "$NC" >&2; }
_log_info() { printf '%b%s%b\n'  "$BLUE"   "$*" "$NC" >&2; }
_log_ok()   { printf '%b%s%b\n'  "$GREEN"  "$*" "$NC" >&2; }

# =============================================================================
# (a) resolve_plugin_root  — BASH_SOURCE 推導，零 env 依賴
# =============================================================================
# Layout assumed:
#   <skill-root>/scripts/lib/common.sh   <- BASH_SOURCE[0] of THIS file
#   <skill-root>/scripts/create.sh ...
#   <skill-root>/charts/odoo-dev-sandbox/
resolve_plugin_root() {
    local lib_dir skill_root
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || {
        _log_err "resolve_plugin_root: cannot resolve directory of common.sh"
        return 1
    }
    # scripts/lib -> scripts -> skill root  (two levels up from lib/)
    skill_root="$(cd "${lib_dir}/../.." && pwd)" || {
        _log_err "resolve_plugin_root: cannot ascend to skill root from ${lib_dir}"
        return 1
    }
    if [[ ! -d "${skill_root}/charts/odoo-dev-sandbox" ]]; then
        _log_err "resolve_plugin_root: bundled chart not found at ${skill_root}/charts/odoo-dev-sandbox"
        _log_err "  (is the chart properly bundled into the plugin? see packaging steps)"
        return 1
    fi
    printf '%s\n' "$skill_root"
}

# =============================================================================
# (b) resolve_repo_root  — fail-loud，絕不 fallback 到 plugin 根
# =============================================================================
# Priority: --repo <path> -> ${CLAUDE_PROJECT_DIR} -> git -C "$PWD" rev-parse
# --show-toplevel -> fail-loud. After resolution, sanity-guards that the root
# looks like an Odoo addon repo (src/__manifest__.py | ./__manifest__.py |
# <child>/__manifest__.py).
resolve_repo_root() {
    local repo_flag="" root=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --repo)
                repo_flag="${2:-}"
                shift 2 || { _log_err "resolve_repo_root: --repo needs a value"; return 1; }
                ;;
            *) shift ;;
        esac
    done

    if [[ -n "$repo_flag" ]]; then
        if [[ ! -d "$repo_flag" ]]; then
            _log_err "resolve_repo_root: --repo path does not exist: ${repo_flag}"
            return 1
        fi
        root="$(cd "$repo_flag" && pwd)"
    elif [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
        if [[ ! -d "$CLAUDE_PROJECT_DIR" ]]; then
            _log_err "resolve_repo_root: CLAUDE_PROJECT_DIR set but missing: ${CLAUDE_PROJECT_DIR}"
            return 1
        fi
        root="$(cd "$CLAUDE_PROJECT_DIR" && pwd)"
    elif root="$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null)"; then
        :
    else
        _log_err "resolve_repo_root: could not locate the user addon repo."
        _log_err "  Tried (in order): --repo flag, \$CLAUDE_PROJECT_DIR, git toplevel of \$PWD."
        _log_err "  Fix: pass --repo \"\$(git rev-parse --show-toplevel)\" or run inside a git repo."
        return 1
    fi

    # Hard guard: never accept the plugin root itself as the repo root.
    local plugin_root=""
    plugin_root="$(resolve_plugin_root 2>/dev/null || true)"
    if [[ -n "$plugin_root" && "$root" == "$plugin_root" ]]; then
        _log_err "resolve_repo_root: refusing to use the plugin root as the repo root: ${root}"
        _log_err "  Pass --repo \"\$(git rev-parse --show-toplevel)\" pointing at your addon repo."
        return 1
    fi

    if [[ -f "${root}/src/__manifest__.py" ]] || [[ -f "${root}/__manifest__.py" ]]; then
        printf '%s\n' "$root"; return 0
    fi
    local child
    for child in "${root}"/*/; do
        [[ -d "$child" ]] || continue
        if [[ -f "${child}__manifest__.py" ]]; then
            printf '%s\n' "$root"; return 0
        fi
    done

    _log_err "resolve_repo_root: '${root}' does not look like an Odoo addon repo."
    _log_err "  Expected one of: src/__manifest__.py, ./__manifest__.py, or <child>/__manifest__.py."
    _log_err "  Wrong repo? Pass --repo <path-to-addon-repo> explicitly."
    return 1
}

# =============================================================================
# (c) compute_slug  — 與 in-repo create.sh 完全相同的正規化
# =============================================================================
compute_slug() {
    local branch="${1:-}"
    if [[ -z "$branch" ]]; then
        local where="${REPO_ROOT:-$PWD}"
        branch="$(git -C "$where" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"
    fi
    local slug
    slug="$(printf '%s' "$branch" \
        | sed 's/[^a-zA-Z0-9]/-/g' \
        | tr '[:upper:]' '[:lower:]' \
        | sed 's/-\{2,\}/-/g' \
        | sed 's/^-//;s/-$//' \
        | cut -c1-40)"
    if [[ -z "$slug" ]]; then
        _log_err "compute_slug: branch '${branch}' normalised to an empty slug"
        return 1
    fi
    printf '%s\n' "$slug"
}

# =============================================================================
# (d) compute_db_name  — 前綴可設，預設中性 sandbox_
# =============================================================================
# 注意：create.sh 會把結果以 --set postgres.database="$DB_NAME" 傳給 chart，
# 讓 script 成為 DB 名的「單一真相來源」，消除與 _helpers.tpl dbName 前綴的
# lock-step 風險（chart 顯式 database 優先於 dbPrefix 計算）。
compute_db_name() {
    local slug="${1:-}"
    local prefix="${2:-${DB_PREFIX:-sandbox_}}"
    if [[ -z "$slug" ]]; then
        _log_err "compute_db_name: slug argument is required"; return 1
    fi
    printf '%s\n' "${prefix}$(printf '%s' "$slug" | sed 's/-/_/g')"
}

# =============================================================================
# (e) detect_layout  — (b)->(a)->(c) 命中即停，fail-loud
# =============================================================================
# Prints ONE line: "<layout>\t<modules-csv>"
#   root/src : modules-csv = 單一 module 技術名（=目錄名；(a) 不可用 "src"）
#   multi    : modules-csv = 逗號接的合格子目錄名
detect_layout() {
    local repo="${1:-${REPO_ROOT:-}}"
    if [[ -z "$repo" || ! -d "$repo" ]]; then
        _log_err "detect_layout: repo root '${repo}' is not a directory"; return 1
    fi
    repo="$(cd "$repo" && pwd)"

    _is_valid_module_name() { [[ "$1" =~ ^[a-z][a-z0-9_]*$ ]]; }
    local -a IGNORE=(setup docs doc scripts tests test .git .github .gitea __pycache__ node_modules .venv venv)
    _is_ignored() {
        local name="$1" ig
        for ig in "${IGNORE[@]}"; do [[ "$name" == "$ig" ]] && return 0; done
        return 1
    }

    if [[ -f "${repo}/__manifest__.py" ]]; then
        printf '%s\t%s\n' "root" "$(basename "$repo")"; return 0
    fi
    if [[ -f "${repo}/src/__manifest__.py" ]]; then
        printf '%s\t%s\n' "src" "$(basename "$repo")"; return 0
    fi

    local -a accepted=() rejected=()
    local child name
    for child in "${repo}"/*/; do
        [[ -d "$child" ]] || continue
        name="$(basename "$child")"
        if _is_ignored "$name"; then rejected+=("${name} (ignored dir)"); continue; fi
        if [[ ! -f "${child}__manifest__.py" ]]; then rejected+=("${name} (no __manifest__.py)"); continue; fi
        if ! _is_valid_module_name "$name"; then rejected+=("${name} (invalid technical name)"); continue; fi
        accepted+=("$name")
    done

    if [[ ${#accepted[@]} -gt 0 ]]; then
        _log_info "detect_layout: multi-addon repo; accepted: ${accepted[*]}"
        [[ ${#rejected[@]} -gt 0 ]] && _log_warn "detect_layout: rejected: ${rejected[*]}"
        local IFS=,
        printf '%s\t%s\n' "multi" "${accepted[*]}"; return 0
    fi

    _log_err "detect_layout: no Odoo addon found under '${repo}'."
    _log_err "  Checked: ./__manifest__.py (root), ./src/__manifest__.py (src), first-level children (multi)."
    if [[ ${#rejected[@]} -gt 0 ]]; then
        _log_err "  Rejected children:"
        local r; for r in "${rejected[@]}"; do _log_err "    - ${r}"; done
    fi
    _log_err "  Fix: run from an addon repo, or declare layout/module in \${REPO_ROOT}/.sandbox.yml."
    return 1
}

# =============================================================================
# (f) load_sandbox_yml  — 無 yq 依賴的扁平 key parser；缺值退回偵測
# =============================================================================
# 設定（呼叫後 export）：SBX_LAYOUT / SBX_MODULE / SBX_INSTALL_MODULES（永遠前置
# base,web）/ SBX_DB_PREFIX（預設 sandbox_）/ SBX_EXTRA_ADDONS（空格分隔，絕對化）
# / SBX_POST_CREATE_HOOK。
load_sandbox_yml() {
    local repo="${1:-${REPO_ROOT:-}}"
    if [[ -z "$repo" || ! -d "$repo" ]]; then
        _log_err "load_sandbox_yml: repo root '${repo}' is not a directory"; return 1
    fi
    repo="$(cd "$repo" && pwd)"
    local yml="${repo}/.sandbox.yml"

    SBX_LAYOUT="" SBX_MODULE="" SBX_INSTALL_MODULES=""
    SBX_DB_PREFIX="" SBX_EXTRA_ADDONS="" SBX_POST_CREATE_HOOK=""
    local yml_install_modules="" yml_extra_addons=""

    if [[ -f "$yml" ]]; then
        _log_info "load_sandbox_yml: reading ${yml}"
        local line key val cur_list=""
        while IFS= read -r line || [[ -n "$line" ]]; do
            line="${line%$'\r'}"; line="${line%%#*}"
            if [[ "$line" =~ ^[[:space:]]+-[[:space:]]*(.+)$ ]]; then
                val="${BASH_REMATCH[1]}"
                val="${val%"${val##*[![:space:]]}"}"; val="${val#"${val%%[![:space:]]*}"}"
                val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
                case "$cur_list" in
                    install_modules) yml_install_modules="${yml_install_modules:+$yml_install_modules,}${val}" ;;
                    extra_addons)    yml_extra_addons="${yml_extra_addons:+$yml_extra_addons }${val}" ;;
                esac
                continue
            fi
            if [[ "$line" =~ ^([a-zA-Z_][a-zA-Z0-9_]*):[[:space:]]*(.*)$ ]]; then
                key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
                val="${val%"${val##*[![:space:]]}"}"
                val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
                cur_list=""
                case "$key" in
                    layout)           SBX_LAYOUT="$val" ;;
                    module)           SBX_MODULE="$val" ;;
                    db_prefix)        SBX_DB_PREFIX="$val" ;;
                    post_create_hook) SBX_POST_CREATE_HOOK="$val" ;;
                    install_modules)
                        cur_list="install_modules"
                        if [[ "$val" =~ ^\[(.*)\]$ ]]; then
                            local inner="${BASH_REMATCH[1]}" item; IFS=',' read -ra _items <<< "$inner"
                            for item in "${_items[@]}"; do
                                item="${item#"${item%%[![:space:]]*}"}"; item="${item%"${item##*[![:space:]]}"}"
                                [[ -n "$item" ]] && yml_install_modules="${yml_install_modules:+$yml_install_modules,}${item}"
                            done
                            cur_list=""
                        fi
                        ;;
                    extra_addons)
                        cur_list="extra_addons"
                        if [[ "$val" =~ ^\[(.*)\]$ ]]; then
                            local inner="${BASH_REMATCH[1]}" item; IFS=',' read -ra _items <<< "$inner"
                            for item in "${_items[@]}"; do
                                item="${item#"${item%%[![:space:]]*}"}"; item="${item%"${item##*[![:space:]]}"}"
                                [[ -n "$item" ]] && yml_extra_addons="${yml_extra_addons:+$yml_extra_addons }${item}"
                            done
                            cur_list=""
                        fi
                        ;;
                esac
            fi
        done < "$yml"
    else
        _log_info "load_sandbox_yml: no .sandbox.yml in repo; using auto-detection."
    fi

    if [[ -z "$SBX_LAYOUT" || -z "$SBX_MODULE" ]]; then
        local det layout det_modules
        det="$(detect_layout "$repo")" || return 1
        layout="${det%%$'\t'*}"; det_modules="${det#*$'\t'}"
        [[ -z "$SBX_LAYOUT" ]] && SBX_LAYOUT="$layout"
        [[ -z "$SBX_MODULE" ]] && SBX_MODULE="${det_modules%%,*}"
        [[ -z "$yml_install_modules" ]] && yml_install_modules="$det_modules"
    fi

    [[ -z "$yml_install_modules" ]] && yml_install_modules="$SBX_MODULE"
    # Ensure leading "base,web," exactly once.
    yml_install_modules="${yml_install_modules#base,}"; yml_install_modules="${yml_install_modules#web,}"
    SBX_INSTALL_MODULES="base,web,${yml_install_modules}"

    SBX_DB_PREFIX="${SBX_DB_PREFIX:-sandbox_}"

    local resolved="" src abs
    for src in $yml_extra_addons; do
        case "$src" in /*) abs="$src" ;; *) abs="${repo}/${src}" ;; esac
        if [[ ! -d "$abs" ]]; then
            _log_err "load_sandbox_yml: extra_addons source does not exist: ${src} (resolved: ${abs})"; return 1
        fi
        abs="$(cd "$abs" && pwd)"
        if ! find "$abs" -maxdepth 2 -name '__manifest__.py' -print -quit 2>/dev/null | grep -q .; then
            _log_err "load_sandbox_yml: extra_addons source has NO addon (no __manifest__.py): ${abs}"
            _log_err "  An empty-but-present dir would silently install nothing — refusing."
            return 1
        fi
        resolved="${resolved:+$resolved }${abs}"
    done
    SBX_EXTRA_ADDONS="$resolved"

    export SBX_LAYOUT SBX_MODULE SBX_INSTALL_MODULES SBX_DB_PREFIX SBX_EXTRA_ADDONS SBX_POST_CREATE_HOOK
    _log_ok "load_sandbox_yml: layout=${SBX_LAYOUT} module=${SBX_MODULE} install=[${SBX_INSTALL_MODULES}] db_prefix=${SBX_DB_PREFIX}"
    return 0
}

# =============================================================================
# require_kube_context — 沿用 in-repo create/destroy 的 context 安全 pattern
# =============================================================================
require_kube_context() {
    local ctx="${1:-}"
    if ! command -v kubectl >/dev/null 2>&1; then _log_err "kubectl not found."; return 1; fi
    if [[ -z "$ctx" ]]; then
        _log_err "ERROR: kubectl context is required (use --context <name> or export KUBE_CONTEXT)."
        _log_warn "This is required to prevent accidental cross-cluster deploys."
        kubectl config get-contexts -o name 2>/dev/null | sed 's/^/  /' >&2 || true
        return 1
    fi
    if ! kubectl config get-contexts -o name 2>/dev/null | grep -qx "$ctx"; then
        _log_err "ERROR: kubectl context '${ctx}' not found in kubeconfig."
        kubectl config get-contexts -o name 2>/dev/null | sed 's/^/  /' >&2 || true
        return 1
    fi
    _log_info "Using kubectl context: ${ctx}"
    kubectl config use-context "$ctx" >/dev/null
    export KUBE_CONTEXT="$ctx"
    if ! kubectl --request-timeout=30s cluster-info >/dev/null 2>&1; then
        _log_err "Cannot connect to Kubernetes cluster '${ctx}'."; return 1
    fi
    return 0
}

# =============================================================================
# resolve_workload — 用 component label 動態查實名，取代字面 fullname 後綴串接
# =============================================================================
# helm fullname trunc 43，長 slug 字面串接會壞；e2e-seed.sh:172 已是正解範本。
# Usage: resolve_workload <namespace> <kind> <slug> <component>
#   kind=deploy|statefulset|svc|secret ; component=odoo|postgres|nginx
resolve_workload() {
    local ns="$1" kind="$2" slug="$3" comp="$4"
    kubectl get "$kind" -n "$ns" \
        -l "app.kubernetes.io/instance=${slug},app.kubernetes.io/component=${comp}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

# =============================================================================
# yaml_sq — single-quoted YAML scalar emitter (verbatim from in-repo create.sh)
# =============================================================================
yaml_sq() {
    local v="${1:-}"
    v="${v//\'/\'\'}"
    printf "'%s'" "$v"
}
```

- [ ] **Step 2：`shellcheck` 過關**

Run：`shellcheck -x …/scripts/lib/common.sh`
Expected：no errors（warnings 可酌情處理）。

- [ ] **Step 3：Commit**

```bash
git add skills/k8s-dev-sandbox/scripts/lib/common.sh
git commit -m "feat: 新增 k8s-dev-sandbox plugin lib/common.sh 雙根與偵測骨架"
```

### Task 1.2：`common_test.sh` 函式單測（先寫測試）

**Files:** Create `…/scripts/lib/common_test.sh`

- [ ] **Step 1：寫測試（建臨時 fixture repo 三樣態 + 斷言）**

```bash
#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/common.sh"
fails=0
assert_eq() { [[ "$2" == "$3" ]] && echo "ok: $1" || { echo "FAIL: $1 — got [$2] want [$3]"; fails=$((fails+1)); }; }

# compute_slug
assert_eq "slug feature/Login" "$(compute_slug 'feature/Login')" "feature-login"
# compute_db_name default + explicit
assert_eq "db default" "$(compute_db_name 'my-feat')" "sandbox_my_feat"
assert_eq "db woow_"   "$(compute_db_name 'my-feat' 'woow_')" "woow_my_feat"

# detect_layout — build three fixtures under a tmp dir
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/a/src"; : > "$TMP/a/src/__manifest__.py"
assert_eq "layout a(src)" "$(detect_layout "$TMP/a" | cut -f1)" "src"
mkdir -p "$TMP/b"; : > "$TMP/b/__manifest__.py"
assert_eq "layout b(root)" "$(detect_layout "$TMP/b" | cut -f1)" "root"
mkdir -p "$TMP/c/mod_one" "$TMP/c/docs" "$TMP/c/not-a-mod"
: > "$TMP/c/mod_one/__manifest__.py"; : > "$TMP/c/not-a-mod/__manifest__.py"
out="$(detect_layout "$TMP/c")"
assert_eq "layout c(multi)" "$(printf '%s' "$out" | cut -f1)" "multi"
assert_eq "c filters non-name" "$(printf '%s' "$out" | cut -f2)" "mod_one"   # not-a-mod 被技術名規則刷掉

# resolve_repo_root fail-loud on non-addon dir
mkdir -p "$TMP/empty"
if resolve_repo_root --repo "$TMP/empty" >/dev/null 2>&1; then echo "FAIL: empty dir accepted"; fails=$((fails+1)); else echo "ok: empty dir rejected"; fi

# load_sandbox_yml install_modules 前置 base,web
REPO_ROOT="$TMP/a"; load_sandbox_yml "$TMP/a" >/dev/null 2>&1
assert_eq "install base,web prefix" "${SBX_INSTALL_MODULES:0:9}" "base,web,"

[[ $fails -eq 0 ]] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
```

- [ ] **Step 2：跑測試確認全綠**

Run：`bash …/scripts/lib/common_test.sh`
Expected：`ALL PASS`。若紅 → 修 common.sh 對應函式（尤其 detect_layout 的技術名過濾、install_modules 前置）。

- [ ] **Step 3：Commit**

```bash
git add skills/k8s-dev-sandbox/scripts/lib/common_test.sh
git commit -m "test: 新增 lib/common.sh 三樣態偵測與雙根 fail-loud 單測"
```

---

## Phase 2：Chart 參數化 + 去品牌（in-repo，bundle 前先在 repo 改好）

> 在 `woow_paas_platform` repo 內改 chart（plugin bundle 是 `cp -r`，先改好再複製）。`ttl.image` 已由 PR #177 完成，不在此重做。

### Task 2.1：新增 values keys

**Files:** Modify `charts/odoo-dev-sandbox/values.yaml`

- [ ] **Step 1：新增/改下列 keys**

```yaml
# sandbox 區塊新增（去品牌 label 前綴；woow profile 覆寫為 sandbox.woow.tw）
sandbox:
  labelPrefix: dev-sandbox.io

# namespace 區塊新增
namespace:
  prefix: dev-sandbox

# postgres 區塊新增（woow profile 覆寫為 woow_）
postgres:
  dbPrefix: sandbox_

# odoo.extraPipRequirementsFile 改 default 空（script 依 primary_module 注入且檔案存在才指）
odoo:
  extraPipRequirementsFile: ""
  stgAddonsPath: /opt/bitnami/odoo/lib/odoo/addons,/opt/bitnami/odoo/addons,/opt/bitnami/odoo/custom-addons

# codeMount 升級（取代 hostPath+moduleName+extraAddonsHostPath）
codeMount:
  mounts: []          # 每項 { hostPath, mountSubdir }；script 由 detect_layout 注入
  mountWholeDir: false # true=整目錄掛 /mnt/extra-addons（樣態 c）
  extraMounts: []     # 每項 { hostPath, containerPath }；多相依來源

# registry 新增 secretName（去品牌 jfrog；woow profile 覆寫）
registry:
  secretName: image-registry-creds

# 去品牌的 default
ingress:
  domain: ""          # woow profile 覆寫為 woowtech.io
pgadmin:
  defaultEmail: admin@example.com
```

> woow 字面值（`sandbox.woow.tw`、`woow_`、`woowtech.io`、`jfrog-registry`、`woow-odoo-dev`）移到 woow profile（`values-ci.yaml` / 平台 overlay 或 woow repo 的 `.sandbox.yml` 驅動 `--set`），不留通用 default。

- [ ] **Step 2：`helm lint` 過**

Run：`helm lint charts/odoo-dev-sandbox`
Expected：0 chart(s) failed。

### Task 2.2：`_helpers.tpl` 去品牌前綴

**Files:** Modify `charts/odoo-dev-sandbox/templates/_helpers.tpl`

- [ ] **Step 1：namespace 前綴（:54-60）**

把 `printf "dev-sandbox-%s" .Values.sandbox.branch` 改：
```
{{- printf "%s-%s" (.Values.namespace.prefix | default "dev-sandbox") .Values.sandbox.branch | trunc 63 | trimSuffix "-" }}
```

- [ ] **Step 2：dbName 前綴（:65-71）**

把 `printf "woow_%s" (...)` 改：
```
{{- printf "%s%s" (.Values.postgres.dbPrefix | default "sandbox_") (.Values.sandbox.branch | replace "-" "_") | trunc 63 }}
```
（`postgres.database` 顯式覆寫優先邏輯保留；create.sh 會以 `--set postgres.database` 傳全名，故此分支多為 fallback。）

### Task 2.3：label/annotation 去品牌（三檔 lock-step）

**Files:** Modify `templates/namespace.yaml`、`templates/ttl-cronjob.yaml`、`values.yaml:13`

- [ ] **Step 1：`values.yaml:13`** `sandbox.labels` 改 `{{ .Values.sandbox.labelPrefix }}/managed: "true"`（用 tpl 或在 namespace.yaml 組）。
- [ ] **Step 2：`namespace.yaml:12-23`** 5 個 annotation key（branch/ttl/mode/created/expires）前綴改 `{{ .Values.sandbox.labelPrefix }}/...`。
- [ ] **Step 3：`ttl-cronjob.yaml:77`** jsonpath 讀 expires 改用同前綴（**lock-step 關鍵，否則 TTL 自動清理靜默失效**）：
```
-o jsonpath="{.metadata.annotations.{{ .Values.sandbox.labelPrefix | replace "." "\\." }}/expires}"
```
- [ ] **Step 4：驗 TTL lock-step**

Run：`helm template t charts/odoo-dev-sandbox --set sandbox.branch=x --show-only templates/namespace.yaml --show-only templates/ttl-cronjob.yaml | grep -E 'dev-sandbox.io/expires'`
Expected：namespace 寫入端與 cronjob 讀取端前綴**一致**。

### Task 2.4：codeMount range 掛載（三樣態 + 多相依）

**Files:** Modify `templates/odoo-deployment.yaml`、`templates/odoo-configmap.yaml`

- [ ] **Step 1：`odoo-deployment.yaml:136-143`（volumeMounts, local mode）改 range**

```yaml
{{- if eq .Values.sandbox.mode "local" }}
{{- range $i, $m := .Values.codeMount.mounts }}
- name: code-mount-{{ $i }}
  mountPath: {{ if $.Values.codeMount.mountWholeDir }}/mnt/extra-addons{{ else }}/mnt/extra-addons/{{ $m.mountSubdir }}{{ end }}
{{- end }}
{{- range $i, $e := .Values.codeMount.extraMounts }}
- name: extra-mount-{{ $i }}
  mountPath: {{ $e.containerPath }}
{{- end }}
{{- end }}
```

- [ ] **Step 2：`odoo-deployment.yaml:165-176`（volumes, local mode）改 range**

```yaml
{{- if eq .Values.sandbox.mode "local" }}
{{- range $i, $m := .Values.codeMount.mounts }}
- name: code-mount-{{ $i }}
  hostPath: { path: {{ $m.hostPath }}, type: Directory }
{{- end }}
{{- range $i, $e := .Values.codeMount.extraMounts }}
- name: extra-mount-{{ $i }}
  hostPath: { path: {{ $e.hostPath }}, type: Directory }
{{- end }}
{{- end }}
```

- [ ] **Step 3：`odoo-configmap.yaml:24-28` 非 stg 分支動態 append addons_path**

```
{{- if eq .Values.sandbox.mode "stg" }}
addons_path = {{ .Values.odoo.stgAddonsPath }}
{{- else }}
addons_path = {{ .Values.odoo.addonsPath }}{{- range .Values.codeMount.extraMounts }},{{ .containerPath }}{{- end }}
{{- end }}
```

- [ ] **Step 4：驗 render（樣態 a 單掛 + 一個 extraMount）**

Run：
```bash
helm template t charts/odoo-dev-sandbox \
  --set sandbox.mode=local --set sandbox.branch=x \
  --set 'codeMount.mounts[0].hostPath=/tmp/repo/src' --set 'codeMount.mounts[0].mountSubdir=woow_paas_platform' \
  --set 'codeMount.extraMounts[0].hostPath=/tmp/deps' --set 'codeMount.extraMounts[0].containerPath=/mnt/extra-addons-ext' \
  --show-only templates/odoo-deployment.yaml | grep -E 'mountPath|path:'
```
Expected：`/mnt/extra-addons/woow_paas_platform` + `/mnt/extra-addons-ext` 兩個 mount，volumes 對應兩個 hostPath。

### Task 2.5：registry secret 名去品牌

**Files:** Modify `templates/registry-secret.yaml:6`、`templates/odoo-deployment.yaml:25`

- [ ] **Step 1：兩處 imagePullSecret 名改** `{{ .Values.registry.secretName | default "image-registry-creds" }}`（lock-step）。
- [ ] **Step 2：`helm lint` + Commit**

```bash
helm lint charts/odoo-dev-sandbox
git add charts/odoo-dev-sandbox
git commit -m "feat: chart 參數化三樣態掛載與去品牌（labelPrefix/dbPrefix/nsPrefix/secretName）"
```

---

## Phase 3：泛化 7 支 script（在 plugin 目錄改；先複製）

> 本 phase 在 plugin 目錄操作（`cp` 自 in-repo 後改）。`e2e-seed.sh`/`seed-from-stg.sh` **不複製**。

### Task 3.1：複製 8 支 + 改標頭（雙根）

**Files:** Create `…/scripts/{create,destroy,list,status,logs,extend,test,build}.sh`

- [ ] **Step 1：複製**

```bash
SKILL=~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox
cp .claude/skills/k8s-dev-sandbox/scripts/{create,destroy,list,status,logs,extend,test,build}.sh "$SKILL/scripts/"
chmod +x "$SKILL"/scripts/*.sh
```

- [ ] **Step 2：每支標頭改雙根（canonical pattern）**

把每支的 `SCRIPT_DIR=...` + `PROJECT_ROOT=$(cd "${SCRIPT_DIR}/../../../.." && pwd)` 換成：

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"
PLUGIN_SKILL_ROOT="$(resolve_plugin_root)" || exit 1   # chart / sibling
# REPO_ROOT：在 arg 解析拿到 --repo 後呼叫
```

並在每支的 arg 解析迴圈加 `--repo) REPO_FLAG="$2"; shift 2 ;;`，解析完後：
```bash
REPO_ROOT="$(resolve_repo_root --repo "${REPO_FLAG:-}")" || exit 1
```

- [ ] **Step 3：per-script 精確改點（逐檔套用下表）**

| 檔 | 行 | 現值 | 改 |
|---|---|---|---|
| 全部 7 支 | 標頭 | `PROJECT_ROOT=…/../../../..` | 雙根（Step 2） |
| destroy/status/logs/extend | git branch | `git -C "$PROJECT_ROOT"` | `git -C "$REPO_ROOT"`；slug 改 `compute_slug` |
| create | `:166` | `CHART_DIR=${PROJECT_ROOT}/charts/...` | `CHART_DIR="${PLUGIN_SKILL_ROOT}/charts/odoo-dev-sandbox"` |
| create | `:410` | `SRC_PATH=${PROJECT_ROOT}/src` | 由 `SBX_LAYOUT` 決定來源；改用 `codeMount.mounts[]` `--set` |
| create | `:196` | `DB_NAME="woow_…"` | `DB_NAME="$(compute_db_name "$SLUG" "$SBX_DB_PREFIX")"`；並 `--set postgres.database="$DB_NAME"` |
| create | `:521-538` | install gate `WHERE name='woow_paas_platform'` | 查 `$SBX_MODULE`（primary）|
| create | `:567` | `MODULES="base,web,woow_paas_platform"` | `MODULES="$SBX_INSTALL_MODULES"` |
| create | `:582` | `--load-language=zh_TW` | 改可選 `--lang`（預設不帶）|
| create | `:230-326`,`:588-719` | woow stg profile + 平台 seed SQL | **剝離**（Phase 4 hook）|
| test | `:38`,`:66` | `MODULE="woow_paas_platform"` | `--module` → `SBX_MODULE`（無則 fail-loud）|
| test | `:110` | `FULLNAME="${SLUG}-odoo-dev-sandbox"` | `resolve_workload`（odoo/postgres）|
| test | `:205` | `e2e-seed.sh …` | 改 opt-in post-test hook；通用不內建 |
| build | `:16-17` | `PROJECT_ROOT=…` | 全部語意=`REPO_ROOT`（Dockerfile.dev/context/git）|
| build | `:70` | `IMAGE_NAME="${REGISTRY}woow-odoo-dev"` | `--image-name` 或 `${SBX_MODULE}-odoo-dev` |
| build | `:76-78` | `Dockerfile.dev` at `$PROJECT_ROOT` | `${REPO_ROOT}/Dockerfile.dev`，不存在 fail-loud |
| status/logs | `:286`/`:124` | `FULLNAME="${SLUG}-odoo-dev-sandbox"` | `resolve_workload`（避開 trunc 43）|
| list/status/extend | annotation/label | `sandbox.woow.tw/*` | 與 chart `labelPrefix` 同源（common.sh 定義 `LABEL_DOMAIN`，預設 `dev-sandbox.io`）|

- [ ] **Step 4：`shellcheck` 全部**

Run：`shellcheck -x "$SKILL"/scripts/*.sh`
Expected：no errors。

- [ ] **Step 5：Commit**

```bash
git add skills/k8s-dev-sandbox/scripts/*.sh
git commit -m "feat: 泛化 7 支 sandbox script（雙根/module 偵測/db 前綴/label-selector 動態查）"
```

---

## Phase 4：剝離 woow 平台 seed 成 opt-in **雙 hook**（已實作）

> **Plan-gap 修正（實作時發現）**：原設計只規劃單一 post-create hook，但 woow stg 的
> agent-runtime 共享 secret 是 **install-time** 的（要在 `helm install` 當下從 operator 的
> k8s secret 動態撈、寫進 chart Secret 成 pod env），無法搬到 install 之後才跑的 post-create
> hook。因此通用 create.sh 提供 **兩個** opt-in hook：
>
> | hook | 時機 | 用途（woow） |
> |------|------|------|
> | `${REPO_ROOT}/.sandbox/pre-install.sh` | `helm install` **之前** | 寫額外 helm values 到 `$EXTRA_VALUES_FILE`（create.sh `-f` 它）—— agent-runtime secret 動態撈 + stg networkPolicy/storageClass/JCR registry |
> | `${REPO_ROOT}/.sandbox/post-create.sh` | module install 完成、**最終 restart 之前** | operator 探索 + `ir.config_parameter` / ECPay / WoowBot AI / journal seed SQL |
>
> post-create 在最終 restart **之前**跑（seed 走 psql、odoo 因 `--stop-after-init` 停著也行；
> restart 在 seed 後做讓 `ir.config_parameter` in-process 快取重載、seed 生效）。

### Task 4.1：create.sh 雙 hook 偵測（通用主線零 woow 分支）✅

**Files:** Modify `…/scripts/create.sh`（plugin `7a32bee`）

- [x] **pre-install hook**（build helm args 後、`helm install` 前）：

```bash
_PREINSTALL_VALUES=""
PRE_HOOK="${REPO_ROOT}/.sandbox/pre-install.sh"
if [ -f "$PRE_HOOK" ]; then
    _PREINSTALL_VALUES="$(mktemp)"; trap 'rm -f "${_PREINSTALL_VALUES:-}"' EXIT
    EXTRA_VALUES_FILE="$_PREINSTALL_VALUES" REPO_ROOT="$REPO_ROOT" NAMESPACE="$NAMESPACE" \
        SLUG="$SLUG" MODE="$MODE" DB_NAME="$DB_NAME" KUBE_CONTEXT="$KUBE_CONTEXT" \
        PRIMARY_MODULE="$PRIMARY_MODULE" bash "$PRE_HOOK" || echo "pre-install hook 失敗（繼續）" >&2
    [ -s "$_PREINSTALL_VALUES" ] && EXTRA_ARGS="$EXTRA_ARGS -f $_PREINSTALL_VALUES"
fi
```

- [x] **post-create hook**（module install + seed 後、最終 restart 前）：存在才跑、`bash` 子行程隔離、失敗不中止；export 14 變數（`REPO_ROOT`/`PLUGIN_SKILL_ROOT`/`NAMESPACE`/`SLUG`/`DB_NAME`/`DB_PREFIX`/`MODE`/`KUBE_CONTEXT`/`PRIMARY_MODULE`/`INSTALL_MODULES`/`ODOO_DEPLOY`/`PG_STATEFULSET`/`NGINX_SVC`/`OPERATOR_NS`）。

### Task 4.2：在 woow repo 建外置 seed ✅

**Files:** Create `.sandbox.yml`、`.sandbox/pre-install.sh`、`.sandbox/post-create.sh`（woow `693ef62`）

- [x] **`.sandbox.yml`**：`layout: src` / `module: woow_paas_platform` / `db_prefix: woow_` / `post_create_hook: .sandbox/post-create.sh`。
- [x] **`.sandbox/pre-install.sh`**（install-time）＝ 舊 `create.sh:230-326`（stg agent-runtime secret 從 operator k8s secret 動態撈 + networkPolicy/storageClass/JCR registry），只 stg 生效，寫 YAML 到 `$EXTRA_VALUES_FILE`。
- [x] **`.sandbox/post-create.sh`**（post-install）＝ 舊 `create.sh:588-719`（operator 探索 + `ir_config_parameter` / ECPay / WoowBot AI / subscription journal SQL），讀 `NAMESPACE`/`PG_STATEFULSET`/`DB_NAME`/`MODE`/`OPERATOR_NS` env。
- [x] 驗證：`bash -n` 全綠；pre-install 產出 YAML 經 `helm template -f` 消費驗證。

---

## Phase 5：打包進 plugin + SKILL.md

### Task 5.1：bundle chart + 寫 SKILL.md

- [ ] **Step 1：整包複製 chart（plugin 不能 symlink 出 plugin 根）**

```bash
cp -r charts/odoo-dev-sandbox ~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/charts/
```

- [ ] **Step 2：寫 SKILL.md**（內容見下，frontmatter `name: k8s-dev-sandbox`、`${CLAUDE_PLUGIN_ROOT}` + `--repo`、命名空間 `/woowprd:k8s-dev-sandbox`、保留 create/destroy/list/status/logs/extend/test/build/up/down）

<details><summary>SKILL.md 完整內容</summary>

> 內容由 grounding 草擬，落地時直接寫入 `…/skills/k8s-dev-sandbox/SKILL.md`。重點：每支呼叫格式
> `bash "${CLAUDE_PLUGIN_ROOT}/skills/k8s-dev-sandbox/scripts/X.sh" --repo "$(git rev-parse --show-toplevel)" --context <ctx> <args>`；
> 內文標註「`--repo` 必帶」「`--context` 必填」「三樣態自動偵測」「平台 seed 外置」「local 只在 node=本機可用」「reload 限制」。
> （全文見 session workflow `wmrfi6i1o` artifacts.skill_md；DRY 起見不在此重貼，落地時逐字寫入。）

</details>

- [ ] **Step 3：reload + Phase 0 雙根複驗**

Run：`/reload-plugins`；在 woow repo `/woowprd:k8s-dev-sandbox`（無子命令顯示說明）確認被發現。

---

## Phase 6：端到端驗收（三樣態）+ 收尾

### Task 6.1：樣態 a（woow，docker-desktop local）

- [ ] **Step 1：建沙盒**

Run：`/woowprd:k8s-dev-sandbox up --context docker-desktop`（在 woow repo）
Expected：helm install 成功、pod ready。

- [ ] **Step 2：驗 module installed（最終真相）**

Run：`kubectl --context docker-desktop exec -n dev-sandbox-<slug> statefulset/<pg> -- psql -U odoo -d woow_<slug> -tAc "SELECT state FROM ir_module_module WHERE name='woow_paas_platform'"`
Expected：`installed`。

- [ ] **Step 3：驗 post-create hook 跑過**（operator/ECPay config 已 seed）。

### Task 6.2：樣態 b/c（乾淨測試 addon）

- [ ] **Step 1：樣態 b** — 用一個「根即 addon」的測試 repo（根有 `__manifest__.py`，無 `.sandbox/`）建沙盒，驗「不帶任何 woow seed 也能起 + 裝對 module（偵測到的 root module）」。
- [ ] **Step 2：樣態 c** — 用多 addon repo（含 1 個非 addon 子目錄）建沙盒，驗 `detect_layout` 接受/拒絕清單正確、Odoo 自動發現多 addon、非 addon 子目錄被排除。
- [ ] **Step 3：驗相依 fail-loud** — `.sandbox.yml` 指一個空 `extra_addons` 目錄 → create **報錯中止**（非 warning 放行）。

### Task 6.3：移除 in-repo 副本 + 更新 CLAUDE.md

- [ ] **Step 1：移除雙寫**

```bash
git rm -r .claude/skills/k8s-dev-sandbox
```
（保留 `.sandbox.yml` / `.sandbox/post-create.sh`。）

- [ ] **Step 2：改 `CLAUDE.md`** 內所有 `./.claude/skills/k8s-dev-sandbox/scripts/...` 範例為 plugin 叫法（`/woowprd:k8s-dev-sandbox` + `--repo "$(git rev-parse --show-toplevel)"` + `--context`）。

- [ ] **Step 3：Commit**

```bash
git add -A
git commit -m "refactor: 移除 in-repo k8s-dev-sandbox skill，改走 woowprd plugin"
```

---

## Self-Review

- **Spec coverage：** 設計文件四大改動組（A script 泛化 / B chart / C 打包 / D seed 剝離）→ Phase 3 / Phase 2 / Phase 5 / Phase 4；三難點（context / 三樣態 / 相依）→ common.sh `require_kube_context` 保留 / `detect_layout` + chart range / `extra_addons` fail-loud。✅
- **Placeholder scan：** common.sh、common_test.sh、chart 片段、hook、`.sandbox.yml` 皆完整碼；SKILL.md 全文指向 grounding artifact（落地逐字寫入，非 placeholder）。
- **Type consistency：** `SBX_MODULE`/`SBX_INSTALL_MODULES`/`SBX_DB_PREFIX`/`SBX_EXTRA_ADDONS`/`SBX_POST_CREATE_HOOK` 在 load_sandbox_yml 定義並於 create/test/hook 一致引用；`resolve_workload` 簽名一致；DB 名單一真相（`--set postgres.database`）消除前綴 lock-step。
- **已知留尾（design §風險）：** 樣態 c 預設全裝、`primary_module` tie-break、相依 repo 遞迴偵測 → 仍為未決，標於設計文件 §6，不阻擋本 plan。

## Execution Handoff

**Plan complete，存於 `docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-implementation.md`。**

兩種執行選項：① Subagent-Driven（推薦，每 task 一個 fresh subagent + task 間 review）；② Inline（本 session 批次執行 + checkpoint）。Phase 0 變數實測務必先過再進 Phase 1+。
