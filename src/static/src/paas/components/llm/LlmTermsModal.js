/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { safeHtml } from "../../services/html_sanitize";
import { llmService } from "../../services/llm_service";
import { llmErrorMessage } from "../../services/llm_errors";
import { inertOutside, trapTab } from "../../utils/modal_focus";

/**
 * 條款內容裡的連結點下去要怎麼處理（純函式，tests/ui/test_llm_terms.mjs 直接驗）。
 *
 * - `http(s)://` ⇒ `"new_tab"`：一律開新分頁（`noopener,noreferrer`）。條款本體有外部連結（第 12 條的
 *   原始碼連結），在對話框裡照預設點下去，`/woow` 會整頁跳走、離開「尚未開通」畫面與勾選流程（LLM-05
 *   第二輪審查）——不管後台存的 HTML 有沒有 `target="_blank"` 都一樣處理。
 * - `mailto:` ⇒ `"default"`：交給瀏覽器（開郵件程式，不離開頁面）。
 * - 其他（`#…`、相對路徑、看不懂的 scheme）⇒ `"block"`：`#…` 會被 /woow 的 hash router 當成換頁。
 */
export function termsLinkAction(href) {
  const text = typeof href === "string" ? href.trim() : "";
  if (/^https?:\/\//i.test(text)) return "new_tab";
  if (/^mailto:/i.test(text)) return "default";
  return "block";
}

/**
 * 《AI 模型服務條款》對話框（DESIGN §6.4；票 LLM-04）。
 *
 * - 內容來自 `/api/llm/terms`（published 版）。`content_html` 是後台存的 HTML（伺服器端寫入時已
 *   sanitize），**顯示前再經 `safeHtml()`**（DOMPurify；`<script>`、事件屬性都清掉）才 `t-out`。
 *   標題與條文是資料值，不翻譯（不經 `_t`）。
 * - `mode="read"`：「尚未開通」畫面勾選框旁的連結，只能閱讀與關閉；勾選與〔開通〕在頁面上。
 * - `mode="accept"`：總覽「條款已更新」橫幅的〔查看並同意〕。〔我已閱讀並同意〕送
 *   `/api/llm/account/accept_terms`，帶的是**對話框裡顯示的那一版**：送出前又改版了，後端回
 *   `llm_tos_outdated`，這裡重新讀條款、顯示新版，使用者要再按一次。
 *   `blocking`（頁面的 `tosBlocking`＝`/api/llm/state` 的 `tos.blocking`）決定底部的提示：會擋才說
 *   「同意後即可再建立金鑰…」；`requires_reaccept=false` 的改版什麼都沒擋，提示也不能暗示有東西被擋
 *   （畫面與 gate 要一致；LLM-05 審查）。
 * - 關閉：`onClose({ accepted, version })`（`version`＝對話框讀到的版本，頁面據此判斷畫面上的版本
 *   是否已經過期）。Esc、背景、右上角都能關（同意送出中除外）；Tab 只在對話框裡循環、背景 inert。
 *   這裡沒有一次性金鑰對話框的 history 哨兵：關掉條款不會遺失任何東西。
 */
export class LlmTermsModal extends Component {
  static template = "woow_paas_platform.LlmTermsModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    mode: { type: String, validate: (mode) => mode === "read" || mode === "accept" },
    /** accept 模式：條款 gate 目前會不會擋（建金鑰、轉換、啟用金鑰）。沒給＝不擋。 */
    blocking: { type: Boolean, optional: true },
    onClose: { type: Function },
  };

  setup() {
    this.state = useState({
      phase: "loading",
      version: null,
      title: null,
      isDraft: false,
      content: "",
      loadError: null,
      accepting: false,
      actionError: null,
    });
    this.backdropRef = useRef("backdrop");
    this.dialogRef = useRef("dialog");
    this.contentRef = useRef("content");
    this.destroyed = false;
    this.closed = false;
    this.restoreInert = null;
    /** 每次讀取的序號：較早送出、較晚回來的回應直接丟掉。 */
    this.loadSeq = 0;

    this.onKeydown = (ev) => {
      if (ev.key === "Escape") {
        this.requestClose();
      } else if (ev.key === "Tab") {
        trapTab(ev, this.dialogRef.el);
      }
    };

    onMounted(() => {
      document.addEventListener("keydown", this.onKeydown);
      if (this.backdropRef.el) {
        this.restoreInert = inertOutside(this.backdropRef.el);
      }
      if (this.dialogRef.el) {
        this.dialogRef.el.focus();
      }
      this.load();
    });
    onWillUnmount(() => {
      this.destroyed = true;
      this.teardown();
    });
  }

  async load() {
    const seq = ++this.loadSeq;
    this.state.phase = "loading";
    this.state.loadError = null;
    let result;
    try {
      result = await llmService.fetchTerms();
    } catch {
      result = { success: false, code: "llm_internal_error" };
    }
    if (this.destroyed || this.closed || seq !== this.loadSeq) return;
    const data = result && result.success ? result.data : null;
    if (data && typeof data.version === "string" && data.version) {
      this.state.version = data.version;
      this.state.title = typeof data.title === "string" && data.title ? data.title : null;
      this.state.isDraft = data.is_draft === true;
      this.state.content = safeHtml(typeof data.content_html === "string" ? data.content_html : "");
      this.state.phase = "shown";
      return;
    }
    this.state.loadError = llmErrorMessage(result && !result.success ? result : { code: "llm_tos_unavailable" });
    this.state.phase = "error";
  }

  // 文案放在 JS（單行 _t）：模板的多行文字節點會把換行與縮排一起抽進 msgid。
  /** 後台的標題（資料值）；還沒讀到或沒有標題時用固定的名稱。 */
  get heading() {
    return this.state.title || _t("AI Models Terms of Service");
  }

  get versionText() {
    return this.state.version ? _t("Version: %(version)s", { version: this.state.version }) : "";
  }

  get draftText() {
    return _t("These terms are a draft and have not taken effect yet.");
  }

  get loadingText() {
    return _t("Loading the terms…");
  }

  get acceptHint() {
    return this.props.blocking === true
      ? _t("Accepting lets you create keys, convert points and re-enable keys again. Existing keys keep working either way.")
      : _t("This update does not affect anything you can do now. Existing keys keep working.");
  }

  get closeLabel() {
    return _t("Close");
  }

  get isAcceptMode() {
    return this.props.mode === "accept";
  }

  get canAccept() {
    return this.isAcceptMode && this.state.phase === "shown" && !!this.state.version
      && !this.state.accepting;
  }

  async accept() {
    if (!this.canAccept) return;
    const version = this.state.version;
    this.state.accepting = true;
    this.state.actionError = null;
    let result;
    try {
      result = await llmService.acceptTerms(version);
    } catch {
      result = { success: false, code: "llm_internal_error" };
    }
    if (this.destroyed || this.closed) return;
    this.state.accepting = false;
    if (result && result.success) {
      this.close(true);
      return;
    }
    this.state.actionError = llmErrorMessage(result);
    if (result && result.code === "llm_tos_outdated") {
      // 對話框開著的時候又改版了：換成新版，要使用者看過再按一次。
      await this.load();
    }
  }

  onContentClick(ev) {
    const target = ev.target;
    const link = target && typeof target.closest === "function" ? target.closest("a[href]") : null;
    const content = this.contentRef.el;
    if (!link || !content || !content.contains(link)) return;
    const action = termsLinkAction(link.getAttribute("href"));
    if (action === "default") return;
    ev.preventDefault();
    if (action === "new_tab") {
      window.open(link.href, "_blank", "noopener,noreferrer");
    }
  }

  requestClose() {
    if (this.closed || this.state.accepting) return;
    this.close(false);
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget) {
      this.requestClose();
    }
  }

  close(accepted) {
    if (this.closed) return;
    this.closed = true;
    // 先拆守衛（背景解除 inert），頁面在 onClose 裡才能把焦點還回去。
    this.teardown();
    this.props.onClose({ accepted: !!accepted, version: this.state.version });
  }

  teardown() {
    document.removeEventListener("keydown", this.onKeydown);
    if (this.restoreInert) {
      this.restoreInert();
      this.restoreInert = null;
    }
  }
}
