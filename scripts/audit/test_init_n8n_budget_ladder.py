#!/usr/bin/env python3
"""#1257 Q31：透過真 repo-lint CLI 驗 n8n init budget 的 source mutation，不 mock guard。

形狀比照 ``test_init_odoo_budget_ladder.py``：--all 驗整合與 exit code；多筆
mutation 用 --hook 的 block JSON。每次只改一個來源，最後還原，不碰 working tree。
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
OPERATOR = 'extra/paas-operator/src/services/n8n_init.py'
KUBECTL = 'extra/paas-operator/src/services/helm.py'
RULE = 'init-n8n-budget-ladder'


class InitN8nBudgetCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='init-n8n-budget-')
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
                self.assertTrue(result.stdout.strip(), 'guard 靜默放行 source mutation：' + new)
                payload = json.loads(result.stdout)
                self.assertEqual(payload['decision'], 'block')
                reason = payload['reason']
            self.assertIn(RULE, reason)
            self.assertIn(message, reason)
        finally:
            path.write_text(original)

    def test_real_tree_passes_full_cli(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(RULE, result.stdout)

    def test_read_budget_must_exceed_server_envelope(self):
        # 真 envelope＝531（find_pod 60＋readiness 206＋5 次 node 75＋secret／patch／
        # deployment 90＋restart 100）；把 read 壓到 envelope 以下／相等都要紅。
        self.mutation(CLIENT, 'INIT_N8N_READ_TIMEOUT = 600',
                      'INIT_N8N_READ_TIMEOUT = 120', 'envelope=531', full=True)
        self.mutation(CLIENT, 'INIT_N8N_READ_TIMEOUT = 600',
                      'INIT_N8N_READ_TIMEOUT = 531', 'envelope=531')

    def test_dedicated_tuple_wiring_cannot_regress(self):
        original = 'timeout=(INIT_N8N_CONNECT_TIMEOUT, INIT_N8N_READ_TIMEOUT)'
        for replacement in (
            'timeout=HELM_OPERATION_TIMEOUT',
            'timeout=INIT_N8N_READ_TIMEOUT',
            'timeout=(INIT_N8N_READ_TIMEOUT, INIT_N8N_CONNECT_TIMEOUT)',
            'timeout=(INIT_ODOO_CONNECT_TIMEOUT, INIT_ODOO_READ_TIMEOUT)',
        ):
            with self.subTest(replacement=replacement):
                self.mutation(CLIENT, original, replacement, 'timeout wiring')
        self.mutation(CLIENT, 'INIT_N8N_CONNECT_TIMEOUT = 120',
                      'INIT_N8N_CONNECT_TIMEOUT = 600', 'connect 必須維持')
        self.mutation(CLIENT, 'INIT_N8N_CONNECT_TIMEOUT = 120',
                      'INIT_N8N_CONNECT_TIMEOUT = HELM_OPERATION_TIMEOUT', '正整數 literal')

    def test_server_substeps_are_read_from_source(self):
        cases = (
            # readiness 20×10 + 19×4 = 276（原 206）
            (OPERATOR, 'MAX_READY_RETRIES = 15', 'MAX_READY_RETRIES = 20', 'envelope=601'),
            # readiness 15×10 + 14×10 = 290
            (OPERATOR, 'READY_RETRY_DELAY = 4', 'READY_RETRY_DELAY = 10', 'envelope=615'),
            # readiness exec 15×20 = 300（原 150）
            (OPERATOR, 'timeout=10,', 'timeout=20,', 'envelope=681'),
            # 5 次沒帶 timeout 的 node request：5×30（原 5×15）
            (OPERATOR, 'capture_headers: bool = False,\n        timeout: int = 15,',
             'capture_headers: bool = False,\n        timeout: int = 30,', 'envelope=606'),
            # restart 6×30 + 2×5 = 190（原 3×30 + 2×5 = 100）
            (OPERATOR, 'for attempt in range(3):', 'for attempt in range(6):', 'envelope=621'),
            # restart 間隔 2×60（原 2×5）
            (OPERATOR, 'time.sleep(5)', 'time.sleep(60)', 'envelope=641'),
            # 8 次沒帶 timeout 的 kubectl：8×40（原 8×30）
            (KUBECTL, 'input_data: Optional[str] = None,\n        timeout: int = 30,',
             'input_data: Optional[str] = None,\n        timeout: int = 40,', 'envelope=611'),
        )
        for case in cases:
            with self.subTest(source=case[0], change=case[2]):
                self.mutation(*case)

    def test_unknown_control_flow_fails_loud(self):
        cases = (
            (OPERATOR, 'if attempt < MAX_READY_RETRIES - 1:', 'if attempt < MAX_READY_RETRIES:'),
            (OPERATOR, 'for attempt in range(MAX_READY_RETRIES):',
             'for attempt in range(MAX_READY_RETRIES + 1):'),
            (OPERATOR, 'cookie = self._login(namespace, pod_name, container, owner_email, owner_password)',
             'cookie = self._login(namespace, pod_name, container, owner_email, owner_password)\n'
             '        cookie = self._login(namespace, pod_name, container, owner_email, owner_password)'),
            (OPERATOR, '"POST", "/rest/owner/setup",', '"POST", "/rest/owner/setup", timeout=999,'),
            (OPERATOR, 'f"app.kubernetes.io/instance={release_name}",\n        ]',
             'f"app.kubernetes.io/instance={release_name}",\n            "x",\n        ]'),
            (OPERATOR, 'if attempt < 2:', 'if attempt < 3:'),
            (KUBECTL, 'args,\n                input_data=input_data,\n                timeout=timeout,',
             'args,\n                input_data=input_data,\n                timeout=999,'),
            (CLIENT, '                timeout=timeout,', '                timeout=30,'),
        )
        for case in cases:
            with self.subTest(source=case[0], change=case[2]):
                self.mutation(*case, '結構變動')

    def test_reassigned_budget_cannot_keep_the_old_literal(self):
        self.mutation(OPERATOR, 'MAX_READY_RETRIES = 15',
                      'MAX_READY_RETRIES = 15\nMAX_READY_RETRIES += 70', '唯一寫入')

    def test_missing_or_unparseable_inputs_do_not_pass(self):
        self.mutation(OPERATOR, 'MAX_READY_RETRIES = 15', 'MAX_READY_RETRIES = unknown()', '正整數 literal')
        self.mutation(OPERATOR, 'MAX_READY_RETRIES = 15', 'MAX_READY_RETRIES = (', '無法驗證')
        for rel in (OPERATOR, 'scripts/audit/baselines/init-n8n-budget-structure.json',
                    'scripts/audit/init_n8n_budget.py'):
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
