"""租戶 app 自管的管理員密碼重設（#1370／#1186）：Odoo 的 base.user_admin、n8n 的 owner、
Omnigent 的 admin（#1370 後續）。

這兩種 app 的密碼權威在 app 自己的 DB（不是 chart Secret、也不是 PVC 上的
config.json），所以既有的 reset-config（helm upgrade／exec patch config.json）
動不到它們。這裡走「exec 一支腳本進 app 容器、直接改 DB」：

- Odoo：``odoo_scripts/reset_admin_password.py``（ORM，同交易清 2FA）
- n8n：``n8n_scripts/reset_owner_password.js``（bcryptjs hash ＋ 單列 UPDATE，同交易關 MFA）
- Omnigent：``omnigent_scripts/reset_admin_password.py``（omnigent 自己的 AccountStore＋
  hash_password，與 Members → Reset 同一條寫入；不動 account_generation ⇒ 與上游 admin
  reset 一樣，已登入的 session 與已配對的 host 不受影響）

共同紀律：

- **新密碼只走 stdin**（``exec_in_pod(..., input_data=...)``）——argv 會出現在
  operator 的 ``Executing: kubectl ...`` log 與 KubectlException.command，stdin 不會。
- 不 helm upgrade、不 rollout restart：兩個 app 每次登入都讀 DB，改完立刻生效。
- 結果分類沿用 ``InitErrorKind``：找不到 pod＝``not_executed``（DB 沒被碰）；
  腳本自己回報失敗＝``failed``（交易已 rollback，密碼未變）；逾時／輸出不可解析
  ＝``unknown``（不知道 commit 了沒有，平台不得宣稱「密碼未變」）。
"""

import logging
from pathlib import Path

from src.models.schemas import InitErrorKind
from src.services.helm import KubectlException, KubernetesService
from src.services.n8n_init import N8nInitError, N8nInitService
from src.services.odoo_chart_contract import ODOO_CONTAINER_NAME, ODOO_PYTHON
from src.services.odoo_init import OdooInitError, OdooInitService, _last_json_object

logger = logging.getLogger(__name__)

# 預算階梯（repo-lint `app-admin-reset-budget-ladder` 驗算）：
#   exec 逾時 + 找 pod 的 kubectl 餘裕 < 平台 client 逾時（APP_ADMIN_RESET_TIMEOUT）
#   < 前端 RESET_REQUEST_TIMEOUT_MS。
# Odoo 要在容器內冷啟動 registry（裝了 sale／account 的 DB 實測十多秒），n8n 只是
# bcrypt ＋ 一次 UPDATE（實測約 1 秒）。
ODOO_RESET_EXEC_TIMEOUT_SECONDS = 45
N8N_RESET_EXEC_TIMEOUT_SECONDS = 30
# Omnigent：冷 import omnigent＋一次 scrypt/argon hash＋SQLite 寫入，沙盒實測約 5.5 秒。
OMNIGENT_RESET_EXEC_TIMEOUT_SECONDS = 30
# 找 pod 最多三次 kubectl get（selector 兩次＋fallback 驗證一次）的餘裕。
APP_ADMIN_RESET_LOOKUP_SLACK_SECONDS = 15

N8N_CONTAINER_NAME = "n8n"
# woow-paas-charts omnigent：server Deployment 的主容器（另有 runner-gc sidecar）。
OMNIGENT_CONTAINER_NAME = "server"
# omnigent server image 的 venv python（PATH 上的 python 即此；寫絕對路徑免受 PATH 影響）。
OMNIGENT_PYTHON = "/opt/venv/bin/python"

_SCRIPTS = Path(__file__).resolve().parent
# import 時就讀——檔案不見要在 operator 啟動當下就炸，不要等到租戶按 Reset 才發現。
ODOO_RESET_ADMIN_SCRIPT = (_SCRIPTS / "odoo_scripts" / "reset_admin_password.py").read_text(encoding="utf-8")
N8N_RESET_OWNER_SCRIPT = (_SCRIPTS / "n8n_scripts" / "reset_owner_password.js").read_text(encoding="utf-8")
OMNIGENT_RESET_ADMIN_SCRIPT = (
    _SCRIPTS / "omnigent_scripts" / "reset_admin_password.py"
).read_text(encoding="utf-8")


class AppAdminResetError(Exception):
    """重設失敗。``kind`` 值域見 ``schemas.InitErrorKind``；``code`` 是腳本回報的原因碼。

    ⛔ message 一律不得含新密碼——呼叫端會把它回給平台、寫進 log。
    """

    def __init__(self, message: str, step: str, kind: InitErrorKind = "unknown", code: str | None = None):
        self.step = step
        self.kind = kind
        self.code = code
        super().__init__(message)


