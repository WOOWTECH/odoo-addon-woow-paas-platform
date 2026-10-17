// #1647：服務顯示 running、但平台確認它沒有在執行（workload 期望有 Pod、實際一顆都沒有）時，
// 詳情頁 banner 與服務卡片的固定文案，以及其他原本只看 state 的「正常」訊號要讓路——詳情頁
// Connection Info 的連線狀態、工作區頁統計卡的 Healthy／Alerts（瀏覽器實走 F1）。
// 純邏輯測試——載入手法同 test_service_presentation_1159.mjs（把 `_t` 換成恆等函式後以
// data: URL import）。元件的 getter 沒辦法在 node 裡 import（依賴 @odoo/owl），所以把 getter
// 的本體從原始碼切出來、餵真的 helper 執行。
//
// 跑法：node --test tests/ui/*.mjs（沒有 CI 會跑；元件有沒有接上 helper 另由 Odoo 端的
// TestWorkloadFaultFrontendSignals1647 靜態守）
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const helperUrl = new URL(
  "../../src/static/src/paas/utils/service_presentation.js",
  import.meta.url,
);
const source = await readFile(helperUrl, "utf8");
const testableSource = source.replace(
  'import { _t } from "@web/core/l10n/translation";',
  'const _t = (message, ...args) => message.replace("%s", args[0]);',
);
const presentation = await import(
  `data:text/javascript;base64,${Buffer.from(testableSource).toString("base64")}`,
);

/**
 * 從元件原始碼切出 `get <name>() { … }` 的本體（不含外層大括號）。
 * 大括號配對時跳過字串與註解；找不到就拋錯，測試不會靜靜地變成什麼都沒驗。
 */
function getterBody(componentSource, name) {
  const head = `get ${name}() {`;
  const start = componentSource.indexOf(head);
  assert.notEqual(start, -1, `找不到 getter ${name}`);
  let depth = 1;
  let index = start + head.length;
  while (index < componentSource.length) {
    const two = componentSource.slice(index, index + 2);
    const char = componentSource[index];
    if (two === "//") {
      index = componentSource.indexOf("\n", index);
    } else if (two === "/*") {
      index = componentSource.indexOf("*/", index) + 2;
    } else if (char === '"' || char === "'" || char === "`") {
      index += 1;
      while (componentSource[index] !== char) {
        index += componentSource[index] === "\\" ? 2 : 1;
      }
      index += 1;
    } else {
      if (char === "{") depth += 1;
      if (char === "}") depth -= 1;
      if (depth === 0) {
        return componentSource.slice(start + head.length, index);
      }
      index += 1;
    }
  }
  throw new Error(`getter ${name} 的大括號沒有配對`);
}

async function readComponent(relativePath) {
  return readFile(new URL(`../../src/static/src/paas/${relativePath}`, import.meta.url), "utf8");
}

const overviewTabSource = await readComponent("pages/service/tabs/OverviewTab.js");
const workspacePageSource = await readComponent("pages/workspace/WorkspaceDetailPage.js");

/** OverviewTab 的 connectionStatus getter，接上真的 hasConfirmedWorkloadFault。 */
const connectionStatusOf = (service) =>
  new Function(
    "hasConfirmedWorkloadFault",
    `return function () {${getterBody(overviewTabSource, "connectionStatus")}};`,
  )(presentation.hasConfirmedWorkloadFault).call({ props: { service } });

/** WorkspaceDetailPage 的 computedStats getter，接上真的 countServiceHealth；回 {Healthy, Alerts}。 */
const workspaceStatsOf = (services) => {
  const stats = new Function(
    "_t",
    "countServiceHealth",
    `return function () {${getterBody(workspacePageSource, "computedStats")}};`,
  )((message) => message, presentation.countServiceHealth).call({
    state: { services },
    estimatedMonthlyCost: "0",
  });
  return Object.fromEntries(stats.map((stat) => [stat.label, stat.value]));
};

const QUOTA_TEXT =
  "This service is not running right now: the workspace does not have enough CPU or memory quota left to start it. Please contact support. Our operations team has been notified.";
