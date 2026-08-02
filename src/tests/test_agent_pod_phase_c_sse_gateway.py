"""TDD red phase — Phase C 塊④: SSE generator dual-mode flag gate.

Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase C Tasks 14-15
Design:
  - §5.6 SSE generator rewrite — ``if project.use_agent_runtime:`` branch
         (flag gate, default False); False → existing ai_client.py path;
         True → spawn_or_attach + relay 15-type envelopes as SSE events.
  - §1.2 frontend ai_service.js EventSource — ZERO change.

Acceptance criteria covered:

SSE.1  REGRESSION — ``project.use_agent_runtime`` defaults False (the gate
       is OFF by default → Phase C merge does not change production chat).
SSE.2  REGRESSION — frontend ``ai_service.js`` still builds the EventSource
       URL ``/api/ai/stream/${channelId}?csrf_token=...`` (the §1.2 / §5.6
       "frontend zero-change" contract — backend rewrite must keep the same
       SSE entry point + query shape).
SSE.3  REGRESSION — the SSE endpoint's CSRF gate stays FIRST: a request
       with no/invalid csrf_token returns the legacy ``csrf_error`` SSE
       frame (200 + text/event-stream) regardless of the runtime flag.
SSE.4  RED — with ``use_agent_runtime=True`` the endpoint takes the
       agent-runtime branch, NOT the legacy assistant path. Observable: the
       resulting SSE ``error_code`` is no longer the legacy ``no_agent``
       (it becomes a runtime-branch code such as ``no_template`` /
       ``project_access_denied`` / ``spawn_failed`` per §5.6 pseudocode).
SSE.5  REGRESSION — with ``use_agent_runtime=False`` (default) the endpoint
       keeps the legacy assistant path: a channel without an assistant
       yields ``no_agent`` (production behaviour unchanged).

Red expectation today (Phase C impl has NOT started):

* SSE.1 / SSE.2 / SSE.3 / SSE.5 → PASS today and MUST STAY GREEN post-impl
  (these lock the flag-default + frontend contract + csrf ordering + legacy
  path — i.e. "Phase C merge changes nothing while flag is False").
* SSE.4 → RED: today there is no ``use_agent_runtime`` branch, so a flag-True
  project still runs the legacy generator → ``no_agent`` → the assertion
  "error_code != no_agent" fails. Once the dual-mode branch ships, flag-True
  routes to the runtime path → a non-``no_agent`` code → green.

Why this design (failure-mode honesty):

* SSE.4 is a pure HTTP behavioural probe — it does NOT import or mock the
  block-③ client module, so its red points squarely at "the flag branch is
  missing", not at an unrelated unbuilt dependency.
* CSRF token is fetched from the rendered app bootstrap (deterministic in a
  test DB); the helper asserts a token was obtained so a csrf-extraction
  failure surfaces loudly rather than masquerading as the red.

Fixture / env assumptions:

* Phase A field ``project.use_agent_runtime`` present.
* Admin user (group_system) drives the SSE endpoint so channel/project
  access checks pass deterministically; channel has admin as a member and
  a task linking channel_id → channel + project_id → project.

Boundary (intentionally NOT tested here):

* Actual WS relay of 15-type envelopes → that path requires a live pod;
  Phase D pilot / paas-operator integration tests cover it. Here we only
  assert the *dispatch decision* (which branch the flag selects).
* spawn_or_attach client mechanics → block ③.
* Enabling the flag for a pilot project + cold-start latency → Phase D.
"""
import json
import os
import re
import uuid

from odoo.tests.common import HttpCase, TransactionCase, tagged


# =========================================================================== #
# 前端 HTML sink 掃描器（XSS.0-XSS.5 與改寫後的 SSE.9 共用）                    #
# =========================================================================== #
#
# 為什麼需要一個真的詞法掃描器，而不是 assertIn 幾個字面量：
#
# 舊 SSE.9 只釘 ``safeHtml(m.body)`` / ``safeHtml(res.data.body)`` 兩個字面量。
# 兩個方向都壞：新增第三個未消毒注入點它擋不住（假綠）；把變數 ``m`` 改名成
# ``msg``（完全正確的重構）它會變紅（假紅）。下面的掃描器改成守「形態」而非
# 「字面量」，所以不綁變數名，且新站點會自動進入檢查範圍。
#
# 這裡守的到底是什麼（2026-08-02 以真 chromium 實測 OWL 2 的 t-out 語意後確定）：
#
#   OWL 的 ``t-out`` 只有在值是 ``Markup`` 實例時才輸出原始 HTML（見 owl.js 的
#   ``value instanceof Markup`` 分支），其餘一律走 ``text()`` 逃脫。實測四種形態：
#
#     safeHtml(payload)      → payload 被消乾淨，不觸發
#     裸字串 payload         → 被逃脫成文字，**不觸發**
#     markup(未消毒 payload) → **觸發 XSS**
#     innerHTML = 裸字串     → **觸發 XSS**
#
#   也就是說「少包一層 safeHtml」在 t-out 路徑上只是顯示 bug，不是 XSS；真正
#   的 XSS 向量只有兩個：``markup()`` 包未消毒資料，以及 ``.innerHTML =`` 賦值。
#   本掃描器就守這兩個，加上 portal 聊天室 ``html:`` 的正向清單。
#
# ⚠ 誠實聲明：這仍然是**靜態原始碼檢查**，不是行為測試。它守不住
#   ``safeHtml`` / ``parseMarkdown`` 內部實作被改壞（例如 DOMPurify 設定被放寬），
#   也守不住執行期真實渲染結果。本 repo 沒有可執行 JS 的測試基礎設施
#   （無 ``web.assets_unit_tests`` bundle、無 hoot/QUnit 測試、paas-odoo image
#   內既無 chrome 執行檔也無 websocket 模組，``HttpCase.browser_js`` 只會
#   SkipTest），硬寫 hoot 測試會得到一條永遠靜默 skip 的假覆蓋。

# `/` 之前若是這些字元，代表接下來是 regex literal 而非除號。
_REGEX_PREV_CHARS = set('(,=:[!&|?{};+-*%~^<>')
_REGEX_PREV_WORDS = frozenset({
    'return', 'typeof', 'case', 'in', 'of', 'new', 'delete', 'void', 'throw',
    'do', 'else', 'yield', 'await',
})

# 第三方 vendored 資產。排除規則刻意寫成**具名檔案清單**而不是 ``lib/`` 目錄式
# 過濾：目錄式排除會讓日後任何放進 lib/ 的自家檔案自動繞過整個 gate。搭配
# XSS.4 的封閉性斷言（lib/ 實際內容必須等於這份清單），新增檔案會直接讓測試
# 變紅，逼人在這裡明確裁決它算不算第三方。
_VENDORED_JS = frozenset({
    'paas/lib/purify.min.js',
    'paas/lib/marked.min.js',
})
_VENDOR_DIR = 'paas/lib'

