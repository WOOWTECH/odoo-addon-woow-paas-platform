# PR-278 NS Quota/LimitRange Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（本輪走 ultracode workflow orchestration）。Steps use `- [ ]`.

**Goal:** 補平台級 quota 安全網——CS ns quota drift 修（權威覆寫）、CS + agent ns 加 LimitRange、agent ns 補 per-project ResourceQuota，堵住 agent 零-quota cluster DoS 逃逸口。

**Architecture:** 三處 operator 改動（`extra/paas-operator/src/`）+ 一處 Odoo 改動（`src/`）。operator 加 manifest-builder 純函式 + apply、加 PUT quota 端點；Odoo 抽權威 quota 計算 + 刪除路徑重算覆寫。spec：`docs/superpowers/specs/2026-07-07-w1-ns-quota-governance-design.md`（miniclaude 2 輪 APPROVED）。

**Tech Stack:** FastAPI + kubectl（operator，pytest 原生）；Odoo 18 ORM（`cloud_service.py` + `paas_operator.py` client，TransactionCase）。

## Global Constraints

- operator manifest 值一律**字串 quantity**（`"3"`/`"3Gi"`/`"500m"`），與 `NamespaceCreateRequest.cpu_limit:str` 及既有 quota 一致。
- operator 任何 mutate ns 的方法**第一行 `validate_namespace(name)`**（fail-closed prefix 守衛，raise ValueError→400）。
- operator API 認證是**全域 middleware**（`main.py:93 verify_api_key`）→ 新端點**不加** `Depends`。
- **B2 LimitRange 是 B1 的必要配套（correctness）**：ns quota 一旦 hard 宣告 `limits.cpu/memory`，admission 拒收未宣告 limits 的容器；LimitRange `default` 補上。兩者同 PR、同落點。
- Odoo 刪除路徑的 quota 重算是 **best-effort**（catch + log，**絕不 abort 刪除**）；operator client 為 None（未設定）時 no-op。
- LimitRange default/defaultRequest = **cpu 500m / mem 512Mi**（CS + agent 同值；只填未宣告 limits 的容器，declared 容器不受影響）。
- A1 quota 覆寫用 `apply` + **完整 6-key hard map**（含 `persistentvolumeclaims:"10"`），無 stale key 問題。
- A1 last-CS edge：workspace 清空後 survivor 集為空 → floor `8/8Gi/100Gi`，**PUT floor**（不 skip、不刪 quota；ns 刪除是 #277）。

---

## Task 1: operator — CS ns LimitRange（#278 A2）

**Files:**
- Modify: `extra/paas-operator/src/services/helm.py`（create_namespace ~925 插入 + 新 builder）
- Test: `extra/paas-operator/tests/test_helm.py`

**Interfaces:**
- Produces: `KubernetesService._build_limitrange_manifest(name: str) -> dict`（純函式，Task 3 agent 側也複用同形狀但另建 agent builder）。

- [ ] **Step 1: 失敗測試**（test_helm.py，`helm_service` fixture 既有）

```python
def test_build_limitrange_manifest_shape(self, helm_service):
    m = helm_service._build_limitrange_manifest("paas-ws-test-abc")
    assert m["kind"] == "LimitRange"
    assert m["metadata"]["name"] == "paas-ws-test-abc-limits"
    assert m["metadata"]["namespace"] == "paas-ws-test-abc"
    entry = m["spec"]["limits"][0]
    assert entry["type"] == "Container"
    assert entry["default"] == {"cpu": "500m", "memory": "512Mi"}
    assert entry["defaultRequest"] == {"cpu": "500m", "memory": "512Mi"}

@patch("subprocess.run")
def test_create_namespace_applies_limitrange(self, mock_run, helm_service):
    mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
    helm_service.create_namespace("paas-ws-test-abc", "8", "8Gi", "100Gi")
    applied = [c.args[0] for c in mock_run.call_args_list]  # each = full kubectl argv
    # 至少一次 apply 帶 -f，且 create_namespace 現在做 ns+quota+limitrange 三次 apply
    apply_calls = [a for a in applied if "apply" in a and "-f" in a]
    assert len(apply_calls) >= 3
```

- [ ] **Step 2: 跑，確認紅**：`cd extra/paas-operator && pytest tests/test_helm.py -k limitrange -q` → FAIL（`_build_limitrange_manifest` 不存在）。

