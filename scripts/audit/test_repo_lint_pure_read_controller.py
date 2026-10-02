#!/usr/bin/env python3
"""`poll-release-facts-single-source` 判準 6／6b（#1159 Q1／#1257）的自檢（#1304）。

判準 6：`PaasController._list_services`／`_get_service` 本體內不得呼叫
`_PURE_READ_FORBIDDEN_CALLS`（list/get 純讀）。
判準 6b：整個 controller 檔不得呼叫 `_CONTROLLER_FORBIDDEN_CALLS`（init 入口、poll、commit）。

守的是**這兩條判準自己**的鑑別力，不是被守的那棵樹乾不乾淨。分三半：

* **敏感度**：每一個禁用名稱放進 list/get 本體都要紅、訊息要點名函式與名稱；
  init／poll／commit 放進 controller 任何函式（含巢狀函式）都要紅；端點找不到、
  controller parse 不了都要紅（fail-loud，不得靜默恆綠）。
* **特異性**：list/get 以外的 controller 端點照常寫入／取 client／raw SQL（例如
  `ha_paas.py` 的 advisory lock 形狀）、`rollback`／`savepoint` 都不得誤報。
* **真實 repo**：develop 上這條 gate 必須是綠的——「check 沒有恆紅」的陽性對照。

🔴 禁用清單用本檔的**字面下限**（`_PURE_READ_FLOOR`／`_CONTROLLER_FLOOR`）驅動，
不是迭代 repo-lint 的 tuple：迭代被測物自己的清單時，從清單拿掉一項＝測試少跑一輪＝
照樣綠（宣告式清單的假綠洞，同 `_registry_diff` docstring）。清單要縮減時，改本檔的
下限是刻意的摩擦——那個 diff 看得到。

契約（同 CI「scripts/ 自我測試」那步的宣告）：無外部相依——不碰 ~/.claude、網路、
Odoo、特定 cwd。
"""
import importlib.util
import os
import shutil
import tempfile
import textwrap
import unittest

# repo-lint.py 的檔名帶連字號，import 不進來 ⇒ 走 spec_from_file_location
# （與 test_repo_lint_access_payload_shape.py 同一個慣例）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_GATE_PATH = os.path.join(_HERE, "repo-lint.py")
_spec = importlib.util.spec_from_file_location("repo_lint", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))

#: 判準 6 至少要擋的名稱（#1159 Q1 ＋ #1304 的 execute）。
_PURE_READ_FLOOR = (
    "get_paas_operator_client", "get_status", "_poll_and_update_status",
    "_update_service_status", "_refresh_public_url_dns_flag",
    "_public_url_dns_state", "write", "create", "unlink", "sudo", "execute",
)
#: 判準 6b 至少要擋的名稱（#1257 ＋ #1304 的 commit）。
_CONTROLLER_FLOOR = ("_run_post_deploy_init", "_poll_and_update_status", "commit")

# 判準 1～5 的最小合規 model：只為了讓本檔的違規數只反映判準 6／6b。
_MODEL_SOURCE = textwrap.dedent("""\
    class CloudService:
        def _release_facts(self, status):
            return {}

        def _poll_and_update_status(self, client):
            release_facts = self._release_facts(None)
            self.write({'state': 'running', **release_facts})

        def _cron_reconcile_release_facts(self):
            release_facts = self._release_facts(None)
            self.write({**release_facts})
    """)

_CLEAN_LIST_BODY = "return {'services': [s.state for s in workspace.service_ids]}"
_CLEAN_GET_BODY = "return {'state': service.state}"

# list/get 以外的端點：寫入、取 client、raw SQL、rollback、savepoint 都是合法的。
_OTHER_ENDPOINTS = textwrap.dedent("""\
        def _create_service(self, workspace):
            from ..services.paas_operator import get_paas_operator_client
            client = get_paas_operator_client(request.env)
            request.env.cr.execute('SELECT pg_advisory_xact_lock(%s, %s)', (1, 2))
            with request.env.cr.savepoint():
                svc = request.env['cloud.service'].sudo().create({'name': 'x'})
            svc.write({'state': 'deploying'})
            request.env.cr.rollback()
            return svc
    """)


def _controller_source(list_body=_CLEAN_LIST_BODY, get_body=_CLEAN_GET_BODY,
                       extra=_OTHER_ENDPOINTS, list_name="_list_services",
                       get_name="_get_service"):
    def _indent(body):
        return textwrap.indent(textwrap.dedent(body).strip("\n"), " " * 8)

    return ("class PaasController:\n"
            "    def %s(self, workspace):\n%s\n\n"
            "    def %s(self, service):\n%s\n\n"
            "%s"
            % (list_name, _indent(list_body), get_name, _indent(get_body),
               textwrap.indent(extra, " " * 4)))


