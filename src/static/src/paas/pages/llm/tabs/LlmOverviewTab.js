/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../../../components/button/WoowButton";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { LlmKeyRevealModal } from "../../../components/llm/LlmKeyRevealModal";
import { LlmTermsModal } from "../../../components/llm/LlmTermsModal";
import { llmService } from "../../../services/llm_service";
import { router } from "../../../core/router";
import { LLM_SESSION_EXPIRED, llmErrorMessage, llmSuspendReason } from "../../../services/llm_errors";
import { representativeModels } from "./LlmModelsTab";
import { copyText } from "../../../utils/clipboard";
import {
  formatAiQuota,
  formatFreeTrialUsage,
  formatLatency,
  formatTaipeiDate,
  formatTaipeiTime,
  formatTokens,
  formatWholePoints,
  isExpiringSoon,
  isZeroCreditQuota,
  overdraftPointsText,
} from "../../../utils/llm_format";

/** 開通中輪詢 `/api/llm/state`（讀 DB、不外呼）：前 2 分鐘每 3 秒，之後每 10 秒。 */
export const PROVISION_POLL_FAST_MS = 3000;
export const PROVISION_POLL_SLOW_MS = 10000;
const PROVISION_FAST_WINDOW_MS = 2 * 60 * 1000;
/** 連續失敗時的退避（3 秒之後 6 → 12 → 30 秒，之後都是 30 秒）；成功一次就回到正常節奏。 */
export const PROVISION_POLL_BACKOFF_MS = Object.freeze([6000, 12000, 30000]);
/** 連續失敗幾次之後，開通中畫面多一句「暫時無法取得最新狀態」。 */
export const PROVISION_FAILURE_HINT_AFTER = 3;
/** 開通超過 3 分鐘：多一句「比平常久」（DESIGN §3.5 的 SLO 是「通常 1 分鐘內」）。 */
const PROVISION_SLOW_HINT_MS = 3 * 60 * 1000;
/**
 * 「最近 10 筆」每 30 秒重新整理一次。`/api/llm/usage/recent` 只讀後端快取（SWR，N26；LLM-02）：
 * 快取超過 60 秒時後端排一次背景刷新，這次先回舊資料與 `data_as_of`。分頁在背景時計時器到了也不刷，
 * 等回到前景才補一次；回到前景時計時器還沒到就不刷（反覆切分頁不會多打）。
 */
export const USAGE_REFRESH_MS = 30000;
/**
 * 還沒有今天的快取（`data_as_of: null`，背景刷新剛排下去）：先每 3 秒再問一次、最多 5 次，
 * 之後回到 30 秒的節奏（new-api 連不上時不會一直狂問）。
 */
export const USAGE_FIRST_FILL_MS = 3000;
export const USAGE_FIRST_FILL_POLLS = 5;

/** 下一次「最近 10 筆」的間隔（純函式，tests/ui/test_llm_overview.mjs 直接驗）。 */
export function usageRefreshDelay(result, firstFillPolls) {
  const data = result && result.success ? result.data : null;
  if (data && data.data_as_of == null && firstFillPolls < USAGE_FIRST_FILL_POLLS) {
    return USAGE_FIRST_FILL_MS;
  }
  return USAGE_REFRESH_MS;
}
/** 建立中的試用金鑰超過這麼久（與後端 `_STALE_CREATING_SECONDS` 相同），處理它的 worker 已經不在了。 */
export const STALE_CREATING_MS = 300 * 1000;

/** 下一次開通輪詢的間隔（純函式，tests/ui/test_llm_overview.mjs 直接驗）。 */
export function provisionPollDelay(elapsedMs, consecutiveFailures) {
  if (consecutiveFailures > 0) {
    const index = Math.min(consecutiveFailures, PROVISION_POLL_BACKOFF_MS.length) - 1;
    return PROVISION_POLL_BACKOFF_MS[index];
  }
  return elapsedMs < PROVISION_FAST_WINDOW_MS ? PROVISION_POLL_FAST_MS : PROVISION_POLL_SLOW_MS;
}

