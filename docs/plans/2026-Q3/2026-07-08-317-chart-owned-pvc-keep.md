---
title: "#317 chart-owned PVC keep — install-time post-renderer Implementation Plan"
category: plans
status: done
created: 2026-07-08
updated: 2026-07-19
source: docs/superpowers/plans/2026-07-08-317-chart-owned-pvc-keep.md
promoted-at: 2026-07-19
---

# #317 chart-owned PVC keep — install-time post-renderer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 operator 在 `helm install`／`helm upgrade` 時，透過 post-renderer 對**渲染後 manifest 的每個 PVC** 打上 `helm.sh/resource-policy: keep`，使 chart-owned PVC（如 8gears n8n 的資料 PVC）在 `helm uninstall` 時被 helm 保留，之後由既有 orphan-label 流程 + pvc-cleanup cron（90 天 grace）接手，補齊資料保護承諾。

**Architecture:** 已用生產 helm 版本（operator image = helm **3.13.3**）實測確認：`helm uninstall` 是否刪除某資源，取決於 **install/upgrade 當時記錄進 release stored manifest** 的 `helm.sh/resource-policy` annotation，**不看 live cluster 物件**。因此「delete 時對 live PVC 打 keep」無效（實測 PVC 仍被刪）；「manifest 帶 keep」才有效（實測 PVC 留存）。修法改為 **install-time**：helm install/upgrade 加 `--post-renderer <python> --post-renderer-args <script>`，script 只對 `kind: PersistentVolumeClaim` 加 keep annotation，其餘資源原樣通過。`delete_release` **維持現狀**（先 uninstall、後標 remaining PVC）——新 install 的 PVC 因 manifest keep 而在 uninstall 後留存，正好被既有 orphan-label 流程接手。

**Tech Stack:** Python 3.11 / FastAPI / helm 3.13.3 CLI（`--post-renderer` 是 executable 機制，非 helm 4 plugin）/ PyYAML 6.0.2（operator image 已含）/ pytest（unit + `-m integration` 真 K8s）。

## Global Constraints

- **pytest 直譯器**：一律用 `extra/paas-operator/.venv/bin/python`（3.11）。系統 python 3.9 會在 collection 期因 `config.py` 的 `str | None`（PEP 604）炸掉。
- **整合測試（必跑，唯一防線）**：`kubectl config use-context docker-desktop` + `HELM_BINARY=$(which helm) .venv/bin/python -m pytest -m integration tests/integration/ -v`。叢集操作一律 `docker-desktop`，**不碰 woow-k3s**。
  - ⚠️ 本機 `which helm` 可能是 helm 4（post-renderer 是 plugin 機制、語法不同）。整合測試必須用 **helm 3.x** 對齊 operator image（helm 3.13.3）。若本機預設是 helm 4，用下載的 helm 3.13.3：`HELM_BINARY=<helm3-path>`（下載：`curl -fsSL https://get.helm.sh/helm-v3.13.3-darwin-arm64.tar.gz | tar xz`）。
- **不 bump** `src/__manifest__.py`：本任務只動 `extra/paas-operator/`。
- **commit message**：zh-tw、prefix 英文小寫（`feat:`/`fix:`/`test:`…）、**絕不含任何 AI 產生字樣**。
- **post-renderer scope 鐵律（leader 邊界 2）**：post-renderer **只准**對 `kind: PersistentVolumeClaim` 加 `helm.sh/resource-policy: keep` annotation；**嚴禁**改動任何其他資源的 render 結果（含非 PVC 資源、PVC 的非-annotation 欄位）。必須有測試證明其他資源逐字不變。
- **既有 orphan-label 接手（leader 邊界 3）**：`delete_release` 維持「uninstall 後標 remaining PVC」；留存的 chart-owned PVC 必須被此既有流程標上 orphan labels。要有測試蓋住。
- **scope（#281）**：`delete_release` 的 orphan-label 仍以 `app.kubernetes.io/instance={release}` 圈定，只碰自己 release 的 PVC。post-renderer 天然 scoped——它只作用於「該 release 自己的 rendered manifest」，不會碰到別的 service。

## 設計決策與已知 concern（供 plan-review 檢視）

