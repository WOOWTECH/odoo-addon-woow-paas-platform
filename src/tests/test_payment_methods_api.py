"""Tests for /api/billing/payment-methods/* wrapper endpoints."""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

import requests

from odoo.modules import get_module_path
from odoo.tests.common import HttpCase, TransactionCase

from odoo.addons.woow_paas_platform.controllers.billing import (
    _sanitize_phone_for_ecpay,
)


class TestSanitizePhoneForEcpay(TransactionCase):
    """Unit tests for the phone sanitization helper."""

    def test_strips_plus_dash_space_parens(self):
        self.assertEqual(_sanitize_phone_for_ecpay('+1 555-555-5555'), '15555555555')
        self.assertEqual(_sanitize_phone_for_ecpay('(02) 2655-1775'), '0226551775')

    def test_returns_empty_for_none_or_blank(self):
        self.assertEqual(_sanitize_phone_for_ecpay(None), '')
        self.assertEqual(_sanitize_phone_for_ecpay(''), '')
        self.assertEqual(_sanitize_phone_for_ecpay('   '), '')

    def test_returns_empty_when_too_few_digits(self):
        # Less than 7 digits is unlikely to be a real phone; prefer empty.
        self.assertEqual(_sanitize_phone_for_ecpay('ext. 123'), '')

    def test_preserves_plausible_taiwan_mobile(self):
        self.assertEqual(_sanitize_phone_for_ecpay('0912-345-678'), '0912345678')


def _read_asset(*parts):
    path = Path(get_module_path('woow_paas_platform')).joinpath(*parts)
    return path.read_text(encoding='utf-8')


# JavaScript regex literal 只能出現在「這裡需要一個值」的位置。這組關鍵字後面的
# `/` 必定是 regex literal 起始而不是除號。
_JS_REGEX_PRECEDING_KEYWORDS = frozenset({
    'await', 'case', 'delete', 'do', 'else', 'in', 'instanceof', 'new',
    'of', 'return', 'throw', 'typeof', 'void', 'yield',
})


def _js_quoted_literal_end(source, index):
    """回傳從 ``index`` 的引號起算、該字串字面量結束後的位置。"""
    quote = source[index]
    end = index + 1
    while end < len(source):
        if source[end] == '\\':
            end = min(end + 2, len(source))
            continue
        end += 1
        if source[end - 1] == quote:
            break
    return end


def _js_slash_starts_regex(source, index):
    """判斷 ``index`` 上的 ``/`` 是 regex literal 起始還是除號。

    JS 的經典歧義：只有前一個有意義 token 能決定語意。這裡採保守判準——凡是
    「可以結束一個值」的 token（識別字、數字、``)``、``]``、字串結尾）之後一律
    視為除號；其餘（運算子、分隔符、以及後面必接值的關鍵字）才視為 regex。
    判錯的代價是可觀測的：真實前端檔那格陰性對照會立刻紅。
    """
    cursor = index - 1
    while cursor >= 0 and source[cursor].isspace():
        cursor -= 1
    if cursor < 0:
        return True
    char = source[cursor]
    if char in ')]' or char in '"\'`':
        return False
    if char.isalnum() or char in '_$':
        start = cursor + 1
        while start > 0 and (
            source[start - 1].isalnum() or source[start - 1] in '_$'
        ):
            start -= 1
        return source[start:cursor + 1] in _JS_REGEX_PRECEDING_KEYWORDS
    return True


def _js_regex_literal_end(source, index):
    """回傳 regex literal（含 flags）結束後的位置；不像 regex 則回 ``-1``。

    regex literal 不能跨行，所以碰到換行仍未收尾就判定「不是 regex」。這個安全閥
    讓誤判只會退回舊行為（把 ``/`` 當普通字元），不會把後面整段程式碼吃掉。
    """
    cursor = index + 1
    in_character_class = False
    while cursor < len(source):
        char = source[cursor]
        if char == '\n':
            return -1
        if char == '\\':
            cursor += 2
            continue
        if in_character_class:
            if char == ']':
                in_character_class = False
        elif char == '[':
            in_character_class = True
        elif char == '/':
            cursor += 1
            while cursor < len(source) and source[cursor].isalpha():
                cursor += 1
            return cursor
        cursor += 1
    return -1


