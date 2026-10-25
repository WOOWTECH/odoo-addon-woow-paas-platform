"""前台 bundle（woow-security-sweep-launcher）build.py 的 CI 檢查（不需 omnigent）。

build.py --check 的 1／3 項要 import omnigent，只在本機跑；其餘項目搬進 unittest，
讓 CI（security-sweep-tests）在前台 bundle、launch skill 或 generate-worker.py 變動時擋下 drift。

執行：python3 -m unittest discover -s .omnigent/woow-security-sweep/tests -v
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / ".omnigent/woow-security-sweep-launcher"
_spec = importlib.util.spec_from_file_location("launcher_build", LAUNCHER / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


class TestLauncherBuild(unittest.TestCase):
    def test_config_yaml_matches_build_output(self):
        self.assertEqual(build.CONFIG.read_text(encoding="utf-8"), build.config_text(),
                         "config.yaml 與 build.py 不同步：重跑 python3 .omnigent/woow-security-sweep-launcher/build.py")

    def test_skill_copy_matches_source(self):
        self.assertEqual(build.skill_copy_problems(), [])

    def test_no_symlinks_in_bundle(self):
        self.assertEqual(build.symlinks(), [])

    def test_top_level_harness_not_native(self):
        self.assertFalse(build.HARNESS.endswith("-native"))
        self.assertIn(f"    harness: {build.HARNESS}\n", build.CONFIG.read_text(encoding="utf-8"))

    def test_no_spawn(self):
        self.assertIn("\nspawn: false\n", build.CONFIG.read_text(encoding="utf-8"))

    def test_guard_table_matches_config(self):
        cfg = build.CONFIG.read_text(encoding="utf-8")
        claimed = build.claimed_guards()
        self.assertTrue({"p_schedule_via_script", "p_state_via_script", "p_secret_no_api_key_read",
                         "p_forge_no_public_write", "p_gitea_issue_via_script"} <= claimed)
        self.assertEqual(sorted(n for n in claimed if f"\n    {n}:\n" not in cfg), [])

    def test_shared_cel_is_sweep_cel(self):
        """sweep 共用的三條必須逐字取自 sweep build.py（不是各自抄一份）。"""
        cfg = build.CONFIG.read_text(encoding="utf-8")
        for name in build.SHARED_CEL:
            with self.subTest(policy=name):
                block = next(p for p in build.policies() if p.startswith(f"    {name}:"))
                self.assertIn(block, cfg)
                self.assertIn(block, build.SB.config_text())


class TestRuntimeDocsHostAgnostic(unittest.TestCase):
    DOCS = [LAUNCHER / "AGENTS.md", REPO / ".agents/skills/woow-security-sweep-launch/SKILL.md"]

    def test_no_host_paths_and_python312(self):
        for doc in self.DOCS:
            text = doc.read_text(encoding="utf-8")
            with self.subTest(doc=doc.name):
                self.assertNotIn("/home/elmo", text)
                self.assertNotRegex(text, r"python3 [<~/]")


if __name__ == "__main__":
    unittest.main()
