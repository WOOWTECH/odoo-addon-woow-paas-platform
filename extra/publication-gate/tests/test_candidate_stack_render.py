import importlib.util
from pathlib import Path
import unittest


PATH = Path(__file__).parents[1] / "deploy" / "render_candidate_stack.py"
SPEC = importlib.util.spec_from_file_location("candidate_stack_render", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CandidateStackRenderTest(unittest.TestCase):
    def config(self):
        return {
            "mode": "test-only", "namespace": "b38-test-only",
            "owner_uid": "test-owner", "ha_deployment": "owned-ha",
            "ha_container": "home-assistant",
            "gate_image": "registry.test/gate@sha256:" + "a" * 64,
            "authority_image": "registry.test/odoo@sha256:" + "b" * 64,
            "source_pvc": "test-source", "state_pvc": "test-state",
            "identity_secret": "test-identity", "odoo_config_secret": "test-odoo",
            "database": "owned_test", "actor_uid": 48,
            "canonical_name": "test-canonical", "resource_identity": "test:scope",
            "installation_id": "test-origin", "issuer": "https://test.invalid",
            "audience": "test-audience", "authority_server_sha256": "c" * 64,
            "gate_client_sha256": "d" * 64,
            "gate_source_sha256": "e" * 64,
            "gate_requirements_sha256": "f" * 64,
        }

    def test_sidecar_loopback_and_private_authority_are_structurally_bound(self):
        output = MODULE.render(self.config())
        items = output["items"]
        patch = [x for x in items if x["kind"] == "Deployment" and x["metadata"]["name"] == "owned-ha"][0]
        names = [x["name"] for x in patch["spec"]["template"]["spec"]["containers"]]
        self.assertEqual(names, ["home-assistant", "publication-gate"])
        pod = patch["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"], {
            "fsGroup": 65532, "fsGroupChangePolicy": "OnRootMismatch"})
        gate = pod["containers"][1]
        self.assertEqual(gate["securityContext"]["runAsUser"], 65532)
        self.assertEqual(gate["securityContext"]["runAsGroup"], 65532)
        identity = [v for v in pod["volumes"] if v["name"] == "publication-identity"][0]
        self.assertEqual(identity["secret"]["defaultMode"], 0o440)
        cm = [x for x in items if x["kind"] == "ConfigMap"][0]
        self.assertIn('"upstream":"http://127.0.0.1:8123/manifest.json"', cm["data"]["gate.json"])
        authority = [x for x in items if x["kind"] == "Deployment" and x["metadata"]["name"].endswith("authority")][0]
        authority_pod = authority["spec"]["template"]["spec"]
        self.assertFalse(authority_pod["automountServiceAccountToken"])
        self.assertEqual(authority_pod["securityContext"]["fsGroup"], 100)
        authority_container = authority_pod["containers"][0]
        self.assertEqual(authority_container["securityContext"]["runAsUser"], 100)
        self.assertEqual(authority_container["securityContext"]["runAsGroup"], 100)
        projected = [v for v in authority_pod["volumes"] if v["name"] == "identity"][0]
        self.assertEqual(projected["projected"]["defaultMode"], 0o440)
        self.assertEqual(authority_container["command"],
                         ["python", "/candidate/scripts/rev01_authority_server.py"])
        self.assertIn("publication_gate and locked dependencies",
                      output["x-b38"]["gate_image_contract"])
        self.assertEqual(output["x-b38"]["mode"], "test-only")
        self.assertFalse(output["x-b38"]["apply"])

    def test_secret_values_are_not_an_input_or_output(self):
        output = MODULE.render(self.config())
        self.assertNotIn("kind': 'Secret", str(output))
        value = self.config()
        value["private_key"] = "forbidden"
        with self.assertRaisesRegex(ValueError, "exact candidate"):
            MODULE.render(value)

    def test_missing_authorized_metadata_fails_fast(self):
        for key in ("audience", "authority_server_sha256", "canonical_name", "actor_uid",
                    "gate_source_sha256", "gate_requirements_sha256"):
            with self.subTest(key=key):
                value = self.config()
                del value[key]
                with self.assertRaisesRegex(ValueError, "missing="):
                    MODULE.render(value)

    def test_live_mode_rejects_test_values(self):
        value = self.config()
        value["mode"] = "live"
        with self.assertRaisesRegex(ValueError, "forbidden in live"):
            MODULE.render(value)


if __name__ == "__main__":
    unittest.main()