/**
 * 總覽要顯示哪一個畫面（DESIGN §6.2）。純函式，tests/ui/test_llm_overview.mjs 直接驗。
 *
 * - `llm_disabled`：開關在頁面載入後被關掉（router 的 gate 只看載入時的 session）。
 * - 登入逾時（`llm_session_expired`）或 gate 的帳號拒絕（`llm_auth_required`）：即使有舊資料
 *   也換成「請重新整理頁面」——重試沒用，輪詢也不能拿著舊的「開通中」一直轉。
 * - 其他失敗：已經有上一次的資料就照舊顯示（輪詢途中的一次失敗不把畫面整個換掉），
 *   沒有才顯示「暫時無法連線」。
 * - `closed`：後端目前不回已關閉的帳戶（`_llm_account_for_partner` 排除 closed；關閉與重新開通
 *   在 LLM-13），回了就顯示「已關閉」畫面，不落進「尚未開通」。
 */
export function llmOverviewScreen({ loaded, overview, error }) {
  if (!loaded) return "loading";
  if (error === "llm_disabled") return "disabled";
  if (error === LLM_SESSION_EXPIRED || error === "llm_auth_required") return "auth";
  if (error && !overview) return "unavailable";
  const state = overview && overview.account ? overview.account.state : null;
  if (state === "provisioning") return "provisioning";
  if (state === "error") return "failed";
  if (state === "suspended") return "suspended";
  if (state === "closed") return "closed";
  if (state === "active") return "active";
  return "not_activated";
}

const KEY_STATUS = Object.assign(Object.create(null), {
  creating: { tone: "pending", label: () => _t("Creating…") },
  active: { tone: "ok", label: () => _t("Active") },
  disabled: { tone: "off", label: () => _t("Disabled") },
  expired: { tone: "off", label: () => _t("Expired") },
  failed_reveal: { tone: "warn", label: () => _t("Creation interrupted") },
  lost: { tone: "error", label: () => _t("Lost in a system restore. Please create a new key.") },
});

const UNKNOWN_KEY_STATUS = { tone: "off", label: () => _t("Unknown status") };

function keyStatus(status) {
  return typeof status === "string" && Object.prototype.hasOwnProperty.call(KEY_STATUS, status)
    ? KEY_STATUS[status] : UNKNOWN_KEY_STATUS;
}

/** 清單一列（只有遮罩；明文從來不在這裡）。 */
export function llmKeyRow(key, nowMillis = Date.now()) {
  const freeCapped = key.status === "disabled" && key.disabled_by === "free_daily_cap";
  const status = keyStatus(key.status);
  return {
    id: key.id,
    name: key.display_name || (key.kind === "free_trial" ? _t("Free trial key") : _t("Unnamed key")),
    kind: key.kind === "free_trial" ? _t("Free trial") : _t("Paid key"),
    masked: key.masked_key || "—",
    statusLabel: freeCapped ? _t("Today's free trial is used up") : status.label(),
    statusNote: freeCapped ? _t("Restores automatically at 00:00 Taipei time") : null,
    tone: freeCapped ? "warn" : status.tone,
    expires: key.expires_at ? formatTaipeiDate(key.expires_at) : _t("Never expires"),
    expiringSoon: key.status === "active" && isExpiringSoon(key.expires_at, nowMillis),
  };
}

/** 「最近 10 筆」一列。花費不顯示：免費試用一律免費（DESIGN §6.5）。 */
export function llmUsageRow(item, index) {
  return {
    key: item.request_id || "row-" + index,
    time: formatTaipeiTime(item.time),
    model: item.model || "—",
    input: formatTokens(item.input_tokens),
    output: formatTokens(item.output_tokens),
    latency: formatLatency(item.duration_ms),
  };
}

