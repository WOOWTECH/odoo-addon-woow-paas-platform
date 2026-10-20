// #1414／#1456：回復對話框的「目前」取自 helm history 裡部署中的那一版（後端標 `is_current`），
// 不依賴平台帳本 `helm_revision`——帳本在更新／回復後會落後（INFRA-TS-003-1、AI-HER-007-1：
// 對話框把舊版標成「目前」，真正部署中的那一版反而能被選來「回復」）。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const toDataUrl = (source) =>
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

const source = (await readFile(
  new URL("../../src/static/src/paas/components/modal/RollbackModal.js", import.meta.url),
  "utf8",
))
  .replace('import { Component, useState, onMounted } from "@odoo/owl";',
    "class Component {} const useState = (v) => v; const onMounted = () => {};")
  .replace('import { _t } from "@web/core/l10n/translation";', "const _t = (m) => m;")
  .replace(/import \{ (WoowButton|WoowIcon) \} from "[^"]+";/g, "const $1 = {};")
  .replace('import { cloudService } from "../../services/cloud_service";',
    "const cloudService = {};")
  .replace('import { formatUserDateTime } from "../../utils/datetime_helpers";',
    "const formatUserDateTime = (v) => v;");
assert.doesNotMatch(source, /from "\.\.?\//, "所有相對 import 都必須被替換成 stub");
const { RollbackModal } = await import(toDataUrl(source));

const rev = (revision, status, extra = {}) => ({
  revision, status, chart: "tailscale-node-0.1.9", updated: "", description: "", ...extra,
});

function makeModal(helmRevision, revisions) {
  const modal = Object.create(RollbackModal.prototype);
  modal.props = { service: { id: 1, workspace_id: 2, helm_revision: helmRevision } };
  modal.state = { revisions, selectedRevision: null, rolling: false, confirming: false };
  return modal;
}

test("stale ledger: the deployed revision flagged by the backend is current", () => {
  // INFRA-TS-003-1 的形狀：rev 3 已部署，帳本還停在 2。
  const revisions = [
    rev(1, "superseded", { is_current: false }),
    rev(2, "superseded", { is_current: false }),
    rev(3, "deployed", { is_current: true }),
  ];
  const modal = makeModal(2, revisions);
  assert.equal(modal.currentRevision, 3);
  assert.equal(modal.isCurrent(revisions[2]), true);
  assert.equal(modal.isCurrent(revisions[1]), false, "舊版不得被標成「目前」");
  modal.selectRevision(revisions[1]);
  assert.equal(modal.state.selectedRevision, 2, "落後的那一版要能選來回復");
  modal.selectRevision(revisions[2]);
  assert.equal(modal.state.selectedRevision, 2, "部署中的那一版不能選");
});

test("ledger ahead of history (update in progress) still follows the deployed one", () => {
  const revisions = [
    rev(4, "deployed", { is_current: true }),
    rev(5, "pending-upgrade", { is_current: false }),
  ];
  assert.equal(makeModal(5, revisions).currentRevision, 4);
});

test("older backend without is_current falls back to the ledger", () => {
  const revisions = [rev(1, "superseded"), rev(2, "deployed")];
  assert.equal(makeModal(2, revisions).currentRevision, 2);
  assert.equal(makeModal(0, [rev(1, "deployed")]).currentRevision, 1);
});

test("no deployed revision flagged falls back to the ledger", () => {
  const revisions = [rev(1, "failed", { is_current: false })];
  assert.equal(makeModal(1, revisions).currentRevision, 1);
});
