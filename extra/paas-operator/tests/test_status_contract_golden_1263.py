"""#1263（#1251 Q44）：``GET …/status`` 的 wire 契約 golden（防 Pod 身分欄位 drift）。

與 route golden（#648，``test_route_contract_golden_648.py``）同型：一份 checked-in
golden，由 ``tests/generate_status_contract_golden.py`` **打真的端點**產生；本檔斷言
「現在打同一條路得到的 body == golden」。與 route 那支不同的是，這裡不只做 model
round-trip，而是**重跑整條端點**——身分欄位是 ``get_pods`` 從 kubectl JSON 抽出來
的，round-trip 驗不到抽取邏輯（label key 打錯、owner 取錯筆，schema 形狀都不變）。

平台端（Odoo）由 ``scripts/audit/test_operator_contract_shape.py`` 把
``src/tests/operator_contract.py`` 的 ``status_response()``／``pod_info()`` 對到本
golden 的 key 集合；schema 形狀改了沒重產 golden，由 repo-lint
``status-contract-golden`` 抓（本檔沒有 CI runner）。

重產：
    cd extra/paas-operator
    .venv/bin/python tests/generate_status_contract_golden.py
"""
import json
from pathlib import Path

from tests.generate_status_contract_golden import CASES, build_cases

GOLDEN = (Path(__file__).resolve().parents[3]
          / "src" / "tests" / "fixtures" / "status_contract_golden.json")
_META_KEYS = {"_contract_fingerprint"}


def _golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_golden_carries_a_contract_fingerprint():
    assert _golden().get("_contract_fingerprint"), (
        "golden 缺指紋——手寫或舊腳本產的？重產：tests/generate_status_contract_golden.py")


def test_golden_covers_every_case():
    assert set(_golden()) - _META_KEYS == set(CASES)


def test_endpoint_still_serializes_to_golden():
    """重跑真的端點：身分抽取或序列化任一改變，這裡就紅。"""
    golden = {k: v for k, v in _golden().items() if k not in _META_KEYS}
    assert build_cases() == golden, (
        "GET …/status 的輸出偏離 golden → operator↔Odoo 契約漂移。"
        "是刻意的就重跑 tests/generate_status_contract_golden.py 並同步平台端。")


def test_golden_pins_woow_odoo_identity():
    """golden 本身的內容要對：chart 的 server／PG／backup Job Pod 身分（#1251 Q19）。"""
    case = _golden()["woow_odoo_chart"]
    assert case["pod_identity_included"] is True
    identity = {p["name"]: (p["component"], p["task"], p["owner_kind"]) for p in case["pods"]}
    assert identity["svc-1263-6d8f7c9b5-server"] == ("server", None, "ReplicaSet")
    assert identity["svc-1263-postgresql-0"] == ("postgresql", None, "StatefulSet")
    assert identity["svc-1263-backup-29000000-abcde"] == ("db-backup", "backup", "Job")
