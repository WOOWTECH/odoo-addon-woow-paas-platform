#!/usr/bin/env python3
"""#1152：透過真 repo-lint CLI 驗 source mutation，不 mock guard。

--all 驗實際整合與 exit code；多筆 mutation 用 --hook 的 block JSON，不能把
hook 的 exit 0 誤當通過。每次只改一個來源，最後還原，不碰 working tree。
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CLIENT = 'src/services/paas_operator.py'
OPERATOR = 'extra/paas-operator/src/services/odoo_init.py'
KUBECTL = 'extra/paas-operator/src/services/helm.py'
CONTRACT = 'extra/paas-operator/src/services/odoo_chart_contract.py'
RULE = 'init-odoo-budget-ladder'


class InitOdooBudgetCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='init-odoo-budget-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.repo = Path(cls.temp.name) / 'repo'
        shutil.copytree(ROOT, cls.repo, ignore=shutil.ignore_patterns(
            '.git', '__pycache__', '*.pyc', '.venv', '.pi',
        ))

    def cli(self, hook_path=None):
        return subprocess.run(
            [sys.executable, str(self.repo / 'scripts/audit/repo-lint.py'),
             '--hook' if hook_path else '--all'],
            input=json.dumps({'tool_input': {'file_path': str(self.repo / hook_path)}}) if hook_path else None,
            cwd=self.temp.name,
            env={**os.environ, 'CLAUDE_PROJECT_DIR': str(self.repo)},
            text=True, capture_output=True, timeout=60,
        )

    def mutation(self, rel, old, new, message, full=False):
        path = self.repo / rel
        original = path.read_text()
        self.assertEqual(original.count(old), 1, f'mutation 前提不成立：{rel} / {old}')
        try:
            path.write_text(original.replace(old, new))
            result = self.cli(None if full else rel)
            if full:
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                reason = result.stdout
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload['decision'], 'block')
                reason = payload['reason']
            self.assertIn(RULE, reason)
            self.assertIn(message, reason)
        finally:
            path.write_text(original)

    def test_read_budget_must_exceed_server_envelope(self):
        self.mutation(CLIENT, 'INIT_ODOO_READ_TIMEOUT = 900',
                      'INIT_ODOO_READ_TIMEOUT = 120', 'envelope=805', full=True)
        self.mutation(CLIENT, 'INIT_ODOO_READ_TIMEOUT = 900',
                      'INIT_ODOO_READ_TIMEOUT = 805', 'envelope=805')

    def test_real_tree_passes_full_cli(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(RULE, result.stdout)

    def test_dedicated_tuple_wiring_cannot_regress(self):
        original = 'timeout=(INIT_ODOO_CONNECT_TIMEOUT, INIT_ODOO_READ_TIMEOUT)'
        for replacement in (
            'timeout=HELM_OPERATION_TIMEOUT',
            'timeout=INIT_ODOO_READ_TIMEOUT',
            'timeout=(INIT_ODOO_READ_TIMEOUT, INIT_ODOO_CONNECT_TIMEOUT)',
            'timeout=(INIT_ODOO_CONNECT_TIMEOUT, INIT_ODOO_CONNECT_TIMEOUT)',
        ):
            with self.subTest(replacement=replacement):
                self.mutation(CLIENT, original, replacement, 'timeout wiring')
        self.mutation(CLIENT, 'INIT_ODOO_CONNECT_TIMEOUT = 120',
                      'INIT_ODOO_CONNECT_TIMEOUT = 900', 'connect 必須維持')
        self.mutation(CLIENT, 'INIT_ODOO_CONNECT_TIMEOUT = 120',
                      'INIT_ODOO_CONNECT_TIMEOUT = HELM_OPERATION_TIMEOUT', '正整數 literal')

    def test_server_substeps_are_read_from_source(self):
        cases = (
            (OPERATOR, 'MAX_READY_RETRIES = 30', 'MAX_READY_RETRIES = 40', 'envelope=1005'),
            (OPERATOR, 'READY_RETRY_DELAY = 5', 'READY_RETRY_DELAY = 10', 'envelope=950'),
            (OPERATOR, 'timeout=15,', 'timeout=20,', 'envelope=955'),
            (OPERATOR, 'MODULE_UPDATE_TIMEOUT = 120', 'MODULE_UPDATE_TIMEOUT = 300', 'envelope=985'),
            (KUBECTL, 'input_data: Optional[str] = None,\n        timeout: int = 30,',
             'input_data: Optional[str] = None,\n        timeout: int = 70,', 'envelope=925'),
        )
        for case in cases:
            with self.subTest(source=case[0], change=case[2]):
                self.mutation(*case)

    def test_unknown_control_flow_fails_loud(self):
        cases = (
            (CONTRACT, 'return f"{instance},{ODOO_SERVER_LABEL}", instance',
             'return f"{instance},{ODOO_SERVER_LABEL}", instance, instance'),
            (OPERATOR, 'if index == 1:', 'if index == 0:'),
            (OPERATOR, 'pod = self.k8s.get_pod(namespace, pod_name)',
             'pod = self.k8s.get_pod(namespace, pod_name)\n                    pod = self.k8s.get_pod(namespace, pod_name)'),
            (OPERATOR, 'if attempt < MAX_READY_RETRIES - 1:', 'if attempt < MAX_READY_RETRIES:'),
            (OPERATOR, 'range(MAX_READY_RETRIES)', 'range(MAX_READY_RETRIES + 1)'),
            (OPERATOR, 'result = self._update_module_list(namespace, pod_name, container)',
             'result = self._update_module_list(namespace, pod_name, container)\n        result = self._update_module_list(namespace, pod_name, container)'),
            (OPERATOR, 'timeout=MODULE_UPDATE_TIMEOUT,', 'timeout=999,'),
            (KUBECTL, 'args,\n                input_data=input_data,\n                timeout=timeout,',
             'args,\n                input_data=input_data,\n                timeout=999,'),
            (CLIENT, '                timeout=timeout,', '                timeout=30,'),
        )
        for case in cases:
            with self.subTest(source=case[0], change=case[2]):
                self.mutation(*case, '結構變動')

    def test_missing_or_unparseable_inputs_do_not_pass(self):
        self.mutation(OPERATOR, 'MAX_READY_RETRIES = 30', 'MAX_READY_RETRIES = unknown()', '正整數 literal')
        self.mutation(OPERATOR, 'MAX_READY_RETRIES = 30', 'MAX_READY_RETRIES = (', '無法驗證')
        for rel in (OPERATOR, 'scripts/audit/baselines/init-odoo-budget-structure.json',
                    'scripts/audit/init_odoo_budget.py'):
            with self.subTest(missing=rel):
                path = self.repo / rel
                original = path.read_bytes()
                try:
                    path.unlink()
                    result = self.cli(rel)
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload['decision'], 'block')
                    self.assertIn(RULE, payload['reason'])
                    self.assertIn('無法', payload['reason'])
                finally:
                    path.write_bytes(original)

    def test_comment_only_change_keeps_structure_valid(self):
        path = self.repo / OPERATOR
        original = path.read_text()
        try:
            path.write_text(original + '\n# 純註解不改 budget。\n')
            result = self.cli(OPERATOR)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
        finally:
            path.write_text(original)


if __name__ == '__main__':
    unittest.main(verbosity=2)
