# LLM-12b I2 persisted candidate foundation — 2026-10-21

Base: c0d703353b94e800207aff1f5cd08849c73a4415. This is a dormant foundation,
**not completion of I2, I3, or LLM-12b**. No live cluster operations.

## Behavior

- New private service model `woow_paas_platform.llm_reconcile_candidate`, one
  observation state per account. No cron, routes, AP calls, incidents, or alerts.
- `_observe(account_id, BalanceSample)` first returns on an unconfigured DB.
  Otherwise it requires sudo and a verified caller-provided sample at most
  60 seconds old, no future timestamp, strictly newer than accepted state.
- A NOWAIT account row lock protects local lifecycle observation. Contention or
  PostgreSQL serialization failures propagate for caller transaction retry.
  No independent transaction/commit is hidden here. Callers must perform any
  upstream sample collection before entry; this lock must never span HTTP.
- L = credited quota; U = credited + sending/unknown/needs_review quota, scoped
  to user/manual/dr/recharge. All other states (including credit_lost) excluded.
  One SQL statement also sums signed known discrepancies. Each SUM is cast to
  text in PostgreSQL and converted to Python int, including aggregates >int64.
- Existing pure `llm_reconcile_balance` recalculates both endpoints against the
  current ledger/discrepancy at each observation. Five-minute baseline fields
  persist as bigint columns; no process-local candidate storage. Traffic restarts
  downward checks. A downward mismatch retains its quiet baseline for future
  sink/deduplication wiring; there is no incident effect now.
- I3 evidence is always absent, never represented as a zero count. Upward
  rechecks remain `evidence_pending`. Closed/missing accounts discard their
  candidate and skip balance classification. Account deletion cascades state.
- CRUD requires a module-local object capability even under sudo. Only system
  administrators have read ACL; no frontend/API exposure or write ACL.

## Executed verification

- `python tests/test_llm_reconcile_candidate.py -v`: **8 passed**. Fake adapter
  storage tests >2^53 exact values, rehydration in a fresh worker object, 299/300
  seconds, changed bounds/signed discrepancies, traffic, stale/replayed/future
  samples, closed/missing accounts, unconfigured zero effects and sudo guard.
  These are explicitly **not** real Odoo/PostgreSQL or crash/restart tests.
- `python tests/test_llm_reconcile_balance.py -v`: **11 passed**.
- `python -m unittest discover -s tests -p 'test_llm_reconcile_*.py' -v`:
  **29 passed**, including the existing evidence normalization tests.
- `REPO_LINT_INDENT_BASE=c0d703353b94e800207aff1f5cd08849c73a4415
  <proxy-control-venv>/bin/python scripts/audit/repo-lint.py --all`: passed.
- `python scripts/audit/test_collection_audit.py --all`: passed; baseline adds
  six Odoo test methods. `git diff --check`: passed.

## Added but NOT executed here

`src/tests/test_llm_reconcile_candidate.py`: six tagged real Odoo tests, including
transaction-local PostgreSQL ledger fixtures for every source/state and >int64
SUM, signed deltas, ORM cache/environment reload, five-minute recheck, lifecycle,
capability and outward-effect tripwires. Temporary tables shadow only two ledger
read tables inside each rolled-back test; real candidate/account ORM tables are
used. The source/state matrix deliberately contains synthetic combinations;
it does not manufacture financial documents or claim valid business workflows.

No Odoo installation/upgrade or real PostgreSQL execution was performed because
this sidecar must avoid the live cluster. Parent must run tagged tests in an
owned sandbox and verify install/upgrade before PR/activation. Environment reload
within one transaction is not committed process-crash recovery; commit/restart
and concurrent-worker coverage remain pending.

## Integration prerequisites

A trusted sample/identity collector; shared AP budget; verified I3 coverage and
ownership; I8 freshness; bounded scheduler/deadline; persisted incident dedup and
sinks; real sandbox install/upgrade/unconfigured cron regression; staged financial
acceptance. No classification is a GA financial-consistency claim before these
are implemented and verified. Only Elmo merges PRs; this sidecar does not push.
