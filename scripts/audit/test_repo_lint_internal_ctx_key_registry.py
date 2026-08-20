#!/usr/bin/env python3
"""``check_internal_ctx_key_registry`` 的注入測試（#788）。

這條 gate 守的是「拿 context key 當內部呼叫憑證、判定只看 truthy」這個 bug class：
``/web/dataset/call_kw`` 把呼叫端送來的 context **整包**換進 env ⇒ 租戶一行 JSON
``context={"<key>": true}`` 就能開閘（#522 在 18.0 上實測成功過）。

**先注入紅燈，再相信綠燈**：一條新 gate 第一個要證明的不是「現在是綠的」，而是
「它會紅」——而且要紅在**該紅的那條判準**上，不是紅在別的理由。#788 明列三條判準
各要一次注入：

  (i)   故意留一把裸 truthy 閘              → ``TestBareTruthyGateIsBlocked``
  (ii)  故意加一個 ``key=True`` 字面值      → ``TestTruthyLiteralIsBlocked``
  (iii) 故意新增一把未登記的 truthy 閘      → ``TestCoverageIsBlocked``

外加四格陽性對照（本 gate 的所有判準都是否定性的，「找不到違規」與「掃描器壞掉」
輸出完全相同）：sentinel 改名／reader 消失／enforced 沒有任何判定點／豁免條目掃不到
布林 guard 讀取點——見 ``TestProbeFailuresAreBlocked``。

跑法：
    python3 scripts/audit/test_repo_lint_internal_ctx_key_registry.py
    # 或 python3 -m pytest scripts/audit/test_repo_lint_internal_ctx_key_registry.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部
依賴——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo，也不依賴 cwd。
"""
import contextlib
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")

_spec = importlib.util.spec_from_file_location("repo_lint_ctx_key_ut", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)


# --------------------------------------------------------------------------- #
#  合成 fixture：一個最小的「模組私有 sentinel」模組                            #
# --------------------------------------------------------------------------- #
#
# 形狀刻意抄真實的 src/models/security_access.py（sentinel class → 實例 → 開閘
# helper → 判定函式），這樣「注入紅燈」改的就是真的會發生的那一個字。

MODEL_OK = '''
class _InternalWidgetWrite:
    __slots__ = ()


_INTERNAL_WIDGET_WRITE = _InternalWidgetWrite()

_CTX_WIDGET_WRITE = 'ut_widget_internal_write'


def _widget_write(records):
    return records.with_context(**{_CTX_WIDGET_WRITE: _INTERNAL_WIDGET_WRITE})


def _is_widget_write(env):
    return env.context.get(_CTX_WIDGET_WRITE) is _INTERNAL_WIDGET_WRITE


class Widget(object):
    def write(self, vals):
        if 'anchor_id' in vals and not _is_widget_write(self.env):
            raise UserError("immutable")
        return super().write(vals)
'''

# (i) 注入：判定退回裸 truthy。這一行在 diff 裡看起來完全無害。
MODEL_BARE_TRUTHY = MODEL_OK.replace(
    "    return env.context.get(_CTX_WIDGET_WRITE) is _INTERNAL_WIDGET_WRITE",
    "    return bool(env.context.get(_CTX_WIDGET_WRITE))")

# (ii) 注入：生產碼寫 truthy 字面值。sentinel 判定根本不認它 ⇒ 這一行**不會開閘**，
#      是無聲失效；更糟的是它會教下一個人「傳 True 就行」。
MODEL_TRUTHY_LITERAL = MODEL_OK.replace(
    "    return records.with_context(**{_CTX_WIDGET_WRITE: _INTERNAL_WIDGET_WRITE})",
    "    return records.with_context(ut_widget_internal_write=True)")

MODEL_TRUTHY_DICT_LITERAL = MODEL_OK.replace(
    "    return records.with_context(**{_CTX_WIDGET_WRITE: _INTERNAL_WIDGET_WRITE})",
    "    return records.with_context(**{'ut_widget_internal_write': True})")

# (iii) 注入：新寫一把沒登記的 truthy 閘。這正是 bug class 每一次復發的形狀。
MODEL_NEW_UNREGISTERED_GATE = MODEL_OK + '''

class Gadget(object):
    def unlink(self):
        if not self.env.context.get('ut_gadget_cascade_unlink'):
            raise UserError("primary gadget is not deletable")
        return super().unlink()
'''

