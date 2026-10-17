#!/usr/bin/env python3
"""LLM-05：《AI 模型服務條款》草稿（``docs/reference/features/ai-models-terms-draft.md``）的守線。

Run:  python3 scripts/audit/test_ai_models_terms_draft.py -v
CI:   ``.gitea/workflows/repo-lint.yml`` 的「scripts/ 自我測試」步驟（跑 ``scripts/audit/test_*.py``）；
      該 workflow 的 paths 已列入這份草稿，只改草稿的 PR 也會跑到。

## 守什麼

這份草稿會被負責人與法務改寫（D15），再由 LLM-04 匯入 stg、LLM-18 發佈到正式站。改寫時最容易
悄悄壞掉、而且人工審閱不容易看出來的，是下面幾件事：

1. **票面規定的 12 條都還在、每條都標了依據**（LLM-05 驗收標準 1）。依據寫在每條後面的
   「審閱註記｜依據：」引用區塊；引用的每個 D／N 編號都要在同一份檔的〈決定摘要〉表裡有一列，
   審閱者不必翻 repo 外的 DECISIONS／DESIGN 就能對照。
2. **沒有和設計衝突的規則**（驗收標準 1 的「例如不出現『可退款』、不寫死『1 個工作天』」）。
   ``_CONFLICT_RULES`` 逐條掃條款本體（不含審閱註記，註記本來就要討論這些字眼）；
   ``test_conflict_rules_fire_on_planted_violations`` 用刻意放進去的違規句證明每一條都會觸發、
   合法寫法不會誤報——規則寫錯而永遠不觸發時，輸出會和「沒有違規」完全相同。
3. **和程式一致的數字**：條款寫的免費每日次數、金鑰上限、單次轉換上下限，讀
   ``src/models/llm_common.py`` 的常數比對；常數改了，條款要跟著改（條款是對使用者的揭露）。
4. **待確認的地方有對到問題清單**：條款本體裡每個【待…  Qn】標記都要在〈給法務與負責人的
   問題清單〉有同一個 Qn；問題清單要涵蓋票面指定的 U9 三點、U20、U21、AUP 停權程序、責任上限。
5. **匯入 stg 不會掉格式**（驗收標準 3）：本體用到的標題層級要在
   ``src/static/src/paas/services/html_sanitize.js`` 的 ``ALLOWED_TAGS`` 內（h1／h2 不在，經
   ``safeHtml()`` 後只剩純文字），不能有原始 HTML 或圖片；開頭要有「草稿，尚未生效」。

## 法務改寫時

條文措辭可以改；改到這裡的必要字句（``_REQUIRED_FACTS``）時，先確認新寫法仍然符合同一個決定，
再同步改這張表。決定本身改了（例如負責人改了 D6），先改 DECISIONS，再改條款與這裡。
"""
import ast
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOC = _REPO_ROOT / 'docs/reference/features/ai-models-terms-draft.md'
_LLM_COMMON = _REPO_ROOT / 'src/models/llm_common.py'
_SANITIZER = _REPO_ROOT / 'src/static/src/paas/services/html_sanitize.js'

BODY_BEGIN = '<!-- terms-body:begin -->'
BODY_END = '<!-- terms-body:end -->'
# 標記要自成一行；說明文字裡用行內程式碼提到它們不算（〈怎麼讀〉一節就這樣提）。
_BEGIN_LINE_RE = re.compile(r'^' + re.escape(BODY_BEGIN) + r'$', re.M)
_END_LINE_RE = re.compile(r'^' + re.escape(BODY_END) + r'$', re.M)
DRAFT_BANNER = '草稿，尚未生效'
NOTE_PREFIX = '> 審閱註記'
BASIS_PREFIX = '> 審閱註記｜依據：'
QUESTION_SECTION = '## 給法務與負責人的問題清單'
SUMMARY_SECTION = '## 決定摘要'

