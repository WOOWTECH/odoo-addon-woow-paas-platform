"""租戶 app 自管的管理員密碼重設（#1370／#1186）：Odoo 的 base.user_admin、n8n 的 owner、
Omnigent 的 admin（#1370 後續）、EMQX 的 Dashboard admin（#1774）。

這兩種 app 的密碼權威在 app 自己的 DB（不是 chart Secret、也不是 PVC 上的
config.json），所以既有的 reset-config（helm upgrade／exec patch config.json）
動不到它們。這裡走「exec 一支腳本進 app 容器、直接改 DB」：

- Odoo：``odoo_scripts/reset_admin_password.py``（ORM，同交易清 2FA；commit 後送 cache
  signaling，讓常駐 worker 的 session／密碼快取失效，#1485）
- n8n：``n8n_scripts/reset_owner_password.js``（bcryptjs hash ＋ 單列 UPDATE，同交易關 MFA）
- Omnigent：``omnigent_scripts/reset_admin_password.py``（omnigent 自己的 AccountStore＋
  hash_password，與 Members → Reset 同一條寫入；不動 account_generation ⇒ 與上游 admin
  reset 一樣，已登入的 session 與已配對的 host 不受影響）
- EMQX：``emqx_scripts/reset_admin_password.sh``（``emqx ctl admins passwd``，與 Dashboard
  「修改密碼」同一個 change_password：EMQX 自己檢查密碼規則、寫 mnesia、撤銷該帳號的
  Dashboard token ⇒ 已登入的 Dashboard 被登出；CE 沒有 2FA）。``emqx ctl`` 只收 argv，所以
  腳本從 stdin 讀密碼後在容器內自己帶進 argv——operator 的 kubectl 指令列與 log 仍然看不到。

共同紀律：

- **新密碼只走 stdin**（``exec_in_pod(..., input_data=...)``）——argv 會出現在
  operator 的 ``Executing: kubectl ...`` log 與 KubectlException.command，stdin 不會。
- 不 helm upgrade、不 rollout restart：兩個 app 每次登入都讀 DB，改完立刻生效。
- 結果分類沿用 ``InitErrorKind``：找不到 pod＝``not_executed``（DB 沒被碰）；
  腳本自己回報失敗＝``failed``（交易已 rollback，密碼未變）——例外是 commit 之後才
  失敗的 ``SCRIPT_CODES_AFTER_COMMIT``（#1485）＝``unknown``；逾時／輸出不可解析
  ＝``unknown``（不知道 commit 了沒有，平台不得宣稱「密碼未變」）。
"""

import base64
import logging
from pathlib import Path

from src.models.schemas import InitErrorKind
from src.services.helm import KubectlException, KubernetesService, validate_label_value
from src.services.n8n_init import N8nInitError, N8nInitService
from src.services.odoo_chart_contract import (
    ODOO_ADMIN_PASSWORD_SECRET_KEY,
    ODOO_CONF_SECRET_SUFFIX,
    ODOO_CONTAINER_NAME,
    ODOO_PYTHON,
)
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
# EMQX：emqx ctl 走 nodetool 連本機 broker，平台釘的 5.8.9 沙盒實測約 3 秒。
EMQX_RESET_EXEC_TIMEOUT_SECONDS = 30
# 找 pod 最多三次 kubectl get（selector 兩次＋fallback 驗證一次）的餘裕。
APP_ADMIN_RESET_LOOKUP_SLACK_SECONDS = 15

N8N_CONTAINER_NAME = "n8n"
# woow-paas-charts omnigent：server Deployment 的主容器（另有 runner-gc sidecar）。
OMNIGENT_CONTAINER_NAME = "server"
# omnigent server image 的 venv python（PATH 上的 python 即此；寫絕對路徑免受 PATH 影響）。
OMNIGENT_PYTHON = "/opt/venv/bin/python"
# woow-paas-charts emqx：StatefulSet 的 broker 容器（另有 entrance／ngrok sidecar）。
EMQX_CONTAINER_NAME = "emqx"

# 腳本在 commit **之後**才回報的失敗碼：密碼可能已變 ⇒ 不可歸類成 failed。
SCRIPT_CODES_AFTER_COMMIT = frozenset({"sessions_not_revoked"})

