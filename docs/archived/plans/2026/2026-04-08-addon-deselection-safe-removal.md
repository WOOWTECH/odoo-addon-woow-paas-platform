---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-08-addon-deselection-safe-removal.md
archived: 2026-07-19
archived-reason: "已落地；addon 安全移除現況在 src 與 reference/features/cloud-services.md"
---

# Addon Deselection Safe Removal — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Safely handle addon deselection by checking Odoo module states before removal, preventing data loss from removing installed modules.

**Architecture:** Pre-check flow — frontend queries module states before saving, branches to direct removal (uninstalled) or confirmation dialog (installed). Force-uninstall runs `button_immediate_uninstall()` in-pod via operator before helm upgrade removes files. Dependency conflicts block deselection entirely.

**Tech Stack:** FastAPI (operator), Python/Odoo ORM (backend), OWL 2 (frontend)

---

## Task 1: OdooModuleService — Operator Service Layer

**Files:**
- Create: `extra/paas-operator/src/services/odoo_module.py`
- Modify: `extra/paas-operator/src/models/schemas.py`

**Step 1: Add Pydantic schemas**

In `extra/paas-operator/src/models/schemas.py`, append:

```python
# --- Odoo Module Management ---

class OdooModuleStatesRequest(BaseModel):
    """Request to query Odoo module installation states."""
    module_names: List[str] = Field(..., description="List of module technical names to check")

class OdooModuleStateInfo(BaseModel):
    """Installation state and reverse dependencies for a single module."""
    state: str = Field(..., description="Module state: installed, uninstalled, uninstallable, etc.")
    dependents: List[str] = Field(default_factory=list, description="Installed modules that depend on this one")

class OdooModuleStatesResponse(BaseModel):
    """Response with module states."""
    success: bool
    modules: Dict[str, OdooModuleStateInfo] = Field(default_factory=dict)
    error: Optional[str] = None

class OdooUninstallModulesRequest(BaseModel):
    """Request to uninstall Odoo modules."""
    module_names: List[str] = Field(..., description="Module technical names to uninstall")

class OdooUninstallModulesResponse(BaseModel):
    """Response from module uninstallation."""
    success: bool
    uninstalled: List[str] = Field(default_factory=list)
    error: Optional[str] = None
```

**Step 2: Create OdooModuleService**

Create `extra/paas-operator/src/services/odoo_module.py`:

```python
"""Odoo module state query and uninstall service.

Provides two operations via kubectl exec into Odoo containers:
- get_module_states: Query ir.module.module for installation state + reverse deps
- uninstall_modules: Run button_immediate_uninstall() to properly remove modules
"""

import json
import logging

from src.services.helm import KubectlException, KubernetesService

logger = logging.getLogger(__name__)

MODULE_QUERY_TIMEOUT = 30  # seconds
MODULE_UNINSTALL_TIMEOUT = 120  # seconds


class OdooModuleError(Exception):
    """Raised when Odoo module operations fail."""

    def __init__(self, message: str, step: str = ""):
        self.step = step
        super().__init__(message)


class OdooModuleService:
    """Handles Odoo module state queries and uninstallation."""

    def __init__(self, k8s_service: KubernetesService | None = None):
        self.k8s = k8s_service or KubernetesService()

    def _get_odoo_pod(self, namespace: str, release_name: str) -> tuple[str, str]:
        """Find Odoo pod name and container name.

        Returns:
            Tuple of (pod_name, container_name)
        """
        selectors = [
            f"app.kubernetes.io/instance={release_name},app.kubernetes.io/name=odoo",
            f"app.kubernetes.io/instance={release_name}",
        ]
        for selector in selectors:
            try:
                pod_name = self.k8s.get_pod_name(namespace, selector)
                return pod_name, f"{release_name}-odoo"
            except KubectlException:
                continue

        raise OdooModuleError(
            f"Cannot find Odoo pod for release {release_name} in {namespace}",
            step="find_pod",
        )

    def get_module_states(
        self, namespace: str, release_name: str, module_names: list[str]
    ) -> dict:
        """Query module installation states and reverse dependencies.

        Args:
            namespace: K8s namespace
            release_name: Helm release name
            module_names: List of module technical names

        Returns:
            Dict mapping module_name -> {state, dependents}
        """
        pod_name, container = self._get_odoo_pod(namespace, release_name)

        names_json = json.dumps(module_names)
        query_script = (
            "import odoo, json, sys\n"
            "try:\n"
            "    odoo.tools.config.parse_config("
            "['-c', '/opt/bitnami/odoo/conf/odoo.conf', '--no-http'])\n"
            "    db_name = odoo.tools.config['db_name']\n"
            "    registry = odoo.registry(db_name)\n"
            "    with registry.cursor() as cr:\n"
            "        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})\n"
            "        Module = env['ir.module.module']\n"
            "        Dep = env['ir.module.module.dependency']\n"
            f"        names = {names_json}\n"
            "        result = {}\n"
            "        for name in names:\n"
            "            mod = Module.search([('name', '=', name)], limit=1)\n"
            "            if not mod:\n"
            "                result[name] = {'state': 'not_found', 'dependents': []}\n"
            "                continue\n"
            "            # Find installed modules that depend on this one\n"
            "            deps = Dep.search([('name', '=', name)])\n"
            "            dependents = []\n"
            "            for dep in deps:\n"
            "                parent = dep.module_id\n"
            "                if parent.state == 'installed' and parent.name not in names:\n"
            "                    dependents.append(parent.name)\n"
            "            result[name] = {'state': mod.state, 'dependents': dependents}\n"
            "        print(json.dumps({'success': True, 'modules': result}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'success': False, 'error': str(e)}))\n"
            "    sys.exit(1)\n"
        )

        command = ["/opt/bitnami/odoo/venv/bin/python3", "-c", query_script]

        try:
            result = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=container,
                command=command,
                timeout=MODULE_QUERY_TIMEOUT,
            )
        except KubectlException as e:
            raise OdooModuleError(
                f"Failed to query module states: {e}", step="query_states"
            ) from e

        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        json_line = lines[-1] if lines else ""

        try:
            data = json.loads(json_line)
        except json.JSONDecodeError as e:
            raise OdooModuleError(
                f"Invalid JSON from module state query: {json_line[:200]}",
                step="query_states",
            ) from e

        if not data.get("success"):
            raise OdooModuleError(
                f"Module state query failed: {data.get('error', 'unknown')}",
                step="query_states",
            )

        return data["modules"]

    def uninstall_modules(
        self, namespace: str, release_name: str, module_names: list[str]
    ) -> list[str]:
        """Uninstall modules in the target Odoo instance.

        Calls button_immediate_uninstall() which properly cleans up
        database artifacts (views, menus, data, columns).

        Args:
            namespace: K8s namespace
            release_name: Helm release name
            module_names: Module technical names to uninstall

        Returns:
            List of successfully uninstalled module names

        Raises:
            OdooModuleError: On dependency conflict or execution failure
        """
        pod_name, container = self._get_odoo_pod(namespace, release_name)

        names_json = json.dumps(module_names)
        uninstall_script = (
            "import odoo, json, sys\n"
            "try:\n"
            "    odoo.tools.config.parse_config("
            "['-c', '/opt/bitnami/odoo/conf/odoo.conf', '--no-http'])\n"
            "    db_name = odoo.tools.config['db_name']\n"
            "    registry = odoo.registry(db_name)\n"
            "    with registry.cursor() as cr:\n"
            "        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})\n"
            "        Module = env['ir.module.module']\n"
            f"        names = {names_json}\n"
            "        uninstalled = []\n"
            "        for name in names:\n"
            "            mod = Module.search([('name', '=', name), ('state', '=', 'installed')], limit=1)\n"
            "            if not mod:\n"
            "                continue\n"
            "            try:\n"
            "                mod.button_immediate_uninstall()\n"
            "                uninstalled.append(name)\n"
            "            except Exception as e:\n"
            "                cr.rollback()\n"
            "                print(json.dumps({'success': False, "
            "'error': f'Cannot uninstall {name}: {e}'}))\n"
            "                sys.exit(1)\n"
            "        cr.commit()\n"
            "        print(json.dumps({'success': True, 'uninstalled': uninstalled}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'success': False, 'error': str(e)}))\n"
            "    sys.exit(1)\n"
        )

        command = ["/opt/bitnami/odoo/venv/bin/python3", "-c", uninstall_script]

        try:
            result = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=container,
                command=command,
                timeout=MODULE_UNINSTALL_TIMEOUT,
            )
        except KubectlException as e:
            raise OdooModuleError(
                f"Failed to uninstall modules: {e}", step="uninstall"
            ) from e

        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        json_line = lines[-1] if lines else ""

        try:
            data = json.loads(json_line)
        except json.JSONDecodeError as e:
            raise OdooModuleError(
                f"Invalid JSON from uninstall: {json_line[:200]}",
                step="uninstall",
            ) from e

        if not data.get("success"):
            raise OdooModuleError(
                data.get("error", "Unknown uninstall error"),
                step="uninstall",
            )

        logger.info("Uninstalled modules: %s", data["uninstalled"])
        return data["uninstalled"]
```

**Step 3: Commit**

```bash
git add extra/paas-operator/src/services/odoo_module.py extra/paas-operator/src/models/schemas.py
git commit -m "feat: add OdooModuleService for module state query and uninstall"
```

---

## Task 2: Operator API Endpoints

**Files:**
- Create: `extra/paas-operator/src/api/odoo_modules.py`
- Modify: `extra/paas-operator/src/main.py:10,153`

**Step 1: Create API router**

Create `extra/paas-operator/src/api/odoo_modules.py`:

```python
"""API endpoints for Odoo module management."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import (
    OdooModuleStatesRequest,
    OdooModuleStatesResponse,
    OdooModuleStateInfo,
    OdooUninstallModulesRequest,
    OdooUninstallModulesResponse,
)
from src.services.helm import KubernetesService
from src.services.odoo_module import OdooModuleError, OdooModuleService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/odoo", tags=["odoo-modules"])

k8s_service = KubernetesService()
odoo_module_service = OdooModuleService(k8s_service=k8s_service)


@router.post(
    "/{namespace}/{release_name}/module-states",
    response_model=OdooModuleStatesResponse,
    summary="Query Odoo module installation states",
)
async def get_module_states(
    namespace: str,
    release_name: str,
    request: OdooModuleStatesRequest,
):
    try:
        modules = odoo_module_service.get_module_states(
            namespace=namespace,
            release_name=release_name,
            module_names=request.module_names,
        )
        return OdooModuleStatesResponse(
            success=True,
            modules={
                name: OdooModuleStateInfo(**info)
                for name, info in modules.items()
            },
        )
    except OdooModuleError as e:
        logger.error("Module state query failed at '%s': %s", e.step, str(e))
        return OdooModuleStatesResponse(success=False, error=str(e))
    except Exception as e:
        logger.exception("Unexpected error querying module states")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Module state query failed: {str(e)}",
        )


@router.post(
    "/{namespace}/{release_name}/uninstall-modules",
    response_model=OdooUninstallModulesResponse,
    summary="Uninstall Odoo modules",
)
async def uninstall_modules(
    namespace: str,
    release_name: str,
    request: OdooUninstallModulesRequest,
):
    try:
        uninstalled = odoo_module_service.uninstall_modules(
            namespace=namespace,
            release_name=release_name,
            module_names=request.module_names,
        )
        return OdooUninstallModulesResponse(
            success=True,
            uninstalled=uninstalled,
        )
    except OdooModuleError as e:
        logger.error("Module uninstall failed at '%s': %s", e.step, str(e))
        return OdooUninstallModulesResponse(success=False, error=str(e))
    except Exception as e:
        logger.exception("Unexpected error uninstalling modules")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Module uninstall failed: {str(e)}",
        )
```

