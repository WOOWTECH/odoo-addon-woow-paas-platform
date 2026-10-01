"""#107 第四層：日期／時間／數字格式化必須跟隨 Odoo 使用者語系的守線。

本 repo 沒有可執行 JS 的測試基礎設施（無 hoot/QUnit，`HttpCase.browser_js` 在
paas-odoo image 內只會 SkipTest），所以這組是**靜態原始碼掃描**，與
``test_agent_pod_phase_c_sse_gateway.py`` 的 XSS guard 同一形態。

它守得住的：新的 call site 又寫死 locale、或又用回跟隨瀏覽器語言的 Intl API。
它守不住的：helper 內部實作被改壞（例如 `parseBackendDate` 改成回 invalid
DateTime）、以及執行期真實渲染結果——那兩者要靠 #107 交付報告裡的 D1/D2
瀏覽器判別式，本檔刻意不假裝涵蓋。

驗收判準（issue #107 §驗收）：

LOC.0  探針自檢（陽性對照）——先證明掃描器真的會動，下面的陰性結果才可採信。
LOC.1  `src/static/src/paas/**/*.js` 內 `toLocale*` 呼叫站點歸零。
       ⚠ 判準是「呼叫站點」不是「字面出現次數」：註解裡提到這個名字不算。
LOC.2  沒有任何硬編 BCP-47 語言標籤字面值（`en-US` / `zh_TW` …）。
       ⚠ 判準是「地區碼大寫」——具名盲區與理由見 `_BCP47_LITERAL_RE` 上方。
LOC.3  QWeb 樣板不得把後端日期欄位裸 `t-esc` 出來（那是把伺服器序列化格式
       直接印給租戶看，#107 已修 5 處）。
LOC.4  `datetime_helpers` 的行為開關 `BACKEND_NAIVE_DATETIME_READING`
       仍存在且預設維持現況——它是 #914 的切換點，被人順手改掉會在沒有任何
       時區驗證的情況下改變全站顯示時間。
"""

import os
import re

from odoo.tests.common import TransactionCase, tagged

# ---------------------------------------------------------------------------
# 掃描範圍
# ---------------------------------------------------------------------------
_STATIC_SRC = ('..', 'static', 'src')

# vendored 第三方打包檔：不是本專案原始碼，其 minified 內容命中沒有意義。
_VENDORED_DIRS = ('paas/lib/',)
_VENDORED_FILES = frozenset({
    'paas/lib/purify.min.js',
    'paas/lib/marked.min.js',
})

# 跟隨「瀏覽器語言」而非 Odoo 語系的 Intl 家族 API。
# 命名為 method 名而非整串呼叫，因為它們都是 `<expr>.toLocaleX(` 的形態。
_BROWSER_LOCALE_METHODS = (
    'toLocaleDateString',
    'toLocaleTimeString',
    'toLocaleString',
)
_INTL_CONSTRUCTORS = (
    'Intl.DateTimeFormat',
    'Intl.NumberFormat',
)

