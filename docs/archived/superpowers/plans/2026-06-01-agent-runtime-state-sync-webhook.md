---
title: Agent-Runtime State-Sync Webhook (§5.5 loop close) Implementation Plan
category: archived
status: archived
created: 2026-06-01
archived: 2026-07-19
archived-from: docs/superpowers/plans/2026-06-01-agent-runtime-state-sync-webhook.md
archived-reason: "功能已隨 PR #118 落地，state-sync 機制與剩餘 secret 佈署缺口的現行權威為 docs/guides/development/agent-runtime-chat-architecture.md §4/§5.1，延後項由 follow-ups.md F-ASW-1~6 追蹤"
---

# Agent-Runtime State-Sync Webhook (§5.5 loop close) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `woow_paas_platform.agent_session.state` track the live pod lifecycle by wiring the operator to actually POST the §5.5 state-update webhook on spawn/attach/reap and provisioning the shared webhook HMAC secret on both the operator and Odoo sides.

**Architecture:** The Odoo receiver (`agent_runtime_webhook.py`), the `agent_session` model, the drift cron, and the operator's `webhook_dispatcher` already exist — but the loop is open: the operator never *calls* the dispatcher, and `AGENT_RUNTIME_WEBHOOK_SECRET` is empty on both sides (so any webhook would 401 anyway). This plan adds a best-effort `notify_state_update` helper, fires it from the operator's `spawn_or_attach` + `reap` via FastAPI `BackgroundTasks`, and provisions one shared HMAC secret (Odoo via `create.sh`/chart — already templated; operator via its Fleet-managed deployment).

**Tech Stack:** FastAPI + httpx + pydantic-settings (operator, Python 3.11), Odoo 18 controller (`http.Controller`), Helm chart `charts/odoo-dev-sandbox`, k8s (`woow-k3s`), operator deploy via `operator-v*` tag → CI → Fleet (`paas-app-bundle-cd`).

**Root cause reference:** see investigation in this session — confirmed (a) `dispatch_state_update` has zero production callers (only def + unit tests), (b) Odoo `AGENT_RUNTIME_WEBHOOK_SECRET`/`_PREVIOUS` both `len=0`, (c) drift cron excludes `'absent'`, (d) project-2 session row `create_date == write_date`, never updated. Same family as `docs/tracking/pitfalls/agent-runtime-stg-enablement-gap-chain.md` (this is a 6th, un-persisted layer).

---

## Design decisions (locked)