/**
 * 「AI 模型」總覽（DESIGN §6.2、§6.3.1；票 LLM-01c tracer）：尚未開通 → 開通中（輪詢）→
 * 開通失敗／已開通；已開通時有 AI 額度、今日免費試用、Base URL、建立免費試用金鑰、
 * 金鑰清單（只有遮罩）與最近 10 筆免費試用呼叫。
 */
export class LlmOverviewTab extends Component {
  static template = "woow_paas_platform.LlmOverviewTab";
  static components = { WoowButton, WoowCard, WoowIcon, LlmKeyRevealModal, LlmTermsModal };
  static props = {};

  setup() {
    this.llm = useState(llmService);
    this.state = useState({
      tosAccepted: false,
      activating: false,
      refreshing: false,
      actionError: null,
      showReveal: false,
      /** 條款對話框：null（關）、"read"（尚未開通畫面的連結）、"accept"（改版橫幅的〔查看並同意〕）。 */
      showTerms: null,
      baseUrlCopied: false,
      baseUrlCopyFailed: false,
      refreshingUsage: false,
      /** 後端還沒有今天的快取、正在快問（「讀取中」而不是「今天還沒有呼叫紀錄」）。 */
      usageWaiting: false,
      provisionSince: null,
      pollFailing: false,
      announcement: "",
      now: Date.now(),
    });
    /** 上一次 onStateLoaded 的畫面（開通中 → 已開通／失敗時給讀屏播報一次）。 */
    this.lastScreen = null;
    this.freeTrialHeadingRef = useRef("freeTrialHeading");
    /** 打開條款對話框的那顆鈕：關閉後焦點還給它（還在畫面上的話）。 */
    this.termsTrigger = null;
    this.alive = true;
    /** `/api/llm/state` 連續失敗次數（開通輪詢的退避用；成功一次歸零）。 */
    this.stateFailures = 0;
    this.pollTimer = null;
    /** 開通輪詢的計時器在背景分頁到期：先不打，回到前景再補一次。 */
    this.pollPaused = false;
    this.usageTimer = null;
    /** 「最近 10 筆」的計時器在背景分頁到期（已滿最短間隔）：回到前景再補一次。 */
    this.usagePaused = false;
    /** 還沒有快取時已經快問了幾次（見 USAGE_FIRST_FILL_POLLS）。 */
    this.usageFirstFillPolls = 0;
    this.onVisibilityChange = () => {
      if (document.hidden || !this.alive) return;
      if (this.pollPaused) {
        this.pollPaused = false;
        this.load();
      }
      if (this.usagePaused) {
        this.usagePaused = false;
        if (this.screen === "active") {
          this.refreshUsage();
        }
      }
    };
    onMounted(() => {
      document.addEventListener("visibilitychange", this.onVisibilityChange);
      this.load();
      llmService.fetchModels();
    });
    onWillUnmount(() => {
      this.alive = false;
      clearTimeout(this.pollTimer);
      clearTimeout(this.usageTimer);
      document.removeEventListener("visibilitychange", this.onVisibilityChange);
    });
  }

  // ------------------------------------------------------------------ 資料

  async load() {
    const result = await llmService.fetchState();
    if (!this.alive) return;
    this.stateFailures = result && result.success ? 0 : this.stateFailures + 1;
    this.onStateLoaded();
  }

  onStateLoaded() {
    clearTimeout(this.pollTimer);
    this.pollTimer = null;
    this.pollPaused = false;
    const screen = this.screen;
    if (this.lastScreen === "provisioning" && screen !== "provisioning") {
      this.state.announcement = screen === "active" ? this.activatedText
        : screen === "failed" ? this.failedText : "";
    }
    this.lastScreen = screen;
    if (screen === "provisioning") {
      const now = Date.now();
      if (!this.state.provisionSince) {
        this.state.provisionSince = now;
      }
      this.state.now = now;
      this.state.pollFailing = this.stateFailures >= PROVISION_FAILURE_HINT_AFTER;
      const delay = provisionPollDelay(now - this.state.provisionSince, this.stateFailures);
      this.pollTimer = setTimeout(() => this.onPollTimer(), delay);
      return;
    }
    this.state.provisionSince = null;
    this.state.pollFailing = false;
    if (screen === "active" || screen === "suspended") {
      llmService.fetchKeys();
    }
    if (screen === "active") {
      this.refreshUsage();
    } else {
      clearTimeout(this.usageTimer);
      this.usageTimer = null;
      this.usagePaused = false;
    }
  }