- [ ] **Step 3: 實作**（helm.py，`create_namespace` 附近加 builder + 插入 apply）

```python
# KubernetesService 內新方法（放 create_namespace 之後、_apply_baseline_netpol 之前）
def _build_limitrange_manifest(self, name: str) -> dict:
    """CS ns LimitRange（#278 A2）：ns quota hard-宣告 limits.cpu/memory 後，
    admission 拒收未設 limits 的容器；default 補上安全預設讓其可建。"""
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {"name": f"{name}-limits", "namespace": name},
        "spec": {"limits": [{
            "type": "Container",
            "default": {"cpu": "500m", "memory": "512Mi"},
            "defaultRequest": {"cpu": "500m", "memory": "512Mi"},
        }]},
    }
```

在 `create_namespace` 的 quota apply 之後（現 helm.py:925 `Path(quota_file).unlink(...)` 後）、`self.ensure_registry_secret(name)`（:927）之前插入：

```python
        # #278 A2: CS ns LimitRange（quota 之後、registry secret 之前）
        import yaml
        limits_manifest = self._build_limitrange_manifest(name)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(limits_manifest, f)
            limits_file = f.name
        try:
            self._run_command(["apply", "-f", limits_file])
        finally:
            Path(limits_file).unlink(missing_ok=True)
```

- [ ] **Step 4: 跑，確認綠**：`pytest tests/test_helm.py -k "limitrange or create_namespace" -q` → PASS。
- [ ] **Step 5: commit**：`test: #278 A2 CS ns LimitRange（quota hard-limits 兜底）`

---

## Task 2: operator — PUT /api/namespaces/{name}/quota（#278 A1 operator 側）

**Files:**
- Modify: `extra/paas-operator/src/models/schemas.py`（+request model）
- Modify: `extra/paas-operator/src/services/helm.py`（+update_namespace_quota）
- Modify: `extra/paas-operator/src/api/namespaces.py`（+PUT 端點 + import）
- Test: `extra/paas-operator/tests/test_helm.py` + `tests/test_api.py`

**Interfaces:**
- Produces: `KubernetesService.update_namespace_quota(name, cpu_limit, memory_limit, storage_limit) -> dict`；`PUT /api/namespaces/{name}/quota`；`NamespaceQuotaUpdateRequest`。

- [ ] **Step 1: 失敗測試**

test_helm.py：
```python
@patch("subprocess.run")
def test_update_namespace_quota_applies_full_hard_map(self, mock_run, helm_service):
    mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
    helm_service.update_namespace_quota("paas-ws-test-abc", "12", "12Gi", "150Gi")
    apply_calls = [c.args[0] for c in mock_run.call_args_list if "apply" in c.args[0]]
    assert apply_calls, "should apply the quota"

def test_update_namespace_quota_rejects_foreign_ns(self, helm_service):
    with pytest.raises(ValueError):
        helm_service.update_namespace_quota("not-a-paas-ns", "8", "8Gi", "100Gi")
```

test_api.py（`client` fixture 既有）：
```python
@patch("src.api.namespaces.k8s_service")
def test_put_quota_ok(self, mock_k8s, client):
    mock_k8s.update_namespace_quota.return_value = {"message": "ok"}
    r = client.put("/api/namespaces/paas-ws-test-abc/quota",
                   json={"cpu_limit": "12", "memory_limit": "12Gi", "storage_limit": "150Gi"})
    assert r.status_code == 200
    mock_k8s.update_namespace_quota.assert_called_once_with(
        "paas-ws-test-abc", "12", "12Gi", "150Gi")

@patch("src.api.namespaces.k8s_service")
def test_put_quota_bad_ns_400(self, mock_k8s, client):
    mock_k8s.update_namespace_quota.side_effect = ValueError("bad prefix")
    r = client.put("/api/namespaces/xxx/quota",
                   json={"cpu_limit": "8", "memory_limit": "8Gi", "storage_limit": "100Gi"})
    assert r.status_code == 400
```

- [ ] **Step 2: 跑紅**：`pytest tests/test_helm.py tests/test_api.py -k quota -q` → FAIL。

- [ ] **Step 3: 實作**

