---
title: PR-277 孤兒 NS Reconciler Implementation Plan
category: plans
status: done
created: 2026-07-07
updated: 2026-07-19
source: docs/superpowers/plans/2026-07-07-pr277-orphan-ns-reconciler.md
promoted-at: 2026-07-19
---

# PR-277 孤兒 NS Reconciler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（本輪走 ultracode workflow orchestration）。Steps use `- [ ]`.

**Goal:** 延伸既有 L4 reverse-orphan reconciler，加入 k8s namespace 為一類 dry-run fact——operator 唯讀列 `paas-ws-*` ns 當右集，Odoo 對 live helm_namespace 左集差集出「cluster 有、Odoo 無」的孤兒 ns，只報告不刪；並在建 ns 時 stamp `woow.tw/owner` label（孤兒失去 Odoo record 後唯一的擁有者線索）。

**Architecture:** 三處 code 改動 + 一處運維動作。operator：`build_managed_ns_labels` 加 owner + 新增唯讀 `list_managed_namespaces` + reconcile.py inventory 加 ns leg；Odoo：reconciler.py 左集 union CS∪agent ns + kind 對映 + ns env 分類。spec：`docs/plans/2026-Q3/2026-07-07-w1-ns-quota-governance-design.md`（peer-reviewed）。

**Tech Stack:** FastAPI + kubectl（operator，pytest via repo `.venv`）；Odoo 18 `src/services/reconciler.py`（TransactionCase，走 sandbox）。

## Global Constraints

- **operator pytest 必用 repo `.venv/bin/python`**（系統 py3.9 因 config.py `str|None` collection 期炸）。
- **Odoo test 走 woow-k3s sandbox cp-overlay**（本機 arm64 兩道牆；recipe 見 memory）。
- **created-at 不自訂**：用 k8s 原生 immutable `metadata.creationTimestamp`（RFC3339 冒號是非法 label value + 會被 re-stamp clobber → 自訂 label 是錯的）。Part A 只 stamp `woow.tw/owner`。
- **owner 來源**：CS ns = `workspace_id`（create_namespace 有）；agent ns = **project**（`_ensure_namespace` 需新增 owner 參數，從 spawn_or_attach 的 `request.project_id` 傳入——agent ns 是 per-project、無單一 workspace）。
- **operator + Odoo 必須同批上**：operator 開始回 `kind=namespace` 但 Odoo `_KIND_TO_LEFTSET_ATTR` 沒對映 → compute_orphans 把該 kind skip + 標 partial（靜默失效）。反之 Odoo 有對映但 operator 沒回 ns → 永遠報不出。
- **左集必須 union CS ∪ agent ns**：cluster `paas-ws-*` = CS 共用 `paas-ws-{hash}`（`cloud.service.helm_namespace`）**＋** agent per-project `paas-ws-pa-{hash}`（`agent_session.helm_namespace`）。**漏 agent ns = 把所有活躍 agent ns 誤判孤兒（災難）**。SmartHome/SecurityAccess 無 ns。
- 左集 domain 排除 `state='deleting'`（正在刪的過渡態，否則誤判即將消失的 ns 為活躍）；helm_namespace 非唯一（多 CS 共用）→ 入 set 去重。
- **reconcilable 恆 False**：ns 不進 `_RECONCILABLE_ELIGIBLE_KINDS`（空集）→ dry-run report-only、非破壞。誤判成本 = 一列多餘報表。
- **ns 列舉走 helm.py 唯讀 service 方法**（`list_managed_namespaces`），**絕不**在 reconcile.py inline subprocess/kubectl（`test_reconcile_delete_gate.py` 禁 subprocess/破壞動詞）。方法名勿含 delete 字樣。
- **RBAC 已驗證**：`paas-operator`/`paas-operator-stg` ClusterRole 已有 `namespaces:[get,list,...]`（2026-07-07 實查 woow-k3s）→ ns leg 無 RBAC blocker。
- ns fact 語意：id = ns 名本身（差集鍵，非 UUID）→ `CfResource(id=ns_name, name=ns_name, kind=OrphanKind.NAMESPACE)`，**不改 CfResource schema**（避免 golden test churn）。

