// Run: node --test tests/frontend/service-resources.test.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = (path) => readFile(new URL(`../../src/static/src/paas/${path}`, import.meta.url), "utf8");
const importSource = (code) => import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
const presentation = await importSource((await source("utils/resource_observations.js")).replace(
    'import { _t } from "@web/core/l10n/translation";',
    'const _t = (message, ...args) => { let i = 0; return message.replace(/%s/g, () => args[i++]); };',
));
const { createResourcePoller } = await importSource(await source("utils/resource_polling.js"));
const { resourceRow, retainResourceSamples } = presentation;
const now = Date.parse("2026-10-13T02:00:00Z");
const sampled_at = "2026-10-13T01:59:50Z";
const metric = (extra = {}) => ({ usage: 0, limit: 4, limit_status: "bounded", status: "ok", sampled_at, coverage: { observed: 2, expected: 2 }, ...extra });

test("real zero is displayed and unknown usage is never converted to zero", () => {
    const zero = resourceRow("cpu", metric(), { now });
    assert.equal(zero.usage, "0");
    assert.equal(zero.percent, 0);
    const missing = resourceRow("cpu", metric({ usage: null, status: "unavailable" }), { now });
    assert.equal(missing.usage, "—");
    assert.equal(missing.percent, null);
    assert.equal(missing.capacity, "4");
    for (const usage of [undefined, false, "0", NaN, Infinity, -1]) {
        assert.equal(resourceRow("cpu", metric({ usage }), { now }).usage, "—");
    }
});

test("uses live cores and binary bytes, without substituting requested configuration", () => {
    const ram = resourceRow("memory", metric({ usage: 12 * 1024 ** 3, limit: 64 * 1024 ** 3, request: 32 * 1024 ** 3 }), { now });
    assert.equal(ram.usage, "12");
    assert.equal(ram.capacity, "64");
    assert.equal(ram.unit, "GiB");
    assert.equal(ram.percent, 19);
    const disk = resourceRow("storage", { usage: 106 * 1024 ** 3, capacity: 252 * 1024 ** 3, requested: 256 * 1024 ** 3, status: "ok", sampled_at }, { now });
    assert.equal(disk.capacity, "252");
    assert.equal(disk.percent, 42);
});

test("partial totals and unlimited resources never produce misleading percentages", () => {
    const partial = resourceRow("cpu", metric({ usage: 2, status: "partial", coverage: { observed: 1, expected: 2 } }), { now });
    assert.equal(partial.usage, "2");
    assert.equal(partial.percent, null);
    assert.match(partial.note, /1\/2/);
    for (const limit_status of ["unknown", "unbounded"]) {
        const row = resourceRow("cpu", metric({ usage: 2, limit: 8, limit_status }), { now });
        assert.equal(row.capacity, "—");
        assert.equal(row.percent, null);
    }
});

test("stale data remain visibly stale; unsupported and stopped are not zero usage", () => {
    const stale = resourceRow("cpu", metric({ usage: 1, sampled_at: "2026-10-13T01:50:00Z" }), { now });
    assert.equal(stale.usage, "1");
    assert.equal(stale.status, "stale");
    assert.equal(stale.tone, "stale");
    assert.equal(resourceRow("cpu", metric(), { now, refreshFailed: true }).status, "stale");
    for (const status of ["stopped", "unsupported"]) {
        const row = resourceRow("cpu", metric({ status }), { now });
        assert.equal(row.usage, "—");
        assert.equal(row.percent, null);
    }
    assert.equal(resourceRow("cpu", metric({ usage: 5 }), { now }).width, 100);
    assert.equal(resourceRow("cpu", metric({ usage: 5 }), { now }).percent, 125);
});

test("a telemetry outage retains the last valid zero or value, but a stopped service clears it", () => {
    const previous = { status: "ok", cpu: metric(), memory: metric({ usage: 12 }), storage: metric({ usage: null }) };
    const outage = { status: "unavailable", cpu: { status: "unavailable" }, memory: { status: "unavailable" }, storage: { status: "unavailable" } };
    const retained = retainResourceSamples(previous, outage);
    assert.equal(retained.cpu.usage, 0);
    assert.equal(retained.cpu.status, "stale");
    assert.equal(retained.cpu.sampled_at, sampled_at);
    assert.equal(retained.memory.usage, 12);
    assert.equal(retained.storage.usage, undefined);
    const stopped = { ...outage, status: "stopped" };
    assert.equal(retainResourceSamples(previous, stopped), stopped);
    assert.equal(previous.cpu.status, "ok", "retention must not mutate earlier observations");
});

test("explicitly absent persistent storage is distinct from unavailable telemetry", () => {
    const absent = { status: "unavailable", usage: null, capacity: null, requested: null, coverage: { observed: 0, expected: 0 } };
    const row = resourceRow("storage", absent, { now });
    assert.equal(row.status, "not_applicable");
    assert.equal(row.note, "No persistent storage configured");
    assert.equal(row.usage, "—");
    assert.equal(row.percent, null);
    assert.equal(resourceRow("storage", { ...absent, coverage: { expected: null } }, { now }).status, "unavailable");
    assert.equal(resourceRow("storage", { ...absent, capacity: 50 }, { now }).status, "unavailable");
    const current = { status: "ok", storage: absent };
    const retained = retainResourceSamples({ storage: { usage: 10, status: "ok" } }, current);
    assert.equal(retained.storage, absent, "confirmed removal must not retain old disk usage");
});