  onPollTimer() {
    this.pollTimer = null;
    if (!this.alive) return;
    if (document.hidden) {
      this.pollPaused = true;
      return;
    }
    this.load();
  }

  async refreshUsage() {
    if (this.state.refreshingUsage) return;
    clearTimeout(this.usageTimer);
    this.usageTimer = null;
    this.usagePaused = false;
    this.state.refreshingUsage = true;
    const result = await llmService.fetchUsageRecent();
    if (!this.alive) return;
    this.state.refreshingUsage = false;
    if (result && result.code === LLM_SESSION_EXPIRED) {
      // 登入逾時：再打也一樣，等使用者重新整理頁面。
      return;
    }
    const delay = usageRefreshDelay(result, this.usageFirstFillPolls);
    if (delay === USAGE_FIRST_FILL_MS) {
      this.usageFirstFillPolls += 1;
    } else if (result && result.success && result.data && result.data.data_as_of != null) {
      // 第一次快問已用完時保持慢速；只有拿到新快取，才重設下一次缺資料的預算。
      this.usageFirstFillPolls = 0;
    }
    this.state.usageWaiting = delay === USAGE_FIRST_FILL_MS;
    this.usageTimer = setTimeout(() => this.onUsageTimer(), delay);
  }

  onUsageTimer() {
    this.usageTimer = null;
    if (!this.alive || this.screen !== "active") return;
    if (document.hidden) {
      this.usagePaused = true;
      return;
    }
    this.refreshUsage();
  }

  // ------------------------------------------------------------------ 畫面狀態

  get screen() {
    return llmOverviewScreen({
      loaded: this.llm.overviewLoaded,
      overview: this.llm.overview,
      error: this.llm.overviewError,
    });
  }

  get account() {
    return (this.llm.overview && this.llm.overview.account) || null;
  }

  get tosVersion() {
    const overview = this.llm.overview;
    if (!overview) return null;
    return overview.tos_current_version
      || (overview.account && overview.account.tos && overview.account.tos.current_version)
      || null;
  }

  /** published 的條款還是草稿文字（D15：stg 的草稿；`/api/llm/state` 的 tos_current_draft）。 */
  get tosIsDraft() {
    const overview = this.llm.overview;
    return !!overview && overview.tos_current_draft === true;
  }

  /** 沒有 published 的條款（或讀不到版本）：不能開通，也不能叫使用者「請先同意」。 */
  get tosUnavailable() {
    return this.screen === "not_activated" && !this.tosVersion;
  }

  get tosUnavailableText() {
    return llmErrorMessage({ code: "llm_tos_unavailable" });
  }

  get canActivate() {
    return this.state.tosAccepted && !this.state.activating && !!this.tosVersion;
  }

  get unavailableText() {
    return llmErrorMessage({ code: this.llm.overviewError });
  }

  get disabledText() {
    return llmErrorMessage({ code: "llm_disabled" });
  }

  get authText() {
    return llmErrorMessage({ code: this.llm.overviewError });
  }

  // 文案放在 JS（單行 _t）：模板的多行文字節點會把換行與縮排一起抽進 msgid。
  get heroTitle() {
    return _t("Call AI models with your credit");
  }

  get heroSubtitle() {
    return _t("OpenAI-compatible API, plus a free trial of Qwen3-4B.");
  }