# 只有這兩個消毒模組可以碰 ``markup()``。
_MARKUP_ALLOWED = frozenset({
    'paas/services/html_sanitize.js',
    'paas/services/markdown_parser.js',
})

# 可接受的消毒函式（``.innerHTML =`` 與 ``html:`` 的右手邊都吃這組）。
_SANITISERS = ('safeHtml(', 'parseMarkdown(')

# ``.innerHTML =`` 的具名豁免。key 是「空白正規化後的右手邊原文」而不是行號：
# 行號會被格式化重排改掉（新的假紅），右手邊原文則只有真的改了程式碼才會變。
# 任何**新的**未消毒賦值都不在這份 dict 裡 ⇒ 直接紅。
_INNERHTML_EXEMPT = {
    '`<div class="o_woow_mermaid__svg_wrapper">${svg}</div>`': (
        'mermaid_loader.js：${svg} 來自 window.mermaid.render()。防線是 mermaid '
        '自身的 securityLevel:"strict"（由 XSS.3 配套釘住），不是 safeHtml。'
    ),
    '\'<span class="material-symbols-outlined" style="font-size:16px;'
    'vertical-align:middle;margin-right:4px">warning</span>\' + message': (
        'mermaid_loader.js 的 _showError()：message 全部來自同檔硬編的中文字串，'
        '無使用者可控輸入。'
    ),
}


class JsLexError(RuntimeError):
    """詞法掃描收在非程式碼狀態 —— 掃描器被騙了，必須大聲失敗而非靜默略過。"""


def _lex_js(src):
    """回傳 ``(code, mask)``，兩者都與 src 等長（等長是刻意的，行號才不會漂移）。

    code —— 只把「註解」換成空白。用來做 token 比對（例如找 ``markup(``）。
            這正是為什麼 ``@returns ... markup(), or ""`` 那種 JSDoc 不會被
            誤算成呼叫站點。
    mask —— 註解 + 字串／樣板／regex 的**內容**也換成空白（分隔符保留）。
            用來做結構掃描（括號深度、``,`` ``;`` ``?`` ``:`` 的位置），才不會被
            字串裡的 ``style="font-size:16px;..."`` 這種內容騙走。

    需要原文時一律回頭切 src 本身。
    """
    code = list(src)
    mask = list(src)
    i, n = 0, len(src)
    state = 'code'
    quote = ''
    tpl_expr_depth = 0
    last_sig = ''

    def blank(idx):
        mask[idx] = ' ' if src[idx] != '\n' else '\n'

    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ''
        if state == 'code':
            if c == '/' and nxt in '/*':
                state = 'line_comment' if nxt == '/' else 'block_comment'
                code[i] = code[i + 1] = ' '
                blank(i)
                blank(i + 1)
                i += 2
                continue
            if c in '"\'`':
                state = 'template' if c == '`' else 'string'
                quote = c
                tpl_expr_depth = 0
            elif c == '/':
                j = i - 1
                while j >= 0 and src[j].isspace():
                    j -= 1
                k = j
                while k >= 0 and (src[k].isalnum() or src[k] in '_$'):
                    k -= 1
                if (last_sig == '' or last_sig in _REGEX_PREV_CHARS
                        or src[k + 1:j + 1] in _REGEX_PREV_WORDS):
                    state = 'regex'
            if not c.isspace():
                last_sig = c
            i += 1
            continue

        if state == 'line_comment':
            if c == '\n':
                state = 'code'
            else:
                code[i] = ' '
                blank(i)
            i += 1
            continue

        if state == 'block_comment':
            if c == '*' and nxt == '/':
                code[i] = code[i + 1] = ' '
                blank(i)
                blank(i + 1)
                state = 'code'
                i += 2
                continue
            if c != '\n':
                code[i] = ' '
            blank(i)
            i += 1
            continue

        if state == 'string':
            if c == '\\':
                blank(i)
                if i + 1 < n:
                    blank(i + 1)
                i += 2
                continue
            if c == quote:
                state = 'code'
                last_sig = c
            else:
                blank(i)
            i += 1
            continue

        if state == 'template':
            if c == '\\':
                blank(i)
                if i + 1 < n:
                    blank(i + 1)
                i += 2
                continue
            if c == '$' and nxt == '{':
                tpl_expr_depth += 1
                blank(i)
                blank(i + 1)
                i += 2
                continue
            if c == '}' and tpl_expr_depth > 0:
                tpl_expr_depth -= 1
                blank(i)
                i += 1
                continue
            if c == '`' and tpl_expr_depth == 0:
                state = 'code'
                last_sig = c
            else:
                blank(i)
            i += 1
            continue

        if state == 'regex':
            if c == '\\':
                blank(i)
                if i + 1 < n:
                    blank(i + 1)
                i += 2
                continue
            if c == '[':
                state = 'regex_class'
                blank(i)
            elif c == '/':
                state = 'code'
                last_sig = c
            else:
                blank(i)
            i += 1
            continue

        if state == 'regex_class':
            if c == '\\':
                blank(i)
                if i + 1 < n:
                    blank(i + 1)
                i += 2
                continue
            if c == ']':
                state = 'regex'
            blank(i)
            i += 1
            continue

        raise AssertionError('unreachable lexer state: %s' % state)

    if state != 'code':
        # 大聲失敗，不要靜默略過 —— 掃描器被騙了的話，它給的任何「沒找到問題」
        # 都不可採信（陰性結果需要探針本身是好的）。
        raise JsLexError(
            "詞法掃描結束時停在 %r 狀態（預期 'code'）" % state)
    return ''.join(code), ''.join(mask)


_OPENERS = frozenset('([{')
_CLOSERS = frozenset(')]}')


def _line_of(src, idx):
    return src.count('\n', 0, idx) + 1


def _scan_expr(mask, start):
    """用 mask 從 start 掃到同層的 ``,`` / ``;`` / 多餘 closer，回傳 end_idx。"""
    depth = 0
    i, n = start, len(mask)
    while i < n:
        c = mask[i]
        if c in _OPENERS:
            depth += 1
        elif c in _CLOSERS:
            if depth == 0:
                break
            depth -= 1
        elif c in ',;' and depth == 0:
            break
        i += 1
    return i


def _split_depth0(mask_seg, char):
    depth = 0
    for i, c in enumerate(mask_seg):
        if c in _OPENERS:
            depth += 1
        elif c in _CLOSERS:
            depth -= 1
        elif c == char and depth == 0:
            return i
    return -1


