/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { dataRetrievalAction } from "../../services/data_retrieval";
import { formatUserDateTime } from "../../utils/datetime_helpers";

/** 開好之後每幾秒問一次就緒（pod 掛磁碟、拉 image 通常不到一分鐘）。 */
const READY_POLL_MS = 5000;
/** 最多問多久（之後只顯示「仍在準備」，不再自動輪詢）。 */
const READY_POLL_LIMIT_MS = 180000;

/**
 * DataRetrievalModal（#1540）
 *
 * 停權中的服務／已刪除服務的保留資料共用的「取回資料」視窗：
 *   1. 讀狀態（status）：已有開著的入口 → 顯示網址、到期、關閉鈕；可以開 → 說明＋確認；
 *      都不行 → 說明為什麼。
 *   2. 開（open）：顯示網址／帳號／**一次性密碼**（只此一次，平台不存）＋整包下載指令，
 *      並輪詢到入口就緒。
 *   3. 關（close）：手動提早關閉。
 *
 * 能不能按、按了會不會被拒，判準都在後端（payload 的 data_retrieval.available／session）；
 * 這裡不自建判準。
 *
 * Props:
 *   - endpoint (String): 兩種入口之一的 data-retrieval 端點
 *   - serviceName (String)
 *   - deleted (Boolean, optional): 已刪除服務（文案不同）
 *   - onClose (Function)
 *   - onChanged (Function, optional): 開或關成功後呼叫（讓頁面重讀）
 */
export class DataRetrievalModal extends Component {
  static template = "woow_paas_platform.DataRetrievalModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    endpoint: { type: String },
    serviceName: { type: String },
    deleted: { type: Boolean, optional: true },
    onClose: { type: Function },
    onChanged: { type: Function, optional: true },
  };

  setup() {
    this.state = useState({
      loading: true,
      busy: false,
      error: null,
      view: null,
      credentials: null,
      copiedKey: null,
    });
    this.pollTimer = null;
    this.pollStartedAt = null;
    onMounted(() => this.loadStatus());
    onWillUnmount(() => this.stopPolling());
  }

  // ── 文案（放 JS 的單行 _t：模板的多行文字節點會把換行與縮排一起抽成 msgid）──

  get title() {
    return _t("Retrieve Data");
  }

  get introText() {
    return this.props.deleted
      ? _t("This service was deleted, but its disk is still retained. You can open a temporary, read-only download link for its files.")
      : _t("This service is suspended. You can open a temporary, read-only download link for its files. The service itself stays stopped.");
  }

  get notes() {
    return [
      _t("The link closes automatically after 72 hours (or earlier, when the retained disk is reclaimed). You can close it sooner yourself."),
      _t("Files can only be viewed and downloaded; nothing can be changed or deleted."),
      _t("Each user's files are in their own folder. Calendars, contacts and other app data are not included."),
      _t("Anyone with the address and password can download the files. The password is shown only once."),
    ];
  }

  get unavailableText() {
    return _t("Data retrieval is available while the service is suspended, or after it was deleted until its disk is reclaimed.");
  }

  get saveNowText() {
    return _t("Save these now. The password is shown only once and cannot be displayed again.");
  }

  get activeText() {
    return _t("A download link for this service is open. The password was shown only once when it was opened; if you lost it, close the link and open a new one.");
  }

  get wgetHint() {
    return _t("To download everything at once (e.g. on a computer with wget):");
  }

  get readinessText() {
    const status = this.session?.live_status;
    if (status === "active") {
      return _t("Ready. Open the address and sign in with the username and password.");
    }
    if (status === "stopped") {
      return _t("The link has stopped. Close it and open a new one if you still need the files.");
    }
    return _t("Preparing the download link (usually under a minute)...");
  }

  // ── 資料 ─────────────────────────────────────────────────────────────────

  get session() {
    return this.state.view?.session || null;
  }

  get canOpen() {
    return !!this.state.view?.available && !this.session;
  }

  get credentials() {
    return this.state.credentials;
  }

  get availableUntilLabel() {
    return _t("Available until");
  }

  get addressLabel() {
    return _t("Download address");
  }

  get sessionFacts() {
    const s = this.session;
    if (!s) {
      return [];
    }
    const facts = [];
    if (s.username) {
      facts.push({ key: "username", label: _t("Username"), value: s.username });
    }
    if (s.expires_at) {
      facts.push({ key: "expires", label: _t("Available until"), value: this.formatTime(s.expires_at) });
    }
    if (s.opened_by) {
      facts.push({ key: "opened_by", label: _t("Opened by"), value: s.opened_by });
    }
    return facts;
  }

  get credentialRows() {
    const c = this.state.credentials;
    if (!c) {
      return [];
    }
    return [
      { key: "url", label: _t("Download address"), value: c.url || c.hostname || "" },
      { key: "username", label: _t("Username"), value: c.username || "" },
      { key: "password", label: _t("Password"), value: c.password || "" },
    ];
  }

  get wgetCommand() {
    const url = this.state.credentials?.url || this.session?.url;
    const user = this.state.credentials?.username || this.session?.username || "download";
    return url ? `wget -r -np -nH --user=${user} --ask-password ${url}` : "";
  }

  formatTime(value) {
    return formatUserDateTime(value, {}, "");
  }

  // ── 動作 ─────────────────────────────────────────────────────────────────

  async loadStatus() {
    const result = await dataRetrievalAction(this.props.endpoint, "status");
    this.state.loading = false;
    if (result.success) {
      this.state.view = result.data;
      this.state.error = null;
      if (this.session && this.session.live_status !== "active") {
        this.startPolling();
      }
    } else {
      this.state.error = result.error || _t("Could not load the data retrieval status.");
    }
  }

  async onOpen() {
    if (this.state.busy) {
      return;
    }
    this.state.busy = true;
    this.state.error = null;
    const result = await dataRetrievalAction(this.props.endpoint, "open");
    this.state.busy = false;
    if (!result.success) {
      this.state.error = result.error || _t("Could not open the data retrieval link. Please try again later.");
      return;
    }
    this.state.credentials = result.data.credentials || null;
    this.state.view = result.data;
    this.props.onChanged?.();
    this.startPolling();
  }

  async onCloseLink() {
    if (this.state.busy) {
      return;
    }
    this.state.busy = true;
    this.state.error = null;
    const result = await dataRetrievalAction(this.props.endpoint, "close");
    this.state.busy = false;
    if (!result.success) {
      this.state.error = result.error || _t("Could not close the data retrieval link. Please try again later.");
      return;
    }
    this.stopPolling();
    this.state.credentials = null;
    this.state.view = result.data;
    this.props.onChanged?.();
  }

  startPolling() {
    this.stopPolling();
    this.pollStartedAt = Date.now();
    this.pollTimer = setInterval(async () => {
      if (Date.now() - this.pollStartedAt > READY_POLL_LIMIT_MS) {
        this.stopPolling();
        return;
      }
      const result = await dataRetrievalAction(this.props.endpoint, "status");
      if (!result.success) {
        return;
      }
      this.state.view = result.data;
      const status = result.data.session?.live_status;
      if (!result.data.session || status === "active" || status === "stopped") {
        this.stopPolling();
      }
    }, READY_POLL_MS);
  }

  stopPolling() {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  async copy(key, value) {
    await navigator.clipboard.writeText(value);
    this.state.copiedKey = key;
  }

  onBackdropClick(ev) {
    // 一次性密碼還在畫面上時，點背景不關（避免誤觸丟掉唯一一次的密碼）。
    if (ev.target === ev.currentTarget && !this.state.busy && !this.state.credentials) {
      this.props.onClose();
    }
  }
}