---

## Task 1: operator — ns owner label stamp（Part A）

**Files:**
- Modify: `extra/paas-operator/src/services/helm.py`（build_managed_ns_labels + create_namespace）
- Modify: `extra/paas-operator/src/api/agent_runtimes.py`（_ensure_namespace + caller）
- Test: `extra/paas-operator/tests/test_helm.py` + `tests/test_agent_runtimes.py`

**Interfaces:**
- Produces: `build_managed_ns_labels(workspace_id=None, reach_workspace_ids=None, owner=None)`；`OWNER_LABEL = "woow.tw/owner"`。

- [ ] **Step 1: 失敗測試**

test_helm.py（`k8s_service` = TestKubernetesService fixture；`build_managed_ns_labels` 是 KubernetesService 用的模組級函式）：
```python
def test_build_managed_ns_labels_stamps_owner(self):
    from src.services.helm import build_managed_ns_labels, OWNER_LABEL
    labels = build_managed_ns_labels(workspace_id=7, owner="7")
    assert labels[OWNER_LABEL] == "7"

@patch("src.services.helm.build_managed_ns_labels", wraps=build_managed_ns_labels)
@patch("subprocess.run")
def test_create_namespace_passes_owner(self, mock_run, mock_labels, k8s_service):
    mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
    k8s_service.create_namespace("paas-ws-abc", "8", "8Gi", "100Gi", workspace_id=7)
    # 建 ns 那次 build_managed_ns_labels 帶 owner="7"
    assert any(c.kwargs.get("owner") == "7" for c in mock_labels.call_args_list)
```

test_agent_runtimes.py：
```python
@patch("src.api.agent_runtimes.k8s_service")
def test_ensure_namespace_stamps_project_owner(self, mock_k8s):
    from src.api.agent_runtimes import _ensure_namespace
    _ensure_namespace("paas-ws-pa-abc", reach_workspace_ids=[1], owner="proj-42")
    ns_manifest = mock_k8s.apply_manifest.call_args_list[0].args[0]
    assert ns_manifest["metadata"]["labels"]["woow.tw/owner"] == "proj-42"
```

- [ ] **Step 2: 跑紅**（`.venv/bin/python -m pytest tests/test_helm.py tests/test_agent_runtimes.py -k owner -q`）→ FAIL。

- [ ] **Step 3: 實作**

helm.py：`OWNER_LABEL` 常數（WORKSPACE_ID_LABEL 附近）+ `build_managed_ns_labels` 加 owner 參數：
```python
OWNER_LABEL = "woow.tw/owner"  # #277：孤兒 ns 失去 Odoo record 後唯一的擁有者線索

def build_managed_ns_labels(
    workspace_id: Optional[int] = None,
    reach_workspace_ids: Optional[List[int]] = None,
    owner: Optional[str] = None,
) -> Dict[str, str]:
    labels = {"paas-managed": "true"}
    if settings.paas_env:
        validate_label_value("paas.woowtech.io/env", settings.paas_env)
        labels["paas.woowtech.io/env"] = settings.paas_env
    if workspace_id is not None:
        value = str(int(workspace_id))
        validate_label_value(WORKSPACE_ID_LABEL, value)
        labels[WORKSPACE_ID_LABEL] = value
    for wid in reach_workspace_ids or ():
        labels[reach_ws_label_key(wid)] = REACH_WS_LABEL_VALUE
    if owner is not None:
        validate_label_value(OWNER_LABEL, owner)   # owner 須為合法 label value（int-as-str 或 slug，無冒號）
        labels[OWNER_LABEL] = owner
    return labels
```

create_namespace（helm.py:881 label 呼叫）改傳 owner：
```python
            "labels": build_managed_ns_labels(
                workspace_id=workspace_id,
                owner=str(workspace_id) if workspace_id is not None else None,
            ),
```

