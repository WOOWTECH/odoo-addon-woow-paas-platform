"""Tests for Helm service."""
import json
import subprocess
import sys
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.services.helm import (
    HelmException,
    HelmService,
    KubectlException,
    KubernetesService,
    build_managed_ns_labels,
    validate_namespace,
)


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

    def test_validate_namespace_rejects_empty_prefix(self, monkeypatch):
        """空 namespace_prefix 使 startswith('') 恆真 → 隔離靜默失效（唯一 app-layer
        邊界被移除）。validate_namespace 必須 fail-closed，而非放行任何 ns（#284 W0
        覆核揪出的 config 脆弱性）。"""
        monkeypatch.setattr("src.config.settings.namespace_prefix", "")
        with pytest.raises(ValueError, match="empty"):
            validate_namespace("paas-ws-anything")

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
    def test_install_adds_pvc_keep_post_renderer(self, mock_run, helm_service):
        """#317: install must run the PVC-keep post-renderer so chart-owned PVCs
        get helm.sh/resource-policy: keep baked into the STORED manifest (helm
        uninstall reads the manifest, not live objects — so post-teardown labeling
        alone can't save chart-owned PVC data). Invoked via the operator's own
        python + --post-renderer-args so PyYAML is always present."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "r", "namespace": "paas-ws-test",
                "info": {"status": "deployed", "revision": 1, "last_deployed": ""},
                "chart": {"metadata": {"name": "n8n", "appVersion": "1.0.0"}},
            }),
            stderr="",
        )
        helm_service.install(namespace="paas-ws-test", name="r", chart="n8n")
        cmd = mock_run.call_args[0][0]
        assert "--post-renderer" in cmd
        assert cmd[cmd.index("--post-renderer") + 1] == sys.executable
        assert "--post-renderer-args" in cmd
        assert cmd[cmd.index("--post-renderer-args") + 1].endswith(
            "pvc_keep_post_renderer.py"
        )

    @patch("subprocess.run")
    def test_upgrade_adds_pvc_keep_post_renderer(self, mock_run, helm_service):
        """#317: upgrade must ALSO run the post-renderer — it rewrites the stored
        manifest, so without it an upgrade drops keep and the next uninstall
        deletes the PVC (regression). This is also the backfill path for existing
        releases."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "r", "namespace": "paas-ws-test",
                "info": {"status": "deployed", "revision": 2, "last_deployed": ""},
                "chart": {"metadata": {"name": "n8n", "appVersion": "1.0.0"}},
            }),
            stderr="",
        )
        helm_service.upgrade(namespace="paas-ws-test", name="r", chart="n8n")
        cmd = mock_run.call_args[0][0]
        assert "--post-renderer" in cmd
        assert cmd[cmd.index("--post-renderer") + 1] == sys.executable
        assert cmd[cmd.index("--post-renderer-args") + 1].endswith(
            "pvc_keep_post_renderer.py"
        )

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

    # ── #461: per-call timeout plumbing ────────────────────────────────────

    @staticmethod
    def _upgrade_stdout():
        return json.dumps({
            "name": "test-release",
            "namespace": "paas-ws-test",
            "info": {"status": "deployed", "revision": 2, "last_deployed": ""},
            "chart": {"metadata": {"name": "mcp-server", "appVersion": "1.0.0"}},
        })

    @patch("subprocess.run")
    def test_upgrade_without_timeouts_is_behaviour_neutral(self, mock_run, helm_service):
        """#461：不傳 timeout 的呼叫端一行都不能變。

        `--timeout` 缺席 ⇒ helm 用自己的 5m 預設（改動前的實況），subprocess 上限
        仍是 settings.helm_timeout。install / redeploy / reconfigure 全走這條，
        本次只有 reset 路徑帶界，blast radius 必須為零。
        """
        mock_run.return_value = Mock(returncode=0, stdout=self._upgrade_stdout(), stderr="")

        helm_service.upgrade(
            namespace="paas-ws-test", name="test-release", chart="oci://example/mcp-server",
        )

        assert "--timeout" not in mock_run.call_args[0][0]
        assert mock_run.call_args.kwargs["timeout"] == helm_service.timeout

    @patch("subprocess.run")
    def test_upgrade_renders_timeout_and_bounds_subprocess(self, mock_run, helm_service):
        """兩個預算各管一段，不可互相推導。

        `--timeout` 只約束 helm 等 K8s 資源就緒的那一段；subprocess 上限管整個
        CLI 進程。reset 路徑刻意讓後者 = 2×前者 + CLI 開銷，因為 `--atomic` 失敗
        時 helm 會再跑一次帶 wait 的 rollback——上限抓太緊會把 rollback 腰斬。
        """
        mock_run.return_value = Mock(returncode=0, stdout=self._upgrade_stdout(), stderr="")

        helm_service.upgrade(
            namespace="paas-ws-test", name="test-release", chart="oci://example/mcp-server",
            atomic=True, timeout_seconds=15, subprocess_timeout_seconds=45,
        )

        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--timeout") + 1] == "15s"
        assert "--atomic" in cmd
        assert mock_run.call_args.kwargs["timeout"] == 45

    @patch("subprocess.run")
    def test_subprocess_timeout_message_reports_the_effective_budget(
        self, mock_run, helm_service,
    ):
        """逾時訊息要講**這次**的預算，不是服務層預設——否則 log 無法判讀。"""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="helm", timeout=45)

        with pytest.raises(HelmException) as error:
            helm_service.upgrade(
                namespace="paas-ws-test", name="test-release",
                chart="oci://example/mcp-server", subprocess_timeout_seconds=45,
            )

        assert "45s" in error.value.message
        assert str(helm_service.timeout) not in error.value.message

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
        # stdout="{}"：#423 的 default-SA 收斂會 `get serviceaccount -o json`，
        # 需要合法 JSON 才解析得動。
        mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")

        result = k8s_service.create_namespace(
            name="paas-ws-new",
            cpu_limit="2",
            memory_limit="4Gi",
            storage_limit="20Gi",
        )

        assert "created" in result["message"]
        # #278 A2：三次 apply（namespace + quota + limitrange）
        # #423：再加 default SA 的 get + patch（registry 未設定 → 不含 secret apply）
        assert mock_run.call_count == 5

    def test_create_namespace_invalid_prefix(self, k8s_service):
        """Test create namespace with invalid prefix."""
        with pytest.raises(ValueError):
            k8s_service.create_namespace(
                name="invalid",
                cpu_limit="2",
                memory_limit="4Gi",
                storage_limit="20Gi",
            )

    def test_build_managed_ns_labels_stamps_owner(self):
        """#277：build_managed_ns_labels 帶 owner 參數時 stamp woow.tw/owner。"""
        from src.services.helm import build_managed_ns_labels, OWNER_LABEL
        labels = build_managed_ns_labels(workspace_id=7, owner="7")
        assert labels[OWNER_LABEL] == "7"

    @patch("src.services.helm.build_managed_ns_labels", wraps=build_managed_ns_labels)
    @patch("subprocess.run")
    def test_create_namespace_passes_owner(self, mock_run, mock_labels, k8s_service):
        """#277：create_namespace 建 ns 時把 workspace_id 當 owner 傳入。"""
        mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
        k8s_service.create_namespace("paas-ws-abc", "8", "8Gi", "100Gi", workspace_id=7)
        # 建 ns 那次 build_managed_ns_labels 帶 owner="7"
        assert any(c.kwargs.get("owner") == "7" for c in mock_labels.call_args_list)

    @patch("subprocess.run")
    def test_list_managed_namespaces_parses_names(self, mock_run, k8s_service):
        """#277：list_managed_namespaces 唯讀解析帶 paas-managed=true 的 ns 名。"""
        mock_run.return_value = Mock(returncode=0, stderr="", stdout=json.dumps({
            "items": [
                {"metadata": {"name": "paas-ws-abc", "labels": {"paas-managed": "true"}}},
                {"metadata": {"name": "paas-ws-pa-def", "labels": {"paas-managed": "true"}}},
            ]
        }))
        names = k8s_service.list_managed_namespaces()
        assert set(names) == {"paas-ws-abc", "paas-ws-pa-def"}
        # 走 label selector 圈定右集邊界（反向發現全部、無單一輸入 ns 可驗）。
        argv = mock_run.call_args_list[0].args[0]
        assert "-l" in argv and "paas-managed=true" in argv

    @patch("subprocess.run")
    def test_list_managed_namespaces_scopes_by_env(self, mock_run, k8s_service, monkeypatch):
        """#277：PAAS_ENV 有設時 selector AND paas.woowtech.io/env（不回別 env 的 ns，
        避免共叢集 stg inventory 把 prod 活躍 ns 灌成孤兒噪音）。"""
        monkeypatch.setattr("src.config.settings.paas_env", "stg")
        mock_run.return_value = Mock(returncode=0, stderr="", stdout='{"items": []}')
        k8s_service.list_managed_namespaces()
        argv = mock_run.call_args_list[0].args[0]
        assert "paas-managed=true,paas.woowtech.io/env=stg" in argv

    def test_build_limitrange_manifest_shape(self, k8s_service):
        """#278 A2：CS ns LimitRange manifest 形狀正確。"""
        m = k8s_service._build_limitrange_manifest("paas-ws-test-abc")
        assert m["kind"] == "LimitRange"
        assert m["metadata"]["name"] == "paas-ws-test-abc-limits"
        assert m["metadata"]["namespace"] == "paas-ws-test-abc"
        entry = m["spec"]["limits"][0]
        assert entry["type"] == "Container"
        assert entry["default"] == {"cpu": "500m", "memory": "512Mi"}
        assert entry["defaultRequest"] == {"cpu": "500m", "memory": "512Mi"}

    @patch("subprocess.run")
    def test_create_namespace_applies_limitrange(self, mock_run, k8s_service):
        """#278 A2：create_namespace 現在做 ns + quota + limitrange 三次 apply。"""
        mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
        k8s_service.create_namespace("paas-ws-test-abc", "8", "8Gi", "100Gi")
        applied = [c.args[0] for c in mock_run.call_args_list]  # 每個 = 完整 kubectl argv
        apply_calls = [a for a in applied if "apply" in a and "-f" in a]
        assert len(apply_calls) >= 3

    @patch("subprocess.run")
    def test_update_namespace_quota_applies_full_hard_map(self, mock_run, k8s_service):
        """#278 A1：整組 hard overwrite ns ResourceQuota 會 apply 一次。"""
        mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
        k8s_service.update_namespace_quota("paas-ws-test-abc", "12", "12Gi", "150Gi")
        apply_calls = [c.args[0] for c in mock_run.call_args_list if "apply" in c.args[0]]
        assert apply_calls, "should apply the quota"

    def test_update_namespace_quota_rejects_foreign_ns(self, k8s_service):
        """#278 A1：fail-closed prefix 守衛，非 paas ns 直接 raise。"""
        with pytest.raises(ValueError):
            k8s_service.update_namespace_quota("not-a-paas-ns", "8", "8Gi", "100Gi")

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
    def test_apply_manifest_rejects_invalid_namespace(self, mock_run, k8s_service):
        """#284 W0-hardening: apply_manifest 是 mutate choke-point，但入口原本不驗
        namespace（RBAC 不 enforce prefix → 無 backstop）。namespaced 資源的
        metadata.namespace 出前綴外 → 在任何 kubectl apply 前拒絕。"""
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "x", "namespace": "kube-system"},
        }
        with pytest.raises(ValueError, match="must start with"):
            k8s_service.apply_manifest(manifest)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_apply_manifest_rejects_invalid_namespace_kind_name(self, mock_run, k8s_service):
        """Namespace-kind manifest 的 ns 在 metadata.name（無 metadata.namespace）——
        apply_manifest 必須同樣驗證該 name（_ensure_namespace 走此路徑）。"""
        manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "kube-system"},
        }
        with pytest.raises(ValueError, match="must start with"):
            k8s_service.apply_manifest(manifest)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_apply_manifest_allows_valid_namespace_kind(self, mock_run, k8s_service):
        """合法 paas-ws-* Namespace manifest（_ensure_namespace 冷啟 stamp 路徑）不被
        新 guard 破壞。"""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({"kind": "Namespace", "metadata": {"name": "paas-ws-pa-abc"}}),
            stderr="",
        )
        result = k8s_service.apply_manifest({
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "paas-ws-pa-abc", "labels": {"paas-managed": "true"}},
        })
        assert result["metadata"]["name"] == "paas-ws-pa-abc"
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_ensure_registry_secret_rejects_invalid_namespace(self, mock_run, k8s_service):
        """ensure_registry_secret 會 kubectl apply 一個 Secret，但入口原本不驗 ns
        （靠 caller 紀律）。出前綴外的 ns 必須在入口拒絕，即使 registry 未設定。"""
        with pytest.raises(ValueError, match="must start with"):
            k8s_service.ensure_registry_secret("kube-system")
        mock_run.assert_not_called()

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

    @patch("subprocess.run")
    def test_list_pvcs_applies_label_selector(self, mock_run, k8s_service):
        """#281: list_pvcs must support a label_selector so teardown can scope
        orphan-labeling to its OWN release (app.kubernetes.io/instance={name}).

        A per-workspace ns is shared by multiple services; without a selector the
        teardown labeled EVERY PVC in the ns — including other live services' data
        PVCs — for cron deletion (data-loss level, #281).
        """
        mock_run.return_value = Mock(
            returncode=0, stdout="svc-a-mcp-server-data", stderr="")
        k8s_service.list_pvcs(
            "paas-ws-test", label_selector="app.kubernetes.io/instance=svc-a")
        cmd = mock_run.call_args[0][0]
        assert "--selector" in cmd, f"list_pvcs must pass --selector, got {cmd!r}"
        assert "app.kubernetes.io/instance=svc-a" in cmd

    @patch("subprocess.run")
    def test_list_pvcs_no_selector_lists_all(self, mock_run, k8s_service):
        """Back-compat: no selector → no --selector flag (lists the whole ns)."""
        mock_run.return_value = Mock(returncode=0, stdout="a b", stderr="")
        result = k8s_service.list_pvcs("paas-ws-test")
        cmd = mock_run.call_args[0][0]
        assert "--selector" not in cmd
        assert result == ["a", "b"]


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
        # stdout="{}"：#423 default-SA 收斂會 get -o json，需合法 JSON；ns-label
        # 捕捉只認 kind==Namespace，不受影響。
        mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
        monkeypatch.setattr("src.config.settings.paas_env", "stg")
        captured = self._capture_ns_labels(monkeypatch)
        KubernetesService().create_namespace(
            name="paas-ws-stg-x", cpu_limit="1", memory_limit="1Gi", storage_limit="5Gi")
        assert captured["labels"] == {
            "paas-managed": "true", "paas.woowtech.io/env": "stg"}

    @patch("subprocess.run")
    def test_create_namespace_omits_env_label_in_manifest_when_empty(self, mock_run, monkeypatch):
        """PAAS_ENV 空 → ns manifest 只帶 paas-managed（不貼空 env value）。"""
        # stdout="{}"：#423 default-SA 收斂會 get -o json，需合法 JSON；ns-label
        # 捕捉只認 kind==Namespace，不受影響。
        mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
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
        """Model B：reach_workspace_ids=[3,5] → 兩把 reach-ws label（跨 workspace 可達集合）。
        value 用共用常數 REACH_WS_LABEL_VALUE（O4 leg-2 selector 必引用同一個，避免
        "true" vs "1" silent deny-all）。"""
        from src.services.helm import build_managed_ns_labels, REACH_WS_LABEL_VALUE
        monkeypatch.setattr("src.config.settings.paas_env", "")
        labels = build_managed_ns_labels(reach_workspace_ids=[3, 5])
        assert labels["woow.tw/reach-ws-3"] == REACH_WS_LABEL_VALUE
        assert labels["woow.tw/reach-ws-5"] == REACH_WS_LABEL_VALUE
        assert REACH_WS_LABEL_VALUE == "true"

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
        # stdout="{}"：workspace_id 使 create_namespace 續呼 #279 O4 baseline NP（apply_manifest
        # 會 json.loads(stdout)），需合法 JSON；ns-label 捕捉只認 kind==Namespace、不受 NP 影響。
        mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
        monkeypatch.setattr("src.config.settings.paas_env", "stg")
        captured = TestManagedNsLabels._capture_ns_labels(monkeypatch)
        KubernetesService().create_namespace(
            name="paas-ws-stg-x", cpu_limit="1", memory_limit="1Gi",
            storage_limit="5Gi", workspace_id=7)
        assert captured["labels"] == {
            "paas-managed": "true",
            "paas.woowtech.io/env": "stg",
            "woow.tw/workspace-id": "7",
            "woow.tw/owner": "7",  # #277：CS ns owner＝workspace_id
        }


