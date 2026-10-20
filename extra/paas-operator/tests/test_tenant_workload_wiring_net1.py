"""AI-FIX-NET-1：helm.py 把租戶政策接到 post-renderer 的方式（開關、例外、mirror-live）。"""
import json
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from src.config import Settings, settings
from src.services import helm as helm_module
from src.services.helm import HelmService, _pvc_keep_post_renderer_args, _tenant_policy

NS = "paas-ws-4ce0979e"
AVOID = "ubuntuserver-59mqj"


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "tenant_avoid_nodes", AVOID)
    monkeypatch.setattr(settings, "tenant_egress_gate_enabled", True)
    monkeypatch.setattr(settings, "tenant_baseline_egress_enabled", True)
    monkeypatch.setattr(settings, "psa_exempt_namespaces", "paas-ws-ab6564da")
    # 預設「讀不到叢集現況」：非 hook Job／Pod 的查詢不影響既有斷言（各測試自己 patch 覆寫）。
    with patch.object(helm_module, "_has_baseline_egress", return_value=True), \
            patch.object(helm_module, "_live_objects", return_value=None):
        yield


class TestDefaults:
    def test_everything_is_off_by_default(self):
        fresh = Settings()
        assert fresh.tenant_avoid_nodes == ""
        assert fresh.tenant_egress_gate_enabled is False
        assert fresh.tenant_egress_gate_on_timeout == "proceed"

    def test_off_means_no_policy_and_no_extra_argv(self):
        assert _tenant_policy(NS) == ({}, {})
        assert len(_pvc_keep_post_renderer_args({}, {}, {})) == 4      # --post-renderer X --post-renderer-args script

    def test_pvc_snapshot_only_argv_is_unchanged(self):
        args = _pvc_keep_post_renderer_args({"pvc": {"d": {"storage": "1Gi"}}})
        assert json.loads(args[-1]) == {"pvc": {"d": {"storage": "1Gi"}}}

    def test_validators(self):
        with pytest.raises(ValidationError):
            Settings(tenant_egress_gate_on_timeout="explode")
        with pytest.raises(ValidationError):
            Settings(tenant_egress_gate_timeout_seconds=1)
        assert Settings(tenant_egress_gate_on_timeout="fail").tenant_egress_gate_on_timeout == "fail"


class TestTenantPolicy:
    def test_both_switches(self, on):
        options, live = _tenant_policy(NS)
        assert options["avoid_nodes"] == [AVOID]
        assert options["egress_gate"]["image"] == "busybox:1.36"
        assert options["egress_gate"]["timeout_seconds"] == 60
        assert "mirror_live" not in options and live == {}

    def test_avoid_only_when_gate_off(self, on, monkeypatch):
        monkeypatch.setattr(settings, "tenant_egress_gate_enabled", False)
        options, _ = _tenant_policy(NS)
        assert options == {"avoid_nodes": [AVOID]}

    def test_gate_needs_baseline_egress_switch(self, on, monkeypatch):
        monkeypatch.setattr(settings, "tenant_baseline_egress_enabled", False)
        options, _ = _tenant_policy(NS)
        assert "egress_gate" not in options and options["avoid_nodes"] == [AVOID]

    def test_gate_skipped_when_namespace_has_no_baseline_policy(self, on):
        with patch.object(helm_module, "_has_baseline_egress", return_value=False):
            options, _ = _tenant_policy(NS)
        assert "egress_gate" not in options and options["avoid_nodes"] == [AVOID]

    def test_psa_exempt_namespace_gets_nothing(self, on):
        assert _tenant_policy("paas-ws-ab6564da") == ({}, {})

    def test_non_tenant_namespace_gets_nothing(self, on):
        assert _tenant_policy("paas-system") == ({}, {})
        assert _tenant_policy("kube-system") == ({}, {})

    def test_several_avoid_nodes(self, on, monkeypatch):
        monkeypatch.setattr(settings, "tenant_avoid_nodes", " a , b,a ")
        assert _tenant_policy(NS)[0]["avoid_nodes"] == ["a", "b"]

    def test_mirror_reads_live_workloads(self, on):
        live_items = [{"kind": "Deployment", "metadata": {"name": "web"},
                       "spec": {"template": {"spec": {"containers": [], "initContainers": [{"name": "woow-egress-gate"}]}}}}]
        with patch.object(helm_module, "_live_objects", return_value=live_items) as lo:
            options, live = _tenant_policy(NS, mirror_live=True)
        assert lo.call_args[0][1] == "deployments,statefulsets,daemonsets,cronjobs"
        assert options["mirror_live"] is True
        assert live == {"Deployment/web": {"gate": True, "avoid": False}}

    def test_immutable_workloads_are_read_on_every_install_and_upgrade(self, on):
        """netpol-startup-window：非 hook Job／bare Pod 要知道叢集上有沒有同名物件才能插 gate。"""
        job = {"kind": "Job", "metadata": {"name": "init-db"},
               "spec": {"template": {"spec": {"containers": []}}}}
        with patch.object(helm_module, "_live_objects", return_value=[job]) as lo:
            options, live = _tenant_policy(NS)
        assert lo.call_args_list[0][0] == (NS, "jobs,pods")
        assert options["immutable_live"] == {"Job/init-db": {"gate_container": None, "affinity": None, "avoid": False}}
        assert "mirror_live" not in options and live == {}

    def test_immutable_lookup_failure_leaves_key_out(self, on):
        """讀不到＝未知：不帶 immutable_live，post-renderer 退回「不動非 hook Job／Pod」。"""
        options, _ = _tenant_policy(NS)
        assert "immutable_live" not in options

    def test_immutable_lookup_skipped_when_policy_off(self, monkeypatch):
        with patch.object(helm_module, "_live_objects") as lo:
            assert _tenant_policy(NS) == ({}, {})
        lo.assert_not_called()

    def test_mirror_with_unreadable_cluster_skips_policy(self, on):
        with patch.object(helm_module, "_live_objects", return_value=None):
            assert _tenant_policy(NS, mirror_live=True) == ({}, {})


