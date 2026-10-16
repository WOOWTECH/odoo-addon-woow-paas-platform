/** @odoo-module **/

import { Component, onMounted, onPatched, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { router } from "../../core/router";
import { copyText } from "../../utils/clipboard";
import { inertOutside, trapTab } from "../../utils/modal_focus";
import { llmService, newClientRequestId } from "../../services/llm_service";
import { LLM_KEY_CREATE_INTERRUPTED, llmErrorMessage } from "../../services/llm_errors";

/** 免費試用金鑰的範例模型（DESIGN §6.3.6）；兩個 Qwen 都能叫，教學用 cluster1 這個。 */
export const FREE_TRIAL_EXAMPLE_MODEL = "qwen3-4b-cluster1";

/**
 * 〔複製為環境變數〕的內容（DESIGN §6.3.2 的兩個變數）。
 *
 * - **一定要有 Base URL**，沒有就回 `null`（畫面不給這顆鈕）：OpenAI SDK、n8n、Open WebUI
 *   等工具沒設 `OPENAI_BASE_URL`（或設成空字串）時會連預設的 `https://api.openai.com/v1`，
 *   照著貼就等於把我們的金鑰當 Bearer 送給第三方。
 * - 一行一個、前面加 `export`：貼進 shell 之後啟動的工具才讀得到（不加 export 只是 shell 變數）；
 *   python-dotenv、Node dotenv 也接受 `export KEY=value` 的寫法，所以貼進 `.env` 一樣能用。
 *   兩個值都只有英數與 URL 字元，不需要加引號。
 */
export function buildEnvVarsText(key, baseUrl) {
  if (typeof key !== "string" || !key || typeof baseUrl !== "string" || !baseUrl) {
    return null;
  }
  return ["export OPENAI_API_KEY=" + key, "export OPENAI_BASE_URL=" + baseUrl].join("\n");
}

/** 這兩個階段關掉或離開頁面，明文就再也拿不到（建立中：請求照樣跑完、後端標成 active）。 */
const GUARDED_PHASES = new Set(["creating", "shown"]);

/**
 * 一次性金鑰對話框（DESIGN §5.4、§6.3.2；參考 OneTimeCredentialsModal）。
 *
 * **建立金鑰的請求由本元件自己送**，明文只放在本元件的 state：不經頁面 state、不進
 * llm_service 的 store；關閉（或元件卸載）時清掉。流程：
 *
 *   creating（送出中，不能關：關了就再也拿不到明文，而免費試用每帳號只有一把）
 *   → shown（顯示一次；要勾「我已妥善保存此金鑰」才能〔完成〕；點背景或 Esc 先問一次）
 *   → error（沒建成或沒拿到回應：顯示 llm_errors.js 的句子，可直接關閉）
 *
 * creating／shown 期間，關閉鈕、Esc、背景之外的離開路徑也要擋（Root 依 router.current 換分支，
 * 一換頁整串元件就卸載）：
 *
 * - 瀏覽器上一頁、Android 返回鍵、iOS 邊緣滑回：打開時 `history.pushState` 一筆同網址的哨兵，
 *   返回鍵彈掉它（網址沒變、router 不換頁）時補回去並走 requestClose（顯示中＝跳關閉確認）；
 * - 側欄、Header 等改 hash 的換頁（瀏覽器對 hash 換頁也會先發 popstate，網址已經不同）：交給 router
 *   的離開守衛（`router.setLeaveGuard`）把 hash 還原，還原之後再補一筆哨兵——不能在網址還是
 *   新 hash 時補，否則哨兵帶著新 hash，關閉時收回哨兵就會落到使用者剛才想去的那一頁；
 * - 鍵盤：Tab 只在對話框內循環、背景設 inert；
 * - 重新整理、關分頁：`beforeunload` 提醒。
 *
 * 不自動寫入剪貼簿；複製走 utils/clipboard.js 的 copyText()，失敗要有提示（#1310）。
 */
export class LlmKeyRevealModal extends Component {
  static template = "woow_paas_platform.LlmKeyRevealModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    // 本票只有免費試用（付費金鑰在 LLM-07，會帶建立表單的欄位進來）。
    kind: { type: String, validate: (kind) => kind === "free_trial" },
    // `/api/llm/state` 的 public_base_url；建金鑰的回應有帶 base_url 時以回應為準。
    baseUrl: { type: String, optional: true },
    // 關閉後通知頁面：{ created: boolean }（頁面據此重新整理清單；不帶任何金鑰內容）。
    onClose: { type: Function },
  };

  setup() {
    this.state = useState({
      phase: "creating",
      key: null,
      baseUrl: this.props.baseUrl || null,
      maskedKey: null,
      errorMessage: null,
      saved: false,
      copied: null,
      copyFailed: false,
      confirmClose: false,
    });
    // 打開時產生一次（DESIGN §3.2）：同一個對話框的請求只會送一次，id 不重用。
    this.clientRequestId = newClientRequestId();
    this.backdropRef = useRef("backdrop");
    this.dialogRef = useRef("dialog");
    /** 元件已卸載：create() 的回應晚到時直接丟掉，不寫進已銷毀元件的 state。 */
    this.destroyed = false;
    /** 已經呼叫過 close()（守衛全部拆掉、明文已清）。 */
    this.closed = false;
    /** 哨兵還在 history 的最上面（還沒被返回鍵彈掉、也還沒由 close() 收回）。 */
    this.sentinelPushed = false;
    /** 對話框打開時的網址（哨兵與還原都以它為準）。 */
    this.guardedHref = null;
    this.focusedPhase = null;
    this.focusedConfirm = false;
    this.restoreInert = null;
    this.removeLeaveGuard = null;

    this.onKeydown = (ev) => {
      if (ev.key === "Escape") {
        this.requestClose();
      } else if (ev.key === "Tab") {
        trapTab(ev, this.dialogRef.el);
      }
    };
    this.onPopState = () => {
      // 不論是哪一種，現在最上面那筆是不是自己的哨兵，看 state 才準。
      this.sentinelPushed = this.isOwnSentinel(window.history.state);
      if (this.closed) return;
      if (window.location.href !== this.guardedHref) {
        // hash 換頁（連結、location.hash、跨 hash 的上一頁）：隨後的 hashchange 由 router 的
        // 離開守衛還原 hash 並 requestClose，還原後才補哨兵（見 rearmSentinel）。
        return;
      }
      // 返回鍵彈掉了哨兵（網址沒變，所以 router 不會換頁）。
      if (this.guarding && !this.sentinelPushed) {
        this.pushSentinel();
      }
      this.requestClose();
    };
    this.onBeforeUnload = (ev) => {
      if (this.guarding) {
        ev.preventDefault();
        ev.returnValue = "";
      }
    };

    onMounted(() => {
      document.addEventListener("keydown", this.onKeydown);
      window.addEventListener("popstate", this.onPopState);
      window.addEventListener("beforeunload", this.onBeforeUnload);
      this.removeLeaveGuard = router.setLeaveGuard(() => {
        if (!this.guarding) return false;
        this.requestClose();
        // router 在守衛回 true 之後（同一個 handler 裡）才用 replaceState 還原 hash；microtask
        // 一定排在它後面，那時網址已經回到對話框這一頁。
        Promise.resolve().then(() => this.rearmSentinel());
        return true;
      });
      this.guardedHref = window.location.href;
      this.pushSentinel();
      if (this.backdropRef.el) {
        this.restoreInert = inertOutside(this.backdropRef.el);
      }
      this.focusForPhase();
      this.create();
    });
    onPatched(() => this.focusForPhase());
    onWillUnmount(() => {
      this.destroyed = true;
      this.teardown();
      this.forgetKey();
    });
  }

  /** creating 與 shown：明文還沒到手或正在顯示，任何離開路徑都要先擋下來。 */
  get guarding() {
    return !this.closed && !this.destroyed && GUARDED_PHASES.has(this.state.phase);
  }

  async create() {
    let result;
    try {
      result = await llmService.createFreeTrialKey(this.clientRequestId);
    } catch {
      // llm_service 的契約是不丟例外；萬一丟了，當成「沒拿到回應」。
      result = { success: false, code: LLM_KEY_CREATE_INTERRUPTED };
    }
    if (this.destroyed || this.closed) {
      // 已經卸載（例如重新整理途中）：丟掉回應，明文不寫進 state。
      return;
    }
    try {
      const data = result && result.success ? result.data : null;
      if (data && typeof data.key === "string" && data.key.startsWith("sk-")) {
        this.state.key = data.key;
        this.state.maskedKey = typeof data.masked_key === "string" ? data.masked_key : null;
        if (typeof data.base_url === "string" && data.base_url) {
          this.state.baseUrl = data.base_url;
        }
        this.state.phase = "shown";
        return;
      }
      // 成功卻沒有金鑰（不該發生）：當成「金鑰可能已建好卻沒顯示」。
      this.state.errorMessage = llmErrorMessage(result && !result.success
        ? result : { code: LLM_KEY_CREATE_INTERRUPTED });
      this.state.phase = "error";
    } catch {
      // 後半段任何一步丟例外：一律進 error（creating 沒有關閉鈕，停在那裡就只能重新整理）。
      this.state.key = null;
      this.state.errorMessage = llmErrorMessage({ code: LLM_KEY_CREATE_INTERRUPTED });
      this.state.phase = "error";
    }
  }

  // 文案放在 JS（單行 _t）：模板的多行文字節點會把換行與縮排一起抽進 msgid。
  /** 標題跟著階段走：建立中、沒建成時都不能說「請立即複製」（使用者會以為有東西可複製）。 */
  get title() {
    if (this.state.phase === "creating") {
      return _t("Creating your free trial key");
    }
    if (this.state.phase === "error") {
      return _t("The key was not created");
    }
    return _t("Copy your API key now");
  }

  /**
   * 免費試用金鑰在金鑰管理（LLM-07）上線前不能停用或刪除，所以不能照 DESIGN §6.3.2 寫
   * 「遺失時請建立新金鑰並停用舊的」；付費金鑰上線時再換回 DESIGN 的句子。
   */
  get intro() {
    return _t("For security, the full key is shown only this once. You cannot view it again after closing this window; if you lose it, please contact support to reset it.");
  }

  get creatingText() {
    return _t("Creating your key… This can take up to about 40 seconds.");
  }

  get freeTrialHint() {
    return _t("Free trial keys can only call Qwen3-4B: 10 calls per minute and about 200 calls per day.");
  }

  get closeConfirmText() {
    return _t("Once closed, this key cannot be shown again. Close anyway?");
  }

  get copyFailedText() {
    return _t("Copy failed. Please select the text and copy it manually.");
  }

  get baseUrlMissingText() {
    return _t("Not available yet");
  }

  get envUnavailableText() {
    return _t("The Base URL is not available yet, so environment variables cannot be generated. Do not use this key with tools that connect to OpenAI by default.");
  }

  get envVarsAvailable() {
    return !!this.state.key && !!this.state.baseUrl;
  }

  /** 四顆〔複製〕各自的可讀名稱（畫面上都只寫「複製」，讀屏要分得出複製的是哪一格）。 */
  get copyLabels() {
    return {
      key: _t("Copy API key"),
      base: _t("Copy Base URL"),
      model: _t("Copy model name"),
      env: _t("Copy as environment variables"),
    };
  }

  /** 複製成功時給讀屏的播報（按鈕的可讀名稱是 aria-label，按鈕上的「已複製」讀不到）。 */
  get copiedAnnouncement() {
    switch (this.state.copied) {
      case "key":
        return _t("API key copied.");
      case "base":
        return _t("Base URL copied.");
      case "model":
        return _t("Model name copied.");
      case "env":
        return _t("Environment variables copied.");
      default:
        return "";
    }
  }

  get closeLabel() {
    return _t("Close");
  }

  get exampleModel() {
    return FREE_TRIAL_EXAMPLE_MODEL;
  }

  get canFinish() {
    return this.state.phase === "shown" && this.state.saved;
  }

  async copy(what) {
    let text = null;
    if (what === "key") {
      text = this.state.key;
    } else if (what === "base") {
      text = this.state.baseUrl;
    } else if (what === "env") {
      text = buildEnvVarsText(this.state.key, this.state.baseUrl);
    } else if (what === "model") {
      text = FREE_TRIAL_EXAMPLE_MODEL;
    }
    const ok = text ? await copyText(text) : false;
    if (this.destroyed) return;
    this.state.copied = ok ? what : null;
    this.state.copyFailed = !ok;
  }

  onSavedChange(ev) {
    this.state.saved = !!ev.target.checked;
  }

  /** 點背景、Esc、右上角關閉、返回鍵、換頁：顯示中先確認；建立中不能關；錯誤時直接關。 */
  requestClose() {
    if (this.closed || this.state.phase === "creating") {
      return;
    }
    if (this.state.phase === "shown") {
      this.state.confirmClose = true;
      return;
    }
    this.close();
  }

  cancelClose() {
    this.state.confirmClose = false;
    this.focusedPhase = null;
  }

  finish() {
    if (this.canFinish) {
      this.close();
    }
  }

  close() {
    if (this.closed) return;
    const created = this.state.phase === "shown";
    this.closed = true;
    // 先拆守衛（背景解除 inert），頁面在 onClose 裡才能把焦點還回去。
    this.teardown();
    this.forgetKey();
    this.props.onClose({ created });
  }

  forgetKey() {
    this.state.key = null;
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget) {
      this.requestClose();
    }
  }

  // ------------------------------------------------------------------ 守衛與焦點

  isOwnSentinel(state) {
    return !!state && typeof state === "object" && state.woowLlmReveal === this.clientRequestId;
  }

  /** hash 換頁被擋下、網址還原之後：最上面不是自己的哨兵就補一筆（返回鍵才擋得住下一次）。 */
  rearmSentinel() {
    if (!this.guarding || window.location.href !== this.guardedHref) return;
    this.sentinelPushed = this.isOwnSentinel(window.history.state);
    if (!this.sentinelPushed) {
      this.pushSentinel();
    }
  }

  pushSentinel() {
    try {
      const current = window.history.state;
      const base = current && typeof current === "object" ? current : {};
      window.history.pushState({ ...base, woowLlmReveal: this.clientRequestId }, "", window.location.href);
      this.sentinelPushed = true;
    } catch {
      this.sentinelPushed = false;
    }
  }

  /** 拆掉所有守衛（冪等）：listener、router 離開守衛、背景 inert、history 哨兵。 */
  teardown() {
    document.removeEventListener("keydown", this.onKeydown);
    window.removeEventListener("popstate", this.onPopState);
    window.removeEventListener("beforeunload", this.onBeforeUnload);
    if (this.removeLeaveGuard) {
      this.removeLeaveGuard();
      this.removeLeaveGuard = null;
    }
    if (this.restoreInert) {
      this.restoreInert();
      this.restoreInert = null;
    }
    if (this.sentinelPushed) {
      this.sentinelPushed = false;
      // 只收回自己推的那一筆（網址相同，返回不會觸發 hashchange、router 不換頁）。
      if (this.isOwnSentinel(window.history.state)) {
        window.history.back();
      }
    }
  }

  /**
   * 打開時焦點移進對話框；金鑰顯示後移到金鑰的〔複製〕；沒建成時移到〔關閉〕；
   * 出現關閉確認時移到〔保持開啟〕。每個階段只移一次（使用者自己 Tab 走之後不搶回來）。
   */
  focusForPhase() {
    const dialog = this.dialogRef.el;
    if (!dialog || this.closed) return;
    if (this.state.confirmClose && !this.focusedConfirm) {
      this.focusedConfirm = true;
      const keep = dialog.querySelector(".o_woow_llm_reveal__confirm button");
      if (keep) keep.focus();
      return;
    }
    if (!this.state.confirmClose) {
      this.focusedConfirm = false;
    }
    if (this.focusedPhase === this.state.phase) return;
    this.focusedPhase = this.state.phase;
    let target = null;
    if (this.state.phase === "shown") {
      target = dialog.querySelector(".o_woow_llm_reveal__actions--key button");
    } else if (this.state.phase === "error") {
      target = dialog.querySelector(".o_woow_modal_footer button");
    }
    (target || dialog).focus();
  }
}
