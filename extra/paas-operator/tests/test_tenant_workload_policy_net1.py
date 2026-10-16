"""AI-FIX-NET-1：租戶 workload 的 egress gate（#1497）與節點迴避（INFRA-XSVC-007）。

純函式（``src/tenant_workload_policy.py``）、post-renderer 子程序（和 helm 實際呼叫方式
相同：stdin 進 YAML、argv 帶 JSON）、``helm.py`` 的接線，以及 gate 腳本本身的邏輯。
"""
import copy
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src import tenant_workload_policy as pol
from src.pvc_keep_post_renderer import apply_tenant_stage

_SCRIPT = Path(__file__).resolve().parent.parent / "src" / "pvc_keep_post_renderer.py"
AVOID = ["ubuntuserver-59mqj"]
GATE_CFG = {"image": "busybox:1.36", "timeout_seconds": 60, "on_timeout": "proceed", "target": ""}


def deployment(name="web", labels=None, spec_extra=None, annotations=None):
    labels = labels or {"app": name}
    pod_spec = {"containers": [{"name": "app", "image": "x:1"}]}
    pod_spec.update(spec_extra or {})
    meta = {"labels": dict(labels)}
    if annotations:
        meta["annotations"] = dict(annotations)
    return {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
        "spec": {"selector": {"matchLabels": labels}, "template": {"metadata": meta, "spec": pod_spec}},
    }


def pod_spec_of(doc):
    return doc["spec"]["template"]["spec"]


def terms_of(spec):
    return spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]


# --------------------------------------------------------------------------- #
# 節點迴避                                                                      #
# --------------------------------------------------------------------------- #

