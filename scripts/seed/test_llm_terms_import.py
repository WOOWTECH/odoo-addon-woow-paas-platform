# -*- coding: utf-8 -*-
"""``llm_terms_import``（LLM-04：條款草稿 → stg 的 ``llm_terms``）的自測。

Run:  python3 scripts/seed/test_llm_terms_import.py -v
CI:   ``.gitea/workflows/repo-lint.yml`` 的「scripts/ 自我測試」（scripts/seed/test_*.py）。

守的是「匯入 stg 的那一份＝使用者同意的那一份」：

1. 拿 repo 裡真的草稿轉：審閱註記一段都不剩、「草稿，尚未生效」與【待…】還在、每一條標題都在、
   每一個清單項目都在（數量對得上）、用到的標籤都在 ``html_sanitize.js`` 的 ``ALLOWED_TAGS`` 內、
   外部連結都開新分頁。
2. 不支援的寫法一律報錯（不悄悄掉內容），而且每一條規則都證明會觸發。
3. ``--format shell`` 產生的程式是合法的 Python。
4. D20：條款本體還寫著「0 點模型」就拒絕匯入（published 不能改，鎖進去就只能再發一版）。本鏈還不含
   #1815，repo 裡的草稿是第一輪的文字：:meth:`TestRealDraft.test_real_draft_follows_d20_or_is_refused`
   在兩種狀態下都有斷言（還有舊字句 ⇒ 預設匯入失敗；#1815 rebase 進來之後 ⇒ 預設匯入成功）。
"""
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import llm_terms_import as m

REPO = Path(__file__).resolve().parents[2]
SANITIZER = REPO / 'src/static/src/paas/services/html_sanitize.js'


def _allowed_tags():
    source = SANITIZER.read_text(encoding='utf-8')
    block = re.search(r'const ALLOWED_TAGS = \[(.*?)\];', source, re.S).group(1)
    return set(re.findall(r'"([a-z0-9]+)"', block))


def _doc(body):
    return '前言\n\n%s\n%s\n%s\n\n結尾\n' % (m.BODY_BEGIN, body, m.BODY_END)


