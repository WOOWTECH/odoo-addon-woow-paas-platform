"""Tests for API endpoints."""
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import (
    PodInfo,
    PodPhase,
    ReleaseInfo,
    ReleaseRevision,
    ReleaseStatus,
)
from src.services.helm import KubectlException


@pytest.fixture
def client():
    """Create FastAPI test client (auth satisfied with a real key + header).

    F-PC-4: an unset key now fails closed (503), so the client supplies a real
    key and a matching X-API-Key header rather than relying on the old empty-key
    pass-through. Auth-specific behaviour is covered by TestAuthentication.
    """
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestHealthEndpoint:
    """Test health check endpoint."""

    def test_health_check_healthy(self, client):
        """Test health check when Helm is available."""
        with patch("src.main.HelmService") as mock_helm:
            mock_helm.return_value.get_version.return_value = "v3.13.3"

            response = client.get("/health")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert "helm_version" in data

    def test_health_check_degraded(self, client):
        """Test health check when Helm is unavailable."""
        with patch("src.main.HelmService") as mock_helm:
            mock_helm.return_value.get_version.side_effect = Exception("Helm not found")

            response = client.get("/health")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "degraded"


class TestReleaseEndpoints:
    """Test release management endpoints."""

    @patch("src.api.releases.helm_service")
    def test_create_release(self, mock_helm, client):
        """Test create release endpoint."""
        mock_helm.install.return_value = ReleaseInfo(
            name="test-release",
            namespace="paas-ws-test",
            revision=1,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
        )

        response = client.post(
            "/api/releases",
            json={
                "namespace": "paas-ws-test",
                "name": "test-release",
                "chart": "nginx",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "test-release"
        assert data["status"] == "deployed"

    @patch("src.api.releases.helm_service")
    def test_create_release_forwards_sensitive_values_and_masks_response(
        self, mock_helm, client
    ):
        """Sensitive values must travel separately and never echo back."""
        mock_helm.install.return_value = ReleaseInfo(
            name="mcp-x",
            namespace="paas-ws-test",
            revision=1,
            status=ReleaseStatus.DEPLOYED,
            chart="mcp-server",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
            values={
                "config": {
                    "sensitive": {"admin_password": "secret-password"},
                    "nonSensitive": {"connection": {"url": "https://tenant.example"}},
                }
            },
        )

        response = client.post(
            "/api/releases",
            json={
                "namespace": "paas-ws-test",
                "name": "mcp-x",
                "chart": "oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server",
                "values": {"config": {"nonSensitive": {"connection": {"url": "https://tenant.example"}}}},
                "sensitive_values": {"config": {"sensitive": {"admin_password": "secret-password"}}},
            },
        )

        assert response.status_code == 201
        call_kwargs = mock_helm.install.call_args.kwargs
        assert call_kwargs["sensitive_values"] == {
            "config": {"sensitive": {"admin_password": "secret-password"}}
        }
        assert response.json()["values"]["config"]["sensitive"]["admin_password"] == "***"

    @patch("src.api.releases.helm_service")
    def test_create_release_injects_image_pull_secret(self, mock_helm, client):
        """releases install must inject imagePullSecrets so the kubelet can pull
        private images. layer-3 gap: the cloud-service install path missed this
        while agent_runtimes.py had it (F-PILOT-4) — mcp-server's private
        hermes-mcp-admin → pod Init:ImagePullBackOff 401 without it. chart
        consumes top-level .Values.imagePullSecrets (confirmed)."""
        mock_helm.install.return_value = ReleaseInfo(
            name="mcp-x",
            namespace="paas-ws-test",
            revision=1,
            status=ReleaseStatus.DEPLOYED,
            chart="mcp-server",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
        )

        client.post(
            "/api/releases",
            json={
                "namespace": "paas-ws-test",
                "name": "mcp-x",
                "chart": "oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server",
                "values": {},
            },
        )

        # operator injected imagePullSecrets (default paas-registry) into the
        # helm install values; chart renders it to pod spec.imagePullSecrets.
        injected = mock_helm.install.call_args.kwargs["values"]
        assert injected.get("imagePullSecrets") == [{"name": "paas-registry"}]

    @patch("src.api.releases.helm_service")
    def test_create_release_invalid_namespace(self, mock_helm, client):
        """Test create release with invalid namespace."""
        mock_helm.install.side_effect = ValueError("Invalid namespace")

        response = client.post(
            "/api/releases",
            json={
                "namespace": "invalid",
                "name": "test",
                "chart": "nginx",
            },
        )

        assert response.status_code == 400

    @patch("src.api.releases.helm_service")
    def test_get_release(self, mock_helm, client):
        """Test get release endpoint."""
        mock_helm.get.return_value = ReleaseInfo(
            name="test-release",
            namespace="paas-ws-test",
            revision=2,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
        )

        response = client.get("/api/releases/paas-ws-test/test-release")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "test-release"
        assert data["revision"] == 2

    @patch("src.api.releases.helm_service")
    def test_get_release_not_found(self, mock_helm, client):
        """Test get release when release doesn't exist."""
        from src.services.helm import HelmException

        mock_helm.get.side_effect = HelmException(
            message="Not found",
            command="helm get",
            stderr="Error: release not found",
        )

        response = client.get("/api/releases/paas-ws-test/missing")

        assert response.status_code == 404

    @patch("src.api.releases.helm_service")
    def test_upgrade_release(self, mock_helm, client):
        """Test upgrade release endpoint."""
        mock_helm.upgrade.return_value = ReleaseInfo(
            name="test-release",
            namespace="paas-ws-test",
            revision=3,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.1.0",
            updated="2024-01-02T00:00:00Z",
        )

        response = client.patch(
            "/api/releases/paas-ws-test/test-release",
            json={
                "version": "1.1.0",
                "values": {"replicas": 3},
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["revision"] == 3

    @patch("src.api.releases.helm_service")
    def test_upgrade_release_forwards_sensitive_values(self, mock_helm, client):
        mock_helm.upgrade.return_value = ReleaseInfo(
            name="test-release",
            namespace="paas-ws-test",
            revision=3,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.1.0",
            updated="2024-01-02T00:00:00Z",
        )

        client.patch(
            "/api/releases/paas-ws-test/test-release",
            json={
                "version": "1.1.0",
                "values": {"replicas": 3},
                "sensitive_values": {"config": {"sensitive": {"admin_password": "secret"}}},
            },
        )

        assert mock_helm.upgrade.call_args.kwargs["sensitive_values"] == {
            "config": {"sensitive": {"admin_password": "secret"}}
        }

    @patch("src.api.releases.helm_service")
    def test_upgrade_release_injects_image_pull_secret(self, mock_helm, client):
        """#448：upgrade 也必須注入預設 imagePullSecrets。

        install 在 a0e7c25a 已補注入，但 upgrade 漏了同樣的 setdefault；
        #411 之後 upgrade 走全量 reset values，私有 image 的 CS 任一次
        redeploy / reconfigure / reset-password 都會掉 pull secret →
        ImagePullBackOff 401（stg 實證）。"""
        mock_helm.upgrade.return_value = ReleaseInfo(
            name="mcp-x",
            namespace="paas-ws-test",
            revision=2,
            status=ReleaseStatus.DEPLOYED,
            chart="mcp-server",
            app_version="1.0.0",
            updated="2024-01-02T00:00:00Z",
        )

        response = client.patch(
            "/api/releases/paas-ws-test/mcp-x",
            json={
                "chart": "oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server",
                "values": {"replicas": 2},
                "reset_values": True,
                "reuse_values": False,
            },
        )

        assert response.status_code == 200
        injected = mock_helm.upgrade.call_args.kwargs["values"]
        assert injected.get("imagePullSecrets") == [{"name": "paas-registry"}]
        # 呼叫端原有 values 不能被吃掉
        assert injected.get("replicas") == 2

    @patch("src.api.releases.helm_service")
    def test_upgrade_release_preserves_caller_image_pull_secrets(
        self, mock_helm, client
    ):
        """#448：呼叫端已明帶 imagePullSecrets 時不可覆蓋（setdefault 語意）。"""
        mock_helm.upgrade.return_value = ReleaseInfo(
            name="mcp-x",
            namespace="paas-ws-test",
            revision=2,
            status=ReleaseStatus.DEPLOYED,
            chart="mcp-server",
            app_version="1.0.0",
            updated="2024-01-02T00:00:00Z",
        )

        client.patch(
            "/api/releases/paas-ws-test/mcp-x",
            json={
                "chart": "oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server",
                "values": {"imagePullSecrets": [{"name": "tenant-custom"}]},
            },
        )

        injected = mock_helm.upgrade.call_args.kwargs["values"]
        assert injected.get("imagePullSecrets") == [{"name": "tenant-custom"}]

    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_delete_release(self, mock_helm, mock_k8s, mock_cf, client):
        """Test delete release endpoint."""
        # MINOR-7 (hermetic): without these patches the teardown path shells
        # out to a REAL kubectl (and cloudflare) from a unit test.
        mock_helm.uninstall.return_value = {"message": "Release uninstalled"}
        mock_k8s.list_pvcs.return_value = []
        mock_k8s.has_datavolume_crd.return_value = True
        mock_k8s.list_datavolumes.return_value = []
        mock_cf.delete_route = AsyncMock(return_value=True)

        response = client.delete("/api/releases/paas-ws-test/test-release")

        assert response.status_code == 200
        data = response.json()
        assert "uninstalled" in data["message"].lower()

    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_delete_release_orphan_pvc_label_value_is_k8s_safe(
        self, mock_helm, mock_k8s, mock_cf, client
    ):
        """orphaned-at PVC label value must be a valid k8s label value.

        Regression for operator #208: the teardown stamped the orphaned-at label
        with an RFC3339 timestamp ("2026-06-20T08:03:00Z"). The ':' chars are
        illegal in a k8s label value, so `kubectl label` was rejected atomically
        (all three labels dropped) and the error was silently swallowed. The
        pvc-cleanup cron's `-l paas.woowtech.io/orphaned-at` selector then never
        matched → every postgres-backed service leaked its data PVC forever.
        The stamped value must match the k8s label-value regex.
        """
        k8s_label_value = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
        mock_helm.uninstall.return_value = {"message": "Release uninstalled"}
        mock_k8s.list_pvcs.return_value = ["data-test-release-postgresql-0"]
        # #423 W4.4: stub the new DataVolume lookup — an unstubbed MagicMock
        # returns a truthy Mock and drags this test into the VM branch.
        mock_k8s.has_datavolume_crd.return_value = True
        mock_k8s.list_datavolumes.return_value = []
        mock_cf.delete_route = AsyncMock(return_value=True)

        response = client.delete(
            "/api/releases/paas-ws-test/test-release?subdomain=paas-cs-test"
        )

        assert response.status_code == 200
        mock_k8s.label_pvcs.assert_called_once()
        labels = mock_k8s.label_pvcs.call_args[0][2]
        value = labels["paas.woowtech.io/orphaned-at"]
        assert k8s_label_value.match(value) and len(value) <= 63, (
            f"orphaned-at label value {value!r} is not a valid k8s label value "
            f"(must match {k8s_label_value.pattern} and be <=63 chars)"
        )
        # Pin the production contract — epoch SECONDS — not just k8s-syntax. The
        # k8s regex above ALSO matches the empty string, so a value-drop-to-''
        # regression (which still passes the cron's key-existence selector but
        # bricks the grace-period age math) would false-green on the regex alone.
        assert value.isdigit() and 9 <= len(value) <= 11, (
            f"orphaned-at must be epoch seconds (9-11 digits), got {value!r}"
        )

    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_delete_release_scopes_orphan_labeling_to_own_release(
        self, mock_helm, mock_k8s, mock_cf, client
    ):
        """#281 (data-loss): a per-workspace ns is SHARED by multiple services.

        delete_release must scope its orphan-PVC labeling to ITS OWN release via
        the app.kubernetes.io/instance selector — otherwise it lists (and stamps
        orphaned-at on) every PVC in the ns, marking OTHER live services' Bound
        data PVCs for the pvc-cleanup cron to delete. Verified live on stg: a
        svc-02c671c7 delete had mislabeled svc-06b55af1-odoo/-postgresql + svc-
        5b78f97b-n8n. Pin that list_pvcs is scoped, not ns-wide.
        """
        mock_helm.uninstall.return_value = {"message": "Release uninstalled"}
        mock_k8s.list_pvcs.return_value = ["svc-deadbeef-mcp-server-data"]
        mock_k8s.has_datavolume_crd.return_value = True
        mock_k8s.list_datavolumes.return_value = []
        mock_cf.delete_route = AsyncMock(return_value=True)

        response = client.delete(
            "/api/releases/paas-ws-test/svc-deadbeef?subdomain=paas-cs-test"
        )
        assert response.status_code == 200

        mock_k8s.list_pvcs.assert_called_once()
        call = mock_k8s.list_pvcs.call_args
        selector = call.kwargs.get("label_selector")
        if selector is None and len(call.args) > 1:
            selector = call.args[1]
        assert selector == "app.kubernetes.io/instance=svc-deadbeef", (
            "list_pvcs must be scoped to the deleted release's own PVCs via the "
            f"instance selector (data-loss #281), got {selector!r}"
        )

    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_delete_release_also_orphan_labels_datavolumes(
        self, mock_helm, mock_k8s, mock_cf, client
    ):
        """#423 W4.4: VM releases keep their disk in a CDI DataVolume.

        The chart stamps the DataVolume helm.sh/resource-policy: keep, so
        `helm uninstall` leaves it behind — it therefore needs the same
        orphan stamp as a PVC. And the cleanup cron must reclaim it via the
        DataVolume: deleting the DV-owned PVC alone makes CDI recreate it and
        re-run the whole import (measured in the W1 PoC, #423).
        """
        mock_helm.uninstall.return_value = {"message": "Release uninstalled"}
        mock_k8s.list_pvcs.return_value = ["svc-haos-root"]
        mock_k8s.list_datavolumes.return_value = ["svc-haos-root"]
        mock_cf.delete_route = AsyncMock(return_value=True)

        response = client.delete(
            "/api/releases/paas-ws-test/svc-haos?subdomain=paas-cs-test"
        )
        assert response.status_code == 200

        mock_k8s.label_datavolumes.assert_called_once()
        ns, names, labels = mock_k8s.label_datavolumes.call_args[0]
        assert ns == "paas-ws-test"
        assert names == ["svc-haos-root"]
        assert labels["paas.woowtech.io/orphaned-at"].isdigit()

        # Same #281 scoping rule as PVCs — a workspace ns is shared.
        call = mock_k8s.list_datavolumes.call_args
        selector = call.kwargs.get("label_selector")
        if selector is None and len(call.args) > 1:
            selector = call.args[1]
        assert selector == "app.kubernetes.io/instance=svc-haos"

    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_delete_release_survives_missing_cdi_crd(
        self, mock_helm, mock_k8s, mock_cf, client
    ):
        """Container-only clusters have no CDI — that must not break teardown,
        and must not stop the PVC half from being labeled."""
        mock_helm.uninstall.return_value = {"message": "Release uninstalled"}
        mock_k8s.list_pvcs.return_value = ["svc-abc-data"]
        mock_k8s.list_datavolumes.side_effect = KubectlException(
            message="no CRD",
            command="kubectl get datavolumes",
            stderr='the server doesn\'t have a resource type "datavolumes"',
        )
        mock_cf.delete_route = AsyncMock(return_value=True)

        response = client.delete(
            "/api/releases/paas-ws-test/svc-abc?subdomain=paas-cs-test"
        )

        assert response.status_code == 200
        mock_k8s.label_pvcs.assert_called_once()
        mock_k8s.label_datavolumes.assert_not_called()

    @patch("src.api.releases.cloudflare_service.delete_route", new_callable=AsyncMock)
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_delete_release_mcp_route_matches_deploy_naming(
        self, mock_helm, mock_k8s, mock_delete_route, client
    ):
        """MCP sidecar route delete must target the SAME subdomain deploy created.

        Regression for operator #207: deploy builds the mcp route from
        generate_subdomain(ns, name)+'-mcp' (scheme A: '{release}-{ns_suffix}-mcp')
        while delete used the Odoo-passed subdomain+'-mcp' (scheme B:
        'paas-cs-...-mcp'). The two schemes never match → the mcp CF route is
        orphaned on every sidecar teardown. Delete must use generate_subdomain so
        it matches deploy. The MAIN route legitimately keeps using the caller
        subdomain — pin both so a future 'consistency' refactor can't re-break it.
        """
        from src.api.releases import cloudflare_service

        mock_helm.uninstall.return_value = {"message": "Release uninstalled"}
        mock_k8s.list_pvcs.return_value = []
        mock_k8s.has_datavolume_crd.return_value = True
        mock_k8s.list_datavolumes.return_value = []

        ns, name, caller_subdomain = "paas-ws-test", "svc-deadbeef", "paas-cs-abc-def"
        response = client.delete(
            f"/api/releases/{ns}/{name}?subdomain={caller_subdomain}"
        )
        assert response.status_code == 200

        deploy_mcp = cloudflare_service.generate_subdomain(ns, name) + "-mcp"  # scheme A
        called = [c.args[0] for c in mock_delete_route.call_args_list]

        assert deploy_mcp in called, (
            f"mcp route delete must target deploy naming {deploy_mcp!r}, got {called!r}"
        )
        assert f"{caller_subdomain}-mcp" not in called, (
            "mcp route delete must NOT use the caller subdomain scheme (orphan bug)"
        )
        # MAIN route still uses the caller-passed subdomain (must stay symmetric).
        assert caller_subdomain in called

    @patch("src.api.releases.helm_service")
    def test_rollback_release(self, mock_helm, client):
        """Test rollback release endpoint."""
        mock_helm.rollback.return_value = {"message": "Rollback successful"}

        response = client.post(
            "/api/releases/paas-ws-test/test-release/rollback",
            json={"revision": 1},
        )

        assert response.status_code == 200
        data = response.json()
        assert "successful" in data["message"].lower()

    @patch("src.api.releases.helm_service")
    def test_get_release_revisions(self, mock_helm, client):
        """Test get release revisions endpoint."""
        mock_helm.history.return_value = [
            ReleaseRevision(
                revision=1,
                updated="2024-01-01T00:00:00Z",
                status=ReleaseStatus.SUPERSEDED,
                chart="nginx-1.0.0",
                app_version="1.0.0",
                description="Install complete",
            ),
            ReleaseRevision(
                revision=2,
                updated="2024-01-02T00:00:00Z",
                status=ReleaseStatus.DEPLOYED,
                chart="nginx-1.1.0",
                app_version="1.1.0",
                description="Upgrade complete",
            ),
        ]

        response = client.get("/api/releases/paas-ws-test/test-release/revisions")

        assert response.status_code == 200
        data = response.json()
        assert len(data["revisions"]) == 2
        assert data["revisions"][1]["revision"] == 2

    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_get_release_status(self, mock_helm, mock_k8s, client):
        """Test get release status endpoint."""
        mock_helm.get.return_value = ReleaseInfo(
            name="test-release",
            namespace="paas-ws-test",
            revision=1,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
        )

        mock_k8s.get_pods.return_value = [
            PodInfo(
                name="test-pod-1",
                phase=PodPhase.RUNNING,
                ready="1/1",
                restarts=0,
                age="5h",
            )
        ]

        response = client.get("/api/releases/paas-ws-test/test-release/status")

        assert response.status_code == 200
        data = response.json()
        assert data["release"]["name"] == "test-release"
        assert len(data["pods"]) == 1
        assert data["pods"][0]["phase"] == "Running"


class TestSidecarEndpoints:
    """Test sidecar patch endpoints."""

    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    def test_patch_sidecar_creates_service(self, mock_k8s, mock_cf, client):
        """Test that sidecar patch creates K8s Service and returns MCP URLs."""
        mock_k8s.get_deployments.return_value = [
            {"name": "n8n-deploy", "namespace": "paas-ws-test"}
        ]
        mock_k8s.patch_deployment.return_value = {}
        mock_k8s.apply_manifest.return_value = {}
        mock_cf.enabled = False

        response = client.post(
            "/api/releases/paas-ws-test/test-n8n/sidecar",
            json={
                "container": {
                    "name": "n8n-mcp",
                    "image": "ghcr.io/czlonkowski/n8n-mcp:latest",
                    "ports": [{"containerPort": 3000, "protocol": "TCP"}],
                    "env": [
                        {"name": "MCP_MODE", "value": "http"},
                        {"name": "AUTH_TOKEN", "value": "test-token"},
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["container_name"] == "n8n-mcp"
        assert data["mcp_service_name"] == "test-n8n-mcp"
        assert data["mcp_internal_url"] == (
            "http://test-n8n-mcp.paas-ws-test.svc.cluster.local:3000"
        )
        # No Cloudflare URL since it's disabled
        assert data["mcp_endpoint_url"] is None

        # Verify K8s Service manifest was applied
        mock_k8s.apply_manifest.assert_called_once()
        manifest = mock_k8s.apply_manifest.call_args[0][0]
        assert manifest["kind"] == "Service"
        assert manifest["metadata"]["name"] == "test-n8n-mcp"
        assert manifest["spec"]["ports"][0]["port"] == 3000

    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    def test_patch_sidecar_does_not_create_public_cloudflare_route(self, mock_k8s, mock_cf, client):
        """#301: even when Cloudflare is ENABLED, the sidecar patch must NOT create a
        public {subdomain}-mcp CF route. That route was dead weight (Odoo drops the
        public URL, agent data-plane uses in-cluster DNS) and an unprotected public
        attack surface (no CF Access, NP allows cloudflared→sidecar, only a bearer
        token). The in-cluster ClusterIP Service is still created."""
        mock_k8s.get_deployments.return_value = [
            {"name": "n8n-deploy", "namespace": "paas-ws-test"}
        ]
        mock_k8s.patch_deployment.return_value = {}
        mock_k8s.apply_manifest.return_value = {}
        mock_cf.enabled = True
        mock_cf.create_route = AsyncMock()
        mock_cf.generate_subdomain = MagicMock(return_value="svc-abc-paas-ws-test")
        mock_cf.generate_service_url = MagicMock(return_value="http://x:3000")

        response = client.post(
            "/api/releases/paas-ws-test/test-n8n/sidecar",
            json={
                "container": {
                    "name": "n8n-mcp",
                    "image": "ghcr.io/czlonkowski/n8n-mcp:latest",
                    "ports": [{"containerPort": 3000, "protocol": "TCP"}],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        # In-cluster Service still created (agent data-plane path)
        assert data["mcp_service_name"] == "test-n8n-mcp"
        assert data["mcp_internal_url"] == (
            "http://test-n8n-mcp.paas-ws-test.svc.cluster.local:3000"
        )
        mock_k8s.apply_manifest.assert_called_once()
        # But NO public CF route, even with Cloudflare enabled
        mock_cf.create_route.assert_not_called()
        assert data["mcp_endpoint_url"] is None

    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    def test_patch_sidecar_no_ports(self, mock_k8s, mock_cf, client):
        """Test sidecar patch without ports skips service creation."""
        mock_k8s.get_deployments.return_value = [
            {"name": "n8n-deploy", "namespace": "paas-ws-test"}
        ]
        mock_k8s.patch_deployment.return_value = {}
        mock_cf.enabled = False

        response = client.post(
            "/api/releases/paas-ws-test/test-n8n/sidecar",
            json={
                "container": {
                    "name": "helper",
                    "image": "busybox:latest",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["mcp_service_name"] is None
        assert data["mcp_endpoint_url"] is None
        assert data["mcp_internal_url"] is None
        mock_k8s.apply_manifest.assert_not_called()

    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    def test_patch_sidecar_service_creation_failure_non_fatal(self, mock_k8s, mock_cf, client):
        """Test that K8s Service creation failure doesn't fail the sidecar patch."""
        mock_k8s.get_deployments.return_value = [
            {"name": "n8n-deploy", "namespace": "paas-ws-test"}
        ]
        mock_k8s.patch_deployment.return_value = {}
        mock_k8s.apply_manifest.side_effect = Exception("apply failed")
        mock_cf.enabled = False

        response = client.post(
            "/api/releases/paas-ws-test/test-n8n/sidecar",
            json={
                "container": {
                    "name": "n8n-mcp",
                    "image": "ghcr.io/czlonkowski/n8n-mcp:latest",
                    "ports": [{"containerPort": 3000}],
                }
            },
        )

        # Sidecar patch itself succeeded
        assert response.status_code == 200
        data = response.json()
        assert data["container_name"] == "n8n-mcp"
        assert data["mcp_service_name"] == "test-n8n-mcp"
        # But URLs are None since service creation failed
        assert data["mcp_internal_url"] is None
        assert data["mcp_endpoint_url"] is None


class TestNamespaceEndpoints:
    """Test namespace management endpoints."""

    @patch("src.api.namespaces.k8s_service")
    def test_create_namespace(self, mock_k8s, client):
        """Test create namespace endpoint."""
        mock_k8s.create_namespace.return_value = {
            "message": "Namespace paas-ws-new created"
        }

        response = client.post(
            "/api/namespaces",
            json={
                "name": "paas-ws-new",
                "cpu_limit": "2",
                "memory_limit": "4Gi",
                "storage_limit": "20Gi",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert "created" in data["message"]

    @patch("src.api.namespaces.k8s_service")
    def test_create_namespace_invalid_name(self, mock_k8s, client):
        """Test create namespace with invalid name."""
        mock_k8s.create_namespace.side_effect = ValueError("Invalid namespace")

        response = client.post(
            "/api/namespaces",
            json={
                "name": "invalid",
                "cpu_limit": "2",
                "memory_limit": "4Gi",
                "storage_limit": "20Gi",
            },
        )

        assert response.status_code == 400

    @patch("src.api.namespaces.k8s_service")
    def test_put_quota_ok(self, mock_k8s, client):
        """#278 A1：PUT quota 端點正常轉呼 update_namespace_quota。"""
        mock_k8s.update_namespace_quota.return_value = {"message": "ok"}
        r = client.put(
            "/api/namespaces/paas-ws-test-abc/quota",
            json={"cpu_limit": "12", "memory_limit": "12Gi", "storage_limit": "150Gi"},
        )
        assert r.status_code == 200
        mock_k8s.update_namespace_quota.assert_called_once_with(
            "paas-ws-test-abc", "12", "12Gi", "150Gi")

    @patch("src.api.namespaces.k8s_service")
    def test_put_quota_bad_ns_400(self, mock_k8s, client):
        """#278 A1：prefix 守衛 raise ValueError → 400。"""
        mock_k8s.update_namespace_quota.side_effect = ValueError("bad prefix")
        r = client.put(
            "/api/namespaces/xxx/quota",
            json={"cpu_limit": "8", "memory_limit": "8Gi", "storage_limit": "100Gi"},
        )
        assert r.status_code == 400

    @patch("src.api.namespaces.k8s_service")
    def test_backfill_baseline_netpol(self, mock_k8s, client):
        """#279 P5: baseline-netpol backfill applies NP bundle + re-stamps labels."""
        mock_k8s.stamp_managed_ns_labels.return_value = {
            "paas-managed": "true", "woow.tw/workspace-id": "7"}
        resp = client.post(
            "/api/namespaces/paas-ws-x/baseline-netpol", json={"workspace_id": 7})
        assert resp.status_code == 200, resp.text
        mock_k8s._apply_baseline_netpol.assert_called_once_with("paas-ws-x", 7)
        mock_k8s.stamp_managed_ns_labels.assert_called_once_with(
            "paas-ws-x", workspace_id=7)
        assert resp.json()["labels"]["woow.tw/workspace-id"] == "7"

    @patch("src.api.namespaces.k8s_service")
    def test_backfill_baseline_netpol_invalid_ns_400(self, mock_k8s, client):
        mock_k8s._apply_baseline_netpol.side_effect = ValueError("bad prefix")
        resp = client.post(
            "/api/namespaces/nope/baseline-netpol", json={"workspace_id": 7})
        assert resp.status_code == 400


class TestAuthentication:
    """Test API key authentication."""

    def test_missing_api_key(self):
        """Test request without API key when auth is enabled."""
        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "secret-key"
            mock_settings.namespace_prefix = "paas-ws-"

            client = TestClient(app)
            response = client.get("/api/releases/paas-ws-test/test")

            assert response.status_code == 401

    def test_valid_api_key(self):
        """Test request with valid API key."""
        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "secret-key"
            mock_settings.namespace_prefix = "paas-ws-"

            client = TestClient(app)
            with patch("src.api.releases.helm_service") as mock_helm:
                mock_helm.get.return_value = ReleaseInfo(
                    name="test",
                    namespace="paas-ws-test",
                    revision=1,
                    status=ReleaseStatus.DEPLOYED,
                    chart="nginx",
                    app_version="1.0.0",
                    updated="2024-01-01T00:00:00Z",
                )

                response = client.get(
                    "/api/releases/paas-ws-test/test",
                    headers={"X-API-Key": "secret-key"},
                )

                assert response.status_code == 200

    def test_health_bypass_auth(self):
        """Test that health endpoint bypasses authentication."""
        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "secret-key"

            client = TestClient(app)
            response = client.get("/health")

            # Should succeed without API key
            assert response.status_code == 200


class TestFullDeploymentFlow:
    """Test complete deployment lifecycle."""

    @patch("src.api.releases.k8s_service")
    @patch("src.api.namespaces.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_full_deployment_flow(self, mock_helm, mock_ns_k8s, mock_releases_k8s, client):
        """Test complete flow: namespace → install → status."""
        # Step 1: Create namespace
        mock_ns_k8s.create_namespace.return_value = {
            "message": "Namespace paas-ws-test created"
        }

        namespace_response = client.post(
            "/api/namespaces",
            json={
                "name": "paas-ws-test",
                "cpu_limit": "2",
                "memory_limit": "4Gi",
                "storage_limit": "20Gi",
            },
        )
        assert namespace_response.status_code == 201

        # Step 2: Install release
        mock_helm.install.return_value = ReleaseInfo(
            name="test-app",
            namespace="paas-ws-test",
            revision=1,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
        )

        install_response = client.post(
            "/api/releases",
            json={
                "namespace": "paas-ws-test",
                "name": "test-app",
                "chart": "nginx",
                "version": "1.0.0",
            },
        )
        assert install_response.status_code == 201
        data = install_response.json()
        assert data["name"] == "test-app"
        assert data["status"] == "deployed"

        # Step 3: Check status
        mock_helm.get.return_value = ReleaseInfo(
            name="test-app",
            namespace="paas-ws-test",
            revision=1,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
        )
        mock_releases_k8s.get_pods.return_value = [
            PodInfo(
                name="test-pod-1",
                phase=PodPhase.RUNNING,
                ready="1/1",
                restarts=0,
                age="5m",
            )
        ]

        status_response = client.get("/api/releases/paas-ws-test/test-app/status")
        assert status_response.status_code == 200
        status_data = status_response.json()
        assert status_data["release"]["name"] == "test-app"
        assert len(status_data["pods"]) == 1

    @patch("src.api.releases.helm_service")
    def test_upgrade_flow(self, mock_helm, client):
        """Test upgrade flow."""
        # Install initial version
        mock_helm.install.return_value = ReleaseInfo(
            name="test-app",
            namespace="paas-ws-test",
            revision=1,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
        )

        client.post(
            "/api/releases",
            json={
                "namespace": "paas-ws-test",
                "name": "test-app",
                "chart": "nginx",
            },
        )

        # Upgrade to new version
        mock_helm.upgrade.return_value = ReleaseInfo(
            name="test-app",
            namespace="paas-ws-test",
            revision=2,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="2.0.0",
            updated="2024-01-02T00:00:00Z",
        )

        upgrade_response = client.patch(
            "/api/releases/paas-ws-test/test-app",
            json={
                "version": "2.0.0",
                "values": {"replicas": 3},
            },
        )

        assert upgrade_response.status_code == 200
        data = upgrade_response.json()
        assert data["revision"] == 2

    @patch("src.api.releases.helm_service")
    def test_rollback_flow(self, mock_helm, client):
        """Test rollback flow."""
        # Setup: release is at revision 2
        mock_helm.get.return_value = ReleaseInfo(
            name="test-app",
            namespace="paas-ws-test",
            revision=2,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="2.0.0",
            updated="2024-01-02T00:00:00Z",
        )

        # Rollback to revision 1
        mock_helm.rollback.return_value = {"message": "Rollback successful"}

        rollback_response = client.post(
            "/api/releases/paas-ws-test/test-app/rollback",
            json={"revision": 1},
        )

        assert rollback_response.status_code == 200
        data = rollback_response.json()
        assert "successful" in data["message"].lower()

    @patch("src.api.releases.helm_service")
    def test_delete_flow(self, mock_helm, client):
        """Test uninstall and cleanup flow."""
        # Uninstall release
        mock_helm.uninstall.return_value = {"message": "Release uninstalled"}

        delete_response = client.delete("/api/releases/paas-ws-test/test-app")

        assert delete_response.status_code == 200
        data = delete_response.json()
        assert "uninstalled" in data["message"].lower()

        # Verify release is gone
        from src.services.helm import HelmException

        mock_helm.get.side_effect = HelmException(
            message="Not found",
            command="helm get",
            stderr="Error: release not found",
        )

        get_response = client.get("/api/releases/paas-ws-test/test-app")
        assert get_response.status_code == 404
