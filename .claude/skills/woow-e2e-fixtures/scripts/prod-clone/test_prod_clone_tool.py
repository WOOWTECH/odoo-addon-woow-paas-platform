#!/usr/bin/env python3
"""prod_clone_tool.py 的單元測試（不碰叢集）。Run: python3 .claude/skills/woow-e2e-fixtures/scripts/prod-clone/test_prod_clone_tool.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import prod_clone_tool as t  # noqa: E402

DEPLOY = {
    "metadata": {"name": "rel", "namespace": "dev-sandbox-x"},
    "spec": {"template": {
        "metadata": {"labels": {"app.kubernetes.io/instance": "rel", "app.kubernetes.io/component": "server",
                                "app.kubernetes.io/name": "odoo"}},
        "spec": {
            "securityContext": {"fsGroup": 101},
            "imagePullSecrets": [{"name": "sandbox-registry"}],
            "initContainers": [{"name": "bootstrap", "image": "img"}],
            "containers": [
                {"name": "odoo", "image": "reg/odoo@sha256:abc", "securityContext": {"runAsUser": 100},
                 "env": [{"name": "ODOO_RC", "value": "/etc/odoo/odoo.conf"}, {"name": "X", "value": "1"}],
                 "volumeMounts": [{"name": "odoo-conf", "mountPath": "/etc/odoo/odoo.conf", "subPath": "odoo.conf"},
                                  {"name": "data", "mountPath": "/var/lib/odoo"},
                                  {"name": "addons", "mountPath": "/mnt/extra-addons"}]},
                {"name": "nginx", "image": "nginx"}],
            "volumes": [{"name": "odoo-conf", "secret": {"secretName": "rel-odoo-conf"}},
                        {"name": "data", "persistentVolumeClaim": {"claimName": "rel-data"}},
                        {"name": "addons", "emptyDir": {}},
                        {"name": "nginx-conf", "configMap": {"name": "n"}}]}}}}


class NeutralizeJobTest(unittest.TestCase):
    def test_job_shape(self):
        job = t.build_neutralize_job(DEPLOY, "j", "woow_prod", "prod-clone-neutralize", "rel-prod-clone-login")
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual([c["name"] for c in pod["containers"]], ["neutralize"])
        self.assertNotIn("initContainers", pod)
        labels = job["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels["app.kubernetes.io/instance"], "rel")
        self.assertEqual(labels["app.kubernetes.io/component"], "prod-clone-neutralize")
        vols = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(vols["data"], {"name": "data", "emptyDir": {}})  # RWO PVC 不掛
        self.assertNotIn("nginx-conf", vols)
        self.assertEqual(pod["imagePullSecrets"], [{"name": "sandbox-registry"}])
        c = pod["containers"][0]
        self.assertEqual(c["image"], "reg/odoo@sha256:abc")
        names = [e["name"] for e in c["env"]]
        self.assertIn("PROD_CLONE_PASSWORD", names)
        self.assertNotIn("X", names)
        self.assertIn("--stdout", c["command"][2])

    def test_requires_instance_label(self):
        bad = {"metadata": DEPLOY["metadata"], "spec": {"template": {"metadata": {"labels": {}},
                                                                    "spec": DEPLOY["spec"]["template"]["spec"]}}}
        with self.assertRaises(ValueError):
            t.build_neutralize_job(bad, "j", "d", "c", "")


class ExtractTest(unittest.TestCase):
    LOG = "\n".join(["noise", t.MARKER_BEGIN, "BEGIN;", t.PLATFORM_SQL_MARKER, "UPDATE a SET b=1;",
                     "COMMIT;", t.MARKER_END, "CORE_DATE=18.0.20250807", "PWHASH=$pbkdf2-sha512$600000$x$y"])

    def test_extract(self):
        sql, core, pwhash = t.extract(self.LOG)
        self.assertNotIn("BEGIN;", sql)
        self.assertNotIn("COMMIT;", sql)
        self.assertIn("UPDATE a SET b=1;", sql)
        self.assertEqual(core, "18.0.20250807")
        self.assertTrue(pwhash.startswith("$pbkdf2-sha512$600000$"))

    def test_truncated_log_rejected(self):
        with self.assertRaises(ValueError):
            t.extract(self.LOG.replace(t.MARKER_END, ""))


def _np(name, spec):
    return {"metadata": {"name": name}, "spec": spec}


DENY = _np("deny", {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []})
DNS = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
       "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}
SAME_NS = {"to": [{"podSelector": {}}]}


class NpCheckTest(unittest.TestCase):
    def test_lockdown_passes(self):
        allow = _np("allow", {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                              "ingress": [{"from": [{"podSelector": {}}]},
                                          {"from": [{"namespaceSelector": {"matchLabels": {
                                              "kubernetes.io/metadata.name": "kube-system"}}}]}],
                              "egress": [DNS, SAME_NS]})
        ok, infos, errs = t.np_check([DENY, allow])
        self.assertTrue(ok, errs)
        self.assertTrue(infos)

    def test_internet_fails(self):
        allow = _np("allow", {"podSelector": {}, "egress": [DNS, {"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}]})
        self.assertFalse(t.np_check([DENY, allow])[0])

    def test_paas_namespace_fails(self):
        for direction, key in (("egress", "to"), ("ingress", "from")):
            allow = _np("allow", {"podSelector": {}, direction: [{key: [{"namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "paas-system"}}}]}]})
            self.assertFalse(t.np_check([DENY, allow])[0], direction)

    def test_paas_managed_and_wildcards_fail(self):
        cases = [
            {"podSelector": {}, "egress": [{"to": [{"namespaceSelector": {"matchLabels": {"paas-managed": "true"}}}]}]},
            {"podSelector": {}, "egress": [{"to": [{"namespaceSelector": {}}]}]},
            {"podSelector": {}, "egress": [{"ports": [{"port": 443}]}]},  # 沒有 peer＝全部目的
            {"podSelector": {}, "egress": [{"to": [{"namespaceSelector": {"matchExpressions": [
                {"key": "x", "operator": "Exists"}]}}]}]},
        ]
        for spec in cases:
            self.assertFalse(t.np_check([DENY, _np("allow", spec)])[0], spec)

    def test_missing_default_deny_fails(self):
        self.assertFalse(t.np_check([_np("allow", {"podSelector": {}, "egress": [DNS]})])[0])


class FixtureTest(unittest.TestCase):
    FIXTURE = os.path.join(HERE, "assertions.toml")

    def test_repo_fixture_loads(self):
        checks = t.load_fixture(self.FIXTURE)
        ids = [c["check_id"] for c in checks]
        self.assertGreater(len(ids), 40)
        for required in ("A00_is_neutralized", "A01_cron_active_non_autovacuum", "B21_operator_url",
                         "K01_np_content", "K02_np_marker", "K03_np_probe", "K04_transfer_hole_absent",
                         "K05_incoming_clean", "K06_all_dbs_neutralized", "K00_target_guard",
                         "C08_conf_email_from", "D10_passwords_reset", "I01_admin_fingerprint"):
            self.assertIn(required, ids)
        sql = t.assert_sql(checks)
        self.assertIn("\\if :chk_present", sql)
        self.assertEqual(sql.count("\\if"), sum(1 for c in checks if c.get("kind", "sql") == "sql"))

    def test_eval(self):
        checks = [
            {"check_id": "A01_x", "table": "t", "query": "q", "expected": "0"},
            {"check_id": "A02_x", "table": "t", "query": "q", "expected": ">=1"},
            {"check_id": "A03_x", "table": "t", "query": "q", "expected": "0", "when_absent": "pass"},
            {"check_id": "A04_x", "table": "t", "query": "q", "expected": "0"},
            {"check_id": "A05_x", "table": "t", "query": "q", "expected": "0"},
            {"check_id": "I01_x", "table": "t", "query": "q", "expected": "*"},
            {"check_id": "K01_x", "table": "", "query": "np_content", "expected": "PASS", "kind": "builtin"},
            {"check_id": "K02_x", "table": "", "query": "np_marker", "expected": "present", "kind": "builtin"},
        ]
        out = "CHK|A01_x|0\nCHK|A02_x|3\nCHK|A03_x|__ABSENT__\nCHK|A04_x|__ABSENT__\nCHK|I01_x|abc\nnoise\n"
        rows, failed = t.assert_eval(checks, out, {"np_content": "PASS"})
        status = {r[0]: r[1] for r in rows}
        self.assertEqual(status, {"A01_x": "PASS", "A02_x": "PASS", "A03_x": "PASS", "A04_x": "FAIL",
                                  "A05_x": "FAIL", "I01_x": "INFO", "K01_x": "PASS", "K02_x": "SKIP"})
        self.assertEqual(failed, 2)

    def test_bad_fixture_rejected(self):
        import tempfile
        bad = ['[[check]]\ncheck_id = "bad"\ntable = "t"\nquery = "q"\nexpected = "0"\n',
               '[[check]]\ncheck_id = "A01_x"\ntable = "t; drop"\nquery = "q"\nexpected = "0"\n',
               '[[check]]\ncheck_id = "A01_x"\ntable = "t"\nquery = "q; delete from x"\nexpected = "0"\n',
               '[[check]]\ncheck_id = "A01_x"\ntable = "t"\nquery = "q"\nexpected = ">= x"\n']
        for text in bad:
            with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
                fh.write(text)
            try:
                with self.assertRaises(ValueError, msg=text):
                    t.load_fixture(fh.name)
            finally:
                os.unlink(fh.name)


if __name__ == "__main__":
    unittest.main()
