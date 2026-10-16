// Local frontend integration: real direct-route parsing, Root props, page lifecycle,
// XML branches, JSON-RPC adapters and transport. OWL/UI components and HTTP are
// stubbed; this is not the repository's headed Odoo sandbox browser acceptance.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const sourceRoot = "../../src/static/src/paas/";
const toDataUrl = (source) =>
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const translate = (text, ...args) =>
  args.reduce((value, arg) => value.replace("%s", arg), text);

let constructing;
const dependencies = {
  "@web/core/l10n/translation": { _t: translate },
  "@odoo/owl": {
    Component: class {},
    reactive: (value) => value,
    useState: (value) => value,
    onMounted: (callback) => constructing.mounted.push(callback),
    onWillUpdateProps: (callback) => constructing.update.push(callback),
    onWillUnmount: (callback) => constructing.unmount.push(callback),
  },
};
globalThis.__configureGuardDependencies = dependencies;

async function loadModule(path, imports = {}) {
  const source = await readFile(new URL(sourceRoot + path, import.meta.url), "utf8");
  const rewritten = source.replace(
    /import\s*\{([^}]+)\}\s*from\s*"([^"]+)";/g,
    (_, names, specifier) => {
      const key = path + ":" + specifier;
      dependencies[key] = dependencies[specifier] || imports[specifier] ||
        Object.fromEntries(names.split(",").map((name) => [name.trim(), {}]));
      return `const { ${names} } = globalThis.__configureGuardDependencies[${JSON.stringify(key)}];`;
    },
  );
  assert.doesNotMatch(rewritten, /^import /m, "all imports must be explicitly replaced");
  return import(toDataUrl(rewritten));
}

const rpc = await loadModule("services/rpc.js");
const { workspaceService } = await loadModule("services/workspace_service.js", { "./rpc": rpc });
const { cloudService } = await loadModule("services/cloud_service.js", { "./rpc": rpc });
// invitation_context reads window.location at import time; the configure route does not use it.
const invitationContext = {
  captureInvitationFragment: () => {},
  clearInvitationToken: () => {},
  getInvitationRevision: () => 0,
};
const { router } = await loadModule("core/router.js", {
  "./platform_config": { showProjects: () => false },
  "./invitation_context": invitationContext,
});
const plans = await loadModule("components/subscription/plan_selector_utils.js");
const availability = await loadModule("utils/availability_label.js");
const credentials = await loadModule("components/modal/OneTimeCredentialsModal.js");
const breadcrumbs = [];
const { AppConfigurationPage } = await loadModule("pages/configure/AppConfigurationPage.js", {
  "../../services/workspace_service": { workspaceService },
  "../../services/cloud_service": { cloudService },
  "../../services/breadcrumb_service": { breadcrumbService: {
    set: (key, items) => breadcrumbs.push({ key, items }),
  } },
  "../../core/router": { router },
  "../../components/subscription/plan_selector_utils": plans,
  "../../utils/availability_label": availability,
  "../../components/modal/OneTimeCredentialsModal": credentials,
});
const { Root } = await loadModule("root.js", {
  "./core/router": { router }, "./services/cloud_service": { cloudService },
  "./pages/configure/AppConfigurationPage": { AppConfigurationPage },
});
const xml = await readFile(new URL(sourceRoot + "pages/configure/AppConfigurationPage.xml", import.meta.url), "utf8");
const rootXml = await readFile(new URL(sourceRoot + "root.xml", import.meta.url), "utf8");
assert.match(rootXml, /<AppConfigurationPage workspaceId="workspaceId" templateId="templateId"\/>/);