def _js_lexical_code(source):
    """Mark JavaScript code, excluding comments, strings and regex literals.

    regex literal 一定要一起 mask 掉（#983 盲區 2）：``/^\\}$/`` 裡的大括號本來會
    被當成真的 block 邊界，讓 brace depth 憑空位移，於是巢狀的 cleanup 讀起來像
    top-level 的共用 cleanup。
    """
    is_code = bytearray(b'\x01') * len(source)
    index = 0
    while index < len(source):
        if source.startswith('//', index):
            end = source.find('\n', index + 2)
            end = len(source) if end < 0 else end
        elif source.startswith('/*', index):
            closing = source.find('*/', index + 2)
            end = len(source) if closing < 0 else closing + 2
        elif source[index] in ('"', "'", '`'):
            end = _js_quoted_literal_end(source, index)
        elif source[index] == '/' and _js_slash_starts_regex(source, index):
            end = _js_regex_literal_end(source, index)
            if end < 0:
                index += 1
                continue
        else:
            index += 1
            continue
        is_code[index:end] = b'\x00' * (end - index)
        index = end
    return is_code


def _find_js_code(source, needle, start=0, is_code=None):
    """Find ``needle`` whose first character is lexical JavaScript code."""
    is_code = is_code or _js_lexical_code(source)
    index = source.find(needle, start)
    while index >= 0 and not is_code[index]:
        index = source.find(needle, index + 1)
    return index


def _extract_js_block_span(source, anchor, start=0):
    """Return a lexical JavaScript block after ``anchor`` and its brace span."""
    is_code = _js_lexical_code(source)
    anchor_index = _find_js_code(source, anchor, start, is_code)
    if anchor_index < 0:
        return '', -1, -1
    opening = _find_js_code(source, '{', anchor_index + len(anchor), is_code)
    if opening < 0:
        return '', -1, -1
    depth = 0
    for index in range(opening, len(source)):
        if not is_code[index]:
            continue
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[opening + 1:index], opening, index
    return '', -1, -1


def _js_code_depths(source, needle):
    """Return lexical brace depths for every code occurrence of ``needle``."""
    is_code = _js_lexical_code(source)
    depths = []
    depth = 0
    cursor = 0
    while cursor < len(source):
        found = _find_js_code(source, needle, cursor, is_code)
        stop = len(source) if found < 0 else found
        for index in range(cursor, stop):
            if not is_code[index]:
                continue
            if source[index] == '{':
                depth += 1
            elif source[index] == '}':
                depth -= 1
        if found < 0:
            break
        depths.append((found, depth))
        cursor = found + len(needle)
    return depths


def _assigned_error_message(block, is_code=None):
    """在**程式碼位置**上尋找 ``this.state.error`` 的字面量指派。

    只認 lexer 判定為 code 的起點（#983 盲區 1）：指派若只活在 comment 或字串
    字面量裡，這裡一律看不到。訊息本體位在字串字面量內（被 mask 成非 code）是
    預期的——被要求必須是 code 的是**指派語句的起點**，不是訊息文字本身。
    """
    if is_code is None:
        is_code = _js_lexical_code(block)
    pattern = re.compile(
        r'''this\.state\.error\s*=\s*_t\(\s*(["'])(?P<message>.*?)\1\s*\)''',
        re.DOTALL,
    )
    index = _find_js_code(block, 'this.state.error', 0, is_code)
    while index >= 0:
        match = pattern.match(block, index)
        if match:
            return match.group('message')
        index = _find_js_code(block, 'this.state.error', index + 1, is_code)
    return None


def _statement_starts_unconditionally(source, index, is_code):
    """``index`` 上的呼叫是不是自己那句 statement 的開頭。

    #983 盲區 3：``if (x) call();``、``x && call();``、``x ? call() : y`` 都不會
    改變 brace depth，所以只看 depth 抓不到。改看「從上一個 statement 邊界
    （code 位置的 ``;`` / ``{`` / ``}``）到呼叫起點之間還剩什麼」——真的無條件
    執行時那段只會是空白（comment 已被 lexer mask 成空白）。任何條件、短路、
    三元運算子、arrow wrapper 都必然在這段留下非空白字元，所以抓得到。
    """
    cursor = index - 1
    while cursor >= 0:
        if is_code[cursor] and source[cursor] in ';{}':
            break
        cursor -= 1
    prefix = ''.join(
        source[position] if is_code[position] else ' '
        for position in range(cursor + 1, index)
    )
    return not prefix.strip()


