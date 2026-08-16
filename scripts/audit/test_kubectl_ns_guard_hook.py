#!/usr/bin/env python3
"""kubectl-ns-guard hook 的行為測試：deny 案例＝注入紅燈，allow 案例＝假陽性迴歸。

新 gate 的第一個測試是注入紅燈——先證明它擋得住該擋的（TestDeny），再證明它不會
誤擋（TestAllow）。TestAllow 的每一條都對應「報告範例 hook」會誤擋的形狀（heredoc
寫檔、引號內字串、白名單內 namespace 被當禁區），這些是本 hook 存在的差異點，退化
就等於退回 denylist grep。

白名單內容對不對**不在這裡驗**——那是 repo-lint kubectl-ns-whitelist-sync 的職責
（三份副本對帳），重複驗只會多一份會漂的清單。這裡只驗行為：解析、分流、deny 訊息。

跑法：
    python3 scripts/audit/test_kubectl_ns_guard_hook.py
    # 或 python3 -m pytest scripts/audit/test_kubectl_ns_guard_hook.py

契約（.gitea/workflows/repo-lint.yml 的 test_*.py glob 步驟）：不得有外部依賴——
不碰 kubeconfig、不碰叢集、不碰網路，也不依賴 cwd。
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_HOOK_PATH = _REPO_ROOT / ".claude" / "hooks" / "kubectl-ns-guard.py"

# 檔名有連字號，只能走 importlib（同 test_kubectl_ns_whitelist_sync.py 的做法）。
_spec = importlib.util.spec_from_file_location("kubectl_ns_guard", _HOOK_PATH)
guard = importlib.util.module_from_spec(_spec)
sys.modules["kubectl_ns_guard"] = guard
_spec.loader.exec_module(guard)


def _denies(command):
    return guard.analyze(command)


class TestDeny(unittest.TestCase):
    """該擋的要擋——每一條都是真實事故形狀或其最小變體。"""

    def test_exec_into_foreign_namespace(self):
        # 2026-08 實際發生過的事故形狀：exec 進非白名單 ns。
        reasons = _denies(
            "kubectl --context woow-k3s exec -it pod-x -n longhorn-system -- sh")
        self.assertTrue(reasons)
        self.assertIn("longhorn-system", reasons[0])

    def test_delete_in_foreign_namespace(self):
        self.assertTrue(_denies(
            "kubectl --context woow-k3s delete pod x -n gitea"))

    def test_write_without_namespace(self):
        # 沒帶 -n ⇒ 落在 default ns，不在白名單。
        self.assertTrue(_denies(
            "kubectl --context woow-k3s apply -f deploy.yaml"))

    def test_write_without_context(self):
        # current-context 可能正指著 woow-k3s ⇒ 寫入類必須明確指定 --context。
        self.assertTrue(_denies("kubectl delete pod x -n paas-system"))

    def test_write_all_namespaces(self):
        self.assertTrue(_denies(
            "kubectl --context woow-k3s delete pods -A"))

    def test_inline_equals_forms(self):
        # --context=、-n= 黏值寫法要解析得到。
        self.assertTrue(_denies(
            "kubectl --context=woow-k3s patch deploy x -n=default -p '{}'"))

    def test_bash_dash_c_is_recursed(self):
        # bash -c 是最自然的繞道，要遞迴解析一層。
        self.assertTrue(_denies(
            'bash -c "kubectl --context woow-k3s exec pod -n longhorn-system -- sh"'))

    def test_second_command_in_chain(self):
        # 複合指令的第二段也要看。
        reasons = _denies(
            "kubectl --context woow-k3s get pods && "
            "kubectl --context woow-k3s delete pod y -n cattle-system")
        self.assertTrue(reasons)
        self.assertIn("cattle-system", reasons[0])

    def test_unknown_verb_is_fail_closed(self):
        # 沒見過的動詞當寫入類：漏擋比錯擋貴。
        self.assertTrue(_denies(
            "kubectl --context woow-k3s frobnicate thing -n gitea"))


class TestAllow(unittest.TestCase):
    """不該擋的不擋——這些是與 denylist grep 的差異點，退化即退回原型錯誤。"""

    def test_read_verbs_any_namespace(self):
        # CLAUDE.md 允許跨 ns 唯讀盤點供歸屬分流。
        for cmd in (
            "kubectl --context woow-k3s get pods -A",
            "kubectl --context woow-k3s describe pod x -n longhorn-system",
            "kubectl --context woow-k3s logs pod-x -n gitea",
            "kubectl get pods",  # 唯讀不帶 context：本機日常，不擋
        ):
            self.assertEqual(_denies(cmd), [], cmd)

    def test_whitelisted_namespaces_writeable(self):
        # 白名單內的 ns 是授權範圍——報告範例 hook 把 paas-system 當禁區，
        # 這條就是防那個原型錯誤的迴歸。
        for ns in ("paas-system", "paas-stg-system", "paas-ws-tenant1",
                   "paas-ws-stg-a", "dev-sandbox-foo", "fleet-local"):
            cmd = ("kubectl --context woow-k3s exec -it web-0 -n %s -- sh" % ns)
            self.assertEqual(_denies(cmd), [], ns)

    def test_docker_desktop_unrestricted(self):
        self.assertEqual(_denies(
            "kubectl --context docker-desktop delete ns anything"), [])

    def test_no_cluster_verbs(self):
        self.assertEqual(_denies("kubectl config get-contexts"), [])
        self.assertEqual(_denies("kubectl version --client"), [])

    def test_heredoc_body_is_not_execution(self):
        # 寫一個內容含 kubectl 的 script 檔 ≠ 執行 kubectl。
        cmd = ("cat > /tmp/s.sh <<'EOF'\n"
               "kubectl --context woow-k3s exec pod -n longhorn-system -- sh\n"
               "EOF")
        self.assertEqual(_denies(cmd), [])

    def test_quoted_string_is_not_command_position(self):
        for cmd in (
            'echo "kubectl --context woow-k3s delete ns gitea"',
            'git commit -m "fix: kubectl exec -n gitea bug"',
            'grep -r "kubectl delete" docs/',
        ):
            self.assertEqual(_denies(cmd), [], cmd)

    def test_quoted_separator_does_not_split(self):
        # 引號內的 && 不是指令分隔——denylist grep 分不出這個，本 hook 必須分得出。
        self.assertEqual(_denies(
            'echo "a && kubectl --context woow-k3s delete pod x -n gitea"'), [])

    def test_remote_command_after_double_dash(self):
        # `--` 之後屬遠端指令，那裡的 -n 不是 kubectl 的 namespace。
        self.assertEqual(_denies(
            "kubectl --context woow-k3s exec web-0 -n paas-system -- "
            "psql -n other_flag"), [])

    def test_rollout_in_whitelisted_ns(self):
        self.assertEqual(_denies(
            "kubectl --context woow-k3s rollout restart deploy/x -n paas-stg-system"),
            [])


class TestEndToEndContract(unittest.TestCase):
    """subprocess 層契約：stdin JSON 進、deny JSON／空輸出出，exit 一律 0。"""

    def _run(self, command, env_extra=None):
        env = dict(os.environ)
        env.pop("KUBECTL_NS_GUARD", None)
        env.update(env_extra or {})
        proc = subprocess.run(
            [sys.executable, str(_HOOK_PATH)],
            input=json.dumps({"tool_input": {"command": command}}),
            capture_output=True, text=True, env=env, timeout=30)
        return proc

    def test_deny_emits_permission_decision(self):
        proc = self._run(
            "kubectl --context woow-k3s exec pod -n longhorn-system -- sh")
        self.assertEqual(proc.returncode, 0)
        decision = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("longhorn-system", decision["permissionDecisionReason"])

    def test_allow_is_silent(self):
        proc = self._run("kubectl --context woow-k3s get pods -A")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_non_kubectl_fast_path(self):
        proc = self._run("ls -la && git status")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_broken_stdin_fails_open(self):
        proc = subprocess.run(
            [sys.executable, str(_HOOK_PATH)], input="not json",
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_inline_bypass(self):
        proc = self._run(
            "KUBECTL_NS_GUARD=off kubectl --context woow-k3s delete ns x -n gitea")
        self.assertEqual(proc.stdout, "")

    def test_env_bypass(self):
        proc = self._run(
            "kubectl --context woow-k3s delete pod x -n gitea",
            env_extra={"KUBECTL_NS_GUARD": "off"})
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