# BCP-47 語言標籤字面值。
#
# 🔴 判準是「**地區碼大寫**」（`zh-TW` / `en_US` / `pt-BR`），刻意**不**涵蓋全小寫寫法
# （`"en-us"`）。理由是實測出來的訊噪比：寬判準（地區碼大小寫皆收）在本 repo 的 JS 裡
# 命中 37 處，其中 **36 處是誤報**——`in_progress` / `ip_range` / `vpn_key` / `ai-chat` /
# `not-found` 這類識別字在形狀上與 BCP-47 標籤不可區分。噪音大的 gate 會被關掉，
# 關掉的 gate 等於沒有。
#
# ⚠ 這是**具名的盲區**，不是遺漏，而且它比「不常見的寫法」更嚴重一點：
# **BCP-47 標籤本身是 case-insensitive**（RFC 5646 §2.1.1——大小寫只是慣例，
# `zh-tw` 與 `zh-TW` 一樣合法，`Intl` / `toLocaleDateString` 兩者都吃）
# ⇒ 有人寫小寫，本 check **不會紅**。
#
# 這是**刻意用漏報換掉 36:1 的誤報**：形狀上分不開的東西，判準只能選一邊犧牲。
# 選漏報的理由是它有第二道防線、而誤報沒有——那一格由 LOC.1 覆蓋：本 app 已經
# 沒有任何 `toLocale*` / `Intl.*` 呼叫站點，locale 字面值**沒有地方可以被當成
# locale 參數用**。要繞過這兩條就得同時（a）新增一個 `toLocale*` 呼叫站點且
# （b）用小寫標籤——(a) 會先被 LOC.1 擋下。
#
# ⚠ 誠實註記：當初選「地區碼大寫」是照實測的訊噪比選的（36 誤報 : 1 真發現），
# **不是**先評估過 BCP-47 的大小寫規則才選的。取捨結論經覆核後不變，
# 但理由補完整——不然下一個人會以為小寫是非法寫法而不是被放掉的一格。
_BCP47_LITERAL_RE = re.compile(r"""(['"])([a-z]{2,3}[-_][A-Z][A-Za-z]{1,7})\1""")

# LOC.2 的具名豁免。key 是「檔案:標籤值」。
#
# ⚠ 裡面的每一格都是**已知的、仍在發生的**缺陷，不是「可以這樣寫」。
# 🔴 任何**新的**硬編語言標籤都不在這份 dict 裡 ⇒ 直接紅。
# 修掉一格就把 entry 刪掉——本 check 有反向檢查，留著已修好的 entry 會紅。
_LOCALE_LITERAL_EXEMPT = {
    ('paas/pages/settings/AddPaymentMethodPage.js', 'zh-TW'): '#920',
}

# LOC.3 的具名豁免。key 是「檔案:t-esc 表達式」而不是行號：行號會被格式化重排
# 改掉（新的假紅），表達式則只有真的改了樣板才會變。
#
# ⚠ 這份 dict 裡的每一格都是**已知的、仍在發生的**缺陷，不是「可以這樣寫」。
# 之所以不在 #107 那批一起修：那批的範圍判準是「這個 component 有沒有消費本批
# 要改的日期 helper」——`OverviewTab` 有（`services/utils.js` 的 `formatDate`，
# 同一張卡片的 `:133` / `:141` 都走它）⇒ 不修 `:107` 會讓同一張卡片出現兩種格式
# ⇒ 在範圍。下面這幾格所在的 component **完全沒有 import 任何日期 helper**，
# 其日期顯示本來就全部是裸的 ⇒ #107 那批不改變它們任何行為 ⇒ 不在範圍。
#
# 🔴 任何**新的**裸 `t-esc` 都不在這份 dict 裡 ⇒ 直接紅。
# 修掉一格就把對應的 entry 刪掉——entry 留著而缺陷已修，不會紅，但會誤導下一個人。
# #1310 已把 SecurityAccessDetailPage 的三處（next_payment／trial_end_date／
# created_at）改走 formatUserDate／formatUserDateTime，自清單移除。
_BARE_DATE_TESC_EXEMPT = {
    ('paas/pages/project-detail/sections/McpServersSection.xml',
     'server.last_sync'): '#918',
}

# `datetime_helpers.js` 的行為開關：#914 的切換點。
_ZONE_SWITCH_FILE = 'paas/utils/datetime_helpers.js'
_ZONE_SWITCH_NAME = 'BACKEND_NAIVE_DATETIME_READING'
_ZONE_SWITCH_DEFAULT = 'ecmascript-date'