1. **為何 install-time 而非 issue 原述的 delete-time**：issue 描述「delete 時對 live PVC 打 keep」經實測（helm 3.13.3 + helm 4）**技術無效**——helm uninstall 依 stored manifest 判斷 resource-policy，不讀 live 物件。Eugene 已據此改拍板為 install-time post-renderer（本 plan）。「helm 只讀 stored manifest」的發現由 Eugene 同步至 issue #317 留言。
2. **install 與 upgrade 都要帶 post-renderer**：`helm upgrade` 會重新 render 並覆寫 stored manifest；若 upgrade 不帶 post-renderer，新 revision manifest 會失去 keep，下次 uninstall 就刪 PVC（regress）。故兩條路徑都必須帶。這也順帶提供既存 release 的 backfill 機制（`helm upgrade --post-renderer` 注入 keep）。
3. **不覆蓋 chart 明確設定的 resource-policy**：post-renderer 用 `setdefault`——若 chart 作者已明確設 `helm.sh/resource-policy`（含 `delete`），尊重其意圖不覆蓋。絕大多數 chart 的 PVC 無此 annotation（正是被刪的原因），會被補上 keep。
4. **已知 concern — rollback 到 pre-keep revision**：`helm rollback` 用舊 revision 的 stored manifest；rollback 到 #317 上線前（無 keep）的 revision 後，該 revision 若被 uninstall 會刪 PVC。屬過渡期風險（rollback 在本平台罕用；suspend/resume 走 scale 不走 rollback），本 PR 不處理，列為 concern。
5. **既存 CS backfill 不在本 PR rollout（leader 邊界 4）**：stg 4 顆既存 CS 的 manifest 仍無 keep，需一輪 `helm upgrade --post-renderer` 補上。**不自行 rollout**，寫入 PR 描述當 ops follow-up 由 leader 排。
6. **post-renderer 執行方式**：以 `--post-renderer <sys.executable> --post-renderer-args <script>` 呼叫（helm 3.13.3 支援 `--post-renderer-args`，實測可行），用 operator 自己的 python 跑 script，保證有 PyYAML，不依賴 `$PATH`／shebang／executable bit。script 放 `src/`（Dockerfile `COPY src/` 已涵蓋，無需改 Dockerfile、無需 chmod）。

---

## File Structure

- `extra/paas-operator/src/pvc_keep_post_renderer.py`（新）
  - post-renderer script：純函式 `stamp_keep_on_pvcs(docs)` + `main()`（stdin→stdout）。只對 PVC 加 keep。放 `src/` 下以進 image 且可被測試 import。
- `extra/paas-operator/src/services/helm.py`（改）
  - 加 `import sys`；module 級常數 `_PVC_KEEP_POST_RENDERER`（script 絕對路徑）+ helper `_pvc_keep_post_renderer_args()`；`install()` 與 `upgrade()` 各 `args.extend(_pvc_keep_post_renderer_args())`。
- `extra/paas-operator/tests/test_pvc_keep_post_renderer.py`（新）
  - `stamp_keep_on_pvcs` 純函式單元測試（PVC 加 keep、非 PVC 不動、保留既有 annotation、不覆蓋 explicit policy、None doc、null annotations）+ 一個 subprocess 跑 script 的 stdin/stdout 契約測試。
- `extra/paas-operator/tests/test_helm.py`（改）
  - install/upgrade 帶 `--post-renderer sys.executable --post-renderer-args <script>` 的單元測試。
- `extra/paas-operator/tests/integration/conftest.py`（改）
  - app_client fixture 補 `ALLOW_UNSET_PAAS_ENV`（dev/local 繞過 #279 F6 env guard，否則 operator lifespan 啟動即 abort）。
- `extra/paas-operator/tests/integration/test_pvc_keep_before_uninstall.py`（新）
  - 端到端（真 helm 3 + 真 kubectl，全程走 operator HTTP）：operator install chart-owned PVC → PVC 帶 keep（post-renderer 生效）、同 chart 的 ConfigMap 不帶 keep（scope）→ operator delete → PVC 留存 + orphan-labeled（既有流程接手）；StatefulSet volumeClaimTemplate 不回歸。

---

### Task 1: post-renderer script + 純函式單元測試

**Files:**
- Create: `extra/paas-operator/src/pvc_keep_post_renderer.py`
- Test: `extra/paas-operator/tests/test_pvc_keep_post_renderer.py`

