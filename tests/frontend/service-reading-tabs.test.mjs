// Run: node --test tests/frontend/service-reading-tabs.test.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = (path) => readFile(new URL(`../../src/static/src/paas/${path}`, import.meta.url), "utf8");
const importSource = (code) => import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
const { createLogPoller, filterLogLines, downloadLogSnapshot, logDownloadFilename } =
  await importSource(await source("pages/service/tabs/service_logs.js"));
const settle = () => new Promise((resolve) => setImmediate(resolve));

function fixture(load) {
  const listeners = new Set();
  const timers = new Map();
  const data = [];
  let errors = 0;
  let nextId = 0;
  const documentRef = {
    visibilityState: "visible",
    addEventListener(name, fn) { assert.equal(name, "visibilitychange"); listeners.add(fn); },
    removeEventListener(name, fn) { listeners.delete(fn); },
  };
  const poller = createLogPoller({ load, onData: (value) => data.push(value), onError: () => errors++, documentRef,
    schedule(fn, delay) { const id = ++nextId; timers.set(id, { fn, delay }); return id; },
    cancel(id) { timers.delete(id); },
  });
  return { poller, timers, data, listeners, get errors() { return errors; },
    visibility(value) { documentRef.visibilityState = value; for (const fn of listeners) fn(); },
    async tick() { const [id, timer] = timers.entries().next().value; timers.delete(id); await timer.fn(); },
  };
}

test("log polling is sequential and source changes discard the previous response", async () => {
  let finish;
  let calls = 0;
  const f = fixture(() => { calls++; return new Promise((resolve) => { finish = resolve; }); });
  f.poller.start();
  f.poller.refresh();
  f.poller.refresh();
  assert.equal(calls, 1);
  finish("old source");
  await settle();
  assert.deepEqual(f.data, []);
  assert.equal(f.timers.size, 1);
  const next = f.tick();
  assert.equal(calls, 2);
  finish("new source");
  await next;
  assert.deepEqual(f.data, ["new source"]);
  assert.equal([...f.timers.values()][0].delay, 5000);
  f.poller.stop();
});

test("hiding the document invalidates pending output and visible resumes once", async () => {
  let finish;
  let calls = 0;
  const f = fixture(() => { calls++; return new Promise((resolve) => { finish = resolve; }); });
  f.poller.start();
  f.visibility("hidden");
  finish("hidden output");
  await settle();
  assert.deepEqual(f.data, []);
  assert.equal(f.timers.size, 0);
  f.visibility("visible");
  assert.equal(calls, 2);
  finish("visible output");
  await settle();
  assert.deepEqual(f.data, ["visible output"]);
  f.poller.stop();
});

test("pause stops updates but still allows an explicit snapshot refresh", async () => {
  let calls = 0;
  const f = fixture(async () => ++calls);
  f.poller.start();
  await settle();
  f.poller.setPaused(true);
  assert.equal(f.timers.size, 0);
  f.visibility("hidden");
  f.visibility("visible");
  assert.equal(calls, 1);
  f.poller.refresh();
  await settle();
  assert.deepEqual(f.data, [1, 2]);
  assert.equal(f.timers.size, 0);
  f.poller.setPaused(false);
  await settle();
  assert.equal(calls, 3);
  assert.equal([...f.timers.values()][0].delay, 5000);
  f.poller.stop();
});

test("unmount discards late success and errors and removes timers/listeners", async () => {
  for (const fail of [false, true]) {
    let finish;
    const f = fixture(() => new Promise((resolve, reject) => { finish = fail ? reject : resolve; }));
    f.poller.start();
    f.poller.stop();
    finish("late response");
    await settle();
    assert.deepEqual(f.data, []);
    assert.equal(f.errors, 0);
    assert.equal(f.timers.size, 0);
    assert.equal(f.listeners.size, 0);
  }
});

test("a failed request continues polling and can recover", async () => {
  let calls = 0;
  const f = fixture(async () => { if (++calls === 1) throw new Error("private error"); return "recovered"; });
  f.poller.start();
  await settle();
  assert.equal(f.errors, 1);
  assert.equal([...f.timers.values()][0].delay, 5000);
  await f.tick();
  assert.deepEqual(f.data, ["recovered"]);
  f.poller.stop();
});