# 陽性對照用的突變。
MODEL_SENTINEL_RENAMED = (MODEL_OK
                          .replace("_INTERNAL_WIDGET_WRITE", "_WIDGET_OK"))
MODEL_READER_GONE = MODEL_OK.replace("_is_widget_write", "_widget_allowed")
MODEL_NO_JUDGE_AT_ALL = '''
class _InternalWidgetWrite:
    __slots__ = ()


_INTERNAL_WIDGET_WRITE = _InternalWidgetWrite()

_CTX_WIDGET_WRITE = 'ut_widget_internal_write'


def _widget_write(records):
    return records.with_context(**{_CTX_WIDGET_WRITE: _INTERNAL_WIDGET_WRITE})
'''

# 值的使用（不是布林閘）——#787 B 段的形態，本 gate 刻意不管，不得誤報。
MODEL_VALUE_USE = MODEL_OK + '''

class Reaper(object):
    def run(self):
        for cf_id in self.env.context.get('ut_unlinked_policy_ids') or []:
            self._drop(cf_id)
        password = self.env.context.get('ut_admin_password')
        return password
'''

# 豁免條目（狀態＝「這把現在就是裸 truthy 閘」）的真實形狀。
MODEL_PENDING_GATE = '''
class Legacy(object):
    def write(self, vals):
        if not self.env.context.get('ut_legacy_skip_sync'):
            self._sync()
        return super().write(vals)
'''


_REGISTRY_OK = {
    "ut_widget_internal_write": {
        "status": "enforced",
        "sentinel": "_INTERNAL_WIDGET_WRITE",
        "defined_in": "src/models/widget.py",
        "reader": "_is_widget_write",
        "issue": "#788",
        "why": "unit-test fixture",
    },
    "ut_legacy_skip_sync": {
        "status": "pending",
        "issue": "#787",
        "why": "unit-test fixture：已知未修的裸 truthy 閘",
    },
}


class _FixtureRoot:
    """一個最小的 addon root（manifest ＋ src/models/*.py）＋ 一份合成 registry。

    registry 換掉是刻意的：真 registry 的 enforced 條目全部錨定在真檔案上，套到
    合成樹會整批紅在「defined_in 不存在」。那種噪音會淹掉「這次注入該紅的那一條」
    ——而測試要證明的正是**紅在對的判準上**。
    """

    def __init__(self, registry=None):
        self.dir = tempfile.mkdtemp(prefix="ctx-key-registry-gate-")
        self.registry = dict(_REGISTRY_OK if registry is None else registry)
        self.write("src/__manifest__.py", "{'name': 'ut'}\n")

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return self

    def run(self):
        with _patched_registry(self.registry):
            return rl.check_internal_ctx_key_registry(self.dir)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


@contextlib.contextmanager
def _patched_registry(registry):
    original = rl.INTERNAL_CTX_KEY_REGISTRY
    rl.INTERNAL_CTX_KEY_REGISTRY = registry
    try:
        yield
    finally:
        rl.INTERNAL_CTX_KEY_REGISTRY = original


def _tree(model=MODEL_OK, legacy=MODEL_PENDING_GATE, registry=None, extra=None):
    with _FixtureRoot(registry=registry) as fx:
        fx.write("src/models/widget.py", model)
        fx.write("src/models/legacy.py", legacy)
        for rel, text in (extra or {}).items():
            fx.write(rel, text)
        return fx.run()


def _joined(violations):
    return "\n".join(violations)


# ------------------------------------------------------------ 陽性對照（綠的前提）