schemas.py（NamespaceCreateRequest 附近）：
```python
class NamespaceQuotaUpdateRequest(BaseModel):
    """#278 A1：整組覆寫 ns ResourceQuota（值由 Odoo 依存活 service 集算好傳入）。"""
    cpu_limit: str = Field(..., description="requests.cpu / limits.cpu")
    memory_limit: str = Field(..., description="requests.memory / limits.memory, e.g. 8Gi")
    storage_limit: str = Field(..., description="requests.storage, e.g. 100Gi")
```

helm.py（KubernetesService 新方法；複用 create_namespace 的 quota_manifest 形狀）：
```python
def update_namespace_quota(self, name: str, cpu_limit: str,
                           memory_limit: str, storage_limit: str) -> Dict[str, str]:
    """#278 A1：整組 hard overwrite ns ResourceQuota（不動 ns/NP/secret）。
    apply 完整 6-key hard map（含 pvc=10），無 stale key。"""
    validate_namespace(name)
    import yaml
    quota_manifest = {
        "apiVersion": "v1", "kind": "ResourceQuota",
        "metadata": {"name": f"{name}-quota", "namespace": name},
        "spec": {"hard": {
            "requests.cpu": cpu_limit, "requests.memory": memory_limit,
            "limits.cpu": cpu_limit, "limits.memory": memory_limit,
            "persistentvolumeclaims": "10", "requests.storage": storage_limit,
        }},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(quota_manifest, f)
        quota_file = f.name
    try:
        self._run_command(["apply", "-f", quota_file])
    finally:
        Path(quota_file).unlink(missing_ok=True)
    return {"message": f"Namespace {name} quota updated"}
```

namespaces.py（import 補 `NamespaceQuotaUpdateRequest`；加端點，複用 baseline-netpol 的 error 範式）：
```python
@router.put("/{name}/quota", response_model=Dict[str, str])
async def update_namespace_quota(name: str, request: NamespaceQuotaUpdateRequest):
    try:
        result = k8s_service.update_namespace_quota(
            name, request.cpu_limit, request.memory_limit, request.storage_limit)
        logger.info(f"Updated quota for namespace {name}")
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update quota for {name}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update quota: {str(e)}")
```

- [ ] **Step 4: 跑綠**：`pytest tests/test_helm.py tests/test_api.py -k quota -q` → PASS。
- [ ] **Step 5: commit**：`feat: #278 A1 operator PUT /api/namespaces/{ns}/quota（整組 hard overwrite）`

---

## Task 3: operator — agent ns ResourceQuota + LimitRange（#278 B1+B2）

**Files:**
- Modify: `extra/paas-operator/src/api/agent_runtimes.py`（常數 + _ensure_namespace）
- Test: `extra/paas-operator/tests/test_agent_runtimes.py`

**Interfaces:**
- Consumes: `k8s_service.apply_manifest(dict)`（helm.py:1348，冪等、內建 validate_namespace）。
- Produces: 模組常數 `AGENT_QUOTA_CPU/MEMORY/PVC/STORAGE`。

- [ ] **Step 1: 失敗測試**（patch apply_manifest，assert 三次 apply：ns → quota → limitrange）

```python
@patch("src.api.agent_runtimes.k8s_service")
def test_ensure_namespace_applies_quota_and_limitrange(self, mock_k8s):
    from src.api.agent_runtimes import _ensure_namespace
    _ensure_namespace("paas-ws-pa-abc123")
    kinds = [c.args[0]["kind"] for c in mock_k8s.apply_manifest.call_args_list]
    assert kinds == ["Namespace", "ResourceQuota", "LimitRange"]
    quota = mock_k8s.apply_manifest.call_args_list[1].args[0]
    assert quota["metadata"]["name"] == "paas-ws-pa-abc123-quota"
    assert quota["spec"]["hard"]["limits.cpu"] == "3"
    assert quota["spec"]["hard"]["limits.memory"] == "3Gi"
    lr = mock_k8s.apply_manifest.call_args_list[2].args[0]
    assert lr["kind"] == "LimitRange"
    assert lr["spec"]["limits"][0]["default"] == {"cpu": "500m", "memory": "512Mi"}
```

- [ ] **Step 2: 跑紅**：`pytest tests/test_agent_runtimes.py -k quota_and_limitrange -q` → FAIL。

