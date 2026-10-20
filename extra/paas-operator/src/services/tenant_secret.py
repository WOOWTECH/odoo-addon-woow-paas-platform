"""INFRA-FIX-019-1：租戶憑證放在 helm release **之外**的 Secret。

helm 把每個 revision 的 user values 與渲染後的 manifest 存在
``sh.helm.release.v1.<release>.v<N>`` Secret（helm 自己的 release 紀錄）。租戶的
ngrok authtoken（headscale／emqx 的 ``ngrok.authtoken``）與 Tailscale auth key
（``tailscale.authKey``）原本走 values、再被 chart 渲染進自己的 Secret ⇒ 租戶清除或
換掉之後，舊值仍完整留在每個 superseded revision（Headscale 238：v19／v21／v22／v25）。

修法：平台送 ``tenant_secret = {name, data}``，values 只帶 chart 的
``tenantSecret.{name, revision, nonEmptyKeys}``（woow-paas-charts headscale 0.1.7、
tailscale 0.1.8、emqx 0.1.11）。operator 在 helm **之前**把 data 寫進
``<release>-tenant-secret``：

* **server-side apply**：client-side ``kubectl apply`` 會把整份 manifest（含 data）存進
  ``kubectl.kubernetes.io/last-applied-configuration`` annotation——等於再存一份明文。
  SSA 只記 managedFields（欄位名，不含值）。
* manifest 走 **stdin**：不落暫存檔、不上 argv（``_redact_cmd`` 只是第二道）。
* ``data``（base64）而非 ``stringData``：SSA 對 stringData 沒有欄位所有權，舊鍵刪不掉。
* 名稱只接受 ``<release>-tenant-secret``：這條路不能被拿來覆寫 chart 自己的 Secret、helm
  的 release Secret 或同 namespace 其他服務的東西。
* 不帶 Helm 的 managed-by／meta.helm.sh 標記：helm 不會認領、也不會在 uninstall 時處理它，
  所以 ``DELETE /api/releases`` 另外刪。

已經寫進舊 revision 的值這裡清不掉（helm 歷史只會被新 revision 擠出 history-max）：撤銷後
仍要到 ngrok 後台輪換 token／到 Tailscale、Headplane 撤銷 key——chart values 與平台說明都寫明。
"""
import base64
import re
from typing import Any, Dict

TENANT_SECRET_SUFFIX = "-tenant-secret"
#: server-side apply 的 field manager（managedFields 只記欄位名，不記值）。
TENANT_SECRET_FIELD_MANAGER = "paas-operator-tenant-secret"
TENANT_SECRET_LABEL = "paas.woowtech.io/tenant-secret"

#: Secret data 的鍵：Kubernetes 規則（``[-._a-zA-Z0-9]+``、≤ 253）。
_KEY_RE = re.compile(r"[-._A-Za-z0-9]{1,253}")
_MAX_KEYS = 16
#: 一把 token／key 遠小於此；上限只為擋掉錯誤或惡意的大 payload（Secret 總上限 1 MiB）。
_MAX_VALUE_BYTES = 64 * 1024


def tenant_secret_name(release: str) -> str:
    """``<release>-tenant-secret``——平台與 chart 的約定，也是本端點唯一接受的名稱。"""
    return f"{release}{TENANT_SECRET_SUFFIX}"


def validate_tenant_secret(release: str, spec: Any) -> Dict[str, str]:
    """檢查 ``TenantSecretSpec``；不合格 raise ``ValueError``（訊息不含任何值）。回傳 data。"""
    if spec.name != tenant_secret_name(release):
        raise ValueError(
            "tenant_secret.name must be %s (the release's own tenant Secret)"
            % tenant_secret_name(release)
        )
    data = spec.data
    if not isinstance(data, dict) or len(data) > _MAX_KEYS:
        raise ValueError("tenant_secret.data must be an object with at most %d keys" % _MAX_KEYS)
    for key, value in data.items():
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise ValueError("tenant_secret.data has an invalid key")
        if not isinstance(value, str):
            raise ValueError("tenant_secret.data values must be strings")
        if len(value.encode("utf-8")) > _MAX_VALUE_BYTES:
            raise ValueError("tenant_secret.data value is too large")
    return dict(data)


def build_tenant_secret_manifest(namespace: str, release: str, data: Dict[str, str]) -> Dict[str, Any]:
    """``<release>-tenant-secret`` 的 manifest（``data`` base64、不帶 helm 標記）。"""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": tenant_secret_name(release),
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/instance": release,
                "app.kubernetes.io/managed-by": "paas-operator",
                TENANT_SECRET_LABEL: "true",
            },
        },
        "type": "Opaque",
        "data": {
            key: base64.b64encode(value.encode("utf-8")).decode("ascii")
            for key, value in data.items()
        },
    }