def _ternary_leaves(raw_seg, mask_seg):
    """把（可能巢狀的）三元運算式拆成葉子。raw/mask 必須同長度、同起點。"""
    q = _split_depth0(mask_seg, '?')
    if q == -1:
        return [raw_seg.strip()]
    raw_rest, mask_rest = raw_seg[q + 1:], mask_seg[q + 1:]
    c = _split_depth0(mask_rest, ':')
    if c == -1:
        return [raw_seg.strip()]
    return (_ternary_leaves(raw_rest[:c], mask_rest[:c])
            + _ternary_leaves(raw_rest[c + 1:], mask_rest[c + 1:]))


def _norm(text):
    """空白正規化 —— prettier 重排不該讓守衛變紅。"""
    return ' '.join(text.split())


def _is_pure_literal(expr):
    """整段運算式就是單一個字串／樣板字面量，且無 ``${}`` 插值、無 ``+`` 串接。"""
    e = expr.strip()
    if e[:1] not in ('"', "'", '`') or '${' in e:
        return False
    try:
        _code, mask = _lex_js(e)
    except JsLexError:
        return False
    # 遮完之後只該剩下頭尾兩個分隔符 —— 中間有東西就代表不是「單一字面量」。
    return _norm(mask)[1:-1].strip() == '' and _norm(mask)[-1:] == e[:1]


# --------------------------------------------------------------------------- #
# SSE.1 / SSE.2 — flag default + frontend contract (no HTTP needed)             #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCSseFlagAndFrontendContract(TransactionCase):
    """SSE.1-SSE.2 — gate default + frozen frontend EventSource URL."""

    def test_SSE1_use_agent_runtime_defaults_false(self):
        project = self.env['project.project'].sudo().create(
            {'name': f'sse1-{uuid.uuid4().hex[:6]}'})
        self.assertIn(
            'use_agent_runtime', project._fields,
            "SSE.1: project.use_agent_runtime field missing (Phase A).",
        )
        self.assertFalse(
            project.use_agent_runtime,
            "SSE.1: use_agent_runtime MUST default False — Phase C merge "
            "keeps production chat on the legacy path until Phase D enables "
            "the flag per pilot project.",
        )

    def test_SSE2_frontend_eventsource_url_unchanged(self):
        """SSE.2: ai_service.js still builds /api/ai/stream/{id}?csrf_token=..."""
        js_path = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..',
            'static', 'src', 'paas', 'services', 'ai_service.js',
        ))
        with open(js_path, 'r', encoding='utf-8') as f:
            src = f.read()
        self.assertIn(
            '/api/ai/stream/', src,
            "SSE.2: frontend EventSource URL must remain /api/ai/stream/ "
            "(§5.6 frontend zero-change). The backend SSE rewrite must not "
            "move the entry point.",
        )
        self.assertIn(
            'csrf_token=', src,
            "SSE.2: EventSource URL must keep the csrf_token query param.",
        )

    def test_SSE6_skill_snapshot_carries_description(self):
        """SSE.6 (F-PILOT-10 Gap 3 hybrid): _build_skill_snapshot serialises
        published skills with name/version/sha256 + description (host-owned,
        lets the pod register a StructuredTool description WITHOUT fetching the
        bundle). lock-step guard on the spawn snapshot contract (credential-
        adjacent assembly). parameters/args_schema is intentionally absent
        (runtime-owned; see follow-ups F-PILOT-11)."""
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
            AiAssistantController,
        )
        project = self.env['project.project'].sudo().create(
            {'name': f'sse6-{uuid.uuid4().hex[:6]}'})
        published = self.env['woow_paas_platform.skill'].sudo().create({
            'name': 'sk-pub', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'project_id': project.id, 'state': 'published',
            'description': 'Fetches the weather for a city.',
        })
        # a draft skill must NOT leak into the snapshot
        self.env['woow_paas_platform.skill'].sudo().create({
            'name': 'sk-draft', 'version': '0.1.0', 'bundle_sha256': 'b' * 64,
            'project_id': project.id, 'state': 'draft',
            'description': 'not published',
        })
        template = self.env['woow_paas_platform.agent_template'].sudo().create({
            'name': f'tmpl-{uuid.uuid4().hex[:6]}',
            # bind the project so _check_skill_subset accepts its user-scope skills
            'project_ids': [(6, 0, [project.id])],
            'skill_ids': [(6, 0, (published + self.env['woow_paas_platform.skill']
                                  .sudo().search([('name', '=', 'sk-draft')])).ids)],
        })
        snapshot = AiAssistantController()._build_skill_snapshot(project, template)
        names = [e['name'] for e in snapshot]
        self.assertIn('sk-pub', names,
                      "published project skill must be in the snapshot")
        self.assertNotIn('sk-draft', names,
                         "SSE.6: draft skill must NOT be in the snapshot")
        entry = next(e for e in snapshot if e['name'] == 'sk-pub')
        self.assertEqual(entry['version'], '0.1.0')
        self.assertEqual(entry['sha256'], 'a' * 64)
        self.assertEqual(
            entry['description'], 'Fetches the weather for a city.',
            "SSE.6: snapshot must carry the skill description (Gap 3 hybrid)")


