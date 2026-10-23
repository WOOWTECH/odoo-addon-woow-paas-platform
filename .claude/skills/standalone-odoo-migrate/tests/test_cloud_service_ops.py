"""scripts/platform/cloud_service_ops.py 與 cloud-service.sh 的測試（不需要叢集、不需要 Odoo）。

cloud-service.sh 的端到端測試用一支假 kubectl：它把 stdin（cloud_service_ops.py＋main 呼叫）接在假 env 後面用 python3 執行。
"""
import datetime
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "platform"
sys.path.insert(0, str(SCRIPTS))

import cloud_service_ops as ops  # noqa: E402

FAKE_ENV = textwrap.dedent('''
    import datetime, json

    class _Rec:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Service:
        def __init__(self, env, sid, **kw):
            self.env, self.id = env, sid
            self.calls = []
            self.name = kw.get("name", "harumi-rehearsal")
            self.helm_release_name = kw.get("release", "svc-ef66136d")
            self.state = kw.get("state", "running")
            self.error_message = False
            self.workspace_id = _Rec(id=1)
            self.subscription_id = _Rec(state="in_progress")
            self.helm_values = json.dumps({"conf": {"adminPassword": "s3cret", "adminEmail": "a@example.com",
                                                     "limitTimeCpu": 60}, "replicas": 1})
            self._exists = kw.get("exists", True)
            self._fail = kw.get("fail")

        def sudo(self):
            return self

        def browse(self, sid):
            return self if sid == self.id else Service(self.env, sid, exists=False)

        def exists(self):
            return self._exists

        def _helm_values_dict(self):
            return json.loads(self.helm_values)

        def _apply_helm_values(self, values):
            self.calls.append("apply")
            self.helm_values = json.dumps(values)

        def _do_suspend(self):
            self.calls.append("suspend")
            self.state = "error" if self._fail == "suspend" else "suspended"

        def _do_resume(self):
            self.calls.append("resume")
            self.state = "running"

        def action_redeploy(self):
            self.calls.append("redeploy")
            if self._fail == "redeploy":
                raise RuntimeError("operator 503")
            self.state = "deploying"

    class Cr:
        def __init__(self):
            self.log = []

        def commit(self):
            self.log.append("commit")

        def rollback(self):
            self.log.append("rollback")

    class Env:
        def __init__(self, **kw):
            self.cr = Cr()
            self.service = Service(self, 263, **kw)

        def __getitem__(self, model):
            assert model == "woow_paas_platform.cloud_service", model
            return self.service

        def ref(self, xmlid, raise_if_not_found=True):
            assert xmlid == "woow_paas_platform.ir_cron_subscription_lifecycle", xmlid
            return _Rec(nextcall=datetime.datetime(2026, 10, 15, 14, 50, 54), active=True)
''')
NS = {}
exec(FAKE_ENV, NS)  # noqa: S102 — 測試用假 env


def call(action, env=None, **kw):
    env = env or NS["Env"]()
    args = {"action": action, "id": 263, "name": "harumi-rehearsal", "release": "svc-ef66136d", **kw}
    buf = io.StringIO()
    with redirect_stdout(buf):
        out = ops.main(env, args)
    lines = buf.getvalue().splitlines()
    assert lines[0] == ops.MARK_START and lines[-1] == ops.MARK_END, lines
    assert json.loads(lines[1])["ok"] == out["ok"]
    return out, env


