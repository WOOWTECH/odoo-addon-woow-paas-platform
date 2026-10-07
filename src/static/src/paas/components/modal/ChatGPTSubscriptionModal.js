/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";
import { copyText } from "../../utils/clipboard";
import {
  chatgptPlanLabel,
  describeAddedModels,
  formatCountdown,
} from "../chatgpt-subscription/chatgpt_subscription_helpers";

/** 預設輪詢間隔（chart 回的 pending.interval 缺席時）。 */
const DEFAULT_POLL_INTERVAL_S = 5;
/** 連續幾次「網路／逾時」類失敗（沒有 error_code）才放棄；有 error_code 的失敗立即停。 */
const MAX_TRANSIENT_POLL_FAILURES = 3;

/**
 * ChatGPTSubscriptionModal — 用 device code 連接 ChatGPT 訂閱（LiteLLM chart 0.2.0 起）。
 *
 * 步驟：
 *   1. consent：條款風險與前置設定（ChatGPT「設定 → 安全性」開啟裝置代碼登入），勾選同意
 *      才可「取得登入代碼」（action=start）。
 *   2. pending：大字顯示一次性代碼（可複製）、OpenAI 登入頁連結（新分頁）、倒數到期；
 *      **自己的計時器**每 `interval` 秒呼叫 `poll`（預設 5 秒），到期即停。頁面的 5／60 秒
 *      輪詢不碰 status——父頁的 `showChatgptSubscriptionModal` 讓 isModalOpen 為真，頁面輪詢暫停。
 *   3. connected：顯示帳號與方案，可「加入預設模型」（action=add_models）。
 *   expired／error：說明＋重新取得代碼。
 * 關閉或 unmount 一律清掉兩個計時器（輪詢與倒數）。代碼只活在畫面上：不 console.log、
 * 不進網址、不存瀏覽器儲存空間。
 *
 * Props:
 *   - workspaceId (Number)
 *   - serviceId (Number)
 *   - initialPending (Object, optional)：接續尚未完成的登入（卡片的「繼續登入」）——
 *     同一組代碼，略過同意步驟（同意已在拿代碼時給過）。
 *   - onClose (Function)：(changed: boolean) => void；changed＝這次有連上或加了模型。
 */
