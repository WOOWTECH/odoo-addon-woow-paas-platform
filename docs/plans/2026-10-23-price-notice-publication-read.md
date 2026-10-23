# Price notice publication read seam implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This session is already authorized to continue; Codex executes this isolated worktree and leaves merges to Elmo.

**Goal:** Let the shared AI page read a frozen, allowlisted price notice from durable Odoo state while every publication and pricing writer remains disabled.

**Architecture:** Add a publication record separate from #1936's immutable draft and recipients. Its ordinary ORM create/write/delete/copy methods always reject, including sudo; a future reviewed publisher must supply a trusted insertion path and receipt. The authenticated endpoint reads only records whose campaign binding, runtime environment and public DTO validate, and only when a separate default-off setting is enabled. It does not read the draft ledger as a notice or mark visibility evidence.

**Tech Stack:** Odoo 18 models and controller, PostgreSQL constraints, Python 3.12 `unittest` and Odoo `TransactionCase`, existing UI DTO contract.

---

### Task 1: Pure public DTO validation

**Files:** `src/services/llm_price_notice_public.py`, `tests/test_llm_price_notice_public.py`

1. Test rejection of private keys, malformed rates, oversized text/collections, duplicate IDs and noncanonical date/price fields; assert no private data survives projection.
2. Implement a strict whitelist matching the already merged `priceNoticeDtos` frontend contract. Return an empty list on any malformed stored row; never echo invalid raw JSON.
3. Run the focused Python tests, Ruff F and whitespace check.

### Task 2: Dormant Odoo publication table and reader

**Files:** `src/models/llm_price_notice_publication.py`, `src/models/__init__.py`, `src/security/ir.model.access.csv`, `src/tests/test_llm_price_notice_publication.py`, `src/tests/__init__.py`.

1. Add immutable publication rows with draft campaign binding, exact draft fingerprint, scoped environment, public payload JSON and withdrawal timestamp; unique per campaign. Administrators have read only ACL; no user has ORM create/write/delete. Override methods to reject even sudo, so a future publisher must explicitly introduce its own controlled insertion and withdrawal path.
2. Reader requires both `llm_price_notice_public_read_enabled` and `llm_price_notice_runtime_environment` to be explicitly set. Search only nonwithdrawn publications, check campaign fingerprint/environment/body/version against the public DTO, and return at most ten projected notices. No mail, clock, approval or pricing effects.
3. Odoo tests insert synthetic rows through a local SQL fixture to exercise the reader, assert the default returns empty, ACL and ORM mutation denial, environment/binding/withdrawal rejection, and no draft or address exposure. The SQL fixture is test-only, not a runtime publishing path.

### Task 3: Connect the authenticated empty endpoint to the reader

**Files:** `src/controllers/llm.py`, `tests/ui/test_llm_price_notices.mjs`, `docs/index.md`.

1. Keep `auth='user'`, POST JSON and `readonly=True`. The route calls only the private reader, with a fixed empty fallback for non-operational failures. It must not use the normal AI activation gate because a price notice can concern suspended or unactivated users.
2. Verify the existing UI still treats empty and valid DTOs correctly, including direct visits to six tabs. Add a focused route test only if the current Odoo test harness can exercise it without inventing a fake framework.
3. Run Python/JS focused checks, full repo lint, manifest/documentation gates, then dispatch the full Odoo suite on the PR head. Publish a draft PR with exact evidence. Keep it draft until the Odoo and security review gates are clear.

## Deliberate limits

- The table has no runtime writer. This PR does not publish, send, prove page visibility, start seven days, or authorize any price change. It remains empty on staging and production until a later authorized release adds a controlled writer and enables the reader after staging validation.
- Production legal copy, complete affected audience, effective catalog and real SMTP acceptance remain owner/legal and integration blockers. The new-api deny-only CLI gate remains in force.