def _code_keyword_positions(source, is_code, keyword):
    """回傳 ``keyword`` 在程式碼位置上的所有出現位置（整字比對）。"""
    return [
        match.start()
        for match in re.finditer(r'\b%s\b' % re.escape(keyword), source)
        if is_code[match.start()]
    ]


def _guarded_has_only_known_branches(guarded, spans, is_code):
    """guarded 區塊裡除了三個已知分支，不得再有第四個分支。

    #983 盲區 4：原本的迴圈硬編死 ``('ok', 'failed', 'error')``，對 guarded 區塊
    裡任何第四個 else / fallback 分支既不抽取也不檢查。改成「消費掉三個已知分支
    之後，對剩下沒被消費的文字做掃描」：
      - 相鄰兩個已知分支之間的間隙，只能是一個 ``else`` 加空白；
      - 三分支鏈以外的地方不得出現 code 位置的 ``else``（scope creep 的形狀）；
      - 三分支以外不得再出現 ``bindStatus ===`` 比對（沒掛 else 的平行分支）。
    """
    ordered = [spans[status] for status in ('ok', 'failed', 'error')]
    if any(anchor < 0 or closing < 0 for anchor, _opening, closing in ordered):
        return False
    for previous, following in zip(ordered, ordered[1:]):
        if not previous[0] < previous[2] < following[0]:
            return False
    gaps = [
        (previous[2] + 1, following[0])
        for previous, following in zip(ordered, ordered[1:])
    ]
    for start, stop in gaps:
        gap = ''.join(
            guarded[position] if is_code[position] else ' '
            for position in range(start, stop)
        )
        if re.sub(r'\belse\b', ' ', gap, count=1).strip():
            return False
    chain_start, chain_stop = ordered[0][0], ordered[-1][2]
    for position in _code_keyword_positions(guarded, is_code, 'else'):
        if not chain_start <= position <= chain_stop:
            return False
        if any(start <= position < stop for start, stop in gaps):
            continue
        if not any(
            anchor <= position <= closing
            for anchor, _opening, closing in ordered
        ):
            return False
    comparison = 'bindStatus ==='
    index = _find_js_code(guarded, comparison, 0, is_code)
    while index >= 0:
        if not any(
            anchor <= index <= closing
            for anchor, _opening, closing in ordered
        ):
            return False
        index = _find_js_code(guarded, comparison, index + 1, is_code)
    return True


def _bind_frontend_contract(source):
    """Analyse the known callback states and their lexical common cleanup.

    吃的是**原始碼字串**、不讀檔：真實前端檔與合成破壞樣本共用這同一份判準，
    所以合成陽性對照不可能是「另寫一份判準」的自我實現假綠。
    """
    callback, _, _ = _extract_js_block_span(source, 'onMounted(async () =>')
    guarded, _, _ = _extract_js_block_span(
        callback, 'if (["ok", "failed", "error"].includes(bindStatus))',
    )
    guarded_is_code = _js_lexical_code(guarded)
    blocks = {}
    spans = {}
    for status in ('ok', 'failed', 'error'):
        anchor = f'if (bindStatus === "{status}")'
        anchor_index = _find_js_code(guarded, anchor, 0, guarded_is_code)
        block, opening, closing = _extract_js_block_span(guarded, anchor)
        blocks[status] = block
        spans[status] = (anchor_index, opening, closing)

    cleanup_depths = _js_code_depths(
        guarded, 'window.history.replaceState',
    )
    cleanup_index = cleanup_depths[0][0] if len(cleanup_depths) == 1 else -1
    cleanup_is_lexically_common = cleanup_depths == [(cleanup_index, 0)]
    return {
        'ok_feedback': _find_js_code(
            blocks['ok'], 'this.state.bindSuccess = true',
        ) >= 0,
        'failed_message': _assigned_error_message(blocks['failed']),
        'error_message': _assigned_error_message(blocks['error']),
        'cleanup_common': (
            cleanup_is_lexically_common
            and cleanup_index > spans['error'][2] >= 0
            and _statement_starts_unconditionally(
                guarded, cleanup_index, guarded_is_code,
            )
        ),
        'known_branches_only': _guarded_has_only_known_branches(
            guarded, spans, guarded_is_code,
        ),
    }