_SCRIPTS = Path(__file__).resolve().parent
# import 時就讀——檔案不見要在 operator 啟動當下就炸，不要等到租戶按 Reset 才發現。
ODOO_RESET_ADMIN_SCRIPT = (_SCRIPTS / "odoo_scripts" / "reset_admin_password.py").read_text(encoding="utf-8")
N8N_RESET_OWNER_SCRIPT = (_SCRIPTS / "n8n_scripts" / "reset_owner_password.js").read_text(encoding="utf-8")
OMNIGENT_RESET_ADMIN_SCRIPT = (
    _SCRIPTS / "omnigent_scripts" / "reset_admin_password.py"
).read_text(encoding="utf-8")
EMQX_RESET_ADMIN_SCRIPT = (_SCRIPTS / "emqx_scripts" / "reset_admin_password.sh").read_text(encoding="utf-8")


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
    """Odoo／n8n／Omnigent／EMQX 管理員密碼重設（operator 端唯一實作）。"""

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
        result = self._run(
            namespace, pod_name, ODOO_CONTAINER_NAME,
            [ODOO_PYTHON, "-c", ODOO_RESET_ADMIN_SCRIPT],
            new_password, ODOO_RESET_EXEC_TIMEOUT_SECONDS, app="odoo",
        )
        # 密碼已在 app DB commit（上面沒 raise）——chart conf Secret 裡的初始密碼對齊到新值。
        # best-effort：同步失敗不得把「已生效」的重設翻成失敗（平台據 secret_sync 決定要不要重試）。
        result["secret_sync"] = self.sync_odoo_admin_secret(namespace, release_name, new_password)
        return result

    def sync_odoo_admin_secret(self, namespace: str, release_name: str, password: str) -> str:
        """把 ``<release>-odoo-conf`` 的 ``admin-password`` 對齊到目前實際的 Odoo 管理員密碼。

        Odoo 的密碼權威在 app DB，chart Secret 的這一格只是「首次 ``-i base`` 的初始密碼」；
        重設（或回復版本把 Secret 還原成舊 revision 的值）之後它會與實際不符。平台下一次
        helm upgrade 本來就會重送目前的密碼，這裡把空窗收掉——維運讀 Secret 永遠拿到目前這把。

        回傳 ``synced``／``not_applicable``（沒有這個 Secret 或這一格，例如 platform profile）／
        ``failed``。**永不 raise、永不把密碼寫進 log**；只動 ``data.admin-password`` 一格（merge
        patch 保留 Helm 的 ownership 標記，其他鍵與 ``odoo.conf`` 不碰）。
        """
        secret_name = f"{release_name}{ODOO_CONF_SECRET_SUFFIX}"
        try:
            validate_label_value("release", release_name)
            items = self.k8s.get_items("secret", namespace, f"app.kubernetes.io/instance={release_name}")
        except (KubectlException, ValueError) as exc:
            logger.warning("Odoo admin Secret sync could not read secrets in %s for %s (%s)",
                           namespace, release_name, type(exc).__name__)
            return "failed"
        target = next((item for item in items
                       if isinstance(item, dict) and (item.get("metadata") or {}).get("name") == secret_name), None)
        if target is None or ODOO_ADMIN_PASSWORD_SECRET_KEY not in (target.get("data") or {}):
            return "not_applicable"
        try:
            self.k8s.patch_secret(
                namespace, secret_name,
                {ODOO_ADMIN_PASSWORD_SECRET_KEY: base64.b64encode(password.encode("utf-8")).decode("ascii")},
            )
        except (KubectlException, ValueError) as exc:
            logger.warning("Odoo admin Secret sync failed for %s/%s (%s)",
                           namespace, secret_name, type(exc).__name__)
            return "failed"
        logger.info("Odoo admin Secret %s/%s aligned with the live admin password", namespace, secret_name)
        return "synced"

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

    def reset_emqx_admin(self, namespace: str, release_name: str, new_password: str) -> dict:
        # charts/emqx 的 StatefulSet pod（emqx.labels：name=emqx、component=broker）。
        selector = (f"app.kubernetes.io/instance={release_name},"
                    f"app.kubernetes.io/name=emqx,app.kubernetes.io/component=broker")
        try:
            pod_name = self.k8s.get_pod_name(namespace, selector)
        except (KubectlException, ValueError) as exc:
            raise AppAdminResetError(
                f"Cannot find the EMQX broker pod for release {release_name} in {namespace}",
                step="find_pod", kind="not_executed",
            ) from exc
        return self._run(
            namespace, pod_name, EMQX_CONTAINER_NAME,
            ["sh", "-c", EMQX_RESET_ADMIN_SCRIPT],
            new_password, EMQX_RESET_EXEC_TIMEOUT_SECONDS, app="emqx",
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
        # 密碼已 commit、只差通知其他 worker（#1485）：不是「交易已 rollback」的確定失敗，
        # 平台不得宣稱密碼未變 ⇒ unknown（租戶重按一次即補送訊號）。
        kind: InitErrorKind = "unknown" if code in SCRIPT_CODES_AFTER_COMMIT else "failed"
        return AppAdminResetError(str(error), step="reset", kind=kind, code=code if isinstance(code, str) else None)