# --------------------------------------------------------------------------- #
# SSE.8 / SSE.9 / SSE.10 — frontend polish contract (adversarial-review        #
#                          medium/low findings, source-level assertions)        #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCSseFrontendPolishContract(TransactionCase):
    """SSE.8-SSE.10 — adversarial-review polish findings, asserted at the
    frontend source level (these are pure JS/manifest contracts that need no
    live pod). Same family as SSE.2's "frozen frontend contract" probe.

    SSE.8  (F1) — pod state → zh-tw label mapping is a SINGLE shared module
           (``services/agent_pod_state.js``), wired into web.assets_frontend,
           imported by BOTH the portal status component AND the /woow
           ProjectKanbanPage. No inline hand-written ``運行中``/``啟動中`` map
           may remain in those two consumers (duplicate-drift was the finding).
    SSE.9  (F2) — the portal task chat must wrap USER message html in the same
           ``safeHtml`` DOMPurify path /woow AiChat uses (defence-in-depth);
           AI messages keep going through ``parseMarkdown`` (already sanitised)
           and must NOT be double-wrapped.
    SSE.10 (F3) — ``openAiStream`` dispatch (the typed-event branch block, not
           just JSON.parse) is inside the try/catch so a handler-time exception
           routes to ``onParseError`` — restoring the original AiChat onmessage
           equivalence (handler faults counted toward parse-error recovery).
    """

    _BASE = ('..', 'static', 'src')

    def _read(self, *parts):
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), *parts))
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    # ----- SSE.8 (F1) shared pod-state label module ----------------------- #
    def test_SSE8_shared_pod_state_label_module(self):
        shared = self._read(
            *self._BASE, 'paas', 'services', 'agent_pod_state.js')
        self.assertIn(
            'export const POD_STATE_LABEL', shared,
            "SSE.8(F1): shared module must export POD_STATE_LABEL.")
        self.assertIn(
            'export function podStateLabel', shared,
            "SSE.8(F1): shared module must export podStateLabel(state) helper.")
        # the merged map must cover every state both consumers can show, incl.
        # spawning + unknown (ProjectKanbanPage's old inline map dropped these).
        for state in ('absent', 'spawning', 'active', 'reaped', 'unknown'):
            self.assertIn(
                state, shared,
                f"SSE.8(F1): shared map missing '{state}' state.")

        # both consumers import from the shared module ...
        portal = self._read(
            *self._BASE, 'portal', 'portal_agent_status.js')
        kanban = self._read(
            *self._BASE, 'paas', 'pages', 'project-kanban',
            'ProjectKanbanPage.js')
        for src, who in ((portal, 'portal_agent_status.js'),
                         (kanban, 'ProjectKanbanPage.js')):
            self.assertIn(
                'agent_pod_state', src,
                f"SSE.8(F1): {who} must import the shared agent_pod_state "
                "module instead of hand-rolling the label map.")
        # ... and no longer carry their OWN inline {active: "運行中"} map.
        self.assertNotIn(
            'const map = { active: "運行中"', kanban,
            "SSE.8(F1): ProjectKanbanPage must drop its inline pod-state map "
            "(the duplicate that drifted, missing spawning/unknown).")
        self.assertNotIn(
            'const POD_STATE_LABEL = {', portal,
            "SSE.8(F1): portal_agent_status must drop its inline "
            "POD_STATE_LABEL map and use the shared module.")

        # shared module is wired into the frontend bundle (portal uses it).
        manifest = self._read('..', '__manifest__.py')
        self.assertIn(
            'services/agent_pod_state.js', manifest,
            "SSE.8(F1): agent_pod_state.js must be listed in "
            "web.assets_frontend (the portal bundle), beside ai_stream.js.")

    # ----- SSE.9 (F2) portal user-message sanitize ------------------------ #
    def test_SSE9_portal_user_message_sanitized(self):
        """portal 聊天室的每個 ``html:`` 值都必須來自消毒函式（正向清單）。

        改寫自 2026-08-02 之前的版本：舊版斷言 ``assertIn('safeHtml(m.body)')``
        與 ``assertIn('safeHtml(res.data.body)')``，兩個方向都壞：

        * 假綠 —— 只釘兩個**已知**站點。新增第三個未消毒的 ``html:`` 它照樣綠。
        * 假紅 —— 把變數 ``m`` 改名成 ``msg``（完全正確的重構）就會變紅，長期
          會訓練出「紅了就改測試字串」的反射。

        現在改成列舉檔案裡**所有** ``html:`` 站點、逐一檢查其值的形態，不比對
        變數名。三元運算子會拆成兩支各自檢查。
        """
        path = os.path.abspath(os.path.join(
            os.path.dirname(__file__), *self._BASE, 'portal',
            'portal_task_chat.js'))
        with open(path, 'r', encoding='utf-8') as f:
            chat = f.read()

        # 陽性對照 ①：檔案真的讀到了。讀成空字串的話，下面每一條迴圈都會 vacuous
        # 通過，輸出與「全部都消毒好了」一模一樣。
        self.assertGreater(
            len(chat), 500,
            f"SSE.9 陽性對照失敗：portal_task_chat.js 只讀到 {len(chat)} 字元。")
        self.assertIn(
            'safeHtml', chat,
            "SSE.9(F2): portal task chat must import + use safeHtml for USER "
            "message html (defence-in-depth, same as /woow AiChat).")

        code, mask = _lex_js(chat)
        sites = []
        for m in re.finditer(r'\bhtml\s*:', code):
            end = _scan_expr(mask, m.end())
            leaves = _ternary_leaves(chat[m.end():end], mask[m.end():end])
            sites.append((_line_of(code, m.start()), [_norm(x) for x in leaves]))

        # 陽性對照 ②：真的找到站點了。找到 0 個時「每個站點都合格」恆真。
        self.assertGreaterEqual(
            len(sites), 4,
            f"SSE.9 陽性對照失敗：只找到 {len(sites)} 個 html: 站點 "
            f"（{sites}），掃描器沒抓到東西，下面的檢查會空轉通過。")

        for line, leaves in sites:
            for leaf in leaves:
                self.assertTrue(
                    leaf.startswith(_SANITISERS),
                    f"SSE.9(F2): portal_task_chat.js:{line} 的 html: 值 "
                    f"{leaf!r} 沒有走消毒函式。每一支（含三元的兩邊）都必須是 "
                    f"{list(_SANITISERS)} 之一開頭。注意判準看的是**形態**不是"
                    "變數名，所以改名不會讓這條紅。")
                # AI 訊息走 parseMarkdown（內含 DOMPurify），不可再包一層。
                self.assertFalse(
                    re.match(r'safeHtml\(\s*parseMarkdown\b', leaf),
                    f"SSE.9(F2): portal_task_chat.js:{line} 把 parseMarkdown "
                    "的結果又包了一次 safeHtml —— parseMarkdown 已經跑過 "
                    "DOMPurify，重複消毒只會多剝一次標籤。")

    # ----- SSE.10 (F3) stream dispatch equivalence ------------------------ #
    def test_SSE10_stream_dispatch_inside_try_catch(self):
        # ⚠ 已知限制（狀態記於 2026-08-02，本次刻意不修）：
        #
        # 這條用「原始碼字元位置的先後」當 try-scope 的代理判準 —— dispatch 的
        # 字元位置若在 catch 之前，就推論它在同一個 try 裡面。這個代理在
        # prettier／任何格式化重排之下會失真（先後順序不變但語意可能已變，反之
        # 亦然），也就是說它守的不是真正的不變式。
        #
        # 為什麼截至 2026-08-02 仍維持原樣：真正的做法是解析出 try 區塊的括號
        # 範圍再斷言 dispatch 落在其中，但手寫的大括號配對器必須正確處理字串、
        # 註解、regex literal 裡的括號 —— 等於用一個容易寫錯的東西當守衛，會把
        # 守衛本身變成新的待修項。而這條與 XSS 無關（它守的是 handler 例外有沒有
        # 被計進 parse-error 復原），不享有「風險隱形」那條優先級。
        #
        # 同一次改動裡 SSE.9 已從字面量比對改寫成形態掃描（見上），本檔頂端有
        # 現成的 _lex_js/_scan_expr 可用；若日後這條開始誤報，那是重做它的時機。
        stream = self._read(*self._BASE, 'paas', 'services', 'ai_stream.js')
        # The catch must route handler-time exceptions to onParseError ...
        self.assertIn(
            'handlers.onParseError?.(event.data, err)', stream,
            "SSE.10(F3): catch must call onParseError(event.data, err).")
        # ... AND the catch must come AFTER the whole dispatch block, so a
        # handler fault is caught too (original AiChat onmessage equivalence).
        # We anchor on the LAST dispatch branch's handler call (onDone) inside
        # the function body — NOT the JSDoc mention. The dispatch onDone call
        # is uniquely written as ``handlers.onDone?.(`` whereas the doc comment
        # writes ``onDone(data)``.
        dispatch_marker = 'handlers.onDone?.('
        self.assertIn(
            dispatch_marker, stream,
            "SSE.10(F3): expected the onDone dispatch branch "
            "(handlers.onDone?.(...)).")
        catch_call_idx = stream.index('handlers.onParseError?.(event.data, err)')
        dispatch_idx = stream.index(dispatch_marker)
        self.assertLess(
            dispatch_idx, catch_call_idx,
            "SSE.10(F3): the typed-event dispatch (through handlers.onDone) "
            "must sit INSIDE the same try whose catch calls onParseError — "
            "i.e. dispatch comes BEFORE the onParseError catch in source. Today "
            "the catch only guards JSON.parse and returns before dispatch, so a "
            "handler-time exception escapes uncounted. Restore the original "
            "AiChat onmessage equivalence by widening the try to cover dispatch.")