class TestPaymentMethodBindFrontendContract(TransactionCase):
    """Source-level guard: this repo has no JavaScript runtime test framework."""

    def _sources(self):
        js = _read_asset(
            'static', 'src', 'paas', 'pages', 'settings',
            'PaymentMethodsListPage.js',
        )
        xml = ET.fromstring(_read_asset(
            'static', 'src', 'paas', 'pages', 'settings',
            'PaymentMethodsListPage.xml',
        ))
        return js, xml

    def test_ok_bind_status_has_visible_feedback(self):
        js, template = self._sources()
        contract = _bind_frontend_contract(js)
        self.assertTrue(
            contract['ok_feedback'],
            "bind_status=ok must assign the visible success state",
        )
        success_branches = [
            node for node in template.iter('t')
            if node.get('t-if') == 'state.bindSuccess'
            and any(child.get('role') == 'alert' for child in node.iter('div'))
        ]
        self.assertEqual(len(success_branches), 1)

    def test_failed_bind_status_has_visible_feedback(self):
        js, template = self._sources()
        contract = _bind_frontend_contract(js)
        self.assertIn(
            'Card binding failed', contract['failed_message'] or '',
            "bind_status=failed must assign a dedicated visible failure message",
        )
        error_branches = [
            node for node in template.iter('t')
            if node.get('t-if') == 'state.error'
            and any(child.get('role') == 'alert' for child in node.iter('div'))
        ]
        self.assertEqual(
            len(error_branches), 1,
            "The state.error feedback surface must remain a visible alert banner",
        )

    def test_error_bind_status_has_distinct_visible_feedback(self):
        js, _template = self._sources()
        contract = _bind_frontend_contract(js)
        self.assertIn(
            "couldn't confirm", contract['error_message'] or '',
            "bind_status=error must assign a visible processing-error message",
        )
        self.assertNotEqual(
            contract['error_message'], contract['failed_message'],
            "Business failure and processing error need distinguishable feedback",
        )

    def test_all_known_bind_statuses_share_query_cleanup(self):
        js, _template = self._sources()
        contract = _bind_frontend_contract(js)
        self.assertTrue(contract['ok_feedback'])
        self.assertTrue(contract['failed_message'])
        self.assertTrue(contract['error_message'])
        self.assertTrue(
            contract['cleanup_common'],
            "ok/failed/error must reach one lexical top-level cleanup after "
            "their branch chain",
        )
        self.assertTrue(
            contract['known_branches_only'],
            "The guarded block must hold exactly the ok/failed/error chain; "
            "any extra fallback branch is unreviewed callback scope creep",
        )

    def test_contract_probe_distinguishes_common_from_ok_only_cleanup(self):
        """Synthetic +/- controls keep this source guard from token-scan false green."""
        positive = '''
            onMounted(async () => {
                const bindStatus = q.bind_status;
                if (["ok", "failed", "error"].includes(bindStatus)) {
                    const stringBrace = "}";
                    // Comment braces must not close the callback: { }
                    /* Nor may block-comment braces alter nesting: { } */
                    if (bindStatus === "ok") {
                        this.state.bindSuccess = true;
                    } else if (bindStatus === "failed") {
                        this.state.error = _t("Card binding failed.");
                    } else if (bindStatus === "error") {
                        this.state.error = _t("We couldn't confirm the card binding.");
                    }
                    window.history.replaceState(null, "", cleanHash);
                }
            });
        '''
        ok_only_cleanup = '''
            onMounted(async () => {
                const bindStatus = q.bind_status;
                if (["ok", "failed", "error"].includes(bindStatus)) {
                    if (bindStatus === "ok") {
                        this.state.bindSuccess = true;
                        window.history.replaceState(null, "", cleanHash);
                    } else if (bindStatus === "failed") {
                        this.state.error = _t("Card binding failed.");
                    } else if (bindStatus === "error") {
                        this.state.error = _t("We couldn't confirm the card binding.");
                    }
                }
            });
        '''
        self.assertTrue(_bind_frontend_contract(positive)['cleanup_common'])
        self.assertFalse(
            _bind_frontend_contract(ok_only_cleanup)['cleanup_common'],
            "Negative control has every expected token but cleanup is ok-only",
        )

    def test_contract_probe_rejects_conditionally_nested_cleanup(self):
        conditional_cleanup = '''
            onMounted(async () => {
                const bindStatus = q.bind_status;
                if (["ok", "failed", "error"].includes(bindStatus)) {
                    if (bindStatus === "ok") {
                        this.state.bindSuccess = true;
                    } else if (bindStatus === "failed") {
                        this.state.error = _t("Card binding failed.");
                    } else if (bindStatus === "error") {
                        this.state.error = _t("We couldn't confirm the card binding.");
                    }
                    if (shouldCleanup) {
                        window.history.replaceState(null, "", cleanHash);
                    }
                }
            });
        '''
        self.assertFalse(
            _bind_frontend_contract(conditional_cleanup)['cleanup_common'],
            "Cleanup nested in an extra condition is not common cleanup",
        )

    # ── #983：四種靜態守衛盲區的合成陽性對照 ─────────────────────────
    # 每個樣本只注入一種盲區，其餘部分刻意寫成合法的三分支 + 共用 cleanup。
    # 所以「這格紅了」只能歸因到那一種盲區，而不是樣本本身寫壞。
    # 修好之前這四格全部**錯誤地通過**（RED 見 issue #983）。

    def test_guard_rejects_comment_only_feedback(self):
        """盲區 1：feedback 只存在於 comment / 字串字面量裡也算數。"""
        comment_only_feedback = '''
            onMounted(async () => {
              const bindStatus = q.bind_status;
              if (["ok", "failed", "error"].includes(bindStatus)) {
                if (bindStatus === "ok") {
                  // this.state.bindSuccess = true;
                  trackBindOutcome("ok");
                } else if (bindStatus === "failed") {
                  // this.state.error = _t("Card binding failed. Please try again.");
                  trackBindOutcome("failed");
                } else if (bindStatus === "error") {
                  const todo = `this.state.error = _t("We couldn\'t confirm.")`;
                  trackBindOutcome("error", todo);
                }
                const cleanHash = window.location.hash.split("?")[0];
                window.history.replaceState(null, "", cleanHash);
              }
            });
        '''
        contract = _bind_frontend_contract(comment_only_feedback)
        self.assertFalse(
            contract['ok_feedback'],
            "Success feedback living only in a comment must not count",
        )
        self.assertIsNone(
            contract['failed_message'],
            "Failure message living only in a comment must not count",
        )
        self.assertIsNone(
            contract['error_message'],
            "Error message living only in a template literal must not count",
        )

    def test_guard_rejects_regex_literal_hidden_conditional_cleanup(self):
        """盲區 2：regex literal 裡的大括號不得污染 brace depth。"""
        regex_literal_cleanup = r'''
            onMounted(async () => {
              const bindStatus = q.bind_status;
              if (["ok", "failed", "error"].includes(bindStatus)) {
                if (bindStatus === "ok") {
                  this.state.bindSuccess = true;
                } else if (bindStatus === "failed") {
                  this.state.error = _t("Card binding failed. Please try again.");
                } else if (bindStatus === "error") {
                  this.state.error = _t("We couldn't confirm the card binding.");
                }
                if (shouldCleanupHash) {
                  const trailingBrace = /\}\s*$/;
                  window.history.replaceState(null, "", cleanHash);
                  const leadingBrace = /^\s*\{/;
                  log(trailingBrace, leadingBrace);
                }
              }
            });
        '''
        contract = _bind_frontend_contract(regex_literal_cleanup)
        # 三個 feedback 都在，證明這格紅的原因只可能是 cleanup 判定本身
        self.assertTrue(contract['ok_feedback'])
        self.assertTrue(contract['failed_message'])
        self.assertTrue(contract['error_message'])
        self.assertFalse(
            contract['cleanup_common'],
            "Braces inside regex literals must not fake a top-level cleanup",
        )

    def test_guard_rejects_braceless_conditional_cleanup(self):
        """盲區 3：沒有大括號的條件不改變 depth，但仍是條件執行。"""
        braceless_conditional_cleanup = '''
            onMounted(async () => {
              const bindStatus = q.bind_status;
              if (["ok", "failed", "error"].includes(bindStatus)) {
                if (bindStatus === "ok") {
                  this.state.bindSuccess = true;
                } else if (bindStatus === "failed") {
                  this.state.error = _t("Card binding failed. Please try again.");
                } else if (bindStatus === "error") {
                  this.state.error = _t("We couldn't confirm the card binding.");
                }
                const cleanHash = window.location.hash.split("?")[0];
                if (shouldCleanupHash) window.history.replaceState(null, "", cleanHash);
              }
            });
        '''
        contract = _bind_frontend_contract(braceless_conditional_cleanup)
        self.assertTrue(contract['ok_feedback'])
        self.assertTrue(contract['failed_message'])
        self.assertTrue(contract['error_message'])
        self.assertFalse(
            contract['cleanup_common'],
            "A braceless conditional still makes the cleanup conditional",
        )

    def test_guard_rejects_unknown_else_branch(self):
        """盲區 4：guarded 區塊裡多出來的第四個分支必須被看見。"""
        unknown_else_branch = '''
            onMounted(async () => {
              const bindStatus = q.bind_status;
              if (["ok", "failed", "error"].includes(bindStatus)) {
                if (bindStatus === "ok") {
                  this.state.bindSuccess = true;
                } else if (bindStatus === "failed") {
                  this.state.error = _t("Card binding failed. Please try again.");
                } else if (bindStatus === "error") {
                  this.state.error = _t("We couldn't confirm the card binding.");
                } else {
                  window.location.replace("/web/session/logout");
                }
                const cleanHash = window.location.hash.split("?")[0];
                window.history.replaceState(null, "", cleanHash);
              }
            });
        '''
        contract = _bind_frontend_contract(unknown_else_branch)
        # 三個已知分支與 cleanup 都合法，紅的只能是那個第四分支
        self.assertTrue(contract['ok_feedback'])
        self.assertTrue(contract['failed_message'])
        self.assertTrue(contract['error_message'])
        self.assertTrue(contract['cleanup_common'])
        self.assertFalse(
            contract['known_branches_only'],
            "A fourth fallback branch inside the guarded block is scope creep",
        )


