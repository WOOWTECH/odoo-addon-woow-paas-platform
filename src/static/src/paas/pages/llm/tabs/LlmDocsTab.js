/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../../../components/button/WoowButton";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { copyText } from "../../../utils/clipboard";
import { llmService } from "../../../services/llm_service";
import { publishedPaidModel, shellQuote } from "./llm_docs_helpers";

const SAFE_KEY = "YOUR_API_KEY";
const SAFE_BASE_URL = "YOUR_BASE_URL";

function curlExample(base, key, model, content) {
  const body = JSON.stringify({ model, messages: [{ role: "user", content }] });
  const continuation = " \\" + "\n";
  return [
    `curl ${base}/chat/completions`,
    `  -H "Authorization: Bearer ${key}"`,
    `  -H "Content-Type: application/json"`,
    `  -d ${shellQuote(body)}`,
  ].join(continuation);
}

export class LlmDocsTab extends Component {
  static template = "woow_paas_platform.LlmDocsTab";
  static components = { WoowButton, WoowCard, WoowIcon };
  static props = {
    initialKey: { type: String, optional: true },
    initialMode: { type: String, optional: true },
    initialAllowedModels: { type: [Array, { value: null }], optional: true },
    onConsumeKey: { type: Function, optional: true },
    baseUrl: { type: [String, { value: null }], optional: true },
  };

  setup() {
    this.llm = useState(llmService);
    this.state = useState({
      key: this.props.initialKey || "",
      allowedModels: Array.isArray(this.props.initialAllowedModels) ? [...this.props.initialAllowedModels] : null,
      mode: this.props.initialMode === "free" ? "free" : "paid",
      keyMode: this.props.initialMode === "free" ? "free" : "paid",
      tool: "curl",
      copied: "",
      copyFailed: false,
    });
    if (this.props.initialKey) this.props.onConsumeKey?.();
    onMounted(() => {
      if (!this.llm.overviewLoaded || this.llm.overviewError) llmService.fetchState();
      if (!this.llm.modelsLoaded || this.llm.models?.state !== "ok") llmService.fetchModels();
    });
    onWillUnmount(() => {
      this.state.key = "";
    });
  }

  get apiKey() {
    return this.endpointAvailable && this.state.key && this.state.keyMode === this.state.mode
      ? this.state.key : SAFE_KEY;
  }
  get endpoint() { return this.endpointAvailable ? this.props.baseUrl : SAFE_BASE_URL; }
  get endpointAvailable() { return typeof this.props.baseUrl === "string" && this.props.baseUrl.length > 0; }
  get selectedModel() { return publishedPaidModel(this.llm.models, this.state.allowedModels); }
  get model() { return this.state.mode === "free" ? "qwen3-4b-cluster1" : this.selectedModel || "YOUR_PUBLISHED_PAID_MODEL"; }
  get modelPlaceholder() { return this.state.mode === "paid" && !this.selectedModel; }
  get keyLifetimeText() { return _t("This key is held only by this guide instance and disappears when you leave it."); }
  get placeholderKeyText() { return _t("Replace YOUR_API_KEY with your own key. This example value is not a real credential."); }
  get unavailableText() { return _t("The Base URL is unavailable until the service provides its public endpoint."); }
  get examplesPlaceholderText() { return _t("Examples use YOUR_BASE_URL and YOUR_API_KEY placeholders until the Base URL is available. Copying is disabled."); }
  get copyFailedText() { return _t("Copy failed. Please select the text and copy it manually."); }
  get copiedLabel() { return _t("Copied."); }
  get copyLabel() { return _t("Copy"); }
  get copyBaseLabel() { return _t("Copy Base URL"); }
  get pageTitle() { return _t("API usage guide"); }
  get pageIntro() { return _t("Connect your preferred tools with the examples below. Replace the placeholders with your own values."); }
  get baseUrlTitle() { return _t("Base URL"); }
  get keyTitle() { return _t("API key"); }
  get paidTitle() { return _t("Paid key example"); }
  get freeTitle() { return _t("Free trial example"); }
  get paidLimit() { return _t("Paid keys allow up to 120 successful requests per minute"); }
  get freeLimit() { return _t("Free trial keys allow up to 10 requests per minute"); }
  get sharedLimitText() { return _t("The 120 successful-request ceiling is shared across paid and free traffic."); }
  get freeBucketText() { return _t("Free requests use a token bucket of 10. One token is refilled every six seconds, and failed requests count."); }
  get freeDailyText() { return _t("The approximate daily free allowance is 200 successful requests."); }
  get keyRateLimit() { return _t("Key creation and credit conversion have a 10-per-hour limit after the request reaches new-api"); }
  get guardRateLimit() { return _t("All key-creation and conversion attempts also have a separate 30-per-hour guard"); }
  get limitsTitle() { return _t("Request limits"); }
  get examplesTitle() { return _t("Setup examples"); }
  get toolsLabel() { return _t("Choose a tool"); }
  get modesLabel() { return _t("Choose key type"); }
  get paidModeLabel() { return _t("Paid model"); }
  get freeModeLabel() { return _t("Free Qwen trial"); }
  get modelPlaceholderText() { return _t("No published paid model is available in the catalog. Replace YOUR_PUBLISHED_PAID_MODEL after selecting a model from Models & Pricing."); }
  get pointsText() { return _t("1 point corresponds to NT$1 (New Taiwan Dollar), including tax. Ordinary voluntary conversions are non-refundable. Exceptions for service termination, material price increases, incorrect charges, and statutory rights are handled under the AI Models Terms."); }
  get sourceText() { return _t("This service uses new-api, licensed under AGPL-3.0. View the source for the deployed version."); }
  get faqTitle() { return _t("Frequently asked questions"); }
  get faq401() { return _t("401 — The key is invalid, disabled, expired, out of quota, or its free allowance for today is used. The response says 無效的令牌, or Invalid token when Accept-Language is en. Go to API keys to check the key status."); }
  get faq403() { return _t("403 — This is the request that crossed a limit. It can also mean insufficient AI credit, or that the key lacks permission for the requested model."); }
  get faq429() { return _t("429 — Too many requests in a short period. Wait briefly, then reduce the request rate."); }
  get faq503() { return _t("503 — No available channel can serve this request. For example, a paid key cannot call the free Qwen channel. Retry later or choose an available model."); }
  get faq524() { return _t("524 — Cloudflare timed out while waiting for the upstream request. Retry later, use streaming for long requests, or shorten the request."); }
  get curlTitle() { return "curl"; } // CLI name, not the English word.
  get pythonTitle() { return _t("Python with OpenAI SDK"); }
  get nodeTitle() { return _t("Node.js with OpenAI SDK"); }
  get n8nTitle() { return _t("n8n"); }
  get openWebUiTitle() { return _t("Open WebUI"); }
  get openCodeTitle() { return _t("OpenCode"); }
  get tools() {
    return [
      { id: "curl", label: this.curlTitle }, { id: "python", label: this.pythonTitle },
      { id: "node", label: this.nodeTitle }, { id: "n8n", label: this.n8nTitle },
      { id: "webui", label: this.openWebUiTitle }, { id: "opencode", label: this.openCodeTitle },
    ];
  }
  get copyNames() { return { base: _t("Copy Base URL"), example: _t("Copy setup example") }; }

