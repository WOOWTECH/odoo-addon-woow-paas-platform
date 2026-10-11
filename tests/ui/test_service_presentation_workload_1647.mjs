// #1647：服務顯示 running、但平台確認它沒有在執行（workload 期望有 Pod、實際一顆都沒有）時，
// 詳情頁 banner 與服務卡片的固定文案。純邏輯測試——載入手法同
// test_service_presentation_1159.mjs（把 `_t` 換成恆等函式後以 data: URL import）。
//
// 跑法：node --test tests/ui/*.mjs
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

const QUOTA_TEXT =
  "This service is not running right now: it cannot start because the workspace's CPU or memory quota is full. Please contact support. Our operations team has been notified.";
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
