"""#1471：n8n init 可以重跑——建 API key 前先刪掉同名（mcp-sidecar）的舊 key。

n8n 2.x 不允許兩把 key 同名；init 重跑（partial 後重試、redeploy 開新一代）時第二次建 key
會收到 "There is already an entry with this name"，服務永遠卡在 initializing。
"""
import json
import re
from unittest.mock import MagicMock

from tests.test_init_error_kind_1263 import _http, _n8n_k8s, _post_init_n8n


def _with_existing_keys(keys, delete_status=200, list_answer=None):
    k8s = _n8n_k8s({("GET", "/rest/api-keys"): list_answer or _http(200, json.dumps({"data": keys}))})
    inner = k8s.exec_in_pod.side_effect
    deleted = []

    def exec_in_pod(namespace, pod_name, container, command, timeout=30):
        script = command[-1]
        method = re.search(r'method:"([A-Z]+)"', script).group(1)
        path = re.search(r'path:"([^"]+)"', script).group(1)
        if method == "DELETE":
            deleted.append(path)
            return _http(delete_status, "{}")
        return inner(namespace, pod_name, container, command, timeout)

    k8s.exec_in_pod.side_effect = exec_in_pod
    return k8s, deleted


def test_rerun_deletes_previous_sidecar_key_then_creates_new_one():
    k8s, deleted = _with_existing_keys([
        {"id": "old1", "label": "mcp-sidecar"},
        {"id": "keep", "label": "external-mcp"},
    ])
    body = _post_init_n8n(k8s).json()

    assert body["success"] is True
    assert body["api_key"] == "key-1263"
    assert deleted == ["/rest/api-keys/old1"], "只刪 init 自己的 key，客戶自建的 key 不能動"


def test_listing_failure_does_not_block_init():
    k8s, deleted = _with_existing_keys([], list_answer=_http(500, "boom"))
    body = _post_init_n8n(k8s).json()

    assert body["success"] is True
    assert deleted == []


def test_delete_failure_falls_through_to_create():
    # 刪不掉就照原路建 key；結果由 n8n 的回應決定（這裡 fake 的建 key 成功）。
    k8s, deleted = _with_existing_keys([{"id": "old1", "label": "mcp-sidecar"}], delete_status=500)
    body = _post_init_n8n(k8s).json()

    assert deleted == ["/rest/api-keys/old1"]
    assert body["success"] is True