const GENERIC_TEXT =
  "This service is not running right now: the platform could not start it. Our operations team has been notified.";
const LABEL = "Not running — the platform could not start this service";

// 後端不會把這些放進 payload；放進測試物件是為了證明 helper 根本不讀它們。
const CLUSTER_NOISE = {
  error_message:
    'pods "svc-65b75f59-server-7d9c8b6f5-" is forbidden: exceeded quota: paas-ws-ab6564da-quota',
  workload_fault_detail: "Deployment svc-65b75f59-server: 0 of 1 pods exist (Recreate)",
};

test("running＋quota_exceeded 顯示配額文案", () => {
  assert.equal(
    presentation.getWorkloadFaultText({ state: "running", workload_fault: "quota_exceeded" }),
    QUOTA_TEXT,
  );
});

test("配額文案只請租戶聯絡客服，不建議刪除／停用／停止服務或改小方案來釋出配額", () => {
  // 工作區的 CPU／記憶體上限＝存活服務的方案規格加總 × 3（另有下限；cloud_service.py 的
  // _compute_ns_quota）：刪掉一台上限少 3 份、用量只少約 1 份，剩餘空間通常變小而不是變大。
  // 租戶做得到又有效的只有聯絡客服（管理員調整配額的正式入口在 #1648）。
  const text = presentation.getWorkloadFaultText({
    state: "running",
    workload_fault: "quota_exceeded",
  });
  assert.doesNotMatch(
    text,
    /\b(delet\w*|remov\w*|stop\w*|paus\w*|suspend\w*|disabl\w*|shut\w* down|turn\w* off|downgrad\w*|smaller|free\w* up)\b/i,
  );
  assert.match(text, /quota/);
  assert.match(text, /contact support/);
});

test("配額文案說的是「剩餘配額不足以啟動這台」，不說配額已滿", () => {
  // 被擋下的常見形狀是「還有剩，但不夠這台啟動」（事故：上限 12、其他 workload 佔 8、這台
  // 要 8）。寫成「已滿」的話，同一個租戶在工作區頁的配額面板看到的是「還剩 4 vCPU」。
  const text = presentation.getWorkloadFaultText({
    state: "running",
    workload_fault: "quota_exceeded",
  });
  assert.doesNotMatch(text, /\b(full|used up|exhausted|ran out|run out|no\b.*\bleft)\b/i);
  assert.match(text, /not have enough\b.*\bquota left to start/);
});

test("create_failed、no_pods 與不認得的代碼都顯示一般文案", () => {
  for (const code of ["create_failed", "no_pods", "some_future_code"]) {
    assert.equal(
      presentation.getWorkloadFaultText({ state: "running", workload_fault: code }),
      GENERIC_TEXT,
      code,
    );
  }
});

test("workload_fault 為 null、空字串、缺席或非字串時不顯示", () => {
  for (const fault of [null, undefined, "", true, 1, {}, ["quota_exceeded"]]) {
    const service = { state: "running", workload_fault: fault };
    assert.equal(presentation.getWorkloadFaultText(service), null, String(fault));
    assert.equal(presentation.getWorkloadFaultLabel(service), null, String(fault));
  }
  assert.equal(presentation.getWorkloadFaultText({ state: "running" }), null);
  assert.equal(presentation.getWorkloadFaultLabel({ state: "running" }), null);
});

test("state 不是 running 時不顯示（其他狀態各有自己的說明）", () => {
  for (const state of [
    "error",
    "upgrading",
    "deploying",
    "initializing",
    "suspended",
    "deleting",
    "pending",
    undefined,
  ]) {
    const service = { state, workload_fault: "quota_exceeded" };
    assert.equal(presentation.getWorkloadFaultText(service), null, String(state));
    assert.equal(presentation.getWorkloadFaultLabel(service), null, String(state));
  }
});

test("service 為 null／undefined 時不拋錯", () => {
  for (const service of [null, undefined]) {
    assert.equal(presentation.getWorkloadFaultText(service), null);
    assert.equal(presentation.getWorkloadFaultLabel(service), null);
  }
});

