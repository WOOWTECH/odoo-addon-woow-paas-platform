# Smart Home durable provision prototype Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> Repository authority retires superpowers; the current authorized execution uses writing-plans, tdd and verification-before-completion directly. No missing plugin installation or extra approval is required.

**Goal:** Build an off-default, reviewable queue-to-anchored-tunnel slice that never blindly recreates a tunnel after an ambiguous first create.

**Architecture:** Normal Odoo requests persist immutable intent and return accepted/pending; a private cron worker performs external effects only after committed intent. It commits a create-may-have-started marker before one bare-create call, commits a received ID before token/configuration, and quarantines ambiguous outcomes. Existing verified security/UI fixes remain the committed base; automatic adoption, authoritative absence, compensation automation and commercial release are outside this prototype.

**Tech Stack:** Odoo18/PostgreSQL17 independent cursors, existing PaaSOperatorClient, FastAPI/Pydantic/httpx, OWL, own k3s sandbox and loopback-only Fake-CF HTTP ledger.

---

## Accepted seams and scope

Parent explicitly requested real Odoo request enqueue, operator bare-create, worker restart/committed ID, unknown-result no-recreate, safe queued UI, own independent DB/process-barrier evidence. This is the pre-agreed integration scope. Use private capability markers only for platform enqueue/operation writes; no normal request commit, new billing/order path, production credentials or namespace network widening.

Base commit c3704efb596205fd04421f7a43b8d8bd21867938, worktree candidate-c1-prototype. Preserve manifest225 and frozen candidates. Default Odoo flag absent/False and operator setting False; prototype cron inactive unless explicitly enabled in own QA. Existing unfinished operations remain guarded even if the flag is later disabled.

## Task 1: Bare-create API vertical slice

**Files:** modify extra/paas-operator/src/config.py, src/models/schemas.py, src/api/tunnels.py; create tests/test_tunnel_bare_prototype.py.

1. Write an ASGI public endpoint test: enabled synthetic account permits POST /api/tunnels/bare with name/account; response contains only ID/name/account and provider create count1, token/config/DNS count0.
2. Run operator-venv python -m pytest tests/test_tunnel_bare_prototype.py -p no:cacheprovider. Expected RED404 before endpoint exists.
3. Add off-default config boolean, strict request/response schemas, readonly provider-binding endpoint and truly bare create. Validate expected account against configured account before CF POST; reject missing/mismatched account without mutation. Keep existing all-in-one endpoint unchanged.
4. Run same test GREEN, then add separate disabled/auth/account mismatch/provider failure/malformed result tests one slice at a time. Errors use fixed code/stage/outcome, no raw credential/body.
5. Add PaaSOperatorClient provider-binding/bare-create methods. New code never treats existing hostname=None create as bare.

## Task 2: Protected immutable Odoo intent

**Files:** create src/models/smart_home_provision_operation.py and src/tests/test_shc_durable_provision_prototype.py; modify src/models/__init__.py, src/security/ir.model.access.csv, src/hooks.py.

1. Write addon tests for enqueue no remote mutation, spoofed operation writes rejected even sudo, feature off preserving original behavior and flag registered for uninstall.
2. Run own sandbox class and preserve failing result before adding model.
3. Operation fields: UUID/home/workspace/requester/subscription, operator URL/domain/env/provider-account snapshot, fixed name/hostname/service, phase/generation/lease, remote ID/name, sanitized code. One unfinished operation per home with DB unique partial index; CRUD requires in-process object sentinel, system read ACL only. No token field in operation.
4. Add private enqueue checks: workspace active, existing real subscription active, caller role/privilege, field access preflight. Only new pending/no-ID rows can create new intent; legacy no-ID error/provisioning require read-only manual investigation. Existing stored-ID homes retain same-ID converge when no unfinished operation.
5. Lock home row for enqueue/dedup/delete. Normal transaction only, no commit. New journal FKs conservatively retain lifecycle mapping; prototype deletion of any journal-bearing home/workspace is blocked rather than silently losing recovery evidence.
6. Keep guard active for existing journal records if prototype flag switches off.

## Task 3: Committed worker stages

**Files:** operation model; create src/data/smart_home_provision_prototype_cron.xml; modify src/__manifest__.py.

