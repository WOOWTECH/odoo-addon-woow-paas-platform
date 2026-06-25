"""End-to-end: real HTTP → real kubectl → real K8s state changes.

These would have caught BUG-7/8/9 before they hit staging:

- ``test_scale_statefulset_only`` exercises the StatefulSet branch
  (BUG-7 — Bitnami releases were 404'ing because only Deployments were
  queried).
- ``params={"replicas": ...}`` everywhere documents the wire contract
  (BUG-8 — client was sending it as a body and the operator's Query default
  silently kicked in).
- When run with the operator's real ServiceAccount in CI, the kubectl
  scale call exercises the ``deployments/scale`` and ``statefulsets/scale``
  subresource permissions (BUG-9).
"""
import subprocess

import pytest


pytestmark = pytest.mark.integration


def _replicas(kind: str, name: str, namespace: str) -> int:
    out = subprocess.check_output([
        "kubectl", "get", kind, name, "-n", namespace,
        "-o", "jsonpath={.spec.replicas}",
    ])
    return int(out.decode() or "0")


class TestScaleDeployment:

    def test_scale_to_zero(self, app_client, deployment_release):
        ns = deployment_release["namespace"]
        rel = deployment_release["release"]
        name = deployment_release["name"]
        assert _replicas("deployment", name, ns) == 1

        r = app_client.post(
            f"/api/releases/{ns}/{rel}/scale",
            params={"replicas": 0},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Scaled 1 workload" in body["message"]
        assert body["deployments"] == name
        assert body["statefulsets"] == ""

        assert _replicas("deployment", name, ns) == 0

    def test_scale_back_up(self, app_client, deployment_release):
        ns = deployment_release["namespace"]
        rel = deployment_release["release"]
        name = deployment_release["name"]

        r0 = app_client.post(f"/api/releases/{ns}/{rel}/scale",
                             params={"replicas": 0})
        assert r0.status_code == 200

        r1 = app_client.post(f"/api/releases/{ns}/{rel}/scale",
                             params={"replicas": 1})
        assert r1.status_code == 200

        assert _replicas("deployment", name, ns) == 1


class TestScaleStatefulSet:
    """BUG-7 regression: Bitnami charts ship as StatefulSets."""

    def test_scale_to_zero(self, app_client, statefulset_release):
        ns = statefulset_release["namespace"]
        rel = statefulset_release["release"]
        name = statefulset_release["name"]
        assert _replicas("statefulset", name, ns) == 1

        r = app_client.post(
            f"/api/releases/{ns}/{rel}/scale",
            params={"replicas": 0},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Scaled 1 workload" in body["message"]
        assert body["statefulsets"] == name
        assert body["deployments"] == ""

        assert _replicas("statefulset", name, ns) == 0

    def test_scale_back_up(self, app_client, statefulset_release):
        ns = statefulset_release["namespace"]
        rel = statefulset_release["release"]
        name = statefulset_release["name"]

        app_client.post(f"/api/releases/{ns}/{rel}/scale",
                        params={"replicas": 0})
        r1 = app_client.post(f"/api/releases/{ns}/{rel}/scale",
                             params={"replicas": 1})
        assert r1.status_code == 200
        assert _replicas("statefulset", name, ns) == 1


class TestScaleMixed:
    """Real releases often pair an app Deployment with a backing StatefulSet."""

    def test_scale_both_kinds(self, app_client, mixed_release):
        ns = mixed_release["namespace"]
        rel = mixed_release["release"]
        dep = mixed_release["deployment_name"]
        sts = mixed_release["statefulset_name"]

        r = app_client.post(
            f"/api/releases/{ns}/{rel}/scale",
            params={"replicas": 0},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Scaled 2 workload" in body["message"]
        assert body["deployments"] == dep
        assert body["statefulsets"] == sts

        assert _replicas("deployment", dep, ns) == 0
        assert _replicas("statefulset", sts, ns) == 0


class TestScaleErrors:

    def test_404_when_no_workloads(self, app_client, test_namespace):
        r = app_client.post(
            f"/api/releases/{test_namespace}/nonexistent-rel/scale",
            params={"replicas": 0},
        )
        assert r.status_code == 404
        assert "No deployments or statefulsets" in r.json()["detail"]

    def test_replicas_in_body_is_ignored_so_query_is_authoritative(
        self, app_client, deployment_release,
    ):
        """BUG-8 contract: ``replicas`` is read from the URL query string.

        Sending it in the body alone (as the pre-fix Odoo client did) silently
        falls back to ``Query(default=0)`` — i.e. always scales to 0. This test
        documents the contract: query wins, body is decoration.
        """
        ns = deployment_release["namespace"]
        rel = deployment_release["release"]
        name = deployment_release["name"]

        # Body says 5, query says 1. Server must honor the query.
        r = app_client.post(
            f"/api/releases/{ns}/{rel}/scale",
            params={"replicas": 1},
            json={"replicas": 5},
        )
        assert r.status_code == 200
        assert _replicas("deployment", name, ns) == 1