class TestRealDraft(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = m.DEFAULT_DOC.read_text(encoding='utf-8')
        cls.body = m.strip_notes(m.extract_body(cls.text))
        # 轉換本身（結構、標籤、連結）不受 D20 守線影響：直接轉，D20 另外驗。
        cls.html = m.body_to_html(cls.body)
        cls.record = {'version': m.DEFAULT_VERSION, 'title': m.DEFAULT_TITLE,
                      'content_html': cls.html, 'requires_reaccept': True}

    def test_defaults_follow_the_import_section(self):
        record = m.build_record(_doc('x'))
        self.assertEqual((record['version'], record['title'], record['requires_reaccept']),
                         ('2026-10-draft-1', 'AI 模型服務條款（草稿，尚未生效）', True))

    def test_real_draft_follows_d20_or_is_refused(self):
        if m.UNSOLD_MODELS_RE.search(self.html):
            # 第一輪的文字（本鏈 rebase 到 #1815 之前）：預設匯入一定失敗，不會把舊字句鎖進 published。
            with self.assertRaises(m.TermsFormatError) as caught:
                m.build_record(self.text)
            self.assertIn('D20', str(caught.exception))
        else:
            self.assertEqual(m.build_record(self.text), self.record)

    def test_review_notes_are_gone_and_draft_markers_stay(self):
        self.assertIn(m.NOTE_PREFIX, m.extract_body(self.text), '陽性對照：原文有註記')
        self.assertNotIn('審閱註記', self.html)
        self.assertNotIn('<blockquote', self.html)
        self.assertIn('草稿，尚未生效', self.html)
        self.assertIn('【待', self.html)

    def test_every_heading_and_list_item_survives(self):
        headings = re.findall(r'^### (.+)$', self.body, re.M)
        self.assertGreaterEqual(len(headings), 13)
        self.assertEqual(len(re.findall(r'<h3>', self.html)), len(headings))
        items = re.findall(r'^(?:[0-9]+\.|-| {2,4}-) ', self.body, re.M)
        self.assertEqual(len(re.findall(r'<li>', self.html)), len(items))

    def test_only_sanitizer_allowed_tags_and_safe_links(self):
        tags = set(re.findall(r'<([a-z0-9]+)', self.html))
        self.assertLessEqual(tags, _allowed_tags())
        self.assertNotIn('h1', tags)
        self.assertNotIn('h2', tags)
        for anchor in re.findall(r'<a [^>]*>', self.html):
            href = re.search(r'href="([^"]+)"', anchor).group(1)
            self.assertRegex(href, r'^(https?://|mailto:)')
            if not href.startswith('mailto:'):
                self.assertIn('target="_blank"', anchor)
                self.assertIn('rel="noopener noreferrer"', anchor)

    def test_shell_script_is_valid_python_and_carries_the_record(self):
        script = m.shell_script(self.record)
        compile(script, 'terms.py', 'exec')
        literal = re.search(r'json\.loads\((.*)\)\n', script).group(1)
        self.assertEqual(json.loads(eval(literal)), self.record)  # noqa: S307 - our own repr()
        self.assertIn('action_publish()', script)
        self.assertIn('env.registry.clear_cache()', script)
        self.assertIn('env.registry.signal_changes()', script)


class TestConverter(unittest.TestCase):

    def convert(self, body):
        return m.build_record(_doc(body))['content_html']

    def test_supported_subset(self):
        html = self.convert('\n'.join([
            '**草稿，尚未生效。** 說明 <請看> & 其他',
            '',
            '### 第 1 條 `x<y` 標題',
            '',
            '1. 第一項 **粗體** 與 `a & b`',
            '2. 第二項：',
            '   - 子項 [原始碼](https://example.test/a?b=1&c=2)',
            '   - 信箱 [support](mailto:support@woowtech.io)',
            '3. 第三項',
            '',
            '- 清單',
            '- 清單二',
            '段落接在清單後面',
        ]))
        self.assertEqual(html, '\n'.join([
            '<p><strong>草稿，尚未生效。</strong> 說明 &lt;請看&gt; &amp; 其他</p>',
            '<h3>第 1 條 <code>x&lt;y</code> 標題</h3>',
            '<ol><li>第一項 <strong>粗體</strong> 與 <code>a &amp; b</code></li>'
            '<li>第二項：<ul><li>子項 <a href="https://example.test/a?b=1&amp;c=2" '
            'target="_blank" rel="noopener noreferrer">原始碼</a></li>'
            '<li>信箱 <a href="mailto:support@woowtech.io">support</a></li></ul></li>'
            '<li>第三項</li></ol>',
            '<ul><li>清單</li><li>清單二</li></ul>',
            '<p>段落接在清單後面</p>',
        ]))

    def test_review_notes_are_stripped_before_converting(self):
        html = self.convert('段落\n\n> 審閱註記｜依據：D6、N11\n> 第二行\n\n下一段')
        self.assertEqual(html, '<p>段落</p>\n<p>下一段</p>')

    def test_unsupported_constructs_fail_loudly(self):
        cases = {
            '# 一級標題': 'headings',
            '## 二級標題': 'headings',
            '#### 四級標題': 'headings',
            '| a | b |': 'tables',
            '> 不是審閱註記的引用': 'block quotes',
            '<b>原始 HTML</b>': 'raw HTML',
            '![圖](https://example.test/x.png)': 'images',
            '[相對連結](/woow#/llm)': 'only http',
            '[腳本](javascript:alert(1))': 'only http',
            '**沒有收尾': 'unbalanced',
            '`沒有收尾': 'malformed',
            '1. 一\n3. 三': 'numbered',
            '2. 從二開始': 'numbered',
            '1. 一\n\n2. 空行隔開的二': 'numbered',
            '1. 一\n   續行文字': 'continuation',
            '段落\n   - 子清單接在段落後': 'nested list',
        }
        for body, needle in cases.items():
            with self.subTest(body=body), self.assertRaises(m.TermsFormatError) as caught:
                self.convert(body)
            self.assertIn(needle, str(caught.exception))

    def test_markers_and_record_fields_are_validated(self):
        for text in ('沒有標記', m.BODY_END + '\n' + m.BODY_BEGIN,
                     _doc('x') + m.BODY_BEGIN + '\n'):
            with self.subTest(text=text[:20]), self.assertRaises(m.TermsFormatError):
                m.build_record(text)
        with self.assertRaises(m.TermsFormatError):
            m.build_record(_doc('x'), version='bad version')
        with self.assertRaises(m.TermsFormatError):
            m.build_record(_doc('x'), title='  ')
        with self.assertRaises(m.TermsFormatError):
            m.build_record(_doc('> 審閱註記｜只有註記'))
        self.assertFalse(m.build_record(_doc('x'), requires_reaccept=False)['requires_reaccept'])

    def test_unsold_zero_point_models_are_refused(self):
        for body in ('包括付費模型、0 點模型與免費試用模型', '- **0點模型**：說明', '０點模型',
                     '零點模型由上游供應商免費提供', '### 第 2 條 0 點模型'):
            with self.subTest(body=body), self.assertRaises(m.TermsFormatError) as caught:
                self.convert(body)
            self.assertIn('D20', str(caught.exception))
        for body in ('單次轉換 50 點模型不限', '透支 10 點', '付費模型與免費試用模型'):
            with self.subTest(allowed=body):
                self.convert(body)
        # 只出現在審閱註記裡（匯入時會去掉）不算。
        self.convert('段落\n\n> 審閱註記｜所以條款不另設「0 點模型」類別')

    def test_cli_refuses_without_writing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / 'terms.md'
            doc.write_text(_doc('付費模型與 0 點模型'), encoding='utf-8')
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = m.main(['--doc', str(doc), '--format', 'shell'])
            self.assertEqual((rc, out.getvalue()), (2, ''))
            self.assertIn('D20', err.getvalue())
            doc.write_text(_doc('付費模型與免費試用模型'), encoding='utf-8')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = m.main(['--doc', str(doc), '--format', 'shell'])
            self.assertEqual(rc, 0)
            compile(out.getvalue(), 'terms.py', 'exec')


if __name__ == '__main__':
    unittest.main()