test("search is literal, case-insensitive, and does not transform HTML-like logs", () => {
  const text = 'INFO started\nERROR <img src=x onerror=alert(1)>\nerror [.*] literal\n中文日誌';
  assert.equal(filterLogLines(text, "  ERROR  "), 'ERROR <img src=x onerror=alert(1)>\nerror [.*] literal');
  assert.equal(filterLogLines(text, "[.*]"), "error [.*] literal");
  assert.equal(filterLogLines(text, "中文"), "中文日誌");
  assert.equal(filterLogLines(text, "   "), text);
  assert.equal(filterLogLines(text, "absent"), "");
});

test("download contains the bounded raw snapshot and revokes its object URL", async () => {
  let blob;
  let revoked;
  let cleanup;
  let clicked = false;
  let removed = false;
  const link = { click() { clicked = true; }, remove() { removed = true; } };
  const filename = logDownloadFilename(42, "pod/../../x", "app", new Date("2026-10-18T12:34:56Z"));
  assert.match(filename, /^service-42-pod_\.\._\.\._x-app-2026-10-18T12-34-56-000Z\.log$/);
  downloadLogSnapshot("INFO hidden by search\nERROR shown\n", filename, {
    documentRef: { createElement(tag) { assert.equal(tag, "a"); return link; }, body: { appendChild(value) { assert.equal(value, link); } } },
    urlRef: { createObjectURL(value) { blob = value; return "blob:snapshot"; }, revokeObjectURL(value) { revoked = value; } },
    schedule(fn) { cleanup = fn; },
  });
  assert.equal(await blob.text(), "INFO hidden by search\nERROR shown\n");
  assert.equal(link.download, filename);
  assert.equal(clicked && removed, true);
  cleanup();
  assert.equal(revoked, "blob:snapshot");
});

const componentSource = (await source("pages/service/tabs/LogsTab.js"))
  .replace(/^import .*;\s*$/gm, "").replace(/^export /gm, "");
function componentFixture(canManage = true) {
  const mounted = [], destroyed = [];
  let started = 0, stopped = 0;
  const LogsTab = new Function("Component", "useState", "useRef", "onMounted", "onPatched", "onWillDestroy",
    "_t", "WoowIcon", "createLogPoller", "filterLogLines", `${componentSource};return LogsTab;`)(
    class {}, (value) => value, () => ({ el: null }), (fn) => mounted.push(fn), () => {}, (fn) => destroyed.push(fn),
    (text) => text, class {}, () => ({ start() { started++; }, stop() { stopped++; }, setPaused() {} }), filterLogLines,
  );
  const component = new LogsTab();
  component.props = { canManage, workspaceId: 9, service: { id: 12 } };
  component.setup();
  return { component, mount() { mounted.forEach((fn) => fn()); }, destroy() { destroyed.forEach((fn) => fn()); },
    get started() { return started; }, get stopped() { return stopped; } };
}

test("read-only members never start log retrieval", () => {
  const f = componentFixture(false);
  f.mount();
  assert.equal(f.started, 0);
  f.destroy();
  assert.equal(f.stopped, 1);
});

test("pausing the first pending request exits loading with an actionable paused state", () => {
  const { component } = componentFixture();
  assert.equal(component.state.loading, true);
  component.togglePause();
  assert.equal(component.state.loading, false);
  assert.equal(component.state.paused, true);
  assert.equal(component.emptyMessage, "Auto-refresh is paused. Resume or refresh to load logs.");
});

test("rollout invalidates old selections and does not force a history reader to the end", () => {
  const { component } = componentFixture();
  component.state.atBottom = false;
  component.acceptLogs({ pod: "old", container: "app", reason: "source_not_found", sources: [{ pod: "new", containers: ["app"] }] });
  assert.equal(component.state.pod, "");
  assert.equal(component.state.container, "");
  assert.equal(component._scrollAfterPatch, false);
  component.state.atBottom = true;
  component.acceptLogs({ pod: "new", container: "app", available: true, text: "new log" });
  assert.equal(component._scrollAfterPatch, true);
  assert.equal(component.filteredText, "new log");
});

test("information and logs deep links resolve through the actual service route pattern", async () => {
  const routerSource = await source("core/router.js");
  const route = routerSource.split("\n").find((line) => line.includes('path: "workspace/:id/services/:serviceId/:tab"'));
  const patternLiteral = route.match(/pattern: (\/.*\/)\s*}/)[1];
  const pattern = new Function(`return ${patternLiteral}`)();
  for (const tab of ["overview", "configuration", "information", "logs", "addons", "mcp-servers", "access"]) {
    assert.equal(pattern.exec(`workspace/9/services/12/${tab}`)?.[3], tab);
  }
  assert.equal(pattern.test("workspace/9/services/12/arbitrary"), false);
});