class TestReconcileNsReachLabels:
    """O3: reconcile_ns_reach_labels — 顯式增刪 agent ns 的 reach-ws label 到 desired
    集合。apply_manifest 只 merge（能長不能刪），unbind 撤銷靠這條顯式刪 key（#279 F1）。
    只 diff reach-ws-*，不碰 paas-managed / env / workspace-id。add+刪同一原子 kubectl。
    """

    @staticmethod
    def _svc_with_current(monkeypatch, current_labels):
        """KubernetesService，其 get_namespace_labels 回固定 current、_run_command 攔 args。"""
        svc = KubernetesService()
        monkeypatch.setattr(svc, "get_namespace_labels", lambda ns: dict(current_labels))
        calls = []
        monkeypatch.setattr(
            svc, "_run_command",
            lambda args: calls.append(args) or Mock(returncode=0, stdout="", stderr=""))
        return svc, calls

    def test_adds_missing_reach_ws_key(self, monkeypatch):
        svc, calls = self._svc_with_current(
            monkeypatch, {"paas-managed": "true", "woow.tw/reach-ws-3": "true"})
        res = svc.reconcile_ns_reach_labels("paas-ws-x", [3, 5])
        assert res["added"] == ["woow.tw/reach-ws-5"]
        assert res["removed"] == []
        assert "woow.tw/reach-ws-5=true" in calls[0]

    def test_removes_stale_reach_ws_key_explicitly(self, monkeypatch):
        svc, calls = self._svc_with_current(
            monkeypatch, {"woow.tw/reach-ws-3": "true", "woow.tw/reach-ws-5": "true"})
        res = svc.reconcile_ns_reach_labels("paas-ws-x", [3])
        assert res["removed"] == ["woow.tw/reach-ws-5"]
        assert res["added"] == []
        # trailing '-' = kubectl delete-label syntax (顯式刪，非只 merge)
        assert "woow.tw/reach-ws-5-" in calls[0]

    def test_noop_when_already_reconciled(self, monkeypatch):
        svc, calls = self._svc_with_current(
            monkeypatch, {"paas-managed": "true", "woow.tw/reach-ws-3": "true"})
        res = svc.reconcile_ns_reach_labels("paas-ws-x", [3])
        assert res == {"added": [], "removed": []}
        assert calls == []  # idempotent → 不發任何 kubectl label

    def test_empty_desired_removes_all_reach_ws(self, monkeypatch):
        """空綁定 = deny-all：desired [] → 刪掉所有 reach-ws（opt-in 收斂）。"""
        svc, calls = self._svc_with_current(monkeypatch, {
            "paas-managed": "true",
            "woow.tw/reach-ws-3": "true", "woow.tw/reach-ws-5": "true"})
        res = svc.reconcile_ns_reach_labels("paas-ws-x", [])
        assert res["removed"] == ["woow.tw/reach-ws-3", "woow.tw/reach-ws-5"]
        assert "woow.tw/reach-ws-3-" in calls[0]
        assert "woow.tw/reach-ws-5-" in calls[0]

    def test_never_touches_non_reach_labels(self, monkeypatch):
        """reach 集已對 → 無命令；paas-managed / env / workspace-id 一律不進 diff。"""
        svc, calls = self._svc_with_current(monkeypatch, {
            "paas-managed": "true", "paas.woowtech.io/env": "stg",
            "woow.tw/workspace-id": "9", "woow.tw/reach-ws-3": "true"})
        svc.reconcile_ns_reach_labels("paas-ws-x", [3])
        assert calls == []

    def test_add_and_remove_in_single_atomic_command(self, monkeypatch):
        svc, calls = self._svc_with_current(monkeypatch, {"woow.tw/reach-ws-3": "true"})
        svc.reconcile_ns_reach_labels("paas-ws-x", [5])
        assert len(calls) == 1  # 一條原子 kubectl label 同時 add+刪
        assert "woow.tw/reach-ws-5=true" in calls[0]
        assert "woow.tw/reach-ws-3-" in calls[0]

    @patch("subprocess.run")
    def test_get_namespace_labels_parses_json(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stderr="", stdout=json.dumps(
            {"metadata": {"labels": {"paas-managed": "true", "woow.tw/reach-ws-3": "true"}}}))
        labels = KubernetesService().get_namespace_labels("paas-ws-x")
        assert labels == {"paas-managed": "true", "woow.tw/reach-ws-3": "true"}

    @patch("subprocess.run")
    def test_get_namespace_labels_empty_when_none(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stderr="", stdout=json.dumps({"metadata": {}}))
        assert KubernetesService().get_namespace_labels("paas-ws-x") == {}