**Interfaces:**
- Produces: `stamp_keep_on_pvcs(docs: list) -> list` — 對每個 `kind == "PersistentVolumeClaim"` 的 dict doc 於 `metadata.annotations` `setdefault("helm.sh/resource-policy", "keep")`；丟掉 `None` doc；非 PVC doc 原樣返回。`main()` 從 stdin 讀 multi-doc YAML、套用、寫回 stdout。Task 2 由 helm CLI 以 `<python> <script>` 呼叫 `main()`。

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_pvc_keep_post_renderer.py`:

```python
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
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd extra/paas-operator && .venv/bin/python -m pytest tests/test_pvc_keep_post_renderer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.pvc_keep_post_renderer'`

- [ ] **Step 3: 實作 post-renderer script**

Create `src/pvc_keep_post_renderer.py`:

```python
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
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd extra/paas-operator && .venv/bin/python -m pytest tests/test_pvc_keep_post_renderer.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: commit**

```bash
git add extra/paas-operator/src/pvc_keep_post_renderer.py extra/paas-operator/tests/test_pvc_keep_post_renderer.py
git commit -m "feat: #317 新增 PVC-keep helm post-renderer（render 時給 PVC 打 resource-policy: keep）"
```

---

### Task 2: helm.py install/upgrade 帶 post-renderer

**Files:**
- Modify: `extra/paas-operator/src/services/helm.py`（imports 加 `sys`；`logger = ...` 後加 module 常數 + helper；`install()` args（約 433 行 base args 後）與 `upgrade()` args（約 561 行 base args 後）各 extend）
- Test: `extra/paas-operator/tests/test_helm.py`（`TestHelmService` class 內）

**Interfaces:**
- Consumes: Task 1 的 `src/pvc_keep_post_renderer.py`（路徑常數指向它）。
- Produces: `_pvc_keep_post_renderer_args() -> List[str]` 回 `["--post-renderer", sys.executable, "--post-renderer-args", <script abs path>]`；install/upgrade 的 helm 指令都含這些 flag。Task 3 端到端驗證其效果。

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_helm.py` 的 `TestHelmService` class 內、`test_install_success` 之後新增：

```python
    @patch("subprocess.run")
    def test_install_adds_pvc_keep_post_renderer(self, mock_run, helm_service):
        """#317: install must run the PVC-keep post-renderer so chart-owned PVCs
        get helm.sh/resource-policy: keep baked into the STORED manifest (helm
        uninstall reads the manifest, not live objects — so post-teardown labeling
        alone can't save chart-owned PVC data). Invoked via the operator's own
        python + --post-renderer-args so PyYAML is always present."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "r", "namespace": "paas-ws-test",
                "info": {"status": "deployed", "revision": 1, "last_deployed": ""},
                "chart": {"metadata": {"name": "n8n", "appVersion": "1.0.0"}},
            }),
            stderr="",
        )
        helm_service.install(namespace="paas-ws-test", name="r", chart="n8n")
        cmd = mock_run.call_args[0][0]
        assert "--post-renderer" in cmd
        assert cmd[cmd.index("--post-renderer") + 1] == sys.executable
        assert "--post-renderer-args" in cmd
        assert cmd[cmd.index("--post-renderer-args") + 1].endswith(
            "pvc_keep_post_renderer.py"
        )

    @patch("subprocess.run")
    def test_upgrade_adds_pvc_keep_post_renderer(self, mock_run, helm_service):
        """#317: upgrade must ALSO run the post-renderer — it rewrites the stored
        manifest, so without it an upgrade drops keep and the next uninstall
        deletes the PVC (regression). This is also the backfill path for existing
        releases."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "r", "namespace": "paas-ws-test",
                "info": {"status": "deployed", "revision": 2, "last_deployed": ""},
                "chart": {"metadata": {"name": "n8n", "appVersion": "1.0.0"}},
            }),
            stderr="",
        )
        helm_service.upgrade(namespace="paas-ws-test", name="r", chart="n8n")
        cmd = mock_run.call_args[0][0]
        assert "--post-renderer" in cmd
        assert cmd[cmd.index("--post-renderer") + 1] == sys.executable
        assert cmd[cmd.index("--post-renderer-args") + 1].endswith(
            "pvc_keep_post_renderer.py"
        )
```