# --------------------------------------------------------------------------- #
# XSS.0-XSS.4 — 前端 HTML sink 守衛（全 static/src 範圍）                        #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestFrontendHtmlSinkGuards(TransactionCase):
    """守住實測會造成 XSS 的兩種形態，涵蓋整個 ``static/src``。

    這組取代舊 SSE.9「只釘 portal_task_chat.js 裡兩個字面量」的做法。範圍從
    2 個站點擴大到全部 JS 檔，所以新增的注入點會自動被檢查到。判準不綁變數名，
    所以 ``m`` → ``msg`` 這種正確重構不會變紅。

    檔頭那段長註解說明了為什麼守的是 ``markup()`` 與 ``.innerHTML =``，而不是
    「有沒有呼叫 safeHtml」——以及這仍是靜態檢查、守不住什麼。
    """

    _STATIC_SRC = ('..', 'static', 'src')

    def _base_dir(self):
        return os.path.abspath(os.path.join(
            os.path.dirname(__file__), *self._STATIC_SRC))

    def _iter_js(self, include_vendored=False):
        """走訪 static/src 下的 .js，預設跳過 vendored 第三方檔。"""
        base = self._base_dir()
        for root, _dirs, files in os.walk(base):
            for fn in sorted(files):
                if not fn.endswith('.js'):
                    continue
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, base).replace(os.sep, '/')
                if not include_vendored and rel in _VENDORED_JS:
                    continue
                with open(path, 'r', encoding='utf-8') as f:
                    yield rel, f.read()

    # ----- XSS.0 掃描器自檢（陽性對照）----------------------------------- #
    def test_XSS0_scanner_self_check(self):
        """探針自檢：先證明掃描器真的會動，下面幾條的陰性結果才可採信。

        「找不到未消毒的 sink」與「掃描器壞掉／檔案讀成空字串」輸出長得一模一樣。
        這條就是那個陽性對照。
        """
        # (名稱, 輸入, 剝完必須還在, 剝完必須不見)
        cases = [
            ('行註解', 'const a = 1; // markup(evil)\nconst b = 2;',
             ['const a = 1;', 'const b = 2;'], ['markup(evil)']),
            ('區塊註解', 'x;/* markup(evil)\n innerHTML = bad */y;',
             ['x;', 'y;'], ['markup(evil)', 'innerHTML']),
            ('字串內的 //（URL）', 'const u = "https://a.b/c"; markup(real);',
             ['markup(real)'], []),
            ('字串內的 /*', "const s = '/* x */'; markup(real);",
             ['markup(real)'], []),
            ('regex 含斜線', 'const p = /^a\\/b$/; markup(real);',
             ['markup(real)'], []),
            ('regex 含引號', 'x.replace(/"/g, "&q;"); markup(real);',
             ['markup(real)'], []),
            ('regex class 含斜線', 'const r = /[/"]/g; markup(real);',
             ['markup(real)'], []),
            ('樣板含 //', 'const t = `a//b`; markup(real);',
             ['markup(real)'], []),
            ('除號不是 regex', 'const q = a / b / c; markup(real);',
             ['markup(real)'], []),
        ]
        for name, src, keep, drop in cases:
            code, mask = _lex_js(src)
            self.assertEqual(
                len(code), len(src),
                f"XSS.0[{name}]: code 長度改變，行號會漂移。")
            self.assertEqual(
                len(mask), len(src),
                f"XSS.0[{name}]: mask 長度改變，行號會漂移。")
            for frag in keep:
                self.assertIn(
                    frag, code,
                    f"XSS.0[{name}]: 掃描器把程式碼 {frag!r} 一起吃掉了 —— "
                    "它會漏看真正的 sink。")
            for frag in drop:
                self.assertNotIn(
                    frag, code,
                    f"XSS.0[{name}]: 註解裡的 {frag!r} 沒被剝掉 —— "
                    "會把 JSDoc 誤報成呼叫站點（假紅）。")

        # mask 必須遮掉字串內容，否則 `style="a;b"` 裡的 `;` 會腰斬運算式。
        _code, mask = _lex_js('a.innerHTML = "x;y" + z;')
        self.assertNotIn(
            ';', mask[mask.index('"'):mask.rindex('"')],
            "XSS.0: mask 沒遮掉字串內的 `;`，結構掃描會被字串內容騙走。")

        # 詞法器被騙時必須大聲失敗，不可以靜默回傳。
        with self.assertRaises(JsLexError):
            _lex_js('const s = "未收尾的字串')

        # 掃描器真的讀到檔案了（不是掃了個空目錄）。
        rels = [rel for rel, _src in self._iter_js()]
        self.assertGreater(
            len(rels), 50,
            f"XSS.0: 只掃到 {len(rels)} 個 js 檔，static/src 應遠多於此 —— "
            "路徑錯了或目錄空了，下面所有陰性結果都不可採信。")
        self.assertIn(
            'portal/portal_task_chat.js', rels,
            "XSS.0: 掃描範圍沒涵蓋 portal_task_chat.js。")

    # ----- XSS.1 markup() 只准出現在消毒模組 ------------------------------ #
    def test_XSS1_markup_confined_to_sanitisers(self):
        """``markup()`` 把字串變成 OWL Markup ⇒ t-out 會原樣輸出 HTML。

        2026-08-02 以真 chromium 實測：``markup(未消毒payload)`` 會觸發 XSS，
        而同一份 payload 以裸字串交給 t-out 只會被逃脫成文字。所以「誰可以呼叫
        markup()」就是這條路徑上真正的不變式。
        """
        offenders = []
        importers = []
        for rel, src in self._iter_js():
            code, _mask = _lex_js(src)
            if rel in _MARKUP_ALLOWED:
                continue
            for m in re.finditer(r'\bmarkup\s*\(', code):
                offenders.append(f'{rel}:{_line_of(code, m.start())}')
            for m in re.finditer(
                    r'import\s*\{([^}]*)\}\s*from\s*["\']@odoo/owl["\']', code):
                if re.search(r'\bmarkup\b', m.group(1)):
                    importers.append(f'{rel}:{_line_of(code, m.start())}')

        self.assertFalse(
            offenders,
            "XSS.1: 消毒模組以外出現 markup() 呼叫，這是真的 XSS 向量（實測會"
            f"觸發）。站點：{offenders}。允許清單：{sorted(_MARKUP_ALLOWED)}。"
            "若確有需要，請先讓資料通過 safeHtml()/parseMarkdown()。")
        self.assertFalse(
            importers,
            "XSS.1: 消毒模組以外 import 了 markup —— 即使目前沒呼叫，也等於把"
            f"這個向量攤開。站點：{importers}。")

        # 陽性對照：允許清單裡的檔案確實有呼叫，證明上面的正則抓得到 markup(。
        found_in_allowed = 0
        for rel, src in self._iter_js():
            if rel not in _MARKUP_ALLOWED:
                continue
            code, _mask = _lex_js(src)
            found_in_allowed += len(re.findall(r'\bmarkup\s*\(', code))
        self.assertGreaterEqual(
            found_in_allowed, 2,
            "XSS.1 陽性對照失敗：連消毒模組裡的 markup() 都沒抓到，代表偵測"
            f"正則本身壞了（只抓到 {found_in_allowed} 個），上面的『沒有違規』"
            "是假綠。")

    # ----- XSS.2 .innerHTML = 賦值必須消毒或字面量 ------------------------ #
    def test_XSS2_innerhtml_assignments_sanitised(self):
        """``.innerHTML =`` 是繞過 OWL 自動逃脫的第二個真 XSS 向量。

        2026-08-02 實測：``el.innerHTML = 裸字串payload`` 會觸發 XSS
        （``el.innerHTML = safeHtml(payload)`` 不會）。所以每個賦值的右手邊都要
        是消毒函式、純字面量，或列在 ``_INNERHTML_EXEMPT`` 具名豁免裡。
        """
        sites = []
        offenders = []
        for rel, src in self._iter_js():
            code, mask = _lex_js(src)
            for m in re.finditer(r'\.innerHTML\s*=(?!=)', code):
                end = _scan_expr(mask, m.end())
                expr = _norm(src[m.end():end])
                where = f'{rel}:{_line_of(code, m.start())}'
                sites.append(where)
                if expr.startswith(_SANITISERS):
                    continue
                if _is_pure_literal(expr):
                    continue
                if expr in _INNERHTML_EXEMPT:
                    continue
                offenders.append((where, expr[:120]))

        self.assertFalse(
            offenders,
            "XSS.2: 出現未消毒的 .innerHTML 賦值（實測這個形態會觸發 XSS）。"
            f"站點：{offenders}。右手邊必須是 safeHtml(...)／parseMarkdown(...)、"
            "純字面量，或在 _INNERHTML_EXEMPT 具名豁免（豁免要附理由，key 是"
            "空白正規化後的右手邊原文）。")

        # 陽性對照：確實掃到了賦值站點。掃到 0 個和「全部都安全」輸出一樣。
        self.assertGreaterEqual(
            len(sites), 5,
            f"XSS.2 陽性對照失敗：只掃到 {len(sites)} 個 .innerHTML 賦值站點"
            f"（{sites}），偵測正則或遍歷壞了，『沒有違規』是假綠。")

        # 豁免清單不得留下已不存在的殭屍條目（否則它會默默擴大授權面）。
        live = set()
        for rel, src in self._iter_js():
            code, mask = _lex_js(src)
            for m in re.finditer(r'\.innerHTML\s*=(?!=)', code):
                live.add(_norm(src[m.end():_scan_expr(mask, m.end())]))
        stale = sorted(set(_INNERHTML_EXEMPT) - live)
        self.assertFalse(
            stale,
            f"XSS.2: _INNERHTML_EXEMPT 有已不存在的殭屍豁免 {stale} —— "
            "請刪掉，留著只會擴大授權面。")

    # ----- XSS.3 mermaid 豁免的配套防線 ----------------------------------- #
    def test_XSS3_mermaid_strict_security_level(self):
        """XSS.2 放行 mermaid 的 ``${svg}`` 插值，靠的是這條配套斷言。

        mermaid 自己在 ``securityLevel: "strict"`` 下會消毒圖表內容。豁免與這條
        是一組的：把 strict 拿掉，XSS.2 的豁免理由就不成立，所以這裡必須紅。
        """
        path = os.path.join(self._base_dir(), 'paas', 'services',
                            'mermaid_loader.js')
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        code, _mask = _lex_js(src)

        inits = len(re.findall(r'\bmermaid\s*\.\s*initialize\s*\(', code))
        stricts = len(re.findall(
            r'securityLevel\s*:\s*["\']strict["\']', code))
        self.assertGreater(
            inits, 0,
            "XSS.3 陽性對照失敗：mermaid_loader.js 裡找不到 mermaid.initialize("
            " —— 偵測正則壞了或檔案結構變了。")
        self.assertEqual(
            stricts, inits,
            f"XSS.3: mermaid.initialize( 有 {inits} 處，但 "
            f"securityLevel:\"strict\" 只有 {stricts} 處 —— 有初始化路徑沒設"
            "strict。XSS.2 對 ${svg} 的豁免就是建立在 strict 之上，這裡漏一處"
            "等於那個豁免失效。")

    # ----- XSS.4 vendored 排除清單的封閉性 -------------------------------- #
    def test_XSS4_vendored_lib_exclusion_is_closed(self):
        """排除清單必須是封閉的，否則往 lib/ 丟檔案就能繞過整個 gate。

        這條刻意不是「排除 lib/ 目錄」而是「排除具名的兩個檔」：新增任何檔案到
        lib/ 都會讓這條紅，逼人明確裁決它是第三方資產還是自家程式碼。
        """
        lib_dir = os.path.join(self._base_dir(), *_VENDOR_DIR.split('/'))
        self.assertTrue(
            os.path.isdir(lib_dir),
            f"XSS.4 陽性對照失敗：{_VENDOR_DIR} 不存在，排除清單無從比對。")
        actual = {
            f'{_VENDOR_DIR}/{fn}'
            for fn in os.listdir(lib_dir) if fn.endswith('.js')
        }
        self.assertEqual(
            actual, set(_VENDORED_JS),
            f"XSS.4: {_VENDOR_DIR} 的實際內容與 _VENDORED_JS 排除清單不一致。"
            f"實際={sorted(actual)}，清單={sorted(_VENDORED_JS)}。新增的檔案若"
            "是自家程式碼，請搬出 lib/ 讓 gate 掃得到；若真是第三方資產，才把它"
            "加進 _VENDORED_JS。")

        # 陽性對照：直接驗「排除機制本身」有作用 —— 預設遍歷看不到 vendored 檔，
        # 打開 include_vendored 就看得到。刻意不去斷言 purify.min.js 的內容長怎樣：
        # 那會把第三方檔的當下內容寫死成契約，DOMPurify 一升版就無關地變紅。
        default_rels = {rel for rel, _src in self._iter_js()}
        all_rels = {rel for rel, _src in self._iter_js(include_vendored=True)}
        self.assertEqual(
            all_rels - default_rels, set(_VENDORED_JS),
            "XSS.4 陽性對照失敗：預設遍歷與含 vendored 遍歷的差集不等於排除"
            f"清單（差集={sorted(all_rels - default_rels)}）—— 排除機制沒在作用，"
            "或者它排掉了不該排的東西。")
        self.assertTrue(
            default_rels,
            "XSS.4 陽性對照失敗：預設遍歷是空的。")

    # ----- XSS.5 其餘 HTML sink 一律不使用 -------------------------------- #
    def test_XSS5_other_html_sinks_unused(self):
        """``.innerHTML`` 不是唯一能繞過 OWL 逃脫的 API，其餘一律不准用。

        截至 2026-08-02，自家程式碼裡這些 API 是 0 命中，所以直接禁用是免費的
        ——而且比「等有人加了再來補判準」便宜。真的需要其中一個時，請回到這裡
        把它加進判準（想清楚右手邊要怎麼消毒），而不是繞過這條。
        """
        # 每個 pattern 都配一個「一定要命中」的合成樣本。刻意不拿 purify.min.js
        # 當對照物：實測它只是提到 outerHTML 一次、一個 banned 形態都沒有，拿它
        # 當對照會讓這條在乾淨的程式碼上恆紅；而且對照物若綁在第三方檔的當下
        # 內容上，DOMPurify 一升版就會無關地變紅。
        banned = {
            'outerHTML': (r'\.outerHTML\s*=(?!=)', 'el.outerHTML = x;'),
            'insertAdjacentHTML': (r'\.insertAdjacentHTML\s*\(',
                                   'el.insertAdjacentHTML("beforeend", x);'),
            'document.write': (r'\bdocument\s*\.\s*write(?:ln)?\s*\(',
                               'document.write(x);'),
            'srcdoc': (r'\bsrcdoc\b', 'frame.srcdoc = x;'),
            'eval': (r'(?<![.\w])eval\s*\(', 'eval(x);'),
            'new Function': (r'\bnew\s+Function\s*\(', 'const f = new Function(x);'),
        }

        # 陽性對照：先證明每一條正則都真的抓得到它要抓的東西。少了這步，
        # 「自家程式碼 0 命中」與「正則全寫錯」的輸出完全一樣。
        for name, (pattern, sample) in banned.items():
            self.assertRegex(
                sample, pattern,
                f"XSS.5 陽性對照失敗：{name} 的正則連自己的樣本 {sample!r} 都"
                "抓不到，下面的『0 命中』是假綠。")
            # 反向：正則不該亂咬（例如 eval 不可誤傷 el.evaluate / myeval）
            for decoy in ('el.evaluate(x);', 'myeval(x);', 'const innerHTML = 1;'):
                if re.search(pattern, decoy):
                    self.fail(
                        f"XSS.5: {name} 的正則誤咬了 {decoy!r} —— 會產生假紅。")

        offenders = []
        scanned = 0
        for rel, src in self._iter_js():
            code, _mask = _lex_js(src)
            scanned += 1
            for name, (pattern, _sample) in banned.items():
                for m in re.finditer(pattern, code):
                    offenders.append(f'{name} @ {rel}:{_line_of(code, m.start())}')
        self.assertGreater(
            scanned, 50,
            f"XSS.5 陽性對照失敗：只掃到 {scanned} 個檔案，遍歷壞了。")
        self.assertFalse(
            offenders,
            f"XSS.5: 用到了被禁用的 HTML/程式碼注入 sink：{offenders}。這些都能"
            "繞過 OWL 的自動逃脫（或直接執行字串）。若真有需要，請在本測試的 "
            "banned 判準裡明確處理，不要只是把它從清單刪掉。")