class TestBaselineIsGreen(unittest.TestCase):
    """未突變時必須綠——而且綠得有前提（掃描器真的看見了東西）。"""

    def test_real_repo_is_green(self):
        self.assertEqual(
            rl.check_internal_ctx_key_registry(str(_REPO_ROOT)), [])

    def test_minimal_clean_fixture_passes(self):
        self.assertEqual(_tree(), [])

    def test_probe_actually_sees_the_real_tree(self):
        """真 repo 上掃描器要真的抽到讀取點——不然「零違規」是假的。

        `violations == []` 在「一個檔都沒掃到」「AST 判準抓不到任何形態」時是同一個
        值（memory: feedback_negative_verification_needs_positive_control）。這條直接
        盯掃描結果，讓「綠」與「沒掃到」分得開。
        """
        facts = rl._internal_ctx_key_scan(str(_REPO_ROOT))
        self.assertEqual(facts["parse_errors"], [],
                         "生產碼有檔案 parse 不了 ⇒ 本 gate 對它失去鑑別力")
        self.assertGreater(facts["files"], 50,
                           "掃到的生產檔太少，掃描範圍可能壞了")
        self.assertGreater(facts["read_sites"], 0,
                           "一個 context.get 都沒掃到 ⇒ AST 判準壞了")
        self.assertGreater(len(facts["reads"]), 0,
                           "沒有任何 key 解得出字面值 ⇒ 常數解析壞了")

    def test_probe_sees_the_cross_file_reader_calls(self):
        """#536 那三把**沒有任何直接讀取點**，只經 `_is_sync_control(env, KEY)`。

        少了 reader 這條路，它們會因為「掃不到讀取點」而恆綠——這正是本 gate 最容易
        被做成假綠的一格，所以直接對真 repo 斷言 reader 判定點抓得到，而且抓得到的是
        **跨檔 import 進來的**那個 key 常數（`security_access_access_policy.py` 從
        `security_access` import `_CTX_SKIP_ROUTE_SYNC`）。
        """
        facts = rl._internal_ctx_key_scan(str(_REPO_ROOT))
        keys = {k for _fn, k, _rel, _ln in facts["reader_calls"]}
        self.assertIn("sa_skip_route_sync", keys)
        self.assertIn("sa_rule_replace", keys)
        self.assertIn("sa_force_access_sync", keys)
        cross_file = {rel for _fn, k, rel, _ln in facts["reader_calls"]
                      if k == "sa_skip_route_sync"}
        self.assertIn("src/models/security_access_access_policy.py", cross_file,
                      "跨檔 import 的 key 常數解不出來 ⇒ 判定點會被漏算")

    def test_every_enforced_key_has_a_judge_site_in_the_real_tree(self):
        """真 registry 上每一把 enforced 都要有判定點——這是 (b) 對照的實測面。"""
        facts = rl._internal_ctx_key_scan(str(_REPO_ROOT))
        reads = {}
        for key, rel, lineno, node, parent, owner in facts["reads"]:
            reads.setdefault(key, []).append((node, parent, owner))
        for key, entry in rl.INTERNAL_CTX_KEY_REGISTRY.items():
            if entry.get("status") != "enforced":
                continue
            sentinel = entry["sentinel"]
            direct = sum(
                1 for node, parent, owner in reads.get(key, [])
                if rl._ctx_is_compare(node, parent, sentinel)
                or rl._ctx_assigned_then_compared(node, parent, owner, sentinel))
            via_reader = sum(1 for _f, k, _r, _l in facts["reader_calls"]
                             if k == key)
            self.assertGreater(
                direct + via_reader, 0,
                "enforced key `%s` 在真 repo 找不到任何判定點 ⇒ 它的綠是假的" % key)


# ------------------------------------------------ (i) 注入紅燈：裸 truthy 閘

