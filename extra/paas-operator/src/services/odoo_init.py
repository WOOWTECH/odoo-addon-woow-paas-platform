"""Odoo post-deploy initialization service.

Handles automatic module list refresh for newly deployed Odoo instances
so that custom addons are immediately discoverable without manual
intervention via Settings > Apps > Update Apps List.

Uses two execution strategies inside the Odoo container:
- XML-RPC for readiness checks (lightweight, no ORM boot required)
- Direct ORM access for module list update (bypasses authentication,
  uses SUPERUSER_ID to avoid admin password issues)
"""

import json
import logging
import time

from src.services.helm import KubectlException, KubernetesService

logger = logging.getLogger(__name__)

# Retry configuration for readiness polling
MAX_READY_RETRIES = 30
READY_RETRY_DELAY = 5  # seconds (total wait: 150s)

# Timeout for the module list update operation
MODULE_UPDATE_TIMEOUT = 120  # seconds


class OdooInitError(Exception):
    """Raised when Odoo initialization fails."""

    def __init__(self, message: str, step: str = ""):
        self.step = step
        super().__init__(message)


class OdooInitService:
    """Handles Odoo post-deploy module list initialization.

    Performs the init sequence:
    1. Locate the Odoo pod in the target namespace
    2. Wait for Odoo HTTP service to be ready (XML-RPC version check)
    3. Execute module list update via direct ORM access
    """

    def __init__(self, k8s_service: KubernetesService | None = None):
        self.k8s = k8s_service or KubernetesService()

    def initialize(self, namespace: str, release_name: str) -> dict:
        """Complete Odoo module list initialization sequence.

        Args:
            namespace: Kubernetes namespace where Odoo is deployed.
            release_name: Helm release name for the Odoo deployment.

        Returns:
            Dict with keys: success, modules_added, modules_updated, message.

        Raises:
            OdooInitError: If any step in the initialization fails.
        """
        logger.info(
            "Starting Odoo module list initialization for %s/%s",
            namespace,
            release_name,
        )

        # Step 1: Find the Odoo pod
        pod_name = self._get_odoo_pod(namespace, release_name)
        container = f"{release_name}-odoo"
        logger.info(
            "Found Odoo pod: %s (container: %s)", pod_name, container
        )

        # Step 2: Wait for Odoo to be ready
        self._wait_for_ready(namespace, pod_name, container)

        # Step 3: Update module list
        result = self._update_module_list(namespace, pod_name, container)

        logger.info(
            "Odoo module list initialization complete for %s/%s: "
            "added=%d, updated=%d",
            namespace,
            release_name,
            result.get("added", 0),
            result.get("updated", 0),
        )

        return {
            "success": True,
            "modules_added": result.get("added", 0),
            "modules_updated": result.get("updated", 0),
            "message": "Module list updated successfully",
        }

    def _get_odoo_pod(self, namespace: str, release_name: str) -> str:
        """Find the Odoo pod name by label selectors.

        Tries a specific selector first (instance + name=odoo), then
        falls back to instance-only selector for non-standard charts.

        Args:
            namespace: Kubernetes namespace.
            release_name: Helm release name.

        Returns:
            The pod name string.

        Raises:
            OdooInitError: If no matching pod is found.
        """
        selectors = [
            f"app.kubernetes.io/instance={release_name},app.kubernetes.io/name=odoo",
            f"app.kubernetes.io/instance={release_name}",
        ]

        for selector in selectors:
            try:
                pod_name = self.k8s.get_pod_name(namespace, selector)
                logger.debug(
                    "Found pod '%s' with selector '%s'", pod_name, selector
                )
                return pod_name
            except KubectlException:
                logger.debug(
                    "No pod found with selector '%s', trying next", selector
                )
                continue

        raise OdooInitError(
            f"Cannot find Odoo pod for release {release_name} in {namespace}",
            step="find_pod",
        )

    def _wait_for_ready(
        self, namespace: str, pod_name: str, container: str
    ) -> None:
        """Wait for Odoo HTTP service to be ready.

        Polls the XML-RPC common endpoint to check if Odoo is accepting
        connections. This is a lightweight check that does not require
        ORM initialization.

        Args:
            namespace: Kubernetes namespace.
            pod_name: Name of the Odoo pod.
            container: Name of the Odoo container.

        Raises:
            OdooInitError: If Odoo is not ready after all retries.
        """
        logger.info("Waiting for Odoo to be ready (max %ds)...", MAX_READY_RETRIES * READY_RETRY_DELAY)

        check_script = (
            "import xmlrpc.client, json, sys\n"
            "try:\n"
            "    common = xmlrpc.client.ServerProxy('http://localhost:8069/xmlrpc/2/common')\n"
            "    v = common.version()\n"
            '    print(json.dumps({"ready": True, "version": v.get("server_version", "unknown")}))\n'
            "except Exception as e:\n"
            '    print(json.dumps({"ready": False, "error": str(e)}))\n'
            "    sys.exit(1)\n"
        )

        command = ["/opt/bitnami/odoo/venv/bin/python3", "-c", check_script]

        for attempt in range(MAX_READY_RETRIES):
            try:
                result = self.k8s.exec_in_pod(
                    namespace=namespace,
                    pod_name=pod_name,
                    container=container,
                    command=command,
                    timeout=15,
                )

                output = result.stdout.strip()
                if output:
                    data = json.loads(output)
                    if data.get("ready"):
                        logger.info(
                            "Odoo is ready (version: %s, attempt %d/%d)",
                            data.get("version", "unknown"),
                            attempt + 1,
                            MAX_READY_RETRIES,
                        )
                        return

            except KubectlException as e:
                logger.debug(
                    "Readiness check attempt %d/%d failed: %s",
                    attempt + 1,
                    MAX_READY_RETRIES,
                    e,
                )
            except json.JSONDecodeError as e:
                logger.debug(
                    "Readiness check attempt %d/%d returned invalid JSON: %s",
                    attempt + 1,
                    MAX_READY_RETRIES,
                    e,
                )

            if attempt < MAX_READY_RETRIES - 1:
                logger.debug(
                    "Odoo not ready yet, retrying in %ds... (%d/%d)",
                    READY_RETRY_DELAY,
                    attempt + 1,
                    MAX_READY_RETRIES,
                )
                time.sleep(READY_RETRY_DELAY)

        raise OdooInitError(
            f"Odoo not ready after {MAX_READY_RETRIES * READY_RETRY_DELAY}s",
            step="wait_ready",
        )

    def _update_module_list(
        self, namespace: str, pod_name: str, container: str
    ) -> dict:
        """Execute module list update via direct ORM access.

        Runs a Python script inside the Odoo container that:
        1. Parses odoo.conf to get the database name
        2. Opens a registry cursor as SUPERUSER_ID
        3. Calls ir.module.module.update_list()
        4. Commits the transaction

        This approach bypasses XML-RPC authentication entirely, avoiding
        issues with admin passwords or API keys.

        Args:
            namespace: Kubernetes namespace.
            pod_name: Name of the Odoo pod.
            container: Name of the Odoo container.

        Returns:
            Dict with keys: success, added, updated.

        Raises:
            OdooInitError: If the module list update fails.
        """
        logger.info("Updating Odoo module list via direct ORM access...")

        update_script = (
            "import odoo, json, sys\n"
            "try:\n"
            "    odoo.tools.config.parse_config("
            "['-c', '/opt/bitnami/odoo/conf/odoo.conf', '--no-http'])\n"
            "    db_name = odoo.tools.config['db_name']\n"
            "    if not db_name:\n"
            '        print(json.dumps({"success": False, '
            '"error": "No db_name found in odoo.conf"}))\n'
            "        sys.exit(1)\n"
            "    registry = odoo.registry(db_name)\n"
            "    with registry.cursor() as cr:\n"
            "        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})\n"
            "        result = env['ir.module.module'].update_list()\n"
            "        cr.commit()\n"
            "        added = result[0] if isinstance(result, (list, tuple)) else 0\n"
            "        updated = result[1] if isinstance(result, (list, tuple)) else 0\n"
            '        print(json.dumps({"success": True, '
            '"added": added, "updated": updated}))\n'
            "except Exception as e:\n"
            '    print(json.dumps({"success": False, "error": str(e)}))\n'
            "    sys.exit(1)\n"
        )

        command = ["/opt/bitnami/odoo/venv/bin/python3", "-c", update_script]

        try:
            result = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=container,
                command=command,
                timeout=MODULE_UPDATE_TIMEOUT,
            )
        except KubectlException as e:
            raise OdooInitError(
                f"Failed to execute module list update: {e}",
                step="update_module_list",
            ) from e

        output = result.stdout.strip()
        if not output:
            raise OdooInitError(
                "Module list update returned empty output",
                step="update_module_list",
            )

        # The script may produce extra output lines (e.g. Odoo logging to
        # stdout). We take the last non-empty line which should be our JSON.
        lines = [line for line in output.splitlines() if line.strip()]
        json_line = lines[-1] if lines else ""

        try:
            data = json.loads(json_line)
        except json.JSONDecodeError as e:
            raise OdooInitError(
                f"Module list update returned invalid JSON: {json_line[:200]}",
                step="update_module_list",
            ) from e

        if not data.get("success"):
            raise OdooInitError(
                f"Module list update failed: {data.get('error', 'unknown error')}",
                step="update_module_list",
            )

        logger.info(
            "Module list update complete: %d added, %d updated",
            data.get("added", 0),
            data.get("updated", 0),
        )

        return data