# 票面 12 條的標題關鍵字（LLM-05「範圍 In」第 1–12 項）。
_CLAUSE_TITLES = {
    1: '服務說明',
    2: '帳戶',
    3: '計價',
    4: '不退款',
    5: '免費試用',
    6: '資料與隱私',
    7: '金鑰',
    8: '可接受使用',
    9: '透支',
    10: '責任限制',
    11: '條款變更',
    12: '開源',
}

# 每條必須保留的字句：只收「決定本身」或票面明定的寫法，不收一般措辭。
_REQUIRED_FACTS = {
    1: ('OpenAI 相容', 'New API', '不保證'),  # 票面第 1 項；D2
    2: ('本人', '同意本條款'),  # D1、D10
    3: ('1 點＝NT$1', '含稅', '點／百萬 tokens', '7 天', '不保價'),  # D5、DECISIONS 預設
    # D6：與轉換對話框的必勾文字相同（DESIGN §4.6、§6.3.5）；作廢不是退款；D17 盜用也不退。
    4: ('轉換後的 AI 額度不能退回點數或現金', '不屬於退款', '盜用'),
    5: ('Qwen3-4B', '每分鐘 10 次', '台北時間 00:00', '以系統統計為準'),  # D11
    # DECISIONS 預設（境外、180 天）；U21 未確認前的弱寫法逐字照票面。
    6: ('OpenRouter', '境外', '180 天',
        'WoowTech 不主動保存請求與回應內容；上游服務可能依其政策記錄'),
    7: ('顯示一次', '不是我操作'),  # DECISIONS 預設、N25
    8: ('轉售', '暫停'),  # 票面第 8 項
    9: ('在途', '下次轉換時先扣抵'),  # N12
    10: ('不保證本服務不中斷', '我們會盡快處理並通知您'),  # 票面第 10 項
    11: ('重新同意', '既有的金鑰'),  # DESIGN §6.4
    12: ('AGPL-3.0', 'https://github.com/QuantumNous/new-api'),  # DESIGN §5.8
}

_MARKER_RE = re.compile(r'【待(法務確認|負責人決定|負責人確認|負責人填寫) (Q\d+)】')
_DECISION_ID_RE = re.compile(r'(?<![A-Za-z0-9])([DN]\d{1,2})(?![0-9])')
_HEADING_RE = re.compile(r'^(#{1,6}) ', re.M)
_CLAUSE_RE = re.compile(r'^第 (\d+) 條 (.+)$')

# 否定前綴：「不得申請退款」「不提供全額退款」「不保證服務不中斷」是合法寫法。
_NEGATORS = ('不', '無法', '不得', '不能', '不可', '不予', '不會', '不提供', '不承諾')


def _phrase_rule(phrases, negation_aware):
    def find(text):
        hits = []
        for phrase in phrases:
            for match in re.finditer(re.escape(phrase), text):
                prefix = text[max(0, match.start() - 4):match.start()]
                if negation_aware and any(prefix.endswith(n) for n in _NEGATORS):
                    continue
                hits.append(match.group(0))
        return hits
    return find


def _regex_rule(pattern, flags=0, negation_aware=False):
    compiled = re.compile(pattern, flags)

    def find(text):
        hits = []
        for match in compiled.finditer(text):
            prefix = text[max(0, match.start() - 4):match.start()]
            if negation_aware and any(prefix.endswith(n) for n in _NEGATORS):
                continue
            hits.append(match.group(0))
        return hits
    return find


_NUM = r'[0-9０-９一二兩三四五六七八九十]+'