# --------------------------------------------------------------------------- #
# SSE.3 / SSE.4 / SSE.5 — dual-mode dispatch behaviour                          #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCSseDualMode(HttpCase):
    """SSE.3-SSE.5 — csrf ordering + flag-keyed dispatch."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Project = cls.env['project.project']
        cls.Channel = cls.env['discuss.channel']
        cls.Task = cls.env['project.task']
        cls.admin = cls.env.ref('base.user_admin')

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.project = self.Project.sudo().create(
            {'name': f'sse-{self.suffix}'})
        # Channel with admin as member so _check_channel_access passes.
        self.channel = self.Channel.sudo().create({
            'name': f'sse-ch-{self.suffix}',
            'channel_type': 'channel',
            'channel_partner_ids': [(4, self.admin.partner_id.id)],
        })
        # Task binds channel → project so the SSE generator resolves project.
        # ai_auto_reply=False makes the "no opted-in assistant" premise explicit:
        # project.task.ai_auto_reply defaults to True, and the sandbox full-module
        # DB seeds default_ai_assistant_id (create.sh), so a default-True task
        # would resolve the seeded assistant → 'no_message' instead of the legacy
        # 'no_agent' this test locks (F-ARC-1 / gitea #131).
        self.task = self.Task.sudo().create({
            'name': f'sse-task-{self.suffix}',
            'project_id': self.project.id,
            'channel_id': self.channel.id,
            'ai_auto_reply': False,
        })

    # ----- helpers -------------------------------------------------------- #
    def _csrf_token(self):
        """Fetch a csrf token bound to the current authenticated session.

        Parsed from the rendered app bootstrap (odoo.csrf_token = "...").
        Asserts a token was found so a csrf-extraction problem surfaces
        loudly instead of being mistaken for the SSE red.
        """
        # Odoo csrf tokens are "<hmachex>o<expiryhex>" — capture the WHOLE
        # quoted value (a [0-9a-f]+ class truncates at the 'o' separator and
        # yields a malformed token that validate_csrf rejects).
        for page in ('/woow', '/web'):
            resp = self.url_open(page, timeout=15)
            if resp.status_code != 200:
                continue
            m = re.search(r'csrf_token[\"\']?\s*[:=]\s*[\"\']([^\"\']+)[\"\']',
                          resp.text)
            if m:
                return m.group(1)
        self.fail(
            "csrf token could not be extracted from /woow or /web — SSE "
            "behavioural tests need a valid token (this is a fixture issue, "
            "not the feature red).",
        )

    def _open_stream(self, csrf_token):
        url = f'/api/ai/stream/{self.channel.id}?csrf_token={csrf_token}'
        return self.url_open(url, timeout=20)

    def _first_sse_frame(self, resp):
        """Parse the first ``data: {json}`` SSE frame → dict."""
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith('data:'):
                return json.loads(line[len('data:'):].strip())
        return {}

    # ----- SSE.3 csrf gate first (regression) ----------------------------- #
    def test_SSE3_csrf_gate_first_regardless_of_flag(self):
        self.project.sudo().write({'use_agent_runtime': True})
        self.authenticate('admin', 'admin')
        # No csrf_token query param → must be rejected at the csrf gate.
        resp = self.url_open(
            f'/api/ai/stream/{self.channel.id}', timeout=15)
        self.assertEqual(resp.status_code, 200)
        frame = self._first_sse_frame(resp)
        self.assertEqual(
            frame.get('error_code'), 'csrf_error',
            "SSE.3: the CSRF gate must run FIRST — a request without a valid "
            "csrf_token returns 'csrf_error' before any flag/project "
            "resolution (security ordering preserved by the rewrite). Got "
            "frame=%r." % frame,
        )

    # ----- SSE.4 flag True → runtime branch (RED) ------------------------- #
    def test_SSE4_flag_true_takes_runtime_branch(self):
        self.project.sudo().write({'use_agent_runtime': True})
        self.authenticate('admin', 'admin')
        token = self._csrf_token()
        resp = self._open_stream(token)
        self.assertEqual(resp.status_code, 200)
        frame = self._first_sse_frame(resp)
        self.assertNotEqual(
            frame.get('error_code'), 'no_agent',
            "SSE.4: with use_agent_runtime=True the endpoint must take the "
            "agent-runtime branch (§5.6), NOT the legacy assistant lookup. "
            "Today there is no flag branch so a flag-True project still hits "
            "the legacy path → 'no_agent'. Post-impl the runtime branch "
            "yields a different code (no_template / project_access_denied / "
            "spawn_failed). Got frame=%r." % frame,
        )

    # ----- SSE.5 flag False → legacy path (regression) -------------------- #
    def test_SSE5_flag_false_keeps_legacy_path(self):
        # Default flag False (do not write it) — channel has no assistant.
        self.assertFalse(self.project.use_agent_runtime)
        self.authenticate('admin', 'admin')
        token = self._csrf_token()
        resp = self._open_stream(token)
        self.assertEqual(resp.status_code, 200)
        frame = self._first_sse_frame(resp)
        self.assertEqual(
            frame.get('error_code'), 'no_agent',
            "SSE.5: with use_agent_runtime=False (default) the endpoint must "
            "keep the legacy assistant path — a channel with no opted-in "
            "assistant yields 'no_agent'. This locks production behaviour "
            "unchanged while the flag is off. Got frame=%r." % frame,
        )


# --------------------------------------------------------------------------- #
# SSE.7 — #129 error envelope → browser SSE forwarding (code + retryable)       #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCSseErrorForwarding(TransactionCase):
    """SSE.7 — agent-runtime §4a error envelope must reach the browser with its
    additive ``code`` (→ error_code, friendly-message lookup) and ``retryable``
    (→ retry affordance). Two-layer lock-step with paas-agent-runtime #129: the
    runtime added these fields; the SSE gateway must forward them or the browser
    only ever sees the raw English ``error`` and cannot distinguish a retryable
    empty-response from a terminal failure.

    Pure-function probes on ``AiAssistantController._agent_error_sse_data`` — the
    full WS→SSE relay needs a live pod, but the mapping that introduced the gap
    is a pure dict transform and is unit-testable in isolation.
    """

    def _map(self, envelope):
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
            AiAssistantController,
        )
        return AiAssistantController._agent_error_sse_data(envelope)

    def test_SSE7_empty_response_forwards_code_and_retryable(self):
        """llm_empty_response（#129 round-2 空串流）→ 帶 error_code + retryable=True。"""
        data = self._map({
            'type': 'error',
            'payload': {
                'error': 'LLM returned an empty response',
                'code': 'llm_empty_response',
                'fatal': True,
                'retryable': True,
            },
        })
        self.assertEqual(data['error'], 'LLM returned an empty response')
        self.assertEqual(
            data.get('error_code'), 'llm_empty_response',
            "SSE.7: runtime payload.code MUST forward as error_code so the "
            "frontend ERROR_MESSAGES lookup + friendly zh-tw message works. "
            "Got %r." % data,
        )
        self.assertIs(
            data.get('retryable'), True,
            "SSE.7: payload.retryable MUST forward so the browser shows 「重試」. "
            "Got %r." % data,
        )
        self.assertTrue(data['done'])

    def test_SSE7_non_retryable_error_omits_retryable(self):
        """llm_error（不可重試）→ 帶 error_code 但不帶 retryable（前端不誤導重試）。"""
        data = self._map({
            'type': 'error',
            'payload': {'error': 'boom', 'code': 'llm_error', 'fatal': True},
        })
        self.assertEqual(data.get('error_code'), 'llm_error')
        self.assertNotIn(
            'retryable', data,
            "SSE.7: when runtime omits retryable, the SSE frame must not invent "
            "it (additive-only). Got %r." % data,
        )

    def test_SSE7_missing_payload_degrades_safely(self):
        """缺 payload / 非 dict → 退回最小可見 frame，不 crash、不靜默。"""
        for envelope in ({'type': 'error'}, {'type': 'error', 'payload': None}, {}):
            data = self._map(envelope)
            self.assertEqual(data.get('error'), 'Agent runtime error')
            self.assertTrue(data['done'])
            self.assertNotIn('error_code', data)
            self.assertNotIn('retryable', data)
