// LLM-15 Task 6/7: Docs-tab contract and one-time reveal-modal handoff.
// This deliberately stays red until the Docs tab and modal action are implemented.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { readPaas } from "./lib/llm_test_support.mjs";

const DEMO_KEY = "one-time-demo-key";

async function readContractSource(path) {
  try {
    return await readPaas(path);
  } catch (error) {
    // Planned components fail their contract assertions instead of aborting on ENOENT.
    if (error?.code === "ENOENT") return "";
    throw error;
  }
}

const [modalSource, modalXml, docsJs, docsXml, docsScss] = await Promise.all([
  readPaas("components/llm/LlmKeyRevealModal.js"),
  readPaas("components/llm/LlmKeyRevealModal.xml"),
  readContractSource("pages/llm/tabs/LlmDocsTab.js"),
  readContractSource("pages/llm/tabs/LlmDocsTab.xml"),
  readContractSource("pages/llm/tabs/LlmDocsTab.scss"),
]);

function stripJavaScriptComments(source) {
  let out = "";
  let quote = null;
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    const next = source[index + 1];
    if (quote) {
      out += char;
      if (char === "\\") {
        out += next || "";
        index += 1;
      } else if (char === quote) {
        quote = null;
      }
      continue;
    }
    if (char === "'" || char === '"' || char.charCodeAt(0) === 96) {
      quote = char;
      out += char;
    } else if (char === "/" && next === "/") {
      while (index < source.length && source[index] !== "\n") index += 1;
      out += "\n";
    } else if (char === "/" && next === "*") {
      index += 2;
      while (index < source.length && !(source[index] === "*" && source[index + 1] === "/")) {
        if (source[index] === "\n") out += "\n";
        index += 1;
      }
      index += 1;
    } else {
      out += char;
    }
  }
  return out;
}

function stripXmlComments(source) {
  return source.replace(/<!--[\s\S]*?-->/g, "");
}

function escapeRegExp(text) {
  return text.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");
}

function assertSourceMatches(source, pattern, message) {
  pattern.lastIndex = 0;
  assert.ok(pattern.test(source), message);
}

function assertSourceDoesNotMatch(source, pattern, message) {
  pattern.lastIndex = 0;
  assert.ok(!pattern.test(source), message);
}

function assertRenderedText(phrase) {
  const exact = escapeRegExp(phrase).replace(/ /g, "\\s+");
  const quoted = "[\"'\\x60]";
  const translatedJs = new RegExp("\\b_t\\(\\s*" + quoted + exact + quoted, "s");
  const renderedXml = new RegExp(exact, "i");
  assert.ok(
    translatedJs.test(stripJavaScriptComments(docsJs)) || renderedXml.test(stripXmlComments(docsXml)),
    "Expected rendered/translatable text: " + phrase,
  );
}

const SECRET_VALUE_REFERENCE = String.raw`(?:this\.state\.key|this\.props\.initialKey|props\.initialKey)`;
const SECRET_TRANSPORT_PATTERNS = [
  new RegExp(
    String.raw`\brouter\.(?:navigate|push|replace|go)\s*\((?:(?!;)[\s\S]){0,1200}?\b${SECRET_VALUE_REFERENCE}\b`,
  ),
  new RegExp(
    String.raw`\b(?:window\.)?location\.(?:href|hash|search)\s*=\s*[^;\n]*\b${SECRET_VALUE_REFERENCE}\b`,
  ),
  new RegExp(
    String.raw`\b(?:window\.)?location\.(?:assign|replace)\s*\((?:(?!;)[\s\S]){0,1200}?\b${SECRET_VALUE_REFERENCE}\b`,
  ),
  new RegExp(
    String.raw`\b(?:new\s+)?URLSearchParams\s*\((?:(?!;)[\s\S]){0,1200}?\b${SECRET_VALUE_REFERENCE}\b`,
  ),
  new RegExp(
    String.raw`\bURLSearchParams\b[\s\S]{0,240}\.(?:set|append)\s*\(\s*[^,]+,\s*(?:(?!;)[\s\S]){0,400}?\b${SECRET_VALUE_REFERENCE}\b`,
  ),
];

function assertNoSecretTransport(name, source) {
  assertSourceDoesNotMatch(
    source,
    /\b(?:localStorage|sessionStorage|indexedDB|cookieStore|document\.cookie)\b/i,
    name + " must not persist a plaintext key",
  );
  for (const pattern of SECRET_TRANSPORT_PATTERNS) {
    assertSourceDoesNotMatch(
      source,
      pattern,
      name + " must not put a plaintext key value in URL or router state",
    );
  }
}

