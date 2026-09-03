"""#107：`zh_TW` 啟用鏈的每一種世界（含它最容易做出的那個假綠）。

## 🔴 本題特有的假綠：`assertTrue(lang.active)` 恆綠

`_activate_lang` 對**已經 active** 的語言一次 write 都不發。所以在一個 `zh_TW`
本來就 active 的 DB 上，「跑完之後 active 是 True」這條斷言**與被測程式碼完全
無關**——把 `_ensure_zh_tw_lang_active` 整個換成 `pass`，它照樣綠。

⇒ 本檔每一條啟用斷言都**先造出 inactive 的世界**，而且造完之後**當場斷言那個世界
真的存在**——兩件事綁在同一個 helper（`_given_inactive_zh_tw`）裡。少了後半，
fixture 壞掉（SQL 沒生效、ORM cache 沒失效、或根本忘了呼叫）會讓所有後續斷言為了
錯的理由變綠；本檔第一版就是忘了呼叫「關掉」那半，8 支測試靠這條斷言才紅出來。

⚠ 不能拿「沙盒 DB 預設不帶 `--load-language` ⇒ 多半是 en_US only」當前提：那是
**這台機器現在剛好**的狀態，不是測試前提。stg clone、`--load-language=zh_TW` 建的
庫、或別人先跑過本檔的啟用測試，都會讓它不成立。

## `.po` 自檢的判準為什麼要用 Odoo 自己的 reader

`PoFileReader` 就是 runtime 真的會拿去讀這個檔的東西。用 polib 或 `msgfmt -c` 當
判準只能證明「那把尺讀得動」——而它們與 Odoo 的**分歧**正是最危險的一格：條目缺
`#. module:` 註解時 Odoo 是整份 AttributeError 硬崩，polib 與 msgfmt 都認為那份檔
完全合法。`test_po_gate_blocks_missing_module_comment` 就是那一格，它同時**正面
舉證**了 polib 讀得動（若不舉證，「gate 有擋」與「檔案本來就壞到誰都讀不動」在
斷言上不可區分）。

## migration loader

版本目錄不是 Python package，一律 importlib 走檔案路徑。
🔴 `spec_from_file_location` 的 name **就是** migration 內 `logging.getLogger(__name__)`
的 logger 名（#744 那支測試因此 mute 錯路徑、靜默什麼都沒 mute）——本檔讓兩者同源。
"""
import importlib.util
import logging
import os
import tempfile
from unittest.mock import patch

import polib

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

#: 被測 model 的 logger（`.po` 自檢的 CRITICAL 從這裡出來）。
LOG_MODEL = 'odoo.addons.woow_paas_platform.models.res_lang'

#: 載入 migration 時給的模組名 ＝ 它的 logger 名（見 docstring）。
_MIGRATION_MODULE = 'woow_paas_platform_lang_zh_tw_107_post_migrate'
LOG_MIGRATION = _MIGRATION_MODULE

#: 本批新增的 migration 目錄；與 `__manifest__.py` 的版號同號（規則 B）。
_MIGRATION_VERSION = '18.0.1.0.176'

#: 任何非空字串即可——`migrate(cr, version)` 只把它當「這不是 fresh install」的訊號。
_FROM_VERSION = '18.0.1.0.175'

_ZH_TW = 'zh_TW'

#: 合法但**缺 `#. module:` 註解**的 po：polib 讀得動，Odoo 的 reader 整份 AttributeError。
_PO_NO_MODULE_COMMENT = '''msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"

#. odoo-javascript
#: code:addons/woow_paas_platform/static/src/paas/x.xml:0
msgid "Hello"
msgstr "哈囉"
'''

#: 連 polib 都解不動的 po（語法壞）。
_PO_SYNTAX_ERROR = '''msgid "unterminated
msgstr "壞掉
this is not a po file at all
'''

#: 合法、可解析、但**一條可用條目都沒有**（只有檔頭）。
_PO_HEADER_ONLY = '''msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
'''


