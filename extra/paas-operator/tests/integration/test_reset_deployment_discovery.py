"""#357 C04: a Helm release name is not its Deployment name — real kubectl.

The reset saga restarted ``deployment/{release}`` while the mcp-server chart
renders its Deployment as ``{release}-mcp-server``. The unit suite could not
catch it: it mocks ``k8s_service``, so the assertion at
``tests/test_reset_config.py:70`` simply recorded whatever name the handler
passed — the bug *was* the contract, and 9/9 stayed green.

This is the same BUG-7/8/9 family the integration suite exists for
(``tests/integration/README.md``): the mock layer hides exactly the boundary
that drifts. ``test_restart_by_release_name_fails_the_way_production_did``
pins the real consequence rather than the mocked shape.
"""
import subprocess

import pytest

from src.services.helm import KubectlException, KubernetesService


pytestmark = pytest.mark.integration


class TestDeploymentNameDiscovery:

    def test_discovers_chart_fullname_not_release_name(self, chart_fullname_release):
        ns = chart_fullname_release["namespace"]
        release = chart_fullname_release["release"]

        resolved = KubernetesService().get_deployment_name(
            namespace=ns,
            label_selector=f"app.kubernetes.io/instance={release}",
        )

        assert resolved == chart_fullname_release["name"]
        # The whole point: discovery must not collapse back onto the release name.
        assert resolved != release

    def test_release_name_is_not_a_deployment(self, chart_fullname_release):
        """Ground truth for the assertion above, straight from the API server."""
        ns = chart_fullname_release["namespace"]
        release = chart_fullname_release["release"]

        probe = subprocess.run(
            ["kubectl", "get", "deployment", release, "--namespace", ns],
            capture_output=True,
        )

        assert probe.returncode != 0
        assert b"not found" in probe.stderr.lower()

    def test_restart_by_release_name_fails_the_way_production_did(
        self, chart_fullname_release,
    ):
        """Pre-fix behaviour, pinned against a live cluster.

        This is what the mocked suite asserted was correct. On the main path it
        surfaced as a 500; inside ``_rollback_reset`` the same call poisoned the
        compensation too, turning a recoverable failure into
        'operator intervention required'.
        """
        ns = chart_fullname_release["namespace"]
        release = chart_fullname_release["release"]

        with pytest.raises(KubectlException):
            KubernetesService().rollout_restart_deployment(
                namespace=ns,
                deployment_name=release,
            )

    def test_restart_by_discovered_name_succeeds(self, chart_fullname_release):
        """And the fixed path completes a real rollout end to end."""
        ns = chart_fullname_release["namespace"]
        release = chart_fullname_release["release"]
        k8s = KubernetesService()

        deployment_name = k8s.get_deployment_name(
            namespace=ns,
            label_selector=f"app.kubernetes.io/instance={release}",
        )
        k8s.rollout_restart_deployment(namespace=ns, deployment_name=deployment_name)
        k8s.rollout_status_deployment(
            namespace=ns, deployment_name=deployment_name, timeout_seconds=90,
        )

        restarted_at = subprocess.check_output([
            "kubectl", "get", "deployment", deployment_name, "--namespace", ns,
            "--output",
            "jsonpath={.spec.template.metadata.annotations.kubectl\\.kubernetes\\.io/restartedAt}",
        ])
        assert restarted_at.strip(), "rollout restart left no restartedAt annotation"

    def test_raises_when_no_deployment_matches(self, test_namespace):
        with pytest.raises(KubectlException) as error:
            KubernetesService().get_deployment_name(
                namespace=test_namespace,
                label_selector="app.kubernetes.io/instance=svc-does-not-exist",
            )

        assert "No deployments found" in error.value.message

    def test_ambiguous_selector_raises(self, ambiguous_instance_label):
        """Two matches must fail loudly, not silently restart items[0].

        Today's chart renders one Deployment per release, so this branch is
        unreachable in production — which is precisely why it needs a test
        rather than a comment asserting the assumption holds.
        """
        ns = ambiguous_instance_label["namespace"]
        release = ambiguous_instance_label["release"]

        with pytest.raises(KubectlException) as error:
            KubernetesService().get_deployment_name(
                namespace=ns,
                label_selector=f"app.kubernetes.io/instance={release}",
            )

        assert "Ambiguous selector" in error.value.message
        for name in ambiguous_instance_label["names"]:
            assert name in error.value.message