agent_runtimes.py：`_ensure_namespace` 加 owner 參數 + caller 傳入：
```python
def _ensure_namespace(
    namespace: str,
    reach_workspace_ids: Optional[List[int]] = None,
    owner: Optional[str] = None,
) -> None:
    k8s_service.apply_manifest({
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": build_managed_ns_labels(
                reach_workspace_ids=reach_workspace_ids, owner=owner
            ),
        },
    })
```
caller（agent_runtimes.py:395，cold-start）——owner 用 project 身分（label value 安全字串，`p{id}` 避免純數字/冒號疑慮）：
```python
        _ensure_namespace(
            namespace,
            reach_workspace_ids=request.project_workspace_ids,
            owner="p%d" % request.project_id,
        )
```

> **created-at 不 stamp**：用 k8s 原生 `metadata.creationTimestamp`（Task 2 的 list-ns 會一併帶回，供報表/未來 TTL）。`stamp_managed_ns_labels`（re-stamp 路徑）自動繼承 owner（stable、safe on overwrite），無需另改。

- [ ] **Step 4: 跑綠**（`-k owner` + 兩檔全跑不回歸）→ PASS。
- [ ] **Step 5: commit**：`feat: #277 ns owner label stamp（build_managed_ns_labels + CS/agent caller）`

---

## Task 2: operator — list_managed_namespaces + inventory ns leg（Part B operator）

**Files:**
- Modify: `extra/paas-operator/src/services/helm.py`（+list_managed_namespaces 唯讀）
- Modify: `extra/paas-operator/src/api/reconcile.py`（get_inventory 加 ns leg）
- Test: `extra/paas-operator/tests/test_helm.py` + `tests/test_reconcile_inventory.py`

**Interfaces:**
- Produces: `KubernetesService.list_managed_namespaces() -> list[str]`（回 `paas-ws-*` + `paas-managed=true` 的 ns 名）。
- Consumes: `OrphanKind.NAMESPACE`（schemas.py 已存在）、`CfResource`。

- [ ] **Step 1: 失敗測試**

test_helm.py（mock subprocess，assert 解析 ns 名）：
```python
@patch("subprocess.run")
def test_list_managed_namespaces_parses_names(self, mock_run, helm_service):
    mock_run.return_value = Mock(returncode=0, stderr="", stdout=json.dumps({
        "items": [
            {"metadata": {"name": "paas-ws-abc", "labels": {"paas-managed": "true"}}},
            {"metadata": {"name": "paas-ws-pa-def", "labels": {"paas-managed": "true"}}},
        ]
    }))
    names = helm_service.list_managed_namespaces()
    assert set(names) == {"paas-ws-abc", "paas-ws-pa-def"}
```

test_reconcile_inventory.py（ns leg 進 inventory；mock k8s + cloudflare。`client` = 既有 TestClient fixture；env-pin 需 PAAS_ENV∈{prod,stg}——沿用既有測試設定 env 的方式）：
```python
def test_inventory_includes_namespace_facts(self, client, monkeypatch):
    import src.api.reconcile as rec
    monkeypatch.setattr(rec.k8s_service, "list_managed_namespaces",
                        lambda: ["paas-ws-abc", "paas-ws-pa-def"])
    # CF list-all 全回空（沿用既有測試 mock cloudflare_service 的手法）
    monkeypatch.setattr(rec.cloudflare_service, "list_all_tunnels", _async_return([]))
    monkeypatch.setattr(rec.cloudflare_service, "list_all_access_apps", _async_return([]))
    monkeypatch.setattr(rec.cloudflare_service, "list_all_reusable_policies", _async_return([]))
    r = client.get("/api/reconcile/inventory")
    assert r.status_code == 200
    ns_facts = [x for x in r.json()["resources"] if x["kind"] == "namespace"]
    assert {x["id"] for x in ns_facts} == {"paas-ws-abc", "paas-ws-pa-def"}
    assert r.json()["partial"] is False
```
（`_async_return(v)` = 回一個 `async def` 傳回 v 的 helper；若既有測試檔已有等價 async-mock helper 直接複用。）

- [ ] **Step 2: 跑紅**（`.venv/bin/python -m pytest tests/test_helm.py tests/test_reconcile_inventory.py -k "managed_namespaces or namespace_facts" -q`）→ FAIL。

- [ ] **Step 3: 實作**

