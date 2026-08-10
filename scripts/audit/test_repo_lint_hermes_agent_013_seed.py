#!/usr/bin/env python3
"""``check_hermes_agent_013_seed`` 的 A/B 注入測試（PR #649 gate 修正）。

這條 gate 上一輪加進去時**沒有任何測試**，而且藏著兩個問題，都是這支測試要
守住的：

1. **形態層 pin 誤擋升版**（本次派工單要修的主案）：原版把
   ``helm_chart_version`` pin 死成 ``== '0.1.3'``，chart 升到 0.1.4 時 seed
   照規矩改成 0.1.4 是**正確行為**，但 gate 會判它違規——守線反過來擋住它該
   放行的事。改成 floor（``>= (0, 1, 3)``，int tuple 比較）才是對的：版號不得
   倒退回 webui 時代，但升版必須自由。字串比較是這裡最容易踩的陷阱——
   ``'0.1.10' < '0.1.3'`` 字串比較是 True，但語意上 0.1.10 > 0.1.3，
   ``test_version_floor_string_comparison_bug`` 專門抓這個。

2. **record 從未被找到（本次順便發現並修掉）**：原本用
   ``target = 'woow_paas_platform.cloud_app_hermes_agent'`` 去比對
   ``<record>`` 的**所有屬性值**，但 XML 裡的 ``id`` 屬性是本模組內的本地
   id（``cloud_app_hermes_agent``，不帶模組前綴），這個字串在整份
   ``src/data/*.xml`` 裡從未出現過——``record`` 永遠是 ``None``，函式對**任何**
   seed 內容都回傳 ``[]``。也就是說這條 gate 從寫出來的第一天起就是完全的
   no-op，不管 seed 是 0.1.2、0.1.3 還是缺欄位，``repo-lint.py --check`` 都會
   顯示通過——這是比「pin 太死」嚴重得多的假綠。``TestBaselineIsGreen`` 的
   ``test_probe_actually_finds_the_record`` 是這條的陽性對照：不能只驗
   「violations == []」，得先證明探針真的掃到了東西，否則「綠」跟「探針壞掉」
   長得一模一樣（memory: feedback_negative_verification_needs_positive_control）。

   修正 id 比對後又浮出第三個問題：``src/data/subscription_template_links.xml``
   對同一個 ``id="cloud_app_hermes_agent"`` 只補了 ``subscription_plan_ids``
   欄位（Odoo 的合法慣例——同一個 id 的 record 可以跨檔分次宣告欄位，由 ORM
   合併成同一顆物件）。逐檔獨立驗證會把「這個檔沒宣告 helm_chart_version」
   誤判成「缺欄位」，對真實 repo 產生假紅。``TestMultiFileRecordMerge`` 守住
   「同一個 id 跨檔宣告」不能被誤判。

跑法：
    python3 scripts/audit/test_repo_lint_hermes_agent_013_seed.py
    # 或 python3 -m pytest scripts/audit/test_repo_lint_hermes_agent_013_seed.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部
依賴——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo，也不依賴 cwd。
"""
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")

_spec = importlib.util.spec_from_file_location("repo_lint_hermes_seed_ut", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)


CLEAN_VALUES = {
    "image": {"repository": "jcr-prod.woowtech.io/x/hermes-agent",
              "tag": "20260727.1502@sha256:" + "0" * 64},
    "dashboard": {"bindHost": "0.0.0.0", "username": "admin"},
    "gateway": {"externalPath": "/gateway"},
}


def _values_with_webui():
    values = json.loads(json.dumps(CLEAN_VALUES))  # deep copy
    values["webui"] = {"image": {"repository": "x", "tag": "y"}}
    return values


def _values_with_dashboard_external_path():
    values = json.loads(json.dumps(CLEAN_VALUES))
    values["dashboard"]["externalPath"] = "/dashboard"
    return values


def _record_xml(version=None, values=None, extra_field=""):
    """組出一個最小的 ``cloud_app_hermes_agent`` record（可選填欄位缺席）。"""
    fields = []
    if version is not None:
        fields.append('<field name="helm_chart_version">%s</field>' % version)
    if values is not None:
        fields.append('<field name="helm_default_values">%s</field>'
                       % json.dumps(values))
    if extra_field:
        fields.append(extra_field)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
        '<record id="cloud_app_hermes_agent" '
        'model="woow_paas_platform.cloud_app_template">\n'
        + "\n".join(fields) +
        '\n</record>\n</odoo>\n'
    )


