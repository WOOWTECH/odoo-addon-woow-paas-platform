#!/usr/bin/env python3
"""`access-payload-shape-matches-operator-schema`（repo-lint 第 37 條）的自檢。

守的是**這條規則自己**的鑑別力，不是被守的那棵樹乾不乾淨。分兩半：

* **敏感度**：builder 少一個 required key、多一個 key、schema class 不見、builder 少於
  三個、return 形狀解不出來（下標賦值／`.update()`／`|=`／綁兩次）——都要紅，而且訊息
  要說得出是哪一種；
* **特異性**：三條腿都送滿 schema 欄位的合成樹不得誤報；`return name` 綁到 dict
  literal 的形狀（documented shape ii）也不得誤報；省略**有預設值**的欄位不得誤報
  （#1052 review：原版把 Optional 當 required，schema 加一個 Optional 就三腿全紅）；
  巢狀 helper 裡撞名的區域變數不得污染外層判定。

最後一組對**真實 repo** 跑：今天三條腿都送滿 10 個欄位（#1052 覆核實測），這條 gate
在真樹上必須是綠的——這是「check 沒有恆紅」的陽性對照，也是 CI 每張 PR 都會跑到的那格。

契約（同 CI 那步的宣告）：無外部相依——不碰 ~/.claude、網路、Odoo、特定 cwd。
"""
import importlib.util
import os
import shutil
import tempfile
import unittest

# repo-lint.py 的檔名帶連字號，import 不進來 ⇒ 走 spec_from_file_location
# （與 test_repo_lint_access_gate_no_ledger_read.py 同一個慣例）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_GATE_PATH = os.path.join(_HERE, "repo-lint.py")
_spec = importlib.util.spec_from_file_location("repo_lint", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))

_FIELDS = ("enabled", "canonical_name", "app_name", "primary_hostname",
           "protected_hostnames", "session_duration", "login_idp_types",
           "policies", "existing_app_id", "existing_policy_ids")


def _schema_source(fields=_FIELDS, class_name="AccessSyncRequest", optional=()):
    """`fields` 全 required（`Field(...)`）；`optional` 另加成 `Optional[str] = Field(None)`。"""
    lines = ["from typing import Optional", "from pydantic import BaseModel, Field", "",
             "class Other(BaseModel):", "    x: int", "",
             "class %s(BaseModel):" % class_name]
    lines += ["    %s: str = Field(...)" % f for f in fields]
    lines += ["    %s: Optional[str] = Field(None)" % f for f in optional]
    return "\n".join(lines) + "\n"


def _builder_source(keys=_FIELDS, shape="literal"):
    body = ",\n".join("            '%s': 1" % k for k in keys)
    if shape == "literal":
        ret = "        return {\n%s,\n        }\n" % body
    elif shape == "bound-name":
        ret = ("        payload = {\n%s,\n        }\n"
               "        return payload\n" % body)
    elif shape == "mutated":
        ret = ("        payload = {\n%s,\n        }\n"
               "        payload['late'] = 1\n"
               "        return payload\n" % body)
    elif shape == "updated":
        ret = ("        payload = {\n%s,\n        }\n"
               "        payload.update({'late': 1})\n"
               "        return payload\n" % body)
    elif shape == "augassign":
        ret = ("        payload = {\n%s,\n        }\n"
               "        payload |= {'late': 1}\n"
               "        return payload\n" % body)
    elif shape == "double-bound":
        ret = ("        payload = {}\n"
               "        payload = {\n%s,\n        }\n"
               "        return payload\n" % body)
    elif shape == "nested-shadow":
        ret = ("        payload = {\n%s,\n        }\n"
               "        def _helper():\n"
               "            payload = {}\n"
               "            payload['x'] = 1\n"
               "            return payload\n"
               "        return payload\n" % body)
    else:
        raise ValueError(shape)
    return ("class M:\n"
            "    def _build_access_payload(self):\n"
            "        hostname = 'h'\n" + ret)


