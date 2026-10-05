"""Odoo module management service.

Provides operations for querying Odoo module installation states
via kubectl exec into Odoo containers.

Uses direct ORM access inside the container (same pattern as odoo_init.py):
- Parses odoo.conf to get the database name
- Opens a registry cursor as SUPERUSER_ID
- Queries ir.module.module records
- Outputs JSON on the last line for parsing
"""

import json
import logging
import re

from src.services.helm import KubectlException, KubernetesService
from src.services.odoo_chart_contract import (
    ODOO_CONF_PATH,
    ODOO_CONTAINER_NAME,
    ODOO_PYTHON,
)
from src.services.odoo_init import OdooInitError, OdooInitService

logger = logging.getLogger(__name__)

# Timeout for querying module states via kubectl exec
MODULE_QUERY_TIMEOUT = 120


class OdooModuleError(Exception):
    """Raised when an Odoo module operation fails."""

    def __init__(self, message: str, step: str = ""):
        self.step = step
        super().__init__(message)


class OdooModuleService:
    """Handles Odoo module state queries.

    All operations execute Python scripts inside the Odoo container
    via kubectl exec, using direct ORM access with SUPERUSER_ID.
    """

    def __init__(self, k8s_service: KubernetesService | None = None):
        self.k8s = k8s_service or KubernetesService()
        self._odoo = OdooInitService(k8s_service=self.k8s)

    def _get_odoo_pod(
        self, namespace: str, release_name: str
    ) -> tuple[str, str]:
        """Find the Odoo pod name and container name.

        #1477：pod 定位委派 ``OdooInitService._get_odoo_pod``——與 init/odoo、
        admin-password 同一份 woow-odoo 契約（``component=server`` selector、
        instance-only fallback 經 ``validate_fallback_pod`` 驗身分），不在這裡再寫一份。

        Args:
            namespace: Kubernetes namespace.
            release_name: Helm release name.

        Returns:
            Tuple of (pod_name, container_name).

        Raises:
            OdooModuleError: If no verified Odoo server pod is found.
        """
        try:
            pod_name = self._odoo._get_odoo_pod(namespace, release_name)
        except OdooInitError as exc:
            raise OdooModuleError(str(exc), step="find_pod") from exc
        return pod_name, ODOO_CONTAINER_NAME

    def get_module_states(
        self,
        namespace: str,
        release_name: str,
        module_names: list[str],
    ) -> dict:
        """Query Odoo module installation states and reverse dependencies.

        Runs a Python script inside the Odoo container that:
        1. Parses odoo.conf to get the database name
        2. Opens a registry cursor as SUPERUSER_ID
        3. Queries ir.module.module for state of each requested module
        4. Queries ir.module.module.dependency for reverse deps
           (only includes dependents that are 'installed' AND not in
           the requested module list)

        Args:
            namespace: Kubernetes namespace.
            release_name: Helm release name.
            module_names: List of module technical names to check.

        Returns:
            Dict mapping module_name -> {state, dependents}.

        Raises:
            OdooModuleError: If the query fails.
        """
        _MODULE_NAME_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
        for name in module_names:
            if not _MODULE_NAME_RE.match(name):
                raise OdooModuleError(
                    f"Invalid module name: {name}", step="validate",
                )

        logger.info(
            "Querying module states for %s in %s/%s",
            module_names,
            namespace,
            release_name,
        )

        pod_name, container = self._get_odoo_pod(namespace, release_name)

        # Build the in-container Python script.
        # We pass module_names as a JSON-encoded string inside the script.
        modules_json = json.dumps(module_names)
        query_script = (
            "import odoo, json, sys\n"
            "try:\n"
            "    odoo.tools.config.parse_config("
            f"['-c', {ODOO_CONF_PATH!r}, '--no-http'])\n"
            "    db_name = odoo.tools.config['db_name']\n"
            "    if not db_name:\n"
            '        print(json.dumps({"success": False, '
            '"error": "No db_name found in odoo.conf"}))\n'
            "        sys.exit(1)\n"
            "    registry = odoo.registry(db_name)\n"
            "    with registry.cursor() as cr:\n"
            "        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})\n"
            f"        module_names = {modules_json}\n"
            "        result = {}\n"
            "        Module = env['ir.module.module']\n"
            "        Dep = env['ir.module.module.dependency']\n"
            "        for name in module_names:\n"
            "            mod = Module.search([('name', '=', name)], limit=1)\n"
            "            if not mod:\n"
            "                result[name] = {'state': 'not_found', 'dependents': []}\n"
            "                continue\n"
            "            deps = Dep.search([('name', '=', name)])\n"
            "            dependents = []\n"
            "            for dep in deps:\n"
            "                parent = dep.module_id\n"
            "                if parent.state == 'installed' and parent.name not in module_names:\n"
            "                    dependents.append(parent.name)\n"
            "            result[name] = {'state': mod.state, 'dependents': dependents}\n"
            '        print(json.dumps({"success": True, "modules": result}))\n'
            "except Exception as e:\n"
            '    print(json.dumps({"success": False, "error": str(e)}))\n'
            "    sys.exit(1)\n"
        )

        command = [ODOO_PYTHON, "-c", query_script]

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
                f"Failed to execute module state query: {e}",
                step="query_modules",
            ) from e

        return self._parse_json_output(result.stdout, step="query_modules")

    @staticmethod
    def _parse_json_output(output: str, step: str) -> dict:
        """Parse the last non-empty line of kubectl exec output as JSON.

        The in-container script may produce extra output lines (e.g. Odoo
        logging to stdout). We take the last non-empty line which should
        be our JSON payload.

        Args:
            output: Raw stdout from kubectl exec.
            step: Current operation step (for error messages).

        Returns:
            Parsed dict from the JSON output.

        Raises:
            OdooModuleError: If output is empty, not valid JSON, or
                indicates failure.
        """
        stripped = output.strip()
        if not stripped:
            raise OdooModuleError(
                f"Command returned empty output at step '{step}'",
                step=step,
            )

        lines = [line for line in stripped.splitlines() if line.strip()]
        json_line = lines[-1] if lines else ""

        try:
            data = json.loads(json_line)
        except json.JSONDecodeError as e:
            raise OdooModuleError(
                f"Invalid JSON output at step '{step}': {json_line[:200]}",
                step=step,
            ) from e

        if not data.get("success"):
            raise OdooModuleError(
                f"Operation failed at step '{step}': "
                f"{data.get('error', 'unknown error')}",
                step=step,
            )

        return data