- [ ] **Step 3: 實作**

agent_runtimes.py 模組層（緊接 `_POD_WS_PORT` :50 之後）：
```python
# #278 B1：agent per-project ns quota（值 > 單 pod sidecar 2CPU/2Gi 留 headroom；
# agent 一 project 一 pod、非 sum）。attach 不重跑 _ensure_namespace → 改值不自動
# re-apply 既有 ns，需 teardown 重冷啟（與 label MERGE-only 同「冷啟才收斂」限制）。
AGENT_QUOTA_CPU = "3"
AGENT_QUOTA_MEMORY = "3Gi"
AGENT_QUOTA_PVC = "3"
AGENT_QUOTA_STORAGE = "10Gi"
```

`_ensure_namespace` 貼完 Namespace 後（apply_manifest ns 之後）依序加：
```python
    k8s_service.apply_manifest({
        "apiVersion": "v1", "kind": "ResourceQuota",
        "metadata": {"name": f"{namespace}-quota", "namespace": namespace,
                     "labels": {"paas-managed": "true"}},
        "spec": {"hard": {
            "requests.cpu": AGENT_QUOTA_CPU, "requests.memory": AGENT_QUOTA_MEMORY,
            "limits.cpu": AGENT_QUOTA_CPU, "limits.memory": AGENT_QUOTA_MEMORY,
            "persistentvolumeclaims": AGENT_QUOTA_PVC,
            "requests.storage": AGENT_QUOTA_STORAGE,
        }},
    })
    # #278 B2（必要配套）：quota 逼出 limits → LimitRange 補未宣告 limits 的容器
    k8s_service.apply_manifest({
        "apiVersion": "v1", "kind": "LimitRange",
        "metadata": {"name": f"{namespace}-limits", "namespace": namespace,
                     "labels": {"paas-managed": "true"}},
        "spec": {"limits": [{"type": "Container",
                             "default": {"cpu": "500m", "memory": "512Mi"},
                             "defaultRequest": {"cpu": "500m", "memory": "512Mi"}}]},
    })
```

- [ ] **Step 4: 跑綠** + 既有 agent-runtime 測試不回歸：`pytest tests/test_agent_runtimes.py -q` → PASS。
- [ ] **Step 5: commit**：`feat: #278 B1+B2 agent ns ResourceQuota + LimitRange（堵零-quota 逃逸口）`

---

## Task 4: Odoo — CS quota drift 權威重算（#278 A1 Odoo 側）

**Files:**
- Modify: `src/models/cloud_service.py`（抽 `_compute_ns_quota` + 刪除路徑重算）
- Modify: `src/services/paas_operator.py`（+`update_namespace_quota` client 方法）
- Test: `src/tests/test_cloud_service.py`

**Interfaces:**
- Consumes: Task 2 的 `PUT /api/namespaces/{ns}/quota`。
- Produces: `CloudService._compute_ns_quota(workspace) -> (cpu, mem, storage)`；`PaaSOperatorClient.update_namespace_quota(namespace, cpu_limit, memory_limit, storage_limit)`。

- [ ] **Step 1: 失敗測試**（2 service 同 workspace，刪一個，assert 以存活集重算並 PUT）

fixture：serviceA + serviceB 同 workspace（同 helm_namespace），各 `allocated_vcpu=10, allocated_ram_gb=10, allocated_storage_gb=50`（值刻意大於 floor，讓「存活集」與「全部」算出不同值以資鑑別）。

```python
def test_delete_recomputes_quota_over_survivors(self):
    with patch('odoo.addons.woow_paas_platform.services.paas_operator.'
               'get_paas_operator_client', return_value=MagicMock()) as gm:
        client = gm.return_value
        client.uninstall_release.return_value = True
        ns = self.serviceA.helm_namespace  # unlink 後就取不到，先捕獲
        self.serviceA.unlink()
        # 存活集只剩 serviceB（vcpu=10, ram=10, storage=50）→
        #   cpu=str(max(10*3,8))="30"、mem=f"{max(int(10*3),8)}Gi"="30Gi"、storage=f"{max(50*3,100)}Gi"="150Gi"
        #   （若誤把已刪的 A 算進去 → "60"/"60Gi"/"300Gi"，測試就會抓到）
        client.update_namespace_quota.assert_called_once_with(ns, "30", "30Gi", "150Gi")
```
重點：**A 被刪後不計入 sum**。`assert_called_once_with` 精確釘 survivor-only 語意。

