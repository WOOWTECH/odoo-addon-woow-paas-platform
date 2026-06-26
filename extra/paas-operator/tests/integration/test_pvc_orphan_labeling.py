"""Integration test for operator #208: the orphaned-at PVC label must survive
REAL kubectl so the pvc-cleanup cron's label selector actually matches.

The unit suite mocks ``subprocess.run`` and therefore CANNOT exercise kubectl's
own label-value validation — that exact blind spot let a ':'-bearing RFC3339
value brick PVC garbage collection for 74 days (every postgres-backed service
leaked its data PVC). These tests shell out to real kubectl against a real
cluster: they are the load-bearing regression the mocks can't provide.

Run::

    kubectl config use-context docker-desktop
    pytest -m integration tests/integration/test_pvc_orphan_labeling.py -v
"""
import subprocess

import pytest

from src.api.releases import build_orphan_pvc_labels
from src.services.helm import KubernetesService, validate_label_value

pytestmark = pytest.mark.integration

# Exactly the selector the pvc-cleanup CronJob uses (woow-paas-charts
# charts/paas-operator/templates/pvc-cleanup-cronjob.yaml: `-l <this>`).
_CRON_SELECTOR = "paas.woowtech.io/orphaned-at"


def _make_pvc(namespace: str, name: str) -> str:
    return f"""
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {name}
  namespace: {namespace}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 8Mi
"""


def _apply_pvc(namespace: str, name: str) -> None:
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=_make_pvc(namespace, name).encode(),
        check=True, capture_output=True,
    )


def test_orphan_label_is_selectable_by_cleanup_cron(test_namespace):
    """The production teardown labels must make a leftover PVC match the cron
    selector — the assertion a mock structurally cannot make (real kubectl
    enforces label-value syntax; a mock returns success regardless)."""
    pvc = "data-svc-deadbeef-postgresql-0"
    _apply_pvc(test_namespace, pvc)

    # Exercise the EXACT value production + labeling path the teardown uses.
    k8s = KubernetesService()
    k8s.label_pvcs(test_namespace, [pvc], build_orphan_pvc_labels("svc-deadbeef", 7))

    selected = subprocess.check_output(
        ["kubectl", "get", "pvc", "-n", test_namespace,
         "-l", _CRON_SELECTOR,
         "-o", "jsonpath={.items[*].metadata.name}"],
    ).decode()
    assert pvc in selected, (
        f"orphaned-at selector matched nothing in {test_namespace} — the cron "
        f"GC would never see {pvc!r} (this is the operator #208 failure mode)"
    )

    # All three labels must land (atomic command — one bad value drops the batch).
    value = subprocess.check_output(
        ["kubectl", "get", "pvc", pvc, "-n", test_namespace,
         "-o", r"jsonpath={.metadata.labels.paas\.woowtech\.io/orphaned-at}"],
    ).decode()
    assert value.isdigit(), f"orphaned-at should be epoch seconds, got {value!r}"
    grace = subprocess.check_output(
        ["kubectl", "get", "pvc", pvc, "-n", test_namespace,
         "-o", r"jsonpath={.metadata.labels.paas\.woowtech\.io/grace-period-days}"],
    ).decode()
    assert grace == "7"


def test_real_kubectl_rejects_colon_label_value(test_namespace):
    """Pin the root cause AND prove the validator's regex matches real kubectl.

    ``validate_label_value`` rejects a ':'-bearing value in pure Python (so the
    bug can't reach kubectl again); this confirms real kubectl agrees it is
    illegal — a colon value the validator let through but kubectl rejected would
    be a silent contract drift (cf. operator-cf-access-delete-202 mock pitfall).
    """
    bad = "2026-06-20T08:03:00Z"  # the original orphaned-at value

    with pytest.raises(ValueError):
        validate_label_value("paas.woowtech.io/orphaned-at", bad)

    pvc = "data-colon-probe-0"
    _apply_pvc(test_namespace, pvc)
    res = subprocess.run(
        ["kubectl", "label", "pvc", pvc, "-n", test_namespace, "--overwrite",
         f"paas.woowtech.io/orphaned-at={bad}"],
        capture_output=True,
    )
    assert res.returncode != 0, "real kubectl must reject a ':'-bearing label value"
    assert b"invalid" in res.stderr.lower()
