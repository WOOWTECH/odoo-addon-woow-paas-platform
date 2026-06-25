"""Integration test (L2): the namespace env label must survive REAL kubectl,
be selectable by ``kubectl get ns -l``, and ``validate_label_value`` must agree
with kubectl's own acceptance/rejection.

The unit suite mocks ``subprocess.run`` so it cannot exercise kubectl's own
label-value validation — the exact blind spot that let a ':'-bearing value brick
PVC GC for 74 days (operator #208). These shell out to real kubectl.

Run::

    kubectl config use-context docker-desktop
    pytest -m integration tests/integration/test_ns_env_labeling.py -v
"""
import subprocess

import pytest

from src.services.helm import build_managed_ns_labels, validate_label_value

pytestmark = pytest.mark.integration

_ENV_LABEL = "paas.woowtech.io/env"


def _label(namespace: str, key: str, value: str):
    return subprocess.run(
        ["kubectl", "label", "namespace", namespace, f"{key}={value}", "--overwrite"],
        capture_output=True,
    )


def test_env_label_lands_and_is_selectable(test_namespace, monkeypatch):
    """PAAS_ENV=stg → build_managed_ns_labels 的 env label 真貼上後，kubectl -l 選得到。"""
    monkeypatch.setattr("src.config.settings.paas_env", "stg")
    value = build_managed_ns_labels()[_ENV_LABEL]
    r = _label(test_namespace, _ENV_LABEL, value)
    assert r.returncode == 0, r.stderr.decode()
    selected = subprocess.check_output(
        ["kubectl", "get", "ns", "-l", f"{_ENV_LABEL}={value}",
         "-o", "jsonpath={.items[*].metadata.name}"],
    ).decode().split()
    assert test_namespace in selected


def test_whitelist_values_accepted_by_real_kubectl(test_namespace):
    """白名單值（prod/stg）validate 放行且真 kubectl 也接受（L1 白名單↔kubectl 一致）。"""
    for value in ("prod", "stg"):
        assert validate_label_value(_ENV_LABEL, value) is None
        r = _label(test_namespace, _ENV_LABEL, value)
        assert r.returncode == 0, r.stderr.decode()


def test_validator_and_kubectl_agree_on_illegal_value(test_namespace):
    """validate_label_value 拒的值，真 kubectl 也拒（validator↔kubectl 契約，#208 守線）。"""
    illegal = "paas-ws-stg-"  # 尾 dash → 非法 label value
    with pytest.raises(ValueError):
        validate_label_value(_ENV_LABEL, illegal)
    r = _label(test_namespace, _ENV_LABEL, illegal)
    assert r.returncode != 0, "real kubectl 應一致拒絕非法 label value"