class TestPaymentMethodBindResult(HttpCase):
    """Public-route contract for the browser-facing ECPG bind landing."""

    _CALLBACK_PATCH = (
        'odoo.addons.payment_ecpay_ecpg.controllers.ecpg_callback.'
        'ECPGCallback._handle_bind_callback'
    )

    def _post_bind_result(
        self,
        *,
        callback_result=None,
        callback_error=None,
        trade_no='BIND_PUBLIC_SEAM',
    ):
        patch_kwargs = (
            {'side_effect': callback_error}
            if callback_error is not None
            else {'return_value': callback_result}
        )
        with patch(self._CALLBACK_PATCH, **patch_kwargs) as callback:
            response = requests.post(
                f"{self.base_url()}"
                "/api/billing/payment-methods/bind-result/"
                f"{quote(trade_no, safe='')}",
                data={'RtnCode': '1', 'RtnMsg': 'OK'},
                allow_redirects=False,
                timeout=10,
            )
        self.assertEqual(response.status_code, 200, response.text[:500])
        callback.assert_called_once()
        match = re.search(
            r'<script>window\.location\.replace\((?P<target>.+?)\);</script>',
            response.text,
        )
        self.assertIsNotNone(match, response.text[:500])
        target = json.loads(match.group('target'))
        landing = urlsplit(target)
        configured_base = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url',
        ).rstrip('/')
        self.assertEqual(f'{landing.scheme}://{landing.netloc}', configured_base)
        self.assertEqual(landing.path, '/woow')
        fragment = landing.fragment
        path, separator, query_string = fragment.partition('?')
        self.assertEqual(path, '/settings/payment-methods')
        self.assertEqual(separator, '?')
        return response, target, parse_qs(query_string)

    def test_bind_result_true_redirects_with_ok_public_result(self):
        """True / ALREADY_EXISTS remains a successful browser result."""
        internal_reason = 'ALREADY_EXISTS_INTERNAL_MARKER'
        trade_no = 'BIND%&#PUBLIC'
        response, target, query = self._post_bind_result(
            callback_result=(True, internal_reason),
            trade_no=trade_no,
        )

        self.assertEqual(query.get('bind_status'), ['ok'])
        self.assertEqual(query.get('bind_code'), ['binding_succeeded'])
        self.assertEqual(query.get('trade_no'), [trade_no])
        self.assertIn('trade_no=BIND%25%26%23PUBLIC', target)
        self.assertNotIn(internal_reason, target)
        self.assertNotIn(internal_reason, response.text)

    def test_bind_result_false_redirects_with_failed_public_result(self):
        """Business rejection is visible without exposing its internal reason."""
        internal_reason = 'PARTNER_NOT_FOUND_INTERNAL_MARKER'
        response, target, query = self._post_bind_result(
            callback_result=(False, internal_reason),
        )

        self.assertEqual(query.get('bind_status'), ['failed'])
        self.assertEqual(query.get('bind_code'), ['binding_failed'])
        self.assertEqual(query.get('trade_no'), ['BIND_PUBLIC_SEAM'])
        self.assertNotIn(internal_reason, target)
        self.assertNotIn(internal_reason, response.text)

    def test_bind_result_none_redirects_with_error_public_result(self):
        """Parse/decrypt failure is a coarse processing error for the browser."""
        internal_reason = 'DECRYPT_FAIL_INTERNAL_MARKER'
        response, target, query = self._post_bind_result(
            callback_result=(None, internal_reason),
        )

        self.assertEqual(query.get('bind_status'), ['error'])
        self.assertEqual(query.get('bind_code'), ['processing_error'])
        self.assertEqual(query.get('trade_no'), ['BIND_PUBLIC_SEAM'])
        self.assertNotIn(internal_reason, target)
        self.assertNotIn(internal_reason, response.text)

    def test_bind_result_exception_still_lands_with_error_public_result(self):
        """Unexpected helper failure must land in the SPA, never a 500 page."""
        internal_reason = 'RTN_999_EXCEPTION_INTERNAL_MARKER'
        response, target, query = self._post_bind_result(
            callback_error=RuntimeError(internal_reason),
        )

        self.assertEqual(query.get('bind_status'), ['error'])
        self.assertEqual(query.get('bind_code'), ['processing_error'])
        self.assertEqual(query.get('trade_no'), ['BIND_PUBLIC_SEAM'])
        self.assertNotIn(internal_reason, target)
        self.assertNotIn(internal_reason, response.text)