  get representativePrices() {
    return this.llm.models && this.llm.models.state === "ok" ? representativeModels(this.llm.models.items) : [];
  }

  get previewUnit() { return _t("Input / output points per million tokens"); }

  /** 價目與開通各自載入：讀不到價格仍可同意條款並開通。 */
  get pricingFallbackText() {
    return _t("Prices are temporarily unavailable. You can view Models & Pricing after activation.");
  }

  get tosDraftText() {
    return _t("These terms are a draft and have not taken effect yet.");
  }

  get provisioningText() {
    return _t("Setting up your AI models account… (usually done within 1 minute)");
  }

  get provisioningProgressLabel() {
    return _t("Setting up your AI models account");
  }

  get provisioningElapsedText() {
    const since = this.state.provisionSince;
    const seconds = since ? Math.max(0, Math.floor((this.state.now - since) / 1000)) : 0;
    return _t("Waited %(seconds)s s", { seconds });
  }

  get provisioningSlow() {
    const since = this.state.provisionSince;
    return !!since && this.state.now - since > PROVISION_SLOW_HINT_MS;
  }

  get provisioningSlowText() {
    return _t("This is taking longer than usual. Setup continues in the background; you can come back later.");
  }

  get activatedText() {
    return _t("Your AI models service is now active.");
  }

  get pollFailingText() {
    return _t("The latest status cannot be fetched right now. We will keep retrying automatically.");
  }

  /**
   * 開通失敗：tracer 的帳戶 `error` 是終態（重新開戶 `/api/llm/account/retry` 在 LLM-03），
   * 所以不顯示 DESIGN §6.2 的「請稍後再試」〔重試〕，直接說要聯絡客服（llm_account_error 的句子）。
   */
  get failedText() {
    return llmErrorMessage({ code: "llm_account_error" });
  }

  get suspendedText() {
    const reason = llmSuspendReason(this.account && this.account.suspend_reason);
    return reason
      ? _t("The AI models service is suspended: %(reason)s", { reason })
      : _t("The AI models service is suspended. Please contact support.");
  }

  get closedText() {
    const quota = this.account ? this.account.balance_quota : null;
    const points = Number.isInteger(quota) && quota > 0 ? formatAiQuota(quota) : null;
    return points
      ? _t("Your AI models account is closed. The remaining %(points)s are not refundable under the terms. To reactivate, please contact support.", { points })
      : _t("Your AI models account is closed. To reactivate, please contact support.");
  }

  /** AI 額度：用整數 quota 換算（2 位小數、捨去）；只有整數點數時不帶小數。 */
  get balanceText() {
    const account = this.account;
    if (!account) return "—";
    if (Number.isInteger(account.balance_quota)) {
      return formatAiQuota(account.balance_quota);
    }
    if (Number.isInteger(account.overdraft_points) && account.overdraft_points > 0) {
      return formatWholePoints(-account.overdraft_points);
    }
    return formatWholePoints(account.balance_points);
  }

  get overdraftText() {
    const account = this.account;
    if (!account || (this.screen !== "active" && this.screen !== "suspended")) return null;
    let points = overdraftPointsText(account.balance_quota);
    if (points === null && Number.isInteger(account.overdraft_points) && account.overdraft_points > 0) {
      points = String(account.overdraft_points);
    }
    return points === null
      ? null
      : _t("Your AI credit is overdrawn by %(points)s points; the next conversion will offset it first.", { points });
  }

  get showZeroCreditBanner() {
    const account = this.account;
    if (this.screen !== "active" || !account) return false;
    if (Number.isInteger(account.balance_quota)) {
      return isZeroCreditQuota(account.balance_quota);
    }
    return account.balance_points === 0 && !account.overdraft_points;
  }

  /** 〔轉換點數〕在 LLM-08：tracer 期間只說「即將推出」，不叫使用者去找畫面上沒有的按鈕。 */
  get zeroCreditText() {
    return _t("Your AI credit is 0. You can use the free trial model for now; moving credit from your points wallet is coming soon.");
  }