class _Case(unittest.TestCase):
    """合成一棵最小 repo 樹（manifest ＋ src/models ＋ operator schemas.py）。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="repo-lint-aps-")
        os.makedirs(os.path.join(self.root, "src", "models"))
        os.makedirs(os.path.join(self.root, "extra", "paas-operator",
                                 "src", "models"))
        with open(os.path.join(self.root, "src", "__manifest__.py"), "w",
                  encoding="utf-8") as f:
            f.write("{'name': 'synthetic'}\n")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write_schema(self, source):
        with open(os.path.join(self.root, rl.ACCESS_PAYLOAD_SCHEMAS_REL), "w",
                  encoding="utf-8") as f:
            f.write(source)

    def write_builders(self, sources):
        for i, source in enumerate(sources):
            with open(os.path.join(self.root, "src", "models",
                                   "leg%d.py" % i), "w", encoding="utf-8") as f:
                f.write(source)

    def run_check(self):
        return rl.check_access_payload_shape_matches_operator_schema(self.root)


class TestSpecificity(_Case):
    """三條腿都送滿欄位 ⇒ 零違規；兩種可解的 return 形狀都要放行。"""

    def test_three_full_literal_builders_are_clean(self):
        self.write_schema(_schema_source())
        self.write_builders([_builder_source()] * 3)
        self.assertEqual(self.run_check(), [])

    def test_optional_field_may_be_omitted(self):
        """schema 多一個 `Optional[str] = Field(None)`、三腿都不送 ⇒ 不是違規（pydantic 合法）。"""
        self.write_schema(_schema_source(optional=("later_added",)))
        self.write_builders([_builder_source(), _builder_source(), _builder_source()])
        self.assertEqual(self.run_check(), [])

    def test_nested_helper_shadowing_name_does_not_poison_outer(self):
        """巢狀 helper 裡同名區域變數的下標賦值不算外層 payload 的改動。"""
        self.write_schema(_schema_source())
        self.write_builders([_builder_source(shape="nested-shadow"),
                             _builder_source(), _builder_source()])
        self.assertEqual(self.run_check(), [])

    def test_bound_name_return_is_recognised(self):
        self.write_schema(_schema_source())
        self.write_builders([_builder_source(shape="bound-name")] * 3)
        self.assertEqual(self.run_check(), [])


class TestSensitivity(_Case):
    """五種失效各自要紅，且訊息分得出是哪一種。"""

    def test_missing_key_names_the_key(self):
        self.write_schema(_schema_source())
        short = tuple(f for f in _FIELDS if f != "session_duration")
        self.write_builders([_builder_source(), _builder_source(short),
                             _builder_source()])
        v = self.run_check()
        self.assertEqual(len(v), 1, v)
        self.assertIn("leg1.py", v[0])
        self.assertIn("缺 required 欄位 ['session_duration']", v[0])
        self.assertNotIn("多了", v[0])

    def test_extra_key_names_the_key(self):
        self.write_schema(_schema_source())
        self.write_builders([_builder_source(_FIELDS + ("bogus",)),
                             _builder_source(), _builder_source()])
        v = self.run_check()
        self.assertEqual(len(v), 1, v)
        self.assertIn("多了 schema 沒有的 ['bogus']", v[0])
        self.assertNotIn("缺 required", v[0])

    def test_schema_class_absent_is_a_violation_not_clean(self):
        self.write_schema(_schema_source(class_name="SomethingElse"))
        self.write_builders([_builder_source()] * 3)
        v = self.run_check()
        self.assertEqual(len(v), 1, v)
        self.assertIn("AccessSyncRequest", v[0])
        self.assertIn("找不到", v[0])

    def test_fewer_than_three_builders_is_a_violation(self):
        """三條腿是拍板留分的；只掃到兩個＝有人改了名或掃描範圍壞了。"""
        self.write_schema(_schema_source())
        self.write_builders([_builder_source()] * 2)
        v = self.run_check()
        self.assertEqual(len(v), 1, v)
        self.assertIn("少於", v[0])

    def test_update_call_is_unrecognised_shape(self):
        """`payload.update({...})`：#1052 review 抓到原版當乾淨放行；必須紅。"""
        self.write_schema(_schema_source())
        self.write_builders([_builder_source(shape="updated"),
                             _builder_source(), _builder_source()])
        v = self.run_check()
        self.assertEqual(len(v), 1, v)
        self.assertIn("解不出來", v[0])

    def test_augassign_is_unrecognised_shape(self):
        """`payload |= {...}`：同上。"""
        self.write_schema(_schema_source())
        self.write_builders([_builder_source(shape="augassign"),
                             _builder_source(), _builder_source()])
        v = self.run_check()
        self.assertEqual(len(v), 1, v)
        self.assertIn("解不出來", v[0])

    def test_double_binding_is_unrecognised_shape(self):
        """綁兩次：AST 走訪順序不是原始碼順序，哪份是最後的看不出來 ⇒ 紅。"""
        self.write_schema(_schema_source())
        self.write_builders([_builder_source(shape="double-bound"),
                             _builder_source(), _builder_source()])
        v = self.run_check()
        self.assertEqual(len(v), 1, v)
        self.assertIn("解不出來", v[0])

    def test_mutated_dict_is_unrecognised_shape(self):
        """`payload['late'] = …` 之後才 return：靜態看不到 key，必須紅而不是放行。"""
        self.write_schema(_schema_source())
        self.write_builders([_builder_source(shape="mutated"),
                             _builder_source(), _builder_source()])
        v = self.run_check()
        self.assertEqual(len(v), 1, v)
        self.assertIn("解不出來", v[0])


class TestRealRepo(unittest.TestCase):
    """陽性對照：真樹上三條腿今天送滿 10 個欄位 ⇒ 本 gate 必須綠。"""

    def test_real_tree_is_green(self):
        self.assertTrue(
            os.path.exists(os.path.join(_REPO_ROOT, "src", "__manifest__.py")),
            "repo root 推導失敗：%s" % _REPO_ROOT)
        self.assertEqual(
            rl.check_access_payload_shape_matches_operator_schema(_REPO_ROOT),
            [])


if __name__ == "__main__":
    unittest.main()