class TestPaymentMethodsApi(HttpCase):

    def setUp(self):
        super().setUp()
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not self.provider:
            self.skipTest("ECPay provider not configured in this DB")
        self.provider.write({
            'enable_ecpg': True,
            'MerchantID': '3002607',
            'HashKey': 'pwFHCqoQZGmho4w6',
            'HashIV': 'EkRm7iFT261dpevs',
            'state': 'test',
        })
        self.authenticate('admin', 'admin')
        # self.env.user defaults to __system__ (partner id=2 OdooBot) in HttpCase,
        # but authenticate() made the HTTP session authenticate as admin (partner id=3).
        # Tests must use admin's partner when creating records referenced by the HTTP
        # endpoints, otherwise the endpoint's request.env.user.partner_id filter won't
        # match the fixture data.
        self.partner = self.env.ref('base.partner_admin')

    def test_prepare_bind_returns_token(self):
        fake_result = {
            'RtnCode': 1, 'RtnMsg': 'Success',
            'Token': 'FAKE_TOKEN_ABC123',
            'TokenExpireDate': '2026/12/31 23:59:59',
        }
        with patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient.get_token_by_binding_card',
            return_value=fake_result,
        ) as mock_get_token:
            response = self.url_open(
                '/api/billing/payment-methods/prepare-bind',
                data='{"jsonrpc":"2.0","method":"call","params":{}}',
                headers={'Content-Type': 'application/json'},
            )
            body = response.json()['result']

        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.assertEqual(body['data']['token'], 'FAKE_TOKEN_ABC123')
        self.assertIn('member_id', body['data'])
        self.assertIn('trade_no', body['data'])
        self.assertEqual(body['data']['merchant_id'], '3002607')
        self.assertTrue(body['data']['sandbox'])
        mock_get_token.assert_called_once()
        kwargs = mock_get_token.call_args.kwargs
        # OrderResultURL must point at our bind-result bouncer (which 303's into
        # the OWL SPA hash route) — ECPay POSTs here so the SPA shell can't accept.
        self.assertIn('/api/billing/payment-methods/bind-result/', kwargs['order_result_url'])
        self.assertTrue(
            kwargs['order_result_url'].endswith(body['data']['trade_no']),
            f"trade_no should be the path tail, got {kwargs['order_result_url']!r}",
        )

    def test_prepare_bind_no_provider_returns_error(self):
        self.provider.enable_ecpg = False
        response = self.url_open(
            '/api/billing/payment-methods/prepare-bind',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'provider_not_found')

    def test_create_bind_returns_three_d_url(self):
        """create_bind calls CreateBindCard and returns ThreeDURL if 3DS required."""
        fake_result = {
            'RtnCode': 1, 'RtnMsg': 'Success',
            'BindCardID': 'BC12345',
            'ThreeDInfo': {'ThreeDURL': 'https://3ds.example/verify?id=xyz'},
        }
        member_id = self.provider._ecpg_build_member_id(self.partner)
        with patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient.create_bind_card',
            return_value=fake_result,
        ) as mock_create:
            response = self.url_open(
                '/api/billing/payment-methods/create-bind',
                data=(
                    '{"jsonrpc":"2.0","method":"call","params":{'
                    f'"member_id":"{member_id}",'
                    '"trade_no":"BIND0001",'
                    '"bind_card_pay_token":"PAYTOKEN_XYZ"}}'
                ),
                headers={'Content-Type': 'application/json'},
            )
            body = response.json()['result']

        mock_create.assert_called_once()
        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.assertEqual(body['data']['three_d_url'], 'https://3ds.example/verify?id=xyz')
        self.assertEqual(body['data']['bind_card_id'], 'BC12345')

    def test_create_bind_member_id_mismatch(self):
        """Member ID from request must match the expected one for the current user."""
        response = self.url_open(
            '/api/billing/payment-methods/create-bind',
            data=(
                '{"jsonrpc":"2.0","method":"call","params":{'
                '"member_id":"WRONG_MEMBER",'
                '"trade_no":"BIND0001",'
                '"bind_card_pay_token":"PAYTOKEN_XYZ"}}'
            ),
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'member_id_mismatch')

    def test_list_cards_returns_payment_tokens(self):
        """List cards reads from payment.token with ecpg_bindcard_id set."""
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** 4242',
            'provider_ref': 'BC_TEST_1',
            'ecpg_bindcard_id': 'BC_TEST_1',
            'ecpg_member_id': 'M_1',
            'ecpg_card_expire': '2612',  # YYMM (matches ecpay callback: f"{yy}{mm}")
            'ecpg_issuing_bank': 'TEST BANK',
        })
        response = self.url_open(
            '/api/billing/payment-methods',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertTrue(body['success'])
        ids = [c['id'] for c in body['data']]
        self.assertIn(token.id, ids)
        entry = next(c for c in body['data'] if c['id'] == token.id)
        self.assertEqual(entry['last4'], '4242')
        self.assertEqual(entry['expire'], '12/26')  # formatted MM/YY
        self.assertEqual(entry['bank'], 'TEST BANK')

    def test_delete_archives_token(self):
        """Delete archives the payment.token (active=False), triggering ECPay delete via hook."""
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** 4242',
            'provider_ref': 'BC_TO_DELETE',
            'ecpg_bindcard_id': 'BC_TO_DELETE',
            'ecpg_member_id': 'M_1',
        })
        with patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient.delete_member_bind_card',
            return_value={'RtnCode': 1},
        ):
            response = self.url_open(
                f'/api/billing/payment-methods/{token.id}/delete',
                data='{"jsonrpc":"2.0","method":"call","params":{}}',
                headers={'Content-Type': 'application/json'},
            )
        body = response.json()['result']
        self.assertTrue(body['success'])
        token.invalidate_recordset()
        self.assertFalse(token.active)

    def test_bouncer_does_not_overwrite_session_cookie(self):
        """Bouncer at /api/billing/payment-methods/bind-result/<trade_no> must
        NOT set a new session_id cookie on its response.

        Why: ECPay POSTs cross-site to this URL after 3DS. SameSite=Lax strips
        the user's session_id from the request, so Odoo's default behavior is
        to mint a new anonymous session and Set-Cookie it back — overwriting
        the user's authenticated session_id in their browser. We use
        save_session=False on the route to prevent this. Upstream pattern:
        payment_mollie.mollie_return_from_checkout / payment_aps.aps_return_from_checkout.
        """
        import requests
        url = (
            f"{self.base_url()}"
            "/api/billing/payment-methods/bind-result/TEST_NO_SESSION"
        )
        with patch(
            'odoo.addons.payment_ecpay_ecpg.controllers.ecpg_callback.'
            'ECPGCallback._handle_bind_callback',
            return_value=(True, 'OK'),
        ):
            # Fresh requests.post() (not self.url_open) to isolate from
            # self.opener's authenticated cookies — simulates ECPay's
            # cookieless cross-site POST.
            response = requests.post(
                url,
                data={'RtnCode': '1', 'RtnMsg': 'OK'},
                allow_redirects=False,
                timeout=10,
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            'session_id', response.cookies,
            f"Bouncer must not Set-Cookie session_id "
            f"(got: {dict(response.cookies)})",
        )
        # Sanity: bouncer still serves the HTML+JS redirect page
        self.assertIn('window.location.replace', response.text)