  /** 條款改版（DESIGN §6.4-3）：版本不同就顯示橫幅與〔查看並同意〕。 */
  get needsReaccept() {
    const account = this.account;
    return !!account && !!account.tos && account.tos.needs_reaccept === true
      && (this.screen === "active" || this.screen === "suspended");
  }

  /**
   * gate 會擋（`tos.blocking`）：建金鑰、轉換、啟用金鑰都要先同意。`requires_reaccept=false` 的改版只有
   * 橫幅、不擋——畫面與 gate 要一致（LLM-05 審查）。舊後端沒有 `blocking` 時，版本不同就當成會擋。
   */
  get tosBlocking() {
    const tos = this.account && this.account.tos;
    if (!tos) return false;
    if (typeof tos.blocking === "boolean") return tos.blocking;
    return tos.needs_reaccept === true;
  }

  get needsReacceptText() {
    return this.tosBlocking
      ? _t("The AI Models Terms of Service have been updated. Please accept them again before creating keys, converting points or re-enabling keys; existing keys are not affected.")
      : _t("The AI Models Terms of Service have been updated. Please review the new version; nothing you can do now is affected.");
  }

  /** 全域 guard（DESIGN §6.2「系統維護」）：建金鑰與轉換都停，既有金鑰照常。 */
  get guardTripped() {
    const overview = this.llm.overview;
    return !!overview && !!overview.guard && overview.guard.tripped === true;
  }

  get guardText() {
    return _t("System maintenance in progress: keys cannot be created and points cannot be converted right now. Existing keys keep working.");
  }

  get freeTrial() {
    // Both endpoints read llm_free_usage. Keep a fresh state response authoritative
    // when the usage refresh fails or still belongs to the previous Taipei day.
    const state = (this.llm.overview && this.llm.overview.free_trial) || {};
    const usage = this.llm.usage && this.llm.usage.free_trial;
    if (!usage || this.llm.usageError ||
        (state.day_tpe && (!usage.day_tpe || usage.day_tpe < state.day_tpe))) {
      return state;
    }
    return { ...state, ...usage };
  }

  get freeTrialUsageText() {
    return formatFreeTrialUsage(this.freeTrial.today_count, this.freeTrial.daily_limit || 200);
  }

  get freeTrialCappedText() {
    return this.freeTrial.capped
      ? _t("Today's free trial is used up. Restores automatically at 00:00 Taipei time.") : null;
  }

  get freeTrialCreateDisabledText() {
    if (this.freeTrial.capped) return this.freeTrialCappedText;
    return this.freeTrial.keys_created_today >= 3
      ? llmErrorMessage({ code: "llm_free_key_daily_limit" }) : null;
  }

  get baseUrl() {
    const url = this.llm.overview && this.llm.overview.public_base_url;
    return typeof url === "string" && url ? url : null;
  }

  /** LlmKeyRevealModal 的 baseUrl 是選填的 String：沒有值時傳 undefined、不傳 null。 */
  get revealBaseUrl() {
    return this.baseUrl || undefined;
  }

  get baseUrlMissingText() {
    return _t("Not available yet");
  }

  /** state 的 free_trial.key_id 對到的清單列（只有遮罩）；清單還沒讀到時是 null。 */
  get freeTrialKey() {
    const id = this.freeTrial.key_id;
    if (!id) return null;
    return (this.llm.keys || []).find((key) => key.id === id) || null;
  }

  /**
   * 已經有試用金鑰。後端把超過 5 分鐘還在「建立中」的列排除在 key_id 之外；舊版後端沒排除時，
   * 清單列的 created_at 超過同一個門檻也不算（處理它的 worker 已經不在了，送出建立會由後端收尾）。
   */
  get hasFreeTrialKey() {
    if (!this.freeTrial.key_id) return false;
    const key = this.freeTrialKey;
    if (key && key.status === "creating" && typeof key.created_at === "string") {
      const created = Date.parse(key.created_at);
      if (!Number.isNaN(created) && Date.now() - created > STALE_CREATING_MS) {
        return false;
      }
    }
    return true;
  }

