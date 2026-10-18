// #914／#916／X-LC-006-1、X-LC-006-2：後端日期字串在任何瀏覽器時區都要顯示成正確的當地時間。
//
// 後端（Odoo ORM）送來的 datetime 是**不帶 offset 的 UTC**（`fields.Datetime.isoformat()`），
// 只有日期的欄位（`fields.Date`）是**日曆日**。修補前前端用 `new Date()` 的規則讀：
// 帶 T 的當成「當地時間」（台北差 8 小時，交易 84 的 09:13 UTC 顯示成 09:13），只有日期的
// 當成「UTC 午夜」（UTC 以西少一天，發票 2026-10-07 在洛杉磯顯示 10-06）。
//
// 本檔以子行程在五個時區各跑一次真的 `datetime_helpers.js`＋luxon 3.4.4（Odoo 18 內附版本）：
// Asia/Taipei（+8）、UTC、America/Los_Angeles（-7，夏令）、Pacific/Pago_Pago（-11）、
// Pacific/Kiritimati（+14）。⛔ 只驗台北會漏掉正負號錯誤（#914 驗收第 3 點）。
//
// luxon：CI 由 paas-ui-tests workflow 安裝到暫存目錄並設 LUXON_PATH；本機沒有時，
// 設了 WOOW_REQUIRE_LUXON=1 就失敗，否則略過（附安裝指令）。
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const PROBE = fileURLToPath(new URL("./lib/datetime_tz_probe.mjs", import.meta.url));
const require = createRequire(import.meta.url);

function luxonAvailable() {
  try {
    if (process.env.LUXON_PATH) {
      require(path.join(process.env.LUXON_PATH, "build", "node", "luxon.js"));
    } else {
      require("luxon");
    }
    return true;
  } catch {
    return false;
  }
}

const HAVE_LUXON = luxonAvailable();
if (!HAVE_LUXON && process.env.WOOW_REQUIRE_LUXON === "1") {
  throw new Error("找不到 luxon：CI 必須先 `npm install --prefix <dir> luxon@3.4.4` 並設 LUXON_PATH=<dir>/node_modules/luxon");
}
const skip = HAVE_LUXON ? false : "本機沒有 luxon（npm install --prefix /tmp/lx luxon@3.4.4；LUXON_PATH=/tmp/lx/node_modules/luxon）";

// 交易 84（X-LC-006-1 原始證據）：DB 存 2026-10-16 09:13:23 UTC。
const TXN_84 = "2026-10-16T09:13:23";
// 發票 INV/2026/00317 invoice_date（X-LC-006-2）與試用結束日。
const INVOICE_DATE = "2026-10-07";
const TRIAL_END = "2026-10-20";
// 已帶 offset 的值（helm history 的 updated、後端 _resource_timestamp 的 observed_at）不得被重複位移。
const WITH_OFFSET = "2026-10-16T11:28:57+00:00";
const WITH_Z = "2026-10-16T11:28:57.000Z";
const WITH_PLUS8 = "2026-08-26T10:44:22.962052+08:00";
// SQL 空格格式（Odoo json_default；#916 線 B 的形狀）同樣是 UTC。
const SQL_SPACE = "2026-10-16 09:13:23";

const INPUTS = [TXN_84, INVOICE_DATE, TRIAL_END, WITH_OFFSET, WITH_Z, WITH_PLUS8, SQL_SPACE, "garbage", ""];

const ZONES = {
  "Asia/Taipei": { txnLocal: "2026-10-16 17:13", obsLocal: "2026-10-16 19:28" },
  UTC: { txnLocal: "2026-10-16 09:13", obsLocal: "2026-10-16 11:28" },
  "America/Los_Angeles": { txnLocal: "2026-10-16 02:13", obsLocal: "2026-10-16 04:28" },
  "Pacific/Pago_Pago": { txnLocal: "2026-10-15 22:13", obsLocal: "2026-10-16 00:28" },
  "Pacific/Kiritimati": { txnLocal: "2026-10-16 23:13", obsLocal: "2026-10-17 01:28" },
};

function probe(tz, today = "2026-10-16") {
  const stdout = execFileSync(process.execPath, [PROBE, JSON.stringify(INPUTS)], {
    env: { ...process.env, TZ: tz, PROBE_TODAY: today },
    encoding: "utf8",
  });
  return JSON.parse(stdout);
}

for (const [tz, expected] of Object.entries(ZONES)) {
  test(`${tz}：naive UTC 轉成當地時間、日期欄位不位移、帶 offset 的不重複位移`, { skip }, () => {
    const out = probe(tz);
    assert.equal(out.tz, tz);
    assert.equal(out.luxonVersion, "3.4.4", "luxon 版本要對齊 Odoo 18 內附版本");
    assert.equal(out.reading, "utc-wire-format");
    const r = out.results;

    // X-LC-006-1：交易紀錄／近期動態。同一瞬間在每個時區都對到 09:13:23Z。
    assert.equal(r[TXN_84].iso, "2026-10-16T09:13:23.000Z");
    assert.equal(r[TXN_84].datetime, expected.txnLocal);
    assert.equal(r[SQL_SPACE].datetime, expected.txnLocal, "SQL 空格格式同樣是 UTC");

    // X-LC-006-2：只有日期的欄位是日曆日，任何時區都不能少一天或多一天。
    assert.equal(r[INVOICE_DATE].date, "2026-10-07");
    assert.equal(r[TRIAL_END].date, "2026-10-20");

    // 已帶 offset 的值（服務詳情「最後同步於」那條路徑）結果與修補前相同。
    assert.equal(r[WITH_OFFSET].datetime, expected.obsLocal);
    assert.equal(r[WITH_Z].datetime, expected.obsLocal);
    assert.equal(r[WITH_PLUS8].iso, "2026-08-26T02:44:22.962Z");

    // 解析不了的值走呼叫端 fallback，絕不顯示 "Invalid DateTime"。
    assert.equal(r.garbage.datetime, null);
    assert.equal(r[""].date, null);
  });
}

test("calendarDaysFromToday：以當地日曆日計算（發票到期、任務截止），UTC 以西不提早一天逾期", { skip }, () => {
  for (const tz of Object.keys(ZONES)) {
    const out = probe(tz, "2026-10-07");
    assert.equal(out.results[INVOICE_DATE].daysFromToday, 0, `${tz}：到期日當天是 0（今天到期）`);
    assert.equal(out.results[TRIAL_END].daysFromToday, 13, tz);
    assert.equal(out.results.garbage.daysFromToday, null, tz);
    const later = probe(tz, "2026-10-08");
    assert.equal(later.results[INVOICE_DATE].daysFromToday, -1, `${tz}：隔天才逾期`);
  }
});
