"""共用 integration 測試 namespace 前綴；不改 operator 的部署設定預設。"""

import os
import re

DEFAULT_TEST_NAMESPACE_PREFIX = "paas-ws-test-op-"


def integration_namespace_prefix() -> str:
    """只接受既有測試前綴或具名 dev sandbox，為 pa- + 16 hex 保留長度。"""
    prefix = os.environ.get("OPERATOR_TEST_NAMESPACE_PREFIX", DEFAULT_TEST_NAMESPACE_PREFIX)
    if len(prefix) > 44 or (
        prefix != DEFAULT_TEST_NAMESPACE_PREFIX
        and not re.fullmatch(r"dev-sandbox-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-", prefix)
    ):
        raise ValueError(
            "Invalid integration namespace prefix: expected paas-ws-test-op- or "
            "a named dev-sandbox-* prefix ending in '-', at most 44 characters"
        )
    return prefix


def integration_storage_class() -> dict:
    """只覆寫本次 PVC／DataVolume 的 SC；不改 cluster 預設或 reclaimPolicy。"""
    value = os.environ.get("OPERATOR_TEST_STORAGE_CLASS")
    if value is None:
        return {}
    if len(value) > 253 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", value):
        raise ValueError("Invalid integration StorageClass name")
    return {"storageClassName": value}


def integration_storage_class_yaml(indent: int = 2) -> str:
    """供既有 YAML 字串 fixture 插入；未設定時輸出空字串。"""
    return "".join(
        f"{' ' * indent}{key}: {value}\n"
        for key, value in integration_storage_class().items()
    )