# (名稱, 找違規的函式, 為什麼不能寫)
_CONFLICT_RULES = (
    ('refund-allowed',
     _phrase_rule(('可以退款', '可退款', '得退款', '能退款', '申請退款', '辦理退款',
                   '全額退款', '部分退款', '按比例退', '例外退回', '個案退回', '個案退還'),
                  negation_aware=True),
     'D6 完全不退、D17 盜用期間也不退；「轉換未完成退回點數」要寫成不屬於退款（DESIGN §4.6）'),
    ('fixed-support-sla',
     _regex_rule(_NUM + r'\s*個?\s*工作(?:天|日)'
                 r'|' + _NUM + r'\s*(?:小時|天|日)內(?:回覆|回應|答覆|處理)'),
     '客服不承諾固定時限（D14；票面明令不寫死「1 個工作天」），只寫「我們會盡快處理並通知您」'),
    ('no-content-storage-claim',
     _regex_rule(r'不(?:會)?(?:儲存|保存|記錄|保留)(?:您的)?(?:API ?)?(?:請求|回應|提示詞|對話)'),
     'U21：上游 LiteLLM 關閉 prompt／response 紀錄是前提，D16 後不會做；'
     '只能寫「WoowTech 不主動保存請求與回應內容；上游服務可能依其政策記錄」'),
    ('no-key-storage-claim',
     _regex_rule(r'不(?:會)?(?:儲存|保存|保留|留存)(?:您的)?(?:完整的?)?(?:API ?)?金鑰'),
     'new-api 以明文保存金鑰（D2 不改碼，DESIGN §5.1）；只能寫「平台不會再次顯示」'),
    ('uptime-guarantee',
     _regex_rule(r'保證(?:本服務|服務)?(?:不會?中斷|持續可用|正常運作)', negation_aware=True),
     '服務依現況提供，不保證不中斷（票面第 10 項）'),
    ('free-daily-without-approx',
     _regex_rule(r'每(?:天|日)\s*' + _NUM + r'\s*次'),
     '每日計數有誤差（最壞約 220–240 次，DESIGN §1.3.3），要寫「每天約 200 次（以系統統計為準）」'),
    ('internal-units-or-components',
     _regex_rule(r'USD|US\$|美元|quota|LiteLLM|svc203|cluster\.local|newapi-proxy|new-api-stg'
                 r'|GroupRatio', re.I),
     '使用者介面一律用「點」（DESIGN §6.6）；條文不揭露內部元件'),
)


def _conflicts(text):
    found = []
    for name, find, why in _CONFLICT_RULES:
        for hit in find(text):
            found.append((name, hit, why))
    return found


def _read(path):
    return path.read_text(encoding='utf-8')


def _section(doc, heading):
    """``heading`` 那一節到下一個 ``## `` 之前的文字。"""
    start = doc.index(heading)
    rest = doc[start + len(heading):]
    nxt = re.search(r'^## ', rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def _body(doc):
    begin = _BEGIN_LINE_RE.search(doc)
    end = _END_LINE_RE.search(doc)
    if not begin or not end:
        raise AssertionError(f'找不到自成一行的 {BODY_BEGIN} ／ {BODY_END}')
    return doc[begin.end():end.start()]


def _strip_notes(text):
    """去掉「審閱註記」引用區塊（正式版會刪的部分）。"""
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


def _sections(body):
    """依 ``### `` 切成 [(標題, 內文)]。"""
    parts = re.split(r'^### (.+)$', body, flags=re.M)
    return list(zip(parts[1::2], parts[2::2]))


def _clauses(body):
    clauses = {}
    for title, text in _sections(body):
        match = _CLAUSE_RE.match(title.strip())
        if match:
            clauses[int(match.group(1))] = (match.group(2), text)
    return clauses


def _table_rows(section, first_cell_re):
    rows = []
    for line in section.splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if cells and re.fullmatch(first_cell_re, cells[0]):
            rows.append(cells)
    return rows


def _int_constants(path):
    tree = ast.parse(_read(path))
    values = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, int)):
            values[node.targets[0].id] = node.value.value
    return values


def _allowed_tags():
    match = re.search(r'const ALLOWED_TAGS = \[(.*?)\];', _read(_SANITIZER), re.S)
    if not match:
        raise AssertionError('找不到 html_sanitize.js 的 ALLOWED_TAGS（檔案改名或改寫法了？）')
    return set(re.findall(r'"([a-z0-9]+)"', match.group(1)))