function pollingFixture(load) {
    const listeners = new Set();
    const timers = new Map();
    let nextId = 0;
    const data = [];
    let errors = 0;
    const documentRef = {
        visibilityState: "visible",
        addEventListener(name, fn) { assert.equal(name, "visibilitychange"); listeners.add(fn); },
        removeEventListener(name, fn) { listeners.delete(fn); },
    };
    const poller = createResourcePoller({ load, onData: (value) => data.push(value), onError: () => errors++, documentRef,
        schedule(fn, delay) { const id = ++nextId; timers.set(id, { fn, delay }); return id; },
        cancel(id) { timers.delete(id); },
    });
    return { poller, timers, data, listeners, get errors() { return errors; },
        visibility(value) { documentRef.visibilityState = value; for (const fn of listeners) fn(); },
        async tick() { const [id, timer] = timers.entries().next().value; timers.delete(id); await timer.fn(); },
    };
}
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("poller batches once, never overlaps, and refreshes immediately after visibility returns", async () => {
    let finish;
    let calls = 0;
    const fixture = pollingFixture(() => { calls++; return new Promise((resolve) => { finish = resolve; }); });
    fixture.poller.start();
    fixture.poller.start();
    fixture.poller.refresh();
    assert.equal(calls, 1);
    fixture.visibility("hidden");
    finish("hidden result");
    await settle();
    assert.deepEqual(fixture.data, []);
    assert.equal(fixture.timers.size, 0);
    fixture.visibility("visible");
    assert.equal(calls, 2);
    finish("visible result");
    await settle();
    assert.deepEqual(fixture.data, ["visible result"]);
    // A requested refresh during the first round may schedule a single catch-up.
    assert.equal(fixture.timers.size, 1);
    fixture.poller.stop();
    assert.equal(fixture.listeners.size, 0);
    assert.equal(fixture.timers.size, 0);
});

test("normal polling uses 30 seconds and a rejected request does not stop future updates", async () => {
    let calls = 0;
    const fixture = pollingFixture(async () => { if (++calls === 1) throw new Error("private backend error"); return "recovered"; });
    fixture.poller.start();
    await settle();
    assert.equal(fixture.errors, 1);
    assert.equal([...fixture.timers.values()][0].delay, 30000);
    await fixture.tick();
    assert.deepEqual(fixture.data, ["recovered"]);
    fixture.poller.stop();
});

test("unmounted pages ignore in-flight success and never schedule another request", async () => {
    let finish;
    const fixture = pollingFixture(() => new Promise((resolve) => { finish = resolve; }));
    fixture.poller.start();
    fixture.poller.stop();
    finish("late result");
    await settle();
    assert.deepEqual(fixture.data, []);
    assert.equal(fixture.timers.size, 0);
    assert.equal(fixture.listeners.size, 0);
});

const hookSource = (await source("services/service_resources.js")).replace(/^import .*;\s*$/gm, "").replace(/^export /gm, "");
function hookFixture(serviceIds, rpc) {
    const mounted = [], unmounted = [], timers = new Map();
    const documentRef = { visibilityState: "visible", addEventListener() {}, removeEventListener() {} };
    let nextTimer = 0;
    const useServiceResources = new Function("useState", "onMounted", "onWillUnmount", "jsonRpc", "createResourcePoller", "retainResourceSamples", "document", `${hookSource};return useServiceResources;`)(
        (state) => state, (fn) => mounted.push(fn), (fn) => unmounted.push(fn), rpc,
        (options) => createResourcePoller({ ...options, documentRef,
            schedule(fn, delay) { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; }, cancel(id) { timers.delete(id); },
        }), retainResourceSamples, documentRef,
    );
    const resources = useServiceResources(77, () => serviceIds);
    return { resources, start() { mounted.forEach((fn) => fn()); }, stop() { unmounted.forEach((fn) => fn()); }, timers };
}

test("workspace hook batches 205 services in bounded requests and keeps observations keyed to service IDs", async () => {
    const calls = [];
    const fixture = hookFixture(Array.from({ length: 205 }, (_, i) => i + 1), async (route, params) => {
        calls.push({ route, params });
        return { success: true, data: { services: Object.fromEntries(params.service_ids.map((id) => [String(id), { status: "ok", cpu: metric({ usage: id }) }])) } };
    });
    fixture.start();
    await settle();
    assert.equal(calls.length, 3, "not one request per service");
    assert.deepEqual(calls.map((call) => call.params.service_ids.length), [100, 100, 5]);
    assert.ok(calls.every((call) => call.route === "/api/workspaces/77/service-resource-observations"));
    assert.equal(Object.keys(fixture.resources.state.observations).length, 205);
    assert.equal(fixture.resources.state.observations[205].cpu.usage, 205);
    assert.equal(fixture.resources.state.loading, false);
    fixture.stop();
});

test("an unmounted workspace does not issue the next batch or publish late results", async () => {
    let finish, calls = 0;
    const fixture = hookFixture(Array.from({ length: 101 }, (_, i) => i + 1), () => {
        calls++;
        return new Promise((resolve) => { finish = resolve; });
    });
    fixture.start();
    fixture.stop();
    finish({ success: true, data: { services: {} } });
    await settle();
    assert.equal(calls, 1);
    assert.deepEqual(fixture.resources.state.observations, {});
    assert.equal(fixture.timers.size, 0);
});