def _strip_js_comments(src):
    """把註解換成等長空白，字串／樣板／regex 的內容保留。

    與 XSS guard 的 ``_lex_js`` 同一個目的，但本檔只需要「去掉註解」這一半：
    LOC.2 要在**字串內容**裡找 locale 字面值，所以不能把字串也遮掉。

    回傳與 ``src`` 等長的字串（等長是刻意的——行號才不會漂移）。
    """
    out = list(src)
    i, n = 0, len(src)
    state = None          # None / 'line' / 'block' / 'str' / 'tpl' / 're'
    quote = ''
    prev_significant = ''
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ''
        if state is None:
            if ch == '/' and nxt == '/':
                state = 'line'
                out[i] = out[i + 1] = ' '
                i += 2
                continue
            if ch == '/' and nxt == '*':
                state = 'block'
                out[i] = out[i + 1] = ' '
                i += 2
                continue
            if ch in ('"', "'"):
                state, quote = 'str', ch
            elif ch == '`':
                state = 'tpl'
            elif ch == '/' and prev_significant not in ('', ')', ']', '}') \
                    and not prev_significant.isalnum() and prev_significant != '_':
                # 前一個有意義字元不像運算元 ⇒ 這個 `/` 開啟 regex literal。
                state = 're'
            if not ch.isspace():
                prev_significant = ch
            i += 1
            continue

        if state == 'line':
            if ch == '\n':
                state = None
            else:
                out[i] = ' '
            i += 1
            continue

        if state == 'block':
            if ch == '*' and nxt == '/':
                out[i] = out[i + 1] = ' '
                state = None
                i += 2
                continue
            if ch != '\n':
                out[i] = ' '
            i += 1
            continue

        # 字串／樣板／regex：內容保留，只處理跳脫與結束
        if ch == '\\':
            i += 2
            continue
        if state == 'str' and ch == quote:
            state = None
        elif state == 'tpl' and ch == '`':
            state = None
        elif state == 're' and ch == '/':
            state = None
        elif state == 're' and ch == '\n':
            state = None          # regex 不跨行 ⇒ 判錯時自我修正，不會吃掉整檔
        i += 1
    return ''.join(out)


