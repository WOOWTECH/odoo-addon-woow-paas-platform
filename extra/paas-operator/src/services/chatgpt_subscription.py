"""LiteLLM「ChatGPT 訂閱」登入：把平台的請求轉成 chart CLI 的一次 ``kubectl exec``。

LiteLLM 的 ``chatgpt/`` provider 用 ChatGPT Plus/Pro 訂閱額度，登入是 OAuth device code
（借用 Codex CLI 的登入身分）。上游把代碼印在 pod log、租戶看不到；woow-paas-charts
``charts/litellm`` 0.2.0 起在 proxy 容器內附一支 CLI：

    python /woow/py/woow_chatgpt.py <status|start|poll|disconnect|add-models|test> [args...]

每個指令只在 stdout 印**一行 JSON**（``ok: true|false``；exit 0＝ok、1＝ok:false、
2＝用法錯），絕不含 token。這裡只做轉接：白名單動作、驗模型名稱、找 pod、exec、
解析那一行 JSON、分類結果。介面契約：DESIGN §1（chart CLI）／§2（本端點）。

紀律：

- **argv 只有指令與模型名**（``Executing: kubectl ...`` 那行 log 會印出 argv，所以
  模型名稱先過白名單 regex）；不用 stdin、不送任何秘密進容器。
- ``pending.user_code``（一次性登入代碼，15 分鐘）要回給平台顯示給租戶，但**不可進
  log**：本模組的 log 只記 action、success、error_code，不記 stdout／stderr／data。
- 回應的 ``data`` 不得帶 token：就算 CLI 出錯印了，``access_token``／``refresh_token``／
  ``id_token``（與 device flow 的中間值）也一律遞迴刪掉。
- 結果分類沿用 ``InitErrorKind`` 的語意（不含 partial）：沒 exec＝``not_executed``；
  CLI 明說失敗＝``failed``；逾時／輸出不可解析＝``unknown``（不知道 CLI 跑到哪）。
"""

import logging
import re
from typing import Any, get_args

from src.models.schemas import ChatGPTSubscriptionAction
from src.services.helm import KubectlException, KubernetesService, validate_label_value
from src.services.odoo_init import _last_json_object

logger = logging.getLogger(__name__)

# woow-paas-charts charts/litellm：proxy Deployment 的 pod label 與主容器名
# （0.1.x 與 0.2.0 相同；0.1.x 只是沒有 CLI 檔 ⇒ unsupported_chart）。
LITELLM_APP_NAME = "litellm"
LITELLM_COMPONENT = "app"
LITELLM_CONTAINER_NAME = "litellm"
# 容器內 PATH 上的 python 即 /app/.venv/bin/python（env 已含 DB 連線與 master key）。
LITELLM_PYTHON = "python"
CHATGPT_CLI_PATH = "/woow/py/woow_chatgpt.py"

# 每個動作的 exec 逾時。test 會真的打一次上游（CLI 自己給 120 s），add-models 逐一
# POST /model/new。預算階梯：exec 逾時 + 找 pod 的餘裕 < 平台 client 逾時
# （DESIGN §3：test 180 s、add-models 90 s、其他 45 s；由 tests 驗算）。
CHATGPT_EXEC_TIMEOUT_SECONDS: dict[str, int] = {
    "status": 30,
    "start": 30,
    "poll": 30,
    "disconnect": 30,
    "add-models": 60,
    "test": 150,
}
# 找 pod 是一次 `kubectl get pods --selector`（實測 1 秒內）。
CHATGPT_POD_LOOKUP_SLACK_SECONDS = 10

#: 暴露給平台的動作白名單＝schema 的 Literal（restore／selfcheck 只給 chart 自己用）。
CHATGPT_ACTIONS: tuple[str, ...] = get_args(ChatGPTSubscriptionAction)