1. Write restart test using committed QA fixture and real child process. An independent connection must see queued intent; uncommitted/rolled-back request must remain invisible to worker.
2. Private worker acquires short transaction row lock/generation, validates immutable mapping and requester membership/subscription, commits create_unknown marker, releases transaction, calls bare-create once.
3. Unknown result or worker crash leaves marker durable; another invocation cannot POST again. No inventory adoption/delete/recreate endpoint in prototype.
4. Validate the received ID/name/account against the immutable snapshot and commit provider evidence to the trusted journal first. In a separate short transaction, revalidate current requester authority and home mapping before attaching the immutable home anchor. Authority or mapping drift preserves the known journal ID and quarantines the operation; no token/configuration call follows. Any uncommitted provider response still leaves the durable unknown marker.
5. Resume known-ID only: validate binding/snapshot/authority, retrieve token into privileged home field, same-ID existing converge. Token/config failures retain anchor and error; active only after converge succeeds. Stage transactions use registry cursors; never share an HTTP request cursor or hold a home lock while updating it from another cursor.
6. Bounded lease supports retrying anchored configuration and quarantines expired unknown create; stale generation cannot overwrite a later lease.

## Task 4: Pending BFF and OWL

**Files:** src/controllers/smart_home.py; CreateSmartHomeModal.js; SmartHomeDetailPage.js/xml; operation extension to_dict.

1. Write enabled-prototype HttpCase asserting accepted=True/success=False/pending data and opaque operation status, no token/provider IDs in operation payload. Auth negatives still zero provider mutations.
2. Capture action return and handle accepted separately before state!=active. Existing feature-off SEC03 contract remains unchanged.
3. Create modal navigates to pending home without showing done; detail consumes accepted and starts normal polling. Server provides retry_allowed/phase; unknown has no blind-create control. Show concise pending/needs-confirmation status, not implementation details.
4. Run actual compiled own-QA UI; preserve original224/225 UI files and results.

## Task 5: Real durable fault matrix in separate own DB

**Files:** create scripts/audit/shc-prototype-committed-integration.py and fixture Fake-CF server/process wrapper under tests/support or task evidence.

1. Create empty woow_shc_c1_proto_1011 DB in own existing PG. Install same addon dependencies with cron0, synthetic key/operator loopback, invalid domain, all payment providers disabled/mail absent.
2. Stage Fake-CF/adapter in own runner and bind localhost only; ledger uses SQLite/JSON fsync per call and barrier release signals. No Service/Ingress/public tunnel/CF key. Unit test actual operator route separately if full FastAPI fixture dependency staging is pending; explicitly distinguish this limitation.
3. Test queue rollback/commit visibility via separate registry cursors; duplicate enqueue from two committed sessions; enqueue never creates subscription/charge.
4. SIGKILL worker at explicit create-dispatched barrier and after fake provider durable create/response loss; restarted worker create count remains1 and journal unknown survives.
5. Token/ingress failure after success ID: independent cursor sees anchor before failure; restart converges same ID, create count1.
6. Inject anchor commit failure with genuine transaction rollback, response serialization failure outside worker transaction, stale generation and authority/snapshot changes. Verify no blind mutation or ledger loss.
7. Never infer durable recovery from HttpCase shared cursor alone. Record sanitized counts/stages/IDs, no code/token/password/provider body.

## Task 6: Review gates and limits

Run focused new tests, relevant existing406 once feature-off addon is installed, operator unit subset then full unit suite if runtime/source changes warrant it; update collection baseline using test_collection_audit.py --update-baseline. Run repo-lint --all, git diff --check, compile/XML validation. Generate docs index after adding this plan and prototype runbook; no manifest bump. Ask parent/security for independent review when substantive code is available.

Report prototype/off-default, never “commercial C1 fixed.” Remaining gates: provider account/zone mutation contracts across operator rolling update, authoritative inventory visibility/adoption, legacy unknown recovery runbook, partial cleanup journal, real worker throughput/alerting and broader live-provider fault contracts. No push/merge/build/prod deploy by this worktree.


## Isolation and prototype activation

This candidate is off by default and is not a commercial C1 completion. The
platform parameter `woow_paas_platform.shc_durable_provision_prototype_enabled`
and operator setting `SHC_DURABLE_PROVISION_PROTOTYPE_ENABLED` must both be
explicitly enabled in an isolated QA environment. The bundled cron is inactive
by default; runtime tests invoke only the private worker against a dedicated
empty QA database. No billing hooks, production cloning, live provider keys,
email, or automatic cleanup are enabled. Existing stored-ID legacy recovery
continues using the original path. When the prototype is enabled, a legacy
error/provisioning row without an operation and without a tunnel ID is rejected
for confirmation rather than creating another tunnel. Turning the flag off
cannot bypass an unfinished operation.

