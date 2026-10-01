// Local control-flow tests only; the HttpCase tests exercise real Odoo/Owl.
// Run: node --test tests/frontend/test_portal_paas_boot_20261001.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../src/static/src/paas/boot.js", import.meta.url), "utf8");
const { bootPaaS } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const appSource = await readFile(new URL("../../src/static/src/paas/app.js", import.meta.url), "utf8");
const browserTests = await readFile(new URL("../../src/tests/test_portal_paas_bootstrap_20261001.py", import.meta.url), "utf8");

test("Odoo browser readiness predicates return booleans, not DOM objects", () => {
  const expressions = [...browserTests.matchAll(/ready="([^"]+)"/g)].map(match => match[1]);
  assert.equal(expressions.length, 2, "guard both real browser readiness sites");
  for (const expression of expressions) {
    for (const found of [null, {}]) {
      const value = vm.runInNewContext(expression, { document: { querySelector: () => found } });
      assert.equal(typeof value, "boolean", expression);
      assert.equal(value, found !== null, expression);
    }
  }
});

function fixture() {
  const message = { textContent: "Loading your workspace…" };
  const failure = { textContent: "We couldn't start Woow PaaS. Please try again." };
  const target = {
    dataset: { bootState: "loading" },
    children: [],
    replaceChildren() { this.children = []; },
  };
  const fallback = {
    removed: false,
    attributes: { role: "status", "aria-busy": "true" },
    querySelector(selector) {
      assert.ok(["[data-boot-message]", "[data-boot-failure]"].includes(selector));
      return selector === "[data-boot-message]" ? message : failure;
    },
    setAttribute(name, value) { this.attributes[name] = value; },
    remove() { this.removed = true; },
  };
  // Any attempted HTML injection must fail, even if the error contains markup.
  Object.defineProperty(message, "innerHTML", { set() { assert.fail("Unsafe HTML"); } });
  return { target, fallback, message, failure };
}

test("loading panel remains until asynchronous mount resolves", async () => {
  const { target, fallback } = fixture();
  let finish;
  const pending = bootPaaS((actualTarget) => {
    assert.equal(actualTarget, target);
    return new Promise((resolve) => { finish = resolve; });
  }, target, fallback);
  assert.equal(target.dataset.bootState, "loading");
  assert.equal(fallback.removed, false);
  finish();
  assert.equal(await pending, true);
  assert.equal(target.dataset.bootState, "ready");
  assert.equal(fallback.removed, true);
});

for (const synchronous of [false, true]) {
  test(`${synchronous ? "sync throw" : "async rejection"} leaves safe retry panel, not partial UI`, async () => {
    const { target, fallback, message, failure } = fixture();
    const secret = new Error("<img src=x onerror=alert(1)> server SQL / private-token");
    const mount = () => {
      target.children.push("partial app");
      if (synchronous) throw secret;
      return Promise.reject(secret);
    };
    assert.equal(await bootPaaS(mount, target, fallback), false);
    assert.deepEqual(target.children, []);
    assert.equal(target.dataset.bootState, "failed");
    assert.equal(fallback.removed, false);
    assert.equal(fallback.attributes.role, "alert");
    assert.equal(fallback.attributes["aria-busy"], "false");
    assert.equal(message.textContent, failure.textContent);
    assert.ok(!message.textContent.includes("private-token"));
    assert.equal(await bootPaaS(async () => {}, target, fallback), true);
    assert.equal(fallback.removed, true, "successful mount must remove stale failure panel");
  });
}

test("app entrypoint returns the guarded mount promise with the real DOM targets", async () => {
  const { target, fallback } = fixture();
  class Root {}
  let ready;
  const context = {
    Root, bootPaaS,
    whenReady(callback) { ready = callback; },
    document: {
      getElementById(id) {
        assert.ok(["woow-paas-root", "woow-paas-boot"].includes(id));
        return id === "woow-paas-root" ? target : fallback;
      },
    },
    async mountComponent(component, actualTarget) {
      assert.equal(component, Root);
      assert.equal(actualTarget, target);
      throw new Error("startup rejected");
    },
  };
  vm.runInNewContext(appSource.replace(/^import .*;\s*$/gm, ""), context);
  assert.equal(await ready(), false);
  assert.equal(target.dataset.bootState, "failed");
  assert.equal(fallback.removed, false);
});