class _Case(unittest.TestCase):
    """在 tmp root 造出判準 1～5 合規的 model＋可替換的 controller。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="repo-lint-pure-read-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._write(rl.POLL_MODEL_FILE, _MODEL_SOURCE)
        self.write_controller(_controller_source())

    def _write(self, rel, source):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)

    def write_controller(self, source):
        self._write(rl.POLL_CONTROLLER_FILE, source)

    def run_check(self):
        return rl.check_poll_release_facts_single_source(self.root)


class TestFloorFoldedIn(unittest.TestCase):
    """清單只能長、不能被靜默縮減：縮減要改本檔的下限。"""

    def test_pure_read_list_keeps_floor(self):
        missing = set(_PURE_READ_FLOOR) - set(rl._PURE_READ_FORBIDDEN_CALLS)
        self.assertFalse(missing, "判準 6 的禁用清單少了：%s" % sorted(missing))

    def test_controller_list_keeps_floor(self):
        missing = set(_CONTROLLER_FLOOR) - set(rl._CONTROLLER_FORBIDDEN_CALLS)
        self.assertFalse(missing, "判準 6b 的禁用清單少了：%s" % sorted(missing))

    def test_pure_read_endpoints_still_targeted(self):
        self.assertEqual(
            set(rl._PURE_READ_ENDPOINTS),
            {(rl.POLL_CONTROLLER_FILE, "_list_services"),
             (rl.POLL_CONTROLLER_FILE, "_get_service")})


class TestSpecificity(_Case):
    """合規樹不得誤報——否則下面的「會紅」沒有意義。"""

    def test_clean_tree_has_no_violation(self):
        self.assertEqual(self.run_check(), [])

    def test_other_endpoints_may_write_and_use_raw_sql(self):
        # _OTHER_ENDPOINTS 已含 write／create／sudo／execute／rollback／savepoint／
        # get_paas_operator_client；這裡再明確驗一次它真的在樹裡（避免空跑）。
        with open(os.path.join(self.root, rl.POLL_CONTROLLER_FILE),
                  encoding="utf-8") as f:
            source = f.read()
        for name in ("execute", "write", "rollback", "savepoint",
                     "get_paas_operator_client"):
            self.assertIn(name, source)
        self.assertEqual(self.run_check(), [])


class TestCriterion6Sensitivity(_Case):
    """判準 6：每一個禁用名稱放進 list/get 本體都要紅。"""

    def _assert_flags(self, violations, func_name, name):
        hits = [v for v in violations
                if "::%s " % func_name in v and "`%s`" % name in v
                and "純讀" in v]
        self.assertTrue(hits, "判準 6 沒擋下 %s 內的 `%s`：%r"
                        % (func_name, name, violations))

    def test_each_forbidden_attr_call_in_list_is_flagged(self):
        for name in _PURE_READ_FLOOR:
            with self.subTest(name=name):
                self.write_controller(_controller_source(
                    list_body="workspace.%s()\n%s" % (name, _CLEAN_LIST_BODY)))
                self._assert_flags(self.run_check(), "_list_services", name)

    def test_each_forbidden_attr_call_in_get_is_flagged(self):
        for name in _PURE_READ_FLOOR:
            with self.subTest(name=name):
                self.write_controller(_controller_source(
                    get_body="service.%s()\n%s" % (name, _CLEAN_GET_BODY)))
                self._assert_flags(self.run_check(), "_get_service", name)

    def test_bare_name_call_is_flagged(self):
        # `from ..services.paas_operator import get_paas_operator_client` 後直接呼叫
        # ＝ast.Name 分支；只認 Attribute 的實作會漏掉這個最常見的形狀。
        self.write_controller(_controller_source(
            list_body="client = get_paas_operator_client(request.env)\n"
                      + _CLEAN_LIST_BODY))
        self._assert_flags(self.run_check(), "_list_services",
                           "get_paas_operator_client")

    def test_raw_sql_execute_in_get_is_flagged(self):
        self.write_controller(_controller_source(
            get_body="request.env.cr.execute(\"UPDATE cloud_service SET state='x'\")\n"
                     + _CLEAN_GET_BODY))
        self._assert_flags(self.run_check(), "_get_service", "execute")

    def test_missing_endpoint_is_a_violation_not_clean(self):
        self.write_controller(_controller_source(list_name="_list_services_v2"))
        violations = self.run_check()
        self.assertTrue(any("找不到 list/get 端點 `_list_services`" in v
                            for v in violations), violations)


class TestCriterion6bSensitivity(_Case):
    """判準 6b：整個 controller 檔不得呼叫 init／poll／commit。"""

    def _assert_flags(self, violations, name):
        hits = [v for v in violations
                if v.startswith(rl.POLL_CONTROLLER_FILE + " 第 ")
                and "`%s`" % name in v]
        self.assertTrue(hits, "判準 6b 沒擋下 `%s`：%r" % (name, violations))

    def test_each_forbidden_call_in_other_endpoint_is_flagged(self):
        for name in _CONTROLLER_FLOOR:
            with self.subTest(name=name):
                extra = _OTHER_ENDPOINTS + textwrap.dedent("""\

                        def _deploy(self, service):
                            service.%s()
                            return True
                    """ % name)
                self.write_controller(_controller_source(extra=extra))
                self._assert_flags(self.run_check(), name)

    def test_nested_function_is_flagged(self):
        extra = _OTHER_ENDPOINTS + textwrap.dedent("""\

                def _deploy(self, service):
                    def _later():
                        request.env.cr.commit()
                    return _later
            """)
        self.write_controller(_controller_source(extra=extra))
        self._assert_flags(self.run_check(), "commit")

    def test_commit_inside_list_is_flagged_by_6b(self):
        # commit 不在判準 6 的清單裡（避免同一行報兩次），由 6b 的全檔掃描涵蓋 list/get。
        self.write_controller(_controller_source(
            list_body="request.env.cr.commit()\n" + _CLEAN_LIST_BODY))
        self._assert_flags(self.run_check(), "commit")

    def test_unparsable_controller_is_a_violation(self):
        self.write_controller("class PaasController(:\n")
        violations = self.run_check()
        self.assertTrue(any(v.startswith(rl.POLL_CONTROLLER_FILE + "：讀不到或 parse 失敗")
                            for v in violations), violations)


class TestRealRepo(unittest.TestCase):
    """陽性對照：develop 上 list/get 純讀、controller 無 init／poll／commit。"""

    def test_real_repo_is_clean(self):
        self.assertEqual(rl.check_poll_release_facts_single_source(_REPO_ROOT), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