helm.py（KubernetesService，唯讀 list，仿 list_pvcs 的 selector + get_namespace_labels 的 `-o json` 解析）：
```python
def list_managed_namespaces(self) -> List[str]:
    """#277 唯讀反向發現：列所有帶 paas-managed=true 的 ns 名。
    不呼 validate_namespace（反向發現全部、無單一輸入 ns 可驗），邊界靠 label selector。"""
    result = self._run_command([
        "get", "ns", "-l", "paas-managed=true", "-o", "json",
    ])
    data = json.loads(result.stdout)
    return [
        item["metadata"]["name"]
        for item in data.get("items", [])
        if item.get("metadata", {}).get("name")
    ]
```

reconcile.py：模組頂部 `import asyncio` + `from src.services.helm import KubernetesService` + `k8s_service = KubernetesService()`；在 `get_inventory` 的 CF loop **之後**加獨立 ns leg（sync k8s 用 to_thread 併入 async、同一 completeness-gate 語意）：
```python
    # #277 ns leg：獨立於 CF 迴圈（k8s 同步 subprocess vs CF async），同 completeness-gate。
    try:
        ns_names = await asyncio.to_thread(k8s_service.list_managed_namespaces)
        resources.extend(
            CfResource(id=n, name=n, kind=OrphanKind.NAMESPACE) for n in ns_names
        )
    except Exception as exc:  # noqa: BLE001 —— 與 CF 類同等 fail-SAFE、可觀測
        logger.warning(
            "reconcile inventory: namespace list-all 失敗（%s）→ 標 partial",
            exc, exc_info=True,
        )
        failed_kinds.append(OrphanKind.NAMESPACE.value)
```
（`partial = bool(failed_kinds)` 已在函式尾自動涵蓋 ns leg。）

- [ ] **Step 4: 跑綠** + golden/schema 測試不回歸（`tests/test_reconcile_schema_golden.py`、`tests/test_reconcile_inventory_schema.py`——CfResource 未改 schema 故應綠）→ PASS。
- [ ] **Step 5: commit**：`feat: #277 operator inventory ns leg（list_managed_namespaces + get_inventory）`

---

## Task 3: Odoo — reconciler ns 左集 + 差集 + env 分類（Part B Odoo）

**Files:**
- Modify: `src/services/reconciler.py`（LeftSet + collect_left_set + _KIND_TO_LEFTSET_ATTR + ns env 分類）
- Test: `src/tests/test_reconciler_diff.py`（或既有 reconciler 測試檔）

**Interfaces:**
- Consumes: operator inventory 的 `kind='namespace'` CfResource（Task 2）。
- Produces: `LeftSet.namespace_names`；`_KIND_TO_LEFTSET_ATTR['namespace'] = 'namespace_names'`。

- [ ] **Step 1: 失敗測試**（cluster 有兩 ns，Odoo 左集只有一個 → 另一個成孤兒；活躍 ns 不成孤兒）

```python
def test_orphan_namespace_not_in_left_set(self):
    from odoo.addons.woow_paas_platform.services.reconciler import (
        LeftSet, compute_orphans,
    )
    from odoo.addons.woow_paas_platform.services import reconciler as R
    left = LeftSet()
    left.namespace_names = {"paas-ws-live"}   # Odoo 只認得這個
    # operator 右集回兩個 ns
    resources = [
        {"id": "paas-ws-live", "name": "paas-ws-live", "kind": "namespace"},
        {"id": "paas-ws-orphan", "name": "paas-ws-orphan", "kind": "namespace"},
    ]
    result = compute_orphans(resources, left)
    orphans = {c.id for c in result.orphan_candidates}
    assert orphans == {"paas-ws-orphan"}   # live 不報、orphan 報

def test_collect_left_set_unions_cs_and_agent_ns(self):
    # serviceA.helm_namespace='paas-ws-w'；agent_session.helm_namespace='paas-ws-pa-x'
    # 兩者都要進 namespace_names（漏 agent ns = 災難）；state='deleting' 的排除
    from odoo.addons.woow_paas_platform.services.reconciler import collect_left_set
    ls = collect_left_set(self.env)
    assert "paas-ws-w" in ls.namespace_names
    assert "paas-ws-pa-x" in ls.namespace_names
```

- [ ] **Step 2: 跑紅**（sandbox）→ FAIL（`namespace_names` 不存在 / 未 union / kind 未對映）。