**Step 2: Register router in main.py**

In `extra/paas-operator/src/main.py`:

Add import at line 10:
```python
from src.api import init, namespaces, releases, routes, tunnels, odoo_modules
```

Add router after line 153:
```python
app.include_router(odoo_modules.router)
```

**Step 3: Commit**

```bash
git add extra/paas-operator/src/api/odoo_modules.py extra/paas-operator/src/main.py
git commit -m "feat: add operator API endpoints for Odoo module states and uninstall"
```

---

## Task 3: Operator Tests

**Files:**
- Create: `extra/paas-operator/tests/test_odoo_module.py`

**Step 1: Write tests**

Create `extra/paas-operator/tests/test_odoo_module.py`:

```python
"""Tests for Odoo module state query and uninstall endpoints."""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.odoo_module import OdooModuleError, OdooModuleService


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = ""
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app)


class TestModuleStatesEndpoint:
    def test_returns_module_states(self, client):
        mock_result = {
            "sh_subscription": {"state": "installed", "dependents": []},
            "sh_crm": {"state": "uninstalled", "dependents": []},
        }
        with patch(
            "src.api.odoo_modules.odoo_module_service"
        ) as mock_svc:
            mock_svc.get_module_states.return_value = mock_result
            response = client.post(
                "/api/odoo/test-ns/test-release/module-states",
                json={"module_names": ["sh_subscription", "sh_crm"]},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["modules"]["sh_subscription"]["state"] == "installed"
        assert data["modules"]["sh_crm"]["state"] == "uninstalled"

    def test_returns_dependents(self, client):
        mock_result = {
            "sh_subscription": {
                "state": "installed",
                "dependents": ["sh_billing"],
            },
        }
        with patch(
            "src.api.odoo_modules.odoo_module_service"
        ) as mock_svc:
            mock_svc.get_module_states.return_value = mock_result
            response = client.post(
                "/api/odoo/test-ns/test-release/module-states",
                json={"module_names": ["sh_subscription"]},
            )
        data = response.json()
        assert data["modules"]["sh_subscription"]["dependents"] == ["sh_billing"]

    def test_handles_odoo_error(self, client):
        with patch(
            "src.api.odoo_modules.odoo_module_service"
        ) as mock_svc:
            mock_svc.get_module_states.side_effect = OdooModuleError(
                "Pod not found", step="find_pod"
            )
            response = client.post(
                "/api/odoo/test-ns/test-release/module-states",
                json={"module_names": ["sh_crm"]},
            )
        data = response.json()
        assert data["success"] is False
        assert "Pod not found" in data["error"]


class TestUninstallModulesEndpoint:
    def test_uninstall_success(self, client):
        with patch(
            "src.api.odoo_modules.odoo_module_service"
        ) as mock_svc:
            mock_svc.uninstall_modules.return_value = ["sh_subscription"]
            response = client.post(
                "/api/odoo/test-ns/test-release/uninstall-modules",
                json={"module_names": ["sh_subscription"]},
            )
        data = response.json()
        assert data["success"] is True
        assert data["uninstalled"] == ["sh_subscription"]

    def test_uninstall_dependency_conflict(self, client):
        with patch(
            "src.api.odoo_modules.odoo_module_service"
        ) as mock_svc:
            mock_svc.uninstall_modules.side_effect = OdooModuleError(
                "Cannot uninstall sh_subscription: module sh_billing depends on it",
                step="uninstall",
            )
            response = client.post(
                "/api/odoo/test-ns/test-release/uninstall-modules",
                json={"module_names": ["sh_subscription"]},
            )
        data = response.json()
        assert data["success"] is False
        assert "sh_billing" in data["error"]


class TestOdooModuleService:
    def test_get_module_states_parses_output(self):
        mock_k8s = MagicMock()
        mock_k8s.get_pod_name.return_value = "test-pod"
        mock_k8s.exec_in_pod.return_value = MagicMock(
            stdout=json.dumps({
                "success": True,
                "modules": {
                    "mod_a": {"state": "installed", "dependents": ["mod_b"]},
                },
            })
        )
        svc = OdooModuleService(k8s_service=mock_k8s)
        result = svc.get_module_states("ns", "rel", ["mod_a"])
        assert result["mod_a"]["state"] == "installed"
        assert result["mod_a"]["dependents"] == ["mod_b"]

    def test_uninstall_modules_returns_list(self):
        mock_k8s = MagicMock()
        mock_k8s.get_pod_name.return_value = "test-pod"
        mock_k8s.exec_in_pod.return_value = MagicMock(
            stdout=json.dumps({
                "success": True,
                "uninstalled": ["mod_a"],
            })
        )
        svc = OdooModuleService(k8s_service=mock_k8s)
        result = svc.uninstall_modules("ns", "rel", ["mod_a"])
        assert result == ["mod_a"]

    def test_raises_on_pod_not_found(self):
        from src.services.helm import KubectlException
        mock_k8s = MagicMock()
        mock_k8s.get_pod_name.side_effect = KubectlException(
            message="not found", command="kubectl", stderr=""
        )
        svc = OdooModuleService(k8s_service=mock_k8s)
        with pytest.raises(OdooModuleError, match="Cannot find Odoo pod"):
            svc.get_module_states("ns", "rel", ["mod_a"])
```

