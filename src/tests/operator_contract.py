# -*- coding: utf-8 -*-
"""#1052：``PaaSOperatorClient`` 的測試 contract——唯一一份 response 形狀 factory ＋
受 spec 約束的 client double。

**這個模組刻意不在 ``tests/__init__.py`` 裡註冊**（比照 ``smart_home_fixture.py``）——
它沒有 TestCase，只是給其他測試檔 import 的共用件。模組頂層**不 import odoo**：
``scripts/audit/test_operator_contract_shape.py`` 會用 importlib 以檔案路徑單獨載入它，
對 ``extra/paas-operator/src/models/schemas.py`` 的 pydantic 宣告做 AST 對帳（repo-lint
CI 每張 PR 都跑；Odoo pod 裡沒有 ``extra/``，所以對帳不能放 ``src/tests/``）。

## 為什麼需要它

架構覆核 #1052 量到：63 支測試檔引用 ``get_paas_operator_client``、60 支各自手刻
``MagicMock()``、``spec=``／``autospec`` 命中 0（全 suite 另有 14 檔在別的 seam 用
autospec）；``get_status()`` 的回應形狀被手抄成 4 份 ``_status()``，7 key／8 key 兩種
版本並存。這條 seam 唯一的真事故（BUG-8，``f705b3b4``，body vs Query）修正 commit 動
0 個測試檔——mock 與 client 在錯的 wire 形狀上互相同意。本檔**不碰 wire**（那要靠
``src/tests/test_paas_operator.py`` 與 ``test_tunnel_config_wire_769.py`` 的 transport
層斷言），只補兩格：

1. ``status_response()``：``get_status()`` 回應形狀的**唯一定義處**，頂層 key 集合由
   scripts/audit 對帳到 ``ReleaseStatusResponse``；``pods`` 的元素用 ``pod_info()``
   組（#1263），key 集合對帳到 ``PodInfo``；
2. ``operator_client()``：``create_autospec(PaaSOperatorClient, instance=True)``——測試配了
   一個 client 上不存在的 method 時，是 ``AttributeError`` 而不是 MagicMock 真值假裝通過。

## ⚠ autospec 要打在 client 物件上，不是 patch 上

現有寫法是 ``patch('…get_paas_operator_client', return_value=client)``。在那個 ``patch()``
上加 ``autospec=True`` 只會 spec 到 **factory**（一個接 env 的 function），碰不到它回傳
的 client——這正是 repo 別處會用 autospec、唯獨這條 seam 是 0 的原因。要約束的是
client 本身，所以是 ``create_autospec`` 出來的物件再交給 ``patch(..., return_value=)``。
"""
from unittest.mock import create_autospec

#: ``get_status()`` 回應的頂層 key（＝ ``schemas.py`` ``ReleaseStatusResponse`` 的欄位）。
#: ⚠ 真值來源是 operator 的 pydantic 宣告，不是本 tuple；
#: ``scripts/audit/test_operator_contract_shape.py`` 對帳兩者，漂了就紅。
RELEASE_STATUS_KEYS = (
    'release', 'pods', 'pod_retrieval_error', 'pod_identity_included',
    'vm_included', 'vmis', 'datavolumes', 'vmi_retrieval_error',
    'datavolume_retrieval_error',
)


#: 傳給 ``status_response()`` 的某個 key 時，把那個 key **拿掉**，用來建舊 operator 的
#: 真實形狀：#1263 之前的 operator 根本不送 ``pod_identity_included``（缺 key，不是
#: False）。fail-closed 測試兩種都要覆蓋——只測 False 抓不到
#: ``.get('pod_identity_included', True)`` 這種寫錯的預設。
ABSENT = object()


def pod_info(**overrides):
    """組一顆 operator ``PodInfo`` 形狀的 Pod（#1263）。

    預設是一顆健康、**沒有身分 label／controller owner** 的 Pod（``component``／
    ``task``／``owner_kind`` 皆 None）——#1251 的 readiness 判定要 server／PG 身分時，
    由呼叫端明寫（例：``pod_info(component='server', owner_kind='ReplicaSet')``），
    不在預設裡偷塞「剛好是 server」。
    ⚠ 不認得的 key 直接 ``TypeError``：拼錯的 ``compnent=`` 若被靜默加進去，測試會以為
    自己設了身分、實際 ``component`` 仍是 None。
    """
    pod = {
        'name': 'svc-odoo-0',
        'phase': 'Running',
        'ready': '1/1',
        'restarts': 0,
        'age': '5m',
        'deletion_timestamp': None,
        'component': None,
        'task': None,
        'owner_kind': None,
        'creation_timestamp': None,
        # #1408：spec.containers 的 image 參照；預設空清單＝不觸發 Redeploy 後的核對。
        'images': [],
    }
    unknown = set(overrides) - set(pod)
    if unknown:
        raise TypeError('pod_info() 不認得的欄位：%s' % sorted(unknown))
    pod.update(overrides)
    return pod


def status_response(release_status='deployed', pods=None, **overrides):
    """組一份 operator ``get_status`` 形狀的回應。

    預設模擬「未帶 include_vm」——operator 此時回 ``vm_included: False`` 與 ``vmis: None``
    （不是 ``[]``），這個區別正是 fail-closed 的依據。``pod_identity_included`` 預設
    True（#1263 起的 operator 一律回 True）；模擬舊 operator 用
    ``pod_identity_included=ABSENT``（缺 key），``False`` 只是 schema 預設、wire 上不會出現。
    ⚠ woow-odoo 情境的 ``pods`` 一律用 ``pod_info()`` 組：沒有身分 key 的手寫 Pod 配上
    預設 True，是真 operator 不會送出的組合。``release`` 子物件刻意只給
    ``status``／``revision``（``ReleaseInfo`` 的**子集**，與四份既有 fixture 相同；對帳
    對這一層只驗「是子集」）；需要別的欄位（``chart_version`` 等）由呼叫端 ``overrides``。
    ``pods`` 預設空清單；要一顆健康 pod 的呼叫端自己傳（``test_public_url_dns_gate_648``
    的形狀）。
    """
    body = {
        'release': {'status': release_status, 'revision': 1},
        'pods': pods if pods is not None else [],
        'pod_retrieval_error': None,
        'pod_identity_included': True,
        'vm_included': False,
        'vmis': None,
        'datavolumes': None,
        'vmi_retrieval_error': None,
        'datavolume_retrieval_error': None,
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not ABSENT}


def operator_client(**method_return_values):
    """回傳受 ``PaaSOperatorClient`` spec 約束的 client double。

    ``method_return_values`` 逐一設成對應 method 的 ``return_value``；名字不是 client 上的
    method 時 ``getattr`` 直接 ``AttributeError``——那就是本 helper 存在的理由。
    呼叫端仍可照舊 ``client.create_tunnel.return_value = {...}`` 或
    ``client.foo.side_effect = ...``（autospec instance 支援）。
    """
    # 延遲 import：本模組會被 scripts/audit 在沒有 Odoo 的環境下載入。
    from odoo.addons.woow_paas_platform.services.paas_operator import (
        PaaSOperatorClient,
    )
    client = create_autospec(PaaSOperatorClient, instance=True)
    for name, value in method_return_values.items():
        getattr(client, name).return_value = value
    return client
