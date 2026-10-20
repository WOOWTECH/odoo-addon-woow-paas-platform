// #914／X-LC-006：在「目前這個 process 的 TZ」下，把後端日期字串交給真的
// `utils/datetime_helpers.js`（＋真的 luxon）解析，印出 JSON 給 test_datetime_tz_914.mjs 比對。
//
// 不在 tests/ui/*.mjs 的 glob 裡，本檔不會被當成測試跑；由測試以 `TZ=<zone>` 開子行程呼叫，
// 因為 JS 引擎的本地時區要在 process 起來時就定好，同一個 process 內切 TZ 不可靠。
//
// luxon 來源：環境變數 LUXON_PATH（指向 luxon 套件根目錄），否則 `import("luxon")`。
// 版本要對齊 Odoo 18 內附的 luxon（web/static/lib/luxon/luxon.js，3.4.4）。
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import path from "node:path";

const require = createRequire(import.meta.url);

function loadLuxon() {
  if (process.env.LUXON_PATH) {
    return require(path.join(process.env.LUXON_PATH, "build", "node", "luxon.js"));
  }
  return require("luxon");
}

const luxon = loadLuxon();
globalThis.luxon = luxon;

const PAAS = new URL("../../../src/static/src/paas/", import.meta.url);
let source = await readFile(new URL("utils/datetime_helpers.js", PAAS), "utf8");

// Odoo 的格式化函式換成固定格式（只驗「解析成哪個瞬間／哪一天」，不驗語系格式）。
const stubs = {
  "@web/core/l10n/dates":
    "const odooFormatDate = (dt) => dt.toFormat('yyyy-MM-dd');\n" +
    "const odooFormatDateTime = (dt) => dt.toFormat('yyyy-MM-dd HH:mm');",
  "@web/core/l10n/localization": "const localization = { shortTimeFormat: 'HH:mm' };",
};
for (const [spec, code] of Object.entries(stubs)) {
  const pattern = new RegExp(`import \\{[^}]*\\} from "${spec.replace(/[/.@]/g, "\\$&")}";`);
  if (!pattern.test(source)) {
    throw new Error(`datetime_helpers.js 找不到 import：${spec}`);
  }
  source = source.replace(pattern, code);
}
const helpers = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

const inputs = JSON.parse(process.argv[2]);
const out = {
  tz: process.env.TZ,
  luxonVersion: luxon.VERSION,
  reading: helpers.BACKEND_NAIVE_DATETIME_READING,
  results: {},
};
for (const value of inputs) {
  const dt = helpers.parseBackendDate(value);
  out.results[value] = {
    iso: dt ? dt.toUTC().toISO() : null,
    date: helpers.formatUserDate(value, null),
    datetime: helpers.formatUserDateTime(value, {}, null),
    daysFromToday: helpers.calendarDaysFromToday(value, { today: process.env.PROBE_TODAY }),
  };
}
process.stdout.write(JSON.stringify(out));