- [ ] **Step 3: 實作**（reconciler.py）

`LeftSet`（:38-42）加集屬性：
```python
    namespace_names: set = field(default_factory=set)  # #277：CS ∪ agent 的 live ns 名
```

`collect_left_set`（:90 `return ls` 前）加 ns 左集 union（**CS ∪ agent，排除 deleting，去重**）：
```python
    # ── ns（#277）：cluster paas-ws-* 的 live 白名單 = CS 共用 ns ∪ agent per-project ns ──
    # 漏 agent ns 會把所有活躍 agent ns 誤判孤兒（災難）；state='deleting' 排除（過渡態）。
    cs_ns = env['woow_paas_platform.cloud_service'].sudo().search(
        [('helm_namespace', '!=', False), ('state', '!=', 'deleting')]
    )
    ls.namespace_names |= _clean(cs_ns.mapped('helm_namespace'))
    ag_ns = env['woow_paas_platform.agent_session'].sudo().search(
        [('helm_namespace', '!=', False)]
    )
    ls.namespace_names |= _clean(ag_ns.mapped('helm_namespace'))
```

`_KIND_TO_LEFTSET_ATTR`（:122）加對映：
```python
    'namespace': 'namespace_names',   # #277
```

ns env 分類（classify 階段；classify_env_hint/parse_paas_name 不認 ns 名 → 另寫，鏡像 operator `_env_hint_from_prefix`）：
```python
def _ns_env_hint(name: str) -> str:
    """#277 ns 專用 env 歸屬（classify_env_hint 的 paas-{env}-{sa|sm|cs} 格式不認 paas-ws-* ns）。"""
    if name.startswith('paas-ws-test') or name.startswith('paas-ws-dev'):
        return 'dev'
    if name.startswith('paas-ws-stg-'):
        return 'stg'
    if name.startswith('paas-ws-'):
        return 'prod'
    return 'unknown'
```
在 classify_candidates 對 `candidate.kind == 'namespace'` 用 `_ns_env_hint(candidate.name)` 覆寫 env_hint（其餘 kind 走原 classify_env_hint）。reconcilable 仍恆 False（ns 不在 `_RECONCILABLE_ELIGIBLE_KINDS`）。

- [ ] **Step 4: 跑綠**（sandbox，cp-overlay recipe 跑 `TestReconciler*` 相關）→ PASS。
- [ ] **Step 5: commit**：`feat: #277 Odoo reconciler ns 左集差集（CS∪agent union + kind 對映 + ns env 分類）`

---

## 驗證 / 收尾

- **operator**：`.venv/bin/python -m pytest tests/ -q`（全綠不回歸；重點 golden/schema/delete-gate 測試）。有 cluster 時 integration 驗真 list-ns。
- **Odoo**：woow-k3s sandbox cp-overlay 跑 reconciler 測試（recipe 見 memory `reference_odoo_test_amd64_woowk3s_cp_overlay`）。**端到端** sanity：sandbox 內觸發 `_cron_reconcile_report()` → 確認 ns 孤兒進報表、活躍 ns 不進。
- `__manifest__.py` version bump（.46 → .47）。
- 對抗式 code review（workflow）→ PR-277 → develop。
- **運維（非 code，merge 後）**：一次性清既有孤兒空 ns `paas-ws-stg-ad023482`（判準：`kubectl get all -n <ns>` 空 **且** 無 Odoo helm_namespace record；即上線後首份 dry-run 報表的第一批人工處置對象）。
- **注意**：operator 改動需 rebuild image + Fleet pin 才在 stg/prod 生效（非本 PR）。operator+Odoo 兩側須同版上線（否則 kind=namespace 靜默 skip/partial）。

## 非目標（follow-up）

- reconciler graduate 成 auto-delete（reconcilable=True，F-ENV-2 graduation，另開）。
- ns TTL 自動回收（用原生 creationTimestamp + 政策）。
- ns fact 帶 labels/owner 回報表（本版報表顯示 ns 名 + env_hint 足夠；owner 由人工 `kubectl get ns -o yaml` 查 Task 1 stamp 的 label）。
