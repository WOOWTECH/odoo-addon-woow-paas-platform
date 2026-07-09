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

HelmService invokes it as:
    helm install/upgrade ... --post-renderer <python> --post-renderer-args <this>
so it always runs under the operator's own interpreter (PyYAML guaranteed).
"""
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


def main():
    docs = list(yaml.safe_load_all(sys.stdin))
    out = stamp_keep_on_pvcs(docs)
    yaml.safe_dump_all(out, sys.stdout, default_flow_style=False, sort_keys=False)


if __name__ == "__main__":
    main()