test("文案固定：不含 Pod、namespace 或 quota 物件名稱，也不讀其他欄位", () => {
  for (const code of ["quota_exceeded", "create_failed", "no_pods"]) {
    const service = { state: "running", workload_fault: code, ...CLUSTER_NOISE };
    for (const text of [
      presentation.getWorkloadFaultText(service),
      presentation.getWorkloadFaultLabel(service),
    ]) {
      assert.doesNotMatch(text, /pod|namespace|paas-ws|svc-|ResourceQuota|Deployment|forbidden/i);
      assert.doesNotMatch(text, new RegExp(code));
    }
  }
});

test("卡片提示與 banner 用同一個前置條件，文案不分代碼", () => {
  for (const code of ["quota_exceeded", "create_failed", "no_pods", "some_future_code"]) {
    assert.equal(
      presentation.getWorkloadFaultLabel({ state: "running", workload_fault: code }),
      LABEL,
      code,
    );
  }
});

// ---- F1：異常已確認時，其他以 state 為準的訊號不可以再說它正常 ----

const FAULTED = { state: "running", workload_fault: "quota_exceeded" };

test("hasConfirmedWorkloadFault：只有 running＋非空字串的 workload_fault 才是 true", () => {
  for (const code of ["quota_exceeded", "create_failed", "no_pods", "some_future_code"]) {
    assert.equal(
      presentation.hasConfirmedWorkloadFault({ state: "running", workload_fault: code }),
      true,
      code,
    );
  }
  for (const fault of [null, undefined, "", true, 1, {}, ["quota_exceeded"]]) {
    assert.equal(
      presentation.hasConfirmedWorkloadFault({ state: "running", workload_fault: fault }),
      false,
      String(fault),
    );
  }
  for (const state of ["error", "upgrading", "deploying", "suspended", "deleting", undefined]) {
    assert.equal(
      presentation.hasConfirmedWorkloadFault({ state, workload_fault: "quota_exceeded" }),
      false,
      String(state),
    );
  }
  assert.equal(presentation.hasConfirmedWorkloadFault(null), false);
  assert.equal(presentation.hasConfirmedWorkloadFault(undefined), false);
});

test("banner、卡片提示與 hasConfirmedWorkloadFault 永遠同進同出", () => {
  const cases = [
    FAULTED,
    { state: "running", workload_fault: "no_pods" },
    { state: "running", workload_fault: null },
    { state: "running" },
    { state: "error", workload_fault: "quota_exceeded" },
    { state: "upgrading", workload_fault: "quota_exceeded" },
  ];
  for (const service of cases) {
    const flagged = presentation.hasConfirmedWorkloadFault(service);
    assert.equal(presentation.getWorkloadFaultText(service) !== null, flagged, JSON.stringify(service));
    assert.equal(presentation.getWorkloadFaultLabel(service) !== null, flagged, JSON.stringify(service));
  }
});

test("連線狀態：running 但異常已確認時是 offline，不是 online", () => {
  for (const code of ["quota_exceeded", "create_failed", "no_pods"]) {
    assert.equal(connectionStatusOf({ state: "running", workload_fault: code }), "offline", code);
  }
});

test("連線狀態：沒有確認的異常時，#1456 的三態不變", () => {
  assert.equal(connectionStatusOf({ state: "running" }), "online");
  assert.equal(connectionStatusOf({ state: "running", workload_fault: null }), "online");
  assert.equal(connectionStatusOf({ state: "upgrading" }), "updating");
  assert.equal(connectionStatusOf({ state: "deploying", last_upgraded_at: "2026-10-01" }), "updating");
  assert.equal(connectionStatusOf({ state: "initializing", last_upgraded_at: "2026-10-01" }), "updating");
  assert.equal(connectionStatusOf({ state: "deploying" }), "offline");
  assert.equal(connectionStatusOf({ state: "error" }), "offline");
  assert.equal(connectionStatusOf({ state: "suspended" }), "offline");
});