**Step 2: Run tests**

```bash
cd extra/paas-operator && pytest tests/test_odoo_module.py -v
```

Expected: All tests PASS.

**Step 3: Commit**

```bash
git add extra/paas-operator/tests/test_odoo_module.py
git commit -m "test: add tests for Odoo module state and uninstall endpoints"
```

---

## Task 4: Operator Client Methods in Odoo Backend

**Files:**
- Modify: `src/services/paas_operator.py`

**Step 1: Add client methods**

After the existing `init_odoo()` method (around line 530), add:

```python
    def get_odoo_module_states(
        self,
        namespace: str,
        release_name: str,
        module_names: list[str],
    ) -> Dict[str, Any]:
        """Query Odoo module installation states.

        Args:
            namespace: K8s namespace
            release_name: Helm release name
            module_names: Module technical names to query

        Returns:
            Dict with 'success' and 'modules' mapping name -> {state, dependents}
        """
        return self._request(
            'POST',
            f'/api/odoo/{namespace}/{release_name}/module-states',
            data={'module_names': module_names},
        )

    def uninstall_odoo_modules(
        self,
        namespace: str,
        release_name: str,
        module_names: list[str],
    ) -> Dict[str, Any]:
        """Uninstall modules in target Odoo instance.

        Args:
            namespace: K8s namespace
            release_name: Helm release name
            module_names: Module technical names to uninstall

        Returns:
            Dict with 'success' and 'uninstalled' list
        """
        return self._request(
            'POST',
            f'/api/odoo/{namespace}/{release_name}/uninstall-modules',
            data={'module_names': module_names},
            timeout=180,
        )
```

**Step 2: Commit**

```bash
git add src/services/paas_operator.py
git commit -m "feat: add get_odoo_module_states and uninstall_odoo_modules to operator client"
```

---

## Task 5: Backend Controller — Check Addon Removal & Uninstall Routes

**Files:**
- Modify: `src/controllers/paas.py`

**Step 1: Add check-addon-removal route**

Add after the existing service update endpoint (around line 915). Place near other cloud service API methods:

```python
    @http.route(
        '/api/cloud-services/services/<int:service_id>/check-addon-removal',
        type='json', auth='user', methods=['POST'],
    )
    def api_check_addon_removal(self, service_id):
        """Check if removed addons can be safely deselected.

        Queries the target Odoo instance for module states and reverse deps.

        Returns:
            {
                success: bool,
                modules: { technical_name: { addon_id, state, dependents } },
                error: str (optional)
            }
        """
        params = request.get_json_data()
        removed_addon_ids = params.get('removed_addon_ids', [])

        if not removed_addon_ids:
            return {'success': True, 'modules': {}}

        service = request.env['woow_paas_platform.cloud_service'].browse(service_id)
        if not service.exists():
            return {'success': False, 'error': 'Service not found'}

        if service.state != 'running':
            return {'success': False, 'error': f'Service must be running to check module states (current: {service.state})'}

        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': 'PaaS Operator not configured'}

        # Map addon IDs to technical names
        CloudOdooAddon = request.env['woow_paas_platform.cloud_odoo_addon']
        addons = CloudOdooAddon.browse(removed_addon_ids).filtered('active')
        if not addons:
            return {'success': True, 'modules': {}}

        tech_names = [a.technical_name for a in addons]
        name_to_id = {a.technical_name: a.id for a in addons}

        try:
            result = client.get_odoo_module_states(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                module_names=tech_names,
            )
        except Exception as e:
            return {'success': False, 'error': f'Failed to query module states: {e}'}

        if not result.get('success'):
            return {'success': False, 'error': result.get('error', 'Unknown error')}

        # Enrich with addon IDs
        modules = {}
        for name, info in result.get('modules', {}).items():
            modules[name] = {
                'addon_id': name_to_id.get(name),
                'state': info.get('state', 'unknown'),
                'dependents': info.get('dependents', []),
            }

        return {'success': True, 'modules': modules}
```

**Step 2: Add uninstall-addons route**

```python
    @http.route(
        '/api/cloud-services/services/<int:service_id>/uninstall-addons',
        type='json', auth='user', methods=['POST'],
    )
    def api_uninstall_addons(self, service_id):
        """Uninstall Odoo modules before removing addons.

        Must be called before updateServiceAddons when force-removing
        installed modules.

        Returns:
            { success: bool, uninstalled: [...], error: str (optional) }
        """
        params = request.get_json_data()
        addon_technical_names = params.get('addon_technical_names', [])

        if not addon_technical_names:
            return {'success': True, 'uninstalled': []}

        service = request.env['woow_paas_platform.cloud_service'].browse(service_id)
        if not service.exists():
            return {'success': False, 'error': 'Service not found'}

        if service.state != 'running':
            return {'success': False, 'error': f'Service must be running to uninstall modules (current: {service.state})'}

        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': 'PaaS Operator not configured'}

        try:
            result = client.uninstall_odoo_modules(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                module_names=addon_technical_names,
            )
        except Exception as e:
            return {'success': False, 'error': f'Failed to uninstall modules: {e}'}

        if not result.get('success'):
            return {'success': False, 'error': result.get('error', 'Unknown error')}

        return {
            'success': True,
            'uninstalled': result.get('uninstalled', []),
        }
```