- [ ] **Step 2: 跑紅**：sandbox 跑 `test_cloud_service.py::...::test_delete_recomputes_quota_over_survivors` → FAIL（無 update_namespace_quota / 刪除路徑不重算）。

- [ ] **Step 3: 實作**

paas_operator.py（PaaSOperatorClient 新方法，mirror create_namespace）：
```python
def update_namespace_quota(self, namespace, cpu_limit, memory_limit, storage_limit):
    """#278 A1：PUT 整組 quota（值 = 存活 service 集重算）。"""
    return self._request("PUT", f"/api/namespaces/{namespace}/quota",
                         data={"cpu_limit": cpu_limit, "memory_limit": memory_limit,
                               "storage_limit": storage_limit},
                         timeout=HELM_OPERATION_TIMEOUT)
```

cloud_service.py：抽 `_compute_ns_quota`（從 `_execute_deploy` 434-448 搬邏輯，改吃 workspace），`_execute_deploy` 改呼叫它；刪除路徑收尾重算：
```python
def _compute_ns_quota(self, workspace):
    """#278 A1 權威值：以 workspace 內『非 deleting』存活 service 加總算 quota。
    domain 已排除 state='deleting' → 呼叫時機在 state 翻 deleting / super().unlink() 之後
    即天然反映刪除後存活集。"""
    Svc = self.env['woow_paas_platform.cloud_service'].sudo()
    live = Svc.search([('workspace_id', '=', workspace.id), ('state', '!=', 'deleting')])
    vcpu = sum(live.mapped('allocated_vcpu'))
    ram = sum(live.mapped('allocated_ram_gb'))
    storage = sum(live.mapped('allocated_storage_gb'))
    return (str(max(vcpu * 3, 8)), f"{max(int(ram * 3), 8)}Gi", f"{max(storage * 3, 100)}Gi")

def _push_ns_quota(self, workspace, helm_namespace):
    """best-effort：重算並 PUT；operator 未設定 / 失敗只 log，不 abort 刪除。"""
    from ..services.paas_operator import get_paas_operator_client
    client = get_paas_operator_client(self.env)
    if not client:
        return
    cpu, mem, storage = self._compute_ns_quota(workspace)
    try:
        client.update_namespace_quota(helm_namespace, cpu, mem, storage)
    except Exception as e:
        _logger.warning("quota recompute PUT failed for %s: %s", helm_namespace, e)
```

- `_do_cleanup`：在 `self.write({'state': 'deleting'})`（現 :1112）**之後**，`self._push_ns_quota(self.workspace_id, self.helm_namespace)`（此時本 record 已 state=deleting，被 domain 排除）。
- `unlink`：**pre-delete 捕獲** `[(s.workspace_id, s.helm_namespace) for s in self]`（比照既有 `affected`/`old` 快照），`super().unlink()` 之後對每個 (workspace, ns) 呼叫 `self._push_ns_quota(...)`（record 已離場）。

- [ ] **Step 4: 跑綠**（sandbox）：`test_cloud_service.py -k quota` → PASS。⚠️ Odoo test 走 k8s sandbox（避 amd64 模擬）。
- [ ] **Step 5: commit**：`fix: #278 A1 CS ns quota drift 權威重算（刪除路徑以存活集覆寫）`

---

## 驗證 / 收尾

- operator：`cd extra/paas-operator && pytest tests/ -q`（原生，全綠不回歸）；有 cluster 時 `HELM_BINARY=$(which helm) pytest -m integration tests/integration/ -v` 驗真 apply + admission。
- Odoo：sandbox 跑 `TestCloudService` quota 系列。
- `__manifest__.py` version bump（.45 → .46）。
- 對抗式 code review（workflow）→ PR-278 → develop。
- **注意**：operator 改動需 rebuild operator image + Fleet pin 才在 stg/prod 生效（build-operator / deploy，非本 PR）。

## 非目標（→ PR-277 / follow-up）

owner/TTL label + dry-run orphan reconciler（PR-277）；訂閱規格單一來源 quota；reconciler auto-delete graduate。
