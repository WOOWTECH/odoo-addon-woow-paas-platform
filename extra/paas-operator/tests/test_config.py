"""L1: PAAS_ENV operator config field (deploy-env primitive for ns env label)."""
import pytest
from pydantic import ValidationError

from src.config import Settings
from src.services.helm import validate_label_value


def test_paas_env_defaults_empty(monkeypatch):
    """未設 PAAS_ENV → 空字串（unknown，啟動不 crash；fail-closed 下推消費點 omit）。"""
    monkeypatch.delenv("PAAS_ENV", raising=False)
    assert Settings(_env_file=None).paas_env == ""


def test_paas_env_reads_env_var(monkeypatch):
    """注入鏈：PAAS_ENV env var → settings.paas_env（驗鏈非 mock 物件）。"""
    monkeypatch.setenv("PAAS_ENV", "prod")
    assert Settings(_env_file=None).paas_env == "prod"
    monkeypatch.setenv("PAAS_ENV", "stg")
    assert Settings(_env_file=None).paas_env == "stg"


@pytest.mark.parametrize("bad", ["staging", "production", "Prod", "PROD", "paas-ws-stg-", "stg "])
def test_paas_env_rejects_invalid_value_fail_closed(monkeypatch, bad):
    """非白名單值（含大寫變體 / prefix 字面）→ config load 即 ValidationError（fail-closed）。

    寧可 operator 啟動 fail-loud，也不要靜默貼錯 env label。
    """
    monkeypatch.setenv("PAAS_ENV", bad)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_paas_env_whitelist_values_are_legal_k8s_label_values():
    """L1↔L2 契約：白名單每個值都是合法 k8s label value（供 ns env label 直接用）。

    防「白名單值帶尾 dash 之類」→ L2 atomic 拒整批（operator #208 類）。
    """
    for v in ("prod", "stg"):
        validate_label_value("paas.woowtech.io/env", v)  # 不得 raise
