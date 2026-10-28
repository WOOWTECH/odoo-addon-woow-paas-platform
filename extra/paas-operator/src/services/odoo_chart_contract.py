"""woow-odoo 主容器的 exec 契約；注入租戶的 script 不可 import 本模組。"""

ODOO_CONTAINER_NAME = "odoo"
# 正式 image 透過 PATH launcher 選共同 venv，不綁 image 內部路徑。
ODOO_PYTHON = "python3"
ODOO_SERVER_LABEL = "app.kubernetes.io/component=server"
# chart 掛 conf Secret 的位置（含 db_*）；注入租戶的 script 各自寫同值常數。
ODOO_CONF_PATH = "/etc/odoo/odoo.conf"
# chart 的 conf Secret（``<release>-odoo-conf``，woow-odoo templates/secrets.yaml）。tenant profile 另帶
# 初始管理員密碼（鍵 ``admin-password``；platform profile 沒有這一格）。它只在首次 ``-i base`` 套用，
# 之後密碼的權威在 app 自己的 DB；平台重設密碼後要把這一格對齊到目前的密碼，免得維運讀到過期憑證。
ODOO_CONF_SECRET_SUFFIX = "-odoo-conf"
ODOO_ADMIN_PASSWORD_SECRET_KEY = "admin-password"


def odoo_pod_selectors(release_name: str) -> tuple[str, str]:
    """先定位 server，再保留非標準 chart 的 instance-only discovery。"""
    instance = f"app.kubernetes.io/instance={release_name}"
    return f"{instance},{ODOO_SERVER_LABEL}", instance


def validate_fallback_pod(
    pod: dict, namespace: str, release_name: str, pod_name: str,
) -> None:
    """instance-only 只負責發現；未確認為 Odoo web 的候選不可 exec。"""
    if not isinstance(pod, dict):
        raise ValueError("Missing Pod metadata")
    metadata = pod.get("metadata")
    spec = pod.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ValueError("Missing Pod metadata or spec")
    labels = metadata.get("labels")
    if (
        metadata.get("namespace") != namespace
        or metadata.get("name") != pod_name
        or not isinstance(labels, dict)
        or labels.get("app.kubernetes.io/instance") != release_name
    ):
        raise ValueError("Pod identity does not match namespace/release/name")
    # 缺 label 的 legacy web 可相容；明示其他角色則不能用 readiness 冒充 web。
    for key, expected in (
        ("app.kubernetes.io/component", "server"),
        ("app.kubernetes.io/name", "odoo"),
    ):
        if key in labels and labels[key] != expected:
            raise ValueError(f"Pod is not an Odoo server: {key}")
    owners = metadata.get("ownerReferences", [])
    if (
        "job-name" in labels
        or "batch.kubernetes.io/job-name" in labels
        or not isinstance(owners, list)
        or any(not isinstance(owner, dict) or owner.get("kind") in (None, "Job") for owner in owners)
    ):
        raise ValueError("Pod is a Job or has unverifiable ownership")
    containers = spec.get("containers")
    if not isinstance(containers, list) or not any(
        isinstance(container, dict) and container.get("name") == ODOO_CONTAINER_NAME
        for container in containers
    ):
        raise ValueError("Pod has no odoo container")