class TestBareTruthyGateIsBlocked(unittest.TestCase):
    """registry 上 enforced 的 key 被當裸 truthy 布林 guard 讀 ⇒ 必紅。"""

    def test_bare_truthy_read_is_blocked(self):
        violations = _tree(model=MODEL_BARE_TRUTHY)
        self.assertTrue(violations, "裸 truthy 判定卻放行了")
        text = _joined(violations)
        self.assertIn("ut_widget_internal_write", text)
        self.assertIn("裸 truthy", text)
        self.assertIn("src/models/widget.py", text)

    def test_if_statement_form_is_also_blocked(self):
        """`bool(...)` 與 `if ...:` 是同一件事，判準不得只認其中一種寫法。"""
        model = MODEL_OK.replace(
            "    return env.context.get(_CTX_WIDGET_WRITE) is _INTERNAL_WIDGET_WRITE",
            "    if env.context.get(_CTX_WIDGET_WRITE):\n"
            "        return True\n"
            "    return False")
        violations = _tree(model=model)
        self.assertTrue(violations, "`if ctx.get(K):` 形態沒被擋")
        self.assertIn("裸 truthy", _joined(violations))

    def test_not_and_boolop_form_is_also_blocked(self):
        """`if not ctx.get(K) and X:` —— BoolOp 包住的布林 guard 一樣要擋。"""
        model = MODEL_OK.replace(
            "        if 'anchor_id' in vals and not _is_widget_write(self.env):",
            "        if not self.env.context.get(_CTX_WIDGET_WRITE) and vals:")
        violations = _tree(model=model)
        self.assertTrue(violations, "BoolOp 內的布林 guard 沒被擋")
        self.assertIn("裸 truthy", _joined(violations))

    def test_wrong_sentinel_is_blocked(self):
        """比對了，但比的是**別的**物件 ⇒ 不算判定（形狀對、判準錯）。"""
        model = MODEL_OK.replace(
            "is _INTERNAL_WIDGET_WRITE", "is _SOMETHING_ELSE") + \
            "\n_SOMETHING_ELSE = object()\n"
        violations = _tree(model=model)
        self.assertTrue(violations, "比錯 sentinel 卻放行了")

    def test_assign_then_compare_form_is_accepted(self):
        """`v = ctx.get(K)` ＋同函式內 `v is SENTINEL` 是安全且常見的寫法，不得誤報。

        沒有這一格，gate 會逼人把可讀的兩行改寫成一行才過得去——那是 gate 在改壞
        程式碼，不是在守線。
        """
        model = MODEL_OK.replace(
            "    return env.context.get(_CTX_WIDGET_WRITE) is _INTERNAL_WIDGET_WRITE",
            "    value = env.context.get(_CTX_WIDGET_WRITE)\n"
            "    if value is _INTERNAL_WIDGET_WRITE:\n"
            "        return True\n"
            "    return False")
        self.assertEqual(_tree(model=model), [])


# ------------------------------------------- (ii) 注入紅燈：truthy 字面值

class TestTruthyLiteralIsBlocked(unittest.TestCase):
    """enforced key 在生產碼出現 truthy 字面值 ⇒ 必紅。"""

    def test_keyword_true_literal_is_blocked(self):
        violations = _tree(model=MODEL_TRUTHY_LITERAL)
        self.assertTrue(violations, "`key=True` 字面值卻放行了")
        text = _joined(violations)
        self.assertIn("ut_widget_internal_write=True", text)
        self.assertIn("truthy 字面值", text)

    def test_dict_literal_is_blocked(self):
        """`**{'key': True}` 與 `key=True` 是同一件事，兩種載體都要認。"""
        violations = _tree(model=MODEL_TRUTHY_DICT_LITERAL)
        self.assertTrue(violations, "dict literal 形態的 truthy 值沒被擋")
        self.assertIn("truthy 字面值", _joined(violations))

    def test_falsy_literal_is_not_blocked(self):
        """`key=False` 是「關掉」語意，原封不動、不得誤報（#536 明列）。"""
        model = MODEL_OK.replace(
            "    return records.with_context(**{_CTX_WIDGET_WRITE: _INTERNAL_WIDGET_WRITE})",
            "    return records.with_context(ut_widget_internal_write=False)")
        # 開閘 helper 不再開閘，但那不是本條要管的事；本條只管「有沒有 truthy 字面值」。
        self.assertEqual(
            [v for v in _tree(model=model) if "truthy 字面值" in v], [])

    def test_tests_dir_is_exempt(self):
        """`src/tests/` 偽造 payload 正是它的工作，不得誤報（#788 明列）。"""
        violations = _tree(extra={
            "src/tests/test_widget_forgery.py":
                "def test_forge(self):\n"
                "    rec.with_context(ut_widget_internal_write=True).write({})\n"})
        self.assertEqual(violations, [])

    def test_exempt_key_literal_is_not_blocked(self):
        """pending／not-security 的 key 豁免 (ii)——它們本來就還是舊寫法。

        沒有這一格，gate 上線當天就會紅在 #787 那 5 把身上，而它們**刻意先不修**
        （#787 決定逐把驗可達性再動）。
        """
        legacy = MODEL_PENDING_GATE + '''

class LegacyCaller(object):
    def go(self):
        return self.rec.with_context(ut_legacy_skip_sync=True).write({})
'''
        self.assertEqual(_tree(legacy=legacy), [])