The public contract is `success=false, accepted=true` for queued/converging
setup. `success=true` is reserved for an active completed setup. Unknown first
create outcomes and invalid authority/provider binding produce a confirmation
state, with no adoption, deletion, or replacement create. The operation UUID is
traceability only and is not an ownership credential. Journal fields are
immutable after enqueue; trusted internal writes require an object-identity
sentinel, and even ordinary sudo/model RPC cannot forge it with JSON.

Verified received remote ID evidence commits to the private journal before
home authority is revalidated. Revocation, binding drift, or a late reply after
lease quarantine retains that evidence without authorizing home binding or
provider changes. The home anchor commits before token, ingress, DNS, or Access
configuration. Configuration retries keep the anchored ID. Every provider
create is preceded by a committed `create_unknown` marker. An expired marker
with an uncertain response is quarantined; it never treats absence of a local
ID as proof that the remote create did not happen.

Committed regression scripts are `scripts/audit/shc-durable-prototype-committed.py`
and `scripts/audit/shc-durable-prototype-provider.py`, with the actual operator
child in `scripts/audit/shc-durable-prototype-operator-child.py`. They require
explicit `SHC_PROTOTYPE_QA_NAMESPACE=dev-sandbox-shc-audit-*` and
`SHC_PROTOTYPE_QA_DB=woow_shc_c1_proto_*` plus the owned sandbox PostgreSQL host.
The operator test child uses pinned public Python wheels only in its isolated
PYTHONPATH. Its actual HTTP transport rejects every non-loopback endpoint. It
runs the real FastAPI app, API-key middleware, tunnel router and Cloudflare
service; lifespan is disabled, so it makes no Helm/Kubernetes startup claim.
The provider ledger fsyncs sanitized resource IDs/names before replying. Tests
use independent Odoo worker processes, real committed registry cursors and
process barriers, including SIGKILL, rather than shared TransactionCase
cursors as proof of durability.

## Remaining limitations and release gates

There is no automatic confirmation/reconciliation UI, provider-enforced
idempotency, verified absence/adoption protocol, or full cleanup/retention
workflow. All journal-bearing homes, including completed ones, are
conservatively blocked from deletion pending that separate design. A database
failure before received-ID evidence can commit still requires manual recovery.
Provider account binding checks do not atomically constrain later legacy
token/configuration endpoints during a rolling operator account change.
Configuration stages hold database locks during bounded synchronous network
requests; broader worker cancellation and resource budgeting are future work.
This prototype does not address the production HA dependency conflict, billing
price disclosure/invariants, infrastructure RBAC/backups, or the complete
commercial release checklist. It must not be merged/deployed/enabled in
production based only on these isolated tests.

## R2: Accepted retries and compiled UI

Actual R1 compiled UI reproduced an anchored retry returning `accepted=True` with a persisted `error` home, leaving the retry button enabled and polling stopped. R2 locks reused intents in the same operation→home order as the worker, invalidates both cached records, revalidates current authority and immutable local binding, and records an anchored retry as `home.state=provisioning` in the normal request transaction. The journal phase and known ID stay unchanged; no provider mutation or billing change occurs in the request. A busy worker returns a controlled error without waiting behind the operation lock. A completed operation observed after lock/cache refresh returns the actual active outcome and cannot downgrade the home. A disabled worker refuses an anchored retry and preserves the error/anchor.

The committed-process harness now exercises the public user action between configuration failure and worker restart, a user retry while the worker holds its operation/home locks, and a user process with an explicitly committed read barrier followed by a new anchored/error cached read while another process completes the worker. The real Odoo HTTP transaction retry service handles the resulting serialization conflict and refetches completed/active state. That barrier is a QA process interleave, not an early commit inside an HTTP controller. Native HTTP/browser evidence uses real buttons and the actual compiled bundle, with no interception or asset substitution. Unknown outcomes remain confirmation-only and never trigger blind create/adopt/delete.

Commercial gates remain unchanged: atomic provider account/zone/domain binding across all configuration endpoints, verified exact-resource reconcile/cleanup and retention, cron fairness/backoff, production backup/restore evidence, and coordinated release validation. Both flags remain off by default and the cron remains inactive. This is an opt-in prototype, not the commercial C1 fix.
