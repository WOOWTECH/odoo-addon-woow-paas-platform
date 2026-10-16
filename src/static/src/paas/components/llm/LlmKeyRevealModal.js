/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { copyText } from "../../utils/clipboard";
import { llmService, newClientRequestId } from "../../services/llm_service";
import { llmErrorMessage } from "../../services/llm_errors";

/** 免費試用金鑰的範例模型（DESIGN §6.3.6）；兩個 Qwen 都能叫，教學用 cluster1 這個。 */
export const FREE_TRIAL_EXAMPLE_MODEL = "qwen3-4b-cluster1";

/**
 * 〔複製為環境變數〕的內容（DESIGN §6.3.2）：一行一個變數，貼進 `.env` 或 shell 都能用。
 * Base URL 還沒設定時只給金鑰那一行（不放一個空的 OPENAI_BASE_URL 讓工具連到預設的 OpenAI）。
 */
export function buildEnvVarsText(key, baseUrl) {
  const lines = ["OPENAI_API_KEY=" + key];
  if (baseUrl) {
    lines.push("OPENAI_BASE_URL=" + baseUrl);
  }
  return lines.join("\n");
}

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
    this.onKeydown = (ev) => {
      if (ev.key === "Escape") {
        this.requestClose();
      }
    };
    onMounted(() => {
      document.addEventListener("keydown", this.onKeydown);
      this.create();
    });
    onWillUnmount(() => {
      document.removeEventListener("keydown", this.onKeydown);
      this.forgetKey();
    });
  }

  async create() {
    const result = await llmService.createFreeTrialKey(this.clientRequestId);
    const data = result.success ? result.data : null;
    if (data && typeof data.key === "string" && data.key.startsWith("sk-")) {
      this.state.key = data.key;
      this.state.maskedKey = typeof data.masked_key === "string" ? data.masked_key : null;
      if (typeof data.base_url === "string" && data.base_url) {
        this.state.baseUrl = data.base_url;
      }
      this.state.phase = "shown";
      return;
    }
    // 成功卻沒有金鑰（不該發生）：當成「金鑰可能已建好卻沒顯示」，引導使用者重建。
    this.state.errorMessage = llmErrorMessage(result.success
      ? { code: "llm_key_create_interrupted" } : result);
    this.state.phase = "error";
  }

  // 文案放在 JS（單行 _t）：模板的多行文字節點會把換行與縮排一起抽進 msgid。
  get title() {
    return _t("Copy your API key now");
  }

  get intro() {
    return _t("For security, the full key is shown only this once. You cannot view it again after closing this window; if you lose it, create a new key and disable the old one.");
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
      text = this.state.key ? buildEnvVarsText(this.state.key, this.state.baseUrl) : null;
    } else if (what === "model") {
      text = FREE_TRIAL_EXAMPLE_MODEL;
    }
    const ok = text ? await copyText(text) : false;
    this.state.copied = ok ? what : null;
    this.state.copyFailed = !ok;
  }

  onSavedChange(ev) {
    this.state.saved = !!ev.target.checked;
  }

  /** 點背景、Esc、右上角關閉：顯示中先確認；建立中不能關；錯誤時直接關。 */
  requestClose() {
    if (this.state.phase === "creating") {
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
  }

  finish() {
    if (this.canFinish) {
      this.close();
    }
  }

  close() {
    const created = this.state.phase === "shown";
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
}
