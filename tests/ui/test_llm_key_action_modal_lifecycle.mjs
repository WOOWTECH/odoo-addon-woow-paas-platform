import assert from "node:assert/strict";
import test from "node:test";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const mountedHooks = [];
const unmountHooks = [];
const listenerCalls = [];
const inertCalls = [];
let activeElement = null;

const componentSource = stubImports(await readPaas("components/llm/LlmKeyActionConfirmModal.js"), {
  "@odoo/owl": `
    class Component {}
    const onMounted = (callback) => globalThis.__actionModalHooks.mounted.push(callback);
    const onWillUnmount = (callback) => globalThis.__actionModalHooks.unmount.push(callback);
    const useRef = (name) => ({ el: globalThis.__actionModalRefs[name] });
    const useState = (value) => value;
  `,
  "@web/core/l10n/translation": "const _t = (message) => message;",
  "../button/WoowButton": "const WoowButton = {};",
  "../../utils/modal_focus": `
    const inertOutside = (element) => {
      globalThis.__actionModalInert.calls.push(element);
      let restored = false;
      return () => { restored = true; globalThis.__actionModalInert.restores++; };
    };
    const trapTab = () => {};
  `,
});

globalThis.__actionModalHooks = { mounted: mountedHooks, unmount: unmountHooks };
globalThis.__actionModalRefs = {};
globalThis.__actionModalInert = { calls: inertCalls, restores: 0 };
globalThis.document = {
  get activeElement() { return activeElement; },
  addEventListener: (...args) => listenerCalls.push(["add", ...args]),
  removeEventListener: (...args) => listenerCalls.push(["remove", ...args]),
};

const { LlmKeyActionConfirmModal } = await import(toDataUrl(componentSource));

function createModal(trigger, fallback) {
  const dialog = { focusCalls: 0, focus() { this.focusCalls++; } };
  const backdrop = { id: "modal-backdrop" };
  globalThis.__actionModalRefs.backdrop = backdrop;
  globalThis.__actionModalRefs.dialog = dialog;
  const modal = new LlmKeyActionConfirmModal();
  modal.props = {
    item: { kind: "paid", display_name: "Production" }, action: "delete", busy: false,
    errorText: "", onClose() {}, onConfirm() {}, onReturnFocus: fallback,
  };
  activeElement = trigger;
  modal.setup();
  assert.equal(mountedHooks.length, 1, "setup registers the mounted callback");
  assert.equal(unmountHooks.length, 1, "setup registers the unmount callback");
  return { modal, dialog, backdrop };
}

test("mounted captures trigger, installs listener and inert state, and focuses dialog; unmount restores all", () => {
  const trigger = { isConnected: true, focusCalls: 0, focus() { this.focusCalls++; } };
  const { modal, dialog, backdrop } = createModal(trigger, () => assert.fail("connected trigger uses normal focus return"));
  const mounted = mountedHooks.shift();
  const unmounted = unmountHooks.shift();

  mounted();

  assert.equal(modal.returnFocus, trigger);
  assert.equal(dialog.focusCalls, 1, "dialog receives initial focus");
  assert.deepEqual(listenerCalls.at(-1), ["add", "keydown", modal.onKeydown]);
  assert.deepEqual(inertCalls, [backdrop], "the real inertOutside hook receives the modal backdrop");

  unmounted();

  assert.deepEqual(listenerCalls.at(-1), ["remove", "keydown", modal.onKeydown]);
  assert.equal(globalThis.__actionModalInert.restores, 1, "unmount calls the restore function returned by inertOutside");
  assert.equal(modal.restoreInert, null);
  assert.equal(trigger.focusCalls, 1, "connected trigger receives focus on unmount");
  assert.equal(modal.returnFocus, null);
});

test("unmount skips a detached trigger and returns focus through the supplied fallback", () => {
  const trigger = { isConnected: false, focusCalls: 0, focus() { this.focusCalls++; } };
  let fallbackCalls = 0;
  const { modal } = createModal(trigger, () => { fallbackCalls++; });
  const mounted = mountedHooks.shift();
  const unmounted = unmountHooks.shift();

  mounted();
  unmounted();

  assert.equal(trigger.focusCalls, 0, "detached trigger is never focused");
  assert.equal(fallbackCalls, 1, "onReturnFocus fallback runs once");
  assert.equal(listenerCalls.filter(([kind]) => kind === "add").length, 2);
  assert.equal(listenerCalls.filter(([kind]) => kind === "remove").length, 2);
  assert.equal(globalThis.__actionModalInert.restores, 2);
});