**Step 3: Commit**

```bash
git add src/controllers/paas.py
git commit -m "feat: add check-addon-removal and uninstall-addons API routes"
```

---

## Task 6: Bug 2 Fix — All Addons Deselected + _remove_addon_helm_values

**Files:**
- Modify: `src/models/cloud_service.py`
- Modify: `src/controllers/paas.py:1542`

**Step 1: Add `_remove_addon_helm_values` to CloudService**

In `src/models/cloud_service.py`, after `_build_odoo_addon_helm_values()` (after line 461):

```python
    def _remove_addon_helm_values(self, values):
        """Remove only addon-selector related entries from helm values.

        Used when all addons are deselected. Precisely removes addon-specific
        items without affecting other extraVolumes/initContainers.
        """
        if 'initContainers' in values:
            values['initContainers'] = [
                c for c in values['initContainers']
                if c.get('name') != 'addon-selector'
            ]
        if 'extraVolumes' in values:
            values['extraVolumes'] = [
                v for v in values['extraVolumes']
                if v.get('name') != 'selected-addons'
            ]
        if 'extraVolumeMounts' in values:
            values['extraVolumeMounts'] = [
                m for m in values['extraVolumeMounts']
                if m.get('name') != 'selected-addons'
            ]
        if 'customPostInitScripts' in values:
            values['customPostInitScripts'].pop('00-addons-path.sh', None)
```

**Step 2: Fix condition in `_update_service`**

In `src/controllers/paas.py`, replace lines 1541-1549:

```python
            # Rebuild addon initContainer helm values when addon selection changed
            if addon_changed and service.odoo_addon_ids:
                image_vals = merged_values.get('image', {})
                registry = image_vals.get('registry', '')
                repo = image_vals.get('repository', '')
                tag = image_vals.get('tag', 'latest')
                image_ref = f"{registry}/{repo}:{tag}" if registry else f"{repo}:{tag}"
                addon_values = service._build_odoo_addon_helm_values(image_ref)
                merged_values = self._deep_merge(merged_values, addon_values)
```

With:

```python
            # Rebuild or remove addon initContainer helm values when addon selection changed
            if addon_changed:
                if service.odoo_addon_ids:
                    image_vals = merged_values.get('image', {})
                    registry = image_vals.get('registry', '')
                    repo = image_vals.get('repository', '')
                    tag = image_vals.get('tag', 'latest')
                    image_ref = f"{registry}/{repo}:{tag}" if registry else f"{repo}:{tag}"
                    addon_values = service._build_odoo_addon_helm_values(image_ref)
                    merged_values = self._deep_merge(merged_values, addon_values)
                else:
                    # All addons deselected — remove addon-specific helm values
                    service._remove_addon_helm_values(merged_values)
```

**Step 3: Commit**

```bash
git add src/models/cloud_service.py src/controllers/paas.py
git commit -m "fix: handle all-addon deselection by removing addon-specific helm values"
```

---

## Task 7: Frontend — cloud_service.js API Methods

**Files:**
- Modify: `src/static/src/paas/services/cloud_service.js`

**Step 1: Add API methods**

After `updateServiceAddons()` (after line 380), add:

```javascript
    /**
     * Check if removed addons can be safely deselected
     * @param {number} serviceId - Service ID
     * @param {number[]} removedAddonIds - Addon IDs being removed
     * @returns {Promise<{success: boolean, modules?: Object, error?: string}>}
     */
    async checkAddonRemoval(serviceId, removedAddonIds) {
        try {
            return await jsonRpc(`/api/cloud-services/services/${serviceId}/check-addon-removal`, {
                removed_addon_ids: removedAddonIds,
            });
        } catch (err) {
            return { success: false, error: err.message };
        }
    },

    /**
     * Uninstall Odoo modules before removing addons
     * @param {number} serviceId - Service ID
     * @param {string[]} addonTechnicalNames - Module technical names to uninstall
     * @returns {Promise<{success: boolean, uninstalled?: string[], error?: string}>}
     */
    async uninstallServiceAddons(serviceId, addonTechnicalNames) {
        try {
            return await jsonRpc(`/api/cloud-services/services/${serviceId}/uninstall-addons`, {
                addon_technical_names: addonTechnicalNames,
            });
        } catch (err) {
            return { success: false, error: err.message };
        }
    },
```

**Step 2: Commit**

```bash
git add src/static/src/paas/services/cloud_service.js
git commit -m "feat: add checkAddonRemoval and uninstallServiceAddons API methods"
```

---

## Task 8: Frontend — AddonRemovalDialog Component

**Files:**
- Create: `src/static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.js`
- Create: `src/static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.xml`
- Create: `src/static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.scss`
- Modify: `src/__manifest__.py` (add to assets)

**Step 1: Create JS component**

Create `src/static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.js`:

```javascript
/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";

/**
 * AddonRemovalDialog Component
 *
 * Shows warnings when deselecting addons that are installed in Odoo.
 *
 * Props:
 *   - mode ('blocked' | 'confirm'): Dialog type
 *   - modules (Object): { technical_name: { state, dependents } }
 *   - odooAppsUrl (string): URL to open Odoo Apps page
 *   - onClose (Function): Called when dialog is dismissed
 *   - onForceUninstall (Function): Called when user confirms force uninstall
 */
export class AddonRemovalDialog extends Component {
  static template = "woow_paas_platform.AddonRemovalDialog";
  static components = { WoowButton, WoowIcon };
  static props = {
    mode: { type: String }, // 'blocked' | 'confirm'
    modules: { type: Object },
    odooAppsUrl: { type: String, optional: true },
    onClose: { type: Function },
    onForceUninstall: { type: Function, optional: true },
  };

  setup() {
    this.state = useState({ uninstalling: false, error: null });
  }

  get installedModules() {
    return Object.entries(this.props.modules)
      .filter(([_, m]) => m.state === 'installed')
      .map(([name, m]) => ({ name, ...m }));
  }

  get blockedModules() {
    return Object.entries(this.props.modules)
      .filter(([_, m]) => m.dependents && m.dependents.length > 0)
      .map(([name, m]) => ({ name, dependents: m.dependents }));
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.uninstalling) {
      this.props.onClose();
    }
  }

  openOdooApps() {
    if (this.props.odooAppsUrl) {
      window.open(this.props.odooAppsUrl, "_blank");
    }
    this.props.onClose();
  }

  async onForceUninstall() {
    if (this.state.uninstalling) return;
    this.state.uninstalling = true;
    this.state.error = null;
    try {
      await this.props.onForceUninstall();
    } catch (err) {
      this.state.error = err.message || "Uninstall failed";
      this.state.uninstalling = false;
    }
  }
}
```

**Step 2: Create XML template**

Create `src/static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">
    <t t-name="woow_paas_platform.AddonRemovalDialog">
        <div class="o_woow_modal_overlay" t-on-click="onBackdropClick">
            <div class="o_woow_modal o_woow_addon_removal_dialog">
                <div class="o_woow_modal_header">
                    <WoowIcon t-if="props.mode === 'blocked'" name="'block'" size="24"/>
                    <WoowIcon t-else="" name="'warning'" size="24"/>
                    <h3 t-if="props.mode === 'blocked'">Cannot Remove Addon</h3>
                    <h3 t-else="">Addon Is Active in Odoo</h3>
                </div>

                <div class="o_woow_modal_body">
                    <!-- Blocked mode: dependency conflict -->
                    <t t-if="props.mode === 'blocked'">
                        <p class="o_woow_dialog_desc">
                            The following addon(s) cannot be removed because other installed modules depend on them:
                        </p>
                        <div class="o_woow_blocked_list">
                            <t t-foreach="blockedModules" t-as="mod" t-key="mod.name">
                                <div class="o_woow_blocked_item">
                                    <strong t-esc="mod.name"/>
                                    <span> is required by: </span>
                                    <span class="o_woow_dep_names" t-esc="mod.dependents.join(', ')"/>
                                </div>
                            </t>
                        </div>
                        <p class="o_woow_dialog_hint">
                            Please uninstall the dependent modules first, either from this panel or directly in Odoo.
                        </p>
                    </t>

                    <!-- Confirm mode: installed but no conflicts -->
                    <t t-else="">
                        <p class="o_woow_dialog_desc">
                            The following addon(s) are currently active (installed) in your Odoo instance:
                        </p>
                        <div class="o_woow_installed_list">
                            <t t-foreach="installedModules" t-as="mod" t-key="mod.name">
                                <div class="o_woow_installed_item">
                                    <WoowIcon name="'extension'" size="18"/>
                                    <span t-esc="mod.name"/>
                                </div>
                            </t>
                        </div>
                        <p class="o_woow_dialog_hint">
                            Force uninstalling will remove all related data (menus, views, settings) from Odoo. This cannot be undone.
                        </p>
                        <div t-if="state.error" class="o_woow_error_message">
                            <WoowIcon name="'error'" size="18"/>
                            <span t-esc="state.error"/>
                        </div>
                    </t>
                </div>

                <div class="o_woow_modal_footer">
                    <t t-if="props.mode === 'blocked'">
                        <WoowButton variant="'primary'" size="'md'" onClick.bind="props.onClose">
                            OK
                        </WoowButton>
                    </t>
                    <t t-else="">
                        <WoowButton
                            t-if="props.odooAppsUrl"
                            variant="'secondary'"
                            size="'md'"
                            onClick.bind="openOdooApps">
                            <WoowIcon name="'open_in_new'" size="18"/>
                            Open Odoo Apps
                        </WoowButton>
                        <WoowButton
                            variant="'danger'"
                            size="'md'"
                            onClick.bind="onForceUninstall"
                            disabled="state.uninstalling">
                            <t t-if="state.uninstalling">
                                <WoowIcon name="'sync'" size="18" class="'o_woow_spin'"/>
                                Uninstalling...
                            </t>
                            <t t-else="">
                                <WoowIcon name="'delete'" size="18"/>
                                Force Uninstall &amp; Remove
                            </t>
                        </WoowButton>
                        <WoowButton
                            variant="'secondary'"
                            size="'md'"
                            onClick.bind="props.onClose"
                            disabled="state.uninstalling">
                            Cancel
                        </WoowButton>
                    </t>
                </div>
            </div>
        </div>
    </t>
</templates>
```

**Step 3: Create SCSS**

Create `src/static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.scss`:

```scss
.o_woow_addon_removal_dialog {
  max-width: 520px;

  .o_woow_modal_header {
    h3 {
      color: var(--woow-warning, #f59e0b);
    }
  }

  .o_woow_dialog_desc {
    margin-bottom: 12px;
    color: var(--woow-text-secondary);
  }

  .o_woow_dialog_hint {
    margin-top: 12px;
    font-size: 0.85rem;
    color: var(--woow-text-muted);
  }

  .o_woow_blocked_list,
  .o_woow_installed_list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: var(--woow-bg-subtle, rgba(0, 0, 0, 0.03));
    border-radius: 8px;
  }

  .o_woow_blocked_item {
    .o_woow_dep_names {
      color: var(--woow-danger, #ef4444);
      font-weight: 500;
    }
  }

  .o_woow_installed_item {
    display: flex;
    align-items: center;
    gap: 8px;
  }
}
```

**Step 4: Register assets in `__manifest__.py`**

In `src/__manifest__.py`, add to `woow_paas_platform.assets_paas` bundle:

```python
'static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.js',
'static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.xml',
'static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.scss',
```

**Step 5: Commit**

```bash
git add src/static/src/paas/components/addon-removal-dialog/ src/__manifest__.py
git commit -m "feat: add AddonRemovalDialog component for safe addon deselection"
```

---

## Task 9: Frontend — Rewrite AddonsTab.saveAddons()

**Files:**
- Modify: `src/static/src/paas/pages/service/tabs/AddonsTab.js`
- Modify: `src/static/src/paas/pages/service/tabs/AddonsTab.xml`

**Step 1: Update AddonsTab.js**

Replace entire file content:

```javascript
/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { OdooAddonSelector } from "../../../components/odoo-addon-selector/OdooAddonSelector";
import { AddonRemovalDialog } from "../../../components/addon-removal-dialog/AddonRemovalDialog";
import { cloudService } from "../../../services/cloud_service";

export class AddonsTab extends Component {
  static template = "woow_paas_platform.AddonsTab";
  static components = { WoowCard, WoowIcon, WoowButton, OdooAddonSelector, AddonRemovalDialog };
  static props = {
    service: { type: Object },
    workspaceId: { type: Number },
    onSave: { type: Function, optional: true },
  };

  setup() {
    this.state = useState({
      editing: false,
      saving: false,
      checking: false,
      error: null,
      selectedIds: [],
      // Dialog state
      showDialog: false,
      dialogMode: null, // 'blocked' | 'confirm'
      dialogModules: {},
    });
  }

  get currentAddons() {
    return this.props.service.odoo_addons || [];
  }

  get currentAddonIds() {
    return this.currentAddons.map(a => a.id);
  }

  get hasChanges() {
    const current = new Set(this.currentAddonIds);
    const selected = new Set(this.state.selectedIds);
    if (current.size !== selected.size) return true;
    for (const id of current) {
      if (!selected.has(id)) return true;
    }
    return false;
  }

  get canEdit() {
    const disabledStates = ["deploying", "initializing", "upgrading", "deleting", "pending"];
    return !disabledStates.includes(this.props.service.state);
  }

  get removedAddonIds() {
    const selected = new Set(this.state.selectedIds);
    return this.currentAddonIds.filter(id => !selected.has(id));
  }

  get odooAppsUrl() {
    const svc = this.props.service;
    if (svc.subdomain) {
      // Construct from subdomain — actual domain comes from config
      return `https://${svc.subdomain}.woowtech.io/odoo/apps/modules`;
    }
    return null;
  }

  startEditing() {
    this.state.selectedIds = [...this.currentAddonIds];
    this.state.editing = true;
    this.state.error = null;
  }

  cancelEditing() {
    this.state.editing = false;
    this.state.selectedIds = [];
    this.state.error = null;
    this.state.showDialog = false;
  }

  onSelectionChange(ids) {
    this.state.selectedIds = ids;
  }

  async saveAddons() {
    if (!this.hasChanges) return;

    const removed = this.removedAddonIds;

    // No removals — go directly to update
    if (removed.length === 0) {
      return this._doUpdateAddons();
    }

    // Has removals — check module states first
    this.state.checking = true;
    this.state.error = null;

    try {
      const result = await cloudService.checkAddonRemoval(
        this.props.service.id,
        removed,
      );

      if (!result.success) {
        this.state.error = result.error || "Failed to check addon states";
        return;
      }

      const modules = result.modules || {};
      const hasInstalled = Object.values(modules).some(m => m.state === 'installed');
      const hasBlocked = Object.values(modules).some(m => m.dependents && m.dependents.length > 0);

      if (!hasInstalled) {
        // All removed addons are uninstalled — safe to proceed
        return this._doUpdateAddons();
      }

      if (hasBlocked) {
        this.state.dialogMode = 'blocked';
      } else {
        this.state.dialogMode = 'confirm';
      }
      this.state.dialogModules = modules;
      this.state.showDialog = true;
    } catch (err) {
      this.state.error = err.message || "Failed to check addon states";
    } finally {
      this.state.checking = false;
    }
  }

  closeDialog() {
    this.state.showDialog = false;
    this.state.dialogMode = null;
    this.state.dialogModules = {};
  }

  async onForceUninstall() {
    const modules = this.state.dialogModules;
    const toUninstall = Object.entries(modules)
      .filter(([_, m]) => m.state === 'installed')
      .map(([name]) => name);

    const result = await cloudService.uninstallServiceAddons(
      this.props.service.id,
      toUninstall,
    );

    if (!result.success) {
      throw new Error(result.error || "Uninstall failed");
    }

    this.closeDialog();
    await this._doUpdateAddons();
  }

  async _doUpdateAddons() {
    this.state.saving = true;
    this.state.error = null;

    try {
      const result = await cloudService.updateServiceAddons(
        this.props.workspaceId,
        this.props.service.id,
        this.state.selectedIds,
      );

      if (result.success) {
        this.state.editing = false;
        this.state.selectedIds = [];
        if (this.props.onSave) {
          this.props.onSave(result.data);
        }
      } else {
        this.state.error = result.error || "Failed to update addons";
      }
    } catch (err) {
      this.state.error = err.message || "Failed to update addons";
    } finally {
      this.state.saving = false;
    }
  }
}
```

**Step 2: Update AddonsTab.xml**

Replace the Save Changes button section (lines 29-42) to show checking state:

```xml
                        <WoowButton
                            variant="'primary'"
                            size="'md'"
                            onClick.bind="saveAddons"
                            disabled="!hasChanges or state.saving or state.checking">
                            <t t-if="state.checking">
                                <WoowIcon name="'sync'" size="18" class="'o_woow_spin'"/>
                                Checking...
                            </t>
                            <t t-elif="state.saving">
                                <WoowIcon name="'sync'" size="18" class="'o_woow_spin'"/>
                                Saving...
                            </t>
                            <t t-else="">
                                <WoowIcon name="'save'" size="18"/>
                                Save Changes
                            </t>
                        </WoowButton>
