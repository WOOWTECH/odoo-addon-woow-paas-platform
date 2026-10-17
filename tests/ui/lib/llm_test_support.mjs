// LLM-01c 前端 node 測試的共用工具（不在 tests/ui/*.mjs 的 glob 裡，所以本檔不會被當成測試跑）。
//
// - `stubImports`：把真實檔案的 import 換成 stub 後以 data: URL 載入（同 test_addons_tab_capability_1159）。
// - `loadPo`／`translator`：讀 src/i18n/zh_TW.po，做一個與 Odoo 前端相同規則的 `_t`——只採用
//   `#. odoo-javascript` 而且 msgstr 非空的條目（`CodeTranslations._load_web_translations`），
//   具名參數照 `%(name)s` 代換。測試因此驗得到「畫面上真的會出現的中文」。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

export const PAAS = new URL("../../../src/static/src/paas/", import.meta.url);
export const PO_URL = new URL("../../../src/i18n/zh_TW.po", import.meta.url);

export const toDataUrl = (source) =>
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

export const readPaas = (rel) => readFile(new URL(rel, PAAS), "utf8");

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");

/** 把 `import { a, b as c } from "spec";` 換成 stub；每個 import 都必須被換掉。 */
export function stubImports(source, replacements) {
  let out = source;
  for (const [spec, code] of Object.entries(replacements)) {
    const pattern = new RegExp(`import \\{[^}]*\\} from "${escapeRegExp(spec)}";`);
    assert.match(out, pattern, `找不到要替換的 import：${spec}`);
    out = out.replace(pattern, code);
  }
  assert.doesNotMatch(out, /^\s*import\s/m, "所有 import 都必須被換成 stub");
  return out;
}

/** PO 的 C 風格字串字面值（`"…"`）→ 內容。只處理 PO 會出現的跳脫：\n \t \" \\。 */
function unquote(literal) {
  const body = literal.trim().slice(1, -1);
  let out = "";
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (ch !== "\\") {
      out += ch;
      continue;
    }
    const next = body[++i];
    out += next === "n" ? "\n" : next === "t" ? "\t" : next;
  }
  return out;
}

/** 解析 .po：回傳 Map(msgid → {msgstr, comments[], references[]})。 */
export async function loadPo(url = PO_URL) {
  const text = await readFile(url, "utf8");
  const entries = new Map();
  for (const block of text.split(/\n\s*\n/)) {
    const comments = [];
    const references = [];
    let msgid = null;
    let msgstr = null;
    let current = null;
    for (const line of block.split("\n")) {
      if (line.startsWith("#.")) comments.push(line.slice(2).trim());
      else if (line.startsWith("#:")) references.push(line.slice(2).trim());
      else if (line.startsWith("msgid ")) { msgid = unquote(line.slice(6)); current = "id"; }
      else if (line.startsWith("msgstr ")) { msgstr = unquote(line.slice(7)); current = "str"; }
      else if (line.startsWith("\"")) {
        if (current === "id") msgid += unquote(line);
        else if (current === "str") msgstr += unquote(line);
      }
    }
    if (msgid) entries.set(msgid, { msgstr: msgstr || "", comments, references });
  }
  return entries;
}

/**
 * Odoo 前端 `_t` 的替身。`lang: "en"` 時不翻；`"zh_TW"` 時只用 odoo-javascript 條目
 * （與 runtime 相同的過濾條件：只有 python 註解的條目在前端**不會**被翻）。
 */
export function translator(po, lang = "zh_TW") {
  return (message, values) => {
    let text = message;
    if (lang === "zh_TW") {
      const entry = po.get(message);
      if (entry && entry.msgstr && entry.comments.includes("odoo-javascript")) {
        text = entry.msgstr;
      }
    }
    if (!values) return text;
    return text.replace(/%\((\w+)\)s/g, (_, key) => String(values[key]));
  };
}

/** stub 程式碼：`_t` 讀 globalThis 上的翻譯函式（各 test 可切換語系）。 */
export const translateFromGlobal = "const _t = (message, values) => globalThis.__llmT(message, values);";