_RELEASE = {
    "name": "r", "namespace": NS,
    "info": {"status": "deployed", "revision": 2, "last_deployed": ""},
    "chart": {"metadata": {"name": "x", "appVersion": "1"}},
    "manifest": "kind: ConfigMap\nmetadata:\n  name: c\n",
}


def _renderer_payload(mock_run):
    cmd = [c[0][0] for c in mock_run.call_args_list if c[0][0][0] != "kubectl"][-1]
    idx = [i for i, a in enumerate(cmd) if a == "--post-renderer-args"]
    return json.loads(cmd[idx[1] + 1]) if len(idx) > 1 else None


class TestHelmHandsPolicyToRenderer:
    @patch("subprocess.run")
    def test_install_passes_tenant_options(self, mock_run, on):
        mock_run.return_value = Mock(returncode=0, stdout=json.dumps(_RELEASE), stderr="")
        HelmService().install(namespace=NS, name="r", chart="x")
        payload = _renderer_payload(mock_run)
        assert payload["tenant"]["avoid_nodes"] == [AVOID]
        assert payload["tenant"]["egress_gate"]["on_timeout"] == "proceed"

    @patch("subprocess.run")
    def test_upgrade_adopts_by_default(self, mock_run, on):
        mock_run.return_value = Mock(returncode=0, stdout=json.dumps(_RELEASE), stderr="")
        HelmService().upgrade(namespace=NS, name="r", chart="x", version="1.0.0")
        assert "mirror_live" not in _renderer_payload(mock_run)["tenant"]

    @patch("subprocess.run")
    def test_upgrade_mirror_flag_reaches_renderer(self, mock_run, on):
        mock_run.return_value = Mock(returncode=0, stdout=json.dumps(_RELEASE), stderr="")
        live_items = [{"kind": "StatefulSet", "metadata": {"name": "s"}, "spec": {"template": {"spec": {"containers": []}}}}]
        with patch.object(helm_module, "_live_objects", return_value=live_items):
            HelmService().upgrade(namespace=NS, name="r", chart="x", version="1.0.0", tenant_policy_mirror=True)
        payload = _renderer_payload(mock_run)
        assert payload["tenant"]["mirror_live"] is True
        assert payload["tenant_live"] == {"StatefulSet/s": {"gate": False, "avoid": False}}

    @patch("subprocess.run")
    def test_switches_off_leave_argv_identical(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout=json.dumps(_RELEASE), stderr="")
        HelmService().upgrade(namespace=NS, name="r", chart="x", version="1.0.0")
        cmd = [c[0][0] for c in mock_run.call_args_list][-1]
        assert cmd.count("--post-renderer-args") == 1
