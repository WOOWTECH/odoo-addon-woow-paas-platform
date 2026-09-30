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
