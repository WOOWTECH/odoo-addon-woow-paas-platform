#!/usr/bin/env python3
"""Render (never apply) the owned gate sidecar and native authority stack.

Live rendering is deliberately fail-fast: every enrollment, identity, image,
and storage input must be supplied by the controlled handoff.  Secret values
are neither accepted nor emitted.
"""
import argparse
import json
import re
import sys


DNS = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
IMAGE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

REQUIRED = {
    "mode", "namespace", "owner_uid", "ha_deployment", "ha_container",
    "gate_image", "authority_image", "source_pvc", "state_pvc",
    "identity_secret", "odoo_config_secret", "database", "actor_uid",
    "canonical_name", "resource_identity", "installation_id", "issuer",
    "audience", "authority_server_sha256", "gate_client_sha256",
    "gate_source_sha256", "gate_requirements_sha256",
}


def _dns(value, key):
    if not isinstance(value, str) or not DNS.fullmatch(value):
        raise ValueError("invalid " + key)


def exact(value):
    if not isinstance(value, dict) or set(value) != REQUIRED:
        missing = sorted(REQUIRED - set(value)) if isinstance(value, dict) else sorted(REQUIRED)
        extra = sorted(set(value) - REQUIRED) if isinstance(value, dict) else []
        raise ValueError("exact candidate config required; missing=%s extra=%s" % (missing, extra))
    if value["mode"] not in ("live", "test-only"):
        raise ValueError("mode must be live or test-only")
    for key in ("namespace", "ha_deployment", "ha_container", "source_pvc",
                "state_pvc", "identity_secret", "odoo_config_secret"):
        _dns(value[key], key)
    for key in ("gate_image", "authority_image"):
        if not isinstance(value[key], str) or not IMAGE.fullmatch(value[key]):
            raise ValueError("digest-pinned " + key + " required")
    for key in ("authority_server_sha256", "gate_client_sha256",
                "gate_source_sha256", "gate_requirements_sha256"):
        if not isinstance(value[key], str) or not SHA256.fullmatch(value[key]):
            raise ValueError("invalid " + key)
    for key in ("owner_uid", "database", "canonical_name", "resource_identity",
                "installation_id", "issuer", "audience"):
        item = value[key]
        if not isinstance(item, str) or not item or len(item) > 512:
            raise ValueError("invalid " + key)
    if type(value["actor_uid"]) is not int or value["actor_uid"] <= 1:
        raise ValueError("normal non-superuser actor_uid required")
    if value["mode"] == "live":
        forbidden = ("example", "placeholder", "test-only", "invalid")
        flattened = " ".join(str(value[key]).lower() for key in REQUIRED)
        if any(word in flattened for word in forbidden):
            raise ValueError("test or placeholder value forbidden in live mode")
    return value