export class ChatGPTSubscriptionModal extends Component {
  static template = "woow_paas_platform.ChatGPTSubscriptionModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    workspaceId: { type: Number },
    serviceId: { type: Number },
    initialPending: { type: Object, optional: true },
    onClose: { type: Function },
  };

  setup() {
    this.state = useState({
      step: "consent",
      agreed: false,
      busy: false,
      pending: null,
      deadline: 0,
      now: Date.now(),
      account: null,
      error: null,
      copied: false,
      addingModels: false,
      modelsNotice: null,
      modelsNoticeIsError: false,
    });
    this.changed = false;
    this.closed = false;
    this.pollTimer = null;
    this.tickTimer = null;
    this.transientFailures = 0;

    onMounted(() => {
      const pending = this.props.initialPending;
      if (pending && pending.user_code && pending.expires_in > 0) {
        this._enterPending(pending);
      }
    });
    onWillUnmount(() => {
      this.closed = true;
      this._stopTimers();
    });
  }

  // ---- 文案（_t 單行；模板多行文字的 msgid 會帶換行與縮排，翻譯對不上） ----

  get consentText() {
    return _t("ChatGPT Plus or Pro is required. First, in ChatGPT go to Settings → Security and turn on device code sign-in (Codex). This feature borrows the sign-in method of Codex; it is not an official OpenAI integration for third parties. Usage counts against your ChatGPT subscription, and you accept any risk to your account. The code is for your own use only — do not give it to anyone.");
  }

  get consentLabel() {
    return _t("I have read the above and agree.");
  }

  get pendingInstructions() {
    return _t("Open the OpenAI sign-in page, sign in with your ChatGPT account and enter this code. This window updates by itself once you finish.");
  }

  get expiredText() {
    return _t("The code has expired. Get a new one to try again.");
  }

  get connectedText() {
    return _t("Connected. Add the default models to start using them, or close this window.");
  }

  get title() {
    return _t("Connect ChatGPT subscription");
  }

  // ---- 顯示 ----

  get countdown() {
    return formatCountdown(this.state.deadline - this.state.now);
  }

  get planLabel() {
    return chatgptPlanLabel(this.state.account?.plan_type);
  }

  get verificationUrl() {
    return this.state.pending?.verification_url || "https://auth.openai.com/codex/device";
  }

  // ---- 計時器 ----

  _stopTimers() {
    if (this.pollTimer) {
      clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
    if (this.tickTimer) {
      clearInterval(this.tickTimer);
      this.tickTimer = null;
    }
  }

  _pollIntervalMs() {
    const interval = Number(this.state.pending?.interval) || DEFAULT_POLL_INTERVAL_S;
    return Math.max(3, interval) * 1000;
  }

  _schedulePoll() {
    if (this.closed) {
      return;
    }
    if (this.pollTimer) {
      clearTimeout(this.pollTimer);
    }
    this.pollTimer = setTimeout(() => {
      this.pollTimer = null;
      this.poll();
    }, this._pollIntervalMs());
  }

  _enterPending(pending) {
    this._stopTimers();
    this.state.pending = pending;
    this.state.deadline = Date.now() + Math.max(0, Number(pending.expires_in) || 0) * 1000;
    this.state.now = Date.now();
    this.state.copied = false;
    this.state.error = null;
    this.state.step = "pending";
    this.transientFailures = 0;
    this.tickTimer = setInterval(() => {
      this.state.now = Date.now();
      if (this.state.now >= this.state.deadline) {
        this._expire();
      }
    }, 1000);
    this._schedulePoll();
  }

  _expire() {
    this._stopTimers();
    this.state.step = "expired";
  }

  _fail(message) {
    this._stopTimers();
    this.state.error = message || _t("Sign-in failed. Start again.");
    this.state.step = "error";
  }

  // ---- 動作 ----

  onAgreeChange(ev) {
    this.state.agreed = !!ev.target.checked;
  }

  async getCode() {
    if (this.state.busy || (this.state.step === "consent" && !this.state.agreed)) {
      return;
    }
    this._stopTimers();
    this.state.busy = true;
    this.state.error = null;
    const result = await cloudService.chatgptSubscription(
      this.props.workspaceId, this.props.serviceId, "start"
    );
    if (this.closed) {
      return;
    }
    this.state.busy = false;
    const pending = result.success ? result.data?.pending : null;
    if (pending && pending.user_code) {
      this._enterPending(pending);
    } else {
      this._fail(result.error || _t("Could not get a sign-in code from OpenAI. Try again later."));
    }
  }

  async poll() {
    if (this.closed || this.state.step !== "pending") {
      return;
    }
    if (Date.now() >= this.state.deadline) {
      this._expire();
      return;
    }
    const result = await cloudService.chatgptSubscription(
      this.props.workspaceId, this.props.serviceId, "poll"
    );
    if (this.closed || this.state.step !== "pending") {
      return;
    }
    if (!result.success) {
      if (!result.error_code && this.transientFailures + 1 < MAX_TRANSIENT_POLL_FAILURES) {
        // 網路抖動、單次逾時：再等一輪（代碼仍有效）。
        this.transientFailures += 1;
        this._schedulePoll();
        return;
      }
      this._fail(result.error);
      return;
    }
    this.transientFailures = 0;
    const data = result.data || {};
    if (data.state === "connected" || data.connected === true) {
      this._stopTimers();
      this.changed = true;
      this.state.account = { email: data.email, plan_type: data.plan_type };
      this.state.step = "connected";
    } else if (data.state === "expired") {
      this._expire();
    } else if (data.state === "idle") {
      // 服務端已沒有這次登入（例如服務重啟過）：要重新取得代碼。
      this._expire();
    } else {
      if (data.pending && data.pending.interval) {
        this.state.pending = { ...this.state.pending, interval: data.pending.interval };
      }
      this._schedulePoll();
    }
  }

  async copyCode() {
    const code = this.state.pending?.user_code;
    if (code && (await copyText(code)) && !this.closed) {
      this.state.copied = true;
    }
  }

  async addDefaultModels() {
    if (this.state.addingModels) {
      return;
    }
    this.state.addingModels = true;
    this.state.modelsNotice = null;
    const result = await cloudService.chatgptSubscription(
      this.props.workspaceId, this.props.serviceId, "add_models"
    );
    if (this.closed) {
      return;
    }
    this.state.addingModels = false;
    this.changed = true;
    if (result.success) {
      this.state.modelsNotice = describeAddedModels(result.data);
      this.state.modelsNoticeIsError = false;
    } else {
      this.state.modelsNotice = result.error || _t("Could not add the models.");
      this.state.modelsNoticeIsError = true;
    }
  }

  close() {
    if (this.state.busy || this.state.addingModels) {
      return;
    }
    this.closed = true;
    this._stopTimers();
    this.props.onClose(this.changed);
  }

  onBackdropClick(ev) {
    // 等待登入時點到背景不關閉（關掉會停止輪詢；卡片雖可「繼續登入」，誤觸仍很惱人）。
    if (ev.target === ev.currentTarget && this.state.step !== "pending") {
      this.close();
    }
  }
}
