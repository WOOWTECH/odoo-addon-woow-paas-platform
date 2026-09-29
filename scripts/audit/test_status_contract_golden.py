#!/usr/bin/env python3
"""#1263：repo-lint ``status-contract-golden`` 的陽性／陰性對照。

``check_status_contract_golden`` 只讀兩個檔（operator ``schemas.py`` 與
``src/tests/fixtures/status_contract_golden.json``），所以每支 mutation 只在暫存目錄
放這兩個檔的副本，不碰 working tree、不複製整個 repo。

契約（同 repo-lint CI 那步的宣告）：無外部相依——不碰 ~/.claude、網路、Odoo、特定 cwd。
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
_SCHEMA = "extra/paas-operator/src/models/schemas.py"
_GOLDEN = "src/tests/fixtures/status_contract_golden.json"


def _load_lint():
    spec = importlib.util.spec_from_file_location(
        "repo_lint_1263", os.path.join(_HERE, "repo-lint.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestStatusContractGolden(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.lint = _load_lint()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="status-golden-")
        self.addCleanup(shutil.rmtree, self.tmp)
        for rel in (_SCHEMA, _GOLDEN):
            os.makedirs(os.path.dirname(os.path.join(self.tmp, rel)), exist_ok=True)
            shutil.copy(os.path.join(_ROOT, rel), os.path.join(self.tmp, rel))

    def _edit(self, rel, old, new):
        path = os.path.join(self.tmp, rel)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count(old), 1, "mutation 前提不成立：%s / %s" % (rel, old))
        with open(path, "w", encoding="utf-8") as f:
            f.write(src.replace(old, new))

    def _violations(self):
        return self.lint.check_status_contract_golden(self.tmp)

    def test_real_tree_is_clean(self):
        self.assertEqual(self.lint.check_status_contract_golden(_ROOT), [])

    def test_pod_info_field_added_without_regenerating_golden_is_flagged(self):
        self._edit(_SCHEMA, "    task: Optional[str] = None",
                   "    task: Optional[str] = None\n    node_name: Optional[str] = None")
        violations = self._violations()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("generate_status_contract_golden.py", violations[0])

    def test_status_response_default_change_is_flagged(self):
        """預設值是 wire 行為的一部分：舊平台缺旗標就 fail-closed。"""
        self._edit(_SCHEMA, "    pod_identity_included: bool = Field(\n        default=False,",
                   "    pod_identity_included: bool = Field(\n        default=True,")
        self.assertEqual(len(self._violations()), 1)

    def test_description_only_change_is_not_flagged(self):
        """只改說明文字不該紅——噪音大的 gate 會被關掉。"""
        self._edit(_SCHEMA, '"True when this operator populates PodInfo.component/task/owner_kind. "',
                   '"Reworded description. "')
        self.assertEqual(self._violations(), [])

    def test_golden_without_fingerprint_is_flagged(self):
        path = os.path.join(self.tmp, _GOLDEN)
        with open(path, encoding="utf-8") as f:
            golden = json.load(f)
        del golden["_contract_fingerprint"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(golden, f)
        self.assertEqual(len(self._violations()), 1)

    def test_missing_inputs_fail_loud(self):
        for rel in (_SCHEMA, _GOLDEN):
            with self.subTest(rel=rel):
                self.setUp()
                os.remove(os.path.join(self.tmp, rel))
                self.assertEqual(len(self._violations()), 1)

    def test_route_fingerprint_is_unchanged_by_the_shared_helper(self):
        """route 與 status 共用指紋演算法；抽共用 helper 不得改動 route 既有的指紋。"""
        with open(os.path.join(_ROOT, "src/tests/fixtures/route_contract_golden.json"),
                  encoding="utf-8") as f:
            recorded = json.load(f)["_contract_fingerprint"]
        fp, err = self.lint.route_contract_fingerprint(_ROOT)
        self.assertIsNone(err)
        self.assertEqual(fp, recorded)


if __name__ == "__main__":
    unittest.main()
