/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../../../components/button/WoowButton";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { copyText } from "../../../utils/clipboard";

const SAFE_KEY = "YOUR_API_KEY";
const SAFE_BASE_URL = "YOUR_BASE_URL";

function curlExample(base, key, model, content) {
  const body = JSON.stringify({ model, messages: [{ role: "user", content }] });
  const continuation = " \\" + "\n";
  return [
    `curl ${base}/chat/completions`,
    `  -H "Authorization: Bearer ${key}"`,
    `  -H "Content-Type: application/json"`,
    `  -d '${body}'`,
  ].join(continuation);
}

export class LlmDocsTab extends Component {
  static template = "woow_paas_platform.LlmDocsTab";
  static components = { WoowButton, WoowCard, WoowIcon };
  static props = {
    initialKey: { type: String, optional: true },
    onConsumeKey: { type: Function, optional: true },
    baseUrl: { type: String, optional: true },
  };

  setup() {
    this.state = useState({ key: this.props.initialKey || "", copied: "", copyFailed: false });
    if (this.props.initialKey) this.props.onConsumeKey?.();
  }

  get apiKey() { return this.endpointAvailable && this.state.key ? this.state.key : SAFE_KEY; }
  get endpoint() { return this.endpointAvailable ? this.props.baseUrl : SAFE_BASE_URL; }
  get endpointAvailable() { return typeof this.props.baseUrl === "string" && this.props.baseUrl.trim().length > 0; }
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
  get pointsText() { return _t("1 point corresponds to NT$1 (New Taiwan Dollar)."); }
  get sourceText() { return _t("This service uses new-api, licensed under AGPL-3.0. View the source for the deployed version."); }
  get faqTitle() { return _t("Frequently asked questions"); }
  get faq401() { return _t("401 — The key is invalid, disabled, expired, out of quota, or its free allowance for today is used. The response says 無效的令牌, or Invalid token when Accept-Language is en. Go to API keys to check the key status."); }
  get faq403() { return _t("403 — This is the request that crossed a limit. It can also mean insufficient AI credit, or that the key lacks permission for the requested model."); }
  get faq429() { return _t("429 — Too many requests in a short period. Wait briefly, then reduce the request rate."); }
  get faq503() { return _t("503 — The service is temporarily unavailable. Retry after a short wait."); }
  get faq524() { return _t("524 — The upstream request took too long. Retry later or shorten the request."); }
  get curlTitle() { return _t("curl"); }
  get pythonTitle() { return _t("Python with OpenAI SDK"); }
  get nodeTitle() { return _t("Node.js with OpenAI SDK"); }
  get n8nTitle() { return _t("n8n"); }
  get openWebUiTitle() { return _t("Open WebUI"); }
  get openCodeTitle() { return _t("OpenCode"); }
  get copyNames() {
    return { base: _t("Copy Base URL"), curl: _t("Copy curl example"), python: _t("Copy Python example"), node: _t("Copy Node example"), n8n: _t("Copy n8n example"), webui: _t("Copy Open WebUI example"), opencode: _t("Copy OpenCode example"), paid: _t("Copy paid key example"), free: _t("Copy free trial example") };
  }

  get examples() {
    const base = this.endpoint;
    const key = this.apiKey;
    return [
      { id: "curl", title: this.curlTitle, text: curlExample(base, key, "gpt-4o-mini", "Hello") },
      { id: "python", title: this.pythonTitle, text: `from openai import OpenAI\n\nclient = OpenAI(api_key="${key}", base_url="${base}")\nresponse = client.chat.completions.create(\n    model="gpt-4o-mini", messages=[{"role": "user", "content": "Hello"}]\n)\nprint(response.choices[0].message.content)` },
      { id: "node", title: this.nodeTitle, text: `import OpenAI from "openai";\n\nconst client = new OpenAI({ apiKey: "${key}", baseURL: "${base}" });\nconst response = await client.chat.completions.create({\n  model: "gpt-4o-mini", messages: [{ role: "user", content: "Hello" }],\n});\nconsole.log(response.choices[0].message.content);` },
      { id: "n8n", title: this.n8nTitle, text: _t("In the OpenAI Chat Model node, set the credential API key to %(key)s and the Base URL to %(base)s.", { key, base }) },
      { id: "webui", title: this.openWebUiTitle, text: _t("In the OpenAI connection settings, set the API Base URL to %(base)s and the API key to %(key)s.", { key, base }) },
      { id: "opencode", title: this.openCodeTitle, text: _t("Set the provider's baseURL to %(base)s and its API key to %(key)s.", { key, base }) },
      { id: "paid", title: this.paidTitle, text: curlExample(base, key, "gpt-4o-mini", "Paid request") },
      { id: "free", title: this.freeTitle, text: curlExample(base, key, "qwen3-4b-cluster1", "Free trial request") },
    ];
  }

  async copy(value, id) {
    if (!this.endpointAvailable || typeof value !== "string" || !value) {
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

  async copyText(value, id) { return this.copy(value, id); }
}
