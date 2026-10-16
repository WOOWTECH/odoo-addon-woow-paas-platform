#!/usr/bin/env python3
"""Helm post-renderer (#317): stamp `helm.sh/resource-policy: keep` on every PVC.

Helm decides whether to delete a resource on `helm uninstall` from the annotation
stored in the RELEASE MANIFEST at install/upgrade time — NOT from the live cluster
object (verified against helm 3.13.3, the operator image version). So a chart-owned
PVC (a plain PVC template, e.g. the 8gears n8n data PVC) that ships without
`helm.sh/resource-policy: keep` is deleted outright on uninstall, and no amount of
post-teardown labeling can save data that is already gone.

Run at install/upgrade, this adds the keep annotation to every PVC in the rendered
manifest, so the STORED manifest carries it and helm SKIPS those PVCs on uninstall.
They survive as orphaned PVCs for the deferred pvc-cleanup cron (90-day grace).

SCOPE (hard rule): touches ONLY `kind: PersistentVolumeClaim` objects, and ONLY
their annotations. Every other resource passes through unchanged. Never clobbers an
explicit resource-policy the chart author already set (setdefault).

Second job (#1407): reuse the LIVE values of fields Kubernetes will not let an
upgrade change. A bound PVC's ``storageClassName``/``accessModes``/``volumeMode`` are
immutable and its size can only grow; a StatefulSet's ``volumeClaimTemplates`` are
immutable as a whole. helm's three-way merge "corrects" live drift, so whenever the
render differs from the live object (a volume handed off from another cluster with
``longhorn``, a platform default that changed since install, a chart that changed
its default size, …) the whole ``helm upgrade`` is rejected — after helm may already
have applied other resources. Rendering the live value for an object that already
exists cannot change anything the API server would have accepted, so it only removes
a guaranteed failure. Objects that do not exist yet render untouched. What cannot be
reused this way (a claim template added/removed, a selector change) is left alone
for the operator's upgrade preflight to refuse with a readable message.

Third job (AI-FIX-NET-1, ``tenant_workload_policy``): when the JSON carries a ``tenant``
object, add the egress-gate init container (#1497) and the node-avoidance nodeAffinity
(INFRA-XSVC-007) to the rendered pod templates. Strictly opt-in and fail-open: any error
in that stage leaves the manifest exactly as the earlier stages produced it.

HelmService invokes it as:
    helm install/upgrade ... --post-renderer <python> --post-renderer-args <this>
        [--post-renderer-args <JSON live snapshot, see ``live_snapshot``>]
so it always runs under the operator's own interpreter (PyYAML guaranteed).
"""
import json
import sys

import yaml


def stamp_keep_on_pvcs(docs):
    """Return docs with helm.sh/resource-policy: keep added to every PVC.

    PVC docs are mutated in place (annotation added); None entries (blank YAML
    docs) are dropped; all other docs are returned untouched.
    """
    out = []
    for doc in docs:
        if doc is None:
            continue
        if isinstance(doc, dict) and doc.get("kind") == "PersistentVolumeClaim":
            metadata = doc.setdefault("metadata", {})
            annotations = metadata.get("annotations")
            if annotations is None:
                annotations = {}
                metadata["annotations"] = annotations
            annotations.setdefault("helm.sh/resource-policy", "keep")
        out.append(doc)
    return out


# --------------------------------------------------------------------------- #
# #1407 live-value reuse                                                      #
# --------------------------------------------------------------------------- #

_UNITS = {
    "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50,
    "k": 10 ** 3, "K": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15,
}


