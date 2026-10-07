/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../card/WoowCard";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";
import {
  chatgptPlanLabel,
  describeAddedModels,
  formatEpochSeconds,
  formatSubscriptionUntil,
} from "./chatgpt_subscription_helpers";

/**
 * 重試也不會好、要租戶先做別的事的狀態錯誤（重新部署到新版 chart、等平台更新）：
 * 卡片只顯示說明，不給「重試」。
 */
const TERMINAL_STATUS_ERRORS = ["unsupported_chart", "operator_unsupported", "not_supported"];

/**
 * ChatGPTSubscriptionCard — 服務頁的「ChatGPT 訂閱」卡片（LiteLLM chart 0.2.0 起）。
 *
 * 顯示判準在父頁（ServiceDetailPage.showChatgptSubscriptionCard：鏡射
 * template.chatgpt_subscription_supported、檢視者是 owner／admin、服務 running），
 * 本元件只管內容：
 * - 掛載時呼叫**一次** `status`（不跟著頁面的 5／60 秒輪詢；父頁用 t-key 重掛來刷新）。
 * - 已連接：帳號、方案、chatgpt/ 模型、登入憑證的自動更新時間；加入預設模型、測試連線、
 *   重新連接、中斷連接（要先確認）。
 * - 未連接：說明＋「連接」；有進行中的登入（使用者關了視窗）時提供「繼續登入」。
 *
 * Props:
 *   - workspaceId (Number)
 *   - serviceId (Number)
 *   - onConnect (Function): (pending|null) => void，父頁打開連接視窗；pending 非空＝接續
 *     尚未完成的登入（同一組代碼）。
 */
export class ChatGPTSubscriptionCard extends Component {
  static template = "woow_paas_platform.ChatGPTSubscriptionCard";
  static components = { WoowCard, WoowButton, WoowIcon };
  static props = {
    workspaceId: { type: Number },
    serviceId: { type: Number },
    onConnect: { type: Function },
  };

  setup() {
    this.state = useState({
      loading: true,
      status: null,
      error: null,
      errorCode: null,
      busy: null,
      notice: null,
      noticeIsError: false,
      confirmDisconnect: false,
    });
    this.alive = true;
    onMounted(() => this.loadStatus());
    onWillUnmount(() => {
      this.alive = false;
    });
  }

  async loadStatus({ silent = false } = {}) {
    if (!silent) {
      this.state.loading = true;
      this.state.error = null;
      this.state.errorCode = null;
    }
    const result = await cloudService.chatgptSubscription(
      this.props.workspaceId, this.props.serviceId, "status"
    );
    if (!this.alive) {
      return;
    }
    if (result.success) {
      this.state.status = result.data;
      this.state.error = null;
      this.state.errorCode = null;
    } else if (!silent) {
      this.state.status = null;
      this.state.error = result.error || _t("Could not check the ChatGPT subscription.");
      this.state.errorCode = result.error_code || null;
    }
    this.state.loading = false;
  }

  // ---- 顯示 ----

  get status() {
    return this.state.status || {};
  }

  get connected() {
    return this.status.connected === true;
  }

  get canRetry() {
    return !TERMINAL_STATUS_ERRORS.includes(this.state.errorCode);
  }

  get planLabel() {
    return chatgptPlanLabel(this.status.plan_type);
  }

  /** null＝proxy 還沒起來、查不到（與「查了但沒有」的空陣列不同）。 */
  get modelsKnown() {
    return Array.isArray(this.status.models);
  }

  get models() {
    return this.status.models || [];
  }

  get tokenRenewal() {
    return formatEpochSeconds(this.status.access_token_expires_at);
  }

  get subscriptionUntil() {
    return formatSubscriptionUntil(this.status.subscription_active_until);
  }

  /** 使用者關掉視窗、但代碼還沒過期的登入：可以接續，不必重拿代碼。 */
  get pendingSignIn() {
    const pending = this.status.pending;
    return pending && pending.user_code && pending.expires_in > 0 ? pending : null;
  }

  get pendingHint() {
    const minutes = Math.max(1, Math.ceil((this.pendingSignIn?.expires_in || 0) / 60));
    return _t("A sign-in is waiting for you. Its code expires in about %s min.", minutes);
  }

  get notSavedHint() {
    return _t("This sign-in has not been saved to the service's database yet; it is retried each time this card checks the status. If the service restarts before that, you will need to connect again.");
  }

  get introText() {
    return _t("Use your ChatGPT Plus or Pro subscription as a model provider in this gateway (model names start with chatgpt/). You sign in on OpenAI's own page with a one-time code; your ChatGPT password never passes through the platform.");
  }

  get disconnectWarning() {
    return _t("Disconnect ChatGPT? The chatgpt/ models stop working until you connect again. The models stay in the list, and nothing else in the service changes.");
  }

  get isBusy() {
    return !!this.state.busy;
  }

  // ---- 動作 ----

  connect() {
    this.state.notice = null;
    this.props.onConnect(null);
  }

  continueSignIn() {
    this.state.notice = null;
    this.props.onConnect(this.pendingSignIn);
  }

  _setNotice(text, isError = false) {
    this.state.notice = text;
    this.state.noticeIsError = isError;
  }

  async _run(action, params = {}) {
    if (this.state.busy) {
      return null;
    }
    this.state.busy = action;
    this.state.notice = null;
    try {
      return await cloudService.chatgptSubscription(
        this.props.workspaceId, this.props.serviceId, action, params
      );
    } finally {
      if (this.alive) {
        this.state.busy = null;
      }
    }
  }

  async addDefaultModels() {
    const result = await this._run("add_models");
    if (!result || !this.alive) {
      return;
    }
    if (result.success) {
      this._setNotice(describeAddedModels(result.data));
    } else {
      this._setNotice(result.error || _t("Could not add the models."), true);
    }
    await this.loadStatus({ silent: true });
  }

  async testConnection() {
    const result = await this._run("test");
    if (!result || !this.alive) {
      return;
    }
    if (result.success) {
      const data = result.data || {};
      this._setNotice(_t("%s answered in %s seconds.", data.model || "chatgpt", data.seconds ?? "?"));
    } else {
      this._setNotice(result.error || _t("The test request failed."), true);
    }
  }

  askDisconnect() {
    this.state.notice = null;
    this.state.confirmDisconnect = true;
  }

  cancelDisconnect() {
    this.state.confirmDisconnect = false;
  }

  async disconnect() {
    const result = await this._run("disconnect");
    if (!result || !this.alive) {
      return;
    }
    this.state.confirmDisconnect = false;
    if (result.success) {
      this._setNotice(_t("ChatGPT has been disconnected."));
      await this.loadStatus({ silent: true });
    } else {
      this._setNotice(result.error || _t("Could not disconnect ChatGPT."), true);
    }
  }
}
