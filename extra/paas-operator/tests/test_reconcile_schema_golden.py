"""TS-2: ReconcileResponse / OrphanCandidate wire 契約 golden（BUG-8 防 shape drift）。

operator 端產 ReconcileResponse、Odoo client 端（src/services/reconciler.py，TS-3）反序列化。
兩端跨 repo，最易發生「一端改欄位、另一端沒跟」的 wire shape drift（BUG-8 class）。本檔把
ReconcileResponse 的 JSON shape 固化成 checked-in golden：operator 端斷言「序列化 == golden」、
client 端用同一份 golden 反序列化逐欄位讀回 → 任一端改欄位即一端必紅。設計 doc §機械守線。
"""
import json
from pathlib import Path

from src.models.schemas import (
    OrphanCandidate,
    OrphanKind,
    ReconcileRequest,
    ReconcileResponse,
)

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "reconcile_response_golden.json"


def test_reconcile_response_serializes_to_golden():
    """operator 端：由 golden 建模型再序列化，shape 必須 byte-逐欄 == golden（防 wire drift）。"""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    resp = ReconcileResponse(**golden)
    assert resp.model_dump(mode="json") == golden, (
        "ReconcileResponse 序列化 shape 偏離 golden → operator↔Odoo wire 契約漂移（BUG-8）。"
    )


def test_orphan_candidate_fields_read_back_from_golden():
    """Odoo client 端視角：用同一份 golden 反序列化，逐欄位讀回正確（雙向對齊）。"""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    resp = ReconcileResponse(**golden)
    assert resp.dry_run is True
    assert resp.partial is False
    assert len(resp.orphan_candidates) == 2

    c0 = resp.orphan_candidates[0]
    assert c0.id == "tunnel-abc123"
    assert c0.name == "paas-stg-sm-115223fa-5c25b8a1"
    assert c0.kind == OrphanKind.TUNNEL
    assert c0.env_hint == "stg"
    assert c0.reconcilable is False
    assert c0.reason

    c1 = resp.orphan_candidates[1]
    assert c1.kind == OrphanKind.DNS_RECORD
    assert c1.env_hint == "unknown"


def test_reconcilable_defaults_false():
    """首版零自動刪除：OrphanCandidate.reconcilable 預設 False（不寫就不可回收）。"""
    c = OrphanCandidate(id="x", kind=OrphanKind.DNS_RECORD)
    assert c.reconcilable is False
    assert c.env_hint == "unknown"


def test_dry_run_defaults_true_both_ways():
    """dry-run-first：request 與 response 的 dry_run 都預設 True。"""
    assert ReconcileResponse().dry_run is True
    assert ReconcileResponse().partial is False
    assert ReconcileRequest().dry_run is True
    assert ReconcileRequest().stored_ids == {}


def test_orphan_kind_values_stable():
    """kind 的 enum 值是 wire 契約的一部分，鎖死字面值（改值即 client 對不上）。"""
    assert OrphanKind.TUNNEL.value == "tunnel"
    assert OrphanKind.DNS_RECORD.value == "dns_record"
    assert OrphanKind.ACCESS_APP.value == "access_app"
    assert OrphanKind.ACCESS_POLICY.value == "access_policy"
    assert OrphanKind.NAMESPACE.value == "namespace"