> `sys` 需在 `test_helm.py` 頂部 import（現有頂部沒有 `import sys`）：於 import 區加 `import sys`。`json`、`Mock`、`patch` 已 import。

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd extra/paas-operator && .venv/bin/python -m pytest tests/test_helm.py -k "pvc_keep_post_renderer" -v`
Expected: FAIL — `--post-renderer` 不在 cmd（`AssertionError`）。

- [ ] **Step 3: 實作**

在 `src/services/helm.py` 頂部 import 區加 `import sys`（放在 `import subprocess` 附近，維持字母序）：

```python
import subprocess
import sys
import tempfile
```

在 `logger = logging.getLogger(__name__)` 之後、`class HelmService` 之前加：

```python
# #317: post-renderer that stamps helm.sh/resource-policy: keep on every PVC so
# chart-owned PVCs survive `helm uninstall`. helm decides deletion from the STORED
# manifest, not live objects, so keep must be present at install/upgrade time.
# Run via the operator's own interpreter (sys.executable) so PyYAML is guaranteed,
# regardless of $PATH / shebang / executable bit.
_PVC_KEEP_POST_RENDERER = str(
    Path(__file__).resolve().parent.parent / "pvc_keep_post_renderer.py"
)


def _pvc_keep_post_renderer_args() -> List[str]:
    """Helm CLI flags to run the PVC-keep post-renderer (shared by install/upgrade)."""
    return [
        "--post-renderer", sys.executable,
        "--post-renderer-args", _PVC_KEEP_POST_RENDERER,
    ]
```

在 `install()` 中，把 base args list 與其後的 `if create_namespace:`——

```python
        args = [
            "install",
            name,
            chart,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if create_namespace:
```

——改為（在 base args list `]` 之後、`if create_namespace:` **之前**插入，即所有 conditional flag 之前；flag 順序對 helm 無關緊要）：

```python
        args = [
            "install",
            name,
            chart,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]
        # #317: PVC-keep post-renderer — before ALL conditional flags.
        args.extend(_pvc_keep_post_renderer_args())

        if create_namespace:
```

在 `upgrade()` 中，把 base args list 與其後的 `if version:`——

```python
        args = [
            "upgrade",
            name,
            chart,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if version:
```

——改為（在 base args list `]` 之後、`if version:` **之前**插入。關鍵：這是在 `if reset_values / elif reuse_values` 兩個分支**之前**，所以 `--reset-values` 與 `--reuse-values` 兩種 upgrade 變體都帶 post-renderer；若漏了任一分支，該變體的 upgrade 會改寫 stored manifest 卻不含 keep，下次 uninstall 就刪 PVC——regress #317）：

```python
        args = [
            "upgrade",
            name,
            chart,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]
        # #317: PVC-keep post-renderer — MUST match install. Placed before ALL
        # conditional flags (incl. both the --reset-values and --reuse-values
        # branches) so every upgrade variant rewrites the stored manifest WITH
        # keep; otherwise the next uninstall would delete the PVC.
        args.extend(_pvc_keep_post_renderer_args())

        if version:
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd extra/paas-operator && .venv/bin/python -m pytest tests/test_helm.py -k "pvc_keep_post_renderer" -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 跑全 test_helm.py 確認既有 install/upgrade 測試不回歸**

Run: `cd extra/paas-operator && .venv/bin/python -m pytest tests/test_helm.py -v`
Expected: 全綠（既有 `test_install_*`、`test_upgrade_*` 等仍 pass；它們只斷言各自關心的 flag，多出 --post-renderer 不影響）。

- [ ] **Step 6: commit**

```bash
git add extra/paas-operator/src/services/helm.py extra/paas-operator/tests/test_helm.py
git commit -m "feat: #317 helm install/upgrade 帶 PVC-keep post-renderer"
```

---

### Task 3: 端到端整合測試（真 helm 3 + operator HTTP）

**Files:**
- Modify: `extra/paas-operator/tests/integration/conftest.py`（app_client fixture 補 ALLOW_UNSET_PAAS_ENV）
- Create: `extra/paas-operator/tests/integration/test_pvc_keep_before_uninstall.py`

**Interfaces:**
- Consumes: conftest fixtures `app_client`、`test_namespace`；環境 `HELM_BINARY`（必須是 helm 3.x）。
- Produces: 無（純驗證）。這是 mock（mock 掉 helm）結構上做不到的契約防線——真 helm 對 stored-manifest keep 的實際保留行為。