class AppAdminPasswordService:
    """Odoo／n8n／Omnigent 管理員密碼重設（operator 端唯一實作）。"""

    def __init__(self, k8s_service: KubernetesService | None = None):
        self.k8s = k8s_service or KubernetesService()
        self._odoo = OdooInitService(k8s_service=self.k8s)
        self._n8n = N8nInitService(k8s_service=self.k8s)

    # --- public -----------------------------------------------------------

    def reset_odoo_admin(self, namespace: str, release_name: str, new_password: str) -> dict:
        try:
            # pod 定位沿用 init/odoo 的 selector＋fallback 驗證（#1129 契約）。
            pod_name = self._odoo._get_odoo_pod(namespace, release_name)
        except (OdooInitError, ValueError) as exc:
            # ValueError＝namespace 不在 operator 管轄（validate_namespace）：什麼都沒碰。
            raise AppAdminResetError(str(exc), step="find_pod", kind="not_executed") from exc
        return self._run(
            namespace, pod_name, ODOO_CONTAINER_NAME,
            [ODOO_PYTHON, "-c", ODOO_RESET_ADMIN_SCRIPT],
            new_password, ODOO_RESET_EXEC_TIMEOUT_SECONDS, app="odoo",
        )

    def reset_n8n_owner(self, namespace: str, release_name: str, new_password: str) -> dict:
        try:
            pod_name = self._n8n._get_n8n_pod(namespace, release_name)
        except (N8nInitError, ValueError) as exc:
            raise AppAdminResetError(str(exc), step="find_pod", kind="not_executed") from exc
        return self._run(
            namespace, pod_name, N8N_CONTAINER_NAME,
            ["node", "-e", N8N_RESET_OWNER_SCRIPT],
            new_password, N8N_RESET_EXEC_TIMEOUT_SECONDS, app="n8n",
        )

    def reset_omnigent_admin(self, namespace: str, release_name: str, new_password: str) -> dict:
        # server pod 才有 DATABASE_URL；runner Job 的 pod 不帶 instance label，不會被選到。
        selector = (f"app.kubernetes.io/instance={release_name},"
                    f"app.kubernetes.io/name=omnigent,app.kubernetes.io/component=server")
        try:
            pod_name = self.k8s.get_pod_name(namespace, selector)
        except (KubectlException, ValueError) as exc:
            raise AppAdminResetError(
                f"Cannot find the Omnigent server pod for release {release_name} in {namespace}",
                step="find_pod", kind="not_executed",
            ) from exc
        return self._run(
            namespace, pod_name, OMNIGENT_CONTAINER_NAME,
            [OMNIGENT_PYTHON, "-c", OMNIGENT_RESET_ADMIN_SCRIPT],
            new_password, OMNIGENT_RESET_EXEC_TIMEOUT_SECONDS, app="omnigent",
        )

    # --- internal ---------------------------------------------------------

    def _run(self, namespace, pod_name, container, command, new_password, timeout, app):
        logger.info("Resetting %s admin password in %s/%s", app, namespace, pod_name)
        try:
            result = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=container,
                command=command,
                timeout=timeout,
                input_data=new_password,
            )
        except KubectlException as exc:
            # 腳本失敗時先印 JSON 再 exit 1 ⇒ kubectl 非 0，但最後一行就是腳本明說的
            # 失敗（交易已 rollback）⇒ failed。逾時沒有可信輸出、其他 exec 失敗分不出
            # 腳本跑到哪 ⇒ unknown。⛔ 不把 e.stderr／e.stdout 原文往外送。
            reported = None if exc.timed_out else _last_json_object(exc.stdout)
            if reported is not None and reported.get("success") is False:
                raise self._script_failure(reported, app) from exc
            logger.error("%s admin password reset exec failed in %s/%s (timed_out=%s)",
                         app, namespace, pod_name, exc.timed_out)
            raise AppAdminResetError(
                "timed out waiting for the reset script" if exc.timed_out
                else "failed to execute the reset script",
                step="exec", kind="unknown",
            ) from exc

        reported = _last_json_object(result.stdout)
        if reported is None:
            raise AppAdminResetError("reset script returned no parsable result", step="exec", kind="unknown")
        if reported.get("success") is not True:
            raise self._script_failure(reported, app)
        logger.info("%s admin password reset done in %s/%s (second factor cleared: %s)",
                    app, namespace, pod_name, bool(reported.get("second_factor_cleared")))
        return {
            "success": True,
            "login": reported.get("login"),
            "second_factor_cleared": bool(reported.get("second_factor_cleared")),
        }

    @staticmethod
    def _script_failure(reported: dict, app: str) -> AppAdminResetError:
        code = reported.get("code")
        error = reported.get("error") or "reset script reported a failure"
        logger.warning("%s admin password reset refused by script: code=%s error=%s", app, code, error)
        return AppAdminResetError(str(error), step="reset", kind="failed", code=code if isinstance(code, str) else None)