const modalCode = stripJavaScriptComments(modalSource);
const docsCode = stripJavaScriptComments(docsJs);
const docsRenderedSource = docsCode + "\n" + stripXmlComments(docsXml);

test("reveal modal exposes and binds openDocs before closing its one-time key", () => {
  assertSourceMatches(
    modalCode,
    /\bstatic\s+props\s*=\s*\{[\s\S]{0,900}\bonOpenDocs\s*:\s*\{[\s\S]{0,160}\btype\s*:\s*Function\b/,
    "LlmKeyRevealModal declares onOpenDocs as a callback prop",
  );
  assertSourceMatches(
    modalCode,
    /\bclose\s*\(\s*\)\s*\{(?:(?!\n\s{2}(?:async\s+)?(?:(?:get|set)\s+)?[A-Za-z_$][\w$]*\s*\()[\s\S])*\b(?:this\.forgetKey\s*\(\s*\)|this\.state\.key\s*=\s*null\b)/,
    "close clears the one-time key through forgetKey or direct state reset",
  );
  assertSourceMatches(
    modalCode,
    /\bopenDocs\s*\(\s*\)\s*\{(?:(?!\n\s{2}(?:async\s+)?(?:(?:get|set)\s+)?[A-Za-z_$][\w$]*\s*\()[\s\S])*\bthis\.props\.onOpenDocs\s*\(\s*this\.state\.key\s*\)(?:(?!\n\s{2}(?:async\s+)?(?:(?:get|set)\s+)?[A-Za-z_$][\w$]*\s*\()[\s\S])*\bthis\.close\s*\(\s*\)/,
    "openDocs hands the current key to its caller before the verified key-clearing close path",
  );
  assert.equal(
    (modalCode.match(/\bthis\.props\.onOpenDocs\s*\(\s*this\.state\.key\s*\)/g) || []).length,
    1,
    "the reveal modal has exactly one plaintext Docs handoff",
  );
  assertSourceMatches(
    modalCode,
    /\bopenDocs\s*\(\s*\)\s*\{[\s\S]{0,240}\bif\s*\(\s*this\.props\.onOpenDocs\s*\)\s*\{[\s\S]{0,240}\bthis\.props\.onOpenDocs\s*\(\s*this\.state\.key\s*\)/,
    "openDocs guards the optional callback before handing off the key",
  );
  assertSourceMatches(
    stripXmlComments(modalXml),
    /\bt-on-click\s*=\s*"[^"]*\bopenDocs\b[^"]*"/,
    "the reveal template binds a user action to openDocs",
  );
});

test("Docs tab declares and consumes its one-time initial key locally", () => {
  assertSourceDoesNotMatch(docsCode, /\bllmService\b/, "LlmDocsTab must not use shared service");
  assertSourceMatches(
    docsCode,
    /\binitialKey\s*:\s*\{[\s\S]{0,160}\btype\s*:\s*String\b/,
    "LlmDocsTab declares initialKey",
  );
  assertSourceMatches(
    docsCode,
    /\bonConsumeKey\s*:\s*\{[\s\S]{0,160}\btype\s*:\s*Function\b/,
    "LlmDocsTab declares onConsumeKey",
  );
  assertSourceMatches(
    docsCode,
    /\buseState\s*\(\s*(?:\{[\s\S]{0,600}?\b(?:key|initialKey)\s*:\s*)?(?:this\.)?props\.initialKey\b[\s\S]{0,800}?\)\s*;?[\s\S]{0,800}?\b(?:this\.)?props\.onConsumeKey\s*(?:\?\.)?\s*\(/,
    "LlmDocsTab copies initialKey into local useState before consuming the handoff",
  );
  assert.equal(
    (docsCode.match(/\b(?:this\.)?props\.onConsumeKey\s*(?:\?\.)?\s*\(/g) || []).length,
    1,
    "LlmDocsTab consumes the handoff once",
  );
});

test("Docs tab renders copyable setup examples, limits, FAQ, points, and attribution", () => {
  const docsMarkup = stripXmlComments(docsXml);
  assertSourceMatches(docsMarkup, /\bBase\s*URL\b/i, "Docs include a Base URL example");
  assertSourceMatches(docsMarkup, /<(?:pre|code)\b[^>]*>/i, "Docs examples use a pre or code container");
  assertSourceMatches(docsCode, /\bcopyText\s*\(/, "Base URL has a copy action");
  assertSourceMatches(
    docsMarkup,
    /(?:Base\s*URL[\s\S]{0,1200}t-on-click\s*=\s*"copyBase"|t-on-click\s*=\s*"copyBase"[\s\S]{0,1200}Base\s*URL)/i,
    "the Base URL is adjacent to the real copyBase handler",
  );

  for (const [label, pattern] of [
    ["curl", /\bcurl\b/i],
    ["Python", /\bPython\b/i],
    ["OpenAI SDK", /\bopenai\b/i],
    ["Node", /\bNode(?:\.js)?\b/i],
    ["n8n", /\bn8n\b/i],
    ["Open WebUI", /\bOpen\s*WebUI\b/i],
    ["OpenCode", /\bOpenCode\b/i],
    ["paid-key example", /\bPaid\b/i],
    ["free-trial example", /\bFree(?:\s+trial)?\b/i],
  ]) {
    assertSourceMatches(docsRenderedSource, pattern, "Docs include a " + label + " setup example");
  }

  for (const phrase of [
    "Paid keys allow up to 120 successful requests per minute",
    "Free trial keys allow up to 10 requests per minute",
    "The 120 successful-request ceiling is shared across paid and free traffic.",
    "Free requests use a token bucket of 10. One token is refilled every six seconds, and failed requests count.",
    "The approximate daily free allowance is 200 successful requests.",
    "Key creation and credit conversion have a 10-per-hour limit after the request reaches new-api",
    "All key-creation and conversion attempts also have a separate 30-per-hour guard",
  ]) {
    assertRenderedText(phrase);
  }

  for (const status of ["401", "403", "429", "503", "524"]) {
    assertSourceMatches(
      stripXmlComments(docsXml),
      new RegExp("<h[1-6]\\b[^>]*>[\\s\\S]*?\\b" + status + "\\b[\\s\\S]*?<\\/h[1-6]>", "i"),
      status + " is an FAQ heading",
    );
  }
  assertSourceMatches(
    docsRenderedSource,
    /\b1\s+point\b[\s\S]{0,120}(?:\bNT\$(?!\w)|\bTWD\b|\bNew\s+Taiwan\s+Dollar\b|台幣|新台幣)/i,
    "Docs explain what one point represents",
  );
  assertSourceMatches(docsRenderedSource, /\bAGPL(?:-3\.0)?\b/i, "Docs identify the AGPL license");
  assertSourceMatches(
    docsMarkup,
    /https:\/\/github\.com\/QuantumNous\/new-api\/tree\/v1\.0\.0-rc\.44/,
    "Docs link to the required new-api target",
  );
  assertSourceDoesNotMatch(docsMarkup, /\bClaude\s+Code\b/i, "Docs must not mention Claude Code");
  for (const [fact, pattern] of [
    ["Traditional-Chinese 401 literal", /無效的令牌/],
    ["English 401 literal", /\bInvalid token\b/],
    ["English Accept-Language condition", /\bAccept-Language is en\b/],
    ["401 API keys direction", /\bAPI keys\b/],
    ["403 crossed-limit request", /\brequest that crossed a limit\b/],
    ["403 AI credit", /\binsufficient AI credit\b/],
    ["403 model permission", /\blacks permission for the requested model\b/],
  ]) {
    assertSourceMatches(docsRenderedSource, pattern, "Docs include approved " + fact);
  }
  assertSourceMatches(
    docsCode,
    /\bfunction\s+curlExample\s*\([\s\S]{0,800}\bContent-Type:\s*application\/json\b[\s\S]{0,400}\.join\(continuation\)/,
    "curl helper creates a JSON request with a multiline continuation",
  );
  assert.ok(
    docsCode.includes('const continuation = " \\\\" + "\\n";'),
    "curl helper joins lines with a shell continuation",
  );
  assertSourceMatches(
    docsCode,
    /\bid\s*:\s*"curl"[\s\S]{0,240}\bcurlExample\s*\(\s*base\s*,\s*key\s*,\s*"gpt-4o-mini"\s*,\s*"Hello"\s*\)/,
    "normal curl example uses the multiline helper",
  );
  assertSourceDoesNotMatch(
    docsCode,
    /\bid\s*:\s*"curl"\s*,\s*title\s*:\s*this\.curlTitle\s*,\s*text\s*:\s*`[^`]*\\n[^`]*`/,
    "normal curl example has no literal backslash+n text",
  );
  assertSourceMatches(
    docsCode,
    /\bid\s*:\s*"paid"[\s\S]{0,240}\bcurlExample\s*\(\s*base\s*,\s*key\s*,\s*"gpt-4o-mini"/,
    "paid curl example uses the multiline helper",
  );
  assertSourceMatches(
    docsCode,
    /\bid\s*:\s*"free"[\s\S]{0,240}\bcurlExample\s*\(\s*base\s*,\s*key\s*,\s*"qwen3-4b-cluster1"/,
    "free curl example uses the multiline helper",
  );
  assertSourceDoesNotMatch(docsCode, /\\n\+\s*-(?:H|d)\b/, "curl examples have no diff-marker prefixes");
});

test("Docs copy controls require an authoritative Base URL", () => {
  const docsMarkup = stripXmlComments(docsXml);
  assertSourceMatches(
    docsMarkup,
    /<button\b[^>]*\bt-att-disabled\s*=\s*"!\s*endpointAvailable"[^>]*\bt-on-click\s*=\s*"copyBase"/,
    "Base URL copy button is disabled without an endpoint and invokes copyBase",
  );
  assertSourceDoesNotMatch(docsMarkup, /\bt-on-click\s*=\s*"copy\s+base"/, "Docs do not use an invalid copy handler");
  assertSourceMatches(
    docsMarkup,
    /<WoowButton\b[^>]*\bdisabled\s*=\s*"!\s*endpointAvailable"[^>]*\bonClick\s*=\s*"[^"]*this\.copy\s*\(/,
    "all example copy controls are disabled without an endpoint",
  );
  assertSourceMatches(
    docsCode,
    /\basync\s+copy\s*\(\s*value\s*,\s*id\s*\)\s*\{[\s\S]{0,500}?\bif\s*\(\s*!this\.endpointAvailable\s*\|\|\s*typeof\s+value\s*!==\s*"string"\s*\|\|\s*!value\s*\)[\s\S]{0,300}?\bcopyText\s*\(\s*value\s*\)/,
    "copy refuses a missing endpoint or value before using the clipboard",
  );
  assertSourceMatches(
    docsCode,
    /\basync\s+copyBase\s*\(\s*\)\s*\{\s*return\s+this\.copy\s*\(\s*this\.props\.baseUrl\s*,\s*"base"\s*\)/,
    "copyBase delegates the supplied Base URL through the guarded copy path",
  );
  assertSourceMatches(
    docsCode,
    /\bget\s+apiKey\s*\(\s*\)\s*\{\s*return\s+this\.endpointAvailable\s*&&\s*this\.state\.key\s*\?\s*this\.state\.key\s*:\s*SAFE_KEY/,
    "the key display uses the safe placeholder until both key and endpoint are available",
  );
  assertSourceMatches(
    docsMarkup,
    /<p\b[^>]*\bt-if\s*=\s*"endpointAvailable\s*&amp;&amp;\s*state\.key"[^>]*This key is held only by this guide instance/,
    "the one-time-key lifecycle hint is gated by the endpoint and local key",
  );
  assertSourceMatches(
    docsMarkup,
    /<p\b[^>]*\bt-else\s*=\s*""[^>]*Replace YOUR_API_KEY with your own key/,
    "the safe-placeholder explanation remains available when the key hint is gated",
  );
});

test("Docs code blocks offer horizontal overflow", () => {
  assertSourceMatches(
    docsScss,
    /(?:\bpre\b|\bcode\b|__code|__example)[^{]*\{[\s\S]{0,500}\boverflow-x\s*:\s*(?:auto|scroll)\b/i,
    "a Docs code container has horizontal overflow",
  );
  assertSourceMatches(
    docsScss,
    /&__row\s*\{[\s\S]{0,320}\bflex-wrap\s*:\s*wrap\b/,
    "Docs rows wrap on narrow screens",
  );
  assertSourceMatches(
    docsScss,
    /&__row\s*>\s*code\s*\{[\s\S]{0,240}\boverflow-x\s*:\s*(?:auto|scroll)\b/,
    "long Base URLs can scroll inside a Docs row",
  );
  assertSourceMatches(docsScss, /@media\s*\(\s*max-width\s*:\s*400px\s*\)/, "Docs have a 400px layout rule");
});

test("Docs and reveal-modal sources keep plaintext keys out of persistence and navigation", () => {
  for (const [name, source] of [
    ["reveal modal JS", modalSource],
    ["reveal modal XML", modalXml],
    ["Docs JS", docsJs],
    ["Docs XML", docsXml],
    ["Docs SCSS", docsScss],
  ]) {
    assertNoSecretTransport(name, source);
  }
});

test("contract expectations use only the harmless demo key", async () => {
  const testSource = await readFile(new URL(import.meta.url), "utf8");
  assertSourceMatches(testSource, new RegExp(escapeRegExp(DEMO_KEY)), "Contract expectations include the demo key");
  assertSourceDoesNotMatch(testSource, /sk-[A-Za-z0-9_-]{20,}/, "Contract expectations contain no real-looking key");
});
