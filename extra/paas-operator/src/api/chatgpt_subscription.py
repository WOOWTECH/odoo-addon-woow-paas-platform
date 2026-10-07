"""LiteLLM「ChatGPT 訂閱」登入端點（device code 經 ``kubectl exec`` 進 proxy 容器）。

平台服務頁的「ChatGPT 訂閱」卡片 → 平台 ``cloud.service._chatgpt_subscription`` →
本端點 → ``ChatGPTSubscriptionService``（``src/services/chatgpt_subscription.py``）→
chart CLI ``python /woow/py/woow_chatgpt.py <action>``。

回應慣例比照 init／admin-password：業務失敗回 200＋``success=false``＋``error_kind``；
namespace 不在租戶前綴回 403；非預期例外回 500＋``detail.error_kind=unknown``（不帶
stderr、不帶 data）。⛔ ``data.pending.user_code`` 不得進 log。
"""
import logging

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import (
    ChatGPTSubscriptionAction,
    ChatGPTSubscriptionRequest,
    ChatGPTSubscriptionResponse,
)
from src.services.chatgpt_subscription import ChatGPTSubscriptionService
from src.services.helm import KubernetesService, validate_namespace

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/releases", tags=["chatgpt-subscription"])

chatgpt_subscription_service = ChatGPTSubscriptionService(k8s_service=KubernetesService())


@router.post(
    "/{namespace}/{release_name}/chatgpt-subscription/{action}",
    response_model=ChatGPTSubscriptionResponse,
    summary="ChatGPT subscription sign-in for a LiteLLM release",
    description="Run one command of the litellm chart's ChatGPT subscription CLI (chart >= 0.2.0) "
                "in the proxy container: status, start (device code), poll, disconnect, add-models, "
                "test. Business failures are 200 with success=false and error_code/error_kind; "
                "an older chart answers error_code=unsupported_chart. Tokens never leave the pod.",
)
def chatgpt_subscription(
    namespace: str,
    release_name: str,
    action: ChatGPTSubscriptionAction,
    request: ChatGPTSubscriptionRequest | None = None,
):
    try:
        validate_namespace(namespace)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    body = request or ChatGPTSubscriptionRequest()
    try:
        result = chatgpt_subscription_service.run(
            namespace=namespace,
            release_name=release_name,
            action=action,
            models=body.models,
            model=body.model,
        )
    except Exception as e:
        # 只記例外類別：訊息可能夾帶 CLI 輸出（含 user_code）。
        logger.error("Unexpected %s during ChatGPT subscription %s in %s/%s",
                     type(e).__name__, action, namespace, release_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": f"ChatGPT subscription {action} failed: {type(e).__name__}",
                    "error_step": None, "error_kind": "unknown"},
        )
    return ChatGPTSubscriptionResponse(**result)
