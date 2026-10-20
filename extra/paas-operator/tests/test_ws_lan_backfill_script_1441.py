"""#1441：scripts/ws_lan_backfill.py 的預覽／套用邏輯（kubectl 全部 mock）。"""
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ws_lan_backfill",
    Path(__file__).resolve().parents[1] / "scripts" / "ws_lan_backfill.py",
)
bf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bf)
tb = bf.tb


def _proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _ns(name, labels=None, annotations=None):
    meta = {"name": name, "labels": {"paas-managed": "true", "woow.tw/workspace-id": "7",
                                     "paas.woowtech.io/env": "prod", **(labels or {})}}
    if annotations:
        meta["annotations"] = annotations
    return {"metadata": meta}


def _opts(**kw):
    base = dict(only=None, rollback=False, psa_exempt="paas-ws-ab6564da",
                psa_version="v1.34")
    base.update(kw)
    return SimpleNamespace(**base)


class FakeKubectl:
    def __init__(self, live_np=None, warnings=""):
        self.calls = []
        self.live_np = live_np
        self.warnings = warnings

    def __call__(self, ctx, args, stdin=None, check=True):
        self.calls.append((args, stdin))
        if args[:3] == ["-n", args[1], "get"]:
            # 只有名稱相符才回 live（paas-baseline-egress-web 不存在時要回 NotFound）
            if self.live_np is None or self.live_np["metadata"]["name"] != args[4]:
                return _proc(1, err='Error from server (NotFound): networkpolicies "x" not found')
            return _proc(0, out=json.dumps(self.live_np))
        if "--dry-run=server" in args:
            return _proc(0, err=self.warnings)
        return _proc(0)


@pytest.fixture
def fake(monkeypatch):
    def make(**kw):
        f = FakeKubectl(**kw)
        monkeypatch.setattr(bf, "kubectl", f)
        return f
    return make


def test_plan_new_ns_creates_np_and_labels(fake):
    f = fake()
    item = bf.plan_ns("ctx", _ns("paas-ws-aaa"), _opts())
    assert item["egress"] == "create"
    assert "pod-security.kubernetes.io/enforce: ∅ → baseline" in item["psa"]["changes"]
    assert not item["psa"]["exempt"]
    # 預覽只會用 server-side dry-run，不會有真正的寫入
    writes = [a for a, _ in f.calls if a[0] in ("apply", "label", "annotate", "delete")]
    assert writes and all("--dry-run=server" in a for a in writes)


def test_plan_existing_identical_np_unchanged(fake):
    live = tb.build_baseline_egress_netpol("paas-ws-aaa")
    fake(live_np=live)
    labels = tb.build_pod_security_labels(version="v1.34")
    item = bf.plan_ns("ctx", _ns("paas-ws-aaa", labels), _opts())
    assert item["egress"] == "unchanged"
    assert item["psa"]["changes"] == []


def test_plan_drifted_np_is_update(fake):
    live = tb.build_baseline_egress_netpol("paas-ws-aaa")
    live["spec"]["egress"] = [{}]
    fake(live_np=live)
    assert bf.plan_ns("ctx", _ns("paas-ws-aaa"), _opts())["egress"] == "update"


def test_plan_collects_psa_warnings(fake):
    fake(warnings='Warning: existing pods in namespace "x" violate ...\n')
    item = bf.plan_ns("ctx", _ns("paas-ws-aaa"), _opts())
    assert any("violate" in w for w in item["warnings"])


def test_plan_exempt_ns_only_removes_and_annotates(fake):
    fake()
    ns = _ns("paas-ws-ab6564da", {"pod-security.kubernetes.io/enforce": "baseline"})
    item = bf.plan_ns("ctx", ns, _opts())
    assert item["psa"]["exempt"] is True
    assert "pod-security.kubernetes.io/enforce: baseline → ∅" in item["psa"]["changes"]
    assert any("annotation" in c for c in item["psa"]["changes"])


def test_apply_order_psa_then_np(fake):
    f = fake()
    item = bf.plan_ns("ctx", _ns("paas-ws-aaa"), _opts())
    f.calls.clear()
    bf.apply_ns("ctx", item, _opts())
    verbs = [a[0] for a, _ in f.calls]
    assert verbs == ["label", "apply"]
    assert all("--dry-run=server" not in a for a, _ in f.calls)
    assert json.loads(f.calls[1][1]) == tb.build_baseline_egress_netpol("paas-ws-aaa")


def test_rollback_deletes_np_and_removes_labels(fake):
    live = tb.build_baseline_egress_netpol("paas-ws-aaa")
    f = fake(live_np=live)
    labels = tb.build_pod_security_labels(version="v1.34")
    item = bf.plan_ns("ctx", _ns("paas-ws-aaa", labels), _opts(rollback=True))
    assert item["egress"] == "delete"
    f.calls.clear()
    bf.apply_ns("ctx", item, _opts(rollback=True))
    assert f.calls[0][0] == ["-n", "paas-ws-aaa", "delete", "networkpolicy",
                             tb.BASELINE_EGRESS_NP_NAME]
    assert f.calls[1][0][:3] == ["label", "ns", "paas-ws-aaa"]
    assert all(a.endswith("-") for a in f.calls[1][0][3:])


