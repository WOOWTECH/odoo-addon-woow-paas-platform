"""Integration test for operator #317: chart-owned PVCs must survive teardown.

helm decides deletion on `helm uninstall` from the annotation in the STORED
release manifest, NOT the live object (verified: helm 3.13.3). So a chart-owned
PVC (a plain PVC template, e.g. 8gears n8n data) shipping without
`helm.sh/resource-policy: keep` is deleted outright — and the post-teardown
orphan-labeling had nothing left to stamp. The fix is a helm post-renderer that
bakes keep into the manifest at install time (so uninstall skips the PVC).

The unit suite mocks helm, so it cannot exercise helm's real resource-policy
honouring — the exact blind spot this file covers. It shells out to real helm 3 +
kubectl through the operator's own HTTP endpoints (docker-desktop for dev).

Run::

    kubectl config use-context docker-desktop
    HELM_BINARY=<helm3>  # must be helm 3.x (operator image = 3.13.3)
    HELM_BINARY=$HELM_BINARY .venv/bin/python -m pytest -m integration \
        tests/integration/test_pvc_keep_before_uninstall.py -v
"""
import subprocess

import pytest

from tests.namespace_scope import integration_storage_class_yaml

pytestmark = pytest.mark.integration

_ORPHAN_AT = "paas.woowtech.io/orphaned-at"


def _write_chart_owned_pvc_chart(chart_dir) -> str:
    """Minimal chart: a plain (chart-owned) PVC with the helm instance label and
    NO resource-policy (like 8gears n8n data), plus a ConfigMap to prove the
    post-renderer's scope (only the PVC gets keep). Returns chart path."""
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: pvc-keep-probe\nversion: 0.1.0\n"
    )
    templates = chart_dir / "templates"
    templates.mkdir(exist_ok=True)
    (templates / "pvc.yaml").write_text(
        "apiVersion: v1\n"
        "kind: PersistentVolumeClaim\n"
        "metadata:\n"
        "  name: {{ .Release.Name }}-data\n"
        "  labels:\n"
        "    app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "spec:\n"
        f"{integration_storage_class_yaml()}"
        "  accessModes: [ReadWriteOnce]\n"
        "  resources:\n"
        "    requests:\n"
        "      storage: 8Mi\n"
    )
    (templates / "cm.yaml").write_text(
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: {{ .Release.Name }}-cfg\n"
        "  labels:\n"
        "    app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "data:\n"
        "  hello: world\n"
    )
    return str(chart_dir)


def _get(namespace: str, kind: str, name: str, jsonpath: str) -> str:
    return subprocess.run(
        ["kubectl", "get", kind, name, "-n", namespace, "-o", jsonpath],
        capture_output=True, text=True,
    ).stdout


def test_operator_install_keeps_chart_owned_pvc_through_teardown(
    app_client, test_namespace, tmp_path
):
    """End-to-end through operator HTTP: install a chart-owned PVC, then delete.
    The post-renderer must bake keep at install (PVC only, not the ConfigMap), and
    the PVC must survive uninstall and get orphan-labeled by the existing teardown.

    With the bug present the PVC would be GONE after delete (helm deletes it), so a
    bare "PVC still exists" check is itself the #317 guard.
    """
    chart = _write_chart_owned_pvc_chart(tmp_path / "chart")
    release = "probe-n8n"
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": release, "chart": chart,
    })
    assert r.status_code == 201, r.text

    pvc = f"{release}-data"
    # post-renderer stamped keep at install — on the PVC …
    keep = _get(test_namespace, "pvc", pvc,
                r"jsonpath={.metadata.annotations.helm\.sh/resource-policy}")
    assert keep == "keep", f"post-renderer must stamp keep on the PVC, got {keep!r}"
    # … and NOT on the ConfigMap (scope rule).
    cm_keep = _get(test_namespace, "cm", f"{release}-cfg",
                   r"jsonpath={.metadata.annotations.helm\.sh/resource-policy}")
    assert cm_keep == "", f"post-renderer must NOT touch non-PVC resources, got {cm_keep!r}"

    # Tear down through the operator (helm uninstall + existing orphan-labeling).
    r = app_client.delete(f"/api/releases/{test_namespace}/{release}")
    assert r.status_code == 200, r.text

    # PVC survived helm uninstall (the #317 fix) …
    survived = _get(test_namespace, "pvc", pvc, "jsonpath={.metadata.name}")
    assert survived == pvc, (
        f"chart-owned PVC {pvc!r} was deleted by helm uninstall — #317 data-loss "
        "(keep not baked into the stored manifest at install)"
    )
    # … and the existing teardown path orphan-labeled it (leader boundary 3).
    orphaned = _get(test_namespace, "pvc", pvc,
                    r"jsonpath={.metadata.labels.paas\.woowtech\.io/orphaned-at}")
    assert orphaned.isdigit(), (
        f"leftover PVC must be orphan-labeled so the cleanup cron's -l {_ORPHAN_AT} "
        f"selector matches; got {orphaned!r}"
    )


