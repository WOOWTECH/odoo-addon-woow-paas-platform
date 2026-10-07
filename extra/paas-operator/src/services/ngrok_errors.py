"""#1559：ngrok sidecar 的失敗原因碼 → 給管理員／平台看的中文說明。

原因碼來自 ``KubernetesService._ngrok_error_code``（封閉集合：``ERR_NGROK_<n>``、
``NO_AUTHTOKEN``、``SERVER_URL_MISMATCH``）。這裡只做「代碼 → 固定文案」，不回任何原始 log。
平台依 ``error_code`` 自己組租戶看的文案（有多語系）；``error`` 是 operator 端的中文備援。
"""
from typing import Optional

_KNOWN = {
    "NO_AUTHTOKEN": "已開啟 ngrok，但還沒有填 ngrok authtoken；請在服務設定填入你自己 ngrok 帳號的 authtoken。",
    "ERR_NGROK_105": "ngrok authtoken 不正確（ERR_NGROK_105）：請到 ngrok 後台 Your Authtoken 頁複製後重新填入服務設定。",
    "ERR_NGROK_107": "ngrok authtoken 無效或已被撤銷（ERR_NGROK_107）：請到 ngrok 後台確認後重新填入服務設定。",
    "ERR_NGROK_108": "這個 ngrok 帳號同時連線的 agent 數已達上限（ERR_NGROK_108）：請停掉其他使用同一個帳號的 ngrok agent，或改用另一個帳號。",
    "ERR_NGROK_334": "這個 ngrok 網域已經在別處上線（ERR_NGROK_334）：同一個網域同時只能給一個 agent 用，請停掉另一邊或改用別的網域。",
    "ERR_NGROK_4018": "ngrok 需要已驗證的帳號與 authtoken（ERR_NGROK_4018）：請確認 ngrok 帳號已完成驗證並填入正確的 authtoken。",
    "SERVER_URL_MISMATCH": "ngrok 已連上，但 Headscale 啟動時還沒等到它，目前仍使用叢集內網址；請重新啟動服務以改用 ngrok 對外網址。",
}


def describe_ngrok_error(code: Optional[str]) -> Optional[str]:
    """原因碼 → 中文說明；未知的 ERR_NGROK_<n> 給通用說明＋官方文件連結。"""
    if not code:
        return None
    if code in _KNOWN:
        return _KNOWN[code]
    if code.startswith("ERR_NGROK_") and code[len("ERR_NGROK_"):].isdigit():
        return "ngrok agent 無法連線（%s），詳見 https://ngrok.com/docs/errors/%s ；請檢查服務設定裡的 ngrok authtoken 與網域。" % (
            code, code.lower())
    return None
