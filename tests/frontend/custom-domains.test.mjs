// Run: node --test tests/frontend/custom-domains.test.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = (path) => readFile(new URL(`../../src/static/src/paas/${path}`, import.meta.url), "utf8");
const importSource = (code) => import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
const polling = await importSource(await source("utils/custom_domains.js"));
const settle = () => new Promise((resolve) => setImmediate(resolve));

function pollerFixture(load) {
    const timers = new Map();
    const values = [];
    const errors = [];
    let nextId = 0;
    const poller = polling.createCustomDomainPoller({
        load,
        onData: (value) => values.push(value),
        onError: (error) => errors.push(error),
        schedule(fn, delay) { const id = ++nextId; timers.set(id, { fn, delay }); return id; },
        cancel(id) { timers.delete(id); },
    });
    return { poller, timers, values, errors };
}

test("domain polling never overlaps and coalesces refreshes", async () => {
    let finish;
    let calls = 0;
    const fixture = pollerFixture(() => {
        calls += 1;
        return new Promise((resolve) => { finish = resolve; });
    });
    fixture.poller.start();
    fixture.poller.refresh();
    fixture.poller.refresh();
    assert.equal(calls, 1);
    finish("first");
    await settle();
    assert.equal(calls, 2, "multiple refreshes become one follow-up");
    finish("second");
    await settle();
    assert.deepEqual(fixture.values, ["first", "second"]);
    assert.equal([...fixture.timers.values()][0].delay, 10000);
    fixture.poller.stop();
});

test("stopping the modal discards an in-flight result and future schedule", async () => {
    let finish;
    const fixture = pollerFixture(() => new Promise((resolve) => { finish = resolve; }));
    fixture.poller.start();
    fixture.poller.stop();
    finish("late");
    await settle();
    assert.deepEqual(fixture.values, []);
    assert.equal(fixture.timers.size, 0);
});

const modalRaw = await source("components/modal/EditDomainModal.js");
const modalSource = modalRaw.replace(/^import .*;\s*$/gm, "").replace(/^export /gm, "");
const translate = (message, ...args) => {
    let index = 0;
    return message.replace(/%s/g, () => args[index++]);
};
const { EditDomainModal, domainStatus } = new Function(
    "Component", "onMounted", "onWillUnmount", "useState", "_t", "WoowButton", "WoowIcon", "cloudService", "createCustomDomainPoller",
    `${modalSource}; return { EditDomainModal, domainStatus };`,
)(class {}, () => {}, () => {}, (value) => value, translate, class {}, class {}, {}, () => ({}));

test("only the exact active state is presented as active and unknown states stay explicit", () => {
    assert.deepEqual(domainStatus("active"), { label: "Active", tone: "success" });
    for (const state of ["pending_dns", "verifying", "issuing", "configuring", "waiting_cname", "error", "removing", "removed", "needs_review"]) {
        assert.notEqual(domainStatus(state).tone, "success", state);
    }
    assert.deepEqual(domainStatus("future_state"), { label: "Unknown status: future_state", tone: "warning" });
});

test("create and check are capability-gated while an existing domain remains removable", async () => {
    const component = new EditDomainModal();
    component.props = { service: { id: 7, custom_domain_capability: { supported: false, reason: "disabled", max_domains: 2 } }, onSaved() {} };
    component.setup();
    component.state.hostname = "app.example.com";
    assert.equal(component.canCreate, false);
    const calls = [];
    component.perform = async (action, data) => calls.push({ action, data });
    component.checkDomain({ id: 11, state: "error" });
    await component.removeDomain({ id: 11, state: "error" });
    assert.deepEqual(calls, [{ action: "remove", data: { domain_id: 11 } }]);
});

test("frontend contract uses the dedicated JSON-RPC route and backend DNS records", async () => {
    const serviceSource = await source("services/cloud_service.js");
    const templateSource = await source("components/modal/EditDomainModal.xml");
    const detailSource = await source("pages/service/ServiceDetailPage.js");
    assert.match(serviceSource, /`\/api\/services\/\$\{serviceId\}\/domains`/);
    assert.match(detailSource, /Boolean\(this\.state\.service\?\.custom_domain_capability\?\.supported\)/);
    assert.match(detailSource, /customDomainManagementAvailable[\s\S]*Boolean\(this\.state\.service\?\.custom_domain_capability\?\.management_available\)/);
    assert.match(templateSource, /!state\.capability\.supported/);
    assert.match(templateSource, /domain\.dns_records/);
    assert.match(templateSource, /record\.type/);
    assert.match(templateSource, /record\.name/);
    assert.match(templateSource, /record\.value/);
    assert.doesNotMatch(modalRaw + templateSource, /wassa|fullSubdomain|CNAME<\/span>/i);
    assert.doesNotMatch(templateSource, /is_primary|primary domain|make primary/i);
});