def render(c):
    c = exact(c)
    labels = {"app.kubernetes.io/part-of": "b38-fault02",
              "woow.io/owner-uid": c["owner_uid"]}
    gate_annotations = {
        "woow.io/gate-source-sha256": c["gate_source_sha256"],
        "woow.io/gate-requirements-sha256": c["gate_requirements_sha256"],
        "woow.io/gate-image-contract": "publication_gate-and-locked-dependencies-in-image",
    }
    gate_name = c["ha_deployment"] + "-publication-gate"
    authority_name = c["ha_deployment"] + "-publication-authority"
    gate_binding = {
        "resource_identity": c["resource_identity"],
        "installation_id": c["installation_id"],
        "state": "/var/lib/woow-publication/latch.sqlite3",
        "issuer": c["issuer"], "audience": c["audience"],
        "jwt_public_key": "/etc/woow-publication/identity/access-jwt.pem",
        "client_sha256": c["gate_client_sha256"],
        "server_cert": "/etc/woow-publication/identity/gate.crt",
        "server_key": "/etc/woow-publication/identity/gate.key",
        "ca": "/etc/woow-publication/identity/ca.crt",
        "control_bind": "0.0.0.0", "control_port": 18443,
        "data_bind": "0.0.0.0", "data_port": 18303,
        "upstream": "http://127.0.0.1:8123/manifest.json",
    }
    client_binding = {
        "host": authority_name + "." + c["namespace"] + ".svc.cluster.local",
        "port": 18444, "server_sha256": c["authority_server_sha256"],
        "ca": "/etc/woow-publication/identity/ca.crt",
        "client_cert": "/etc/woow-publication/identity/gate-client.crt",
        "client_key": "/etc/woow-publication/identity/gate-client.key",
    }
    server_binding = {
        "db": c["database"], "odoo_config": "/identity/odoo.conf",
        "addons_path": "/candidate/src", "bind": "0.0.0.0", "port": 18444,
        "canonical_name": c["canonical_name"], "actor_uid": c["actor_uid"],
        "server_cert": "/identity/authority.crt",
        "server_key": "/identity/authority.key", "ca": "/identity/ca.crt",
        "gate_client_cert": "/identity/gate-client.crt",
    }
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": gate_name, "namespace": c["namespace"], "labels": labels},
          "data": {"gate.json": json.dumps(gate_binding, separators=(",", ":")),
                   "authority-client.json": json.dumps(client_binding, separators=(",", ":")),
                   "authority-server.json": json.dumps(server_binding, separators=(",", ":"))}}
    sidecar = {
        "name": "publication-gate", "image": c["gate_image"],
        "command": ["python", "-m", "publication_gate.main"],
        "env": [
            {"name": "PUBLICATION_GATE_BINDING", "value": "/etc/woow-publication/config/gate.json"},
            {"name": "PUBLICATION_AUTHORITY_BINDING", "value": "/etc/woow-publication/config/authority-client.json"}],
        "ports": [{"name": "manifest-gate", "containerPort": 18303},
                  {"name": "gate-control", "containerPort": 18443}],
        "readinessProbe": {"tcpSocket": {"port": "manifest-gate"}, "periodSeconds": 3},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532,
                            "runAsGroup": 65532,
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                      "limits": {"cpu": "250m", "memory": "128Mi"}},
        "volumeMounts": [
            {"name": "publication-gate-config", "mountPath": "/etc/woow-publication/config", "readOnly": True},
            {"name": "publication-identity", "mountPath": "/etc/woow-publication/identity", "readOnly": True},
            {"name": "publication-state", "mountPath": "/var/lib/woow-publication"}],
    }
    patch = {"apiVersion": "apps/v1", "kind": "Deployment",
             "metadata": {"name": c["ha_deployment"], "namespace": c["namespace"]},
             "spec": {"template": {"metadata": {"labels": labels,
                                                    "annotations": gate_annotations}, "spec": {
                 "securityContext": {"fsGroup": 65532,
                                     "fsGroupChangePolicy": "OnRootMismatch"},
                 "containers": [{"name": c["ha_container"]}, sidecar],
                 "volumes": [
                     {"name": "publication-gate-config", "configMap": {"name": gate_name}},
                     {"name": "publication-identity", "secret": {"secretName": c["identity_secret"], "defaultMode": 288}},
                     {"name": "publication-state", "persistentVolumeClaim": {"claimName": c["state_pvc"]}}]}}}}
    authority_container = {
        "name": "authority", "image": c["authority_image"],
        "command": ["python", "/candidate/scripts/rev01_authority_server.py"],
        "env": [{"name": "REV01_AUTHORITY_BINDING", "value": "/config/authority-server.json"}],
        "ports": [{"name": "authority", "containerPort": 18444}],
        "readinessProbe": {"tcpSocket": {"port": "authority"}, "periodSeconds": 3},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 100,
                            "runAsGroup": 100, "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "25m", "memory": "128Mi"},
                      "limits": {"cpu": "500m", "memory": "512Mi"}},
        "volumeMounts": [
            {"name": "source", "mountPath": "/candidate", "readOnly": True},
            {"name": "config", "mountPath": "/config", "readOnly": True},
            {"name": "identity", "mountPath": "/identity", "readOnly": True}],
    }
    authority_pod = {
        "automountServiceAccountToken": False,
        "securityContext": {"fsGroup": 100, "fsGroupChangePolicy": "OnRootMismatch"},
        "containers": [authority_container],
        "volumes": [
            {"name": "source", "persistentVolumeClaim": {"claimName": c["source_pvc"]}},
            {"name": "config", "configMap": {"name": gate_name}},
            {"name": "identity", "projected": {"defaultMode": 288, "sources": [
                {"secret": {"name": c["identity_secret"]}},
                {"secret": {"name": c["odoo_config_secret"]}}]}}],
    }
    authority = {"apiVersion": "apps/v1", "kind": "Deployment",
                 "metadata": {"name": authority_name, "namespace": c["namespace"], "labels": labels},
                 "spec": {"replicas": 1, "selector": {"matchLabels": {"app": authority_name}},
                          "template": {"metadata": {"labels": dict(labels, app=authority_name)},
                                       "spec": authority_pod}}}
    service = {"apiVersion": "v1", "kind": "Service",
               "metadata": {"name": authority_name, "namespace": c["namespace"], "labels": labels},
               "spec": {"selector": {"app": authority_name},
                        "ports": [{"name": "authority", "port": 18444, "targetPort": "authority"}]}}
    network = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
               "metadata": {"name": authority_name, "namespace": c["namespace"], "labels": labels},
               "spec": {"podSelector": {"matchLabels": {"app": authority_name}},
                        "policyTypes": ["Ingress"], "ingress": [{"from": [{"podSelector": {
                            "matchLabels": {"woow.io/owner-uid": c["owner_uid"]}}}],
                            "ports": [{"protocol": "TCP", "port": 18444}]}]}}
    return {"apiVersion": "v1", "kind": "List", "items": [cm, patch, authority, service, network],
            "x-b38": {"mode": c["mode"], "apply": False,
                       "gate_image_contract": "image contains publication_gate and locked dependencies",
                       "state_volume_contract": "kubelet fsGroup 65532 makes the owned PVC group-writable",
                       "requires_existing": [c["source_pvc"], c["state_pvc"],
                                             c["identity_secret"], c["odoo_config_secret"]],
                       "ha_rollout_required": True}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    args = parser.parse_args()
    try:
        with open(args.config, encoding="utf-8") as stream:
            result = render(json.load(stream))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
