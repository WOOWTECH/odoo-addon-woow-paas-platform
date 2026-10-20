"""#1407: upgrades must not die on immutable fields — reuse live values, refuse the rest.

Seen on prod: six home-assistant-full config PVCs handed off from woowhademo carry
``storageClassName: longhorn`` while the platform injects ``longhorn-delete``; helm's
three-way merge tried to "correct" it, the API server refused, the release went
``failed`` after the Deployment had already been patched to the new chart, and the
admin only saw "Helm upgrade failed. Check operator logs for details."
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from src.models.schemas import ReleaseInfo, ReleaseStatus
from src.pvc_keep_post_renderer import (
    _parse_snapshot,
    live_snapshot,
    parse_quantity,
    reuse_live_values,
)
from src.services import helm as helm_module
from src.services.helm import HelmException, HelmService, _live_objects
from src.services.upgrade_preflight import (
    describe_conflicts,
    describe_helm_failure,
    find_immutable_conflicts,
)

_SCRIPT = Path(__file__).resolve().parent.parent / "src" / "pvc_keep_post_renderer.py"


def _pvc(name, sc=None, size="10Gi", modes=("ReadWriteOnce",), volume_mode=None):
    spec = {"accessModes": list(modes), "resources": {"requests": {"storage": size}}}
    if sc is not None:
        spec["storageClassName"] = sc
    if volume_mode:
        spec["volumeMode"] = volume_mode
    return {"kind": "PersistentVolumeClaim", "metadata": {"name": name}, "spec": spec}


def _sts(name, templates, selector=None, service_name="svc"):
    return {
        "kind": "StatefulSet", "metadata": {"name": name},
        "spec": {
            "serviceName": service_name,
            "selector": {"matchLabels": selector or {"app": name}},
            "volumeClaimTemplates": [
                {"metadata": {"name": t}, "spec": _pvc(t, sc, size)["spec"]}
                for t, sc, size in templates
            ],
        },
    }


# --- renderer: live-value reuse ------------------------------------------------

def test_handoff_pvc_keeps_live_storage_class():
    """The prod case: render longhorn-delete, live longhorn → render longhorn."""
    snap = live_snapshot([_pvc("svc-800e44fe-home-assistant-full-config", "longhorn")])
    out = reuse_live_values([_pvc("svc-800e44fe-home-assistant-full-config", "longhorn-delete")], snap)
    assert out[0]["spec"]["storageClassName"] == "longhorn"


def test_live_class_filled_in_when_chart_omits_it():
    snap = live_snapshot([_pvc("d", "longhorn")])
    out = reuse_live_values([_pvc("d")], snap)
    assert out[0]["spec"]["storageClassName"] == "longhorn"


def test_access_modes_and_volume_mode_follow_live():
    snap = live_snapshot([_pvc("d", "x", modes=("ReadWriteMany",), volume_mode="Block")])
    out = reuse_live_values([_pvc("d", "x", volume_mode="Filesystem")], snap)
    assert out[0]["spec"]["accessModes"] == ["ReadWriteMany"]
    assert out[0]["spec"]["volumeMode"] == "Block"


def test_size_never_shrinks_but_may_grow():
    snap = live_snapshot([_pvc("a", "x", size="20Gi"), _pvc("b", "x", size="5Gi")])
    out = reuse_live_values([_pvc("a", "x", size="10Gi"), _pvc("b", "x", size="10Gi")], snap)
    assert out[0]["spec"]["resources"]["requests"]["storage"] == "20Gi"
    assert out[1]["spec"]["resources"]["requests"]["storage"] == "10Gi"


def test_new_objects_render_untouched():
    snap = live_snapshot([_pvc("other", "longhorn")])
    out = reuse_live_values([_pvc("new", "longhorn-delete")], snap)
    assert out[0] == _pvc("new", "longhorn-delete")


def test_statefulset_claim_templates_follow_live():
    snap = live_snapshot([_sts("db", [("data", "longhorn", "8Gi")])])
    out = reuse_live_values([_sts("db", [("data", "longhorn-delete", "8Gi")])], snap)
    spec = out[0]["spec"]["volumeClaimTemplates"][0]["spec"]
    assert spec["storageClassName"] == "longhorn"


def test_non_pvc_with_same_name_untouched():
    cm = {"kind": "ConfigMap", "metadata": {"name": "d"}, "data": {"k": "v"}}
    snap = live_snapshot([_pvc("d", "longhorn")])
    assert reuse_live_values([dict(cm)], snap) == [cm]


def test_parse_quantity():
    assert parse_quantity("10Gi") == 10 * 2 ** 30
    assert parse_quantity("500M") == 500 * 10 ** 6
    assert parse_quantity("1073741824") == 2 ** 30
    assert parse_quantity(None) is None
    assert parse_quantity("lots") is None


def test_parse_snapshot_optional():
    assert _parse_snapshot(["prog"]) == {}
    assert _parse_snapshot(["prog", ""]) == {}
    with pytest.raises(ValueError):
        _parse_snapshot(["prog", "[]"])


def test_cli_with_snapshot_reuses_and_still_stamps_keep():
    manifest = yaml.safe_dump_all([_pvc("d", "longhorn-delete"), _pvc("fresh", "longhorn-delete")])
    snap = live_snapshot([_pvc("d", "longhorn")])
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), json.dumps(snap)],
        input=manifest, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    out = {d["metadata"]["name"]: d for d in yaml.safe_load_all(r.stdout) if d}
    assert out["d"]["spec"]["storageClassName"] == "longhorn"
    assert out["fresh"]["spec"]["storageClassName"] == "longhorn-delete"
    for d in out.values():
        assert d["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert "reusing live value" in r.stderr


# --- HelmService wiring ------------------------------------------------------------

_RELEASE = {
    "name": "r", "namespace": "paas-ws-test",
    "version": 2, "info": {"status": "deployed", "last_deployed": ""},
    "chart": {"metadata": {"name": "home-assistant-full", "appVersion": "1"}},
    "manifest": "kind: ConfigMap\nmetadata:\n  name: c\n",
}
_LIVE = json.dumps({"items": [
    {"kind": "PersistentVolumeClaim", "metadata": {"name": "r-config"},
     "spec": {"storageClassName": "longhorn", "accessModes": ["ReadWriteOnce"],
              "resources": {"requests": {"storage": "10Gi"}}}},
]})


def _fake_run(live_stdout=_LIVE, live_rc=0):
    def run(cmd, **kwargs):
        if cmd[0] == "kubectl":
            assert cmd[1] == "get" and "paas-ws-test" in cmd
            return Mock(returncode=live_rc, stdout=live_stdout, stderr="boom")
        return Mock(returncode=0, stdout=json.dumps(_RELEASE), stderr="")
    return run


def _helm_cmds(mock_run):
    return [c[0][0] for c in mock_run.call_args_list if c[0][0][0] != "kubectl"]


# Captured at import (collection) time — before conftest's autouse
# ``live_objects_lookup`` stubs the seam for every test.
_REAL_SNAPSHOT = helm_module._live_snapshot


@pytest.fixture
def real_snapshot():
    """Undo the conftest stub: the real lookup, over a mocked ``subprocess.run``."""
    with patch("src.services.helm._live_snapshot", _REAL_SNAPSHOT):
        yield


@patch("subprocess.run")
def test_upgrade_hands_live_snapshot_to_renderer(mock_run, real_snapshot):
    mock_run.side_effect = _fake_run()
    HelmService().upgrade(namespace="paas-ws-test", name="r", chart="ha",
                          version="0.1.5", reset_values=True)
    cmd = _helm_cmds(mock_run)[-1]
    i = cmd.index("--post-renderer-args")
    assert cmd[i + 1].endswith("pvc_keep_post_renderer.py")
    assert cmd[i + 2] == "--post-renderer-args"
    assert json.loads(cmd[i + 3])["pvc"]["r-config"]["storageClassName"] == "longhorn"


@patch("subprocess.run")
def test_install_hands_live_snapshot_to_renderer(mock_run, real_snapshot):
    mock_run.side_effect = _fake_run()
    HelmService().install(namespace="paas-ws-test", name="r", chart="ha")
    cmd = _helm_cmds(mock_run)[-1]
    assert "r-config" in json.loads(cmd[cmd.index("--post-renderer-args") + 3])["pvc"]


@patch("subprocess.run")
def test_lookup_failure_degrades_to_previous_behaviour(mock_run, real_snapshot):
    """A failed live lookup must never fail the upgrade — renderer just gets no snapshot."""
    mock_run.side_effect = _fake_run(live_rc=1)
    HelmService().upgrade(namespace="paas-ws-test", name="r", chart="ha")
    assert _helm_cmds(mock_run)[-1].count("--post-renderer-args") == 1


@patch("subprocess.run")
def test_nothing_live_adds_no_extra_arg(mock_run, real_snapshot):
    mock_run.side_effect = _fake_run(live_stdout='{"items": []}')
    HelmService().upgrade(namespace="paas-ws-test", name="r", chart="ha")
    assert _helm_cmds(mock_run)[-1].count("--post-renderer-args") == 1


@patch("subprocess.run")
def test_dry_run_returns_manifest_and_applies_nothing(mock_run):
    mock_run.side_effect = _fake_run()
    manifest = HelmService().upgrade(namespace="paas-ws-test", name="r", chart="ha",
                                     dry_run=True)
    assert manifest == _RELEASE["manifest"]
    assert "--dry-run" in _helm_cmds(mock_run)[-1]


@patch("subprocess.run")
def test_live_objects_none_on_failure(mock_run):
    mock_run.side_effect = _fake_run(live_rc=1)
    assert _live_objects("paas-ws-test") is None


# --- preflight ---------------------------------------------------------------------

def test_no_conflict_after_reuse():
    live = [_pvc("d", "longhorn")]
    rendered = reuse_live_values([_pvc("d", "longhorn-delete")], live_snapshot(live))
    assert find_immutable_conflicts(rendered, live) == []


def test_unreused_storage_class_is_a_conflict():
    """Defence in depth: if a render still differs (renderer bypassed), refuse."""
    c = find_immutable_conflicts([_pvc("d", "longhorn-delete")], [_pvc("d", "longhorn")])
    assert [(x["kind"], x["name"], x["field"]) for x in c] == [
        ("PersistentVolumeClaim", "d", "storageClassName")]


def test_shrink_is_a_conflict():
    c = find_immutable_conflicts([_pvc("d", "x", size="5Gi")], [_pvc("d", "x", size="10Gi")])
    assert c[0]["field"] == "resources.requests.storage"


def test_selector_change_is_a_conflict():
    dep = lambda sel: {"kind": "Deployment", "metadata": {"name": "web"},  # noqa: E731
                       "spec": {"selector": {"matchLabels": sel}}}
    c = find_immutable_conflicts([dep({"app": "new"})], [dep({"app": "old"})])
    assert c[0]["field"] == "spec.selector.matchLabels"


def test_claim_template_added_is_a_conflict():
    live = [_sts("db", [("data", "longhorn", "8Gi")])]
    rendered = [_sts("db", [("data", "longhorn", "8Gi"), ("logs", "longhorn", "1Gi")])]
    c = find_immutable_conflicts(rendered, live)
    assert c[0]["field"] == "spec.volumeClaimTemplates"


def test_objects_not_live_are_ignored():
    assert find_immutable_conflicts([_pvc("new", "a")], [_pvc("old", "b")]) == []


def test_conflict_message_is_chinese_and_names_object_and_field():
    msg = describe_conflicts(find_immutable_conflicts(
        [_pvc("svc-x-config", "longhorn-delete")], [_pvc("svc-x-config", "longhorn")]))
    assert "沒有套用任何變更" in msg
    assert "svc-x-config" in msg and "storageClassName" in msg
    assert "longhorn" in msg and "longhorn-delete" in msg


# --- helm failure → Chinese --------------------------------------------------------

_PROD_STDERR = (
    'Error: UPGRADE FAILED: cannot patch "svc-800e44fe-home-assistant-full-config" with '
    'kind PersistentVolumeClaim: PersistentVolumeClaim "svc-800e44fe-home-assistant-full-config" '
    'is invalid: spec: Forbidden: spec is immutable after creation except resources.requests '
    'and volumeAttributesClassName for bound claims'
)


def test_prod_stderr_names_pvc_and_field():
    msg = describe_helm_failure(_PROD_STDERR)
    assert "svc-800e44fe-home-assistant-full-config" in msg
    assert "磁碟（PVC）" in msg and "spec" in msg and "不能修改" in msg


def test_unknown_failure_keeps_last_line():
    msg = describe_helm_failure("blah\nError: something odd happened")
    assert msg == "Helm 升級失敗：something odd happened"


def test_empty_stderr():
    assert "operator log" in describe_helm_failure("")


# --- endpoint ----------------------------------------------------------------------

_INFO = ReleaseInfo(name="r", namespace="paas-ws-test", revision=3,
                    status=ReleaseStatus.DEPLOYED, chart="ha", app_version="1",
                    updated="2024-01-02T00:00:00Z")


def _dry_run_aware(manifest_docs):
    def upgrade(**kwargs):
        if kwargs.get("dry_run"):
            return yaml.safe_dump_all(manifest_docs)
        return _INFO
    return upgrade


@patch("src.api.releases.helm_service")
def test_endpoint_refuses_conflict_before_applying(mock_helm, client, live_objects_lookup):
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-config", "longhorn-delete")])
    live_objects_lookup["objects"].return_value = [_pvc("r-config", "longhorn")]
    r = client.patch("/api/releases/paas-ws-test/r",
                     json={"chart": "ha", "version": "0.1.5", "reset_values": True})
    assert r.status_code == 409
    assert "r-config" in r.json()["detail"]
    # only the dry-run ran — nothing was applied
    assert [c.kwargs.get("dry_run") for c in mock_helm.upgrade.call_args_list] == [True]


@patch("src.api.releases.helm_service")
def test_endpoint_upgrades_when_clean(mock_helm, client, live_objects_lookup):
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-config", "longhorn")])
    live_objects_lookup["objects"].return_value = [_pvc("r-config", "longhorn")]
    r = client.patch("/api/releases/paas-ws-test/r",
                     json={"chart": "ha", "version": "0.1.5", "reset_values": True})
    assert r.status_code == 200
    assert [c.kwargs.get("dry_run") for c in mock_helm.upgrade.call_args_list] == [True, None]
    # both calls carry identical arguments apart from dry_run
    first, second = mock_helm.upgrade.call_args_list
    assert {k: v for k, v in first.kwargs.items() if k != "dry_run"} == second.kwargs


@patch("src.api.releases.helm_service")
def test_endpoint_helm_failure_detail_is_chinese(mock_helm, client):
    def upgrade(**kwargs):
        if kwargs.get("dry_run"):
            return ""
        raise HelmException(message="Helm command failed with code 1",
                            command="helm upgrade", stderr=_PROD_STDERR)
    mock_helm.upgrade.side_effect = upgrade
    r = client.patch("/api/releases/paas-ws-test/r", json={"chart": "ha"})
    assert r.status_code == 500
    assert "svc-800e44fe-home-assistant-full-config" in r.json()["detail"]
    assert "Check operator logs" not in r.json()["detail"]


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from src.main import app

    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})
