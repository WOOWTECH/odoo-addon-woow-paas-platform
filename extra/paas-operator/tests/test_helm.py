"""Tests for Helm service."""
import json
import subprocess
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.services.helm import HelmException, HelmService, KubernetesService, validate_namespace


class TestHelmService:
    """Test cases for HelmService."""

    @pytest.fixture
    def helm_service(self):
        """Create HelmService instance."""
        return HelmService()

    def test_validate_namespace_valid(self, helm_service):
        """Test namespace validation with valid prefix."""
        # Should not raise
        validate_namespace("paas-ws-test")
        validate_namespace("paas-ws-123")

    def test_validate_namespace_invalid(self, helm_service):
        """Test namespace validation with invalid prefix."""
        with pytest.raises(ValueError, match="must start with"):
            validate_namespace("invalid-namespace")

        with pytest.raises(ValueError, match="must start with"):
            validate_namespace("test")

    @patch("subprocess.run")
    def test_run_command_success(self, mock_run, helm_service):
        """Test successful command execution."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout='{"test": "data"}',
            stderr="",
        )

        result = helm_service._run_command(["version"])

        assert result.returncode == 0
        assert result.stdout == '{"test": "data"}'
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_run_command_failure(self, mock_run, helm_service):
        """Test command execution failure."""
        mock_run.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="Error: command failed",
        )

        with pytest.raises(HelmException) as exc_info:
            helm_service._run_command(["invalid"])

        assert "failed with code 1" in exc_info.value.message
        assert exc_info.value.stderr == "Error: command failed"

    @patch("subprocess.run")
    def test_run_command_timeout(self, mock_run, helm_service):
        """Test command timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["helm", "test"],
            timeout=300,
        )

        with pytest.raises(HelmException) as exc_info:
            helm_service._run_command(["test"])

        assert "timed out" in exc_info.value.message

    @patch("subprocess.run")
    def test_install_success(self, mock_run, helm_service):
        """Test successful Helm install."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "test-release",
                "namespace": "paas-ws-test",
                "info": {
                    "status": "deployed",
                    "revision": 1,
                    "last_deployed": "2024-01-01T00:00:00Z",
                },
                "chart": {
                    "metadata": {
                        "name": "nginx",
                        "appVersion": "1.0.0",
                    }
                },
            }),
            stderr="",
        )

        result = helm_service.install(
            namespace="paas-ws-test",
            name="test-release",
            chart="nginx",
        )

        assert result.name == "test-release"
        assert result.namespace == "paas-ws-test"
        assert result.chart == "nginx"

    @patch("subprocess.run")
    def test_install_invalid_namespace(self, mock_run, helm_service):
        """Test install with invalid namespace."""
        with pytest.raises(ValueError):
            helm_service.install(
                namespace="invalid",
                name="test",
                chart="nginx",
            )

        # Should not call subprocess
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_install_with_repo_and_version_adds_flags(self, mock_run, helm_service):
        """install() with a classic Helm repo URL appends --repo and --version.

        agent-runtime path (design gap C): the chart lives in a private Gitea
        classic Helm repo, fetched statelessly via --repo (no `helm repo add`).
        """
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "agent-pa-abc",
                "namespace": "paas-ws-pa-abc",
                "info": {"status": "deployed", "revision": 1, "last_deployed": ""},
                "chart": {"metadata": {"name": "agent-runtime", "appVersion": "1.0.0"}},
            }),
            stderr="",
        )

        helm_service.install(
            namespace="paas-ws-pa-abc",
            name="agent-pa-abc",
            chart="agent-runtime",
            repo="http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm",
            version="0.1.0",
        )

        cmd = mock_run.call_args[0][0]
        assert "--repo" in cmd
        assert cmd[cmd.index("--repo") + 1] == (
            "http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm"
        )
        assert "--version" in cmd
        assert cmd[cmd.index("--version") + 1] == "0.1.0"

    @patch("subprocess.run")
    def test_install_with_repo_credentials_adds_auth_flags(self, mock_run, helm_service):
        """install() with repo credentials appends --username and --password.

        woow-paas is a private Gitea org: anonymous index.yaml pull = 401, so a
        read:package token must be passed through to helm.
        """
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "agent-pa-abc",
                "namespace": "paas-ws-pa-abc",
                "info": {"status": "deployed", "revision": 1, "last_deployed": ""},
                "chart": {"metadata": {"name": "agent-runtime", "appVersion": "1.0.0"}},
            }),
            stderr="",
        )

        helm_service.install(
            namespace="paas-ws-pa-abc",
            name="agent-pa-abc",
            chart="agent-runtime",
            repo="http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm",
            username="ci-bot",
            password="gitea-token",
        )

        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--username") + 1] == "ci-bot"
        assert cmd[cmd.index("--password") + 1] == "gitea-token"

    @patch("src.services.helm.settings")
    @patch("subprocess.run")
    def test_install_private_oci_chart_logs_in_to_registry_first(self, mock_run, mock_settings):
        """Private OCI chart install() must `helm registry login` before pull.

        GA blocker (stg layer-3 e2e 2026-06-30): operator helm pulled
        oci://jcr-prod.../mcp-server anonymously -> 401. OCI auth (registry
        login) != classic-repo --username/--password (ignored for oci:// refs).
        Reuses the tenant-imagePullSecret creds (settings.registry_*).
        """
        mock_settings.helm_binary = "/usr/local/bin/helm"
        mock_settings.helm_timeout = 300
        mock_settings.namespace_prefix = "paas-ws-"
        mock_settings.registry_username = "ci-bot"
        mock_settings.registry_password = "jcr-token"
        mock_settings.registry_server = "jcr-prod.woowtech.io"
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "svc-x", "namespace": "paas-ws-test",
                "info": {"status": "deployed", "revision": 1, "last_deployed": ""},
                "chart": {"metadata": {"name": "mcp-server", "appVersion": "9d10944"}},
            }),
            stderr="",
        )

        svc = HelmService()
        svc.install(
            namespace="paas-ws-test",
            name="svc-x",
            chart="oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server",
            version="0.1.1",
        )

        # First subprocess call: `helm registry login <host>` (before install)
        first_cmd = mock_run.call_args_list[0][0][0]
        assert "registry" in first_cmd and "login" in first_cmd
        assert "jcr-prod.woowtech.io" in first_cmd
        assert "--password-stdin" in first_cmd  # password via stdin, never argv
        assert "jcr-token" not in first_cmd     # password NOT in argv
        # Password passed as a *str* via stdin (input kwarg), NOT pre-encoded:
        # text=True makes subprocess encode it; a pre-encoded bytes input would
        # crash in subprocess._save_input (bytes.encode). Regression guard.
        assert mock_run.call_args_list[0].kwargs.get("input") == "jcr-token"
        assert not isinstance(mock_run.call_args_list[0].kwargs.get("input"), bytes)
        # Last call: the actual install
        install_cmd = mock_run.call_args_list[-1][0][0]
        assert "install" in install_cmd
        assert "oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server" in install_cmd

    @patch("src.services.helm.settings")
    @patch("subprocess.run")
    def test_install_public_oci_chart_skips_registry_login(self, mock_run, mock_settings):
        """Public OCI chart (no registry creds) skips login — bitnami unchanged."""
        mock_settings.helm_binary = "/usr/local/bin/helm"
        mock_settings.helm_timeout = 300
        mock_settings.namespace_prefix = "paas-ws-"
        mock_settings.registry_username = ""
        mock_settings.registry_password = ""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "pg", "namespace": "paas-ws-test",
                "info": {"status": "deployed", "revision": 1, "last_deployed": ""},
                "chart": {"metadata": {"name": "postgresql", "appVersion": "16"}},
            }),
            stderr="",
        )

        svc = HelmService()
        svc.install(
            namespace="paas-ws-test",
            name="pg",
            chart="oci://registry-1.docker.io/bitnamicharts/postgresql",
            version="16.0.0",
        )

        # No registry login call — only the install
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert not ("registry" in cmd and "login" in cmd)

    @patch("src.services.helm.settings")
    @patch("subprocess.run")
    def test_install_third_party_oci_chart_skips_registry_login(self, mock_run, mock_settings):
        """Third-party public OCI chart must stay anonymous even when the operator
        HAS its own registry creds configured.

        Regression (stg live e2e 2026-07-01): _ensure_oci_login logged into ANY
        oci:// host with the JCR creds (settings.registry_*) whenever creds were
        present, missing a host==registry_server guard. For a third-party public
        chart (oci://8gears.container-registry.com/library/n8n — pullable
        anonymously) that meant `helm registry login 8gears... --username woow-prod`
        with the wrong creds, which then made the subsequent pull fail
        `401 unauthorized`. The n8n full-app service could never deploy. Only the
        operator's OWN registry (registry_server) should get a login.
        """
        mock_settings.helm_binary = "/usr/local/bin/helm"
        mock_settings.helm_timeout = 300
        mock_settings.namespace_prefix = "paas-ws-"
        mock_settings.registry_username = "woow-prod"
        mock_settings.registry_password = "jcr-token"
        mock_settings.registry_server = "jcr-prod.woowtech.io"
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "svc-n8n", "namespace": "paas-ws-test",
                "info": {"status": "deployed", "revision": 1, "last_deployed": ""},
                "chart": {"metadata": {"name": "n8n", "appVersion": "1.0"}},
            }),
            stderr="",
        )

        svc = HelmService()
        svc.install(
            namespace="paas-ws-test",
            name="svc-n8n",
            chart="oci://8gears.container-registry.com/library/n8n",
            version="2.0.1",
        )

        # No `helm registry login` for a host that isn't the operator's own registry.
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert not ("registry" in cmd and "login" in cmd), (
                "must not log into a third-party OCI registry with JCR creds"
            )
        # The install itself must still run (anonymous pull).
        install_cmd = mock_run.call_args_list[-1][0][0]
        assert "install" in install_cmd
        assert "oci://8gears.container-registry.com/library/n8n" in install_cmd

    @patch("subprocess.run")
    def test_install_without_repo_is_backward_compatible(self, mock_run, helm_service):
        """install() without repo/credentials must NOT emit --repo/--username/--password.

        Guards the existing marketplace path (OCI refs like
        oci://registry-1.docker.io/bitnamicharts/postgresql) against regression.
        """
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "test-release",
                "namespace": "paas-ws-test",
                "info": {"status": "deployed", "revision": 1, "last_deployed": ""},
                "chart": {"metadata": {"name": "postgresql", "appVersion": "16"}},
            }),
            stderr="",
        )

        helm_service.install(
            namespace="paas-ws-test",
            name="test-release",
            chart="oci://registry-1.docker.io/bitnamicharts/postgresql",
        )

        cmd = mock_run.call_args[0][0]
        assert "--repo" not in cmd
        assert "--username" not in cmd
        assert "--password" not in cmd

    @patch("subprocess.run")
    def test_upgrade_with_repo_and_version_adds_flags(self, mock_run, helm_service):
        """upgrade() with a classic Helm repo URL appends --repo and --version.

        sync-workspaces re-renders the NetworkPolicy egress allowlist via helm
        upgrade and must resolve the same private-repo chart as install.
        """
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "agent-pa-abc",
                "namespace": "paas-ws-pa-abc",
                "info": {"status": "deployed", "revision": 2, "last_deployed": ""},
                "chart": {"metadata": {"name": "agent-runtime", "appVersion": "1.0.0"}},
            }),
            stderr="",
        )

        helm_service.upgrade(
            namespace="paas-ws-pa-abc",
            name="agent-pa-abc",
            chart="agent-runtime",
            repo="http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm",
            version="0.1.0",
        )

        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--repo") + 1] == (
            "http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm"
        )
        assert cmd[cmd.index("--version") + 1] == "0.1.0"

    @patch("subprocess.run")
    def test_upgrade_without_repo_is_backward_compatible(self, mock_run, helm_service):
        """upgrade() without repo/credentials must NOT emit --repo/--username/--password."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "test-release",
                "namespace": "paas-ws-test",
                "info": {"status": "deployed", "revision": 2, "last_deployed": ""},
                "chart": {"metadata": {"name": "postgresql", "appVersion": "16"}},
            }),
            stderr="",
        )

        helm_service.upgrade(
            namespace="paas-ws-test",
            name="test-release",
            chart="oci://registry-1.docker.io/bitnamicharts/postgresql",
        )

        cmd = mock_run.call_args[0][0]
        assert "--repo" not in cmd
        assert "--username" not in cmd
        assert "--password" not in cmd

    @patch("subprocess.run")
    def test_get_release(self, mock_run, helm_service):
        """Test get release info."""
        # helm list returns a JSON array, not a single object
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps([{
                "name": "test-release",
                "namespace": "paas-ws-test",
                "status": "deployed",
                "revision": "2",
                "updated": "2024-01-01 00:00:00.000000000 +0000 UTC",
                "chart": "nginx-1.0.0",
                "app_version": "1.0.0",
            }]),
            stderr="",
        )

        result = helm_service.get("paas-ws-test", "test-release")

        assert result.name == "test-release"
        assert result.revision == 2

    @patch("subprocess.run")
    def test_uninstall(self, mock_run, helm_service):
        """Test uninstall release."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="release 'test-release' uninstalled",
            stderr="",
        )

        result = helm_service.uninstall("paas-ws-test", "test-release")

        assert "uninstalled" in result["message"]

    @patch("subprocess.run")
    def test_history(self, mock_run, helm_service):
        """Test get release history."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps([
                {
                    "revision": 1,
                    "updated": "2024-01-01T00:00:00Z",
                    "status": "superseded",
                    "chart": "nginx-1.0.0",
                    "app_version": "1.0.0",
                    "description": "Install complete",
                },
                {
                    "revision": 2,
                    "updated": "2024-01-02T00:00:00Z",
                    "status": "deployed",
                    "chart": "nginx-1.0.1",
                    "app_version": "1.0.1",
                    "description": "Upgrade complete",
                },
            ]),
            stderr="",
        )

        result = helm_service.history("paas-ws-test", "test-release")

        assert len(result) == 2
        assert result[0].revision == 1
        assert result[1].revision == 2
        assert result[1].status.value == "deployed"

    @patch("subprocess.run")
    def test_get_version(self, mock_run, helm_service):
        """Test get Helm version."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="v3.13.3+g3ff1c00",
            stderr="",
        )

        result = helm_service.get_version()

        assert "v3.13" in result


class TestCommandRedaction:
    """issue #29: credential values must be redacted before they reach the
    'Executing:' log line / HelmException.command — never persist secrets in
    operator stdout. The real subprocess still receives the true values."""

    def test_redact_masks_helm_password_and_username(self):
        from src.services.helm import _redact_cmd
        joined = " ".join(_redact_cmd([
            "helm", "install", "x", "--repo", "https://r",
            "--username", "eugene", "--password", "deadbeef40hextoken",
        ]))
        assert "deadbeef40hextoken" not in joined
        assert "eugene" not in joined
        assert "--password <redacted>" in joined
        assert "--username <redacted>" in joined
        assert "--repo https://r" in joined  # non-secret preserved

    def test_redact_masks_equals_form(self):
        from src.services.helm import _redact_cmd
        joined = " ".join(_redact_cmd(["helm", "install", "--password=secrettok"]))
        assert "secrettok" not in joined
        assert "--password=<redacted>" in joined

    def test_redact_masks_kubectl_set_env_token(self):
        # issue #29 audit: resume injects the signed SKILL_BUNDLE_TOKEN via
        # `kubectl set env`, also logged through _run_command.
        from src.services.helm import _redact_cmd
        joined = " ".join(_redact_cmd([
            "kubectl", "set", "env", "deployment/x", "--namespace", "paas-ws-x",
            "SKILL_BUNDLE_TOKEN=signed.jwt.value", "PROJECT_HASH=abc",
        ]))
        assert "signed.jwt.value" not in joined
        assert "SKILL_BUNDLE_TOKEN=<redacted>" in joined
        assert "PROJECT_HASH=abc" in joined  # non-secret env preserved

    def test_redact_masks_set_secret_key(self):
        from src.services.helm import _redact_cmd
        joined = " ".join(_redact_cmd([
            "helm", "install", "--set", "auth.currentKey=topsecret",
            "--set", "image.tag=1.4.0",
        ]))
        assert "topsecret" not in joined
        assert "auth.currentKey=<redacted>" in joined
        assert "image.tag=1.4.0" in joined  # non-secret --set preserved

    def test_redact_preserves_non_secrets(self):
        from src.services.helm import _redact_cmd
        cmd = ["helm", "install", "rel", "agent-runtime", "--namespace",
               "paas-ws-x", "--version", "0.4.0", "--values", "/tmp/abc.yaml",
               "--output", "json"]
        assert _redact_cmd(cmd) == cmd

    @patch("subprocess.run")
    def test_run_command_logs_redacted_but_subprocess_gets_real(self, mock_run, caplog):
        import logging
        from src.services.helm import HelmService
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="{}", stderr="")
        with caplog.at_level(logging.INFO):
            HelmService()._run_command(["install", "x", "--password", "TOPSECRETTOKEN"])
        # log is redacted
        assert "TOPSECRETTOKEN" not in caplog.text
        assert "<redacted>" in caplog.text
        # subprocess still received the real token (functionality intact)
        assert "TOPSECRETTOKEN" in mock_run.call_args[0][0]


class TestKubernetesService:
    """Test cases for KubernetesService."""

    @pytest.fixture
    def k8s_service(self):
        """Create KubernetesService instance."""
        return KubernetesService()

    @patch("subprocess.run")
    def test_get_pods(self, mock_run, k8s_service):
        """Test get pods in namespace."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "items": [
                    {
                        "metadata": {
                            "name": "test-pod-1",
                            "creationTimestamp": "2024-01-01T00:00:00Z",
                        },
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "ready": True,
                                    "restartCount": 0,
                                }
                            ],
                        },
                    }
                ]
            }),
            stderr="",
        )

        result = k8s_service.get_pods("paas-ws-test")

        assert len(result) == 1
        assert result[0].name == "test-pod-1"
        assert result[0].phase.value == "Running"
        assert result[0].ready == "1/1"
        assert result[0].restarts == 0

    @patch("subprocess.run")
    def test_create_namespace(self, mock_run, k8s_service):
        """Test create namespace with quota."""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

        result = k8s_service.create_namespace(
            name="paas-ws-new",
            cpu_limit="2",
            memory_limit="4Gi",
            storage_limit="20Gi",
        )

        assert "created" in result["message"]
        # Should be called twice (namespace + quota)
        assert mock_run.call_count == 2

    def test_create_namespace_invalid_prefix(self, k8s_service):
        """Test create namespace with invalid prefix."""
        with pytest.raises(ValueError):
            k8s_service.create_namespace(
                name="invalid",
                cpu_limit="2",
                memory_limit="4Gi",
                storage_limit="20Gi",
            )

    @patch("subprocess.run")
    def test_apply_manifest(self, mock_run, k8s_service):
        """Test apply a K8s manifest via kubectl apply."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "kind": "Service",
                "metadata": {"name": "test-mcp", "namespace": "paas-ws-test"},
            }),
            stderr="",
        )

        manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "test-mcp",
                "namespace": "paas-ws-test",
            },
            "spec": {
                "type": "ClusterIP",
                "ports": [{"port": 3000, "targetPort": 3000}],
            },
        }

        result = k8s_service.apply_manifest(manifest)

        assert result["kind"] == "Service"
        assert result["metadata"]["name"] == "test-mcp"
        # Verify kubectl apply was called
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "apply" in cmd_args
        assert "-f" in cmd_args

    @patch("subprocess.run")
    def test_patch_configmap_merge_patches_data_only(self, mock_run, k8s_service):
        """patch_configmap merge-patches ONLY the data field so the chart-created
        ConfigMap's Helm ownership metadata (managed-by:Helm + meta.helm.sh/*) is
        preserved — a full kubectl apply would strip it and break helm upgrade
        (ownership conflict)."""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

        k8s_service.patch_configmap(
            "paas-ws-pa-abc123", "agent-pa-abc123-skills", {"skills.json": "[]"},
        )

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "patch" in cmd and "configmap" in cmd
        assert "agent-pa-abc123-skills" in cmd
        assert "--type=merge" in cmd
        patch_arg = next(a for a in cmd if a.startswith("--patch="))
        body = json.loads(patch_arg[len("--patch="):])
        assert body == {"data": {"skills.json": "[]"}}, (
            "patch body must touch ONLY data (no metadata/labels/annotations)"
        )

    @patch("subprocess.run")
    def test_delete_service(self, mock_run, k8s_service):
        """Test delete a K8s Service."""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

        result = k8s_service.delete_service("paas-ws-test", "test-mcp")

        assert result is True
        cmd_args = mock_run.call_args[0][0]
        assert "delete" in cmd_args
        assert "service" in cmd_args
        assert "test-mcp" in cmd_args
        assert "--ignore-not-found" in cmd_args

    def test_delete_service_invalid_namespace(self, k8s_service):
        """Test delete service with invalid namespace."""
        with pytest.raises(ValueError):
            k8s_service.delete_service("invalid", "test-mcp")

    @patch("subprocess.run")
    def test_delete_namespace(self, mock_run, k8s_service):
        """Test delete a namespace (two-step teardown §8.4.1 step 2).

        Fire-and-return (--wait=false, brainstorm decision #4): K8s marks the
        namespace Terminating and reaps contained objects (PVC / Secret /
        NetworkPolicy / LLM Secret `{release}-llm`) in the background.
        Idempotent via --ignore-not-found.
        """
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

        result = k8s_service.delete_namespace("paas-ws-test")

        assert result is True
        cmd_args = mock_run.call_args[0][0]
        assert "delete" in cmd_args
        assert "namespace" in cmd_args
        assert "paas-ws-test" in cmd_args
        assert "--ignore-not-found" in cmd_args
        assert "--wait=false" in cmd_args

    def test_delete_namespace_invalid_namespace(self, k8s_service):
        """Test delete namespace with invalid name raises before any kubectl."""
        with pytest.raises(ValueError):
            k8s_service.delete_namespace("invalid")

    @patch("subprocess.run")
    def test_label_pvcs_rejects_invalid_label_value(self, mock_run, k8s_service):
        """label_pvcs must fail loud on a value kubectl would reject — not build
        a doomed atomic `kubectl label` whose failure is swallowed as a warning.

        Regression for operator #208: a ':'-bearing RFC3339 value made the whole
        label command fail (all three labels dropped); the failure was double-
        swallowed (helm.py per-PVC warning + releases.py outer try/except), so
        the broken GC went unnoticed for 74 days. Validating in pure Python
        moves detection from 'real kubectl only' to a unit-catchable boundary.
        """
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        with pytest.raises(ValueError, match="label value"):
            k8s_service.label_pvcs(
                "paas-ws-test",
                ["data-x-postgresql-0"],
                {"paas.woowtech.io/orphaned-at": "2026-06-20T08:03:00Z"},
            )
        # Reject BEFORE shelling out: no doomed kubectl command is attempted.
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_label_pvcs_applies_valid_labels(self, mock_run, k8s_service):
        """A valid (colon-free) label batch is applied via one kubectl call."""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        k8s_service.label_pvcs(
            "paas-ws-test",
            ["data-x-postgresql-0"],
            {
                "paas.woowtech.io/orphaned-at": "1750406580",
                "paas.woowtech.io/release-name": "svc-deadbeef",
                "paas.woowtech.io/grace-period-days": "7",
            },
        )
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "label" in cmd and "pvc" in cmd and "data-x-postgresql-0" in cmd
        assert "paas.woowtech.io/orphaned-at=1750406580" in cmd


class TestManagedNsLabels:
    """L2: build_managed_ns_labels — 平台管理 namespace 的 env label（PAAS_ENV）。

    單一 source（helm.build_managed_ns_labels），create_namespace 與
    agent_runtimes._ensure_namespace 都由它取 labels → 兩注入點 lock-step。
    """

    def test_omits_env_label_when_paas_env_empty(self, monkeypatch):
        """PAAS_ENV 空 → 只有 paas-managed（dev/unknown ns 不帶 env label）。"""
        from src.services.helm import build_managed_ns_labels
        monkeypatch.setattr("src.config.settings.paas_env", "")
        assert build_managed_ns_labels() == {"paas-managed": "true"}

    def test_includes_env_label_when_paas_env_set(self, monkeypatch):
        """PAAS_ENV=stg → 加 paas.woowtech.io/env=stg（值=env，可 kubectl -l 查）。"""
        from src.services.helm import build_managed_ns_labels
        monkeypatch.setattr("src.config.settings.paas_env", "stg")
        labels = build_managed_ns_labels()
        assert labels["paas-managed"] == "true"
        assert labels["paas.woowtech.io/env"] == "stg"

    def test_validates_env_label_value_before_use(self, monkeypatch):
        """非法值（raw set_param 繞 Settings 白名單）→ pure-Python fail loud（#208 類）。"""
        from src.services.helm import build_managed_ns_labels
        monkeypatch.setattr("src.config.settings.paas_env", "paas-ws-stg-")
        with pytest.raises(ValueError):
            build_managed_ns_labels()

    @staticmethod
    def _capture_ns_labels(monkeypatch):
        """攔 create_namespace 真正寫進 ns manifest 的 labels（捕 dict，非 spy）。

        create_namespace 內 `import yaml; yaml.safe_dump(ns_manifest, f)` —— patch
        yaml 模組的 safe_dump 即可在不落檔下捕到 manifest。比 spy 強：能擋「呼叫了
        build_managed_ns_labels 但丟棄回傳、改硬編 labels」的 mutation。
        """
        import yaml as yaml_mod
        captured = {}
        orig = yaml_mod.safe_dump

        def cap(obj, *a, **k):
            if isinstance(obj, dict) and obj.get("kind") == "Namespace":
                captured["labels"] = obj["metadata"]["labels"]
            return orig(obj, *a, **k)

        monkeypatch.setattr(yaml_mod, "safe_dump", cap)
        return captured

    @patch("subprocess.run")
    def test_create_namespace_stamps_env_label_in_manifest(self, mock_run, monkeypatch):
        """PAAS_ENV=stg → ns manifest 真帶 {paas-managed, env=stg}（捕 manifest，擋 mutation）。"""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("src.config.settings.paas_env", "stg")
        captured = self._capture_ns_labels(monkeypatch)
        KubernetesService().create_namespace(
            name="paas-ws-stg-x", cpu_limit="1", memory_limit="1Gi", storage_limit="5Gi")
        assert captured["labels"] == {
            "paas-managed": "true", "paas.woowtech.io/env": "stg"}

    @patch("subprocess.run")
    def test_create_namespace_omits_env_label_in_manifest_when_empty(self, mock_run, monkeypatch):
        """PAAS_ENV 空 → ns manifest 只帶 paas-managed（不貼空 env value）。"""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("src.config.settings.paas_env", "")
        captured = self._capture_ns_labels(monkeypatch)
        KubernetesService().create_namespace(
            name="paas-ws-x", cpu_limit="1", memory_limit="1Gi", storage_limit="5Gi")
        assert captured["labels"] == {"paas-managed": "true"}


class TestTenantIsolationLabels:
    """L2: #279 租戶隔離 label — build_managed_ns_labels 的 workspace-id / reach-ws。

    CS ns 帶 woow.tw/workspace-id=<W>（leg-1 egress + leg-2 baseline 都比對）；agent
    ns 帶多把 woow.tw/reach-ws-<W>=true（Model B 可同時可達多 workspace）。二者必與
    env label 共存（F6：workspace int id per-DB 非叢集唯一、prod/stg 同叢集，selector
    靠 env 分環境，否則 stg agent 達 prod CS）。value 全 str()（F9：egressWorkspaceIds
    是 List[int]，label value 必須字串）。
    """

    def test_workspace_id_label_is_str_cast(self, monkeypatch):
        """workspace_id=5 (int) → woow.tw/workspace-id 值為字串 "5"（非 int，F9）。"""
        from src.services.helm import build_managed_ns_labels, WORKSPACE_ID_LABEL
        monkeypatch.setattr("src.config.settings.paas_env", "")
        labels = build_managed_ns_labels(workspace_id=5)
        assert labels[WORKSPACE_ID_LABEL] == "5"
        assert isinstance(labels[WORKSPACE_ID_LABEL], str)

    def test_reach_ws_labels_one_per_workspace(self, monkeypatch):
        """Model B：reach_workspace_ids=[3,5] → 兩把 reach-ws label（跨 workspace 可達集合）。"""
        from src.services.helm import build_managed_ns_labels
        monkeypatch.setattr("src.config.settings.paas_env", "")
        labels = build_managed_ns_labels(reach_workspace_ids=[3, 5])
        assert labels["woow.tw/reach-ws-3"] == "true"
        assert labels["woow.tw/reach-ws-5"] == "true"

    def test_isolation_labels_coexist_with_env(self, monkeypatch):
        """F6：#279 label 與 env label 一定共存（selector 靠 env 分 prod/stg，缺 env 即跨環境洩漏）。"""
        from src.services.helm import build_managed_ns_labels
        monkeypatch.setattr("src.config.settings.paas_env", "stg")
        labels = build_managed_ns_labels(workspace_id=5, reach_workspace_ids=[5])
        assert labels["paas.woowtech.io/env"] == "stg"
        assert labels["woow.tw/workspace-id"] == "5"
        assert labels["woow.tw/reach-ws-5"] == "true"

    def test_no_isolation_labels_when_params_omitted(self, monkeypatch):
        """向後相容：不傳 #279 參數 → 行為與舊版一致（只有 paas-managed）。"""
        from src.services.helm import build_managed_ns_labels
        monkeypatch.setattr("src.config.settings.paas_env", "")
        assert build_managed_ns_labels() == {"paas-managed": "true"}

    def test_reach_ws_label_key_format(self):
        """單一真源：reach_ws_label_key(42) == 'woow.tw/reach-ws-42'（跨 repo byte-exact 契約）。"""
        from src.services.helm import reach_ws_label_key, REACH_WS_LABEL_PREFIX
        assert reach_ws_label_key(42) == f"{REACH_WS_LABEL_PREFIX}42"
        assert reach_ws_label_key(42) == "woow.tw/reach-ws-42"

    def test_validate_label_key_accepts_isolation_keys(self):
        """好 key 過關（woow.tw/workspace-id、reach-ws-5）。"""
        from src.services.helm import validate_label_key
        validate_label_key("woow.tw/workspace-id")
        validate_label_key("woow.tw/reach-ws-5")

    def test_validate_label_key_rejects_bad_key(self):
        """F4：非法 key pure-Python fail loud（#208 類），避免 selector 靜默 match nothing = deny-all。"""
        from src.services.helm import validate_label_key
        with pytest.raises(ValueError):
            validate_label_key("woow.tw/reach ws 5")   # 空白非法
        with pytest.raises(ValueError):
            validate_label_key("woow.tw/" + "x" * 64)  # name >63
        with pytest.raises(ValueError):
            validate_label_key("woow.tw/-bad")         # name 不得 '-' 起頭

    @patch("subprocess.run")
    def test_create_namespace_stamps_workspace_id_in_manifest(self, mock_run, monkeypatch):
        """CS ns：create_namespace(workspace_id=7) → manifest 真帶 workspace-id=7 + env（捕 manifest 擋 mutation）。"""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("src.config.settings.paas_env", "stg")
        captured = TestManagedNsLabels._capture_ns_labels(monkeypatch)
        KubernetesService().create_namespace(
            name="paas-ws-stg-x", cpu_limit="1", memory_limit="1Gi",
            storage_limit="5Gi", workspace_id=7)
        assert captured["labels"] == {
            "paas-managed": "true",
            "paas.woowtech.io/env": "stg",
            "woow.tw/workspace-id": "7",
        }