class TestNodeAvoidance:
    def test_adds_required_notin_hostname(self):
        out, _ = pol.apply_tenant_policy([deployment()], {"avoid_nodes": AVOID})
        assert terms_of(pod_spec_of(out[0])) == [{"matchExpressions": [
            {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": AVOID}]}]

    def test_and_into_every_existing_term(self):
        """nodeSelectorTerms 之間是 OR：只加一個新 term 等於放寬，必須加進每一個。"""
        existing = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "disk", "operator": "In", "values": ["ssd"]}]},
            {"matchExpressions": [{"key": "zone", "operator": "In", "values": ["a"]}]},
        ]}}}
        out, _ = pol.apply_tenant_policy([deployment(spec_extra={"affinity": existing})], {"avoid_nodes": AVOID})
        terms = terms_of(pod_spec_of(out[0]))
        assert len(terms) == 2
        for term in terms:
            assert {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": AVOID} in term["matchExpressions"]
        assert terms[0]["matchExpressions"][0]["key"] == "disk"       # 原條件還在
        assert terms[1]["matchExpressions"][0]["key"] == "zone"

    def test_keeps_pod_affinity_and_preferred(self):
        existing = {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": "x"}]},
                    "nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [{"weight": 1, "preference": {}}]}}
        out, _ = pol.apply_tenant_policy([deployment(spec_extra={"affinity": existing})], {"avoid_nodes": AVOID})
        aff = pod_spec_of(out[0])["affinity"]
        assert aff["podAffinity"] == existing["podAffinity"]
        assert aff["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        assert terms_of(pod_spec_of(out[0]))

    def test_merges_into_existing_hostname_notin(self):
        existing = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["other"]}]}]}}}
        out, _ = pol.apply_tenant_policy([deployment(spec_extra={"affinity": existing})], {"avoid_nodes": AVOID})
        values = terms_of(pod_spec_of(out[0]))[0]["matchExpressions"][0]["values"]
        assert sorted(values) == sorted(["other"] + AVOID)

    def test_idempotent(self):
        once, _ = pol.apply_tenant_policy([deployment()], {"avoid_nodes": AVOID})
        twice, log = pol.apply_tenant_policy(once, {"avoid_nodes": AVOID})
        assert twice == once
        assert "node-avoid present" in log[0]

    def test_pinned_pod_is_left_alone(self):
        out, log = pol.apply_tenant_policy([deployment(spec_extra={"nodeName": "ubuntuserver-31zix"})], {"avoid_nodes": AVOID})
        assert "affinity" not in pod_spec_of(out[0])
        assert "skip-pinned" in log[0]

    def test_selector_conflict_is_left_alone(self):
        spec = {"nodeSelector": {"kubernetes.io/hostname": "ubuntuserver-59mqj"}}
        out, log = pol.apply_tenant_policy([deployment(spec_extra=spec)], {"avoid_nodes": AVOID})
        assert "affinity" not in pod_spec_of(out[0])
        assert "skip-selector-conflict" in log[0]

    def test_statefulset_daemonset_cronjob_and_vm(self):
        sts = {"kind": "StatefulSet", "metadata": {"name": "s"}, "spec": {"template": {"spec": {"containers": []}}}}
        ds = {"kind": "DaemonSet", "metadata": {"name": "d"}, "spec": {"template": {"spec": {"containers": []}}}}
        cj = {"kind": "CronJob", "metadata": {"name": "c"},
              "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": []}}}}}}
        vm = {"kind": "VirtualMachine", "metadata": {"name": "v"}, "spec": {"template": {"spec": {"domain": {}}}}}
        out, _ = pol.apply_tenant_policy([sts, ds, cj, vm], {"avoid_nodes": AVOID})
        assert terms_of(out[0]["spec"]["template"]["spec"])
        assert terms_of(out[1]["spec"]["template"]["spec"])
        assert terms_of(out[2]["spec"]["jobTemplate"]["spec"]["template"]["spec"])
        assert terms_of(out[3]["spec"]["template"]["spec"])

    def test_non_workload_docs_untouched(self):
        docs = [{"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"a": "b"}},
                {"kind": "Service", "metadata": {"name": "s"}, "spec": {"ports": []}}]
        out, _ = pol.apply_tenant_policy(docs, {"avoid_nodes": AVOID, "egress_gate": GATE_CFG})
        assert out == docs

    def test_does_not_mutate_input(self):
        docs = [deployment()]
        before = copy.deepcopy(docs)
        pol.apply_tenant_policy(docs, {"avoid_nodes": AVOID, "egress_gate": GATE_CFG})
        assert docs == before

    def test_no_options_is_a_noop(self):
        docs = [deployment()]
        assert pol.apply_tenant_policy(docs, None) == (docs, [])
        assert pol.apply_tenant_policy(docs, {}) == (docs, [])
        assert pol.apply_tenant_policy(docs, {"avoid_nodes": [], "egress_gate": None}) == (docs, [])


# --------------------------------------------------------------------------- #
# egress gate                                                                  #
# --------------------------------------------------------------------------- #

class TestEgressGate:
    def test_gate_is_first_init_container(self):
        other = {"name": "wait-data", "image": "busybox:1.36"}
        out, _ = pol.apply_tenant_policy([deployment(spec_extra={"initContainers": [other]})], {"egress_gate": GATE_CFG})
        names = [c["name"] for c in pod_spec_of(out[0])["initContainers"]]
        assert names == [pol.GATE_CONTAINER_NAME, "wait-data"]

    def test_gate_passes_restricted_pod_security_and_pins_resources(self):
        g = pol.build_egress_gate_container()
        sc = g["securityContext"]
        assert sc["runAsNonRoot"] is True and sc["runAsUser"] == 65534
        assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"] == {"drop": ["ALL"]}
        assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
        for side in ("requests", "limits"):
            assert {"cpu", "memory", "ephemeral-storage"} <= set(g["resources"][side])

    def test_idempotent(self):
        once, _ = pol.apply_tenant_policy([deployment()], {"egress_gate": GATE_CFG})
        twice, log = pol.apply_tenant_policy(once, {"egress_gate": GATE_CFG})
        assert twice == once
        assert len(pod_spec_of(twice[0])["initContainers"]) == 1
        assert "egress-gate present" in log[0]

    def test_host_network_pod_skipped(self):
        out, log = pol.apply_tenant_policy([deployment(spec_extra={"hostNetwork": True})], {"egress_gate": GATE_CFG})
        assert "initContainers" not in pod_spec_of(out[0])
        assert "skip-host-network" in log[0]

    def test_skip_annotation(self):
        doc = deployment(annotations={pol.GATE_SKIP_ANNOTATION: "skip"})
        out, log = pol.apply_tenant_policy([doc], {"egress_gate": GATE_CFG})
        assert "initContainers" not in pod_spec_of(out[0])
        assert "annotation" in log[0]

    def test_vm_never_gets_a_gate(self):
        vm = {"kind": "VirtualMachine", "metadata": {"name": "v"}, "spec": {"template": {"spec": {"domain": {}}}}}
        out, _ = pol.apply_tenant_policy([vm], {"egress_gate": GATE_CFG})
        assert "initContainers" not in out[0]["spec"]["template"]["spec"]

    def test_target_and_timeout_are_forwarded(self):
        cfg = dict(GATE_CFG, target="10.43.0.1:443", timeout_seconds=30, on_timeout="fail", image="reg/x:1")
        out, _ = pol.apply_tenant_policy([deployment()], {"egress_gate": cfg})
        gate = pod_spec_of(out[0])["initContainers"][0]
        env = {e["name"]: e["value"] for e in gate["env"]}
        assert env["GATE_TARGET"] == "10.43.0.1:443" and env["GATE_TIMEOUT"] == "30" and env["GATE_ON_TIMEOUT"] == "fail"
        assert gate["image"] == "reg/x:1"

    def test_bad_on_timeout_falls_back_to_proceed(self):
        g = pol.build_egress_gate_container(on_timeout="explode")
        assert {e["name"]: e["value"] for e in g["env"]}["GATE_ON_TIMEOUT"] == "proceed"


class TestChartNetworkPolicyThatAllowsTheProbe:
    """chart 自帶的 egress 規則若本來就放行探測目標，gate 永遠等不到 → 不插。"""

    @staticmethod
    def np(egress, selector=None, types=("Egress",)):
        return {"kind": "NetworkPolicy", "metadata": {"name": "n"},
                "spec": {"podSelector": selector if selector is not None else {}, "policyTypes": list(types), "egress": egress}}

    def inject(self, np_doc, labels=None):
        out, log = pol.apply_tenant_policy([deployment(labels=labels or {"app": "web"}), np_doc], {"egress_gate": GATE_CFG})
        return ("initContainers" in pod_spec_of(out[0])), log

    def test_open_ipblock_allows_target(self):
        injected, log = self.inject(self.np([{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}]))
        assert injected is False and "allows the probe target" in log[0]

    def test_ipblock_with_rfc1918_except_does_not(self):
        injected, _ = self.inject(self.np([{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8"]}}]}]))
        assert injected is True

    def test_rule_without_to_allows_everything(self):
        injected, _ = self.inject(self.np([{}]))
        assert injected is False

    def test_port_restricted_rule_that_excludes_443_does_not_allow(self):
        injected, _ = self.inject(self.np([{"to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}], "ports": [{"protocol": "TCP", "port": 5432}]}]))
        assert injected is True

    def test_pod_selector_peers_are_not_ip_grants(self):
        injected, _ = self.inject(self.np([{"to": [{"podSelector": {}}]}]))
        assert injected is True

    def test_selector_mismatch_ignored(self):
        injected, _ = self.inject(self.np([{}], selector={"matchLabels": {"app": "other"}}))
        assert injected is True

    def test_match_expressions_selector(self):
        sel = {"matchExpressions": [{"key": "app", "operator": "In", "values": ["web"]}]}
        injected, _ = self.inject(self.np([{}], selector=sel))
        assert injected is False

    def test_api_only_allow_switches_probe_to_the_node_kubelet(self):
        """open-design console 這種 chart 明確放行 10.43.0.1/32：改探測 status.hostIP:10250，仍要 gate。"""
        np_doc = self.np([{"to": [{"ipBlock": {"cidr": "10.43.0.1/32"}}], "ports": [{"protocol": "TCP", "port": 443}]}])
        out, log = pol.apply_tenant_policy([deployment(labels={"app": "web"}), np_doc], {"egress_gate": GATE_CFG})
        gate = pod_spec_of(out[0])["initContainers"][0]
        names = [e["name"] for e in gate["env"]]
        assert names.index("GATE_HOST_IP") < names.index("GATE_TARGET")        # $(VAR) 只能引用前面的 env
        env = {e["name"]: e for e in gate["env"]}
        assert env["GATE_HOST_IP"]["valueFrom"]["fieldRef"]["fieldPath"] == "status.hostIP"
        assert env["GATE_TARGET"]["value"] == "$(GATE_HOST_IP):10250"
        assert "probing node kubelet" in log[0]

    def test_api_and_lan_both_allowed_means_skip(self):
        np_doc = self.np([{"to": [{"ipBlock": {"cidr": "10.43.0.1/32"}}]}, {"to": [{"ipBlock": {"cidr": "192.168.0.0/16"}}]}])
        injected, log = self.inject(np_doc)
        assert injected is False and "allows the probe target" in log[0]

    def test_explicit_target_never_falls_back(self):
        np_doc = self.np([{"to": [{"ipBlock": {"cidr": "10.43.0.1/32"}}]}])
        cfg = dict(GATE_CFG, target="10.43.0.1:443")
        out, log = pol.apply_tenant_policy([deployment(labels={"app": "web"}), np_doc], {"egress_gate": cfg})
        assert "initContainers" not in pod_spec_of(out[0]) and "allows the probe target" in log[0]

    def test_ingress_only_policy_ignored(self):
        np_doc = {"kind": "NetworkPolicy", "metadata": {"name": "n"},
                  "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{}]}}
        injected, _ = self.inject(np_doc)
        assert injected is True


# --------------------------------------------------------------------------- #
# Job／Pod 不可變                                                                #
# --------------------------------------------------------------------------- #

class TestImmutableTemplates:
    @staticmethod
    def job(hook=False):
        meta = {"name": "j"}
        if hook:
            meta["annotations"] = {"helm.sh/hook": "post-install"}
        return {"kind": "Job", "metadata": meta, "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}}}

    def test_plain_job_is_never_touched(self):
        """非 hook 的 Job 改 template 會讓 helm upgrade 整個 immutable 失敗。"""
        out, log = pol.apply_tenant_policy([self.job()], {"avoid_nodes": AVOID, "egress_gate": GATE_CFG})
        assert out[0] == self.job()
        assert "immutable" in log[0]

    def test_hook_job_is_patched(self):
        out, _ = pol.apply_tenant_policy([self.job(hook=True)], {"avoid_nodes": AVOID, "egress_gate": GATE_CFG})
        spec = out[0]["spec"]["template"]["spec"]
        assert spec["initContainers"][0]["name"] == pol.GATE_CONTAINER_NAME
        assert terms_of(spec)

    def test_cronjob_is_patched(self):
        cj = {"kind": "CronJob", "metadata": {"name": "c"},
              "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": []}}}}}}
        out, _ = pol.apply_tenant_policy([cj], {"egress_gate": GATE_CFG})
        assert out[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"]


# --------------------------------------------------------------------------- #
# mirror-live（reset-config：不得改 pod template）                                 #
# --------------------------------------------------------------------------- #

class TestMirrorLive:
    OPTS = {"avoid_nodes": AVOID, "egress_gate": GATE_CFG, "mirror_live": True}

    def test_existing_workload_without_policy_stays_as_it_is(self):
        live = {"Deployment/web": {"gate": False, "avoid": False}}
        out, log = pol.apply_tenant_policy([deployment()], self.OPTS, live)
        assert out[0] == deployment()
        assert any("kept as live" in line for line in log)

    def test_existing_workload_with_policy_keeps_it(self):
        live = {"Deployment/web": {"gate": True, "avoid": True}}
        out, _ = pol.apply_tenant_policy([deployment()], self.OPTS, live)
        assert pod_spec_of(out[0])["initContainers"][0]["name"] == pol.GATE_CONTAINER_NAME
        assert terms_of(pod_spec_of(out[0]))

    def test_partial_live_state_is_mirrored_per_item(self):
        live = {"Deployment/web": {"gate": True, "avoid": False}}
        out, _ = pol.apply_tenant_policy([deployment()], self.OPTS, live)
        assert "initContainers" in pod_spec_of(out[0])
        assert "affinity" not in pod_spec_of(out[0])

    def test_new_workload_gets_the_policy(self):
        out, _ = pol.apply_tenant_policy([deployment("brand-new")], self.OPTS, {"Deployment/web": {"gate": False, "avoid": False}})
        assert "initContainers" in pod_spec_of(out[0]) and terms_of(pod_spec_of(out[0]))

    def test_live_facts_reads_real_workloads(self):
        gated, _ = pol.apply_tenant_policy([deployment()], {"avoid_nodes": AVOID, "egress_gate": GATE_CFG})
        item = dict(gated[0])
        plain = deployment("plain")
        facts = pol.live_facts([item, plain, {"kind": "Service", "metadata": {"name": "s"}}], AVOID)
        assert facts == {"Deployment/web": {"gate": True, "avoid": True},
                         "Deployment/plain": {"gate": False, "avoid": False}}


# --------------------------------------------------------------------------- #
# post-renderer 子程序（helm 實際呼叫方式）                                          #
# --------------------------------------------------------------------------- #

def run_renderer(docs, arg=None):
    argv = [sys.executable, str(_SCRIPT)]
    if arg is not None:
        argv.append(arg if isinstance(arg, str) else json.dumps(arg))
    proc = subprocess.run(argv, input=yaml.safe_dump_all(docs), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return list(yaml.safe_load_all(proc.stdout)), proc.stderr


class TestPostRendererProcess:
    def test_without_tenant_key_output_is_unchanged(self):
        docs = [deployment(), {"kind": "PersistentVolumeClaim", "metadata": {"name": "d"}}]
        base, _ = run_renderer(docs)
        with_empty, _ = run_renderer(docs, {"pvc": {}, "sts": {}})
        assert base == with_empty
        assert "initContainers" not in pod_spec_of(base[0])

    def test_tenant_key_applies_policy_in_script_mode(self):
        out, err = run_renderer([deployment()], {"tenant": {"avoid_nodes": AVOID, "egress_gate": GATE_CFG}})
        spec = pod_spec_of(out[0])
        assert spec["initContainers"][0]["name"] == pol.GATE_CONTAINER_NAME
        assert terms_of(spec)
        assert "tenant-policy: Deployment/web: egress-gate added" in err

    def test_pvc_stage_still_runs_alongside(self):
        out, _ = run_renderer([{"kind": "PersistentVolumeClaim", "metadata": {"name": "d"}}, deployment()],
                              {"tenant": {"avoid_nodes": AVOID}})
        assert out[0]["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"

    def test_policy_failure_fails_open(self):
        """政策階段壞掉（例如 options 型別不對）不能讓 helm render 失敗，輸出原樣。"""
        docs = [deployment()]
        base, _ = run_renderer(docs)
        out, err = run_renderer(docs, {"tenant": {"avoid_nodes": 5}})
        assert out == base
        assert "tenant-policy: skipped" in err

    def test_apply_tenant_stage_returns_input_on_error(self):
        docs = [deployment()]
        assert apply_tenant_stage(docs, {"tenant": {"avoid_nodes": 5}}) is docs
        assert apply_tenant_stage(docs, {}) is docs


# --------------------------------------------------------------------------- #
# gate 腳本本身（用假的 nc 驗邏輯）                                                  #
# --------------------------------------------------------------------------- #

def run_gate(tmp_path, nc_exit_codes, env_extra=None, timeout=20):
    """跑 GATE_SCRIPT；假 nc 依序回傳 ``nc_exit_codes``（用完後一直回最後一個）。"""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    counter = tmp_path / "count"
    counter.write_text("0")
    codes = " ".join(str(c) for c in nc_exit_codes)
    nc = bindir / "nc"
    nc.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        n=$(cat {counter}); n=$((n+1)); echo $n > {counter}
        set -- {codes}
        idx=$n; [ "$idx" -gt "$#" ] && idx=$#
        eval "exit \\${{$idx}}"
        """))
    nc.chmod(nc.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": "%s:%s" % (bindir, os.environ["PATH"]), "KUBERNETES_SERVICE_HOST": "10.43.0.1",
           "KUBERNETES_SERVICE_PORT": "443"}
    # 與 pod 內相同：容器 env 由 build_egress_gate_container 決定（含 GATE_CONSECUTIVE）。
    env.update({e["name"]: e["value"] for e in pol.build_egress_gate_container()["env"]})
    env.update(env_extra or {})
    proc = subprocess.run(["sh", "-c", pol.GATE_SCRIPT], env=env, capture_output=True, text=True, timeout=timeout)
    return proc, int(counter.read_text())


class TestGateScript:
    def test_waits_while_reachable_then_releases_after_two_blocked(self, tmp_path):
        proc, calls = run_gate(tmp_path, [0, 0, 0, 1, 1])
        assert proc.returncode == 0
        assert "policy active" in proc.stdout
        assert calls == 5

    def test_container_requires_two_consecutive_blocked_probes(self):
        env = {e["name"]: e["value"] for e in pol.build_egress_gate_container()["env"]}
        assert env["GATE_CONSECUTIVE"] == "2"

    def test_a_single_blip_does_not_release(self, tmp_path):
        """1,0,1,1：中間又連得上，計數歸零；要連續兩次連不到才放行。"""
        proc, calls = run_gate(tmp_path, [1, 0, 1, 1])
        assert proc.returncode == 0 and calls == 4

    def test_timeout_proceeds_by_default(self, tmp_path):
        proc, _ = run_gate(tmp_path, [0], {"GATE_TIMEOUT": "1"})
        assert proc.returncode == 0
        assert "NOT observed" in proc.stderr and "proceeding" in proc.stderr

    def test_timeout_fails_when_configured(self, tmp_path):
        proc, _ = run_gate(tmp_path, [0], {"GATE_TIMEOUT": "1", "GATE_ON_TIMEOUT": "fail"})
        assert proc.returncode == 1

    def test_probes_the_cluster_api_by_default_and_honours_override(self, tmp_path):
        bindir = tmp_path / "b2"
        bindir.mkdir()
        log = tmp_path / "args"
        nc = bindir / "nc"
        nc.write_text("#!/bin/sh\necho \"$@\" >> %s\nexit 1\n" % log)
        nc.chmod(nc.stat().st_mode | stat.S_IEXEC)
        base = {"PATH": "%s:%s" % (bindir, os.environ["PATH"])}
        subprocess.run(["sh", "-c", pol.GATE_SCRIPT], env=dict(base, KUBERNETES_SERVICE_HOST="10.43.0.1", KUBERNETES_SERVICE_PORT="443"),
                       capture_output=True, text=True, timeout=20)
        assert "-z -w 1 10.43.0.1 443" in log.read_text()
        log.write_text("")
        subprocess.run(["sh", "-c", pol.GATE_SCRIPT], env=dict(base, GATE_TARGET="192.168.10.21:10250"),
                       capture_output=True, text=True, timeout=20)
        assert "-z -w 1 192.168.10.21 10250" in log.read_text()
