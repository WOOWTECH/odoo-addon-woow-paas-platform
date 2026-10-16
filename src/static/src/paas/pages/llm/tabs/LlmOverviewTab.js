/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../../../components/button/WoowButton";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { LlmKeyRevealModal } from "../../../components/llm/LlmKeyRevealModal";
import { llmService } from "../../../services/llm_service";
import { llmErrorMessage, llmSuspendReason } from "../../../services/llm_errors";
import { copyText } from "../../../utils/clipboard";
import {
  formatAiPoints,
  formatFreeTrialUsage,
  formatLatency,
  formatTaipeiDate,
  formatTaipeiTime,
  formatTokens,
  isExpiringSoon,
} from "../../../utils/llm_format";

/** 開通中輪詢 `/api/llm/state`（讀 DB、不外呼）：前 2 分鐘每 3 秒，之後每 10 秒。 */
export const PROVISION_POLL_FAST_MS = 3000;
export const PROVISION_POLL_SLOW_MS = 10000;
const PROVISION_FAST_WINDOW_MS = 2 * 60 * 1000;
/** 開通超過 3 分鐘：多一句「比平常久」（DESIGN §3.5 的 SLO 是「通常 1 分鐘內」）。 */
const PROVISION_SLOW_HINT_MS = 3 * 60 * 1000;
/**
 * 「最近 10 筆」每 30 秒重新整理一次（分頁在背景時不刷，回到前景立刻刷）。tracer 期間
 * `/api/llm/usage/recent` 是同步外呼，每次吃租戶合成 IP 1 格 GA（360 次／180 秒，DESIGN §1.5）。
 */
export const USAGE_REFRESH_MS = 30000;

/**
 * 總覽要顯示哪一個畫面（DESIGN §6.2）。純函式，tests/ui/test_llm_nav.mjs 直接驗。
 *
 * - `llm_disabled`：開關在頁面載入後被關掉（router 的 gate 只看載入時的 session）。
 * - 其他失敗：已經有上一次的資料就照舊顯示（輪詢途中的一次失敗不把畫面整個換掉），
 *   沒有才顯示「暫時無法連線」。
 * - 後端不回已關閉的帳戶（`_llm_account_for_partner` 排除 closed）⇒ 沒有帳戶＝尚未開通。
 */
export function llmOverviewScreen({ loaded, overview, error }) {
  if (!loaded) return "loading";
  if (error === "llm_disabled") return "disabled";
  if (error && !overview) return "unavailable";
  const state = overview && overview.account ? overview.account.state : null;
  if (state === "provisioning") return "provisioning";
  if (state === "error") return "failed";
  if (state === "suspended") return "suspended";
  if (state === "active") return "active";
  return "not_activated";
}

const KEY_STATUS = {
  creating: { tone: "pending", label: () => _t("Creating…") },
  active: { tone: "ok", label: () => _t("Active") },
  disabled: { tone: "off", label: () => _t("Disabled") },
  expired: { tone: "off", label: () => _t("Expired") },
  failed_reveal: { tone: "warn", label: () => _t("Creation interrupted") },
  lost: { tone: "error", label: () => _t("Lost in a system restore. Please create a new key.") },
};

