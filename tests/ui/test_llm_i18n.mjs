// LLM-01c：「AI 模型」分頁每一句 UI 文案都有繁中譯文，而且前端真的載得到。
//
// Odoo 前端只載 `#. odoo-javascript` 而且 msgstr 非空的條目（CodeTranslations._load_web_translations）；
// 只有 `#. odoo-python` 的條目（例如後端 controllers/llm.py 的 fallback 句）在 /woow **不會**被翻。
// 本檔逐句對帳：JS 的 `_t("…")` 字面值、template 的文字節點與可翻譯屬性。
// ⚠ 只驗「有譯文、前端載得到」，不驗 Odoo 抽取器的實際輸出（那要沙盒 --i18n-export）；
// 多行文字節點與 template literal 內的 `_t()` 由 repo-lint 第 2、34 條擋。
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import test from "node:test";
import { PAAS, loadPo, translator } from "./lib/llm_test_support.mjs";

const po = await loadPo();

async function filesUnder(rel, extension) {
  const out = [];
  async function walk(url) {
    for (const entry of await readdir(url, { withFileTypes: true })) {
      const child = new URL(entry.name + (entry.isDirectory() ? "/" : ""), url);
      if (entry.isDirectory()) await walk(child);
      else if (entry.name.endsWith(extension)) out.push(child);
    }
  }
  await walk(new URL(rel, PAAS));
  return out;
}

const JS_FILES = [
  new URL("services/llm_errors.js", PAAS),
  new URL("services/llm_service.js", PAAS),
  new URL("utils/llm_format.js", PAAS),
  ...(await filesUnder("components/llm/", ".js")),
  ...(await filesUnder("pages/llm/", ".js")),
];
const XML_FILES = [
  ...(await filesUnder("components/llm/", ".xml")),
  ...(await filesUnder("pages/llm/", ".xml")),
];

const LLM07_FILES = [...JS_FILES, ...XML_FILES].filter((url) =>
  /\/components\/llm\/LlmKey[^/]*\.(js|xml)$/.test(url.pathname)
  || /\/pages\/llm\/tabs\/LlmKeysTab\.(js|xml)$/.test(url.pathname));
/** 導覽（Sidebar／BottomNav／router）上新增的那一句。 */
const NAV_MSGIDS = ["AI Models"];

/** 譯文刻意改用另一個具名參數的條目（tokens：英文 K、繁中「萬」，同一次呼叫兩個參數都給）。 */
const ALTERNATE_PLACEHOLDERS = { "%(thousands)sK": ["tenThousands"] };