def test_select_skips_agent_ns_and_rejects_non_tenant(monkeypatch):
    items = [_ns("paas-ws-aaa"),
             {"metadata": {"name": "paas-ws-stg-pa-x", "labels": {"paas-managed": "true"}}},
             {"metadata": {"name": "kube-system", "labels": {}}}]
    monkeypatch.setattr(bf, "kubectl",
                        lambda *a, **k: _proc(0, out=json.dumps({"items": items})))
    picked = bf.select_namespaces("ctx", [], True, None)
    assert [n["metadata"]["name"] for n in picked] == ["paas-ws-aaa"]
    with pytest.raises(SystemExit):
        bf.select_namespaces("ctx", ["kube-system"], False, None)


# --- 商用驗收 egress-all-ports-open：腳本與 operator 產生同一份 manifest ------------------


class FakeKubectlByName(FakeKubectl):
    """``get networkpolicy <name>`` 依名稱回不同的 live 物件。"""

    def __init__(self, live=None):
        super().__init__()
        self.live = live or {}

    def __call__(self, ctx, args, stdin=None, check=True):
        if args[:3] == ["-n", args[1], "get"]:
            self.calls.append((args, stdin))
            obj = self.live.get(args[4])
            if obj is None:
                return _proc(1, err='Error from server (NotFound): networkpolicies "x" not found')
            return _proc(0, out=json.dumps(obj))
        return super().__call__(ctx, args, stdin, check)


def _egress_opts(**kw):
    base = dict(only="egress", egress_blocked_ports="", egress_web_profile=False)
    base.update(kw)
    return _opts(**base)


def test_old_opts_without_new_fields_still_work(fake):
    """既有呼叫端（沒有新屬性的 opts）行為不變。"""
    fake()
    item = bf.plan_ns("ctx", _ns("paas-ws-aaa"), _opts(only="egress"))
    assert item["egress"] == "create"
    assert item.get("egress_web") is None


def test_blocked_ports_drift_detected_against_default_np(monkeypatch):
    f = FakeKubectlByName({tb.BASELINE_EGRESS_NP_NAME: tb.build_baseline_egress_netpol("paas-ws-aaa")})
    monkeypatch.setattr(bf, "kubectl", f)
    item = bf.plan_ns("ctx", _ns("paas-ws-aaa"), _egress_opts(egress_blocked_ports="25,22"))
    assert item["egress"] == "update"


def test_blocked_ports_matching_live_is_unchanged(monkeypatch):
    want = tb.build_baseline_egress_netpol("paas-ws-aaa", blocked_ports=[22, 25])
    monkeypatch.setattr(bf, "kubectl", FakeKubectlByName({tb.BASELINE_EGRESS_NP_NAME: want}))
    item = bf.plan_ns("ctx", _ns("paas-ws-aaa"), _egress_opts(egress_blocked_ports="22,25"))
    assert item["egress"] == "unchanged"


def test_web_profile_apply_order_web_then_general(monkeypatch):
    f = FakeKubectlByName({tb.BASELINE_EGRESS_NP_NAME: tb.build_baseline_egress_netpol("paas-ws-aaa")})
    monkeypatch.setattr(bf, "kubectl", f)
    opts = _egress_opts(egress_blocked_ports="25", egress_web_profile=True)
    item = bf.plan_ns("ctx", _ns("paas-ws-aaa"), opts)
    assert item["egress_web"] == "create"
    assert item["egress"] == "update"
    f.calls.clear()
    bf.apply_ns("ctx", item, opts)
    applied = [json.loads(stdin) for a, stdin in f.calls if a[0] == "apply"]
    assert applied == tb.build_baseline_egress_netpols("paas-ws-aaa", blocked_ports=[25],
                                                       web_profile=True)


def test_rollback_also_deletes_web_np(monkeypatch):
    live = {tb.BASELINE_EGRESS_NP_NAME: tb.build_baseline_egress_netpol("paas-ws-aaa"),
            tb.BASELINE_EGRESS_WEB_NP_NAME: tb.build_baseline_egress_web_netpol("paas-ws-aaa")}
    f = FakeKubectlByName(live)
    monkeypatch.setattr(bf, "kubectl", f)
    opts = _egress_opts(rollback=True)
    item = bf.plan_ns("ctx", _ns("paas-ws-aaa"), opts)
    assert item["egress"] == "delete" and item["egress_web"] == "delete"
    f.calls.clear()
    bf.apply_ns("ctx", item, opts)
    deleted = [a[4] for a, _ in f.calls if a[2] == "delete"]
    assert deleted == [tb.BASELINE_EGRESS_NP_NAME, tb.BASELINE_EGRESS_WEB_NP_NAME]


def test_cli_rejects_bad_blocked_ports():
    with pytest.raises(SystemExit):
        bf.main(["--ns", "paas-ws-aaa", "--egress-blocked-ports", "smtp"])