def _addon_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _migrate_fn():
    """以檔案路徑載入 migration（版本目錄不是 Python package，import 不到）。"""
    path = os.path.join(
        _addon_root(), 'migrations', _MIGRATION_VERSION, 'post-migrate.py')
    spec = importlib.util.spec_from_file_location(_MIGRATION_MODULE, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.migrate


class _ZhTwActivationFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Lang = self.env['res.lang']
        self.migrate = _migrate_fn()

    # ------------------------------------------------------------------ #
    #  fixture：造出 inactive 的世界（本檔所有判別式的前提）                #
    # ------------------------------------------------------------------ #
    def _deactivate_zh_tw(self):
        """裸 SQL 關掉 zh_TW。

        走 ORM 會撞 `res_lang.write()` 的三個 `UserError`（有 user／partner 的
        lang 指著它、或它是 Odoo 建置時的語言）。裸 SQL 在 `TransactionCase` 的
        cursor 內，隨測試 rollback，不外溢。
        """
        self.env.cr.execute(
            "UPDATE res_lang SET active = false WHERE code = %s", (_ZH_TW,))
        self.env['res.lang'].invalidate_model(['active'])
        self.env.registry.clear_cache()

    def _zh_tw(self):
        return self.Lang.with_context(active_test=False).search(
            [('code', '=', _ZH_TW)])

    def _given_inactive_zh_tw(self):
        """造出 inactive 的世界，並**當場證明**它成立。

        🔴 「關掉」與「證明關掉了」刻意綁在同一個 helper 裡，不拆成兩個方法：
        本檔第一版把它們拆開，於是有 8 支測試只呼叫了斷言那半、忘了呼叫關掉那半
        —— 那 8 支在「zh_TW 已經 active」的 DB 上會**全部變綠而且完全沒有鑑別力**
        （`_activate_lang` 對已 active 的語言一次 write 都不發）。是這條斷言把它
        抓出來的；helper 合併之後，連「忘了造世界」這個錯法都不存在了。
        """
        self._deactivate_zh_tw()
        lang = self._zh_tw()
        self.assertTrue(
            lang, "res.lang 裡找不到 zh_TW —— base 的 res.lang.csv 應該恆有這一列")
        self.assertFalse(
            lang.active,
            "fixture 失效：zh_TW 仍是 active，接下來的啟用斷言將完全沒有鑑別力")
        return lang

    def _po_path_patch(self, path):
        """把 model 讀的 `.po` 路徑換掉（context manager，離開自動還原）。"""
        return patch.object(
            type(self.env['res.lang']), '_woow_zh_tw_po_path',
            lambda self: path)


class TestLangZhTwActivation107(_ZhTwActivationFixture):

    # ------------------------------------------------------------------ #
    #  P4 —— 啟用判別式：先造 inactive 世界，再看它真的被翻起來             #
    # ------------------------------------------------------------------ #
    def test_migrate_activates_zh_tw_from_inactive_world(self):
        lang = self._given_inactive_zh_tw()
        before = lang.active

        self.migrate(self.env.cr, _FROM_VERSION)

        lang = self._zh_tw()
        self.assertFalse(before, "前置狀態應為 inactive（見 _given_inactive_zh_tw）")
        self.assertTrue(
            lang.active,
            "migrate 跑完之後 zh_TW 仍未啟用 —— 這正是 /woow 停在英文的形狀")

    def test_model_method_activates_zh_tw_from_inactive_world(self):
        """三個落點共用的那一份實作，直驅一次。"""
        self._given_inactive_zh_tw()

        returned = self.Lang._ensure_zh_tw_lang_active()

        self.assertTrue(returned, "應回傳被啟用的 res.lang recordset，不是空集合")
        self.assertEqual(returned.code, _ZH_TW)
        self.assertTrue(self._zh_tw().active)

    # ------------------------------------------------------------------ #
    #  P5 —— 冪等                                                          #
    # ------------------------------------------------------------------ #
    def test_migrate_is_idempotent(self):
        self._given_inactive_zh_tw()

        try:
            self.migrate(self.env.cr, _FROM_VERSION)
            self.migrate(self.env.cr, _FROM_VERSION)
        except Exception as exc:  # noqa: BLE001
            self.fail("migrate 跑第二次炸了：%s: %s" % (type(exc).__name__, exc))

        self.assertTrue(self._zh_tw().active)
        self.assertEqual(
            len(self._zh_tw()), 1, "不該長出第二列 zh_TW")

    # ------------------------------------------------------------------ #
    #  P3 —— `.po` 自檢：三種「不啟用」的世界，log 訊息必須互相可分辨        #
    # ------------------------------------------------------------------ #
    def test_po_gate_blocks_when_file_missing(self):
        self._given_inactive_zh_tw()
        missing = os.path.join(
            _addon_root(), 'i18n', 'zh_TW.this-file-does-not-exist.po')
        self.assertFalse(os.path.exists(missing))

        with self._po_path_patch(missing):
            with self.assertLogs(LOG_MODEL, level=logging.CRITICAL) as captured:
                returned = self.Lang._ensure_zh_tw_lang_active()

        self.assertFalse(returned, "gate 擋下時應回空 recordset")
        self.assertFalse(
            self._zh_tw().active,
            "🔴 fail-safe 方向錯了：.po 不存在時仍啟用了語系 ⇒ 畫面會變成 "
            "Odoo 原生中文混本模組英文")
        self.assertIn('翻譯檔不存在', '\n'.join(captured.output))

    def test_po_gate_blocks_missing_module_comment(self):
        """🔴 polib 讀得動、Odoo 讀不動的那一格。

        先**正面舉證** polib 認為這份檔完全合法，再斷言 gate 仍然擋下來——少了
        前半，「gate 有擋」與「這份檔壞到誰都讀不動」在斷言上不可區分。
        """
        self._given_inactive_zh_tw()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'zh_TW.po')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(_PO_NO_MODULE_COMMENT)

            # 陽性對照：第二把尺（polib）認為這份檔沒問題。
            self.assertEqual(
                len(polib.pofile(path)), 1,
                "陽性對照失效：polib 本來就該讀得動這份檔")

            with self._po_path_patch(path):
                with self.assertLogs(
                        LOG_MODEL, level=logging.CRITICAL) as captured:
                    returned = self.Lang._ensure_zh_tw_lang_active()

        self.assertFalse(returned)
        self.assertFalse(self._zh_tw().active)
        joined = '\n'.join(captured.output)
        self.assertIn('讀不動', joined)
        self.assertIn(
            'AttributeError', joined,
            "log 要帶出實際例外型別，否則現場分不出是哪一種壞")

    def test_po_gate_blocks_syntax_error(self):
        self._given_inactive_zh_tw()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'zh_TW.po')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(_PO_SYNTAX_ERROR)

            with self._po_path_patch(path):
                with self.assertLogs(
                        LOG_MODEL, level=logging.CRITICAL) as captured:
                    returned = self.Lang._ensure_zh_tw_lang_active()

        self.assertFalse(returned)
        self.assertFalse(self._zh_tw().active)
        self.assertIn('讀不動', '\n'.join(captured.output))

    def test_po_gate_blocks_empty_po(self):
        self._given_inactive_zh_tw()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'zh_TW.po')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(_PO_HEADER_ONLY)

            with self._po_path_patch(path):
                with self.assertLogs(
                        LOG_MODEL, level=logging.CRITICAL) as captured:
                    returned = self.Lang._ensure_zh_tw_lang_active()

        self.assertFalse(returned)
        self.assertFalse(self._zh_tw().active)
        self.assertIn('一條可用條目都沒有', '\n'.join(captured.output))

    def test_po_gate_messages_are_mutually_distinguishable(self):
        """三種成因各有一個**只出現在自己身上**的標記詞。

        ⚠ 判準不是「三句話不一樣」——那太弱：只要其中一句是另一句的超字串，
        用 grep 找現場 log 時就會同時命中兩種成因。這裡逐個標記詞去數它命中
        幾句，答案必須恰好是 1。
        """
        from odoo.addons.woow_paas_platform.models import res_lang as mod

        markers = {
            mod._PROBLEM_MISSING: '翻譯檔不存在',
            mod._PROBLEM_UNREADABLE: '讀不動',
            mod._PROBLEM_EMPTY: '一條可用條目都沒有',
        }
        self.assertEqual(
            set(markers), set(mod._PROBLEM_MESSAGES),
            "model 端新增／改名了成因，本測試的標記詞表沒跟上")

        for kind, marker in markers.items():
            hits = [k for k, msg in mod._PROBLEM_MESSAGES.items()
                    if marker in msg]
            self.assertEqual(
                hits, [kind],
                "標記詞 %r 命中 %s（應只命中 %s）⇒ 現場 grep 分不出成因"
                % (marker, hits, kind))

    def test_migration_logs_error_when_gate_blocks(self):
        """gate 擋下時 migration 要留可追的 ERROR，而不是安靜跑完。"""
        self._given_inactive_zh_tw()
        missing = os.path.join(_addon_root(), 'i18n', 'zh_TW.absent.po')

        with self._po_path_patch(missing):
            with self.assertLogs(LOG_MIGRATION, level=logging.ERROR) as cap:
                self.migrate(self.env.cr, _FROM_VERSION)

        self.assertFalse(self._zh_tw().active)
        self.assertIn('**未**啟用', '\n'.join(cap.output))

    # ------------------------------------------------------------------ #
    #  真實出貨的 `.po` 本身要通過自己的 gate                                #
    # ------------------------------------------------------------------ #
    def test_shipped_po_passes_its_own_gate(self):
        """本 repo 出貨的 `src/i18n/zh_TW.po` 必須是「可用」的。

        這條同時是前面那些 gate 測試的陽性對照：沒有它，一支「什麼都擋」的壞
        gate 會讓上面每一條負向斷言全部變綠。
        """
        self.assertIsNone(
            self.Lang._woow_zh_tw_po_problem(),
            "出貨的 zh_TW.po 過不了自己的 gate ⇒ 上線後語系不會被啟用")

    # ------------------------------------------------------------------ #
    #  fail-loud：`res_lang` 根本沒有 zh_TW 那一列                          #
    # ------------------------------------------------------------------ #
    def test_raises_when_res_lang_row_absent(self):
        """空 recordset 不是降級狀態，是 base 的 seed 壞了 —— 不得靜默 return。"""
        Lang = type(self.env['res.lang'])
        with patch.object(Lang, '_activate_lang',
                          lambda self, code: self.browse()):
            with self.assertRaises(ValidationError):
                self.Lang._ensure_zh_tw_lang_active()