function unescapeJs(text) {
  return text.replace(/\\(["'\\])/g, "$1").replace(/\\n/g, "\n");
}

async function jsMsgids() {
  const ids = new Map();
  for (const url of JS_FILES) {
    const source = await readFile(url, "utf8");
    for (const match of source.matchAll(/\b_t\(\s*"((?:[^"\\]|\\.)*)"/g)) {
      ids.set(unescapeJs(match[1]), url.pathname);
    }
    assert.doesNotMatch(source, /\b_t\(\s*'/, `${url.pathname}：_t() 一律用雙引號字面值（本檔的抽取只認雙引號）`);
    assert.doesNotMatch(source, /\b_t\(\s*`/, `${url.pathname}：_t() 不收 template literal`);
  }
  return ids;
}

async function xmlMsgids() {
  const ids = new Map();
  for (const url of XML_FILES) {
    const source = (await readFile(url, "utf8")).replace(/<!--[\s\S]*?-->/g, "");
    for (const match of source.matchAll(/>([^<>]+)</g)) {
      const text = match[1].trim();
      if (!/[A-Za-z]/.test(text)) continue;
      assert.doesNotMatch(text, /\n/, `${url.pathname}：多行文字節點「${text}」——msgid 會連換行與縮排一起抽出`);
      ids.set(text, url.pathname);
    }
    for (const match of source.matchAll(/<([a-z][\w-]*)\b[^>]*?\s(aria-label|title|placeholder|alt)="([^"]+)"/g)) {
      ids.set(match[3], url.pathname);
    }
  }
  return ids;
}

const placeholders = (text) => new Set([...text.matchAll(/%\((\w+)\)s/g)].map((m) => m[1]));

test("抽取有抓到東西（掃描壞了 ≠ 全部有譯文）", async () => {
  const js = await jsMsgids();
  const xml = await xmlMsgids();
  assert.ok(js.size >= 60, `JS 只抓到 ${js.size} 句`);
  assert.ok(xml.size >= 15, `XML 只抓到 ${xml.size} 句`);
  assert.ok(xml.has("Activate AI Models"));
  assert.ok(js.has("Copy your API key now"));
});

test("每一句都有前端載得到的繁中譯文（odoo-javascript＋msgstr 非空）", async () => {
  const all = new Map([...(await jsMsgids()), ...(await xmlMsgids())]);
  for (const id of NAV_MSGIDS) all.set(id, "nav");
  const problems = [];
  for (const [msgid, where] of all) {
    const entry = po.get(msgid);
    if (!entry) problems.push(`缺條目：${JSON.stringify(msgid)}（${where}）`);
    else if (!entry.comments.includes("odoo-javascript")) problems.push(`只有 python 註解，前端不會翻：${JSON.stringify(msgid)}`);
    else if (!entry.msgstr) problems.push(`msgstr 是空的：${JSON.stringify(msgid)}`);
  }
  assert.deepEqual(problems, []);
});

test("LLM-07 前端 PO 條目有對應的 Odoo code occurrence", async () => {
  const sources = new Map();
  const addSource = (msgid, url) => {
    if (!sources.has(msgid)) sources.set(msgid, new Set());
    const marker = "/static/src/paas/";
    const index = url.pathname.indexOf(marker);
    assert.notEqual(index, -1, `${url.pathname} 不在 PaaS 前端 source 目錄`);
    sources.get(msgid).add(`code:addons/woow_paas_platform/static/src/paas/${url.pathname.slice(index + marker.length)}:`);
  };
  for (const url of LLM07_FILES) {
    const source = await readFile(url, "utf8");
    if (url.pathname.endsWith(".js")) {
      for (const match of source.matchAll(/\b_t\(\s*"((?:[^"\\]|\\.)*)"/g)) {
        addSource(unescapeJs(match[1]), url);
      }
    } else {
      const clean = source.replace(/<!--[\s\S]*?-->/g, "");
      for (const match of clean.matchAll(/>([^<>]+)</g)) {
        const text = match[1].trim();
        if (/[A-Za-z]/.test(text)) addSource(text, url);
      }
      for (const match of clean.matchAll(/<([a-z][\w-]*)\b[^>]*?\s(aria-label|title|placeholder|alt)="([^"]+)"/g)) {
        addSource(match[3], url);
      }
    }
  }

  const missing = [];
  for (const [msgid, expectedRefs] of sources) {
    const entry = po.get(msgid);
    if (!entry) continue; // The translation coverage test above reports missing entries.
    for (const expected of expectedRefs) {
      if (!entry.references.some((reference) => reference.startsWith(expected))) {
        missing.push(`${JSON.stringify(msgid)} 缺少 source occurrence ${expected}`);
      }
    }
  }
  assert.deepEqual(missing, []);
});

test("LLM-07 金鑰頁關鍵控件載入指定繁中譯文", () => {
  const t = translator(po, "zh_TW");
  const expected = {
    "API keys": "API 金鑰",
    "Manage paid API keys and review masked key details.": "管理付費 API 金鑰並查看遮蔽後的金鑰資訊。",
    "Create paid key": "建立付費金鑰",
    "API key type": "API 金鑰類型",
    "Used / total": "已使用／總額度",
    "Last use": "最近使用",
    Expiry: "到期時間",
    Disable: "停用",
  };
  for (const [msgid, translation] of Object.entries(expected)) {
    assert.equal(t(msgid), translation, `${msgid} 應載入指定繁中譯文`);
    assert.notEqual(t(msgid), msgid, `${msgid} 不可回退到英文原文`);
  }
});

test("譯文的具名參數都是原文有的（打錯字會在畫面上留下 %(xxx)s）", async () => {
  const all = new Map([...(await jsMsgids()), ...(await xmlMsgids())]);
  const problems = [];
  for (const msgid of all.keys()) {
    const entry = po.get(msgid);
    if (!entry || !entry.msgstr) continue;
    const allowed = placeholders(msgid);
    for (const name of ALTERNATE_PLACEHOLDERS[msgid] || []) allowed.add(name);
    for (const name of placeholders(entry.msgstr)) {
      if (!allowed.has(name)) problems.push(`${JSON.stringify(msgid)} 的譯文用了 %(${name})s`);
    }
  }
  assert.deepEqual(problems, []);
});

test("用詞：金鑰、額度、點（不出現「密钥」「密鑰」「積分」等）", async () => {
  const all = new Map([...(await jsMsgids()), ...(await xmlMsgids())]);
  for (const msgid of all.keys()) {
    const msgstr = (po.get(msgid) || {}).msgstr || "";
    // The API's literal 401 body is mandated by LLM-15; keep this external error text verbatim.
    if (msgid.startsWith("401 —") && msgstr.includes("無效的令牌")) {
      assert.doesNotMatch(msgstr, /密钥|密鑰|积分|積分|額度值/);
      continue;
    }
    assert.doesNotMatch(msgstr, /密钥|密鑰|积分|積分|額度值|令牌/, `${msgid} → ${msgstr}`);
  }
});