def test_statefulset_volumeclaimtemplate_pvc_not_regressed(
    app_client, test_namespace, tmp_path
):
    """#281 non-regression: a StatefulSet volumeClaimTemplate PVC (helm never
    deletes it) must still be orphan-labeled after teardown. Proves the install-
    time post-renderer change did not disturb the pre-existing STS behaviour."""
    chart_dir = tmp_path / "sts-chart"
    (chart_dir / "templates").mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: sts-probe\nversion: 0.1.0\n"
    )
    (chart_dir / "templates" / "sts.yaml").write_text(
        "apiVersion: apps/v1\n"
        "kind: StatefulSet\n"
        "metadata:\n"
        "  name: {{ .Release.Name }}\n"
        "  labels:\n"
        "    app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "spec:\n"
        "  serviceName: {{ .Release.Name }}\n"
        "  replicas: 1\n"
        "  selector:\n"
        "    matchLabels:\n"
        "      app: {{ .Release.Name }}\n"
        "  template:\n"
        "    metadata:\n"
        "      labels:\n"
        "        app: {{ .Release.Name }}\n"
        "        app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "    spec:\n"
        "      containers:\n"
        "      - name: pause\n"
        "        image: registry.k8s.io/pause:3.9\n"
        "  volumeClaimTemplates:\n"
        "  - metadata:\n"
        "      name: data\n"
        "      labels:\n"
        "        app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "    spec:\n"
        f"{integration_storage_class_yaml(indent=6)}"
        "      accessModes: [ReadWriteOnce]\n"
        "      resources:\n"
        "        requests:\n"
        "          storage: 8Mi\n"
    )
    release = "probe-sts"
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": release, "chart": str(chart_dir),
    })
    assert r.status_code == 201, r.text

    # STS controller creates the PVC named data-{release}-0; wait for it to appear.
    pvc = f"data-{release}-0"
    appeared = False
    for _ in range(60):  # up to ~30s
        if _get(test_namespace, "pvc", pvc, "jsonpath={.metadata.name}") == pvc:
            appeared = True
            break
        subprocess.run(["sleep", "0.5"])
    assert appeared, f"STS volumeClaimTemplate PVC {pvc!r} never appeared"

    r = app_client.delete(f"/api/releases/{test_namespace}/{release}")
    assert r.status_code == 200, r.text

    survived = _get(test_namespace, "pvc", pvc, "jsonpath={.metadata.name}")
    assert survived == pvc, f"STS PVC {pvc!r} unexpectedly gone after teardown"
    orphaned = _get(test_namespace, "pvc", pvc,
                    r"jsonpath={.metadata.labels.paas\.woowtech\.io/orphaned-at}")
    assert orphaned.isdigit(), (
        f"STS PVC lost its orphan-at label after #317 — regression; got {orphaned!r}"
    )