- [ ] **Step 1: conftest 補 ALLOW_UNSET_PAAS_ENV**

在 `tests/integration/conftest.py` 的 `app_client` fixture，把：

```python
    api_key = os.environ.get("API_KEY") or "integration-test-key"
    os.environ["API_KEY"] = api_key
    os.environ.setdefault("NAMESPACE_PREFIX", "paas-ws-test-op-")

    from src.config import settings
    from src.main import app
    settings.api_key = api_key
    with TestClient(app, headers={"X-API-Key": api_key}) as client:
        yield client
```

改為：

```python
    api_key = os.environ.get("API_KEY") or "integration-test-key"
    os.environ["API_KEY"] = api_key
    os.environ.setdefault("NAMESPACE_PREFIX", "paas-ws-test-op-")
    # #279 F6 env guard: an unset PAAS_ENV aborts boot on the shared prod/stg
    # cluster (cross-env label collision). Integration tests run against a
    # single-env dev/local cluster (docker-desktop), so opt into the documented
    # dev/local bypass rather than pinning a fake PAAS_ENV. The test prefix
    # (paas-ws-test-op-) names no real env, so the bypass is accepted. Forced onto
    # the settings singleton too (it may have been read at import, like api_key).
    os.environ.setdefault("ALLOW_UNSET_PAAS_ENV", "true")

    from src.config import settings
    from src.main import app
    settings.api_key = api_key
    settings.allow_unset_paas_env = True
    with TestClient(app, headers={"X-API-Key": api_key}) as client:
        yield client
```

- [ ] **Step 2: 寫端到端測試**

Create `tests/integration/test_pvc_keep_before_uninstall.py`:

```python
"""Integration test for operator #317: chart-owned PVCs must survive teardown.

helm decides deletion on `helm uninstall` from the annotation in the STORED
release manifest, NOT the live object (verified: helm 3.13.3). So a chart-owned
PVC (a plain PVC template, e.g. 8gears n8n data) shipping without
`helm.sh/resource-policy: keep` is deleted outright — and the post-teardown
orphan-labeling had nothing left to stamp. The fix is a helm post-renderer that
bakes keep into the manifest at install time (so uninstall skips the PVC).

The unit suite mocks helm, so it cannot exercise helm's real resource-policy
honouring — the exact blind spot this file covers. It shells out to real helm 3 +
kubectl through the operator's own HTTP endpoints (docker-desktop for dev).

Run::

    kubectl config use-context docker-desktop
    HELM_BINARY=<helm3>  # must be helm 3.x (operator image = 3.13.3)
    HELM_BINARY=$HELM_BINARY .venv/bin/python -m pytest -m integration \
        tests/integration/test_pvc_keep_before_uninstall.py -v
"""
import subprocess

import pytest

pytestmark = pytest.mark.integration

_ORPHAN_AT = "paas.woowtech.io/orphaned-at"


def _write_chart_owned_pvc_chart(chart_dir) -> str:
    """Minimal chart: a plain (chart-owned) PVC with the helm instance label and
    NO resource-policy (like 8gears n8n data), plus a ConfigMap to prove the
    post-renderer's scope (only the PVC gets keep). Returns chart path."""
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: pvc-keep-probe\nversion: 0.1.0\n"
    )
    templates = chart_dir / "templates"
    templates.mkdir(exist_ok=True)
    (templates / "pvc.yaml").write_text(
        "apiVersion: v1\n"
        "kind: PersistentVolumeClaim\n"
        "metadata:\n"
        "  name: {{ .Release.Name }}-data\n"
        "  labels:\n"
        "    app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "spec:\n"
        "  accessModes: [ReadWriteOnce]\n"
        "  resources:\n"
        "    requests:\n"
        "      storage: 8Mi\n"
    )
    (templates / "cm.yaml").write_text(
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: {{ .Release.Name }}-cfg\n"
        "  labels:\n"
        "    app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "data:\n"
        "  hello: world\n"
    )
    return str(chart_dir)


def _get(namespace: str, kind: str, name: str, jsonpath: str) -> str:
    return subprocess.run(
        ["kubectl", "get", kind, name, "-n", namespace, "-o", jsonpath],
        capture_output=True, text=True,
    ).stdout


def test_operator_install_keeps_chart_owned_pvc_through_teardown(
    app_client, test_namespace, tmp_path
):
    """End-to-end through operator HTTP: install a chart-owned PVC, then delete.
    The post-renderer must bake keep at install (PVC only, not the ConfigMap), and
    the PVC must survive uninstall and get orphan-labeled by the existing teardown.

    With the bug present the PVC would be GONE after delete (helm deletes it), so a
    bare "PVC still exists" check is itself the #317 guard.
    """
    chart = _write_chart_owned_pvc_chart(tmp_path / "chart")
    release = "probe-n8n"
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": release, "chart": chart,
    })
    assert r.status_code == 201, r.text

    pvc = f"{release}-data"
    # post-renderer stamped keep at install — on the PVC …
    keep = _get(test_namespace, "pvc", pvc,
                r"jsonpath={.metadata.annotations.helm\.sh/resource-policy}")
    assert keep == "keep", f"post-renderer must stamp keep on the PVC, got {keep!r}"
    # … and NOT on the ConfigMap (scope rule).
    cm_keep = _get(test_namespace, "cm", f"{release}-cfg",
                   r"jsonpath={.metadata.annotations.helm\.sh/resource-policy}")
    assert cm_keep == "", f"post-renderer must NOT touch non-PVC resources, got {cm_keep!r}"

    # Tear down through the operator (helm uninstall + existing orphan-labeling).
    r = app_client.delete(f"/api/releases/{test_namespace}/{release}")
    assert r.status_code == 200, r.text

    # PVC survived helm uninstall (the #317 fix) …
    survived = _get(test_namespace, "pvc", pvc, "jsonpath={.metadata.name}")
    assert survived == pvc, (
        f"chart-owned PVC {pvc!r} was deleted by helm uninstall — #317 data-loss "
        "(keep not baked into the stored manifest at install)"
    )
    # … and the existing teardown path orphan-labeled it (leader boundary 3).
    orphaned = _get(test_namespace, "pvc", pvc,
                    r"jsonpath={.metadata.labels.paas\.woowtech\.io/orphaned-at}")
    assert orphaned.isdigit(), (
        f"leftover PVC must be orphan-labeled so the cleanup cron's -l {_ORPHAN_AT} "
        f"selector matches; got {orphaned!r}"
    )


def test_statefulset_volumeclaimtemplate_pvc_not_regressed(
    app_client, test_namespace, tmp_path
):
    """#281 non-regression: a StatefulSet volumeClaimTemplate PVC (helm never
    deletes it) must still be orphan-labeled after teardown. Proves the install-
    time post-renderer change did not disturb the pre-existing STS behaviour."""
    chart_dir = tmp_path / "sts-chart"
    (chart_dir / "templates").mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: sts-probe\nversion: 0.1.0\n"
    )
    (chart_dir / "templates" / "sts.yaml").write_text(
        "apiVersion: apps/v1\n"
        "kind: StatefulSet\n"
        "metadata:\n"
        "  name: {{ .Release.Name }}\n"
        "  labels:\n"
        "    app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "spec:\n"
        "  serviceName: {{ .Release.Name }}\n"
        "  replicas: 1\n"
        "  selector:\n"
        "    matchLabels:\n"
        "      app: {{ .Release.Name }}\n"
        "  template:\n"
        "    metadata:\n"
        "      labels:\n"
        "        app: {{ .Release.Name }}\n"
        "        app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "    spec:\n"
        "      containers:\n"
        "      - name: pause\n"
        "        image: registry.k8s.io/pause:3.9\n"
        "  volumeClaimTemplates:\n"
        "  - metadata:\n"
        "      name: data\n"
        "      labels:\n"
        "        app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "    spec:\n"
        "      accessModes: [ReadWriteOnce]\n"
        "      resources:\n"
        "        requests:\n"
        "          storage: 8Mi\n"
    )
    release = "probe-sts"
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": release, "chart": str(chart_dir),
    })
    assert r.status_code == 201, r.text

    # STS controller creates the PVC named data-{release}-0; wait for it to appear.
    pvc = f"data-{release}-0"
    appeared = False
    for _ in range(60):  # up to ~30s
        if _get(test_namespace, "pvc", pvc, "jsonpath={.metadata.name}") == pvc:
            appeared = True
            break
        subprocess.run(["sleep", "0.5"])
    assert appeared, f"STS volumeClaimTemplate PVC {pvc!r} never appeared"

    r = app_client.delete(f"/api/releases/{test_namespace}/{release}")
    assert r.status_code == 200, r.text

    survived = _get(test_namespace, "pvc", pvc, "jsonpath={.metadata.name}")
    assert survived == pvc, f"STS PVC {pvc!r} unexpectedly gone after teardown"
    orphaned = _get(test_namespace, "pvc", pvc,
                    r"jsonpath={.metadata.labels.paas\.woowtech\.io/orphaned-at}")
    assert orphaned.isdigit(), (
        f"STS PVC lost its orphan-at label after #317 — regression; got {orphaned!r}"
    )
```

