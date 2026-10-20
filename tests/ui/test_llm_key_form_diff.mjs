import assert from "node:assert/strict";
import test from "node:test";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

const formSource = stubImports(await readPaas("components/llm/LlmKeyFormModal.js"), {
  "@odoo/owl": "class Component {} const onMounted = () => {}; const onWillUnmount = () => {}; const useRef = () => ({ el: null }); const useState = (value) => value;",
  "@web/core/l10n/translation": "const _t = (message) => message;",
  "../icon/WoowIcon": "const WoowIcon = {};",
  "../button/WoowButton": "const WoowButton = {};",
  "../../services/llm_service": "const llmService = {};",
  "../../services/llm_errors": "const llmErrorMessage = (result) => result.code;",
  "../../utils/modal_focus": "const inertOutside = () => () => {}; const trapTab = () => {};",
});
const { paidKeyEditDiff } = await import(toDataUrl(formSource));

const paidModels = ["model-a"];
const legacyWhitespaceKey = {
  display_name: " Name ",
  limit_points: 100,
  models: null,
};

function editDraft(name) {
  return { name, limitPoints: "100", expires: "keep", models: null };
}

test("paid key edit keeps an untouched legacy whitespace name out of the diff", () => {
  assert.deepEqual(paidKeyEditDiff(legacyWhitespaceKey, editDraft(" Name "), paidModels), {});
});

test("paid key edit submits a trimmed name after an intentional whitespace cleanup", () => {
  assert.deepEqual(paidKeyEditDiff(legacyWhitespaceKey, editDraft("Name"), paidModels), { name: "Name" });
});

test("paid key edit submits an ordinary name change", () => {
  assert.deepEqual(paidKeyEditDiff(legacyWhitespaceKey, editDraft("Renamed"), paidModels), { name: "Renamed" });
});
