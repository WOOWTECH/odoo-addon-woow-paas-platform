#!/usr/bin/env bash
#
# woow_paas_platform — sandbox post-test hook（opt-in 的 e2e fixture seed）。
#
# 由 woow-dev plugin 的通用 test.sh 在「模組測試通過、odoo rollout 完成、pod 重新服務」之後
# 執行（plugin test.sh 的 POST_TEST_HOOK 區塊），傳入：
#   NAMESPACE / DB_NAME / SLUG / MODULE / REPO_ROOT / ODOO_DEPLOY
# 取代 2026-09-04 退役的 in-repo `woow-test.sh --e2e-seed`。用法：
#   WOOW_E2E_SEED=1 bash "${CLAUDE_PLUGIN_ROOT}/skills/k8s-dev-sandbox/scripts/test.sh" <slug> \
#       --repo "$(git rev-parse --show-toplevel)" --context <ctx>
# 沒設 WOOW_E2E_SEED 就 no-op（測試通過不代表要 seed；seed 會寫沙盒 DB，維持舊版 opt-in 語意）。
# --reset：spec 會把 fixture 推到 archived，每輪都要回到 draft 起點。
set -euo pipefail

: "${SLUG:?post-test hook 需要 SLUG}"
: "${REPO_ROOT:?post-test hook 需要 REPO_ROOT}"

if [ "${WOOW_E2E_SEED:-0}" != "1" ]; then
    echo "post-test hook: WOOW_E2E_SEED 未設，略過 e2e fixture seed（要 seed 請帶 WOOW_E2E_SEED=1）。"
    exit 0
fi

SEEDER="${REPO_ROOT}/.claude/skills/woow-e2e-fixtures/scripts/e2e-seed.sh"
if [ ! -f "$SEEDER" ]; then
    echo "post-test hook: 找不到 seeder ${SEEDER}" >&2
    exit 1
fi

CTX_ARGS=()
if [ -n "${KUBE_CONTEXT:-}" ]; then
    CTX_ARGS=(--context "$KUBE_CONTEXT")
fi

echo "post-test hook: seeding e2e skill fixture into ${SLUG} (--reset)..."
exec bash "$SEEDER" "$SLUG" --reset ${CTX_ARGS[@]+"${CTX_ARGS[@]}"}
