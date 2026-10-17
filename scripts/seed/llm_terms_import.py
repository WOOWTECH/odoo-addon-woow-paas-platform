#!/usr/bin/env python3
"""把《AI 模型服務條款》草稿（Markdown）轉成 ``llm_terms`` 的一筆資料，給 stg 匯入用（票 LLM-04）。

Run:
    python3 scripts/seed/llm_terms_import.py                       # JSON 到 stdout
    python3 scripts/seed/llm_terms_import.py --format shell > /tmp/terms.py
    # 沙盒：kubectl exec … -- odoo shell -d <db> --no-http < /tmp/terms.py

規則（``docs/reference/features/ai-models-terms-draft.md``〈給 LLM-04：匯入 stg〉）：

* 只取 ``<!-- terms-body:begin -->`` 與 ``<!-- terms-body:end -->`` 兩行之間；**去掉每一段「審閱註記」
  引用區塊**（從 ``> 審閱註記`` 開頭的那一行，到後面連續以 ``>`` 開頭的行；和
  ``scripts/audit/test_ai_models_terms_draft.py`` 的 ``_strip_notes()`` 同一個規則）。「草稿，尚未生效」
  與【待…】標記保留（``llm_terms.is_draft_text`` 靠它們判斷草稿；正式站不能發佈草稿文字）。
* 外部連結一律 ``target="_blank" rel="noopener noreferrer"``（LLM-05 第二輪審查：在對話框裡點了
  不能讓 /woow 整頁跳走）。
* 只支援草稿實際用到的 Markdown 子集：``###`` 標題、段落、``-`` 清單、``1.`` 清單（可以內含一層
  ``-`` 子清單）、``**粗體**``、`` `行內程式碼` ``、``[文字](https://…)``。**其他寫法一律報錯**
  （``h1``／``h2``、表格、原始 HTML、圖片、引用區塊、清單項目的續行…）：轉換器寧可失敗，也不能
  悄悄掉內容——使用者同意的是畫面上那一份。輸出的標籤都在 ``html_sanitize.js`` 的 ``ALLOWED_TAGS`` 內。
* 所有文字先 HTML escape，再套行內格式。

預設值照〈給 LLM-04：匯入 stg〉的建議：``version``＝``2026-10-draft-1``、``title``＝
「AI 模型服務條款（草稿，尚未生效）」、``requires_reaccept``＝true（既有帳戶同意的是 LLM-01b 的
``draft-0``，沒有任何條文；不遷移那些紀錄）。

``--format shell`` 產生給 ``odoo shell`` 的程式：同一個 version 已存在就不重建（published／retired
不能改），是草稿就發佈，最後 commit、清 registry cache。發佈會讓原本的 published 轉 retired。
"""
import argparse
import html
import json
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DOC = REPO_ROOT / 'docs/reference/features/ai-models-terms-draft.md'
DEFAULT_VERSION = '2026-10-draft-1'
DEFAULT_TITLE = 'AI 模型服務條款（草稿，尚未生效）'

BODY_BEGIN = '<!-- terms-body:begin -->'
BODY_END = '<!-- terms-body:end -->'
NOTE_PREFIX = '> 審閱註記'
VERSION_RE = re.compile(r'[0-9A-Za-z][0-9A-Za-z._-]{0,63}', re.ASCII)

_ORDERED_RE = re.compile(r'([0-9]{1,3})\. (.+)')
_BULLET_RE = re.compile(r'- (.+)')
_NESTED_BULLET_RE = re.compile(r' {2,4}- (.+)')
_CODE_RE = re.compile(r'`([^`]+)`')
_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')
_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')
_LINK_SCHEMES = ('https://', 'http://', 'mailto:')


class TermsFormatError(ValueError):
    """草稿用了轉換器不支援的寫法（訊息帶行號）。"""


def extract_body(text):
    """兩個標記行之間的內容（各要剛好一行、begin 在 end 之前）。"""
    lines = text.splitlines()
    begins = [i for i, line in enumerate(lines) if line == BODY_BEGIN]
    ends = [i for i, line in enumerate(lines) if line == BODY_END]
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        raise TermsFormatError('the terms body markers must appear exactly once, begin before end')
    return '\n'.join(lines[begins[0] + 1:ends[0]])


def strip_notes(text):
    """去掉「審閱註記」引用區塊（規則同 test_ai_models_terms_draft._strip_notes）。"""
    out = []
    in_note = False
    for line in text.splitlines():
        if line.startswith(NOTE_PREFIX):
            in_note = True
            continue
        if in_note and line.startswith('>'):
            continue
        in_note = False
        out.append(line)
    return '\n'.join(out)


def _inline(text, lineno):
    codes = []

    def keep_code(match):
        codes.append('<code>%s</code>' % html.escape(match.group(1), quote=False))
        return '\x00%d\x00' % (len(codes) - 1)

    # 行內程式碼裡的 < 是字面值（escape 掉）；程式碼以外的才算原始 HTML。
    text = _CODE_RE.sub(keep_code, text)
    if re.search(r'<[A-Za-z/!]', text):
        raise TermsFormatError('line %d: raw HTML is not supported' % lineno)
    if '![' in text:
        raise TermsFormatError('line %d: images are not supported' % lineno)
    links = []

    def keep_link(match):
        label, url = match.group(1), match.group(2)
        if not url.startswith(_LINK_SCHEMES):
            raise TermsFormatError('line %d: only http(s) and mailto links are supported' % lineno)
        attrs = '' if url.startswith('mailto:') else ' target="_blank" rel="noopener noreferrer"'
        links.append('<a href="%s"%s>%s</a>' % (html.escape(url, quote=True), attrs,
                                                _bold(html.escape(label, quote=False), lineno)))
        return '\x01%d\x01' % (len(links) - 1)

    text = _LINK_RE.sub(keep_link, text)
    if '](' in text or '`' in text:
        raise TermsFormatError('line %d: malformed link or code span' % lineno)
    text = _bold(html.escape(text, quote=False), lineno)
    text = re.sub('\x01([0-9]+)\x01', lambda m: links[int(m.group(1))], text)
    return re.sub('\x00([0-9]+)\x00', lambda m: codes[int(m.group(1))], text)