- [ ] **Step 3: 確認 context 與 helm 版本**

Run: `kubectl config current-context`（應為 `docker-desktop`，否則 `kubectl config use-context docker-desktop`）
Run: `<helm3> version --short`（確認 3.x）

- [ ] **Step 4: 跑新整合測試確認 pass**

Run: `cd extra/paas-operator && HELM_BINARY=<helm3-path> .venv/bin/python -m pytest -m integration tests/integration/test_pvc_keep_before_uninstall.py -v`
Expected: PASS（2 passed）。STS 測試若因 PVC binding 慢而 flaky，確認 `kubectl get sc` 有 default SC；pause image 極小，PVC object 應數秒內出現。

- [ ] **Step 5: 跑全整合套件確認無回歸**

Run: `cd extra/paas-operator && HELM_BINARY=<helm3-path> .venv/bin/python -m pytest -m integration tests/integration/ -v`
Expected: 全綠（既有 `test_pvc_orphan_labeling.py`、`test_ns_env_labeling.py`、`test_scale.py` + 新檔）。teardown 後 `kubectl get ns | grep paas-ws-test-op-` 應為空或即將消失。

- [ ] **Step 6: commit**

```bash
git add extra/paas-operator/tests/integration/conftest.py extra/paas-operator/tests/integration/test_pvc_keep_before_uninstall.py
git commit -m "test: #317 端到端整合測試（operator install 打 keep、delete 後 PVC 留存+orphan、STS 不回歸）"
```

---

## Self-Review

**1. Spec coverage（對照 issue #317 驗收 + leader 邊界）:**
- 「chart-owned PVC delete 後：PVC 留存、帶 orphan 標與 grace」→ Task 1+2（install-time keep 進 manifest）+ Task 3 `test_operator_install_keeps_chart_owned_pvc_through_teardown`。✓
- 「StatefulSet volumeClaimTemplate 不回歸」→ Task 3 `test_statefulset_volumeclaimtemplate_pvc_not_regressed`。✓
- 「scope：只碰自己 release」→ post-renderer 只作用該 release 的 rendered manifest；delete_release 的 orphan-label 仍 instance-scoped（未改動）。✓
- leader 邊界 2（post-renderer 只加 PVC keep、不動其他）→ Task 1 `test_leaves_non_pvc_untouched` + Task 3 ConfigMap 斷言。✓
- leader 邊界 3（留存 PVC 被既有 orphan-label 接手）→ delete_release 未改 + Task 3 orphan-at 斷言。✓
- leader 邊界 4（既存 CS backfill 不自 rollout）→ 寫入 PR 描述 ops follow-up（決策 5）。✓
- upgrade 一致性（不 regress keep）→ Task 2 `test_upgrade_adds_pvc_keep_post_renderer`。✓

**2. Placeholder scan:** 無 TBD/TODO；每個 code step 為完整可貼上內容。`<helm3-path>` 是執行者環境變數（非程式碼佔位），已在 Global Constraints 說明取得方式。✓

**3. Type consistency:** `stamp_keep_on_pvcs(docs: list) -> list`（Task 1 定義、Task 1 subprocess 測試與 Task 3 端到端一致使用）；`_pvc_keep_post_renderer_args() -> List[str]`（Task 2 定義、install/upgrade 一致 extend）；`_PVC_KEEP_POST_RENDERER` 指向 `src/pvc_keep_post_renderer.py`（Task 1 建立的檔）。✓

<!-- peer-reviewed: 2026-07-08T23:53:59Z rounds=2 verdict=approved by=miniclaude -->
