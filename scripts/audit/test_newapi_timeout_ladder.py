#!/usr/bin/env python3
"""repo-lint 第 42 條 newapi-timeout-ladder：透過真 repo-lint CLI 驗 source mutation（不 mock guard）。

形狀比照 ``test_init_n8n_budget_ladder.py``：``--all`` 驗整合與 exit code；其餘 mutation 用
``--hook`` 的 block JSON（同時驗 hook 分派接得到這幾個檔）。每次只改一個來源，最後還原。
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
CLIENT = 'src/services/newapi_client.py'
COMMON = 'src/models/llm_common.py'
KEYS = 'src/models/llm_api_key.py'
PROXY_APP = 'extra/newapi-proxy/newapi_proxy/app.py'
BASELINE = 'scripts/audit/baselines/newapi-timeout-ladder.json'
RULE = 'newapi-timeout-ladder'


class NewapiTimeoutLadderCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='newapi-ladder-')
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
            text=True, capture_output=True, timeout=120,
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

    def test_read_must_leave_room_for_the_proxy_deadline(self):
        # read=1 ⇒ 標頭被夾成 1，不小於 read：Odoo 會先逾時。
        self.mutation(CLIENT, 'TIMEOUT_INTERACTIVE_READ = (2, 3)',
                      'TIMEOUT_INTERACTIVE_READ = (2, 1)', '必須 < Odoo 的 read', full=True)
        self.mutation(CLIENT, 'PROXY_READ_TIMEOUT_MARGIN = 1',
                      'PROXY_READ_TIMEOUT_MARGIN = 0', '必須 ≥ 1')

    def test_client_clamp_must_equal_the_proxy_range(self):
        self.mutation(CLIENT, 'PROXY_READ_TIMEOUT_MAX = 30', 'PROXY_READ_TIMEOUT_MAX = 40',
                      '必須等於代理接受的上限 30')
        self.mutation(PROXY_APP, '_READ_TIMEOUT_RE = re.compile(r"[1-9]|[12][0-9]|30", re.ASCII)',
                      '_READ_TIMEOUT_RE = re.compile(r"[1-9]|1[0-9]|20", re.ASCII)',
                      '必須等於代理接受的上限 20')

    def test_header_must_use_the_named_clamp(self):
        self.mutation(CLIENT, 'str(max(1, min(PROXY_READ_TIMEOUT_MAX,',
                      'str(max(1, min(30,', '不要寫回字面值')

    def test_budgets_and_lease(self):
        self.mutation(CLIENT, 'KEY_CREATE_BUDGET_SECONDS = 20', 'KEY_CREATE_BUDGET_SECONDS = 10',
                      'KEY_CREATE_BUDGET_SECONDS=10 必須 ≥ 一次完整呼叫 11')
        self.mutation(KEYS, '_STALE_CREATING_SECONDS = 300', '_STALE_CREATING_SECONDS = 25',
                      '_STALE_CREATING_SECONDS=25')
        self.mutation(CLIENT, 'PROVISION_TICK_SECONDS = 60', 'PROVISION_TICK_SECONDS = 290',
                      'lease（PROVISION_LEASE_MINUTES）=300s')
        self.mutation(COMMON, 'PROVISION_LEASE_MINUTES = 5', 'PROVISION_LEASE_MINUTES = 1',
                      'lease（PROVISION_LEASE_MINUTES）=60s')

    def test_method_timeout_classes_are_pinned(self):
        self.mutation(CLIENT, "def user_log_self(self, client_ip, up, query, *, timeout=TIMEOUT_INTERACTIVE_READ,",
                      "def user_log_self(self, client_ip, up, query, *, timeout=TIMEOUT_CRON_READ,",
                      'user_log_self')
        self.mutation(CLIENT, "def ap_get_user(self, user_id, *, timeout=TIMEOUT_PROVISION, deadline=None):",
                      "def ap_get_user(self, user_id, *, timeout=(5, 15), deadline=None):",
                      '不是 TIMEOUT_* 常數：ap_get_user')

    def test_missing_constant_fails_loud(self):
        self.mutation(CLIENT, 'SESSION_REVOKE_GRACE_SECONDS = 5', 'SESSION_REVOKE_GRACE = 5',
                      '無法載入或解析')
        self.mutation(BASELINE, '"methods"', '"method"', '與 scripts/audit/baselines/newapi-timeout-ladder.json 不符')


if __name__ == '__main__':
    unittest.main()