  get example() {
    const base = this.endpoint;
    const key = this.apiKey;
    const model = this.model;
    const pyKey = JSON.stringify(key);
    const pyBase = JSON.stringify(base);
    const pyModel = JSON.stringify(model);
    const jsKey = JSON.stringify(key);
    const jsBase = JSON.stringify(base);
    const jsModel = JSON.stringify(model);
    const snippets = {
      curl: curlExample(base, key, model, this.state.mode === "free" ? "Free trial request" : "Paid request"),
      python: `from openai import OpenAI\n\nclient = OpenAI(api_key=${pyKey}, base_url=${pyBase})\nresponse = client.chat.completions.create(\n    model=${pyModel}, messages=[{"role": "user", "content": "Hello"}]\n)\nprint(response.choices[0].message.content)`,
      node: `import OpenAI from "openai";\n\nconst client = new OpenAI({ apiKey: ${jsKey}, baseURL: ${jsBase} });\nconst response = await client.chat.completions.create({\n  model: ${jsModel}, messages: [{ role: "user", content: "Hello" }],\n});\nconsole.log(response.choices[0].message.content);`,
      n8n: _t("In the OpenAI Chat Model node, set the credential API key to %(key)s and the Base URL to %(base)s. Select model %(model)s.", { key, base, model }),
      webui: _t("In Open WebUI's OpenAI connection settings, set the API Base URL to %(base)s, the API key to %(key)s, and the model to %(model)s.", { key, base, model }),
      opencode: _t("In OpenCode, configure an OpenAI-compatible provider with baseURL %(base)s, API key %(key)s, and model %(model)s.", { key, base, model }),
    };
    return { id: this.state.tool, title: this.tools.find((tool) => tool.id === this.state.tool)?.label || this.curlTitle, text: snippets[this.state.tool] };
  }

  selectTool(tool) { this.state.tool = tool; this.state.copied = ""; }
  selectMode(mode) {
    const nextMode = mode === "free" ? "free" : "paid";
    if (nextMode !== this.state.mode) {
      this.state.key = "";
      this.state.allowedModels = null;
      this.state.keyMode = nextMode;
      this.state.copied = "";
    }
    this.state.mode = nextMode;
  }

  async copy(value, id) {
    if (!this.endpointAvailable || this.modelPlaceholder && id === "example" || typeof value !== "string" || !value) {
      this.state.copied = "";
      this.state.copyFailed = false;
      return false;
    }
    const ok = await copyText(value);
    this.state.copied = ok ? id : "";
    this.state.copyFailed = !ok;
    return ok;
  }

  async copyBase() { return this.copy(this.props.baseUrl, "base"); }

  async copyExample() { return this.copy(this.example.text, "example"); }
}