  get canCreateFreeTrialKey() {
    return this.screen === "active" && !this.hasFreeTrialKey && !this.state.showReveal
      && !this.guardTripped && !this.tosBlocking && !this.freeTrialCreateDisabledText;
  }

  get freeTrialHint() {
    return _t("Free trial keys can only call Qwen3-4B: 10 calls per minute and about 200 calls per day.");
  }

  /**
   * 已有試用金鑰時的說明。金鑰管理（停用、刪除後重建，LLM-07）上線前，遺失或到期的免費試用
   * 金鑰使用者自己沒有出路，只能請客服重設——句子要給走得通的下一步。
   */
  get freeTrialExistsText() {
    const key = this.freeTrialKey;
    if (key && key.status === "expired") {
      return _t("Your free trial key has expired. Go to Key Management to create a new key and disable or delete the old one.");
    }
    return _t("You already have a free trial key (one per account). If you did not save it, go to Key Management to create a new key and disable or delete the old one.");
  }

  get keyRows() {
    const now = Date.now();
    return (this.llm.keys || []).map((key) => llmKeyRow(key, now));
  }

  get keysErrorText() {
    return this.llm.keysError ? llmErrorMessage({ code: this.llm.keysError }) : null;
  }

  get keysLoading() {
    return !this.llm.keysLoaded && !this.llm.keysError;
  }

  get usageRows() {
    const items = (this.llm.usage && Array.isArray(this.llm.usage.items)) ? this.llm.usage.items : [];
    return items.map((item, index) => llmUsageRow(item, index));
  }

  get usageErrorText() {
    return this.llm.usageError ? llmErrorMessage({ code: this.llm.usageError }) : null;
  }

  /**
   * usage 還沒回來（null）、或後端還沒有今天的快取而且還在快問，都不能說「今天還沒有呼叫紀錄」。
   */
  get usageLoading() {
    if (this.llm.usageError) return false;
    return !this.llm.usage || (this.llm.usage.data_as_of == null && this.state.usageWaiting);
  }

  get usageDataTimeText() {
    const asOf = this.llm.usage && this.llm.usage.data_as_of;
    return asOf ? _t("Data as of %(time)s (Taipei time)", { time: formatTaipeiTime(asOf) }) : null;
  }

  get emptyUsageText() {
    if (this.llm.usage && this.llm.usage.data_as_of == null) {
      // 快問了幾次還是沒有快取（new-api 暫時連不上）：不能假裝「今天沒有呼叫」。
      return _t("Usage data is not available yet. Please check back later.");
    }
    return _t("No free trial calls today yet.");
  }

  get emptyKeysText() {
    return _t("No API keys yet.");
  }

  get listLoadingText() {
    return _t("Loading list…");
  }

  /** 卡片堆疊（手機）時每一格前面的欄名（data-label，CSS ::before 顯示）。 */
  get labels() {
    return {
      name: _t("Name"),
      kind: _t("Key type"),
      key: _t("API key"),
      status: _t("Status"),
      expires: _t("Expires on (Taipei time)"),
      time: _t("Time (Taipei time)"),
      model: _t("Model"),
      input: _t("Input tokens"),
      output: _t("Output tokens"),
      latency: _t("Latency"),
    };
  }

  get copyBaseUrlLabel() {
    return _t("Copy Base URL");
  }

  get expiringSoonText() {
    return _t("Expiring soon");
  }

  get copyFailedText() {
    return _t("Copy failed. Please select the text and copy it manually.");
  }

  // ------------------------------------------------------------------ 動作

  onTosChange(ev) {
    this.state.tosAccepted = !!ev.target.checked;
  }

