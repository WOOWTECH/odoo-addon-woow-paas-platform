#!/usr/bin/env python3
"""#1277：驗證三份 leader config 的 create-only 配額與續談行為。

執行：python3 .omnigent/test-spawn-bounds.py
使用與 build-bundles --check 相同的 OMNIGENT_SITE_PACKAGES 設定。
這是 policy evaluator 回歸測試，不是 server／runner 雙重 gate 的整合證據。
"""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("build_bundles", HERE / "build-bundles.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)
BUILDER._import_omnigent()

import yaml
from omnigent.policies.builtins.orchestration import spawn_bounds


class TestSpawnBounds(unittest.TestCase):
    def setUp(self):
        self.configs = [HERE / "woow-issue-leader" / "config.yaml"]
        self.configs.extend(
            HERE / "woow-issue-leader" / "agents" / name / "config.yaml"
            for name in ("repo_leader_cc", "repo_leader_pi")
        )

    def policies(self):
        for path in self.configs:
            config = yaml.safe_load(path.read_text())
            args = config["guardrails"]["policies"]["spawn_bounds"]["function"]["arguments"]
            yield path, args, spawn_bounds(**args)

    @staticmethod
    def call(policy, tool):
        return policy({"type": "tool_call", "data": {"name": tool, "arguments": {}}})

    def test_all_leaders_use_create_only_limit_12(self):
        for path, args, _ in self.policies():
            with self.subTest(config=str(path)):
                self.assertEqual(args, {
                    "max_dispatches_per_turn": 12,
                    "dispatch_tools": ["sys_session_create"],
                })

    def test_six_workers_create_and_send_in_one_turn(self):
        for path, _, policy in self.policies():
            with self.subTest(config=str(path)):
                for _ in range(6):
                    for tool in ("sys_session_create", "sys_session_send"):
                        self.assertEqual(self.call(policy, tool)["result"], "ALLOW")

    def test_continuations_do_not_consume_creation_budget(self):
        for path, _, policy in self.policies():
            with self.subTest(config=str(path)):
                for _ in range(12):
                    self.assertEqual(self.call(policy, "sys_session_create")["result"], "ALLOW")
                for _ in range(100):
                    result = self.call(policy, "sys_session_send")
                    self.assertEqual(result["result"], "ALLOW")
                    self.assertNotIn("state_updates", result)
                self.assertEqual(self.call(policy, "sys_session_create")["result"], "DENY")

    def test_new_request_resets_creation_budget(self):
        for path, _, policy in self.policies():
            with self.subTest(config=str(path)):
                for _ in range(13):
                    self.call(policy, "sys_session_create")
                result = policy({"type": "request"})
                self.assertEqual(result["result"], "ALLOW")
                self.assertEqual(result["state_updates"][0]["value"], 0)
                self.assertEqual(self.call(policy, "sys_session_create")["result"], "ALLOW")

    def test_persisted_count_survives_evaluator_rebuild(self):
        for path, args, _ in self.policies():
            with self.subTest(config=str(path)):
                state = {}
                for index in range(13):
                    # 模擬 server 每次 tool call 重建 evaluator 並套用 state_updates。
                    result = spawn_bounds(**args)({
                        "type": "tool_call",
                        "data": {"name": "sys_session_create", "arguments": {}},
                        "session_state": state,
                    })
                    self.assertEqual(result["result"], "ALLOW" if index < 12 else "DENY")
                    for update in result["state_updates"]:
                        state[update["key"]] = state.get(update["key"], 0) + update["value"]


if __name__ == "__main__":
    unittest.main()