def _bold(text, lineno):
    text = _BOLD_RE.sub(r'<strong>\1</strong>', text)
    if '**' in text:
        raise TermsFormatError('line %d: unbalanced ** (bold)' % lineno)
    return text


def body_to_html(body):
    """條款本體（已去掉審閱註記）→ HTML。不支援的寫法丟 :class:`TermsFormatError`。"""
    out = []
    paragraph = []
    list_kind = None      # 'ol' / 'ul'
    items = []            # [(inline_html, [nested inline_html])]
    expected_number = 1

    def flush_paragraph():
        if paragraph:
            out.append('<p>%s</p>' % ' '.join(paragraph))
            paragraph.clear()

    def flush_list():
        nonlocal list_kind, expected_number
        if list_kind:
            parts = []
            for item, nested in items:
                if nested:
                    item += '<ul>%s</ul>' % ''.join('<li>%s</li>' % n for n in nested)
                parts.append('<li>%s</li>' % item)
            out.append('<%s>%s</%s>' % (list_kind, ''.join(parts), list_kind))
        list_kind = None
        items.clear()
        expected_number = 1

    for lineno, raw in enumerate(body.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            # 清單要連續：空行結束清單（之後再出現的 2. 會因為編號不連續而報錯，不會悄悄併錯清單）。
            flush_paragraph()
            flush_list()
            continue
        if line.startswith('#'):
            if not line.startswith('### ') or line.startswith('####'):
                raise TermsFormatError('line %d: only ### headings are supported' % lineno)
            flush_paragraph()
            flush_list()
            out.append('<h3>%s</h3>' % _inline(line[4:].strip(), lineno))
            continue
        if line.startswith('>'):
            raise TermsFormatError('line %d: block quotes are not supported (review note left?)'
                                   % lineno)
        if line.lstrip().startswith('|'):
            raise TermsFormatError('line %d: tables are not supported' % lineno)
        nested = _NESTED_BULLET_RE.fullmatch(line)
        if nested:
            if not items or paragraph:
                raise TermsFormatError('line %d: a nested list must follow a list item' % lineno)
            items[-1][1].append(_inline(nested.group(1), lineno))
            continue
        if line[0].isspace():
            raise TermsFormatError('line %d: indented continuation lines are not supported'
                                   % lineno)
        ordered = _ORDERED_RE.fullmatch(line)
        bullet = _BULLET_RE.fullmatch(line)
        if ordered or bullet:
            kind = 'ol' if ordered else 'ul'
            flush_paragraph()
            if list_kind != kind:
                flush_list()
                list_kind = kind
            if ordered:
                if int(ordered.group(1)) != expected_number:
                    raise TermsFormatError('line %d: ordered list items must be numbered 1, 2, 3…'
                                           % lineno)
                expected_number += 1
            items.append((_inline((ordered or bullet).group(2 if ordered else 1), lineno), []))
            continue
        if list_kind:
            flush_list()
        paragraph.append(_inline(line.strip(), lineno))
    flush_paragraph()
    flush_list()
    return '\n'.join(out)


def build_record(doc_text, version=DEFAULT_VERSION, title=DEFAULT_TITLE, requires_reaccept=True):
    if not VERSION_RE.fullmatch(version or ''):
        raise TermsFormatError('invalid version %r' % version)
    if not (title or '').strip():
        raise TermsFormatError('the title is empty')
    content = body_to_html(strip_notes(extract_body(doc_text)))
    if not content:
        raise TermsFormatError('the terms body is empty')
    return {
        'version': version,
        'title': title.strip(),
        'content_html': content,
        'requires_reaccept': bool(requires_reaccept),
    }


_SHELL_TEMPLATE = '''# Generated by scripts/seed/llm_terms_import.py (LLM-04). Run inside `odoo shell`.
import json
data = json.loads(%s)
Terms = env['woow_paas_platform.llm_terms'].sudo()
record = Terms.search([('version', '=', data['version'])], limit=1)
if not record:
    record = Terms.create(data)
elif record.state == 'draft':
    record.write({k: data[k] for k in ('title', 'content_html', 'requires_reaccept')})
if record.state == 'draft':
    record.action_publish()
env.cr.commit()
env.registry.clear_cache()
env.registry.signal_changes()
print('llm_terms', record.version, record.state, 'publish_seq', record.publish_seq,
      'draft_text', record.is_draft_text)
'''


def shell_script(record):
    return _SHELL_TEMPLATE % repr(json.dumps(record, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--doc', type=pathlib.Path, default=DEFAULT_DOC)
    parser.add_argument('--version', default=DEFAULT_VERSION)
    parser.add_argument('--title', default=DEFAULT_TITLE)
    parser.add_argument('--no-reaccept', action='store_true',
                        help='publish with requires_reaccept=false (existing accounts are not blocked)')
    parser.add_argument('--format', choices=('json', 'shell'), default='json')
    args = parser.parse_args(argv)
    try:
        record = build_record(args.doc.read_text(encoding='utf-8'), args.version, args.title,
                              not args.no_reaccept)
    except TermsFormatError as exc:
        print('llm_terms_import: %s' % exc, file=sys.stderr)
        return 2
    if args.format == 'shell':
        sys.stdout.write(shell_script(record))
    else:
        json.dump(record, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
