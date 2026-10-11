/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

export const TRANSITIONAL_SERVICE_STATES = [
  "deploying",
  "initializing",
  "upgrading",
  "deleting",
];

export const STALE_OBSERVATION_SECONDS = 180;

/**
 * 將 init 失敗轉成租戶可見的固定文案。
 *
 * 原始 operator 錯誤可能含 Pod 名稱或內部細節，因此這裡只讀明確的 init 結果、
 * 後端能力欄位與嘗試序號。ServiceCard 與 ServiceDetailPage 必須共用此入口。
 *
 * @param {Object|null|undefined} service
 * @returns {string|null}
 */
export function getInitFailureText(service) {
  if (service?.state !== "error" || !service.init_exit) {
    return null;
  }

  const attempt = Number(service.init_attempt_seq);
  const hasAttempt = Number.isInteger(attempt) && attempt > 0;
  const canReconfigure = service.init_reconfigure_allowed === true;

  if (canReconfigure && hasAttempt) {
    return _t(
      "Initialization did not complete (attempt #%s). You can change the configuration to try again, or contact support.",
      attempt,
    );
  }
  if (canReconfigure) {
    return _t(
      "Initialization did not complete. You can change the configuration to try again, or contact support.",
    );
  }
  if (hasAttempt) {
    return _t("Initialization did not complete (attempt #%s). Please contact support.", attempt);
  }
  // fast-fail／等待逾時可能在任何 init 送出前發生，不顯示不存在的「#0」。
  return _t("Initialization did not complete. Please contact support.");
}

/**
 * 服務顯示 running、但平台確認它其實沒有在執行（workload 期望有 Pod、實際一顆都沒有）時，
 * 回傳後端確認過的異常代碼；否則回 null。
 *
 * 只讀 payload 的 `workload_fault`（後端 `_workload_fault_public`：running ＋ 已確認才給值），
 * 前端不自己判斷。state 不是 running 時一律不顯示：其他狀態各有自己的說明。
 *
 * @param {Object|null|undefined} service
 * @returns {string|null}
 */
function getConfirmedWorkloadFault(service) {
  if (service?.state !== "running") {
    return null;
  }
  const fault = service.workload_fault;
  return typeof fault === "string" && fault ? fault : null;
}

/**
 * 詳情頁 banner 的固定文案（#1647）。
 *
 * 叢集的原始訊息含 Pod、namespace、quota 物件名稱等內部細節，只留在後台；這裡只依代碼挑
 * 文案。不認得的代碼（後端日後新增）一律用一般文案，不顯示代碼本身。
 *
 * ⛔ quota_exceeded 的文案只請租戶聯絡客服，不要建議刪除／停用／停止其他服務、或改用較小
 * 的方案來釋出 CPU 或記憶體配額：工作區的上限是存活服務的方案規格加總 × 3（另有下限；
 * `cloud_service.py` 的 `_compute_ns_quota`），刪掉一台上限少 3 份、用量只少約 1 份，剩餘
 * 空間通常變小而不是變大——單台 limits 被調高到超過上限時（2026-10-10 事故）刪別台反而更糟。
 * 租戶做得到又有效的只有聯絡客服（管理員調整配額的正式入口在 #1648）。
 *
 * ⛔ 也不要寫成配額「已滿／用完」：被擋下的常見形狀是「還有剩，但不夠這台啟動」（事故：
 * 上限 12、其他 workload 佔 8、這台要 8）——同一個租戶在工作區頁的配額面板看到的是
 * 「還剩 4 vCPU」，兩邊對不起來。要說的是「剩餘的配額不足以啟動這台服務」。
 *
 * @param {Object|null|undefined} service
 * @returns {string|null}
 */
export function getWorkloadFaultText(service) {
  const fault = getConfirmedWorkloadFault(service);
  if (!fault) {
    return null;
  }
  if (fault === "quota_exceeded") {
    return _t(
      "This service is not running right now: the workspace does not have enough CPU or memory quota left to start it. Please contact support. Our operations team has been notified.",
    );
  }
  return _t(
    "This service is not running right now: the platform could not start it. Our operations team has been notified.",
  );
}

/**
 * 服務卡片上的一行提示（#1647）；判準與 getWorkloadFaultText 相同，詳細文案在詳情頁。
 *
 * @param {Object|null|undefined} service
 * @returns {string|null}
 */
export function getWorkloadFaultLabel(service) {
  if (!getConfirmedWorkloadFault(service)) {
    return null;
  }
  return _t("Not running — the platform could not start this service");
}

/**
 * 回傳過久未觀測的顯示資料；等待首次真觀測時不可誤稱為「最後觀測」。
 *
 * @param {Object|null|undefined} service
 * @returns {{minutes: number, observationPending: boolean}|null}
 */
export function getStaleObservation(service) {
  if (!service || !TRANSITIONAL_SERVICE_STATES.includes(service.state)) {
    return null;
  }

  const age = service.observed_age_seconds;
  if (typeof age !== "number" || age <= STALE_OBSERVATION_SECONDS) {
    return null;
  }

  return {
    minutes: Math.floor(age / 60),
    observationPending: service.observation_pending === true,
  };
}

/**
 * @param {{minutes: number, observationPending: boolean}|null} observation
 * @returns {string|null}
 */
export function getStaleObservationLabel(observation) {
  if (!observation) {
    return null;
  }
  if (observation.observationPending) {
    return _t("Waiting for status update %s min", observation.minutes);
  }
  return _t("Last updated %s min ago", observation.minutes);
}

/**
 * Support project 的唯一入口判準。載入中、已開啟建立視窗或 Projects 關閉時一律不動作，
 * 避免連點開出第二個 project。
 *
 * @param {{projectsVisible: boolean, supportProject: Object|null, supportProjectLoading: boolean, createModalOpen: boolean}} state
 * @returns {"open"|"create"|null}
 */
export function getSupportProjectAction({
  projectsVisible,
  supportProject,
  supportProjectLoading,
  createModalOpen,
}) {
  if (!projectsVisible || supportProjectLoading || createModalOpen) {
    return null;
  }
  return supportProject ? "open" : "create";
}