// Evaluate the actual outer XML branch chain, not a hand-written version of its gate.
// Nested sections use different indentation and are left inside their parent branch.
const outerBranches = [...xml.matchAll(
  /^            <t t-(if|elif|else)="([^"]*)">\n([\s\S]*?)^            <\/t>/gm,
)];
assert.ok(outerBranches.length >= 3, "the real loading/error/form branch chain is present");
function visibleForm(page) {
  for (const [, kind, condition, body] of outerBranches) {
    if (kind === "else" || Function("page", `with (page) { return Boolean(${condition}); }`)(page)) {
      return /id="service_name"/.test(body) && /onClick.bind="onLaunch"/.test(body);
    }
  }
  return false;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = async () => {
  for (let i = 0; i < 12; i++) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
};
function template(id) {
  return {
    id, name: `Template ${id}`, slug: "nextcloud", category: "storage",
    availability: { purchasable: true },
    helm_value_specs: { required: [], optional: [] },
    helm_default_values: { storage: "1Gi" },
  };
}
function workspace(id, role) {
  return { success: true, data: { id, role, name: `Workspace ${id}` } };
}
function response(result) {
  return { ok: true, status: 200, json: async () => ({ result }) };
}
function directProps(workspaceId = 41, templateId = 22) {
  globalThis.window.location.hash = `#/workspace/${workspaceId}/services/configure/${templateId}`;
  router.parseRoute();
  assert.equal(router.current, "configure", "the actual direct route resolves to configure");
  const root = Object.create(Root.prototype);
  root.router = router;
  return { workspaceId: root.workspaceId, templateId: root.templateId };
}
function fixture(role = "owner", override) {
  const calls = [];
  globalThis.window = { location: { hash: "" }, history: {}, addEventListener() {} };
  breadcrumbs.length = 0;
  // Deliberately misleading cached authority must never enable the form.
  workspaceService.workspaces = [{ id: 41, role: "owner", name: "Cached owner workspace" }];
  globalThis.fetch = async (url, options) => {
    const params = JSON.parse(options.body).params;
    calls.push({ url, params });
    const custom = override && await override(url, params);
    if (custom !== undefined) return response(custom);
    const wsMatch = url.match(/^\/api\/workspaces\/(\d+)$/);
    if (wsMatch) return response(role === "nonmember"
      ? { success: false, error: "Workspace not found or access denied" }
      : workspace(Number(wsMatch[1]), role));
    const plansMatch = url.match(/^\/api\/cloud-services\/templates\/(\d+)\/plans$/);
    if (plansMatch) return response({ success: true, data: [
      { id: Number(plansMatch[1]) + 100, unit: "month", price: 25 },
    ] });
    const templateMatch = url.match(/^\/api\/cloud\/templates\/(\d+)$/);
    if (templateMatch) return response({ success: true, data: template(Number(templateMatch[1])) });
    if (url.match(/^\/api\/workspaces\/\d+\/services$/) && params.action === "create") {
      return response({ success: true, data: { id: 501 } });
    }
    assert.fail(`Unexpected endpoint: ${url}`);
  };
  const hooks = { mounted: [], update: [], unmount: [] };
  constructing = hooks;
  const page = Object.create(AppConfigurationPage.prototype);
  page.props = directProps();
  page.setup();
  constructing = null;
  return {
    page, hooks, calls,
    creates: () => calls.filter((call) => call.params.action === "create"),
    async mount() {
      await Promise.all(hooks.mounted.map((callback) => callback()));
      await flush();
    },
    async switchRoute(workspaceId, templateId = 22) {
      assert.equal(hooks.update.length, 1, "prop changes must reload server authority");
      const props = directProps(workspaceId, templateId);
      const pending = hooks.update[0](props);
      await pending;
      // A superseded OWL update must not install its stale props.
      if (Number(router.params.id) === props.workspaceId &&
          Number(router.params.templateId) === props.templateId) page.props = props;
      await flush();
    },
  };
}
function validForm(page) {
  page.state.name = "Role Guard Service";
  page.state.selectedPlanId = page.props.templateId + 100;
}

for (const role of ["owner", "admin"]) {
  test(`${role}: direct route reads server role, shows form and launches with a valid plan`, async () => {
    const f = fixture(role);
    await f.mount();
    assert.equal(visibleForm(f.page), true);
    assert.equal(f.page.launchDisabled, false);
    assert.deepEqual(f.calls[0], { url: "/api/workspaces/41", params: { action: "get" } });
    assert.ok(breadcrumbs.at(-1).items.some((item) => item.label === "Workspace 41"),
      "breadcrumbs use the fresh workspace response");
    validForm(f.page);
    await f.page.onLaunch();
    assert.deepEqual(f.creates(), [{ url: "/api/workspaces/41/services", params: {
      action: "create", template_id: 22, name: "Role Guard Service",
      values: { storage: "1Gi" }, plan_id: 122,
    } }]);
    assert.equal(window.location.hash, "#/workspace/41/services/501");
  });
}

for (const [label, role] of [
  ["member/user", "user"], ["guest", "guest"], ["nonmember", "nonmember"],
  ["missing role", undefined], ["unknown role", "operator"],
]) {
  test(`${label}: direct route never exposes or submits the create form despite cached owner`, async () => {
    const f = fixture(role, role === undefined ? (url) =>
      url === "/api/workspaces/41" ? workspace(41, undefined) : undefined : undefined);
    await f.mount();
    assert.equal(visibleForm(f.page), false);
    assert.equal(f.page.launchDisabled, true);
    validForm(f.page);
    await f.page.onLaunch();
    assert.deepEqual(f.creates(), []);
    assert.deepEqual(f.calls, [{ url: "/api/workspaces/41", params: { action: "get" } }],
      "denied callers never load the purchasable template or plans");
  });
}

test("pending workspace authority hides the form and blocks a direct launch call", async () => {
  const pending = deferred();
  const f = fixture("owner", (url) => url === "/api/workspaces/41" ? pending.promise : undefined);
  const mounting = f.mount();
  assert.equal(visibleForm(f.page), false);
  assert.equal(f.page.launchDisabled, true);
  validForm(f.page);
  await f.page.onLaunch();
  assert.deepEqual(f.creates(), []);
  pending.resolve(workspace(41, "owner"));
  await mounting;
  assert.equal(visibleForm(f.page), true);
});

for (const [label, failure] of [
  ["API denial", { success: false, error: "Access denied" }],
  ["malformed success", { success: true }],
  ["wrong workspace payload", workspace(99, "owner")],
]) {
  test(`${label}: workspace response fails closed`, async () => {
    const f = fixture("owner", (url) => url === "/api/workspaces/41" ? failure : undefined);
    await f.mount();
    assert.equal(visibleForm(f.page), false);
    assert.equal(f.page.launchDisabled, true);
    validForm(f.page);
    await f.page.onLaunch();
    assert.deepEqual(f.creates(), []);
  });
}

test("network failure while reading workspace leaves an error and no create form", async () => {
  const f = fixture("owner", (url) => {
    if (url === "/api/workspaces/41") throw new Error("offline");
  });
  await f.mount();
  assert.equal(visibleForm(f.page), false);
  assert.ok(f.page.state.error);
  assert.equal(f.page.launchDisabled, true);
});

test("owner to guest workspace switch clears authorization and typed form immediately", async () => {
  const next = deferred();
  const f = fixture("owner", (url) => url === "/api/workspaces/42" ? next.promise : undefined);
  await f.mount();
  validForm(f.page);
  f.page.state.helmValues = { privateSetting: "previous-workspace" };
  f.page.state.selectedAddonIds = [5];
  f.page.state.oneTimeCredentials = [{ label: "test", value: "synthetic" }];
  const changing = f.switchRoute(42);
  assert.equal(visibleForm(f.page), false);
  assert.equal(f.page.state.name, "");
  assert.deepEqual(f.page.state.helmValues, {});
  assert.deepEqual(f.page.state.selectedAddonIds, []);
  assert.equal(f.page.state.oneTimeCredentials, null);
  await f.page.onLaunch();
  assert.deepEqual(f.creates(), []);
  next.resolve(workspace(42, "guest"));
  await changing;
  assert.equal(visibleForm(f.page), false);
});

test("late owner response cannot override a newer denied workspace route", async () => {
  const old = deferred();
  const f = fixture("guest", (url) => url === "/api/workspaces/41" ? old.promise : undefined);
  const mounting = f.mount();
  await f.switchRoute(42);
  old.resolve(workspace(41, "owner"));
  await mounting;
  assert.equal(f.page.props.workspaceId, 42);
  assert.equal(visibleForm(f.page), false);
  assert.equal(f.page.launchDisabled, true);
  assert.equal(f.calls.some((call) => call.url.includes("/templates/")), false);
});

test("late denied response cannot hide the newer authorized workspace and template", async () => {
  const old = deferred();
  const f = fixture("admin", (url) => url === "/api/workspaces/41" ? old.promise : undefined);
  const mounting = f.mount();
  await f.switchRoute(42, 23);
  old.resolve(workspace(41, "guest"));
  await mounting;
  assert.equal(visibleForm(f.page), true);
  assert.equal(f.page.template.id, 23);
  assert.equal(f.page.state.selectedPlanId, 123);
  validForm(f.page);
  await f.page.onLaunch();
  assert.equal(f.creates()[0].url, "/api/workspaces/42/services");
  assert.equal(f.creates()[0].params.template_id, 23);
});

test("late plans response cannot restore the previous route's form", async () => {
  const oldPlans = deferred();
  const f = fixture("owner", (url) =>
    url === "/api/cloud-services/templates/22/plans" ? oldPlans.promise :
    url === "/api/workspaces/42" ? workspace(42, "user") : undefined);
  const mounting = f.mount();
  await flush();
  assert.ok(f.calls.some((call) => call.url.endsWith("/22/plans")));
  await f.switchRoute(42, 23);
  oldPlans.resolve({ success: true, data: [{ id: 122, unit: "month", price: 25 }] });
  await mounting;
  assert.equal(visibleForm(f.page), false);
  assert.equal(f.page.template, null);
  assert.deepEqual(f.page.state.plans, []);
});

test("same workspace template change reloads the current template and defaults", async () => {
  const f = fixture("admin");
  await f.mount();
  validForm(f.page);
  await f.switchRoute(41, 23);
  assert.equal(visibleForm(f.page), true);
  assert.equal(f.page.template.id, 23);
  assert.equal(f.page.state.selectedPlanId, 123);
  assert.equal(f.page.state.name, "");
  assert.equal(f.calls.filter((call) => call.url === "/api/workspaces/41").length, 2);
});

test("unmounted page ignores late workspace responses without loading templates", async () => {
  const pending = deferred();
  const f = fixture("owner", (url) => url === "/api/workspaces/41" ? pending.promise : undefined);
  const mounting = f.mount();
  assert.equal(f.hooks.unmount.length, 1);
  f.hooks.unmount[0]();
  pending.resolve(workspace(41, "owner"));
  await mounting;
  assert.equal(f.page.template, null);
  assert.equal(f.calls.length, 1);
});

test("duplicate launch sends one create; late success does not redirect a new workspace", async () => {
  const created = deferred();
  const f = fixture("owner", (url, params) =>
    params.action === "create" ? created.promise :
    url === "/api/workspaces/42" ? workspace(42, "guest") : undefined);
  await f.mount();
  validForm(f.page);
  const first = f.page.onLaunch();
  await flush();
  const second = f.page.onLaunch();
  await flush();
  const createCount = f.creates().length;
  await f.switchRoute(42);
  created.resolve({ success: true, data: {
    id: 501, admin_password: "synthetic-fixture-only",
    template: { admin_reset_supported: false },
  } });
  await Promise.all([first, second]);
  assert.equal(createCount, 1);
  assert.equal(window.location.hash, "#/workspace/42/services/configure/22");
  assert.equal(f.page.state.oneTimeCredentials, null);
  assert.equal(visibleForm(f.page), false);
});

test("rapid A to B to A return rechecks A even before B's props are installed", async () => {
  const pendingB = deferred();
  const f = fixture("owner", (url) => url === "/api/workspaces/42" ? pendingB.promise : undefined);
  await f.mount();
  const toB = f.switchRoute(42);
  try {
    assert.equal(visibleForm(f.page), false);
    await f.switchRoute(41);
    assert.equal(visibleForm(f.page), true);
    assert.equal(f.calls.filter((call) => call.url === "/api/workspaces/41").length, 2);
  } finally {
    pendingB.resolve(workspace(42, "guest"));
    await toB;
  }
  assert.equal(f.page.props.workspaceId, 41);
  assert.equal(visibleForm(f.page), true);
});

test("unchanged props preserve typed inputs without refetching role", async () => {
  const f = fixture("admin");
  await f.mount();
  validForm(f.page);
  const reads = f.calls.length;
  await f.switchRoute(41);
  assert.equal(f.page.state.name, "Role Guard Service");
  assert.equal(f.calls.length, reads);
  assert.equal(visibleForm(f.page), true);
});

test("backend rejection after an authorized load is shown without pretending creation succeeded", async () => {
  const f = fixture("owner", (url, params) => params.action === "create"
    ? { success: false, error: "Permission denied" } : undefined);
  await f.mount();
  validForm(f.page);
  await f.page.onLaunch();
  assert.equal(f.creates().length, 1);
  assert.equal(f.page.state.launchError, "Permission denied");
  assert.equal(f.page.state.launching, false);
  assert.equal(window.location.hash, "#/workspace/41/services/configure/22");
});

test("authorized role with an unavailable template cannot submit programmatically", async () => {
  const f = fixture("owner", (url) => url === "/api/cloud/templates/22"
    ? { success: true, data: { ...template(22), availability: { purchasable: false, reason: "paused" } } }
    : undefined);
  await f.mount();
  assert.equal(visibleForm(f.page), true);
  assert.equal(f.page.launchDisabled, true);
  validForm(f.page);
  await f.page.onLaunch();
  assert.deepEqual(f.creates(), []);
});

test("late template response cannot restore the previous route's form or load its plans", async () => {
  const oldTemplate = deferred();
  const f = fixture("owner", (url) =>
    url === "/api/cloud/templates/22" ? oldTemplate.promise :
    url === "/api/workspaces/42" ? workspace(42, "guest") : undefined);
  const mounting = f.mount();
  await flush();
  assert.ok(f.calls.some((call) => call.url === "/api/cloud/templates/22"));
  await f.switchRoute(42, 23);
  oldTemplate.resolve({ success: true, data: template(22) });
  await mounting;
  assert.equal(visibleForm(f.page), false);
  assert.equal(f.page.template, null);
  assert.equal(f.calls.some((call) => call.url.endsWith("/22/plans")), false);
});

test("owner creation preserves the real one-time credentials helper and close navigation", async () => {
  const f = fixture("owner", (url, params) => params.action === "create"
    ? { success: true, data: {
      id: 501, admin_password: "synthetic-fixture-only",
      template: { admin_reset_supported: false },
    } } : undefined);
  await f.mount();
  validForm(f.page);
  await f.page.onLaunch();
  assert.equal(f.page.state.oneTimeCredentials.length, 2);
  assert.equal(f.page.state.oneTimeCredentials[1].value, "synthetic-fixture-only");
  assert.equal(f.page.state.createdServiceId, 501);
  assert.equal(window.location.hash, "#/workspace/41/services/configure/22");
  f.page.onCredentialsClose();
  assert.equal(f.page.state.oneTimeCredentials, null);
  assert.equal(window.location.hash, "#/workspace/41/services/501");
});

test("unmount after launch ignores late credentials and redirect", async () => {
  const created = deferred();
  const f = fixture("admin", (url, params) => params.action === "create" ? created.promise : undefined);
  await f.mount();
  validForm(f.page);
  const launching = f.page.onLaunch();
  await flush();
  f.hooks.unmount[0]();
  created.resolve({ success: true, data: {
    id: 501, admin_password: "synthetic-fixture-only",
    template: { admin_reset_supported: false },
  } });
  await launching;
  assert.equal(f.page.state.oneTimeCredentials, null);
  assert.equal(f.page.state.createdServiceId, null);
  assert.equal(window.location.hash, "#/workspace/41/services/configure/22");
});

test("admin Odoo creation keeps selected addon IDs in the existing valid payload", async () => {
  const f = fixture("admin", (url) => url === "/api/cloud/templates/22"
    ? { success: true, data: { ...template(22), slug: "odoo" } } : undefined);
  await f.mount();
  validForm(f.page);
  f.page.state.selectedAddonIds = [7, 8];
  await f.page.onLaunch();
  assert.deepEqual(f.creates()[0].params.odoo_addon_ids, [7, 8]);
});
