"""Unit tests for the #317 PVC-keep helm post-renderer."""
import subprocess
import sys
from pathlib import Path

import yaml

from src.pvc_keep_post_renderer import stamp_keep_on_pvcs

_SCRIPT = Path(__file__).resolve().parent.parent / "src" / "pvc_keep_post_renderer.py"


def test_stamps_keep_on_pvc():
    docs = [{"kind": "PersistentVolumeClaim", "metadata": {"name": "data"}}]
    out = stamp_keep_on_pvcs(docs)
    assert out[0]["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_leaves_non_pvc_untouched():
    """scope rule: non-PVC resources pass through unchanged (no keep added)."""
    docs = [
        {"kind": "ConfigMap", "metadata": {"name": "cfg"}},
        {"kind": "Deployment", "metadata": {"name": "web", "annotations": {"a": "b"}}},
    ]
    out = stamp_keep_on_pvcs(docs)
    assert out[0]["metadata"].get("annotations", {}) == {}
    assert out[1]["metadata"]["annotations"] == {"a": "b"}


def test_preserves_existing_pvc_annotations():
    docs = [{"kind": "PersistentVolumeClaim",
             "metadata": {"name": "d", "annotations": {"foo": "bar"}}}]
    out = stamp_keep_on_pvcs(docs)
    ann = out[0]["metadata"]["annotations"]
    assert ann["foo"] == "bar"
    assert ann["helm.sh/resource-policy"] == "keep"


def test_does_not_clobber_explicit_resource_policy():
    """A chart author's explicit resource-policy is respected (setdefault)."""
    docs = [{"kind": "PersistentVolumeClaim",
             "metadata": {"name": "d",
                          "annotations": {"helm.sh/resource-policy": "delete"}}}]
    out = stamp_keep_on_pvcs(docs)
    assert out[0]["metadata"]["annotations"]["helm.sh/resource-policy"] == "delete"


def test_drops_none_docs():
    """Blank YAML docs parse to None and must be dropped, not re-emitted."""
    docs = [None, {"kind": "PersistentVolumeClaim", "metadata": {"name": "d"}}, None]
    out = stamp_keep_on_pvcs(docs)
    assert len(out) == 1
    assert out[0]["kind"] == "PersistentVolumeClaim"


def test_handles_null_annotations_block():
    """helm can render `annotations:` (null) — must not crash."""
    docs = [{"kind": "PersistentVolumeClaim",
             "metadata": {"name": "d", "annotations": None}}]
    out = stamp_keep_on_pvcs(docs)
    assert out[0]["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_main_reads_stdin_writes_stdout():
    """CLI contract helm relies on: multi-doc YAML in on stdin, transformed out."""
    manifest = (
        "kind: PersistentVolumeClaim\nmetadata:\n  name: d\n"
        "---\nkind: ConfigMap\nmetadata:\n  name: c\n"
    )
    r = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=manifest, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    out = [d for d in yaml.safe_load_all(r.stdout) if d]
    pvc = next(d for d in out if d["kind"] == "PersistentVolumeClaim")
    cm = next(d for d in out if d["kind"] == "ConfigMap")
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert cm["metadata"].get("annotations", {}) == {}