1. **Notify on EVERY `spawn_or_attach`** (not only transitions). Reason: the failure mode is a *running pod with a stale `absent` session* (exactly project 2 now). If we only notified on cold-start, an already-running pod on the attach path would never self-heal. The webhook write is idempotent (`session.write({'state', ...})`), runs in a background task (off the chat critical path), and is best-effort (never breaks chat).
2. **`new_state` = the operator's computed `pod_state`** (`"spawning"` cold-start / `"active"` attach-or-resume) for `spawn_or_attach`; `"reaped"` for `reap`. All are valid `agent_session.state` selection values.
3. **Operator reads `AGENT_RUNTIME_WEBHOOK_SECRET` from env** (mirroring `jwt_signer._current_secret()`), NOT pydantic Settings — the existing auth secret uses the env-read pattern and the chart injects secrets as env. Empty secret → helper no-ops (fail-safe, never sends an unsigned/garbage webhook).
4. **One shared secret, current-only to start.** Operator signs with current; Odoo accepts current OR previous (rotation parity already coded). Initial provisioning sets only current on both sides.
5. **Drift-cron `absent` exclusion is left as-is** (Task 9, optional). With the webhook firing on every chat the state stays fresh, so the exclusion is no longer the active bug; changing it has an operator-polling cost tradeoff (every never-spawned project's `absent` session would be polled). Out of core scope.

---

## File Structure

| File | Repo | Responsibility | Change |
| --- | --- | --- | --- |
| `extra/paas-operator/src/services/webhook_dispatcher.py` | this | sign+POST (`dispatch_state_update`) + NEW `notify_state_update` best-effort wrapper + env secret read | modify |
| `extra/paas-operator/src/api/agent_runtimes.py` | this | call `notify_state_update` from `spawn_or_attach` + `reap` via `BackgroundTasks` | modify |
| `extra/paas-operator/tests/test_agent_runtimes.py` | this | W.3/W.4 helper tests + S.* spawn/reap-notify tests | modify |
| `.claude/skills/k8s-dev-sandbox/scripts/create.sh` | this | inject `agentRuntime.webhookSecret`(+previous) from env | modify |
| `.claude/skills/deploy-stg/scripts/deploy-stg.sh` | this | warn when webhook secret env unset (parity with auth/ns) | modify |
| `.claude/skills/deploy-stg/SKILL.md` + `CLAUDE.md` | this | document the `AGENT_RUNTIME_WEBHOOK_SECRET` precondition | modify |
| `charts/odoo-dev-sandbox/templates/{secrets,odoo-deployment}.yaml` | this | webhook secret → env wiring | **already done — no change** |
| operator deployment + secret (`paas-operator-stg-agent-runtime-auth` / new key) | `paas-app-bundle-cd` (cross-repo) **or** `kubectl` stg bootstrap | inject `AGENT_RUNTIME_WEBHOOK_SECRET` env into the operator | Task 6 |

---

## Task 1: Operator `notify_state_update` helper

**Files:**
- Modify: `extra/paas-operator/src/services/webhook_dispatcher.py`
- Test: `extra/paas-operator/tests/test_agent_runtimes.py` (class `TestWebhookDispatcher`)

- [ ] **Step 1: Write the failing tests** (append inside `class TestWebhookDispatcher`)

```python
    def test_notify_state_update_reads_env_secret_and_posts(self, monkeypatch):
        """W.3: notify_state_update signs with AGENT_RUNTIME_WEBHOOK_SECRET env + sends ts/state/url."""
        from src.services import webhook_dispatcher
        monkeypatch.setenv("AGENT_RUNTIME_WEBHOOK_SECRET", "wh-secret-1")
        captured = {}

        def _fake_dispatch(odoo_base_url, payload, secret):
            captured["url"] = odoo_base_url
            captured["payload"] = payload
            captured["secret"] = secret
            resp = MagicMock(); resp.status_code = 200
            return resp

        with patch.object(webhook_dispatcher, "dispatch_state_update", _fake_dispatch):
            webhook_dispatcher.notify_state_update(
                "http://odoo.local", _HASH, "active", pod_ws_url="ws://pod:8000/v1/chat",
            )
        assert captured["secret"] == "wh-secret-1"
        assert captured["payload"]["project_hash"] == _HASH
        assert captured["payload"]["new_state"] == "active"
        assert captured["payload"]["pod_ws_url"] == "ws://pod:8000/v1/chat"
        assert captured["payload"]["ts"].endswith("Z")

    def test_notify_state_update_noop_without_secret(self, monkeypatch):
        """W.4: empty webhook secret → no dispatch (fail-safe, never sends unsigned)."""
        from src.services import webhook_dispatcher
        monkeypatch.delenv("AGENT_RUNTIME_WEBHOOK_SECRET", raising=False)
        with patch.object(webhook_dispatcher, "dispatch_state_update") as mock_dispatch:
            webhook_dispatcher.notify_state_update("http://odoo.local", _HASH, "active")
        mock_dispatch.assert_not_called()

    def test_notify_state_update_swallows_exceptions(self, monkeypatch):
        """W.5: a dispatch error must never propagate (best-effort, off chat path)."""
        from src.services import webhook_dispatcher
        monkeypatch.setenv("AGENT_RUNTIME_WEBHOOK_SECRET", "wh-secret-1")
        with patch.object(webhook_dispatcher, "dispatch_state_update", side_effect=RuntimeError("boom")):
            webhook_dispatcher.notify_state_update("http://odoo.local", _HASH, "active")  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd extra/paas-operator && python -m pytest tests/test_agent_runtimes.py::TestWebhookDispatcher -v`
Expected: FAIL — `AttributeError: module 'src.services.webhook_dispatcher' has no attribute 'notify_state_update'`.

- [ ] **Step 3: Implement `notify_state_update`** (append to `webhook_dispatcher.py`; add `os`, `datetime` imports at top)

```python
import os
from datetime import datetime, timezone

_ENV_WEBHOOK_SECRET = "AGENT_RUNTIME_WEBHOOK_SECRET"


def _webhook_secret() -> str:
    """Current outbound webhook signing secret (mirrors jwt_signer._current_secret)."""
    return os.environ.get(_ENV_WEBHOOK_SECRET, "")


def notify_state_update(odoo_base_url, project_hash, new_state, pod_ws_url=None):
    """Best-effort §5.5 state-update notify. No-op when unconfigured; never raises.

    Designed to run inside a FastAPI BackgroundTask: it must NEVER raise into the
    request path. Skips entirely when odoo_base_url or the webhook secret is unset
    (an unsigned webhook would 401 anyway).
    """
    secret = _webhook_secret()
    if not odoo_base_url or not secret:
        logger.info(
            "state-update notify skipped (odoo_base_url/secret unset) for %s -> %s",
            project_hash, new_state,
        )
        return
    payload = {
        "project_hash": project_hash,
        "new_state": new_state,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if pod_ws_url:
        payload["pod_ws_url"] = pod_ws_url
    try:
        resp = dispatch_state_update(odoo_base_url, payload, secret)
        if resp is not None and resp.status_code >= 400:
            logger.warning(
                "state-update notify got HTTP %s for %s -> %s",
                resp.status_code, project_hash, new_state,
            )
    except Exception:
        logger.warning(
            "state-update notify failed for %s -> %s", project_hash, new_state,
            exc_info=True,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd extra/paas-operator && python -m pytest tests/test_agent_runtimes.py::TestWebhookDispatcher -v`
Expected: PASS (5 tests: W.1, W.2 existing + W.3, W.4, W.5 new).

- [ ] **Step 5: Commit**

```bash
git add extra/paas-operator/src/services/webhook_dispatcher.py extra/paas-operator/tests/test_agent_runtimes.py
git commit -m "feat: add best-effort notify_state_update helper to operator webhook dispatcher"
```

---

## Task 2: Fire the webhook from `spawn_or_attach`

**Files:**
- Modify: `extra/paas-operator/src/api/agent_runtimes.py` (import + `spawn_or_attach` signature/body ~225-308)
- Test: `extra/paas-operator/tests/test_agent_runtimes.py` (class `TestSpawnOrAttach`)

- [ ] **Step 1: Write the failing test** (append inside `class TestSpawnOrAttach`)

```python
    def test_spawn_or_attach_notifies_odoo_state(self, client):
        """S.N: spawn-or-attach enqueues a state-update notify with the pod_state."""
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s, \
             patch("src.api.agent_runtimes._ensure_namespace"), \
             patch("src.api.agent_runtimes.webhook_dispatcher") as mock_wh:
            mock_helm.release_exists.return_value = False  # cold start
            mock_k8s.get_pods.return_value = []
            resp = client.post("/api/agent-runtimes/spawn-or-attach", json=_SPAWN_BODY)
        assert resp.status_code == 200
        mock_wh.notify_state_update.assert_called_once()
        kwargs = mock_wh.notify_state_update.call_args
        # project_hash + new_state present as positional/keyword
        called_args = list(kwargs.args) + list(kwargs.kwargs.values())
        assert _HASH in called_args
        assert "spawning" in called_args
```

> NOTE for implementer: mirror the exact `patch(...)` targets used by the sibling cold-start test `test_spawn_or_attach_cold_start_signs_both_tokens` (line ~370) — the helpers it stubs (`_release_exists` vs `helm_service.release_exists`, `_ensure_namespace`, `k8s_service`, `_provision_llm_secret`, `jwt_signer`) must match so the handler reaches the `return`. Read that test first and copy its `with patch(...)` block verbatim, adding only the `webhook_dispatcher` patch.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd extra/paas-operator && python -m pytest tests/test_agent_runtimes.py::TestSpawnOrAttach::test_spawn_or_attach_notifies_odoo_state -v`
Expected: FAIL — `notify_state_update` not called (handler doesn't enqueue it yet).

- [ ] **Step 3: Implement the wiring**

In `extra/paas-operator/src/api/agent_runtimes.py`:

a. Add imports near the top (line ~28 alongside `from src.services import jwt_signer, ...`):

```python
from fastapi import BackgroundTasks
from src.services import webhook_dispatcher
from src.config import settings
```
(If `settings` is already imported, do not duplicate. If `jwt_signer, mcp_invalidator, skill_invalidator` is the existing import line, append `webhook_dispatcher` to it.)

b. Change the handler signature (line 225):

```python
def spawn_or_attach(request: SpawnOrAttachRequest, background_tasks: BackgroundTasks):
```

c. Immediately BEFORE the `return {` (line ~302), enqueue the notify:

```python
    background_tasks.add_task(
        webhook_dispatcher.notify_state_update,
        settings.odoo_base_url,
        request.project_hash,
        pod_state,
        _pod_ws_url(namespace, release),
    )
    return {
        "pod_ws_url": _pod_ws_url(namespace, release),
        ...
    }
```

- [ ] **Step 4: Run the test (and the full spawn suite for regressions)**

Run: `cd extra/paas-operator && python -m pytest tests/test_agent_runtimes.py::TestSpawnOrAttach -v`
Expected: PASS — new test green, all existing spawn tests still green (the added `background_tasks` param is injected by FastAPI; TestClient runs background tasks after the response).

- [ ] **Step 5: Commit**

```bash
git add extra/paas-operator/src/api/agent_runtimes.py extra/paas-operator/tests/test_agent_runtimes.py
git commit -m "feat: fire state-update webhook from spawn_or_attach via BackgroundTasks"
```

---

## Task 3: Fire the webhook from `reap`

**Files:**
- Modify: `extra/paas-operator/src/api/agent_runtimes.py` (`reap`, line ~311)
- Test: `extra/paas-operator/tests/test_agent_runtimes.py`

- [ ] **Step 1: Write the failing test** (append near the reap tests)

```python
    def test_reap_notifies_odoo_reaped(self, client):
        """reap enqueues a state-update notify with new_state='reaped'."""
        with patch("src.api.agent_runtimes.k8s_service") as mock_k8s, \
             patch("src.api.agent_runtimes._release_exists", return_value=True), \
             patch("src.api.agent_runtimes.webhook_dispatcher") as mock_wh:
            resp = client.post(f"/api/agent-runtimes/{_HASH}/reap")
        assert resp.status_code == 200
        mock_wh.notify_state_update.assert_called_once()
        called_args = list(mock_wh.notify_state_update.call_args.args) + \
            list(mock_wh.notify_state_update.call_args.kwargs.values())
        assert "reaped" in called_args
```

> NOTE: mirror the existing reap test's `patch(...)` set (read the `reap` body 311-end for the exact helper names it calls — `_release_exists`, `k8s_service.scale_deployment` / annotation stamp). Match them so the handler reaches its return.

- [ ] **Step 2: Run to verify fail**

Run: `cd extra/paas-operator && python -m pytest tests/test_agent_runtimes.py -k reap_notifies -v`
Expected: FAIL — notify not called.

- [ ] **Step 3: Implement** — add `background_tasks: BackgroundTasks` to `reap(project_hash, background_tasks)` and, on the success path (after the scale-to-0), before returning:

```python
    background_tasks.add_task(
        webhook_dispatcher.notify_state_update,
        settings.odoo_base_url,
        project_hash,
        "reaped",
    )
```

- [ ] **Step 4: Run to verify pass + full operator suite**

Run: `cd extra/paas-operator && HELM_BINARY=$(which helm) python -m pytest tests/test_agent_runtimes.py -v`
Expected: PASS (all). Then full unit run: `python -m pytest tests/ -v` (skips `-m integration`).

- [ ] **Step 5: Commit**

```bash
git add extra/paas-operator/src/api/agent_runtimes.py extra/paas-operator/tests/test_agent_runtimes.py
git commit -m "feat: fire state-update webhook (reaped) from reap via BackgroundTasks"
```

---

## Task 4: `create.sh` — inject the Odoo-side webhook secret

**Files:**
- Modify: `.claude/skills/k8s-dev-sandbox/scripts/create.sh` (lines 247-264)

> The chart is already wired (`secrets.yaml:24-28` renders `agent-runtime-webhook-secret`(+`-previous`); `odoo-deployment.yaml:96-106` injects `AGENT_RUNTIME_WEBHOOK_SECRET`(+`_PREVIOUS`)). Only `create.sh` fails to forward the value.

- [ ] **Step 1: Write the failing assertion test** (one-off shell, no framework)

```bash
# /tmp/test_create_webhook.sh
set -euo pipefail
export AGENT_RUNTIME_WEBHOOK_SECRET="wh-test-123"
# Render only the agentRuntime values block create.sh would write (extract the heredoc logic):
out=$(bash -c '
  AGENT_RUNTIME_WEBHOOK_SECRET="wh-test-123"
  { echo "agentRuntime:";
    [ -n "${AGENT_RUNTIME_WEBHOOK_SECRET:-}" ] && echo "  webhookSecret: \"${AGENT_RUNTIME_WEBHOOK_SECRET}\""; }')
echo "$out" | grep -q "webhookSecret: \"wh-test-123\"" && echo PASS || { echo FAIL; exit 1; }
```

Run: `bash /tmp/test_create_webhook.sh` → expect `FAIL` BEFORE the edit only if testing the real script path; this standalone proves the snippet shape. (The real verification is Task 8 via `helm get values`.)

- [ ] **Step 2: Edit `create.sh`** — extend the condition (line 255) and the heredoc (258-262):

```bash
if [ "$MODE" = "stg" ] && { [ -n "${AGENT_RUNTIME_AUTH_SECRET_CURRENT:-}" ] || [ -n "${AGENT_RUNTIME_NAMESPACE_SECRET:-}" ] || [ -n "${AGENT_RUNTIME_WEBHOOK_SECRET:-}" ]; }; then
    _AGENT_RUNTIME_VALUES=$(mktemp)
    trap "rm -f ${_REGISTRY_VALUES:-} $_AGENT_RUNTIME_VALUES" EXIT
    {
        echo "agentRuntime:"
        [ -n "${AGENT_RUNTIME_AUTH_SECRET_CURRENT:-}" ] && echo "  authSecretCurrent: \"${AGENT_RUNTIME_AUTH_SECRET_CURRENT}\""
        [ -n "${AGENT_RUNTIME_NAMESPACE_SECRET:-}" ] && echo "  namespaceSecret: \"${AGENT_RUNTIME_NAMESPACE_SECRET}\""
        [ -n "${AGENT_RUNTIME_WEBHOOK_SECRET:-}" ] && echo "  webhookSecret: \"${AGENT_RUNTIME_WEBHOOK_SECRET}\""
        [ -n "${AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS:-}" ] && echo "  webhookSecretPrevious: \"${AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS}\""
    } > "$_AGENT_RUNTIME_VALUES"
    EXTRA_ARGS="$EXTRA_ARGS -f $_AGENT_RUNTIME_VALUES"
fi
```

Also update the comment block (247-254) to add the webhook secret line:
```bash
#   - AGENT_RUNTIME_WEBHOOK_SECRET: Odoo verifies the operator-signed §5.5
#     state-update webhook with it. MUST equal the operator's webhook secret
#     (empty → every state-update 401s → agent_session.state stuck 'absent').
```

- [ ] **Step 3: Verify the script still parses**

Run: `bash -n .claude/skills/k8s-dev-sandbox/scripts/create.sh`
Expected: no output (syntax OK).

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/k8s-dev-sandbox/scripts/create.sh
git commit -m "feat: inject agentRuntime.webhookSecret into stg sandbox helm values"
```

---

## Task 5: `deploy-stg` — warn + document the webhook secret env

**Files:**
- Modify: `.claude/skills/deploy-stg/scripts/deploy-stg.sh` (the agent-runtime env warning block ~196-201)
- Modify: `.claude/skills/deploy-stg/SKILL.md` (Agent-runtime env preconditions section)
- Modify: `CLAUDE.md` (Deploy Staging agent-runtime env note)

- [ ] **Step 1: Edit the warning block** in `deploy-stg.sh` — extend the guard to also flag a missing webhook secret:

```bash
if [ -z "${AGENT_RUNTIME_AUTH_SECRET_CURRENT:-}" ] || [ -z "${AGENT_RUNTIME_NAMESPACE_SECRET:-}" ] || [ -z "${AGENT_RUNTIME_WEBHOOK_SECRET:-}" ]; then
  echo -e "${YELLOW}Warning: agent-runtime secrets not fully set — pod path / state-sync may fail.${NC}" >&2
  echo -e "${YELLOW}  export AGENT_RUNTIME_AUTH_SECRET_CURRENT=<operator auth/current>${NC}" >&2
  echo -e "${YELLOW}  export AGENT_RUNTIME_NAMESPACE_SECRET=<odoo-side namespace-hash secret>${NC}" >&2
  echo -e "${YELLOW}  export AGENT_RUNTIME_WEBHOOK_SECRET=<same as operator webhook secret; else agent_session.state stuck 'absent'>${NC}" >&2
fi
```

- [ ] **Step 2: Document** the new precondition in `SKILL.md` (Agent-runtime env section) and `CLAUDE.md` (Deploy Staging) — add `AGENT_RUNTIME_WEBHOOK_SECRET` to the export list with the one-line rationale ("operator signs §5.5 state-update; mismatch/empty → state stuck absent").

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/deploy-stg/scripts/deploy-stg.sh .claude/skills/deploy-stg/SKILL.md CLAUDE.md
git commit -m "docs: document AGENT_RUNTIME_WEBHOOK_SECRET deploy-stg precondition"
```

---

## Task 6: Provision the shared webhook secret (operator side)

> The operator helm chart is NOT in this repo (`extra/paas-operator/helm/` is empty). The operator deployment + its `paas-operator-stg-agent-runtime-auth` secret are Fleet-managed via `paas-app-bundle-cd`. Two tracks: **(6a) stg bootstrap** (immediate, for e2e validation; reverts on next Fleet sync) and **(6b) persistent** (CD repo — needs repo access).

- [ ] **Step 1: Generate ONE shared secret** (used for both sides; 64 hex chars to match existing secret lengths)

```bash
WEBHOOK_SECRET=$(openssl rand -hex 32); echo "len=${#WEBHOOK_SECRET}"   # 64
```
Keep this value for Task 7 (do NOT commit it; export only).

- [ ] **Step 2 (6a, stg bootstrap): add the key to the operator secret + env**

```bash
# add webhook key to the existing operator secret
kubectl --context woow-k3s -n paas-stg-system patch secret paas-operator-stg-agent-runtime-auth \
  --type merge -p "{\"data\":{\"webhook-current\":\"$(printf %s "$WEBHOOK_SECRET" | base64)\"}}"

# inject env into the operator deployment (Fleet will revert on next sync — track in 6b)
kubectl --context woow-k3s -n paas-stg-system set env deploy/paas-operator-stg \
  AGENT_RUNTIME_WEBHOOK_SECRET="$WEBHOOK_SECRET"

kubectl --context woow-k3s -n paas-stg-system rollout status deploy/paas-operator-stg --timeout=120s
```

- [ ] **Step 3 (6b, persistent): file the CD change** — in `paas-app-bundle-cd/paas-operator-stg/values-stg.yaml` (or the operator chart there): add `webhook-current` to the agent-runtime secret + `AGENT_RUNTIME_WEBHOOK_SECRET` env (sourced from that secret key). If repo access is unavailable, record this as a follow-up in `docs/tracking/followups/follow-ups.md` so the `kubectl` bootstrap (6a) isn't silently lost on Fleet sync.

- [ ] **Step 4: Verify operator now has the env**

```bash
kubectl --context woow-k3s -n paas-stg-system exec deploy/paas-operator-stg -- \
  sh -c 'printf "WEBHOOK len=%s\n" "${#AGENT_RUNTIME_WEBHOOK_SECRET}"'
```
Expected: `WEBHOOK len=64`.

---

## Task 7: Build operator + roll out both sides

- [ ] **Step 1: Tag + build the new operator image** (after Tasks 1-3 merged)

```bash
git tag operator-v$(date -u +'%Y%m%d.%H%M') && git push origin operator-v<that-tag>
# (or use the build-operator skill). Wait for CI: jcr-prod.woowtech.io/.../paas-operator:<tag>
```

- [ ] **Step 2: Bump Fleet CD** to the new operator image tag in `paas-app-bundle-cd/paas-operator-stg/values-stg.yaml`; let Fleet sync. Verify:

```bash
kubectl --context woow-k3s -n paas-stg-system get deploy paas-operator-stg \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```
Expected: the new `paas-operator:<tag>`.

> If 6b/CD is not yet landed, re-apply the 6a env after the Fleet image bump (the deploy template may reset env).

- [ ] **Step 3: Redeploy Odoo with the webhook secret** (same value as Task 6)

```bash
export AGENT_RUNTIME_WEBHOOK_SECRET="$WEBHOOK_SECRET"   # the value from Task 6 Step 1
# re-supply the existing auth+ns secrets too (preserve, per this session's deploy pattern):
VALS=$(helm --kube-context woow-k3s get values stg -n dev-sandbox-stg -o json)
export AGENT_RUNTIME_AUTH_SECRET_CURRENT=$(echo "$VALS" | jq -r '.agentRuntime.authSecretCurrent // empty')
export AGENT_RUNTIME_NAMESPACE_SECRET=$(echo "$VALS" | jq -r '.agentRuntime.namespaceSecret // empty')
yes | bash .claude/skills/deploy-stg/scripts/deploy-stg.sh --skip-build --tag <existing-or-new>
# (or just re-run create.sh upgrade with --image <current> + the three env vars set)
```

- [ ] **Step 4: Confirm Odoo now has the webhook secret**

```bash
kubectl --context woow-k3s -n dev-sandbox-stg exec deploy/stg-odoo-dev-sandbox-odoo -- \
  sh -c 'printf "WEBHOOK len=%s\n" "${#AGENT_RUNTIME_WEBHOOK_SECRET}"'
```
Expected: `WEBHOOK len=64`. Then cross-check it equals the operator's (sha256[:16] match, like the auth check earlier).

---

## Task 8: Verify the loop end-to-end (runtime observation)

- [ ] **Step 1: Baseline** — `agent_session.state` for project 2:

```bash
kubectl --context woow-k3s -n dev-sandbox-stg exec stg-odoo-dev-sandbox-postgres-0 -- \
  psql -U odoo -d woow_stg -t -A -F'|' -c \
  "SELECT state,last_synced_at FROM woow_paas_platform_agent_session WHERE project_id=2;"
```
Expected (before): `absent|`.

- [ ] **Step 2: Trigger a chat turn** for project 2 / task test4 (channel 7) — via `playwright-cli` (login admin, send a message) OR re-use the verified UI path from this session.

- [ ] **Step 3: Observe the webhook landed** — operator + Odoo logs:

```bash
kubectl --context woow-k3s -n paas-stg-system logs deploy/paas-operator-stg --since=2m | grep -iE "state-update|webhook|c2ptg2sx4ozusxyb"
kubectl --context woow-k3s -n dev-sandbox-stg logs deploy/stg-odoo-dev-sandbox-odoo --since=2m | grep -iE "webhook|signature" || echo "(no 401 — good)"
```
Expected: operator dispatched; NO "invalid/missing HMAC signature" 401 in Odoo.

- [ ] **Step 4: Confirm state advanced**

```bash
kubectl --context woow-k3s -n dev-sandbox-stg exec stg-odoo-dev-sandbox-postgres-0 -- \
  psql -U odoo -d woow_stg -t -A -F'|' -c \
  "SELECT state,pod_ws_url,last_synced_at FROM woow_paas_platform_agent_session WHERE project_id=2;"
```
Expected (after): `active|...|<recent ts>` — state off `absent`, `last_synced_at` stamped. **This is the success criterion.**

- [ ] **Step 5: Regression** — confirm the chat round-trip still works (UI shows "round-trip 已接通" + fresh agent-pod `user_message_received` log), i.e. the webhook addition didn't break the chat path.

---

## Task 9 (OPTIONAL hardening): drift-cron rescue + pitfall doc

- [ ] **Step 1 (optional):** Decide whether `_cron_drift_detection` should also reconcile `absent` sessions that have a live pod. Tradeoff: polling every `absent` session (incl. never-spawned projects) adds operator `get_status` load. If pursued, scope the domain to `absent` sessions with a non-null `project_namespace_hash` AND a recent chat — NOT a blanket include. Leave OUT of core unless Task 8 shows missed-webhook gaps.

- [ ] **Step 2: Document** — add `docs/tracking/pitfalls/agent-session-state-webhook-loop-unwired.md` (link from `[[agent-runtime-stg-enablement-gap-chain]]` as the 6th layer) capturing: operator dispatcher unwired + webhook secret un-provisioned + drift-cron absent-exclusion; how to verify (Task 8). Run `/docs:new tracking ...` or write directly.

- [ ] **Step 3: Commit**

```bash
git add docs/tracking/pitfalls/agent-session-state-webhook-loop-unwired.md
git commit -m "docs: pitfall — agent_session.state stuck absent (unwired §5.5 webhook loop)"
```

---

## Self-Review notes

- **Spec coverage:** operator-sends-webhook (Tasks 1-3), secret on both sides (Tasks 4-7), state leaves absent (Task 8). ✓
- **Cross-repo flag:** Task 6b (operator secret/env in `paas-app-bundle-cd`) needs CD repo access; 6a `kubectl` bootstrap unblocks e2e meanwhile — explicitly called out, not hidden.
- **Idempotency / chat-path safety:** notify is background + best-effort + no-op without secret (Task 1 W.4/W.5) — chat never blocked.
- **Type/name consistency:** `notify_state_update(odoo_base_url, project_hash, new_state, pod_ws_url=None)` used identically in Tasks 1/2/3; env var `AGENT_RUNTIME_WEBHOOK_SECRET` consistent across operator read, create.sh, chart, deploy-stg.
