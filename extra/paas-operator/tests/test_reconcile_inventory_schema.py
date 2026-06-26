"""TS-3a: operator → Odoo 唯讀 inventory wire 契約 golden（方向 A）。

方向 A：operator 對 CF 帳號級資源**只做唯讀發現**，回 raw inventory（右集 ground truth），
差集 / 分類 / 刪除全在 Odoo（`src/services/reconciler.py`）。故 operator 端的 wire 是
`InventoryResponse`（未分類），**不是** `ReconcileResponse`（後者是 Odoo 端產出的分類後 inventory）。

本檔把 `InventoryResponse` / `CfResource` 的 JSON shape 固化成 checked-in golden（BUG-8 防 wire
shape drift）：operator 端斷言「序列化 == golden」；Odoo client 端（TS-3d）用同一份 golden 反序列化
逐欄位讀回。任一端改欄位即一端必紅。設計 doc §機械守線（golden 契約）。
"""
import json
from pathlib import Path

from src.models.schemas import CfResource, InventoryResponse, OrphanKind

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "inventory_response_golden.json"


def test_inventory_response_serializes_to_golden():
    """operator 端：由 golden 建模型再序列化，shape 必須逐欄 == golden（防 wire drift）。"""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    resp = InventoryResponse(**golden)
    assert resp.model_dump(mode="json") == golden, (
        "InventoryResponse 序列化 shape 偏離 golden → operator↔Odoo wire 契約漂移（BUG-8）。"
    )


def test_cf_resource_fields_read_back_from_golden():
    """Odoo client 端視角：用同一份 golden 反序列化，逐欄位讀回正確（雙向對齊）。"""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    resp = InventoryResponse(**golden)
    assert resp.paas_env == "stg"
    assert resp.partial is False
    assert resp.failed_kinds == []
    assert len(resp.resources) == 3

    r0 = resp.resources[0]
    assert r0.id == "tun-abc123"
    assert r0.name == "paas-stg-sm-115223fa"
    assert r0.kind == OrphanKind.TUNNEL

    assert resp.resources[1].kind == OrphanKind.ACCESS_APP
    assert resp.resources[2].kind == OrphanKind.ACCESS_POLICY


def test_inventory_defaults_safe():
    """空 inventory：partial 預設 False、resources/failed_kinds 空（最小合法 payload）。"""
    resp = InventoryResponse(paas_env="prod")
    assert resp.partial is False
    assert resp.failed_kinds == []
    assert resp.resources == []


def test_cf_resource_carries_no_classification_fields():
    """方向 A intent-lock：operator inventory 的單筆只回『事實』（id/name/kind），
    **不得**帶 env_hint / reconcilable / reason — 那是 Odoo reconciler.py 的分類職責。
    若有人把分類欄位塞進 operator wire（退回 Direction B），此測試立即紅。"""
    fields = set(CfResource.model_fields.keys())
    assert fields == {"id", "name", "kind"}, (
        f"CfResource 應只含 {{id,name,kind}}，實際 {fields}；分類欄位（env_hint/"
        "reconcilable/reason）屬 Odoo reconciler.py，operator 維持唯讀回事實（方向 A）。"
    )


def test_orphan_kind_values_stable():
    """kind 的 enum 值是 wire 契約一部分（與 ReconcileResponse 共用 enum），鎖死字面值。"""
    assert OrphanKind.TUNNEL.value == "tunnel"
    assert OrphanKind.ACCESS_APP.value == "access_app"
    assert OrphanKind.ACCESS_POLICY.value == "access_policy"
    assert OrphanKind.DNS_RECORD.value == "dns_record"
    assert OrphanKind.NAMESPACE.value == "namespace"