class CloudServiceOpsTest(unittest.TestCase):
    def test_mismatch_does_nothing(self):
        out, env = call("suspend", name="harumi-odoo")
        self.assertFalse(out["ok"])
        self.assertIn("does not match", out["error"])
        self.assertEqual(env.service.calls, [])
        self.assertEqual(env.cr.log, ["rollback"])

    def test_not_found(self):
        out, env = call("status", id=999)
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["error"])

    def test_status_is_read_only_and_hides_secrets(self):
        out, env = call("status")
        self.assertTrue(out["ok"])
        self.assertEqual(env.cr.log, [])
        self.assertEqual(out["conf"], {"limitTimeCpu": 60, "limitTimeReal": None, "serverWideModules": None})
        self.assertEqual(out["lifecycle_nextcall_utc"], datetime.datetime(2026, 10, 15, 14, 50, 54))
        self.assertNotIn("s3cret", json.dumps(out, default=str))

    def test_suspend_then_resume(self):
        out, env = call("suspend")
        self.assertTrue(out["ok"])
        self.assertEqual((out["state_before"], out["state"]), ("running", "suspended"))
        self.assertEqual(env.cr.log, ["commit"])
        out, env = call("resume", env=env)
        self.assertEqual((out["state_before"], out["state"]), ("suspended", "running"))

    def test_wrong_state_is_refused(self):
        env = NS["Env"](state="suspended")
        out, env = call("suspend", env=env)
        self.assertFalse(out["ok"])
        self.assertIn("needs state", out["error"])
        self.assertEqual(env.service.calls, [])
        out, env = call("redeploy", env=NS["Env"](state="suspended"))
        self.assertFalse(out["ok"])

    def test_operator_error_state_is_not_ok(self):
        out, env = call("suspend", env=NS["Env"](fail="suspend"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "error")

    def test_conf_merges_only_known_keys_and_redeploys(self):
        out, env = call("conf", conf={"limitTimeCpu": 1200, "limitTimeReal": 2400,
                                      "serverWideModules": "base,web,mail_fetchmail_durable", "adminPassword": "x"})
        self.assertTrue(out["ok"])
        self.assertEqual(env.service.calls, ["apply", "redeploy"])
        self.assertEqual(env.cr.log, ["commit", "commit"])
        conf = json.loads(env.service.helm_values)["conf"]
        self.assertEqual(conf["adminPassword"], "s3cret")  # 不在三個鍵內的不動
        self.assertEqual(conf["adminEmail"], "a@example.com")
        self.assertEqual((conf["limitTimeCpu"], conf["limitTimeReal"]), (1200, 2400))
        self.assertEqual(out["conf_before"]["limitTimeCpu"], 60)
        self.assertNotIn("s3cret", json.dumps(out, default=str))

    def test_conf_keeps_values_when_redeploy_fails(self):
        out, env = call("conf", env=NS["Env"](fail="redeploy"), conf={"limitTimeCpu": 1200})
        self.assertFalse(out["ok"])
        self.assertIn("operator 503", out["error"])
        self.assertEqual(env.cr.log, ["commit", "rollback"])  # helm_values 先 commit 了

    def test_conf_without_keys_is_refused(self):
        out, env = call("conf", conf={})
        self.assertFalse(out["ok"])
        self.assertEqual(env.service.calls, [])


class CloudServiceShTest(unittest.TestCase):
    """cloud-service.sh：參數處理、base64 傳參、JSON 擷取與 exit code。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "fake_env.py").write_text(FAKE_ENV)
        kubectl = tmp / "kubectl"
        # 假 kubectl：記下參數，把 stdin 接在假 env 後面執行（模擬 odoo shell 的 log 與 env 變數）
        kubectl.write_text(textwrap.dedent(f'''\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" > {tmp}/kubectl.args
            echo "2026-10-15 INFO odoo: shell started"
            python3 -c 'import sys; ns = {{}}; exec(open("{tmp}/fake_env.py").read(), ns); \\
            ns["env"] = ns["Env"](**__import__("json").loads(sys.argv[1])); exec(sys.stdin.read(), ns)' \\
              "$FAKE_ENV_KW"
        '''))
        kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_sh(self, *args, fake_kw=None):
        env = dict(self.env, FAKE_ENV_KW=json.dumps(fake_kw or {}))
        return subprocess.run(["bash", str(SCRIPTS / "cloud-service.sh"), *args], capture_output=True, text=True, env=env)

    BASE = ("--context", "woow-k3s", "--id", "263", "--name", "harumi-rehearsal", "--release", "svc-ef66136d")

    def test_status_end_to_end(self):
        r = self.run_sh("status", *self.BASE)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["action"], out["state"]), ("status", "running"))
        args = (Path(self.tmp.name) / "kubectl.args").read_text()
        self.assertIn("-n paas-system exec -i deploy/paas-odoo-platform-cron -c odoo", args)
        self.assertIn("-d woow_prod", args)

    def test_conf_end_to_end(self):
        r = self.run_sh("conf", *self.BASE, "--limit-time-cpu", "1200",
                        "--server-wide-modules", "base,web,mail_fetchmail_durable")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["conf"]["limitTimeCpu"], 1200)
        self.assertEqual(out["conf"]["serverWideModules"], "base,web,mail_fetchmail_durable")
        self.assertIsNone(out["conf"]["limitTimeReal"])

    def test_mismatch_exits_1(self):
        r = self.run_sh("suspend", "--context", "woow-k3s", "--id", "263", "--name", "harumi-odoo",
                        "--release", "svc-ef66136d")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match", json.loads(r.stdout)["error"])

    def test_stg_flags(self):
        r = self.run_sh("status", *self.BASE, "--platform-ns", "paas-stg-system",
                        "--deploy", "paas-odoo-platform-stg", "--db", "woow_stg")
        self.assertEqual(r.returncode, 0, r.stderr)
        args = (Path(self.tmp.name) / "kubectl.args").read_text()
        self.assertIn("-n paas-stg-system exec -i deploy/paas-odoo-platform-stg -c odoo", args)
        self.assertIn("-d woow_stg", args)

    def test_bad_args_exit_2(self):
        self.assertEqual(self.run_sh("conf", *self.BASE).returncode, 2)  # conf 沒給任何鍵
        self.assertEqual(self.run_sh("status", *self.BASE, "--limit-time-cpu", "10").returncode, 2)
        self.assertEqual(self.run_sh("status", "--context", "x", "--id", "abc", "--name", "n",
                                     "--release", "r").returncode, 2)
        self.assertEqual(self.run_sh("delete", *self.BASE).returncode, 2)

    def test_no_json_exits_3(self):
        kubectl = Path(self.tmp.name) / "kubectl"
        kubectl.write_text('#!/usr/bin/env bash\necho "Error from server (NotFound): pods not found" >&2\nexit 1\n')
        r = self.run_sh("status", *self.BASE)
        self.assertEqual(r.returncode, 3)
        self.assertIn("NotFound", r.stderr)


if __name__ == "__main__":
    unittest.main()