@tagged('post_install', '-at_install')
class TestLocaleFormattingGuard107(TransactionCase):
    """守住「日期／數字格式跟隨 Odoo 使用者語系」（#107 第四層）。"""

    # ----- 掃描 helper ---------------------------------------------------- #
    def _base_dir(self):
        return os.path.abspath(os.path.join(os.path.dirname(__file__), *_STATIC_SRC))

    def _iter_js(self):
        base = self._base_dir()
        for root, _dirs, files in os.walk(base):
            for fn in sorted(files):
                if not fn.endswith('.js'):
                    continue
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, base).replace(os.sep, '/')
                if rel in _VENDORED_FILES or rel.startswith(_VENDORED_DIRS):
                    continue
                with open(path, 'r', encoding='utf-8') as f:
                    yield rel, f.read()

    def _iter_xml(self):
        base = self._base_dir()
        for root, _dirs, files in os.walk(base):
            for fn in sorted(files):
                if not fn.endswith('.xml'):
                    continue
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, base).replace(os.sep, '/')
                with open(path, 'r', encoding='utf-8') as f:
                    yield rel, f.read()

    def _backend_date_keys(self):
        """資源側枚舉：後端會送進 payload 的日期欄位名。

        ⚠ 刻意**不用**「欄位名長得像日期」的 heuristic ——#107 盤點時那個做法
        漏掉了 ``next_payment`` 與 ``last_sync`` 兩格（名字裡沒有 date/_at/time）。
        改成從後端「誰會寫到那個位置」枚舉：凡是「字面 key 冒號 某表達式
        `.isoformat()`」的形態，那個 key 就算。

        🔴 掃描範圍刻意只有 `src/controllers/` 與 `src/models/`——payload 的產地只在
        這兩處。**不掃 `src/tests/`** 的理由不是效能，是本檔自己踩過：這個 docstring
        底下原本寫著一行示範用的字面形態，而掃描器把**自己的說明文字**當成了一個
        真的 payload 欄位（欄位名 `key`），於是 `t-esc="spec.key"` / `t-esc="group.key"`
        兩處完全無關的樣板被誤報。談論某個形態的檔與實作它的檔，在 regex 眼中一樣。
        （實測：收窄範圍後 23 → 22 個 key，失去的那一個正是本檔自己的示範。）
        """
        src_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        keys = set()
        pat = re.compile(r"""['"]([a-z_][a-z0-9_]*)['"]\s*:\s*[^,\n]*\.isoformat\(\)""")
        for sub in ('controllers', 'models'):
            for root, _dirs, files in os.walk(os.path.join(src_root, sub)):
                for fn in files:
                    if not fn.endswith('.py'):
                        continue
                    with open(os.path.join(root, fn), 'r', encoding='utf-8') as f:
                        keys.update(pat.findall(f.read()))
        # 線 B：`src/controllers/paas.py` 漏了 `.isoformat()`，靠 Odoo 的
        # json_default 序列化 ⇒ 上面的 pattern 抓不到，具名補進來（另案 #916）。
        keys.add('last_release_sync_at')
        return keys

    # ----- LOC.0 掃描器自檢（陽性對照）------------------------------------ #
    def test_LOC0_scanner_self_check(self):
        """先證明掃描器真的會動，下面幾條的陰性結果才可採信。

        「找不到硬編 locale」與「掃描器壞掉／檔案讀成空字串」輸出長得一模一樣。
        這條就是那個陽性對照。
        """
        cases = [
            # (名稱, 輸入, 剝完必須還在, 剝完必須不見)
            ('行註解裡的呼叫',
             'const a = 1; // d.toLocaleDateString("en-US")\nconst b = 2;',
             ['const a = 1;', 'const b = 2;'], ['toLocaleDateString']),
            ('區塊註解裡的呼叫',
             'x;/* d.toLocaleString("zh-TW")\n more */y;',
             ['x;', 'y;'], ['toLocaleString', 'zh-TW']),
            ('字串內的 //（URL）不可被當成註解',
             'const u = "https://a.b/c"; d.toLocaleDateString("en-US");',
             ['toLocaleDateString', 'en-US', 'https://a.b/c'], []),
            ('字串內容必須保留（LOC.2 靠它找字面值）',
             'const s = "en-US";',
             ['en-US'], []),
            ('樣板含 // 不可被當成註解',
             'const t = `a//b`; d.toLocaleString("ja-JP");',
             ['toLocaleString', 'ja-JP'], []),
            ('regex 含斜線不可吃掉後續程式碼',
             'const p = /^a\\/b$/; d.toLocaleDateString("de-DE");',
             ['toLocaleDateString', 'de-DE'], []),
            ('除號不是 regex',
             'const q = a / b / c; d.toLocaleString("fr-FR");',
             ['toLocaleString', 'fr-FR'], []),
        ]
        for name, src, keep, drop in cases:
            stripped = _strip_js_comments(src)
            self.assertEqual(
                len(stripped), len(src),
                f"LOC.0[{name}]: 剝完長度改變，行號會漂移。")
            for frag in keep:
                self.assertIn(
                    frag, stripped,
                    f"LOC.0[{name}]: 掃描器把 {frag!r} 一起吃掉了 —— 它會漏看真正的站點。")
            for frag in drop:
                self.assertNotIn(
                    frag, stripped,
                    f"LOC.0[{name}]: 掃描器沒有剝掉註解裡的 {frag!r} —— 會產生假紅。")

        # 陽性對照的第二半：掃描器對「真的存在的站點」必須命中。
        positive = 'const d = new Date(); d.toLocaleDateString("en-US", {});'
        hits = self._scan_browser_locale_calls('probe.js', positive)
        self.assertEqual(
            len(hits), 1,
            "LOC.0: 掃描器對一個明確存在的 toLocaleDateString 站點沒有命中 —— "
            f"LOC.1/LOC.2 的零命中結果一律不可採信。實得 {hits!r}")

    def _scan_browser_locale_calls(self, rel, src):
        """回傳 ``[(行號, 方法名, 該行原文), ...]``。"""
        stripped = _strip_js_comments(src)
        hits = []
        for lineno, line in enumerate(stripped.split('\n'), 1):
            for method in _BROWSER_LOCALE_METHODS:
                if f'.{method}(' in line:
                    hits.append((lineno, method, src.split('\n')[lineno - 1].strip()))
            for ctor in _INTL_CONSTRUCTORS:
                if f'{ctor}(' in line or f'new {ctor}(' in line:
                    hits.append((lineno, ctor, src.split('\n')[lineno - 1].strip()))
        return hits

    # ----- LOC.1 toLocale* 呼叫站點歸零 ----------------------------------- #
    def test_LOC1_no_browser_locale_calls(self):
        """`toLocale*` / `Intl.*` 一律不得用於 `/woow` 的顯示格式化。

        它們跟隨 ``navigator.language``（瀏覽器語言），不跟隨 Odoo 使用者的
        ``res.users.lang`` ⇒ 同一個使用者換個瀏覽器就會看到不同格式，
        而且完全不受 Odoo 語系設定影響。

        正確做法：`utils/datetime_helpers.js` 的 `formatUserDate` /
        `formatUserDateTime` / `formatUserTime`，或數字用
        `@web/core/utils/numbers` 的 `formatFloat`。
        """
        offenders = []
        for rel, src in self._iter_js():
            for lineno, method, text in self._scan_browser_locale_calls(rel, src):
                offenders.append(f"  {rel}:{lineno}  [{method}]  {text}")
        self.assertEqual(
            offenders, [],
            "LOC.1：發現跟隨瀏覽器語言的格式化呼叫（#107 已把這些收斂到 "
            "utils/datetime_helpers.js 與 @web/core/utils/numbers）：\n"
            + "\n".join(offenders))

    # ----- LOC.2 硬編 locale 字面值歸零 ----------------------------------- #
    def test_LOC2_no_hardcoded_locale_literal(self):
        """任何 BCP-47 語言標籤字面值都不該出現在 `/woow` 的 JS 裡。

        #107 之前有 6 處：5 個 ``"en-US"`` ＋ 1 個 ``"zh-TW"``，
        造成同一個畫面上的日期格式取決於走哪條 code path。
        """
        offenders, exempted = [], []
        for rel, src in self._iter_js():
            stripped = _strip_js_comments(src)
            for lineno, line in enumerate(stripped.split('\n'), 1):
                for m in _BCP47_LITERAL_RE.finditer(line):
                    tag = m.group(2)
                    if (rel, tag) in _LOCALE_LITERAL_EXEMPT:
                        exempted.append((rel, tag))
                        continue
                    offenders.append(
                        f"  {rel}:{lineno}  {tag!r}  "
                        f"{src.split(chr(10))[lineno - 1].strip()}")
        self.assertEqual(
            offenders, [],
            "LOC.2：發現硬編的語言標籤字面值。顯示格式一律跟隨 Odoo 使用者語系"
            "（`res.lang.date_format`），⛔ 不要寫死：\n" + "\n".join(offenders))

        stale = sorted(set(_LOCALE_LITERAL_EXEMPT) - set(exempted))
        self.assertEqual(
            stale, [],
            "LOC.2：豁免清單有過期 entry —— 這些位置已經沒有硬編語言標籤了，"
            "請把對應的 entry 從 `_LOCALE_LITERAL_EXEMPT` 刪掉：\n"
            + "\n".join(f"  {r}  {t}" for r, t in stale))

    # ----- LOC.3 QWeb 不得裸印後端日期欄位 -------------------------------- #
    def test_LOC3_no_bare_backend_date_in_qweb(self):
        """樣板不得把後端日期欄位直接 `t-esc` 出來。

        那會把伺服器的序列化格式（`2026-08-26T12:34:56.123456` 或
        `2026-08-26 12:34:56`）原封不動印給租戶看 —— 既不是任何語系的格式，
        也洩漏了 wire format。#107 已修 5 處。
        """
        keys = self._backend_date_keys()
        # 陽性對照兩層：條數下限 ＋ 具名的必含項。
        # 只有條數的話，範圍收窄收過頭（例如少掃一個目錄）仍可能滿足門檻。
        self.assertGreater(
            len(keys), 10,
            "LOC.3 陽性對照：從後端枚舉到的日期欄位名太少，掃描器很可能壞了 —— "
            f"實得 {sorted(keys)!r}")
        must_have = {'created_date', 'deployed_at', 'invoice_date',
                     'next_payment', 'last_sync', 'trial_end_date'}
        self.assertEqual(
            sorted(must_have - keys), [],
            "LOC.3 陽性對照：枚舉不到這些一定存在的 payload 日期欄位 —— "
            f"掃描範圍壞了，本條的零命中不可採信。實得 {sorted(keys)!r}")

        pat = re.compile(r"""t-(?:esc|out)="([^"]+)\"""")
        offenders, exempted = [], []
        for rel, src in self._iter_xml():
            for lineno, line in enumerate(src.split('\n'), 1):
                for m in pat.finditer(line):
                    expr = m.group(1).strip()
                    if '(' in expr:      # 有經過格式化函式
                        continue
                    if expr.split('.')[-1].strip() not in keys:
                        continue
                    ticket = _BARE_DATE_TESC_EXEMPT.get((rel, expr))
                    if ticket:
                        exempted.append((rel, expr, ticket))
                        continue
                    offenders.append(f"  {rel}:{lineno}  t-esc=\"{expr}\"")
        self.assertEqual(
            offenders, [],
            "LOC.3：發現裸印後端日期欄位的樣板。請改走元件上的 "
            "formatUserDate / formatUserDateTime：\n" + "\n".join(offenders))

        # 豁免清單的反向檢查：每一格都必須**真的還在**。
        # 缺陷修好了卻留著 entry 不會紅，但會讓下一個人以為那裡還有問題；
        # 更糟的是它會讓「豁免了幾格」這個數字停止反映現況。
        still_present = {(rel, expr) for rel, expr, _ in exempted}
        stale = sorted(set(_BARE_DATE_TESC_EXEMPT) - still_present)
        self.assertEqual(
            stale, [],
            "LOC.3：豁免清單有過期 entry —— 這些位置已經沒有裸 `t-esc` 了，"
            "請把對應的 entry 從 `_BARE_DATE_TESC_EXEMPT` 刪掉：\n"
            + "\n".join(f"  {r}  {e}" for r, e in stale))

    # ----- LOC.4 時區行為開關仍在、且維持現況 ----------------------------- #
    def test_LOC4_timezone_switch_still_defaults_to_current_behaviour(self):
        """`BACKEND_NAIVE_DATETIME_READING` 是 #914 的切換點，不可被順手改掉。

        改成 ``"utc-wire-format"`` 會同時改變**每一個顯示時間的地方**的值
        （台北下相差 8 小時），而且只有日期的欄位與帶時間的欄位位移方向相反。
        那需要跨時區、跨粒度的獨立驗證 ⇒ 由 #914 負責，⛔ 不要在別的 PR 裡順手翻。

        這條紅了不代表程式壞了，代表**有人在做一個需要 #914 那組驗證的變更**。
        """
        path = os.path.join(self._base_dir(), _ZONE_SWITCH_FILE)
        self.assertTrue(
            os.path.exists(path),
            f"LOC.4：{_ZONE_SWITCH_FILE} 不存在 —— 日期格式化的單一入口被移除了？")
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        m = re.search(
            rf"""export\s+const\s+{_ZONE_SWITCH_NAME}\s*=\s*(['"])([^'"]+)\1""", src)
        self.assertIsNotNone(
            m,
            f"LOC.4：在 {_ZONE_SWITCH_FILE} 找不到 `{_ZONE_SWITCH_NAME}` 的宣告。")
        self.assertEqual(
            m.group(2), _ZONE_SWITCH_DEFAULT,
            f"LOC.4：`{_ZONE_SWITCH_NAME}` 已從 {_ZONE_SWITCH_DEFAULT!r} 改成 "
            f"{m.group(2)!r}。切換時區讀法需要 #914 的跨時區驗證，"
            "⛔ 不可在其他 PR 裡順手改；若這就是 #914 本身，請一併更新本測試的期望值。")
