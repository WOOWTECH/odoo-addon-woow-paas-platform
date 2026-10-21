# I3 Logical Attempt Evidence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Collapse an ingested proxy `intent` and optional `outcome` into one conservative, immutable logical attempt without declaring that quota was credited or an order was owned.

**Architecture:** A pure service consumes the already normalized minimal event fields stored by #1900. It validates identity and ordering, returns only a fixed classification (`not_sent`, `correlation_candidate`, or `uncertain`), and never performs ORM writes, network calls, alerting, or account/order matching. Future I3 code must separately establish environment ownership, source generation, upstream type=1 log evidence, and a unique order claim.

**Tech Stack:** Python 3.12+ standard library, `unittest`, existing `src/services` and `.gitea/workflows/llm-services-unit-tests.yml`.

---

### Task 1: Specify the safe collapse behavior

**Files:**
- Create: `tests/test_llm_audit_attempt.py`
- Create: `src/services/llm_audit_attempt.py`

**Steps:**
1. Write tests for a complete pair, intent only, `not_sent`, every uncertain outcome, mismatched attempt IDs, reversed sequence, malformed types, and an exact sequence number above 2^53. Assert no path calls anything external or marks credit/order ownership.
2. Run `python tests/test_llm_audit_attempt.py -v`; confirm it fails because the service is missing.
3. Implement a frozen result with `attempt_id`, target/quota, event sequence(s), optional safe request ID, and fixed classification. Validate the exact minimal schema and reject inconsistent pairs. A forwarded response with a request ID is merely a correlation candidate; it is never a verified credit. Intent without outcome, forwarded without request ID, timeout/reset/connect/cancelled remain uncertain. `not_sent` is the only definitely unsent outcome.
4. Run the test file again and confirm all tests pass. Run `python -m compileall -q src/services/llm_audit_attempt.py`.

### Task 2: Keep the helper in the pure-test CI and document its limit

**Files:**
- Modify: `.gitea/workflows/llm-services-unit-tests.yml`
- Modify: `docs/index.md` (generated index for this plan)
- Modify: `docs/validation/llm12b-i3-audit-ingest-1021.md`

**Steps:**
1. Add the two paths to the existing workflow trigger and invoke the test file in the pure-unit-tests step.
2. State in the validation document that this dormant classifier cannot establish order ownership, a type=1 log match, or complete audit coverage.
3. Run `python scripts/audit/gen-docs-index.py`, then `python scripts/audit/gen-docs-index.py --check`.
4. Run `REPO_LINT_INDENT_BASE=feat/llm-12b-i3-audit-ingest-1021 python scripts/audit/repo-lint.py --all`, test-collection C2, `git diff --check`, and the new unit tests.
5. Commit and open a draft stacked PR on #1900. Mark ready only if exact-head CI succeeds; Elmo alone merges it.