# ------------------------------------------- (iii) 注入紅燈：涵蓋率（核心）

class TestCoverageIsBlocked(unittest.TestCase):
    """新寫一把沒登記的 truthy 閘 ⇒ 必紅。少了這條，gate 等於沒做。"""

    def test_unregistered_truthy_gate_is_blocked(self):
        violations = _tree(model=MODEL_NEW_UNREGISTERED_GATE)
        self.assertTrue(violations, "未登記的 truthy 閘卻放行了")
        text = _joined(violations)
        self.assertIn("ut_gadget_cascade_unlink", text)
        self.assertIn("INTERNAL_CTX_KEY_REGISTRY", text)

    def test_message_offers_both_ways_out(self):
        """訊息要給得出下一步：改 sentinel，或登記豁免並寫理由。"""
        text = _joined(_tree(model=MODEL_NEW_UNREGISTERED_GATE))
        self.assertIn("sentinel", text)
        self.assertIn("not-security", text)

    def test_registering_it_as_exempt_makes_it_green(self):
        """登記＋寫理由就放行——但那個動作會出現在 diff 裡被覆核。"""
        registry = dict(_REGISTRY_OK)
        registry["ut_gadget_cascade_unlink"] = {
            "status": "not-security",
            "why": "unit-test fixture：非安全決策",
        }
        self.assertEqual(
            _tree(model=MODEL_NEW_UNREGISTERED_GATE, registry=registry), [])

    def test_exempt_registration_without_reason_is_blocked(self):
        """豁免沒寫理由 ⇒ 必紅。豁免的價值全在那句理由上。"""
        registry = dict(_REGISTRY_OK)
        registry["ut_gadget_cascade_unlink"] = {"status": "not-security"}
        violations = _tree(model=MODEL_NEW_UNREGISTERED_GATE, registry=registry)
        self.assertTrue(violations, "沒有理由的豁免卻放行了")
        self.assertIn("why", _joined(violations))

    def test_pending_without_issue_is_blocked(self):
        """pending 沒單號 ＝ 永久豁免換一個名字 ⇒ 必紅。"""
        registry = dict(_REGISTRY_OK)
        registry["ut_legacy_skip_sync"] = {
            "status": "pending", "why": "先不修"}
        violations = _tree(registry=registry)
        self.assertTrue(violations, "沒單號的 pending 卻放行了")
        self.assertIn("issue", _joined(violations))

    def test_unknown_status_is_blocked(self):
        """status 打錯字會靜默變成豁免（不等於 enforced ⇒ (i)(ii) 全跳過）⇒ 必紅。"""
        registry = dict(_REGISTRY_OK)
        registry["ut_widget_internal_write"] = dict(
            _REGISTRY_OK["ut_widget_internal_write"], status="enforce")
        violations = _tree(model=MODEL_BARE_TRUTHY, registry=registry)
        self.assertTrue(violations, "status 打錯字卻放行了")
        self.assertIn("status", _joined(violations))

    def test_value_use_is_out_of_scope(self):
        """#787 B 段的「值注入」面（`ctx.get(K) or []`、讀密碼）不是 truthy 閘。

        誤報這一類會逼人把不同形態的東西塞進同一份 registry，讓清單失去意義；
        漏報則是刻意的**已宣告**邊界（見 check 的 docstring）。
        """
        self.assertEqual(_tree(model=MODEL_VALUE_USE), [])


# --------------------------------------------- 陽性對照：探針壞掉必須紅