class TestDefaultSaImagePullSecret:
    """#423：租戶 ns 的 default ServiceAccount 必須帶 registry pull secret。

    為什麼不是靠 chart values 就好：VM 型 cloud service 的 golden image 由 CDI
    importer pod 匯入，那顆 pod 由 CDI controller 生成、不屬於任何 chart 的
    podSpec，`_ensure_image_pull_secrets` 注入 values 的那一層完全碰不到它。它跑在
    ns 的 default SA 上，只能靠 ServiceAccount admission 併入 —— default SA 是空的
    就永久卡在 ImportScheduled。

    合併語意是本組的重點：JSON merge patch 對 array 是**整份覆寫**，所以實作必須
    先讀既有清單再 append 送出，否則會靜默洗掉租戶／其他機制放進去的 pull secret。
    """

    _SECRET = "paas-registry"

    @staticmethod
    def _svc_with_sa(sa_body, patch_result=None):
        """回傳 (service, calls)：`_run_command` 被換掉，calls 收所有 kubectl argv。

        直接換 `_run_command` 而非 subprocess.run，讓「get 回什麼」與「patch 收到
        什麼」在同一組 argv 裡看得一清二楚。
        """
        svc = KubernetesService()
        calls = []

        def fake(args, *a, **k):
            calls.append(args)
            if args[0] == "get":
                return Mock(returncode=0, stderr="", stdout=json.dumps(sa_body))
            return patch_result or Mock(returncode=0, stderr="", stdout="{}")

        svc._run_command = fake
        return svc, calls

    @staticmethod
    def _patched_secrets(calls):
        """從 kubectl patch argv 取出送出去的 imagePullSecrets 清單。"""
        patch_calls = [c for c in calls if c[0] == "patch"]
        assert len(patch_calls) == 1, f"expected exactly one patch, got {patch_calls}"
        payload = json.loads(patch_calls[0][patch_calls[0].index("--patch") + 1])
        return payload["imagePullSecrets"]

    def test_patches_empty_sa(self):
        """SA 完全沒有 imagePullSecrets → 補上唯一一筆。"""
        svc, calls = self._svc_with_sa({"metadata": {"name": "default"}})
        assert svc.ensure_default_sa_image_pull_secret("paas-ws-abc") is True
        assert self._patched_secrets(calls) == [{"name": self._SECRET}]

    def test_patches_sa_with_null_field(self):
        """`imagePullSecrets: null`（欄位在但空）不得讓實作炸在 None 上。"""
        svc, calls = self._svc_with_sa({"imagePullSecrets": None})
        assert svc.ensure_default_sa_image_pull_secret("paas-ws-abc") is True
        assert self._patched_secrets(calls) == [{"name": self._SECRET}]

    def test_preserves_existing_entries(self):
        """已有別的 pull secret → 保留並 append，不覆蓋（merge patch 整份送出）。

        這條是本功能最容易寫錯的地方：只送 `[{"name": "paas-registry"}]` 會讓
        kubectl 用整份覆寫語意把 tenant-custom 洗掉，而且測試若只斷言「有沒有
        paas-registry」完全看不出來。
        """
        svc, calls = self._svc_with_sa(
            {"imagePullSecrets": [{"name": "tenant-custom"}]}
        )
        assert svc.ensure_default_sa_image_pull_secret("paas-ws-abc") is True
        assert self._patched_secrets(calls) == [
            {"name": "tenant-custom"},
            {"name": self._SECRET},
        ]

    def test_idempotent_when_already_present(self):
        """已含目標 secret → 完全不發 patch（回 False）。"""
        svc, calls = self._svc_with_sa(
            {"imagePullSecrets": [{"name": self._SECRET}]}
        )
        assert svc.ensure_default_sa_image_pull_secret("paas-ws-abc") is False
        assert [c for c in calls if c[0] == "patch"] == []

    def test_idempotent_when_present_among_others(self):
        """目標 secret 夾在其他項目之間也算已具備，不重複 append。"""
        svc, calls = self._svc_with_sa({"imagePullSecrets": [
            {"name": "tenant-custom"}, {"name": self._SECRET}, {"name": "other"},
        ]})
        assert svc.ensure_default_sa_image_pull_secret("paas-ws-abc") is False
        assert [c for c in calls if c[0] == "patch"] == []

    def test_patch_targets_default_sa_with_merge_type(self):
        """argv 契約：patch 的是該 ns 的 serviceaccount/default，且 --type merge。"""
        svc, calls = self._svc_with_sa({})
        svc.ensure_default_sa_image_pull_secret("paas-ws-abc")
        argv = [c for c in calls if c[0] == "patch"][0]
        assert argv[:3] == ["patch", "serviceaccount", "default"]
        assert argv[argv.index("--namespace") + 1] == "paas-ws-abc"
        assert argv[argv.index("--type") + 1] == "merge"

    def test_tolerates_absent_sa_after_retries(self):
        """ns 剛建、SA 還沒被 controller 補上 → 重試耗盡後容忍（False，不 raise）。

        不 raise 是刻意的：SA 出現後任何一次 install / upgrade / reset 都會再收斂，
        沒必要為了這個空窗把整個 ns 建立弄失敗。
        """
        from src.services.helm import _SA_LOOKUP_ATTEMPTS

        svc = KubernetesService()
        calls = []

        def always_missing(args, *a, **k):
            calls.append(args)
            raise KubectlException(
                message="kubectl command failed with code 1",
                command="kubectl get serviceaccount default",
                stderr='Error from server (NotFound): serviceaccounts "default" not found',
            )

        svc._run_command = always_missing
        with patch("src.services.helm.time.sleep") as mock_sleep:
            assert svc.ensure_default_sa_image_pull_secret("paas-ws-abc") is False
        assert len(calls) == _SA_LOOKUP_ATTEMPTS, "NotFound 要重試滿設定次數"
        assert [c for c in calls if c[0] == "patch"] == []
        assert mock_sleep.call_count == _SA_LOOKUP_ATTEMPTS - 1, "最後一次不必再等"

    def test_recovers_when_sa_appears_on_retry(self):
        """race 復原：第一次 NotFound、第二次拿到 SA → 照樣補上。"""
        svc = KubernetesService()
        calls = []

        def missing_once(args, *a, **k):
            calls.append(args)
            if args[0] == "get" and len([c for c in calls if c[0] == "get"]) == 1:
                raise KubectlException(
                    message="not found",
                    command="kubectl get serviceaccount default",
                    stderr='Error from server (NotFound): serviceaccounts "default" not found',
                )
            if args[0] == "get":
                return Mock(returncode=0, stderr="", stdout="{}")
            return Mock(returncode=0, stderr="", stdout="{}")

        svc._run_command = missing_once
        with patch("src.services.helm.time.sleep"):
            assert svc.ensure_default_sa_image_pull_secret("paas-ws-abc") is True
        assert [c for c in calls if c[0] == "patch"], "SA 出現後必須真的 patch"

    def test_reraises_non_notfound_kubectl_error(self):
        """RBAC Forbidden 之類**不是** NotFound → 直接拋，不得偽裝成 race 靜默重試。

        把整個 KubectlException 當「等一下就好」，會讓權限設定錯誤表現成 VM 開通
        神秘卡住，而不是一個看得見的錯誤。
        """
        svc = KubernetesService()
        calls = []

        def forbidden(args, *a, **k):
            calls.append(args)
            raise KubectlException(
                message="kubectl command failed with code 1",
                command="kubectl get serviceaccount default",
                stderr=(
                    'Error from server (Forbidden): serviceaccounts "default" is '
                    'forbidden: User "system:serviceaccount:paas-system:paas-operator" '
                    "cannot get resource"
                ),
            )

        svc._run_command = forbidden
        with patch("src.services.helm.time.sleep") as mock_sleep:
            with pytest.raises(KubectlException):
                svc.ensure_default_sa_image_pull_secret("paas-ws-abc")
        assert len(calls) == 1, "非 NotFound 不重試"
        mock_sleep.assert_not_called()

    def test_rejects_foreign_namespace(self):
        """mutate 方法的前綴守衛：非 paas ns 直接 raise，且一次 kubectl 都不發。"""
        svc, calls = self._svc_with_sa({})
        with pytest.raises(ValueError):
            svc.ensure_default_sa_image_pull_secret("kube-system")
        assert calls == []

    def test_is_not_found_error_narrows_to_notfound(self):
        """判別式本身：只認 NotFound，Forbidden / timeout 一律 False。"""
        def _exc(stderr):
            return KubectlException(message="m", command="c", stderr=stderr)

        assert KubernetesService.is_not_found_error(
            _exc('Error from server (NotFound): serviceaccounts "default" not found'))
        assert not KubernetesService.is_not_found_error(
            _exc('Error from server (Forbidden): cannot get resource'))
        assert not KubernetesService.is_not_found_error(
            _exc("kubectl command timed out after 30s"))
        assert not KubernetesService.is_not_found_error(_exc(""))

    @patch("subprocess.run")
    def test_create_namespace_converges_default_sa(self, mock_run):
        """ns 準備階段接線：create_namespace 對剛建的 ns 收斂 default SA。"""
        mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
        k = KubernetesService()
        k.ensure_default_sa_image_pull_secret = MagicMock()
        k.create_namespace("paas-ws-abc", "8", "8Gi", "100Gi")
        k.ensure_default_sa_image_pull_secret.assert_called_once_with("paas-ws-abc")