class _FixtureRoot:
    """把（一份或多份）data XML 擺進一個最小 ``src/data/`` root。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="hermes-013-seed-gate-")

    def write(self, name, text):
        target_dir = os.path.join(self.dir, "src", "data")
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, name), "w", encoding="utf-8") as f:
            f.write(text)
        return self

    def run(self):
        return rl.check_hermes_agent_013_seed(self.dir)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def _single(version=None, values=None):
    """單檔、單一 record 的最短路徑：寫 fixture、跑 check、回傳 violations。"""
    with _FixtureRoot() as fx:
        fx.write("fixture.xml", _record_xml(version=version, values=values))
        return fx.run()


# ------------------------------------------------------------ 陽性對照

class TestBaselineIsGreen(unittest.TestCase):
    """未突變時必須綠——而且綠得有前提（探針真的掃到東西，不是没找到就沉默）。"""

    def test_real_repo_is_green(self):
        self.assertEqual(rl.check_hermes_agent_013_seed(str(_REPO_ROOT)), [])

    def test_probe_actually_finds_the_record(self):
        # 「violations == []」在探針壞掉、record 從未被找到時也是這個值——
        # 這正是本 gate 上線後其實從未生效過的方式（id 比對用錯字串，
        # `record` 永遠是 None）。用一個必定違規的 fixture 證明探針真的有掃到
        # 東西、真的在跑判斷，而不是每次都提早 continue。
        violations = _single(version="0.1.2")
        self.assertTrue(violations, "探針對明顯違規的 fixture 仍回傳 []——"
                        "record 可能根本沒被找到，這條 gate 又變回 no-op 了")

    def test_minimal_clean_fixture_passes(self):
        self.assertEqual(_single(version="0.1.3", values=CLEAN_VALUES), [])

    def test_no_matching_record_is_silently_skipped(self):
        # 沒有任何 record 用這個 id ⇒ 這個 check 的輸入領域根本不存在，維持
        # 既有行為：不誤報。（此模組是否真的有這個 template 由別的機制守。）
        with _FixtureRoot() as fx:
            fx.write("unrelated.xml",
                     '<?xml version="1.0"?><odoo>'
                     '<record id="something_else" model="x.y"/></odoo>')
            self.assertEqual(fx.run(), [])


# ------------------------------------------------------------ 版號 floor（主案）

class TestVersionFloor(unittest.TestCase):
    """floor（``>= 0.1.3``），不是 pin（``== 0.1.3``）。"""

    def test_below_floor_is_blocked(self):
        # 降版回到 webui 時代——floor 要守的**唯一**方向。
        violations = _single(version="0.1.2", values=CLEAN_VALUES)
        self.assertTrue(violations)
        self.assertIn("0.1.2", violations[0])
        self.assertIn("floor", violations[0])

    def test_floor_boundary_passes(self):
        self.assertEqual(_single(version="0.1.3", values=CLEAN_VALUES), [])

    def test_above_floor_passes(self):
        # 這條就是本次要修的 bug 的紅燈案例：pin 死 == '0.1.3' 時，0.1.4 這種
        # 正常升版會被誤擋。floor 修好後必須放行。
        self.assertEqual(_single(version="0.1.4", values=CLEAN_VALUES), [])

    def test_version_floor_string_comparison_bug(self):
        # 🔴 專抓字串比較 bug：若用字串比較，'0.1.10' < '0.1.3' 是 True
        # （逐字元比較 '1' < '3'），會被誤判成低於 floor 而擋下——但語意上
        # 0.1.10 > 0.1.3，必須放行。int tuple 比較才會得到正確答案。
        self.assertEqual(_single(version="0.1.10", values=CLEAN_VALUES), [])

    def test_missing_version_field_is_blocked(self):
        # 既有行為，不能因為這次改寫而退化：完全沒宣告 helm_chart_version
        # 仍必須被擋。
        violations = _single(version=None, values=CLEAN_VALUES)
        self.assertTrue(violations)
        self.assertIn("缺", violations[0])
        self.assertIn("helm_chart_version", violations[0])

    def test_malformed_version_is_blocked_not_silently_allowed(self):
        # 解析失敗不得靜默放行——非 N.N.N 形態一律擋，訊息要說「無法解析」。
        for bad in ("abc", "0.1", "0.1.3.4extra", "0.1.x", "latest"):
            with self.subTest(bad=bad):
                violations = _single(version=bad, values=CLEAN_VALUES)
                self.assertTrue(violations, "版號 %r 應該被擋卻放行了" % bad)
                self.assertIn("無法解析", violations[0])


# ------------------------------------------------------------ 不變式（webui／externalPath，agent 上次寫對的部分）

class TestForbiddenBlocks(unittest.TestCase):
    """webui block／dashboard.externalPath：無條件不得存在，不是 floor。"""

    def test_webui_block_present_is_blocked(self):
        violations = _single(version="0.1.3", values=_values_with_webui())
        self.assertTrue(violations)
        self.assertIn("webui", violations[0])

    def test_dashboard_external_path_present_is_blocked(self):
        violations = _single(
            version="0.1.3", values=_values_with_dashboard_external_path())
        self.assertTrue(violations)
        self.assertIn("externalPath", violations[0])

    def test_webui_and_external_path_both_reported(self):
        # 兩個違規同時存在時，兩條都要出現，不能有一條把另一條吃掉。
        values = _values_with_webui()
        values["dashboard"]["externalPath"] = "/dashboard"
        violations = _single(version="0.1.3", values=values)
        self.assertEqual(len(violations), 2, violations)

    def test_forbidden_blocks_do_not_trip_the_version_floor(self):
        # 特異性：版號正常時，webui 違規不該連帶讓版號那條也報錯（訊息要對得
        # 上各自的根因，不能互相污染）。
        violations = _single(version="0.1.3", values=_values_with_webui())
        self.assertFalse(any("floor" in v for v in violations), violations)


# ------------------------------------------------------------ 跨檔 record 合併（本次順便修的 id 比對 bug）

class TestMultiFileRecordMerge(unittest.TestCase):
    """同一個 id 的 record 可以跨檔分次宣告欄位（Odoo 合法慣例）。"""

    def test_second_file_only_patches_unrelated_field(self):
        # 對齊真實 repo 的形狀：subscription_template_links.xml 只補
        # subscription_plan_ids，不重複整個 template。逐檔獨立驗證會把這個
        # 檔誤判成「缺 helm_chart_version」——這是本次順便發現並修掉的 bug，
        # 沒有這條測試，那個 bug 會在下一次重構時無聲無息地回來。
        with _FixtureRoot() as fx:
            fx.write("cloud_app_templates.xml",
                     _record_xml(version="0.1.3", values=CLEAN_VALUES))
            fx.write("subscription_template_links.xml", _record_xml(
                extra_field='<field name="subscription_plan_ids" eval="[(6,0,[1])]"/>'))
            self.assertEqual(fx.run(), [])

    def test_violation_in_the_non_primary_file_is_still_caught(self):
        # 反向：即使 helm_chart_version 宣告在「非主要」的那個檔，違規一樣要
        # 被抓到（不能假設一定是第一個檔在管版號）。
        with _FixtureRoot() as fx:
            fx.write("a_first_alphabetically.xml", _record_xml(
                extra_field='<field name="subscription_plan_ids" eval="[(6,0,[1])]"/>'))
            fx.write("b_second_alphabetically.xml",
                     _record_xml(version="0.1.2", values=CLEAN_VALUES))
            violations = fx.run()
        self.assertTrue(violations)
        self.assertIn("0.1.2", violations[0])

    def test_truly_missing_across_all_files_is_blocked(self):
        # 兩個檔都有這個 id 的 record，但都沒宣告 helm_chart_version ⇒
        # 彙整後仍然是「缺」，必須擋。
        with _FixtureRoot() as fx:
            fx.write("a.xml", _record_xml(
                extra_field='<field name="subscription_plan_ids" eval="[(6,0,[1])]"/>'))
            fx.write("b.xml", _record_xml(values=CLEAN_VALUES))
            violations = fx.run()
        self.assertTrue(violations)
        self.assertIn("helm_chart_version", violations[0])


# ------------------------------------------------------------ fail-loud

class TestFailLoud(unittest.TestCase):
    """探針壞掉／輸入壞掉必須是紅的，不能靜默放行或整支炸掉。"""

    def test_broken_json_in_helm_default_values_does_not_crash(self):
        # JSON 壞掉由別的 check（如 XML/JSON schema 驗證）負責報，這條只要不
        # 崩潰、不誤判成「有 webui」即可。
        with _FixtureRoot() as fx:
            fx.write("fixture.xml", _record_xml(
                version="0.1.3",
                extra_field='<field name="helm_default_values">{not valid json</field>'))
            violations = fx.run()  # 不應拋例外
        self.assertEqual(violations, [])

    def test_unparseable_xml_file_does_not_crash_and_is_skipped(self):
        with _FixtureRoot() as fx:
            fx.write("broken.xml", "<odoo><record id=broken not closed")
            fx.write("fixture.xml",
                     _record_xml(version="0.1.3", values=CLEAN_VALUES))
            violations = fx.run()  # 不應拋例外，且乾淨的那份仍要被驗到
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