test("連線狀態：不是 running 時不看 workload_fault（殘留的值不影響其他狀態）", () => {
  // 後端只在 running 時給值；這裡證明前端也不會因為一個殘留的值改掉 #1456 的判斷。
  assert.equal(connectionStatusOf({ state: "upgrading", workload_fault: "quota_exceeded" }), "updating");
  assert.equal(connectionStatusOf({ state: "error", workload_fault: "quota_exceeded" }), "offline");
});

test("連線狀態只有既有的三種，沒有新增 state", () => {
  const seen = new Set();
  for (const service of [
    FAULTED,
    { state: "running" },
    { state: "upgrading" },
    { state: "deploying" },
    { state: "deploying", last_upgraded_at: "2026-10-01" },
    { state: "error" },
    { state: "deleting" },
    { state: "pending" },
  ]) {
    seen.add(connectionStatusOf(service));
  }
  assert.deepEqual([...seen].sort(), ["offline", "online", "updating"]);
});

test("countServiceHealth：異常已確認的 running 服務不算 Healthy，算進 Alerts", () => {
  assert.deepEqual(presentation.countServiceHealth([FAULTED]), { healthy: 0, alerts: 1 });
  assert.deepEqual(
    presentation.countServiceHealth([
      { state: "running" },
      { state: "running", workload_fault: null },
      FAULTED,
      { state: "running", workload_fault: "no_pods" },
      { state: "error" },
      { state: "upgrading" },
      { state: "deploying" },
      { state: "suspended" },
    ]),
    { healthy: 2, alerts: 3 },
  );
});

test("countServiceHealth：沒有任何確認的異常時，與原本只數 state 的結果相同", () => {
  const services = [
    { state: "running" },
    { state: "running" },
    { state: "error" },
    { state: "upgrading" },
    { state: "initializing" },
    { state: "deleting" },
  ];
  assert.deepEqual(presentation.countServiceHealth(services), {
    healthy: services.filter((service) => service.state === "running").length,
    alerts: services.filter((service) => service.state === "error").length,
  });
});

test("countServiceHealth：一台服務最多只算一邊；空清單與缺席不拋錯", () => {
  // error 的服務就算帶著殘留的 workload_fault 也只算一次 Alert（後端只在 running 時給值）。
  assert.deepEqual(
    presentation.countServiceHealth([{ state: "error", workload_fault: "quota_exceeded" }]),
    { healthy: 0, alerts: 1 },
  );
  for (const services of [[], null, undefined]) {
    assert.deepEqual(presentation.countServiceHealth(services), { healthy: 0, alerts: 0 });
  }
  assert.deepEqual(presentation.countServiceHealth([null, undefined, {}]), { healthy: 0, alerts: 0 });
});

test("工作區統計卡：Healthy／Alerts 兩格用 countServiceHealth 的結果", () => {
  assert.deepEqual(
    (({ Healthy, Alerts }) => ({ Healthy, Alerts }))(
      workspaceStatsOf([{ state: "running" }, FAULTED, { state: "error" }, { state: "upgrading" }]),
    ),
    { Healthy: "1", Alerts: "2" },
  );
  // 事故當下的畫面：工作區唯一的一台服務被配額擋下。
  const incident = workspaceStatsOf([FAULTED]);
  assert.equal(incident.Healthy, "0");
  assert.equal(incident.Alerts, "1");
  assert.equal(incident["Total Apps"], "1");
});

test("#1456：Pod 在、沒有一顆就緒的三個代碼走一般文案，連線狀態不是 online、統計算 Alerts", () => {
  // 後端只在「首次看到滿 5 分鐘後又看到一次」才把這三個代碼放進 payload；前端不必認得它們，
  // 一律用一般文案（不顯示代碼本身）。
  for (const code of ["oom_killed", "crash_loop", "not_ready"]) {
    const service = { state: "running", workload_fault: code };
    assert.equal(presentation.getWorkloadFaultText(service), GENERIC_TEXT, code);
    assert.ok(presentation.getWorkloadFaultLabel(service), code);
    assert.notEqual(connectionStatusOf(service), "online", code);
    assert.deepEqual(presentation.countServiceHealth([service]), { healthy: 0, alerts: 1 }, code);
  }
});