CHATGPT_MAX_MODELS = 12
# fullmatch：`$` 會吃掉結尾的 "\n"，不能只靠 re.match。
_MODEL_NAME_RE = re.compile(r"(chatgpt/)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# CLI 的錯誤碼形狀；test 的 error 是上游訊息（不是碼），不能直接當 error_code。
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

#: 回應 data 裡一律刪掉的鍵（遞迴）。前三個是 DESIGN §2 指定；後三個是 device flow
#: 的中間值，拿到就能換 token，CLI 本來就不印，這裡一併擋。
_SECRET_KEYS = frozenset({
    "access_token", "refresh_token", "id_token",
    "authorization_code", "code_verifier", "device_auth_id",
})


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _is_old_chart(exc: KubectlException) -> bool:
    """0.1.x chart（或關掉 chatgptSubscription）沒有 CLI 檔：python 直接 exit 2、stdout 空。"""
    stderr = exc.stderr or ""
    return "woow_chatgpt.py" in stderr and ("No such file" in stderr or "can't open file" in stderr)


def _result(success: bool, data: dict | None = None, error: str | None = None,
            error_code: str | None = None, error_kind: str | None = None) -> dict:
    return {"success": success, "data": data, "error": error,
            "error_code": error_code, "error_kind": error_kind}


def _not_executed(error_code: str, error: str) -> dict:
    return _result(False, error=error, error_code=error_code, error_kind="not_executed")


class ChatGPTSubscriptionService:
    """LiteLLM ChatGPT 訂閱登入的 operator 端（唯一實作）。

    ``run`` 回傳 ``ChatGPTSubscriptionResponse`` 形狀的 dict；業務失敗不丟例外。
    非預期例外照常往外丟（API 層轉 500）。
    """

    def __init__(self, k8s_service: KubernetesService | None = None):
        self.k8s = k8s_service or KubernetesService()

    # --- public -----------------------------------------------------------

    def run(self, namespace: str, release_name: str, action: str,
            models: list[str] | None = None, model: str | None = None) -> dict:
        result = self._run(namespace, release_name, action, models, model)
        # ⛔ 只記這三樣：data 帶 user_code、email；stdout／stderr 也不記。
        logger.info("ChatGPT subscription %s in %s/%s: success=%s error_code=%s",
                    action if action in CHATGPT_ACTIONS else "<invalid>",
                    namespace, release_name, result["success"], result["error_code"])
        return result

    # --- internal ---------------------------------------------------------

    def _run(self, namespace, release_name, action, models, model) -> dict:
        if action not in CHATGPT_ACTIONS:
            return _not_executed("invalid_action", "unsupported action")

        args, invalid = self._args(action, models, model)
        if invalid is not None:
            return _not_executed("invalid_model", invalid)

        try:
            validate_label_value("app.kubernetes.io/instance", release_name)
            pod_name = self.k8s.get_pod_name(
                namespace,
                f"app.kubernetes.io/instance={release_name},"
                f"app.kubernetes.io/name={LITELLM_APP_NAME},"
                f"app.kubernetes.io/component={LITELLM_COMPONENT}",
            )
        except (KubectlException, ValueError):
            # ValueError＝namespace 不在 operator 管轄／release 名不合法：什麼都沒碰。
            return _not_executed("pod_not_found", "LiteLLM pod not found")

        try:
            completed = self.k8s.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container=LITELLM_CONTAINER_NAME,
                command=[LITELLM_PYTHON, CHATGPT_CLI_PATH, action, *args],
                timeout=CHATGPT_EXEC_TIMEOUT_SECONDS[action],
            )
        except KubectlException as exc:
            if exc.timed_out:
                # 逾時不證明遠端已停 ⇒ 沒有可信輸出。
                return _result(False, error="timed out waiting for the LiteLLM pod",
                               error_code="exec_timeout", error_kind="unknown")
            # CLI 先印 JSON 再 exit 1（ok:false）／exit 2（usage）⇒ kubectl 非 0，
            # 但最後一行就是 CLI 明說的結果。⛔ 不把 exc.stderr／exc.stdout 原文往外送。
            reported = _last_json_object(exc.stdout)
            if reported is None:
                if _is_old_chart(exc):
                    return _not_executed(
                        "unsupported_chart",
                        "this LiteLLM chart has no ChatGPT subscription support (redeploy to 0.2.0+)")
                return _result(False, error="failed to execute the ChatGPT subscription command",
                               error_code="bad_output", error_kind="unknown")
            return self._classify(action, reported)

        reported = _last_json_object(completed.stdout)
        if reported is None:
            return _result(False, error="the ChatGPT subscription command returned no parsable result",
                           error_code="bad_output", error_kind="unknown")
        return self._classify(action, reported)

    @staticmethod
    def _args(action: str, models: list[str] | None, model: str | None) -> tuple[list[str], str | None]:
        """回 (argv 尾段, 錯誤訊息)。只驗該動作用得到的欄位；訊息不回顯名稱。"""
        if action == "add-models":
            names = list(models or [])
            if len(names) > CHATGPT_MAX_MODELS:
                return [], f"at most {CHATGPT_MAX_MODELS} models per request"
            for i, name in enumerate(names):
                if not (isinstance(name, str) and _MODEL_NAME_RE.fullmatch(name)):
                    return [], f"models[{i}] is not a valid model name"
            return names, None
        if action == "test" and model is not None and model != "":
            if not (isinstance(model, str) and _MODEL_NAME_RE.fullmatch(model)):
                return [], "model is not a valid model name"
            return [model], None
        return [], None

    @staticmethod
    def _classify(action: str, reported: dict) -> dict:
        ok = reported.get("ok")
        data = _scrub({k: v for k, v in reported.items() if k != "ok"})
        if ok is True:
            return _result(True, data=data)
        if ok is not False:
            return _result(False, error="the ChatGPT subscription command returned no parsable result",
                           error_code="bad_output", error_kind="unknown")
        raw = reported.get("error")
        if isinstance(raw, str) and _ERROR_CODE_RE.fullmatch(raw):
            code = raw
        elif action == "add-models" and reported.get("failed"):
            code = "models_failed"       # ok:false、failed 非空、沒有 error 鍵
        elif action == "test":
            code = "test_failed"         # error 是上游訊息（例：401 的提示），原文在 data／error
        else:
            code = "command_failed"
        message = raw if isinstance(raw, str) and raw else code
        return _result(False, data=data, error=message[:500], error_code=code, error_kind="failed")
