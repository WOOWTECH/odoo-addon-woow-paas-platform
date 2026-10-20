import assert from "node:assert/strict";
import test from "node:test";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const hooks = { mounted: [], patched: [], unmount: [] };
const refs = new Map();
let trappedRoot = null;
let restoredInert = 0;
globalThis.__formHooks = hooks;
globalThis.__formRefs = refs;
globalThis.__recordTrap = (root) => { trappedRoot = root; };
globalThis.__recordInertRestore = () => { restoredInert++; };
const formSource = stubImports(await readPaas("components/llm/LlmKeyFormModal.js"), {
  "@odoo/owl": `class Component {} const onMounted = (fn) => globalThis.__formHooks.mounted.push(fn); const onPatched = (fn) => globalThis.__formHooks.patched.push(fn); const onWillUnmount = (fn) => globalThis.__formHooks.unmount.push(fn); const useRef = (name) => { const ref = { el: null }; globalThis.__formRefs.set(name, ref); return ref; }; const useState = (value) => value;`,
  "@web/core/l10n/translation": "const _t = (message) => message;",
  "../icon/WoowIcon": "const WoowIcon = {};",
  "../button/WoowButton": "const WoowButton = {};",
  "../../services/llm_service": "const llmService = { modelsLoaded: true, models: { items: [] } };",
  "../../services/llm_errors": "const llmErrorMessage = (result) => result.code;",
  "../../utils/modal_focus": `const inertOutside = () => () => globalThis.__recordInertRestore(); const trapTab = (event, root) => { globalThis.__recordTrap(root); if (event.key !== "Tab") return; event.preventDefault(); const nodes = root?.focusable || []; if (!nodes.length) return root?.focus(); const active = globalThis.document.activeElement; if (event.shiftKey && (active === nodes[0] || !root.contains(active))) nodes.at(-1).focus(); else if (!event.shiftKey && (active === nodes.at(-1) || !root.contains(active))) nodes[0].focus(); };`,
});
const { LlmKeyFormModal } = await import(toDataUrl(formSource));

class FakeElement {
  constructor(name, focusable = []) {
    this.name = name;
    this.focusable = focusable;
    this.isConnected = true;
    this.ownerDocument = globalThis.document;
    this.children = [];
  }
  focus() { globalThis.document.activeElement = this; }
  contains(target) { return this === target || this.children.some((child) => child === target || child.contains?.(target)); }
}

test("form lifecycle traps focus in discard alertdialog and restores focus/cleanup", () => {
  const listeners = new Set();
  globalThis.document = {
    activeElement: null,
    addEventListener(_name, fn) { listeners.add(fn); },
    removeEventListener(_name, fn) { listeners.delete(fn); },
  };
  const modal = new LlmKeyFormModal();
  modal.props = { onClose() {}, onDraft() {} };
  modal.setup();
  const { dialogRef, confirmRef, keepEditingRef } = modal;
  const outside = new FakeElement("outside");
  const dialog = new FakeElement("form-dialog");
  const formCancel = new FakeElement("form-cancel");
  const keepEditing = new FakeElement("keep-editing");
  const closeForm = new FakeElement("close-form");
  const confirm = new FakeElement("discard-alertdialog", [keepEditing, closeForm]);
  dialog.children = [formCancel, confirm];
  confirm.children = [keepEditing, closeForm];
  dialogRef.el = dialog;
  confirmRef.el = confirm;
  keepEditingRef.el = keepEditing;

  globalThis.document.activeElement = outside;

  hooks.mounted.at(-1)();
  assert.equal(globalThis.document.activeElement, dialog, "initial focus enters the form dialog");
  assert.equal(listeners.size, 1);

  modal.state.name = "unsaved draft";
  formCancel.focus();
  modal.requestClose();
  assert.equal(modal.state.confirmingClose, true);
  hooks.patched.at(-1)();
  assert.equal(globalThis.document.activeElement, keepEditing, "alertdialog opens on the safe cancel action");

  const tab = { key: "Tab", shiftKey: false, preventDefault() {} };
  modal.onKeydown(tab);
  assert.equal(trappedRoot, confirm, "Tab trap targets only the alertdialog");
  assert.equal(globalThis.document.activeElement, keepEditing);
  keepEditing.focus();
  const shiftTab = { key: "Tab", shiftKey: true, preventDefault() {} };
  modal.onKeydown(shiftTab);
  assert.equal(globalThis.document.activeElement, closeForm, "Shift+Tab wraps to the final alertdialog action");

  modal.cancelClose();
  hooks.patched.at(-1)();
  assert.equal(globalThis.document.activeElement, formCancel, "cancel returns focus to the invoking form control");

  hooks.unmount.at(-1)();
  assert.equal(listeners.size, 0, "keydown listener is removed on unmount");
  assert.equal(restoredInert, 1, "inert background state is restored on unmount");
  assert.equal(globalThis.document.activeElement, outside, "unmount returns focus to the original opener");
});