class TestAiModelsTermsDraft(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.doc = _read(_DOC)
        cls.body = _body(cls.doc)
        cls.clauses = _clauses(cls.body)
        cls.questions = {
            row[0]: row for row in _table_rows(_section(cls.doc, QUESTION_SECTION), r'Q\d+')
        }

    def clause_text(self, number):
        if number not in self.clauses:
            self.fail(f'條款本體找不到「### 第 {number} 條 …」')
        return self.clauses[number][1]

    def test_frontmatter_marks_reference_draft(self):
        front = self.doc.split('---', 2)[1]
        self.assertIn('category: reference', front)
        self.assertIn('status: draft', front)

    def test_body_markers_appear_once_in_order(self):
        begins = list(_BEGIN_LINE_RE.finditer(self.doc))
        ends = list(_END_LINE_RE.finditer(self.doc))
        self.assertEqual((len(begins), len(ends)), (1, 1))
        self.assertLess(begins[0].start(), ends[0].start())

    def test_body_opens_with_draft_banner(self):
        # LLM-05 驗收標準 3：匯入 stg 後要標示「草稿，尚未生效」。
        first = next(line for line in self.body.splitlines() if line.strip())
        self.assertIn(DRAFT_BANNER, first)

    def test_all_twelve_ticket_clauses_present_in_order(self):
        numbers = sorted(self.clauses)
        self.assertEqual(numbers[:12], list(range(1, 13)), numbers)
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)), '條號要連續')
        for number, keyword in _CLAUSE_TITLES.items():
            with self.subTest(clause=number):
                self.assertIn(keyword, self.clauses.get(number, ('', ''))[0])

    def test_every_ticket_clause_cites_its_basis(self):
        for number in _CLAUSE_TITLES:
            text = self.clause_text(number)
            notes = [line for line in text.splitlines() if line.startswith(BASIS_PREFIX)]
            with self.subTest(clause=number):
                self.assertEqual(len(notes), 1, '每條恰好一段「審閱註記｜依據：」')
                self.assertTrue(
                    _DECISION_ID_RE.search(notes[0])
                    or 'DECISIONS 預設' in notes[0] or 'DESIGN §' in notes[0],
                    notes[0])

    def test_cited_decisions_are_listed_in_summary(self):
        summary_ids = {row[0] for row in _table_rows(_section(self.doc, SUMMARY_SECTION),
                                                     r'[DN]\d{1,2}')}
        cited = set()
        for line in self.body.splitlines():
            if line.startswith(NOTE_PREFIX):
                cited.update(_DECISION_ID_RE.findall(line))
        self.assertTrue(cited)
        self.assertEqual(cited - summary_ids, set(), '審閱註記引用了〈決定摘要〉沒有的編號')

    def test_required_facts_kept(self):
        consts = _int_constants(_LLM_COMMON)
        from_code = {
            # D11：條款的每日次數＝程式的每日上限常數。
            5: (f"每天約 {consts['FREE_DAILY_LIMIT']} 次",),
            # D10：每帳號金鑰上限。
            7: (f"最多可以有 {consts['MAX_KEYS_PER_ACCOUNT']} 把",),
            # N6：單次轉換上下限。
            3: (f"{consts['CONVERT_MIN_POINTS']:,}–{consts['CONVERT_MAX_POINTS']:,} 點",),
        }
        for number, facts in _REQUIRED_FACTS.items():
            text = _strip_notes(self.clause_text(number))
            for fact in facts + from_code.get(number, ()):
                with self.subTest(clause=number, fact=fact):
                    self.assertIn(fact, text)

    def test_no_rule_conflicts_with_design(self):
        found = _conflicts(_strip_notes(self.body))
        self.assertEqual(found, [], '\n'.join(f'{n}: 「{h}」— {w}' for n, h, w in found))

    def test_conflict_rules_fire_on_planted_violations(self):
        planted = {
            'refund-allowed': ('剩餘額度可退款', '使用者可以申請退款', '提供全額退款',
                               '按比例退還剩餘額度', '遭盜用時可個案退回點數'),
            'fixed-support-sla': ('我們會在 1 個工作天內回覆', '24 小時內回覆', '三個工作天'),
            'no-content-storage-claim': ('WoowTech 不儲存請求與回應內容', '我們不會保存您的請求'),
            'no-key-storage-claim': ('WoowTech 不保存完整金鑰', '我們不會儲存您的金鑰'),
            'uptime-guarantee': ('我們保證服務不中斷', '保證本服務持續可用'),
            'free-daily-without-approx': ('每天 200 次', '每日200次'),
            'internal-units-or-components': ('額度以 USD 計', '經 LiteLLM 轉送', 'Quota'),
        }
        self.assertEqual(set(planted), {name for name, _, _ in _CONFLICT_RULES})
        for name, samples in planted.items():
            for sample in samples:
                with self.subTest(rule=name, sample=sample):
                    self.assertIn(name, {n for n, _, _ in _conflicts(sample)})
        allowed = (
            '轉換後的 AI 額度不能退回點數或現金', '這是取消沒有完成的轉換，不屬於退款',
            '不得申請退款', '不提供全額退款', '暫停不等於退款',
            '我們會盡快處理並通知您，但不承諾固定的回覆時限', '終止日至少 30 天前',
            'WoowTech 不主動保存請求與回應內容；上游服務可能依其政策記錄',
            '關閉視窗後平台不會再次顯示', 'WoowTech 不保證本服務不中斷',
            '每天約 200 次', '每天可以建立的免費試用金鑰數量有限（目前為 3 把）',
            '1 點＝NT$1（新臺幣 1 元，含稅）', '`qwen3-4b-cluster1`',
        )
        for sample in allowed:
            with self.subTest(allowed=sample):
                self.assertEqual(_conflicts(sample), [])

    def test_review_markers_resolve_to_question_list(self):
        self.assertEqual(self.body.count('【待'), len(_MARKER_RE.findall(self.body)),
                         '有格式不對的【待…】標記（要寫成【待法務確認 Q1】這種形式）')
        for kind, qid in _MARKER_RE.findall(self.body):
            with self.subTest(marker=f'{kind} {qid}'):
                self.assertIn(qid, self.questions)

    def test_question_list_covers_ticket_topics(self):
        refs = [row[1] for row in self.questions.values()]
        self.assertEqual(len(self.questions),
                         len(_table_rows(_section(self.doc, QUESTION_SECTION), r'Q\d+')),
                         '問題編號重複')
        for topic in ('停服', '預付', '境外'):  # U9 的三點
            with self.subTest(u9=topic):
                self.assertTrue(any('U9' in r and topic in r for r in refs))
        for topic in ('U20', 'U21', '責任上限'):
            with self.subTest(topic=topic):
                self.assertTrue(any(topic in r for r in refs))
        self.assertTrue(any('AUP' in r and '停權' in r for r in refs))

    def test_legal_markers_sit_on_the_sensitive_clauses(self):
        def questions_marked_in(number):
            return [qid for _, qid in _MARKER_RE.findall(self.clause_text(number))]

        # 第 4 條的停服不退（U9）與第 6 條的不儲存內容（U21）一定要帶標記。
        for number, u_id in ((4, 'U9'), (6, 'U9'), (6, 'U21')):
            with self.subTest(clause=number, u=u_id):
                self.assertTrue(any(u_id in self.questions.get(q, ('', ''))[1]
                                    for q in questions_marked_in(number)))

    def test_body_markdown_survives_the_sanitizer(self):
        allowed = _allowed_tags()
        for hashes in _HEADING_RE.findall(self.body):
            with self.subTest(heading=hashes):
                self.assertIn(f'h{len(hashes)}', allowed,
                              'html_sanitize.js 不允許這一層標題，匯入後會變成純文字')
        self.assertIsNone(re.search(r'<[A-Za-z/!]', self.body), '條款本體不能有原始 HTML')
        if 'img' not in allowed:
            self.assertNotIn('![', self.body)
        for url in re.findall(r'\]\(([^)]+)\)', self.body):
            with self.subTest(url=url):
                self.assertTrue(url.startswith('https://'), '本體的連結只能是 https 絕對網址')


if __name__ == '__main__':
    unittest.main()
