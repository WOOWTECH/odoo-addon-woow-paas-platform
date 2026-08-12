#!/usr/bin/env python3
"""memory-links-guard hook 的自測（CI 每 PR 跑，見 .gitea/workflows/repo-lint.yml）。

被測物：.claude/hooks/memory-links-guard.py（PostToolUse wrapper，只在寫入
`…/.claude/projects/<slug>/memory/` 時跑 memory-integrity.py --links）。

🔴 第一個測試是**注入紅燈**（斷鏈＋孤兒 fixture → 必須產出 block decision），
不是「跑一遍看乾不乾淨」——wrapper 的 fail-open 設計讓「hook 壞掉」與「沒有
finding」輸出相同（都是安靜 exit 0），沒有陽性對照的話，把 subprocess 呼叫整段
刪掉這套測試照樣全綠。

契約（repo-lint.yml 的 scripts/ 自我測試步驟）：無外部依賴——不碰 ~/.claude、
不需網路／kubeconfig／Odoo／特定 cwd。fixture 全在 tempfile 目錄裡自建，
memory 目錄形態靠路徑樣式（`…/.claude/projects/<slug>/memory/`）匹配，與真實
$HOME 無關。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD = os.path.join(REPO_ROOT, ".claude", "hooks", "memory-links-guard.py")


def run_guard(stdin_text, env_overrides=None):
    env = {"PATH": os.environ.get("PATH", ""), "CLAUDE_PROJECT_DIR": REPO_ROOT}
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, GUARD], input=stdin_text,
        capture_output=True, text=True, timeout=30, env=env,
    )


def hook_json(file_path):
    return json.dumps({"tool_name": "Write", "tool_input": {"file_path": file_path}})


class MemoryLinksGuardTest(unittest.TestCase):

    def make_memory_dir(self, tmp):
        """fixture 的路徑必須長成 …/.claude/projects/<slug>/memory/ 才會觸發。"""
        mem = os.path.join(tmp, ".claude", "projects", "test-slug", "memory")
        os.makedirs(mem)
        return mem

    def test_red_broken_index_produces_block_decision(self):
        """注入紅燈：斷鏈＋孤兒都在場 → 必須產出 decision:block 且兩種都點名。

        這是整套測試的陽性對照：它若不紅，代表 wrapper 根本沒把 finding 帶回來
        （fail-open 把一切吞掉），其餘綠燈測試全部失去意義。
        """
        with tempfile.TemporaryDirectory() as tmp:
            mem = self.make_memory_dir(tmp)
            with open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
                fh.write("- [不存在的檔](missing_target.md)\n")
            with open(os.path.join(mem, "orphan_file.md"), "w", encoding="utf-8") as fh:
                fh.write("孤兒：存在但索引沒連到\n")

            proc = run_guard(hook_json(os.path.join(mem, "MEMORY.md")))

            self.assertEqual(proc.returncode, 0)  # hook 永遠 exit 0，訊號在 stdout JSON
            out = json.loads(proc.stdout)
            self.assertEqual(out["decision"], "block")
            self.assertIn("missing_target.md", out["reason"])
            self.assertIn("orphan_file.md", out["reason"])
            self.assertIn("斷鏈", out["reason"])
            self.assertIn("孤兒", out["reason"])

    def test_block_reason_offers_the_topic_file_route_and_the_current_size(self):
        """🔴 #693：這道 gate 誤報時，舊文案會**主動教**人把連結搬回 MEMORY.md。

        MEMORY.md 有載入上限，照著做等於製造一個比原問題更嚴重的問題。所以文案
        必須同時給出另一條合法路徑（在既有 topic 檔補一行），並把目前大小／上限
        端出來，讓讀的人看得到搬回索引的代價。
        """
        with tempfile.TemporaryDirectory() as tmp:
            mem = self.make_memory_dir(tmp)
            with open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
                fh.write("- [不存在的檔](missing_target.md)\n")

            proc = run_guard(hook_json(os.path.join(mem, "MEMORY.md")))

            out = json.loads(proc.stdout)
            self.assertEqual(out["decision"], "block")
            self.assertIn("topic 檔", out["reason"])
            self.assertIn("載入上限", out["reason"])
            # 目前大小／上限那一行必須真的在 reason 裡（不是只講「有上限」）
            self.assertIn("MEMORY.md ", out["reason"])
            self.assertIn("/ 25,000 bytes", out["reason"])

    def test_orphan_reachable_through_a_topic_file_does_not_block(self):
        """🔴 #693 迴歸：照官方做法把索引外移到 topic 檔，不得再擋住 memory 寫入。

        這正是本 issue 的症狀：`--links` 判一批孤兒 → hook 升級成 block →
        之後每一次 memory 目錄的寫入都被擋住。
        """
        with tempfile.TemporaryDirectory() as tmp:
            mem = self.make_memory_dir(tmp)
            with open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
                fh.write("- [主題檔](topic_x.md)\n")
            with open(os.path.join(mem, "topic_x.md"), "w", encoding="utf-8") as fh:
                fh.write("- [a](reference_a.md)\n- [b](reference_b.md)\n")
            for name in ("reference_a.md", "reference_b.md"):
                with open(os.path.join(mem, name), "w", encoding="utf-8") as fh:
                    fh.write(f"---\nname: {name[:-3]}\n---\n內容\n")

            proc = run_guard(hook_json(os.path.join(mem, "reference_a.md")))

            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "",
                             "經 topic 檔可達的檔不得被判孤兒、更不得 block")

    def test_warning_only_findings_do_not_block(self):
        """wiki-link 指不到東西、索引越過壓縮線——都只是提醒，不能擋寫入。"""
        with tempfile.TemporaryDirectory() as tmp:
            mem = self.make_memory_dir(tmp)
            with open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
                fh.write("- [a](reference_a.md)\n" + ("x" * 499 + "\n") * 40)
            with open(os.path.join(mem, "reference_a.md"), "w", encoding="utf-8") as fh:
                fh.write("---\nname: reference_a\n---\n見 [[reference_never_written]]\n")

            proc = run_guard(hook_json(os.path.join(mem, "reference_a.md")))

            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

    def test_green_clean_index_stays_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = self.make_memory_dir(tmp)
            with open(os.path.join(mem, "linked.md"), "w", encoding="utf-8") as fh:
                fh.write("有入口的 memory 檔\n")
            with open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
                fh.write("- [有入口](linked.md)\n")

            proc = run_guard(hook_json(os.path.join(mem, "linked.md")))

            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

    def test_non_memory_path_passes_without_running_check(self):
        """一般 repo 檔案的編輯：安靜放行。

        用「不存在的路徑」當輸入——若 wrapper 誤跑了 --links，memory-integrity.py
        會因找不到目錄而 exit 1 產出 block，本測試就會紅；安靜通過即證明路徑
        過濾在子程序之前。
        """
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_guard(hook_json(os.path.join(tmp, "src", "models", "x.py")))
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

    def test_malformed_stdin_fails_open(self):
        proc = run_guard("這不是 JSON")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_missing_integrity_script_fails_open(self):
        """CLAUDE_PROJECT_DIR 指到沒有本 repo 內容的地方 → 安靜放行，不炸不擋。"""
        with tempfile.TemporaryDirectory() as tmp:
            mem = self.make_memory_dir(tmp)
            with open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
                fh.write("- [不存在的檔](missing_target.md)\n")

            proc = run_guard(hook_json(os.path.join(mem, "MEMORY.md")),
                             env_overrides={"CLAUDE_PROJECT_DIR": tmp})

            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
