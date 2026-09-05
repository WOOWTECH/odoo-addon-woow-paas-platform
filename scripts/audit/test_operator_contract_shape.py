#!/usr/bin/env python3
"""#1052：``src/tests/operator_contract.py`` 的 ``status_response()`` 形狀 ↔ operator
``ReleaseStatusResponse`` pydantic 宣告的對帳。

為什麼住在 scripts/audit 而不是 src/tests：Odoo pod 的 hostPath 只掛 ``src/``，
``extra/paas-operator/`` 不在裡面（同 repo-lint 第 30／37 條的理由——沒有任何一個測試執行
環境同時看得到兩端）。scripts/audit 的 ``test_*.py`` 由 ``.gitea/workflows/repo-lint.yml``
逐支跑，每張 PR 都會到。

判準：
  (i)  ``status_response()`` 的頂層 key 集合 == ``ReleaseStatusResponse`` 的欄位集合；
  (ii) ``status_response()['release']`` 的 key 集合 ⊆ ``ReleaseInfo`` 的欄位集合
       （fixture 刻意只給子集，所以這層是子集不是等值）；
  (iii) ``RELEASE_STATUS_KEYS`` 常數與 (i) 同步（它是給測試檔 import 用的宣告）。
陽性對照：兩個 pydantic class 都必須找得到、欄位數都必須 >= 2——否則「對帳通過」與
「什麼都沒比」輸出相同。

契約（同 CI 那步的宣告）：無外部相依——不碰 ~/.claude、網路、Odoo、特定 cwd。
"""
import ast
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
_CONTRACT = os.path.join(_REPO_ROOT, "src", "tests", "operator_contract.py")
_SCHEMAS = os.path.join(_REPO_ROOT, "extra", "paas-operator", "src", "models",
                        "schemas.py")


def _load_contract():
    spec = importlib.util.spec_from_file_location("operator_contract", _CONTRACT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _basemodel_fields(source):
    """schemas.py 的 ``BaseModel`` 子類 → 欄位名集合（AnnAssign，排除 ClassVar）。"""
    out = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {getattr(b, "id", getattr(b, "attr", None)) for b in node.bases}
        if "BaseModel" not in bases:
            continue
        fields = set()
        for stmt in node.body:
            if not (isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)):
                continue
            ann = ast.unparse(stmt.annotation) if hasattr(ast, "unparse") else ""
            if ann.startswith("ClassVar"):
                continue
            fields.add(stmt.target.id)
        out[node.name] = fields
    return out


class TestOperatorContractShape(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(_SCHEMAS, encoding="utf-8") as f:
            cls.models = _basemodel_fields(f.read())
        cls.contract = _load_contract()

    def test_positive_control_schema_classes_present(self):
        for name in ("ReleaseStatusResponse", "ReleaseInfo"):
            self.assertIn(name, self.models,
                          "%s 找不到 class %s(BaseModel)——對帳沒有對象" % (_SCHEMAS, name))
            self.assertGreaterEqual(len(self.models[name]), 2, name)

    def test_top_level_keys_match_release_status_response(self):
        body = self.contract.status_response()
        self.assertEqual(
            set(body), self.models["ReleaseStatusResponse"],
            "status_response() 的頂層 key 與 operator ReleaseStatusResponse 不一致——"
            "operator 改了欄位就改 src/tests/operator_contract.py，不要再手抄第五份")

    def test_release_subset_of_release_info(self):
        body = self.contract.status_response()
        self.assertTrue(
            set(body["release"]) <= self.models["ReleaseInfo"],
            "release 子物件出現 ReleaseInfo 沒有的 key：%s"
            % sorted(set(body["release"]) - self.models["ReleaseInfo"]))

    def test_declared_constant_matches_factory(self):
        self.assertEqual(set(self.contract.RELEASE_STATUS_KEYS),
                         set(self.contract.status_response()))

    def test_overrides_and_pods_are_honoured(self):
        """factory 的兩個把手真的有作用（不然遷移過去的 fixture 會靜默改語意）。"""
        pod = {"name": "p", "phase": "Running", "ready": "1/1"}
        body = self.contract.status_response("failed", pods=[pod],
                                             vm_included=True, vmis=[])
        self.assertEqual(body["release"]["status"], "failed")
        self.assertEqual(body["pods"], [pod])
        self.assertTrue(body["vm_included"])
        self.assertEqual(body["vmis"], [])


if __name__ == "__main__":
    unittest.main()