class TestProbeFailuresAreBlocked(unittest.TestCase):
    """本 gate 的判準全是否定性的 ⇒ 探針壞掉必須紅，不能靜默恆綠。"""

    def test_renamed_sentinel_is_blocked(self):
        """sentinel 改名 ⇒ 所有 `is` 比對都比不中 ⇒ 整條會恆綠。必須紅。"""
        violations = _tree(model=MODEL_SENTINEL_RENAMED)
        self.assertTrue(violations, "sentinel 改名卻放行了（恆綠）")
        self.assertIn("_INTERNAL_WIDGET_WRITE", _joined(violations))

    def test_missing_reader_is_blocked(self):
        """登記的 reader 消失 ⇒ 只經 reader 判定的 key 會完全沉默。必須紅。"""
        violations = _tree(model=MODEL_READER_GONE)
        self.assertTrue(violations, "reader 消失卻放行了")
        self.assertIn("_is_widget_write", _joined(violations))

    def test_reader_downgraded_to_truthy_is_blocked(self):
        """reader 自己被改成裸 truthy ⇒ 走它的 key 一起靜默失守。必須紅。"""
        violations = _tree(model=MODEL_BARE_TRUTHY)
        self.assertIn("_is_widget_write", _joined(violations))

    def test_enforced_key_with_no_judge_site_is_blocked(self):
        """enforced 卻一個判定點都沒有 ⇒ 「找不到裸 truthy」是假綠。必須紅。"""
        violations = _tree(model=MODEL_NO_JUDGE_AT_ALL)
        self.assertTrue(violations, "沒有任何判定點卻放行了")
        self.assertIn("判定點", _joined(violations))

    def test_missing_defined_in_file_is_blocked(self):
        """sentinel 的定義檔搬家／改名 ⇒ 錨點消失。必須紅。"""
        registry = dict(_REGISTRY_OK)
        registry["ut_widget_internal_write"] = dict(
            _REGISTRY_OK["ut_widget_internal_write"],
            defined_in="src/models/nowhere.py")
        violations = _tree(registry=registry)
        self.assertTrue(violations, "defined_in 指到不存在的檔卻放行了")
        self.assertIn("nowhere.py", _joined(violations))

    def test_exempt_key_with_no_bool_site_is_blocked(self):
        """豁免條目掃不到布林 guard 讀取點 ⇒ 掃描器壞了，或這把已被修好。必須紅。

        🔴 這是 (iii) 唯一的**反向**對照。判準若只有「有沒有多出來的 key」，掃描器
        回空集合時它永遠是綠的——那正是「相等就算通過」最容易被探針故障滿足的形態。
        """
        violations = _tree(legacy="class Empty(object):\n    pass\n")
        self.assertTrue(violations, "豁免條目掃不到讀取點卻放行了")
        text = _joined(violations)
        self.assertIn("ut_legacy_skip_sync", text)
        self.assertIn("掃描器", text)

    def test_unparseable_production_file_is_blocked(self):
        """讀不到／parse 不了的生產檔算違規，不算無事（fail-loud）。"""
        violations = _tree(extra={"src/models/broken.py": "def (:\n"})
        self.assertTrue(violations, "parse 失敗卻當成無事")
        self.assertIn("broken.py", _joined(violations))

    def test_non_addon_root_has_no_input(self):
        """不是本 addon 的 root（沒有 manifest）＝本條沒有輸入，不得誤擋。"""
        tmp = tempfile.mkdtemp(prefix="ctx-key-registry-noroot-")
        try:
            with _patched_registry(_REGISTRY_OK):
                self.assertEqual(rl.check_internal_ctx_key_registry(tmp), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------- 四處登記（新增 check 的義務）

class TestGateIsWiredUp(unittest.TestCase):
    """gate 寫好了沒人跑，輸出與「跑了而且沒事」完全相同（#596 的形態）。"""

    def test_registered_in_rules_and_run_all(self):
        self.assertIn("internal-ctx-key-registry", rl.REPO_LINT_RULES)
        self.assertEqual(rl.REPO_LINT_RULES["internal-ctx-key-registry"],
                         ("check_internal_ctx_key_registry",))

    def test_hook_dispatch_watches_both_ends(self):
        """registry 與生產碼是同一條不變式的兩端，兩端都要能觸發 hook。"""
        self.assertTrue(rl._internal_ctx_key_watches("src/models/cloud_service.py"))
        self.assertTrue(rl._internal_ctx_key_watches(rl.REPO_LINT_SELF_REL))
        self.assertFalse(rl._internal_ctx_key_watches("src/tests/test_x.py"))
        self.assertFalse(rl._internal_ctx_key_watches("docs/index.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