class TestZhTwActivationWiring107(TransactionCase):
    """落點接線的結構守線 —— 這些錯法**全部是靜默的**。"""

    def test_function_element_is_not_under_noupdate(self):
        """🔴 `<function>` 包進 `<data noupdate="1">` 會靜默不跑（`_tag_function`）。"""
        from lxml import etree

        path = os.path.join(
            _addon_root(), 'data', 'lang_zh_tw_activation.xml')
        tree = etree.parse(path)
        functions = tree.findall('.//function')
        self.assertEqual(
            len(functions), 1,
            "data/lang_zh_tw_activation.xml 應該恰好有一個 <function>")

        fn = functions[0]
        self.assertEqual(fn.get('model'), 'res.lang')
        self.assertEqual(fn.get('name'), '_ensure_zh_tw_lang_active')

        for ancestor in fn.iterancestors():
            self.assertNotEqual(
                (ancestor.get('noupdate') or '0').strip(), '1',
                "🔴 <function> 落在 noupdate=\"1\" 的區塊底下 ⇒ `-u` 路徑會**靜默**"
                "不跑（odoo/tools/convert.py::_tag_function），既有 DB 永遠不會"
                "啟用 zh_TW，而且沒有任何 log")

    def test_data_file_is_registered_in_manifest(self):
        """沒進 manifest 的 data 檔不會被載入 —— 同樣沒有任何錯誤輸出。"""
        import ast

        manifest_path = os.path.join(_addon_root(), '__manifest__.py')
        with open(manifest_path, encoding='utf-8') as fh:
            manifest = ast.literal_eval(fh.read())
        self.assertIn('data/lang_zh_tw_activation.xml', manifest['data'])

    def test_migration_dir_is_within_the_manifest_window(self):
        """本支 migration 的目錄版號必須 **<= manifest 版號**（否則靜默 no-op）。

        Odoo 的執行視窗是 `installed < 目錄 <= manifest`
        （`odoo/modules/migration.py` 的 `compare()`）⇒ 目錄版號**大於** manifest
        的那一支永遠不會被執行，而且**不報錯**。這條守的就是那一格。

        🔴 **2026-09-03（#993）改判準：`==` → `<=`。**
        原本寫的是 `assertEqual(manifest['version'], _MIGRATION_VERSION)`，
        那把「manifest 版號」硬綁在 **#107 自己的目錄名**上
        ⇒ **之後任何一顆帶 migration 的 PR bump 版號都會讓這條紅**，
        而它紅的理由與 #107 這支 migration 會不會被執行**無關**
        （#993 bump 到 `18.0.1.0.177` 時實際撞到：`'18.0.1.0.177' != '18.0.1.0.176'`）。

        ⚠ 換句話說，原本的斷言驗的是一個**會過期的巧合**（當時 #107 剛好是最新版號），
        不是它宣稱要守的性質。改成 `<=` 之後，這條在往後每一次 bump 都仍然成立，
        而「目錄 > manifest ⇒ 靜默 no-op」那個真正的風險依然被擋住。
        """
        import ast

        from odoo.tools.parse_version import parse_version

        manifest_path = os.path.join(_addon_root(), '__manifest__.py')
        with open(manifest_path, encoding='utf-8') as fh:
            manifest = ast.literal_eval(fh.read())
        self.assertLessEqual(
            parse_version(_MIGRATION_VERSION),
            parse_version(manifest['version']),
            '#107 的 migration 目錄 %s 必須 <= manifest 版號 %s，否則 Odoo 的'
            '執行視窗（installed < 目錄 <= manifest）永遠涵蓋不到它，而且不報錯'
            % (_MIGRATION_VERSION, manifest['version']))
        self.assertTrue(os.path.isfile(os.path.join(
            _addon_root(), 'migrations', _MIGRATION_VERSION,
            'post-migrate.py')))