/** 清單一列（只有遮罩；明文從來不在這裡）。 */
export function llmKeyRow(key, nowMillis = Date.now()) {
  const freeCapped = key.status === "disabled" && key.disabled_by === "free_daily_cap";
  const status = KEY_STATUS[key.status] || { tone: "off", label: () => _t("Unknown status") };
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
  static components = { WoowButton, WoowCard, WoowIcon, LlmKeyRevealModal };
  static props = {};

  setup() {
    this.llm = useState(llmService);
    this.state = useState({
      tosAccepted: false,
      activating: false,
      retrying: false,
      actionError: null,
      showReveal: false,
      baseUrlCopied: false,
      baseUrlCopyFailed: false,
      refreshingUsage: false,
      provisionSince: null,
      now: Date.now(),
    });
    this.alive = true;
    this.pollTimer = null;
    this.usageTimer = null;
    this.onVisibilityChange = () => {
      if (!document.hidden && this.screen === "active") {
        this.refreshUsage();
      }
    };
    onMounted(() => {
      document.addEventListener("visibilitychange", this.onVisibilityChange);
      this.load();
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
    await llmService.fetchState();
    if (this.alive) {
      this.onStateLoaded();
    }
  }

  onStateLoaded() {
    clearTimeout(this.pollTimer);
    const screen = this.screen;
    if (screen === "provisioning") {
      const now = Date.now();
      if (!this.state.provisionSince) {
        this.state.provisionSince = now;
      }
      this.state.now = now;
      const fast = now - this.state.provisionSince < PROVISION_FAST_WINDOW_MS;
      this.pollTimer = setTimeout(() => this.load(), fast ? PROVISION_POLL_FAST_MS : PROVISION_POLL_SLOW_MS);
      return;
    }
    this.state.provisionSince = null;
    if (screen === "active" || screen === "suspended") {
      llmService.fetchKeys();
    }
    if (screen === "active") {
      this.refreshUsage();
    } else {
      clearTimeout(this.usageTimer);
    }
  }

  async refreshUsage() {
    if (this.state.refreshingUsage) return;
    clearTimeout(this.usageTimer);
    this.state.refreshingUsage = true;
    await llmService.fetchUsageRecent();
    if (!this.alive) return;
    this.state.refreshingUsage = false;
    this.usageTimer = setTimeout(() => {
      // 背景分頁不刷（回到前景時 visibilitychange 會立刻刷一次）。
      if (this.alive && !document.hidden && this.screen === "active") {
        this.refreshUsage();
      }
    }, USAGE_REFRESH_MS);
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

  get tosIsDraft() {
    return typeof this.tosVersion === "string" && this.tosVersion.startsWith("draft");
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

  // 文案放在 JS（單行 _t）：模板的多行文字節點會把換行與縮排一起抽進 msgid。
  get heroTitle() {
    return _t("Call 40+ AI models with your credit");
  }

  get heroSubtitle() {
    return _t("OpenAI-compatible API, plus a free trial of Qwen3-4B.");
  }

  get pricingFallbackText() {
    return _t("Prices cannot be shown right now. After activation, see them under Models & Pricing.");
  }

  get tosDraftText() {
    return _t("These terms are a draft and have not taken effect yet.");
  }

  get provisioningText() {
    return _t("Setting up your AI models account… (usually done within 1 minute)");
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

  get failedText() {
    return _t("Activation did not complete. Please try again later.");
  }

  get failedHint() {
    return _t("If this keeps happening, please contact support.");
  }

  get suspendedText() {
    const reason = llmSuspendReason(this.account && this.account.suspend_reason);
    return reason
      ? _t("The AI models service is suspended: %(reason)s", { reason })
      : _t("The AI models service is suspended.");
  }

  get balanceText() {
    return formatAiPoints(this.account ? this.account.balance_points : null);
  }

  get showZeroCreditBanner() {
    const account = this.account;
    return this.screen === "active" && !!account
      && account.balance_points === 0 && !account.overdraft_points;
  }

  get zeroCreditText() {
    return _t("Your AI credit is 0. You can use the free trial model first, or move credit from your points wallet to use paid models.");
  }

  get freeTrial() {
    return (this.llm.overview && this.llm.overview.free_trial) || {};
  }

  /** 今日次數：tracer 期間由 usage/recent 提供（state 的 today_count 在 LLM-10 之前是 null）。 */
  get freeTrialUsageText() {
    const usage = this.llm.usage;
    const count = usage && typeof usage.today_count === "number"
      ? usage.today_count : this.freeTrial.today_count;
    const limit = (usage && usage.daily_limit) || this.freeTrial.daily_limit || 200;
    return formatFreeTrialUsage(count, limit);
  }

  get freeTrialUsageSentence() {
    return _t("Free trial today: %(usage)s", { usage: this.freeTrialUsageText });
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

  get hasFreeTrialKey() {
    return !!this.freeTrial.key_id;
  }

  get canCreateFreeTrialKey() {
    return this.screen === "active" && !this.hasFreeTrialKey && !this.state.showReveal;
  }

  get freeTrialHint() {
    return _t("Free trial keys can only call Qwen3-4B: 10 calls per minute and about 200 calls per day.");
  }

  get freeTrialExistsText() {
    return _t("You already have a free trial key. Each account can have one.");
  }

  get keyRows() {
    const now = Date.now();
    return (this.llm.keys || []).map((key) => llmKeyRow(key, now));
  }

  get keysErrorText() {
    return this.llm.keysError ? llmErrorMessage({ code: this.llm.keysError }) : null;
  }

  get usageRows() {
    const items = (this.llm.usage && Array.isArray(this.llm.usage.items)) ? this.llm.usage.items : [];
    return items.map((item, index) => llmUsageRow(item, index));
  }

  get usageErrorText() {
    return this.llm.usageError ? llmErrorMessage({ code: this.llm.usageError }) : null;
  }

  get usageDataTimeText() {
    const asOf = this.llm.usage && this.llm.usage.data_as_of;
    return asOf ? _t("Data as of %(time)s (Taipei time)", { time: formatTaipeiTime(asOf) }) : null;
  }

  get emptyUsageText() {
    return _t("No free trial calls today yet.");
  }

  get emptyKeysText() {
    return _t("No API keys yet.");
  }

  /** 卡片堆疊（手機）時每一格前面的欄名（data-label，CSS ::before 顯示）。 */
  get labels() {
    return {
      name: _t("Name"),
      kind: _t("Key type"),
      key: _t("API key"),
      status: _t("Status"),
      expires: _t("Expires on"),
      time: _t("Time (Taipei time)"),
      model: _t("Model"),
      input: _t("Input tokens"),
      output: _t("Output tokens"),
      latency: _t("Latency"),
    };
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
    if (result.code === "llm_tos_outdated") {
      // 條款版本換了：取新版本、要求重新勾選。
      this.state.tosAccepted = false;
      await this.load();
    }
  }

  /**
   * 開通失敗的〔重試〕：重新讀一次帳戶狀態。⚠ 真正的重新開戶（`/api/llm/account/retry`，
   * 每小時 3 次）在 LLM-03；在那之前帳戶 `error` 是終態，這裡只能確認狀態並引導聯絡客服。
   */
  async retry() {
    if (this.state.retrying) return;
    this.state.retrying = true;
    this.state.actionError = null;
    await this.load();
    if (!this.alive) return;
    this.state.retrying = false;
    if (this.screen === "failed") {
      this.state.actionError = this.failedHint;
    }
  }

  async reload() {
    await this.load();
  }

  async copyBaseUrl() {
    const ok = this.baseUrl ? await copyText(this.baseUrl) : false;
    this.state.baseUrlCopied = ok;
    this.state.baseUrlCopyFailed = !ok;
  }

  openFreeTrialKey() {
    if (!this.canCreateFreeTrialKey) return;
    this.state.actionError = null;
    this.state.showReveal = true;
  }

  /** 對話框關閉：只拿到「有沒有建成」，金鑰內容不經過這裡。重新整理狀態與清單。 */
  onRevealClosed() {
    this.state.showReveal = false;
    this.load();
  }
}
