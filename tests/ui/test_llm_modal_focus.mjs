// LLM-01c：一次性金鑰對話框的鍵盤／讀屏守線（utils/modal_focus.js）與複製退路的清理
// （utils/clipboard.js）。用最小的假 DOM，不起瀏覽器；實際畫面仍要在沙盒用瀏覽器驗。
//
// - Tab／Shift+Tab 只在對話框內循環（backdrop 只擋滑鼠，鍵盤會 Tab 到背景的側欄連結）。
// - 打開時背景設 inert、關閉時只還原自己設的那些。
// - 複製退路的隱藏 textarea 帶著完整金鑰：select()／execCommand() 丟例外也一定清空並移除，
//   焦點回到原本的元素。
import assert from "node:assert/strict";
import test from "node:test";
import { readPaas, toDataUrl } from "./lib/llm_test_support.mjs";

const doc = { activeElement: null, body: null };

class FakeElement {
  constructor(name, { focusable = false, rendered = true } = {}) {
    this.name = name;
    this.children = [];
    this.parentElement = null;
    this.attributes = new Map();
    this.focusable = focusable;
    this.rendered = rendered;
    this.hidden = false;
    this.ownerDocument = doc;
  }

  append(...kids) {
    for (const kid of kids) {
      kid.parentElement = this;
      this.children.push(kid);
    }
    return this;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  getClientRects() {
    return this.rendered ? [{}] : [];
  }

  contains(other) {
    for (let node = other; node; node = node.parentElement) {
      if (node === this) return true;
    }
    return false;
  }

  focus() {
    doc.activeElement = this;
  }

  /** 選擇器不解析：回傳所有標成 focusable 的子孫（DOM 順序）。 */
  querySelectorAll() {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (child.focusable) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

const focus = await import(toDataUrl(await readPaas("utils/modal_focus.js")));

function tabEvent(shiftKey = false) {
  return {
    key: "Tab",
    shiftKey,
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };
}

function dialogFixture() {
  const dialog = new FakeElement("dialog");
  const first = new FakeElement("copy", { focusable: true });
  const hidden = new FakeElement("hidden", { focusable: true, rendered: false });
  const middle = new FakeElement("checkbox", { focusable: true });
  const last = new FakeElement("done", { focusable: true });
  dialog.append(first, hidden, middle, last);
  return { dialog, first, middle, last, hidden };
}

test("focusableWithin：只算有畫出來、沒 hidden 的元素", () => {
  const { dialog, first, middle, last, hidden } = dialogFixture();
  assert.deepEqual(focus.focusableWithin(dialog), [first, middle, last]);
  middle.hidden = true;
  assert.deepEqual(focus.focusableWithin(dialog), [first, last]);
  assert.ok(!focus.focusableWithin(dialog).includes(hidden));
  assert.deepEqual(focus.focusableWithin(null), []);
});

test("trapTab：最後一個再 Tab 回到第一個、第一個 Shift+Tab 到最後一個；焦點在對話框外時拉回來", () => {
  const { dialog, first, middle, last } = dialogFixture();
  const outside = new FakeElement("sidebar-link", { focusable: true });

  last.focus();
  let ev = tabEvent();
  focus.trapTab(ev, dialog);
  assert.equal(ev.prevented, true);
  assert.equal(doc.activeElement, first);

  first.focus();
  ev = tabEvent(true);
  focus.trapTab(ev, dialog);
  assert.equal(ev.prevented, true);
  assert.equal(doc.activeElement, last);

  // 中間的元素：交給瀏覽器自己走（不攔）。
  middle.focus();
  ev = tabEvent();
  focus.trapTab(ev, dialog);
  assert.equal(ev.prevented, false);

  // 焦點在對話框本身（剛打開）或背景：Tab 進第一個、Shift+Tab 進最後一個。
  dialog.focus();
  ev = tabEvent();
  focus.trapTab(ev, dialog);
  assert.equal(doc.activeElement, first);
  outside.focus();
  ev = tabEvent(true);
  focus.trapTab(ev, dialog);
  assert.equal(doc.activeElement, last);

  // 沒有可以 Tab 的元素：焦點留在對話框上。
  const empty = new FakeElement("empty-dialog");
  outside.focus();
  ev = tabEvent();
  focus.trapTab(ev, empty);
  assert.equal(ev.prevented, true);
  assert.equal(doc.activeElement, empty);

  // 不是 Tab：不管。
  ev = { key: "Enter", preventDefault() { throw new Error("不該攔"); } };
  focus.trapTab(ev, dialog);
});

test("inertOutside：對話框所有祖先的兄弟節點設 inert；還原只動自己設的那些（冪等）", () => {
  const body = new FakeElement("body");
  doc.body = body;
  const app = new FakeElement("app");
  const toast = new FakeElement("toast");
  const alreadyInert = new FakeElement("already-inert");
  alreadyInert.setAttribute("inert", "");
  body.append(app, toast, alreadyInert);
  const sidebar = new FakeElement("sidebar");
  const main = new FakeElement("main");
  app.append(sidebar, main);
  const cards = new FakeElement("cards");
  const backdrop = new FakeElement("backdrop");
  main.append(cards, backdrop);
  const dialog = new FakeElement("dialog");
  backdrop.append(dialog);

  const restore = focus.inertOutside(backdrop);
  for (const el of [cards, sidebar, toast, alreadyInert]) {
    assert.equal(el.hasAttribute("inert"), true, el.name);
  }
  for (const el of [backdrop, dialog, main, app, body]) {
    assert.equal(el.hasAttribute("inert"), false, el.name);
  }
  restore();
  for (const el of [cards, sidebar, toast]) {
    assert.equal(el.hasAttribute("inert"), false, el.name);
  }
  assert.equal(alreadyInert.hasAttribute("inert"), true, "本來就 inert 的不還原");
  restore();
  assert.equal(alreadyInert.hasAttribute("inert"), true);
});

// ----------------------------------------------------------------------------- clipboard.js

const clipboard = await import(toDataUrl(await readPaas("utils/clipboard.js")));

/** 舊式退路的假 DOM：沒有 Clipboard API、不是安全來源，execCommand 的行為由各 test 指定。 */
function legacyDom(execCommand) {
  const appended = [];
  const trigger = { focus() { document.activeElement = trigger; } };
  globalThis.window = { isSecureContext: false };
  globalThis.document = {
    activeElement: trigger,
    body: {
      appendChild(el) {
        appended.push(el);
        el.parentNode = this;
      },
    },
    createElement() {
      const area = {
        value: "",
        style: {},
        parentNode: null,
        setAttribute() {},
        select() {
          document.activeElement = area;
        },
        remove() {
          const index = appended.indexOf(area);
          if (index >= 0) appended.splice(index, 1);
          area.parentNode = null;
        },
      };
      return area;
    },
    execCommand,
  };
  return { appended, trigger };
}

test("複製退路：execCommand 丟例外時回 false，帶明文的 textarea 一定清空並移除、焦點還回去", async () => {
  const secret = "sk-" + "Q7".repeat(24);
  let seen = null;
  const { appended, trigger } = legacyDom(() => {
    seen = document.activeElement;
    throw new Error("execCommand blocked");
  });
  assert.equal(await clipboard.copyText(secret), false);
  assert.equal(appended.length, 0, "body 不能留下 textarea");
  assert.equal(seen.value, "", "textarea 的值清空");
  assert.equal(document.activeElement, trigger, "焦點回到原本的元素（不會留在對話框外）");
});

test("複製退路：成功時回 true，一樣清空、移除、還焦點；沒有文字直接回 false", async () => {
  const { appended, trigger } = legacyDom(() => true);
  assert.equal(await clipboard.copyText("https://newapi.woowtech.io/v1"), true);
  assert.equal(appended.length, 0);
  assert.equal(document.activeElement, trigger);
  assert.equal(await clipboard.copyText(""), false);

  legacyDom(() => false);
  assert.equal(await clipboard.copyText("x"), false, "execCommand 回 false 就是失敗");
});