  async activate() {
    if (!this.canActivate) return;
    this.state.activating = true;
    this.state.actionError = null;
    const result = await llmService.activate(this.tosVersion);
    if (!this.alive) return;
    this.state.activating = false;
    // llm_provisioning：同一帳號另一個請求先建好了帳戶（後端 docstring 的預期結果），照常輪詢。
    if (result.success || result.code === "llm_provisioning") {
      this.state.provisionSince = Date.now();
      await this.load();
      return;
    }
    this.state.actionError = llmErrorMessage(result);
    if (result.code === "llm_tos_required" || result.code === "llm_tos_outdated") {
      // 送出的不是目前的版本（頁面打開之後改版了）：取新版本、要求重新勾選。
      this.state.tosAccepted = false;
      await this.load();
    }
  }

  /** 打開條款對話框（「尚未開通」的連結＝read；改版橫幅的〔查看並同意〕＝accept）。 */
  openTerms(mode, ev) {
    if (this.state.showTerms) return;
    this.termsTrigger = ev && ev.currentTarget ? ev.currentTarget : null;
    this.state.showTerms = mode === "accept" ? "accept" : "read";
  }

  /**
   * 條款對話框關閉：焦點還給打開它的那顆鈕。同意了 ⇒ 重新讀狀態（橫幅消失、建金鑰恢復）；
   * 只是閱讀、而對話框讀到的版本和畫面上的不同（頁面打開之後改版）⇒ 也重新讀，並取消勾選。
   */
  onTermsClosed({ accepted, version } = {}) {
    this.state.showTerms = null;
    const trigger = this.termsTrigger;
    this.termsTrigger = null;
    // 同意之後橫幅（連同〔查看並同意〕）會消失：焦點改放「免費試用」標題（tabindex=-1，畫面上一定還在）。
    const heading = accepted ? this.freeTrialHeadingRef.el : null;
    const target = heading || trigger;
    if (target && target.isConnected !== false && typeof target.focus === "function") {
      target.focus();
    }
    if (accepted) {
      this.state.actionError = null;
      this.state.announcement = this.termsAcceptedText;
      this.load();
      return;
    }
    if (typeof version === "string" && version && this.tosVersion && version !== this.tosVersion) {
      this.state.tosAccepted = false;
      this.load();
    }
  }

  get termsAcceptedText() {
    return _t("You have accepted the updated AI Models Terms of Service.");
  }

  /** 開通失敗與「暫時無法連線」的〔重新整理〕：重新讀一次帳戶狀態（tracer 沒有重新開戶）。 */
  async refresh() {
    if (this.state.refreshing) return;
    this.state.refreshing = true;
    this.state.actionError = null;
    await this.load();
    if (!this.alive) return;
    this.state.refreshing = false;
  }

  /** 登入逾時：只有重新整理整頁（重新登入）才有用。 */
  reloadPage() {
    window.location.reload();
  }

  async copyBaseUrl() {
    const ok = this.baseUrl ? await copyText(this.baseUrl) : false;
    if (!this.alive) return;
    this.state.baseUrlCopied = ok;
    this.state.baseUrlCopyFailed = !ok;
  }

  openFreeTrialKey() {
    if (!this.canCreateFreeTrialKey) return;
    this.state.actionError = null;
    this.state.showReveal = true;
  }

  createFreeTrialKey(requestId) { return llmService.createFreeTrialKey(requestId); }

  goToKeyManagement() { router.navigate("llm/keys"); }

  /**
   * 對話框關閉：只拿到「有沒有建成」，金鑰內容不經過這裡。焦點還給「免費試用」標題（觸發鈕在
   * 對話框開著時是停用的，建成之後也換成說明文字），再重新整理狀態與清單。
   */
  onRevealClosed() {
    this.state.showReveal = false;
    const heading = this.freeTrialHeadingRef.el;
    if (heading && typeof heading.focus === "function") {
      heading.focus();
    }
    this.load();
  }
}