def parse_quantity(value):
    """Kubernetes storage quantity → bytes (int); None when absent/unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    for suffix in sorted(_UNITS, key=len, reverse=True):
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * _UNITS[suffix])
            except ValueError:
                return None
    try:
        return int(float(text))
    except ValueError:
        return None


def claim_facts(spec):
    """The immutable-ish facts of a PVC spec / claim template spec, as plain JSON."""
    spec = spec if isinstance(spec, dict) else {}
    requests = (spec.get("resources") or {}).get("requests") or {}
    return {
        "storageClassName": spec.get("storageClassName"),
        "accessModes": list(spec.get("accessModes") or []),
        "volumeMode": spec.get("volumeMode"),
        "storage": requests.get("storage"),
    }


def live_snapshot(live_objects):
    """kubectl ``items`` (PVCs + StatefulSets) → the JSON this renderer consumes.

    ``{"pvc": {name: facts}, "sts": {name: {claim template name: facts}}}``.
    """
    snap = {"pvc": {}, "sts": {}}
    for item in live_objects or []:
        kind = item.get("kind")
        name = (item.get("metadata") or {}).get("name")
        if not name:
            continue
        if kind == "PersistentVolumeClaim":
            snap["pvc"][name] = claim_facts(item.get("spec"))
        elif kind == "StatefulSet":
            templates = (item.get("spec") or {}).get("volumeClaimTemplates") or []
            snap["sts"][name] = {
                (t.get("metadata") or {}).get("name"): claim_facts(t.get("spec"))
                for t in templates if (t.get("metadata") or {}).get("name")
            }
    return snap


def _reuse_claim_facts(spec, live, where):
    """Overwrite ``spec`` (a rendered claim spec) with ``live`` facts; log changes."""
    changes = []
    if live.get("storageClassName") and spec.get("storageClassName") != live["storageClassName"]:
        changes.append(("storageClassName", spec.get("storageClassName"), live["storageClassName"]))
        spec["storageClassName"] = live["storageClassName"]
    if live.get("accessModes") and list(spec.get("accessModes") or []) != live["accessModes"]:
        changes.append(("accessModes", spec.get("accessModes"), live["accessModes"]))
        spec["accessModes"] = list(live["accessModes"])
    if live.get("volumeMode") and spec.get("volumeMode") not in (None, live["volumeMode"]):
        changes.append(("volumeMode", spec.get("volumeMode"), live["volumeMode"]))
        spec["volumeMode"] = live["volumeMode"]
    live_size = parse_quantity(live.get("storage"))
    if live_size is not None:
        resources = spec.get("resources")
        if not isinstance(resources, dict):
            resources = spec["resources"] = {}
        requests = resources.get("requests")
        if not isinstance(requests, dict):
            requests = resources["requests"] = {}
        rendered_size = parse_quantity(requests.get("storage"))
        # Size may only grow: a smaller render is the one change to undo.
        if rendered_size is None or rendered_size < live_size:
            changes.append(("resources.requests.storage", requests.get("storage"), live["storage"]))
            requests["storage"] = live["storage"]
    for field, old, new in changes:
        sys.stderr.write(
            "pvc-keep: %s %s %r -> %r (reusing live value, #1407)\n" % (where, field, old, new)
        )
    return changes


def reuse_live_values(docs, snapshot):
    """Apply the #1407 live-value reuse to rendered docs (in place); returns docs."""
    pvcs = (snapshot or {}).get("pvc") or {}
    stss = (snapshot or {}).get("sts") or {}
    if not pvcs and not stss:
        return docs
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        name = (doc.get("metadata") or {}).get("name")
        if doc.get("kind") == "PersistentVolumeClaim" and name in pvcs:
            if not isinstance(doc.get("spec"), dict):
                doc["spec"] = {}
            _reuse_claim_facts(doc["spec"], pvcs[name], "PVC %s" % name)
        elif doc.get("kind") == "StatefulSet" and name in stss:
            live_templates = stss[name]
            for tmpl in (doc.get("spec") or {}).get("volumeClaimTemplates") or []:
                tname = (tmpl.get("metadata") or {}).get("name")
                if tname in live_templates:
                    if not isinstance(tmpl.get("spec"), dict):
                        tmpl["spec"] = {}
                    _reuse_claim_facts(
                        tmpl["spec"], live_templates[tname],
                        "StatefulSet %s volumeClaimTemplate %s" % (name, tname),
                    )
    return docs


def _parse_snapshot(argv):
    """argv[1] (optional) is the JSON produced by ``live_snapshot``."""
    if len(argv) < 2 or not argv[1]:
        return {}
    data = json.loads(argv[1])
    if not isinstance(data, dict):
        raise ValueError("live snapshot must be a JSON object")
    return data


def _load_policy():
    """``tenant_workload_policy`` under either import style (package vs. script mode)."""
    try:
        from src import tenant_workload_policy as policy
    except ImportError:
        import tenant_workload_policy as policy
    return policy


def apply_tenant_stage(docs, snapshot):
    """AI-FIX-NET-1 stage; returns docs unchanged when not requested or on ANY error.

    A policy bug must never fail a tenant's install/upgrade, so every failure is swallowed
    after being written to stderr (helm shows it only if the render itself fails).
    """
    options = (snapshot or {}).get("tenant")
    if not options:
        return docs
    try:
        policy = _load_policy()
        new_docs, log = policy.apply_tenant_policy(docs, options, (snapshot or {}).get("tenant_live"))
    except Exception as exc:  # noqa: BLE001 — fail open, see docstring
        sys.stderr.write("tenant-policy: skipped (%s: %s)\n" % (type(exc).__name__, exc))
        return docs
    for line in log:
        sys.stderr.write("tenant-policy: %s\n" % line)
    return new_docs


def main():
    snapshot = _parse_snapshot(sys.argv)
    docs = list(yaml.safe_load_all(sys.stdin))
    out = reuse_live_values(stamp_keep_on_pvcs(docs), snapshot)
    out = apply_tenant_stage(out, snapshot)
    yaml.safe_dump_all(out, sys.stdout, default_flow_style=False, sort_keys=False)


if __name__ == "__main__":
    main()
