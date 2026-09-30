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
from pathlib import Path

from src.models.schemas import InitErrorKind
from src.services.helm import KubectlException, KubernetesService
from src.services.odoo_chart_contract import (
    ODOO_CONTAINER_NAME,
    ODOO_PYTHON,
    odoo_pod_selectors,
    validate_fallback_pod,
)

logger = logging.getLogger(__name__)

# Retry configuration for readiness polling
MAX_READY_RETRIES = 30
READY_RETRY_DELAY = 5  # seconds (total wait: 150s)

# Timeout for the module list update operation
MODULE_UPDATE_TIMEOUT = 120  # seconds

# 容器內執行的 module list 更新腳本。原始碼放在獨立檔案（而非本檔的字串常數）
# 有兩個理由：一是它就是普通 Python、看得懂也 lint 得到；二是測試可以拿到
# 「與 kubectl exec 送進去的完全同一份 bytes」在假 odoo 環境下執行。
# import 時就讀進來——檔案不見要在 operator 啟動當下就炸，不要等到某個租戶
# 部署到一半才發現。
UPDATE_MODULE_LIST_SCRIPT = (
    Path(__file__).resolve().parent / "odoo_scripts" / "update_module_list.py"
).read_text(encoding="utf-8")


def _last_json_object(output: str) -> dict | None:
    """腳本輸出的最後一個非空行若是 JSON object 就回傳它，否則 None。

    Odoo 的 logging 可能先印到 stdout，腳本的 JSON 回報固定是最後一行。
    """
    lines = [line for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class OdooInitError(Exception):
    """Raised when Odoo initialization fails.

    ``kind``（#1263）：失敗分類，值域見 ``schemas.InitErrorKind``。預設 ``unknown``
    ——新增的 raise 點沒想清楚副作用時，寧可讓平台保守處理，也不要誤報成可重送。
    """

    def __init__(self, message: str, step: str = "", kind: InitErrorKind = "unknown"):
        self.step = step
        self.kind = kind
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
        container = ODOO_CONTAINER_NAME
        logger.info(
            "Found Odoo pod: %s (container: %s)", pod_name, container
        )

        # Step 2: Wait for Odoo to be ready
        self._wait_for_ready(namespace, pod_name, container)

        # Step 3: Update module list
        result = self._update_module_list(namespace, pod_name, container)

        logger.info(
            "Odoo module list initialization complete for %s/%s: "
            "added=%d, updated=%d, stale_cleaned=%d",
            namespace,
            release_name,
            result.get("added", 0),
            result.get("updated", 0),
            result.get("cleaned", 0),
        )

        return {
            "success": True,
            "modules_added": result.get("added", 0),
            "modules_updated": result.get("updated", 0),
            "modules_cleaned": result.get("cleaned", 0),
            "message": "Module list updated successfully",
        }

    def _get_odoo_pod(self, namespace: str, release_name: str) -> str:
        """Find the Odoo pod name by label selectors.

        Tries a specific selector first (instance + component=server), then
        falls back to instance-only discovery with metadata validation.

        Args:
            namespace: Kubernetes namespace.
            release_name: Helm release name.

        Returns:
            The pod name string.

        Raises:
            OdooInitError: If no matching pod is found.
        """
        selectors = odoo_pod_selectors(release_name)

        for index, selector in enumerate(selectors):
            try:
                pod_name = self.k8s.get_pod_name(namespace, selector)
            except KubectlException:
                logger.debug(
                    "No pod found with selector '%s', trying next", selector
                )
                continue

            if index == 1:
                # 只驗 fallback 的首個候選，不擴成跨 workload 的搜尋框架。
                try:
                    pod = self.k8s.get_pod(namespace, pod_name)
                    validate_fallback_pod(pod, namespace, release_name, pod_name)
                except (KubectlException, ValueError) as exc:
                    raise OdooInitError(
                        f"Cannot verify fallback Odoo pod {pod_name}: {exc}",
                        step="find_pod",
                        kind="not_executed",
                    ) from exc
            logger.debug("Found pod '%s' with selector '%s'", pod_name, selector)
            return pod_name

        raise OdooInitError(
            f"Cannot find Odoo pod for release {release_name} in {namespace}",
            step="find_pod",
            kind="not_executed",
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

        command = [ODOO_PYTHON, "-c", check_script]

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

        # readiness 探測只打 XML-RPC version，唯讀 ⇒ 還沒動到租戶 DB。
        raise OdooInitError(
            f"Odoo not ready after {MAX_READY_RETRIES * READY_RETRY_DELAY}s",
            step="wait_ready",
            kind="not_executed",
        )

    def _update_module_list(
        self, namespace: str, pod_name: str, container: str
    ) -> dict:
        """Execute module list update and stale record cleanup via direct ORM.

        Runs a Python script inside the Odoo container that:
        1. Parses odoo.conf to get the database name
        2. Opens a registry cursor as SUPERUSER_ID
        3. Calls ir.module.module.update_list()
        4. Removes stale module records — **自管 addon 專屬**：state=uninstalled、
           名字在 /all-addons/catalog.json 裡、而且已經不在磁碟上
        5. Commits the transaction

        Odoo's update_list() never deletes module records for addons that are
        no longer on the filesystem (known behavior since Odoo 8, see
        github.com/odoo/odoo/issues/1677). Step 4 cleans up these stale
        records so deselected addons disappear from the Apps list.

        Only records with state='uninstalled' are removed (safe — no DB
        artifacts). Installed modules are left alone for the pre-check
        uninstall flow to handle.

        ⛔ 第 4 步的刪除候選集必須是自管 addon 清單的子集（#748）——沒有這層
        域限制時它會把 Odoo core 的 record 整批刪掉。理由與 fail-safe 行為寫在
        腳本本體：``src/services/odoo_scripts/update_module_list.py``。

        Args:
            namespace: Kubernetes namespace.
            pod_name: Name of the Odoo pod.
            container: Name of the Odoo container.

        Returns:
            Dict with keys: success, added, updated, cleaned.

        Raises:
            OdooInitError: If the module list update fails.
        """
        logger.info("Updating Odoo module list via direct ORM access...")

        command = [
            ODOO_PYTHON,
            "-c",
            UPDATE_MODULE_LIST_SCRIPT,
        ]

        try:
            result = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=container,
                command=command,
                timeout=MODULE_UPDATE_TIMEOUT,
            )
        except KubectlException as e:
            # 腳本失敗時先印 JSON 回報再 exit 1 ⇒ kubectl exec 非 0，但 stdout 最後一行
            # 就是腳本明說的失敗 ⇒ failed。timeout 沒有可信輸出、其他 exec 失敗分不出
            # 容器內 ORM 跑到哪 ⇒ 維持預設 unknown（#1263）。
            reported = None if e.timed_out else _last_json_object(e.stdout)
            if reported is not None and reported.get("success") is False:
                raise OdooInitError(
                    f"Module list update failed: {reported.get('error', 'unknown error')}",
                    step="update_module_list",
                    kind="failed",
                ) from e
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

        # 上面的 exec 失敗（含 timeout）／空輸出／JSON 無效都不能證明容器內的 ORM
        # 程序沒跑或已停，維持預設 unknown；只有腳本自己回報失敗才是確定失敗。
        if not data.get("success"):
            raise OdooInitError(
                f"Module list update failed: {data.get('error', 'unknown error')}",
                step="update_module_list",
                kind="failed",
            )

        logger.info(
            "Module list update complete: %d added, %d updated, %d stale cleaned",
            data.get("added", 0),
            data.get("updated", 0),
            data.get("cleaned", 0),
        )

        # #748 fail-safe：自管 addon 清單或磁碟探針拿不到時，腳本會跳過整段
        # cleanup 而不是退化成大範圍刪除。跳過本身是安全的，但必須留痕——
        # 否則「Apps 清單一直有殘影」會查不出是這裡靜默停工造成的。
        skipped = data.get("cleanup_skipped")
        if skipped:
            logger.warning(
                "Stale module record cleanup skipped for %s/%s: %s",
                namespace,
                pod_name,
                skipped,
            )

        return data