```

Add dialog at the end of the template, before closing `</div>`:

```xml
            <!-- Addon Removal Dialog -->
            <AddonRemovalDialog
                t-if="state.showDialog"
                mode="state.dialogMode"
                modules="state.dialogModules"
                odooAppsUrl="odooAppsUrl"
                onClose.bind="closeDialog"
                onForceUninstall.bind="onForceUninstall"/>
```

**Step 3: Commit**

```bash
git add src/static/src/paas/pages/service/tabs/AddonsTab.js src/static/src/paas/pages/service/tabs/AddonsTab.xml
git commit -m "feat: rewrite AddonsTab saveAddons with pre-check and confirmation dialog"
```

---

## Task 10: Frontend — OdooAddonSelector Dependency Guard

**Files:**
- Modify: `src/static/src/paas/components/odoo-addon-selector/OdooAddonSelector.js`
- Modify: `src/static/src/paas/components/odoo-addon-selector/OdooAddonSelector.xml`

**Step 1: Update toggleAddon in OdooAddonSelector.js**

Replace `toggleAddon()` method (lines 73-89):

```javascript
    toggleAddon(addonId) {
        const current = new Set(this.props.selectedIds || []);
        if (current.has(addonId)) {
            // Check if other selected addons depend on this one
            const dependedBy = this._getDependentAddons(addonId);
            if (dependedBy.length > 0) {
                // Cannot deselect — other selected addons depend on it
                return;
            }
            current.delete(addonId);
        } else {
            current.add(addonId);
            // Auto-add dependencies
            const allAddons = cloudService.odooAddons || [];
            const addon = allAddons.find(a => a.id === addonId);
            if (addon) {
                for (const dep of addon.depends_on) {
                    current.add(dep.id);
                }
            }
        }
        this.props.onSelectionChange([...current]);
    }

    /**
     * Get names of selected addons that depend on the given addon.
     */
    _getDependentAddons(addonId) {
        const selected = this.props.selectedIds || [];
        const allAddons = cloudService.odooAddons || [];
        const dependents = [];
        for (const id of selected) {
            if (id === addonId) continue;
            const addon = allAddons.find(a => a.id === id);
            if (addon && addon.depends_on.some(d => d.id === addonId)) {
                dependents.push(addon.name);
            }
        }
        return dependents;
    }

    /**
     * Check if this addon cannot be deselected due to dependents.
     */
    isRequiredByOthers(addonId) {
        if (!this.isSelected(addonId)) return false;
        return this._getDependentAddons(addonId).length > 0;
    }

    /**
     * Get tooltip text for why an addon cannot be deselected.
     */
    getRequiredByTooltip(addonId) {
        const dependents = this._getDependentAddons(addonId);
        if (dependents.length === 0) return "";
        return `Required by: ${dependents.join(", ")}`;
    }
```

**Step 2: Update OdooAddonSelector.xml checkbox**

Find the checkbox input in the template and add disabled + tooltip when `isRequiredByOthers`:

```xml
<input
    type="checkbox"
    t-att-checked="isSelected(addon.id)"
    t-att-disabled="isRequiredByOthers(addon.id)"
    t-att-title="getRequiredByTooltip(addon.id)"
    t-on-change="() => this.toggleAddon(addon.id)"/>
```

**Step 3: Commit**

```bash
git add src/static/src/paas/components/odoo-addon-selector/OdooAddonSelector.js src/static/src/paas/components/odoo-addon-selector/OdooAddonSelector.xml
git commit -m "feat: add dependency guard to prevent deselecting required addons"
```

---

## Task Summary

| Task | Component | Type |
|------|-----------|------|
| 1 | OdooModuleService + schemas | operator service |
| 2 | Operator API endpoints | operator API |
| 3 | Operator tests | tests |
| 4 | Operator client methods | backend |
| 5 | Controller routes (check + uninstall) | backend |
| 6 | Bug 2 fix + _remove_addon_helm_values | backend fix |
| 7 | cloud_service.js API methods | frontend |
| 8 | AddonRemovalDialog component | frontend |
| 9 | AddonsTab.saveAddons rewrite | frontend |
| 10 | OdooAddonSelector dependency guard | frontend |
